"""M10 area C: owned cameras, focus, cuts and the candidate timeline.

Tools (writers; apply=false is a proposal that never reaches Blender):
    cinematic_create_camera, cinematic_configure_camera, cinematic_animate_camera, cinematic_create_cuts,
    cinematic_plan_focus, cinematic_manage_timeline
plus `timeline_report(ctx, plan, state)` (read-only payload, no frame_set).

cinematic_manage_timeline also
- converts options.interactions (a cinematic_plan_interaction result or analysis-format events) into
  state['interactions'], exactly the rows analysis.py reads, with EVENT_<event_id> narrative markers;
- finalizes the montage (options.finalize=true): fresh candidate + timeline report + cuts/shots + actor integrity,
  then records ASSEMBLED in the ledger without changing Blender, or refuses with CINEMATIC_NOT_ASSEMBLABLE.

Rules kept here (docs/cinematic-architecture.md, docs/camera-systems.md):
- cameras are owned objects + owned camera data in the candidate `_CIN` collection; nothing outside the candidate is
  written. Constraints that aim a camera at an actor live on the owned camera and only *reference* the actor.
- animation is one owned Action per camera with an OBJECT slot and a CAMERA slot (measured pattern, Blender 5.2.1),
  LINEAR keys on every frame of the keyed range only;
- cuts are `timeline_markers` with `marker.camera` in the candidate scene (frame_set switches scene.camera, measured);
- every payload validates and samples first, then mutates, then measures: a refusal never leaves a half-applied
  candidate behind (store.fresh would otherwise lock the plan with CINEMATIC_STALE_CANDIDATE);
- lengths in options are metres; payloads convert with scene.unit_settings.scale_length;
- thresholds are labelled PROPOSAL; measurements come from Blender. No quality scores.
"""
from __future__ import annotations

from copy import deepcopy
import inspect
import math

from . import model, planning, registry, store
from .lifecycle import actor_objects
from .runtime import COMMON
from .service import handler

# ---------------------------------------------------------------------------------------------------------------
# Measured API facts and declared policy
# ---------------------------------------------------------------------------------------------------------------

CAMERA_TYPES = ('PERSP', 'ORTHO')                 # PANO/CUSTOM exist in 5.2.1 but are out of scope (no render QA).
SENSOR_FITS = ('AUTO', 'HORIZONTAL', 'VERTICAL')  # enum measured in 5.2.1
LENS_UNITS = ('MILLIMETERS', 'FOV')               # enum measured in 5.2.1
TRACK_MODES = ('NONE', 'TRACK_TO', 'DAMPED_TRACK')
AXES = ('X', 'Y', 'Z')

# Accepted value ranges. Lower bound = RNA hard_min, upper bound = RNA soft_max (the UI range), both measured in
# Blender 5.2.1 (bpy.types.Camera / CameraDOFSettings bl_rna). Refusing beyond the UI range is a declared policy,
# not a Blender limit: values the UI cannot reach are treated as input errors.
RANGES = {
    'lens': (1.0, 5000.0), 'sensor_width': (1.0, 100.0), 'sensor_height': (1.0, 100.0),
    'shift_x': (-2.0, 2.0), 'shift_y': (-2.0, 2.0),
    # clip: hard_min 1e-6 measured; no soft max exists, 1e6 m is the repo-wide coordinate bound (model.vector).
    'clip_start': (1e-6, 1e6), 'clip_end': (1e-6, 1e6),
    'ortho_scale': (0.001, 10000.0),
    'focus_distance': (0.0, 5000.0), 'aperture_fstop': (0.1, 128.0), 'aperture_blades': (0, 16),
    'aperture_rotation_deg': (-180.0, 180.0), 'aperture_ratio': (0.01, 2.0),
}
# Blender 5.2.1 defaults of a new camera datablock (measured): used when the caller does not choose.
DEFAULTS = {'type': 'PERSP', 'lens': 50.0, 'lens_unit': 'MILLIMETERS', 'sensor_width': 36.0, 'sensor_height': 24.0,
            'sensor_fit': 'AUTO', 'shift_x': 0.0, 'shift_y': 0.0, 'clip_start': 0.1, 'clip_end': 1000.0,
            'ortho_scale': 6.0}
DOF_DEFAULTS = {'use_dof': False, 'focus_distance': 10.0, 'aperture_fstop': 2.8, 'aperture_blades': 0}

# Proposed thresholds (PROPOSAL: not measured truths; every result carries threshold_source).
MIN_SHOT_FRAMES = 6
HANDHELD_LIMITS = {'amplitude_m': 0.05, 'amplitude_deg': 3.0, 'frequency_hz': 8.0}
HANDHELD_DEFAULTS = {'amplitude_m': 0.01, 'amplitude_deg': 0.5, 'frequency_hz': 1.0, 'seed': 0}
FOCUS_PULL_DEFAULTS = {'frames': 8, 'easing': 'EASE_IN_OUT'}
FOCUS_THRESHOLDS = {'excessive_coc_ratio': 0.01, 'jump_ratio': 0.25, 'keyed_tolerance_m': 1e-3}
MAX_KEYED_FRAMES = 7201          # 120 s at 60 fps, the model's longest timeline
SAMPLE_ROWS = 10                 # frames listed per finding; counts are always complete

MOVE_PARAMS = {
    'STATIC': set(), 'DOLLY': {'distance_m'}, 'TRUCK': {'distance_m'}, 'CRANE': {'height_m'},
    'ORBIT': {'angle_deg', 'axis'}, 'PAN': {'angle_deg'}, 'TILT': {'angle_deg'}, 'TRACKING': {'offset_m'},
    'HANDHELD': {'seed', 'amplitude_m', 'amplitude_deg', 'frequency_hz'},
}
COMMON_MOVE_PARAMS = {'look_at_target', 'lens_end_mm', 'start_location', 'start_rotation_deg'}
TARGET_REQUIRED = {'ORBIT', 'TRACKING'}
TARGET_FORBIDDEN = {'PAN', 'TILT'}


def fail(code, detail):
    raise ValueError(code + ': ' + detail)


# ---------------------------------------------------------------------------------------------------------------
# Pure functions shipped to Blender with inspect.getsource (plain math only, safe-mode clean) and unit-tested here
# ---------------------------------------------------------------------------------------------------------------

def cam_ease(kind, t):
    """Declared easing curves: progress s in [0, 1] for t in [0, 1].
    LINEAR s=t; EASE_IN s=t^2; EASE_OUT s=1-(1-t)^2; EASE_IN_OUT s=t^2 (3-2t)."""
    t = min(1.0, max(0.0, t))
    if kind == 'EASE_IN':
        return t * t
    if kind == 'EASE_OUT':
        return 1.0 - (1.0 - t) * (1.0 - t)
    if kind == 'EASE_IN_OUT':
        return t * t * (3.0 - 2.0 * t)
    return t


def cam_lcg(state):
    # Numerical Recipes LCG: deterministic on every Python build, unlike hash() randomisation.
    return (1103515245 * state + 12345) % 2147483648


def cam_handheld_terms(seed, frequency_hz):
    """Per channel (x, y, z, rx, ry, rz) three sine terms with seeded phases. Weights sum to 1, so the offset of a
    channel never exceeds its amplitude (bounded by construction, not by clipping)."""
    state = int(seed) % 2147483648
    terms = []
    for channel in range(6):
        row = []
        for ratio, weight in ((1.0, 0.5), (2.3, 0.3), (5.1, 0.2)):
            state = cam_lcg(state)
            phase = state / 2147483648.0 * 2.0 * math.pi
            row.append([frequency_hz * ratio, weight, phase])
        terms.append(row)
    return terms


def cam_handheld_offset(terms, time_s, amplitude_m, amplitude_rad):
    values = []
    for channel in range(6):
        value = 0.0
        for frequency, weight, phase in terms[channel]:
            value += weight * math.sin(2.0 * math.pi * frequency * time_s + phase)
        values.append(value * (amplitude_m if channel < 3 else amplitude_rad))
    return values


def cam_quat_mul(a, b):
    return [a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
            a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
            a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
            a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0]]


def cam_angular_velocity(q0, q1, dt):
    """World-frame angular velocity (deg/s) taking q0 to q1 in dt along the shortest arc."""
    rel = cam_quat_mul(q1, [q0[0], -q0[1], -q0[2], -q0[3]])
    if rel[0] < 0.0:
        rel = [-rel[0], -rel[1], -rel[2], -rel[3]]
    s = math.sqrt(rel[1] * rel[1] + rel[2] * rel[2] + rel[3] * rel[3])
    if s < 1e-12:
        return [0.0, 0.0, 0.0]
    scale = math.degrees(2.0 * math.atan2(s, rel[0])) / dt / s
    return [rel[1] * scale, rel[2] * scale, rel[3] * scale]


def cam_vec_norm(values):
    return math.sqrt(sum(v * v for v in values))


def cam_finite_diff(rows, dt):
    return [[(rows[i][k] - rows[i - 1][k]) / dt for k in range(3)] for i in range(1, len(rows))]


def cam_peak(rows, frames, offset):
    best = 0.0
    frame = None
    for i in range(len(rows)):
        value = cam_vec_norm(rows[i])
        if value > best:
            best = value
            frame = frames[i + offset]
    return [round(best, 6), frame]


def cam_derivatives(frames, positions, quaternions, fps):
    """Backward differences on uniformly spaced samples: speed, acceleration and jerk (m/s, m/s2, m/s3) and the
    angular equivalents (deg/s, deg/s2, deg/s3, from world angular velocity vectors). Peaks carry their frame."""
    if len(frames) < 2:
        return {'samples': len(frames), 'insufficient_samples': True}
    step = frames[1] - frames[0]
    for i in range(1, len(frames)):
        if abs((frames[i] - frames[i - 1]) - step) > 1e-9 or step <= 0:
            return {'samples': len(frames), 'non_uniform_samples': True}
    dt = step / float(fps)
    velocity = cam_finite_diff(positions, dt)
    acceleration = cam_finite_diff(velocity, dt)
    jerk = cam_finite_diff(acceleration, dt)
    omega = [cam_angular_velocity(quaternions[i - 1], quaternions[i], dt) for i in range(1, len(quaternions))]
    alpha = cam_finite_diff(omega, dt)
    angular_jerk = cam_finite_diff(alpha, dt)
    path = sum(cam_vec_norm(v) * dt for v in velocity)
    turned = sum(cam_vec_norm(w) * dt for w in omega)
    speed = cam_peak(velocity, frames, 1)
    accel = cam_peak(acceleration, frames, 2)
    jerk_peak = cam_peak(jerk, frames, 3)
    w_peak = cam_peak(omega, frames, 1)
    a_peak = cam_peak(alpha, frames, 2)
    j_peak = cam_peak(angular_jerk, frames, 3)
    return {'samples': len(frames), 'dt_s': dt, 'path_length_m': round(path, 6), 'rotation_travel_deg': round(turned, 6),
            'max_speed_m_s': speed[0], 'max_speed_frame': speed[1],
            'max_acceleration_m_s2': accel[0], 'max_acceleration_frame': accel[1],
            'max_jerk_m_s3': jerk_peak[0], 'max_jerk_frame': jerk_peak[1],
            'max_angular_speed_deg_s': w_peak[0], 'max_angular_speed_frame': w_peak[1],
            'max_angular_acceleration_deg_s2': a_peak[0], 'max_angular_acceleration_frame': a_peak[1],
            'max_angular_jerk_deg_s3': j_peak[0], 'max_angular_jerk_frame': j_peak[1],
            'method': 'backward finite differences of per-frame samples; angular from world angular velocity vectors'}


def cam_coc_mm(lens_mm, fstop, focus_m, subject_m):
    """Thin-lens circle of confusion on the sensor (mm): c = f^2 / (N (s - f)) * |d - s| / d, with f the focal
    length, N the f-number, s the focus distance and d the subject depth along the view axis. None when undefined
    (subject behind the camera or focus inside the focal length)."""
    f = lens_mm / 1000.0
    if fstop <= 0.0 or subject_m <= 0.0 or focus_m <= f:
        return None
    return (f * f / (fstop * (focus_m - f))) * abs(subject_m - focus_m) / subject_m * 1000.0


def cam_frames_summary(frames, limit):
    return {'count': len(frames), 'frames': frames[:limit], 'truncated': len(frames) > limit}


PURE_FUNCTIONS = (cam_ease, cam_lcg, cam_handheld_terms, cam_handheld_offset, cam_quat_mul, cam_angular_velocity,
                  cam_vec_norm, cam_finite_diff, cam_peak, cam_derivatives, cam_coc_mm, cam_frames_summary)
PURE = '\n' + ''.join(inspect.getsource(function) + '\n' for function in PURE_FUNCTIONS)

# ---------------------------------------------------------------------------------------------------------------
# Blender runtime (helpers + one body per tool)
# ---------------------------------------------------------------------------------------------------------------

