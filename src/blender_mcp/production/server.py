"""Production-only MCP entry point. No external asset or paid-service tools."""
from __future__ import annotations

import os
os.environ["DISABLE_TELEMETRY"] = "true"
os.environ["BLENDER_MCP_SAFE_MODE"] = "1"

from functools import partial, wraps
import inspect as pyinspect
import json
import logging
import re
import time
from uuid import uuid4

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .common.bridge import Bridge
from .common.storage import Storage
from .common.lease import ControllerLease
from .common.ledger import OperationLedger, idempotency_block, outcome_state, replayed
from . import checkpoint as checkpoints
from .inspect import scene_audit, inspect_armature
from .rig import rig_analyze
from .animation import analyze, detect_foot_sliding, detect_quaternion_flips
from .qa import capture_viewport, render_backend, render_preview

_bridge = None
_storage = None
mcp = FastMCP("Blender Production MCP", instructions="Inspect first. Work on copies. Use checkpoints before structural changes. Analyze, propose, preview, apply only explicit corrections to duplicate Actions, verify numerically, render and visually review, then accept or revert. Candidate contacts require validation; numerical success is not artistic acceptance. Never load or modify a user's master without explicit authorization. Only one upstream OR production controller per Blender instance.")


_ERROR_CODE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+):")


def precondition_code(message) -> str | None:
    """Structured code of a production error ("POSE_POSITION_REQUIRED: ..."), even when the
    message travelled through the upstream socket wrapped in a JSON traceback string."""
    match = _ERROR_CODE.search(str(message))
    return match.group(1) if match else None


def error_record(exc: Exception) -> dict:
    record = {"type": type(exc).__name__, "message": str(exc)}
    code = precondition_code(record["message"])
    if code:
        record["code"] = code
    return record


def error_codes(errors) -> list:
    codes = []
    for item in errors or []:
        code = item.get("code") if isinstance(item, dict) else None
        code = code or precondition_code(item.get("message", "") if isinstance(item, dict) else item)
        if code and code not in codes:
            codes.append(code)
    return codes


def resources():
    global _bridge, _storage
    if _bridge is None:
        _bridge = Bridge()
    if _storage is None:
        _storage = Storage()
    return _bridge, _storage


# Client idempotency keys (AUD-010): printable, bounded, long enough not to collide by accident.
_OPERATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
# Modes whose uncertain outcome can be retried under the same key: analysis only reads Blender, and
# proposal cycles reconcile through their own ledger instead of executing twice.
_RETRYABLE_MODES = {"analyze", "propose", "verify", "preview", "apply", "revert"}


def _begin_client_operation(storage, name, key, inputs, retry_safe=False):
    """Returns (ledger, record, stored_envelope | None) for a client-supplied operation_id."""
    if not _OPERATION_KEY.match(key):
        raise ValueError("INVALID_OPERATION_ID: use 8-128 characters from [A-Za-z0-9._:-]")
    ledger = OperationLedger(storage)
    ledger_key = "client:" + key
    request = {"tool": name, "arguments": inputs}
    action, record = ledger.begin(ledger_key, name, request)
    if action == "replay":
        return ledger, record, replayed(record)
    if action == "in_progress":
        raise ValueError("OPERATION_IN_PROGRESS: operation_id " + key + " is still running; query production.operation_status")
    if action == "uncertain":
        # retry_safe tools are idempotent by content (they find their own earlier result in Blender).
        if not retry_safe and inputs.get("mode", None) not in _RETRYABLE_MODES:
            raise ValueError("OPERATION_OUTCOME_UNCERTAIN: operation_id " + key + " may or may not have completed in Blender ("
                             + str(record.get("uncertainty") or "transport failure") + "). It is not executed again: inspect the "
                             "scene and checkpoint " + str((record.get("result") or {}).get("checkpoint")) + ", then use a new operation_id")
        ledger.abandon(ledger_key)
        action, record = ledger.begin(ledger_key, name, request)
        ledger.mark(ledger_key, "RUNNING", previous_attempt="UNCERTAIN")
    return ledger, record, None


