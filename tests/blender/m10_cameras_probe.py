"""M10 area C in real Blender (own background process, controlled fixture): owned cameras, configuration,
dolly/orbit/tracking/handheld animation, cuts by camera markers, focus pulls, timeline markers and coverage,
interaction events converted into state['interactions'] (and read by the contact analysis), finalize to ASSEMBLED
with its refusals, refusals that leave the candidate usable, broken-cut detection, and rejection that restores the
source.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_cameras_probe.py -- <out_dir>
"""
from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
package = types.ModuleType('blender_mcp')
package.__path__ = [str(ROOT / 'src' / 'blender_mcp')]
sys.modules.setdefault('blender_mcp', package)

import bpy  # noqa: E402
import mathutils  # noqa: E402

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402


class LocalBridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = 0
        self.bytes = 0

    def run(self, body, params=None):
        code = build_code(body, params)
        self.calls += 1
        self.bytes += len(code.encode('utf-8'))
        ns = {}
        exec(code.rsplit('print(', 1)[0], ns)
        return ns['result']


REPORT = {'suite': 'm10_cameras', 'blender': bpy.app.version_string, 'checks': [], 'measurements': {}, 'errors': []}
OUT_NAME = 'm10_cameras.json'


def check(out, name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    (out / OUT_NAME).write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def expect(out, name, code, function):
    try:
        function()
    except Exception as exc:
        check(out, name + ' -> ' + code, str(exc).split(':')[0] == code, str(exc)[:300])
        return
    check(out, name + ' -> ' + code, False, 'unexpected success')


def build_fixture(out):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 30
    arm_data = bpy.data.armatures.new('ActorRig')
    rig = bpy.data.objects.new('ActorRig', arm_data)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    # hand.R exists so an interaction effector can be resolved through the registry role hand_right.
    for name, head, tail, parent in (('root', (0, 0, 0), (0, 0, .5), None), ('spine', (0, 0, .5), (0, 0, 1.2), 'root'),
                                     ('head', (0, 0, 1.2), (0, 0, 1.6), 'spine'),
                                     ('hand.R', (0.3, 0, 1.1), (0.5, 0, 1.1), 'spine')):
        bone = arm_data.edit_bones.new(name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = arm_data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    action = bpy.data.actions.new('Actor_Walk')
    rig.animation_data_create().action = action
    for frame, y in ((1, 0.0), (45, 2.2), (90, 4.5)):
        rig.pose.bones['root'].location = (0, y, 0)
        rig.pose.bones['root'].keyframe_insert('location', frame=frame)
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0, 0, 0.8))
    body = bpy.context.active_object
    body.name = 'ActorBody'
    body.parent = rig
    bpy.ops.mesh.primitive_plane_add(size=20)
    bpy.context.active_object.name = 'Ground'
    source_cam = bpy.data.objects.new('SourceCam', bpy.data.cameras.new('SourceCam'))
    scene.collection.objects.link(source_cam)
    source_cam.location = (0, -10, 2)
    scene.camera = source_cam
    scene.frame_start, scene.frame_end = 1, 90
    path = out / 'm10_cameras_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


def action_keys(action):
    rows = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for fc in bag.fcurves:
                    rows.append([fc.data_path, fc.array_index, [list(k.co) for k in fc.keyframe_points]])
    return rows


def camera_curves(cam):
    """{(id_type, data_path, index): fcurve} over the camera Action's two slots."""
    result = {}
    act = cam.animation_data.action
    strip = act.layers[0].strips[0]
    for idblock, kind in ((cam, 'OBJECT'), (cam.data, 'CAMERA')):
        ad = idblock.animation_data
        if ad is None or ad.action_slot is None:
            continue
        bag = strip.channelbag(ad.action_slot)
        if bag is None:
            continue
        for fc in bag.fcurves:
            result[(kind, fc.data_path, fc.array_index)] = fc
    return result


def evaluated(scene, obj):
    return obj.evaluated_get(bpy.context.evaluated_depsgraph_get())


RT = {}


def channels(objects):
    rows = {}
    for obj in objects:
        rows[obj.name] = [tuple(obj.location), tuple(obj.rotation_euler), tuple(obj.scale)]
        if obj.type == 'ARMATURE' and obj.pose:
            for bone in obj.pose.bones:
                rows[obj.name + ':' + bone.name] = [tuple(bone.location), tuple(bone.rotation_quaternion)]
    return rows


def sample(scene, objects, frames, function):
    """The probe's own measurements follow the payload discipline: capture, one frame_set per frame, restore.
    Returns (rows, names whose ORIGINAL channels the frame_set loop changed) so the flush is measured, not assumed."""
    rows = []
    with bpy.context.temp_override(scene=scene):
        state = RT['cm_capture'](scene, objects)
        before = channels(objects)
        try:
            for f in frames:
                scene.frame_set(f)
                rows.append(function(f))
            after = channels(objects)
            flushed = sorted(name for name in before if before[name] != after[name])
        finally:
            RT['cm_restore'](scene, objects, state)
    return rows, flushed


