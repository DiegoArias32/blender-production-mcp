import math

import pytest

from blender_mcp.production.physics.qa import (
    RUNTIME, closed_triangles, geometry_metrics, motion_step, same_topology,quaternion_angle_deg, validate_thresholds,
)
from blender_mcp.safe_mode import validate_code
from blender_mcp.production.common.bridge import build_code


PLANE = [(0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.)]
EDGES = [(0,1),(1,2),(2,3),(3,0),(0,2)]
TRIANGLES = [(0,1,2),(0,2,3)]
TETRA = [(0.,0.,0.),(1.,0.,0.),(0.,1.,0.),(0.,0.,1.)]
TETRA_TRIANGLES = [(0,2,1),(0,1,3),(1,2,3),(2,0,3)]


def measure(points, previous=None, older=None, thresholds=None):
    return motion_step(points,PLANE,previous,older,30.,EDGES,TRIANGLES,validate_thresholds(thresholds))


def test_runtime_keeps_upstream_safe_mode():
    for entry in ('pq_motion','pq_collision','pq_bake_compare'):
        validate_code(build_code(RUNTIME+'\nresult='+entry+'(params)\n',{}))
    assert 'sign(normal)' not in RUNTIME
    assert 'captured_objects,captured_state=pq_capture' in RUNTIME
    assert 'px_restore(captured_objects,captured_state)' in RUNTIME


def test_quaternion_angle_is_shortest_normalized_and_stable_near_zero():
    identity=(1.,0.,0.,0.)
    ten=(math.cos(math.radians(5)),0.,0.,math.sin(math.radians(5)))
    assert quaternion_angle_deg(identity,ten)==pytest.approx(10.)
    assert quaternion_angle_deg(identity,tuple(-v for v in ten))==pytest.approx(10.)
    assert quaternion_angle_deg(ten,ten)==0
    assert quaternion_angle_deg(tuple(v*3 for v in ten),ten)==pytest.approx(0,abs=1e-12)


@pytest.mark.parametrize('bad', [(0.,0.,0.,0.),(math.nan,0.,0.,0.),(1.,0.,0.)])
def test_invalid_quaternion_cannot_certify_angular_bound(bad):
    with pytest.raises(ValueError,match='SECONDARY_ANGLE_ROTATION_INVALID'):
        quaternion_angle_deg(bad,(1.,0.,0.,0.))


def test_nonmanifold_and_open_surfaces_are_not_closed():
    assert not closed_triangles(TRIANGLES)
    assert not closed_triangles(TETRA_TRIANGLES+[TETRA_TRIANGLES[0]])
    assert not closed_triangles([(0,1,2),(0,1,2)])


def test_quad_retessellation_preserves_topology_and_uses_one_reference_diagonal():
    primary={'vertex_count':4,'edges':EDGES[:-1],'polygons':[(0,1,2,3)],'triangles':[(0,1,2),(0,2,3)]}
    candidate=dict(primary,triangles=[(0,1,3),(1,2,3)])
    assert same_topology(candidate,primary)
    bent=list(PLANE)
    bent[2]=(1.,1.,.2)
    report=motion_step(bent,PLANE,None,None,30.,candidate['edges'],candidate['triangles'],validate_thresholds())
    assert report['passed']
    assert report['geometry']['min_area_ratio']>=1


@pytest.mark.parametrize('change', [
    {'vertex_count':5},
    {'polygons':[(0,1,3,2)]},
    {'polygons':[(1,2,3,0)]},
    {'edges':[(0,1),(1,3),(3,2),(2,0)]},
])
def test_real_polygon_vertex_order_or_connectivity_change_is_not_retessellation(change):
    primary={'vertex_count':4,'edges':EDGES[:-1],'polygons':[(0,1,2,3)],'triangles':TRIANGLES}
    assert not same_topology(dict(primary,**change),primary)


def test_closedness_requires_consistent_winding_but_all_normal_reversal_is_valid():
    assert closed_triangles(TETRA_TRIANGLES)
    assert closed_triangles([tuple(reversed(t)) for t in TETRA_TRIANGLES])
    mixed = list(TETRA_TRIANGLES)
    mixed[0] = tuple(reversed(mixed[0]))
    assert not closed_triangles(mixed)


def test_rest_geometry_has_area_and_no_degeneracy():
    report = geometry_metrics(PLANE,EDGES,TRIANGLES,PLANE)
    assert report['valid']
    assert report['surface_area_m2'] == 1.
    assert report['min_area_ratio'] == 1.
    assert report['max_edge_stretch_ratio'] == 1.
    assert report['degenerate_triangles'] == 0
    assert report['signed_volume_m3'] is None