CAMERA_RUNTIME = COMMON + PURE + r'''
def cam_unit(scene):
    # metres per Blender unit; option lengths are metres.
    value = scene.unit_settings.scale_length
    return value if value and value > 0 else 1.0

def cam_fps(scene):
    return scene.render.fps / scene.render.fps_base

def cam_owned_camera(params, name, cin):
    obj = bpy.data.objects.get(name)
    if obj is None:
        cm_fail('CAMERA_MISSING', name + ' no longer exists')
    if obj.type != 'CAMERA' or not cm_is_owned(obj, params) or not cm_is_owned(obj.data, params):
        cm_fail('CAMERA_NOT_OWNED', obj.name + ' is not a camera created by this cinematic')
    if cin.all_objects.get(obj.name) is None:
        cm_fail('CAMERA_NOT_IN_CANDIDATE', obj.name)
    return obj

def cam_rna_check(props, prop, value):
    # Defense in depth against API drift: the host validated against ranges measured in 5.2.1. Type-level RNA, so
    # the check runs before any datablock exists (validate -> sample -> mutate -> measure).
    rna = props[prop]
    if value < rna.hard_min or value > rna.hard_max:
        cm_fail('CAMERA_VALUE_OUT_OF_RANGE', prop + '=' + str(value) + ' outside RNA [' + str(rna.hard_min) + ', ' + str(rna.hard_max) + ']')

def cam_settings(data, unit):
    dof = data.dof
    return {'type': data.type, 'lens': round(data.lens, 6), 'lens_unit': data.lens_unit,
            'sensor_width': round(data.sensor_width, 6), 'sensor_height': round(data.sensor_height, 6),
            'sensor_fit': data.sensor_fit, 'shift_x': round(data.shift_x, 6), 'shift_y': round(data.shift_y, 6),
            'clip_start': round(data.clip_start * unit, 9), 'clip_end': round(data.clip_end * unit, 6),
            'ortho_scale': round(data.ortho_scale, 6), 'angle_deg': round(math.degrees(data.angle), 6),
            'dof': {'use_dof': dof.use_dof, 'focus_object': dof.focus_object.name if dof.focus_object else None,
                    'focus_subtarget': dof.focus_subtarget, 'focus_distance': round(dof.focus_distance * unit, 6),
                    'aperture_fstop': round(dof.aperture_fstop, 6), 'aperture_blades': dof.aperture_blades,
                    'aperture_rotation_deg': round(math.degrees(dof.aperture_rotation), 6),
                    'aperture_ratio': round(dof.aperture_ratio, 6)}}

def cam_check_settings(data, s, unit):
    props = bpy.types.Camera.bl_rna.properties
    for key in ('lens', 'sensor_width', 'sensor_height', 'shift_x', 'shift_y', 'ortho_scale'):
        if key in s:
            cam_rna_check(props, key, s[key])
    for key in ('clip_start', 'clip_end'):
        if key in s:
            cam_rna_check(props, key, s[key] / unit)
    start = s['clip_start'] / unit if 'clip_start' in s else (data.clip_start if data else 0.0)
    end = s['clip_end'] / unit if 'clip_end' in s else (data.clip_end if data else 0.0)
    if start >= end:
        cm_fail('CAMERA_CLIP_INVALID', 'clip_start must be below clip_end')

def cam_apply_settings(data, s, unit):
    # Explicit assignments: safe mode forbids setattr with a computed name.
    if 'type' in s:
        data.type = s['type']
    if 'lens_unit' in s:
        data.lens_unit = s['lens_unit']
    if 'sensor_fit' in s:
        data.sensor_fit = s['sensor_fit']
    if 'lens' in s:
        data.lens = s['lens']
    if 'sensor_width' in s:
        data.sensor_width = s['sensor_width']
    if 'sensor_height' in s:
        data.sensor_height = s['sensor_height']
    if 'shift_x' in s:
        data.shift_x = s['shift_x']
    if 'shift_y' in s:
        data.shift_y = s['shift_y']
    if 'ortho_scale' in s:
        data.ortho_scale = s['ortho_scale']
    # clip_end first when growing and clip_start first when shrinking keeps start < end at every step.
    if 'clip_end' in s:
        data.clip_end = s['clip_end'] / unit
    if 'clip_start' in s:
        data.clip_start = s['clip_start'] / unit

def cam_check_dof(d, unit):
    props = bpy.types.CameraDOFSettings.bl_rna.properties
    for key in ('aperture_fstop', 'aperture_blades', 'aperture_ratio'):
        if key in d:
            cam_rna_check(props, key, d[key])
    if 'focus_distance' in d:
        cam_rna_check(props, 'focus_distance', d['focus_distance'] / unit)
    if 'aperture_rotation_deg' in d:
        cam_rna_check(props, 'aperture_rotation', math.radians(d['aperture_rotation_deg']))

def cam_apply_dof(data, d, unit, focus_object, subtarget):
    dof = data.dof
    if 'use_dof' in d:
        dof.use_dof = d['use_dof']
    if 'focus_distance' in d:
        dof.focus_distance = d['focus_distance'] / unit
    if 'aperture_fstop' in d:
        dof.aperture_fstop = d['aperture_fstop']
    if 'aperture_blades' in d:
        dof.aperture_blades = d['aperture_blades']
    if 'aperture_rotation_deg' in d:
        dof.aperture_rotation = math.radians(d['aperture_rotation_deg'])
    if 'aperture_ratio' in d:
        dof.aperture_ratio = d['aperture_ratio']
    if 'focus_object' in d:
        dof.focus_object = focus_object
        dof.focus_subtarget = subtarget or ''
    elif 'focus_subtarget' in d:
        dof.focus_subtarget = d['focus_subtarget'] or ''

def cam_animated_paths(idblock):
    ad = idblock.animation_data
    if ad is None or ad.action is None or ad.action_slot is None:
        return []
    paths = []
    for layer in ad.action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(ad.action_slot)
            if bag is not None:
                for fc in bag.fcurves:
                    paths.append(fc.data_path)
    return paths

def cam_target(scene, spec):
    """Resolve a target spec {kind, object, bone} against the candidate scene. Actors without a bone use their root:
    the single parentless bone of an armature, else the object origin."""
    if spec is None or spec.get('object') is None:
        return None, None, None
    obj = scene.objects.get(spec['object'])
    if obj is None:
        cm_fail('CAMERA_TARGET_MISSING', spec['object'] + ' is not in the candidate scene')
    bone = spec.get('bone')
    rule = 'bone' if bone else 'object_origin'
    if bone is None and obj.type == 'ARMATURE' and spec.get('kind') == 'actor' and spec.get('root_bone'):
        # The registry's root role (explicit or M3 semantics): rigs with parentless IK controls have several roots.
        bone = spec['root_bone']
        rule = 'registry_root_bone'
    elif bone is None and obj.type == 'ARMATURE' and spec.get('kind') == 'actor':
        roots = sorted(b.name for b in obj.data.bones if b.parent is None)
        if len(roots) > 1:
            cm_fail('CAMERA_TARGET_ROOT_AMBIGUOUS', obj.name + ' has several root bones ' + ', '.join(roots[:8]) + '; pass bone')
        if roots:
            bone = roots[0]
            rule = 'single_root_bone'
    if bone is not None and (obj.type != 'ARMATURE' or obj.data.bones.get(bone) is None):
        cm_fail('CAMERA_TARGET_BONE_MISSING', str(bone) + ' on ' + obj.name)
    return obj, bone, rule

def cam_target_point(scene, obj, bone):
    ev = cm_evaluated(scene, obj)
    if bone is None:
        return ev.matrix_world.translation.copy()
    return ev.matrix_world @ ev.pose.bones[bone].head

def cam_capture_set(params, cin):
    objects = []
    for name in params.get('capture') or []:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj not in objects:
            objects.append(obj)
    for obj in cin.all_objects:
        if obj not in objects:
            objects.append(obj)
    return objects

def cam_look(direction, previous):
    if direction.length < 1e-9:
        return previous.copy()
    quat = direction.to_track_quat('-Z', 'Y')
    return quat.to_euler('XYZ', previous)

def cam_slot(act, id_type, name):
    identifier = ('OB' if id_type == 'OBJECT' else 'CA') + name
    for slot in act.slots:
        if slot.identifier == identifier:
            return slot
    return None

def cam_action_check(params, cam):
    """Existing animation of an owned camera must be this generation's camera Action (or none)."""
    act = bpy.data.actions.get(params['action'])
    if act is not None and not cm_is_owned(act, params):
        cm_fail('CINEMATIC_NAME_COLLISION', params['action'])
    for idblock in (cam, cam.data):
        ad = idblock.animation_data
        if ad is not None and ad.action is not None and ad.action != act:
            cm_fail('CAMERA_ACTION_FOREIGN', idblock.name + ' is animated by ' + ad.action.name)
        if ad is not None and len(ad.nla_tracks):
            cm_fail('CAMERA_NLA_UNSUPPORTED', idblock.name + ' has NLA tracks')
    return act

def cam_keyed_in_range(params, cam, channels, frame_start, frame_end):
    act = bpy.data.actions.get(params['action'])
    if act is None or len(act.layers) == 0:
        return []
    strip = act.layers[0].strips[0]
    hits = []
    for id_type, path, index in channels:
        slot = cam_slot(act, id_type, cam.name if id_type == 'OBJECT' else cam.data.name)
        bag = strip.channelbag(slot) if slot is not None else None
        fc = cam_fcurve(bag, path, index) if bag is not None else None
        if fc is None:
            continue
        for kp in fc.keyframe_points:
            if frame_start - 0.5 <= kp.co[0] <= frame_end + 0.5:
                hits.append(path + '[' + str(index) + ']')
                break
    return hits

def cam_action(params, cam):
    """Get or create the owned camera Action with its OBJECT and CAMERA slots (measured pattern) and assign both."""
    act = bpy.data.actions.get(params['action'])
    created = act is None
    if created:
        act = cm_new_action(params, params['action'], 'camera_action')
    slot_object = cam_slot(act, 'OBJECT', cam.name) or act.slots.new(id_type='OBJECT', name=cam.name)
    slot_camera = cam_slot(act, 'CAMERA', cam.data.name) or act.slots.new(id_type='CAMERA', name=cam.data.name)
    if len(act.layers) == 0:
        act.layers.new('Layer').strips.new(type='KEYFRAME')
    strip = act.layers[0].strips[0]
    for idblock, slot in ((cam, slot_object), (cam.data, slot_camera)):
        ad = idblock.animation_data_create()
        if ad.action != act:
            cm_ensure_pose(idblock)
            ad.action = act
        if ad.action_slot != slot:
            ad.action_slot = slot
    return act, strip, slot_object, slot_camera, created

def cam_fcurve(bag, path, index):
    for fc in bag.fcurves:
        if fc.data_path == path and fc.array_index == index:
            return fc
    return None

def cam_write_channel(bag, path, index, frames, values, frame_start, frame_end):
    # Rebuild the channel: keys outside the range are kept, keys inside are replaced. Removing keyframes one by one
    # would invalidate the Python references to the remaining ones.
    kept = []
    fc = cam_fcurve(bag, path, index)
    if fc is not None:
        for kp in fc.keyframe_points:
            if kp.co[0] < frame_start - 0.5 or kp.co[0] > frame_end + 0.5:
                kept.append([kp.co[0], kp.co[1]])
        bag.fcurves.remove(fc)
    fc = bag.fcurves.new(path, index=index)
    rows = sorted(kept + [[float(frames[i]), float(values[i])] for i in range(len(frames))])
    fc.keyframe_points.add(len(rows))
    flat = []
    for row in rows:
        flat.extend(row)
    fc.keyframe_points.foreach_set('co', flat)
    for kp in fc.keyframe_points:
        kp.interpolation = 'LINEAR'
    fc.update()
    return fc

def cam_marker_rows(scene, params):
    rows = []
    for m in sorted(scene.timeline_markers, key=cam_marker_sort):
        camera = m.camera
        rows.append([m.name, m.frame, camera.name if camera else None,
                     bool(camera is not None and cm_is_owned(camera, params) and camera.type == 'CAMERA')])
    return rows

def cam_marker_sort(marker):
    return (marker.frame, marker.name)

def cam_find_marker(scene, name):
    for m in scene.timeline_markers:
        if m.name == name:
            return m
    return None

def cam_timeline_state(scene, params):
    return {'frame_start': scene.frame_start, 'frame_end': scene.frame_end, 'fps': scene.render.fps,
            'fps_base': scene.render.fps_base, 'frame_current': scene.frame_current,
            'camera': scene.camera.name if scene.camera else None, 'markers': cam_marker_rows(scene, params)}
'''

CREATE_BODY = CAMERA_RUNTIME + r'''
def cam_create(params):
    scene, src, cin = cm_candidate(params)
    unit = cam_unit(scene)
    cm_free_name(bpy.data.objects, params['camera'])
    cm_free_name(bpy.data.cameras, params['camera_data'])
    target_obj, bone, rule = cam_target(scene, params.get('look_at'))
    point = None
    if params.get('look_at') and params['look_at'].get('point') is not None:
        point = mathutils.Vector(params['look_at']['point']) / unit
    location = mathutils.Vector(params['location']) / unit
    cam_check_settings(None, params['settings'], unit)
    # Sample the target before anything changes (validate -> sample -> mutate -> measure).
    if target_obj is not None:
        objects = cam_capture_set(params, cin)
        with bpy.context.temp_override(scene=scene):
            state = cm_capture(scene, objects)
            try:
                scene.frame_set(params['reference_frame'])
                point = cam_target_point(scene, target_obj, bone).copy()
            finally:
                cm_restore(scene, objects, state)
    if point is not None:
        if (point - location).length < 1e-6:
            cm_fail('CAMERA_LOOK_AT_DEGENERATE', 'camera location coincides with the look_at target')
        rotation = cam_look(point - location, mathutils.Euler((0.0, 0.0, 0.0), 'XYZ'))
    else:
        rotation = mathutils.Euler([math.radians(v) for v in params['rotation_euler_deg']], 'XYZ')
    data = bpy.data.cameras.new(params['camera_data'])
    if data.name != params['camera_data']:
        cm_fail('CINEMATIC_NAME_COLLISION', params['camera_data'])
    cam_apply_settings(data, params['settings'], unit)
    cam = cm_new_object(params, params['camera'], data, cin, 'camera')
    cam.rotation_mode = 'XYZ'
    cam.location = location
    cam.rotation_euler = rotation
    owned_constraints = []
    track = params['track']
    if track['mode'] != 'NONE':
        constraint = cam.constraints.new(track['mode'])
        constraint.name = track['name']
        constraint.target = target_obj
        if bone:
            constraint.subtarget = bone
        constraint.track_axis = 'TRACK_NEGATIVE_Z'
        if track['mode'] == 'TRACK_TO':
            constraint.up_axis = 'UP_Y'
        owned_constraints.append([cam.name, constraint.name])
    scene_camera_set = False
    if params['set_scene_camera'] and scene.camera is None:
        scene.camera = cam
        scene_camera_set = True
    bpy.context.view_layer.update()
    return {'object': cam.name, 'data': data.name, 'settings': cam_settings(data, unit),
            'location_m': [round(v * unit, 6) for v in cam.location],
            'rotation_euler_deg': [round(math.degrees(v), 6) for v in cam.rotation_euler],
            'target_point_m': [round(v * unit, 6) for v in point] if point is not None else None,
            'target_resolved': {'object': target_obj.name if target_obj else None, 'bone': bone, 'rule': rule},
            'scene_camera_set': scene_camera_set, 'scene_camera': scene.camera.name if scene.camera else None,
            'owned': {'objects': [cam.name], 'data': [['CAMERA', data.name]], 'constraints': owned_constraints},
            'fingerprints': cm_fingerprints(params)}

result = cam_create(params)
'''

CONFIGURE_BODY = CAMERA_RUNTIME + r'''
def cam_configure(params):
    scene, src, cin = cm_candidate(params)
    unit = cam_unit(scene)
    cam = cam_owned_camera(params, params['camera'], cin)
    data = cam.data
    before = cam_settings(data, unit)
    settings = params['settings']
    dof = params['dof']
    animated = set(cam_animated_paths(data))
    names = {'focus_distance': 'dof.focus_distance', 'aperture_fstop': 'dof.aperture_fstop'}
    conflicts = sorted(p for p in [names.get(k, k) for k in list(settings) + list(dof)] if p in animated)
    if conflicts:
        cm_fail('CAMERA_PROPERTY_ANIMATED', ', '.join(conflicts) + ' are keyed in the camera Action; re-animate instead')
    cam_check_settings(data, settings, unit)
    cam_check_dof(dof, unit)
    focus_object = None
    subtarget = dof.get('focus_subtarget')
    if dof.get('focus_object'):
        focus_object, bone, rule = cam_target(scene, dof['focus_object'])
        subtarget = subtarget or bone
    if subtarget:
        owner = focus_object if focus_object is not None else data.dof.focus_object
        if owner is None or owner.type != 'ARMATURE' or owner.data.bones.get(subtarget) is None:
            cm_fail('CAMERA_TARGET_BONE_MISSING', str(subtarget) + ' is not a bone of the focus object')
    tracking = params.get('tracking')
    track_target = None
    track_bone = None
    if tracking and tracking['mode'] != 'NONE':
        track_target, track_bone, rule = cam_target(scene, tracking['target'])
    # mutate
    cam_apply_settings(data, settings, unit)
    cam_apply_dof(data, dof, unit, focus_object, subtarget)
    removed = []
    owned_constraints = []
    if tracking:
        existing = cam.constraints.get(tracking['name'])
        if existing is not None:
            removed.append(existing.name)
            cam.constraints.remove(existing)
        if tracking['mode'] != 'NONE':
            constraint = cam.constraints.new(tracking['mode'])
            constraint.name = tracking['name']
            constraint.target = track_target
            if track_bone:
                constraint.subtarget = track_bone
            constraint.track_axis = 'TRACK_NEGATIVE_Z'
            if tracking['mode'] == 'TRACK_TO':
                constraint.up_axis = 'UP_Y'
            constraint.influence = tracking['influence']
            if not removed:
                owned_constraints.append([cam.name, constraint.name])
    bpy.context.view_layer.update()
    return {'object': cam.name, 'before': before, 'after': cam_settings(data, unit),
            'constraints': [cm_constraint_row(c) for c in cam.constraints], 'removed_constraints': removed,
            'owned': {'constraints': owned_constraints}, 'fingerprints': cm_fingerprints(params)}

result = cam_configure(params)
'''

ANIMATE_BODY = CAMERA_RUNTIME + r'''
def cam_path(params, base_location, base_rotation, targets, unit, fps, frames):
    """Per-frame location (Blender units) and Euler rotation for the declared move."""
    move = params['move']
    p = params['params']
    count = len(frames)
    base_quat = base_rotation.to_quaternion()
    forward = base_quat @ mathutils.Vector((0.0, 0.0, -1.0))
    right = base_quat @ mathutils.Vector((1.0, 0.0, 0.0))
    look = bool(targets) and p.get('look_at_target', True)
    locations = []
    rotations = []
    previous = base_rotation.copy()
    offset = None
    if move == 'TRACKING':
        offset = mathutils.Vector(p['offset_m']) / unit if p.get('offset_m') is not None else base_location - targets[0]
    if move == 'DOLLY' and targets:
        direction = targets[0] - base_location
        forward = direction.normalized() if direction.length > 1e-9 else forward
    pivot_offset = None
    axis = None
    if move == 'ORBIT':
        pivot_offset = base_location - targets[0]
        axis = mathutils.Vector({'X': (1.0, 0.0, 0.0), 'Y': (0.0, 1.0, 0.0), 'Z': (0.0, 0.0, 1.0)}[p.get('axis', 'Z')])
    terms = cam_handheld_terms(p.get('seed', 0), p.get('frequency_hz', 1.0)) if move == 'HANDHELD' else None
    for i in range(count):
        t = i / (count - 1) if count > 1 else 0.0
        s = cam_ease(params['easing'], t)
        target = targets[i] if targets else None
        location = base_location.copy()
        rotation = None
        if move == 'DOLLY':
            location = base_location + forward * (p['distance_m'] / unit) * s
        elif move == 'TRUCK':
            location = base_location + right * (p['distance_m'] / unit) * s
        elif move == 'CRANE':
            location = base_location + mathutils.Vector((0.0, 0.0, p['height_m'] / unit * s))
        elif move == 'ORBIT':
            turn = mathutils.Quaternion(axis, math.radians(p['angle_deg']) * s)
            location = target + turn @ pivot_offset
            rotation = cam_look(target - location, previous)
        elif move == 'PAN':
            quat = mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(p['angle_deg']) * s) @ base_quat
            rotation = quat.to_euler('XYZ', previous)
        elif move == 'TILT':
            quat = base_quat @ mathutils.Quaternion((1.0, 0.0, 0.0), math.radians(p['angle_deg']) * s)
            rotation = quat.to_euler('XYZ', previous)
        elif move == 'TRACKING':
            location = target + offset
        if rotation is None:
            rotation = cam_look(target - location, previous) if look and target is not None else base_rotation.copy()
            rotation = rotation.to_quaternion().to_euler('XYZ', previous)
        if move == 'HANDHELD':
            noise = cam_handheld_offset(terms, i / fps, p['amplitude_m'], math.radians(p['amplitude_deg']))
            location = location + mathutils.Vector(noise[:3]) / unit
            jitter = mathutils.Euler(noise[3:], 'XYZ').to_quaternion()
            rotation = (rotation.to_quaternion() @ jitter).to_euler('XYZ', previous)
        locations.append(location)
        rotations.append(rotation)
        previous = rotation
    return locations, rotations

def cam_animate(params):
    scene, src, cin = cm_candidate(params)
    unit = cam_unit(scene)
    fps = cam_fps(scene)
    cam = cam_owned_camera(params, params['camera'], cin)
    cam_action_check(params, cam)
    frames = list(range(params['frame_start'], params['frame_end'] + 1))
    channels = [['OBJECT', 'location', 0], ['OBJECT', 'location', 1], ['OBJECT', 'location', 2],
                ['OBJECT', 'rotation_euler', 0], ['OBJECT', 'rotation_euler', 1], ['OBJECT', 'rotation_euler', 2]]
    zoom = params['params'].get('lens_end_mm') is not None
    if zoom:
        cam_rna_check(bpy.types.Camera.bl_rna.properties, 'lens', params['params']['lens_end_mm'])
        channels.append(['CAMERA', 'lens', 0])
    overlap = cam_keyed_in_range(params, cam, channels, frames[0], frames[-1])
    if overlap and not params['replace']:
        cm_fail('CAMERA_ANIMATION_OVERLAP', ', '.join(overlap) + ' already keyed in ' + str(frames[0]) + '-' + str(frames[-1]) + '; pass replace=true')
    if cam.rotation_mode != 'XYZ':
        cm_fail('CAMERA_ROTATION_MODE', cam.name + ' uses ' + cam.rotation_mode)
    spec = params.get('target')
    target_obj, bone, rule = cam_target(scene, spec)
    objects = cam_capture_set(params, cin)
    targets = []
    if target_obj is not None:
        with bpy.context.temp_override(scene=scene):
            state = cm_capture(scene, objects)
            try:
                for f in frames:
                    scene.frame_set(f)
                    targets.append(cam_target_point(scene, target_obj, bone).copy())
            finally:
                cm_restore(scene, objects, state)
    elif spec and spec.get('point') is not None:
        point = mathutils.Vector(spec['point']) / unit
        targets = [point.copy() for f in frames]
    base_location = mathutils.Vector(params['base_location']) / unit
    base_rotation = mathutils.Euler([math.radians(v) for v in params['base_rotation_deg']], 'XYZ')
    locations, rotations = cam_path(params, base_location, base_rotation, targets, unit, fps, frames)
    for loc in locations:
        for v in loc:
            if not math.isfinite(v):
                cm_fail('CAMERA_PATH_NONFINITE', 'computed path is not finite')
    # mutate
    act, strip, slot_object, slot_camera, created = cam_action(params, cam)
    bag_object = strip.channelbag(slot_object, ensure=True)
    bag_camera = strip.channelbag(slot_camera, ensure=True)
    curves = []
    for k in range(3):
        curves.append(cam_write_channel(bag_object, 'location', k, frames, [loc[k] for loc in locations], frames[0], frames[-1]))
    for k in range(3):
        curves.append(cam_write_channel(bag_object, 'rotation_euler', k, frames, [rot[k] for rot in rotations], frames[0], frames[-1]))
    lens_curve = None
    if zoom:
        start = params['lens_start_mm']
        end = params['params']['lens_end_mm']
        count = len(frames)
        values = [start + (end - start) * cam_ease(params['easing'], (i / (count - 1)) if count > 1 else 0.0) for i in range(count)]
        lens_curve = cam_write_channel(bag_camera, 'lens', 0, frames, values, frames[0], frames[-1])
    keys_written = len(frames) * (7 if zoom else 6)
    # measure on the written keys (F-Curve evaluation) ...
    key_positions = []
    key_quats = []
    for f in frames:
        loc = mathutils.Vector([curves[k].evaluate(f) for k in range(3)])
        rot = mathutils.Euler([curves[3 + k].evaluate(f) for k in range(3)], 'XYZ')
        key_positions.append([v * unit for v in loc])
        key_quats.append(list(rot.to_quaternion()))
    keys_analysis = cam_derivatives(frames, key_positions, key_quats, fps)
    # ... and on the evaluated camera (includes constraints) with one frame_set per frame.
    eval_positions = []
    eval_quats = []
    offset_errors = []
    radius_errors = []
    behind = []
    distances = []
    base_radius = (base_location - targets[0]).length * unit if params['move'] == 'ORBIT' and targets else None
    tracking_offset = None
    if params['move'] == 'TRACKING' and targets:
        tracking_offset = (locations[0] - targets[0]) * unit
    with bpy.context.temp_override(scene=scene):
        state = cm_capture(scene, objects)
        try:
            for i in range(len(frames)):
                scene.frame_set(frames[i])
                matrix = cm_evaluated(scene, cam).matrix_world
                position = matrix.translation * unit
                eval_positions.append(list(position))
                eval_quats.append(list(matrix.to_quaternion()))
                if target_obj is not None:
                    target = cam_target_point(scene, target_obj, bone) * unit
                elif targets:
                    target = targets[i] * unit
                else:
                    target = None
                if target is not None:
                    forward = matrix.to_quaternion() @ mathutils.Vector((0.0, 0.0, -1.0))
                    if (target - position).dot(forward) <= 0.0:
                        behind.append(frames[i])
                    distances.append((target - position).length)
                    if tracking_offset is not None:
                        offset_errors.append(((position - target) - tracking_offset).length)
                    if base_radius is not None:
                        radius_errors.append(abs((position - target).length - base_radius))
        finally:
            cm_restore(scene, objects, state)
    eval_analysis = cam_derivatives(frames, eval_positions, eval_quats, fps)
    constrained = [c.name for c in cam.constraints if c.type in ('TRACK_TO', 'DAMPED_TRACK') and not c.mute and c.influence > 0.0]
    verification = {'target_resolved': {'object': target_obj.name if target_obj else None, 'bone': bone, 'rule': rule},
                    'target_behind_camera': cam_frames_summary(behind, 10),
                    'target_distance_m': [round(min(distances), 6), round(max(distances), 6)] if distances else None}
    if tracking_offset is not None:
        verification['tracking_offset_m'] = [round(v, 6) for v in tracking_offset]
        verification['tracking_offset_max_error_m'] = round(max(offset_errors), 9) if offset_errors else None
    if base_radius is not None:
        verification['orbit_radius_m'] = round(base_radius, 6)
        verification['orbit_radius_max_error_m'] = round(max(radius_errors), 9) if radius_errors else None
    return {'object': cam.name, 'action': act.name, 'action_created': created,
            'slots': [slot_object.identifier, slot_camera.identifier], 'frames': [frames[0], frames[-1]],
            'keys_written': keys_written, 'lens_keyed': zoom, 'rotation_constraints': constrained,
            'start_location_m': [round(v * unit, 6) for v in locations[0]],
            'end_location_m': [round(v * unit, 6) for v in locations[-1]],
            'keys_analysis': keys_analysis, 'evaluated_analysis': eval_analysis, 'verification': verification,
            'owned': {'actions': [act.name] if created else []}, 'fingerprints': cm_fingerprints(params)}

result = cam_animate(params)
'''

