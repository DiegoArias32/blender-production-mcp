"""M10 lighting without Blender: plan geometry and measured energy relations, setup normalisation and refusals,
frame selection, PNG metrics/findings, handler wiring, payload safety/budgets, and the quality-report provider
(validation summary bound to the candidate fingerprint, exposure_flicker, lighting_report)."""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path
import re

import pytest

from blender_mcp.production.cinematic import imaging, lighting, model, review, service, store
from blender_mcp.production.cinematic.imaging import Image
from blender_mcp.production.cinematic.service import HANDLERS
from blender_mcp.production.common.bridge import build_code, payload_bytes
from blender_mcp.production.common.payload import link_payload
from blender_mcp.production.common.storage import Storage

GID = 'ab12cd34' + '0' * 24
ACTOR = {'actor_id': '0123456789abcdef', 'object': 'Rig', 'meshes': ['Body'], 'role': 'PROTAGONIST'}
INPUTS = {'subject': {'objects': ['Rig', 'Body'], 'min': [-0.25, -0.15, 0.0], 'max': [0.25, 0.15, 1.8],
                      'source': 'test'},
          'camera': {'object': 'Cam', 'location': [0.0, -6.0, 1.4], 'forward': [0.0, 1.0, 0.0], 'lens': 50.0},
          'scene_lights': [{'name': 'Lamp', 'type': 'POINT', 'energy': 30.0, 'owned': False, 'hide_render': False}],
          'world': {'name': 'W', 'owned': False, 'color': [0.2, 0.2, 0.2], 'strength': 1.0},
          'view': {'view_transform': 'AgX', 'look': 'None', 'exposure': 0.0, 'gamma': 1.0}, 'frame_current': 1}


def _plan(preset='NEUTRAL_THREE_POINT', **opts):
    return lighting.compose_plan(preset, INPUTS, opts, GID, 4, ACTOR)


# ---- plan ----

@pytest.mark.parametrize('preset', [p for p in model.MOOD_PRESETS if p != 'PRACTICAL_ONLY'])
def test_every_preset_plans_from_measured_relations(preset):
    plan = _plan(preset)
    assert plan['schema'] == lighting.PLAN_SCHEMA and plan['generation_id'] == GID and plan['candidate_revision'] == 4
    assert plan['lights'] and all(r['value_source'] == 'PROPOSAL' for r in plan['lights'])
    for row in plan['lights']:
        d = math.dist(row['location'], row['target'])
        assert d == pytest.approx(row['distance_m'], abs=1e-3) and d >= lighting.MIN_DISTANCE - 1e-9
        # Inverting the measured response relation gives back the planned response.
        assert lighting.response_for(row['type'], row['energy'], row['distance_m']) == pytest.approx(row['response'], rel=1e-3)
        assert row['energy_unit'] == ('W/m2' if row['type'] == 'SUN' else 'W')
    assert plan['energy_model']['source'].startswith('MEASURED')
    assert 'not a universal formula' in plan['note']


def test_energy_relations_match_the_measured_calibration():
    measured = lighting.ENERGY_MODEL['measured']
    # response for 1 W at 1 m (or 1 W/m2) from the formula versus what EEVEE rendered at 2 m
    assert lighting.response_for('SUN', 1.0, 1.0) == pytest.approx(measured['sun_per_w_m2'], rel=1e-3)
    assert lighting.response_for('POINT', 1.0, 1.0) == pytest.approx(measured['point_2m'], rel=0.01)
    assert lighting.response_for('AREA', 1.0, 1.0) == pytest.approx(measured['area_small_2m'], rel=0.01)
    assert lighting.energy_for('SPOT', 1.0, 2.0) == lighting.energy_for('POINT', 1.0, 2.0)
    with pytest.raises(ValueError, match='CINEMATIC_CHOICE_INVALID'):
        lighting.energy_for('MESH', 1.0, 1.0)


def test_neutral_geometry_follows_the_camera_axis_and_key_side():
    plan = _plan()
    assert plan['camera_axis']['vector'] == pytest.approx([0.0, -1.0, 0.0], abs=1e-9)
    assert plan['subject']['size_m'] == pytest.approx(1.8) and plan['subject']['aim_point'][2] == pytest.approx(1.26)
    roles = {r['role']: r for r in plan['lights']}
    assert set(roles) == {'KEY', 'FILL', 'RIM'}
    key, fill, rim = roles['KEY'], roles['FILL'], roles['RIM']
    # camera at -Y looking +Y: camera right is +X; the key sits there, the fill opposite, the rim behind (+Y).
    assert key['location'][0] > 0 and fill['location'][0] < 0 and rim['location'][1] > key['target'][1]
    assert key['location'][2] > key['target'][2]
    assert key['distance_m'] == pytest.approx(2.7) and key['type'] == 'AREA' and key['size'] == pytest.approx(0.9)
    assert plan['key_to_fill_response_ratio'] == pytest.approx(2.0)
    mirrored = {r['role']: r for r in _plan(key_side='LEFT')['lights']}
    assert mirrored['KEY']['location'][0] == pytest.approx(-key['location'][0])
    assert any(w['code'] == 'SOURCE_LIGHTS_IN_CANDIDATE' for w in plan['warnings'])


