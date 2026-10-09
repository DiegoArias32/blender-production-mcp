"""Narrow, evidence-based body-animation adapter executed by Blender.

The upstream transport and M3 transaction remain authoritative. This module
supplies a backend, never writes an existing source Action and never changes a
rig, rotation mode, solver, rest pose, modifier or deformation weight.
"""
from __future__ import annotations

import ast
from functools import lru_cache

from ...common.fcurve_window import WINDOW_RUNTIME
from .integrity import INTEGRITY_RUNTIME
from .window import WINDOW_RUNTIME as GENERATION_WINDOW_RUNTIME
from .pose_policy import POSE_BASELINE_RUNTIME


GENERATION_RUNTIME = INTEGRITY_RUNTIME + WINDOW_RUNTIME + GENERATION_WINDOW_RUNTIME + POSE_BASELINE_RUNTIME + r'''
from mathutils import Euler

def m6_number(value, label, lower=None, upper=None):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('INVALID_GENERATION_VALUE: '+label+' must be finite')
    if lower is not None and value<lower or upper is not None and value>upper:
        raise ValueError('INVALID_GENERATION_VALUE: '+label+' outside documented range')
    return float(value)

def m6_vector(value,label):
    if not isinstance(value,(list,tuple)) or len(value)!=3:
        raise ValueError('INVALID_GENERATION_VECTOR: '+label)
    return Vector([m6_number(x,label) for x in value])

def m6_validate_options(options):
    allowed={'brief','plan','mapping','stage','generation_id','candidate_name','ground_height_m',
        'contact_tolerance_m','sliding_tolerance_m','maximum_angular_step_deg','meshes',
        'operation_id','created_utc','mcp_version','generation_ref','brief_ref',
        'pose','pose_index','action_name','frame','delay_frames','strength','roles','chain','phase','phases',
        'damping','duration_frames','duration_s','preserve_impact','effector',
        'contact_height_m','contact_speed_m_s',
        'root_position_tolerance_m','root_angle_tolerance_deg','root_max_step_m','root_vertical_tolerance_m',
        'loop_position_tolerance_m','loop_angle_tolerance_deg','loop_velocity_tolerance_m_s','loop_angular_velocity_tolerance_deg_s'}
    if not isinstance(options,dict):
        raise ValueError('INVALID_GENERATION_OPTIONS: dictionary required')
    if set(options)-allowed:
        raise ValueError('UNSUPPORTED_GENERATION_OPTIONS: '+', '.join(sorted(set(options)-allowed)))
    qa_thresholds={'root_position_tolerance_m','root_angle_tolerance_deg','root_max_step_m','root_vertical_tolerance_m',
        'loop_position_tolerance_m','loop_angle_tolerance_deg','loop_velocity_tolerance_m_s','loop_angular_velocity_tolerance_deg_s'}
    for field in qa_thresholds:
        if field in options:
            m6_number(options[field],field,0)
    stage=options.get('stage','').upper()
    secondary={'delay_frames','strength','roles','chain','phase','phases','damping','duration_frames','duration_s','preserve_impact'}
    if stage and stage not in ('OVERLAP','FOLLOW_THROUGH') and set(options)&secondary:
        raise ValueError('SECONDARY_OPTIONS_STAGE_MISMATCH: these options apply only to overlap/follow-through')
    if stage=='OVERLAP' and set(options)&{'damping','duration_frames','duration_s','preserve_impact'}:
        raise ValueError('FOLLOW_THROUGH_OPTIONS_STAGE_MISMATCH')
    if stage and stage!='EFFECTOR' and 'effector' in options:
        raise ValueError('EFFECTOR_OPTIONS_STAGE_MISMATCH: use the explicit hand-contact stage')

def m6_role(role):
    aliases={'left_upper_arm':'upper_arm_l','left_lower_arm':'lower_arm_l','left_hand':'hand_l',
        'right_upper_arm':'upper_arm_r','right_lower_arm':'lower_arm_r','right_hand':'hand_r',
        'left_upper_leg':'upper_leg_l','left_lower_leg':'lower_leg_l','left_foot':'foot_l',
        'right_upper_leg':'upper_leg_r','right_lower_leg':'lower_leg_r','right_foot':'foot_r',
        'left_foot_control':'foot_control_l','right_foot_control':'foot_control_r',
        'left_pole':'pole_l','right_pole':'pole_r'}
    return aliases.get(role,role)

def m6_mapping(obj,options):
    explicit=options.get('mapping',{})
    if not isinstance(explicit,dict):
        raise ValueError('INVALID_MAPPING: semantic roles must be a dictionary')
    inferred=m6_infer_mapping(obj)
    model={'inference':'Rest hierarchy, branch direction, bilateral geometry and corroborated side tokens'}
    mapping=dict(inferred)
    caller_confidence={}
    for raw_role,value in explicit.items():
        role=m6_role(raw_role)
        if isinstance(value,dict):
            name=value.get('bone',value.get('name'))
            confidence=value.get('confidence',1)
            caller_confidence[role]=m6_number(confidence,'mapping confidence',0,1)
        else:
            name=value
        if not isinstance(name,str) or obj.pose.bones.get(name) is None:
            raise ValueError('MAPPED_BONE_MISSING: '+str(raw_role))
        mapping[role]=name
    # Controls are identified by real IK constraints, never by caller labels.
    for side in ('l','r'):
        lower=obj.pose.bones.get(mapping.get('lower_leg_'+side,''))
        if lower:
            rows=[c for c in lower.constraints if c.type=='IK' and c.target==obj and c.subtarget]
            if len(rows)==1:
                mapping.setdefault('foot_control_'+side,rows[0].subtarget)
                if rows[0].pole_target==obj and rows[0].pole_subtarget:
                    mapping.setdefault('pole_'+side,rows[0].pole_subtarget)
    return mapping,caller_confidence,model

def m6_infer_mapping(obj):
    # Compact first-pass biped detection. Rotated or ambiguous rigs require an
    # explicit mapping, whose structural confidence is still computed below.
    bones=list(obj.data.bones);mapping={};pelvis_rows=[]
    for bone in bones:
        if not bone.use_deform:
            continue
        down=[c for c in bone.children if c.use_deform and c.tail_local.z<bone.head_local.z-.05]
        up=[c for c in bone.children if c.use_deform and c.tail_local.z>bone.head_local.z+.05]
        if len(down)==2 and down[0].head_local.x*down[1].head_local.x<0 and len(up)==1:
            pelvis_rows.append((bone,down,up[0]))
    if len(pelvis_rows)!=1:
        return mapping
    pelvis,legs,spine=pelvis_rows[0];root=pelvis
    while root.parent:
        root=root.parent
    mapping.update(root=root.name,pelvis=pelvis.name,spine=spine.name)
    for upper in legs:
        tokens=upper.name.lower().replace('_','.').replace('-','.').split('.')
        side='l' if any(t in ('l','left') for t in tokens) and upper.head_local.x>0 else 'r' if any(t in ('r','right') for t in tokens) and upper.head_local.x<0 else None
        lower=[c for c in upper.children if c.use_deform]
        foot=[c for c in lower[0].children if c.use_deform] if len(lower)==1 else []
        if side and len(lower)==1 and len(foot)==1:
            mapping.update({'upper_leg_'+side:upper.name,'lower_leg_'+side:lower[0].name,'foot_'+side:foot[0].name})
    torso=spine;head=spine;visited=set()
    while torso.name not in visited:
        visited.add(torso.name)
        branches=[c for c in torso.children if c.use_deform and abs(c.tail_local.x)>abs(c.head_local.x)+.05]
        if len(branches)==2:
            mapping['chest']=torso.name
            for upper in branches:
                tokens=upper.name.lower().replace('_','.').replace('-','.').split('.')
                side='l' if any(t in ('l','left') for t in tokens) and upper.head_local.x>0 else 'r' if any(t in ('r','right') for t in tokens) and upper.head_local.x<0 else None
                lower=[c for c in upper.children if c.use_deform];hand=[c for c in lower[0].children if c.use_deform] if len(lower)==1 else []
                if side and len(lower)==1 and len(hand)==1:
                    mapping.update({'upper_arm_'+side:upper.name,'lower_arm_'+side:lower[0].name,'hand_'+side:hand[0].name})
        central=[c for c in torso.children if c.use_deform and c.tail_local.z>torso.tail_local.z and abs(c.tail_local.x)<.1]
        if len(central)!=1:
            break
        torso=central[0];head=torso
    mapping['head']=head.name
    return mapping

def m6_descends(child,ancestor):
    current=child
    for i in range(2048):
        if current==ancestor:
            return True
        current=current.parent
        if current is None:
            return False
    return False

def m6_basis(obj,mapping,options):
    pelvis=obj.data.bones[mapping['pelvis']]
    chest=obj.data.bones[mapping['chest']]
    left=obj.data.bones[mapping['upper_leg_l']]
    right=obj.data.bones[mapping['upper_leg_r']]
    up=chest.tail_local-pelvis.head_local
    lateral=left.head_local-right.head_local
    if up.length<1e-6 or lateral.length<1e-6:
        raise ValueError('BODY_BASIS_AMBIGUOUS: distinct torso and bilateral legs required')
    up.normalize();lateral=lateral-up*lateral.dot(up)
    if lateral.length<1e-6:
        raise ValueError('BODY_BASIS_AMBIGUOUS: collinear torso and leg-side axes')
    lateral.normalize();forward=up.cross(lateral).normalized()
    axis=options.get('brief',{}).get('forward_axis','+Y')
    if axis not in ('+Y','-Y'):
        raise ValueError('FORWARD_AXIS_UNSUPPORTED: explicit +Y or -Y required')
    if axis=='-Y':
        forward=-forward
    return Matrix((lateral,forward,up)).transposed()

def m6_compatibility(obj,options):
    reasons=[];mapping={};evidence=[];limbs=[];confidence=0.0
    try:
        m6_validate_options(options)
        if obj.type!='ARMATURE':
            raise ValueError('ARMATURE_REQUIRED')
        if len(obj.data.bones)>2048:
            raise ValueError('CAPACITY_EXCEEDED: maximum 2048 rig bones')
        if any(b.bbone_segments>1 for b in obj.data.bones):
            raise ValueError('BBONE_GENERATION_ADAPTER_REQUIRED: dynamic segmented B-Bone pose dependencies unsupported')
        if obj.parent or obj.constraints or obj.library or obj.override_library:
            raise ValueError('UNSUPPORTED_RIG: object parenting/constraints/linked data')
        prod_require_pose_position(obj,'generation target')
        mapping,caller,model=m6_mapping(obj,options)
        required=['root','pelvis','spine','chest','head']
        required+=['upper_arm_'+s for s in ('l','r')]+['lower_arm_'+s for s in ('l','r')]+['hand_'+s for s in ('l','r')]
        required+=['upper_leg_'+s for s in ('l','r')]+['lower_leg_'+s for s in ('l','r')]+['foot_'+s for s in ('l','r')]
        missing=[r for r in required if r not in mapping]
        if missing:
            raise ValueError('SEMANTIC_MAPPING_REQUIRED: '+', '.join(missing))
        if len({mapping[r] for r in required})!=len(required):
            raise ValueError('AMBIGUOUS_CONTROLS: anatomical roles must have distinct bones')
        root=obj.pose.bones[mapping['root']];pelvis=obj.pose.bones[mapping['pelvis']]
        if root.parent or not m6_descends(pelvis,root):
            raise ValueError('INVALID_ROOT: parentless root must own the pelvis hierarchy')
        for parent,child in [('pelvis','spine'),('spine','chest'),('chest','head')]:
            if not m6_descends(obj.pose.bones[mapping[child]],obj.pose.bones[mapping[parent]]):
                raise ValueError('SEMANTIC_STRUCTURE_MISMATCH: '+child+' must descend from '+parent)
        basis=m6_basis(obj,mapping,options)
        mapped_fk=[root,pelvis]+[obj.pose.bones[mapping[r]] for r in ('spine','chest','head')]
        protected=set()
        for side in ('l','r'):
            upper=obj.pose.bones[mapping['upper_leg_'+side]];lower=obj.pose.bones[mapping['lower_leg_'+side]];foot=obj.pose.bones[mapping['foot_'+side]]
            if lower.parent!=upper or foot.parent!=lower or not m6_descends(upper,pelvis):
                raise ValueError('LEG_CHAIN_MISMATCH: '+side)
            if (basis.transposed()@(upper.bone.head_local-pelvis.bone.head_local)).x*(1 if side=='l' else -1)<.001:
                raise ValueError('SIDE_MAPPING_MISMATCH: leg '+side)
            solvers=[c for c in lower.constraints if c.type=='IK']
            if solvers:
                if len(solvers)!=1:
                    raise ValueError('AMBIGUOUS_IK_CONSTRAINTS: '+side)
                solver=solvers[0]
                target=obj.pose.bones.get(mapping.get('foot_control_'+side,''));pole=obj.pose.bones.get(mapping.get('pole_'+side,''))
                if target is None or pole is None or target==pole or target in (upper,lower,foot) or pole in (upper,lower,foot):
                    raise ValueError('IK_CONTROL_MAPPING_REQUIRED: '+side)
                if solver.target!=obj or solver.subtarget!=target.name or solver.pole_target!=obj or solver.pole_subtarget!=pole.name:
                    raise ValueError('IK_CONTROL_RELATION_MISMATCH: '+side)
                if solver.chain_count!=2 or solver.use_stretch or not solver.use_tail or solver.use_rotation or solver.mute or abs(solver.influence-1)>1e-7:
                    raise ValueError('CONFLICTING_IK_FK: full-influence nonstretch two-bone tail IK required')
                if upper.constraints or any(c!=solver for c in lower.constraints):
                    raise ValueError('AMBIGUOUS_LEG_CONTROLS: '+side)
                for control in (target,pole):
                    if control.parent or control.constraints or control.bone.use_deform or any(control.lock_location):
                        raise ValueError('UNSUPPORTED_IK_CONTROL: independent unlocked nondeform controls required')
                if any(target.lock_rotation):
                    raise ValueError('LOCKED_IK_ROTATION: '+target.name)
                for constraint in foot.constraints:
                    if constraint.type!='COPY_ROTATION' or constraint.target!=obj or constraint.subtarget!=target.name or constraint.mix_mode!='REPLACE' or constraint.owner_space!='POSE' or constraint.target_space!='POSE' or constraint.mute or abs(constraint.influence-1)>1e-7:
                        raise ValueError('UNSUPPORTED_FOOT_CONSTRAINT: '+foot.name)
                if any(p.ik_stretch>1e-7 or p.use_ik_limit_x or p.use_ik_limit_y or p.use_ik_limit_z for p in (upper,lower)):
                    raise ValueError('IK_LIMIT_ADAPTER_REQUIRED: '+side)
                if target.name in protected or pole.name in protected:
                    raise ValueError('AMBIGUOUS_CONTROLS: shared leg target/pole')
                protected.update([target.name,pole.name])
                limbs.append({'side':side,'kind':'leg','mode':'IK','chain':[upper.name,lower.name,foot.name],
                    'target':target.name,'pole':pole.name,'constraint':solver.name,'confidence':.98})
            else:
                if upper.constraints or lower.constraints or foot.constraints:
                    raise ValueError('UNSUPPORTED_FK_LEG_CONTROLS: '+side)
                mapped_fk.extend([upper,lower,foot])
                limbs.append({'side':side,'kind':'leg','mode':'FK','chain':[upper.name,lower.name,foot.name],
                    'confidence':.92,'foot_lock_supported':False})
            arm=[obj.pose.bones[mapping[r+'_'+side]] for r in ('upper_arm','lower_arm','hand')]
            if arm[1].parent!=arm[0] or arm[2].parent!=arm[1] or not m6_descends(arm[0],obj.pose.bones[mapping['chest']]):
                raise ValueError('ARM_CHAIN_MISMATCH: '+side)
            if (basis.transposed()@(arm[0].bone.head_local-pelvis.bone.head_local)).x*(1 if side=='l' else -1)<.001:
                raise ValueError('SIDE_MAPPING_MISMATCH: arm '+side)
            if any(p.constraints for p in arm):
                raise ValueError('AMBIGUOUS_ARM_CONTROLS: explicit FK-only arm adapter required')
            mapped_fk.extend(arm)
            limbs.append({'side':side,'kind':'arm','mode':'FK','chain':[p.name for p in arm],'confidence':.94})
        for pose in mapped_fk:
            if pose.constraints or any(pose.lock_rotation) or any(pose.lock_location):
                raise ValueError('LOCKED_OR_CONSTRAINED_FK_CONTROL: '+pose.name)
            if pose.rotation_mode=='AXIS_ANGLE':
                raise ValueError('AXIS_ANGLE_ADAPTER_REQUIRED: '+pose.name)
        # A different control driving any anatomical FK bone makes direct
        # deformation-channel keying unsafe. Nothing is deleted to simplify it.
        for pose in obj.pose.bones:
            for constraint in pose.constraints:
                if hasattr(constraint,'target') and constraint.target==obj and hasattr(constraint,'subtarget') and constraint.subtarget in [p.name for p in mapped_fk]:
                    evidence.append('Mapped FK bone is a constraint target: '+constraint.subtarget)
        data=obj.animation_data
        if data and data.drivers:
            raise ValueError('DRIVEN_GENERATION_ADAPTER_REQUIRED: M6 does not key IK/FK switches')
        source=data.action if data else None
        m3_guard(obj,source,[])
        if source and any(c.data_path in ('location','rotation_euler','rotation_quaternion','rotation_axis_angle','scale') for s,l,t,c in action_curves(source,m2_slot(obj))):
            raise ValueError('ANIMATED_OBJECT_ADAPTER_REQUIRED: generation uses an explicit root bone')
        baseline=m6_pose_baseline(obj,source,mapping,limbs)
        if not baseline['passed']:
            raise ValueError('UNAUTHORED_POSE_CHANNELS: '+json.dumps(baseline,sort_keys=True,separators=(',',':')))
        scale=list(obj.matrix_world.to_scale())
        if min(scale)<=0 or max(scale)-min(scale)>1e-5:
            raise ValueError('NONUNIFORM_RIG_SCALE: separate adapter required; transforms remain untouched')
        world_basis=obj.matrix_world.to_3x3()
        if world_basis.determinant()<=0 or any(abs(world_basis.col[a].normalized().dot(world_basis.col[b].normalized()))>1e-5 for a,b in [(0,1),(0,2),(1,2)]):
            raise ValueError('SHEARED_OR_REFLECTED_RIG_MATRIX: separate adapter required; transforms remain untouched')
        confidence=min([.92]+[row['confidence'] for row in limbs]+[caller.get(r,1) for r in required])
        if confidence<.85:
            raise ValueError('LOW_CONFIDENCE: mapping evidence below automatic-mutation floor 0.85')
        evidence.extend(['Parentless root and verified pelvis/torso hierarchy','Bilateral rest-position corroboration',
            'Exact limb parents and constraints inspected','Caller labels cannot raise confidence'])
        return {'supported':True,'status':'compatible','armature':obj.name,'mapping':mapping,'confidence':confidence,
            'bone_count':len(obj.data.bones),
            'controlled_bone_count':len({p.name for p in mapped_fk}|{r['target'] for r in limbs if r['kind']=='leg' and r['mode']=='IK'}),
            'confidence_scope':'Structural adapter evidence, not a calibrated anatomical probability; reviewed semantic side labels remain explicit',
            'limbs':limbs,'body_basis_armature':[list(r) for r in basis],
            'fps':bpy.context.scene.render.fps/bpy.context.scene.render.fps_base,
            'unit_scale_m':bpy.context.scene.unit_settings.scale_length,
            'scene_frame_range':[bpy.context.scene.frame_start,bpy.context.scene.frame_end],
            'capabilities':{'body_poses':True,'fk_arms':True,'foot_contacts':all(r['mode']=='IK' for r in limbs if r['kind']=='leg'),
                'root_motion':True,'ik_fk_switching':False,'finger_animation':False,'quadruped_generation':False},
            'unauthored_pose_baseline':baseline,
            'evidence':evidence,'reasons':[],'rig_fingerprint':m6_rig_fingerprint(obj),
            'warnings':['Narrow biped FK-arm / two-bone IK-leg adapter. Artistic review remains mandatory.']}
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        reasons.append(str(exc))
        return {'supported':False,'status':'unsupported_or_ambiguous','armature':obj.name,'mapping':mapping,
            'confidence':confidence,'limbs':limbs,'evidence':evidence,'reasons':reasons,'warnings':reasons,
            'capabilities':{'body_poses':False,'foot_contacts':False,'root_motion':False}}

def m6_rig_signature(obj):
    # Excludes the active Action, frame and evaluated pose transform channels.
    return {'object':obj.name,'data':obj.data.name,'matrix_world':[list(r) for r in obj.matrix_world],
        'parent':obj.parent.name if obj.parent else None,'pose_position':obj.data.pose_position,
        'units':bpy.context.scene.unit_settings.scale_length,'fps':bpy.context.scene.render.fps/bpy.context.scene.render.fps_base,
        'ik_solver':obj.pose.ik_solver,'source_asset_fingerprint':m6_asset_fingerprint(obj,{}),
        'bones':[{'name':p.name,'parent':p.parent.name if p.parent else None,'deform':p.bone.use_deform,
            'rest':[list(r) for r in p.bone.matrix_local],'head':list(p.bone.head_local),'tail':list(p.bone.tail_local),
            'rotation_mode':p.rotation_mode,'locks':[list(p.lock_location),list(p.lock_rotation),list(p.lock_scale)],
            'constraints':[m3_constraint_record(c) for c in p.constraints],
            'ik_limits':[p.use_ik_limit_x,p.use_ik_limit_y,p.use_ik_limit_z,p.ik_min_x,p.ik_max_x,p.ik_min_y,p.ik_max_y,p.ik_min_z,p.ik_max_z],
            'ik_stretch':p.ik_stretch} for p in obj.pose.bones]}

def m6_rig_fingerprint(obj):
    return m2_digest(m6_rig_signature(obj))

def m6_poses(options):
    plan=options.get('plan',{})
    stage=options.get('stage','BLOCKING').upper()
    validate_pose_window(plan,options.get('brief',{}),stage,options)
    poses=list(plan.get('poses',[]))
    if stage=='POSE':
        pose=options.get('pose')
        if pose:
            poses=[pose]
        else:
            selected=options.get('pose_index',0)
            if type(selected) is not int or selected<0 or selected>=len(poses):
                raise ValueError('POSE_INDEX_INVALID')
            poses=[poses[selected]]
    if stage in ('BREAKDOWNS','SPLINE','OVERLAP','FOLLOW_THROUGH','REFINE','ROOT','CONTACTS','TIMING','ARCS','BALANCE','CURVES','EFFECTOR'):
        poses+=list(plan.get('breakdowns',[]))
    unique={}
    for pose in poses:
        if not isinstance(pose,dict) or not isinstance(pose.get('semantic'),dict):
            raise ValueError('POSE_PLAN_REQUIRED: each pose needs structured semantic channels')
        frame=m6_number(pose.get('frame'),'pose frame',-1048574,1048574)
        unique[frame]=pose
    poses=[unique[frame] for frame in sorted(unique)]
    if not poses or len(poses)>601:
        raise ValueError('CAPACITY_EXCEEDED: pose count must be 1..601')
    if poses[-1]['frame']-poses[0]['frame']>600:
        raise ValueError('CAPACITY_EXCEEDED: generation window maximum 600 frames')
    return poses

def m6_verification_frames(poses,options):
    frames=set([float(p['frame']) for p in poses]+[float(f) for f in range(int(math.ceil(poses[0]['frame'])),int(math.floor(poses[-1]['frame']))+1)])
    if options.get('effector') is not None:
        config,side,target,pole,start,end,tolerance=m6_effector_config(options)
        frames.update([start+i*.25 for i in range(int(math.floor((end-start)*4))+1)])
        frames.add(end)
    return sorted(frames)

def m6_rest_world(obj,name):
    return obj.matrix_world@obj.data.bones[name].head_local

def m6_world_offset(obj,basis,value):
    local=basis@m6_vector(value,'semantic translation')
    world=obj.matrix_world.to_3x3()@local
    if world.length>0:
        world*=local.length/world.length
    return world/bpy.context.scene.unit_settings.scale_length

def m6_bone_offset(obj,pose,basis,value):
    world=m6_world_offset(obj,basis,value)
    armature=obj.matrix_world.to_3x3().inverted()@world
    return pose.bone.matrix_local.to_3x3().inverted()@armature

def m6_rotation(pose,basis,degrees,previous=None):
    x,y,z=[math.radians(m6_number(v,'semantic rotation degrees',-180,180)) for v in degrees]
    body=basis@Euler((x,y,z),'XYZ').to_matrix()@basis.inverted()
    rest=pose.bone.matrix_local.to_3x3()
    value=(rest.inverted()@body@rest).to_quaternion().normalized()
    if previous is not None and previous.dot(value)<0:
        value=-value
    if pose.rotation_mode=='QUATERNION':
        pose.rotation_quaternion=value
    else:
        pose.rotation_euler=value.to_euler(pose.rotation_mode,pose.rotation_euler)
    return value

def m6_arm_rotation(pose,basis,swing,abduction,side,previous=None):
    # Adapt T/A/neutral rest poses to the relaxed downward semantic arm.
    down=basis@Vector((0,0,-1));direction=pose.bone.tail_local-pose.bone.head_local
    if direction.length<1e-6:
        raise ValueError('ZERO_LENGTH_ARM: '+pose.name)
    neutral=direction.normalized().rotation_difference(down)
    arm_body=Euler((math.radians(m6_number(swing,'arm swing',-170,170)),
        math.radians(m6_number(abduction,'arm abduction',-150,150))*(-1 if side=='l' else 1),0),'XYZ').to_quaternion()
    world=(basis@arm_body.to_matrix()@basis.inverted()).to_quaternion()@neutral
    rest=pose.bone.matrix_local.to_quaternion();value=(rest.inverted()@world@rest).normalized()
    if previous is not None and previous.dot(value)<0:
        value=-value
    if pose.rotation_mode=='QUATERNION':
        pose.rotation_quaternion=value
    else:
        pose.rotation_euler=value.to_euler(pose.rotation_mode,pose.rotation_euler)
    return value

def m6_semantic_rotations(semantic):
    rotations={'root':[0,0,semantic.get('root_yaw_deg',0)],
        'pelvis':[semantic.get('pelvis_pitch_deg',0),semantic.get('pelvis_roll_deg',0),semantic.get('pelvis_yaw_deg',0)],
        'spine':[semantic.get('torso_pitch_deg',0)*.45,semantic.get('torso_roll_deg',0)*.45,semantic.get('torso_yaw_deg',0)*.45],
        'chest':[semantic.get('torso_pitch_deg',0)*.55,semantic.get('torso_roll_deg',0)*.55,semantic.get('torso_yaw_deg',0)*.55],
        'head':[semantic.get('head_pitch_deg',0),0,semantic.get('head_yaw_deg',0)]}
    for side,suffix in [('left','l'),('right','r')]:
        rotations['upper_arm_'+suffix]=[semantic.get('arm_swing_deg',{}).get(side,0),0,
            semantic.get('arm_abduction_deg',{}).get(side,0)*(1 if side=='left' else -1)]
        rotations['lower_arm_'+suffix]=[0,0,semantic.get('elbow_flex_deg',{}).get(side,0)*(1 if side=='left' else -1)]
        rotations['hand_'+suffix]=[semantic.get('hand_pitch_deg',{}).get(side,0),0,0]
    return rotations

def m6_location_roles(semantic):
    return [('root',semantic.get('root_translation_m',[0,0,0])),('pelvis',semantic.get('pelvis_offset_m',[0,0,0]))]

def m6_rotation_channel(control):
    return 'rotation_quaternion' if control.rotation_mode=='QUATERNION' else 'rotation_euler'

def m6_written_pose_paths(obj,mapping,limbs):
    paths=set()
    for role in m6_semantic_rotations({}):
        control=obj.pose.bones[mapping[role]]
        paths.add(control.path_from_id(m6_rotation_channel(control)))
    for role,value in m6_location_roles({}):
        paths.add(obj.pose.bones[mapping[role]].path_from_id('location'))
    for limb in limbs:
        if limb['kind']!='leg':
            continue
        if limb['mode']=='IK':
            control=obj.pose.bones[limb['target']]
            paths.add(control.path_from_id('location'))
            paths.add(control.path_from_id(m6_rotation_channel(control)))
        else:
            for role in ('upper_leg','lower_leg','foot'):
                control=obj.pose.bones[mapping[role+'_'+limb['side']]]
                paths.add(control.path_from_id(m6_rotation_channel(control)))
    return paths

def m6_pose_baseline(obj,source,mapping,limbs):
    channels=[]
    for pose in obj.pose.bones:
        rotation=m6_rotation_channel(pose)
        active=list(pose.rotation_quaternion) if pose.rotation_mode=='QUATERNION' else list(pose.rotation_euler)
        expected=[1,0,0,0] if pose.rotation_mode=='QUATERNION' else [0,0,0]
        if pose.rotation_mode=='AXIS_ANGLE':
            rotation='rotation_axis_angle';active=list(pose.rotation_axis_angle);expected=[0,0,1,0]
        channels.extend([{'path':pose.path_from_id('location'),'values':list(pose.location),'neutral':[0,0,0]},
            {'path':pose.path_from_id('scale'),'values':list(pose.scale),'neutral':[1,1,1]},
            {'path':pose.path_from_id(rotation),'values':active,'neutral':expected}])
    written=m6_written_pose_paths(obj,mapping,limbs)
    active_paths={row['path'] for row in channels}
    curves=[]
    if source:
        for s,l,t,curve in action_curves(source,m2_slot(obj)):
            if curve.data_path in active_paths and curve.data_path not in written:
                curves.append({'path':curve.data_path,'index':curve.array_index,'extrapolation':curve.extrapolation,
                    'points':[{'value':float(point.co.y),'left':float(point.handle_left.y),'right':float(point.handle_right.y),
                        'interpolation':point.interpolation} for point in curve.keyframe_points]})
    return validate_unwritten_pose(channels,curves,written)

def m6_place_pose(obj,compat,pose,key=False,stage='BLOCKING',previous=None):
    mapping=compat['mapping'];basis=Matrix(compat['body_basis_armature']);semantic=pose['semantic'];frame=pose['frame']
    if previous is None:
        previous={}
    rotations=m6_semantic_rotations(semantic)
    if stage not in ('CONTACTS',):
        for role,degrees in rotations.items():
            if stage=='ROOT' and role!='root':
                continue
            control=obj.pose.bones[mapping[role]]
            if role in ('upper_arm_l','upper_arm_r'):
                side='left' if role.endswith('_l') else 'right'
                previous[role]=m6_arm_rotation(control,basis,semantic.get('arm_swing_deg',{}).get(side,0),
                    semantic.get('arm_abduction_deg',{}).get(side,0),role[-1],previous.get(role))
            else:
                previous[role]=m6_rotation(control,basis,degrees,previous.get(role))
            if key:
                control.keyframe_insert(data_path=m6_rotation_channel(control),frame=frame)
        for role,value in m6_location_roles(semantic):
            if stage=='ROOT' and role!='root':
                continue
            control=obj.pose.bones[mapping[role]]
            control.location=m6_bone_offset(obj,control,basis,value)
            if key:
                control.keyframe_insert(data_path='location',frame=frame)
    for limb in compat['limbs']:
        if limb['kind']!='leg':
            continue
        side='left' if limb['side']=='l' else 'right'
        foot=semantic.get('feet',{}).get(side,{})
        if limb['mode']=='IK':
            target=obj.pose.bones[limb['target']]
            world=m6_rest_world(obj,mapping['foot_'+limb['side']])+m6_world_offset(obj,basis,foot.get('offset_m',[0,0,0]))
            armature=obj.matrix_world.inverted()@world
            target.location=target.bone.matrix_local.to_3x3().inverted()@(armature-target.bone.head_local)
            degrees=[foot.get('heel_roll_deg',0),0,semantic.get('root_yaw_deg',0)]
            previous['target_'+limb['side']]=m6_rotation(target,basis,degrees,previous.get('target_'+limb['side']))
            if key:
                target.keyframe_insert(data_path='location',frame=frame)
                target.keyframe_insert(data_path=m6_rotation_channel(target),frame=frame)
        elif stage not in ('ROOT','CONTACTS'):
            flex=m6_number(foot.get('knee_flex_deg',0),'FK knee flexion',0,150)
            for role,degrees in [('upper_leg',[flex*.5,0,0]),('lower_leg',[-flex,0,0]),('foot',[flex*.5,0,0])]:
                control=obj.pose.bones[mapping[role+'_'+limb['side']]]
                m6_rotation(control,basis,degrees)
                if key:
                    control.keyframe_insert(data_path=m6_rotation_channel(control),frame=frame)
    obj.update_tag();bpy.context.view_layer.update()

def m6_pose_metrics(obj,compat,pose,options):
    evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());unit=bpy.context.scene.unit_settings.scale_length
    ground=m6_number(options.get('ground_height_m',options.get('brief',{}).get('ground_height_m',0)),'ground height')
    max_reach=0.0;max_stretch=1.0;penetration=0.0;feet={};errors=[];warnings=[]
    tolerance=m6_number(options.get('contact_tolerance_m',.02),'contact tolerance',0,.5)
    for limb in compat['limbs']:
        if limb['kind']!='leg':
            continue
        side='left' if limb['side']=='l' else 'right'
        names=limb['chain'];chain=[evaluated.pose.bones[n] for n in names]
        foot_world=evaluated.matrix_world@chain[2].head
        foot_semantic=pose['semantic'].get('feet',{}).get(side,{})
        contact=foot_semantic.get('contact') is True
        if limb['mode']=='IK':
            target=evaluated.pose.bones[limb['target']]
            desired=evaluated.matrix_world@target.head
            reach=(foot_world-desired).length*unit;max_reach=max(max_reach,reach)
            if reach>tolerance:
                errors.append('IMPOSSIBLE_REACH: '+side+' foot target residual '+str(round(reach,6))+' m')
        else:
            reach=None
            if contact:
                warnings.append('FK_CONTACT_UNVERIFIED: '+side+' uses no foot-lock adapter')
        for bone in chain[:2]:
            ratio=(bone.tail-bone.head).length/max(obj.data.bones[bone.name].length,1e-9)
            max_stretch=max(max_stretch,ratio)
        pen=max(0,ground-foot_world.z*unit);penetration=max(penetration,pen)
        if pen>tolerance:
            errors.append('GROUND_PENETRATION: '+side+' '+str(round(pen,6))+' m')
        contact_confirmed=contact and abs(foot_world.z*unit-ground)<=tolerance and (reach is None or reach<=tolerance)
        if contact and not contact_confirmed:
            errors.append('CONTACT_UNCONFIRMED: '+side+' effector is not on the declared ground')
        feet[side]={'position_world_m':list(foot_world*unit),'contact_requested':contact,
            'contact_confirmed':contact_confirmed,
            'contact_phase':foot_semantic.get('contact_phase'),'ground_distance_m':foot_world.z*unit-ground,
            'target_residual_m':reach}
    if max_stretch>1.005:
        errors.append('LIMB_STRETCH: evaluated limb length changed')
    # Bone-based center is a documented proxy. It is not a true surface-mass COM
    # or proof of dynamic balance. Intentional creature asymmetry is preserved.
    mass=options.get('brief',{}).get('mass_asymmetry',{'left':1,'right':1})
    left_mass=m6_number(mass.get('left',1),'left relative mass',.1,10)
    right_mass=m6_number(mass.get('right',1),'right relative mass',.1,10)
    weights={'pelvis':3.0,'spine':2.0,'chest':3.0,'head':1.0,'hand_l':left_mass,'hand_r':right_mass}
    total=sum(weights.values());center=Vector((0,0,0))
    for role,weight in weights.items():
        center+=(evaluated.matrix_world@evaluated.pose.bones[compat['mapping'][role]].head)*weight/total
    supports=[Vector(row['position_world_m']) for row in feet.values() if row['contact_confirmed']]
    balance=None
    if len(supports)==2:
        a,b=supports;axis=b-a;axis.z=0;point=center*unit;point.z=0;a=a.copy();a.z=0
        projection=a+axis*max(0,min(1,(point-a).dot(axis)/max(axis.length_squared,1e-9)))
        balance=(point-projection).length
    elif len(supports)==1:
        point=center*unit-supports[0];point.z=0;balance=point.length
    return {'passed':not errors,'errors':errors,'warnings':warnings,'feet':feet,
        'maximum_target_residual_m':max_reach,'maximum_limb_length_ratio':max_stretch,
        'maximum_ground_penetration_m':penetration,'balance_proxy_distance_m':balance,
        'balance_scope':'Weighted bone-head proxy; support segment distance, not physical COM or artistic score'}

def m6_plan_validation(obj,compat,poses,options):
    state=m3_capture(obj);reports=[]
    try:
        for pose in poses:
            m3_set_frame(pose['frame']);m6_place_pose(obj,compat,pose,False)
            if options.get('stage','').upper()=='EFFECTOR':
                m6_effector_pose(obj,compat,options,pose['frame'],False)
            metrics=m6_pose_metrics(obj,compat,pose,options)
            reports.append({'frame':pose['frame'],'phase':pose.get('phase'),'metrics':metrics})
    finally:
        m3_restore(obj,state)
    errors=[e for row in reports for e in row['metrics']['errors']]
    warnings=[w for row in reports for w in row['metrics']['warnings']]
    return {'passed':not errors,'errors':list(dict.fromkeys(errors))[:30],'warnings':list(dict.fromkeys(warnings))[:30],
        'samples':len(reports),'maximum_target_residual_m':max(r['metrics']['maximum_target_residual_m'] for r in reports),
        'maximum_ground_penetration_m':max(r['metrics']['maximum_ground_penetration_m'] for r in reports),
        'maximum_limb_length_ratio':max(r['metrics']['maximum_limb_length_ratio'] for r in reports),
        'problem_frames':[r['frame'] for r in reports if not r['metrics']['passed']][:30]}

def m3_allowed_drivers(obj,options):
    return []

def m3_related_objects(obj,options):
    return []

def m3_context(obj,options):
    context=m3_context_base(obj)
    context['generation_mapping']=m6_compatibility(obj,options)
    context['source_asset_context']=m6_asset_context(obj,options)
    context['asset_stamps']=[{'scene':name,'production_asset':bpy.data.scenes[name].get('production_asset')}
        for name in sorted([s.name for s in bpy.data.scenes])]
    return context

def m3_propose(obj,source,options):
    try:
        compatibility=m6_compatibility(obj,options)
        if not compatibility['supported']:
            raise ValueError('; '.join(compatibility['reasons']))
        brief=options.get('brief',{})
        if brief.get('fps') is not None and abs(m6_number(brief['fps'],'brief FPS',1,120)-compatibility['fps'])>1e-6:
            raise ValueError('FPS_MISMATCH: generation brief and actual Blender FPS differ; no scene FPS is changed automatically')
        stage=options.get('stage','BLOCKING').upper()
        if stage not in ('BLOCKING','POSE','BREAKDOWNS','SPLINE','OVERLAP','FOLLOW_THROUGH','ROOT','CONTACTS','REFINE','TIMING','ARCS','BALANCE','CURVES','EFFECTOR'):
            raise ValueError('UNSUPPORTED_GENERATION_STAGE: '+stage)
        if stage not in ('BLOCKING','POSE') and source is None:
            raise ValueError('GENERATED_SOURCE_REQUIRED: staged edits require the generated candidate')
        effective=dict(options)
        if stage not in ('BLOCKING','POSE') and source.get('production_generation_effector_config'):
            effective.setdefault('effector',json.loads(source['production_generation_effector_config']))
        name=options.get('candidate_name')
        if name is not None and (not isinstance(name,str) or not name or bpy.data.actions.get(name)):
            raise ValueError('CANDIDATE_ACTION_COLLISION: explicit Action name exists or is invalid')
        poses=m6_poses(options);window=[poses[0]['frame'],poses[-1]['frame']]
        if stage=='EFFECTOR':
            config,side,target,pole,start,end,tolerance=m6_effector_config(options)
            if start<=window[0] or end>=window[1]:
                raise ValueError('EFFECTOR_INTERVAL_INVALID: finite hand contact must lie strictly inside the generated clip')
        if source and stage not in ('BLOCKING','POSE'):
            if not source.get('production_generation_id') or source.get('production_generation_id')!=options.get('generation_id'):
                raise ValueError('GENERATED_SOURCE_REQUIRED: Action must belong to this generation')
            if any(k.co.x<window[0]-1e-6 or k.co.x>window[1]+1e-6 for s,l,t,c in action_curves(source,m2_slot(obj)) for k in c.keyframe_points):
                raise ValueError('GENERATION_WINDOW_MISMATCH: candidate has animation outside the declared window')
        frames=len(m6_verification_frames(poses,effective))
        if stage in ('OVERLAP','FOLLOW_THROUGH'):
            m6_secondary_config(obj,compatibility,poses,options,stage=='FOLLOW_THROUGH')
        max_bone_frames=min(100000,m6_number(brief.get('max_bone_frame_samples',100000),'bone/frame budget',1,200000))
        max_keys=min(100000,m6_number(brief.get('max_keyframes',100000),'key budget',1,100000))
        capacity={'poses':len(poses),'frames':frames,'bones':len(obj.pose.bones),
            'bone_frames_per_qa_pass':frames*len(obj.pose.bones),'maximum_bone_frames':max_bone_frames,
            'estimated_keys':len(poses)*80,'maximum_keys':max_keys}
        if capacity['bone_frames_per_qa_pass']>max_bone_frames or capacity['estimated_keys']>max_keys:
            raise ValueError('CAPACITY_EXCEEDED: narrow generation window or rig')
        if stage in ('CONTACTS','ROOT') and not compatibility['capabilities']['foot_contacts']:
            raise ValueError('FOOT_CONTACT_ADAPTER_REQUIRED: FK contacts are analysis-only')
        validation=m6_plan_validation(obj,compatibility,poses,options)
        if not validation['passed']:
            return {'supported':False,'status':'pose_plan_rejected','reasons':validation['errors'],
                'warnings':validation['warnings'],'pose_validation':validation,'compatibility':compatibility,'capacity':capacity}
        return {'supported':True,'candidate_policy':'new' if stage in ('BLOCKING','POSE') else 'duplicate',
            'stage':stage,'adapter':'verified_biped_fk_arms_two_bone_ik_legs','compatibility':compatibility,
            'poses':poses,'frame_range':window,'generation_id':options.get('generation_id'),
            'affected_controls':[compatibility['mapping'][r] for r in ('root','pelvis','spine','chest','head','upper_arm_l','lower_arm_l','hand_l','upper_arm_r','lower_arm_r','hand_r')]+
                [r['target'] for r in compatibility['limbs'] if r['kind']=='leg' and r['mode']=='IK'],
            'pose_validation':validation,'capacity':capacity,'rig_fingerprint':m6_rig_fingerprint(obj),
            'source_fingerprint':m2_digest(m3_source_snapshot(source)),
            'mutation_window':{'window':window,'outside':'source Action is immutable; candidate contains no keys outside window'},
            'warnings':compatibility['warnings']+validation['warnings']+['Numeric QA and temporal visual review remain mandatory before acceptance.']}
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return {'supported':False,'status':'unsupported_or_ambiguous','reasons':[str(exc)],'warnings':[str(exc)]}

def m6_curve_interpolation(obj,candidate,poses,stage):
    mapping={float(p['frame']):p for p in poses};counts={};contact_paths=set()
    if stage=='BLOCKING' or stage=='POSE':
        interpolation='CONSTANT'
    elif stage in ('SPLINE','CURVES'):
        interpolation='BEZIER'
    else:
        interpolation='LINEAR'
    # Root and IK-location channels remain linear: cubic overshoot may invalidate
    # a planted foot or drive a nonstretch target outside the reachable region.
    compat=m6_compatibility(obj,params['options'])
    for limb in compat.get('limbs',[]):
        if limb['kind']=='leg' and limb['mode']=='IK':
            contact_paths.add(obj.pose.bones[limb['target']].path_from_id('location'))
    contact_paths.add(obj.pose.bones[compat['mapping']['root']].path_from_id('location'))
    limited=None
    if stage in ('ROOT','CONTACTS'):
        roles=['foot_control_l','foot_control_r']+(['root'] if stage=='ROOT' else [])
        limited={obj.pose.bones[compat['mapping'][r]].path_from_id(c) for r in roles if r in compat['mapping'] for c in ('location','rotation_quaternion','rotation_euler')}
    elif stage=='EFFECTOR':
        side=params['options'].get('effector',{}).get('side','left');suffix='l' if side=='left' else 'r'
        limited={obj.pose.bones[compat['mapping'][r+'_'+suffix]].path_from_id(c) for r in ('upper_arm','lower_arm','hand') for c in ('rotation_quaternion','rotation_euler')}
    elif stage in ('OVERLAP','FOLLOW_THROUGH'):
        limited={obj.pose.bones[compat['mapping'][r]].path_from_id(c) for r in params['options'].get('chain',params['options'].get('roles',['chest','head'])) for c in ('rotation_quaternion','rotation_euler')}
    for s,l,t,curve in action_curves(candidate,m2_slot(obj)):
        if limited is not None and curve.data_path not in limited:
            continue
        for point in curve.keyframe_points:
            if stage=='EFFECTOR':
                config,side,target,pole,start,end,tolerance=m6_effector_config(params['options'])
                if point.co.x<start or point.co.x>end:
                    continue
            if point.co.x not in mapping:
                continue
            point.interpolation='LINEAR' if stage in ('SPLINE','CURVES') and curve.data_path in contact_paths else interpolation
            if point.interpolation=='BEZIER':
                point.handle_left_type='AUTO_CLAMPED';point.handle_right_type='AUTO_CLAMPED'
            counts[point.interpolation]=counts.get(point.interpolation,0)+1
        curve.update()
    return counts

def m6_preserve_holds(obj,candidate,compat,options):
    holds=options.get('plan',{}).get('holds',[])
    if not isinstance(holds,list) or len(holds)>100:
        raise ValueError('GENERATION_HOLD_PLAN_INVALID')
    body_roles=['pelvis','spine','chest','head','upper_arm_l','lower_arm_l','hand_l','upper_arm_r','lower_arm_r','hand_r']
    paths={obj.pose.bones[compat['mapping'][r]].path_from_id(c) for r in body_roles for c in ('location','rotation_quaternion','rotation_euler')}
    count=0
    for hold in holds:
        start=m6_number(hold.get('frame_start'),'hold frame_start');end=m6_number(hold.get('frame_end'),'hold frame_end')
        if end<=start:
            continue
        for s,l,t,curve in action_curves(candidate,m2_slot(obj)):
            if curve.data_path not in paths:
                continue
            first=fw_key_at(curve,start)
            if first is None:
                continue
            value=first.co.y
            last=fw_key_at(curve,end)
            if last is None:
                curve.keyframe_points.insert(end,value,options={'FAST'})
                last=fw_key_at(curve,end)
            else:
                last.co.y=value
            if last is None:
                raise ValueError('GENERATION_HOLD_INSERT_FAILED: exact end key was not created')
            last.interpolation='BEZIER';last.handle_left_type='AUTO_CLAMPED';last.handle_right_type='AUTO_CLAMPED'
            first=fw_key_at(curve,start);first.interpolation='CONSTANT';curve.update();count+=1
    return {'hold_channels':count,'holds':len(holds),'scope':'Declared body event holds; root and foot-target trajectories preserved separately'}

def m6_secondary_config(obj,compat,poses,options,follow):
    delay=m6_number(options.get('delay_frames',2),'secondary delay frames',0,12)
    strength=m6_number(options.get('strength',.15),'secondary strength',0,.5)
    roles=options.get('chain',options.get('roles',['chest','head']))
    if options.get('chain') is not None and options.get('roles') is not None and options['chain']!=options['roles']:
        raise ValueError('SECONDARY_CHAIN_AMBIGUOUS: chain and roles disagree')
    supported={'spine','chest','head','upper_arm_l','lower_arm_l','hand_l','upper_arm_r','lower_arm_r','hand_r'}
    if not isinstance(roles,list) or not roles or len(roles)>6 or len(set(roles))!=len(roles) or any(r not in supported for r in roles):
        raise ValueError('SECONDARY_CHAIN_UNSUPPORTED: declared torso/head/FK-arm semantic roles only')
    if options.get('chain') is not None:
        for parent,child in zip(roles,roles[1:]):
            if not m6_descends(obj.pose.bones[compat['mapping'][child]],obj.pose.bones[compat['mapping'][parent]]):
                raise ValueError('SECONDARY_CHAIN_MISMATCH: roles are not a contiguous hierarchy branch')
    selected=options.get('phases',options.get('phase'))
    if options.get('phase') is not None and options.get('phases') is not None:
        raise ValueError('SECONDARY_PHASE_AMBIGUOUS: specify phase or phases')
    if isinstance(selected,str):
        selected=[selected]
    windows=[]
    if selected is None:
        windows=[[poses[0]['frame'],poses[-1]['frame']]]
    elif not isinstance(selected,list) or not selected or any(not isinstance(p,str) for p in selected):
        raise ValueError('SECONDARY_PHASE_INVALID')
    else:
        rows=options.get('plan',{}).get('phases',[])
        for phase in selected:
            found=[r for r in rows if r.get('phase')==phase]
            if not found:
                raise ValueError('SECONDARY_PHASE_NOT_IN_PLAN: '+phase)
            windows.extend([[r['frame_start'],r['frame_end']] for r in found])
    duration=options.get('duration_frames')
    if options.get('duration_s') is not None:
        if duration is not None:
            raise ValueError('FOLLOW_THROUGH_DURATION_AMBIGUOUS')
        duration=m6_number(options['duration_s'],'follow-through duration seconds',.001,10)*(bpy.context.scene.render.fps/bpy.context.scene.render.fps_base)
    duration=m6_number(12 if duration is None else duration,'follow-through duration frames',.001,600)
    damping=m6_number(options.get('damping',1.5),'follow-through damping',0,20)
    preserve=options.get('preserve_impact',True)
    if type(preserve) is not bool:
        raise ValueError('PRESERVE_IMPACT_MUST_BE_BOOLEAN')
    return {'delay_frames':delay,'strength':strength,'roles':roles,'windows':windows,'phases':selected,
        'duration_frames':duration,'damping':damping,'preserve_impact':preserve,'chain':options.get('chain')}

def m6_delayed_secondary(obj,candidate,compat,poses,options,follow=False):
    settings=m6_secondary_config(obj,compat,poses,options,follow)
    delay=settings['delay_frames'];strength=settings['strength'];roles=settings['roles']
    state=m3_capture(obj);samples=[];start=poses[0]['frame'];end=poses[-1]['frame']
    try:
        for pose in poses:
            frame=pose['frame'];window=next((w for w in settings['windows'] if w[0]<=frame<=w[1]),None)
            if window is None:
                continue
            holds=options.get('plan',{}).get('holds',[])
            if any(h.get('frame_start',0)<=frame<=h.get('frame_end',-1) for h in holds):
                continue
            if follow and settings['preserve_impact'] and pose.get('phase')=='IMPACT':
                continue
            m3_set_frame(frame)
            current={r:obj.pose.bones[compat['mapping'][r]].matrix_basis.to_quaternion() for r in roles}
            for index,role in enumerate(roles):
                role_delay=delay*(index+1) if settings['chain'] else delay
                m3_set_frame(max(start,frame-role_delay));prior=obj.pose.bones[compat['mapping'][role]].matrix_basis.to_quaternion()
                amount=strength
                if follow:
                    elapsed=max(0,frame-window[0])
                    amount=strength*math.exp(-settings['damping']*elapsed/settings['duration_frames']) if elapsed<=settings['duration_frames'] else 0
                value=current[role].slerp(prior,amount)
                if frame in (start,end,window[0],window[1]):
                    value=current[role]
                samples.append({'frame':frame,'role':role,'quaternion':list(value)})
        for row in samples:
            bone=obj.pose.bones[compat['mapping'][row['role']]];value=Quaternion(row['quaternion'])
            if bone.rotation_mode=='QUATERNION':
                bone.rotation_quaternion=value;channel='rotation_quaternion'
            else:
                bone.rotation_euler=value.to_euler(bone.rotation_mode,bone.rotation_euler);channel='rotation_euler'
            bone.keyframe_insert(data_path=channel,frame=row['frame'])
    finally:
        m3_restore(obj,state)
    return dict(settings,keys_adjusted=len(samples),
        method='bounded phase-dependent previous orientation; endpoints preserved; no generic oscillation')

def m6_effector_config(options):
    config=options.get('effector')
    if not isinstance(config,dict):
        raise ValueError('EFFECTOR_CONFIGURATION_REQUIRED: exact side, target, pole and static plane')
    side=config.get('side')
    if side not in ('left','right'):
        raise ValueError('EFFECTOR_SIDE_REQUIRED')
    target=m6_vector(config.get('target_world_m'),'effector target_world_m')
    pole=m6_vector(config.get('pole_world_m'),'effector pole_world_m')
    surface=config.get('surface')
    if not isinstance(surface,dict) or surface.get('type')!='PLANE' or surface.get('static') is not True:
        raise ValueError('STATIC_SURFACE_ADAPTER_REQUIRED: declared static analytic PLANE only')
    point=m6_vector(surface.get('point_world_m'),'surface point_world_m');normal=m6_vector(surface.get('normal_world'),'surface normal_world')
    if normal.length<1e-6:
        raise ValueError('SURFACE_NORMAL_INVALID')
    normal.normalize();tolerance=m6_number(config.get('tolerance_m',.01),'effector tolerance',0,.1)
    if abs((target-point).dot(normal))>tolerance:
        raise ValueError('EFFECTOR_SURFACE_MISMATCH: target is not on declared plane')
    start=m6_number(config.get('frame_start'),'effector frame_start');end=m6_number(config.get('frame_end'),'effector frame_end')
    if end<start:
        raise ValueError('EFFECTOR_INTERVAL_INVALID')
    return config,side,target,pole,start,end,tolerance

def m6_effector_pose(obj,compat,options,frame,key):
    config,side,target_world,pole_world,start,end,tolerance=m6_effector_config(options)
    if frame<start or frame>end:
        return None
    suffix='l' if side=='left' else 'r';mapping=compat['mapping']
    upper=obj.pose.bones[mapping['upper_arm_'+suffix]];lower=obj.pose.bones[mapping['lower_arm_'+suffix]];hand=obj.pose.bones[mapping['hand_'+suffix]]
    unit=bpy.context.scene.unit_settings.scale_length;inverse=obj.matrix_world.inverted()
    target=inverse@(target_world/unit);pole=inverse@(pole_world/unit)
    bpy.context.view_layer.update();shoulder=upper.head.copy();axis=target-shoulder;distance=axis.length
    length_a=upper.bone.length;length_b=lower.bone.length
    if distance<1e-6 or distance>length_a+length_b-1e-5 or distance<abs(length_a-length_b)+1e-5:
        raise ValueError('IMPOSSIBLE_REACH: effector target outside two-link arm workspace')
    direction=axis/distance;bend=pole-shoulder-direction*(pole-shoulder).dot(direction)
    if bend.length<1e-6:
        raise ValueError('EFFECTOR_POLE_AMBIGUOUS: pole is collinear with target')
    bend.normalize();along=(length_a*length_a-length_b*length_b+distance*distance)/(2*distance)
    radius=math.sqrt(max(0,length_a*length_a-along*along));elbow=shoulder+direction*along+bend*radius
    old_hand=hand.matrix.to_quaternion()
    for bone,origin,endpoint in [(upper,shoulder,elbow),(lower,elbow,target)]:
        rest_direction=(bone.bone.tail_local-bone.bone.head_local).normalized()
        desired=(endpoint-origin).normalized();rotation=rest_direction.rotation_difference(desired)@bone.bone.matrix_local.to_quaternion()
        bone.matrix=Matrix.LocRotScale(origin,rotation,Vector((1,1,1)))
        bpy.context.view_layer.update()
        if bone.location.length>1e-4:
            raise ValueError('EFFECTOR_LOCAL_TRANSLATION_REQUIRED: arm hierarchy cannot be solved with rotation channels alone')
        bone.location=(0,0,0)
        if key:
            bone.keyframe_insert(data_path='rotation_quaternion' if bone.rotation_mode=='QUATERNION' else 'rotation_euler',frame=frame)
    hand_matrix=hand.matrix.copy();hand_matrix.translation=target
    hand_matrix=Matrix.LocRotScale(target,old_hand,Vector((1,1,1)));hand.matrix=hand_matrix
    if hand.location.length>1e-4:
        raise ValueError('EFFECTOR_WRIST_TRANSLATION_REQUIRED')
    hand.location=(0,0,0)
    if key:
        hand.keyframe_insert(data_path='rotation_quaternion' if hand.rotation_mode=='QUATERNION' else 'rotation_euler',frame=frame)
    obj.update_tag();bpy.context.view_layer.update()
    evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    endpoint=evaluated.matrix_world@evaluated.pose.bones[hand.name].head
    residual=(endpoint*unit-target_world).length
    if residual>tolerance:
        raise ValueError('EFFECTOR_REACH_VERIFICATION_FAILED: residual '+str(residual)+' m')
    return {'residual_m':residual,'side':side,'frame':frame,'surface_type':'static_analytic_plane','passed':True}

def m3_apply(obj,source,candidate,proposal,options):
    requested=options.get('candidate_name')
    if requested:
        existing=bpy.data.actions.get(requested)
        if existing is not None and existing!=candidate:
            raise ValueError('CANDIDATE_ACTION_COLLISION: Action name became occupied after proposal')
        candidate.name=requested
        if candidate.name!=requested:
            raise ValueError('CANDIDATE_ACTION_COLLISION: Blender changed the requested name')
    compatibility=proposal['compatibility'];poses=proposal['poses'];stage=proposal['stage'];previous={}
    effector_samples=None
    if stage in ('OVERLAP','FOLLOW_THROUGH'):
        secondary=m6_delayed_secondary(obj,candidate,compatibility,poses,options,stage=='FOLLOW_THROUGH')
    else:
        secondary=None
        if stage in ('TIMING','ARCS','BALANCE','REFINE'):
            owned={obj.pose.bones[name].path_from_id(channel) for name in proposal['affected_controls']
                for channel in ('location','rotation_euler','rotation_quaternion')}
            for s,l,t,curve in action_curves(candidate,m2_slot(obj)):
                if curve.data_path in owned:
                    while curve.keyframe_points:
                        curve.keyframe_points.remove(curve.keyframe_points[-1])
        if stage=='EFFECTOR':
            config,side,target,pole,start,end,tolerance=m6_effector_config(options)
            suffix='l' if side=='left' else 'r'
            written={obj.pose.bones[compatibility['mapping'][r+'_'+suffix]].path_from_id(c) for r in ('upper_arm','lower_arm','hand') for c in ('rotation_quaternion','rotation_euler')}
            for s,l,t,curve in action_curves(candidate,m2_slot(obj)):
                if curve.data_path in written:
                    fw_prepare(curve,start,end)
            contact_frames=sorted(set([float(p['frame']) for p in poses if start<=p['frame']<=end]+[start,end]+[float(f) for f in range(int(math.ceil(start)),int(math.floor(end))+1)]))
            effector_samples=len(contact_frames)
            for frame in contact_frames:
                m3_set_frame(frame);m6_effector_pose(obj,compatibility,options,frame,True)
            for s,l,t,curve in action_curves(candidate,m2_slot(obj)):
                if curve.data_path in written:
                    for point in curve.keyframe_points:
                        if start<=point.co.x<=end:
                            point.interpolation='LINEAR'
                    curve.update()
        elif stage not in ('SPLINE','CURVES'):
            for pose in poses:
                m3_set_frame(pose['frame']);m6_place_pose(obj,compatibility,pose,True,stage,previous)
    if stage in ('OVERLAP','FOLLOW_THROUGH'):
        counts={}
        for s,l,t,c in action_curves(candidate,m2_slot(obj)):
            for point in c.keyframe_points:
                counts[point.interpolation]=counts.get(point.interpolation,0)+1
    else:
        counts=m6_curve_interpolation(obj,candidate,poses,stage)
    holds=m6_preserve_holds(obj,candidate,compatibility,options) if stage in ('SPLINE','CURVES') else None
    candidate['production_generation_id']=str(options.get('generation_id',''))
    candidate['production_generation_stage']=stage
    candidate['production_generation_lifecycle']='GENERATED_CANDIDATE'
    candidate['production_generation_adapter']='biped_fk_arms_two_bone_ik_legs'
    candidate['production_generation_frame_start']=float(poses[0]['frame'])
    candidate['production_generation_frame_end']=float(poses[-1]['frame'])
    candidate['production_generation_rig_fingerprint']=proposal['rig_fingerprint']
    if stage=='EFFECTOR':
        candidate['production_generation_effector_config']=json.dumps(options['effector'],sort_keys=True,separators=(',',':'))
    for key in ('operation_id','created_utc','mcp_version','generation_ref','brief_ref'):
        value=options.get(key)
        if value is not None:
            if not isinstance(value,str) or len(value)>2048:
                raise ValueError('GENERATION_METADATA_INVALID: compact string required for '+key)
            candidate['production_generation_'+key]=value
    candidate['production_generation_profile']=str(options.get('brief',{}).get('profile','UNSPECIFIED'))
    return {'stage':stage,'poses_keyed':0 if stage in ('SPLINE','CURVES','OVERLAP','FOLLOW_THROUGH','EFFECTOR') else len(poses),
        'effector_contact_samples_keyed':effector_samples,
        'interpolation_counts':counts,'secondary_motion':secondary,'holds_preserved':holds,'candidate_name':candidate.name,
        'generation_id':options.get('generation_id'),'source_action_immutable':True}

def m6_nearest_pose(poses,frame):
    previous=poses[0]
    for pose in poses:
        if pose['frame']>frame:
            break
        previous=pose
    return previous

def m6_contact_pose(poses,frame,options):
    pose=m6_nearest_pose(poses,frame)
    intervals=options.get('plan',{}).get('contact_intervals')
    if intervals is None:
        return pose
    if not isinstance(intervals,list) or len(intervals)>1200:
        raise ValueError('CONTACT_INTERVAL_PLAN_INVALID')
    semantic=dict(pose['semantic']);feet={side:dict(row) for side,row in semantic.get('feet',{}).items()}
    for side in ('left','right'):
        if side in feet:
            feet[side]['contact']=any(row.get('side')==side and row.get('frame_start',0)<=frame<=row.get('frame_end',-1) for row in intervals)
    semantic['feet']=feet;result=dict(pose);result['semantic']=semantic
    return result

def m6_sample_candidate(obj,candidate,compat,poses,options):
    options=dict(options)
    if candidate.get('production_generation_effector_config'):
        options.setdefault('effector',json.loads(candidate['production_generation_effector_config']))
    state=m3_capture(obj);frames=m6_verification_frames(poses,options)
    errors=[];warnings=[];worst_reach=0.0;worst_pen=0.0;worst_stretch=1.0;problem_frames=[];previous_feet={};contact_travel={'left':0.0,'right':0.0};contact_count={'left':0,'right':0}
    max_jump=0.0;bone_previous={};max_angular=0.0;root_positions=[]
    contact_intervals=[];active_contacts={};effector_report=None
    fps=bpy.context.scene.render.fps/bpy.context.scene.render.fps_base;unit=bpy.context.scene.unit_settings.scale_length
    threshold=m6_number(options.get('sliding_tolerance_m',.02),'sliding tolerance',0,.5)
    try:
        for frame in frames:
            m3_set_frame(frame);pose=m6_contact_pose(poses,frame,options);metrics=m6_pose_metrics(obj,compat,pose,options)
            if not metrics['passed']:
                problem_frames.append(frame)
            errors+=metrics['errors'];warnings+=metrics['warnings'];worst_reach=max(worst_reach,metrics['maximum_target_residual_m'])
            worst_pen=max(worst_pen,metrics['maximum_ground_penetration_m']);worst_stretch=max(worst_stretch,metrics['maximum_limb_length_ratio'])
            for side,row in metrics['feet'].items():
                point=Vector(row['position_world_m']);old=previous_feet.get(side)
                if row['contact_confirmed'] and old and old['contact']:
                    distance=(point-old['point']).length;contact_travel[side]+=distance;contact_count[side]+=1
                    max_jump=max(max_jump,distance)
                    active_contacts[side]['path_distance_m']+=distance
                    active_contacts[side]['sample_pairs']+=1
                    active_contacts[side]['frame_end']=frame
                    active_contacts[side]['maximum_step_m']=max(active_contacts[side]['maximum_step_m'],distance)
                    if active_contacts[side]['path_distance_m']>threshold:
                        problem_frames.append(frame)
                elif row['contact_confirmed']:
                    active_contacts[side]={'side':side,'frame_start':frame,'frame_end':frame,'path_distance_m':0.0,'sample_pairs':0,'maximum_step_m':0.0}
                elif side in active_contacts:
                    contact_intervals.append(active_contacts.pop(side))
                previous_feet[side]={'point':point,'contact':row['contact_confirmed']}
            evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            root_positions.append(list((evaluated.matrix_world@evaluated.pose.bones[compat['mapping']['root']].head)*unit))
            for role in ('hand_l','hand_r','head','pelvis'):
                bone=evaluated.pose.bones[compat['mapping'][role]];q=bone.matrix.to_quaternion().normalized();old=bone_previous.get(role)
                if old:
                    max_angular=max(max_angular,m2_angle(q,old))
                bone_previous[role]=q
            if options.get('effector') is not None:
                config,side,target,pole,start,end,tolerance=m6_effector_config(options)
                if start<=frame<=end:
                    role='hand_l' if side=='left' else 'hand_r'
                    endpoint=evaluated.matrix_world@evaluated.pose.bones[compat['mapping'][role]].head
                    residual=(endpoint*unit-target).length
                    if effector_report is None:
                        effector_report={'side':side,'frame_range':[start,end],'target_world_m':list(target),
                            'tolerance_m':tolerance,'maximum_residual_m':0.0,'sampled_times':0,'sample_step_frames':.25,
                            'surface_type':'configured_static_analytic_plane'}
                    effector_report['maximum_residual_m']=max(effector_report['maximum_residual_m'],residual)
                    effector_report['sampled_times']+=1
                    if residual>tolerance:
                        errors.append('EFFECTOR_CONTACT_LOST: frame '+str(frame))
    finally:
        m3_restore(obj,state)
    contact_intervals+=list(active_contacts.values())
    for interval in contact_intervals:
        interval['status']='UNVERIFIED' if not interval['sample_pairs'] else 'PASS' if interval['path_distance_m']<=threshold else 'FAIL'
    # Sliding is measured only when there are two confirmed contact samples.
    # Missing evidence is null, never an invented zero.
    sliding={side:{'translation_during_contact_m':travel if contact_count[side] else None,
        'sample_pairs':contact_count[side],'status':'UNVERIFIED' if not contact_count[side] else 'PASS' if travel<=threshold else 'FAIL'} for side,travel in contact_travel.items()}
    for side,row in sliding.items():
        if row['status']=='FAIL':
            errors.append('UNACCEPTABLE_SLIDING: '+side+' '+str(round(row['translation_during_contact_m'],6))+' m')
    angular_limit=m6_number(options.get('maximum_angular_step_deg',90),'maximum angular sample step',0,180)
    stepped=options.get('stage',candidate.get('production_generation_stage','')).upper() in ('BLOCKING','POSE') and all(
        k.interpolation=='CONSTANT' for s,l,t,c in action_curves(candidate,m2_slot(obj)) for k in c.keyframe_points)
    if max_angular>angular_limit:
        if stepped:
            warnings.append('INTENTIONAL_STEPPED_POSE_JUMP: '+str(round(max_angular,3))+' degrees; spline temporal QA still required')
        else:
            errors.append('SEVERE_LIMB_POP: angular change exceeds configured sample threshold')
    delta=Vector(root_positions[-1])-Vector(root_positions[0]);distance=sum((Vector(b)-Vector(a)).length for a,b in zip(root_positions,root_positions[1:]))
    duration=(frames[-1]-frames[0])/fps
    return {'passed':not errors,'errors':list(dict.fromkeys(errors))[:30],'warnings':list(dict.fromkeys(warnings))[:30],
        'metrics':{'sampled_frames':len(frames),'maximum_target_residual_m':worst_reach,'maximum_ground_penetration_m':worst_pen,
            'maximum_limb_length_ratio':worst_stretch,'sliding':sliding,'sliding_tolerance_m':threshold,
            'contact_intervals':contact_intervals[:64],'contact_interval_count':len(contact_intervals),
            'effector_contact':effector_report,
            'maximum_contact_sample_step_m':max_jump,'maximum_angular_sample_step_deg':max_angular,
            'angular_sample_threshold_deg':angular_limit,'intentional_stepped_blocking':stepped,
            'root_motion':{'displacement_xyz_m':list(delta),'distance_m':distance,'average_speed_m_s':distance/duration if duration else 0},
            'problem_frames':sorted(set(problem_frames))[:30]},
        'visual_verification':'required','qa_scope':'Sampled evaluated bones and confirmed requested contacts; surface deformation and temporal artistic QA are separate mandatory gates'}

def m3_verify(obj,source,candidate,proposal,options):
    report=m6_sample_candidate(obj,candidate,proposal['compatibility'],proposal['poses'],options)
    if proposal['stage']=='EFFECTOR' and source:
        config,side,target,pole,start,end,tolerance=m6_effector_config(options);lo,hi=fw_fences(start,end)
        samples=[float(f) for f in range(int(math.floor(proposal['frame_range'][0])),int(math.ceil(proposal['frame_range'][1]))+1) if f<=lo or f>=hi]+[lo,hi]
        source_curves={(c.data_path,c.array_index):c for s,l,t,c in action_curves(source,None)}
        error=0.0
        for s,l,t,curve in action_curves(candidate,None):
            old=source_curves.get((curve.data_path,curve.array_index))
            if old:
                for frame in samples:
                    error=max(error,abs(curve.evaluate(frame)-old.evaluate(frame)))
        report['metrics']['outside_effector_window_max_channel_error']=error
        if error>1e-5:
            report['passed']=False;report['errors'].append('EFFECTOR_WINDOW_CHANGED: outside contact interval verification failed')
    source_intact=m2_digest(m3_source_snapshot(source))==proposal['source_fingerprint']
    report['metrics']['source_action_immutable']=source_intact
    if not source_intact:
        report['passed']=False;report['errors'].append('SOURCE_ACTION_CHANGED: generation source preservation failed')
    report['metrics']['generated_lifecycle']=candidate.get('production_generation_lifecycle')
    return report

def m6_validate(obj,action,options):
    compat=m6_compatibility(obj,options)
    if not compat['supported']:
        return compat
    return m6_sample_candidate(obj,action,compat,m6_poses(options),options)

def m6_silhouette(obj,compat,frame,options):
    state=m3_capture(obj)
    try:
        m3_set_frame(frame);evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());unit=bpy.context.scene.unit_settings.scale_length
        basis=obj.matrix_world.to_3x3()@Matrix(compat['body_basis_armature']);basis.normalize()
        center=evaluated.matrix_world@evaluated.pose.bones[compat['mapping']['pelvis']].head
        points={role:tuple(basis.inverted()@((evaluated.matrix_world@evaluated.pose.bones[name].head)-center)*unit) for role,name in compat['mapping'].items() if role in ('head','pelvis','chest','hand_l','hand_r','foot_l','foot_r')}
        views={}
        for view,angle in [('FRONT',0),('SIDE',math.pi/2),('THREE_QUARTER',math.pi/4)]:
            projected={role:[v[0]*math.cos(angle)+v[1]*math.sin(angle),v[2]] for role,v in points.items()}
            left=Vector(projected['hand_l']);right=Vector(projected['hand_r']);chest=Vector(projected['chest'])
            views[view]={'projected_bone_heads_m':projected,'hand_separation_m':(left-right).length,
                'hand_to_torso_m':{'left':(left-chest).length,'right':(right-chest).length},
                'observation':'Bone-head projection only; render required for actual mesh overlap, clipping and negative space'}
        return {'frame':frame,'views':views,'artistic_score':None,'visual_verification':'required'}
    finally:
        m3_restore(obj,state)
'''


