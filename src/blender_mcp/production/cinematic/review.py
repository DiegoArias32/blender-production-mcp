"""M10 area G: previs, full temporal preview, real visual review, measured refinement (at most 3 cycles), technical
quality report and acceptance of the cinematic candidate.

Why the payloads look the way they do:
- Everything renders or measures the candidate scene `M10C_<g8>_v<n>` and restores it inside the same payload:
  frame, scene camera, render settings, Workbench shading, shared actors' channels/shape keys/NLA time, animated
  camera/light/world/exposure values and the window scene. frame_set on a scene that is not the window scene writes
  evaluated values back into the shared originals (measured, Blender 5.2.1), so restoring is not optional. The proof
  of restoration is the source and candidate fingerprints recomputed at the end of every payload, not the absence of
  an exception.
- Cuts are the candidate's camera markers. Renders call frame_set so Blender switches scene.camera at each marker
  (measured: the render obeys the marker, a hand-set scene.camera is ignored), and every rendered frame records the
  camera Blender used next to the camera the markers predict. Markers are never disabled here: they are the edit.
- Renders are Workbench (GPU rasterizer); `render.opengl` does not work in background (measured). Each bridge call
  stays well under the 180 s transport budget by sizing chunks from the measured seconds per image.

Technical findings (measured by tools) and artistic observations (written by a human reviewer) stay separate. No
score is produced or accepted. Numbers that are limits are proposals labelled `PROPOSAL`; measurements come from
Blender.
"""
from __future__ import annotations

from copy import deepcopy
import html as htmllib
from importlib import import_module
import inspect
import json
import math
from pathlib import Path
import struct
import time

from ..animation.generation import store as gstore
from ..animation.generation.render import write_apng
from ..checkpoint import create_checkpoint
from ..common.asset_safety import require_mutable_asset
from ..common.ledger import is_uncertain
from ..common.storage import sha256, write_json
from ..qa.capture import _STATE, _verify_images
from . import model, store
from .runtime import COMMON
from .service import HANDLERS, Request, handler, load_handlers

# ---------------------------------------------------------------------------------------------------- constants

DEFAULT_SIZE = 480                 # INFERRED_DEFAULT: long edge in pixels of Workbench previs/preview images
SIZE_RANGE = (64, 1920)            # input validation range
CHUNK_BUDGET_S = 90.0              # PROPOSAL: target seconds per bridge call (contract limit: < 180 s per call)
MAX_CHUNK_FRAMES = 240             # PROPOSAL
MAX_PREVIEW_FRAMES = int(model.MAX_DURATION_S * max(model.FPS_ALLOWED))
MAX_PREVIEW_PIXELS = 2_000_000_000  # PROPOSAL: disk/time budget of one preview
MEASURE_CHUNK_FRAMES = 600         # PROPOSAL: frames per measurement call
MAX_CHANGES = 8
MAX_STEP = 1000
PATH_LIMIT = 259                   # Windows MAX_PATH without the long-path prefix
LIMITS_SOURCE = 'PROPOSAL'
REVIEW_SPEEDS_REQUIRED = (1.0, 0.25)
PLAYER_SPEEDS = (1.0, 0.5, 0.25)
FORBIDDEN_REVIEW_KEYS = frozenset({'score', 'rating', 'aaa_score', 'quality_score'})
SYNTHETIC_LABEL = 'SYNTHETIC_REVIEW_FOR_MECHANISM_TEST'
SNAPSHOT_KEYS = ('state', 'render', 'review', 'quality_report')
NO_SCORE = ('No quality or AAA score is produced or accepted: measurements (TECHNICAL FINDINGS) and human judgement '
            '(ARTISTIC OBSERVATIONS) stay separate.')

# Art-direction guide the reviewer fills in. Never auto-filled: templates carry NOT_FILLED and an ACCEPT needs every
# item answered by the reviewer.
DIRECTION_CHECKLIST = (
    ('shot_readability', 'Legibilidad del plano'), ('visual_hierarchy', 'Jerarquía visual'),
    ('dramatic_emphasis', 'Énfasis dramático'), ('anticipation', 'Anticipación'), ('rhythm', 'Ritmo'),
    ('composition', 'Composición'), ('camera_movement', 'Movimiento de cámara'), ('visibility', 'Visibilidad'),
    ('silhouette', 'Silueta'), ('expression', 'Expresión'), ('mood', 'Mood'), ('transitions', 'Transiciones'),
    ('action_clarity', 'Claridad de acción'),
)
CHECKLIST_STATUS = ('OK', 'ISSUE', 'NOT_APPLICABLE')
REVIEW_KEYS = frozenset({'reviewer', 'technical_findings', 'artistic_observations', 'decision', 'artifact_reference',
                         'temporal_reviewed', 'shots_reviewed', 'speeds_reviewed', 'direction_checklist'})

# Refinement targets: direction of improvement and where the measurement comes from. BUILTIN metrics are measured by
# this module's own payload (the same definition before and after); SECTION metrics are read from the quality-report
# provider of that section.
REFINE_TARGETS = {
    'actor_screen_height': {'direction': 'lower', 'source': 'BUILTIN', 'goal': (0.01, 2.0), 'units': 'frame heights',
                            'definition': 'max over measured frames/actors of |projected AABB height / frame height - goal|'},
    'headroom_error': {'direction': 'lower', 'source': 'BUILTIN', 'goal': (-1.0, 1.0), 'units': 'frame heights',
                       'definition': 'max over measured frames/actors of |(1 - projected AABB top) - goal|'},
    'visibility_min_fraction': {'direction': 'higher', 'source': 'BUILTIN', 'goal': None, 'units': 'fraction',
                                'definition': 'min over measured frames/actors of the fraction of 9 AABB sample points '
                                              '(8 corners + centre) inside the frame and not occluded by other geometry'},
    'camera_max_jerk': {'direction': 'lower', 'source': 'BUILTIN', 'goal': None, 'units': 'm/s^3',
                        'definition': 'max |third finite difference of the evaluated camera position| * fps^3 inside '
                                      'each continuous same-camera run (cuts excluded)'},
    'max_penetration_m': {'direction': 'lower', 'source': 'SECTION:staging', 'goal': None, 'units': 'm',
                          'definition': 'metrics.max_penetration_m of the staging (actor collision) section provider'},
    'exposure_flicker': {'direction': 'lower', 'source': 'SECTION:lighting', 'goal': None, 'units': 'provider units',
                         'definition': 'metrics.exposure_flicker of the lighting section provider'},
}
CHANGE_KINDS = ('camera_settings', 'camera_offset', 'light_energy', 'exposure', 'actor_offset', 'clip_shift')
CAMERA_SETTINGS = {'lens': (1.0, 5000.0), 'sensor_width': (1.0, 200.0), 'sensor_height': (1.0, 200.0),
                   'shift_x': (-10.0, 10.0), 'shift_y': (-10.0, 10.0), 'clip_start': (1e-6, 1e6),
                   'clip_end': (1e-6, 1e7), 'focus_distance': (0.0, 1e6), 'aperture_fstop': (0.1, 1e3)}

# Quality report sections. Providers are looked up lazily by name: a module that does not exist yet yields NOT_RUN
# with the reason, never PASS. `handler` is the tool whose handler is used when no provider function exists.
SECTIONS = (
    {'id': 'integrity', 'label': 'integridad de fuentes', 'providers': (('actors', 'integrity_report'),),
     'handler': 'cinematic_verify_integrity', 'mandatory': 'always'},
    {'id': 'staging', 'label': 'staging (colisiones entre actores)', 'providers': (('analysis', 'collision_report'),),
     'handler': 'cinematic_analyze_actor_collisions', 'mandatory': 'multi_actor'},
    {'id': 'sync', 'label': 'sincronización', 'providers': (('performance', 'sync_report'), ('analysis', 'sync_report')),
     'handler': None, 'mandatory': 'clips'},
    {'id': 'contacts', 'label': 'contactos', 'providers': (('analysis', 'contact_report'),),
     'handler': 'cinematic_analyze_contacts', 'mandatory': 'props'},
    {'id': 'facial_timing', 'label': 'timing facial', 'providers': (('performance', 'facial_report'),),
     'handler': None, 'mandatory': 'facial'},
    {'id': 'secondary', 'label': 'secundario', 'providers': (('performance', 'secondary_report'),),
     'handler': None, 'mandatory': 'secondary'},
    {'id': 'continuity', 'label': 'continuidad', 'providers': (('analysis', 'continuity_report'),),
     'handler': 'cinematic_validate_continuity', 'mandatory': 'always'},
    {'id': 'framing', 'label': 'encuadre', 'providers': (('analysis', 'composition_report'),),
     'handler': 'cinematic_analyze_composition', 'mandatory': 'always'},
    {'id': 'visibility', 'label': 'visibilidad', 'providers': (('analysis', 'visibility_report'),),
     'handler': 'cinematic_analyze_visibility', 'mandatory': 'always'},
    {'id': 'motion', 'label': 'movimiento', 'providers': (('analysis', 'motion_report'),),
     'handler': 'cinematic_analyze_motion', 'mandatory': 'always'},
    # Lighting validation renders EEVEE and needs apply=true (a writer): the report reads its recorded result when it
    # belongs to the current candidate revision instead of re-running it as a side effect.
    {'id': 'lighting', 'label': 'luz', 'providers': (('lighting', 'lighting_report'),), 'handler': None,
     'recorded': ('qa', 'lighting'), 'mandatory': 'lighting'},
    {'id': 'render', 'label': 'render (preview temporal)', 'providers': (), 'handler': None, 'mandatory': 'always'},
)
SECTION_BY_ID = {s['id']: s for s in SECTIONS}

# ---------------------------------------------------------------------------------------------------- Blender payloads

