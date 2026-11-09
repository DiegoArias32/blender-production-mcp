"""M10 measured cinematic QA in real Blender (own background process, controlled inline fixture).

Two animated actors on a candidate scene with two cameras and a marker cut at frame 48:
- composition: actor B framed by a camera aimed at its bounds centre -> screen x, y = 0.5 and distance 6 m;
- continuity: the cut crosses the A-B action line (180-degree rule) -> detected, then allowed with allow_axis_break;
- visibility: a wall hides A in shot 2 (and its face at a facial event); a hide_render wall in front of B must NOT
  count (scene.ray_cast still hits it - measured); control run with that wall visible occludes B;
- collisions: bodies interpenetrate at 40-45 (accidental) vs a planned hand contact at 64-72 (intended); the
  top-level metrics.max_penetration_m feeds review.normalize_section (staging section) and the refinement target
  max_penetration_m through review.measure_target, measured on the real candidate;
- contacts: timing/alignment/penetration/sliding of the planned touch, plus the unplanned body contact;
- motion: root teleport of B at 85, unexplained camera jump at 90, light jump at 70, Action gap of A at 1-4;
- after every analysis: frame, scene.camera, every channel/pose and both fingerprints are back exactly.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_analysis_probe.py -- <out_dir>
"""
from __future__ import annotations

import json
import math
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

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402


