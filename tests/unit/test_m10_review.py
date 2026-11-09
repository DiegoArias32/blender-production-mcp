"""M10 area G without Blender: payload budgets and safe mode, review gate, acceptance gates, refinement validation
and metrics, quality-report aggregation (a missing provider is NOT_RUN, never PASS) and the local HTML artefacts."""
from __future__ import annotations

import ast
import math
import struct
import sys
import types
import zlib

import pytest

from blender_mcp.production.animation.generation.render import write_apng
from blender_mcp.production.cinematic import model, review, store
from blender_mcp.production.cinematic.service import HANDLERS, dispatch, load_handlers
from blender_mcp.production.cinematic.tools import READ_ONLY, TRANSIENT
from blender_mcp.production.common.bridge import build_code, payload_bytes, compose_code
from blender_mcp.production.common.storage import Storage

TOOLS = ('cinematic_generate_previs', 'cinematic_render_preview', 'cinematic_review', 'cinematic_refine',
         'cinematic_accept', 'cinematic_quality_report')
ACTOR = 'a' * 16
PARAMS = {'source_scene': 'Scene', 'scene': 'M10C_x_v1', 'collections': {'src': 'a', 'cin': 'b'}, 'generation_id': 'g',
          'version': 1, 'frames': list(range(1, 601)), 'actors': [[ACTOR, ['Body']]], 'width': 480, 'height': 270,
          'directory': 'C:/x', 'stats': True, 'changes': [{'kind': 'exposure', 'exposure': 0.5}] * 8,
          'captured': [{'kind': 'exposure', 'before': 0.0, 'after': 0.5}] * 8, 'actor_objects': ['Rig'],
          'owned_prefix': 'M10C_g_v1', 'lifecycle': 'ACCEPTED', 'review_reference': 'r', 'dry_run': False}


# ------------------------------------------------------------------------------------------------ catalogue, payloads

def test_area_g_tools_are_registered_writers():
    load_handlers()
    assert set(TOOLS) <= set(HANDLERS)
    assert not set(TOOLS) & (READ_ONLY | TRANSIENT)


@pytest.mark.parametrize('name', sorted(review.PAYLOADS))
def test_payloads_pass_safe_mode_within_budget(name):
    body = review.PAYLOADS[name]
    code = build_code(body, PARAMS)  # raises on any safe-mode violation
    assert payload_bytes(body, PARAMS) < 140_000
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 14_000


@pytest.mark.parametrize('name', sorted(review.PAYLOADS))
def test_payloads_avoid_forbidden_constructs(name):
    linked = compose_code(review.PAYLOADS[name], PARAMS)
    for forbidden in ('lambda', '__class__', 'bpy_extras', 'numpy', 'import os', 'import time', 'hashlib',
                      'hasattr(bpy.types', 'driver_add(', 'render.opengl'):
        assert forbidden not in linked, forbidden
    for node in ast.walk(ast.parse(linked)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('getattr', 'setattr', 'hasattr'):
            assert isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)


def test_render_payload_uses_frame_set_markers_and_restores():
    render = review.RUNTIME
    # Cuts are the markers: frame_set inside the candidate override, never marker.camera = None.
    assert 'scene.frame_set(frame)' in render and 'temp_override(scene=scene)' in render
    assert 'marker.camera = None' not in render and 'm.camera = None' not in render
    # temp_override(scene=candidate) without scene=: render(scene=name) renders the WINDOW scene's frame (measured,
    # docs/cinematic-architecture.md section 8).
    assert "bpy.ops.render.render(write_still=True)" in render and 'render.render(write_still=True, scene=' not in render
    assert 'cr_end(scene, begin)' in render and 'cm_fingerprints(params)' in render
    assert "'BLENDER_WORKBENCH'" in render and 'CYCLES' not in render.split('def cr_render(')[1].split('def ')[0]


# ------------------------------------------------------------------------------------------------ review gate

RENDER = {'status': 'ok', 'temporal_complete': True, 'output_dir': 'C:/store/preview-1', 'candidate_fingerprint': 'F',
          'shots': [{'shot_id': 'S1'}, {'shot_id': 'S2'}]}
STATE = {'fingerprints': {'candidate': 'F', 'source': 'S'}}


def full_checklist(status='OK'):
    return {key: {'status': status, 'note': 'n'} for key, _ in review.DIRECTION_CHECKLIST}


def good_review(**changes):
    value = {'reviewer': 'Diego', 'artistic_observations': ['the cut lands on the reaction'], 'decision': 'ACCEPT',
             'artifact_reference': RENDER['output_dir'], 'temporal_reviewed': True, 'shots_reviewed': ['S1', 'S2'],
             'speeds_reviewed': [1, 0.25], 'direction_checklist': full_checklist()}
    value.update(changes)
    return value


