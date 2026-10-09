"""Bounded cold-process M6 threshold qualification on an owned fixture/port.

This runner performs no render or artistic acceptance. It checks configuration
propagation through actual MCP, the saved M3 proposal and integrated Blender QA.
"""
from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from run_m5_mcp import Run, listening, session
from blender_mcp.production.animation.generation import store
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.m3_transaction import BASE_RUNTIME
from blender_mcp.production.common.storage import Storage, sha256

ROOT = Path(__file__).resolve().parents[2]
PORT = 9878
ROOT_LOOP_THRESHOLDS = {
    "root_position_tolerance_m": .011,
    "root_angle_tolerance_deg": .9,
    "root_max_step_m": .23,
    "root_vertical_tolerance_m": .04,
    "loop_position_tolerance_m": .012,
    "loop_angle_tolerance_deg": .8,
    "loop_velocity_tolerance_m_s": .09,
    "loop_angular_velocity_tolerance_deg_s": 9.,
}


def control(run, info, body, params=None):
    return run.bridge.run(
        "import bpy\nif bpy.data.filepath!=params['fixture']: raise ValueError('OWNED_FIXTURE_REQUIRED')\n" + body,
        dict(params or {}, fixture=info["blend"]),
    )


def snapshot(run, info, spec):
    return control(run, info, BASE_RUNTIME + r'''
obj=bpy.data.objects[params['object']]
source=bpy.data.actions[params['source']]
result={'source_action_fingerprint':m2_digest(m3_source_snapshot(source)),
        'active_action':obj.animation_data.action.name if obj.animation_data.action else None,
        'action_names':sorted(action.name for action in bpy.data.actions),
        'frame':bpy.context.scene.frame_current,'filepath':bpy.data.filepath}
''', {"object": spec["object"], "source": spec["action"]})


