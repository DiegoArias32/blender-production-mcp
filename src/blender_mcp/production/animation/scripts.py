"""Small, safe-mode compatible scripts executed in Blender by the shared bridge."""

from ..common.state_guard import STATE_RUNTIME

ACTION_HELPERS = r'''
import bpy
import math
''' + STATE_RUNTIME + r'''
def action_selection():
    selected_object = None
    chosen_actions = []
    slot_handle = None
    object_name = params.get("object_name")
    action_name = params.get("action_name")
    if object_name:
        selected_object = bpy.data.objects.get(object_name)
        if selected_object is None:
            raise ValueError("Object not found: " + object_name)
        animation_data = selected_object.animation_data
        if animation_data is not None and animation_data.action is not None:
            chosen_actions = [animation_data.action]
            active_slot = getattr(animation_data, "action_slot", None)
            if active_slot is not None:
                slot_handle = active_slot.handle
            elif getattr(animation_data.action, "is_action_layered", False):
                slot_handle = -1
        if action_name and (not chosen_actions or chosen_actions[0].name != action_name):
            raise ValueError("Requested Action is not the object's active Action; omit object_name to analyze all its slots")
    elif action_name:
        selected_action = bpy.data.actions.get(action_name)
        if selected_action is None:
            raise ValueError("Action not found: " + action_name)
        chosen_actions = [selected_action]
    else:
        chosen_actions = list(bpy.data.actions)
    if len(chosen_actions) > 256:
        raise ValueError("More than 256 Actions; select one Action or object")
    return selected_object, chosen_actions, slot_handle

def action_curves(action, slot_handle):
    rows = []
    if getattr(action, "is_action_layered", False):
        if slot_handle is None:
            allowed_slots = None
        else:
            allowed_slots = {slot_handle}
        for layer_index, layer in enumerate(action.layers):
            for strip_index, strip in enumerate(layer.strips):
                if strip.type != "KEYFRAME":
                    continue
                for bag in strip.channelbags:
                    if allowed_slots is not None and bag.slot_handle not in allowed_slots:
                        continue
                    for curve in bag.fcurves:
                        rows.append((bag.slot_handle, layer_index, strip_index, curve))
    elif hasattr(action, "fcurves"):
        rows = [(None, None, None, curve) for curve in action.fcurves]
    if len(rows) > 50000:
        raise ValueError("More than 50000 F-Curves in an Action; analysis budget exceeded")
    return rows
'''

ANALYZE = ACTION_HELPERS + r'''
selected_object, selected_actions, selected_slot = action_selection()
action_summaries = []
channel_summaries = []
warnings = []
total_channels = 0
total_keys = 0
total_sampled_points = 0
invalid_curves = 0
interpolation_counts = {}
for action in selected_actions:
    rows = action_curves(action, selected_slot)
    action_keys = 0
    for slot_handle, layer_index, strip_index, curve in rows:
        key_count = len(curve.keyframe_points)
        action_keys += key_count
        total_keys += key_count
        total_sampled_points += len(curve.sampled_points)
        if total_keys > 500000:
            raise ValueError("More than 500000 keys; select one object or Action")
        for point in curve.keyframe_points:
            interpolation_counts[point.interpolation] = interpolation_counts.get(point.interpolation, 0) + 1
        if not curve.is_valid:
            invalid_curves += 1
        if params["offset"] <= total_channels < params["offset"] + params["limit"]:
            key_frames = [float(point.co[0]) for point in curve.keyframe_points]
            channel_summaries.append({
                "action": action.name, "slot_handle": slot_handle,
                "layer_index": layer_index, "strip_index": strip_index,
                "data_path": curve.data_path, "array_index": curve.array_index,
                "key_count": key_count, "sampled_point_count": len(curve.sampled_points),
                "frame_range": [min(key_frames), max(key_frames)] if key_frames else None,
                "muted": curve.mute, "valid": curve.is_valid,
                "modifier_types": [modifier.type for modifier in curve.modifiers],
            })
        total_channels += 1
    action_summaries.append({
        "action": action.name, "layered": getattr(action, "is_action_layered", False),
        "frame_range": list(action.frame_range), "channel_count": len(rows),
        "key_count": action_keys,
        "slots": [{"identifier": slot.identifier, "handle": slot.handle,
                   "target_id_type": slot.target_id_type}
                  for slot in getattr(action, "slots", [])][:100],
        "slot_count": len(getattr(action, "slots", [])),
    })
nla_tracks = []
if selected_object is not None and selected_object.animation_data is not None:
    for track in selected_object.animation_data.nla_tracks:
        nla_tracks.append({"name": track.name, "muted": track.mute,
                           "strip_count": len(track.strips)})
    if nla_tracks:
        warnings.append("Channel summaries describe the active Action only; NLA composition is not flattened")
result = {
    "object": selected_object.name if selected_object is not None else None,
    "actions": action_summaries, "channels": channel_summaries,
    "pagination": {"offset": params["offset"], "limit": params["limit"],
                   "total": total_channels,
                   "next_offset": params["offset"] + len(channel_summaries)
                       if params["offset"] + len(channel_summaries) < total_channels else None},
    "metrics": {"action_count": len(selected_actions), "channel_count": total_channels,
                "key_count": total_keys, "sampled_point_count": total_sampled_points,
                "invalid_curve_count": invalid_curves,
                "interpolation_counts": interpolation_counts},
    "scene": {"frame_start": bpy.context.scene.frame_start,
              "frame_end": bpy.context.scene.frame_end,
              "fps": bpy.context.scene.render.fps / bpy.context.scene.render.fps_base},
    "nla_tracks": nla_tracks[:100], "nla_track_count": len(nla_tracks),
    "warnings": warnings,
}
'''

