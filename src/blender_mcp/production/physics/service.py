"""M8 generation transactions built on the existing asset guard, ledger and store."""
from __future__ import annotations

from copy import deepcopy
import importlib.metadata
import json
import math
from pathlib import Path
import time

from ..animation.generation import store
from ..checkpoint import create_checkpoint
from ..common.asset_safety import require_mutable_asset
from ..common.bridge import build_code
from ..common.operations import fingerprint
from ..common.ledger import is_uncertain
from ..common.storage import write_json, sha256
from . import cache, candidate, cloth, colliders, inspection, integrity, model, procedural, qa, render, spring

CATEGORY='physics'
STATES={'PLANNED','SECONDARY_CANDIDATE','SIMULATED','REVIEWED','ACCEPTED','REJECTED'}
KINDS={'CLOTH':'CLOTH','HAIR':'HAIR','SPRING_BONE':'SPRING_BONE','ACCESSORY':'DANGLING_ACCESSORY',
       'CHAIN':'CHAIN_LIKE','CABLE':'CABLE_LIKE','SOFT':'SOFT_REGION'}
OPTIONS={'manifest','primary_objects','colliders','pin_group','chain','anchor_object','anchor_bone',
         'proxy_strategy','max_proxy_deviation_m','segment_radius_m','max_vertices','thresholds'}
REFINEMENT_MINIMIZE_METRICS=frozenset({'max_displacement_m','max_velocity_m_s','max_acceleration_m_s2',
    'max_secondary_displacement_m','max_secondary_velocity_m_s','max_secondary_acceleration_m_s2',
    'max_secondary_angle_deg','degenerate_fraction','anchor_detachment_m',
    'root_detachment_m','anchor_origin_detachment_m','outside_region_max_error_m',
    'max_penetration_depth_m','penetration_vertex_frame_pairs'})


def backend_fingerprint():
    return fingerprint({p.name:sha256(p) for p in sorted(Path(__file__).parent.glob('*.py'))})


def load(storage,reference):
    return store.load(storage,reference,CATEGORY)


def event(path,state):
    store.event(path.parent,state,STATES)


def brief_options(options):
    values=dict(options or {})
    if set(values)-OPTIONS:
        raise ValueError('PHYSICS_OPTION_UNSUPPORTED: '+str(sorted(set(values)-OPTIONS)))
    return values


def compact_status(s):
    return {k:s.get(k) for k in ('source_object','source_fingerprint','primary_fingerprint','dependency_fingerprint',
                                  'context_fingerprint','fingerprint','source','context','state_restored')}


def public(path,plan,state):
    result={'physics':str(path),'generation_id':plan['generation_id'],'state':state['state'],
            'object_name':plan['object_name'],'subsystem':plan['brief']['subsystem'],
            'version':state.get('version',0),'refinement_cycles':state.get('refinement_cycles',0),
            'candidate':state.get('candidate'),'candidate_action':state.get('candidate_action'),
            'checkpoint':state.get('checkpoint'),'technical_qa':state.get('technical_qa'),
            'cache':state.get('cache'),'bake_deviation':{k:v for k,v in (state.get('bake_deviation') or {}).items() if k!='frames'} or None,
            'pending_refinement':bool(state.get('pending_refinement')),'no_action_created':not state.get('candidate')}
    if state.get('render'):
        result['render']={k:state['render'].get(k) for k in ('status','output_dir','player','animations','temporal_complete','metrics','camera_settings')}
    return result


def fresh(bridge,storage,plan,state,require_candidate=True):
    if state.get('uncertain_operation'):
        raise ValueError('OPERATION_OUTCOME_UNCERTAIN: inspect operation_status and recovery checkpoint; no automatic retry')
    if plan['backend_fingerprint']!=backend_fingerprint():
        raise ValueError('PHYSICS_BACKEND_CHANGED: plan again with the current backend')
    status=bridge.run(integrity.STATUS,plan['params'])
    if status['fingerprint']!=plan['source_status']['fingerprint']:
        raise ValueError('PHYSICS_STALE_SOURCE: body, face, geometry, collider, FPS, scale or context changed')
    evaluated=bridge.run(integrity.PRIMARY,dict(plan['params'],frames=plan['sample_frames']))
    if evaluated['fingerprint']!=plan['primary_snapshot']['fingerprint']:
        raise ValueError('PHYSICS_STALE_PRIMARY: evaluated primary pose or facial values changed; analyze again')
    if require_candidate and state.get('candidate'):
        cp=candidate_params(plan,state)
        actual=bridge.run(integrity.CANDIDATE_STATUS,dict(cp,candidate_object=cp['candidate_name']))
        if actual['fingerprint']!=state['candidate_fingerprint']:
            raise ValueError('PHYSICS_STALE_CANDIDATE: candidate geometry/settings/motion changed outside the plan')
    if state.get('baked_candidate'):
        cp=candidate_params(plan,state)
        baked=state['baked_candidate']
        actual=bridge.run(integrity.CANDIDATE_STATUS,dict(cp,candidate_object=baked['candidate_object'],
                                                        candidate_rig=baked.get('candidate_rig')))
        if actual['fingerprint']!=state.get('baked_fingerprint'):
            raise ValueError('PHYSICS_STALE_BAKED_CANDIDATE: re-bake/review after any baked geometry, motion or playback edit')
    if state.get('cache'):
        if state['cache']['representation']=='PC2':
            cache.verify(storage,state['cache'],cache_dependencies(plan,state))
        elif sha256(Path(state['cache']['path']))!=state['cache']['sha256']:
            raise ValueError('STALE_CACHE: serialized secondary Action changed')
    return status


