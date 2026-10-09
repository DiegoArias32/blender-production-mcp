"""Numerical and safety contracts independent of an installed Blender."""
import copy
from types import SimpleNamespace

import pytest

from blender_mcp.production.animation.curve_ops import ANALYZE_FCURVES, analyze_fcurves
from blender_mcp.production.animation.curve_runtime import CURVE_RUNTIME
from blender_mcp.production.common.bridge import build_code


class Points(list):
    def remove(self, point, fast=False):
        super().remove(point)


class Curve:
    def __init__(self, values, path="location", index=0):
        self.data_path, self.array_index = path, index
        self.keyframe_points = Points(SimpleNamespace(co=[float(frame), float(value)],
            handle_left=[float(frame) - 0.2, float(value)],
            handle_right=[float(frame) + 0.2, float(value)],
            handle_left_type="FREE", handle_right_type="FREE", interpolation="LINEAR")
            for frame, value in values)
        self.modifiers, self.sampled_points, self.mute = [], [], False

    def evaluate(self, frame):
        points = self.keyframe_points
        if frame <= points[0].co[0]:
            return points[0].co[1]
        for left, right in zip(points, points[1:]):
            if frame <= right.co[0]:
                if left.interpolation == "CONSTANT" and frame < right.co[0]:
                    return left.co[1]
                return left.co[1] + (right.co[1] - left.co[1]) * (frame - left.co[0]) / (right.co[0] - left.co[0])
        return points[-1].co[1]

    def update(self):
        pass


@pytest.fixture
def runtime():
    namespace = {"action_curves": lambda action, slot: [(None, None, None, curve) for curve in action.curves]}
    exec(CURVE_RUNTIME, namespace)
    return SimpleNamespace(**{key: value for key, value in namespace.items() if key.startswith("m2_")})


def fixture(curves):
    action = SimpleNamespace(name="Source", curves=curves)
    obj = SimpleNamespace(name="Fixture", rotation_mode="QUATERNION", animation_data=SimpleNamespace(action=action))
    return obj, action


def quaternion(values):
    return [Curve([(frame, q[index]) for frame, q in values], "rotation_quaternion", index) for index in range(4)]


def correction(runtime, curves, kind, options=None):
    obj, action = fixture(curves)
    proposal = runtime.m2_curve_proposal(obj, action, kind, options)
    duplicate = copy.deepcopy(action)
    duplicate.name = "Candidate"
    applied = runtime.m2_curve_apply(obj, duplicate, kind, proposal, options)
    verified = runtime.m2_curve_verify(obj, action, duplicate, kind, proposal, options)
    return proposal, applied, verified, action, duplicate


def test_real_payload_passes_unchanged_safe_mode():
    # Linked payloads carry only reachable functions (M3.1 payload linker), so the complete
    # runtime — including verification code the analysis payload no longer ships — is validated
    # separately against the unchanged upstream validator.
    from blender_mcp.safe_mode import validate_code
    assert "m2_curve_analyze" in build_code(ANALYZE_FCURVES, {"object_name": "Fixture", "options": {}})
    assert "def m2_curve_verify" in ANALYZE_FCURVES
    validate_code("import json\nparams = json.loads('{}')\n" + ANALYZE_FCURVES)


@pytest.mark.parametrize("options", [
    {"tolerance": float("nan")}, {"angular_tolerance_degrees": 6},
    {"samples_per_interval": 3}, {"frame_start": 10, "frame_end": 1},
    {"bones": []}, {"severity": "catastrophic"}, {"limit": True},
    {"per_channel_tolerances": {"location[0]": -1}},
    {"array_indices": [-1]}, {"detail": "raw"},
    {"cleanup_strategy": "redundant"}, {"allow_smoothing": 1},
])
def test_options_reject_invalid_values(runtime, options):
    with pytest.raises(ValueError):
        runtime.m2_curve_options(options)


def test_constant_fast_motion_not_a_spike(runtime):
    obj, action = fixture([Curve([(i, i * 100) for i in range(1, 10)])])
    report = runtime.m2_curve_analyze(obj, action, {})
    assert report["metrics"]["counts_by_type"] == {"redundant_linear_key": 7}


