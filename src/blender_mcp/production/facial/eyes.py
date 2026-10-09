"""Eyes: gaze geometry, convergence, limits, saccades and eye/head/neck coordination (host side).

Angles are degrees in the face frame of the facial profile: yaw positive toward character left, pitch
positive up. Each eye aims at the *same 3D point*, so convergence comes from geometry, never from copying
one eye's Euler values to the other.

Defaults below are starting proposals with their source stated, not measurements of this rig:
- eye range limits: the rig's own limits when it has them; otherwise +/-30 deg yaw, +20/-25 deg pitch
  (conservative proposal inside commonly cited human oculomotor ranges of about +/-45 deg);
- saccade duration ~ 21 ms + 2.2 ms/deg (a common linear approximation of the saccadic "main sequence");
- typical small fixational re-targets of 1-5 deg every 0.3-0.8 s during attentive looking (proposal).
"""
from __future__ import annotations

import math
import random

DEFAULT_LIMITS = {'yaw': (-30.0, 30.0), 'pitch': (-25.0, 20.0)}
MAX_VERGENCE_DEG = 25.0
SACCADE_MS_BASE = 21.0
SACCADE_MS_PER_DEG = 2.2


def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def angles_to_point(eye_center, point, frame):
    """Yaw/pitch (deg) of the direction eye -> point, measured in the face frame."""
    d = _sub(point, eye_center)
    x, y, z = _dot(d, frame['left']), _dot(d, frame['forward']), _dot(d, frame['up'])
    if y <= 1e-6:
        return None
    yaw = math.degrees(math.atan2(x, y))
    pitch = math.degrees(math.atan2(z, math.hypot(x, y)))
    return yaw, pitch


IDENTITY = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _apply(m, p):
    return [m[0] * p[0] + m[1] * p[1] + m[2] * p[2] + m[3], m[4] * p[0] + m[5] * p[1] + m[6] * p[2] + m[7],
            m[8] * p[0] + m[9] * p[1] + m[10] * p[2] + m[11]]


def _rotate(m, v):
    return [m[0] * v[0] + m[1] * v[1] + m[2] * v[2], m[4] * v[0] + m[5] * v[1] + m[6] * v[2], m[8] * v[0] + m[9] * v[1] + m[10] * v[2]]


def moved_frame(frame, m):
    """The face frame carried by the body: m maps rest world points of the head to their current position."""
    return dict(frame, origin=_apply(m, frame['origin']), left=_rotate(m, frame['left']), up=_rotate(m, frame['up']),
                forward=_rotate(m, frame['forward']))


def resolve_point(target, frame, eyes, motion, f):
    """World point of a target at frame f: fixed points/angles are world-locked; objects are sampled per frame."""
    if target.get('type') == 'object':
        rows = ((motion or {}).get('objects') or {}).get(target['name'])
        if not rows:
            raise ValueError('GAZE_OBJECT_UNSAMPLED: object target ' + str(target.get('name')) + ' was not sampled')
        i = min(max(0, int(round(f)) - motion['frames'][0]), len(rows) - 1)
        return rows[i]
    return target_point(target, frame, eyes)


