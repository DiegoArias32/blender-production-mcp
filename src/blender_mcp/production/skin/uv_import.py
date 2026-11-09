"""Typed, verified import of external per-loop UVs into a NEW secondary UV map.

Never overwrites: an existing name is refused, geometry/topology/material indices/existing UV maps are
compared before and after inside Blender, and the active and render UV maps are restored. Only layers
created by this importer can be removed by ``remove_layer``.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from ..checkpoint import create_checkpoint
from ..common.asset_safety import require_mutable_asset
from ..common.storage import write_json
from . import uv_runtime, uvio

_LAYER = re.compile(r"^[A-Za-z0-9_.\- ]{1,63}$")
SAMPLES = 1024


def _layer_name(name) -> str:
    if not isinstance(name, str) or not _LAYER.match(name) or name.strip() != name:
        raise ValueError("UV_LAYER_NAME_INVALID: 1-63 characters from [A-Za-z0-9_.- ], no leading/trailing spaces")
    return name


def _object_name(name) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("UV_OBJECT_REQUIRED: object_name is required")
    return name


def sample_indices(count: int, samples: int = SAMPLES) -> list[int]:
    """Deterministic exact-check positions: both ends, a uniform stride and an LCG scatter."""
    picks = {0, 1, count - 1, count - 2} if count > 1 else {0}
    stride = max(1, count // samples)
    picks.update(range(0, count, stride))
    state = 2463534242
    for _ in range(samples):
        state = (1103515245 * state + 12345) % 2147483648
        picks.add(state % count)
    return sorted(i for i in picks if 0 <= i < count)


def mesh_state(bridge, object_name: str) -> dict:
    return bridge.run(uv_runtime.STATE, {"object_name": _object_name(object_name)})


def analyze(bridge, object_name: str, source_path: str, layer_name: str) -> dict:
    """Read-only: does the file fit this mesh? Nothing is written anywhere."""
    layer = _layer_name(layer_name)
    state = mesh_state(bridge, object_name)
    values, info = uvio.read_uv_array(source_path, expected_loops=state["loops"])
    blockers = []
    if layer in state["uv_layers"]:
        blockers.append({"code": "UV_LAYER_EXISTS", "message": layer + " already exists; nothing is overwritten"})
    if len(state["uv_layers"]) >= 8:
        blockers.append({"code": "UV_LAYER_CAPACITY", "message": "the mesh already has 8 UV maps"})
    if state["mode"] != "OBJECT":
        blockers.append({"code": "UV_OBJECT_MODE_REQUIRED", "message": "object is in " + state["mode"] + " mode"})
    warnings = []
    if state["mesh_users"] > 1:
        warnings.append({"code": "UV_SHARED_MESH", "message": "mesh data has " + str(state["mesh_users"])
                         + " users; the new UV map is visible on all of them"})
    bounds = info["bounds"]
    if bounds["u_min"] < 0 or bounds["v_min"] < 0:
        warnings.append({"code": "UV_NEGATIVE_COORDINATES", "message": "values below 0 are stored as given"})
    tiles = sorted({1001 + int(math.floor(u)) + 10 * int(math.floor(v))
                    for u, v in ((bounds["u_min"], bounds["v_min"]), (bounds["u_max"], bounds["v_max"]))
                    if 0 <= u < 10 and v >= 0})
    fingerprint = {k: state[k] for k in ("vertices", "edges", "polygons", "loops")}
    return {"status": "warning" if blockers or warnings else "success", "mode": "analyze", "object": state["object"],
            "mesh": state, "source": info, "layer_name": layer, "fingerprint": fingerprint,
            "udim_tiles_at_bounds": tiles, "blockers": blockers, "warnings": warnings,
            "ready": not blockers, "_values": values}


def apply(bridge, storage, operation_id: str, object_name: str, source_path: str, layer_name: str) -> dict:
    asset = require_mutable_asset(bridge, storage, "typed UV import")
    report = analyze(bridge, object_name, source_path, layer_name)
    values = report.pop("_values")
    if report["blockers"]:
        raise ValueError(report["blockers"][0]["code"] + ": " + report["blockers"][0]["message"])
    folder = storage.reserve("skin_uv", operation_id)
    carrier = uvio.write_carrier_exr(folder / "carrier.exr", values)
    checkpoint = create_checkpoint(bridge, storage, "Before typed UV import of " + layer_name + " on " + report["object"],
                                   operation_id, "mesh_import_uv_layer")
    picks = sample_indices(len(values))
    params = {"object_name": report["object"], "layer_name": layer_name, "loops": report["source"]["loops"],
              "fingerprint": report["fingerprint"], "carrier_path": carrier["path"],
              "carrier_width": carrier["width"], "carrier_height": carrier["height"],
              "sum_u": math.fsum(values[0::2]), "sum_v": math.fsum(values[1::2]),
              "checks": [[i, values[i]] for i in picks]}
    result = bridge.run(uv_runtime.IMPORT, params)
    record = {"operation_id": operation_id, "asset": asset, "source": report["source"], "carrier": carrier,
              "checkpoint": checkpoint["manifest"], "exact_samples": len(picks), "result": result}
    write_json(folder / "import.json", record)
    # The carrier is derived data (the caller's .npy stays); remove it once Blender verified the import.
    Path(carrier["path"]).unlink()
    return {"status": "success", "mode": "apply", "object": report["object"], "layer_name": layer_name,
            "source": report["source"], "carrier": {k: carrier[k] for k in ("width", "height", "bytes", "sha256")},
            "carrier_removed": True, "exact_samples_checked": len(picks), "verification": result["verification"],
            "written_bounds": result["written_bounds"], "carrier_read": result["carrier"],
            "before": result["before"], "after": result["after"], "warnings": report["warnings"],
            "checkpoint": checkpoint["manifest"], "record": str(folder / "import.json")}


def remove_layer(bridge, storage, object_name: str, layer_name: str) -> dict:
    require_mutable_asset(bridge, storage, "imported UV map removal")
    return bridge.run(uv_runtime.REMOVE, {"object_name": _object_name(object_name), "layer_name": _layer_name(layer_name)})