def head_world(scene, rig):
    ev = evaluated(scene, rig)
    return ev.matrix_world @ ev.pose.bones['head'].head


def root_world(scene, rig):
    ev = evaluated(scene, rig)
    return ev.matrix_world @ ev.pose.bones['root'].head


def lambda_free_camera(scene):
    def active(f):
        return scene.camera.name if scene.camera else None
    return active


def marker_rows(scene):
    return sorted((m.name, m.frame, m.camera.name if m.camera else None) for m in scene.timeline_markers)


def refusal(function):
    """The refusal message of a call that must fail, or None when it unexpectedly succeeded."""
    try:
        function()
    except Exception as exc:
        return str(exc)
    return None


def gate_codes(message):
    """(gate, code) pairs of a CINEMATIC_NOT_ASSEMBLABLE message."""
    return set(re.findall(r'\[([a-z_]+)\] ([A-Z_]+) ', message))


def root_y_curve(action):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for fc in bag.fcurves:
                    if fc.data_path == 'pose.bones["root"].location' and fc.array_index == 1:
                        return fc
    raise RuntimeError('fixture Action has no root Y location curve')


def blender_state(scene, bridge, storage, ref):
    """What finalize must leave untouched: markers, frame, active camera, datablock counts and both fingerprints
    (measured with the store's own read-only status payload)."""
    from blender_mcp.production.cinematic import store
    path, plan, state = store.load(storage, ref)
    status = store.status(bridge, plan, state)
    return {'markers': marker_rows(scene), 'frame_current': scene.frame_current,
            'camera': scene.camera.name if scene.camera else None, 'objects': len(bpy.data.objects),
            'actions': len(bpy.data.actions), 'scenes': len(bpy.data.scenes),
            'source_fingerprint': status['source']['fingerprint'], 'candidate_fingerprint': status['candidate']['fingerprint']}