def target_point(target, frame, eyes):
    """World point from a target spec: {'point':[x,y,z]} | {'object_point':...} | camera | offscreen | angles."""
    mid = frame['origin']
    iod = frame['iod_m']
    kind = target.get('type') or ('point' if 'point' in target else target.get('target', 'camera'))
    if kind == 'point':
        return list(target['point'])
    distance = float(target.get('distance_m', 2.0))
    if kind == 'camera':
        if target.get('camera_location'):
            return list(target['camera_location'])
        return [mid[k] + frame['forward'][k] * distance for k in range(3)]
    if kind in ('angles', 'offscreen'):
        # Off-screen default: 18 deg to the side (proposal), inside the common +/-25..30 deg eye ranges so the
        # eyes alone can hold it when the body owns the head; a rig limit still refuses larger angles.
        yaw = float(target.get('yaw_deg', 18.0 if target.get('side', 'LEFT') == 'LEFT' else -18.0)
                    if kind == 'offscreen' else target.get('yaw_deg', 0.0))
        pitch = float(target.get('pitch_deg', 0.0))
        dir_ = [math.cos(math.radians(pitch)) * math.sin(math.radians(yaw)), math.cos(math.radians(pitch)) * math.cos(math.radians(yaw)),
                math.sin(math.radians(pitch))]
        return [mid[k] + (frame['left'][k] * dir_[0] + frame['forward'][k] * dir_[1] + frame['up'][k] * dir_[2]) * distance
                for k in range(3)]
    if kind == 'object':
        raise ValueError('GAZE_OBJECT_UNSAMPLED: object targets need per-frame sampling (service)')
    raise ValueError('GAZE_TARGET_INVALID: use point, object, camera, angles or offscreen')


def limits_for(mapping, side, axis):
    entry = mapping.get('eye_' + axis + '_' + side) or {}
    if entry.get('method') == 'bone_rotation' and entry.get('channel_limits') and entry.get('degrees_per_unit'):
        low, high = entry['channel_limits']
        factor = entry['degrees_per_unit']
        values = sorted([low * factor, high * factor])
        return tuple(values), 'rig limit'
    if entry.get('method') == 'shape_key_pair':
        pos = entry['positive'].get('degrees_at_full', 0)
        neg = entry['negative'].get('degrees_at_full', 0)
        return (min(pos, neg), max(pos, neg)), 'measured shape-key range'
    return DEFAULT_LIMITS[axis], 'default proposal (no rig limit)'


def gaze_for_point(point, eyes, frame, mapping, check_range=True):
    """Per-eye yaw/pitch to a point, convergence, and limit/reachability findings.

    check_range=False when head/neck will take part of the shift: reachability is then checked on the
    eyes' own share after coordination (``gaze_tracks``).
    """
    rows = {}
    findings = []
    for side in ('L', 'R'):
        angles = angles_to_point(eyes[side], point, frame)
        if angles is None:
            findings.append({'type': 'TARGET_BEHIND_FACE', 'side': side, 'severity': 'error'})
            return None, findings
        yaw, pitch = angles
        (ylo, yhi), ysrc = limits_for(mapping, side, 'yaw')
        (plo, phi), psrc = limits_for(mapping, side, 'pitch')
        reachable = ylo - 1e-6 <= yaw <= yhi + 1e-6 and plo - 1e-6 <= pitch <= phi + 1e-6
        rows[side] = {'yaw': yaw, 'pitch': pitch, 'reachable': reachable,
                      'limits': {'yaw': [ylo, yhi], 'pitch': [plo, phi], 'source': ysrc}}
        if not reachable and check_range:
            findings.append({'type': 'EYE_TARGET_OUT_OF_RANGE', 'side': side, 'yaw': round(yaw, 2), 'pitch': round(pitch, 2),
                             'limits': rows[side]['limits'], 'severity': 'error'})
    vergence = rows['R']['yaw'] - rows['L']['yaw']
    distance = math.sqrt(sum((point[k] - frame['origin'][k]) ** 2 for k in range(3)))
    if vergence > MAX_VERGENCE_DEG:
        findings.append({'type': 'IMPOSSIBLE_CONVERGENCE', 'vergence_deg': round(vergence, 2), 'distance_m': round(distance, 4),
                         'severity': 'error', 'note': 'target too close: cross-eye beyond ' + str(MAX_VERGENCE_DEG) + ' deg (proposal)'})
    elif vergence > 12:
        findings.append({'type': 'CROSS_EYE_RISK', 'vergence_deg': round(vergence, 2), 'severity': 'warning'})
    if vergence < -1e-3:
        findings.append({'type': 'DIVERGENT_GAZE', 'vergence_deg': round(vergence, 3), 'severity': 'error'})
    return {'eyes': rows, 'vergence_deg': round(vergence, 4), 'distance_m': round(distance, 4)}, findings