def test_review_gate_accepts_a_complete_review_and_keeps_sections_separate():
    record = review.review_gate(good_review(technical_findings=['foot slides at 31']), RENDER, STATE)
    assert record['decision'] == 'ACCEPT' and record['checklist_complete'] and not record['synthetic_review']
    assert record['artistic_observations'] == ['the cut lands on the reaction']
    assert record['technical_findings'] == ['foot slides at 31'] and record['candidate_fingerprint'] == 'F'
    assert review.review_gate(good_review(artistic_observations='one line'), RENDER, STATE)['artistic_observations'] == ['one line']


@pytest.mark.parametrize('extra', [{'score': 9}, {'Rating': 'A'}, {'aaa_score': 1},
                                   {'direction_checklist': {'mood': {'status': 'OK', 'note': '', 'quality_score': 3}}},
                                   {'technical_findings': [{'quality_score': 2}]}])
def test_review_gate_refuses_any_score(extra):
    with pytest.raises(ValueError, match='^VISUAL_REVIEW_SCORE_FORBIDDEN'):
        review.review_gate(good_review(**extra), RENDER, STATE)


@pytest.mark.parametrize('changes, render, state, code', [
    ({'artifact_reference': 'C:/store/previs-1'}, RENDER, STATE, 'VISUAL_REVIEW_STALE'),
    ({}, dict(RENDER, candidate_fingerprint='OLD'), STATE, 'VISUAL_REVIEW_STALE'),
    ({}, RENDER, {'fingerprints': {'candidate': 'NEW'}}, 'VISUAL_REVIEW_STALE'),
    ({'temporal_reviewed': False}, RENDER, STATE, 'VISUAL_REVIEW_STALE'),
    ({}, dict(RENDER, temporal_complete=False), STATE, 'TEMPORAL_REVIEW_REQUIRED'),
    ({}, None, STATE, 'TEMPORAL_REVIEW_REQUIRED'),
    ({'shots_reviewed': ['S1']}, RENDER, STATE, 'VISUAL_REVIEW_INCOMPLETE'),
    ({'speeds_reviewed': [1]}, RENDER, STATE, 'VISUAL_REVIEW_INCOMPLETE'),
    ({'speeds_reviewed': [True, 0.25]}, RENDER, STATE, 'VISUAL_REVIEW_INCOMPLETE'),
    ({'artistic_observations': []}, RENDER, STATE, 'VISUAL_REVIEW_INVALID'),
    ({'decision': 'MAYBE'}, RENDER, STATE, 'VISUAL_REVIEW_INVALID'),
    ({'unknown': 1}, RENDER, STATE, 'VISUAL_REVIEW_INVALID'),
    ({'direction_checklist': {'mood': {'status': 'OK', 'note': ''}}}, RENDER, STATE, 'VISUAL_REVIEW_CHECKLIST_REQUIRED'),
    ({'direction_checklist': None}, RENDER, STATE, 'VISUAL_REVIEW_CHECKLIST_REQUIRED'),
    ({'direction_checklist': {'mood': {'status': 'ISSUE', 'note': ' '}}}, RENDER, STATE, 'VISUAL_REVIEW_INVALID'),
    ({'direction_checklist': {'not_an_item': {'status': 'OK'}}}, RENDER, STATE, 'VISUAL_REVIEW_INVALID'),
])
def test_review_gate_refusals(changes, render, state, code):
    with pytest.raises(ValueError, match='^' + code):
        review.review_gate(good_review(**changes), render, state)


def test_partial_checklist_is_allowed_for_reject_and_never_auto_filled():
    record = review.review_gate(good_review(decision='REJECT', direction_checklist={'mood': {'status': 'ISSUE', 'note': 'flat'}}),
                                RENDER, STATE)
    assert not record['checklist_complete']
    assert record['direction_checklist']['mood']['status'] == 'ISSUE'
    assert record['direction_checklist']['rhythm']['status'] == 'NOT_FILLED'
    template = review.checklist_template()
    assert len(template) == 13 and {row['status'] for row in template.values()} == {'NOT_FILLED'}


def test_synthetic_label_is_flagged():
    record = review.review_gate(good_review(reviewer=review.SYNTHETIC_LABEL), RENDER, STATE)
    assert record['synthetic_review'] is True


# ------------------------------------------------------------------------------------------------ acceptance gates