def test_isolated_spike_contextual_detection(runtime):
    obj, action = fixture([Curve([(i, 10 if i == 5 else 0) for i in range(1, 10)])])
    report = runtime.m2_curve_analyze(obj, action, {})
    spikes = [f for f in report["findings"] if f["type"] == "isolated_spike_candidate"]
    assert [f["frame"] for f in spikes] == [5]


def test_bone_channel_frame_severity_pagination(runtime):
    obj, action = fixture([Curve([(1, 0), (2, 1), (3, 2), (4, 3)], 'pose.bones["Ankle"].location'),
                           Curve([(1, 0), (2, 1), (3, 2)], 'pose.bones["Hand"].location')])
    report = runtime.m2_curve_analyze(obj, action, {"bones": ["Ankle"], "channels": ["location"], "severity": "info", "frame_start": 3, "limit": 1, "detail": "full"})
    assert report["metrics"]["channels_analyzed"] == 1
    assert report["findings"][0]["frame"] == 3
    assert report["pagination"]["total"] == 1


def test_redundant_cleanup_preserves_source_and_motion(runtime):
    proposal, applied, verified, original, duplicate = correction(runtime, [Curve([(1, 0), (2, 1), (3, 2), (4, 3)])], "cleanup")
    assert proposal["supported"] and applied["keys_removed"] == 2
    assert len(original.curves[0].keyframe_points) == 4
    assert len(duplicate.curves[0].keyframe_points) == 2
    assert verified["passed"] and verified["metrics"]["max_error"] == 0


def test_quaternion_signs_handles_and_dense_orientation(runtime):
    curves = quaternion([(1, (1, 0, 0, 0)), (2, (-1, 0, 0, 0)), (3, (1, 0, 0, 0))])
    proposal, applied, verified, original, duplicate = correction(runtime, curves, "quaternion")
    assert applied["keys_modified"] == 4
    assert original.curves[0].keyframe_points[1].co[1] == -1
    assert duplicate.curves[0].keyframe_points[1].handle_left[1] == 1
    assert verified["passed"]
    assert verified["metrics"]["hemisphere_changes_before"] == 2
    assert verified["metrics"]["hemisphere_changes_after"] == 0
    assert verified["metrics"]["undefined_source_samples"] == 2


def test_distinct_orientations_fail_dense_preservation(runtime):
    curves = quaternion([(1, (1, 0, 0, 0)), (3, (-0.70710678, 0, 0, -0.70710678))])
    proposal, applied, verified, original, duplicate = correction(runtime, curves, "quaternion")
    assert applied["keys_modified"] == 4
    assert not verified["passed"]
    assert verified["metrics"]["max_angular_error_degrees"] > 100


def test_incomplete_quaternion_group_refused(runtime):
    obj, action = fixture(quaternion([(1, (1, 0, 0, 0)), (2, (-1, 0, 0, 0))])[:3])
    proposal = runtime.m2_curve_proposal(obj, action, "quaternion", {})
    assert not proposal["supported"] and not proposal["patches"]


def test_unaligned_quaternion_group_refused(runtime):
    curves = quaternion([(1, (1, 0, 0, 0)), (2, (-1, 0, 0, 0))])
    curves[2].keyframe_points[1].co[0] = 2.5
    obj, action = fixture(curves)
    assert not runtime.m2_curve_proposal(obj, action, "quaternion", {})["supported"]


@pytest.mark.parametrize("strategy,extra", [
    ("small_noise", {}), ("mocap_smoothing", {}), ("spike", {}),
    ("interpolation", {}), ("small_noise", {"allow_smoothing": True}),
])
def test_nondefault_cleanup_requires_explicit_optin_tolerance(runtime, strategy, extra):
    obj, action = fixture([Curve([(1, 0), (2, .001), (3, 0)])])
    proposal = runtime.m2_curve_proposal(obj, action, "cleanup", {"strategies": [strategy], **extra})
    assert not proposal["supported"]