CUTS_BODY = CAMERA_RUNTIME + r'''
def cam_cuts(params):
    scene, src, cin = cm_candidate(params)
    cameras = {}
    for row in params['cuts']:
        cameras[row['camera']] = cam_owned_camera(params, row['camera'], cin)
        if not scene.frame_start <= row['frame'] <= scene.frame_end:
            cm_fail('CUT_OUTSIDE_RANGE', row['marker'] + ' at ' + str(row['frame']))
    removing = set(params['remove'])
    for name in removing:
        if not name.startswith('CUT_'):
            cm_fail('CUT_MARKER_INVALID', name)
    for row in params['cuts']:
        existing = cam_find_marker(scene, row['marker'])
        if existing is not None and row['marker'] not in removing:
            cm_fail('CINEMATIC_NAME_COLLISION', 'marker ' + row['marker'])
    # mutate
    removed = []
    for name in sorted(removing):
        marker = cam_find_marker(scene, name)
        if marker is not None:
            scene.timeline_markers.remove(marker)
            removed.append(name)
    for row in params['cuts']:
        marker = scene.timeline_markers.new(row['marker'], frame=row['frame'])
        marker.camera = cameras[row['camera']]
    # measure: frame_set at the first and last frame of every shot must select the cut camera (measured behaviour).
    checks = []
    objects = cam_capture_set(params, cin)
    with bpy.context.temp_override(scene=scene):
        state = cm_capture(scene, objects)
        try:
            for row in params['cuts']:
                for frame in sorted(set([row['frame'], row['frame_end']])):
                    scene.frame_set(frame)
                    active = scene.camera.name if scene.camera else None
                    checks.append([row['shot_id'], frame, row['camera'], active, active == row['camera']])
        finally:
            cm_restore(scene, objects, state)
    return {'removed': removed, 'checks': checks, 'timeline': cam_timeline_state(scene, params),
            'fingerprints': cm_fingerprints(params)}

result = cam_cuts(params)
'''

FOCUS_BODY = CAMERA_RUNTIME + r'''
def cam_focus_value(row, depths, f_index):
    if row['target'].get('distance_m') is not None:
        return row['target']['distance_m']
    return depths[row['target_key']][f_index]

def cam_focus(params):
    scene, src, cin = cm_candidate(params)
    unit = cam_unit(scene)
    fps = cam_fps(scene)
    cam = cam_owned_camera(params, params['camera'], cin)
    cam_action_check(params, cam)
    data = cam.data
    frames = list(range(params['frame_start'], params['frame_end'] + 1))
    rows = params['rows']
    mode = params['mode']
    if mode == 'KEYED' and not params['replace'] and cam_keyed_in_range(params, cam, [['CAMERA', 'dof.focus_distance', 0]], frames[0], frames[-1]):
        cm_fail('CAMERA_ANIMATION_OVERLAP', 'dof.focus_distance already keyed in this range; pass replace=true')
    if mode == 'OBJECT' and 'dof.focus_distance' in cam_animated_paths(data):
        cm_fail('CAMERA_PROPERTY_ANIMATED', 'dof.focus_distance is keyed; focus_object would be shadowed in review')
    resolved = {}
    for key, spec in params['targets'].items():
        resolved[key] = cam_target(scene, spec)
    for actor in params['actors']:
        resolved['actor:' + actor['actor_id']] = cam_target(scene, actor)
    objects = cam_capture_set(params, cin)
    depths = {key: [] for key in resolved}
    lens = []
    clip = []
    with bpy.context.temp_override(scene=scene):
        state = cm_capture(scene, objects)
        try:
            for f in frames:
                scene.frame_set(f)
                ev = cm_evaluated(scene, cam)
                matrix = ev.matrix_world
                position = matrix.translation
                forward = (matrix.to_quaternion() @ mathutils.Vector((0.0, 0.0, -1.0))).normalized()
                lens.append(ev.data.lens)
                clip.append([ev.data.clip_start * unit, ev.data.clip_end * unit])
                for key, value in resolved.items():
                    point = cam_target_point(scene, value[0], value[1])
                    # Depth along the view axis: the distance Blender's DOF uses for a focus object.
                    depths[key].append((point - position).dot(forward) * unit)
        finally:
            cm_restore(scene, objects, state)
    # planned focus distance per frame and pull windows
    pull_frames = set()
    planned = []
    active_rows = []
    for i in range(len(frames)):
        f = frames[i]
        active = 0
        for j in range(len(rows)):
            if rows[j]['frame'] <= f:
                active = j
        row = rows[active]
        value = cam_focus_value(row, depths, i)
        if active > 0 and row['pull_frames'] > 0 and f < row['frame'] + row['pull_frames']:
            s = cam_ease(params['pull_easing'], (f - row['frame'] + 1) / float(row['pull_frames']))
            previous = cam_focus_value(rows[active - 1], depths, i)
            value = previous + (value - previous) * s
            pull_frames.add(f)
        planned.append(value)
        active_rows.append(active)
    behind = []
    outside = []
    for i in range(len(frames)):
        row = rows[active_rows[i]]
        keys = [row.get('target_key')]
        if frames[i] in pull_frames and active_rows[i] > 0:
            keys.append(rows[active_rows[i] - 1].get('target_key'))
        for key in keys:
            if key is not None and depths[key][i] <= 0.0:
                behind.append(frames[i])
        if not clip[i][0] <= planned[i] <= clip[i][1]:
            outside.append(frames[i])
    if behind:
        cm_fail('FOCUS_TARGET_BEHIND_CAMERA', 'frames ' + ', '.join(str(f) for f in sorted(set(behind))[:10]))
    if outside:
        cm_fail('FOCUS_OUTSIDE_CLIP', 'planned focus outside clip range at frames ' + ', '.join(str(f) for f in outside[:10]))
    if params.get('aperture_fstop') is not None:
        cam_rna_check(bpy.types.CameraDOFSettings.bl_rna.properties, 'aperture_fstop', params['aperture_fstop'])
        if 'dof.aperture_fstop' in cam_animated_paths(data):
            cm_fail('CAMERA_PROPERTY_ANIMATED', 'dof.aperture_fstop is keyed')
    # mutate
    before = {'focus_object': data.dof.focus_object.name if data.dof.focus_object else None,
              'focus_subtarget': data.dof.focus_subtarget, 'use_dof': data.dof.use_dof}
    data.dof.use_dof = True
    if params.get('aperture_fstop') is not None:
        data.dof.aperture_fstop = params['aperture_fstop']
    act = None
    created = False
    keys_written = 0
    if mode == 'OBJECT':
        target = resolved[rows[0]['target_key']]
        data.dof.focus_object = target[0]
        data.dof.focus_subtarget = target[1] or ''
    else:
        data.dof.focus_object = None
        data.dof.focus_subtarget = ''
        act, strip, slot_object, slot_camera, created = cam_action(params, cam)
        bag = strip.channelbag(slot_camera, ensure=True)
        cam_write_channel(bag, 'dof.focus_distance', 0, frames, [v / unit for v in planned], frames[0], frames[-1])
        keys_written = len(frames)
    # measure the effective focus per frame after the change
    effective = []
    fstops = []
    with bpy.context.temp_override(scene=scene):
        state = cm_capture(scene, objects)
        try:
            for i in range(len(frames)):
                scene.frame_set(frames[i])
                ev = cm_evaluated(scene, cam)
                fstops.append(ev.data.dof.aperture_fstop)
                if mode == 'OBJECT':
                    effective.append(abs(depths[rows[0]['target_key']][i]))
                else:
                    effective.append(ev.data.dof.focus_distance * unit)
        finally:
            cm_restore(scene, objects, state)
    sensor = data.sensor_width
    th = params['thresholds']
    in_focus = th['in_focus_coc_mm']
    lost = []
    unverified = []
    target_coc = []
    jumps = []
    blurred = {}
    max_error = 0.0
    for i in range(len(frames)):
        f = frames[i]
        row = rows[active_rows[i]]
        if mode == 'KEYED':
            max_error = max(max_error, abs(effective[i] - planned[i]))
        subject = row.get('subject_key') or row.get('target_key')
        if subject is None:
            unverified.append(f)
        else:
            coc = cam_coc_mm(lens[i], fstops[i], effective[i], depths[subject][i])
            target_coc.append([f, coc])
            if f not in pull_frames and (coc is None or coc > in_focus):
                lost.append(f)
        if i > 0 and f not in pull_frames:
            base = max(effective[i - 1], 1e-6)
            if abs(effective[i] - effective[i - 1]) / base > th['jump_ratio']:
                jumps.append(f)
        for actor in params['actors']:
            key = 'actor:' + actor['actor_id']
            coc = cam_coc_mm(lens[i], fstops[i], effective[i], depths[key][i])
            if coc is not None and coc / sensor > th['excessive_coc_ratio']:
                blurred.setdefault(actor['actor_id'], []).append(f)
    findings = []
    if lost:
        findings.append({'code': 'FOCUS_LOST', 'severity': 'error', 'frames': cam_frames_summary(lost, 10),
                         'threshold': {'in_focus_coc_mm': in_focus, 'threshold_source': th['in_focus_source']}})
    if jumps:
        findings.append({'code': 'FOCUS_JUMP', 'severity': 'warning', 'frames': cam_frames_summary(jumps, 10),
                         'threshold': {'jump_ratio': th['jump_ratio'], 'threshold_source': 'PROPOSAL'}})
    for actor_id, rows_blurred in sorted(blurred.items()):
        findings.append({'code': 'DEFOCUS_EXCESSIVE', 'severity': 'warning', 'actor_id': actor_id,
                         'frames': cam_frames_summary(rows_blurred, 10),
                         'threshold': {'excessive_coc_ratio': th['excessive_coc_ratio'], 'threshold_source': 'PROPOSAL'},
                         'note': 'may be an intended artistic choice; review the frames'})
    if mode == 'KEYED' and max_error > th['keyed_tolerance_m']:
        findings.append({'code': 'FOCUS_KEYS_MISMATCH', 'severity': 'error', 'max_error_m': max_error,
                         'threshold': {'keyed_tolerance_m': th['keyed_tolerance_m'], 'threshold_source': 'PROPOSAL'}})
    measured = [v for f, v in target_coc if v is not None]
    peak = max(target_coc, key=cam_second) if measured else None
    return {'object': cam.name, 'mode': mode, 'frames': [frames[0], frames[-1]], 'keys_written': keys_written,
            'action': act.name if act is not None else None, 'before': before,
            'focus_range_m': [round(min(effective), 6), round(max(effective), 6)],
            'pull_frames': cam_frames_summary(sorted(pull_frames), 20),
            'target_coc_mm_max': [round(peak[1], 6), peak[0]] if peak else None,
            'unverified_frames': cam_frames_summary(unverified, 10),
            'keyed_max_error_m': round(max_error, 9) if mode == 'KEYED' else None,
            'findings': findings, 'owned': {'actions': [act.name] if created else []},
            'fingerprints': cm_fingerprints(params)}

def cam_second(row):
    return row[1] if row[1] is not None else -1.0

result = cam_focus(params)
'''

TIMELINE_BODY = CAMERA_RUNTIME + r'''
def cam_interaction_checks(scene, rows):
    # Objects and bones named by interaction events must resolve in the candidate before anything is written: an
    # event the QA cannot resolve would otherwise only surface later as an ignored analysis row.
    for row in rows:
        obj = scene.objects.get(row['object'])
        if obj is None:
            cm_fail('INTERACTION_OBJECT_NOT_IN_CANDIDATE', row['event_id'] + ': ' + row['object'] + ' is not in the candidate scene')
        for bone, event_id in row['bones']:
            if obj.type != 'ARMATURE' or obj.data.bones.get(bone) is None:
                cm_fail('INTERACTION_BONE_MISSING', event_id + ': ' + bone + ' is not a bone of ' + obj.name)

def cam_timeline(params):
    scene, src, cin = cm_candidate(params)
    if scene.render.fps != params['fps'] or abs(scene.render.fps_base - params['fps_base']) > 1e-9:
        cm_fail('CINEMATIC_FPS_MISMATCH', 'candidate fps changed outside the cinematic tools')
    removing = set(params['remove'])
    for name in removing:
        if not (name.startswith('BEAT_') or name.startswith('EVENT_')):
            cm_fail('TIMELINE_MARKER_INVALID', name)
    for row in params['add']:
        if cam_find_marker(scene, row['name']) is not None and row['name'] not in removing:
            cm_fail('CINEMATIC_NAME_COLLISION', 'marker ' + row['name'])
    cam_interaction_checks(scene, params.get('checks') or [])
    start = params.get('frame_start')
    end = params.get('frame_end')
    # mutate. Blender pushes frame_end when frame_start passes it (and the reverse): order the two writes.
    if start is not None:
        if start <= scene.frame_end:
            scene.frame_start = start
            scene.frame_end = end
        else:
            scene.frame_end = end
            scene.frame_start = start
    removed = []
    for name in sorted(removing):
        marker = cam_find_marker(scene, name)
        if marker is not None:
            scene.timeline_markers.remove(marker)
            removed.append(name)
    for row in params['add']:
        scene.timeline_markers.new(row['name'], frame=row['frame'])
    return {'removed': removed, 'timeline': cam_timeline_state(scene, params), 'fingerprints': cm_fingerprints(params)}

result = cam_timeline(params)
'''

TIMELINE_READ_BODY = CAMERA_RUNTIME + r'''
def cam_timeline_read(params):
    scene, src, cin = cm_candidate(params)
    cameras = {}
    for name in params['cameras']:
        obj = bpy.data.objects.get(name)
        cameras[name] = bool(obj is not None and obj.type == 'CAMERA' and cm_is_owned(obj, params)
                             and cin.all_objects.get(obj.name) is not None)
    return {'timeline': cam_timeline_state(scene, params), 'cameras': cameras}

result = cam_timeline_read(params)
'''

BODIES = {'create': CREATE_BODY, 'configure': CONFIGURE_BODY, 'animate': ANIMATE_BODY, 'cuts': CUTS_BODY,
          'focus': FOCUS_BODY, 'timeline': TIMELINE_BODY, 'timeline_read': TIMELINE_READ_BODY}

# ---------------------------------------------------------------------------------------------------------------
# Host-side validation (pure, no Blender)
# ---------------------------------------------------------------------------------------------------------------


def text_name(value, field, maximum=63):
    """Object/bone names may carry characters model.name refuses (e.g. 'mixamorig:Hips'); they travel as JSON
    data, never as code, so only length and control characters are checked."""
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip() \
            or any(ord(ch) < 32 for ch in value):
        fail('CINEMATIC_NAME_INVALID', field + ' must be 1-' + str(maximum) + ' printable characters')
    return value


def integer(value, field, minimum=-1_048_574, maximum=1_048_574):
    return model.number(value, field, minimum, maximum, integer=True)


