"""M10 area B in real Blender (own background process, inline controlled fixture): scene inventory, actor
registration (identity, idempotency, fingerprint change), actor inspection and source integrity before/after the
candidate, after a recorded NLA mutation, and after real edits to a source mesh, shape key and camera.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_actors_probe.py -- <out_dir>

The M7 and M8 ledgers used here are SYNTHETIC fixture ledgers (marked synthetic_fixture=True): they exercise the
reference validation and digest paths, not an M7/M8 generation.
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


REPORT = {'suite': 'actors', 'blender': bpy.app.version_string, 'checks': [], 'errors': [], 'tool_calls': [],
          'synthetic_ledgers': 'M7 and M8 ledgers in this probe are synthetic fixture ledgers'}
OUT = None


def save_report():
    (OUT / 'm10_actors.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def check(name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    save_report()


def expect(name, code, function):
    try:
        function()
    except Exception as exc:
        check(name + ' -> ' + code, str(exc).startswith(code), str(exc)[:300])
        return
    check(name + ' -> ' + code, False, 'unexpected success')


def make_armature(name, bones, location, rotation_z=0.0):
    data = bpy.data.armatures.new(name)
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    rig.location = location
    rig.rotation_euler = (0.0, 0.0, rotation_z)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for bone_name, head, tail, parent in bones:
        bone = data.edit_bones.new(bone_name)
        bone.head, bone.tail = head, tail
        bone.roll = 0.0
        if parent:
            bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    return rig


BIPED = [('root', (0, 0, 0), (0, 0.3, 0), None), ('hips', (0, 0, 1.0), (0, 0, 1.1), 'root'),
         ('spine', (0, 0, 1.1), (0, 0, 1.3), 'hips'), ('chest', (0, 0, 1.3), (0, 0, 1.5), 'spine'),
         ('neck', (0, 0, 1.5), (0, 0, 1.6), 'chest'), ('head', (0, 0, 1.6), (0, 0, 1.8), 'neck'),
         ('thigh.L', (0.1, 0, 1.0), (0.1, 0, 0.55), 'hips'), ('shin.L', (0.1, 0, 0.55), (0.1, 0, 0.1), 'thigh.L'),
         ('foot.L', (0.1, 0, 0.1), (0.1, -0.15, 0.0), 'shin.L'),
         ('thigh.R', (-0.1, 0, 1.0), (-0.1, 0, 0.55), 'hips'), ('shin.R', (-0.1, 0, 0.55), (-0.1, 0, 0.1), 'thigh.R'),
         ('foot.R', (-0.1, 0, 0.1), (-0.1, -0.15, 0.0), 'shin.R'),
         ('upper_arm.L', (0.2, 0, 1.45), (0.45, 0, 1.45), 'chest'), ('forearm.L', (0.45, 0, 1.45), (0.7, 0, 1.45), 'upper_arm.L'),
         ('hand.L', (0.7, 0, 1.45), (0.8, 0, 1.45), 'forearm.L'),
         ('upper_arm.R', (-0.2, 0, 1.45), (-0.45, 0, 1.45), 'chest'), ('forearm.R', (-0.45, 0, 1.45), (-0.7, 0, 1.45), 'upper_arm.R'),
         ('hand.R', (-0.7, 0, 1.45), (-0.8, 0, 1.45), 'forearm.R')]
CREATURE = [('root', (0, 0, 0), (0, 0.3, 0), None), ('spine', (0, 0, 0.5), (0, 0, 1.0), 'root'),
            ('head', (0, 0, 1.0), (0, 0, 1.3), 'spine')]


def root_action(rig, name, values):
    action = bpy.data.actions.new(name)
    rig.animation_data_create().action = action
    for frame, y in values:
        rig.pose.bones['root'].location = (0, y, 0)
        rig.pose.bones['root'].keyframe_insert('location', frame=frame)
        rig.pose.bones['spine'].rotation_quaternion = (1, 0, 0, 0.1 * frame / 60)
        rig.pose.bones['spine'].keyframe_insert('rotation_quaternion', frame=frame)
    return action


def build_fixture(out):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.frame_start, scene.frame_end = 1, 60
    hero = make_armature('HeroRig', BIPED, (-2.0, 0.0, 0.0))
    root_action(hero, 'Hero_Walk', ((1, 0.0), (30, 1.5), (60, 3.0)))
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    body = bpy.context.active_object
    body.name = 'HeroBody'
    body.data.name = 'HeroBodyMesh'
    for vertex in body.data.vertices:
        vertex.co = (vertex.co.x * 0.6, vertex.co.y * 0.3, vertex.co.z * 1.8 + 0.9)
    body.parent = hero
    body.modifiers.new('Armature', 'ARMATURE').object = hero
    body.data.materials.append(bpy.data.materials.new('HeroMat'))
    creature = make_armature('CreatureRig', CREATURE, (2.0, 0.0, 0.0), math.radians(90.0))
    root_action(creature, 'Creature_Prowl', ((1, 0.0), (60, 2.0)))
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.2, location=(0, 0, 0))
    face = bpy.context.active_object
    face.name = 'CreatureFace'
    face.data.name = 'CreatureFaceMesh'
    face.parent = creature
    face.location = (0.0, 0.0, 1.15)
    face.shape_key_add(name='Basis')
    smile = face.shape_key_add(name='Smile')
    smile.data[0].co.z += 0.05
    smile.value = 0.0  # Blender 5.2.1 creates Python-added keys at 1.0 (measured in M7)
    key = face.data.shape_keys
    action = bpy.data.actions.new('M7F_probe_v1')
    slot = action.slots.new(id_type='KEY', name=key.name)
    strip = action.layers.new('M7_FACE').strips.new(type='KEYFRAME')
    curve = strip.channelbag(slot, ensure=True).fcurves.new('key_blocks["Smile"].value', index=0)
    curve.keyframe_points.insert(1, 0.0)
    curve.keyframe_points.insert(30, 1.0)
    action['production_facial_owner'] = 'M7'
    action['production_facial_lifecycle'] = 'ACCEPTED'
    track = key.animation_data_create().nla_tracks.new()
    track.name = 'M7_FACE M7F_probe_v1'
    track.strips.new('M7_FACE M7F_probe_v1', 1, action)
    mesh = bpy.data.meshes.new('M8_probe_v1_BakedMesh')
    mesh.from_pydata([(0, 0, 0), (0.2, 0, 0), (0, 0.2, 0)], [], [(0, 1, 2)])
    baked = bpy.data.objects.new('M8_probe_v1_Baked', mesh)
    scene.collection.objects.link(baked)
    baked['production_m8_owned'] = True
    baked['production_generation_id'] = 'probe'
    baked['production_source_object'] = 'HeroCape'
    baked['production_secondary_state'] = 'ACCEPTED'
    cache = baked.modifiers.new('M8_PC2_PLAYBACK', 'MESH_CACHE')
    cache.cache_format = 'PC2'
    cache.frame_start = 1.0
    baked.hide_render = True
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0.0, 2.0, 0.25))
    crate = bpy.context.active_object
    crate.name = 'Crate'
    crate.data.materials.append(bpy.data.materials.new('CrateMat'))
    camera_data = bpy.data.cameras.new('SrcCamData')
    camera_data.lens = 35.0
    camera = bpy.data.objects.new('SrcCam', camera_data)
    camera.location = (0.0, -8.0, 1.6)
    camera.rotation_euler = (math.radians(90.0), 0.0, 0.0)
    scene.collection.objects.link(camera)
    scene.camera = camera
    scene.timeline_markers.new('SHOT_A', frame=1).camera = camera
    light_data = bpy.data.lights.new('SrcKeyData', 'AREA')
    light_data.energy = 500.0
    light_data.size = 2.0
    light = bpy.data.objects.new('SrcKey', light_data)
    light.location = (3.0, -3.0, 4.0)
    scene.collection.objects.link(light)
    world = bpy.data.worlds.new('SrcWorld')
    world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0.8
    scene.world = world
    scene.frame_set(1)
    path = out / 'm10_actors_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


def main():
    global OUT
    OUT = Path(sys.argv[sys.argv.index('--') + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        from blender_mcp.production.animation.generation import store as gstore
        from blender_mcp.production.cinematic import actors, lifecycle, model, registry, store
        from blender_mcp.production.cinematic.runtime import COMMON
        from blender_mcp.production.cinematic.service import Context, Request, dispatch
        from blender_mcp.production.physics.service import STATES as M8_STATES
        build_fixture(OUT)
        bridge = LocalBridge()
        storage = Storage(OUT / 'store')
        register_open_fixture(bridge, storage, OUT)
        state_body = COMMON + r'''
def probe_state(params):
    scene = bpy.context.scene
    return {'frame': [scene.frame_current, scene.frame_subframe], 'camera': scene.camera.name if scene.camera else None,
            'mode': bpy.context.mode, 'scenes': sorted(s.name for s in bpy.data.scenes),
            'counts': [len(bpy.data.objects), len(bpy.data.actions), len(bpy.data.meshes), len(bpy.data.scenes)],
            'actions': {a.name: cm_action_digest(a) for a in bpy.data.actions},
            'matrices': {o.name: [list(r) for r in o.matrix_world] for o in bpy.data.objects},
            'deltas': {o.name: [list(o.delta_location), list(o.delta_rotation_euler)] for o in bpy.data.objects},
            'pose': {o.name: {p.name: [list(p.location), list(p.rotation_quaternion)] for p in o.pose.bones}
                     for o in bpy.data.objects if o.type == 'ARMATURE' and o.pose},
            'keys': {k.name: [[b.name, b.value] for b in k.key_blocks] for k in bpy.data.shape_keys},
            'animation': {o.name: cm_animation_digest(o) for o in bpy.data.objects},
            'verts': {m.name: [list(v.co) for v in m.vertices][:4] for m in bpy.data.meshes},
            'source': cm_source_fingerprint(scene)['fingerprint']}

result = probe_state(params)
'''

        def state():
            return bridge.run(state_body, {})

        def tool(label, name, **kwargs):
            before_calls, before_bytes = bridge.calls, bridge.bytes
            begin = time.perf_counter()
            try:
                return dispatch(name, bridge, storage, 'op-' + label, **kwargs)
            finally:
                REPORT['tool_calls'].append({'label': label, 'tool': name, 'bridge_calls': bridge.calls - before_calls,
                                             'payload_bytes': bridge.bytes - before_bytes,
                                             'seconds': round(time.perf_counter() - begin, 4)})

        def read_only(label, name, **kwargs):
            before = state()
            result = tool(label, name, **kwargs)
            after = state()
            changed = sorted(k for k in before if before[k] != after[k])
            check(label + ': Blender state identical before/after (frame, camera, pose, keys, Actions, matrices, '
                  'NLA, vertices, scenes)', not changed, changed)
            return result

        # ---------------------------------------------------------------- synthetic M7 / M8 ledgers
        face_digest = bridge.run(COMMON + "\nresult = {'digest': cm_action_digest(bpy.data.actions['M7F_probe_v1'])}\n", {})['digest']
        m7_ref, _ = gstore.save(storage, 'probe-m7', {'object_name': 'CreatureFace', 'profile': {'armature': 'CreatureRig'},
                                                       'brief': {'fps': 30, 'frame_start': 1, 'frame_end': 60},
                                                       'head_policy': 'BODY_OWNS', 'synthetic_fixture': True},
                                 'facial', gstore.FACIAL_STATES)
        gstore.event(Path(m7_ref).parent, {'state': 'ACCEPTED', 'refinement_cycles': 0, 'candidate_action': 'M7F_probe_v1',
                                           'candidate_fingerprint': face_digest,
                                           'accepted_tracks': [['Key', 'M7_FACE M7F_probe_v1']]}, gstore.FACIAL_STATES)
        m8_ref, _ = gstore.save(storage, 'probe-m8', {'object_name': 'HeroCape',
                                                       'params': {'source_object': 'HeroCape', 'primary_objects': ['HeroRig']},
                                                       'brief': {'subsystem': 'CLOTH', 'fps': 30, 'frame_start': 1,
                                                                 'frame_end': 60}, 'synthetic_fixture': True},
                                 'physics', M8_STATES)
        gstore.event(Path(m8_ref).parent, {'state': 'ACCEPTED', 'refinement_cycles': 0, 'candidate': None,
                                           'baked_candidate': {'candidate_object': 'M8_probe_v1_Baked'},
                                           'cache': {'representation': 'PC2', 'sha256': '0' * 64},
                                           'bake_deviation': {'passed': True}}, M8_STATES)

        # The read-only comparisons are only as good as the state they compare: prove it is not empty.
        sample = state()
        sizes = {'pose_bones': sum(len(v) for v in sample['pose'].values()), 'shape_keys': sum(len(v) for v in sample['keys'].values()),
                 'actions': len(sample['actions']), 'objects': len(sample['matrices'])}
        check('probe state snapshot covers pose bones, shape keys, Actions and objects',
              sizes['pose_bones'] == len(BIPED) + len(CREATURE) and sizes['shape_keys'] == 2 and sizes['actions'] == 3
              and sizes['objects'] == 8, sizes)

        # ---------------------------------------------------------------- A. scene inventory
        scene_result = read_only('inspect_scene', 'cinematic_inspect_scene')
        sections = scene_result['sections']
        candidates = {row['name']: row for row in sections['actor_candidates']}
        check('inspect_scene: both armatures are actor candidates with bound meshes and root channels',
              candidates.get('HeroRig', {}).get('kind') == 'ARMATURE' and candidates['HeroRig']['bound_meshes'] == ['HeroBody']
              and candidates['HeroRig']['root_channels'] == {'object_location': False, 'root_bone_location': ['root']}
              and candidates.get('CreatureRig', {}).get('bound_meshes') == ['CreatureFace'],
              {k: candidates.get(k) for k in ('HeroRig', 'CreatureRig')})
        camera = next(row for row in sections['cameras'] if row['name'] == 'SrcCam')
        light = next(row for row in sections['lights'] if row['name'] == 'SrcKey')
        check('inspect_scene: camera lens/sensor/clip/dof and light read from the source',
              camera['lens'] == 35.0 and camera['sensor'][0] == 36.0 and 'focus_distance' in camera['dof']
              and light['type'] == 'AREA' and light['energy'] == 500.0 and light['area'][1] == 2.0, [camera, light])
        check('inspect_scene: marker with camera, world and fps', sections['markers'] == [{'name': 'SHOT_A', 'frame': 1, 'camera': 'SrcCam'}]
              and scene_result['scene']['fps'] == 30
              and abs(scene_result['scene']['world']['background']['strength'] - 0.8) < 1e-6,
              {'markers': sections['markers'], 'scene': scene_result['scene']})
        props = [row['name'] for row in sections['props']]
        check('inspect_scene: props are static undeformed meshes only', 'Crate' in props and 'HeroBody' not in props
              and 'CreatureFace' not in props and 'M8_probe_v1_Baked' not in props, props)
        actions = {row['name']: row for row in sections['actions']}
        check('inspect_scene: Actions with slots, range and root channels', actions['Hero_Walk']['bone_location_sample'] == ['root']
              and actions['Hero_Walk']['frame_range'] == [1.0, 60.0] and actions['Hero_Walk']['slots'] == [['OBHeroRig', 'OBJECT']]
              and actions['M7F_probe_v1']['owner'] == 'M7', actions)
        m7 = sections['facial_m7']
        check('inspect_scene: M7 Action lifecycle and M7_FACE track on the Key',
              any(r['kind'] == 'ACTION' and r['name'] == 'M7F_probe_v1' and r['lifecycle'] == 'ACCEPTED' for r in m7)
              and any(r['kind'] == 'TRACK' and r['owner_type'] == 'KEY' and r['track'] == 'M7_FACE M7F_probe_v1' for r in m7), m7)
        m8 = sections['secondary_m8']
        check('inspect_scene: M8 owned object and M8_PC2_PLAYBACK modifier',
              any(r['kind'] == 'OBJECT' and r['name'] == 'M8_probe_v1_Baked' for r in m8)
              and any(r['kind'] == 'MODIFIER' and r['modifier'] == 'M8_PC2_PLAYBACK' and r['type'] == 'MESH_CACHE' for r in m8), m8)
        page = tool('inspect_scene_page', 'cinematic_inspect_scene', options={'limit': 1, 'offset': 1, 'sections': ['actions']})
        check('inspect_scene: pagination returns one row from offset with totals', list(page['sections']) == ['actions']
              and len(page['sections']['actions']) == 1 and page['pagination']['totals']['actions'] == len(bpy.data.actions)
              and page['sections']['actions'][0]['name'] == sorted(a.name for a in bpy.data.actions)[1], page['pagination'])

        # ---------------------------------------------------------------- B. registration
        before = state()
        hero = tool('register_hero', 'cinematic_register_actor', object_name='HeroRig',
                    options={'role': 'PROTAGONIST', 'secondary': [m8_ref]})
        after = state()
        check('register: Blender unchanged (registry is host-only)', before == after,
              sorted(k for k in before if before[k] != after[k]))
        record = registry.find(storage, hero['asset_key'], hero['actor_id'])
        bones = record['interaction_capabilities']['bones']
        check('register: hands/root from M3 semantics >= 0.9, head below gate stays UNKNOWN with warning',
              bones['hand_left']['bone'] == 'hand.L' and bones['hand_left']['source'] == 'M3_SEMANTICS'
              and bones['hand_left']['confidence'] >= 0.9 and bones['root']['bone'] == 'root' and bones['head'] == 'UNKNOWN'
              and any(w['code'] == 'ACTOR_SEMANTICS_LOW_CONFIDENCE' and w['role'] == 'head' for w in hero['warnings']),
              {'bones': bones, 'warnings': hero['warnings']})
        check('register: reach = rest chain hand+forearm+upper arm (0.1+0.25+0.25 m)',
              abs(record['interaction_capabilities']['reach_m'] - 0.6) < 1e-6, record['interaction_capabilities']['reach'])
        check('register: rest AABB = body box and bone extents [1.6, 0.45, 1.8] m',
              all(abs(a - b) < 1e-5 for a, b in zip(record['dimensions_m'], (1.6, 0.45, 1.8))), record['bounds_m'])
        motion = record['body_actions'][0]['root_motion']
        check('register: Hero_Walk root motion from raw F-Curves = +3.0 m along +Y',
              record['body_actions'][0]['action'] == 'Hero_Walk' and motion['status'] == 'MEASURED'
              and abs(motion['horizontal_displacement_m'][0]) < 1e-6 and abs(motion['horizontal_displacement_m'][1] - 3.0) < 1e-6
              and motion['dominant_axis'] == '+Y' and abs(motion['duration_s'] - 59 / 30) < 1e-9, motion)
        # Cross-check the raw-curve formula against evaluated world positions (the probe itself may change frames).
        keep = bpy.context.scene.frame_current
        heads = []
        for frame in (1, 60):
            bpy.context.scene.frame_set(frame)
            evaluated = bpy.data.objects['HeroRig'].evaluated_get(bpy.context.evaluated_depsgraph_get())
            heads.append(evaluated.matrix_world @ evaluated.pose.bones['root'].head)
        bpy.context.scene.frame_set(keep)
        evaluated_delta = [heads[1].x - heads[0].x, heads[1].y - heads[0].y]
        check('register: raw-curve root motion equals evaluated world displacement (<= 1e-5 m)',
              max(abs(a - b) for a, b in zip(evaluated_delta, motion['horizontal_displacement_m'])) <= 1e-5,
              {'evaluated': evaluated_delta, 'raw': motion['horizontal_displacement_m']})
        check('register: M8 reference stored with state and object digest',
              record['secondary'][0]['state'] == 'ACCEPTED' and record['secondary'][0]['objects'] == ['M8_probe_v1_Baked']
              and record['secondary'][0]['objects_digest'] and not record['secondary'][0]['objects_missing'], record['secondary'])
        again = tool('register_hero_again', 'cinematic_register_actor', object_name='HeroRig',
                     options={'role': 'PROTAGONIST', 'secondary': [m8_ref]})
        check('register: idempotent (same object, data and fingerprint -> same actor_id, no new record)',
              again['actor_id'] == hero['actor_id'] and again['created'] is False
              and len(registry.records(storage, hero['asset_key'])) == 1, again['actor_id'])
        creature = tool('register_creature', 'cinematic_register_actor', object_name='CreatureRig',
                        options={'role': 'CREATURE', 'facial': [m7_ref], 'bones': {'root': 'root', 'head': 'head'}})
        crecord = registry.find(storage, creature['asset_key'], creature['actor_id'])
        cmotion = crecord['body_actions'][0]['root_motion']
        check('register: creature rotated 90 deg -> root motion 2.0 m along -X', abs(cmotion['horizontal_displacement_m'][0] + 2.0) < 1e-6
              and abs(cmotion['horizontal_displacement_m'][1]) < 1e-6 and cmotion['dominant_axis'] == '-X', cmotion)
        check('register: M7 reference ACCEPTED, Action digest equals ledger fingerprint, M7_FACE track seen',
              crecord['facial'][0]['state'] == 'ACCEPTED' and crecord['facial'][0]['fingerprint_matches_ledger'] is True
              and crecord['facial'][0]['m7_tracks'] and crecord['meshes'] == ['CreatureFace']
              and crecord['integrity']['meshes']['CreatureFace']['shape_keys'] is not None, crecord['facial'])
        check('register: explicit bones used; no hands -> reach UNKNOWN',
              crecord['interaction_capabilities']['bones']['head']['source'] == 'EXPLICIT'
              and crecord['interaction_capabilities']['reach_m'] == 'UNKNOWN', crecord['interaction_capabilities']['bones'])
        expect('M7 reference of another actor', 'ACTOR_FACIAL_OBJECT_MISMATCH',
               lambda: tool('register_bad_face', 'cinematic_register_actor', object_name='HeroRig',
                            options={'role': 'PROTAGONIST', 'facial': [m7_ref]}))
        expect('missing explicit bone', 'ACTOR_BONE_MISSING',
               lambda: tool('register_bad_bone', 'cinematic_register_actor', object_name='HeroRig',
                            options={'role': 'PROTAGONIST', 'bones': {'hand_left': 'nope'}}))
        expect('camera is not an actor type', 'ACTOR_TYPE_UNSUPPORTED',
               lambda: tool('register_camera', 'cinematic_register_actor', object_name='SrcCam', options={'role': 'EXTRA'}))
        expect('missing object', 'ACTOR_OBJECT_MISSING',
               lambda: tool('register_missing', 'cinematic_register_actor', object_name='Nobody', options={'role': 'EXTRA'}))
        expect('unknown option', 'CINEMATIC_OPTION_UNSUPPORTED',
               lambda: tool('register_option', 'cinematic_register_actor', object_name='HeroRig',
                            options={'role': 'EXTRA', 'speed': 2}))
        # Fingerprint change: one rest vertex of the body moves -> new record and warning; restoring returns the old id.
        vertex = bpy.data.objects['HeroBody'].data.vertices[0]
        original = tuple(vertex.co)
        vertex.co.x += 0.01
        moved = tool('register_moved', 'cinematic_register_actor', object_name='HeroRig',
                     options={'role': 'PROTAGONIST', 'secondary': [m8_ref]})
        warning = next((w for w in moved['warnings'] if w['code'] == 'ACTOR_FINGERPRINT_CHANGED'), {})
        check('register: changed rest geometry -> new actor_id + ACTOR_FINGERPRINT_CHANGED naming the section',
              moved['created'] is True and moved['actor_id'] != hero['actor_id']
              and 'meshes:HeroBody:positions' in warning.get('changed', []) and 'rig' not in warning.get('changed', []), warning)
        vertex.co = original
        restored = tool('register_restored', 'cinematic_register_actor', object_name='HeroRig',
                        options={'role': 'PROTAGONIST', 'secondary': [m8_ref]})
        check('register: exact restore -> original actor_id again (identity by content, not name)',
              restored['actor_id'] == hero['actor_id'] and restored['created'] is False, restored['actor_id'])
        fresh = read_only('inspect_actor', 'cinematic_inspect_actor', actor=hero['actor_id'])
        check('inspect_actor: fresh right after registration', fresh['freshness'] == 'fresh' and fresh['code'] is None
              and fresh['moved'] is False, {k: fresh[k] for k in ('freshness', 'counts', 'moved')})

        # ---------------------------------------------------------------- C. integrity around the candidate
        ctx = Context(bridge, storage, 'op-plan')
        brief = {'schema': 'production.cinematic_brief.v1', 'duration_s': 2.0, 'fps': 30,
                 'actors': [{'actor_id': hero['actor_id'], 'role': 'PROTAGONIST'},
                            {'actor_id': creature['actor_id'], 'role': 'CREATURE'}]}
        plan_ref, _ = store.save(storage, 'op-plan', lifecycle.build_plan(ctx, Request(brief=brief), {}))
        planned = read_only('verify_planned', 'cinematic_verify_integrity', cinematic=plan_ref, options={'detail': 'full'})
        check('verify_integrity before the candidate: passed, basis plan, candidate not applicable',
              planned['passed'] is True and planned['state'] == 'PLANNED' and planned['source']['basis'] == 'plan'
              and planned['candidate']['status'] == 'NOT_APPLICABLE' and planned['checked_total'] >= 10,
              {k: planned[k] for k in ('passed', 'counts', 'source', 'candidate', 'cameras_lights')})
        created = tool('create_candidate', 'cinematic_create_candidate', cinematic=plan_ref, apply=True)
        check('candidate created from the PLANNED plan', created['state'] == 'CINEMATIC_CANDIDATE', created['scene'])
        after_create = read_only('verify_candidate', 'cinematic_verify_integrity', cinematic=plan_ref)
        check('verify_integrity after the candidate: passed, candidate measured, ledger basis',
              after_create['passed'] is True and after_create['candidate']['status'] == 'UNCHANGED'
              and after_create['source']['basis'] == 'ledger', {k: after_create[k] for k in ('passed', 'counts', 'candidate')})
        path, plan, cstate = store.load(storage, plan_ref)
        body = COMMON + r'''
def probe_nla(params):
    scene, src, cin = cm_candidate(params)
    rig = cm_object('HeroRig', ['ARMATURE'])
    touched = [cm_touch_animdata(rig, 'OBJECT')]
    action = rig.animation_data.action
    track = cm_new_track(rig, 'OBJECT', params['track'], params)
    touched.append(cm_touch_track(rig, 'OBJECT', params['track']))
    cm_ensure_pose(rig)
    rig.animation_data.action = None
    track.strips.new(params['track'], 11, action)
    return {'touched': touched, 'owned': {}, 'fingerprints': cm_fingerprints(params)}

result = probe_nla(params)
'''
        params = store.assembly_params(plan, cstate, {'track': model.owned_name(plan['generation_id'], 1, 'BODY_HeroRig')})
        new_state, _ = store.mutate(ctx, path, plan, cstate, 'probe_nla', body, params)
        store.record(path, new_state)
        after_nla = read_only('verify_after_nla', 'cinematic_verify_integrity', cinematic=plan_ref, options={'detail': 'full'})
        rows = {row['item']: row['status'] for row in after_nla['checked'] if row.get('object') == 'HeroRig'}
        check('verify_integrity after a recorded NLA mutation: passed, Hero_Walk and rig unchanged, source changed vs plan',
              after_nla['passed'] is True and rows.get('body_action:Hero_Walk') == 'UNCHANGED' and rows.get('rig') == 'UNCHANGED'
              and new_state['fingerprints']['source'] != plan['source']['fingerprint']
              and bpy.data.objects['HeroRig'].animation_data.action is None, {'rows': rows, 'counts': after_nla['counts']})
        nla_actor = read_only('inspect_actor_after_nla', 'cinematic_inspect_actor', actor=hero['actor_id'])
        check('inspect_actor after NLA: still fresh (NLA is not identity), active Action reported as None',
              nla_actor['freshness'] == 'fresh' and nla_actor['active_action'] is None, nla_actor['counts'])

        # ---------------------------------------------------------------- D. real source edits
        face = bpy.data.objects['CreatureFace'].data
        original = tuple(face.vertices[3].co)
        face.vertices[3].co.y += 0.02
        edited = read_only('verify_mesh_edit', 'cinematic_verify_integrity', cinematic=plan_ref)
        found = [(r['item'], r['code'], r.get('section')) for r in edited['changed']]
        check('verify_integrity detects a moved source vertex (positions) that the scene fingerprint misses',
              edited['passed'] is False and ('mesh:CreatureFace', 'MESH_CHANGED', 'positions') in found
              and edited['source']['status'] == 'UNCHANGED', found)
        stale = read_only('inspect_actor_stale', 'cinematic_inspect_actor', actor=creature['actor_id'])
        check('inspect_actor reports ACTOR_STALE with what changed', stale['freshness'] == 'stale' and stale['code'] == 'ACTOR_STALE'
              and any(r['section'] == 'positions' for r in stale['changed']), stale['changed'])
        face.vertices[3].co = original
        smile = face.shape_keys.key_blocks['Smile'].data[5]
        key_original = tuple(smile.co)
        smile.co.x += 0.01
        keyed = read_only('verify_shape_key_edit', 'cinematic_verify_integrity', cinematic=plan_ref)
        check('verify_integrity detects an edited shape-key coordinate (shape_keys section only)',
              [(r['item'], r.get('section')) for r in keyed['changed']] == [('mesh:CreatureFace', 'shape_keys')], keyed['changed'])
        smile.co = key_original
        lens = bpy.data.cameras['SrcCamData'].lens
        bpy.data.cameras['SrcCamData'].lens = 50.0
        lensed = read_only('verify_camera_edit', 'cinematic_verify_integrity', cinematic=plan_ref)
        # Since the plan stores the source cameras/lights baseline (integration), the edit is also named per object.
        codes = [r['code'] for r in lensed['changed']]
        check('verify_integrity detects a source camera edit through the source fingerprint and names the camera',
              codes == ['SOURCE_SCENE_CHANGED', 'SOURCE_CAMERA_LIGHT_CHANGED']
              and lensed['changed'][0]['expected'] != lensed['changed'][0]['observed']
              and lensed['changed'][1].get('objects') == ['SrcCam'], lensed['changed'])
        bpy.data.cameras['SrcCamData'].lens = lens
        clean = read_only('verify_restored', 'cinematic_verify_integrity', cinematic=plan_ref)
        check('verify_integrity passes again after exact restores', clean['passed'] is True, clean['counts'])
        rejected = tool('reject', 'cinematic_reject', cinematic=plan_ref, apply=True)
        final = read_only('verify_rejected', 'cinematic_verify_integrity', cinematic=plan_ref)
        check('after rejection: source restored and verify_integrity passes on the plan basis',
              rejected['source_restored'] is True and final['passed'] is True and final['source']['basis'] == 'plan'
              and final['candidate']['status'] == 'NOT_APPLICABLE', {k: final[k] for k in ('passed', 'counts', 'source')})
        REPORT['metrics'] = {'bridge_calls': bridge.calls, 'payload_bytes': bridge.bytes,
                             'suite_seconds': round(time.perf_counter() - started, 3)}
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    save_report()
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
