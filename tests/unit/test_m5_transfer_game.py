"""Transfer conservation, safe schema boundaries and installed-runtime payload invariants."""
import ast

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.transfer import service as transfer
from blender_mcp.production.transfer.runtime import TRANSFER_RUNTIME, WEIGHT_RUNTIME, ATTRIBUTE_RUNTIME, MORPH_RUNTIME
from blender_mcp.production.game_mesh import service as game
from blender_mcp.production.game_mesh.runtime import NORMAL_RUNTIME, TRI_RUNTIME, GAME_RUNTIME


def function(name):
    tree = ast.parse(TRANSFER_RUNTIME)
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<transfer invariant>', 'exec'), namespace)
    return namespace[name]


def test_barycentric_attribute_interpolation_conserves_constants_and_linear_fields():
    sample = function('mt_sample_value')
    assert sample([.4, .4, .4], [0, 1, 2], [.2, .3, .5], False) == pytest.approx(.4)
    assert sample([[0, 0, 1, 1], [1, 0, 1, 1], [0, 1, 1, 1]], [0, 1, 2], [.2, .3, .5], False) == pytest.approx([.3, .5, 1, 1])


def test_discrete_semantic_attributes_are_not_fractionally_blended():
    sample = function('mt_sample_value')
    assert sample([3, 9, 11], [0, 1, 2], [.2, .3, .5], True) == 11
    assert sample([False, True, False], [0, 1, 2], [.1, .8, .1], True) is True


@pytest.mark.parametrize('values', [
    {'influence_limit': True}, {'influence_limit': 0}, {'max_distance_m': float('nan')},
    {'normalization_tolerance': 2}, {'stress_poses': 'automatic'},
    {'side_pairs': [{'positive': 'a', 'negative': 'a'}]}, {'side_plane': {'axis': True}},
    {'groups': ['a', 'a']}, {'groups': ['']}, {'normalize': True}, {'reject_discontinuities': 1},
])
def test_weight_validation_never_accepts_unsafe_or_implicit_policy(values):
    with pytest.raises(ValueError):
        transfer.options(values)


def test_engine_influence_limit_and_deformation_tolerance_are_explicitly_configurable():
    a = transfer.options({'influence_limit': 4, 'deformation_error_m': .002})
    b = transfer.options({'influence_limit': 12, 'deformation_error_m': .025})
    assert a['influence_limit'] == 4 and b['influence_limit'] == 12
    assert a['deformation_error_m'] < b['deformation_error_m']


@pytest.mark.parametrize('values', [
    {'custom_properties': {'object': []}}, {'attributes': ['color', 'color']},
    {'attributes': ['']}, {'materials': False}, {'materials': 'all'}, {'copy_everything': True},
])
def test_attribute_transfer_requires_allowlisted_selection(values):
    with pytest.raises(ValueError):
        transfer.options(values, 'attributes')


def test_custom_normal_rebuilding_requires_a_distinct_confirmation():
    assert game.options()['custom_normals'] == 'preserve'
    with pytest.raises(ValueError, match='CUSTOM_NORMAL_REBUILD_REQUIRES_CONFIRMATION'):
        game.options({'custom_normals': 'rebuild'})
    assert game.options({'custom_normals': 'rebuild', 'confirm_rebuild_custom_normals': True})['custom_normals'] == 'rebuild'


@pytest.mark.parametrize('values', [
    {'quad_method': 'RANDOM'}, {'ngon_method': 'FAST'}, {'normal_policy': 'flip_all'},
    {'uv_area_epsilon': 0}, {'normal_convention': 'guess'}, {'sample_count': True}, {'stress_poses': 'guess'},
])
def test_geometry_and_tangent_policies_are_finite_and_explicit(values):
    with pytest.raises(ValueError):
        game.options(values)


@pytest.mark.parametrize('template', [WEIGHT_RUNTIME, ATTRIBUTE_RUNTIME, MORPH_RUNTIME, NORMAL_RUNTIME, TRI_RUNTIME], ids=['weights', 'attrs', 'morphs', 'normals', 'tris'])
def test_mutation_and_verification_payloads_obey_upstream_sandbox(template):
    # Both apply and verify are roots: computed getattr/lambda errors must be caught before Blender.
    code = build_code(template + '\nhigh=m5_obj("h");low=m5_obj("l");candidate=m5_obj("c")\n'
                      'changes=m5_apply(high,low,candidate,{}, {})\nresult=m5_verify(high,low,candidate,{}, {})', {})
    assert len(code.encode()) < 80000
    assert len(list(ast.walk(ast.parse(code)))) < 15000


def test_tangent_payload_uses_real_blender_generation_with_no_dynamic_rna_access():
    code = build_code(GAME_RUNTIME+'\nlow=m5_obj("low")\nresult=gm_tangents(low,{})', {})
    assert 'mesh.calc_tangents(uvmap=layer)' in code
    assert 'getattr(' not in code