def coordinate(total_yaw, total_pitch, profile='NATURAL', head_policy='BODY_OWNS', limits=None):
    """Split a gaze shift between eyes, head and neck. Ratios are configurable proposals."""
    ratios = {'NATURAL': (0.65, 0.28, 0.07), 'EYES_ONLY': (1.0, 0.0, 0.0), 'HEAD_LEADS': (0.45, 0.42, 0.13),
              'RESTRAINED': (0.8, 0.17, 0.03)}
    if isinstance(profile, dict):
        eyes, head, neck = profile['eyes'], profile['head'], profile['neck']
    else:
        eyes, head, neck = ratios[profile]
    if head_policy == 'BODY_OWNS':
        eyes, head, neck = 1.0, 0.0, 0.0
    small = abs(total_yaw) < 8 and abs(total_pitch) < 6
    if small and not isinstance(profile, dict):
        eyes, head, neck = 1.0, 0.0, 0.0  # small shifts are eye-only; not every gaze change moves the head
    return {'eyes': [total_yaw * eyes, total_pitch * eyes], 'head': [total_yaw * head, total_pitch * head],
            'neck': [total_yaw * neck, total_pitch * neck], 'ratios': [eyes, head, neck],
            'note': 'eye-only (small shift)' if small else 'eyes lead, head follows, neck follows slightly'}


def saccade_frames(amplitude_deg, fps):
    ms = SACCADE_MS_BASE + SACCADE_MS_PER_DEG * abs(amplitude_deg)
    return max(1, int(round(ms / 1000.0 * fps)))


