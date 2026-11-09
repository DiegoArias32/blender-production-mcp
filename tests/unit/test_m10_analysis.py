"""M10 measured cinematic QA without Blender: payload budgets and safe mode, transient hints, closed options, and every
pure judgement (composition, continuity, visibility, collisions, contacts, motion) on synthetic sampler rows."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from blender_mcp.production.cinematic import analysis, store
from blender_mcp.production.cinematic.service import HANDLERS, Context, Request, load_handlers
from blender_mcp.production.cinematic.tools import READ_ONLY, TRANSIENT
from blender_mcp.production.common.bridge import build_code, payload_bytes
from blender_mcp.production.common.payload import link_payload
from blender_mcp.production.common.storage import Storage

A = 'a' * 16
B = 'b' * 16


# ---------------------------------------------------------------- payloads, safe mode, catalogue

def realistic_params(kind):
    """Worst-case-ish parameters: 8 actors, 64 events, 600 explicit frames, long names."""
    actors = [{'actor_id': str(i) * 16, 'object': 'Actor_' + 'x' * 40 + str(i), 'meshes': ['Mesh_' + 'y' * 40 + str(j) for j in range(4)],
               'head_bone': 'head', 'root_bone': 'root'} for i in range(8)]
    events = [{'index': i, 'event_id': 'event_' + str(i), 'kind': 'REACH', 'mode': 'CONTACT', 'actor_id': actors[0]['actor_id'],
               'frame': i, 'frame_start': i, 'frame_end': i + 10, 'window': [i, i + 20], 'target': {'actor_id': actors[1]['actor_id']},
               'effector': {'bone': 'hand'}, 'contact': True, 'head_bone': None, 'forward_axis': None, 'reach_m': None}
              for i in range(64)]
    params = {'generation_id': 'g' * 32, 'version': 1, 'scene': 'M10C_gggggggg_v1',
              'collections': {'src': 'M10C_gggggggg_v1_SRC', 'cin': 'M10C_gggggggg_v1_CIN'}, 'source_scene': 'Scene',
              'expected_fingerprints': {'source': 'm521:' + 'f' * 300, 'candidate': 'm521:' + 'e' * 300}}
    return analysis.build_params(kind, {'params': params, 'actors': [dict(a, bones_source={}) for a in actors]}, {},
                                 analysis.PROPOSALS[kind], {'events': events, 'ray_frames': list(range(600)),
                                                            'frames': {'max': 600, 'list': list(range(600)), 'extra': [],
                                                                       'start': 0, 'end': 600}})


@pytest.mark.parametrize('kind', analysis.KINDS)
def test_every_payload_passes_safe_mode_within_the_m10_budget(kind):
    body = analysis.BODIES[kind]
    params = realistic_params(kind)
    code = build_code(body, params)  # raises if safe mode rejects it
    size = payload_bytes(body, params)
    nodes = sum(1 for _ in ast.walk(ast.parse(code)))
    assert size < 140_000, size
    assert nodes < 14_000, nodes


@pytest.mark.parametrize('kind', analysis.KINDS)
def test_every_payload_samples_with_frame_set_and_restores(kind):
    linked = link_payload(analysis.BODIES[kind])
    assert 'scene.frame_set(frame)' in linked and 'cm_restore(scene, objects, baseline)' in linked
    assert 'ca_data_restore(scene, data_state)' in linked and 'scene.camera = camera_before' in linked
    # Ownership and freshness are checked by ca_target before the first frame_set of the sampler.
    assert linked.index('def ca_target') < linked.index('def ca_run')
    run = linked[linked.index('def ca_run'):]
    assert run.index('ca_target(params)') < run.index('scene.frame_set(frame)')


def test_linker_ships_only_what_each_analysis_reaches():
    motion = link_payload(analysis.BODIES['motion'])
    view = link_payload(analysis.BODIES['visibility'])
    assert 'def ca_ray' not in motion and 'def pq_collision_frame' not in motion
    assert 'def ca_ray' in view and 'def ca_collide' not in view
    assert 'def pq_collision_frame' in link_payload(analysis.BODIES['collisions'])
    assert 'def contact_query' in link_payload(analysis.BODIES['contacts'])


def test_payload_source_never_uses_forbidden_constructs():
    for text in (analysis._PAYLOAD, analysis._RUN_TEMPLATE):
        for forbidden in ('lambda', 'bpy_extras', 'numpy', '__class__', 'hasattr(bpy.types', 'import os', 'import time',
                          'hashlib', 'driver_add('):
            assert forbidden not in text, forbidden
        assert 'getattr(' not in text and 'setattr(' not in text


def test_six_transient_handlers_registered_and_none_read_only():
    load_handlers()
    for name, kind in analysis.TOOL_KIND.items():
        assert name in TRANSIENT and name not in READ_ONLY
        assert HANDLERS[name].__module__ == analysis.__name__
    assert set(analysis.TOOL_KIND) == set(TRANSIENT)


class Captured(Exception):
    pass


class Recorder:
    def __init__(self, reply=None):
        self.bodies = []
        self.params = []
        self.reply = reply

    def run(self, body, params=None):
        self.bodies.append(body)
        self.params.append(params)
        if self.reply is None:
            raise Captured()
        return self.reply(params)


@pytest.mark.parametrize('kind', analysis.KINDS)
def test_scene_mode_first_payload_is_the_sampler(kind, tmp_path):
    """The transient contract (tests/unit/test_m31_hints.py): the first payload already changes the frame."""
    bridge = Recorder()
    with pytest.raises(Captured):
        analysis.run_tool(kind, Context(bridge, Storage(tmp_path), 'op'), Request(object_name='Rig', options={}))
    assert len(bridge.bodies) == 1 and 'frame_set(' in link_payload(bridge.bodies[0])
    assert bridge.params[0]['scene_name'] is None and bridge.params[0]['actors'][0]['object'] == 'Rig'


def test_scene_mode_needs_explicit_actors(tmp_path):
    with pytest.raises(ValueError, match='CINEMATIC_ACTORS_REQUIRED'):
        analysis.run_tool('composition', Context(Recorder(), Storage(tmp_path), 'op'), Request(options={}))
    with pytest.raises(ValueError, match='CINEMATIC_REFERENCE_REQUIRED'):
        analysis.run_tool('composition', Context(Recorder(), Storage(tmp_path), 'op'), Request(actor=A, object_name='Rig'))


@pytest.mark.parametrize('request_kwargs, code', [
    ({'options': {'unknown': 1}}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ({'apply': True}, 'CINEMATIC_ANALYSIS_READ_ONLY'),
    ({'brief': {}}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ({'options': {'thresholds': {'quality_score': 1}}}, 'CINEMATIC_OPTION_UNSUPPORTED'),
])
def test_requests_are_closed(request_kwargs, code, tmp_path):
    kwargs = dict({'object_name': 'Rig'}, **request_kwargs)
    with pytest.raises(ValueError, match=code):
        analysis.run_tool('composition', Context(Recorder(), Storage(tmp_path), 'op'), Request(**kwargs))


def test_motion_refuses_sparse_sampling(tmp_path):
    with pytest.raises(ValueError, match='CINEMATIC_DENSE_SAMPLING_REQUIRED'):
        analysis.run_tool('motion', Context(Recorder(), Storage(tmp_path), 'op'), Request(object_name='Rig', options={'step': 2}))
    plan = analysis._frame_plan('motion', {}, {'frame_start': 1, 'frame_end': 2000})
    assert plan['dense'] is True and plan['step'] is None  # the payload refuses > 600 dense frames itself


def test_frame_plan_bounds_and_timeline():
    timeline = {'frame_start': 1, 'frame_end': 96}
    with pytest.raises(ValueError, match='CINEMATIC_RANGE_OUTSIDE_TIMELINE'):
        analysis._frame_plan('composition', {'frame_start': 0, 'frame_end': 10}, timeline)
    with pytest.raises(ValueError, match='CINEMATIC_SAMPLING_CAPACITY'):
        analysis._frame_plan('contacts', {}, timeline, explicit=range(601))
    plan = analysis._frame_plan('collisions', {'step': 4}, timeline, extra=[3, 50])
    assert plan['step'] == 4 and plan['extra'] == [3, 50] and plan['max'] == 600


def test_thresholds_are_proposals_with_override_provenance():
    values, overrides = analysis.thresholds_for('visibility', {'min_visible_fraction': 0.3, 'min_screen_height': {'WIDE': 0.1}})
    assert values['min_visible_fraction'] == 0.3 and values['min_screen_height']['WIDE'] == 0.1
    assert values['min_screen_height']['MEDIUM'] == analysis.SHOT_MIN_HEIGHT['MEDIUM']
    assert overrides == ['min_screen_height', 'min_visible_fraction']
    assert analysis.PROPOSALS['visibility']['min_screen_height']['WIDE'] == 0.15  # defaults untouched
    with pytest.raises(ValueError, match='CINEMATIC_NUMBER_INVALID'):
        analysis.thresholds_for('collisions', {'error_depth_m': float('nan')})


# ---------------------------------------------------------------- plan mode end to end with a synthetic bridge

def saved_plan(tmp_path, **state_changes):
    storage = Storage(tmp_path)
    plan = {'schema': 'production.cinematic.v1', 'brief': {}, 'beats': None,
            'shots': [{'shot_id': 'S1', 'frame_start': 1, 'frame_end': 4, 'camera_id': 'cam_a', 'shot_type': 'MEDIUM',
                       'subjects': [A]},
                      {'shot_id': 'S2', 'frame_start': 5, 'frame_end': 8, 'camera_id': 'cam_b', 'shot_type': 'WIDE',
                       'subjects': [A, 'not-an-actor']}],
            'timeline': {'fps': 24, 'fps_base': 1.0, 'frame_start': 1, 'frame_end': 8},
            'actors': [{'actor_id': A, 'object': 'A', 'meshes': ['A_Body'], 'fingerprint': 'fa', 'role': 'PROTAGONIST'}],
            'actor_registry_key': 'k', 'source': {'scene': 'Scene', 'fingerprint': 'src', 'settings': {}, 'filepath': ''},
            'asset': {'role': 'FIXTURE', 'asset_id': 'x', 'path': None}, 'exclude_objects': [],
            'backend_fingerprint': store.backend_fingerprint()}
    reference, plan = store.save(storage, 'op-plan', plan)
    state = {'state': 'CINEMATIC_CANDIDATE', 'assembly': {'version': 1, 'scene': 'M10C_x_v1',
                                                          'collections': {'src': 's', 'cin': 'c'}},
             'fingerprints': {'source': 'src', 'candidate': 'cand'},
             'cameras': {'cam_a': {'object': 'CamA'}, 'cam_b': {'object': 'CamB'}}}
    state.update(state_changes)
    store.record(Path(reference), state)
    return storage, reference


def synthetic_result(params, restored=True, filepath=''):
    rows = []
    for frame in range(1, 9):
        camera = 'CamA' if frame < 5 else 'CamB'
        row = {'f': frame, 'cam': {'name': camera, 'type': 'CAMERA', 'position': [0, -6 if frame < 5 else 6, 1],
                                   'rotation': [1, 0, 0, 0], 'lens': 50.0}}
        row['a'] = {A: {'n': 8, 'front': 8, 'inside': 8, 'bbox': [0.4, 0.1, 0.6, 0.9], 'depth': [5.0, 6.0], 'near': False,
                        'far': False, 'center': [0.5, 0.5, 6.0], 'head': [0.5, 0.85, 6.0], 'root': [0.5, 0.1, 6.0],
                        'distance': 6.0}}
        rows.append(row)
    return {'scene': 'M10C_x_v1', 'fps': 24.0, 'unit_scale': 1.0, 'resolution': [1920, 1080],
            'coverage': {'frame_start': 1, 'frame_end': 8, 'step': 1, 'step_source': 'DEFAULT', 'frames_sampled': 8},
            'frames': list(range(1, 9)),
            'actors': {A: {'object': 'A', 'meshes': ['A_Body'], 'meshes_source': 'DECLARED', 'in_scene': True,
                           'hidden_in_render': False, 'root_source': 'OBJECT_ORIGIN', 'head_source': 'BOUNDS_TOP_PROXY'}},
            'static': {'markers': [['CUT_S1', 1, 'CamA'], ['CUT_S2', 5, 'CamB']]}, 'rows': rows, 'hidden_skipped': {},
            'objects': {}, 'capacity': {}, 'entities': [],
            'restoration': {'state_digest_before': 'd', 'state_digest_after': 'd' if restored else 'other',
                            'restored': restored, 'source_before': [1, 0.0, None], 'source_after': [1, 0.0, None]},
            'fingerprints_before': {'source': 'src', 'candidate': 'cand'},
            'fingerprints_after': {'source': 'src', 'candidate': 'cand'}, 'filepath': filepath}


def test_plan_mode_sends_identity_freshness_and_plan_shots(tmp_path):
    storage, reference = saved_plan(tmp_path)
    bridge = Recorder(synthetic_result)
    report = analysis.run_tool('composition', Context(bridge, storage, 'op'), Request(cinematic=reference))
    params = bridge.params[0]
    assert len(bridge.bodies) == 1 and params['expected_fingerprints'] == {'source': 'src', 'candidate': 'cand'}
    assert params['scene'] == 'M10C_x_v1' and params['frames']['start'] == 1 and params['frames']['end'] == 8
    assert report['mode'] == 'PLAN' and report['state_restored'] is True and report['shots_source'] == 'PLAN'
    assert report['ignored_shot_subjects'] == ['not-an-actor'] and report['visual_verification'] == 'required'
    assert {r['shot_id'] for r in report['per_shot']} == {'S1', 'S2'}
    assert not any('score' in key.lower() for key in report)


def test_plan_mode_reports_an_unrestored_state_as_error(tmp_path):
    storage, reference = saved_plan(tmp_path)
    report = analysis.run_tool('composition', Context(Recorder(lambda p: synthetic_result(p, restored=False)), storage, 'op'),
                               Request(cinematic=reference))
    assert report['status'] == 'error' and report['passed'] is False and report['state_restored'] is False
    assert report['errors'][0]['code'] == 'CINEMATIC_STATE_NOT_RESTORED'


def test_plan_mode_discards_a_result_from_another_file(tmp_path):
    storage, reference = saved_plan(tmp_path)
    bridge = Recorder(lambda p: synthetic_result(p, filepath=str(tmp_path / 'other.blend')))
    with pytest.raises(ValueError, match='CINEMATIC_STALE_ASSET'):
        analysis.run_tool('composition', Context(bridge, storage, 'op'), Request(cinematic=reference))


@pytest.mark.parametrize('changes, code', [
    ({'uncertain_operation': True}, 'CINEMATIC_OPERATION_UNCERTAIN'),
    ({'state': 'PLANNED'}, 'CINEMATIC_STATE_INVALID'),
])
def test_plan_mode_refuses_blocked_plans_before_any_payload(changes, code, tmp_path):
    storage, reference = saved_plan(tmp_path, **changes)
    bridge = Recorder()
    with pytest.raises(ValueError, match=code):
        analysis.run_tool('composition', Context(bridge, storage, 'op'), Request(cinematic=reference))
    assert bridge.bodies == []


def test_plan_mode_refuses_scene_options_and_unknown_actors(tmp_path):
    storage, reference = saved_plan(tmp_path)
    for options, code in (({'shots': []}, 'CINEMATIC_PLAN_SHOTS_FIXED'), ({'scene': 'Scene'}, 'CINEMATIC_OPTION_UNSUPPORTED'),
                          ({'actors': [B]}, 'ACTOR_NOT_IN_PLAN'), ({'actor_bones': {B: {'head_bone': 'h'}}}, 'ACTOR_NOT_IN_PLAN')):
        with pytest.raises(ValueError, match=code):
            analysis.run_tool('composition', Context(Recorder(), storage, 'op'), Request(cinematic=reference, options=options))


# ---------------------------------------------------------------- pure helpers

def test_runs_and_frame_runs():
    assert analysis.runs([1, 2, 3, 5, 6], [True, True, False, True, True]) == [[1, 2, 2], [5, 6, 2]]
    assert analysis.frame_runs([5, 1, 2, 3, 9]) == [[1, 3], [5, 5], [9, 9]]


def test_axis_side_flips_across_the_action_line_and_detects_degenerate_cameras():
    side_a, dist_a = analysis.axis_side([0, 0, 0], [1, 0, 0], [0.5, -6, 1])
    side_b, dist_b = analysis.axis_side([0, 0, 0], [1, 0, 0], [0.5, 6, 1])
    assert side_a == -side_b != 0 and dist_a == dist_b == 6.0
    assert analysis.axis_side([0, 0, 0], [0, 0, 0], [1, 1, 0]) == (0, 0.0)


def test_screen_geometry_helpers():
    assert analysis.clipped_area([-1, -1, 2, 2]) == 1.0 and analysis.clipped_height([0, -0.5, 1, 0.5]) == 0.5
    assert analysis.overlap_ratio([0, 0, 0.5, 0.5], [0.25, 0.25, 0.75, 0.75]) == pytest.approx(0.25)
    assert analysis.overlap_ratio([0, 0, 0.5, 0.5], None) == 0.0
    assert analysis.union_coverage([[0, 0, 0.5, 1]]) == pytest.approx(0.5, abs=1 / 64)
    assert analysis.thirds_distance([1 / 3, 2 / 3]) == pytest.approx(0.0)
    assert analysis.quaternion_angle([1, 0, 0, 0], [0.7071068, 0, 0, 0.7071068]) == pytest.approx(90.0, abs=1e-3)


@pytest.mark.parametrize('rows, gaps', [
    ([['STRIP', 'A', 5.0, 78.0, 'HOLD_FORWARD', 't']], [[1, 4]]),
    ([['STRIP', 'A', 5.0, 78.0, 'HOLD', 't']], []),
    ([['STRIP', 'A', 5.0, 20.0, 'NOTHING', 't']], [[1, 4], [21, 96]]),
    ([['STRIP', 'A', 1.0, 10.0, 'NOTHING', 't'], ['STRIP', 'B', 30.0, 40.0, 'HOLD', 't']], [[11, 29]]),
    ([['ACTIVE', 'A', 10.0, 20.0, 'HOLD', None]], []),
    ([], [[1, 96]]),
])
def test_action_coverage_follows_nla_extrapolation(rows, gaps):
    assert analysis.coverage_gaps(rows, 1, 96) == gaps


def test_shot_normalisation_keeps_declared_subjects_and_reports_unknown_ones():
    shots, dropped = analysis.normalize_shots([{'shot_id': 'S2', 'frame_start': 10, 'frame_end': 20, 'subjects': [A, 'x'],
                                                'shot_type': 'BOGUS', 'camera_id': 'c'},
                                               {'frame_start': 1, 'frame_end': 9}], [A, B], {'c': {'object': 'CamC'}})
    assert [s['shot_id'] for s in shots] == ['SHOT_2', 'S2'] and dropped == ['x']
    assert shots[1]['subjects'] == [A] and shots[1]['shot_type'] == 'CUSTOM' and shots[1]['camera_object'] == 'CamC'
    assert shots[0]['subjects'] == [A, B] and shots[0]['subjects_declared'] is False


def test_events_are_normalised_or_reported_never_silently_dropped():
    rows = [{'event_id': 'ok', 'kind': 'REACH', 'actor_id': A, 'frame': 10, 'effector': 'hand', 'target': B},
            {'event_id': 'no_effector', 'kind': 'REACH', 'actor_id': A, 'frame': 10, 'target': B},
            {'event_id': 'gaze', 'kind': 'GAZE', 'actor_id': A, 'frame': 10, 'head_bone': 'head', 'target': B},
            {'event_id': 'bad_kind', 'kind': 'DANCE', 'actor_id': A, 'frame': 10},
            {'event_id': 'window', 'kind': 'REACH', 'actor_id': A, 'frame': 30, 'frame_start': 10, 'frame_end': 20,
             'effector': 'hand', 'target': B},
            {'event_id': 'stranger', 'kind': 'REACH', 'actor_id': 'z' * 16, 'frame': 1}]
    events, ignored = analysis._events(rows, [A, B])
    assert [e['event_id'] for e in events] == ['ok'] and events[0]['contact'] is True and events[0]['index'] == 0
    assert events[0]['target'] == {'actor_id': B} and events[0]['effector'] == {'bone': 'hand'}
    assert {i['event_id']: i['reason'] for i in ignored} == {
        'no_effector': 'EFFECTOR_REQUIRED', 'gaze': 'GAZE_FORWARD_AXIS_REQUIRED', 'bad_kind': 'KIND_UNSUPPORTED',
        'window': 'FRAME_WINDOW_INVALID', 'stranger': 'ACTOR_NOT_ANALYSED'}


def test_explicit_events_are_authoritative_over_stored_interactions():
    rows, sources = analysis._plan_events({'interactions': [{'x': 1}]}, {'interactions': {'events': [{'y': 2}]}},
                                          {'events': [{'z': 3}]})
    assert rows == [{'z': 3}] and sources == ['OPTIONS']
    rows, sources = analysis._plan_events({'interactions': [{'x': 1}]}, {'interactions': {'events': [{'y': 2}]}}, {})
    assert rows == [{'y': 2}] and sources == ['STATE']


def test_bones_are_explicit_never_guessed():
    bones, source = analysis._bones_for(A, {'bones': {'head': 'Head_jnt'}}, {A: {'root_bone': 'Root'}})
    assert bones == {'head_bone': 'Head_jnt', 'root_bone': 'Root'} and source == {'head_bone': 'REGISTRY', 'root_bone': 'OPTIONS'}
    bones, source = analysis._bones_for(A, {'meshes': ['x']}, None)
    assert bones == {'head_bone': None, 'root_bone': None} and source == {'head_bone': 'NONE', 'root_bone': 'NONE'}


# ---------------------------------------------------------------- synthetic analyses

def screen_row(x, y=0.5, inside=8, front=8, bbox=None, vis=None, head=None, world=None, near=False):
    item = {'n': 8, 'front': front, 'inside': inside, 'bbox': bbox or [x - 0.1, y - 0.4, x + 0.1, y + 0.4],
            'depth': [5.0, 6.0], 'near': near, 'far': False, 'center': [x, y, 6.0] if front else None,
            'head': head or [x, y + 0.35, 6.0], 'root': [x, y - 0.4, 6.0], 'distance': 6.0}
    if vis is not None:
        item['vis'] = vis
    if world is not None:
        item['w'] = world
    return item


def target_for(shots, actors=(A, B), timeline=None):
    return {'mode': 'PLAN', 'selected': list(actors), 'shots': shots, 'timeline': timeline, 'props': [], 'gaze': []}


def result_for(rows, markers=(), actors=(A, B)):
    frames = [r['f'] for r in rows]
    return {'rows': rows, 'fps': 24.0, 'coverage': {'frame_start': frames[0], 'frame_end': frames[-1], 'step': 1},
            'static': {'markers': list(markers), 'coverage': {}, 'caches': []}, 'hidden_skipped': {}, 'objects': {},
            'actors': {a: {'in_scene': True, 'hidden_in_render': False, 'root_source': 'OBJECT_ORIGIN',
                           'head_source': 'HEAD_BONE'} for a in actors}}


def shot(shot_id, start, end, subjects=(A, B), shot_type='MEDIUM', camera=None):
    return {'shot_id': shot_id, 'frame_start': start, 'frame_end': end, 'shot_type': shot_type, 'camera_id': None,
            'camera_object': camera, 'subjects': list(subjects), 'subjects_declared': True, 'axis': None}


def test_composition_measures_centre_and_keeps_observations_out_of_findings():
    rows = [{'f': f, 'cam': {'name': 'C', 'type': 'CAMERA'}, 'a': {A: screen_row(0.5), B: screen_row(1.4, inside=0)}}
            for f in range(1, 11)]
    report = analysis.analyze_composition(target_for([shot('S1', 1, 10)]), result_for(rows),
                                          analysis.thresholds_for('composition')[0])
    row = next(r for r in report['per_shot'] if r['actor_id'] == A)
    assert row['screen_center_x'] == {'min': 0.5, 'median': 0.5, 'max': 0.5} and row['in_frame_fraction'] == 1.0
    assert any(o['code'] == 'SUBJECT_CENTERED' and o['actor_id'] == A for o in report['observations'])
    out = [f for f in report['findings'] if f['code'] == 'SUBJECT_OUT_OF_FRAME']
    assert out and out[0]['actor_id'] == B and out[0]['frames'] == [1, 10] and out[0]['severity'] == 'WARNING'
    assert all(o['kind'] == 'CREATIVE_OBSERVATION' for o in report['observations'])
    assert not any(f['code'] == 'SUBJECT_CENTERED' for f in report['findings'])


def test_composition_flags_missing_camera_and_clipping():
    rows = [{'f': 1, 'cam': None, 'a': {A: None}},
            {'f': 2, 'cam': {'name': 'C', 'type': 'CAMERA'}, 'a': {A: screen_row(0.5, near=True)}}]
    report = analysis.analyze_composition(target_for([shot('S1', 1, 2, (A,))], (A,)), result_for(rows, actors=(A,)),
                                          analysis.thresholds_for('composition')[0])
    codes = {f['code']: f for f in report['findings']}
    assert codes['NO_ACTIVE_CAMERA']['severity'] == 'ERROR' and codes['NO_ACTIVE_CAMERA']['frames'] == [1, 1]
    assert codes['NEAR_CLIP_CUT']['confidence'] == 'BOUNDS_CONSERVATIVE'


def continuity_rows(cam_y_after):
    rows = []
    for f, x in ((3, 0.40), (4, 0.39), (5, 0.38), (6, 0.37)):
        after = f >= 5
        world_a = {'center': [0.0, 0.0, 1.0], 'root': [0.0, 0.0, 0.0], 'head': [0, 0, 1.6]}
        world_b = {'center': [1.0, 0.0, 1.0], 'root': [1.0, 0.0, 0.0], 'head': [1, 0, 1.6]}
        screen_x = (1.0 - x) if after and cam_y_after > 0 else x
        rows.append({'f': f, 'cam': {'name': 'B' if after else 'A', 'type': 'CAMERA',
                                     'position': [0.5, cam_y_after if after else -6.0, 1.0]},
                     'a': {A: screen_row(screen_x, world=world_a, vis={'rays': 8, 'clear': 8, 'occ': {}, 'head': 'VISIBLE', 'head_by': None}),
                           B: screen_row(0.6, world=world_b, vis={'rays': 8, 'clear': 8, 'occ': {}, 'head': 'VISIBLE', 'head_by': None})}})
    return rows


def test_continuity_detects_the_180_degree_break_and_allows_it_per_cut():
    shots = [shot('S1', 1, 4), shot('S2', 5, 8)]
    thresholds = analysis.thresholds_for('continuity')[0]
    crossed = analysis.analyze_continuity(target_for(shots, timeline={'frame_start': 1, 'frame_end': 8}),
                                          result_for(continuity_rows(6.0), markers=[['m', 5, 'B']]), thresholds, {})
    codes = [f['code'] for f in crossed['findings']]
    assert 'AXIS_CROSSED' in codes and 'SCREEN_DIRECTION_FLIP' in codes
    assert crossed['cuts'][0]['camera_side'] == [-1, 1]
    allowed = analysis.analyze_continuity(target_for(shots, timeline={'frame_start': 1, 'frame_end': 8}),
                                          result_for(continuity_rows(6.0), markers=[['m', 5, 'B']]), thresholds,
                                          {'allow_axis_break': ['S1>S2']})
    codes = {f['code']: f['severity'] for f in allowed['findings']}
    assert codes.get('AXIS_BREAK_ALLOWED') == 'INFO' and 'AXIS_CROSSED' not in codes and 'SCREEN_DIRECTION_FLIP' not in codes
    same_side = analysis.analyze_continuity(target_for(shots, timeline={'frame_start': 1, 'frame_end': 8}),
                                            result_for(continuity_rows(-4.0), markers=[['m', 5, 'B']]), thresholds, {})
    assert same_side['cuts'][0]['axis_status'] == 'SAME_SIDE'
    assert not any(f['code'] == 'AXIS_CROSSED' for f in same_side['findings'])


def test_continuity_checks_the_timeline_and_the_planned_cuts():
    shots = [shot('S1', 1, 4, camera='A'), shot('S2', 7, 8, camera='B'), shot('S3', 8, 8, camera='B')]
    report = analysis.analyze_continuity(target_for(shots, timeline={'frame_start': 1, 'frame_end': 10}),
                                         result_for(continuity_rows(6.0), markers=[['m', 5, 'B']]),
                                         analysis.thresholds_for('continuity')[0], {})
    codes = {}
    for f in report['findings']:
        codes.setdefault(f['code'], []).append(f['frames'])
    assert codes['TIMELINE_GAP'] == [[5, 6]] and codes['SHOT_OVERLAP'] == [[8, 8]]
    assert codes['TIMELINE_END_UNCOVERED'] == [[9, 10]] and codes['CUT_NOT_IN_PLAN'] == [[5, 5]]
    assert codes['PLANNED_CUT_WITHOUT_MARKER'] == [[7, 7], [8, 8]]
    # Two consecutive shots on the same camera need no marker.
    same = analysis.analyze_continuity(target_for([shot('S1', 1, 4, camera='A'), shot('S2', 5, 8, camera='A')],
                                                  timeline={'frame_start': 1, 'frame_end': 8}),
                                       result_for(continuity_rows(6.0)), analysis.thresholds_for('continuity')[0], {})
    assert not any(f['code'] == 'PLANNED_CUT_WITHOUT_MARKER' for f in same['findings'])
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_INVALID'):
        analysis.analyze_continuity(target_for(shots), result_for(continuity_rows(6.0)), analysis.thresholds_for('continuity')[0],
                                    {'allow_axis_break': 'yes'})


def test_visibility_occlusion_runs_intent_and_face_events():
    rows = []
    for f in range(1, 7):
        occluded = 2 <= f <= 4
        vis = {'rays': 10, 'clear': 2 if occluded else 10, 'occ': {'Wall': 8} if occluded else {},
               'head': 'OCCLUDED' if occluded else 'VISIBLE', 'head_by': 'Wall' if occluded else None}
        rows.append({'f': f, 'a': {A: screen_row(0.5, vis=vis)}})
    result = result_for(rows, actors=(A,))
    thresholds = analysis.thresholds_for('visibility')[0]
    report = analysis.analyze_visibility(target_for([shot('S1', 1, 6, (A,), 'MEDIUM')], (A,)), result, thresholds,
                                         [{'actor_id': A, 'frame': 3, 'label': 'gasp'}], [])
    occlusion = [f for f in report['findings'] if f['code'] == 'EXCESSIVE_OCCLUSION']
    assert occlusion and occlusion[0]['frames'] == [2, 4] and occlusion[0]['measured']['occluder'] == 'Wall'
    assert occlusion[0]['measured']['occluder_class'] == 'ENVIRONMENT' and occlusion[0]['threshold_source'] == 'PROPOSAL'
    face = [f for f in report['findings'] if f['code'] == 'FACE_HIDDEN_AT_EVENT']
    assert face and face[0]['frames'] == [3, 3] and face[0]['measured']['occluder'] == 'Wall'
    intended = analysis.analyze_visibility(target_for([shot('S1', 1, 6, (A,))], (A,)), result, thresholds, [],
                                           [{'actor_id': A, 'frame_start': 1, 'frame_end': 6, 'occluder': 'Wall'}])
    assert [f['code'] for f in intended['findings']] == ['INTENDED_OCCLUSION']


def test_visibility_flags_small_actors_per_shot_type_and_props():
    rows = [{'f': f, 'a': {A: screen_row(0.5, bbox=[0.45, 0.45, 0.55, 0.5],
                                         vis={'rays': 10, 'clear': 6, 'occ': {'Cup': 4}, 'head': 'VISIBLE', 'head_by': None})}}
            for f in range(1, 4)]
    result = result_for(rows, actors=(A,))
    result['objects'] = {'Cup': {'role': 'prop_copy', 'owned': True, 'actor_id': None}}
    report = analysis.analyze_visibility(target_for([shot('S1', 1, 3, (A,), 'CLOSE_UP')], (A,)), result,
                                         analysis.thresholds_for('visibility')[0], [], [])
    codes = {f['code']: f for f in report['findings']}
    assert codes['ACTOR_TOO_SMALL_FOR_SHOT_TYPE']['threshold'] == 0.7
    assert codes['PROP_OCCLUDING_SUBJECT']['measured']['prop'] == 'Cup'


def pair_rows(spec):
    """spec: {frame: (count, depth, unknown, clearance)}."""
    return [{'f': f, 'pairs': [[A, B, c, d, ['B_Body'] if c else [], u, [], cl, 0.0]]} for f, (c, d, u, cl) in sorted(spec.items())]


def test_collisions_separate_intent_from_accident_and_never_read_unknown_as_zero():
    spec = {f: (0, 0.0, [], 0.1) for f in range(1, 21)}
    spec.update({f: (4, 0.05, [], None) for f in (3, 4, 5)})
    spec.update({f: (2, 0.002, [], None) for f in (10, 11)})
    spec.update({f: (0, 0.0, ['Cloth'], None) for f in (15,)})
    events = [{'event_id': 'touch', 'actor_id': A, 'target': {'actor_id': B}, 'contact': True, 'frame_start': 10, 'frame_end': 11}]
    pairs, findings, observations = analysis.classify_collisions(pair_rows(spec), events, analysis.thresholds_for('collisions')[0], 1)
    by_code = {f['code']: f for f in findings}
    assert by_code['ACCIDENTAL_COLLISION']['frames'] == [3, 5] and by_code['ACCIDENTAL_COLLISION']['severity'] == 'ERROR'
    assert by_code['INTENDED_CONTACT']['measured']['event_id'] == 'touch' and by_code['INTENDED_CONTACT']['severity'] == 'INFO'
    assert by_code['COLLISION_UNKNOWN_OPEN_MESH']['confidence'] == 'UNKNOWN'
    assert pairs[0]['unknown_colliders'] == ['Cloth'] and pairs[0]['max_depth_m'] == 0.05
    sparse = analysis.classify_collisions(pair_rows(spec), [], analysis.thresholds_for('collisions')[0], 4)[1]
    assert all(f['confidence'] in ('SPARSE_SAMPLES', 'UNKNOWN') for f in sparse)
    assert sum(f['code'] == 'ACCIDENTAL_COLLISION' for f in sparse) == 1  # without the event the touch is no longer intended
    assert any(f['code'] == 'ACCIDENTAL_CONTACT_WITHIN_TOLERANCE' for f in sparse)


def test_collision_report_blocks_on_unknown_and_never_certifies_freedom():
    rows = pair_rows({1: (0, 0.0, ['Cloth'], None), 2: (0, 0.0, [], 0.2)})
    result = result_for(rows)
    report = analysis.analyze_collisions(target_for([]), result, analysis.thresholds_for('collisions')[0], [])
    assert report['metrics']['unknown'] is True and report['collision_coverage']['continuous_collision_freedom_certified'] is False
    status, passed = analysis._status(report['findings'], True, report['metrics']['unknown'])
    assert passed is False


def contact_samples(distances, inside_frames=(), local_drift=0.0):
    rows = []
    for f, d in distances.items():
        inside = f in inside_frames
        rows.append({'f': f, 'ev': [{'e': 0, 'f': f, 'root': [0, 0, 0], 'troot': [1, 0, 0], 'eff': [0.9, 0, 1],
                                     'd': 0.0 if inside else d, 'inside': inside, 'depth': 0.002 if inside else 0.0,
                                     'local': [0.1 + local_drift * f, 0, 0]}], 'pairs': []})
    return rows


def contact_event(start=10, end=14, frame=10, contact=True):
    return {'index': 0, 'event_id': 'touch', 'kind': 'REACH', 'mode': 'CONTACT', 'actor_id': A, 'frame': frame,
            'frame_start': start, 'frame_end': end, 'target': {'actor_id': B}, 'effector': {'bone': 'hand'}, 'contact': contact}


def test_contacts_timing_alignment_penetration_and_sliding():
    thresholds = analysis.thresholds_for('contacts')[0]
    distances = {f: (0.3 if f < 10 else 0.0 if f <= 14 else 0.3) for f in range(4, 21)}
    report = analysis.analyze_contacts(target_for([]), result_for(contact_samples(distances, range(10, 15))), thresholds,
                                       [contact_event()])
    event = report['events'][0]
    assert event['status'] == 'CONTACT' and event['timing'] == 'ON_TIME' and event['timing_offset_frames'] == [0, 0]
    assert event['max_penetration_m'] == 0.002 and event['relative_sliding_m'] == 0.0 and event['distance_at_planned_frame_m'] == 0.0
    late = {f: (0.3 if f < 15 else 0.01) for f in range(4, 21)}
    report = analysis.analyze_contacts(target_for([]), result_for(contact_samples(late, local_drift=0.01)), thresholds, [contact_event()])
    codes = {f['code'] for f in report['findings']}
    assert report['events'][0]['timing'] == 'LATE' and {'CONTACT_TIMING_OFF', 'RELATIVE_SLIDING'} <= codes
    never = {f: 0.3 for f in range(4, 21)}
    report = analysis.analyze_contacts(target_for([]), result_for(contact_samples(never)), thresholds, [contact_event()])
    unreached = [f for f in report['findings'] if f['code'] == 'TARGET_UNREACHED']
    assert report['events'][0]['status'] == 'NO_CONTACT' and unreached[0]['measured']['min_distance_m'] == 0.3


def test_gaze_events_measure_alignment_inside_the_window():
    event = {'index': 0, 'event_id': 'look', 'kind': 'GAZE', 'mode': 'GAZE', 'actor_id': A, 'frame': 10, 'frame_start': 10,
             'frame_end': 12, 'target': {'actor_id': B}, 'effector': {}, 'contact': False}
    rows = [{'f': f, 'ev': [{'e': 0, 'f': f, 'root': [0, 0, 0], 'angle': 40.0 if f < 11 else 5.0}], 'pairs': []}
            for f in range(8, 15)]
    thresholds = analysis.thresholds_for('contacts')[0]
    report = analysis.analyze_contacts(target_for([]), result_for(rows), thresholds, [event])
    gaze = report['events'][0]
    assert gaze['first_aligned_frame'] == 11 and gaze['timing_offset_frames'] == 1
    assert gaze['aligned_fraction_in_window'] == pytest.approx(2 / 3, abs=1e-6) and not report['findings']
    away = [dict(r, ev=[dict(r['ev'][0], angle=170.0)]) for r in rows]
    report = analysis.analyze_contacts(target_for([]), result_for(away), thresholds, [event])
    assert [f['code'] for f in report['findings']] == ['GAZE_NOT_ALIGNED']


def test_eyeline_direction_flip_across_a_cut_is_reported():
    rows = continuity_rows(6.0)
    for r in rows:
        r['gz'] = [{'g': 0, 'angle': 3.0, 'screen': [0.05 if r['f'] < 5 else -0.05, 0.0]}]
    target = target_for([shot('S1', 1, 4), shot('S2', 5, 8)], timeline={'frame_start': 1, 'frame_end': 8})
    target['gaze'] = [{'index': 0, 'actor_id': A}]
    report = analysis.analyze_continuity(target, result_for(rows, markers=[['m', 5, 'B']]),
                                         analysis.thresholds_for('continuity')[0], {})
    flips = [f for f in report['findings'] if f['code'] == 'EYELINE_DIRECTION_FLIP']
    assert flips and flips[0]['measured']['screen_dx'] == [0.05, -0.05] and report['cuts'][0]['gaze'][0]['angle_deg'] == [3.0, 3.0]


def test_gaze_specs_need_explicit_head_bone_and_axis():
    actors = [{'actor_id': A, 'head_bone': None}, {'actor_id': B, 'head_bone': 'head'}]
    with pytest.raises(ValueError, match='GAZE_HEAD_BONE_REQUIRED'):
        analysis._gaze_specs({'gaze': [{'actor_id': A, 'forward_axis': [0, 1, 0], 'target_actor_id': B}]}, actors)
    with pytest.raises(ValueError, match='GAZE_FORWARD_AXIS_REQUIRED'):
        analysis._gaze_specs({'gaze': [{'actor_id': B, 'forward_axis': [0, 0, 0], 'target_actor_id': A}]}, actors)
    spec = analysis._gaze_specs({'gaze': [{'actor_id': B, 'forward_axis': [0, 0, 1], 'target_point': [1, 2, 3]}]}, actors)
    assert spec[0]['head_bone'] == 'head' and spec[0]['target_point'] == [1.0, 2.0, 3.0]


def test_contact_survey_separates_planned_and_unplanned_contacts():
    spec = {f: (0, 0.0, [], 0.5) for f in range(1, 31)}
    spec.update({f: (3, 0.04, [], None) for f in (3, 4)})
    spec.update({f: (0, 0.0, [], 0.01) for f in (20, 21)})
    survey = analysis.contact_survey(pair_rows(spec), [contact_event(19, 22, 20)], analysis.thresholds_for('contacts')[0])
    assert [(r['frames'], r['planned_event']) for r in survey] == [([3, 4], None), ([20, 21], 'touch')]


def motion_rows():
    rows = []
    for f in range(1, 21):
        camera = 'CamA' if f < 10 else 'CamB'
        cam_x = 0.0 if f < 10 else (0.01 * f if f < 15 else 0.01 * f + 1.0)
        b_y = 0.0 if f < 17 else 1.0
        rows.append({'f': f, 'cam': {'name': camera, 'type': 'CAMERA', 'position': [cam_x, -6 if f < 10 else 6, 1],
                                     'rotation': [1, 0, 0, 0], 'lens': 50.0},
                     'm': {'roots': {A: [0.02 * f, 0, 0, 1, 0, 0, 0], B: [1, b_y, 0, 1, 0, 0, 0]},
                           'lights': {'Key': 1000.0 if f < 10 else 5000.0, 'Fill': 100.0 if f < 12 else 300.0},
                           'exposure': 0.0, 'world': [1.0]}})
    return rows


def test_motion_finds_jumps_not_explained_by_cuts_and_lighting_jumps():
    result = result_for(motion_rows(), markers=[['m', 10, 'CamB']])
    result['static']['coverage'] = {A: {'use_nla': True, 'rows': [['STRIP', 'A', 3.0, 20.0, 'HOLD_FORWARD', 't']]},
                                    B: {'use_nla': True, 'rows': [['STRIP', 'B', 1.0, 20.0, 'HOLD', 't']]}}
    report = analysis.analyze_motion(target_for([]), result, analysis.thresholds_for('motion')[0], [], [], {})
    by_code = {}
    for f in report['findings']:
        by_code.setdefault(f['code'], []).append(f)
    assert [f['frames'] for f in by_code['ROOT_JUMP']] == [[16, 17]] and by_code['ROOT_JUMP'][0]['actor_id'] == B
    assert [f['frames'] for f in by_code['CAMERA_JUMP_UNEXPLAINED']] == [[14, 15]]
    assert by_code['LIGHTING_JUMP_AT_CUT'][0]['severity'] == 'INFO' and by_code['LIGHTING_JUMP_AT_CUT'][0]['frames'] == [9, 10]
    assert by_code['LIGHTING_JUMP'][0]['frames'] == [11, 12]
    assert by_code['ACTION_GAP'][0]['frames'] == [1, 2] and by_code['ACTION_GAP'][0]['actor_id'] == A
    assert report['cuts'] == [10]


def test_motion_flags_events_on_undriven_frames_and_cache_windows():
    result = result_for(motion_rows())
    result['static']['coverage'] = {A: {'use_nla': True, 'rows': [['STRIP', 'A', 5.0, 10.0, 'NOTHING', 't']]}}
    result['static']['caches'] = [{'object': 'Cape', 'modifier': 'MeshCache'}, {'object': 'Hair', 'modifier': 'MeshCache'}]
    event = {'event_id': 'e', 'actor_id': A, 'frame': 15, 'frame_start': 15, 'frame_end': 16}
    report = analysis.analyze_motion(target_for([], (A,)), result, analysis.thresholds_for('motion')[0], [event], [], {'Cape': [1, 12]})
    codes = {f['code'] for f in report['findings']}
    assert {'EVENT_OUTSIDE_CLIP', 'PHYSICS_CACHE_GAP', 'PHYSICS_CACHE_COVERAGE_UNKNOWN'} <= codes
    assert report['caches'][0]['gaps'] == [[13, 20]]


def test_status_and_pagination():
    errors = [analysis.finding('X', 'ERROR', 'TECHNICAL')]
    assert analysis._status(errors, True) == ('warning', False)
    assert analysis._status([analysis.finding('Y', 'INFO', 'TECHNICAL')], True) == ('success', True)
    assert analysis._status([], False) == ('error', False)
    page, meta = analysis._paginate(list(range(7)), 3, 3)
    assert page == [3, 4, 5] and meta == {'total': 7, 'offset': 3, 'limit': 3, 'next_offset': 6}
    ordered = analysis._sorted_findings([analysis.finding('B', 'INFO', 'T', frames=[1, 1]),
                                         analysis.finding('A', 'ERROR', 'T', frames=[9, 9])])
    assert [f['code'] for f in ordered] == ['A', 'B']



# ---------------------------------------------------------------- quality-report provider contract

def test_registry_unknown_bones_are_missing_not_names():
    record = {'bones': {'head': 'UNKNOWN', 'root': 'pelvis_root'}, 'head_bone': 'UNKNOWN'}
    bones, source = analysis._bones_for(A, record, None)
    assert bones == {'head_bone': None, 'root_bone': 'pelvis_root'} and source['head_bone'] == 'NONE'


def test_collision_metrics_publish_the_max_penetration_or_none_when_unknown():
    thresholds = analysis.thresholds_for('collisions')[0]
    spec = {f: (0, 0.0, [], 0.1) for f in range(1, 21)}
    spec.update({f: (4, 0.05, [], None) for f in (3, 4, 5)})
    spec.update({f: (2, 0.002, [], None) for f in (10, 11)})
    events = [{'event_id': 'touch', 'actor_id': A, 'target': {'actor_id': B}, 'contact': True, 'frame_start': 10,
               'frame_end': 11}]
    metrics = analysis.analyze_collisions(target_for([]), dict(result_for(pair_rows(spec)), entities=[[A], [B]]),
                                          thresholds, events)['metrics']
    assert metrics['max_penetration_m'] == 0.05 and metrics['max_penetration_status'] == 'MEASURED'
    assert metrics['max_accidental_penetration_m'] == 0.05 and metrics['max_intended_penetration_m'] == 0.002
    assert metrics['max_penetration_units'] == 'm'
    assert metrics['accidental_intervals'] == 1 and metrics['accidental_sampled_frames'] == 3
    spec[15] = (0, 0.0, ['Cloth'], None)
    unknown = analysis.analyze_collisions(target_for([]), result_for(pair_rows(spec)), thresholds, events)['metrics']
    assert unknown['max_penetration_m'] is None and unknown['max_penetration_status'] == 'UNKNOWN_OPEN_MESH'
    apart = analysis.analyze_collisions(target_for([]), dict(result_for([{'f': 1, 'pairs': []}]), entities=[[A], [B]]),
                                        thresholds, [])['metrics']
    assert apart['max_penetration_m'] == 0.0 and apart['max_penetration_status'] == 'MEASURED'
    alone = analysis.analyze_collisions(target_for([]), dict(result_for([{'f': 1, 'pairs': []}]), entities=[[A]]),
                                        thresholds, [])['metrics']
    assert alone['max_penetration_m'] is None and alone['max_penetration_status'] == 'NO_PAIRS'


def test_every_report_function_takes_the_quality_report_pool():
    pool = {'ctx', 'context', 'req', 'request', 'path', 'plan', 'state', 'bridge', 'storage', 'options', 'cinematic',
            'operation_id'}
    for kind, function in analysis.REPORTS.items():
        parameters = inspect.signature(function).parameters
        required = {n for n, p in parameters.items() if p.default is inspect.Parameter.empty}
        assert required <= pool and parameters['options'].default is None, kind


def collision_result(spec, entities=2):
    def reply(params):
        result = synthetic_result(params)
        result.update(rows=pair_rows(spec), entities=[[A, 'ACTOR', ['A_Body']], [B, 'ACTOR', ['B_Body']]][:entities],
                      capacity={'vertex_frames': 16, 'limit': 3_000_000, 'limit_source': 'PROPOSAL'})
        return result
    return reply


def test_collision_provider_feeds_the_quality_report_and_the_refinement_target(tmp_path):
    from blender_mcp.production.cinematic import review
    storage, reference = saved_plan(tmp_path)
    path, plan, state = store.load(storage, reference)
    spec = {f: (0, 0.0, [], 0.1) for f in range(1, 9)}
    spec.update({f: (4, 0.05, [], None) for f in (3, 4)})
    ctx = Context(Recorder(collision_result(spec)), storage, 'op')
    report = analysis.collision_report(ctx, plan, state, None)
    for key in ('passed', 'status', 'findings', 'metrics', 'state_restored', 'findings_by_severity'):
        assert key in report, key
    assert report['passed'] is False and report['status'] == 'warning' and report['state_restored'] is True
    assert report['metrics']['max_penetration_m'] == 0.05 and report['findings_by_severity'] == {'ERROR': 1}
    assert report['failures'] == ['ACCIDENTAL_COLLISION: 1 ERROR finding(s)']
    section = review.run_section(ctx, path, plan, state, review.SECTION_BY_ID['staging'])
    assert section['provider'] == 'analysis.collision_report' and section['status'] == 'FAIL'
    assert section['metrics']['max_penetration_m'] == 0.05 and section['metrics']['max_penetration_units'] == 'm'
    measured = review.measure_target(ctx, path, plan, state, {'source': 'SECTION:staging', 'target': 'max_penetration_m'})
    assert measured['value'] == 0.05 and measured['units'] == 'm'
    spec[6] = (0, 0.0, ['Cloth'], None)
    unknown_ctx = Context(Recorder(collision_result(spec)), storage, 'op')
    unknown = review.run_section(unknown_ctx, path, plan, state, review.SECTION_BY_ID['staging'])
    assert unknown['status'] == 'FAIL' and unknown['metrics']['max_penetration_m'] is None
    with pytest.raises(ValueError, match='REFINEMENT_TARGET_UNAVAILABLE'):
        review.measure_target(unknown_ctx, path, plan, state, {'source': 'SECTION:staging', 'target': 'max_penetration_m'})
    clear = {f: (0, 0.0, [], 0.1) for f in range(1, 9)}
    passed = review.run_section(Context(Recorder(collision_result(clear)), storage, 'op'), path, plan, state,
                                review.SECTION_BY_ID['staging'])
    assert passed['status'] == 'PASS' and passed['metrics']['max_penetration_m'] == 0.0


def test_unknown_only_failures_are_never_reported_bare(tmp_path):
    from blender_mcp.production.cinematic import review
    storage, reference = saved_plan(tmp_path)
    path, plan, state = store.load(storage, reference)
    spec = {f: (0, 0.0, ['Cloth'] if f == 2 else [], 0.1) for f in range(1, 9)}
    report = analysis.collision_report(Context(Recorder(collision_result(spec)), storage, 'op'), plan, state)
    assert report['passed'] is False and report['status'] == 'warning' and not report['findings_by_severity'].get('ERROR')
    section = review.normalize_section(report)
    assert section['status'] == 'FAIL' and any('COLLISION_UNKNOWN_OPEN_MESH' in e for e in section['errors'])


def test_unrestored_provider_result_is_an_error_with_its_reason(tmp_path):
    from blender_mcp.production.cinematic import review
    storage, reference = saved_plan(tmp_path)
    path, plan, state = store.load(storage, reference)
    ctx = Context(Recorder(lambda p: synthetic_result(p, restored=False)), storage, 'op')
    report = analysis.composition_report(ctx, plan, state)
    assert report['status'] == 'error' and report['passed'] is False and report['state_restored'] is False
    assert report['failures'][0].startswith('CINEMATIC_STATE_NOT_RESTORED')
    section = review.run_section(ctx, path, plan, state, review.SECTION_BY_ID['framing'])
    assert section['status'] == 'FAIL' and any('CINEMATIC_STATE_NOT_RESTORED' in e for e in section['errors'])
    ok = analysis.composition_report(Context(Recorder(synthetic_result), storage, 'op'), plan, state, {})
    assert ok['state_restored'] is True and 'failures' not in ok and isinstance(ok['metrics'], dict)
