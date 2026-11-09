"""M10 planning without Blender: intent -> brief, beats, shots, actor paths, interactions, render plan, the
normalize_plan contract and the read-only payload budgets. A fake bridge answers the read payloads from a scene
dictionary and records every body it receives, so the tests also prove no state-changing payload is ever sent."""
from __future__ import annotations

import ast
from copy import deepcopy
import math
import re

import pytest

from blender_mcp.production.cinematic import lifecycle, model, planning, registry
from blender_mcp.production.cinematic import planning_handlers as handlers
from blender_mcp.production.cinematic.service import HANDLERS, dispatch, load_handlers
from blender_mcp.production.common.asset_safety import CURRENT_FILE
from blender_mcp.production.common.bridge import build_code, payload_bytes
from blender_mcp.production.common.payload import link_payload
from blender_mcp.production.common.storage import Storage

FRAME = re.compile(r'frame_set\(|m3_set_frame\(|m2_set_frame\(')
ASSIGN = re.compile(r'animation_data\.action\s*=(?!=)|m2_assign\(|m3_assign\(|mv_assign\(|\.action_slot\s*=(?!=)')
POSE = re.compile(r'\.rotation_quaternion\s*=(?!=)|\.location\s*=(?!=)|pose_position\s*=(?!=)')
PLANNING_TOOLS = ('cinematic_create_brief', 'cinematic_plan_beats', 'cinematic_plan_shots', 'cinematic_plan_actor_paths',
                  'cinematic_plan_interaction', 'cinematic_render_plan')

SCENE = {
    'settings': {'name': 'Scene', 'frame_start': 1, 'frame_end': 250, 'frame_current': 1, 'fps': 30, 'fps_base': 1.0,
                 'camera': None, 'world': None, 'engine': 'BLENDER_EEVEE', 'resolution': [1920, 1080, 100],
                 'pixel_aspect': [1, 1], 'units': {}, 'view': {}, 'markers': []},
    'objects': {'Creature': {'type': 'ARMATURE', 'location': [0.0, -6.0, 0.0], 'dimensions': [0.4, 0.4, 1.0]},
                'CreatureBody': {'type': 'MESH', 'location': [0.0, -6.0, 0.6], 'dimensions': [0.8, 1.6, 1.2]},
                'Hero': {'type': 'ARMATURE', 'location': [0.0, 2.0, 0.0], 'dimensions': [0.3, 0.2, 1.7]},
                'HeroBody': {'type': 'MESH', 'location': [0.0, 2.0, 0.9], 'dimensions': [0.5, 0.3, 1.8]},
                'Rock': {'type': 'MESH', 'location': [3.0, 0.0, 0.0], 'dimensions': [1.0, 1.0, 1.0]},
                'Hidden': {'type': 'EMPTY', 'location': [0.0, 0.0, 0.0], 'dimensions': [0.0, 0.0, 0.0], 'in_scene': False}},
    'actions': {'Creature_Walk': [1, 31], 'Creature_Idle': [1, 61], 'Hero_Idle': [1, 61], 'Orphan_Action': [1, 10]},
}
BACKEND_NONE = {'gpu_backend': 'OPENGL', 'cycles_addon': True, 'compute_device_type': 'NONE', 'devices': [],
                'preferences_modified': False}
BACKEND_OPTIX = dict(BACKEND_NONE, compute_device_type='OPTIX',
                     devices=[['NVIDIA GeForce RTX 4060', 'OPTIX', True], ['AMD Ryzen 7 5700G', 'CPU', False]])


class FakeBridge:
    """Answers only the read payloads the planning tools are allowed to send; any other body fails the test."""

    def __init__(self, scene=None, backend=None):
        self.scene = deepcopy(scene or SCENE)
        self.backend = backend or BACKEND_NONE
        self.bodies = []

    def run(self, body, params=None):
        self.bodies.append(body)
        if body is lifecycle.SOURCE_INFO:
            # Same keys as lifecycle.SOURCE_INFO (cameras_lights is the per-object camera/light baseline of the plan).
            return {'scene': 'Scene', 'settings': deepcopy(self.scene['settings']), 'fingerprint': 'fp-scene',
                    'objects': len(self.scene['objects']), 'filepath': '',
                    'cameras_lights': {'digest': 'cl-none', 'count': 0, 'objects': {}, 'scene_camera': None}}
        if body is CURRENT_FILE:
            return {'filepath': '', 'stamps': []}
        if body is handlers.PRESENCE:
            objects = {}
            for name in params['objects']:
                row = self.scene['objects'].get(name)
                objects[name] = None if row is None else {
                    'type': row['type'], 'in_scene': row.get('in_scene', True), 'location': list(row['location']),
                    'yaw_deg': 0.0, 'dimensions': list(row['dimensions'])}
            actions = {n: ({'frame_range': self.scene['actions'][n]} if n in self.scene['actions'] else None)
                       for n in params['actions']}
            return {'scene': 'Scene', 'frame_current': 1, 'objects': objects, 'actions': actions}
        if body is handlers.BACKEND:
            return deepcopy(self.backend)
        raise AssertionError('unexpected payload sent by a planning tool')


