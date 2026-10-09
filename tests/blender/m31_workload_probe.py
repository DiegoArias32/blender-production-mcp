"""Phase 25 synthetic large-workload measurement (scalability, not artistic quality).

Generated in memory, never a production character:
- a 120-bone rig, 1001 frames (24 fps);
- LoadDense: every bone's quaternion keyed on every frame (480 480 keys);
- LoadMedium: 24 bones keyed on every frame (96 096 keys, just under the 100 000 snapshot limit);
- LoadSparse: every bone keyed every 10 frames (48 480 keys over the full 1001-frame range);
- the M3 IK/FK rig fixture with 150 extra bones, and the M3 mocap fixture over long ranges.

Each measurement records wall time, peak Python allocation inside Blender (tracemalloc, second pass),
result size in JSON bytes and time as a fraction of the 180 s socket timeout. Capacity refusals are
recorded as outcomes: the point is that they happen at propose time, before any checkpoint.
The 150k-triangle mesh signature benchmark lives in m31_mesh_probe.py.
"""
import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
import traceback
import types
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PROMOTED = ROOT / 'test-artifacts' / 'm3.1-workload'
TIMEOUT_S = 180.0


def build_rig(bpy, bones=120, frames=1001):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, frames
    scene.render.fps, scene.render.fps_base = 24, 1.0
    data = bpy.data.armatures.new('LoadRigData')
    rig = bpy.data.objects.new('LoadRig', data)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    root = data.edit_bones.new('Root')
    root.head, root.tail = (0, 0, 0), (0, 0, 0.1)
    names = ['Root']
    chains, per_chain = 7, (bones - 1) // 7
    for chain in range(chains):
        parent = root
        angle = 2 * math.pi * chain / chains
        for link in range(per_chain):
            bone = data.edit_bones.new('C%d_%02d' % (chain, link))
            start = parent.tail
            bone.head = start
            bone.tail = (start[0] + 0.05 * math.cos(angle), start[1] + 0.05 * math.sin(angle), start[2] + 0.02)
            bone.parent = parent
            bone.use_connect = True
            parent = bone
            names.append(bone.name)
    bpy.ops.object.mode_set(mode='OBJECT')
    for pose in rig.pose.bones:
        pose.rotation_mode = 'QUATERNION'
    rig.animation_data_create()
    return rig, names


def keyed_action(bpy, rig, name, bones, frames, every):
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    rig.animation_data.action = action
    times = list(range(1, frames + 1, every))
    for number, bone in enumerate(bones):
        path = 'pose.bones["%s"].rotation_quaternion' % bone
        for index in range(4):
            curve = action.fcurve_ensure_for_datablock(rig, path, index=index)
            curve.keyframe_points.add(len(times))
            flat = []
            for frame in times:
                half = 0.25 * math.sin(frame * 0.05 + number)
                value = (math.cos(half), math.sin(half), 0.0, 0.0)[index]
                flat.extend((float(frame), value))
            curve.keyframe_points.foreach_set('co', flat)
            curve.update()
    return action


