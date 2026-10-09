"""M4 character modeling & redesign without Blender: payload budgets, validation, refusals and
host-side arithmetic. Real geometry, edits, renders and demos: tests/blender/m4_character_probe.py.
"""
import ast
import threading

import pytest

from blender_mcp.production.common.asset_safety import CURRENT_FILE, AssetRegistry
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.character import edit, render, runtime, service
from blender_mcp.production.character.transaction import resolve_regions, validate_operation, validate_options
from blender_mcp.production.mesh.service import compare_region, validate_regions

TEMPLATES = [(runtime, n) for n in ('INSPECT', 'SILHOUETTE', 'SURFACE', 'ASYMMETRY', 'INTERSECTIONS', 'REGIONS', 'BONES')] + \
            [(edit, n) for n in ('READ', 'PREFLIGHT', 'EXECUTE', 'CHECK', 'RECONCILE')] + [(render, 'RENDER'), (service, 'PLAN_ANALYZE')]


@pytest.mark.parametrize('module, name', TEMPLATES, ids=[n for _, n in TEMPLATES])
def test_character_payloads_pass_safe_mode_within_budgets(module, name):
    code = build_code(getattr(module, name), {})
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 18000
    assert len(code.encode('utf-8')) < 60000


# ------------------------------------------------------------------ operation schema

def test_operation_defaults_are_explicit():
    op = validate_operation({'type': 'inflate', 'region': 'SHOULDERS', 'strength': 0.01})
    assert op == {'type': 'inflate', 'region': 'SHOULDERS', 'space': 'object', 'preserve_boundary': True,
                  'falloff': {'type': 'smooth', 'width_m': 0.05}, 'strength': 0.01}
    scale = validate_operation({'type': 'scale', 'region': 'S', 'factors': [1.04, 1, 1]})
    assert scale['factors'] == [1.04, 1.0, 1.0] and scale['pivot'] == 'region_centroid'
    smooth = validate_operation({'type': 'smooth', 'region': 'S'})
    assert smooth['strength'] == 0.5 and smooth['iterations'] == 3


@pytest.mark.parametrize('operation', [
    {'type': 'remesh', 'region': 'S'},
    {'type': 'inflate', 'region': 'S', 'strength': 0.6},
    {'type': 'inflate', 'region': 'S', 'strength': 0.01, 'vertices': [1, 2]},
    {'type': 'inflate', 'strength': 0.01},
    {'type': 'translate', 'region': 'S', 'strength': 0.01, 'direction': [0, 0, 0]},
    {'type': 'translate', 'region': 'S', 'strength': 0.01},
    {'type': 'grab', 'region': 'S', 'strength': 0.01, 'direction': [0, 0, 1]},
    {'type': 'scale', 'region': 'S', 'factors': [3, 1, 1]},
    {'type': 'smooth', 'region': 'S', 'iterations': 500},
    {'type': 'inflate', 'region': 'S', 'strength': 0.01, 'falloff': {'type': 'gaussian'}},
    {'type': 'inflate', 'region': 'S', 'strength': 0.01, 'space': 'screen'},
    {'type': 'inflate', 'region': 'S', 'strength': float('nan')},
])
def test_invalid_operations_are_refused(operation):
    with pytest.raises(ValueError):
        validate_operation(operation)


def test_options_require_manifest_and_known_shape_key_policy():
    with pytest.raises(ValueError, match='regions_manifest'):
        validate_options({'operation': {'type': 'inflate', 'region': 'S', 'strength': 0.01}})
    with pytest.raises(ValueError, match='shape_keys'):
        validate_options({'regions_manifest': 'm', 'operation': {'type': 'inflate', 'region': 'S', 'strength': 0.01},
                          'shape_keys': 'only_basis'})


# ------------------------------------------------------------------ regions

def manifest(**roles):
    rows = []
    for name, (role, source, confidence) in roles.items():
        rows.append({'name': name, 'role': role, 'source': source, 'confidence': confidence, 'ambiguous': confidence < 0.6,
                     'definition': {'method': 'vertex_group', 'name': name}})
    return {'regions': rows}


