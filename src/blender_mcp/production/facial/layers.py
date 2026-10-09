"""Logical facial layers from a profile and a brief (host side, pure Python).

A layer (BASE_EXPRESSION, BROWS, GAZE, BLINK, SPEECH, MICROEXPRESSION, HEAD_NECK) is made of named *parts*
(``expression``, ``asymmetry``, ``jaw``, ``gaze``, ``saccades``, ``lid_follow``, ``blinks``, ``speech``,
``micro``, ``mocap``...). Regenerating one part never touches the others. Every part keeps its parameters,
provenance and findings so a reviewer can see why each motion exists.
"""
from __future__ import annotations

import math

from . import blinks as blink_mod
from . import eyes as eye_mod
from . import micro as micro_mod
from . import speech as speech_mod
from .model import expression_dimensions, semantic_targets, VISEMES
from .tracks import LAYERS, combine, ease_track, hold_layer, sample, simplify

PART_LAYER = {'expression': 'BASE_EXPRESSION', 'asymmetry': 'BASE_EXPRESSION', 'jaw': 'BASE_EXPRESSION',
              'mocap': 'BASE_EXPRESSION', 'brows': 'BROWS', 'speech_brows': 'BROWS', 'gaze': 'GAZE',
              'saccades': 'GAZE', 'lid_follow': 'GAZE', 'blinks': 'BLINK', 'speech': 'SPEECH',
              'micro': 'MICROEXPRESSION', 'head': 'HEAD_NECK', 'speech_head': 'HEAD_NECK'}
TOLERANCE = {'normalized': 0.001, 'degrees': 0.02}


def _unit(name):
    return 'degrees' if name.startswith(('eye_', 'head_', 'neck_')) else 'normalized'


def compact(tracks):
    return {k: simplify(v, TOLERANCE[_unit(k)]) for k, v in tracks.items() if v}


def merge(track_sets):
    """Sum several {semantic: track} sets per frame (union of their key frames)."""
    out = {}
    names = set()
    for tracks in track_sets:
        names.update(tracks)
    for name in names:
        frames = sorted({f for tracks in track_sets for f, _ in tracks.get(name, [])})
        out[name] = [[f, round(sum(sample(tracks[name], f) for tracks in track_sets if name in tracks), 6)] for f in frames]
    return out


def layer_view(parts):
    """state parts -> combine() input: {LAYER: {'tracks': merged, 'weight': speech weight}}."""
    grouped = {layer: [] for layer in LAYERS}
    weight = None
    for name, part in parts.items():
        grouped[PART_LAYER[name]].append(part['tracks'])
        if part.get('weight'):
            weight = part['weight']
    view = {}
    for layer, sets in grouped.items():
        if sets:
            view[layer] = {'tracks': merge(sets)}
    if weight:
        view.setdefault('SPEECH', {'tracks': {}})['weight'] = weight
    return view


def compose(parts, brief, head_policy):
    return combine(layer_view(parts), brief['frame_start'], brief['frame_end'], head_policy)


def _usable(profile):
    return {k for k, v in profile['mapping'].items() if v.get('usable')}


# ---------------------------------------------------------------- BASE_EXPRESSION

def expression_part(profile, brief, options=None):
    o = dict(options or {})
    dims = expression_dimensions(brief)
    for key, value in (o.get('dimensions') or {}).items():
        if key not in dims or isinstance(value, bool) or not isinstance(value, (int, float)) or not -1 <= value <= 1:
            raise ValueError('DIMENSION_INVALID: ' + str(key) + ' must be a known dimension in -1..1')
        dims[key] = float(value)
    targets, reasons = semantic_targets(dims)
    usable = _usable(profile)
    kept = {k: round(v, 4) for k, v in targets.items() if k in usable and v > 1e-4}
    missing = sorted(k for k, v in targets.items() if k not in usable and v > 1e-4)
    fps = brief['fps']
    attack = max(1, int(round(o.get('attack_s', 0.4) * fps)))
    release = int(round(o.get('release_s', 0.0) * fps))
    settle = None
    if o.get('settle', True):
        # A held expression relaxes a little after its onset instead of freezing at the peak (proposal: 8 % over 0.8 s).
        settle = {'frames': max(1, int(round(0.8 * fps))), 'ratio': 0.92}
    if attack + release + (settle['frames'] if settle else 0) >= brief['frame_end'] - brief['frame_start']:
        settle = None
    tracks = hold_layer(kept, brief['frame_start'], brief['frame_end'], attack, release, settle)
    findings = [{'type': 'EXPRESSION_OUTPUT_UNSUPPORTED', 'semantic': k, 'severity': 'warning',
                 'note': 'the rig has no confident control for it; it is dropped'} for k in missing]
    return {'tracks': compact(tracks), 'params': {'dimensions': {k: round(v, 4) for k, v in dims.items() if abs(v) > 1e-6},
            'attack_s': o.get('attack_s', 0.4), 'release_s': o.get('release_s', 0.0), 'settle': settle},
            'targets': kept, 'reasons': {k: reasons[k] for k in kept}, 'findings': findings,
            'provenance': 'brief emotions -> library dimensions (proposal) -> measured rig outputs'}


