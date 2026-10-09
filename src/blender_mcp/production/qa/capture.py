"""Rendering templates; every bpy payload is checked by upstream safe mode."""

from pathlib import Path
import struct
from typing import Any


_STATE = '''
import bpy

def qa_state(scene):
    render = scene.render
    image = render.image_settings
    return {
        "frame": scene.frame_current, "subframe": scene.frame_subframe,
        "camera": scene.camera.name if scene.camera else None,
        "filepath": render.filepath,
        "resolution_x": render.resolution_x, "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "media_type": getattr(image, "media_type", None),
        "file_format": image.file_format, "color_mode": image.color_mode,
        "color_depth": image.color_depth,
        "use_file_extension": render.use_file_extension,
        "use_border": render.use_border, "use_crop_to_border": render.use_crop_to_border,
        "use_compositing": render.use_compositing, "use_sequencer": render.use_sequencer,
        "use_multiview": render.use_multiview,
    }

def qa_restore(scene, state, camera):
    render = scene.render
    scene.frame_set(state["frame"], subframe=state["subframe"])
    scene.camera = camera
    render.filepath = state["filepath"]
    render.resolution_x = state["resolution_x"]
    render.resolution_y = state["resolution_y"]
    render.resolution_percentage = state["resolution_percentage"]
    if state["media_type"] is not None:
        render.image_settings.media_type = state["media_type"]
    render.image_settings.file_format = state["file_format"]
    render.image_settings.color_mode = state["color_mode"]
    render.image_settings.color_depth = state["color_depth"]
    render.use_file_extension = state["use_file_extension"]
    render.use_border = state["use_border"]
    render.use_crop_to_border = state["use_crop_to_border"]
    render.use_compositing = state["use_compositing"]
    render.use_sequencer = state["use_sequencer"]
    render.use_multiview = state["use_multiview"]

def qa_render_backend(scene, allow_cpu_fallback=False):
    # AUD-015: report the render backend and never claim a GPU render that ran on the CPU.
    # Preferences are only read, never changed; the device list is the one Blender already holds.
    engine = scene.render.engine
    gpu_backend = bpy.context.preferences.system.gpu_backend
    if engine != "CYCLES":
        # Workbench and EEVEE rasterize through Blender's GPU backend; they have no CPU path.
        rasterizer = "GPU rasterizer (" + gpu_backend + ")"
        return {"engine": engine, "requested_backend": "GPU", "available_backend": rasterizer,
                "actual_backend": rasterizer, "fallback_used": False, "warning": None}
    cycles_addon = bpy.context.preferences.addons.get("cycles")
    compute = cycles_addon.preferences.compute_device_type if cycles_addon else "NONE"
    devices = [[d.name, d.type, bool(d.use)] for d in cycles_addon.preferences.devices] if cycles_addon else []
    enabled = [d[0] for d in devices if d[1] == compute and d[2]]
    gpu_types = ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI")
    backend = {"engine": engine, "samples": scene.cycles.samples,
               "requested_backend": ("GPU " + compute) if scene.cycles.device == "GPU" else "CPU",
               "available_backend": compute + ": " + (", ".join(enabled) if enabled else "no enabled device"),
               "devices": devices, "fallback_used": False, "warning": None}
    if scene.cycles.device == "GPU" and compute in gpu_types and enabled:
        backend["actual_backend"] = "GPU " + compute + ": " + ", ".join(enabled)
        if compute != "OPTIX":
            backend["warning"] = "GPU compute is " + compute + ", not OptiX"
        return backend
    if scene.cycles.device != "GPU":
        reason = "the scene renders Cycles on " + scene.cycles.device
    else:
        reason = "no enabled GPU compute device (compute type " + compute + ")"
    if not allow_cpu_fallback:
        code = "RENDER_GPU_REQUIRED" if scene.cycles.device != "GPU" else "RENDER_GPU_UNAVAILABLE"
        raise ValueError(code + ": " + reason + ". QA does not render Cycles on the CPU unless allow_cpu_fallback=true; "
                         "set Render Properties > Device to GPU Compute with a device enabled in Preferences > System, "
                         "or use Workbench (quality=lightweight)")
    backend.update(actual_backend="CPU", fallback_used=True,
                   warning="CPU_FALLBACK_APPROVED: " + reason + "; this is not a GPU render")
    return backend

def qa_configure(scene, width, height):
    render = scene.render
    render.resolution_x = width
    render.resolution_y = height
    render.resolution_percentage = 100
    if hasattr(render.image_settings, "media_type"):
        render.image_settings.media_type = "IMAGE"
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGBA"
    render.image_settings.color_depth = "8"
    render.use_file_extension = True
    render.use_border = False
    render.use_crop_to_border = False
    # Do not execute compositor File Output nodes or other sequence scenes.
    render.use_compositing = False
    render.use_sequencer = False
    render.use_multiview = False
'''