CREATURE = {'object': 'Creature', 'data': 'CreatureRig', 'type': 'ARMATURE', 'role': 'CREATURE', 'meshes': ['CreatureBody'],
            'fingerprint': 'fp-creature', 'interaction_capabilities': {'reach_m': 1.2},
            'body_actions': [{'action': 'Creature_Walk', 'slot': 'OBCreature', 'frame_range': [1, 31], 'digest': 'd1',
                              'root_motion': {'horizontal_distance_m': 1.5, 'duration_frames': 30, 'direction': [0.0, 1.0]}},
                             {'action': 'Creature_Idle', 'slot': 'OBCreature', 'frame_range': [1, 61], 'digest': 'd2',
                              'root_motion': {'horizontal_distance_m': 0.0, 'duration_frames': 60}}],
            'facial': [], 'secondary': []}
HERO = {'object': 'Hero', 'data': 'HeroRig', 'type': 'ARMATURE', 'role': 'PROTAGONIST', 'meshes': ['HeroBody'],
        'fingerprint': 'fp-hero',
        'body_actions': [{'action': 'Hero_Idle', 'slot': 'OBHero', 'frame_range': [1, 61], 'digest': 'd3',
                          'root_motion': {'horizontal_distance_m': 0.01, 'duration_frames': 60}}],
        'facial': [{'ref': 'facial-1', 'state': 'CANDIDATE'}], 'secondary': [{'ref': 'phys-1', 'state': 'ACCEPTED'}]}
INTENT = {
    'title': 'Creature reveal', 'duration_s': 15.0, 'fps': 30,
    'actors': [{'object': 'Creature', 'role': 'CREATURE', 'label': 'creature'}, {'object': 'Hero', 'label': 'hero'}],
    'events': [
        {'event_id': 'emerge', 'kind': 'REVEAL', 'actors': ['creature'], 'time_s': 2.0,
         'description': 'The creature emerges from the dark'},
        {'event_id': 'approach', 'kind': 'APPROACH', 'actors': ['creature'], 'target': 'hero',
         'description': 'It approaches the hero'},
        {'event_id': 'look', 'kind': 'GAZE', 'actors': ['creature'], 'target': 'hero', 'description': 'It looks at the hero'},
        {'event_id': 'prepare', 'kind': 'PREPARE_ATTACK', 'actors': ['creature'], 'target': 'hero', 'time_s': 11.0,
         'description': 'It prepares to attack'}],
    'atmosphere': {'mood': 'ominous', 'mood_preset': 'LOW_KEY'},
}


@pytest.fixture
def world(tmp_path):
    storage = Storage(tmp_path / 'store')
    key = registry.asset_key({'role': 'TEMPORARY', 'asset_id': None}, 'Scene')
    creature = registry.write(storage, key, dict(CREATURE))
    hero = registry.write(storage, key, dict(HERO))
    return {'storage': storage, 'key': key, 'creature': creature['actor_id'], 'hero': hero['actor_id'], 'tmp': tmp_path}


def call(world, name, bridge=None, **kwargs):
    bridge = bridge or FakeBridge()
    result = dispatch(name, bridge, world['storage'], 'op-test', **kwargs)
    for body in bridge.bodies:
        linked = link_payload(body)
        assert not (FRAME.search(linked) or ASSIGN.search(linked) or POSE.search(linked)), name
    return result


def brief_of(world, intent=None, bridge=None):
    return call(world, 'cinematic_create_brief', bridge=bridge, brief=deepcopy(intent or INTENT))


# ---------------------------------------------------------------------------------------------------------- brief

def test_planning_handlers_are_registered():
    load_handlers()
    assert set(PLANNING_TOOLS) <= set(HANDLERS)