def accepted_state(**changes):
    state = {'state': 'REVIEWED', 'fingerprints': {'candidate': 'F', 'source': 'S'},
             'render': {'temporal_complete': True, 'candidate_fingerprint': 'F', 'output_dir': 'D'},
             'review': {'decision': 'ACCEPT', 'artifact_reference': 'D', 'candidate_fingerprint': 'F'},
             'quality_report': {'passed': True, 'candidate_fingerprint': 'F'}}
    state.update(changes)
    return state


@pytest.mark.parametrize('changes, code', [
    ({}, None),
    ({'pending_refinement': {'phase': 'REVIEW_REQUIRED'}}, 'REFINEMENT_REVIEW_REQUIRED'),
    ({'state': 'ASSEMBLED'}, 'CINEMATIC_STATE_INVALID'),
    ({'render': None}, 'TEMPORAL_REVIEW_REQUIRED'),
    ({'review': {'decision': 'REFINE', 'artifact_reference': 'D', 'candidate_fingerprint': 'F'}}, 'CINEMATIC_ACCEPT_REVIEW_REQUIRED'),
    ({'review': {'decision': 'ACCEPT', 'artifact_reference': 'OLD', 'candidate_fingerprint': 'F'}}, 'VISUAL_REVIEW_STALE'),
    ({'quality_report': None}, 'CINEMATIC_QUALITY_REPORT_REQUIRED'),
    ({'quality_report': {'passed': True, 'candidate_fingerprint': 'OLD'}}, 'CINEMATIC_QUALITY_REPORT_REQUIRED'),
    ({'quality_report': {'passed': False, 'candidate_fingerprint': 'F'}}, 'CINEMATIC_QUALITY_REPORT_NOT_PASSED'),
])
def test_accept_gates(changes, code):
    failures = review.accept_gates(accepted_state(**changes))
    if code is None:
        assert failures == []
    else:
        assert failures and failures[0].startswith(code)


# ------------------------------------------------------------------------------------------------ handler gates (no Blender)

class NoBridge:
    def run(self, body, params=None):
        raise AssertionError('Blender must not be contacted before the gate')


def saved_cinematic(tmp_path, state):
    storage = Storage(tmp_path)
    plan = {'schema': model.SCHEMA, 'actors': [{'actor_id': ACTOR, 'object': 'Rig', 'meshes': ['Body']}],
            'source': {'scene': 'Scene', 'fingerprint': 'S'}, 'asset': {'path': None}, 'shots': [],
            'backend_fingerprint': store.backend_fingerprint()}
    reference, _ = store.save(storage, 'op', plan)
    store.record(reference, dict({'refinement_cycles': 0, 'fingerprints': {'source': 'S', 'candidate': 'F'}}, **state))
    return storage, reference


REFINE = {'target': 'actor_screen_height', 'goal': 0.5, 'minimum_improvement': 0.01,
          'changes': [{'kind': 'camera_settings', 'camera': 'Cam', 'settings': {'lens': 50}}]}


@pytest.mark.parametrize('state, options, code', [
    ({'state': 'ASSEMBLED'}, dict(REFINE, target='aaa_score'), 'REFINEMENT_TARGET_UNSUPPORTED'),
    ({'state': 'ASSEMBLED'}, dict(REFINE, goal=None), 'CINEMATIC_NUMBER_INVALID'),
    ({'state': 'ASSEMBLED', 'refinement_cycles': 3}, REFINE, 'REFINEMENT_LIMIT'),
    ({'state': 'ASSEMBLED', 'pending_refinement': {'phase': 'REVIEW_REQUIRED'}}, REFINE, 'REFINEMENT_REVIEW_REQUIRED'),
    ({'state': 'CINEMATIC_CANDIDATE'}, REFINE, 'CINEMATIC_STATE_INVALID'),
    ({'state': 'ACCEPTED'}, REFINE, 'GENERATION_TERMINAL'),
    ({'state': 'REVIEWED'}, dict(REFINE, minimum_improvement=0), 'REFINEMENT_THRESHOLD_INVALID'),
    ({'state': 'REVIEWED'}, dict(REFINE, unknown=1), 'CINEMATIC_OPTION_UNSUPPORTED'),
])
def test_refine_refuses_before_spending_a_cycle_or_touching_blender(tmp_path, state, options, code):
    storage, reference = saved_cinematic(tmp_path, state)
    with pytest.raises(ValueError, match='^' + code):
        dispatch('cinematic_refine', NoBridge(), storage, 'op-r', cinematic=reference, options=options, apply=True)
    _, _, after = store.load(storage, reference)
    assert after.get('refinement_cycles', 0) == state.get('refinement_cycles', 0)


