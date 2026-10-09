"""Geometry invariants and safety contracts; no Blender installation needed."""
from copy import deepcopy
from unittest.mock import patch
import math

import pytest
from blender_mcp.production.common.asset_safety import CURRENT_FILE

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.deformation import options, poses, run, test_pose as stress_pose
from blender_mcp.production.deformation.geometry import dq_closed, dq_metrics, dq_volume
from blender_mcp.production.deformation.runtime import EXECUTE, PREFLIGHT
from blender_mcp.production.qa.multiview import _options, _frames, _RENDER


POINTS = [[0., 0., 0.], [1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]
TRIANGLES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]
EDGES = [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]]


def measure(current, **kwargs):
    return dq_metrics(POINTS, current, EDGES, TRIANGLES, options(), **kwargs)


def test_identical_geometry_has_no_findings():
    result = measure(deepcopy(POINTS))
    assert result["issues"] == []
    assert result["metrics"]["volume_ratio"] == 1
    assert result["metrics"]["max_displacement_m"] == 0


def test_rigid_motion_is_not_deformation_even_with_large_displacement():
    points = [[-p[1]+100, p[0]-75, p[2]+35] for p in POINTS]
    result = measure(points)
    assert result["issues"] == []
    assert result["metrics"]["max_displacement_m"] > 100
    assert result["metrics"]["min_edge_ratio"] == 1
    assert result["metrics"]["volume_ratio"] == pytest.approx(1)


def test_collapse_and_volume_loss_are_measured():
    result = measure([[p[0]*.01, p[1]*.01, p[2]*.01] for p in POINTS])
    assert {row["issue"] for row in result["issues"]} == {"edge_collapse", "triangle_collapse", "approximate_volume_loss"}
    assert result["metrics"]["volume_ratio"] == pytest.approx(1e-6)


def test_stretch_tolerance_is_configurable_for_intentional_deformation():
    stretched = [[p[0]*3, p[1], p[2]] for p in POINTS]
    assert "abnormal_edge_stretch" in {i["issue"] for i in measure(stretched)["issues"]}
    assert not dq_metrics(POINTS, stretched, EDGES, TRIANGLES, options({"stretch_ratio": 4}))["issues"]


def test_open_or_inconsistently_oriented_mesh_has_no_volume_claim():
    assert dq_closed(TRIANGLES)
    assert not dq_closed(TRIANGLES[:-1])
    flipped = [list(reversed(TRIANGLES[0])), *TRIANGLES[1:]]
    assert not dq_closed(flipped)
    result = dq_metrics(POINTS, POINTS, EDGES, TRIANGLES[:-1], options())
    assert result["metrics"]["volume_ratio"] is None


def test_region_metrics_are_local_and_volume_is_not_invented():
    current = deepcopy(POINTS)
    current[3][2] = 100
    result = measure(current, indices=[0, 1, 2])
    assert not result["issues"]
    assert result["metrics"]["volume_ratio"] is None
    assert result["metrics"]["vertices"] == 3


def test_reference_degeneracy_reported_separately():
    ref = [[0, 0, 0]] * 4
    result = dq_metrics(ref, ref, EDGES, TRIANGLES, options())
    assert result["metrics"]["reference_degenerate_edges"] == 6
    assert result["metrics"]["reference_degenerate_triangles"] == 4
    assert result["metrics"]["volume_ratio"] is None


@pytest.mark.parametrize("data", [[[0, 0, 0]], [[0, 0, float('nan')]]*4])
def test_missing_correspondence_and_nonfinite_geometry_refused(data):
    with pytest.raises(ValueError):
        measure(data)


def test_volume_translation_stable():
    shifted = [[v+10000 for v in p] for p in POINTS]
    assert dq_volume(shifted, TRIANGLES) == pytest.approx(1/6)


