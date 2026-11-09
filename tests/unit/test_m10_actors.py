"""M10 area B without Blender: scene/actor/integrity handlers, registry identity, payload budgets and read-only payloads.

A recording bridge validates every payload with the real safe-mode validator (build_code) and answers with canned
Blender results, so these tests prove the host logic (validation, idempotency, change detection, M7/M8 reference
checks, integrity comparison) and that only the expected payloads are ever sent.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import math
from pathlib import Path
import re

import pytest

from blender_mcp.production.animation.generation import store as gstore
from blender_mcp.production.cinematic import actors, registry, store
from blender_mcp.production.cinematic.service import HANDLERS, dispatch, load_handlers
from blender_mcp.production.common.asset_safety import CURRENT_FILE
from blender_mcp.production.common.bridge import build_code, payload_bytes
from blender_mcp.production.common.payload import link_payload
from blender_mcp.production.common.storage import Storage

FRAME = re.compile(r'frame_set\(|m3_set_frame\(|m2_set_frame\(')
ASSIGN = re.compile(r'animation_data\.action\s*=(?!=)|m2_assign\(|m3_assign\(|mv_assign\(|\.action_slot\s*=(?!=)')
POSE = re.compile(r'\.rotation_quaternion\s*=(?!=)|\.location\s*=(?!=)|pose_position\s*=(?!=)')
IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
SECTIONS = {'topology': 't1', 'positions': 'p1', 'uv': 'u1', 'shape_keys': None, 'material_slots': 'm1',
            'material_index': 'i1', 'vertex_groups': 'g1'}


def snapshot(positions='p1', action_digest='a1', facial=None, secondary=None, low=None):
    sections = dict(SECTIONS, positions=positions)
    return {'scene': 'Scene', 'fps': 30.0, 'unit_scale_length': 1.0, 'object': 'Rig', 'data': 'RigData',
            'type': 'ARMATURE', 'mode': 'OBJECT', 'parent': None, 'parent_type': 'OBJECT', 'parent_bone': None,
            'constraints': [], 'children': ['Body'], 'mesh_names': ['Body'],
            'meshes': [{'object': 'Body', 'data': 'BodyMesh', 'key': None, 'shape_key_names': [],
                        'counts': {'vertices': 8, 'edges': 12, 'polygons': 6, 'uv_layers': 1, 'shape_keys': 0,
                                   'material_slots': 1}}],
            'mesh_sections': {'Body': sections},
            'rig': {'digest': 'rig1', 'bones': 3, 'pose_position': 'POSE', 'pose_evaluated': True, 'drivers': 0,
                    'root_bones': ['root']},
            'asset_fingerprint': 'asset:' + positions,
            'bones': {'root': {'bone': 'root', 'source': 'HIERARCHY_SINGLE_ROOT', 'confidence': None},
                      'hand_left': {'bone': 'hand.L', 'source': 'M3_SEMANTICS', 'confidence': 0.93}},
            'low_confidence': low or {}, 'semantic_status': 'ANALYZED',
            'body_actions': [{'action': 'Walk', 'slot': 'OBRig', 'origin': 'ACTIVE', 'frame_range': [1.0, 60.0],
                              'digest': action_digest,
                              'root_motion': {'status': 'MEASURED', 'horizontal_distance_m': 3.0, 'dominant_axis': '+Y'}}],
            'active_action': 'Walk', 'active_slot': 'OBRig',
            'reach': {'hand_left': {'value_m': 0.7, 'chain': ['hand.L', 'forearm.L', 'upper_arm.L'],
                                    'source': 'M3_SEMANTICS'}},
            'bounds': {'min_m': [-0.3, -0.2, 0.0], 'max_m': [0.3, 0.2, 1.8], 'dimensions_m': [0.6, 0.4, 1.8],
                       'method': 'REST_DATA_AABB_WORLD'},
            'world_transform': {'location_m': [0.0, 0.0, 0.0], 'rotation_euler': [0.0, 0.0, 0.0], 'scale': [1.0, 1.0, 1.0],
                                'matrix_world': IDENTITY, 'delta_location': [0.0, 0.0, 0.0],
                                'delta_rotation_euler': [0.0, 0.0, 0.0]},
            'facial': facial or {}, 'secondary': secondary or {}, 'limitations': []}


def observed_from(snap, **changes):
    row = {'exists': True, 'type': snap['type'], 'data': snap['data'], 'mode': 'OBJECT', 'in_scene': True,
           'rig': snap['rig']['digest'], 'mesh_set': list(snap['mesh_names']), 'meshes': deepcopy(snap['mesh_sections']),
           'body_actions': {row['action']: {'digest': row['digest'], 'slot': True} for row in snap['body_actions']},
           'facial': deepcopy(snap['facial']), 'secondary': deepcopy(snap['secondary']), 'matrix_world': IDENTITY,
           'active_action': snap['active_action']}
    row.update(changes)
    return row


class FakeBridge:
    """Validates each payload with the real safe-mode validator and answers with canned Blender results."""

    def __init__(self, snap=None, digests=None, scene_result=None):
        self.snap = snap
        self.digests = digests
        self.scene_result = scene_result
        self.bodies = []
        self.params = []

    def run(self, body, params=None):
        build_code(body, params)
        self.bodies.append(body)
        self.params.append(deepcopy(params))
        if body == CURRENT_FILE:
            return {'filepath': '', 'is_dirty': False, 'stamps': []}
        if body is actors.REGISTER_BODY:
            return deepcopy(self.snap)
        if body is actors.DIGEST_BODY:
            return deepcopy(self.digests(params) if callable(self.digests) else self.digests)
        if body is actors.SCENE_NAME_BODY:
            return {'scene': 'Scene'}
        if body is actors.SCENE_BODY:
            return deepcopy(self.scene_result)
        raise AssertionError('unexpected payload sent to Blender')


class NoBridge:
    def run(self, body, params=None):
        raise AssertionError('validation must refuse before any Blender call')


def register(storage, bridge, **options):
    options.setdefault('role', 'PROTAGONIST')
    return dispatch('cinematic_register_actor', bridge, storage, 'op-reg', object_name='Rig', options=options)


def test_handlers_are_registered():
    load_handlers()
    assert {'cinematic_inspect_scene', 'cinematic_register_actor', 'cinematic_inspect_actor',
            'cinematic_verify_integrity'} <= set(HANDLERS)


def _spec(index):
    return {'actor_id': format(index, '016x'), 'object': 'Actor_' + str(index), 'meshes': ['Body_' + str(i) for i in range(32)],
            'body_actions': [['Action_' + str(i), 'OBActor'] for i in range(32)], 'facial_actions': ['M7F_x_v1'],
            'secondary': [{'key': 'secondary:0', 'names': ['M8_x_v1', 'M8_x_v1_Baked']}]}


@pytest.mark.parametrize('name,params', [
    ('SCENE_BODY', {'source_scene': 'Scene', 'limit': 200, 'offset': 0, 'detail': 'full',
                    'sections': list(actors.SCENE_SECTIONS), 'schema': actors.SCENE_SCHEMA}),
    ('REGISTER_BODY', {'object': 'Rig', 'source_scene': None, 'bones': {role: 'bone_' + role for role in actors.BONE_ROLES},
                       'body_actions': ['Action_' + str(i) for i in range(32)], 'semantic_roles': actors.SEMANTIC_ROLES,
                       'confidence_min': 0.9, 'facial_actions': ['M7F_' + str(i) for i in range(8)],
                       'secondary': [{'key': 'secondary:' + str(i), 'names': ['a', 'b']} for i in range(8)],
                       'max_meshes': 32, 'max_vertices': 2_000_000}),
    ('DIGEST_BODY', {'source_scene': 'Scene', 'scene': 'M10C_x_v1', 'generation_id': 'g' * 32, 'version': 1,
                     'collections': {'src': 'a', 'cin': 'b'}, 'scene_checks': True,
                     'actors': [_spec(i) for i in range(8)]}),
    ('SCENE_NAME_BODY', {}),
])
def test_payloads_pass_safe_mode_within_budget(name, params):
    body = getattr(actors, name)
    code = build_code(body, params)
    assert payload_bytes(body, params) < 140_000
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 14_000


@pytest.mark.parametrize('name', ['SCENE_BODY', 'DIGEST_BODY', 'REGISTER_BODY', 'SCENE_NAME_BODY'])
def test_payloads_never_change_frame_action_or_pose(name):
    # The three READ_ONLY tools must not, and registration promises it never touches Blender either.
    linked = link_payload(getattr(actors, name))
    assert not FRAME.search(linked) and not ASSIGN.search(linked) and not POSE.search(linked)
    for forbidden in ('lambda ', '__class__', 'bpy_extras', 'import numpy', 'hashlib', 'import os', 'import time'):
        assert forbidden not in linked


@pytest.mark.parametrize('tool,kwargs,code', [
    ('cinematic_inspect_scene', {'object_name': 'Rig'}, 'CINEMATIC_ARGUMENT_UNSUPPORTED'),
    ('cinematic_inspect_scene', {'options': {'bogus': 1}}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ('cinematic_inspect_scene', {'options': {'limit': 0}}, 'CINEMATIC_RANGE_INVALID'),
    ('cinematic_inspect_scene', {'options': {'limit': 2.5}}, 'CINEMATIC_INTEGER_REQUIRED'),
    ('cinematic_inspect_scene', {'options': {'detail': 'everything'}}, 'CINEMATIC_CHOICE_INVALID'),
    ('cinematic_inspect_scene', {'options': {'sections': ['cameras', 'graph']}}, 'CINEMATIC_CHOICE_INVALID'),
    ('cinematic_inspect_scene', {'apply': True}, 'CINEMATIC_APPLY_UNSUPPORTED'),
    ('cinematic_register_actor', {'options': {'role': 'PROTAGONIST'}}, 'ACTOR_OBJECT_REQUIRED'),
    ('cinematic_register_actor', {'object_name': 'Rig'}, 'ACTOR_ROLE_REQUIRED'),
    ('cinematic_register_actor', {'object_name': 'Rig', 'options': {'role': 'HERO'}}, 'CINEMATIC_CHOICE_INVALID'),
    ('cinematic_register_actor', {'object_name': 'Rig', 'options': {'role': 'EXTRA', 'mood': 1}}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ('cinematic_register_actor', {'object_name': 'Rig', 'options': {'role': 'EXTRA', 'bones': {'tail': 'b'}}},
     'ACTOR_BONE_ROLE_UNSUPPORTED'),
    ('cinematic_register_actor', {'object_name': 'Rig', 'options': {'role': 'EXTRA', 'body_actions': ['A', 'A']}},
     'CINEMATIC_LIST_INVALID'),
    ('cinematic_register_actor', {'object_name': 'X' * 64, 'options': {'role': 'EXTRA'}}, 'CINEMATIC_NAME_INVALID'),
    ('cinematic_register_actor', {'object_name': 'Rig', 'cinematic': 'p', 'options': {'role': 'EXTRA'}},
     'CINEMATIC_ARGUMENT_UNSUPPORTED'),
    ('cinematic_inspect_actor', {}, 'ACTOR_ID_INVALID'),
    ('cinematic_inspect_actor', {'actor': 'Rig'}, 'ACTOR_ID_INVALID'),
    ('cinematic_inspect_actor', {'actor': '0' * 16, 'options': {'limit': 1}}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ('cinematic_verify_integrity', {}, 'CINEMATIC_REFERENCE_REQUIRED'),
    ('cinematic_verify_integrity', {'cinematic': 'x', 'options': {'fix': True}}, 'CINEMATIC_OPTION_UNSUPPORTED'),
    ('cinematic_verify_integrity', {'cinematic': 'x', 'apply': True}, 'CINEMATIC_APPLY_UNSUPPORTED'),
])
def test_validation_refuses_before_any_blender_call(tmp_path, tool, kwargs, code):
    with pytest.raises(ValueError, match=code):
        dispatch(tool, NoBridge(), Storage(tmp_path), 'op', **kwargs)


def test_registration_is_idempotent_and_records_the_contract(tmp_path):
    storage = Storage(tmp_path)
    bridge = FakeBridge(snapshot())
    first = register(storage, bridge)
    again = register(storage, bridge)
    key = first['asset_key']
    assert first['created'] is True and again['created'] is False and again['idempotent'] is True
    assert first['actor_id'] == again['actor_id'] and re.match(r'^[0-9a-f]{16}$', first['actor_id'])
    assert len(registry.records(storage, key)) == 1 and key.startswith('TEMPORARY-')
    record = registry.find(storage, key, first['actor_id'])
    for field in ('actor_id', 'object', 'data', 'type', 'role', 'asset_role', 'meshes', 'body_actions', 'active_action',
                  'facial', 'secondary', 'world_transform', 'dimensions_m', 'interaction_capabilities', 'fingerprint',
                  'limitations', 'integrity'):
        assert field in record, field
    assert record['meshes'] == ['Body'] and record['type'] == 'ARMATURE' and record['asset_role'] == 'TEMPORARY'
    assert record['body_actions'][0]['root_motion']['horizontal_distance_m'] == 3.0
    caps = record['interaction_capabilities']
    assert caps['reach_m'] == 0.7 and caps['bones']['head'] == 'UNKNOWN' and caps['threshold_source'] == 'PROPOSAL'
    assert caps['bones']['hand_left']['source'] == 'M3_SEMANTICS' and caps['can_hold_props'] is True
    assert record['integrity']['meshes']['Body']['positions'] == 'p1'
    # Registration never sends anything but the asset probe and the read-only snapshot.
    assert set(map(id, bridge.bodies)) <= {id(CURRENT_FILE), id(actors.REGISTER_BODY)}
    assert 'integrity' not in first['actor'] and first['blender_changed'] is False


def test_changed_fingerprint_writes_a_new_record_with_warning(tmp_path):
    storage = Storage(tmp_path)
    first = register(storage, FakeBridge(snapshot()))
    moved = register(storage, FakeBridge(snapshot(positions='p2')))
    assert moved['created'] is True and moved['actor_id'] != first['actor_id']
    warning = next(w for w in moved['warnings'] if w['code'] == 'ACTOR_FINGERPRINT_CHANGED')
    assert warning['previous_actor_id'] == first['actor_id']
    assert 'asset' in warning['changed'] and 'meshes:Body:positions' in warning['changed']
    assert 'rig' not in warning['changed'] and 'role' not in warning['changed']
    recast = register(storage, FakeBridge(snapshot()), role='ANTAGONIST')
    assert recast['created'] is True and 'role' in next(w for w in recast['warnings']
                                                        if w['code'] == 'ACTOR_FINGERPRINT_CHANGED')['changed']
    back = register(storage, FakeBridge(snapshot()))
    assert back['actor_id'] == first['actor_id'] and back['created'] is False
    assert len(registry.records(storage, first['asset_key'])) == 3
    # The previous records are immutable: the first one still describes the first measurement.
    assert registry.find(storage, first['asset_key'], first['actor_id'])['integrity']['meshes']['Body']['positions'] == 'p1'


def test_low_confidence_semantics_is_reported_not_used(tmp_path):
    result = register(Storage(tmp_path), FakeBridge(snapshot(low={'head': {'bone': 'head', 'confidence': 0.88}})))
    warning = next(w for w in result['warnings'] if w['code'] == 'ACTOR_SEMANTICS_LOW_CONFIDENCE')
    assert warning['role'] == 'head' and warning['confidence'] == 0.88
    assert result['actor']['interaction_capabilities']['bones']['head'] == 'UNKNOWN'


def _facial_ledger(storage, armature, obj, state='ACCEPTED', fingerprint='fa1'):
    ref, _ = gstore.save(storage, 'op-face-' + obj, {'object_name': obj, 'profile': {'armature': armature},
                                                    'brief': {'fps': 30, 'frame_start': 1, 'frame_end': 60},
                                                    'head_policy': 'BODY_OWNS'}, 'facial', gstore.FACIAL_STATES)
    gstore.event(Path(ref).parent, {'state': state, 'refinement_cycles': 0, 'candidate_action': 'M7F_face_v1',
                                    'candidate_fingerprint': fingerprint,
                                    'accepted_tracks': [[armature, 'M7_FACE M7F_face_v1']]}, gstore.FACIAL_STATES)
    return ref


def test_facial_reference_is_validated_against_the_actor(tmp_path):
    storage = Storage(tmp_path)
    facial = {'M7F_face_v1': {'exists': True, 'digest': 'fa1', 'keys': ['Key'], 'owner': 'M7', 'lifecycle': 'ACCEPTED',
                              'm7_tracks': [['Rig', 'M7_FACE M7F_face_v1', False]]}}
    ref = _facial_ledger(storage, 'Rig', 'Face')
    result = register(storage, FakeBridge(snapshot(facial=facial)), facial=[ref])
    row = result['actor']['facial'][0]
    assert row['state'] == 'ACCEPTED' and row['fingerprint_matches_ledger'] is True and row['m7_tracks']
    assert result['actor']['interaction_capabilities']['facial_performance'] is True
    other = _facial_ledger(storage, 'OtherRig', 'OtherFace')
    with pytest.raises(ValueError, match='ACTOR_FACIAL_OBJECT_MISMATCH'):
        register(storage, FakeBridge(snapshot(facial=facial)), facial=[other])
    changed = _facial_ledger(storage, 'Rig', 'Body', state='FACIAL_CANDIDATE', fingerprint='other')
    result = register(storage, FakeBridge(snapshot(facial=facial)), facial=[changed])
    codes = {w['code'] for w in result['warnings']}
    assert {'FACIAL_NOT_ACCEPTED', 'FACIAL_ACTION_CHANGED'} <= codes
    with pytest.raises(ValueError, match='ACTOR_FACIAL_REFERENCE_INVALID'):
        register(storage, FakeBridge(snapshot()), facial=[str(tmp_path / 'nowhere' / 'plan.json')])


def _physics_ledger(storage, source, primary):
    from blender_mcp.production.physics.service import STATES
    ref, _ = gstore.save(storage, 'op-m8-' + source, {'object_name': source,
                                                      'params': {'source_object': source, 'primary_objects': primary},
                                                      'brief': {'subsystem': 'CLOTH', 'fps': 30}}, 'physics', STATES)
    gstore.event(Path(ref).parent, {'state': 'ACCEPTED', 'refinement_cycles': 0,
                                    'candidate': {'candidate': 'M8_ab_v1', 'candidate_object': 'M8_ab_v1'},
                                    'baked_candidate': {'candidate_object': 'M8_ab_v1_Baked'},
                                    'cache': {'representation': 'PC2', 'sha256': 'c' * 64},
                                    'bake_deviation': {'passed': True}}, STATES)
    return ref


def test_secondary_reference_is_validated_against_the_actor(tmp_path):
    storage = Storage(tmp_path)
    ref = _physics_ledger(storage, 'Cape', ['Rig'])
    secondary = {'secondary:0': {'digest': 's1', 'missing': []}}
    result = register(storage, FakeBridge(snapshot(secondary=secondary)), secondary=[ref])
    row = result['actor']['secondary'][0]
    assert row['state'] == 'ACCEPTED' and row['objects'] == ['M8_ab_v1', 'M8_ab_v1_Baked'] and row['objects_digest'] == 's1'
    other = _physics_ledger(storage, 'OtherCape', ['OtherRig'])
    with pytest.raises(ValueError, match='ACTOR_SECONDARY_OBJECT_MISMATCH'):
        register(storage, FakeBridge(snapshot(secondary=secondary)), secondary=[other])


def test_motion_summary_is_pure_and_threshold_free():
    summary = actors.cax_motion_summary([[0.0, 0.0, 0.0], [-1.0, 0.0, 0.1], [-2.0, 0.5, 0.0]], [1, 2, 3], 30.0)
    assert summary['horizontal_displacement_m'] == [-2.0, 0.5] and summary['dominant_axis'] == '-X'
    assert math.isclose(summary['path_length_m'], 1.0 + math.hypot(1.0, 0.5))
    assert math.isclose(summary['max_horizontal_excursion_m'], math.hypot(2.0, 0.5))
    assert summary['vertical_offset_range_m'] == [0.0, 0.1] and math.isclose(summary['duration_s'], 2 / 30)
    still = actors.cax_motion_summary([[1.0, 1.0, 0.0], [1.0, 1.0, 0.0]], [1, 2], 24.0)
    assert still['dominant_axis'] == 'NONE' and still['horizontal_distance_m'] == 0.0
    assert actors.cax_motion_summary([[0, 0, 0], [1.0, -1.0, 0]], [1, 2], 30.0)['dominant_axis'] == 'TIE_XY'


def _record(tmp_path):
    storage = Storage(tmp_path)
    result = register(storage, FakeBridge(snapshot()))
    return storage, result['asset_key'], registry.find(storage, result['asset_key'], result['actor_id'])


def test_compare_actor_names_each_changed_section(tmp_path):
    _, _, record = _record(tmp_path)
    snap = snapshot()
    checked, changed, info = actors.compare_actor(record, observed_from(snap))
    assert not changed and {row['status'] for row in checked} == {'UNCHANGED'} and info['moved'] is False
    meshes = deepcopy(snap['mesh_sections'])
    meshes['Body']['uv'] = 'u2'
    meshes['Body']['shape_keys'] = 'k2'
    _, changed, _ = actors.compare_actor(record, observed_from(snap, rig='rig2', meshes=meshes,
                                                               body_actions={'Walk': {'digest': 'a2', 'slot': True}}))
    found = {(row['code'], row.get('section')) for row in changed}
    assert found == {('RIG_CHANGED', None), ('MESH_CHANGED', 'uv'), ('MESH_CHANGED', 'shape_keys'),
                     ('BODY_ACTION_CHANGED', None)}
    _, changed, _ = actors.compare_actor(record, observed_from(snap, mesh_set=['Body', 'Sword'], body_actions={}))
    assert {row['code'] for row in changed} == {'ACTOR_MESH_SET_CHANGED', 'BODY_ACTION_MISSING'}
    _, changed, _ = actors.compare_actor(record, {'exists': False})
    assert [row['code'] for row in changed] == ['ACTOR_OBJECT_MISSING']
    moved = [list(row) for row in IDENTITY]
    moved[0][3] = 2.0
    _, changed, info = actors.compare_actor(record, observed_from(snap, matrix_world=moved))
    assert not changed and info['moved'] is True and info['world_matrix_max_abs_change'] == 2.0


def _plan(record, key, fingerprint='src0'):
    return {'generation_id': 'c' * 32, 'actor_registry_key': key, 'source': {'scene': 'Scene', 'fingerprint': fingerprint},
            'actors': [{'actor_id': record['actor_id'], 'object': record['object'], 'fingerprint': record['fingerprint']}]}


def _digests(record, source='src1', candidate='cand1', **changes):
    def answer(params):
        return {'source_scene_exists': True, 'source': source, 'candidate': candidate if params.get('scene') else None,
                'candidate_status': 'MEASURED' if params.get('scene') else 'NOT_APPLICABLE',
                'cameras_lights': {'digest': 'cl', 'count': 2, 'objects': {'Cam': 'c1', 'Key': 'l1'}, 'scene_camera': 'Cam'},
                'actors': {record['actor_id']: observed_from(snapshot(), **changes)}}
    return answer


def test_integrity_report_passes_and_detects_real_changes(tmp_path):
    storage, key, record = _record(tmp_path)
    plan = _plan(record, key)
    state = {'state': 'CINEMATIC_CANDIDATE', 'fingerprints': {'source': 'src1', 'candidate': 'cand1'},
             'assembly': {'scene': 'M10C_cccccccc_v1', 'version': 1, 'collections': {'src': 'S', 'cin': 'C'}}}

    class Ctx:
        pass
    ctx = Ctx()
    ctx.storage = storage
    ctx.bridge = FakeBridge(digests=_digests(record))
    report = actors.integrity_report(ctx, plan, state)
    assert report['passed'] is True and report['source']['basis'] == 'ledger'
    assert report['candidate']['status'] == 'UNCHANGED' and report['cameras_lights']['basis'] == 'covered_by_source_fingerprint'
    assert ctx.bridge.params[-1]['scene'] == 'M10C_cccccccc_v1' and ctx.bridge.params[-1]['scene_checks'] is True
    meshes = deepcopy(snapshot()['mesh_sections'])
    meshes['Body']['positions'] = 'p9'
    ctx.bridge = FakeBridge(digests=_digests(record, meshes=meshes))
    report = actors.integrity_report(ctx, plan, state)
    assert report['passed'] is False
    assert [(row['item'], row['code'], row['section']) for row in report['changed']] == [('mesh:Body', 'MESH_CHANGED', 'positions')]
    ctx.bridge = FakeBridge(digests=_digests(record, source='src2'))
    report = actors.integrity_report(ctx, plan, state)
    assert [row['code'] for row in report['changed']] == ['SOURCE_SCENE_CHANGED']
    assert report['cameras_lights']['status'] == 'UNKNOWN'
    rejected = dict(state, state='REJECTED')
    ctx.bridge = FakeBridge(digests=_digests(record, source='src0'))
    report = actors.integrity_report(ctx, plan, rejected)
    assert report['passed'] is True and report['source']['basis'] == 'plan'
    assert report['candidate']['status'] == 'NOT_APPLICABLE' and ctx.bridge.params[-1]['scene'] is None
    planned = {'state': 'PLANNED'}
    ctx.bridge = FakeBridge(digests=_digests(record, source='src0'))
    assert actors.integrity_report(ctx, plan, planned)['passed'] is True


def test_integrity_report_flags_missing_record_and_candidate(tmp_path):
    storage, key, record = _record(tmp_path)
    plan = _plan(record, key)
    plan['actors'].append({'actor_id': 'f' * 16, 'object': 'Ghost', 'fingerprint': 'x'})
    state = {'state': 'CINEMATIC_CANDIDATE', 'fingerprints': {'source': 'src1', 'candidate': 'cand1'},
             'assembly': {'scene': 'M10C_cccccccc_v1', 'version': 1, 'collections': {'src': 'S', 'cin': 'C'}}}

    class Ctx:
        pass
    ctx = Ctx()
    ctx.storage = storage
    ctx.bridge = FakeBridge(digests=_digests(record, candidate='edited'))
    report = actors.integrity_report(ctx, plan, state)
    assert {row['code'] for row in report['changed']} == {'ACTOR_RECORD_MISSING', 'CANDIDATE_SCENE_CHANGED'}


def test_inspect_actor_reports_fresh_then_stale(tmp_path):
    storage, _, record = _record(tmp_path)
    fresh = dispatch('cinematic_inspect_actor', FakeBridge(digests=_digests(record)), storage, 'op', actor=record['actor_id'])
    assert fresh['freshness'] == 'fresh' and fresh['code'] is None and fresh['status'] == 'success'
    assert 'integrity' not in fresh['actor']
    meshes = deepcopy(snapshot()['mesh_sections'])
    meshes['Body']['material_index'] = 'i2'
    bridge = FakeBridge(digests=_digests(record, meshes=meshes))
    stale = dispatch('cinematic_inspect_actor', bridge, storage, 'op', actor=record['actor_id'], options={'detail': 'full'})
    assert stale['freshness'] == 'stale' and stale['code'] == 'ACTOR_STALE' and stale['status'] == 'warning'
    assert stale['changed'][0]['section'] == 'material_index' and stale['warnings'][0]['code'] == 'ACTOR_STALE'
    assert stale['actor']['integrity']['meshes']['Body']['positions'] == 'p1'
    assert bridge.params[-1]['scene_checks'] is False
    with pytest.raises(ValueError, match='ACTOR_MISSING'):
        dispatch('cinematic_inspect_actor', FakeBridge(digests=_digests(record)), storage, 'op', actor='e' * 16)


def test_verify_integrity_handler_reads_the_ledger(tmp_path):
    storage, key, record = _record(tmp_path)
    reference, _ = store.save(storage, 'op-plan', dict(_plan(record, key, 'src1'), shots=[], timeline={'frame_start': 1}))
    result = dispatch('cinematic_verify_integrity', FakeBridge(digests=_digests(record)), storage, 'op', cinematic=reference)
    assert result['passed'] is True and result['state'] == 'PLANNED' and result['read_only'] is True
    assert result['checked'] == [] and result['checked_total'] >= 6
    full = dispatch('cinematic_verify_integrity', FakeBridge(digests=_digests(record)), storage, 'op', cinematic=reference,
                    options={'detail': 'full'})
    assert len(full['checked']) == full['checked_total']
    broken = dispatch('cinematic_verify_integrity', FakeBridge(digests=_digests(record, rig='rig9')), storage, 'op',
                      cinematic=reference)
    assert broken['passed'] is False and broken['status'] == 'warning' and broken['changed'][0]['code'] == 'RIG_CHANGED'
    assert broken['warnings'][0]['code'] == 'CINEMATIC_INTEGRITY_CHANGED'


def test_inspect_scene_passes_validated_options(tmp_path):
    bridge = FakeBridge(scene_result={'schema': actors.SCENE_SCHEMA, 'scene': {'name': 'Scene'}, 'sections': {},
                                      'pagination': {'limit': 5, 'offset': 10, 'totals': {}, 'more': []}})
    result = dispatch('cinematic_inspect_scene', bridge, Storage(tmp_path), 'op',
                      options={'limit': 5, 'offset': 10, 'sections': ['cameras', 'props'], 'detail': 'full'})
    assert result['status'] == 'success' and result['read_only'] is True
    assert bridge.params[-1] == {'source_scene': None, 'limit': 5, 'offset': 10, 'detail': 'full',
                                 'sections': ['cameras', 'props'], 'schema': actors.SCENE_SCHEMA}


def test_public_record_hides_digests_unless_full(tmp_path):
    _, _, record = _record(tmp_path)
    summary = actors.public_record(record)
    assert 'integrity' not in summary and 'identity_sections' not in summary
    assert summary['mesh_details'] == [{'object': 'Body', 'counts': record['mesh_details'][0]['counts']}]
    # Two digests whose residues share a long prefix (what a small edit produces) must still get different labels.
    first = actors.short_digest('m521:1f:' + 'a' * 130 + '1:' + 'b' * 150)
    second = actors.short_digest('m521:1f:' + 'a' * 130 + '2:' + 'b' * 150)
    assert first.startswith('m521:1f#') and len(first) == len('m521:1f#') + 16 and first != second
