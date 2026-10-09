"""Facial profile (host side): inspection + measured semantic mapping + measured geometric calibration.

The profile is what every plan is built on. It is derived, never supplied by the caller: control probes give
the semantic mapping (``semantics.analyze``) and explicit control poses measured in Blender give the
calibration (eyelid closure, jaw clearance, lip-gap model). Thresholds below are starting proposals stated
in millimetres or ratios; each finding carries the measured number next to the threshold.
"""
from __future__ import annotations

import time

from . import semantics
from .model import viseme_support
from .runtime import INSPECT, PROBE
from .scene_runtime import CAPABILITY

QA_THRESHOLDS = {
    'blink_closure_min': 0.90,          # fraction of the rest lid gap closed at a full blink peak
    'eye_penetration_mm': 0.20,         # increase of lid depth inside the eyeball sphere
    'lip_crossing_mm': -1.0,            # upper-lip margin below the lower-lip margin by more than 1 mm
    'mbp_gap_mm': 1.0,                  # remaining lip gap at a bilabial closure centre
    'new_intersections': 0,             # facial polygons newly intersecting another mesh
    'collapsed_faces': 0, 'flipped_faces': 0,
}
THRESHOLD_SOURCE = 'starting proposals for synthetic QA (not measured on a production character)'
BLOCKING_WARNINGS = ('ABSOLUTE_SHAPE_KEYS', 'BASIS_INVALID')
GAP_OUTPUTS = ('lip_press_L', 'lip_press_R', 'lip_roll_lower', 'lip_roll_upper', 'lip_pucker', 'lip_funnel',
               'lip_stretch_L', 'lip_stretch_R', 'lip_corner_up_L', 'lip_corner_up_R', 'lip_corner_down_L', 'lip_corner_down_R',
               'upper_lip_raise_L', 'upper_lip_raise_R', 'lower_lip_depress_L', 'lower_lip_depress_R', 'jaw_forward')


def compact_mapping(mapping):
    keep = ('semantic', 'usable', 'confidence', 'method', 'control', 'secondary', 'positive', 'negative',
            'degrees_per_unit', 'channel_limits', 'aim', 'alternatives')
    out = {}
    for name, entry in mapping.items():
        row = {k: entry[k] for k in keep if k in entry}
        row['evidence'] = list(entry.get('evidence', []))[:5]
        out[name] = row
    return out


def control_pose(entry, scale=1.0):
    keys, bones = [], []
    controls = [entry['control']] + list(entry.get('secondary', []))
    for c in controls:
        if c['type'] == 'shape_key':
            keys.append([c['mesh'], c['key'], round(c.get('full', 1.0) * scale, 6)])
        elif c['type'] == 'bone':
            bones.append([c['bone'], c['path'], c['index'], round(c['full'] * scale, 6)])
    return keys, bones


def _pose(name, entries):
    keys, bones = [], []
    for entry, scale in entries:
        k, b = control_pose(entry, scale)
        keys += k
        bones += b
    return {'name': name, 'keys': keys, 'bones': bones}


def calibration_poses(mapping):
    usable = {k: v for k, v in mapping.items() if v.get('usable') and v.get('control', {}).get('type') in ('shape_key', 'bone')}
    poses = []
    for side in ('L', 'R'):
        if 'lid_close_' + side in usable:
            poses.append(_pose('lid_close_' + side, [(usable['lid_close_' + side], 1.0)]))
    if 'jaw_open' in usable:
        poses.append(_pose('jaw_open', [(usable['jaw_open'], 1.0)]))
    # Every other usable mouth output changes the lip gap too (press, rolls, stretch...): its gain is measured so
    # that lips_close can seal what the whole mouth pose leaves open, not only the jaw.
    for name in GAP_OUTPUTS:
        if name in usable:
            poses.append(_pose('gap:' + name, [(usable[name], 1.0)]))
    if 'lips_close' in usable:
        poses.append(_pose('lips_close', [(usable['lips_close'], 1.0)]))
        if 'jaw_open' in usable:
            poses.append(_pose('jaw_lips', [(usable['jaw_open'], 1.0), (usable['lips_close'], 1.0)]))
            poses.append(_pose('jaw_lips_half', [(usable['jaw_open'], 0.5), (usable['lips_close'], 0.5)]))
    return poses