@pytest.mark.parametrize('tool, state, options, code', [
    ('cinematic_accept', {'state': 'REVIEWED'}, {}, 'TEMPORAL_REVIEW_REQUIRED'),
    ('cinematic_accept', {'state': 'ACCEPTED'}, {}, 'GENERATION_TERMINAL'),
    ('cinematic_accept', {'state': 'REVIEWED'}, {'force': True}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ('cinematic_review', {'state': 'CINEMATIC_CANDIDATE'}, {'review': {}}, 'CINEMATIC_STATE_INVALID'),
    ('cinematic_review', {'state': 'ASSEMBLED', 'pending_refinement': {'phase': 'MEASURING'}}, {'review': {}}, 'REFINEMENT_INCOMPLETE'),
    ('cinematic_review', {'state': 'ASSEMBLED'}, {'review': good_review(score=1)}, 'VISUAL_REVIEW_SCORE_FORBIDDEN'),
    ('cinematic_review', {'state': 'ASSEMBLED'}, {'review': good_review()}, 'TEMPORAL_REVIEW_REQUIRED'),
    ('cinematic_render_preview', {'state': 'ACCEPTED'}, {}, 'GENERATION_TERMINAL'),
    ('cinematic_render_preview', {'state': 'ASSEMBLED'}, {'views': ['FRONT']}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ('cinematic_generate_previs', {'state': 'REJECTED'}, {}, 'GENERATION_TERMINAL'),
    ('cinematic_quality_report', {'state': 'ASSEMBLED', 'pending_refinement': {'phase': 'REVIEW_REQUIRED'}}, {},
     'REFINEMENT_REVIEW_REQUIRED'),
    ('cinematic_quality_report', {'state': 'ASSEMBLED'}, {'section_options': {'nope': {}}}, 'CINEMATIC_OPTION_UNSUPPORTED'),
])
def test_handler_gates_without_blender(tmp_path, tool, state, options, code):
    storage, reference = saved_cinematic(tmp_path, state)
    with pytest.raises(ValueError, match='^' + code):
        dispatch(tool, NoBridge(), storage, 'op-x', cinematic=reference, options=options, apply=True)


class Uncertain(RuntimeError):
    uncertain = True


def test_uncertain_failure_during_refinement_blocks_the_plan_without_reverting(tmp_path):
    storage, reference = saved_cinematic(tmp_path, {'state': 'ASSEMBLED'})
    path, _, state = store.load(storage, reference)
    with pytest.raises(Uncertain):
        review._block_if_uncertain(path, state, 'refine_preview', Uncertain('timeout'))
    _, _, after = store.load(storage, reference)
    assert after['uncertain_operation'] is True and after['uncertain_step'] == 'refine_preview'
    review._block_if_uncertain(path, after, 'refine_preview', ValueError('X: certain failure'))  # certain: no-op


def test_uncertain_render_is_recorded_but_does_not_block(tmp_path):
    storage, reference = saved_cinematic(tmp_path, {'state': 'ASSEMBLED'})
    path, _, state = store.load(storage, reference)
    review._note_render_error(path, state, 'render_preview', Uncertain('timeout'))
    _, _, after = store.load(storage, reference)
    assert after['last_render_error']['uncertain'] is True and not after.get('uncertain_operation')


# ------------------------------------------------------------------------------------------------ refinement validation

PLAN = {'actors': [{'actor_id': ACTOR, 'object': 'Rig'}]}


@pytest.mark.parametrize('options, code', [
    ({'target': 'headroom_error', 'changes': [{'kind': 'exposure', 'exposure': 1}]}, 'REFINEMENT_GOAL_REQUIRED'),
    ({'target': 'camera_max_jerk', 'goal': 1, 'changes': [{'kind': 'exposure', 'exposure': 1}]}, 'REFINEMENT_GOAL_UNSUPPORTED'),
    ({'target': 'camera_max_jerk', 'scope': {'shot': 'S1'}, 'changes': [{'kind': 'exposure', 'exposure': 1}]}, 'REFINEMENT_SCOPE_INVALID'),
    ({'target': 'camera_max_jerk', 'scope': {'actor': 'b' * 16}, 'changes': [{'kind': 'exposure', 'exposure': 1}]}, 'ACTOR_NOT_IN_CINEMATIC'),
    ({'target': 'camera_max_jerk', 'scope': {'frame_start': 9, 'frame_end': 2}, 'changes': [{'kind': 'exposure', 'exposure': 1}]}, 'REFINEMENT_SCOPE_INVALID'),
    ({'target': 'camera_max_jerk', 'changes': []}, 'REFINEMENT_CHANGES_REQUIRED'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'exposure', 'exposure': 1}] * 9}, 'REFINEMENT_CHANGES_REQUIRED'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'paint'}]}, 'CINEMATIC_CHOICE_INVALID'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'camera_settings', 'camera': 'C', 'settings': {'fov': 1}}]}, 'REFINEMENT_CHANGE_INVALID'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'camera_settings', 'camera': 'C', 'settings': {'lens': True}}]}, 'CINEMATIC_NUMBER_INVALID'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'camera_settings', 'camera': 'C', 'settings': {'lens': 0.5}}]}, 'CINEMATIC_RANGE_INVALID'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'exposure', 'exposure': 1}, {'kind': 'exposure', 'exposure': 2}]}, 'REFINEMENT_CHANGE_DUPLICATE'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'actor_offset', 'actor': 'b' * 16, 'offset': [0, 0, 1]}]}, 'ACTOR_NOT_IN_CINEMATIC'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'clip_shift', 'actor': ACTOR, 'track': 'T', 'strip': 'S', 'frames': 0}]}, 'REFINEMENT_CHANGE_INVALID'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'light_energy', 'light': 'L', 'energy': float('nan')}]}, 'CINEMATIC_NUMBER_INVALID'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'exposure', 'exposure': 1, 'extra': 2}]}, 'REFINEMENT_CHANGE_INVALID'),
    ({'target': 'camera_max_jerk', 'changes': [{'kind': 'camera_offset', 'camera': ' C', 'offset': [0, 0, 1]}]}, 'CINEMATIC_NAME_INVALID'),
])
def test_refine_request_validation(options, code):
    with pytest.raises(ValueError, match='^' + code):
        review.refine_request(options, PLAN)


