"""Blinks: shapes and context-aware scheduling (host side).

Starting values with their source (proposals, not measurements of a given character):
- spontaneous blink durations of roughly 100-400 ms, closing faster than opening (values commonly reported
  in the blink literature); defaults: full blink ~ 0.25 s at 30/60 fps, closing ~35% of it;
- spontaneous rates around 15-20 blinks/min at rest, higher in conversation and lower during attentive
  looking (commonly reported ranges, e.g. Bentivoglio et al., 1997); the scheduler uses a bounded random
  interval around a context rate, seeded for deterministic tests.
Blinks also cluster around large gaze shifts; a blink is preferred at the start of a shift.
"""
from __future__ import annotations

import random

from .tracks import sample

KINDS = {'FULL': {'seconds': 0.25, 'strength': 1.0, 'close_fraction': 0.35, 'hold': 0.0},
         'PARTIAL': {'seconds': 0.20, 'strength': 0.6, 'close_fraction': 0.4, 'hold': 0.0},
         'SLOW': {'seconds': 0.60, 'strength': 1.0, 'close_fraction': 0.4, 'hold': 0.08}}
CONTEXT_RATE_PER_MIN = {'rest': 17.0, 'speech': 22.0, 'attentive': 8.0, 'tired': 25.0, 'tense': 20.0}


def blink_keys(frame, fps, kind='FULL', strength=None, duration_s=None, asymmetry=0.0, lead_side='L'):
    """Keys for lid_close_L/R. asymmetry (0..0.5) delays/weakens the trailing lid; never a rigid switch."""
    if kind not in KINDS:
        raise ValueError('BLINK_KIND_INVALID: FULL, PARTIAL or SLOW')
    spec = KINDS[kind]
    seconds = float(duration_s or spec['seconds'])
    if not 0.06 <= seconds <= 1.5:
        raise ValueError('BLINK_DURATION_INVALID: 0.06..1.5 s')
    s = float(spec['strength'] if strength is None else strength)
    if not 0 < s <= 1:
        raise ValueError('BLINK_STRENGTH_INVALID: (0, 1]')
    if not 0 <= asymmetry <= 0.5:
        raise ValueError('BLINK_ASYMMETRY_INVALID: 0..0.5')
    total = max(3.0, seconds * fps)
    # Whole closing frames: the fully closed key lands on a rendered frame (at 30 fps a fractional peak is never shown).
    close = float(max(1, int(round(total * spec['close_fraction']))))
    hold = spec['hold'] * fps
    out = {}
    for side in ('L', 'R'):
        trailing = side != lead_side
        delay = asymmetry * close * (1.0 if trailing else 0.0)
        peak = s * (1 - 0.5 * asymmetry if trailing else 1.0)
        f0 = frame + delay
        keys = [[f0, 0.0], [f0 + close * 0.55, peak * 0.75], [f0 + close, peak]]
        if hold:
            keys.append([f0 + close + hold, peak])
        opening = total - close
        keys += [[f0 + close + hold + opening * 0.4, peak * 0.45], [f0 + close + hold + opening, 0.0]]
        out['lid_close_' + side] = [[round(f, 3), round(v, 4)] for f, v in keys]
    return out, {'frame': frame, 'kind': kind, 'duration_s': round(seconds, 4), 'frames': round(total, 2),
                 'closing_frames': round(close, 2), 'strength': s, 'asymmetry': asymmetry}


def plan_blinks(frame_start, frame_end, fps, seed, context='rest', gaze_shifts=(), speech_intervals=(),
                emotion=None, rate_per_min=None, min_gap_s=1.2):
    """Context-aware, bounded, seeded schedule: not blink-every-N-seconds."""
    if context not in CONTEXT_RATE_PER_MIN:
        raise ValueError('BLINK_CONTEXT_INVALID: ' + ', '.join(CONTEXT_RATE_PER_MIN))
    rate = float(rate_per_min or CONTEXT_RATE_PER_MIN[context])
    if emotion in ('FATIGUE',):
        rate *= 1.25
    if emotion in ('ANGER', 'SUSPICION'):
        rate *= 0.8  # fixed stares read as tension (proposal)
    rng = random.Random(seed)
    mean_gap = 60.0 / rate
    events = []
    for shift in gaze_shifts:
        if shift.get('amplitude_deg', 0) >= 15:
            events.append({'frame': int(round(shift['frame'])), 'kind': 'FULL', 'reason': 'large gaze shift (' + str(shift['amplitude_deg']) + ' deg)'})
    f = frame_start + rng.uniform(0.3, 1.0) * mean_gap * fps
    while f < frame_end - KINDS['FULL']['seconds'] * fps:
        speaking = any(a <= f <= b for a, b in speech_intervals)
        kind = 'PARTIAL' if speaking and rng.random() < 0.35 else ('SLOW' if context == 'tired' and rng.random() < 0.4 else 'FULL')
        events.append({'frame': int(round(f)), 'kind': kind, 'reason': 'scheduled (' + context + (', during speech' if speaking else '') + ')'})
        f += rng.uniform(0.55, 1.45) * mean_gap * fps
    events.sort(key=lambda e: e['frame'])
    kept = []
    for e in events:
        if kept and e['frame'] - kept[-1]['frame'] < min_gap_s * fps:
            if 'gaze' in e['reason'] and 'gaze' not in kept[-1]['reason']:
                kept[-1] = e
            continue
        kept.append(e)
    gaps = [b['frame'] - a['frame'] for a, b in zip(kept, kept[1:])]
    return kept, {'count': len(kept), 'rate_per_min': round(len(kept) / max(1e-9, (frame_end - frame_start) / fps) * 60, 3),
                  'context_rate_per_min': rate, 'seed': seed, 'min_gap_frames': round(min(gaps), 2) if gaps else None,
                  'gap_cv': round(_cv(gaps), 4) if len(gaps) > 1 else None,
                  'note': 'bounded random intervals around the context rate (not a fixed period)'}


def _cv(values):
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean if mean else 0.0


def blink_layer(events, fps, asymmetry=0.0):
    """Merge scheduled blinks into lid_close tracks (max of overlapping blinks)."""
    tracks = {'lid_close_L': [], 'lid_close_R': []}
    details = []
    per_blink = []
    for e in events:
        keys, info = blink_keys(e['frame'], fps, e.get('kind', 'FULL'), e.get('strength'), e.get('duration_s'),
                                e.get('asymmetry', asymmetry), e.get('lead_side', 'L'))
        details.append(dict(info, reason=e.get('reason')))
        per_blink.append(keys)
    for name in tracks:
        frames = sorted({f for keys in per_blink for f, _ in keys[name]})
        merged = []
        for f in frames:
            value = 0.0
            for keys in per_blink:
                rows = keys[name]
                if rows[0][0] <= f <= rows[-1][0]:
                    value = max(value, sample(rows, f))
            merged.append([f, round(value, 4)])
        tracks[name] = merged
    return tracks, details
