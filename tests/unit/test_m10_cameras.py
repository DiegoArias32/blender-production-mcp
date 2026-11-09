"""M10 area C without Blender: pure trajectory/noise/focus math, cut and coverage validation, option refusals,
proposals that never reach Blender, ledger bookkeeping of applied steps, payload budgets, the conversion of
interaction events into state['interactions'] and the finalize gates (CINEMATIC_CANDIDATE -> ASSEMBLED)."""
from __future__ import annotations

import ast
from copy import deepcopy
import math
from pathlib import Path
import re

import pytest

from blender_mcp.production.cinematic import actors as actors_module
from blender_mcp.production.cinematic import analysis, cameras, model, registry, store
from blender_mcp.production.cinematic.service import HANDLERS, dispatch, load_handlers
from blender_mcp.production.cinematic.tools import NAMES, READ_ONLY, TRANSIENT
from blender_mcp.production.common.bridge import build_code, payload_bytes
from blender_mcp.production.common.payload import link_payload
from blender_mcp.production.common.storage import Storage, write_json

TOOLS = ('cinematic_create_camera', 'cinematic_configure_camera', 'cinematic_animate_camera', 'cinematic_create_cuts',
         'cinematic_plan_focus', 'cinematic_manage_timeline')
ACTOR = 'a' * 16
ACTOR_B = 'b' * 16
KEY = 'TEST-KEY'
GID_PREFIX = 'M10C_'
ACTOR_ROW = {'actor_id': ACTOR, 'object': 'ActorRig', 'data': 'ActorRig', 'meshes': ['ActorBody'], 'fingerprint': 'f',
             'role': 'PROTAGONIST'}
CREATURE_ROW = {'actor_id': ACTOR_B, 'object': 'CreatureRig', 'data': 'CreatureRig', 'meshes': ['CreatureBody'],
                'fingerprint': 'g', 'role': 'CREATURE'}
DEFAULT_SHOTS = [{'shot_id': 'S1', 'frame_start': 1, 'frame_end': 30, 'camera_id': 'A'},
                 {'shot_id': 'S2', 'frame_start': 31, 'frame_end': 60, 'camera_id': 'B'},
                 {'shot_id': 'S3', 'frame_start': 61, 'frame_end': 90, 'camera_id': 'C'}]


class NoBridge:
    """Proposals must not reach Blender: any call is a test failure."""
    calls = 0

    def run(self, body, params=None):
        NoBridge.calls += 1
        raise AssertionError('proposal reached Blender')


def make_cinematic(tmp_path, actors=None, shots=None, **state_extra):
    storage = Storage(tmp_path)
    timeline = {'fps': 30, 'fps_base': 1.0, 'frame_start': 1, 'frame_end': 90, 'duration_s': 3.0, 'frames': 90,
                'conversions': []}
    plan = {'schema': model.SCHEMA, 'brief': {'schema': 'production.cinematic_brief.v1', 'duration_s': 3.0},
            'beats': None, 'timeline': timeline,
            'shots': deepcopy(DEFAULT_SHOTS if shots is None else shots),
            'actors': deepcopy(actors or [ACTOR_ROW]), 'actor_registry_key': KEY,
            'source': {'scene': 'Scene', 'fingerprint': 'src', 'settings': {}, 'filepath': ''},
            'asset': {'role': 'FIXTURE', 'asset_id': 'x', 'path': 'p'}, 'exclude_objects': [],
            'backend_fingerprint': store.backend_fingerprint()}
    path, plan = store.save(storage, 'op-test', plan)
    gid = plan['generation_id']
    state = {'state': 'CINEMATIC_CANDIDATE', 'refinement_cycles': 0, 'revision': 0,
             'assembly': {'version': 1, 'scene': model.owned_prefix(gid, 1),
                          'collections': {'src': model.owned_name(gid, 1, 'SRC'), 'cin': model.owned_name(gid, 1, 'CIN')},
                          'source_scene': 'Scene', 'touched': [], 'owned': {}},
             'timeline': deepcopy(timeline), 'actors': {}, 'cameras': {}, 'history': [],
             'fingerprints': {'source': 'src', 'candidate': 'cand'}}
    state.update(state_extra)
    store.record(path, state)
    return storage, path, plan


def call(name, storage, path, options=None, apply=False, bridge=None, object_name=None):
    return dispatch(name, bridge or NoBridge(), storage, 'op', cinematic=path, options=options, apply=apply,
                    object_name=object_name)


def camera_row(plan, camera_id, shot_id=None):
    names = cameras.camera_names(plan, {'assembly': {'version': 1}}, camera_id)
    return {'object': names['object'], 'data': names['data'], 'shot_id': shot_id,
            'settings': {'type': 'PERSP', 'lens': 50.0, 'lens_unit': 'MILLIMETERS', 'sensor_width': 36.0,
                         'sensor_height': 24.0, 'sensor_fit': 'AUTO', 'shift_x': 0.0, 'shift_y': 0.0,
                         'clip_start': 0.1, 'clip_end': 1000.0, 'ortho_scale': 6.0},
            'placement': {'location_m': [0.0, -5.0, 1.5], 'rotation_euler_deg': [90.0, 0.0, 0.0]},
            'constraints': [], 'animation': None, 'focus': None}


def with_cameras(tmp_path, ids=('A', 'B', 'C'), actors=None, shots=None, **extra):
    storage, path, plan = make_cinematic(tmp_path, actors=actors, shots=shots)
    _, plan, state = store.load(storage, path)
    shots = {'A': 'S1', 'B': 'S2', 'C': 'S3'}
    state['cameras'] = {cid: camera_row(plan, cid, shots.get(cid)) for cid in ids}
    state.update(extra)
    store.record(path, state)
    return storage, path, plan


# ---------------------------------------------------------------------------------------------------------------
# catalogue, payload budgets and safe mode
# ---------------------------------------------------------------------------------------------------------------

def test_area_c_tools_are_registered_writers():
    load_handlers()
    for name in TOOLS:
        assert name in NAMES and name in HANDLERS
        assert name not in READ_ONLY and name not in TRANSIENT


PARAMS = {'generation_id': 'g' * 32, 'version': 1, 'scene': 'M10C_gggggggg_v1', 'source_scene': 'Scene',
          'collections': {'src': 'a', 'cin': 'b'}, 'capture': ['ActorRig', 'ActorBody'], 'camera': 'Cam',
          'camera_data': 'CamD', 'action': 'Act', 'settings': dict(cameras.DEFAULTS), 'location': [0, 0, 1],
          'rotation_euler_deg': [90, 0, 0], 'look_at': None, 'reference_frame': 1,
          'track': {'mode': 'NONE', 'name': 'T'}, 'set_scene_camera': True, 'dof': {}, 'tracking': None,
          'frame_start': 1, 'frame_end': 90, 'move': 'DOLLY', 'easing': 'LINEAR', 'params': {'distance_m': 2.0},
          'target': None, 'replace': False, 'base_location': [0, 0, 1], 'base_rotation_deg': [90, 0, 0],
          'lens_start_mm': 50.0, 'cuts': [{'marker': 'CUT_S1', 'shot_id': 'S1', 'camera': 'Cam', 'frame': 1,
                                            'frame_end': 90}], 'remove': [],
          'rows': [{'frame': 1, 'target': {'kind': 'distance', 'distance_m': 3.0}, 'pull_frames': 0}],
          'targets': {}, 'mode': 'KEYED', 'pull_easing': 'EASE_IN_OUT', 'aperture_fstop': None,
          'thresholds': {'in_focus_coc_mm': 0.03, 'in_focus_source': 'PROPOSAL', 'excessive_coc_ratio': 0.01,
                         'jump_ratio': 0.25, 'keyed_tolerance_m': 0.001},
          'actors': [{'kind': 'actor', 'actor_id': ACTOR, 'object': 'ActorRig', 'bone': None}], 'add': [],
          'fps': 30, 'fps_base': 1.0, 'cameras': ['Cam'],
          'checks': [{'event_id': 'reach', 'object': 'ActorRig', 'bones': [['hand.R', 'reach']]}]}


@pytest.mark.parametrize('name', sorted(cameras.BODIES))
def test_every_payload_passes_safe_mode_within_budget(name):
    body = cameras.BODIES[name]
    code = build_code(body, PARAMS)  # raises SandboxViolation on any forbidden construct
    assert payload_bytes(body, PARAMS) < 140_000
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 14_000


def test_runtime_avoids_forbidden_constructs():
    for forbidden in ('import bpy_extras', 'from bpy_extras', 'import numpy', '__class__', 'lambda ', 'driver_add(',
                      'hasattr(bpy.types', 'import os', 'import time', 'import hashlib'):
        assert forbidden not in cameras.CAMERA_RUNTIME
        for body in cameras.BODIES.values():
            assert forbidden not in body


def test_timeline_report_payload_is_read_only():
    # Same patterns test_m31_hints uses for READ_ONLY tools: no frame change, no Action/slot assignment, no pose write.
    linked = link_payload(cameras.TIMELINE_READ_BODY)
    assert not re.search(r'frame_set\(|m3_set_frame\(|m2_set_frame\(', linked)
    assert not re.search(r'animation_data\.action\s*=(?!=)|\.action_slot\s*=(?!=)', linked)
    assert not re.search(r'\.rotation_quaternion\s*=(?!=)|\.location\s*=(?!=)|pose_position\s*=(?!=)', linked)


def test_mutating_payloads_sample_with_capture_and_restore():
    for name in ('create', 'animate', 'cuts', 'focus'):
        linked = link_payload(cameras.BODIES[name])
        assert linked.count('cm_capture(scene, objects)') == linked.count('cm_restore(scene, objects, state)') >= 1
        assert 'cam_capture_set(params, cin)' in linked  # owned cameras are restored too (flush measured in 5.2.1)


