"""M10 area G in real Blender (own background process, controlled inline fixture): previs per shot, full temporal
preview through camera-marker cuts, review gates, measured refinement (improve / revert / limit), quality report and
acceptance, and a source scene that never changes.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_review_probe.py -- <out_dir>

Reviews here are labelled SYNTHETIC_REVIEW_FOR_MECHANISM_TEST: they exercise the gates in this probe's own temporary
storage and are never a visual judgement. The final acceptance also replaces the quality-report providers with a
labelled synthetic provider (SYNTHETIC_PROVIDER_FOR_MECHANISM_TEST) after recording what the real providers reported.
"""
from __future__ import annotations

from array import array
import json
import math
import struct
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

SYNTHETIC = 'SYNTHETIC_REVIEW_FOR_MECHANISM_TEST'
FPS = 30


class LocalBridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = 0
        self.bytes = 0
        self.max_bytes = 0

    def run(self, body, params=None):
        code = build_code(body, params)
        self.calls += 1
        size = len(code.encode('utf-8'))
        self.bytes += size
        self.max_bytes = max(self.max_bytes, size)
        ns = {}
        exec(code.rsplit('print(', 1)[0], ns)
        return ns['result']


REPORT = {'suite': 'm10_review', 'blender': bpy.app.version_string, 'checks': [], 'errors': [], 'metrics': {},
          'measured': {}, 'notes': ['Reviews are ' + SYNTHETIC + ' (mechanism only, no visual judgement).']}
OUT = None


def save():
    (OUT / 'm10_review.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def check(name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    save()


def expect(name, code, function):
    try:
        function()
    except Exception as exc:
        check(name + ' -> ' + code, str(exc).startswith(code), str(exc)[:400])
        return None
    check(name + ' -> ' + code, False, 'unexpected success')


def material(name, color):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = color
    return mat


def build_fixture(out):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = FPS
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    arm_data = bpy.data.armatures.new('ActorRig')
    rig = bpy.data.objects.new('ActorRig', arm_data)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    # Root bone along +Y so its local Y translation walks the actor along world +Y.
    bone = arm_data.edit_bones.new('root')
    bone.head, bone.tail = (0, 0, 0), (0, 0.5, 0)
    bpy.ops.object.mode_set(mode='OBJECT')
    action = bpy.data.actions.new('Actor_Walk')
    rig.animation_data_create().action = action
    for frame, y in ((1, 0.0), (30, 1.5), (60, 3.0)):
        rig.pose.bones['root'].location = (0, y, 0)
        rig.pose.bones['root'].keyframe_insert('location', frame=frame)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.8))
    body = bpy.context.active_object
    body.name = 'ActorBody'
    body.scale = (0.4, 0.4, 1.6)
    body.data.materials.append(material('ActorBlue', (0.15, 0.25, 0.9, 1.0)))
    group = body.vertex_groups.new(name='root')
    group.add(list(range(len(body.data.vertices))), 1.0, 'REPLACE')
    body.modifiers.new('Armature', 'ARMATURE').object = rig
    body.parent = rig
    # Backdrops: camera A (looking -X) sees the red wall above the actor, camera B (looking +Y) the green one.
    bpy.ops.mesh.primitive_plane_add(size=30, location=(-4, 1.5, 5), rotation=(0, math.pi / 2, 0))
    red = bpy.context.active_object
    red.name = 'WallRed'
    red.data.materials.append(material('Red', (1.0, 0.0, 0.0, 1.0)))
    bpy.ops.mesh.primitive_plane_add(size=30, location=(0, 8, 5), rotation=(math.pi / 2, 0, 0))
    green = bpy.context.active_object
    green.name = 'WallGreen'
    green.data.materials.append(material('Green', (0.0, 1.0, 0.0, 1.0)))
    bpy.ops.mesh.primitive_plane_add(size=40)
    bpy.context.active_object.name = 'Ground'
    # A camera that belongs to the source: refinement must refuse to touch it (not owned by the cinematic).
    source_cam = bpy.data.objects.new('SourceCam', bpy.data.cameras.new('SourceCam'))
    source_cam.data.lens = 35.0
    source_cam.location = (0, -10, 3)
    scene.collection.objects.link(source_cam)
    scene.frame_start, scene.frame_end = 1, 60
    scene.frame_set(1)
    path = out / 'm10_review_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


SCAFFOLD = None


def scaffold_body():
    from blender_mcp.production.cinematic.runtime import COMMON
    return COMMON + r'''
def probe_cameras(params):
    scene, src, cin = cm_candidate(params)
    owned = {'objects': [], 'data': []}
    rows = {}
    for key, location, target, lens in params['cameras']:
        name = params['names'][key]
        data = bpy.data.cameras.new(name)
        cam = cm_new_object(params, name, data, cin, 'camera')
        cam.location = location
        direction = mathutils.Vector(target) - mathutils.Vector(location)
        cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
        data.lens = lens
        owned['objects'].append(cam.name)
        owned['data'].append(['CAMERA', data.name])
        rows[key] = {'object': cam.name, 'data': data.name}
    for marker_name, frame, key in params['cuts']:
        marker = scene.timeline_markers.new(marker_name, frame=frame)
        marker.camera = bpy.data.objects[params['names'][key]]
    return {'touched': [], 'owned': owned, 'cameras': rows, 'fingerprints': cm_fingerprints(params)}

result = probe_cameras(params)
'''


