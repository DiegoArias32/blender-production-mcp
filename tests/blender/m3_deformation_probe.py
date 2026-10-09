"""Real installed Blender deformation QA, state safety, previews and benchmark.

Run with host Python --blender <executable>. All work is in an isolated factory
process and newly generated fixture files. No addon, socket or GUI is used.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
# M3.1: BLENDER_PRODUCTION_TEST_ARTIFACTS moves large probe output out of the repository
# (default unchanged: <repo>/test-artifacts).
ARTIFACTS = Path(os.environ.get('BLENDER_PRODUCTION_TEST_ARTIFACTS') or ROOT / 'test-artifacts')


def inside(output):
    import bpy
    import types
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "tests/blender"))
    package = types.ModuleType("blender_mcp")
    package.__path__ = [str(ROOT / "src/blender_mcp")]
    sys.modules["blender_mcp"] = package
    from m3_deformation_fixtures import FIXTURE
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage, sha256
    from blender_mcp.production.deformation import run, test_pose
    from blender_mcp.production.deformation.runtime import RUNTIME
    from blender_mcp.production.animation.actions import BODY
    from blender_mcp.production.qa.multiview import compare_before_after

    class LocalBridge:
        def run(self, body, params=None):
            code = build_code(body, params)
            namespace = {}
            exec(code.rsplit("print(", 1)[0], namespace)
            return namespace["result"]

    bridge = LocalBridge()
    storage = Storage(output / "s")
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print("PASS " + name, flush=True)

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    fixture = bridge.run(FIXTURE + "\nresult = dq_create_fixtures()")
    path = output / "weighted-fixtures.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    source_hash = sha256(path)
    # Zero-user Actions can exist during a probe but disappear on .blend reload.
    # Check the exact saved fixture consumed by the MCP integration, not only
    # the generator's in-memory state.
    expected_actions = [row[key] for row in fixture.values() for key in ("bad_action", "good_action", "flat_action")]
    opened = bpy.ops.wm.open_mainfile(filepath=str(path), use_scripts=False, load_ui=False)
    check("saved fixture reopens successfully", "FINISHED" in opened and bpy.data.filepath == str(path))
    check("all six fixture Actions survive save and reload", all(name in bpy.data.actions for name in expected_actions))
    check("unassigned validation Actions retain fake users", all(bpy.data.actions[name].use_fake_user for name in expected_actions))
    check("reload preserves generated fixture file hash", source_hash == sha256(path))
    from m31_fixture_support import register_open_fixture
    register_open_fixture(bridge, storage, output)
    original = bridge.run(BODY + "\nresult = {a.name: m2_snapshot(a) for a in bpy.data.actions}")
    results = {}
    for key, row in fixture.items():
        common = {"reference_frame": 1, "frame": 47, "detail": "full", "regions": {key: ["JointRegion"]},
                  "self_intersection": True}
        measured = run(bridge, row["rig"], "analyze", common)
        results[key] = measured
        print(json.dumps({key: measured}, indent=2), flush=True)
        check(key + " real evaluated weighted geometry", measured["status"] == "ok" and measured["rows"][0]["metrics"]["vertices"] == row["vertices"])
        check(key + " severe deformation identified", measured["metrics"]["issue_count"] > 0)
        check(key + " vertex displacement physical units", measured["metrics"]["max_displacement_m"] > .1)
        check(key + " configured region included", any(item["region"] == key for item in measured["rows"]))
        check(key + " real BVH indicator available", measured["rows"][0]["self_intersection"]["status"] == "measured")
        check(key + " frame pose Action restored", measured["state_restored"])
        valid = run(bridge, row["rig"], "animation", {**common, "frames": [1, 12, 24, 47, 60]}, [row["good_action"]])
        results[key + "_valid"] = valid
        check(key + " valid negative pose has no deformation warnings", valid["status"] == "ok" and valid["metrics"]["issue_count"] == 0)
        compare = run(bridge, row["rig"], "compare", {**common, "frames": [1, 47]}, [row["good_action"], row["bad_action"]])
        results[key + "_comparison"] = compare
        check(key + " before after evaluated comparably", compare["state_restored"] and compare["metrics"]["issues_by_label"]["before"] == 0
              and compare["metrics"]["issues_by_label"]["after"] > 0)
        check(key + " comparison returns compact concrete delta", compare["metrics"]["delta"]["collapsed_edges"] > 0
              and compare["before"]["collapsed_edges"] == 0)
        auto_compare = run(bridge, row["rig"], "compare", {"reference_frame": 1, "max_samples": 5}, [row["flat_action"], row["bad_action"]])
        results[key + "_automatic_comparison"] = auto_compare
        check(key + " automatic comparison includes new after-only extreme", 47 in [s["frame"] for s in auto_compare["samples"]]
              and any("after:maximum_rotation" in s["reasons"] for s in auto_compare["samples"]))
        check(key + " automatic comparison detects introduced collapse", auto_compare["before"]["issue_count"] == 0
              and auto_compare["after"]["collapsed_edges"] > 0)
        sampled = run(bridge, row["rig"], "animation", {"reference_frame": 1, "contact_frames": [12], "max_samples": 8})
        results[key + "_sampled"] = sampled
        check(key + " extremum frame 47 selected", 47 in [s["frame"] for s in sampled["samples"]])
        check(key + " explicit contact event retained", any(s["frame"] == 12 and "provided_contact_event" in s["reasons"] for s in sampled["samples"]))
        check(key + " samples remain bounded", sampled["metrics"]["sampled_frames"] <= 8)
    check("folded knee has a real self-overlap indicator", results["knee"]["rows"][0]["self_intersection"]["nonadjacent_bvh_overlap_pairs"] > 0)
    row = fixture["shoulder"]
    poses = [{"region": region, "bone": row["bone"], "axis": "Y", "angle_degrees": 35}
             for region in ["shoulder", "elbow", "wrist", "hip", "knee", "ankle"]]
    proposal = test_pose(bridge, storage, "stress-propose", row["rig"], poses)
    check("stress defaults to proposal without checkpoint", proposal["dry_run"] and proposal["checkpoint"] is None)
    stressed = test_pose(bridge, storage, "stress-execute", row["rig"], poses, False, {"detail": "full"})
    results["stress"] = stressed
    check("all six explicitly mapped stress regions sampled", stressed["status"] == "ok" and len(stressed["samples"]) == 6)
    check("stress checkpoint exists and has verified hash", Path(stressed["checkpoint"]["path"]).is_file()
          and sha256(Path(stressed["checkpoint"]["path"])) == stressed["checkpoint"]["sha256"])
    check("stress restores exact original state", stressed["state_restored"])
    check("stress does not leave quaternion rotation mode", bpy.data.objects[row["rig"]].pose.bones[row["bone"]].rotation_mode == "XYZ")
    try:
        test_pose(bridge, storage, "bad-map", row["rig"], [{**poses[0], "bone": "missing"}], False)
    except ValueError:
        check("missing explicit mapping fails before mutation", True)
    else:
        raise AssertionError("Missing mapping accepted")
    target = bpy.data.objects[row["rig"]]
    con = target.pose.bones[row["bone"]].constraints.new("COPY_ROTATION")
    try:
        try:
            test_pose(bridge, storage, "constrained", row["rig"], poses, False)
        except ValueError:
            check("ambiguous constrained stress control refused", True)
        else:
            raise AssertionError("Constrained control accepted")
    finally:
        target.pose.bones[row["bone"]].constraints.remove(con)
    limited = run(bridge, row["rig"], "animation", {"frames": [1, 47], "max_vertex_samples": 1})
    check("budget failure restores pose and Action", limited["status"] == "error" and limited["state_restored"])
    original_after = bridge.run(BODY + "\nresult = {a.name: m2_snapshot(a) for a in bpy.data.actions}")
    check("all original Action content remains exact", original == original_after)
    before_state = bridge.run(RUNTIME + "\nresult = dq_state(bpy.data.objects[params['name']])", {"name": row["rig"]})
    visual = compare_before_after(bridge, storage, "low-angle", row["rig"], row["good_action"], row["bad_action"],
              {"width": 256, "height": 256, "frame_start": 47, "frame_end": 47, "max_frames": 1,
               "views": ["LOW_ANGLE", "FOOT"], "framing_bones": [row["bone"]], "framing_padding_m": .3})
    results["visual"] = visual
    check("low-angle and explicit FOOT comparison rendered", visual["status"] == "ok" and visual["metrics"]["rendered_images"] == 4)
    check("before after cameras are equivalent", visual["comparable"] and visual["state_restored"])
    after_state = bridge.run(RUNTIME + "\nresult = dq_state(bpy.data.objects[params['name']])", {"name": row["rig"]})
    check("new views restore original pose", before_state == after_state)
    started = time.perf_counter()
    benchmark = run(bridge, fixture["knee"]["rig"], "animation", {"max_samples": 12, "candidate_frames": 60})
    elapsed = (time.perf_counter()-started)*1000
    results["benchmark"] = {"elapsed_ms": elapsed, "public_bytes": len(json.dumps(benchmark).encode()),
                            "vertices": fixture["knee"]["vertices"], "sampled_frames": benchmark["metrics"]["sampled_frames"],
                            "candidate_frames": 60, "scope": "272 weighted vertices, closed triangulated tube, no self-intersection query"}
    check("benchmark actual evaluated mesh sampling", benchmark["status"] == "ok" and benchmark["metrics"]["sampled_frames"] == 12)
    check("fixture source file hash unchanged", source_hash == sha256(path))
    report = {"status": "pass", "blender_version": bpy.app.version_string, "checks": checks, "count": len(checks),
              "fixture": {"path": str(path), "sha256": source_hash, "objects": fixture}, "results": results}
    with (output / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print("M3_DEFORMATION_REPORT=" + str(output / "report.json"), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender")
    parser.add_argument("--inside")
    args = parser.parse_args(sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else None)
    if args.inside:
        inside(Path(args.inside))
        return
    output = ARTIFACTS / ("m3d-" + uuid4().hex[:8])
    output.mkdir(parents=True)
    command = [args.blender, "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1",
               "--python", str(Path(__file__).resolve()), "--", "--inside", str(output)]
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    (output / "blender.log").write_text(process.stdout + "\n" + process.stderr, encoding="utf-8")
    if process.returncode:
        print(process.stdout[-14000:] + process.stderr[-3000:])
        raise RuntimeError("Deformation probe failed: " + str(output))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    print(json.dumps({"status": report["status"], "count": report["count"], "report": str(output / "report.json")}))


if __name__ == "__main__":
    main()
