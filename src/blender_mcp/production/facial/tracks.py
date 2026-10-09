"""Semantic tracks and logical facial layers (host side, pure Python).

A *track* is a sorted list of ``[frame, value]`` keys for one semantic output (0..1, or degrees for eye and
head outputs). A *layer* is ``{semantic: track}``. Layers are never written over each other: ``combine``
evaluates every layer per frame and applies explicit, documented rules; every place where two layers want
incompatible things is reported as a conflict with the rule that resolved it.
"""
from __future__ import annotations

import math

LAYERS = ('BASE_EXPRESSION', 'BROWS', 'GAZE', 'BLINK', 'SPEECH', 'MICROEXPRESSION', 'HEAD_NECK')
MOUTH = ('jaw_open', 'jaw_forward', 'lips_close', 'lip_pucker', 'lip_funnel', 'lip_press_L', 'lip_press_R',
         'lip_roll_lower', 'lip_roll_upper', 'lip_stretch_L', 'lip_stretch_R', 'lower_lip_depress_L',
         'lower_lip_depress_R', 'upper_lip_raise_L', 'upper_lip_raise_R', 'lip_corner_up_L', 'lip_corner_up_R',
         'lip_corner_down_L', 'lip_corner_down_R', 'tongue_out')
CORNERS = ('lip_corner_up_L', 'lip_corner_up_R', 'lip_corner_down_L', 'lip_corner_down_R')
DEGREE_OUTPUTS = ('eye_yaw_L', 'eye_yaw_R', 'eye_pitch_L', 'eye_pitch_R', 'head_yaw', 'head_pitch', 'head_roll',
                  'neck_yaw', 'neck_pitch', 'neck_roll')
EXPRESSION_RESIDUAL_DURING_SPEECH = 0.30
CORNER_RESIDUAL_DURING_SPEECH = 0.70


def smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def sample(track, frame):
    if not track:
        return 0.0
    if frame <= track[0][0]:
        return track[0][1]
    if frame >= track[-1][0]:
        return track[-1][1]
    lo, hi = 0, len(track) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if track[mid][0] <= frame:
            lo = mid
        else:
            hi = mid
    (f0, v0), (f1, v1) = track[lo], track[hi]
    if f1 == f0:
        return v1
    t = (frame - f0) / (f1 - f0)
    return v0 + (v1 - v0) * t


def ease_track(points, frames_per_step=1.0):
    """Keys [(frame, value)] with smoothstep interpolation baked at integer frames between them."""
    points = sorted(points)
    out = []
    for (f0, v0), (f1, v1) in zip(points, points[1:]):
        start, end = int(math.floor(f0)), int(math.ceil(f1))
        for f in range(start, end):
            t = 0.0 if f1 == f0 else (f - f0) / (f1 - f0)
            out.append([f, v0 + (v1 - v0) * smoothstep(t)])
    if points:
        out.append([points[-1][0], points[-1][1]])
    return dedupe(out)


def dedupe(track):
    out = []
    for frame, value in sorted(track):
        if out and abs(out[-1][0] - frame) < 1e-9:
            out[-1] = [frame, value]
        else:
            out.append([frame, value])
    return out


def simplify(track, tolerance):
    """Ramer-Douglas-Peucker on (frame, value); keeps first/last and every local extremum."""
    if len(track) <= 2:
        return [list(k) for k in track]
    keep = {0, len(track) - 1}
    for i in range(1, len(track) - 1):
        a, b, c = track[i - 1][1], track[i][1], track[i + 1][1]
        if (b - a) * (c - b) < 0:
            keep.add(i)
    stack = [(0, len(track) - 1)]
    while stack:
        i, j = stack.pop()
        (f0, v0), (f1, v1) = track[i], track[j]
        worst, worst_k = -1.0, None
        for k in range(i + 1, j):
            f, v = track[k]
            t = (f - f0) / (f1 - f0) if f1 != f0 else 0.0
            err = abs(v - (v0 + (v1 - v0) * t))
            if err > worst:
                worst, worst_k = err, k
        if worst_k is not None and worst > tolerance:
            keep.add(worst_k)
            stack.append((i, worst_k))
            stack.append((worst_k, j))
    return [list(track[i]) for i in sorted(keep)]


def frames_of(layers):
    frames = set()
    for layer in layers.values():
        for track in layer.get('tracks', {}).values():
            for f, _ in track:
                frames.add(f)
    return sorted(frames)


