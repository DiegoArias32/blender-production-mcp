"""Contact-preserving translations or rotation-only two-bone FK solving.

Candidate curves are sampled cubic Hermite tracks. Original Actions/handles
remain untouched; dense deviation and derivative verification bounds the bake.
"""
from .runtime import CONTACT_RUNTIME

FOOT_RUNTIME=CONTACT_RUNTIME+r'''
def m3_allowed_drivers(obj,options):
    return []

def m3_context(obj,options):
    return {'rig':m3_context_base(obj),'surfaces':{s['name']:s['signature'] for s in contact_static_surfaces(options.get('surfaces',[]))}}

def contact_adapter(obj,foot):
    chain=foot.get('chain',[])
    if len(chain)!=3 or any(n not in obj.pose.bones for n in chain): raise ValueError('Explicit upper/lower/foot chain required')
    upper,lower,end=[obj.pose.bones[n] for n in chain]
    if lower.parent!=upper or end.parent!=lower: raise ValueError('Noncontiguous chain unsupported')
    if any(any(p.lock_rotation) or abs(p.scale.x-1)>1e-6 or abs(p.scale.y-1)>1e-6 or abs(p.scale.z-1)>1e-6 for p in (upper,lower,end)):
        raise ValueError('Locked or scaled limb unsupported')
    if any(p.lock_rotations_4d and p.lock_rotation_w for p in (upper,lower,end)):
        raise ValueError('Quaternion W lock unsupported')
    solvers=[c for c in lower.constraints if c.type=='IK' and not c.mute and c.influence>.99999]
    if solvers:
        if len(solvers)!=1: raise ValueError('Multiple IK solvers unsupported')
        solver=solvers[0];control=obj.pose.bones.get(foot.get('control',''))
        if solver.chain_count!=2 or solver.target!=obj or control is None or solver.subtarget!=control.name:
            raise ValueError('Explicit full influence self-targeted two-bone IK required')
        if solver.pole_target not in (None,obj): raise ValueError('External IK pole dependency unsupported')
        if any(c.type!='COPY_ROTATION' or c.target!=obj or c.subtarget!=control.name or c.mute or abs(c.influence-1)>1e-7
               or c.owner_space!='POSE' or c.target_space!='POSE' or c.mix_mode!='REPLACE' for c in end.constraints):
            raise ValueError('Foot orientation constraints require a direct local control adapter')
        if control.bone.use_deform or control.constraints or any(control.lock_location): raise ValueError('IK control is not independently translatable')
        if solver.use_stretch: raise ValueError('Contact edits currently require IK stretch disabled; analysis supports intentional stretch')
        parent=control.parent
        while parent:
            if parent.name in chain: raise ValueError('IK target depends on solved limb')
            parent=parent.parent
        if upper.constraints or len(lower.constraints)!=1: raise ValueError('Additional limb constraints unsupported')
        return {'mode':'IK','control':control.name,'chain':chain}
    if any(p.constraints for p in (upper,lower,end)): raise ValueError('Unknown constrained FK limb unsupported')
    if any((p.bone.head_local-p.parent.bone.tail_local).length>1e-5 for p in (lower,end)):
        raise ValueError('FK solver requires rest joints meeting at bone endpoints')
    if any(p.rotation_mode not in {'QUATERNION','XYZ','XZY','YXZ','YZX','ZXY','ZYX'} for p in (upper,lower,end)):
        raise ValueError('Unsupported FK rotation mode')
    if any(p.bone.inherit_scale!='FULL' or not p.bone.use_inherit_rotation for p in (upper,lower,end)):
        raise ValueError('Custom FK inheritance unsupported')
    return {'mode':'FK','chain':chain}

def contact_offsets(sample,options):
    offsets={}
    for name,rows in sample['feet'].items():
        values=[];anchor=None;previous_marker=None;previous_surface=None;last=Vector((0,0,0));release_frame=None;release_offset=None
        for row in rows:
            phase=row['phase'];marker=row['marker'];hit=row['hits'][marker]
            active=phase in {'HEEL_STRIKE','FOOT_FLAT','MID_STANCE','HEEL_RISE'} and hit is not None
            point=Vector(row['points'][marker])
            if active:
                if anchor is None or marker!=previous_marker or hit['surface_object']!=previous_surface:
                    anchor=point+last
                normal=Vector(hit['surface_normal'])
                movement=anchor-point
                last=movement-normal*movement.dot(normal)
                previous_marker=marker;previous_surface=hit['surface_object'];release_frame=None
            else:
                if anchor is not None:
                    release_frame=row['frame'];release_offset=last.copy()
                anchor=None;previous_marker=None;previous_surface=None
                if release_frame is not None:
                    blend=max(float(options.get('blend_frames',3)),.25)
                    last=release_offset*(1-smoothstep((row['frame']-release_frame)/blend))
                else: last=Vector((0,0,0))
            values.append(list(last))
        # Symmetric triangular kernel blends marker switches without rotating the foot.
        # Constant boundary extension retains an already planted first/last sample.
        step=sample['frames'][1]-sample['frames'][0] if len(sample['frames'])>1 else 1.
        radius=max(1,int(float(options.get('phase_blend_frames',1.))/step))
        smoothed=[]
        for i in range(len(values)):
            total=Vector((0,0,0));weight_sum=0.
            for j in range(-radius,radius+1):
                weight=radius+1-abs(j);total+=Vector(values[min(len(values)-1,max(0,i+j))])*weight;weight_sum+=weight
            smoothed.append(list(total/weight_sum))
        offsets[name]=smoothed
    return offsets

def m3_propose(obj,source,options):
    try:
        if source is None: raise ValueError('An active source Action is required')
        if options.get('confirm_contacts') is not True: raise ValueError('confirm_contacts=true and reviewed local markers are required')
        feet=contact_feet(obj,options)
        adapters=[contact_adapter(obj,f) for f in feet]
        if len({n for a in adapters for n in a['chain']})!=3*len(adapters): raise ValueError('Overlapping limb adapters unsupported')
        if any(c.data_path.endswith('scale') or '.constraints[' in c.data_path for _,_,_,c in action_curves(source,m2_slot(obj))):
            raise ValueError('Animated scale/solver constraints unsupported for foot correction')
        if options.get('frame_start',bpy.context.scene.frame_start)!=bpy.context.scene.frame_start or options.get('frame_end',bpy.context.scene.frame_end)!=bpy.context.scene.frame_end:
            raise ValueError('M3 foot correction currently verifies the complete scene range; partial ranges unsupported')
        # Keep enough native animation channels to re-evaluate the preserved source exactly.
        keys={(c.data_path,c.array_index) for _,_,_,c in action_curves(source,m2_slot(obj))}
        for adapter in adapters:
            names=[adapter['control']] if adapter['mode']=='IK' else adapter['chain']
            for name in names:
                pose=obj.pose.bones[name]
                channel='location' if adapter['mode']=='IK' else 'rotation_quaternion' if pose.rotation_mode=='QUATERNION' else 'rotation_euler'
                size=4 if channel=='rotation_quaternion' else 3
                if not all((pose.path_from_id()+'.'+channel,i) in keys for i in range(size)):
                    raise ValueError('Complete keyed correction channels required to restore original evaluation')
        opts=dict(options,step=float(options.get('sample_step',.25)))
        sample=contact_sample(obj,opts)
        if len(sample['frames'])*len(feet)>1600: raise ValueError('CAPACITY_EXCEEDED: foot correction budget is <=1600 effector-frame samples')
        capacity=contact_capacity(obj,options,feet,adapters,sample['frames'])
        if not capacity['supported']:
            raise ValueError('CAPACITY_EXCEEDED: '+capacity['reason'])
        before=contact_summary(sample,options)
        statuses={f['bone']:f['contact_status'] for f in before['feet']}
        if 'AMBIGUOUS_SURFACE' in statuses.values():
            raise ValueError('AMBIGUOUS_SURFACE: competing support surfaces for '+', '.join(b for b,s in statuses.items() if s=='AMBIGUOUS_SURFACE')+'; resolve the surface list first')
        if 'CONTACT_CONFIRMED' not in statuses.values():
            raise ValueError('NO_CONFIRMED_CONTACT: no foot has a confirmed contact model ('+before['contact_status']+'); there is nothing verifiable to lock')
        offsets=contact_offsets(sample,options)
        return {'supported':True,'candidate_policy':'duplicate','adapters':adapters,'feet':feet,
            'frames':sample['frames'],'targets':offsets,'before_contacts':before,'contact_statuses':statuses,
            'warnings':['Candidate contact channels are resampled to cubic Hermite curves; source Bézier keys/handles stay intact.',
                'Only explicit reviewed markers/limbs are edited. Roll orientation is preserved; geometric support is heuristic.'],
            'strategy':'phase_marker_tangent_lock_with_quintic_release','sampling_step':opts['step'],'capacity':capacity}
    except ValueError as exc:
        return {'supported':False,'status':'unsupported_or_ambiguous','warnings':[str(exc)]}

def contact_key_values(obj,adapter,offset):
    scale=bpy.context.scene.unit_settings.scale_length
    displacement=obj.matrix_world.to_3x3().inverted()@Vector(offset)/scale
    if adapter['mode']=='IK':
        pose=obj.pose.bones[adapter['control']]
        desired=pose.matrix.copy();desired.translation+=displacement
        local=obj.convert_space(pose_bone=pose,matrix=desired,from_space='POSE',to_space='LOCAL')
        return [(pose.name,'location',list(local.translation))]
    upper,lower,foot=[obj.pose.bones[n] for n in adapter['chain']]
    a,b,c=upper.head.copy(),lower.head.copy(),foot.head.copy()
    target=c+displacement;direction=target-a;distance=direction.length
    length1=(b-a).length;length2=(c-b).length
    if distance>=length1+length2-1e-5 or distance<=abs(length1-length2)+1e-5:
        raise ValueError('FK target outside conservative two-bone reach')
    axis=direction.normalized();bend=b-a-axis*(b-a).dot(axis)
    if bend.length<1e-5: raise ValueError('FK bend plane ambiguous near singularity')
    projection=(length1*length1-length2*length2+distance*distance)/(2*distance)
    new_knee=a+axis*projection+bend.normalized()*math.sqrt(max(0.,length1*length1-projection*projection))
    old_matrices=[p.matrix.copy() for p in (upper,lower,foot)]
    rotations=[(b-a).rotation_difference(new_knee-a)@old_matrices[0].to_quaternion(),
        (c-b).rotation_difference(target-new_knee)@old_matrices[1].to_quaternion(),old_matrices[2].to_quaternion()]
    values=[]
    for pose,rotation,head in zip((upper,lower,foot),rotations,(a,new_knee,target)):
        desired=rotation.to_matrix().to_4x4();desired.translation=head
        local=obj.convert_space(pose_bone=pose,matrix=desired,from_space='POSE',to_space='LOCAL')
        channel='rotation_quaternion' if pose.rotation_mode=='QUATERNION' else 'rotation_euler'
        value=local.to_quaternion() if channel=='rotation_quaternion' else local.to_euler(pose.rotation_mode,pose.rotation_euler)
        if channel=='rotation_quaternion' and value.dot(pose.rotation_quaternion)<0: value.negate()
        if channel=='rotation_quaternion': pose.rotation_quaternion=value
        else: pose.rotation_euler=value
        bpy.context.view_layer.update()
        values.append((pose.name,channel,list(value)))
    return values

def contact_write_tracks(obj,tracks,frames,tangents=None):
    action=obj.animation_data.action
    created=0;removed=0
    for identity,values in tracks.items():
        bone_name,channel,index=identity
        pose=obj.pose.bones[bone_name];path=pose.path_from_id()+'.'+channel
        curve=next((c for _,_,_,c in action_curves(action,m2_slot(obj)) if c.data_path==path and c.array_index==index),None)
        if curve is None: raise ValueError('Expected keyed correction channel missing')
        for point_index in range(len(curve.keyframe_points)-1,-1,-1):
            point=curve.keyframe_points[point_index]
            if frames[0]-1e-6<=point.co.x<=frames[-1]+1e-6:
                curve.keyframe_points.remove(point,fast=True);removed+=1
        slopes=tangents[identity] if tangents is not None else [v[0] for v in derivative([[v] for v in values],frames)]
        for i,(frame,value) in enumerate(zip(frames,values)):
            point=curve.keyframe_points.insert(frame,value,options={'FAST'})
            left=(frame-frames[i-1])/3 if i else (frames[1]-frame)/3 if len(frames)>1 else 1/3
            right=(frames[i+1]-frame)/3 if i<len(frames)-1 else left
            point.interpolation='BEZIER';point.handle_left_type='FREE';point.handle_right_type='FREE'
            point.handle_left=(frame-left,value-slopes[i]*left);point.handle_right=(frame+right,value+slopes[i]*right)
            created+=1
        curve.update()
    return {'keys_created':created,'keys_replaced':removed,'affected_channels':len(tracks),'frames_processed':len(frames)}

def m3_apply(obj,source,candidate,proposal,options):
    frames=proposal['frames'];tracks={};tangents={};state=m3_capture(obj)
    try:
        m3_assign(obj,source)
        for i,frame in enumerate(frames):
            # Small local samples preserve original Bézier tangents; a secant across
            # the whole bake interval would introduce acceleration spikes at keys.
            epsilon=.005
            samples={};sample_frames=[max(frames[0],frame-epsilon),frame,min(frames[-1],frame+epsilon)]
            offsets=contact_interpolated_offsets(proposal,sample_frames)
            for j,sample_frame in enumerate(sample_frames):
                m3_set_frame(sample_frame)
                for foot,adapter in zip(proposal['feet'],proposal['adapters']):
                    values=contact_key_values(obj,adapter,offsets[foot['bone']][j])
                    for name,channel,vector in values:
                        for axis,value in enumerate(vector): samples.setdefault((name,channel,axis),[]).append(value)
            for identity,values in samples.items():
                tracks.setdefault(identity,[]).append(values[1])
                tangents.setdefault(identity,[]).append((values[2]-values[0])/max(sample_frames[2]-sample_frames[0],1e-6))
    finally:
        m3_assign(obj,candidate);m3_restore(obj,state)
    return contact_write_tracks(obj,tracks,frames,tangents)

def contact_protected_names(obj,adapters):
    affected={k for a in adapters for k in a['chain']}|{a.get('control') for a in adapters}
    for name in list(affected):
        if name is not None and name in obj.pose.bones:
            affected.update(p.name for p in obj.pose.bones[name].children_recursive)
    return [n for n in obj.pose.bones.keys() if n not in affected]

def contact_capacity(obj,options,feet,adapters,frames):
    # Verification re-samples at a denser step and measures every protected bone twice; its limits
    # (contact_frames <=4001 frames, contact_protected_sample <=100000 bone/frames) are checked here
    # so an oversized case is unsupported at propose time, not after checkpoint + candidate (AUD-008).
    dense_step=max(.125,min(.25,float(options.get('sample_step',.25))/2))
    try:
        dense=len(contact_frames(dict(options,step=dense_step)))
    except ValueError:
        dense=None
    protected=len(contact_protected_names(obj,adapters))
    capacity={'propose_effector_samples':len(frames)*len(feet),'propose_limit':1600,
        'verify_step':dense_step,'verify_frames':dense,'verify_frame_limit':4001,'protected_bones':protected,
        'protected_bone_frames_per_pass':None if dense is None else dense*protected,'protected_limit':100000}
    if dense is None:
        capacity.update(supported=False,reason='verification at step '+str(dense_step)+' exceeds 4001 frames; narrow the scene range')
    elif dense*protected>100000:
        capacity.update(supported=False,reason='verification needs '+str(dense*protected)+' protected bone/frames per pass (> 100000)')
    else:
        capacity.update(supported=True,reason=None)
    return capacity

def contact_protected_sample(obj,frames,names):
    if len(frames)*len(names)>100000:
        raise ValueError('Protected transform sampling exceeds 100000 bone/frames')
    state=m3_capture(obj);samples={name:[] for name in names}
    unit=bpy.context.scene.unit_settings.scale_length
    try:
        for frame in frames:
            m3_set_frame(frame)
            evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for name in names:
                pose=evaluated.pose.bones[name];matrix=evaluated.matrix_world@pose.matrix
                head=matrix.translation*unit;tail=evaluated.matrix_world@pose.tail*unit
                samples[name].append({'position':list(head),'tail':list(tail),
                    'rotation':list(matrix.to_quaternion().normalized()),'scale':list(matrix.to_scale()),
                    'length':(tail-head).length,'handedness':-1 if matrix.determinant()<0 else 1})
        return samples
    finally:
        m3_restore(obj,state)

def contact_shortest_rotation_error(first,second):
    difference=Quaternion(first).rotation_difference(Quaternion(second)).normalized()
    return math.degrees(2*math.atan2(Vector((difference.x,difference.y,difference.z)).length,abs(difference.w)))

def contact_evaluate_pair(obj,source,candidate,proposal,options):
    dense=dict(options,step=max(.125,min(.25,float(options.get('sample_step',.25))/2)))
    chains=[a['chain'] for a in proposal['adapters']]
    names=contact_protected_names(obj,proposal['adapters'])
    state=m3_capture(obj);active=obj.animation_data.action;slot=m2_slot(obj)
    try:
        m3_assign(obj,source);m3_set_frame(dense.get('frame_start',bpy.context.scene.frame_start))
        before=contact_sample(obj,dense);limbs_before=contact_limb_sample(obj,chains,before['frames'])
        protected_before=contact_protected_sample(obj,before['frames'],names)
        m3_assign(obj,candidate);m3_set_frame(dense.get('frame_start',bpy.context.scene.frame_start))
        after=contact_sample(obj,dense);limbs_after=contact_limb_sample(obj,chains,after['frames'])
        protected_after=contact_protected_sample(obj,after['frames'],names)
        pairs=[(a,b) for name in names for a,b in zip(protected_before[name],protected_after[name])]
        protected={'position_m':max(((Vector(a['position'])-Vector(b['position'])).length for a,b in pairs),default=0.),
            'tail_m':max(((Vector(a['tail'])-Vector(b['tail'])).length for a,b in pairs),default=0.),
            'rotation_degrees':max((contact_shortest_rotation_error(a['rotation'],b['rotation']) for a,b in pairs),default=0.),
            'length_m':max((abs(a['length']-b['length']) for a,b in pairs),default=0.),
            'scale':max((max(abs(x-y) for x,y in zip(a['scale'],b['scale'])) for a,b in pairs),default=0.),
            'reflection_changes':sum(a['handedness']!=b['handedness'] for a,b in pairs)}
        return before,after,limbs_before,limbs_after,protected
    finally:
        m3_assign(obj,active,slot);m3_restore(obj,state)

def contact_interpolated_offsets(proposal,frames):
    source_frames=proposal['frames'];output={}
    for name,values in proposal['targets'].items():
        slopes=derivative(values,source_frames);evaluated=[]
        for frame in frames:
            if len(source_frames)==1:
                evaluated.append(values[0]);continue
            index=max(0,min(len(source_frames)-2,sum(v<=frame for v in source_frames)-1))
            dt=source_frames[index+1]-source_frames[index];t=(frame-source_frames[index])/dt
            evaluated.append([(2*t**3-3*t*t+1)*a+(t**3-2*t*t+t)*dt*va+(-2*t**3+3*t*t)*b+(t**3-t*t)*dt*vb
                for a,b,va,vb in zip(values[index],values[index+1],slopes[index],slopes[index+1])])
        output[name]=evaluated
    return output

def m3_verify(obj,source,candidate,proposal,options):
    before,after,limbs_before,limbs_after,protected=contact_evaluate_pair(obj,source,candidate,proposal,options)
    fps=before['fps'];frames=before['frames'];expected=contact_interpolated_offsets(proposal,frames)
    target_error=0.;roll_error=0.;accel_increase=0.;joint_increase=0.;stretch_error=0.
    velocity_increase=0.;angular_acceleration_increase=0.;pop_count_before=0;pop_count_after=0;plane_increase=0.;knee_error=0.;new_pops=[]
    for name,left in before['feet'].items():
        right=after['feet'][name]
        for i,(a,b) in enumerate(zip(left,right)):
            for marker in ('heel','sole','toe'):
                desired=Vector(a['points'][marker])+Vector(expected[name][i])
                target_error=max(target_error,(Vector(b['points'][marker])-desired).length)
            roll_error=max(roll_error,m2_angle(a['orientation'],b['orientation']))
        da=motion_derivatives([{'position':r['points']['sole'],'rotation':r['orientation']} for r in left],frames,fps)
        db=motion_derivatives([{'position':r['points']['sole'],'rotation':r['orientation']} for r in right],frames,fps)
        accel_increase=max(accel_increase,max((norm(b)-norm(a) for a,b in zip(da['acceleration'],db['acceleration'])),default=0.))
        velocity_increase=max(velocity_increase,max((norm(b)-norm(a) for a,b in zip(da['velocity'],db['velocity'])),default=0.))
        angular_acceleration_increase=max(angular_acceleration_increase,max((abs(b)-abs(a) for a,b in zip(da['angular_acceleration'],db['angular_acceleration'])),default=0.))
    for name,samples in limbs_before.items():
        a=joint_metrics(samples,frames,fps,options);b=joint_metrics(limbs_after[name],frames,fps,options)
        joint_increase=max(joint_increase,max((y-x for x,y in zip(a['steps'],b['steps'])),default=0.))
        plane_increase=max(plane_increase,max((y-x for x,y in zip(a['bend_plane_steps'],b['bend_plane_steps'])),default=0.))
        knee_error=max(knee_error,max((Vector(x['knee'])-Vector(y['knee'])).length for x,y in zip(samples,limbs_after[name])))
        stretch_error=max(stretch_error,max((abs(x-y) for x,y in zip(a['lengths'],b['lengths'])),default=0.))
        pop_count_before+=len(a['pops']);pop_count_after+=len(b['pops'])
        for event in b['pops']:
            if not any(abs(previous['frame']-event['frame'])<=.125 for previous in a['pops']):
                new_pops.append({'limb':name,'frame':event['frame']})
    summary_before=contact_summary(before,options);summary_after=contact_summary(after,options)
    drift_before=summary_before['metrics']['max_contact_sliding_m'];drift_after=summary_after['metrics']['max_contact_sliding_m']
    no_confirmed=drift_before is None
    # Compare a fixed source phase classification as well; a correction cannot hide drift by relabeling contact.
    fixed_drift=0.
    for name,left in before['feet'].items():
        for segment in intervals(left):
            if segment['value']=='SWING': continue
            indices=[i for i,r in enumerate(left) if segment['start']<=r['frame']<=segment['end']]
            marker=left[indices[0]]['marker'];hit=left[indices[0]]['hits'][marker]
            if hit is None: continue
            normal=Vector(hit['surface_normal']);origin=Vector(after['feet'][name][indices[0]]['points'][marker])
            for i in indices:
                movement=Vector(after['feet'][name][i]['points'][marker])-origin
                fixed_drift=max(fixed_drift,(movement-normal*movement.dot(normal)).length)
    metrics={'sliding_before_m':drift_before,'sliding_after_m':None if no_confirmed else fixed_drift,
        'contact_status_before':summary_before['contact_status'],'contact_status_after':summary_after['contact_status'],
        'max_target_error_m':target_error,'max_foot_rotation_error_degrees':roll_error,
        'max_protected_trajectory_error_m':protected['position_m'],
        'max_protected_tail_error_m':protected['tail_m'],'max_protected_rotation_error_degrees':protected['rotation_degrees'],
        'max_protected_length_change_m':protected['length_m'],'max_protected_scale_error':protected['scale'],
        'protected_reflection_changes':protected['reflection_changes'],'max_acceleration_increase_m_s2':accel_increase,
        'max_velocity_increase_m_s':velocity_increase,'max_angular_acceleration_increase_deg_s2':angular_acceleration_increase,
        'max_joint_step_increase_degrees':joint_increase,'max_limb_length_change_m':stretch_error,
        'max_bend_plane_step_increase_degrees':plane_increase,'max_knee_trajectory_change_m':knee_error,
        'limb_pops_before':pop_count_before,'limb_pops_after':pop_count_after,'verification_samples':len(frames)}
    metrics['new_limb_pop_events']=new_pops
    errors=[]
    checks=[(target_error,float(options.get('position_tolerance_m',.001)),'contact trajectory deviation'),
        (roll_error,float(options.get('angular_tolerance_degrees',.1)),'foot roll changed'),
        (protected['position_m'],float(options.get('protected_tolerance_m',.0001)),'protected trajectory changed'),
        (protected['tail_m'],float(options.get('protected_tolerance_m',.0001)),'protected tail changed'),
        (protected['length_m'],float(options.get('protected_tolerance_m',.0001)),'protected length changed'),
        (protected['rotation_degrees'],float(options.get('protected_angular_tolerance_degrees',.01)),'protected orientation changed'),
        (protected['scale'],float(options.get('protected_scale_tolerance',.00001)),'protected scale changed'),
        (accel_increase,float(options.get('max_acceleration_increase_m_s2',6.)),'acceleration increase'),
        (velocity_increase,float(options.get('max_velocity_increase_m_s',.25)),'velocity increase'),
        (angular_acceleration_increase,float(options.get('max_angular_acceleration_increase_deg_s2',200.)),'angular acceleration increase'),
        (joint_increase,float(options.get('max_joint_step_increase_degrees',3.)),'joint pop increase'),
        (plane_increase,float(options.get('max_bend_plane_step_increase_degrees',5.)),'bend plane change'),
        (knee_error,float(options.get('max_knee_trajectory_change_m',.1)),'knee trajectory change'),
        (stretch_error,float(options.get('stretch_tolerance_m',.001)),'limb stretch introduced')]
    for measured,tolerance,label in checks:
        if measured>tolerance: errors.append(label+': '+str(measured)+' > '+str(tolerance))
    if protected['reflection_changes']: errors.append('Protected transform handedness changed')
    if new_pops or pop_count_after>pop_count_before: errors.append('Additional localized limb pops detected')
    if no_confirmed:
        errors.append('NO_CONFIRMED_CONTACT: the source has no confirmed contact; an improvement cannot be certified')
    elif fixed_drift>max(float(options.get('sliding_tolerance_m',.002)),drift_before*.4): errors.append('Contact drift did not improve sufficiently')
    return {'passed':not errors,'metrics':metrics,'errors':errors,'warnings':['Dense finite sampling bounds numerical error; visual comparison remains required.']}
'''
