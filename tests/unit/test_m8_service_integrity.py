"""Actual lifecycle/store/cache code with explicit raw vs evaluated Blender doubles.

These tests qualify transactions and tamper detection, not simulation or visual
quality. The Blender double hashes its data independently of service expectations.
"""
from copy import deepcopy
import array
import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from blender_mcp.production.physics import service, cache, integrity, model, procedural, tools
from blender_mcp.production.animation.generation import store
from blender_mcp.production.common.digest import runtime_digest
from blender_mcp.production.common.operations import fingerprint
from blender_mcp.production.common.storage import Storage, sha256, write_json
from blender_mcp.production.common.transport import BlenderTimeoutError


class BlenderState:
    """Raw authored data differs from transient/evaluated pose and shape values."""
    def __init__(self):
        self.raw={'mesh':{'co':[[0.,0.,0.],[1.,0.,0.],[0.,0.,1.]],'faces':[[0,1,2]],'weights':[1.,1.,1.]},
            'body_action':{'slot':'OBJECT','keys':[[1.,0.],[31.,.1]],'handle_right':[[2.,0.],[32.,.1]]},
            'face_action':{'slot':'KEY','keys':[[1.,0.],[31.,1.]],'nla':{'scale':1.,'influence':1.}},
            'context':{'fps':30,'gravity':[0.,0.,-9.81],'unit_scale':1.}}
        self.evaluated={'unkeyed_primary_bone':0.,'static_shape':0.}
        self.objects={'Candidate_v1':{'mesh':deepcopy(self.raw['mesh']),'settings':{'mass':1.,'damping':8.},
            'secondary_action':{'keys':[[1.,0.],[31.,.05]]},'hidden':True,'version':1}}
        self.collections={'Candidate_v1':['Candidate_v1']}
        self.calls=[]

    def source(self):
        return {'fingerprint':runtime_digest(self.raw),'source':{'type':'MESH','vertices':3}}

    def primary(self,frames):
        rows=[{'frame':f,'evaluation':deepcopy(self.evaluated)} for f in frames]
        return {'fingerprint':runtime_digest(rows),'frames':rows,'state_restored':True}

    def signature(self,name):
        raw=runtime_digest({'object':self.objects[name],'source_dependencies':self.raw})
        return {'raw_fingerprint':raw,'fingerprint':runtime_digest({'raw':raw,'collection':self.collections[name]})}

    def run(self,body,params):
        self.calls.append((body,deepcopy(params)))
        if body==integrity.STATUS:
            return self.source()
        if body==integrity.PRIMARY:
            return self.primary(params['frames'])
        if body==integrity.CANDIDATE_STATUS:
            return self.signature(params['candidate_object'])
        if body.endswith('result=pb_clone(params)\n'):
            native=params['candidate_name'];name=params['baked_name']
            self.objects[name]=deepcopy(self.objects[native]);self.objects[name]['bake_of']=native
            self.collections[native].append(name);self.collections[name]=self.collections[native]
            return {'candidate_object':name,'candidate_rig':None,'generation_id':params['generation_id'],
                    'source_map':{name:params['source_object']}}
        if body.endswith('result=pc_playback(params)\n'):
            obj=self.objects[params['candidate_name']]
            obj['playback']={'filepath':params['filepath'],'frame_start':params['frame_start'],'scale':1.}
            return {'configured':True}
        if body.endswith('result=pq_bake_compare(params)\n'):
            # Geometry equivalence is supplied as a backend measurement, not a visual verdict.
            return {'passed':True,'max_error_m':0.,'frames':[{'frame':f,'passed':True} for f in params['frames']]}
        if body==procedural.MARK:
            for name in params['owned_objects']:
                self.objects[name]['hidden']=not params['enabled']
            return {'state':params['state'],'original_visibility_changed':False}
        raise AssertionError('Unexpected bridge payload in transaction test')


