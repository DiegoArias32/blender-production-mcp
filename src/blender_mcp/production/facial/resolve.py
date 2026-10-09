"""Semantic tracks -> actual rig control channels (host side, pure Python).

Every value written comes from the facial profile's *measured* mapping: shape keys within their slider range,
bone channels within their limit constraints (with the measured sign), multi-control recipes, eyes through
bone rotation / shape-key pairs / aim targets, head and neck in bone-local space. Unsupported semantic outputs
are dropped and reported, never invented.
"""
from __future__ import annotations

import math

from .tracks import sample, simplify

TOLERANCE = {'shape_key': 0.002, 'rotation': 0.0005, 'location': 0.00003}


def _channel(store, key, info):
    if key not in store:
        store[key] = dict(info, contributions=[], keys={})
    return store[key]


def seal_track(lips, tracks, calibration):
    """lips_close (0..1 = fraction of a full seal) -> close-control fraction from the measured gap model.

    gap = g0 + jaw_gain * jaw + sum(gain_i * output_i) - (close_gain - interaction * jaw) * c  (mm; all measured).
    A full seal needs c = (g0 + jaw_gain * jaw + sum(gain_i * output_i)) / (close_gain - interaction * jaw), clamped.
    """
    g0, jaw_gain, close_gain = calibration['rest_gap_mm'], calibration['jaw_gain_mm'], calibration['close_gain_mm']
    others = {k: v for k, v in (calibration.get('other_gains_mm') or {}).items() if k in tracks}
    jaw = tracks.get('jaw_open', [])
    frames = sorted({f for f, _ in lips} | {f for f, _ in jaw} | {f for k in others for f, _ in tracks[k]})
    out = []
    for f in frames:
        j = sample(jaw, f)
        gap = g0 + jaw_gain * j + sum(gain * sample(tracks[k], f) for k, gain in others.items())
        effective = max(1e-6, close_gain - calibration.get('interaction_mm', 0.0) * j)
        out.append([f, round(min(1.0, max(0.0, sample(lips, f) * gap / effective)), 6)])
    return out


