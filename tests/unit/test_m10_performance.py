"""M10 area D without Blender: option contracts, acceptance gates, the measured NLA time mapping and payload budgets."""
from __future__ import annotations

import ast
import math
import types

import pytest

from blender_mcp.production.cinematic import performance as P
from blender_mcp.production.cinematic.service import HANDLERS, dispatch
from blender_mcp.production.cinematic.tools import NAMES, READ_ONLY, TRANSIENT
from blender_mcp.production.common.bridge import build_code, payload_bytes

TOOLS = ('cinematic_stage_actors', 'cinematic_sync_actions', 'cinematic_integrate_facial', 'cinematic_integrate_secondary',
         'cinematic_manage_props')
A = 'a' * 16
B = 'b' * 16
PLAN = {'generation_id': 'c' * 32, 'actors': [
    {'actor_id': A, 'object': 'RigA', 'meshes': ['BodyA', 'FaceA']},
    {'actor_id': B, 'object': 'RigB', 'meshes': ['BodyB']}]}
TIMELINE = {'fps': 30, 'fps_base': 1.0, 'frame_start': 1, 'frame_end': 120}


def test_area_tools_are_registered_writers():
    for name in TOOLS:
        assert name in NAMES and name in HANDLERS
        assert name not in READ_ONLY and name not in TRANSIENT


@pytest.mark.parametrize('name', TOOLS)
def test_unknown_options_are_refused_before_any_storage_access(name):
    # storage=None proves the refusal happens before the ledger or Blender is touched
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_UNSUPPORTED: unknown keys bogus'):
        dispatch(name, None, None, 'op', cinematic='x', options={'bogus': 1})


# ------------------------------------------------------------------ staging

def test_staging_validates_actor_numbers_and_duplicates():
    rows = P.normalize_staging({'actors': [{'actor_id': A, 'location': [1, 2, 0], 'yaw_deg': 90}]}, PLAN)
    assert rows == [{'actor_id': A, 'object': 'RigA', 'location': [1.0, 2.0, 0.0], 'yaw_deg': 90.0}]
    with pytest.raises(ValueError, match='ACTOR_NOT_IN_CINEMATIC'):
        P.normalize_staging({'actors': [{'actor_id': 'f' * 16}]}, PLAN)
    with pytest.raises(ValueError, match='ACTOR_DUPLICATED'):
        P.normalize_staging({'actors': [{'actor_id': A}, {'actor_id': A}]}, PLAN)
    with pytest.raises(ValueError, match='CINEMATIC_NUMBER_INVALID'):
        P.normalize_staging({'actors': [{'actor_id': A, 'yaw_deg': float('nan')}]}, PLAN)
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_UNSUPPORTED'):
        P.normalize_staging({'actors': [{'actor_id': A, 'scale': 2}]}, PLAN)
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_REQUIRED'):
        P.normalize_staging({'actors': []}, PLAN)


# ------------------------------------------------------------------ clips

def clip(**extra):
    row = {'actor_id': A, 'action': 'Walk', 'start_frame': 11}
    row.update(extra)
    return row


def test_clips_default_to_no_retime_and_nothing_extrapolation():
    [actor] = P.normalize_clips({'clips': [clip()]}, PLAN)
    assert actor['object'] == 'RigA'
    assert actor['clips'][0] | {} == {'action': 'Walk', 'slot': None, 'start_frame': 11, 'action_frame_start': None,
                                      'action_frame_end': None, 'scale': 1.0, 'repeat': 1.0, 'extrapolation': 'NOTHING',
                                      'retime': False, 'carry_root': False}


def test_carry_root_is_a_boolean_and_never_on_the_first_clip():
    rows = [clip(action_frame_start=1, action_frame_end=60), clip(action='Stare', start_frame=80, carry_root=True)]
    [actor] = P.normalize_clips({'clips': rows}, PLAN)
    assert [c['carry_root'] for c in actor['clips']] == [False, True]
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_INVALID'):
        P.normalize_clips({'clips': [clip(), clip(action='Stare', start_frame=80, carry_root='yes')]}, PLAN)
    # The first clip by start frame, whatever the order of the rows, has no previous root to continue.
    with pytest.raises(ValueError, match='CARRY_ROOT_FIRST_CLIP'):
        P.normalize_clips({'clips': [clip(action='Stare', start_frame=80), clip(start_frame=1, carry_root=True)]}, PLAN)


def test_carry_runtime_keeps_the_formula_and_its_guards():
    runtime = P.PERF_RUNTIME
    body = runtime.split('def pf_carry_apply(')[1].split('\ndef ')[0]
    # world = T(L + dloc) @ R_delta @ R @ S, so G @ world is again a delta transform (exact for an unparented actor).
    assert 'dloc = acc.translation + rot @ (location + base_loc) - location' in body
    assert "pf_insert(bag, 'delta_location'" in body and "strip.blend_type = 'REPLACE'" in body
    assert "touched.append(cm_touch_delta(obj))" in body and "cm_new_action(params, row['carry_track'], 'root_carry')" in body
    check = runtime.split('def pf_carry_check(')[1].split('\ndef ')[0]
    for code in ('CARRY_ROOT_UNSUPPORTED', 'ACTOR_DELTA_ANIMATED', 'CARRY_ROOT_SECONDARY_UNSUPPORTED'):
        assert code in check