@pytest.fixture
def scenario(tmp_path,monkeypatch):
    storage=Storage(tmp_path/'store');bridge=BlenderState()
    brief=model.create_brief('Controlled accessory',{'subsystem':'ACCESSORY','duration_s':1,'fps':30})
    monkeypatch.setattr(service,'backend_fingerprint',lambda:'tested-backend')
    params={'source_object':'Source','primary_objects':['BodyRig','Face'],'colliders':['Body'],
            'anchor_object':'BodyRig','anchor_bone':'Chest'}
    data={'object_name':'Source','params':params,'brief':brief,'supported':True,
        'source_status':bridge.source(),'sample_frames':[1,16,31],
        'primary_snapshot':bridge.primary([1,16,31]),'backend_fingerprint':'tested-backend',
        'capacity':{'estimated_cache_bytes':1148}}
    reference,plan=store.save(storage,'integrity-plan',data,service.CATEGORY,service.STATES)
    path=Path(reference);signature=bridge.signature('Candidate_v1')
    state={'state':'REVIEWED','candidate':{'candidate_object':'Candidate_v1','candidate_rig':None,
        'collection':'Owned_v1','source_map':{'Candidate_v1':'Source'}},'candidate_fingerprint':signature['fingerprint'],
        'candidate_raw_fingerprint':signature['raw_fingerprint'],'version':1,'last_version':1,'refinement_cycles':0,
        'current_settings':deepcopy(brief['settings']), 'colliders':{'collider_names':[]},
        'technical_qa':{'passed':True,'metrics':{'max_velocity_m_s':1.}},
        'review':{'record':{'decision':'ACCEPT'}},'cache':None,'baked_candidate':None}
    service.event(path,state)
    return NS(storage=storage,bridge=bridge,plan=plan,path=path,state=state)


def pc2(s):
    frames=[]
    for frame in range(1,32):
        coordinates=array.array('f',[0.,0.,0.,1.,0.,0.,0.,0.,1.])
        frames.append({'frame':frame,'vertex_count':3,'hex':coordinates.tobytes().hex()})
    record=cache.write_pc2(s.storage,'integrity-cache',frames,service.cache_dependencies(s.plan,s.state),1)
    s.state['cache']=record;service.event(s.path,s.state)
    return record


def test_fresh_roundtrip_keeps_source_primary_candidate_scope(scenario):
    s=scenario;reference=str(s.path);pc2(s)
    path,plan,state=service.load(Storage(s.storage.root),reference)
    assert service.fresh(s.bridge,s.storage,plan,state)['fingerprint']==s.plan['source_status']['fingerprint']
    assert any(body==integrity.PRIMARY for body,params in s.bridge.calls)
    assert path==s.path


@pytest.mark.parametrize('change',['mesh','body_key','body_handle','face_key','face_nla','fps'])
def test_source_or_primary_action_changes_reject_before_candidate_mutation(scenario,change):
    s=scenario
    if change=='mesh':s.bridge.raw['mesh']['co'][0][0]=.01
    elif change=='body_key':s.bridge.raw['body_action']['keys'][0][1]=.01
    elif change=='body_handle':s.bridge.raw['body_action']['handle_right'][0][1]=.01
    elif change=='face_key':s.bridge.raw['face_action']['keys'][0][1]=.01
    elif change=='face_nla':s.bridge.raw['face_action']['nla']['scale']=2.
    else:s.bridge.raw['context']['fps']=60
    with pytest.raises(ValueError,match='PHYSICS_STALE_SOURCE'):
        service.fresh(s.bridge,s.storage,s.plan,s.state)
    assert all(body==integrity.STATUS for body,params in s.bridge.calls)


@pytest.mark.parametrize('property',['unkeyed_primary_bone','static_shape'])
def test_evaluated_primary_change_blocks_even_with_identical_raw_signatures(scenario,property):
    s=scenario;before=s.bridge.source()['fingerprint'];native=s.bridge.signature('Candidate_v1')
    s.bridge.evaluated[property]=.3
    assert s.bridge.source()['fingerprint']==before and s.bridge.signature('Candidate_v1')==native
    with pytest.raises(ValueError,match='PHYSICS_STALE_PRIMARY'):
        service.fresh(s.bridge,s.storage,s.plan,s.state)


