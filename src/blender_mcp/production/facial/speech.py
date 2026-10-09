"""Speech: validated timing input, coarticulation, lip-sync tracks, emphasis and lip-sync QA (host side).

No speech recognition and no phoneme extraction from raw audio: timing must arrive as explicit phonemes or
visemes. Without it the answer is TIMING_INPUT_REQUIRED. An optional energy envelope may scale jaw
amplitude within +/-20 % but never creates or moves linguistic events.

Coarticulation follows the dominance-function idea (Cohen & Massaro, 1993): every segment pulls the mouth
toward its target with a time-decaying dominance; the result at each frame is the dominance-weighted mean.
Closure/contact visemes (M_B_P, F_V, W_Q) are *dominant*: they must reach their target inside their
interval, so neighbouring vowels cannot dilute a bilabial closure.
"""
from __future__ import annotations

import math

from .model import CONSONANTS, DOMINANT, PHONEME_TO_VISEME, VISEMES

MOUTH_OUTPUTS = sorted({k for targets in VISEMES.values() for k in targets})


def prepare(timeline, fps, frame_start, frame_end):
    """Validate a timed phoneme/viseme timeline; return segments in frames. Rejects malformed input."""
    if not isinstance(timeline, dict):
        raise ValueError('TIMING_INPUT_REQUIRED: give timed phonemes or visemes; raw audio is not transcribed')
    if 'audio' in timeline and not (timeline.get('phonemes') or timeline.get('visemes')):
        raise ValueError('TIMING_INPUT_REQUIRED: no validated local phoneme aligner exists; supply timed phonemes/visemes')
    units = timeline.get('units', 'seconds')
    if units not in ('seconds', 'frames'):
        raise ValueError('TIMELINE_INVALID: units must be seconds or frames')
    rows = timeline.get('phonemes')
    kind = 'phoneme'
    if rows is None:
        rows = timeline.get('visemes')
        kind = 'viseme'
    if not isinstance(rows, list) or not rows:
        raise ValueError('TIMING_INPUT_REQUIRED: empty timeline')
    if len(rows) > 2000:
        raise ValueError('CAPACITY_EXCEEDED: at most 2000 timed segments')
    segments = []
    previous_end = None
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or 'start' not in row or 'end' not in row or kind not in row:
            raise ValueError('TIMELINE_INVALID: segment ' + str(index) + ' needs ' + kind + ', start and end')
        start, end = row['start'], row['end']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start, end)):
            raise ValueError('TIMELINE_INVALID: segment ' + str(index) + ' has non-finite times')
        if end <= start:
            raise ValueError('TIMELINE_INVALID: segment ' + str(index) + ' ends before it starts')
        if previous_end is not None and start < previous_end - 1e-9:
            raise ValueError('TIMELINE_INVALID: segment ' + str(index) + ' overlaps the previous one')
        previous_end = end
        if kind == 'phoneme':
            symbol = str(row['phoneme']).upper().rstrip('012')
            if symbol not in PHONEME_TO_VISEME:
                raise ValueError('TIMELINE_INVALID: unknown phoneme ' + str(row['phoneme']) + ' (ARPAbet expected)')
            viseme = PHONEME_TO_VISEME[symbol]
        else:
            viseme = str(row['viseme']).upper()
            if viseme not in VISEMES:
                raise ValueError('UNSUPPORTED_VISEME: ' + viseme)
            symbol = None
        to_frame = (lambda t: frame_start + t * fps) if units == 'seconds' else (lambda t: float(t))
        s, e = to_frame(start), to_frame(end)
        if s < frame_start - 1e-6 or e > frame_end + 1e-6:
            raise ValueError('TIMELINE_INVALID: segment ' + str(index) + ' lies outside the clip frames')
        segments.append({'index': index, 'phoneme': symbol, 'viseme': viseme, 'start': s, 'end': e,
                         'center': (s + e) / 2, 'duration_frames': e - s, 'class': 'consonant' if viseme in CONSONANTS else
                         ('rest' if viseme == 'REST' else 'vowel')})
    words = []
    for row in timeline.get('words', []) or []:
        if not isinstance(row, dict) or not {'start', 'end'} <= set(row):
            raise ValueError('TIMELINE_INVALID: words need start and end')
        stress = row.get('stress', 0.0)
        if isinstance(stress, bool) or not isinstance(stress, (int, float)) or not 0 <= stress <= 1:
            raise ValueError('TIMELINE_INVALID: word stress must be 0..1')
        s = frame_start + row['start'] * fps if units == 'seconds' else float(row['start'])
        e = frame_start + row['end'] * fps if units == 'seconds' else float(row['end'])
        words.append({'word': row.get('word'), 'start': s, 'end': e, 'stress': float(stress)})
    energy = timeline.get('energy')
    if energy is not None:
        if not isinstance(energy, list) or any(not isinstance(r, list) or len(r) != 2 for r in energy):
            raise ValueError('TIMELINE_INVALID: energy must be [[time, value], ...]')
    speaking = [s for s in segments if s['viseme'] != 'REST']
    return {'schema': 'production.prepared_speech.v1', 'kind': kind, 'units': units, 'segments': segments, 'words': words,
            'frame_start': frame_start,
            'energy': energy, 'speech_start': speaking[0]['start'] if speaking else None,
            'speech_end': speaking[-1]['end'] if speaking else None, 'segment_count': len(segments),
            'visemes_used': sorted({s['viseme'] for s in segments}),
            'source_note': timeline.get('source', 'explicit timing input')}


