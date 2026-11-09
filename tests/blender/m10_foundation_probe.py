"""M10 foundation in real Blender (own background process, controlled fixture): candidate scene, recorded
object-level mutation, external-edit detection, rejection that restores the source exactly, MASTER refusal.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_foundation_probe.py -- <out_dir>
"""
from __future__ import annotations

import json
import sys
import threading
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


REPORT = {'suite': 'foundation', 'blender': bpy.app.version_string, 'checks': [], 'errors': []}


def check(out, name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    (out / 'm10_foundation.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def expect(out, name, code, function):
    try:
        function()
    except Exception as exc:
        check(out, name + ' -> ' + code, code in str(exc).split(':')[0] or str(exc).startswith(code), str(exc)[:300])
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
    for name, head, tail, parent in (('root', (0, 0, 0), (0, 0, .5), None), ('spine', (0, 0, .5), (0, 0, 1.2), 'root'),
                                     ('head', (0, 0, 1.2), (0, 0, 1.6), 'spine')):
        bone = arm_data.edit_bones.new(name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = arm_data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    action = bpy.data.actions.new('Actor_Walk')
    rig.animation_data_create().action = action
    for frame, x in ((1, 0.0), (30, 1.5), (60, 3.0)):
        rig.pose.bones['root'].location = (0, x, 0)
        rig.pose.bones['root'].keyframe_insert('location', frame=frame)
        rig.pose.bones['spine'].rotation_quaternion = (1, 0, 0, 0.1 * frame / 60)
        rig.pose.bones['spine'].keyframe_insert('rotation_quaternion', frame=frame)
    bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0, 0, 0.8))
    body = bpy.context.active_object
    body.name = 'ActorBody'
    body.parent = rig
    bpy.ops.mesh.primitive_plane_add(size=10)
    bpy.context.active_object.name = 'Ground'
    # Collections: the actor lives in 'Cast' (linked whole), the ground and a prop in 'Environment' (mirrored
    # because the prop is excluded); the Lantern original must not reach the candidate.
    cast = bpy.data.collections.new('Cast'); scene.collection.children.link(cast)
    env = bpy.data.collections.new('Environment'); scene.collection.children.link(env)
    for name, target in (('ActorRig', cast), ('ActorBody', cast), ('Ground', env)):
        obj = bpy.data.objects[name]
        target.objects.link(obj)
        scene.collection.objects.unlink(obj)
    bpy.ops.mesh.primitive_cube_add(size=0.2, location=(1, 0, 0.1))
    lantern = bpy.context.active_object
    lantern.name = 'Lantern'
    env.objects.link(lantern)
    scene.collection.objects.unlink(lantern)
    scene.frame_start, scene.frame_end = 1, 60
    path = out / 'm10_foundation_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


MUTATE = None


def main():
    out = Path(sys.argv[sys.argv.index('--') + 1])
    out.mkdir(parents=True, exist_ok=True)
    try:
        from blender_mcp.production.cinematic import model, registry, store
        from blender_mcp.production.cinematic.runtime import COMMON
        from blender_mcp.production.cinematic.service import dispatch
        from blender_mcp.production.common.asset_safety import current_asset
        fixture = build_fixture(out)
        bridge = LocalBridge()
        storage = Storage(out / 'store')
        register_open_fixture(bridge, storage, out)
        asset = current_asset(bridge, storage)
        key = registry.asset_key(asset, 'Scene')
        actor = dispatch('cinematic_register_actor', bridge, storage, 'op-r1', object_name='ActorRig',
                         options={'role': 'PROTAGONIST'})
        again = dispatch('cinematic_register_actor', bridge, storage, 'op-r2', object_name='ActorRig',
                         options={'role': 'PROTAGONIST'})
        check(out, 'real registration is idempotent', actor['actor_id'] == again['actor_id'] and again['idempotent']
              and len(registry.records(storage, key)) == 1, actor['actor_id'])
        brief = {'schema': 'production.cinematic_brief.v1', 'duration_s': 2.0, 'fps': 30,
                 'actors': [{'actor_id': actor['actor_id'], 'role': 'PROTAGONIST'}]}
        walk_before = bpy.data.actions['Actor_Walk']
        proposal = dispatch('cinematic_create_candidate', bridge, storage, 'op-1', brief=brief)
        check(out, 'proposal does not touch Blender', proposal['status'] == 'proposal'
              and not any(s.get('production_m10_owner') for s in bpy.data.scenes), proposal['plan']['timeline'])
        expect(out, 'brief at another fps is refused', 'CINEMATIC_FPS_MISMATCH',
               lambda: dispatch('cinematic_create_candidate', bridge, storage, 'op-x', brief=dict(brief, fps=24)))
        rig_collections = sorted(c.name for c in bpy.data.objects['ActorRig'].users_collection)
        created = dispatch('cinematic_create_candidate', bridge, storage, 'op-2', brief=brief, apply=True,
                           options={'exclude_objects': ['Lantern']})
        ref = created['cinematic']
        scene = bpy.data.scenes.get(created['scene'])
        check(out, 'candidate scene with SRC/CIN collections', scene is not None and scene.frame_end == 60
              and scene.render.fps == 30 and created['linked_objects'] == 3, created)
        check(out, 'source objects are linked, not copied', len(bpy.data.objects['ActorRig'].users_scene) == 2
              and len(bpy.data.objects) == 4)
        check(out, 'Cast linked whole: actor users_collection unchanged (M8 signs it)',
              sorted(c.name for c in bpy.data.objects['ActorRig'].users_collection) == rig_collections
              and created['linked_collections'] == ['Cast'], {'before': rig_collections, 'linked': created['linked_collections']})
        check(out, 'excluded prop absent from the candidate, present in the source',
              scene.objects.get('Lantern') is None and bpy.data.scenes['Scene'].objects.get('Lantern') is not None
              and scene.objects.get('Ground') is not None, created['individually_linked_objects'])
        path, plan, state = store.load(storage, ref)
        check(out, 'state CINEMATIC_CANDIDATE with baseline capture', state['state'] == 'CINEMATIC_CANDIDATE'
              and 'ActorRig' in state['assembly']['baseline']['objects'])

        # A recorded object-level mutation: detach the active Action into a candidate NLA strip offset by 10 frames
        # and add a staging delta. This is the pattern the subsystems use.
        body = COMMON + r'''
def probe_mutate(params):
    scene, src, cin = cm_candidate(params)
    rig = cm_object('ActorRig', ['ARMATURE'])
    touched = [cm_touch_animdata(rig, 'OBJECT'), cm_touch_delta(rig)]
    action = rig.animation_data.action
    track_name = params['track']
    track = cm_new_track(rig, 'OBJECT', track_name, params)
    touched.append(cm_touch_track(rig, 'OBJECT', track_name))
    cm_ensure_pose(rig)
    rig.animation_data.action = None
    strip = track.strips.new('walk', 11, action)
    rig.delta_location = (2.0, 0.0, 0.0)
    camera_data = bpy.data.cameras.new(params['camera'])
    camera = cm_new_object(params, params['camera'], camera_data, cin, 'camera')
    with bpy.context.temp_override(scene=scene):
        scene.frame_set(40)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        sampled = list(rig.evaluated_get(depsgraph).pose.bones['root'].location)
    return {'touched': touched, 'owned': {'objects': [camera.name]}, 'sampled_root_at_40': sampled,
            'strip': [strip.frame_start, strip.frame_end], 'fingerprints': cm_fingerprints(params)}

result = probe_mutate(params)
'''
        class Ctx:
            pass
        ctx = Ctx(); ctx.bridge = bridge; ctx.storage = storage; ctx.operation_id = 'op-3'
        params = store.assembly_params(plan, state, {'track': model.owned_name(plan['generation_id'], 1, 'BODY_ActorRig'),
                                                     'camera': model.owned_name(plan['generation_id'], 1, 'CAM_A')})
        new_state, result = store.mutate(ctx, path, plan, state, 'probe_mutation', body, params)
        store.record(path, new_state)
        check(out, 'NLA strip offset 10 frames evaluates the Action exactly (frame 40 -> action frame 30)',
              abs(result['sampled_root_at_40'][1] - 1.5) < 1e-6, result)
        check(out, 'source Action unchanged (same datablock, no new keys)', bpy.data.actions['Actor_Walk'] == walk_before
              and len(bpy.data.actions) == 1)
        check(out, 'touched rows recorded', len(new_state['assembly']['touched']) == 3)
        # External edit: someone moves the ground in the source scene -> next mutation must refuse.
        bpy.data.objects['Ground'].location.x = 0.25
        path, plan, state = store.load(storage, ref)
        expect(out, 'external source edit detected', 'CINEMATIC_STALE_SOURCE',
               lambda: store.mutate(ctx, path, plan, state, 'probe_again', body, params))
        bpy.data.objects['Ground'].location.x = 0.0
        rejected = dispatch('cinematic_reject', bridge, storage, 'op-4', cinematic=ref, apply=True)
        check(out, 'reject restores the source fingerprint exactly', rejected['source_restored'] is True,
              {k: rejected[k] for k in ('removed', 'undo_failed')})
        rig = bpy.data.objects['ActorRig']
        check(out, 'active Action back, NLA track gone, delta back, owned objects gone',
              rig.animation_data.action == walk_before and len(rig.animation_data.nla_tracks) == 0
              and tuple(rig.delta_location) == (0.0, 0.0, 0.0) and bpy.data.objects.get(params['camera']) is None
              and not any(s.get('production_m10_owner') for s in bpy.data.scenes))
        expect(out, 'terminal after rejection', 'GENERATION_TERMINAL',
               lambda: dispatch('cinematic_reject', bridge, storage, 'op-5', cinematic=ref, apply=True))
        # A rig edited after registration must not be planned against (ACTOR_STALE).
        stale_store = Storage(out / 'store_stale')
        register_open_fixture(bridge, stale_store, out)
        from blender_mcp.production.cinematic.service import Context, Request
        from blender_mcp.production.cinematic.actors import register_actor
        registered = dispatch('cinematic_register_actor', bridge, stale_store, 'op-r', object_name='ActorRig',
                              options={'role': 'PROTAGONIST'})
        stale_id = registered['actor']['actor_id'] if 'actor' in registered else registered.get('actor_id')
        bpy.data.objects['ActorRig'].data.bones['head'].use_deform = not bpy.data.objects['ActorRig'].data.bones['head'].use_deform
        expect(out, 'actor edited after registration is refused at planning', 'ACTOR_STALE',
               lambda: dispatch('cinematic_create_candidate', bridge, stale_store, 'op-s',
                                brief=dict(brief, actors=[{'actor_id': stale_id, 'role': 'PROTAGONIST'}])))
        bpy.data.objects['ActorRig'].data.bones['head'].use_deform = not bpy.data.objects['ActorRig'].data.bones['head'].use_deform
        # MASTER: save a separate copy, register it MASTER in another store, refuse candidate creation.
        bpy.ops.wm.save_as_mainfile(filepath=str(out / 'm10_master_copy.blend'))
        master_store = Storage(out / 'store_master')
        from blender_mcp.production.common.asset_safety import register_master
        register_master(bridge, master_store)
        key2 = registry.asset_key(current_asset(bridge, master_store), 'Scene')
        actor2 = dispatch('cinematic_register_actor', bridge, master_store, 'op-r3', object_name='ActorRig',
                          options={'role': 'PROTAGONIST'})
        brief2 = dict(brief, actors=[{'actor_id': actor2['actor_id'], 'role': 'PROTAGONIST'}])
        expect(out, 'MASTER refuses candidate creation', 'MASTER_ASSET_READ_ONLY',
               lambda: dispatch('cinematic_create_candidate', bridge, master_store, 'op-6', brief=brief2, apply=True))
        check(out, 'MASTER refusal created no scene', not any(s.get('production_m10_owner') for s in bpy.data.scenes))
        REPORT['metrics'] = {'bridge_calls': bridge.calls, 'payload_bytes': bridge.bytes}
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    (out / 'm10_foundation.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
