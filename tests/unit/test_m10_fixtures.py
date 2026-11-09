"""M10 demo fixture contracts without Blender: gait/turn invariants, Action specs, layouts, determinism, geometry.

The Blender builders are qualified by tests/blender/m10_fixtures_probe.py (evaluation in a real process); these tests
only check the pure specifications the builders key, so a broken spec fails here before reaching Blender.
"""
import importlib.util
import math
from pathlib import Path

import pytest

from blender_mcp.production.cinematic import model, planning

SPEC = importlib.util.spec_from_file_location('m10_cinematic_fixtures',
                                              Path(__file__).resolve().parents[1] / 'blender' / 'm10_cinematic_fixtures.py')
FX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FX)


def _boundary_edges(faces):
    counts = {}
    for face in faces:
        for a, b in zip(face, face[1:] + face[:1]):
            edge = tuple(sorted((a, b)))
            counts[edge] = counts.get(edge, 0) + 1
    return [edge for edge, count in counts.items() if count != 2]


def _gaits():
    rows = []
    for spec in FX.humanoid_action_specs('H'):
        if spec.get('gait'):
            rows.append(('humanoid', 1.0, spec))
    for spec in FX.creature_action_specs('C', 1.9):
        if spec.get('gait'):
            rows.append(('asymmetric', 1.9, spec))
    return rows


def test_module_imports_without_blender_and_reuses_the_m6_rows():
    assert 'bpy' not in FX.__dict__
    names = {row[0] for row in FX.m6().bone_definitions('humanoid')}
    assert set(FX.KEYED_BONES) <= names
    explicit = [FX.BONES['root'], FX.BONES['head']] + list(FX.BONES['hands'].values()) + list(FX.BONES['feet'].values())
    assert set(explicit) <= names


@pytest.mark.parametrize('variant,scale,spec', _gaits(), ids=lambda v: v['name'] if isinstance(v, dict) else str(v))
def test_gaits_keep_planted_feet_reachable_and_land_together(variant, scale, spec):
    g = spec['gait']
    rows = FX.gait(g['distance_m'], g['steps'], g['step_frames'], g['lift_m'], g['crouch_m'], g['bob_m'])
    reach = FX.planted_reach(rows, g['step_frames'], g['crouch_m'], g['bob_m'], variant, scale)
    assert reach['reach_margin_m'] > .01  # the IK chain is never asked to over-extend while a foot is planted
    assert rows[0]['feet'] == {'L': 0.0, 'R': 0.0} and rows[-1]['feet'] == {'L': g['distance_m'], 'R': g['distance_m']}
    assert rows[-1]['root'] == pytest.approx(g['distance_m']) and rows[-1]['frame'] == spec['frame_range'][1]
    speeds = [(b['root'] - a['root']) / (b['frame'] - a['frame']) for a, b in zip(rows, rows[1:])]
    assert max(speeds) - min(speeds) < 1e-12  # constant root speed
    for previous, row in zip(rows, rows[1:]):
        moved = [f for f in 'LR' if row['feet'][f] != previous['feet'][f]]
        assert len(moved) <= 1  # one foot at a time
        assert all(row['feet'][f] >= previous['feet'][f] for f in 'LR')  # feet only advance
        if row['phase'] == 'PASSING':
            assert row['lift'] == {row['swing']: g['lift_m']} and row['pelvis_z'] > previous['pelvis_z']


def test_stalk_is_a_cycle_relative_to_the_root():
    stalk = [s for s in FX.creature_action_specs('C', 1.9) if s['name'] == 'C_Stalk'][0]
    first, last = stalk['keys'][0][1], stalk['keys'][-1][1]
    assert stalk['cyclic_relative_to_root']
    for bone in FX.KEYED_BONES:
        a, b = first.get(bone, {}), last.get(bone, {})
        assert a.get('r') == b.get('r')
        ta, tb = a.get('t', [0, 0, 0]), b.get('t', [0, 0, 0])
        if bone in ('root', 'foot_ik.L', 'foot_ik.R'):
            assert tb[1] - ta[1] == pytest.approx(-stalk['root_motion_m']) and tb[2] == ta[2]
        else:
            assert ta == tb


def test_gait_rejects_invalid_inputs():
    with pytest.raises(ValueError, match='M10_FIXTURE_RANGE_INVALID'):
        FX.gait(1.0, 1, 10, .1, .1, .01)
    with pytest.raises(ValueError, match='M10_FIXTURE_RANGE_INVALID'):
        FX.gait(1.0, 4, 9, .1, .1, .01)
    with pytest.raises(ValueError, match='M10_FIXTURE_NUMBER_INVALID'):
        FX.gait(float('nan'), 4, 10, .1, .1, .01)
    with pytest.raises(ValueError, match='M10_FIXTURE_CHOICE_INVALID'):
        FX.gait(1.0, 4, 10, .1, .1, .01, lead='X')