# Readers whose payload changes Blender state while it runs (frame_set, temporary Action/pose
# assignment) and restores it before returning. Net effect is none, but they are not read-only:
# an interrupted call can leave the frame or an Action changed (AUD-016). Measured by
# tests/unit/test_m31_hints.py, which inspects every tool's linked payload.
TRANSIENT_READERS = frozenset({
    "production.animation_detect_foot_sliding", "production.animation_compare_actions",
    "production.animation_analyze_contacts", "production.animation_analyze_root_motion",
    "production.animation_analyze_balance", "production.animation_analyze_motion_quality",
    "production.animation_refinement_plan", "production.animation_analyze_foot_phase",
    "production.animation_analyze_effector_contact", "production.animation_detect_limb_pops",
    "production.animation_detect_limb_stretch", "production.deformation_analyze",
    "production.deformation_compare", "production.animation_deformation_qa", "production.mocap_analyze",
    # M7: control probes / measured poses / per-frame facial QA, all restored before returning.
    "production.facial_classify_rig", "production.facial_analyze_shape_keys", "production.facial_analyze_bones",
    "production.facial_analyze_hybrid", "production.facial_compatibility_report", "production.facial_analyze_eyes",
    "production.facial_analyze_eyelids", "production.facial_analyze_jaw", "production.facial_analyze_lips",
    "production.facial_map_visemes", "production.facial_analyze_asymmetry", "production.facial_validate_expression",
    "production.facial_validate_lipsync", "production.facial_deformation_qa", "production.facial_mocap_map",
})


DESTRUCTIVE = frozenset({"production.restore_checkpoint", "production.cleanup_artifacts"})
from .physics.tools import TRANSIENT as PHYSICS_TRANSIENT
TRANSIENT_READERS = TRANSIENT_READERS | frozenset('production.'+name for name in PHYSICS_TRANSIENT)


def tool(name, read_only=None, retry_safe=False):
    def register(function):
        effective_read_only = read_only if read_only is not None else name in {
            "production.scene_audit", "production.inspect_armature", "production.rig_analyze",
            "production.animation_analyze", "production.animation_detect_foot_sliding",
            "production.animation_detect_quaternion_flips", "production.diff_scene",
        }

        @wraps(function)
        def wrapped(*args, **kwargs):
            # Each execution gets its own id (checkpoints, proposals). A client operation_id is the
            # idempotency key of a mutating call: a retry with it never executes the mutation twice.
            operation_id = None if effective_read_only else kwargs.pop("operation_id", None)
            execution_id = uuid4().hex
            started = time.perf_counter()
            result = {"status": "success", "operation_id": operation_id or execution_id, "execution_id": execution_id,
                      "tool": name, "affected": {}, "before": None, "after": None,
                      "warnings": [], "errors": [], "metrics": {}, "checkpoint": None}
            storage = None
            ledger = record = None
            try:
                bridge, storage = resources()
                inputs = pyinspect.signature(function).bind_partial(*args, **kwargs).arguments
                result["affected"] = {key: inputs[key] for key in ("object_name", "armature", "action_name") if inputs.get(key)}
                if operation_id is not None:
                    ledger, record, stored = _begin_client_operation(storage, name, operation_id, inputs, retry_safe)
                    if stored is not None:
                        return stored
                with bridge.lock:
                    data = function(*args, _operation_id=execution_id, **kwargs)
                result["data"] = data
                for key in ("before", "after", "checkpoint", "warnings", "affected", "mode", "proposal",
                            "target", "correction_id", "operation", "numeric_verification", "visual_verification",
                            "idempotency"):
                    if key in data:
                        result[key] = data[key]
                result["metrics"].update(data.get("metrics", {}))
                if data.get("status") == "error":
                    result["status"] = "error"
                    result["errors"] = data.get("errors", ["Blender operation failed"])
                elif data.get("status") == "warning" or result["warnings"]:
                    result["status"] = "warning"
            except Exception as exc:
                result["status"] = "error"
                result["errors"] = [error_record(exc)]
            result["error_codes"] = error_codes(result["errors"])
            result["metrics"]["elapsed_ms"] = round((time.perf_counter()-started)*1000, 3)
            if ledger is not None:
                state = outcome_state(result)
                result["idempotency"] = dict(idempotency_block(dict(record, state=state), False),
                                             inner=result.get("idempotency"))
                try:
                    ledger.finish("client:" + operation_id, state, result=result)
                except OSError as exc:
                    result["warnings"].append(f"Could not record operation ledger: {exc}")
            event = {k: result[k] for k in ("status", "operation_id", "execution_id", "tool", "errors", "metrics")}
            logging.getLogger("production").info(json.dumps(event))
            if storage:
                try:
                    with (storage.root / "operations.jsonl").open("a", encoding="utf-8") as log:
                        log.write(json.dumps(event, ensure_ascii=False)+"\n")
                except OSError as exc:
                    result["warnings"].append(f"Could not write operation log: {exc}")
                    if result["status"] == "success":
                        result["status"] = "warning"
            return result
        # Internal execution IDs are generated by the server; clients may only add an idempotency key.
        signature = pyinspect.signature(function)
        parameters = [p for n, p in signature.parameters.items() if n != "_operation_id"]
        if not effective_read_only:
            parameters.append(pyinspect.Parameter("operation_id", pyinspect.Parameter.KEYWORD_ONLY,
                                                  default=None, annotation="str | None"))
        wrapped.__signature__ = signature.replace(parameters=parameters)

        # FastMCP runs synchronous tools inside its event loop (AUD-009): a long Blender call would
        # block every other request, including production.operation_status. Work runs in a thread;
        # bridge.lock still serializes Blender access.
        @wraps(function)
        async def invoke(**kwargs):
            return await anyio.to_thread.run_sync(partial(wrapped, **kwargs))
        invoke.__signature__ = wrapped.__signature__
        transient = effective_read_only and name in TRANSIENT_READERS
        scene_effects = "transient_restored" if transient else "none" if effective_read_only else "persistent_or_artifacts"
        mcp.tool(name=name, annotations=ToolAnnotations(
            readOnlyHint=effective_read_only and not transient, destructiveHint=(name in DESTRUCTIVE),
            idempotentHint=effective_read_only, openWorldHint=False),
            meta={"production": {"scene_effects": scene_effects, "accepts_operation_id": not effective_read_only}})(invoke)
        return wrapped
    return register


