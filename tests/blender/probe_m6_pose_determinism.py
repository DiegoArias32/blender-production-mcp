"""Reproduce or refute implicit pose-state dependence in a controlled M6 copy.

This diagnostic never opens a user asset, starts a socket, changes the runtime,
or overwrites its qualified input. A copied fixture Action receives a small
chest-scale animation; retained source evidence is immutable during generation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import types
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/blender"))
from probe_m6_preview_performance import controlled_source, file_sha256, DEFAULT_REPORT


def inside(source_report, source_copy, output, expect_rejection=False):
    import bpy
    from mathutils import Vector

    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("An isolated, fresh headless Blender process is required")
    fixture_report, qualified_source, qualified_sha = controlled_source(source_report)
    output, source_copy = Path(output).resolve(), Path(source_copy).resolve()
    if source_copy.parent != output or source_copy.name != "controlled-input-copy.blend":
        raise ValueError("Only the explicitly reserved diagnostic copy is accepted")
    if file_sha256(source_copy) != qualified_sha:
        raise ValueError("Diagnostic copy does not match the verified controlled source")
    result = bpy.ops.wm.open_mainfile(filepath=str(source_copy), load_ui=False, use_scripts=False)
    if "FINISHED" not in result:
        raise RuntimeError("Could not open the controlled diagnostic copy")
    sys.path.insert(0, str(ROOT / "src"))
    package = types.ModuleType("blender_mcp")
    package.__path__ = [str(ROOT / "src/blender_mcp")]
    sys.modules["blender_mcp"] = package
    from fixtures_m6 import activate_fixture, GROUND_HEIGHT_M
    from blender_mcp.production.common.bridge import build_code, payload_bytes
    from blender_mcp.production.common.digest import runtime_digest
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME, READ, EXECUTE
    from blender_mcp.production.animation.generation.runtime import GENERATION_RUNTIME, COMPAT_READ, runtime_for_stage
    from blender_mcp.production.animation.generation.planner import create_brief, plan_poses
    from blender_mcp.production.animation.generation.candidate import CandidateBridge

    class InsideBridge:
        maximum_payload_bytes = 0
        calls = 0

        def run(self, body, params=None):
            code = build_code(body, params)
            self.maximum_payload_bytes = max(self.maximum_payload_bytes, payload_bytes(body, params))
            self.calls += 1
            namespace = {}
            exec(code.rsplit("print(", 1)[0], namespace)
            return json.loads(json.dumps(namespace["result"], allow_nan=False))

    bridge = InsideBridge()
    started = time.perf_counter()
    report = {"schema": 1, "status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
              "scope": "controlled_generation_pose_state_determinism_diagnostic_only",
              "blender": bpy.app.version_string, "source": str(qualified_source),
              "source_sha256": qualified_sha, "controlled_copy": str(source_copy),
              "checks": [], "generations": [], "observations": [], "errors": [],
              "expect_rejection": expect_rejection, "compatibility_smoke": {}, "profile_smoke": []}

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")

    def check(name, passed, metrics=None):
        report["checks"].append({"name": name, "passed": bool(passed), "metrics": metrics})
        save()
        if not passed:
            raise AssertionError(name + ": " + repr(metrics))
        print("PASS " + name, flush=True)

    def source_snapshot(action_name):
        return bridge.run(BASE_RUNTIME + "\naction=bpy.data.actions.get(params['action_name'])\nresult={'fingerprint':m2_digest(m3_source_snapshot(action))}\n",
                          {"action_name": action_name})["fingerprint"]

    def candidate_metrics(action_name, caller_frame, rig):
        rig.animation_data.action = source
        bpy.context.scene.frame_set(caller_frame)
        bpy.context.view_layer.update()
        prior = {"frame": bpy.context.scene.frame_current, "action": rig.animation_data.action.name,
                 "chest_scale": list(rig.pose.bones[row["mapping"]["chest"]].scale)}
        candidate = CandidateBridge(bridge, rig.name, action_name)
        measured = candidate.run(BASE_RUNTIME + r'''
obj=m2_object(params['object_name'])
m3_set_frame(params['sample_frame'])
evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
unit=bpy.context.scene.unit_settings.scale_length
action=obj.animation_data.action
result={'sample_frame':params['sample_frame'],'candidate_fingerprint':m2_digest(m2_snapshot(action)),
        'chest_scale':list(obj.pose.bones[params['mapping']['chest']].scale),
        'positions_m':{role:list((evaluated.matrix_world@evaluated.pose.bones[name].head)*unit)
                       for role,name in params['mapping'].items()},
        'head_matrix':[list(r) for r in evaluated.pose.bones[params['mapping']['head']].matrix]}
''', {"object_name": rig.name, "sample_frame": 16, "mapping": row["mapping"]})
        after = {"frame": bpy.context.scene.frame_current, "action": rig.animation_data.action.name,
                 "chest_scale": list(rig.pose.bones[row["mapping"]["chest"]].scale)}
        check("restoring candidate observation from caller frame " + str(caller_frame), prior == after,
              {"before": prior, "after": after})
        return measured

    try:
        row = activate_fixture(fixture_report["fixtures"], "standard_humanoid")
        rig = bpy.data.objects[row["object"]]
        check("input is fixture-only", bool(rig.get("fixture_only")))
        scene = bpy.context.scene
        scene.render.fps, scene.render.fps_base = 30, 1.0
        scene.frame_start, scene.frame_end = 1, 31
        original = bpy.data.actions[row["action"]]
        original_fingerprint = source_snapshot(original.name)
        checkpoint = output / "before-diagnostic-source-animation.blend"
        saved = bpy.ops.wm.save_as_mainfile(filepath=str(checkpoint), copy=True)
        check("diagnostic setup checkpoint exists", "FINISHED" in saved and checkpoint.is_file())
        report["checkpoint"] = {"path": str(checkpoint), "sha256": file_sha256(checkpoint),
                                "description": "Before animating only the copied fixture source chest scale",
                                "source": str(source_copy), "created_at": datetime.now(timezone.utc).isoformat()}

        # A new diagnostic Action is edited; the pre-existing fixture source is retained.
        source = original.copy()
        source.name = "M6_Determinism_DiagnosticSource_" + uuid4().hex[:12]
        source.use_fake_user = True
        rig.animation_data.action = source
        chest = rig.pose.bones[row["mapping"]["chest"]]
        chest.scale = (1.0, 1.0, 1.0)
        chest.keyframe_insert(data_path="scale", frame=1)
        chest.scale = (1.04, 1.04, 1.04)
        chest.keyframe_insert(data_path="scale", frame=31)
        curves = [curve for layer in source.layers for strip in layer.strips
                  for bag in strip.channelbags for curve in bag.fcurves]
        for curve in curves:
            if curve.data_path == chest.path_from_id("scale"):
                for key in curve.keyframe_points:
                    key.interpolation = "LINEAR"
                curve.update()
        source_fingerprint = source_snapshot(source.name)
        report["diagnostic_source"] = {"action": source.name, "fingerprint": source_fingerprint,
                                       "edited_channel": chest.path_from_id("scale"),
                                       "keys": [{"frame": 1, "scale": [1, 1, 1]}, {"frame": 31, "scale": [1.04, 1.04, 1.04]}]}

        brief = create_brief({"type": "IDLE", "profile": row["profile"], "fps": 30, "duration_s": 1,
                              "root_mode": "IN_PLACE", "strength": .2, "forward_axis": "-Y"})
        compatibility = []
        plan = None
        for caller_frame in (1, 16):
            rig.animation_data.action = source
            scene.frame_set(caller_frame)
            options = {"mapping": row["mapping"], "brief": brief, "ground_height_m": GROUND_HEIGHT_M,
                       "stage": "BLOCKING", "generation_id": "determinism-diagnostic-" + str(caller_frame),
                       "candidate_name": "M6_Determinism_Frame" + str(caller_frame) + "_" + uuid4().hex[:10]}
            comp = bridge.run(BASE_RUNTIME + GENERATION_RUNTIME + COMPAT_READ,
                              {"object_name": rig.name, "options": options})
            compatibility.append(comp)
            report["observations"].append({"caller_frame": caller_frame, "compatibility": comp,
                                           "source_chest_scale": list(chest.scale)})
            if not comp["supported"]:
                if expect_rejection:
                    count = len(bpy.data.actions)
                    proposed = bridge.run(BASE_RUNTIME + runtime_for_stage("BLOCKING") + READ,
                                          {"object_name": rig.name, "options": dict(options, plan=plan_poses(brief, {}))})
                    check("animated unauthored scale rejects from caller frame " + str(caller_frame),
                          "UNAUTHORED_POSE_CHANNELS" in " ".join(comp["reasons"])
                          and not proposed["proposal"].get("supported") and len(bpy.data.actions) == count,
                          {"compatibility_reasons": comp["reasons"], "proposal_reasons": proposed["proposal"].get("reasons")})
                    continue
                report["status"] = "refuted_by_compatibility_rejection"
                report["reasons"] = comp["reasons"]
                break
            if plan is None:
                plan = plan_poses(brief, comp)
            options["plan"] = plan
            stage_body = BASE_RUNTIME + runtime_for_stage("BLOCKING")
            proposed = bridge.run(stage_body + READ, {"object_name": rig.name, "options": options})
            check("proposal from caller frame " + str(caller_frame) + " is supported",
                  proposed["proposal"].get("supported"), proposed["proposal"].get("reasons"))
            params = {key: value for key, value in proposed.items() if key not in {"source_snapshot", "context"}}
            params.update(object_name=rig.name, options=options, mode="preview", suffix=uuid4().hex[:8],
                          source_fingerprint=runtime_digest(proposed["source_snapshot"]),
                          context_fingerprint=runtime_digest(proposed["context"]))
            result = bridge.run(stage_body + EXECUTE, params)
            result.pop("working_snapshot", None)
            check("generation from caller frame " + str(caller_frame) + " preserves diagnostic source",
                  result["source_intact"] and source_snapshot(source.name) == source_fingerprint)
            measured = candidate_metrics(result["working_action"], caller_frame, rig)
            report["generations"].append({"caller_frame": caller_frame, "rig_fingerprint": comp["rig_fingerprint"],
                                          "candidate": result["working_action"], "result": result, "evaluated": measured})
        if len(report["generations"]) == 2:
            check("rig signature is unchanged by the active original frame",
                  compatibility[0]["rig_fingerprint"] == compatibility[1]["rig_fingerprint"])
            first, second = report["generations"]
            head_difference = (Vector(first["evaluated"]["positions_m"]["head"]) -
                               Vector(second["evaluated"]["positions_m"]["head"])).length
            same_candidate_first = candidate_metrics(first["candidate"], 1, rig)
            same_candidate_second = candidate_metrics(first["candidate"], 16, rig)
            repeated_difference = (Vector(same_candidate_first["positions_m"]["head"]) -
                                   Vector(same_candidate_second["positions_m"]["head"])).length
            report["metrics"] = {"two_generated_candidates_head_difference_m": head_difference,
                                  "same_candidate_head_difference_m": repeated_difference,
                                  "same_candidate_fingerprint_unchanged": same_candidate_first["candidate_fingerprint"] == same_candidate_second["candidate_fingerprint"],
                                  "same_candidate_from_frame_1": same_candidate_first,
                                  "same_candidate_from_frame_16": same_candidate_second}
            report["generation_determinism"] = "fail" if max(head_difference, repeated_difference) > 1e-6 else "pass"
            report["status"] = "bug_reproduced" if report["generation_determinism"] == "fail" else "concern_refuted"
            report["all_numeric_generation_checks_passed"] = all(item["result"]["numeric_verification"] == "pass" for item in report["generations"])
            print(json.dumps({"diagnostic": report["status"], "metrics": {k: v for k, v in report["metrics"].items() if not k.startswith("same_candidate_from")}}), flush=True)
        check("existing qualified source Action is unchanged", source_snapshot(original.name) == original_fingerprint)
        check("diagnostic source remains unchanged after generation", source_snapshot(source.name) == source_fingerprint)
        if expect_rejection:
            check("no scale-dependent candidates created", not report["generations"])
            row = activate_fixture(fixture_report["fixtures"], "medium_rig")
            rig = bpy.data.objects[row["object"]]
            original = bpy.data.actions[row["action"]]
            original_fingerprint = source_snapshot(original.name)
            source = original.copy()
            source.name = "M6_UnmappedOffset_DiagnosticSource_" + uuid4().hex[:12]
            source.use_fake_user = True
            rig.animation_data.action = source
            auxiliary = rig.pose.bones["auxiliary_000"]
            auxiliary.location = (0, 0, 0)
            auxiliary.keyframe_insert(data_path="location", frame=1)
            auxiliary.location = (.02, 0, 0)
            auxiliary.keyframe_insert(data_path="location", frame=31)
            source_fingerprint = source_snapshot(source.name)
            for caller_frame in (1, 16):
                scene.frame_set(caller_frame)
                options = {"mapping": row["mapping"], "brief": brief, "ground_height_m": GROUND_HEIGHT_M,
                           "stage": "BLOCKING", "generation_id": "unmapped-offset-rejection",
                           "plan": plan_poses(brief, {})}
                count = len(bpy.data.actions)
                comp = bridge.run(BASE_RUNTIME + GENERATION_RUNTIME + COMPAT_READ,
                                  {"object_name": rig.name, "options": options})
                proposed = bridge.run(BASE_RUNTIME + runtime_for_stage("BLOCKING") + READ,
                                      {"object_name": rig.name, "options": options})
                check("animated unmapped location rejects from caller frame " + str(caller_frame),
                      not comp["supported"] and "UNAUTHORED_POSE_CHANNELS" in " ".join(comp["reasons"])
                      and not proposed["proposal"].get("supported") and len(bpy.data.actions) == count,
                      {"reasons": comp["reasons"], "original_frame": caller_frame})
            check("unmapped offset diagnostic and existing source remain unchanged",
                  source_snapshot(source.name) == source_fingerprint
                  and source_snapshot(original.name) == original_fingerprint)

            for scenario in fixture_report["fixtures"]["scenarios"]:
                row = activate_fixture(fixture_report["fixtures"], scenario)
                bpy.data.objects[row["object"]].animation_data.action=bpy.data.actions[row["action"]]
                scene.frame_set(1)
                options = {"mapping": row["mapping"], "brief": brief, "ground_height_m": GROUND_HEIGHT_M}
                comp = bridge.run(BASE_RUNTIME + GENERATION_RUNTIME + COMPAT_READ,
                                  {"object_name": row["object"], "options": options})
                expected = row["expected_compatibility"] == "supported"
                check(scenario + " compatibility remains correct", comp["supported"] == expected, comp.get("reasons"))
                report["compatibility_smoke"][scenario] = comp

            cases = [("standard_humanoid", "IDLE", "IN_PLACE"), ("heavy_humanoid", "ATTACK", "IN_PLACE"),
                     ("asymmetric_creature", "IDLE", "IN_PLACE"), ("heavy_humanoid", "HIT_REACTION", "IN_PLACE"),
                     ("standard_humanoid", "LOCOMOTION", "IN_PLACE"), ("heavy_humanoid", "LOCOMOTION", "ROOT_MOTION"),
                     ("large_rig", "IDLE", "IN_PLACE")]
            for scenario, intent_type, root_mode in cases:
                row = activate_fixture(fixture_report["fixtures"], scenario)
                rig = bpy.data.objects[row["object"]]
                source = bpy.data.actions[row["action"]]
                rig.animation_data.action=source
                scene.frame_set(1)
                source_fingerprint = source_snapshot(source.name)
                normalized = create_brief({"type": intent_type, "profile": row["profile"], "fps": 30,
                                           "duration_s": 1, "root_mode": root_mode, "strength": .5,
                                           "forward_axis": "-Y", "stride_length_m": .04, "speed_m_s": .04})
                options = {"mapping": row["mapping"], "brief": normalized, "ground_height_m": GROUND_HEIGHT_M}
                comp = bridge.run(BASE_RUNTIME + GENERATION_RUNTIME + COMPAT_READ,
                                  {"object_name": rig.name, "options": options})
                pose_plan = plan_poses(normalized, comp)
                generation_id = "baseline-smoke-" + uuid4().hex[:10]
                stages = []
                for name in ("BLOCKING", "BREAKDOWNS", "SPLINE"):
                    options.update(plan=pose_plan, stage=name, generation_id=generation_id,
                                   candidate_name="M6_PoseBaseline_" + name + "_" + uuid4().hex[:10])
                    stage_body = BASE_RUNTIME + runtime_for_stage(name)
                    proposed = bridge.run(stage_body + READ, {"object_name": rig.name, "options": options})
                    check(scenario + "/" + intent_type + "/" + root_mode + " " + name + " proposal supported",
                          proposed["proposal"].get("supported"), proposed["proposal"].get("reasons"))
                    params = {key: value for key, value in proposed.items() if key not in {"source_snapshot", "context"}}
                    params.update(object_name=rig.name, options=options, mode="apply", suffix=uuid4().hex[:8],
                                  source_fingerprint=runtime_digest(proposed["source_snapshot"]),
                                  context_fingerprint=runtime_digest(proposed["context"]))
                    value = bridge.run(stage_body + EXECUTE, params)
                    check(scenario + "/" + intent_type + "/" + root_mode + " " + name + " numeric generation passes",
                          value.get("numeric_verification") == "pass" and value.get("source_intact"), value.get("errors"))
                    stages.append({"stage": name, "candidate": value["working_action"],
                                   "numeric_verification": value["numeric_verification"]})
                check(scenario + "/" + intent_type + "/" + root_mode + " immutable original remains unchanged",
                      source_snapshot(source.name) == source_fingerprint)
                report["profile_smoke"].append({"scenario": scenario, "type": intent_type, "root_mode": root_mode,
                                               "bones": comp["bone_count"], "controlled_bones": comp["controlled_bone_count"],
                                               "stages": stages})
            report["status"] = "pass"
            report["generation_determinism"] = "unsupported_inputs_safely_rejected"
        check("qualified input file remains byte-identical", file_sha256(qualified_source) == qualified_sha)
        check("reserved input copy remains byte-identical", file_sha256(source_copy) == qualified_sha)
    except Exception as exc:
        report["status"] = "diagnostic_error"
        report["errors"] = [str(exc), traceback.format_exc()]
        raise
    finally:
        report["elapsed_seconds"] = time.perf_counter() - started
        report["maximum_safe_payload_bytes"] = bridge.maximum_payload_bytes
        report["safe_payload_calls"] = bridge.calls
        report["count"] = len(report["checks"])
        save()


def run(blender, source_report, expect_rejection=False):
    _, candidate, expected = controlled_source(source_report)
    output = ROOT / "test-artifacts/m6" / ("pose-determinism-" + uuid4().hex[:10])
    output.mkdir(parents=True, exist_ok=False)
    source_copy = output / "controlled-input-copy.blend"
    shutil.copy2(candidate, source_copy)
    if file_sha256(source_copy) != expected:
        raise RuntimeError("Controlled input copy failed checksum validation")
    args = [str(Path(blender).resolve(strict=True)), "--background", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", str(Path(__file__).resolve()), "--", "--inside",
            "--source-report", str(Path(source_report).resolve()), "--source-copy", str(source_copy), "--output", str(output)]
    if expect_rejection:
        args.append("--expect-rejection")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (output / "blender.log").open("x", encoding="utf-8") as log:
        process = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        print(json.dumps({"status": "running", "pid": process.pid, "report": str(output / "report.json")}), flush=True)
        code = process.wait(timeout=180)
    if code:
        raise RuntimeError("Isolated diagnostic failed: " + str(output))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    print(json.dumps({"status": report["status"], "count": report["count"], "report": str(output / "report.json"),
                      "metrics": {key: value for key, value in report.get("metrics", {}).items() if not key.startswith("same_candidate_from")}}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--blender", default=r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe")
    parser.add_argument("--source-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--source-copy", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expect-rejection", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
    if args.inside:
        inside(args.source_report, args.source_copy, args.output, args.expect_rejection)
    else:
        run(args.blender, args.source_report, args.expect_rejection)
