"""Evidence-based rig semantics; inference and mutation compatibility are distinct."""
from __future__ import annotations

from ..common.m3_transaction import BASE_RUNTIME

SEMANTIC_RUNTIME = r'''
import re

def m3_name_tokens(name):
    return re.sub('[^a-z0-9]+',' ',name.lower()).split()

def m3_score_first(row):
    return row[0]

def m3_score_assignment(row):
    return row['confidence']

def m3_side(bone):
    tokens=m3_name_tokens(bone.name)
    if any(t in tokens for t in ['left','l']):
        return 'left',0.96,['Naming side token combined with hierarchy/rest position']
    if any(t in tokens for t in ['right','r']):
        return 'right',0.96,['Naming side token combined with hierarchy/rest position']
    if abs(bone.head_local.x)>1e-5:
        return ('left' if bone.head_local.x>0 else 'right'),0.76,['Armature-local X side convention; anatomical side unconfirmed']
    return 'unknown',0.2,['No reliable side signal']

def m3_descendants(bone):
    queue=list(bone.children);result=[]
    while queue:
        child=queue.pop(0);result.append(child);queue.extend(child.children)
    return result

def m3_switch(obj,constraint):
    path=constraint.path_from_id('influence')
    curves=[c for c in obj.animation_data.drivers if c.data_path==path] if obj.animation_data else []
    if len(curves)!=1:
        return None
    curve=curves[0];driver=curve.driver
    if driver.type!='SCRIPTED' or driver.use_self or len(driver.variables)!=1:
        return {'path':path,'supported':False,'confidence':0.4,'driver':m3_driver_record(curve)}
    variable=driver.variables[0]
    target=variable.targets[0]
    direct=driver.expression.replace(' ','')
    valid=variable.type=='SINGLE_PROP' and target.id==obj and direct in {variable.name,'1-'+variable.name}
    try:
        value=obj.path_resolve(target.data_path)
    except (ValueError,TypeError,AttributeError):
        value=None;valid=False
    return {'path':target.data_path,'driver_path':path,'value':value,'supported':valid,
        'expression':driver.expression,'ik_value':1 if direct==variable.name else 0,
        'fk_value':0 if direct==variable.name else 1,'confidence':0.98 if valid else 0.4,
        'driver':m3_driver_record(curve)}

def m3_limb_record(obj,chain,kind,side,confidence,evidence):
    poses=[obj.pose.bones[name] for name in chain]
    ik=[(pose,c) for pose in poses[:2] for c in pose.constraints if c.type=='IK']
    active=[(pose,c) for pose,c in ik if not c.mute and c.influence>1e-6]
    switches=[m3_switch(obj,c) for pose,c in ik]
    switches=[s for s in switches if s]
    mode='IK' if active and all(abs(c.influence-1)<1e-6 for p,c in active) else 'MIXED' if active else 'FK'
    if any(p.constraints for p in poses[:2]) and not ik:
        mode='UNKNOWN'
    controls=[c.subtarget for p,c in ik if c.target==obj and c.subtarget]
    poles=[c.pole_subtarget for p,c in ik if c.pole_target==obj and c.pole_subtarget]
    return {'limb':side+'_'+kind,'chain':chain,'current_mode':mode,
        'classification':'switchable_ik_fk' if any(s.get('supported') for s in switches) else 'ik_driven' if active else 'fk_driven' if mode=='FK' else 'unknown',
        'ik_controls':controls,'fk_controls':chain[:2],'pole_targets':poles,
        'switches':switches,'constraints':[{'owner':p.name,'name':c.name,'type':c.type,'influence':c.influence,'mute':c.mute} for p in poses for c in p.constraints],
        'confidence':min(confidence,0.98 if ik else 0.86),'evidence':evidence+['Explicit constraint and deform hierarchy analysis'],
        'alternatives':[]}

def m3_semantic_model(obj,options=None):
    options=options or {}
    if obj.type!='ARMATURE':
        raise ValueError('Rig semantics requires an armature')
    if len(obj.data.bones)>2048:
        raise ValueError('Rig exceeds 2048 bones; explicit reduced analysis required')
    bones=list(obj.data.bones);deform=[b for b in bones if b.use_deform]
    candidates={};limbs=[];issues=[]
    def add(role,bone,confidence,evidence):
        if bone:
            candidates.setdefault(role,[]).append({'bone':bone.name,'confidence':round(confidence,3),'evidence':evidence})
    extent=max([b.head_local.length for b in bones]+[1e-6])
    roots=[b for b in bones if b.parent is None]
    for bone in roots:
        score=0.5+0.25*min(1,len(m3_descendants(bone))/max(1,len(deform)))
        if abs(bone.head_local.x)<extent*.03:
            score+=0.1
        if any(t in m3_name_tokens(bone.name) for t in ['root','motionbase','master']):
            score+=0.12
        add('root',bone,min(.98,score),['Hierarchy root','Central rest position and descendant coverage'])
    pelvis_candidates=[]
    for bone in deform:
        branches=[c for c in bone.children if c.use_deform]
        down=[c for c in branches if c.tail_local.z<bone.head_local.z-extent*.05]
        up=[c for c in branches if c.tail_local.z>bone.head_local.z+extent*.02]
        paired=len(down)>=2 and any(a.head_local.x*b.head_local.x<0 for a in down for b in down)
        score=.42+.36*paired+.1*bool(up)
        if any(t in m3_name_tokens(bone.name) for t in ['hips','hip','pelvis']):
            score+=.1
        if paired:
            pelvis_candidates.append((score,bone,down,up))
            add('pelvis',bone,min(.98,score),['Bilateral descending deform branches','Rest-pose vertical/hierarchy structure']+(['Ascending torso branch'] if up else []))
    pelvis_candidates.sort(key=m3_score_first,reverse=True)
    if pelvis_candidates:
        _,pelvis,legs,up=pelvis_candidates[0]
        for upper in legs:
            children=[b for b in upper.children if b.use_deform]
            if len(children)!=1:
                continue
            lower=children[0];ends=[b for b in lower.children if b.use_deform]
            if len(ends)!=1:
                continue
            foot=ends[0];side,score,evidence=m3_side(upper)
            if side=='unknown':
                continue
            chain=[upper.name,lower.name,foot.name]
            for role,bone in [('upper_leg',upper),('lower_leg',lower),('foot',foot)]:
                add(side+'_'+role,bone,min(score,.94),evidence+['Connected three-bone descending limb below pelvis'])
            toes=[b for b in foot.children if b.use_deform]
            if len(toes)==1:
                add(side+'_toe',toes[0],min(score,.9),evidence+['Single distal child of inferred foot'])
            limbs.append(m3_limb_record(obj,chain,'leg',side,min(score,.94),evidence))
        if len(up)==1:
            spine=up[0];spine_chain=[spine]
            while True:
                children=[b for b in spine.children if b.use_deform and b.tail_local.z>spine.tail_local.z and abs(b.head_local.x)<extent*.12]
                if len(children)!=1:
                    break
                spine=children[0];spine_chain.append(spine)
            for index,bone in enumerate(spine_chain):
                role='head' if index==len(spine_chain)-1 and len(spine_chain)>1 else 'neck' if index==len(spine_chain)-2 and len(spine_chain)>2 else 'spine' if index==0 else 'spine_'+str(index)
                add(role,bone,.88,['Central ascending deform chain above pelvis','Rest-pose hierarchy'])
                branches=[c for c in bone.children if c.use_deform and c not in spine_chain and abs(c.tail_local.x)>abs(c.head_local.x)+extent*.03]
                for upper in branches:
                    kids=[c for c in upper.children if c.use_deform]
                    if len(kids)!=1:
                        continue
                    lower=kids[0];hands=[c for c in lower.children if c.use_deform]
                    if len(hands)!=1:
                        continue
                    hand=hands[0];side,score,evidence=m3_side(upper)
                    if side=='unknown':
                        continue
                    for role,part in [('upper_arm',upper),('lower_arm',lower),('hand',hand)]:
                        add(side+'_'+role,part,min(score,.93),evidence+['Lateral three-bone torso branch'])
                    limbs.append(m3_limb_record(obj,[upper.name,lower.name,hand.name],'arm',side,min(score,.93),evidence))
    # Explicit semantic mapping is a reviewed adapter, never inferred silently.
    explicit=options.get('mapping',{})
    if not isinstance(explicit,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in explicit.items()):
        raise ValueError('mapping must map semantic roles to exact bone names')
    for role,name in explicit.items():
        bone=obj.data.bones.get(name)
        if bone is None:
            raise ValueError('Mapped bone does not exist: '+name)
        add(role,bone,1.0,['Explicit reviewed semantic mapping'])
    assignments=[]
    for role,choices in sorted(candidates.items()):
        choices.sort(key=m3_score_assignment,reverse=True)
        selected=dict(choices[0]);selected['semantic']=role;selected['alternatives']=choices[1:4]
        if len(choices)>1 and choices[0]['bone']!=choices[1]['bone'] and choices[0]['confidence']-choices[1]['confidence']<.1:
            selected['confidence']=min(selected['confidence'],.6)
            selected['evidence'].append('Ambiguous competing assignment')
        selected_bone=obj.data.bones[selected['bone']]
        selected['rest_head_local']=list(selected_bone.head_local)
        selected['rest_head_world']=list(obj.matrix_world@selected_bone.head_local)
        assignments.append(selected)
    by_role={row['semantic']:row for row in assignments}
    for row in assignments:
        if row['semantic'].startswith('left_'):
            opposite=by_role.get('right_'+row['semantic'][5:])
            if opposite:
                left=obj.data.bones[row['bone']];right=obj.data.bones[opposite['bone']]
                mirrored=left.head_local.copy();mirrored.x=-mirrored.x
                symmetry_error=(mirrored-right.head_local).length/max(extent,1e-6)
                if symmetry_error<.03 and abs(left.length-right.length)/max(left.length,right.length,1e-6)<.1:
                    for assignment in [row,opposite]:
                        assignment['evidence'].append('Bilateral rest-position and limb-length symmetry')
                        assignment['symmetry_relative_error']=symmetry_error
    targets=set();ik_targets=set();poles=set();mechanism=set();constraints=[]
    for p in obj.pose.bones:
        for c in p.constraints:
            constraints.append({'owner':p.name,'name':c.name,'type':c.type,'influence':c.influence,'muted':c.mute,'valid':c.is_valid,
                'target':c.target.name if hasattr(c,'target') and c.target else None,
                'subtarget':c.subtarget if hasattr(c,'subtarget') else None})
            if hasattr(c,'target') and c.target==obj and hasattr(c,'subtarget') and c.subtarget:
                targets.add(c.subtarget)
            if c.type=='IK':
                if c.target==obj and c.subtarget:
                    ik_targets.add(c.subtarget)
                if c.pole_target==obj and c.pole_subtarget:
                    poles.add(c.pole_subtarget)
                if not p.bone.use_deform:
                    mechanism.add(p.name)
    controls=[]
    animated_paths={c.data_path for s,l,t,c in action_curves(obj.animation_data.action,m2_slot(obj))} if obj.animation_data and obj.animation_data.action else set()
    for p in obj.pose.bones:
        evidence=[];role='deform' if p.bone.use_deform else 'helper_unknown';confidence=.5
        if p.name in targets:
            role='ik_target' if p.name in ik_targets else 'constraint_target'
            evidence.append('Explicit constraint target relationship');confidence=.99
        if p.name in poles:
            role='pole_target';evidence.append('Explicit IK pole target relationship');confidence=.99
        if p.name in mechanism:
            role='mechanism';evidence.append('Nondeform constrained solver bone');confidence=.9
        if any(t in m3_name_tokens(p.name) for t in ['twist','roll']) and p.bone.use_deform:
            evidence.append('Twist naming candidate; requires deformation/driver validation')
        if p.custom_shape:
            evidence.append('Custom control shape');confidence=max(confidence,.7)
        controls.append({'bone':p.name,'role':role,'confidence':confidence,'evidence':evidence,'alternatives':[],
            'custom_properties':m3_properties(p),'rest_head_local':list(p.bone.head_local),
            'rest_head_world':list(obj.matrix_world@p.bone.head_local),
            'animated':any(path.startswith(p.path_from_id()) for path in animated_paths)})
    modes={limb['current_mode'] for limb in limbs}
    classification='mixed_ik_fk' if 'IK' in modes and 'FK' in modes else 'switchable_ik_fk' if any(l['switches'] for l in limbs) else 'ik_control_rig' if 'IK' in modes else 'deform_only' if limbs and all(b.use_deform for b in bones) else 'fk_control_rig' if limbs and modes=={'FK'} else 'unknown'
    if not limbs:
        issues.append({'code':'unknown_limb_topology','severity':'unsupported','message':'No unambiguous supported biped limb topology inferred'})
    return {'armature':obj.name,'bone_count':len(bones),'deform_bone_count':len(deform),
        'classification':classification,'confidence':min([l['confidence'] for l in limbs],default=.2),
        'assignments':assignments,'limbs':limbs,'controls':controls,'constraints':constraints,
        'drivers':[m3_driver_record(c) for c in obj.animation_data.drivers] if obj.animation_data else [],
        'custom_properties':m3_properties(obj),'issues':issues,
        'evidence_scope':{'rest_space':'armature local; world coordinates reported','animation':'channel presence and current evaluated solver mode; not inferred motion purpose'},
        'warnings':['Confidence is an evidence score, not a calibrated probability. Non-biped/rotated/ambiguous structures may remain unknown.',
            'Anatomical side inferred from unnamed bones uses local X convention; confirm before mutation.']}
'''