def candidate_params(plan,state):
    c=state.get('candidate');b=plan['brief']
    if not isinstance(c,dict) or not c.get('candidate_object'):
        raise ValueError('PHYSICS_CANDIDATE_REQUIRED: create an isolated candidate before simulation or measurement')
    return dict(plan['params'],candidate_name=c['candidate_object'],candidate_rig=c.get('candidate_rig'),
                source_chain=plan['params'].get('chain'),
                chain=c.get('chain') or plan['params'].get('chain'),generation_id=plan['generation_id'],
                source_name=plan['object_name'],frame_start=b['frame_start'],frame_end=b['frame_end'],fps=b['fps'],
                gravity=b['gravity'],expected_status={'fingerprint':plan['source_status']['fingerprint']},
                collider_names=(state.get('colliders') or {}).get('collider_names',[]),
                collider_collection_name=(state.get('colliders') or {}).get('collider_collection_name'))


def thresholds(plan):
    b=plan['brief']['qa']
    mapped={'max_displacement_m':b['max_displacement_m'],'max_velocity_m_s':b['max_velocity_m_s'],
            'max_acceleration_m_s2':b['max_acceleration_m_s2'],'bbox_growth_ratio':b['max_extent_ratio'],
            'max_edge_stretch_ratio':b['max_stretch_ratio'],'max_penetration_depth_m':b['max_penetration_m'],
            'min_area_ratio':b['min_area_ratio'],
            'anchor_detachment_m':b['max_root_detachment_m'],'root_detachment_m':b['max_root_detachment_m']}
    mapped.update(plan['params'].get('thresholds',{}))
    return qa.validate_thresholds(mapped)


def validate_state(status,brief):
    context=status['context']
    if abs(context['units']['scale_length']-1)>1e-8:
        raise ValueError('PHYSICS_SCALE_UNSUPPORTED: calibrated meter-unit backend requires unit scale 1; never applies rig transforms')
    if any(any(not math.isfinite(v) or abs(v-1)>1e-6 for v in scale) for scale in context.get('object_scales',{}).values()):
        raise ValueError('PHYSICS_INVALID_SCALE: source/dependency transforms must have unit scale; never applies rig transforms')
    if brief['qa']['max_inverted_faces']!=0:
        raise ValueError('PHYSICS_INVERSION_COUNT_UNSUPPORTED: local face inversion count is not certified; global closed inversion always fails')
    if abs(context['fps']/context['fps_base']-brief['fps'])>1e-6:
        raise ValueError('PHYSICS_FPS_CHANGED: brief FPS must match the existing scene')
    if not context['use_gravity'] or any(abs(a-b)>1e-5 for a,b in zip(context['gravity'],brief['gravity'])):
        raise ValueError('PHYSICS_GRAVITY_CHANGED: scene gravity must match the explicit plan')
    if brief['frame_start']<context['range'][0] or brief['frame_end']>context['range'][1]:
        raise ValueError('PHYSICS_FRAME_RANGE: simulation clip must lie inside the existing scene range')


def plan(bridge,storage,operation_id,object_name,intent,brief,options):
    options=brief_options(options);b=model.create_brief(intent,brief)
    raw=inspection.inspect(bridge,object_name,options)
    classification=inspection.classify(raw,options.get('manifest',{}))
    params=dict(options,source_object=object_name)
    status=bridge.run(integrity.STATUS,params);validate_state(status,b)
    if classification['classification']!=KINDS[b['subsystem']]:
        supported=False
    else:
        supported=classification['automatic_setup_allowed']
    if b['subsystem'] in {'CLOTH','HAIR','SOFT'} and not options.get('colliders'):
        raise ValueError('PHYSICS_COLLIDERS_REQUIRED: explicitly identify collision bodies before planning')
    if b['subsystem']=='CLOTH' and not options.get('pin_group'):
        raise ValueError('CLOTH_PIN_PLAN_REQUIRED: explicit nonempty source vertex group')
    if b['subsystem']=='CLOTH' and b['frame_end']<1:
        raise ValueError('CLOTH_NATIVE_CACHE_WINDOW_UNSUPPORTED: installed PointCache frame_end has hard minimum 1')
    if b['subsystem']=='SOFT' and (not b['region_authorized'] or not b['region']):
        raise ValueError('SOFT_REGION_AUTHORIZATION_REQUIRED')
    if b['subsystem']=='SOFT' and not isinstance(b['region'],str):
        raise ValueError('SOFT_REGION_ADAPTER_LIMITED: this backend requires an explicit vertex-group mask')
    guards=bridge.run(procedural.GUARD_SELECTION,dict(params,subsystem=b['subsystem'],region=b['region']))
    count=b['frame_end']-b['frame_start']+1;vertices=status['source']['vertices']
    transfer_limit=16_000_000 if status['source']['type']=='MESH' else cache.MAX_CACHE_BYTES
    if vertices>20000 or vertices*count*12>min(cache.MAX_CACHE_BYTES,transfer_limit):
        raise ValueError('PHYSICS_CAPACITY: source mesh/frame cache exceeds the bounded backend')
    sample_frames=sorted(set(round(b['frame_start']+(count-1)*i/min(63,count-1)) for i in range(min(64,count))))
    primary=bridge.run(integrity.PRIMARY,dict(params,frames=sample_frames))
    data={'schema':'production.physics_plan.v1','object_name':object_name,'params':params,'brief':b,
          'classification':classification,'supported':supported,'source_status':status,
          'guard_selection':guards,
          'primary_snapshot':primary,'sample_frames':sample_frames,'backend_fingerprint':backend_fingerprint(),
          'capacity':{'frames':count,'vertices':vertices,'estimated_cache_bytes':32+vertices*count*12,
                      'max_sample_transfer_bytes':transfer_limit},
          'no_action_created':True}
    reference,saved=store.save(storage,operation_id,data,CATEGORY,STATES)
    return public(Path(reference),saved,{'state':'PLANNED'})|{'brief':b,'classification':classification,'supported':supported,'capacity':data['capacity']}


def checkpoint(bridge,storage,path,operation_id,description):
    require_mutable_asset(bridge,storage,'M8 '+description)
    return create_checkpoint(bridge,storage,description,operation_id,str(path))['manifest']


