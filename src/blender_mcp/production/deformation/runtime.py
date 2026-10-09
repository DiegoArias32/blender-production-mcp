"""Blender payloads for temporary poses and evaluated geometric sampling."""
import inspect
from . import geometry
from ..common.state_guard import STATE_RUNTIME

GEOMETRY_RUNTIME = "import math\n" + "\n\n".join(inspect.getsource(getattr(geometry, name)) for name in (
    "dq_sub", "dq_dot", "dq_cross", "dq_length", "dq_area", "dq_volume", "dq_closed", "dq_metrics"))

RUNTIME = GEOMETRY_RUNTIME + "\nimport bpy\n" + STATE_RUNTIME + r'''
import bpy
from array import array
from mathutils import Quaternion, Vector
from mathutils.bvhtree import BVHTree

def dq_channels(item):
    return {"location": list(item.location), "scale": list(item.scale), "rotation_euler": list(item.rotation_euler),
            "rotation_quaternion": list(item.rotation_quaternion), "rotation_axis_angle": list(item.rotation_axis_angle)}

def dq_restore_channels(item, values):
    item.location = values["location"]
    item.scale = values["scale"]
    item.rotation_euler = values["rotation_euler"]
    item.rotation_quaternion = values["rotation_quaternion"]
    item.rotation_axis_angle = values["rotation_axis_angle"]

def dq_state(target):
    return {"object": dq_channels(target), "rotation_mode": target.rotation_mode,
            "deltas": {"delta_location": list(target.delta_location), "delta_scale": list(target.delta_scale),
                       "delta_rotation_euler": list(target.delta_rotation_euler), "delta_rotation_quaternion": list(target.delta_rotation_quaternion)},
            "bones": {bone.name: {"channels": dq_channels(bone), "mode": bone.rotation_mode}
                      for bone in target.pose.bones} if target.type == "ARMATURE" else {}}

def dq_restore(target, state):
    target.rotation_mode = state["rotation_mode"]
    dq_restore_channels(target, state["object"])
    target.delta_location = state["deltas"]["delta_location"]
    target.delta_scale = state["deltas"]["delta_scale"]
    target.delta_rotation_euler = state["deltas"]["delta_rotation_euler"]
    target.delta_rotation_quaternion = state["deltas"]["delta_rotation_quaternion"]
    for name, values in state["bones"].items():
        bone = target.pose.bones[name]
        bone.rotation_mode = values["mode"]
        dq_restore_channels(bone, values["channels"])
    bpy.context.view_layer.update()

def dq_curves(action, slot):
    if action is None:
        return []
    if getattr(action, "is_action_layered", False):
        return [curve for layer in action.layers for strip in layer.strips for bag in strip.channelbags
                if slot and bag.slot_handle == slot.handle for curve in bag.fcurves]
    return list(action.fcurves)

def dq_action(target, name):
    action = bpy.data.actions.get(name) if name else (target.animation_data.action if target.animation_data else None)
    if name and action is None:
        raise ValueError("Action does not exist: " + name)
    if action and not target.animation_data:
        raise ValueError("Temporary Action sampling requires existing animation_data")
    slot = None
    if action and getattr(action, "is_action_layered", False):
        slots = [s for s in action.slots if s.target_id_type == "OBJECT"]
        if len(slots) != 1 or len(action.layers) != 1 or len(action.layers[0].strips) != 1:
            raise ValueError("Deformation QA requires one compatible Action slot/layer/strip")
        slot = slots[0]
    paths = set(dq_channels(target)) | {"delta_location", "delta_scale", "delta_rotation_euler", "delta_rotation_quaternion"}
    if target.type == "ARMATURE":
        for bone in target.pose.bones:
            paths.update(bone.path_from_id() + "." + key for key in dq_channels(bone))
    unsupported = sorted(set(c.data_path for c in dq_curves(action, slot)) - paths)
    if unsupported:
        raise ValueError("QA cannot restore animated non-transform paths: " + str(unsupported[:5]))
    return action, slot

def dq_assign(target, action, slot):
    if target.animation_data:
        target.animation_data.action = action
        if slot:
            target.animation_data.action_slot = slot

def dq_target(name, options):
    scene = bpy.context.scene
    target = scene.objects.get(name)
    if target is None or target.type not in {"ARMATURE", "MESH"}:
        raise ValueError("Deformation QA needs an existing armature or mesh")
    if bpy.context.mode != "OBJECT":
        raise ValueError("MODE_NOT_SUPPORTED: Deformation QA requires Object mode")
    prod_require_pose_position(target, "deformation target")
    if target.library or target.override_library:
        raise ValueError("Linked/override targets are unsupported for temporary pose sampling")
    ad = target.animation_data
    if ad and (ad.use_tweak_mode or (ad.use_nla and any(not t.mute and len(t.strips) for t in ad.nla_tracks))):
        raise ValueError("Active NLA/tweak mode is unsupported")
    if ad and (ad.action_blend_type != "REPLACE" or abs(ad.action_influence-1) > 1e-8 or ad.drivers):
        raise ValueError("Driven/blended animation is unsupported for temporary sampling")
    if target.data.animation_data and target.data.animation_data.drivers:
        raise ValueError("Driven target datablock is unsupported for temporary sampling")
    names = options["meshes"]
    meshes = [scene.objects.get(name) for name in names] if names else (
        [target] if target.type == "MESH" else [obj for obj in scene.objects if obj.type == "MESH"
         and any(mod.type == "ARMATURE" and mod.object == target and mod.show_viewport for mod in obj.modifiers)])
    if not meshes or len(meshes) > 16 or any(obj is None or obj.type != "MESH" for obj in meshes):
        raise ValueError("Select 1..16 existing meshes bound to the target")
    if target.type == "ARMATURE" and any(not any(mod.type == "ARMATURE" and mod.object == target and mod.show_viewport
                                                  for mod in obj.modifiers) for obj in meshes):
        raise ValueError("Explicit mesh is not deformed by this armature")
    if any(obj.library or obj.override_library for obj in meshes):
        raise ValueError("Linked/override meshes are unsupported")
    if any(len(obj.data.vertices) > options["max_vertices"] for obj in meshes):
        raise ValueError("Mesh vertex budget exceeded before evaluation")
    for region, groups in options["regions"].items():
        if not any(any(mesh.vertex_groups.get(group) for group in groups) for mesh in meshes):
            raise ValueError("Region has no matching vertex groups: " + region)
    return target, meshes

def dq_geometry(mesh, options):
    evaluated = mesh.evaluated_get(bpy.context.evaluated_depsgraph_get())
    data = evaluated.to_mesh()
    try:
        if len(data.vertices) > options["max_vertices"] or len(data.polygons) > 250000:
            raise ValueError("Evaluated mesh exceeds bounded geometry budget")
        data.calc_loop_triangles()
        if len(data.loop_triangles) > 500000:
            raise ValueError("Evaluated triangle budget exceeded")
        buffer = array("f", [0.0]) * (len(data.vertices)*3)
        data.vertices.foreach_get("co", buffer)
        scale = bpy.context.scene.unit_settings.scale_length
        points = [list(evaluated.matrix_world @ Vector(buffer[i:i+3]) * scale) for i in range(0, len(buffer), 3)]
        edges = [list(edge.vertices) for edge in data.edges]
        polygons = [list(polygon.vertices) for polygon in data.polygons]
        triangles = [list(triangle.vertices) for triangle in data.loop_triangles]
        regions = {}
        if options["regions"] and len(mesh.data.vertices) != len(data.vertices):
            raise ValueError("Regions require topology-preserving modifiers")
        for name, groups in options["regions"].items():
            group_ids = {group.index for group in mesh.vertex_groups if group.name in groups}
            chosen = [v.index for v in mesh.data.vertices if any(g.group in group_ids and g.weight >= options["region_weight"] for g in v.groups)]
            if chosen:
                regions[name] = chosen
        return {"points": points, "edges": edges, "polygons": polygons, "triangles": triangles, "regions": regions}
    finally:
        evaluated.to_mesh_clear()

def dq_intersections(points, triangles, options):
    if not options["self_intersection"]:
        return {"status": "not_requested", "nonadjacent_bvh_overlap_pairs": None}
    if len(triangles) > 2000:
        return {"status": "budget_exceeded", "nonadjacent_bvh_overlap_pairs": None,
                "warning": "Self-overlap indicator limited to 2000 triangles"}
    tree = BVHTree.FromPolygons(points, triangles, all_triangles=True, epsilon=0.0)
    pairs = tree.overlap(tree) if tree else []
    count = 0
    for first, second in pairs:
        if first < second and not set(triangles[first]) & set(triangles[second]):
            count += 1
    return {"status": "measured", "nonadjacent_bvh_overlap_pairs": count,
            "scope": "BVH triangle overlap indicator; coplanar/touching cases may be ambiguous, not a watertight collision proof"}

def dq_measure(mesh, reference, current, options, frame, label):
    if reference["edges"] != current["edges"] or reference["polygons"] != current["polygons"]:
        raise ValueError("Evaluated topology changed; vertex correspondence is unsafe")
    rows = []
    regions = {"mesh": None, **reference["regions"]}
    for region, indices in regions.items():
        measured = dq_metrics(reference["points"], current["points"], reference["edges"], reference["triangles"], options, indices)
        rows.append({"mesh": mesh.name, "region": region, "frame": frame, "label": label, **measured})
    overlap = dq_intersections(current["points"], reference["triangles"], options)
    rows[0]["self_intersection"] = overlap
    if overlap.get("nonadjacent_bvh_overlap_pairs", 0):
        rows[0]["issues"].append({"issue": "nonadjacent_triangle_overlap_indicator", "severity": "warning", "confidence": 0.6,
                                  "count": overlap["nonadjacent_bvh_overlap_pairs"]})
    return rows

def dq_frame(frame):
    base = math.floor(frame)
    bpy.context.scene.frame_set(base, subframe=frame-base)

def dq_sample_frames(target, options):
    scene = bpy.context.scene
    start = options["frame_start"] if options["frame_start"] is not None else scene.frame_start
    end = options["frame_end"] if options["frame_end"] is not None else scene.frame_end
    if end < start:
        raise ValueError("frame_end precedes frame_start")
    if any(f < start or f > end for f in options["contact_frames"] + options["frames"]):
        raise ValueError("Explicit/contact frames must be inside the requested range")
    if options["frames"]:
        return [{"frame": f, "reasons": ["explicit"]} for f in sorted(set(options["frames"] + options["contact_frames"]))]
    count = min(options["candidate_frames"], max(1, int(end-start)+1))
    frames = sorted(set(round(start+(end-start)*i/max(1, count-1), 4) for i in range(count)))
    chosen = {start: ["range_start"], end: ["range_end"]}
    for frame in options["contact_frames"]:
        chosen.setdefault(frame, []).append("provided_contact_event")
    if len(chosen) > options["max_samples"]:
        raise ValueError("Contact events exceed sample budget; narrow the range")
    previous = None
    initial_rotations = None
    scored = []
    for frame in frames:
        dq_frame(frame)
        evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
        rotations = {}
        flex = 0.0
        extent = 0.0
        if target.type == "ARMATURE":
            if len(target.pose.bones)*len(frames) > 100000:
                raise ValueError("Skeletal candidate sampling exceeds 100000 bone/frames")
            for bone in evaluated.pose.bones:
                rotations[bone.name] = list(bone.matrix.to_quaternion().normalized())
                extent = max(extent, (bone.tail-bone.bone.tail_local).length)
                if bone.parent and (bone.head-bone.parent.head).length > 1e-8 and (bone.tail-bone.head).length > 1e-8:
                    flex = max(flex, (bone.head-bone.parent.head).angle(bone.tail-bone.head))
        else:
            rotations[""] = list(evaluated.matrix_world.to_quaternion().normalized())
            extent = evaluated.matrix_world.translation.length
        angular = max((2*math.acos(min(1.0, abs(sum(q[i]*previous[1][name][i] for i in range(4)))))
                       / max(1e-6, frame-previous[0]) for name, q in rotations.items()), default=0.0) if previous else 0.0
        if initial_rotations is None:
            initial_rotations = rotations
        angular_extent = max((2*math.acos(min(1.0, abs(sum(q[i]*initial_rotations[name][i] for i in range(4)))))
                              for name, q in rotations.items()), default=0.0)
        scored.append({"frame": frame, "maximum_flexion": flex, "high_angular_velocity": angular,
                       "pose_extent": extent, "maximum_rotation": angular_extent})
        previous = (frame, rotations)
    rankings = [[entry[2] for entry in sorted([(row[key], row["frame"], row) for row in scored], reverse=True)]
                for key in ["maximum_rotation", "maximum_flexion", "high_angular_velocity", "pose_extent"]]
    for depth in range(len(scored)):
        for key, ranking in zip(["maximum_rotation", "maximum_flexion", "high_angular_velocity", "pose_extent"], rankings):
            row = ranking[depth]
            if row["frame"] in chosen:
                if key not in chosen[row["frame"]]:
                    chosen[row["frame"]].append(key)
            elif len(chosen) < options["max_samples"]:
                chosen[row["frame"]] = [key]
        if len(chosen) >= options["max_samples"]:
            break
    priority = list(chosen)
    return [{"frame": frame, "reasons": reasons, "priority": priority.index(frame)} for frame, reasons in sorted(chosen.items())]

def dq_preflight(params):
    target, meshes = dq_target(params["object_name"], params["options"])
    actions = [dq_action(target, name) for name in params.get("actions", [None])]
    poses = params.get("poses", [])
    if poses and target.type != "ARMATURE":
        raise ValueError("Stress pose needs an armature")
    for pose in poses:
        bone = target.pose.bones.get(pose["bone"])
        if bone is None:
            raise ValueError("Explicit mapped control does not exist: " + pose["bone"])
        if bone.constraints or any(bone.lock_rotation) or bone.lock_rotation_w:
            raise ValueError("Stress pose requires an unconstrained unlocked explicit control: " + bone.name)
    return target, meshes, actions

def dq_run(params):
    options = params["options"]
    target, meshes, actions = dq_preflight(params)
    scene = bpy.context.scene
    old_frame, old_subframe = scene.frame_current, scene.frame_subframe
    original = dq_state(target)
    ad = target.animation_data
    original_action = ad.action if ad else None
    original_slot = ad.action_slot if ad else None
    last_slot = ad.last_slot_identifier if ad and hasattr(ad, "last_slot_identifier") else None
    rows = []
    result = {"status": "ok", "object": target.name, "meshes": [mesh.name for mesh in meshes],
              "actions": [action.name if action else None for action, slot in actions],
              "warnings": ["Geometric indicators are advisory; anatomy/artistic quality is not certified.",
                           "Reference frame must represent acceptable deformation; no automatic rest-pose alteration.",
                           "Sampling is finite; unsampled frames and external handler effects are not certified."],
              "errors": [], "state_restored": False, "samples": [], "rows": []}
    try:
        reference_frame = old_frame+old_subframe if params["kind"] == "pose" else (
            options["reference_frame"] if options["reference_frame"] is not None else scene.frame_start)
        if params["kind"] != "pose":
            dq_assign(target, actions[0][0], actions[0][1])
            dq_frame(reference_frame)
        reference = {mesh.name: dq_geometry(mesh, options) for mesh in meshes}
        vertex_count = sum(len(item["points"]) for item in reference.values())
        result["reference"] = {"frame": reference_frame, "action": actions[0][0].name if actions[0][0] else None,
                               "vertices": vertex_count}
        if params["kind"] == "pose":
            # Stress tests start from the current evaluated pose, without touching Action keys.
            poses = params["poses"]
            if vertex_count*len(poses) > options["max_vertex_samples"]:
                raise ValueError("Stress deformation sampling exceeds vertex/sample budget")
            for pose in poses:
                dq_restore(target, original)
                bone = target.pose.bones[pose["bone"]]
                initial = bone.matrix_basis.to_quaternion()
                bone.rotation_mode = "QUATERNION"
                bone.rotation_quaternion = initial @ Quaternion(Vector(pose["axis"]), math.radians(pose["angle_degrees"]))
                bpy.context.view_layer.update()
                result["samples"].append({"frame": old_frame+old_subframe, "reasons": ["explicit_stress_pose"], "pose": pose})
                for mesh in meshes:
                    current = dq_geometry(mesh, options)
                    rows.extend(dq_measure(mesh, reference[mesh.name], current, options, old_frame+old_subframe, pose["region"]))
        else:
            if params["kind"] == "analyze":
                samples = [{"frame": options["frame"] if options["frame"] is not None else old_frame+old_subframe,
                            "reasons": ["requested_frame"]}]
            else:
                samples = dq_sample_frames(target, options)
                if len(actions) == 2 and not options["frames"]:
                    first_samples = samples
                    dq_restore(target, original)
                    dq_assign(target, actions[1][0], actions[1][1])
                    second_samples = dq_sample_frames(target, options)
                    ranked = sorted([(item["priority"], index, item["frame"], item) for index, group in enumerate([first_samples, second_samples]) for item in group])
                    combined = {}
                    for priority, index, frame, item in ranked:
                        reasons = [("before:" if index == 0 else "after:") + reason for reason in item["reasons"]]
                        if frame in combined:
                            combined[frame].extend(reasons)
                        elif len(combined) < options["max_samples"]:
                            combined[frame] = reasons
                    samples = [{"frame": frame, "reasons": reasons} for frame, reasons in sorted(combined.items())]
            if len(samples) > options["max_samples"] or vertex_count*len(samples)*len(actions) > options["max_vertex_samples"]:
                raise ValueError("Deformation sampling exceeds bounded vertex/sample budget")
            result["samples"] = samples
            for index, (action, slot) in enumerate(actions):
                dq_restore(target, original)
                dq_assign(target, action, slot)
                label = ["before", "after"][index] if len(actions) == 2 else "sample"
                for item in samples:
                    dq_frame(item["frame"])
                    for mesh in meshes:
                        rows.extend(dq_measure(mesh, reference[mesh.name], dq_geometry(mesh, options), options, item["frame"], label))
    except Exception as exc:
        result["status"] = "error"
        result["errors"].append({"type": str(type(exc)), "message": str(exc)})
    finally:
        dq_assign(target, original_action, original_slot)
        if last_slot is not None:
            ad.last_slot_identifier = last_slot
        scene.frame_set(old_frame, subframe=old_subframe)
        dq_restore(target, original)
    result["state_restored"] = (dq_state(target) == original and scene.frame_current == old_frame
                                 and scene.frame_subframe == old_subframe
                                 and (not ad or ad.action == original_action and ad.action_slot == original_slot))
    if not result["state_restored"]:
        result["status"] = "error"
        result["errors"].append({"type": "StateRestorationError", "message": "Pose/frame/Action restoration differs"})
    issues = [{"frame": row["frame"], "region": row["region"], "mesh": row["mesh"], "label": row["label"],
               "metrics": row["metrics"], **issue} for row in rows for issue in row["issues"]]
    result["metrics"] = {"meshes": len(meshes), "sampled_frames": len(result["samples"]), "evaluations": len(rows),
                         "issue_count": len(issues), "issues_by_label": {label: sum(i["label"] == label for i in issues)
                             for label in sorted(set(row["label"] for row in rows))},
                         "max_displacement_m": max((row["metrics"]["max_displacement_m"] for row in rows), default=0.0)}
    for label in sorted(set(row["label"] for row in rows)):
        selected_rows = [row for row in rows if row["label"] == label and row["region"] == "mesh"]
        summary = {"frames": sorted(set(row["frame"] for row in selected_rows)),
                   "issue_count": sum(len(row["issues"]) for row in selected_rows),
                   "collapsed_edges": sum(row["metrics"]["collapsed_edges"] for row in selected_rows),
                   "collapsed_triangles": sum(row["metrics"]["collapsed_triangles"] for row in selected_rows),
                   "stretched_edges": sum(row["metrics"]["stretched_edges"] for row in selected_rows),
                   "max_displacement_m": max((row["metrics"]["max_displacement_m"] for row in selected_rows), default=0.0),
                   "min_volume_ratio": min((row["metrics"]["volume_ratio"] for row in selected_rows
                                             if row["metrics"]["volume_ratio"] is not None), default=None)}
        if len(actions) == 2 and label in {"before", "after"}:
            result[label] = summary
        else:
            result.setdefault("summaries", {})[label] = summary
    if "before" in result and "after" in result:
        result["metrics"]["delta"] = {key: result["after"][key]-result["before"][key]
            for key in ["issue_count", "collapsed_edges", "collapsed_triangles", "stretched_edges", "max_displacement_m"]}
    result["thresholds"] = {key: options[key] for key in ["collapse_ratio", "stretch_ratio", "area_ratio", "volume_ratio", "epsilon_m"]}
    result["rows_total"] = len(rows)
    result["issues"] = issues[options["offset"]:options["offset"]+options["limit"]]
    if options["detail"] == "full":
        result["rows"] = rows[options["offset"]:options["offset"]+options["limit"]]
    result["pagination"] = {"offset": options["offset"], "limit": options["limit"], "issues_total": len(issues)}
    return result
'''

PREFLIGHT = RUNTIME + '\ntarget, meshes, actions = dq_preflight(params)\nresult = {"object": target.name, "meshes": [mesh.name for mesh in meshes], "supported": True, "poses": params.get("poses", [])}\n'
EXECUTE = RUNTIME + '\nresult = dq_run(params)\n'
