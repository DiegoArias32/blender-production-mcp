"""M7 resolution to real controls, ledger, review gate, tool inventory and payload budgets."""
import ast
import json
import math
from pathlib import Path

import pytest

from blender_mcp.production.animation.generation import store
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.facial import resolve as R
from blender_mcp.production.facial import service as SV
from blender_mcp.production.facial import tools as TL


def key(mesh, name, full=1.0):
    return {'type': 'shape_key', 'mesh': mesh, 'key': name, 'min': 0.0, 'max': 1.0, 'full': full}


def bone(name, path, index, full, limit=None):
    return {'type': 'bone', 'bone': name, 'path': path, 'index': index, 'full': full, 'limit': limit}


def profile(mapping, **extra):
    return dict({'mapping': mapping, 'aims': [], 'frame': {'origin': [0, 0, 0], 'left': [1, 0, 0], 'up': [0, 0, 1], 'forward': [0, -1, 0], 'iod_m': 0.04},
                 'driven_keys': [], 'driven_bone_channels': [], 'body': {'animated_bones': []}, 'calibration': {}}, **extra)


def channel(channels, label):
    return next(c for c in channels if label in (c.get('key'), c.get('bone')))


def test_shape_keys_bones_limits_and_unsupported_outputs():
    mapping = {'brow_lower_L': {'usable': True, 'control': key('Face', 'browDownLeft')},
               'jaw_open': {'usable': True, 'control': bone('CTRL-jaw', 'rotation_euler', 0, 0.3, [0.0, 0.3])},
               'tongue_out': {'usable': False, 'control': key('Face', 'tongue')}}
    tracks = {'brow_lower_L': [[1, 0.0], [10, 0.5]], 'jaw_open': [[1, 0.0], [10, 1.5]], 'tongue_out': [[1, 0.0], [10, 0.4]]}
    channels, report = R.resolve(tracks, profile(mapping))
    assert channel(channels, 'browDownLeft')['keys'][-1] == [10, 0.5]
    assert channel(channels, 'CTRL-jaw')['keys'][-1][1] == pytest.approx(0.3)  # clamped to the rig limit
    assert report['clamped'] and report['unsupported'] == ['tongue_out']


def test_one_bilateral_control_takes_the_mean_and_reports_lost_asymmetry():
    shared = key('Face', 'browInnerUp')
    mapping = {'brow_inner_raise_L': {'usable': True, 'control': shared}, 'brow_inner_raise_R': {'usable': True, 'control': shared}}
    channels, report = R.resolve({'brow_inner_raise_L': [[1, 0.8]], 'brow_inner_raise_R': [[1, 0.2]]}, profile(mapping))
    assert channels[0]['keys'][0][1] == pytest.approx(0.5)
    assert any(r.get('asymmetry_lost_max') == pytest.approx(0.6) for r in report['shared'])


def test_driven_controls_and_body_owned_bones_are_never_keyed():
    mapping = {'lip_corner_up_L': {'usable': True, 'control': key('Face', 'smileL')},
               'jaw_open': {'usable': True, 'control': bone('jaw', 'rotation_euler', 0, 0.3)}}
    prof = profile(mapping, driven_keys=['Face:smileL'], body={'animated_bones': ['jaw']})
    channels, report = R.resolve({'lip_corner_up_L': [[1, 0.5]], 'jaw_open': [[1, 0.5]]}, prof)
    assert channels == []
    assert any(e.startswith('DRIVER_CONFLICT') for e in report['errors'])
    assert any(e.startswith('BODY_FACE_CHANNEL_CONFLICT') for e in report['errors'])


def test_lip_seal_uses_every_measured_gain_and_the_jaw_interaction():
    calibration = {'calibrated': True, 'mode': 'JAW_RELATIVE', 'rest_gap_mm': 5.0, 'jaw_gain_mm': 20.0, 'close_gain_mm': 25.0,
                   'interaction_mm': -5.0, 'other_gains_mm': {'lip_press_L': -2.0}, 'linear_model_error_mm': 0.0}
    tracks = {'lips_close': [[1, 1.0]], 'jaw_open': [[1, 0.5]], 'lip_press_L': [[1, 1.0]]}
    sealed = R.seal_track(tracks['lips_close'], tracks, calibration)
    gap = 5.0 + 20.0 * 0.5 - 2.0
    assert sealed[0][1] == pytest.approx(gap / (25.0 + 5.0 * 0.5))