def refresh_candidate(bridge,plan,state):
    cp=candidate_params(plan,state)
    actual=bridge.run(integrity.CANDIDATE_STATUS,dict(cp,candidate_object=cp['candidate_name']))
    state['candidate_fingerprint']=actual['fingerprint']
    state['candidate_raw_fingerprint']=actual['raw_fingerprint']


def ensure_candidate(bridge,storage,path,plan,state,operation_id):
    store.mutable(state)
    if not plan['supported']:
        raise ValueError('PHYSICS_PLAN_UNSUPPORTED: classification/compatibility blocks automatic setup')
    fresh(bridge,storage,plan,state)
    if state.get('candidate'):
        return state
    cp=checkpoint(bridge,storage,path,operation_id,'Create isolated secondary candidate')
    version=state.get('last_version',0)+1;name='M8_'+plan['generation_id'][:8]+'_v'+str(version)
    params=dict(plan['params'],candidate_name=name,generation_id=plan['generation_id'],version=version,
                subsystem=KINDS[plan['brief']['subsystem']],expected_status={'fingerprint':plan['source_status']['fingerprint']})
    created=candidate.create(bridge,params)
    state.update(state='SECONDARY_CANDIDATE',candidate=created,version=version,last_version=version,
                 candidate_fingerprint=created['candidate_status']['fingerprint'],checkpoint=cp,
                 candidate_raw_fingerprint=created['candidate_status']['raw_fingerprint'],
                 current_settings=deepcopy(plan['brief']['settings']),cache=None,render=None,review=None)
    # Match the complete primary/dependency scope used by later freshness checks.
    refresh_candidate(bridge,plan,state)
    event(path,state);return state


def prepare_colliders(bridge,storage,path,plan,state,operation_id):
    state=ensure_candidate(bridge,storage,path,plan,state,operation_id)
    if state.get('colliders'):
        return state
    if not plan['params'].get('colliders'):
        state['colliders']={'collider_names':[],'status':'NOT_APPLICABLE','reason':'No collider declared in this bounded object-motion plan'}
    else:
        fresh(bridge,storage,plan,state);cp=checkpoint(bridge,storage,path,operation_id,'Prepare owned collision proxies')
        params=candidate_params(plan,state)|{'candidate_collection':state['candidate']['collection'],'version':state['version'],
                 'collider_collection_name':'M8_COLLIDERS_'+plan['generation_id']+'_v'+str(state['version'])}
        state['colliders']=bridge.run(colliders.PREPARE,params);state['checkpoint']=cp
        refresh_candidate(bridge,plan,state)
    event(path,state);return state


def pins(raw,name):
    row=next((g for g in raw['vertex_groups'] if g['name']==name),None)
    if not row or not 0<row['weighted_vertices']<raw['geometry']['vertices']:
        raise ValueError('CLOTH_PIN_REGION_INVALID: pins must leave both attached and dynamic vertices')
    return {'group':name,'coverage':row['coverage'],'count':row['weighted_vertices'],'boundary_vertices':row['boundary_vertices'],
            'weights':{'minimum':row['min_weight'],'maximum':row['max_weight']},'explicit':True}


def setup_cloth(bridge,storage,path,plan,state,operation_id):
    state=prepare_colliders(bridge,storage,path,plan,state,operation_id);fresh(bridge,storage,plan,state)
    if state.get('cloth_setup'):
        return state
    raw=inspection.inspect(bridge,plan['object_name'],plan['params']);pins(raw,plan['params']['pin_group'])
    cp=checkpoint(bridge,storage,path,operation_id,'Configure native cloth on the copied mesh')
    cp_params=candidate_params(plan,state)|{'settings':state['current_settings'],'pin_group':plan['params']['pin_group']}
    result=bridge.run(qa.RUNTIME+cloth.RUNTIME+'\nresult=pc_setup(params)\n',cp_params)
    state.update(cloth_setup=result,checkpoint=cp);refresh_candidate(bridge,plan,state);event(path,state);return state


def summarize_qa(result):
    rows=result.get('frames',result.get('qa',[]));metrics={}
    for row in rows:
        for name,value in row.get('metrics',{}).items():
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
                metrics[name]=(min if name=='min_area_ratio' else max)(metrics.get(name,value),value)
    for key in ('max_penetration_depth_m','penetration_vertex_frame_pairs','frames_affected','root_detachment_m'):
        if key in result:metrics[key]=result[key]
    errors=list(result.get('error_codes',[]))
    errors.extend(e for row in rows if not row.get('passed',True) for e in row.get('errors',[]))
    return {'passed':bool(result.get('passed')),'metrics':metrics,'errors':sorted(set(errors)),
            'thresholds':result.get('thresholds'),'coverage':result.get('coverage'),
            'limitations':result.get('limitations',[]),'visual_verification':'required'}


def qa_params(plan,state):
    params=candidate_params(plan,state)|{'frames':list(range(plan['brief']['frame_start'],plan['brief']['frame_end']+1)),
                                        'thresholds':thresholds(plan)}
    if params.get('chain'):
        params['bone_names']=params['chain']['bones']
        if state['candidate'].get('candidate_rig') and state['candidate']['candidate_object']==state['candidate']['candidate_rig']:
            params['source_name']=plan['params']['chain']['armature']
    params.update({k:plan.get('guard_selection',{}).get(k,[]) for k in ('anchor_indices','root_indices','outside_region_indices')})
    if plan['brief']['subsystem']!='CLOTH':
        params['secondary_displacement_limit_m']=state['current_settings']['max_displacement_m']
    if plan['brief']['subsystem'] not in {'CLOTH','SOFT'}:
        params['secondary_angle_limit_deg']=state['current_settings']['max_angle_deg']
    if plan['brief']['subsystem']=='ACCESSORY':
        params['anchor_mode']='OBJECT_ORIGIN'
    return params


