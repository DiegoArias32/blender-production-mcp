"""QA safety tests independent of Blender; real rendering is tested separately."""

from contextlib import nullcontext
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from blender_mcp.production.qa import capture_viewport, render_preview
from blender_mcp.production.qa.capture import _PREVIEW, _VIEWPORT
from blender_mcp.production.common.bridge import build_code
from blender_mcp.safe_mode import validate_code


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * width * 4) * height)) + chunk(b"IEND", b""))


class FakeBridge:
    """Execute the exact templates against a small stateful bpy double."""

    def __init__(self, *, camera=True, background=False, cancel=False, missing_file=False, cycles=None):
        self.calls = 0
        self.render_calls = 0
        self.cancel = cancel
        self.missing_file = missing_file
        render = SimpleNamespace(
            filepath="original.exr", resolution_x=1800, resolution_y=1000,
            resolution_percentage=67, image_settings=SimpleNamespace(
                media_type="IMAGE", file_format="OPEN_EXR", color_mode="RGB", color_depth="32"),
            use_file_extension=False, use_border=True, use_crop_to_border=True,
            use_compositing=True, use_sequencer=True, use_multiview=True,
            engine="TEST_ENGINE",
        )
        self.camera = SimpleNamespace(name="ExistingCamera", type="CAMERA") if camera else None
        self.scene = SimpleNamespace(
            render=render, camera=self.camera, frame_current=7, frame_subframe=0.25,
            frame_start=1, frame_end=250,
        )
        self.scene.frame_set = self.frame_set
        region = SimpleNamespace(type="WINDOW", width=900, height=600)
        area = SimpleNamespace(type="VIEW_3D", regions=[region])
        window = SimpleNamespace(screen=SimpleNamespace(areas=[area]))
        # cycles=(scene_device, compute_device_type, optix_in_use) switches the fake scene to Cycles.
        addons = {}
        if cycles is not None:
            render.engine = "CYCLES"
            self.scene.cycles = SimpleNamespace(device=cycles[0], samples=16)
            devices = [SimpleNamespace(name="RTX", type="OPTIX", use=cycles[2]),
                       SimpleNamespace(name="CPU", type="CPU", use=False)]
            addons["cycles"] = SimpleNamespace(preferences=SimpleNamespace(compute_device_type=cycles[1], devices=devices))
        preferences = SimpleNamespace(system=SimpleNamespace(gpu_backend="OPENGL"), addons=addons)
        self.bpy = SimpleNamespace(
            context=SimpleNamespace(scene=self.scene, window=window, preferences=preferences,
                                    temp_override=lambda **_: nullcontext()),
            app=SimpleNamespace(background=background),
            ops=SimpleNamespace(render=SimpleNamespace(render=self.render, opengl=self.render)),
        )

    def frame_set(self, frame, subframe=0.0):
        self.scene.frame_current = frame
        self.scene.frame_subframe = subframe
        self.scene.camera = SimpleNamespace(name="MarkerCamera", type="CAMERA")

    def render(self, **kwargs):
        self.render_calls += 1
        if self.scene.camera is not self.camera:
            raise AssertionError("QA did not preserve the chosen camera")
        render = self.scene.render
        if render.use_compositing or render.use_sequencer or render.use_multiview:
            raise AssertionError("Secondary output paths were not disabled")
        if self.cancel:
            return {"CANCELLED"}
        if not self.missing_file:
            Path(render.filepath).write_bytes(png(render.resolution_x, render.resolution_y))
        return {"FINISHED"}

    def run(self, body, params):
        self.calls += 1
        source = "import bpy\nimport json\nparams = json.loads(" + repr(json.dumps(params)) + ")\n" + body
        validate_code(source)
        namespace = {}
        with patch.dict("sys.modules", {"bpy": self.bpy}):
            exec(source, namespace)
        return namespace["result"]


