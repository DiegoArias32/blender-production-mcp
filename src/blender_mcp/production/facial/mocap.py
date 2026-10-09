"""Generic channel-based facial mocap: analysis, mapping, retarget and cleanup (host side).

Input: ``{'fps', 'frame_start', 'channels': {name: [values per frame]}}`` (JSON or CSV with one column per
channel). ARKit-like names are recognised, but mapping goes through normalized tokens and the semantic model,
so other naming schemes work; unknown channels stay unmapped with an explicit reason.

Assumptions stated where they are made: normalized channels in 0..1; the full value of an eye-look channel
is taken as ``eye_look_full_deg`` (default 25 deg, a proposal; capture SDKs do not standardize it); channel
"Left" refers to the performer's left unless ``mirror`` is set.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path

from .tracks import simplify

TOKENS = {
    # normalized token sequence -> (semantic, sign) ; eye look channels map to signed degrees
    'eyeblink': ('lid_close', 1), 'eyesquint': ('lid_raise', 1), 'eyewide': ('lid_wide', 1),
    'browdown': ('brow_lower', 1), 'browouterup': ('brow_outer_raise', 1), 'browinnerup': ('brow_inner_raise', 1),
    'cheeksquint': ('cheek_raise', 1), 'nosesneer': ('nose_sneer', 1), 'jawopen': ('jaw_open', 1),
    'jawforward': ('jaw_forward', 1), 'jawleft': ('jaw_side_L', 1), 'jawright': ('jaw_side_R', 1),
    'mouthclose': ('lips_close', 1), 'mouthpucker': ('lip_pucker', 1), 'mouthfunnel': ('lip_funnel', 1),
    'mouthsmile': ('lip_corner_up', 1), 'mouthfrown': ('lip_corner_down', 1), 'mouthstretch': ('lip_stretch', 1),
    'mouthpress': ('lip_press', 1), 'mouthrolllower': ('lip_roll_lower', 1), 'mouthrollupper': ('lip_roll_upper', 1),
    'mouthupperup': ('upper_lip_raise', 1), 'mouthlowerdown': ('lower_lip_depress', 1),
    'eyelookup': ('eye_pitch', 1), 'eyelookdown': ('eye_pitch', -1), 'eyelookin': ('eye_yaw', 'in'),
    'eyelookout': ('eye_yaw', 'out'), 'tongueout': ('tongue_out', 1),
}
BILATERAL = {'brow_inner_raise', 'jaw_open', 'jaw_forward', 'jaw_side_L', 'jaw_side_R', 'lips_close', 'lip_pucker',
             'lip_funnel', 'lip_roll_lower', 'lip_roll_upper', 'tongue_out'}


def load(source):
    """Mocap from a dict, a .json path or a .csv path (header row of channel names; optional 'frame')."""
    if isinstance(source, dict):
        data = source
    else:
        path = Path(source)
        if not path.is_file():
            raise ValueError('MOCAP_SOURCE_NOT_FOUND: ' + str(source))
        if path.suffix.lower() == '.json':
            data = json.loads(path.read_text(encoding='utf-8'))
        elif path.suffix.lower() == '.csv':
            text = path.read_text(encoding='utf-8')
            rows = list(csv.reader(io.StringIO(text)))
            header, body = rows[0], rows[1:]
            channels = {name: [] for name in header if name.lower() not in ('frame', 'timecode', 'time')}
            for row in body:
                for name, value in zip(header, row):
                    if name in channels:
                        channels[name].append(float(value))
            data = {'fps': 30, 'frame_start': 1, 'channels': channels, 'source': str(path)}
        else:
            raise ValueError('MOCAP_FORMAT_UNSUPPORTED: .json or .csv')
    channels = data.get('channels')
    if not isinstance(channels, dict) or not channels:
        raise ValueError('MOCAP_INVALID: no channels')
    lengths = {len(v) for v in channels.values()}
    if len(lengths) != 1:
        raise ValueError('MOCAP_INVALID: channels have different frame counts')
    for name, values in channels.items():
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError('MOCAP_INVALID: non-finite values in ' + name)
    fps = data.get('fps', 30)
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 1 <= fps <= 240:
        raise ValueError('MOCAP_INVALID: fps 1..240')
    return {'fps': fps, 'frame_start': int(data.get('frame_start', 1)), 'channels': channels,
            'frames': lengths.pop(), 'source': data.get('source', 'inline')}


def _normalize_name(name):
    """Name -> (alphanumeric token, side). Single-letter sides only count after a separator (``brow_up.L``)."""
    low = name.lower().strip()
    side = None
    match = re.search(r'[._\-\s]?(left|right)$', low)
    if match:
        side = 'L' if match.group(1) == 'left' else 'R'
        low = low[:match.start()]
    else:
        match = re.search(r'[._\-\s](l|r)$', low)
        if match:
            side = 'L' if match.group(1) == 'l' else 'R'
            low = low[:match.start()]
    return ''.join(c for c in low if c.isalnum()), side


def _hf_noise(values):
    """RMS of the second difference (a high-frequency noise indicator, in channel units per frame^2)."""
    if len(values) < 3:
        return 0.0
    d2 = [values[i + 1] - 2 * values[i] + values[i - 1] for i in range(1, len(values) - 1)]
    return math.sqrt(sum(d * d for d in d2) / len(d2))


def analyze(mocap):
    rows = {}
    for name, values in mocap['channels'].items():
        token, side = _normalize_name(name)
        rows[name] = {'min': round(min(values), 5), 'max': round(max(values), 5), 'mean': round(sum(values) / len(values), 5),
                      'hf_noise': round(_hf_noise(values), 6), 'recognised': token in TOKENS, 'side': side}
    blinks = {}
    for name, values in mocap['channels'].items():
        token, side = _normalize_name(name)
        if token == 'eyeblink':
            peaks = [i for i in range(1, len(values) - 1) if values[i] >= 0.6 and values[i] >= values[i - 1] and values[i] >= values[i + 1]]
            merged = []
            for p in peaks:
                if not merged or p - merged[-1] > 3:
                    merged.append(p)
            blinks[name] = {'count': len(merged), 'frames': [mocap['frame_start'] + p for p in merged][:30],
                            'peak_values': [round(values[p], 3) for p in merged][:30]}
    groups = {'eyes': 'eyelook', 'blinks': 'eyeblink', 'jaw': 'jaw', 'lips': 'mouth', 'brows': 'brow'}
    present = {g: [n for n in mocap['channels'] if _normalize_name(n)[0].startswith(t)] for g, t in groups.items()}
    unknown = [n for n, r in rows.items() if not r['recognised']]
    noisy = sorted(rows, key=lambda n: -rows[n]['hf_noise'])[:6]
    return {'fps': mocap['fps'], 'frame_start': mocap['frame_start'], 'frames': mocap['frames'],
            'duration_s': round(mocap['frames'] / mocap['fps'], 4), 'channel_count': len(rows), 'channels': rows,
            'groups': {g: len(v) for g, v in present.items()}, 'blinks': blinks, 'unrecognised_channels': unknown,
            'noisiest_channels': [{'channel': n, 'hf_noise': rows[n]['hf_noise']} for n in noisy],
            'out_of_range_channels': [n for n, r in rows.items() if r['min'] < -1e-6 or r['max'] > 1 + 1e-6],
            'note': 'analysis only; nothing is modified'}


def map_channels(mocap, mapping, mirror=False, eye_look_full_deg=25.0):
    """Source channel -> semantic output of the target rig, with confidence; low confidence blocks retarget."""
    usable = {k for k, v in mapping.items() if v.get('usable')}
    rows, unmapped = {}, []
    for name in mocap['channels']:
        token, side = _normalize_name(name)
        if token not in TOKENS:
            unmapped.append({'channel': name, 'reason': 'no semantic token matches this name'})
            continue
        semantic, sign = TOKENS[token]
        if mirror and side:
            side = 'R' if side == 'L' else 'L'
        targets = []
        if semantic in BILATERAL:
            if semantic == 'brow_inner_raise':
                targets = ['brow_inner_raise_L', 'brow_inner_raise_R']
            else:
                targets = [semantic]
        elif semantic in ('eye_yaw', 'eye_pitch'):
            if side is None:
                unmapped.append({'channel': name, 'reason': 'eye channel without side'}); continue
            targets = [semantic + '_' + side]
            if sign in ('in', 'out'):
                toward_left = (sign == 'out') == (side == 'L')
                sign = 1 if toward_left else -1
        else:
            if side is None:
                unmapped.append({'channel': name, 'reason': 'side expected but missing'}); continue
            targets = [semantic + '_' + side]
        missing = [t for t in targets if t not in usable]
        confidence = 0.9 if not missing else 0.3
        evidence = ['token ' + token + (' side ' + side if side else '')]
        if missing:
            evidence.append('TARGET_OUTPUT_MISSING: ' + ', '.join(missing))
        rows[name] = {'targets': targets, 'sign': sign, 'confidence': confidence, 'usable': not missing,
                      'unit': 'degrees' if semantic.startswith('eye_') else 'normalized',
                      'scale': eye_look_full_deg if semantic.startswith('eye_') else 1.0, 'evidence': evidence}
    needed = {'jaw_open', 'lid_close_L', 'lid_close_R'}
    covered = {t for r in rows.values() if r['usable'] for t in r['targets']}
    complete = needed <= covered
    confident = [r for r in rows.values() if r['usable']]
    ratio = len(confident) / max(1, len(mocap['channels']))
    blocked = not complete or ratio < 0.6
    return {'channels': rows, 'unmapped': unmapped, 'mapped_ratio': round(ratio, 3), 'complete_core': complete,
            'blocked': blocked, 'mirror': mirror, 'eye_look_full_deg': eye_look_full_deg,
            'reason': ('MOCAP_MAPPING_INCOMPLETE: core outputs jaw/blinks not covered or fewer than 60% channels mapped'
                       if blocked else None)}


def neutral_offsets(mocap, neutral_frames=10, explicit=None):
    out = {}
    for name, values in mocap['channels'].items():
        if explicit and name in explicit:
            out[name] = float(explicit[name])
        else:
            head = sorted(values[:max(1, neutral_frames)])
            out[name] = head[len(head) // 2]
    return out


def retarget_tracks(mocap, channel_map, frame_start, neutral=None):
    """Semantic tracks (one key per source frame) from mapped channels, neutral-offset and range-normalized."""
    neutral = neutral or {}
    tracks = {}
    for name, row in channel_map['channels'].items():
        if not row['usable']:
            continue
        base = neutral.get(name, 0.0)
        values = mocap['channels'][name]
        for target in row['targets']:
            track = tracks.setdefault(target, [[frame_start + i, 0.0] for i in range(len(values))])
            for i, v in enumerate(values):
                if row['unit'] == 'degrees':
                    track[i][1] += row['sign'] * max(0.0, v - base) / max(1e-6, 1 - base) * row['scale']
                else:
                    track[i][1] = max(track[i][1], max(0.0, (v - base) / max(1e-6, 1 - base)))
    for name in tracks:
        tracks[name] = [[f, round(min(1.0, v) if not name.startswith('eye_') else v, 6)] for f, v in tracks[name]]
    return tracks


def _smooth(values, protect, window=2):
    out = list(values)
    for i in range(len(values)):
        if i in protect:
            continue
        lo, hi = max(0, i - window), min(len(values), i + window + 1)
        weights = [window + 1 - abs(j - i) for j in range(lo, hi)]
        out[i] = sum(values[j] * w for j, w in zip(range(lo, hi), weights)) / sum(weights)
    return out


def _fast_frames(values, threshold, pad=2):
    fast = set()
    for i in range(1, len(values)):
        if abs(values[i] - values[i - 1]) >= threshold:
            for j in range(max(0, i - pad - 1), min(len(values), i + pad + 1)):
                fast.add(j)
    return fast


def _saccade_frames(values, min_amplitude, max_frames=4, pad=1):
    """Frames inside saccade-like runs: same-sign steps reaching min_amplitude within max_frames.

    Random jitter changes sign from frame to frame and is not protected; a real saccade is a short monotonic run.
    """
    protect = set()
    n = len(values)
    for i in range(1, n):
        total, j, sign = 0.0, i, 0
        while j < n and j - i < max_frames:
            step = values[j] - values[j - 1]
            s = 1 if step > 0 else (-1 if step < 0 else 0)
            if sign and s and s != sign:
                break
            sign = sign or s
            total += step
            if abs(total) >= min_amplitude:
                for k in range(max(0, i - 1 - pad), min(n, j + 1 + pad)):
                    protect.add(k)
                break
            j += 1
    return protect


def cleanup(tracks, fps, options=None):
    """Proposal-ready cleanup that preserves fast intentional motion; returns new tracks and measurements."""
    o = dict({'normalized_fast_per_frame': 0.12, 'saccade_min_deg': 3.0, 'window': 2, 'simplify_normalized': 0.004,
              'simplify_degrees': 0.12, 'jaw_chatter_amplitude': 0.06, 'blink_fill': True}, **(options or {}))
    cleaned, report = {}, {}
    for name, track in tracks.items():
        values = [v for _, v in track]
        degrees = name.startswith('eye_') or name.startswith('head_') or name.startswith('neck_')
        protect = _saccade_frames(values, o['saccade_min_deg']) if degrees else _fast_frames(values, o['normalized_fast_per_frame'])
        if name.startswith('lid_close'):
            protect |= {i for i, v in enumerate(values) if v > 0.5}  # never smooth a blink away
        smoothed = _smooth(values, protect, o['window'])
        if name == 'jaw_open':
            # Chatter: small fast oscillation around a slow trend; replace by the trend only where it is small.
            trend = _smooth(values, set(), 4)
            smoothed = [trend[i] if abs(values[i] - trend[i]) <= o['jaw_chatter_amplitude'] and i not in protect else smoothed[i]
                        for i in range(len(values))]
        if name.startswith('lid_close') and o['blink_fill']:
            for i in range(1, len(smoothed) - 1):
                if 0.75 <= smoothed[i] < 0.95 and smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1]:
                    smoothed[i] = 1.0  # incomplete blink peak completed (reported as a deviation)
        new = [[f, round(v, 6)] for (f, _), v in zip(track, smoothed)]
        simplified = simplify(new, o['simplify_degrees'] if degrees else o['simplify_normalized'])
        deviation = [abs(a - b) for a, b in zip(values, smoothed)]
        cleaned[name] = simplified
        report[name] = {'keys_before': len(track), 'keys_after': len(simplified),
                        'hf_noise_before': round(_hf_noise(values), 6), 'hf_noise_after': round(_hf_noise(smoothed), 6),
                        'max_deviation': round(max(deviation), 5) if deviation else 0.0,
                        'mean_deviation': round(sum(deviation) / max(1, len(deviation)), 6),
                        'protected_frames': len(protect), 'unit': 'degrees' if degrees else 'normalized'}
    by_unit = {}
    for unit in ('normalized', 'degrees'):
        rows = [r for r in report.values() if r['unit'] == unit]
        if not rows:
            continue
        before = sum(r['hf_noise_before'] for r in rows)
        after = sum(r['hf_noise_after'] for r in rows)
        by_unit[unit] = {'channels': len(rows), 'noise_reduction_ratio': round(1 - after / before, 4) if before > 0 else 0.0,
                         'max_deviation': round(max(r['max_deviation'] for r in rows), 5),
                         'mean_deviation': round(sum(r['mean_deviation'] for r in rows) / len(rows), 6)}
    ratios = [1 - r['hf_noise_after'] / r['hf_noise_before'] for r in report.values() if r['hf_noise_before'] > 0]
    return cleaned, {'channels': report, 'by_unit': by_unit,
                     'noise_reduction_ratio': round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
                     'noise_reduction_definition': 'mean over channels of 1 - hf_noise_after / hf_noise_before (unit-free)',
                     'keys_before': sum(r['keys_before'] for r in report.values()),
                     'keys_after': sum(r['keys_after'] for r in report.values()),
                     'options': o, 'note': 'fast intentional motion (blinks, saccades, closures) is protected from smoothing'}