def interpret_calibration(measured, mapping):
    """Turn measured poses into lid/jaw/lip facts and findings (numbers next to thresholds)."""
    t = QA_THRESHOLDS
    rows = {p['name']: p['metrics'] for p in measured.get('poses', [])}
    out = {'lids': {}, 'jaw': None, 'lips': None, 'findings': [], 'thresholds': t, 'threshold_source': THRESHOLD_SOURCE,
           'landmarks': measured.get('landmarks', {}), 'iod_m': measured.get('iod_m')}
    for side in ('L', 'R'):
        m = rows.get('lid_close_' + side)
        if not m:
            continue
        lid = m['landmarks'].get('eye_' + side, {})
        eye = m['eyes'].get(side, {})
        row = {'closure_at_full': lid.get('closure'), 'rest_gap_mm': lid.get('baseline_gap_mean_mm'),
               'gap_at_full_mm': lid.get('gap_mean_mm'), 'penetration_increase_mm': eye.get('penetration_increase_mm'),
               'new_intersections_with_eye': m['overlap_new'].get('face/eye_' + side, 0)}
        out['lids'][side] = row
        if lid.get('closure') is not None and lid['closure'] < t['blink_closure_min']:
            out['findings'].append({'type': 'BLINK_INCOMPLETE', 'side': side, 'closure': lid['closure'],
                                    'threshold': t['blink_closure_min'], 'severity': 'error',
                                    'note': 'the mapped lid control closes only part of the eye at full value'})
        if (eye.get('penetration_increase_mm') or 0) > t['eye_penetration_mm']:
            out['findings'].append({'type': 'EYE_PENETRATION', 'side': side, 'penetration_mm': eye.get('penetration_increase_mm'),
                                    'new_intersections': row['new_intersections_with_eye'], 'threshold_mm': t['eye_penetration_mm'],
                                    'severity': 'error'})
        if lid.get('closure') is None:
            out['findings'].append({'type': 'LID_LANDMARK_NOT_FOUND', 'side': side, 'severity': 'warning',
                                    'note': 'no eye-opening boundary loop: lid closure cannot be measured geometrically'})
    jaw = rows.get('jaw_open')
    if jaw:
        mouth = jaw['landmarks'].get('mouth', {})
        out['jaw'] = {'mouth_gap_at_full_mm': mouth.get('gap_mean_mm'), 'rest_gap_mm': mouth.get('baseline_gap_mean_mm'),
                      'new_intersections': {k: v for k, v in jaw['overlap_new'].items() if v},
                      'collapsed_faces': jaw['collapsed'], 'flipped_faces': jaw['flipped']}
        if any(jaw['overlap_new'].values()) or jaw['collapsed'] or jaw['flipped']:
            out['findings'].append({'type': 'JAW_COLLISION', 'new_intersections': out['jaw']['new_intersections'],
                                    'collapsed_faces': jaw['collapsed'], 'flipped_faces': jaw['flipped'], 'severity': 'error',
                                    'note': 'full jaw opening intersects another mesh or degenerates faces; extremes are limited'})
    close, both, half = rows.get('lips_close'), rows.get('jaw_lips'), rows.get('jaw_lips_half')
    if close and both and jaw and 'mouth' in close['landmarks']:
        g0 = close['landmarks']['mouth']['baseline_gap_mean_mm']
        g_c = close['landmarks']['mouth']['gap_mean_mm']
        g_j = jaw['landmarks']['mouth']['gap_mean_mm']
        g_jc = both['landmarks']['mouth']['gap_mean_mm']
        predicted = g0 + (g_j - g0) + (g_c - g0)
        # Bilinear model gap(j, c) = g0 + a j - (k - d j) c fitted exactly to the four measured corners; the measured
        # half pose (j = c = 0.5) validates it (a bone jaw with a shape-key closure is not linear: measured 4 mm error).
        interaction = g_jc - g_j - g_c + g0
        lips = {'rest_gap_mm': g0, 'jaw_full_gap_mm': g_j, 'close_full_gap_mm': g_c, 'jaw_and_close_gap_mm': g_jc,
                'linear_model_error_mm': round(abs(predicted - g_jc), 3), 'close_gain_mm': round(g0 - g_c, 3),
                'jaw_gain_mm': round(g_j - g0, 3), 'interaction_mm': round(interaction, 3),
                'mode': 'JAW_RELATIVE' if g_c < QA_THRESHOLDS['lip_crossing_mm'] else 'ABSOLUTE',
                'crossing_at_full_jaw_and_close_mm': round(min(0.0, g_jc), 3)}
        if half and 'mouth' in half['landmarks']:
            predicted_half = g0 + 0.5 * (g_j - g0) + 0.5 * (g_c - g0) + 0.25 * interaction
            lips['half_model_error_mm'] = round(abs(predicted_half - half['landmarks']['mouth']['gap_mean_mm']), 3)
        lips['other_gains_mm'] = {}
        for name in GAP_OUTPUTS:
            row = rows.get('gap:' + name)
            if row and 'mouth' in row['landmarks']:
                lips['other_gains_mm'][name] = round(row['landmarks']['mouth']['gap_mean_mm'] - g0, 3)
        lips['calibrated'] = (lips['close_gain_mm'] > 0.1 and lips['close_gain_mm'] - interaction > 0.1 and
                              lips.get('half_model_error_mm', 0.0) < 1.5)
        if lips['close_gain_mm'] < g0 - 0.05:
            out['findings'].append({'type': 'LIP_SEAL_INSUFFICIENT', 'close_gain_mm': lips['close_gain_mm'], 'rest_gap_mm': g0,
                                    'severity': 'warning',
                                    'note': 'the lip-closure control at its full range cannot close the rest gap; bilabials stay open'})
        out['lips'] = lips
        if lips['mode'] == 'JAW_RELATIVE' and g_jc < QA_THRESHOLDS['lip_crossing_mm']:
            out['findings'].append({'type': 'LIP_CROSSING_AT_FULL', 'gap_mm': g_jc, 'threshold_mm': QA_THRESHOLDS['lip_crossing_mm'],
                                    'severity': 'warning',
                                    'note': 'the close control overshoots the jaw: uncalibrated use crosses the lips; '
                                            'candidates use the measured gap model instead'})
        if not lips['calibrated']:
            out['findings'].append({'type': 'LIP_CLOSURE_UNCALIBRATED', 'severity': 'warning',
                                    'model_error_mm': lips['linear_model_error_mm'],
                                    'note': 'lip gap does not follow the measured bilinear jaw/close model; closure keys use the control full value'})
    return out


