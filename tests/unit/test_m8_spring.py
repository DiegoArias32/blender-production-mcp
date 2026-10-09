import math

import pytest

from blender_mcp.production.physics.spring import integrate_offsets, motion_metrics, solve, solve_chain


def norm(v):
    return math.sqrt(sum(x*x for x in v))


def distance(a, b):
    return norm([x-y for x, y in zip(a, b)])


def driver(points, rotate=False):
    return [{'frame': i + 7, 'position': p,
             'quaternion': [math.cos(i*.005), 0, 0, math.sin(i*.005)] if rotate else [1, 0, 0, 0]}
            for i, p in enumerate(points)]


def test_no_motion_introduces_no_noise_or_spontaneous_oscillation():
    offsets, metrics = integrate_offsets([[2, 3, 4]] * 121, 30)
    assert offsets == [[0, 0, 0]] * 121
    assert metrics['max_displacement_m'] == 0
    assert metrics['oscillation_period_s'] is None
    assert metrics['settling_time_s'] is None
    assert metrics['measurement_status']['delay_s'] == 'UNKNOWN'


def test_impulse_excites_motion_then_decays():
    points = [[0, 0, 0]] * 10 + [[.1, 0, 0]] + [[0, 0, 0]] * 290
    offsets, metrics = integrate_offsets(points, 30)
    assert metrics['max_displacement_m'] > .01
    assert norm(offsets[-1]) < 1e-10
    assert metrics['decay_rate_s_inv'] > 0
    assert metrics['oscillation_period_s'] > 0
    assert metrics['settling_time_s'] > 0
    assert metrics['overshoot_m'] > 0
    assert offsets == integrate_offsets(points, 30)[0]


def test_hard_displacement_limit_reports_projections():
    offsets, metrics = integrate_offsets([[0, 0, 0]] * 5 + [[2, 0, 0]] * 30, 30,
                                         {'max_displacement_m': .01})
    assert max(norm(o) for o in offsets) <= .01 + 1e-12
    assert metrics['displacement_clamp_count'] > 0


def test_gravity_is_explicit_and_bounded():
    zero = integrate_offsets([[0, 0, 0]] * 90, 30, {'gravity_influence': 0})[0]
    gravity = integrate_offsets([[0, 0, 0]] * 90, 30, {'gravity_influence': .1})[0]
    assert zero[-1] == [0, 0, 0]
    assert gravity[-1][2] < 0
    assert max(norm(p) for p in gravity) <= .1 + 1e-12


def test_30_and_60_fps_converge_on_the_same_smooth_driver():
    def points(fps):
        # Sinusoid belongs to the test input only, never the secondary generator.
        return [[.03 * (1 - math.cos(2 * math.pi * i / fps)), 0, 0] for i in range(3 * fps + 1)]
    lower = integrate_offsets(points(30), 30, {'substeps': 32})[0]
    higher = integrate_offsets(points(60), 60, {'substeps': 32})[0]
    assert max(distance(a, b) for a, b in zip(lower, higher[::2])) < .004


@pytest.mark.parametrize('points,settings,fps', [
    ([[0, 0, 0]], {}, 30), ([[0, 0, 0]] * 602, {}, 30),
    ([[0, 0, 0], [math.nan, 0, 0]], {}, 30),
    ([[0, 0, 0], [True, 0, 0]], {}, 30),
    ([[0, 0, 0], [1e8, 0, 0]], {}, 30),
    ([[0, 0, 0], [1000, 0, 0]], {}, 30),
    ([[0, 0, 0], [0, 0, 0]], {'mass': .001, 'stiffness': 10000, 'substeps': 1}, 30),
    ([[0, 0, 0], [0, 0, 0]], {'noise': .2}, 30),
    ([[0, 0, 0], [0, 0, 0]], {}, True),
    ([[0, 0, 0], [0, 0, 0]], {}, 24),
])
def test_invalid_or_runaway_inputs_stop_safely(points, settings, fps):
    with pytest.raises(ValueError):
        integrate_offsets(points, fps, settings)


