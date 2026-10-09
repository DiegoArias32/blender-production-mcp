"""Microexpressions and intentional asymmetry (host side).

Microexpressions are small, brief, intentional and contextual changes, never noise. Bounds below are
starting proposals with their source: brief "leakage" expressions are commonly described as lasting under
about 0.5 s (Ekman's work on microexpressions); here each event lasts 0.2-0.5 s, peaks at no more than
0.25 of a control's range, and events are at least 1.5 s apart and at most 0.5 per second.
"""
from __future__ import annotations

import random

MAX_AMPLITUDE = 0.25
MIN_SPACING_S = 1.5
MAX_RATE_HZ = 0.5
CATALOG = {
    'brow_flash': {'brow_inner_raise_L': 1.0, 'brow_inner_raise_R': 1.0},
    'brow_knit': {'brow_lower_L': 1.0, 'brow_lower_R': 1.0},
    'lip_compress': {'lip_press_L': 1.0, 'lip_press_R': 1.0},
    'brief_squint': {'lid_raise_L': 1.0, 'lid_raise_R': 1.0},
    'nose_flare': {'nose_sneer_L': 0.8, 'nose_sneer_R': 0.8},
    'corner_tug': {'lip_corner_up_L': 1.0},
    'corner_drop': {'lip_corner_down_L': 0.7, 'lip_corner_down_R': 0.7},
    'jaw_clench': {'lip_press_L': 0.6, 'lip_press_R': 0.6, 'jaw_forward': 0.4},
    'eye_aversion': {'eye_yaw_L': -6.0, 'eye_yaw_R': -6.0, 'eye_pitch_L': -3.0, 'eye_pitch_R': -3.0},
}
CONTEXT = {
    'ANGER': ['lip_compress', 'jaw_clench', 'brow_knit', 'nose_flare'],
    'FEAR': ['brow_flash', 'eye_aversion', 'lip_compress'],
    'SUSPICION': ['brief_squint', 'corner_tug', 'eye_aversion'],
    'SADNESS': ['corner_drop', 'brow_flash'],
    'PAIN': ['brief_squint', 'brow_knit', 'jaw_clench'],
    'DISGUST': ['nose_flare', 'corner_drop'],
    'JOY': ['corner_tug', 'brow_flash'],
    'FATIGUE': ['eye_aversion', 'corner_drop'],
    'NEUTRAL': ['brow_flash', 'lip_compress'],
}


def plan(brief, frame_start, frame_end, fps, seed, count=None, amplitude=0.18, avoid=()):
    """Choose a few context-appropriate events with seeded timing; bounded amplitude, spacing and rate."""
    if not 0 < amplitude <= MAX_AMPLITUDE:
        raise ValueError('MICRO_AMPLITUDE_INVALID: (0, ' + str(MAX_AMPLITUDE) + '] of a control range')
    rng = random.Random(seed)
    emotions = sorted(brief['emotions'].items(), key=lambda kv: -kv[1])
    kinds = []
    for emotion, _ in emotions:
        kinds += [k for k in CONTEXT.get(emotion, []) if k not in kinds]
    seconds = (frame_end - frame_start) / fps
    limit = max(1, int(seconds * MAX_RATE_HZ))
    n = min(limit, count if count is not None else max(1, int(seconds / 2.5)))
    events = []
    spacing = MIN_SPACING_S * fps
    attempts = 0
    while len(events) < n and attempts < 200:
        attempts += 1
        f = rng.uniform(frame_start + 0.4 * fps, frame_end - 0.6 * fps)
        if any(abs(f - e['frame']) < spacing for e in events) or any(a <= f <= b for a, b in avoid):
            continue
        kind = kinds[len(events) % len(kinds)] if kinds else 'brow_flash'
        duration = rng.uniform(0.2, 0.5)
        events.append({'frame': round(f, 2), 'kind': kind, 'duration_s': round(duration, 3), 'amplitude': amplitude,
                       'context': emotions[0][0] if emotions else 'NEUTRAL',
                       'purpose': 'leak of ' + (emotions[0][0].lower() if emotions else 'neutral') + ' under the base expression'})
    events.sort(key=lambda e: e['frame'])
    return events, {'count': len(events), 'max_rate_hz': MAX_RATE_HZ, 'min_spacing_s': MIN_SPACING_S,
                    'max_amplitude': MAX_AMPLITUDE, 'seed': seed, 'rate_hz': round(len(events) / max(1e-9, seconds), 3)}


def tracks(events, fps):
    out = {}
    for e in events:
        if e['kind'] not in CATALOG:
            raise ValueError('MICRO_KIND_INVALID: ' + e['kind'])
        if e['amplitude'] > MAX_AMPLITUDE + 1e-9:
            raise ValueError('MICRO_AMPLITUDE_INVALID: bounded to ' + str(MAX_AMPLITUDE))
        d = e['duration_s'] * fps
        f0 = e['frame']
        for name, weight in CATALOG[e['kind']].items():
            # Normalized outputs peak at weight x amplitude; eye weights are degrees at the default amplitude 0.18.
            peak = weight * e['amplitude'] / 0.18 if name.startswith('eye_') else weight * e['amplitude']
            out.setdefault(name, []).extend([[f0, 0.0], [f0 + d * 0.3, peak], [f0 + d * 0.65, peak * 0.8], [f0 + d, 0.0]])
    return {k: sorted(v) for k, v in out.items()}


def asymmetry_tracks(rows, frame_start, frame_end, fps):
    """Intentional asymmetry: every entry has purpose, strength and timing; nothing random."""
    areas = {'brow': ('brow_inner_raise_', 'brow_outer_raise_'), 'lip_corner': ('lip_corner_up_',),
             'lid': ('lid_raise_',), 'cheek': ('cheek_raise_',), 'brow_lower': ('brow_lower_',)}
    out, notes = {}, []
    for row in rows:
        if not {'area', 'side', 'strength', 'purpose'} <= set(row):
            raise ValueError('ASYMMETRY_INVALID: area, side, strength, purpose and optional timing are required')
        if row['area'] not in areas or row['side'] not in ('L', 'R'):
            raise ValueError('ASYMMETRY_INVALID: area ' + ', '.join(areas) + '; side L or R')
        strength = float(row['strength'])
        if not 0 < strength <= 0.6:
            raise ValueError('ASYMMETRY_INVALID: strength in (0, 0.6]')
        start = row.get('start_frame', frame_start)
        end = row.get('end_frame', frame_end)
        ramp = max(1.0, row.get('ramp_s', 0.3) * fps)
        for prefix in areas[row['area']]:
            name = prefix + row['side']
            out.setdefault(name, []).extend([[start, 0.0], [start + ramp, strength], [end, strength]])
        notes.append({'area': row['area'], 'side': row['side'], 'strength': strength, 'purpose': row['purpose'],
                      'frames': [start, end]})
    return {k: sorted(v) for k, v in out.items()}, notes