def verify_primary(bridge,plan):
    status=bridge.run(integrity.STATUS,plan['params'])
    actual=bridge.run(integrity.PRIMARY,dict(plan['params'],frames=plan['sample_frames']))
    passed=status['fingerprint']==plan['source_status']['fingerprint'] and actual['fingerprint']==plan['primary_snapshot']['fingerprint']
    return {'passed':passed,'body_and_face_unchanged':passed,'source_fingerprint_before':plan['source_status']['fingerprint'],
            'source_fingerprint_after':status['fingerprint'],'before':plan['primary_snapshot']['fingerprint'],
            'after':actual['fingerprint'],'evaluated_sample_frames':len(plan['sample_frames']),
            'limitations':['Action/NLA/source data signed in full; evaluated primary motion sampled at up to 64 frames']}


def analyze(bridge,plan,state,kind):
    params=qa_params(plan,state)
    if kind=='collision':
        if not params['collider_names']:
            return {'passed':True,'status':'NOT_APPLICABLE','metrics':{},'reason':'No collider explicitly applicable to this plan'}
        result=bridge.run(qa.RUNTIME+'\nresult=pq_collision(params)\n',params)
    else:
        result=bridge.run(qa.RUNTIME+'\nresult=pq_motion(params)\n',params)
    return summarize_qa(result)


def qualify(bridge,storage,path,plan,state):
    movement=analyze(bridge,plan,state,'motion');collision=analyze(bridge,plan,state,'collision');primary=verify_primary(bridge,plan)
    passed=movement['passed'] and collision['passed'] and primary['passed']
    metrics=dict(movement['metrics'],**collision.get('metrics',{}))
    state['technical_qa']={'passed':passed,'motion':movement,'collision':collision,'primary_integrity':primary,
                           'solver_predictions':deepcopy(state.get('solver_metrics',{})),
                           'metrics':metrics,'threshold_source':'PROPOSED_PARAMETER','visual_verification':'required'}
    state.update(state='SIMULATED' if passed else 'SECONDARY_CANDIDATE',render=None,review=None)
    refresh_candidate(bridge,plan,state);event(path,state);return state


def simulate_cloth(bridge,storage,path,plan,state,operation_id):
    if plan['brief']['subsystem']!='CLOTH':raise ValueError('PHYSICS_SUBSYSTEM_MISMATCH')
    state=setup_cloth(bridge,storage,path,plan,state,operation_id);fresh(bridge,storage,plan,state)
    cp=checkpoint(bridge,storage,path,operation_id,'Guarded chronological cloth simulation')
    params=qa_params(plan,state)
    result=bridge.run(qa.RUNTIME+cloth.RUNTIME+'\nresult=pc_simulate(params)\n',params)
    state.update(simulation=result,checkpoint=cp)
    if not result['passed']:
        state['technical_qa']=summarize_qa(result);refresh_candidate(bridge,plan,state);event(path,state);return state
    return qualify(bridge,storage,path,plan,state)


def cache_dependencies(plan,state):
    return {'source':plan['source_status']['fingerprint'],'backend':plan['backend_fingerprint'],
            'evaluated_primary':plan['primary_snapshot']['fingerprint'],
            'settings':state['current_settings'],'candidate':state['candidate_raw_fingerprint'],'range':
            [plan['brief']['frame_start'],plan['brief']['frame_end']],'fps':plan['brief']['fps']}


def cache_plan(plan,state):
    b=plan['brief'];mesh=plan['source_status']['source']['type']=='MESH'
    return {'status':'PLANNED' if not state.get('cache') else state['cache']['status'],
            'representation':'PC2' if mesh else 'SECONDARY_ACTION','frame_start':b['frame_start'],'frame_end':b['frame_end'],
            'source_dependencies':compact_status(plan['source_status']),'settings_fingerprint':fingerprint(state.get('current_settings',b['settings'])),
            'estimated_bytes':plan['capacity']['estimated_cache_bytes'],'no_data_written':True}