@pytest.mark.parametrize('change',['geometry','settings','motion','membership'])
def test_native_candidate_edits_reject(scenario,change):
    s=scenario;obj=s.bridge.objects['Candidate_v1']
    if change=='geometry':obj['mesh']['co'][0][0]=.05
    elif change=='settings':obj['settings']['damping']=24.
    elif change=='motion':obj['secondary_action']['keys'][0][1]=.01
    else:s.bridge.collections['Candidate_v1'].append('ForeignObject')
    with pytest.raises(ValueError,match='PHYSICS_STALE_CANDIDATE'):
        service.fresh(s.bridge,s.storage,s.plan,s.state)


def test_cache_byte_tamper_rejects_after_restart(scenario):
    s=scenario;record=pc2(s);path=Path(record['path']);data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
    _,plan,state=service.load(Storage(s.storage.root),str(s.path))
    with pytest.raises(ValueError,match='STALE_CACHE'):
        service.fresh(s.bridge,s.storage,plan,state)


def test_cache_manifest_tamper_rejects_after_restart(scenario):
    s=scenario;record=pc2(s);path=Path(record['manifest']);metadata=json.loads(path.read_text());metadata['frame_start']=2
    path.write_text(json.dumps(metadata),encoding='utf-8')
    _,plan,state=service.load(Storage(s.storage.root),str(s.path))
    with pytest.raises(ValueError,match='STALE_CACHE'):
        service.fresh(s.bridge,s.storage,plan,state)


def test_cache_dependencies_sign_evaluated_primary_but_not_bookkeeping_membership(scenario):
    s=scenario;before=service.cache_dependencies(s.plan,s.state)
    assert before['evaluated_primary']==s.plan['primary_snapshot']['fingerprint']
    s.state['candidate_fingerprint']='collection membership changed after an authorized clone'
    assert service.cache_dependencies(s.plan,s.state)==before
    s.state['candidate_raw_fingerprint']='native motion changed'
    assert service.cache_dependencies(s.plan,s.state)!=before


def test_bake_clone_refreshes_native_and_baked_signatures_without_invalidating_cache(scenario,monkeypatch):
    s=scenario;record=pc2(s);original=deepcopy(s.bridge.raw);old_fp=s.state['candidate_fingerprint']
    monkeypatch.setattr(service,'checkpoint',lambda *args:'owned-checkpoint')
    state=service.bake_secondary(s.bridge,s.storage,s.path,s.plan,s.state,'integrity-bake')
    assert state['candidate_fingerprint']!=old_fp
    assert state['candidate_raw_fingerprint']==record['dependencies']['candidate']
    assert state['baked_fingerprint']==s.bridge.signature(state['baked_candidate']['candidate_object'])['fingerprint']
    assert service.fresh(s.bridge,s.storage,s.plan,state)
    assert cache.verify(s.storage,state['cache'],service.cache_dependencies(s.plan,state))['status']=='FRESH'
    assert s.bridge.raw==original
    _,plan,restarted=service.load(Storage(s.storage.root),str(s.path))
    assert service.fresh(s.bridge,s.storage,plan,restarted)


@pytest.mark.parametrize('change',['geometry','playback'])
def test_baked_candidate_tamper_rejects_after_restart(scenario,monkeypatch,change):
    s=scenario;pc2(s);monkeypatch.setattr(service,'checkpoint',lambda *args:'owned-checkpoint')
    state=service.bake_secondary(s.bridge,s.storage,s.path,s.plan,s.state,'integrity-bake')
    obj=s.bridge.objects[state['baked_candidate']['candidate_object']]
    if change=='geometry':obj['mesh']['co'][0][0]=.01
    else:obj['playback']['frame_start']=2
    _,plan,restarted=service.load(Storage(s.storage.root),str(s.path))
    with pytest.raises(ValueError,match='PHYSICS_STALE_BAKED_CANDIDATE'):
        service.fresh(s.bridge,s.storage,plan,restarted)