@pytest.mark.parametrize('target, code', [
    ('FACE', 'LOCKED_REGION_EDIT'), ('NECK', 'REGION_NOT_EDITABLE'), ('TORSO', 'REGION_AMBIGUOUS'), ('TAIL', 'REGION_UNDEFINED')])
def test_region_resolution_refusals(target, code):
    data = manifest(FACE=('locked', 'user', 1.0), NECK=('unassigned', 'automatic', 0.75), TORSO=('editable', 'automatic', 0.3),
                    SHOULDERS=('editable', 'user', 1.0))
    with pytest.raises(ValueError, match=code):
        resolve_regions(data, {'region': target})


def test_region_resolution_returns_every_lock():
    data = manifest(FACE=('locked', 'user', 1.0), HANDS=('locked', 'automatic', 0.9), SHOULDERS=('editable', 'automatic', 0.9))
    editable, locked = resolve_regions(data, {'region': 'SHOULDERS'})
    assert editable == {'method': 'vertex_group', 'name': 'SHOULDERS'} and [d['name'] for d in locked] == ['FACE', 'HANDS']


def test_unassigned_role_is_valid_and_never_fails_verification():
    assert validate_regions([{'name': 'NECK', 'role': 'unassigned', 'definition': {'method': 'vertex_group', 'name': 'neck'}}])
    stored = {'topology_fingerprint': 'T', 'indices': [1], 'indices_digest': 'I', 'positions': [0, 0, 0], 'shape_keys': {},
              'weights_digest': 'W', 'matrix_world': [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], 'unit_scale': 1.0}
    moved = dict(stored, positions=[0, 0, 1])
    row = compare_region({'name': 'NECK', 'role': 'unassigned', 'tolerance_m': 1e-5, 'capture': stored}, moved)
    assert row['passed'] and row['changed']


@pytest.mark.parametrize('key, code', [('faces_digest', 'REGION_FACE_MEMBERSHIP_CHANGED'),
                                       ('materials_digest', 'LOCKED_MATERIAL_CHANGED'), ('uv_digests', 'LOCKED_UV_CHANGED')])
def test_m4_region_captures_detect_face_material_uv_changes(key, code):
    stored = {'topology_fingerprint': 'T', 'indices': [1], 'indices_digest': 'I', 'positions': [0, 0, 0], 'shape_keys': {},
              'weights_digest': 'W', 'matrix_world': [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], 'unit_scale': 1.0,
              'faces_digest': 'F', 'face_count': 1, 'materials_digest': 'M', 'uv_digests': {'UVMap': 'U'}}
    current = dict(stored, **{key: {'UVMap': 'U2'} if key == 'uv_digests' else 'X'})
    row = compare_region({'name': 'FACE', 'role': 'locked', 'tolerance_m': 1e-5, 'capture': stored}, current)
    assert not row['passed'] and any(f.startswith(code) for f in row['failures'])


def test_user_region_rows_are_validated():
    with pytest.raises(ValueError):
        service._validate_region_rows([{'region_id': 'A', 'definition': {}, 'role': 'frozen'}])
    with pytest.raises(ValueError):
        service._validate_region_rows([{'region_id': '', 'definition': {}}])


# ------------------------------------------------------------------ intent, proportions, asymmetry

NAMES = ['FACE', 'HEAD', 'SHOULDERS', 'UPPER_BACK', 'HANDS', 'TORSO', 'LEFT_ARM', 'RIGHT_ARM', 'MUTATED_ARM', 'HUMP']


def test_intent_rules_read_change_before_preserve_clause():
    matched, preserve = service.parse_intent('Increase upper-body mass and improve shoulder/back transition while preserving '
                                             'the face and rig dependencies', NAMES)
    assert [m['goal'] for m in matched] == ['more upper-back volume', 'wider shoulder projection', 'softer shoulder/back transition']
    assert preserve == ['FACE']
    matched, preserve = service.parse_intent('keep the heavy hump', NAMES)
    assert matched == [] and preserve == ['HUMP']