def test_refine_request_normalizes_changes_and_resolves_actor_objects():
    request = review.refine_request({'target': 'actor_screen_height', 'goal': 0.6, 'scope': {'camera': 'CamA'}, 'changes': [
        {'kind': 'camera_settings', 'camera': 'CamA', 'settings': {'lens': 50, 'shift_x': 0}},
        {'kind': 'actor_offset', 'actor': ACTOR, 'offset': [1, 0, 0]},
        {'kind': 'clip_shift', 'actor': ACTOR, 'track': 'M10C_g_v1_BODY', 'strip': 'walk', 'frames': -3}]}, PLAN)
    assert request['direction'] == 'lower' and request['source'] == 'BUILTIN' and request['goal'] == 0.6
    assert request['changes'][0]['settings'] == {'lens': 50.0, 'shift_x': 0.0}
    assert request['changes'][1]['object'] == 'Rig' and request['changes'][2]['frames'] == -3


@pytest.mark.parametrize('value', [0, -0.1, float('nan'), float('inf'), True, '0.1', None])
def test_minimum_improvement_must_be_positive_and_finite(value):
    with pytest.raises(ValueError, match='^REFINEMENT_THRESHOLD_INVALID'):
        review.minimum_improvement(value)


def test_improvement_direction():
    assert review.improvement('lower', 0.3, 0.1) == pytest.approx(0.2)
    assert review.improvement('higher', 0.5, 0.8) == pytest.approx(0.3)
    assert review.improvement('lower', 0.1, 0.3) < 0


# ------------------------------------------------------------------------------------------------ metrics

def stats(height, top, visible=1.0, in_front=True):
    row = {'in_front': in_front, 'visible_fraction': visible}
    if in_front:
        row.update(height=height, top=top)
    return row


ROWS = [[1, 'A', [0, 0, 0], {ACTOR: stats(0.40, 0.70), 'b' * 16: stats(0.10, 0.20, 0.5)}],
        [2, 'A', [0, 0, 0], {ACTOR: stats(0.50, 0.95, 0.75)}],
        [3, 'B', [0, 0, 0], {ACTOR: stats(0.90, 0.99, 0.25)}],
        [4, 'B', [0, 0, 0], {ACTOR: stats(None, None, 0.0, in_front=False)}],
        [5, None, None, {}]]


def test_screen_height_error_is_the_worst_frame_in_scope():
    result = review.compute_metric('actor_screen_height', 0.5, {'camera': 'A', 'actor': ACTOR}, ROWS, 30)
    assert result['value'] == pytest.approx(0.1) and result['worst_frame'] == 1 and result['samples'] == 2
    everything = review.compute_metric('actor_screen_height', 0.5, {}, ROWS, 30)
    assert everything['value'] == pytest.approx(0.4) and everything['worst_frame'] in (1, 3)
    assert everything['source'] == 'MEASURED_IN_BLENDER' and everything['units'] == 'frame heights'