def test_original_chain_and_primary_object_scope_are_retained_in_candidate_params(scenario):
    s=scenario;s.plan['params']['chain']={'armature':'OriginalRig','bones':['HairA','HairB']}
    s.state['candidate']['chain']={'armature':'OwnedRig','bones':['HairA','HairB']}
    params=service.candidate_params(s.plan,s.state)
    assert params['source_chain']['armature']=='OriginalRig' and params['chain']['armature']=='OwnedRig'
    assert params['primary_objects']==['BodyRig','Face'] and params['source_object']=='Source'


def test_uncertain_generation_blocks_freshness_before_another_blender_call(scenario):
    s=scenario;s.state['uncertain_operation']=True
    with pytest.raises(ValueError,match='OPERATION_OUTCOME_UNCERTAIN'):
        service.fresh(s.bridge,s.storage,s.plan,s.state)
    assert not s.bridge.calls


@pytest.mark.parametrize('terminal',['ACCEPTED','REJECTED'])
def test_terminal_generation_cannot_create_another_candidate(scenario,terminal):
    s=scenario;s.state['state']=terminal
    with pytest.raises(ValueError,match='GENERATION_TERMINAL'):
        service.ensure_candidate(s.bridge,s.storage,s.path,s.plan,s.state,'forbidden-new-candidate')
    assert not s.bridge.calls


def test_primary_verification_preserves_requested_complete_object_scope(scenario):
    s=scenario;result=service.verify_primary(s.bridge,s.plan)
    assert result['passed'] and result['body_and_face_unchanged']
    for body,params in s.bridge.calls:
        assert params['primary_objects']==['BodyRig','Face']
    assert result['before']==result['after']


def refinement_backend(s,monkeypatch,passed=False,velocity=1.):
    creations=[]
    def create(bridge,storage,path,plan,state,operation_id):
        version=state['last_version']+1;name='Candidate_v'+str(version)
        bridge.objects[name]=deepcopy(bridge.objects['Candidate_v1']);bridge.objects[name]['version']=version
        bridge.objects[name]['settings']=deepcopy(state['current_settings']);bridge.collections[name]=[name]
        state.update(candidate={'candidate_object':name,'candidate_rig':None,'collection':'Owned_v'+str(version),
            'source_map':{name:'Source'}},version=version,last_version=version,state='SECONDARY_CANDIDATE')
        signature=bridge.signature(name);state.update(candidate_fingerprint=signature['fingerprint'],candidate_raw_fingerprint=signature['raw_fingerprint'])
        creations.append(name);service.event(path,state);return state
    def generate(bridge,storage,path,plan,state,operation_id):
        state.update(state='SIMULATED' if passed else 'SECONDARY_CANDIDATE',
            colliders={'collider_names':[]},technical_qa={'passed':passed,'metrics':{'max_velocity_m_s':velocity}})
        service.event(path,state);return state
    monkeypatch.setattr(service,'ensure_candidate',create)
    monkeypatch.setattr(service,'generate_procedural',generate)
    return creations


def test_failed_refinements_restore_previous_scope_and_consume_all_three_cycles(scenario,monkeypatch):
    s=scenario;pc2(s);original=deepcopy(s.bridge.raw);initial=deepcopy(s.state)
    creations=refinement_backend(s,monkeypatch,passed=False)
    state=s.state
    for cycle in range(1,4):
        state=service.refine(s.bridge,s.storage,s.path,s.plan,state,'refine-'+str(cycle),{'settings':{'damping':8.+cycle}})
        assert state['candidate']==initial['candidate'] and state['current_settings']==initial['current_settings']
        assert state['refinement_cycles']==cycle and state['last_version']==cycle+1
        assert state['cache']==initial['cache'] and state['refinement_outcome']=='FAILED_AND_REVERTED'
        _,_,state=service.load(Storage(s.storage.root),str(s.path))
        assert service.fresh(s.bridge,s.storage,s.plan,state)
    with pytest.raises(ValueError,match='REFINEMENT_LIMIT'):
        service.refine(s.bridge,s.storage,s.path,s.plan,state,'refine-four',{'settings':{'damping':16.}})
    assert len(creations)==3 and s.bridge.raw==original