def test_chain_attachment_lengths_and_cumulative_displacement_are_exact():
    points = [[.04 * math.sin(i / 9), 0, 0] for i in range(121)]
    samples = driver(points, rotate=True)
    lengths = [.17, .23, .31]
    result = solve_chain(samples, lengths, 30, {'max_displacement_m': .025, 'max_angle_deg': 7})
    for row, sample in zip(result['positions'], samples):
        assert row[0] == sample['position']
        for j, length in enumerate(lengths):
            assert distance(row[j], row[j+1]) == pytest.approx(length, abs=1e-12)
    assert result['frames'][0] == 7
    assert result['metrics']['max_root_detachment_m'] == 0
    assert result['metrics']['max_displacement_m'] <= .025 + 1e-12
    assert result['metrics']['max_angle_deg'] <= 7 + 1e-8
    assert result['metrics']['max_segment_length_error_m'] < 1e-12
    assert result['tracks'][2]['positions'] == [row[3] for row in result['positions']]


def test_chain_rotation_only_driver_excites_secondary_and_root_never_moves():
    samples = driver([[1, 2, 3]] * 90, rotate=True)
    result = solve_chain(samples, [.3, .3], 30)
    assert result['metrics']['max_displacement_m'] > 0
    assert all(row[0] == [1, 2, 3] for row in result['positions'])


def test_per_bone_world_directions_are_supported_without_guessing_names():
    samples = driver([[0, 0, 0]] * 30)
    for sample in samples:
        sample['directions'] = [[1, 0, 0], [0, 0, -1]]
    result = solve_chain(samples, [.2, .4], 30)
    assert result['positions'][0] == [[0, 0, 0], [.2, 0, 0], [.2, 0, -.4]]
    assert result['metrics']['max_displacement_m'] == 0


def test_chain_hard_angle_limit_reports_clamps():
    result = solve_chain(driver([[0, 0, 0]] * 5 + [[1, 0, 0]] * 30), [.1, .1], 30,
                         {'max_displacement_m': 1, 'max_angle_deg': 1})
    assert result['metrics']['max_angle_deg'] <= 1 + 1e-8
    assert result['metrics']['angle_clamp_count'] > 0


@pytest.mark.parametrize('samples,lengths', [
    (driver([[0, 0, 0]] * 3), []), (driver([[0, 0, 0]] * 3), [0]),
    ([{'frame': 1, 'position': [0, 0, 0]}, {'frame': 3, 'position': [0, 0, 0]}], [.1]),
    ([{'position': [0, 0, 0], 'quaternion': [0, 0, 0, 0]}] * 3, [.1]),
    ([{'position': [0, 0, 0], 'directions': [[0, 0, 0]]}] * 3, [.1]),
    ([{'position': [0, 0, 0], 'directions': [[1, 0, 0]]}] * 3, [.1, .2]),
    ([{'position': [0, 0, 0], 'unknown': 1}] * 3, [.1]),
])
def test_invalid_chain_samples_reject(samples, lengths):
    with pytest.raises(ValueError):
        solve_chain(samples, lengths, 30)


def test_accessory_anchor_stays_attached_and_full_quaternion_roll_is_driven():
    samples = driver([[1, 2, 3]] * 90)
    for i, row in enumerate(samples):
        row['quaternion'] = [math.cos(i*.005), 0, math.sin(i*.005), 0]
    result = solve(samples, {'max_angle_deg': 4}, 30, 'ACCESSORY')
    assert result['positions'] == [row['position'] for row in samples]
    assert result['offsets'] == [[0, 0, 0]] * 90
    assert any(abs(q[2]) > 1e-5 for q in result['rotations'])
    assert 0 < result['metrics']['max_angle_deg'] <= 4 + 1e-8
    assert result['metrics']['max_root_detachment_m'] == 0


def test_accessory_fixed_driver_is_neutral():
    result = solve(driver([[0, 0, 0]] * 61), fps=60, kind='ACCESSORY')
    assert result['rotations'] == [[1, 0, 0, 0]] * 61


def test_soft_offsets_are_separate_bounded_weights_input():
    result = solve(driver([[0, 0, 0]] * 5 + [[.4, 0, 0]] * 60),
                   {'max_displacement_m': .02}, 30, 'SOFT')
    assert max(norm(p) for p in result['offsets']) <= .02 + 1e-12
    assert result['metrics']['kind'] == 'SOFT'


def test_unknown_motion_kind_rejects():
    with pytest.raises(ValueError):
        solve([[0, 0, 0]] * 3, kind='FLUID')


def test_unmeasurable_motion_properties_remain_unknown():
    measured = motion_metrics([[.01, 0, 0]] * 40, 30, [[0, 0, 0]] * 40)
    assert measured['max_displacement_m'] == .01
    assert all(measured[field] is None for field in measured['measurement_status'])