QUATERNIONS = ACTION_HELPERS + r'''
selected_object, selected_actions, selected_slot = action_selection()
groups = []
warnings = ["Negative quaternion dot products are representation discontinuity candidates, not proof of visible rotation flips",
            "Analysis samples F-Curves at the union of keyed or baked sample times; it does not solve NLA blending, drivers, or between-key overshoot"]
total_evaluations = 0
for action in selected_actions:
    grouped = {}
    for slot_handle, layer_index, strip_index, curve in action_curves(action, selected_slot):
        if curve.data_path == "rotation_quaternion" or curve.data_path.endswith(".rotation_quaternion"):
            group_id = (slot_handle, layer_index, strip_index, curve.data_path)
            if group_id not in grouped:
                grouped[group_id] = {}
            grouped[group_id][curve.array_index] = curve
    for group_id, curves in grouped.items():
        slot_handle, layer_index, strip_index, data_path = group_id
        identity = {"action": action.name, "slot_handle": slot_handle,
                    "layer_index": layer_index, "strip_index": strip_index,
                    "data_path": data_path}
        if set(curves) != {0, 1, 2, 3}:
            groups.append(dict(identity, status="inconclusive",
                               missing_components=sorted({0, 1, 2, 3} - set(curves))))
            continue
        frames = sorted({float(point.co[0]) for curve in curves.values()
                         for point in curve.keyframe_points} |
                        {float(point.co[0]) for curve in curves.values()
                         for point in curve.sampled_points})
        total_evaluations += len(frames) * 4
        if total_evaluations > 200000:
            raise ValueError("Quaternion analysis exceeds 200000 component evaluations; select an Action or object")
        values = [[float(curves[index].evaluate(frame)) for index in range(4)] for frame in frames]
        report = quaternion_report(frames, values)
        review = report["sign_discontinuity_count"] + report["orientation_jump_count"] + report["invalid_quaternion_count"]
        groups.append(dict(identity, status="review" if review else ("pass" if len(frames) > 1 else "inconclusive"),
                           sample_count=len(frames), **report))
result = {
    "object": selected_object.name if selected_object is not None else None,
    "groups": groups[params["offset"]:params["offset"] + params["limit"]],
    "pagination": {"offset": params["offset"], "limit": params["limit"], "total": len(groups),
                   "next_offset": params["offset"] + params["limit"]
                       if params["offset"] + params["limit"] < len(groups) else None},
    "metrics": {"quaternion_group_count": len(groups), "component_evaluations": total_evaluations,
                "sign_discontinuity_count": sum(group.get("sign_discontinuity_count", 0) for group in groups),
                "orientation_jump_count": sum(group.get("orientation_jump_count", 0) for group in groups),
                "invalid_quaternion_count": sum(group.get("invalid_quaternion_count", 0) for group in groups),
                "incomplete_group_count": sum("missing_components" in group for group in groups)},
    "warnings": warnings,
}
'''

