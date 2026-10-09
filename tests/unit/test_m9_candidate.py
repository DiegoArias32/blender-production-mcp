"""Candidate ownership and atomic preconditions cannot be caller assertions."""
import ast
from types import SimpleNamespace

import pytest

from blender_mcp.production.materials import candidate
from blender_mcp.production.common.bridge import build_code


def functions(*names):
    tree = ast.parse(candidate.RUNTIME)
    selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
    namespace = {}
    exec(compile(selected, '<M9 candidate guards>', 'exec'), namespace)
    return namespace


class Item(dict):
    pass


def owned_pair():
    mesh = Item(production_m9_owned=True, production_generation_id='a' * 32)
    mesh.users = 1
    material = Item(production_m9_owned=True, production_generation_id='a' * 32)
    obj = Item(production_m9_owned=True, production_generation_id='a' * 32)
    obj.data = mesh
    obj.material_slots = [SimpleNamespace(material=material)]
    return obj, material


def test_shared_candidate_material_rejected():
    ns = functions('m9_fail', 'm9_owned')
    obj, material = owned_pair()
    foreign = Item(name='Foreign')
    foreign.material_slots = [SimpleNamespace(material=material)]
    ns['bpy'] = SimpleNamespace(data=SimpleNamespace(objects=[obj, foreign]))
    with pytest.raises(ValueError, match='MATERIAL_CANDIDATE_SHARED'):
        ns['m9_owned'](obj, material, 'a' * 32)


def test_caller_cannot_claim_foreign_generation():
    ns = functions('m9_fail', 'm9_owned')
    obj, material = owned_pair()
    with pytest.raises(ValueError, match='MATERIAL_CANDIDATE_OWNERSHIP'):
        ns['m9_owned'](obj, material, 'b' * 32)


def test_shared_candidate_mesh_rejected():
    ns = functions('m9_fail', 'm9_owned')
    obj, material = owned_pair()
    obj.data.users = 2
    with pytest.raises(ValueError, match='MATERIAL_CANDIDATE_SHARED'):
        ns['m9_owned'](obj, material, 'a' * 32)


@pytest.mark.parametrize('generation,version', [('not-a-generation', 1), ('A' * 32, 1), ('a' * 32, True), ('a' * 32, 0)])
def test_invalid_candidate_scope_rejected(generation, version):
    ns = functions('m9_fail', 'm9_candidate_scope')
    with pytest.raises(ValueError, match='MATERIAL_GENERATION_INVALID|MATERIAL_VERSION_INVALID'):
        ns['m9_candidate_scope']({'generation_id': generation, 'version': version, 'candidate_name': 'OwnCandidate'})


def test_create_payload_remains_safe_mode_valid():
    assert len(build_code(candidate.CREATE, {}).encode()) < 200000


def test_external_texture_change_rejected_before_candidate_runtime(monkeypatch):
    monkeypatch.setattr(candidate.integrity, 'status', lambda *args: {'fingerprint': 'new file bytes'})
    class NoMutation:
        def run(self, *args):
            raise AssertionError('stale external image reached candidate creation')
    with pytest.raises(ValueError, match='MATERIAL_STALE_SOURCE'):
        candidate.create(NoMutation(), {'object_name': 'Source', 'material_name': 'SourceMaterial',
            'expected_status': {'fingerprint': 'previous file bytes'}})