class QATests(unittest.TestCase):
    def test_templates_validate_without_disabling_safe_mode(self):
        for body in (_VIEWPORT, _PREVIEW):
            validate_code(build_code(body, {}))

    def test_sequence_files_dimensions_and_camera_state_restored(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            result = render_preview(bridge, directory, 2, 6, 2, 64, 40)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["state_restored"])
            self.assertEqual([Path(item["path"]).name for item in result["files"]],
                             ["frame_000002.png", "frame_000004.png", "frame_000006.png"])
            self.assertEqual([(item["width"], item["height"]) for item in result["files"]], [(64, 40)] * 3)
        self.assertEqual(bridge.scene.frame_current, 7)
        self.assertEqual(bridge.scene.frame_subframe, 0.25)
        self.assertIs(bridge.scene.camera, bridge.camera)
        self.assertEqual(bridge.scene.render.image_settings.file_format, "OPEN_EXR")
        self.assertEqual(bridge.scene.render.filepath, "original.exr")

    def test_operator_cancellation_restores_all_settings(self):
        bridge = FakeBridge(cancel=True)
        with tempfile.TemporaryDirectory() as directory:
            result = render_preview(bridge, directory)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["before"], result["after"])
        self.assertTrue(result["errors"])
        self.assertEqual(result["files"], [])

    def test_new_blender_video_media_type_is_restored(self):
        bridge = FakeBridge()
        bridge.scene.render.image_settings.media_type = "VIDEO"
        bridge.scene.render.image_settings.file_format = "FFMPEG"
        with tempfile.TemporaryDirectory() as directory:
            result = render_preview(bridge, directory)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["state_restored"])
        self.assertEqual(bridge.scene.render.image_settings.media_type, "VIDEO")
        self.assertEqual(bridge.scene.render.image_settings.file_format, "FFMPEG")

    def test_missing_camera_refuses_before_mutation(self):
        bridge = FakeBridge(camera=False)
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "no active camera"):
            render_preview(bridge, directory)
        self.assertEqual(bridge.render_calls, 0)
        self.assertEqual(bridge.scene.render.filepath, "original.exr")
        self.assertEqual(bridge.scene.frame_subframe, 0.25)

    def test_oversized_scene_range_refuses_before_mutation(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "max_frames"):
            render_preview(bridge, directory, frame_start=1)
        self.assertEqual(bridge.render_calls, 0)
        self.assertEqual(bridge.scene.render.filepath, "original.exr")

    def test_non_cycles_engine_reports_gpu_rasterizer(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            result = render_preview(bridge, directory)
        self.assertEqual(result["render_backend"]["actual_backend"], "GPU rasterizer (OPENGL)")
        self.assertIs(result["render_backend"]["fallback_used"], False)

    def test_cycles_on_cpu_is_refused_before_rendering(self):
        for cycles, code in ((("CPU", "OPTIX", True), "RENDER_GPU_REQUIRED"),
                             (("GPU", "NONE", True), "RENDER_GPU_UNAVAILABLE"),
                             (("GPU", "OPTIX", False), "RENDER_GPU_UNAVAILABLE")):
            bridge = FakeBridge(cycles=cycles)
            with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, code):
                render_preview(bridge, directory)
            self.assertEqual(bridge.render_calls, 0)
            self.assertEqual(bridge.scene.render.filepath, "original.exr")

    def test_cycles_on_enabled_optix_renders_and_reports_device(self):
        bridge = FakeBridge(cycles=("GPU", "OPTIX", True))
        with tempfile.TemporaryDirectory() as directory:
            result = render_preview(bridge, directory)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["render_backend"]["actual_backend"], "GPU OPTIX: RTX")
        self.assertEqual(result["render_backend"]["requested_backend"], "GPU OPTIX")
        self.assertIsNone(result["render_backend"]["warning"])

    def test_cpu_fallback_only_when_explicitly_approved_and_reported(self):
        bridge = FakeBridge(cycles=("CPU", "NONE", False))
        with tempfile.TemporaryDirectory() as directory:
            result = render_preview(bridge, directory, allow_cpu_fallback=True)
        self.assertEqual(result["status"], "ok")
        backend = result["render_backend"]
        self.assertEqual(backend["actual_backend"], "CPU")
        self.assertIs(backend["fallback_used"], True)
        self.assertIn("CPU_FALLBACK_APPROVED", backend["warning"])

    def test_existing_outputs_never_overwritten(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "important.png"
            existing.write_bytes(b"keep me")
            with self.assertRaises(FileExistsError):
                capture_viewport(bridge, existing)
            with self.assertRaises(ValueError):
                render_preview(bridge, directory)
            self.assertEqual(existing.read_bytes(), b"keep me")
        self.assertEqual(bridge.calls, 0)

    def test_headless_capture_has_explicit_error(self):
        bridge = FakeBridge(background=True)
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "requires a Blender GUI"):
            capture_viewport(bridge, Path(directory) / "capture.png")
        self.assertEqual(bridge.render_calls, 0)
        self.assertEqual(bridge.scene.render.filepath, "original.exr")

    def test_viewport_aspect_ratio_and_restoration(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            result = capture_viewport(bridge, Path(directory) / "capture.png", max_size=90)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["state_restored"])
        self.assertEqual((result["files"][0]["width"], result["files"][0]["height"]), (90, 60))

    def test_finished_operator_without_output_is_error(self):
        bridge = FakeBridge(missing_file=True)
        with tempfile.TemporaryDirectory() as directory:
            result = render_preview(bridge, directory)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["state_restored"])
        self.assertTrue(result["errors"])

    def test_invalid_arguments_do_not_reach_blender(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as directory:
            for values in ({"step": 0}, {"width": True}, {"frame_start": 3, "frame_end": 1}, {"height": 9000}):
                with self.assertRaises(ValueError):
                    render_preview(bridge, directory, **values)
        self.assertEqual(bridge.calls, 0)


if __name__ == "__main__":
    unittest.main()
