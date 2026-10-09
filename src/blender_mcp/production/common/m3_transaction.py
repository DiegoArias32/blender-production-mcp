"""M3 proposal transactions; reuse upstream transport and M1/M2 storage unchanged.

Backends supply five Blender functions: m3_propose, m3_context, m3_apply,
m3_verify and m3_allowed_drivers (plus their helpers). A proposal may request
candidate_policy='new'; otherwise the selected Action is copied.
"""
from __future__ import annotations

from ..animation.scripts import ACTION_HELPERS
from ..animation.edit_runtime import ACTION_RUNTIME
from ..animation.corrections import _validate_options, _record_result
from .digest import DIGEST_RUNTIME, runtime_digest
from .operations import fingerprint, save_proposal, load_proposal, record_event, latest_candidate
from ..checkpoint import create_checkpoint
from .asset_safety import require_mutable_asset
from .ledger import guarded_cycle, is_uncertain
from .bridge import payload_bytes

M3_RUNTIME = r'''
import bpy
import math
from mathutils import Vector, Matrix, Quaternion

def m3_properties(item):
    values={}
    for key,value in item.items():
        if isinstance(value,(str,int,float,bool)):
            values[key]=value
        elif isinstance(value,(list,tuple)) or hasattr(value,'to_list'):
            values[key]=list(value)
    return values

def m3_channels(item):
    return {'location':list(item.location),'scale':list(item.scale),
        'rotation_mode':item.rotation_mode,'rotation_euler':list(item.rotation_euler),
        'rotation_quaternion':list(item.rotation_quaternion),'rotation_axis_angle':list(item.rotation_axis_angle),
        'custom':m3_properties(item),'constraints':{c.name:{'influence':c.influence,'mute':c.mute} for c in item.constraints}}

def m3_set_channels(item,values):
    item.location=values['location'];item.scale=values['scale'];item.rotation_mode=values['rotation_mode']
    item.rotation_euler=values['rotation_euler'];item.rotation_quaternion=values['rotation_quaternion']
    item.rotation_axis_angle=values['rotation_axis_angle']
    for key,value in values['custom'].items():
        item[key]=value
    for name,value in values['constraints'].items():
        c=item.constraints.get(name)
        if c is None:
            raise ValueError('Constraint disappeared during state recovery: '+name)
        c.influence=value['influence'];c.mute=value['mute']

def m3_capture(obj):
    scene=bpy.context.scene
    return {'frame':scene.frame_current,'subframe':scene.frame_subframe,'animation_data_existed':obj.animation_data is not None,'object':m3_channels(obj),
        'pose':{p.name:m3_channels(p) for p in obj.pose.bones} if obj.type=='ARMATURE' else {}}

def m3_restore(obj,state):
    bpy.context.scene.frame_set(state['frame'],subframe=state['subframe'])
    m3_set_channels(obj,state['object'])
    for name,values in state['pose'].items():
        pose=obj.pose.bones.get(name)
        if pose is None:
            raise ValueError('Bone disappeared during state recovery: '+name)
        m3_set_channels(pose,values)
    obj.update_tag()
    bpy.context.view_layer.update()

def m3_assign(obj,action,slot_handle=None):
    data=obj.animation_data_create()
    chosen=None
    if action is not None and getattr(action,'is_action_layered',False) and action.slots:
        slots=[s for s in action.slots if s.target_id_type=='OBJECT' and (slot_handle is None or s.handle==slot_handle)]
        if len(slots)!=1:
            raise ValueError('Ambiguous/missing candidate Action slot')
        chosen=slots[0]
    prior=data.action;prior_slot=getattr(data,'action_slot',None)
    try:
        data.action=action
        if chosen is not None:
            data.action_slot=chosen
    except Exception:
        data.action=prior
        if prior_slot is not None:
            data.action_slot=prior_slot
        raise

def m3_driver_record(curve):
    driver=curve.driver
    return {'path':curve.data_path,'index':curve.array_index,'mute':curve.mute,'type':driver.type,
        'expression':driver.expression,'use_self':driver.use_self,'valid':driver.is_valid,
        'variables':[{'name':v.name,'type':v.type,'targets':[{'id':t.id.name if t.id else None,
            'path':t.data_path,'bone':t.bone_target,'transform_type':t.transform_type,
            'transform_space':t.transform_space} for t in v.targets]} for v in driver.variables]}

def m3_constraint_record(constraint):
    values={}
    for prop in constraint.bl_rna.properties:
        if prop.identifier in {'rna_type','name'} or prop.type=='COLLECTION':
            continue
        try:
            value=constraint.path_resolve(prop.identifier)
            if prop.type=='POINTER':
                values[prop.identifier]=value.name if value and hasattr(value,'name') else None
            elif prop.is_array:
                values[prop.identifier]=list(value)
            elif isinstance(value,(str,int,float,bool)):
                values[prop.identifier]=value
        except (ValueError,AttributeError,TypeError):
            values[prop.identifier]='unavailable'
    return {'name':constraint.name,'properties':values}

def m3_context_base(obj):
    state=m3_capture(obj)
    try:
        bpy.context.scene.frame_set(bpy.context.scene.frame_start)
        result={'source_file':bpy.data.filepath,'scene':bpy.context.scene.name,
            'fps':bpy.context.scene.render.fps/bpy.context.scene.render.fps_base,
            'units':bpy.context.scene.unit_settings.scale_length,
            'range':[bpy.context.scene.frame_start,bpy.context.scene.frame_end],
            'name':obj.name,'type':obj.type,'matrix': [list(row) for row in obj.matrix_world],
            'rotation_mode':obj.rotation_mode,'custom':m3_properties(obj),
            'parent':obj.parent.name if obj.parent else None,
            'constraints':[m3_constraint_record(c) for c in obj.constraints],
            'drivers':[m3_driver_record(c) for c in obj.animation_data.drivers] if obj.animation_data else [],
            'data_drivers':[m3_driver_record(c) for c in obj.data.animation_data.drivers] if obj.data and obj.data.animation_data else [],
            'bones':[]}
        if obj.type=='ARMATURE':
            for pose in obj.pose.bones:
                bone=pose.bone
                result['bones'].append({'name':bone.name,'parent':bone.parent.name if bone.parent else None,
                    'rest':[list(row) for row in bone.matrix_local],'head':list(bone.head_local),'tail':list(bone.tail_local),
                    'deform':bone.use_deform,'inherit_scale':bone.inherit_scale,'use_inherit_rotation':bone.use_inherit_rotation,
                    'channels':m3_channels(pose),'ik_stretch':pose.ik_stretch,
                    'ik_limits':[pose.use_ik_limit_x,pose.use_ik_limit_y,pose.use_ik_limit_z,
                        pose.ik_min_x,pose.ik_max_x,pose.ik_min_y,pose.ik_max_y,pose.ik_min_z,pose.ik_max_z],
                    'locks':[list(pose.lock_location),list(pose.lock_rotation),list(pose.lock_scale)],
                    'constraints':[m3_constraint_record(c) for c in pose.constraints]})
        return result
    finally:
        m3_restore(obj,state)

def m3_related_objects(obj,options):
    # Backends that evaluate other objects (e.g. a mocap source) redefine this.
    return []

def m3_context(obj,options):
    # Default backend context; backends redefine it later in the same payload.
    return m3_context_base(obj)

def m3_preflight(obj,options):
    related=[o for o in m3_related_objects(obj,options) if o is not None]
    prod_require_evaluable([obj]+related,['target']+['related']*len(related))

def m3_full_context(obj,options):
    # Backend context plus global evaluation state (pose position, IK solver, units, FPS...).
    context=m3_context(obj,options)
    state=m3_capture(obj)
    try:
        bpy.context.scene.frame_set(bpy.context.scene.frame_start)
        related=[o for o in m3_related_objects(obj,options) if o is not None]
        return {'backend':context,'global_state':prod_global_state([obj]+related)}
    finally:
        m3_restore(obj,state)

def m3_owner_channels(obj,state,path):
    # Maps an animated data path to the captured channel record that owns it, using Blender's own
    # escaped bone paths instead of parsing names.
    if obj.type=='ARMATURE':
        for pose in obj.pose.bones:
            prefix=pose.path_from_id()
            if path.startswith(prefix) and path[len(prefix):len(prefix)+1] in ('.','['):
                return pose,state['pose'].get(pose.name),path[len(prefix):]
    return obj,state['object'],('.'+path if not path.startswith('[') else path)

def m3_set_transform_channel(owner,name,value):
    # Literal attribute names only: safe mode forbids computed setattr.
    if name=='location':
        owner.location=value
    elif name=='scale':
        owner.scale=value
    elif name=='rotation_euler':
        owner.rotation_euler=value
    elif name=='rotation_quaternion':
        owner.rotation_quaternion=value
    elif name=='rotation_axis_angle':
        owner.rotation_axis_angle=value

def m3_restore_paths(obj,state,paths):
    """Restore only the static values of channels an operation keyed but the source never animated."""
    restored=[]
    for path in sorted(paths):
        owner,record,rest=m3_owner_channels(obj,state,path)
        if owner is None or record is None:
            continue
        if rest.startswith('.') and rest[1:] in ('location','scale','rotation_euler','rotation_quaternion','rotation_axis_angle'):
            m3_set_transform_channel(owner,rest[1:],record[rest[1:]]);restored.append(path)
        elif rest.startswith('[') and rest.endswith(']'):
            key=json.loads(rest[1:-1])
            if key in record['custom']:
                owner[key]=record['custom'][key];restored.append(path)
        elif rest.startswith('.constraints[') and rest.endswith('.influence'):
            cname=json.loads(rest[len('.constraints['):-len('].influence')])
            if cname in record['constraints'] and owner.constraints.get(cname):
                owner.constraints[cname].influence=record['constraints'][cname]['influence'];restored.append(path)
    return restored

def m3_written_static_paths(source,candidate):
    source_paths={c.data_path for s,l,t,c in action_curves(source,None)} if source else set()
    return {c.data_path for s,l,t,c in action_curves(candidate,None)}-source_paths

def m3_guard(obj,action,allowed_driver_paths=None):
    allowed=set(allowed_driver_paths or [])
    prod_require_pose_position(obj,'target')
    if obj.library or obj.override_library or (obj.data and (obj.data.library or obj.data.override_library)):
        raise ValueError('Linked/override rig unsupported')
    if obj.parent or obj.constraints:
        raise ValueError('Object parenting/constraints unsupported by this M3 mutation adapter')
    if action and (action.library or action.override_library):
        raise ValueError('Linked/override Action unsupported')
    data=obj.animation_data
    if data:
        if data.action!=action or data.use_tweak_mode or any(not t.mute and len(t.strips) for t in data.nla_tracks):
            raise ValueError('Active NLA/tweak or changed Action unsupported')
        if data.action_blend_type!='REPLACE' or abs(data.action_influence-1)>1e-7:
            raise ValueError('Partial/blended Action unsupported')
        for curve in data.drivers:
            d=curve.driver
            if curve.data_path not in allowed or d.type!='SCRIPTED' or d.use_self or curve.mute or len(d.variables)!=1:
                raise ValueError('Unvalidated rig driver: '+curve.data_path)
            variable=d.variables[0]
            if variable.type!='SINGLE_PROP' or len(variable.targets)!=1 or variable.targets[0].id!=obj:
                raise ValueError('Driver must use one local validated switch property')
            if d.expression.replace(' ','') not in {variable.name,'1-'+variable.name}:
                raise ValueError('Only direct/inverse local switch drivers supported')
    if obj.data and obj.data.animation_data and obj.data.animation_data.drivers:
        raise ValueError('Armature datablock drivers unsupported')
    if action:
        if getattr(action,'is_action_layered',False) and (len(action.slots)!=1 or len(action.layers)!=1 or len(action.layers[0].strips)!=1):
            raise ValueError('One Action slot/layer/strip required')
        for slot,layer,strip,curve in action_curves(action,m2_slot(obj)):
            if curve.modifiers or curve.sampled_points or curve.mute or curve.lock:
                raise ValueError('Muted/locked/modifier/sampled F-Curves unsupported')
            supported={'location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale'}
            if obj.type=='ARMATURE':
                for pose in obj.pose.bones:
                    supported.update(pose.path_from_id()+'.'+channel for channel in ['location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale'])
            if curve.data_path not in supported|allowed:
                raise ValueError('Animated RNA channel outside validated/restorable adapter: '+curve.data_path)

def m3_frames(options,maximum=1001):
    scene=bpy.context.scene
    start=options.get('frame_start',scene.frame_start)
    end=options.get('frame_end',scene.frame_end)
    step=options.get('step',1)
    if isinstance(start,bool) or isinstance(end,bool) or isinstance(step,bool) or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in [start,end,step]):
        raise ValueError('Frames/step must be finite numbers')
    if step<=0 or end<start or (end-start)/step+1>maximum:
        raise ValueError('Invalid or excessive sampling range')
    frames=[float(start+i*step) for i in range(int(math.floor((end-start)/step))+1)]
    if frames[-1]<end-1e-8:
        frames.append(float(end))
    return frames

def m3_set_frame(frame):
    base=math.floor(frame)
    bpy.context.scene.frame_set(base,subframe=frame-base)

def m3_source_snapshot(action):
    return m2_snapshot(action) if action else None
'''

