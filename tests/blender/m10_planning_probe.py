"""M10 planning in real Blender (own background process, inline fixture): the request's example — a creature emerges,
approaches, looks and prepares to attack, 15 s at 30 fps — planned end to end with registered actors, checking the
seconds->frames conversions, the measured inputs the planners use, and that no planning tool touches Blender
(scene fingerprint, frame, poses, active Actions and datablock counts identical before and after).

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_planning_probe.py -- <out_dir>
"""
from __future__ import annotations

import ast
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

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.payload import link_payload  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402

FRAME = re.compile(r'frame_set\(|m3_set_frame\(|m2_set_frame\(')
ASSIGN = re.compile(r'animation_data\.action\s*=(?!=)|m2_assign\(|m3_assign\(|mv_assign\(|\.action_slot\s*=(?!=)')
POSE = re.compile(r'\.rotation_quaternion\s*=(?!=)|\.location\s*=(?!=)|pose_position\s*=(?!=)')


class LocalBridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.bodies = []

    def run(self, body, params=None):
        code = build_code(body, params)
        self.bodies.append({'body': body, 'bytes': len(code.encode('utf-8')),
                            'nodes': sum(1 for _ in ast.walk(ast.parse(code)))})
        ns = {}
        exec(code.rsplit('print(', 1)[0], ns)
        return ns['result']


REPORT = {'suite': 'planning', 'blender': bpy.app.version_string, 'checks': [], 'errors': [], 'tools': {}, 'measured': {}}
OUT = None