RUNTIME = COMMON + _STATE + r'''
def cr_num(value):
    # Results travel as strict JSON (allow_nan=False): a non-finite projection becomes None, never NaN.
    value = float(value)
    return round(value, 6) if math.isfinite(value) else None

def cr_raw(value):
    # Unrounded: a third finite difference multiplies rounding noise by up to 8 * fps^3.
    value = float(value)
    return value if math.isfinite(value) else None

def cr_windows():
    return [w.scene.name for w in bpy.context.window_manager.windows]

def cr_marker_rows(scene):
    return sorted([[m.frame, m.camera.name] for m in scene.timeline_markers if m.camera is not None])

def cr_marker_camera(rows, frame):
    # Blender uses the camera of the last camera marker at or before the frame and, before the first marker, the
    # first marker's camera (measured, Blender 5.2.1). Recomputed so every rendered frame proves it obeyed the cuts.
    if not rows:
        return None
    chosen = rows[0][1]
    for marker_frame, camera_name in rows:
        if marker_frame <= frame:
            chosen = camera_name
    return chosen

def cr_light_row(light):
    row = [light.energy, list(light.color), light.shadow_soft_size]
    if light.type == 'SPOT':
        row += [light.spot_size, light.spot_blend]
    elif light.type == 'AREA':
        row += [light.size, light.size_y]
    elif light.type == 'SUN':
        row += [light.angle]
    return row

def cr_data_state(scene):
    # frame_set writes evaluated values of animated properties back into the original datablocks (measured for shared
    # objects); animated lens, light energy, world strength or exposure would otherwise leak into the candidate digest.
    cameras = {}
    lights = {}
    for obj in scene.objects:
        data = obj.data
        if obj.type == 'CAMERA' and data is not None and data.name not in cameras:
            dof = data.dof
            cameras[data.name] = [data.lens, data.sensor_width, data.sensor_height, data.shift_x, data.shift_y,
                                  data.clip_start, data.clip_end, data.ortho_scale, dof.use_dof, dof.focus_distance,
                                  dof.aperture_fstop]
        elif obj.type == 'LIGHT' and data is not None and data.name not in lights:
            lights[data.name] = cr_light_row(data)
    world = []
    if scene.world is not None and scene.world.node_tree is not None:
        for node in scene.world.node_tree.nodes:
            if node.type == 'BACKGROUND':
                world.append([node.name, list(node.inputs['Color'].default_value), node.inputs['Strength'].default_value])
    view = scene.view_settings
    return {'cameras': cameras, 'lights': lights, 'world': world, 'exposure': view.exposure, 'gamma': view.gamma}

def cr_restore_data(scene, state):
    for name, row in state['cameras'].items():
        data = bpy.data.cameras.get(name)
        if data is None:
            continue
        data.lens = row[0]
        data.sensor_width = row[1]
        data.sensor_height = row[2]
        data.shift_x = row[3]
        data.shift_y = row[4]
        data.clip_start = row[5]
        data.clip_end = row[6]
        data.ortho_scale = row[7]
        data.dof.use_dof = row[8]
        data.dof.focus_distance = row[9]
        data.dof.aperture_fstop = row[10]
    for name, row in state['lights'].items():
        light = bpy.data.lights.get(name)
        if light is None:
            continue
        light.energy = row[0]
        light.color = row[1]
        light.shadow_soft_size = row[2]
        if light.type == 'SPOT':
            light.spot_size = row[3]
            light.spot_blend = row[4]
        elif light.type == 'AREA':
            light.size = row[3]
            light.size_y = row[4]
        elif light.type == 'SUN':
            light.angle = row[3]
    if scene.world is not None and scene.world.node_tree is not None:
        for node_name, color, strength in state['world']:
            node = scene.world.node_tree.nodes.get(node_name)
            if node is not None:
                node.inputs['Color'].default_value = color
                node.inputs['Strength'].default_value = strength
    scene.view_settings.exposure = state['exposure']
    scene.view_settings.gamma = state['gamma']

def cr_begin(scene):
    objects = list(scene.objects)
    return {'objects': objects, 'captured': cm_capture(scene, objects), 'data': cr_data_state(scene),
            'qa': qa_state(scene), 'camera': scene.camera, 'windows': cr_windows()}

def cr_end(scene, begin):
    # Render settings first (qa_restore frame_sets and re-assigns the camera), then channels (cm_restore frame_sets
    # once and writes every captured channel), then animated data, then the camera again: the last frame_set may have
    # switched it through a marker.
    with bpy.context.temp_override(scene=scene):
        qa_restore(scene, begin['qa'], begin['camera'])
        cm_restore(scene, begin['objects'], begin['captured'])
    cr_restore_data(scene, begin['data'])
    scene.camera = begin['camera']
    return {'render_settings': qa_state(scene) == begin['qa'],
            'channels': cm_capture(scene, begin['objects']) == begin['captured'],
            'animated_data': cr_data_state(scene) == begin['data'],
            'window_scene': cr_windows() == begin['windows']}

def cr_depsgraph(scene):
    depsgraph = cm_depsgraph(scene)
    if depsgraph.scene.name != scene.name:
        cm_fail('CINEMATIC_DEPSGRAPH_MISMATCH', depsgraph.scene.name + ' evaluated instead of ' + scene.name)
    return depsgraph

def cr_camera_pose(camera, depsgraph):
    evaluated = camera.evaluated_get(depsgraph)
    matrix = evaluated.matrix_world
    data = evaluated.data
    dof = data.dof
    return {'camera': camera.name, 'data': camera.data.name, 'type': data.type,
            'location': [cr_num(v) for v in matrix.translation],
            'rotation_euler_rad': [cr_num(v) for v in matrix.to_euler()],
            'lens_mm': cr_num(data.lens), 'lens_unit': data.lens_unit, 'sensor_width_mm': cr_num(data.sensor_width),
            'sensor_fit': data.sensor_fit, 'shift': [cr_num(data.shift_x), cr_num(data.shift_y)],
            'dof': {'use_dof': dof.use_dof, 'focus_object': dof.focus_object.name if dof.focus_object else None,
                    'focus_distance_m': cr_num(dof.focus_distance), 'aperture_fstop': cr_num(dof.aperture_fstop)}}

def cr_box(depsgraph, names):
    points = []
    for name in names:
        obj = cm_object(name)
        evaluated = obj.evaluated_get(depsgraph)
        matrix = evaluated.matrix_world
        for corner in evaluated.bound_box:
            points.append(matrix @ mathutils.Vector((corner[0], corner[1], corner[2])))
    low = [min(p[i] for p in points) for i in range(3)]
    high = [max(p[i] for p in points) for i in range(3)]
    box = []
    for x in (low[0], high[0]):
        for y in (low[1], high[1]):
            for z in (low[2], high[2]):
                box.append(mathutils.Vector((x, y, z)))
    box.append(mathutils.Vector(((low[0] + high[0]) * 0.5, (low[1] + high[1]) * 0.5, (low[2] + high[2]) * 0.5)))
    return box

def cr_unoccluded(scene, depsgraph, start, point, names):
    # scene.ray_cast still hits hide_render objects (measured): step through them instead of counting them as cover.
    direction = point - start
    remaining = direction.length
    if remaining <= 2e-4:
        return True
    direction.normalize()
    for attempt in range(16):
        hit, location, normal, index, obj, matrix = scene.ray_cast(depsgraph, start, direction, distance=remaining - 1e-4)
        if not hit or obj is None:
            return True
        original = bpy.data.objects.get(obj.name)
        if obj.name in names:
            return True
        if original is not None and original.hide_render:
            moved = (location - start).length + 1e-4
            start = location + direction * 1e-4
            remaining = remaining - moved
            if remaining <= 1e-4:
                return True
            continue
        return False
    return False

def cr_actor_stats(scene, depsgraph, camera, actors):
    projection, view = cm_camera_matrices(camera, scene, depsgraph)
    evaluated = camera.evaluated_get(depsgraph)
    origin = evaluated.matrix_world.translation.copy()
    forward = (evaluated.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))).normalized()
    ortho = camera.data.type == 'ORTHO'
    stats = {}
    for actor_id, names in actors:
        box = cr_box(depsgraph, names)
        projected = [cm_project(projection, view, p) for p in box]
        corners = projected[:8]
        in_front = all(p is not None and p[2] > 0 for p in corners)
        visible = 0
        for point, proj in zip(box, projected):
            if proj is None or proj[2] <= 0 or not (0.0 <= proj[0] <= 1.0 and 0.0 <= proj[1] <= 1.0):
                continue
            start = point - forward * proj[2] if ortho else origin
            if cr_unoccluded(scene, depsgraph, start, point, names):
                visible += 1
        row = {'in_front': in_front, 'visible_fraction': cr_num(visible / len(box))}
        if in_front:
            xs = [p[0] for p in corners]
            ys = [p[1] for p in corners]
            row.update(height=cr_num(max(ys) - min(ys)), top=cr_num(max(ys)), bottom=cr_num(min(ys)),
                       left=cr_num(min(xs)), right=cr_num(max(xs)))
        stats[actor_id] = row
    return stats

def cr_render_frame(params, scene, markers, frame):
    with bpy.context.temp_override(scene=scene):
        scene.frame_set(frame)
        depsgraph = cr_depsgraph(scene)
        camera = scene.camera
        if camera is None or camera.type != 'CAMERA':
            cm_fail('CINEMATIC_CAMERA_REQUIRED', 'no camera at frame ' + str(frame) + ': add a camera marker or a scene camera')
        row = {'frame': frame, 'camera': camera.name, 'expected_camera': cr_marker_camera(markers, frame)}
        if params.get('stats'):
            row['camera_pose'] = cr_camera_pose(camera, depsgraph)
            row['actors'] = cr_actor_stats(scene, depsgraph, camera, params['actors'])
        path = params['directory'] + '/frame_' + format(frame, '06d') + '.png'
        scene.render.filepath = path
        # Inside temp_override(scene=candidate) without scene=: render(scene=name) rendered the window scene's frame
        # and camera (measured by the lighting area: candidate frames 31 and 60 came out as frame 1).
        outcome = bpy.ops.render.render(write_still=True)
        if 'FINISHED' not in outcome:
            cm_fail('CINEMATIC_RENDER_FAILED', 'frame ' + str(frame) + ': ' + str(outcome))
        row['camera_after_render'] = scene.camera.name if scene.camera else None
        row['path'] = path
    return row

def cr_render(params):
    scene, src, cin = cm_candidate(params)
    begin = cr_begin(scene)
    shading = scene.display.shading
    look = [scene.render.engine, shading.light, shading.color_type]
    markers = cr_marker_rows(scene)
    rows = []
    errors = []
    backend = None
    try:
        scene.render.engine = 'BLENDER_WORKBENCH'
        shading.light = 'STUDIO'
        shading.color_type = 'MATERIAL'
        qa_configure(scene, params['width'], params['height'])
        backend = qa_render_backend(scene, False)
        for frame in params['frames']:
            rows.append(cr_render_frame(params, scene, markers, frame))
    except Exception as exc:
        errors.append(str(exc)[:500])
    finally:
        scene.render.engine = look[0]
        shading.light = look[1]
        shading.color_type = look[2]
        restored = cr_end(scene, begin)
    restored['workbench_look'] = [scene.render.engine, shading.light, shading.color_type] == look
    return {'status': 'error' if errors else 'ok', 'errors': errors, 'rows': rows, 'render_backend': backend,
            'markers': markers, 'restored': restored, 'state_restored': all(restored.values()),
            'fingerprints': cm_fingerprints(params)}

def cr_measure(params):
    scene, src, cin = cm_candidate(params)
    begin = cr_begin(scene)
    rows = []
    errors = []
    try:
        for frame in params['frames']:
            with bpy.context.temp_override(scene=scene):
                scene.frame_set(frame)
                depsgraph = cr_depsgraph(scene)
                camera = scene.camera
                if camera is None or camera.type != 'CAMERA':
                    rows.append([frame, None, None, {}])
                    continue
                location = [cr_raw(v) for v in camera.evaluated_get(depsgraph).matrix_world.translation]
                rows.append([frame, camera.name, location, cr_actor_stats(scene, depsgraph, camera, params['actors'])])
    except Exception as exc:
        errors.append(str(exc)[:500])
    finally:
        restored = cr_end(scene, begin)
    return {'status': 'error' if errors else 'ok', 'errors': errors, 'rows': rows, 'restored': restored,
            'state_restored': all(restored.values()), 'fingerprints': cm_fingerprints(params)}

def cr_info(params):
    scene, src, cin = cm_candidate(params)
    render = scene.render
    return {'scene': scene.name, 'frame_start': scene.frame_start, 'frame_end': scene.frame_end, 'fps': render.fps,
            'fps_base': render.fps_base,
            'resolution': [render.resolution_x, render.resolution_y, render.resolution_percentage],
            'markers': sorted([[m.frame, m.name, m.camera.name if m.camera else None] for m in scene.timeline_markers]),
            'camera': scene.camera.name if scene.camera else None,
            'cameras': sorted(o.name for o in scene.objects if o.type == 'CAMERA'), 'windows': cr_windows()}

# ---- refinement changes: only owned cameras/lights, the candidate exposure, recorded actor deltas and owned strips ----

def cr_animated_paths(idblock):
    ad = getattr(idblock, 'animation_data', None)
    paths = set()
    if ad is None:
        return paths
    pairs = []
    if ad.action is not None:
        pairs.append([ad.action, ad.action_slot])
    for track in ad.nla_tracks:
        for strip in track.strips:
            if strip.action is not None:
                pairs.append([strip.action, strip.action_slot])
    for action, slot in pairs:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    if slot is None or bag.slot_handle == slot.handle:
                        for curve in bag.fcurves:
                            paths.add(curve.data_path)
    for curve in ad.drivers:
        paths.add(curve.data_path)
    return paths

def cr_camera_path(key):
    if key in ('focus_distance', 'aperture_fstop'):
        return 'dof.' + key
    return key

def cr_camera_value(data, key):
    if key == 'lens':
        return data.lens
    if key == 'sensor_width':
        return data.sensor_width
    if key == 'sensor_height':
        return data.sensor_height
    if key == 'shift_x':
        return data.shift_x
    if key == 'shift_y':
        return data.shift_y
    if key == 'clip_start':
        return data.clip_start
    if key == 'clip_end':
        return data.clip_end
    if key == 'focus_distance':
        return data.dof.focus_distance
    if key == 'aperture_fstop':
        return data.dof.aperture_fstop
    cm_fail('REFINEMENT_CHANGE_INVALID', 'camera setting ' + str(key))

def cr_set_camera_value(data, key, value):
    if key == 'lens':
        data.lens = value
    elif key == 'sensor_width':
        data.sensor_width = value
    elif key == 'sensor_height':
        data.sensor_height = value
    elif key == 'shift_x':
        data.shift_x = value
    elif key == 'shift_y':
        data.shift_y = value
    elif key == 'clip_start':
        data.clip_start = value
    elif key == 'clip_end':
        data.clip_end = value
    elif key == 'focus_distance':
        data.dof.focus_distance = value
    elif key == 'aperture_fstop':
        data.dof.aperture_fstop = value
    else:
        cm_fail('REFINEMENT_CHANGE_INVALID', 'camera setting ' + str(key))

def cr_owned_object(params, name, kind):
    obj = cm_object(name, [kind])
    if not cm_is_owned(obj, params) or not cm_is_owned(obj.data, params):
        cm_fail('REFINEMENT_TARGET_NOT_OWNED', name + ' is not a ' + kind.lower() + ' owned by this cinematic; source '
                'datablocks are never changed')
    return obj

def cr_actor_object(params, name):
    if name not in params['actor_objects']:
        cm_fail('ACTOR_NOT_IN_CINEMATIC', name)
    return cm_object(name)

def cr_apply_one(params, scene, change, captured, touched, dry):
    kind = change['kind']
    if kind == 'camera_settings':
        obj = cr_owned_object(params, change['camera'], 'CAMERA')
        animated = cr_animated_paths(obj.data)
        for key in sorted(change['settings']):
            if cr_camera_path(key) in animated:
                cm_fail('REFINEMENT_CHANNEL_ANIMATED', obj.data.name + '.' + cr_camera_path(key) + ' is animated; '
                        'change its keys with the camera tools')
            before = cr_camera_value(obj.data, key)
            if not dry:
                cr_set_camera_value(obj.data, key, change['settings'][key])
            captured.append({'kind': kind, 'camera': obj.name, 'key': key, 'before': before,
                             'after': cr_camera_value(obj.data, key)})
    elif kind == 'camera_offset':
        obj = cr_owned_object(params, change['camera'], 'CAMERA')
        if 'delta_location' in cr_animated_paths(obj):
            cm_fail('REFINEMENT_CHANNEL_ANIMATED', obj.name + '.delta_location is animated')
        before = list(obj.delta_location)
        if not dry:
            obj.delta_location = [before[i] + change['offset'][i] for i in range(3)]
        captured.append({'kind': kind, 'camera': obj.name, 'before': before, 'after': list(obj.delta_location)})
    elif kind == 'light_energy':
        obj = cr_owned_object(params, change['light'], 'LIGHT')
        if 'energy' in cr_animated_paths(obj.data):
            cm_fail('REFINEMENT_CHANNEL_ANIMATED', obj.data.name + '.energy is animated')
        before = obj.data.energy
        if not dry:
            obj.data.energy = change['energy']
        captured.append({'kind': kind, 'light': obj.name, 'before': before, 'after': obj.data.energy})
    elif kind == 'exposure':
        if 'view_settings.exposure' in cr_animated_paths(scene):
            cm_fail('REFINEMENT_CHANNEL_ANIMATED', scene.name + '.view_settings.exposure is animated')
        before = scene.view_settings.exposure
        if not dry:
            scene.view_settings.exposure = change['exposure']
        captured.append({'kind': kind, 'before': before, 'after': scene.view_settings.exposure})
    elif kind == 'actor_offset':
        obj = cr_actor_object(params, change['object'])
        if obj.parent is not None or len(obj.constraints):
            cm_fail('ACTOR_STAGING_UNSUPPORTED', obj.name + ' has a parent or constraints; a delta would not move it '
                    'predictably')
        if any(p.startswith('delta_') for p in cr_animated_paths(obj)):
            cm_fail('ACTOR_DELTA_ANIMATED', obj.name + ' animates delta transforms')
        if not dry:
            touched.append(cm_touch_delta(obj))
        before = list(obj.delta_location)
        if not dry:
            obj.delta_location = [before[i] + change['offset'][i] for i in range(3)]
        captured.append({'kind': kind, 'object': obj.name, 'before': before, 'after': list(obj.delta_location)})
    elif kind == 'clip_shift':
        obj = cr_actor_object(params, change['object'])
        ad = obj.animation_data
        track = ad.nla_tracks.get(change['track']) if ad is not None else None
        if track is None or not track.name.startswith(params['owned_prefix'] + '_'):
            cm_fail('REFINEMENT_TARGET_NOT_OWNED', str(change['track']) + ' is not an NLA track owned by this cinematic')
        strip = track.strips.get(change['strip'])
        if strip is None:
            cm_fail('REFINEMENT_TARGET_MISSING', 'strip ' + str(change['strip']) + ' in ' + track.name)
        before = strip.frame_start
        wanted = before + change['frames']
        if not dry:
            # frame_start_ui keeps the strip length; the raw frame_start setter remaps time incoherently (measured).
            strip.frame_start_ui = wanted
            if abs(strip.frame_start - wanted) > 1e-6:
                strip.frame_start_ui = before
                cm_fail('REFINEMENT_CLIP_SHIFT_BLOCKED', strip.name + ' cannot move to ' + str(wanted) + ' (neighbour '
                        'strip or track bounds)')
        captured.append({'kind': kind, 'object': obj.name, 'track': track.name, 'strip': strip.name, 'before': before,
                         'after': strip.frame_start})
    else:
        cm_fail('REFINEMENT_CHANGE_INVALID', str(kind))

def cr_current(scene, row):
    kind = row['kind']
    if kind == 'camera_settings':
        return cr_camera_value(cm_object(row['camera']).data, row['key'])
    if kind == 'camera_offset':
        return list(cm_object(row['camera']).delta_location)
    if kind == 'actor_offset':
        return list(cm_object(row['object']).delta_location)
    if kind == 'light_energy':
        return cm_object(row['light']).data.energy
    if kind == 'exposure':
        return scene.view_settings.exposure
    if kind == 'clip_shift':
        return cm_object(row['object']).animation_data.nla_tracks[row['track']].strips[row['strip']].frame_start
    cm_fail('REFINEMENT_CHANGE_INVALID', str(kind))

def cr_set(scene, row, value):
    kind = row['kind']
    if kind == 'camera_settings':
        cr_set_camera_value(cm_object(row['camera']).data, row['key'], value)
    elif kind == 'camera_offset':
        cm_object(row['camera']).delta_location = value
    elif kind == 'actor_offset':
        cm_object(row['object']).delta_location = value
    elif kind == 'light_energy':
        cm_object(row['light']).data.energy = value
    elif kind == 'exposure':
        scene.view_settings.exposure = value
    elif kind == 'clip_shift':
        cm_object(row['object']).animation_data.nla_tracks[row['track']].strips[row['strip']].frame_start_ui = value
    else:
        cm_fail('REFINEMENT_CHANGE_INVALID', str(kind))

def cr_same(a, b):
    if isinstance(a, list) or isinstance(b, list):
        return len(a) == len(b) and all(abs(x - y) <= 1e-6 for x, y in zip(a, b))
    return abs(a - b) <= 1e-6

def cr_revert_rows(scene, rows):
    for row in reversed(rows):
        cr_set(scene, row, row['before'])

def cr_apply(params):
    scene, src, cin = cm_candidate(params)
    captured = []
    touched = []
    dry = bool(params.get('dry_run'))
    try:
        for change in params['changes']:
            cr_apply_one(params, scene, change, captured, touched, dry)
    except Exception:
        if not dry:
            cr_revert_rows(scene, captured)
        raise
    result = {'captured': captured, 'touched': touched, 'owned': {}, 'dry_run': dry}
    if not dry:
        result['fingerprints'] = cm_fingerprints(params)
    return result

def cr_revert(params):
    scene, src, cin = cm_candidate(params)
    cr_revert_rows(scene, params['captured'])
    mismatched = [i for i, row in enumerate(params['captured']) if not cr_same(cr_current(scene, row), row['before'])]
    return {'reverted': len(params['captured']), 'verified': not mismatched, 'mismatched': mismatched, 'touched': [],
            'owned': {}, 'fingerprints': cm_fingerprints(params)}

def cr_mark(params):
    # A custom property on the owned candidate scene: not part of the candidate digest, so acceptance cannot
    # invalidate the reviewed preview it is based on (verified by the host after the call).
    scene, src, cin = cm_candidate(params)
    scene['production_m10_lifecycle'] = params['lifecycle']
    scene['production_m10_review'] = params['review_reference']
    return {'scene': scene.name, 'lifecycle': scene['production_m10_lifecycle'], 'touched': [], 'owned': {},
            'fingerprints': cm_fingerprints(params)}
'''

