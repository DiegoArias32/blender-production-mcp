"""Facial curve analysis and polish on semantic tracks (host side).

Normalized outputs (0..1) and degree outputs (eyes/head) use different thresholds. Polish is a proposal first
and never smooths protected windows: speech closures, blinks, fast eye motion and declared impact events.
The inherited M2/M6 F-Curve tools stay the reference for body channels; facial channels are judged at the
semantic level because shape-key and bone controls have different units.
"""
from __future__ import annotations

import math

from .tracks import sample, simplify

THRESHOLDS = {
    'normalized': {'spike': 0.15, 'noise_hf': 0.02, 'overshoot': 1e-6, 'hold_frames': 12, 'velocity_per_s': 12.0},
    'degrees': {'spike': 3.0, 'noise_hf': 0.6, 'overshoot': None, 'hold_frames': 24, 'velocity_per_s': 900.0},
}
OPPOSING = [('brow_inner_raise_{s}', 'brow_lower_{s}'), ('lip_corner_up_{s}', 'lip_corner_down_{s}'),
            ('lid_close_{s}', 'lid_wide_{s}')]


def _unit(name):
    return 'degrees' if name.startswith(('eye_', 'head_', 'neck_')) else 'normalized'


def _per_frame(track):
    if not track:
        return []
    start, end = int(math.floor(track[0][0])), int(math.ceil(track[-1][0]))
    return [[f, sample(track, f)] for f in range(start, end + 1)]


def analyze(tracks, fps, protected=None, limits=None):
    protected = protected or {}
    rows, findings = {}, []
    for name, track in tracks.items():
        unit = _unit(name)
        t = THRESHOLDS[unit]
        dense = _per_frame(track)
        values = [v for _, v in dense]
        frames = [f for f, _ in dense]
        if len(values) < 3:
            continue
        spikes = []
        for i in range(1, len(values) - 1):
            mid = (values[i - 1] + values[i + 1]) / 2
            if abs(values[i] - mid) > t['spike'] and abs(values[i - 1] - values[i + 1]) < t['spike'] * 0.5:
                if not _inside(frames[i], protected.get(name, [])):
                    spikes.append(frames[i])
        d2 = [values[i + 1] - 2 * values[i] + values[i - 1] for i in range(1, len(values) - 1)]
        # Noise is measured outside protected windows: a blink or a closure is fast on purpose, not noise.
        free = [d for i, d in zip(range(1, len(values) - 1), d2) if not _inside(frames[i], protected.get(name, []))]
        hf = math.sqrt(sum(d * d for d in free) / len(free)) if free else 0.0
        velocity = max(abs(b - a) for a, b in zip(values, values[1:])) * fps
        accel = max(abs(d) for d in d2) * fps * fps if d2 else 0.0
        overshoot = []
        if unit == 'normalized':
            overshoot = [f for f, v in dense if v < -t['overshoot'] or v > 1 + t['overshoot']]
        elif limits and name in limits:
            lo, hi = limits[name]
            overshoot = [f for f, v in dense if v < lo - 1e-6 or v > hi + 1e-6]
        holds, run = [], 1
        for i in range(1, len(values)):
            if abs(values[i] - values[i - 1]) < 1e-5:
                run += 1
            else:
                if run >= t['hold_frames'] and abs(values[i - 1]) > 1e-4:
                    holds.append([frames[i - run], frames[i - 1]])
                run = 1
        rows[name] = {'unit': unit, 'keys': len(track), 'spikes': spikes[:20], 'spike_count': len(spikes),
                      'hf_noise': round(hf, 6), 'max_velocity_per_s': round(velocity, 4), 'max_accel_per_s2': round(accel, 3),
                      'overshoot_frames': overshoot[:20], 'holds': holds[:10]}
        if spikes:
            findings.append({'type': 'SPIKE', 'semantic': name, 'count': len(spikes), 'severity': 'warning'})
        if hf > t['noise_hf']:
            findings.append({'type': 'NOISE', 'semantic': name, 'hf_noise': round(hf, 5), 'threshold': t['noise_hf'], 'severity': 'warning'})
        if overshoot:
            findings.append({'type': 'OVERSHOOT', 'semantic': name, 'frames': len(overshoot), 'severity': 'error'})
        if velocity > t['velocity_per_s'] and not protected.get(name):
            findings.append({'type': 'VELOCITY', 'semantic': name, 'max_per_s': round(velocity, 3), 'severity': 'warning'})
    for pattern in OPPOSING:
        for side in ('L', 'R'):
            a, b = pattern[0].format(s=side), pattern[1].format(s=side)
            if a in tracks and b in tracks:
                both = [f for f, v in _per_frame(tracks[a]) if v > 0.5 and sample(tracks[b], f) > 0.5]
                if both:
                    findings.append({'type': 'OPPOSING_OUTPUTS', 'pair': [a, b], 'frames': len(both), 'severity': 'warning'})
    return {'channels': rows, 'findings': findings, 'thresholds': THRESHOLDS}


def _inside(frame, windows):
    return any(a <= frame <= b for a, b in windows)


def polish(tracks, fps, protected=None, strength=0.5, simplify_tolerance=None):
    """Spike removal + gentle smoothing outside protected windows, then key reduction. Returns new tracks."""
    protected = protected or {}
    out, report = {}, {}
    for name, track in tracks.items():
        unit = _unit(name)
        dense = _per_frame(track)
        values = [v for _, v in dense]
        frames = [f for f, _ in dense]
        windows = protected.get(name, [])
        new = list(values)
        t = THRESHOLDS[unit]
        for i in range(1, len(values) - 1):
            if _inside(frames[i], windows):
                continue
            mid = (values[i - 1] + values[i + 1]) / 2
            if abs(values[i] - mid) > t['spike'] and abs(values[i - 1] - values[i + 1]) < t['spike'] * 0.5:
                new[i] = mid
        smooth = list(new)
        for i in range(1, len(new) - 1):
            if _inside(frames[i], windows):
                continue
            smooth[i] = new[i] * (1 - strength) + (new[i - 1] + new[i + 1]) / 2 * strength
        tol = simplify_tolerance if simplify_tolerance is not None else (0.003 if unit == 'normalized' else 0.1)
        result = simplify([[f, round(v, 6)] for f, v in zip(frames, smooth)], tol)
        dev = [abs(a - b) for a, b in zip(values, smooth)]
        protected_dev = [abs(a - b) for f, a, b in zip(frames, values, smooth) if _inside(f, windows)]
        out[name] = result
        report[name] = {'keys_before': len(track), 'keys_after': len(result), 'max_deviation': round(max(dev), 6) if dev else 0.0,
                        'protected_max_deviation': round(max(protected_dev), 8) if protected_dev else 0.0,
                        'protected_windows': len(windows)}
    return out, report