@tool("production.scene_audit")
def audit(object_name: str | None = None, limit: int = 100, offset: int = 0, detail: str = "summary", *, _operation_id="") -> dict:
    """Summarize scene, units, frame range, objects and issues; paginated."""
    return scene_audit(resources()[0], object_name=object_name, limit=limit, offset=offset, detail=detail)


@tool("production.create_checkpoint")
def checkpoint(description: str, operation: str | None = None, *, _operation_id="") -> dict:
    """Save a new .blend copy plus provenance, snapshot and SHA256 manifest."""
    bridge, storage = resources()
    data = checkpoints.create_checkpoint(bridge, storage, description, _operation_id, operation)
    return {**data, "checkpoint": data["manifest"]}


@tool("production.restore_checkpoint")
def restore(checkpoint: str, dry_run: bool = True, *, _operation_id="") -> dict:
    """Plan by default; apply saves recovery checkpoint then opens a new working copy."""
    bridge, storage = resources()
    return checkpoints.restore_checkpoint(bridge, storage, checkpoint, _operation_id, dry_run)


@tool("production.diff_scene")
def diff(checkpoint: str, other_checkpoint: str | None = None, limit: int = 100, offset: int = 0, *, _operation_id="") -> dict:
    """Compare checkpoint metadata with live scene or another checkpoint; not a full binary diff."""
    bridge, storage = resources()
    return checkpoints.diff_scene(bridge, storage, checkpoint, other_checkpoint, limit, offset)


@tool("production.operation_status", read_only=True)
def operation_status(operation_id: str | None = None, correction_id: str | None = None, *, _operation_id="") -> dict:
    """What happened to an operation: ledger state (NEW/RUNNING/SUCCEEDED/FAILED/UNCERTAIN/REVERTED) of a client operation_id or of a correction_id's preview/apply/revert. Never contacts Blender."""
    if bool(operation_id) == bool(correction_id):
        raise ValueError("Give exactly one of operation_id or correction_id")
    ledger = OperationLedger(resources()[1])

    def summary(key):
        record = ledger.get(key)
        if record is None:
            return {"key": key, "state": "NEW", "meaning": "no record: never started, or abandoned before reaching Blender"}
        stored = record.get("result") or {}
        return {"key": key, "state": record["state"], "tool": record.get("tool"), "attempt": record.get("attempt"),
                "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
                "checkpoint": record.get("checkpoint") or stored.get("checkpoint"),
                "candidate_name": record.get("candidate_name"), "uncertainty": record.get("uncertainty"),
                "reconciliation": record.get("reconciliation"), "reverted_by": record.get("reverted_by"),
                "result_status": stored.get("status"), "error_codes": stored.get("error_codes") or error_codes(stored.get("errors")),
                "correction_id": stored.get("correction_id"), "operation": stored.get("operation"),
                "working_action": stored.get("working_action") or (stored.get("data") or {}).get("working_action"),
                "history": record.get("history", [])}
    if operation_id:
        return {"status": "success", "operation": summary("client:" + operation_id)}
    applied = ledger.get(correction_id + ":apply")
    keys = [correction_id + ":preview", correction_id + ":apply"]
    keys += [correction_id + ":revert:" + str(n) for n in range(0, int(applied.get("attempt", 1)) + 1 if applied else 1)]
    return {"status": "success", "cycle": [summary(key) for key in keys]}