def combine(layers, frame_start, frame_end, head_policy='BODY_OWNS'):
    """Evaluate all logical layers per integer frame; return (semantic tracks, conflicts, provenance)."""
    names = set()
    for layer in layers.values():
        names.update(layer.get('tracks', {}))
    frames = list(range(int(frame_start), int(frame_end) + 1))
    out = {name: [] for name in names}
    conflicts = {}

    def value(layer, name, f):
        tracks = layers.get(layer, {}).get('tracks', {})
        return sample(tracks[name], f) if name in tracks else None

    speech_weight = layers.get('SPEECH', {}).get('weight')
    for f in frames:
        w = sample(speech_weight, f) if speech_weight else 0.0
        for name in names:
            base = value('BASE_EXPRESSION', name, f) or 0.0
            brows = value('BROWS', name, f) or 0.0
            micro = value('MICROEXPRESSION', name, f) or 0.0
            expression = base + brows + micro
            result = expression
            if name in DEGREE_OUTPUTS:
                result = (value('GAZE', name, f) or 0.0) + (value('HEAD_NECK', name, f) or 0.0) + expression
            elif name in MOUTH:
                speech = value('SPEECH', name, f)
                residual = CORNER_RESIDUAL_DURING_SPEECH if name in CORNERS else EXPRESSION_RESIDUAL_DURING_SPEECH
                if speech is not None or w > 0:
                    s = speech or 0.0
                    result = s * w + expression * (1 - w * (1 - residual))
                    if w > 0.5 and expression >= 0.45 and name in ('lip_press_L', 'lip_press_R', 'lips_close') and \
                            (value('SPEECH', 'jaw_open', f) or 0) > 0.25:
                        _conflict(conflicts, 'expression_closed_lips_vs_open_vowel', name, f,
                                  'SPEECH owns the mouth while speaking; expression keeps ' +
                                  str(int(residual * 100)) + '% residual', expression, result)
            if name.startswith('lid_close'):
                blink = value('BLINK', name, f) or 0.0
                follow = value('GAZE', name, f) or 0.0
                closed = min(1.0, max(0.0, expression + follow))
                result = closed + (1 - closed) * blink
            elif name.startswith('lid_wide'):
                side = name[-1]
                expression = expression + (value('GAZE', name, f) or 0.0)
                blink = value('BLINK', 'lid_close_' + side, f) or 0.0
                if blink > 0.2 and expression > 0.2:
                    _conflict(conflicts, 'blink_vs_wide_eyes', name, f,
                              'BLINK wins: lid_wide is scaled by (1 - blink) while the lid closes', expression, expression * (1 - blink))
                result = expression * (1 - blink)
            if name not in DEGREE_OUTPUTS:
                result = min(1.0, max(0.0, result))
            out[name].append([f, round(result, 5)])
    if head_policy == 'BODY_OWNS':
        for name in [n for n in out if n.startswith(('head_', 'neck_'))]:
            if any(abs(v) > 1e-6 for _, v in out[name]):
                _conflict(conflicts, 'head_neck_owned_by_body', name, frames[0], 'BODY_OWNS: facial head/neck motion '
                          'is dropped; eyes keep the full gaze within their limits', max(abs(v) for _, v in out[name]), 0.0)
            del out[name]
    return out, [dict(v, frames=sorted(set(v['frames']))[:20], frame_count=len(set(v['frames']))) for v in conflicts.values()]


def _conflict(conflicts, kind, name, frame, rule, before, after):
    key = kind + ':' + name
    row = conflicts.setdefault(key, {'type': kind, 'semantic': name, 'rule': rule, 'frames': [],
                                     'max_requested': 0.0, 'max_resolved_change': 0.0})
    row['frames'].append(frame)
    row['max_requested'] = round(max(row['max_requested'], abs(before)), 4)
    row['max_resolved_change'] = round(max(row['max_resolved_change'], abs(before - after)), 4)


def hold_layer(targets, frame_start, frame_end, attack_frames, release_frames=0, settle=None):
    """Expression held over the clip: ease in from neutral, hold, optional ease out."""
    tracks = {}
    for name, target in targets.items():
        points = [(frame_start, 0.0), (frame_start + attack_frames, target)]
        if settle:
            points.append((frame_start + attack_frames + settle['frames'], target * settle['ratio']))
        if release_frames:
            points.append((frame_end - release_frames, points[-1][1]))
            points.append((frame_end, 0.0))
        else:
            points.append((frame_end, points[-1][1]))
        tracks[name] = ease_track(points)
    return tracks


def track_stats(track, fps):
    values = [v for _, v in track]
    if len(values) < 2:
        return {'keys': len(values), 'max': max(values) if values else 0.0, 'max_rate_per_s': 0.0}
    rate = max(abs(b[1] - a[1]) / max(1e-9, b[0] - a[0]) for a, b in zip(track, track[1:])) * fps
    return {'keys': len(values), 'min': min(values), 'max': max(values), 'max_rate_per_s': round(rate, 4)}