def _dominance(segment, t, overlap, fps):
    """Dominance of a segment at time t (frames). Width grows with duration and overlap; consonants narrower."""
    viseme = segment['viseme']
    width = max(1.5, segment['duration_frames'] * (0.55 + 0.9 * overlap))
    if viseme in DOMINANT:
        width *= 0.7
        alpha = 1.0 + 2.0 * DOMINANT[viseme]
    elif viseme == 'REST':
        alpha = 0.6
    else:
        alpha = 1.0
    d = abs(t - segment['center'])
    inside = segment['start'] <= t <= segment['end']
    return alpha * math.exp(-(d / width) ** 1.6) * (1.5 if inside else 1.0)


def coarticulate(prepared, fps, frame_start, frame_end, overlap=0.5, support=None, hard=False):
    """Per-frame mouth tracks. hard=True gives pose-to-pose holds (the thing coarticulation avoids)."""
    if not 0 <= overlap <= 1:
        raise ValueError('OVERLAP_INVALID: 0..1')
    segments = prepared['segments']
    tracks = {name: [] for name in MOUTH_OUTPUTS}
    approximations = {}
    for f in range(int(frame_start), int(frame_end) + 1):
        if hard:
            current = [s for s in segments if s['start'] <= f < s['end']]
            target = VISEMES[current[0]['viseme']] if current else {}
            for name in MOUTH_OUTPUTS:
                tracks[name].append([f, target.get(name, 0.0)])
            continue
        weights = [(_dominance(s, f, overlap, fps), s) for s in segments if abs(f - s['center']) < 6 * max(2, s['duration_frames'])]
        total = sum(w for w, _ in weights)
        for name in MOUTH_OUTPUTS:
            value = sum(w * VISEMES[s['viseme']].get(name, 0.0) for w, s in weights) / total if total > 0 else 0.0
            tracks[name].append([f, value])
    if not hard:
        for s in segments:
            if s['viseme'] not in DOMINANT:
                continue
            strength = DOMINANT[s['viseme']]
            for name, target in VISEMES[s['viseme']].items():
                if target <= 0 and name != 'jaw_open':
                    continue
                for row in tracks[name]:
                    f = row[0]
                    if s['start'] - 0.5 <= f <= s['end'] + 0.5:
                        k = math.cos(math.pi * (f - s['center']) / max(2.0, s['duration_frames'] + 1.0)) ** 2
                        if name == 'jaw_open':
                            row[1] = min(row[1], target + (row[1] - target) * (1 - strength * k))
                        else:
                            row[1] = max(row[1], target * strength * k + row[1] * (1 - strength * k))
    if support:
        for viseme, info in support.items():
            if info['status'] != 'SUPPORTED':
                approximations[viseme] = info
    return tracks, approximations


def speech_weight(prepared, fps, ramp_s=0.12):
    if prepared['speech_start'] is None:
        return []
    ramp = max(1.0, ramp_s * fps)
    s, e = prepared['speech_start'], prepared['speech_end']
    return [[s - ramp, 0.0], [s, 1.0], [e, 1.0], [e + ramp, 0.0]]


def emphasis(tracks, prepared, fps, jaw_gain=0.3, lip_gain=0.2, nod_deg=2.0, brow_gain=0.2, energy_scale=True):
    """Stress/emphasis modifiers; amplitude envelope may scale jaw within +/-20 % only."""
    extra = {'head_pitch': [], 'brow_inner_raise_L': [], 'brow_inner_raise_R': []}
    for word in prepared['words']:
        s, e, stress = word['start'], word['end'], word['stress']
        if stress <= 0:
            continue
        for name, rows in tracks.items():
            gain = jaw_gain if name == 'jaw_open' else lip_gain
            for row in rows:
                if s <= row[0] <= e:
                    row[1] = min(1.0, row[1] * (1 + gain * stress))
        peak = s + min(0.12 * fps, (e - s) / 2)
        extra['head_pitch'] += [[s - 1, 0.0], [peak, -nod_deg * stress], [e + 0.1 * fps, 0.0]]
        for side in ('L', 'R'):
            extra['brow_inner_raise_' + side] += [[s - 1, 0.0], [peak, brow_gain * stress], [e + 0.15 * fps, 0.0]]
    applied_energy = False
    energy = prepared.get('energy')
    if energy and energy_scale:
        values = [v for _, v in energy]
        top = max(values) if values else 0.0
        if top > 0:
            for row in tracks.get('jaw_open', []):
                t = (row[0] - prepared['frame_start']) / fps if prepared['units'] == 'seconds' else row[0]
                near = min(energy, key=lambda r: abs(r[0] - t))[1] / top
                row[1] = min(1.0, row[1] * (0.8 + 0.4 * near))
            applied_energy = True
    for name in extra:
        extra[name] = sorted(extra[name])
    return extra, {'stressed_words': sum(1 for w in prepared['words'] if w['stress'] > 0), 'energy_envelope_used': applied_energy,
                   'rule': 'energy scales jaw amplitude only (x0.8..x1.2); semantics come from timed input'}