def inside(output, label):
    import bpy
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.animation.service import analyze, detect_quaternion_flips
    from blender_mcp.production.animation.curve_ops import analyze_fcurves
    from blender_mcp.production.animation.corrections import correction, runtime

    class LocalBridge:
        def run(self, body, params=None):
            namespace = {}
            exec(build_code(body, params).rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    report = {'label': label, 'blender': bpy.app.version_string, 'python': sys.version.split()[0],
              'socket_timeout_s': TIMEOUT_S, 'measurements': {}}

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

    def measure(name, function, memory=True, **context):
        row = dict(context)
        started = time.perf_counter()
        try:
            value = function()
            row['outcome'] = 'completed'
        except Exception as exc:  # capacity refusals are outcomes, recorded verbatim
            value = None
            row.update(outcome='refused' if 'CAPACITY_EXCEEDED' in str(exc) or 'exceeds' in str(exc).lower() else 'error',
                       error=str(exc)[:400])
        row['seconds'] = round(time.perf_counter() - started, 3)
        row['timeout_fraction'] = round(row['seconds'] / TIMEOUT_S, 4)
        if value is not None:
            row['result_json_bytes'] = len(json.dumps(value, default=str))
        if memory and row['outcome'] == 'completed':
            tracemalloc.start()
            try:
                function()
                row['peak_python_alloc_bytes'] = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        report['measurements'][name] = row
        save()
        print('MEASURE', name, row['outcome'], row['seconds'], flush=True)
        return value

    try:
        rig, names = build_rig(bpy)
        dense = keyed_action(bpy, rig, 'LoadDense', names, 1001, 1)
        medium = keyed_action(bpy, rig, 'LoadMedium', names[:24], 1001, 1)
        sparse = keyed_action(bpy, rig, 'LoadSparse', names, 1001, 10)
        report['workload'] = {'bones': len(rig.pose.bones), 'frames': 1001,
                              'keys': {a.name: sum(len(c.keyframe_points) for c in a.layers[0].strips[0].channelbags[0].fcurves)
                                       for a in (dense, medium, sparse)}}
        save()

        rig.animation_data.action = dense
        rig.animation_data.action_slot = dense.slots[0]
        measure('analyze_summary_dense_480k_keys', lambda: analyze(bridge, action_name='LoadDense'))
        measure('quaternion_flips_dense_480k_keys', lambda: detect_quaternion_flips(bridge, action_name='LoadDense'))
        measure('fcurve_analysis_dense_480k_keys', lambda: analyze_fcurves(bridge, 'LoadRig', {}), memory=False)
        snapshot = runtime('retime') + (
            "\naction = bpy.data.actions[params['action']]"
            "\nsnap = m2_snapshot(action)"
            "\nresult = {'keys': sum(len(c['keys']) for c in snap['channels']),"
            " 'snapshot_json_bytes': len(json.dumps(snap, separators=(',', ':'))), 'digest': m2_digest(snap)}")
        measure('snapshot_and_digest_dense_480k_keys', lambda: bridge.run(snapshot, {'action': 'LoadDense'}), memory=False)
        medium_result = measure('snapshot_and_digest_medium_96k_keys', lambda: bridge.run(snapshot, {'action': 'LoadMedium'}))
        if medium_result:
            report['measurements']['snapshot_and_digest_medium_96k_keys'].update(
                keys=medium_result['keys'], snapshot_json_bytes=medium_result['snapshot_json_bytes'],
                bytes_per_key=round(medium_result['snapshot_json_bytes'] / medium_result['keys'], 1))

        rig.animation_data.action = sparse
        rig.animation_data.action_slot = sparse.slots[0]
        storage = Storage(output / 's')
        proposal = measure('retime_propose_120_bones_1001_frames',
                           lambda: correction(bridge, storage, 'wl-retime', 'retime', 'LoadRig', 'propose', {'scale_factor': 1.1}))
        if proposal:
            report['measurements']['retime_propose_120_bones_1001_frames'].update(
                supported=proposal['proposal'].get('supported'), capacity=proposal['proposal'].get('capacity'),
                capacity_warnings=[w for w in proposal['proposal'].get('warnings', []) if 'CAPACITY' in w],
                checkpoints_created=(storage.root / 'checkpoints').exists())

        world = ("import bpy\nobj = bpy.data.objects[params['name']]\nrows = {}\n"
                 "for frame in range(1, params['frames'] + 1):\n"
                 "    bpy.context.scene.frame_set(frame)\n"
                 "    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())\n"
                 "    rows[frame] = {p.name: (evaluated.matrix_world @ p.head, evaluated.matrix_world @ p.tail,"
                 " (evaluated.matrix_world @ p.matrix).to_quaternion().normalized()) for p in evaluated.pose.bones}\n"
                 "result = {'bone_frames': sum(len(r) for r in rows.values())}\n")
        world_result = measure('world_rows_120_bones_x_200_frames', lambda: bridge.run(world, {'name': 'LoadRig', 'frames': 200}))
        if world_result:
            row = report['measurements']['world_rows_120_bones_x_200_frames']
            row['bone_frames'] = world_result['bone_frames']
            row['bone_frames_per_second'] = round(world_result['bone_frames'] / row['seconds'], 1)
            if row.get('peak_python_alloc_bytes'):
                row['alloc_bytes_per_bone_frame'] = round(row['peak_python_alloc_bytes'] / world_result['bone_frames'], 1)

        # ------------------------------------------------ IK/FK on the M3 rig fixture plus 150 bones
        from m3_rig_fixtures import build as build_m3_rig
        from blender_mcp.production.rig.ik_fk import operate
        from blender_mcp.production.common.asset_safety import register_fixture
        folder = output / 'rig'
        folder.mkdir()
        manifest = build_m3_rig(folder)
        register_fixture(bridge, Storage(output / 'r'), str(manifest['blend']), allowed_roots=[str(output)])
        rig3 = bpy.data.objects['M3_MixedRig']
        base = {'chain': ['UpperLeg.R', 'LowerLeg.R', 'Foot.R'], 'ik_target': 'ContactControl.R',
                'pole_target': 'PoleControl.R', 'confirm_mapping': True}
        control = measure('ikfk_bake_propose_fixture_rig_1_9',
                          lambda: operate(bridge, Storage(output / 'r'), 'wl-ik0', 'fk_to_ik', True, 'M3_MixedRig', 'propose',
                                          dict(base, frame_start=1, frame_end=9, step=1)), memory=False)
        if control:
            report['measurements']['ikfk_bake_propose_fixture_rig_1_9'].update(
                supported=control['proposal'].get('supported'), capacity=control['proposal'].get('capacity'),
                bones=len(rig3.pose.bones))
        # Operable bake window on the fixture rig: payload size grows with the window (phase 25).
        sweep = []
        for end in (16, 31, 46, 61, 91, 121):
            bpy.context.scene.frame_end = max(bpy.context.scene.frame_end, end)
            result = operate(bridge, Storage(output / 'sw'), 'wl-sw%d' % end, 'fk_to_ik', True, 'M3_MixedRig', 'propose',
                             dict(base, frame_start=1, frame_end=end, step=1))
            capacity = result['proposal'].get('capacity') or {}
            sweep.append({'window_frames': end, 'supported': result['proposal'].get('supported'),
                          'payload_bytes': capacity.get('payload_bytes'), 'reasons': result['proposal'].get('reasons')})
        report['measurements']['ikfk_bake_window_sweep_fixture_rig'] = {'outcome': 'completed', 'bones': len(rig3.pose.bones), 'rows': sweep}
        save()
        bpy.context.view_layer.objects.active = rig3
        bpy.ops.object.mode_set(mode='EDIT')
        anchor = rig3.data.edit_bones[0]
        for index in range(150):
            extra = rig3.data.edit_bones.new('Extra_%03d' % index)
            extra.head = (0.01 * index, 1.0, 0.0)
            extra.tail = (0.01 * index, 1.0, 0.05)
            extra.parent = anchor
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.context.scene.frame_end = max(bpy.context.scene.frame_end, 241)
        large = measure('ikfk_bake_propose_plus_150_bones_1_241',
                        lambda: operate(bridge, Storage(output / 'r'), 'wl-ik1', 'fk_to_ik', True, 'M3_MixedRig', 'propose',
                                        dict(base, frame_start=1, frame_end=241, step=1)), memory=False)
        if large:
            report['measurements']['ikfk_bake_propose_plus_150_bones_1_241'].update(
                supported=large['proposal'].get('supported'), capacity=large['proposal'].get('capacity'),
                reasons=large['proposal'].get('reasons'), bones=len(rig3.pose.bones),
                checkpoints_created=((output / 'r') / 'checkpoints').exists())

        if large and large['proposal'].get('supported'):
            # Real verification cost of the largest supported bake (timeout risk).
            preview = measure('ikfk_bake_preview_plus_150_bones_1_241',
                              lambda: operate(bridge, Storage(output / 'r'), 'wl-ik1p', 'fk_to_ik', True, mode='preview',
                                              operation=large['operation']), memory=False)
            if preview:
                report['measurements']['ikfk_bake_preview_plus_150_bones_1_241'].update(
                    numeric_verification=preview.get('numeric_verification'), status=preview.get('status'))
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, 400
        wide = measure('ikfk_bake_propose_plus_150_bones_100_340_scene_1_400',
                       lambda: operate(bridge, Storage(output / 'r2'), 'wl-ik2', 'fk_to_ik', True, 'M3_MixedRig', 'propose',
                                       dict(base, frame_start=100, frame_end=340, step=1)), memory=False)
        if wide:
            report['measurements']['ikfk_bake_propose_plus_150_bones_100_340_scene_1_400'].update(
                supported=wide['proposal'].get('supported'), capacity=wide['proposal'].get('capacity'),
                reasons=wide['proposal'].get('reasons'), checkpoints_created=((output / 'r2') / 'checkpoints').exists())

        # ------------------------------------------------ mocap retarget over long ranges
        from m3_mocap_fixtures import build as build_mocap
        from blender_mcp.production.mocap.service import retarget
        (output / 'mocap').mkdir()
        fx = build_mocap(output / 'mocap')
        storage_m = Storage(output / 'm')
        register_fixture(bridge, storage_m, str(fx['path']), allowed_roots=[str(output)])
        for frame_end in (11, 21):
            bpy.context.scene.frame_end = max(bpy.context.scene.frame_end, frame_end)
            key = 'mocap_retarget_propose_1_%d' % frame_end
            proposal = measure(key, lambda: retarget(bridge, storage_m, 'wl-mc%d' % frame_end, fx['target'], 'propose',
                                                     {'source_armature': fx['source'], 'mapping': fx['mapping'],
                                                      'mapping_approved': True, 'frame_start': 1, 'frame_end': frame_end}),
                               memory=False, mapped_bones=len(fx['mapping']))
            if proposal:
                report['measurements'][key].update(supported=proposal['proposal'].get('supported'),
                                                   capacity=proposal['proposal'].get('capacity'),
                                                   errors=[e for e in proposal['proposal'].get('errors', []) if 'CAPACITY' in e],
                                                   checkpoints_created=(storage_m.root / 'checkpoints').exists())
                if frame_end == 1001 and proposal['proposal'].get('supported'):
                    preview = measure('mocap_retarget_preview_1_1001', lambda: retarget(bridge, storage_m, 'wl-mcp', mode='preview',
                                                                                     operation=proposal['operation']), memory=False)
                    if preview:
                        report['measurements']['mocap_retarget_preview_1_1001'].update(
                            numeric_verification=preview.get('numeric_verification'), status=preview.get('status'),
                            errors=(preview.get('errors') or [])[:3])
    except Exception as exc:
        report['exception'] = repr(exc)
        report['traceback'] = traceback.format_exc()[-4000:]
    save()
    print('M31_WORKLOAD_REPORT=' + str(output / 'report.json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--label', default='workload')
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm31' / (args.label + '-' + run_id)
    output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output), args.label],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'stderr.txt').write_text(process.stderr, encoding='utf-8')
    print('\n'.join(line for line in process.stdout.splitlines() if line.startswith(('MEASURE', 'M31_'))))
    report_path = output / 'report.json'
    if not report_path.exists():
        print(process.stdout[-4000:])
        print(process.stderr[-4000:])
        raise SystemExit(1)
    PROMOTED.mkdir(parents=True, exist_ok=True)
    promoted = PROMOTED / (args.label + '-' + run_id + '.json')
    shutil.copyfile(report_path, promoted)
    print('PROMOTED=' + str(promoted))


if __name__ == '__main__':
    if '--inside' in sys.argv:
        position = sys.argv.index('--inside')
        inside(Path(sys.argv[position + 1]), sys.argv[position + 2])
    else:
        main()
