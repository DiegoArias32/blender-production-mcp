"""Conservative structure-based rig analysis; never edits a rig."""

from __future__ import annotations

from ..inspect import _name, _page


RIG_SCRIPT = r'''
import bpy
import math

arm = bpy.context.scene.objects.get(params["armature"])
if arm is None or arm.type != "ARMATURE":
    raise ValueError("Armature object not found in active scene: " + params["armature"])
limit = params["limit"]
offset = params["offset"]
ik_chains = []
constraints = []
issues = []
ik_members = set()
target_bones = set()
pole_targets = []

for pose in arm.pose.bones:
    for con in pose.constraints:
        target = getattr(con, "target", None)
        subtarget = getattr(con, "subtarget", "")
        record = {"owner": pose.name, "name": con.name, "type": con.type,
                  "influence": con.influence, "muted": con.mute, "valid": con.is_valid,
                  "target": target.name if target else None, "subtarget": subtarget or None}
        constraints.append(record)
        if not con.is_valid:
            issues.append({"code": "invalid_constraint", "bone": pose.name, "constraint": con.name})
        if target and subtarget and target.type == "ARMATURE" and target.data.bones.get(subtarget) is None:
            issues.append({"code": "missing_constraint_subtarget", "bone": pose.name, "constraint": con.name,
                           "target": target.name, "subtarget": subtarget})
        if target == arm and subtarget:
            target_bones.add(subtarget)
        if con.type == "IK":
            chain = []
            cursor = pose
            while cursor is not None and (con.chain_count == 0 or len(chain) < con.chain_count):
                chain.append(cursor.name)
                cursor = cursor.parent
            # IK with zero influence is retained as a detected mechanism, not an active solver.
            active = not con.mute and con.influence > 0.0 and con.is_valid
            if active:
                ik_members.update(chain)
            pole = {"object": con.pole_target.name if con.pole_target else None,
                    "bone": con.pole_subtarget or None}
            if con.pole_target:
                pole_targets.append({"constraint_owner": pose.name, "constraint": con.name, **pole})
                if con.pole_target == arm and con.pole_subtarget:
                    target_bones.add(con.pole_subtarget)
                if con.pole_subtarget and con.pole_target.type == "ARMATURE" and con.pole_target.data.bones.get(con.pole_subtarget) is None:
                    issues.append({"code": "missing_pole_subtarget", "bone": pose.name, "constraint": con.name})
            ik_chains.append({"owner": pose.name, "constraint": con.name,
                              "bones_tip_to_root": chain, "chain_count_setting": con.chain_count,
                              "target": target.name if target else None,
                              "subtarget": subtarget or None, "pole": pole,
                              "use_tail": con.use_tail, "active": active,
                              "confidence": 1.0, "evidence": ["Explicit Blender IK constraint"]})

# Maximal paths in the hierarchy that are not currently solved by explicit IK.
# This does not prove a custom control's FK behavior (drivers can switch solvers).
fk_candidates = []
eligible = {bone.name for bone in arm.data.bones if bone.name not in ik_members}
for bone in arm.data.bones:
    if bone.name not in eligible:
        continue
    parent = bone.parent
    if parent and parent.name in eligible and len([child for child in parent.children if child.name in eligible]) == 1:
        continue
    chain = [bone.name]
    cursor = bone
    while True:
        children = [child for child in cursor.children if child.name in eligible]
        if len(children) != 1:
            break
        cursor = children[0]
        chain.append(cursor.name)
    if len(chain) >= 2:
        other_constraints = any(len(arm.pose.bones[name].constraints) > 0 for name in chain)
        fk_candidates.append({"bones_root_to_tip": chain,
                              "confidence": 0.35 if other_constraints else 0.65,
                              "evidence": ["Contiguous parent hierarchy", "Outside active explicit IK chains"],
                              "qualification": "Candidate only; custom drivers, constraints and IK/FK switches require manual validation."})

bones = list(arm.data.bones)
extent = max([bone.head_local.length for bone in bones] + [bone.tail_local.length for bone in bones] + [0.000001])
tolerance = max(extent * 0.0001, 0.000001)
# Spatial X-mirror pairing is reported as a signal, never anatomical left/right.
symmetry_pairs = []
spatial_index = {}
for bone in bones:
    key = tuple(round(value / tolerance) for value in bone.head_local)
    spatial_index.setdefault(key, []).append(bone)
for bone in bones:
    if bone.head_local.x <= tolerance:
        continue
    mirrored_head = bone.head_local.copy()
    mirrored_tail = bone.tail_local.copy()
    mirrored_head.x = -mirrored_head.x
    mirrored_tail.x = -mirrored_tail.x
    center = tuple(round(value / tolerance) for value in mirrored_head)
    matches = []
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            for dz in [-1, 0, 1]:
                for other in spatial_index.get((center[0] + dx, center[1] + dy, center[2] + dz), []):
                    if (other.head_local - mirrored_head).length <= tolerance and (other.tail_local - mirrored_tail).length <= tolerance:
                        matches.append(other)
    if len(matches) == 1:
        other = matches[0]
        same_deform = bone.use_deform == other.use_deform
        symmetry_pairs.append({"positive_x": bone.name, "negative_x": other.name,
                               "confidence": 0.9 if same_deform else 0.7,
                               "evidence": ["Mirrored rest head and tail", "Matching deform flag" if same_deform else "Different deform flags"],
                               "anatomical_side": "unknown"})

drivers = []
for datablock, label in [(arm, "object"), (arm.data, "armature_data")]:
    if datablock.animation_data:
        for curve in datablock.animation_data.drivers:
            drivers.append({"owner": label, "data_path": curve.data_path, "array_index": curve.array_index,
                            "valid": curve.driver.is_valid, "muted": curve.mute,
                            "variables": len(curve.driver.variables)})
            if not curve.driver.is_valid:
                issues.append({"code": "invalid_driver", "owner": label, "data_path": curve.data_path})
zero_length = [bone.name for bone in bones if bone.length <= 0.000001]
issues.extend({"code": "zero_length_bone", "bone": name} for name in zero_length)
control_candidates = [{"name": bone.name,
                       "confidence": 0.9 if bone.name in target_bones and not bone.use_deform else 0.55,
                       "evidence": ["Constraint target"] + (["Non-deform"] if not bone.use_deform else ["Deform bone"])}
                      for bone in bones if bone.name in target_bones]
result = {"armature": arm.name, "bones": len(bones),
          "deform_bones": sum(1 for bone in bones if bone.use_deform),
          "non_deform_bones": sum(1 for bone in bones if not bone.use_deform),
          "root_bones": [bone.name for bone in bones if bone.parent is None][offset:offset + limit],
          "ik_chains": ik_chains[offset:offset + limit],
          "fk_chain_candidates": fk_candidates[offset:offset + limit],
          "pole_targets": pole_targets[offset:offset + limit],
          "control_candidates": control_candidates[offset:offset + limit],
          "symmetry_pairs": symmetry_pairs[offset:offset + limit],
          "constraints": constraints[offset:offset + limit],
          "drivers": drivers[offset:offset + limit],
          "issues": issues[offset:offset + limit],
          "pagination": {"offset": offset, "limit": limit,
                         "totals": {"root_bones": sum(1 for bone in bones if bone.parent is None),
                                    "ik_chains": len(ik_chains), "fk_chain_candidates": len(fk_candidates),
                                    "pole_targets": len(pole_targets), "control_candidates": len(control_candidates),
                                    "symmetry_pairs": len(symmetry_pairs), "constraints": len(constraints),
                                    "drivers": len(drivers), "issues": len(issues)}},
          "warnings": ["Roles are inferred conservatively. No anatomical limb labels or IK/FK switch semantics are assumed.",
                       "Confidence is an evidence score, not a calibrated probability. No modifications are made."],
          "spatial_symmetry": {"axis": "armature-local X", "tolerance_blender_units": tolerance},
}
'''


def rig_analyze(bridge, armature: str, limit: int = 100, offset: int = 0) -> dict:
    """Describe explicit IK and conservative FK/control/symmetry candidates."""
    _name(armature)
    _page(limit, offset)
    return bridge.run(RIG_SCRIPT, {"armature": armature, "limit": limit, "offset": offset})
