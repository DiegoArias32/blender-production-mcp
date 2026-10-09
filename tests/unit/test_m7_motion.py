"""M7 motion builders (pure): speech, eyes, blinks, layers, microexpressions, curves and mocap cleanup."""
import math

import pytest

from blender_mcp.production.facial import blinks as B
from blender_mcp.production.facial import curves as C
from blender_mcp.production.facial import eyes as E
from blender_mcp.production.facial import micro as MI
from blender_mcp.production.facial import mocap as MO
from blender_mcp.production.facial import speech as SP
from blender_mcp.production.facial import tracks as T

FRAME = {'origin': [0, 0, 0], 'left': [1, 0, 0], 'up': [0, 0, 1], 'forward': [0, -1, 0], 'iod_m': 0.04}
EYES = {'L': [0.02, 0, 0], 'R': [-0.02, 0, 0]}
PHRASE = {'units': 'seconds', 'phonemes': [
    {'phoneme': 'SIL', 'start': 0.0, 'end': 0.2}, {'phoneme': 'B', 'start': 0.2, 'end': 0.28}, {'phoneme': 'AA', 'start': 0.28, 'end': 0.42},
    {'phoneme': 'M', 'start': 0.42, 'end': 0.52}, {'phoneme': 'SIL', 'start': 0.52, 'end': 0.9}],
    'words': [{'word': 'bom', 'start': 0.2, 'end': 0.52, 'stress': 0.8}]}


# ---------------------------------------------------------------- speech

def test_speech_needs_explicit_timing_and_rejects_malformed_timelines():
    with pytest.raises(ValueError, match='TIMING_INPUT_REQUIRED'):
        SP.prepare({'audio': 'line.wav'}, 30, 1, 31)
    with pytest.raises(ValueError, match='TIMELINE_INVALID'):
        SP.prepare({'phonemes': [{'phoneme': 'B', 'start': 0.1, 'end': 0.3}, {'phoneme': 'AA', 'start': 0.2, 'end': 0.4}]}, 30, 1, 31)
    with pytest.raises(ValueError, match='TIMELINE_INVALID'):
        SP.prepare({'phonemes': [{'phoneme': 'QQ', 'start': 0.1, 'end': 0.3}]}, 30, 1, 31)
    with pytest.raises(ValueError, match='UNSUPPORTED_VISEME'):
        SP.prepare({'visemes': [{'viseme': 'XX', 'start': 0.1, 'end': 0.3}]}, 30, 1, 31)
    with pytest.raises(ValueError, match='outside the clip'):
        SP.prepare({'phonemes': [{'phoneme': 'B', 'start': 0.1, 'end': 3.0}]}, 30, 1, 31)


def test_coarticulation_keeps_bilabial_closures_and_is_smoother_than_pose_to_pose():
    prepared = SP.prepare(PHRASE, 30, 1, 28)
    soft, _ = SP.coarticulate(prepared, 30, 1, 28)
    hard, _ = SP.coarticulate(prepared, 30, 1, 28, hard=True)
    check = SP.validate_lipsync(soft, prepared, 30)
    assert check['metrics']['mbp_closed'] == check['metrics']['mbp_segments'] == 2
    rate = lambda tracks: max(abs(b[1] - a[1]) for a, b in zip(tracks['jaw_open'], tracks['jaw_open'][1:]))  # noqa: E731
    assert rate(soft) < rate(hard)


def test_energy_scales_only_the_jaw_within_twenty_percent():
    timeline = dict(PHRASE, energy=[[t / 30, 1.0 if t % 2 else 0.0] for t in range(28)])
    prepared = SP.prepare(timeline, 30, 1, 28)
    tracks, _ = SP.coarticulate(prepared, 30, 1, 28)
    before = {k: [list(r) for r in v] for k, v in tracks.items()}
    extra, info = SP.emphasis(tracks, prepared, 30, jaw_gain=0.0, lip_gain=0.0)
    assert info['energy_envelope_used']
    for name in tracks:
        for (f, a), (_, b) in zip(before[name], tracks[name]):
            if name == 'jaw_open':
                assert b <= a * 1.2 + 1e-9 and b >= a * 0.8 - 1e-9
            else:
                assert a == b


# ---------------------------------------------------------------- eyes

def test_each_eye_aims_at_the_same_point_so_near_targets_converge():
    gaze, findings = E.gaze_for_point([0.0, -0.3, 0.0], EYES, FRAME, {})
    assert not findings
    assert gaze['eyes']['L']['yaw'] < 0 < gaze['eyes']['R']['yaw']  # never identical Euler values
    assert gaze['vergence_deg'] > 5
    near, problems = E.gaze_for_point([0.0, -0.06, 0.0], EYES, FRAME, {})
    assert {p['type'] for p in problems} >= {'IMPOSSIBLE_CONVERGENCE'}