def test_preset_character_is_encoded_not_graded():
    low, high = _plan('LOW_KEY'), _plan('HIGH_KEY')
    assert low['key_to_fill_response_ratio'] > high['key_to_fill_response_ratio']
    assert low['world']['strength'] * max(low['world']['color']) < high['world']['strength'] * max(high['world']['color'])
    horror = {r['role']: r for r in _plan('HORROR_UNDERLIGHT')['lights']}
    assert horror['KEY']['type'] == 'SPOT' and horror['KEY']['location'][2] < horror['KEY']['target'][2]
    assert 'FILL' not in horror
    night = {r['role']: r for r in _plan('NIGHT_EXTERIOR')['lights']}
    assert night['KEY']['type'] == 'SUN' and night['KEY']['energy'] == pytest.approx(math.pi * 0.35, rel=1e-3)
    assert _plan('NIGHT_EXTERIOR')['exposure']['exposure_ev'] == 0.5
    assert _plan(exposure_ev=-1.5)['exposure']['exposure_ev'] == -1.5
    for plan in (low, high):
        assert 'score' not in str(plan).lower() and 'rating' not in str(plan).lower()


def test_without_camera_the_axis_is_an_explicit_assumption():
    plan = lighting.compose_plan('NEUTRAL_THREE_POINT', dict(INPUTS, camera=None), {}, GID, 1, ACTOR)
    assert plan['camera_axis']['vector'] == [0.0, -1.0, 0.0]
    assert plan['camera_axis']['source'].startswith('ASSUMED')
    assert any(w['code'] == 'LIGHTING_NO_CAMERA_AXIS' for w in plan['warnings'])
    overhead = dict(INPUTS, camera={'object': 'C', 'location': [0.0, 0.0, 9.0], 'forward': [0.0, 0.6, -0.8]})
    axis = lighting.compose_plan('NEUTRAL_THREE_POINT', overhead, {}, GID, 1, ACTOR)['camera_axis']
    assert axis['vector'] == pytest.approx([0.0, -1.0, 0.0]) and 'forward' in axis['source']


def test_practical_sources():
    with pytest.raises(ValueError, match='LIGHTING_PRACTICAL_SOURCES_REQUIRED'):
        _plan('PRACTICAL_ONLY')
    plan = _plan('PRACTICAL_ONLY', motivated_sources=[{'position': [1.0, -1.0, 1.26], 'label': 'lamp'},
                                                      {'position': [0.0, 2.0, 1.26], 'energy': 50.0, 'color': [1, 0.8, 0.6]}])
    first, second = plan['lights']
    assert first['role'] == 'PRACTICAL' and first['type'] == 'POINT' and first['temperature'] == 2700.0
    assert first['energy'] == pytest.approx(lighting.energy_for('POINT', 0.4, math.sqrt(2)), rel=1e-3)
    assert second['energy'] == 50.0 and second['temperature'] is None and second['color'] == [1, 0.8, 0.6]
    with pytest.raises(ValueError, match='LIGHTING_MOTIVATED_SOURCES_INVALID'):
        _plan(motivated_sources=[{'position': [1, 0, 1], 'watts': 3}])
    with pytest.raises(ValueError, match='LIGHTING_MOTIVATED_SOURCES_INVALID'):
        _plan(motivated_sources=[{'position': [0.0, 0.0, 1.26]}])
    with pytest.raises(ValueError, match='CINEMATIC_CHOICE_INVALID'):
        _plan('DISCO')


# ---- setup normalisation ----

LEDGER_PLAN = {'generation_id': GID, 'source': {'scene': 'Scene', 'settings': {'view': {'transform': 'AgX'}}},
               'actors': [ACTOR], 'timeline': {'frame_start': 1, 'frame_end': 60}, 'shots': []}


def _state(**extra):
    return dict({'state': 'CINEMATIC_CANDIDATE', 'revision': 4, 'assembly': {'version': 1}}, **extra)


def test_setup_from_plan_names_and_normalises():
    spec = lighting.setup_spec({'plan': _plan()}, LEDGER_PLAN, _state())
    names = [r['name'] for r in spec['lights']]
    assert names == ['M10C_ab12cd34_v1_LGT_KEY', 'M10C_ab12cd34_v1_LGT_FILL', 'M10C_ab12cd34_v1_LGT_RIM']
    assert spec['world']['name'] == 'M10C_ab12cd34_v1_WORLD' and spec['view']['exposure'] == 0.0
    assert spec['revision'] == 1 and not spec['remove_lights'] and not spec['warnings']
    rows = lighting.setup_spec({'plan': _plan('RIM_DRAMATIC')}, LEDGER_PLAN, _state())['lights']
    assert sorted(r['name'] for r in rows if r['role'] == 'RIM') == ['M10C_ab12cd34_v1_LGT_RIM', 'M10C_ab12cd34_v1_LGT_RIM_2']
    blender = lighting._blender_light(spec['lights'][0])
    assert 'info' not in blender and blender['target'] is not None and blender['rotation_euler'] is None
    stale = lighting.setup_spec({'plan': _plan()}, LEDGER_PLAN, _state(revision=9))
    assert stale['warnings'][0]['code'] == 'LIGHTING_PLAN_OLDER_THAN_CANDIDATE'


