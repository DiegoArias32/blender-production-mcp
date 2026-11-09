"""M10 area D in real Blender (own background process, controlled inline fixture), integrated with every M10
handler: exclusions decided at candidate creation (source collections linked whole), staging, NLA clip sync with
offsets (exact mapping measured) and root reset in NLA gaps, refusals, accepted-M7 face with facial events,
accepted-M8 secondary signed BEFORE the candidate (real order), prop attach with hand-off, the three quality-report
providers (sync_report, facial_report, secondary_report: measured values and NOT_APPLICABLE) and a rejection that
gives back the source and every Action intact.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_performance_probe.py -- <out_dir>

The M7 and M8 "accepted" generations used here are SYNTHETIC_ACCEPTANCE_FOR_MECHANISM_TEST: minimal ledger states
written only into this probe's temporary storage so the integration mechanism can be exercised. They are not review
evidence and never claim visual quality.
"""
from __future__ import annotations

import array
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
from mathutils import Matrix, Vector  # noqa: E402

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402

SYNTHETIC = 'SYNTHETIC_ACCEPTANCE_FOR_MECHANISM_TEST'
FPS = 30


class LocalBridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = 0
        self.bytes = 0
        self.largest = 0

    def run(self, body, params=None):
        code = build_code(body, params)
        self.calls += 1
        size = len(code.encode('utf-8'))
        self.bytes += size
        self.largest = max(self.largest, size)
        ns = {}
        exec(code.rsplit('print(', 1)[0], ns)
        return ns['result']


REPORT = {'suite': 'performance', 'blender': bpy.app.version_string, 'acceptance_label': SYNTHETIC,
          'checks': [], 'errors': [], 'measured': {}, 'timings_s': {}}
OUT = None