def gaze_tracks(sequence, eyes, frame, mapping, fps, frame_start, frame_end, head_policy='BODY_OWNS',
                coordination='NATURAL', motion=None):
    """Gaze shifts over time. sequence: [{'frame': f, 'target': spec, 'hold'?}] -> (tracks, findings, shifts).

    motion (optional, measured in Blender): per-frame head transform from the body Action and per-frame object
    positions. With it the eyes are solved per frame so targets stay fixated while the body moves the head.
    """
    tracks = {k: [] for k in ('eye_yaw_L', 'eye_pitch_L', 'eye_yaw_R', 'eye_pitch_R', 'head_yaw', 'head_pitch',
                              'neck_yaw', 'neck_pitch')}
    findings, shifts = [], []
    current = None
    head_lag = max(1, int(round(0.10 * fps)))  # head starts ~100 ms after the eyes (proposal)
    head_dur = max(2, int(round(0.35 * fps)))
    for index, row in enumerate(sorted(sequence, key=lambda r: r['frame'])):
        point = resolve_point(row['target'], frame, eyes, motion, row['frame'])
        seen_frame, seen_eyes = frame, eyes
        if motion and motion.get('head'):
            # Judge the target from where the body has put the head at this frame, not from the rest pose.
            i = min(max(0, int(row['frame']) - motion['frames'][0]), len(motion['head']) - 1)
            seen_frame = moved_frame(frame, motion['head'][i])
            seen_eyes = {s: _apply(motion['head'][i], eyes[s]) for s in ('L', 'R')}
        gaze, problems = gaze_for_point(point, seen_eyes, seen_frame, mapping, check_range=False)
        findings.extend(problems)
        if gaze is None or any(p['severity'] == 'error' for p in problems):
            continue
        L, R = gaze['eyes']['L'], gaze['eyes']['R']
        total_yaw = (L['yaw'] + R['yaw']) / 2
        total_pitch = (L['pitch'] + R['pitch']) / 2
        split = coordinate(total_yaw, total_pitch, coordination, head_policy)
        head_yaw, head_pitch = split['head']
        neck_yaw, neck_pitch = split['neck']
        # Eyes carry what head/neck do not; eye angles keep per-eye convergence.
        eye_sub_yaw, eye_sub_pitch = head_yaw + neck_yaw, head_pitch + neck_pitch
        target_eyes = {'L': (L['yaw'] - eye_sub_yaw, L['pitch'] - eye_sub_pitch), 'R': (R['yaw'] - eye_sub_yaw, R['pitch'] - eye_sub_pitch)}
        out_of_range = []
        for side in ('L', 'R'):
            (ylo, yhi), source = limits_for(mapping, side, 'yaw')
            (plo, phi), _ = limits_for(mapping, side, 'pitch')
            yaw, pitch = target_eyes[side]
            if not (ylo - 1e-6 <= yaw <= yhi + 1e-6 and plo - 1e-6 <= pitch <= phi + 1e-6):
                out_of_range.append({'type': 'EYE_TARGET_OUT_OF_RANGE', 'side': side, 'yaw': round(yaw, 2), 'pitch': round(pitch, 2),
                                     'limits': {'yaw': [ylo, yhi], 'pitch': [plo, phi], 'source': source},
                                     'eye_share': split['ratios'][0], 'severity': 'error'})
        if out_of_range:
            findings.extend(out_of_range)
            continue
        f0 = row['frame']
        if current is None:
            for side in ('L', 'R'):
                tracks['eye_yaw_' + side].append([frame_start, target_eyes[side][0]])
                tracks['eye_pitch_' + side].append([frame_start, target_eyes[side][1]])
            for name, value in (('head_yaw', head_yaw), ('head_pitch', head_pitch), ('neck_yaw', neck_yaw), ('neck_pitch', neck_pitch)):
                tracks[name].append([frame_start, value])
        else:
            amplitude = math.hypot(total_yaw - current['yaw'], total_pitch - current['pitch'])
            dur = saccade_frames(amplitude, fps)
            h0, h1 = f0 + head_lag, f0 + head_lag + head_dur
            new = {'head_yaw': head_yaw, 'head_pitch': head_pitch, 'neck_yaw': neck_yaw, 'neck_pitch': neck_pitch}

            def follower(name, f):
                t = 0.0 if f <= h0 else (1.0 if f >= h1 else (f - h0) / (h1 - h0))
                t = t * t * (3 - 2 * t)  # head and neck ease in/out (smoothstep)
                return current[name] + (new[name] - current[name]) * t
            for name in new:
                for f in range(int(h0), int(h1) + 1):
                    tracks[name].append([f, round(follower(name, f), 5)])
            # Eyes saccade first, then counter-rotate while the head turns (vestibulo-ocular compensation), so the
            # gaze reaches the target before the head does; every value stays inside the eye limits.
            for side, angles in (('L', L), ('R', R)):
                for axis, k in (('yaw', 0), ('pitch', 1)):
                    name = 'eye_' + axis + '_' + side
                    (lo, hi), _ = limits_for(mapping, side, axis)
                    tracks[name].append([f0, current['eyes'][side][k]])
                    for f in range(int(f0 + dur), int(max(h1, f0 + dur)) + 1):
                        need = angles[axis] - follower('head_' + axis, f) - follower('neck_' + axis, f)
                        tracks[name].append([f, round(min(hi, max(lo, need)), 5)])
            shifts.append({'frame': f0, 'amplitude_deg': round(amplitude, 3), 'saccade_frames': dur,
                           'saccade_ms': round(SACCADE_MS_BASE + SACCADE_MS_PER_DEG * amplitude, 1), 'split': split})
        current = {'yaw': total_yaw, 'pitch': total_pitch, 'eyes': target_eyes, 'head_yaw': head_yaw,
                   'head_pitch': head_pitch, 'neck_yaw': neck_yaw, 'neck_pitch': neck_pitch, 'point': point,
                   'vergence': gaze['vergence_deg']}
    for name in tracks:
        if tracks[name]:
            tracks[name].append([frame_end, tracks[name][-1][1]])
    tracks = {k: _clean(v) for k, v in tracks.items() if v}
    if motion and not any(f['severity'] == 'error' for f in findings):
        eye_tracks, problems = stabilize(tracks, sequence, eyes, frame, mapping, fps, frame_start, frame_end, motion, shifts)
        tracks.update(eye_tracks)
        findings.extend(problems)
    return tracks, findings, shifts


