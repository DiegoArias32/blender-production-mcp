import asyncio
import math
from pathlib import Path
import pytest
from blender_mcp.production.contacts.algorithms import derivative,phase_label,smoothstep,joint_metrics,motion_derivatives
from blender_mcp.production.contacts.runtime import CONTACT_RUNTIME
from blender_mcp.production.contacts.correction_runtime import FOOT_RUNTIME
from blender_mcp.production.animation.bezier import BEZIER_RUNTIME
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.m3_transaction import BASE_RUNTIME,READ,EXECUTE,CHECK


@pytest.mark.parametrize('flags,expected',[
    ((True,False,0,0,None),'HEEL_STRIKE'),((True,True,0,0,None),'FOOT_FLAT'),
    ((True,True,0,0,'FOOT_FLAT'),'MID_STANCE'),((False,True,.2,0,None),'HEEL_RISE'),
    ((False,True,.2,.06,None),'TOE_OFF'),((False,False,0,0,None),'SWING')])
def test_phase_contact_evidence(flags,expected):
    assert phase_label(*flags)==expected


@pytest.mark.parametrize('speed',[0,.01,1,100,-100])
def test_constant_speed_has_no_acceleration(speed):
    frames=list(range(9));fps=30
    samples=[{'position':[speed*f/fps,0,0],'rotation':[1,0,0,0]} for f in frames]
    result=motion_derivatives(samples,frames,fps)
    assert result['max_speed_m_s']==pytest.approx(abs(speed))
    assert result['max_acceleration_m_s2']<1e-9


@pytest.mark.parametrize('times',[[1,1],[2,1],[1]])
def test_derivative_invalid_times(times):
    with pytest.raises(ValueError): derivative([[0],[1]],times)


@pytest.mark.parametrize('value,expected',[(-1,0),(0,0),(.5,.5),(1,1),(2,1)])
def test_quintic_release_bounded(value,expected):
    assert smoothstep(value)==pytest.approx(expected)


def test_quintic_release_zero_boundary_derivatives():
    h=1e-5
    assert smoothstep(h)/h<1e-7
    assert (1-smoothstep(1-h))/h<1e-7


def test_deliberate_sustained_fast_joint_motion_not_local_pop():
    samples=[]
    for angle in range(10,171,20):
        r=math.radians(angle)
        samples.append({'hip':[0,0,1],'knee':[0,0,0],'end':[math.sin(r),0,math.cos(r)]})
    assert not joint_metrics(samples,list(range(len(samples))),30)['pops']


def test_localized_joint_change_detected_with_measurements():
    samples=[]
    for angle in (90,90,90,90,140,90,90,90,90):
        r=math.radians(angle)
        samples.append({'hip':[0,0,1],'knee':[0,0,0],'end':[math.sin(r),0,math.cos(r)]})
    result=joint_metrics(samples,list(range(9)),30)
    assert len(result['pops'])==2
    assert all('angular_acceleration_deg_s2' in row['measurements'] for row in result['pops'])


def test_knee_plane_flip_detected_even_with_identical_joint_angle():
    rows=[{'hip':[0,0,2],'knee':[0,y,1],'end':[0,0,0]} for y in (.5,-.5,.5,-.5)]
    measured=joint_metrics(rows,[1,2,3,4],30)
    assert max(measured['steps'])<1e-6
    assert len(measured['pops'])==3
    assert measured['max_bend_plane_step_degrees']==pytest.approx(180)


def test_angular_axis_reversal_has_acceleration_at_constant_speed():
    rows=[]
    for angle in (0,10,0,10,0,10,0):
        half=math.radians(angle)/2
        rows.append({'position':[0,0,0],'rotation':[math.cos(half),0,0,math.sin(half)]})
    result=motion_derivatives(rows,list(range(7)),30)
    assert max(result['angular_velocity'])==pytest.approx(300)
    assert all(value>17000 for value in result['angular_acceleration'][2:])


@pytest.mark.parametrize('runtime',[FOOT_RUNTIME,BEZIER_RUNTIME],ids=['foot','bezier'])
@pytest.mark.parametrize('suffix',[READ,EXECUTE,CHECK],ids=['read','execute','check'])
def test_all_domain_payloads_use_unchanged_upstream_ast(runtime,suffix):
    assert len(build_code(BASE_RUNTIME+runtime+suffix,{}))<100000


M31_NAMES={'operation_status','asset_status','asset_register_master','asset_register_fixture',
    'asset_create_working_copy','cleanup_artifacts','mesh_signature','mesh_region_define','mesh_region_verify',
    'mesh_candidate_create','mesh_candidate_verify'}
M4_NAMES={'character_inspect','character_define_regions','character_lock_regions','character_validate_locked_regions',
    'character_analyze_silhouette','character_analyze_proportions','character_analyze_asymmetry','character_analyze_surface',
    'character_analyze_intersections','character_modeling_audit','character_redesign_plan','character_create_candidate',
    'character_adjust_shape','character_deformation_qa','character_render_multiview','character_compare_before_after',
    'character_capture_reference_views','character_capture_candidate_views','character_compare_reference',
    'character_redesign_session'}
M2_NAMES={'scene_audit','create_checkpoint','restore_checkpoint','diff_scene','inspect_armature','rig_analyze',
    'animation_analyze','animation_detect_foot_sliding','animation_detect_quaternion_flips','qa_capture_viewport','qa_render_preview',
    'animation_create_action','animation_duplicate_action','animation_compare_actions','animation_restore_action',
    'animation_analyze_contacts','animation_fix_foot_sliding','animation_fix_quaternion_flips','animation_analyze_fcurves',
    'animation_cleanup_fcurves','animation_analyze_root_motion','animation_prepare_root_motion','animation_retime',
    'animation_analyze_control_mode','animation_analyze_balance','animation_analyze_motion_quality','animation_refinement_plan',
    'qa_render_multiview','qa_compare_before_after'}


def test_all_historical_tools_retained_and_m3_output_schemas():
    from blender_mcp.production.server import mcp
    tools=asyncio.run(mcp.list_tools());names={t.name for t in tools}
    assert {'production.'+name for name in M2_NAMES}<=names
    # M3.1 adds tools; the 54 historical ones stay (asserted above).
    assert {'production.'+name for name in M31_NAMES}<=names
    assert {'production.'+name for name in M4_NAMES}<=names
    from test_m5_inventory import M5_NAMES
    assert {'production.'+name for name in M5_NAMES}<=names
    from blender_mcp.production.animation.generation.tools import NAMES as M6_NAMES
    from blender_mcp.production.facial.tools import NAMES as M7_NAMES
    assert {'production.'+name for name in M7_NAMES}<=names
    from blender_mcp.production.physics.tools import NAMES as M8_NAMES
    assert {'production.'+name for name in M8_NAMES}<=names
    from blender_mcp.production.materials.tools import NAMES as M9_NAMES
    assert len(M9_NAMES)==38
    assert {'production.'+name for name in M9_NAMES}<=names
    assert len(names)==54+len(M31_NAMES)+len(M4_NAMES)+len(M5_NAMES)+len(M6_NAMES)+len(M7_NAMES)+len(M8_NAMES)+len(M9_NAMES)
    assert all(t.outputSchema for t in tools)
    for t in tools:
        props=t.inputSchema['properties']
        assert '_operation_id' not in props
        if 'mode' in props: assert props['mode']['default']=='analyze'