# ---------------------------------------------------------------------------------------------------------------
# pure math shipped to Blender
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('kind', model.EASINGS)
def test_easing_endpoints_and_monotonic(kind):
    values = [cameras.cam_ease(kind, i / 100) for i in range(101)]
    assert values[0] == 0.0 and values[-1] == 1.0
    assert all(b >= a - 1e-15 for a, b in zip(values, values[1:]))
    assert cameras.cam_ease(kind, -1) == 0.0 and cameras.cam_ease(kind, 2) == 1.0


def test_ease_in_out_is_symmetric():
    assert cameras.cam_ease('EASE_IN_OUT', 0.5) == 0.5
    assert abs(cameras.cam_ease('EASE_IN_OUT', 0.2) + cameras.cam_ease('EASE_IN_OUT', 0.8) - 1.0) < 1e-12


def test_handheld_noise_is_deterministic_and_bounded():
    terms = cameras.cam_handheld_terms(7, 1.5)
    assert terms == cameras.cam_handheld_terms(7, 1.5)
    assert terms != cameras.cam_handheld_terms(8, 1.5)
    amplitude_m, amplitude_rad = 0.02, math.radians(1.0)
    peak = [0.0] * 6
    for i in range(3000):
        row = cameras.cam_handheld_offset(terms, i / 30.0, amplitude_m, amplitude_rad)
        peak = [max(p, abs(v)) for p, v in zip(peak, row)]
    assert all(p <= amplitude_m + 1e-12 for p in peak[:3]) and all(p <= amplitude_rad + 1e-12 for p in peak[3:])
    assert min(peak[:3]) > 0.5 * amplitude_m  # the noise actually moves, not a degenerate constant
    assert cameras.cam_handheld_offset(terms, 1.0, 0.0, 0.0) == [0.0] * 6


def test_derivatives_constant_velocity_and_constant_acceleration():
    frames = list(range(1, 31))
    fps = 30.0
    line = [[2.0 * (f - 1) / fps, 0.0, 0.0] for f in frames]  # 2 m/s
    identity = [[1.0, 0.0, 0.0, 0.0] for _ in frames]
    result = cameras.cam_derivatives(frames, line, identity, fps)
    assert abs(result['max_speed_m_s'] - 2.0) < 1e-9 and result['max_acceleration_m_s2'] < 1e-6
    assert result['max_jerk_m_s3'] < 1e-3 and abs(result['path_length_m'] - 2.0 * 29 / 30) < 1e-6  # rounded to 6 decimals
    assert result['max_angular_speed_deg_s'] == 0.0
    parabola = [[0.5 * 3.0 * ((f - 1) / fps) ** 2, 0.0, 0.0] for f in frames]  # a = 3 m/s2
    result = cameras.cam_derivatives(frames, parabola, identity, fps)
    assert abs(result['max_acceleration_m_s2'] - 3.0) < 1e-6 and result['max_jerk_m_s3'] < 1e-3


def test_derivatives_angular_speed_about_z():
    frames = list(range(1, 31))
    fps = 30.0
    rate = 45.0  # deg/s
    quats = []
    for f in frames:
        half = math.radians(rate * (f - 1) / fps) / 2
        quats.append([math.cos(half), 0.0, 0.0, math.sin(half)])
    result = cameras.cam_derivatives(frames, [[0.0, 0.0, 0.0]] * 30, quats, fps)
    assert abs(result['max_angular_speed_deg_s'] - rate) < 1e-6
    assert result['max_angular_acceleration_deg_s2'] < 1e-3
    assert abs(result['rotation_travel_deg'] - rate * 29 / 30) < 1e-6


def test_derivatives_refuse_irregular_sampling():
    assert cameras.cam_derivatives([1, 2, 4], [[0, 0, 0]] * 3, [[1, 0, 0, 0]] * 3, 30)['non_uniform_samples']
    assert cameras.cam_derivatives([1], [[0, 0, 0]], [[1, 0, 0, 0]], 30)['insufficient_samples']


def test_circle_of_confusion_thin_lens():
    assert cameras.cam_coc_mm(50, 2.0, 2.0, 2.0) == 0.0
    expected = (0.05 ** 2 / (2.0 * (2.0 - 0.05))) * abs(4.0 - 2.0) / 4.0 * 1000.0
    assert abs(cameras.cam_coc_mm(50, 2.0, 2.0, 4.0) - expected) < 1e-12
    assert cameras.cam_coc_mm(50, 2.0, 2.0, -1.0) is None  # behind the camera
    assert cameras.cam_coc_mm(50, 2.0, 0.04, 3.0) is None  # focus inside the focal length
    assert cameras.cam_coc_mm(50, 4.0, 2.0, 4.0) < cameras.cam_coc_mm(50, 2.0, 2.0, 4.0)  # stopping down sharpens


# ---------------------------------------------------------------------------------------------------------------
# cuts and coverage
# ---------------------------------------------------------------------------------------------------------------

CAMS = {'A': {}, 'B': {}, 'C': {}}


def cut(shot, camera, frame):
    return {'shot_id': shot, 'camera_id': camera, 'frame': frame}


def test_valid_cuts_get_their_end_frames():
    rows, errors, warnings = cameras.check_cuts([cut('S1', 'A', 1), cut('S2', 'B', 31), cut('S3', 'C', 61)], 1, 90, 6, CAMS)
    assert not errors and not warnings
    assert [(r['frame'], r['frame_end'], r['frames'], r['marker']) for r in rows] == [
        (1, 30, 30, 'CUT_S1'), (31, 60, 30, 'CUT_S2'), (61, 90, 30, 'CUT_S3')]


@pytest.mark.parametrize('rows,code', [
    ([], 'CUTS_EMPTY'),
    ([cut('S1', 'A', 1), cut('S2', 'B', 200)], 'CUT_OUTSIDE_RANGE'),
    ([cut('S1', 'A', 1), cut('S3', 'C', 61), cut('S2', 'B', 31)], 'CUT_ORDER_INVALID'),
    ([cut('S1', 'A', 1), cut('S2', 'B', 1)], 'CUT_OVERLAP'),
    ([cut('S1', 'A', 5)], 'CUT_FIRST_NOT_AT_START'),
    ([cut('S1', 'A', 1), cut('S2', 'B', 4)], 'CUT_TOO_SHORT'),
    ([cut('S1', 'A', 1), cut('S1', 'B', 31)], 'CUT_SHOT_DUPLICATE'),
    ([cut('S1', 'Z', 1)], 'CAMERA_NOT_OWNED'),
    ([dict(cut('S1', 'A', 1), extra=1)], 'CUT_INVALID'),
    ([{'shot_id': 'S1', 'camera_id': 'A', 'frame': 1.5}], 'CINEMATIC_INTEGER_REQUIRED'),
])
def test_invalid_cuts_are_refused(rows, code):
    _, errors, _ = cameras.check_cuts(rows, 1, 90, 6, CAMS)
    assert code in {e['code'] for e in errors}


def test_short_cut_threshold_is_labelled_proposal_and_overridable():
    _, errors, _ = cameras.check_cuts([cut('S1', 'A', 1), cut('S2', 'B', 4)], 1, 90, 6, CAMS)
    assert errors[0]['threshold'] == {'min_shot_frames': 6, 'threshold_source': 'PROPOSAL'}
    assert not cameras.check_cuts([cut('S1', 'A', 1), cut('S2', 'B', 4)], 1, 90, 3, CAMS)[1]


def test_consecutive_cuts_to_the_same_camera_warn():
    _, errors, warnings = cameras.check_cuts([cut('S1', 'A', 1), cut('S2', 'A', 31)], 1, 90, 6, CAMS)
    assert not errors and warnings[0]['code'] == 'CUT_SAME_CAMERA'


def test_marker_audit_detects_broken_cuts():
    expected = [{'marker': 'CUT_S1', 'frame': 1, 'camera_object': 'CamA'},
                {'marker': 'CUT_S2', 'frame': 31, 'camera_object': 'CamB'},
                {'marker': 'CUT_S9', 'frame': 70, 'camera_object': 'CamC'}]
    markers = [['CUT_S1', 1, 'CamA', True], ['CUT_S2', 31, None, False], ['CUT_X', 31, 'Foreign', False],
               ['CUT_Y', 120, 'CamA', True], ['BEAT_a', 5, None, False]]
    errors, warnings = cameras.audit_cut_markers(markers, expected, 1, 90, 6)
    codes = [e['code'] for e in errors]
    for code in ('CUT_CAMERA_MISSING', 'CUT_CAMERA_NOT_OWNED', 'CUT_OUTSIDE_RANGE', 'CUT_OVERLAP', 'CUT_MARKER_MISSING',
                 'CUT_MARKER_CHANGED'):
        assert code in codes
    assert {'CUT_MARKER_UNRECORDED'} <= {w['code'] for w in warnings}


def test_gap_and_merge_helpers():
    assert cameras.merge_ranges([[10, 20], [1, 5], [6, 8], [19, 25]]) == [[1, 8], [10, 25]]
    assert cameras.gaps([[1, 8], [10, 25]], 1, 30) == [[9, 9], [26, 30]]
    assert cameras.gaps([], 1, 3) == [[1, 3]]
    assert cameras.gaps([[1, 30]], 1, 30) == []


def test_shot_coverage_reports_gaps_overlaps_and_ends():
    shots = [{'shot_id': 'S1', 'frame_start': 1, 'frame_end': 30}, {'shot_id': 'S2', 'frame_start': 35, 'frame_end': 60},
             {'shot_id': 'S3', 'frame_start': 55, 'frame_end': 80}]
    report, errors, _ = cameras.shot_coverage(shots, 1, 90)
    assert {e['code'] for e in errors} == {'SHOT_GAP', 'SHOT_OVERLAP', 'SHOTS_END_MISMATCH'}
    assert report['contiguous'] is False
    report, errors, _ = cameras.shot_coverage(shots[:1] + [{'shot_id': 'S2', 'frame_start': 31, 'frame_end': 90}], 1, 90)
    assert not errors and report['contiguous'] is True