def png_size(path):
    with open(path, 'rb') as stream:
        header = stream.read(24)
    return struct.unpack('>II', header[16:24]) if header[:8] == b'\x89PNG\r\n\x1a\n' else None


def top_center(path):
    """RGB of the pixel 3 rows below the top edge at the horizontal centre (Blender images are bottom-up)."""
    image = bpy.data.images.load(str(path), check_existing=False)
    width, height = image.size
    buffer = array('f', [0.0]) * (width * height * 4)
    image.pixels.foreach_get(buffer)
    index = ((height - 3) * width + width // 2) * 4
    rgb = [round(buffer[index + i], 4) for i in range(3)]
    bpy.data.images.remove(image)
    return rgb


def dominant(rgb):
    if rgb[0] > rgb[1] + 0.1 and rgb[0] > rgb[2] + 0.1:
        return 'RED'
    if rgb[1] > rgb[0] + 0.1 and rgb[1] > rgb[2] + 0.1:
        return 'GREEN'
    return 'OTHER'


def apng_frames(path):
    raw = Path(path).read_bytes()
    offset, frames, controls = 8, None, 0
    while offset + 8 <= len(raw):
        length = struct.unpack('>I', raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        if kind == b'acTL':
            frames = struct.unpack('>I', raw[offset + 8:offset + 12])[0]
        elif kind == b'fcTL':
            controls += 1
        offset += 12 + length
    return frames, controls


def checklist(status='OK'):
    from blender_mcp.production.cinematic.review import DIRECTION_CHECKLIST
    return {key: {'status': status, 'note': SYNTHETIC} for key, _ in DIRECTION_CHECKLIST}


def synthetic_review(render, decision='ACCEPT', **extra):
    review = {'reviewer': SYNTHETIC, 'artistic_observations': [SYNTHETIC + ': mechanism test, no visual judgement'],
              'technical_findings': [], 'decision': decision, 'artifact_reference': render['output_dir'],
              'temporal_reviewed': True, 'shots_reviewed': [s['shot_id'] for s in render['shots']],
              'speeds_reviewed': [1, 0.5, 0.25], 'direction_checklist': checklist()}
    review.update(extra)
    return review


def action_digest(name):
    action = bpy.data.actions[name]
    rows = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    rows.append([curve.data_path, curve.array_index, [list(p.co) for p in curve.keyframe_points]])
    return json.dumps(rows)


def scaffold_b_body():
    """Cameras and cuts as in generation A, plus an owned area light and the actor's walk moved into an owned NLA
    track (the pattern of cinematic_sync_actions: the active Action is unassigned and both changes are touched rows)."""
    return scaffold_body().replace('\nresult = probe_cameras(params)\n', '') + r'''
def probe_b(params):
    data = probe_cameras(params)
    scene, src, cin = cm_candidate(params)
    light_data = bpy.data.lights.new(params['light'], 'AREA')
    light = cm_new_object(params, params['light'], light_data, cin, 'light')
    light.location = (3.0, -3.0, 4.0)
    light_data.energy = 200.0
    data['owned']['objects'].append(light.name)
    data['owned']['data'].append(['LIGHT', light_data.name])
    rig = cm_object(params['actor'], ['ARMATURE'])
    touched = [cm_touch_animdata(rig, 'OBJECT')]
    action = rig.animation_data.action
    track = cm_new_track(rig, 'OBJECT', params['track'], params)
    touched.append(cm_touch_track(rig, 'OBJECT', params['track']))
    cm_ensure_pose(rig)
    rig.animation_data.action = None
    strip = track.strips.new('walk', 1, action)
    data['touched'] = touched
    data['strip'] = [strip.frame_start, strip.frame_end]
    data['fingerprints'] = cm_fingerprints(params)
    return data

result = probe_b(params)
'''


def generation_b(bridge, storage, dispatch, model, store, brief, shots, aid, source_plan_fp):
    """Second generation: the change kinds generation A did not use (exposure, camera_offset, light_energy,
    clip_shift) in one reverted cycle, a kept-by-measurement cycle reverted by a REJECT review, then rejection."""
    created = dispatch('cinematic_create_candidate', bridge, storage, 'op-createB', brief=brief,
                       options={'shots': shots}, apply=True)
    ref = created['cinematic']
    path, plan, state = store.load(storage, ref)
    gid = plan['generation_id']
    names = {'CAM_A': model.owned_name(gid, 1, 'CAM_A'), 'CAM_B': model.owned_name(gid, 1, 'CAM_B')}
    light_name = model.owned_name(gid, 1, 'KEY')
    track_name = model.owned_name(gid, 1, 'BODY')

    class Ctx:
        pass
    ctx = Ctx()
    ctx.bridge, ctx.storage, ctx.operation_id = bridge, storage, 'op-scaffoldB'
    params = store.assembly_params(plan, state, {
        'names': names, 'cuts': [['CUT_A', 1, 'CAM_A'], ['CUT_B', 31, 'CAM_B']], 'light': light_name,
        'track': track_name, 'actor': 'ActorRig',
        'cameras': [['CAM_A', [6.0, 1.5, 1.0], [0.0, 1.5, 1.0], 35.0], ['CAM_B', [0.0, -6.0, 1.0], [0.0, 2.0, 1.0], 35.0]]})
    new, result = store.mutate(ctx, path, plan, state, 'probe_scaffold_b', scaffold_b_body(), params)
    new['state'] = 'ASSEMBLED'
    new['probe_scaffold'] = 'cameras, cuts, light, NLA track and ASSEMBLED written by the probe'
    store.record(path, new)
    candidate = bpy.data.scenes[created['scene']]
    rig = bpy.data.objects['ActorRig']
    cam_a = bpy.data.objects[names['CAM_A']]
    light = bpy.data.objects[light_name]
    strip = rig.animation_data.nla_tracks[track_name].strips['walk']

    def fps():
        p, pl, st = store.load(storage, ref)
        observed = store.status(bridge, pl, st)
        return {'source': observed['source']['fingerprint'], 'candidate': observed['candidate']['fingerprint']}, st

    before_fp, _ = fps()
    values = lambda: [candidate.view_settings.exposure, list(cam_a.delta_location), light.data.energy, strip.frame_start]  # noqa: E731
    before_values = values()
    cycle = dispatch('cinematic_refine', bridge, storage, 'op-b1', cinematic=ref, apply=True, options={
        'target': 'visibility_min_fraction', 'scope': {'camera': names['CAM_B']}, 'minimum_improvement': 0.5,
        'changes': [{'kind': 'exposure', 'exposure': 0.5},
                    {'kind': 'camera_offset', 'camera': names['CAM_A'], 'offset': [0.0, 0.0, 0.5]},
                    {'kind': 'light_energy', 'light': light_name, 'energy': 50.0},
                    {'kind': 'clip_shift', 'actor': aid, 'track': track_name, 'strip': 'walk', 'frames': 5}]})
    after_fp, st = fps()
    REPORT['measured']['generation_b_cycle1'] = cycle['refinement']
    applied = {row['kind']: row['after'] for row in cycle['refinement']['captured']}
    check('B cycle 1: exposure, camera_offset, light_energy and clip_shift applied, measured, reverted and verified',
          cycle['status'] == 'reverted' and cycle['refinement']['revert_verified'] and values() == before_values
          and after_fp == before_fp and applied == {'exposure': 0.5, 'camera_offset': [0.0, 0.0, 0.5],
                                                    'light_energy': 50.0, 'clip_shift': 6.0},
          {'captured': cycle['refinement']['captured'], 'values': values()})
    cycle2 = dispatch('cinematic_refine', bridge, storage, 'op-b2', cinematic=ref, apply=True, options={
        'target': 'actor_screen_height', 'goal': 0.75, 'scope': {'camera': names['CAM_A']}, 'minimum_improvement': 0.02,
        'changes': [{'kind': 'camera_settings', 'camera': names['CAM_A'], 'settings': {'lens': 55}}],
        'render': {'size': 128}})
    _, st = fps()
    rejected_review = dispatch('cinematic_review', bridge, storage, 'op-b2r', cinematic=ref, options={
        'review': synthetic_review(st['render'], decision='REJECT', direction_checklist={
            'composition': {'status': 'ISSUE', 'note': SYNTHETIC + ': mechanism test of the revert path'}})})
    fp2, st2 = fps()
    check('B cycle 2: improved by measurement, REJECT review reverts it (lens back, fingerprints back, no preview)',
          cycle2['status'] == 'visual_review_required' and rejected_review['status'] == 'reverted'
          and cam_a.data.lens == 35.0 and fp2 == before_fp and st2['state'] == 'ASSEMBLED' and st2.get('render') is None
          and st2['refinement_cycles'] == 2 and st2['last_refinement']['outcome'] == 'REFINEMENT_REVERTED_BY_REVIEW',
          rejected_review['refinement'])
    rejected = dispatch('cinematic_reject', bridge, storage, 'op-b3', cinematic=ref, apply=True)
    check('B rejected: source restored to the plan fingerprint, walk Action active again, owned track gone',
          rejected['source_restored'] and rig.animation_data.action is not None
          and rig.animation_data.action.name == 'Actor_Walk' and len(rig.animation_data.nla_tracks) == 0
          and fps_source_only(bridge, store, plan) == source_plan_fp, rejected.get('undo_failed'))


def fps_source_only(bridge, store, plan):
    return store.status(bridge, plan, {})['source']['fingerprint']


def main():
    global OUT
    OUT = Path(sys.argv[sys.argv.index('--') + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        from blender_mcp.production.cinematic import model, registry, review, store
        from blender_mcp.production.cinematic.service import dispatch
        from blender_mcp.production.common.asset_safety import current_asset
        build_fixture(OUT)
        bridge = LocalBridge()
        storage = Storage(OUT / 'store')
        register_open_fixture(bridge, storage, OUT)
        from blender_mcp.production.cinematic.service import HANDLERS, load_handlers
        load_handlers()
        aid = None
        if 'cinematic_register_actor' in HANDLERS:
            try:
                registered = dispatch('cinematic_register_actor', bridge, storage, 'op-register', object_name='ActorRig',
                                      options={'role': 'PROTAGONIST'})
                aid = registered['actor_id']
                REPORT['notes'].append('actor registered with the real cinematic_register_actor handler')
            except Exception as exc:
                REPORT['notes'].append('cinematic_register_actor failed, registry.write fallback: ' + str(exc)[:300])
        if aid is None:
            key = registry.asset_key(current_asset(bridge, storage), 'Scene')
            aid = registry.write(storage, key, {'object': 'ActorRig', 'data': 'ActorRig', 'meshes': ['ActorBody'],
                                                'fingerprint': 'probe-fingerprint', 'role': 'PROTAGONIST'})['actor_id']
            REPORT['notes'].append('actor written with registry.write (no digests: integrity provider cannot pass)')
        brief = {'schema': 'production.cinematic_brief.v1', 'duration_s': 2.0, 'fps': FPS,
                 'actors': [{'actor_id': aid, 'role': 'PROTAGONIST'}]}
        shots = {'schema': 'production.cinematic_shots.v1', 'shots': [
            {'shot_id': 'S1_WIDE', 'frame_start': 1, 'frame_end': 30, 'type': 'WIDE', 'camera_id': 'CAM_A',
             'purpose': 'Establecer al actor caminando hacia la cámara B'},
            {'shot_id': 'S2_SIDE', 'frame_start': 31, 'frame_end': 60, 'type': 'MEDIUM', 'camera_id': 'CAM_B',
             'purpose': 'Seguir la llegada del actor'}]}
        walk_before = action_digest('Actor_Walk')
        created = dispatch('cinematic_create_candidate', bridge, storage, 'op-create', brief=brief,
                           options={'shots': shots}, apply=True)
        ref = created['cinematic']
        path, plan, state = store.load(storage, ref)
        source_plan_fp = plan['source']['fingerprint']
        gid = plan['generation_id']
        names = {'CAM_A': model.owned_name(gid, 1, 'CAM_A'), 'CAM_B': model.owned_name(gid, 1, 'CAM_B')}

        class Ctx:
            pass
        ctx = Ctx()
        ctx.bridge, ctx.storage, ctx.operation_id = bridge, storage, 'op-scaffold'
        params = store.assembly_params(plan, state, {
            'names': names, 'cuts': [['CUT_A', 1, 'CAM_A'], ['CUT_B', 31, 'CAM_B']],
            'cameras': [['CAM_A', [6.0, 1.5, 1.0], [0.0, 1.5, 1.0], 35.0], ['CAM_B', [0.0, -6.0, 1.0], [0.0, 2.0, 1.0], 35.0]]})
        new, result = store.mutate(ctx, path, plan, state, 'probe_scaffold_cameras_cuts', scaffold_body(), params)
        new['cameras'] = {k: dict(v) for k, v in result['cameras'].items()}
        new['state'] = 'ASSEMBLED'
        new['probe_scaffold'] = 'cameras, cuts and ASSEMBLED written by the probe (timeline/camera tools are other areas)'
        store.record(path, new)
        candidate = bpy.data.scenes[created['scene']]
        check('scaffold: two owned cameras and two camera markers on the candidate only',
              len([m for m in candidate.timeline_markers if m.camera]) == 2 and len(bpy.data.scenes['Scene'].timeline_markers) == 0,
              result['cameras'])

        def status_fp():
            p, pl, st = store.load(storage, ref)
            observed = store.status(bridge, pl, st)
            return {'source': observed['source']['fingerprint'], 'candidate': observed['candidate']['fingerprint']}, st

        def raw_state():
            rig = bpy.data.objects['ActorRig']
            return {'root': list(rig.pose.bones['root'].location), 'frame_source': bpy.data.scenes['Scene'].frame_current,
                    'frame_candidate': candidate.frame_current, 'camera_candidate': candidate.camera.name if candidate.camera else None,
                    'window': bpy.context.window.scene.name, 'engine': candidate.render.engine,
                    'resolution': [candidate.render.resolution_x, candidate.render.resolution_y],
                    'filepath': candidate.render.filepath}

        # ---------------------------------------------------------------- previs
        before_raw = raw_state()
        fp_before, _ = status_fp()
        began = time.perf_counter()
        previs = dispatch('cinematic_generate_previs', bridge, storage, 'op-previs', cinematic=ref, options={'size': 192})
        REPORT['metrics']['previs_elapsed_s'] = round(time.perf_counter() - began, 3)
        try:
            import gpu
            REPORT['measured']['gpu_renderer_after_render'] = gpu.platform.renderer_get()
            REPORT['measured']['gpu_backend'] = gpu.platform.backend_type_get()
        except Exception as exc:
            REPORT['measured']['gpu_renderer_after_render'] = 'unavailable: ' + str(exc)
        shots_by_id = {s['shot_id']: s for s in previs['shots']}
        check('previs: one representative frame per shot (centre), camera chosen by the markers',
              previs['status'] == 'previs_rendered' and shots_by_id['S1_WIDE']['frame'] == 15
              and shots_by_id['S2_SIDE']['frame'] == 45 and shots_by_id['S1_WIDE']['camera'] == names['CAM_A']
              and shots_by_id['S2_SIDE']['camera'] == names['CAM_B'] and all(s['cut_respected'] for s in previs['shots']),
              previs['shots'])
        sizes = [png_size(s['image']) for s in previs['shots']]
        colors = {s['shot_id']: dominant(top_center(s['image'])) for s in previs['shots']}
        check('previs: PNG files 192x108 and the backdrop colour proves which camera rendered each shot',
              sizes == [(192, 108), (192, 108)] and colors == {'S1_WIDE': 'RED', 'S2_SIDE': 'GREEN'}, [sizes, colors])
        previs_json = json.loads(Path(previs['previs_json']).read_text(encoding='utf-8'))
        pose = previs_json['shots'][0]['camera_pose']
        check('previs: metadata has camera pose, lens, actors, time and narrative purpose; storyboard exists',
              Path(previs['storyboard']).is_file() and pose['lens_mm'] == 35.0 and abs(pose['location'][0] - 6.0) < 1e-6
              and previs['shots'][0]['actors_visible'] == [aid] and previs['shots'][0]['time_s'] == round(14 / FPS, 4)
              and 'Establecer' in previs['shots'][0]['purpose'] and previs_json['render_backend']['engine'] == 'BLENDER_WORKBENCH',
              {'pose': pose, 'backend': previs_json['render_backend']})
        fp_after, _ = status_fp()
        after_raw = raw_state()
        check('previs: source/candidate fingerprints, frames, camera, render settings and window scene restored',
              fp_after == fp_before and after_raw == before_raw, {'before': before_raw, 'after': after_raw})
        REPORT['measured']['previs_actor_stats_S1'] = previs_json['shots'][0]['actors']

        # ---------------------------------------------------------------- preview
        began = time.perf_counter()
        preview = dispatch('cinematic_render_preview', bridge, storage, 'op-preview', cinematic=ref, options={'size': 192})
        REPORT['metrics']['preview_elapsed_s'] = round(time.perf_counter() - began, 3)
        REPORT['metrics']['preview'] = preview['metrics']
        frames, controls = apng_frames(preview['animations'][0]['path'])
        check('preview: every frame of the range rendered, temporal_complete, APNG with 60 frames',
              preview['status'] == 'rendered' and preview['temporal_complete'] and preview['frames'] == 60
              and preview['frame_range'] == [1, 60] and frames == 60 and controls == 60, [frames, controls, preview['frame_range']])
        check('preview: cuts respected (camera per frame = marker camera)',
              preview['cuts_respected'] and preview['segments'] == [[names['CAM_A'], 1, 30], [names['CAM_B'], 31, 60]],
              preview['segments'])
        frame_dir = Path(preview['output_dir']) / 'frames'
        per_frame = {f: dominant(top_center(frame_dir / ('frame_' + format(f, '06d') + '.png'))) for f in range(1, 61)}
        wrong = [f for f, c in per_frame.items() if c != ('RED' if f <= 30 else 'GREEN')]
        check('preview: pixels of all 60 frames show camera A until 30 and camera B from 31', not wrong, wrong[:10])
        check('preview: backend reported, state restored, candidate fingerprint bound, player with 1, 1/2, 1/4 and stepping',
              preview['render_backend']['engine'] == 'BLENDER_WORKBENCH' and preview['state_restored']
              and preview['candidate_fingerprint'] == status_fp()[0]['candidate']
              and all(t in Path(preview['player']).read_text(encoding='utf-8') for t in ('value="1.0"', 'value="0.5"', 'value="0.25"', 'id="next"', 'id="prev"')),
              preview['render_backend'])
        check('preview: window scene, source and candidate unchanged after preview', status_fp()[0] == fp_before
              and raw_state() == before_raw)

        # ---------------------------------------------------------------- review gates
        _, st = status_fp()
        render = st['render']
        expect('review with a score', 'VISUAL_REVIEW_SCORE_FORBIDDEN', lambda: dispatch(
            'cinematic_review', bridge, storage, 'op-r1', cinematic=ref, options={'review': synthetic_review(render, score=9)}))
        expect('review with a nested rating', 'VISUAL_REVIEW_SCORE_FORBIDDEN', lambda: dispatch(
            'cinematic_review', bridge, storage, 'op-r1b', cinematic=ref, options={'review': synthetic_review(
                render, direction_checklist={'mood': {'status': 'OK', 'note': 'x', 'rating': 5}})}))
        expect('review pointing to another artefact (stale)', 'VISUAL_REVIEW_STALE', lambda: dispatch(
            'cinematic_review', bridge, storage, 'op-r2', cinematic=ref,
            options={'review': synthetic_review(render, artifact_reference=previs['output_dir'])}))
        expect('review without quarter speed', 'VISUAL_REVIEW_INCOMPLETE', lambda: dispatch(
            'cinematic_review', bridge, storage, 'op-r3', cinematic=ref,
            options={'review': synthetic_review(render, speeds_reviewed=[1])}))
        expect('review missing a shot', 'VISUAL_REVIEW_INCOMPLETE', lambda: dispatch(
            'cinematic_review', bridge, storage, 'op-r4', cinematic=ref,
            options={'review': synthetic_review(render, shots_reviewed=['S1_WIDE'])}))
        expect('ACCEPT without the art-direction checklist', 'VISUAL_REVIEW_CHECKLIST_REQUIRED', lambda: dispatch(
            'cinematic_review', bridge, storage, 'op-r5', cinematic=ref,
            options={'review': synthetic_review(render, direction_checklist={'mood': {'status': 'OK', 'note': ''}})}))
        cam_a = bpy.data.objects[names['CAM_A']]
        cam_a.data.lens = 40.0  # edit outside the tools
        expect('review after an external candidate edit', 'CINEMATIC_STALE_CANDIDATE', lambda: dispatch(
            'cinematic_review', bridge, storage, 'op-r6', cinematic=ref, options={'review': synthetic_review(render)}))
        cam_a.data.lens = 35.0
        reviewed = dispatch('cinematic_review', bridge, storage, 'op-r7', cinematic=ref,
                            options={'review': synthetic_review(render)})
        rj = json.loads(Path(reviewed['review']['review_json']).read_text(encoding='utf-8'))
        md = Path(reviewed['review']['review_md']).read_text(encoding='utf-8')
        check('review: REVIEWED, review.json/md with TECHNICAL FINDINGS and ARTISTIC OBSERVATIONS separate, synthetic flagged',
              reviewed['state'] == 'REVIEWED' and 'TECHNICAL_FINDINGS' in rj and 'ARTISTIC_OBSERVATIONS' in rj
              and '## TECHNICAL FINDINGS' in md and '## ARTISTIC OBSERVATIONS' in md and rj['synthetic_review'] is True
              and review._forbidden_keys(rj) == [], reviewed['review'])

        # ---------------------------------------------------------------- refinement
        refine_a = {'target': 'actor_screen_height', 'goal': 0.75, 'scope': {'camera': names['CAM_A']}}
        expect('invalid target', 'REFINEMENT_TARGET_UNSUPPORTED', lambda: dispatch(
            'cinematic_refine', bridge, storage, 'op-f0', cinematic=ref, apply=True,
            options={'target': 'aaa_score', 'changes': [{'kind': 'exposure', 'exposure': 0.5}], 'minimum_improvement': 0.1}))
        expect('change on a source (not owned) camera', 'REFINEMENT_TARGET_NOT_OWNED', lambda: dispatch(
            'cinematic_refine', bridge, storage, 'op-f0b', cinematic=ref, apply=True,
            options=dict(refine_a, changes=[{'kind': 'camera_settings', 'camera': 'SourceCam', 'settings': {'lens': 50}}],
                         minimum_improvement=0.02)))
        check('the source camera was not touched', bpy.data.objects['SourceCam'].data.lens == 35.0,
              bpy.data.objects['SourceCam'].data.lens)
        check('rejected targets/changes did not consume a cycle', status_fp()[1].get('refinement_cycles') == 0,
              status_fp()[1].get('refinement_cycles'))
        proposal = dispatch('cinematic_refine', bridge, storage, 'op-f1p', cinematic=ref, options=dict(
            refine_a, changes=[{'kind': 'camera_settings', 'camera': names['CAM_A'], 'settings': {'lens': 55}}]))
        REPORT['measured']['actor_screen_height_error_lens35'] = proposal['findings']
        check('refine proposal measures the target without changing Blender or spending a cycle',
              proposal['status'] == 'proposal' and status_fp()[1].get('refinement_cycles') == 0 and cam_a.data.lens == 35.0
              and status_fp()[0] == fp_before, proposal['findings'])
        cycle1 = dispatch('cinematic_refine', bridge, storage, 'op-f1', cinematic=ref, apply=True, options=dict(
            refine_a, changes=[{'kind': 'camera_settings', 'camera': names['CAM_A'], 'settings': {'lens': 55}}],
            minimum_improvement=0.02, render={'size': 192}))
        REPORT['measured']['refine_cycle1'] = cycle1['refinement']
        check('cycle 1 (lens 35 -> 55) improves the measured target and waits for a review',
              cycle1['status'] == 'visual_review_required' and cycle1['refinement']['improved']
              and cycle1['refinement']['after'] < cycle1['refinement']['before'] and abs(cam_a.data.lens - 55.0) < 1e-6
              and cycle1['refinement_cycles'] == 1 and cycle1['pending_refinement'], cycle1['refinement'])
        _, st = status_fp()
        expect('review of the previous preview after refinement', 'VISUAL_REVIEW_STALE', lambda: dispatch(
            'cinematic_review', bridge, storage, 'op-r8', cinematic=ref, options={'review': synthetic_review(render)}))
        expect('accept while a refinement waits for review', 'REFINEMENT_REVIEW_REQUIRED', lambda: dispatch(
            'cinematic_accept', bridge, storage, 'op-a0', cinematic=ref, apply=True))
        expect('refine while a refinement waits for review', 'REFINEMENT_REVIEW_REQUIRED', lambda: dispatch(
            'cinematic_refine', bridge, storage, 'op-f1b', cinematic=ref, apply=True, options=dict(
                refine_a, changes=[{'kind': 'exposure', 'exposure': 0.1}], minimum_improvement=0.02)))
        kept = dispatch('cinematic_review', bridge, storage, 'op-r9', cinematic=ref,
                        options={'review': synthetic_review(st['render'])})
        check('ACCEPT review keeps the refinement (REVIEWED, nothing pending)', kept['status'] == 'refined'
              and kept['state'] == 'REVIEWED' and not kept['pending_refinement'], kept['refinement']['outcome'])
        fp_cycle1, st_cycle1 = status_fp()
        cycle2 = dispatch('cinematic_refine', bridge, storage, 'op-f2', cinematic=ref, apply=True, options=dict(
            refine_a, changes=[{'kind': 'camera_settings', 'camera': names['CAM_A'], 'settings': {'shift_x': 0.05}}],
            minimum_improvement=0.02))
        fp_c2, st_c2 = status_fp()
        REPORT['measured']['refine_cycle2'] = cycle2['refinement']
        check('cycle 2 (shift_x) does not improve -> reverted and verified by fingerprint',
              cycle2['status'] == 'reverted' and cycle2['refinement']['outcome'] == 'REFINEMENT_REVERTED'
              and cycle2['refinement']['revert_verified'] and cam_a.data.shift_x == 0.0 and fp_c2 == fp_cycle1
              and st_c2['refinement_cycles'] == 2 and st_c2['state'] == 'REVIEWED'
              and st_c2['render']['output_dir'] == st_cycle1['render']['output_dir'], cycle2['refinement'])
        cycle3 = dispatch('cinematic_refine', bridge, storage, 'op-f3', cinematic=ref, apply=True, options=dict(
            refine_a, changes=[{'kind': 'actor_offset', 'actor': aid, 'offset': [-3.0, 0.0, 0.0]}],
            minimum_improvement=0.02))
        fp_c3, st_c3 = status_fp()
        REPORT['measured']['refine_cycle3'] = cycle3['refinement']
        touched = st_c3['assembly']['touched']
        check('cycle 3 (shared actor delta) worsens -> reverted; delta recorded as touched and restored on both scenes',
              cycle3['status'] == 'reverted' and tuple(bpy.data.objects['ActorRig'].delta_location) == (0.0, 0.0, 0.0)
              and fp_c3 == fp_cycle1 and any(r['kind'] == 'delta' and r['object'] == 'ActorRig' for r in touched)
              and st_c3['refinement_cycles'] == 3, {'refinement': cycle3['refinement'], 'touched': touched})
        expect('fourth refinement', 'REFINEMENT_LIMIT', lambda: dispatch(
            'cinematic_refine', bridge, storage, 'op-f4', cinematic=ref, apply=True, options=dict(
                refine_a, changes=[{'kind': 'camera_settings', 'camera': names['CAM_A'], 'settings': {'lens': 50}}],
                minimum_improvement=0.02)))

        # ---------------------------------------------------------------- quality report and acceptance
        expect('accept before any quality report', 'CINEMATIC_QUALITY_REPORT_REQUIRED', lambda: dispatch(
            'cinematic_accept', bridge, storage, 'op-a00', cinematic=ref, apply=True))
        real = dispatch('cinematic_quality_report', bridge, storage, 'op-q1', cinematic=ref)
        REPORT['measured']['quality_report_real_providers'] = real['sections']
        unprovided_pass = [s['section'] for s in real['sections'] if s['provider'] is None and s['status'] == 'PASS']
        mandatory_not_run = [s['section'] for s in real['sections'] if s['mandatory'] and s['status'] == 'NOT_RUN']
        check('quality report with the real providers: a missing provider is NOT_RUN, never PASS; passed only without '
              'mandatory NOT_RUN', not unprovided_pass and (real['passed'] is False if mandatory_not_run else True)
              and any(s['section'] == 'render' and s['status'] == 'PASS' for s in real['sections']),
              {'passed': real['passed'], 'mandatory_not_run': mandatory_not_run})
        qr = json.loads(Path(real['report']).read_text(encoding='utf-8'))
        check('quality report: TECHNICAL FINDINGS separate, ARTISTIC OBSERVATIONS empty (reviewer writes them), no score',
              'TECHNICAL_FINDINGS' in qr and qr['ARTISTIC_OBSERVATIONS']['observations'] == [] and 'score' not in qr,
              qr['rule'])
        REPORT['measured']['quality_report_real_passed'] = real['passed']
        original = review.run_section
        if not real['passed']:
            expect('accept with a quality report that did not pass', 'CINEMATIC_QUALITY_REPORT_NOT_PASSED', lambda: dispatch(
                'cinematic_accept', bridge, storage, 'op-a1', cinematic=ref, apply=True))

            def synthetic_section(ctx, path, plan, state, spec, options=None):
                if spec['id'] == 'render':
                    return original(ctx, path, plan, state, spec, options)
                return {'section': spec['id'], 'label': spec['label'], 'mandatory': review._mandatory(spec, plan, state),
                        'status': 'PASS', 'provider': 'SYNTHETIC_PROVIDER_FOR_MECHANISM_TEST', 'metrics': {},
                        'errors': [], 'error_count': 0, 'findings_by_severity': {}, 'warnings': []}
            review.run_section = synthetic_section
            REPORT['notes'].append('acceptance used SYNTHETIC_PROVIDER_FOR_MECHANISM_TEST quality sections because '
                                   'the real providers did not pass on this fixture')
        else:
            REPORT['notes'].append('acceptance used the real quality-report providers (reviews remain synthetic)')
        try:
            passed = dispatch('cinematic_quality_report', bridge, storage, 'op-q2', cinematic=ref)
            check('quality report passes and is bound to the candidate fingerprint',
                  passed['passed'] and status_fp()[1]['quality_report']['candidate_fingerprint'] == fp_cycle1['candidate'],
                  passed['sections'])
            _, st = status_fp()
            dispatch('cinematic_review', bridge, storage, 'op-r10', cinematic=ref,
                     options={'review': synthetic_review(st['render'], decision='REFINE')})
            expect('accept after a REFINE decision', 'CINEMATIC_ACCEPT_REVIEW_REQUIRED', lambda: dispatch(
                'cinematic_accept', bridge, storage, 'op-a2', cinematic=ref, apply=True))
            again = dispatch('cinematic_review', bridge, storage, 'op-r11', cinematic=ref,
                             options={'review': synthetic_review(st['render'])})
            check('a second review of the same preview gets its own files', again['review']['review_json'].endswith('review_003.json'),
                  again['review']['review_json'])
            proposal = dispatch('cinematic_accept', bridge, storage, 'op-a3', cinematic=ref)
            check('accept proposal: gates met, nothing changed', proposal['status'] == 'proposal'
                  and bpy.data.scenes[created['scene']].get('production_m10_lifecycle') is None)
            accepted = dispatch('cinematic_accept', bridge, storage, 'op-a4', cinematic=ref, apply=True)
        finally:
            review.run_section = original
        fp_final, st_final = status_fp()
        check('accept: ACCEPTED terminal, candidate scene marked, source scene kept as the window scene',
              accepted['status'] == 'accepted' and st_final['state'] == 'ACCEPTED'
              and bpy.data.scenes[created['scene']].get('production_m10_lifecycle') == 'ACCEPTED'
              and bpy.context.window.scene.name == 'Scene' and accepted['acceptance']['source_scene_replaced'] is False
              and fp_final == fp_cycle1 and accepted['acceptance']['synthetic_review'] is True, accepted['acceptance'])
        expect('refine after acceptance', 'GENERATION_TERMINAL', lambda: dispatch(
            'cinematic_refine', bridge, storage, 'op-f5', cinematic=ref, apply=True, options=dict(
                refine_a, changes=[{'kind': 'exposure', 'exposure': 0.1}], minimum_improvement=0.02)))
        expect('preview after acceptance', 'GENERATION_TERMINAL', lambda: dispatch(
            'cinematic_render_preview', bridge, storage, 'op-p9', cinematic=ref))
        source = bpy.data.scenes['Scene']
        check('the source never changed: plan fingerprint, Action keys, no markers, delta zero',
              fp_final['source'] == source_plan_fp and action_digest('Actor_Walk') == walk_before
              and len(source.timeline_markers) == 0 and tuple(bpy.data.objects['ActorRig'].delta_location) == (0, 0, 0)
              and source.camera is None, {'source_fp': fp_final['source'], 'plan_fp': source_plan_fp})
        generation_b(bridge, storage, dispatch, model, store, brief, shots, aid, source_plan_fp)
        REPORT['metrics'].update(bridge_calls=bridge.calls, payload_bytes_total=bridge.bytes,
                                 payload_bytes_max=bridge.max_bytes)
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    save()
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