COMPAT_READ = r'''
obj=m2_object(params['object_name'])
m3_preflight(obj,params.get('options',{}))
state=m3_capture(obj)
try:
    result=m6_compatibility(obj,params.get('options',{}))
    result['rig_fingerprint']=m6_rig_fingerprint(obj) if obj.type=='ARMATURE' else None
finally:
    m3_restore(obj,state)
'''


POSE_READ = r'''
obj=m2_object(params['object_name'])
m3_preflight(obj,params.get('options',{}))
options=params.get('options',{})
compat=m6_compatibility(obj,options)
if not compat['supported']:
    result=compat
else:
    poses=m6_poses(options)
    result=m6_plan_validation(obj,compat,poses,options)
    result['armature']=obj.name
    result['compatibility']=compat
'''


ANALYZE_READ = r'''
obj=m2_object(params['object_name'])
m3_preflight(obj,params.get('options',{}))
options=params.get('options',{})
compat=m6_compatibility(obj,options)
if not compat['supported']:
    result=compat
else:
    action=obj.animation_data.action if obj.animation_data else None
    requested=options.get('action_name')
    if requested and (action is None or action.name!=requested):
        raise ValueError('ACTION_NOT_ACTIVE: activate reviewed candidate before generation QA')
    if action is None:
        raise ValueError('GENERATED_ACTION_REQUIRED')
    poses=m6_poses(options)
    result=m6_sample_candidate(obj,action,compat,poses,options)
    result['action']=action.name
    result['armature']=obj.name
    result['action_fingerprint']=m2_digest(m2_snapshot(action))
'''