RENDER = RUNTIME + '\nresult = cr_render(params)\n'
MEASURE = RUNTIME + '\nresult = cr_measure(params)\n'
INFO = RUNTIME + '\nresult = cr_info(params)\n'
APPLY = RUNTIME + '\nresult = cr_apply(params)\n'
REVERT = RUNTIME + '\nresult = cr_revert(params)\n'
MARK = RUNTIME + '\nresult = cr_mark(params)\n'
PAYLOADS = {'render': RENDER, 'measure': MEASURE, 'info': INFO, 'apply': APPLY, 'revert': REVERT, 'mark': MARK}

# ---------------------------------------------------------------------------------------------------- pure helpers


def _fail(code, detail):
    raise ValueError(code + ': ' + detail)


def _short(exc, limit=500):
    return str(exc)[:limit]


def _blender_name(value, field):
    if not isinstance(value, str) or not value or value.strip() != value or len(value.encode('utf-8')) > 63:
        _fail('CINEMATIC_NAME_INVALID', field + ' must be a Blender name of 1-63 bytes without surrounding spaces')
    return value


def _strings(value, field, maximum_items=200, maximum_length=200):
    if not isinstance(value, list) or len(value) > maximum_items:
        _fail('VISUAL_REVIEW_INVALID', field + ' must be a list of at most ' + str(maximum_items) + ' strings')
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > maximum_length:
            _fail('VISUAL_REVIEW_INVALID', field + ' items must be non-empty text')
    return list(value)


def render_size(size, resolution):
    """Output pixels with the candidate's aspect ratio; `size` is the long edge."""
    size = model.number(size, 'options.size', SIZE_RANGE[0], SIZE_RANGE[1], integer=True)
    width = resolution[0] * resolution[2] / 100.0
    height = resolution[1] * resolution[2] / 100.0
    if width <= 0 or height <= 0:
        _fail('CINEMATIC_RESOLUTION_INVALID', 'candidate render resolution is empty')
    if width >= height:
        return size, max(16, int(round(size * height / width)))
    return max(16, int(round(size * width / height))), size


def expected_camera(markers, frame):
    """Host replica of cr_marker_camera: camera markers [[frame, camera]] sorted by frame."""
    rows = sorted([m[0], m[-1]] for m in markers if m[-1])
    if not rows:
        return None
    chosen = rows[0][1]
    for marker_frame, camera in rows:
        if marker_frame <= frame:
            chosen = camera
    return chosen


def segments(frame_cameras):
    """Run-length [[camera, first_frame, last_frame]] of consecutive frames with the same camera."""
    rows = []
    for frame, camera in frame_cameras:
        if rows and rows[-1][0] == camera and rows[-1][2] == frame - 1:
            rows[-1][2] = frame
        else:
            rows.append([camera, frame, frame])
    return rows


def plan_shots(plan):
    rows = []
    for index, shot in enumerate(plan.get('shots') or []):
        rows.append({'shot_id': str(shot.get('shot_id') or 'SHOT_' + str(index + 1).zfill(2)),
                     'frame_start': int(shot['frame_start']), 'frame_end': int(shot['frame_end']),
                     'type': shot.get('type') or shot.get('shot_type'),
                     'purpose': shot.get('purpose') or shot.get('narrative_purpose') or shot.get('description'),
                     'beat_id': shot.get('beat_id'), 'camera_id': shot.get('camera_id'),
                     'representative_frame': shot.get('representative_frame'), 'source': 'PLAN'})
    return rows


def marker_shots(info):
    """Shots derived from the candidate's camera markers when the plan has none (one shot per cut)."""
    start, end = info['frame_start'], info['frame_end']
    if not any(m[-1] for m in info['markers']):
        return [{'shot_id': 'SHOT_01', 'frame_start': start, 'frame_end': end, 'purpose': None, 'beat_id': None,
                 'camera_id': None, 'representative_frame': None, 'source': 'SCENE_CAMERA'}]
    runs = segments([(f, expected_camera(info['markers'], f)) for f in range(start, end + 1)])
    return [{'shot_id': 'CUT_' + str(i + 1).zfill(2), 'frame_start': first, 'frame_end': last, 'purpose': None,
             'beat_id': None, 'camera_id': None, 'representative_frame': None, 'source': 'MARKERS', 'camera': camera}
            for i, (camera, first, last) in enumerate(runs)]


def beat_rows(plan):
    beats = plan.get('beats') or {}
    rows = beats.get('beats') if isinstance(beats, dict) else beats
    return [b for b in rows or [] if isinstance(b, dict)]


def representative_frame(shot, beats):
    """Explicit representative frame, else the frame of the shot's beat, else the centre of the shot."""
    start, end = shot['frame_start'], shot['frame_end']
    frame = shot.get('representative_frame')
    if isinstance(frame, int) and not isinstance(frame, bool) and start <= frame <= end:
        return frame, 'SHOT_REPRESENTATIVE_FRAME', None
    for beat in beats:
        if shot.get('beat_id') is not None and beat.get('beat_id') == shot.get('beat_id'):
            for key in ('frame', 'key_frame'):
                value = beat.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and start <= value <= end:
                    return value, 'BEAT', beat.get('purpose') or beat.get('description') or beat.get('kind')
    return (start + end) // 2, 'CENTER', None


def next_chunk(elapsed_s, frames):
    """Frames for the next render call from the measured seconds per image (first call renders one frame)."""
    per_image = max(elapsed_s / max(1, frames), 1e-3)
    return max(1, min(MAX_CHUNK_FRAMES, int(CHUNK_BUDGET_S / per_image)))


def apng_info(path):
    raw = Path(path).read_bytes()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        return {'valid': False, 'reason': 'not a PNG'}
    offset, frames, controls, size = 8, None, 0, None
    while offset + 8 <= len(raw):
        length = struct.unpack('>I', raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:offset + 8 + length]
        if kind == b'IHDR':
            size = struct.unpack('>II', data[:8])
        elif kind == b'acTL':
            frames = struct.unpack('>I', data[:4])[0]
        elif kind == b'fcTL':
            controls += 1
        offset += 12 + length
        if kind == b'IEND':
            break
    return {'valid': frames is not None and frames == controls, 'frames': frames, 'frame_controls': controls,
            'width': size[0] if size else None, 'height': size[1] if size else None}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def camera_jerk(rows, fps):
    """Max camera jerk (m/s^3) inside continuous same-camera runs; rows are [frame, camera, location, stats]."""
    best = None
    windows = 0
    run = []

    def scan(run):
        nonlocal best, windows
        for i in range(len(run) - 3):
            p = [run[i + k][2] for k in range(4)]
            if any(q is None or any(v is None for v in q) for q in p):
                continue
            value = math.sqrt(sum(((p[3][a] - 3 * p[2][a] + 3 * p[1][a] - p[0][a]) * fps ** 3) ** 2 for a in range(3)))
            windows += 1
            if best is None or value > best[0]:
                best = (value, run[i + 3][0], run[i][1])

    for row in sorted(rows, key=lambda r: r[0]):
        if row[1] is None:
            continue
        if run and (row[1] != run[-1][1] or row[0] != run[-1][0] + 1):
            scan(run)
            run = []
        run.append(row)
    scan(run)
    if best is None:
        _fail('REFINEMENT_METRIC_UNAVAILABLE', 'camera_max_jerk needs 4 consecutive frames of one camera')
    return {'value': best[0], 'worst_frame': best[1], 'worst_camera': best[2], 'samples': windows}


def compute_metric(target, goal, scope, rows, fps):
    """Aggregate per-frame measurement rows into the refinement metric (same definition before and after)."""
    camera = scope.get('camera')
    actor = scope.get('actor')
    rows = [r for r in rows if r[1] is not None and (camera is None or r[1] == camera)]
    if target == 'camera_max_jerk':
        result = camera_jerk(rows, fps)
    else:
        samples = []
        for frame, _, _, stats in rows:
            for actor_id, row in sorted(stats.items()):
                if actor and actor_id != actor:
                    continue
                if target == 'visibility_min_fraction':
                    value = row.get('visible_fraction')
                elif not row.get('in_front') or row.get('height') is None:
                    continue  # the projected height is undefined while a corner is behind the camera
                elif target == 'actor_screen_height':
                    value = abs(row['height'] - goal)
                elif target == 'headroom_error':
                    value = abs((1.0 - row['top']) - goal)
                else:
                    _fail('REFINEMENT_TARGET_UNSUPPORTED', target)
                if _finite(value):
                    samples.append((value, frame, actor_id))
        if not samples:
            _fail('REFINEMENT_METRIC_UNAVAILABLE', target + ' has no measurable frame in the requested scope')
        worst = min(samples) if target == 'visibility_min_fraction' else max(samples)
        result = {'value': worst[0], 'worst_frame': worst[1], 'worst_actor': worst[2], 'samples': len(samples)}
    spec = REFINE_TARGETS[target]
    result.update(target=target, goal=goal, scope=scope, frames=len(rows), units=spec['units'],
                  definition=spec['definition'], direction=spec['direction'], source='MEASURED_IN_BLENDER')
    return result