def test_cloth_rotating_normals_is_not_claimed_to_be_inverted():
    flipped = [(x,-y,-z) for x,y,z in PLANE]
    report = geometry_metrics(flipped,EDGES,TRIANGLES,PLANE)
    assert report['valid']
    assert not report['closed_volume_inverted']
    assert report['inversion_method'] == 'not_defined_for_open_folding_surface'


def test_closed_mesh_global_volume_inversion_is_detected():
    report = geometry_metrics([(x,y,-z) for x,y,z in TETRA],[],TETRA_TRIANGLES,TETRA)
    assert report['closed_oriented_mesh']
    assert report['closed_volume_inverted']
    assert report['signed_volume_m3'] == pytest.approx(-1/6)


def test_closed_volume_is_translation_invariant():
    translated = [(x+5.,y-3.,z+9.) for x,y,z in TETRA]
    report = geometry_metrics(translated,[],TETRA_TRIANGLES,TETRA)
    assert not report['closed_volume_inverted']
    assert report['signed_volume_m3'] == pytest.approx(1/6)


def test_collapsed_surface_is_detected_and_fails_motion():
    points = [(x,0.,z) for x,y,z in PLANE]
    report = measure(points)
    assert not report['passed']
    assert report['geometry']['degenerate_triangles'] == 2
    assert 'DEGENERATE_FRACTION_EXCEEDED' in report['errors']


def test_needle_area_fails_requested_minimum_without_numerical_degeneracy():
    report=measure([(x,y*.005,z) for x,y,z in PLANE])
    assert not report['passed']
    assert report['geometry']['degenerate_fraction']==0
    assert report['geometry']['min_area_ratio']==pytest.approx(.005)
    assert report['metrics']['min_area_ratio']==pytest.approx(.005)
    assert 'MIN_AREA_RATIO_BELOW_THRESHOLD' in report['errors']


def test_configurable_min_area_ratio_is_enforced_instead_of_default_only():
    needle=[(x,y*.1,z) for x,y,z in PLANE]
    assert measure(needle,thresholds={'min_area_ratio':.05})['passed']
    assert not measure(needle,thresholds={'min_area_ratio':.5})['passed']


@pytest.mark.parametrize('value', [-.1,1.01,math.inf,math.nan,True,'0.1'])
def test_minimum_area_threshold_requires_finite_zero_to_one(value):
    with pytest.raises(ValueError,match='INVALID_QA_THRESHOLD'):
        validate_thresholds({'min_area_ratio':value})


def test_bone_segments_do_not_invent_area_or_inverted_face_count():
    report=motion_step(PLANE,PLANE,None,None,30.,EDGES,[],validate_thresholds())
    assert report['passed']
    assert report['geometry']['min_area_ratio'] is None
    assert report['geometry']['area_measurement_status']=='NOT_APPLICABLE_NO_REFERENCE_TRIANGLE_AREA'
    assert report['geometry']['inversion_count'] is None
    assert not report['geometry']['local_face_inversion_certified']


def test_global_closed_inversion_is_a_failure_but_never_a_fabricated_face_count():
    points=[(x,y,-z) for x,y,z in TETRA]
    report=motion_step(points,TETRA,None,None,30.,[],TETRA_TRIANGLES,validate_thresholds())
    assert not report['passed']
    assert 'CLOSED_VOLUME_INVERTED' in report['errors']
    assert report['geometry']['inversion_count'] is None
    assert report['geometry']['inversion_measurement_status']=='MEASURED_GLOBAL_CLOSED_VOLUME_SIGN'
    assert not report['geometry']['local_face_inversion_certified']


def test_closed_volume_change_is_measured_against_same_frame_primary_without_new_limit():
    primary=[(x+10.,y,z) for x,y,z in TETRA]
    secondary=[(x+10.,y,z*.8) for x,y,z in TETRA]
    report=motion_step(secondary,primary,None,None,30.,[],TETRA_TRIANGLES,validate_thresholds())
    assert report['passed']
    assert report['geometry']['absolute_volume_ratio']==pytest.approx(.8)
    assert report['metrics']['absolute_volume_ratio']==pytest.approx(.8)
    assert 'CLOSED_VOLUME_CHANGE_MEASURED' in report['geometry']['stability_indicators']


def test_open_cloth_does_not_invent_a_volume_ratio():
    report=geometry_metrics(PLANE,EDGES,TRIANGLES,PLANE)
    assert report['absolute_volume_ratio'] is None
    assert report['reference_signed_volume_m3'] is None
    assert report['inversion_count'] is None
    assert report['inversion_measurement_status']=='UNDEFINED_OPEN_FOLDING_SURFACE'