def test_shoulder_widening_is_a_scale_not_an_inflate():
    rule = next(step for tokens, steps in service.INTENT_RULES for step in steps if step['goal'] == 'wider shoulder projection')
    assert rule['operation'] == 'scale' and rule['factors'][0] > 1


def test_limb_lengths_skip_horizontal_connectors():
    bones = {'hip.L': {'length_m': 0.1, 'region': 'LEG', 'side': 'LEFT', 'vertical_fraction': 0.49},
             'thigh.L': {'length_m': 0.4, 'region': 'LEG', 'side': 'LEFT', 'vertical_fraction': 1.0},
             'upper_arm.L': {'length_m': 0.26, 'region': 'ARM', 'side': 'LEFT', 'vertical_fraction': 0.0},
             'hand.L': {'length_m': 0.1, 'region': 'HAND', 'side': 'LEFT', 'vertical_fraction': 0.0},
             'spine': {'length_m': 0.2, 'region': 'TORSO', 'side': None, 'vertical_fraction': 1.0}}
    assert service.limb_lengths(bones) == {'leg_LEFT': 0.4, 'arm_LEFT': pytest.approx(0.36)}


def test_asymmetry_classification_uses_declared_thresholds():
    data = {'mirror': {'symmetric_fraction': 0.95},
            'pairs': [{'left': 'LEFT_ARM', 'right': 'RIGHT_ARM', 'log_deviation': 0.5},
                      {'left': 'LEFT_LEG', 'right': 'RIGHT_LEG', 'log_deviation': 0.05},
                      {'left': 'LEFT_HAND', 'right': 'RIGHT_HAND', 'log_deviation': 0.001},
                      {'left': 'LEFT_FOOT', 'right': 'RIGHT_FOOT', 'log_deviation': None},
                      {'left': 'LEFT_WING', 'right': 'RIGHT_WING', 'log_deviation': 0.05}],
            'regions': {'HUMP': {'mirror_mismatch_fraction': 0.9}, 'TORSO': {'mirror_mismatch_fraction': 0.1}}}
    pairs, features = service.classify_asymmetry(data, {'LEFT_WING'})
    assert [p['classification'] for p in pairs] == ['likely_intentional', 'likely_accidental', 'symmetric', 'unknown', 'intentional']
    assert features == [{'region': 'HUMP', 'mirror_mismatch_fraction': 0.9, 'classification': 'likely_intentional'}]
    data['mirror']['symmetric_fraction'] = 0.3  # an asymmetric design: small deviations are not called accidental
    assert service.classify_asymmetry(data, set())[0][1]['classification'] == 'unknown'


def test_named_measurements_come_from_regions_or_stay_absent():
    view = {'view': 'FRONT', 'left_area_fraction': 0.5, 'negative_space': {'holes': 0, 'hole_area_m2': 0.0},
            'extremity_reach_m': 0.8, 'waist_width_m': 0.15,
            'regions': {'HEAD': {'u_min': -0.1, 'u_max': 0.1, 'v_min': 1.6, 'v_max': 1.83},
                        'SHOULDERS': {'u_min': -0.32, 'u_max': 0.32, 'v_min': 1.36, 'v_max': 1.56}}}
    named = service.named_measurements({'views': [view], 'world_bounds': {'size': [1.6, 0.25, 1.83]}})
    assert named['shoulder_width_m'] == pytest.approx(0.64) and named['head_height_m'] == pytest.approx(0.23)
    assert named['pelvis_width_m'] is None and named['torso_width_m'] is None


# ------------------------------------------------------------------ MASTER refusal before any Blender work

class RecordingBridge:
    def __init__(self, path):
        self.path = path
        self.bodies = []
        self.lock = threading.RLock()

    def run(self, body, params=None):
        self.bodies.append(body)
        if body == CURRENT_FILE:
            return {'filepath': self.path, 'is_dirty': False, 'stamps': []}
        raise AssertionError('no Blender payload may run after a MASTER refusal')


