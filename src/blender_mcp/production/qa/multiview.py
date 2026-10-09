"""Bounded multiview previews with transactional Blender state restoration.

The existing capture preview template performs rendering. This module supplies
temporary orthographic cameras and strictly scoped Action assignment around it.
One Blender payload owns the complete mutation/finally sequence: host timeouts
never cause an automatic retry of an uncertain Blender operation.
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path
from textwrap import indent

from ..checkpoint import create_checkpoint
from ..common.storage import write_json
from .capture import _PREVIEW, _STATE, _integer, _verify_images, render_backend
from .png_montage import write_montage
from ..common.state_guard import STATE_RUNTIME


VIEWS = ("FRONT", "SIDE", "THREE_QUARTER")
OPTIONAL_VIEWS = ("LOW_ANGLE", "FOOT")

_PREFLIGHT = '''
import bpy
import json
''' + STATE_RUNTIME + '''

def mv_supported_paths(target):
    transforms = ["location", "scale", "rotation_euler", "rotation_quaternion", "rotation_axis_angle"]
    paths = set(transforms + ["delta_location", "delta_scale", "delta_rotation_euler", "delta_rotation_quaternion"])
    paths.update("[" + json.dumps(name, ensure_ascii=False) + "]" for name in target.keys())
    for constraint in target.constraints:
        paths.update(constraint.path_from_id() + "." + field for field in ["influence", "mute"])
    if target.type == "ARMATURE":
        for bone in target.pose.bones:
            prefix = bone.path_from_id()
            paths.update(prefix + "." + field for field in transforms)
            paths.update(prefix + "[" + json.dumps(name, ensure_ascii=False) + "]" for name in bone.keys())
            for constraint in bone.constraints:
                paths.update(constraint.path_from_id() + "." + field for field in ["influence", "mute"])
    return paths

def mv_validate_paths(action, slot, supported):
    if action is None:
        return
    if hasattr(action, "is_action_layered") and action.is_action_layered:
        curves = [curve for layer in action.layers for strip in layer.strips
                  for bag in strip.channelbags if slot and bag.slot_handle == slot.handle for curve in bag.fcurves]
    else:
        curves = list(action.fcurves)
    if len(curves) > 20000:
        raise ValueError("QA Action channel budget exceeded")
    unsupported = sorted(set(curve.data_path for curve in curves if curve.data_path not in supported))
    if unsupported:
        raise ValueError("QA cannot safely restore these animated property paths: " + str(unsupported[:5]))

scene = bpy.context.scene
target = scene.objects.get(params["object_name"])
if target is None:
    raise ValueError("Object does not exist in the active scene")
if bpy.context.mode != "OBJECT":
    raise ValueError("MODE_NOT_SUPPORTED: Multiview QA requires Object mode; mode is never changed automatically")
# A render of an armature in Rest Position shows the bind pose, not the Action under review.
prod_require_pose_position(target, "QA target")
if target.library or target.override_library:
    raise ValueError("Linked/overridden target is not supported for temporary Action QA")
ad = target.animation_data
if ad and (ad.use_tweak_mode or (ad.use_nla and any(not t.mute and len(t.strips) for t in ad.nla_tracks))):
    raise ValueError("Active NLA/tweak mode is not supported for isolated Action comparison")
if ad and (ad.action_blend_type != "REPLACE" or abs(ad.action_influence - 1.0) > 1e-8):
    raise ValueError("Action comparison requires REPLACE blend and full influence")
actions = []
supported_paths = mv_supported_paths(target)
for selection in params["selections"]:
    name = selection["action"]
    action = bpy.data.actions.get(name) if name else (ad.action if ad else None)
    if name and action is None:
        raise ValueError("Action does not exist: " + name)
    if name and ad is None:
        raise ValueError("Target must already have animation_data; QA never creates it")
    slot_id = selection["slot"]
    slot = None
    slots = list(action.slots) if action and hasattr(action, "slots") else []
    if slots:
        compatible = [s for s in slots if s.target_id_type == target.id_type]
        if slot_id:
            slot = next((s for s in compatible if s.identifier == slot_id), None)
            if slot is None:
                raise ValueError("Requested Action slot is absent or incompatible: " + slot_id)
        elif not name and ad and ad.action_slot:
            slot = ad.action_slot
        elif len(compatible) == 1:
            slot = compatible[0]
        else:
            raise ValueError("Ambiguous Action slots; provide an explicit slot identifier")
    elif slot_id:
        raise ValueError("Slot identifier was provided for a slotless Action")
    elif action and hasattr(action, "is_action_layered") and action.is_action_layered:
        raise ValueError("Layered Action has no assignable slot")
    mv_validate_paths(action, slot, supported_paths)
    actions.append({"label": selection["label"], "action": action.name if action else None,
                    "slot": slot.identifier if slot else None})
result = {"object": target.name, "actions": actions, "frame_current": scene.frame_current,
          "frame_start": scene.frame_start, "frame_end": scene.frame_end,
          "fps": scene.render.fps, "fps_base": scene.render.fps_base,
          "source_file": bpy.data.filepath}
'''

# Use the unchanged M1 rendering implementation within the transaction. Local
# params and result in this function cannot replace the outer operation state.
_PREVIEW_FUNCTION = "\ndef mv_preview(params):\n" + indent(_PREVIEW, "    ") + "\n    return result\n"

_RENDER = _STATE + _PREVIEW_FUNCTION + '''
import math
import mathutils

def mv_custom(item):
    values = {}
    for name, value in item.items():
        if hasattr(value, "to_dict"):
            values[name] = value.to_dict()
        elif hasattr(value, "to_list"):
            values[name] = value.to_list()
        else:
            values[name] = value
    return values

def mv_channels(item):
    return {"location": list(item.location), "scale": list(item.scale),
            "rotation_mode": item.rotation_mode, "rotation_euler": list(item.rotation_euler),
            "rotation_quaternion": list(item.rotation_quaternion),
            "rotation_axis_angle": list(item.rotation_axis_angle), "custom": mv_custom(item),
            "constraints": {con.name: {"influence": con.influence, "mute": con.mute} for con in item.constraints}}

def mv_restore_channels(item, values):
    item.location = values["location"]
    item.scale = values["scale"]
    item.rotation_mode = values["rotation_mode"]
    item.rotation_euler = values["rotation_euler"]
    item.rotation_quaternion = values["rotation_quaternion"]
    item.rotation_axis_angle = values["rotation_axis_angle"]
    current = mv_custom(item)
    for name, value in values["custom"].items():
        if name not in current or current[name] != value:
            item[name] = value
    for name, state in values["constraints"].items():
        constraint = item.constraints[name]
        constraint.influence = state["influence"]
        constraint.mute = state["mute"]

def mv_pose(target):
    return {"object": mv_channels(target),
            "delta_location": list(target.delta_location), "delta_scale": list(target.delta_scale),
            "delta_rotation_euler": list(target.delta_rotation_euler),
            "delta_rotation_quaternion": list(target.delta_rotation_quaternion),
            "bones": {bone.name: mv_channels(bone) for bone in target.pose.bones} if target.type == "ARMATURE" else {}}

def mv_restore_pose(target, values):
    mv_restore_channels(target, values["object"])
    target.delta_location = values["delta_location"]
    target.delta_scale = values["delta_scale"]
    target.delta_rotation_euler = values["delta_rotation_euler"]
    target.delta_rotation_quaternion = values["delta_rotation_quaternion"]
    if target.type == "ARMATURE":
        for name, channels in values["bones"].items():
            mv_restore_channels(target.pose.bones[name], channels)

def mv_state(scene, target):
    ad = target.animation_data
    shading = scene.display.shading
    state = qa_state(scene)
    state.update({"engine": scene.render.engine,
                  "pixel_aspect_x": scene.render.pixel_aspect_x,
                  "pixel_aspect_y": scene.render.pixel_aspect_y,
                  "film_transparent": scene.render.film_transparent,
                  "fps": scene.render.fps, "fps_base": scene.render.fps_base,
                  "frame_start": scene.frame_start, "frame_end": scene.frame_end,
                  "action": ad.action.name if ad and ad.action else None,
                  "action_slot": ad.action_slot.identifier if ad and ad.action_slot else None,
                  "last_slot_identifier": ad.last_slot_identifier if ad and hasattr(ad, "last_slot_identifier") else None,
                  "light": shading.light, "color_type": shading.color_type,
                  "background_type": shading.background_type,
                  "background_color": list(shading.background_color),
                  "show_shadows": shading.show_shadows, "show_cavity": shading.show_cavity,
                  "camera_markers": [[marker.name, marker.frame, marker.camera.name if marker.camera else None]
                                     for marker in scene.timeline_markers],
                  "object_count": len(bpy.data.objects), "camera_count": len(bpy.data.cameras)})
    return state

def mv_assign(target, selection):
    if selection["action"] is not None:
        action = bpy.data.actions.get(selection["action"])
        if action is None:
            raise ValueError("Selected Action disappeared before rendering")
        target.animation_data.action = action
        if selection["slot"] is not None:
            slot = next((s for s in action.slots if s.identifier == selection["slot"]), None)
            if slot is None:
                raise ValueError("Selected slot disappeared before rendering")
            target.animation_data.action_slot = slot

def mv_points(target, scene):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    bone_names = params.get("framing_bones", [])
    mesh_names = params.get("framing_meshes", [])
    if bone_names:
        if target.type != "ARMATURE" or any(target.pose.bones.get(name) is None for name in bone_names):
            raise ValueError("Explicit framing bones must belong to the selected armature")
        evaluated = target.evaluated_get(depsgraph)
        points = [evaluated.matrix_world @ point for name in bone_names
                  for point in [evaluated.pose.bones[name].head, evaluated.pose.bones[name].tail]]
        if any(not math.isfinite(component) for point in points for component in point):
            raise ValueError("Non-finite framing bone coordinates cannot be rendered safely")
        padding = params.get("framing_padding_m", 0.05) / scene.unit_settings.scale_length
        return [point + mathutils.Vector((x,y,z)) for point in points
                for x in [-padding,padding] for y in [-padding,padding] for z in [-padding,padding]]
    candidates = [target]
    for item in scene.objects:
        if item != target and (item in target.children_recursive or
                              any(m.type == "ARMATURE" and m.object == target for m in item.modifiers)):
            candidates.append(item)
    if mesh_names:
        selected = [scene.objects.get(name) for name in mesh_names]
        if any(item is None or item.type != "MESH" or item not in candidates for item in selected):
            raise ValueError("Explicit framing meshes must belong to the selected object/armature")
        candidates = selected
    if len(candidates) > 512:
        raise ValueError("QA framing exceeds 512 related objects")
    points = []
    for item in candidates:
        evaluated = item.evaluated_get(depsgraph)
        if item.type == "ARMATURE":
            if len(evaluated.pose.bones) > 2048:
                raise ValueError("QA framing exceeds 2048 pose bones")
            for bone in evaluated.pose.bones:
                points.extend([evaluated.matrix_world @ bone.head, evaluated.matrix_world @ bone.tail])
        elif item.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"} and not item.hide_render:
            points.extend([evaluated.matrix_world @ mathutils.Vector(corner) for corner in evaluated.bound_box])
    if not points:
        raise ValueError("Target has no renderable geometry or pose bounds")
    if any(not math.isfinite(component) for point in points for component in point):
        raise ValueError("Non-finite target bounds cannot be framed safely")
    return points

scene = bpy.context.scene
target = scene.objects.get(params["object_name"])
if target is None:
    raise ValueError("QA target disappeared before rendering")
original_camera = scene.camera
original_marker_cameras = [(marker, marker.camera) for marker in scene.timeline_markers]
ad = target.animation_data
original_action = ad.action if ad else None
original_slot = ad.action_slot if ad else None
original_last_slot = ad.last_slot_identifier if ad and hasattr(ad, "last_slot_identifier") else None
before = mv_state(scene, target)
original_pose = mv_pose(target)
camera_object = None
camera_data = None
result = {"status": "ok", "before": before, "files": [], "series": [], "cameras": {},
          "warnings": ["World axes: FRONT looks from -Y, SIDE from +X, THREE_QUARTER from +X/-Y/+Z.",
                       "Framing includes selected object, descendants and meshes using its armature; other scene geometry remains visible.",
                       "This is a sampled technical preview, not a judgement of artistic animation quality.",
                       "Blender Render Result is replaced; its previous buffer is not preserved."],
          "errors": [], "state_restored": False, "comparable": False,
          "timing": {"frames": params["frames"], "fps": scene.render.fps,
                     "fps_base": scene.render.fps_base},
          "resolution": [params["width"], params["height"]]}
try:
    # Bounds are the union of BOTH Actions at identical requested frames.
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    for selection in params["actions"]:
        # Each Action evaluates from the same unkeyed baseline. Otherwise a
        # channel absent from 'before' can retain the sampled 'after' value.
        mv_restore_pose(target, original_pose)
        mv_assign(target, selection)
        for frame in params["frames"]:
            scene.frame_set(frame, subframe=0.0)
            for point in mv_points(target, scene):
                for axis in range(3):
                    low[axis] = min(low[axis], point[axis])
                    high[axis] = max(high[axis], point[axis])
    corners = [mathutils.Vector((x,y,z)) for x in [low[0],high[0]]
               for y in [low[1],high[1]] for z in [low[2],high[2]]]
    center = mathutils.Vector([(a+b)/2.0 for a,b in zip(low,high)])
    radius = max((point-center).length for point in corners)
    if radius < 1e-8:
        raise ValueError("Target bounds are degenerate; camera framing is undefined")
    camera_data = bpy.data.cameras.new("ProductionQA_" + params["operation_id"])
    camera_data.type = "ORTHO"
    camera_data.clip_start = max(radius * 0.001, 0.00001)
    camera_data.clip_end = max(radius * 20.0, 1.0)
    camera_object = bpy.data.objects.new("ProductionQA_" + params["operation_id"], camera_data)
    scene.collection.objects.link(camera_object)
    # render.render can resolve camera markers again internally after the
    # explicit scene.camera assignment in M1. Suspend only camera bindings;
    # preserve every marker and restore its pointer in finally.
    for marker, marker_camera in original_marker_cameras:
        marker.camera = None
    directions = {"FRONT": (0,-1,0), "SIDE": (1,0,0), "THREE_QUARTER": (1,-1,0.55),
                  "LOW_ANGLE": (1,-1,0.12), "FOOT": (0,-1,0.18)}
    for view in params["views"]:
        direction = mathutils.Vector(directions[view]).normalized()
        rotation = (-direction).to_track_quat("-Z", "Y")
        inverse = rotation.inverted()
        projected = [inverse @ (point-center) for point in corners]
        span_x = max(p.x for p in projected)-min(p.x for p in projected)
        span_y = max(p.y for p in projected)-min(p.y for p in projected)
        scale = max(span_y, span_x * params["height"] / params["width"]) * params["margin"]
        result["cameras"][view] = {"location": list(center + direction*radius*4.0),
                                    "rotation_quaternion": list(rotation), "type": "ORTHO",
                                    "ortho_scale": scale,
                                    "clip_start": camera_data.clip_start, "clip_end": camera_data.clip_end}
    if params["quality"] == "lightweight":
        scene.render.engine = "BLENDER_WORKBENCH"
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.color_type = "MATERIAL"
        shading.background_type = "VIEWPORT"
        shading.background_color = (0.055,0.055,0.055)
        shading.show_shadows = True
        shading.show_cavity = False
    scene.render.pixel_aspect_x = 1.0
    scene.render.pixel_aspect_y = 1.0
    scene.render.film_transparent = False
    result["render_engine"] = scene.render.engine
    for selection in params["actions"]:
        mv_restore_pose(target, original_pose)
        mv_assign(target, selection)
        for view in params["views"]:
            definition = result["cameras"][view]
            camera_object.location = definition["location"]
            camera_object.rotation_mode = "QUATERNION"
            camera_object.rotation_quaternion = definition["rotation_quaternion"]
            camera_data.ortho_scale = definition["ortho_scale"]
            scene.camera = camera_object
            bpy.context.view_layer.update()
            output_dir = params["directories"][selection["label"]][view]
            preview = mv_preview({"frame_start": params["frames"][0], "frame_end": params["frames"][-1],
                                  "step": params["step"], "max_frames": len(params["frames"]),
                                  "width": params["width"], "height": params["height"],
                                  "output_dir": output_dir, "allow_cpu_fallback": params["allow_cpu_fallback"]})
            series = {"label": selection["label"], "action": selection["action"],
                      "slot": selection["slot"], "view": view, "files": preview["files"],
                      "render_backend": preview.get("render_backend"),
                      "status": preview["status"], "camera": definition,
                      "timing": result["timing"], "resolution": result["resolution"]}
            result["series"].append(series)
            result["files"].extend(preview["files"])
            if preview["status"] != "ok" or not preview["state_restored"]:
                result["errors"].extend(preview["errors"])
                raise RuntimeError("Rendering failed; partial artifacts retained")
except Exception as exc:
    result["status"] = "error"
    result["errors"].append({"type": str(type(exc)), "message": str(exc)})
finally:
    # Restore Action BEFORE frame evaluation to recover the original pose.
    if ad:
        ad.action = original_action
        if original_slot is not None:
            ad.action_slot = original_slot
        if original_last_slot is not None:
            ad.last_slot_identifier = original_last_slot
    for marker, marker_camera in original_marker_cameras:
        marker.camera = marker_camera
    qa_restore(scene, before, original_camera)
    # Missing channels in the original Action do not reset automatically after
    # evaluating a different Action. Restore saved transforms/custom values.
    mv_restore_pose(target, original_pose)
    bpy.context.view_layer.update()
    scene.render.engine = before["engine"]
    scene.render.pixel_aspect_x = before["pixel_aspect_x"]
    scene.render.pixel_aspect_y = before["pixel_aspect_y"]
    scene.render.film_transparent = before["film_transparent"]
    shading = scene.display.shading
    shading.light = before["light"]
    shading.color_type = before["color_type"]
    shading.background_type = before["background_type"]
    shading.background_color = before["background_color"]
    shading.show_shadows = before["show_shadows"]
    shading.show_cavity = before["show_cavity"]
    if camera_object is not None:
        bpy.data.objects.remove(camera_object, do_unlink=True)
    if camera_data is not None:
        bpy.data.cameras.remove(camera_data)
result["after"] = mv_state(scene, target)
result["pose_state_restored"] = original_pose == mv_pose(target)
result["state_restored"] = result["before"] == result["after"] and result["pose_state_restored"]
if not result["state_restored"]:
    result["status"] = "error"
    result["errors"].append({"type": "StateRestorationError", "message": "QA state differs after restoration"})
expected_images = len(params["frames"]) * len(params["views"]) * len(params["actions"])
result["metrics"] = {"requested_images": expected_images, "rendered_images": len(result["files"]),
                     "view_count": len(params["views"]), "frames_per_view": len(params["frames"])}
result["comparable"] = (len(params["actions"]) == 2 and result["status"] == "ok"
                          and result["state_restored"] and len(result["files"]) == expected_images)
'''


def _options(options):
    values = dict(options or {})
    defaults = {"width": 480, "height": 480, "max_frames": 3, "step": None,
                "frame_start": None, "frame_end": None, "views": list(VIEWS),
                "quality": "lightweight", "margin": 1.25, "montage": True,
                "action_slot": None, "before_slot": None, "after_slot": None,
                "framing_bones": [], "framing_meshes": [], "framing_padding_m": 0.05,
                "allow_cpu_fallback": False}
    unknown = values.keys() - defaults.keys()
    if unknown:
        raise ValueError("Unknown QA options: " + ", ".join(sorted(unknown)))
    defaults.update(values)
    for name in ("width", "height"):
        _integer(name, defaults[name], 16, 1920)
    _integer("max_frames", defaults["max_frames"], 1, 24)
    for name in ("frame_start", "frame_end"):
        if defaults[name] is not None:
            _integer(name, defaults[name], -1_048_574, 1_048_574)
    if defaults["step"] is not None:
        _integer("step", defaults["step"], 1, 1_000_000)
    views = defaults["views"]
    if not isinstance(views, list) or not views or len(views) > 3 or any(v not in VIEWS + OPTIONAL_VIEWS for v in views) or len(set(views)) != len(views):
        raise ValueError("views must contain 1..3 unique FRONT, SIDE, THREE_QUARTER, LOW_ANGLE, FOOT values")
    for name in ("framing_bones", "framing_meshes"):
        selected = defaults[name]
        if not isinstance(selected, list) or len(selected) > 16 or any(not isinstance(item, str) or not item for item in selected) or len(set(selected)) != len(selected):
            raise ValueError(name + " must contain at most 16 unique explicit names")
    if defaults["framing_bones"] and defaults["framing_meshes"]:
        raise ValueError("Choose framing_bones or framing_meshes, not both")
    if "FOOT" in views and not (defaults["framing_bones"] or defaults["framing_meshes"]):
        raise ValueError("FOOT view needs explicit framing_bones or framing_meshes; no guessed foot selection")
    padding = defaults["framing_padding_m"]
    if isinstance(padding, bool) or not isinstance(padding, (int, float)) or not math.isfinite(padding) or not 0.001 <= padding <= 10:
        raise ValueError("framing_padding_m must be finite and between 0.001 and 10 meters")
    if defaults["quality"] not in {"lightweight", "scene"}:
        raise ValueError("quality must be lightweight (Workbench) or scene (existing engine)")
    margin = defaults["margin"]
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(margin) or not 1.05 <= margin <= 3.0:
        raise ValueError("margin must be finite and between 1.05 and 3.0")
    if not isinstance(defaults["montage"], bool):
        raise ValueError("montage must be boolean")
    if not isinstance(defaults["allow_cpu_fallback"], bool):
        raise ValueError("allow_cpu_fallback must be boolean")
    for name in ("action_slot", "before_slot", "after_slot"):
        if defaults[name] is not None and (not isinstance(defaults[name], str) or not defaults[name]):
            raise ValueError(name + " must be a nonempty slot identifier or null")
    return defaults


def _frames(options, preflight, action_count):
    start = options["frame_start"] if options["frame_start"] is not None else preflight["frame_start"]
    end = options["frame_end"] if options["frame_end"] is not None else preflight["frame_end"]
    if end < start:
        raise ValueError("frame_end must be >= frame_start")
    step = options["step"]
    if step is None:
        step = max(1, math.ceil((end - start) / max(1, options["max_frames"] - 1)))
        if options["max_frames"] == 1:
            step = end - start + 1
    frames = list(range(start, end + 1, step))
    if len(frames) > options["max_frames"]:
        raise ValueError("Requested frames exceed max_frames; increase step or narrow range")
    images = len(frames) * len(options["views"]) * action_count
    if images > 72 or images * options["width"] * options["height"] > 48_000_000:
        raise ValueError("QA request exceeds bounded budget (72 images / 48 million pixels)")
    return frames, step


def _run(bridge, storage, operation_id, object_name, selections, options):
    started = time.perf_counter()
    if not isinstance(object_name, str) or not object_name.strip():
        raise ValueError("object_name is required")
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id is required")
    for selection in selections:
        if selection["action"] is not None and (not isinstance(selection["action"], str) or not selection["action"]):
            raise ValueError("Action names must be nonempty strings")
    preflight = bridge.run(_PREFLIGHT, {"object_name": object_name, "selections": selections})
    frames, step = _frames(options, preflight, len(selections))
    # quality=scene keeps the scene engine: a Cycles scene must render on OptiX (AUD-015).
    backend = render_backend(bridge, options["allow_cpu_fallback"]) if options["quality"] == "scene" else None
    checkpoint = create_checkpoint(bridge, storage, "Before temporary multiview cameras and Action comparison",
                                   operation_id, "qa_compare_before_after" if len(selections) == 2 else "qa_render_multiview")
    folder = storage.reserve("qa", operation_id)
    directories = {}
    for selection in selections:
        label = selection["label"]
        directories[label] = {}
        for view in options["views"]:
            directory = folder / label / view
            longest_name = max((f"frame_{frame:06d}.png" for frame in frames), key=len)
            if os.name == "nt" and len(str(directory / longest_name)) >= 260:
                raise ValueError("Blender PNG output exceeds Windows path limit; use a shorter BLENDER_PRODUCTION_WORKDIR. Recovery checkpoint: " + checkpoint["manifest"])
            directory.mkdir(parents=True, exist_ok=False)
            directories[label][view] = str(directory)
    params = {**options, "object_name": object_name, "operation_id": operation_id,
              "actions": preflight["actions"], "frames": frames, "step": step,
              "directories": directories}
    write_json(folder / "intent.json", {"operation_id": operation_id, "checkpoint": checkpoint,
                                        "preflight": preflight, "settings": params})
    try:
        result = bridge.run(_RENDER, params)
    except Exception as exc:
        result = {"status": "error", "state_restored": None, "comparable": False,
                  "errors": [{"type": type(exc).__name__, "message": str(exc)}],
                  "warnings": ["Blender state is unverified after transport failure. Do not retry automatically; inspect the recovery checkpoint and partial artifacts."],
                  "files": [], "series": [], "metrics": {}}
    result.update(operation_id=operation_id, checkpoint=checkpoint, output_dir=str(folder),
                  affected={"object": object_name, "actions": preflight["actions"]})
    series_backends = [s.get("render_backend") for s in result.get("series", []) if s.get("render_backend")]
    result["render_backend"] = series_backends[0] if series_backends else backend
    try:
        for series in result.get("series", []):
            directory = Path(directories[series["label"]][series["view"]])
            verified = _verify_images({"status": "ok", "files": series["files"], "errors": []}, directory)
            if verified["status"] != "ok":
                result["status"] = "error"
                result.setdefault("errors", []).extend(verified["errors"])
            for item in series["files"]:
                if (item.get("width"), item.get("height")) != (options["width"], options["height"]):
                    raise ValueError("Rendered dimensions differ from requested comparison dimensions")
            if result.get("status") == "ok":
                if [item["frame"] for item in series["files"]] != frames:
                    raise ValueError("Rendered frame sequence differs from requested timing")
                if series["camera"] != result["cameras"][series["view"]] or series["timing"] != result["timing"]:
                    raise ValueError("Viewpoint/timing mismatch in comparison artifacts")
                if series["resolution"] != [options["width"], options["height"]]:
                    raise ValueError("Comparison resolution metadata mismatch")
        expected = len(frames) * len(options["views"]) * len(selections)
        if result.get("status") == "ok" and (len(result.get("series", [])) != len(options["views"])*len(selections) or len(result.get("files", [])) != expected):
            raise ValueError("Rendered artifact inventory is incomplete")
        if result.get("status") == "ok":
            pairs = [(s["label"], s["view"]) for s in result["series"]]
            if len(set(pairs)) != len(pairs):
                raise ValueError("Comparison contains duplicate viewpoint/Action entries")
            result["comparable"] = len(selections) == 2 and result.get("state_restored") is True
        if result.get("status") == "ok" and options["montage"]:
            representative = len(frames) // 2
            rows = []
            for selection in selections:
                row = []
                for view in options["views"]:
                    series = next(s for s in result["series"] if s["label"] == selection["label"] and s["view"] == view)
                    row.append(Path(series["files"][representative]["path"]))
                rows.append(row)
            montage = folder / "montage.png"
            width, height = write_montage(rows, montage)
            result["montage"] = {"path": str(montage), "width": width, "height": height,
                                 "frame": frames[representative], "rows": [s["label"] for s in selections],
                                 "columns": options["views"]}
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        result["status"] = "error"
        result.setdefault("errors", []).append({"type": type(exc).__name__, "message": str(exc)})
    if result.get("status") != "ok":
        result["comparable"] = False
    result.setdefault("metrics", {})["elapsed_ms"] = round((time.perf_counter()-started)*1000, 3)
    write_json(folder / "result.json", result)
    return result


def render_multiview(bridge, storage, operation_id, object_name, options=None) -> dict:
    """Checkpoint and render bounded FRONT/SIDE/THREE_QUARTER PNG samples.

    Defaults: Workbench, 480x480, up to three frames across the scene range.
    options supports frame_start/end, step, max_frames, dimensions, views,
    margin, quality (lightweight/scene), montage, and explicit action_slot.
    World axes are camera conventions, not inferred character orientation.
    """
    settings = _options(options)
    return _run(bridge, storage, operation_id, object_name,
                [{"label": "preview", "action": None, "slot": settings["action_slot"]}], settings)


def compare_before_after(bridge, storage, operation_id, object_name, before_action, after_action, options=None) -> dict:
    """Render two existing Actions with identical timing and union-framed cameras.

    Restores the original Action and slot; never edits Action contents. Active
    NLA/tweak mode is refused. Ambiguous layered Actions require before_slot /
    after_slot identifiers. Result.comparable is technical comparability only.
    """
    if before_action is None or after_action is None:
        raise ValueError("Both before_action and after_action are required")
    settings = _options(options)
    return _run(bridge, storage, operation_id, object_name,
                [{"label": "before", "action": before_action, "slot": settings["before_slot"]},
                 {"label": "after", "action": after_action, "slot": settings["after_slot"]}], settings)