def test_scale_needs_explicit_retime():
    with pytest.raises(ValueError, match='CLIP_RETIME_REQUIRED'):
        P.normalize_clips({'clips': [clip(scale=2)]}, PLAN)
    [actor] = P.normalize_clips({'clips': [clip(scale=2, retime=True)]}, PLAN)
    assert actor['clips'][0]['scale'] == 2.0


@pytest.mark.parametrize('bad, code', [({'start_frame': 1.5}, 'CINEMATIC_INTEGER_REQUIRED'),
                                       ({'extrapolation': 'CYCLE'}, 'CINEMATIC_CHOICE_INVALID'),
                                       ({'action_frame_start': 10, 'action_frame_end': 5}, 'CLIP_RANGE_INVALID'),
                                       ({'retime': 'yes'}, 'CINEMATIC_OPTION_INVALID'),
                                       ({'repeat': 0}, 'CINEMATIC_RANGE_INVALID'),
                                       ({'slot': ''}, 'CINEMATIC_OPTION_INVALID')])
def test_clip_fields_are_strict(bad, code):
    with pytest.raises(ValueError, match=code):
        P.normalize_clips({'clips': [clip(**bad)]}, PLAN)


def test_explicit_overlapping_clips_of_one_actor_are_refused_on_the_host():
    rows = [clip(action_frame_start=1, action_frame_end=60), clip(action='Idle', start_frame=50, action_frame_start=1,
                                                                  action_frame_end=30)]
    with pytest.raises(ValueError, match='CLIP_OVERLAP'):
        P.normalize_clips({'clips': rows}, PLAN)
    rows[1]['start_frame'] = 70  # touching (start == previous end) is allowed: measured in Blender 5.2.1
    assert len(P.normalize_clips({'clips': rows}, PLAN)[0]['clips']) == 2


def test_root_bones_only_for_actors_in_the_call():
    [actor] = P.normalize_clips({'clips': [clip()], 'root_bones': {A: 'root'}}, PLAN)
    assert actor['root_bone'] == 'root'
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_INVALID'):
        P.normalize_clips({'clips': [clip()], 'root_bones': {B: 'root'}}, PLAN)


# ------------------------------------------------------------------ props

def prop(**extra):
    row = {'prop_id': 'Cup', 'object': 'CupMesh'}
    row.update(extra)
    return row


def test_props_hand_off_windows_are_contiguous_not_overlapping():
    attach = [{'actor_id': A, 'bone': 'hand', 'frame_start': 20, 'frame_end': 40},
              {'actor_id': B, 'bone': 'hand', 'frame_start': 41, 'frame_end': 60}]
    [row] = P.normalize_props({'props': [prop(attach=attach)]}, PLAN, TIMELINE)
    assert [w['actor_object'] for w in row['attach']] == ['RigA', 'RigB']
    attach[1]['frame_start'] = 40
    with pytest.raises(ValueError, match='PROP_ATTACH_OVERLAP'):
        P.normalize_props({'props': [prop(attach=attach)]}, PLAN, TIMELINE)


@pytest.mark.parametrize('row, code', [
    (prop(object='BodyA'), 'PROP_IS_ACTOR'),
    (prop(object='RigB'), 'PROP_IS_ACTOR'),
    (prop(prop_id='x' * 25), 'CINEMATIC_NAME_INVALID'),
    (prop(attach=[{'actor_id': A, 'bone': 'hand', 'frame_start': 1, 'frame_end': 9}]), 'PROP_ATTACH_WINDOW_INVALID'),
    (prop(attach=[{'actor_id': A, 'bone': 'hand', 'frame_start': 30, 'frame_end': 20}]), 'PROP_ATTACH_WINDOW_INVALID'),
    (prop(attach=[{'actor_id': A, 'frame_start': 30, 'frame_end': 40, 'parent': 'x'}]), 'CINEMATIC_OPTION_UNSUPPORTED'),
    (prop(visibility=[{'frame': 3, 'visible': 1}]), 'CINEMATIC_OPTION_INVALID'),
    (prop(placement={'location': [0, 0]}), 'CINEMATIC_VECTOR_INVALID'),
])
def test_prop_rows_are_strict(row, code):
    with pytest.raises(ValueError, match=code):
        P.normalize_props({'props': [row]}, PLAN, TIMELINE)


def test_duplicate_prop_ids_are_refused():
    with pytest.raises(ValueError, match='PROP_DUPLICATED'):
        P.normalize_props({'props': [prop(), prop(object='Other')]}, PLAN, TIMELINE)


# ------------------------------------------------------------------ acceptance gates

