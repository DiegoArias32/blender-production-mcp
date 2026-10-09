"""M3 stdio/socket acceptance on four explicitly selected generated fixtures.

Only the coordinator may run this against the live addon. Bridge loading and
MCP sessions are strictly sequential; no second controller is kept connected.
The runner refuses production paths, checkpoints before every load, works in
new copies, restores a baseline per fixture and verifies original Action/file
fingerprints. A failed/uncertain mutation is recorded and never retried.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from blender_mcp.production.checkpoint import create_checkpoint
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.storage import Storage, sha256
from blender_mcp.production.inspect import snapshot


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "test-artifacts"
NEW_TOOLS = {
    "rig_classify", "rig_map_semantics", "rig_analyze_controls", "rig_analyze_limbs",
    "rig_compatibility_report", "rig_match_fk_to_ik", "rig_match_ik_to_fk",
    "rig_bake_fk_to_ik", "rig_bake_ik_to_fk", "animation_analyze_foot_phase",
    "animation_detect_contact_surface", "animation_analyze_effector_contact",
    "animation_detect_limb_pops", "animation_detect_limb_stretch",
    "animation_analyze_bezier", "animation_repair_bezier", "mocap_analyze",
    "mocap_map_skeleton", "mocap_preview_retarget", "mocap_retarget", "mocap_cleanup",
    "deformation_analyze", "deformation_compare", "deformation_test_pose",
    "animation_deformation_qa",
}


def fixture_descriptor(kind: str, selected: Path) -> dict:
    """No recursive autodetection: every file must be an explicit fixture input."""
    path = selected.resolve(strict=True)
    expected = {"rig": "m3-rig-fixtures.blend", "contact": "m3-contact-fixtures.blend",
                "mocap": "m3-mocap-fixture.blend", "deformation": "weighted-fixtures.blend"}
    if not path.is_file() or not path.is_relative_to(ARTIFACTS) or path.name != expected[kind]:
        raise ValueError("Only the named generated M3 fixture inside repository test-artifacts is allowed: " + kind)
    metadata_path = path.parent / ("fixtures.json" if kind == "rig" else "report.json" if kind == "deformation" else "fixture.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if kind == "deformation":
        if metadata.get("status") != "pass":
            raise ValueError("Deformation fixture must come from a passing controlled probe")
        metadata = metadata["fixture"]
    declared = metadata[{"rig": "blend", "contact": "file", "mocap": "path", "deformation": "path"}[kind]]
    if Path(declared).resolve(strict=True) != path:
        raise ValueError("Fixture manifest does not identify the selected file")
    if kind == "rig" and metadata.get("fixture_only") is not True:
        raise ValueError("Rig manifest lacks its controlled-fixture marker")
    actual_hash = sha256(path)
    if metadata.get("sha256") and metadata["sha256"] != actual_hash:
        raise ValueError("Fixture does not match its recorded SHA256")
    return {"kind": kind, "path": str(path), "sha256": actual_hash,
            "metadata_path": str(metadata_path), "metadata": metadata}


@asynccontextmanager
async def mcp_session(store: Path, log):
    environment = dict(os.environ, BLENDER_HOST="localhost", BLENDER_PORT="9876",
                       BLENDER_MCP_SAFE_MODE="1", DISABLE_TELEMETRY="true",
                       BLENDER_PRODUCTION_WORKDIR=str(store), PYTHONIOENCODING="utf-8",
                       # M3.1 asset guard: this demo's own workspace is the only extra fixture root.
                       BLENDER_PRODUCTION_FIXTURE_ROOTS=str(store))
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "blender_mcp.production.server"], env=environment)
    async with stdio_client(parameters, errlog=log) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


class Demo:
    def __init__(self, descriptors: list[dict], render_contact: bool = True):
        self.folder = ARTIFACTS / ("m3e-" + uuid4().hex[:8])
        self.folder.mkdir(parents=True, exist_ok=False)
        self.storage = Storage(self.folder / "s")
        self.render_contact = render_contact
        self.report = {"status": "running", "fixtures": descriptors, "checks": [],
                       "calls": [], "sessions": {}, "fixture_results": {},
                       "controller_policy": "Direct Bridge closes before MCP starts; MCP closes before next fixture load",
                       "production_assets_used": False, "visual_acceptance": "not_claimed"}
        self.kind = None
        self.client = None
        self.save()

    def save(self):
        (self.folder / "report.json").write_text(json.dumps(self.report, indent=2, ensure_ascii=False), encoding="utf-8")

    def check(self, name, condition):
        if not condition:
            raise AssertionError(name)
        self.report["checks"].append({"fixture": self.kind, "check": name})
        self.save()
        print("PASS " + name, flush=True)

    def load(self, descriptor):
        if self.client is not None:
            raise RuntimeError("MCP must be closed before the next direct Bridge connection")
        if sha256(Path(descriptor["path"])) != descriptor["sha256"]:
            raise ValueError("Fixture source changed before loading")
        bridge = Bridge()
        try:
            live = snapshot(bridge)
            source = live.get("source_file")
            # Empty/unknown scenes are refused as well: a new unsaved scene can
            # contain valuable work even without a production filename.
            if not source or not any(Path(source).resolve().is_relative_to(ROOT / part)
                                     for part in ("test-artifacts", ".production")):
                raise ValueError("Refusing to leave an unknown or production scene; only an existing generated working copy is allowed")
            checkpoint = create_checkpoint(bridge, self.storage,
                "Before M3 controlled fixture load: " + self.kind, "m3-load-" + self.kind, "M3 fixture load")
            state = self.report["fixture_results"][self.kind]
            state["before_load_source"] = source
            state["before_load_checkpoint"] = checkpoint
            self.save()
            working = self.storage.reserve("working", "m3-load-" + self.kind) / "scene.blend"
            if working.exists():
                raise FileExistsError(working)
            state["working_copy"] = bridge.run('''
import bpy
opened = bpy.ops.wm.open_mainfile(filepath=params["fixture"], use_scripts=False, load_ui=False)
if "FINISHED" not in opened:
    raise RuntimeError("Blender did not open the generated fixture")
# M3.1 (AUD-015): QA renders Cycles only on the GPU; the working copy's scenes use GPU Compute.
for scene in bpy.data.scenes:
    scene.cycles.device = "GPU"
saved = bpy.ops.wm.save_as_mainfile(filepath=params["working"], relative_remap=True)
if "FINISHED" not in saved or bpy.data.filepath != params["working"]:
    raise RuntimeError("Blender did not create the fresh fixture working copy")
result = {"path": bpy.data.filepath, "blender_version": bpy.app.version_string,
          "objects": sorted(obj.name for obj in bpy.context.scene.objects)}
''', {"fixture": descriptor["path"], "working": str(working)})
            self.save()
        finally:
            bridge.close()

    async def call(self, name, arguments=None, *, expected_error=False):
        if self.client is None:
            raise RuntimeError("No active MCP session")
        started = time.perf_counter()
        entry = {"fixture": self.kind, "tool": "production." + name,
                 "arguments": arguments or {}, "expected_error": expected_error}
        self.report["calls"].append(entry)
        self.save()
        try:
            response = await asyncio.wait_for(self.client.call_tool(entry["tool"], arguments or {}), timeout=210)
        except Exception as exc:
            entry.update(transport_error=str(exc), elapsed_ms=round((time.perf_counter()-started)*1000, 3))
            self.save()
            raise RuntimeError("MCP call failed or timed out; Blender state is unverified; do not retry automatically: " + name) from exc
        content = response.structuredContent
        envelope = content.get("result") if isinstance(content, dict) else None
        if not isinstance(envelope, dict):
            entry["unstructured_error"] = str(response.content)
            self.save()
            raise AssertionError("Expected structured MCP output: " + name)
        entry.update(response=envelope, elapsed_ms=round((time.perf_counter()-started)*1000, 3))
        self.save()
        failed = response.isError or envelope.get("status") == "error"
        if expected_error:
            self.check("explicit unsupported request rejected: " + name, failed)
            return envelope
        if failed:
            raise AssertionError(json.dumps(envelope, indent=2))
        if not envelope.get("operation_id") or not isinstance(envelope.get("data"), dict):
            raise AssertionError("Incomplete production envelope: " + name)
        print(name + " " + envelope["status"], flush=True)
        return envelope["data"]

    async def inventory(self):
        listed = await self.client.list_tools()
        names = {tool.name for tool in listed.tools}
        # M3.1 keeps the 54 historical tools and adds 11 (operation status, assets, cleanup, mesh safety).
        self.check("all 65 tools registered over MCP stdio", len(names) == 65 and len(listed.tools) == 65)
        self.check("all 25 M3 tools present", {"production."+name for name in NEW_TOOLS} <= names)
        for tool in listed.tools:
            if not isinstance(tool.inputSchema, dict) or tool.inputSchema.get("type") != "object":
                raise AssertionError("Invalid input schema: " + tool.name)
            if not isinstance(tool.outputSchema, dict) or tool.outputSchema.get("type") != "object":
                raise AssertionError("Missing structured output schema: " + tool.name)
        self.check("54 input and output schemas expose object contracts", True)
        self.report["sessions"][self.kind] = {"inventory": sorted(names),
            "schemas": {tool.name: {"input": tool.inputSchema, "output": tool.outputSchema} for tool in listed.tools}}
        self.save()

    async def action_fingerprint(self, obj, action):
        result = await self.call("animation_compare_actions", {"object_name": obj,
            "before_action": action, "after_action": action, "frame_start": 1, "frame_end": 1})
        self.check("Action identity comparison returns identical content: " + action, result["identical_content"])
        return result["before_fingerprint"]

    async def assign(self, obj, action):
        result = await self.call("animation_restore_action", {"object_name": obj, "action_name": action, "dry_run": False})
        self.check("existing Action assigned without content edits: " + action, result["source_intact"] and result["action"] == action)

    async def cycle(self, tool, obj, options, *, apply=True):
        proposal = await self.call(tool, {"object_name": obj, "mode": "propose", "options": options})
        self.check(tool + " reviewed proposal supported", proposal["proposal"]["supported"])
        mode = "apply" if apply else "preview"
        changed = await self.call(tool, {"mode": mode, "operation": proposal["operation"]})
        self.check(tool + " " + mode + " passes numerical safeguards", changed["numeric_verification"] == "pass" and changed["source_intact"])
        verified = await self.call(tool, {"mode": "verify", "operation": proposal["operation"]})
        self.check(tool + " repeat verification passes", verified["numeric_verification"] == "pass" and verified["source_intact"])
        if apply:
            reverted = await self.call(tool, {"mode": "revert", "operation": proposal["operation"]})
            self.check(tool + " reverts assignment and retains candidate", reverted["reverted"] and reverted["candidate_preserved"] == changed["working_action"])
        return changed

    async def rig(self, metadata):
        mixed = metadata["scenarios"]["mixed"]
        obj, action = mixed["object"], mixed["action"]
        for tool in ("rig_classify", "rig_map_semantics", "rig_analyze_controls", "rig_analyze_limbs", "rig_compatibility_report"):
            result = await self.call(tool, {"object_name": obj})
            if tool == "rig_classify":
                self.check("mixed fixture recognized as mixed IK/FK", result["classification"] == "mixed_ik_fk")
        await self.call("animation_analyze_control_mode", {"object_name": obj})
        results = {}
        for direction, side in (("fk_to_ik", "R"), ("ik_to_fk", "L")):
            for mode in ("match", "bake"):
                tool = "rig_" + mode + "_" + direction
                names = mixed["names"]
                settings = {"chain": [names[part+"."+side] for part in ("UpperLeg", "LowerLeg", "Foot")],
                    "ik_target": names["ContactControl."+side], "pole_target": names["PoleControl."+side],
                    "confirm_mapping": True, "frame": 5, "frame_start": 1, "frame_end": 9, "step": 1}
                results[tool] = await self.cycle(tool, obj, settings, apply=mode == "bake" and direction == "fk_to_ik")
        unknown = metadata["scenarios"]["unknown"]["object"]
        unsupported = await self.call("rig_compatibility_report", {"object_name": unknown})
        self.check("unknown rig is explicitly unsupported", unsupported["classification"] == "unknown"
                   and unsupported["capabilities"]["ik_fk_matching"] == "unsupported_or_ambiguous")
        rejected = await self.call("rig_bake_fk_to_ik", {"object_name": unknown, "mode": "propose", "options": {"confirm_mapping": False}})
        self.check("unknown rig gets no mutable proposal", not rejected["proposal"]["supported"])
        await self.call("rig_bake_fk_to_ik", {"mode": "apply", "operation": rejected["operation"]}, expected_error=True)
        return {"rig_operations": results}

    async def contacts(self, metadata):
        obj, action = metadata["rig"], metadata["action"]
        options = metadata["options"]
        foot = options["feet"][0]
        surface = await self.call("animation_detect_contact_surface", {"object_name": obj,
            "options": {"bone": foot["bone"], "surfaces": options["surfaces"]}})
        self.check("actual slope surface returned", surface["contact"] is not None and surface["contact"]["surface_object"] in options["surfaces"])
        phases = await self.call("animation_analyze_foot_phase", {"object_name": obj, "options": options})
        self.check("heel/flat/rise/swing phases present", {"HEEL_STRIKE", "FOOT_FLAT", "HEEL_RISE", "SWING"} <= {p["phase"] for p in phases["phases"]})
        await self.call("animation_analyze_contacts", {"object_name": obj, "options": options})
        no_confirm = await self.call("animation_fix_foot_sliding", {"object_name": obj, "mode": "propose", "options": options})
        self.check("unconfirmed contact operation is refused", not no_confirm["proposal"]["supported"])
        await self.call("animation_fix_foot_sliding", {"mode": "apply", "operation": no_confirm["operation"]}, expected_error=True)
        corrected = await self.cycle("animation_fix_foot_sliding", obj, dict(options, confirm_contacts=True))
        self.check("advanced contact drift improves using source labels", corrected["metrics"]["sliding_after_m"] < corrected["metrics"]["sliding_before_m"]*.4)
        limb_options = {"chains": [foot["chain"]], "frame_start": options["frame_start"], "frame_end": options["frame_end"]}
        await self.call("animation_detect_limb_pops", {"object_name": obj, "options": limb_options})
        await self.call("animation_detect_limb_stretch", {"object_name": obj, "options": limb_options})
        visual = None
        if self.render_contact:
            visual = await self.call("qa_compare_before_after", {"object_name": obj, "before_action": action,
                "after_action": corrected["working_action"], "options": {"frame_start": 10, "frame_end": 10,
                "max_frames": 1, "width": 256, "height": 192, "views": ["FOOT"], "framing_bones": [foot["bone"]]}})
            self.check("two bounded before/after contact images are comparable", visual["comparable"] and visual["state_restored"] and len(visual["files"]) == 2)
        await self.assign(obj, metadata["actions"]["handle_overshoot"])
        bezier = await self.call("animation_analyze_bezier", {"object_name": obj})
        self.check("actual Bézier handle defect identified", bezier["metrics"]["overshoot_findings"] > 0)
        repaired = await self.cycle("animation_repair_bezier", obj,
            {"channels": [{"path": "pose.bones["+json.dumps(foot["control"])+"].location", "index": 0}],
             "max_deviation": .02, "max_acceleration_increase": .1}, apply=False)
        await self.assign(obj, action)
        await self.assign("M3_HandRig", metadata["actions"]["hand_contact"])
        hand = await self.call("animation_analyze_effector_contact", {"object_name": "M3_HandRig", "options": {"bone": "effector", "surfaces": ["M3_Wall"]}})
        self.check("stationary hand dwell contact identified", len(hand["contacts"]) == 1)
        await self.assign("M3_HandRig", metadata["actions"]["hand_near_moving"])
        near = await self.call("animation_analyze_effector_contact", {"object_name": "M3_HandRig", "options": {"bone": "effector", "surfaces": ["M3_Wall"]}})
        self.check("moving hand near a wall remains a negative contact", not near["contacts"])
        return {"contact_correction": corrected, "bezier_repair": repaired, "visual": visual}

    async def mocap(self, metadata):
        source, target = metadata["source"], metadata["target"]
        analysis = await self.call("mocap_analyze", {"object_name": source})
        self.check("mocap analysis reads controlled skeleton", analysis["bone_count"] == metadata["source_bones"])
        automatic = await self.call("mocap_map_skeleton", {"source_armature": source, "target_armature": target})
        self.check("inferred skeleton map is not automatically approved", not automatic["validated"])
        mapping = await self.call("mocap_map_skeleton", {"source_armature": source, "target_armature": target,
            "mapping": metadata["mapping"], "mapping_approved": True})
        self.check("explicit controlled mapping validates", mapping["validated"])
        settings = {"source_armature": source, "mapping": metadata["mapping"], "mapping_approved": True,
                    "frame_start": 1, "frame_end": 21, "ground_height_m": .12, "contact_height_m": .03}
        proposal = await self.call("mocap_preview_retarget", {"object_name": target, "options": settings})
        self.check("rest-pose compensated retarget proposal supported", proposal["proposal"]["supported"])
        preview = await self.call("mocap_preview_retarget", {"operation": proposal["operation"]})
        self.check("retarget preview verified and target original retained", preview["numeric_verification"] == "pass" and preview["active_action"] == metadata["target_action"])
        applied = await self.call("mocap_retarget", {"mode": "apply", "operation": proposal["operation"]})
        self.check("retarget applies a verified new Action", applied["numeric_verification"] == "pass" and applied["source_intact"]
                   and applied["working_action"] != metadata["target_action"])
        contact = await self.call("animation_analyze_contacts", {"object_name": target,
            "options": {"feet": metadata["feet"], "surfaces": metadata["surfaces"], "frame_start": 1, "frame_end": 21}})
        limbs = await self.call("animation_detect_limb_pops", {"object_name": target,
            "options": {"chains": [f["chain"] for f in metadata["feet"]], "frame_start": 1, "frame_end": 21}})
        await self.call("animation_detect_limb_stretch", {"object_name": target,
            "options": {"chains": [f["chain"] for f in metadata["feet"]], "frame_start": 1, "frame_end": 21}})
        root = next(row["target_bone"] for row in metadata["mapping"] if row["semantic_role"] == "root")
        await self.call("animation_analyze_root_motion", {"object_name": target, "options": {"root_bone": root, "frame_start": 1, "frame_end": 21}})
        deformation = await self.call("animation_deformation_qa", {"object_name": target, "action_name": applied["working_action"],
            "options": {"frames": [1, 11, 21], "max_samples": 3, "contact_frames": [1, 21]}})
        self.check("retarget deformation QA restores state", deformation["state_restored"] and not deformation["errors"])
        verification = await self.call("mocap_retarget", {"mode": "verify", "operation": proposal["operation"]})
        self.check("retarget independently verifies through MCP", verification["numeric_verification"] == "pass" and verification["source_intact"])
        reverted = await self.call("mocap_retarget", {"mode": "revert", "operation": proposal["operation"]})
        self.check("retarget reverts and retains candidate", reverted["reverted"] and reverted["candidate_preserved"] == applied["working_action"])
        low = [dict(row) for row in metadata["mapping"]]
        low[0]["confidence"] = .5
        rejected = await self.call("mocap_retarget", {"object_name": target, "mode": "propose", "options": dict(settings, mapping=low)})
        self.check("low-confidence map refuses retarget proposal", not rejected["proposal"]["supported"])
        await self.call("mocap_retarget", {"mode": "apply", "operation": rejected["operation"]}, expected_error=True)
        cleanup_analysis = await self.call("mocap_cleanup", {"object_name": source})
        self.check("mocap cleanup defaults to no apply", cleanup_analysis["apply"] is False)
        await self.assign(source, metadata["noisy_action"])
        source_root = next(row["source_bone"] for row in metadata["mapping"] if row["semantic_role"] == "root")
        cleanup_proposal = await self.call("mocap_cleanup", {"object_name": source, "mode": "propose", "strategy": "curves",
            "options": {"strategies": ["mocap_smoothing"], "allow_smoothing": True, "tolerance": .0005,
                        "data_paths": ["pose.bones["+json.dumps(source_root)+"].location"], "array_indices": [0]}})
        cleaned = await self.call("mocap_cleanup", {"mode": "preview", "operation": cleanup_proposal["operation"]})
        self.check("mocap smoothing preview is measured and bounded", cleaned["numeric_verification"] == "pass" and cleaned["metrics"]["max_error"] <= .0005)
        await self.assign(source, metadata["source_action"])
        return {"retarget": applied, "cleanup": cleaned, "contacts": contact, "limbs": limbs, "deformation": deformation}

    async def deformation(self, metadata):
        row = metadata["objects"]["shoulder"]
        obj = row["rig"]
        analysis = await self.call("deformation_analyze", {"object_name": obj,
            "options": {"reference_frame": 1, "frame": 47, "regions": {"shoulder": ["JointRegion"]}}})
        self.check("weighted shoulder collapse detected over MCP", analysis["metrics"]["issue_count"] > 0 and analysis["state_restored"])
        compare = await self.call("deformation_compare", {"object_name": obj, "before_action": row["good_action"], "after_action": row["bad_action"],
            "options": {"reference_frame": 1, "frames": [1, 47]}})
        self.check("deformation comparison distinguishes valid and collapsed pose", compare["before"]["issue_count"] == 0 and compare["after"]["collapsed_edges"] > 0)
        sampled = await self.call("animation_deformation_qa", {"object_name": obj,
            "options": {"reference_frame": 1, "contact_frames": [12], "max_samples": 8}})
        self.check("animation deformation sampling catches extreme frame", 47 in [sample["frame"] for sample in sampled["samples"]])
        poses = [{"region": region, "bone": row["bone"], "axis": "Y", "angle_degrees": 35}
                 for region in ("shoulder", "elbow", "wrist", "hip", "knee", "ankle")]
        plan = await self.call("deformation_test_pose", {"object_name": obj, "poses": poses})
        self.check("stress default proposes without changing pose", plan["dry_run"] and plan["checkpoint"] is None)
        stress = await self.call("deformation_test_pose", {"object_name": obj, "poses": poses, "dry_run": False})
        self.check("six temporary stress poses checkpoint and restore", stress["state_restored"] and len(stress["samples"]) == 6 and bool(stress["checkpoint"]))
        return {"analysis": analysis, "comparison": compare, "sampling": sampled, "stress": stress}

    def originals(self, descriptor):
        data = descriptor["metadata"]
        if self.kind == "rig":
            row = data["scenarios"]["mixed"]
            return [(row["object"], row["action"])]
        if self.kind == "contact":
            return [(data["rig"], data["action"]), (data["rig"], data["actions"]["handle_overshoot"]),
                    ("M3_HandRig", data["actions"]["hand_contact"]), ("M3_HandRig", data["actions"]["hand_near_moving"])]
        if self.kind == "mocap":
            return [(data["source"], data["source_action"]), (data["source"], data["noisy_action"]), (data["target"], data["target_action"])]
        return [(row["rig"], row["bad_action"]) for row in data["objects"].values()]

    async def execute(self):
        started = time.perf_counter()
        try:
            for descriptor in self.report["fixtures"]:
                self.kind = descriptor["kind"]
                self.report["fixture_results"][self.kind] = {}
                self.save()
                self.load(descriptor)
                with (self.folder / (self.kind+"-mcp.log")).open("w", encoding="utf-8") as log:
                    async with mcp_session(self.storage.root, log) as client:
                        self.client = client
                        try:
                            await self.inventory()
                            # M3.1 (AUD-014): mutation needs an explicit role for the generated working copy.
                            registered = await self.call("asset_register_fixture", {"path": self.report["fixture_results"][self.kind]["working_copy"]["path"]})
                            self.check("generated working copy registered as FIXTURE", registered["asset"]["role"] == "FIXTURE")
                            await self.call("scene_audit")
                            baseline = await self.call("create_checkpoint", {"description": "M3 integration baseline: "+self.kind, "operation": "M3 stdio demo"})
                            self.report["fixture_results"][self.kind]["baseline_checkpoint"] = baseline
                            fingerprints = {}
                            for obj, action in self.originals(descriptor):
                                fingerprints[action] = await self.action_fingerprint(obj, action)
                            handler = {"rig": self.rig, "contact": self.contacts, "mocap": self.mocap, "deformation": self.deformation}[self.kind]
                            results = await handler(descriptor["metadata"])
                            self.report["fixture_results"][self.kind].update(results)
                            for obj, action in self.originals(descriptor):
                                after = await self.action_fingerprint(obj, action)
                                self.check("original Action content preserved: "+action, after == fingerprints[action])
                            final = await self.call("create_checkpoint", {"description": "M3 integration candidates/evidence: "+self.kind, "operation": "M3 stdio final state"})
                            restored = await self.call("restore_checkpoint", {"checkpoint": baseline["manifest"], "dry_run": False})
                            delta = await self.call("diff_scene", {"checkpoint": baseline["manifest"]})
                            self.check("fixture checkpoint restore recovers original object metadata", delta["counts"] == {"added": 0, "removed": 0, "changed": 0})
                            for obj, action in self.originals(descriptor):
                                after = await self.action_fingerprint(obj, action)
                                self.check("restored checkpoint recovers original Action: "+action, after == fingerprints[action])
                            self.report["fixture_results"][self.kind].update(final_checkpoint=final, restoration=restored,
                                original_action_fingerprints=fingerprints, restore_diff=delta)
                            self.save()
                        finally:
                            self.client = None
                self.check("generated fixture file remains byte-exact: "+self.kind, sha256(Path(descriptor["path"])) == descriptor["sha256"])
            invoked = {entry["tool"] for entry in self.report["calls"] if entry.get("response")}
            successful = {entry["tool"] for entry in self.report["calls"] if entry.get("response", {}).get("status") != "error" and entry.get("response")}
            expected = {"production."+name for name in NEW_TOOLS}
            self.check("all 25 new M3 tools invoked successfully via MCP", expected <= successful)
            self.report.update(status="pass", elapsed_seconds=round(time.perf_counter()-started, 3),
                total_calls=len(self.report["calls"]), distinct_tools_invoked=len(invoked), new_tools_invoked=len(expected & successful),
                existing_tools_invoked=sorted(invoked-expected), invoked_tools=sorted(invoked),
                expected_negative_calls=sum(call["expected_error"] for call in self.report["calls"]),
                check_count=len(self.report["checks"]))
            self.save()
            print("M3_DEMO_REPORT="+str(self.folder/"report.json"), flush=True)
        except Exception as exc:
            self.report.update(status="fail", error=str(exc), elapsed_seconds=round(time.perf_counter()-started, 3),
                state_after_failure="Inspect the last operation/checkpoint; no automatic retry or recovery was attempted")
            self.save()
            print("M3_DEMO_FAILED_REPORT="+str(self.folder/"report.json"), flush=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for kind in ("rig", "contact", "mocap", "deformation"):
        parser.add_argument("--"+kind+"-fixture", type=Path, required=True)
    parser.add_argument("--skip-contact-render", action="store_true",
                        help="Skip only the two optional contact-closeup PNGs; real headless demos remain separate evidence")
    args = parser.parse_args()
    descriptors = [fixture_descriptor(kind, getattr(args, kind+"_fixture")) for kind in ("rig", "contact", "mocap", "deformation")]
    asyncio.run(Demo(descriptors, not args.skip_contact_render).execute())


if __name__ == "__main__":
    main()