def test_reviewed_nonimprovement_reverts_without_resetting_counter(scenario,monkeypatch):
    s=scenario;initial=deepcopy(s.state);refinement_backend(s,monkeypatch,passed=True,velocity=1.)
    state=service.refine(s.bridge,s.storage,s.path,s.plan,s.state,'no-improvement',{'settings':{'damping':12.}})
    assert state['pending_refinement']['improved'] is False and state['refinement_cycles']==1
    state['render']={'status':'ok','temporal_complete':True,'output_dir':'contract-render'}
    # This is synthetic review-contract input, not stored real visual evidence.
    feedback={'reviewer':'Unit contract validator','observations':['Contract-only input'],'findings':[],
        'decision':'REJECT','artifact_reference':'contract-render','temporal_reviewed':True,
        'views_reviewed':['FRONT','SIDE','THREE_QUARTER'],'speeds_reviewed':[1,.25]}
    state=service.review(s.bridge,s.storage,s.path,s.plan,state,feedback)
    assert state['candidate']==initial['candidate'] and state['refinement_cycles']==1
    assert state['last_version']==2 and state['pending_refinement'] is None and state['refinement_outcome']=='REVERTED'


@pytest.mark.parametrize('target',['min_area_ratio','absolute_volume_ratio','bbox_growth_ratio','max_edge_stretch_ratio','delay_s','unknown_metric'])
def test_ambiguous_refinement_target_rejects_before_cycle_or_candidate_mutation(scenario,monkeypatch,target):
    s=scenario
    s.state['technical_qa']['metrics'][target]=1.
    service.event(s.path,s.state)
    initial=deepcopy(s.state);objects=deepcopy(s.bridge.objects)
    events=list(s.path.parent.glob('event-*.json'))
    def unexpected(*args,**kwargs):
        raise AssertionError('Rejected objective reached a checkpoint or candidate mutation')
    monkeypatch.setattr(service,'checkpoint',unexpected)
    monkeypatch.setattr(service,'ensure_candidate',unexpected)
    with pytest.raises(ValueError,match='PHYSICS_REFINEMENT_TARGET_UNSUPPORTED'):
        service.refine(s.bridge,s.storage,s.path,s.plan,s.state,'invalid-objective',
                       {'target':target,'settings':{'damping':12.}})
    assert s.state==initial and s.bridge.objects==objects
    assert list(s.path.parent.glob('event-*.json'))==events
    assert service.load(s.storage,str(s.path))[2]==initial


@pytest.mark.parametrize('value',[True,float('nan'),float('inf')])
def test_nonfinite_or_boolean_refinement_baseline_rejects_without_consuming_cycle(scenario,value):
    s=scenario;s.state['technical_qa']['metrics']['max_velocity_m_s']=value
    events=list(s.path.parent.glob('event-*.json'))
    with pytest.raises(ValueError,match='PHYSICS_REFINEMENT_METRIC_UNAVAILABLE'):
        service.refine(s.bridge,s.storage,s.path,s.plan,s.state,'invalid-baseline',{'settings':{'damping':12.}})
    assert s.state['refinement_cycles']==0 and list(s.path.parent.glob('event-*.json'))==events


@pytest.mark.parametrize('value',[True,float('nan'),float('inf')])
def test_invalid_refinement_measurement_restores_previous_candidate_and_consumes_attempt(scenario,monkeypatch,value):
    s=scenario;initial=deepcopy(s.state)
    refinement_backend(s,monkeypatch,passed=True,velocity=1.)
    # Supply malformed in-memory QA directly to the guard; the actual bridge
    # and event serializer already reject NaN/Infinity before this point.
    def malformed_measurement(bridge,storage,path,plan,state,operation_id):
        state.update(state='SIMULATED',technical_qa={'passed':True,'metrics':{'max_velocity_m_s':value}})
        return state
    monkeypatch.setattr(service,'generate_procedural',malformed_measurement)
    with pytest.raises(ValueError,match='PHYSICS_REFINEMENT_METRIC_UNAVAILABLE'):
        service.refine(s.bridge,s.storage,s.path,s.plan,s.state,'invalid-measurement',{'settings':{'damping':12.}})
    restored=service.load(s.storage,str(s.path))[2]
    assert restored['candidate']==initial['candidate'] and restored['refinement_cycles']==1
    assert restored['refinement_outcome']=='ERROR_AND_REVERTED'