def test_headroom_and_visibility_metrics():
    # headroom = 1 - top: frame 1 has 0.30 against a goal of 0.10, the worst error (0.20).
    headroom = review.compute_metric('headroom_error', 0.1, {'actor': ACTOR}, ROWS, 30)
    assert headroom['value'] == pytest.approx(0.2) and headroom['worst_frame'] == 1
    visibility = review.compute_metric('visibility_min_fraction', None, {'actor': ACTOR}, ROWS, 30)
    assert visibility['value'] == 0.0 and visibility['worst_frame'] == 4  # behind the camera still counts as invisible


def test_metric_without_samples_is_unavailable_not_zero():
    with pytest.raises(ValueError, match='^REFINEMENT_METRIC_UNAVAILABLE'):
        review.compute_metric('actor_screen_height', 0.5, {'camera': 'Z'}, ROWS, 30)


def test_camera_jerk_is_exact_for_a_cubic_and_ignores_cuts():
    fps, a = 24.0, 0.7
    rows = [[f, 'A', [a * (f / fps) ** 3, 0.0, 0.0], {}] for f in range(1, 11)]
    # A cut to a camera 100 m away must not create a jerk spike: jerk is measured inside each camera run.
    rows += [[f, 'B', [100.0 + a * (f / fps) ** 3, 0.0, 0.0], {}] for f in range(11, 21)]
    result = review.camera_jerk(rows, fps)
    assert result['value'] == pytest.approx(6 * a, rel=1e-6) and result['samples'] == 14
    with pytest.raises(ValueError, match='^REFINEMENT_METRIC_UNAVAILABLE'):
        review.camera_jerk(rows[:3], fps)


# ------------------------------------------------------------------------------------------------ quality report

def test_normalize_section_never_passes_without_an_explicit_verdict():
    assert review.normalize_section({'passed': True, 'findings': []})['status'] == 'PASS'
    assert review.normalize_section({'passed': True, 'errors': ['x']})['status'] == 'FAIL'
    assert review.normalize_section({'passed': False, 'changed': [{'item': 'mesh'}]})['errors'] == ["changed: {\"item\": \"mesh\"}"]
    assert review.normalize_section({'status': 'success', 'findings': [{'severity': 'warning'}]})['status'] == 'PASS'
    assert review.normalize_section({'status': 'success', 'findings': [{'severity': 'ERROR'}]})['status'] == 'FAIL'
    assert review.normalize_section({'status': 'success'})['status'] == 'INDETERMINATE'
    assert review.normalize_section({})['status'] == 'INDETERMINATE'
    assert review.normalize_section({'status': 'proposal', 'passed': True})['status'] == 'NOT_RUN'
    assert review.normalize_section({'status': 'not_applicable'})['status'] == 'NOT_APPLICABLE'
    assert review.normalize_section(['x'])['status'] == 'ERROR'


@pytest.mark.parametrize('sections, passed', [
    ([('a', True, 'PASS'), ('b', False, 'NOT_RUN')], True),
    ([('a', True, 'PASS'), ('b', True, 'NOT_RUN')], False),
    ([('a', True, 'PASS'), ('b', True, 'INDETERMINATE')], False),
    ([('a', True, 'PASS'), ('b', False, 'FAIL')], False),
    ([('a', True, 'NOT_APPLICABLE'), ('b', False, 'INDETERMINATE')], True),
    ([('a', True, 'ERROR')], False),
])
def test_report_verdict(sections, passed):
    rows = [{'section': s, 'mandatory': m, 'status': st} for s, m, st in sections]
    assert review.report_verdict(rows)[0] is passed
    findings = review.technical_findings(rows)
    assert all(f['severity'] in ('error', 'info') for f in findings)
    assert passed or any(f['severity'] == 'error' for f in findings)


class FakeCtx:
    bridge = NoBridge()
    storage = None
    operation_id = 'op'


@pytest.fixture
def fake_providers(monkeypatch):
    module = types.ModuleType('blender_mcp.production.cinematic.zz_fake_provider')

    def good(ctx, plan, state, options=None):
        return {'passed': True, 'findings': [], 'metrics': {'m': 1.5, 'nested': {'x': 1}}}

    def needs_other(ctx, plan, mystery):
        return {'passed': True}

    def refuses(ctx, state):
        raise ValueError('CINEMATIC_EVENTS_REQUIRED: nothing planned')

    def positional(state, /):
        return {'passed': state.get('ok', False), 'findings': []}

    module.good, module.needs_other, module.refuses, module.positional = good, needs_other, refuses, positional
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