def test_small_noise_optin_bounded_motion(runtime):
    proposal, applied, verified, original, duplicate = correction(runtime,
        [Curve([(1, 0), (2, .001), (3, 0)])], "cleanup",
        {"strategies": ["small_noise"], "allow_smoothing": True, "tolerance": .0006})
    assert applied["keys_modified"] == 1
    assert verified["passed"] and verified["metrics"]["max_error"] == .0005


def test_verification_detects_error_beyond_tolerance(runtime):
    obj, source = fixture([Curve([(1, 0), (2, 1), (3, 2)])])
    proposal = runtime.m2_curve_proposal(obj, source, "cleanup", {})
    candidate = copy.deepcopy(source)
    runtime.m2_curve_apply(obj, candidate, "cleanup", proposal, {})
    candidate.curves[0].keyframe_points[-1].co[1] += .1
    verified = runtime.m2_curve_verify(obj, source, candidate, "cleanup", proposal, {})
    assert not verified["passed"]


def test_stale_proposal_rejected_before_mutating(runtime):
    obj, source = fixture([Curve([(1, 0), (2, 1), (3, 2)])])
    proposal = runtime.m2_curve_proposal(obj, source, "cleanup", {})
    candidate = copy.deepcopy(source)
    candidate.curves[0].keyframe_points[1].co[1] = 9
    with pytest.raises(ValueError, match="Stale"):
        runtime.m2_curve_apply(obj, candidate, "cleanup", proposal, {})
    assert len(candidate.curves[0].keyframe_points) == 3


def test_quaternion_excluded_from_scalar_cleanup(runtime):
    obj, source = fixture(quaternion([(1, (1, 0, 0, 0)), (2, (1, 0, 0, 0)), (3, (1, 0, 0, 0))]))
    proposal = runtime.m2_curve_proposal(obj, source, "cleanup", {})
    assert not proposal["patches"] and proposal["warnings"]


def test_analysis_only_bridge_contract():
    class Bridge:
        def run(self, body, params):
            assert body == ANALYZE_FCURVES
            assert params["object_name"] == "Fixture"
            return {"metrics": {}}
    assert analyze_fcurves(Bridge(), "Fixture") == {"metrics": {}}
    with pytest.raises(ValueError):
        analyze_fcurves(Bridge(), "")


def test_untargeted_channel_change_fails(runtime):
    obj, source = fixture([Curve([(1, 0), (2, 1), (3, 2)]), Curve([(1, 0), (3, 0)], "scale")])
    proposal = runtime.m2_curve_proposal(obj, source, "cleanup", {"channels": ["location"]})
    candidate = copy.deepcopy(source)
    runtime.m2_curve_apply(obj, candidate, "cleanup", proposal, {})
    candidate.curves[1].keyframe_points[0].co[1] = 1
    assert not runtime.m2_curve_verify(obj, source, candidate, "cleanup", proposal, {})["passed"]


def test_nonfinite_key_and_handle_rejected(runtime):
    obj, source = fixture([Curve([(1, 0), (2, 1), (3, 2)])])
    source.curves[0].keyframe_points[1].handle_left[1] = float("nan")
    with pytest.raises(ValueError, match="Non-finite"):
        runtime.m2_curve_proposal(obj, source, "cleanup", {})


def test_dense_sampling_budget_checked_before_expansion(runtime):
    curves = [Curve([(i, i) for i in range(1000)], "location", i) for i in range(4)]
    with pytest.raises(ValueError, match="budget"):
        runtime.m2_curve_frames(curves, runtime.m2_curve_options({"samples_per_interval": 64}))


def test_per_channel_tolerance_overrides_default(runtime):
    obj, source = fixture([Curve([(1, 0), (2, .001), (3, 0)])])
    default = runtime.m2_curve_proposal(obj, source, "cleanup", {})
    custom = runtime.m2_curve_proposal(obj, source, "cleanup", {"per_channel_tolerances": {"location[0]": .002}})
    assert not default["patches"] and len(custom["patches"]) == 1


def test_inactive_quaternion_rotation_mode_refused(runtime):
    obj, source = fixture(quaternion([(1, (1, 0, 0, 0)), (2, (-1, 0, 0, 0))]))
    obj.rotation_mode = "XYZ"
    assert not runtime.m2_curve_proposal(obj, source, "quaternion", {})["supported"]