@pytest.mark.parametrize('angle', [90.0, -90.0, 135.0, 180.0, -180.0])
def test_turn_leads_with_the_turn_side_foot_and_ends_on_the_angle(angle):
    rows = FX.turn(angle, 18)
    lead = 'L' if angle > 0 else 'R'
    trail = 'R' if lead == 'L' else 'L'
    assert rows[0]['root_yaw_deg'] == 0 and rows[-1]['root_yaw_deg'] == angle
    assert rows[-1]['feet_yaw_deg'] == {lead: angle, trail: angle} and rows[-1]['head_yaw_deg'] == 0
    assert rows[1]['swing'] == lead and rows[3]['swing'] == trail
    assert rows[2]['feet_yaw_deg'][lead] == angle and rows[2]['feet_yaw_deg'][trail] == 0
    steps = [abs(b['root_yaw_deg'] - a['root_yaw_deg']) for a, b in zip(rows, rows[1:])]
    assert max(steps) <= 45.0  # keys close enough for component-wise quaternion interpolation


@pytest.mark.parametrize('angle', [45.0, 200.0, True])
def test_turn_outside_90_180_is_refused(angle):
    with pytest.raises(ValueError):
        FX.turn(angle, 18)


def _check_specs(specs, expected_suffixes):
    names = [s['name'] for s in specs]
    assert [n.split('_', 1)[1] for n in names] == list(expected_suffixes) and len(set(names)) == len(names)
    ranges = {tuple(s['frame_range']) for s in specs}
    assert len(ranges) == len(specs)  # every Action has its own length
    signatures = set()
    for spec in specs:
        frames = [k[0] for k in spec['keys']]
        assert frames == sorted(set(frames)) and frames[0] == spec['frame_range'][0] == 1
        assert frames[-1] == spec['frame_range'][1]
        for _, pose in spec['keys']:
            assert set(pose) <= set(FX.KEYED_BONES)
            for row in pose.values():
                assert set(row) <= {'t', 'r'}
                assert all(math.isfinite(v) for v in row.get('t', []))
                assert all(axis in ('X', 'Y', 'Z') and math.isfinite(deg) for axis, deg in row.get('r', []))
        signatures.add(repr(spec['keys']))
        assert spec['measure'] in {'STATIC_ROOT', 'ROOT_YAW', 'HEAD_BACK', 'ROOT_MOTION', 'HAND_FORWARD', 'HEAD_YAW',
                                   'HANDS_RAISED', 'HAND_LOWERED'}
        assert (spec['measure'] == 'ROOT_MOTION') == (spec['root_motion_m'] > 0)
    assert len(signatures) == len(specs)  # distinct motion, not renamed copies


def test_humanoid_actions_are_distinct_named_per_actor_and_measurable():
    specs = FX.humanoid_action_specs('Giver', turn_deg=-120.0, reach_side='L')
    _check_specs(specs, ('Idle', 'Turn', 'React', 'Walk', 'Reach'))
    turn = specs[1]
    assert turn['yaw_deg'] == -120.0 and turn['linear_bones'] == ['root']
    assert specs[4]['reach_side'] == 'L' and 'upper_arm.L' in specs[4]['keys'][-1][1]
    with pytest.raises(ValueError, match='M10_FIXTURE_CHOICE_INVALID'):
        FX.humanoid_action_specs('X', reach_side='C')


def test_lower_is_opt_in_and_returns_the_reach_to_rest():
    assert [s['name'] for s in FX.humanoid_action_specs('Taker')][-1] == 'Taker_Reach'
    specs = FX.humanoid_action_specs('Taker', turn_deg=-90.0, lower=True)
    _check_specs(specs, ('Idle', 'Turn', 'React', 'Walk', 'Reach', 'Lower'))
    reach, lower = specs[4], specs[5]
    assert lower['measure'] == 'HAND_LOWERED' and lower['root_motion_m'] == 0.0
    assert lower['keys'][0][1] == reach['keys'][-1][1]  # starts where the reach ends: no pop at the clip boundary
    assert lower['keys'][-1][1] == reach['keys'][0][1]  # ends at rest
    layout = FX.demo_layout('E_interaction')
    assert all(a['lower'] for a in layout['actors'])
    assert not any(a['lower'] for a in FX.demo_layout('B_two_character')['actors'])


def test_creature_actions_are_distinct_and_scale_bounded():
    specs = FX.creature_action_specs('Beast', 2.0)
    _check_specs(specs, ('Emerge', 'Stalk', 'Stare', 'AttackPrep'))
    assert specs[0]['root_motion_m'] > specs[1]['root_motion_m'] > 0 == specs[2]['root_motion_m']
    assert specs[0]['gait']['crouch_m'] == pytest.approx(.14 * 2.0)  # postures scale with the creature
    for bad in (0.5, 3.5, float('inf')):
        with pytest.raises(ValueError):
            FX.creature_action_specs('Beast', bad)