def generate_procedural(bridge,storage,path,plan,state,operation_id):
    store.mutable(state);fresh(bridge,storage,plan,state)
    b=plan['brief'];kind=b['subsystem']
    if kind=='CLOTH':raise ValueError('PHYSICS_SUBSYSTEM_MISMATCH')
    # Read and solve BEFORE creating a copy: unsupported data/capacity must not mutate Blender.
    data=bridge.run(procedural.SAMPLES,dict(plan['params'],subsystem=kind,frame_start=b['frame_start'],frame_end=b['frame_end']))
    samples=data['samples'];settings=state.get('current_settings',b['settings'])
    settings=dict(settings,gravity=b['gravity'])
    if plan['params'].get('chain'):
        ratio=data.get('mesh_displacement_budget_ratio',1.0)
        if isinstance(ratio,bool) or not isinstance(ratio,(int,float)) or not math.isfinite(ratio) or not 0<ratio<=1:
            raise ValueError('SECONDARY_MESH_BUDGET_INVALID')
        settings['max_displacement_m']*=ratio
    if kind=='ACCESSORY':
        radius=data['maximum_object_radius_m']
        if isinstance(radius,bool) or not isinstance(radius,(int,float)) or not math.isfinite(radius) or radius<=0:
            raise ValueError('ACCESSORY_GEOMETRY_RADIUS_REQUIRED')
        geometry_angle=math.degrees(2*math.asin(min(1.0,settings['max_displacement_m']/(2*radius))))
        settings['max_angle_deg']=min(settings['max_angle_deg'],geometry_angle)
    driver=[{k:v for k,v in row.items() if k in {'frame','position','quaternion','directions'}} for row in samples]
    solved=spring.solve_chain(driver,data['lengths'],b['fps'],settings) if plan['params'].get('chain') else spring.solve(driver,settings,b['fps'],kind)
    state=prepare_colliders(bridge,storage,path,plan,state,operation_id);fresh(bridge,storage,plan,state)
    cp=checkpoint(bridge,storage,path,operation_id,'Generate bounded secondary motion on isolated copies')
    params=candidate_params(plan,state);channels=[]
    action_name=params['candidate_name']+'_Motion'
    if plan['params'].get('chain'):
        names=plan['params']['chain']['bones'];values=[[] for _ in names]
        for f,row in enumerate(samples):
            desired_bases=[]
            for j,name in enumerate(names):
                endpoint=solved['positions'][f][j+1];start=solved['positions'][f][j]
                direction=spring._sub(endpoint,start)
                delta_world=spring._between(row['directions'][j],direction)
                desired=spring._qmul(delta_world,row['bone_basis'][j]);desired_bases.append(desired)
                propagated=row['bone_basis'][j]
                if j:
                    propagated=spring._qmul(spring._qmul(desired_bases[j-1],spring._qinv(row['bone_basis'][j-1])),propagated)
                local=spring._qmul(spring._qinv(propagated),desired)
                if row['bone_modes'][j]!='QUATERNION':
                    raise ValueError('SECONDARY_ROTATION_MODE_UNSUPPORTED: no rotation-mode conversion is performed')
                local=spring._quaternion(spring._qmul(row['bone_base_quaternions'][j],local),'secondary_bone')
                if values[j] and sum(a*b for a,b in zip(values[j][-1],local))<0:local=[-v for v in local]
                values[j].append(local)
        for j,name in enumerate(names):
            prefix='pose.bones['+json.dumps(name,ensure_ascii=False)+'].rotation_quaternion'
            for axis in range(4):
                channels.append({'bone':name,'path':prefix,'index':axis,'co':[v for frame,q in zip(solved['frames'],values[j]) for v in (frame,q[axis])]})
    elif kind=='SOFT':
        bridge.run(procedural.SOFT_SETUP,params|{'region':b['region'],'region_authorized':b['region_authorized'],
                                               'max_displacement_m':settings['max_displacement_m']})
        # Transform each world-space residual through the evaluated source basis in Blender.
        converted=bridge.run(procedural.RUNTIME+r'''
result={'values':[]}
for sample,offset in zip(params['samples'],params['offsets']):
    matrix=Matrix(sample['source_world']).to_3x3().inverted()
    result['values'].append(list(matrix@Vector(offset)/params['maximum']))
''',{'samples':samples,'offsets':solved['offsets'],'maximum':settings['max_displacement_m']})
        for axis,label in enumerate(('X','Y','Z')):
            channels.append({'path':'key_blocks["M8_Soft'+label+'"].value','index':0,
                             'co':[v for frame,value in zip(solved['frames'],converted['values']) for v in (frame,value[axis])]})
        params['soft']=True
    else:
        if any(row['source_rotation_mode']!='QUATERNION' for row in samples):
            raise ValueError('SECONDARY_ROTATION_MODE_UNSUPPORTED: object adapter requires an existing quaternion mode')
        # The origin is copied from the primary local transform; rotation swings about that anchor.
        rotations=[spring._qmul(row['source_rotation'],delta) for row,delta in zip(samples,solved['rotations'])]
        for field,arrays,size in (('location',[r['source_location'] for r in samples],3),('rotation_quaternion',rotations,4),('scale',[r['source_scale'] for r in samples],3)):
            for axis in range(size):
                channels.append({'path':field,'index':axis,'co':[v for row,array in zip(samples,arrays) for v in (row['frame'],array[axis])]})
    # Channel-sized chunks preserve the upstream payload budget, even on long clips.
    for i,ch in enumerate(channels):
        chunk=params|{'action_name':action_name,'channels':[ch],'write_mode':'create' if i==0 else 'append',
                       'finalize':i==len(channels)-1,'mcp_version':importlib.metadata.version('blender-production-mcp')}
        build_code(procedural.WRITE,chunk)
        bridge.run(procedural.WRITE,chunk)
    state.update(candidate_action=action_name,solver_metrics=solved['metrics'],checkpoint=cp,
                 solver_configuration={'settings':settings,'mesh_displacement_budget_ratio':data.get('mesh_displacement_budget_ratio',1.0),
                                       'bound_basis':'weighted rest lever arms; evaluated geometry guard remains mandatory'},
                 simulation={'backend':'BOUNDED_PROCEDURAL_SPRING','frames_processed':len(samples),'passed':True},cache=None)
    return qualify(bridge,storage,path,plan,state)


BAKE_RUNTIME = procedural.RUNTIME + r'''
import array
def pb_clone(params):
    original=pp_owned(params);name=params['baked_name']
    if bpy.data.objects.get(name):
        px_fail('PHYSICS_BAKE_COLLISION','never overwrite a baked object')
    copy=original.copy();copy.data=original.data.copy();copy.name=name
    bpy.data.collections[params['candidate_collection']].objects.link(copy)
    copy.hide_render=True;copy['production_m8_bake_of']=original.name
    owners=[copy]+([copy.data.shape_keys] if copy.type=='MESH' and copy.data.shape_keys else [])
    for index,owner in enumerate(owners):
        ad=owner.animation_data
        if ad and ad.action and ad.action.get('production_m8_owned'):
            action=ad.action.copy();action.name=name+'_Motion_'+str(index);action.use_fake_user=True
            ad.action=action;ad.action_slot=action.slots[0]
    if copy.type=='ARMATURE':
        for track in copy.animation_data.nla_tracks if copy.animation_data else []:
            for strip in track.strips:
                if strip.action and strip.action.get('production_m8_owned'):
                    action=strip.action.copy();action.name=name+'_Motion';action.use_fake_user=True
                    strip.action=action;strip.action_slot=action.slots[0]
    return {'candidate_object':copy.name,'candidate_rig':copy.name if copy.type=='ARMATURE' else params.get('candidate_rig'),
            'generation_id':params['generation_id'],'source_map':{copy.name:params['source_object']}}

def pb_collect(params):
    obj=pp_owned(params);source,primary,deps=px_selection(params);saved=px_capture(deps);frames=[]
    try:
        for frame in params['frames']:
            bpy.context.scene.frame_set(frame);graph=bpy.context.evaluated_depsgraph_get();evaluated=obj.evaluated_get(graph)
            mesh=evaluated.to_mesh()
            try:
                values=array.array('f',[0])*(len(mesh.vertices)*3);mesh.vertices.foreach_get('co',values)
                frames.append({'frame':frame,'vertex_count':len(mesh.vertices),'hex':values.tobytes().hex()})
            finally:
                evaluated.to_mesh_clear()
        return {'frames':frames,'not_for_model':True}
    finally:
        px_restore(deps,saved)
'''


