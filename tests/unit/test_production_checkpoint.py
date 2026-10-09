"""Checkpoint provenance, failure recovery and confinement without a live Blender."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import threading

import pytest

from blender_mcp.production import checkpoint
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.storage import Storage, sha256, write_json
from blender_mcp.production.inspect import SCENE_SCRIPT


class CheckpointBridge:
    """Minimal disk-producing Blender double; never starts or contacts Blender."""

    def __init__(self, source=None):
        self.scene = {"source_file": str(source) if source else None,
                      "scene": {"frame": 1}, "objects": {"Cube": {"vertices": 8}}}
        self.calls = []
        self.fail_save = False
        self.fail_restore = False
        self.fail_snapshot_after_restore = False
        self.restored = False
        self.omit_save = False
        self.lock = threading.RLock()

    def run(self, body, params):
        self.calls.append((body, params))
        if body == SCENE_SCRIPT:
            if self.restored and self.fail_snapshot_after_restore:
                raise ConnectionError("synthetic post-restore snapshot failure")
            return deepcopy(self.scene)
        if body == checkpoint.SAVE_COPY:
            if self.fail_save:
                raise RuntimeError("synthetic disk/save failure")
            if not self.omit_save:
                # Creation mode deliberately refuses clobbering any fixture.
                with Path(params["path"]).open("xb") as output:
                    output.write(b"BLENDER-fixture\n" + json.dumps(self.scene).encode())
            return {"source_path": self.scene["source_file"], "path": params["path"], "copy": True}
        if body == checkpoint.RESTORE_COPY:
            if self.fail_restore:
                raise RuntimeError("synthetic open failure")
            payload = Path(params["checkpoint_path"]).read_bytes().split(b"\n", 1)[1]
            self.scene = json.loads(payload)
            self.scene["source_file"] = params["path"]
            with Path(params["path"]).open("xb") as output:
                output.write(b"BLENDER-fixture\n" + json.dumps(self.scene).encode())
            self.restored = True
            return {"working_path": params["path"], "scripts_enabled": False}
        raise AssertionError("Unexpected Blender code")


@pytest.fixture
def store_and_bridge(tmp_path):
    master = tmp_path / "MASTER.blend"
    master.write_bytes(b"original production source fixture")
    return Storage(tmp_path / "artifacts"), CheckpointBridge(master), master


def test_checkpoint_keeps_source_and_records_provenance_hash(store_and_bridge):
    store, bridge, master = store_and_bridge
    original = master.read_bytes()
    result = checkpoint.create_checkpoint(bridge, store, "Before rig edit", "op-1", "rig_edit")
    saved, manifest = store.checkpoint_manifest(result["manifest"])
    assert saved != master and master.read_bytes() == original
    assert bridge.scene["source_file"] == str(master)
    assert manifest["operation_id"] == "op-1"
    assert manifest["operation"] == "rig_edit"
    assert manifest["description"] == "Before rig edit"
    assert manifest["created_at"].endswith("+00:00")
    assert manifest["source"] == bridge.scene
    assert manifest["sha256"] == sha256(saved)
    assert "not copied" in manifest["external_assets"]


def test_same_operation_creates_distinct_immutable_artifacts(store_and_bridge):
    store, bridge, _ = store_and_bridge
    first = checkpoint.create_checkpoint(bridge, store, "Repeated operation", "op-1")
    contents = Path(first["path"]).read_bytes()
    bridge.scene["objects"]["Cube"]["vertices"] = 12
    second = checkpoint.create_checkpoint(bridge, store, "Repeated operation", "op-1")
    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == contents


@pytest.mark.parametrize("failure_mode", ["fail_save", "omit_save"])
def test_failed_save_leaves_intent_but_never_success_manifest(store_and_bridge, failure_mode):
    store, bridge, master = store_and_bridge
    setattr(bridge, failure_mode, True)
    with pytest.raises(RuntimeError):
        checkpoint.create_checkpoint(bridge, store, "Save must fail", "op-fail")
    assert len(list(store.root.glob("checkpoints/*/intent.json"))) == 1
    assert not list(store.root.glob("checkpoints/*/manifest.json"))
    assert master.read_bytes() == b"original production source fixture"


def test_empty_description_fails_without_blender_call(store_and_bridge):
    store, bridge, _ = store_and_bridge
    with pytest.raises(ValueError):
        checkpoint.create_checkpoint(bridge, store, "  ", "op-invalid")
    assert not bridge.calls


def test_restore_dry_run_only_validates_checkpoint(store_and_bridge):
    store, bridge, _ = store_and_bridge
    saved = checkpoint.create_checkpoint(bridge, store, "Original", "op-first")
    bridge.calls.clear()
    before = sorted(store.root.rglob("*"))
    result = checkpoint.restore_checkpoint(bridge, store, saved["manifest"], "op-plan")
    assert result["dry_run"] is True
    assert bridge.calls == []
    assert sorted(store.root.rglob("*")) == before


def test_corrupt_checkpoint_cannot_trigger_recovery_or_restore(store_and_bridge):
    store, bridge, _ = store_and_bridge
    saved = checkpoint.create_checkpoint(bridge, store, "Original", "op-first")
    Path(saved["path"]).write_bytes(b"altered")
    bridge.calls.clear()
    with pytest.raises(ValueError, match="integrity"):
        checkpoint.restore_checkpoint(bridge, store, saved["manifest"], "op-restore", False)
    assert bridge.calls == []


def test_manifest_cannot_escape_store_using_traversal(store_and_bridge, tmp_path):
    store, _, _ = store_and_bridge
    outside = tmp_path / "untrusted.blend"
    outside.write_bytes(b"BLENDER-untrusted")
    folder = store.reserve("checkpoints", "malformed")
    path = folder / "manifest.json"
    write_json(path, {"filename": "../../../untrusted.blend", "sha256": sha256(outside)})
    with pytest.raises(ValueError, match="Invalid checkpoint file path"):
        store.checkpoint_manifest(str(path))


def test_restore_saves_recovery_before_opening_unique_copy(store_and_bridge):
    store, bridge, master = store_and_bridge
    saved = checkpoint.create_checkpoint(bridge, store, "Original", "op-first")
    old_bytes = Path(saved["path"]).read_bytes()
    bridge.scene["objects"]["Cube"]["vertices"] = 100
    bridge.calls.clear()
    result = checkpoint.restore_checkpoint(bridge, store, saved["manifest"], "op-restore", False)
    scripts = [body for body, _ in bridge.calls]
    assert scripts.index(checkpoint.SAVE_COPY) < scripts.index(checkpoint.RESTORE_COPY)
    _, recovery_manifest = store.checkpoint_manifest(result["checkpoint"]["manifest"])
    assert recovery_manifest["source"]["objects"]["Cube"]["vertices"] == 100
    assert bridge.scene["objects"]["Cube"]["vertices"] == 8
    assert result["working_path"] not in {str(master), saved["path"]}
    assert result["scripts_enabled"] is False
    assert master.read_bytes() == b"original production source fixture"
    assert Path(saved["path"]).read_bytes() == old_bytes


def test_restore_failure_exposes_existing_recovery_manifest(store_and_bridge):
    store, bridge, _ = store_and_bridge
    saved = checkpoint.create_checkpoint(bridge, store, "Original", "op-first")
    bridge.fail_restore = True
    with pytest.raises(RuntimeError, match="Restore failed; recovery manifest:") as error:
        checkpoint.restore_checkpoint(bridge, store, saved["manifest"], "op-restore", False)
    manifests = list(store.root.glob("checkpoints/*/manifest.json"))
    assert len(manifests) == 2
    recovery = next(path for path in manifests if str(path) != saved["manifest"])
    assert str(recovery) in str(error.value)
    store.checkpoint_manifest(str(recovery))


def test_post_restore_snapshot_failure_keeps_recovery_context(store_and_bridge):
    store, bridge, _ = store_and_bridge
    saved = checkpoint.create_checkpoint(bridge, store, "Original", "op-first")
    bridge.fail_snapshot_after_restore = True
    with pytest.raises(RuntimeError, match="Restore failed; recovery manifest:") as error:
        checkpoint.restore_checkpoint(bridge, store, saved["manifest"], "op-restore", False)
    assert "post-restore snapshot failure" in str(error.value)
    recovery = next(path for path in store.root.glob("checkpoints/*/manifest.json")
                    if str(path) != saved["manifest"])
    assert str(recovery) in str(error.value)
    store.checkpoint_manifest(str(recovery))
    assert bridge.restored and Path(bridge.scene["source_file"]).is_file()
    assert sum(body == checkpoint.RESTORE_COPY for body, _ in bridge.calls) == 1


def test_metadata_diff_reports_add_remove_change_and_scope():
    before = {"objects": {"Gone": {}, "Changed": {"scale": 1}, "Same": {}}, "scene": {"fps": 24}}
    after = {"objects": {"New": {}, "Changed": {"scale": 2}, "Same": {}}, "scene": {"fps": 30}}
    result = checkpoint.metadata_diff(before, after)
    assert result["added"] == ["New"]
    assert result["removed"] == ["Gone"]
    assert list(result["changed"]) == ["Changed"]
    assert result["scene_changes"]["scene"]["after"] == {"fps": 30}
    assert "animation values not compared" in result["scope"]


def test_diff_pages_changed_objects_but_retains_totals(store_and_bridge):
    store, bridge, _ = store_and_bridge
    bridge.scene["objects"] = {name: {"value": 0} for name in ["A", "B", "C"]}
    saved = checkpoint.create_checkpoint(bridge, store, "Original", "op-first")
    bridge.scene["objects"] = {name: {"value": 1} for name in ["A", "B", "C"]}
    result = checkpoint.diff_scene(bridge, store, saved["manifest"], limit=1, offset=1)
    assert list(result["changed"]) == ["B"]
    assert result["counts"]["changed"] == 3


@pytest.mark.parametrize("script", [checkpoint.SAVE_COPY, checkpoint.RESTORE_COPY])
def test_checkpoint_payloads_always_clear_safe_mode(script):
    build_code(script, {"path": r"C:\temporary folder\quote' and Unicode é.blend"})


def test_registered_mcp_schemas_hide_internal_operation_ids():
    from blender_mcp.production.server import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {"production.create_checkpoint", "production.restore_checkpoint", "production.scene_audit"} <= names
    for tool in tools:
        assert "_operation_id" not in tool.inputSchema.get("properties", {})
    restore = next(tool for tool in tools if tool.name == "production.restore_checkpoint")
    assert restore.inputSchema["properties"]["dry_run"]["default"] is True


def test_mcp_annotations_distinguish_readers_from_artifact_writers_and_restore():
    from blender_mcp.production.server import mcp
    tools = asyncio.run(mcp.list_tools())
    readers = {
        "production.scene_audit", "production.inspect_armature", "production.rig_analyze",
        "production.animation_analyze", "production.animation_detect_foot_sliding",
        "production.animation_detect_quaternion_flips", "production.diff_scene", "production.operation_status",
        "production.asset_status", "production.mesh_region_verify", "production.mesh_candidate_verify",
        "production.animation_compare_actions", "production.animation_analyze_contacts",
        "production.animation_analyze_fcurves", "production.animation_analyze_root_motion",
        "production.animation_analyze_control_mode", "production.animation_analyze_balance",
        "production.animation_analyze_motion_quality", "production.animation_refinement_plan",
        "production.rig_classify", "production.rig_map_semantics", "production.rig_analyze_controls",
        "production.rig_analyze_limbs", "production.rig_compatibility_report",
        "production.animation_analyze_foot_phase", "production.animation_detect_contact_surface",
        "production.animation_analyze_effector_contact", "production.animation_detect_limb_pops",
        "production.animation_detect_limb_stretch", "production.animation_analyze_bezier",
        "production.mocap_analyze", "production.mocap_map_skeleton", "production.deformation_analyze",
        "production.deformation_compare", "production.animation_deformation_qa",
        "production.character_inspect", "production.character_validate_locked_regions",
        "production.character_analyze_silhouette", "production.character_analyze_proportions",
        "production.character_analyze_asymmetry", "production.character_analyze_surface",
        "production.character_analyze_intersections", "production.character_modeling_audit",
        "production.character_redesign_plan",
    }
    writers = {
        "production.create_checkpoint", "production.restore_checkpoint",
        "production.qa_capture_viewport", "production.qa_render_preview",
        "production.animation_create_action", "production.animation_duplicate_action",
        "production.animation_restore_action", "production.animation_fix_foot_sliding",
        "production.animation_fix_quaternion_flips", "production.animation_cleanup_fcurves",
        "production.animation_prepare_root_motion", "production.animation_retime",
        "production.qa_render_multiview", "production.qa_compare_before_after",
        "production.rig_match_fk_to_ik", "production.rig_match_ik_to_fk",
        "production.rig_bake_fk_to_ik", "production.rig_bake_ik_to_fk",
        "production.animation_repair_bezier", "production.mocap_preview_retarget",
        "production.mocap_retarget", "production.mocap_cleanup", "production.deformation_test_pose",
        "production.asset_register_master", "production.asset_register_fixture",
        "production.asset_create_working_copy", "production.cleanup_artifacts",
        "production.mesh_signature", "production.mesh_region_define", "production.mesh_candidate_create",
        "production.character_define_regions", "production.character_lock_regions",
        "production.character_create_candidate", "production.character_adjust_shape",
        "production.character_deformation_qa", "production.character_render_multiview",
        "production.character_compare_before_after", "production.character_capture_reference_views",
        "production.character_capture_candidate_views", "production.character_compare_reference",
        "production.character_redesign_session",
    }
    from test_m5_inventory import M5_READERS,M5_WRITERS
    readers|={'production.'+name for name in M5_READERS}
    writers|={'production.'+name for name in M5_WRITERS}
    from blender_mcp.production.animation.generation.tools import NAMES as M6_NAMES
    readers.add('production.animation_create_brief')
    writers|={'production.'+name for name in M6_NAMES if name!='animation_create_brief'}
    from blender_mcp.production.facial.tools import READ_ONLY as M7_READ_ONLY, TRANSIENT as M7_TRANSIENT, NAMES as M7_NAMES
    readers|={'production.'+name for name in M7_READ_ONLY|M7_TRANSIENT}
    writers|={'production.'+name for name in M7_NAMES if name not in M7_READ_ONLY|M7_TRANSIENT}
    from blender_mcp.production.physics.tools import READ_ONLY as M8_READ_ONLY, TRANSIENT as M8_TRANSIENT, NAMES as M8_NAMES
    readers|={'production.'+name for name in M8_READ_ONLY|M8_TRANSIENT}
    writers|={'production.'+name for name in M8_NAMES if name not in M8_READ_ONLY|M8_TRANSIENT}
    from blender_mcp.production.materials.tools import READ_ONLY as M9_READ_ONLY, NAMES as M9_NAMES
    assert len(M9_NAMES)==38 and len(M9_READ_ONLY)==21
    readers|={'production.'+name for name in M9_READ_ONLY}
    # Material renders persist hashed artifacts and accept operation_id, so they
    # remain writers even though their temporary scene is restored on completion.
    writers|={'production.'+name for name in M9_NAMES if name not in M9_READ_ONLY}
    assert {tool.name for tool in tools} == readers | writers
    from blender_mcp.production.server import TRANSIENT_READERS
    assert TRANSIENT_READERS <= readers
    for tool in tools:
        assert tool.annotations is not None
        # AUD-016: readers that change frame/Action/pose while running and restore them are not read-only.
        assert tool.annotations.readOnlyHint is (tool.name in readers - TRANSIENT_READERS)
        assert tool.annotations.idempotentHint is (tool.name in readers)
        assert tool.annotations.destructiveHint is (tool.name in {"production.restore_checkpoint", "production.cleanup_artifacts"})
        assert tool.annotations.openWorldHint is False


def test_tool_failure_has_structured_error_and_operation_log(store_and_bridge, monkeypatch):
    from blender_mcp.production import server
    store, bridge, _ = store_and_bridge
    bridge.fail_save = True
    monkeypatch.setattr(server, "resources", lambda: (bridge, store))
    result = server.checkpoint("Expected failure")
    assert result["status"] == "error"
    assert result["operation_id"] and result["metrics"]["elapsed_ms"] >= 0
    assert result["errors"][0]["type"] == "RuntimeError"
    event = json.loads((store.root / "operations.jsonl").read_text())
    assert event["operation_id"] == result["operation_id"]
    assert event["status"] == "error"