def analyze(bridge, object_name, options=None, kind='semantics'):
    options={} if options is None else options
    if not isinstance(options,dict) or set(options)-{'mapping','limit','offset','detail'}:
        raise ValueError('Unknown rig analysis options')
    limit=options.get('limit',100);offset=options.get('offset',0)
    if type(limit) is not int or not 1<=limit<=500 or type(offset) is not int or offset<0:
        raise ValueError('Invalid rig analysis pagination')
    if kind not in {'semantics','classify','controls','limbs','control_mode','compatibility'}:
        raise ValueError('Unknown rig analysis kind')
    body=BASE_RUNTIME+SEMANTIC_RUNTIME+r'''
obj=m2_object(params['object_name'])
model=m3_semantic_model(obj,params['options'])
kind=params['kind'];limit=params['limit'];offset=params['offset']
result={'armature':model['armature'],'classification':model['classification'],'confidence':model['confidence'],
    'warnings':model['warnings'],'issues':model['issues'],'bone_count':model['bone_count']}
collections={'semantics':['assignments','limbs'],'classify':['limbs'],'controls':['controls','drivers','custom_properties'],
    'limbs':['limbs'],'control_mode':['limbs','drivers'],'compatibility':['limbs']}[kind]
result['pagination']={'limit':limit,'offset':offset,'totals':{}}
for name in collections:
    value=model[name]
    result[name]=value[offset:offset+limit] if isinstance(value,list) else value
    result['pagination']['totals'][name]=len(value)
if kind=='compatibility':
    reasons=[]
    if obj.parent or obj.constraints:
        reasons.append('Object parents/constraints require a separate adapter')
    if obj.animation_data and any(not t.mute and t.strips for t in obj.animation_data.nla_tracks):
        reasons.append('Active NLA composition is unsupported for automatic mutation')
    if obj.library or obj.override_library:
        reasons.append('Linked/override rig is read-only')
    known=bool(model['limbs']) and not reasons
    result['capabilities']={'semantic_mapping':'supported_with_conditions' if model['assignments'] else 'unknown',
        'ik_detection':'supported','fk_detection':'supported_with_conditions',
        'ik_fk_matching':'explicit_adapter_required' if known else 'unsupported_or_ambiguous',
        'foot_locking':'explicit_adapter_required' if known else 'unsupported_or_ambiguous',
        'mocap_retarget':'validated_fk_mapping_required' if known else 'unsupported_or_ambiguous',
        'deformation_qa':'supported_with_conditions'}
    result['limitations']=reasons+['Capability inference is advisory; each operation validates exact adapter, Action and current state.']
    result['confidence_by_capability']={name:model['confidence'] for name in result['capabilities']}
'''
    return bridge.run(body,{'object_name':object_name,'options':options,'kind':kind,'limit':limit,'offset':offset})