BASE_RUNTIME = ACTION_HELPERS + ACTION_RUNTIME + DIGEST_RUNTIME + M3_RUNTIME

READ = r'''
obj=m2_object(params['object_name'])
m3_preflight(obj,params['options'])
source=obj.animation_data.action if obj.animation_data else None
state=m3_capture(obj)
try:
    proposal=m3_propose(obj,source,params['options'])
    if proposal.get('supported',False):
        m3_guard(obj,source,m3_allowed_drivers(obj,params['options']))
    result={'source_action':source.name if source else None,'source_snapshot':m3_source_snapshot(source),
        'source_slot':m2_slot(obj),'source_state':state,'context':m3_full_context(obj,params['options']),
        'proposal':proposal,'before':m2_curve_summary(source) if source else {'name':None,'keys':0,'channels':0}}
finally:
    m3_restore(obj,state)
'''

PREFLIGHT = r'''
obj=m2_object(params['object_name'])
m3_preflight(obj,params['options'])
source=obj.animation_data.action if obj.animation_data else None
m3_guard(obj,source,m3_allowed_drivers(obj,params['options']))
result={'matches':(source.name if source else None)==params['source_action'] and m2_digest(m3_source_snapshot(source))==params['source_fingerprint'] and m2_digest(m3_full_context(obj,params['options']))==params['context_fingerprint']}
'''