def test_setup_replace_and_refusals():
    existing = {'setup_revision': 1, 'lights': {'M10C_ab12cd34_v1_LGT_KEY': {}},
                'world': {'name': 'M10C_ab12cd34_v1_WORLD', 'owned': True}}
    with pytest.raises(ValueError, match='LIGHTING_ALREADY_SET'):
        lighting.setup_spec({'plan': _plan()}, LEDGER_PLAN, _state(lighting=existing))
    spec = lighting.setup_spec({'plan': _plan(), 'replace': True}, LEDGER_PLAN, _state(lighting=existing))
    assert spec['remove_lights'] == ['M10C_ab12cd34_v1_LGT_KEY'] and spec['remove_world'] == 'M10C_ab12cd34_v1_WORLD'
    assert spec['lights'][0]['name'].endswith('_LGT_KEY_r2') and spec['world']['name'].endswith('_WORLD_r2')
    only_exposure = lighting.setup_spec({'exposure_ev': 1.0}, LEDGER_PLAN, _state(lighting=existing))
    assert not only_exposure['remove_lights'] and only_exposure['remove_world'] is None
    with pytest.raises(ValueError, match='LIGHTING_PLAN_MISMATCH'):
        lighting.setup_spec({'plan': dict(_plan(), generation_id='f' * 32)}, LEDGER_PLAN, _state())
    with pytest.raises(ValueError, match='LIGHTING_PLAN_INVALID'):
        lighting.setup_spec({'plan': {'lights': []}}, LEDGER_PLAN, _state())
    with pytest.raises(ValueError, match='LIGHTING_INPUT_AMBIGUOUS'):
        lighting.setup_spec({'plan': _plan(), 'lights': []}, LEDGER_PLAN, _state())
    with pytest.raises(ValueError, match='LIGHTING_NOTHING_TO_SET'):
        lighting.setup_spec({}, LEDGER_PLAN, _state())
    with pytest.raises(ValueError, match='CINEMATIC_BOOLEAN_REQUIRED'):
        lighting.setup_spec({'exposure_ev': 0.0, 'replace': 'yes'}, LEDGER_PLAN, _state())
    with pytest.raises(ValueError, match='CINEMATIC_RANGE_INVALID'):
        lighting.setup_spec({'exposure_ev': 40.0}, LEDGER_PLAN, _state())


def test_looks_follow_the_measured_agx_prefix():
    assert lighting.setup_spec({'look': 'AgX - Punchy'}, LEDGER_PLAN, _state())['view']['look'] == 'AgX - Punchy'
    with pytest.raises(ValueError, match='LIGHTING_LOOK_INVALID'):
        lighting.setup_spec({'look': 'Punchy'}, LEDGER_PLAN, _state())
    with pytest.raises(ValueError, match='LIGHTING_LOOK_INVALID'):
        lighting.setup_spec({'view_transform': 'Filmic', 'look': 'AgX - Punchy'}, LEDGER_PLAN, _state())
    assert lighting.setup_spec({'view_transform': 'Filmic', 'look': 'Medium High Contrast'}, LEDGER_PLAN,
                               _state())['view']['view_transform'] == 'Filmic'
    later = _state(lighting={'exposure': {'view_transform': 'Standard'}})
    lighting.setup_spec({'look': 'Medium Contrast'}, LEDGER_PLAN, later)  # current transform is Standard


def test_explicit_light_validation():
    base = {'role': 'KEY', 'type': 'POINT', 'location': [1, 2, 3], 'target': [0, 0, 1], 'energy': 100.0}
    row = lighting.normalize_light(base, 0)
    assert row['use_shadow'] is True and row['temperature'] is None and row['color'] == [1.0, 1.0, 1.0]
    assert lighting.normalize_light(dict(base, size=1.0, size_y=0.5, type='AREA'), 0)['shape'] == 'RECTANGLE'
    assert lighting.normalize_light(dict(base, type='SUN', energy=5.0), 0)['energy'] == 5.0
    cases = [(dict(base, rotation_euler=[0, 0, 0]), 'LIGHTING_AIM_REQUIRED'),
             ({k: v for k, v in base.items() if k != 'target'}, 'LIGHTING_AIM_REQUIRED'),
             (dict(base, type='SUN', energy=5000.0), 'CINEMATIC_RANGE_INVALID'),
             (dict(base, temperature=500.0), 'CINEMATIC_RANGE_INVALID'),
             (dict(base, color=[2, 0, 0]), 'CINEMATIC_RANGE_INVALID'),
             (dict(base, role='GOBO'), 'CINEMATIC_CHOICE_INVALID'),
             (dict(base, material='x'), 'LIGHTING_LIGHT_OPTION_UNSUPPORTED'),
             (dict(base, use_shadow=1), 'CINEMATIC_BOOLEAN_REQUIRED'),
             (dict(base, spot_size=0.0, type='SPOT'), 'CINEMATIC_RANGE_INVALID'),
             ('KEY', 'LIGHTING_LIGHT_INVALID')]
    for raw, code in cases:
        with pytest.raises(ValueError, match=code):
            lighting.normalize_light(raw, 0)
    with pytest.raises(ValueError, match='CINEMATIC_CAPACITY'):
        lighting.setup_spec({'lights': [base] * 33}, LEDGER_PLAN, _state())


# ---- frame selection ----

SHOT_PLAN = dict(LEDGER_PLAN, shots=[{'shot_id': 'S1', 'frame_start': 1, 'frame_end': 30},
                                     {'shot_id': 'S2', 'frame_start': 31, 'frame_end': 60}])


