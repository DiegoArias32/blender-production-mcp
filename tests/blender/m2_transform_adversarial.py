"""Regression checks for unsafe transform corrections and stale rig context.

Only a new factory-startup fixture is created. No socket or production asset.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
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
    from production_fixtures import build
    from blender_mcp.production.animation.corrections import runtime, correction
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage

    class LocalBridge:
        def run(self, body, params=None):
            code = build_code(body, params)
            namespace = {}
            exec(code.rsplit("print(", 1)[0], namespace)
            return namespace["result"]

    build(output / "fixture")
    storage = Storage(output / "store")
    bridge = LocalBridge()
    from m31_fixture_support import register_open_fixture
    register_open_fixture(bridge, storage, output)
    namespace = {"params": {}}
    exec(runtime("foot_sliding"), namespace)
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
    def rejects(name, function):
        try:
            function()
        except ValueError:
            check(name, True)
        else:
            raise AssertionError(name + " was accepted")

    rig = bpy.data.objects["M1_Rig"]
    source = rig.animation_data.action
    options = {"controls": ["FootIK.L"], "confirm_contacts": True,
               "frame_start": 1, "frame_end": 10, "blend_frames": 3}
    curve = next(row[3] for row in namespace["action_curves"](source, None)
                 if row[3].data_path == 'pose.bones["FootIK.L"].location' and row[3].array_index == 1)
    first = next(point for point in curve.keyframe_points if point.co.x == 10)
    second = next(point for point in curve.keyframe_points if point.co.x == 11)
    first.interpolation = "BEZIER"
    first.handle_right_type = "FREE"
    second.handle_left_type = "FREE"
    first.handle_right = (10.333333, .15)
    second.handle_left = (10.666667, .15)
    curve.update()
    original = namespace["m2_snapshot"](source)
    rejects("BEZIER flight height cannot be silently linearized", lambda: namespace["m2_transform_proposal"](rig, source, "foot_sliding", options))
    check("rejected proposal leaves source exact", namespace["m2_snapshot"](source) == original)
    # RNA point references can be invalidated by curve.update(); reacquire them.
    for point in curve.keyframe_points:
        point.interpolation = "LINEAR"
    curve.update()

    bone = rig.pose.bones["Shin.L"]
    initial = bone.ik_stretch
    before = namespace["m2_context"](rig)
    bone.ik_stretch = .25 if initial != .25 else .5
    check("IK stretch configuration included in context", before != namespace["m2_context"](rig))
    bone.ik_stretch = initial
    constraint = next(c for c in bone.constraints if c.type == "IK")
    initial = constraint.use_stretch
    before = namespace["m2_context"](rig)
    constraint.use_stretch = not initial
    check("IK use_stretch included in context", before != namespace["m2_context"](rig))
    constraint.use_stretch = initial

    pole = bpy.data.objects.new("M2_ExternalPole", None)
    bpy.context.scene.collection.objects.link(pole)
    previous_target, previous_subtarget = constraint.pole_target, constraint.pole_subtarget
    constraint.pole_target, constraint.pole_subtarget = pole, ""
    before = namespace["m2_context"](rig)
    pole.location.x += .2
    check("external pole transform included in context", before != namespace["m2_context"](rig))
    constraint.pole_target, constraint.pole_subtarget = previous_target, previous_subtarget

    no_slide = namespace["m2_transform_proposal"](rig, source, "foot_sliding", dict(options, controls=["FootIK.R"]))
    copy_action = source.copy()
    slot = namespace["m2_slot"](rig)
    namespace["m2_assign"](rig, copy_action, slot)
    before = namespace["m2_snapshot"](copy_action)
    changes = namespace["m2_transform_apply"](rig, copy_action, "foot_sliding", no_slide, options)
    check("no failed contact leaves keys exactly unchanged", before == namespace["m2_snapshot"](copy_action))
    check("no failed contact reports no key mutations", changes["created_keys"] == 0 and changes["modified_keys"] == 0)
    namespace["m2_assign"](rig, source, slot)

    proposal = correction(bridge, storage, "adversarial-context", "foot_sliding", rig.name, "propose", options)
    initial = bone.ik_stretch
    bone.ik_stretch = .25 if initial != .25 else .5
    action_count = len(bpy.data.actions)
    rejects("changed IK configuration rejects apply before duplicate", lambda: correction(bridge, storage, "stale-context-apply", "foot_sliding", mode="apply", operation=proposal["operation"]))
    check("stale context allocates no candidate", len(bpy.data.actions) == action_count)
    bone.ik_stretch = initial

    proposal = correction(bridge, storage, "adversarial-switch", "foot_sliding", rig.name, "propose", options)
    preview = correction(bridge, storage, "adversarial-switch-preview", "foot_sliding", mode="preview", operation=proposal["operation"])
    check("valid preview numerically verified", preview["numeric_verification"] == "pass")
    unrelated = source.copy()
    unrelated.name = "M2_UserSelectedDifferentAction"
    namespace["m2_assign"](rig, unrelated, slot)
    rejects("revert does not override user's other Action", lambda: correction(bridge, storage, "adversarial-revert", "foot_sliding", mode="revert", operation=proposal["operation"]))
    check("failed revert restores user-selected Action", rig.animation_data.action == unrelated)
    namespace["m2_assign"](rig, source, slot)
    candidate = bpy.data.actions[preview["working_action"]]
    namespace["action_curves"](candidate, None)[0][3].keyframe_points[0].co.y += .1
    verified = correction(bridge, storage, "adversarial-changed-candidate", "foot_sliding", mode="verify", operation=proposal["operation"])
    check("manually changed candidate invalidates verification", verified["numeric_verification"] == "stale")
    check("failed verification keeps active source", rig.animation_data.action == source)

    report = {"status": "PASS", "blender": bpy.app.version_string, "checks": checks, "check_count": len(checks)}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("M2_ADVERSARIAL_RESULT=" + json.dumps(report))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender")
    parser.add_argument("--inside")
    parser.add_argument("--output")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else None)
    if args.inside:
        inside(Path(args.inside))
        return
    output = Path(args.output) if args.output else ARTIFACTS / ("m2adv-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    process = subprocess.run([args.blender, "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1", "--python", str(Path(__file__).resolve()), "--", "--inside", str(output)], text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=240)
    (output / "blender.log").write_text(process.stdout + process.stderr, encoding="utf-8")
    if process.returncode:
        raise RuntimeError(process.stdout + process.stderr)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    print(json.dumps({"status": report["status"], "check_count": report["check_count"], "output": str(output)}))


if __name__ == "__main__":
    main()
