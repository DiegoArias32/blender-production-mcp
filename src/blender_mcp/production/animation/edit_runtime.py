"""Shared Blender-side Action guards and exact content snapshots.

Loaded as source through the existing safe-mode bridge, never imported in bpy.
"""

ACTION_RUNTIME = r'''
from mathutils import Vector

def m2_keys(action):
    # Count before building rows: an oversized Action is refused in milliseconds instead of after
    # serializing hundreds of thousands of keys (measured 4.3 s for 480480 keys, AUD-008).
    curves = list(action_curves(action, None))
    if sum(len(row[3].keyframe_points) + len(row[3].sampled_points) for row in curves) > 100000:
        raise ValueError("CAPACITY_EXCEEDED: Action snapshot exceeds 100000 points; select a smaller Action")
    output = []
    for slot, layer, strip, curve in curves:
        output.append({"slot": slot, "layer": layer, "strip": strip,
            "path": curve.data_path, "index": curve.array_index,
            "mute": curve.mute, "lock": curve.lock, "extrapolation": curve.extrapolation,
            "group": curve.group.name if curve.group else None,
            "modifiers": [modifier.type for modifier in curve.modifiers],
            "keys": [[list(point.co), list(point.handle_left),
                      list(point.handle_right), point.interpolation,
                      point.handle_left_type, point.handle_right_type,
                      point.type, point.easing,
                      point.amplitude, point.back, point.period]
                     for point in curve.keyframe_points],
            "samples": [list(point.co) for point in curve.sampled_points]})
    if sum(len(row["keys"]) + len(row["samples"]) for row in output) > 100000:
        raise ValueError("CAPACITY_EXCEEDED: Action snapshot exceeds 100000 points; select a smaller Action")
    return output

def m2_snapshot(action):
    return {"snapshot_format":2,"key_fields":["co","left","right","interpolation","left_type","right_type","type","easing","amplitude","back","period"],
            "channels": m2_keys(action), "custom_properties": [[key, str(value)] for key, value in action.items()],
            "slots": [{"handle": slot.handle, "identifier": slot.identifier, "type": slot.target_id_type}
                      for slot in getattr(action, "slots", [])],
            "markers": [{"name": marker.name, "frame": marker.frame} for marker in action.pose_markers],
            "fake_user": action.use_fake_user,
            "manual_range": action.use_frame_range, "frame_start": action.frame_start,
            "frame_end": action.frame_end, "cyclic": action.use_cyclic,
            "layers": [{"name": layer.name, "strip_types": [strip.type for strip in layer.strips]}
                       for layer in getattr(action, "layers", [])]}

def m2_object(name):
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise ValueError("Object missing from active scene: " + name)
    return obj

def m2_slot(obj):
    slot = getattr(obj.animation_data, "action_slot", None) if obj.animation_data else None
    return slot.handle if slot else None

def m2_assign(obj, action, slot_handle):
    selected_slot=None
    if action is not None and getattr(action, "is_action_layered", False):
        matches = [slot for slot in action.slots if slot.handle == slot_handle]
        if not matches or matches[0].target_id_type!='OBJECT':
            raise ValueError("Matching Action slot not found; assignment refused")
        selected_slot=matches[0]
    data = obj.animation_data_create()
    prior_action=data.action
    prior_slot=getattr(data,'action_slot',None)
    try:
        data.action = action
        if selected_slot is not None:
            data.action_slot = selected_slot
    except Exception:
        data.action=prior_action
        if prior_slot is not None:
            data.action_slot=prior_slot
        raise

def m2_guard(obj, action):
    if obj.library or action.library or obj.override_library or action.override_library:
        raise ValueError("Linked/override animation correction is unsupported")
    if obj.animation_data is None or obj.animation_data.action != action:
        raise ValueError("Correction requires the explicitly selected active Action")
    data = obj.animation_data
    if data.use_tweak_mode or any(not track.mute and len(track.strips) for track in data.nla_tracks):
        raise ValueError("Active NLA/tweak mode is unsupported; no implicit baking")
    if len(data.drivers) or (obj.data and obj.data.animation_data and len(obj.data.animation_data.drivers)):
        raise ValueError("Driven animation is unsupported for automatic correction")
    if data.action_blend_type != 'REPLACE' or abs(data.action_influence - 1) > 1e-7:
        raise ValueError("Blended or partial-influence Action correction is unsupported")
    if obj.constraints:
        raise ValueError("Object-level constraints require manual correction preparation")
    if obj.parent and obj.parent.parent:
        raise ValueError("Multi-level object parenting is not supported by correction context validation")
    related=[]
    if obj.parent:
        related.append(obj.parent)
    if obj.type=='ARMATURE':
        for bone in obj.pose.bones:
            for constraint in bone.constraints:
                if constraint.type=='IK' and constraint.pole_target and constraint.pole_target!=obj:
                    related.append(constraint.pole_target)
    for other in related:
        if other.constraints or other.parent or (other.animation_data and (other.animation_data.drivers or any(not track.mute and len(track.strips) for track in other.animation_data.nla_tracks))):
            raise ValueError('Driven/constrained/parented external targets require manual preparation')
    prod_require_evaluable([obj] + related, ['target'] + ['related'] * len(related))
    if getattr(action, "is_action_layered", False):
        if len(action.slots) != 1 or len(action.layers) != 1 or len(action.layers[0].strips) != 1:
            raise ValueError("Corrections currently require one slot, layer and strip; no flattening")
    for slot, layer, strip, curve in action_curves(action, m2_slot(obj)):
        if curve.modifiers or curve.sampled_points or curve.mute or curve.lock:
            raise ValueError("Muted/locked/modifier/baked-sample F-Curves need manual preparation")
        if '.constraints[' in curve.data_path or '.ik_' in curve.data_path:
            raise ValueError('Animated solver/constraint settings require manual preparation')

def m2_active(name):
    obj = m2_object(name)
    if obj.animation_data is None or obj.animation_data.action is None:
        raise ValueError("Object has no active Action")
    return obj, obj.animation_data.action

def m2_context(obj):
    scene = bpy.context.scene
    old_frame, old_subframe = scene.frame_current, scene.frame_subframe
    try:
        scene.frame_set(scene.frame_start)
        context = {"source_file": bpy.data.filepath, "scene": scene.name,
            "fps": scene.render.fps / scene.render.fps_base,
            "units": scene.unit_settings.scale_length, "unit_system": scene.unit_settings.system,
            "range": [scene.frame_start, scene.frame_end],
            "rotation_mode":obj.rotation_mode,
            "object": obj.name, "matrix": [list(row) for row in obj.matrix_world],
            "parent": obj.parent.name if obj.parent else None,
            "parent_action": m2_snapshot(obj.parent.animation_data.action)
               if obj.parent and obj.parent.animation_data and obj.parent.animation_data.action else None,
            "global_state": prod_global_state([obj] + ([obj.parent] if obj.parent else [])),
            "bones": []}
        if obj.type == 'ARMATURE':
            for bone in obj.pose.bones:
                context['bones'].append({"name": bone.name, "parent": bone.parent.name if bone.parent else None,
                    "rest": [list(row) for row in bone.bone.matrix_local], "deform": bone.bone.use_deform,
                    "tail":list(bone.bone.tail_local),"head":list(bone.bone.head_local),
                    "ik_stretch":bone.ik_stretch,"lock_ik":[bone.lock_ik_x,bone.lock_ik_y,bone.lock_ik_z],
                    "limits_ik":[bone.use_ik_limit_x,bone.use_ik_limit_y,bone.use_ik_limit_z,
                        bone.ik_min_x,bone.ik_max_x,bone.ik_min_y,bone.ik_max_y,bone.ik_min_z,bone.ik_max_z],
                    "location":list(bone.location),"scale":list(bone.scale),
                    "rotation_quaternion":list(bone.rotation_quaternion),"rotation_euler":list(bone.rotation_euler),
                    "rotation_axis_angle":list(bone.rotation_axis_angle),
                    "rotation_mode": bone.rotation_mode,
                    "constraints": [{"name": c.name, "type": c.type, "mute": c.mute,
                        "influence": c.influence, "target": c.target.name if hasattr(c, 'target') and c.target else None,
                        "subtarget": c.subtarget if hasattr(c, 'subtarget') else None,
                        "chain_count": c.chain_count if c.type == 'IK' else None,
                        "iterations": c.iterations if c.type=='IK' else None,
                        "use_stretch": c.use_stretch if c.type=='IK' else None,
                        "use_rotation": c.use_rotation if c.type=='IK' else None,
                        "use_tail": c.use_tail if c.type=='IK' else None,
                        "pole": c.pole_subtarget if c.type == 'IK' else None,
                        "pole_target": c.pole_target.name if c.type=='IK' and c.pole_target else None,
                        "pole_matrix": [list(row) for row in c.pole_target.matrix_world] if c.type=='IK' and c.pole_target else None,
                        "pole_action":m2_snapshot(c.pole_target.animation_data.action)
                            if c.type=='IK' and c.pole_target and c.pole_target!=obj and c.pole_target.animation_data and c.pole_target.animation_data.action else None,
                        "pole_angle": c.pole_angle if c.type == 'IK' else None} for c in bone.constraints]})
        return context
    finally:
        scene.frame_set(old_frame, subframe=old_subframe)

def m2_capture_state(obj):
    state={'object':{'location':list(obj.location),'scale':list(obj.scale),
        'quaternion':list(obj.rotation_quaternion),'euler':list(obj.rotation_euler),
        'axis_angle':list(obj.rotation_axis_angle)},'pose':{}}
    if obj.type=='ARMATURE':
        for bone in obj.pose.bones:
            state['pose'][bone.name]={'location':list(bone.location),'scale':list(bone.scale),
                'quaternion':list(bone.rotation_quaternion),'euler':list(bone.rotation_euler),
                'axis_angle':list(bone.rotation_axis_angle)}
    return state

def m2_restore_state(obj,state):
    obj.location=state['object']['location'];obj.scale=state['object']['scale']
    obj.rotation_quaternion=state['object']['quaternion'];obj.rotation_euler=state['object']['euler']
    obj.rotation_axis_angle=state['object']['axis_angle']
    for name,values in state['pose'].items():
        bone=obj.pose.bones.get(name)
        if bone is None:
            raise ValueError('Bone missing during state restore: '+name)
        bone.location=values['location'];bone.scale=values['scale']
        bone.rotation_quaternion=values['quaternion'];bone.rotation_euler=values['euler']
        bone.rotation_axis_angle=values['axis_angle']
    bpy.context.scene.frame_set(bpy.context.scene.frame_current,subframe=bpy.context.scene.frame_subframe)

M2_STATE_KEYS = {'location': 'location', 'scale': 'scale', 'rotation_quaternion': 'quaternion',
                 'rotation_euler': 'euler', 'rotation_axis_angle': 'axis_angle'}

def m2_written_static_paths(source, candidate):
    # Channels a correction keyed that the source never animated: their static values are
    # operation residue and are the only static values verify/revert may return to proposal time.
    source_paths = {row[3].data_path for row in action_curves(source, None)} if source else set()
    return {row[3].data_path for row in action_curves(candidate, None)} - source_paths

def m2_restore_paths(obj, state, paths):
    restored = []
    for path in sorted(paths):
        owner, record, attr = obj, state['object'], path
        if obj.type == 'ARMATURE':
            for pose in obj.pose.bones:
                prefix = pose.path_from_id() + '.'
                if path.startswith(prefix):
                    owner, record, attr = pose, state['pose'].get(pose.name), path[len(prefix):]
                    break
        if record is None or attr not in M2_STATE_KEYS:
            continue
        value = record[M2_STATE_KEYS[attr]]
        # Literal attribute names only: safe mode forbids computed setattr.
        if attr == 'location':
            owner.location = value
        elif attr == 'scale':
            owner.scale = value
        elif attr == 'rotation_quaternion':
            owner.rotation_quaternion = value
        elif attr == 'rotation_euler':
            owner.rotation_euler = value
        else:
            owner.rotation_axis_angle = value
        restored.append(path)
    return restored

def m2_sample(obj, frames, names):
    if len(frames) * max(1, len(names)) > 100000:
        raise ValueError("Motion verification exceeds 100000 bone/frame samples")
    scene = bpy.context.scene
    old_frame, old_subframe = scene.frame_current, scene.frame_subframe
    samples = {name: [] for name in names}
    try:
        for frame in frames:
            base = math.floor(frame)
            scene.frame_set(base, subframe=frame-base)
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for name in names:
                bone = evaluated.pose.bones.get(name) if name else None
                matrix = evaluated.matrix_world @ bone.matrix if bone else evaluated.matrix_world
                samples[name].append({"position": list(matrix.translation * scene.unit_settings.scale_length),
                    "rotation": list(matrix.to_quaternion().normalized()),
                    "tail": list(evaluated.matrix_world @ bone.tail * scene.unit_settings.scale_length) if bone else None,
                    "length": bone.length * scene.unit_settings.scale_length if bone else None})
        return samples
    finally:
        scene.frame_set(old_frame, subframe=old_subframe)

def m2_angle(first, second):
    dot = min(1.0, max(0.0, abs(sum(a*b for a,b in zip(first, second)))))
    return math.degrees(2*math.acos(dot))

def m2_curve_summary(action):
    rows = action_curves(action, None)
    return {"name": action.name, "channels": len(rows),
            "keys": sum(len(row[3].keyframe_points) for row in rows),
            "range": list(action.frame_range)}
'''