def asymmetry_part(profile, brief, options=None):
    rows = (options or {}).get('asymmetry', brief.get('asymmetry') or [])
    if not rows:
        return None
    tracks, notes = micro_mod.asymmetry_tracks(rows, brief['frame_start'], brief['frame_end'], brief['fps'])
    usable = _usable(profile)
    dropped = sorted(k for k in tracks if k not in usable)
    return {'tracks': compact({k: v for k, v in tracks.items() if k in usable}), 'params': {'entries': notes},
            'findings': [{'type': 'ASYMMETRY_OUTPUT_UNSUPPORTED', 'semantic': k, 'severity': 'warning'} for k in dropped],
            'provenance': 'intentional asymmetry declared in the brief (purpose, strength, timing); never random'}


JAW_MODES = {
    # peak semantic targets per mode (proposals); 'speech' is produced by the lip-sync layer instead.
    'shout': {'jaw_open': 0.75, 'lip_stretch_L': 0.45, 'lip_stretch_R': 0.45, 'upper_lip_raise_L': 0.35, 'upper_lip_raise_R': 0.35},
    'roar': {'jaw_open': 0.95, 'lip_stretch_L': 0.55, 'lip_stretch_R': 0.55, 'upper_lip_raise_L': 0.7, 'upper_lip_raise_R': 0.7,
             'nose_sneer_L': 0.5, 'nose_sneer_R': 0.5},
    'tension': {'lip_press_L': 0.6, 'lip_press_R': 0.6, 'jaw_forward': 0.25},
    'pain': {'jaw_open': 0.12, 'lip_stretch_L': 0.5, 'lip_stretch_R': 0.5, 'lip_press_L': 0.3, 'lip_press_R': 0.3},
}


def jaw_part(profile, brief, options=None):
    o = dict(options or {})
    mode = o.get('mode', 'breathing')
    usable = _usable(profile)
    if 'jaw_open' not in usable and mode != 'tension':
        raise ValueError('MISSING_JAW_CONTROL: no usable jaw_open control')
    fps, fs, fe = brief['fps'], brief['frame_start'], brief['frame_end']
    start = o.get('start_frame', fs)
    end = o.get('end_frame', fe)
    if not fs <= start < end <= fe:
        raise ValueError('JAW_WINDOW_INVALID: start/end inside the clip')
    tracks = {}
    if mode == 'breathing':
        amplitude = float(o.get('amplitude', 0.05))
        period = float(o.get('period_s', 4.0))
        if not 0 < amplitude <= 0.12 or not 2.0 <= period <= 8.0:
            raise ValueError('JAW_BREATHING_INVALID: amplitude (0, 0.12], period 2..8 s (proposal: resting breath 12-20/min)')
        keys = []
        f = start
        while f <= end:
            t = (f - start) / fps
            keys.append([f, round(amplitude * (0.5 - 0.5 * math.cos(2 * math.pi * t / period)), 5)])
            f += 1
        tracks['jaw_open'] = keys
        peak = {'jaw_open': amplitude}
    elif mode in JAW_MODES:
        scale = float(o.get('intensity', 1.0))
        if not 0 < scale <= 1.0:
            raise ValueError('JAW_INTENSITY_INVALID: (0, 1]')
        attack = max(1, int(round(o.get('attack_s', 0.15 if mode in ('shout', 'roar') else 0.35) * fps)))
        release = max(1, int(round(o.get('release_s', 0.3) * fps)))
        if start + attack >= end - release:
            raise ValueError('JAW_WINDOW_INVALID: window shorter than attack + release')
        peak = {k: round(v * scale, 4) for k, v in JAW_MODES[mode].items() if k in usable}
        for name, value in peak.items():
            tracks[name] = ease_track([(start, 0.0), (start + attack, value), (end - release, value * 0.9), (end, 0.0)])
    else:
        raise ValueError('JAW_MODE_INVALID: breathing, shout, roar, tension or pain (speech comes from lip-sync)')
    if max(peak.values() or [0]) > 1.0:
        raise ValueError('JAW_EXTREME_INVALID: peak above the control range')
    return {'tracks': compact(tracks), 'params': dict(o, mode=mode), 'peak': peak, 'findings': [],
            'provenance': 'jaw mode ' + mode + ' (proposal values; validated against measured geometry at its peak)'}