def resolve(semantic_tracks, profile, head_policy='BODY_OWNS'):
    mapping = profile['mapping']
    channels = {}
    report = {'dropped': [], 'clamped': {}, 'shared': [], 'unsupported': [], 'errors': [], 'lip_seal': None}
    driven_keys = set(profile.get('driven_keys', []))
    driven_bones = set(profile.get('driven_bone_channels', []))
    body_bones = set(profile.get('body', {}).get('animated_bones', []))
    lips = (profile.get('calibration') or {}).get('lips')
    for name, track in semantic_tracks.items():
        if name.startswith(('eye_', 'head_', 'neck_')):
            continue
        entry = mapping.get(name)
        if entry is None or not entry.get('usable'):
            if any(abs(v) > 1e-6 for _, v in track):
                report['unsupported'].append(name)
            continue
        if name == 'lips_close' and lips and lips.get('calibrated'):
            track = seal_track(track, semantic_tracks, lips)
            report['lip_seal'] = {'model': 'measured gap model', 'mode': lips['mode'], 'rest_gap_mm': lips['rest_gap_mm'],
                                  'jaw_gain_mm': lips['jaw_gain_mm'], 'close_gain_mm': lips['close_gain_mm'],
                                  'other_gains_mm': lips.get('other_gains_mm'), 'linear_model_error_mm': lips['linear_model_error_mm']}
        controls = [entry['control']] + list(entry.get('secondary', []))
        for control in controls:
            if control['type'] == 'shape_key' and control['mesh'] + ':' + control['key'] in driven_keys:
                report['errors'].append('DRIVER_CONFLICT: ' + control['key'] + ' is driven; keying it would fight the driver')
                continue
            if control['type'] == 'bone':
                if control['bone'] + '|' + control['path'] in driven_bones:
                    report['errors'].append('DRIVER_CONFLICT: ' + control['bone'] + '.' + control['path'] + ' has a driver')
                    continue
                if control['bone'] in body_bones:
                    report['errors'].append('BODY_FACE_CHANNEL_CONFLICT: the body Action animates ' + control['bone'] +
                                            '; its keys would override the facial layer')
                    continue
            _contribute(channels, control, name, track)
    for key, ch in channels.items():
        names = [c[0] for c in ch['contributions']]
        if len(names) > 1:
            report['shared'].append({'control': key, 'semantics': names})
    out = []
    for key, ch in channels.items():
        frames = sorted({f for c in ch['contributions'] for f, _ in c[2]})
        twins = {}
        for semantic, factor, track in ch['contributions']:
            base = semantic[:-2] if semantic.endswith(('_L', '_R')) else None
            if base:
                twins.setdefault(base, []).append((semantic, factor, track))
        keys = []
        clamped = 0
        lost = 0.0
        for f in frames:
            value = 0.0
            used = set()
            for base, rows in twins.items():
                if len(rows) == 2 and rows[0][1] == rows[1][1]:
                    # One bilateral control receives both sides: drive it with their mean and report the loss.
                    a, b = sample(rows[0][2], f), sample(rows[1][2], f)
                    value += (a + b) / 2 * rows[0][1]
                    lost = max(lost, abs(a - b))
                    used.update(r[0] for r in rows)
            for semantic, factor, track in ch['contributions']:
                if semantic in used:
                    continue
                value += sample(track, f) * factor
            lo, hi = ch['range']
            if value < lo - 1e-9 or value > hi + 1e-9:
                clamped += 1
            keys.append([f, round(min(hi, max(lo, value)), 6)])
        if lost > 0.05:
            report['shared'].append({'control': key, 'asymmetry_lost_max': round(lost, 4),
                                     'note': 'one bilateral control: left/right differences cannot be expressed'})
        if clamped:
            report['clamped'][key] = clamped
        out.append(_finish(ch, keys))
    eye_channels, eye_report = _resolve_eyes(semantic_tracks, mapping, profile.get('aims') or [], profile)
    out.extend(eye_channels)
    report['eyes'] = eye_report
    if head_policy in ('FACE_OWNS', 'BLENDED'):
        head_channels, head_report = _resolve_head(semantic_tracks, profile, head_policy)
        out.extend(head_channels)
        report['head_neck'] = head_report
    return out, report


def _contribute(channels, control, semantic, track):
    if control['type'] == 'shape_key':
        key = control['mesh'] + '|key_blocks["' + control['key'] + '"].value'
        ch = _channel(channels, key, {'kind': 'shape_key', 'mesh': control['mesh'], 'key': control['key'],
                                      'path': 'key_blocks["' + control['key'] + '"].value', 'index': 0,
                                      'range': (control.get('min', 0.0), control.get('max', 1.0)), 'unit': 'shape_key'})
        ch['contributions'].append((semantic, control.get('full', 1.0), track))
    elif control['type'] == 'bone':
        key = control['bone'] + '|' + control['path'] + '[' + str(control['index']) + ']'
        limit = control.get('limit')
        full = control['full']
        rng = (min(limit), max(limit)) if limit else (-abs(full), abs(full))
        ch = _channel(channels, key, {'kind': 'bone', 'bone': control['bone'], 'path': control['path'],
                                      'index': control['index'], 'range': rng,
                                      'unit': 'rotation' if control['path'].startswith('rotation') else 'location'})
        if limit:
            ch['range'] = (min(ch['range'][0], min(limit)), max(ch['range'][1], max(limit)))
        else:
            ch['range'] = (min(ch['range'][0], -abs(full)), max(ch['range'][1], abs(full)))
        ch['contributions'].append((semantic, full, track))