@pytest.mark.parametrize('kind', FX.DEMO_KINDS)
def test_demo_layouts_match_the_task_and_the_cinematic_timeline(kind):
    layout = FX.demo_layout(kind)
    names = [a['name'] for a in layout['actors']]
    assert len(set(names)) == len(names) and all(model.NAME.match(n) for n in names)
    assert all(a['role'] in model.ACTOR_ROLES for a in layout['actors'])
    assert layout['fps'] == 30 and layout['duration_source'] in ('TASK_REQUIREMENT', 'PROPOSAL')
    brief = FX.brief_hint(layout, {n: '0' * 16 for n in names})
    assert brief['schema'] == planning.BRIEF_SCHEMA
    timeline = planning.timeline_for(brief, {'fps': 30, 'fps_base': 1.0})
    assert [timeline['frame_start'], timeline['frame_end']] == layout['frame_range']
    longest = max(max(s['frame_range'][1] for s in (FX.creature_action_specs(a['name'], a['scale'])
                                                     if a['kind'] == 'CREATURE' else FX.humanoid_action_specs(a['name'])))
                  for a in layout['actors'])
    assert longest <= layout['frame_range'][1]  # every Action fits inside the demo without retiming


def test_demo_contents_follow_the_brief():
    a = FX.demo_layout('A_creature_reveal')
    assert a['duration_s'] == 15.0 and a['duration_source'] == 'TASK_REQUIREMENT' and a['frame_range'] == [1, 450]
    assert [x['kind'] for x in a['actors']] == ['CREATURE'] and a['environment']['cave'] and a['environment']['lantern']
    assert a['actors'][0]['location'][1] > 3.0  # starts inside the dark zone (cave mouth at y = 3)
    b = FX.demo_layout('B_two_character')
    assert [x['kind'] for x in b['actors']] == ['HUMANOID', 'HUMANOID']
    assert b['actors'][0]['turn_deg'] > 0 > b['actors'][1]['turn_deg']  # they turn towards each other
    c = FX.demo_layout('C_body_face_physics')
    assert c['actors'][0]['face'] and c['actors'][0]['secondary'] == 'pendant'
    d = FX.demo_layout('D_editorial')
    assert {row['shot'] for row in d['coverage_proposal']} == {'WIDE', 'MEDIUM', 'CLOSE_UP', 'REACTION'}
    assert all(x['face'] for x in d['actors'])
    e = FX.demo_layout('E_interaction')
    held = e['props'][0]['held_by']
    giver = [x for x in e['actors'] if x['name'] == held['actor']][0]
    taker = [x for x in e['actors'] if x['name'] != held['actor']][0]
    # The prop is in the hand facing the taker (+X is the left of an actor facing -Y).
    assert (held['side'] == 'L') == (taker['location'][0] > giver['location'][0])
    with pytest.raises(ValueError, match='M10_FIXTURE_DEMO_UNKNOWN'):
        FX.demo_layout('F_unknown')


def test_frame_range_uses_the_planning_rounding():
    assert FX.frame_range(15.0) == [1, 450]
    assert FX.frame_range(0.51, fps=24) == [1, 12]
    assert FX.frame_range(1.0, frame_start=101) == [101, 130]
    with pytest.raises(ValueError):
        FX.frame_range(0.0)


def test_rock_jitter_is_deterministic_and_bounded():
    values = [FX.rock_jitter(i, s) for s in range(1, 6) for i in range(42)]
    assert values == [FX.rock_jitter(i, s) for s in range(1, 6) for i in range(42)]
    assert all(.82 <= v <= 1.18 for v in values) and len(set(values)) > 150


@pytest.mark.parametrize('kind', ['box', 'lantern'])
def test_props_are_closed_with_the_grip_at_the_origin_and_the_body_below_the_hand(kind):
    geometry = FX.prop_geometry(.25, kind)
    assert not _boundary_edges(geometry['faces']) and len(geometry['faces']) == 24
    assert len(geometry['materials']) == len(geometry['faces']) and set(geometry['materials']) == {0, 1}
    assert geometry['grip'] == [0.0, 0.0, 0.0]
    top = geometry['body_center'][2] + geometry['body_half'][2]
    assert top <= -.10 + 1e-12  # below the M6 fingertips (~6 cm under the grip)
    with pytest.raises(ValueError):
        FX.prop_geometry(.25, 'sword')


def test_leg_geometry_comes_from_the_m6_rows():
    legs = FX.leg_geometry('humanoid')
    assert legs['hip_height_m'] == pytest.approx(.92)
    assert legs['leg_length_m'] == pytest.approx(math.hypot(.055, .48) + math.hypot(.055, .44))
    assert FX.leg_geometry('asymmetric', 2.0)['leg_length_m'] == pytest.approx(2 * legs['leg_length_m'])