def test_eye_shape_key_pair_and_aim_target_are_solved_from_degrees():
    pos, neg = dict(key('EyeL', 'lookOut'), degrees_at_full=25.0), dict(key('EyeL', 'lookIn'), degrees_at_full=-25.0)
    mapping = {'eye_yaw_L': {'usable': True, 'method': 'shape_key_pair', 'positive': pos, 'negative': neg, 'control': pos},
               'eye_pitch_L': {'usable': True, 'method': 'shape_key_pair', 'positive': dict(pos, key='up'), 'negative': dict(neg, key='down'),
                               'control': pos}}
    channels, _ = R.resolve({'eye_yaw_L': [[1, 12.5]], 'eye_pitch_L': [[1, 0.0]]}, profile(mapping))
    assert channel(channels, 'lookOut')['keys'][0][1] == pytest.approx(0.5) and channel(channels, 'lookIn')['keys'][0][1] == 0.0
    aim = {'target_bone': 'T.L', 'eye_center': [0, 0, 0], 'target_rest_world': [0, -1, 0], 'distance_m': 1.0,
           'target_axes_world': [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}
    mapping = {s: {'usable': True, 'method': 'aim_target', 'aim': aim, 'control': {'type': 'aim_target', 'bone': 'T.L'}}
               for s in ('eye_yaw_L', 'eye_pitch_L')}
    channels, _ = R.resolve({'eye_yaw_L': [[1, 90.0]], 'eye_pitch_L': [[1, 0.0]]}, profile(mapping))
    x = next(c for c in channels if c['index'] == 0)
    assert x['keys'][0][1] == pytest.approx(1.0, abs=1e-6)  # looking 90 deg left moves the target +X by the distance


def test_facial_ledger_has_its_own_category_and_candidate_state(tmp_path):
    storage = Storage(tmp_path)
    reference, _ = store.save(storage, 'test-m7-ledger', {'object_name': 'Face'}, 'facial', store.FACIAL_STATES)
    path, plan, state = SV.load(storage, reference)
    assert state['state'] == 'PLANNED' and path.is_relative_to(tmp_path / 'facial')
    SV.record(path, {'state': 'FACIAL_CANDIDATE', 'candidate_action': 'M7F_x_v1'})
    with pytest.raises(ValueError, match='INVALID_GENERATION_STATE'):
        SV.record(path, {'state': 'GENERATED_CANDIDATE'})
    data = json.loads(Path(reference).read_text())
    data['object_name'] = 'RealMaster'
    Path(reference).write_text(json.dumps(data))
    with pytest.raises(ValueError, match='STALE_PROPOSAL'):
        SV.load(storage, reference)
    with pytest.raises(ValueError, match='REFINEMENT_LIMIT'):
        store.refinement_gate({'state': 'FACIAL_CANDIDATE', 'refinement_cycles': 3})


def test_review_gate_separates_artistic_judgement_and_rejects_scores():
    render = {'status': 'ok', 'temporal_complete': True, 'output_dir': 'D', 'candidate_action': 'C1'}
    review = {'reviewer': 'r', 'artistic_observations': ['reads tense'], 'decision': 'ACCEPT', 'artifact_reference': 'D', 'temporal_reviewed': True}
    assert SV.review_gate(review, render, 'C1') is review
    with pytest.raises(ValueError, match='VISUAL_REVIEW_STALE'):
        SV.review_gate(review, render, 'C2')
    with pytest.raises(ValueError, match='no quality score'):
        SV.review_gate(dict(review, score=9), render, 'C1')
    with pytest.raises(ValueError, match='TEMPORAL_REVIEW_REQUIRED'):
        SV.review_gate(review, dict(render, temporal_complete=False), 'C1')


def test_close_up_cameras_are_identical_except_direction():
    prof = {'frame': {'origin': [0, 0, 1.6], 'left': [1, 0, 0], 'up': [0, 0, 1], 'forward': [0, -1, 0], 'iod_m': 0.06, 'mouth_z': -1.6}}
    rows, settings = SV.cameras(prof, list(SV.VIEWS))
    assert len({tuple(r['target']) for r in rows}) == 1 and len({r['distance_m'] for r in rows}) == 1
    assert settings['lens_mm'] == 85.0
    assert [round(math.degrees(math.acos(-r['direction'][1])), 3) for r in rows] == [0.0, 45.0, 45.0, 90.0]


def test_inventory_and_hint_classes():
    assert len(TL.NAMES) == 46 and len(set(TL.NAMES)) == 46
    assert TL.READ_ONLY.isdisjoint(TL.TRANSIENT)
    assert {'facial_create_brief', 'facial_prepare_speech', 'facial_mocap_analyze'} <= TL.READ_ONLY
    brief = TL.dispatch('facial_create_brief', None, None, 'op', intent='sad')['brief']
    assert brief['emotions']['SADNESS'] > 0
    with pytest.raises(ValueError, match='TIMING_INPUT_REQUIRED'):
        TL.dispatch('facial_prepare_speech', None, None, 'op', options={'timeline': {'audio': 'x.wav'}})


@pytest.mark.parametrize('name', ['WRITE', 'CAPABILITY', 'CANDIDATE_QA', 'INTEGRATION', 'MARK', 'STATUS', 'RENDER', 'HEAD_TRACK'])
def test_scene_payloads_pass_safe_mode_within_budget(name):
    from blender_mcp.production.facial import scene_runtime
    code = build_code(getattr(scene_runtime, name), {'x': 1})
    assert len(code.encode('utf-8')) < 120_000
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 15_000


@pytest.mark.parametrize('name,shipped', [('INSPECT', 'def fz_frame_point'), ('PROBE', 'def fz_field_stats')])
def test_probe_payloads_pass_safe_mode_within_budget(name, shipped):
    from blender_mcp.production.facial import runtime
    code = build_code(getattr(runtime, name), {'x': 1})
    assert shipped in code  # zones.py is shipped verbatim; the linker keeps only what the payload reaches
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 15_000


def test_candidate_versions_never_reuse_a_reverted_number():
    reverted = {'version': 1, 'history': [{'version': 1}, {'version': 2}, {'version': 3}]}
    assert SV.next_version(reverted) == 4
    assert SV.next_version({}) == 1