def _finish(ch, keys):
    unit = ch['unit']
    tol = TOLERANCE['shape_key' if unit == 'shape_key' else unit]
    reduced = simplify(keys, tol) if len(keys) > 2 else keys
    row = {k: ch[k] for k in ('kind', 'path', 'index', 'unit') if k in ch}
    if ch['kind'] == 'shape_key':
        row.update(mesh=ch['mesh'], key=ch['key'])
    else:
        row.update(bone=ch['bone'])
    row.update(keys=reduced, keys_dense=len(keys), semantics=[c[0] for c in ch['contributions']], range=list(ch['range']))
    return row


def _resolve_eyes(tracks, mapping, aims, profile):
    out, report = [], {}
    for side in ('L', 'R'):
        yaw_t, pitch_t = tracks.get('eye_yaw_' + side), tracks.get('eye_pitch_' + side)
        if not yaw_t and not pitch_t:
            continue
        yaw_m, pitch_m = mapping.get('eye_yaw_' + side), mapping.get('eye_pitch_' + side)
        if not yaw_m or not pitch_m or not yaw_m.get('usable') or not pitch_m.get('usable'):
            report[side] = 'UNSUPPORTED: gaze outputs not mapped for this eye'
            continue
        frames = sorted({f for f, _ in (yaw_t or [])} | {f for f, _ in (pitch_t or [])})
        method = yaw_m.get('method')
        if method == 'aim_target':
            aim = yaw_m['aim']
            center, rest, axes, distance = aim['eye_center'], aim['target_rest_world'], aim['target_axes_world'], aim['distance_m']
            frame = profile['frame']
            loc_keys = [[], [], []]
            for f in frames:
                yaw = math.radians(sample(yaw_t or [], f))
                pitch = math.radians(sample(pitch_t or [], f))
                d = [math.cos(pitch) * math.sin(yaw), math.cos(pitch) * math.cos(yaw), math.sin(pitch)]
                world = [center[k] + (frame['left'][k] * d[0] + frame['forward'][k] * d[1] + frame['up'][k] * d[2]) * distance for k in range(3)]
                delta = [world[k] - rest[k] for k in range(3)]
                for i in range(3):
                    loc_keys[i].append([f, round(sum(delta[k] * axes[i][k] for k in range(3)), 7)])
            for i in range(3):
                out.append({'kind': 'bone', 'bone': aim['target_bone'], 'path': 'location', 'index': i, 'unit': 'location',
                            'keys': simplify(loc_keys[i], TOLERANCE['location']), 'keys_dense': len(loc_keys[i]),
                            'semantics': ['eye_yaw_' + side, 'eye_pitch_' + side], 'range': [-1.0, 1.0]})
            report[side] = 'aim_target ' + aim['target_bone'] + ' at ' + str(round(distance, 3)) + ' m'
            continue
        for axis, entry, track in (('yaw', yaw_m, yaw_t), ('pitch', pitch_m, pitch_t)):
            if not track:
                continue
            if entry['method'] == 'bone_rotation':
                c = entry['control']
                factor = entry['degrees_per_unit']
                limit = entry.get('channel_limits') or [-math.pi, math.pi]
                keys = [[f, round(min(max(limit), max(min(limit), sample(track, f) / factor)), 7)] for f in frames]
                out.append({'kind': 'bone', 'bone': c['bone'], 'path': c['path'], 'index': c['index'], 'unit': 'rotation',
                            'keys': simplify(keys, TOLERANCE['rotation']), 'keys_dense': len(keys),
                            'semantics': ['eye_' + axis + '_' + side], 'range': list(limit)})
            elif entry['method'] == 'shape_key_pair':
                pos, neg = entry['positive'], entry['negative']
                kp = [[f, round(min(1.0, max(0.0, sample(track, f) / pos['degrees_at_full'])), 6)] for f in frames]
                kn = [[f, round(min(1.0, max(0.0, sample(track, f) / neg['degrees_at_full'])), 6)] for f in frames]
                for ctrl, keys in ((pos, kp), (neg, kn)):
                    out.append({'kind': 'shape_key', 'mesh': ctrl['mesh'], 'key': ctrl['key'],
                                'path': 'key_blocks["' + ctrl['key'] + '"].value', 'index': 0, 'unit': 'shape_key',
                                'keys': simplify(keys, TOLERANCE['shape_key']), 'keys_dense': len(keys),
                                'semantics': ['eye_' + axis + '_' + side], 'range': [0.0, 1.0]})
        report[side] = method
    return out, report