def improvement(direction, before, after):
    return (before - after) if direction == 'lower' else (after - before)


def normalize_changes(changes, plan):
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        _fail('REFINEMENT_CHANGES_REQUIRED', 'changes must list 1-' + str(MAX_CHANGES) + ' changes')
    actors = {a['actor_id']: a['object'] for a in plan.get('actors') or []}
    rows, seen = [], set()
    for change in changes:
        if not isinstance(change, dict):
            _fail('REFINEMENT_CHANGE_INVALID', 'each change is an object with a kind')
        kind = model.choice(change.get('kind'), 'changes.kind', CHANGE_KINDS)
        if kind == 'camera_settings':
            c = model.closed(change, {'kind', 'camera', 'settings'}, 'REFINEMENT_CHANGE_INVALID')
            settings = c.get('settings')
            if not isinstance(settings, dict) or not settings:
                _fail('REFINEMENT_CHANGE_INVALID', 'camera_settings needs settings {' + ', '.join(sorted(CAMERA_SETTINGS)) + '}')
            model.closed(settings, set(CAMERA_SETTINGS), 'REFINEMENT_CHANGE_INVALID')
            row = {'kind': kind, 'camera': _blender_name(c.get('camera'), 'camera'),
                   'settings': {k: model.number(v, 'settings.' + k, *CAMERA_SETTINGS[k]) for k, v in sorted(settings.items())}}
            keys = [(kind, row['camera'], k) for k in row['settings']]
        elif kind == 'camera_offset':
            c = model.closed(change, {'kind', 'camera', 'offset'}, 'REFINEMENT_CHANGE_INVALID')
            row = {'kind': kind, 'camera': _blender_name(c.get('camera'), 'camera'),
                   'offset': model.vector(c.get('offset'), 'offset', 3, -100.0, 100.0)}
            keys = [(kind, row['camera'])]
        elif kind == 'light_energy':
            c = model.closed(change, {'kind', 'light', 'energy'}, 'REFINEMENT_CHANGE_INVALID')
            row = {'kind': kind, 'light': _blender_name(c.get('light'), 'light'),
                   'energy': model.number(c.get('energy'), 'energy', 0.0, 1e7)}
            keys = [(kind, row['light'])]
        elif kind == 'exposure':
            c = model.closed(change, {'kind', 'exposure'}, 'REFINEMENT_CHANGE_INVALID')
            row = {'kind': kind, 'exposure': model.number(c.get('exposure'), 'exposure', -10.0, 10.0)}
            keys = [(kind,)]
        elif kind == 'actor_offset':
            c = model.closed(change, {'kind', 'actor', 'offset'}, 'REFINEMENT_CHANGE_INVALID')
            if c.get('actor') not in actors:
                _fail('ACTOR_NOT_IN_CINEMATIC', str(c.get('actor')))
            row = {'kind': kind, 'actor': c['actor'], 'object': actors[c['actor']],
                   'offset': model.vector(c.get('offset'), 'offset', 3, -50.0, 50.0)}
            keys = [(kind, row['object'])]
        else:
            c = model.closed(change, {'kind', 'actor', 'track', 'strip', 'frames'}, 'REFINEMENT_CHANGE_INVALID')
            if c.get('actor') not in actors:
                _fail('ACTOR_NOT_IN_CINEMATIC', str(c.get('actor')))
            frames = model.number(c.get('frames'), 'frames', -10000, 10000, integer=True)
            if frames == 0:
                _fail('REFINEMENT_CHANGE_INVALID', 'clip_shift frames must be non-zero')
            row = {'kind': kind, 'actor': c['actor'], 'object': actors[c['actor']],
                   'track': _blender_name(c.get('track'), 'track'), 'strip': _blender_name(c.get('strip'), 'strip'),
                   'frames': frames}
            keys = [(kind, row['object'], row['track'], row['strip'])]
        for key in keys:
            if key in seen:
                _fail('REFINEMENT_CHANGE_DUPLICATE', ' '.join(str(k) for k in key))
            seen.add(key)
        rows.append(row)
    return rows


def refine_request(opts, plan):
    """Pure validation of a refinement request: an invalid target never consumes a cycle."""
    target = opts.get('target')
    if target not in REFINE_TARGETS:
        _fail('REFINEMENT_TARGET_UNSUPPORTED', 'measured targets: ' + ', '.join(sorted(REFINE_TARGETS)))
    spec = REFINE_TARGETS[target]
    goal = None
    if spec['goal'] is not None:
        if 'goal' not in opts:
            _fail('REFINEMENT_GOAL_REQUIRED', target + ' measures the distance to a goal the user declares')
        goal = model.number(opts['goal'], 'goal', *spec['goal'])
    elif 'goal' in opts:
        _fail('REFINEMENT_GOAL_UNSUPPORTED', target + ' has no goal')
    scope = model.closed(opts.get('scope') or {}, {'camera', 'actor', 'frame_start', 'frame_end'}, 'REFINEMENT_SCOPE_INVALID')
    if 'camera' in scope:
        _blender_name(scope['camera'], 'scope.camera')
    if 'actor' in scope and scope['actor'] not in {a['actor_id'] for a in plan.get('actors') or []}:
        _fail('ACTOR_NOT_IN_CINEMATIC', str(scope['actor']))
    for key in ('frame_start', 'frame_end'):
        if key in scope:
            scope[key] = model.number(scope[key], 'scope.' + key, -1_048_574, 1_048_574, integer=True)
    if 'frame_start' in scope and 'frame_end' in scope and scope['frame_start'] > scope['frame_end']:
        _fail('REFINEMENT_SCOPE_INVALID', 'frame_start > frame_end')
    return {'target': target, 'goal': goal, 'scope': scope, 'direction': spec['direction'], 'source': spec['source'],
            'changes': normalize_changes(opts.get('changes'), plan)}


def minimum_improvement(value):
    if not _finite(value) or value <= 0:
        _fail('REFINEMENT_THRESHOLD_INVALID', 'declare a positive finite minimum_improvement in the metric units')
    return float(value)