@tool("production.inspect_armature")
def armature(armature: str, limit: int = 100, offset: int = 0, *, _operation_id="") -> dict:
    """Inspect bones, hierarchy, constraints and drivers without modifying the rig."""
    return inspect_armature(resources()[0], armature, limit=limit, offset=offset)


@tool("production.rig_analyze")
def rig(armature: str, limit: int = 100, offset: int = 0, *, _operation_id="") -> dict:
    """Detect actual IK constraints and infer FK/control chains with confidence."""
    return rig_analyze(resources()[0], armature, limit=limit, offset=offset)


@tool("production.animation_analyze")
def animation(object_name: str | None = None, action_name: str | None = None, limit: int = 100, offset: int = 0, *, _operation_id="") -> dict:
    """Summarize Actions and F-Curves, including layered actions and warnings."""
    return analyze(resources()[0], object_name=object_name, action_name=action_name, limit=limit, offset=offset)


@tool("production.animation_detect_foot_sliding")
def feet(armature: str, bones: list[str] | None = None, frame_start: int | None = None,
         frame_end: int | None = None, step: int = 1, ground_height_m: float = 0.0,
         contact_height_m: float = 0.03, contact_speed_m_s: float = 0.25,
         sliding_tolerance_m: float = 0.01, min_contact_frames: int = 3, *, _operation_id="") -> dict:
    """Measure potential planted-foot drift in meters. Thresholds are configurable, not universal quality standards."""
    return detect_foot_sliding(resources()[0], armature, bones=bones, frame_start=frame_start,
        frame_end=frame_end, step=step, ground_height_m=ground_height_m, contact_height_m=contact_height_m,
        contact_speed_m_s=contact_speed_m_s, sliding_tolerance_m=sliding_tolerance_m,
        min_contact_frames=min_contact_frames)


@tool("production.animation_detect_quaternion_flips")
def quaternions(object_name: str | None = None, action_name: str | None = None, limit: int = 100, offset: int = 0, *, _operation_id="") -> dict:
    """Detect raw quaternion hemisphere changes; q and -q represent the same orientation."""
    return detect_quaternion_flips(resources()[0], object_name=object_name, action_name=action_name, limit=limit, offset=offset)


@tool("production.asset_status", read_only=True)
def asset_status(*, _operation_id="") -> dict:
    """Role of the open .blend (MASTER/WORKING_COPY/FIXTURE/TEMPORARY/UNREGISTERED) and whether persistent mutation is allowed."""
    from .common.asset_safety import current_asset
    bridge, storage = resources()
    return {"status": "success", "asset": current_asset(bridge, storage)}


@tool("production.asset_register_master")
def asset_register_master(confirm_master: bool = False, *, _operation_id="") -> dict:
    """Register the open saved .blend as a read-only MASTER (records SHA256). Requires confirm_master=true. Never modifies the file."""
    from .common.asset_safety import register_master
    if confirm_master is not True:
        raise ValueError("CONFIRMATION_REQUIRED: set confirm_master=true to register the open file as a read-only MASTER")
    bridge, storage = resources()
    return {"status": "success", "asset": register_master(bridge, storage)}


@tool("production.asset_register_fixture")
def asset_register_fixture(path: str, *, _operation_id="") -> dict:
    """Register the open generated test file as FIXTURE; only under the system temp dir, <workdir>/fixtures or BLENDER_PRODUCTION_FIXTURE_ROOTS."""
    from .common.asset_safety import register_fixture
    bridge, storage = resources()
    return {"status": "success", "asset": register_fixture(bridge, storage, path)}


@tool("production.asset_create_working_copy")
def asset_create_working_copy(*, _operation_id="") -> dict:
    """Save a stamped copy of the open .blend under <workdir>/working, open it (scripts disabled) and register it as WORKING_COPY. The source file is not modified."""
    from .common.asset_safety import create_working_copy
    bridge, storage = resources()
    copy = create_working_copy(bridge, storage, _operation_id)
    return {"status": "warning", "asset": copy, "warnings": copy.pop("warnings", [])}