def _quat_axis_angle(axis, angle):
    s = math.sin(angle / 2)
    return [math.cos(angle / 2), axis[0] * s, axis[1] * s, axis[2] * s]


def _qmul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return [w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2]


def _to_local(q_world, axes):
    """Rotation given in world/face axes -> the same rotation in a bone's rest axes (columns of ``axes``)."""
    w, x, y, z = q_world
    v = [x, y, z]
    local = [sum(v[k] * axes[i][k] for k in range(3)) for i in range(3)]
    return [w] + local


def _euler_xyz(q):
    w, x, y, z = q
    sinr = 2 * (w * x + y * z)
    cosr = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr, cosr)
    sinp = max(-1.0, min(1.0, 2 * (w * y - z * x)))
    pitch = math.asin(sinp)
    siny = 2 * (w * z + x * y)
    cosy = 1 - 2 * (y * y + z * z)
    return [roll, pitch, math.atan2(siny, cosy)]


def _resolve_head(tracks, profile, policy):
    out, report = [], {}
    frame = profile['frame']
    for bone_key, prefix in (('head_bone_info', 'head_'), ('neck_bone_info', 'neck_')):
        info = profile.get(bone_key)
        names = [prefix + a for a in ('yaw', 'pitch', 'roll')]
        if not any(tracks.get(n) for n in names):
            continue
        if not info:
            report[prefix[:-1]] = 'UNSUPPORTED: no ' + prefix[:-1] + ' bone'
            continue
        frames = sorted({f for n in names for f, _ in tracks.get(n, [])})
        quats = []
        for f in frames:
            yaw = math.radians(sample(tracks.get(prefix + 'yaw', []), f))
            pitch = math.radians(sample(tracks.get(prefix + 'pitch', []), f))
            roll = math.radians(sample(tracks.get(prefix + 'roll', []), f))
            q = _qmul(_quat_axis_angle(frame['up'], yaw), _quat_axis_angle(frame['left'], -pitch))
            q = _qmul(q, _quat_axis_angle(frame['forward'], roll))
            quats.append(_to_local(q, info['axes_world']))
        if info['rotation_mode'] == 'QUATERNION':
            for i in range(4):
                keys = [[f, round(q[i], 7)] for f, q in zip(frames, quats)]
                out.append({'kind': 'bone', 'bone': info['name'], 'path': 'rotation_quaternion', 'index': i, 'unit': 'rotation',
                            'keys': simplify(keys, TOLERANCE['rotation']), 'keys_dense': len(keys), 'semantics': names,
                            'range': [-1.0, 1.0], 'blend': policy})
        elif info['rotation_mode'] == 'XYZ':
            eulers = [_euler_xyz(q) for q in quats]
            for i in range(3):
                keys = [[f, round(e[i], 7)] for f, e in zip(frames, eulers)]
                out.append({'kind': 'bone', 'bone': info['name'], 'path': 'rotation_euler', 'index': i, 'unit': 'rotation',
                            'keys': simplify(keys, TOLERANCE['rotation']), 'keys_dense': len(keys), 'semantics': names,
                            'range': [-math.pi, math.pi], 'blend': policy})
        else:
            report[prefix[:-1]] = 'HEAD_ROTATION_MODE_UNSUPPORTED: ' + info['rotation_mode']
            continue
        report[prefix[:-1]] = policy + ' via ' + info['name']
    return out, report


def payload_estimate(channels):
    keys = sum(len(c['keys']) for c in channels)
    return {'channels': len(channels), 'keys': keys, 'estimated_bytes': keys * 22 + len(channels) * 120}