EXECUTE = r'''
obj=m2_object(params['object_name'])
m3_preflight(obj,params['options'])
transient=prod_transient_capture()
source=obj.animation_data.action if obj.animation_data else None
m3_guard(obj,source,m3_allowed_drivers(obj,params['options']))
if (source.name if source else None)!=params['source_action'] or m2_digest(m3_source_snapshot(source))!=params['source_fingerprint'] or m2_digest(m3_full_context(obj,params['options']))!=params['context_fingerprint']:
    raise ValueError('STALE_PROPOSAL: proposal source/context changed; analyze again')
state=m3_capture(obj);slot=m2_slot(obj);verified=False
candidate=source.copy() if source and params['proposal'].get('candidate_policy','duplicate')=='duplicate' else bpy.data.actions.new(obj.name+'__M3')
candidate.name=(source.name if source else obj.name)+'__M3_'+params['suffix'];candidate.use_fake_user=True
try:
    m3_assign(obj,candidate,slot if candidate.slots and source else None)
    changes=m3_apply(obj,source,candidate,params['proposal'],params['options'])
    verification=m3_verify(obj,source,candidate,params['proposal'],params['options'])
    intact=m2_digest(m3_source_snapshot(source))==params['source_fingerprint']
    verified=bool(verification.get('passed')) and intact
    result={'source_action':source.name if source else None,'working_action':candidate.name,
        'working_snapshot':m2_snapshot(candidate),'source_intact':intact,
        'before':params['before'],'after':m2_curve_summary(candidate),
        'verification':verification,'metrics':dict(verification.get('metrics',{}),**(changes or {})),
        'numeric_verification':'pass' if verified else 'fail','visual_verification':'required',
        'status':'warning' if verified else 'error','warnings':params['proposal'].get('warnings',[])+verification.get('warnings',[]),
        'errors':verification.get('errors',[]),
        'active_action':candidate.name if verified and params['mode']=='apply' else (source.name if source else None)}
finally:
    if not verified or params['mode']!='apply':
        m3_assign(obj,source,slot);m3_restore(obj,state)
        if source is None and not state['animation_data_existed']:
            obj.animation_data_clear()
    else:
        # Static channels return to their pre-operation values; animated ones re-evaluate from the
        # active candidate. No scratch value written while solving survives an apply.
        m3_restore(obj,state)
        m3_set_frame(state['frame']+state['subframe'])
    prod_transient_restore(transient)
changed=prod_transient_diff(transient,prod_transient_capture())
result['transient_context_restored']=not changed
if changed:
    result['status']='error';result['numeric_verification']='fail'
    result['errors']=result['errors']+['TRANSIENT_CONTEXT_CHANGED: '+', '.join(changed)]
'''