FPLAN = {'brief': {'fps': 30}, 'head_policy': 'BODY_OWNS', 'profile': {'armature': 'RigA', 'head_bone': 'head', 'neck_bone': 'neck'}}
FSTATE = {'state': 'ACCEPTED', 'candidate_action': 'M7F_x_v1', 'candidate_fingerprint': 'm521:1',
          'accepted_tracks': [['RigA', 'M7_FACE M7F_x_v1'], ['Key', 'M7_FACE M7F_x_v1']]}


def test_facial_gate_accepts_only_current_accepted_same_fps():
    gate = P.facial_gate(FPLAN, FSTATE, PLAN, TIMELINE)
    assert gate['candidate'] == 'M7F_x_v1' and gate['head_bones'] == ['head', 'neck'] and gate['policy'] == 'BODY_OWNS'
    with pytest.raises(ValueError, match='FACIAL_NOT_ACCEPTED'):
        P.facial_gate(FPLAN, dict(FSTATE, state='REVIEWED'), PLAN, TIMELINE)
    with pytest.raises(ValueError, match='FACIAL_NOT_ACCEPTED'):
        P.facial_gate(FPLAN, dict(FSTATE, pending_refinement={'x': 1}), PLAN, TIMELINE)
    with pytest.raises(ValueError, match='FACIAL_FPS_MISMATCH'):
        P.facial_gate(dict(FPLAN, brief={'fps': 24}), FSTATE, PLAN, TIMELINE)
    with pytest.raises(ValueError, match='FACIAL_FPS_MISMATCH'):
        P.facial_gate(FPLAN, FSTATE, PLAN, dict(TIMELINE, fps_base=1.001))
    with pytest.raises(ValueError, match='FACIAL_HEAD_POLICY_UNSUPPORTED'):
        P.facial_gate(dict(FPLAN, head_policy='BLENDED'), FSTATE, PLAN, TIMELINE)
    with pytest.raises(ValueError, match='FACIAL_TRACK_MISSING'):
        P.facial_gate(FPLAN, dict(FSTATE, accepted_tracks=[]), PLAN, TIMELINE)


PPLAN = {'brief': {'fps': 30}, 'object_name': 'Pendant', 'params': {'source_object': 'Pendant', 'primary_objects': ['RigB']}}
PSTATE = {'state': 'ACCEPTED', 'cache': {'representation': 'PC2', 'frame_start': 1, 'frame_end': 60},
          'bake_deviation': {'passed': True}, 'baked_candidate': {'candidate_object': 'M8_x_v1_Baked'},
          'candidate': {'candidate_object': 'M8_x_v1'}}


def test_secondary_gate_contract():
    gate = P.secondary_gate(PPLAN, PSTATE, TIMELINE, 10, [11, 70])
    # m8_candidate travels to the payload, which refuses it (and the baked object and the source) in the candidate
    assert gate == {'baked': 'M8_x_v1_Baked', 'cache_start': 1, 'cache_end': 60, 'source_object': 'Pendant',
                    'primary_objects': ['RigB'], 'm8_candidate': 'M8_x_v1'}
    cases = [
        (dict(PSTATE, cache={'representation': 'SECONDARY_ACTION'}), 'SECONDARY_BACKEND_UNSUPPORTED'),
        (dict(PSTATE, state='REVIEWED'), 'SECONDARY_NOT_ACCEPTED'),
        (dict(PSTATE, uncertain_operation=True), 'SECONDARY_NOT_ACCEPTED'),
        (dict(PSTATE, bake_deviation={'passed': False}), 'SECONDARY_BAKE_NOT_EQUIVALENT'),
        (dict(PSTATE, baked_candidate=None), 'SECONDARY_BAKED_MISSING'),
    ]
    for state, code in cases:
        with pytest.raises(ValueError, match=code):
            P.secondary_gate(PPLAN, state, TIMELINE, 10, [11, 70])
    with pytest.raises(ValueError, match='SECONDARY_FPS_MISMATCH'):
        P.secondary_gate(dict(PPLAN, brief={'fps': 60}), PSTATE, TIMELINE, 10, [11, 70])
    with pytest.raises(ValueError, match='SECONDARY_WINDOW_OUTSIDE_CACHE'):
        P.secondary_gate(PPLAN, PSTATE, TIMELINE, 10, [10, 70])
    with pytest.raises(ValueError, match='SECONDARY_WINDOW_OUTSIDE_CACHE'):
        P.secondary_gate(PPLAN, PSTATE, TIMELINE, 10, [11, 71])


# ------------------------------------------------------------------ measured NLA time mapping (pure runtime function)

def runtime_function(name, *helpers):
    tree = ast.parse(P.PERF_RUNTIME)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in (name,) + helpers]
    namespace = {'math': math}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), name, 'exec'), namespace)
    return namespace[name]


