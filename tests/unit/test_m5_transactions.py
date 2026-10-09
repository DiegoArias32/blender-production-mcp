"""M5 guards: failure cannot promote a candidate; uncertain mutations never replay."""
import json
import pytest
from blender_mcp.production.common import m5
from blender_mcp.production.common.bridge import build_code,payload_bytes
from blender_mcp.production.common.ledger import OperationLedger
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.common.transport import BlenderTimeoutError

def test_signature_payload_passes_unchanged_safe_mode():
    body=m5.M5_RUNTIME+'\nresult=m5_signature(m5_obj(params["name"]))'
    assert build_code(body,{'name':'Generated'})
    assert payload_bytes(body,{'name':'Generated'})<200000

def test_owned_candidate_name_is_bounded_and_never_renames_source():
    source='á'*100+'__M5_old'
    a=m5.candidate_name(source,'operation-A');b=m5.candidate_name(source,'operation-B')
    assert a!=b and len(a.encode('utf8'))<64 and source=='á'*100+'__M5_old'

RUNTIME='''
def m5_plan(high,low,options,relation):return {'supported':True,'strategy':'CONTROLLED_TEST'}
def m5_apply(high,low,candidate,proposal,options):return {}
def m5_verify(high,low,candidate,proposal,options):return {'passed':True,'metrics':{},'errors':[]}
'''

@pytest.fixture
def cycle(tmp_path,monkeypatch):
    store=Storage(tmp_path/'s');high={'object':'H','fingerprint':'high','geometry_fingerprint':'G','counts':{}}
    low={'object':'L','fingerprint':'low','geometry_fingerprint':'G','counts':{}}
    monkeypatch.setattr(m5,'require_mutable_asset',lambda *a:{'asset_id':'fixture'})
    monkeypatch.setattr(m5,'current_asset',lambda *a:{'asset_id':'fixture'})
    path,relation=m5._relation(store,'pair',high,low,{'asset_id':'fixture'})
    monkeypatch.setattr(m5,'validate_relationship',lambda *a:relation)
    monkeypatch.setattr(m5,'signature',lambda bridge,name:high if name=='H' else low)
    from blender_mcp.production import checkpoint
    monkeypatch.setattr(checkpoint,'create_checkpoint',lambda *a,**kw:{'manifest':'fixture-checkpoint'})
    class Bridge:
        calls=0;fail=False;timeout=False;race=False
        def run(self,body,params=None):
            if 'target_signature' in body:return {'proposal':{'supported':True},'target_signature':low,'asset_context':{'filepath':'fixture.blend','stamps':[]}}
            if 'changes=m5_apply' in body:
                assert params['expected_high_fingerprint']=='high' and params['expected_low_fingerprint']=='low'
                assert params['expected_asset_context']=={'filepath':'fixture.blend','stamps':[]}
                # Execute only the atomic guard with small fake Blender objects. It must
                # reject a concurrent edit before m5_copy can create anything.
                if self.race:
                    guard=body[body.index('high=m5_obj(params'):body.index('changes=m5_apply')]
                    ns={'params':params,'m5_obj':lambda name:name,
                        'm5_signature':lambda obj:dict(high if obj=='H' else low,fingerprint='concurrent_edit'),
                        'm5_copy':lambda *a:pytest.fail('stale scene created a candidate')}
                    exec(guard,ns)
                self.calls+=1
                if self.timeout:raise BlenderTimeoutError('lost answer after executing')
                return {'candidate':'C','signature':dict(low,object='C',fingerprint='candidate'),
                    'changes':{},'verification':{'passed':not self.fail,'metrics':{},'errors':['failure'] if self.fail else []},'source_intact':True}
            raise AssertionError(body[-100:])
    bridge=Bridge()
    op=m5.transaction(bridge,store,'proposal','test',RUNTIME,path,'propose')['operation']
    return bridge,store,op,path

def test_failed_verification_keeps_previous_low(cycle):
    bridge,store,op,path=cycle;bridge.fail=True
    result=m5.transaction(bridge,store,'apply','test',RUNTIME,mode='apply',operation=op)
    assert result['numeric_verification']=='fail' and result['relationship']==path
    replay=m5.transaction(bridge,store,'retry','test',RUNTIME,mode='apply',operation=op)
    assert replay['numeric_verification']=='fail' and replay['idempotency']['replayed'] and bridge.calls==1

def test_timeout_after_execution_never_mutates_twice(cycle):
    bridge,store,op,path=cycle;bridge.timeout=True
    with pytest.raises(BlenderTimeoutError):m5.transaction(bridge,store,'apply','test',RUNTIME,mode='apply',operation=op)
    with pytest.raises(ValueError,match='OPERATION_OUTCOME_UNCERTAIN'):
        m5.transaction(bridge,store,'retry','test',RUNTIME,mode='apply',operation=op)
    assert bridge.calls==1

def test_frozen_proposal_detects_tampering_before_mutation(cycle):
    bridge,store,op,path=cycle
    from pathlib import Path
    p=Path(op);d=json.loads(p.read_text());d['options']['target_triangles']=1;p.write_text(json.dumps(d))
    with pytest.raises(ValueError,match='STALE_PROPOSAL'):
        m5.transaction(bridge,store,'apply','test',RUNTIME,mode='apply',operation=op)
    assert bridge.calls==0

def test_no_parameter_replacement_on_apply(cycle):
    bridge,store,op,path=cycle
    with pytest.raises(ValueError,match='FROZEN_OPTIONS'):
        m5.transaction(bridge,store,'apply','test',RUNTIME,mode='apply',operation=op,options={'target_triangles':1})
    assert bridge.calls==0

def test_concurrent_edit_between_checkpoint_and_execute_is_rejected(cycle):
    bridge,store,op,path=cycle;bridge.race=True
    with pytest.raises(ValueError,match='STALE_PROPOSAL'):
        m5.transaction(bridge,store,'race','test',RUNTIME,mode='apply',operation=op)
    assert bridge.calls==0

def test_asset_context_guard_rejects_switch_to_another_file():
    from types import SimpleNamespace
    start=m5.M5_RUNTIME.index('def m5_asset_context():');end=m5.M5_RUNTIME.index('def m5_buffer(')
    ns={'bpy':SimpleNamespace(data=SimpleNamespace(filepath='MASTER.blend',scenes=[]))}
    exec(m5.M5_RUNTIME[start:end],ns)
    with pytest.raises(ValueError,match='ASSET_CONTEXT_CHANGED'):
        ns['m5_guard_asset_context']({'filepath':'fixture.blend','stamps':[]})