# ---------------------------------------------------------------- BROWS

BROW_KINDS = {'raise': ('brow_inner_raise_', 'brow_outer_raise_'), 'inner_raise': ('brow_inner_raise_',),
              'outer_raise': ('brow_outer_raise_',), 'furrow': ('brow_lower_',), 'flash': ('brow_inner_raise_', 'brow_outer_raise_')}


def brows_part(profile, brief, options=None):
    events = (options or {}).get('events') or []
    if not events:
        raise ValueError('BROW_EVENTS_REQUIRED: give events [{frame, kind, strength, duration_s, side}]')
    usable = _usable(profile)
    tracks, rows, dropped = {}, [], set()
    fps = brief['fps']
    for e in events:
        kind = e.get('kind')
        if kind not in BROW_KINDS:
            raise ValueError('BROW_KIND_INVALID: ' + ', '.join(BROW_KINDS))
        strength = float(e.get('strength', 0.5))
        duration = float(e.get('duration_s', 0.25 if kind == 'flash' else 1.0))
        if not 0 < strength <= 1 or not 0.1 <= duration <= 5:
            raise ValueError('BROW_EVENT_INVALID: strength (0, 1], duration 0.1..5 s')
        side = e.get('side', 'both')
        sides = ('L', 'R') if side == 'both' else (side,)
        f0 = e['frame']
        d = duration * fps
        if f0 < brief['frame_start'] or f0 + d > brief['frame_end']:
            raise ValueError('BROW_EVENT_OUTSIDE_CLIP: frame ' + str(f0))
        for prefix in BROW_KINDS[kind]:
            for s in sides:
                name = prefix + s
                if name not in usable:
                    dropped.add(name)
                    continue
                tracks.setdefault(name, []).append(ease_track([(f0, 0.0), (f0 + d * 0.25, strength), (f0 + d * 0.7, strength * 0.9), (f0 + d, 0.0)]))
        rows.append({'frame': f0, 'kind': kind, 'strength': strength, 'duration_s': duration, 'side': side})
    merged = merge([{name: track} for name, tl in tracks.items() for track in tl])
    return {'tracks': compact(merged), 'params': {'events': rows}, 'findings':
            [{'type': 'BROW_OUTPUT_UNSUPPORTED', 'semantic': k, 'severity': 'warning'} for k in sorted(dropped)],
            'provenance': 'explicit brow events'}


# ---------------------------------------------------------------- GAZE / HEAD_NECK

def _target(spec):
    if isinstance(spec, dict) and 'type' not in spec and spec.get('target') in ('camera', 'offscreen', 'point', 'angles'):
        spec = dict(spec, type=spec['target'])
    return spec