SILHOUETTE_READ = r'''
obj=m2_object(params['object_name'])
m3_preflight(obj,params.get('options',{}))
options=params.get('options',{})
compat=m6_compatibility(obj,options)
if not compat['supported']:
    result=compat
else:
    frame=m6_number(options.get('frame',bpy.context.scene.frame_current),'silhouette frame')
    result=m6_silhouette(obj,compat,frame,options)
    result['armature']=obj.name
'''


class _StageSpecializer(ast.NodeTransformer):
    """Remove statically unreachable branches from an immutable stage backend.

    This is a source transformation, not dynamic code execution. Unknown
    conditions are retained and the normal upstream validator checks the result.
    """

    _unknown = object()

    def __init__(self, stage: str):
        self.stage = stage

    def _value(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id == 'stage':
            return self.stage
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            values = [self._value(x) for x in node.elts]
            return values if all(v is not self._unknown for v in values) else self._unknown
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'upper' and not node.args and not node.keywords):
            value = self._value(node.func.value)
            return value.upper() if isinstance(value, str) else self._unknown
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'options'
                and node.func.attr == 'get' and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == 'stage'):
            return self.stage
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = self._value(node.left); right = self._value(node.comparators[0])
            if left is self._unknown or right is self._unknown:
                return self._unknown
            operator = node.ops[0]
            if isinstance(operator, ast.In):
                return left in right
            if isinstance(operator, ast.NotIn):
                return left not in right
            if isinstance(operator, ast.Eq):
                return left == right
            if isinstance(operator, ast.NotEq):
                return left != right
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = self._value(node.operand)
            return not value if value is not self._unknown else self._unknown
        return self._unknown

    def visit_If(self, node):
        value = self._value(node.test)
        if value is self._unknown:
            return self.generic_visit(node)
        result = []
        for statement in node.body if value else node.orelse:
            transformed = self.visit(statement)
            result.extend(transformed if isinstance(transformed, list) else [transformed])
        return result

    def visit_IfExp(self, node):
        value = self._value(node.test)
        if value is self._unknown:
            return self.generic_visit(node)
        return self.visit(node.body if value else node.orelse)