def check(name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    (OUT / 'm10_planning.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def expect(name, code, function):
    try:
        function()
    except Exception as exc:
        check(name + ' -> ' + code, str(exc).startswith(code), str(exc)[:300])
        return
    check(name + ' -> ' + code, False, 'unexpected success')


def add_rig(name, location, body_name, body_size, bone_tail):
    data = bpy.data.armatures.new(name + 'Rig')
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    # The root bone points along +Y so a pose translation on its local Y travels horizontally in the world.
    for bone_name, head, tail, parent in (('root', (0, 0, 0), bone_tail, None), ('spine', (0, 0, 0.5), (0, 0, 1.1), 'root'),
                                          ('head', (0, 0, 1.1), (0, 0, 1.5), 'spine')):
        bone = data.edit_bones.new(bone_name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.location = location
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    body = bpy.context.active_object
    body.name = body_name
    body.scale = body_size
    # Identity parent inverse: the body location is local to the rig, so it stands on the ground under it.
    body.parent = rig
    body.location = (0.0, 0.0, body_size[2] / 2)
    return rig


def key_action(rig, name, keys, active):
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    rig.animation_data_create().action = action
    for frame, root_y, spine in keys:
        rig.pose.bones['root'].location = (0, root_y, 0)
        rig.pose.bones['root'].keyframe_insert('location', frame=frame)
        rig.pose.bones['spine'].rotation_quaternion = (1, 0, 0, spine)
        rig.pose.bones['spine'].keyframe_insert('rotation_quaternion', frame=frame)
    if not active:
        rig.animation_data.action = None
    return action


def build_fixture(out):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps, scene.render.fps_base = 30, 1.0
    scene.frame_start, scene.frame_end = 1, 450
    creature = add_rig('Creature', (0.0, -6.0, 0.0), 'CreatureBody', (0.8, 1.6, 1.2), (0, 0.5, 0))
    key_action(creature, 'Creature_Idle', ((1, 0.0, 0.0), (61, 0.0, 0.05)), active=False)
    key_action(creature, 'Creature_Walk', ((1, 0.0, 0.0), (31, 1.5, 0.05)), active=True)
    hero = add_rig('Hero', (0.0, 2.0, 0.0), 'HeroBody', (0.5, 0.3, 1.8), (0, 0.5, 0))
    key_action(hero, 'Hero_Idle', ((1, 0.0, 0.0), (61, 0.0, 0.08)), active=True)
    orphan = bpy.data.actions.new('Orphan_Action')
    orphan.use_fake_user = True
    bpy.ops.mesh.primitive_plane_add(size=30)
    bpy.context.active_object.name = 'Ground'
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(3.0, 0.0, 0.5))
    bpy.context.active_object.name = 'Rock'
    scene.frame_set(1)
    path = out / 'm10_planning_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


def measure_root_motion(rig, action, first, last):
    """What cinematic_register_actor records as root_motion, measured here on the evaluated root bone (the probe
    owns this process; it runs before the 'before' snapshot)."""
    scene = bpy.context.scene
    previous = rig.animation_data.action
    rig.animation_data.action = action
    points = []
    for frame in (first, last):
        scene.frame_set(frame)
        evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        points.append(evaluated.matrix_world @ evaluated.pose.bones['root'].head)
    rig.animation_data.action = previous
    scene.frame_set(1)
    dx, dy = points[1].x - points[0].x, points[1].y - points[0].y
    distance = math.hypot(dx, dy)
    return {'horizontal_distance_m': distance, 'duration_frames': last - first,
            'direction': [dx / distance, dy / distance] if distance > 1e-9 else None,
            'vertical_distance_m': points[1].z - points[0].z, 'bone': 'root', 'source': 'm10_planning_probe evaluated root head'}


def snapshot(bridge, fingerprint_body):
    scene = bpy.data.scenes['Scene']
    data = {'fingerprint': bridge.run(fingerprint_body, {'scene': 'Scene'})['fingerprint'],
            'frame': [scene.frame_current, scene.frame_subframe], 'camera': scene.camera.name if scene.camera else None,
            'markers': len(scene.timeline_markers), 'is_dirty': bpy.data.is_dirty,
            'counts': {k: len(getattr(bpy.data, k)) for k in ('objects', 'actions', 'scenes', 'cameras', 'lights', 'collections',
                                                            'worlds', 'meshes', 'armatures', 'materials', 'images')},
            'active_actions': {o.name: (o.animation_data.action.name if o.animation_data and o.animation_data.action else None)
                               for o in bpy.data.objects},
            'pose': {o.name: [[b.name] + [round(v, 9) for v in list(b.location) + list(b.rotation_quaternion)]
                              for b in o.pose.bones] for o in bpy.data.objects if o.type == 'ARMATURE'}}
    return data


def main():
    global OUT
    OUT = Path(sys.argv[sys.argv.index('--') + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        from blender_mcp.production.cinematic import model, planning, registry
        from blender_mcp.production.cinematic.runtime import COMMON
        from blender_mcp.production.cinematic.service import HANDLERS, dispatch, load_handlers
        from blender_mcp.production.common.asset_safety import current_asset
        build_fixture(OUT)
        creature, hero = bpy.data.objects['Creature'], bpy.data.objects['Hero']
        walk = measure_root_motion(creature, bpy.data.actions['Creature_Walk'], 1, 31)
        idle = measure_root_motion(creature, bpy.data.actions['Creature_Idle'], 1, 61)
        hero_idle = measure_root_motion(hero, bpy.data.actions['Hero_Idle'], 1, 61)
        REPORT['measured']['root_motion'] = {'Creature_Walk': walk, 'Creature_Idle': idle, 'Hero_Idle': hero_idle}
        check('fixture root motion measured (walk travels, idles stay)', abs(walk['horizontal_distance_m'] - 1.5) < 1e-4
              and idle['horizontal_distance_m'] < 1e-6 and hero_idle['horizontal_distance_m'] < 1e-6, REPORT['measured']['root_motion'])
        bridge = LocalBridge()
        storage = Storage(OUT / 'store')
        register_open_fixture(bridge, storage, OUT)
        key = registry.asset_key(current_asset(bridge, storage), 'Scene')
        creature_record = registry.write(storage, key, {
            'object': 'Creature', 'data': 'CreatureRig', 'type': 'ARMATURE', 'role': 'CREATURE', 'meshes': ['CreatureBody'],
            'fingerprint': 'probe-creature', 'facial': [], 'secondary': [],
            # Fixture declaration, not a measurement: the registry field the reach check reads.
            'interaction_capabilities': {'reach_m': 1.2},
            'body_actions': [{'action': 'Creature_Walk', 'slot': 'OBCreature', 'frame_range': [1, 31], 'root_motion': walk},
                             {'action': 'Creature_Idle', 'slot': 'OBCreature', 'frame_range': [1, 61], 'root_motion': idle}]})
        hero_record = registry.write(storage, key, {
            'object': 'Hero', 'data': 'HeroRig', 'type': 'ARMATURE', 'role': 'PROTAGONIST', 'meshes': ['HeroBody'],
            'fingerprint': 'probe-hero', 'facial': [{'ref': 'facial-plan', 'state': 'FACIAL_CANDIDATE'}], 'secondary': [],
            'body_actions': [{'action': 'Hero_Idle', 'slot': 'OBHero', 'frame_range': [1, 61], 'root_motion': hero_idle}]})
        cid, hid = creature_record['actor_id'], hero_record['actor_id']
        load_handlers()
        names = ('cinematic_create_brief', 'cinematic_plan_beats', 'cinematic_plan_shots', 'cinematic_plan_actor_paths',
                 'cinematic_plan_interaction', 'cinematic_render_plan')
        check('planning handlers registered', set(names) <= set(HANDLERS))
        fingerprint_body = COMMON + "\nresult = cm_source_fingerprint(bpy.data.scenes[params['scene']])\n"
        before = snapshot(bridge, fingerprint_body)

        def tool(name, **kwargs):
            start = len(bridge.bodies)
            began = time.perf_counter()
            try:
                return dispatch(name, bridge, storage, 'probe-' + name, **kwargs)
            finally:
                sent = bridge.bodies[start:]
                row = REPORT['tools'].setdefault(name, {'calls': 0, 'payloads': 0, 'max_bytes': 0, 'max_nodes': 0,
                                                        'state_changing_payloads': 0, 'elapsed_ms': []})
                row['calls'] += 1
                row['payloads'] += len(sent)
                row['elapsed_ms'].append(round((time.perf_counter() - began) * 1000, 2))
                for item in sent:
                    row['max_bytes'] = max(row['max_bytes'], item['bytes'])
                    row['max_nodes'] = max(row['max_nodes'], item['nodes'])
                    linked = link_payload(item['body'])
                    if FRAME.search(linked) or ASSIGN.search(linked) or POSE.search(linked):
                        row['state_changing_payloads'] += 1

        intent = {'title': 'Creature reveal', 'duration_s': 15.0, 'fps': 30,
                  'actors': [{'object': 'Creature', 'role': 'CREATURE', 'label': 'creature'}, {'object': 'Hero', 'label': 'hero'}],
                  'events': [{'event_id': 'emerge', 'kind': 'REVEAL', 'actors': ['creature'], 'time_s': 2.0,
                              'description': 'The creature emerges from the dark'},
                             {'event_id': 'approach', 'kind': 'APPROACH', 'actors': ['creature'], 'target': 'hero',
                              'description': 'It approaches the hero', 'action_refs': ['Creature_Walk']},
                             {'event_id': 'look', 'kind': 'GAZE', 'actors': ['creature'], 'target': 'hero',
                              'description': 'It looks at the hero'},
                             {'event_id': 'prepare', 'kind': 'PREPARE_ATTACK', 'actors': ['creature'], 'target': 'hero',
                              'time_s': 11.0, 'description': 'It prepares to attack'}],
                  'atmosphere': {'mood': 'ominous', 'mood_preset': 'LOW_KEY'}}
        created = tool('cinematic_create_brief', brief=intent)
        brief = created['brief']
        groups = brief['provenance_groups']
        check('brief ready with both registered actors', created['status'] == 'success' and brief['ready']
              and [a['actor_id'] for a in brief['actors']] == [cid, hid], created['summary'])
        check('provenance keeps the three values separated', set(groups) == set(model.PROVENANCE)
              and 'fps' in groups['USER_REQUIREMENT'] and 'fps_base' in groups['INFERRED_DEFAULT']
              and 'lighting_intentions' in groups['PROPOSED_CREATIVE_DECISION']
              and len({n for v in groups.values() for n in v}) == sum(len(v) for v in groups.values()), groups)
        measured = brief['available_performances'][cid]['measured']
        # Expected bounds read directly from bpy (independent of the payload): max over rig and body dimensions.
        dims = {n: list(bpy.data.objects[n].dimensions) for n in ('Creature', 'CreatureBody', 'Hero', 'HeroBody')}
        REPORT['measured']['dimensions'] = dims
        radius = {a: max(math.hypot(dims[n][0], dims[n][1]) / 2 for n in (a, a + 'Body')) for a in ('Creature', 'Hero')}
        height = max(dims['Creature'][2], dims['CreatureBody'][2])
        check('actor bounds measured read-only from the scene', abs(measured['height_m'] - height) < 1e-5
              and abs(measured['footprint_radius_m'] - radius['Creature']) < 1e-5
              and measured['location_m'] == [0.0, -6.0, 0.0], {'payload': measured, 'bpy': dims})
        check('timeline 15 s at 30 fps = frames 1..450', brief['timeline']['frame_start'] == 1
              and brief['timeline']['frame_end'] == 450 and brief['timeline']['frames'] == 450, brief['timeline'])
        bad = dict(intent, actors=intent['actors'] + [{'object': 'Ghost'}],
                   required_performances=[{'actor': 'creature', 'body_actions': ['Creature_Run', 'Orphan_Action']},
                                          {'actor': 'hero', 'facial': True}])
        incomplete = tool('cinematic_create_brief', brief=bad)['brief']
        codes = sorted(m['code'] for m in incomplete['missing_prerequisites'])
        check('missing prerequisites reported, not invented', not incomplete['ready'] and codes == [
            'ACTION_NOT_AVAILABLE', 'ACTION_NOT_AVAILABLE', 'ACTOR_NOT_REGISTERED', 'FACIAL_PERFORMANCE_NOT_ACCEPTED'],
            incomplete['missing_prerequisites'])
        expect('brief at another fps', 'CINEMATIC_FPS_MISMATCH', lambda: tool('cinematic_create_brief', brief=dict(intent, fps=24)))

        beats = tool('cinematic_plan_beats', brief=brief)
        rows = beats['beats']
        check('beats of the request: Establishing/Reveal/Approach/Recognition/Threat/Ending',
              [b['kind'] for b in rows] == ['ESTABLISHING', 'REVEAL', 'APPROACH', 'RECOGNITION', 'THREAT', 'ENDING'],
              [[b['beat_id'], b['start_s'], b['end_s'], b['frame_start'], b['frame_end']] for b in rows])
        recomputed = [1 + math.floor(b['start_s'] * 30 + 0.5) for b in rows]
        check('every beat frame equals frame_start + floor(s * fps + 0.5) and beats are contiguous',
              recomputed == [b['frame_start'] for b in rows] and rows[-1]['frame_end'] == 450
              and all(b['frame_start'] == a['frame_end'] + 1 for a, b in zip(rows, rows[1:])), recomputed)
        check('user times anchor REVEAL (2 s -> 61) and THREAT (11 s -> 331)', rows[1]['frame_start'] == 61
              and rows[4]['frame_start'] == 331 and rows[1]['provenance']['start_s'] == 'USER_REQUIREMENT')
        shots = tool('cinematic_plan_shots', brief=brief, options={'beats': beats})
        shot_rows = shots['shots']
        check('shots contiguous and covering the timeline', shot_rows[0]['frame_start'] == 1 and shot_rows[-1]['frame_end'] == 450
              and all(b['frame_start'] == a['frame_end'] + 1 for a, b in zip(shot_rows, shot_rows[1:]))
              and all(s['type'] in model.SHOT_TYPES for s in shot_rows), [[s['shot_id'], s['type'], s['frame_start'],
                                                                         s['frame_end']] for s in shot_rows])
        paths = tool('cinematic_plan_actor_paths', brief=brief, options={'beats': beats})
        path = paths['paths'][0]
        stop = radius['Creature'] + radius['Hero'] + planning.PROPOSALS['clearance_m']
        distance = 8.0 - stop
        check('creature path reuses Creature_Walk with measured root motion (no new locomotion)',
              path['status'] == 'PLANNED' and path['clip']['action'] == 'Creature_Walk' and paths['no_locomotion_generated']
              and abs(path['clip']['repeat'] - distance / walk['horizontal_distance_m']) < 1e-5
              and path['clip']['start_frame'] == 151 and path['start'] == [0.0, -6.0, 0.0], path)
        check('clearance between straight paths measured against the proposal', paths['clearance']['pairs'][0]['passed'] is True
              and abs(paths['clearance']['pairs'][0]['min_distance_m'] - stop) < 1e-3, paths['clearance'])
        static = tool('cinematic_plan_actor_paths', brief=brief, options={'beats': beats, 'paths': [
            {'actor': 'hero', 'target': [3.0, 2.0, 0.0], 'departure_s': 1.0}]})['paths'][0]
        check('actor with only in-place clips gets static staging + cut', static['status'] == 'ALTERNATIVE_REQUIRED'
              and static['clip'] is None and static['alternative']['kind'] == 'STATIC_STAGING_WITH_CUT'
              and 'IN_PLACE' in static['alternative']['detail'], static['alternative'])
        interactions = tool('cinematic_plan_interaction', brief=brief, options={'beats': beats, 'paths': paths})
        by_id = {r['event_id']: r for r in interactions['interactions']}
        check('interactions scheduled from the beats', set(by_id) == {'approach', 'look', 'prepare'}
              and by_id['look']['frame'] == 241 and by_id['prepare']['frame'] == 331 and not interactions['animation_resolved'],
              {k: [v['frame'], v['reachability']['status']] for k, v in by_id.items()})
        reach = tool('cinematic_plan_interaction', brief=brief, options={'interactions': [
            {'kind': 'TOUCH_SURFACE', 'actors': ['hero'], 'spatial_target': 'Rock', 'frame': 30}]})['interactions'][0]
        check('reach without registry data is UNKNOWN, never zero', reach['reachability']['status'] == 'UNKNOWN'
              and reach['reachability']['reach_m'] == 'UNKNOWN', reach['reachability'])
        preview = tool('cinematic_render_plan', brief=brief)
        stage = preview['stages'][0]
        check('render plan: Workbench preview of all 450 frames with the measured 320x180 rate', stage['engine'] == 'BLENDER_WORKBENCH'
              and stage['frames'] == 450 and stage['resolution'] == [320, 180] and stage['estimate']['status'] == 'MEASURED_RATE'
              and preview['rendered'] is False, stage['estimate'])
        expect('EEVEE without explicit scope', 'CINEMATIC_FINAL_SCOPE_REQUIRED',
               lambda: tool('cinematic_render_plan', brief=brief, options={'quality_preset': 'PRODUCTION'}))
        cycles = {'quality_preset': 'DRAFT', 'engine': 'CYCLES', 'frame_range': [241, 330]}
        REPORT['measured']['factory_cycles_compute'] = bpy.context.preferences.addons['cycles'].preferences.compute_device_type
        expect('Cycles in a factory process without OptiX', 'CINEMATIC_RENDER_OPTIX_REQUIRED',
               lambda: tool('cinematic_render_plan', brief=brief, options=cycles))
        expect('existing output path', 'CINEMATIC_OUTPUT_EXISTS',
               lambda: tool('cinematic_render_plan', brief=brief, options={'output_path': str(OUT)}))
        fresh = OUT / 'render-plan-target'
        planned = tool('cinematic_render_plan', brief=brief, options={'output_path': str(fresh)})
        check('output path validated but never created', planned['output']['path'] == str(fresh) and not fresh.exists())
        # The records above are fixture declarations for the planning checks (reach, facial state). The candidate
        # re-measures registered actors (ACTOR_STALE), so it is planned with records written by the real tool.
        real = {}
        for old, name, role in ((cid, 'Creature', 'CREATURE'), (hid, 'Hero', 'PROTAGONIST')):
            real[old] = dispatch('cinematic_register_actor', bridge, storage, 'probe-register-' + name, object_name=name,
                                 options={'role': role})['actor_id']

        def swap(value):
            text = json.dumps(value)
            for old, new in real.items():
                text = text.replace(old, new)
            return json.loads(text)
        brief, beats, shots = swap(brief), swap(beats), swap(shots)
        proposal = dispatch('cinematic_create_candidate', bridge, storage, 'probe-candidate', brief=brief,
                            options={'beats': beats, 'shots': shots})
        check('create_candidate proposal accepts brief+beats+shots (normalize_plan)', proposal['status'] == 'proposal'
              and len(proposal['plan']['shots']) == len(shot_rows) and not any(s.get('production_m10_owner') for s in bpy.data.scenes))
        broken = json.loads(json.dumps(shots))
        broken['shots'][1]['frame_start'] = 30
        expect('overlapping shots refused by normalize_plan', 'CINEMATIC_SHOT_OVERLAP',
               lambda: dispatch('cinematic_create_candidate', bridge, storage, 'probe-broken', brief=brief,
                                options={'beats': beats, 'shots': broken}))
        after = snapshot(bridge, fingerprint_body)
        diff = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
        check('no planning call changed Blender (fingerprint, frame, poses, Actions, datablocks)', not diff, diff or before['counts'])
        # Negative control: the same snapshot must notice a frame change and a moved object, or the check above
        # would prove nothing.
        bpy.data.scenes['Scene'].frame_set(2)
        bpy.data.objects['Rock'].location.x += 0.5
        control = snapshot(bridge, fingerprint_body)
        changed = sorted(k for k in before if before[k] != control[k])
        check('negative control: the snapshot detects a frame change and a moved object',
              'frame' in changed and 'fingerprint' in changed, changed)
        bpy.data.objects['Rock'].location.x -= 0.5
        bpy.data.scenes['Scene'].frame_set(1)
        check('no planning payload sets frames, Actions, slots or poses',
              all(r['state_changing_payloads'] == 0 for r in REPORT['tools'].values()),
              {k: v['state_changing_payloads'] for k, v in REPORT['tools'].items()})
        check('every planning payload under 140000 bytes and 14000 AST nodes',
              all(r['max_bytes'] < 140_000 and r['max_nodes'] < 14_000 for r in REPORT['tools'].values()),
              {k: [v['max_bytes'], v['max_nodes']] for k, v in REPORT['tools'].items()})
        # Blender facts the planner relies on, read from RNA only.
        image = bpy.types.ImageFormatSettings.bl_rna.properties
        ffmpeg = bpy.types.FFmpegSettings.bl_rna.properties
        formats = {'media_type': [e.identifier for e in image['media_type'].enum_items],
                   'file_format': [e.identifier for e in image['file_format'].enum_items],
                   'ffmpeg_format': [e.identifier for e in ffmpeg['format'].enum_items],
                   'ffmpeg_codec': [e.identifier for e in ffmpeg['codec'].enum_items]}
        needed = all(value in formats[key] for spec in planning.RENDER_FORMATS.values() for key, value in spec.items())
        camera = bpy.types.Camera.bl_rna.properties
        eevee = bpy.types.SceneEEVEE.bl_rna.properties['taa_render_samples'].default
        REPORT['measured']['rna'] = {'camera_sensor_width_default': camera['sensor_width'].default,
                                     'camera_sensor_fit_default': camera['sensor_fit'].default,
                                     'eevee_taa_render_samples_default': eevee}
        check('render format enums, camera defaults and EEVEE default samples match the planner',
              needed and camera['sensor_width'].default == planning.CAMERA_DEFAULTS['sensor_width_mm']
              and camera['sensor_fit'].default == planning.CAMERA_DEFAULTS['sensor_fit'] and eevee == 64, REPORT['measured']['rna'])
        # OptiX accepted once this own process enables it. Preferences are changed in memory only: factory-startup
        # never saves them and autosave is switched off first for this process.
        bpy.context.preferences.use_preferences_save = False
        prefs = bpy.context.preferences.addons['cycles'].preferences
        prefs.compute_device_type = 'OPTIX'
        prefs.refresh_devices()
        for device in prefs.devices:
            device.use = device.type == 'OPTIX'
        devices = [[d.name, d.type, bool(d.use)] for d in prefs.devices]
        REPORT['measured']['optix_devices'] = devices
        if any(d[1] == 'OPTIX' for d in devices):
            cycles_plan = tool('cinematic_render_plan', brief=brief, options=cycles)
            final = cycles_plan['stages'][1]
            check('Cycles planned on OptiX GPU with NOT_MEASURED workload', final['backend'].startswith('GPU OPTIX')
                  and final['estimate']['status'] == 'NOT_MEASURED' and final['frames'] == 90, final['backend'])
        else:
            check('OptiX device visible to this process', False, devices)
        prefs.compute_device_type = 'NONE'
        REPORT['metrics'] = {'bridge_payloads': len(bridge.bodies),
                             'max_payload_bytes': max(b['bytes'] for b in bridge.bodies),
                             'max_payload_nodes': max(b['nodes'] for b in bridge.bodies)}
        REPORT['plan_summary'] = {'beats': [[b['beat_id'], b['start_s'], b['end_s'], b['frame_start'], b['frame_end']] for b in rows],
                                  'shots': [[s['shot_id'], s['type'], s['frame_start'], s['frame_end'], s['lens_mm'],
                                             s['suggested_distance_m']] for s in shot_rows],
                                  'creature_path': {k: path[k] for k in ('distance_m', 'speed_m_s', 'departure_s', 'arrival_s',
                                                                          'arrival_frame')},
                                  'preview_estimate_s': stage['estimate']['seconds']}
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    (OUT / 'm10_planning.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
