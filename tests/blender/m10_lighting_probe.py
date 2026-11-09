"""M10 lighting in real Blender (own background process, inline fixture): EEVEE light-response calibration, plan
(read-only), setup of three presets with owned lights + owned world on the candidate only, EEVEE validation with exact
Workbench masks, source scene/world/material integrity, refusals, and rejection that removes owned lights/worlds.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_lighting_probe.py -- <out_dir>

Only lifecycle_handlers, lighting and review (for normalize_section, the quality-report reader of lighting_report)
are imported (service._loaded is set by hand): the other M10 subsystems must not decide whether this probe runs.
Integration checks: the recorded validation summary carries the candidate fingerprint and metrics.exposure_flicker,
lighting_report returns it without rendering, and a later montage change makes it LIGHTING_VALIDATION_STALE.
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
import mathutils  # noqa: E402

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402

REPORT = {'suite': 'm10_lighting', 'blender': bpy.app.version_string, 'checks': [], 'errors': [], 'metrics': {}}
OUT = None


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
        return json.loads(json.dumps(ns['result']))


def save():
    (OUT / 'm10_lighting.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def check(name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    save()


def expect(name, code, function):
    try:
        function()
    except Exception as exc:
        check(name + ' -> ' + code, str(exc).startswith(code), str(exc)[:300])
        return
    check(name + ' -> ' + code, False, 'unexpected success')


# ---- 0. calibration: EEVEE response of a white diffuse plane (reproduces the constants in lighting.ENERGY_MODEL) ----

def calibrate():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_EEVEE'
    scene.eevee.taa_render_samples = 16
    scene.render.resolution_x = scene.render.resolution_y = 64
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.render.dither_intensity = 0.0
    world = bpy.data.worlds.new('CalWorld')
    scene.world = world
    background = world.node_tree.nodes['Background']
    background.inputs['Strength'].default_value = 0.0
    mesh = bpy.data.meshes.new('CalPlane')
    mesh.from_pydata([(-5, -5, 0), (5, -5, 0), (5, 5, 0), (-5, 5, 0)], [], [(0, 1, 2, 3)])
    plane = bpy.data.objects.new('CalPlane', mesh)
    scene.collection.objects.link(plane)
    mat = bpy.data.materials.new('CalWhite')
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    for node in list(nodes):
        if node.type != 'OUTPUT_MATERIAL':
            nodes.remove(node)
    diffuse = nodes.new('ShaderNodeBsdfDiffuse')
    diffuse.inputs['Color'].default_value = (1, 1, 1, 1)
    mat.node_tree.links.new(diffuse.outputs['BSDF'], nodes['Material Output'].inputs['Surface'])
    mesh.materials.append(mat)
    cam_data = bpy.data.cameras.new('CalCam')
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = 0.5
    cam = bpy.data.objects.new('CalCam', cam_data)
    scene.collection.objects.link(cam)
    cam.location = (0, 0, 5)
    scene.camera = cam
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'

    def center(tag):
        path = str(OUT / ('cal_' + tag + '.exr'))
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(path, check_existing=False)
        w, h = image.size
        pixels = list(image.pixels)
        bpy.data.images.remove(image)
        i = ((h // 2) * w + w // 2) * 4
        return pixels[i]

    rows = {}
    for tag, kind, energy, distance, size in (('sun', 'SUN', 1.0, 3.0, None), ('point_2m', 'POINT', 100.0, 2.0, None),
                                              ('point_4m', 'POINT', 100.0, 4.0, None),
                                              ('area_small_2m', 'AREA', 100.0, 2.0, 0.05),
                                              ('area_small_4m', 'AREA', 100.0, 4.0, 0.05)):
        data = bpy.data.lights.new('CalLight', kind)
        data.energy = energy
        if kind == 'POINT':
            data.shadow_soft_size = 0.01
        if size:
            data.size = size
        lamp = bpy.data.objects.new('CalLight', data)
        scene.collection.objects.link(lamp)
        lamp.location = (0, 0, distance)
        value = center(tag)
        rows[tag] = value / energy if kind == 'SUN' else value * distance * distance / energy
        bpy.data.objects.remove(lamp)
        bpy.data.lights.remove(data)
    background.inputs['Color'].default_value = (0.5, 0.5, 0.5, 1.0)
    background.inputs['Strength'].default_value = 1.0
    rows['world_ratio'] = center('world') / 0.5
    import gpu  # allowed in the probe (not in payloads): which GPU actually rasterized
    renderer = gpu.platform.renderer_get()
    return rows, renderer


# ---- fixture ----

def build_fixture():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    world = bpy.data.worlds.new('SourceWorld')
    world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.2, 0.25, 0.3, 1.0)
    world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0.5
    scene.world = world
    arm = bpy.data.armatures.new('ActorRig')
    rig = bpy.data.objects.new('ActorRig', arm)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for name, head, tail, parent in (('root', (0, 0, 0), (0, 0, .5), None), ('spine', (0, 0, .5), (0, 0, 1.2), 'root'),
                                     ('head', (0, 0, 1.2), (0, 0, 1.7), 'spine')):
        bone = arm.edit_bones.new(name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = arm.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.animation_data_create()
    for frame, x in ((1, 0.0), (60, 0.5)):
        rig.location = (x, 0, 0)
        rig.keyframe_insert('location', frame=frame)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.9))
    body = bpy.context.active_object
    body.name = 'ActorBody'
    body.scale = (0.5, 0.3, 1.8)
    body.parent = rig
    material = bpy.data.materials.new('ActorSkin')
    material.use_nodes = True
    material.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.6, 0.45, 0.35, 1.0)
    body.data.materials.append(material)
    bpy.ops.mesh.primitive_plane_add(size=12, location=(0, 0, 0))
    bpy.context.active_object.name = 'Ground'
    bpy.ops.mesh.primitive_plane_add(size=12, location=(0, 3, 3), rotation=(math.radians(90), 0, 0))
    bpy.context.active_object.name = 'Backdrop'
    lamp_data = bpy.data.lights.new('SourceLamp', 'POINT')
    lamp_data.energy = 30.0
    lamp = bpy.data.objects.new('SourceLamp', lamp_data)
    scene.collection.objects.link(lamp)
    lamp.location = (-4, -2, 3)
    cam_data = bpy.data.cameras.new('SourceCam')
    cam = bpy.data.objects.new('SourceCam', cam_data)
    scene.collection.objects.link(cam)
    cam.location = (0, -6, 1.5)
    cam.rotation_euler = (math.radians(88), 0, 0)
    scene.camera = cam
    scene.frame_start, scene.frame_end = 1, 60
    scene.frame_set(1)
    path = OUT / 'm10_lighting_fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


def world_snapshot(world):
    bg = world.node_tree.nodes['Background']
    return {'name': world.name, 'color': [round(v, 6) for v in bg.inputs['Color'].default_value],
            'strength': round(bg.inputs['Strength'].default_value, 6), 'use_nodes': world.use_nodes,
            'nodes': sorted(n.name for n in world.node_tree.nodes)}


def material_snapshot():
    rows = {}
    for mat in bpy.data.materials:
        values = []
        if mat.node_tree:
            for node in mat.node_tree.nodes:
                for socket in node.inputs:
                    if hasattr(socket, 'default_value'):
                        try:
                            values.append([node.name, socket.name, [round(v, 6) for v in socket.default_value]])
                        except TypeError:
                            values.append([node.name, socket.name, round(socket.default_value, 6)
                                           if isinstance(socket.default_value, float) else str(socket.default_value)])
        rows[mat.name] = values
    return rows



def main():
    global OUT
    OUT = Path(sys.argv[sys.argv.index('--') + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        from blender_mcp.production.cinematic import lifecycle_handlers, lighting, model, registry, service, store  # noqa: F401
        from blender_mcp.production.cinematic import review  # quality-report reader of lighting_report
        from blender_mcp.production.cinematic import imaging
        from blender_mcp.production.cinematic.runtime import COMMON
        from blender_mcp.production.common.asset_safety import current_asset
        service._loaded = True
        dispatch = service.dispatch

        cal, renderer = calibrate()
        REPORT['metrics']['calibration'] = cal
        REPORT['metrics']['gpu_renderer'] = renderer
        check('EEVEE rasterized on the NVIDIA GPU (abort otherwise)', 'NVIDIA' in renderer, renderer)
        if 'NVIDIA' not in renderer:
            raise RuntimeError('GPU_REQUIRED: EEVEE did not run on the RTX; aborting instead of measuring a CPU path')
        measured = lighting.ENERGY_MODEL['measured']
        check('calibration reproduces ENERGY_MODEL constants (<2 %)',
              all(abs(cal[k] - measured[k if k != 'sun' else 'sun_per_w_m2']) / measured[k if k != 'sun' else 'sun_per_w_m2'] < 0.02
                  for k in ('sun', 'point_2m', 'point_4m', 'area_small_2m', 'area_small_4m', 'world_ratio')), cal)
        check('formulas match the renders within 6 % up to 4 m',
              abs(cal['sun'] * math.pi - 1) < 0.06 and abs(cal['point_2m'] * 4 * math.pi ** 2 - 1) < 0.06
              and abs(cal['point_4m'] * 4 * math.pi ** 2 - 1) < 0.06 and abs(cal['area_small_2m'] * math.pi ** 2 - 1) < 0.06
              and abs(cal['area_small_4m'] * math.pi ** 2 - 1) < 0.06,
              {'sun': cal['sun'] * math.pi, 'point_2m': cal['point_2m'] * 4 * math.pi ** 2,
               'point_4m': cal['point_4m'] * 4 * math.pi ** 2, 'area_2m': cal['area_small_2m'] * math.pi ** 2,
               'area_4m': cal['area_small_4m'] * math.pi ** 2})

        build_fixture()
        bridge = LocalBridge()
        storage = Storage(OUT / 'store')
        register_open_fixture(bridge, storage, OUT)
        key = registry.asset_key(current_asset(bridge, storage), 'Scene')
        # Real registration (measured record with integrity digests): create_candidate re-measures every actor and
        # refuses hand-written records (ACTOR_STALE).
        from blender_mcp.production.cinematic import actors as actor_module  # noqa: F401 (registers the handler)
        actor = dispatch('cinematic_register_actor', bridge, storage, 'op-reg', object_name='ActorRig',
                         options={'role': 'PROTAGONIST', 'bones': {'head': 'head'}})
        check('actor registered through cinematic_register_actor with its measured meshes',
              registry.find(storage, key, actor['actor_id'])['meshes'] == ['ActorBody'], actor.get('warnings'))
        brief = {'schema': 'production.cinematic_brief.v1', 'duration_s': 2.0, 'fps': 30,
                 'actors': [{'actor_id': actor['actor_id'], 'role': 'PROTAGONIST'}]}
        shots = {'schema': 'production.cinematic_shots.v1',
                 'shots': [{'shot_id': 'S1', 'frame_start': 1, 'frame_end': 30},
                           {'shot_id': 'S2', 'frame_start': 31, 'frame_end': 60}]}
        created = dispatch('cinematic_create_candidate', bridge, storage, 'op-create', brief=brief,
                           options={'shots': shots}, apply=True)
        ref = created['cinematic']
        source = bpy.data.scenes['Scene']
        candidate = bpy.data.scenes[created['scene']]
        check('candidate shares the source world before lighting', candidate.world == source.world)

        # Cameras + cuts stand in for the cameras subsystem (owned objects, recorded via store.mutate).
        path, plan, state = store.load(storage, ref)
        gid = plan['generation_id']
        cam_body = COMMON + r'''
def probe_cameras(params):
    scene, src, cin = cm_candidate(params)
    made = []
    for name, loc, target in params['cameras']:
        data = bpy.data.cameras.new(name)
        obj = cm_new_object(params, name, data, cin, 'camera')
        obj.location = loc
        obj.rotation_euler = (mathutils.Vector(target) - mathutils.Vector(loc)).to_track_quat('-Z', 'Y').to_euler()
        made.append(obj)
    scene.camera = made[0]
    for marker_name, frame, index in params['markers']:
        marker = scene.timeline_markers.new(marker_name, frame=frame)
        marker.camera = made[index]
    return {'owned': {'objects': [o.name for o in made], 'data': [['CAMERA', o.data.name] for o in made]},
            'touched': [], 'fingerprints': cm_fingerprints(params)}

result = probe_cameras(params)
'''
        cam_a, cam_b = model.owned_name(gid, 1, 'CAM_A'), model.owned_name(gid, 1, 'CAM_B')

        class Ctx:
            pass
        ctx = Ctx()
        ctx.bridge, ctx.storage, ctx.operation_id = bridge, storage, 'op-cams'
        params = store.assembly_params(plan, state, {
            'cameras': [[cam_a, [0.6, -6.0, 1.4], [0.0, 0.0, 1.1]], [cam_b, [4.0, -4.5, 1.6], [0.0, 0.0, 1.1]]],
            'markers': [['S1', 1, 0], ['S2', 31, 1]]})
        new_state, _ = store.mutate(ctx, path, plan, state, 'probe_cameras', cam_body, params)
        new_state['cameras'] = {'CAM_A': {'object': cam_a}, 'CAM_B': {'object': cam_b}}
        new_state['shots'] = [{'shot_id': 'S1', 'frame_start': 1, 'frame_end': 30, 'camera_id': 'CAM_A'},
                              {'shot_id': 'S2', 'frame_start': 31, 'frame_end': 60, 'camera_id': 'CAM_B'}]
        store.record(path, new_state)

        source_world = source.world
        world_before = world_snapshot(source_world)
        materials_before = material_snapshot()
        source_fp = store.status(bridge, plan, new_state)['source']['fingerprint']
        source_settings = (source.render.engine, source.view_settings.view_transform, source.view_settings.look,
                           source.view_settings.exposure, source.frame_current, source.camera.name)
        check('source fingerprint equals the plan before lighting', source_fp == plan['source']['fingerprint'])

        # ---- plan (READ_ONLY) ----
        plans = {}
        for preset in ('HIGH_KEY', 'NEUTRAL_THREE_POINT', 'LOW_KEY', 'HORROR_UNDERLIGHT', 'RIM_DRAMATIC', 'NIGHT_EXTERIOR'):
            _, _, st = store.load(storage, ref)
            before = store.status(bridge, plan, st)['candidate']['fingerprint']
            frame_before = (candidate.frame_current, source.frame_current)
            objects_before = len(bpy.data.objects)
            out = dispatch('cinematic_plan_lighting', bridge, storage, 'op-plan', cinematic=ref,
                           options={'preset': preset, 'key_subject': actor['actor_id']})
            after = store.status(bridge, plan, st)['candidate']['fingerprint']
            plans[preset] = out['plan']
            check('plan ' + preset + ' is read-only (candidate fingerprint, frames, objects unchanged)',
                  before == after and frame_before == (candidate.frame_current, source.frame_current)
                  and objects_before == len(bpy.data.objects))
        neutral = plans['NEUTRAL_THREE_POINT']
        roles = sorted(row['role'] for row in neutral['lights'])
        check('NEUTRAL plan has KEY/FILL/RIM from the candidate camera', roles == ['FILL', 'KEY', 'RIM']
              and neutral['camera']['object'] == cam_a, {'roles': roles, 'camera': neutral['camera']})
        cam_obj = bpy.data.objects[cam_a]
        cam_loc = list(cam_obj.matrix_basis.translation)  # no parent/constraint: basis is the world transform
        check('plan reads the evaluated camera and subject (not a stale matrix_world)',
              max(abs(a - b) for a, b in zip(neutral['camera']['location'], cam_loc)) < 1e-4
              and max(abs(a - b) for a, b in zip(neutral['subject']['center'], [0.0, 0.0, 0.9])) < 1e-3,
              {'camera': neutral['camera'], 'subject': neutral['subject']})
        toward = [cam_loc[0] - neutral['subject']['center'][0], cam_loc[1] - neutral['subject']['center'][1]]
        norm = math.hypot(*toward)
        axis = [toward[0] / norm, toward[1] / norm]
        check('camera axis is subject->camera from the real camera position',
              max(abs(a - b) for a, b in zip(neutral['camera_axis']['vector'][:2], axis)) < 1e-5, neutral['camera_axis'])
        right = [-axis[1], axis[0]]  # +90 deg around Z: camera right for an observer at the camera
        key = next(r for r in neutral['lights'] if r['role'] == 'KEY')
        rel = [key['location'][0] - key['target'][0], key['location'][1] - key['target'][1]]
        check('KEY sits camera-right and above the aim point (key_side RIGHT)',
              rel[0] * right[0] + rel[1] * right[1] > 0 and key['location'][2] > key['target'][2], key)
        check('KEY energy follows the measured AREA relation', abs(key['energy'] - math.pi ** 2 * key['response']
                                                                  * key['distance_m'] ** 2) < 1e-2, key)
        check('every planned value is labelled PROPOSAL',
              all(r['value_source'] == 'PROPOSAL' for p in plans.values() for r in p['lights']))
        check('source light in candidate is reported',
              any(w['code'] == 'SOURCE_LIGHTS_IN_CANDIDATE' for w in neutral['warnings']), neutral['warnings'])
        horror_key = next(r for r in plans['HORROR_UNDERLIGHT']['lights'] if r['role'] == 'KEY')
        check('HORROR key is an underlight spot', horror_key['type'] == 'SPOT'
              and horror_key['location'][2] < horror_key['target'][2], horror_key)
        shot2 = dispatch('cinematic_plan_lighting', bridge, storage, 'op-plan2', cinematic=ref,
                         options={'preset': 'NEUTRAL_THREE_POINT', 'shot': 'S2'})
        check('plan for shot S2 uses its camera', shot2['plan']['camera']['object'] == cam_b)
        practical = dispatch('cinematic_plan_lighting', bridge, storage, 'op-plan3', cinematic=ref,
                             options={'preset': 'PRACTICAL_ONLY', 'motivated_sources': [{'position': [1.5, -1.0, 1.0]}]})
        check('PRACTICAL_ONLY plan from motivated source', [r['role'] for r in practical['plan']['lights']] == ['PRACTICAL'])
        expect('PRACTICAL_ONLY without sources', 'LIGHTING_PRACTICAL_SOURCES_REQUIRED',
               lambda: dispatch('cinematic_plan_lighting', bridge, storage, 'op-x', cinematic=ref,
                                options={'preset': 'PRACTICAL_ONLY'}))
        expect('unknown plan option', 'CINEMATIC_OPTION_UNSUPPORTED',
               lambda: dispatch('cinematic_plan_lighting', bridge, storage, 'op-x', cinematic=ref, options={'mood': 1}))

        # ---- setup + validate three presets ----
        validations = {}
        for index, preset in enumerate(('NEUTRAL_THREE_POINT', 'LOW_KEY', 'HIGH_KEY')):
            fresh_plan = dispatch('cinematic_plan_lighting', bridge, storage, 'op-p' + str(index), cinematic=ref,
                                  options={'preset': preset})['plan']
            proposal = dispatch('cinematic_setup_lighting', bridge, storage, 'op-s' + str(index), cinematic=ref,
                                options={'plan': fresh_plan, 'replace': index > 0})
            check('setup ' + preset + ' proposal changes nothing', proposal['status'] == 'proposal'
                  and all(bpy.data.objects.get(r['name']) is None for r in proposal['lights']))
            if index == 0:
                expect('setup without candidate-matching plan', 'LIGHTING_PLAN_MISMATCH',
                       lambda: dispatch('cinematic_setup_lighting', bridge, storage, 'op-x', cinematic=ref,
                                        options={'plan': dict(fresh_plan, generation_id='0' * 32)}, apply=True))
            old_lights = [o.name for o in bpy.data.objects if o.type == 'LIGHT' and o.get('production_m10_owner') == gid]
            old_world = candidate.world.name
            setup = dispatch('cinematic_setup_lighting', bridge, storage, 'op-S' + str(index), cinematic=ref,
                             options={'plan': fresh_plan, 'replace': index > 0}, apply=True)
            names = [r['name'] for r in setup['lights']]
            objs = [bpy.data.objects.get(n) for n in names]
            check('setup ' + preset + ' creates owned lights in _CIN only on the candidate',
                  all(o is not None and o.get('production_m10_owner') == gid and o.data.get('production_m10_owner') == gid
                      and list(o.users_collection) == [bpy.data.collections[plan_cin(plan, store, storage, ref)]]
                      and list(o.users_scene) == [candidate] for o in objs), names)
            aim_errors = []
            for row, obj in zip(fresh_plan['lights'], objs):
                # matrix_basis: matrix_world of an object outside the window view layer is only refreshed on
                # evaluation (it read the identity rotation right after setup).
                axis_z = obj.matrix_basis.to_3x3() @ mathutils.Vector((0, 0, -1))
                want = (mathutils.Vector(row['target']) - mathutils.Vector(row['location'])).normalized()
                aim_errors.append(axis_z.angle(want))
            check('setup ' + preset + ' energies and aim match the plan',
                  all(abs(o.data.energy - r['energy']) <= 1e-3 * max(1, r['energy']) for o, r in zip(objs, fresh_plan['lights']))
                  and max(aim_errors) < 1e-4, {'aim_error_rad': max(aim_errors)})
            check('setup ' + preset + ' assigns an owned world to the candidate only',
                  candidate.world.get('production_m10_owner') == gid and source.world == source_world
                  and world_snapshot(source_world) == world_before,
                  {'candidate_world': candidate.world.name, 'source_world': world_snapshot(source_world)})
            if index > 0:
                check('replace removed previous owned lights and world',
                      all(bpy.data.objects.get(n) is None and bpy.data.lights.get(n) is None for n in old_lights)
                      and bpy.data.worlds.get(old_world) is None and names[0].endswith('_r' + str(index + 1)),
                      {'old': old_lights, 'new': names})
            check('materials untouched after setup ' + preset, material_snapshot() == materials_before)
            if index == 0:
                expect('second setup without replace', 'LIGHTING_ALREADY_SET',
                       lambda: dispatch('cinematic_setup_lighting', bridge, storage, 'op-x', cinematic=ref,
                                        options={'plan': fresh_plan}, apply=True))
            state_before = (candidate.frame_current, candidate.camera.name, candidate.render.engine,
                            candidate.view_settings.view_transform, candidate.render.resolution_x,
                            [list(o.color) for o in candidate.objects])
            started = time.perf_counter()
            validation = dispatch('cinematic_validate_lighting', bridge, storage, 'op-V' + str(index), cinematic=ref,
                                  options={'samples_per_shot': 2, 'size': 320, 'samples': 16}, apply=True)
            wall = time.perf_counter() - started
            validations[preset] = validation
            state_after = (candidate.frame_current, candidate.camera.name, candidate.render.engine,
                           candidate.view_settings.view_transform, candidate.render.resolution_x,
                           [list(o.color) for o in candidate.objects])
            check('validate ' + preset + ' restores frame, camera, engine, view, resolution and object colours',
                  state_before == state_after)
            _, _, st = store.load(storage, ref)
            store.fresh(bridge, storage, plan, st)  # raises if the candidate fingerprint moved
            check('validate ' + preset + ' leaves the candidate fingerprint as recorded', True)
            frames = validation['frame_metrics']
            check('validate ' + preset + ' renders EEVEE on the GPU rasterizer',
                  str(validation['render_backend']['actual_backend']).startswith('GPU')
                  and validation['render_backend']['engine'] == 'BLENDER_EEVEE', validation['render_backend'])
            report = json.loads(Path(validation['report']).read_text(encoding='utf-8'))
            unmatched = [report['frames'][str(r['frame'])]['mask_unmatched_fraction'] for r in frames]
            check('validate ' + preset + ' masks are exact (no unmatched pixel) and the subject is visible',
                  max(unmatched) == 0 and all(r['key_subject']['coverage_fraction'] > 0.01 for r in frames),
                  {'coverage': [r['key_subject']['coverage_fraction'] for r in frames]})
            cams = {r['frame']: r['camera'] for r in frames}
            check('validate ' + preset + ' follows the camera cuts (S1 -> CAM_A, S2 -> CAM_B)',
                  all(cams[f] == (cam_a if f <= 30 else cam_b) for f in cams), cams)
            check('validate ' + preset + ' sampled 8 frames with 4 flicker pairs', validation['frames'] == 8
                  and len(report['pair_metrics']) == 4, report['selection'])
            # ---- integration: recorded summary, exposure_flicker and the quality-report provider ----
            _, _, st = store.load(storage, ref)
            recorded = st['qa']['lighting']
            blender_fp = store.status(bridge, plan, st)['candidate']['fingerprint']
            check('validate ' + preset + ' records its summary bound to the current candidate fingerprint',
                  recorded == st['lighting']['validation'] and recorded['candidate_fingerprint'] == st['fingerprints']['candidate']
                  == blender_fp, {'recorded': recorded['candidate_fingerprint'][-16:], 'blender': blender_fp[-16:]})
            same_shot = [abs(p['mean_change_stops']) for p in report['pair_metrics'] if not p['cut']]
            check('validate ' + preset + ' publishes metrics.exposure_flicker (stops) = max same-shot consecutive change',
                  recorded['metrics']['exposure_flicker'] == round(max(same_shot), 6)
                  and recorded['metrics']['exposure_flicker_units'] == 'stops' and report['metrics'] == recorded['metrics'],
                  {'metrics': recorded['metrics'], 'same_shot_pairs': same_shot})
            calls_before = bridge.calls
            provided = lighting.lighting_report(ctx, plan, st)
            section = review.normalize_section(provided)
            check('lighting_report hands the current validation to the quality report without any bridge call',
                  bridge.calls == calls_before and provided['passed'] == recorded['passed']
                  and provided['metrics'] == recorded['metrics'] and section['status'] in ('PASS', 'FAIL')
                  and section['metrics']['exposure_flicker'] == recorded['metrics']['exposure_flicker'],
                  {'section': section['status'], 'findings_by_severity': recorded['findings_by_severity']})
            REPORT['metrics'][preset] = {'wall_s': round(wall, 3), 'timing': validation['timing'],
                                         'shots': validation['shots'], 'frames': frames,
                                         'findings': [(f['code'], f.get('frame') or f.get('frames'), f['value'])
                                                      for f in report['technical_findings']],
                                         'artifacts': validation['artifacts'],
                                         'summary_metrics': recorded['metrics'],
                                         'summary_findings': [(g['code'], g['severity'], g['count'])
                                                              for g in recorded['findings']]}
            if index == 0:
                eevee_png = report['frames'][str(frames[0]['frame'])]['files']['eevee']
                decoded = imaging.read_png(eevee_png)
                image = bpy.data.images.load(eevee_png, check_existing=False)
                pixels = list(image.pixels)
                w, h = image.size
                bpy.data.images.remove(image)
                diff = 0
                for y in range(h):
                    for x in range(w):
                        i = ((h - 1 - y) * w + x) * 4  # Blender stores rows bottom-up
                        j = (y * w + x) * decoded.channels
                        for c in range(3):
                            diff = max(diff, abs(round(pixels[i + c] * 255) - decoded.pixels[j + c]))
                check('pure-Python PNG decode equals Blender image loader (max code diff 0)',
                      diff == 0 and (w, h) == (decoded.width, decoded.height), {'max_diff': diff, 'size': [w, h]})

        means = {p: sum(r['mean'] for r in v['frame_metrics']) / len(v['frame_metrics']) for p, v in validations.items()}
        subject = {p: sum(r['key_subject']['mean'] for r in v['frame_metrics']) / len(v['frame_metrics'])
                   for p, v in validations.items()}
        REPORT['metrics']['mean_luminance_by_preset'] = {'frame': means, 'key_subject': subject}
        check('measured frame luminance orders LOW_KEY < NEUTRAL < HIGH_KEY (design expectation)',
              means['LOW_KEY'] < means['NEUTRAL_THREE_POINT'] < means['HIGH_KEY'], means)
        check('measured key-subject luminance LOW_KEY < HIGH_KEY', subject['LOW_KEY'] < subject['HIGH_KEY'], subject)

        # ---- a montage change after validation makes the recorded lighting QA stale (no re-render) ----
        lens_body = COMMON + r'''
def probe_lens(params):
    scene, src, cin = cm_candidate(params)
    cam = bpy.data.objects[params['camera']]
    if not cm_is_owned(cam, params) or not cm_is_owned(cam.data, params):
        cm_fail('PROBE_NOT_OWNED', cam.name)
    cam.data.lens = params['lens']
    return {'touched': [], 'owned': {}, 'fingerprints': cm_fingerprints(params)}

result = probe_lens(params)
'''
        _, _, st = store.load(storage, ref)
        lens_before = bpy.data.objects[cam_a].data.lens
        ctx.operation_id = 'op-lens'
        changed, _ = store.mutate(ctx, path, plan, st, 'probe_lens', lens_body,
                                  store.assembly_params(plan, st, {'camera': cam_a, 'lens': lens_before + 5.0}))
        store.record(path, changed)
        calls_before = bridge.calls
        stale = lighting.lighting_report(ctx, plan, changed)
        stale_section = review.normalize_section(stale)
        check('a camera lens change after validation makes lighting_report NOT_RUN (LIGHTING_VALIDATION_STALE) '
              'without rendering; the recorded summary itself is kept',
              changed['fingerprints']['candidate'] != st['fingerprints']['candidate']
              and stale['status'] == 'NOT_RUN' and stale['passed'] is None
              and stale['reason'].startswith('LIGHTING_VALIDATION_STALE: ') and stale_section['status'] != 'PASS'
              and lighting._short_fingerprint(st['fingerprints']['candidate']) in stale['reason']
              and lighting._short_fingerprint(changed['fingerprints']['candidate']) in stale['reason']
              and changed['qa']['lighting'] == st['qa']['lighting'] and bridge.calls == calls_before,
              {'reason': stale['reason'], 'section': stale_section['status'],
               'fingerprint_tails': [st['fingerprints']['candidate'][-12:], changed['fingerprints']['candidate'][-12:]]})

        # ---- explicit lights, look and refusals ----
        expect('AgX look without prefix', 'LIGHTING_LOOK_INVALID',
               lambda: dispatch('cinematic_setup_lighting', bridge, storage, 'op-x', cinematic=ref,
                                options={'look': 'Punchy'}, apply=True))
        expect('unknown setup option', 'CINEMATIC_OPTION_UNSUPPORTED',
               lambda: dispatch('cinematic_setup_lighting', bridge, storage, 'op-x', cinematic=ref,
                                options={'materials': {}}, apply=True))
        explicit = {'lights': [{'role': 'KEY', 'type': 'POINT', 'location': [2.0, -2.5, 2.5], 'target': [0, 0, 1.2],
                                'energy': 400.0, 'temperature': 4500.0},
                               {'role': 'PRACTICAL', 'type': 'SPOT', 'location': [-1.5, -1.0, 2.2],
                                'target': [0, 0, 1.0], 'energy': 150.0, 'spot_size': 0.6}],
                    'world': {'color': [0.02, 0.03, 0.05], 'strength': 1.0}, 'look': 'AgX - Punchy',
                    'exposure_ev': 0.5, 'replace': True}
        preview = dispatch('cinematic_setup_lighting', bridge, storage, 'op-e0', cinematic=ref, options=explicit)
        blocker = bpy.data.objects.new(preview['lights'][1]['name'], None)  # not in any scene: fingerprints unaffected
        lights_before = sorted(o.name for o in bpy.data.objects if o.type == 'LIGHT')
        look_before = (candidate.view_settings.look, candidate.view_settings.exposure, candidate.world.name)
        expect('name collision is refused before any change', 'CINEMATIC_NAME_COLLISION',
               lambda: dispatch('cinematic_setup_lighting', bridge, storage, 'op-e1', cinematic=ref, options=explicit,
                                apply=True))
        bpy.data.objects.remove(blocker)
        degenerate = dict(explicit, lights=[explicit['lights'][0], dict(explicit['lights'][1],
                                                                          target=explicit['lights'][1]['location'])])
        expect('failure after the first light is created rolls everything back', 'LIGHTING_AIM_DEGENERATE',
               lambda: dispatch('cinematic_setup_lighting', bridge, storage, 'op-e3', cinematic=ref,
                                options=degenerate, apply=True))
        _, _, st = store.load(storage, ref)
        store.fresh(bridge, storage, plan, st)  # raises CINEMATIC_STALE_CANDIDATE if anything was left behind
        check('refused setups left no partial light, world or view change and the candidate fingerprint intact',
              sorted(o.name for o in bpy.data.objects if o.type == 'LIGHT') == lights_before
              and (candidate.view_settings.look, candidate.view_settings.exposure, candidate.world.name) == look_before
              and not [d for d in bpy.data.lights if d.users == 0], look_before)
        done = dispatch('cinematic_setup_lighting', bridge, storage, 'op-e2', cinematic=ref, options=explicit, apply=True)
        check('explicit lights + AgX look + exposure on the candidate only',
              candidate.view_settings.look == 'AgX - Punchy' and abs(candidate.view_settings.exposure - 0.5) < 1e-6
              and (source.view_settings.look, source.view_settings.exposure) == (source_settings[2], source_settings[3])
              and bpy.data.objects[done['lights'][0]['name']].data.use_temperature
              and bpy.data.objects[done['lights'][1]['name']].data.type == 'SPOT', done['view'])
        check('source scene settings untouched', (source.render.engine, source.view_settings.view_transform,
                                                  source.view_settings.look, source.view_settings.exposure,
                                                  source.frame_current, source.camera.name) == source_settings)

        # ---- rejection ----
        rejected = dispatch('cinematic_reject', bridge, storage, 'op-reject', cinematic=ref, apply=True)
        check('reject restores the source fingerprint', rejected['source_restored'] is True, rejected.get('removed'))
        check('reject removed every owned light and world',
              not [o for o in bpy.data.objects if o.get('production_m10_owner') == gid]
              and not [d for d in bpy.data.lights if d.get('production_m10_owner') == gid]
              and not [w for w in bpy.data.worlds if w.get('production_m10_owner') == gid], rejected.get('removed'))
        check('source world identical after reject', source.world == source_world
              and world_snapshot(source_world) == world_before and bpy.data.lights.get('SourceLamp') is not None)
        check('materials untouched after reject', material_snapshot() == materials_before)
        REPORT['metrics']['bridge'] = {'calls': bridge.calls, 'payload_bytes': bridge.bytes,
                                       'max_payload_bytes': bridge.max_bytes}
    except Exception as exc:
        REPORT['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
        traceback.print_exc()
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    save()
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


def plan_cin(plan, store, storage, ref):
    _, _, state = store.load(storage, ref)
    return state['assembly']['collections']['cin']


main()