def test_transport_uncertainty_is_persisted_and_another_operation_key_cannot_retry(scenario,monkeypatch):
    s=scenario;functions={};mutations=[]
    def registration(name,**kwargs):
        def keep(function):functions[name]=function;return function
        return keep
    tools.register(registration,lambda:(s.bridge,s.storage))
    def uncertain_dispatch(name,bridge,storage,operation_id,object_name,physics,intent,brief,options,apply):
        _,plan,state=service.load(storage,physics);service.fresh(bridge,storage,plan,state)
        mutations.append(operation_id);raise BlenderTimeoutError('sent; outcome uncertain')
    monkeypatch.setattr(service,'dispatch',uncertain_dispatch)
    execute=functions['production.physics_generate_accessory_motion']
    with pytest.raises(BlenderTimeoutError):
        execute(physics=str(s.path),apply=True,_operation_id='first-operation')
    _,plan,state=service.load(Storage(s.storage.root),str(s.path))
    assert state['uncertain_operation'] is True and state['uncertain_operation_id']=='first-operation'
    with pytest.raises(ValueError,match='OPERATION_OUTCOME_UNCERTAIN'):
        execute(physics=str(s.path),apply=True,_operation_id='different-operation')
    assert mutations==['first-operation']


def test_mesh_cache_collection_restores_primary_rna_in_actual_runtime_helper():
    node=next(n for n in ast.parse(service.BAKE_RUNTIME).body if isinstance(n,ast.FunctionDef) and n.name=='pb_collect')
    face=NS(value=.63);scene=NS(frame_current=8,frame_subframe=.25)
    def frame_set(frame,subframe=0.):
        scene.frame_current=frame;scene.frame_subframe=subframe;face.value=frame/31.
    scene.frame_set=frame_set
    class Vertices(list):
        def foreach_get(self,name,values):
            values[:]=array.array('f',[0.,0.,0.])
    evaluated=NS(to_mesh=lambda:NS(vertices=Vertices([NS()])),to_mesh_clear=lambda:None)
    candidate=NS(evaluated_get=lambda graph:evaluated)
    captured=[]
    def capture(objects):
        captured.extend(objects);return {'frame':scene.frame_current,'subframe':scene.frame_subframe,'face':face.value}
    def restore(objects,state):
        frame_set(state['frame'],state['subframe']);face.value=state['face']
    ns={'array':array,'bpy':NS(context=NS(scene=scene,evaluated_depsgraph_get=lambda:NS())),
        'pp_owned':lambda params:candidate,'px_selection':lambda params:(NS(),[face],[face]),
        'px_capture':capture,'px_restore':restore}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-cache-collection-helper>','exec'),ns)
    result=ns['pb_collect']({'frames':[1,16,31]})
    assert captured==[face] and face.value==.63 and scene.frame_current==8 and scene.frame_subframe==.25
    assert [row['frame'] for row in result['frames']]==[1,16,31]


def test_candidate_params_supports_explicitly_reset_collider_state_before_refinement_setup(scenario):
    s=scenario;s.state['colliders']=None
    params=service.candidate_params(s.plan,s.state)
    assert params['collider_names']==[] and params['collider_collection_name'] is None


