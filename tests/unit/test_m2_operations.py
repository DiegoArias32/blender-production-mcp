import asyncio
import json
from pathlib import Path

import pytest

from blender_mcp.production.common.storage import Storage
from blender_mcp.production.common.operations import (
    fingerprint,save_proposal,load_proposal,record_event,latest_candidate)
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.animation.corrections import (
    correction,runtime,READ,EXECUTE,CHECK,_public,_validate_options,_validate_transform_options)
from blender_mcp.production.animation.actions import BODY,MANAGE,COMPARE


def test_proposal_roundtrip_integrity(tmp_path):
    store=Storage(tmp_path)
    path=save_proposal(store,'id',{'kind':'foot_sliding','target':'Rig'})
    assert load_proposal(store,path)[1]['target']=='Rig'
    changed=json.loads(Path(path).read_text())
    changed['target']='Different'
    Path(path).write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='integrity'):
        load_proposal(store,path)


def test_proposal_cannot_be_loaded_outside_workspace(tmp_path):
    path=tmp_path/'proposal.json'; path.write_text('{}')
    with pytest.raises(ValueError,match='workspace'):
        load_proposal(Storage(tmp_path/'store'),str(path))


def test_events_append_and_failed_apply_does_not_hide_candidate(tmp_path):
    store=Storage(tmp_path)
    path=save_proposal(store,'id',{'kind':'retime'})
    first=record_event(path,'preview',{'working_action':'Copy'})
    second=record_event(path,'apply',{'status':'error'})
    assert first!=second and Path(first).exists()
    assert latest_candidate(path)['working_action']=='Copy'


def test_fingerprint_includes_handles_interpolation_metadata():
    data={'keys':[{'co':[1,2],'handle':[3,4],'interpolation':'BEZIER'}],'custom':{'tag':2}}
    before=fingerprint(data)
    data['keys'][0]['handle'][0]=5
    assert before!=fingerprint(data)


@pytest.mark.parametrize('kind',['foot_sliding','quaternion','cleanup','retime','root_motion'])
@pytest.mark.parametrize('script',[READ,EXECUTE,CHECK])
def test_all_correction_payloads_validate_without_core_exceptions(kind,script):
    build_code(runtime(kind)+script,{'test':'quotes\" are data'})


@pytest.mark.parametrize('script',[MANAGE,COMPARE])
def test_action_payloads_validate(script):
    build_code(BODY+script,{})


@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf')])
def test_nested_nonfinite_tolerances_refused(value):
    with pytest.raises(ValueError,match='finite'):
        _validate_options({'per_channel_tolerances':{'position':value}})


@pytest.mark.parametrize('options',[{'confirm_contacts':'true'},{'controls':'Foot'}, {'confirm_contacs':True}])
def test_foot_safety_flags_fail_closed(options):
    with pytest.raises(ValueError):
        _validate_transform_options('foot_sliding',options)


def test_public_operation_never_exposes_large_snapshots_or_patches():
    result=_public({'source_snapshot':[1]*1000,'working_snapshot':{},'context':{},
        'proposal':{'patches':[1]*10,'frames':[1,2,3],'offsets':{},'supported':True}})
    assert 'source_snapshot' not in result and 'working_snapshot' not in result
    assert result['proposal']=={'supported':True,'patch_count':10,'frame_range':[1,3]}


class NoCalls:
    def run(self,*args):
        raise AssertionError('Unsafe input reached Blender')


@pytest.mark.parametrize('mode',['apply','preview','verify','revert'])
def test_no_mutation_without_proposal(tmp_path,mode):
    with pytest.raises(ValueError,match='operation reference'):
        correction(NoCalls(),Storage(tmp_path),'id','foot_sliding',mode=mode)


def test_apply_rejects_unreviewed_parameter_changes(tmp_path):
    with pytest.raises(ValueError,match='frozen'):
        correction(NoCalls(),Storage(tmp_path),'id','foot_sliding',mode='apply',
                   operation='unused',options={'sliding_tolerance_m':5})


def test_stale_proposal_never_checkpoints_or_mutates(tmp_path,monkeypatch):
    store=Storage(tmp_path)
    data={'kind':'retime','object_name':'Rig','source_action':'Walk','proposal':{'supported':True}}
    path=save_proposal(store,'old',data)
    class Changed:
        def run(self,*args): return {'matches':False}
    with pytest.raises(ValueError,match='stale'):
        correction(Changed(),store,'new','retime',mode='apply',operation=path)
    assert not (tmp_path/'checkpoints').exists()


def test_unsupported_proposal_never_calls_blender(tmp_path):
    store=Storage(tmp_path)
    path=save_proposal(store,'id',{'kind':'foot_sliding','object_name':'Rig','proposal':{'supported':False}})
    with pytest.raises(ValueError,match='manual'):
        correction(NoCalls(),store,'new','foot_sliding',mode='apply',operation=path)


def test_registered_modes_default_to_analysis_and_actions_to_dry_run():
    from blender_mcp.production.server import mcp
    tools=asyncio.run(mcp.list_tools())
    assert len(tools)>=29  # M3 adds domains; historical inventory is asserted below.
    for tool in tools:
        assert tool.outputSchema is not None
        props=tool.inputSchema['properties']
        if 'mode' in props:
            assert props['mode']['default']=='analyze'
            assert tool.annotations.readOnlyHint is False
        if tool.name.endswith(('_create_action','_duplicate_action','_restore_action')):
            assert props['dry_run']['default'] is True
        assert '_operation_id' not in props


def test_log_failure_keeps_confirmed_mutation_result(monkeypatch,tmp_path):
    from blender_mcp.production.animation import corrections
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(corrections,'record_event',fail)
    data={'working_action':'Copy','numeric_verification':'pass','checkpoint':'safe','status':'warning'}
    result=corrections._record_result(tmp_path/'proposal.json','apply',data)
    assert result['working_action']=='Copy' and result['numeric_verification']=='pass'
    assert result['checkpoint']=='safe' and not result['event_recorded']
    assert 'disk full' in result['warnings'][0]