async def scenario(run, info):
    spec = info["scenarios"]["standard_humanoid"]
    before = snapshot(run, info, spec)
    run.check("isolated_fixture_loaded", before["filepath"] == info["blend"])
    inventory = await run.client.list_tools()
    run.check("cold_server_inventory_155", len(inventory.tools) == 155)
    await run.good("asset_register_fixture", {"path": info["blend"]})
    options = dict(ROOT_LOOP_THRESHOLDS, mapping=spec["mapping"],
                   ground_height_m=spec["ground_height_m"], meshes=[spec["body"]],
                   contact_tolerance_m=.001, sliding_tolerance_m=.002)
    intent = {"type": "IDLE", "profile": "HUMAN", "fps": 30, "duration_s": 1,
              "root_mode": "IN_PLACE", "forward_axis": "-Y"}
    planned = await run.good("animation_generation_plan", {
        "object_name": spec["object"], "intent": intent, "options": options,
    })
    run.check("configured_plan_supported_nonmutating", planned["supported"] is True
              and planned["no_action_created"] is True
              and snapshot(run, info, spec)["action_names"] == before["action_names"])
    run.check("plan_preserves_all_configured_thresholds", all(
        planned["options"].get(key) == value for key, value in options.items()
        if key in ROOT_LOOP_THRESHOLDS or key in {"contact_tolerance_m", "sliding_tolerance_m"}
    ))
    generated = await run.good("animation_generate_pose", {
        "generation": planned["generation"], "options": {"pose_index": 0}, "apply": True,
    })
    run.check("real_pose_candidate_and_source_preserved", generated["status"] == "candidate_created"
              and generated["source_intact"] is True
              and generated["candidate_action"] != spec["action"])
    _, saved_plan, state = store.load(Storage(run.folder / "s"), planned["generation"])
    operation_path = Path(state["last_operation"])
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    run.check("saved_m3_proposal_inherits_contact_and_sliding", operation["options"]["contact_tolerance_m"] == .001
              and operation["options"]["sliding_tolerance_m"] == .002,
              operation=str(operation_path))
    metadata_keys = ["production_generation_" + name for name in (
        "id", "operation_id", "mcp_version", "profile", "rig_fingerprint", "generation_ref",
        "brief_ref", "qa_result", "qa_reference", "lifecycle", "created_utc",
    )]
    metadata = control(run, info, r'''
action=bpy.data.actions[params['action']]
result={key:action.get(key) for key in params['keys']}
''', {"action": generated["candidate_action"], "keys": metadata_keys})
    expected_metadata = {
        "production_generation_id": saved_plan["generation_id"],
        "production_generation_operation_id": operation["options"]["operation_id"],
        "production_generation_mcp_version": version("blender-production-mcp"),
        "production_generation_profile": "HUMAN",
        "production_generation_rig_fingerprint": saved_plan["rig_fingerprint"],
        "production_generation_generation_ref": planned["generation"],
        "production_generation_brief_ref": planned["generation"],
        "production_generation_lifecycle": "GENERATED_CANDIDATE",
        "production_generation_created_utc": saved_plan["created_at"],
    }
    run.check("candidate_identity_metadata_matches_saved_operation", all(
        metadata.get(key) == value for key, value in expected_metadata.items()), metadata=metadata)
    run.check("candidate_qa_metadata_matches_actual_integrated_result", metadata["production_generation_qa_result"]
              == ("PASS" if generated["technical_qa"]["passed"] else "FAIL"))
    qa_reference = metadata.get("production_generation_qa_reference")
    run.check("candidate_qa_reference_is_existing_generation_artifact", qa_reference == planned["generation"]
              and Path(qa_reference).is_file() and saved_plan["generation_ref"] == planned["generation"])
    run.check("candidate_metadata_is_compact", all(isinstance(value, str) and len(value.encode("utf-8")) <= 2048
              for value in metadata.values()) and len(json.dumps(metadata).encode("utf-8")) <= 5000)
    thresholds = generated["technical_qa"]["thresholds"]
    run.check("qa_reads_exact_eight_root_loop_thresholds", all(
        thresholds.get(key) == value for key, value in ROOT_LOOP_THRESHOLDS.items()
    ), expected=ROOT_LOOP_THRESHOLDS, actual=thresholds)
    run.check("qa_reads_configured_sliding", thresholds["sliding_tolerance_m"] == .002)
    run.check("integrated_technical_qa_pass", generated["technical_qa"]["passed"] is True,
              gates=generated["technical_qa"]["gates"], errors=generated["technical_qa"]["errors"])
    checkpoint_path = Path(generated["checkpoint"])
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    run.check("automatic_checkpoint_persisted", checkpoint_path.is_file() and bool(checkpoint),
              checkpoint=str(checkpoint_path))
    after_pose = snapshot(run, info, spec)
    invalid = await run.call("animation_generate_pose", {
        "generation": planned["generation"], "options": {"unsupported_config_knob": True}, "apply": True,
    })
    run.check("unknown_option_rejected_before_candidate_creation", "UNSUPPORTED_GENERATION_OPTIONS" in json.dumps(invalid)
              and snapshot(run, info, spec)["action_names"] == after_pose["action_names"], response=invalid)
    filtered = await run.call("animation_analyze_spacing", {
        "generation": planned["generation"], "options": {"frame_start": 2, "frame_end": 4},
    })
    run.check("unsupported_observation_window_rejected", filtered["status"] == "error"
              and "UNSUPPORTED_OBSERVATION_OPTIONS" in json.dumps(filtered), response=filtered)
    final = snapshot(run, info, spec)
    run.check("source_action_fingerprint_unchanged", final["source_action_fingerprint"] == before["source_action_fingerprint"])
    run.check("source_action_remains_active", final["active_action"] == before["active_action"] == spec["action"])
    run.check("only_one_new_candidate_action", len(set(final["action_names"]) - set(before["action_names"])) == 1)
    run.report.update(source_before=before, source_after=final, generation=planned["generation"],
                      source_action=spec["action"], candidate_action=generated["candidate_action"],
                      configured_thresholds=options, saved_proposal=str(operation_path),
                      saved_proposal_thresholds={key: operation["options"].get(key) for key in
                          [*ROOT_LOOP_THRESHOLDS, "contact_tolerance_m", "sliding_tolerance_m"]},
                      candidate_metadata=metadata,
                      qa_thresholds=thresholds, checkpoint=str(checkpoint_path),
                      brief=saved_plan["brief"], scope="cold_actual_MCP_configuration_only_no_render_or_artistic_acceptance")