def gaze_parts(profile, brief, options=None, head_policy='BODY_OWNS', motion=None):
    o = dict(options or {})
    sequence = o.get('sequence') or [{'frame': brief['frame_start'], 'target': brief.get('gaze') or {'type': 'camera'}}]
    sequence = [dict(row, target=_target(row['target'])) for row in sequence]
    for row in sequence:
        if not brief['frame_start'] <= row['frame'] <= brief['frame_end']:
            raise ValueError('GAZE_FRAME_OUTSIDE_CLIP: ' + str(row['frame']))
    eyes = profile['eye_centers']
    if set(eyes) != {'L', 'R'}:
        raise ValueError('EYES_NOT_FOUND: gaze needs both eyes')
    tracks, findings, shifts = eye_mod.gaze_tracks(sequence, eyes, profile['frame'], profile['mapping'], brief['fps'],
                                                   brief['frame_start'], brief['frame_end'], head_policy,
                                                   o.get('coordination', 'NATURAL'), motion)
    # Convergence problems explain per-eye range errors (a too-near target), so they are reported first.
    errors = sorted((f for f in findings if f['severity'] == 'error'),
                    key=lambda f: 0 if f['type'] in ('IMPOSSIBLE_CONVERGENCE', 'DIVERGENT_GAZE', 'TARGET_BEHIND_FACE') else 1)
    if errors:
        raise ValueError(errors[0]['type'] + ': ' + str({k: v for k, v in errors[0].items() if k not in ('type', 'severity')}))
    eye_tracks = {k: v for k, v in tracks.items() if k.startswith('eye_')}
    head_tracks = {k: v for k, v in tracks.items() if k.startswith(('head_', 'neck_')) and any(abs(x) > 1e-6 for _, x in v)}
    for name, track in eye_tracks.items():
        side, axis = name[-1], name.split('_')[1]
        (lo, hi), source = eye_mod.limits_for(profile['mapping'], side, axis)
        worst = max(abs(v) for _, v in track)
        if any(v < lo - 1e-6 or v > hi + 1e-6 for _, v in track):
            raise ValueError('EYE_TARGET_OUT_OF_RANGE: ' + name + ' needs ' + format(worst, '.2f') + ' deg; limits ' + str([lo, hi]) + ' (' + source + ')')
    stabilized = bool(motion and (motion.get('head') or motion.get('objects')))
    gaze = {'tracks': compact(eye_tracks), 'params': {'sequence': sequence, 'coordination': o.get('coordination', 'NATURAL'),
            'head_policy': head_policy, 'world_locked': stabilized,
            'body_head_motion': {k: (motion or {}).get(k) for k in ('max_head_translation_m', 'max_head_rotation_deg')} if motion else None},
            'shifts': shifts, 'findings': findings,
            'provenance': 'per-eye angles to one 3D target (convergence from geometry); eyes lead, head follows'}
    head = None
    if head_tracks:
        head = {'tracks': compact(head_tracks), 'params': {'from': 'gaze coordination'}, 'findings': [],
                'provenance': 'head/neck share of the gaze shifts (configurable ratios, proposal)'}
    return gaze, head


def saccade_part(profile, brief, gaze, options=None):
    o = dict(options or {})
    base = gaze['tracks'] if gaze else {}
    tracks, stats = eye_mod.saccade_tracks(base, brief['fps'], brief['frame_start'], brief['frame_end'], o.get('seed', 7),
                                           o.get('frequency_hz', 1.6), o.get('amplitude_deg', 2.0),
                                           o.get('attention_style', 'ATTENTIVE'), o.get('cluster_deg'),
                                           [tuple(w) for w in o.get('quiet', [])])
    findings = []
    combined = merge([base, tracks]) if base else tracks
    for name, track in combined.items():
        side, axis = name[-1], name.split('_')[1]
        (lo, hi), source = eye_mod.limits_for(profile['mapping'], side, axis)
        if any(v < lo - 1e-6 or v > hi + 1e-6 for _, v in track):
            findings.append({'type': 'EYE_LIMIT_EXCEEDED_BY_SACCADES', 'semantic': name, 'limits': [lo, hi], 'severity': 'error'})
    return {'tracks': compact(tracks), 'params': {k: o.get(k) for k in ('seed', 'frequency_hz', 'amplitude_deg', 'attention_style',
            'cluster_deg', 'quiet')}, 'stats': stats, 'findings': findings,
            'provenance': 'seeded, bounded re-targets around the base gaze (both eyes move together)'}