_PREVIEW = _STATE + '''
scene = bpy.context.scene
before = qa_state(scene)
camera = scene.camera
start = params["frame_start"]
end = params["frame_end"]
if start is None and end is None:
    start = scene.frame_current
    end = start
else:
    start = scene.frame_start if start is None else start
    end = scene.frame_end if end is None else end
frames = list(range(start, end + 1, params["step"]))
if not frames or len(frames) > params["max_frames"]:
    raise ValueError("Requested sequence is empty or exceeds max_frames; narrow range or increase step")
if camera is None or camera.type != "CAMERA":
    raise ValueError("Scene has no active camera; preview requires an existing scene camera")
backend = qa_render_backend(scene, params.get("allow_cpu_fallback", False))
result = {"status": "ok", "before": before, "render_backend": backend, "files": [], "warnings": [
    "Compositing, sequencer and multiview are temporarily disabled for bounded QA output.",
    "Blender's Render Result is replaced; scene render settings and frame are restored.",
], "errors": [], "camera": camera.name, "render_engine": scene.render.engine}
try:
    qa_configure(scene, params["width"], params["height"])
    for frame in frames:
        scene.frame_set(frame, subframe=0.0)
        # Camera markers may change scene.camera during frame evaluation.
        scene.camera = camera
        path = params["output_dir"] + "/frame_" + format(frame, "06d") + ".png"
        scene.render.filepath = path
        operator_result = bpy.ops.render.render(write_still=True)
        if "FINISHED" not in operator_result:
            raise RuntimeError("Render operator did not finish: " + str(operator_result))
        result["files"].append({"path": path, "frame": frame})
except Exception as exc:
    result["status"] = "error"
    result["errors"].append({"type": str(type(exc)), "message": str(exc)})
finally:
    qa_restore(scene, before, camera)
result["after"] = qa_state(scene)
result["state_restored"] = result["before"] == result["after"]
result["metrics"] = {"requested_frames": len(frames), "rendered_frames": len(result["files"])}
if not result["state_restored"]:
    result["status"] = "error"
    result["errors"].append({"type": "StateRestorationError", "message": "Scene state differs after QA"})
'''


_BACKEND = _STATE + '''
result = qa_render_backend(bpy.context.scene, params["allow_cpu_fallback"])
'''


def render_backend(bridge, allow_cpu_fallback: bool = False) -> dict:
    """Read-only preflight: the backend a render would use now; raises instead of an unapproved CPU fallback."""
    if not isinstance(allow_cpu_fallback, bool):
        raise ValueError("allow_cpu_fallback must be boolean")
    return bridge.run(_BACKEND, {"allow_cpu_fallback": allow_cpu_fallback})


_VIEWPORT = _STATE + '''
scene = bpy.context.scene
window = bpy.context.window
if bpy.app.background or window is None:
    raise ValueError("Viewport capture requires a Blender GUI with a VIEW_3D area")
area = next((item for item in window.screen.areas if item.type == "VIEW_3D"), None)
if area is None:
    raise ValueError("No VIEW_3D area exists in the active Blender window")
region = next((item for item in area.regions if item.type == "WINDOW"), None)
if region is None or region.width < 1 or region.height < 1:
    raise ValueError("VIEW_3D has no drawable WINDOW region")
ratio = min(1.0, params["max_size"] / max(region.width, region.height))
width = max(4, int(region.width * ratio))
height = max(4, int(region.height * ratio))
before = qa_state(scene)
camera = scene.camera
result = {"status": "ok", "before": before, "files": [], "warnings": [
    "This is a viewport OpenGL render using existing viewport shading, not a desktop screenshot.",
    "Blender's Render Result is replaced; scene render settings and frame are restored.",
], "errors": [], "method": "viewport_opengl"}
try:
    qa_configure(scene, width, height)
    scene.render.filepath = params["output_path"]
    with bpy.context.temp_override(window=window, area=area, region=region):
        operator_result = bpy.ops.render.opengl(write_still=True, view_context=True)
    if "FINISHED" not in operator_result:
        raise RuntimeError("Viewport render did not finish: " + str(operator_result))
    result["files"].append({"path": params["output_path"], "frame": scene.frame_current})
except Exception as exc:
    result["status"] = "error"
    result["errors"].append({"type": str(type(exc)), "message": str(exc)})
finally:
    qa_restore(scene, before, camera)
result["after"] = qa_state(scene)
result["state_restored"] = result["before"] == result["after"]
result["metrics"] = {"requested_width": width, "requested_height": height}
if not result["state_restored"]:
    result["status"] = "error"
    result["errors"].append({"type": "StateRestorationError", "message": "Scene state differs after QA"})
'''


