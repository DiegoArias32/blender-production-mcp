import pytest

from blender_mcp.production.animation.generation.pose_policy import validate_unwritten_pose, POSE_BASELINE_RUNTIME
from blender_mcp.production.common.bridge import build_code


def channel(path='bone.scale', values=None, neutral=None):
    return {'path':path,'values':values or [1,1,1],'neutral':neutral or [1,1,1]}


def curve(path='bone.scale', values=(1,1), interpolation='LINEAR', handles=None):
    return {'path':path,'index':0,'extrapolation':'CONSTANT','points':[
        {'value':value,'left':value if handles is None else handles[index][0],
         'right':value if handles is None else handles[index][1],'interpolation':interpolation}
        for index,value in enumerate(values)]}


def test_neutral_extra_channels_are_supported_without_authoring_them():
    result=validate_unwritten_pose([channel()], [curve()], set())
    assert result['passed'] and result['issue_count']==0


def test_actual_authored_path_can_have_non_neutral_source_animation():
    path='bone.rotation_quaternion'
    result=validate_unwritten_pose([channel(path,[.8,.6,0,0],[1,0,0,0])],
                                  [curve(path,(.8,.2))],{path})
    assert result['passed']


@pytest.mark.parametrize('path,values,expected',[
    ('extra.location',[.01,0,0],[0,0,0]),
    ('pole.location',[0,.01,0],[0,0,0]),
    ('extra.rotation_quaternion',[.99,.1,0,0],[1,0,0,0])])
def test_manual_unwritten_channels_reject_without_a_source_action(path,values,expected):
    result=validate_unwritten_pose([channel(path,values,expected)],[],set())
    assert not result['passed']
    assert result['issues'][0]['reason']=='NON_NEUTRAL_CURRENT_POSE'


def test_neutral_unmapped_location_and_rotation_remain_supported():
    rows=[channel('extra.location',[0,0,0],[0,0,0]),
          channel('extra.rotation_quaternion',[1,0,0,0],[1,0,0,0])]
    assert validate_unwritten_pose(rows,[curve('extra.location',(0,0))],set())['passed']


@pytest.mark.parametrize('value',[1.02,float('nan'),float('inf'),True])
def test_unwritten_current_scale_requires_finite_neutral_components(value):
    result=validate_unwritten_pose([channel(values=[value,1,1])],[],set())
    assert not result['passed']
    assert result['issues'][0]['reason']=='NON_NEUTRAL_CURRENT_POSE'


@pytest.mark.parametrize('interpolation',['LINEAR','CONSTANT','BEZIER'])
def test_non_neutral_unwritten_source_key_rejects_even_when_current_pose_is_neutral(interpolation):
    result=validate_unwritten_pose([channel()],[curve(values=(1,1.04),interpolation=interpolation)],set())
    assert not result['passed']
    assert result['issues'][0]['reason']=='NON_NEUTRAL_SOURCE_KEY'


def test_bezier_overshoot_between_neutral_unwritten_keys_rejects():
    result=validate_unwritten_pose([channel()],[curve(interpolation='BEZIER',handles=((1,1.1),(1,1)))],set())
    assert not result['passed']
    assert result['issues'][0]['reason']=='NON_NEUTRAL_SOURCE_TANGENT'


def test_incoming_bezier_handle_uses_previous_key_interpolation():
    source=curve(handles=((1,1),(1.1,1)))
    source['points'][0]['interpolation']='BEZIER'
    result=validate_unwritten_pose([channel()],[source],set())
    assert not result['passed']


def test_inactive_rotation_storage_is_not_an_evaluated_channel():
    # Blender passes only the active rotation representation to this policy.
    result=validate_unwritten_pose([channel()], [curve('inactive.rotation_euler',(0,1))],set())
    assert result['passed']


def test_issue_output_is_bounded_but_reports_total_failure_count():
    rows=[channel('bone'+str(index)+'.scale',[2,2,2]) for index in range(40)]
    result=validate_unwritten_pose(rows,[],set(),limit=4)
    assert not result['passed'] and result['issue_count']==120
    assert len(result['issues'])==4 and result['truncated']


def test_policy_payload_obeys_upstream_safe_mode():
    code=build_code(POSE_BASELINE_RUNTIME+'\nresult=validate_unwritten_pose(params["channels"],params["curves"],params["written"])\n',
                    {'channels':[channel()],'curves':[curve()],'written':[]})
    namespace={}
    exec(code.rsplit('print(',1)[0],namespace)
    assert namespace['result']['passed']