def test_small_shifts_move_only_the_eyes_and_body_owns_keeps_head_still():
    assert E.coordinate(5, 2)['ratios'] == [1.0, 0.0, 0.0]
    assert E.coordinate(30, 0, 'NATURAL', 'BODY_OWNS')['ratios'] == [1.0, 0.0, 0.0]
    assert E.coordinate(30, 0, 'NATURAL', 'FACE_OWNS')['ratios'] == [0.65, 0.28, 0.07]


def test_eyes_lead_and_counter_rotate_while_the_head_follows():
    seq = [{'frame': 1, 'target': {'type': 'camera'}}, {'frame': 31, 'target': {'type': 'angles', 'yaw_deg': 30, 'pitch_deg': 0}}]
    tracks, findings, shifts = E.gaze_tracks(seq, EYES, FRAME, {}, 30, 1, 91, 'FACE_OWNS', 'NATURAL')
    assert not findings and shifts[0]['saccade_frames'] == 3
    world = lambda f: sum(T.sample(tracks[n], f) for n in ('eye_yaw_L', 'head_yaw', 'neck_yaw'))  # noqa: E731
    assert world(34) == pytest.approx(world(60), abs=0.05)  # gaze on target before the head finishes
    assert T.sample(tracks['head_yaw'], 34) < 0.5 * T.sample(tracks['head_yaw'], 60)


def test_world_locked_gaze_compensates_measured_body_head_motion():
    angle = math.radians(10)
    rot = [math.cos(angle), -math.sin(angle), 0, 0, math.sin(angle), math.cos(angle), 0, 0, 0, 0, 1, 0]
    motion = {'frames': list(range(1, 11)), 'head': [E.IDENTITY] * 5 + [rot] * 5, 'objects': {}}
    seq = [{'frame': 1, 'target': {'type': 'camera', 'distance_m': 2.0}}]
    tracks, findings, _ = E.gaze_tracks(seq, EYES, FRAME, {}, 30, 1, 10, 'BODY_OWNS', 'NATURAL', motion)
    assert not findings
    # The head turned 10 degrees about +Z; the eyes turn back by about the same amount to keep the target.
    assert abs(T.sample(tracks['eye_yaw_L'], 10) - T.sample(tracks['eye_yaw_L'], 1) + 10) < 1.0


def test_saccades_are_seeded_bounded_and_not_perpetual_jitter():
    a, sa = E.saccade_tracks({}, 30, 1, 151, seed=3, frequency_hz=1.5, amplitude_deg=2.0)
    b, sb = E.saccade_tracks({}, 30, 1, 151, seed=3, frequency_hz=1.5, amplitude_deg=2.0)
    assert a == b and sa['count'] == sb['count'] > 0
    assert sa['mean_rate_hz'] <= 1.5 and sa['max_amplitude_deg'] <= 2.0 * math.sqrt(1 + 0.36) + 1e-6
    with pytest.raises(ValueError, match='SACCADE_PARAMETERS_INVALID'):
        E.saccade_tracks({}, 30, 1, 151, seed=3, frequency_hz=9)


# ---------------------------------------------------------------- blinks

def test_blink_peak_lands_on_a_frame_and_asymmetry_is_bounded():
    keys, info = B.blink_keys(20, 30, 'FULL')
    peak = max(keys['lid_close_L'], key=lambda r: r[1])
    assert peak[1] == 1.0 and float(peak[0]).is_integer()
    with pytest.raises(ValueError, match='BLINK_ASYMMETRY_INVALID'):
        B.blink_keys(20, 30, 'FULL', asymmetry=0.8)


def test_blink_schedule_is_seeded_context_aware_and_not_periodic():
    events, stats = B.plan_blinks(1, 901, 30, seed=4, context='rest', gaze_shifts=[{'frame': 400, 'amplitude_deg': 30}])
    again, _ = B.plan_blinks(1, 901, 30, seed=4, context='rest', gaze_shifts=[{'frame': 400, 'amplitude_deg': 30}])
    assert events == again and stats['gap_cv'] > 0.05
    assert any(e['frame'] == 400 for e in events)
    assert stats['min_gap_frames'] >= 1.2 * 30 - 1e-9
    overlapping, _ = B.blink_layer([{'frame': 10}, {'frame': 12}], 30)
    assert max(v for _, v in overlapping['lid_close_L']) <= 1.0


# ---------------------------------------------------------------- layers and conflicts