def test_frames_are_sampled_per_shot_with_adjacent_pairs():
    sel = lighting.select_frames({'samples_per_shot': 2}, SHOT_PLAN, _state())
    assert sel['frames'] == [1, 2, 29, 30, 31, 32, 59, 60]
    assert sel['pairs'] == [(1, 2, 'S1'), (29, 30, 'S1'), (31, 32, 'S2'), (59, 60, 'S2')]
    assert sel['shot_of'][30] == 'S1' and sel['shot_of'][31] == 'S2'
    three = lighting.select_frames({}, SHOT_PLAN, _state())
    assert three['samples'] == {'S1': [1, 16, 30], 'S2': [31, 46, 60]}  # 14.5 rounds half up: 1 + 15
    no_pairs = lighting.select_frames({'flicker_pairs': False, 'shots': ['S2'], 'samples_per_shot': 1}, SHOT_PLAN, _state())
    assert no_pairs['frames'] == [45] and no_pairs['pairs'] == []
    explicit = lighting.select_frames({'frames': [10, 40]}, SHOT_PLAN, _state())
    assert explicit['frames'] == [10, 11, 40, 41] and explicit['shot_of'][40] == 'S2'
    timeline = lighting.select_frames({'samples_per_shot': 1}, LEDGER_PLAN, _state())
    assert timeline['samples'] == {'TIMELINE': [30]}
    state_shots = _state(shots=[{'shot_id': 'X', 'frame_start': 5, 'frame_end': 9, 'camera_id': 'A'}])
    assert lighting.select_frames({'samples_per_shot': 1}, SHOT_PLAN, state_shots)['samples'] == {'X': [7]}


def test_frame_selection_refusals():
    for opts, code in (({'shots': ['S9']}, 'LIGHTING_SHOT_UNKNOWN'), ({'shots': []}, 'LIGHTING_SHOTS_INVALID'),
                       ({'frames': [0]}, 'CINEMATIC_RANGE_INVALID'), ({'frames': []}, 'LIGHTING_FRAMES_INVALID'),
                       ({'frames': [5], 'shots': ['S1']}, 'LIGHTING_INPUT_AMBIGUOUS'),
                       ({'samples_per_shot': 20, 'max_frames': 10}, 'LIGHTING_CAPACITY'),
                       ({'samples_per_shot': 1.5}, 'CINEMATIC_INTEGER_REQUIRED')):
        with pytest.raises(ValueError, match=code):
            lighting.select_frames(opts, SHOT_PLAN, _state())


def test_thresholds_are_proposals_and_validated():
    values = lighting.thresholds_from({'max_flicker_stops': 0.2})
    assert values['max_flicker_stops'] == 0.2 and values['clip_level'] == 0.99
    with pytest.raises(ValueError, match='LIGHTING_THRESHOLDS_INVALID'):
        lighting.thresholds_from({'quality': 1})
    with pytest.raises(ValueError, match='CINEMATIC_RANGE_INVALID'):
        lighting.thresholds_from({'clip_level': 2})


# ---- measurement ----

def _frame(width, height, subject_code, background_code, box):
    """EEVEE-like gray frame and its exact Workbench mask (actor 0 = red) for a rectangular subject."""
    eevee, mask = bytearray(), bytearray()
    for y in range(height):
        for x in range(width):
            inside = box[0] <= x < box[2] and box[1] <= y < box[3]
            code = subject_code if inside else background_code
            eevee += bytes((code, code, code))
            mask += b'\xff\x00\x00' if inside else b'\x00\x00\x00'
    return Image(width, height, 3, bytes(eevee)), Image(width, height, 3, bytes(mask))


def test_frame_metrics_contrast_and_mask_integrity():
    eevee, mask = _frame(10, 10, 200, 60, (2, 2, 6, 6))
    row, values, labels = lighting.measure_frame(eevee, mask, [('a', 0)], lighting.THRESHOLDS)
    subject = row['actors']['a']
    assert subject['coverage_fraction'] == 0.16 and row['mask_unmatched_fraction'] == 0
    assert subject['luminance']['mean'] == pytest.approx(imaging.SRGB_LUT[200], abs=1e-6)
    assert row['background']['mean'] == pytest.approx(imaging.SRGB_LUT[60], abs=1e-6)
    expected = math.log2((imaging.SRGB_LUT[200] + 1e-4) / (imaging.SRGB_LUT[60] + 1e-4))
    assert subject['contrast_stops_vs_background'] == pytest.approx(expected, abs=1e-4)
    smeared = Image(10, 10, 3, mask.pixels[:3] + b'\x80\x00\x00' + mask.pixels[6:])
    assert lighting.measure_frame(eevee, smeared, [('a', 0)], lighting.THRESHOLDS)[0]['mask_unmatched_fraction'] == 0.01
    with pytest.raises(ValueError, match='LIGHTING_MASK_SIZE_MISMATCH'):
        lighting.measure_frame(eevee, Image(5, 5, 3, bytes(75)), [('a', 0)], lighting.THRESHOLDS)


def test_pairs_measure_flicker_and_shadow_jump_proxy():
    a = lighting.measure_frame(*_frame(10, 10, 200, 60, (2, 2, 6, 6)), [('a', 0)], lighting.THRESHOLDS)
    b = lighting.measure_frame(*_frame(10, 10, 90, 60, (2, 2, 6, 6)), [('a', 0)], lighting.THRESHOLDS)
    pair = lighting.measure_pair(a, b, [('a', 0)], lighting.THRESHOLDS)
    assert pair['actors']['a']['overlap_pixels'] == 16 and pair['actors']['a']['changed_fraction'] == 1.0
    assert pair['mean_change_stops'] < -0.1
    same = lighting.measure_pair(a, a, [('a', 0)], lighting.THRESHOLDS)
    assert same['mean_change_stops'] == 0 and same['actors']['a']['changed_fraction'] == 0