CHECK = r'''
obj=m2_object(params['object_name'])
m3_preflight(obj,params['options'])
transient=prod_transient_capture()
source=bpy.data.actions.get(params['source_action']) if params['source_action'] else None
candidate=bpy.data.actions.get(params['working_action'])
if candidate is None or (params['source_action'] and source is None):
    raise ValueError('CANDIDATE_MISSING: source/candidate Action missing; recover from checkpoint')
active=obj.animation_data.action if obj.animation_data else None;slot=m2_slot(obj);state=m3_capture(obj);reverted=False
source_intact=m2_digest(m3_source_snapshot(source))==params['source_fingerprint']
candidate_intact=m2_digest(m2_snapshot(candidate))==params['working_fingerprint']
written=m3_written_static_paths(source,candidate)
try:
    # The context is signed against the CURRENT user state (AUD-007). Only the static channels the
    # operation itself keyed go back to their proposal-time values; every other user change stays
    # visible to the fingerprint and makes the proposal stale.
    m3_assign(obj,source,params['source_slot'])
    m3_restore_paths(obj,params['source_state'],written)
    m3_guard(obj,source,m3_allowed_drivers(obj,params['options']))
    context_intact=m2_digest(m3_full_context(obj,params['options']))==params['context_fingerprint']
    if params['mode']=='revert':
        # Reassigning the preserved source is safe while the source itself is unchanged; later user
        # changes (context, candidate edits) are kept and reported, never undone (AUD-007).
        if not source_intact:
            raise ValueError('STALE_PROPOSAL: the source Action changed after the correction; automatic revert refused')
        if active not in (source,candidate):
            raise ValueError('UNRELATED_ACTION_ACTIVE: another Action is active; automatic revert refused')
        if source is None and not params['source_state']['animation_data_existed']:
            obj.animation_data_clear()
        # Revert restores animation data only; frame and unrelated user state are kept.
        m3_set_frame(state['frame']+state['subframe'])
        kept=[]
        if not context_intact:
            kept.append('USER_CONTEXT_KEPT: scene/rig state changed after the proposal; those changes were kept')
        if not candidate_intact:
            kept.append('CANDIDATE_EDITS_KEPT: the candidate Action changed after apply; it is preserved as edited')
        result={'reverted':True,'source_intact':True,'candidate_preserved':candidate.name,
            'active_action':source.name if source else None,'numeric_verification':params['numeric_verification'],
            'visual_verification':'required','status':'warning','metrics':params.get('metrics',{}),'warnings':kept,
            'context_changed_since_proposal':not context_intact,'candidate_changed_since_apply':not candidate_intact,
            'restored_static_channels':sorted(written)}
        reverted=True
    elif not source_intact or not candidate_intact or not context_intact:
        raise ValueError('STALE_PROPOSAL: source/candidate/context changed; automatic operation refused')
    else:
        m3_assign(obj,candidate)
        verification=m3_verify(obj,source,candidate,params['proposal'],params['options'])
        source_intact=m2_digest(m3_source_snapshot(source))==params['source_fingerprint']
        candidate_intact=m2_digest(m2_snapshot(candidate))==params['working_fingerprint']
        if not source_intact or not candidate_intact:
            verification['passed']=False
            verification.setdefault('errors',[]).append('Verification unexpectedly changed Action content; recover checkpoint')
        result={'source_intact':source_intact,'candidate_intact':candidate_intact,'verification':verification,
            'numeric_verification':'pass' if verification.get('passed') else 'fail','visual_verification':'required',
            'status':'warning' if verification.get('passed') else 'error','metrics':verification.get('metrics',{}),
            'warnings':verification.get('warnings',[]),'errors':verification.get('errors',[])}
finally:
    if not reverted:
        m3_assign(obj,active,slot);m3_restore(obj,state)
        if active is None and not state['animation_data_existed']:
            obj.animation_data_clear()
    prod_transient_restore(transient)
changed=prod_transient_diff(transient,prod_transient_capture())
result['transient_context_restored']=not changed
if changed:
    result['status']='error';result.setdefault('errors',[]).append('TRANSIENT_CONTEXT_CHANGED: '+', '.join(changed))
'''