def validate_lipsync(combined, prepared, fps, closure_threshold=0.9, open_ratio=0.6, max_rate_per_s=12.0,
                     approximated=None):
    """Channel-level lip-sync QA on the combined semantic tracks (geometry QA is separate)."""
    from .tracks import sample
    findings, rows = [], []
    ms_per_frame = 1000.0 / fps
    closure_errors = []
    approximated = approximated or {}
    for s in prepared['segments']:
        lo, hi = int(math.floor(s['start'] - 1)), int(math.ceil(s['end'] + 1))
        frames = range(lo, hi + 1)
        if s['viseme'] == 'M_B_P':
            values = [(sample(combined.get('lips_close', []), f), f) for f in frames]
            peak, at = max(values) if values else (0.0, s['center'])
            error_ms = (at - s['center']) * ms_per_frame
            ok = peak >= closure_threshold
            closure_errors.append(abs(error_ms))
            rows.append({'segment': s['index'], 'viseme': 'M_B_P', 'phoneme': s['phoneme'], 'peak_closure': round(peak, 3),
                         'peak_frame': at, 'timing_error_ms': round(error_ms, 1), 'passed': ok})
            if not ok:
                findings.append({'type': 'MBP_CLOSURE_NOT_REACHED', 'segment': s['index'], 'peak': round(peak, 3), 'severity': 'error'})
        elif s['viseme'] == 'F_V':
            name = 'lip_roll_lower'
            values = [sample(combined.get(name, []), f) for f in frames]
            peak = max(values) if values else 0.0
            ok = peak >= 0.45 or 'F_V' in approximated
            rows.append({'segment': s['index'], 'viseme': 'F_V', 'peak_lower_lip': round(peak, 3), 'passed': ok,
                         'approximated': 'F_V' in approximated})
            if not ok:
                findings.append({'type': 'FV_CONTACT_NOT_REACHED', 'segment': s['index'], 'peak': round(peak, 3), 'severity': 'error'})
        elif s['class'] == 'vowel':
            target = VISEMES[s['viseme']].get('jaw_open', 0.0)
            values = [(sample(combined.get('jaw_open', []), f), f) for f in frames]
            peak, at = max(values) if values else (0.0, s['center'])
            ok = peak >= open_ratio * target - 1e-9
            rows.append({'segment': s['index'], 'viseme': s['viseme'], 'peak_jaw': round(peak, 3), 'target_jaw': target,
                         'peak_frame': at, 'passed': ok})
            if not ok:
                findings.append({'type': 'VOWEL_OPENING_LOW', 'segment': s['index'], 'peak': round(peak, 3),
                                 'target': target, 'severity': 'warning'})
    overlaps = 0
    for f in range(int(prepared['segments'][0]['start']), int(prepared['segments'][-1]['end']) + 1):
        if sample(combined.get('lips_close', []), f) >= 0.8 and sample(combined.get('jaw_open', []), f) >= 0.4:
            overlaps += 1
    if overlaps:
        findings.append({'type': 'JAW_LIP_CONFLICT', 'frames': overlaps, 'severity': 'error',
                         'note': 'lips closed while the jaw is open beyond 0.4'})
    rates = {}
    for name in ('jaw_open', 'lips_close', 'lip_pucker', 'lip_funnel'):
        track = combined.get(name) or []
        if len(track) > 1:
            rates[name] = round(max(abs(b[1] - a[1]) / max(1e-9, b[0] - a[0]) for a, b in zip(track, track[1:])) * fps, 3)
    popping = {k: v for k, v in rates.items() if v > max_rate_per_s}
    if popping:
        findings.append({'type': 'MOUTH_POPPING', 'channels': popping, 'limit_per_s': max_rate_per_s, 'severity': 'warning'})
    passed = not any(f['severity'] == 'error' for f in findings)
    return {'passed': passed, 'segments': rows, 'findings': findings,
            'metrics': {'mbp_segments': sum(1 for r in rows if r['viseme'] == 'M_B_P'),
                        'mbp_closed': sum(1 for r in rows if r['viseme'] == 'M_B_P' and r['passed']),
                        'max_closure_timing_error_ms': round(max(closure_errors), 1) if closure_errors else None,
                        'jaw_lip_conflict_frames': overlaps, 'max_rate_per_s': rates},
            'note': 'Technical timing checks only; linguistic accuracy is not claimed.'}