def test_findings_use_proposal_thresholds_and_skip_cuts():
    t = lighting.THRESHOLDS
    frames = {1: {'shot_id': 'S1', 'luminance': {'clipped_fraction': 0.5, 'crushed_fraction': 0.0},
                  'mask_unmatched_fraction': 0.0,
                  'actors': {'a': {'coverage_fraction': 0.2, 'contrast_stops_vs_background': 0.1,
                                   'luminance': {'clipped_fraction': 0.0, 'crushed_fraction': 0.5}},
                             'b': {'coverage_fraction': 0.0, 'contrast_stops_vs_background': None,
                                   'luminance': {'clipped_fraction': None, 'crushed_fraction': None}}}}}
    pairs = [{'frames': [1, 2], 'shot_id': 'S1', 'cut': False, 'mean_change_stops': 0.3,
              'actors': {'a': {'changed_fraction': 0.5}}},
             {'frames': [2, 3], 'shot_id': 'S1', 'cut': True, 'mean_change_stops': 2.0, 'actors': {}}]
    found = lighting.findings_for(frames, pairs, [('a', 0), ('b', 1)], 'a', t)
    codes = sorted(f['code'] for f in found)
    assert codes == ['LIGHTING_CLIPPED', 'LIGHTING_FLICKER', 'LIGHTING_SHADOW_JUMP', 'LIGHTING_SUBJECT_CRUSHED',
                     'LIGHTING_SUBJECT_LOW_CONTRAST', 'LIGHTING_SUBJECT_NOT_VISIBLE']
    assert all(f['kind'] == 'TECHNICAL' for f in found)
    assert all(f['threshold_source'] == 'PROPOSAL' for f in found if 'threshold' in f)
    assert next(f for f in found if f['code'] == 'LIGHTING_FLICKER')['frames'] == [1, 2]


def test_analyse_end_to_end_on_png_files(tmp_path):
    rendered = {}
    for frame, code in ((1, 200), (2, 200), (10, 120)):
        eevee, mask = _frame(12, 8, code, 50, (3, 2, 7, 6))
        e, m = tmp_path / ('e' + str(frame) + '.png'), tmp_path / ('m' + str(frame) + '.png')
        imaging.write_png(e, eevee)
        imaging.write_png(m, mask)
        rendered[frame] = {'eevee': str(e), 'mask': str(m), 'camera': 'CamA'}
    selection = {'frames': [1, 2, 10], 'shot_of': {1: 'S1', 2: 'S1', 10: 'S1'}, 'pairs': [(1, 2, 'S1')]}
    report = lighting.analyse(rendered, selection, [('a', 0)], 'a', lighting.THRESHOLDS, tmp_path)
    assert report['pairs'][0]['mean_change_stops'] == 0 and not report['pairs'][0]['cut']
    assert report['shots']['S1']['max_flicker_stops'] == 0 and report['shots']['S1']['drift_stops'] > 0
    assert imaging.read_png(report['artifacts']['contact_sheet']).channels == 3
    assert imaging.read_png(report['artifacts']['exposure_sheet']).width > 12


# ---- handlers ----

class FakeBridge:
    def __init__(self, answers):
        self.answers = answers
        self.bodies = []

    def run(self, body, params=None):
        self.bodies.append(body)
        return self.answers[body]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    storage = Storage(tmp_path)
    ref, saved = store.save(storage, 'op-test', dict(SHOT_PLAN, generation_id=None))
    path, plan, _ = store.load(storage, ref)
    store.record(path, _state(fingerprints={'source': 's', 'candidate': 'c'}, timeline={'frame_start': 1, 'frame_end': 60},
                              assembly={'version': 1, 'scene': 'M10C_x_v1', 'collections': {'src': 'a', 'cin': 'b'}}))
    monkeypatch.setattr(store, 'fresh', lambda *a, **k: {})
    monkeypatch.setattr(service, '_loaded', True)  # other subsystems are developed in parallel
    return storage, ref, plan


def test_handlers_are_registered():
    assert {'cinematic_plan_lighting', 'cinematic_setup_lighting', 'cinematic_validate_lighting'} <= set(HANDLERS)


def test_plan_handler_runs_one_read_only_payload(ledger):
    storage, ref, plan = ledger
    bridge = FakeBridge({lighting.PLAN_BODY: INPUTS})
    out = service.dispatch('cinematic_plan_lighting', bridge, storage, 'op', cinematic=ref,
                           options={'preset': 'LOW_KEY'})
    assert bridge.bodies == [lighting.PLAN_BODY]
    assert out['plan']['preset'] == 'LOW_KEY' and out['plan']['generation_id'] == plan['generation_id']
    assert out['plan']['candidate_revision'] == 4 and out['state'] == 'CINEMATIC_CANDIDATE'
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_UNSUPPORTED'):
        service.dispatch('cinematic_plan_lighting', bridge, storage, 'op', cinematic=ref, options={'quality': 'AAA'})
    with pytest.raises(ValueError, match='LIGHTING_SUBJECT_UNKNOWN'):
        service.dispatch('cinematic_plan_lighting', bridge, storage, 'op', cinematic=ref, options={'key_subject': 'f' * 16})
    with pytest.raises(ValueError, match='LIGHTING_SHOT_CAMERA_MISSING'):
        service.dispatch('cinematic_plan_lighting', bridge, storage, 'op', cinematic=ref, options={'shot': 'S1'})


