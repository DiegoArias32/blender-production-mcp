"""Conservative location corrections, retiming and in-place root preparation."""

TRANSFORM_RUNTIME = r'''
def m2_frames(action, options):
    start = options.get('frame_start', math.floor(action.frame_range[0]))
    end = options.get('frame_end', math.ceil(action.frame_range[1]))
    if type(start) is not int or type(end) is not int or end <= start or end-start > 1000:
        raise ValueError('Correction range requires 2..1001 integer frames')
    if start < -1048574 or end > 1048574:
        raise ValueError('Frame bounds exceed Blender limits')
    return list(range(start, end+1))

def m2_transform_capacity(obj, frames):
    # m2_transform_verify samples every bone at half-frame steps, twice (source and candidate), and
    # refuses more than 128 bones or 100000 bone/frames per pass. Checked here so an oversized case
    # is unsupported at propose time instead of failing after the checkpoint and candidate (AUD-008).
    bones = len(obj.pose.bones) if obj.type == 'ARMATURE' else 1
    dense = 2 * (frames[-1] - frames[0]) + 1
    capacity = {'verification_frames': dense, 'bones': bones, 'bone_frame_samples_per_pass': dense * bones,
                'limit_per_pass': 100000, 'max_bones': 128}
    capacity['supported'] = bones <= 128 and dense * bones <= 100000
    return capacity

def m2_transform_proposal(obj, action, kind, options):
    proposal = m2_transform_plan(obj, action, kind, options)
    capacity = m2_transform_capacity(obj, proposal['frames'])
    proposal['capacity'] = capacity
    if proposal['supported'] and not capacity['supported']:
        proposal['supported'] = False
        proposal['warnings'].append('CAPACITY_EXCEEDED: verification needs ' + str(capacity['bone_frame_samples_per_pass'])
            + ' bone/frame samples per pass for ' + str(capacity['bones']) + ' bones (limits 100000 and 128 bones); narrow the frame range')
    return proposal

def m2_transform_plan(obj, action, kind, options):
    frames = m2_frames(action, options)
    warnings = []
    proposal = {'supported': True, 'kind': kind, 'frames': frames,
                'patches': [], 'metrics': {}, 'warnings': warnings}
    if kind == 'retime':
        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        factor = options.get('scale_factor')
        duration = options.get('target_duration_seconds')
        if (factor is None) == (duration is None):
            raise ValueError('Supply either scale_factor or target_duration_seconds')
        factor = factor if factor is not None else duration*fps/(frames[-1]-frames[0])
        if isinstance(factor, bool) or not math.isfinite(factor) or not 0.05 <= factor <= 20:
            raise ValueError('Retiming factor must be finite within 0.05..20')
        proposal['factor'] = factor
        proposal['metrics'] = {'factor': factor, 'duration_before_seconds': (frames[-1]-frames[0])/fps,
                               'duration_after_seconds': (frames[-1]-frames[0])*factor/fps}
        for row in action_curves(action, None):
            if any(point.interpolation not in {'LINEAR','BEZIER','CONSTANT'} for point in row[3].keyframe_points):
                raise ValueError('Retiming supports LINEAR, BEZIER and CONSTANT interpolation only')
            times = [point.co.x for point in row[3].keyframe_points]
            if not all(any(abs(time-bound) < 1e-5 for time in times) for bound in (frames[0],frames[-1])):
                raise ValueError('Each channel needs keyed selected-range boundaries to preserve endpoint poses')
        for marker in action.pose_markers:
            new_frame = m2_warp(marker.frame, frames[0],frames[-1],factor)
            if abs(new_frame-round(new_frame)) > 1e-5:
                raise ValueError('Action marker would require fractional frame; choose compatible duration')
        warnings.append('Action markers move with keys; scene markers, FPS and scene range remain unchanged. Contacts must be reviewed after timing changes.')
        return proposal
    if kind == 'root_motion':
        bone_name = options.get('root_bone')
        if options.get('strategy', 'in_place') != 'in_place':
            raise ValueError('M2 supports in_place horizontal trajectory removal only; no rig restructuring')
        if not bone_name:
            proposal.update(supported=False)
            warnings.append('Explicit validated root_bone required; candidates are analysis-only')
            return proposal
        bone = obj.pose.bones.get(bone_name) if obj.type == 'ARMATURE' else None
        if bone is None or bone.parent or bone.constraints:
            raise ValueError('Root preparation requires explicit unparented unconstrained pose root')
        if any(bone.lock_location):
            raise ValueError('Root location channels are locked')
        m2_linear_location(action,bone)
        proposal['controls'] = [bone_name]
        samples = m2_sample(obj, frames, [bone_name])
        origin = samples[bone_name][0]['position']
        proposal['offsets'] = {bone_name: [[origin[0]-row['position'][0],origin[1]-row['position'][1],0]
                                         for row in samples[bone_name]]}
        proposal['metrics'] = {'horizontal_displacement_m': math.dist(origin[:2],samples[bone_name][-1]['position'][:2])}
        warnings.append('Removes world-horizontal motion on a duplicate root channel; vertical motion and rig hierarchy preserved. World-space IK controls may need separate contact refinement.')
        return proposal
    controls = options.get('controls')
    if not controls or not options.get('confirm_contacts', False):
        proposal['supported'] = False
        warnings.append('Explicit IK controls and confirm_contacts=true required after contact review; inferred contacts alone never authorize foot locking')
        return proposal
    if obj.type != 'ARMATURE' or len(controls) > 8 or len(set(controls)) != len(controls):
        raise ValueError('Choose 1..8 distinct armature IK controls')
    blend = options.get('blend_frames', 3)
    if type(blend) is not int or not 1 <= blend <= 30:
        raise ValueError('blend_frames must be 1..30')
    chains = {}
    for name in controls:
        bone = obj.pose.bones.get(name)
        if bone is None or bone.bone.use_deform or bone.constraints or any(bone.lock_location):
            raise ValueError('Automatic foot correction requires unlocked unconstrained non-deform IK control: '+name)
        m2_linear_location(action,bone)
        matches = [(pb,c) for pb in obj.pose.bones for c in pb.constraints
                   if c.type == 'IK' and not c.mute and c.target == obj and c.subtarget == name]
        if len(matches)!=1 or abs(matches[0][1].influence-1)>1e-6 or matches[0][1].chain_count!=2:
            raise ValueError('A single full-influence two-bone IK constraint must explicitly target '+name)
        tip, constraint = matches[0]
        if tip.parent is None or tip.parent.parent is None or bone == tip or bone == tip.parent:
            raise ValueError('Cannot establish independent IK controller and hip/root relationship')
        chains[name] = [tip.name, tip.parent.name, tip.parent.parent.name]
    for name in controls:
        if any(parent.name in controls for parent in obj.pose.bones[name].parent_recursive):
            raise ValueError('Selected IK controls cannot be ancestors of one another')
    contact_frames = list(frames)
    frames = list(range(max(math.floor(action.frame_range[0]),frames[0]-blend),
                        min(math.ceil(action.frame_range[1]),frames[-1]+blend)+1))
    proposal['frames'] = frames
    samples = m2_sample(obj, frames, controls)
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
    offsets, contacts, detected = {}, {}, 0
    for name in controls:
        positions = [row['position'] for row in samples[name]]
        contact_positions = [positions[frame-frames[0]] for frame in contact_frames]
        report = contact_report(contact_frames, contact_positions, fps, options.get('ground_height_m',0.0),
            options.get('contact_height_m',0.03),options.get('contact_speed_m_s',0.25),
            options.get('sliding_tolerance_m',0.01),options.get('min_contact_frames',3))
        chosen = [row for row in report['contacts'] if row['status']=='fail']
        detected += report['contact_count']
        offsets[name] = [[0.0,0.0,0.0] for frame in frames]
        contacts[name] = chosen
        covered = set()
        for contact in chosen:
            first,last = contact['frame_start']-frames[0],contact['frame_end']-frames[0]
            anchor = positions[first]
            # Anchor at entry preserves approach. Exit correction decays smoothly.
            for index in range(first,min(len(frames),last+blend+1)):
                if index in covered:
                    raise ValueError('Overlapping contact blends require explicit interval review')
                covered.add(index)
                if index <= last:
                    offsets[name][index] = [anchor[axis]-positions[index][axis] for axis in (0,1)] + [0.0]
                else:
                    t = (index-last)/blend
                    weight = 1-(t*t*(3-2*t))
                    offsets[name][index] = [(anchor[axis]-positions[last][axis])*weight for axis in (0,1)] + [0.0]
    changed_controls = [name for name in controls if contacts[name]]
    if not detected:
        # No contact evidence is not zero sliding: nothing verifiable to lock (AUD-005).
        proposal['supported'] = False
        warnings.append('NO_CONFIRMED_CONTACT: no contact interval met the reviewed thresholds for the selected controls')
    elif not changed_controls:
        proposal['supported'] = False
        warnings.append('NOTHING_TO_CORRECT: every detected contact is within sliding_tolerance_m')
    proposal.update(controls=changed_controls,chains={name:chains[name] for name in changed_controls},
                    offsets={name:offsets[name] for name in changed_controls},contacts=contacts)
    proposal['metrics'] = {'before_sliding_cm':max([row['translation_during_contact_cm'] for value in contacts.values() for row in value] or [0.0]),
        'contact_intervals':sum(len(value) for value in contacts.values())}
    warnings.append('Locks reviewed IK head markers in XY only; preserves rotation/toe channels. Rolling soles, moving ground, FK and ambiguous constraints require manual alternatives. Visual knee/toe review remains required.')
    return proposal

def m2_linear_location(action,bone):
    path=bone.path_from_id('location')
    for row in action_curves(action,None):
        curve=row[3]
        if curve.data_path==path and any(point.interpolation!='LINEAR' for point in curve.keyframe_points):
            raise ValueError('Automatic location correction currently requires LINEAR channels; preserve BEZIER/CONSTANT motion for manual alternatives')

def m2_warp(frame, start, end, factor):
    if frame < start:
        return frame
    if frame > end:
        return frame + (end-start)*(factor-1)
    return start+(frame-start)*factor

def m2_transform_apply(obj, action, kind, proposal, options):
    frames = proposal['frames']
    modified, created = 0,0
    channels = []
    if kind == 'retime':
        for slot,layer,strip,curve in action_curves(action,None):
            for point in curve.keyframe_points:
                for coordinate in (point.co,point.handle_left,point.handle_right):
                    coordinate.x = m2_warp(coordinate.x,frames[0],frames[-1],proposal['factor'])
                modified += 1
            curve.update()
            channels.append(curve.data_path+'['+str(curve.array_index)+']')
        for marker in action.pose_markers:
            marker.frame = round(m2_warp(marker.frame,frames[0],frames[-1],proposal['factor']))
        if action.use_frame_range:
            action.frame_start = m2_warp(action.frame_start,frames[0],frames[-1],proposal['factor'])
            action.frame_end = m2_warp(action.frame_end,frames[0],frames[-1],proposal['factor'])
    else:
        scene = bpy.context.scene
        old_frame,old_subframe = scene.frame_current,scene.frame_subframe
        edits = []
        try:
            for name in proposal['controls']:
                pb = obj.pose.bones[name]
                for frame, offset in zip(frames,proposal['offsets'][name]):
                    scene.frame_set(frame)
                    desired = pb.matrix.copy()
                    world_delta = Vector(offset)/scene.unit_settings.scale_length
                    desired.translation += obj.matrix_world.inverted().to_3x3() @ world_delta
                    local = obj.convert_space(pose_bone=pb, matrix=desired, from_space='POSE', to_space='LOCAL')
                    edits.append((name,frame,list(local.translation)))
            # Calculate all local values before inserting: parent transforms remain original.
            for name,frame,value in edits:
                pb = obj.pose.bones[name]
                path = pb.path_from_id('location')
                existing = {(curve.array_index,float(point.co.x)) for row in action_curves(action,m2_slot(obj))
                            for curve in [row[3]] if curve.data_path == path for point in curve.keyframe_points}
                pb.location = value
                pb.keyframe_insert(data_path='location',frame=frame,group=pb.name)
                for axis in range(3):
                    modified += int((axis,float(frame)) in existing)
                    created += int((axis,float(frame)) not in existing)
                if path not in channels:
                    channels.append(path)
            for row in action_curves(action,m2_slot(obj)):
                curve = row[3]
                if curve.data_path in channels:
                    for point in curve.keyframe_points:
                        if frames[0] <= point.co.x <= frames[-1]:
                            point.interpolation='LINEAR'
                    curve.update()
        finally:
            scene.frame_set(old_frame,subframe=old_subframe)
    return {'created_keys':created,'modified_keys':modified,'affected_channels':channels[:100],
            'affected_frames':[frames[0],frames[-1]]}

def m2_transform_verify(obj,before_action,after_action,kind,proposal,options):
    frames = proposal['frames']
    slot = m2_slot(obj)
    source_slot = before_action.slots[0].handle if getattr(before_action,'is_action_layered',False) else None
    names = list(obj.pose.bones.keys()) if obj.type=='ARMATURE' else ['']
    if len(names)>128:
        raise ValueError('Motion verification supports up to 128 bones')
    dense = [frames[0]+index*0.5 for index in range(2*(frames[-1]-frames[0])+1)]
    try:
        m2_assign(obj,before_action,source_slot)
        if params.get('source_state'):
            m2_restore_state(obj,params['source_state'])
        before = m2_sample(obj,dense,names)
        m2_assign(obj,after_action,slot)
        mapped = [m2_warp(frame,frames[0],frames[-1],proposal['factor']) for frame in dense] if kind=='retime' else dense
        after = m2_sample(obj,mapped,names)
        errors, warnings = [],[]
        metrics = {}
        if kind=='retime':
            error = max(math.dist(a['position'],b['position']) for name in names for a,b in zip(before[name],after[name]))
            angle = max(m2_angle(a['rotation'],b['rotation']) for name in names for a,b in zip(before[name],after[name]))
            metrics.update(max_pose_error_m=error,max_pose_error_degrees=angle)
            if error>options.get('position_tolerance_m',0.0001) or angle>options.get('angular_tolerance_degrees',0.1):
                errors.append('Retimed evaluated poses exceed configured tolerance')
            effectors=sorted({constraint.subtarget for pb in obj.pose.bones for constraint in pb.constraints
                if constraint.type=='IK' and constraint.target==obj and constraint.subtarget in before}) if obj.type=='ARMATURE' else []
            contact_results=[]
            for name in effectors:
                arguments=(bpy.context.scene.render.fps/bpy.context.scene.render.fps_base,
                    options.get('ground_height_m',0.0),options.get('contact_height_m',0.03),
                    options.get('contact_speed_m_s',0.25),options.get('sliding_tolerance_m',0.01),3)
                first=contact_report(dense,[row['position'] for row in before[name]],*arguments)
                second=contact_report(mapped,[row['position'] for row in after[name]],*arguments)
                contact_results.append({'effector':name,'before_count':first['contact_count'],
                    'after_count':second['contact_count'],'before_intervals':first['contacts'],
                    'after_intervals':second['contacts']})
                if first['contact_count']!=second['contact_count']:
                    warnings.append('Contact detection changed after retiming for '+name+'; review changed contact speeds')
            metrics['contact_reanalysis']=contact_results[:16]
            warnings.append('Contact timestamps scaled with duration; contact speeds must be re-analyzed, no FPS change was made')
        elif kind=='root_motion':
            name=proposal['controls'][0]
            horizontal=max(math.dist(after[name][0]['position'][:2],row['position'][:2]) for row in after[name])
            vertical=max(abs(a['position'][2]-b['position'][2]) for a,b in zip(before[name],after[name]))
            metrics.update(horizontal_motion_after_m=horizontal,max_vertical_error_m=vertical)
            if horizontal>options.get('position_tolerance_m',0.0001) or vertical>options.get('position_tolerance_m',0.0001):
                errors.append('In-place root trajectory exceeds tolerance')
        else:
            peak_before,peak_after = 0.0,0.0
            for name,contacts in proposal['contacts'].items():
                for interval in contacts:
                    indices=[index for index,frame in enumerate(dense) if interval['frame_start']<=frame<=interval['frame_end']]
                    anchor_a=before[name][indices[0]]['position']; anchor_b=after[name][indices[0]]['position']
                    peak_before=max(peak_before,max(math.dist(anchor_a[:2],before[name][i]['position'][:2]) for i in indices))
                    peak_after=max(peak_after,max(math.dist(anchor_b[:2],after[name][i]['position'][:2]) for i in indices))
            metrics.update(before_sliding_cm=100*peak_before,after_sliding_cm=100*peak_after,
                improvement_cm=100*(peak_before-peak_after),improvement_percent=100*(peak_before-peak_after)/peak_before if peak_before else 0)
            protected_error,rotation_error=0.0,0.0
            for name in proposal['controls']:
                for index,frame in enumerate(dense):
                    relative=frame-frames[0]
                    left=min(len(frames)-1,math.floor(relative)); right=min(len(frames)-1,left+1)
                    fraction=relative-left
                    expected_offset=[(1-fraction)*proposal['offsets'][name][left][axis]+fraction*proposal['offsets'][name][right][axis] for axis in range(3)]
                    expected=[before[name][index]['position'][axis]+expected_offset[axis] for axis in range(3)]
                    protected_error=max(protected_error,math.dist(expected,after[name][index]['position']))
                    rotation_error=max(rotation_error,m2_angle(before[name][index]['rotation'],after[name][index]['rotation']))
            metrics.update(max_protected_trajectory_error_m=protected_error,max_control_orientation_error_degrees=rotation_error)
            if protected_error>options.get('position_tolerance_m',0.0001) or rotation_error>0.05:
                errors.append('Protected vertical/transition/rotation trajectory changed beyond tolerance')
            if peak_after>options.get('sliding_tolerance_m',0.01) or (peak_before>1e-6 and peak_after>peak_before*0.2):
                errors.append('Contact drift did not meet tolerance and 80 percent reduction requirement')
            max_jump_increase,max_hip_error,max_stretch,max_target_error=0.0,0.0,0.0,0.0
            for control,chain in proposal['chains'].items():
                for name in chain[:2]:
                    jump_increases=[m2_angle(c['rotation'],d['rotation'])-m2_angle(a['rotation'],b['rotation'])
                        for a,b,c,d in zip(before[name],before[name][1:],after[name],after[name][1:])]
                    max_jump_increase=max(max_jump_increase,max(jump_increases,default=0.0))
                    max_stretch=max(max_stretch,max(abs(a['length']-b['length']) for a,b in zip(before[name],after[name])))
                hip=chain[2]
                max_hip_error=max(max_hip_error,max(math.dist(a['position'],b['position']) for a,b in zip(before[hip],after[hip])))
                max_target_error=max(max_target_error,max(math.dist(a['tail'],b['position']) for a,b in zip(after[chain[0]],after[control])))
            metrics.update(max_knee_ankle_step_increase_degrees=max_jump_increase,max_hip_root_error_m=max_hip_error,
                max_bone_length_change_m=max_stretch,max_ik_target_error_m=max_target_error)
            if max_jump_increase>options.get('max_joint_step_increase_degrees',10):
                errors.append('New knee/ankle angular step exceeds tolerance')
            if max_hip_error>options.get('position_tolerance_m',0.0001) or max_stretch>options.get('stretch_tolerance_m',0.002):
                errors.append('Hip/root trajectory or bone stretch changed beyond tolerance')
            if max_target_error>options.get('ik_target_tolerance_m',0.01):
                errors.append('IK chain cannot reach corrected control within tolerance')
            if list(before_action.frame_range)!=list(after_action.frame_range):
                errors.append('Correction changed Action duration')
            warnings.append('Joint step and stretch checks are numerical safeguards, not a guarantee against visual knee pops or toe artifacts')
        return {'passed':not errors,'status':'pass' if not errors else 'fail','metrics':metrics,'errors':errors,'warnings':warnings}
    finally:
        m2_assign(obj,after_action,slot)
'''