@pytest.mark.parametrize('bad', [float('nan'),float('inf'),float('-inf')])
def test_nonfinite_geometry_is_never_pass(bad):
    points = list(PLANE)
    points[0] = (bad,0.,0.)
    report = measure(points)
    assert not report['passed']
    assert report['errors'] == ['NONFINITE_GEOMETRY']


def test_nonfinite_history_is_never_false_pass():
    previous = list(PLANE)
    previous[1] = (math.nan,0.,0.)
    assert measure(PLANE,previous)['errors'] == ['NONFINITE_MOTION_HISTORY']


def test_topology_change_and_empty_geometry_fail():
    assert measure(PLANE[:-1])['errors'] == ['TOPOLOGY_CHANGED']
    assert measure([])['errors'] == ['TOPOLOGY_CHANGED']
    assert measure(PLANE,PLANE[:-1])['errors'] == ['TOPOLOGY_CHANGED']
    assert measure(PLANE,PLANE,PLANE[:-1])['errors'] == ['TOPOLOGY_CHANGED']


def test_unbounded_stretch_fails_before_acceptance():
    report = measure([(x*10,y,z) for x,y,z in PLANE])
    assert not report['passed']
    assert report['geometry']['max_edge_stretch_ratio'] == 10
    assert 'MAX_EDGE_STRETCH_RATIO_EXCEEDED' in report['errors']


def test_displacement_bounds_are_not_silently_normalized():
    report = measure([(x+.25,y,z) for x,y,z in PLANE],thresholds={'max_displacement_m':.1})
    assert not report['passed']
    assert report['metrics']['max_displacement_m'] == .25


def test_dense_motion_derivatives_use_seconds_and_not_frame_units():
    previous = [(x+.1,y,z) for x,y,z in PLANE]
    current = [(x+.3,y,z) for x,y,z in PLANE]
    report = measure(current,previous,PLANE)
    assert report['passed']
    assert report['metrics']['max_velocity_m_s'] == pytest.approx(6)
    assert report['metrics']['max_acceleration_m_s2'] == pytest.approx(90)


def test_primary_reference_avoids_false_detachment_from_body_translation():
    moved = [(x+10.,y,z) for x,y,z in PLANE]
    report = motion_step(moved,moved,None,None,30.,EDGES,TRIANGLES,validate_thresholds())
    assert report['passed']
    assert report['metrics']['max_displacement_m'] == 0


def test_secondary_derivatives_remove_measured_primary_translation():
    old = PLANE
    previous_primary = [(x+.1,y,z) for x,y,z in PLANE]
    current_primary = [(x+.3,y,z) for x,y,z in PLANE]
    report = motion_step(current_primary,current_primary,previous_primary,old,30.,EDGES,TRIANGLES,
                         validate_thresholds(),previous_primary,old)
    assert report['metrics']['max_velocity_m_s'] == pytest.approx(6)
    assert report['metrics']['max_secondary_displacement_m'] == 0
    assert report['metrics']['max_secondary_velocity_m_s'] == 0
    assert report['metrics']['max_secondary_acceleration_m_s2'] == 0


def test_missing_secondary_history_is_unknown_and_not_zero_evidence():
    report = measure(PLANE)
    assert report['metrics']['max_secondary_velocity_m_s'] is None
    assert report['metrics']['max_secondary_acceleration_m_s2'] is None


def test_bad_primary_history_never_passes_secondary_metrics():
    report = motion_step(PLANE,PLANE,PLANE,None,30.,EDGES,TRIANGLES,
                         validate_thresholds(),PLANE[:-1],None)
    assert not report['passed']
    assert report['errors'] == ['PRIMARY_REFERENCE_HISTORY_INVALID']


@pytest.mark.parametrize('value', [-1,math.nan,math.inf,True,'1',None])
def test_invalid_proposed_threshold_rejected(value):
    with pytest.raises(ValueError,match='INVALID_QA_THRESHOLD'):
        validate_thresholds({'max_displacement_m':value})


def test_unknown_threshold_is_not_ignored():
    with pytest.raises(ValueError,match='unknown'):
        validate_thresholds({'max_disp_m':1})
    with pytest.raises(ValueError):
        validate_thresholds({'max_penetrating_vertices':1.2})


def test_default_thresholds_are_configurable_and_finite():
    report = validate_thresholds({'max_displacement_m':.3,'max_penetrating_vertices':4})
    assert report['max_displacement_m'] == .3
    assert report['max_penetrating_vertices'] == 4
    assert all(math.isfinite(v) for v in report.values())