def test_action_frame_mapping_matches_the_measured_strip_behaviour():
    # Measured in Blender 5.2.1 (docs/milestone-10-baseline.md, NLA section): Action keys 1..11, strip at 101.
    f = runtime_function('pf_action_frame')
    assert f(101, 101, 1, 11, 1, 1) == 1 and f(106, 101, 1, 11, 1, 1) == 6 and f(111, 101, 1, 11, 1, 1) == 11
    assert f(106, 101, 1, 11, 2, 1) == 3.5 and f(121, 101, 1, 11, 2, 1) == 11          # scale 2: frame_end 121
    assert f(110, 101, 1, 11, 1, 2) == 10 and f(111, 101, 1, 11, 1, 2) == 1            # repeat 2: cycle restarts at 111
    assert f(116, 101, 1, 11, 1, 2) == 6 and f(121, 101, 1, 11, 1, 2) == 11            # ... and ends on the last frame
    assert f(201, 201, 3, 11, 1, 1) == 3                                                # action_frame_start trims the source


def test_fractional_repeat_ends_inside_a_cycle():
    """Measured in Blender 5.2.1 (E2E diagnostic bpm10-e2e-diag1/nla_boundary.json): a 0->60 clip (keys 1..61) with
    repeat 2.75 at 200 ends at 365 with the value of action frame 46; repeat 2.0 at 400 ends at 520 on frame 61.
    Before the fix every fractional repeat expected the last action frame at the strip end (CLIP_MAPPING_FAILED on
    demo B: HumanB_Idle repeat 2.75, error 0.0199)."""
    f = runtime_function('pf_action_frame')
    assert f(365, 200, 1, 61, 1, 2.75) == 46 and f(364, 200, 1, 61, 1, 2.75) == 45 and f(260, 200, 1, 61, 1, 2.75) == 1
    assert f(520, 400, 1, 61, 1, 2.0) == 61 and f(460, 400, 1, 61, 1, 2.0) == 1


def test_shared_boundary_frame_belongs_to_the_earlier_strip():
    """Measured (same diagnostic): strips [1, 11] and [11, 21] on one track evaluate the first strip's last frame at
    11. The later clip must not sample its start frame (demo B: React after Idle reported 0.0199, Idle after React
    0.1647, both from the shared frame)."""
    body = P.PERF_RUNTIME.split('def pf_sync_verify(')[1].split(chr(10) + 'def ')[0]
    assert 'previous_end' in body and "abs(f - clip['start']) > 1e-6" in body


def test_integer_sampling_keeps_both_ends():
    f = runtime_function('pf_int_frames')
    assert f(11, 70, 8)[0] == 11 and f(11, 70, 8)[-1] == 70 and len(f(11, 70, 8)) <= 12
    assert f(11, 13, 8) == [11, 12, 13] and f(5.5, 5.6, 8) == []


# ------------------------------------------------------------------ NLA gaps (root reset hazard)

def test_gap_runs_follow_the_measured_extrapolation_rules():
    runs = runtime_function('pf_gap_runs')
    # The probe layout: Walk [11, 70] NOTHING, Idle [75, 118.5] HOLD_FORWARD, timeline 1..120.
    spans = [[11.0, 70.0, 'NOTHING', 'Walk'], [75.0, 118.5, 'HOLD_FORWARD', 'Idle']]
    rows = runs(spans, 1, 120)
    assert [(r['kind'], r['frames']) for r in rows] == [('BEFORE_FIRST', [1, 10]), ('BETWEEN', [71, 74])]
    assert rows[1]['after'] == 'Walk' and rows[1]['before'] == 'Idle'
    # NOTHING on the last clip leaves the tail undriven; a fractional end starts the gap at the next integer frame.
    tail = runs([[1.0, 118.5, 'NOTHING', 'Idle']], 1, 120)
    assert [(r['kind'], r['frames']) for r in tail] == [('AFTER_LAST', [119, 120])]
    # HOLD on the first strip holds backwards and forwards; HOLD_FORWARD only forwards.
    assert runs([[11.0, 70.0, 'HOLD', 'Walk']], 1, 120) == []
    assert [r['frames'] for r in runs([[11.0, 70.0, 'HOLD_FORWARD', 'Walk']], 1, 120)] == [[1, 10]]
    # Contiguous clips (start == previous end) leave no gap; clips beyond the timeline are clipped to it.
    assert runs([[1.0, 60.0, 'NOTHING', 'A'], [60.0, 120.0, 'HOLD_FORWARD', 'B']], 1, 120) == []
    assert runs([[1.0, 200.0, 'NOTHING', 'A']], 1, 120) == []
    assert runs([], 1, 120) == []


def test_transform_keys_cover_object_and_root_bone_transforms_only():
    keys = runtime_function('pf_transform_keys', 'pf_channel')
    curves = [types.SimpleNamespace(data_path=p, array_index=i) for p, i in (
        ('location', 0), ('pose.bones["root"].location', 1), ('pose.bones["root"].rotation_quaternion', 0),
        ('pose.bones["spine"].rotation_euler', 0), ('pose.bones["root"]["custom"]', 0), ('scale', 2))]
    assert keys(curves, ['root']) == {'location[0]', 'pose.bones["root"].location[1]',
                                      'pose.bones["root"].rotation_quaternion[0]', 'scale[2]'}
    assert keys(curves, []) == {'location[0]', 'scale[2]'}