def ranged(value, key, field):
    low, high = RANGES[key]
    return model.number(value, field, low, high, integer=key == 'aperture_blades')


def boolean(value, field):
    if not isinstance(value, bool):
        fail('CINEMATIC_BOOLEAN_REQUIRED', field + ' must be true or false')
    return value


def plan_actor(plan, actor_id):
    for actor in plan.get('actors') or []:
        if actor['actor_id'] == actor_id:
            return actor
    fail('ACTOR_NOT_IN_PLAN', str(actor_id) + ' is not an actor of this cinematic')


def resolve_target(plan, value, field, allow_point=True, allow_distance=False):
    """Target spec -> {kind, object, bone} | {kind:'point', point} | {kind:'distance', distance_m}."""
    if not isinstance(value, dict):
        fail('CAMERA_TARGET_INVALID', field + ' must be an object')
    allowed = {'actor_id', 'object', 'bone'} | ({'point'} if allow_point else set()) | ({'distance_m'} if allow_distance else set())
    value = model.closed(value, allowed, 'CAMERA_TARGET_INVALID')
    kinds = [k for k in ('actor_id', 'object', 'point', 'distance_m') if k in value]
    if len(kinds) != 1:
        fail('CAMERA_TARGET_INVALID', field + ' needs exactly one of ' + ', '.join(k for k in ('actor_id', 'object', 'point', 'distance_m') if k in allowed))
    bone = text_name(value['bone'], field + '.bone') if value.get('bone') is not None else None
    if 'actor_id' in value:
        actor = plan_actor(plan, value['actor_id'])
        return {'kind': 'actor', 'actor_id': actor['actor_id'], 'object': actor['object'], 'bone': bone}
    if 'object' in value:
        return {'kind': 'object', 'object': text_name(value['object'], field + '.object'), 'bone': bone}
    if bone is not None:
        fail('CAMERA_TARGET_INVALID', field + '.bone needs actor_id or object')
    if 'point' in value:
        return {'kind': 'point', 'point': model.vector(value['point'], field + '.point')}
    low, high = RANGES['focus_distance']
    return {'kind': 'distance', 'distance_m': model.number(value['distance_m'], field + '.distance_m', 0.001, high)}


def normalize_settings(opts, current=None):
    """Camera datablock settings from options (metres for clip). Returns (settings, provenance). With current=None
    (creation) missing fields take Blender's measured defaults."""
    settings = {}
    provenance = {}
    mapping = (('type', 'type'), ('lens_mm', 'lens'), ('lens_unit', 'lens_unit'), ('sensor_width_mm', 'sensor_width'),
               ('sensor_height_mm', 'sensor_height'), ('sensor_fit', 'sensor_fit'), ('shift_x', 'shift_x'),
               ('shift_y', 'shift_y'), ('clip_start', 'clip_start'), ('clip_end', 'clip_end'), ('ortho_scale', 'ortho_scale'))
    for option, key in mapping:
        if option not in opts:
            if current is None:
                settings[key] = DEFAULTS[key]
                provenance[key] = 'INFERRED_DEFAULT'
            continue
        value = opts[option]
        if key == 'type':
            settings[key] = model.choice(value, option, CAMERA_TYPES)
        elif key == 'lens_unit':
            settings[key] = model.choice(value, option, LENS_UNITS)
        elif key == 'sensor_fit':
            settings[key] = model.choice(value, option, SENSOR_FITS)
        else:
            settings[key] = ranged(value, key, option)
        provenance[key] = 'USER_REQUIREMENT'
    merged = dict(DEFAULTS, **(current or {}))
    merged.update(settings)
    if merged['clip_start'] >= merged['clip_end']:
        fail('CAMERA_CLIP_INVALID', 'clip_start ' + str(merged['clip_start']) + ' must be below clip_end ' + str(merged['clip_end']))
    return settings, provenance


def normalize_dof(plan, value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        fail('CAMERA_DOF_INVALID', 'dof must be an object')
    value = model.closed(value, {'use_dof', 'focus_object', 'focus_subtarget', 'focus_distance_m', 'aperture_fstop',
                                 'aperture_blades', 'aperture_rotation_deg', 'aperture_ratio'}, 'CAMERA_DOF_INVALID')
    dof = {}
    if 'use_dof' in value:
        dof['use_dof'] = boolean(value['use_dof'], 'dof.use_dof')
    if 'focus_distance_m' in value:
        dof['focus_distance'] = ranged(value['focus_distance_m'], 'focus_distance', 'dof.focus_distance_m')
    for key in ('aperture_fstop', 'aperture_blades', 'aperture_ratio', 'aperture_rotation_deg'):
        if key in value:
            dof[key] = ranged(value[key], key, 'dof.' + key)
    if 'focus_object' in value:
        target = value['focus_object']
        if target is None:
            dof['focus_object'] = None
        else:
            if isinstance(target, str):
                target = {'object': target}
            spec = resolve_target(plan, target, 'dof.focus_object', allow_point=False)
            dof['focus_object'] = spec
            if spec.get('bone'):
                dof['focus_subtarget'] = spec['bone']
    if 'focus_subtarget' in value:
        sub = value['focus_subtarget']
        dof['focus_subtarget'] = text_name(sub, 'dof.focus_subtarget') if sub else ''
    return dof


def shot_rows(plan, state):
    rows = state.get('shots') or plan.get('shots') or []
    return [r for r in rows if isinstance(r, dict)]


def find_shot(plan, state, shot_id):
    for row in shot_rows(plan, state):
        if row.get('shot_id') == shot_id:
            return row
    fail('CINEMATIC_SHOT_UNKNOWN', str(shot_id) + ' is not a shot of this cinematic')


def timeline_range(plan, state):
    timeline = state.get('timeline') or plan['timeline']
    return int(timeline['frame_start']), int(timeline['frame_end'])


def frame_range(plan, state, opts, camera=None):
    """shot_id > explicit frame_start/frame_end > the camera's own shot > the whole candidate timeline."""
    start, end = timeline_range(plan, state)
    if opts.get('shot_id') is not None:
        if 'frame_start' in opts or 'frame_end' in opts:
            fail('CAMERA_RANGE_AMBIGUOUS', 'pass shot_id or frame_start/frame_end, not both')
        shot = find_shot(plan, state, opts['shot_id'])
        first, last = int(shot['frame_start']), int(shot['frame_end'])
        source = 'shot:' + str(opts['shot_id'])
    elif 'frame_start' in opts or 'frame_end' in opts:
        if 'frame_start' not in opts or 'frame_end' not in opts:
            fail('CAMERA_RANGE_INCOMPLETE', 'pass both frame_start and frame_end')
        first = integer(opts['frame_start'], 'frame_start')
        last = integer(opts['frame_end'], 'frame_end')
        source = 'explicit'
    elif camera and camera.get('shot_id'):
        shot = find_shot(plan, state, camera['shot_id'])
        first, last = int(shot['frame_start']), int(shot['frame_end'])
        source = 'camera_shot:' + camera['shot_id']
    else:
        first, last = start, end
        source = 'timeline'
    if not start <= first <= last <= end:
        fail('CAMERA_RANGE_OUTSIDE_TIMELINE', str(first) + '-' + str(last) + ' outside ' + str(start) + '-' + str(end))
    if last - first + 1 > MAX_KEYED_FRAMES:
        fail('CINEMATIC_CAPACITY', 'at most ' + str(MAX_KEYED_FRAMES) + ' frames per call')
    return first, last, source


def owned_camera(plan, state, opts, req):
    """camera_id (or object_name) -> (camera_id, row). Only cameras this cinematic created are accepted."""
    cameras = state.get('cameras') or {}
    if opts.get('camera_id') is not None:
        camera_id = model.name(opts['camera_id'], 'camera_id')
        if camera_id not in cameras:
            fail('CAMERA_NOT_OWNED', camera_id + ' was not created by cinematic_create_camera in this cinematic')
        return camera_id, cameras[camera_id]
    if req.object_name:
        for camera_id, row in cameras.items():
            if row['object'] == req.object_name:
                return camera_id, row
        fail('CAMERA_NOT_OWNED', str(req.object_name) + ' is not a camera created by this cinematic')
    fail('CAMERA_ID_REQUIRED', 'pass options.camera_id')


def camera_names(plan, state, camera_id):
    gid = plan['generation_id']
    version = (state.get('assembly') or {}).get('version', 1)
    return {'object': model.owned_name(gid, version, 'CAM_' + camera_id),
            'data': model.owned_name(gid, version, 'CAMD_' + camera_id),
            'action': model.owned_name(gid, version, 'CAMACT_' + camera_id),
            'constraint': model.owned_name(gid, version, 'TRACK')}


def normalize_move(move, params, target):
    model.choice(move, 'move', model.CAMERA_MOVES)
    params = model.closed(params or {}, MOVE_PARAMS[move] | COMMON_MOVE_PARAMS, 'CAMERA_MOVE_PARAMS_INVALID')
    if move in TARGET_REQUIRED and target is None:
        fail('CAMERA_TARGET_REQUIRED', move + ' needs options.target')
    if move in TARGET_FORBIDDEN and target is not None:
        fail('CAMERA_TARGET_UNSUPPORTED', move + ' rotates in place; it takes no target')
    if move == 'TRACKING' and target and target['kind'] == 'point':
        fail('CAMERA_TARGET_UNSUPPORTED', 'TRACKING follows an actor or object; a fixed point is a STATIC/DOLLY shot')
    out = {}
    provenance = {}
    if move in ('DOLLY', 'TRUCK'):
        if 'distance_m' not in params:
            fail('CAMERA_MOVE_PARAMS_INVALID', move + ' needs params.distance_m')
        out['distance_m'] = model.number(params['distance_m'], 'params.distance_m', -1000, 1000)
        if out['distance_m'] == 0:
            fail('CAMERA_MOVE_PARAMS_INVALID', 'distance_m must not be 0; use STATIC')
    if move == 'CRANE':
        if 'height_m' not in params:
            fail('CAMERA_MOVE_PARAMS_INVALID', 'CRANE needs params.height_m')
        out['height_m'] = model.number(params['height_m'], 'params.height_m', -1000, 1000)
        if out['height_m'] == 0:
            fail('CAMERA_MOVE_PARAMS_INVALID', 'height_m must not be 0; use STATIC')
    if move in ('ORBIT', 'PAN', 'TILT'):
        if 'angle_deg' not in params:
            fail('CAMERA_MOVE_PARAMS_INVALID', move + ' needs params.angle_deg')
        out['angle_deg'] = model.number(params['angle_deg'], 'params.angle_deg', -3600, 3600)
        if out['angle_deg'] == 0:
            fail('CAMERA_MOVE_PARAMS_INVALID', 'angle_deg must not be 0; use STATIC')
    if move == 'ORBIT':
        out['axis'] = model.choice(params.get('axis', 'Z'), 'params.axis', AXES)
        provenance['axis'] = 'USER_REQUIREMENT' if 'axis' in params else 'INFERRED_DEFAULT'
    if move == 'TRACKING' and params.get('offset_m') is not None:
        out['offset_m'] = model.vector(params['offset_m'], 'params.offset_m', minimum=-1000, maximum=1000)
    if move == 'HANDHELD':
        out['seed'] = integer(params.get('seed', HANDHELD_DEFAULTS['seed']), 'params.seed', 0, 2_147_483_647)
        for key in ('amplitude_m', 'amplitude_deg', 'frequency_hz'):
            value = params.get(key, HANDHELD_DEFAULTS[key])
            out[key] = model.number(value, 'params.' + key, 0.0, HANDHELD_LIMITS[key])
            provenance[key] = 'USER_REQUIREMENT' if key in params else 'PROPOSED_CREATIVE_DECISION'
        if out['frequency_hz'] <= 0:
            fail('CINEMATIC_RANGE_INVALID', 'params.frequency_hz must be above 0')
        provenance['seed'] = 'USER_REQUIREMENT' if 'seed' in params else 'INFERRED_DEFAULT'
    if 'look_at_target' in params:
        out['look_at_target'] = boolean(params['look_at_target'], 'params.look_at_target')
        if target is None:
            fail('CAMERA_TARGET_REQUIRED', 'look_at_target needs options.target')
    if params.get('lens_end_mm') is not None:
        out['lens_end_mm'] = ranged(params['lens_end_mm'], 'lens', 'params.lens_end_mm')
    if params.get('start_location') is not None:
        out['start_location'] = model.vector(params['start_location'], 'params.start_location')
    if params.get('start_rotation_deg') is not None:
        out['start_rotation_deg'] = model.vector(params['start_rotation_deg'], 'params.start_rotation_deg', minimum=-3600, maximum=3600)
    return out, provenance


def check_cuts(rows, frame_start, frame_end, min_frames, cameras):
    """Validate cuts in the order given. rows: [{shot_id, camera_id, frame}]. Returns (normalized, errors, warnings);
    normalized rows gain frame_end (the frame before the next cut, or the timeline end) and frames."""
    errors = []
    warnings = []
    normalized = []
    if not rows:
        errors.append({'code': 'CUTS_EMPTY', 'detail': 'at least one cut is required'})
        return normalized, errors, warnings
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append({'code': 'CUT_INVALID', 'detail': 'cut ' + str(index) + ' must be an object'})
            continue
        try:
            row = model.closed(row, {'shot_id', 'camera_id', 'frame'}, 'CUT_INVALID')
            shot_id = model.name(row.get('shot_id'), 'cuts[' + str(index) + '].shot_id')
            camera_id = model.name(row.get('camera_id'), 'cuts[' + str(index) + '].camera_id')
            frame = integer(row.get('frame'), 'cuts[' + str(index) + '].frame')
        except ValueError as exc:
            code, _, detail = str(exc).partition(': ')
            errors.append({'code': code, 'detail': detail})
            continue
        if shot_id in seen:
            errors.append({'code': 'CUT_SHOT_DUPLICATE', 'detail': shot_id})
        seen.add(shot_id)
        if camera_id not in cameras:
            errors.append({'code': 'CAMERA_NOT_OWNED', 'detail': camera_id + ' (cut ' + shot_id + ')'})
        if not frame_start <= frame <= frame_end:
            errors.append({'code': 'CUT_OUTSIDE_RANGE', 'detail': shot_id + ' at ' + str(frame) + ' outside ' + str(frame_start) + '-' + str(frame_end)})
        normalized.append({'shot_id': shot_id, 'camera_id': camera_id, 'frame': frame, 'marker': 'CUT_' + shot_id})
    if errors:
        return normalized, errors, warnings
    for previous, current in zip(normalized, normalized[1:]):
        if current['frame'] == previous['frame']:
            errors.append({'code': 'CUT_OVERLAP', 'detail': previous['shot_id'] + ' and ' + current['shot_id'] + ' at ' + str(current['frame'])})
        elif current['frame'] < previous['frame']:
            errors.append({'code': 'CUT_ORDER_INVALID', 'detail': current['shot_id'] + ' at ' + str(current['frame']) + ' before ' + previous['shot_id'] + ' at ' + str(previous['frame'])})
    if normalized[0]['frame'] != frame_start:
        errors.append({'code': 'CUT_FIRST_NOT_AT_START', 'detail': 'first cut at ' + str(normalized[0]['frame']) + ', timeline starts at ' + str(frame_start)})
    if errors:
        return normalized, errors, warnings
    for index, row in enumerate(normalized):
        row['frame_end'] = normalized[index + 1]['frame'] - 1 if index + 1 < len(normalized) else frame_end
        row['frames'] = row['frame_end'] - row['frame'] + 1
        if row['frames'] < min_frames:
            errors.append({'code': 'CUT_TOO_SHORT', 'detail': row['shot_id'] + ' lasts ' + str(row['frames']) + ' frames',
                           'threshold': {'min_shot_frames': min_frames, 'threshold_source': 'PROPOSAL'}})
    for previous, current in zip(normalized, normalized[1:]):
        if previous['camera_id'] == current['camera_id']:
            warnings.append({'code': 'CUT_SAME_CAMERA', 'detail': previous['shot_id'] + ' -> ' + current['shot_id'] + ' keep ' + current['camera_id']})
    return normalized, errors, warnings


def raise_errors(errors):
    first = errors[0]
    extra = '' if len(errors) == 1 else ' (+' + str(len(errors) - 1) + ': ' + '; '.join(e['code'] + ' ' + e['detail'] for e in errors[1:6]) + ')'
    fail(first['code'], first['detail'] + extra)


def audit_cut_markers(markers, expected, frame_start, frame_end, min_frames):
    """Broken-cut detection on the markers read from Blender: [[name, frame, camera, camera_owned]]."""
    errors = []
    warnings = []
    cuts = [m for m in markers if m[0].startswith('CUT_')]
    for name, frame, camera, owned in cuts:
        if camera is None:
            errors.append({'code': 'CUT_CAMERA_MISSING', 'detail': name + ' has no camera (deleted or never set)'})
        elif not owned:
            errors.append({'code': 'CUT_CAMERA_NOT_OWNED', 'detail': name + ' -> ' + camera})
        if not frame_start <= frame <= frame_end:
            errors.append({'code': 'CUT_OUTSIDE_RANGE', 'detail': name + ' at ' + str(frame)})
    frames = sorted(m[1] for m in cuts)
    for previous, current in zip(frames, frames[1:]):
        if previous == current:
            errors.append({'code': 'CUT_OVERLAP', 'detail': 'two cuts at frame ' + str(current)})
    if cuts and frames[0] != frame_start:
        errors.append({'code': 'CUT_FIRST_NOT_AT_START', 'detail': 'first cut at ' + str(frames[0])})
    inside = [f for f in frames if frame_start <= f <= frame_end]
    for index, frame in enumerate(inside):
        last = inside[index + 1] - 1 if index + 1 < len(inside) else frame_end
        if last - frame + 1 < min_frames and (index + 1 >= len(inside) or inside[index + 1] != frame):
            warnings.append({'code': 'CUT_TOO_SHORT', 'detail': 'shot from ' + str(frame) + ' lasts ' + str(last - frame + 1) + ' frames',
                             'threshold': {'min_shot_frames': min_frames, 'threshold_source': 'PROPOSAL'}})
    by_name = {m[0]: m for m in cuts}
    for row in expected or []:
        marker = by_name.get(row['marker'])
        if marker is None:
            errors.append({'code': 'CUT_MARKER_MISSING', 'detail': row['marker']})
        elif marker[1] != row['frame'] or marker[2] != row['camera_object']:
            errors.append({'code': 'CUT_MARKER_CHANGED', 'detail': row['marker'] + ' is ' + str(marker[1:3]) + ', recorded ' + str([row['frame'], row['camera_object']])})
    recorded = {row['marker'] for row in expected or []}
    for name in sorted(set(by_name) - recorded):
        warnings.append({'code': 'CUT_MARKER_UNRECORDED', 'detail': name + ' was not created by cinematic_create_cuts'})
    return errors, warnings


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def gaps(ranges, frame_start, frame_end):
    holes = []
    cursor = frame_start
    for start, end in merge_ranges(ranges):
        if start > cursor:
            holes.append([cursor, min(start - 1, frame_end)])
        cursor = max(cursor, end + 1)
        if cursor > frame_end:
            break
    if cursor <= frame_end:
        holes.append([cursor, frame_end])
    return [h for h in holes if h[0] <= h[1]]


def shot_coverage(shots, frame_start, frame_end):
    errors = []
    warnings = []
    rows = sorted(([int(s['frame_start']), int(s['frame_end']), s.get('shot_id')] for s in shots
                   if isinstance(s.get('frame_start'), int) and isinstance(s.get('frame_end'), int)))
    if not rows:
        return {'shots': 0, 'contiguous': None, 'note': 'no shot rows with integer frame ranges'}, errors, warnings
    if rows[0][0] != frame_start:
        errors.append({'code': 'SHOTS_START_MISMATCH', 'detail': 'first shot starts at ' + str(rows[0][0]) + ', timeline at ' + str(frame_start)})
    if rows[-1][1] != frame_end:
        errors.append({'code': 'SHOTS_END_MISMATCH', 'detail': 'last shot ends at ' + str(rows[-1][1]) + ', timeline at ' + str(frame_end)})
    for previous, current in zip(rows, rows[1:]):
        if current[0] > previous[1] + 1:
            errors.append({'code': 'SHOT_GAP', 'detail': str(previous[2]) + ' ends ' + str(previous[1]) + ', ' + str(current[2]) + ' starts ' + str(current[0])})
        elif current[0] <= previous[1]:
            errors.append({'code': 'SHOT_OVERLAP', 'detail': str(previous[2]) + ' and ' + str(current[2]) + ' share frames ' + str(current[0]) + '-' + str(min(previous[1], current[1]))})
    return {'shots': len(rows), 'contiguous': not errors, 'ranges': [r[:3] for r in rows[:20]]}, errors, warnings


def clip_range(clip):
    """Frame range of an actor clip row as recorded by the staging/sync step; None when the row is not readable."""
    if not isinstance(clip, dict):
        return None
    start = clip.get('frame_start', clip.get('start'))
    end = clip.get('frame_end', clip.get('end'))
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        return [int(math.floor(start)), int(math.ceil(end))]
    afs, afe = clip.get('action_frame_start'), clip.get('action_frame_end')
    if isinstance(start, (int, float)) and isinstance(afs, (int, float)) and isinstance(afe, (int, float)):
        scale = clip.get('scale', 1.0) or 1.0
        repeat = clip.get('repeat', 1.0) or 1.0
        return [int(math.floor(start)), int(math.ceil(start + (afe - afs) * scale * repeat))]
    return None


def actor_coverage(actors, frame_start, frame_end):
    errors = []
    warnings = []
    report = {}
    for actor_id, row in sorted((actors or {}).items()):
        if not isinstance(row, dict):
            continue
        entry = {}
        clips = row.get('clips')
        if clips:
            ranges = [clip_range(c) for c in clips]
            readable = [r for r in ranges if r is not None]
            entry['clips'] = len(clips)
            entry['unreadable_clips'] = len(ranges) - len(readable)
            holes = gaps(readable, frame_start, frame_end)
            entry['uncovered'] = holes[:10]
            if holes:
                warnings.append({'code': 'ACTOR_CLIP_GAP', 'detail': actor_id + ' has no clip in ' + str(holes[:3]),
                                 'note': 'extrapolation may hold a pose there; review'})
            if len(ranges) != len(readable):
                warnings.append({'code': 'ACTOR_CLIP_UNREADABLE', 'detail': actor_id + ': ' + str(len(ranges) - len(readable)) + ' clip rows without a frame range'})
        secondary = row.get('secondary')
        if secondary:
            windows = []
            for item in secondary if isinstance(secondary, list) else [secondary]:
                window = None
                if isinstance(item, dict):
                    if isinstance(item.get('window'), list) and len(item['window']) == 2:
                        window = item['window']
                    elif isinstance(item.get('frame_start'), (int, float)) and isinstance(item.get('frame_end'), (int, float)):
                        window = [item['frame_start'], item['frame_end']]
                if window is None:
                    warnings.append({'code': 'SECONDARY_WINDOW_UNREADABLE', 'detail': actor_id})
                    continue
                windows.append(window)
                if window[0] > frame_start or window[1] < frame_end:
                    if window[1] < frame_start or window[0] > frame_end:
                        errors.append({'code': 'SECONDARY_WINDOW_OUTSIDE', 'detail': actor_id + ' cache window ' + str(window) + ' outside the timeline'})
                    else:
                        warnings.append({'code': 'SECONDARY_WINDOW_PARTIAL', 'detail': actor_id + ' cache window ' + str(window) + ' covers part of ' + str([frame_start, frame_end])})
            entry['secondary_windows'] = windows[:10]
        if entry:
            report[actor_id] = entry
    return report, errors, warnings


def coverage_from(plan, state, markers, cameras_alive, scene_range, min_frames):
    """Pure coverage report: shots contiguity, cuts (recorded and in Blender), actor clips and M8 cache windows."""
    frame_start, frame_end = scene_range
    errors = []
    warnings = []
    shots, e, w = shot_coverage(shot_rows(plan, state), frame_start, frame_end)
    errors += e
    warnings += w
    expected = state.get('cuts') or []
    if markers is not None:
        e, w = audit_cut_markers(markers, expected, frame_start, frame_end, min_frames)
        errors += e
        warnings += w
    for row in expected:
        if cameras_alive is not None and not cameras_alive.get(row['camera_object'], False):
            errors.append({'code': 'CUT_CAMERA_MISSING', 'detail': row['marker'] + ' -> ' + row['camera_object']})
    if expected:
        cut_ranges = [[r['frame'], r['frame_end']] for r in expected]
        holes = gaps(cut_ranges, frame_start, frame_end)
        if holes:
            errors.append({'code': 'CUTS_DO_NOT_COVER_TIMELINE', 'detail': str(holes[:5])})
        shot_by_id = {s.get('shot_id'): s for s in shot_rows(plan, state)}
        for row in expected:
            shot = shot_by_id.get(row['shot_id'])
            if shot and (shot.get('frame_start') != row['frame'] or shot.get('frame_end') != row['frame_end']):
                warnings.append({'code': 'CUT_SHOT_MISMATCH', 'detail': row['shot_id'] + ' cut ' + str([row['frame'], row['frame_end']])
                                 + ' vs shot ' + str([shot.get('frame_start'), shot.get('frame_end')])})
    else:
        warnings.append({'code': 'CUTS_MISSING', 'detail': 'no cuts recorded; the candidate renders with scene.camera only'})
    actors, e, w = actor_coverage(state.get('actors'), frame_start, frame_end)
    errors += e
    warnings += w
    return {'shots': shots, 'cuts': {'recorded': len(expected),
                                     'rows': [[r['shot_id'], r['camera_id'], r['frame'], r['frame_end']] for r in expected[:30]]},
            'actors': actors, 'errors': errors[:50], 'warnings': warnings[:50],
            'error_count': len(errors), 'warning_count': len(warnings)}


# ---------------------------------------------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------------------------------------------


def load_candidate(ctx, req):
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, model.ASSEMBLY_STATES)
    if not state.get('assembly'):
        fail('CINEMATIC_CANDIDATE_REQUIRED', 'create the candidate scene first')
    return path, plan, state