def build(bridge, object_name, options=None):
    """Inspect, probe, map and calibrate. Returns the profile and timings (measured)."""
    options = dict(options or {})
    timings = {}
    # The probe poses controls transiently (restored before it returns); it runs first so a tool built on a
    # profile is classified by what its first payload really does (tests/unit/test_m31_hints.py).
    t0 = time.perf_counter()
    probe = bridge.run(PROBE, {'object_name': object_name, 'options': options})
    timings['probe_s'] = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    inspection = bridge.run(INSPECT, {'object_name': object_name, 'options': options})
    timings['inspect_s'] = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    analysis = semantics.analyze(probe, inspection)
    timings['mapping_s'] = round(time.perf_counter() - t0, 3)
    mapping = analysis['mapping']
    calibration = {'lids': {}, 'jaw': None, 'lips': None, 'findings': [], 'thresholds': QA_THRESHOLDS}
    blocked = [w for w in inspection.get('warnings', []) if w.startswith(BLOCKING_WARNINGS)]
    if analysis['classification']['type'] not in ('UNSUPPORTED', 'AMBIGUOUS') and inspection.get('eyes'):
        poses = calibration_poses(mapping)
        if poses:
            t0 = time.perf_counter()
            measured = bridge.run(CAPABILITY, {'object_name': object_name, 'options': options, 'poses': poses})
            timings['calibration_s'] = round(time.perf_counter() - t0, 3)
            calibration = interpret_calibration(measured, mapping)
            calibration['restored'] = measured.get('restored')
            if any(f['type'] == 'LIP_SEAL_INSUFFICIENT' for f in calibration['findings']):
                analysis['compatibility']['lip_sync'] = {'status': 'LIMITED', 'reason': 'measured: lip closure cannot seal the rest gap '
                                                         '(bilabials stay open); timing input still required'}
    driven_keys = sorted(p['mesh'] + ':' + p['key'] for p in probe.get('key_probes', []) if p.get('driven'))
    driven_bones = sorted({p['bone'] + '|' + p['path'] for p in probe.get('bone_probes', []) if p.get('driver_on_channel')})
    eyes = {}
    for index, row in enumerate(inspection.get('eyes', [])[:2]):
        eyes['L' if index == 0 else 'R'] = row['center']
    profile = {
        'schema': 'production.facial_profile.v1', 'object_name': object_name, 'options': options,
        'armature': inspection.get('armature'), 'head_bone': inspection.get('head_bone'), 'neck_bone': inspection.get('neck_bone'),
        'face_mesh': inspection.get('face_mesh'), 'eye_objects': [e['object'] for e in inspection.get('eyes', [])],
        'eye_centers': eyes, 'frame': probe.get('frame') or inspection.get('frame'),
        'mapping': compact_mapping(mapping), 'classification': analysis['classification'],
        'compatibility': analysis['compatibility'], 'hybrid': analysis['hybrid'], 'aims': probe.get('aims', []),
        'head_bone_info': probe.get('head_info'), 'neck_bone_info': probe.get('neck_info'),
        'rig_fingerprint': probe.get('rig_fingerprint'), 'calibration': calibration,
        'viseme_support': viseme_support(mapping), 'driven_keys': driven_keys, 'driven_bone_channels': driven_bones,
        'body': {'action': inspection.get('body_action'), 'animated_bones': inspection.get('body_animated_bones', []),
                 'blend_type': inspection.get('action_blend_type'), 'nla_tracks': inspection.get('nla_tracks', [])},
        'inspection_warnings': inspection.get('warnings', []), 'blocking': blocked,
        'meshes': inspection.get('meshes', []), 'timings': timings,
        'counts': {'bone_probes': len(probe.get('bone_probes', [])), 'key_probes': len(probe.get('key_probes', [])),
                   'usable_outputs': sum(1 for v in mapping.values() if v.get('usable'))},
    }
    return profile, {'inspection': inspection, 'probe': probe, 'analysis': analysis}