def stabilize(tracks, sequence, eyes, frame, mapping, fps, frame_start, frame_end, motion, shifts):
    """Per-frame eye angles that keep the current target fixated while the body moves the head.

    Facial head/neck shares (FACE_OWNS / BLENDED) are subtracted; saccades between targets keep their measured
    main-sequence duration; values are clamped to the eye limits and every clamped frame is reported.
    """
    from .tracks import sample
    rows = sorted(sequence, key=lambda r: r['frame'])
    durations = {s['frame']: s['saccade_frames'] for s in shifts}
    head = motion.get('head')
    out = {k: [] for k in ('eye_yaw_L', 'eye_pitch_L', 'eye_yaw_R', 'eye_pitch_R')}
    clamped, behind, verging = [], [], []
    limits = {(s, a): limits_for(mapping, s, a)[0] for s in ('L', 'R') for a in ('yaw', 'pitch')}
    for f in range(int(frame_start), int(frame_end) + 1):
        i = f - motion['frames'][0]
        m = head[i] if head and 0 <= i < len(head) else IDENTITY
        fr = moved_frame(frame, m)
        centers = {s: _apply(m, eyes[s]) for s in ('L', 'R')}

        def angles(index):
            point = resolve_point(rows[index]['target'], frame, eyes, motion, f)
            return {s: angles_to_point(centers[s], point, fr) for s in ('L', 'R')}
        index = 0
        for k, row in enumerate(rows):
            if row['frame'] <= f:
                index = k
        current = angles(index)
        start = rows[index]['frame']
        dur = durations.get(start, 0)
        if index > 0 and dur and f < start + dur:
            previous = angles(index - 1)
            if previous['L'] and previous['R'] and current['L'] and current['R']:
                t = (f - start) / dur
                current = {s: (previous[s][0] + (current[s][0] - previous[s][0]) * t,
                               previous[s][1] + (current[s][1] - previous[s][1]) * t) for s in ('L', 'R')}
        if not current['L'] or not current['R']:
            behind.append(f)
            continue
        if current['R'][0] - current['L'][0] > MAX_VERGENCE_DEG:
            verging.append(f)
        for s in ('L', 'R'):
            values = {'yaw': current[s][0] - sample(tracks.get('head_yaw', []), f) - sample(tracks.get('neck_yaw', []), f),
                      'pitch': current[s][1] - sample(tracks.get('head_pitch', []), f) - sample(tracks.get('neck_pitch', []), f)}
            for axis, value in values.items():
                lo, hi = limits[(s, axis)]
                if value < lo - 1e-6 or value > hi + 1e-6:
                    clamped.append(f)
                out['eye_' + axis + '_' + s].append([f, round(min(hi, max(lo, value)), 4)])
    findings = []
    if behind:
        findings.append({'type': 'TARGET_BEHIND_FACE', 'frames': behind[:20], 'frame_count': len(behind), 'severity': 'error'})
    if verging:
        findings.append({'type': 'IMPOSSIBLE_CONVERGENCE', 'frames': verging[:20], 'frame_count': len(verging), 'severity': 'error'})
    if clamped:
        frames = sorted(set(clamped))
        findings.append({'type': 'EYE_TARGET_OUT_OF_RANGE', 'frames': frames[:20], 'frame_count': len(frames), 'severity': 'error',
                         'note': 'the body moves the head so far that the eyes cannot keep the target inside their limits'})
    return out, findings