def proposal(path, plan, state, step, body):
    # The ledger summary goes first so the step's own keys (e.g. 'timeline') are never shadowed by it.
    return {**store.public(path, plan, state), 'status': 'proposal', 'apply': False, 'step': step, **body,
            'next': 'call again with apply=true to change the candidate scene'}


def with_registry_roots(value, roots):
    """Copy of a payload parameter tree where every actor target without a bone carries the registry's root bone
    (`root_bone`), so the payload does not have to pick one parentless bone (IK controls are parentless too)."""
    if isinstance(value, dict):
        row = {k: with_registry_roots(v, roots) for k, v in value.items()}
        if row.get('kind') == 'actor' and row.get('bone') is None and roots.get(row.get('actor_id')):
            row['root_bone'] = roots[row['actor_id']]
        return row
    if isinstance(value, list):
        return [with_registry_roots(v, roots) for v in value]
    return value


def run(ctx, path, plan, state, label, body, extra):
    roots = {aid: registry_bone(record, 'root') for aid, record in actor_records(ctx, plan).items()}
    extra = with_registry_roots(extra, roots)
    params = store.assembly_params(plan, state, dict(extra, capture=actor_objects(plan.get('actors') or [])))
    return store.mutate(ctx, path, plan, state, label, body, params)


def status_of(errors, warnings):
    return 'error' if errors else 'warning' if warnings else 'success'


CREATE_KEYS = {'camera_id', 'shot_id', 'type', 'lens_mm', 'lens_unit', 'sensor_width_mm', 'sensor_height_mm', 'sensor_fit',
               'shift_x', 'shift_y', 'clip_start', 'clip_end', 'ortho_scale', 'location', 'rotation_euler_deg', 'look_at',
               'track', 'reference_frame', 'set_scene_camera'}


@handler('cinematic_create_camera')
def create_camera(ctx, req):
    opts = model.closed(req.options, CREATE_KEYS)
    path, plan, state = load_candidate(ctx, req)
    camera_id = model.name(opts.get('camera_id'), 'camera_id')
    cameras = state.get('cameras') or {}
    if camera_id in cameras:
        fail('CAMERA_EXISTS', camera_id + ' already exists; use cinematic_configure_camera')
    if len(cameras) >= model.MAX_CAMERAS:
        fail('CINEMATIC_CAPACITY', 'at most ' + str(model.MAX_CAMERAS) + ' cameras')
    names = camera_names(plan, state, camera_id)
    shot_id = None
    if opts.get('shot_id') is not None:
        shot_id = find_shot(plan, state, opts['shot_id'])['shot_id']
    settings, provenance = normalize_settings(opts)
    if 'location' not in opts:
        fail('CAMERA_PLACEMENT_REQUIRED', 'options.location (metres) is required; placement is never inferred')
    location = model.vector(opts['location'], 'location')
    if ('look_at' in opts) == ('rotation_euler_deg' in opts):
        fail('CAMERA_ORIENTATION_REQUIRED', 'pass exactly one of look_at or rotation_euler_deg')
    look_at = resolve_target(plan, opts['look_at'], 'look_at') if 'look_at' in opts else None
    rotation = model.vector(opts['rotation_euler_deg'], 'rotation_euler_deg', minimum=-3600, maximum=3600) if 'rotation_euler_deg' in opts else None
    track = model.choice(opts.get('track', 'NONE'), 'track', TRACK_MODES)
    if track != 'NONE' and (look_at is None or look_at['kind'] == 'point'):
        fail('CAMERA_TRACK_TARGET_OBJECT_REQUIRED', track + ' needs look_at with actor_id or object')
    start, end = timeline_range(plan, state)
    if opts.get('reference_frame') is not None:
        reference = integer(opts['reference_frame'], 'reference_frame', start, end)
    elif shot_id is not None:
        reference = int(find_shot(plan, state, shot_id)['frame_start'])
    else:
        reference = start
    set_scene_camera = boolean(opts.get('set_scene_camera', True), 'set_scene_camera')
    spec = {'camera_id': camera_id, 'object': names['object'], 'data': names['data'], 'shot_id': shot_id,
            'settings': settings, 'location_m': location, 'look_at': look_at, 'rotation_euler_deg': rotation,
            'track': track, 'reference_frame': reference}
    if not req.apply:
        return proposal(path, plan, state, 'create_camera', {'camera': spec, 'provenance': provenance,
                                                              'measurements': 'look_at rotation is computed at apply from the evaluated target'})
    new, result = run(ctx, path, plan, state, 'create_camera', CREATE_BODY,
                      {'camera': names['object'], 'camera_data': names['data'], 'settings': settings, 'location': location,
                       'rotation_euler_deg': rotation, 'look_at': look_at, 'reference_frame': reference,
                       'track': {'mode': track, 'name': names['constraint']}, 'set_scene_camera': set_scene_camera})
    new.setdefault('cameras', {})[camera_id] = {
        'object': result['object'], 'data': result['data'], 'shot_id': shot_id, 'settings': result['settings'],
        'provenance': provenance,
        'placement': {'location_m': result['location_m'], 'rotation_euler_deg': result['rotation_euler_deg'],
                      'look_at': look_at, 'reference_frame': reference, 'target_point_m': result['target_point_m'],
                      'target_resolved': result['target_resolved']},
        'constraints': [{'name': names['constraint'], 'type': track, 'target': look_at}] if track != 'NONE' else [],
        'animation': None, 'focus': None}
    store.record(path, new)
    return {**store.public(path, plan, new), 'status': 'success', 'apply': True, 'step': 'create_camera', 'camera_id': camera_id,
            'object': result['object'], 'data': result['data'], 'settings': result['settings'],
            'location_m': result['location_m'], 'rotation_euler_deg': result['rotation_euler_deg'],
            'target_point_m': result['target_point_m'], 'target_resolved': result['target_resolved'],
            'scene_camera': result['scene_camera'], 'provenance': provenance}


CONFIGURE_KEYS = {'camera_id', 'type', 'lens_mm', 'lens_unit', 'sensor_width_mm', 'sensor_height_mm', 'sensor_fit', 'shift_x',
                  'shift_y', 'clip_start', 'clip_end', 'ortho_scale', 'dof', 'tracking'}


@handler('cinematic_configure_camera')
def configure_camera(ctx, req):
    opts = model.closed(req.options, CONFIGURE_KEYS)
    path, plan, state = load_candidate(ctx, req)
    camera_id, camera = owned_camera(plan, state, opts, req)
    current = {k: v for k, v in (camera.get('settings') or {}).items() if k in DEFAULTS}
    settings, provenance = normalize_settings({k: v for k, v in opts.items() if k not in {'camera_id', 'dof', 'tracking'}}, current)
    dof = normalize_dof(plan, opts.get('dof'))
    tracking = None
    if opts.get('tracking') is not None:
        value = model.closed(opts['tracking'], {'mode', 'target', 'influence'}, 'CAMERA_TRACKING_INVALID')
        mode = model.choice(value.get('mode'), 'tracking.mode', TRACK_MODES)
        target = None
        if mode != 'NONE':
            if 'target' not in value:
                fail('CAMERA_TRACK_TARGET_OBJECT_REQUIRED', 'tracking.target is required')
            target = resolve_target(plan, value['target'], 'tracking.target', allow_point=False)
        influence = model.number(value.get('influence', 1.0), 'tracking.influence', 0.0, 1.0)
        tracking = {'mode': mode, 'target': target, 'influence': influence,
                    'name': camera_names(plan, state, camera_id)['constraint']}
    if not settings and not dof and tracking is None:
        fail('CAMERA_CONFIGURATION_EMPTY', 'nothing to change')
    change = {'settings': settings, 'dof': dof, 'tracking': tracking}
    if not req.apply:
        return proposal(path, plan, state, 'configure_camera', {'camera_id': camera_id, 'object': camera['object'],
                                                                 'before': camera.get('settings'), 'change': change})
    new, result = run(ctx, path, plan, state, 'configure_camera', CONFIGURE_BODY,
                      {'camera': camera['object'], 'settings': settings, 'dof': dof, 'tracking': tracking})
    row = new['cameras'][camera_id]
    row['settings'] = result['after']
    if tracking is not None:
        row['constraints'] = [{'name': tracking['name'], 'type': tracking['mode'], 'target': tracking['target'],
                               'influence': tracking['influence']}] if tracking['mode'] != 'NONE' else []
    store.record(path, new)
    return {**store.public(path, plan, new), 'status': 'success', 'apply': True, 'step': 'configure_camera', 'camera_id': camera_id,
            'object': result['object'], 'before': result['before'], 'after': result['after'],
            'constraints': result['constraints']}


ANIMATE_KEYS = {'camera_id', 'shot_id', 'frame_start', 'frame_end', 'move', 'params', 'easing', 'target', 'replace'}


@handler('cinematic_animate_camera')
def animate_camera(ctx, req):
    opts = model.closed(req.options, ANIMATE_KEYS)
    path, plan, state = load_candidate(ctx, req)
    camera_id, camera = owned_camera(plan, state, opts, req)
    first, last, range_source = frame_range(plan, state, opts, camera)
    if last - first < 1:
        fail('CAMERA_RANGE_TOO_SHORT', 'a camera move needs at least 2 frames')
    move = model.choice(opts.get('move'), 'move', model.CAMERA_MOVES)
    easing = model.choice(opts.get('easing', 'LINEAR'), 'easing', model.EASINGS)
    target = resolve_target(plan, opts['target'], 'target') if opts.get('target') is not None else None
    params, provenance = normalize_move(move, opts.get('params'), target)
    replace = boolean(opts.get('replace', False), 'replace')
    placement = camera.get('placement') or {}
    base_location = params.pop('start_location', None) or placement.get('location_m')
    base_rotation = params.pop('start_rotation_deg', None) or placement.get('rotation_euler_deg')
    warnings = []
    if move == 'HANDHELD':
        warnings.append({'code': 'HANDHELD_IS_PROCEDURAL_NOISE',
                         'detail': 'deterministic bounded noise (seeded sines); not a substitute for operated camera work'})
    if any(c.get('type') in ('TRACK_TO', 'DAMPED_TRACK') for c in camera.get('constraints') or []):
        warnings.append({'code': 'CAMERA_ROTATION_FROM_CONSTRAINT',
                         'detail': 'a tracking constraint overrides the keyed rotation; evaluated_analysis reflects it'})
    channels = 7 if params.get('lens_end_mm') is not None else 6
    spec = {'camera_id': camera_id, 'move': move, 'easing': easing, 'frames': [first, last], 'range_source': range_source,
            'params': params, 'target': target, 'replace': replace, 'keys_to_write': (last - first + 1) * channels,
            'interpolation': 'LINEAR, one key per frame', 'base_location_m': base_location,
            'base_rotation_euler_deg': base_rotation}
    if not req.apply:
        return proposal(path, plan, state, 'animate_camera', {'animation': spec, 'provenance': provenance,
                                                               'warnings': warnings})
    names = camera_names(plan, state, camera_id)
    lens_start = (camera.get('settings') or {}).get('lens', DEFAULTS['lens'])
    new, result = run(ctx, path, plan, state, 'animate_camera', ANIMATE_BODY,
                      {'camera': camera['object'], 'action': names['action'], 'frame_start': first, 'frame_end': last,
                       'move': move, 'easing': easing, 'params': params, 'target': target, 'replace': replace,
                       'base_location': base_location, 'base_rotation_deg': base_rotation, 'lens_start_mm': lens_start})
    row = new['cameras'][camera_id]
    animation = row.get('animation') or {'action': result['action'], 'slots': result['slots'], 'ranges': []}
    animation['ranges'] = [r for r in animation['ranges'] if r['frame_end'] < first or r['frame_start'] > last]
    animation['ranges'].append({'frame_start': first, 'frame_end': last, 'move': move, 'easing': easing, 'params': params,
                                'target': target, 'lens_keyed': result['lens_keyed'], 'keys_written': result['keys_written'],
                                'verification': result['verification']})
    animation['ranges'].sort(key=range_start)
    row['animation'] = animation
    store.record(path, new)
    if result['verification']['target_behind_camera']['count']:
        warnings.append({'code': 'CAMERA_TARGET_BEHIND', 'detail': 'target behind the camera on '
                         + str(result['verification']['target_behind_camera']['count']) + ' frames'})
    return {**store.public(path, plan, new), 'status': status_of([], warnings), 'apply': True, 'step': 'animate_camera', 'camera_id': camera_id,
            'object': result['object'], 'action': result['action'], 'slots': result['slots'], 'move': move,
            'easing': easing, 'frames': result['frames'], 'keys_written': result['keys_written'],
            'start_location_m': result['start_location_m'], 'end_location_m': result['end_location_m'],
            'trajectory': {'keys': result['keys_analysis'], 'evaluated': result['evaluated_analysis']},
            'verification': result['verification'], 'warnings': warnings, 'provenance': provenance,
            **store.public(path, plan, new)}