def require_mutable_profile(profile, needs=()):
    """Gates before any facial mutation; each failure names the measured reason."""
    kind = profile['classification']['type']
    if profile['blocking']:
        raise ValueError('; '.join(profile['blocking']))
    if kind == 'UNSUPPORTED':
        raise ValueError('FACIAL_RIG_UNSUPPORTED: ' + '; '.join(profile['classification']['limitations']))
    if kind == 'AMBIGUOUS':
        raise ValueError('AMBIGUOUS_MAPPING: semantic confidence too low for mutation (' +
                         str(profile['classification']['confidence']) + '); review alternatives, nothing is written')
    mapping = profile['mapping']
    for need in needs:
        if need == 'eyes':
            low = [k for k in semantics.EYE_OUTPUTS if not mapping.get(k, {}).get('usable')]
            if low:
                # Name the measured reason: low confidence, or a gaze axis measured in one direction only.
                detail = ', '.join(k + '=' + (mapping[k]['method'] + ' (one gaze direction measured)'
                                               if mapping.get(k, {}).get('method') == 'shape_key_single_direction'
                                               else 'confidence ' + str(mapping.get(k, {}).get('confidence', 'absent'))) for k in low)
                raise ValueError('EYE_MAPPING_LOW_CONFIDENCE: ' + detail + ' (usable >= ' + str(semantics.USABLE_CONFIDENCE) + ')')
        if need == 'jaw' and not mapping.get('jaw_open', {}).get('usable'):
            raise ValueError('MISSING_JAW_CONTROL: no usable jaw_open control; speech and jaw performance are blocked')
        if need == 'lids' and not all(mapping.get('lid_close_' + s, {}).get('usable') for s in ('L', 'R')):
            raise ValueError('MISSING_LID_CONTROL: blinks need lid_close on both sides')
    return True


def ownership(profile, policy):
    """BODY_OWNS / FACE_OWNS / BLENDED, checked against what the body Action really animates."""
    if policy not in ('BODY_OWNS', 'FACE_OWNS', 'BLENDED'):
        raise ValueError('HEAD_POLICY_INVALID: BODY_OWNS, FACE_OWNS or BLENDED')
    animated = set(profile['body']['animated_bones'])
    owned = [b for b in (profile['head_bone'], profile['neck_bone']) if b]
    clash = [b for b in owned if b in animated]
    if policy == 'FACE_OWNS' and clash:
        raise ValueError('HEAD_OWNERSHIP_CONFLICT: the body Action animates ' + ', '.join(clash) +
                         '; use BODY_OWNS or BLENDED (blended adds the facial head motion on top)')
    note = {'BODY_OWNS': 'body keeps head/neck; facial gaze uses the eyes only',
            'FACE_OWNS': 'facial layer drives head/neck; the body does not animate them',
            'BLENDED': 'body Action blend type COMBINE adds body head/neck motion on top of the facial strip'}[policy]
    return {'policy': policy, 'body_animates': clash, 'note': note}