@lru_cache(maxsize=16)
def runtime_for_stage(stage: str | None = None) -> str:
    """Return a smaller backend signed to one exact generation stage.

    Read-only compatibility/measurement callers may omit the stage. Mutations
    use this factory so the unrelated IK solver, hold splitter and secondary
    animation algorithms do not all occupy the upstream 20,000-node budget.
    """
    if stage is None:
        return GENERATION_RUNTIME
    selected = stage.upper()
    if selected not in {'BLOCKING','POSE','BREAKDOWNS','SPLINE','CURVES','OVERLAP','FOLLOW_THROUGH',
                        'ROOT','CONTACTS','REFINE','TIMING','ARCS','BALANCE','EFFECTOR'}:
        raise ValueError('UNSUPPORTED_GENERATION_STAGE: ' + selected)
    tree = ast.parse(GENERATION_RUNTIME)
    specializer = _StageSpecializer(selected)
    functions = {'m3_propose','m3_apply','m6_plan_validation','m6_curve_interpolation'}
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            tree.body[index] = specializer.visit(node)
    # The actual frozen proposal/options must still match the specialized code.
    # This guard is deliberately inserted after pruning so it cannot disappear.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == 'm3_apply':
            guard = ast.parse("if proposal['stage'] != " + repr(selected) + ":\n    raise ValueError('GENERATION_BACKEND_STAGE_MISMATCH')").body[0]
            node.body.insert(0, guard)
        elif isinstance(node, ast.FunctionDef) and node.name == 'm3_propose':
            guard = ast.parse("if options.get('stage','BLOCKING').upper() != " + repr(selected) + ":\n    return {'supported':False,'reasons':['GENERATION_BACKEND_STAGE_MISMATCH']}").body[0]
            node.body.insert(0, guard)
    return ast.unparse(ast.fix_missing_locations(tree)) + '\n'