def test_actor_coverage_reads_clips_and_cache_windows():
    actors = {'x': {'clips': [{'start': 1, 'action_frame_start': 1, 'action_frame_end': 40, 'scale': 1.0, 'repeat': 1.0},
                              {'frame_start': 50, 'frame_end': 90}, {'name': 'mystery'}],
                    'secondary': [{'window': [1, 60]}, {'frame_start': 100, 'frame_end': 120}, {'ref': 'r'}]}}
    report, errors, warnings = cameras.actor_coverage(actors, 1, 90)
    assert report['x']['uncovered'] == [[41, 49]] and report['x']['unreadable_clips'] == 1
    assert {e['code'] for e in errors} == {'SECONDARY_WINDOW_OUTSIDE'}
    assert {'ACTOR_CLIP_GAP', 'ACTOR_CLIP_UNREADABLE', 'SECONDARY_WINDOW_PARTIAL', 'SECONDARY_WINDOW_UNREADABLE'} <= {
        w['code'] for w in warnings}


# ---------------------------------------------------------------------------------------------------------------
# option validation
# ---------------------------------------------------------------------------------------------------------------

def test_settings_defaults_are_measured_blender_defaults_with_provenance():
    settings, provenance = cameras.normalize_settings({'lens_mm': 35})
    assert settings['lens'] == 35.0 and provenance['lens'] == 'USER_REQUIREMENT'
    assert settings['sensor_width'] == 36.0 and provenance['sensor_width'] == 'INFERRED_DEFAULT'


@pytest.mark.parametrize('options,code', [
    ({'lens_mm': 0.5}, 'CINEMATIC_RANGE_INVALID'),
    ({'lens_mm': 6000}, 'CINEMATIC_RANGE_INVALID'),
    ({'lens_mm': True}, 'CINEMATIC_NUMBER_INVALID'),
    ({'lens_mm': float('nan')}, 'CINEMATIC_NUMBER_INVALID'),
    ({'type': 'PANO'}, 'CINEMATIC_CHOICE_INVALID'),
    ({'sensor_fit': 'DIAGONAL'}, 'CINEMATIC_CHOICE_INVALID'),
    ({'clip_start': 5, 'clip_end': 2}, 'CAMERA_CLIP_INVALID'),
    ({'shift_x': 3}, 'CINEMATIC_RANGE_INVALID'),
])
def test_settings_out_of_range_are_refused(options, code):
    with pytest.raises(ValueError, match=code):
        cameras.normalize_settings(options)


def test_configure_merges_with_the_current_clip_range():
    with pytest.raises(ValueError, match='CAMERA_CLIP_INVALID'):
        cameras.normalize_settings({'clip_start': 20}, {'clip_start': 0.1, 'clip_end': 10.0})
    settings, _ = cameras.normalize_settings({'clip_end': 50}, {'clip_start': 0.1, 'clip_end': 10.0})
    assert settings == {'clip_end': 50.0}


PLAN = {'actors': [{'actor_id': ACTOR, 'object': 'ActorRig'}]}


def test_targets_need_exactly_one_kind():
    assert cameras.resolve_target(PLAN, {'actor_id': ACTOR, 'bone': 'head'}, 't') == {
        'kind': 'actor', 'actor_id': ACTOR, 'object': 'ActorRig', 'bone': 'head'}
    assert cameras.resolve_target(PLAN, {'object': 'mixamorig:Hips'}, 't')['object'] == 'mixamorig:Hips'
    for value, code in (({'actor_id': ACTOR, 'point': [0, 0, 0]}, 'CAMERA_TARGET_INVALID'),
                        ({'actor_id': 'b' * 16}, 'ACTOR_NOT_IN_PLAN'), ({'point': [0, 0, 0], 'bone': 'x'}, 'CAMERA_TARGET_INVALID'),
                        ({'distance_m': 3}, 'CAMERA_TARGET_INVALID'), ({'object': ''}, 'CINEMATIC_NAME_INVALID')):
        with pytest.raises(ValueError, match=code):
            cameras.resolve_target(PLAN, value, 't')
    assert cameras.resolve_target(PLAN, {'distance_m': 3}, 't', allow_distance=True)['distance_m'] == 3.0


@pytest.mark.parametrize('move,params,target,code', [
    ('DOLLY', {}, None, 'CAMERA_MOVE_PARAMS_INVALID'),
    ('DOLLY', {'distance_m': 0}, None, 'CAMERA_MOVE_PARAMS_INVALID'),
    ('ORBIT', {'angle_deg': 45}, None, 'CAMERA_TARGET_REQUIRED'),
    ('TRACKING', {}, None, 'CAMERA_TARGET_REQUIRED'),
    ('TRACKING', {}, {'kind': 'point', 'point': [0, 0, 0]}, 'CAMERA_TARGET_UNSUPPORTED'),
    ('PAN', {'angle_deg': 30}, {'kind': 'point', 'point': [0, 0, 0]}, 'CAMERA_TARGET_UNSUPPORTED'),
    ('HANDHELD', {'amplitude_m': 0.2}, None, 'CINEMATIC_RANGE_INVALID'),
    ('HANDHELD', {'frequency_hz': 0}, None, 'CINEMATIC_RANGE_INVALID'),
    ('STATIC', {'distance_m': 1}, None, 'CAMERA_MOVE_PARAMS_INVALID'),
    ('STATIC', {'look_at_target': True}, None, 'CAMERA_TARGET_REQUIRED'),
    ('ZOOM', {}, None, 'CINEMATIC_CHOICE_INVALID'),
])
def test_move_parameters_are_validated(move, params, target, code):
    with pytest.raises(ValueError, match=code):
        cameras.normalize_move(move, params, target)


def test_handheld_defaults_are_labelled_proposals():
    params, provenance = cameras.normalize_move('HANDHELD', {'seed': 3}, None)
    assert params['amplitude_m'] == cameras.HANDHELD_DEFAULTS['amplitude_m']
    assert provenance['amplitude_m'] == 'PROPOSED_CREATIVE_DECISION' and provenance['seed'] == 'USER_REQUIREMENT'


# ---------------------------------------------------------------------------------------------------------------
# handlers: proposals never reach Blender; refusals happen before any mutation
# ---------------------------------------------------------------------------------------------------------------

def test_proposals_do_not_touch_blender(tmp_path):
    storage, path, plan = with_cameras(tmp_path)
    NoBridge.calls = 0
    created = call('cinematic_create_camera', storage, path, {'camera_id': 'D', 'location': [1, 2, 3],
                                                               'look_at': {'actor_id': ACTOR}, 'lens_mm': 85})
    assert created['status'] == 'proposal' and created['camera']['object'].endswith('_CAM_D')
    assert created['provenance']['lens'] == 'USER_REQUIREMENT'
    configured = call('cinematic_configure_camera', storage, path, {'camera_id': 'A', 'lens_mm': 40,
                                                                   'dof': {'focus_object': {'actor_id': ACTOR, 'bone': 'head'}}})
    assert configured['change']['dof']['focus_subtarget'] == 'head'
    animated = call('cinematic_animate_camera', storage, path, {'camera_id': 'C', 'move': 'TRACKING',
                                                               'target': {'actor_id': ACTOR}})
    assert animated['animation']['frames'] == [61, 90] and animated['animation']['range_source'] == 'camera_shot:S3'
    assert animated['animation']['keys_to_write'] == 30 * 6
    handheld = call('cinematic_animate_camera', storage, path, {'camera_id': 'B', 'shot_id': 'S2', 'move': 'HANDHELD'})
    assert handheld['warnings'][0]['code'] == 'HANDHELD_IS_PROCEDURAL_NOISE'
    cuts = call('cinematic_create_cuts', storage, path, {'from_shots': True})
    assert [c['marker'] for c in cuts['cuts']] == ['CUT_S1', 'CUT_S2', 'CUT_S3']
    focus = call('cinematic_plan_focus', storage, path, {'camera_id': 'C', 'focus': [
        {'frame': 61, 'target': {'actor_id': ACTOR, 'bone': 'head'}}, {'frame': 76, 'target': {'distance_m': 3.0}}]})
    assert focus['focus']['mode'] == 'KEYED' and focus['focus']['pull_windows'] == [[76, 83]]
    assert focus['focus']['thresholds']['in_focus_source'].startswith('PROPOSAL')
    timeline = call('cinematic_manage_timeline', storage, path, {'markers': [{'kind': 'EVENT', 'id': 'turn', 'seconds': 1.5}]})
    assert timeline['timeline']['add_markers'][0]['frame'] == 46
    assert timeline['timeline']['conversions'][0]['rule'].startswith('frame = frame_start')
    assert NoBridge.calls == 0