@pytest.mark.parametrize('order',[(0,1,2),(2,1,0),(1,2,0)])
def test_restore_sets_frame_once_then_preserves_two_manual_rigs_and_key_owner(order):
    node=next(n for n in ast.parse(integrity.RUNTIME).body if isinstance(n,ast.FunctionDef) and n.name=='px_restore')
    poses=[NS(name='HeadA',manual=.17),NS(name='HeadZ',manual=-.29)]
    class Bones(dict):pass
    rigs=[NS(name='A_Rig',pose=NS(bones=Bones(HeadA=poses[0])),manual=.31,update_tag=lambda:None),
          NS(name='Z_Rig',pose=NS(bones=Bones(HeadZ=poses[1])),manual=-.43,update_tag=lambda:None)]
    key=NS(value=.63);mesh=NS(name='M_Face',data=NS(shape_keys=NS(eval_time=12.,key_blocks={'Smile':key})),
        manual=.5,update_tag=lambda:None)
    objects=rigs+[mesh];scene=NS(frame_current=8,frame_subframe=.25);frames=[]
    strip=NS(influence=.75,strip_time=4.5)
    rigs[0].animation_data=NS(nla_tracks={'Track':NS(strips={'Strip':strip})})
    def frame_set(frame,subframe=0.):
        frames.append(frame);scene.frame_current=frame;scene.frame_subframe=subframe
        for obj in objects:obj.manual=0.
        for pose in poses:pose.manual=0.
        key.value=0.;strip.influence=0.
    scene.frame_set=frame_set
    state={'objects':{o.name:{'frame':8,'subframe':.25,'object':{'manual':o.manual},
        'pose':{p.name:{'manual':p.manual} for p in poses if p.name in getattr(o,'pose',NS(bones={})).bones}} for o in objects},
        'keys':{'M_Face':{'eval_time':12.,'values':{'Smile':.63}}},'nla':[(rigs[0],'Track','Strip',.75,4.5)]}
    def channels(item,record):item.manual=record['manual']
    ns={'bpy':NS(context=NS(scene=scene,view_layer=NS(update=lambda:None))),
        'm3_set_channels':channels,'px_fail':lambda code,msg:None}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-ordered-rna-restoration>','exec'),ns)
    ns['px_restore']([objects[i] for i in order],state)
    assert frames==[8] and (scene.frame_current,scene.frame_subframe)==(8,.25)
    assert [o.manual for o in objects]==[.31,-.43,.5] and [p.manual for p in poses]==[.17,-.29]
    assert key.value==.63 and strip.influence==.75 and strip.strip_time==4.5


@pytest.mark.parametrize('changed',['rotation_mode','ik_stretch','use_ik_limit_x','bbone_curveinx','bbone_scalein'])
def test_actual_static_pose_configuration_signature_covers_modes_ik_and_bbone(changed):
    names={'px_pose_configuration','px_value','px_fail'}
    nodes=[n for n in ast.parse(integrity.RUNTIME).body if isinstance(n,ast.FunctionDef) and n.name in names]
    class Pose(NS):
        def path_resolve(self,key):return getattr(self,key)
    values={'rotation_mode':'QUATERNION','ik_stretch':0.,'use_ik_limit_x':False,'bbone_curveinx':0.,'bbone_scalein':[1.,1.,1.],
        'location':[0.,0.,0.],'rotation_quaternion':[1.,0.,0.,0.],'matrix_basis':[[1.,0.,0.,0.]]}
    pose=Pose(**values,bl_rna=NS(properties=[NS(identifier=k,type='ENUM' if k=='rotation_mode' else 'BOOLEAN' if k=='use_ik_limit_x' else 'FLOAT',is_readonly=False) for k in values]))
    import math
    ns={'math':math};exec(compile(ast.Module(body=nodes,type_ignores=[]),'<static-pose-configuration>','exec'),ns)
    before=runtime_digest(ns['px_pose_configuration'](pose))
    pose.location[0]=.2;pose.rotation_quaternion[1]=.1
    assert runtime_digest(ns['px_pose_configuration'](pose))==before
    if changed=='rotation_mode':pose.rotation_mode='XYZ'
    elif changed=='ik_stretch':pose.ik_stretch=.1
    elif changed=='use_ik_limit_x':pose.use_ik_limit_x=True
    elif changed=='bbone_curveinx':pose.bbone_curveinx=.1
    else:pose.bbone_scalein[0]=1.1
    assert runtime_digest(ns['px_pose_configuration'](pose))!=before