def main():
    out = Path(sys.argv[sys.argv.index('--') + 1])
    out.mkdir(parents=True, exist_ok=True)
    try:
        from blender_mcp.production.cinematic import cameras, model, store
        from blender_mcp.production.cinematic.service import Context, dispatch
        from blender_mcp.production.cinematic.runtime import COMMON
        exec(COMMON, RT)
        build_fixture(out)
        bridge = LocalBridge()
        storage = Storage(out / 'store')
        register_open_fixture(bridge, storage, out)
        # A real registry record (cinematic_register_actor): the plan re-measures registered actors (ACTOR_STALE) and
        # finalize runs the integrity report against it. Bones are explicit, never inferred from names.
        registered = dispatch('cinematic_register_actor', bridge, storage, 'op-reg', object_name='ActorRig',
                              options={'role': 'PROTAGONIST', 'bones': {'root': 'root', 'head': 'head', 'hand_right': 'hand.R'}})
        aid = registered['actor_id']
        check(out, 'actor registered with explicit bones (head, hand_right)',
              registered['actor']['bones'].get('hand_right') == 'hand.R' and registered['actor']['bones'].get('head') == 'head',
              registered['actor']['bones'])
        brief = {'schema': 'production.cinematic_brief.v1', 'duration_s': 3.0, 'fps': 30,
                 'actors': [{'actor_id': aid, 'role': 'PROTAGONIST'}]}
        shots = {'schema': 'production.cinematic_shots.v1', 'shots': [
            {'shot_id': 'S1', 'frame_start': 1, 'frame_end': 30, 'camera_id': 'A'},
            {'shot_id': 'S2', 'frame_start': 31, 'frame_end': 60, 'camera_id': 'B'},
            {'shot_id': 'S3', 'frame_start': 61, 'frame_end': 90, 'camera_id': 'C'}]}
        walk_keys = action_keys(bpy.data.actions['Actor_Walk'])
        created = dispatch('cinematic_create_candidate', bridge, storage, 'op-cand', brief=brief,
                           options={'shots': shots}, apply=True)
        ref = created['cinematic']
        scene = bpy.data.scenes[created['scene']]
        rig = bpy.data.objects['ActorRig']
        check(out, 'candidate 1-90 at 30 fps', scene.frame_start == 1 and scene.frame_end == 90, created['scene'])

        def call(name, options=None, apply=False, op='op', object_name=None):
            return dispatch(name, bridge, storage, op, cinematic=ref, options=options, apply=apply, object_name=object_name)

        # ---- create cameras -----------------------------------------------------------------------------------
        objects_before = len(bpy.data.objects)
        prop = call('cinematic_create_camera', {'camera_id': 'A', 'shot_id': 'S1', 'lens_mm': 35, 'location': [5, -6, 2],
                                                 'look_at': {'actor_id': aid, 'bone': 'head'}})
        check(out, 'create_camera apply=false is a proposal without Blender change',
              prop['status'] == 'proposal' and len(bpy.data.objects) == objects_before
              and prop['camera']['settings']['lens'] == 35.0, prop['camera'])
        a = call('cinematic_create_camera', {'camera_id': 'A', 'shot_id': 'S1', 'lens_mm': 35, 'location': [5, -6, 2],
                                              'look_at': {'actor_id': aid, 'bone': 'head'}}, apply=True, op='op-a')
        cam_a = bpy.data.objects[a['object']]
        cin = bpy.data.collections[model.owned_name(store.load(storage, ref)[1]['generation_id'], 1, 'CIN')]
        def aim(f):
            head = head_world(scene, rig)
            matrix = evaluated(scene, cam_a).matrix_world
            forward = matrix.to_quaternion() @ mathutils.Vector((0, 0, -1))
            return forward.angle(head - matrix.translation)
        rows, flushed = sample(scene, [rig, cam_a], [1], aim)
        aim_error = rows[0]
        check(out, 'camera A owned, in _CIN, data owned, lens 35, aimed at the head (angle measured)',
              cin.objects.get(cam_a.name) is not None and cam_a.get('production_m10_owner')
              and cam_a.data.get('production_m10_owner') and abs(cam_a.data.lens - 35) < 1e-6 and aim_error < 1e-4,
              {'aim_error_rad': aim_error, 'object': cam_a.name})
        REPORT['measurements']['camera_a_aim_error_rad'] = aim_error
        b = call('cinematic_create_camera', {'camera_id': 'B', 'shot_id': 'S2', 'lens_mm': 50, 'location': [0, -6, 1.5],
                                              'look_at': {'actor_id': aid}}, apply=True, op='op-b')
        c = call('cinematic_create_camera', {'camera_id': 'C', 'shot_id': 'S3', 'lens_mm': 50, 'location': [3, 1, 1.6],
                                              'look_at': {'actor_id': aid, 'bone': 'head'}, 'clip_start': 0.05},
                 apply=True, op='op-c')
        cam_b = bpy.data.objects[b['object']]
        cam_c = bpy.data.objects[c['object']]
        # The actor is registered with an explicit root: the registry's root wins over discovering a single root bone.
        check(out, 'three owned cameras; scene.camera of the candidate set once (first camera)',
              scene.camera == cam_a and b['target_resolved']['rule'] == 'registry_root_bone' and b['target_resolved']['bone'] == 'root'
              and abs(cam_c.data.clip_start - 0.05) < 1e-6, [a['scene_camera'], b['target_resolved']])
        d = call('cinematic_create_camera', {'camera_id': 'D', 'location': [-4, 2, 1.7], 'type': 'ORTHO', 'ortho_scale': 4.0,
                                              'look_at': {'actor_id': aid, 'bone': 'head'}, 'track': 'DAMPED_TRACK'},
                 apply=True, op='op-d')
        cam_d = bpy.data.objects[d['object']]
        damped = cam_d.constraints[0] if len(cam_d.constraints) else None

        def aim_d(f):
            matrix = evaluated(scene, cam_d).matrix_world
            forward = matrix.to_quaternion() @ mathutils.Vector((0, 0, -1))
            return forward.angle(head_world(scene, rig) - matrix.translation)
        aims, _ = sample(scene, [rig] + list(cin.all_objects), [1, 45, 90], aim_d)
        # DAMPED_TRACK is created with track_axis TRACK_Y in 5.2.1 (measured); the camera needs -Z.
        check(out, 'DAMPED_TRACK on an owned ORTHO camera keeps the moving head centred (aim error measured)',
              damped is not None and damped.type == 'DAMPED_TRACK' and damped.track_axis == 'TRACK_NEGATIVE_Z'
              and damped.target == rig and damped.subtarget == 'head' and cam_d.data.type == 'ORTHO'
              and max(aims) < 1e-4, {'aim_error_rad_f1_f45_f90': aims})
        REPORT['measurements']['damped_track_aim_error_rad'] = aims

        # ---- refusals -----------------------------------------------------------------------------------------
        expect(out, 'lens below the measured RNA minimum', 'CINEMATIC_RANGE_INVALID',
               lambda: call('cinematic_create_camera', {'camera_id': 'X', 'lens_mm': 0.5, 'location': [0, 0, 1],
                                                         'rotation_euler_deg': [90, 0, 0]}, apply=True))
        expect(out, 'configure a source (not owned) camera', 'CAMERA_NOT_OWNED',
               lambda: call('cinematic_configure_camera', {'lens_mm': 40}, apply=True, object_name='SourceCam'))
        expect(out, 'clip_start above clip_end', 'CAMERA_CLIP_INVALID',
               lambda: call('cinematic_configure_camera', {'camera_id': 'A', 'clip_start': 2000}, apply=True))
        expect(out, 'unknown option keys are refused', 'CINEMATIC_OPTION_UNSUPPORTED',
               lambda: call('cinematic_configure_camera', {'camera_id': 'A', 'focal': 40}, apply=True))
        # The payload's own guard (defense in depth) refuses a non-owned camera even if the host were bypassed,
        # and the refusal happens before any change: the next step still passes the freshness check.
        path, plan, state = store.load(storage, ref)
        ctx = Context(bridge, storage, 'op-direct')
        params = store.assembly_params(plan, state, {'camera': 'SourceCam', 'settings': {'lens': 40.0}, 'dof': {},
                                                     'tracking': None})
        expect(out, 'payload guard refuses SourceCam', 'CAMERA_NOT_OWNED',
               lambda: store.mutate(ctx, path, plan, state, 'probe_direct', cameras.CONFIGURE_BODY, params))
        check(out, 'source camera untouched', abs(bpy.data.objects['SourceCam'].data.lens - 50.0) < 1e-6)

        # ---- configure ----------------------------------------------------------------------------------------
        conf = call('cinematic_configure_camera', {'camera_id': 'A', 'lens_mm': 40, 'dof': {'aperture_fstop': 2.0},
                                                   'tracking': {'mode': 'TRACK_TO', 'target': {'actor_id': aid, 'bone': 'head'}}},
                    apply=True, op='op-conf')
        constraint = cam_a.constraints.get(cameras.camera_names(plan, state, 'A')['constraint'])
        check(out, 'configure: lens 40, f/2, TRACK_TO on the owned camera targeting the actor head',
              conf['status'] == 'success' and abs(cam_a.data.lens - 40) < 1e-6 and abs(cam_a.data.dof.aperture_fstop - 2.0) < 1e-6
              and constraint is not None and constraint.type == 'TRACK_TO' and constraint.target == rig
              and constraint.subtarget == 'head' and constraint.track_axis == 'TRACK_NEGATIVE_Z', conf['after'])

        # ---- animate ------------------------------------------------------------------------------------------
        dolly = call('cinematic_animate_camera', {'camera_id': 'A', 'shot_id': 'S1', 'move': 'DOLLY',
                                                  'params': {'distance_m': 2.0}, 'easing': 'EASE_IN_OUT',
                                                  'target': {'actor_id': aid, 'bone': 'head'}}, apply=True, op='op-dolly')
        curves_a = camera_curves(cam_a)
        frames_a = sorted({k.co[0] for fc in curves_a.values() for k in fc.keyframe_points})
        act_a = cam_a.animation_data.action
        check(out, 'dolly: one owned Action, OBJECT+CAMERA slots, 180 LINEAR keys only in 1-30',
              act_a.get('production_m10_owner') and len(act_a.slots) == 2 and cam_a.data.animation_data.action == act_a
              and dolly['keys_written'] == 180 and frames_a[0] == 1 and frames_a[-1] == 30
              and all(k.interpolation == 'LINEAR' for fc in curves_a.values() for k in fc.keyframe_points),
              {'slots': [s.identifier for s in act_a.slots], 'frames': [frames_a[0], frames_a[-1]]})
        path_keys = dolly['trajectory']['keys']['path_length_m']
        check(out, 'dolly path length measured on keys equals the requested 2.0 m (straight line)',
              abs(path_keys - 2.0) < 1e-3, dolly['trajectory']['keys'])
        check(out, 'dolly warns that TRACK_TO overrides the keyed rotation',
              any(w['code'] == 'CAMERA_ROTATION_FROM_CONSTRAINT' for w in dolly['warnings']))
        REPORT['measurements']['dolly'] = dolly['trajectory']
        orbit = call('cinematic_animate_camera', {'camera_id': 'B', 'shot_id': 'S2', 'move': 'ORBIT',
                                                  'params': {'angle_deg': 90}, 'target': {'actor_id': aid}},
                     apply=True, op='op-orbit')
        check(out, 'orbit: radius kept around the moving actor root (max error measured)',
              orbit['verification']['orbit_radius_max_error_m'] is not None
              and orbit['verification']['orbit_radius_max_error_m'] < 1e-4, orbit['verification'])
        REPORT['measurements']['orbit'] = {'verification': orbit['verification'], 'trajectory': orbit['trajectory']}
        track = call('cinematic_animate_camera', {'camera_id': 'C', 'shot_id': 'S3', 'move': 'TRACKING',
                                                  'target': {'actor_id': aid}}, apply=True, op='op-track')
        offset = mathutils.Vector(track['verification']['tracking_offset_m'])
        def offset_error(f):
            cam_pos = evaluated(scene, cam_c).matrix_world.translation
            return ((cam_pos - root_world(scene, rig)) - offset).length
        owned = [o for o in cin.all_objects]
        errors, flushed = sample(scene, [rig] + owned, list(range(61, 91)), offset_error)
        REPORT['measurements']['probe_frame_set_flushed_original_channels'] = flushed
        check(out, 'tracking keeps the fixed offset to the evaluated actor root (probe measures it independently)',
              max(errors) < 1e-4 and track['verification']['tracking_offset_max_error_m'] < 1e-4,
              {'probe_max_error_m': max(errors), 'payload_max_error_m': track['verification']['tracking_offset_max_error_m'],
               'offset_m': list(offset)})
        REPORT['measurements']['tracking'] = {'probe_max_offset_error_m': max(errors), 'trajectory': track['trajectory']}
        hand = call('cinematic_animate_camera', {'camera_id': 'B', 'frame_start': 1, 'frame_end': 30, 'move': 'HANDHELD',
                                                 'params': {'seed': 7, 'amplitude_m': 0.02, 'amplitude_deg': 1.0}},
                    apply=True, op='op-hand')
        base = mathutils.Vector(b['location_m'])
        curves_b = camera_curves(cam_b)
        deviation = max(abs(curves_b[('OBJECT', 'location', k)].evaluate(f) - base[k]) for k in range(3) for f in range(1, 31))
        check(out, 'handheld: bounded deterministic noise (per-channel deviation <= amplitude) with explicit warning',
              deviation <= 0.02 + 1e-6 and any(w['code'] == 'HANDHELD_IS_PROCEDURAL_NOISE' for w in hand['warnings'])
              and len(bpy.data.actions) == 4, {'max_channel_deviation_m': deviation})
        REPORT['measurements']['handheld_max_channel_deviation_m'] = deviation
        expect(out, 'overlapping animation without replace', 'CAMERA_ANIMATION_OVERLAP',
               lambda: call('cinematic_animate_camera', {'camera_id': 'B', 'frame_start': 40, 'frame_end': 50,
                                                         'move': 'STATIC'}, apply=True))
        expect(out, 'ORBIT without target', 'CAMERA_TARGET_REQUIRED',
               lambda: call('cinematic_animate_camera', {'camera_id': 'B', 'shot_id': 'S2', 'move': 'ORBIT',
                                                         'params': {'angle_deg': 30}}, apply=True))

        # ---- cuts ---------------------------------------------------------------------------------------------
        expect(out, 'cut outside the timeline', 'CUT_OUTSIDE_RANGE',
               lambda: call('cinematic_create_cuts', {'cuts': [{'shot_id': 'S1', 'camera_id': 'A', 'frame': 1},
                                                               {'shot_id': 'S2', 'camera_id': 'B', 'frame': 200}]}, apply=True))
        expect(out, 'cuts out of order', 'CUT_ORDER_INVALID',
               lambda: call('cinematic_create_cuts', {'cuts': [{'shot_id': 'S1', 'camera_id': 'A', 'frame': 1},
                                                               {'shot_id': 'S3', 'camera_id': 'C', 'frame': 61},
                                                               {'shot_id': 'S2', 'camera_id': 'B', 'frame': 31}]}, apply=True))
        expect(out, 'first cut not at frame_start', 'CUT_FIRST_NOT_AT_START',
               lambda: call('cinematic_create_cuts', {'cuts': [{'shot_id': 'S1', 'camera_id': 'A', 'frame': 5}]}, apply=True))
        expect(out, 'shot shorter than the proposed minimum', 'CUT_TOO_SHORT',
               lambda: call('cinematic_create_cuts', {'cuts': [{'shot_id': 'S1', 'camera_id': 'A', 'frame': 1},
                                                               {'shot_id': 'S2', 'camera_id': 'B', 'frame': 4}]}, apply=True))
        expect(out, 'cut to a camera that is not owned', 'CAMERA_NOT_OWNED',
               lambda: call('cinematic_create_cuts', {'cuts': [{'shot_id': 'S1', 'camera_id': 'SourceCam', 'frame': 1}]}, apply=True))
        cuts = call('cinematic_create_cuts', {'from_shots': True}, apply=True, op='op-cuts')
        frames_cut = [1, 15, 30, 31, 45, 60, 61, 75, 90]
        names, flushed = sample(scene, [rig] + list(cin.all_objects), frames_cut, lambda_free_camera(scene))
        observed = dict(zip(frames_cut, names))
        expected = {1: cam_a.name, 15: cam_a.name, 30: cam_a.name, 31: cam_b.name, 45: cam_b.name, 60: cam_b.name,
                    61: cam_c.name, 75: cam_c.name, 90: cam_c.name}
        check(out, 'cuts: frame_set switches scene.camera at every cut (probe and payload checks)',
              observed == expected and cuts['camera_checks_passed'] == len(cuts['camera_checks']) and not cuts['errors'],
              {'observed': observed, 'payload_checks': cuts['camera_checks']})
        check(out, 'cut markers named CUT_<shot_id> with owned cameras',
              sorted((m.name, m.frame, m.camera.name) for m in scene.timeline_markers)
              == [('CUT_S1', 1, cam_a.name), ('CUT_S2', 31, cam_b.name), ('CUT_S3', 61, cam_c.name)])

        # ---- focus --------------------------------------------------------------------------------------------
        keys_before = len(camera_curves(cam_c))
        plan_only = call('cinematic_plan_focus', {'camera_id': 'C', 'focus': [
            {'frame': 61, 'target': {'actor_id': aid, 'bone': 'head'}},
            {'frame': 76, 'target': {'distance_m': 3.0}}], 'pulls': {'frames': 8}})
        check(out, 'plan_focus apply=false plans without Blender change',
              plan_only['status'] == 'proposal' and plan_only['focus']['mode'] == 'KEYED'
              and len(camera_curves(cam_c)) == keys_before, plan_only['focus']['pull_windows'])
        focus = call('cinematic_plan_focus', {'camera_id': 'C', 'aperture_fstop': 2.8, 'focus': [
            {'frame': 61, 'target': {'actor_id': aid, 'bone': 'head'}},
            {'frame': 76, 'target': {'distance_m': 3.0}, 'subject': {'actor_id': aid, 'bone': 'head'}}],
            'pulls': {'frames': 8}}, apply=True, op='op-focus')
        curves_c = camera_curves(cam_c)
        fc = curves_c.get(('CAMERA', 'dof.focus_distance', 0))
        def focus_row(f):
            ev = evaluated(scene, cam_c)
            fwd = (ev.matrix_world.to_quaternion() @ mathutils.Vector((0, 0, -1))).normalized()
            return [(head_world(scene, rig) - ev.matrix_world.translation).dot(fwd), ev.data.dof.focus_distance]
        rows, flushed = sample(scene, [rig] + list(cin.all_objects), [61, 90], focus_row)
        depth_61, focus_61 = rows[0]
        focus_90 = rows[1][1]
        check(out, 'focus pull keyed in the CAMERA slot: f61 = head depth, f90 = 3.0 m (evaluated)',
              fc is not None and len(fc.keyframe_points) == 30 and abs(focus_61 - depth_61) < 1e-4
              and abs(focus_90 - 3.0) < 1e-5 and cam_c.data.dof.use_dof and cam_c.data.dof.focus_object is None,
              {'depth_61': depth_61, 'focus_61': focus_61, 'focus_90': focus_90, 'findings': [f['code'] for f in focus['errors'] + focus['warnings']],
               'keyed_max_error_m': focus['keyed_max_error_m']})
        REPORT['measurements']['focus'] = {k: focus[k] for k in ('focus_range_m', 'target_coc_mm_max', 'keyed_max_error_m',
                                                                 'errors', 'warnings', 'thresholds')}
        lost = next((f for f in focus['errors'] if f['code'] == 'FOCUS_LOST'), None)
        # Rows: head (61) then a fixed 3.0 m pulled over 76-83 while the head stays ~4.2 m away: the subject must be
        # reported out of focus from 84 to 90 and never inside the declared pull window.
        check(out, 'focus lost detected when a fixed distance replaces the tracked subject (outside the pull window)',
              lost is not None and lost['frames']['frames'] == list(range(84, 91)), lost)
        focus_a = call('cinematic_plan_focus', {'camera_id': 'A', 'focus': [{'frame': 1, 'target': {'actor_id': aid, 'bone': 'head'}}]},
                       apply=True, op='op-focus-a')
        check(out, 'single-target focus uses dof.focus_object/subtarget (no keys)',
              focus_a['mode'] == 'OBJECT' and cam_a.data.dof.focus_object == rig and cam_a.data.dof.focus_subtarget == 'head'
              and focus_a['keys_written'] == 0, {'findings': [f['code'] for f in focus_a['errors'] + focus_a['warnings']]})
        expect(out, 'focus target that is not in the candidate', 'CAMERA_TARGET_MISSING',
               lambda: call('cinematic_plan_focus', {'camera_id': 'B', 'shot_id': 'S2', 'focus': [
                   {'frame': 31, 'target': {'object': 'NoSuchObject'}}]}, apply=True))

        # ---- timeline -----------------------------------------------------------------------------------------
        timeline = call('cinematic_manage_timeline', {'markers': [{'kind': 'BEAT', 'id': 'intro', 'frame': 1},
                                                                  {'kind': 'EVENT', 'id': 'turn', 'seconds': 1.5}],
                                                      'conversions': [{'seconds': 2.0, 'purpose': 'shot 3 start'}]},
                        apply=True, op='op-tl')
        markers = {m.name: (m.frame, m.camera) for m in scene.timeline_markers}
        camera_46 = sample(scene, [rig] + list(cin.all_objects), [46], lambda_free_camera(scene))[0][0]
        check(out, 'narrative markers without camera; seconds converted explicitly (1.5 s -> frame 46)',
              markers.get('BEAT_intro') == (1, None) and markers.get('EVENT_turn') == (46, None) and camera_46 == cam_b.name
              and timeline['conversions'][-1]['frame'] == 61, {'conversions': timeline['conversions']})
        check(out, 'timeline report: shots contiguous, cuts cover the timeline, no errors, fps untouched',
              timeline['report']['passed'] and scene.render.fps == 30
              and bpy.data.scenes['Scene'].frame_end == 90 and bpy.data.scenes['Scene'].render.fps == 30, timeline['report'])
        expect(out, 'range that would orphan cuts', 'TIMELINE_CUTS_OUTSIDE',
               lambda: call('cinematic_manage_timeline', {'frame_end': 50}, apply=True))

        # ---- interactions -> state['interactions'] ----------------------------------------------------------
        planned = dispatch('cinematic_plan_interaction', bridge, storage, 'op-pi', options={'source_scene': 'Scene', 'interactions': [
            {'event_id': 'touch', 'kind': 'TOUCH_SURFACE', 'actors': [aid], 'frame': 40, 'duration_s': 0.3,
             'spatial_target': 'Ground', 'effector': 'hand_right'},
            {'event_id': 'look', 'kind': 'OBSERVE', 'actors': [aid], 'frame': 20, 'spatial_target': [0, 6, 1.5]},
            {'event_id': 'react', 'kind': 'REACT', 'actors': [aid], 'frame': 70}]})
        markers_before = marker_rows(scene)
        proposed = call('cinematic_manage_timeline', {'interactions': planned})
        check(out, 'interactions apply=false: converted rows, Blender unchanged',
              proposed['status'] == 'proposal' and marker_rows(scene) == markers_before
              and len(proposed['interactions']['events']) == 3, proposed['interactions']['provenance'])
        applied = call('cinematic_manage_timeline', {'interactions': planned}, apply=True, op='op-int')
        path, plan, state = store.load(storage, ref)
        expected_events = [
            {'event_id': 'touch', 'kind': 'TOUCH_SURFACE', 'actor_id': aid, 'frame': 40, 'frame_start': 40, 'frame_end': 48,
             'effector': {'bone': 'hand.R', 'end': 'TAIL'}, 'target': {'surfaces': ['Ground']}, 'contact': True},
            {'event_id': 'look', 'kind': 'OBSERVE', 'actor_id': aid, 'frame': 20, 'frame_start': 20, 'frame_end': 20,
             'effector': None, 'target': {'point': [0.0, 6.0, 1.5]}, 'contact': False, 'head_bone': 'head'},
            {'event_id': 'react', 'kind': 'REACT', 'actor_id': aid, 'frame': 70, 'frame_start': 70, 'frame_end': 70,
             'effector': None, 'target': None, 'contact': False}]
        check(out, "state['interactions'] = analysis schema; effector hand_right -> hand.R and head from the registry",
              state['interactions'] == expected_events, state['interactions'])
        markers = {m.name: (m.frame, m.camera) for m in scene.timeline_markers}
        check(out, 'EVENT_<id> markers without camera; earlier BEAT/EVENT markers kept; report passes',
              markers.get('EVENT_touch') == (40, None) and markers.get('EVENT_look') == (20, None)
              and markers.get('EVENT_react') == (70, None) and markers.get('EVENT_turn') == (46, None)
              and markers.get('BEAT_intro') == (1, None) and applied['report']['passed'], marker_rows(scene))
        unmeasurable = {w['event_id']: w['reason'] for w in applied['interactions']['warnings']
                        if w['code'] == 'INTERACTION_NOT_MEASURABLE'}
        check(out, 'gaze without a forward axis is reported with the analysis reason (never dropped)',
              unmeasurable == {'look': 'GAZE_FORWARD_AXIS_REQUIRED'}, applied['interactions']['warnings'])
        markers_before = marker_rows(scene)
        expect(out, 'interaction bone that the rig does not have (payload check before writing)', 'INTERACTION_BONE_MISSING',
               lambda: call('cinematic_manage_timeline', {'interactions': [
                   {'event_id': 'bad', 'kind': 'REACH', 'actor_id': aid, 'frame': 30, 'effector': 'NoSuchBone',
                    'target': 'Ground'}]}, apply=True))
        check(out, 'refused interactions left markers and ledger unchanged',
              marker_rows(scene) == markers_before and store.load(storage, ref)[2]['interactions'] == expected_events)
        contacts = call('cinematic_analyze_contacts', {})
        check(out, 'contact analysis reads the events from state (events_source STATE, gaze ignored, state restored)',
              contacts['events_source'] == ['STATE'] and {e['event_id'] for e in contacts['events_ignored']} == {'look'}
              and contacts['state_restored'] is True,
              {'events_source': contacts['events_source'], 'events_ignored': contacts['events_ignored'],
               'events': [[e.get('event_id'), e.get('status')] for e in contacts.get('events') or []],
               'findings': contacts.get('findings_by_code')})
        REPORT['measurements']['contacts_from_state'] = {'events': [[e.get('event_id'), e.get('status')] for e in contacts.get('events') or []],
                                                         'findings_by_code': contacts.get('findings_by_code')}

        # ---- finalize -> ASSEMBLED ----------------------------------------------------------------------------
        plan_f = call('cinematic_manage_timeline', {'finalize': True})
        check(out, 'finalize apply=false lists the gates; the ledger alone has no failures',
              plan_f['status'] == 'proposal' and not plan_f['ledger_errors'], plan_f['gates'])
        expect(out, 'finalize mixed with timeline changes', 'TIMELINE_FINALIZE_EXCLUSIVE',
               lambda: call('cinematic_manage_timeline', {'finalize': True, 'frame_end': 80}, apply=True))
        # Integrity gate on a real change: one key of the source Action moved, then restored to the exact value.
        # Measured (bpm10-cameras-6): after fcurve.update() a KeyframePoint reference held from before no longer writes
        # the real key, so the restore re-fetches it; restored that way co and both handles are bit-identical.
        walk_curve = root_y_curve(bpy.data.actions['Actor_Walk'])
        original = walk_curve.keyframe_points[1].co[1]
        walk_curve.keyframe_points[1].co[1] = original + 0.1
        walk_curve.update()
        try:
            message = refusal(lambda: call('cinematic_manage_timeline', {'finalize': True}, apply=True))
        finally:
            walk_curve.keyframe_points[1].co[1] = original
            walk_curve.update()
        gates = gate_codes(message or '')
        check(out, 'finalize refuses a moved source key: fresh STALE_SOURCE and an integrity gate in one list',
              message is not None and message.startswith('CINEMATIC_NOT_ASSEMBLABLE')
              and ('fresh', 'CINEMATIC_STALE_SOURCE') in gates and any(g[0] == 'integrity' for g in gates),
              {'message': (message or '')[:600]})
        before = blender_state(scene, bridge, storage, ref)
        calls_before = bridge.calls
        started = time.perf_counter()
        assembled = call('cinematic_manage_timeline', {'finalize': True}, apply=True, op='op-final')
        elapsed = time.perf_counter() - started
        after = blender_state(scene, bridge, storage, ref)
        path, plan, state = store.load(storage, ref)
        check(out, 'finalize records ASSEMBLED with the measured candidate fingerprint; Blender unchanged',
              assembled['state'] == 'ASSEMBLED' and state['state'] == 'ASSEMBLED'
              and state['assembled']['candidate_fingerprint'] == after['candidate_fingerprint'] == before['candidate_fingerprint']
              and before == after and state['assembled']['integrity']['passed'] and state['assembled']['timeline_report']['passed']
              and state['assembled']['shots'] == [['S1', 'A', 1, 30], ['S2', 'B', 31, 60], ['S3', 'C', 61, 90]]
              and state['assembled']['interactions'] == 3,
              {'assembled': state['assembled'], 'before': before, 'after': after})
        REPORT['measurements']['finalize'] = {'seconds_localbridge': elapsed, 'bridge_calls': bridge.calls - calls_before,
                                              'warnings': assembled['warnings']}
        expect(out, 'finalize twice', 'CINEMATIC_STATE_INVALID',
               lambda: call('cinematic_manage_timeline', {'finalize': True}, apply=True))
        cleared = call('cinematic_create_cuts', {'clear': True}, apply=True, op='op-clear')
        message = refusal(lambda: call('cinematic_manage_timeline', {'finalize': True}, apply=True))
        gates = gate_codes(message or '')
        check(out, 'an assembly change returns to CINEMATIC_CANDIDATE; finalize without cuts lists the missing cut gates',
              cleared['state'] == 'CINEMATIC_CANDIDATE' and ('cuts', 'FINALIZE_CUTS_MISSING') in gates
              and ('shots', 'FINALIZE_SHOT_WITHOUT_CUT') in gates, {'message': (message or '')[:600]})
        call('cinematic_create_cuts', {'from_shots': True}, apply=True, op='op-recut')

        # ---- broken cut detection (external deletion), then reject ---------------------------------------------
        path, plan, state = store.load(storage, ref)
        bpy.data.objects.remove(cam_b)
        report = cameras.timeline_report(Context(bridge, storage, 'op-report'), plan, state)
        codes = {e['code'] for e in report['errors']}
        check(out, 'deleted camera -> broken cut detected (marker camera None, camera missing)',
              not report['passed'] and 'CUT_CAMERA_MISSING' in codes, sorted(codes))
        message = refusal(lambda: call('cinematic_manage_timeline', {'finalize': True}, apply=True))
        gates = gate_codes(message or '')
        check(out, 'finalize after an external camera deletion: stale candidate and broken cut in one refusal',
              ('fresh', 'CINEMATIC_STALE_CANDIDATE') in gates and ('timeline_report', 'CUT_CAMERA_MISSING') in gates
              and store.load(storage, ref)[2]['state'] == 'CINEMATIC_CANDIDATE', {'message': (message or '')[:600]})
        expect(out, 'external deletion makes the candidate stale', 'CINEMATIC_STALE_CANDIDATE',
               lambda: call('cinematic_configure_camera', {'camera_id': 'A', 'lens_mm': 45}, apply=True))
        rejected = dispatch('cinematic_reject', bridge, storage, 'op-reject', cinematic=ref, apply=True)
        leftovers = [o.name for o in bpy.data.objects if o.get('production_m10_owner')]
        owned_data = [c.name for c in bpy.data.cameras if c.get('production_m10_owner')]
        owned_actions = [a.name for a in bpy.data.actions if a.get('production_m10_owner')]
        check(out, 'reject removes cameras, camera data, Actions, markers and the candidate scene',
              rejected['source_restored'] is True and not leftovers and not owned_data and not owned_actions
              and [s.name for s in bpy.data.scenes] == ['Scene'], rejected['removed'])
        source = bpy.data.scenes['Scene']
        check(out, 'source scene intact: camera, markers, range, fps, Actor_Walk keys',
              source.camera.name == 'SourceCam' and len(source.timeline_markers) == 0 and source.frame_end == 90
              and action_keys(bpy.data.actions['Actor_Walk']) == walk_keys and len(bpy.data.actions) == 1)
        REPORT['metrics'] = {'bridge_calls': bridge.calls, 'payload_bytes': bridge.bytes}
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    (out / OUT_NAME).write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