def lid_follow_part(profile, brief, eye_tracks, options=None):
    o = dict(options or {})
    gain = float(o.get('gain', 0.35))
    if not 0 < gain <= 0.6:
        raise ValueError('LID_FOLLOW_GAIN_INVALID: (0, 0.6]')
    tracks = eye_mod.lid_follow_tracks(eye_tracks, brief['fps'], gain)
    usable = _usable(profile)
    tracks = {k: v for k, v in tracks.items() if k in usable}
    return {'tracks': compact(tracks), 'params': {'gain': gain}, 'findings': [],
            'provenance': 'upper lids follow eye pitch (proposal gain per full downward pitch)'}


# ---------------------------------------------------------------- BLINK

def blinks_part(profile, brief, options=None, gaze_shifts=(), speech_intervals=()):
    o = dict(options or {})
    fps = brief['fps']
    if o.get('events'):
        events = [dict(e) for e in o['events']]
        for e in events:
            if not brief['frame_start'] <= e['frame'] <= brief['frame_end'] - 3:
                raise ValueError('BLINK_OUTSIDE_CLIP: ' + str(e['frame']))
        stats = {'count': len(events), 'source': 'explicit events'}
    else:
        emotion = max(brief['emotions'], key=brief['emotions'].get) if brief.get('emotions') else None
        context = o.get('context', 'speech' if brief.get('speech', {}).get('enabled') else 'rest')
        events, stats = blink_mod.plan_blinks(brief['frame_start'], brief['frame_end'], fps, o.get('seed', 11), context,
                                              gaze_shifts, speech_intervals, emotion, o.get('rate_per_min'),
                                              o.get('min_gap_s', 1.2))
    tracks, details = blink_mod.blink_layer(events, fps, o.get('asymmetry', 0.0))
    return {'tracks': compact(tracks), 'params': {'events': events, 'asymmetry': o.get('asymmetry', 0.0), 'seed': o.get('seed', 11)},
            'details': details, 'stats': stats, 'findings': [],
            'provenance': 'context-aware seeded schedule (not a fixed period); blinks preferred at large gaze shifts'}


# ---------------------------------------------------------------- SPEECH

def speech_parts(profile, brief, options=None):
    o = dict(options or {})
    timeline = o.get('timeline') or (brief.get('speech') or {}).get('timeline')
    if not timeline:
        raise ValueError('TIMING_INPUT_REQUIRED: give timed phonemes or visemes; audio is never transcribed here')
    fps = brief['fps']
    prepared = speech_mod.prepare(timeline, fps, brief['frame_start'], brief['frame_end'])
    support = profile['viseme_support']
    used = prepared['visemes_used']
    blocking = [v for v in used if support.get(v, {}).get('status') == 'UNSUPPORTED' and v in ('M_B_P', 'A', 'O')]
    if blocking:
        raise ValueError('VISEME_UNSUPPORTED_BY_RIG: ' + ', '.join(blocking) + ' cannot be formed (' +
                         '; '.join(v + ' missing ' + ','.join(support[v]['missing']) for v in blocking) + ')')
    tracks, approximations = speech_mod.coarticulate(prepared, fps, brief['frame_start'], brief['frame_end'],
                                                     o.get('overlap', 0.5), {v: support[v] for v in used if v in support},
                                                     o.get('hard', False))
    extra, emphasis_info = speech_mod.emphasis(tracks, prepared, fps, o.get('jaw_gain', 0.3), o.get('lip_gain', 0.2),
                                               o.get('nod_deg', 2.0), o.get('brow_gain', 0.2), o.get('energy_scale', True))
    usable = _usable(profile)
    mouth = {k: v for k, v in tracks.items() if k in usable and any(abs(x) > 1e-6 for _, x in v)}
    weight = speech_mod.speech_weight(prepared, fps)
    windows = [[s['start'], s['end'], s['viseme']] for s in prepared['segments'] if s['viseme'] in ('M_B_P', 'F_V')]
    speech = {'tracks': compact(mouth), 'weight': weight, 'params': {'overlap': o.get('overlap', 0.5), 'hard': o.get('hard', False),
              'timeline_source': prepared['source_note']}, 'prepared': prepared, 'approximations': approximations,
              'emphasis': emphasis_info, 'closure_windows': windows, 'findings':
              [{'type': 'VISEME_APPROXIMATED', 'viseme': v, 'detail': info, 'severity': 'warning'} for v, info in approximations.items()],
              'provenance': 'explicit timed ' + prepared['kind'] + 's -> dominance coarticulation (Cohen & Massaro 1993)'}
    head = {'tracks': compact({'head_pitch': extra['head_pitch']})} if extra['head_pitch'] else None
    brows = {k: v for k, v in extra.items() if k.startswith('brow') and v and k in usable}
    brow_part = {'tracks': compact(brows), 'params': {'from': 'word stress'}, 'findings': []} if brows else None
    if head:
        head.update(params={'from': 'word stress nods'}, findings=[])
    return speech, head, brow_part


