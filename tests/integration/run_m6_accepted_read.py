"""Cold actual MCP qualification of safe reads after real visual acceptance.

Owns Blender 9878 and local review HTTP 8769. Blocking and spline reviews are
supplied by the coordinating agent after inspecting the actual temporal views.
This runner never manufactures an ACCEPTED generation or review feedback.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
from uuid import uuid4

from run_m5_mcp import Run, listening, session
from run_m6_config import ROOT, control
from blender_mcp.production.animation.generation import store
from blender_mcp.production.animation.generation.service import fresh
from blender_mcp.production.animation.generation.render import temporal
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.transport import BlenderExecutionError
from blender_mcp.production.common.m3_transaction import BASE_RUNTIME
from blender_mcp.production.common.storage import Storage, sha256

PORT, HTTP_PORT = 9878, 8769
READS = (
    "animation_validate_pose", "animation_validate_blocking", "animation_analyze_pose_silhouette",
    "animation_analyze_timing", "animation_analyze_spacing", "animation_analyze_arcs",
    "animation_analyze_body_mechanics",
)


def snapshot(run, info, spec, candidate=None):
    return control(run, info, BASE_RUNTIME + r'''
obj=bpy.data.objects[params['object']]
source=bpy.data.actions[params['source']]
candidate=bpy.data.actions.get(params['candidate']) if params['candidate'] else None
result={'source_fingerprint':m2_digest(m3_source_snapshot(source)),
        'candidate_fingerprint':m2_digest(m2_snapshot(candidate)) if candidate else None,
        'pose_context_fingerprint':m2_digest(m3_capture(obj)),
        'transient_context_fingerprint':m2_digest(prod_transient_capture()),
        'active_action':obj.animation_data.action.name if obj.animation_data.action else None,
        'frame':bpy.context.scene.frame_current,'subframe':bpy.context.scene.frame_subframe}
''', {"object": spec["object"], "source": spec["action"], "candidate": candidate})


async def review(run, generation, rendered, label):
    token = uuid4().hex[:8]
    feedback = run.folder / ("review-feedback-" + token + ".json")
    relative = Path(rendered["output_dir"]).relative_to(run.folder)
    request = dict(label=label, generation=generation, render=rendered, feedback=str(feedback),
                   url="http://127.0.0.1:" + str(HTTP_PORT) + "/" + quote(relative.as_posix()) + "/preview.html")
    path = run.folder / ("visual-request-" + token + ".json")
    path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    run.report["pending_visual_review"] = str(path)
    run.save()
    print("VISUAL_REVIEW_REQUEST=" + str(path), flush=True)
    deadline = time.monotonic() + 1200
    while not feedback.is_file():
        if time.monotonic() > deadline:
            raise TimeoutError("Actual temporal review missing; generation remains unaccepted")
        await asyncio.sleep(.5)
    supplied = json.loads(feedback.read_text(encoding="utf-8-sig"))
    run.report.setdefault("visual_reviews", []).append(dict(request=str(path), review=supplied))
    run.report.pop("pending_visual_review", None)
    run.check(label + "_actual_review_supplied", supplied.get("temporal_reviewed") is True
              and supplied.get("artifact_reference") == rendered["output_dir"]
              and supplied.get("decision") == "ACCEPT", review=supplied)
    if supplied.get("decision") != "ACCEPT":
        raise AssertionError("Visual review did not permit progression: " + label)
    return supplied


async def reject(run, tool, arguments, marker):
    response = await run.call(tool, arguments)
    run.check("reject_" + marker + "_" + str(len(run.report["calls"])), response["status"] == "error"
              and marker in json.dumps(response), response=response)
    if response["status"] != "error" or marker not in json.dumps(response):
        raise AssertionError("Expected safe rejection: " + marker)


def counts(run, info):
    actions = control(run, info, "result={'actions':sorted(action.name for action in bpy.data.actions)}")["actions"]
    checkpoints = sorted(str(path) for path in (run.folder / "s" / "checkpoints").glob("*/manifest.json"))
    return {"actions": actions, "checkpoints": checkpoints}


async def reject_window(run, info, generation, tool, options, label):
    before = counts(run, info)
    response = await run.call(tool, {"generation": generation, "apply": True, "options": options})
    refused = response["status"] == "error" or response.get("data", {}).get("status") == "unsupported"
    run.check(label + "_before_checkpoint_or_candidate", refused
              and "GENERATION_WINDOW_INVALID" in json.dumps(response) and counts(run, info) == before,
              response=response)
    if not refused or "GENERATION_WINDOW_INVALID" not in json.dumps(response):
        raise AssertionError("Expected immutable brief-window rejection: " + label)


def assign(run, info, spec, action):
    return control(run, info, BASE_RUNTIME + r'''
obj=bpy.data.objects[params['object']]
action=bpy.data.actions.get(params['action']) if params['action'] else None
if params['action'] and action is None: raise ValueError('TEST_ACTION_MISSING')
m3_assign(obj,action)
m3_set_frame(13)
result={'active_action':action.name if action else None}
''', {"object": spec["object"], "action": action})


def edit_first_key(run, info, action, value=None):
    return control(run, info, BASE_RUNTIME + r'''
action=bpy.data.actions[params['action']]
curve=list(action_curves(action,None))[0][3]
key=curve.keyframe_points[0]
old=float(key.co.y)
key.co.y=old+.005 if params['value'] is None else params['value']
bpy.context.scene.frame_set(bpy.context.scene.frame_current,subframe=bpy.context.scene.frame_subframe)
result={'original':old,'path':curve.data_path,'index':curve.array_index,'frame':float(key.co.x)}
''', {"action": action, "value": value})


async def scenario(run, info):
    spec = info["scenarios"]["standard_humanoid"]
    initial = snapshot(run, info, spec)
    await run.good("asset_register_fixture", {"path": info["blend"]})
    intent = {"type": "IDLE", "profile": "HUMAN", "fps": 30, "duration_s": 1,
              "root_mode": "IN_PLACE", "forward_axis": "-Y", "strength": .6}
    options = {"mapping": spec["mapping"], "ground_height_m": spec["ground_height_m"],
               "meshes": [spec["body"]], "contact_tolerance_m": .001, "sliding_tolerance_m": .002}
    planned = await run.good("animation_generation_plan", {
        "object_name": spec["object"], "intent": intent, "options": options,
    })
    generation = planned["generation"]
    run.check("cold_plan_has_real_immutable_source", planned["supported"]
              and planned["source_action"] == spec["action"]
              and planned["source_fingerprint"] == initial["source_fingerprint"])
    extended = deepcopy(planned["pose_plan"])
    extended["poses"][-1]["frame"] = 45
    await reject_window(run, info, generation, "animation_generate_blocking", {"pose_plan": extended}, "extended_pose_plan")
    outside = deepcopy(planned["pose_plan"]["poses"][0])
    outside["frame"] = 45
    await reject_window(run, info, generation, "animation_generate_pose", {"pose": outside}, "outside_pose_override")
    held = deepcopy(planned["pose_plan"])
    held["holds"] = [{"phase": "NEUTRAL", "frame_start": 1, "frame_end": 45, "reason": "Deliberate invalid test hold"}]
    await reject_window(run, info, generation, "animation_generate_blocking", {"pose_plan": held}, "outside_hold_end")
    blocking = await run.good("animation_generate_blocking", {"generation": generation, "apply": True})
    run.check("blocking_integrated_qa", blocking["technical_qa"]["passed"])
    if not blocking["technical_qa"]["passed"]:
        raise AssertionError("Blocking technical QA failed")
    render_options = {"width": 192, "height": 256, "framing_meshes": [spec["body"]]}
    rendered = await run.good("animation_generation_render", {"generation": generation, "options": render_options})
    supplied = await review(run, generation, rendered["render"], "cold accepted-read blocking")
    reviewed = await run.good("animation_accept_generated", {"generation": generation, "options": {"review": supplied}})
    run.check("blocking_reviewed_without_final_acceptance", reviewed["state"] == "REVIEWED")
    await run.good("animation_generate_breakdowns", {"generation": generation, "apply": True})
    splined = await run.good("animation_spline", {"generation": generation, "apply": True})
    run.check("spline_integrated_qa", splined["technical_qa"]["passed"])
    if not splined["technical_qa"]["passed"]:
        raise AssertionError("Spline technical QA failed")
    rendered = await run.good("animation_generation_render", {"generation": generation, "options": render_options})
    supplied = await review(run, generation, rendered["render"], "cold accepted-read spline")
    await run.good("animation_accept_generated", {"generation": generation, "options": {"review": supplied}})
    accepted = await run.good("animation_accept_generated", {"generation": generation, "apply": True})
    candidate = accepted["candidate_action"]
    run.check("real_accepted_candidate_is_active", accepted["state"] == "ACCEPTED"
              and accepted["active_action"] == candidate and accepted["source_intact"])
    await accepted_checks(run, info, spec, generation, candidate, initial, accepted["checkpoint"], intent, options)


async def accepted_checks(run, info, spec, generation, candidate, initial, accepted_checkpoint, intent, options):
    render_options = {"width": 192, "height": 256, "framing_meshes": [spec["body"]]}
    assign(run, info, spec, candidate)
    baseline = snapshot(run, info, spec, candidate)
    run.report["accepted_baseline"] = baseline
    run.report["accepted_generation"] = generation
    run.report["candidate_action"] = candidate
    run.report["accepted_reads"] = {}
    for name in READS:
        measured = await run.good(name, {"generation": generation})
        after = snapshot(run, info, spec, candidate)
        run.check(name + "_after_accept_restores_context", after == baseline)
        run.report["accepted_reads"][name] = {"status": measured.get("status"),
                                            "passed": measured.get("passed"), "state_restored": after == baseline}
    await run.good("create_checkpoint", {"description": "Before controlled post-accept Action freshness negatives",
                                          "operation": "m6-accepted-read-negatives"})
    clone = control(run, info, BASE_RUNTIME + r'''
action=bpy.data.actions[params['candidate']].copy()
action.name='M6_AcceptedRead_OutsideWindow';action.use_fake_user=True
curve=list(action_curves(action,None))[0][3]
curve.keyframe_points.insert(45,float(curve.evaluate(45)))
result={'action':action.name}
''', {"candidate": candidate})["action"]
    before_render = counts(run, info)
    _, saved_plan, _ = store.load(Storage(run.folder / "s"), generation)

    class FixtureOnlyBridge:
        def run(self, body, parameters=None):
            return control(run, info, body, parameters)

    try:
        temporal(FixtureOnlyBridge(), Storage(run.folder / "s"), "m6-outside-render-" + uuid4().hex[:8],
                 saved_plan, clone, render_options)
    except (ValueError, BlenderExecutionError) as exc:
        run.check("renderer_outside_action_rejected_before_checkpoint", "TEMPORAL_ACTION_WINDOW_MISMATCH" in str(exc)
                  and counts(run, info) == before_render, error=str(exc), owned_clone=clone)
        if "TEMPORAL_ACTION_WINDOW_MISMATCH" not in str(exc):
            raise
    else:
        raise AssertionError("Direct owned renderer accepted an Action beyond immutable brief")
    run.check("renderer_rejection_preserves_accepted_context", snapshot(run, info, spec, candidate) == baseline)
    changed = edit_first_key(run, info, spec["action"])
    try:
        await reject(run, "animation_analyze_timing", {"generation": generation}, "STALE_PROPOSAL")
    finally:
        edit_first_key(run, info, spec["action"], changed["original"])
    run.check("original_source_recovered_exactly", snapshot(run, info, spec, candidate) == baseline)
    changed = edit_first_key(run, info, candidate)
    try:
        await reject(run, "animation_analyze_timing", {"generation": generation}, "STALE_PROPOSAL")
    finally:
        edit_first_key(run, info, candidate, changed["original"])
    run.check("accepted_candidate_recovered_exactly", snapshot(run, info, spec, candidate) == baseline)
    unrelated = control(run, info, r'''
source=bpy.data.actions[params['source']]
action=source.copy();action.name='M6_AcceptedRead_Unrelated';action.use_fake_user=True
result={'action':action.name}
''', {"source": spec["action"]})["action"]
    assign(run, info, spec, unrelated)
    try:
        await reject(run, "animation_analyze_timing", {"generation": generation}, "STALE_PROPOSAL")
    finally:
        assign(run, info, spec, candidate)
    run.check("unrelated_active_action_restore", snapshot(run, info, spec, candidate) == baseline)
    await reject(run, "animation_generate_pose", {"generation": generation, "apply": True}, "GENERATION_TERMINAL")
    await run.good("create_checkpoint", {"description": "Before controlled no-source-Action generation probe",
                                          "operation": "m6-empty-source-action"})
    assign(run, info, spec, None)
    try:
        empty = await run.good("animation_generation_plan", {
            "object_name": spec["object"], "intent": intent, "options": options,
        })
        expected = control(run, info, BASE_RUNTIME + "result={'fingerprint':m2_digest(m3_source_snapshot(None))}")["fingerprint"]
        run.check("empty_source_uses_actual_canonical_hash", empty["source_action"] is None
                  and empty["source_fingerprint"] == expected and empty["supported"])
        pose = await run.good("animation_generate_pose", {"generation": empty["generation"], "apply": True})
        run.check("empty_source_pose_candidate_preserves_empty_source", pose["status"] == "candidate_created"
                  and pose["source_intact"] is True and snapshot(run, info, spec)["active_action"] is None)
        run.report["empty_source_case"] = {"generation": empty["generation"], "source_fingerprint": expected,
                                           "candidate": pose["candidate_action"], "scope": "actual POSE only, not artistic acceptance"}
    finally:
        assign(run, info, spec, candidate)
    final_read = await run.good("animation_validate_pose", {"generation": generation})
    final = snapshot(run, info, spec, candidate)
    run.check("all_reads_and_negatives_preserve_source_candidate_and_pose", final == baseline
              and final["source_fingerprint"] == initial["source_fingerprint"])
    _, _, state = store.load(Storage(run.folder / "s"), generation)
    run.check("accepted_lifecycle_is_still_terminal", state["state"] == "ACCEPTED")
    run.report.update(final=final, final_validation_passed=final_read.get("passed"),
                      checkpoint=accepted_checkpoint, scope="actual_MCP_accepted_reads_with_real_temporal_reviews")


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


def stop_owned(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


class ContinuationRun(Run):
    def __init__(self, folder):
        super().__init__(folder)
        self.report_path = folder / ("continuation-report-" + uuid4().hex[:8] + ".json")

    def save(self):
        self.report_path.write_text(
            json.dumps(self.report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


async def continuation_execute(folder, info, origin, checkpoint, preserved):
    run = ContinuationRun(folder)
    run.report.update(origin_report=str(folder / "report.json"), origin_status=origin["status"],
                      scope="checkpoint_restored_continuation_no_new_acceptance_or_render",
                      checkpoint_restoration=checkpoint, original_evidence_hashes=preserved)
    run.bridge = Bridge("localhost", PORT)
    with (folder / "continuation-server-stderr.txt").open("w", encoding="utf-8") as log:
        async with session(folder, PORT, log) as client:
            run.client = client
            try:
                generation = origin["accepted_generation"]
                candidate = origin["candidate_action"]
                spec = info["scenarios"]["standard_humanoid"]
                _, plan, state = store.load(Storage(folder / "s"), generation)
                actual = snapshot(run, info, spec, candidate)
                run.check("actual_checkpoint_source_candidate_pose_context_match_accepted_baseline",
                          actual == origin["accepted_baseline"], actual=actual)
                if actual != origin["accepted_baseline"]:
                    raise AssertionError("Restored checkpoint differs from actually accepted baseline")
                compatible = fresh(run.bridge, plan, state)
                run.check("fresh_guard_verifies_restored_source_candidate_rig_file_and_stamp",
                          state["state"] == "ACCEPTED" and compatible["supported"]
                          and compatible["rig_fingerprint"] == plan["rig_fingerprint"]
                          and compatible["source_fingerprint"] == plan["source_fingerprint"]
                          and compatible["filepath"] == info["blend"] and compatible["stamps"] == plan["stamps"])
                if state["state"] != "ACCEPTED" or not compatible["supported"]:
                    raise AssertionError("Actual preserved accepted state failed freshness")
                options = {key: value for key, value in plan["options"].items() if key != "brief"}
                await accepted_checks(run, info, spec, generation, candidate, actual,
                                      checkpoint["manifest"], plan["brief"]["user_requirements"], options)
            except Exception as exc:
                run.report["exception"] = repr(exc)
                print("EXCEPTION " + repr(exc), flush=True)
            finally:
                run.bridge.close()
    return run


def continue_owned(args):
    folder = Path(args.continue_from).resolve(strict=True)
    if not folder.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not folder.name.startswith("bpm6read-"):
        raise ValueError("Only this runner's owned temporary fixtures may be continued")
    origin_path = folder / "report.json"
    origin = json.loads(origin_path.read_text(encoding="utf-8"))
    if origin.get("production_assets_used") is not False or not origin.get("accepted_baseline"):
        raise ValueError("Actual owned ACCEPTED baseline and original report required")
    info = json.loads((folder / "fixture" / "fixtures.json").read_text(encoding="utf-8"))
    fixture = Path(info["blend"]).resolve(strict=True)
    if not fixture.is_relative_to(folder) or sha256(fixture) != origin["fixture_sha256"]:
        raise ValueError("Owned immutable source fixture changed")
    spec = info["scenarios"]["standard_humanoid"]
    manifests = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in
                 (folder / "s" / "checkpoints").glob("*/manifest.json")]
    matches = [(path, value) for path, value in manifests if value["operation"] == "m6-accepted-read-negatives"]
    checkpoint_path, checkpoint = max(matches, key=lambda row: row[1]["created_at"])
    checkpoint_blend = checkpoint_path.parent / checkpoint["filename"]
    if sha256(checkpoint_blend) != checkpoint["sha256"] or checkpoint["source"]["source_file"] != info["blend"]:
        raise ValueError("Actual checkpoint identity or content changed")
    generation_path = Path(origin["accepted_generation"])
    ledger_paths = [generation_path, *generation_path.parent.glob("event-*.json"), origin_path,
                    *folder.glob("continuation-report*.json")]
    preserved = {str(path): sha256(path) for path in ledger_paths}
    setup = folder / ("continuation-setup-" + uuid4().hex[:8])
    setup.mkdir(exist_ok=False)
    # This setup modifies only Blender memory. The binary source stays intact;
    # this independent copy records the state before importing retained Actions.
    source_copy = setup / "before-import.blend"
    source_copy.write_bytes(fixture.read_bytes())
    (setup / "intent.json").write_text(json.dumps({
        "source": str(fixture), "source_sha256": sha256(fixture), "checkpoint": str(checkpoint_path),
        "description": "Restore owned accepted fixture memory from a verified actual checkpoint; source file and signed generation ledger are immutable",
        "operation": "m6-accepted-read-continuation", "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2), encoding="utf-8")
    extracted = setup / "checkpoint-state.json"
    extract_script = setup / "extract.py"
    extract_script.write_text("import json\nfrom pathlib import Path\n" + BASE_RUNTIME + "\n" +
        "obj=bpy.data.objects[" + repr(spec["object"]) + "]\n" +
        "data={'pose':m3_capture(obj),'transient':prod_transient_capture()," +
        "'stamps':[scene.get('production_asset') for scene in bpy.data.scenes]," +
        "'actions':[a.name for a in bpy.data.actions if a.name!=" + repr(spec["action"]) + "]," +
        "'action_fake_users':{a.name:a.use_fake_user for a in bpy.data.actions}," +
        "'source_fingerprint':m2_digest(m3_source_snapshot(bpy.data.actions[" + repr(spec["action"]) + "]))," +
        "'candidate_fingerprint':m2_digest(m2_snapshot(bpy.data.actions[" + repr(origin["candidate_action"]) + "]))}\n" +
        "Path(" + repr(str(extracted)) + ").write_text(json.dumps(data,allow_nan=False),encoding='utf-8')\n",
        encoding="utf-8")
    result = subprocess.run([args.blender, "--background", "--factory-startup", "--disable-autoexec",
                             str(checkpoint_blend), "--python-exit-code", "1", "--python", str(extract_script)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    (setup / "extract-log.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("Own checkpoint extraction failed")
    restored = json.loads(extracted.read_text(encoding="utf-8"))
    _, plan, state = store.load(Storage(folder / "s"), origin["accepted_generation"])
    if (state["state"] != "ACCEPTED" or restored["stamps"] != plan["stamps"]
            or restored["source_fingerprint"] != origin["accepted_baseline"]["source_fingerprint"]
            or restored["candidate_fingerprint"] != origin["accepted_baseline"]["candidate_fingerprint"]):
        raise ValueError("Checkpoint is not the actually accepted Source and Candidate")
    startup_script = setup / "restore-memory.py"
    startup_script.write_text("import json\nfrom pathlib import Path\n" + BASE_RUNTIME + "\n" +
        "assert bpy.data.filepath==" + repr(info["blend"]) + "\n" +
        "data=json.loads(Path(" + repr(str(extracted)) + ").read_text(encoding='utf-8'))\n" +
        "assert m2_digest(m3_source_snapshot(bpy.data.actions[" + repr(spec["action"]) + "]))==data['source_fingerprint']\n" +
        "assert not any(bpy.data.actions.get(name) for name in data['actions'])\n" +
        "with bpy.data.libraries.load(" + repr(str(checkpoint_blend)) + ",link=False) as (available,loaded):\n" +
        "    assert all(name in available.actions for name in data['actions'])\n    loaded.actions=list(data['actions'])\n" +
        # Blender's real append resets fake_user. Restore its measured checkpoint
        # value so content, not just keys, exactly matches the accepted Action.
        "for name in data['actions']: bpy.data.actions[name].use_fake_user=data['action_fake_users'][name]\n" +
        "assert len(bpy.data.scenes)==1 and len(data['stamps'])==1\n" +
        "bpy.context.scene['production_asset']=data['stamps'][0]\n" +
        "obj=bpy.data.objects[" + repr(spec["object"]) + "]\n" +
        "m3_assign(obj,bpy.data.actions[" + repr(origin["candidate_action"]) + "])\n" +
        "m3_restore(obj,data['pose'])\nprod_transient_restore(data['transient'])\n" +
        "for other in bpy.context.scene.objects: other.select_set(other.name in data['transient']['selected'])\n" +
        "bpy.context.view_layer.objects.active=bpy.data.objects.get(data['transient']['active'])\n" +
        "print('RESTORED_ACTUAL_CHECKPOINT_MEMORY_SOURCE_UNWRITTEN',flush=True)\n", encoding="utf-8")
    if listening(PORT):
        raise SystemExit("Own9878 busy; no existing instance is contacted")
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    env = dict(os.environ, BLENDER_HOST="localhost", BLENDER_PORT=str(PORT),
               BLENDER_MCP_SAFE_MODE="1", DISABLE_TELEMETRY="true")
    with (setup / "blender-log.txt").open("w", encoding="utf-8") as log:
        child = subprocess.Popen([args.blender, "--factory-startup", "--disable-autoexec", str(fixture),
                                  "--python-exit-code", "1", "--python", str(startup_script), "--python",
                                  str(ROOT / "tests/integration/m4_own_blender_start.py"), "--", str(PORT)],
                                 stdout=log, stderr=subprocess.STDOUT, startupinfo=startup, env=env)
        try:
            deadline = time.monotonic() + 90
            while not listening(PORT):
                if child.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Owned continuation addon did not start")
                time.sleep(.5)
            run = asyncio.run(continuation_execute(folder, info, origin,
                {"manifest": str(checkpoint_path), "blend": str(checkpoint_blend), "sha256": checkpoint["sha256"],
                 "setup": str(setup), "method": "retained Actions imported into original owned fixture memory; exact original ledger"}, preserved))
        finally:
            stop_owned(child)
    run.check("source_fixture_file_sha256_unchanged", sha256(fixture) == origin["fixture_sha256"])
    run.check("original_fail_report_and_accepted_signed_ledger_unchanged",
              all(sha256(Path(path)) == digest for path, digest in preserved.items()))
    run.report.update(fixture_sha256=origin["fixture_sha256"], blender_port=PORT,
                      owned_pids_closed={str(child.pid): child.poll() is not None}, no_new_render_or_acceptance=True)
    run.report["status"] = "pass" if "exception" not in run.report and all(
        row["passed"] for row in run.report["checks"].values()) else "fail"
    run.save()
    print("M6_ACCEPTED_CONTINUATION_REPORT=" + str(run.report_path) + " status=" + run.report["status"], flush=True)
    raise SystemExit(0 if run.report["status"] == "pass" else 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", required=True)
    parser.add_argument("--continue-from")
    args = parser.parse_args()
    if args.continue_from:
        continue_owned(args)
        return
    if listening(PORT) or listening(HTTP_PORT):
        raise SystemExit("Own ports 9878/8769 busy; no existing instance is contacted")
    folder = Path(tempfile.gettempdir()) / ("bpm6read-" + uuid4().hex[:8])
    (folder / "fixture").mkdir(parents=True, exist_ok=False)
    expression = ("import sys\nsys.path.insert(0," + repr(str(ROOT / "tests" / "blender"))
                  + ")\nfrom fixtures_m6 import build\nbuild(" + repr(str(folder / "fixture"))
                  + ",fps=30,duration_seconds=1,suite=False,primary='standard_humanoid')")
    built = subprocess.run([args.blender, "--background", "--factory-startup", "--disable-autoexec",
                            "--python-exit-code", "1", "--python-expr", expression], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
    (folder / "build.txt").write_text(built.stdout + built.stderr, encoding="utf-8")
    if built.returncode:
        raise RuntimeError("Owned fixture build failed: " + str(folder))
    info = json.loads((folder / "fixture" / "fixtures.json").read_text(encoding="utf-8"))
    fixture, digest = Path(info["blend"]), sha256(Path(info["blend"]))
    if listening(PORT) or listening(HTTP_PORT):
        raise SystemExit("Own ports became busy; no existing instance is contacted")
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
    env = dict(os.environ, BLENDER_HOST="localhost", BLENDER_PORT=str(PORT),
               BLENDER_MCP_SAFE_MODE="1", DISABLE_TELEMETRY="true")
    with (folder / "processes.txt").open("w", encoding="utf-8") as log:
        web = subprocess.Popen([sys.executable, "-m", "http.server", str(HTTP_PORT), "--bind", "127.0.0.1",
                                "--directory", str(folder)], stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        child = subprocess.Popen([args.blender, "--factory-startup", "--disable-autoexec", str(fixture), "--python",
                                  str(ROOT / "tests" / "integration" / "m4_own_blender_start.py"), "--", str(PORT)],
                                 stdout=log, stderr=subprocess.STDOUT, startupinfo=startup, env=env)
        try:
            deadline = time.monotonic() + 90
            while not listening(PORT):
                if child.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Owned addon did not start: " + str(folder))
                time.sleep(.5)
            run = asyncio.run(execute(folder, info))
        finally:
            stop_owned(child)
            stop_owned(web)
    run.report.update(fixture_file_unchanged=sha256(fixture) == digest, fixture_sha256=digest,
                      production_assets_used=False, blender_port=PORT, http_port=HTTP_PORT,
                      owned_pids_closed={str(child.pid): child.poll() is not None, str(web.pid): web.poll() is not None})
    run.check("source_fixture_file_sha256_unchanged", run.report["fixture_file_unchanged"])
    run.report["status"] = "pass" if "exception" not in run.report and all(
        row["passed"] for row in run.report["checks"].values()) else "fail"
    run.save()
    print("M6_ACCEPTED_READ_REPORT=" + str(folder / "report.json") + " status=" + run.report["status"], flush=True)
    raise SystemExit(0 if run.report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
