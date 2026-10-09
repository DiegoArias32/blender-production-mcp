"""Phases 18-20 without Blender: payload safety, validation and locked-region arithmetic.

Real geometry is exercised by tests/blender/m31_mesh_probe.py.
"""
import ast
import copy

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.mesh import runtime
from blender_mcp.production.mesh.service import compare, compare_region, validate_regions


@pytest.mark.parametrize('template', ['SIGNATURE', 'REGION_CAPTURE', 'CANDIDATE_FIND', 'CANDIDATE_CREATE', 'CANDIDATE_PAIR'])
def test_mesh_payloads_pass_safe_mode_within_node_budget(template):
    code = build_code(getattr(runtime, template), {'object_name': 'Mesh', 'regions': [], 'candidate_name': 'C',
                                                   'source_fingerprint': 'm521:x', 'suffix': 'abc', 'operation_id': 'op'})
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 18000


@pytest.mark.parametrize('regions', [
    [], [{'name': 'Face', 'role': 'frozen', 'definition': {'method': 'vertex_group', 'name': 'Face'}}],
    [{'name': 'Face', 'role': 'locked', 'definition': {'method': 'object_name', 'name': 'Head'}}],
    [{'name': 'Face', 'role': 'locked', 'definition': {'method': 'indices', 'indices': [1, 2]}}],  # unbound indices
    [{'name': 'Face', 'role': 'locked', 'definition': {'method': 'vertex_group', 'name': 'Face'}, 'tolerance_m': 1.0}],
    [{'name': 'A', 'role': 'locked', 'definition': {'method': 'face_set', 'ids': []}}],
    [{'name': 'A', 'role': 'locked', 'definition': {'method': 'mask', 'min_mask': 0}}],
    [{'name': 'A', 'role': 'locked', 'definition': {'method': 'vertex_group', 'name': 'A'}}] * 2,
])
def test_invalid_region_definitions_are_refused(regions):
    with pytest.raises(ValueError):
        validate_regions(regions)


IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def capture(**overrides):
    base = {'topology_fingerprint': 'T', 'indices': [3, 7], 'indices_digest': 'I', 'positions': [0, 0, 0, 1, 1, 1],
            'shape_keys': {'Basis': [0, 0, 0, 1, 1, 1]}, 'weights_digest': 'W', 'matrix_world': IDENTITY, 'unit_scale': 1.0}
    base.update(overrides)
    return base


def region(role='locked', tolerance=1e-5):
    return {'name': 'Face', 'role': role, 'tolerance_m': tolerance, 'capture': capture()}


def test_unchanged_locked_region_passes():
    row = compare_region(region(), capture())
    assert row['passed'] and row['max_displacement_m'] == 0.0 and not row['failures']


def test_locked_vertex_moved_beyond_tolerance_fails_with_world_distance():
    scaled = [[2, 0, 0, 0], [0, 2, 0, 0], [0, 0, 2, 0], [0, 0, 0, 1]]
    row = compare_region(region(), capture(positions=[0, 0, 0, 1, 1, 1.001], matrix_world=scaled))
    assert not row['passed'] and row['worst_vertex'] == 7
    assert row['max_displacement_m'] == pytest.approx(0.002)  # 1 mm local x object scale 2
    assert row['failures'][0].startswith('LOCKED_REGION_MODIFIED')


def test_editable_region_changes_are_reported_not_failed():
    row = compare_region(region('editable'), capture(positions=[0, 0, 0, 1, 1, 2]))
    assert row['passed'] and row['changed']


@pytest.mark.parametrize('overrides, code', [
    ({'topology_fingerprint': 'T2'}, 'TOPOLOGY_CHANGED'),
    ({'indices_digest': 'I2'}, 'REGION_MEMBERSHIP_CHANGED'),
    ({'weights_digest': 'W2'}, 'LOCKED_WEIGHTS_CHANGED'),
    ({'shape_keys': {}}, 'SHAPE_KEY_MISSING'),
    ({'shape_keys': {'Basis': [0, 0, 0, 1, 1, 1], 'Smile': [0, 0, 0.01, 1, 1, 1]}}, 'NEW_SHAPE_KEY_MOVES_LOCKED_REGION'),
])
def test_locked_region_mutations_are_detected(overrides, code):
    row = compare_region(region(), capture(**overrides))
    assert not row['passed'] and any(f.startswith(code) for f in row['failures'])


def test_preservation_report_names_the_changed_dependency():
    sections = {name: {'fingerprint': name} for name in ('relationships', 'modifiers', 'vertex_groups', 'shape_keys', 'materials',
                                                         'uv_layers', 'normals', 'custom_properties', 'topology', 'geometry',
                                                         'attributes', 'transform')}
    source = {'sections': sections}
    candidate = {'sections': copy.deepcopy(sections)}
    assert all(item['preserved'] for item in compare(source, candidate).values())
    candidate['sections']['modifiers']['fingerprint'] = 'changed'
    report = compare(source, candidate)
    assert report['armature_modifier'] == {'preserved': False, 'differing_sections': ['modifiers']}
    assert sum(not item['preserved'] for item in report.values()) == 1