@pytest.mark.parametrize("setting", [{"unknown": 1}, {"max_vertices": True}, {"max_samples": 33},
    {"frames": [float('inf')]}, {"reference_frame": False}, {"collapse_ratio": 0},
    {"stretch_ratio": .9}, {"self_intersection": "yes"}, {"meshes": ["A", "A"]},
    {"regions": {"mesh": ["bone"]}}, {"regions": {"knee": []}}, {"region_weight": float('nan')}])
def test_invalid_options_fail_before_blender(setting):
    with pytest.raises(ValueError):
        options(setting)


@pytest.mark.parametrize("region", ["shoulder", "elbow", "wrist", "hip", "knee", "ankle"])
def test_six_stress_regions_require_explicit_controls(region):
    value = poses([{"region": region, "bone": "unusual-control", "axis": [2, 0, 0], "angle_degrees": 35}])
    assert value[0]["axis"] == [1, 0, 0]


@pytest.mark.parametrize("entry", [{}, {"region": "knee", "bone": "", "axis": "X", "angle_degrees": 90},
    {"region": "knee", "bone": "control", "axis": [0, 0, 0], "angle_degrees": 90},
    {"region": "knee", "bone": "control", "axis": "X", "angle_degrees": 181}])
def test_ambiguous_stress_mapping_is_rejected(entry):
    with pytest.raises(ValueError):
        poses([entry])


def test_templates_pass_upstream_safe_mode_without_exemptions():
    for template in [EXECUTE, PREFLIGHT, _RENDER]:
        assert build_code(template)


class Recorder:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def run(self, body, params):
        self.calls.append(body)
        if body == PREFLIGHT:
            return {"supported": True, "object": "Rig", "meshes": ["Mesh"]}
        if body == CURRENT_FILE:
            # Unsaved session: TEMPORARY role, mutable (AUD-014 asset guard runs before the checkpoint).
            return {"filepath": "", "is_dirty": True, "stamps": []}
        if self.fail:
            raise TimeoutError("operation may still be active")
        return {"status": "ok", "state_restored": True, "metrics": {}}


POSE = [{"region": "knee", "bone": "explicit", "axis": "X", "angle_degrees": 90}]


def test_pose_default_only_proposes_without_checkpoint_or_execution():
    bridge = Recorder()
    with patch("blender_mcp.production.deformation.create_checkpoint") as checkpoint:
        result = stress_pose(bridge, None, "op", "Rig", POSE)
    assert result["status"] == "proposed"
    assert bridge.calls == [PREFLIGHT]
    checkpoint.assert_not_called()


def test_checkpoint_precedes_pose_execution_and_transport_failure_keeps_recovery():
    bridge = Recorder(fail=True)
    def checkpoint(*args):
        assert bridge.calls == [PREFLIGHT, CURRENT_FILE]
        bridge.calls.append("checkpoint")
        return {"manifest": "recovery.json"}
    with patch("blender_mcp.production.deformation.create_checkpoint", side_effect=checkpoint):
        result = stress_pose(bridge, None, "op", "Rig", POSE, False)
    assert bridge.calls == [PREFLIGHT, CURRENT_FILE, "checkpoint", EXECUTE]
    assert result["state_restored"] is None
    assert result["checkpoint"]["manifest"] == "recovery.json"
    assert result["status"] == "error"


def test_comparison_requires_two_actions():
    with pytest.raises(ValueError):
        run(Recorder(), "Rig", "compare", actions=[None])


def test_m2_multiview_defaults_preserved_and_new_views_bounded():
    assert _options({})["views"] == ["FRONT", "SIDE", "THREE_QUARTER"]
    settings = _options({"views": ["LOW_ANGLE", "FOOT"], "framing_bones": ["toe"]})
    assert len(settings["views"]) == 2
    with pytest.raises(ValueError):
        _options({"views": ["FOOT"]})
    with pytest.raises(ValueError):
        _options({"framing_bones": ["foot"], "framing_meshes": ["mesh"]})
    with pytest.raises(ValueError):
        _options({"views": ["FRONT", "SIDE", "LOW_ANGLE", "FOOT"], "framing_bones": ["toe"]})
