"""A temporal preview must cover every frame authored by its generation plan."""
from copy import deepcopy
import math

import pytest

from blender_mcp.production.animation.generation.window import WINDOW_RUNTIME, validate_pose_window
from blender_mcp.production.common.bridge import build_code


BRIEF = {'type': 'ATTACK', 'frame_start': 1, 'frame_end': 31}


def plan():
    return {'poses': [{'frame': 1, 'semantic': {}}, {'frame': 31, 'semantic': {}}],
            'breakdowns': [{'frame': 15.5, 'semantic': {}}],
            'holds': [{'frame_start': 1, 'frame_end': 2}],
            'phases': [{'frame_start': 1, 'frame_end': 31}],
            'contact_intervals': [{'frame_start': 31, 'frame_end': 31}]}


def test_full_window_fractional_breakdown_and_point_interval_validate_without_mutation():
    data = plan(); before = deepcopy(data)
    result = validate_pose_window(data, BRIEF, 'SPLINE')
    assert result['status'] == 'validated' and result['frame_end'] == 31
    assert result['interval_counts'] == {'holds': 1, 'phases': 1, 'contact_intervals': 1}
    assert data == before


def test_attack_hidden_tail_reproduction_fails_even_when_preview_would_end_at_31():
    data = plan(); data['poses'][-1]['frame'] = 45
    with pytest.raises(ValueError, match='GENERATION_WINDOW_INVALID'):
        validate_pose_window(data, BRIEF, 'ARCS')


def test_pose_plan_cannot_cover_only_a_smaller_window_inside_the_brief():
    data = plan(); data['poses'][0]['frame'] = 2; data['poses'][-1]['frame'] = 30
    with pytest.raises(ValueError, match='endpoints must exactly match'):
        validate_pose_window(data, BRIEF, 'BLOCKING')


def test_outside_interior_key_cannot_hide_between_valid_first_and_last_keys():
    data = plan(); data['poses'].insert(1, {'frame': 45, 'semantic': {}})
    with pytest.raises(ValueError, match=r'poses\[1\]\.frame lies outside'):
        validate_pose_window(data, BRIEF, 'CURVES')


def test_outside_fractional_breakdown_fails_before_keys_or_preview():
    data = plan(); data['breakdowns'][0]['frame'] = 31.01
    with pytest.raises(ValueError, match=r'breakdowns\[0\]\.frame lies outside'):
        validate_pose_window(data, BRIEF, 'BREAKDOWNS')


def test_all_authored_frames_must_be_finite_real_numbers_not_booleans():
    for field in ('poses', 'breakdowns'):
        for value in (True, math.nan, math.inf, -math.inf, '15', None):
            data = plan(); data[field][0]['frame'] = value
            with pytest.raises(ValueError, match='finite numeric frame'):
                validate_pose_window(data, BRIEF, 'SPLINE')


def test_full_plan_remains_required_even_for_a_single_pose_stage():
    for change in ({'poses': []}, {'poses': [{'frame': 1}]}, {'poses': None}, {'poses': ['bad', 'bad']}):
        data = plan(); data.update(change)
        with pytest.raises(ValueError, match='GENERATION_WINDOW_INVALID'):
            validate_pose_window(data, BRIEF, 'POSE', {'pose': {'frame': 1}})


def test_every_declared_interval_must_be_finite_inside_and_not_reversed():
    for field in ('holds', 'phases', 'contact_intervals'):
        for interval in ({'frame_start': 0, 'frame_end': 2}, {'frame_start': 30, 'frame_end': 32},
                         {'frame_start': 20, 'frame_end': 19}, {'frame_start': math.nan, 'frame_end': 31},
                         {'frame_start': 1, 'frame_end': True}, {'frame_start': 1}):
            data = plan(); data[field] = [interval]
            with pytest.raises(ValueError, match='GENERATION_WINDOW_INVALID'):
                validate_pose_window(data, BRIEF, 'OVERLAP')


def test_pose_selection_accepts_actual_endpoint_index_and_fractional_override():
    assert validate_pose_window(plan(), BRIEF, 'POSE', {'pose_index': 1})['selected_pose_frame'] == 31
    assert validate_pose_window(plan(), BRIEF, 'POSE', {'pose': {'frame': 15.5}})['selected_pose_frame'] == 15.5


def test_pose_override_cannot_author_an_unseen_tail_or_nonfinite_frame():
    for override in ({'frame': 45}, {'frame': 0}, {'frame': math.inf}, {}, {'frame': False}, 'bad'):
        with pytest.raises(ValueError, match='GENERATION_WINDOW_INVALID'):
            validate_pose_window(plan(), BRIEF, 'POSE', {'pose': override})


def test_pose_index_must_select_an_existing_row_without_coercion():
    for index in (-1, 2, True, '0', None):
        with pytest.raises(ValueError, match='POSE_INDEX_INVALID'):
            validate_pose_window(plan(), BRIEF, 'POSE', {'pose_index': index})


def test_brief_window_itself_must_be_finite_and_increasing():
    for brief in ({'frame_start': 1, 'frame_end': 1}, {'frame_start': 1, 'frame_end': 0},
                  {'frame_start': True, 'frame_end': 31}, {'frame_start': 1, 'frame_end': math.nan}, {}):
        with pytest.raises(ValueError, match='GENERATION_WINDOW_INVALID'):
            validate_pose_window(plan(), brief, 'SPLINE')


def test_shared_window_runtime_is_self_contained_and_passes_upstream_safe_ast():
    code = build_code(WINDOW_RUNTIME + "\nresult=validate_pose_window(params['plan'],params['brief'],'SPLINE')",
                      {'plan': plan(), 'brief': BRIEF})
    namespace = {}
    exec(code.rsplit('print(', 1)[0], namespace)
    assert namespace['result']['status'] == 'validated'