def range_start(row):
    return row['frame_start']


CUTS_KEYS = {'cuts', 'from_shots', 'min_shot_frames', 'clear'}


@handler('cinematic_create_cuts')
def create_cuts(ctx, req):
    opts = model.closed(req.options, CUTS_KEYS)
    path, plan, state = load_candidate(ctx, req)
    start, end = timeline_range(plan, state)
    min_frames = integer(opts.get('min_shot_frames', MIN_SHOT_FRAMES), 'min_shot_frames', 1, 100000)
    cameras = state.get('cameras') or {}
    clear = boolean(opts.get('clear', False), 'clear')
    if clear and (opts.get('cuts') or opts.get('from_shots')):
        fail('CUTS_AMBIGUOUS', 'clear removes every cut; do not pass cuts with it')
    if clear:
        rows, warnings = [], []
    else:
        if opts.get('cuts') is not None:
            if not isinstance(opts['cuts'], list):
                fail('CUT_INVALID', 'cuts must be a list')
            source_rows = opts['cuts']
            source = 'options'
        else:
            source_rows = [{'shot_id': s.get('shot_id'), 'camera_id': s.get('camera_id'), 'frame': s.get('frame_start')}
                           for s in shot_rows(plan, state)]
            if not source_rows or any(r['camera_id'] is None for r in source_rows):
                fail('CUTS_REQUIRED', 'pass options.cuts; the shot plan has no camera_id for every shot')
            source = 'shots'
        rows, errors, warnings = check_cuts(source_rows, start, end, min_frames, cameras)
        if errors:
            raise_errors(errors)
        for row in rows:
            row['camera_object'] = cameras[row['camera_id']]['object']
        shots = {s.get('shot_id'): s for s in shot_rows(plan, state)}
        for row in rows:
            shot = shots.get(row['shot_id'])
            if shot and (shot.get('frame_start') != row['frame'] or shot.get('frame_end') != row['frame_end']):
                warnings.append({'code': 'CUT_SHOT_MISMATCH', 'detail': row['shot_id'] + ' cut ' + str([row['frame'], row['frame_end']])
                                 + ' vs shot ' + str([shot.get('frame_start'), shot.get('frame_end')])})
    previous = [r['marker'] for r in state.get('cuts') or []]
    if not req.apply:
        return proposal(path, plan, state, 'create_cuts', {'cuts': rows, 'replaces': previous, 'warnings': warnings,
                                                            'threshold': {'min_shot_frames': min_frames,
                                                                          'threshold_source': 'PROPOSAL' if 'min_shot_frames' not in opts else 'USER_REQUIREMENT'}})
    new, result = run(ctx, path, plan, state, 'create_cuts', CUTS_BODY,
                      {'cuts': [{'marker': r['marker'], 'shot_id': r['shot_id'], 'camera': r['camera_object'],
                                 'frame': r['frame'], 'frame_end': r['frame_end']} for r in rows],
                       'remove': previous})
    timeline = dict(new.get('timeline') or plan['timeline'])
    timeline['markers'] = [m[:3] for m in result['timeline']['markers']]
    new['timeline'] = timeline
    new['cuts'] = rows
    errors = [{'code': 'CUT_CAMERA_NOT_ACTIVE', 'detail': str(c)} for c in result['checks'] if not c[4]]
    audit_errors, audit_warnings = audit_cut_markers(result['timeline']['markers'], rows, start, end, min_frames)
    errors += audit_errors
    warnings += audit_warnings
    new['cuts_verification'] = {'checks': len(result['checks']), 'errors': errors}
    store.record(path, new)
    return {**store.public(path, plan, new), 'status': status_of(errors, warnings), 'apply': True, 'step': 'create_cuts', 'cuts': len(rows),
            'removed_markers': result['removed'], 'camera_checks': result['checks'][:40],
            'camera_checks_passed': sum(1 for c in result['checks'] if c[4]), 'errors': errors, 'warnings': warnings,
            'threshold': {'min_shot_frames': min_frames,
                          'threshold_source': 'PROPOSAL' if 'min_shot_frames' not in opts else 'USER_REQUIREMENT'},
            **store.public(path, plan, new)}


FOCUS_KEYS = {'camera_id', 'shot_id', 'frame_start', 'frame_end', 'focus', 'pulls', 'aperture_fstop', 'thresholds', 'replace'}


def sensor_diagonal_coc(camera):
    settings = camera.get('settings') or {}
    width = settings.get('sensor_width', DEFAULTS['sensor_width'])
    height = settings.get('sensor_height', DEFAULTS['sensor_height'])
    return math.sqrt(width * width + height * height) / 1500.0


def normalize_focus(plan, opts, first, last, camera):
    rows = opts.get('focus')
    if not isinstance(rows, list) or not rows:
        fail('FOCUS_ROWS_REQUIRED', 'options.focus must be a non-empty list of {frame, target}')
    pulls = model.closed(opts.get('pulls') or {}, {'frames', 'easing'}, 'FOCUS_PULLS_INVALID')
    pull_frames = integer(pulls.get('frames', FOCUS_PULL_DEFAULTS['frames']), 'pulls.frames', 0, 1000)
    pull_easing = model.choice(pulls.get('easing', FOCUS_PULL_DEFAULTS['easing']), 'pulls.easing', model.EASINGS)
    targets = {}
    normalized = []
    previous = None
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            fail('FOCUS_ROW_INVALID', 'focus[' + str(index) + '] must be an object')
        row = model.closed(row, {'frame', 'target', 'pull_frames', 'subject'}, 'FOCUS_ROW_INVALID')
        frame = integer(row.get('frame'), 'focus[' + str(index) + '].frame', first, last)
        if previous is not None and frame <= previous:
            fail('FOCUS_ORDER_INVALID', 'focus frames must be strictly increasing')
        previous = frame
        target = resolve_target(plan, row.get('target'), 'focus[' + str(index) + '].target', allow_point=False, allow_distance=True)
        entry = {'frame': frame, 'target': target,
                 'pull_frames': integer(row.get('pull_frames', pull_frames), 'focus[' + str(index) + '].pull_frames', 0, 1000) if index else 0}
        if target['kind'] != 'distance':
            key = target['object'] + '|' + str(target.get('bone'))
            targets[key] = target
            entry['target_key'] = key
        if row.get('subject') is not None:
            subject = resolve_target(plan, row['subject'], 'focus[' + str(index) + '].subject', allow_point=False)
            key = subject['object'] + '|' + str(subject.get('bone'))
            targets[key] = subject
            entry['subject_key'] = key
        normalized.append(entry)
    mode = 'OBJECT' if len(normalized) == 1 and 'target_key' in normalized[0] else 'KEYED'
    thresholds = model.closed(opts.get('thresholds') or {}, {'in_focus_coc_mm', 'excessive_coc_ratio', 'jump_ratio'}, 'FOCUS_THRESHOLDS_INVALID')
    out = {'in_focus_coc_mm': model.number(thresholds['in_focus_coc_mm'], 'thresholds.in_focus_coc_mm', 1e-6, 10.0)
           if 'in_focus_coc_mm' in thresholds else round(sensor_diagonal_coc(camera), 6),
           'in_focus_source': 'USER_REQUIREMENT' if 'in_focus_coc_mm' in thresholds else 'PROPOSAL (sensor diagonal / 1500 convention)',
           'excessive_coc_ratio': model.number(thresholds.get('excessive_coc_ratio', FOCUS_THRESHOLDS['excessive_coc_ratio']), 'thresholds.excessive_coc_ratio', 1e-6, 1.0),
           'jump_ratio': model.number(thresholds.get('jump_ratio', FOCUS_THRESHOLDS['jump_ratio']), 'thresholds.jump_ratio', 1e-6, 100.0),
           'keyed_tolerance_m': FOCUS_THRESHOLDS['keyed_tolerance_m']}
    return normalized, targets, mode, pull_easing, out


@handler('cinematic_plan_focus')
def plan_focus(ctx, req):
    opts = model.closed(req.options, FOCUS_KEYS)
    path, plan, state = load_candidate(ctx, req)
    camera_id, camera = owned_camera(plan, state, opts, req)
    first, last, range_source = frame_range(plan, state, opts, camera)
    rows, targets, mode, pull_easing, thresholds = normalize_focus(plan, opts, first, last, camera)
    fstop = ranged(opts['aperture_fstop'], 'aperture_fstop', 'aperture_fstop') if opts.get('aperture_fstop') is not None else None
    replace = boolean(opts.get('replace', False), 'replace')
    actors = [{'kind': 'actor', 'actor_id': a['actor_id'], 'object': a['object'], 'bone': None} for a in plan.get('actors') or []]
    windows = [[r['frame'], min(last, r['frame'] + r['pull_frames'] - 1)] for r in rows if r['pull_frames'] > 0]
    spec = {'camera_id': camera_id, 'mode': mode, 'frames': [first, last], 'range_source': range_source, 'rows': rows,
            'pull_windows': windows, 'pull_easing': pull_easing, 'aperture_fstop': fstop, 'thresholds': thresholds,
            'formula': 'CoC_mm = f^2 / (N (s - f)) * |d - s| / d  (thin lens; f focal length, N f-number, s focus distance, '
                       'd subject depth along the view axis)',
            'mode_rule': 'OBJECT = one object/actor target, no pulls: dof.focus_object follows it; KEYED = '
                         'dof.focus_distance keyed every frame in the CAMERA slot of the camera Action'}
    if not req.apply:
        return proposal(path, plan, state, 'plan_focus', {'focus': spec,
                                                           'measurements': 'depths, CoC, jumps and lost focus are measured at apply'})
    names = camera_names(plan, state, camera_id)
    new, result = run(ctx, path, plan, state, 'plan_focus', FOCUS_BODY,
                      {'camera': camera['object'], 'action': names['action'], 'frame_start': first, 'frame_end': last,
                       'rows': rows, 'targets': targets, 'mode': mode, 'pull_easing': pull_easing, 'aperture_fstop': fstop,
                       'thresholds': thresholds, 'actors': actors, 'replace': replace})
    row = new['cameras'][camera_id]
    row['focus'] = {'mode': mode, 'frames': [first, last], 'rows': rows, 'pull_windows': windows, 'thresholds': thresholds,
                    'findings': [f['code'] for f in result['findings']], 'focus_range_m': result['focus_range_m']}
    if result['action'] and not row.get('animation'):
        row['animation'] = {'action': result['action'], 'ranges': []}
    settings = dict(row.get('settings') or {})
    dof = dict(settings.get('dof') or {})
    dof.update({'use_dof': True, 'focus_object': targets[rows[0]['target_key']]['object'] if mode == 'OBJECT' else None})
    if fstop is not None:
        dof['aperture_fstop'] = fstop
    settings['dof'] = dof
    row['settings'] = settings
    store.record(path, new)
    errors = [f for f in result['findings'] if f['severity'] == 'error']
    warnings = [f for f in result['findings'] if f['severity'] != 'error']
    return {**store.public(path, plan, new), 'status': status_of(errors, warnings), 'apply': True, 'step': 'plan_focus', 'camera_id': camera_id,
            'object': result['object'], 'mode': mode, 'frames': result['frames'], 'keys_written': result['keys_written'],
            'action': result['action'], 'focus_range_m': result['focus_range_m'], 'pull_frames': result['pull_frames'],
            'target_coc_mm_max': result['target_coc_mm_max'], 'unverified_frames': result['unverified_frames'],
            'keyed_max_error_m': result['keyed_max_error_m'], 'errors': errors, 'warnings': warnings,
            'formula': spec['formula'], 'thresholds': thresholds}


TIMELINE_KEYS = {'frame_start', 'frame_end', 'markers', 'replace_markers', 'conversions', 'interactions', 'finalize',
                 'min_shot_frames'}


def normalize_markers(rows, timeline, frame_start, frame_end):
    if not isinstance(rows, list):
        fail('TIMELINE_MARKERS_INVALID', 'markers must be a list')
    out = []
    conversions = []
    names = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            fail('TIMELINE_MARKERS_INVALID', 'markers[' + str(index) + '] must be an object')
        row = model.closed(row, {'kind', 'id', 'frame', 'seconds'}, 'TIMELINE_MARKERS_INVALID')
        kind = model.choice(row.get('kind'), 'markers[' + str(index) + '].kind', ('BEAT', 'EVENT'))
        marker_id = model.name(row.get('id'), 'markers[' + str(index) + '].id')
        name = kind + '_' + marker_id
        if name in names:
            fail('TIMELINE_MARKER_DUPLICATE', name)
        names.add(name)
        if ('frame' in row) == ('seconds' in row):
            fail('TIMELINE_MARKERS_INVALID', name + ' needs exactly one of frame or seconds')
        if 'frame' in row:
            frame = integer(row['frame'], name + '.frame')
            source = 'frame'
        else:
            frame, record = model.seconds_to_frame(row['seconds'], timeline['fps'], timeline.get('fps_base', 1.0), frame_start)
            conversions.append(dict(record, purpose='marker:' + name))
            source = 'seconds'
        if not frame_start <= frame <= frame_end:
            fail('TIMELINE_MARKER_OUTSIDE', name + ' at ' + str(frame) + ' outside ' + str(frame_start) + '-' + str(frame_end))
        out.append({'name': name, 'kind': kind, 'id': marker_id, 'frame': frame, 'source': source})
    return out, conversions


def normalize_conversions(rows, timeline, frame_start):
    if not isinstance(rows, list):
        fail('TIMELINE_CONVERSIONS_INVALID', 'conversions must be a list')
    out = []
    for index, row in enumerate(rows):
        row = model.closed(row if isinstance(row, dict) else {'?': row}, {'seconds', 'frame', 'purpose'}, 'TIMELINE_CONVERSIONS_INVALID')
        purpose = model.text(row.get('purpose', 'conversion ' + str(index)), 'conversions[' + str(index) + '].purpose', 200)
        if ('seconds' in row) == ('frame' in row):
            fail('TIMELINE_CONVERSIONS_INVALID', 'conversion ' + str(index) + ' needs exactly one of seconds or frame')
        if 'seconds' in row:
            frame, record = model.seconds_to_frame(row['seconds'], timeline['fps'], timeline.get('fps_base', 1.0), frame_start)
            out.append(dict(record, purpose=purpose, direction='seconds_to_frame'))
        else:
            frame = integer(row['frame'], 'conversions[' + str(index) + '].frame')
            seconds = model.frame_to_seconds(frame, timeline['fps'], timeline.get('fps_base', 1.0), frame_start)
            out.append({'frame': frame, 'seconds': seconds, 'fps': timeline['fps'], 'fps_base': timeline.get('fps_base', 1.0),
                        'frame_start': frame_start, 'purpose': purpose, 'direction': 'frame_to_seconds',
                        'rule': 'seconds = (frame - frame_start) * fps_base / fps'})
    return out


# ---------------------------------------------------------------------------------------------------------------
# Interaction events -> state['interactions'] (the rows analysis._events reads; docs/cinematic-visual-qa.md §5)
# ---------------------------------------------------------------------------------------------------------------

INTERACTION_SOURCE = 'interaction'      # narrative rows owned by options.interactions (replaced together)
MAX_INTERACTION_EVENTS = 64             # declared capacity per cinematic (policy, not a Blender limit)
MAX_SURFACES = 16
EFFECTOR_ENDS = ('TAIL', 'HEAD', 'MID')
EVENT_KEYS = {'event_id', 'kind', 'actor_id', 'frame', 'frame_start', 'frame_end', 'effector', 'target', 'contact',
              'head_bone', 'forward_axis', 'reach_m'}
TARGET_KEYS = {'actor_id', 'bone', 'end', 'object', 'surfaces', 'generic', 'point'}


def registry_bone(record, role):
    """Bone the actor registry resolved for a role (explicit, or M3 semantics at or above its confidence), else None.
    UNKNOWN and absent roles stay None: a bone is never guessed from its name."""
    bones = (record or {}).get('bones')
    value = bones.get(role) if isinstance(bones, dict) else None
    if isinstance(value, dict):
        value = value.get('bone')
    return value if isinstance(value, str) and value and value != 'UNKNOWN' else None