class LocalBridge:
    """Same contract as the real bridge: every payload goes through build_code (safe mode validator)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.calls = 0
        self.bytes = 0
        self.max_bytes = 0
        self.seconds = []

    def run(self, body, params=None):
        code = build_code(body, params)
        self.calls += 1
        size = len(code.encode('utf-8'))
        self.bytes += size
        self.max_bytes = max(self.max_bytes, size)
        ns = {}
        started = time.perf_counter()
        exec(code.rsplit('print(', 1)[0], ns)
        self.seconds.append(time.perf_counter() - started)
        return ns['result']


REPORT = {'suite': 'm10_analysis', 'blender': bpy.app.version_string, 'checks': [], 'errors': [], 'measured': {}}
OUT = None


def check(name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    (OUT / 'm10_analysis.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def expect(name, code, function):
    try:
        function()
    except Exception as exc:
        check(name + ' -> ' + code, str(exc).startswith(code), str(exc)[:300])
        return
    check(name + ' -> ' + code, False, 'unexpected success')


def box(name, low, high):
    x0, y0, z0 = low
    x1, y1, z1 = high
    verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def rig(name, hand):
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bones = [('root', (0, 0, 0), (0, 0, 0.5), None), ('spine', (0, 0, 0.5), (0, 0, 1.2), 'root'),
             ('head', (0, 0, 1.5), (0, 0, 1.7), 'spine')]
    if hand:
        bones.append(('hand', (0.2, 0, 1.3), (0.5, 0, 1.3), 'spine'))
    for bone_name, head, tail, parent in bones:
        bone = data.edit_bones.new(bone_name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    return obj


def linear(action):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    for point in curve.keyframe_points:
                        point.interpolation = 'LINEAR'


def build_fixture(out):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 24
    scene.render.fps_base = 1.0
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1920, 1080, 100
    scene.frame_start, scene.frame_end = 1, 96
    actor_a = rig('ActorA', hand=True)
    actor_b = rig('ActorB', hand=False)
    body_a = box('ActorA_Body', (-0.25, -0.2, 0.0), (0.25, 0.2, 1.7))
    body_a.parent = actor_a
    hand_a = box('ActorA_Hand', (-0.03, -0.06, -0.03), (0.03, 0.0, 0.03))
    hand_a.parent = actor_a
    hand_a.parent_type = 'BONE'
    hand_a.parent_bone = 'hand'
    body_b = box('ActorB_Body', (-0.25, -0.25, -0.05), (0.25, 0.25, 1.75))
    body_b.parent = actor_b
    # A walks into B (bodies interpenetrate 40-45), retreats, then reaches B with the hand at 64-72.
    actor_a.animation_data_create()
    for frame, x in ((5, -2.0), (40, 0.0), (45, 0.0), (56, -0.75), (96, -0.75)):
        actor_a.location = (x, 0.0, 0.0)
        actor_a.keyframe_insert('location', frame=frame)
    hand = actor_a.pose.bones['hand']
    for frame, d in ((5, 0.0), (58, 0.0), (64, 0.302), (72, 0.302), (78, 0.0)):
        hand.location = (0.0, d, 0.0)
        hand.keyframe_insert('location', frame=frame)
    actor_a.animation_data.action.name = 'A_Perform'
    linear(actor_a.animation_data.action)
    # B stands still and teleports 1 m in Y between 84 and 85.
    actor_b.animation_data_create()
    for frame, y in ((1, 0.0), (84, 0.0), (85, 1.0), (96, 1.0)):
        actor_b.location = (0.3, y, 0.0)
        actor_b.keyframe_insert('location', frame=frame)
    actor_b.animation_data.action.name = 'B_Idle'
    linear(actor_b.animation_data.action)
    light_data = bpy.data.lights.new('KeyLight', 'POINT')
    light = bpy.data.objects.new('KeyLight', light_data)
    light.location = (0.0, -3.0, 4.0)
    scene.collection.objects.link(light)
    for frame, energy in ((1, 1000.0), (69, 1000.0), (70, 5000.0), (96, 5000.0)):
        light_data.energy = energy
        light_data.keyframe_insert('energy', frame=frame)
    linear(light_data.animation_data.action)
    box('Wall', (-1.4, 2.95, 0.0), (0.0, 3.05, 2.5))
    ghost = box('GhostWall', (0.1, 2.95, 0.0), (0.5, 3.05, 2.5))
    ghost.hide_render = True
    scene.frame_set(1)
    path = out / 'm10_analysis_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


ASSEMBLE = r'''
def probe_assemble(params):
    scene, src, cin = cm_candidate(params)
    touched = []
    owned = {'objects': [], 'data': [], 'actions': []}
    for row in params['actors']:
        rig = cm_object(row['object'], ['ARMATURE'])
        touched.append(cm_touch_animdata(rig, 'OBJECT'))
        action = rig.animation_data.action
        track = cm_new_track(rig, 'OBJECT', row['track'], params)
        touched.append(cm_touch_track(rig, 'OBJECT', row['track']))
        cm_ensure_pose(rig)
        rig.animation_data.action = None
        strip = track.strips.new(row['strip'], row['start'], action)
        strip.extrapolation = row['extrapolation']
    cameras = {}
    for row in params['cameras']:
        data = bpy.data.cameras.new(row['data'])
        cam = cm_new_object(params, row['object'], data, cin, 'camera')
        cam.location = row['location']
        cam.rotation_euler = row['rotation']
        owned['objects'].append(cam.name)
        owned['data'].append(['CAMERA', data.name])
        if row.get('keys'):
            action = cm_new_action(params, row['action'], 'camera_animation')
            slot = action.slots.new(id_type='OBJECT', name=cam.name)
            layer = action.layers.new('Layer')
            keyed = layer.strips.new(type='KEYFRAME')
            bag = keyed.channelbag(slot, ensure=True)
            for index in range(3):
                curve = bag.fcurves.new('location', index=index)
                for frame, value in row['keys']:
                    point = curve.keyframe_points.insert(frame, value[index])
                    point.interpolation = 'LINEAR'
            cam.animation_data_create().action = action
            owned['actions'].append(action.name)
        cameras[row['object']] = cam
    for name, frame, camera in params['markers']:
        marker = scene.timeline_markers.new(name, frame=frame)
        marker.camera = cameras[camera]
    scene.camera = cameras[params['markers'][0][2]]
    return {'touched': touched, 'owned': owned, 'fingerprints': cm_fingerprints(params)}

result = probe_assemble(params)
'''


def snapshot(candidate_name):
    """Exact host-side snapshot of what an analysis must leave untouched."""
    candidate = bpy.data.scenes[candidate_name]
    source = bpy.data.scenes['Scene']
    rows = {'candidate_frame': [candidate.frame_current, candidate.frame_subframe],
            'candidate_camera': candidate.camera.name if candidate.camera else None,
            'source_frame': [source.frame_current, source.frame_subframe],
            'source_camera': source.camera.name if source.camera else None, 'objects': {}}
    for obj in bpy.data.objects:
        row = [list(obj.location), list(obj.rotation_euler), list(obj.rotation_quaternion), list(obj.scale)]
        if obj.type == 'ARMATURE' and obj.pose:
            row.append({b.name: [list(b.location), list(b.rotation_quaternion), list(b.rotation_euler), list(b.scale)]
                        for b in obj.pose.bones})
        if obj.type == 'CAMERA':
            row.append([obj.data.lens, obj.data.clip_start, obj.data.clip_end])
        if obj.type == 'LIGHT':
            row.append([obj.data.energy])
        rows['objects'][obj.name] = row
    return rows


def find(report, code, **match):
    rows = []
    for row in report.get('findings') or []:
        if row['code'] != code:
            continue
        if all(row.get(k) == v or (row.get('measured') or {}).get(k) == v for k, v in match.items()):
            rows.append(row)
    return rows


def overlaps(frames, lo, hi):
    return frames and frames[0] <= hi and lo <= frames[1]


def main():
    global OUT
    OUT = Path(sys.argv[sys.argv.index('--') + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        from blender_mcp.production.cinematic import analysis, lifecycle, model, registry, store
        from blender_mcp.production.cinematic.runtime import COMMON
        from blender_mcp.production.cinematic.service import Context, Request
        from blender_mcp.production.common.asset_safety import current_asset
        build_fixture(OUT)
        bridge = LocalBridge()
        storage = Storage(OUT / 'store')
        register_open_fixture(bridge, storage, OUT)
        key = registry.asset_key(current_asset(bridge, storage), 'Scene')
        # Real registration (measured record with integrity digests): create_candidate re-measures every actor and
        # refuses hand-written records (ACTOR_STALE). Head bones are explicit, never guessed.
        from blender_mcp.production.cinematic import actors as actor_module
        a = actor_module.register_actor(Context(bridge, storage, 'op-reg-a'), Request(
            object_name='ActorA', options={'role': 'PROTAGONIST', 'bones': {'head': 'head'}}))['actor_id']
        b = actor_module.register_actor(Context(bridge, storage, 'op-reg-b'), Request(
            object_name='ActorB', options={'role': 'ANTAGONIST', 'bones': {'head': 'head'}}))['actor_id']
        REPORT['measured']['registered_bones'] = {k: registry.find(storage, key, v)['bones'] for k, v in (('A', a), ('B', b))}
        brief = {'schema': 'production.cinematic_brief.v1', 'duration_s': 4.0, 'fps': 24,
                 'actors': [{'actor_id': a, 'role': 'PROTAGONIST'}, {'actor_id': b, 'role': 'ANTAGONIST'}]}
        shots = {'schema': 'production.cinematic_shots.v1', 'shots': [
            {'shot_id': 'S1', 'frame_start': 1, 'frame_end': 47, 'camera_id': 'cam_a', 'shot_type': 'MEDIUM', 'subjects': [a, b]},
            {'shot_id': 'S2', 'frame_start': 48, 'frame_end': 96, 'camera_id': 'cam_b', 'shot_type': 'WIDE', 'subjects': [a, b]}]}

        def ctx(op):
            return Context(bridge, storage, op)

        created = lifecycle.create_candidate(ctx('op-create'), Request(brief=brief, options={'shots': shots}, apply=True))
        ref = created['cinematic']
        path, plan, state = store.load(storage, ref)
        gid = plan['generation_id']
        names = {'cam_a': model.owned_name(gid, 1, 'CAM_cam_a'), 'cam_b': model.owned_name(gid, 1, 'CAM_cam_b')}
        params = store.assembly_params(plan, state, {
            'actors': [{'object': 'ActorA', 'track': model.owned_name(gid, 1, 'BODY_A'), 'strip': 'A_Perform', 'start': 5,
                        'extrapolation': 'HOLD_FORWARD'},
                       {'object': 'ActorB', 'track': model.owned_name(gid, 1, 'BODY_B'), 'strip': 'B_Idle', 'start': 1,
                        'extrapolation': 'HOLD'}],
            'cameras': [{'object': names['cam_a'], 'data': model.owned_name(gid, 1, 'CAMD_cam_a'),
                         'location': [0.3, -6.0, 0.85], 'rotation': [math.pi / 2, 0.0, 0.0]},
                        {'object': names['cam_b'], 'data': model.owned_name(gid, 1, 'CAMD_cam_b'),
                         'location': [0.3, 6.0, 0.85], 'rotation': [math.pi / 2, 0.0, math.pi],
                         'action': model.owned_name(gid, 1, 'CAMACT_cam_b'),
                         'keys': [[48, [0.3, 6.0, 0.85]], [89, [0.3, 5.5, 0.85]], [90, [1.1, 5.5, 0.85]]]}],
            'markers': [['CUT_S1', 1, names['cam_a']], ['CUT_S2', 48, names['cam_b']]]})
        new_state, result = store.mutate(ctx('op-assemble'), path, plan, state, 'probe_assemble', COMMON + ASSEMBLE, params)
        new_state['cameras'] = {'cam_a': {'object': names['cam_a'], 'data': params['cameras'][0]['data']},
                                'cam_b': {'object': names['cam_b'], 'data': params['cameras'][1]['data']}}
        new_state['timeline'] = dict(new_state.get('timeline') or plan['timeline'],
                                     markers=[m for m in params['markers']])
        store.record(path, new_state)
        path, plan, state = store.load(storage, ref)
        candidate = state['assembly']['scene']
        check('candidate assembled with NLA clips, two cameras and a marker cut at 48',
              state['state'] == 'CINEMATIC_CANDIDATE' and len(state['assembly']['touched']) == 4, state['assembly']['owned'])
        scene = bpy.data.scenes[candidate]
        store.fresh(bridge, storage, plan, state)

        def run(kind, options=None, cinematic=True, object_name=None):
            before = snapshot(candidate)
            started = time.perf_counter()
            report = analysis.run_tool(kind, ctx('op-' + kind), Request(cinematic=ref if cinematic else None,
                                                                         object_name=object_name, options=options or {}))
            elapsed = time.perf_counter() - started
            after = snapshot(candidate)
            REPORT['measured'].setdefault('elapsed_s', []).append([kind, round(elapsed, 4)])
            check(kind + (' (scene mode)' if not cinematic else '') + ': frame, scene.camera, channels, poses and data '
                  'restored exactly; fingerprints equal', before == after and report['state_restored'] is True,
                  {'restoration': report['restoration'], 'diff': [k for k in after['objects']
                                                                  if after['objects'][k] != before['objects'].get(k)]
                   + [k for k in ('candidate_frame', 'candidate_camera', 'source_frame') if after[k] != before[k]]})
            return report

        # ---- composition ----
        comp = run('composition')
        rows = {(r['shot_id'], r['actor_id']): r for r in comp['per_shot']}
        b1 = rows.get(('S1', b))
        REPORT['measured']['composition_S1_B'] = b1
        check('composition: actor B aimed at by camera A is centred (screen x, y = 0.5 within 1e-3)',
              b1 and abs(b1['screen_center_x']['median'] - 0.5) < 1e-3 and abs(b1['screen_center_y']['median'] - 0.5) < 1e-3
              and abs(b1['screen_center_x']['max'] - 0.5) < 1e-3, b1 and [b1['screen_center_x'], b1['screen_center_y']])
        check('composition: camera-to-B distance measured 6.0 m (constructed)',
              b1 and abs(b1['distance_m']['median'] - 6.0) < 1e-3, b1 and b1['distance_m'])
        check('composition: SUBJECT_CENTERED is a creative observation, never a finding or a score',
              any(o['code'] == 'SUBJECT_CENTERED' and o.get('actor_id') == b and o.get('shot_id') == 'S1'
                  for o in comp['creative_observations'])
              and not any(f['code'] == 'SUBJECT_CENTERED' for f in comp['findings'])
              and not any('score' in k.lower() for k in comp), [o['code'] for o in comp['creative_observations']])
        a1 = rows.get(('S1', a))
        check('composition: A walks out of frame at the start of S1 (partial framing measured, < 1 fully in frame)',
              a1 and a1['fully_in_frame_fraction'] < 1.0, a1 and {k: a1[k] for k in ('in_frame_fraction', 'fully_in_frame_fraction')})
        # ---- continuity ----
        cont = run('continuity')
        crossed = find(cont, 'AXIS_CROSSED')
        REPORT['measured']['continuity_cuts'] = cont.get('cuts')
        check('continuity: the cut at 48 crosses the A-B action line (180-degree rule) -> AXIS_CROSSED',
              crossed and crossed[0]['frames'] == [47, 48] and crossed[0]['severity'] == 'WARNING', crossed)
        allowed = run('continuity', {'allow_axis_break': [48]})
        check('continuity: allow_axis_break [48] turns it into AXIS_BREAK_ALLOWED (INFO), no AXIS_CROSSED',
              not find(allowed, 'AXIS_CROSSED') and find(allowed, 'AXIS_BREAK_ALLOWED')
              and find(allowed, 'AXIS_BREAK_ALLOWED')[0]['severity'] == 'INFO',
              [f['code'] for f in allowed['findings']])
        check('continuity: plan cuts match the markers and the planned cameras (no gap/overlap/mismatch)',
              not any(find(cont, c) for c in ('TIMELINE_GAP', 'SHOT_OVERLAP', 'SHOT_CAMERA_MISMATCH', 'CUT_NOT_IN_PLAN',
                                                'PLANNED_CUT_WITHOUT_MARKER', 'NO_ACTIVE_CAMERA')),
              cont['findings_by_code'])
        # ---- visibility ----
        vis = run('visibility', {'facial_events': [{'actor_id': a, 'frame': 70, 'label': 'reaction'}]})
        vrows = {(r['shot_id'], r['actor_id']): r for r in vis['per_shot']}
        occl = [f for f in find(vis, 'EXCESSIVE_OCCLUSION', actor_id=a) if f['measured']['occluder'] == 'Wall']
        REPORT['measured']['visibility_S2'] = {'A': vrows.get(('S2', a)), 'B': vrows.get(('S2', b)),
                                               'hidden_skipped': vis.get('hidden_in_render_skipped')}
        check('visibility: the wall occludes A in shot 2 -> EXCESSIVE_OCCLUSION by Wall (ENVIRONMENT)',
              occl and occl[0]['shot_id'] == 'S2' and occl[0]['measured']['occluder_class'] == 'ENVIRONMENT', occl[:2])
        all_occluders = {o['object'] for r in vis['per_shot'] for o in r['occluders']}
        check('visibility: the hide_render wall never counts as occluder but is reported as skipped',
              'GhostWall' not in all_occluders and (vis.get('hidden_in_render_skipped') or {}).get('GhostWall', 0) > 0,
              {'occluders': sorted(all_occluders), 'skipped': vis.get('hidden_in_render_skipped')})
        b2 = vrows.get(('S2', b))
        check('visibility: B behind the hide_render wall stays visible (median visible fraction >= 0.9)',
              b2 and b2['visible_fraction']['median'] >= 0.9, b2 and b2['visible_fraction'])
        face = find(vis, 'FACE_HIDDEN_AT_EVENT', actor_id=a)
        check('visibility: A face hidden by the wall at the facial event frame 70',
              face and face[0]['frames'] == [70, 70] and face[0]['measured']['occluder'] == 'Wall', face)
        # Control: the same wall visible must occlude B (proves the hidden wall sits on the ray path).
        ghost = bpy.data.objects['GhostWall']
        ghost.hide_render = False
        try:
            control = run('visibility', {'scene': candidate, 'actors': [{'object': 'ActorB', 'meshes': ['ActorB_Body'],
                                                                           'head_bone': 'head', 'actor_id': 'B'}]},
                          cinematic=False)
        finally:
            ghost.hide_render = True
        crow = [r for r in control['per_shot'] if r['actor_id'] == 'B' and r['frames'][0] == 48]
        check('visibility control: with the wall visible, B is occluded by GhostWall in the second camera segment',
              crow and crow[0]['visible_fraction']['median'] < 0.5 and crow[0]['occluders'][0]['object'] == 'GhostWall',
              crow and {k: crow[0][k] for k in ('visible_fraction', 'occluders')})
        check('scene mode derives camera segments from markers (no plan shots)',
              control['shots_source'] == 'MEASURED_CAMERA_SEGMENTS' and control['mode'] == 'SCENE', control['shots_source'])
        store.fresh(bridge, storage, plan, state)
        check('after the control the source fingerprint is the recorded one again (hide_render restored)', True)
        # ---- collisions ----
        touch = {'event_id': 'touch', 'kind': 'REACH', 'actor_id': a, 'frame': 64, 'frame_start': 63, 'frame_end': 72,
                 'effector': {'bone': 'hand'}, 'target': {'actor_id': b}, 'contact': True}
        col = run('collisions', {'events': [touch]})
        accidental = find(col, 'ACCIDENTAL_COLLISION')
        REPORT['measured']['collisions'] = {'findings': col['findings'], 'pairs': col['pairs']}
        check('collisions: bodies interpenetrating at 40-45 -> ACCIDENTAL_COLLISION (ERROR, depth > 0.02 m)',
              any(overlaps(f['frames'], 40, 45) and f['severity'] == 'ERROR' for f in accidental), accidental)
        check('collisions: nothing accidental inside the planned touch window 63-72',
              not any(overlaps(f['frames'], 63, 72) for f in accidental + find(col, 'ACCIDENTAL_CONTACT_WITHIN_TOLERANCE')),
              [f['frames'] for f in accidental])
        intended = [f for f in col['findings'] if f['code'].startswith('INTENDED_CONTACT')]
        check('collisions: hand-into-B during the planned window is classified INTENDED (event touch)',
              intended and all(f['measured']['event_id'] == 'touch' for f in intended), intended)
        check('collisions: coverage declared, continuous freedom never certified',
              col['collision_coverage']['continuous_collision_freedom_certified'] is False
              and col['coverage']['step'] == 1, col['collision_coverage'])
        # ---- integration: top-level max_penetration_m, provider contract, quality report and refinement target ----
        metrics = col['metrics']
        depths = [f['measured']['max_depth_m'] for f in col['findings']
                  if f['code'] in analysis.ACCIDENTAL_CODES | analysis.INTENDED_CODES]
        REPORT['measured']['collision_metrics'] = metrics
        check('collisions: metrics.max_penetration_m is the max over every penetrating interval, in metres',
              metrics['max_penetration_status'] == 'MEASURED' and metrics['max_penetration_units'] == 'm'
              and col['findings_pagination']['next_offset'] is None and metrics['max_penetration_m'] == max(depths)
              and metrics['max_accidental_penetration_m'] == metrics['max_penetration_m'] > 0.02
              and metrics['max_intended_penetration_m'] < 0.005, metrics)
        from blender_mcp.production.cinematic import review
        before = snapshot(candidate)
        provider = analysis.collision_report(ctx('op-provider'), plan, state, {'events': [touch]})
        after = snapshot(candidate)
        section = review.normalize_section(provider)
        REPORT['measured']['staging_section'] = {k: section.get(k) for k in ('status', 'metrics', 'errors')}
        check('collision_report (quality-report provider) returns passed/status/findings/metrics/state_restored and the '
              'same max_penetration_m; normalize_section reads FAIL with the reason; state restored',
              all(k in provider for k in ('passed', 'status', 'findings', 'metrics', 'state_restored'))
              and provider['passed'] is False and provider['state_restored'] is True and before == after
              and provider['metrics']['max_penetration_m'] == metrics['max_penetration_m']
              and section['status'] == 'FAIL' and section['metrics']['max_penetration_m'] == metrics['max_penetration_m']
              and any('ACCIDENTAL_COLLISION' in e for e in section['errors']), section)
        target = review.measure_target(ctx('op-target'), path, plan, state,
                                       {'source': 'SECTION:staging', 'target': 'max_penetration_m'})
        REPORT['measured']['refinement_target_max_penetration'] = target
        check('refinement target max_penetration_m is measured through the staging section provider (real Blender)',
              target['value'] == metrics['max_penetration_m'] and target['units'] == 'm'
              and target['source'] == 'PROVIDER analysis.collision_report', target)
        store.fresh(bridge, storage, plan, state)
        # ---- contacts ----
        # Whole range (not only the event window) so the unplanned body contact at 33-51 is surveyed too. Two gaze
        # events use opposite local axes of A's head bone: whatever its roll, their angles to B must sum to 180 deg.
        look = {'event_id': 'look', 'kind': 'GAZE', 'actor_id': a, 'frame': 60, 'frame_start': 60, 'frame_end': 70,
                'head_bone': 'head', 'forward_axis': [1, 0, 0], 'target': {'actor_id': b}}
        away = dict(look, event_id='look_away', forward_axis=[-1, 0, 0])
        con = run('contacts', {'events': [touch, look, away], 'frame_start': 1, 'frame_end': 96})
        events = {e['event_id']: e for e in con.get('events') or []}
        event = events.get('touch', {})
        REPORT['measured']['contact_touch'] = event
        REPORT['measured']['gaze'] = {k: events.get(k) for k in ('look', 'look_away')}
        g1 = (events.get('look') or {}).get('angle_deg') or {}
        g2 = (events.get('look_away') or {}).get('angle_deg') or {}
        check('contacts: gaze angles of opposite head-bone axes sum to 180 deg and one of them is aligned with B (< 1 deg)',
              g1 and g2 and abs(g1['median'] + g2['median'] - 180.0) < 1e-3 and min(g1['median'], g2['median']) < 1.0
              and [f['measured']['event_id'] for f in find(con, 'GAZE_NOT_ALIGNED')] == (
                  ['look_away'] if g1['median'] < g2['median'] else ['look']),
              {'look': g1, 'look_away': g2})
        check('contacts: planned touch measured CONTACT on time (first 64, last 72, offsets within 2 frames)',
              event.get('status') == 'CONTACT' and event.get('timing') == 'ON_TIME', {k: event.get(k) for k in (
                  'status', 'first_contact_frame', 'last_contact_frame', 'timing_offset_frames', 'distance_at_planned_frame_m')})
        check('contacts: effector penetration measured (hand tail 2 mm inside B, not reported as zero or unknown)',
              event.get('penetration_measured') is True and event.get('max_penetration_m') is not None
              and 0.0 < event['max_penetration_m'] < 0.005, event.get('max_penetration_m'))
        check('contacts: relative sliding in B space during the hold is ~0 (< 1 mm)',
              event.get('relative_sliding_m') is not None and event['relative_sliding_m'] < 0.001, event.get('relative_sliding_m'))
        survey = con.get('observed_contacts') or []
        check('contacts: the body contact around 40-45 is reported as UNPLANNED_CONTACT; the touch is matched to its event',
              any(r['planned_event'] is None and overlaps(r['frames'], 40, 45) for r in survey)
              and any(r['planned_event'] == 'touch' for r in survey) and find(con, 'UNPLANNED_CONTACT'), survey)
        # ---- motion ----
        mot = run('motion')
        REPORT['measured']['motion'] = {'cameras': mot['cameras'], 'actors': mot['actors'], 'cuts': mot['cuts'],
                                        'codes': mot['findings_by_code']}
        root = find(mot, 'ROOT_JUMP', actor_id=b)
        check('motion: B root teleport detected at 84->85 (~1.0 m)',
              root and root[0]['frames'] == [84, 85] and abs(root[0]['measured']['step_m'] - 1.0) < 1e-4, root)
        jump = find(mot, 'CAMERA_JUMP_UNEXPLAINED')
        check('motion: camera B jump at 89->90 (0.8 m) not explained by a cut; the cut at 48 is not a jump',
              jump and jump[0]['frames'] == [89, 90] and abs(jump[0]['measured']['step_m'] - 0.8) < 1e-4
              and not any(f['frames'] == [47, 48] for f in jump) and 48 in mot['cuts'], jump)
        light = find(mot, 'LIGHTING_JUMP')
        check('motion: light energy 1000 -> 5000 at 69->70 detected as LIGHTING_JUMP', light and light[0]['frames'] == [69, 70],
              light)
        gap = find(mot, 'ACTION_GAP', actor_id=a)
        check('motion: frames 1-4 of A are driven by no clip (HOLD_FORWARD strip from 5) -> ACTION_GAP [1, 4]',
              gap and gap[0]['frames'] == [1, 4] and not find(mot, 'ACTION_GAP', actor_id=b), gap)
        boundary = find(mot, 'ROOT_JUMP', actor_id=a)
        REPORT['measured']['motion']['root_jumps'] = find(mot, 'ROOT_JUMP')
        check('motion: in the gap A sits at the origin (undriven NLA channels -> default location 0) and jumps 2.0 m '
              'at 4->5, tagged ACTION_GAP_BOUNDARY',
              boundary and boundary[0]['frames'] == [4, 5] and abs(boundary[0]['measured']['step_m'] - 2.0) < 1e-4
              and boundary[0]['measured']['context'] == 'ACTION_GAP_BOUNDARY', boundary)
        # ---- negatives ----
        expect('motion with step 2 is refused (derivatives need consecutive frames)', 'CINEMATIC_DENSE_SAMPLING_REQUIRED',
               lambda: analysis.run_tool('motion', ctx('op-x'), Request(cinematic=ref, options={'step': 2})))
        expect('unknown option refused', 'CINEMATIC_OPTION_UNSUPPORTED',
               lambda: analysis.run_tool('composition', ctx('op-x'), Request(cinematic=ref, options={'nope': 1})))
        expect('apply=true refused on an analysis tool', 'CINEMATIC_ANALYSIS_READ_ONLY',
               lambda: analysis.run_tool('composition', ctx('op-x'), Request(cinematic=ref, apply=True)))
        cam = bpy.data.objects[names['cam_a']]
        frame_before = scene.frame_current
        x_before = cam.location.x
        cam.location.x = x_before + 0.1
        try:
            expect('a candidate edited outside the tools is refused before any frame_set', 'CINEMATIC_STALE_CANDIDATE',
                   lambda: analysis.run_tool('composition', ctx('op-x'), Request(cinematic=ref)))
            check('stale refusal changed no frame', scene.frame_current == frame_before)
        finally:
            cam.location.x = x_before
        store.fresh(bridge, storage, plan, state)
        # ---- scene mode on the source scene (no camera there) ----
        src = run('composition', {'actors': ['ActorB']}, cinematic=False)
        check('scene mode on the source scene without a camera reports NO_ACTIVE_CAMERA (ERROR), restored',
              find(src, 'NO_ACTIVE_CAMERA') and src['passed'] is False and src['state_restored'] is True,
              src['findings_by_code'])
        # ---- the registered tool path (if every subsystem module imports) ----
        try:
            from blender_mcp.production.cinematic.service import dispatch
            via = dispatch('cinematic_analyze_motion', bridge, storage, 'op-dispatch', cinematic=ref)
            check('service.dispatch reaches the analysis handler', via['kind'] == 'motion' and via['state_restored'])
        except Exception as exc:  # another agent's module may not import yet: record, do not hide
            REPORT['measured']['dispatch_error'] = repr(exc)[:400]
        store.fresh(bridge, storage, plan, state)
        check('after every analysis the plan is still fresh (no CINEMATIC_STALE_*)', True)
        REPORT['metrics'] = {'bridge_calls': bridge.calls, 'payload_bytes_total': bridge.bytes,
                             'payload_bytes_max': bridge.max_bytes,
                             'bridge_seconds_max': round(max(bridge.seconds), 4) if bridge.seconds else None}
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    (OUT / 'm10_analysis.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
