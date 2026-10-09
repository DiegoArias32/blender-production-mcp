"""Verify real QA rendering in a temporary factory-startup Blender process.

Run from the repository venv:
    python tests/blender/qa_probe.py --blender PATH_TO_BLENDER

No sockets, GUI control, production assets or preference writes are involved.
The script verifies VIDEO -> PNG -> VIDEO where media_type exists, state
restoration, real PNG pixels, and refusal without GUI or an active camera.
"""

import argparse
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import zlib

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.qa import capture_viewport, render_preview
from blender_mcp.production.qa.capture import _verify_images


class RecordingBridge:
    """Collect the same validated payloads used by public production services."""

    def __init__(self):
        self.payloads = []

    def run(self, body, params):
        self.payloads.append(build_code(body, params))
        return {}


def _expected_refusal(code, message, flag):
    return (f"{flag} = False\ntry:\n"
            + "\n".join("    " + line for line in code.splitlines())
            + f"\nexcept ValueError as qa_error:\n    {flag} = {message!r} in str(qa_error)\n"
            + f"assert {flag}, {('Expected explicit refusal: ' + message)!r}\n")


def build_probe(directory):
    frames = directory / "frames"
    frames.mkdir()
    missing_camera = directory / "missing-camera"
    missing_camera.mkdir()
    recorder = RecordingBridge()
    render_preview(recorder, frames, frame_start=1, frame_end=3, step=2, width=160, height=96)
    capture_viewport(recorder, directory / "headless-must-not-exist.png")
    render_preview(recorder, missing_camera, width=160, height=96)
    setup = """
import bpy, json
qa_scene = bpy.context.scene
qa_scene.render.engine = "BLENDER_WORKBENCH"
qa_scene.frame_set(7, subframe=0.25)
qa_scene.render.resolution_x = 900
qa_scene.render.resolution_y = 700
qa_scene.render.resolution_percentage = 50
qa_scene.render.use_border = True
qa_scene.render.use_crop_to_border = True
qa_scene.render.use_file_extension = False
qa_media_type_available = hasattr(qa_scene.render.image_settings, "media_type")
if qa_media_type_available:
    qa_scene.render.image_settings.media_type = "VIDEO"
qa_scene.render.image_settings.file_format = "FFMPEG"
qa_original_camera = qa_scene.camera
qa_original_engine = qa_scene.render.engine
"""
    setup += "qa_scene.render.filepath = " + repr(str(directory / "must-not-write-original.mp4")) + "\n"
    checks = [
        "real preview produces two requested frames",
        "all tracked render settings restored after render",
        "VIDEO and FFMPEG output restored",
        "frame 7 and subframe 0.25 restored",
        "existing scene camera and engine preserved",
        "viewport explicitly refuses headless Blender",
        "headless refusal leaves scene state unchanged",
        "preview explicitly refuses missing camera",
        "missing camera refusal leaves scene state unchanged",
    ]
    script = setup + recorder.payloads[0] + """
assert result["status"] == "ok", result
assert len(result["files"]) == 2, result
assert result["state_restored"] and result["before"] == result["after"], result
assert qa_scene.render.image_settings.file_format == "FFMPEG"
if qa_media_type_available:
    assert qa_scene.render.image_settings.media_type == "VIDEO"
assert qa_scene.frame_current == 7 and abs(qa_scene.frame_subframe - 0.25) < 1e-6
assert qa_scene.camera == qa_original_camera
assert qa_scene.render.engine == qa_original_engine
qa_preview_result = result
qa_before_refusal = qa_state(qa_scene)
"""
    script += _expected_refusal(recorder.payloads[1], "requires a Blender GUI", "qa_headless_refused")
    script += "assert qa_state(qa_scene) == qa_before_refusal\n"
    script += "qa_scene.camera = None\nqa_before_missing_camera = qa_state(qa_scene)\n"
    script += _expected_refusal(recorder.payloads[2], "no active camera", "qa_camera_refused")
    script += "assert qa_state(qa_scene) == qa_before_missing_camera\n"
    script += ('print("QA_PROBE_RESULT=" + json.dumps({"status":"passed",'
               '"blender_version":bpy.app.version_string,"media_type_available":qa_media_type_available,'
               '"preview":qa_preview_result,"checks":' + repr(checks) + '}))\n')
    return script, checks


