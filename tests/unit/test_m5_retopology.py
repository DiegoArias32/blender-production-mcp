"""M5 input invariants, compact contracts and unchanged safe-mode policy."""
import ast
import math
import pytest
from types import SimpleNamespace

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.retopology import service
from blender_mcp.production.retopology.runtime import RETOPOLOGY_RUNTIME, REDUCTION_RUNTIME, PROJECTION_RUNTIME


@pytest.mark.parametrize('field,value', [('target_triangles', True), ('target_triangles', 0),
    ('target_triangles', 1.5), ('samples', 1000000), ('max_distance_m', math.nan),
    ('max_normal_angle_degrees', math.inf), ('silhouette_resolution', 10000),
    ('minimum_silhouette_iou', 1.1), ('allow_boundary', 1), ('reject_ambiguous_components', 'yes')])
def test_invalid_safety_thresholds_rejected(field, value):
    with pytest.raises(ValueError):
        service.options({field: value})


def test_unknown_field_cannot_silently_disable_protection():
    with pytest.raises(ValueError, match='Unknown'):
        service.options({'ignore_locked': True})


def test_world_units_and_asset_budget_are_configurable():
    assert service.options({'target_triangles': 5000, 'max_distance_m': .0002})['target_triangles'] == 5000
    assert service.options({'target_triangles': 120000})['max_distance_m'] == .01


def test_long_index_region_cannot_enter_rpc_proposal():
    with pytest.raises(ValueError, match='COMPACT_REGIONS'):
        service.regions([{'name': 'face', 'definition': {'method': 'indices', 'indices': [0],
                           'topology_fingerprint': 'source'}, 'protected': True}])


@pytest.mark.parametrize('row', [
    {'name': 'x', 'definition': {'method': 'bounds', 'min': [0, 0, 0], 'max': [1, 1, 1]}, 'priority': math.nan},
    {'name': 'x', 'definition': {'method': 'vertex_group', 'name': 'X'}, 'deformation': 'yes'},
    {'name': 'x', 'definition': {'method': 'vertex_group', 'name': 'X'}, 'budget': {'percentage': 120}},
    {'name': 'x', 'definition': {'method': 'vertex_group', 'name': 'X'}, 'budget': {'percentage': 10, 'absolute': 2}},
])
def test_invalid_region_override_rejected(row):
    with pytest.raises(ValueError):
        service.regions([row])


def test_duplicate_region_name_rejected():
    row = {'name': 'joint', 'definition': {'method': 'vertex_group', 'name': 'J'}}
    with pytest.raises(ValueError, match='unique'):
        service.regions([row, row])


@pytest.mark.parametrize('runtime,entry', [
    (RETOPOLOGY_RUNTIME, 'result=rt_topology(m5_obj(params["object_name"]),params["options"])'),
    (RETOPOLOGY_RUNTIME, 'result=rt_density(m5_obj(params["object_name"]),params["options"])'),
    (RETOPOLOGY_RUNTIME, 'result=rt_silhouette(m5_obj(params["high"]),m5_obj(params["low"]),params["options"])'),
    (REDUCTION_RUNTIME, 'result=m5_plan(m5_obj(params["high"]),m5_obj(params["low"]),params["options"],params["relation"])'),
    (REDUCTION_RUNTIME, 'result=m5_apply(m5_obj(params["high"]),m5_obj(params["low"]),m5_obj(params["candidate"]),params["proposal"],params["options"])'),
    (REDUCTION_RUNTIME, 'result=m5_verify(m5_obj(params["high"]),m5_obj(params["low"]),m5_obj(params["candidate"]),params["proposal"],params["options"])'),
    (PROJECTION_RUNTIME, 'result=m5_plan(m5_obj(params["high"]),m5_obj(params["low"]),params["options"],params["relation"])'),
], ids=['audit', 'density', 'silhouette', 'reduction-plan', 'reduction-apply', 'reduction-verify', 'projection-plan'])
def test_real_payloads_fit_unchanged_upstream_safe_mode(runtime, entry):
    code = build_code(runtime + '\n' + entry, {'object_name': 'x', 'high': 'h', 'low': 'l', 'candidate': 'c',
                                              'proposal': {}, 'options': {}, 'relation': {}})
    assert len(list(ast.walk(ast.parse(code)))) < 20000


def test_orthographic_views_are_unique_and_known():
    with pytest.raises(ValueError):
        service.options({'views': ['FRONT', 'FRONT']})
    with pytest.raises(ValueError):
        service.options({'views': ['MAGICAL']})


def test_priority_region_does_not_infer_human_names():
    result = service.regions([{'name': 'Oversized_Antenna', 'definition': {'method': 'vertex_group', 'name': 'Feature42'},
                               'protected': True, 'priority': 8, 'deformation': False}])
    assert result[0]['name'] == 'Oversized_Antenna'
    assert result[0]['protected'] is True


def region_runtime():
    tree = ast.parse(RETOPOLOGY_RUNTIME)
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'rt_regions')
    namespace = {'mr_region_indices': lambda obj, data, definition: [0, 1]}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<region-safety>', 'exec'), namespace)
    return namespace['rt_regions']


def test_empty_override_cannot_remove_registered_protection():
    fn = region_runtime()
    obj = SimpleNamespace(data=None)
    baseline = {'name': 'LockedMass', 'definition': {'method': 'vertex_group', 'name': 'M'}, 'protected': True}
    result = fn(obj, {'regions': []}, {'metadata': {'regions': [baseline]}})
    assert len(result) == 1 and result[0]['protected']


@pytest.mark.parametrize('update', [{'protected': False}, {'tolerance_m': .01},
                                   {'definition': {'method': 'vertex_group', 'name': 'Other'}}])
def test_registered_protection_cannot_be_implicitly_weakened(update):
    fn = region_runtime()
    obj = SimpleNamespace(data=None)
    baseline = {'name': 'LockedMass', 'definition': {'method': 'vertex_group', 'name': 'M'}, 'protected': True,
                'tolerance_m': 1e-5}
    with pytest.raises(ValueError, match='PROTECTED_REGION_OVERRIDE_REJECTED'):
        fn(obj, {'regions': [dict(baseline, **update)]}, {'metadata': {'regions': [baseline]}})


def test_options_requires_object_even_when_empty():
    with pytest.raises(ValueError):
        service.options([])