def bake_cache(bridge,storage,path,plan,state,operation_id):
    fresh(bridge,storage,plan,state)
    if state.get('cache'):return state
    if not state.get('technical_qa',{}).get('passed'):
        raise ValueError('PHYSICS_QA_REQUIRED: only validated simulation is reusable')
    cp=checkpoint(bridge,storage,path,operation_id,'Create an exclusive validated secondary cache')
    params=qa_params(plan,state);began=time.perf_counter()
    if plan['source_status']['source']['type']=='MESH':
        payload=qa.RUNTIME+cloth.RUNTIME+'\nresult=pc_collect(params)\n' if plan['brief']['subsystem']=='CLOTH' else BAKE_RUNTIME+'\nresult=pb_collect(params)\n'
        sampled=bridge.run(payload,params)
        state['cache']=cache.write_pc2(storage,operation_id,sampled['frames'],cache_dependencies(plan,state),plan['brief']['frame_start'])
    else:
        data=bridge.run(procedural.RUNTIME+"\nresult={'animation':px_animation(px_object(params['candidate_name']))}\n",params)
        folder=storage.reserve('physics-cache',operation_id);file=folder/'secondary-action.json';write_json(file,data)
        state['cache']={'representation':'SECONDARY_ACTION','status':'BAKED','path':str(file),'sha256':sha256(file),
                        'bytes':file.stat().st_size,'dependencies_fingerprint':fingerprint(cache_dependencies(plan,state)),
                        'frames':len(params['frames'])}
    state['cache']['duration_ms']=round((time.perf_counter()-began)*1000,2);state['checkpoint']=cp
    event(path,state);return state


def bake_secondary(bridge,storage,path,plan,state,operation_id):
    state=bake_cache(bridge,storage,path,plan,state,operation_id);fresh(bridge,storage,plan,state)
    if state.get('baked_candidate'):return state
    cp=checkpoint(bridge,storage,path,operation_id,'Bake reusable secondary representation on a new copy')
    params=candidate_params(plan,state)|{'baked_name':state['candidate']['candidate_object']+'_Baked',
                                        'candidate_collection':state['candidate']['collection']}
    baked=bridge.run(BAKE_RUNTIME+'\nresult=pb_clone(params)\n',params)
    if state['cache']['representation']=='PC2':
        playback=params|{'candidate_name':baked['candidate_object'],'filepath':state['cache']['path'],
                         'host_path_validated':True}
        bridge.run(qa.RUNTIME+cloth.RUNTIME+'\nresult=pc_playback(params)\n',playback)
    compare=qa_params(plan,state)|{'native_candidate_name':params['candidate_name'],
                                 'baked_candidate_name':baked['candidate_object'],'tolerance_m':thresholds(plan)['bake_max_error_m']}
    deviation=bridge.run(qa.RUNTIME+'\nresult=pq_bake_compare(params)\n',compare)
    if not deviation['passed']:
        state.update(baked_candidate=baked,bake_deviation=deviation,checkpoint=cp);event(path,state)
        raise ValueError('PHYSICS_BAKE_DEVIATION: baked output differs from the validated simulation')
    state.update(baked_candidate=baked,bake_deviation=deviation,checkpoint=cp)
    refresh_candidate(bridge,plan,state)
    state['baked_fingerprint']=bridge.run(integrity.CANDIDATE_STATUS,dict(params,candidate_object=baked['candidate_object'],
                                    candidate_rig=baked.get('candidate_rig')))['fingerprint']
    event(path,state);return state


def review(bridge,storage,path,plan,state,values):
    fresh(bridge,storage,plan,state);store.mutable(state)
    if not state.get('technical_qa',{}).get('passed'):
        raise ValueError('PHYSICS_QA_REQUIRED')
    accepted=store.review_gate(values,state.get('render'))
    if set(values.get('views_reviewed',[]))!=set(render.VIEWS) or set(values.get('speeds_reviewed',[]))!={1,.25}:
        raise ValueError('PHYSICS_TEMPORAL_REVIEW_REQUIRED: all three views at normal and quarter speed')
    if any(k in values for k in ('score','rating','aaa_score','physics_score')):
        raise ValueError('PHYSICS_REVIEW_SCORE_UNSUPPORTED: observations are not a realism score')
    state.update(state='REVIEWED',review={'TECHNICAL_FINDINGS':values.get('technical_findings',values['findings']),
                                       'ARTISTIC_OBSERVATIONS':values.get('artistic_observations',values['observations']),
                                       'record':accepted})
    if state.get('pending_refinement'):
        pending=state['pending_refinement']
        if not pending['improved'] or values['decision']!='ACCEPT':
            previous=pending['previous'];cycles=state['refinement_cycles'];last=state['last_version']
            rejected=deepcopy(state);event(path,dict(state,refinement_outcome='REVERTED',rejected_candidate=state['candidate']))
            state=deepcopy(previous);state.update(refinement_cycles=cycles,last_version=last,pending_refinement=None,
                                                  refinement_outcome='REVERTED',last_rejected_candidate=rejected['candidate'])
        else:
            state.update(pending_refinement=None,refinement_outcome='IMPROVED_AND_REVIEWED')
    event(path,state);return state


