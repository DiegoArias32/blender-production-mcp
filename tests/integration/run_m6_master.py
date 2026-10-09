"""Real MASTER refusal on a new, independently registered procedural fixture.

This supplement never relabels an existing FIXTURE, renders, reviews or opens
a user project. Each mutation must fail before a checkpoint or Candidate.
"""
from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4

from run_m5_mcp import Run, session, listening
from run_m6_config import ROOT, control
from run_m6_accepted_read import stop_owned
from blender_mcp.production.animation.generation import store
from blender_mcp.production.common.asset_safety import AssetRegistry
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.m3_transaction import BASE_RUNTIME
from blender_mcp.production.common.storage import Storage, sha256

PORT = 9878


def snapshot(run, info, spec):
    return control(run, info, BASE_RUNTIME + r'''
obj=bpy.data.objects[params['object']]
source=bpy.data.actions[params['source']]
result={'source_fingerprint':m2_digest(m3_source_snapshot(source)),
        'pose_context_fingerprint':m2_digest(m3_capture(obj)),
        'transient_context_fingerprint':m2_digest(prod_transient_capture()),
        'actions':sorted(action.name for action in bpy.data.actions),
        'active_action':obj.animation_data.action.name if obj.animation_data.action else None,
        'filepath':bpy.data.filepath,'frame':bpy.context.scene.frame_current}
''', {"object": spec["object"], "source": spec["action"]})


async def scenario(run, info):
    spec = info["scenarios"]["standard_humanoid"]
    inventory = await run.client.list_tools()
    run.check("cold_inventory_155", len(inventory.tools) == 155)
    before = snapshot(run, info, spec)
    run.report["before"] = before
    current = await run.good("asset_status")
    run.check("new_file_unregistered_before_master", current["asset"]["role"] == "UNREGISTERED")
    registered = await run.good("asset_register_master", {"confirm_master": True})
    run.check("first_registration_is_real_master", registered["asset"]["role"] == "MASTER")
    current = await run.good("asset_status")
    run.check("real_master_is_immutable", current["asset"]["role"] == "MASTER" and current["asset"]["mutable"] is False)
    assets = AssetRegistry(Storage(run.folder / "s")).load()["assets"]
    run.check("registry_contains_only_one_master_no_fixture_relabel", len(assets) == 1
              and next(iter(assets.values()))["role"] == "MASTER")
    intent = {"type": "IDLE", "profile": "HUMAN", "duration_s": 1, "fps": 30,
              "root_mode": "IN_PLACE", "forward_axis": "-Y", "strength": .6}
    options = {"mapping": spec["mapping"], "ground_height_m": spec["ground_height_m"], "meshes": [spec["body"]]}
    compatible = await run.good("animation_generation_compatibility", {"object_name": spec["object"], "options": options})
    run.check("master_read_only_compatibility_supported", compatible["supported"])
    planned = await run.good("animation_generation_plan", {"object_name": spec["object"], "intent": intent, "options": options})
    run.check("fresh_master_plan_supported_and_nonmutating", planned["supported"] and planned["no_action_created"]
              and planned["source_fingerprint"] == before["source_fingerprint"] and snapshot(run, info, spec) == before)
    generation = planned["generation"]
    checkpoints = sorted(str(path) for path in (run.folder / "s" / "checkpoints").glob("*/manifest.json"))
    for name in ("animation_generate_pose", "animation_generate_blocking", "animation_generate"):
        response = await run.call(name, {"generation": generation, "apply": True})
        run.check(name + "_master_refused_before_checkpoint_or_candidate",
                  response["status"] == "error" and "MASTER_ASSET_READ_ONLY" in json.dumps(response)
                  and response.get("checkpoint") is None and snapshot(run, info, spec) == before
                  and sorted(str(path) for path in (run.folder / "s" / "checkpoints").glob("*/manifest.json")) == checkpoints,
                  response=response)
        if response["status"] != "error" or "MASTER_ASSET_READ_ONLY" not in json.dumps(response):
            raise AssertionError("Expected actual MASTER mutation refusal: " + name)
    _, _, state = store.load(Storage(run.folder / "s"), generation)
    run.check("refused_generation_stays_planned_without_candidate", state["state"] == "PLANNED"
              and not state.get("candidate_action"))
    run.report.update(after=snapshot(run, info, spec), generation=generation, master=current["asset"],
                      mcp_version=version("blender-production-mcp"), no_render_or_review=True)


async def execute(folder, info):
    run = Run(folder)
    run.bridge = Bridge("localhost", PORT)
    with (folder / "server-stderr.txt").open("w", encoding="utf-8") as log:
        async with session(folder, PORT, log) as client:
            run.client = client
            try:
                await scenario(run, info)
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
        raise SystemExit("Own9878 busy; no existing instance is contacted")
    folder = Path(tempfile.gettempdir()) / ("bpm6master-" + uuid4().hex[:8])
    (folder / "fixture").mkdir(parents=True, exist_ok=False)
    expression = ("import sys\nsys.path.insert(0," + repr(str(ROOT / "tests/blender")) + ")\n"
                  "from fixtures_m6 import build\nbuild(" + repr(str(folder / "fixture"))
                  + ",fps=30,duration_seconds=1,suite=False,primary='standard_humanoid')")
    built = subprocess.run([args.blender, "--background", "--factory-startup", "--disable-autoexec",
                            "--python-exit-code", "1", "--python-expr", expression], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
    (folder / "build.txt").write_text(built.stdout + built.stderr, encoding="utf-8")
    if built.returncode:
        raise RuntimeError("Owned procedural fixture build failed")
    info = json.loads((folder / "fixture/fixtures.json").read_text(encoding="utf-8"))
    fixture = Path(info["blend"])
    digest = sha256(fixture)
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    env = dict(os.environ, BLENDER_HOST="localhost", BLENDER_PORT=str(PORT),
               BLENDER_MCP_SAFE_MODE="1", DISABLE_TELEMETRY="true")
    with (folder / "blender-log.txt").open("w", encoding="utf-8") as log:
        child = subprocess.Popen([args.blender, "--factory-startup", "--disable-autoexec", str(fixture),
                                  "--python-exit-code", "1", "--python", str(ROOT / "tests/integration/m4_own_blender_start.py"),
                                  "--", str(PORT)], stdout=log, stderr=subprocess.STDOUT, startupinfo=startup, env=env)
        try:
            deadline = time.monotonic() + 90
            while not listening(PORT):
                if child.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Owned addon did not start")
                time.sleep(.5)
            run = asyncio.run(execute(folder, info))
        finally:
            stop_owned(child)
    run.check("source_master_file_sha256_unchanged", sha256(fixture) == digest)
    run.report.update(source_fixture=str(fixture), source_sha256=digest, production_assets_used=False,
                      blender_port=PORT, owned_pids_closed={str(child.pid): child.poll() is not None},
                      scope="supplemental_master_first_registration_on_independent_owned_fixture")
    run.report["status"] = "pass" if "exception" not in run.report and all(
        check["passed"] for check in run.report["checks"].values()) else "fail"
    run.save()
    print("M6_MASTER_REPORT=" + str(folder / "report.json") + " status=" + run.report["status"], flush=True)
    raise SystemExit(0 if run.report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