def spec(providers, mandatory='always', **extra):
    return dict({'id': 'x', 'label': 'x', 'providers': providers, 'handler': None, 'mandatory': mandatory}, **extra)


def test_missing_module_or_function_is_not_run_never_pass(fake_providers):
    for providers in ((('zz_missing_module', 'report'),), (('zz_fake_provider', 'absent'),),
                      (('zz_fake_provider', 'needs_other'),)):
        section = review.run_section(FakeCtx(), 'p', {}, {}, spec(providers))
        assert section['status'] == 'NOT_RUN' and section['reason'].startswith('PROVIDER_UNAVAILABLE')


def test_first_usable_provider_wins_and_metrics_stay_scalar(fake_providers):
    section = review.run_section(FakeCtx(), 'p', {}, {}, spec((('zz_fake_provider', 'needs_other'),
                                                                ('zz_fake_provider', 'good'))))
    assert section['status'] == 'PASS' and section['provider'] == 'zz_fake_provider.good'
    assert section['metrics'] == {'m': 1.5}
    assert review.run_section(FakeCtx(), 'p', {}, {'ok': True}, spec((('zz_fake_provider', 'positional'),)))['status'] == 'PASS'


def test_provider_refusal_blocks_only_mandatory_sections(fake_providers):
    mandatory = review.run_section(FakeCtx(), 'p', {}, {}, spec((('zz_fake_provider', 'refuses'),)))
    optional = review.run_section(FakeCtx(), 'p', {'actors': [1]}, {}, spec((('zz_fake_provider', 'refuses'),), 'multi_actor'))
    assert mandatory['status'] == 'ERROR' and optional['status'] == 'NOT_RUN'
    assert optional['reason'].startswith('PROVIDER_REFUSED: CINEMATIC_EVENTS_REQUIRED')


def test_recorded_section_is_used_only_while_current():
    # Same rule and recorded key as the real lighting section, without importing the lighting module.
    lighting = dict(review.SECTION_BY_ID['lighting'], providers=())
    assert review.SECTION_BY_ID['lighting']['recorded'] == ('qa', 'lighting')
    row = {'passed': True, 'errors': 0, 'warnings': 2, 'candidate_revision': 7}
    current = review.run_section(FakeCtx(), 'p', {}, {'revision': 7, 'lighting': {'plan': 1}, 'qa': {'lighting': row}}, lighting)
    stale = review.run_section(FakeCtx(), 'p', {}, {'revision': 8, 'lighting': {'plan': 1}, 'qa': {'lighting': row}}, lighting)
    assert current['status'] == 'PASS' and current['mandatory'] is True
    assert stale['status'] == 'NOT_RUN' and 'stale' in stale['reason']
    failed = review.run_section(FakeCtx(), 'p', {}, {'revision': 7, 'qa': {'lighting': dict(row, passed=False)}}, lighting)
    assert failed['status'] == 'FAIL' and failed['mandatory'] is False


def test_render_section_checks_the_current_preview():
    state = {'fingerprints': {'candidate': 'F'}}
    good = {'candidate_fingerprint': 'F', 'temporal_complete': True, 'state_restored': True, 'cuts_respected': True}
    assert review.render_section(state)['status'] == 'NOT_RUN'
    assert review.render_section(dict(state, render=good))['status'] == 'PASS'
    assert review.render_section(dict(state, render=dict(good, candidate_fingerprint='OLD')))['status'] == 'FAIL'
    assert review.render_section(dict(state, render=dict(good, cuts_respected=False)))['status'] == 'FAIL'


def test_mandatory_rules_follow_what_the_cinematic_uses():
    plan = {'actors': [1, 2]}
    state = {'actors': {'a': {'clips': [1], 'facial': [], 'props': []}}, 'lighting': {'plan': {}}}
    rule = {s['id']: review._mandatory(s, plan, state) for s in review.SECTIONS}
    assert rule == {'integrity': True, 'staging': True, 'sync': True, 'contacts': False, 'facial_timing': False,
                    'secondary': False, 'continuity': True, 'framing': True, 'visibility': True, 'motion': True,
                    'lighting': True, 'render': True}
    assert review._mandatory(review.SECTION_BY_ID['staging'], {'actors': [1]}, {}) is False


# ------------------------------------------------------------------------------------------------ pure helpers and artefacts

def test_render_size_keeps_the_candidate_aspect():
    assert review.render_size(480, [1920, 1080, 100]) == (480, 270)
    assert review.render_size(400, [1080, 1920, 50]) == (225, 400)
    with pytest.raises(ValueError, match='^CINEMATIC_RANGE_INVALID'):
        review.render_size(32, [1920, 1080, 100])


