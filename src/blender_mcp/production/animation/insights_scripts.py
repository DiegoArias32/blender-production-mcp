"""Bounded read-only evaluated sampling for M2 analysis.

The host owns algorithms and public summaries. These payloads only read Blender
and restore evaluation time, including when constraints raise during sampling.
"""

from .scripts import ACTION_HELPERS


CONTROL_CONTEXT = r'''
import bpy
obj = bpy.context.scene.objects.get(params["object_name"])
if obj is None or obj.type != "ARMATURE":
    raise ValueError("Control-mode analysis requires an armature in the active scene")
action = obj.animation_data.action if obj.animation_data else None
if params["action_name"] and (action is None or action.name != params["action_name"]):
    raise ValueError("Requested Action must be the object's active Action")
result = {"object": obj.name, "action": action.name if action else None,
          "frame": bpy.context.scene.frame_current, "subframe": bpy.context.scene.frame_subframe,
          "fps": bpy.context.scene.render.fps / bpy.context.scene.render.fps_base}
'''


SAMPLE = ACTION_HELPERS + r'''
scene = bpy.context.scene
obj = scene.objects.get(params["object_name"])
if obj is None:
    raise ValueError("Object not found in active scene: " + params["object_name"])
if obj.mode != "OBJECT" and obj.mode != "POSE":
    raise ValueError("Evaluated animation analysis requires Object or Pose mode")
prod_require_evaluable([obj], ["analyzed object"])
if params.get("action_name"):
    if obj.animation_data is None or obj.animation_data.action is None or obj.animation_data.action.name != params["action_name"]:
        raise ValueError("Requested Action must be the object's active Action; analysis never changes Action assignments")
start = scene.frame_start if params["frame_start"] is None else params["frame_start"]
end = scene.frame_end if params["frame_end"] is None else params["frame_end"]
if end < start:
    raise ValueError("frame_end must be >= frame_start")
if (end - start) // params["step"] + 1 > params["max_samples"]:
    raise ValueError("Analysis frame budget exceeded; narrow range or increase step")
frames = list(range(start, end + 1, params["step"]))
if frames[-1] != end:
    frames.append(end)
if len(frames) > params["max_samples"]:
    raise ValueError("Analysis frame budget exceeded including final endpoint")
scale = float(scene.unit_settings.scale_length)
fps = scene.render.fps / scene.render.fps_base
if not math.isfinite(scale) or scale <= 0 or fps <= 0:
    raise ValueError("Physical scale and FPS must be positive and finite")
warnings = ["Sampling evaluates current scene constraints, drivers and NLA; it does not isolate raw Action motion", "Sampling can miss between-sample events; confidence is an evidence score, not a probability"]
if scene.unit_settings.system == "NONE":
    warnings.append("Units NONE: meters assume scale_length meters per Blender unit; confirm physical scale")
metadata = []
root_candidates = []
effectors = []
bone_count = 0
is_armature = obj.type == "ARMATURE"
if is_armature:
    bones = list(obj.data.bones)
    bone_count = len(bones)
    if bone_count > 2048:
        raise ValueError("Rig exceeds 2048-bone analysis budget")
    ik_targets = {}
    for pose in obj.pose.bones:
        for con in pose.constraints:
            if con.type == "IK" and con.target == obj and con.subtarget:
                ik_targets.setdefault(con.subtarget, []).append({"owner": pose.name,
                    "constraint": con.name, "active": not con.mute and con.influence > 0 and con.is_valid,
                    "influence": con.influence, "chain_count": con.chain_count})
    heights = [(obj.matrix_world @ bone.head_local).z for bone in bones]
    z_min = min(heights, default=0)
    z_max = max(heights, default=0)
    height = max(z_max - z_min, 1e-8)
    # Rest-local X symmetry is a signal, never an anatomical left/right claim.
    extent = max([bone.head_local.length for bone in bones] + [1e-6])
    tolerance = max(extent * 0.001, 1e-6)
    index = {}
    for bone in bones:
        key = tuple(round(value / tolerance) for value in bone.head_local)
        index.setdefault(key, []).append(bone.name)
    deform_bound = set()
    for mesh in scene.objects:
        if mesh.type == "MESH" and any(modifier.type == "ARMATURE" and modifier.object == obj for modifier in mesh.modifiers):
            deform_bound.update(group.name for group in mesh.vertex_groups)
    for bone in bones:
        depth = 0
        cursor = bone.parent
        while cursor:
            depth += 1
            cursor = cursor.parent
        world = obj.matrix_world @ bone.head_local
        relative_height = (world.z - z_min) / height
        mirror_key = (round(-bone.head_local.x / tolerance), round(bone.head_local.y / tolerance), round(bone.head_local.z / tolerance))
        symmetry = [name for name in index.get(mirror_key, []) if name != bone.name]
        words = bone.name.lower().replace("_", " ").replace(".", " ").replace("-", " ").split()
        foot_name = any(word.startswith(("foot", "ankle", "toe", "pie", "tobillo")) for word in words)
        hand_name = any(word.startswith(("hand", "wrist", "mano", "palm")) for word in words)
        side_hint = "left_candidate" if any(word in ("left", "l", "izq") for word in words) else ("right_candidate" if any(word in ("right", "r", "der") for word in words) else "unresolved")
        explicit_ik = ik_targets.get(bone.name, [])
        low = relative_height <= 0.22
        terminal = len(bone.children) == 0
        lateral = abs(bone.head_local.x) > height * 0.15
        evidence = []
        score = 0.0
        if explicit_ik:
            evidence.append("Explicit IK target")
            score += 0.4
        if low:
            evidence.append("Lower 22 percent of world-Z rest skeleton")
            score += 0.2
        if depth >= 2 or terminal:
            evidence.append("Terminal bone or hierarchy depth at least two")
            score += 0.1
        if symmetry:
            evidence.append("Rest-local X symmetry candidate")
            score += 0.1
        if bone.use_deform and bone.name in deform_bound:
            evidence.append("Deform bone has a bound mesh vertex-group name")
            score += 0.1
        if foot_name or hand_name:
            evidence.append("Anatomical naming hint")
            score += 0.1
        likely = "foot" if low and (foot_name or explicit_ik or depth >= 2) else ("hand" if hand_name or (terminal and lateral and not low) else "unknown")
        record = {"bone": bone.name, "parent": bone.parent.name if bone.parent else None,
            "deform": bone.use_deform, "depth": depth, "child_count": len(bone.children),
            "rest_position_m": [float(value * scale) for value in world],
            "symmetry_candidates": symmetry[:4], "ik_targets": explicit_ik,
            "likely_effector": likely, "confidence": min(score, 0.9), "evidence": evidence,
            "anatomical_side": side_hint if symmetry else "unresolved", "confidence_kind": "uncalibrated evidence score"}
        metadata.append(record)
        if (low or hand_name or (terminal and lateral)) and score >= 0.4:
            effectors.append(record)
        if bone.parent is None:
            descendants = len(bone.children_recursive)
            root_candidates.append({"bone": bone.name, "descendant_count": descendants,
                "confidence": min(0.95, 0.4 + 0.55 * descendants / max(1, bone_count - 1)),
                "evidence": ["No parent", "Descendant coverage " + str(descendants) + "/" + str(bone_count)]})
    def root_order(item):
        return (-item["descendant_count"], item["bone"])
    def effector_order(item):
        return (-item["confidence"], item["bone"])
    root_candidates.sort(key=root_order)
    effectors.sort(key=effector_order)
    if params["bones"] is not None:
        by_name = {item["bone"]: item for item in metadata}
        if any(name not in by_name for name in params["bones"]):
            raise ValueError("One or more selected bones do not exist on target")
        effectors = [dict(by_name[name], selection="explicit_marker", selection_confidence=1.0) for name in params["bones"]]
    else:
        if len(effectors) > 24:
            raise ValueError("More than 24 effector candidates; select explicit bones")
        warnings.append("Automatic effectors assume world-Z upright rest pose; foot/hand and side labels are advisory")
elif params["bones"] or params["root_bone"] or params["pelvis_bone"]:
    raise ValueError("Bone selection requires an armature")
root_bone = params["root_bone"]
if root_bone is not None and (not is_armature or obj.data.bones.get(root_bone) is None):
    raise ValueError("Selected root bone not found")
if root_bone is None and not params["root_object"] and root_candidates:
    root_bone = root_candidates[0]["bone"]
pelvis = params["pelvis_bone"]
if pelvis is not None and (not is_armature or obj.data.bones.get(pelvis) is None):
    raise ValueError("Selected pelvis bone not found")
if pelvis is None:
    pelvis = root_bone
    warnings.append("Balance projection defaults to a root proxy, not anatomical pelvis or center of mass")
names = sorted({item["bone"] for item in effectors} | ({root_bone} if root_bone else set()) | ({pelvis} if pelvis else set()))
if len(names) > 34 or len(names) * len(frames) > 64000:
    raise ValueError("Analysis exceeds 64000 bone-frame evaluations")
tracks = {name: {"positions_m": [], "quaternions": [], "euler_xyz": []} for name in names}
object_track = {"positions_m": [], "quaternions": [], "euler_xyz": []}
old_frame = scene.frame_current
old_subframe = scene.frame_subframe
try:
    for frame in frames:
        scene.frame_set(frame, subframe=0.0)
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        matrix = evaluated.matrix_world
        object_track["positions_m"].append([float(value * scale) for value in matrix.translation])
        object_track["quaternions"].append(list(matrix.to_quaternion()))
        object_track["euler_xyz"].append(list(matrix.to_euler("XYZ")))
        for name in names:
            matrix = evaluated.matrix_world @ evaluated.pose.bones[name].matrix
            tracks[name]["positions_m"].append([float(value * scale) for value in matrix.translation])
            tracks[name]["quaternions"].append(list(matrix.to_quaternion()))
            tracks[name]["euler_xyz"].append(list(matrix.to_euler("XYZ")))
finally:
    scene.frame_set(old_frame, subframe=old_subframe)
key_intervals = []
inspected_keys = 0
if obj.animation_data and obj.animation_data.action:
    action = obj.animation_data.action
    slot = getattr(obj.animation_data, "action_slot", None)
    handle = slot.handle if slot else (-1 if getattr(action, "is_action_layered", False) else None)
    for slot_handle, layer_index, strip_index, curve in action_curves(action, handle):
        inspected_keys += len(curve.keyframe_points)
        if inspected_keys > 500000:
            raise ValueError("More than 500000 keys; narrow target Action before analysis")
        times = sorted({float(key.co.x) for key in curve.keyframe_points if start <= key.co.x <= end})
        if len(times) >= 4:
            intervals = [times[index] - times[index - 1] for index in range(1, len(times))]
            if max(intervals) - min(intervals) <= 1e-6 and len(key_intervals) < 100:
                key_intervals.append({"data_path": curve.data_path, "array_index": curve.array_index,
                    "slot_handle": slot_handle, "layer_index": layer_index, "strip_index": strip_index,
                    "key_count": len(times), "interval_frames": intervals[0]})
result = {"object": obj.name, "is_armature": is_armature, "frames": frames, "fps": fps,
    "action": obj.animation_data.action.name if obj.animation_data and obj.animation_data.action else None,
    "source_file": bpy.data.filepath, "scale_m": scale, "frame_restored": scene.frame_current == old_frame and scene.frame_subframe == old_subframe,
    "bone_count": bone_count, "effectors": effectors, "root_candidates": root_candidates[:8],
    "root_bone": root_bone, "pelvis_bone": pelvis, "tracks": tracks, "object_track": object_track,
    "repeated_key_intervals": key_intervals, "warnings": warnings}
'''