def _forbidden_keys(value, path='review'):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_REVIEW_KEYS:
                found.append(path + '.' + key)
            found += _forbidden_keys(item, path + '.' + str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _forbidden_keys(item, path + '[' + str(index) + ']')
    return found


def checklist_template():
    return {key: {'label': label, 'status': 'NOT_FILLED', 'note': ''} for key, label in DIRECTION_CHECKLIST}


def normalize_checklist(value):
    """Reviewer-filled art-direction checklist; missing items stay NOT_FILLED (never auto-completed)."""
    filled = checklist_template()
    if value is None:
        return filled, False
    if not isinstance(value, dict):
        _fail('VISUAL_REVIEW_INVALID', 'direction_checklist must map items to {status, note}')
    model.closed(value, {k for k, _ in DIRECTION_CHECKLIST}, 'VISUAL_REVIEW_INVALID')
    for key, row in value.items():
        if not isinstance(row, dict):
            _fail('VISUAL_REVIEW_INVALID', 'direction_checklist.' + key + ' must be {status, note}')
        model.closed(row, {'status', 'note', 'shots'}, 'VISUAL_REVIEW_INVALID')
        status = row.get('status')
        if status == 'NOT_FILLED':
            continue
        if status not in CHECKLIST_STATUS:
            _fail('VISUAL_REVIEW_INVALID', 'direction_checklist.' + key + '.status must be one of ' + ', '.join(CHECKLIST_STATUS))
        note = row.get('note', '')
        if not isinstance(note, str) or len(note) > 2000:
            _fail('VISUAL_REVIEW_INVALID', 'direction_checklist.' + key + '.note must be text')
        if status == 'ISSUE' and not note.strip():
            _fail('VISUAL_REVIEW_INVALID', 'direction_checklist.' + key + ': an ISSUE needs a note saying what is wrong')
        filled[key].update(status=status, note=note.strip())
        if 'shots' in row:
            filled[key]['shots'] = _strings(row['shots'], 'direction_checklist.' + key + '.shots')
    complete = all(row['status'] != 'NOT_FILLED' for row in filled.values())
    return filled, complete


def review_gate(review, render, state):
    """M6 review_gate (reviewer/observations/findings/decision/artifact_reference/temporal_reviewed) plus: no score,
    preview of the current candidate fingerprint, every shot of the preview, speeds 1 and 0.25, and a fully
    answered direction checklist for ACCEPT."""
    if not isinstance(review, dict):
        _fail('VISUAL_REVIEW_REQUIRED', 'options.review {reviewer, artistic_observations, decision, artifact_reference, '
              'temporal_reviewed, shots_reviewed, speeds_reviewed}')
    forbidden = _forbidden_keys(review)
    if forbidden:
        _fail('VISUAL_REVIEW_SCORE_FORBIDDEN', ', '.join(forbidden) + ': no quality score; record technical findings '
              'and artistic observations separately')
    model.closed(review, REVIEW_KEYS, 'VISUAL_REVIEW_INVALID')
    observations = review.get('artistic_observations')
    if isinstance(observations, str):
        observations = [observations]
    observations = _strings(observations if observations is not None else [], 'artistic_observations', 100, 4000)
    findings = review.get('technical_findings', [])
    if not isinstance(findings, list) or len(findings) > 100:
        _fail('VISUAL_REVIEW_INVALID', 'technical_findings must be a list')
    for item in findings:
        if not isinstance(item, (str, dict)) or len(json.dumps(item, default=str)) > 4000:
            _fail('VISUAL_REVIEW_INVALID', 'technical_findings items are text or compact objects')
    mapped = {'reviewer': review.get('reviewer'), 'observations': observations, 'findings': findings,
              'decision': review.get('decision'), 'artifact_reference': review.get('artifact_reference'),
              'temporal_reviewed': review.get('temporal_reviewed')}
    if any(v is None for v in mapped.values()):
        _fail('VISUAL_REVIEW_REQUIRED', 'reviewer, artistic_observations, decision, artifact_reference and '
              'temporal_reviewed are required')
    model.text(review['reviewer'], 'reviewer', 200)
    gstore.review_gate(mapped, render)
    fingerprint = (state.get('fingerprints') or {}).get('candidate')
    if not fingerprint or render.get('candidate_fingerprint') != fingerprint:
        _fail('VISUAL_REVIEW_STALE', 'the preview shows another version of the candidate; render it again')
    shots = _strings(review.get('shots_reviewed') or [], 'shots_reviewed')
    missing = sorted({s['shot_id'] for s in render.get('shots') or []} - set(shots))
    if missing:
        _fail('VISUAL_REVIEW_INCOMPLETE', 'shots not reviewed: ' + ', '.join(missing))
    speeds = review.get('speeds_reviewed')
    if not isinstance(speeds, list) or any(not _finite(s) for s in speeds):
        _fail('VISUAL_REVIEW_INCOMPLETE', 'speeds_reviewed must list the playback speeds used (1 and 0.25 required)')
    if not {float(s) for s in speeds} >= set(REVIEW_SPEEDS_REQUIRED):
        _fail('VISUAL_REVIEW_INCOMPLETE', 'the sequence must be watched at normal speed (1) and at quarter speed (0.25)')
    checklist, complete = normalize_checklist(review.get('direction_checklist'))
    if review['decision'] == 'ACCEPT' and not complete:
        unfilled = [k for k, row in checklist.items() if row['status'] == 'NOT_FILLED']
        _fail('VISUAL_REVIEW_CHECKLIST_REQUIRED', 'an ACCEPT answers every art-direction item; NOT_FILLED: ' + ', '.join(unfilled))
    synthetic = SYNTHETIC_LABEL in json.dumps(review, default=str)
    return {'reviewer': review['reviewer'], 'artistic_observations': observations, 'technical_findings': findings,
            'decision': review['decision'], 'artifact_reference': review['artifact_reference'],
            'temporal_reviewed': True, 'shots_reviewed': shots, 'speeds_reviewed': sorted({float(s) for s in speeds}),
            'direction_checklist': checklist, 'checklist_complete': complete, 'synthetic_review': synthetic,
            'candidate_fingerprint': fingerprint}


def accept_gates(state):
    """Every unmet acceptance gate, as CODE: detail strings (empty list = acceptable)."""
    failures = []
    if state.get('pending_refinement'):
        failures.append('REFINEMENT_REVIEW_REQUIRED: a refinement is waiting for its visual review')
    if state.get('state') != 'REVIEWED':
        failures.append('CINEMATIC_STATE_INVALID: ' + str(state.get('state')) + ' is not REVIEWED')
    fingerprint = (state.get('fingerprints') or {}).get('candidate')
    render = state.get('render') or {}
    review = state.get('review') or {}
    if not render.get('temporal_complete') or render.get('candidate_fingerprint') != fingerprint:
        failures.append('TEMPORAL_REVIEW_REQUIRED: no complete preview of the current candidate')
    if review.get('decision') != 'ACCEPT':
        failures.append('CINEMATIC_ACCEPT_REVIEW_REQUIRED: the current review decision is ' + str(review.get('decision')))
    elif review.get('artifact_reference') != render.get('output_dir') or review.get('candidate_fingerprint') != fingerprint:
        failures.append('VISUAL_REVIEW_STALE: the ACCEPT review does not reference the current preview')
    report = state.get('quality_report') or {}
    if not report or report.get('candidate_fingerprint') != fingerprint:
        failures.append('CINEMATIC_QUALITY_REPORT_REQUIRED: run cinematic_quality_report on the current candidate')
    elif not report.get('passed'):
        failures.append('CINEMATIC_QUALITY_REPORT_NOT_PASSED: technical errors or mandatory sections not run')
    return failures


# ---------------------------------------------------------------------------------------------------- quality report


def _mandatory(spec, plan, state):
    actors = [row for row in (state.get('actors') or {}).values() if isinstance(row, dict)]

    def used(field):
        return any(row.get(field) for row in actors)

    rule = spec['mandatory']
    if rule == 'always':
        return True
    if rule == 'multi_actor':
        return len(plan.get('actors') or []) >= 2
    if rule == 'clips':
        return used('clips')
    if rule == 'props':
        return used('props') or bool(state.get('interactions'))
    if rule == 'facial':
        return used('facial')
    if rule == 'secondary':
        return used('secondary')
    if rule == 'lighting':
        return bool(state.get('lighting'))
    return True


def _pool(ctx, path, plan, state, options):
    request = Request(cinematic=str(path), options=dict(options or {}), apply=False)
    return {'ctx': ctx, 'context': ctx, 'req': request, 'request': request, 'path': path, 'plan': plan,
            'state': state, 'bridge': ctx.bridge, 'storage': ctx.storage, 'options': dict(options or {}),
            'cinematic': str(path), 'operation_id': ctx.operation_id}


def call_provider(function, pool):
    """Call a provider whose signature this module does not own: parameters are matched by name from a known pool.
    Returns (result, missing_required_parameters)."""
    args, kwargs, missing = [], {}, []
    for name, parameter in inspect.signature(function).parameters.items():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if name in pool:
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                args.append(pool[name])
            else:
                kwargs[name] = pool[name]
        elif parameter.default is inspect.Parameter.empty:
            missing.append(name)
    if missing:
        return None, missing
    return function(*args, **kwargs), []


def _compact(value, limit=300):
    text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    return text[:limit]


def normalize_section(result):
    """Verdict of a provider result. PASS needs an explicit passed=true, or status success/ok WITH an explicit
    findings list and no error finding: the absence of an exception is never a pass."""
    if not isinstance(result, dict):
        return {'status': 'ERROR', 'errors': ['provider returned ' + type(result).__name__]}
    status = str(result.get('status') or '').lower()
    errors = [_compact(e) for e in result.get('errors') or []] if isinstance(result.get('errors'), list) else []
    findings = result.get('findings') if isinstance(result.get('findings'), list) else None
    severities = {}
    for finding in findings or []:
        if isinstance(finding, dict):
            severity = str(finding.get('severity') or 'unknown').lower()
            severities[severity] = severities.get(severity, 0) + 1
            if severity == 'error':
                errors.append(_compact(finding))
    if status == 'proposal':
        return {'status': 'NOT_RUN', 'reason': 'PROVIDER_RETURNED_PROPOSAL: it measured nothing (apply=false)',
                'errors': [], 'error_count': 0, 'metrics': {}, 'findings_by_severity': {}, 'warnings': []}
    if status in ('not_applicable', 'n/a'):
        verdict = 'NOT_APPLICABLE'
    elif isinstance(result.get('passed'), bool):
        verdict = 'PASS' if result['passed'] and not errors else 'FAIL'
    elif status in ('error', 'failed', 'fail') or errors:
        verdict = 'FAIL'
    elif status in ('ok', 'success', 'pass', 'passed') and findings is not None:
        verdict = 'PASS'
    else:
        verdict = 'INDETERMINATE'
    if verdict == 'FAIL' and not errors:
        # Providers name their failing rows differently (integrity: `changed`); surface them instead of a bare FAIL.
        for key in ('changed', 'failures', 'violations', 'issues'):
            if isinstance(result.get(key), list):
                errors += [key + ': ' + _compact(row) for row in result[key]]
    metrics = result.get('metrics') if isinstance(result.get('metrics'), dict) else {}
    scalars = {k: v for k, v in list(metrics.items())[:60] if isinstance(v, (int, float, str, bool)) or v is None}
    section = {'status': verdict, 'metrics': scalars, 'errors': errors[:20], 'error_count': len(errors),
               'findings_by_severity': severities, 'warnings': [_compact(w) for w in (result.get('warnings') or [])][:10]
               if isinstance(result.get('warnings'), list) else []}
    for key in ('thresholds', 'threshold_source', 'reason', 'limitations', 'synthetic'):
        if key in result:
            section[key] = result[key] if key != 'limitations' else [_compact(x) for x in result[key]][:10]
    return section


def render_section(state):
    render = state.get('render') or {}
    fingerprint = (state.get('fingerprints') or {}).get('candidate')
    if not render:
        return {'status': 'NOT_RUN', 'reason': 'RENDER_PREVIEW_REQUIRED: run cinematic_render_preview', 'provider': 'review.render'}
    metrics = {'frames': render.get('frames'), 'step': render.get('step'), 'seconds_per_image': (render.get('metrics') or {}).get('seconds_per_image'),
               'engine': (render.get('render_backend') or {}).get('engine'),
               'actual_backend': (render.get('render_backend') or {}).get('actual_backend')}
    errors = []
    if render.get('candidate_fingerprint') != fingerprint:
        errors.append('RENDER_PREVIEW_STALE: the preview shows another version of the candidate')
    if not render.get('temporal_complete'):
        errors.append('RENDER_PREVIEW_INCOMPLETE: every frame at step 1 is required')
    if not render.get('state_restored'):
        errors.append('RENDER_STATE_NOT_RESTORED')
    if not render.get('cuts_respected'):
        errors.append('RENDER_CUTS_NOT_RESPECTED: a rendered frame used a camera the markers did not select')
    return {'status': 'FAIL' if errors else 'PASS', 'errors': errors, 'error_count': len(errors), 'metrics': metrics,
            'provider': 'review.render', 'findings_by_severity': {}, 'warnings': []}


def _recorded_current(row, state):
    fingerprint = (state.get('fingerprints') or {}).get('candidate')
    if row.get('candidate_fingerprint') is not None:
        return row['candidate_fingerprint'] == fingerprint
    return row.get('candidate_revision') is not None and row.get('candidate_revision') == state.get('revision', 0)


def _refused(base, provider, exc):
    """A provider that raises on a mandatory section is an ERROR; on an optional one (for example contacts without any
    planned interaction) it simply did not run, and says why."""
    if base['mandatory']:
        return dict(base, status='ERROR', provider=provider, errors=[_short(exc)], error_count=1)
    return dict(base, status='NOT_RUN', provider=provider, reason='PROVIDER_REFUSED: ' + _short(exc), errors=[],
                error_count=0)


def run_section(ctx, path, plan, state, spec, options=None):
    base = {'section': spec['id'], 'label': spec['label'], 'mandatory': _mandatory(spec, plan, state)}
    if spec['id'] == 'render':
        return dict(base, **render_section(state))
    tried = []
    pool = _pool(ctx, path, plan, state, options)
    for module_name, function_name in spec['providers']:
        qualified = module_name + '.' + function_name
        full = __package__ + '.' + module_name
        try:
            module = import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name != full:
                return dict(base, status='ERROR', provider=qualified, errors=['PROVIDER_IMPORT_FAILED: ' + _short(exc)])
            tried.append(qualified + ' (module not present)')
            continue
        except Exception as exc:  # a broken provider is an error, never a silent skip
            return dict(base, status='ERROR', provider=qualified, errors=['PROVIDER_IMPORT_FAILED: ' + _short(exc)])
        function = getattr(module, function_name, None)
        if not callable(function):
            tried.append(qualified + ' (function not present)')
            continue
        try:
            result, missing = call_provider(function, pool)
        except Exception as exc:
            return _refused(base, qualified, exc)
        if missing:
            tried.append(qualified + ' (unsupported parameters: ' + ', '.join(missing) + ')')
            continue
        return dict(base, provider=qualified, **normalize_section(result))
    if spec.get('recorded'):
        owner, key = spec['recorded']
        row = (state.get(owner) or {}).get(key)
        label = 'recorded state.' + owner + '.' + key
        if isinstance(row, dict) and _recorded_current(row, state):
            return dict(base, provider=label, **normalize_section(row))
        tried.append(label + (' (stale: candidate revision ' + str(row.get('candidate_revision')) + ', now '
                              + str(state.get('revision', 0)) + ')' if isinstance(row, dict) else ' (none recorded)'))
    if spec.get('handler'):
        load_handlers()
        function = HANDLERS.get(spec['handler'])
        if function is not None:
            try:
                result = function(ctx, pool['req'])
            except Exception as exc:
                return _refused(base, 'handler ' + spec['handler'], exc)
            return dict(base, provider='handler ' + spec['handler'], **normalize_section(result))
        tried.append('handler ' + spec['handler'] + ' (not registered)')
    return dict(base, status='NOT_RUN', reason='PROVIDER_UNAVAILABLE: ' + '; '.join(tried), provider=None)


def report_verdict(sections):
    errors = [s['section'] for s in sections if s['status'] in ('FAIL', 'ERROR')]
    blocked = [s['section'] for s in sections if s['mandatory'] and s['status'] not in ('PASS', 'NOT_APPLICABLE')]
    return not errors and not blocked, errors, blocked


def technical_findings(sections):
    rows = []
    for s in sections:
        if s['status'] in ('FAIL', 'ERROR'):
            rows += [{'section': s['section'], 'severity': 'error', 'detail': e} for e in s.get('errors') or []] or [
                {'section': s['section'], 'severity': 'error', 'detail': s['status']}]
        elif s['mandatory'] and s['status'] == 'NOT_RUN':
            rows.append({'section': s['section'], 'severity': 'error', 'code': 'MANDATORY_SECTION_NOT_RUN',
                         'detail': s.get('reason')})
        elif s['mandatory'] and s['status'] == 'INDETERMINATE':
            rows.append({'section': s['section'], 'severity': 'error', 'code': 'MANDATORY_SECTION_WITHOUT_VERDICT',
                         'detail': 'the provider returned no explicit passed/findings'})
        elif s['status'] == 'NOT_RUN':
            rows.append({'section': s['section'], 'severity': 'info', 'code': 'OPTIONAL_SECTION_NOT_RUN',
                         'detail': s.get('reason')})
    return rows


# ---------------------------------------------------------------------------------------------------- HTML artefacts


def _json_script(value):
    return json.dumps(value, ensure_ascii=False).replace('</', '<\\/')


def write_storyboard(folder, title, cards, notes):
    e = htmllib.escape
    parts = ['<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
             '<title>' + e(title) + '</title>',
             '<style>body{background:#18181b;color:#e7e7ea;font:14px system-ui,sans-serif;margin:16px}'
             'h1{font-size:20px}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}'
             'article{background:#26262b;border-radius:8px;padding:10px}img{width:100%;border-radius:4px;display:block}'
             'h2{font-size:16px;margin:8px 0 4px}table{width:100%;border-collapse:collapse;font-size:12px}'
             'td{padding:2px 4px;border-top:1px solid #3a3a40;vertical-align:top}td:first-child{color:#a1a1aa;width:38%}'
             '.warn{color:#fbbf24}.note{color:#a1a1aa;font-size:12px}</style>',
             '<h1>' + e(title) + '</h1>']
    for note in notes:
        parts.append('<p class="note">' + e(note) + '</p>')
    parts.append('<main>')
    for card in cards:
        parts.append('<article><img alt="' + e(card['shot_id']) + '" src="' + e(card['image']) + '">')
        parts.append('<h2>' + e(card['shot_id']) + (' · ' + e(str(card['type'])) if card.get('type') else '') + '</h2><table>')
        for label, value in card['rows']:
            css = ' class="warn"' if label.startswith('⚠') else ''
            parts.append('<tr' + css + '><td>' + e(label) + '</td><td>' + e(str(value)) + '</td></tr>')
        parts.append('</table></article>')
    parts.append('</main></html>')
    target = Path(folder) / 'storyboard.html'
    target.write_text('\n'.join(parts), encoding='utf-8')
    return str(target)


def write_player(folder, data):
    """Local player: real frame timing at normal, 1/2 and 1/4 speed, frame stepping, shot jumps, current camera."""
    speeds = ''.join('<option value="' + str(s) + '">' + label + '</option>' for s, label in (
        (1.0, 'Normal (1×)'), (0.5, 'Lenta (1/2)'), (0.25, 'Muy lenta (1/4)')))
    page = ('<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>M10 — Preview temporal</title>'
            '<style>body{background:#111114;color:#e7e7ea;font:15px system-ui,sans-serif;margin:16px;max-width:1100px}'
            'h1{font-size:19px}#stage img{width:100%;background:#000;border-radius:6px;display:block}'
            'button,select,input{font:15px system-ui,sans-serif;margin:4px;padding:6px 10px}'
            '#shots button{font-size:13px}#position{font-variant-numeric:tabular-nums}.note{color:#a1a1aa;font-size:13px}</style>'
            '<h1>M10 — Revisión temporal de la secuencia candidata</h1>'
            '<p class="note">Cortes por markers de la escena candidata (no se desactivan). Revise a velocidad normal y a 1/4. '
            'Espacio: reproducir/pausar · ←/→: frame anterior/siguiente. Animación completa: <a href="sequence.png" '
            'style="color:#93c5fd">sequence.png (APNG)</a>.</p>'
            '<div id="stage"><img id="view" alt="frame"></div>'
            '<div><button id="play">Pausar</button><button id="prev">◀ Frame</button><button id="next">Frame ▶</button>'
            '<label>Velocidad <select id="speed">' + speeds + '</select></label>'
            '<label>Frame <input id="frame" type="number"></label></div>'
            '<p id="position"></p><div id="shots"></div>'
            '<script>const D=' + _json_script(data) + r''';
let at=0,playing=true,last=null,acc=0;
const img=document.getElementById('view'),input=document.getElementById('frame'),speed=document.getElementById('speed'),
button=document.getElementById('play'),pos=document.getElementById('position');
input.min=D.frames[0];input.max=D.frames[D.frames.length-1];input.step=D.step;
// Sliding preload window: decoding every frame of a long sequence up front would take gigabytes.
const cache=new Map(),AHEAD=24;
function preload(i){for(let k=0;k<=AHEAD;k++){const j=(i+k)%D.frames.length;if(!cache.has(j)){const im=new Image();im.src='frames/'+D.files[j];cache.set(j,im);}}
for(const j of [...cache.keys()]){const d=(j-i+D.frames.length)%D.frames.length;if(d>AHEAD)cache.delete(j);}}
function inRange(rows,f,a,b){for(const r of rows){if(f>=r[a]&&f<=r[b])return r;}return null;}
function show(){const f=D.frames[at];preload(at);img.src='frames/'+D.files[at];input.value=f;
const s=inRange(D.segments,f,1,2),p=D.shots.find(x=>f>=x.frame_start&&f<=x.frame_end);
pos.textContent='Frame '+f+' · '+((f-D.frames[0])/D.fps).toFixed(3)+' s · cámara '+(s?s[0]:'?')+' · plano '+(p?p.shot_id:'—')+' · '+speed.value+'×';}
function pause(){playing=false;last=null;acc=0;button.textContent='Reproducir';}
button.onclick=()=>{playing=!playing;last=null;acc=0;button.textContent=playing?'Pausar':'Reproducir';};
document.getElementById('prev').onclick=()=>{pause();at=Math.max(0,at-1);show();};
document.getElementById('next').onclick=()=>{pause();at=Math.min(D.frames.length-1,at+1);show();};
input.onchange=()=>{pause();const i=D.frames.indexOf(Number(input.value));if(i>=0){at=i;}show();};
speed.onchange=show;
document.addEventListener('keydown',ev=>{if(ev.target===input)return;if(ev.key==='ArrowLeft'){document.getElementById('prev').click();}
else if(ev.key==='ArrowRight'){document.getElementById('next').click();}else if(ev.key===' '){ev.preventDefault();button.click();}});
const shots=document.getElementById('shots');
for(const p of D.shots){const b=document.createElement('button');b.textContent=p.shot_id+' ['+p.frame_start+'–'+p.frame_end+']';
b.onclick=()=>{pause();let i=D.frames.findIndex(f=>f>=p.frame_start);at=i<0?0:i;show();};shots.appendChild(b);}
function tick(now){const dt=last===null?0:Math.max(0,now-last);last=now;
if(playing){acc+=dt*Number(speed.value);const per=1000*D.step/D.fps;let moved=false;
while(acc>=per){acc-=per;at=(at+1)%D.frames.length;moved=true;}if(moved)show();}
requestAnimationFrame(tick);}
show();requestAnimationFrame(tick);
</script></html>''')
    target = Path(folder) / 'preview.html'
    target.write_text(page, encoding='utf-8')
    return str(target)


# ---------------------------------------------------------------------------------------------------- host operations


def _info(ctx, plan, state):
    return ctx.bridge.run(INFO, store.assembly_params(plan, state))


def _actor_rows(plan, state, only=None):
    rows = []
    for actor in plan.get('actors') or []:
        if only and actor['actor_id'] != only:
            continue
        rows.append([actor['actor_id'], store.actor_meshes(actor, state) or [actor['object']]])
    return rows


def _check_restored(result, state, label):
    if result.get('status') != 'ok':
        raise ValueError((result.get('errors') or [label + ' failed'])[0])
    if not result.get('state_restored'):
        raise ValueError('CINEMATIC_STATE_NOT_RESTORED: ' + label + ' left the candidate changed: '
                         + json.dumps(result.get('restored')))
    expected = state.get('fingerprints') or {}
    if result['fingerprints'] != {'source': expected.get('source'), 'candidate': expected.get('candidate')}:
        raise ValueError('CINEMATIC_STATE_NOT_RESTORED: ' + label + ' changed the source or candidate fingerprint')


def _folder(ctx, category, sub):
    folder = ctx.storage.reserve(category, ctx.operation_id)
    target = folder / sub
    target.mkdir()
    if len(str(target / 'frame_-00001.png')) > PATH_LIMIT:
        raise ValueError('OUTPUT_PATH_TOO_LONG: use a shorter production artefact store (Windows MAX_PATH)')
    return folder, target


def run_preview(ctx, path, plan, state, opts):
    """Full temporal Workbench preview of the candidate through its cuts, in measured chunks. Pure render: the
    candidate is restored and verified after every chunk; the caller records the result in the ledger."""
    step = model.number(opts.get('step', 1), 'options.step', 1, MAX_STEP, integer=True)
    info = _info(ctx, plan, state)
    width, height = render_size(opts.get('size', DEFAULT_SIZE), info['resolution'])
    frames = list(range(info['frame_start'], info['frame_end'] + 1, step))
    if not frames or len(frames) > MAX_PREVIEW_FRAMES or len(frames) * width * height > MAX_PREVIEW_PIXELS:
        raise ValueError('CAPACITY_EXCEEDED: ' + str(len(frames)) + ' frames of ' + str(width) + 'x' + str(height)
                         + ' exceed the preview budget (' + str(MAX_PREVIEW_FRAMES) + ' frames, '
                         + str(MAX_PREVIEW_PIXELS) + ' px; ' + LIMITS_SOURCE + ')')
    checkpoint = create_checkpoint(ctx.bridge, ctx.storage, 'Before M10 temporal preview', ctx.operation_id,
                                   'cinematic_render_preview')['manifest']
    folder, frames_dir = _folder(ctx, 'cinematic-preview', 'frames')
    params = store.assembly_params(plan, state, {'width': width, 'height': height, 'directory': frames_dir.as_posix(),
                                                 'stats': False, 'actors': []})
    write_json(folder / 'intent.json', dict(params, frames=[frames[0], frames[-1], step], checkpoint=checkpoint))
    began = time.perf_counter()
    rows, chunks, backend, pending, size = [], [], None, list(frames), 1
    while pending:
        part = pending[:size]
        start = time.perf_counter()
        result = ctx.bridge.run(RENDER, dict(params, frames=part))
        elapsed = time.perf_counter() - start
        _check_restored(result, state, 'preview render')
        rows += result['rows']
        backend = result['render_backend']
        chunks.append({'frames': len(part), 'elapsed_s': round(elapsed, 3), 'seconds_per_image': round(elapsed / len(part), 4)})
        pending = pending[len(part):]
        size = next_chunk(elapsed, len(part))
    elapsed_total = time.perf_counter() - began
    files = [{'path': r['path'], 'frame': r['frame']} for r in rows]
    verified = _verify_images({'status': 'ok', 'files': files, 'errors': []}, frames_dir)
    mismatched = [r['frame'] for r in rows if (r['expected_camera'] is not None and r['camera'] != r['expected_camera'])
                  or r['camera_after_render'] != r['camera']]
    shot_list = plan_shots(plan) or marker_shots(info)
    animation = folder / 'sequence.png'
    fps = info['fps'] / info['fps_base']
    write_apng(files, animation, fps / step)
    apng = apng_info(animation)
    runs = segments([(r['frame'], r['camera']) for r in rows]) if step == 1 else [[r['camera'], r['frame'], r['frame']] for r in rows]
    player = write_player(folder, {'frames': [r['frame'] for r in rows], 'files': [Path(r['path']).name for r in rows],
                                   'fps': fps, 'step': step, 'segments': runs,
                                   'shots': [{k: s[k] for k in ('shot_id', 'frame_start', 'frame_end')} for s in shot_list]})
    complete = (step == 1 and [r['frame'] for r in rows] == frames and verified['status'] == 'ok' and not mismatched
                and apng['valid'] and apng['frames'] == len(frames))
    result = {'status': 'ok' if verified['status'] == 'ok' and not mismatched and apng['valid'] else 'error',
              'output_dir': str(folder), 'frames_dir': str(frames_dir), 'temporal_complete': complete,
              'frames': len(rows), 'frame_range': [frames[0], frames[-1]], 'step': step, 'fps': info['fps'],
              'fps_base': info['fps_base'], 'size': [width, height],
              'size_source': 'USER_REQUIREMENT' if 'size' in opts else 'INFERRED_DEFAULT',
              'animations': [{'view': 'SEQUENCE', 'path': str(animation), 'sha256': sha256(animation),
                              'frames': apng['frames'], 'fps': fps / step, 'apng_valid': apng['valid'],
                              'apng_delay_fps': round(fps / step)}],
              'player': player, 'render_backend': backend, 'state_restored': True,
              'candidate_fingerprint': state['fingerprints']['candidate'],
              'source_fingerprint': state['fingerprints']['source'], 'cuts_respected': not mismatched,
              'cut_mismatch_frames': mismatched[:50], 'segments': runs, 'markers': result.get('markers'),
              'shots': [{k: s.get(k) for k in ('shot_id', 'frame_start', 'frame_end', 'source', 'purpose')} for s in shot_list],
              'checkpoint': checkpoint, 'image_errors': verified.get('errors', [])[:10],
              'metrics': {'images': len(rows), 'elapsed_s': round(elapsed_total, 3),
                          'seconds_per_image': round(elapsed_total / max(1, len(rows)), 4), 'chunks': len(chunks),
                          'max_chunk_s': max(c['elapsed_s'] for c in chunks), 'chunk_budget_s': CHUNK_BUDGET_S,
                          'chunk_budget_source': LIMITS_SOURCE},
              'warnings': ['Blender Render Result is replaced by the last rendered frame (scene state is restored).',
                           'APNG frame delay uses round(fps) (animation.generation.render.write_apng); the HTML player '
                           'uses the exact fps/fps_base.']}
    write_json(folder / 'result.json', dict(result, chunks=chunks))
    template = {'review': {'reviewer': '', 'artistic_observations': [], 'technical_findings': [],
                           'decision': 'ACCEPT | REJECT | REFINE', 'artifact_reference': str(folder),
                           'temporal_reviewed': False, 'shots_reviewed': [], 'speeds_reviewed': [],
                           'direction_checklist': checklist_template()},
                'shots_to_review': [s['shot_id'] for s in shot_list], 'speeds_required': list(REVIEW_SPEEDS_REQUIRED),
                'note': 'Template only: fill it after watching the player; nothing here is pre-filled.'}
    write_json(folder / 'review_template.json', template)
    result['review_template'] = str(folder / 'review_template.json')
    return result


def _compact_render(result):
    keys = ('status', 'output_dir', 'temporal_complete', 'frames', 'frame_range', 'step', 'fps', 'fps_base', 'size',
            'animations', 'player', 'render_backend', 'state_restored', 'candidate_fingerprint', 'cuts_respected',
            'segments', 'shots', 'checkpoint', 'metrics', 'review_template')
    return {k: result.get(k) for k in keys}


def measure_rows(ctx, plan, state, scope):
    info = _info(ctx, plan, state)
    start = max(info['frame_start'], scope.get('frame_start', info['frame_start']))
    end = min(info['frame_end'], scope.get('frame_end', info['frame_end']))
    frames = list(range(start, end + 1))
    if not frames:
        _fail('REFINEMENT_SCOPE_INVALID', 'no frame of the candidate range in scope')
    params = store.assembly_params(plan, state, {'actors': _actor_rows(plan, state, scope.get('actor'))})
    rows = []
    for index in range(0, len(frames), MEASURE_CHUNK_FRAMES):
        result = ctx.bridge.run(MEASURE, dict(params, frames=frames[index:index + MEASURE_CHUNK_FRAMES]))
        _check_restored(result, state, 'measurement')
        rows += result['rows']
    return rows, info


def measure_target(ctx, path, plan, state, request):
    if request['source'] == 'BUILTIN':
        rows, info = measure_rows(ctx, plan, state, request['scope'])
        return compute_metric(request['target'], request['goal'], request['scope'], rows, info['fps'] / info['fps_base'])
    section = run_section(ctx, path, plan, state, SECTION_BY_ID[request['source'].split(':', 1)[1]])
    value = (section.get('metrics') or {}).get(request['target'])
    if section.get('status') not in ('PASS', 'FAIL') or not _finite(value):
        _fail('REFINEMENT_TARGET_UNAVAILABLE', request['target'] + ' needs the ' + section['section'] + ' provider ('
              + str(section.get('reason') or section.get('status')) + ')')
    spec = REFINE_TARGETS[request['target']]
    return {'value': float(value), 'target': request['target'], 'source': 'PROVIDER ' + str(section.get('provider')),
            'units': spec['units'], 'definition': spec['definition'], 'direction': spec['direction'], 'scope': {}}


def _review_files(folder, report):
    folder = Path(folder)
    index = 1
    while (folder / ('review.json' if index == 1 else 'review_' + str(index).zfill(3) + '.json')).exists():
        index += 1
    stem = 'review' if index == 1 else 'review_' + str(index).zfill(3)
    write_json(folder / (stem + '.json'), report)
    lines = ['# Revisión de la secuencia cinematográfica M10', '',
             'Candidata: `' + str(report['candidate_fingerprint']) + '` · preview: `' + str(report['artifact_reference']) + '`', '',
             '## TECHNICAL FINDINGS', '', 'Medidas de las herramientas (no las escribe el revisor).', '']
    measured = report['TECHNICAL_FINDINGS']['quality_report']
    lines.append('- quality report vigente: ' + str(measured.get('current')) + ', passed: ' + str(measured.get('passed')))
    for section in measured.get('sections') or []:
        lines.append('  - ' + section['section'] + ': ' + section['status'] + (' (obligatoria)' if section['mandatory'] else ''))
    render = report['TECHNICAL_FINDINGS']['render']
    lines.append('- render: ' + json.dumps(render, ensure_ascii=False, default=str))
    reviewer_findings = report['TECHNICAL_FINDINGS']['reviewer_findings']
    if reviewer_findings:
        lines += ['', 'Hallazgos técnicos anotados por el revisor:']
        lines += ['- ' + _compact(f, 400) for f in reviewer_findings]
    lines += ['', '## ARTISTIC OBSERVATIONS', '', 'Observaciones del revisor (juicio humano, no medidas).', '']
    lines += ['- ' + o for o in report['ARTISTIC_OBSERVATIONS']['observations']]
    lines += ['', '### Guía de dirección artística (rellenada por el revisor)', '']
    for key, row in report['ARTISTIC_OBSERVATIONS']['direction_checklist'].items():
        lines.append('- ' + row['label'] + ': **' + row['status'] + '**' + (' — ' + row['note'] if row['note'] else ''))
    lines += ['', 'Decisión: **' + report['decision'] + '** (' + report['reviewer'] + ')',
              'Planos revisados: ' + ', '.join(report['shots_reviewed']),
              'Velocidades revisadas: ' + ', '.join(str(s) for s in report['speeds_reviewed']), '']
    if report['synthetic_review']:
        lines += ['**SYNTHETIC_REVIEW_FOR_MECHANISM_TEST**: revisión sintética de prueba de mecanismo; no es una revisión visual real.', '']
    lines.append(NO_SCORE)
    (folder / (stem + '.md')).write_text('\n'.join(lines), encoding='utf-8')
    return str(folder / (stem + '.json')), str(folder / (stem + '.md'))


def review_report(state, visual):
    render = state.get('render') or {}
    report = state.get('quality_report') or {}
    current = bool(report) and report.get('candidate_fingerprint') == visual['candidate_fingerprint']
    data = {'schema': 'production.cinematic_review.v1',
            'TECHNICAL_FINDINGS': {
                'quality_report': {'current': current, 'passed': report.get('passed') if current else None,
                                   'report': report.get('report_path') if current else None,
                                   'sections': [{k: s.get(k) for k in ('section', 'status', 'mandatory')}
                                                for s in report.get('sections') or []] if current else []},
                'render': {'frames': render.get('frames'), 'cuts_respected': render.get('cuts_respected'),
                           'state_restored': render.get('state_restored'),
                           'render_backend': (render.get('render_backend') or {}).get('actual_backend'),
                           'seconds_per_image': (render.get('metrics') or {}).get('seconds_per_image')},
                'reviewer_findings': visual['technical_findings'],
                'source': 'measured by production tools; reviewer_findings are labelled as the reviewer wrote them'},
            'ARTISTIC_OBSERVATIONS': {'observations': visual['artistic_observations'],
                                      'direction_checklist': visual['direction_checklist'],
                                      'checklist_complete': visual['checklist_complete']},
            'decision': visual['decision'], 'reviewer': visual['reviewer'],
            'artifact_reference': visual['artifact_reference'], 'shots_reviewed': visual['shots_reviewed'],
            'speeds_reviewed': visual['speeds_reviewed'], 'temporal_reviewed': True,
            'candidate_fingerprint': visual['candidate_fingerprint'], 'synthetic_review': visual['synthetic_review'],
            'no_score': NO_SCORE}
    review_json, review_md = _review_files(visual['artifact_reference'], data)
    record = {'decision': visual['decision'], 'reviewer': visual['reviewer'],
              'artifact_reference': visual['artifact_reference'], 'candidate_fingerprint': visual['candidate_fingerprint'],
              'shots_reviewed': visual['shots_reviewed'], 'speeds_reviewed': visual['speeds_reviewed'],
              'checklist_complete': visual['checklist_complete'], 'synthetic_review': visual['synthetic_review'],
              'review_json': review_json, 'review_md': review_md, 'quality_report_current': current}
    return record


def _note_render_error(path, state, step, exc):
    """A previs/preview render changes nothing persistent, so a timeout does not block the plan: the next mutation's
    freshness check refuses a candidate the render did not restore. The uncertainty is still recorded, never retried."""
    if is_uncertain(exc):
        store.record(path, dict(state, last_render_error={'step': step, 'uncertain': True, 'type': type(exc).__name__,
                                                          'message': _short(exc), 'retry': 'never automatic'}))


def _block_if_uncertain(path, state, step, exc):
    """A transport failure whose outcome is unknown (timeout) may still be running in Blender: never revert or retry
    on top of it. The plan is blocked (M8/M9 pattern) and the checkpoint is the way back."""
    if is_uncertain(exc):
        store.record(path, dict(state, uncertain_operation=True, uncertain_step=step,
                                last_error={'type': type(exc).__name__, 'message': _short(exc)}))
        raise exc


def _revert_cycle(ctx, path, plan, applied, summary, outcome, error=None):
    """Restore the values captured before a refinement and prove it with the fingerprints from before the cycle."""
    pending = applied['pending_refinement']
    params = store.assembly_params(plan, applied, {'captured': pending['captured']})
    try:
        reverted, result = store.mutate(ctx, path, plan, applied, 'refine_revert_' + str(pending['cycle']), REVERT, params,
                                        allowed={'CINEMATIC_CANDIDATE', 'ASSEMBLED', 'REVIEWED'})
    except Exception as exc:
        if not is_uncertain(exc):
            store.record(path, dict(applied, pending_refinement=dict(pending, phase='REVERT_FAILED'),
                                    last_refinement=dict(summary, outcome='REVERT_FAILED', error=_short(exc))))
        raise
    verified = bool(result['verified']) and result['fingerprints'] == pending['previous_fingerprints']
    if verified:
        for key, value in pending['previous'].items():
            reverted[key] = value
    reverted['pending_refinement'] = None
    reverted['last_refinement'] = dict(summary, outcome=outcome if verified else 'REFINEMENT_REVERT_UNVERIFIED',
                                       reverted=True, revert_verified=verified,
                                       fingerprints_after_revert=result['fingerprints'],
                                       error=_short(error) if error is not None else None)
    store.record(path, reverted)
    return reverted, verified


# ---------------------------------------------------------------------------------------------------- handlers


@handler('cinematic_generate_previs')
def generate_previs(ctx, req):
    """One representative Workbench frame per shot through the candidate's cuts, plus a local storyboard."""
    opts = model.closed(req.options, {'size', 'shots'})
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, model.ASSEMBLY_STATES)
    require_mutable_asset(ctx.bridge, ctx.storage, 'M10 previs render')
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    info = _info(ctx, plan, state)
    width, height = render_size(opts.get('size', DEFAULT_SIZE), info['resolution'])
    shots = plan_shots(plan) or marker_shots(info)
    if 'shots' in opts:
        wanted = _strings(opts['shots'], 'options.shots')
        unknown = sorted(set(wanted) - {s['shot_id'] for s in shots})
        if unknown:
            _fail('CINEMATIC_SHOT_UNKNOWN', ', '.join(unknown))
        shots = [s for s in shots if s['shot_id'] in wanted]
    beats = beat_rows(plan)
    for shot in shots:
        shot['frame'], shot['frame_source'], shot['beat_purpose'] = representative_frame(shot, beats)
    frames = sorted({s['frame'] for s in shots})
    checkpoint = create_checkpoint(ctx.bridge, ctx.storage, 'Before M10 previs', ctx.operation_id,
                                   'cinematic_generate_previs')['manifest']
    folder, images = _folder(ctx, 'cinematic-previs', 'shots')
    params = store.assembly_params(plan, state, {'width': width, 'height': height, 'directory': images.as_posix(),
                                                 'stats': True, 'actors': _actor_rows(plan, state), 'frames': frames})
    began = time.perf_counter()
    try:
        result = ctx.bridge.run(RENDER, params)
    except Exception as exc:
        _note_render_error(path, state, 'generate_previs', exc)
        raise
    elapsed = time.perf_counter() - began
    _check_restored(result, state, 'previs render')
    by_frame = {r['frame']: r for r in result['rows']}
    verified = _verify_images({'status': 'ok', 'files': [{'path': r['path'], 'frame': r['frame']} for r in result['rows']],
                               'errors': []}, images)
    fps = info['fps'] / info['fps_base']
    cameras = {cid: (row or {}).get('object') for cid, row in (state.get('cameras') or {}).items() if isinstance(row, dict)}
    cards, rows, warnings = [], [], []
    for shot in shots:
        row = by_frame[shot['frame']]
        planned = cameras.get(shot.get('camera_id')) if shot.get('camera_id') else None
        cut_ok = (row['expected_camera'] is None or row['camera'] == row['expected_camera']) and row['camera_after_render'] == row['camera']
        if planned and planned != row['camera']:
            warnings.append('SHOT_CAMERA_MISMATCH: ' + shot['shot_id'] + ' plans ' + str(planned) + ' but the markers show '
                            + row['camera'] + ' at frame ' + str(shot['frame']))
        visible = {aid: s for aid, s in (row.get('actors') or {}).items() if (s.get('visible_fraction') or 0) > 0}
        entry = {'shot_id': shot['shot_id'], 'frame': shot['frame'], 'frame_source': shot['frame_source'],
                 'frame_range': [shot['frame_start'], shot['frame_end']],
                 'time_s': round((shot['frame'] - info['frame_start']) / fps, 4), 'camera': row['camera'],
                 'expected_camera': row['expected_camera'], 'camera_after_render': row['camera_after_render'],
                 'planned_camera': planned, 'cut_respected': cut_ok, 'camera_pose': row['camera_pose'],
                 'actors': row.get('actors'), 'actors_visible': sorted(visible),
                 'purpose': shot.get('purpose') or shot.get('beat_purpose'), 'type': shot.get('type'),
                 'image': row['path'], 'sha256': sha256(Path(row['path']))}
        rows.append(entry)
        pose = row['camera_pose']
        cards.append({'shot_id': shot['shot_id'], 'type': shot.get('type'), 'image': 'shots/' + Path(row['path']).name,
                      'rows': [('Frame', str(shot['frame']) + ' (' + shot['frame_source'] + ') de ' + str(shot['frame_start']) + '–' + str(shot['frame_end'])),
                               ('Tiempo', str(entry['time_s']) + ' s'),
                               ('Cámara', row['camera'] + (' (marker)' if row['expected_camera'] else ' (scene.camera)')),
                               ('Lente', str(pose['lens_mm']) + ' mm · sensor ' + str(pose['sensor_width_mm']) + ' mm'),
                               ('Posición cámara (m)', ', '.join(str(v) for v in pose['location'])),
                               ('Rotación cámara (rad)', ', '.join(str(v) for v in pose['rotation_euler_rad'])),
                               ('Actores visibles', ', '.join(sorted(visible)) or 'ninguno'),
                               ('Propósito narrativo', entry['purpose'] or 'no declarado en el plan')]
                      + ([('⚠ Corte', 'la cámara renderizada no coincide con la del marker')] if not cut_ok else [])
                      + ([('⚠ Cámara planificada', str(planned))] if planned and planned != row['camera'] else [])})
    storyboard = write_storyboard(folder, 'M10 — Previs por plano', cards, [
        'Workbench (rasterizador GPU), un frame representativo por plano; los cortes los deciden los markers de la '
        'escena candidata. Datos medidos en Blender; el propósito narrativo es el del plan.'])
    ok = verified['status'] == 'ok' and all(r['cut_respected'] for r in rows)
    summary = {'status': 'ok' if ok else 'error', 'output_dir': str(folder), 'storyboard': storyboard,
               'shots': rows, 'size': [width, height], 'render_backend': result['render_backend'],
               'state_restored': True, 'candidate_fingerprint': state['fingerprints']['candidate'],
               'checkpoint': checkpoint, 'warnings': warnings, 'image_errors': verified.get('errors', [])[:10],
               'metrics': {'images': len(frames), 'elapsed_s': round(elapsed, 3),
                           'seconds_per_image': round(elapsed / max(1, len(frames)), 4)}}
    write_json(folder / 'previs.json', summary)
    new = dict(state, previs={'output_dir': str(folder), 'storyboard': storyboard, 'shots': len(rows),
                              'candidate_fingerprint': state['fingerprints']['candidate'], 'status': summary['status']})
    store.record(path, new)
    compact_rows = [{k: r[k] for k in ('shot_id', 'frame', 'frame_source', 'time_s', 'camera', 'expected_camera',
                                       'cut_respected', 'actors_visible', 'purpose', 'image')} for r in rows]
    return {'status': 'previs_rendered' if ok else 'error', **store.public(path, plan, new), 'output_dir': str(folder),
            'storyboard': storyboard, 'previs_json': str(folder / 'previs.json'), 'shots': compact_rows,
            'render_backend': result['render_backend'], 'state_restored': True, 'warnings': warnings,
            'metrics': summary['metrics'], 'checkpoint': checkpoint}