def refine(bridge,storage,path,plan,state,operation_id,options):
    fresh(bridge,storage,plan,state);store.refinement_gate(state)
    if state.get('pending_refinement'):raise ValueError('PHYSICS_REFINEMENT_PENDING_REVIEW')
    if not state.get('technical_qa',{}).get('passed') or state['state']!='REVIEWED':
        raise ValueError('PHYSICS_REVIEW_REQUIRED: start refinement from a qualified and reviewed candidate')
    if set(options)-{'settings','target','minimum_relative_improvement'}:
        raise ValueError('PHYSICS_REFINE_OPTION_UNSUPPORTED')
    target=options.get('target','max_velocity_m_s')
    if not isinstance(target,str) or target not in REFINEMENT_MINIMIZE_METRICS:
        raise ValueError('PHYSICS_REFINEMENT_TARGET_UNSUPPORTED: choose a measured error or magnitude with an explicit minimization direction')
    before=state['technical_qa']['metrics'].get(target)
    if not isinstance(before,(int,float)) or isinstance(before,bool) or not math.isfinite(before):
        raise ValueError('PHYSICS_REFINEMENT_METRIC_UNAVAILABLE')
    minimum=options.get('minimum_relative_improvement',.01)
    if isinstance(minimum,bool) or not isinstance(minimum,(int,float)) or not math.isfinite(minimum) or not 0<minimum<=1:
        raise ValueError('PHYSICS_REFINEMENT_THRESHOLD_INVALID')
    settings=model.normalize_settings(plan['brief']['subsystem'],dict(state['current_settings'],**options.get('settings',{})),
                                      plan['brief']['profile'],plan['brief']['fps'])
    previous=deepcopy(state);state.update(refinement_cycles=state.get('refinement_cycles',0)+1)
    event(path,state)  # Consume before any copy/simulation; failures and reversions do not reset it.
    state.update(state='PLANNED',candidate=None,candidate_fingerprint=None,colliders=None,cloth_setup=None,
                 cache=None,baked_candidate=None,bake_deviation=None,technical_qa=None,render=None,review=None,
                 current_settings=settings)
    try:
        state=ensure_candidate(bridge,storage,path,plan,state,operation_id)
        state['current_settings']=settings
        state=simulate_cloth(bridge,storage,path,plan,state,operation_id) if plan['brief']['subsystem']=='CLOTH' else generate_procedural(bridge,storage,path,plan,state,operation_id)
        after=state.get('technical_qa',{}).get('metrics',{}).get(target)
        valid_after=isinstance(after,(int,float)) and not isinstance(after,bool) and math.isfinite(after)
        if state.get('technical_qa',{}).get('passed') and not valid_after:
            raise ValueError('PHYSICS_REFINEMENT_METRIC_UNAVAILABLE')
        improved=state.get('technical_qa',{}).get('passed') and valid_after and after<before-max(abs(before)*minimum,1e-9)
        state['pending_refinement']={'previous':previous,'target':target,'before':before,'after':after,'improved':bool(improved)}
        if not state.get('technical_qa',{}).get('passed'):
            cycles=state['refinement_cycles'];last=state['last_version'];failed=deepcopy(state)
            event(path,dict(state,refinement_outcome='FAILED_AND_REVERTED'))
            state=deepcopy(previous);state.update(refinement_cycles=cycles,last_version=last,pending_refinement=None,
                                                  refinement_outcome='FAILED_AND_REVERTED',last_rejected_candidate=failed['candidate'])
        event(path,state);return state
    except Exception as exc:
        failed=state.get('candidate');cycles=state['refinement_cycles'];last=state.get('last_version',previous['last_version'])
        if is_uncertain(exc):
            state.update(uncertain_operation=True,refinement_outcome='UNCERTAIN',last_error=repr(exc))
            event(path,state);raise
        restored=deepcopy(previous);restored.update(refinement_cycles=cycles,last_version=last,pending_refinement=None,
                                                    refinement_outcome='ERROR_AND_REVERTED',last_error=repr(exc),last_rejected_candidate=failed)
        event(path,restored);raise


def accept_reject(bridge,storage,path,plan,state,operation_id,accept):
    fresh(bridge,storage,plan,state);store.mutable(state)
    if state.get('pending_refinement'):raise ValueError('PHYSICS_REFINEMENT_PENDING_REVIEW')
    if accept:
        if state['state']!='REVIEWED' or state.get('review',{}).get('record',{}).get('decision')!='ACCEPT' or not state.get('technical_qa',{}).get('passed'):
            raise ValueError('PHYSICS_ACCEPT_GATES_REQUIRED: qualified simulation and current real temporal ACCEPT review')
        if not state.get('bake_deviation',{}).get('passed') or not state.get('cache'):
            raise ValueError('PHYSICS_BAKE_VALIDATION_REQUIRED')
    cp=checkpoint(bridge,storage,path,operation_id,'Accept secondary candidate' if accept else 'Reject secondary candidate')
    terminal='ACCEPTED' if accept else 'REJECTED'
    params=candidate_params(plan,state)|{'state':terminal,'enabled':False,'owned_objects':list(state['candidate']['source_map'])}
    bridge.run(procedural.MARK,params)
    state.update(state=terminal,checkpoint=cp);refresh_candidate(bridge,plan,state);event(path,state);return state