def speech_intervals(parts):
    speech = parts.get('speech')
    if not speech:
        return []
    p = speech['prepared']
    return [[p['speech_start'], p['speech_end']]] if p['speech_start'] is not None else []


# ---------------------------------------------------------------- MICROEXPRESSION

def micro_part(profile, brief, options=None, avoid=()):
    o = dict(options or {})
    events = o.get('events')
    if events:
        stats = {'count': len(events), 'source': 'explicit events'}
    else:
        events, stats = micro_mod.plan(brief, brief['frame_start'], brief['frame_end'], brief['fps'], o.get('seed', 5),
                                       o.get('count'), o.get('amplitude', 0.18), avoid)
    tracks = micro_mod.tracks(events, brief['fps'])
    usable = _usable(profile)
    kept = {k: v for k, v in tracks.items() if k in usable}
    return {'tracks': compact(kept), 'params': {'events': events, 'seed': o.get('seed', 5)}, 'stats': stats,
            'findings': [{'type': 'MICRO_OUTPUT_UNSUPPORTED', 'semantic': k, 'severity': 'warning'} for k in sorted(set(tracks) - set(kept))],
            'provenance': 'small, brief, contextual events (bounded amplitude, spacing and rate)'}


# ---------------------------------------------------------------- protected windows (QA / polish)

def protected_windows(parts, fps):
    windows = {}
    blinks = parts.get('blinks')
    if blinks:
        for d in blinks.get('details', []):
            for side in ('L', 'R'):
                windows.setdefault('lid_close_' + side, []).append([d['frame'] - 1, d['frame'] + d['frames'] + 1])
    speech = parts.get('speech')
    if speech:
        for start, end, viseme in speech.get('closure_windows', []):
            for name in ('lips_close', 'jaw_open', 'lip_press_L', 'lip_press_R', 'lip_roll_lower'):
                windows.setdefault(name, []).append([start - 1, end + 1])
    for key in ('gaze', 'saccades'):
        part = parts.get(key)
        if not part:
            continue
        events = part.get('shifts') or (part.get('stats') or {}).get('events') or []
        for e in events:
            span = e.get('saccade_frames') or e.get('frames') or 2
            for name in ('eye_yaw_L', 'eye_yaw_R', 'eye_pitch_L', 'eye_pitch_R'):
                windows.setdefault(name, []).append([e['frame'] - 1, e['frame'] + span + 1])
    return windows


def event_frames(parts):
    """Frames QA must sample: blink peaks and bilabial closure centres."""
    rows = {'blink_peaks': [], 'mbp_centres': []}
    blinks = parts.get('blinks')
    if blinks:
        for d in blinks.get('details', []):
            if d['strength'] >= 0.99 and d['kind'] in ('FULL', 'SLOW'):
                rows['blink_peaks'].append(int(round(d['frame'] + d['closing_frames'])))
    speech = parts.get('speech')
    if speech:
        for s in speech['prepared']['segments']:
            if s['viseme'] == 'M_B_P':
                rows['mbp_centres'].append(int(round(s['center'])))
    mocap = parts.get('mocap')
    if mocap:
        # Captured blinks: local maxima that reach a full closure in the retargeted lid tracks.
        for name in ('lid_close_L', 'lid_close_R'):
            track = mocap['tracks'].get(name) or []
            for i in range(1, len(track) - 1):
                if track[i][1] >= 0.9 and track[i][1] >= track[i - 1][1] and track[i][1] >= track[i + 1][1]:
                    rows['blink_peaks'].append(int(round(track[i][0])))
        rows['blink_peaks'] = sorted(set(rows['blink_peaks']))
    return rows