def test_setup_and_validate_proposals_do_not_touch_blender(ledger):
    storage, ref, plan = ledger
    bridge = FakeBridge({})
    lighting_plan = lighting.compose_plan('NEUTRAL_THREE_POINT', INPUTS, {}, plan['generation_id'], 4, ACTOR)
    setup = service.dispatch('cinematic_setup_lighting', bridge, storage, 'op', cinematic=ref,
                             options={'plan': lighting_plan})
    assert setup['status'] == 'proposal' and len(setup['lights']) == 3 and bridge.bodies == []
    validate = service.dispatch('cinematic_validate_lighting', bridge, storage, 'op', cinematic=ref,
                                options={'samples_per_shot': 2, 'size': 320})
    assert validate['status'] == 'proposal' and validate['images'] == 16 and bridge.bodies == []
    assert validate['threshold_source'] == 'PROPOSAL'
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_UNSUPPORTED'):
        service.dispatch('cinematic_validate_lighting', bridge, storage, 'op', cinematic=ref, options={'cpu': True})


# ---- payloads ----

FRAME = re.compile(r'frame_set\(|m3_set_frame\(|m2_set_frame\(')
ASSIGN = re.compile(r'animation_data\.action\s*=(?!=)|m2_assign\(|m3_assign\(|mv_assign\(|\.action_slot\s*=(?!=)')
POSE = re.compile(r'\.rotation_quaternion\s*=(?!=)|\.location\s*=(?!=)|pose_position\s*=(?!=)')


def _identity(**extra):
    base = {'generation_id': GID, 'version': 1, 'scene': 'M10C_ab12cd34_v1',
            'collections': {'src': 'M10C_ab12cd34_v1_SRC', 'cin': 'M10C_ab12cd34_v1_CIN'}, 'source_scene': 'Scene'}
    base.update(extra)
    return base


def _payloads():
    long_path = 'C:/Users/Someone/AppData/Local/Temp/bpm10-lighting-9/store/cinematics-qa/20261023T193433.621975Z-7fbe4e8ea3a9/lighting/eevee/frame_'
    spec = lighting.setup_spec({'lights': [{'role': 'PRACTICAL', 'type': 'AREA', 'location': [i, 1, 2], 'target': [0, 0, 1],
                                            'energy': 10.0 + i} for i in range(model.MAX_LIGHTS)],
                                'world': {'color': [0.1, 0.1, 0.1], 'strength': 1.0}, 'look': 'AgX - Punchy',
                                'exposure_ev': 0.5}, LEDGER_PLAN, _state())
    objects = ['Actor_%d_part_%d_with_a_long_descriptive_name' % (a, p) for a in range(8) for p in range(16)]
    return {
        'plan': (lighting.PLAN_BODY, _identity(subject_objects=objects[:16], camera='M10C_ab12cd34_v1_CAM_A')),
        'setup': (lighting.SETUP_BODY, _identity(lights=[lighting._blender_light(r) for r in spec['lights']],
                                                 world=spec['world'], view=spec['view'],
                                                 remove_lights=['M10C_ab12cd34_v1_LGT_PRACTICAL_%d' % i for i in range(32)],
                                                 remove_world='M10C_ab12cd34_v1_WORLD')),
        'preflight': (lighting.PREFLIGHT_BODY, _identity(actor_objects=objects)),
        'render': (lighting.RENDER_BODY, _identity(actor_objects=objects, mode='EEVEE', width=1920, samples=64,
                                                   palette={n: [1.0, 0.0, 0.0, 1.0] for n in objects},
                                                   frames=[{'frame': f, 'path': long_path + format(f, '06d') + '.png'}
                                                           for f in range(240)])),
    }


@pytest.mark.parametrize('name', ['plan', 'setup', 'preflight', 'render'])
def test_payloads_pass_safe_mode_within_budget(name):
    body, params = _payloads()[name]
    code = build_code(body, params)  # raises on any safe-mode violation
    assert payload_bytes(body, params) < 140_000
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 14_000
    linked = link_payload(body)
    for forbidden in ('lambda', '__class__', 'bpy_extras', 'numpy', 'import os', 'import time', 'hashlib', 'import gpu'):
        assert forbidden not in linked


def test_plan_payload_is_read_only_and_render_payload_is_transient():
    plan = link_payload(lighting.PLAN_BODY)
    assert not FRAME.search(plan) and not ASSIGN.search(plan) and not POSE.search(plan)
    preflight = link_payload(lighting.PREFLIGHT_BODY)
    assert not FRAME.search(preflight) and not ASSIGN.search(preflight) and not POSE.search(preflight)
    render = link_payload(lighting.RENDER_BODY)
    assert FRAME.search(render) and not ASSIGN.search(render)
    # Measured: render(scene=name) renders the window scene's frame; the candidate must render inside temp_override.
    assert 'render.render(write_still=True, scene=' not in render
    assert 'qa_restore(' in render and 'cm_restore(' in render and 'cl_settings_restore(' in render
    setup = link_payload(lighting.SETUP_BODY)
    assert 'node_tree.nodes' in setup and '.materials' not in setup and 'material' not in setup.lower().replace(
        'production_m10', '')
    assert 'source.world =' not in setup and 'cm_fingerprints(params)' in setup


# ---- validation summary, exposure_flicker and the quality-report provider ----