def test_layer_rules_speech_owns_the_mouth_and_blink_wins_over_wide_eyes():
    layers = {'BASE_EXPRESSION': {'tracks': {'lip_press_L': [[1, 0.8], [30, 0.8]], 'lid_wide_L': [[1, 0.6], [30, 0.6]]}},
              'SPEECH': {'tracks': {'lip_press_L': [[1, 0.0], [30, 0.0]], 'jaw_open': [[1, 0.5], [30, 0.5]]}, 'weight': [[1, 1.0], [30, 1.0]]},
              'BLINK': {'tracks': {'lid_close_L': [[1, 0.0], [10, 1.0], [12, 0.0], [30, 0.0]]}},
              'HEAD_NECK': {'tracks': {'head_yaw': [[1, 5.0], [30, 5.0]]}}}
    out, conflicts = T.combine(layers, 1, 30, 'BODY_OWNS')
    assert T.sample(out['lip_press_L'], 5) == pytest.approx(0.8 * T.EXPRESSION_RESIDUAL_DURING_SPEECH, abs=1e-4)
    assert T.sample(out['lid_wide_L'], 10) == 0.0
    assert 'head_yaw' not in out
    assert {c['type'] for c in conflicts} >= {'expression_closed_lips_vs_open_vowel', 'blink_vs_wide_eyes', 'head_neck_owned_by_body'}


def test_microexpressions_are_small_spaced_and_asymmetry_needs_a_purpose():
    brief = {'emotions': {'ANGER': 0.6}}
    events, stats = MI.plan(brief, 1, 301, 30, seed=2)
    assert events and stats['max_amplitude'] == 0.25
    assert all(b['frame'] - a['frame'] >= 1.5 * 30 for a, b in zip(events, events[1:]))
    with pytest.raises(ValueError, match='MICRO_AMPLITUDE_INVALID'):
        MI.plan(brief, 1, 301, 30, seed=2, amplitude=0.6)
    with pytest.raises(ValueError, match='ASYMMETRY_INVALID'):
        MI.asymmetry_tracks([{'area': 'brow', 'side': 'L', 'strength': 0.3}], 1, 30, 30)


# ---------------------------------------------------------------- curves and mocap

def test_curve_polish_never_touches_protected_windows():
    track = [[f, (0.3 if f % 2 else 0.25) if f < 20 else (1.0 if 24 <= f <= 26 else 0.0)] for f in range(1, 41)]
    protected = {'lid_close_L': [[22, 28]]}
    analysis = C.analyze({'lid_close_L': track}, 30, protected)
    assert any(f['type'] == 'NOISE' for f in analysis['findings'])
    polished, report = C.polish({'lid_close_L': track}, 30, protected)
    assert report['lid_close_L']['protected_max_deviation'] == 0.0
    assert T.sample(polished['lid_close_L'], 25) == 1.0


def test_mocap_names_map_through_tokens_and_unknown_names_block_retarget():
    assert MO._normalize_name('eyeBlinkLeft') == ('eyeblink', 'L')
    assert MO._normalize_name('mouthPucker') == ('mouthpucker', None)
    assert MO._normalize_name('brow_down.R') == ('browdown', 'R')
    mapping = {k: {'usable': True} for k in ('jaw_open', 'lid_close_L', 'lid_close_R')}
    good = MO.map_channels(MO.load({'fps': 30, 'channels': {'jawOpen': [0, 0.5], 'eyeBlinkLeft': [0, 1], 'eyeBlinkRight': [0, 1]}}), mapping)
    assert not good['blocked']
    bad = MO.map_channels(MO.load({'fps': 30, 'channels': {'chan_01': [0, 0.5], 'chan_02': [0, 1]}}), mapping)
    assert bad['blocked'] and bad['reason'].startswith('MOCAP_MAPPING_INCOMPLETE')


def test_mocap_cleanup_protects_saccades_but_smooths_jitter_and_reports_per_unit():
    jitter = [[f, (1.0 if f % 2 else -1.0)] for f in range(1, 61)]
    saccade = [[f, 0.0 if f < 30 else 12.0] for f in range(1, 61)]
    blink = [[f, 1.0 if 20 <= f <= 22 else 0.0] for f in range(1, 61)]
    cleaned, report = MO.cleanup({'eye_yaw_L': jitter, 'eye_pitch_L': saccade, 'lid_close_L': blink}, 30)
    assert report['channels']['eye_yaw_L']['hf_noise_after'] < 0.3 * report['channels']['eye_yaw_L']['hf_noise_before']
    assert T.sample(cleaned['eye_pitch_L'], 31) == pytest.approx(12.0, abs=0.5)
    assert max(v for _, v in cleaned['lid_close_L']) == 1.0
    assert set(report['by_unit']) == {'degrees', 'normalized'}