def save():
    (OUT / 'm10_performance.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def check(name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    save()


def expect(name, code, function):
    try:
        function()
    except Exception as exc:
        text = str(exc)
        check(name + ' -> ' + code, text.startswith(code) or (code + ':') in text, text[:400])
        return None
    check(name + ' -> ' + code, False, 'unexpected success')


def timed(label, function):
    began = time.perf_counter()
    try:
        return function()
    finally:
        REPORT['timings_s'][label] = round(time.perf_counter() - began, 3)


# ------------------------------------------------------------------ fixture (inline, minimal, controlled)

def collection(name):
    item = bpy.data.collections.get(name)
    if item is None:
        item = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(item)
    return item


def rig(name, x, color, coll):
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    coll.objects.link(obj)
    obj.location = (x, 0.0, 0.0)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    for bone, head, tail, parent in (('root', (0, 0, 0), (0, 0, .3), None), ('spine', (0, 0, .9), (0, 0, 1.4), 'root'),
                                     ('neck', (0, 0, 1.4), (0, 0, 1.55), 'spine'), ('head', (0, 0, 1.55), (0, 0, 1.8), 'neck'),
                                     ('jaw', (0, -.05, 1.62), (0, -.13, 1.58), 'head'),
                                     ('hand_R', (.45, 0, 1.2), (.6, 0, 1.2), 'spine')):
        b = data.edit_bones.new(bone)
        b.head, b.tail = head, tail
        if parent:
            b.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    for pb in obj.pose.bones:
        pb.rotation_mode = 'XYZ'
    body = box(name + '_Body', (x, 0, 1.0), (.2, .12, .45), color, coll)
    body.parent = obj
    body.matrix_parent_inverse = obj.matrix_world.inverted()
    return obj


def box(name, center, half, color=(.5, .5, .5), coll=None):
    hx, hy, hz = half
    verts = [(sx * hx, sy * hy, sz * hz) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    (coll or bpy.context.scene.collection).objects.link(obj)
    obj.location = center
    obj.color = (*color, 1.0)
    return obj


def empty(name, coll):
    obj = bpy.data.objects.new(name, None)
    coll.objects.link(obj)
    return obj


def keyed_action(name, obj, rows, interpolation='LINEAR'):
    """rows: [(data_path, index, [(frame, value), ...])] written on an OBJECT slot of a new layered Action."""
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    slot = action.slots.new(id_type='OBJECT', name=obj.name)
    bag = action.layers.new('Layer').strips.new(type='KEYFRAME').channelbag(slot, ensure=True)
    for path, index, keys in rows:
        curve = bag.fcurves.new(path, index=index)
        for frame, value in keys:
            curve.keyframe_points.insert(frame, value).interpolation = interpolation
    return action


def build_fixture(out):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = FPS
    scene.frame_start, scene.frame_end = 1, 120
    # Collections as a production scene would have them: actors, the M8 secondary source and the props live in
    # different child collections, so excluding the source and the props at candidate creation mirrors only their
    # collections and the actors' collection is linked whole (their users_collection, signed by M8, never changes).
    cast, accessories, props, extras = (collection(n) for n in ('Cast', 'Accessories', 'Props', 'Extras'))
    a = rig('ActorA', 0.0, (.8, .3, .3), cast)
    b = rig('ActorB', 3.0, (.3, .4, .8), cast)
    walk = keyed_action('A_Walk', a, [('pose.bones["root"].location', 1, [(1, 0.0), (60, 3.0)]),
                                       ('pose.bones["spine"].rotation_euler', 0, [(1, 0.0), (30, .2), (60, 0.0)]),
                                       ('pose.bones["hand_R"].location', 2, [(1, 0.0), (30, .3), (60, 0.0)])])
    idle = keyed_action('A_Idle', a, [('pose.bones["root"].location', 1, [(1, 0.0), (30, 0.0)]),
                                       ('pose.bones["spine"].rotation_euler', 1, [(1, 0.0), (15, .15), (30, 0.0)])])
    wave = keyed_action('B_Wave', b, [('pose.bones["root"].location', 0, [(1, 0.0), (60, .6)]),
                                       ('pose.bones["spine"].rotation_euler', 1, [(1, 0.0), (30, .35), (60, 0.0)]),
                                       ('pose.bones["hand_R"].rotation_euler', 2, [(1, 0.0), (30, .8), (60, 0.0)])])
    a.animation_data_create().action = walk
    b.animation_data_create().action = wave
    # Face of A: a Key datablock with one jaw shape, parented to the head bone.
    face = box('A_Face', (0, -.08, 1.65), (.09, .08, .1), (.9, .7, .6), cast)
    face.parent, face.parent_type, face.parent_bone = a, 'BONE', 'head'
    bpy.context.view_layer.update()
    face.matrix_world = Matrix.Translation((0, -.08, 1.65))
    face.shape_key_add(name='Basis')
    jaw = face.shape_key_add(name='jawOpen')
    jaw.value = 0.0  # Blender 5.2.1 creates Python-added shape keys at 1.0 (measured in M7)
    for i in (0, 2, 4, 6):
        jaw.data[i].co.z -= .03
    key = face.data.shape_keys
    # Synthetic accepted M7 candidate: one Action, OBJECT slot (jaw bone) + KEY slot (jawOpen), like fw_write.
    m7 = bpy.data.actions.new('M7F_synth_v1')
    m7.use_fake_user = True
    s_obj = m7.slots.new(id_type='OBJECT', name=a.name)
    s_key = m7.slots.new(id_type='KEY', name=key.name)
    strip = m7.layers.new('M7_FACE').strips.new(type='KEYFRAME')
    for slot, path, keys in ((s_obj, 'pose.bones["jaw"].rotation_euler', [(1, 0.0), (16, .4), (31, 0.0)]),
                             (s_key, 'key_blocks["jawOpen"].value', [(1, 0.0), (16, 1.0), (31, 0.0)])):
        curve = strip.channelbag(slot, ensure=True).fcurves.new(path, index=0)
        for frame, value in keys:
            curve.keyframe_points.insert(frame, value).interpolation = 'LINEAR'
    m7['production_facial_owner'] = 'M7'
    m7['production_facial_lifecycle'] = 'ACCEPTED'
    for owner, slot in ((a, s_obj), (key, s_key)):
        track = owner.animation_data_create().nla_tracks.new()
        track.name = 'M7_FACE ' + m7.name
        st = track.strips.new('M7_FACE ' + m7.name, 1, m7)
        if st.action_slot != slot:
            st.action_slot = slot
        st.blend_type = 'REPLACE'
    # M8 secondary source of B: a pendant parented to B's spine bone.
    pendant = box('B_Pendant', (3.25, -.2, 1.05), (.04, .02, .12), (.9, .6, .1), accessories)
    pendant.parent, pendant.parent_type, pendant.parent_bone = b, 'BONE', 'spine'
    bpy.context.view_layer.update()
    pendant.matrix_world = Matrix.Translation((3.25, -.2, 1.05))
    # Negative actors.
    stage_empty = empty('StageEmpty', extras)
    parented = empty('ParentedActor', extras)
    parented.parent = stage_empty
    mover = empty('RootMover', extras)
    mover.animation_data_create().action = keyed_action('RootMover_Move', mover, [('location', 0, [(1, 0.0), (60, 2.0)])])
    delta = empty('DeltaMover', extras)
    delta.animation_data_create().action = keyed_action('DeltaMover_Move', delta, [('delta_location', 0, [(1, 0.0), (60, 1.0)])])
    tweak = empty('Tweaker', extras)
    tweak_action = keyed_action('Tweaker_Move', tweak, [('location', 2, [(1, 0.0), (20, 1.0)])])
    track = tweak.animation_data_create().nla_tracks.new()
    track.strips.new('tw', 1, tweak_action)
    tweak.animation_data.use_tweak_mode = True
    driven = empty('DrivenActor', extras)
    keyed_action('Driven_Move', driven, [('location', 0, [(1, 0.0), (30, 1.0)])])
    # Fixture-side driver (plain bpy in the probe, never in a payload): an AVERAGE driver with no expression.
    driven.driver_add('location', 0).driver.type = 'AVERAGE'
    two = bpy.data.actions.new('TwoSlots')
    two.use_fake_user = True
    layer_strip = two.layers.new('Layer').strips.new(type='KEYFRAME')
    for slot_name in ('Foo', 'Bar'):
        slot = two.slots.new(id_type='OBJECT', name=slot_name)
        curve = layer_strip.channelbag(slot, ensure=True).fcurves.new('location', index=2)
        curve.keyframe_points.insert(1, 0.0)
        curve.keyframe_points.insert(10, 1.0)
    box('Cup', (.9, .6, 1.0), (.04, .04, .06), (.9, .9, .9), props)
    box('Mug', (-.9, .6, 1.0), (.04, .04, .06), (.9, .9, .9), props)
    box('Ground', (0, 0, -.01), (6, 6, .01), (.3, .3, .3))  # master collection: linked one by one
    path = out / 'm10_performance_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


# ------------------------------------------------------------------ probe-side read-only helpers (payloads)

def probe_bodies():
    from blender_mcp.production.cinematic.runtime import COMMON
    digests = COMMON + r'''
def probe_digests(params):
    actions = {}
    for name in params['actions']:
        a = bpy.data.actions.get(name)
        actions[name] = cm_action_digest(a) if a else None
    objects = {}
    for name in params['objects']:
        o = bpy.data.objects.get(name)
        if o is None:
            objects[name] = None
            continue
        mods = [[m.name, m.type, float(m.frame_start) if m.type == 'MESH_CACHE' else None] for m in o.modifiers]
        objects[name] = m2_digest([o.name, [list(r) for r in o.matrix_basis], list(o.delta_location), list(o.delta_rotation_euler),
                                   o.parent.name if o.parent else None, o.parent_bone, [c.name for c in o.constraints], mods,
                                   o.hide_render, cm_animation_digest(o)])
    keys = {}
    for name in params.get('keys') or []:
        k = bpy.data.shape_keys.get(name)
        keys[name] = cm_animation_digest(k) if k else None
    return {'actions': actions, 'objects': objects, 'keys': keys}

result = probe_digests(params)
'''
    evaluate = COMMON + r'''
def probe_eval(params):
    scene = cm_scene(params['scene'])
    objs = [cm_object(n) for n in params['capture']]
    saved = cm_capture(scene, objs)
    out = []
    with bpy.context.temp_override(scene=scene):
        try:
            for row in params['reads']:
                scene.frame_set(row['frame'])
                dg = bpy.context.evaluated_depsgraph_get()
                if row['kind'] == 'key':
                    item = bpy.data.shape_keys[row['id']].evaluated_get(dg)
                else:
                    item = bpy.data.objects[row['id']].evaluated_get(dg)
                if row['path'] == 'world_translation':
                    out.append(list(item.matrix_world.to_translation()))
                    continue
                if row['path'] == 'bone_head_world':
                    out.append(list(item.matrix_world @ item.pose.bones[row['bone']].head))
                    continue
                if row['path'] == 'vertex_local':
                    mesh = item.to_mesh()
                    out.append(list(mesh.vertices[row['index']].co))
                    item.to_mesh_clear()
                    continue
                value = item.path_resolve(row['path'])
                out.append(float(value[row['index']]) if row.get('index') is not None else float(value))
        finally:
            cm_restore(scene, objs, saved)
    return {'values': out}

result = probe_eval(params)
'''
    return digests, evaluate


# ------------------------------------------------------------------ synthetic accepted M7 / M8 ledgers (probe storage only)

def synthetic_m7(bridge, storage, fps=FPS):
    from blender_mcp.production.animation.generation import store as gstore
    from blender_mcp.production.facial import service as facial_service
    key = bpy.data.objects['A_Face'].data.shape_keys.name
    set_options = {'armature': 'ActorA', 'meshes': ['A_Face']}
    data = {'object_name': 'A_Face', 'set_options': set_options, 'head_policy': 'BODY_OWNS',
            'brief': {'fps': fps, 'frame_start': 1, 'frame_end': 31, 'duration_s': 1.0},
            'profile': {'armature': 'ActorA', 'head_bone': 'head', 'neck_bone': 'neck'},
            'synthetic': SYNTHETIC, 'no_action_created': False}
    ref, saved = gstore.save(storage, 'probe-m7-' + str(fps), data, 'facial', gstore.FACIAL_STATES)
    status = facial_service.scene_status(bridge, 'A_Face', set_options, 'M7F_synth_v1')
    gstore.event(Path(ref).parent, {'state': 'ACCEPTED', 'refinement_cycles': 0, 'version': 1,
                                    'candidate_action': 'M7F_synth_v1', 'candidate_fingerprint': status['candidate_fingerprint'],
                                    'accepted_tracks': [['ActorA', 'M7_FACE M7F_synth_v1'], [key, 'M7_FACE M7F_synth_v1']],
                                    'acceptance_label': SYNTHETIC,
                                    'review': {'label': SYNTHETIC, 'decision': 'ACCEPT', 'reviewer': 'none (synthetic)',
                                               'note': 'ledger state written by the probe; no render was reviewed'}},
                 gstore.FACIAL_STATES)
    return ref, status


def pendant_frames(obj):
    """PC2 samples for frames 1..60: the pendant swings about its top edge (synthetic motion, local coordinates)."""
    base = [v.co.copy() for v in obj.data.vertices]
    top = max(v.z for v in base)
    frames = []
    for f in range(1, 61):
        angle = .35 * math.sin(2 * math.pi * (f - 1) / 30.0)
        turn = Matrix.Rotation(angle, 3, 'X')
        values = array.array('f')
        for co in base:
            p = turn @ (co - Vector((0, 0, top))) + Vector((0, 0, top))
            values.extend([p.x, p.y, p.z])
        frames.append({'frame': f, 'vertex_count': len(base), 'hex': values.tobytes().hex()})
    return frames


def build_m8(bridge, storage, label):
    """Plan, M8-owned candidate/baked copies and the PC2 cache of a synthetic M8 generation (Blender + host)."""
    from blender_mcp.production.animation.generation import store as gstore
    from blender_mcp.production.physics import cache as pcache
    from blender_mcp.production.physics import integrity
    from blender_mcp.production.physics import service as pservice
    params = {'source_object': 'B_Pendant', 'primary_objects': ['ActorB']}
    status = bridge.run(integrity.STATUS, params)
    sample = [1, 30, 60]
    primary = bridge.run(integrity.PRIMARY, dict(params, frames=sample))
    brief = {'schema': 'production.physics_brief.v1', 'subsystem': 'ACCESSORY', 'fps': FPS, 'frame_start': 1,
             'frame_end': 60, 'duration_s': 2.0, 'gravity': [0.0, 0.0, -9.81]}
    data = {'object_name': 'B_Pendant', 'params': params, 'brief': brief, 'source_status': status,
            'primary_snapshot': primary, 'sample_frames': sample, 'backend_fingerprint': pservice.backend_fingerprint(),
            'supported': True, 'synthetic': SYNTHETIC, 'label': label, 'no_action_created': True}
    ref, saved = gstore.save(storage, 'probe-m8-' + label, data, 'physics', pservice.STATES)
    path, plan, state = pservice.load(storage, ref)
    gid = plan['generation_id']
    # Minimal M8-owned candidate and baked copies carrying the ownership marks M8 integrity verifies.
    scene = bpy.context.scene
    coll = bpy.data.collections.new('M8_' + gid + '_v1')
    scene.collection.children.link(coll)
    marks = {'production_m8_owned': True, 'production_generation_id': gid, 'production_candidate_version': 1}
    for k, v in marks.items():
        coll[k] = v
    source = bpy.data.objects['B_Pendant']
    cand = source.copy()
    cand.data = source.data.copy()
    cand.name = 'M8_' + gid[:8] + '_v1'
    for k, v in marks.items():
        cand[k] = v
    cand['production_source_object'] = 'B_Pendant'
    cand['production_subsystem'] = 'DANGLING_ACCESSORY'
    cand.hide_render = True
    coll.objects.link(cand)
    baked = cand.copy()
    baked.data = cand.data.copy()
    baked.name = cand.name + '_Baked'
    baked['production_m8_bake_of'] = cand.name
    coll.objects.link(baked)
    bpy.context.view_layer.update()
    candidate = {'candidate_object': cand.name, 'collection': coll.name, 'generation_id': gid, 'version': 1}
    partial = {'candidate': candidate, 'colliders': {}}
    cp = pservice.candidate_params(plan, partial)
    raw = bridge.run(integrity.CANDIDATE_STATUS, dict(cp, candidate_object=cp['candidate_name']))['raw_fingerprint']
    partial.update(candidate_raw_fingerprint=raw, current_settings={'synthetic': SYNTHETIC})
    record = pcache.write_pc2(storage, 'probe-m8-cache-' + label, pendant_frames(source),
                              pservice.cache_dependencies(plan, partial), 1)
    mod = baked.modifiers.new('M8_PC2_PLAYBACK', 'MESH_CACHE')
    baked['production_m8_cache_modifier'] = mod.name
    mod.cache_format, mod.filepath, mod.factor, mod.deform_mode = 'PC2', record['path'], 1.0, 'OVERWRITE'
    mod.interpolation, mod.time_mode, mod.play_mode = 'LINEAR', 'FRAME', 'SCENE'
    mod.frame_start, mod.frame_scale, mod.forward_axis, mod.up_axis = 1.0, 1.0, 'POS_Y', 'POS_Z'
    bpy.context.view_layer.update()
    return {'ref': ref, 'path': path, 'plan': plan, 'partial': partial, 'cp': cp, 'cache': record,
            'candidate': cand.name, 'baked': baked.name, 'gid': gid}


def sign_m8(bridge, m8):
    """Signs the candidate/baked status as M8 would at acceptance and writes the synthetic ACCEPTED event (host only)."""
    from blender_mcp.production.animation.generation import store as gstore
    from blender_mcp.production.physics import integrity
    from blender_mcp.production.physics import service as pservice
    cp = m8['cp']
    cstatus = bridge.run(integrity.CANDIDATE_STATUS, dict(cp, candidate_object=cp['candidate_name']))
    bstatus = bridge.run(integrity.CANDIDATE_STATUS, dict(cp, candidate_object=m8['baked'], candidate_rig=None))
    if cstatus['raw_fingerprint'] != m8['partial']['candidate_raw_fingerprint']:
        raise RuntimeError('synthetic M8 candidate raw fingerprint moved while building the fixture')
    state = dict(m8['partial'], state='ACCEPTED', refinement_cycles=0, version=1,
                 candidate_fingerprint=cstatus['fingerprint'],
                 baked_candidate={'candidate_object': m8['baked'], 'candidate_rig': None, 'generation_id': m8['gid'],
                                  'source_map': {m8['baked']: 'B_Pendant'}},
                 baked_fingerprint=bstatus['fingerprint'], cache=m8['cache'],
                 bake_deviation={'passed': True, 'label': SYNTHETIC, 'note': 'not measured: synthetic acceptance'},
                 technical_qa={'passed': True, 'label': SYNTHETIC}, acceptance_label=SYNTHETIC,
                 review={'label': SYNTHETIC, 'decision': 'ACCEPT', 'reviewer': 'none (synthetic)',
                         'note': 'ledger state written by the probe; no render was reviewed'})
    gstore.event(m8['path'].parent, state, pservice.STATES)
    return cstatus


def m8_fresh(bridge, storage, ref):
    from blender_mcp.production.physics import service as pservice
    _, plan, state = pservice.load(storage, ref)
    try:
        pservice.fresh(bridge, storage, plan, state)
        return 'FRESH'
    except Exception as exc:
        return str(exc)[:200]


# ------------------------------------------------------------------ main

def main():
    global OUT
    OUT = Path(sys.argv[sys.argv.index('--') + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        from blender_mcp.production.cinematic import performance, review, service, store
        from blender_mcp.production.cinematic.service import Context
        # Integration run: every M10 subsystem is loaded (no isolation from sibling modules any more).
        service.load_handlers()
        dispatch = service.dispatch
        DIGESTS, EVAL = probe_bodies()
        build_fixture(OUT)
        bridge = LocalBridge()
        storage = Storage(OUT / 'store')
        register_open_fixture(bridge, storage, OUT)
        m7_ref, m7_status = synthetic_m7(bridge, storage)
        check('synthetic M7 acceptance: accepted tracks present and fingerprint recorded',
              'M7_FACE M7F_synth_v1' in m7_status['facial_tracks'] and bool(m7_status['candidate_fingerprint']), m7_status['facial_tracks'])
        m7_ref_24, _ = synthetic_m7(bridge, storage, fps=24)
        # Real order: the M8 generation is accepted (signed) BEFORE any cinematic candidate exists.
        m8 = build_m8(bridge, storage, 'real-order')
        signed = sign_m8(bridge, m8)
        m8_ref, m8_cand, m8_baked = m8['ref'], m8['candidate'], m8['baked']
        check('synthetic M8 (signed before any candidate) passes physics.service.fresh',
              m8_fresh(bridge, storage, m8_ref) == 'FRESH')
        # Actors registered with the real handler: digests of rig, meshes and body Actions, explicit bones.
        actors = {}
        registrations = (('ActorA', 'PROTAGONIST', {'bones': {'root': 'root', 'head': 'head', 'neck': 'neck', 'hand_right': 'hand_R'},
                                                    'body_actions': ['A_Walk', 'A_Idle']}),
                         ('ActorB', 'SUPPORTING', {'bones': {'root': 'root', 'hand_right': 'hand_R'}, 'body_actions': ['B_Wave']}),
                         ('ParentedActor', 'EXTRA', {}), ('RootMover', 'EXTRA', {}), ('DeltaMover', 'EXTRA', {}),
                         ('Tweaker', 'EXTRA', {}), ('DrivenActor', 'EXTRA', {}))
        for name, role, options in registrations:
            row = dispatch('cinematic_register_actor', bridge, storage, 'op-reg-' + name, object_name=name,
                           options=dict(options, role=role))
            actors[name] = row['actor_id']
            REPORT['measured'].setdefault('registered', {})[name] = {
                'actor_id': row['actor_id'], 'meshes': (row.get('actor') or {}).get('meshes'),
                'bones': (row.get('actor') or {}).get('bones'), 'warnings': [w.get('code') for w in row.get('warnings') or []]}
        A, B = actors['ActorA'], actors['ActorB']
        action_names = ['A_Walk', 'A_Idle', 'B_Wave', 'M7F_synth_v1', 'RootMover_Move', 'DeltaMover_Move', 'Tweaker_Move',
                        'Driven_Move', 'TwoSlots']
        object_names = ['ActorA', 'ActorB', 'A_Face', 'B_Pendant', 'Cup', 'Mug', m8_cand, m8_baked, 'RootMover', 'DeltaMover']
        face_key = bpy.data.objects['A_Face'].data.shape_keys.name
        before = bridge.run(DIGESTS, {'actions': action_names, 'objects': object_names, 'keys': [face_key]})
        brief = {'schema': 'production.cinematic_brief.v1', 'duration_s': 4.0, 'fps': FPS,
                 'actors': [{'actor_id': v, 'role': 'CUSTOM'} for v in actors.values()]}

        def candidate_fp(ref):
            _, pl, st = store.load(storage, ref)
            return store.status(bridge, pl, st)['candidate']['fingerprint'], st.get('revision')

        def unchanged(ref, label, function, code):
            fp0, rev0 = candidate_fp(ref)
            expect(label, code, function)
            fp1, rev1 = candidate_fp(ref)
            check(label + ' leaves candidate and ledger unchanged', fp0 == fp1 and rev0 == rev1, {'revision': rev1})

        # ---------------- exclusions are decided at candidate creation (source collections linked whole)
        loose = dispatch('cinematic_create_candidate', bridge, storage, 'op-create-x', brief=brief, apply=True,
                         options={'exclude_objects': [m8_cand, m8_baked]})
        loose_ref = loose['cinematic']
        from blender_mcp.production.physics import integrity
        cp = m8['cp']
        after_status = bridge.run(integrity.CANDIDATE_STATUS, dict(cp, candidate_object=cp['candidate_name']))
        moved = {name: [signed['ownership'][name]['collections'], after_status['ownership'][name]['collections']]
                 for name in after_status['ownership'] if signed['ownership'].get(name) != after_status['ownership'][name]}
        diff = {'raw_fingerprint_same': after_status['raw_fingerprint'] == signed['raw_fingerprint'],
                'context_fingerprint_same': after_status['context_fingerprint'] == signed['context_fingerprint'],
                'ownership_changed': moved, 'm8_fresh': m8_fresh(bridge, storage, m8_ref),
                'linked_collections': loose['linked_collections'],
                'individually_linked_objects': loose['individually_linked_objects']}
        REPORT['measured']['m8_signature_after_m10_candidate'] = diff
        check('MEASURED: with source collections linked whole, M8 CANDIDATE_STATUS (ownership included) is unchanged '
              'and the M8 acceptance signed before the candidate stays FRESH', diff['raw_fingerprint_same']
              and diff['context_fingerprint_same'] and not moved and diff['m8_fresh'] == 'FRESH', diff)
        unchanged(loose_ref, 'secondary whose M8 source was not excluded at candidate creation', lambda: dispatch(
            'cinematic_integrate_secondary', bridge, storage, 'op-sx', cinematic=loose_ref, apply=True,
            options={'actor_id': B, 'physics': m8_ref, 'offset_frames': 10, 'window': [15, 65]}),
            'CINEMATIC_SECONDARY_SOURCE_NOT_EXCLUDED')
        unchanged(loose_ref, 'prop whose original was not excluded at candidate creation', lambda: dispatch(
            'cinematic_manage_props', bridge, storage, 'op-px', cinematic=loose_ref, apply=True,
            options={'props': [{'prop_id': 'Cup', 'object': 'Cup', 'attach': [
                {'actor_id': A, 'bone': 'hand_R', 'frame_start': 20, 'frame_end': 40}]}]}), 'CINEMATIC_PROP_NOT_EXCLUDED')
        rejected_x = dispatch('cinematic_reject', bridge, storage, 'op-rx', cinematic=loose_ref, apply=True)
        check('the exploratory candidate is rejected cleanly and M8 is still FRESH',
              rejected_x['source_restored'] is True and m8_fresh(bridge, storage, m8_ref) == 'FRESH',
              {k: rejected_x[k] for k in ('removed', 'undo_failed', 'source_restored')})

        excluded = sorted([m8_cand, m8_baked, 'B_Pendant', 'Cup'])
        created = dispatch('cinematic_create_candidate', bridge, storage, 'op-create', brief=brief, apply=True,
                           options={'exclude_objects': excluded})
        ref = created['cinematic']
        scene_name = created['scene']
        cand_scene = bpy.data.scenes[scene_name]
        check('candidate created: Cast linked whole, source and prop originals excluded, Mug kept',
              created['excluded_objects'] == excluded and 'Cast' in created['linked_collections']
              and cand_scene.objects.get('B_Pendant') is None and cand_scene.objects.get('Cup') is None
              and cand_scene.objects.get('Mug') is not None and cand_scene.objects.get('ActorB') is not None,
              {k: created[k] for k in ('scene', 'linked_objects', 'excluded_objects', 'linked_collections',
                                       'individually_linked_objects')})
        check('M8 acceptance signed before the candidate still FRESH after creating it (real order)',
              m8_fresh(bridge, storage, m8_ref) == 'FRESH')

        # ---------------- quality-report providers with nothing integrated: NOT_APPLICABLE, no Blender call
        path, plan, state = store.load(storage, ref)
        ctx = Context(bridge, storage, 'op-report')
        calls = bridge.calls
        empty_reports = {k: f(ctx, plan, state) for k, f in (('sync', performance.sync_report),
                                                            ('facial', performance.facial_report),
                                                            ('secondary', performance.secondary_report))}
        check('sync/facial/secondary reports are NOT_APPLICABLE (passed None) before anything is integrated, '
              'without any Blender call', all(r['status'] == 'NOT_APPLICABLE' and r['passed'] is None
                                              for r in empty_reports.values()) and bridge.calls == calls,
              {k: r['reason'] for k, r in empty_reports.items()})

        def report(kind):
            p, pl, st = store.load(storage, ref)
            function = {'sync': performance.sync_report, 'facial': performance.facial_report,
                        'secondary': performance.secondary_report}[kind]
            began = time.perf_counter()
            result = function(Context(bridge, storage, 'op-report-' + kind), pl, st)
            REPORT['timings_s']['report_' + kind + '_' + str(len(REPORT['timings_s']))] = round(time.perf_counter() - began, 3)
            store.fresh(bridge, storage, pl, st)  # raises if the report left the candidate or the source changed
            return result

        # ---------------- secondary (actor B must be untouched by M10 for the M8 gate)
        unchanged(ref, 'secondary window outside the cached clip', lambda: dispatch(
            'cinematic_integrate_secondary', bridge, storage, 'op-s0', cinematic=ref, apply=True,
            options={'actor_id': B, 'physics': m8_ref, 'offset_frames': 10}), 'SECONDARY_WINDOW_OUTSIDE_CACHE')
        proposal = dispatch('cinematic_integrate_secondary', bridge, storage, 'op-s1', cinematic=ref,
                            options={'actor_id': B, 'physics': m8_ref, 'offset_frames': 10, 'window': [15, 65]})
        check('secondary proposal runs the gate without changing Blender', proposal['status'] == 'proposal'
              and proposal['checks']['follows_actor'] is True, proposal['checks'])
        sec = timed('integrate_secondary', lambda: dispatch(
            'cinematic_integrate_secondary', bridge, storage, 'op-s2', cinematic=ref, apply=True,
            options={'actor_id': B, 'physics': m8_ref, 'offset_frames': 10, 'window': [15, 65]}))
        s = sec['secondary']
        REPORT['measured']['secondary'] = s
        copy = bpy.data.objects.get(s['copy'])
        cmod = [m for m in copy.modifiers if m.type == 'MESH_CACHE'][0] if copy else None
        check('secondary integrated with the M8 acceptance signed before the candidate (real order): own PC2 copy '
              'in _CIN with MESH_CACHE.frame_start = cache_start + delta, no touched rows',
              sec['status'] == 'success' and cmod is not None and abs(cmod.frame_start - 11.0) < 1e-9
              and bpy.data.collections[scene_name + '_CIN'].objects.get(s['copy']) is not None and sec['touched_rows'] == 0,
              {'status': sec['status'], 'errors': sec['errors'], 'cache_frame_start': s['cache_frame_start']})
        check('the copy of <cand>_Baked renders (hide_render False) while the M8 baked object stays hidden',
              copy is not None and copy.hide_render is False and bpy.data.objects[m8_baked].hide_render is True
              and s['copy_hide_render'] is False, {'copy': copy.hide_render if copy else None,
                                                  'baked': bpy.data.objects[m8_baked].hide_render})
        _, _, st_sec = store.load(storage, ref)
        row_sec = st_sec['actors'][B]['secondary'][0]
        check('state row for motion QA: object = M10 copy, window = scene frames actually cached [11, 70], '
              'requested window kept apart', row_sec['object'] == s['copy'] and row_sec['window'] == [11, 70]
              and row_sec['requested_window'] == [15, 65],
              {k: row_sec[k] for k in ('object', 'window', 'requested_window', 'delta')})
        check('secondary local equivalence measured on every integer frame (copy f+10 vs M8 baked f) <= 1e-5 m',
              s['local_equivalence']['frames'] == 60 and s['local_equivalence']['max_error_m'] <= 1e-5, s['local_equivalence'])
        check('secondary world equivalence deferred until the primary is offset (copy follows the actor)',
              s['world_equivalence'] == {'status': 'PENDING_PRIMARY_OFFSET'} and s['follows_actor'] is True)
        check('M8 objects untouched and the excluded secondary source never reaches the candidate',
              s['m8_baked_unchanged'] and cand_scene.objects.get('B_Pendant') is None
              and bpy.context.scene.objects.get('B_Pendant') is not None)
        REPORT['measured']['secondary_outside_cache_first_frame_delta'] = s['outside_cache_first_frame_delta']
        samples = pendant_frames(bpy.data.objects['B_Pendant'])
        reads = bridge.run(EVAL, {'scene': scene_name, 'capture': ['ActorA', 'ActorB'], 'reads': [
            {'frame': f + 10, 'kind': 'object', 'id': s['copy'], 'path': 'vertex_local', 'index': 6} for f in (1, 8, 23, 60)]})['values']
        pc2 = []
        for f in (1, 8, 23, 60):
            values = array.array('f')
            values.frombytes(bytes.fromhex(samples[f - 1]['hex']))
            pc2.append([values[18], values[19], values[20]])
        gap = max(abs(a - b) for r, q in zip(reads, pc2) for a, b in zip(r, q))
        REPORT['measured']['secondary_copy_vs_pc2_samples'] = {'copy': reads, 'pc2': pc2, 'max_abs_m': gap}
        check('independent read: the copy at f+10 plays the PC2 sample f (vertex 6, 4 frames) and the clip moves',
              gap <= 1e-6 and s['local_equivalence']['copy_motion_range_m'] > 1e-3,
              {'max_abs_m': gap, 'copy_motion_range_m': s['local_equivalence']['copy_motion_range_m']})
        sr0 = report('secondary')
        REPORT['measured']['secondary_report_before_sync'] = sr0
        codes0 = sr0['findings_by_code']
        check('secondary_report before B is synchronized re-measures: local equivalence <= 1e-5 m but the world '
              'equivalence fails because the primary is not offset -> FAIL, state restored',
              sr0['status'] == 'FAIL' and sr0['state_restored'] is True and sr0['metrics']['max_local_error_m'] <= 1e-5
              and sr0['metrics']['max_world_error_m'] > 1e-5 and 'SECONDARY_WORLD_EQUIVALENCE_FAILED' in codes0
              and 'SECONDARY_PRIMARY_NOT_SYNCHRONIZED' in codes0, {'metrics': sr0['metrics'], 'codes': codes0})

        # ---------------- staging
        unchanged(ref, 'stage a parented actor', lambda: dispatch('cinematic_stage_actors', bridge, storage, 'op-g0', cinematic=ref,
                  apply=True, options={'actors': [{'actor_id': actors['ParentedActor'], 'location': [1, 0, 0]}]}),
                  'ACTOR_STAGING_UNSUPPORTED')
        unchanged(ref, 'rotate an actor whose Action animates object location', lambda: dispatch(
            'cinematic_stage_actors', bridge, storage, 'op-g1', cinematic=ref, apply=True,
            options={'actors': [{'actor_id': actors['RootMover'], 'yaw_deg': 30}]}), 'ROOT_MOTION_AMBIGUOUS')
        unchanged(ref, 'stage an actor whose Action animates delta_location', lambda: dispatch(
            'cinematic_stage_actors', bridge, storage, 'op-g2', cinematic=ref, apply=True,
            options={'actors': [{'actor_id': actors['DeltaMover'], 'location': [0, 1, 0]}]}), 'ACTOR_DELTA_ANIMATED')
        fp0, rev0 = candidate_fp(ref)
        proposals = {
            'stage': dispatch('cinematic_stage_actors', bridge, storage, 'op-q1', cinematic=ref,
                              options={'actors': [{'actor_id': A, 'yaw_deg': 90.0}]}),
            'sync': dispatch('cinematic_sync_actions', bridge, storage, 'op-q2', cinematic=ref,
                             options={'clips': [{'actor_id': A, 'action': 'A_Walk', 'start_frame': 11}]}),
            'face': dispatch('cinematic_integrate_facial', bridge, storage, 'op-q3', cinematic=ref,
                             options={'actor_id': A, 'facial': m7_ref, 'start_frame': 41}),
            'props': dispatch('cinematic_manage_props', bridge, storage, 'op-q4', cinematic=ref,
                              options={'props': [{'prop_id': 'Cup', 'object': 'Cup', 'attach': [
                                  {'actor_id': A, 'bone': 'hand_R', 'frame_start': 20, 'frame_end': 40}]}]})}
        fp1, rev1 = candidate_fp(ref)
        check('apply=false proposals run every validation and change neither Blender nor the ledger',
              all(v['status'] == 'proposal' and v['checks'].get('dry_run') for v in proposals.values())
              and fp0 == fp1 and rev0 == rev1 and not any(o.name.endswith('_PROP_Cup') for o in bpy.data.objects),
              {k: v['checks'] for k, v in proposals.items()})
        hand_before = bridge.run(EVAL, {'scene': scene_name, 'capture': ['ActorA', 'ActorB'], 'reads': [
            {'frame': 1, 'kind': 'object', 'id': 'ActorA', 'path': 'bone_head_world', 'bone': 'hand_R'}]})['values'][0]
        staged = timed('stage_actors', lambda: dispatch(
            'cinematic_stage_actors', bridge, storage, 'op-g3', cinematic=ref, apply=True,
            options={'actors': [{'actor_id': A, 'location': [0.0, -1.0, 0.0], 'yaw_deg': 90.0},
                                {'actor_id': B, 'location': [0.5, 0.0, 0.0], 'yaw_deg': 0.0}]}))
        st = staged['staging']
        REPORT['measured']['staging'] = st
        worst = max(max(r['max_position_error_m'] for r in st['actors']), 0.0)
        worst_r = max(r['max_rotation_error_deg'] for r in st['actors'])
        check('staging verified on evaluated world matrices (T(loc+dloc) @ R_delta @ R_anim) within 1e-5 m / 1e-3 deg',
              staged['status'] == 'success' and worst <= 1e-5 and worst_r <= 1e-3,
              {'max_position_error_m': worst, 'max_rotation_error_deg': worst_r})
        hand_after = bridge.run(EVAL, {'scene': scene_name, 'capture': ['ActorA', 'ActorB'], 'reads': [
            {'frame': 1, 'kind': 'object', 'id': 'ActorA', 'path': 'bone_head_world', 'bone': 'hand_R'}]})['values'][0]
        turned = Matrix.Rotation(math.radians(90.0), 3, 'Z') @ Vector(hand_before) + Vector((0.0, -1.0, 0.0))
        REPORT['measured']['staging_independent_hand_R'] = {'before': hand_before, 'after': hand_after, 'expected': list(turned)}
        check('independent read: hand_R head moves exactly by Rz(90) then (0, -1, 0) (actor origin at the world origin)',
              (Vector(hand_after) - turned).length <= 1e-5, REPORT['measured']['staging_independent_hand_R'])
        pair = [p for p in st['separation']['pairs'] if set(p['actors']) == {A, B}]
        sep_objects = st['separation']['objects']
        check('staging reports pairwise separation and dimensions from objects the candidate evaluates: the excluded '
              'M8 source of B is replaced by its M10 copy', bool(pair) and st['separation']['dimensions_m'][A] is not None
              and 'B_Pendant' not in sep_objects[B] and s['copy'] in sep_objects[B],
              {'pair': pair, 'dimensions': {k: st['separation']['dimensions_m'][k] for k in (A, B)},
               'objects': {k: sep_objects[k] for k in (A, B)}})
        unchanged(ref, 'integrate secondary after staging the actor', lambda: dispatch(
            'cinematic_integrate_secondary', bridge, storage, 'op-s3', cinematic=ref, apply=True,
            options={'actor_id': B, 'physics': m8_ref, 'offset_frames': 10, 'window': [15, 65]}), 'SECONDARY_GATE_ORDER')

        # ---------------- clip synchronization
        unchanged(ref, 'sync an actor in NLA tweak mode', lambda: dispatch(
            'cinematic_sync_actions', bridge, storage, 'op-c0', cinematic=ref, apply=True,
            options={'clips': [{'actor_id': actors['Tweaker'], 'action': 'Tweaker_Move', 'start_frame': 1}]}), 'NLA_TWEAK_MODE')
        unchanged(ref, 'a clip whose channel is driven on the actor', lambda: dispatch(
            'cinematic_sync_actions', bridge, storage, 'op-c6', cinematic=ref, apply=True,
            options={'clips': [{'actor_id': actors['DrivenActor'], 'action': 'Driven_Move', 'start_frame': 1}]}),
            'NLA_DRIVER_CONFLICT')
        unchanged(ref, 'an Action with two object slots and none for the actor', lambda: dispatch(
            'cinematic_sync_actions', bridge, storage, 'op-c7', cinematic=ref, apply=True,
            options={'clips': [{'actor_id': A, 'action': 'TwoSlots', 'start_frame': 1}]}), 'SLOT_AMBIGUOUS')
        unchanged(ref, 'two clips of one actor overlapping in time', lambda: dispatch(
            'cinematic_sync_actions', bridge, storage, 'op-c1', cinematic=ref, apply=True,
            options={'clips': [{'actor_id': A, 'action': 'A_Walk', 'start_frame': 11},
                               {'actor_id': A, 'action': 'A_Idle', 'start_frame': 50}]}), 'CLIP_OVERLAP')
        unchanged(ref, 'a scaled clip without explicit retime', lambda: dispatch(
            'cinematic_sync_actions', bridge, storage, 'op-c2', cinematic=ref, apply=True,
            options={'clips': [{'actor_id': A, 'action': 'A_Walk', 'start_frame': 11, 'scale': 2}]}), 'CLIP_RETIME_REQUIRED')
        unchanged(ref, 'secondary actor synchronized at another offset', lambda: dispatch(
            'cinematic_sync_actions', bridge, storage, 'op-c3', cinematic=ref, apply=True,
            options={'clips': [{'actor_id': B, 'action': 'B_Wave', 'start_frame': 21}]}), 'SECONDARY_OFFSET_MISMATCH')
        synced = timed('sync_actions', lambda: dispatch(
            'cinematic_sync_actions', bridge, storage, 'op-c4', cinematic=ref, apply=True,
            options={'clips': [{'actor_id': A, 'action': 'A_Walk', 'start_frame': 11},
                               {'actor_id': A, 'action': 'A_Idle', 'start_frame': 75, 'scale': 1.5, 'retime': True,
                                'extrapolation': 'HOLD_FORWARD'},
                               {'actor_id': B, 'action': 'B_Wave', 'start_frame': 11}],
                     'root_bones': {A: 'root'}}))
        sy = synced['sync']
        REPORT['measured']['sync'] = sy
        REPORT['measured']['sync_warnings'] = synced['warnings']
        clips = [c for a in sy['actors'] for c in a['clips']]
        check('sync applied: every clip maps scene frame -> action frame exactly (max channel error <= 1e-5)',
              synced['status'] == 'success' and all(c['mapping_passed'] for c in clips) and len(clips) == 3,
              [{k: c[k] for k in ('action', 'frame_start', 'frame_end', 'offset_frames', 'max_channel_error', 'mapping_samples')}
               for c in clips])
        walk = [c for c in clips if c['action'] == 'A_Walk'][0]
        idle = [c for c in clips if c['action'] == 'A_Idle'][0]
        check('offset clip lands at [11, 70] with offset 10', walk['frame_start'] == 11 and walk['frame_end'] == 70
              and walk['offset_frames'] == 10, walk)
        check('retimed clip reports its new duration (29 action frames x 1.5 = 43.5)', abs(idle['duration_frames'] - 43.5) < 1e-6
              and idle['scale'] == 1.5, idle)
        actor_a = [a for a in sy['actors'] if a['object'] == 'ActorA'][0]
        actor_b = [a for a in sy['actors'] if a['object'] == 'ActorB'][0]
        check('gap between clips detected (70 -> 75)', any(g['frames'] == [70.0, 75.0] for g in actor_a['gaps']), actor_a['gaps'])
        check('root discontinuity at the clip boundary flagged for review',
              any(b['finding'] == 'ROOT_DISCONTINUITY_REVIEW' for b in actor_a['boundaries']), actor_a['boundaries'])
        _, _, st_sync = store.load(storage, ref)
        timeline = st_sync['timeline']
        resets = {(w['actor_id'], tuple(w['frames'])): w for w in synced['warnings'] if w['code'] == 'ROOT_RESET_IN_NLA_GAP'}
        expected_gaps = {(A, (1, 10)), (A, (71, 74)), (B, (1, 10)), (B, (71, int(timeline['frame_end'])))}
        check('ROOT_RESET_IN_NLA_GAP warnings (never errors) for every frame range no clip drives: before the first '
              'clip, between Walk and Idle (NOTHING) and after B_Wave; none after Idle (HOLD_FORWARD)',
              set(resets) == expected_gaps and all(w['severity'] == 'WARNING' for w in resets.values()),
              {str(k): {'jump_m': v['measured']['jump_m'], 'root': v['measured']['root']} for k, v in resets.items()})
        check('root bone source: A from options, B from the actor registry (never guessed)',
              st_sync['actors'][A]['root_source'] == 'OPTIONS' and st_sync['actors'][B]['root_source'] == 'REGISTRY'
              and st_sync['actors'][B]['root_bone'] == 'root' and actor_b['root_reset_gaps']['root_source'] == 'DECLARED',
              {k: (st_sync['actors'][k]['root_bone'], st_sync['actors'][k]['root_source']) for k in (A, B)})
        jumps = bridge.run(EVAL, {'scene': scene_name, 'capture': ['ActorA', 'ActorB', 'A_Face'], 'reads': [
            {'frame': 70, 'kind': 'object', 'id': 'ActorA', 'path': 'bone_head_world', 'bone': 'root'},
            {'frame': 71, 'kind': 'object', 'id': 'ActorA', 'path': 'bone_head_world', 'bone': 'root'}]})['values']
        independent = (Vector(jumps[1]) - Vector(jumps[0])).length
        tool_jump = resets.get((A, (71, 74)), {}).get('measured', {}).get('jump_m')
        REPORT['measured']['root_reset_gap_A_71'] = {'independent_jump_m': independent, 'tool_jump_m': tool_jump,
                                                     'root_world_70': jumps[0], 'root_world_71': jumps[1]}
        check('independent read of the root reset: the root head jumps between frame 70 (last driven) and 71 (gap) by '
              'the same distance the warning reports', tool_jump is not None and abs(independent - tool_jump) <= 1e-5
              and independent > 1.0, REPORT['measured']['root_reset_gap_A_71'])
        check('channel overlap with the accepted M7 track reported (none expected: disjoint channels)',
              actor_a['track_overlaps'] == [], actor_a['track_overlaps'])
        check('source Actions unchanged after sync (digest before/after in the payload)', sy['source_actions_unchanged'])
        world = sy['secondary'][0] if sy['secondary'] else {}
        REPORT['measured']['secondary_world_after_sync'] = world
        check('secondary world equivalence after the primary is offset and staged <= 1e-5 m',
              world.get('status') == 'MEASURED' and world.get('passed') is True, world)
        # Independent read (not the tool's own verification): scene 40 -> A_Walk frame 30 (root y = 30/59*3).
        values = bridge.run(EVAL, {'scene': scene_name, 'capture': ['ActorA', 'ActorB', 'A_Face'], 'reads': [
            {'frame': 40, 'kind': 'object', 'id': 'ActorA', 'path': 'pose.bones["root"].location', 'index': 1},
            {'frame': 70, 'kind': 'object', 'id': 'ActorA', 'path': 'pose.bones["root"].location', 'index': 1},
            {'frame': 72, 'kind': 'object', 'id': 'ActorA', 'path': 'pose.bones["root"].location', 'index': 1},
            {'frame': 80, 'kind': 'object', 'id': 'ActorB', 'path': 'pose.bones["root"].location', 'index': 0}]})['values']
        expected = [29 / 59 * 3, 3.0, 0.0, None]
        REPORT['measured']['independent_reads'] = {'values': values, 'expected': expected[:3]}
        check('independent evaluated read: frame 40 -> action 30, frame 70 -> action 60, gap frame 72 rests (NOTHING)',
              abs(values[0] - expected[0]) < 1e-5 and abs(values[1] - 3.0) < 1e-5 and abs(values[2]) < 1e-6, values)
        syr = report('sync')
        REPORT['measured']['sync_report'] = {k: syr[k] for k in ('status', 'metrics', 'findings_by_code', 'state_restored', 'restoration')}
        check('sync_report re-measures the strips now: PASS with the gap warnings, every mapping <= 1e-5, source '
              'Actions unchanged, state restored and the plan still fresh',
              syr['status'] == 'PASS' and syr['state_restored'] is True and syr['metrics']['clips'] == 3
              and syr['metrics']['max_channel_error'] <= 1e-5 and syr['metrics']['source_actions_changed'] == 0
              and syr['metrics']['root_reset_gaps'] == 4 and syr['findings_by_code'].get('ROOT_RESET_IN_NLA_GAP') == 4,
              REPORT['measured']['sync_report'])
        sr1 = report('secondary')
        REPORT['measured']['secondary_report_after_sync'] = {k: sr1[k] for k in ('status', 'metrics', 'findings_by_code', 'state_restored')}
        check('secondary_report after the primary is synchronized at the same offset: PASS, local and world '
              'equivalence <= 1e-5 m measured now, state restored', sr1['status'] == 'PASS' and sr1['state_restored'] is True
              and sr1['metrics']['max_local_error_m'] <= 1e-5 and sr1['metrics']['max_world_error_m'] <= 1e-5
              and sr1['metrics']['min_copy_motion_range_m'] > 1e-3, REPORT['measured']['secondary_report_after_sync'])

        # ---------------- face (synthetic accepted M7)
        unchanged(ref, 'M7 performance at another fps', lambda: dispatch(
            'cinematic_integrate_facial', bridge, storage, 'op-f0', cinematic=ref, apply=True,
            options={'actor_id': A, 'facial': m7_ref_24, 'start_frame': 41}), 'FACIAL_FPS_MISMATCH')
        unchanged(ref, 'a facial event outside the facial window', lambda: dispatch(
            'cinematic_integrate_facial', bridge, storage, 'op-f9', cinematic=ref, apply=True,
            options={'actor_id': A, 'facial': m7_ref, 'start_frame': 41, 'events': [{'frame': 90, 'label': 'late'}]}),
            'FACIAL_EVENT_OUTSIDE_WINDOW')
        reuse = dispatch('cinematic_integrate_facial', bridge, storage, 'op-f1', cinematic=ref, apply=True,
                         options={'actor_id': A, 'facial': m7_ref, 'start_frame': 1, 'extrapolation': 'HOLD'})
        check('offset 0 with the M7 extrapolation reuses the accepted M7 track (no own track, no mute); without events '
              'it warns FACIAL_EVENTS_NOT_DECLARED', reuse['status'] == 'success' and reuse['facial']['reuse_m7_track']
              and reuse['touched_rows'] == 0 and all(t['passed'] for t in reuse['facial']['tracks'])
              and [w['code'] for w in reuse['warnings']] == ['FACIAL_EVENTS_NOT_DECLARED'],
              {'facial': reuse['facial'], 'warnings': reuse['warnings']})
        fr0 = report('facial')
        REPORT['measured']['facial_report_reused'] = {k: fr0[k] for k in ('status', 'metrics', 'findings_by_code', 'state_restored')}
        check('facial_report on the reused M7 track: PASS (warning only for the missing events), M7 track unmuted as '
              'recorded, mapping <= 1e-5', fr0['status'] == 'PASS' and fr0['state_restored'] is True
              and fr0['metrics']['reused_m7_faces'] == 1 and fr0['metrics']['max_channel_error'] <= 1e-5
              and fr0['findings_by_code'] == {'FACIAL_EVENTS_NOT_DECLARED': 1}, REPORT['measured']['facial_report_reused'])
        face = timed('integrate_facial', lambda: dispatch(
            'cinematic_integrate_facial', bridge, storage, 'op-f2', cinematic=ref, apply=True,
            options={'actor_id': A, 'facial': m7_ref, 'start_frame': 41, 'events': [{'frame': 56, 'label': 'jaw peak'}]}))
        f = face['facial']
        REPORT['measured']['facial'] = f
        a_obj = bpy.data.objects['ActorA']
        m7_tracks = [a_obj.animation_data.nla_tracks.get('M7_FACE M7F_synth_v1'),
                     bpy.data.shape_keys[face_key].animation_data.nla_tracks.get('M7_FACE M7F_synth_v1')]
        check('offset 40: own FACE tracks on armature and Key, M7 tracks muted, mapping exact',
              face['status'] == 'success' and f['offset_frames'] == 40 and not f['reuse_m7_track']
              and all(t.mute for t in m7_tracks) and len(f['tracks']) == 2 and all(t['passed'] for t in f['tracks'])
              and all(not t['track'].startswith('M7_FACE') for t in f['tracks']), f['tracks'])
        check('facial Action never written (digest before/after)', f['facial_action']['unchanged'])
        _, _, st_face = store.load(storage, ref)
        check('facial events stored for visibility QA: state.facial_events = [{actor_id, frame, label}], window [41, 71]',
              st_face['facial_events'] == [{'actor_id': A, 'frame': 56, 'label': 'jaw peak'}] and face['warnings'] == []
              and f['window'] == [41.0, 71.0], {'facial_events': st_face['facial_events'], 'window': f['window']})
        keyvals = bridge.run(EVAL, {'scene': scene_name, 'capture': ['ActorA', 'A_Face'], 'reads': [
            {'frame': 56, 'kind': 'key', 'id': face_key, 'path': 'key_blocks["jawOpen"].value'},
            {'frame': 16, 'kind': 'key', 'id': face_key, 'path': 'key_blocks["jawOpen"].value'},
            {'frame': 56, 'kind': 'object', 'id': 'ActorA', 'path': 'pose.bones["jaw"].rotation_euler', 'index': 0}]})['values']
        REPORT['measured']['facial_independent_reads'] = keyvals
        check('independent evaluated read: jawOpen peaks at scene 56 (= M7 frame 16) and the muted M7 peak at 16 is gone',
              abs(keyvals[0] - 1.0) < 1e-5 and abs(keyvals[1]) < 1e-6 and abs(keyvals[2] - .4) < 1e-5, keyvals)
        fr1 = report('facial')
        REPORT['measured']['facial_report_own'] = {k: fr1[k] for k in ('status', 'metrics', 'findings_by_code', 'state_restored')}
        check('facial_report on the own tracks: PASS, M7 tracks muted as recorded, Action digest unchanged, mapping '
              '<= 1e-5, BODY_OWNS respected by the synchronized body, state restored',
              fr1['status'] == 'PASS' and fr1['state_restored'] is True and fr1['metrics']['own_track_faces'] == 1
              and fr1['metrics']['tracks'] == 2 and fr1['metrics']['max_channel_error'] <= 1e-5
              and fr1['metrics']['events'] == 1 and not fr1['findings'], REPORT['measured']['facial_report_own'])

        _, _, st_before = store.load(storage, ref)
        resync = dispatch('cinematic_sync_actions', bridge, storage, 'op-c5', cinematic=ref, apply=True,
                          options={'clips': [{'actor_id': A, 'action': 'A_Walk', 'start_frame': 11}]})
        _, _, st_after = store.load(storage, ref)
        again = bridge.run(EVAL, {'scene': scene_name, 'capture': ['ActorA', 'A_Face'], 'reads': [
            {'frame': 90, 'kind': 'object', 'id': 'ActorA', 'path': 'pose.bones["spine"].rotation_euler', 'index': 1},
            {'frame': 56, 'kind': 'key', 'id': face_key, 'path': 'key_blocks["jawOpen"].value'}]})['values']
        check('re-sync replaces the own BODY track (no new touched rows); A_Idle gone, face still plays',
              resync['status'] == 'success' and resync['touched_rows'] == 0
              and len(st_after['assembly']['touched']) == len(st_before['assembly']['touched'])
              and abs(again[0]) < 1e-6 and abs(again[1] - 1.0) < 1e-5, {'values': again})

        # ---------------- props
        unchanged(ref, 'a prop that is an actor mesh', lambda: dispatch(
            'cinematic_manage_props', bridge, storage, 'op-p0', cinematic=ref, apply=True,
            options={'props': [{'prop_id': 'Body', 'object': 'ActorA_Body'}]}), 'PROP_IS_ACTOR')
        unchanged(ref, 'a prop whose original is still in the candidate (Mug, not excluded)', lambda: dispatch(
            'cinematic_manage_props', bridge, storage, 'op-p9', cinematic=ref, apply=True,
            options={'props': [{'prop_id': 'Mug', 'object': 'Mug'}]}), 'CINEMATIC_PROP_NOT_EXCLUDED')
        unchanged(ref, 'overlapping attach windows', lambda: dispatch(
            'cinematic_manage_props', bridge, storage, 'op-p1', cinematic=ref, apply=True,
            options={'props': [{'prop_id': 'Cup', 'object': 'Cup', 'attach': [
                {'actor_id': A, 'bone': 'hand_R', 'frame_start': 20, 'frame_end': 40},
                {'actor_id': B, 'bone': 'hand_R', 'frame_start': 40, 'frame_end': 60}]}]}), 'PROP_ATTACH_OVERLAP')
        props = timed('manage_props', lambda: dispatch(
            'cinematic_manage_props', bridge, storage, 'op-p2', cinematic=ref, apply=True,
            options={'props': [{'prop_id': 'Cup', 'object': 'Cup', 'attach': [
                {'actor_id': A, 'bone': 'hand_R', 'frame_start': 20, 'frame_end': 40},
                {'actor_id': B, 'bone': 'hand_R', 'frame_start': 41, 'frame_end': 60}]}]}))
        pr = props['props']['props'][0]
        REPORT['measured']['props'] = pr
        checks = pr['attach_checks']
        check('prop excluded at creation -> copy in _CIN shares the mesh, no touched rows, original never in the '
              'candidate, never re-parented, unchanged', props['status'] == 'success' and props['touched_rows'] == 0
              and pr['shares_data_with_original'] and pr['original_unchanged'] and pr['original_parent'] is None
              and cand_scene.objects.get('Cup') is None and cand_scene.objects.get(pr['copy']) is not None,
              {k: pr[k] for k in ('copy', 'action', 'original_unchanged', 'original_parent')})
        check('attach: hand-prop slide and pops measured on every frame of each window <= 1e-5 m',
              len(checks) == 2 and all(c['passed'] for c in checks),
              [{k: c.get(k) for k in ('window', 'hand_prop_slide_m', 'hand_prop_rotation_drift_deg', 'pop_in_m', 'pop_out_m',
                                      'handoff_step_m', 'world_at_start')} for c in checks])
        check('hand-off A -> B at frame 41 continuous (two CHILD_OF constraints, keyed influence)',
              len(pr['handoffs']) == 1 and pr['handoffs'][0]['continuity_error_m'] <= 1e-5, pr['handoffs'])
        cup_world = list(bpy.data.objects['Cup'].matrix_world.to_translation())
        start_gap = (Vector(pr['attach_checks'][0]['world_at_start']) - Vector(cup_world)).length
        check('the copy starts where the excluded original is in the source scene (world_at_start of the first window '
              'follows the hand from the original placement)', start_gap <= 1e-5,
              {'original_world': cup_world, 'copy_world_at_20': pr['attach_checks'][0]['world_at_start'], 'distance_m': start_gap})

        # ---------------- the quality report finds the providers by name (review.run_section)
        path, plan, state = store.load(storage, ref)
        sections = {sid: review.run_section(Context(bridge, storage, 'op-qr-' + sid), path, plan, state,
                                            review.SECTION_BY_ID[sid]) for sid in ('sync', 'facial_timing', 'secondary')}
        store.fresh(bridge, storage, plan, state)
        REPORT['measured']['quality_report_sections'] = {k: {x: v.get(x) for x in ('status', 'provider', 'mandatory', 'metrics',
                                                                                     'findings_by_severity')}
                                                         for k, v in sections.items()}
        check('cinematic_quality_report sections sync / facial_timing / secondary resolve to performance.*_report, are '
              'mandatory here and PASS', all(v['status'] == 'PASS' and v['mandatory'] and v['provider'].startswith('performance.')
                                              for v in sections.values()), REPORT['measured']['quality_report_sections'])

        # ---------------- informative measurement: a frame change outside the tools vs the candidate fingerprint
        _, pl, st_now = store.load(storage, ref)
        recorded = st_now['fingerprints']['candidate']
        frame0 = cand_scene.frame_current
        prop_copy = bpy.data.objects[pr['copy']]
        raw_before = {c.name: c.influence for c in prop_copy.constraints}
        with bpy.context.temp_override(scene=cand_scene):
            cand_scene.frame_set(30)
        raw_at30 = {c.name: c.influence for c in prop_copy.constraints}
        at30 = store.status(bridge, pl, st_now)['candidate']['fingerprint']
        for c in prop_copy.constraints:  # put back only the raw influence values, still at frame 30
            c.influence = raw_before[c.name]
        at30_influence_restored = store.status(bridge, pl, st_now)['candidate']['fingerprint']
        with bpy.context.temp_override(scene=cand_scene):
            cand_scene.frame_set(frame0)
        back = store.status(bridge, pl, st_now)['candidate']['fingerprint']
        REPORT['measured']['candidate_fingerprint_after_outside_frame_change'] = {
            'frame_before': frame0, 'equal_at_frame_30': at30 == recorded,
            'equal_at_frame_30_with_raw_influence_put_back': at30_influence_restored == recorded,
            'equal_back_at_frame_before': back == recorded, 'raw_influence_before': raw_before,
            'raw_influence_at_frame_30': raw_at30,
            'note': 'informative (no check): a frame change made outside the tools flushes the keyed CHILD_OF influence '
                    'of the prop copy into its raw value, which cm_candidate_fingerprint reads'}

        # ---------------- rejection gives everything back
        _, plan2, state2 = store.load(storage, ref)
        REPORT['measured']['touched_rows_before_reject'] = len(state2['assembly']['touched'])
        rejected = timed('reject', lambda: dispatch('cinematic_reject', bridge, storage, 'op-r', cinematic=ref, apply=True))
        after = bridge.run(DIGESTS, {'actions': action_names, 'objects': object_names, 'keys': [face_key]})
        check('reject restores the source scene fingerprint exactly', rejected['source_restored'] is True
              and not rejected['undo_failed'], {k: rejected[k] for k in ('removed', 'undo_failed', 'source_restored')})
        check('every Action identical after reject (digests)', before['actions'] == after['actions'],
              {k: before['actions'][k] == after['actions'][k] for k in action_names})
        check('actors, face Key animation, props and M8 objects identical after reject', before['objects'] == after['objects']
              and before['keys'] == after['keys'],
              {k: before['objects'][k] == after['objects'][k] for k in object_names})
        a_obj = bpy.data.objects['ActorA']
        check('A active Action back, M7 tracks unmuted, no M10 tracks, no M10 datablocks',
              a_obj.animation_data.action == bpy.data.actions['A_Walk']
              and [t.name for t in a_obj.animation_data.nla_tracks] == ['M7_FACE M7F_synth_v1']
              and not a_obj.animation_data.nla_tracks[0].mute
              and not any(o.get('production_m10_owner') for o in bpy.data.objects)
              and not any(x.get('production_m10_owner') for x in bpy.data.actions)
              and not any(s.get('production_m10_owner') for s in bpy.data.scenes)
              and not any(c.get('production_m10_owner') for c in bpy.data.collections))
        fresh_after = m8_fresh(bridge, storage, m8_ref)
        REPORT['measured']['m8_after_reject'] = fresh_after
        check('M8 acceptance (real order) fresh after reject: M10 left no trace on its primary', fresh_after == 'FRESH', fresh_after)
        REPORT['metrics'] = {'bridge_calls': bridge.calls, 'payload_bytes_total': bridge.bytes, 'largest_payload_bytes': bridge.largest}
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    save()
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