def _public(data):
    hidden={'source_snapshot','working_snapshot','context','source_state','proposal_sha256'}
    result={k:v for k,v in data.items() if k not in hidden}
    if 'proposal' in result:
        result['proposal']={k:v for k,v in result['proposal'].items() if k not in {'samples','patches','targets','frames','source_tracks','private','state'}}
    return result


def _compact(data):
    result={k:v for k,v in data.items() if k not in {'source_snapshot','working_snapshot','context'}}
    if 'context' in data:
        result['context_fingerprint']=runtime_digest(data['context'])
    if 'working_snapshot' in data:
        result['working_fingerprint']=runtime_digest(data['working_snapshot'])
    return result


CHECK_FIELDS=('working_action','working_snapshot','working_fingerprint','numeric_verification')


def check_fields(previous):
    # CHECK re-reads Blender state; the event's verification details and metrics stay on the host
    # (they are merged back into revert results) so the payload never grows past the propose estimate.
    return dict({k:previous[k] for k in CHECK_FIELDS if k in previous},metrics={})


def payload_capacity(proposal,body,execute,check,params):
    """AUD-008: refuse at propose time a proposal whose apply/verify payload cannot pass the upstream
    safe-mode size limit (parameters travel inside the script), instead of failing at apply."""
    from blender_mcp.safe_mode import MAX_CODE_BYTES
    from .capacity import annotate
    if not proposal.get('supported'):
        if proposal.get('capacity'):
            annotate(proposal['capacity'],False)
        return
    placeholders={'mode':'verify','suffix':'0'*10,'working_action':'x'*256,'working_fingerprint':'m521:'+'f'*400,
                  'numeric_verification':'pass','metrics':{}}
    sizes={'apply':payload_bytes(body+execute,dict(params,mode='apply',suffix='0'*10)),
           'verify':payload_bytes(body+check,dict(params,**placeholders))}
    capacity=dict(proposal.get('capacity') or {})
    capacity.update(payload_bytes=sizes,payload_limit_bytes=MAX_CODE_BYTES)
    proposal['capacity']=capacity
    worst=max(sizes.values())
    if worst>MAX_CODE_BYTES:
        reason=('CAPACITY_EXCEEDED: the apply/verify payload would be '+str(worst)+' bytes; the safe-mode limit is '
                +str(MAX_CODE_BYTES)+' bytes because proposal data travels inside the script. Narrow the range or bones')
        proposal['supported']=False
        proposal.setdefault('reasons',[]).append(reason)
        proposal.setdefault('warnings',[]).append(reason)
    annotate(capacity,proposal['supported'])