def _analysis(flicker=(0.05, 0.2), findings=None):
    shots = {'S1': {'frames': [1, 2], 'max_flicker_stops': flicker[0], 'drift_stops': 0.1,
                    'pairs': 2 if flicker[0] is not None else 0},
             'S2': {'frames': [31, 32], 'max_flicker_stops': flicker[1], 'drift_stops': 0.3,
                    'pairs': 1 if flicker[1] is not None else 0}}
    if findings is None:
        t = lighting.THRESHOLDS
        findings = [lighting._finding('LIGHTING_MASK_INEXACT', 'ERROR', 0.01, None, t, frame=1, shot_id='S1'),
                    lighting._finding('LIGHTING_SUBJECT_LOW_CONTRAST', 'WARNING', 0.2, 'min_subject_contrast_stops', t,
                                      frame=1, shot_id='S1', actor_id='a'),
                    lighting._finding('LIGHTING_SUBJECT_LOW_CONTRAST', 'WARNING', -0.3, 'min_subject_contrast_stops', t,
                                      frame=31, shot_id='S2', actor_id='a'),
                    lighting._finding('LIGHTING_FLICKER', 'WARNING', 0.2, 'max_flicker_stops', t, frames=[31, 32],
                                      shot_id='S2')]
    return {'shots': shots, 'findings': findings, 'artifacts': {}}


def test_validation_summary_binds_the_fingerprint_and_publishes_exposure_flicker(tmp_path):
    state = _state(fingerprints={'source': 's', 'candidate': 'cand-1'})
    summary = lighting.validation_summary(_analysis(), 4, state, tmp_path, tmp_path / 'r.json')
    assert summary['candidate_fingerprint'] == 'cand-1' and summary['candidate_revision'] == 4
    assert summary['passed'] is False and summary['status'] == 'error' and summary['state_restored'] is True
    metrics = summary['metrics']
    assert metrics['exposure_flicker'] == 0.2 and metrics['exposure_flicker_units'] == lighting.FLICKER_UNITS == 'stops'
    assert metrics['exposure_flicker_status'] == 'MEASURED' and metrics['exposure_flicker_pairs'] == 3
    assert summary['metric_definitions']['exposure_flicker'] == lighting.FLICKER_DEFINITION
    groups = {(g['code'], g['severity']): g for g in summary['findings']}
    assert summary['findings'][0]['severity'] == 'ERROR' and summary['findings_total'] == 4
    contrast = groups[('LIGHTING_SUBJECT_LOW_CONTRAST', 'WARNING')]
    assert contrast['count'] == 2 and contrast['frames'] == [1, 31] and contrast['value_range'] == [-0.3, 0.2]
    assert contrast['shot_ids'] == ['S1', 'S2'] and contrast['threshold_source'] == 'PROPOSAL'
    assert groups[('LIGHTING_FLICKER', 'WARNING')]['frames'] == [[31, 32]]
    assert summary['findings_by_severity'] == {'ERROR': 1, 'WARNING': 3}
    assert not any('score' in key.lower() for key in summary)
    json.dumps(summary)  # the ledger stores it as strict JSON
    unmeasured = lighting.validation_summary(_analysis((None, None), []), 2, state, tmp_path, tmp_path / 'r.json')
    assert unmeasured['metrics']['exposure_flicker'] is None  # an unmeasured flicker is never read as zero
    assert unmeasured['metrics']['exposure_flicker_status'] == 'NOT_MEASURED_NO_SAME_SHOT_PAIR'
    assert unmeasured['passed'] is True and unmeasured['status'] == 'success'
    engine = lighting.validation_summary(_analysis((0.0, 0.0), []), 2, state, tmp_path, tmp_path / 'r.json',
                                         {'code': 'LIGHTING_QA_ENGINE_DIFFERS'})
    assert engine['status'] == 'warning' and engine['metrics']['exposure_flicker'] == 0.0


def test_grouped_findings_keep_every_code_and_severity_and_bound_the_frames():
    t = lighting.THRESHOLDS
    rows = [lighting._finding('LIGHTING_SUBJECT_LOW_CONTRAST', 'WARNING', 0.1, 'min_subject_contrast_stops', t,
                              frame=f, shot_id='S1', actor_id='a') for f in range(1, 31)]
    rows.append(lighting._finding('LIGHTING_SUBJECT_NOT_VISIBLE', 'INFO', 0.0, 'min_subject_coverage_fraction', t,
                                  frame=3, shot_id='S1', actor_id='b'))
    groups = lighting.group_findings(rows)
    assert [(g['code'], g['severity'], g['count']) for g in groups] == [
        ('LIGHTING_SUBJECT_LOW_CONTRAST', 'WARNING', 30), ('LIGHTING_SUBJECT_NOT_VISIBLE', 'INFO', 1)]
    assert len(groups[0]['frames']) == lighting.MAX_GROUP_FRAMES and groups[0]['frames_truncated'] is True
    assert groups[1]['frames_truncated'] is False and groups[1]['actor_ids'] == ['b']


class NoBridge:
    def run(self, body, params=None):
        raise AssertionError('lighting_report must never reach Blender')


