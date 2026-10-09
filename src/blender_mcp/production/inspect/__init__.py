"""Read-only scene and armature inspection through the upstream bridge.

The payloads use RNA tested against the installed Blender, retain stable names,
and bound public response sizes. ``snapshot`` is an internal, complete summary
for checkpoint comparisons; it deliberately does not hash mesh/animation data.
"""

from __future__ import annotations


def _page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def _name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("armature must be a non-empty object name")


SCENE_SCRIPT = r'''
import bpy
import math

def item_name(item):
    return item.name

scene = bpy.context.scene
scope = sorted(scene.objects, key=item_name)
if params.get("object_name"):
    found = scene.objects.get(params["object_name"])
    if found is None:
        raise ValueError("Object not found in active scene: " + params["object_name"])
    scope = [found]

def summarize_object(item):
    ad = item.animation_data
    full_detail = params.get("full") or params.get("detail", "summary") == "full"
    record = {
        "name": item.name, "type": item.type,
        "data": item.data.name if item.data else None,
        "parent": item.parent.name if item.parent else None,
        "visible_viewport": not item.hide_viewport,
        "visible_render": not item.hide_render,
        "constraint_count": len(item.constraints),
        "action": ad.action.name if ad and ad.action else None,
        "nla_track_count": len(ad.nla_tracks) if ad else 0,
        "driver_count": len(ad.drivers) if ad else 0,
    }
    if full_detail:
        record.update({
            "parent_bone": item.parent_bone or None,
            "location": list(item.location), "rotation_mode": item.rotation_mode,
            "rotation_quaternion": list(item.rotation_quaternion),
            "rotation_euler": list(item.rotation_euler), "scale": list(item.scale),
            "matrix_world": [list(row) for row in item.matrix_world],
            "modifiers": [{"name": mod.name, "type": mod.type,
                           "viewport": mod.show_viewport, "render": mod.show_render}
                          for mod in item.modifiers],
            "material_slots": [slot.material.name if slot.material else None
                               for slot in item.material_slots],
        })
    else:
        record.update({"modifier_count": len(item.modifiers),
                       "material_slot_count": len(item.material_slots)})
    if item.type == "MESH":
        record["geometry"] = {"vertices": len(item.data.vertices),
                              "edges": len(item.data.edges),
                              "polygons": len(item.data.polygons)}
        record["shape_keys"] = len(item.data.shape_keys.key_blocks) if item.data.shape_keys else 0
        record["vertex_groups"] = len(item.vertex_groups)
    elif item.type == "ARMATURE":
        record["bones"] = len(item.data.bones)
        record["deform_bones"] = sum(1 for bone in item.data.bones if bone.use_deform)
    return record

object_counts = {}
for item in scope:
    object_counts[item.type] = object_counts.get(item.type, 0) + 1
offset = params.get("offset", 0)
limit = params.get("limit", len(scope))
selected = scope[offset:offset + limit]
warnings = []
if not bpy.data.filepath:
    warnings.append("The active blend has not been saved; checkpoints must establish a working copy.")
if scene.unit_settings.system == "NONE":
    warnings.append("Scene unit system is NONE; physical measurements require an explicit unit assumption.")
non_unit_scale = [item.name for item in scope if any(abs(value - 1.0) > 0.000001 for value in item.scale)]
negative_scale = [item.name for item in scope if item.matrix_world.determinant() < 0.0]
action_records = [{"name": action.name, "frame_range": list(action.frame_range),
                   "users": action.users, "fake_user": action.use_fake_user,
                   "slots": len(action.slots) if hasattr(action, "slots") else None}
                  for action in sorted(bpy.data.actions, key=item_name)]
result = {
    "schema_version": 1, "blender_version": bpy.app.version_string,
    "source_file": bpy.data.filepath or None,
    "scene": {"name": scene.name, "frame": scene.frame_current,
              "frame_start": scene.frame_start, "frame_end": scene.frame_end,
              "fps": scene.render.fps / scene.render.fps_base,
              "unit_system": scene.unit_settings.system,
              "scale_length": scene.unit_settings.scale_length,
              "length_unit": scene.unit_settings.length_unit,
              "active_object": bpy.context.view_layer.objects.active.name
                  if bpy.context.view_layer.objects.active else None},
    "totals": {"objects": len(scope), "objects_by_type": object_counts,
               "actions": len(action_records), "materials": len(bpy.data.materials)},
    "objects": {item.name: summarize_object(item) for item in selected},
    "actions": action_records if params.get("full") else action_records[offset:offset + limit],
    "pagination": {"offset": offset, "limit": limit, "total": len(scope),
                   "next_offset": offset + limit if offset + limit < len(scope) else None},
    "action_pagination": {"offset": offset, "limit": limit, "total": len(action_records),
                          "next_offset": offset + limit if offset + limit < len(action_records) else None},
    "issues": {"non_unit_scale": non_unit_scale if params.get("full") else non_unit_scale[:limit],
               "non_unit_scale_count": len(non_unit_scale),
               "negative_world_scale": negative_scale if params.get("full") else negative_scale[:limit],
               "negative_world_scale_count": len(negative_scale)},
    "warnings": warnings,
    "snapshot_scope": "Object metadata, transforms and data counts only; not geometry, weights, bone poses, curve values or material content.",
}
if not params.get("full"):
    result["detail"] = params.get("detail", "summary")
    if result["detail"] == "summary":
        result["snapshot_scope"] = "Object metadata and data counts; transforms and modifier/material details omitted. Request detail='full' to inspect them."
'''