def transaction(bridge, storage, operation_id, kind, runtime, object_name=None,
                mode='analyze', options=None, operation=None):
    """Immutable proposal, checkpoint, duplicate/new Action and bounded verification."""
    if mode not in {'analyze','propose','preview','apply','verify','revert'}:
        raise ValueError('Unknown M3 transaction mode')
    options={} if options is None else options
    _validate_options(options)
    body=BASE_RUNTIME+runtime
    if mode in {'analyze','propose'}:
        if not object_name or operation:
            raise ValueError('Analyze/propose requires object_name and no operation')
        data=bridge.run(body+READ,{'object_name':object_name,'options':options})
        data.update(kind=kind,object_name=object_name,options=options,
                    source_fingerprint=runtime_digest(data['source_snapshot']),
                    transaction_schema=1,backend_fingerprint=fingerprint(body))
        payload_capacity(data['proposal'],body,EXECUTE,CHECK,_compact(data))
        if mode=='propose':
            data.update(operation=save_proposal(storage,operation_id,data),correction_id=operation_id)
        return dict(_public(data),mode=mode,status='warning',numeric_verification='not_applied',
                    visual_verification='not_applied',warnings=data['proposal'].get('warnings',[]))
    if not operation or options:
        raise ValueError('Immutable operation required; options must remain empty')
    path,saved=load_proposal(storage,operation)
    if saved['kind']!=kind or (object_name and object_name!=saved['object_name']):
        raise ValueError('Operation type/target mismatch')
    if not saved['proposal'].get('supported'):
        raise ValueError('unsupported_or_ambiguous: proposal cannot be applied')
    if saved.get('transaction_schema')!=1 or saved.get('backend_fingerprint')!=fingerprint(body):
        raise ValueError('Operation backend changed; analyze/propose again')
    common={'operation':str(path),'correction_id':saved['operation_id'],'mode':mode,
            'target':{'object':saved['object_name'],'source_action':saved['source_action']}}
    if mode=='verify':
        previous=latest_candidate(path)
        data=bridge.run(body+CHECK,_compact({**saved,**check_fields(previous),'mode':mode}))
        data.update(**common,checkpoint=previous.get('checkpoint'),asset=None)
        return _public(_record_result(path,mode,data))
    previous=latest_candidate(path) if mode=='revert' else {}

    def execute(suffix,note):
        if mode!='revert' and not bridge.run(body+PREFLIGHT,_compact(saved))['matches']:
            raise ValueError('STALE_PROPOSAL: proposal is stale; analyze again')
        # AUD-014: persistent mutation only on a registered working copy / fixture / unsaved session.
        asset=require_mutable_asset(bridge,storage,kind+' '+mode)
        checkpoint=create_checkpoint(bridge,storage,'Before M3 '+kind+' '+mode,operation_id,kind+('/revert' if mode=='revert' else ''))['manifest']
        note(checkpoint=checkpoint)
        record_event(path,'intent',dict(common,checkpoint=checkpoint))
        try:
            if mode=='revert':
                data=bridge.run(body+CHECK,_compact({**saved,**check_fields(previous),'mode':mode}))
                data.setdefault('metrics',previous.get('metrics',{}))
                if not data['metrics']:
                    data['metrics']=previous.get('metrics',{})
            else:
                data=bridge.run(body+EXECUTE,_compact({**saved,'mode':mode,'suffix':suffix}))
            data.update(**common,checkpoint=checkpoint,asset=asset)
        except Exception as exc:
            if mode=='revert' and not is_uncertain(exc):
                raise  # Blender answered: CHECK refused before reassigning anything; a retry may run.
            data=dict(common,status='error',checkpoint=checkpoint,asset=asset,errors=[{'type':type(exc).__name__,'message':str(exc)}],
                numeric_verification='unverified',state_restored='unverified',
                warnings=['No automatic retry: inspect Blender, candidate and recovery checkpoint. '
                          'Retrying this same proposal reconciles instead of executing twice (AUD-010).'])
        return _public(_record_result(path,mode,data))

    def measure(candidate,working_fingerprint,checkpoint):
        # The response of the original EXECUTE was lost: re-measure the candidate it left behind.
        data=bridge.run(body+CHECK,_compact({**saved,'working_action':candidate,'working_fingerprint':working_fingerprint,
                                             'numeric_verification':'unverified','metrics':{},'mode':'verify'}))
        record_event(path,mode,dict(common,working_action=candidate,working_fingerprint=working_fingerprint,
            source_action=saved['source_action'],checkpoint=checkpoint,reconciled=True,
            numeric_verification=data.get('numeric_verification'),metrics=data.get('metrics',{})))
        data.update(**common,checkpoint=checkpoint,working_action=candidate,asset=None)
        return _public(data)

    return guarded_cycle(bridge,storage,tool=kind,correction_id=saved['operation_id'],mode=mode,
        request={'operation':str(path),'kind':kind,'mode':mode},body=body,object_name=saved['object_name'],
        source_action=saved['source_action'],candidate_prefix=(saved['source_action'] or saved['object_name'])+'__M3_',
        suffix_length=8,previous_candidate=previous.get('working_action'),execute=execute,measure=measure)