def test_lighting_report_returns_only_the_validation_of_the_current_candidate(tmp_path):
    ctx = service.Context(NoBridge(), Storage(tmp_path), 'op')
    validated = _state(fingerprints={'source': 's', 'candidate': 'cand-1'})
    summary = lighting.validation_summary(_analysis(), 4, validated, tmp_path, tmp_path / 'r.json')
    state = dict(validated, qa={'lighting': summary}, lighting={'validation': summary})
    current = lighting.lighting_report(ctx, LEDGER_PLAN, state)
    assert current['passed'] is False and current['status'] == 'error' and current['metrics']['exposure_flicker'] == 0.2
    assert isinstance(current['findings'], list) and current['state_restored'] is True
    section = review.normalize_section(current)
    assert section['status'] == 'FAIL' and section['metrics']['exposure_flicker'] == 0.2
    assert section['metrics']['exposure_flicker_units'] == 'stops' and section['errors']
    stale_states = (dict(state, fingerprints={'source': 's', 'candidate': 'cand-2'}),
                    dict(validated, qa={}, lighting={}),
                    dict(state, qa={'lighting': dict(summary, candidate_fingerprint=None)}, lighting={}),
                    dict(state, fingerprints={}))
    for stale in stale_states:
        out = lighting.lighting_report(ctx, LEDGER_PLAN, stale)
        assert out['passed'] is None and out['status'] == 'NOT_RUN' and out['metrics'] == {}
        assert out['reason'].startswith('LIGHTING_VALIDATION_STALE: ')
        assert review.normalize_section(out)['status'] != 'PASS'
    reason = lighting.lighting_report(ctx, LEDGER_PLAN, stale_states[0])['reason']
    assert lighting._short_fingerprint('cand-1') in reason and lighting._short_fingerprint('cand-2') in reason
    # Measured in the probe: two m521 candidate fingerprints shared their last 12 hex digits; labels must still differ.
    assert lighting._short_fingerprint('m521:1:aa:0123456789abcdef') != lighting._short_fingerprint('m521:1:bb:0123456789abcdef')
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_UNSUPPORTED'):
        lighting.lighting_report(ctx, LEDGER_PLAN, state, {'render': True})


def _render_answer(params):
    """Fake RENDER_BODY: writes the PNGs Blender would write. EEVEE brightness alternates per frame (flicker)."""
    files = []
    for row in params['frames']:
        eevee, mask = _frame(16, 9, 150 + 20 * (row['frame'] % 2), 60, (5, 2, 11, 8))
        imaging.write_png(row['path'], eevee if params['mode'] == 'EEVEE' else mask)
        files.append({'frame': row['frame'], 'path': row['path'], 'camera': 'CamA' if row['frame'] <= 30 else 'CamB'})
    return {'mode': params['mode'], 'files': files, 'errors': [], 'width': 16, 'height': 9, 'restored': True,
            'restore_checks': {}, 'backend': {'engine': 'BLENDER_EEVEE', 'actual_backend': 'GPU_RASTER'}}


class RenderBridge:
    def __init__(self):
        self.bodies = []

    def run(self, body, params=None):
        self.bodies.append(body)
        if body == lighting.PREFLIGHT_BODY:
            return {'has_camera': True, 'missing_objects': [], 'engine': 'BLENDER_EEVEE', 'scene_lights': [],
                    'world': None, 'view': {'view_transform': 'AgX'}}
        if body == lighting.RENDER_BODY:
            return _render_answer(params)
        raise AssertionError('unexpected payload')


def test_validate_records_a_provider_summary_bound_to_the_candidate(ledger, monkeypatch):
    storage, ref, plan = ledger
    monkeypatch.setattr(lighting, 'require_mutable_asset', lambda *a, **k: None)
    bridge = RenderBridge()
    out = service.dispatch('cinematic_validate_lighting', bridge, storage, 'op', cinematic=ref,
                           options={'samples_per_shot': 2, 'size': 16}, apply=True)
    path, plan, state = store.load(storage, ref)
    summary = state['qa']['lighting']
    assert summary == state['lighting']['validation'] and summary['candidate_fingerprint'] == 'c'
    report = json.loads(Path(summary['report']).read_text(encoding='utf-8'))
    same_shot = [abs(p['mean_change_stops']) for p in report['pair_metrics'] if not p['cut']]
    assert len(same_shot) == 4 and summary['metrics']['exposure_flicker'] == round(max(same_shot), 6) > 0
    assert report['metrics'] == summary['metrics'] and out['metrics'] == summary['metrics']
    assert isinstance(out['shots'], dict) and set(out['shots']) == {'S1', 'S2'}  # not hidden by store.public
    calls = len(bridge.bodies)
    ctx = service.Context(bridge, storage, 'op2')
    section = review.run_section(ctx, path, plan, state, review.SECTION_BY_ID['lighting'])
    assert section['provider'] == 'lighting.lighting_report' and section['status'] in ('PASS', 'FAIL')
    assert section['metrics']['exposure_flicker'] == summary['metrics']['exposure_flicker']
    measured = review.measure_target(ctx, path, plan, state, {'source': 'SECTION:lighting', 'target': 'exposure_flicker'})
    assert measured['value'] == summary['metrics']['exposure_flicker']
    changed = dict(state, fingerprints={'source': 's', 'candidate': 'c-after-a-mutation'})
    stale = review.run_section(ctx, path, plan, changed, review.SECTION_BY_ID['lighting'])
    assert stale['status'] != 'PASS' and stale['reason'].startswith('LIGHTING_VALIDATION_STALE')
    with pytest.raises(ValueError, match='REFINEMENT_TARGET_UNAVAILABLE'):
        review.measure_target(ctx, path, plan, changed, {'source': 'SECTION:lighting', 'target': 'exposure_flicker'})
    assert len(bridge.bodies) == calls  # neither the provider nor the refinement read renders anything