def test_gap_warnings_turn_hazards_into_warnings_never_errors():
    measured = {'root_reset_gaps': {'hazards': [{'kind': 'BETWEEN', 'frames': [71, 74], 'after': 'Walk', 'before': 'Idle',
                                                 'extrapolation': 'NOTHING', 'jump_m': 3.0, 'frames_compared': [70, 71],
                                                 'measured_root': 'root', 'channels': 1}]},
                'boundaries': [{'frames': [70, 75], 'between': ['Walk', 'Idle'], 'jump_m': 3.0, 'neighbour_step_m': 0.05,
                                'root': 'root', 'finding': 'ROOT_DISCONTINUITY_REVIEW'},
                               {'frames': [1, 2], 'between': ['a', 'b'], 'jump_m': 0.0, 'neighbour_step_m': 0.0,
                                'root': 'root', 'finding': None}]}
    rows = P.gap_warnings(A, measured)
    assert [r['code'] for r in rows] == ['ROOT_RESET_IN_NLA_GAP', 'ROOT_DISCONTINUITY_REVIEW']
    assert all(r['severity'] == 'WARNING' for r in rows) and rows[0]['measured']['jump_m'] == 3.0


# ------------------------------------------------------------------ facial events

def test_facial_events_are_strict_and_sorted():
    rows = P.normalize_events([{'frame': 56, 'label': 'jaw peak'}, {'frame': 45}])
    assert rows == [{'frame': 45, 'label': None}, {'frame': 56, 'label': 'jaw peak'}]
    assert P.normalize_events(None) == []
    for bad, code in [([{'frame': 1.5}], 'CINEMATIC_INTEGER_REQUIRED'), ([{'frame': 1, 'kind': 'x'}], 'CINEMATIC_OPTION_UNSUPPORTED'),
                      ([{'frame': 1, 'label': ''}], 'CINEMATIC_TEXT_INVALID'), ({'frame': 1}, 'CINEMATIC_OPTION_INVALID'),
                      ([{'frame': f} for f in range(65)], 'CINEMATIC_OPTION_INVALID'), (['x'], 'CINEMATIC_OPTION_INVALID')]:
        with pytest.raises(ValueError, match=code):
            P.normalize_events(bad)


def test_secondary_rows_keep_the_requested_window_apart_from_the_cached_one():
    state = {'actors': {B: {'object': 'RigB', 'clips': [{'action': 'Wave'}], 'secondary': [
        {'object': 'COPY', 'copy': 'COPY', 'window': [11, 70], 'requested_window': [20, 60], 'delta': 10,
         'reference': [], 'indices': [], 'primary_action': 'Wave'}]}}}
    [row] = P._secondary_records(state, [B])
    assert row['window'] == [20, 60] and row['copy'] == 'COPY'
    del state['actors'][B]['secondary'][0]['requested_window']
    assert P._secondary_records(state, [B])[0]['window'] == [11, 70]


# ------------------------------------------------------------------ quality-report providers (pure halves)

class NoBridge:
    def run(self, body, params=None):
        raise AssertionError('a NOT_APPLICABLE report must not reach Blender')


def test_reports_are_not_applicable_without_integrations_and_review_maps_them():
    from blender_mcp.production.cinematic import review
    from blender_mcp.production.cinematic.service import Context
    ctx = Context(NoBridge(), None, 'op')
    state = {'actors': {A: {'object': 'RigA', 'staging': {'yaw_deg_total': 90.0}}}}
    pool = {'ctx': ctx, 'plan': PLAN, 'state': state, 'options': None}
    for function, kind in ((P.sync_report, 'sync'), (P.facial_report, 'facial'), (P.secondary_report, 'secondary')):
        result, missing = review.call_provider(function, pool)
        assert missing == [] and result['status'] == 'NOT_APPLICABLE' and result['passed'] is None
        assert result['state_restored'] is True and result['kind'] == kind
        assert review.normalize_section(result)['status'] == 'NOT_APPLICABLE'
    # review looks the providers up by these exact names in this module
    names = {fn for spec in review.SECTIONS for module, fn in spec['providers'] if module == 'performance'}
    assert names == {'sync_report', 'facial_report', 'secondary_report'}
    assert all(callable(getattr(P, n)) for n in names)
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_UNSUPPORTED'):
        P.sync_report(ctx, PLAN, state, {'bogus': 1})


def restored(**changes):
    row = {'restoration': {'digest_before': 'd', 'digest_after': 'd', 'source_before': [1, 0.0, 'Cam'],
                           'source_after': [1, 0.0, 'Cam']},
           'fingerprints_before': {'source': 's', 'candidate': 'c'}, 'fingerprints_after': {'source': 's', 'candidate': 'c'}}
    row['restoration'].update(changes)
    return row


