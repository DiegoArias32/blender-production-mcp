"""Real-Blender rest-pose retarget matrix (M3.1, audit AUD-002).

Every case builds a procedural source/target pair whose rests differ only where the case says so,
animates the source with large arm/leg motion (so a rest offset cannot hide in tiny motion), runs
propose -> preview through the production transaction, and measures independently, in world space,
the angle between each target bone and its source bone. A case passes when either:

- the retarget is supported, numerically verified and every limb bone points where its source bone
  points (<= DIRECTION_TOLERANCE_DEG), or
- the retarget is explicitly refused with REST_POSE_INCOMPATIBLE when the case expects refusal.
"""
import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import types
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PROMOTED = ROOT / 'test-artifacts' / 'm3.1-audit-regressions'
DIRECTION_TOLERANCE_DEG = 0.1

ROLES = [('root', None, (0, 0, 0), (0, 0, .2)), ('pelvis', 'root', (0, 0, 1), (0, 0, 1.2)),
         ('spine', 'pelvis', (0, 0, 1.2), (0, 0, 1.6)), ('neck', 'spine', (0, 0, 1.6), (0, 0, 1.7)),
         ('head', 'neck', (0, 0, 1.7), (0, 0, 1.95))]
for _side, _sign in (('left', 1), ('right', -1)):
    ROLES += [(_side + '_upper_leg', 'pelvis', (.15 * _sign, 0, 1), (.15 * _sign, -.025, .55)),
              (_side + '_lower_leg', _side + '_upper_leg', (.15 * _sign, -.025, .55), (.15 * _sign, 0, .1)),
              (_side + '_foot', _side + '_lower_leg', (.15 * _sign, 0, .1), (.15 * _sign, -.22, .05)),
              (_side + '_toe', _side + '_foot', (.15 * _sign, -.22, .05), (.15 * _sign, -.35, .05)),
              (_side + '_upper_arm', 'spine', (.1 * _sign, 0, 1.55), (.45 * _sign, 0, 1.55)),
              (_side + '_lower_arm', _side + '_upper_arm', (.45 * _sign, 0, 1.55), (.78 * _sign, 0, 1.55)),
              (_side + '_hand', _side + '_lower_arm', (.78 * _sign, 0, 1.55), (.93 * _sign, 0, 1.55))]

CASES = {
    # name: (source arm drop L/R degrees, target arm drop L/R degrees, extra, expectation)
    'T_to_T': ((0, 0), (0, 0), {}, 'pass'),
    'A_to_A': ((45, 45), (45, 45), {}, 'pass'),
    'T_to_A': ((0, 0), (45, 45), {}, 'pass'),
    'A_to_T': ((45, 45), (0, 0), {}, 'pass'),
    'asymmetric_rest': ((0, 30), (40, 0), {}, 'pass'),
    'rotated_armature_objects': ((0, 0), (45, 45), {'object_rotation_z': 90}, 'pass'),
    'parent_rotation': ((0, 0), (45, 45), {'root_turn': 90}, 'pass'),
    'scale_mismatch': ((0, 0), (45, 45), {'target_scale': 1.2, 'target_object_scale': 1.5,
                                          'scale_mode': 'explicit', 'scale_factor': 1.0}, 'pass'),
    'T_to_A_strict_refused': ((0, 0), (45, 45), {'rest_compensation': 'none'}, 'refuse'),
    'beyond_compensation_limit_refused': ((0, 0), (45, 45), {'rest_compensation_max_degrees': 30}, 'refuse'),
}