@tool("production.cleanup_artifacts")
def cleanup_artifacts(dry_run: bool = True, min_age_days: float = 7.0, keep_last: int = 5,
                      categories: list[str] | None = None, *, _operation_id="") -> dict:
    """Lifecycle inventory of workspace checkpoints/qa/operations/working copies (created_at, operation_id, source, status, retain_reason, eligible_for_cleanup). dry_run=true (default) deletes nothing; recovery points of uncertain operations, applied proposals and registered assets are always retained. Candidate Actions are reported, never deleted."""
    from .common.lifecycle import cleanup
    bridge, storage = resources()
    return {"status": "success", **cleanup(storage, dry_run=dry_run, min_age_days=min_age_days, keep_last=keep_last,
                                           categories=categories, bridge=bridge)}


@tool("production.qa_capture_viewport")
def viewport(max_size: int = 1280, *, _operation_id="") -> dict:
    """Capture actual viewport to a unique local PNG, restoring temporary render settings."""
    bridge, storage = resources()
    folder = storage.reserve("qa", _operation_id)
    return capture_viewport(bridge, str(folder / "viewport.png"), max_size=max_size)


@tool("production.qa_render_preview")
def preview(frame_start: int | None = None, frame_end: int | None = None, step: int = 1,
            width: int = 640, height: int = 360, max_frames: int = 24, allow_cpu_fallback: bool = False,
            *, _operation_id="") -> dict:
    """Render a bounded PNG sequence using scene camera; save checkpoint and restore settings."""
    bridge, storage = resources()
    # Refuse a CPU render (AUD-015) before saving a checkpoint.
    backend = render_backend(bridge, allow_cpu_fallback)
    safety = checkpoints.create_checkpoint(bridge, storage, "Before QA preview", _operation_id, "qa_render_preview")
    folder = storage.reserve("qa", _operation_id)
    frames_dir = folder / "frames"
    frames_dir.mkdir()
    try:
        data = render_preview(bridge, str(frames_dir), frame_start=frame_start, frame_end=frame_end,
                              step=step, width=width, height=height, max_frames=max_frames,
                              allow_cpu_fallback=allow_cpu_fallback)
    except Exception as exc:
        return {"status": "error", "errors": [{"type": type(exc).__name__, "message": str(exc)}],
                "checkpoint": safety["manifest"], "output_dir": str(frames_dir),
                "state_restored": "unverified", "warnings": ["Do not automatically retry: Blender may still be rendering. Inspect partial artifacts and scene state first."]}
    return {**data, "checkpoint": safety["manifest"], "render_backend": data.get("render_backend", backend)}


from .animation.tools import register as register_animation_tools
register_animation_tools(tool, resources)

from .rig.tools import register as register_rig_tools
from .contacts.service import register as register_contact_tools
from .mocap.service import register as register_mocap_tools
from .deformation import register as register_deformation_tools
register_rig_tools(tool, resources)
register_contact_tools(tool, resources)
register_mocap_tools(tool, resources)
register_deformation_tools(tool, resources)

from .mesh import register as register_mesh_tools
register_mesh_tools(tool, resources)

from .character import register as register_character_tools
register_character_tools(tool, resources)

from .retopology import register as register_retopology_tools
from .transfer.service import register as register_transfer_tools
from .game_mesh.service import register as register_game_mesh_tools
from .game_mesh.validation import register as register_game_validation_tools
from .game_mesh.package import register as register_game_package_tools
from .uv import register as register_uv_tools
from .bake import register as register_bake_tools
register_retopology_tools(tool, resources)
register_transfer_tools(tool, resources)
register_game_mesh_tools(tool, resources)
register_game_validation_tools(tool, resources)
register_game_package_tools(tool, resources)
register_uv_tools(tool, resources)
register_bake_tools(tool, resources)

from .animation.generation.tools import register as register_generation_tools
register_generation_tools(tool, resources)

from .facial.tools import register as register_facial_tools
register_facial_tools(tool, resources)

from .physics.tools import register as register_physics_tools
register_physics_tools(tool, resources)

from .materials.tools import register as register_material_tools
register_material_tools(tool, resources)


def main():
    bridge, _ = resources()
    try:
        with ControllerLease(bridge.port):
            mcp.run(transport="stdio")
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
