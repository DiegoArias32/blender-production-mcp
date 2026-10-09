"""Safety contracts for new-domain operations, independent of numerical backends."""
import json
from pathlib import Path
import pytest
from blender_mcp.production.common.m3_transaction import transaction,BASE_RUNTIME,READ,PREFLIGHT,EXECUTE,CHECK,_public
from blender_mcp.production.common.operations import save_proposal,fingerprint
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.rig.ik_fk import IKFK_RUNTIME
from blender_mcp.production.rig.semantics import SEMANTIC_RUNTIME,analyze


class NoCalls:
    def run(self,*args):
        raise AssertionError('Unsafe request reached Blender')


@pytest.mark.parametrize('mode',['preview','apply','verify','revert'])
def test_m3_requires_frozen_proposal_before_any_blender_mutation(tmp_path,mode):
    with pytest.raises(ValueError,match='Immutable'):
        transaction(NoCalls(),Storage(tmp_path),'id','test','',mode=mode)


@pytest.mark.parametrize('script',[READ,PREFLIGHT,EXECUTE,CHECK])
def test_rig_payload_safe_mode_preserved(script):
    body=build_code(BASE_RUNTIME+IKFK_RUNTIME+script,{'object_name':'Quotes" and \\ names remain data'})
    assert len(body)<100000


@pytest.mark.parametrize('mode',['preview','apply','verify','revert'])
def test_m3_unsupported_cannot_be_forced(tmp_path,mode):
    store=Storage(tmp_path)
    path=save_proposal(store,'proposal',{'kind':'test','object_name':'Unknown','source_action':None,'proposal':{'supported':False}})
    with pytest.raises(ValueError,match='unsupported_or_ambiguous'):
        transaction(NoCalls(),store,'id','test','',mode=mode,operation=path)


def test_m3_parameters_are_frozen(tmp_path):
    with pytest.raises(ValueError,match='empty'):
        transaction(NoCalls(),Storage(tmp_path),'id','test','',mode='apply',options={'tolerance':100},operation='anything')


def test_m3_stale_has_no_checkpoint_or_duplicate(tmp_path):
    store=Storage(tmp_path)
    path=save_proposal(store,'proposal',{'kind':'test','object_name':'Rig','source_action':None,'proposal':{'supported':True},
        'transaction_schema':1,'backend_fingerprint':fingerprint(BASE_RUNTIME)})
    class Stale:
        def run(self,*args):
            return {'matches':False}
    with pytest.raises(ValueError,match='stale'):
        transaction(Stale(),store,'id','test','',mode='apply',operation=path)
    assert not (store.root/'checkpoints').exists()


def test_m3_proposal_mismatch_cannot_reuse_other_backend(tmp_path):
    store=Storage(tmp_path)
    path=save_proposal(store,'proposal',{'kind':'retarget','object_name':'Rig','source_action':None,'proposal':{'supported':True}})
    with pytest.raises(ValueError,match='mismatch'):
        transaction(NoCalls(),store,'id','rigmatch','',mode='apply',operation=path)


def test_m3_backend_changes_invalidate_saved_operation(tmp_path):
    store=Storage(tmp_path)
    path=save_proposal(store,'proposal',{'kind':'test','object_name':'Rig','source_action':None,'proposal':{'supported':True},
        'transaction_schema':1,'backend_fingerprint':fingerprint(BASE_RUNTIME+'old backend')})
    with pytest.raises(ValueError,match='backend changed'):
        transaction(NoCalls(),store,'id','test','new backend',mode='apply',operation=path)


def test_m3_snapshots_and_tracks_stay_local():
    public=_public({'source_snapshot':{'geometry':[1]*5000},'working_snapshot':{},'context':{},'source_state':{},
        'proposal':{'supported':True,'private':{'tracks':[1]*10000},'samples':[1]*10000,'frames':[1,2]}})
    assert public=={'proposal':{'supported':True}}


@pytest.mark.parametrize('options',[{'limit':0},{'limit':501},{'offset':-1},{'unknown':True},{'limit':True}])
def test_rig_analysis_validates_pagination_before_blender(options):
    with pytest.raises(ValueError):
        analyze(NoCalls(),'Rig',options)


@pytest.mark.parametrize('bad',[float('inf'),float('nan'),float('-inf')])
def test_m3_nonfinite_nested_options_fail_before_blender(tmp_path,bad):
    with pytest.raises(ValueError,match='finite'):
        transaction(NoCalls(),Storage(tmp_path),'id','test','',object_name='Rig',options={'mapping':{'tolerance':bad}})


def test_semantics_runtime_respects_upstream_safety():
    build_code(BASE_RUNTIME+SEMANTIC_RUNTIME+"\nresult=m3_semantic_model(m2_object(params['name']),{})",{'name':'Rig'})