def test_envelope_fails_on_errors_or_lost_restoration_and_never_on_warnings():
    ok = P.restoration_of(restored())
    assert ok == {'state_digest_equal': True, 'source_frame_and_camera_equal': True, 'fingerprints_equal': True}
    warning = [{'code': 'ROOT_RESET_IN_NLA_GAP', 'severity': 'WARNING'}]
    report = P.envelope('sync', warning, {'clips': 1}, [ok], [])
    assert report['status'] == 'PASS' and report['passed'] is True and 'score' not in str(sorted(report)).lower()
    lost = P.restoration_of(restored(digest_after='other'))
    report = P.envelope('sync', warning, {}, [ok, lost], [])
    assert report['status'] == 'FAIL' and report['state_restored'] is False
    assert report['findings'][-1]['code'] == 'CINEMATIC_STATE_NOT_RESTORED'
    report = P.envelope('facial', [{'code': 'FACIAL_ACTION_CHANGED', 'severity': 'ERROR'}], {}, [ok], [])
    from blender_mcp.production.cinematic import review
    section = review.normalize_section(report)
    assert report['status'] == 'FAIL' and section['status'] == 'FAIL' and section['error_count'] == 1


def measured_clip(**extra):
    row = {'action': 'Walk', 'frame_start': 11.0, 'frame_end': 70.0, 'action_frame_start': 1.0, 'action_frame_end': 60.0,
           'scale': 1.0, 'repeat': 1.0, 'extrapolation': 'NOTHING', 'mapping_passed': True, 'max_channel_error': 0.0,
           'value_comparisons': 24}
    row.update(extra)
    return row


def test_sync_findings_compare_the_strips_now_with_what_sync_recorded():
    entry = {'clips': [measured_clip()], 'source_action_digests': {'Walk': 'm521:a'}}
    measured = {'actor_id': A, 'status': 'MEASURED', 'clips': [measured_clip()], 'source_action_digests': {'Walk': 'm521:a'},
                'track_overlaps': [], 'boundaries': [], 'root_reset_gaps': {'hazards': []}}
    assert P.sync_findings(measured, entry) == []
    moved = dict(measured, clips=[measured_clip(frame_start=12.0, frame_end=71.0)])
    assert [f['code'] for f in P.sync_findings(moved, entry)] == ['CLIP_PLACEMENT_CHANGED']
    edited = dict(measured, source_action_digests={'Walk': 'm521:b'})
    assert [f['code'] for f in P.sync_findings(edited, entry)] == ['SOURCE_ACTION_CHANGED']
    bad = dict(measured, clips=[measured_clip(mapping_passed=False, max_channel_error=0.3)], track_muted=True)
    assert {f['code'] for f in P.sync_findings(bad, entry)} == {'CLIP_MAPPING_FAILED', 'SYNC_TRACK_MUTED'}
    gone = {'actor_id': A, 'status': 'TRACK_MISSING', 'track': 'T'}
    assert [(f['code'], f['severity']) for f in P.sync_findings(gone, entry)] == [('SYNC_TRACK_MISSING', 'ERROR')]


def test_facial_findings_measure_digest_tracks_policy_and_events():
    facial = {'fingerprint': 'm521:f', 'candidate': 'M7F_x_v1'}
    ledger = {'state': 'ACCEPTED', 'fingerprint_matches': True}
    measured = {'actor_id': A, 'status': 'MEASURED', 'action_digest': 'm521:f', 'problems': [],
                'tracks': [{'owner': 'RigA', 'passed': True, 'max_channel_error': 0.0, 'value_comparisons': 10}],
                'head': {'policy': 'BODY_OWNS', 'face_head': [], 'body_head': ['pose.bones["head"].rotation_euler[0]'],
                         'overlapping_channels': 0, 'face_track_above_body': []}, 'window': [41.0, 71.0]}
    events = [{'actor_id': A, 'frame': 56, 'label': 'peak'}]
    assert P.facial_findings(measured, facial, ledger, events) == []
    codes = [f['code'] for f in P.facial_findings(measured, facial, ledger, [])]
    assert codes == ['FACIAL_EVENTS_NOT_DECLARED']
    nothing = dict(measured, tracks=[])
    assert [f['code'] for f in P.facial_findings(nothing, facial, ledger, events)] == ['FACIAL_NOT_MEASURED']
    changed = dict(measured, action_digest='m521:g', problems=[['M7_TRACK_STATE_CHANGED', 'unmuted']],
                   head=dict(measured['head'], face_head=['pose.bones["head"].location[0]']))
    codes = {f['code'] for f in P.facial_findings(changed, facial, {'state': 'REVIEWED'}, [{'frame': 90}])}
    assert codes == {'FACIAL_NOT_ACCEPTED', 'FACIAL_ACTION_CHANGED', 'M7_TRACK_STATE_CHANGED', 'HEAD_OWNERSHIP_CONFLICT',
                     'FACIAL_EVENT_OUTSIDE_WINDOW'}