ARMATURE_SCRIPT = r'''
import bpy

def item_name(item):
    return item.name

arm = bpy.context.scene.objects.get(params["armature"])
if arm is None or arm.type != "ARMATURE":
    raise ValueError("Armature object not found in active scene: " + params["armature"])
offset = params["offset"]
limit = params["limit"]

def constraint_record(con, owner):
    target = getattr(con, "target", None)
    record = {"owner": owner, "name": con.name, "type": con.type,
              "influence": con.influence, "muted": con.mute, "valid": con.is_valid,
              "target": target.name if target else None,
              "subtarget": getattr(con, "subtarget", "") or None}
    if con.type == "IK":
        record.update({"chain_count": con.chain_count, "use_tail": con.use_tail,
                       "pole_target": con.pole_target.name if con.pole_target else None,
                       "pole_subtarget": con.pole_subtarget or None,
                       "pole_angle_radians": con.pole_angle})
    return record

bones = sorted(arm.data.bones, key=item_name)
bone_records = []
for bone in bones[offset:offset + limit]:
    pose = arm.pose.bones[bone.name]
    bone_records.append({"name": bone.name,
        "parent": bone.parent.name if bone.parent else None,
        "children": [child.name for child in bone.children],
        "deform": bone.use_deform, "connected": bone.use_connect,
        "head_local": list(bone.head_local), "tail_local": list(bone.tail_local),
        "length": bone.length, "rotation_mode": pose.rotation_mode,
        "constraint_count": len(pose.constraints),
        "collections": [collection.name for collection in bone.collections]
            if hasattr(bone, "collections") else [],
    })
constraints = [constraint_record(con, pose.name) for pose in arm.pose.bones for con in pose.constraints]
constraints.extend(constraint_record(con, None) for con in arm.constraints)
drivers = []
for datablock, label in [(arm, "object"), (arm.data, "armature_data")]:
    ad = datablock.animation_data
    if ad:
        for curve in ad.drivers:
            drivers.append({"owner": label, "data_path": curve.data_path,
                            "array_index": curve.array_index,
                            "valid": curve.driver.is_valid, "muted": curve.mute,
                            "type": curve.driver.type,
                            "variable_count": len(curve.driver.variables)})
bound_meshes = []
for item in bpy.context.scene.objects:
    if item.type == "MESH" and any(mod.type == "ARMATURE" and mod.object == arm for mod in item.modifiers):
        bound_meshes.append(item.name)
result = {
    "armature": arm.name, "data": arm.data.name,
    "totals": {"bones": len(bones), "deform_bones": sum(1 for bone in bones if bone.use_deform),
               "non_deform_bones": sum(1 for bone in bones if not bone.use_deform),
               "roots": sum(1 for bone in bones if bone.parent is None),
               "constraints": len(constraints), "drivers": len(drivers),
               "bound_meshes": len(bound_meshes)},
    "bones": bone_records,
    "constraints": constraints[offset:offset + limit],
    "drivers": drivers[offset:offset + limit],
    "bound_meshes": sorted(bound_meshes)[offset:offset + limit],
    "pagination": {"offset": offset, "limit": limit,
                   "totals": {"bones": len(bones), "constraints": len(constraints),
                              "drivers": len(drivers), "bound_meshes": len(bound_meshes)}},
    "warnings": ["Non-deform bones are not necessarily animation controls; role inference needs rig analysis."],
}
'''


def scene_audit(bridge, object_name: str | None = None, limit: int = 100, offset: int = 0,
                detail: str = "summary") -> dict:
    """Summarize the scene; full detail includes transforms and modifier/material metadata."""
    _page(limit, offset)
    if not isinstance(detail, str) or detail not in {"summary", "full"}:
        raise ValueError("detail must be 'summary' or 'full'")
    if object_name is not None and (not isinstance(object_name, str) or not object_name.strip()):
        raise ValueError("object_name must be null or a non-empty exact object name")
    return bridge.run(SCENE_SCRIPT, {"object_name": object_name, "limit": limit,
                                     "offset": offset, "detail": detail})


def snapshot(bridge) -> dict:
    """Internal full metadata snapshot for checkpoint storage and scene diffs."""
    return bridge.run(SCENE_SCRIPT, {"full": True})


def inspect_armature(bridge, armature: str, limit: int = 100, offset: int = 0) -> dict:
    """Inspect rest bones and report constraints/drivers without evaluating poses."""
    _name(armature)
    _page(limit, offset)
    return bridge.run(ARMATURE_SCRIPT, {"armature": armature, "limit": limit, "offset": offset})
