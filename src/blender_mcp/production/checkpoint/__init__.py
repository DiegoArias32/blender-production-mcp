"""Copy-only checkpoints, metadata diffs and guarded restore to a working copy."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4
from pathlib import Path

from ..common.storage import Storage, sha256, write_json
from ..inspect import snapshot
from ..common.asset_safety import register_restored_working_copy

SAVE_COPY = '''
import bpy
original = bpy.data.filepath
outcome = bpy.ops.wm.save_as_mainfile(filepath=params["path"], copy=True, relative_remap=True)
if "FINISHED" not in outcome:
    raise RuntimeError("Blender did not finish saving checkpoint")
if bpy.data.filepath != original:
    raise RuntimeError("Copy operation unexpectedly changed the active file")
result = {"source_path": original, "path": params["path"], "copy": True}
'''

RESTORE_COPY = '''
import bpy
outcome = bpy.ops.wm.open_mainfile(filepath=params["checkpoint_path"], load_ui=False, use_scripts=False)
if "FINISHED" not in outcome:
    raise RuntimeError("Blender did not finish opening checkpoint")
# The restored copy is a registered WORKING_COPY: its stamp ties the file to the asset registry.
for scene in bpy.data.scenes:
    scene["production_asset"] = params["stamp"]
# Let Blender remap relative external paths while saving to the new working directory.
# Byte-copying the .blend to another directory can break relative dependencies.
outcome = bpy.ops.wm.save_as_mainfile(filepath=params["path"], relative_remap=True)
if "FINISHED" not in outcome or bpy.data.filepath != params["path"]:
    raise RuntimeError("Blender did not finish creating restored working copy")
result = {"working_path": bpy.data.filepath, "scripts_enabled": False}
'''


def summary(snapshot_data: dict) -> dict:
    return {key: snapshot_data[key] for key in
            ("schema_version", "blender_version", "source_file", "scene", "totals", "snapshot_scope")
            if key in snapshot_data}


def create_checkpoint(bridge, storage: Storage, description: str, operation_id: str,
                      operation: str | None = None) -> dict:
    if not description.strip():
        raise ValueError("Checkpoint description is required")
    before = snapshot(bridge)
    folder = storage.reserve("checkpoints", operation_id)
    path = folder / "scene.blend"
    # This intent remains even on a failed save; no silent success or retry.
    intent = {"operation_id": operation_id, "operation": operation,
              "description": description, "created_at": datetime.now(timezone.utc).isoformat(),
              "source": before, "filename": "scene.blend"}
    write_json(folder / "intent.json", intent)
    bridge.run(SAVE_COPY, {"path": str(path)})
    if not path.is_file() or path.stat().st_size < 12:
        raise RuntimeError("Checkpoint file is missing or empty")
    manifest = {**intent, "sha256": sha256(path), "bytes": path.stat().st_size,
                "external_assets": "Referenced external files are not copied or packed."}
    write_json(folder / "manifest.json", manifest)
    return {"path": str(path), "manifest": str(folder / "manifest.json"),
            "sha256": manifest["sha256"], "description": description,
            "source": summary(before), "operation": operation,
            "warnings": ["External linked assets are referenced, not packed or copied."],
            "affected": {"scene": before.get("scene", {}).get("name")}}


def metadata_diff(before: dict, after: dict) -> dict:
    """Compare metadata, not vertex buffers, keyframe values or material node graphs."""
    left, right = before.get("objects", {}), after.get("objects", {})
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise ValueError("Snapshot objects must be keyed by object name")
    changed = {k: {"before": left[k], "after": right[k]}
               for k in left.keys() & right.keys() if left[k] != right[k]}
    other = {k: {"before": before.get(k), "after": after.get(k)}
             for k in (before.keys() | after.keys()) - {"objects"}
             if before.get(k) != after.get(k)}
    return {"added": sorted(right.keys()-left.keys()), "removed": sorted(left.keys()-right.keys()),
            "changed": changed, "scene_changes": other,
            "scope": "metadata; renames appear as removal/addition; geometry and animation values not compared"}


def diff_scene(bridge, storage: Storage, checkpoint: str, other_checkpoint: str | None = None,
               limit: int = 100, offset: int = 0) -> dict:
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("limit must be 1..500, offset >= 0")
    _, first = storage.checkpoint_manifest(checkpoint)
    after = storage.checkpoint_manifest(other_checkpoint)[1]["source"] if other_checkpoint else snapshot(bridge)
    result = metadata_diff(first["source"], after)
    all_changed = result["changed"]
    result["counts"] = {k: len(result[k]) for k in ("added", "removed", "changed")}
    for key in ("added", "removed"):
        result[key] = result[key][offset:offset+limit]
    result["changed"] = {k: all_changed[k] for k in sorted(all_changed)[offset:offset+limit]}
    def compact(value, depth=0):
        if depth >= 5:
            return {"summary": "nested data omitted; inspect checkpoint manifest"}
        if isinstance(value, list):
            return {"total": len(value), "items": [compact(v, depth+1) for v in value[offset:offset+limit]],
                    "offset": offset, "limit": limit}
        if isinstance(value, dict):
            items = list(value.items())
            return {k: compact(v, depth+1) for k,v in items[:limit]}
        return value
    result["scene_changes"] = compact(result["scene_changes"])
    result.update({"offset": offset, "limit": limit})
    return result


def restore_checkpoint(bridge, storage: Storage, checkpoint: str, operation_id: str,
                       dry_run: bool = True) -> dict:
    blend, manifest = storage.checkpoint_manifest(checkpoint)
    if dry_run:
        return {"dry_run": True, "checkpoint": checkpoint,
                "plan": ["Save recovery checkpoint", "Open requested checkpoint with scripts disabled",
                         "Save new working file with relative path remapping; originals remain unchanged"],
                "target_scene": summary(manifest["source"])}
    safety = create_checkpoint(bridge, storage, "Before checkpoint restore", operation_id, "restore_checkpoint")
    try:
        folder = storage.reserve("working", operation_id)
        working = folder / "scene.blend"
        # A unique directory prevents overwriting any master, checkpoint or previous working file.
        asset_id = uuid4().hex
        stamp = json.dumps({"asset_id": asset_id, "role": "WORKING_COPY"})
        result = bridge.run(RESTORE_COPY, {"path": str(working), "checkpoint_path": str(blend), "stamp": stamp})
        after = summary(snapshot(bridge))
        result["asset"] = register_restored_working_copy(storage, str(working), asset_id, checkpoint, operation_id)
    except Exception as exc:
        raise RuntimeError(f"Restore failed; recovery manifest: {safety['manifest']}; {exc}") from exc
    return {**result, "checkpoint": safety, "restored_from": checkpoint,
            "before": safety["source"], "after": after}
