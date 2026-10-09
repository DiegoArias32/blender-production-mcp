"""Trusted Blender-side mocap adapter, executed through the upstream safe-mode bridge.

Imports and names supplied by M3 BASE_RUNTIME: bpy, math, action_curves,
m2_snapshot, m2_sample, m2_capture_state, m2_restore_state, m3_context_base.
"""
from mathutils import Matrix, Vector, Quaternion


def mocap_rotation_error(first, second):
    difference = first.rotation_difference(second).normalized()
    return math.degrees(2 * math.atan2(Vector((difference.x, difference.y, difference.z)).length, abs(difference.w)))


def mocap_options(options):
    allowed = {'source_armature', 'mapping', 'mapping_approved', 'minimum_confidence',
               'frame_start', 'frame_end', 'sample_step', 'scale_mode', 'scale_factor',
               'root_motion', 'position_tolerance_m', 'rotation_tolerance_degrees',
               'ground_height_m', 'contact_height_m', 'contact_velocity_m_s',
               'sliding_tolerance_m', 'pop_threshold_degrees', 'detail', 'limit', 'offset',
               'rest_compensation', 'rest_compensation_max_degrees', 'rest_direction_tolerance_degrees'}
    if set(options) - allowed:
        raise ValueError('Unknown mocap options: ' + str(sorted(set(options) - allowed)))
    out = dict(options)
    out.setdefault('sample_step', 1)
    out.setdefault('scale_mode', 'skeleton')
    out.setdefault('root_motion', 'preserve')
    out.setdefault('position_tolerance_m', 0.0001)
    out.setdefault('rotation_tolerance_degrees', 0.05)
    out.setdefault('ground_height_m', 0.0)
    out.setdefault('contact_height_m', 0.03)
    out.setdefault('contact_velocity_m_s', 0.3)
    out.setdefault('sliding_tolerance_m', 0.01)
    out.setdefault('pop_threshold_degrees', 25.0)
    out.setdefault('rest_compensation', 'auto')
    out.setdefault('rest_compensation_max_degrees', 60.0)
    out.setdefault('rest_direction_tolerance_degrees', 2.0)
    if out['rest_compensation'] not in ('auto', 'none'):
        raise ValueError('rest_compensation must be auto or none')
    for key, upper in (('rest_compensation_max_degrees', 90.0), ('rest_direction_tolerance_degrees', 10.0)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= upper:
            raise ValueError(key + ' must be within (0, ' + str(upper) + ']')
    if out['scale_mode'] not in ('skeleton', 'explicit') or out['root_motion'] not in ('preserve', 'in_place'):
        raise ValueError('Supported scale_mode: skeleton/explicit; root_motion: preserve/in_place')
    if type(out['sample_step']) is not int or not 1 <= out['sample_step'] <= 4:
        raise ValueError('sample_step must be integer 1..4')
    for key in ('position_tolerance_m', 'rotation_tolerance_degrees', 'contact_height_m',
                'contact_velocity_m_s', 'sliding_tolerance_m', 'pop_threshold_degrees'):
        if isinstance(out[key], bool) or not isinstance(out[key], (int, float)) or not math.isfinite(out[key]) or out[key] <= 0:
            raise ValueError(key + ' must be positive and finite')
    if not math.isfinite(out['ground_height_m']):
        raise ValueError('ground_height_m must be finite')
    if out['position_tolerance_m'] > 0.01 or out['rotation_tolerance_degrees'] > 1:
        raise ValueError('Retarget verification tolerance exceeds conservative adapter limits')
    if out['scale_mode'] == 'explicit' and (isinstance(out.get('scale_factor'), bool) or not isinstance(out.get('scale_factor'), (int, float)) or not 0.01 <= out['scale_factor'] <= 100):
        raise ValueError('Explicit scale_factor must be 0.01..100')
    return out


def mocap_rig_context(obj):
    action = obj.animation_data.action if obj.animation_data else None
    return {'rig_context': m3_context_base(obj), 'name': obj.name, 'matrix': [list(row) for row in obj.matrix_world],
            'action_name': action.name if action else None,
            'action': m2_snapshot(action) if action else None,
            'rest': [{'name': bone.name, 'parent': bone.parent.name if bone.parent else None,
                      'matrix': [list(row) for row in bone.matrix_local], 'length': bone.length,
                      'inherit_scale': bone.inherit_scale, 'inherit_rotation': bone.use_inherit_rotation,
                      'local_location': bone.use_local_location,
                      'rotation_mode': obj.pose.bones[bone.name].rotation_mode}
                     for bone in obj.data.bones]}


def mocap_rig_bones(obj):
    return {bone.name: {'parent': bone.parent.name if bone.parent else None, 'head': list(bone.head_local),
                        'tail': list(bone.tail_local), 'length': bone.length} for bone in obj.data.bones}


def mocap_y_axis(bone):
    return (bone.matrix_local.to_3x3() @ Vector((0, 1, 0))).normalized()


def mocap_rest_compatibility(source, target, rows, options):
    """AUD-002: decide per bone whether rest poses are identical, compensable or incompatible.

    Directions are armature-space bone Y axes (object orientations are already required to match).
    Root and pelvis bone axes are rig conventions, not poses: they keep delta transfer and are only
    reported. Every other bone is aligned with C = rotation(target rest Y -> source rest Y).
    """
    source_bones, target_bones = mocap_rig_bones(source), mocap_rig_bones(target)
    source_frame = map_body_frame(source_bones, {r['semantic_role']: r['source_bone'] for r in rows})
    target_frame = map_body_frame(target_bones, {r['semantic_role']: r['target_bone'] for r in rows})
    facing = math.degrees(Vector(source_frame['forward']).angle(Vector(target_frame['forward']), 0))
    up = math.degrees(Vector(source_frame['up']).angle(Vector(target_frame['up']), 0))
    blocked = []
    if facing > 10 or up > 10:
        blocked.append('REST_POSE_INCOMPATIBLE: body frames differ (facing ' + str(round(facing, 2)) + ' deg, up '
                       + str(round(up, 2)) + ' deg); armature-space transfer would rotate the character')
    bones, compensation = [], {}
    for row in rows:
        source_bone, target_bone = source.data.bones[row['source_bone']], target.data.bones[row['target_bone']]
        ds, dt = mocap_y_axis(source_bone), mocap_y_axis(target_bone)
        angle = math.degrees(ds.angle(dt, 0))
        role = row['semantic_role']
        if role in ('root', 'pelvis'):
            kind = 'convention'
        elif options['rest_compensation'] == 'none':
            kind = 'compatible' if angle <= options['rest_direction_tolerance_degrees'] else 'incompatible'
            if kind == 'incompatible':
                blocked.append('REST_POSE_INCOMPATIBLE: ' + role + ' rest directions differ ' + str(round(angle, 3))
                               + ' deg and rest_compensation=none')
        elif angle > options['rest_compensation_max_degrees']:
            kind = 'incompatible'
            blocked.append('REST_POSE_INCOMPATIBLE: ' + role + ' rest directions differ ' + str(round(angle, 3))
                           + ' deg > rest_compensation_max_degrees ' + str(options['rest_compensation_max_degrees']))
        elif angle > 1e-4:
            kind = 'compensated'
            compensation[target_bone.name] = list(dt.rotation_difference(ds))
        else:
            kind = 'identical'
        bones.append({'role': role, 'source_bone': source_bone.name, 'target_bone': target_bone.name,
                      'rest_direction_difference_degrees': angle, 'class': kind})
    compensated = [b for b in bones if b['class'] == 'compensated']
    return {'mode': options['rest_compensation'], 'blocked': blocked, 'compensation': compensation,
            'facing_difference_degrees': facing, 'up_difference_degrees': up,
            'max_direction_difference_degrees': max((b['rest_direction_difference_degrees'] for b in bones if b['class'] != 'convention'), default=0.0),
            'compensated_bones': len(compensated), 'bones': sorted(bones, key=mocap_rest_sort_key, reverse=True)[:24],
            'bone_total': len(bones)}


def mocap_rest_sort_key(row):
    return row['rest_direction_difference_degrees']


def mocap_guard(obj, require_action):
    errors = []
    if obj.type != 'ARMATURE':
        return ['Mocap requires armature objects']
    if obj.data.pose_position != 'POSE':
        errors.append('POSE_POSITION_REQUIRED: armature ' + obj.name + ' is evaluated in ' + obj.data.pose_position + ' position')
    if obj.library or obj.override_library or obj.data.library or obj.data.override_library:
        errors.append('Linked/override armatures require a local working copy')
    if obj.parent or obj.constraints:
        errors.append('Retarget adapter requires unparented unconstrained armature objects')
    scale = obj.matrix_world.to_scale()
    if min(scale) <= 0 or obj.matrix_world.determinant() <= 0 or max(scale) - min(scale) > 0.000001:
        errors.append('Nonuniform/negative object scale is unsupported')
    if obj.data.animation_data and (obj.data.animation_data.drivers or obj.data.animation_data.action):
        errors.append('Animated armature datablocks are unsupported')
    data = obj.animation_data
    action = data.action if data else None
    if require_action and action is None:
        errors.append('Mocap source requires an active Action')
    if data:
        if data.drivers or data.use_tweak_mode or any(not track.mute and len(track.strips) for track in data.nla_tracks):
            errors.append('Drivers, active NLA and tweak mode require manual preparation')
        if data.action_blend_type != 'REPLACE' or abs(data.action_influence - 1) > 1e-7:
            errors.append('Blended animation requires manual preparation')
    for bone in obj.pose.bones:
        if not require_action and (any(bone.lock_location) or any(bone.lock_rotation) or any(bone.lock_scale) or bone.lock_rotation_w):
            errors.append('Locked target channels require explicit preparation: ' + bone.name)
        if bone.constraints:
            errors.append('FK retarget adapter does not bypass bone constraints: ' + bone.name)
        if bone.rotation_mode == 'AXIS_ANGLE':
            errors.append('Axis-angle channels are not supported: ' + bone.name)
        if bone.bone.inherit_scale != 'FULL' or not bone.bone.use_inherit_rotation or not bone.bone.use_local_location:
            errors.append('Nonstandard bone inheritance unsupported: ' + bone.name)
    if action:
        if action.library or action.override_library:
            errors.append('Linked/override Actions are unsupported')
        if getattr(action, 'is_action_layered', False) and (len(action.slots) != 1 or len(action.layers) != 1 or len(action.layers[0].strips) != 1):
            errors.append('Retarget requires one Action slot/layer/strip')
        for row in action_curves(action, None):
            curve = row[3]
            if curve.modifiers or curve.sampled_points or curve.mute or curve.lock:
                errors.append('Curve modifiers/samples/mute/lock require manual preparation')
            if not curve.data_path.startswith('pose.bones[') or curve.data_path.rsplit('.', 1)[-1] not in ('location', 'rotation_quaternion', 'rotation_euler', 'scale'):
                errors.append('Only pose transform curves supported: ' + curve.data_path)
    return sorted(set(errors))


def mocap_frames(action, options):
    start = options.get('frame_start', math.ceil(action.frame_range[0]))
    end = options.get('frame_end', math.floor(action.frame_range[1]))
    if type(start) is not int or type(end) is not int or start > end or end - start > 1000:
        raise ValueError('Retarget frame range requires integer endpoints, at most 1001 frames')
    if start < action.frame_range[0] or end > action.frame_range[1]:
        raise ValueError('Requested range lies outside source Action')
    frames = list(range(start, end + 1, options['sample_step']))
    if frames[-1] != end:
        frames.append(end)
    return frames


def mocap_sample(obj, frames, names):
    scene = bpy.context.scene
    old = scene.frame_current, scene.frame_subframe
    samples = []
    state = m2_capture_state(obj)
    if len(frames) * len(names) > 60000:
        raise ValueError('Retarget budget exceeded: 60000 bone/frame samples')
    try:
        for frame in frames:
            scene.frame_set(math.floor(frame), subframe=frame - math.floor(frame))
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            samples.append({name: {'matrix': evaluated.pose.bones[name].matrix.copy(),
                                   'head': evaluated.pose.bones[name].head.copy(),
                                   'tail': evaluated.pose.bones[name].tail.copy(),
                                   'basis_location': obj.pose.bones[name].location.copy()}
                            for name in names})
    finally:
        scene.frame_set(old[0], subframe=old[1])
        m2_restore_state(obj, state)
    return samples


def mocap_expected(source, target, rows, frames, scale, root_motion, compensation=None):
    compensation = compensation or {}
    samples = mocap_sample(source, frames, [row['source_bone'] for row in rows])
    lookup = {row['target_bone']: row for row in rows}
    def depth(row):
        return len(target.data.bones[row['target_bone']].parent_recursive)
    ordered = sorted(rows, key=depth)
    result = []
    source_root = next(row['source_bone'] for row in rows if row['semantic_role'] == 'root')
    initial_root = samples[0][source_root]['head'] - source.data.bones[source_root].head_local
    for sample in samples:
        matrices = {}
        for row in ordered:
            sb, tb = source.data.bones[row['source_bone']], target.data.bones[row['target_bone']]
            source_matrix = sample[sb.name]['matrix']
            # Q_t = Q_s * R_s^-1 * C * R_t; C aligns the target rest direction with the source rest
            # direction (identity for root/pelvis and identical rests). See docs/retarget-math.md.
            correction = Quaternion(compensation[tb.name]) if tb.name in compensation else Quaternion()
            orientation = source_matrix.to_quaternion() @ sb.matrix_local.to_quaternion().inverted() @ correction @ tb.matrix_local.to_quaternion()
            if tb.parent:
                parent_matrix = matrices[tb.parent.name]
                head = parent_matrix @ (tb.parent.matrix_local.inverted() @ tb.head_local)
            else:
                head = tb.head_local.copy()
            if row['semantic_role'] == 'root':
                displacement = sample[sb.name]['head'] - sb.head_local
                if root_motion == 'in_place':
                    world_axes = source.matrix_world.to_3x3()
                    world_displacement = world_axes @ displacement
                    world_initial = world_axes @ initial_root
                    world_displacement.x = world_initial.x; world_displacement.y = world_initial.y
                    displacement = world_axes.inverted() @ world_displacement
                head += displacement * scale
            elif row['semantic_role'] == 'pelvis':
                # Include the animated parent orientation when mapping a local pelvis displacement.
                rest_axes = sb.matrix_local.to_quaternion()
                if sb.parent:
                    rest_axes = sample[sb.parent.name]['matrix'].to_quaternion() @ sb.parent.matrix_local.to_quaternion().inverted() @ rest_axes
                delta = rest_axes @ sample[sb.name]['basis_location']
                head += delta * scale
            matrices[tb.name] = Matrix.LocRotScale(head, orientation, Vector((1, 1, 1)))
        result.append(matrices)
    return result


def mocap_verification_frames(frames):
    # Keys, midpoints and every integer frame in range: a sampling step of 3-4 must not leave
    # rendered frames unverified (AUD-011).
    integers = list(range(int(math.ceil(frames[0])), int(math.floor(frames[-1])) + 1))
    return sorted(set(frames + integers + [(a + b) / 2 for a, b in zip(frames, frames[1:])]))


def mocap_quality(obj, rows, frames, options):
    names = [row['target_bone'] for row in rows]
    samples = mocap_sample(obj, frames, names)
    roles = {row['semantic_role']: row['target_bone'] for row in rows}
    unit = bpy.context.scene.unit_settings.scale_length * obj.matrix_world.to_scale()[0]
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
    contacts, pops, stretches = [], [], []
    max_velocity = 0.0; max_acceleration = 0.0; max_angle_step = 0.0
    for role in ('left_foot', 'right_foot'):
        name = roles[role]
        positions = [obj.matrix_world @ sample[name]['head'] * bpy.context.scene.unit_settings.scale_length for sample in samples]
        active = []
        for index, point in enumerate(positions):
            neighbor = min(index + 1, len(positions) - 1) if index == 0 else index - 1
            seconds = abs(frames[index] - frames[neighbor]) / fps if neighbor != index else 1 / fps
            speed = abs(point.z - positions[neighbor].z) / seconds
            if abs(point.z - options['ground_height_m']) <= options['contact_height_m'] and speed <= options['contact_velocity_m_s']:
                active.append(index)
            elif active:
                segment = [positions[i] for i in active]
                drift = max((Vector((p.x-segment[0].x, p.y-segment[0].y, 0)).length for p in segment), default=0)
                contacts.append({'effector': name, 'frames': [frames[active[0]], frames[active[-1]]], 'sliding_m': drift, 'status': 'fail' if drift > options['sliding_tolerance_m'] else 'pass'})
                active = []
        if active:
            segment = [positions[i] for i in active]
            drift = max((Vector((p.x-segment[0].x, p.y-segment[0].y, 0)).length for p in segment), default=0)
            contacts.append({'effector': name, 'frames': [frames[active[0]], frames[active[-1]]], 'sliding_m': drift, 'status': 'fail' if drift > options['sliding_tolerance_m'] else 'pass'})
    for side in ('left', 'right'):
        for upper_role, lower_role, end_role in (('upper_leg', 'lower_leg', 'foot'), ('upper_arm', 'lower_arm', 'hand')):
            upper, lower, end = [roles[side + '_' + role] for role in (upper_role, lower_role, end_role)]
            rest_length = obj.data.bones[upper].length + obj.data.bones[lower].length
            prior_angle = None
            prior_normal = None
            for index, sample in enumerate(samples):
                first = sample[lower]['head'] - sample[upper]['head']
                second = sample[end]['head'] - sample[lower]['head']
                ratio = (first.length + second.length) / max(rest_length, 1e-9)
                if ratio > 1.01:
                    stretches.append({'limb': side + '_' + upper_role, 'frame': frames[index], 'ratio': ratio})
                angle = math.degrees(first.angle(second, 0.0))
                if prior_angle is not None:
                    step = abs(angle - prior_angle)
                    max_angle_step = max(max_angle_step, step)
                    if step > options['pop_threshold_degrees']:
                        pops.append({'limb': side + '_' + lower_role, 'frame': frames[index], 'angle_step_degrees': step, 'confidence': 0.65, 'interpretation': 'Abrupt flexion candidate; intentional impact requires review'})
                prior_angle = angle
                normal = first.cross(second)
                if normal.length > 1e-6 and angle > 10:
                    normal.normalize()
                    if prior_normal is not None and math.degrees(prior_normal.angle(normal, 0)) > 90:
                        pops.append({'limb': side + '_' + lower_role, 'frame': frames[index], 'kind': 'bend_plane_flip',
                                     'angle_step_degrees': math.degrees(prior_normal.angle(normal, 0)), 'confidence': 0.7,
                                     'interpretation': 'Knee/elbow bend plane flipped between samples'})
                    prior_normal = normal
    for name in names:
        velocities = []
        for index in range(1, len(samples)):
            dt = (frames[index] - frames[index - 1]) / fps
            velocity = (samples[index][name]['head'] - samples[index - 1][name]['head']) * (unit / dt)
            velocities.append(velocity)
            max_velocity = max(max_velocity, velocity.length)
            if len(velocities) > 1:
                max_acceleration = max(max_acceleration, (velocities[-1] - velocities[-2]).length / dt)
    root = roles['root']
    root_positions = [obj.matrix_world @ sample[root]['head'] * bpy.context.scene.unit_settings.scale_length for sample in samples]
    root_distance = sum((b-a).length for a, b in zip(root_positions, root_positions[1:]))
    return {'contacts': contacts[:32], 'contact_total': len(contacts),
            'contact_status': 'CONTACT_CONFIRMED' if contacts else 'NO_CONTACT',
            # No detected contact interval: sliding is unknown, never 0 (AUD-005).
            'foot_sliding_max_m': max((row['sliding_m'] for row in contacts), default=None),
            'limb_pops': pops[:32], 'limb_pop_count': len(pops), 'limb_stretch': stretches[:32], 'limb_stretch_count': len(stretches),
            'root_motion': {'displacement_m': list(root_positions[-1] - root_positions[0]), 'distance_m': root_distance},
            'motion_quality': {'max_velocity_m_s': max_velocity, 'max_acceleration_m_s2': max_acceleration, 'max_joint_angle_step_degrees': max_angle_step},
            'contact_model': 'Configured static horizontal ground plane at bone heads; use M3 geometry contact tools for soles/surfaces',
            'thresholds': {key: options[key] for key in ('ground_height_m', 'contact_height_m', 'contact_velocity_m_s', 'sliding_tolerance_m', 'pop_threshold_degrees')}}


def m3_allowed_drivers(obj, options):
    return []


def m3_related_objects(obj, options):
    # The mocap source is evaluated too: its pose position and global state gate and fingerprint.
    source = bpy.context.scene.objects.get(options.get('source_armature', '') or '')
    return [source] if source is not None and source != obj else []


def m3_context(obj, options):
    source = m2_object(options['source_armature'])
    result = m3_context_base(obj)
    result['mocap_source'] = mocap_rig_context(source)
    result['target_local_location'] = [[bone.name, bone.use_local_location] for bone in obj.data.bones]
    return result


def m3_propose(obj, source_action, options):
    options = mocap_options(options)
    source = m2_object(options.get('source_armature', ''))
    if source.type != 'ARMATURE' or obj.type != 'ARMATURE':
        raise ValueError('Source and target must be armatures')
    # AUD-003: confidence is computed from both rigs; caller numbers can only lower it.
    mapping = validate_mapping(options.get('mapping', []), list(source.data.bones.keys()), list(obj.data.bones.keys()),
                               options.get('mapping_approved', False), options.get('minimum_confidence', 0.9),
                               mocap_rig_bones(source), mocap_rig_bones(obj))
    errors = mapping['errors'] + mocap_guard(source, True) + mocap_guard(obj, False)
    if source == obj:
        errors.append('Source and target must be distinct armatures')
    source_rotation = source.matrix_world.to_quaternion()
    if mocap_rotation_error(source_rotation, obj.matrix_world.to_quaternion()) > 0.01:
        errors.append('Source/target object rotations must be aligned; no implicit transform application')
    rows = mapping['mapping']
    names = {row['target_bone'] for row in rows}
    source_names = {row['source_bone'] for row in rows}
    if set(obj.data.bones.keys()) != names or set(source.data.bones.keys()) != source_names:
        errors.append('Conservative adapter requires every source/target bone mapped; review optional twist/helper roles explicitly')
    by_target = {row['target_bone']: row for row in rows}
    if not errors:
        for row in rows:
            sb, tb = source.data.bones[row['source_bone']], obj.data.bones[row['target_bone']]
            if (sb.parent is None) != (tb.parent is None) or (tb.parent and by_target[tb.parent.name]['source_bone'] != sb.parent.name):
                errors.append('Mapped source/target hierarchy mismatch: ' + row['semantic_role'])
            if row['semantic_role'] == 'root' and (sb.parent or tb.parent):
                errors.append('Mapped root must be unparented')
            if abs(sb.length) < 1e-6 or abs(tb.length) < 1e-6:
                errors.append('Zero-length bone unsupported')
    action = source.animation_data.action if source.animation_data else None
    frames = mocap_frames(action, options) if action else []
    ratios = [obj.data.bones[row['target_bone']].length / source.data.bones[row['source_bone']].length for row in rows if row['source_bone'] in source.data.bones and row['target_bone'] in obj.data.bones and source.data.bones[row['source_bone']].length > 1e-6]
    ratios.sort()
    scale = options.get('scale_factor', 1.0) * source.matrix_world.to_scale()[0] / max(obj.matrix_world.to_scale()[0], 1e-12) if options['scale_mode'] == 'explicit' else (ratios[len(ratios)//2] if ratios else 1.0)
    if not errors:
        samples = mocap_sample(source, frames, [row['source_bone'] for row in rows])
        for row in rows:
            for sample in samples:
                if row['semantic_role'] not in ('root', 'pelvis') and sample[row['source_bone']]['basis_location'].length > 1e-6:
                    errors.append('Translation on a non-root/pelvis bone needs a specialized adapter: ' + row['source_bone']); break
                if max(abs(v-1) for v in sample[row['source_bone']]['matrix'].to_scale()) > 1e-5:
                    errors.append('Animated bone scale/shear is unsupported: ' + row['source_bone']); break
    rest_mismatch = max((mocap_rotation_error(source.data.bones[row['source_bone']].matrix_local.to_quaternion(),obj.data.bones[row['target_bone']].matrix_local.to_quaternion()) for row in rows if row['source_bone'] in source.data.bones and row['target_bone'] in obj.data.bones), default=0)
    rest = mocap_rest_compatibility(source, obj, rows, options) if not errors else None
    if rest:
        errors.extend(rest['blocked'])
    # AUD-008: the complete verification budget is checked before any checkpoint or candidate exists.
    verify_frames = mocap_verification_frames(frames) if frames else []
    estimated = len(verify_frames) * len(rows)
    capacity = {'estimated_samples': estimated * 2 + len(frames) * len(rows), 'per_pass_samples': estimated,
                'configured_limit': 60000, 'verification_frames': len(verify_frames),
                'supported': estimated <= 60000}
    if not capacity['supported']:
        errors.append('CAPACITY_EXCEEDED: verification needs ' + str(estimated) + ' bone/frame samples per pass (> 60000); narrow the range or mapping')
    return {'supported': not errors, 'errors': sorted(set(errors)), 'warnings': ['Retarget correspondence does not certify contacts or artistic motion; inspect full QA and visual preview', 'Different proportions can preserve source foot sliding or change support placement']
                + (['Rest poses differ: ' + str(rest['compensated_bones']) + ' bones are aligned to the source rest directions (max ' + str(round(rest['max_direction_difference_degrees'], 2)) + ' deg); twist/roll conventions are kept'] if rest and rest['compensated_bones'] else []),
            'candidate_policy': 'new', 'mapping': rows, 'frames': frames, 'scale_factor': scale,
            'mapping_confidence': [{'role': r['semantic_role'], 'confidence': r.get('confidence'), 'computed_confidence': r.get('computed_confidence'),
                                    'caller_hint': r.get('caller_hint'), 'conflicts': r.get('conflicts', [])} for r in rows][:32],
            'rest_compatibility': {k: v for k, v in rest.items() if k != 'compensation'} if rest else None,
            'rest_compensation': rest['compensation'] if rest else {}, 'capacity': capacity,
            'source_armature': source.name, 'source_action': action.name if action else None,
            'target_armature': obj.name, 'rest_pose_compensation_max_degrees': rest_mismatch,
            'root_motion': options['root_motion'], 'source_fingerprint': m2_digest(m2_snapshot(action)) if action else None,
            'metrics': {'mapped_bones': len(rows), 'frames': len(frames), 'scale_factor': scale,
                        'rest_pose_compensation_max_degrees': rest_mismatch},
            'sampling': 'Bake at requested step; verification includes half steps; no per-frame MCP roundtrips'}


def m3_apply(obj, source_action, candidate, proposal, options):
    options = mocap_options(options)
    source = m2_object(proposal['source_armature'])
    rows, frames = proposal['mapping'], proposal['frames']
    expected = mocap_expected(source, obj, rows, frames, proposal['scale_factor'], proposal['root_motion'], proposal.get('rest_compensation'))
    previous = {}
    def depth(row):
        return len(obj.data.bones[row['target_bone']].parent_recursive)
    for frame, matrices in zip(frames, expected):
        bpy.context.scene.frame_set(frame)
        for row in sorted(rows, key=depth):
            bone = obj.pose.bones[row['target_bone']]
            bone.matrix = matrices[bone.name]
            bpy.context.view_layer.update()
            quaternion = bone.rotation_quaternion.copy()
            if bone.rotation_mode == 'QUATERNION':
                if bone.name in previous and quaternion.dot(previous[bone.name]) < 0:
                    bone.rotation_quaternion = -quaternion
                previous[bone.name] = bone.rotation_quaternion.copy()
                bone.keyframe_insert(data_path='rotation_quaternion', frame=frame)
            else:
                if bone.name in previous:
                    bone.rotation_euler.make_compatible(previous[bone.name])
                previous[bone.name] = bone.rotation_euler.copy()
                bone.keyframe_insert(data_path='rotation_euler', frame=frame)
            bone.keyframe_insert(data_path='location', frame=frame)
            bone.keyframe_insert(data_path='scale', frame=frame)
    for _, _, _, curve in action_curves(candidate, None):
        for point in curve.keyframe_points:
            point.interpolation = 'LINEAR'
    return {'keys_created': sum(len(row[3].keyframe_points) for row in action_curves(candidate, None)),
            'keys_modified': 0, 'frames_processed': len(frames), 'affected_bones': [row['target_bone'] for row in rows]}


def m3_verify(obj, source_action, candidate, proposal, options):
    options = mocap_options(options)
    source = m2_object(proposal['source_armature'])
    frames = mocap_verification_frames(proposal['frames'])
    expected = mocap_expected(source, obj, proposal['mapping'], frames, proposal['scale_factor'], proposal['root_motion'], proposal.get('rest_compensation'))
    measured = mocap_sample(obj, frames, [row['target_bone'] for row in proposal['mapping']])
    # AUD-002 correspondence, independent of the transfer formula: every non-convention target bone
    # must point where its source bone points; root/pelvis must rotate from rest like the source.
    source_measured = mocap_sample(source, frames, [row['source_bone'] for row in proposal['mapping']])
    direction_errors, convention_errors = [], []
    for got, src in zip(measured, source_measured):
        for row in proposal['mapping']:
            target_matrix, source_matrix = got[row['target_bone']]['matrix'], src[row['source_bone']]['matrix']
            if row['semantic_role'] in ('root', 'pelvis'):
                target_delta = target_matrix.to_quaternion() @ obj.data.bones[row['target_bone']].matrix_local.to_quaternion().inverted()
                source_delta = source_matrix.to_quaternion() @ source.data.bones[row['source_bone']].matrix_local.to_quaternion().inverted()
                convention_errors.append(mocap_rotation_error(target_delta, source_delta))
            else:
                target_axis = (target_matrix.to_3x3() @ Vector((0, 1, 0))).normalized()
                source_axis = (source_matrix.to_3x3() @ Vector((0, 1, 0))).normalized()
                direction_errors.append(math.degrees(target_axis.angle(source_axis, 0)))
    unit = bpy.context.scene.unit_settings.scale_length * obj.matrix_world.to_scale()[0]
    positions, rotations = [], []
    for wanted, got in zip(expected, measured):
        for name, matrix in wanted.items():
            positions.append((matrix.translation - got[name]['head']).length * unit)
            rotations.append(mocap_rotation_error(matrix.to_quaternion(),got[name]['matrix'].to_quaternion()))
    qa = mocap_quality(obj, proposal['mapping'], frames, options)
    source_rows = [dict(row, target_bone=row['source_bone']) for row in proposal['mapping']]
    source_qa = mocap_quality(source, source_rows, frames, options)
    introduced_pops = [event for event in qa['limb_pops'] if not any(
        prior['limb'] == event['limb'] and prior['frame'] == event['frame'] and prior.get('kind') == event.get('kind')
        and event['angle_step_degrees'] <= prior['angle_step_degrees'] + 2*options['rotation_tolerance_degrees']
        for prior in source_qa['limb_pops'])]
    direction_max = max(direction_errors, default=0.0)
    convention_max = max(convention_errors, default=0.0)
    intact = m2_digest(m2_snapshot(source.animation_data.action)) == proposal['source_fingerprint']
    passed = (max(positions, default=0) <= options['position_tolerance_m'] and max(rotations, default=0) <= options['rotation_tolerance_degrees']
              and intact and qa['limb_stretch_count'] == 0 and not introduced_pops and qa['limb_pop_count'] <= 32 and source_qa['limb_pop_count'] <= 32
              and direction_max <= options['rotation_tolerance_degrees'] and convention_max <= options['rotation_tolerance_degrees'])
    metrics = {'max_position_error_m': max(positions, default=0), 'mean_position_error_m': sum(positions)/len(positions) if positions else 0,
               'max_rotation_error_degrees': max(rotations, default=0), 'mean_rotation_error_degrees': sum(rotations)/len(rotations) if rotations else 0,
               'bone_frame_samples': len(positions), 'source_preserved': intact, 'introduced_limb_pops': introduced_pops,
               'verification_frames': len(frames),
               'max_bone_direction_error_degrees': direction_max, 'max_root_pelvis_delta_error_degrees': convention_max,
               'limb_stretch_check': 'structural: FK retarget keeps target bone lengths; informational only',
               'source_qa': source_qa, 'target_qa': qa}
    errors = []
    if direction_max > options['rotation_tolerance_degrees']:
        errors.append('POSE_CORRESPONDENCE_FAILED: target bones do not point where source bones point (max ' + str(round(direction_max, 4)) + ' deg)')
    if convention_max > options['rotation_tolerance_degrees']:
        errors.append('POSE_CORRESPONDENCE_FAILED: root/pelvis rotation from rest differs from the source')
    if introduced_pops:
        errors.append('INTRODUCED_LIMB_EVENTS: ' + str(len(introduced_pops)) + ' limb pops/bend-plane flips absent from the source')
    return {'passed': passed, 'numeric_verification': 'pass' if passed else 'fail', 'metrics': metrics, 'errors': errors,
            'warnings': (['Foot-contact/sliding findings are reported separately; a retarget pass does not imply feet are locked'] if qa['foot_sliding_max_m'] is not None and qa['foot_sliding_max_m'] > options['sliding_tolerance_m'] else [])
                        + (['NO CONTACT EVIDENCE IS NOT ZERO SLIDING: no bone-head contact interval met the plane thresholds'] if qa['foot_sliding_max_m'] is None else [])}
