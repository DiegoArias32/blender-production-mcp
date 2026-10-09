"""Real full-frame preview throughput on existing controlled M6 candidates.

Only the verified candidate file of probe_m6 is opened. Existing SPLINE Actions
are rendered through the production temporal renderer and its validated bpy
payloads; neither animation generation nor a socket/UI controller is started.
Timings include checkpoint, three-view PNG rendering, verification and APNG
packaging. They do not imply artistic acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import types
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / "test-artifacts/m6/fixtures-17b00acbeb/report.json"
CASES = (("standard_humanoid", 30, 1), ("medium_rig", 30, 3), ("large_rig", 60, 10))


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def controlled_source(report_path):
    path = Path(report_path).resolve(strict=True)
    fixtures_root = (ROOT / "test-artifacts/m6").resolve(strict=True)
    if not path.is_relative_to(fixtures_root) or path.name != "report.json":
        raise ValueError("Only a report inside this repository's controlled M6 fixture store is accepted")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("status") != "pass":
        raise ValueError("The source fixture qualification must have passed")
    candidate = Path(report["generation_adapter"]["candidate_scene"]).resolve(strict=True)
    if candidate.parent != path.parent or candidate.name != "m6-adapter-candidates.blend":
        raise ValueError("Only the recorded controlled adapter-candidates file is accepted")
    expected = report["generation_adapter"]["candidate_scene_sha256"]
    if file_sha256(candidate) != expected:
        raise ValueError("Candidate source file no longer matches its qualification digest")
    return report, candidate, expected


def inside(source_report, output, workdir, concurrency_note):
    import bpy

    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("Preview benchmarks require a fresh headless factory-startup process")
    fixture_report, candidate, expected = controlled_source(source_report)
    outcome = bpy.ops.wm.open_mainfile(filepath=str(candidate), load_ui=False, use_scripts=False)
    if "FINISHED" not in outcome or Path(bpy.data.filepath).resolve() != candidate:
        raise RuntimeError("Blender did not open the verified controlled candidate file")
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "tests/blender"))
    package = types.ModuleType("blender_mcp")
    package.__path__ = [str(ROOT / "src/blender_mcp")]
    sys.modules["blender_mcp"] = package
    from fixtures_m6 import activate_fixture, GROUND_HEIGHT_M
    from blender_mcp.production.common.bridge import build_code, payload_bytes
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME
    from blender_mcp.production.animation.generation.runtime import GENERATION_RUNTIME, COMPAT_READ
    from blender_mcp.production.animation.generation.planner import create_brief, plan_poses
    from blender_mcp.production.animation.generation.render import temporal

    class InsideBridge:
        """The same safe payload/JSON boundary used by the fixture probe."""
        calls = 0
        maximum_payload_bytes = 0

        def run(self, body, params=None):
            self.calls += 1
            self.maximum_payload_bytes = max(self.maximum_payload_bytes, payload_bytes(body, params))
            namespace = {}
            code = build_code(body, params)
            exec(code.rsplit("print(", 1)[0], namespace)
            return json.loads(json.dumps(namespace["result"], allow_nan=False))

    def action_digest(action):
        curves = ([curve for layer in action.layers for strip in layer.strips
                   for bag in strip.channelbags for curve in bag.fcurves]
                  if action.is_action_layered else list(action.fcurves))
        value = {"name": action.name, "custom": dict(action.items()), "curves": [
            {"path": curve.data_path, "index": curve.array_index,
             "keys": [{"co": list(key.co), "left": list(key.handle_left), "right": list(key.handle_right),
                       "interpolation": key.interpolation, "left_type": key.handle_left_type,
                       "right_type": key.handle_right_type} for key in curve.keyframe_points]}
            for curve in curves]}
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()

    def independent_state(rig):
        scene = bpy.context.scene
        ad = rig.animation_data
        value = {"frame": scene.frame_current, "subframe": scene.frame_subframe,
                 "frame_range": [scene.frame_start, scene.frame_end],
                 "fps": [scene.render.fps, scene.render.fps_base],
                 "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
                 "selected": sorted(obj.name for obj in bpy.context.selected_objects),
                 "action": ad.action.name if ad and ad.action else None,
                 "slot": ad.action_slot.identifier if ad and ad.action_slot else None,
                 "file": bpy.data.filepath, "engine": scene.render.engine,
                 "camera": scene.camera.name if scene.camera else None,
                 "visibility": {obj.name: [obj.hide_render, obj.hide_get()] for obj in scene.objects},
                 "pose": {bone.name: {"location": list(bone.location), "scale": list(bone.scale),
                                      "mode": bone.rotation_mode, "quaternion": list(bone.rotation_quaternion),
                                      "euler": list(bone.rotation_euler), "axis_angle": list(bone.rotation_axis_angle)}
                          for bone in rig.pose.bones}}
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()

    output, workdir = Path(output), Path(workdir)
    bridge, storage = InsideBridge(), Storage(workdir)
    started = time.perf_counter()
    report = {"schema": 1, "status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
              "scope": "full_temporal_preview_throughput_only_no_artistic_acceptance",
              "source_report": str(Path(source_report).resolve()), "source_candidate": str(candidate),
              "source_candidate_sha256": expected, "blender": bpy.app.version_string,
              "blender_hash": bpy.app.build_hash.decode("ascii"), "cases": [], "checks": [],
              "artifact_store": str(workdir), "concurrency_note": concurrency_note,
              "warnings": ["96x96 lightweight previews measure throughput; production resolution costs more.",
                           "Blender Render Result is replaced; only scene/pose/Action state is restored.",
                           "No artistic or animation acceptance is asserted by this benchmark."]}

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")

    def check(name, value, data=None):
        report["checks"].append({"name": name, "passed": bool(value), "metrics": data})
        save()
        if not value:
            raise AssertionError(name + (": " + repr(data) if data is not None else ""))
        print("PASS " + name, flush=True)

    originals = {action.name: action_digest(action) for action in bpy.data.actions}
    try:
        check("opened candidate is fixture-only", all(bool(bpy.data.objects[row["object"]].get("fixture_only"))
              for row in fixture_report["fixtures"]["scenarios"].values()))
        check("source candidate digest verified before preview", file_sha256(candidate) == expected)
        for scenario, fps, seconds in CASES:
            row = activate_fixture(fixture_report["fixtures"], scenario)
            rig = bpy.data.objects[row["object"]]
            recorded = next(case for case in fixture_report["generation_adapter"]["performance"]
                            if case["scenario"] == scenario and case["fps"] == fps and case["seconds"] == seconds)
            action_name = recorded["actions"][-1]
            action = bpy.data.actions.get(action_name)
            label = "%s-%sfps-%ss" % (scenario, fps, seconds)
            check(label + " recorded SPLINE Action exists", bool(action and action.get("production_generation_stage") == "SPLINE"))
            # Deliberate fixture-only setup occurs before the renderer checkpoint.
            scene = bpy.context.scene
            scene.render.fps, scene.render.fps_base = fps, 1.0
            scene.frame_start, scene.frame_end = 1, 1 + fps * seconds
            scene.frame_set(2, subframe=.25)
            scene.camera = bpy.data.objects[fixture_report["fixtures"]["stage"]["cameras"]["FRONT"]]
            bpy.context.view_layer.update()
            brief = create_brief({"type": "IDLE", "profile": row["profile"], "fps": fps,
                                  "duration_s": seconds, "root_mode": "IN_PLACE", "strength": .6,
                                  "forward_axis": "-Y"})
            options = {"mapping": row["mapping"], "brief": brief, "ground_height_m": GROUND_HEIGHT_M}
            compatibility = bridge.run(BASE_RUNTIME + GENERATION_RUNTIME + COMPAT_READ,
                                       {"object_name": row["object"], "options": options})
            check(label + " actual compatibility and planner counts", compatibility["supported"]
                  and compatibility["bone_count"] == recorded["bones"] and compatibility["controlled_bone_count"] == 13)
            pose_plan = plan_poses(brief, compatibility)
            check(label + " complete planner range matches recorded Action", pose_plan["supported"]
                  and list(action.frame_range) == [brief["frame_start"], brief["frame_end"]])
            before = independent_state(rig)
            begin = time.perf_counter()
            result = temporal(bridge, storage, "m6-preview-perf-" + label,
                              {"object_name": row["object"], "brief": brief, "pose_plan": pose_plan},
                              action_name, {"width": 96, "height": 96, "views": ["FRONT", "SIDE", "THREE_QUARTER"],
                                            "framing_meshes": [row["body"]], "quality": "lightweight"})
            elapsed = time.perf_counter() - begin
            case = {"scenario": scenario, "fps": fps, "duration_s": seconds, "bones": len(rig.data.bones),
                    "action": action_name, "brief": brief, "resolution": [96, 96], "step": 1,
                    "expected_frames_per_view": 1 + fps * seconds, "expected_images": 3 * (1 + fps * seconds),
                    "elapsed_seconds_preview_checkpoint_render_verify_apng": round(elapsed, 6),
                    "elapsed_ms_m6": result.get("metrics", {}).get("elapsed_ms_m6"),
                    "actual_images": len(result["files"]), "actual_frames_per_view": result["metrics"]["frames_per_view"],
                    "temporal_complete": result["temporal_complete"], "state_restored": result["state_restored"],
                    "pose_state_restored": result.get("pose_state_restored"),
                    "independent_state_restored": before == independent_state(rig),
                    "checkpoint": result["checkpoint"], "result": str(Path(result["output_dir"]) / "result.json"),
                    "animations": result["animations"], "render_backends": [series["render_backend"] for series in result["series"]]}
            report["cases"].append(case)
            check(label + " actual full temporal preview succeeded", result["status"] == "ok", result.get("errors"))
            check(label + " all frames and three views rendered", result["temporal_complete"]
                  and len(result["files"]) == case["expected_images"]
                  and all([item["frame"] for item in series["files"]] == list(range(1, 2 + fps * seconds))
                          for series in result["series"]))
            check(label + " renderer and independent pose/Action/frame state restored", result["state_restored"]
                  and result.get("pose_state_restored") and case["independent_state_restored"])
            check(label + " verified animated artifacts and checkpoint exist", len(result["animations"]) == 3
                  and all(file_sha256(item["path"]) == item["sha256"] for item in result["animations"])
                  and Path(result["checkpoint"]).is_file())
            check(label + " all source/candidate Actions unchanged", originals == {
                action.name: action_digest(action) for action in bpy.data.actions})
            check(label + " candidate source file remains byte-identical", file_sha256(candidate) == expected)
        report["status"] = "pass"
    except Exception as exc:
        report["status"] = "fail"
        report["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        raise
    finally:
        report["elapsed_seconds_total"] = round(time.perf_counter() - started, 6)
        report["safe_payload_calls"] = bridge.calls
        report["maximum_safe_payload_bytes"] = bridge.maximum_payload_bytes
        report["count"] = len(report["checks"])
        report["source_candidate_sha256_after"] = file_sha256(candidate)
        save()


def run(blender, source_report, concurrency_note):
    source, candidate, expected = controlled_source(source_report)
    blender = Path(blender).resolve(strict=True)
    output = ROOT / "test-artifacts/m6" / ("preview-performance-" + uuid4().hex[:10])
    workdir = Path(tempfile.gettempdir()) / ("bpm6perf-" + uuid4().hex[:10])
    output.mkdir(parents=True, exist_ok=False)
    workdir.mkdir(parents=True, exist_ok=False)
    args = [str(blender), "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1",
            "--python", str(Path(__file__).resolve()), "--", "--inside", "--source-report", str(Path(source_report).resolve()),
            "--output", str(output), "--workdir", str(workdir), "--concurrency-note", concurrency_note]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (output / "blender.log").open("x", encoding="utf-8") as log:
        process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        print(json.dumps({"status": "running", "pid": process.pid, "report": str(output / "report.json"),
                          "source": str(candidate), "source_sha256": expected, "artifact_store": str(workdir)}), flush=True)
        code = process.wait(timeout=3600)
    if code:
        raise RuntimeError("Preview performance failed; real evidence: " + str(output))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    print(json.dumps({"status": report["status"], "count": report["count"], "report": str(output / "report.json"),
                      "cases": [{"scenario": row["scenario"], "images": row["actual_images"],
                                 "elapsed_seconds": row["elapsed_seconds_preview_checkpoint_render_verify_apng"]}
                                for row in report["cases"]]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--blender", default=r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe")
    parser.add_argument("--source-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--concurrency-note", default="Own headless preview process; no controlled comparison of concurrent system load.")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
    if args.inside:
        inside(args.source_report, args.output, args.workdir, args.concurrency_note)
    else:
        run(args.blender, args.source_report, args.concurrency_note)