@handler('cinematic_render_preview')
def render_preview(ctx, req):
    opts = model.closed(req.options, {'size', 'step'})
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, model.ASSEMBLY_STATES)
    if (state.get('pending_refinement') or {}).get('phase') not in (None, 'REVIEW_REQUIRED'):
        _fail('REFINEMENT_INCOMPLETE', 'a refinement attempt did not finish; inspect last_refinement and the checkpoint')
    require_mutable_asset(ctx.bridge, ctx.storage, 'M10 temporal preview')
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    try:
        result = run_preview(ctx, path, plan, state, opts)
    except Exception as exc:
        _note_render_error(path, state, 'render_preview', exc)
        raise
    new = dict(state, render=_compact_render(result), review=None)
    if new.get('state') == 'REVIEWED':
        new['state'] = 'ASSEMBLED'  # a new preview needs its own review
    store.record(path, new)
    return {'status': 'rendered' if result['status'] == 'ok' else 'error', **store.public(path, plan, new),
            **{k: result[k] for k in ('output_dir', 'temporal_complete', 'frames', 'frame_range', 'step', 'fps',
                                      'fps_base', 'animations', 'player', 'render_backend', 'state_restored',
                                      'candidate_fingerprint', 'cuts_respected', 'cut_mismatch_frames', 'segments',
                                      'shots', 'metrics', 'review_template', 'checkpoint', 'warnings')}}