def _integer(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        raise ValueError("QA output paths must be absolute and reserved by the operation workspace")
    if target.is_symlink():
        raise ValueError("QA output path cannot be a symlink")
    return target


def _verify_images(result: dict, directory: Path) -> dict:
    """Verify PNG signature and dimensions, rather than trust operator status."""
    for item in result.get("files", []):
        try:
            path = Path(item["path"])
            if path.parent.resolve() != directory.resolve() or path.is_symlink():
                raise ValueError("Blender returned a file outside the reserved output directory")
            with path.open("rb") as stream:
                header = stream.read(24)
            if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                raise ValueError("Output is not a PNG with a valid IHDR header")
            width, height = struct.unpack(">II", header[16:24])
            if not width or not height:
                raise ValueError("Output PNG dimensions are empty")
            item.update(width=width, height=height, bytes=path.stat().st_size)
        except (OSError, ValueError, KeyError) as exc:
            result["status"] = "error"
            result.setdefault("errors", []).append({"type": type(exc).__name__, "message": str(exc)})
    return result


def capture_viewport(bridge, output_path: str | Path, max_size: int = 1280) -> dict:
    """Capture the active VIEW_3D to a new PNG; GUI is required."""
    _integer("max_size", max_size, 4, 4096)
    target = _path(output_path)
    if target.suffix.lower() != ".png":
        raise ValueError("Viewport output must have a .png extension")
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite QA output: {target}")
    if not target.parent.is_dir():
        raise ValueError("Caller must create and reserve the QA output directory")
    result = bridge.run(_VIEWPORT, {"output_path": str(target), "max_size": max_size})
    return _verify_images(result, target.parent)


def render_preview(
    bridge, output_dir: str | Path, frame_start: int | None = None,
    frame_end: int | None = None, step: int = 1, width: int = 640,
    height: int = 360, max_frames: int = 24, allow_cpu_fallback: bool = False,
) -> dict:
    """Render a bounded PNG sequence using the existing scene camera/engine.

    With both frame arguments omitted, render only the current frame. With one
    omitted, use the corresponding scene range endpoint. Oversized requests
    fail instead of silently truncating. Output directory must exist and be
    empty; the caller owns confinement/reservation of that directory.
    """
    _integer("step", step, 1, 1_000_000)
    _integer("width", width, 4, 4096)
    _integer("height", height, 4, 4096)
    _integer("max_frames", max_frames, 1, 240)
    for name, value in (("frame_start", frame_start), ("frame_end", frame_end)):
        if value is not None:
            _integer(name, value, -1_048_574, 1_048_574)
    if frame_start is not None and frame_end is not None:
        count = len(range(frame_start, frame_end + 1, step))
        if not count or count > max_frames:
            raise ValueError("Requested sequence is empty or exceeds max_frames")
    directory = _path(output_dir)
    if not directory.is_dir() or any(directory.iterdir()):
        raise ValueError("Caller must reserve an empty QA output directory; existing output is never overwritten")
    result = bridge.run(_PREVIEW, {
        "output_dir": str(directory), "frame_start": frame_start,
        "frame_end": frame_end, "step": step, "width": width,
        "height": height, "max_frames": max_frames, "allow_cpu_fallback": allow_cpu_fallback,
    })
    return _verify_images(result, directory)
