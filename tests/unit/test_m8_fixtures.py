"""Fixture geometry/coverage contracts; actual Blender qualification is separate."""
import importlib.util
import math
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('fixtures_m8', Path(__file__).resolve().parents[1] / 'blender' / 'fixtures_m8.py')
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)


def _boundary_edges(faces):
    counts = {}
    for face in faces:
        for a, b in zip(face, face[1:]+face[:1]):
            edge = tuple(sorted((a,b)))
            counts[edge] = counts.get(edge,0)+1
    return [edge for edge, count in counts.items() if count != 2]


def _triangle_area(a,b,c):
    x, y = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
    cross = (x[1]*y[2]-x[2]*y[1], x[2]*y[0]-x[0]*y[2], x[0]*y[1]-x[1]*y[0])
    return .5*math.sqrt(sum(v*v for v in cross))


def test_all_requested_fixture_families_have_observable_contracts():
    specs = FIXTURES.specifications()
    assert len(specs) >= 20
    assert {s['classification'] for s in specs.values()} >= {
        'CLOTH','HAIR','SPRING_BONE','DANGLING_ACCESSORY','RIGID',
        'CHAIN_LIKE','CABLE_LIKE','SOFT_REGION','UNSUPPORTED','AMBIGUOUS'}
    assert all(s['fixture_only'] and s['expected_qa'] == 'NOT_RUN' for s in specs.values())
    assert all(s['backend_claim'] == 'NONE_BEFORE_PRODUCTION_TOOLS' for s in specs.values())
    defects = {s.get('defect') for s in specs.values()}
    assert defects >= {'INITIAL_BODY_PENETRATION','DRIVER_RUNAWAY','FOLDED_SELF_CONTACT',
                       'INITIAL_HEAD_PENETRATION','LOW_DAMPING','EXCESSIVE_DISPLACEMENT',
                       'CACHE_INPUT_CHANGE_REQUIRED','PRIMARY_ACTION_CHANGE_REQUIRED'}


@pytest.mark.parametrize('grid', [3,16,25])
def test_cloth_has_complete_pins_connected_grid_and_finite_positive_area(grid):
    panel = FIXTURES.panel_geometry(grid)
    vertices, faces = panel['vertices'], panel['faces']
    assert len(vertices) <= 625
    assert len(_boundary_edges(faces)) == 4*(grid-1)
    assert set(panel['pins']) == {i for i,v in enumerate(vertices) if abs(v[2]-FIXTURES.CLOTH_TOP_M)<1e-12}
    assert all(all(math.isfinite(v) for v in vertex) for vertex in vertices)
    assert all(_triangle_area(vertices[f[0]],vertices[f[1]],vertices[f[2]]) > 1e-8 for f in faces)
    for face in faces:
        a,b,c = (vertices[face[i]] for i in range(3))
        cross_y = (b[2]-a[2])*(c[0]-a[0])-(b[0]-a[0])*(c[2]-a[2])
        assert cross_y < 0  # -Y front-facing cloth surface.


def test_penetration_fixture_really_enters_the_torso_bounds():
    panel = FIXTURES.panel_geometry(defect='INITIAL_BODY_PENETRATION')
    penetrated = [v for v in panel['vertices'] if abs(v[0]) < .25 and abs(v[1]) < .13 and 1.05<v[2]<1.65]
    normal = FIXTURES.panel_geometry()
    assert len(penetrated) > 0
    assert not any(abs(v[0]) < .25 and abs(v[1]) < .13 and 1.05<v[2]<1.65 for v in normal['vertices'])


def test_self_contact_variant_has_distinct_folded_layers_with_real_geometry():
    panel = FIXTURES.panel_geometry(defect='FOLDED_SELF_CONTACT')
    assert min(v[1] for v in panel['vertices']) == -.34
    assert max(v[1] for v in panel['vertices']) == -.337
    assert panel['vertices'][-1][2] == pytest.approx(FIXTURES.CLOTH_TOP_M)
    assert panel['pins'] == list(range(16))


def test_colliders_are_closed_with_finite_geometry():
    vertices,faces = FIXTURES.closed_box((0,0,1.35),(.25,.13,.3))
    assert not _boundary_edges(faces)
    assert all(_triangle_area(vertices[f[0]],vertices[f[1]],vertices[f[2]]) > 0 for f in faces)


def test_soft_authorized_region_is_strict_subset_with_protected_asymmetry():
    geometry = FIXTURES.soft_geometry()
    vertices,weights = geometry['vertices'],geometry['weights']
    assert not _boundary_edges(geometry['faces'])
    assert 0 < len(weights) < .8*len(vertices)
    assert set(weights).isdisjoint(geometry['protected_indices'])
    assert len(weights)+len(geometry['protected_indices']) == len(vertices)
    assert all(0 < weight <= 1 for weight in weights.values())
    extent_positive = max(v[0] for v in vertices)-.23
    extent_negative = .23-min(v[0] for v in vertices)
    assert extent_positive > extent_negative*1.3
    assert all(math.isfinite(value) for v in vertices for value in v)