@pytest.mark.parametrize('raw, code', [
    (None, 'CINEMATIC_INTENT_REQUIRED'), ({}, 'CINEMATIC_INTENT_REQUIRED'),
    ({'schema': planning.BRIEF_SCHEMA, 'duration_s': 1}, 'CINEMATIC_INTENT_INVALID'),
    ({'duration_s': 2, 'colour': 'red'}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ({'duration_s': float('nan')}, 'CINEMATIC_NUMBER_INVALID'),
    ({'duration_s': 500}, 'CINEMATIC_RANGE_INVALID'),
    ({'duration_s': 2, 'events': [{'kind': 'DANCE'}]}, 'CINEMATIC_CHOICE_INVALID'),
    ({'duration_s': 2, 'events': [{'kind': 'REVEAL', 'time_s': 3}]}, 'CINEMATIC_RANGE_INVALID'),
    ({'duration_s': 2, 'events': [{'kind': 'REVEAL', 'event_id': 'a'}, {'kind': 'ENDING', 'event_id': 'a'}]},
     'CINEMATIC_EVENT_ID_DUPLICATE'),
    ({'duration_s': 2, 'actors': [{'role': 'CREATURE'}]}, 'CINEMATIC_ACTOR_REF_REQUIRED'),
    ({'duration_s': 2, 'timing_constraints': [{'event_id': 'a', 'beat_kind': 'REVEAL', 'at_s': 1}]}, 'CINEMATIC_CONSTRAINT_INVALID'),
])
def test_intent_is_strictly_validated(raw, code):
    with pytest.raises(ValueError, match=code):
        planning.normalize_intent(raw)


def test_create_brief_resolves_actors_and_separates_the_three_provenance_values(world):
    result = brief_of(world)
    brief = result['brief']
    assert result['status'] == 'success' and brief['ready'] and result['blender_changed'] is False
    assert brief['schema'] == planning.BRIEF_SCHEMA and brief['duration_s'] == 15.0 and brief['fps'] == 30
    assert [a['actor_id'] for a in brief['actors']] == [world['creature'], world['hero']]
    assert brief['actors'][0]['role'] == 'CREATURE' and brief['actors'][1]['role'] == 'PROTAGONIST'
    groups = brief['provenance_groups']
    assert set(groups) == set(model.PROVENANCE)
    assert 'fps' in groups['USER_REQUIREMENT'] and 'duration_s' in groups['USER_REQUIREMENT']
    assert 'fps_base' in groups['INFERRED_DEFAULT'] and 'frame_start' in groups['INFERRED_DEFAULT']
    assert 'actors.' + world['hero'] + '.role' in groups['INFERRED_DEFAULT']
    assert groups['PROPOSED_CREATIVE_DECISION'] == ['lighting_intentions']
    assert brief['lighting_intentions'] == {'preset': 'LOW_KEY'}
    # The three groups never share a field.
    names = [n for values in groups.values() for n in values]
    assert len(names) == len(set(names))
    assert brief['events'][1]['target'] == {'type': 'ACTOR', 'actor_id': world['hero']}
    performance = brief['available_performances'][world['creature']]
    assert [a['action'] for a in performance['body_actions']] == ['Creature_Walk', 'Creature_Idle']
    assert performance['body_actions'][0]['root_motion']['horizontal_distance_m'] == 1.5
    assert performance['measured']['height_m'] == 1.2
    assert performance['measured']['footprint_radius_m'] == pytest.approx(math.hypot(0.8, 1.6) / 2, abs=1e-6)
    assert brief['timeline']['frame_end'] == 450 and brief['timeline']['frames'] == 450
    assert {r['code'] for r in brief['risks']} == {'EVENTS_UNSCHEDULED', 'ACTIONS_SHORTER_THAN_TIMELINE'}
    assert brief['source']['resolution'] == [1920, 1080, 100]


def test_brief_defaults_fps_from_the_scene_and_refuses_another_fps(world):
    intent = dict(INTENT)
    intent.pop('fps')
    brief = brief_of(world, intent)['brief']
    assert brief['fps'] == 30 and brief['provenance']['fps'] == 'INFERRED_DEFAULT'
    with pytest.raises(ValueError, match='CINEMATIC_FPS_MISMATCH'):
        brief_of(world, dict(INTENT, fps=24))


def test_missing_prerequisites_are_reported_not_invented(world):
    intent = deepcopy(INTENT)
    intent['actors'].append({'object': 'Ghost', 'label': 'ghost'})
    intent['required_performances'] = [
        {'actor': 'creature', 'body_actions': ['Creature_Run', 'Orphan_Action']},
        {'actor': 'hero', 'facial': True, 'secondary': ['phys-1']}]
    intent['events'].append({'kind': 'REACT', 'actors': ['ghost'], 'description': 'The ghost reacts'})
    intent['events'].append({'kind': 'TOUCH_SURFACE', 'actors': ['hero'], 'target': 'Nowhere'})
    intent.pop('duration_s')
    result = brief_of(world, intent)
    brief = result['brief']
    codes = sorted(m['code'] for m in brief['missing_prerequisites'])
    assert codes == ['ACTION_NOT_AVAILABLE', 'ACTION_NOT_AVAILABLE', 'ACTOR_NOT_REGISTERED', 'DURATION_REQUIRED',
                     'FACIAL_PERFORMANCE_NOT_ACCEPTED', 'TARGET_OBJECT_MISSING']
    details = {m['ref']: m['detail'] for m in brief['missing_prerequisites'] if m['code'] == 'ACTION_NOT_AVAILABLE'}
    assert 'exists in the file' in details['Orphan_Action'] and 'never generates' in details['Creature_Run']
    assert result['status'] == 'needs_input' and brief['ready'] is False and brief['duration_s'] is None
    assert any('duration' in q for q in brief['open_questions'])
    assert [a['object'] for a in brief['actors']] == ['Creature', 'Hero']
    assert brief['events'][4]['actors'] == []
    with pytest.raises(ValueError, match='CINEMATIC_BRIEF_NOT_READY'):
        call(world, 'cinematic_plan_beats', brief=brief)
    with pytest.raises(ValueError, match='CINEMATIC_BRIEF_NOT_READY'):
        planning.normalize_plan(brief, None, None, SCENE['settings'])


def test_unregistered_or_hidden_actor_objects_block_the_brief(world):
    bridge = FakeBridge()
    bridge.scene['objects'].pop('Hero')
    brief = brief_of(world, bridge=bridge)['brief']
    assert [m['code'] for m in brief['missing_prerequisites']] == ['ACTOR_OBJECT_MISSING'] and not brief['ready']


def test_event_at_or_after_the_end_is_refused(world):
    intent = deepcopy(INTENT)
    intent['events'][3]['time_s'] = 15.0
    with pytest.raises(ValueError, match='CINEMATIC_EVENT_OUTSIDE_DURATION'):
        brief_of(world, intent)


# ---------------------------------------------------------------------------------------------------------- beats

def test_example_beats_match_the_request_and_convert_exactly(world):
    brief = brief_of(world)['brief']
    beats = call(world, 'cinematic_plan_beats', brief=brief)
    rows = beats['beats']
    assert [b['kind'] for b in rows] == ['ESTABLISHING', 'REVEAL', 'APPROACH', 'RECOGNITION', 'THREAT', 'ENDING']
    assert [(b['frame_start'], b['frame_end']) for b in rows] == [(1, 60), (61, 150), (151, 240), (241, 330),
                                                                 (331, 390), (391, 450)]
    for previous, beat in zip(rows, rows[1:]):
        assert beat['frame_start'] == previous['frame_end'] + 1 and beat['start_s'] == previous['end_s']
    for beat in rows:
        frame, _ = model.seconds_to_frame(beat['start_s'], 30, 1.0, 1)
        assert frame == beat['frame_start'] and beat['duration_frames'] == beat['frame_end'] - beat['frame_start'] + 1
    assert all(c['rounding_error_frames'] == 0 for c in beats['conversions'])
    assert rows[1]['provenance']['start_s'] == 'USER_REQUIREMENT' and rows[4]['provenance']['start_s'] == 'USER_REQUIREMENT'
    assert rows[2]['provenance']['start_s'] == 'PROPOSED_CREATIVE_DECISION'
    assert rows[0]['provenance']['kind'] == 'PROPOSED_CREATIVE_DECISION' and rows[1]['provenance']['kind'] == 'USER_REQUIREMENT'
    assert rows[3]['provenance']['kind'] == 'PROPOSED_CREATIVE_DECISION'  # GAZE -> RECOGNITION is a mapping proposal
    assert rows[2]['actors'] == [world['creature'], world['hero']]
    assert rows[2]['staging_constraints'][0]['type'] == 'MOVE_TOWARD'
    assert rows[3]['staging_constraints'][0]['type'] == 'FACE_TARGET'
    assert rows[0]['actors'] == [world['creature'], world['hero']] and rows[0]['provenance']['actors'] == 'PROPOSED_CREATIVE_DECISION'
    assert rows[-1]['transition'] == 'HOLD' and rows[2]['dependencies'] == [rows[1]['beat_id']]
    schedule = {s['event_id']: s for s in beats['event_schedule']}
    assert schedule['emerge']['frame'] == 61 and schedule['prepare']['frame'] == 331
    assert schedule['approach']['time_s'] == 5.0 and schedule['approach']['time_source'] == 'PROPOSED_CREATIVE_DECISION'
    assert beats['status'] == 'success' and beats['blender_changed'] is False


def test_beat_overrides_full_list_patch_gap_and_conflicts(world):
    brief = brief_of(world)['brief']
    custom = call(world, 'cinematic_plan_beats', brief=brief, options={'beats': [
        {'kind': 'ESTABLISHING', 'duration_s': 3.0}, {'kind': 'APPROACH', 'events': ['approach'], 'weight': 3.0},
        {'kind': 'THREAT', 'events': ['prepare'], 'gap_before_s': 0.5}, {'kind': 'ENDING', 'duration_s': 2.0}]})
    rows = custom['beats']
    # 15 s - 3 s - 2 s - 0.5 s gap = 9.5 s shared 3:1 between APPROACH and THREAT.
    assert [b['start_s'] for b in rows] == [0.0, 3.0, 10.625, 13.0]
    assert rows[1]['end_s'] == pytest.approx(10.125) and custom['gaps'][0]['frames'] == 15
    assert rows[2]['frame_start'] == rows[1]['frame_end'] + 1 + 15
    patched = call(world, 'cinematic_plan_beats', brief=brief, options={'beats': {'B03_APPROACH': {'duration_s': 4.0}}})
    approach = patched['beats'][2]
    assert approach['duration_s'] == pytest.approx(4.0) and approach['provenance']['duration_s'] == 'USER_REQUIREMENT'
    # REVEAL (2 s) .. THREAT (11 s) minus the 4 s APPROACH leaves 5 s split equally: placement is a proposal.
    assert approach['start_s'] == pytest.approx(4.5) and approach['provenance']['start_s'] == 'PROPOSED_CREATIVE_DECISION'
    with pytest.raises(ValueError, match='CINEMATIC_BEAT_UNKNOWN'):
        call(world, 'cinematic_plan_beats', brief=brief, options={'beats': {'B99': {'duration_s': 1.0}}})
    with pytest.raises(ValueError, match='CINEMATIC_BEAT_TIMES_INCONSISTENT'):
        call(world, 'cinematic_plan_beats', brief=brief, options={'beats': [
            {'kind': 'ESTABLISHING', 'duration_s': 10.0}, {'kind': 'ENDING', 'duration_s': 10.0}]})
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_UNSUPPORTED'):
        call(world, 'cinematic_plan_beats', brief=brief, options={'tempo': 1})
    intent = deepcopy(INTENT)
    intent['events'][0]['time_s'] = 12.0  # REVEAL after the 11 s THREAT: order conflict, never reordered silently
    with pytest.raises(ValueError, match='CINEMATIC_BEAT'):
        call(world, 'cinematic_plan_beats', brief=brief_of(world, intent)['brief'])


def test_timing_constraints_are_checked_not_forced(world):
    intent = deepcopy(INTENT)
    intent['timing_constraints'] = [{'event_id': 'look', 'not_after_s': 6.0}, {'beat_kind': 'ENDING', 'min_duration_s': 3.0}]
    beats = call(world, 'cinematic_plan_beats', brief=brief_of(world, intent)['brief'])
    assert beats['status'] == 'warning'
    assert {(v.get('event_id') or v.get('beat_id'), v['constraint']) for v in beats['constraint_violations']} == {
        ('look', 'not_after_s'), ('B06_ENDING', 'min_duration_s')}


def test_fractional_fps_conversion_is_recorded(world):
    bridge = FakeBridge()
    bridge.scene['settings'].update(fps=30, fps_base=1.001)
    brief = brief_of(world, dict(INTENT, fps=30), bridge=bridge)['brief']
    beats = call(world, 'cinematic_plan_beats', brief=brief)
    assert beats['timeline']['frame_end'] == 1 + math.floor(15 * 30 / 1.001 + 0.5) - 1
    assert any(abs(c['rounding_error_frames']) > 0 for c in beats['conversions'])
    for previous, beat in zip(beats['beats'], beats['beats'][1:]):
        assert beat['frame_start'] == previous['frame_end'] + 1


# ---------------------------------------------------------------------------------------------------------- shots

def test_default_shots_tile_the_timeline_with_labelled_proposals(world):
    brief = brief_of(world)['brief']
    beats = call(world, 'cinematic_plan_beats', brief=brief)
    shots = call(world, 'cinematic_plan_shots', brief=brief, options={'beats': beats})
    rows = shots['shots']
    assert [s['type'] for s in rows] == ['ESTABLISHING', 'WIDE', 'TRACKING', 'CLOSE_UP', 'LOW_ANGLE', 'WIDE']
    assert rows[0]['frame_start'] == 1 and rows[-1]['frame_end'] == 450
    for previous, shot in zip(rows, rows[1:]):
        assert shot['frame_start'] == previous['frame_end'] + 1
    assert all(s['type'] in model.SHOT_TYPES and s['movement'] in model.CAMERA_MOVES for s in rows)
    assert shots['threshold_source'] == 'PROPOSAL' and rows[0]['framing']['threshold_source'] == 'PROPOSAL'
    assert rows[2]['movement'] == 'TRACKING' and rows[2]['easing'] == 'EASE_IN_OUT'
    close = rows[3]
    sensor_v = 36.0 * 1080 / 1920
    expected = 0.15 * 1.2 * 85.0 / (0.60 * sensor_v)
    assert close['suggested_distance_m'] == pytest.approx(expected, abs=1e-3)
    assert close['camera_height']['height_m'] == pytest.approx(0.92 * 1.2, abs=1e-6)
    assert close['focus_target'] == {'actor_id': world['creature'], 'region': 'HEAD'}
    assert rows[-1]['transition'] == 'HOLD' and rows[0]['provenance']['type'] == 'PROPOSED_CREATIVE_DECISION'
    assert shots['coverage'] == {'frame_start': 1, 'frame_end': 450, 'frames': 450, 'contiguous': True}


def test_shot_overrides_split_beats_and_refuse_gaps_and_overlaps(world):
    brief = brief_of(world)['brief']
    beats = call(world, 'cinematic_plan_beats', brief=brief)
    ids = [b['beat_id'] for b in beats['beats']]
    rows = [{'beat_id': ids[0]}, {'beat_id': ids[1], 'type': 'LOW_ANGLE'}, {'beat_id': ids[1], 'type': 'REACTION'}] + [
        {'beat_id': b} for b in ids[2:]]
    shots = call(world, 'cinematic_plan_shots', brief=brief, options={'beats': beats, 'shots': rows})['shots']
    assert (shots[1]['frame_start'], shots[1]['frame_end'], shots[2]['frame_start'], shots[2]['frame_end']) == (61, 105, 106, 150)
    assert shots[1]['provenance']['type'] == 'USER_REQUIREMENT'
    patched = call(world, 'cinematic_plan_shots', brief=brief, options={'beats': beats, 'shots': {'S04': {'lens_mm': 100}}})
    assert patched['shots'][3]['lens_mm'] == 100 and patched['shots'][3]['provenance']['lens_mm'] == 'USER_REQUIREMENT'
    explicit = [{'frame_start': 1, 'frame_end': 200}, {'frame_start': 202, 'frame_end': 450}]
    with pytest.raises(ValueError, match='CINEMATIC_SHOT_GAP'):
        call(world, 'cinematic_plan_shots', brief=brief, options={'beats': beats, 'shots': explicit})
    explicit = [{'frame_start': 1, 'frame_end': 200}, {'frame_start': 200, 'frame_end': 450}]
    with pytest.raises(ValueError, match='CINEMATIC_SHOT_OVERLAP'):
        call(world, 'cinematic_plan_shots', brief=brief, options={'beats': beats, 'shots': explicit})
    with pytest.raises(ValueError, match='CINEMATIC_SHOT_COVERAGE'):
        call(world, 'cinematic_plan_shots', brief=brief, options={'beats': beats, 'shots': [{'frame_start': 1, 'frame_end': 449}]})
    with pytest.raises(ValueError, match='CINEMATIC_SHOT_SUBJECT_UNKNOWN'):
        call(world, 'cinematic_plan_shots', brief=brief, options={'shots': {'S01': {'subjects': ['nobody']}}})


# ----------------------------------------------------------------------------------------------- normalize_plan

def test_normalize_plan_keeps_its_contract_and_validates_beats_and_shots(world):
    brief = brief_of(world)['brief']
    beats = call(world, 'cinematic_plan_beats', brief=brief)
    shots = call(world, 'cinematic_plan_shots', brief=brief, options={'beats': beats})
    plan = planning.normalize_plan(brief, beats, shots, SCENE['settings'])
    assert set(plan) == {'brief', 'beats', 'shots', 'timeline'} and len(plan['shots']) == 6
    assert plan['timeline']['frame_end'] == 450
    minimal = {'schema': planning.BRIEF_SCHEMA, 'duration_s': 2.0, 'fps': 30, 'actors': [{'actor_id': 'a' * 16, 'role': 'PROTAGONIST'}]}
    assert planning.normalize_plan(minimal, None, None, SCENE['settings'])['timeline']['frame_end'] == 60
    loose = {'schema': planning.SHOTS_SCHEMA, 'shots': [{'frame_start': 1, 'frame_end': 30, 'camera_id': 'CAM_A'}]}
    assert planning.normalize_plan(minimal, None, loose, SCENE['settings'])['shots'][0]['camera_id'] == 'CAM_A'

    def broken(mutate, code, which='shots'):
        b, s = deepcopy(beats), deepcopy(shots)
        mutate(b if which == 'beats' else s)
        with pytest.raises(ValueError, match=code):
            planning.normalize_plan(brief, b, s, SCENE['settings'])

    broken(lambda s: s['shots'][1].update(shot_id='S01'), 'CINEMATIC_SHOT_ID_DUPLICATE')
    broken(lambda s: s['shots'][1].update(frame_start=50), 'CINEMATIC_SHOT_OVERLAP')
    broken(lambda s: s['shots'][5].update(frame_end=451), 'CINEMATIC_SHOT_OUTSIDE_TIMELINE')
    broken(lambda s: s['shots'][0].update(subjects=['f' * 16]), 'CINEMATIC_SHOT_SUBJECT_UNKNOWN')
    broken(lambda s: s['shots'][0].update(beat_id='B77'), 'CINEMATIC_SHOT_BEAT_UNKNOWN')
    broken(lambda s: s['shots'][0].update(type='DUTCH'), 'CINEMATIC_CHOICE_INVALID')
    broken(lambda b: b['beats'][1].update(beat_id=b['beats'][0]['beat_id']), 'CINEMATIC_BEAT_ID_DUPLICATE', 'beats')
    broken(lambda b: b['beats'][1].update(frame_start=59), 'CINEMATIC_BEAT', 'beats')
    broken(lambda b: b['beats'][0].update(actors=['0' * 16]), 'CINEMATIC_BEAT_ACTOR_UNKNOWN', 'beats')
    broken(lambda b: b['timeline'].update(frame_end=999), 'CINEMATIC_BEATS_STALE', 'beats')
    with pytest.raises(ValueError, match='CINEMATIC_FPS_MISMATCH'):
        planning.normalize_plan(brief, beats, shots, dict(SCENE['settings'], fps=24))


def test_create_candidate_proposal_accepts_the_planned_set(world, monkeypatch):
    brief = brief_of(world)['brief']
    beats = call(world, 'cinematic_plan_beats', brief=brief)
    shots = call(world, 'cinematic_plan_shots', brief=brief, options={'beats': beats})
    # build_plan re-measures the registered actors (ACTOR_STALE, tested in test_m10_actors and the foundation probe)
    # through actors.DIGEST_BODY, which this planning FakeBridge does not answer: record the call instead.
    seen = []
    monkeypatch.setattr(lifecycle, 'stale_actors', lambda ctx, records, scene: seen.append(
        ([r['actor_id'] for r in records], scene)) or [])
    proposal = dispatch('cinematic_create_candidate', FakeBridge(), world['storage'], 'op-c', brief=brief,
                        options={'beats': beats, 'shots': shots})
    assert seen == [([world['creature'], world['hero']], 'Scene')]
    assert proposal['status'] == 'proposal' and len(proposal['plan']['shots']) == 6
    assert [a['actor_id'] for a in proposal['plan']['actors']] == [world['creature'], world['hero']]


# ---------------------------------------------------------------------------------------------------- actor paths

def test_paths_reuse_measured_clips_and_report_clearance(world):
    brief = brief_of(world)['brief']
    result = call(world, 'cinematic_plan_actor_paths', brief=brief)
    path = result['paths'][0]
    stop = math.hypot(0.8, 1.6) / 2 + math.hypot(0.5, 0.3) / 2 + planning.PROPOSALS['clearance_m']
    assert path['actor_id'] == world['creature'] and path['status'] == 'PLANNED'
    assert path['stop_distance_m'] == pytest.approx(stop, abs=1e-6)
    assert path['distance_m'] == pytest.approx(8.0 - stop, abs=1e-6)
    clip = path['clip']
    assert clip['action'] == 'Creature_Walk' and clip['scale'] == 1.0 and clip['retime'] is False
    assert clip['repeat'] == pytest.approx((8.0 - stop) / 1.5, abs=1e-6) and clip['start_frame'] == 151
    assert path['speed_m_s'] == pytest.approx(1.5) and path['arrival_s'] == pytest.approx(5.0 + (8.0 - stop) / 1.5)
    assert path['staging']['yaw_offset_deg'] == pytest.approx(0.0) and path['staging']['requires_rotation'] is False
    assert {w['code'] for w in path['warnings']} == {'ARRIVAL_AFTER_BEAT_END'}
    pair = result['clearance']['pairs'][0]
    assert pair['min_distance_m'] == pytest.approx(stop, abs=1e-3) and pair['passed'] is True
    assert result['no_locomotion_generated'] is True and result['threshold_source'] == 'PROPOSAL'


def test_paths_without_locomotion_or_with_another_speed_never_generate_motion(world):
    brief = brief_of(world)['brief']
    hero = call(world, 'cinematic_plan_actor_paths', brief=brief, options={'paths': [
        {'actor': 'hero', 'target': [3.0, 2.0, 0.0], 'departure_s': 1.0}]})['paths'][0]
    assert hero['status'] == 'ALTERNATIVE_REQUIRED' and hero['clip'] is None
    assert hero['alternative']['kind'] == 'STATIC_STAGING_WITH_CUT' and hero['alternative']['cut_frame_candidates'][0] == 61
    assert 'IN_PLACE' in hero['alternative']['detail']
    fast = call(world, 'cinematic_plan_actor_paths', brief=brief, options={'paths': [
        {'actor': 'creature', 'target': [0.0, 0.0, 0.0], 'departure_s': 0.0, 'arrival_s': 2.0}]})
    path = fast['paths'][0]
    assert path['status'] == 'TIMING_CONFLICT' and path['requested_arrival_s'] == 2.0 and fast['status'] == 'warning'
    assert path['arrival_s'] == pytest.approx(4.0) and path['clip']['scale'] == 1.0
    retimed = call(world, 'cinematic_plan_actor_paths', brief=brief, options={'allow_retime': True, 'paths': [
        {'actor': 'creature', 'target': [0.0, 0.0, 0.0], 'departure_s': 0.0, 'arrival_s': 2.0}]})['paths'][0]
    assert retimed['status'] == 'PLANNED' and retimed['clip']['scale'] == pytest.approx(0.5) and retimed['arrival_s'] == pytest.approx(2.0)
    missing = call(world, 'cinematic_plan_actor_paths', brief=brief, options={'paths': [
        {'actor': 'creature', 'target': 'Rock', 'action': 'Creature_Run'}]})
    assert missing['paths'][0]['status'] == 'MISSING_PREREQUISITE'
    assert missing['missing_prerequisites'][0]['code'] == 'ACTION_NOT_AVAILABLE'
    with pytest.raises(ValueError, match='CINEMATIC_ACTOR_UNKNOWN'):
        call(world, 'cinematic_plan_actor_paths', brief=brief, options={'paths': [{'actor': 'ghost', 'target': [0, 0, 0]}]})
    with pytest.raises(ValueError, match='CINEMATIC_PATHS_REQUIRED'):
        call(world, 'cinematic_plan_actor_paths')


def test_clearance_fails_when_straight_paths_cross(world):
    brief = brief_of(world)['brief']
    result = call(world, 'cinematic_plan_actor_paths', brief=brief, options={'paths': [
        {'actor': 'creature', 'target': [0.0, 6.0, 0.0], 'departure_s': 0.0}]})
    pair = result['clearance']['pairs'][0]
    assert pair['passed'] is False and pair['min_distance_m'] < 0.05 and result['status'] == 'warning'


# --------------------------------------------------------------------------------------------------- interactions

def test_interactions_from_the_brief_with_proposed_criteria_and_reach(world):
    brief = brief_of(world)['brief']
    result = call(world, 'cinematic_plan_interaction', brief=brief)
    rows = {r['event_id']: r for r in result['interactions']}
    assert set(rows) == {'approach', 'look', 'prepare'} and result['animation_resolved'] is False
    assert rows['look']['frame'] == 241 and rows['look']['success_criteria'][0]['threshold_source'] == 'PROPOSAL'
    assert rows['prepare']['time_s'] == 11.0 and rows['prepare']['provenance']['time_s'] == 'USER_REQUIREMENT'
    assert rows['look']['reachability'] == {'status': 'NOT_APPLICABLE'}
    reach = call(world, 'cinematic_plan_interaction', brief=brief, options={'interactions': [
        {'kind': 'REACH', 'actors': ['creature'], 'spatial_target': 'hero', 'time_s': 1.0},
        {'kind': 'TOUCH_SURFACE', 'actors': ['hero'], 'spatial_target': 'Rock', 'frame': 30}]})['interactions']
    assert reach[0]['reachability']['status'] == 'OUT_OF_REACH' and reach[0]['reachability']['reach_m'] == 1.2
    assert reach[1]['reachability']['status'] == 'UNKNOWN' and reach[1]['reachability']['reach_m'] == 'UNKNOWN'
    assert reach[1]['contact_required'] is True and reach[1]['provenance']['contact_required'] == 'INFERRED_DEFAULT'
    assert reach[1]['success_criteria'][0]['max'] == 0.005
    assert any(w['code'] == 'EFFECTOR_NOT_DECLARED' for w in reach[1]['warnings'])
    paths = call(world, 'cinematic_plan_actor_paths', brief=brief)
    near = call(world, 'cinematic_plan_interaction', brief=brief, options={'paths': paths, 'interactions': [
        {'kind': 'REACH', 'actors': ['creature'], 'spatial_target': 'hero', 'time_s': 12.0}]})['interactions'][0]
    assert near['reachability']['status'] == 'REACHABLE' and near['reachability']['position_source'] == 'actor paths'
    with pytest.raises(ValueError, match='CINEMATIC_INTERACTION_OUTSIDE_TIMELINE'):
        call(world, 'cinematic_plan_interaction', brief=brief, options={'interactions': [
            {'kind': 'GAZE', 'actors': ['creature'], 'frame': 999}]})


# ---------------------------------------------------------------------------------------------------- render plan

def test_render_plan_uses_only_measured_rates_and_requires_scope(world):
    brief = brief_of(world)['brief']
    preview = call(world, 'cinematic_render_plan', brief=brief)
    stage = preview['stages'][0]
    assert preview['rendered'] is False and len(preview['stages']) == 1
    assert stage['engine'] == 'BLENDER_WORKBENCH' and stage['resolution'] == [320, 180] and stage['frames'] == 450
    assert stage['estimate']['status'] == 'MEASURED_RATE'
    assert stage['estimate']['seconds'] == [pytest.approx(450 * 0.0185, abs=1e-3), pytest.approx(450 * 0.0281, abs=1e-3)]
    assert stage['chunking']['frames_per_call'] == int(120 // 0.0281)
    with pytest.raises(ValueError, match='CINEMATIC_FINAL_SCOPE_REQUIRED'):
        call(world, 'cinematic_render_plan', brief=brief, options={'quality_preset': 'DRAFT'})
    final = call(world, 'cinematic_render_plan', brief=brief, options={'quality_preset': 'PRODUCTION', 'frame_range': [241, 330]})
    assert [s['stage'] for s in final['stages']] == ['PREVIEW', 'FINAL']
    assert final['stages'][0]['frame_ranges'] == [[1, 450]] and final['stages'][1]['frame_ranges'] == [[241, 330]]
    assert final['stages'][1]['resolution'] == [1920, 1080] and final['stages'][1]['samples'] == 64
    assert final['stages'][1]['estimate']['status'] == 'NOT_MEASURED' and final['workload']['seconds'] == 'NOT_MEASURED'
    draft = call(world, 'cinematic_render_plan', brief=brief, options={'quality_preset': 'DRAFT', 'resolution': [320, 180],
                                                                       'frame_range': [1, 30]})
    assert draft['stages'][1]['estimate']['status'] == 'MEASURED_RATE' and draft['stages'][1]['samples'] == 16
    assert draft['provenance']['samples'] == 'PROPOSED_CREATIVE_DECISION'


def test_render_plan_cycles_requires_optix_and_safe_output(world):
    brief = brief_of(world)['brief']
    options = {'quality_preset': 'DRAFT', 'engine': 'CYCLES', 'frame_range': [1, 10]}
    with pytest.raises(ValueError, match='CINEMATIC_RENDER_OPTIX_REQUIRED'):
        call(world, 'cinematic_render_plan', brief=brief, options=options)
    plan = call(world, 'cinematic_render_plan', bridge=FakeBridge(backend=BACKEND_OPTIX), brief=brief, options=options)
    final = plan['stages'][1]
    assert final['blender_settings']['cycles'] == {'device': 'GPU', 'compute_device_type': 'OPTIX', 'samples': 64}
    assert final['estimate']['status'] == 'NOT_MEASURED' and final['backend'].startswith('GPU OPTIX')
    target = world['tmp'] / 'renders' / 'take1'
    ok = call(world, 'cinematic_render_plan', brief=brief, options={'output_path': str(target)})
    assert ok['output']['path'] == str(target) and not target.exists()
    for path, code in (('relative/out', 'CINEMATIC_OUTPUT_PATH_NOT_ABSOLUTE'), (str(world['tmp']), 'CINEMATIC_OUTPUT_EXISTS'),
                       (str(world['tmp'] / ('x' * 200)), 'CINEMATIC_OUTPUT_PATH_TOO_LONG')):
        with pytest.raises(ValueError, match=code):
            call(world, 'cinematic_render_plan', brief=brief, options={'output_path': path})
    for options, code in (({'fps': 24}, 'CINEMATIC_FPS_MISMATCH'), ({'format': 'MP4_H264'}, 'CINEMATIC_PREVIEW_FORMAT'),
                          ({'engine': 'BLENDER_EEVEE'}, 'CINEMATIC_RENDER_PRESET_INVALID'),
                          ({'frame_range': [0, 10]}, 'CINEMATIC_RANGE_INVALID')):
        with pytest.raises(ValueError, match=code):
            call(world, 'cinematic_render_plan', brief=brief, options=options)
    with pytest.raises(ValueError, match='CINEMATIC_RENDER_PLAN_SOURCE_REQUIRED'):
        call(world, 'cinematic_render_plan')


def test_render_plan_by_shots(world):
    brief = brief_of(world)['brief']
    shots = call(world, 'cinematic_plan_shots', brief=brief)
    plan = call(world, 'cinematic_render_plan', brief=brief, options={'shot_plan': shots, 'shots': ['S02', 'S04'],
                                                                      'quality_preset': 'DRAFT'})
    assert plan['stages'][1]['frame_ranges'] == [[61, 150], [241, 330]] and plan['stages'][1]['frames'] == 180


# --------------------------------------------------------------------------------------------- payloads and hints

@pytest.mark.parametrize('body, params', [
    (handlers.PRESENCE, {'source_scene': 'Scene', 'objects': ['O' * 63] * planning.MAX_PRESENCE,
                         'actions': ['A' * 63] * planning.MAX_PRESENCE}),
    (handlers.BACKEND, {}),
], ids=['presence', 'backend'])
def test_planning_payloads_pass_safe_mode_within_budget_and_change_nothing(body, params):
    code = build_code(body, params)
    assert payload_bytes(body, params) < 140_000
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 14_000
    linked = link_payload(body)
    assert not FRAME.search(linked) and not ASSIGN.search(linked) and not POSE.search(linked)
    for forbidden in ('.new(', '.remove(', 'frame_current =', 'keyframe_insert', 'animation_data_create'):
        assert forbidden not in linked.split('def cm_plan_')[-1]


def test_planning_tools_are_read_only_in_the_catalogue():
    from blender_mcp.production.cinematic.tools import READ_ONLY, TRANSIENT
    assert set(PLANNING_TOOLS) <= READ_ONLY and not set(PLANNING_TOOLS) & TRANSIENT
