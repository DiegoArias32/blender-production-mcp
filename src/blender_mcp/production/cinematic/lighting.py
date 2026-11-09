"""M10 lighting: plan (read-only), setup (owned lights + owned world on the candidate scene) and EEVEE validation.

Every number a plan proposes is an artistic STARTING POINT, never a universal formula: presets, angles, distances,
key/fill ratios and thresholds are labelled PROPOSAL. What is measured is labelled so:
- light response (EEVEE, Blender 5.2.1, white albedo-1 diffuse plane facing the light, exposure 0, Standard view):
  sun S W/m2 -> 0.318359*S (= S/pi); point/spot P W at d m -> 0.025234*P/d^2 at 2 m (= P/(4 pi^2 d^2), -0.4 %)
  and 0.024063 at 4 m (-5.0 %); small area P W -> 0.100937*P/d^2 at 2 m (= P/(pi^2 d^2), -0.4 %), 0.09625 at 4 m;
  uniform world colour c, strength s -> 0.992*c*s (docs/cinematic-lighting.md, calibration probe);
- the plan inverts those relations: it chooses a target response per light and returns the energy in W (W/m2 for
  SUN) for the planned distance.

Ownership: lights are `M10C_<g8>_v<n>_LGT_<ROLE>[_k][_r<rev>]` objects + data in `_CIN`; the world is an owned world
assigned only to the candidate scene (the source world is part of the M8 context and is never edited). Materials are
never touched: bad light is not hidden by changing PBR values.

Validation renders EEVEE (GPU rasterizer) frames of the candidate scene and exact Workbench actor masks (FLAT + OBJECT
colour + Standard + AA OFF + dither 0: the only exact combination measured), restores every setting it changed, and
measures the PNGs on the host with `imaging` (pure Python). Its summary (state['qa']['lighting']) is bound to the
candidate fingerprint it validated; `lighting_report` hands it to the quality report only while that fingerprint is
still the current one and never renders by itself.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re
import time

from ..common.asset_safety import require_mutable_asset
from ..common.ledger import is_uncertain
from ..common.storage import write_json
from ..qa.capture import _STATE
from . import imaging, model, store
from .runtime import COMMON
from .service import handler

PLAN_SCHEMA = 'production.cinematic_lighting_plan.v1'
REPORT_SCHEMA = 'production.cinematic_lighting_report.v1'
SUMMARY_SCHEMA = 'production.cinematic_lighting_validation.v1'
QA_CATEGORY = 'cinematics-qa'
# The refinement target `exposure_flicker` (review.REFINE_TARGETS) reads metrics['exposure_flicker'] with this unit.
FLICKER_UNITS = 'stops'
FLICKER_DEFINITION = ('max over the validated shots of |log2((mean_b + 1e-4) / (mean_a + 1e-4))|, where mean is the '
                      'frame-mean linear Rec.709 luminance of the EEVEE QA render and a, b are consecutive frames of '
                      'one shot rendered by the same camera (pairs across a cut are skipped)')
SEVERITY_ORDER = {'ERROR': 0, 'WARNING': 1, 'INFO': 2}
MAX_GROUP_FRAMES = 20          # frame references kept per grouped finding in the ledger summary
PI2 = math.pi * math.pi

# Measured, not chosen: see module docstring and docs/cinematic-lighting.md.
ENERGY_MODEL = {
    'quantity': 'response = scene-linear value of a white (albedo 1) diffuse surface facing the light at exposure 0',
    'formulas': {'POINT': 'P[W] = 4*pi^2 * response * d^2', 'SPOT': 'P[W] = 4*pi^2 * response * d^2 (cone does not '
                 'concentrate power)', 'AREA': 'P[W] = pi^2 * response * d^2 (on-axis, emitter small against d)',
                 'SUN': 'S[W/m2] = pi * response', 'WORLD': 'response = color * strength'},
    'measured': {'engine': 'BLENDER_EEVEE 5.2.1', 'sun_per_w_m2': 0.318359, 'point_2m': 0.025234, 'point_4m': 0.024063,
                 'area_small_2m': 0.100937, 'area_small_4m': 0.09625, 'area_1m_at_2m': 0.093125,
                 'world_ratio': 0.992188},
    'source': 'MEASURED: EEVEE calibration render, docs/cinematic-lighting.md',
    'deviation': 'point and area renders read 0.4 % below the formula at 2 m and 5.0 % below at 4 m (measured); a '
                 '1 m area at 2 m reads 8 % below the small-emitter formula',
}

# PROPOSAL: artistic starting points. azimuth: degrees counter-clockwise seen from above, from the subject->camera
# direction (positive = camera right when key_side=RIGHT); elevation: degrees above the horizontal through the aim
# point; distance: multiple of the subject size; response: target white-diffuse response (see ENERGY_MODEL);
# size: AREA edge as a multiple of the subject size.
PRESETS = {
    'NEUTRAL_THREE_POINT': {'exposure_ev': 0.0, 'world': {'color': [0.05, 0.05, 0.05], 'strength': 1.0}, 'lights': [
        {'role': 'KEY', 'type': 'AREA', 'azimuth': 45, 'elevation': 35, 'distance': 1.5, 'response': 1.0, 'size': 0.5},
        {'role': 'FILL', 'type': 'AREA', 'azimuth': -60, 'elevation': 15, 'distance': 1.8, 'response': 0.5, 'size': 0.8},
        {'role': 'RIM', 'type': 'AREA', 'azimuth': 160, 'elevation': 40, 'distance': 1.5, 'response': 0.8, 'size': 0.3}]},
    'HIGH_KEY': {'exposure_ev': 0.0, 'world': {'color': [0.3, 0.3, 0.3], 'strength': 1.0}, 'lights': [
        {'role': 'KEY', 'type': 'AREA', 'azimuth': 30, 'elevation': 30, 'distance': 1.5, 'response': 1.2, 'size': 1.0},
        {'role': 'FILL', 'type': 'AREA', 'azimuth': -45, 'elevation': 10, 'distance': 1.5, 'response': 0.8, 'size': 1.2},
        {'role': 'RIM', 'type': 'AREA', 'azimuth': 170, 'elevation': 35, 'distance': 1.5, 'response': 0.6, 'size': 0.5}]},
    'LOW_KEY': {'exposure_ev': 0.0, 'world': {'color': [0.01, 0.01, 0.01], 'strength': 1.0}, 'lights': [
        {'role': 'KEY', 'type': 'AREA', 'azimuth': 60, 'elevation': 40, 'distance': 1.5, 'response': 1.0, 'size': 0.3},
        {'role': 'FILL', 'type': 'AREA', 'azimuth': -60, 'elevation': 10, 'distance': 2.0, 'response': 0.125, 'size': 0.6},
        {'role': 'RIM', 'type': 'AREA', 'azimuth': 160, 'elevation': 45, 'distance': 1.5, 'response': 0.6, 'size': 0.2}]},
    'RIM_DRAMATIC': {'exposure_ev': 0.0, 'world': {'color': [0.01, 0.01, 0.01], 'strength': 1.0}, 'lights': [
        {'role': 'KEY', 'type': 'AREA', 'azimuth': 75, 'elevation': 30, 'distance': 1.5, 'response': 0.5, 'size': 0.3},
        {'role': 'FILL', 'type': 'AREA', 'azimuth': -60, 'elevation': 10, 'distance': 2.0, 'response': 0.08, 'size': 0.6},
        {'role': 'RIM', 'type': 'AREA', 'azimuth': 150, 'elevation': 30, 'distance': 1.5, 'response': 1.5, 'size': 0.2},
        {'role': 'RIM', 'type': 'AREA', 'azimuth': -150, 'elevation': 30, 'distance': 1.5, 'response': 1.5, 'size': 0.2}]},
    'HORROR_UNDERLIGHT': {'exposure_ev': 0.0, 'world': {'color': [0.005, 0.006, 0.008], 'strength': 1.0}, 'lights': [
        {'role': 'KEY', 'type': 'SPOT', 'azimuth': 10, 'elevation': -35, 'distance': 1.2, 'response': 1.0,
         'spot_size_deg': 60.0, 'spot_blend': 0.4, 'temperature': 3200.0},
        {'role': 'RIM', 'type': 'AREA', 'azimuth': 170, 'elevation': 30, 'distance': 1.5, 'response': 0.4, 'size': 0.2,
         'temperature': 9000.0}]},
    'NIGHT_EXTERIOR': {'exposure_ev': 0.5, 'world': {'color': [0.01, 0.015, 0.03], 'strength': 1.0}, 'lights': [
        {'role': 'KEY', 'type': 'SUN', 'azimuth': 120, 'elevation': 35, 'distance': 3.0, 'response': 0.35,
         'angle_deg': 0.5, 'temperature': 9000.0},
        {'role': 'RIM', 'type': 'AREA', 'azimuth': -160, 'elevation': 30, 'distance': 1.5, 'response': 0.5, 'size': 0.3,
         'temperature': 10000.0}]},
    'PRACTICAL_ONLY': {'exposure_ev': 0.0, 'world': {'color': [0.01, 0.01, 0.01], 'strength': 1.0}, 'lights': []},
}
PRACTICAL_DEFAULTS = {'type': 'POINT', 'response': 0.4, 'temperature': 2700.0, 'shadow_soft_size': 0.05}  # PROPOSAL
MIN_DISTANCE = 1.0          # PROPOSAL: metres; closer lights make the inverse-square plan extremely sensitive
AIM_HEIGHT_FRACTION = 0.7   # PROPOSAL: aim at the upper body of the subject bounds
MIN_SUBJECT_SIZE = 0.3      # PROPOSAL: metres; floor for tiny or degenerate bounds

THRESHOLDS = {  # PROPOSAL: technical flags for review, not quality grades
    'clip_level': 0.99, 'crush_level': 0.01, 'max_clipped_fraction': 0.02, 'max_crushed_fraction': 0.6,
    'max_subject_clipped_fraction': 0.05, 'max_subject_crushed_fraction': 0.25, 'max_flicker_stops': 0.1,
    'min_subject_contrast_stops': 0.5, 'shadow_change_stops': 1.0, 'max_shadow_change_fraction': 0.15,
    'min_subject_coverage_fraction': 0.001,
}
THRESHOLD_BOUNDS = {'max_flicker_stops': 10.0, 'min_subject_contrast_stops': 10.0, 'shadow_change_stops': 10.0}
# Mask colours: components 0/1 are exact through the sRGB transform (measured); the 8th goes through the OETF and is
# matched with a +-2 code tolerance.
PALETTE = ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 1.0, 0.0], [1.0, 0.0, 1.0], [0.0, 1.0, 1.0],
           [1.0, 1.0, 1.0], [1.0, 0.5, 0.0])
DEFAULT_WIDTH = 320          # PROPOSAL: QA resolution width; height follows the candidate aspect
DEFAULT_SAMPLES = 16         # PROPOSAL: EEVEE taa_render_samples for QA
DEFAULT_SAMPLES_PER_SHOT = 3
DEFAULT_MAX_FRAMES = 48
CALL_BUDGET_S = 90.0         # PROPOSAL: half of the 180 s socket timeout per bridge call
MAX_PATH = 240               # Windows MAX_PATH (260) minus margin: longer PNG paths fail
CODE = re.compile(r'^[A-Z][A-Z0-9_]+: ')

PLAN_OPTIONS = {'preset', 'key_subject', 'motivated_sources', 'exposure_ev', 'shot', 'key_side'}
SETUP_OPTIONS = {'plan', 'lights', 'world', 'exposure_ev', 'view_transform', 'look', 'replace'}
VALIDATE_OPTIONS = {'frames', 'shots', 'samples_per_shot', 'flicker_pairs', 'size', 'samples', 'thresholds',
                    'max_frames', 'frames_per_call', 'key_subject'}
LIGHT_KEYS = {'role', 'type', 'location', 'target', 'rotation_euler', 'energy', 'color', 'temperature', 'size', 'size_y',
              'shape', 'spot_size', 'spot_blend', 'angle', 'shadow_soft_size', 'use_shadow'}
LIGHT_INFO_KEYS = {'energy_unit', 'distance_m', 'response', 'azimuth_deg', 'elevation_deg', 'value_source',
                   'provenance', 'source', 'key_side', 'label'}
AREA_SHAPES = ('SQUARE', 'RECTANGLE', 'DISK', 'ELLIPSE')

# ---------------------------------------------------------------------------------------------------------------
# Blender payloads
# ---------------------------------------------------------------------------------------------------------------

RUNTIME = r'''
def cl_vec(value):
    return [float(x) for x in value]

def cl_marker_frame(marker):
    return marker.frame

def cl_subject(params, depsgraph):
    points = []
    used = []
    for name in params['subject_objects']:
        original = bpy.data.objects.get(name)
        if original is None:
            continue
        used.append(name)
        obj = original.evaluated_get(depsgraph)
        mw = obj.matrix_world
        if obj.type == 'ARMATURE':
            if obj.pose is not None:
                for bone in obj.pose.bones:
                    points.append(mw @ bone.head)
                    points.append(mw @ bone.tail)
        elif obj.type in ('MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES', 'POINTCLOUD', 'VOLUME'):
            for corner in obj.bound_box:
                points.append(mw @ mathutils.Vector(corner))
        else:
            points.append(mw.translation.copy())
    if not points:
        cm_fail('LIGHTING_SUBJECT_MISSING', ', '.join(params['subject_objects']))
    low = [min([p[i] for p in points]) for i in range(3)]
    high = [max([p[i] for p in points]) for i in range(3)]
    return {'objects': used, 'min': low, 'max': high, 'center': [(low[i] + high[i]) * 0.5 for i in range(3)],
            'source': 'evaluated candidate depsgraph at its current frame (no frame change)'}

def cl_camera(scene, name, depsgraph):
    obj = None
    source = None
    if name:
        obj = bpy.data.objects.get(name)
        source = 'shot camera'
        if obj is None:
            cm_fail('LIGHTING_CAMERA_MISSING', name)
    elif scene.camera is not None:
        obj = scene.camera
        source = 'candidate scene.camera'
    else:
        for marker in sorted(scene.timeline_markers, key=cl_marker_frame):
            if marker.camera is not None:
                obj = marker.camera
                source = 'first camera marker ' + marker.name
                break
    if obj is None:
        return None
    if obj.type != 'CAMERA':
        cm_fail('LIGHTING_CAMERA_INVALID', obj.name + ' is ' + obj.type)
    mw = obj.evaluated_get(depsgraph).matrix_world
    forward = (mw.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))).normalized()
    return {'object': obj.name, 'source': source, 'location': cl_vec(mw.translation), 'forward': cl_vec(forward),
            'lens': obj.data.lens}

def cl_world_info(world, params):
    if world is None:
        return None
    row = {'name': world.name, 'owned': cm_is_owned(world, params), 'color': None, 'strength': None}
    if world.node_tree is not None:
        for node in world.node_tree.nodes:
            if node.type == 'BACKGROUND':
                row['color'] = cl_vec(node.inputs['Color'].default_value)[:3]
                row['strength'] = float(node.inputs['Strength'].default_value)
                break
    return row

def cl_view_state(view):
    return {'view_transform': view.view_transform, 'look': view.look, 'exposure': view.exposure, 'gamma': view.gamma}

def cl_view_restore(view, state):
    view.view_transform = state['view_transform']
    view.look = state['look']
    view.exposure = state['exposure']
    view.gamma = state['gamma']

def cl_scene_lights(scene, params):
    rows = []
    for obj in sorted(scene.objects, key=cm_sort_name):
        if obj.type == 'LIGHT':
            rows.append({'name': obj.name, 'type': obj.data.type, 'energy': obj.data.energy,
                         'owned': cm_is_owned(obj, params), 'hide_render': obj.hide_render})
    return rows
'''

PLAN_BODY = COMMON + RUNTIME + r'''
def cl_plan_inputs(params):
    scene, src, cin = cm_candidate(params)
    # Measured (5.2.1): an object that lives only in the candidate scene keeps a stale matrix_world (read (0,0,0)
    # for a camera at (0.6,-6,1.4)) until the candidate view layer is evaluated; cm_depsgraph evaluates it at the
    # CURRENT frame, which changes no frame, Action or pose.
    depsgraph = cm_depsgraph(scene)
    return {'subject': cl_subject(params, depsgraph), 'camera': cl_camera(scene, params.get('camera'), depsgraph),
            'scene_lights': cl_scene_lights(scene, params), 'world': cl_world_info(scene.world, params),
            'view': cl_view_state(scene.view_settings), 'unit_scale': scene.unit_settings.scale_length,
            'frame_current': scene.frame_current, 'engine': scene.render.engine}

result = cl_plan_inputs(params)
'''

SETUP_BODY = COMMON + RUNTIME + r'''
def cl_light_apply(obj, data, row):
    data.energy = row['energy']
    data.color = row['color']
    if row['temperature'] is None:
        data.use_temperature = False
    else:
        data.use_temperature = True
        data.temperature = row['temperature']
    data.use_shadow = row['use_shadow']
    if row['type'] in ('POINT', 'SPOT'):
        data.shadow_soft_size = row['shadow_soft_size']
    if row['type'] == 'SPOT':
        data.spot_size = row['spot_size']
        data.spot_blend = row['spot_blend']
    if row['type'] == 'AREA':
        data.shape = row['shape']
        data.size = row['size']
        data.size_y = row['size_y']
    if row['type'] == 'SUN':
        data.angle = row['angle']
    origin = mathutils.Vector(row['location'])
    obj.location = origin
    obj.rotation_mode = 'XYZ'
    if row['target'] is not None:
        direction = mathutils.Vector(row['target']) - origin
        if direction.length < 1e-9:
            cm_fail('LIGHTING_AIM_DEGENERATE', obj.name + ' targets its own position')
        # Lights shine along their local -Z axis.
        obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    else:
        obj.rotation_euler = row['rotation_euler']

def cl_world_apply(world, row):
    background = None
    if world.node_tree is not None:
        for node in world.node_tree.nodes:
            if node.type == 'BACKGROUND':
                background = node
                break
    if background is None:
        cm_fail('LIGHTING_WORLD_NODES_MISSING', world.name)
    background.inputs['Color'].default_value = row['color'] + [1.0]
    background.inputs['Strength'].default_value = row['strength']

def cl_view_apply(view, values):
    if values.get('view_transform') is not None:
        try:
            view.view_transform = values['view_transform']
        except Exception as exc:
            cm_fail('LIGHTING_VIEW_TRANSFORM_INVALID', str(exc)[:300])
    if values.get('look') is not None:
        try:
            view.look = values['look']
        except Exception as exc:
            cm_fail('LIGHTING_LOOK_INVALID', str(exc)[:300])
    if values.get('exposure') is not None:
        view.exposure = values['exposure']

def cl_light_row(obj, params):
    data = obj.data
    return {'name': obj.name, 'role': obj.get('production_m10_light_role'), 'type': data.type, 'energy': data.energy,
            'location': cl_vec(obj.matrix_basis.translation), 'rotation_euler': cl_vec(obj.rotation_euler),
            'color': cl_vec(data.color), 'use_temperature': data.use_temperature, 'temperature': data.temperature,
            'digest': cm_light_digest(data), 'owned': cm_is_owned(obj, params) and cm_is_owned(data, params)}

def cl_setup(params):
    scene, src, cin = cm_candidate(params)
    source = cm_scene(params['source_scene'])
    source_before = cm_source_fingerprint(source)['fingerprint']
    source_world = source.world
    source_world_digest = cm_world_digest(source_world)
    for row in params['lights']:
        cm_free_name(bpy.data.objects, row['name'])
        cm_free_name(bpy.data.lights, row['name'])
    world_row = params.get('world')
    if world_row:
        cm_free_name(bpy.data.worlds, world_row['name'])
    for name in params['remove_lights']:
        obj = bpy.data.objects.get(name)
        if obj is not None and not cm_is_owned(obj, params):
            cm_fail('LIGHTING_NOT_OWNED', name + ' is not owned by this cinematic; nothing is removed')
    if params.get('remove_world'):
        old = bpy.data.worlds.get(params['remove_world'])
        if old is not None and not cm_is_owned(old, params):
            cm_fail('LIGHTING_NOT_OWNED', old.name + ' is not owned by this cinematic')
    view_before = cl_view_state(scene.view_settings)
    world_before = scene.world
    world_before_name = world_before.name if world_before else None
    created = []
    created_data = []
    created_world = None
    try:
        cl_view_apply(scene.view_settings, params['view'])
        for row in params['lights']:
            data = bpy.data.lights.new(row['name'], row['type'])
            created_data.append(data)
            if data.name != row['name']:
                cm_fail('CINEMATIC_NAME_COLLISION', row['name'])
            obj = cm_new_object(params, row['name'], data, cin, 'light_' + row['role'].lower())
            created.append(obj)
            obj['production_m10_light_role'] = row['role']
            cl_light_apply(obj, data, row)
        if world_row:
            world = bpy.data.worlds.new(world_row['name'])
            created_world = world
            if world.name != world_row['name']:
                cm_fail('CINEMATIC_NAME_COLLISION', world_row['name'])
            cm_mark(world, params, 'world')
            cl_world_apply(world, world_row)
            scene.world = world
    except Exception:
        # Leave nothing half-made: the candidate fingerprint must match the ledger after a refused setup.
        for obj in created:
            bpy.data.objects.remove(obj)
        for data in created_data:
            if data.users == 0:
                bpy.data.lights.remove(data)
        scene.world = world_before
        if created_world is not None:
            bpy.data.worlds.remove(created_world)
        cl_view_restore(scene.view_settings, view_before)
        raise
    removed = {'lights': [], 'worlds': []}
    for name in params['remove_lights']:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        data = obj.data
        bpy.data.objects.remove(obj)
        removed['lights'].append(name)
        if data is not None and data.users == 0 and cm_is_owned(data, params):
            bpy.data.lights.remove(data)
    if params.get('remove_world'):
        old = bpy.data.worlds.get(params['remove_world'])
        if old is not None and len([s for s in bpy.data.scenes if s.world == old]) == 0:
            removed['worlds'].append(old.name)
            bpy.data.worlds.remove(old)
    if source.world != source_world or cm_world_digest(source.world) != source_world_digest:
        cm_fail('LIGHTING_SOURCE_WORLD_CHANGED', 'the source world changed during setup')
    if cm_source_fingerprint(source)['fingerprint'] != source_before:
        cm_fail('CINEMATIC_SOURCE_CHANGED_BY_CANDIDATE', 'lighting setup changed the source scene')
    owned = {'objects': [o.name for o in created], 'data': [['LIGHT', o.data.name] for o in created], 'worlds': []}
    if created_world is not None:
        owned['worlds'].append(created_world.name)
    return {'lights': [cl_light_row(o, params) for o in created], 'world': cl_world_info(scene.world, params),
            'source_world': cl_world_info(source.world, params), 'view': cl_view_state(scene.view_settings),
            'view_before': view_before, 'world_before': world_before_name, 'removed': removed, 'owned': owned, 'touched': [],
            'fingerprints': cm_fingerprints(params)}

result = cl_setup(params)
'''

PREFLIGHT_BODY = COMMON + RUNTIME + r'''
def cl_preflight(params):
    scene, src, cin = cm_candidate(params)
    markers = [[m.name, m.frame, m.camera.name if m.camera else None] for m in scene.timeline_markers]
    cameras = [row for row in markers if row[2]]
    render = scene.render
    return {'scene': scene.name, 'camera': scene.camera.name if scene.camera else None, 'camera_markers': len(cameras),
            'has_camera': scene.camera is not None or len(cameras) > 0,
            'resolution': [render.resolution_x, render.resolution_y], 'engine': render.engine,
            'view': cl_view_state(scene.view_settings), 'frame_range': [scene.frame_start, scene.frame_end],
            'missing_objects': [n for n in params['actor_objects'] if bpy.data.objects.get(n) is None],
            'scene_lights': cl_scene_lights(scene, params), 'world': cl_world_info(scene.world, params)}

result = cl_preflight(params)
'''

RENDER_BODY = COMMON + _STATE + RUNTIME + r'''
def cl_settings_state(scene):
    render = scene.render
    view = scene.view_settings
    shading = scene.display.shading
    return {'engine': render.engine, 'film_transparent': render.film_transparent, 'dither': render.dither_intensity,
            'motion_blur': render.use_motion_blur, 'freestyle': render.use_freestyle,
            'taa_render_samples': scene.eevee.taa_render_samples, 'render_aa': scene.display.render_aa,
            'view': cl_view_state(view), 'curve_mapping': view.use_curve_mapping,
            'white_balance': view.use_white_balance, 'display_device': scene.display_settings.display_device,
            'shading': {'light': shading.light, 'color_type': shading.color_type,
                        'background_type': shading.background_type, 'background_color': cl_vec(shading.background_color),
                        'show_shadows': shading.show_shadows, 'show_cavity': shading.show_cavity,
                        'show_object_outline': shading.show_object_outline,
                        'show_specular_highlight': shading.show_specular_highlight, 'show_xray': shading.show_xray,
                        'use_dof': shading.use_dof, 'show_backface_culling': shading.show_backface_culling}}

def cl_settings_restore(scene, state):
    render = scene.render
    render.engine = state['engine']
    render.film_transparent = state['film_transparent']
    render.dither_intensity = state['dither']
    render.use_motion_blur = state['motion_blur']
    render.use_freestyle = state['freestyle']
    scene.eevee.taa_render_samples = state['taa_render_samples']
    scene.display.render_aa = state['render_aa']
    scene.display_settings.display_device = state['display_device']
    cl_view_restore(scene.view_settings, state['view'])
    scene.view_settings.use_curve_mapping = state['curve_mapping']
    scene.view_settings.use_white_balance = state['white_balance']
    shading = scene.display.shading
    values = state['shading']
    shading.light = values['light']
    shading.color_type = values['color_type']
    shading.background_type = values['background_type']
    shading.background_color = values['background_color']
    shading.show_shadows = values['show_shadows']
    shading.show_cavity = values['show_cavity']
    shading.show_object_outline = values['show_object_outline']
    shading.show_specular_highlight = values['show_specular_highlight']
    shading.show_xray = values['show_xray']
    shading.use_dof = values['use_dof']
    shading.show_backface_culling = values['show_backface_culling']

def cl_mask_configure(scene):
    # Measured (Blender 5.2.1): only FLAT + OBJECT colour + Standard + AA OFF + dither 0 writes exact flat colours;
    # AgX, AA 8 or dither 1 produce blended codes that would misclassify pixels.
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'FLAT'
    shading.color_type = 'OBJECT'
    shading.background_type = 'VIEWPORT'
    shading.background_color = (0.0, 0.0, 0.0)
    shading.show_shadows = False
    shading.show_cavity = False
    shading.show_object_outline = False
    shading.show_specular_highlight = False
    shading.show_xray = False
    shading.use_dof = False
    shading.show_backface_culling = False
    scene.display.render_aa = 'OFF'
    scene.render.dither_intensity = 0.0
    scene.render.use_motion_blur = False
    scene.render.use_freestyle = False
    scene.display_settings.display_device = 'sRGB'
    view = scene.view_settings
    view.view_transform = 'Standard'
    view.look = 'None'
    view.exposure = 0.0
    view.gamma = 1.0
    view.use_curve_mapping = False
    view.use_white_balance = False

def cl_render(params):
    scene, src, cin = cm_candidate(params)
    source = cm_scene(params['source_scene'])
    candidate_before = cm_candidate_fingerprint(scene, src, cin)['fingerprint']
    source_before = cm_source_fingerprint(source)['fingerprint']
    actors = [bpy.data.objects.get(n) for n in params['actor_objects']]
    actors = [a for a in actors if a is not None]
    camera_before = scene.camera
    qa_before = qa_state(scene)
    settings_before = cl_settings_state(scene)
    colors_before = {}
    files = []
    errors = []
    backend = None
    width = int(params['width'])
    height = max(16, int(round(width * scene.render.resolution_y / scene.render.resolution_x)))
    with bpy.context.temp_override(scene=scene):
        channels = cm_capture(scene, actors)
    try:
        qa_configure(scene, width, height)
        scene.render.image_settings.color_mode = 'RGB'
        scene.render.film_transparent = False
        if params['mode'] == 'EEVEE':
            scene.render.engine = 'BLENDER_EEVEE'
            scene.eevee.taa_render_samples = params['samples']
        else:
            cl_mask_configure(scene)
            for obj in scene.objects:
                colors_before[obj.name] = cl_vec(obj.color)
            for obj in scene.objects:
                obj.color = params['palette'].get(obj.name, [0.0, 0.0, 0.0, 1.0])
        backend = qa_render_backend(scene)
        if not str(backend.get('actual_backend')).startswith('GPU'):
            cm_fail('LIGHTING_GPU_REQUIRED', 'render backend is ' + str(backend.get('actual_backend')))
        for row in params['frames']:
            with bpy.context.temp_override(scene=scene):
                scene.frame_set(row['frame'])
            if scene.camera is None:
                cm_fail('LIGHTING_NO_CAMERA', 'no camera at frame ' + str(row['frame']))
            scene.render.filepath = row['path']
            # Measured (Blender 5.2.1, background): render(scene=name) renders the WINDOW scene's frame and moves
            # the candidate's frame_current there; inside temp_override(scene=candidate) the render honours the
            # candidate frame and its camera markers.
            with bpy.context.temp_override(scene=scene):
                outcome = bpy.ops.render.render(write_still=True)
            if 'FINISHED' not in outcome:
                cm_fail('LIGHTING_RENDER_FAILED', str(outcome))
            files.append({'frame': row['frame'], 'path': row['path'], 'camera': scene.camera.name})
    except Exception as exc:
        errors.append(str(exc)[:500])
    finally:
        for name, color in colors_before.items():
            obj = bpy.data.objects.get(name)
            if obj is not None:
                obj.color = color
        cl_settings_restore(scene, settings_before)
        with bpy.context.temp_override(scene=scene):
            qa_restore(scene, qa_before, camera_before)
            cm_restore(scene, actors, channels)
        # frame_set and render follow camera markers; the camera that was active comes back last.
        scene.camera = camera_before
    colors_ok = True
    for name, color in colors_before.items():
        obj = bpy.data.objects.get(name)
        if obj is not None and cl_vec(obj.color) != color:
            colors_ok = False
    checks = {'candidate_fingerprint': cm_candidate_fingerprint(scene, src, cin)['fingerprint'] == candidate_before,
              'source_fingerprint': cm_source_fingerprint(source)['fingerprint'] == source_before,
              'render_settings': qa_state(scene) == qa_before, 'engine_view_shading': cl_settings_state(scene) == settings_before,
              'object_colors': colors_ok, 'camera': scene.camera == camera_before}
    return {'mode': params['mode'], 'files': files, 'errors': errors, 'backend': backend, 'width': width,
            'height': height, 'restored': all(checks.values()), 'restore_checks': checks}

result = cl_render(params)
'''


# ---------------------------------------------------------------------------------------------------------------
# Pure planning (host)
# ---------------------------------------------------------------------------------------------------------------

def energy_for(kind, response, distance):
    """Energy (W, or W/m2 for SUN) that gives `response` at `distance` (ENERGY_MODEL; measured relations)."""
    if kind in ('POINT', 'SPOT'):
        return 4.0 * PI2 * response * distance * distance
    if kind == 'AREA':
        return PI2 * response * distance * distance
    if kind == 'SUN':
        return math.pi * response
    model.fail('CINEMATIC_CHOICE_INVALID', 'light type ' + str(kind))


def response_for(kind, energy, distance):
    if kind == 'SUN':
        return energy / math.pi
    factor = 4.0 * PI2 if kind in ('POINT', 'SPOT') else PI2
    return energy / (factor * distance * distance)


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _length(v):
    return math.sqrt(sum(x * x for x in v))


def _rotate_z(v, degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return [c * v[0] - s * v[1], s * v[0] + c * v[1], 0.0]


def _round(values, digits=6):
    return [round(float(v), digits) for v in values]


def camera_axis(subject_center, camera):
    """Horizontal unit vector from the subject toward the camera, and where it came from."""
    if camera:
        toward = _sub(camera['location'], subject_center)
        toward[2] = 0.0
        if _length(toward) > 1e-6:
            n = _length(toward)
            return [toward[0] / n, toward[1] / n, 0.0], 'camera position'
        back = [-camera['forward'][0], -camera['forward'][1], 0.0]
        if _length(back) > 1e-6:
            n = _length(back)
            return [back[0] / n, back[1] / n, 0.0], 'camera forward (camera above the subject)'
    # Blender's front view looks along +Y: without a camera, the audience is assumed at -Y.
    return [0.0, -1.0, 0.0], 'ASSUMED front view (-Y): no camera in the candidate'


def _light_row(spec, position, target, distance, side, extra=None):
    kind = spec['type']
    energy = spec.get('energy')
    if energy is None:
        energy = energy_for(kind, spec['response'], distance)
    response = response_for(kind, energy, distance)
    row = {'role': spec['role'], 'type': kind, 'location': _round(position), 'target': _round(target),
           'energy': round(energy, 4), 'energy_unit': 'W/m2' if kind == 'SUN' else 'W',
           'color': list(spec.get('color') or [1.0, 1.0, 1.0]), 'temperature': spec.get('temperature'),
           'use_shadow': True, 'distance_m': round(distance, 4), 'response': round(response, 6),
           'value_source': 'PROPOSAL', 'provenance': 'PROPOSED_CREATIVE_DECISION'}
    row.update(extra or {})
    if kind == 'AREA':
        row.update(shape='SQUARE', size=round(spec['size_m'], 4), size_y=round(spec['size_m'], 4))
    elif kind == 'SPOT':
        row.update(spot_size=round(math.radians(spec.get('spot_size_deg', 45.0)), 6),
                   spot_blend=spec.get('spot_blend', 0.15), shadow_soft_size=round(spec.get('shadow_soft_size', 0.05), 4))
    elif kind == 'POINT':
        row.update(shadow_soft_size=round(spec.get('shadow_soft_size', 0.05), 4))
    elif kind == 'SUN':
        row.update(angle=round(math.radians(spec.get('angle_deg', 0.526)), 6))
    if side is not None:
        row['key_side'] = side
    return row


def _motivated(rows, target):
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > model.MAX_LIGHTS:
        model.fail('LIGHTING_MOTIVATED_SOURCES_INVALID', 'a list of at most ' + str(model.MAX_LIGHTS) + ' sources')
    out = []
    for index, raw in enumerate(rows):
        field = 'motivated_sources[' + str(index) + ']'
        if not isinstance(raw, dict):
            model.fail('LIGHTING_MOTIVATED_SOURCES_INVALID', field + ' must be an object')
        raw = model.closed(raw, {'position', 'type', 'response', 'energy', 'color', 'temperature', 'label'},
                           code='LIGHTING_MOTIVATED_SOURCES_INVALID')
        position = model.vector(raw.get('position'), field + '.position')
        kind = model.choice(raw.get('type', PRACTICAL_DEFAULTS['type']), field + '.type', ('POINT', 'SPOT', 'AREA'))
        distance = _length(_sub(target, position))
        if distance < 0.05:
            model.fail('LIGHTING_MOTIVATED_SOURCES_INVALID', field + ' is on the subject aim point')
        spec = {'role': 'PRACTICAL', 'type': kind,
                'response': model.number(raw.get('response', PRACTICAL_DEFAULTS['response']), field + '.response', 0, 100),
                'temperature': (None if raw.get('color') is not None and raw.get('temperature') is None else
                                model.number(raw.get('temperature', PRACTICAL_DEFAULTS['temperature']),
                                             field + '.temperature', 800, 20000)),
                'color': model.vector(raw.get('color', [1.0, 1.0, 1.0]), field + '.color', 3, 0.0, 1.0),
                'shadow_soft_size': PRACTICAL_DEFAULTS['shadow_soft_size'], 'size_m': 0.2}
        if raw.get('energy') is not None:
            spec['energy'] = model.number(raw['energy'], field + '.energy', 0, 1e6)
        extra = {'source': 'motivated'}
        if raw.get('label') is not None:
            extra['label'] = model.text(raw['label'], field + '.label', 64)
        out.append(_light_row(spec, position, target, distance, None, extra))
    return out


def compose_plan(preset, inputs, opts, generation_id, revision, key_actor):
    """Lighting plan from measured scene inputs. Pure: identical inputs give the identical plan."""
    preset = model.choice(preset, 'options.preset', model.MOOD_PRESETS)
    side = model.choice(opts.get('key_side', 'RIGHT'), 'options.key_side', ('LEFT', 'RIGHT'))
    sign = 1.0 if side == 'RIGHT' else -1.0
    table = PRESETS[preset]
    subject = inputs['subject']
    low, high = subject['min'], subject['max']
    dims = [high[i] - low[i] for i in range(3)]
    size = max(max(dims), MIN_SUBJECT_SIZE)
    center = [(low[i] + high[i]) * 0.5 for i in range(3)]
    target = [center[0], center[1], low[2] + AIM_HEIGHT_FRACTION * dims[2]]
    axis, axis_source = camera_axis(center, inputs.get('camera'))
    warnings = []
    if not inputs.get('camera'):
        warnings.append({'code': 'LIGHTING_NO_CAMERA_AXIS', 'message': axis_source})
    lights = []
    for spec in table['lights']:
        distance = max(MIN_DISTANCE, spec['distance'] * size)
        azimuth = spec['azimuth'] * sign
        horizontal = _rotate_z(axis, azimuth)
        el = math.radians(spec['elevation'])
        direction = [math.cos(el) * horizontal[0], math.cos(el) * horizontal[1], math.sin(el)]
        position = [target[i] + distance * direction[i] for i in range(3)]
        row_spec = dict(spec, size_m=spec.get('size', 0.5) * size)
        lights.append(_light_row(row_spec, position, target, distance, side,
                                 {'azimuth_deg': azimuth, 'elevation_deg': spec['elevation']}))
    lights += _motivated(opts.get('motivated_sources'), target)
    if preset == 'PRACTICAL_ONLY' and not lights:
        model.fail('LIGHTING_PRACTICAL_SOURCES_REQUIRED', 'PRACTICAL_ONLY needs options.motivated_sources')
    if len(lights) > model.MAX_LIGHTS:
        model.fail('CINEMATIC_CAPACITY', 'at most ' + str(model.MAX_LIGHTS) + ' lights')
    exposure = table['exposure_ev'] if opts.get('exposure_ev') is None else model.number(
        opts['exposure_ev'], 'options.exposure_ev', -10, 10)
    responses = {row['role']: row['response'] for row in reversed(lights)}
    ratio = None
    if responses.get('KEY') and responses.get('FILL'):
        ratio = round(responses['KEY'] / responses['FILL'], 4)
    source_lights = [row for row in inputs.get('scene_lights') or [] if not row.get('owned')]
    if source_lights:
        warnings.append({'code': 'SOURCE_LIGHTS_IN_CANDIDATE',
                         'message': str(len(source_lights)) + ' source lights are linked into the candidate and add '
                         'to this plan; exclude them at candidate creation (exclude_objects) if the plan must be the '
                         'only lighting. They are never edited.'})
    world = dict(table['world'], response=round(table['world']['strength'] * max(table['world']['color']), 6),
                 value_source='PROPOSAL')
    view = inputs.get('view') or {}
    return {'schema': PLAN_SCHEMA, 'generation_id': generation_id, 'candidate_revision': revision,
            'preset': preset, 'key_subject': key_actor['actor_id'], 'key_side': side,
            'subject': {'objects': subject.get('objects'), 'center': _round(center), 'aim_point': _round(target),
                        'dimensions_m': _round(dims), 'size_m': round(size, 6), 'source': subject.get('source')},
            'camera': inputs.get('camera'), 'camera_axis': {'vector': _round(axis), 'source': axis_source},
            'lights': lights, 'world': world,
            'exposure': {'exposure_ev': exposure, 'view_transform': view.get('view_transform'), 'look': view.get('look'),
                         'planned_key_response': (round(responses['KEY'] * 2.0 ** exposure, 6)
                                                  if responses.get('KEY') else None),
                         'value_source': 'PROPOSAL'},
            'key_to_fill_response_ratio': ratio, 'energy_model': ENERGY_MODEL,
            'conventions': {'azimuth': 'degrees counter-clockwise seen from above from the subject->camera direction; '
                                       'positive = camera right for key_side RIGHT',
                            'elevation': 'degrees above the horizontal through the aim point',
                            'distance': 'multiple of subject size, floor ' + str(MIN_DISTANCE) + ' m (PROPOSAL)'},
            'scene_lights': inputs.get('scene_lights') or [], 'current_world': inputs.get('world'),
            'note': 'Artistic starting point, not a universal formula. Validate with cinematic_validate_lighting and a '
                    'real visual review; adjust per shot.',
            'warnings': warnings}


# ---------------------------------------------------------------------------------------------------------------
# Setup normalisation (host)
# ---------------------------------------------------------------------------------------------------------------

def _boolean(value, field, default):
    if value is None:
        return default
    if not isinstance(value, bool):
        model.fail('CINEMATIC_BOOLEAN_REQUIRED', field + ' must be true or false')
    return value


def normalize_light(raw, index):
    field = 'lights[' + str(index) + ']'
    if not isinstance(raw, dict):
        model.fail('LIGHTING_LIGHT_INVALID', field + ' must be an object')
    raw = model.closed(raw, LIGHT_KEYS | LIGHT_INFO_KEYS, code='LIGHTING_LIGHT_OPTION_UNSUPPORTED')
    kind = model.choice(raw.get('type', 'AREA'), field + '.type', model.LIGHT_TYPES)
    row = {'role': model.choice(raw.get('role'), field + '.role', model.LIGHT_ROLES), 'type': kind,
           'location': model.vector(raw.get('location'), field + '.location')}
    has_target, has_rotation = raw.get('target') is not None, raw.get('rotation_euler') is not None
    if has_target == has_rotation:
        model.fail('LIGHTING_AIM_REQUIRED', field + ' needs exactly one of target or rotation_euler')
    row['target'] = model.vector(raw['target'], field + '.target') if has_target else None
    row['rotation_euler'] = model.vector(raw['rotation_euler'], field + '.rotation_euler', 3, -100, 100) if has_rotation else None
    # Measured soft maximum of Light.energy is 1e6; SUN is irradiance (W/m2).
    row['energy'] = model.number(raw.get('energy'), field + '.energy', 0, 1000 if kind == 'SUN' else 1e6)
    row['color'] = model.vector(raw.get('color', [1.0, 1.0, 1.0]), field + '.color', 3, 0.0, 1.0)
    # Measured hard range of Light.temperature: 800..20000 K.
    row['temperature'] = None if raw.get('temperature') is None else model.number(
        raw['temperature'], field + '.temperature', 800, 20000)
    row['use_shadow'] = _boolean(raw.get('use_shadow'), field + '.use_shadow', True)
    row['shadow_soft_size'] = model.number(raw.get('shadow_soft_size', 0.05), field + '.shadow_soft_size', 0, 100)
    row['spot_size'] = model.number(raw.get('spot_size', math.radians(45.0)), field + '.spot_size',
                                    math.radians(1.0), math.pi)
    row['spot_blend'] = model.number(raw.get('spot_blend', 0.15), field + '.spot_blend', 0, 1)
    row['size'] = model.number(raw.get('size', 0.25), field + '.size', 0.001, 100)
    row['size_y'] = model.number(raw.get('size_y', row['size']), field + '.size_y', 0.001, 100)
    row['shape'] = model.choice(raw.get('shape', 'SQUARE' if raw.get('size_y') is None else 'RECTANGLE'),
                                field + '.shape', AREA_SHAPES)
    row['angle'] = model.number(raw.get('angle', 0.009180432), field + '.angle', 0, math.pi)
    row['info'] = {k: raw[k] for k in sorted(LIGHT_INFO_KEYS) if k in raw}
    return row


def name_lights(generation_id, version, lights, revision):
    counts = {}
    for row in lights:
        counts[row['role']] = counts.get(row['role'], 0) + 1
        suffix = 'LGT_' + row['role'] + ('' if counts[row['role']] == 1 else '_' + str(counts[row['role']]))
        if revision > 1:
            suffix += '_r' + str(revision)
        row['name'] = model.owned_name(generation_id, version, suffix)
    return lights


def _world(raw, field):
    if not isinstance(raw, dict):
        model.fail('LIGHTING_WORLD_INVALID', field + ' must be {color, strength}')
    raw = model.closed(raw, {'color', 'strength', 'response', 'value_source'}, code='LIGHTING_WORLD_INVALID')
    return {'color': model.vector(raw.get('color'), field + '.color', 3, 0.0, 1.0),
            'strength': model.number(raw.get('strength'), field + '.strength', 0, 1000)}


def check_look(transform, look):
    """Looks are tied to the view transform; AgX looks carry the 'AgX - ' prefix (measured in 5.2.1)."""
    if look is None or look == 'None':
        return
    if not isinstance(look, str) or not look.strip():
        model.fail('LIGHTING_LOOK_INVALID', 'look must be a name')
    if transform == 'AgX' and not look.startswith('AgX - '):
        model.fail('LIGHTING_LOOK_INVALID', 'AgX looks are named "AgX - <look>", e.g. "AgX - Punchy"; got ' + look)
    if transform != 'AgX' and look.startswith('AgX - '):
        model.fail('LIGHTING_LOOK_INVALID', look + ' belongs to AgX, not ' + str(transform))


def setup_spec(opts, plan, state):
    existing = state.get('lighting') or {}
    replace = _boolean(opts.get('replace'), 'options.replace', False)
    lighting_plan = None
    lights_raw = None
    world = None
    exposure = None
    if opts.get('plan') is not None and opts.get('lights') is not None:
        model.fail('LIGHTING_INPUT_AMBIGUOUS', 'pass either options.plan or options.lights, not both')
    if opts.get('plan') is not None:
        lighting_plan = opts['plan']
        if not isinstance(lighting_plan, dict) or lighting_plan.get('schema') != PLAN_SCHEMA:
            model.fail('LIGHTING_PLAN_INVALID', 'pass the plan returned by cinematic_plan_lighting (' + PLAN_SCHEMA + ')')
        if lighting_plan.get('generation_id') != plan['generation_id']:
            model.fail('LIGHTING_PLAN_MISMATCH', 'the lighting plan belongs to another cinematic')
        lights_raw = lighting_plan.get('lights') or []
        world = _world(lighting_plan['world'], 'plan.world') if lighting_plan.get('world') else None
        exposure = (lighting_plan.get('exposure') or {}).get('exposure_ev')
    elif opts.get('lights') is not None:
        lights_raw = opts['lights']
        if not isinstance(lights_raw, list):
            model.fail('LIGHTING_LIGHT_INVALID', 'options.lights must be a list')
    if opts.get('world') is not None:
        world = _world(opts['world'], 'options.world')
    if opts.get('exposure_ev') is not None:
        exposure = opts['exposure_ev']
    if exposure is not None:
        exposure = model.number(exposure, 'exposure_ev', -10, 10)
    lights = [normalize_light(raw, i) for i, raw in enumerate(lights_raw or [])]
    if len(lights) > model.MAX_LIGHTS:
        model.fail('CINEMATIC_CAPACITY', 'at most ' + str(model.MAX_LIGHTS) + ' lights')
    transform = opts.get('view_transform')
    if transform is not None:
        transform = model.name(transform, 'options.view_transform')
    current = (existing.get('exposure') or {}).get('view_transform') or \
        ((plan.get('source') or {}).get('settings') or {}).get('view', {}).get('transform')
    check_look(transform or current, opts.get('look'))
    view = {'view_transform': transform, 'look': opts.get('look'), 'exposure': exposure}
    if not lights and world is None and all(v is None for v in view.values()):
        model.fail('LIGHTING_NOTHING_TO_SET', 'pass a plan, lights, world, exposure_ev, view_transform or look')
    remove_lights = []
    remove_world = None
    if lights and existing.get('lights'):
        if not replace:
            model.fail('LIGHTING_ALREADY_SET', 'owned lights exist; pass replace=true to swap them')
        remove_lights = sorted(existing['lights'])
    existing_world = existing.get('world') or {}
    if world is not None and existing_world.get('owned'):
        if not replace:
            model.fail('LIGHTING_ALREADY_SET', 'an owned world exists; pass replace=true to swap it')
        remove_world = existing_world.get('name')
    revision = int(existing.get('setup_revision', 0)) + 1
    version = (state.get('assembly') or {}).get('version', 1)
    name_lights(plan['generation_id'], version, lights, revision)
    if world is not None:
        world['name'] = model.owned_name(plan['generation_id'], version, 'WORLD' + ('' if revision == 1 else '_r' + str(revision)))
    warnings = []
    if lighting_plan is not None and lighting_plan.get('candidate_revision') != state.get('revision'):
        warnings.append({'code': 'LIGHTING_PLAN_OLDER_THAN_CANDIDATE',
                         'message': 'the plan was computed at candidate revision ' + str(lighting_plan.get('candidate_revision'))
                         + ', the candidate is at ' + str(state.get('revision')) + '; positions may be stale'})
    return {'lights': lights, 'world': world, 'view': view, 'replace': replace, 'remove_lights': remove_lights,
            'remove_world': remove_world, 'revision': revision, 'plan': lighting_plan, 'warnings': warnings}


def _blender_light(row):
    return {k: row[k] for k in ('name', 'role', 'type', 'location', 'target', 'rotation_euler', 'energy', 'color',
                                'temperature', 'use_shadow', 'shadow_soft_size', 'spot_size', 'spot_blend', 'size',
                                'size_y', 'shape', 'angle')}


# ---------------------------------------------------------------------------------------------------------------
# Validation: frame selection and measurement (host)
# ---------------------------------------------------------------------------------------------------------------

def shot_rows(plan, state):
    rows = []
    for index, row in enumerate(state.get('shots') or plan.get('shots') or []):
        start = model.number(row.get('frame_start'), 'shot.frame_start', integer=True)
        end = model.number(row.get('frame_end'), 'shot.frame_end', integer=True)
        rows.append({'shot_id': str(row.get('shot_id') or row.get('id') or 'SHOT_' + str(index + 1)),
                     'frame_start': start, 'frame_end': end, 'camera_id': row.get('camera_id')})
    return rows


def thresholds_from(raw):
    values = dict(THRESHOLDS)
    if raw is None:
        return values
    if not isinstance(raw, dict):
        model.fail('LIGHTING_THRESHOLDS_INVALID', 'options.thresholds must be an object')
    raw = model.closed(raw, set(THRESHOLDS), code='LIGHTING_THRESHOLDS_INVALID')
    for key, value in raw.items():
        values[key] = model.number(value, 'thresholds.' + key, 0, THRESHOLD_BOUNDS.get(key, 1.0))
    return values


def select_frames(opts, plan, state):
    timeline = state.get('timeline') or plan['timeline']
    first, last = timeline['frame_start'], timeline['frame_end']
    shots = shot_rows(plan, state)
    wanted = opts.get('shots')
    if wanted is not None:
        if not isinstance(wanted, list) or not wanted:
            model.fail('LIGHTING_SHOTS_INVALID', 'options.shots must be a non-empty list of shot ids')
        known = {row['shot_id'] for row in shots}
        unknown = sorted(str(s) for s in wanted if s not in known)
        if unknown:
            model.fail('LIGHTING_SHOT_UNKNOWN', ', '.join(unknown))
        shots = [row for row in shots if row['shot_id'] in wanted]
    if not shots:
        shots = [{'shot_id': 'TIMELINE', 'frame_start': first, 'frame_end': last, 'camera_id': None}]
    pairs_on = _boolean(opts.get('flicker_pairs'), 'options.flicker_pairs', True)
    samples = model.number(opts.get('samples_per_shot', DEFAULT_SAMPLES_PER_SHOT), 'options.samples_per_shot', 1, 32,
                           integer=True)
    sampled = {}
    if opts.get('frames') is not None:
        if opts.get('shots') is not None:
            model.fail('LIGHTING_INPUT_AMBIGUOUS', 'pass options.frames or options.shots, not both')
        frames = opts['frames']
        if not isinstance(frames, list) or not frames:
            model.fail('LIGHTING_FRAMES_INVALID', 'options.frames must be a non-empty list')
        for value in frames:
            frame = model.number(value, 'options.frames[]', first, last, integer=True)
            owner = next((row for row in shots if row['frame_start'] <= frame <= row['frame_end']), None)
            shot_id = owner['shot_id'] if owner else 'TIMELINE'
            sampled.setdefault(shot_id, set()).add(frame)
    else:
        for row in shots:
            start, end = max(row['frame_start'], first), min(row['frame_end'], last)
            if start > end:
                continue
            if samples == 1 or start == end:
                picks = {(start + end) // 2}
            else:
                # Half-up rounding, the same rule as model.seconds_to_frame (Python's round() is half-to-even).
                picks = {start + int(math.floor(i * (end - start) / (samples - 1) + 0.5)) for i in range(samples)}
            sampled[row['shot_id']] = picks
    bounds = {row['shot_id']: (max(row['frame_start'], first), min(row['frame_end'], last)) for row in shots}
    bounds.setdefault('TIMELINE', (first, last))
    shot_of = {}
    pairs = []
    for shot_id, picks in sorted(sampled.items()):
        start, end = bounds[shot_id]
        for frame in sorted(picks):
            shot_of.setdefault(frame, shot_id)
            if pairs_on:
                mate = frame + 1 if frame + 1 <= end else frame - 1
                if start <= mate <= end and mate != frame:
                    pair = (min(frame, mate), max(frame, mate), shot_id)
                    if pair not in pairs:
                        pairs.append(pair)
                    shot_of.setdefault(mate, shot_id)
    limit = model.number(opts.get('max_frames', DEFAULT_MAX_FRAMES), 'options.max_frames', 1, 240, integer=True)
    if len(shot_of) > limit:
        model.fail('LIGHTING_CAPACITY', str(len(shot_of)) + ' frames requested, max_frames is ' + str(limit))
    return {'frames': sorted(shot_of), 'shot_of': shot_of, 'pairs': sorted(pairs),
            'samples': {k: sorted(v) for k, v in sampled.items()}, 'flicker_pairs': pairs_on}


def _finding(code, severity, value, threshold_key, thresholds, **where):
    row = {'code': code, 'severity': severity, 'kind': 'TECHNICAL', 'value': value}
    if threshold_key is not None:
        row.update(threshold=thresholds[threshold_key], threshold_name=threshold_key, threshold_source='PROPOSAL')
    row.update(where)
    return row


def measure_frame(eevee, mask, actors, thresholds):
    """Metrics of one rendered frame. `actors` = [(actor_id, palette_index)]. Returns (row, luminance, labels)."""
    if (eevee.width, eevee.height) != (mask.width, mask.height):
        model.fail('LIGHTING_MASK_SIZE_MISMATCH', 'EEVEE and mask renders differ in size')
    clip, crush = thresholds['clip_level'], thresholds['crush_level']
    values = imaging.luminance(eevee)
    labels, counts = imaging.label_mask(mask, PALETTE[:max([i for _, i in actors], default=-1) + 1])
    total = eevee.width * eevee.height
    background = [i for i, label in enumerate(labels) if label == -1]
    frame = imaging.stats(values, clip=clip, crush=crush)
    row = {'luminance': frame, 'channel_clipped_fraction': round(imaging.channel_clipped_fraction(eevee), 6),
           'background': imaging.stats(values, background, clip, crush),
           'mask_unmatched_fraction': round(counts.get(-2, 0) / total, 6), 'actors': {}}
    for actor_id, index in actors:
        selected = [i for i, label in enumerate(labels) if label == index]
        subject = imaging.stats(values, selected, clip, crush)
        contrast = None
        if subject['pixels'] and row['background']['pixels']:
            contrast = round(imaging.stops(subject['mean'], row['background']['mean']), 4)
        row['actors'][actor_id] = {'coverage_fraction': round(len(selected) / total, 6), 'luminance': subject,
                                   'contrast_stops_vs_background': contrast}
    return row, values, labels


def measure_pair(a, b, actors, thresholds):
    """Adjacent-frame comparison: frame-mean change (flicker) and, inside the actor mask present in both frames,
    the fraction of pixels whose luminance changed by more than `shadow_change_stops` (shadow-jump proxy)."""
    (row_a, values_a, labels_a), (row_b, values_b, labels_b) = a, b
    out = {'mean_change_stops': round(imaging.stops(row_b['luminance']['mean'], row_a['luminance']['mean']), 6),
           'actors': {}}
    limit = thresholds['shadow_change_stops']
    for actor_id, index in actors:
        overlap = [i for i in range(len(labels_a)) if labels_a[i] == index and labels_b[i] == index]
        changed = sum(1 for i in overlap if abs(imaging.stops(values_b[i], values_a[i])) > limit)
        subject_a = row_a['actors'][actor_id]['luminance']['mean']
        subject_b = row_b['actors'][actor_id]['luminance']['mean']
        out['actors'][actor_id] = {
            'overlap_pixels': len(overlap),
            'changed_fraction': round(changed / len(overlap), 6) if overlap else None,
            'subject_mean_change_stops': (round(imaging.stops(subject_b, subject_a), 6)
                                          if subject_a is not None and subject_b is not None else None)}
    return out


def findings_for(frames, pairs, actors, key_actor, thresholds):
    out = []
    for frame, row in sorted(frames.items()):
        where = {'frame': frame, 'shot_id': row['shot_id']}
        lum = row['luminance']
        if lum['clipped_fraction'] > thresholds['max_clipped_fraction']:
            out.append(_finding('LIGHTING_CLIPPED', 'WARNING', lum['clipped_fraction'], 'max_clipped_fraction', thresholds, **where))
        if lum['crushed_fraction'] > thresholds['max_crushed_fraction']:
            out.append(_finding('LIGHTING_CRUSHED', 'WARNING', lum['crushed_fraction'], 'max_crushed_fraction', thresholds, **where))
        if row['mask_unmatched_fraction'] > 0:
            out.append(_finding('LIGHTING_MASK_INEXACT', 'ERROR', row['mask_unmatched_fraction'], None, thresholds, **where))
        for actor_id, _ in actors:
            subject = row['actors'][actor_id]
            here = dict(where, actor_id=actor_id)
            if subject['coverage_fraction'] < thresholds['min_subject_coverage_fraction']:
                out.append(_finding('LIGHTING_SUBJECT_NOT_VISIBLE', 'INFO', subject['coverage_fraction'],
                                    'min_subject_coverage_fraction', thresholds, **here))
                continue
            sl = subject['luminance']
            if sl['clipped_fraction'] > thresholds['max_subject_clipped_fraction']:
                out.append(_finding('LIGHTING_SUBJECT_CLIPPED', 'WARNING', sl['clipped_fraction'],
                                    'max_subject_clipped_fraction', thresholds, **here))
            if sl['crushed_fraction'] > thresholds['max_subject_crushed_fraction']:
                out.append(_finding('LIGHTING_SUBJECT_CRUSHED', 'WARNING', sl['crushed_fraction'],
                                    'max_subject_crushed_fraction', thresholds, **here))
            contrast = subject['contrast_stops_vs_background']
            if actor_id == key_actor and contrast is not None and abs(contrast) < thresholds['min_subject_contrast_stops']:
                out.append(_finding('LIGHTING_SUBJECT_LOW_CONTRAST', 'WARNING', contrast, 'min_subject_contrast_stops',
                                    thresholds, **here))
    for row in pairs:
        where = {'frames': row['frames'], 'shot_id': row['shot_id']}
        if row.get('cut'):
            continue
        if abs(row['mean_change_stops']) > thresholds['max_flicker_stops']:
            out.append(_finding('LIGHTING_FLICKER', 'WARNING', row['mean_change_stops'], 'max_flicker_stops', thresholds, **where))
        for actor_id, values in row['actors'].items():
            if values['changed_fraction'] is not None and values['changed_fraction'] > thresholds['max_shadow_change_fraction']:
                out.append(_finding('LIGHTING_SHADOW_JUMP', 'WARNING', values['changed_fraction'],
                                    'max_shadow_change_fraction', thresholds, actor_id=actor_id, **where))
    return out


def analyse(rendered, selection, actors, key_actor, thresholds, out_dir):
    """Measure every rendered frame pair (EEVEE + mask) and build the report. Keeps only the previous frame's
    per-pixel arrays in memory."""
    frames = {}
    pair_rows = []
    pair_set = {(a, b): shot for a, b, shot in selection['pairs']}
    previous = None
    thumbs, falses = [], []
    clip, crush = thresholds['clip_level'], thresholds['crush_level']
    for frame in selection['frames']:
        info = rendered[frame]
        eevee = imaging.read_png(info['eevee'])
        mask = imaging.read_png(info['mask'])
        row, values, labels = measure_frame(eevee, mask, actors, thresholds)
        row.update(shot_id=selection['shot_of'][frame], camera=info.get('camera'),
                   files={'eevee': info['eevee'], 'mask': info['mask']})
        frames[frame] = row
        current = (row, values, labels)
        if previous is not None and (previous[0], frame) in pair_set:
            pair = measure_pair(previous[1], current, actors, thresholds)
            cut = frames[previous[0]]['camera'] != row['camera']
            pair_rows.append(dict(pair, frames=[previous[0], frame], shot_id=pair_set[(previous[0], frame)], cut=cut))
        previous = (frame, current)
        if len(thumbs) < 64:
            thumbs.append(imaging.thumbnail(eevee, 160))
            falses.append(imaging.thumbnail(imaging.exposure_false_color(eevee, values, clip, crush), 160))
    shots = {}
    for frame, row in frames.items():
        shot = shots.setdefault(row['shot_id'], {'frames': [], 'means': []})
        shot['frames'].append(frame)
        shot['means'].append(row['luminance']['mean'])
    for shot_id, shot in shots.items():
        flicker = [abs(p['mean_change_stops']) for p in pair_rows if p['shot_id'] == shot_id and not p['cut']]
        low, high = min(shot['means']), max(shot['means'])
        shot.update(max_flicker_stops=round(max(flicker), 6) if flicker else None,
                    drift_stops=round(imaging.stops(high, low), 6), pairs=len(flicker))
        shot.pop('means')
    findings = findings_for(frames, pair_rows, actors, key_actor, thresholds)
    artifacts = {}
    if thumbs:
        artifacts['contact_sheet'] = str(Path(out_dir) / 'contact_sheet.png')
        artifacts['exposure_sheet'] = str(Path(out_dir) / 'exposure_sheet.png')
        imaging.write_png(artifacts['contact_sheet'], imaging.contact_sheet(thumbs))
        imaging.write_png(artifacts['exposure_sheet'], imaging.contact_sheet(falses))
    return {'frames': frames, 'pairs': pair_rows, 'shots': shots, 'findings': findings, 'artifacts': artifacts}


def validation_metrics(analysis, frames):
    """Scalar metrics of one validation (the quality report keeps scalars only). exposure_flicker is None when no
    same-shot consecutive pair was measured: an unmeasured flicker is never read as zero."""
    measured = [row['max_flicker_stops'] for row in analysis['shots'].values() if row.get('max_flicker_stops') is not None]
    drift = [row['drift_stops'] for row in analysis['shots'].values() if row.get('drift_stops') is not None]
    return {'exposure_flicker': round(max(measured), 6) if measured else None,
            'exposure_flicker_units': FLICKER_UNITS,
            'exposure_flicker_status': 'MEASURED' if measured else 'NOT_MEASURED_NO_SAME_SHOT_PAIR',
            'exposure_flicker_pairs': sum(int(row.get('pairs') or 0) for row in analysis['shots'].values()),
            'max_shot_drift_stops': round(max(drift), 6) if drift else None,
            'frames': frames, 'shots': len(analysis['shots'])}


def group_findings(findings):
    """One row per (code, severity) with its count: the ledger keeps every event's full state, so the summary stores
    groups and the per-frame list stays in lighting_report.json. Severities and codes are never dropped."""
    groups = {}
    for row in findings:
        key = (row['code'], row['severity'])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {'code': row['code'], 'severity': row['severity'], 'kind': row.get('kind', 'TECHNICAL'),
                                   'count': 0, 'frames': [], 'shot_ids': set(), 'actor_ids': set(), 'values': []}
            for field in ('threshold', 'threshold_name', 'threshold_source'):
                if field in row:
                    group[field] = row[field]
        group['count'] += 1
        where = row.get('frame') if row.get('frame') is not None else row.get('frames')
        if where is not None and len(group['frames']) < MAX_GROUP_FRAMES:
            group['frames'].append(where)
        if row.get('shot_id') is not None:
            group['shot_ids'].add(str(row['shot_id']))
        if row.get('actor_id') is not None:
            group['actor_ids'].add(str(row['actor_id']))
        if isinstance(row.get('value'), (int, float)) and not isinstance(row.get('value'), bool):
            group['values'].append(float(row['value']))
    out = []
    for key in sorted(groups, key=lambda k: (SEVERITY_ORDER.get(k[1], 3), k[0])):
        group = groups[key]
        values = group.pop('values')
        group.update(shot_ids=sorted(group['shot_ids']), actor_ids=sorted(group['actor_ids']),
                     value_range=[round(min(values), 6), round(max(values), 6)] if values else None,
                     frames_truncated=group['count'] > len(group['frames']))
        out.append(group)
    return out


def validation_summary(analysis, frames, state, output_dir, report_path, engine_note=None):
    """What validate_lighting records in state['qa']['lighting'] and state['lighting']['validation'], in the provider
    shape the quality report reads: passed / status / findings / metrics / state_restored, bound to the candidate
    fingerprint that was fresh when the frames were rendered."""
    findings = analysis['findings']
    severities = {}
    for row in findings:
        severities[row['severity']] = severities.get(row['severity'], 0) + 1
    errors, warnings = severities.get('ERROR', 0), severities.get('WARNING', 0)
    status = 'error' if errors else ('warning' if warnings or engine_note else 'success')
    return {'schema': SUMMARY_SCHEMA, 'passed': not errors, 'status': status,
            'candidate_fingerprint': (state.get('fingerprints') or {}).get('candidate'),
            'candidate_revision': state.get('revision'),
            # Every render call verified its restoration in Blender or raised LIGHTING_STATE_NOT_RESTORED.
            'state_restored': True,
            'findings': group_findings(findings), 'findings_format': 'GROUPED_BY_CODE_AND_SEVERITY',
            'findings_total': len(findings), 'findings_by_severity': dict(sorted(severities.items())),
            'error_count': errors, 'warning_count': warnings,
            'metrics': validation_metrics(analysis, frames),
            'metric_definitions': {'exposure_flicker': FLICKER_DEFINITION},
            'threshold_source': 'PROPOSAL', 'engine_note': engine_note,
            'output_dir': str(output_dir), 'report': str(report_path), 'frames': frames,
            'artifacts': analysis['artifacts']}


def _short_fingerprint(value):
    # m521 fingerprints are residues modulo Mersenne numbers: two different candidates were measured sharing their
    # last 12 hex digits (probe bpm10-lighting-7), so a short label hashes the whole string instead of slicing it.
    return 'sha256:' + hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:12]


def lighting_report(ctx, plan, state, options=None):
    """Quality-report provider of the lighting section. Never renders (validation is a writer that needs apply=true):
    returns the recorded validation only when it was made on the CURRENT candidate fingerprint, otherwise NOT_RUN."""
    model.closed(options, set())
    current = (state.get('fingerprints') or {}).get('candidate')
    row = (state.get('qa') or {}).get('lighting')
    if not isinstance(row, dict):
        row = (state.get('lighting') or {}).get('validation')
    validated = row.get('candidate_fingerprint') if isinstance(row, dict) else None
    if not isinstance(row, dict):
        detail = 'no lighting validation recorded for this candidate; run cinematic_validate_lighting(apply=true)'
    elif not current:
        detail = 'the cinematic has no candidate fingerprint yet; create and assemble the candidate first'
    elif validated is None:
        detail = 'the recorded validation carries no candidate_fingerprint; run cinematic_validate_lighting(apply=true)'
    elif validated != current:
        detail = ('validated candidate ' + _short_fingerprint(validated) + ' is not the current candidate '
                  + _short_fingerprint(current) + ': the candidate changed after validation; run '
                  'cinematic_validate_lighting(apply=true) again')
    else:
        report = deepcopy(row)
        report['provider'] = 'lighting.lighting_report (recorded validation, current candidate)'
        return report
    return {'passed': None, 'status': 'NOT_RUN', 'reason': 'LIGHTING_VALIDATION_STALE: ' + detail, 'findings': [],
            'metrics': {}, 'state_restored': None, 'candidate_fingerprint': current, 'validated_fingerprint': validated}


# ---------------------------------------------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------------------------------------------

def _actor(plan, actor_id):
    actors = plan.get('actors') or []
    if not actors:
        model.fail('LIGHTING_SUBJECT_REQUIRED', 'the cinematic has no registered actors')
    if actor_id is None:
        return actors[0]
    for row in actors:
        if row['actor_id'] == actor_id:
            return row
    model.fail('LIGHTING_SUBJECT_UNKNOWN', str(actor_id) + ' is not an actor of this cinematic')


def _actor_objects(actor, state):
    names = [actor['object']]
    for name in store.actor_meshes(actor, state):
        if name not in names:
            names.append(name)
    return names


def _shot_camera(plan, state, shot_id):
    if shot_id is None:
        return None
    for row in shot_rows(plan, state):
        if row['shot_id'] == shot_id:
            camera_id = row.get('camera_id')
            camera = (state.get('cameras') or {}).get(camera_id) if camera_id else None
            if not camera:
                model.fail('LIGHTING_SHOT_CAMERA_MISSING', 'shot ' + str(shot_id) + ' has no created camera')
            return camera['object']
    model.fail('LIGHTING_SHOT_UNKNOWN', str(shot_id))


def _candidate(state):
    if not state.get('assembly') or state.get('state') not in model.ASSEMBLY_STATES:
        model.fail('LIGHTING_CANDIDATE_REQUIRED', 'create the candidate scene first (state ' + str(state.get('state')) + ')')


@handler('cinematic_plan_lighting')
def plan_lighting(ctx, req):
    opts = model.closed(req.options, PLAN_OPTIONS)
    path, plan, state = store.load(ctx.storage, req.cinematic)
    _candidate(state)
    actor = _actor(plan, opts.get('key_subject'))
    camera = _shot_camera(plan, state, opts.get('shot'))
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    params = store.assembly_params(plan, state, {'subject_objects': _actor_objects(actor, state), 'camera': camera})
    inputs = ctx.bridge.run(PLAN_BODY, params)
    result = compose_plan(opts.get('preset', 'NEUTRAL_THREE_POINT'), inputs, opts, plan['generation_id'],
                          state.get('revision'), actor)
    return {'status': 'warning' if result['warnings'] else 'success', 'plan': result,
            'warnings': result['warnings'], 'frame_current': inputs['frame_current'],
            'next': 'cinematic_setup_lighting(options={plan: <this plan>}, apply=true)', **store.public(path, plan, state)}


@handler('cinematic_setup_lighting')
def setup_lighting(ctx, req):
    opts = model.closed(req.options, SETUP_OPTIONS)
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, model.ASSEMBLY_STATES)
    _candidate(state)
    spec = setup_spec(opts, plan, state)
    proposal = {'lights': [{k: row[k] for k in ('name', 'role', 'type', 'energy', 'location')} for row in spec['lights']],
                'world': spec['world'], 'view': spec['view'], 'remove_lights': spec['remove_lights'],
                'remove_world': spec['remove_world'], 'setup_revision': spec['revision'], 'warnings': spec['warnings']}
    if not req.apply:
        return {'status': 'proposal', 'apply': False, **proposal, **store.public(path, plan, state)}
    params = store.assembly_params(plan, state, {
        'lights': [_blender_light(row) for row in spec['lights']], 'world': spec['world'], 'view': spec['view'],
        'remove_lights': spec['remove_lights'], 'remove_world': spec['remove_world']})
    new, result = store.mutate(ctx, path, plan, state, 'setup_lighting', SETUP_BODY, params)
    existing = state.get('lighting') or {}
    lights = {} if (spec['lights'] and spec['replace']) else dict(existing.get('lights') or {})
    info = {row['name']: row['info'] for row in spec['lights']}
    for row in result['lights']:
        lights[row['name']] = dict({k: row[k] for k in ('role', 'type', 'energy', 'location', 'rotation_euler', 'color',
                                                         'temperature', 'use_temperature', 'digest')},
                                   plan_info=info.get(row['name']))
    world = existing.get('world')
    if spec['world'] is not None:
        world = dict(spec['world'], owned=True, previous=result['world_before'])
    # 'previous' keeps the candidate's view settings from before the first lighting setup.
    exposure = dict(result['view'], previous=(existing.get('exposure') or {}).get('previous') or result['view_before'])
    lighting = {'setup_revision': spec['revision'], 'lights': lights, 'world': world, 'exposure': exposure,
                'plan': ({k: spec['plan'].get(k) for k in ('schema', 'preset', 'key_subject', 'key_side',
                                                          'candidate_revision', 'key_to_fill_response_ratio')}
                         if spec['plan'] else existing.get('plan')),
                'removed': list(existing.get('removed') or []) + result['removed']['lights'] + result['removed']['worlds'],
                'validation': None}
    new['lighting'] = lighting
    new['qa'] = dict(new.get('qa') or {}, lighting=None)
    store.record(path, new)
    return {'status': 'warning' if spec['warnings'] else 'success', 'apply': True, 'lights': result['lights'],
            'world': result['world'], 'source_world': result['source_world'], 'view': result['view'],
            'removed': result['removed'], 'setup_revision': spec['revision'], 'warnings': spec['warnings'],
            'checkpoint': new.get('checkpoint'), **store.public(path, plan, new)}


def _run(ctx, path, state, body, params, step):
    try:
        return ctx.bridge.run(body, params)
    except Exception as exc:
        if is_uncertain(exc):
            # The render may still be running with QA settings applied: block the plan (M8/M9 pattern).
            store.record(path, dict(state, uncertain_operation=True, uncertain_step=step,
                                    last_error={'type': type(exc).__name__, 'message': str(exc)[:500]}))
        raise


def _render_mode(ctx, path, state, params, rows, mode, chunk_limit, timing):
    """Render `rows` in chunks sized from the measured seconds per image so each bridge call stays under the
    budget. The first call renders one frame: it also pays EEVEE shader compilation."""
    files = {}
    pending = list(rows)
    rate = None
    backend = None
    while pending:
        size = 1 if rate is None else max(1, min(chunk_limit, int(CALL_BUDGET_S / max(rate, 1e-3))))
        chunk, pending = pending[:size], pending[size:]
        started = time.perf_counter()
        data = _run(ctx, path, state, RENDER_BODY, dict(params, mode=mode, frames=chunk), 'validate_lighting_' + mode.lower())
        elapsed = time.perf_counter() - started
        timing['calls'].append({'mode': mode, 'images': len(chunk), 'seconds': round(elapsed, 4)})
        if not data['restored']:
            raise RuntimeError('LIGHTING_STATE_NOT_RESTORED: ' + str(data['restore_checks']))
        if data['errors']:
            message = str(data['errors'][0])
            raise ValueError(message if CODE.match(message) else 'LIGHTING_RENDER_FAILED: ' + message)
        backend = data['backend']
        rate = elapsed / len(chunk)
        for row in data['files']:
            files[row['frame']] = row
        timing['size'] = [data['width'], data['height']]
    return files, backend


def _timing_summary(calls, mode):
    rows = [c for c in calls if c['mode'] == mode]
    if not rows:
        return None
    images = sum(c['images'] for c in rows)
    later = rows[1:]
    later_images = sum(c['images'] for c in later)
    return {'calls': len(rows), 'images': images, 'seconds_total': round(sum(c['seconds'] for c in rows), 4),
            'first_call_seconds': rows[0]['seconds'], 'first_call_images': rows[0]['images'],
            'seconds_per_image_after_first': (round(sum(c['seconds'] for c in later) / later_images, 4)
                                              if later_images else None),
            'note': 'host wall time per bridge call (includes payload transfer and restore); the first call also '
                    'compiles shaders'}


@handler('cinematic_validate_lighting')
def validate_lighting(ctx, req):
    opts = model.closed(req.options, VALIDATE_OPTIONS)
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, model.ASSEMBLY_STATES)
    _candidate(state)
    thresholds = thresholds_from(opts.get('thresholds'))
    selection = select_frames(opts, plan, state)
    width = model.number(opts.get('size', DEFAULT_WIDTH), 'options.size', 16, 1920, integer=True)
    samples = model.number(opts.get('samples', DEFAULT_SAMPLES), 'options.samples', 1, 1024, integer=True)
    chunk_limit = model.number(opts.get('frames_per_call', 16), 'options.frames_per_call', 1, 64, integer=True)
    actor_rows = plan.get('actors') or []
    actors = [(row['actor_id'], i) for i, row in enumerate(actor_rows)]
    planned = (state.get('lighting') or {}).get('plan') or {}
    key_actor = _actor(plan, opts.get('key_subject') or planned.get('key_subject'))['actor_id']
    palette = {}
    for row, (actor_id, index) in zip(actor_rows, actors):
        for name in _actor_objects(row, state):
            palette.setdefault(name, PALETTE[index] + [1.0])
    capture_objects = [name for row in actor_rows for name in _actor_objects(row, state)]
    proposal = {'frames': selection['frames'], 'pairs': [list(p) for p in selection['pairs']],
                'samples': selection['samples'], 'width': width, 'eevee_samples': samples,
                'images': 2 * len(selection['frames']), 'thresholds': thresholds, 'threshold_source': 'PROPOSAL'}
    if not req.apply:
        return {'status': 'proposal', 'apply': False, **proposal, **store.public(path, plan, state)}
    require_mutable_asset(ctx.bridge, ctx.storage, 'M10 lighting validation')
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    pre = ctx.bridge.run(PREFLIGHT_BODY, store.assembly_params(plan, state, {'actor_objects': capture_objects}))
    if not pre['has_camera']:
        model.fail('LIGHTING_NO_CAMERA', 'the candidate scene has no camera and no camera markers')
    if pre['missing_objects']:
        model.fail('CINEMATIC_OBJECT_MISSING', ', '.join(pre['missing_objects']))
    out = ctx.storage.reserve(QA_CATEGORY, ctx.operation_id) / 'lighting'
    for sub in ('eevee', 'mask'):
        (out / sub).mkdir(parents=True, exist_ok=False)
    longest = out / 'eevee' / ('frame_' + format(max(selection['frames']), '06d') + '.png')
    if len(str(longest)) > MAX_PATH:
        model.fail('LIGHTING_PATH_TOO_LONG', str(len(str(longest))) + ' characters; use a shorter workdir')
    rows = {mode: [{'frame': f, 'path': (out / sub / ('frame_' + format(f, '06d') + '.png')).as_posix()}
                   for f in selection['frames']] for mode, sub in (('EEVEE', 'eevee'), ('MASK', 'mask'))}
    params = store.assembly_params(plan, state, {'actor_objects': capture_objects, 'palette': palette, 'width': width,
                                                 'samples': samples})
    timing = {'calls': []}
    eevee, backend = _render_mode(ctx, path, state, params, rows['EEVEE'], 'EEVEE', chunk_limit, timing)
    masks, mask_backend = _render_mode(ctx, path, state, params, rows['MASK'], 'MASK', 64, timing)
    rendered = {}
    for frame in selection['frames']:
        if frame not in eevee or frame not in masks or not Path(eevee[frame]['path']).is_file() \
                or not Path(masks[frame]['path']).is_file():
            model.fail('LIGHTING_RENDER_MISSING', 'frame ' + str(frame))
        rendered[frame] = {'eevee': eevee[frame]['path'], 'mask': masks[frame]['path'], 'camera': eevee[frame]['camera']}
    analysis = analyse(rendered, selection, actors, key_actor, thresholds, out)
    errors = [f for f in analysis['findings'] if f['severity'] == 'ERROR']
    engine_note = None
    if pre['engine'] != 'BLENDER_EEVEE':
        engine_note = {'code': 'LIGHTING_QA_ENGINE_DIFFERS', 'message': 'QA rendered EEVEE; the candidate renders '
                       + pre['engine'] + ': final images may differ'}
    timing_summary = {'EEVEE': _timing_summary(timing['calls'], 'EEVEE'), 'WORKBENCH_MASK': _timing_summary(timing['calls'], 'MASK'),
                      'size': timing.get('size'), 'eevee_samples': samples}
    report = {'schema': REPORT_SCHEMA, 'cinematic': str(path), 'generation_id': plan['generation_id'],
              'candidate_revision': state.get('revision'), 'candidate_fingerprint': (state.get('fingerprints') or {}).get('candidate'),
              'selection': {k: selection[k] for k in ('frames', 'samples', 'flicker_pairs')},
              'pairs': [list(p) for p in selection['pairs']], 'thresholds': thresholds, 'threshold_source': 'PROPOSAL',
              'render_backend': {'eevee': backend, 'mask': mask_backend}, 'timing': timing_summary,
              'frames': {str(k): v for k, v in analysis['frames'].items()}, 'pair_metrics': analysis['pairs'],
              'shots': analysis['shots'], 'technical_findings': analysis['findings'],
              'artistic_observations': [], 'artifacts': analysis['artifacts'],
              'definitions': {'luminance': 'linear Rec.709 Y from sRGB-decoded 8-bit PNG (after the view transform)',
                              'contrast_stops_vs_background': 'log2((subject mean + 1e-4)/(non-actor mean + 1e-4))',
                              'flicker': 'frame-mean change in stops between adjacent frames of one shot (cuts skipped)',
                              'shadow_jump_proxy': 'fraction of actor pixels present in both adjacent frames whose '
                                                   'luminance changed by more than shadow_change_stops; motion also '
                                                   'moves pixels, so this is a proxy that needs visual review'},
              'scene_lights': pre['scene_lights'], 'world': pre['world'], 'view': pre['view']}
    summary = validation_summary(analysis, len(selection['frames']), state, out, out / 'lighting_report.json', engine_note)
    report['metrics'] = summary['metrics']
    report['definitions']['exposure_flicker'] = FLICKER_DEFINITION
    write_json(out / 'lighting_report.json', report)
    new = deepcopy(state)
    new['lighting'] = dict(new.get('lighting') or {}, validation=summary)
    new['qa'] = dict(new.get('qa') or {}, lighting=summary)
    store.record(path, new)
    compact = [{'frame': f, 'shot_id': r['shot_id'], 'camera': r['camera'], 'mean': r['luminance']['mean'],
                'median': r['luminance']['median'], 'clipped': r['luminance']['clipped_fraction'],
                'crushed': r['luminance']['crushed_fraction'],
                'key_subject': {k: (r['actors'][key_actor]['luminance'][k] if k != 'coverage_fraction'
                                    else r['actors'][key_actor]['coverage_fraction'])
                                for k in ('mean', 'clipped_fraction', 'crushed_fraction', 'coverage_fraction')},
                'contrast_stops': r['actors'][key_actor]['contrast_stops_vs_background']}
               for f, r in sorted(analysis['frames'].items())]
    # store.public goes first: its plan-level 'shots' count must not hide the measured per-shot rows.
    result = {**store.public(path, plan, new), 'apply': True, **summary, 'render_backend': backend,
              'timing': timing_summary, 'thresholds': thresholds, 'threshold_source': 'PROPOSAL',
              'shots': analysis['shots'], 'frame_metrics': compact,
              'technical_findings': analysis['findings'][:50], 'findings_total': len(analysis['findings']),
              'artistic_observations': 'REQUIRED: look at contact_sheet.png/exposure_sheet.png; numbers do not grade art',
              'warnings': [engine_note] if engine_note else []}
    if errors:
        result['errors'] = [{'code': f['code'], 'message': 'frame ' + str(f.get('frame'))} for f in errors[:10]]
    return result
