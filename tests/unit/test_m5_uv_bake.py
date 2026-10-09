"""Independent UV geometry invariants and strict policy contracts."""
import math
import pytest
from blender_mcp.production.uv.algorithms import signed_area,triangle_overlap,tile_of,shelf_pack
from blender_mcp.production.uv.service import validate
from blender_mcp.production.bake.service import validate as validate_bake


def test_triangle_overlap_shared_edge_is_zero():
    a=[(0,0),(1,0),(0,1)];b=[(1,0),(1,1),(0,1)]
    assert triangle_overlap(a,b)==pytest.approx(0,abs=1e-12)


def test_containment_exact_area_and_orientation():
    a=[(0,0),(1,0),(0,1)];b=[(.1,.1),(.3,.1),(.1,.3)]
    assert triangle_overlap(a,b)==pytest.approx(.02)
    assert triangle_overlap(list(reversed(a)),b)==pytest.approx(.02)
    assert signed_area(list(reversed(a)))==pytest.approx(-.5)


def test_disjoint_overlap_zero():
    assert triangle_overlap([(0,0),(1,0),(0,1)],[(2,2),(3,2),(2,3)])==0


def test_tile_boundaries_use_centroid():
    assert tile_of([(1,0),(2,0),(1,1)])==1002
    assert tile_of([(1,1),(2,1),(1,2)])==1012
    assert tile_of([(.9,0),(1.1,0),(.9,.2)]) is None
    assert tile_of([(-.1,0),(0,0),(-.1,1)]) is None


def test_shelf_pack_reproducible_nonoverlapping_with_padding():
    boxes={1:(2,1),2:(1,1),3:(.5,2)};positions,scale=shelf_pack(boxes,.01)
    assert shelf_pack(boxes,.01)==(positions,scale)
    for i,a in positions.items():
        aw,ah=[c*scale for c in boxes[i]]
        assert a['x']>=.01 and a['y']>=.01
        assert a['x']+aw<=.990001 and a['y']+ah<=.990001
        for j,b in positions.items():
            if i>=j:continue
            bw,bh=[c*scale for c in boxes[j]]
            assert a['x']+aw+.009999<=b['x'] or b['x']+bw+.009999<=a['x'] or a['y']+ah+.009999<=b['y'] or b['y']+bh+.009999<=a['y']


@pytest.mark.parametrize('options',[{'allow_overlap':1},{'resolution':True},{'padding_px':float('nan')},{'unknown':1},{'padding_px':64,'resolution':32},{'expected_tiles':[999]}])
def test_uv_invalid_policy_rejected(options):
    with pytest.raises(ValueError):validate(options)


@pytest.mark.parametrize('row',[{'region':'head','tile':999,'material_index':0},{'region':'head','tile':1001,'faces':[True]},
    {'region':'head','tile':1001,'faces':[0],'material_index':0},{'tile':1001,'material_index':0}])
def test_udim_assignment_contract_rejects_ambiguity(row):
    with pytest.raises(ValueError):validate({'assignments':[row]})


def test_udim_custom_tiles_and_density_resolution():
    result=validate({'assignments':[{'region':'face','tile':1011,'material_index':0}], 'tile_resolutions':{'1011':4096}})
    assert result['assignments'][0]['tile']==1011
    assert result['tile_resolutions']['1011']==4096


@pytest.mark.parametrize('options',[{'maps':['NORMAL','NORMAL']},{'maps':['CURVATURE']},{'allow_cpu_fallback':1},{'front_distance_m':-1},
    {'extrusion_m':float('inf')},{'smoke_resolution':1024},{'normal_convention':'UNKNOWN'},{'tiles':[999]}])
def test_bake_invalid_options_rejected(options):
    with pytest.raises(ValueError):validate_bake(options)


def test_bake_finite_configurable_distances():
    options=validate_bake({'front_distance_m':.004,'rear_distance_m':.002,'extrusion_m':.003,'normal_convention':'DIRECTX','maps':['NORMAL']})
    assert options['front_distance_m']==.004
    assert options['normal_convention']=='DIRECTX'


def test_explicit_smart_project_contract_no_fallback():
    assert validate({'unwrap_method':'SMART_PROJECT'})['unwrap_method']=='SMART_PROJECT'
    with pytest.raises(ValueError):validate({'unwrap_method':'AUTO_FALLBACK'})
