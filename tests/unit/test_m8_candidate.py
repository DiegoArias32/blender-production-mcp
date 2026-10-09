"""M8 candidates atomically recheck and refuse primary-channel overlap."""
import ast
from types import SimpleNamespace as NS

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.physics.candidate import CREATE, RUNTIME, create
from blender_mcp.safe_mode import MAX_CODE_BYTES, MAX_AST_NODES


def functions(*names):
    by_name = {n.name: n for n in ast.parse(RUNTIME).body if isinstance(n, ast.FunctionDef)}
    ns = {}
    exec(compile(ast.Module(body=[by_name[n] for n in names], type_ignores=[]), '<candidate>', 'exec'), ns)
    return ns


def test_candidate_payload_keeps_atomic_recheck_and_no_primary_animation_assignment():
    code = build_code(CREATE, {'source_object': 'Fixture', 'generation_id': 'id', 'candidate_name': 'Owned',
        'subsystem': 'CLOTH', 'expected_status': {'fingerprint': 'old'}})
    assert len(code.encode()) < MAX_CODE_BYTES*.7
    assert sum(1 for _ in ast.walk(ast.parse(code))) < MAX_AST_NODES*.7
    assert 'PHYSICS_STALE_SOURCE' in code and 'm3_guard' not in code
    assert '.action =' not in code and '.action=' not in code
    assert 'production_m8_owned' in code and 'after_original_fingerprint' in code


def test_stale_expected_status_rejects_before_any_datablock_access():
    ns = functions('px_fail', 'px_make_candidate')
    ns['px_status'] = lambda params: {'fingerprint': 'new'}
    with pytest.raises(ValueError, match='PHYSICS_STALE_SOURCE'):
        ns['px_make_candidate']({'expected_status': {'fingerprint': 'old'}})


@pytest.mark.parametrize('path', ['pose.bones["Hair"].rotation_euler', 'pose.bones["Hair"]["custom"]'])
def test_existing_active_or_nla_primary_chain_channels_are_rejected(path):
    ns = functions('px_fail', 'px_chain_guard')
    bone = NS(name='Hair', constraints=[], parent=None, path_from_id=lambda: 'pose.bones["Hair"]')
    class Bones(dict):
        def __iter__(self): return iter(self.values())
    action = NS(name='Primary')
    rig = NS(type='ARMATURE', pose=NS(bones=Bones(Hair=bone)), animation_data=NS(action=None,
        nla_tracks=[NS(strips=[NS(action=action)])]))
    ns['px_object'] = lambda name: rig
    ns['px_animation'] = lambda owner: {}
    ns['action_curves'] = lambda a, slot: [(7, 0, 0, NS(data_path=path))]
    with pytest.raises(ValueError, match='PHYSICS_SECONDARY_PRIMARY_OVERLAP'):
        ns['px_chain_guard']({'armature': 'Rig', 'bones': ['Hair']})


def test_secondary_prefix_does_not_match_other_primary_bone():
    ns = functions('px_fail', 'px_chain_guard')
    bone = NS(name='Hair', constraints=[], parent=None, path_from_id=lambda: 'pose.bones["Hair"]')
    rig = NS(type='ARMATURE', pose=NS(bones={'Hair': bone}), animation_data=NS(action=NS(name='Body'), nla_tracks=[]))
    ns['px_object'] = lambda name: rig; ns['px_animation'] = lambda owner: {}
    ns['action_curves'] = lambda a, slot: [(1, 0, 0, NS(data_path='pose.bones["Root"].location'))]
    returned, names = ns['px_chain_guard']({'armature': 'Rig', 'bones': ['Hair']})
    assert returned is rig and names == ['Hair']


def test_candidate_create_forwards_only_structured_params():
    class Bridge:
        def run(self, body, params):
            assert body == CREATE
            assert params['candidate_name'] == 'Candidate\"safe'
            return {'status': 'SECONDARY_CANDIDATE'}
    assert create(Bridge(), {'candidate_name': 'Candidate\"safe'})['status'] == 'SECONDARY_CANDIDATE'


def test_candidate_scope_versions_share_generation_but_have_distinct_collections():
    ns=functions('px_fail','px_candidate_scope'); generation='0123456789abcdef'*2
    one=ns['px_candidate_scope']({'candidate_name':'Copy_v1','generation_id':generation,'version':1})
    two=ns['px_candidate_scope']({'candidate_name':'Copy_v2','generation_id':generation,'version':2})
    assert one[-1]=='M8_'+generation+'_v1' and two[-1]=='M8_'+generation+'_v2'
    assert one[1]==two[1] and one[-1]!=two[-1]


@pytest.mark.parametrize('generation', ['cloth-probe', 'a'*31, 'a'*33, 'G'*32, 'A'*32, 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'])
def test_candidate_generation_requires_canonical_uuid_hex(generation):
    ns=functions('px_fail','px_candidate_scope')
    with pytest.raises(ValueError,match='PHYSICS_GENERATION_ID_INVALID'):
        ns['px_candidate_scope']({'candidate_name':'Copy','generation_id':generation})


@pytest.mark.parametrize('version', [True, 0, -1, 1.5, '2', None])
def test_candidate_version_is_a_positive_integer(version):
    ns=functions('px_fail','px_candidate_scope')
    with pytest.raises(ValueError,match='PHYSICS_CANDIDATE_VERSION_INVALID'):
        ns['px_candidate_scope']({'candidate_name':'Copy','generation_id':'a'*32,'version':version})