FOOT_SLIDING = r'''
import bpy
import math
scene = bpy.context.scene
rig = bpy.data.objects.get(params["armature"])
if rig is None or rig.type != "ARMATURE":
    raise ValueError("Armature object not found: " + params["armature"])
prod_require_evaluable([rig], ["analyzed armature"])
warnings = ["Contact is inferred on a horizontal ground plane from bone-head height and vertical speed; calibrate ground_height_m to the marker, not the mesh sole",
            "Results measure current evaluated scene including constraints and NLA; samples can miss events between sampled frames",
            "Slope, stairs, moving platforms, heel/toe roll and deliberate low hovering require review"]
unit_scale = float(scene.unit_settings.scale_length)
if not math.isfinite(unit_scale) or unit_scale <= 0:
    raise ValueError("Scene unit scale must be finite and positive")
if scene.unit_settings.system == "NONE":
    warnings.append("Scene units are NONE: meters assume scale_length meters per Blender unit; confirm physical scale")
fps = scene.render.fps / scene.render.fps_base
start = scene.frame_start if params["frame_start"] is None else params["frame_start"]
end = scene.frame_end if params["frame_end"] is None else params["frame_end"]
if end < start:
    raise ValueError("frame_end must be >= frame_start")
step = params["step"]
if (end - start) // step + 1 > 10000:
    raise ValueError("Sampling exceeds 10000 frames; narrow frame range or increase step")
frames = list(range(start, end + 1, step))
if frames[-1] != end:
    frames.append(end)
rest_heads = [rig.matrix_world @ bone.head_local for bone in rig.data.bones]
z_min = min((point.z for point in rest_heads), default=0.0)
z_max = max((point.z for point in rest_heads), default=0.0)
height = max(z_max - z_min, 1e-8)
candidates = []
explicit = params["bones"] is not None
if explicit:
    for bone_name in params["bones"]:
        if rig.pose.bones.get(bone_name) is None:
            raise ValueError("Bone not found: " + bone_name)
        candidates.append({"bone": bone_name, "confidence": 1.0,
                           "confidence_kind": "selection certainty, not anatomical confidence",
                           "selection": "explicit_user_marker", "signals": ["explicit selection; anatomy not inferred"]})
else:
    ik_targets = {constraint.subtarget for pose_bone in rig.pose.bones
                  for constraint in pose_bone.constraints
                  if constraint.type == "IK" and constraint.target == rig}
    for bone in rig.data.bones:
        position = rig.matrix_world @ bone.head_local
        low = (position.z - z_min) / height <= 0.22
        name_tokens = bone.name.lower().replace("_", " ").replace(".", " ").replace("-", " ").replace(":", " ").split()
        name_hint = any(token in ("pie", "tobillo")
                        or token.startswith(("foot", "ankle", "toe"))
                        or token.endswith(("foot", "ankle", "toe", "footik", "ankleik"))
                        for token in name_tokens)
        depth = 0
        ancestor = bone.parent
        while ancestor is not None:
            depth += 1
            ancestor = ancestor.parent
        chain_hint = depth >= 2
        ik_target = bone.name in ik_targets
        tail_vector = rig.matrix_world.to_3x3() @ (bone.tail_local - bone.head_local)
        horizontal = abs(tail_vector.z) < tail_vector.length * 0.5
        signals = []
        score = 0.0
        if low:
            signals.append("lower 22 percent of world-Z rest skeleton")
            score += 0.3
        if chain_hint:
            signals.append("hierarchy depth at least 2")
            score += 0.15
        if name_hint:
            signals.append("anatomical name hint")
            score += 0.2
        if ik_target:
            signals.append("IK target")
            score += 0.2
        if horizontal:
            signals.append("mostly horizontal rest bone")
            score += 0.15
        if low and score >= 0.6:
            candidates.append({"bone": bone.name, "confidence": round(min(score, 0.95), 2),
                               "confidence_kind": "heuristic evidence score, not calibrated probability",
                               "selection": "heuristic", "signals": signals})
    def candidate_order(candidate):
        return (-candidate["confidence"], candidate["bone"])
    candidates.sort(key=candidate_order)
    warnings.append("Automatic candidates are advisory, assume an upright rig along world Z and are not semantic proof of feet; use explicit bones for nonhumanoid or rotated rigs")
if len(candidates) > 32:
    raise ValueError("More than 32 candidate markers; supply an explicit bone list")
if len(candidates) * len(frames) > 100000:
    raise ValueError("Sampling exceeds 100000 bone-frame evaluations; narrow range or select fewer bones")
samples = {candidate["bone"]: [] for candidate in candidates}
original_frame = scene.frame_current
original_subframe = scene.frame_subframe
try:
    if candidates:
        for frame in frames:
            scene.frame_set(frame)
            evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for candidate in candidates:
                pose_bone = evaluated.pose.bones[candidate["bone"]]
                position = evaluated.matrix_world @ pose_bone.head
                samples[candidate["bone"]].append([float(value * unit_scale) for value in position])
finally:
    scene.frame_set(original_frame, subframe=original_subframe)
reports = []
for candidate in candidates:
    measurement = contact_report(frames, samples[candidate["bone"]], fps,
        params["ground_height_m"], params["contact_height_m"], params["contact_speed_m_s"],
        params["sliding_tolerance_m"], params["min_contact_frames"])
    reports.append(dict(candidate, **measurement))
if not reports or all(report["contact_count"] == 0 for report in reports):
    warnings.append("No contacts detected: result is inconclusive; verify selected marker and ground calibration")
result = {
    "armature": rig.name, "feet": reports,
    "measurement_status": "fail" if any(report["failed_contact_count"] for report in reports)
        else ("pass" if reports and all(report["contact_count"] for report in reports) else "inconclusive"),
    "frame_range": [start, end], "sample_step": step, "sample_count": len(frames),
    "fps": fps, "meters_per_blender_unit": unit_scale,
    "marker": "evaluated bone head in world space", "ground_axis": "world Z",
    "thresholds": {"ground_height_m": params["ground_height_m"],
                   "contact_height_m": params["contact_height_m"],
                   "contact_vertical_speed_m_s": params["contact_speed_m_s"],
                   "sliding_tolerance_m": params["sliding_tolerance_m"],
                   "min_contact_frames": params["min_contact_frames"]},
    "metrics": {"candidate_count": len(candidates), "bone_frame_evaluations": len(candidates) * len(frames),
                "failed_contact_count": sum(report["failed_contact_count"] for report in reports),
                "max_sliding_m": max((report["max_sliding_m"] for report in reports if report["max_sliding_m"] is not None), default=None)},
    "warnings": warnings,
}
'''