@pytest.fixture
def master(tmp_path):
    storage = Storage(tmp_path / 'work')
    path = str(tmp_path / 'Hero.blend')
    AssetRegistry(storage).register('MASTER', path)
    return RecordingBridge(path), storage


def test_create_candidate_refuses_master_before_any_payload(master):
    bridge, storage = master
    with pytest.raises(PermissionError, match='MASTER_ASSET_READ_ONLY'):
        service.create_candidate(bridge, storage, 'op', 'HeroBody', 'm521:' + '0' * 64)
    assert bridge.bodies == [CURRENT_FILE]
    assert not (storage.root / 'checkpoints').exists()


def test_redesign_session_apply_refuses_master_first(master):
    bridge, storage = master
    with pytest.raises(PermissionError, match='MASTER_ASSET_READ_ONLY'):
        service.redesign_session(bridge, storage, 'op', 'HeroBody', 'more mass', 'manifest.json', apply=True)
    assert bridge.bodies == [CURRENT_FILE]


# ------------------------------------------------------------------ skinning QA arithmetic

def rows(counts):
    output = []
    for source, candidate in counts:
        for mesh, value in (('Body', source), ('Cand', candidate)):
            metrics = {'collapsed_edges': 0, 'stretched_edges': value, 'collapsed_triangles': 0, 'min_edge_ratio': 0.9,
                       'max_edge_ratio': 1.1, 'min_triangle_area_ratio': 0.9, 'volume_ratio': 1.0}
            output.append({'mesh': mesh, 'region': 'mesh', 'metrics': metrics})
    return output


POSES = [{'region': 'shoulder', 'bone': 'upper_arm.L', 'axis': 'Z', 'angle_degrees': 45},
         {'region': 'elbow', 'bone': 'forearm.L', 'axis': 'Z', 'angle_degrees': 60}]


@pytest.mark.parametrize('counts, passed', [([(0, 0), (3, 3)], True), ([(0, 0), (3, 4)], False)])
def test_skinning_qa_fails_on_any_added_issue(monkeypatch, counts, passed):
    from blender_mcp.production import deformation

    def fake(bridge, storage, operation_id, rig, stress_poses, dry_run, values):
        assert values['meshes'] == ['Body', 'Cand'] and dry_run is False
        return {'status': 'ok', 'rows': rows(counts), 'samples': [{'pose': p} for p in stress_poses], 'state_restored': True}
    monkeypatch.setattr(deformation, 'test_pose', fake)
    result = service.skinning_qa(None, None, 'op', 'Rig', 'Body', 'Cand', POSES)
    assert result['passed'] is passed
    assert bool(result['regressions']) is not passed


def test_skinning_qa_incomplete_run_does_not_pass(monkeypatch):
    from blender_mcp.production import deformation
    monkeypatch.setattr(deformation, 'test_pose', lambda *a, **k: {'status': 'error', 'rows': [], 'samples': [], 'errors': ['x']})
    result = service.skinning_qa(None, None, 'op', 'Rig', 'Body', 'Cand', POSES)
    assert not result['passed'] and result['errors'][0].startswith('DEFORMATION_QA_INCOMPLETE')


# ------------------------------------------------------------------ argument validation

@pytest.mark.parametrize('call', [
    lambda: service._views(['TOP']), lambda: service._views(['FRONT', 'FRONT']),
    lambda: service.analyze_surface(None, None, 'Body', None, {'robust_k': -1}),
    lambda: service.analyze_proportions(None, None, 'Body', 'ALIEN'),
    lambda: service.redesign_plan(None, None, 'Body', '  '),
    lambda: service.inspect(None, None, 'Body', 0.5),
    lambda: service.render_multiview(None, None, 'op', ['Body'], None, 10, 512),
])
def test_invalid_arguments_are_refused(call):
    with pytest.raises(ValueError):
        call()