async def execute(folder, info):
    run = Run(folder)
    run.bridge = Bridge("localhost", PORT)
    with (folder / "server-stderr.txt").open("w", encoding="utf-8") as log:
        async with session(folder, PORT, log) as client:
            run.client = client
            try:
                await asyncio.wait_for(scenario(run, info), timeout=180)
            except Exception as exc:
                run.report["exception"] = repr(exc)
                print("EXCEPTION " + repr(exc), flush=True)
            finally:
                run.bridge.close()
    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", required=True)
    args = parser.parse_args()
    if listening(PORT):
        raise SystemExit("Owned test port 9878 busy; no existing instance is contacted")
    folder = Path(tempfile.gettempdir()) / ("bpm6cfg-" + uuid4().hex[:8])
    (folder / "fixture").mkdir(parents=True, exist_ok=False)
    expression = ("import sys\nsys.path.insert(0," + repr(str(ROOT / "tests" / "blender"))
                  + ")\nfrom fixtures_m6 import build\nbuild(" + repr(str(folder / "fixture"))
                  + ",fps=30,duration_seconds=1,suite=False,primary='standard_humanoid')")
    built = subprocess.run([args.blender, "--background", "--factory-startup", "--disable-autoexec",
                            "--python-exit-code", "1", "--python-expr", expression], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
    (folder / "build.txt").write_text(built.stdout + built.stderr, encoding="utf-8")
    if built.returncode:
        raise RuntimeError("Fixture build failed: " + str(folder / "build.txt"))
    info = json.loads((folder / "fixture" / "fixtures.json").read_text(encoding="utf-8"))
    fixture = Path(info["blend"])
    digest = sha256(fixture)
    if listening(PORT):
        raise SystemExit("Owned test port became busy; no existing instance is contacted")
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    env = dict(os.environ, BLENDER_HOST="localhost", BLENDER_PORT=str(PORT),
               BLENDER_MCP_SAFE_MODE="1", DISABLE_TELEMETRY="true")
    run = None
    with (folder / "blender-stdout.txt").open("w", encoding="utf-8") as log:
        child = subprocess.Popen([args.blender, "--factory-startup", "--disable-autoexec", str(fixture),
                                  "--python", str(ROOT / "tests" / "integration" / "m4_own_blender_start.py"),
                                  "--", str(PORT)], stdout=log, stderr=subprocess.STDOUT,
                                 env=env, startupinfo=startup)
        try:
            deadline = time.monotonic() + 90
            while not listening(PORT):
                if child.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Owned fixture addon did not start: " + str(folder))
                time.sleep(.5)
            run = asyncio.run(execute(folder, info))
        finally:
            # This Popen is the only Blender PID this runner ever terminates.
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
    run.report.update(fixture_file_unchanged=sha256(fixture) == digest,
                      fixture_sha256=digest, blender_port=PORT, production_assets_used=False,
                      blender_pid_started_and_closed_here=child.pid, blender_pid_closed=child.poll() is not None)
    run.check("fixture_file_sha256_unchanged", run.report["fixture_file_unchanged"])
    run.report["status"] = "pass" if "exception" not in run.report and all(
        row["passed"] for row in run.report["checks"].values()) else "fail"
    run.save()
    print("M6_CONFIG_REPORT=" + str(folder / "report.json") + " status=" + run.report["status"], flush=True)
    raise SystemExit(0 if run.report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
