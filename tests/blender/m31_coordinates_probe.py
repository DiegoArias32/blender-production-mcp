"""Phase 24: scale / coordinate / FPS regressions for the IK/FK match, judged independently.

For every configuration the M3 rig fixture is rebuilt, transformed, and an FK->IK single-frame match
is proposed and previewed. The judgement does not reuse production verification:
- at the match frame, the candidate chain (IK) must reproduce the source chain (FK) world positions;
- outside the frame, every bone of the candidate must stay where the source puts it.
Tolerances scale with the object's largest absolute scale (positions are in world meters).
A configuration is ``safe`` when the result is correct or the operation is refused, ``unsafe_pass``
when a numeric pass hides a wrong pose.
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
PROMOTED = ROOT / 'test-artifacts' / 'm3.1-regressions'
CONFIGS = {
    'identity': {},
    'uniform_scale_0p01': {'scale': (0.01, 0.01, 0.01)},
    'uniform_scale_3': {'scale': (3.0, 3.0, 3.0)},
    'negative_scale_x': {'scale': (-1.0, 1.0, 1.0)},
    'non_uniform_scale_1_2_1': {'scale': (1.0, 2.0, 1.0)},
    'rotated_armature': {'rotation': (math.radians(30), 0.0, math.radians(90))},
    'rotated_parent': {'parent_rotation': (0.3, 0.2, 1.0), 'parent_location': (1.0, -2.0, 0.5)},
    'fps_60': {'fps': (60, 1.0)},
    'fps_29p97': {'fps': (30, 1.001)},
    'unit_scale_0p01': {'unit_scale': 0.01},
}


def inside(output, label):
    import bpy
    from mathutils import Euler
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.rig.ik_fk import operate
    from m3_rig_fixtures import build as build_rig
    from m31_fixture_support import register_open_fixture

    class LocalBridge:
        def run(self, body, params=None):
            namespace = {}
            exec(build_code(body, params).rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    report = {'label': label, 'blender': bpy.app.version_string, 'configurations': {}}

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

    def world_rows(rig, action, frames):
        rig.animation_data.action = action
        rig.animation_data.action_slot = action.slots[0]
        unit = bpy.context.scene.unit_settings.scale_length
        rows = {}
        for frame in frames:
            bpy.context.scene.frame_set(frame)
            evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
            rows[frame] = {p.name: ((evaluated.matrix_world @ p.head) * unit, (evaluated.matrix_world @ p.tail) * unit)
                           for p in evaluated.pose.bones}
        return rows

    def deviation(a, b, names=None):
        return max(max((a[n][0] - b[n][0]).length, (a[n][1] - b[n][1]).length) for n in (names or a))

    # The builder needs a fresh session: build once, then reopen the saved fixture for every case.
    fixture = Path(build_rig(output / 'rig')['blend'])
    for name, config in CONFIGS.items():
        folder = output / name
        folder.mkdir()
        try:
            bpy.ops.wm.open_mainfile(filepath=str(fixture), use_scripts=False, load_ui=False)
            storage = Storage(folder / 's')
            register_open_fixture(bridge, storage, output)
            rig = bpy.data.objects['M3_MixedRig']
            scene = bpy.context.scene
            if 'scale' in config:
                rig.scale = config['scale']
            if 'rotation' in config:
                rig.rotation_euler = Euler(config['rotation'])
            if 'parent_rotation' in config:
                empty = bpy.data.objects.new('M31_Parent', None)
                scene.collection.objects.link(empty)
                empty.rotation_euler = Euler(config['parent_rotation'])
                empty.location = config['parent_location']
                rig.parent = empty
            if 'fps' in config:
                scene.render.fps, scene.render.fps_base = config['fps']
            if 'unit_scale' in config:
                scene.unit_settings.scale_length = config['unit_scale']
            bpy.context.view_layer.update()
            source = rig.animation_data.action
            chain = ['UpperLeg.R', 'LowerLeg.R', 'Foot.R']
            options = {'chain': chain, 'ik_target': 'ContactControl.R', 'pole_target': 'PoleControl.R',
                       'confirm_mapping': True, 'frame': 5}
            size = max(abs(v) for v in rig.matrix_world.to_scale()) * scene.unit_settings.scale_length
            tolerance = 1e-4 * max(1.0, size)
            row = {'config': config, 'tolerance_m': tolerance, 'determinant': rig.matrix_world.determinant()}
            proposal = operate(bridge, storage, name, 'fk_to_ik', False, rig.name, 'propose', options)
            row['proposal_supported'] = proposal['proposal'].get('supported')
            if not row['proposal_supported']:
                row.update(verdict='safe', refused=True, reasons=proposal['proposal'].get('reasons'))
            else:
                preview = operate(bridge, storage, name + 'p', 'fk_to_ik', False, mode='preview', operation=proposal['operation'])
                row['numeric_verification'] = preview.get('numeric_verification')
                row['errors'] = (preview.get('errors') or [])[:4]
                frames = list(range(1, 10))
                before = world_rows(rig, source, frames)
                after = world_rows(rig, bpy.data.actions[preview['working_action']], frames)
                rig.animation_data.action = source
                rig.animation_data.action_slot = source.slots[0]
                row['match_frame_chain_deviation_m'] = deviation(before[5], after[5], chain)
                row['outside_max_deviation_m'] = max(deviation(before[f], after[f]) for f in frames if f != 5)
                correct = row['match_frame_chain_deviation_m'] <= tolerance and row['outside_max_deviation_m'] <= tolerance
                if row['numeric_verification'] == 'pass':
                    row['verdict'] = 'safe' if correct else 'unsafe_pass'
                else:
                    row['verdict'] = 'safe'  # refused at verification: fail-closed
                row['independently_correct'] = correct
        except Exception as exc:
            row = {'config': config, 'verdict': 'error', 'exception': repr(exc), 'traceback': traceback.format_exc()[-2000:]}
        report['configurations'][name] = row
        save()
        print('CONFIG', name, row.get('verdict'), row.get('numeric_verification'), row.get('match_frame_chain_deviation_m'), flush=True)
    report['passed'] = all(r.get('verdict') == 'safe' for r in report['configurations'].values())
    save()
    print('M31_COORDINATES_REPORT=' + str(output / 'report.json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--label', default='coordinates')
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm31' / (args.label + '-' + run_id)
    output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output), args.label],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'stderr.txt').write_text(process.stderr, encoding='utf-8')
    print('\n'.join(line for line in process.stdout.splitlines() if line.startswith(('CONFIG', 'M31_'))))
    report_path = output / 'report.json'
    if not report_path.exists():
        print(process.stdout[-4000:])
        print(process.stderr[-4000:])
        raise SystemExit(1)
    PROMOTED.mkdir(parents=True, exist_ok=True)
    promoted = PROMOTED / (args.label + '-' + run_id + '.json')
    shutil.copyfile(report_path, promoted)
    print('PROMOTED=' + str(promoted))
    raise SystemExit(0 if json.loads(report_path.read_text(encoding='utf-8')).get('passed') else 1)


if __name__ == '__main__':
    if '--inside' in sys.argv:
        position = sys.argv.index('--inside')
        inside(Path(sys.argv[position + 1]), sys.argv[position + 2])
    else:
        main()
