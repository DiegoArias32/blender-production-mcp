"""M3 evaluated deformation QA. No mesh/weight/Action edits."""
from __future__ import annotations

import math
import time

from ..checkpoint import create_checkpoint
from ..common.asset_safety import require_mutable_asset
from .runtime import PREFLIGHT, EXECUTE


def options(values=None):
    output = {"meshes": [], "regions": {}, "region_weight": 0.1,
              "frame": None, "reference_frame": None, "frame_start": None, "frame_end": None,
              "frames": [], "contact_frames": [], "max_samples": 12, "candidate_frames": 128,
              "max_vertices": 100000, "max_vertex_samples": 2000000,
              "collapse_ratio": 0.25, "stretch_ratio": 2.0, "area_ratio": 0.1,
              "volume_ratio": 0.6, "epsilon_m": 1e-8, "self_intersection": False,
              "detail": "summary", "offset": 0, "limit": 50}
    if values is not None and not isinstance(values, dict):
        raise ValueError("options must be an object")
    unknown = set(values or {}) - set(output)
    if unknown:
        raise ValueError("Unknown deformation options: " + ", ".join(sorted(unknown)))
    output.update(values or {})
    for key in ("frame", "reference_frame", "frame_start", "frame_end"):
        value = output[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or abs(value) > 1048574):
            raise ValueError(key + " must be a finite Blender frame")
    for key in ("frames", "contact_frames"):
        value = output[key]
        if not isinstance(value, list) or len(value) > 32 or any(isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) or abs(v) > 1048574 for v in value):
            raise ValueError(key + " must contain at most 32 finite frame numbers")
    for key, low, high in (("max_samples", 1, 32), ("candidate_frames", 2, 256), ("max_vertices", 1, 250000),
                           ("max_vertex_samples", 1, 8000000), ("offset", 0, 100000), ("limit", 1, 200)):
        if isinstance(output[key], bool) or not isinstance(output[key], int) or not low <= output[key] <= high:
            raise ValueError(f"{key} must be an integer in {low}..{high}")
    for key, low, high in (("collapse_ratio", 0, 1), ("stretch_ratio", 1, 100), ("area_ratio", 0, 1),
                           ("volume_ratio", 0, 1), ("region_weight", 0, 1), ("epsilon_m", 0, 0.01)):
        value = output[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low < value <= high:
            raise ValueError(f"{key} must be finite in ({low}, {high}]")
    if not isinstance(output["self_intersection"], bool) or output["detail"] not in {"summary", "full"}:
        raise ValueError("self_intersection must be boolean; detail must be summary/full")
    if not isinstance(output["meshes"], list) or len(output["meshes"]) > 16 or any(not isinstance(v, str) or not v for v in output["meshes"]):
        raise ValueError("meshes must contain 0..16 explicit mesh names")
    if len(set(output["meshes"])) != len(output["meshes"]):
        raise ValueError("mesh selection contains duplicates")
    regions = output["regions"]
    if not isinstance(regions, dict) or len(regions) > 12:
        raise ValueError("regions must map up to 12 names to vertex-group lists")
    for key, groups in regions.items():
        if not isinstance(key, str) or not key or key == "mesh" or not isinstance(groups, list) or not 1 <= len(groups) <= 8 or any(not isinstance(g, str) or not g for g in groups):
            raise ValueError("Each region must have a unique name and 1..8 explicit vertex groups")
    return output


def poses(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 18:
        raise ValueError("Provide 1..18 explicit stress poses")
    output = []
    for row in values:
        if not isinstance(row, dict) or set(row) != {"region", "bone", "axis", "angle_degrees"}:
            raise ValueError("A stress pose needs region, bone, axis and angle_degrees")
        if row["region"] not in {"shoulder", "elbow", "wrist", "hip", "knee", "ankle"}:
            raise ValueError("Unknown stress-pose region")
        if not isinstance(row["bone"], str) or not row["bone"]:
            raise ValueError("Explicit mapped bone is required; no heuristic bone selection")
        axis = row["axis"]
        if isinstance(axis, str):
            if axis not in {"X", "Y", "Z"}:
                raise ValueError("Axis must be X/Y/Z or a three-component unit direction")
            axis = {"X": [1, 0, 0], "Y": [0, 1, 0], "Z": [0, 0, 1]}[axis]
        if not isinstance(axis, list) or len(axis) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in axis):
            raise ValueError("Axis must have three finite components")
        length = math.sqrt(sum(v*v for v in axis))
        if length < 1e-8:
            raise ValueError("Axis must be nonzero")
        angle = row["angle_degrees"]
        if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not math.isfinite(angle) or not -180 <= angle <= 180:
            raise ValueError("Stress rotation must be finite and within +/-180 degrees")
        output.append({**row, "axis": [v/length for v in axis]})
    return output


def run(bridge, object_name, kind="analyze", values=None, actions=None):
    if not isinstance(object_name, str) or not object_name:
        raise ValueError("object_name is required")
    if kind not in {"analyze", "animation", "compare"}:
        raise ValueError("Unknown deformation analysis kind")
    if actions is not None and (not isinstance(actions, list) or any(a is not None and (not isinstance(a, str) or not a) for a in actions)):
        raise ValueError("Action names must be nonempty strings")
    if kind == "compare" and (not actions or len(actions) != 2 or any(a is None for a in actions)):
        raise ValueError("Comparison requires before and after Action names")
    started = time.perf_counter()
    params = {"object_name": object_name, "kind": kind, "options": options(values), "actions": actions or [None]}
    result = bridge.run(EXECUTE, params)
    result.setdefault("metrics", {})["elapsed_ms"] = round((time.perf_counter()-started)*1000, 3)
    result["affected"] = {"object": object_name, "actions": result.get("actions", [])}
    return result


def test_pose(bridge, storage, operation_id, object_name, stress_poses, dry_run=True, values=None):
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    settings = options(values)
    params = {"object_name": object_name, "kind": "pose", "options": settings, "poses": poses(stress_poses), "actions": [None]}
    preflight = bridge.run(PREFLIGHT, params)
    if dry_run:
        return {"status": "proposed", "dry_run": True, **preflight,
                "plan": "Checkpoint, apply each explicit local stress rotation temporarily, evaluate weighted meshes, restore exact channels/frame/Action in finally",
                "warnings": ["Stress controls must be mapped explicitly; no inferred low-confidence rig modification."], "checkpoint": None}
    asset = require_mutable_asset(bridge, storage, "deformation stress poses")
    checkpoint = create_checkpoint(bridge, storage, "Before temporary explicit deformation stress poses", operation_id, "deformation_test_pose")
    started = time.perf_counter()
    try:
        result = bridge.run(EXECUTE, params)
    except Exception as exc:
        result = {"status": "error", "state_restored": None, "errors": [{"type": type(exc).__name__, "message": str(exc)}],
                  "warnings": ["Blender state is unverified after transport failure; do not retry automatically. Recovery checkpoint retained."]}
    result.update(checkpoint=checkpoint, asset=asset, operation_id=operation_id, affected={"object": object_name, "controls": [p["bone"] for p in params["poses"]]})
    result.setdefault("metrics", {})["elapsed_ms"] = round((time.perf_counter()-started)*1000, 3)
    return result


def register(tool, resources):
    @tool("production.deformation_analyze", read_only=True)
    def analyze(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Compare evaluated geometry with a reference frame; bounded edge/area/volume metrics, no weight edits."""
        return run(resources()[0], object_name, "analyze", options)

    @tool("production.deformation_compare", read_only=True)
    def compare(object_name: str, before_action: str, after_action: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Sample two Actions against the same reference; restore assignment, pose and frame. No Action keys are edited."""
        return run(resources()[0], object_name, "compare", options, [before_action, after_action])

    @tool("production.animation_deformation_qa", read_only=True)
    def animation(object_name: str, action_name: str | None = None, options: dict | None = None, *, _operation_id="") -> dict:
        """Prioritize pose extremes, high angular changes and supplied contact events, then evaluate weighted mesh deformation."""
        return run(resources()[0], object_name, "animation", options, [action_name])

    @tool("production.deformation_test_pose")
    def stress(object_name: str, poses: list[dict], dry_run: bool = True, options: dict | None = None, *, _operation_id="") -> dict:
        """Explicit shoulder/elbow/wrist/hip/knee/ankle stress poses. Default proposal; checkpoint before temporary poses, finally restore."""
        bridge, storage = resources()
        return test_pose(bridge, storage, _operation_id, object_name, poses, dry_run, options)