@handler('cinematic_review')
def review(ctx, req):
    opts = model.closed(req.options, {'review'})
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, {'ASSEMBLED', 'REVIEWED'})
    pending = state.get('pending_refinement')
    if pending and pending.get('phase') != 'REVIEW_REQUIRED':
        _fail('REFINEMENT_INCOMPLETE', 'the pending refinement is in phase ' + str(pending.get('phase')))
    visual = review_gate(opts.get('review'), state.get('render'), state)
    store.fresh(ctx.bridge, ctx.storage, plan, state)  # Blender still shows the candidate the preview shows
    record = review_report(state, visual)
    if not pending:
        new = dict(state, state='REVIEWED', review=record)
        store.record(path, new)
        return {'status': 'reviewed', **store.public(path, plan, new), 'review': record}
    summary = dict(pending['summary'], visual_review=record)
    if visual['decision'] == 'ACCEPT':
        new = dict(state, state='REVIEWED', review=record, pending_refinement=None,
                   last_refinement=dict(summary, outcome='REFINEMENT_KEPT', kept=True))
        store.record(path, new)
        return {'status': 'refined', **store.public(path, plan, new), 'review': record,
                'refinement': new['last_refinement']}
    reverted, verified = _revert_cycle(ctx, path, plan, state, dict(summary, kept=False), 'REFINEMENT_REVERTED_BY_REVIEW')
    return {'status': 'reverted' if verified else 'error', **store.public(path, plan, reverted), 'review': record,
            'refinement': reverted['last_refinement']}


