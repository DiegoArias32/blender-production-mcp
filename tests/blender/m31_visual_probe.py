"""Phase 28 focused visual QA for the corrected HIGH cases (evidence for a human/agent review).

Cases (same setups as m31_audit_probe.py, current code):
- ikfk_window: FK->IK bake frames 3-6 on the M3 rig fixture; clip 1-9 rendered every frame so the
  transition into and out of the window can be inspected, not only a still;
- retarget_rest: T-pose source arm swing retargeted to the A-pose target (AUD-002 setup);
- contact_inverted: IK foot correction computed on an inverted-normal floor (AUD-005b setup).

Each case renders source vs candidate with qa_compare_before_after (Workbench on the GPU, identical
orthographic cameras FRONT/SIDE/THREE_QUARTER) and writes, per view, a filmstrip PNG: row 1 source,
row 2 candidate, one column per rendered frame. Numbers are not a visual verdict; the filmstrips are
what a reviewer looks at.
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
PROMOTED = ROOT / 'test-artifacts' / 'm3.1-visual'


def inside(output, label):
    import bpy
    from mathutils import Quaternion
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.qa.multiview import compare_before_after
    from blender_mcp.production.qa.png_montage import write_montage
    from m31_fixture_support import register_open_fixture

    class LocalBridge:
        def run(self, body, params=None):
            namespace = {}
            exec(build_code(body, params).rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    report = {'label': label, 'blender': bpy.app.version_string, 'cases': {}}

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

    def render(name, storage, obj, before, after, options):
        result = compare_before_after(bridge, storage, 'vis-' + name, obj, before, after,
                                      dict({'width': 320, 'height': 320, 'views': ['FRONT', 'SIDE', 'THREE_QUARTER'],
                                            'quality': 'lightweight', 'montage': True}, **options))
        strips = {}
        for view in options.get('views', ('FRONT', 'SIDE', 'THREE_QUARTER')):
            rows = []
            for label_ in ('before', 'after'):
                series = next(s for s in result['series'] if s['label'] == label_ and s['view'] == view)
                rows.append([(item['frame'], Path(item['path'])) for item in series['files']])
            strips[view] = []
            # The production montage writer is capped at 3x2: consecutive 3-frame blocks, source over candidate.
            for start in range(0, len(rows[0]), 3):
                block = [row[start:start + 3] for row in rows]
                if len(block[0]) < 3:
                    block = [row[-3:] for row in rows] if len(rows[0]) >= 3 else block
                frames_ = [frame for frame, _ in block[0]]
                strip = output / ('%s-%s-frames-%s-%s-filmstrip.png' % (name, view, frames_[0], frames_[-1]))
                write_montage([[path for _, path in row] for row in block], strip)
                strips[view].append(str(strip))
        frames = [item['frame'] for item in result['series'][0]['files']]
        return {'status': result.get('status'), 'comparable': result.get('comparable'), 'frames': frames,
                'render_backend': result.get('render_backend'), 'montage': (result.get('montage') or {}).get('path'),
                'filmstrips': strips, 'state_restored': result.get('state_restored'), 'errors': result.get('errors')}

    def case(name):
        def wrap(function):
            try:
                report['cases'][name] = function()
            except Exception as exc:
                report['cases'][name] = {'status': 'error', 'exception': repr(exc), 'traceback': traceback.format_exc()[-2500:]}
            save()
            print('CASE', name, report['cases'][name].get('status'), flush=True)
        return wrap

    @case('ikfk_window')
    def _():
        from m3_rig_fixtures import build as build_rig
        from blender_mcp.production.rig.ik_fk import operate
        build_rig(output / 'rig')
        storage = Storage(output / 'rig-s')
        register_open_fixture(bridge, storage, output)
        rig = bpy.data.objects['M3_MixedRig']
        source = rig.animation_data.action.name
        options = {'chain': ['UpperLeg.R', 'LowerLeg.R', 'Foot.R'], 'ik_target': 'ContactControl.R',
                   'pole_target': 'PoleControl.R', 'confirm_mapping': True, 'frame_start': 3, 'frame_end': 6, 'step': 1}
        proposal = operate(bridge, storage, 'vis-ik', 'fk_to_ik', True, rig.name, 'propose', options)
        preview = operate(bridge, storage, 'vis-ikp', 'fk_to_ik', True, mode='preview', operation=proposal['operation'])
        row = render('ikfk_window', storage, rig.name, source, preview['working_action'],
                     {'frame_start': 1, 'frame_end': 9, 'step': 1, 'max_frames': 9,
                      'framing_bones': ['UpperLeg.R', 'LowerLeg.R', 'Foot.R', 'UpperLeg.L', 'LowerLeg.L', 'Foot.L']})
        row.update(numeric_verification=preview.get('numeric_verification'), window=[3, 6],
                   mutation_window=proposal['proposal'].get('mutation_window'))
        return row

    @case('retarget_rest')
    def _():
        from m3_mocap_fixtures import build as build_mocap
        from blender_mcp.production.mocap.service import retarget
        bpy.ops.wm.read_factory_settings(use_empty=True)
        (output / 'mocap').mkdir()
        fx = build_mocap(output / 'mocap')
        storage = Storage(output / 'mocap-s')
        register_open_fixture(bridge, storage, output)
        source = bpy.data.objects[fx['source']]
        roles = {row['semantic_role']: row for row in fx['mapping']}
        swing = bpy.data.actions[fx['source_action']].copy()
        swing.name = 'M31_LeftArmDown'
        swing.use_fake_user = True
        arm = source.pose.bones[roles['left_upper_arm']['source_bone']]
        rest = arm.bone.matrix_local.to_quaternion()
        path = arm.path_from_id('rotation_quaternion')
        for layer in swing.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for curve in bag.fcurves:
                        if curve.data_path == path:
                            for point in curve.keyframe_points:
                                angle = math.radians(80) * (point.co.x - 1) / 20
                                point.co.y = (rest.inverted() @ Quaternion((0, 1, 0), angle) @ rest)[curve.array_index]
        source.animation_data.action = swing
        source.animation_data.action_slot = swing.slots[0]
        options = {'source_armature': fx['source'], 'mapping': fx['mapping'], 'mapping_approved': True,
                   'frame_start': 1, 'frame_end': 21}
        proposal = retarget(bridge, storage, 'vis-rt', fx['target'], 'propose', options)
        preview = retarget(bridge, storage, 'vis-rtp', mode='preview', operation=proposal['operation'])
        row = render('retarget_rest', storage, fx['target'], fx['target_action'], preview['working_action'],
                     {'frame_start': 1, 'frame_end': 21, 'step': 4, 'max_frames': 6})
        row.update(numeric_verification=preview.get('numeric_verification'),
                   rest_compensation=(proposal['proposal'].get('rest_compensation') or {}).get('compensated_bones'))
        return row

    @case('contact_inverted')
    def _():
        import bmesh
        from m3_contact_fixtures import build as build_contacts
        from blender_mcp.production.contacts.service import fix_contacts
        bpy.ops.wm.read_factory_settings(use_empty=True)
        fixture = build_contacts(output / 'contacts')
        storage = Storage(output / 'contacts-s')
        register_open_fixture(bridge, storage, output)
        surface = bpy.data.objects[fixture['options']['surfaces'][0]]

        def flip():
            mesh = bmesh.new()
            mesh.from_mesh(surface.data)
            bmesh.ops.reverse_faces(mesh, faces=mesh.faces)
            mesh.to_mesh(surface.data)
            mesh.free()
            surface.data.update()
        rig = bpy.data.objects[fixture['rig']]
        source = rig.animation_data.action.name
        flip()
        try:
            proposal = fix_contacts(bridge, storage, 'vis-ct', fixture['rig'], 'propose', dict(fixture['options'], confirm_contacts=True))
            preview = fix_contacts(bridge, storage, 'vis-ctp', mode='preview', operation=proposal['operation'])
        finally:
            flip()
        scene = bpy.context.scene
        span = scene.frame_end - scene.frame_start
        row = render('contact_inverted', storage, rig.name, source, preview['working_action'],
                     {'frame_start': scene.frame_start, 'frame_end': scene.frame_end, 'step': max(1, math.ceil(span / 11)),
                      'max_frames': 12})
        # A ~1 cm drift is 2-3 px at full-body framing: a close foot view is the reviewable evidence.
        feet = [foot['bone'] for foot in fixture['options']['feet']]
        row['foot_closeup'] = render('contact_inverted_foot', storage, rig.name, source, preview['working_action'],
                                     {'frame_start': scene.frame_start, 'frame_end': scene.frame_end,
                                      'step': max(1, math.ceil(span / 11)), 'max_frames': 12, 'views': ['FOOT', 'SIDE', 'FRONT'],
                                      'framing_bones': feet, 'framing_padding_m': 0.05})
        metrics = preview.get('metrics', {})
        row.update(numeric_verification=preview.get('numeric_verification'),
                   sliding_before_m=metrics.get('sliding_before_m'), sliding_after_m=metrics.get('sliding_after_m'))
        return row

    save()
    print('M31_VISUAL_REPORT=' + str(output / 'report.json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--label', default='visual')
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm31' / (args.label + '-' + run_id)
    output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output), args.label],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'stderr.txt').write_text(process.stderr, encoding='utf-8')
    print('\n'.join(line for line in process.stdout.splitlines() if line.startswith(('CASE', 'M31_'))))
    report_path = output / 'report.json'
    if not report_path.exists():
        print(process.stdout[-4000:])
        print(process.stderr[-4000:])
        raise SystemExit(1)
    PROMOTED.mkdir(parents=True, exist_ok=True)
    folder = PROMOTED / (args.label + '-' + run_id)
    folder.mkdir()
    shutil.copyfile(report_path, folder / 'report.json')
    for strip in output.glob('*-filmstrip.png'):
        shutil.copyfile(strip, folder / strip.name)  # small review images are evidence; renders stay in %TEMP%
    print('PROMOTED=' + str(folder))


if __name__ == '__main__':
    if '--inside' in sys.argv:
        position = sys.argv.index('--inside')
        inside(Path(sys.argv[position + 1]), sys.argv[position + 2])
    else:
        main()