def dispatch(name,bridge,storage,operation_id,object_name=None,physics=None,intent=None,brief=None,options=None,apply=False):
    options=dict(options or {})
    if type(apply) is not bool:raise ValueError('PHYSICS_APPLY_INVALID')
    if name=='physics_create_brief':
        return {'status':'planned','brief':model.create_brief(intent,brief),'no_action_created':True}
    if name in {'physics_inspect_character','physics_classify_secondary','physics_compatibility_report','physics_analyze_cloth',
                'physics_plan_cloth_pins','physics_analyze_hair','physics_analyze_accessory','physics_analyze_soft_region'} and not physics:
        if not object_name:raise ValueError('PHYSICS_OBJECT_REQUIRED')
        opts=brief_options(options);raw=inspection.inspect(bridge,object_name,opts)
        classified=inspection.classify(raw,opts.get('manifest',{}))
        if name=='physics_plan_cloth_pins':return {'status':'planned','pins':pins(raw,opts.get('pin_group')),'no_action_created':True}
        if name=='physics_compatibility_report':
            return {'status':'analyzed','compatibility':inspection.compatibility(raw,classified,raw['api'])}
        raw.pop('source_status',None)
        return {'status':'analyzed','inspection':raw,'classification':classified,'no_action_created':True}
    if name in {'physics_plan','physics_plan_hair','physics_setup_spring_chain'} and not physics:
        if not object_name:raise ValueError('PHYSICS_OBJECT_REQUIRED')
        result=plan(bridge,storage,operation_id,object_name,intent,brief,options)
        if apply:
            p,pl,st=load(storage,result['physics']);st=ensure_candidate(bridge,storage,p,pl,st,operation_id)
            return public(p,pl,st)
        return result
    if not physics:raise ValueError('PHYSICS_PLAN_REQUIRED: plan before mutation')
    path,pl,st=load(storage,physics)
    if name=='physics_status':
        try:
            fresh(bridge,storage,pl,st)
            return public(path,pl,st)|{'fresh':True}
        except ValueError as error:
            result=public(path,pl,st);result.update(fresh=False,stale_reason=str(error))
            if result.get('cache'):result['cache']=dict(result['cache'],status='STALE')
            return result
    fresh(bridge,storage,pl,st)
    if name=='physics_verify_primary_integrity':return {'status':'analyzed','integrity':verify_primary(bridge,pl)}
    if name in {'physics_collision_report','physics_analyze_penetration','physics_analyze_motion','physics_analyze_stability'}:
        result=analyze(bridge,pl,st,'collision' if name in {'physics_collision_report','physics_analyze_penetration'} else 'motion')
        return {'status':'analyzed','qa':result,'physics':str(path)}
    if name=='physics_cache_plan':return {'status':'planned','cache_plan':cache_plan(pl,st),'physics':str(path)}
    if name in {'physics_inspect_character','physics_classify_secondary','physics_compatibility_report','physics_analyze_cloth',
                'physics_plan_cloth_pins','physics_analyze_hair','physics_analyze_accessory','physics_analyze_soft_region'}:
        return dispatch(name,bridge,storage,operation_id,pl['object_name'],None,None,None,
                        {k:v for k,v in pl['params'].items() if k in OPTIONS},False)
    if name=='physics_review':
        if set(options)!={'review'}:raise ValueError('PHYSICS_REVIEW_REQUIRED: options.review')
        st=review(bridge,storage,path,pl,st,options['review']);return public(path,pl,st)|{'review':st['review']}
    if name in {'physics_render_preview','physics_compare_before_after'}:
        if not st.get('technical_qa',{}).get('passed'):raise ValueError('PHYSICS_QA_REQUIRED')
        store.mutable(st)
        cp=checkpoint(bridge,storage,path,operation_id,'Paired full temporal secondary-motion preview')
        result=render.temporal(bridge,storage,operation_id,pl,st,options)
        st.update(render=result,review=None,checkpoint=cp);event(path,st)
        return public(path,pl,st)|{'status':'rendered'}
    if not apply:
        return public(path,pl,st)|{'status':'proposed','operation':name,'apply':False,'no_changes_applied':True}
    store.mutable(st)
    if st.get('pending_refinement') and name!='physics_reject':raise ValueError('PHYSICS_REFINEMENT_PENDING_REVIEW')
    if options and name!='physics_refine':raise ValueError('PHYSICS_PROPOSAL_OPTIONS_FROZEN: change parameters by a new plan or refinement')
    if name=='physics_create_candidate':st=ensure_candidate(bridge,storage,path,pl,st,operation_id)
    elif name=='physics_prepare_colliders':st=prepare_colliders(bridge,storage,path,pl,st,operation_id)
    elif name=='physics_setup_cloth':st=setup_cloth(bridge,storage,path,pl,st,operation_id)
    elif name=='physics_simulate_cloth':st=simulate_cloth(bridge,storage,path,pl,st,operation_id)
    elif name in {'physics_generate_hair_secondary','physics_generate_secondary_bones','physics_generate_accessory_motion','physics_generate_soft_secondary'}:
        expected={'physics_generate_hair_secondary':{'HAIR'},'physics_generate_secondary_bones':{'SPRING_BONE','CHAIN','CABLE'},
                  'physics_generate_accessory_motion':{'ACCESSORY'},'physics_generate_soft_secondary':{'SOFT'}}
        if pl['brief']['subsystem'] not in expected[name]:raise ValueError('PHYSICS_SUBSYSTEM_MISMATCH')
        st=generate_procedural(bridge,storage,path,pl,st,operation_id)
    elif name=='physics_bake_cache':st=bake_cache(bridge,storage,path,pl,st,operation_id)
    elif name=='physics_bake_secondary_motion':st=bake_secondary(bridge,storage,path,pl,st,operation_id)
    elif name=='physics_refine':st=refine(bridge,storage,path,pl,st,operation_id,options)
    elif name=='physics_accept':st=accept_reject(bridge,storage,path,pl,st,operation_id,True)
    elif name=='physics_reject':st=accept_reject(bridge,storage,path,pl,st,operation_id,False)
    elif name=='physics_setup_spring_chain':st=ensure_candidate(bridge,storage,path,pl,st,operation_id)
    elif name in {'physics_plan','physics_plan_hair'}:return public(path,pl,st)
    else:raise ValueError('PHYSICS_TOOL_UNKNOWN: '+name)
    return public(path,pl,st)