@handler('cinematic_refine')
def refine(ctx, req):
    """FINDINGS -> PROPOSAL -> APPLY on the candidate -> measured QA of the target before/after -> temporal preview ->
    mandatory review. Not improved -> REVERT. At most 3 cycles; the cycle is spent before the attempt."""
    opts = model.closed(req.options, {'target', 'goal', 'scope', 'changes', 'minimum_improvement', 'render'})
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, {'ASSEMBLED', 'REVIEWED'})
    request = refine_request(opts, plan)
    render_options = model.closed(opts.get('render') or {}, {'size'})
    if state.get('pending_refinement'):
        _fail('REFINEMENT_REVIEW_REQUIRED', 'review the pending refinement with cinematic_review first')
    gstore.refinement_gate(state)
    minimum = minimum_improvement(opts.get('minimum_improvement')) if req.apply else None
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    params = store.assembly_params(plan, state, {'changes': request['changes'],
                                                 'actor_objects': [a['object'] for a in plan.get('actors') or []],
                                                 'owned_prefix': model.owned_prefix(plan['generation_id'], (state.get('assembly') or {}).get('version', 1))})
    preflight = ctx.bridge.run(APPLY, dict(params, dry_run=True))  # ownership/animation checks before spending a cycle
    before = measure_target(ctx, path, plan, state, request)
    if not req.apply:
        return {'status': 'proposal', 'apply': False, **store.public(path, plan, state), 'findings': before,
                'proposal': {'target': request['target'], 'goal': request['goal'], 'scope': request['scope'],
                             'changes': request['changes'], 'current_values': preflight['captured']},
                'remaining_cycles': 3 - state.get('refinement_cycles', 0),
                'next': 'call again with apply=true and a positive minimum_improvement in ' + before['units']}
    require_mutable_asset(ctx.bridge, ctx.storage, 'M10 refinement')
    cycle = state.get('refinement_cycles', 0) + 1
    consumed = dict(state, refinement_cycles=cycle)
    store.record(path, consumed)  # spent before the attempt: failures and reversions never give it back
    summary = {'cycle': cycle, 'target': request['target'], 'goal': request['goal'], 'scope': request['scope'],
               'direction': request['direction'], 'before': before['value'], 'before_detail': before,
               'minimum_improvement': minimum, 'changes': request['changes']}
    try:
        applied, result = store.mutate(ctx, path, plan, consumed, 'refine_' + str(cycle), APPLY, params,
                                       allowed={'ASSEMBLED', 'REVIEWED'})
    except Exception as exc:
        if not is_uncertain(exc):
            store.record(path, dict(consumed, last_refinement=dict(summary, outcome='SAFE_REJECTION', error=_short(exc))))
        raise
    applied['state'] = 'ASSEMBLED'  # still a complete montage; the change needs a new preview and review
    applied['pending_refinement'] = {'phase': 'MEASURING', 'cycle': cycle, 'captured': result['captured'],
                                     'previous': {k: deepcopy(consumed.get(k)) for k in SNAPSHOT_KEYS},
                                     'previous_fingerprints': consumed['fingerprints'], 'summary': summary}
    store.record(path, applied)
    try:
        after = measure_target(ctx, path, plan, applied, request)
    except Exception as exc:
        _block_if_uncertain(path, applied, 'refine_measurement', exc)
        reverted, verified = _revert_cycle(ctx, path, plan, applied, summary, 'MEASUREMENT_FAILED_AND_REVERTED', exc)
        return {'status': 'error', **store.public(path, plan, reverted), 'refinement': reverted['last_refinement'],
                'errors': [_short(exc)]}
    gained = improvement(request['direction'], before['value'], after['value'])
    summary.update(after=after['value'], after_detail=after, improvement=gained, improved=gained >= minimum,
                   captured=result['captured'])
    if gained < minimum:
        reverted, verified = _revert_cycle(ctx, path, plan, applied, summary, 'REFINEMENT_REVERTED')
        return {'status': 'reverted' if verified else 'error', **store.public(path, plan, reverted),
                'refinement': reverted['last_refinement'], 'remaining_cycles': 3 - cycle}
    try:
        preview = run_preview(ctx, path, plan, applied, render_options)
    except Exception as exc:
        _block_if_uncertain(path, applied, 'refine_preview', exc)
        reverted, verified = _revert_cycle(ctx, path, plan, applied, summary, 'PREVIEW_FAILED_AND_REVERTED', exc)
        return {'status': 'error', **store.public(path, plan, reverted), 'refinement': reverted['last_refinement'],
                'errors': [_short(exc)]}
    applied['render'] = _compact_render(preview)
    applied['review'] = None
    applied['pending_refinement'] = dict(applied['pending_refinement'], phase='REVIEW_REQUIRED', summary=summary)
    store.record(path, applied)
    return {'status': 'visual_review_required', **store.public(path, plan, applied),
            'refinement': {k: summary[k] for k in ('cycle', 'target', 'goal', 'before', 'after', 'improvement',
                                                    'minimum_improvement', 'improved')},
            'preview': {k: preview[k] for k in ('output_dir', 'player', 'temporal_complete', 'cuts_respected',
                                                 'review_template', 'shots')},
            'remaining_cycles': 3 - cycle,
            'next': 'watch the preview and record cinematic_review; ACCEPT keeps the change, REJECT/REFINE reverts it'}


@handler('cinematic_quality_report')
def quality_report(ctx, req):
    opts = model.closed(req.options, {'section_options'})
    section_options = opts.get('section_options') or {}
    if not isinstance(section_options, dict):
        _fail('CINEMATIC_OPTION_UNSUPPORTED', 'section_options maps a section id to its options')
    model.closed(section_options, set(SECTION_BY_ID), 'CINEMATIC_OPTION_UNSUPPORTED')
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, model.ASSEMBLY_STATES)
    if state.get('pending_refinement'):
        _fail('REFINEMENT_REVIEW_REQUIRED', 'review the pending refinement first')
    observed = store.fresh(ctx.bridge, ctx.storage, plan, state)
    sections = [run_section(ctx, path, plan, state, spec, section_options.get(spec['id'])) for spec in SECTIONS]
    path, plan, state = store.load(ctx.storage, req.cinematic)  # providers may have recorded their own QA rows
    store.fresh(ctx.bridge, ctx.storage, plan, state)  # and none of them may leave the candidate changed
    passed, failing, blocked = report_verdict(sections)
    findings = technical_findings(sections)
    folder = ctx.storage.reserve('cinematic-quality', ctx.operation_id)
    report = {'schema': 'production.cinematic_quality_report.v1', 'passed': passed,
              'candidate_fingerprint': state['fingerprints']['candidate'],
              'source_fingerprint': observed['source']['fingerprint'], 'sections': sections,
              'TECHNICAL_FINDINGS': findings,
              'ARTISTIC_OBSERVATIONS': {'observations': [], 'note': 'Empty by design: written by the human reviewer '
                                        'in cinematic_review, never by this report.'},
              'failing_sections': failing, 'mandatory_not_passed': blocked,
              'rule': 'passed only if no section FAIL/ERROR and every mandatory section is PASS or NOT_APPLICABLE; '
                      'NOT_RUN is never a pass', 'no_score': NO_SCORE}
    write_json(folder / 'quality_report.json', report)
    lines = ['# Informe técnico de calidad M10', '', 'passed: **' + str(passed) + '** · candidata `'
             + report['candidate_fingerprint'] + '`', '', '## TECHNICAL FINDINGS', '',
             '| sección | obligatoria | estado | proveedor | errores |', '|---|---|---|---|---|']
    for s in sections:
        lines.append('| ' + s['label'] + ' | ' + ('sí' if s['mandatory'] else 'no') + ' | ' + s['status'] + ' | '
                     + str(s.get('provider') or '—') + ' | ' + str(s.get('error_count', 0)) + ' |')
    lines += [''] + ['- ' + f['section'] + ': ' + str(f.get('code') or f['severity']) + ' — ' + _compact(f.get('detail'), 300)
                     for f in findings]
    lines += ['', '## ARTISTIC OBSERVATIONS', '', 'Vacías por diseño: las escribe el revisor en `cinematic_review`.', '', NO_SCORE]
    (folder / 'quality_report.md').write_text('\n'.join(lines), encoding='utf-8')
    new = dict(state, quality_report={'passed': passed, 'candidate_fingerprint': report['candidate_fingerprint'],
                                      'report_path': str(folder / 'quality_report.json'),
                                      'sections': [{k: s.get(k) for k in ('section', 'status', 'mandatory', 'provider')}
                                                   for s in sections],
                                      'failing_sections': failing, 'mandatory_not_passed': blocked})
    store.record(path, new)
    return {'status': 'passed' if passed else 'not_passed', **store.public(path, plan, new), 'passed': passed,
            'report': str(folder / 'quality_report.json'), 'report_md': str(folder / 'quality_report.md'),
            'sections': [{k: s.get(k) for k in ('section', 'status', 'mandatory', 'provider', 'reason', 'error_count')}
                         for s in sections],
            'technical_findings': findings[:40], 'failing_sections': failing, 'mandatory_not_passed': blocked}


@handler('cinematic_accept')
def accept(ctx, req):
    model.closed(req.options, set())
    path, plan, state = store.load(ctx.storage, req.cinematic)
    gstore.mutable(state)
    failures = accept_gates(state)
    if failures:
        raise ValueError(failures[0] + (' (+' + str(len(failures) - 1) + ' more: '
                                        + '; '.join(f.split(':')[0] for f in failures[1:]) + ')' if len(failures) > 1 else ''))
    store.fresh(ctx.bridge, ctx.storage, plan, state)
    integrity = run_section(ctx, path, plan, state, SECTION_BY_ID['integrity'])
    if integrity['status'] == 'NOT_RUN':
        _fail('CINEMATIC_INTEGRITY_UNAVAILABLE', integrity.get('reason') or 'no integrity provider')
    if integrity['status'] != 'PASS':
        _fail('CINEMATIC_INTEGRITY_FAILED', '; '.join(integrity.get('errors') or [integrity['status']]))
    if not req.apply:
        return {'status': 'proposal', 'apply': False, **store.public(path, plan, state), 'gates': 'all met',
                'integrity': {k: integrity.get(k) for k in ('status', 'provider')},
                'next': 'call again with apply=true to mark the candidate scene ACCEPTED (the source scene is kept)'}
    params = store.assembly_params(plan, state, {'lifecycle': 'ACCEPTED', 'review_reference': state['review']['review_json']})
    new, result = store.mutate(ctx, path, plan, state, 'accept', MARK, params, allowed={'REVIEWED'})
    if result['fingerprints'] != state['fingerprints']:
        store.record(path, dict(new, last_error={'code': 'CINEMATIC_ACCEPT_CHANGED_CANDIDATE'}))
        _fail('CINEMATIC_ACCEPT_CHANGED_CANDIDATE', 'marking the scene changed a fingerprint; inspect checkpoint '
              + str(new.get('checkpoint')))
    for key in ('render', 'review', 'quality_report'):
        new[key] = state.get(key)  # same fingerprint: the reviewed evidence stays attached to the accepted candidate
    new['state'] = 'ACCEPTED'
    new['acceptance'] = {'scene': result['scene'], 'lifecycle': result['lifecycle'],
                         'review': state['review']['review_json'], 'quality_report': state['quality_report']['report_path'],
                         'integrity': {k: integrity.get(k) for k in ('status', 'provider')},
                         'synthetic_review': bool(state['review'].get('synthetic_review')),
                         'source_scene_replaced': False}
    store.record(path, new)
    response = {'status': 'accepted', **store.public(path, plan, new), 'acceptance': new['acceptance']}
    if new['acceptance']['synthetic_review']:
        response['warnings'] = [SYNTHETIC_LABEL + ': accepted on a synthetic mechanism-test review, not a real visual review']
    return response