def registry_reach(record):
    """interaction_capabilities.reach_m of the registry (a rest-pose estimate), or None when UNKNOWN."""
    caps = (record or {}).get('interaction_capabilities')
    value = caps.get('reach_m') if isinstance(caps, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def prop_copies(state):
    """Original prop -> owned copy. Originals are excluded from the candidate, so the QA must measure the copy."""
    copies = {}
    for row in (state.get('props') or {}).values():
        if isinstance(row, dict) and isinstance(row.get('original'), str) and isinstance(row.get('copy'), str):
            copies[row['original']] = row['copy']
    return copies


def actor_records(ctx, plan):
    """{actor_id: registry record or None}. A missing record leaves the actor's bones unknown (never guessed); the
    integrity gate of finalize reports it."""
    key = plan.get('actor_registry_key')
    records = {}
    for row in plan.get('actors') or []:
        try:
            records[row['actor_id']] = registry.find(ctx.storage, key, row['actor_id']) if key else None
        except ValueError:
            records[row['actor_id']] = None
    return records


def make_event(event_id, kind, actor_id, frame, first, last, effector, target, contact, head_bone=None,
               forward_axis=None, reach_m=None):
    """One state['interactions'] row, with exactly the keys analysis._events reads."""
    event = {'event_id': event_id, 'kind': kind, 'actor_id': actor_id, 'frame': frame, 'frame_start': first,
             'frame_end': last, 'effector': effector, 'target': target, 'contact': contact}
    if head_bone:
        event['head_bone'] = head_bone
    if forward_axis is not None:
        event['forward_axis'] = forward_axis
    if reach_m is not None:
        event['reach_m'] = reach_m
    return event


def missing_effector(warnings, event_id, actor_id, detail):
    warnings.append({'code': 'INTERACTION_EFFECTOR_UNKNOWN', 'event_id': event_id, 'actor_id': actor_id,
                     'detail': detail + '; contacts are never inferred from bone names (register the bone explicitly)'})


def planned_interactions(result, actor_ids, records, timeline, copies, roles, modes):
    """Rows of a cinematic_plan_interaction result (production.cinematic_interactions.v1). The performer is the first
    actor; effector roles (hand_right, ...) resolve through the actor registry, any other effector name is an explicit
    bone that the payload verifies in Blender before writing."""
    if result.get('schema') != planning.INTERACTIONS_SCHEMA:
        fail('INTERACTIONS_INVALID', 'options.interactions must be the result of cinematic_plan_interaction ('
             + planning.INTERACTIONS_SCHEMA + ') or a list of analysis events')
    planned = result.get('timeline') if isinstance(result.get('timeline'), dict) else {}
    expected = {'fps': timeline['fps'], 'fps_base': timeline.get('fps_base', 1.0), 'frame_start': int(timeline['frame_start'])}
    for key, value in expected.items():
        other = planned.get(key)
        if isinstance(other, bool) or not isinstance(other, (int, float)) or abs(other - value) > 1e-9:
            fail('INTERACTIONS_TIMELINE_MISMATCH', 'planned with ' + key + '=' + str(other) + ', the candidate has '
                 + str(value) + '; plan the interactions again against the candidate timeline')
    rows = result.get('interactions')
    if not isinstance(rows, list):
        fail('INTERACTIONS_INVALID', 'interactions must be a list')
    if len(rows) > MAX_INTERACTION_EVENTS:
        fail('CINEMATIC_CAPACITY', 'at most ' + str(MAX_INTERACTION_EVENTS) + ' interaction events')
    rate = (expected['fps'], expected['fps_base'], expected['frame_start'])
    events, provenance, conversions, warnings = [], {}, [], []
    for index, row in enumerate(rows):
        field = 'interactions[' + str(index) + ']'
        if not isinstance(row, dict):
            fail('INTERACTION_INVALID', field + ' must be an object')
        kind = model.choice(row.get('kind'), field + '.kind', model.INTERACTION_KINDS)
        event_id = model.name(row.get('event_id'), field + '.event_id')
        performers = row.get('actors')
        if not isinstance(performers, list) or not performers:
            fail('INTERACTION_INVALID', field + '.actors must list the performing actor first')
        for actor_id in performers:
            if actor_id not in actor_ids:
                fail('ACTOR_NOT_IN_PLAN', field + ': ' + str(actor_id) + ' is not an actor of this cinematic')
        actor_id = performers[0]
        record = records.get(actor_id)
        mode = modes[kind]
        prov = {'source': 'cinematic_plan_interaction', 'actor_id': 'actors[0] (performer)', 'frame': 'planned'}
        if row.get('frame') is None:
            fail('INTERACTION_FRAME_REQUIRED', event_id + ' is UNSCHEDULED; plan it with time_s or frame')
        frame = integer(row['frame'], field + '.frame')
        last = frame
        if row.get('duration_s') is not None:
            duration = model.number(row['duration_s'], field + '.duration_s', 0, model.MAX_DURATION_S)
            if duration > 0:
                begin = model.number(row.get('time_s'), field + '.time_s', 0, model.MAX_DURATION_S)
                after, conversion = model.seconds_to_frame(begin + duration, *rate)
                # A window of duration_s starting at `frame` ends the frame before time_s + duration_s.
                last = max(frame, after - 1)
                conversions.append(dict(conversion, purpose='interaction:' + event_id + ':end',
                                        window_rule='frame_end = frame(time_s + duration_s) - 1'))
                prov['frame_end'] = 'planned duration_s'
        target = None
        spatial = row.get('spatial_target')
        if spatial is not None:
            if not isinstance(spatial, dict):
                fail('INTERACTION_TARGET_INVALID', field + '.spatial_target must be an object')
            if spatial.get('type') == 'ACTOR':
                if spatial.get('actor_id') not in actor_ids:
                    fail('ACTOR_NOT_IN_PLAN', field + '.spatial_target: ' + str(spatial.get('actor_id')))
                target = {'actor_id': spatial['actor_id']}
            elif spatial.get('type') == 'OBJECT':
                name = text_name(spatial.get('object'), field + '.spatial_target.object')
                if name in copies:
                    prov['target'] = 'owned prop copy of ' + name
                    name = copies[name]
                if kind == 'TOUCH_SURFACE':
                    target = {'surfaces': [name]}
                    prov.setdefault('target', 'object measured as a static surface (TOUCH_SURFACE)')
                else:
                    target = {'object': name}
            elif spatial.get('type') == 'POINT':
                target = {'point': model.vector(spatial.get('position'), field + '.spatial_target.position')}
            else:
                fail('INTERACTION_TARGET_INVALID', field + '.spatial_target.type must be ACTOR, OBJECT or POINT')
        contact = row.get('contact_required')
        if not isinstance(contact, bool):
            fail('CINEMATIC_BOOLEAN_REQUIRED', field + '.contact_required must be true or false')
        effector = None
        if row.get('effector') is not None:
            name = text_name(row['effector'], field + '.effector')
            if name in roles:
                bone = registry_bone(record, name)
                prov['effector'] = 'registry role ' + name if bone else 'UNKNOWN (registry role ' + name + ')'
                if bone is None:
                    missing_effector(warnings, event_id, actor_id, 'registry role ' + name + ' is UNKNOWN')
            else:
                bone = name
                prov['effector'] = 'explicit bone (verified in Blender at apply)'
            if bone:
                effector = {'bone': bone, 'end': 'TAIL'}
                prov['effector_end'] = 'INFERRED_DEFAULT (analysis default TAIL)'
        elif mode in ('CONTACT', 'ATTACK'):
            missing_effector(warnings, event_id, actor_id, 'no effector declared')
        head = None
        if mode == 'GAZE':
            head = registry_bone(record, 'head')
            prov['head_bone'] = 'registry role head' if head else 'UNKNOWN'
        reach = None
        if mode == 'ATTACK':
            reach = registry_reach(record)
            prov['reach_m'] = 'registry estimate (interaction_capabilities.reach_m)' if reach is not None else 'UNKNOWN'
        events.append(make_event(event_id, kind, actor_id, frame, frame, last, effector, target, contact, head, None, reach))
        provenance[event_id] = prov
    return events, provenance, conversions, warnings


def listed_target(value, actor_ids, copies, field, prov):
    """Analysis-format target -> {actor_id, bone?, end?} | {object} | {surfaces, generic?} | {point}."""
    if value is None:
        return None
    if isinstance(value, str):
        value = {'actor_id': value} if value in actor_ids else {'object': value}
    if not isinstance(value, dict):
        fail('INTERACTION_TARGET_INVALID', field + ' must be an actor_id, an object name or an object')
    value = model.closed(value, TARGET_KEYS, 'INTERACTION_TARGET_INVALID')
    kinds = [k for k in ('actor_id', 'object', 'surfaces', 'point') if k in value]
    if len(kinds) != 1:
        fail('INTERACTION_TARGET_INVALID', field + ' needs exactly one of actor_id, object, surfaces, point')
    which = kinds[0]
    allowed = {'actor_id': {'bone', 'end'}, 'surfaces': {'generic'}}.get(which, set())
    extra = sorted(set(value) - {which} - allowed)
    if extra:
        fail('INTERACTION_TARGET_INVALID', field + ': ' + ', '.join(extra) + ' do not apply to a ' + which + ' target')
    if which == 'actor_id':
        if value['actor_id'] not in actor_ids:
            fail('ACTOR_NOT_IN_PLAN', field + ': ' + str(value['actor_id']))
        out = {'actor_id': value['actor_id']}
        if value.get('bone') is not None:
            out['bone'] = text_name(value['bone'], field + '.bone')
        if value.get('end') is not None:
            if 'bone' not in out:
                fail('INTERACTION_TARGET_INVALID', field + '.end needs bone')
            out['end'] = model.choice(value['end'], field + '.end', EFFECTOR_ENDS)
        return out
    if which == 'object':
        name = text_name(value['object'], field + '.object')
        if name in copies:
            prov['target'] = 'owned prop copy of ' + name
        return {'object': copies.get(name, name)}
    if which == 'surfaces':
        names = value['surfaces']
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_SURFACES:
            fail('INTERACTION_TARGET_INVALID', field + '.surfaces must list 1-' + str(MAX_SURFACES) + ' objects')
        out = {'surfaces': [copies.get(n, n) for n in (text_name(n, field + '.surfaces') for n in names)]}
        if 'generic' in value:
            out['generic'] = boolean(value['generic'], field + '.generic')
        return out
    return {'point': model.vector(value['point'], field + '.point')}


def listed_interactions(rows, actor_ids, records, copies, modes, contact_kinds):
    """Events already in the analysis format (docs/cinematic-visual-qa.md §5), validated and normalized: string
    shorthands expanded, contact and head bone made explicit."""
    if len(rows) > MAX_INTERACTION_EVENTS:
        fail('CINEMATIC_CAPACITY', 'at most ' + str(MAX_INTERACTION_EVENTS) + ' interaction events')
    events, provenance, warnings = [], {}, []
    for index, row in enumerate(rows):
        field = 'interactions[' + str(index) + ']'
        if not isinstance(row, dict):
            fail('INTERACTION_INVALID', field + ' must be an object')
        row = model.closed(row, EVENT_KEYS, 'INTERACTION_INVALID')
        kind = model.choice(row.get('kind'), field + '.kind', model.INTERACTION_KINDS)
        actor_id = row.get('actor_id')
        if actor_id not in actor_ids:
            fail('ACTOR_NOT_IN_PLAN', field + ': ' + str(actor_id) + ' is not an actor of this cinematic')
        mode = modes[kind]
        prov = {'source': 'events'}
        if row.get('event_id') is not None:
            event_id = model.name(row['event_id'], field + '.event_id')
        else:
            event_id = kind + '_' + str(index)
            prov['event_id'] = 'INFERRED_DEFAULT (analysis naming kind_index)'
        if row.get('frame') is None and row.get('frame_start') is None:
            fail('INTERACTION_FRAME_REQUIRED', field + ' needs frame or frame_start')
        frame = integer(row['frame'] if row.get('frame') is not None else row['frame_start'], field + '.frame')
        first = integer(row['frame_start'], field + '.frame_start') if row.get('frame_start') is not None else frame
        last = integer(row['frame_end'], field + '.frame_end') if row.get('frame_end') is not None else frame
        if not first <= frame <= last:
            fail('INTERACTION_WINDOW_INVALID', field + ' needs frame_start <= frame <= frame_end')
        effector = row.get('effector')
        if effector is not None:
            if isinstance(effector, str):
                effector = {'bone': effector}
            if not isinstance(effector, dict):
                fail('INTERACTION_EFFECTOR_INVALID', field + '.effector must be a bone name or {bone, end}')
            effector = model.closed(effector, {'bone', 'end'}, 'INTERACTION_EFFECTOR_INVALID')
            if 'end' not in effector:
                prov['effector_end'] = 'INFERRED_DEFAULT (analysis default TAIL)'
            effector = {'bone': text_name(effector.get('bone'), field + '.effector.bone'),
                        'end': model.choice(effector.get('end', 'TAIL'), field + '.effector.end', EFFECTOR_ENDS)}
            prov['effector'] = 'explicit bone (verified in Blender at apply)'
        elif mode in ('CONTACT', 'ATTACK'):
            missing_effector(warnings, event_id, actor_id, 'no effector declared')
        target = listed_target(row.get('target'), actor_ids, copies, field + '.target', prov)
        if 'contact' in row:
            contact = boolean(row['contact'], field + '.contact')
        else:
            contact = kind in contact_kinds
            prov['contact'] = 'INFERRED_DEFAULT (analysis rule by kind)'
        head = None
        if row.get('head_bone') is not None:
            head = text_name(row['head_bone'], field + '.head_bone')
            prov['head_bone'] = 'explicit bone (verified in Blender at apply)'
        elif mode == 'GAZE':
            head = registry_bone(records.get(actor_id), 'head')
            prov['head_bone'] = 'registry role head' if head else 'UNKNOWN'
        axis = None
        if row.get('forward_axis') is not None:
            axis = model.vector(row['forward_axis'], field + '.forward_axis', 3, -1.0, 1.0)
            if math.sqrt(sum(v * v for v in axis)) < 1e-9:
                fail('INTERACTION_FORWARD_AXIS_INVALID', field + '.forward_axis must be a non-zero bone-local axis')
        reach = model.number(row['reach_m'], field + '.reach_m', 1e-6, 100.0) if row.get('reach_m') is not None else None
        events.append(make_event(event_id, kind, actor_id, frame, first, last, effector, target, contact, head, axis, reach))
        provenance[event_id] = prov
    return events, provenance, [], warnings


def interaction_checks(events, objects):
    """What the payload verifies before writing: every referenced object is in the candidate and every bone is a bone
    of its armature. One row per object; each bone keeps the first event that needs it."""
    rows = {}
    order = []
    for event in events:
        wanted = [(objects[event['actor_id']], [(event.get('effector') or {}).get('bone'), event.get('head_bone')])]
        target = event.get('target') or {}
        if target.get('actor_id'):
            wanted.append((objects[target['actor_id']], [target.get('bone')]))
        if target.get('object'):
            wanted.append((target['object'], []))
        for name in target.get('surfaces') or []:
            wanted.append((name, []))
        for name, bones in wanted:
            if name not in rows:
                rows[name] = {'event_id': event['event_id'], 'object': name, 'bones': []}
                order.append(name)
            for bone in bones:
                if bone and bone not in [b[0] for b in rows[name]['bones']]:
                    rows[name]['bones'].append([bone, event['event_id']])
    return [rows[name] for name in order]


def convert_interactions(value, plan, state, records, timeline):
    """options.interactions -> {events (state['interactions'] rows), markers (EVENT_<id>, no camera), provenance,
    conversions, warnings, checks (run by the payload), measurable}. Pure apart from reading analysis' own rules:
    every row is passed through analysis._events, and the rows it would ignore are reported with its reason."""
    from . import actors, analysis  # lazy: analysis composes the M3/M8 runtimes; actors owns the registry roles
    actor_ids = [a['actor_id'] for a in plan.get('actors') or []]
    copies = prop_copies(state)
    if isinstance(value, dict):
        events, provenance, conversions, warnings = planned_interactions(
            value, actor_ids, records, timeline, copies, actors.BONE_ROLES, analysis.INTERACTION_MODE)
        origin = planning.INTERACTIONS_SCHEMA
    elif isinstance(value, list):
        events, provenance, conversions, warnings = listed_interactions(
            value, actor_ids, records, copies, analysis.INTERACTION_MODE, analysis.CONTACT_KINDS)
        origin = 'events'
    else:
        fail('INTERACTIONS_INVALID', 'options.interactions must be a cinematic_plan_interaction result or a list of events')
    start, end = int(timeline['frame_start']), int(timeline['frame_end'])
    seen = set()
    for event in events:
        if event['event_id'] in seen:
            fail('INTERACTION_DUPLICATE', event['event_id'] + ' appears twice')
        seen.add(event['event_id'])
        if not (start <= event['frame_start'] and event['frame_end'] <= end):
            fail('INTERACTION_OUTSIDE_TIMELINE', event['event_id'] + ' ' + str([event['frame_start'], event['frame_end']])
                 + ' outside ' + str([start, end]))
    measured, ignored = analysis._events(deepcopy(events), actor_ids)
    for row in ignored:
        warnings.append({'code': 'INTERACTION_NOT_MEASURABLE', 'event_id': row.get('event_id'), 'reason': row['reason'],
                         'detail': 'the QA analyses list this event in events_ignored with this reason'})
    objects = {a['actor_id']: a['object'] for a in plan.get('actors') or []}
    markers = [{'name': 'EVENT_' + e['event_id'], 'kind': 'EVENT', 'id': e['event_id'], 'frame': e['frame'],
                'source': INTERACTION_SOURCE} for e in events]
    return {'events': events, 'markers': markers, 'provenance': provenance, 'conversions': conversions,
            'warnings': warnings, 'checks': interaction_checks(events, objects), 'origin': origin,
            'measurable': [e['event_id'] for e in measured]}


def interaction_view(converted):
    return {'origin': converted['origin'], 'events': converted['events'], 'provenance': converted['provenance'],
            'markers': [[m['name'], m['frame']] for m in converted['markers']], 'warnings': converted['warnings'],
            'measurable': converted['measurable'], 'checks_at_apply': len(converted['checks'])}


# ---------------------------------------------------------------------------------------------------------------
# finalize: CINEMATIC_CANDIDATE -> ASSEMBLED (ledger only; Blender is read, never written)
# ---------------------------------------------------------------------------------------------------------------

FINALIZE_GATES = ('state is CINEMATIC_CANDIDATE', 'store.fresh: source and candidate unchanged since the last step',
                  'timeline_report passed (fps, range, contiguous shots, cut markers, coverage, narrative markers)',
                  'cuts recorded, each to a camera created by this cinematic', 'every shot assigned a camera by a cut',
                  'actors.integrity_report passed (registry, rigs, meshes, Actions, source and candidate)')
FRESH_BLOCKING = frozenset({'CINEMATIC_OPERATION_UNCERTAIN', 'CINEMATIC_BACKEND_CHANGED', 'CINEMATIC_STALE_ASSET'})
PROMOTED_WARNINGS = frozenset({'CUT_MARKER_UNRECORDED'})  # a CUT_ marker the ledger does not know switches cameras


def gate(name, code, detail):
    return {'gate': name, 'code': code, 'detail': detail}


def split_error(exc):
    code, _, detail = str(exc).partition(': ')
    return code, detail


def ledger_gates(plan, state):
    """Pure gates on the recorded montage: cuts exist and name cameras this cinematic created, every shot has a cut.
    Range coverage by the cuts is the timeline report's (CUTS_DO_NOT_COVER_TIMELINE)."""
    errors = []
    warnings = []
    cuts = [c for c in state.get('cuts') or [] if isinstance(c, dict)]
    cameras = state.get('cameras') or {}
    shots = shot_rows(plan, state)
    if not cuts:
        errors.append(gate('cuts', 'FINALIZE_CUTS_MISSING', 'no cuts recorded; run cinematic_create_cuts'))
    for row in cuts:
        camera = cameras.get(row.get('camera_id'))
        if camera is None or camera.get('object') != row.get('camera_object'):
            errors.append(gate('cuts', 'FINALIZE_CUT_CAMERA_UNKNOWN', str(row.get('marker')) + ' -> ' + str(row.get('camera_id'))
                               + ' is not a camera recorded by cinematic_create_camera'))
    if not shots:
        errors.append(gate('shots', 'FINALIZE_SHOTS_MISSING', 'the cinematic has no shots'))
    assigned = {row.get('shot_id') for row in cuts}
    for shot in shots:
        if shot.get('shot_id') not in assigned:
            errors.append(gate('shots', 'FINALIZE_SHOT_WITHOUT_CUT', str(shot.get('shot_id')) + ' has no cut assigning it a camera'))
    planned = {shot.get('shot_id') for shot in shots}
    for row in cuts:
        if row.get('shot_id') not in planned:
            warnings.append(gate('shots', 'FINALIZE_CUT_SHOT_UNPLANNED', str(row.get('shot_id')) + ' is not a shot of the cinematic'))
    return errors, warnings


def assembly_errors(plan, state, report, integrity, fresh_error=None):
    """Every finalize gate in one list (pure). report: timeline_report(...); integrity: actors.integrity_report(...);
    either is None when a blocking freshness failure prevented reading Blender."""
    errors, warnings = ledger_gates(plan, state)
    if fresh_error is not None:
        errors.insert(0, gate('fresh', fresh_error[0], fresh_error[1]))
    if report is not None:
        for row in report.get('errors') or []:
            errors.append(gate('timeline_report', row['code'], row.get('detail', '')))
        if report.get('error_count', 0) > len(report.get('errors') or []):
            errors.append(gate('timeline_report', 'TIMELINE_REPORT_TRUNCATED', str(report['error_count']) + ' errors in total'))
        if not report.get('passed') and not report.get('errors'):
            errors.append(gate('timeline_report', 'TIMELINE_REPORT_FAILED', 'passed is not true'))
        for row in report.get('warnings') or []:
            if row['code'] in PROMOTED_WARNINGS:
                errors.append(gate('timeline_report', row['code'], row.get('detail', '') + ' (finalize refuses cuts the ledger does not know)'))
            else:
                warnings.append(gate('timeline_report', row['code'], row.get('detail', '')))
    if integrity is not None:
        for row in integrity.get('changed') or []:
            where = str(row.get('item')) + ' ' + str(row.get('status'))
            if row.get('actor_id'):
                where += ' (actor ' + str(row['actor_id']) + ')'
            errors.append(gate('integrity', row.get('code') or 'INTEGRITY_CHANGED', where))
        if not integrity.get('passed') and not integrity.get('changed'):
            errors.append(gate('integrity', 'INTEGRITY_FAILED', 'passed is not true'))
    return errors, warnings


def not_assemblable(errors, limit=12):
    text = '; '.join('[' + e['gate'] + '] ' + e['code'] + ' ' + str(e['detail'])[:160] for e in errors[:limit])
    more = ' (+' + str(len(errors) - limit) + ' more)' if len(errors) > limit else ''
    return str(len(errors)) + ' gate failure(s): ' + text + more


def assembled_summary(observed, state, report, integrity, warnings):
    shots = report.get('shots') or {}
    candidate = integrity.get('candidate') or {}
    return {'schema': 'production.cinematic_assembled.v1',
            'candidate_fingerprint': observed['candidate']['fingerprint'],
            'source_fingerprint': observed['source']['fingerprint'], 'revision': state.get('revision', 0),
            'gates': list(FINALIZE_GATES),
            'timeline_report': {'passed': report['passed'], 'error_count': report['error_count'],
                                'warning_count': report['warning_count'],
                                'warnings': [w['code'] for w in report.get('warnings') or []][:20],
                                'scene': report['scene'], 'markers': report['markers'],
                                'shots': {'count': shots.get('shots'), 'contiguous': shots.get('contiguous')},
                                'cuts': report['cuts']['rows'], 'threshold': report['threshold']},
            'integrity': {'passed': integrity['passed'], 'checked_total': integrity.get('checked_total'),
                          'counts': integrity.get('counts'), 'source': (integrity.get('source') or {}).get('status'),
                          'candidate': candidate.get('status'),
                          'cameras_lights': (integrity.get('cameras_lights') or {}).get('status'),
                          'actors': [{k: r.get(k) for k in ('actor_id', 'status', 'checked', 'changed')}
                                     for r in integrity.get('actor_reports') or []]},
            'shots': [[c['shot_id'], c['camera_id'], c['frame'], c['frame_end']] for c in state.get('cuts') or []][:64],
            'interactions': len(state.get('interactions') or []),
            'warnings': [w['code'] for w in warnings][:30], 'blender_changed': False}


def finalize(ctx, req, opts):
    """CINEMATIC_CANDIDATE -> ASSEMBLED. Reads Blender (freshness, timeline and integrity payloads, none of which
    writes or changes the frame) and records the state; any failed gate refuses with the full list."""
    extra = sorted(set(opts) - {'finalize', 'min_shot_frames'})
    if extra:
        fail('TIMELINE_FINALIZE_EXCLUSIVE', 'finalize changes nothing; call it without ' + ', '.join(extra))
    min_frames = integer(opts.get('min_shot_frames', MIN_SHOT_FRAMES), 'min_shot_frames', 1, 100000)
    path, plan, state = load_candidate(ctx, req)
    if state.get('state') != 'CINEMATIC_CANDIDATE':
        fail('CINEMATIC_STATE_INVALID', str(state.get('state')) + ': finalize needs CINEMATIC_CANDIDATE (any assembly '
                                                                   'change returns an ASSEMBLED or REVIEWED cinematic to it)')
    if not req.apply:
        start, end = timeline_range(plan, state)
        ledger_errors, ledger_warnings = ledger_gates(plan, state)
        return proposal(path, plan, state, 'finalize', {
            'gates': list(FINALIZE_GATES), 'ledger_errors': ledger_errors, 'ledger_warnings': ledger_warnings,
            'coverage_from_ledger': coverage_from(plan, state, None, None, (start, end), min_frames),
            'measurements': 'freshness, the timeline report and the integrity report are read from Blender at apply '
                            '(read-only payloads); apply records ASSEMBLED without changing Blender'})
    from . import actors  # lazy: actors registers its own handlers and owns the integrity payload
    fresh_error = None
    observed = None
    try:
        observed = store.fresh(ctx.bridge, ctx.storage, plan, state)
    except ValueError as exc:
        fresh_error = split_error(exc)
    report = integrity = None
    if fresh_error is None or fresh_error[0] not in FRESH_BLOCKING:
        # Read-only; still measured after a stale fingerprint so the refusal lists every failing gate at once.
        report = timeline_report(ctx, plan, state, min_frames)
        integrity = actors.integrity_report(ctx, plan, state)
    errors, warnings = assembly_errors(plan, state, report, integrity, fresh_error)
    if errors:
        fail('CINEMATIC_NOT_ASSEMBLABLE', not_assemblable(errors))
    assembled = assembled_summary(observed, state, report, integrity, warnings)
    new = deepcopy(state)
    new['state'] = 'ASSEMBLED'
    new['assembled'] = assembled
    new.setdefault('history', []).append({'revision': new.get('revision', 0), 'step': 'finalize', 'touched': 0, 'owned': {}})
    store.record(path, new)
    return {**store.public(path, plan, new), 'status': 'warning' if warnings else 'success', 'apply': True,
            'step': 'finalize', 'assembled': assembled, 'warnings': warnings[:30], 'blender_changed': False,
            'next': 'cinematic_render_preview (full temporal preview), then cinematic_review'}


@handler('cinematic_manage_timeline')
def manage_timeline(ctx, req):
    opts = model.closed(req.options, TIMELINE_KEYS)
    if 'finalize' in opts:
        if boolean(opts['finalize'], 'finalize'):
            return finalize(ctx, req, opts)
        opts.pop('finalize')  # finalize=false is an ordinary timeline call
    path, plan, state = load_candidate(ctx, req)
    timeline = dict(state.get('timeline') or plan['timeline'])
    current_start, current_end = int(timeline['frame_start']), int(timeline['frame_end'])
    min_frames = integer(opts.get('min_shot_frames', MIN_SHOT_FRAMES), 'min_shot_frames', 1, 100000)
    new_range = None
    if 'frame_start' in opts or 'frame_end' in opts:
        start = integer(opts.get('frame_start', current_start), 'frame_start', -100000, 1_048_574)
        end = integer(opts.get('frame_end', current_end), 'frame_end', -100000, 1_048_574)
        if end < start:
            fail('TIMELINE_RANGE_INVALID', 'frame_end must be >= frame_start')
        if (end - start + 1) / model.fps_value(timeline['fps'], timeline.get('fps_base', 1.0)) > model.MAX_DURATION_S:
            fail('CINEMATIC_CAPACITY', 'timeline longer than ' + str(model.MAX_DURATION_S) + ' s')
        cuts = state.get('cuts') or []
        outside = [c['marker'] for c in cuts if not start <= c['frame'] <= end]
        if outside:
            fail('TIMELINE_CUTS_OUTSIDE', ', '.join(outside[:5]) + ' fall outside the new range; recreate or clear the cuts first')
        if cuts and cuts[0]['frame'] != start:
            fail('TIMELINE_CUT_START_MISMATCH', 'first cut at ' + str(cuts[0]['frame']) + ', new frame_start ' + str(start)
                 + '; recreate or clear the cuts first')
        new_range = [start, end]
    start, end = new_range or [current_start, current_end]
    narrative = list(timeline.get('narrative') or [])
    add = []
    remove = []
    conversions = []
    interactions = None
    if 'interactions' in opts:
        interactions = convert_interactions(opts['interactions'], plan, state, actor_records(ctx, plan),
                                            dict(timeline, frame_start=start, frame_end=end))
        # The interaction markers belong to state['interactions']: sending interactions replaces all of them.
        remove += [row['name'] for row in narrative if row.get('source') == INTERACTION_SOURCE]
        narrative = [row for row in narrative if row.get('source') != INTERACTION_SOURCE]
        add += interactions['markers']
        conversions += interactions['conversions']
    else:
        outside = [e['event_id'] for e in state.get('interactions') or []
                   if not (start <= e['frame_start'] and e['frame_end'] <= end)]
        if outside:
            fail('TIMELINE_INTERACTION_OUTSIDE', ', '.join(outside[:5]) + ' fall outside the new range; send '
                 'options.interactions again')
    if 'markers' in opts:
        rows, marker_conversions = normalize_markers(opts['markers'], timeline, start, end)
        if boolean(opts.get('replace_markers', True), 'replace_markers'):
            # replace_markers only replaces the markers options.markers created; interaction markers stay.
            remove += [row['name'] for row in narrative if row.get('source') != INTERACTION_SOURCE]
            narrative = [row for row in narrative if row.get('source') == INTERACTION_SOURCE]
        add += rows
        conversions += marker_conversions
    names = [row['name'] for row in add]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        fail('TIMELINE_MARKER_DUPLICATE', ', '.join(duplicates[:5]) + ' (options.markers and options.interactions share a name)')
    clash = sorted(set(names) & {row['name'] for row in narrative})
    if clash:
        fail('CINEMATIC_NAME_COLLISION', 'markers ' + ', '.join(clash[:5]) + ' exist; pass replace_markers=true or send '
                                         'the interactions again')
    remove = sorted(set(remove))
    stale = [row['name'] for row in narrative if not start <= row['frame'] <= end]
    if stale:
        fail('TIMELINE_MARKER_OUTSIDE', ', '.join(stale[:5]) + ' fall outside the new range; replace the markers')
    if 'conversions' in opts:
        conversions += normalize_conversions(opts['conversions'], timeline, start)
    change = {'frame_range': new_range, 'add_markers': add, 'remove_markers': remove, 'conversions': conversions,
              'fps': timeline['fps'], 'fps_base': timeline.get('fps_base', 1.0),
              'fps_policy': 'the candidate keeps the source fps; Actions and the source scene are never retimed'}
    if not req.apply:
        coverage = coverage_from(plan, dict(state, timeline=dict(timeline, frame_start=start, frame_end=end)),
                                 None, None, (start, end), min_frames)
        body = {'timeline': change, 'coverage_from_ledger': coverage}
        if interactions is not None:
            body['interactions'] = dict(interaction_view(interactions),
                                        measurements='bones and objects are verified in the candidate at apply')
        return proposal(path, plan, state, 'manage_timeline', body)
    if new_range or add or remove:
        new, result = run(ctx, path, plan, state, 'manage_timeline', TIMELINE_BODY,
                          {'frame_start': new_range[0] if new_range else None, 'frame_end': new_range[1] if new_range else None,
                           'add': [{'name': r['name'], 'frame': r['frame']} for r in add], 'remove': remove,
                           'fps': timeline['fps'], 'fps_base': timeline.get('fps_base', 1.0),
                           'checks': interactions['checks'] if interactions else []})
        observed = result['timeline']
    else:
        new = deepcopy(state)
        observed = None
    timeline = dict(new.get('timeline') or timeline)
    timeline['frame_start'], timeline['frame_end'] = start, end
    timeline['frames'] = end - start + 1
    timeline['narrative'] = narrative + add
    timeline['conversions'] = list(timeline.get('conversions') or []) + conversions
    if observed is not None:
        timeline['markers'] = [m[:3] for m in observed['markers']]
    new['timeline'] = timeline
    if interactions is not None:
        new['interactions'] = interactions['events']
        new['interaction_conversion'] = {'origin': interactions['origin'], 'events': len(interactions['events']),
                                         'provenance': interactions['provenance'], 'warnings': interactions['warnings'],
                                         'measurable': interactions['measurable']}
    store.record(path, new)
    report = timeline_report(ctx, plan, new, min_frames)
    warnings = report['warnings'] + (interactions['warnings'] if interactions else [])
    response = {**store.public(path, plan, new), 'status': status_of(report['errors'], warnings), 'apply': True,
                'step': 'manage_timeline',
                'timeline': {k: timeline.get(k) for k in ('frame_start', 'frame_end', 'frames', 'fps', 'fps_base')},
                'markers_added': [r['name'] for r in add], 'markers_removed': remove, 'conversions': conversions,
                'report': report}
    if interactions is not None:
        response['interactions'] = interaction_view(interactions)
    return response


def timeline_report(ctx, plan, state, min_shot_frames=MIN_SHOT_FRAMES):
    """Coverage of the candidate timeline for the coordinator's finalize step. Read-only payload: no frame_set, no
    Action/slot assignment, no pose write. Returns {passed, errors, warnings, ...}; never mutates the ledger."""
    if not state.get('assembly'):
        fail('CINEMATIC_CANDIDATE_REQUIRED', 'no candidate scene')
    cameras = sorted({row['object'] for row in (state.get('cameras') or {}).values()})
    params = store.assembly_params(plan, state, {'cameras': cameras})
    observed = ctx.bridge.run(TIMELINE_READ_BODY, params)
    info = observed['timeline']
    timeline = state.get('timeline') or plan['timeline']
    errors = []
    if info['fps'] != plan['timeline']['fps'] or abs(info['fps_base'] - plan['timeline'].get('fps_base', 1.0)) > 1e-9:
        errors.append({'code': 'CINEMATIC_FPS_MISMATCH', 'detail': 'candidate ' + str(info['fps']) + '/' + str(info['fps_base'])
                       + ' vs plan ' + str(plan['timeline']['fps'])})
    if [info['frame_start'], info['frame_end']] != [int(timeline['frame_start']), int(timeline['frame_end'])]:
        errors.append({'code': 'TIMELINE_RANGE_CHANGED', 'detail': 'candidate ' + str([info['frame_start'], info['frame_end']])
                       + ' vs ledger ' + str([timeline['frame_start'], timeline['frame_end']])})
    coverage = coverage_from(plan, state, info['markers'], observed['cameras'], (info['frame_start'], info['frame_end']),
                             min_shot_frames)
    errors += coverage['errors']
    narrative = [m for m in info['markers'] if m[0].startswith('BEAT_') or m[0].startswith('EVENT_')]
    with_camera = [m[0] for m in narrative if m[2] is not None]
    if with_camera:
        errors.append({'code': 'TIMELINE_NARRATIVE_MARKER_HAS_CAMERA', 'detail': ', '.join(with_camera[:5])})
    # Any other marker with a camera switches scene.camera at render (measured) outside the recorded cuts.
    foreign = [m[0] for m in info['markers'] if m[2] is not None and not m[0].startswith(('CUT_', 'BEAT_', 'EVENT_'))]
    if foreign:
        errors.append({'code': 'TIMELINE_FOREIGN_CAMERA_MARKER', 'detail': ', '.join(foreign[:5])
                       + ' switch the camera outside the recorded cuts'})
    observed_narrative = {m[0]: m[1] for m in narrative}
    for row in timeline.get('narrative') or []:
        frame = observed_narrative.get(row['name'])
        if frame is None:
            errors.append({'code': 'TIMELINE_MARKER_MISSING', 'detail': row['name'] + ' (recorded at ' + str(row['frame']) + ')'})
        elif frame != row['frame']:
            errors.append({'code': 'TIMELINE_MARKER_CHANGED', 'detail': row['name'] + ' at ' + str(frame) + ', recorded '
                           + str(row['frame'])})
    return {'schema': 'production.cinematic_timeline_report.v1', 'passed': not errors, 'errors': errors[:60],
            'warnings': coverage['warnings'], 'error_count': len(errors), 'warning_count': coverage['warning_count'],
            'scene': {'frame_start': info['frame_start'], 'frame_end': info['frame_end'], 'fps': info['fps'],
                      'fps_base': info['fps_base'], 'camera': info['camera']},
            'markers': {'cuts': len([m for m in info['markers'] if m[0].startswith('CUT_')]), 'narrative': len(narrative),
                        'other': len([m for m in info['markers'] if not m[0].startswith(('CUT_', 'BEAT_', 'EVENT_'))])},
            'shots': coverage['shots'], 'cuts': coverage['cuts'], 'actors': coverage['actors'],
            'interactions': len(state.get('interactions') or []),
            'conversions': len(timeline.get('conversions') or []), 'threshold': {'min_shot_frames': min_shot_frames,
                                                                                'threshold_source': 'PROPOSAL'}}
