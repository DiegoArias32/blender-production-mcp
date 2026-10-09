"""Real Blender fixture/solver qualification for M6; isolated factory startup.

This probe qualifies the generated rigs, not the finished generation subsystem.
The coordinator runs generation/transaction/demos separately, using this exact
fixture manifest. No assertion below claims artistic or temporal quality.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(os.environ.get("BLENDER_PRODUCTION_TEST_ARTIFACTS") or ROOT / "test-artifacts")


def inside(output, generation=False):
    import bpy
    from mathutils import Vector, Quaternion
    sys.path.insert(0, str(ROOT / "tests/blender"))
    from fixtures_m6 import build, activate_fixture, REQUIRED_SCENARIOS, GROUND_HEIGHT_M
    output = Path(output)
    started = time.perf_counter()
    manifest = build(output, suite=True)
    report = {"schema": 1, "status": "running", "blender": bpy.app.version_string,
              "blender_hash": bpy.app.build_hash.decode("ascii"), "checks": [],
              "fixtures": manifest, "metrics": {}, "warnings": [],
              "scope": "fixture_structure_and_installed_solver_only"}

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def check(name, condition, data=None):
        if not condition:
            raise AssertionError(name + (": " + repr(data) if data is not None else ""))
        report["checks"].append({"name": name, "passed": True, "metrics": data})
        print("PASS " + name, flush=True)
        save()

    def action_digest(action):
        curves = ([curve for layer in action.layers for strip in layer.strips
                   for bag in strip.channelbags for curve in bag.fcurves]
                  if action.is_action_layered else list(action.fcurves))
        values = {"name": action.name, "custom": dict(action.items()), "curves": [
            {"path": curve.data_path, "index": curve.array_index,
             "keys": [{"co": list(key.co), "left": list(key.handle_left), "right": list(key.handle_right),
                       "interpolation": key.interpolation, "left_type": key.handle_left_type,
                       "right_type": key.handle_right_type} for key in curve.keyframe_points]}
            for curve in curves]}
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()

    originals = {row["action"]: action_digest(bpy.data.actions[row["action"]])
                 for row in manifest["scenarios"].values()}
    try:
        check("all fifteen required scenarios exist", set(REQUIRED_SCENARIOS) <= set(manifest["scenarios"]))
        check("fixture checkpoint exists and matches its digest",
              hashlib.sha256(Path(manifest["checkpoint"]["filepath"]).read_bytes()).hexdigest() == manifest["checkpoint"]["sha256"])
        check("generated blend exists and matches its digest",
              hashlib.sha256(Path(manifest["blend"]).read_bytes()).hexdigest() == manifest["sha256"])
        check("controlled metres and FPS", bpy.context.scene.unit_settings.scale_length == 1 and bpy.context.scene.render.fps == 30)
        required_solver_properties = {"chain_count", "use_tail", "use_stretch", "use_rotation", "pole_angle", "pole_target", "pole_subtarget"}
        check("installed IK RNA exposes actual adapter properties",
              required_solver_properties <= set(bpy.types.KinematicConstraint.bl_rna.properties.keys()))
        report["installed_api"] = {"ik_properties": sorted(required_solver_properties),
                                   "layered_actions": all(bpy.data.actions[name].is_action_layered for name in originals)}
        rest_errors, skin_stats = {}, {}
        for scenario, row in manifest["scenarios"].items():
            rig, body = bpy.data.objects[row["object"]], bpy.data.objects[row["body"]]
            check(scenario + " has explicit fixture markers", bool(rig["fixture_only"]) and bool(body["fixture_only"]))
            check(scenario + " keeps distinct immutable source Action", bool(bpy.data.actions[row["action"]]["m6_source_immutable"]))
            check(scenario + " mesh has only bounded summary data", 0 < len(body.data.vertices) < 1000)
            check(scenario + " skin weights cover each proxy vertex exactly once",
                  all(len(vertex.groups) == 1 and abs(vertex.groups[0].weight - 1) < 1e-7 for vertex in body.data.vertices))
            skin_stats[scenario] = {"vertices": len(body.data.vertices), "triangles": row["mesh_triangles"],
                                    "bones": len(rig.data.bones), "deform_bones": row["deform_bones"]}
            if row["variant"] == "unsupported":
                check("unsupported fixture intentionally has no semantic body map", not row["mapping"] and len(rig.data.bones) == 2)
                continue
            activate_fixture(manifest, scenario)
            bpy.context.scene.frame_set(1)
            bpy.context.view_layer.update()
            check(scenario + " explicit mapping resolves actual bones", set(row["mapping"].values()) <= set(rig.data.bones.keys()))
            for side in ("L", "R"):
                shin, control, pole = rig.pose.bones["shin." + side], rig.pose.bones["foot_ik." + side], rig.pose.bones["pole." + side]
                solver = next(constraint for constraint in shin.constraints if constraint.type == "IK")
                check(scenario + " " + side + " solver uses separate independent controls",
                      solver.chain_count == 2 and solver.target == rig and solver.pole_target == rig
                      and solver.subtarget == control.name and solver.pole_subtarget == pole.name
                      and control.parent is None and pole.parent is None and not control.bone.use_deform and not pole.bone.use_deform
                      and solver.influence == 1 and not solver.use_stretch and not solver.use_rotation)
                error = (rig.pose.bones["foot." + side].head - control.head).length
                rest_errors[scenario + "." + side] = error
                check(scenario + " " + side + " installed IK reaches foot target", error <= .0001, {"error_m": error})
                check(scenario + " " + side + " foot marker is on configured ground",
                      abs(rig.pose.bones["foot." + side].head.z - GROUND_HEIGHT_M) <= .0001)
                foot_group = body.vertex_groups["foot." + side].index
                sole = min(vertex.co.z for vertex in body.data.vertices
                           if any(group.group == foot_group for group in vertex.groups))
                check(scenario + " " + side + " visible rest sole shares the contact plane",
                      abs(sole - GROUND_HEIGHT_M) <= .0001, {"minimum_rest_sole_z_m": sole})
                check(scenario + " " + side + " FK arm has no hidden solver", not rig.pose.bones["upper_arm." + side].constraints
                      and not rig.pose.bones["forearm." + side].constraints)
        check("medium fixture has additional hierarchy-only bones", manifest["scenarios"]["medium_rig"]["bones"] == 62)
        check("large fixture really exceeds one hundred bones", manifest["scenarios"]["large_rig"]["bones"] == 131)
        creature = manifest["scenarios"]["asymmetric_creature"]
        rig = bpy.data.objects[creature["object"]]
        right_length = sum(rig.data.bones[name + ".R"].length for name in ("upper_arm", "forearm", "hand"))
        left_length = sum(rig.data.bones[name + ".L"].length for name in ("upper_arm", "forearm", "hand"))
        check("intentional asymmetric arm geometry is preserved", right_length > left_length * 1.25,
              {"right_arm_m": right_length, "left_arm_m": left_length, "ratio": right_length / left_length})
        check("asymmetric mass has visible distinct surface", any(polygon.material_index == 3 for polygon in bpy.data.objects[creature["body"]].data.polygons))
        constrained = bpy.data.objects[manifest["scenarios"]["constrained_rig"]["object"]]
        check("constrained fixture retains deliberately unsupported joint limit", any(c.type == "LIMIT_ROTATION" for c in constrained.pose.bones["chest"].constraints))

        # Solver experiments are transient and happen only in our fixture process.
        row = activate_fixture(manifest, "heavy_humanoid")
        rig = bpy.data.objects[row["object"]]
        bpy.context.scene.frame_set(1)
        before = {side: rig.pose.bones["foot." + side].head.copy() for side in ("L", "R")}
        root = rig.pose.bones["root"]
        state = root.location.copy()
        try:
            root.location = root.bone.matrix_local.to_3x3().inverted() @ Vector((0, -.025, -.025))
            rig.update_tag()
            bpy.context.view_layer.update()
            planted = {side: (rig.pose.bones["foot." + side].head - before[side]).length for side in ("L", "R")}
            check("root movement does not move independent planted IK feet", max(planted.values()) <= .0001,
                  {"foot_marker_displacements_m": planted})
        finally:
            root.location = state
            rig.update_tag()
            bpy.context.view_layer.update()
        control = rig.pose.bones["foot_ik.L"]
        state = control.location.copy()
        try:
            control.location = control.bone.matrix_local.to_3x3().inverted() @ Vector((0, -.045, .04))
            rig.update_tag()
            bpy.context.view_layer.update()
            motion = (rig.pose.bones["foot.L"].head - before["L"]).length
            target_error = (rig.pose.bones["foot.L"].head - control.head).length
            check("IK generation target moves solver without deform keyframes", motion > .05 and target_error <= .0001,
                  {"motion_m": motion, "target_error_m": target_error})
        finally:
            control.location = state
            rig.update_tag()
            bpy.context.view_layer.update()
        arm = rig.pose.bones["upper_arm.R"]
        hand_before = rig.pose.bones["hand.R"].head.copy()
        state = arm.rotation_quaternion.copy()
        try:
            arm.rotation_quaternion = Quaternion((1, 0, 0), .25)
            rig.update_tag()
            bpy.context.view_layer.update()
            hand_motion = (rig.pose.bones["hand.R"].head - hand_before).length
            check("FK arm hierarchy moves its hand", hand_motion > .1, {"hand_motion_m": hand_motion})
        finally:
            arm.rotation_quaternion = state
            rig.update_tag()
            bpy.context.view_layer.update()
        check("all source Actions remain exactly unchanged", originals == {
            name: action_digest(bpy.data.actions[name]) for name in originals})
        check("fixture source file remains unchanged after solver probes",
              hashlib.sha256(Path(manifest["blend"]).read_bytes()).hexdigest() == manifest["sha256"])
        report["source_action_digests"] = originals
        report["metrics"].update(maximum_rest_target_error_m=max(rest_errors.values()),
                                  rest_target_errors_m=rest_errors, fixtures=skin_stats,
                                  elapsed_seconds=round(time.perf_counter() - started, 4))
        if generation:
            qualify_generation(manifest, report, check, output)
            check("all fixture source Actions unchanged after generation/negatives/performance", originals == {
                name: action_digest(bpy.data.actions[name]) for name in originals})
            check("fixture source file unchanged after generation/negatives/performance",
                  hashlib.sha256(Path(manifest["blend"]).read_bytes()).hexdigest() == manifest["sha256"])
        report.update(status="pass", count=len(report["checks"]))
        save()
        print("M6_FIXTURE_REPORT=" + str(output / "report.json"), flush=True)
    except Exception as exc:
        report.update(status="fail", error=str(exc), count=len(report["checks"]))
        save()
        raise


def qualify_generation(manifest, report, check, output):
    """Measure the actual Blender adapter; host lifecycle/asset gates are separate."""
    import bpy
    import types
    sys.path.insert(0, str(ROOT / "src"))
    package = types.ModuleType("blender_mcp")
    package.__path__ = [str(ROOT / "src/blender_mcp")]
    sys.modules["blender_mcp"] = package
    from blender_mcp.production.common.bridge import build_code, payload_bytes
    from blender_mcp.production.common.digest import runtime_digest
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME, READ, EXECUTE
    from blender_mcp.production.animation.generation.runtime import (
        GENERATION_RUNTIME, COMPAT_READ, ANALYZE_READ, SILHOUETTE_READ, runtime_for_stage,
    )
    from blender_mcp.production.animation.generation.planner import create_brief, plan_poses
    from fixtures_m6 import activate_fixture

    class LocalBridge:
        last_bytes = 0
        def run(self, body, params=None):
            self.last_bytes = payload_bytes(body, params)
            namespace = {}
            exec(build_code(body, params).rsplit("print(", 1)[0], namespace)
            # Match the socket JSON boundary; callers cannot retain RNA pointers.
            return json.loads(json.dumps(namespace["result"], allow_nan=False))

    bridge = LocalBridge()
    body = BASE_RUNTIME + GENERATION_RUNTIME
    adapter = {"scope": "installed_Blender_adapter_via_validated_production_payloads",
               "compatibility": {}, "stages": {}, "negative_tests": {}, "performance": [],
               "limitations": ["Controlled semantic plans qualify the adapter; planned_demos qualify the actual intent planner separately",
                               "Direct backend checks do not certify host asset-role, lifecycle, operation-id or visual-review gates"]}
    report["generation_adapter"] = adapter

    def semantic(amplitude=0, root_distance=0):
        return {"root_translation_m": [0, root_distance, 0], "pelvis_offset_m": [0, 0, -.025],
                "torso_pitch_deg": amplitude * .16, "arm_swing_deg": {"left": amplitude, "right": -amplitude},
                "arm_abduction_deg": {"left": 8, "right": 8}, "elbow_flex_deg": {"left": 12, "right": 12},
                "feet": {side: {"offset_m": [0, 0, 0], "contact": True, "contact_phase": "FOOT_FLAT"}
                         for side in ("left", "right")}}

    def controlled_plan(fps=30, seconds=1):
        end = 1 + fps * seconds
        middle = 1 + fps * seconds / 2
        return {"poses": [{"frame": 1., "phase": "NEUTRAL", "semantic": semantic()},
                          {"frame": float(middle), "phase": "WEIGHT_SHIFT", "semantic": semantic(12)},
                          {"frame": float(end), "phase": "RETURN", "semantic": semantic()}],
                "breakdowns": [{"frame": (1 + middle) / 2, "phase": "SECONDARY_BODY_MOTION", "semantic": semantic(5)},
                               {"frame": (middle + end) / 2, "phase": "RECOVERY", "semantic": semantic(4)}]}

    def options(row, stage="BLOCKING", plan=None, generation_id="adapter-fixture"):
        fps = bpy.context.scene.render.fps
        plan = controlled_plan(fps=fps) if plan is None else deepcopy(plan)
        first, last = plan["poses"][0]["frame"], plan["poses"][-1]["frame"]
        has_root_motion = any(any(pose["semantic"].get("root_translation_m", [0, 0, 0]))
                              for pose in plan["poses"])
        # Raw adapter vectors retain their historical +Y basis. Actual planner
        # cases replace this brief with their explicit anatomical -Y intent.
        brief = create_brief({"type": "IDLE", "profile": row["profile"], "forward_axis": "+Y",
                              "fps": fps, "frame_start": int(first), "duration_s": (last - first) / fps,
                              "root_mode": "ROOT_MOTION" if has_root_motion else "IN_PLACE",
                              "mass_asymmetry": {"left": 1, "right": 1.8 if row["variant"] == "asymmetric" else 1}})
        return {"mapping": deepcopy(row["mapping"]), "brief": brief,
                "plan": plan, "stage": stage,
                "generation_id": generation_id, "ground_height_m": .08,
                "candidate_name": "M6_Adapter_" + uuid4().hex[:16]}

    def propose(row, opts):
        stage_body = BASE_RUNTIME + runtime_for_stage(opts["stage"])
        return bridge.run(stage_body + READ, {"object_name": row["object"], "options": opts})

    def execute(row, opts, proposal=None, mode="apply"):
        data = propose(row, opts) if proposal is None else proposal
        if not data["proposal"].get("supported"):
            raise ValueError("Adapter proposal rejected: " + repr(data["proposal"].get("reasons")))
        params = {key: value for key, value in data.items() if key not in {"source_snapshot", "context"}}
        params.update(object_name=row["object"], options=opts, mode=mode, suffix=uuid4().hex[:8],
                      source_fingerprint=runtime_digest(data["source_snapshot"]),
                      context_fingerprint=runtime_digest(data["context"]))
        stage_body = BASE_RUNTIME + runtime_for_stage(opts["stage"])
        value = bridge.run(stage_body + EXECUTE, params)
        value.pop("working_snapshot", None)
        value["payload_bytes"] = bridge.last_bytes
        return value

    def refusal(name, row, opts, token):
        count = len(bpy.data.actions)
        result = propose(row, opts)
        reasons = result["proposal"].get("reasons", [])
        check(name + " rejected before Candidate Action", not result["proposal"].get("supported")
              and token in " ".join(reasons) and len(bpy.data.actions) == count, {"reasons": reasons})
        adapter["negative_tests"][name] = {"status": "safe_rejection", "reasons": reasons, "no_candidate_created": True}

    for scenario, row in manifest["scenarios"].items():
        activate_fixture(manifest, scenario)
        opts = options(row)
        value = bridge.run(body + COMPAT_READ, {"object_name": row["object"], "options": opts})
        expected = row["expected_compatibility"] == "supported"
        check(scenario + " actual generation compatibility", value["supported"] == expected,
              {"confidence": value["confidence"], "reasons": value["reasons"]})
        adapter["compatibility"][scenario] = value
        if expected:
            proposal = propose(row, opts)
            check(scenario + " pose plan validated without Action creation", proposal["proposal"]["supported"]
                  and proposal["proposal"]["pose_validation"]["passed"], proposal["proposal"].get("pose_validation"))

    row = activate_fixture(manifest, "heavy_humanoid")
    rig = bpy.data.objects[row["object"]]
    original = bpy.data.actions[row["action"]]
    generation_id = "m6-direct-stage-" + uuid4().hex[:12]
    previous = original
    stage_plan = controlled_plan()
    try:
        for stage in ("BLOCKING", "BREAKDOWNS", "TIMING", "ARCS", "BALANCE", "ROOT", "CONTACTS", "SPLINE", "CURVES", "OVERLAP", "FOLLOW_THROUGH"):
            if stage == "ROOT":
                for pose in stage_plan["poses"] + stage_plan["breakdowns"]:
                    pose["semantic"]["root_translation_m"] = [0, .05 * (pose["frame"] - 1) / 30, 0]
            opts = options(row, stage=stage, plan=stage_plan, generation_id=generation_id)
            t0 = time.perf_counter()
            value = execute(row, opts)
            elapsed = (time.perf_counter() - t0) * 1000
            adapter["stages"][stage] = dict(value, elapsed_ms=round(elapsed, 3))
            check(stage + " real adapter numeric verification", value["numeric_verification"] == "pass", value.get("errors"))
            check(stage + " creates distinct Candidate Action and retains previous", value["working_action"] != previous.name
                  and previous.name in bpy.data.actions and value["source_intact"])
            check(stage + " respects upstream payload capacity", value["payload_bytes"] <= 200000)
            previous = bpy.data.actions[value["working_action"]]
            if stage == "BLOCKING":
                check("blocking is stepped", set(value["metrics"]["interpolation_counts"]) == {"CONSTANT"})
            if stage == "SPLINE":
                check("spline mixes Bezier body and linear contact/root channels", {"LINEAR", "BEZIER"} <= set(value["metrics"]["interpolation_counts"]))
            if stage == "ROOT":
                root_delta = value["metrics"]["root_motion"]["displacement_xyz_m"]
                check("root-motion stage generates measured five-centimetre travel", abs(root_delta[1] - .05) <= .0001,
                      {"displacement_xyz_m": root_delta})
        opts = options(row, stage="FOLLOW_THROUGH", plan=stage_plan, generation_id=generation_id)
        opts["action_name"] = previous.name
        measured = bridge.run(body + ANALYZE_READ, {"object_name": row["object"], "options": opts})
        adapter["final_measurement"] = measured
        check("independent evaluated candidate QA passes", measured["passed"], measured.get("errors"))
        silhouette = bridge.run(body + SILHOUETTE_READ, {"object_name": row["object"], "options": opts})
        adapter["silhouette"] = silhouette
        check("silhouette returns three measured projections without artistic score",
              set(silhouette["views"]) == {"FRONT", "SIDE", "THREE_QUARTER"} and silhouette["artistic_score"] is None)
        opts = options(row, stage="EFFECTOR", plan=stage_plan, generation_id=generation_id)
        opts["effector"] = {"side": "left", "target_world_m": [.35, .30, 1.25],
                            "pole_world_m": [.65, -.30, 1.1], "frame_start": 8, "frame_end": 23,
                            "tolerance_m": .001,
                            "surface": {"type": "PLANE", "static": True,
                                        "point_world_m": [.35, .30, 1.25], "normal_world": [0, -1, 0]}}
        value = execute(row, opts)
        adapter["stages"]["EFFECTOR"] = value
        check("finite FK hand contact verifies actual endpoint and outside-window channels", value["numeric_verification"] == "pass"
              and value["metrics"].get("outside_effector_window_max_channel_error", 1) <= 1e-5, value.get("errors"))
        check("finite hand contact retains its generated source Action", previous.name in bpy.data.actions and value["source_intact"])
        endpoint = bridge.run(BASE_RUNTIME + r'''
obj=m2_object(params['object_name'])
state=m3_capture(obj)
values=[]
try:
    target=Vector(params['target_world_m'])
    for index in range(61):
        frame=8+index*.25
        m3_set_frame(frame)
        evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        point=evaluated.matrix_world@evaluated.pose.bones[params['hand']].head*bpy.context.scene.unit_settings.scale_length
        values.append((point-target).length)
    result={'samples':len(values),'maximum_endpoint_residual_m':max(values),'step_frames':.25,'frame_range':[8,23]}
finally:
    m3_restore(obj,state)
''', {"object_name": row["object"], "hand": row["mapping"]["hand_l"], "target_world_m": opts["effector"]["target_world_m"]})
        adapter["finite_hand_contact_measurement"] = endpoint
        check("finite hand contact is independently measured at quarter-frame intervals",
              endpoint["samples"] == 61 and endpoint["maximum_endpoint_residual_m"] <= .001, endpoint)
        bad = deepcopy(opts)
        bad["candidate_name"] = "M6_UnreachableHand_" + uuid4().hex[:8]
        bad["effector"]["target_world_m"] = [5, 5, 5]
        bad["effector"]["surface"]["point_world_m"] = [5, 5, 5]
        refusal("impossible_effector_reach", row, bad, "IMPOSSIBLE_REACH")
    finally:
        rig.animation_data.action = original
        if original.slots:
            rig.animation_data.action_slot = original.slots[0]
        bpy.context.scene.frame_set(1)

    bad = options(row)
    bad["candidate_name"] = original.name
    refusal("candidate_name_collision", row, bad, "CANDIDATE_ACTION_COLLISION")
    bad = options(row)
    bad["mapping"]["head"] = {"bone": "head", "confidence": .2}
    refusal("low_mapping_confidence", row, bad, "LOW_CONFIDENCE")
    bad = options(row)
    bad["mapping"]["chest"] = "spine"
    refusal("ambiguous_anatomical_roles", row, bad, "AMBIGUOUS_CONTROLS")
    bad = options(row)
    bad["mapping"]["root"] = "foot_ik.L"
    refusal("invalid_root_hierarchy", row, bad, "INVALID_ROOT")
    bad = options(row)
    bad["plan"]["poses"][1]["semantic"]["feet"]["left"]["offset_m"] = [0, 5, 0]
    refusal("impossible_limb_reach", row, bad, "IMPOSSIBLE_REACH")
    bad = options(row)
    bad["plan"]["poses"] = [{"frame": 1 + index * 30 / 601, "phase": "CAPACITY_TEST", "semantic": {}}
                             for index in range(602)]
    refusal("excessive_generation_capacity", row, bad, "CAPACITY_EXCEEDED")
    bad = options(row, stage="EFFECTOR")
    refusal("unconfigured_effector_contact", row, bad, "EFFECTOR_CONFIGURATION_REQUIRED")
    unsupported = manifest["scenarios"]["unsupported_rig"]
    refusal("unsupported_fixture_rig", unsupported, options(unsupported), "SEMANTIC_MAPPING_REQUIRED")
    constrained = manifest["scenarios"]["constrained_rig"]
    refusal("constrained_fixture_rig", constrained, options(constrained), "LOCKED_OR_CONSTRAINED_FK_CONTROL")
    solver = next(c for c in rig.pose.bones["shin.L"].constraints if c.type == "IK")
    influence = solver.influence
    try:
        solver.influence = .5
        refusal("conflicting_ik_fk", row, options(row), "CONFLICTING_IK_FK")
    finally:
        solver.influence = influence
        rig.update_tag()
        bpy.context.view_layer.update()
    for name, mutate, restore in (
        ("stale_scene_fps", lambda: setattr(bpy.context.scene.render, "fps", 60), lambda: setattr(bpy.context.scene.render, "fps", 30)),
        ("stale_rig_lock", lambda: setattr(rig.pose.bones["head"], "lock_location", (True, False, False)),
         lambda: setattr(rig.pose.bones["head"], "lock_location", (False, False, False))),
    ):
        opts = options(row)
        proposed = propose(row, opts)
        action_count = len(bpy.data.actions)
        mutate()
        try:
            execute(row, opts, proposed)
        except ValueError as exc:
            check(name + " rejects stale context before candidate creation", "STALE_PROPOSAL" in str(exc)
                  and len(bpy.data.actions) == action_count, {"reason": str(exc)})
            adapter["negative_tests"][name] = {"status": "safe_rejection", "reason": str(exc), "no_candidate_created": True}
        else:
            raise AssertionError(name + " stale proposal accepted")
        finally:
            restore()
    for name, change, error_token in (
        ("unacceptable_sliding", lambda plan: plan["poses"][1]["semantic"]["feet"]["left"].update(offset_m=[0, .045, 0]), "UNACCEPTABLE_SLIDING"),
        ("severe_limb_pop", lambda plan: plan["poses"][1]["semantic"]["arm_swing_deg"].update(right=125), "SEVERE_LIMB_POP"),
    ):
        plan = controlled_plan()
        change(plan)
        opts = options(row, plan=plan)
        expected_active = original
        try:
            if name == "severe_limb_pop":
                # A stepped blocking pose change is intentional. The required
                # rejection is a 125-degree change in ONE continuous frame.
                plan["poses"][1]["frame"] = 2.
                opts = options(row, plan=plan)
                blocking = execute(row, opts)
                check("severe-pop fixture blocking remains an inspectable stepped candidate", blocking["numeric_verification"] == "pass")
                expected_active = bpy.data.actions[blocking["working_action"]]
                opts = options(row, stage="SPLINE", plan=plan)
            value = execute(row, opts)
            check(name + " fails measured candidate QA and restores source", value["numeric_verification"] == "fail"
                  and error_token in " ".join(value.get("errors", [])) and rig.animation_data.action == expected_active,
                  {"errors": value.get("errors"), "metrics": value.get("metrics")})
            adapter["negative_tests"][name] = {"status": "candidate_rejected", "result": value}
        finally:
            rig.animation_data.action = original
            if original.slots:
                rig.animation_data.action_slot = original.slots[0]
            bpy.context.scene.frame_set(1)

    # Six bounded cases use the actual IDLE planner, both frame rates, 1/3/10s
    # and 21/62/131-bone rigs. Their scope is throughput, not artistic quality.
    for fps in (30, 60):
        for seconds, scenario in ((1, "standard_humanoid"), (3, "medium_rig"), (10, "large_rig")):
            row = activate_fixture(manifest, scenario)
            rig = bpy.data.objects[row["object"]]
            source = bpy.data.actions[row["action"]]
            bpy.context.scene.render.fps = fps
            bpy.context.scene.frame_end = 1 + fps * seconds
            generation_id = "m6-perf-" + uuid4().hex[:12]
            case = {"fps": fps, "seconds": seconds, "bones": row["bones"], "scenario": scenario, "milliseconds": {}, "actions": []}
            try:
                t0 = time.perf_counter()
                brief = create_brief({"type": "IDLE", "profile": row["profile"], "fps": fps,
                                      "duration_s": seconds, "root_mode": "IN_PLACE", "strength": .6,
                                      "forward_axis": "-Y"})
                case["milliseconds"]["brief"] = round((time.perf_counter() - t0) * 1000, 3)
                comp_opts = options(row)
                comp_opts["brief"] = brief
                comp = bridge.run(body + COMPAT_READ, {"object_name": row["object"], "options": comp_opts})
                check("performance %sfps/%ss/%sbones exposes measured adapter counts" % (fps, seconds, row["bones"]),
                      comp.get("bone_count") == len(rig.data.bones) and comp.get("controlled_bone_count") == 13)
                t0 = time.perf_counter()
                plan = plan_poses(brief, comp)
                case["milliseconds"]["planning"] = round((time.perf_counter() - t0) * 1000, 3)
                case["planning_capacity"] = plan["capacity"]
                case["planner_type"] = brief["type"]
                check("performance %sfps/%ss/%sbones actual intent planner" % (fps, seconds, row["bones"]), plan["supported"])
                for stage in ("BLOCKING", "BREAKDOWNS", "SPLINE"):
                    opts = options(row, stage=stage, plan=plan, generation_id=generation_id)
                    opts["brief"] = brief
                    t0 = time.perf_counter()
                    proposed = propose(row, opts)
                    case["milliseconds"][stage.lower() + "_proposal"] = round((time.perf_counter() - t0) * 1000, 3)
                    t0 = time.perf_counter()
                    value = execute(row, opts, proposed)
                    case["milliseconds"][stage.lower() + "_apply_and_qa"] = round((time.perf_counter() - t0) * 1000, 3)
                    case["actions"].append(value["working_action"])
                    check("performance %sfps/%ss/%sbones %s verifies" % (fps, seconds, row["bones"], stage),
                          value["numeric_verification"] == "pass", value.get("errors"))
                t0 = time.perf_counter()
                value = bridge.run(body + ANALYZE_READ, {"object_name": row["object"], "options": opts})
                case["milliseconds"]["independent_qa"] = round((time.perf_counter() - t0) * 1000, 3)
                case["qa_metrics"] = value["metrics"]
                check("performance %sfps/%ss/%sbones independent QA" % (fps, seconds, row["bones"]), value["passed"])
                adapter["performance"].append(case)
            finally:
                rig.animation_data.action = source
                if source.slots:
                    rig.animation_data.action_slot = source.slots[0]
                bpy.context.scene.render.fps = 30
                bpy.context.scene.frame_end = manifest["frame_range"][1]
                bpy.context.scene.frame_set(1)
    # Real intent-planner recipes, separately identified from the gentle adapter
    # benchmarks above. All requested values retain planner provenance.
    adapter["planned_demos"] = {}
    cases = (
        ("heavy_attack", "heavy_humanoid", {"type": "ATTACK", "profile": "HEAVY_HUMANOID", "attack_type": "OVERHEAD",
                                             "duration_s": 2.4, "root_mode": "IN_PLACE", "strength": 1}),
        ("walk_in_place", "locomotion", {"type": "LOCOMOTION", "profile": "HUMAN", "locomotion_type": "WALK",
                                           "duration_s": 2., "cycle_duration_s": 2., "root_mode": "IN_PLACE",
                                           "stride_length_m": .08, "step_height_m": .04, "speed_m_s": .04}),
        ("heavy_walk_root_motion", "root_motion_rig", {"type": "LOCOMOTION", "profile": "HEAVY_HUMANOID", "locomotion_type": "HEAVY_WALK",
                                                         "duration_s": 2., "cycle_duration_s": 2., "root_mode": "ROOT_MOTION",
                                                         "stride_length_m": .08, "step_height_m": .04, "speed_m_s": .04,
                                                         "forward_distance_m": .08}),
        ("asymmetric_idle", "asymmetric_creature", {"type": "IDLE", "profile": "ASYMMETRIC_CREATURE", "style": "HEAVY",
                                                       "duration_s": 3., "root_mode": "IN_PLACE", "strength": .8,
                                                       "mass_asymmetry": {"left": 1., "right": 1.8}}),
        ("hit_reaction", "hit_reaction", {"type": "HIT_REACTION", "profile": "HEAVY_HUMANOID", "duration_s": 1.8,
                                            "root_mode": "IN_PLACE", "impact_direction": "LEFT", "severity": .55, "strength": 1}),
    )
    for name, scenario, intent in cases:
        row = activate_fixture(manifest, scenario)
        rig = bpy.data.objects[row["object"]]
        source = bpy.data.actions[row["action"]]
        bpy.context.scene.render.fps = 30
        generation_id = "m6-planned-" + name + "-" + uuid4().hex[:8]
        demo = {"intent": intent, "actions": [], "stages": {}, "milliseconds": {}}
        adapter["planned_demos"][name] = demo
        try:
            t0 = time.perf_counter()
            brief = create_brief(dict(intent, fps=30, forward_axis="-Y"))
            demo["milliseconds"]["brief"] = round((time.perf_counter() - t0) * 1000, 3)
            opts = options(row)
            opts["brief"] = brief
            comp = bridge.run(body + COMPAT_READ, {"object_name": row["object"], "options": opts})
            check(name + " compatibility exposes measured planner counts",
                  comp.get("bone_count") == len(rig.data.bones) and comp.get("controlled_bone_count") == 13)
            t0 = time.perf_counter()
            plan = plan_poses(brief, comp)
            demo["milliseconds"]["planning"] = round((time.perf_counter() - t0) * 1000, 3)
            demo.update(brief=brief, pose_plan=plan, compatibility=comp)
            check(name + " real intent planner creates a supported nonmutating plan", plan["supported"] and plan["apply"] is False,
                  plan.get("errors"))
            check(name + " requested values retain user requirement provenance", brief["provenance"]["duration_s"]["source"] == "user_requirement"
                  and brief["provenance"]["profile"]["source"] == "user_requirement")
            bpy.context.scene.frame_end = brief["frame_end"]
            for stage in ("BLOCKING", "BREAKDOWNS", "SPLINE"):
                opts = options(row, stage=stage, plan=plan, generation_id=generation_id)
                opts["brief"] = brief
                t0 = time.perf_counter()
                proposed = propose(row, opts)
                demo["milliseconds"][stage.lower() + "_proposal"] = round((time.perf_counter() - t0) * 1000, 3)
                demo["stages"][stage] = {"proposal": proposed["proposal"]}
                check(name + " " + stage + " validates actual planned poses", proposed["proposal"]["supported"], proposed["proposal"].get("reasons"))
                t0 = time.perf_counter()
                value = execute(row, opts, proposed)
                demo["milliseconds"][stage.lower() + "_apply_and_qa"] = round((time.perf_counter() - t0) * 1000, 3)
                demo["stages"][stage]["result"] = value
                demo["actions"].append(value["working_action"])
                check(name + " " + stage + " generates and verifies in installed Blender", value["numeric_verification"] == "pass", value.get("errors"))
            measured = bridge.run(body + ANALYZE_READ, {"object_name": row["object"], "options": opts})
            demo["measured"] = measured
            check(name + " generated animation has independent sampled QA", measured["passed"], measured.get("errors"))
            root = measured["metrics"]["root_motion"]
            if brief["root_mode"] == "ROOT_MOTION":
                check(name + " measured root travel matches explicit intent", abs(root["displacement_xyz_m"][1] + brief["forward_distance_m"]) <= .0001,
                      {"root_motion": root, "forward_axis": brief["forward_axis"]})
            else:
                check(name + " in-place profile has no root drift", root["distance_m"] <= .0001, root)
            if brief["type"] == "LOCOMOTION":
                loop = bridge.run(BASE_RUNTIME + r'''
obj=m2_object(params['object_name'])
state=m3_capture(obj)
samples=[]
try:
    for frame in [params['start'],params['end']]:
        m3_set_frame(frame)
        evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        root=evaluated.matrix_world@evaluated.pose.bones[params['mapping']['root']].matrix
        samples.append({role:root.inverted()@(evaluated.matrix_world@evaluated.pose.bones[params['mapping'][role]].matrix)
                        for role in ['pelvis','head','hand_l','hand_r','foot_l','foot_r']})
    translations={role:(samples[1][role].translation-samples[0][role].translation).length*bpy.context.scene.unit_settings.scale_length for role in samples[0]}
    rotations={role:m2_angle(samples[1][role].to_quaternion().normalized(),samples[0][role].to_quaternion().normalized()) for role in samples[0]}
    result={'root_relative_position_errors_m':translations,'root_relative_angular_errors_deg':rotations,
            'maximum_position_error_m':max(translations.values()),'maximum_angular_error_deg':max(rotations.values()),
            'scope':'Sampled endpoint root-relative evaluated bone pose; full temporal/artistic loop review remains required'}
finally:
    m3_restore(obj,state)
''', {"object_name": row["object"], "mapping": row["mapping"], "start": brief["frame_start"], "end": brief["frame_end"]})
                demo["loop"] = loop
                check(name + " loop endpoints preserve root-relative limb and pelvis pose", loop["maximum_position_error_m"] <= .0002
                      and loop["maximum_angular_error_deg"] <= .1, loop)
            if brief["profile"] == "ASYMMETRIC_CREATURE":
                check(name + " asymmetric mass creates intentional compensation", any(abs(p["semantic"]["torso_roll_deg"]) > .1 for p in plan["poses"])
                      and brief["mass_asymmetry"]["right"] == 1.8)
        finally:
            rig.animation_data.action = source
            if source.slots:
                rig.animation_data.action_slot = source.slots[0]
            bpy.context.scene.frame_end = manifest["frame_range"][1]
            bpy.context.scene.frame_set(1)
    activate_fixture(manifest, "heavy_humanoid")
    state_path = output / "m6-adapter-candidates.blend"
    if state_path.exists():
        raise FileExistsError(state_path)
    bpy.ops.wm.save_as_mainfile(filepath=str(state_path), copy=True)
    adapter["candidate_scene"] = str(state_path)
    adapter["candidate_scene_sha256"] = hashlib.sha256(state_path.read_bytes()).hexdigest()


def run(blender, generation=False):
    if not blender or not Path(blender).is_file():
        raise ValueError("An explicit installed Blender executable is required")
    output = ARTIFACTS / "m6" / ("fixtures-" + uuid4().hex[:10])
    output.mkdir(parents=True, exist_ok=False)
    command = [blender, "--background", "--factory-startup", "--disable-autoexec",
                              "--python-exit-code", "1", "--python", str(Path(__file__).resolve()),
                              "--", "--inside", str(output)]
    if generation:
        command.append("--generation")
    process = subprocess.run(command, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=300)
    (output / "blender.log").write_text(process.stdout + "\n" + process.stderr, encoding="utf-8")
    if process.returncode:
        print(process.stdout[-14000:] + process.stderr[-3000:])
        raise RuntimeError("M6 fixture qualification failed; evidence: " + str(output))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    print(json.dumps({"status": report["status"], "count": report["count"],
                      "report": str(output / "report.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", type=Path)
    parser.add_argument("--blender")
    parser.add_argument("--generation", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
    inside(args.inside, args.generation) if args.inside else run(args.blender, args.generation)
