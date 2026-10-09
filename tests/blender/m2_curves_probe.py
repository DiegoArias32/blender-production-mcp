"""Isolated real-Blender tests for conservative M2 curve corrections.

No addon/socket, user preferences or production asset is used. Pass --output
to retain a JSON report; the test scene exists only in the subprocess memory.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

from blender_mcp.production.animation.curve_runtime import CURVE_RUNTIME
from blender_mcp.production.animation.scripts import ACTION_HELPERS
from blender_mcp.production.common.bridge import build_code


PROBE = r'''
checks = []
results = {}
def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)

def scalar_fixture(name, values, interpolation="LINEAR"):
    obj = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(obj)
    for frame, value in values:
        obj.location.x = value
        obj.keyframe_insert(data_path="location", index=0, frame=frame)
    action = obj.animation_data.action
    for row in action_curves(action, None):
        for point in row[3].keyframe_points:
            point.interpolation = interpolation
    return obj, action

def quaternion_fixture(name, values, interpolation="LINEAR"):
    obj = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(obj)
    obj.rotation_mode = "QUATERNION"
    for frame, quat in values:
        obj.rotation_quaternion = quat
        obj.keyframe_insert(data_path="rotation_quaternion", frame=frame)
    action = obj.animation_data.action
    for row in action_curves(action, None):
        for point in row[3].keyframe_points:
            point.interpolation = interpolation
            point.handle_left_type = "FREE"
            point.handle_right_type = "FREE"
            point.handle_left = (float(point.co[0]) - 0.25, float(point.co[1]))
            point.handle_right = (float(point.co[0]) + 0.25, float(point.co[1]))
    return obj, action

def snapshot(action):
    return [{"curve": m2_curve_identity(row), "points": [m2_curve_point(p) for p in row[3].keyframe_points]} for row in action_curves(action, None)]

def exercise(obj, source, kind, options):
    original = snapshot(source)
    proposal = m2_curve_proposal(obj, source, kind, options)
    check(obj.name + " proposal supported", proposal["supported"])
    check(obj.name + " proposal read only", snapshot(source) == original)
    candidate = source.copy()
    candidate.name = source.name + "_Candidate"
    obj.animation_data.action = candidate
    applied = m2_curve_apply(obj, candidate, kind, proposal, options)
    verification = m2_curve_verify(obj, source, candidate, kind, proposal, options)
    check(obj.name + " source preserved exactly", snapshot(source) == original)
    results[obj.name] = {"applied": applied, "verification": verification, "proposal_metrics": proposal["metrics"]}
    obj.animation_data.action = source
    return candidate, proposal, applied, verification

obj, source = scalar_fixture("M2_Redundant", [(i, i * .1) for i in range(1, 21)])
source["fixture_metadata"] = "preserved"
candidate, proposal, applied, verified = exercise(obj, source, "cleanup", {"tolerance": .000001})
check("redundant real keys reduced 20 to 2", verified["metrics"]["keys_before"] == 20 and verified["metrics"]["keys_after"] == 2)
check("redundant real motion within tolerance", verified["passed"] and verified["metrics"]["max_error"] <= .000001)
check("copy metadata preserved", candidate["fixture_metadata"] == "preserved")

obj, source = scalar_fixture("M2_FastIntentional", [(i, i * 100) for i in range(1, 10)])
analysis = m2_curve_analyze(obj, source, {})
check("uniform fast motion not labeled spike", "isolated_spike_candidate" not in analysis["metrics"]["counts_by_type"])

obj, source = scalar_fixture("M2_Spike", [(i, 10 if i == 5 else 0) for i in range(1, 10)])
analysis = m2_curve_analyze(obj, source, {})
check("real artificial spike identified", analysis["metrics"]["counts_by_type"].get("isolated_spike_candidate") == 1)
proposal = m2_curve_proposal(obj, source, "cleanup", {"strategies": ["spike"], "tolerance": 20})
check("spike apply blocked without explicit optin", not proposal["supported"])
candidate, proposal, applied, verified = exercise(obj, source, "cleanup", {"strategies": ["spike"], "allow_spike_correction": True, "tolerance": 20})
check("explicit bounded spike proposal applied and verified", applied["keys_modified"] == 1 and verified["passed"])

obj, source = scalar_fixture("M2_Noise", [(1, 0), (2, .001), (3, 0), (4, .001), (5, 0)])
analysis = m2_curve_analyze(obj, source, {"noise_threshold": .002})
check("small alternating noise candidate identified", analysis["metrics"]["counts_by_type"].get("small_alternating_motion_candidate", 0) > 0)
candidate, proposal, applied, verified = exercise(obj, source, "cleanup", {"strategies": ["small_noise"], "allow_smoothing": True, "tolerance": .0006})
check("bounded optional smoothing verified", verified["passed"] and verified["metrics"]["max_error"] < .0006)

obj, source = scalar_fixture("M2_Interpolation", [(1, 0), (3, 2)], "CONSTANT")
candidate, proposal, applied, verified = exercise(obj, source, "cleanup", {"strategies": ["interpolation"], "allow_interpolation_change": True, "target_interpolation": "LINEAR", "tolerance": .1})
check("unsafe interpolation correction fails verification", not verified["passed"] and verified["metrics"]["max_error"] > 1)
check("failed correction original interpolation kept", action_curves(source, None)[0][3].keyframe_points[0].interpolation == "CONSTANT")

obj, source = quaternion_fixture("M2_QuaternionLinear", [(1, (1, 0, 0, 0)), (3, (-1, 0, 0, 0)), (5, (1, 0, 0, 0))])
candidate, proposal, applied, verified = exercise(obj, source, "quaternion", {})
check("quaternion source sign discontinuities measured", verified["metrics"]["hemisphere_changes_before"] == 2)
check("quaternion linear continuity repaired", verified["passed"] and verified["metrics"]["hemisphere_changes_after"] == 0)
check("quaternion original undefined midpoints reported", verified["metrics"]["undefined_source_samples"] == 2)
check("quaternion correction preserves rotation mode", obj.rotation_mode == "QUATERNION")
check("quaternion four components modified together", applied["keys_modified"] == 4)
candidate_curve = action_curves(candidate, None)[0][3]
check("quaternion handles sign corrected", candidate_curve.keyframe_points[1].handle_left.y == 1 and candidate_curve.keyframe_points[1].handle_right.y == 1)

obj, source = quaternion_fixture("M2_QuaternionBezier", [(1, (1, 0, 0, 0)), (3, (-1, 0, 0, 0)), (5, (1, 0, 0, 0))], "BEZIER")
candidate, proposal, applied, verified = exercise(obj, source, "quaternion", {})
check("quaternion BEZIER identity path repaired", verified["passed"] and verified["metrics"]["max_angular_error_degrees"] < .001)

obj, source = quaternion_fixture("M2_QuaternionUnsafePath", [(1, (1, 0, 0, 0)), (3, (-.70710678, 0, 0, -.70710678))])
candidate, proposal, applied, verified = exercise(obj, source, "quaternion", {})
check("distinct quaternion path change rejected", not verified["passed"] and verified["metrics"]["max_angular_error_degrees"] > 100)

obj, source = quaternion_fixture("M2_QuaternionUnaligned", [(1, (1, 0, 0, 0)), (3, (-1, 0, 0, 0))])
action_curves(source, None)[2][3].keyframe_points[1].co.x = 4
proposal = m2_curve_proposal(obj, source, "quaternion", {})
check("unaligned quaternion keys rejected", not proposal["supported"] and not proposal["patches"])

bpy.ops.object.armature_add()
obj = bpy.context.object
obj.name = "M2_QuaternionBone"
bone = obj.pose.bones[0]
bone.name = "UnconventionalControl"
bone.rotation_mode = "QUATERNION"
for frame, value in ((1, 1), (3, -1), (5, 1)):
    bone.rotation_quaternion = (value, 0, 0, 0)
    bone.keyframe_insert(data_path="rotation_quaternion", frame=frame)
source = obj.animation_data.action
for row in action_curves(source, None):
    for point in row[3].keyframe_points:
        point.interpolation = "LINEAR"
candidate, proposal, applied, verified = exercise(obj, source, "quaternion", {"bones": [bone.name]})
check("arbitrary bone name quaternion correction verified", verified["passed"] and applied["keys_modified"] == 4)
bone.rotation_mode = "XYZ"
proposal = m2_curve_proposal(obj, source, "quaternion", {})
check("inactive quaternion mode refused without changing mode", not proposal["supported"] and bone.rotation_mode == "XYZ")

obj, source = scalar_fixture("M2_Overshoot", [(1, 0), (3, 1)], "BEZIER")
curve = action_curves(source, None)[0][3]
curve.keyframe_points[0].handle_right_type = "FREE"
curve.keyframe_points[0].handle_right = (1.5, 5)
curve.keyframe_points[1].handle_left_type = "FREE"
curve.keyframe_points[1].handle_left = (2.5, 5)
curve.update()
analysis = m2_curve_analyze(obj, source, {})
check("real BEZIER overshoot detected", analysis["metrics"]["counts_by_type"].get("between_key_overshoot") == 1)

obj, source = scalar_fixture("M2_Modifier", [(1, 0), (2, 1), (3, 2)])
action_curves(source, None)[0][3].modifiers.new("NOISE")
proposal = m2_curve_proposal(obj, source, "cleanup", {})
check("modifier correction rejected safely", not proposal["supported"])

result = {"status": "PASS", "blender": bpy.app.version_string, "checks": checks,
          "check_count": len(checks), "results": results}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    body = ACTION_HELPERS + CURVE_RUNTIME + PROBE
    code = build_code(body)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="blender-m2-curves-") as directory:
        script = Path(directory) / "probe.py"
        script.write_text(code, encoding="utf-8")
        process = subprocess.run([args.blender, "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1", "--python", str(script)], text=True, capture_output=True, encoding="utf-8", errors="replace", timeout=180)
    marker = "__PRODUCTION_RESULT__="
    rows = [line[len(marker):] for line in process.stdout.splitlines() if line.startswith(marker)]
    if process.returncode or len(rows) != 1:
        raise RuntimeError(process.stdout + "\n" + process.stderr)
    report = json.loads(rows[0])
    report["elapsed_seconds"] = time.perf_counter() - started
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    print(json.dumps({"status": report["status"], "blender": report["blender"], "check_count": report["check_count"], "elapsed_seconds": report["elapsed_seconds"]}))


if __name__ == "__main__":
    main()