def test_marker_camera_rule_and_segments():
    markers = [[10, 'CUT_B', 'CamB'], [1, 'CUT_A', 'CamA'], [20, 'note', None]]
    assert review.expected_camera(markers, -5) == 'CamA'  # before the first marker: first marker's camera (measured)
    assert review.expected_camera(markers, 9) == 'CamA' and review.expected_camera(markers, 10) == 'CamB'
    assert review.expected_camera([[1, 'note', None]], 3) is None
    assert review.segments([(1, 'A'), (2, 'A'), (3, 'B'), (5, 'B')]) == [['A', 1, 2], ['B', 3, 3], ['B', 5, 5]]
    shots = review.marker_shots({'frame_start': 1, 'frame_end': 30, 'markers': markers, 'camera': None})
    assert [(s['shot_id'], s['frame_start'], s['frame_end'], s['camera']) for s in shots] == [
        ('CUT_01', 1, 9, 'CamA'), ('CUT_02', 10, 30, 'CamB')]
    assert review.marker_shots({'frame_start': 1, 'frame_end': 9, 'markers': [], 'camera': 'C'})[0]['source'] == 'SCENE_CAMERA'


def test_representative_frame_policy():
    shot = {'frame_start': 10, 'frame_end': 30, 'beat_id': 'B2', 'representative_frame': None}
    beats = [{'beat_id': 'B2', 'frame': 27, 'purpose': 'reveal'}]
    assert review.representative_frame(dict(shot, representative_frame=12), beats)[:2] == (12, 'SHOT_REPRESENTATIVE_FRAME')
    assert review.representative_frame(shot, beats) == (27, 'BEAT', 'reveal')
    assert review.representative_frame(shot, [{'beat_id': 'B2', 'frame': 99}])[:2] == (20, 'CENTER')
    assert review.beat_rows({'beats': {'schema': 's', 'beats': beats}}) == beats


def test_chunk_sizing_from_measured_seconds_per_image():
    assert review.next_chunk(1.0, 1) == 90
    assert review.next_chunk(0.01, 1) == review.MAX_CHUNK_FRAMES
    assert review.next_chunk(500.0, 1) == 1


def png(width, height, rgb):
    raw = b''.join(b'\x00' + bytes(rgb) * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def test_apng_info_counts_frames_of_a_written_sequence(tmp_path):
    files = []
    for index, colour in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255))):
        path = tmp_path / ('frame_' + str(index) + '.png')
        path.write_bytes(png(4, 2, colour))
        files.append({'path': str(path)})
    write_apng(files, tmp_path / 'sequence.png', 30)
    info = review.apng_info(tmp_path / 'sequence.png')
    assert info == {'valid': True, 'frames': 3, 'frame_controls': 3, 'width': 4, 'height': 2}
    assert review.apng_info(tmp_path / 'frame_0.png')['valid'] is False


def test_player_offers_normal_half_quarter_speed_and_stepping(tmp_path):
    path = review.write_player(tmp_path, {'frames': [1, 2, 3], 'files': ['frame_000001.png'] * 3, 'fps': 24.0,
                                          'step': 1, 'segments': [['CamA', 1, 3]],
                                          'shots': [{'shot_id': '</script><b>', 'frame_start': 1, 'frame_end': 3}]})
    page = (tmp_path / 'preview.html').read_text(encoding='utf-8')
    assert path.endswith('preview.html')
    for needle in ('value="1.0"', 'value="0.5"', 'value="0.25"', 'id="prev"', 'id="next"', 'ArrowLeft', 'sequence.png',
                   'AHEAD=24'):
        assert needle in page
    assert '</script><b>' not in page  # data is embedded as JSON with '</' escaped


def test_storyboard_escapes_plan_text(tmp_path):
    review.write_storyboard(tmp_path, 'T', [{'shot_id': 'S<1>', 'type': None, 'image': 'shots/a.png',
                                             'rows': [('Propósito narrativo', '<img onerror=x>')]}], ['nota'])
    page = (tmp_path / 'storyboard.html').read_text(encoding='utf-8')
    assert '<img onerror=x>' not in page and '&lt;img onerror=x&gt;' in page and 'S&lt;1&gt;' in page


def test_limits_are_labelled_proposals_and_no_score_is_produced():
    assert review.LIMITS_SOURCE == 'PROPOSAL'
    assert 'score' in review.NO_SCORE.lower() and 'No quality' in review.NO_SCORE
    for target, row in review.REFINE_TARGETS.items():
        assert row['direction'] in ('lower', 'higher') and row['definition'] and not math.isnan(len(target))