def test_secondary_findings_measure_equivalence_and_flag_an_unsynchronized_primary():
    rec = {'delta': 10, 'cache': {'frame_start': 1}, 'baked_object_digest': 'm521:b'}
    ledger = {'state': 'ACCEPTED', 'cache_sha_matches': True}
    measured = {'actor_id': B, 'status': 'MEASURED', 'copy': 'COPY', 'baked': 'BAKED', 'cache_frame_start': 11.0,
                'cache_file_matches': True, 'copy_in_cin': True, 'copy_hide_render': False, 'baked_in_candidate': False,
                'baked_digest': 'm521:b', 'follows_actor': True,
                'local_equivalence': {'max_error_m': 0.0, 'frames': 16, 'copy_motion_range_m': 0.08, 'same_topology': True},
                'world_equivalence': {'status': 'MEASURED', 'max_error_m': 4.8e-7, 'frames': 16}}
    assert P.secondary_findings(measured, rec, ledger, True) == []
    rows = P.secondary_findings(measured, rec, ledger, False)
    assert [(f['code'], f['severity']) for f in rows] == [('SECONDARY_PRIMARY_NOT_SYNCHRONIZED', 'WARNING')]
    broken = dict(measured, cache_frame_start=1.0, copy_hide_render=True,
                  world_equivalence={'status': 'MEASURED', 'max_error_m': 0.2, 'frames': 16},
                  local_equivalence={'max_error_m': 0.0, 'frames': 16, 'copy_motion_range_m': 0.0, 'same_topology': True})
    codes = {f['code'] for f in P.secondary_findings(broken, rec, {'state': 'ACCEPTED', 'cache_error': 'STALE_CACHE'}, True)}
    assert codes == {'SECONDARY_CACHE_OFFSET_CHANGED', 'SECONDARY_COPY_HIDDEN', 'SECONDARY_WORLD_EQUIVALENCE_FAILED',
                     'SECONDARY_PLAYBACK_UNPROVEN', 'SECONDARY_CACHE_NOT_CURRENT'}


# ------------------------------------------------------------------ payload budgets (safe mode, bytes, AST nodes)

def realistic_params(name):
    identity = {'generation_id': 'c' * 32, 'version': 1, 'scene': 'M10C_cccccccc_v1', 'source_scene': 'Scene',
                'collections': {'src': 'M10C_cccccccc_v1_SRC', 'cin': 'M10C_cccccccc_v1_CIN'},
                'capture': ['Actor_%02d' % i for i in range(8)] + ['Mesh_%02d' % i for i in range(16)]}
    ref = [{'frame': f, 'actor': [[1.0, 0.0, 0.0, 0.0]] * 4, 'points': [[0.123456789, 1.23456789, 2.3456789]] * 4}
           for f in range(16)]
    secondary = [{'actor_object': 'Actor_00', 'copy': 'M10C_cccccccc_v1_SEC_aaaaaaaa_bbbbbb', 'delta': 10,
                  'reference': ref, 'indices': [0, 1, 2, 3], 'tolerance_m': 1e-5, 'primary_action': 'Walk', 'window': [11, 70]}]
    if name == 'stage':
        actors = [{'actor_id': 'a' * 16, 'object': 'Actor_%02d' % i, 'location': [1.0, 2.0, 3.0], 'yaw_deg': 45.0,
                   'secondary_copies': ['copy']} for i in range(8)]
        return dict(identity, actors=actors, frames=[1, 30, 60, 90, 120], secondary=secondary * 8,
                    all_actors=[{'actor_id': 'a' * 16, 'object': 'Actor_%02d' % i, 'meshes': ['M_%d' % j for j in range(4)],
                                 'copies': ['M10C_cccccccc_v1_SEC_aaaaaaaa_bbbbbb']} for i in range(8)])
    if name == 'sync':
        clips = [{'action': 'Action_%02d' % j, 'slot': 'OBActor', 'start_frame': 100 * j, 'action_frame_start': 1.0,
                  'action_frame_end': 60.0, 'scale': 1.0, 'repeat': 1.0, 'extrapolation': 'HOLD_FORWARD', 'retime': False}
                 for j in range(32)]
        actors = [{'actor_id': 'a' * 16, 'object': 'Actor_%02d' % i, 'clips': clips, 'root_bone': 'root',
                   'root_source': 'OPTIONS', 'rotated': True, 'replace': False,
                   'secondary': [{'primary_action': 'Walk', 'delta': 10, 'window': [11, 70]}],
                   'track': 'M10C_cccccccc_v1_BODY_aaaaaaaa'} for i in range(8)]
        return dict(identity, actors=actors, tolerance=1e-5, root_jump_m=0.05, root_jump_ratio=4.0, secondary=secondary * 8,
                    timeline=[1, 3600])
    if name == 'face':
        return dict(identity, armature='Actor_00', action='M7F_cccccccc_v1', fingerprint='m521:x', start_frame=41,
                    m7_tracks=[['Actor_00', 'M7_FACE M7F_cccccccc_v1']] + [['Key.%03d' % i, 'M7_FACE M7F_cccccccc_v1'] for i in range(8)],
                    extrapolation='NOTHING', own_track='M10C_cccccccc_v1_FACE_aaaaaaaa', head_policy='BODY_OWNS',
                    head_bones=['head', 'neck'], tolerance=1e-5, events=list(range(41, 41 + P.MAX_FACIAL_EVENTS)))
    if name == 'secondary':
        return dict(identity, actor_object='Actor_00', baked='M8_x_v1_Baked', source_object='Pendant',
                    copy='M10C_cccccccc_v1_SEC_aaaaaaaa_bbbbbb', m8_candidate='M8_x_v1', cache_start=1, cache_end=601,
                    delta=10, points=4, reference_frames=16, world_comparable=False)
    expected = {'source': 'm521:' + 'f' * 290, 'candidate': 'm521:' + 'e' * 290}
    if name == 'sync_report':
        return dict(identity, expected_fingerprints=expected, actions=['Action_%02d' % j for j in range(32)],
                    actor={'actor_id': 'a' * 16, 'object': 'Actor_00', 'track': 'M10C_cccccccc_v1_BODY_aaaaaaaa',
                           'root_bone': 'root'}, tolerance=1e-5, root_jump_m=0.05, root_jump_ratio=4.0, timeline=[1, 3600])
    if name == 'face_report':
        return dict(identity, expected_fingerprints=expected, tolerance=1e-5, face={
            'actor_id': 'a' * 16, 'armature': 'Actor_00', 'action': 'M7F_cccccccc_v1', 'mode': 'OWN_TRACKS',
            'own_track': 'M10C_cccccccc_v1_FACE_aaaaaaaa', 'start_frame': 41, 'head_policy': 'BODY_OWNS',
            'head_bones': ['head', 'neck'],
            'm7_tracks': [['Actor_00', 'M7_FACE M7F_cccccccc_v1']] + [['Key.%03d' % i, 'M7_FACE M7F_cccccccc_v1'] for i in range(8)]})
    if name == 'secondary_report':
        row = dict(secondary[0], actor_id='a' * 16, baked='M8_x_v1_Baked')
        return dict(identity, expected_fingerprints=expected, row=row)
    if name == 'props':
        props = [{'prop_id': 'Prop_%02d' % i, 'object': 'Obj_%02d' % i, 'copy': 'M10C_cccccccc_v1_PROP_Prop_%02d' % i,
                  'action': 'M10C_cccccccc_v1_PROPACT_Prop_%02d' % i, 'replace': False,
                  'placement': {'location': [1.0, 2.0, 3.0], 'rotation_deg': [0.0, 0.0, 90.0]},
                  'attach': [{'actor_id': 'a' * 16, 'actor_object': 'Actor_00', 'bone': 'hand', 'frame_start': 10 * j + 2,
                              'frame_end': 10 * j + 11} for j in range(8)],
                  'visibility': [{'frame': f, 'visible': f % 2 == 0} for f in range(16)]} for i in range(16)]
        return dict(identity, props=props, frame_start=1, slide_m=1e-5, pop_m=1e-5, rotation_deg=1e-3)
    raise AssertionError(name)