@pytest.mark.parametrize('name,options,object_name,code', [
    ('cinematic_create_camera', {'camera_id': 'A', 'location': [0, 0, 1], 'rotation_euler_deg': [90, 0, 0]}, None, 'CAMERA_EXISTS'),
    ('cinematic_create_camera', {'camera_id': 'D', 'rotation_euler_deg': [90, 0, 0]}, None, 'CAMERA_PLACEMENT_REQUIRED'),
    ('cinematic_create_camera', {'camera_id': 'D', 'location': [0, 0, 1]}, None, 'CAMERA_ORIENTATION_REQUIRED'),
    ('cinematic_create_camera', {'camera_id': 'D', 'location': [0, 0, 1], 'look_at': {'point': [0, 0, 0]},
                                 'track': 'TRACK_TO'}, None, 'CAMERA_TRACK_TARGET_OBJECT_REQUIRED'),
    ('cinematic_create_camera', {'camera_id': 'D', 'location': [0, 0, 1], 'look_at': {'point': [0, 0, 0]}, 'shot_id': 'S9'},
     None, 'CINEMATIC_SHOT_UNKNOWN'),
    ('cinematic_create_camera', {'camera_id': 'D' * 45, 'location': [0, 0, 1], 'look_at': {'point': [0, 0, 0]}}, None,
     'CINEMATIC_NAME_TOO_LONG'),
    ('cinematic_configure_camera', {'camera_id': 'Z', 'lens_mm': 40}, None, 'CAMERA_NOT_OWNED'),
    ('cinematic_configure_camera', {'lens_mm': 40}, 'SourceCam', 'CAMERA_NOT_OWNED'),
    ('cinematic_configure_camera', {'camera_id': 'A'}, None, 'CAMERA_CONFIGURATION_EMPTY'),
    ('cinematic_configure_camera', {'camera_id': 'A', 'lens': 40}, None, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ('cinematic_configure_camera', {'camera_id': 'A', 'dof': {'aperture_fstop': 0.01}}, None, 'CINEMATIC_RANGE_INVALID'),
    ('cinematic_configure_camera', {'camera_id': 'A', 'dof': {'aperture_blades': 2.5}}, None, 'CINEMATIC_INTEGER_REQUIRED'),
    ('cinematic_animate_camera', {'camera_id': 'A', 'move': 'STATIC', 'frame_start': 80, 'frame_end': 120}, None,
     'CAMERA_RANGE_OUTSIDE_TIMELINE'),
    ('cinematic_animate_camera', {'camera_id': 'A', 'move': 'STATIC', 'shot_id': 'S1', 'frame_start': 1}, None,
     'CAMERA_RANGE_AMBIGUOUS'),
    ('cinematic_animate_camera', {'camera_id': 'A', 'move': 'STATIC', 'frame_start': 5, 'frame_end': 5}, None,
     'CAMERA_RANGE_TOO_SHORT'),
    ('cinematic_create_cuts', {'cuts': [{'shot_id': 'S1', 'camera_id': 'A', 'frame': 1},
                                        {'shot_id': 'S2', 'camera_id': 'B', 'frame': 200}]}, None, 'CUT_OUTSIDE_RANGE'),
    ('cinematic_create_cuts', {'clear': True, 'from_shots': True}, None, 'CUTS_AMBIGUOUS'),
    ('cinematic_plan_focus', {'camera_id': 'A', 'focus': []}, None, 'FOCUS_ROWS_REQUIRED'),
    ('cinematic_plan_focus', {'camera_id': 'A', 'focus': [{'frame': 10, 'target': {'distance_m': 2}},
                                                          {'frame': 5, 'target': {'distance_m': 3}}]}, None, 'FOCUS_ORDER_INVALID'),
    ('cinematic_plan_focus', {'camera_id': 'A', 'focus': [{'frame': 40, 'target': {'distance_m': 2}}]}, None,
     'CINEMATIC_RANGE_INVALID'),
    ('cinematic_manage_timeline', {'finalize': True, 'frame_end': 50}, None, 'TIMELINE_FINALIZE_EXCLUSIVE'),
    ('cinematic_manage_timeline', {'finalize': 'yes'}, None, 'CINEMATIC_BOOLEAN_REQUIRED'),
    ('cinematic_manage_timeline', {'interactions': 'reach'}, None, 'INTERACTIONS_INVALID'),
    ('cinematic_manage_timeline', {'interactions': [{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 200}]}, None,
     'INTERACTION_OUTSIDE_TIMELINE'),
    ('cinematic_manage_timeline', {'interactions': [{'kind': 'REACH', 'actor_id': ACTOR_B, 'frame': 20}]}, None,
     'ACTOR_NOT_IN_PLAN'),
    ('cinematic_manage_timeline', {'frame_start': 10, 'frame_end': 5}, None, 'TIMELINE_RANGE_INVALID'),
    ('cinematic_manage_timeline', {'markers': [{'kind': 'BEAT', 'id': 'x', 'frame': 200}]}, None, 'TIMELINE_MARKER_OUTSIDE'),
    ('cinematic_manage_timeline', {'markers': [{'kind': 'CUT', 'id': 'x', 'frame': 2}]}, None, 'CINEMATIC_CHOICE_INVALID'),
    ('cinematic_manage_timeline', {'markers': [{'kind': 'BEAT', 'id': 'x', 'frame': 2, 'seconds': 1}]}, None,
     'TIMELINE_MARKERS_INVALID'),
])
def test_host_side_refusals_never_reach_blender(tmp_path, name, options, object_name, code):
    storage, path, plan = with_cameras(tmp_path)
    NoBridge.calls = 0
    with pytest.raises(ValueError, match=code):
        call(name, storage, path, options, apply=True, object_name=object_name)
    assert NoBridge.calls == 0


def test_timeline_range_cannot_orphan_recorded_cuts(tmp_path):
    rows = [{'shot_id': 'S1', 'camera_id': 'A', 'frame': 1, 'frame_end': 60, 'frames': 60, 'marker': 'CUT_S1',
             'camera_object': 'x'}, {'shot_id': 'S2', 'camera_id': 'B', 'frame': 61, 'frame_end': 90, 'frames': 30,
                                     'marker': 'CUT_S2', 'camera_object': 'y'}]
    storage, path, plan = with_cameras(tmp_path, cuts=rows)
    with pytest.raises(ValueError, match='TIMELINE_CUTS_OUTSIDE'):
        call('cinematic_manage_timeline', storage, path, {'frame_end': 50}, apply=True)
    with pytest.raises(ValueError, match='TIMELINE_CUT_START_MISMATCH'):
        call('cinematic_manage_timeline', storage, path, {'frame_start': 0}, apply=True)


def test_candidate_states_are_enforced(tmp_path):
    storage, path, plan = make_cinematic(tmp_path)
    _, plan, state = store.load(storage, path)
    store.record(path, dict(state, state='PLANNED'))
    with pytest.raises(ValueError, match='CINEMATIC_STATE_INVALID'):
        call('cinematic_create_camera', storage, path, {'camera_id': 'A'})
    store.record(path, dict(state, state='REJECTED'))
    with pytest.raises(ValueError, match='GENERATION_TERMINAL'):
        call('cinematic_create_cuts', storage, path, {'from_shots': True})
    with pytest.raises(ValueError, match='CINEMATIC_REFERENCE_REQUIRED'):
        dispatch('cinematic_create_camera', NoBridge(), storage, 'op', options={'camera_id': 'A'})


# ---------------------------------------------------------------------------------------------------------------
# applied steps: ledger bookkeeping with a stand-in for store.mutate (the Blender side is the probe's job)
# ---------------------------------------------------------------------------------------------------------------

def fake_mutate(results):
    seen = []

    def mutate(ctx, path, plan, state, label, body, params, allowed=model.ASSEMBLY_STATES, checkpoint=True):
        store.require_state(state, allowed)
        seen.append((label, params))
        result = results[label](params)
        new = store.invalidate_review(deepcopy(state))
        owned = new['assembly'].setdefault('owned', {})
        for key, rows in (result.get('owned') or {}).items():
            owned.setdefault(key, []).extend(rows)
        new['fingerprints'] = result['fingerprints']
        new['revision'] = new.get('revision', 0) + 1
        return new, result
    return mutate, seen


def test_applied_create_camera_records_the_camera(tmp_path, monkeypatch):
    storage, path, plan = make_cinematic(tmp_path)

    def create(params):
        return {'object': params['camera'], 'data': params['camera_data'], 'settings': dict(params['settings']),
                'location_m': params['location'], 'rotation_euler_deg': [80.0, 0.0, 30.0], 'target_point_m': [0, 0, 1.4],
                'target_resolved': {'object': 'ActorRig', 'bone': 'head', 'rule': 'bone'}, 'scene_camera': params['camera'],
                'owned': {'objects': [params['camera']], 'data': [['CAMERA', params['camera_data']]], 'constraints': []},
                'fingerprints': {'source': 'src', 'candidate': 'c2'}}
    mutate, seen = fake_mutate({'create_camera': create})
    monkeypatch.setattr(store, 'mutate', mutate)
    result = call('cinematic_create_camera', storage, path, {'camera_id': 'A', 'shot_id': 'S1', 'location': [5, -6, 2],
                                                              'look_at': {'actor_id': ACTOR, 'bone': 'head'}}, apply=True)
    label, params = seen[0]
    assert params['reference_frame'] == 1 and params['capture'] == ['ActorRig', 'ActorBody']
    assert params['look_at'] == {'kind': 'actor', 'actor_id': ACTOR, 'object': 'ActorRig', 'bone': 'head'}
    _, _, state = store.load(storage, path)
    row = state['cameras']['A']
    assert row['object'] == result['object'] and row['shot_id'] == 'S1' and row['placement']['location_m'] == [5.0, -6.0, 2.0]
    assert state['assembly']['owned']['objects'] == [row['object']] and result['cameras'] == ['A']


def test_actor_targets_without_bone_carry_the_registry_root(tmp_path, monkeypatch):
    """E2E (bpm10-e2e-1/B): the M10 demo rigs have parentless IK controls (foot_ik.L/R) besides 'root', so a bone-less
    actor target (plan_focus defocus rows, ORBIT/TRACKING targets) failed with CAMERA_TARGET_ROOT_AMBIGUOUS although the
    registry declares bones.root. The registry root now travels as root_bone; the payload uses it before guessing."""
    storage, path, plan = with_cameras(tmp_path)
    registry.write(storage, KEY, {'object': 'ActorRig', 'data': 'ActorRig', 'fingerprint': 'f', 'role': 'PROTAGONIST',
                                  'bones': {'root': 'root', 'head': 'head'}})
    actor_id = registry.records(storage, KEY)[0]['actor_id']
    _, plan, state = store.load(storage, path)
    plan['actors'][0]['actor_id'] = actor_id
    assert cameras.with_registry_roots({'actors': [{'kind': 'actor', 'actor_id': actor_id, 'bone': None},
                                                   {'kind': 'actor', 'actor_id': actor_id, 'bone': 'head'}],
                                        'target': {'kind': 'point', 'point': [0, 0, 0]}}, {actor_id: 'root'}) == {
        'actors': [{'kind': 'actor', 'actor_id': actor_id, 'bone': None, 'root_bone': 'root'},
                   {'kind': 'actor', 'actor_id': actor_id, 'bone': 'head'}],
        'target': {'kind': 'point', 'point': [0, 0, 0]}}
    seen = []
    monkeypatch.setattr(store, 'mutate', lambda ctx, path, plan, state, label, body, params, **kw: seen.append(params) or (state, {}))
    cameras.run(type('Ctx', (), {'storage': storage, 'bridge': None, 'operation_id': 'op'})(), path, plan, state,
                'plan_focus', cameras.FOCUS_BODY, {'actors': [{'kind': 'actor', 'actor_id': actor_id, 'object': 'ActorRig',
                                                                'bone': None}]})
    assert seen[0]['actors'][0]['root_bone'] == 'root'
    body = cameras.CAMERA_RUNTIME.split('def cam_target(')[1].split('def cam_target_point(')[0]
    assert "spec.get('root_bone')" in body and body.index("spec.get('root_bone')") < body.index('CAMERA_TARGET_ROOT_AMBIGUOUS')


def test_applied_cuts_and_timeline_update_the_ledger(tmp_path, monkeypatch):
    storage, path, plan = with_cameras(tmp_path)
    names = {cid: cameras.camera_names(plan, {'assembly': {'version': 1}}, cid)['object'] for cid in 'ABC'}

    def cuts(params):
        markers = [[c['marker'], c['frame'], c['camera'], True] for c in params['cuts']]
        checks = [[c['shot_id'], f, c['camera'], c['camera'], True] for c in params['cuts'] for f in (c['frame'], c['frame_end'])]
        return {'removed': [], 'checks': checks, 'timeline': {'markers': markers}, 'fingerprints': {'source': 'src', 'candidate': 'c3'}}

    def timeline(params):
        markers = [['CUT_S1', 1, names['A'], True], ['CUT_S2', 31, names['B'], True], ['CUT_S3', 61, names['C'], True]]
        markers += [[row['name'], row['frame'], None, False] for row in params['add']]
        return {'removed': [], 'timeline': {'markers': markers}, 'fingerprints': {'source': 'src', 'candidate': 'c4'}}
    mutate, seen = fake_mutate({'create_cuts': cuts, 'manage_timeline': timeline})
    monkeypatch.setattr(store, 'mutate', mutate)
    done = call('cinematic_create_cuts', storage, path, {'from_shots': True}, apply=True)
    assert done['status'] == 'success' and done['camera_checks_passed'] == 6
    _, _, state = store.load(storage, path)
    assert [c['marker'] for c in state['cuts']] == ['CUT_S1', 'CUT_S2', 'CUT_S3']

    class ReadBridge:
        def run(self, body, params=None):
            assert body is cameras.TIMELINE_READ_BODY
            return {'timeline': {'frame_start': 1, 'frame_end': 90, 'fps': 30, 'fps_base': 1.0, 'frame_current': 1,
                                 'camera': names['A'],
                                 'markers': [['BEAT_intro', 1, None, False], ['CUT_S1', 1, names['A'], True],
                                             ['CUT_S2', 31, names['B'], True], ['EVENT_turn', 46, None, False],
                                             ['CUT_S3', 61, names['C'], True]]},
                    'cameras': {n: True for n in params['cameras']}}
    result = call('cinematic_manage_timeline', storage, path,
                  {'markers': [{'kind': 'BEAT', 'id': 'intro', 'frame': 1}, {'kind': 'EVENT', 'id': 'turn', 'seconds': 1.5}],
                   'conversions': [{'frame': 61, 'purpose': 'S3 start'}]}, apply=True, bridge=ReadBridge())
    assert result['report']['passed'] and result['status'] == 'success'
    assert seen[-1][1]['add'] == [{'name': 'BEAT_intro', 'frame': 1}, {'name': 'EVENT_turn', 'frame': 46}]
    assert seen[-1][1]['fps'] == 30  # the payload refuses a candidate whose fps changed; fps is never written
    _, _, state = store.load(storage, path)
    assert [r['name'] for r in state['timeline']['narrative']] == ['BEAT_intro', 'EVENT_turn']
    assert state['timeline']['conversions'][-1]['seconds'] == 2.0
    assert state['timeline']['conversions'][-1]['direction'] == 'frame_to_seconds'


def test_timeline_report_flags_broken_cuts_and_fps_changes(tmp_path):
    rows = [{'shot_id': 'S1', 'camera_id': 'A', 'frame': 1, 'frame_end': 30, 'frames': 30, 'marker': 'CUT_S1', 'camera_object': 'CamA'},
            {'shot_id': 'S2', 'camera_id': 'B', 'frame': 31, 'frame_end': 90, 'frames': 60, 'marker': 'CUT_S2', 'camera_object': 'CamB'}]
    storage, path, plan = with_cameras(tmp_path, cuts=rows)
    _, plan, state = store.load(storage, path)

    class Bridge:
        def run(self, body, params=None):
            return {'timeline': {'frame_start': 1, 'frame_end': 90, 'fps': 24, 'fps_base': 1.0, 'frame_current': 1,
                                 'camera': 'CamA', 'markers': [['CUT_S1', 1, 'CamA', True], ['CUT_S2', 31, None, False],
                                                               ['EVENT_x', 5, 'CamA', True]]},
                    'cameras': {'CamA': True, 'CamB': False}}

    class Ctx:
        bridge = Bridge()
    report = cameras.timeline_report(Ctx(), plan, state)
    codes = {e['code'] for e in report['errors']}
    assert not report['passed']
    assert {'CINEMATIC_FPS_MISMATCH', 'CUT_CAMERA_MISSING', 'TIMELINE_NARRATIVE_MARKER_HAS_CAMERA',
            'CUT_MARKER_CHANGED'} <= codes
    assert report['warnings'][0]['code'] == 'CUT_SHOT_MISMATCH'


def test_timeline_report_flags_foreign_camera_markers_and_lost_narrative_markers(tmp_path):
    storage, path, plan = with_cameras(tmp_path)
    _, plan, state = store.load(storage, path)
    names = {cid: state['cameras'][cid]['object'] for cid in 'ABC'}
    state['cuts'] = cut_rows(plan, ('S1', 'A', 1, 30), ('S2', 'B', 31, 60), ('S3', 'C', 61, 90))
    state['timeline'] = dict(state['timeline'], narrative=[
        {'name': 'EVENT_reach', 'kind': 'EVENT', 'id': 'reach', 'frame': 64, 'source': 'interaction'},
        {'name': 'BEAT_intro', 'kind': 'BEAT', 'id': 'intro', 'frame': 1, 'source': 'frame'}])
    markers = cut_markers(names) + [['F_10', 10, names['B'], True], ['BEAT_intro', 2, None, False]]

    class Ctx:
        bridge = ReadBridge(markers)
    report = cameras.timeline_report(Ctx(), plan, state)
    codes = {e['code'] for e in report['errors']}
    assert not report['passed']
    assert {'TIMELINE_FOREIGN_CAMERA_MARKER', 'TIMELINE_MARKER_MISSING', 'TIMELINE_MARKER_CHANGED'} <= codes


# ---------------------------------------------------------------------------------------------------------------
# interaction events -> state['interactions'] (exact rows analysis._events reads)
# ---------------------------------------------------------------------------------------------------------------

TIMELINE_1_90 = {'fps': 30, 'fps_base': 1.0, 'frame_start': 1, 'frame_end': 90}
TWO_ACTORS = {'actors': [ACTOR_ROW, CREATURE_ROW]}
PROPS_STATE = {'props': {'cup': {'original': 'Cup', 'copy': 'M10C_x_v1_PROP_cup'}}}
RECORDS = {ACTOR: {'bones': {'root': 'root', 'head': 'Head', 'hand_right': 'hand.R'},
                   'interaction_capabilities': {'reach_m': 0.6}},
           ACTOR_B: {'bones': {'root': 'c_root', 'head': 'UNKNOWN'}, 'interaction_capabilities': {'reach_m': 'UNKNOWN'}}}
SCHEMA_KEYS = {'event_id', 'kind', 'actor_id', 'frame', 'frame_start', 'frame_end', 'effector', 'target', 'contact',
               'head_bone', 'forward_axis', 'reach_m'}


def planned(rows, **timeline):
    """A cinematic_plan_interaction result (production.cinematic_interactions.v1) with the given rows."""
    return {'schema': 'production.cinematic_interactions.v1', 'status': 'success', 'source': 'USER',
            'timeline': dict(TIMELINE_1_90, **timeline), 'interactions': rows, 'missing_prerequisites': [],
            'animation_resolved': False}


def prow(event_id, kind, frame, actors=(ACTOR,), target=None, effector=None, contact=False, duration=None):
    row = {'event_id': event_id, 'kind': kind, 'actors': list(actors), 'time_s': None if frame is None else (frame - 1) / 30.0,
           'frame': frame, 'spatial_target': target, 'contact_required': contact, 'action_refs': [], 'effector': effector,
           'success_criteria': [], 'reachability': {'status': 'NOT_APPLICABLE'}, 'warnings': [], 'provenance': {}}
    if duration is not None:
        row['duration_s'] = duration
    return row


ACTOR_TARGET = {'type': 'ACTOR', 'actor_id': ACTOR_B, 'position': [0.0, 2.0, 0.0]}
PLANNED_ROWS = [
    prow('reach', 'REACH', 64, actors=(ACTOR, ACTOR_B), target=ACTOR_TARGET, effector='hand_right', contact=True, duration=0.3),
    prow('look', 'OBSERVE', 20, target={'type': 'POINT', 'position': [0.0, 6.0, 1.5]}),
    prow('touch', 'TOUCH_SURFACE', 40, target={'type': 'OBJECT', 'object': 'Wall', 'position': [0, 3, 1]},
         effector='mixamorig:RightHand', contact=True),
    prow('grab', 'HOLD_PROP', 50, target={'type': 'OBJECT', 'object': 'Cup', 'position': [1, 0, 1]}, effector='hand_left',
         contact=True),
    prow('threat', 'PREPARE_ATTACK', 80, target=ACTOR_TARGET, effector='hand_right'),
    prow('react', 'REACT', 85, actors=(ACTOR_B,)),
]


def convert(value, timeline=TIMELINE_1_90, state=PROPS_STATE, records=RECORDS, plan=TWO_ACTORS):
    return cameras.convert_interactions(value, plan, state, records, timeline)


def test_planned_interactions_become_the_rows_analysis_reads():
    converted = convert(planned(PLANNED_ROWS))
    tail = {'bone': 'hand.R', 'end': 'TAIL'}
    assert converted['events'] == [
        {'event_id': 'reach', 'kind': 'REACH', 'actor_id': ACTOR, 'frame': 64, 'frame_start': 64, 'frame_end': 72,
         'effector': tail, 'target': {'actor_id': ACTOR_B}, 'contact': True},
        {'event_id': 'look', 'kind': 'OBSERVE', 'actor_id': ACTOR, 'frame': 20, 'frame_start': 20, 'frame_end': 20,
         'effector': None, 'target': {'point': [0.0, 6.0, 1.5]}, 'contact': False, 'head_bone': 'Head'},
        {'event_id': 'touch', 'kind': 'TOUCH_SURFACE', 'actor_id': ACTOR, 'frame': 40, 'frame_start': 40, 'frame_end': 40,
         'effector': {'bone': 'mixamorig:RightHand', 'end': 'TAIL'}, 'target': {'surfaces': ['Wall']}, 'contact': True},
        {'event_id': 'grab', 'kind': 'HOLD_PROP', 'actor_id': ACTOR, 'frame': 50, 'frame_start': 50, 'frame_end': 50,
         'effector': None, 'target': {'object': 'M10C_x_v1_PROP_cup'}, 'contact': True},
        {'event_id': 'threat', 'kind': 'PREPARE_ATTACK', 'actor_id': ACTOR, 'frame': 80, 'frame_start': 80, 'frame_end': 80,
         'effector': tail, 'target': {'actor_id': ACTOR_B}, 'contact': False, 'reach_m': 0.6},
        {'event_id': 'react', 'kind': 'REACT', 'actor_id': ACTOR_B, 'frame': 85, 'frame_start': 85, 'frame_end': 85,
         'effector': None, 'target': None, 'contact': False}]
    assert all(set(e) <= SCHEMA_KEYS and e['kind'] in model.INTERACTION_KINDS for e in converted['events'])
    # 0.3 s from frame 64 (2.1 s) ends the frame before 2.4 s = frame 73; the conversion is recorded.
    assert converted['conversions'][0]['frame'] == 73 and converted['conversions'][0]['purpose'] == 'interaction:reach:end'
    assert converted['markers'][0] == {'name': 'EVENT_reach', 'kind': 'EVENT', 'id': 'reach', 'frame': 64,
                                       'source': 'interaction'}
    prov = converted['provenance']
    assert prov['reach']['effector'] == 'registry role hand_right' and prov['grab']['effector'].startswith('UNKNOWN')
    assert prov['grab']['target'] == 'owned prop copy of Cup' and 'static surface' in prov['touch']['target']
    assert prov['threat']['reach_m'].startswith('registry estimate')
    # Rows the analyses cannot measure are reported with analysis' own reason, never dropped silently.
    ignored = {w['event_id']: w['reason'] for w in converted['warnings'] if w['code'] == 'INTERACTION_NOT_MEASURABLE'}
    assert ignored == {'look': 'GAZE_FORWARD_AXIS_REQUIRED', 'grab': 'EFFECTOR_REQUIRED'}
    assert converted['measurable'] == ['reach', 'touch', 'threat', 'react']
    assert {w['event_id'] for w in converted['warnings'] if w['code'] == 'INTERACTION_EFFECTOR_UNKNOWN'} == {'grab'}
    checks = {row['object']: row['bones'] for row in converted['checks']}
    assert checks == {'ActorRig': [['hand.R', 'reach'], ['Head', 'look'], ['mixamorig:RightHand', 'touch']],
                      'CreatureRig': [], 'Wall': [], 'M10C_x_v1_PROP_cup': []}


def test_analysis_reads_the_stored_rows_unchanged():
    converted = convert(planned(PLANNED_ROWS))
    rows, sources = analysis._plan_events({}, {'interactions': converted['events']}, {})
    events, ignored = analysis._events(rows, [ACTOR, ACTOR_B])
    assert sources == ['STATE'] and [e['event_id'] for e in events] == converted['measurable']
    reach = events[0]
    assert (reach['frame_start'], reach['frame_end'], reach['effector'], reach['target'], reach['contact']) == (
        64, 72, {'bone': 'hand.R', 'end': 'TAIL'}, {'actor_id': ACTOR_B}, True)


def test_unknown_registry_bones_stay_unknown():
    rows = [prow('look', 'GAZE', 20, actors=(ACTOR_B,), target={'type': 'ACTOR', 'actor_id': ACTOR, 'position': [0, 0, 0]}),
            prow('hit', 'REACH', 30, actors=(ACTOR_B,), target=ACTOR_TARGET, effector='hand_right', contact=True)]
    converted = convert(planned(rows))
    look, hit = converted['events']
    assert 'head_bone' not in look and hit['effector'] is None  # 'UNKNOWN' and absent roles are never guessed
    converted = convert(planned(rows), records={})  # no registry record at all
    assert converted['events'][1]['effector'] is None


def test_listed_events_are_normalized_to_the_same_schema():
    rows = [{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 64, 'frame_end': 72, 'effector': 'hand.R', 'target': ACTOR_B},
            {'event_id': 'gaze', 'kind': 'GAZE', 'actor_id': ACTOR_B, 'frame': 30, 'target': {'actor_id': ACTOR},
             'head_bone': 'c_head', 'forward_axis': [0, 1, 0]},
            {'event_id': 'look', 'kind': 'OBSERVE', 'actor_id': ACTOR, 'frame': 10, 'target': 'Wall', 'forward_axis': [1, 0, 0]},
            {'event_id': 'hold', 'kind': 'HOLD_PROP', 'actor_id': ACTOR, 'frame_start': 40, 'frame_end': 60,
             'effector': {'bone': 'hand.R', 'end': 'MID'}, 'target': {'object': 'Cup'}, 'contact': True},
            {'event_id': 'lean', 'kind': 'TOUCH_SURFACE', 'actor_id': ACTOR, 'frame': 70, 'effector': 'hand.R',
             'target': {'surfaces': ['Wall', 'Cup'], 'generic': True}}]
    converted = convert(rows)
    assert converted['events'] == [
        {'event_id': 'REACH_0', 'kind': 'REACH', 'actor_id': ACTOR, 'frame': 64, 'frame_start': 64, 'frame_end': 72,
         'effector': {'bone': 'hand.R', 'end': 'TAIL'}, 'target': {'actor_id': ACTOR_B}, 'contact': True},
        {'event_id': 'gaze', 'kind': 'GAZE', 'actor_id': ACTOR_B, 'frame': 30, 'frame_start': 30, 'frame_end': 30,
         'effector': None, 'target': {'actor_id': ACTOR}, 'contact': False, 'head_bone': 'c_head',
         'forward_axis': [0.0, 1.0, 0.0]},
        {'event_id': 'look', 'kind': 'OBSERVE', 'actor_id': ACTOR, 'frame': 10, 'frame_start': 10, 'frame_end': 10,
         'effector': None, 'target': {'object': 'Wall'}, 'contact': False, 'head_bone': 'Head',
         'forward_axis': [1.0, 0.0, 0.0]},
        {'event_id': 'hold', 'kind': 'HOLD_PROP', 'actor_id': ACTOR, 'frame': 40, 'frame_start': 40, 'frame_end': 60,
         'effector': {'bone': 'hand.R', 'end': 'MID'}, 'target': {'object': 'M10C_x_v1_PROP_cup'}, 'contact': True},
        {'event_id': 'lean', 'kind': 'TOUCH_SURFACE', 'actor_id': ACTOR, 'frame': 70, 'frame_start': 70, 'frame_end': 70,
         'effector': {'bone': 'hand.R', 'end': 'TAIL'}, 'target': {'surfaces': ['Wall', 'M10C_x_v1_PROP_cup'], 'generic': True},
         'contact': True}]
    assert converted['measurable'] == ['REACH_0', 'gaze', 'look', 'hold', 'lean'] and converted['origin'] == 'events'
    prov = converted['provenance']
    assert prov['REACH_0']['contact'].startswith('INFERRED_DEFAULT') and prov['REACH_0']['event_id'].startswith('INFERRED')
    assert prov['look']['head_bone'] == 'registry role head' and prov['gaze']['head_bone'].startswith('explicit')


def bad_planned(**changes):
    row = prow('reach', 'REACH', 64, target=ACTOR_TARGET, effector='hand_right', contact=True)
    row.update(changes)
    return planned([row])


@pytest.mark.parametrize('value,code', [
    ({'schema': 'production.cinematic_actor_paths.v1', 'paths': []}, 'INTERACTIONS_INVALID'),
    ('reach', 'INTERACTIONS_INVALID'),
    (planned([], fps=24), 'INTERACTIONS_TIMELINE_MISMATCH'),
    (planned([], frame_start=0), 'INTERACTIONS_TIMELINE_MISMATCH'),
    (bad_planned(frame=None, time_s=None), 'INTERACTION_FRAME_REQUIRED'),
    (bad_planned(frame=200), 'INTERACTION_OUTSIDE_TIMELINE'),
    (bad_planned(frame=85, time_s=84 / 30.0, duration_s=1.0), 'INTERACTION_OUTSIDE_TIMELINE'),
    (bad_planned(actors=['c' * 16]), 'ACTOR_NOT_IN_PLAN'),
    (bad_planned(spatial_target={'type': 'ACTOR', 'actor_id': 'c' * 16}), 'ACTOR_NOT_IN_PLAN'),
    (bad_planned(spatial_target={'type': 'UNRESOLVED'}), 'INTERACTION_TARGET_INVALID'),
    (bad_planned(contact_required='yes'), 'CINEMATIC_BOOLEAN_REQUIRED'),
    (bad_planned(kind='CONTACT'), 'CINEMATIC_CHOICE_INVALID'),
    (planned([prow('x', 'REACT', 10), prow('x', 'REACT', 20)]), 'INTERACTION_DUPLICATE'),
    ([{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 10, 'tolerance_frames': 2}], 'INTERACTION_INVALID'),
    ([{'kind': 'CONTACT', 'actor_id': ACTOR, 'frame': 10}], 'CINEMATIC_CHOICE_INVALID'),
    ([{'kind': 'REACH', 'actor_id': ACTOR}], 'INTERACTION_FRAME_REQUIRED'),
    ([{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 10, 'frame_start': 12}], 'INTERACTION_WINDOW_INVALID'),
    ([{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 10, 'target': {'object': 'Wall', 'point': [0, 0, 0]}}],
     'INTERACTION_TARGET_INVALID'),
    ([{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 10, 'target': {'object': 'Wall', 'bone': 'b'}}], 'INTERACTION_TARGET_INVALID'),
    ([{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 10, 'target': {'actor_id': ACTOR_B, 'end': 'TAIL'}}],
     'INTERACTION_TARGET_INVALID'),
    ([{'kind': 'GAZE', 'actor_id': ACTOR, 'frame': 10, 'forward_axis': [0, 0, 0]}], 'INTERACTION_FORWARD_AXIS_INVALID'),
    ([{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 10, 'effector': {'bone': 'hand.R', 'end': 'TIP'}}],
     'CINEMATIC_CHOICE_INVALID'),
    ([{'kind': 'REACH', 'actor_id': ACTOR, 'frame': 10, 'reach_m': -1}], 'CINEMATIC_RANGE_INVALID'),
    ([{'kind': 'REACT', 'actor_id': ACTOR, 'frame': 1 + i % 90} for i in range(65)], 'CINEMATIC_CAPACITY'),
])
def test_interaction_refusals(value, code):
    with pytest.raises(ValueError, match=code):
        convert(value)


def write_records(storage, records):
    for actor_id, record in records.items():
        write_json(registry.folder(storage, KEY) / ('actor-' + actor_id + '.json'),
                   dict(record, actor_id=actor_id, object='ActorRig' if actor_id == ACTOR else 'CreatureRig'))


def test_interaction_proposal_reads_the_registry_and_never_reaches_blender(tmp_path):
    storage, path, plan = with_cameras(tmp_path, actors=[ACTOR_ROW, CREATURE_ROW])
    write_records(storage, RECORDS)
    NoBridge.calls = 0
    result = call('cinematic_manage_timeline', storage, path, {'interactions': planned(PLANNED_ROWS[:2])})
    assert result['status'] == 'proposal' and NoBridge.calls == 0
    assert [e['effector'] for e in result['interactions']['events']] == [{'bone': 'hand.R', 'end': 'TAIL'}, None]
    assert result['interactions']['events'][1]['head_bone'] == 'Head'
    assert [m['name'] for m in result['timeline']['add_markers']] == ['EVENT_reach', 'EVENT_look']


def cut_rows(plan, *rows):
    out = []
    for shot_id, camera_id, frame, frame_end in rows:
        out.append({'shot_id': shot_id, 'camera_id': camera_id, 'frame': frame, 'frame_end': frame_end,
                    'frames': frame_end - frame + 1, 'marker': 'CUT_' + shot_id,
                    'camera_object': cameras.camera_names(plan, {'assembly': {'version': 1}}, camera_id)['object']})
    return out


def cut_markers(names):
    return [['CUT_S1', 1, names['A'], True], ['CUT_S2', 31, names['B'], True], ['CUT_S3', 61, names['C'], True]]


def marker_order(row):
    return (row[1], row[0])


class ReadBridge:
    """Answers the read-only timeline payload with fixed markers (and camera liveness)."""

    def __init__(self, markers, fps=30, alive=None):
        self.markers, self.fps, self.alive, self.calls = markers, fps, alive or {}, 0

    def run(self, body, params=None):
        assert body is cameras.TIMELINE_READ_BODY
        self.calls += 1
        return {'timeline': {'frame_start': 1, 'frame_end': 90, 'fps': self.fps, 'fps_base': 1.0, 'frame_current': 1,
                             'camera': None, 'markers': sorted(self.markers, key=marker_order)},
                'cameras': {n: self.alive.get(n, True) for n in params['cameras']}}


class MarkerWorld(ReadBridge):
    """Stand-in for the candidate's markers: the fake timeline mutation writes them, the read payload returns them."""

    def mutate(self, params):
        removed = [m[0] for m in self.markers if m[0] in params['remove']]
        self.markers = [m for m in self.markers if m[0] not in params['remove']]
        self.markers += [[row['name'], row['frame'], None, False] for row in params['add']]
        return {'removed': removed, 'timeline': {'markers': sorted(self.markers, key=marker_order)},
                'fingerprints': {'source': 'src', 'candidate': 'c' + str(len(self.markers))}}


def test_applied_interactions_replace_their_own_markers_only(tmp_path, monkeypatch):
    storage, path, plan = with_cameras(tmp_path, actors=[ACTOR_ROW, CREATURE_ROW])
    write_records(storage, RECORDS)
    world = MarkerWorld([])  # no cuts: the range check below must meet the interaction windows first
    mutate, seen = fake_mutate({'manage_timeline': world.mutate})
    monkeypatch.setattr(store, 'mutate', mutate)
    first = call('cinematic_manage_timeline', storage, path,
                 {'markers': [{'kind': 'BEAT', 'id': 'intro', 'frame': 1}], 'interactions': planned(PLANNED_ROWS[:3])},
                 apply=True, bridge=world)
    params = seen[-1][1]
    assert [r['name'] for r in params['add']] == ['EVENT_reach', 'EVENT_look', 'EVENT_touch', 'BEAT_intro']
    assert params['checks'][0] == {'event_id': 'reach', 'object': 'ActorRig',
                                   'bones': [['hand.R', 'reach'], ['Head', 'look'], ['mixamorig:RightHand', 'touch']]}
    assert first['report']['passed'] and first['interactions']['measurable'] == ['reach', 'touch']
    _, _, state = store.load(storage, path)
    assert state['interactions'] == convert(planned(PLANNED_ROWS[:3]), state={})['events']
    assert state['interaction_conversion']['origin'] == 'production.cinematic_interactions.v1'
    assert state['timeline']['conversions'][-1]['purpose'] == 'interaction:reach:end'
    # Sending interactions again replaces only the interaction markers; BEAT_intro stays.
    call('cinematic_manage_timeline', storage, path,
         {'interactions': [{'event_id': 'x', 'kind': 'REACT', 'actor_id': ACTOR_B, 'frame': 45}]}, apply=True, bridge=world)
    assert seen[-1][1]['remove'] == ['EVENT_look', 'EVENT_reach', 'EVENT_touch']
    assert seen[-1][1]['add'] == [{'name': 'EVENT_x', 'frame': 45}] and seen[-1][1]['checks'][0]['bones'] == []
    # replace_markers replaces only the options.markers markers; EVENT_x stays.
    call('cinematic_manage_timeline', storage, path, {'markers': [{'kind': 'BEAT', 'id': 'end', 'frame': 80}]},
         apply=True, bridge=world)
    assert seen[-1][1]['remove'] == ['BEAT_intro']
    _, _, state = store.load(storage, path)
    assert sorted(r['name'] for r in state['timeline']['narrative']) == ['BEAT_end', 'EVENT_x']
    assert [e['event_id'] for e in state['interactions']] == ['x']
    count = len(seen)
    with pytest.raises(ValueError, match='CINEMATIC_NAME_COLLISION'):
        call('cinematic_manage_timeline', storage, path, {'markers': [{'kind': 'EVENT', 'id': 'x', 'frame': 5}]}, apply=True)
    with pytest.raises(ValueError, match='TIMELINE_MARKER_DUPLICATE'):
        call('cinematic_manage_timeline', storage, path, {'markers': [{'kind': 'EVENT', 'id': 'y', 'frame': 5}],
                                                          'interactions': [{'event_id': 'y', 'kind': 'REACT',
                                                                            'actor_id': ACTOR, 'frame': 6}]}, apply=True)
    with pytest.raises(ValueError, match='TIMELINE_INTERACTION_OUTSIDE'):
        call('cinematic_manage_timeline', storage, path, {'frame_end': 40}, apply=True)
    assert len(seen) == count  # refusals never reached the mutation
    # An empty list clears the interactions and their markers.
    call('cinematic_manage_timeline', storage, path, {'interactions': []}, apply=True, bridge=world)
    _, _, state = store.load(storage, path)
    assert state['interactions'] == [] and [r['name'] for r in state['timeline']['narrative']] == ['BEAT_end']


def test_worst_case_timeline_payload_fits_the_budget():
    add = [{'name': 'EVENT_' + 'e' * 40 + str(i), 'frame': i + 1} for i in range(cameras.MAX_INTERACTION_EVENTS + 20)]
    checks = [{'event_id': 'e' * 48, 'object': 'o' * 63, 'bones': [['b' * 63, 'e' * 48]] * 3}
              for _ in range(cameras.MAX_INTERACTION_EVENTS * 2)]
    params = dict(PARAMS, add=add, remove=[r['name'] for r in add], checks=checks)
    code = build_code(cameras.TIMELINE_BODY, params)
    size = payload_bytes(cameras.TIMELINE_BODY, params)
    nodes = sum(1 for _ in ast.walk(ast.parse(code)))
    assert size < 140_000 and nodes < 14_000, (size, nodes)


# ---------------------------------------------------------------------------------------------------------------
# finalize: CINEMATIC_CANDIDATE -> ASSEMBLED, refused with the full list of failed gates
# ---------------------------------------------------------------------------------------------------------------

GOOD_INTEGRITY = {'schema': 'production.cinematic_integrity.v1', 'passed': True, 'changed': [], 'checked_total': 6,
                  'counts': {'UNCHANGED': 6}, 'source': {'status': 'UNCHANGED'}, 'candidate': {'status': 'UNCHANGED'},
                  'cameras_lights': {'status': 'UNCHANGED'},
                  'actor_reports': [{'actor_id': ACTOR, 'object': 'ActorRig', 'status': 'UNCHANGED', 'changed': 0, 'checked': 4}]}
OBSERVED = {'source': {'fingerprint': 'src'}, 'candidate': {'fingerprint': 'cand'}, 'filepath': 'p'}
BROKEN_INTEGRITY = dict(GOOD_INTEGRITY, passed=False, changed=[
    {'item': 'body_action:Actor_Walk', 'status': 'CHANGED', 'code': 'ACTION_CHANGED', 'actor_id': ACTOR}])


class Calls:
    def __init__(self):
        self.fresh = self.integrity = 0


def default_cuts(plan):
    return cut_rows(plan, ('S1', 'A', 1, 30), ('S2', 'B', 31, 60), ('S3', 'C', 61, 90))


def finalize_setup(tmp_path, monkeypatch, fresh_error=None, integrity=None, shots=None, cuts=default_cuts,
                   extra_markers=(), fps=30, alive=None, narrative=None):
    storage, path, plan = with_cameras(tmp_path, shots=shots)
    _, plan, state = store.load(storage, path)
    names = {cid: state['cameras'][cid]['object'] for cid in 'ABC'}
    state['cuts'] = cuts(plan)
    if narrative is not None:
        state['timeline'] = dict(state['timeline'], narrative=narrative)
    store.record(path, state)
    markers = [[c['marker'], c['frame'], c['camera_object'], True] for c in state['cuts']] + list(extra_markers)
    calls = Calls()

    def fresh(bridge, storage_, plan_, state_):
        calls.fresh += 1
        if fresh_error:
            raise ValueError(fresh_error)
        return OBSERVED

    def report(ctx, plan_, state_):
        calls.integrity += 1
        return deepcopy(integrity or GOOD_INTEGRITY)

    def no_mutation(*args, **kwargs):
        raise AssertionError('finalize must not mutate Blender')
    monkeypatch.setattr(store, 'fresh', fresh)
    monkeypatch.setattr(actors_module, 'integrity_report', report)
    monkeypatch.setattr(store, 'mutate', no_mutation)
    bridge = ReadBridge(markers, fps=fps, alive={names[k]: v for k, v in (alive or {}).items()})
    return storage, path, plan, bridge, calls


def events_of(path):
    return sorted(Path(path).parent.glob('event-*.json'))


def test_finalize_records_assembled_without_touching_blender(tmp_path, monkeypatch):
    storage, path, plan, bridge, calls = finalize_setup(tmp_path, monkeypatch)
    _, _, before = store.load(storage, path)
    result = call('cinematic_manage_timeline', storage, path, {'finalize': True}, apply=True, bridge=bridge)
    assert result['state'] == 'ASSEMBLED' and result['status'] == 'success' and result['blender_changed'] is False
    assert calls.fresh == 1 and calls.integrity == 1 and bridge.calls == 1
    _, _, state = store.load(storage, path)
    assembled = state['assembled']
    assert assembled['candidate_fingerprint'] == 'cand' and assembled['source_fingerprint'] == 'src'
    assert assembled['timeline_report']['passed'] and assembled['integrity']['passed']
    assert assembled['shots'] == [['S1', 'A', 1, 30], ['S2', 'B', 31, 60], ['S3', 'C', 61, 90]]
    assert state['revision'] == before['revision'] and state['history'][-1]['step'] == 'finalize'
    with pytest.raises(ValueError, match='CINEMATIC_STATE_INVALID'):
        call('cinematic_manage_timeline', storage, path, {'finalize': True}, apply=True, bridge=bridge)
    # Any assembly change returns the cinematic to CINEMATIC_CANDIDATE (store.invalidate_review).
    assert store.invalidate_review(state)['state'] == 'CINEMATIC_CANDIDATE'


def test_finalize_proposal_lists_the_gates_without_blender(tmp_path):
    storage, path, plan = with_cameras(tmp_path)
    NoBridge.calls = 0
    result = call('cinematic_manage_timeline', storage, path, {'finalize': True})
    assert result['status'] == 'proposal' and NoBridge.calls == 0 and len(result['gates']) == len(cameras.FINALIZE_GATES)
    assert {e['code'] for e in result['ledger_errors']} == {'FINALIZE_CUTS_MISSING', 'FINALIZE_SHOT_WITHOUT_CUT'}


@pytest.mark.parametrize('state_name,code', [('ASSEMBLED', 'CINEMATIC_STATE_INVALID'), ('REVIEWED', 'CINEMATIC_STATE_INVALID'),
                                             ('PLANNED', 'CINEMATIC_STATE_INVALID'), ('REJECTED', 'GENERATION_TERMINAL')])
def test_finalize_needs_a_candidate(tmp_path, state_name, code):
    storage, path, plan = with_cameras(tmp_path)
    _, plan, state = store.load(storage, path)
    store.record(path, dict(state, state=state_name))
    NoBridge.calls = 0
    with pytest.raises(ValueError, match=code):
        call('cinematic_manage_timeline', storage, path, {'finalize': True}, apply=True)
    assert NoBridge.calls == 0


def no_cuts(plan):
    return []


def gap_cuts(plan):
    return cut_rows(plan, ('S1', 'A', 1, 30), ('S2', 'B', 31, 60))


def unknown_camera_cuts(plan):
    rows = default_cuts(plan)
    rows[2]['camera_id'] = 'Z'
    return rows


def skipped_shot_cuts(plan):
    return cut_rows(plan, ('S1', 'A', 1, 60), ('S3', 'C', 61, 90))


@pytest.mark.parametrize('setup,codes,reads_blender', [
    ({'fresh_error': 'CINEMATIC_STALE_CANDIDATE: the candidate scene changed outside the cinematic tools'},
     {'CINEMATIC_STALE_CANDIDATE'}, True),
    ({'fresh_error': 'CINEMATIC_STALE_SOURCE: the source scene changed outside the cinematic tools',
      'integrity': BROKEN_INTEGRITY}, {'CINEMATIC_STALE_SOURCE', 'ACTION_CHANGED'}, True),
    ({'fresh_error': 'CINEMATIC_OPERATION_UNCERTAIN: inspect the checkpoint'}, {'CINEMATIC_OPERATION_UNCERTAIN'}, False),
    ({'fresh_error': 'CINEMATIC_STALE_ASSET: other file'}, {'CINEMATIC_STALE_ASSET'}, False),
    ({'integrity': BROKEN_INTEGRITY}, {'ACTION_CHANGED'}, True),
    ({'cuts': no_cuts}, {'FINALIZE_CUTS_MISSING', 'FINALIZE_SHOT_WITHOUT_CUT'}, True),
    ({'cuts': gap_cuts}, {'CUTS_DO_NOT_COVER_TIMELINE', 'FINALIZE_SHOT_WITHOUT_CUT'}, True),
    ({'cuts': unknown_camera_cuts}, {'FINALIZE_CUT_CAMERA_UNKNOWN'}, True),
    ({'cuts': skipped_shot_cuts}, {'FINALIZE_SHOT_WITHOUT_CUT'}, True),
    ({'shots': []}, {'FINALIZE_SHOTS_MISSING'}, True),
    ({'alive': {'B': False}}, {'CUT_CAMERA_MISSING'}, True),
    ({'fps': 24}, {'CINEMATIC_FPS_MISMATCH'}, True),
    ({'extra_markers': [['F_10', 10, 'Foreign', False]]}, {'TIMELINE_FOREIGN_CAMERA_MARKER'}, True),
    ({'extra_markers': [['CUT_X', 45, 'M10C_any', True]]}, {'CUT_MARKER_UNRECORDED'}, True),
    ({'narrative': [{'name': 'EVENT_x', 'kind': 'EVENT', 'id': 'x', 'frame': 40, 'source': 'interaction'}]},
     {'TIMELINE_MARKER_MISSING'}, True),
])
def test_finalize_refuses_each_failed_gate(tmp_path, monkeypatch, setup, codes, reads_blender):
    storage, path, plan, bridge, calls = finalize_setup(tmp_path, monkeypatch, **setup)
    events = len(events_of(path))
    with pytest.raises(ValueError) as caught:
        call('cinematic_manage_timeline', storage, path, {'finalize': True}, apply=True, bridge=bridge)
    message = str(caught.value)
    assert message.startswith('CINEMATIC_NOT_ASSEMBLABLE: ')
    assert codes <= set(re.findall(r'\] ([A-Z_]+) ', message)), message
    assert (bridge.calls == 1 and calls.integrity == 1) is reads_blender
    _, _, state = store.load(storage, path)
    assert state['state'] == 'CINEMATIC_CANDIDATE' and 'assembled' not in state and len(events_of(path)) == events


def test_assembly_errors_never_pass_on_a_bare_failed_flag():
    plan = {'shots': DEFAULT_SHOTS}
    state = {'cameras': {'A': {'object': 'CamA'}},
             'cuts': [{'shot_id': s['shot_id'], 'camera_id': 'A', 'camera_object': 'CamA', 'marker': 'CUT_' + s['shot_id'],
                       'frame': s['frame_start'], 'frame_end': s['frame_end']} for s in DEFAULT_SHOTS]}
    errors, _ = cameras.assembly_errors(plan, state, {'passed': True, 'errors': [], 'warnings': []}, {'passed': True})
    assert errors == []
    errors, _ = cameras.assembly_errors(plan, state, {'passed': False, 'errors': [], 'warnings': []},
                                        {'passed': False, 'changed': []})
    assert {e['code'] for e in errors} == {'TIMELINE_REPORT_FAILED', 'INTEGRITY_FAILED'}
    errors, warnings = cameras.assembly_errors(plan, state, {'passed': False, 'error_count': 70, 'warnings': [
        {'code': 'ACTOR_CLIP_GAP', 'detail': 'x'}], 'errors': [{'code': 'SHOT_GAP', 'detail': 'y'}] * 60}, {'passed': True})
    assert 'TIMELINE_REPORT_TRUNCATED' in {e['code'] for e in errors} and warnings[0]['code'] == 'ACTOR_CLIP_GAP'
    assert cameras.not_assemblable(errors).startswith('61 gate failure(s): [timeline_report] SHOT_GAP y')