def verify_complete_png(path):
    """Check chunk CRCs and decompress pixels; a plausible header is insufficient."""
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"Invalid PNG signature: {path.name}")
    offset = 8
    chunks = []
    compressed = []
    width = height = depth = color_type = None
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:offset + 8 + length]
        crc = raw[offset + 8 + length:offset + 12 + length]
        if len(data) != length or len(crc) != 4:
            raise AssertionError(f"Truncated PNG chunk: {path.name}")
        if struct.unpack(">I", crc)[0] != zlib.crc32(kind + data):
            raise AssertionError(f"PNG CRC mismatch: {path.name}")
        chunks.append(kind)
        if kind == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if (depth, color_type, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise AssertionError(f"Unexpected PNG encoding: {path.name}")
        if kind == b"IDAT":
            compressed.append(data)
        offset += 12 + length
        if kind == b"IEND":
            break
    if not chunks or chunks[0] != b"IHDR" or chunks[-1] != b"IEND" or offset != len(raw):
        raise AssertionError(f"Incomplete PNG structure: {path.name}")
    pixels = zlib.decompress(b"".join(compressed))
    if len(pixels) != height * (width * 4 + 1):
        raise AssertionError(f"Incomplete decoded PNG pixel buffer: {path.name}")
    return width, height


def run(blender):
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="blender-production-qa-probe-") as temporary:
        directory = Path(temporary).resolve()
        script, checks = build_probe(directory)
        script_path = directory / "probe.py"
        script_path.write_text(script, encoding="utf-8")
        command = [str(blender), "--background", "--factory-startup", "--disable-autoexec",
                   "--python-exit-code", "1", "--python", str(script_path)]
        process = subprocess.run(command, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=180)
        if process.returncode:
            raise RuntimeError(f"Blender exited {process.returncode}\n{process.stdout}\n{process.stderr}")
        marker = "QA_PROBE_RESULT="
        lines = [line[len(marker):] for line in process.stdout.splitlines() if line.startswith(marker)]
        if len(lines) != 1:
            raise RuntimeError("Missing unique Blender QA verification result\n" + process.stdout)
        report = json.loads(lines[0])
        preview = _verify_images(report["preview"], directory / "frames")
        if preview["status"] != "ok":
            raise AssertionError(preview)
        expected_names = ["frame_000001.png", "frame_000003.png"]
        if [Path(item["path"]).name for item in preview["files"]] != expected_names:
            raise AssertionError("Wrong preview frame filenames")
        if sorted(path.name for path in (directory / "frames").iterdir()) != expected_names:
            raise AssertionError("Unexpected output written by preview")
        for item in preview["files"]:
            if (item["width"], item["height"]) != (160, 96):
                raise AssertionError("Preview dimensions were not verified correctly")
            if verify_complete_png(Path(item["path"])) != (160, 96):
                raise AssertionError("Actual PNG dimensions do not match requested dimensions")
        if (directory / "headless-must-not-exist.png").exists() or any((directory / "missing-camera").iterdir()):
            raise AssertionError("Refused QA operations unexpectedly wrote files")
        if (directory / "must-not-write-original.mp4").exists():
            raise AssertionError("QA unexpectedly wrote the original render target")
        checks += [
            "exact frame filenames and no extra preview outputs",
            "host verifies actual 160x96 PNG dimensions",
            "complete PNG chunks, CRCs and pixel buffers verified",
            "refused operations create no image outputs",
            "original video render target remains untouched",
        ]
        result = {"status": "passed", "blender_version": report["blender_version"],
                  "media_type_available": report["media_type_available"],
                  "check_count": len(checks), "checks": checks,
                  "elapsed_seconds": round(time.perf_counter() - started, 3)}
        print(json.dumps(result, indent=2))
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", required=True, type=Path)
    run(parser.parse_args().blender)