@pytest.mark.parametrize('name', sorted(P.PAYLOADS))
def test_payloads_pass_safe_mode_within_the_m10_budget(name):
    params = realistic_params(name)
    body = P.PAYLOADS[name]
    code = build_code(body, params)  # validate_code (safe mode) runs inside build_code
    size = payload_bytes(body, params)
    nodes = sum(1 for _ in ast.walk(ast.parse(code)))
    assert size < 140_000, (name, size)
    assert nodes < 14_000, (name, nodes)


def test_runtime_avoids_constructs_safe_mode_refuses():
    own = P.PERF_RUNTIME[P.PERF_RUNTIME.index('def pf_rows'):]  # this area's functions, after the shared COMMON
    for forbidden in ('lambda', '__class__', 'bpy_extras', 'import numpy', 'import os', 'import time', 'hashlib',
                      'hasattr(bpy.types', 'driver_add', 'getattr(', 'setattr('):
        assert forbidden not in own, forbidden


def test_thresholds_are_labelled_proposals_and_no_score_key_exists():
    import re
    assert P.THRESHOLDS['threshold_source'] == 'PROPOSAL'
    source = open(P.__file__, encoding='utf-8').read()
    assert not re.findall(r"'[a-z_]*(?:score|rating)[a-z_]*'", source.lower())


def test_secondary_is_measured_in_the_parent_frame_whatever_carries_it():
    # Demo C (measured): a rigid M8 accessory keeps 91 identical PC2 samples and swings through an object Action on a
    # bone-parented object; mesh-local coordinates read it as never moving.
    runtime = P.PERF_RUNTIME
    integrate = runtime.split('def pf_secondary(')[1].split('\ndef ')[0]
    report = runtime.split('def pf_secondary_report(')[1].split('\ndef ')[0]
    for body in (integrate, report):
        assert 'pf_mesh_parent(baked' in body and 'pf_mesh_parent(copy' in body
        assert 'pf_mesh_local(' not in body
    frame = runtime.split('def pf_parent_frame(')[1].split('\ndef ')[0]
    assert 'ev.pose.bones[obj.parent_bone].matrix' in frame and 'Matrix.Identity(4)' in frame
    # With delta != 0 the object Action is shifted like the cache, through an NLA strip on the owned copy only.
    assert "params['delta'] != 0" in integrate and "track.name = 'M10_SECONDARY_MOTION'" in integrate
    assert 'action.fcurves' not in integrate  # the shared M8 Action is never edited