def _clean(track):
    out = []
    for f, v in sorted(track, key=lambda r: r[0]):
        if out and abs(out[-1][0] - f) < 1e-9:
            out[-1] = [f, v]
        else:
            out.append([f, v])
    return out


def saccade_tracks(base_tracks, fps, frame_start, frame_end, seed, frequency_hz=1.6, amplitude_deg=2.0,
                   attention_style='ATTENTIVE', cluster_deg=None, quiet=()):
    """Bounded, seeded micro re-targets around the base gaze; never perpetual jitter.

    frequency_hz is an upper bound for the mean rate; intervals are drawn in [0.6, 1.4] / frequency_hz.
    Each saccade holds its new fixation; the cluster keeps offsets inside +/- cluster_deg of the base.
    """
    if not 0 < frequency_hz <= 4 or not 0 < amplitude_deg <= 8:
        raise ValueError('SACCADE_PARAMETERS_INVALID: frequency 0..4 Hz, amplitude 0..8 deg')
    styles = {'ATTENTIVE': 0.7, 'SEARCHING': 1.3, 'NERVOUS': 1.6, 'TIRED': 0.4}
    if attention_style not in styles:
        raise ValueError('ATTENTION_STYLE_INVALID: ' + ', '.join(styles))
    rng = random.Random(seed)
    rate = frequency_hz * min(1.0, styles[attention_style])
    cluster = cluster_deg if cluster_deg is not None else amplitude_deg * 1.5
    offsets = []
    f = frame_start + max(2, int(fps * 0.4))
    off_yaw = off_pitch = 0.0
    events = []
    while f < frame_end - 2:
        interval = rng.uniform(0.6, 1.4) / rate
        if not any(a <= f <= b for a, b in quiet):
            new_yaw = max(-cluster, min(cluster, off_yaw + rng.uniform(-amplitude_deg, amplitude_deg)))
            new_pitch = max(-cluster * 0.6, min(cluster * 0.6, off_pitch + rng.uniform(-amplitude_deg, amplitude_deg) * 0.6))
            amp = math.hypot(new_yaw - off_yaw, new_pitch - off_pitch)
            dur = saccade_frames(amp, fps)
            offsets.append((f, off_yaw, off_pitch))
            offsets.append((f + dur, new_yaw, new_pitch))
            events.append({'frame': f, 'amplitude_deg': round(amp, 3), 'frames': dur})
            off_yaw, off_pitch = new_yaw, new_pitch
        f += max(int(round(interval * fps)), 3)
    tracks = {}
    for side in ('L', 'R'):
        for axis, k in (('yaw', 1), ('pitch', 2)):
            name = 'eye_' + axis + '_' + side
            base = base_tracks.get(name, [[frame_start, 0.0], [frame_end, 0.0]])
            keys = [[frame_start, 0.0]] + [[o[0], o[k]] for o in offsets] + [[frame_end, offsets[-1][k] if offsets else 0.0]]
            tracks[name] = _clean(keys)
    rate_measured = len(events) / max(1e-9, (frame_end - frame_start) / fps)
    return tracks, {'count': len(events), 'mean_rate_hz': round(rate_measured, 3), 'max_amplitude_deg': max((e['amplitude_deg'] for e in events), default=0.0),
                    'seed': seed, 'events': events[:40], 'cluster_deg': cluster}


def lid_follow_tracks(gaze_tracks_, fps, gain=0.35, max_pitch_deg=25.0):
    """Upper lids lower slightly when the eyes look down (gain per full downward pitch; proposal)."""
    tracks = {}
    for side in ('L', 'R'):
        pitch = gaze_tracks_.get('eye_pitch_' + side)
        if not pitch:
            continue
        tracks['lid_close_' + side] = [[f, round(gain * max(0.0, -v) / max_pitch_deg, 5)] for f, v in pitch]
        tracks['lid_wide_' + side] = [[f, round(0.5 * gain * max(0.0, v) / max_pitch_deg, 5)] for f, v in pitch]
    return tracks