def inside(output):
    import bpy
    from mathutils import Matrix, Quaternion, Vector
    sys.path.insert(0, str(ROOT / 'src'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.mocap.service import retarget

    class LocalBridge:
        def run(self, body, params=None):
            namespace = {}
            exec(build_code(body, params).rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    storage = Storage(output / 's')
    try:
        from blender_mcp.production.common.asset_safety import register_fixture
    except ImportError:
        register_fixture = None
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end, scene.render.fps = 1, 21, 30

    def drop(point, sign, degrees):
        # Rotate an arm point around the shoulder, downwards, by the rest drop angle.
        shoulder = Vector((.1 * sign, 0, 1.55))
        rotation = Matrix.Rotation(math.radians(-degrees * sign), 3, 'Y')
        return shoulder + rotation @ (Vector(point) - shoulder)

    def build_rig(name, drops, scale=1.0):
        data = bpy.data.armatures.new(name + 'Data')
        obj = bpy.data.objects.new(name, data)
        scene.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        for role, parent, head, tail in ROLES:
            bone = data.edit_bones.new(name + '_' + role)
            head, tail = Vector(head), Vector(tail)
            if 'arm' in role or 'hand' in role:
                sign = 1 if role.startswith('left') else -1
                degrees = drops[0] if sign > 0 else drops[1]
                head, tail = drop(head, sign, degrees), drop(tail, sign, degrees)
            bone.head, bone.tail = head * scale, tail * scale
            if parent:
                bone.parent = data.edit_bones[name + '_' + parent]
        bpy.ops.object.mode_set(mode='OBJECT')
        for pose in obj.pose.bones:
            pose.rotation_mode = 'QUATERNION'
        return obj

    def animate_source(obj, root_turn):
        obj.animation_data_create()
        action = bpy.data.actions.new(obj.name + '_Mocap')
        obj.animation_data.action = action
        for frame in range(1, 22):
            phase = (frame - 1) / 20
            for role, _, _, _ in ROLES:
                pose = obj.pose.bones[obj.name + '_' + role]
                rest = pose.bone.matrix_local.to_quaternion()
                world = Quaternion()
                if role == 'left_upper_arm':
                    world = Quaternion((0, 1, 0), math.radians(80) * phase)       # swings down
                elif role == 'right_upper_arm':
                    world = Quaternion((0, 1, 0), math.radians(60) * phase)       # swings up
                elif role.endswith('lower_arm'):
                    world = Quaternion((0, 0, 1), math.radians(40) * phase)
                elif role.endswith('upper_leg'):
                    world = Quaternion((1, 0, 0), math.radians(30) * math.sin(phase * math.pi) * (1 if role.startswith('left') else -1))
                elif role == 'root' and root_turn:
                    world = Quaternion((0, 0, 1), math.radians(root_turn) * phase)
                # Armature-space delta for bones whose parents stay at rest; limbs below rotate with them.
                pose.rotation_quaternion = rest.inverted() @ world @ rest if role not in ('root',) else world
                pose.location = (phase * .2, 0, 0) if role == 'root' else (0, 0, 0)
                pose.keyframe_insert(data_path='rotation_quaternion', frame=frame)
                pose.keyframe_insert(data_path='location', frame=frame)
                pose.keyframe_insert(data_path='scale', frame=frame)
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for curve in bag.fcurves:
                        for point in curve.keyframe_points:
                            point.interpolation = 'LINEAR'
        obj.animation_data.action_slot = action.slots[0]

    rigs = {}
    for case, (source_drops, target_drops, extra, expectation) in CASES.items():
        source = build_rig('S' + str(len(rigs)), source_drops)
        target = build_rig('T' + str(len(rigs)), target_drops, extra.get('target_scale', 1.0))
        if extra.get('object_rotation_z'):
            source.rotation_euler.z = target.rotation_euler.z = math.radians(extra['object_rotation_z'])
        if extra.get('target_object_scale'):
            target.scale = (extra['target_object_scale'],) * 3
        animate_source(source, extra.get('root_turn', 0))
        rigs[case] = (source, target)
    bpy.context.view_layer.update()
    path = output / 'm31-retarget-matrix.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    if register_fixture is not None:
        register_fixture(bridge, storage, str(path))

    def direction(obj, bone):
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        pose = evaluated.pose.bones[bone]
        return (evaluated.matrix_world @ pose.tail - evaluated.matrix_world @ pose.head).normalized()

    report = {'blender': bpy.app.version_string, 'tolerance_degrees': DIRECTION_TOLERANCE_DEG, 'cases': {}}
    for case, (source, target) in rigs.items():
        source_drops, target_drops, extra, expectation = CASES[case]
        mapping = [{'source_bone': source.name + '_' + role, 'target_bone': target.name + '_' + role,
                    'semantic_role': role} for role, _, _, _ in ROLES]
        options = {'source_armature': source.name, 'mapping': mapping, 'mapping_approved': True,
                   'frame_start': 1, 'frame_end': 21}
        options.update({k: v for k, v in extra.items() if k in ('rest_compensation', 'rest_compensation_max_degrees',
                                                                 'scale_mode', 'scale_factor')})
        row = {'expectation': expectation, 'source_arm_drop_degrees': source_drops, 'target_arm_drop_degrees': target_drops}
        try:
            proposal = retarget(bridge, storage, 'rm-' + case, target.name, 'propose', options)
            row['rest_compatibility'] = proposal['proposal'].get('rest_compatibility')
            if not proposal['proposal'].get('supported'):
                errors = proposal['proposal'].get('errors', [])
                row.update(supported=False, errors=errors[:6])
                refused = any(e.startswith('REST_POSE_INCOMPATIBLE') for e in errors)
                row['verdict'] = 'pass' if expectation == 'refuse' and refused else 'fail'
            else:
                preview = retarget(bridge, storage, 'rm-' + case + '-p', mode='preview', operation=proposal['operation'])
                row.update(supported=True, numeric_verification=preview.get('numeric_verification'),
                           verification_errors=preview.get('errors'),
                           reported={k: preview.get('metrics', {}).get(k) for k in (
                               'max_position_error_m', 'max_rotation_error_degrees', 'max_bone_direction_error_degrees',
                               'max_root_pelvis_delta_error_degrees', 'verification_frames')})
                original = target.animation_data.action if target.animation_data else None
                target.animation_data_create()
                candidate = bpy.data.actions[preview['working_action']]
                target.animation_data.action = candidate
                target.animation_data.action_slot = candidate.slots[0]
                worst = 0.0
                for frame in (1, 6, 11, 16, 21):
                    scene.frame_set(frame)
                    for role, _, _, _ in ROLES:
                        if role in ('root', 'pelvis'):
                            continue
                        worst = max(worst, math.degrees(direction(source, source.name + '_' + role).angle(
                            direction(target, target.name + '_' + role), 0)))
                target.animation_data.action = original
                scene.frame_set(1)
                row['independent_max_direction_error_degrees'] = worst
                ok = preview.get('numeric_verification') == 'pass' and worst <= DIRECTION_TOLERANCE_DEG
                row['verdict'] = 'pass' if expectation == 'pass' and ok else 'fail'
        except Exception as exc:
            row.update(verdict='error', exception=repr(exc), traceback=traceback.format_exc()[-2500:])
        report['cases'][case] = row
        print('CASE', case, row['verdict'], flush=True)
    report['summary'] = {case: row['verdict'] for case, row in report['cases'].items()}
    (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    print('M31_RETARGET_REPORT=' + str(output / 'report.json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm31' / ('retarget-matrix-' + run_id)
    output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output)],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'stderr.txt').write_text(process.stderr, encoding='utf-8')
    print('\n'.join(line for line in process.stdout.splitlines() if line.startswith(('CASE', 'M31_'))))
    report = output / 'report.json'
    if not report.exists():
        print(process.stdout[-4000:]); print(process.stderr[-4000:])
        raise SystemExit(1)
    PROMOTED.mkdir(parents=True, exist_ok=True)
    promoted = PROMOTED / ('retarget-matrix-' + run_id + '.json')
    shutil.copyfile(report, promoted)
    print('PROMOTED=' + str(promoted))
    data = json.loads(report.read_text(encoding='utf-8'))
    if any(v != 'pass' for v in data['summary'].values()):
        raise SystemExit(2)


if __name__ == '__main__':
    if '--inside' in sys.argv:
        inside(Path(sys.argv[sys.argv.index('--inside') + 1]))
    else:
        main()