def test_primary_driver_has_real_acceleration_and_a_rest_tail():
    keys = FIXTURES.primary_keys(30,2)
    assert keys[-1]['frame'] == 61
    assert keys[-2]['translation_x_m'] == keys[-1]['translation_x_m'] == 0
    assert max(k['translation_x_m'] for k in keys) > .1
    assert any(k['head_angle_deg'] != 0 for k in keys)
    assert keys == FIXTURES.primary_keys(30,2)
    assert FIXTURES.primary_keys(60,2)[-1]['frame'] == 121
    assert max(k['translation_x_m'] for k in FIXTURES.primary_keys(30,2,True)) == 1000


@pytest.mark.parametrize('kind,primary,count', [
    ('HAIR','PrimaryHead',6),('SPRING_BONE','PrimaryChest',2),
    ('CHAIN_LIKE','PrimaryChest',2),('CABLE_LIKE','PrimaryChest',2),
])
def test_off_axis_secondary_roots_are_exclusively_primary_bound(kind,primary,count):
    geometry = FIXTURES.secondary_geometry(kind,primary)
    roots = set(geometry['root_indices'])
    assert len(roots) == count
    assert roots == set(geometry['membership'][primary])
    assert geometry['root_group'] == 'M8_ExplicitSecondaryRoots'
    assert all(roots.isdisjoint(geometry['membership'][bone]) for bone in geometry['bone_names'])
    all_members = [index for indices in geometry['membership'].values() for index in indices]
    assert len(all_members) == len(set(all_members)) == len(geometry['vertices'])
    assert min(geometry['vertices'][i][2] for i in roots) == max(v[2] for v in geometry['vertices'])
    assert geometry['qa_status'] == 'NOT_RUN'
    assert geometry['fixture_only'] is True


def test_secondary_root_geometry_does_not_guess_primary_attachment():
    with pytest.raises(ValueError,match='Explicit primary'):
        FIXTURES.secondary_geometry('HAIR','')


def test_hair_fixture_calibration_bounds_rotation_without_relaxing_qa():
    specs = FIXTURES.specifications()
    assert specs['hair_bone_cards']['settings']['max_angle_deg'] == 2
    assert 'qa' not in specs['hair_bone_cards']
    assert specs['hair_bone_cards']['expected_qa'] == 'NOT_RUN'
    assert specs['hair_collision']['settings'] == specs['hair_bone_cards']['settings']


def test_combined_body_selection_requires_controlled_metadata_and_exact_binding():
    summary={'fixture_only':True,'m6_proxy_kind':'closed_segmented_rigid_skin',
             'armatures':['ChosenRig'],'shape_keys':False}
    assert FIXTURES.controlled_combined_body(summary,'ChosenRig')
    for wrong in ({'fixture_only':False},{'m6_proxy_kind':None},{'armatures':['OtherRig']},
                  {'armatures':['ChosenRig','OtherRig']},{'shape_keys':True}):
        assert not FIXTURES.controlled_combined_body(dict(summary,**wrong),'ChosenRig')


def test_visible_body_proxy_removes_only_the_declared_disconnected_head_component():
    body,body_faces=FIXTURES.closed_box((0,0,1),(.2,.1,.3))
    head,head_faces=FIXTURES.closed_box((0,0,1.5),(.1,.1,.1))
    original=body+head
    faces=body_faces+[tuple(i+8 for i in face) for face in head_faces]
    proxy=FIXTURES.body_without_head_geometry(original,faces,list(range(8,16)))
    assert proxy['vertices']==body
    assert proxy['faces']==body_faces
    assert proxy['retained_indices']==list(range(8))
    assert proxy['removed_vertices']==8
    assert original==body+head
    assert not _boundary_edges(proxy['faces'])


def test_visible_body_proxy_rejects_mixed_head_body_faces_instead_of_opening_a_surface():
    vertices=[(0,0,0),(1,0,0),(0,1,0),(0,0,1)]
    with pytest.raises(ValueError,match='HEAD_COMPONENT_NOT_SEPARATE'):
        FIXTURES.body_without_head_geometry(vertices,[(0,1,2),(0,2,3)],[3])


@pytest.mark.parametrize('variant,fps,duration', [
    ('unknown',30,2), ('cloth_heavy',True,2), ('cloth_heavy',24,2),
    ('cloth_heavy',30,False), ('cloth_heavy',30,math.nan),
    ('cloth_heavy',30,math.inf), ('cloth_heavy',30,.5), ('cloth_heavy',30,11),
    ('cloth_heavy',30,1.01),
])
def test_fixture_requests_reject_invalid_scale_in_time(variant,fps,duration):
    with pytest.raises(ValueError):
        FIXTURES.validate_request(variant,fps,duration)
