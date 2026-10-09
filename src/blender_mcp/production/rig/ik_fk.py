"""Explicit two-bone IK/FK adapters with sampled world-space verification."""
from __future__ import annotations

from ..common.m3_transaction import transaction
from .semantics import SEMANTIC_RUNTIME
from ..common.fcurve_window import WINDOW_RUNTIME

IKFK_RUNTIME = SEMANTIC_RUNTIME + WINDOW_RUNTIME + r'''
def m3_adapter(obj,options):
    allowed={'chain','ik_target','pole_target','ik_constraint','switch','direction','bake','frame','frame_start','frame_end','step',
        'position_tolerance_m','rotation_tolerance_degrees','semantic_mapping','confirm_mapping'}
    if set(options)-allowed:
        raise ValueError('Unknown IK/FK adapter options: '+', '.join(sorted(set(options)-allowed)))
    if obj.type!='ARMATURE' or options.get('confirm_mapping') is not True:
        raise ValueError('Explicit reviewed chain mapping and confirm_mapping=true required')
    names=options.get('chain')
    if not isinstance(names,list) or len(names)!=3 or len(set(names))!=3:
        raise ValueError('chain must contain exact upper, lower, end bone names')
    chain=[obj.pose.bones.get(name) for name in names]
    if any(p is None for p in chain) or chain[1].parent!=chain[0] or chain[2].parent!=chain[1]:
        raise ValueError('Unsupported/ambiguous contiguous three-bone limb')
    target=obj.pose.bones.get(options.get('ik_target',''));pole=obj.pose.bones.get(options.get('pole_target',''))
    if target is None or pole is None or target==pole or target in chain or pole in chain:
        raise ValueError('Explicit distinct IK and pole control bones required')
    if target.parent or pole.parent or target.constraints or pole.constraints or target.bone.use_deform or pole.bone.use_deform:
        raise ValueError('Adapter requires independent unconstrained nondeform target/pole controls')
    if any(target.lock_location) or any(pole.lock_location) or any(target.lock_rotation):
        raise ValueError('Locked target/pole channels unsupported')
    constraints=[c for c in chain[1].constraints if c.type=='IK' and c.target==obj and c.subtarget==target.name and c.pole_target==obj and c.pole_subtarget==pole.name]
    if options.get('ik_constraint'):
        constraints=[c for c in constraints if c.name==options['ik_constraint']]
    if len(constraints)!=1:
        raise ValueError('Exactly one explicit matching IK constraint required')
    solver=constraints[0]
    if solver.chain_count!=2 or solver.use_stretch or not solver.use_tail or solver.use_rotation or solver.mute:
        raise ValueError('Only nonstretch two-bone tail IK without solver rotation supported')
    if chain[0].constraints or any(c!=solver for c in chain[1].constraints):
        raise ValueError('Additional proximal limb constraints unsupported')
    for c in chain[2].constraints:
        if c.type!='COPY_ROTATION' or c.target!=obj or c.subtarget!=target.name or c.mix_mode!='REPLACE' or c.target_space!='POSE' or c.owner_space!='POSE' or c.influence!=1 or c.mute:
            raise ValueError('End bone only supports full pose-space COPY_ROTATION from IK target')
    if any(p.rotation_mode not in {'QUATERNION','XYZ','XZY','YXZ','YZX','ZXY','ZYX'} for p in chain+[target,pole]):
        raise ValueError('Unsupported axis-angle control for IK/FK adapter')
    if any(any(p.lock_rotation) or any(p.lock_location) for p in chain):
        raise ValueError('Locked FK channels unsupported')
    if any(p.ik_stretch>1e-7 or p.use_ik_limit_x or p.use_ik_limit_y or p.use_ik_limit_z for p in chain[:2]):
        raise ValueError('IK stretch/limits require another explicit adapter')
    switch=options.get('switch');detected=m3_switch(obj,solver)
    if detected and not switch:
        raise ValueError('Driven IK requires explicit reviewed switch mapping')
    if switch:
        if not isinstance(switch,dict) or set(switch)-{'owner','property','ik_value','fk_value'}:
            raise ValueError('Invalid switch adapter')
        owner=obj if switch.get('owner')=='$object' else obj.pose.bones.get(switch.get('owner',''))
        prop=switch.get('property')
        if owner is None or not isinstance(prop,str) or prop not in owner or not isinstance(owner[prop],(int,float)):
            raise ValueError('Switch must be an existing numeric custom property')
        path=(owner.path_from_id() if owner!=obj else '')+'['+json.dumps(prop,ensure_ascii=False)+']'
        if not detected or not detected.get('supported') or detected['path']!=path:
            raise ValueError('Switch is not connected by a validated direct/inverse local driver')
        if switch.get('ik_value')!=detected['ik_value'] or switch.get('fk_value')!=detected['fk_value']:
            raise ValueError('Switch values disagree with the actual driver')
    return chain,target,pole,solver

def m3_allowed_drivers(obj,options):
    chain,target,pole,solver=m3_adapter(obj,options)
    allowed=[solver.path_from_id('influence')]
    action=obj.animation_data.action if obj.animation_data else None
    # Other limbs may carry constant on/off solver keys. They are captured and
    # preserved, never modified by this adapter; driven/variable solvers remain refused.
    if action:
        driven={c.data_path for c in obj.animation_data.drivers}
        for pose in obj.pose.bones:
            for constraint in pose.constraints:
                path=constraint.path_from_id('influence')
                rows=[c for s,l,t,c in action_curves(action,m2_slot(obj)) if c.data_path==path]
                if constraint.type=='IK' and path not in driven and rows and all(c.keyframe_points and all(abs(k.co.y-c.keyframe_points[0].co.y)<1e-8 and k.co.y in {0,1} for k in c.keyframe_points) for c in rows):
                    allowed.append(path)
    switch=options.get('switch')
    if switch:
        owner=obj if switch['owner']=='$object' else obj.pose.bones[switch['owner']]
        allowed.append((owner.path_from_id() if owner!=obj else '')+'['+json.dumps(switch['property'],ensure_ascii=False)+']')
    return allowed

def m3_context(obj,options):
    return m3_context_base(obj)

def m3_limb_samples(obj,frames,names):
    state=m3_capture(obj);samples=[]
    try:
        for frame in frames:
            m3_set_frame(frame);evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            samples.append({'frame':frame,'bones':{name:{'matrix':[list(r) for r in evaluated.pose.bones[name].matrix],
                'head':list(evaluated.matrix_world@evaluated.pose.bones[name].head),
                'tail':list(evaluated.matrix_world@evaluated.pose.bones[name].tail)} for name in names}})
        return samples
    finally:
        m3_restore(obj,state)

def ikfk_verification_frames(frames):
    # Keys, midpoints and every integer frame inside the window (AUD-011: a step > 1 must not leave
    # rendered frames unverified).
    integers=[float(f) for f in range(int(math.ceil(frames[0])),int(math.floor(frames[-1]))+1)]
    return sorted(set(frames+integers+[(a+b)/2 for a,b in zip(frames,frames[1:])]))

def ikfk_capacity(obj,source,frames,verification_frames):
    # m3_verify evaluates every bone in world space at the window samples plus the outside-window
    # frames, once for the source and once for the candidate. Same 100000 bone/frame per pass
    # budget as the other samplers, checked before any checkpoint or candidate (AUD-008).
    outside=ikfk_outside_frames(source,source,(frames[0],frames[-1]))[0]
    bones=len(obj.pose.bones)
    per_pass=(len(verification_frames)+len(outside))*bones
    return {'window_frames':len(verification_frames),'outside_frames':len(outside),'bones':bones,
            'world_bone_frames_per_pass':per_pass,'limit_per_pass':100000,'passes':2,'supported':per_pass<=100000}

def m3_propose(obj,source,options):
    try:
        chain,target,pole,solver=m3_adapter(obj,options)
        if source is None:
            raise ValueError('An active source Action is required for matching/baking')
        m3_guard(obj,source,m3_allowed_drivers(obj,options))
        if options.get('direction') not in {'fk_to_ik','ik_to_fk'}:
            raise ValueError('Unknown match direction')
        for key,default in [('position_tolerance_m',.001),('rotation_tolerance_degrees',.5)]:
            value=options.get(key,default)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
                raise ValueError('Invalid matching tolerance: '+key)
        frames=m3_frames(options,241) if options.get('bake') else [float(options.get('frame',bpy.context.scene.frame_current+bpy.context.scene.frame_subframe))]
        verification_frames=ikfk_verification_frames(frames)
        capacity=ikfk_capacity(obj,source,frames,verification_frames)
        if not capacity['supported']:
            raise ValueError('CAPACITY_EXCEEDED: verification needs '+str(capacity['world_bone_frames_per_pass'])
                             +' bone/frames per pass for '+str(capacity['bones'])+' bones (> 100000); narrow the window')
        samples=m3_limb_samples(obj,verification_frames,[p.name for p in chain])
        lo,hi=fw_fences(frames[0],frames[-1])
        # Source endpoint motion is checked independently of backend-generated keys.
        return {'supported':True,'candidate_policy':'duplicate','adapter':'explicit_two_bone_ik_fk',
            'direction':options['direction'],'frame_range':[frames[0],frames[-1]],'frames_processed':len(frames),
            'affected_controls':[target.name,pole.name],'affected_bones':[p.name for p in chain],
            'switch':options.get('switch'),'frames':frames,'samples':samples,'capacity':capacity,
            'mutation_window':{'window':[frames[0],frames[-1]],'fences':[lo,hi],
                'transition_intervals':[[lo,frames[0]],[frames[-1],hi]],
                'unchanged':'every frame <= '+str(lo)+' and >= '+str(hi)+' (verified)'},
            'warnings':['Pose/motion matching is numerically verified at sampled times. Visual QA remains required.',
                'Only the declared window changes: fence keys at '+str(lo)+' and '+str(hi)+' hold the source animation outside it; ('+str(lo)+', '+str(frames[0])+') and ('+str(frames[-1])+', '+str(hi)+') are transition intervals.']}
    except (ValueError,TypeError,KeyError) as exc:
        return {'supported':False,'status':'unsupported_or_ambiguous','reasons':[str(exc)],'warnings':[str(exc)]}

def m3_switch_mode(obj,solver,options,ik,frame):
    switch=options.get('switch')
    if switch:
        owner=obj if switch['owner']=='$object' else obj.pose.bones[switch['owner']]
        owner[switch['property']]=switch['ik_value'] if ik else switch['fk_value']
        owner.keyframe_insert(data_path='['+json.dumps(switch['property'],ensure_ascii=False)+']',frame=frame)
    else:
        solver.influence=1.0 if ik else 0.0
        solver.keyframe_insert(data_path='influence',frame=frame)
    obj.update_tag();bpy.context.view_layer.update()

def m3_key_pose(pose,frame,channels):
    for channel in channels:
        pose.keyframe_insert(data_path=channel,frame=frame)

def m3_rotation_channel(pose):
    return 'rotation_quaternion' if pose.rotation_mode=='QUATERNION' else 'rotation_euler'

def m3_key_index(action):
    output={}
    for row in m2_keys(action):
        for point in row['keys']:
            output[(row['slot'],row['layer'],row['strip'],row['path'],row['index'],point[0][0])]=point
    return output

def ikfk_written(obj,options,chain,target,pole,solver):
    # Every channel the adapter keys, declared up front so it can be fenced before writing.
    switch=options.get('switch')
    if switch:
        owner=obj if switch['owner']=='$object' else obj.pose.bones[switch['owner']]
        local='['+json.dumps(switch['property'],ensure_ascii=False)+']'
        full=(owner.path_from_id() if owner!=obj else '')+local
        wanted=switch['ik_value'] if options['direction']=='fk_to_ik' else switch['fk_value']
    else:
        owner=solver;local='influence';full=solver.path_from_id('influence')
        wanted=1.0 if options['direction']=='fk_to_ik' else 0.0
    rows=[{'owner':owner,'local':local,'path':full,'index':0,'kind':'switch','wanted':wanted}]
    bones=[target,pole] if options['direction']=='fk_to_ik' else chain
    for pose in bones:
        channels=['location'] if pose==pole else ['location',m3_rotation_channel(pose),'scale']
        for channel in channels:
            for index in range(4 if channel=='rotation_quaternion' else 3):
                rows.append({'owner':pose,'local':channel,'path':pose.path_from_id(channel),'index':index,'kind':'transform'})
    return rows

def ikfk_curve(action,path,index):
    for slot,layer,strip,curve in action_curves(action,None):
        if curve.data_path==path and curve.array_index==index:
            return curve
    return None

def ikfk_fence_written(obj,candidate,written,window):
    lo,hi=fw_fences(window[0],window[1]);reports=[]
    for row in written:
        curve=ikfk_curve(candidate,row['path'],row['index'])
        if curve is None:
            # Unanimated channel: two fence keys hold its static value outside the window.
            for fence in (lo,hi):
                if row['kind']=='switch':
                    row['owner'].keyframe_insert(data_path=row['local'],frame=fence)
                else:
                    row['owner'].keyframe_insert(data_path=row['local'],index=row['index'],frame=fence)
            curve=ikfk_curve(candidate,row['path'],row['index'])
            if curve is None:
                raise ValueError('WINDOW_INTERNAL: could not create fenced curve '+row['path'])
        reports.append(fw_prepare(curve,window[0],window[1]))
    return reports

def m3_pose_error(obj,chain,desired):
    evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    error=0.0
    for pose in chain[:2]:
        matrix=Matrix(desired[pose.name]['matrix'])
        actual=evaluated.pose.bones[pose.name].matrix
        error+=m2_angle(actual.to_quaternion().normalized(),matrix.to_quaternion().normalized())**2
    return error

def m3_solve_pole(obj,chain,pole,desired):
    points=[Vector(desired[chain[0].name]['head']),Vector(desired[chain[0].name]['tail']),Vector(desired[chain[1].name]['tail'])]
    axis=(points[2]-points[0]).normalized();projection=points[0]+axis*(points[1]-points[0]).dot(axis)
    bend=points[1]-projection
    if bend.length<1e-5:
        raise ValueError('Near-straight limb makes pole matching ambiguous')
    bend.normalize();distance=max((points[1]-points[0]).length+(points[2]-points[1]).length,.01)
    old=pole.matrix.copy();best_angle=0.0;best_error=1e30
    # Bounded solver evaluation derives pole orientation for actual rest axes/pole_angle.
    for angle in [i*math.pi/8 for i in range(16)]:
        direction=Quaternion(axis,angle)@bend
        matrix=old.copy();matrix.translation=obj.matrix_world.inverted()@(projection+direction*distance)
        pole.matrix=matrix;bpy.context.view_layer.update()
        error=m3_pose_error(obj,chain,desired)
        if error<best_error:
            best_angle=angle;best_error=error
    radius=math.pi/8
    for iteration in range(12):
        for angle in [best_angle-radius/2,best_angle,best_angle+radius/2]:
            direction=Quaternion(axis,angle)@bend
            matrix=old.copy();matrix.translation=obj.matrix_world.inverted()@(projection+direction*distance)
            pole.matrix=matrix;bpy.context.view_layer.update()
            error=m3_pose_error(obj,chain,desired)
            if error<best_error:
                best_angle=angle;best_error=error
        radius/=2
    matrix=old.copy();matrix.translation=obj.matrix_world.inverted()@(projection+(Quaternion(axis,best_angle)@bend)*distance)
    pole.matrix=matrix;bpy.context.view_layer.update()

def m3_apply(obj,source,candidate,proposal,options):
    chain,target,pole,solver=m3_adapter(obj,options)
    before=m3_key_index(candidate)
    source_slot=source.slots[0].handle if source.slots else None
    frames=proposal['frames'];window=(frames[0],frames[-1])
    written=ikfk_written(obj,options,chain,target,pole,solver)
    fence_reports=ikfk_fence_written(obj,candidate,written,window)
    desired={row['frame']:row['bones'] for row in proposal['samples']};source_baseline=m3_capture(obj)
    for frame in proposal['frames']:
        m3_assign(obj,source,source_slot);m3_restore(obj,source_baseline);m3_set_frame(frame)
        # Reset solver/custom state by source evaluation, then solve only the candidate.
        source_state=m3_capture(obj)
        m3_assign(obj,candidate);m3_restore(obj,source_state)
        poses=desired[frame]
        if options['direction']=='fk_to_ik':
            m3_switch_mode(obj,solver,options,True,frame)
            target.matrix=Matrix(poses[chain[2].name]['matrix'])
            target.matrix.translation=obj.matrix_world.inverted()@Vector(poses[chain[1].name]['tail'])
            bpy.context.view_layer.update()
            m3_solve_pole(obj,chain,pole,poses)
            m3_key_pose(target,frame,['location',m3_rotation_channel(target),'scale'])
            m3_key_pose(pole,frame,['location'])
        else:
            m3_switch_mode(obj,solver,options,False,frame)
            for pose in chain:
                pose.matrix=Matrix(poses[pose.name]['matrix'])
                bpy.context.view_layer.update()
                m3_key_pose(pose,frame,['location',m3_rotation_channel(pose),'scale'])
    # Switch state is discrete inside the window; transforms interpolate linearly between samples.
    # Only the declared written curves are touched (other limbs' curves stay byte-identical).
    for row in written:
        fw_finish(ikfk_curve(candidate,row['path'],row['index']),window[0],window[1],'CONSTANT' if row['kind']=='switch' else 'LINEAR')
    m3_assign(obj,candidate)
    after=m3_key_index(candidate)
    return {'keys_created':len(set(after)-set(before)),
        'keys_modified':sum(before[key]!=after[key] for key in set(before)&set(after)),
        'keys_removed':len(set(before)-set(after)),'frames_processed':len(proposal['frames']),
        'written_channels':len(written),'fences':fw_fences(window[0],window[1]),
        'fence_keys_inserted':sum(r['inserted_fences'] for r in fence_reports),
        'boundary_interpolation_changes':sum(r['boundary_interpolation_changes'] for r in fence_reports)}

def ikfk_static(obj,row):
    # Proposal-time value of a channel the source never animated.
    owner,record,rest=m3_owner_channels(obj,params['source_state'],row['path'])
    if record is None:
        return None
    if rest.startswith('['):
        return record['custom'].get(json.loads(rest[1:-1]))
    if rest.startswith('.constraints['):
        return record['constraints'][json.loads(rest[len('.constraints['):-len('].influence')])]['influence']
    return record[rest[1:]][row['index']]

def ikfk_outside_frames(source,candidate,window,limit=300):
    lo,hi=fw_fences(window[0],window[1]);scene=bpy.context.scene
    keys=[p.co[0] for action in (source,candidate) for s,l,t,c in action_curves(action,None) for p in c.keyframe_points]
    start=int(math.floor(min(keys+[scene.frame_start])));end=int(math.ceil(max(keys+[scene.frame_end])))
    frames=[float(f) for f in range(start,end+1) if f<=lo or f>=hi]
    if len(frames)>limit:
        near=[f for f in frames if lo-10<=f<=lo or hi<=f<=hi+10]
        step=max(1,int(math.ceil(len(frames)/float(limit-len(near)))))
        frames=sorted(set(near+frames[::step]+[frames[0],frames[-1]]))
    return frames,start,end

def ikfk_world_rows(obj,action,frames,reset_state):
    m3_assign(obj,action,action.slots[0].handle if action.slots else None)
    if reset_state is not None:
        m3_restore(obj,reset_state)
    rows={}
    for frame in frames:
        m3_set_frame(frame);evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        rows[frame]={p.name:(evaluated.matrix_world@p.head,evaluated.matrix_world@p.tail,
                             (evaluated.matrix_world@p.matrix).to_quaternion().normalized()) for p in evaluated.pose.bones}
    return rows

def ikfk_angle(first,second):
    # atan2 form: acos(|dot|) turns 1e-8 of float noise into ~0.016 degrees near zero.
    difference=first.rotation_difference(second).normalized()
    return math.degrees(2*math.atan2(Vector((difference.x,difference.y,difference.z)).length,abs(difference.w)))

def ikfk_row_delta(a,b,names,scale):
    position=0.0;angle=0.0;worst=None
    for name in names:
        head=(a[name][0]-b[name][0]).length*scale;tail=(a[name][1]-b[name][1]).length*scale
        rotation=ikfk_angle(a[name][2],b[name][2])
        if max(head,tail)>position or (worst is None and rotation>angle):
            worst=name
        position=max(position,head,tail);angle=max(angle,rotation)
    return position,angle,worst

def ikfk_masked_end_error(obj,chain,proposal):
    # IK->FK: a full COPY_ROTATION from the IK target hides the baked FK rotation of the end bone.
    # Mute it temporarily (restored with the captured state) and check the FK channel itself.
    masking=[c for c in chain[2].constraints if not c.mute and c.influence>0]
    if not masking:
        return None
    for constraint in masking:
        constraint.mute=True
    worst=0.0
    for row in proposal['samples']:
        if row['frame'] not in proposal['frames']:
            continue
        m3_set_frame(row['frame']);evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        worst=max(worst,ikfk_angle(evaluated.pose.bones[chain[2].name].matrix.to_quaternion().normalized(),
                                 Matrix(row['bones'][chain[2].name]['matrix']).to_quaternion().normalized()))
    return worst

def m3_verify(obj,source,candidate,proposal,options):
    chain,target,pole,solver=m3_adapter(obj,options)
    state=m3_capture(obj);active=obj.animation_data.action;slot=m2_slot(obj)
    scale=bpy.context.scene.unit_settings.scale_length
    frames=proposal['frames'];window=(frames[0],frames[-1]);lo,hi=fw_fences(window[0],window[1])
    written=ikfk_written(obj,options,chain,target,pole,solver)
    outside_tol=float(options.get('outside_window_tolerance_m',1e-5));outside_angle=float(options.get('outside_window_tolerance_degrees',.01))
    curve_tol=float(options.get('outside_window_curve_tolerance',1e-5))
    positions=[];angles=[];stretch=[];errors=[]
    try:
        m3_assign(obj,candidate)
        for row in proposal['samples']:
            m3_set_frame(row['frame']);evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for name,expected in row['bones'].items():
                pose=evaluated.pose.bones[name];matrix=pose.matrix
                head=evaluated.matrix_world@pose.head;tail=evaluated.matrix_world@pose.tail
                positions.extend([(head-Vector(expected['head'])).length*scale,(tail-Vector(expected['tail'])).length*scale])
                angles.append(m2_angle(matrix.to_quaternion().normalized(),Matrix(expected['matrix']).to_quaternion().normalized()))
                stretch.append(abs((tail-head).length-(Vector(expected['tail'])-Vector(expected['head'])).length)*scale)
        # Switch state inside the window must be the requested mode.
        switch_curve=ikfk_curve(candidate,written[0]['path'],0)
        switch_error=max((abs(switch_curve.evaluate(f)-written[0]['wanted']) for f in frames),default=0.0)
        masked=ikfk_masked_end_error(obj,chain,proposal) if options['direction']=='ik_to_fk' else None
        m3_restore(obj,state)
        # Outside the window: untouched curves identical, written curves equal to the source.
        written_keys={(row['path'],row['index']) for row in written}
        src={(c.data_path,c.array_index):c for s,l,t,c in action_curves(source,None)}
        cand={(c.data_path,c.array_index):c for s,l,t,c in action_curves(candidate,None)}
        untouched=[key[0]+'['+str(key[1])+']' for key in sorted(set(src)|set(cand)) if key not in written_keys
                   and (key not in src or key not in cand or fw_curve_rows(src[key])!=fw_curve_rows(cand[key]))]
        outside_frames,start,end=ikfk_outside_frames(source,candidate,window)
        samples=[start+i*.25 for i in range(int((lo-start)/.25)+1)]+[hi+i*.25 for i in range(int((end-hi)/.25)+1)]
        curve_delta=0.0;curve_worst=None
        for row in written:
            key=(row['path'],row['index']);static=None if key in src else ikfk_static(obj,row)
            for frame in samples:
                reference=src[key].evaluate(frame) if key in src else static
                if reference is None:
                    continue
                delta=abs(cand[key].evaluate(frame)-reference)
                if delta>curve_delta:
                    curve_delta=delta;curve_worst=row['path']+'['+str(row['index'])+']@'+str(frame)
        # Evaluated world space outside the window (all bones) and protected bones inside it.
        window_frames=sorted({row['frame'] for row in proposal['samples']})
        affected={p.name for p in chain}|{target.name,pole.name}
        for name in list(affected):
            affected|={child.name for child in obj.pose.bones[name].children_recursive}
        protected=[p.name for p in obj.pose.bones if p.name not in affected]
        source_rows=ikfk_world_rows(obj,source,outside_frames+window_frames,params['source_state'])
        candidate_rows=ikfk_world_rows(obj,candidate,outside_frames+window_frames,None)
        outside_position=0.0;outside_rotation=0.0;outside_worst=None
        for frame in outside_frames:
            position,angle,worst=ikfk_row_delta(source_rows[frame],candidate_rows[frame],list(source_rows[frame]),scale)
            if position>outside_position:
                outside_position=position;outside_worst=str(worst)+'@'+str(frame)
            outside_rotation=max(outside_rotation,angle)
        protected_position=0.0;protected_rotation=0.0
        for frame in window_frames:
            position,angle,worst=ikfk_row_delta(source_rows[frame],candidate_rows[frame],protected,scale)
            protected_position=max(protected_position,position);protected_rotation=max(protected_rotation,angle)
        metrics={'max_position_error':max(positions,default=0),'mean_position_error':sum(positions)/max(1,len(positions)),
            'max_rotation_error':max(angles,default=0),'mean_rotation_error':sum(angles)/max(1,len(angles)),
            'position_error':max(positions,default=0),'rotation_error':max(angles,default=0),
            'max_length_change_m':max(stretch,default=0),'frames_verified':len(proposal['samples']),
            'window':[window[0],window[1]],'fences':[lo,hi],'switch_value_error':switch_error,
            'outside_window_frames_verified':len(outside_frames),'outside_window_curve_samples':len(samples)*len(written),
            'outside_window_max_curve_delta':curve_delta,'outside_window_worst_curve':curve_worst,
            'outside_window_max_position_delta_m':outside_position,'outside_window_max_rotation_delta_degrees':outside_rotation,
            'outside_window_worst':outside_worst,'untouched_curves_changed':untouched[:20],
            'protected_bones_in_window':len(protected),'protected_max_position_delta_m':protected_position,
            'protected_max_rotation_delta_degrees':protected_rotation,'masked_end_bone_error_degrees':masked}
        if metrics['max_position_error']>options.get('position_tolerance_m',.001) or metrics['max_rotation_error']>options.get('rotation_tolerance_degrees',.5):
            errors.append('World-space limb pose exceeds matching tolerance')
        if switch_error>1e-6:
            errors.append('WINDOW_SWITCH_MISMATCH: switch/influence inside the window is not the requested mode')
        if untouched:
            errors.append('UNTOUCHED_CURVES_CHANGED: '+', '.join(untouched[:5]))
        if curve_delta>curve_tol:
            errors.append('OUTSIDE_WINDOW_CHANGED: written curve differs outside the window ('+str(curve_worst)+')')
        if outside_position>outside_tol or outside_rotation>outside_angle:
            errors.append('OUTSIDE_WINDOW_CHANGED: evaluated pose differs outside the window ('+str(outside_worst)+')')
        if protected_position>outside_tol or protected_rotation>outside_angle:
            errors.append('PROTECTED_BONES_CHANGED: bones outside the limb changed inside the window')
        if masked is not None and masked>options.get('rotation_tolerance_degrees',.5):
            errors.append('MASKED_FK_CHANNEL_MISMATCH: baked end-bone FK rotation differs from the IK pose')
        return {'passed':not errors,'metrics':metrics,'errors':errors,
            'warnings':['Numerical verification does not certify artistic quality.',
                        'Transition intervals ('+str(lo)+', '+str(window[0])+') and ('+str(window[1])+', '+str(hi)+') interpolate between source and window keys.']}
    finally:
        m3_assign(obj,active,slot);m3_restore(obj,state)
'''


def operate(bridge,storage,operation_id,direction,bake=False,object_name=None,mode='analyze',options=None,operation=None):
    options={} if options is None else dict(options)
    if mode in {'analyze','propose'}:
        options.update(direction=direction,bake=bake)
    return transaction(bridge,storage,operation_id,'rig_'+('bake_' if bake else 'match_')+direction,
                       IKFK_RUNTIME,object_name,mode,options,operation)
