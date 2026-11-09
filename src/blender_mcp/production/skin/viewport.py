"""Typed inspection and configuration of a 3D viewport for comparable captures.

GlobalSkin review needed the same Material Preview HDRI, orientation and distance for every image, and the
old capture failed when the window showed a console instead of a VIEW_3D. Here the area is selected (or,
only with explicit permission, converted), the shading and view are set from a validated spec, a
signature of the effective state is returned, and the previous UI state is restored exactly unless the
caller asks to keep the configuration.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from ..common.storage import write_json
from ..qa.capture import _verify_images

SHADING_TYPES = ("SOLID", "MATERIAL", "RENDERED", "WIREFRAME")
ORIENTATIONS = ("FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM", "THREE_QUARTER_LEFT", "THREE_QUARTER_RIGHT")
CONVERTIBLE = ("CONSOLE", "INFO", "TEXT_EDITOR", "OUTLINER", "PROPERTIES", "FILE_BROWSER", "IMAGE_EDITOR",
               "NODE_EDITOR", "DOPESHEET_EDITOR", "GRAPH_EDITOR", "NLA_EDITOR", "SEQUENCE_EDITOR", "CLIP_EDITOR",
               "SPREADSHEET", "PREFERENCES")

RUNTIME = r'''
import bpy
import math
import mathutils

def vp_window():
    window = bpy.context.window
    if bpy.app.background or window is None:
        raise ValueError('VIEWPORT_GUI_REQUIRED: this Blender runs without a window (background mode)')
    return window

def vp_round(values):
    # Full precision: these values are written back on restore; signatures round them host-side.
    return [float(v) for v in values]

def vp_space_state(area):
    space = area.spaces.active
    region3d = space.region_3d
    shading = space.shading
    return {'area_type': area.type, 'shading': {'type': shading.type, 'light': shading.light,
            'studio_light': shading.studio_light, 'studiolight_rotate_z': shading.studiolight_rotate_z,
            'studiolight_intensity': shading.studiolight_intensity,
            'studiolight_background_alpha': shading.studiolight_background_alpha,
            'studiolight_background_blur': shading.studiolight_background_blur,
            'use_scene_lights': shading.use_scene_lights, 'use_scene_world': shading.use_scene_world,
            'use_scene_lights_render': shading.use_scene_lights_render, 'use_scene_world_render': shading.use_scene_world_render},
            'view': {'perspective': region3d.view_perspective, 'location': vp_round(region3d.view_location),
            'rotation': vp_round(region3d.view_rotation), 'distance': region3d.view_distance,
            'lens': space.lens, 'clip_start': space.clip_start, 'clip_end': space.clip_end},
            'overlays': space.overlay.show_overlays, 'show_gizmo': space.show_gizmo}

def vp_areas(window):
    rows = []
    for index, area in enumerate(window.screen.areas):
        row = {'index': index, 'type': area.type, 'ui_type': area.ui_type, 'x': area.x, 'y': area.y,
               'width': area.width, 'height': area.height}
        if area.type == 'VIEW_3D':
            row['state'] = vp_space_state(area)
        rows.append(row)
    return rows

def vp_studio_lights():
    return sorted(light.name for light in bpy.context.preferences.studio_lights if light.type == 'WORLD')

def vp_pick(window, spec):
    areas = list(window.screen.areas)
    index = spec.get('area_index')
    if index is not None:
        if not 0 <= index < len(areas):
            raise ValueError('VIEWPORT_AREA_MISSING: area_index ' + str(index) + ' of ' + str(len(areas)))
        area = areas[index]
    else:
        area = None
        for candidate in areas:
            if candidate.type == 'VIEW_3D' and (area is None or candidate.width * candidate.height > area.width * area.height):
                area = candidate
        if area is None:
            if not spec.get('allow_area_conversion'):
                raise ValueError('VIEWPORT_NO_VIEW3D: the window has no VIEW_3D area; pass area_index and allow_area_conversion=true to convert one')
            raise ValueError('VIEWPORT_AREA_INDEX_REQUIRED: choose which area to convert with area_index')
    if area.type != 'VIEW_3D':
        if not spec.get('allow_area_conversion'):
            raise ValueError('VIEWPORT_AREA_NOT_VIEW3D: area ' + str(areas.index(area)) + ' is ' + area.type + '; allow_area_conversion=true converts it and restores it afterwards')
        if area.type not in params['convertible']:
            raise ValueError('VIEWPORT_AREA_NOT_CONVERTIBLE: ' + area.type)
    return areas.index(area), area

def vp_target(spec):
    target = spec.get('target') or {}
    if 'object' in target:
        obj = bpy.data.objects.get(target['object'])
        if obj is None:
            raise ValueError('VIEWPORT_TARGET_MISSING: ' + str(target['object']))
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        corners = [evaluated.matrix_world @ mathutils.Vector(c) for c in evaluated.bound_box]
        center = sum(corners, mathutils.Vector()) / 8.0
        radius = max((c - center).length for c in corners)
        return center, radius, obj.name
    if 'point' in target:
        return mathutils.Vector(target['point']), None, None
    return None, None, None

ORIENT = {'FRONT': (90, 0, 0), 'BACK': (90, 0, 180), 'LEFT': (90, 0, -90), 'RIGHT': (90, 0, 90),
          'TOP': (0, 0, 0), 'BOTTOM': (180, 0, 0), 'THREE_QUARTER_LEFT': (90, 0, -45), 'THREE_QUARTER_RIGHT': (90, 0, 45)}

def vp_apply(window, spec):
    index, area = vp_pick(window, spec)
    previous_type = area.type
    if area.type != 'VIEW_3D':
        area.type = 'VIEW_3D'
    space = area.spaces.active
    region3d = space.region_3d
    shading = space.shading
    previous = vp_space_state(area)
    previous['area_type'] = previous_type
    wanted = spec.get('shading') or {}
    if 'type' in wanted:
        shading.type = wanted['type']
    if 'studio_light' in wanted:
        available = vp_studio_lights()
        if wanted['studio_light'] not in available:
            raise ValueError('VIEWPORT_HDRI_MISSING: ' + wanted['studio_light'] + ' not in ' + str(available))
        shading.studio_light = wanted['studio_light']
    for key, attribute in (('rotate_z_deg', 'studiolight_rotate_z'), ('intensity', 'studiolight_intensity'),
                           ('background_alpha', 'studiolight_background_alpha'), ('background_blur', 'studiolight_background_blur')):
        if key in wanted:
            value = math.radians(wanted[key]) if key == 'rotate_z_deg' else wanted[key]
            if attribute == 'studiolight_rotate_z':
                shading.studiolight_rotate_z = value
            elif attribute == 'studiolight_intensity':
                shading.studiolight_intensity = value
            elif attribute == 'studiolight_background_alpha':
                shading.studiolight_background_alpha = value
            else:
                shading.studiolight_background_blur = value
    for key in ('use_scene_lights', 'use_scene_world'):
        if key in wanted:
            if key == 'use_scene_lights':
                shading.use_scene_lights = wanted[key]
            else:
                shading.use_scene_world = wanted[key]
    view = spec.get('view') or {}
    if 'perspective' in view:
        region3d.view_perspective = view['perspective']
    if 'lens_mm' in view:
        space.lens = view['lens_mm']
    if 'orientation' in view:
        rx, ry, rz = ORIENT[view['orientation']]
        region3d.view_rotation = mathutils.Euler((math.radians(rx), math.radians(ry), math.radians(rz)), 'XYZ').to_quaternion()
    elif 'rotation' in view:
        region3d.view_rotation = mathutils.Quaternion(view['rotation']).normalized()
    center, radius, target_name = vp_target(view)
    if center is not None:
        region3d.view_location = center
    if view.get('distance') == 'fit':
        if radius is None:
            raise ValueError('VIEWPORT_FIT_NEEDS_OBJECT: distance=fit needs target.object')
        # Viewport field of view: Blender's 3D view uses a 72 mm sensor for its lens value.
        half = math.atan(36.0 / space.lens)
        region3d.view_distance = radius * view.get('fit_margin', 1.15) / math.sin(half)
    elif isinstance(view.get('distance'), (int, float)):
        region3d.view_distance = view['distance']
    if 'overlays' in spec:
        space.overlay.show_overlays = spec['overlays']
    if 'show_gizmo' in spec:
        space.show_gizmo = spec['show_gizmo']
    area.tag_redraw()
    return index, area, previous, target_name

def vp_restore(area, previous):
    space = area.spaces.active
    region3d = space.region_3d
    shading = space.shading
    s = previous['shading']; v = previous['view']
    shading.type = s['type']
    shading.light = s['light']
    if s['studio_light']:
        try:
            shading.studio_light = s['studio_light']
        except TypeError:
            pass
    shading.studiolight_rotate_z = s['studiolight_rotate_z']
    shading.studiolight_intensity = s['studiolight_intensity']
    shading.studiolight_background_alpha = s['studiolight_background_alpha']
    shading.studiolight_background_blur = s['studiolight_background_blur']
    shading.use_scene_lights = s['use_scene_lights']
    shading.use_scene_world = s['use_scene_world']
    region3d.view_perspective = v['perspective']
    region3d.view_location = v['location']
    region3d.view_rotation = v['rotation']
    region3d.view_distance = v['distance']
    space.lens = v['lens']
    space.clip_start = v['clip_start']
    space.clip_end = v['clip_end']
    space.overlay.show_overlays = previous['overlays']
    space.show_gizmo = previous['show_gizmo']
    if previous['area_type'] != 'VIEW_3D':
        area.type = previous['area_type']
'''

INSPECT = RUNTIME + r'''
if bpy.app.background or bpy.context.window is None:
    result = {'gui': False, 'windows': 0, 'areas': [], 'studio_lights': vp_studio_lights(),
              'message': 'background Blender: no viewport can be configured or captured'}
else:
    window = bpy.context.window
    result = {'gui': True, 'windows': len(bpy.context.window_manager.windows), 'screen': window.screen.name,
              'areas': vp_areas(window), 'studio_lights': vp_studio_lights()}
'''

CONFIGURE = RUNTIME + r'''
window = vp_window()
index, area, previous, target_name = vp_apply(window, params['spec'])
result = {'area_index': index, 'previous': previous, 'state': vp_space_state(area), 'target': target_name}
'''

RESTORE = RUNTIME + r'''
window = vp_window()
areas = list(window.screen.areas)
index = params['area_index']
if not 0 <= index < len(areas):
    raise ValueError('VIEWPORT_AREA_MISSING: area ' + str(index))
area = areas[index]
if area.type != 'VIEW_3D':
    area.type = 'VIEW_3D'
vp_restore(area, params['previous'])
restored = vp_space_state(area) if area.type == 'VIEW_3D' else {'area_type': area.type}
result = {'area_index': index, 'restored_type': area.type}
'''

CAPTURE = RUNTIME + r'''
window = vp_window()
scene = bpy.context.scene
index, area, previous, target_name = vp_apply(window, params['spec'])
state = vp_space_state(area)
region = next((r for r in area.regions if r.type == 'WINDOW'), None)
render = scene.render
keep = {'filepath': render.filepath, 'x': render.resolution_x, 'y': render.resolution_y, 'pct': render.resolution_percentage,
        'format': render.image_settings.file_format, 'mode': render.image_settings.color_mode,
        'depth': render.image_settings.color_depth}
media = getattr(render.image_settings, 'media_type', None)
result = {'status': 'ok', 'area_index': index, 'state': state, 'target': target_name, 'files': [], 'errors': [],
          'method': 'viewport_opengl', 'warnings': ['OpenGL viewport render of the configured area, not a desktop screenshot.']}
try:
    if region is None or region.width < 4 or region.height < 4:
        raise ValueError('VIEWPORT_REGION_TOO_SMALL: the VIEW_3D window region is not drawable')
    ratio = min(1.0, params['max_size'] / max(region.width, region.height))
    if media is not None:
        render.image_settings.media_type = 'IMAGE'
    render.image_settings.file_format = 'PNG'
    render.image_settings.color_mode = 'RGBA'
    render.image_settings.color_depth = '8'
    render.resolution_x = max(4, int(region.width * ratio))
    render.resolution_y = max(4, int(region.height * ratio))
    render.resolution_percentage = 100
    render.filepath = params['output_path']
    with bpy.context.temp_override(window=window, area=area, region=region):
        outcome = bpy.ops.render.opengl(write_still=True, view_context=True)
    if 'FINISHED' not in outcome:
        raise RuntimeError('Viewport render did not finish: ' + str(outcome))
    result['files'].append({'path': params['output_path'], 'frame': scene.frame_current})
except Exception as exc:
    result['status'] = 'error'
    result['errors'].append({'type': str(type(exc)), 'message': str(exc)})
finally:
    render.filepath = keep['filepath']; render.resolution_x = keep['x']; render.resolution_y = keep['y']
    render.resolution_percentage = keep['pct']
    if media is not None:
        render.image_settings.media_type = media
    render.image_settings.file_format = keep['format']; render.image_settings.color_mode = keep['mode']
    render.image_settings.color_depth = keep['depth']
    if not params['keep']:
        vp_restore(area, previous)
result['restored'] = not params['keep']
result['after_type'] = area.type
result['after_state'] = vp_space_state(area) if area.type == 'VIEW_3D' else None
result['previous'] = previous
'''


def _number(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("VIEWPORT_SPEC_INVALID: " + name + " must be a number in [" + str(low) + ", " + str(high) + "]")
    return float(value)


def validate_spec(spec) -> dict:
    """Closed schema: unknown keys and out-of-range values are refused, never ignored."""
    spec = dict(spec or {})
    allowed = {"area_index", "allow_area_conversion", "shading", "view", "overlays", "show_gizmo"}
    if set(spec) - allowed:
        raise ValueError("VIEWPORT_SPEC_INVALID: unknown keys " + str(sorted(set(spec) - allowed)))
    if "area_index" in spec and (isinstance(spec["area_index"], bool) or not isinstance(spec["area_index"], int) or spec["area_index"] < 0):
        raise ValueError("VIEWPORT_SPEC_INVALID: area_index must be a non-negative integer")
    for key in ("allow_area_conversion", "overlays", "show_gizmo"):
        if key in spec and not isinstance(spec[key], bool):
            raise ValueError("VIEWPORT_SPEC_INVALID: " + key + " must be boolean")
    shading = dict(spec.get("shading") or {})
    known = {"type", "studio_light", "rotate_z_deg", "intensity", "background_alpha", "background_blur", "use_scene_lights", "use_scene_world"}
    if set(shading) - known:
        raise ValueError("VIEWPORT_SPEC_INVALID: unknown shading keys " + str(sorted(set(shading) - known)))
    if "type" in shading and shading["type"] not in SHADING_TYPES:
        raise ValueError("VIEWPORT_SPEC_INVALID: shading.type must be one of " + str(SHADING_TYPES))
    if "studio_light" in shading and (not isinstance(shading["studio_light"], str) or not shading["studio_light"]):
        raise ValueError("VIEWPORT_SPEC_INVALID: studio_light must be an HDRI name from viewport_inspect")
    if "rotate_z_deg" in shading:
        shading["rotate_z_deg"] = _number("rotate_z_deg", shading["rotate_z_deg"], -180, 180)
    if "intensity" in shading:
        shading["intensity"] = _number("intensity", shading["intensity"], 0, 20)
    for key in ("background_alpha", "background_blur"):
        if key in shading:
            shading[key] = _number(key, shading[key], 0, 1)
    for key in ("use_scene_lights", "use_scene_world"):
        if key in shading and not isinstance(shading[key], bool):
            raise ValueError("VIEWPORT_SPEC_INVALID: " + key + " must be boolean")
    view = dict(spec.get("view") or {})
    known = {"perspective", "orientation", "rotation", "target", "distance", "fit_margin", "lens_mm"}
    if set(view) - known:
        raise ValueError("VIEWPORT_SPEC_INVALID: unknown view keys " + str(sorted(set(view) - known)))
    if "perspective" in view and view["perspective"] not in ("PERSP", "ORTHO"):
        raise ValueError("VIEWPORT_SPEC_INVALID: perspective must be PERSP or ORTHO")
    if "orientation" in view and "rotation" in view:
        raise ValueError("VIEWPORT_SPEC_INVALID: give orientation or rotation, not both")
    if "orientation" in view and view["orientation"] not in ORIENTATIONS:
        raise ValueError("VIEWPORT_SPEC_INVALID: orientation must be one of " + str(ORIENTATIONS))
    if "rotation" in view:
        q = view["rotation"]
        if not isinstance(q, list) or len(q) != 4:
            raise ValueError("VIEWPORT_SPEC_INVALID: rotation is a [w, x, y, z] quaternion")
        view["rotation"] = [_number("rotation", c, -1, 1) for c in q]
        if sum(c * c for c in view["rotation"]) < 1e-12:
            raise ValueError("VIEWPORT_SPEC_INVALID: rotation quaternion is zero")
    if "target" in view:
        target = view["target"]
        if not isinstance(target, dict) or len(target) != 1 or not ({"object"} >= set(target) or {"point"} >= set(target)):
            raise ValueError("VIEWPORT_SPEC_INVALID: target is {object: name} or {point: [x, y, z]}")
        if "point" in target:
            if not isinstance(target["point"], list) or len(target["point"]) != 3:
                raise ValueError("VIEWPORT_SPEC_INVALID: target.point is [x, y, z]")
            view["target"] = {"point": [_number("target.point", c, -1e6, 1e6) for c in target["point"]]}
        elif not isinstance(target["object"], str) or not target["object"]:
            raise ValueError("VIEWPORT_SPEC_INVALID: target.object must be an object name")
    if "distance" in view and view["distance"] != "fit":
        view["distance"] = _number("distance", view["distance"], 1e-4, 1e5)
    if "fit_margin" in view:
        view["fit_margin"] = _number("fit_margin", view["fit_margin"], 1.0, 4.0)
    if "lens_mm" in view:
        view["lens_mm"] = _number("lens_mm", view["lens_mm"], 1, 5000)
    spec["shading"] = shading
    spec["view"] = view
    return spec


def _rounded(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return value


def signature(state: dict) -> str:
    """Hash of the effective viewport state: equal signatures = same shading, HDRI, view and lens
    (floats rounded to 1e-6 so float32 noise does not split equal configurations)."""
    canonical = json.dumps(_rounded({k: state[k] for k in ("shading", "view", "overlays")}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def inspect(bridge) -> dict:
    return bridge.run(INSPECT, {"convertible": list(CONVERTIBLE)})


def configure(bridge, storage, operation_id, spec) -> dict:
    """Persistent configuration; the previous state is stored so viewport_restore can put it back."""
    checked = validate_spec(spec)
    data = bridge.run(CONFIGURE, {"spec": checked, "convertible": list(CONVERTIBLE)})
    folder = storage.reserve("viewport", operation_id)
    record = {"operation_id": operation_id, "spec": checked, "area_index": data["area_index"],
              "previous": data["previous"], "state": data["state"]}
    write_json(folder / "viewport.json", record)
    return {"status": "success", "area_index": data["area_index"], "state": data["state"],
            "signature": signature(data["state"]), "target": data["target"],
            "previous_area_type": data["previous"]["area_type"], "restore_record": str(folder / "viewport.json")}


def restore(bridge, storage, record_path: str) -> dict:
    path = Path(record_path).resolve(strict=True)
    if not path.is_relative_to(storage.root / "viewport") or path.name != "viewport.json":
        raise ValueError("VIEWPORT_RECORD_INVALID: only records created by viewport_configure are accepted")
    record = json.loads(path.read_text(encoding="utf-8"))
    data = bridge.run(RESTORE, {"area_index": record["area_index"], "previous": record["previous"],
                                "convertible": list(CONVERTIBLE)})
    return {"status": "success", **data}


def capture(bridge, storage, operation_id, spec, max_size: int = 1280, keep: bool = False) -> dict:
    """Configure -> OpenGL capture of that exact area -> restore (unless keep=true). Returns the signature."""
    if isinstance(max_size, bool) or not isinstance(max_size, int) or not 16 <= max_size <= 4096:
        raise ValueError("VIEWPORT_SPEC_INVALID: max_size must be an integer in [16, 4096]")
    if not isinstance(keep, bool):
        raise ValueError("VIEWPORT_SPEC_INVALID: keep must be boolean")
    checked = validate_spec(spec)
    folder = storage.reserve("viewport", operation_id)
    output = folder / "viewport.png"
    data = bridge.run(CAPTURE, {"spec": checked, "output_path": str(output), "max_size": max_size, "keep": keep,
                                "convertible": list(CONVERTIBLE)})
    data = _verify_images(data, folder)
    data["signature"] = signature(data["state"])
    if not keep:
        previous, after = data["previous"], data.get("after_state")
        restored = data["after_type"] == previous["area_type"] and (
            after is None or _rounded({k: after[k] for k in ("shading", "view", "overlays")})
            == _rounded({k: previous[k] for k in ("shading", "view", "overlays")}))
        data["state_restored"] = restored
        if not restored:
            data["status"] = "error"
            data["errors"].append({"type": "StateRestorationError", "message": "viewport state differs after capture"})
    write_json(folder / "capture.json", {k: v for k, v in data.items()})
    data["record"] = str(folder / "capture.json")
    if data["status"] == "ok":
        data["status"] = "success"
    return data
