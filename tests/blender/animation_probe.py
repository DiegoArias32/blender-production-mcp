"""Run real Blender animation checks in a fresh temporary factory-startup scene.

Usage from the repository venv:
    python tests/blender/animation_probe.py --blender PATH_TO_BLENDER

This launches isolated background processes only. No socket, production file,
user preference or persistent addon is required. Temporary outputs are cleaned
after the checks. Failures include Blender's actual stdout/stderr.
"""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

from blender_mcp.production.animation import analyze, detect_foot_sliding, detect_quaternion_flips
from blender_mcp.safe_mode import validate_code


class ProbeBridge:
    def __init__(self):
        self.parts = ["import bpy, json"]
        self.checks = []

    def run(self, body, params):
        code = "params = " + repr(params) + "\n" + body
        validate_code(code)
        self.parts.append(code)
        return {}

    def check(self, name, expression):
        self.parts.append("assert " + expression + ", " + repr(name))
        self.checks.append(name)

    def state_check(self):
        self.check("frame and subframe restored", "bpy.context.scene.frame_current == 7 and abs(bpy.context.scene.frame_subframe - 0.25) < 1e-6")


def build_probe():
    bridge = ProbeBridge()
    bridge.check("simple armature contains two bones", 'bpy.data.objects["M1_SimpleRig"].type == "ARMATURE" and len(bpy.data.objects["M1_SimpleRig"].data.bones) == 2')
    bridge.check("simple armature hierarchy is Base to Tip", 'bpy.data.objects["M1_SimpleRig"].data.bones["Base"].parent is None and bpy.data.objects["M1_SimpleRig"].data.bones["Tip"].parent.name == "Base"')
    bridge.parts.append('''
fixture_scene = bpy.context.scene
fixture_rig = bpy.data.objects["M1_Rig"]
fixture_frame = fixture_scene.frame_current
fixture_subframe = fixture_scene.frame_subframe
fixture_root_positions = []
try:
    for fixture_sample_frame in (1, 31):
        fixture_scene.frame_set(fixture_sample_frame)
        fixture_evaluated = fixture_rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        fixture_root_position = fixture_evaluated.matrix_world @ fixture_evaluated.pose.bones["Root"].head
        fixture_root_positions.append(fixture_root_position.copy() * fixture_scene.unit_settings.scale_length)
finally:
    fixture_scene.frame_set(fixture_frame, subframe=fixture_subframe)
fixture_root_delta = fixture_root_positions[1] - fixture_root_positions[0]
fixture_skin = bpy.data.objects["M1_DefectiveSkin"]
fixture_deform_names = {bone.name for bone in fixture_rig.data.bones if bone.use_deform}
fixture_deform_groups = {group.index for group in fixture_skin.vertex_groups if group.name in fixture_deform_names}
fixture_weights = {vertex.index: [weight.weight for weight in vertex.groups
                    if weight.group in fixture_deform_groups and weight.weight > 0]
                   for vertex in fixture_skin.data.vertices}
''')
    bridge.check("root bone world displacement is 0.3 meters along X", 'abs(fixture_root_delta.x - 0.3) < 1e-6 and abs(fixture_root_delta.y) < 1e-6 and abs(fixture_root_delta.z) < 1e-6')
    bridge.state_check()
    bridge.check("skin is bound to the fixture rig", 'any(modifier.type == "ARMATURE" and modifier.object == fixture_rig for modifier in fixture_skin.modifiers)')
    bridge.check("defective skin has unweighted vertices 0 and 1", 'sorted(index for index, weights in fixture_weights.items() if not weights) == [0, 1]')
    bridge.check("defective skin vertex 2 has five deform influences", 'len(fixture_weights[2]) == 5 and all(abs(weight - 0.2) < 1e-6 for weight in fixture_weights[2])')
    bridge.check("defective skin vertex 3 has unnormalized weight total 0.3", 'abs(sum(fixture_weights[3]) - 0.3) < 1e-6')
    bridge.check("test shape key and Basis exist", 'fixture_skin.data.shape_keys is not None and fixture_skin.data.shape_keys.key_blocks.get("Basis") is not None and fixture_skin.data.shape_keys.key_blocks.get("M1_TestShape") is not None')
    bridge.check("test shape key contains the intended deformation", 'abs(fixture_skin.data.shape_keys.key_blocks["M1_TestShape"].data[7].co.z - fixture_skin.data.shape_keys.key_blocks["Basis"].data[7].co.z - 0.1) < 1e-6')
    analyze(bridge, object_name="M1_Rig")
    bridge.check("layered action channels", 'result["metrics"]["channel_count"] == 13')
    bridge.check("layered action keys", 'result["metrics"]["key_count"] == 61')
    analyze(bridge, object_name="M1_Cube")
    bridge.check("first slot filtered", 'result["metrics"]["channel_count"] == 3')
    analyze(bridge, object_name="M1_SlotProbe")
    bridge.check("second slot filtered", 'result["metrics"]["channel_count"] == 4')
    analyze(bridge, action_name="M1_CubeAction")
    bridge.check("all slots included for named action", 'result["metrics"]["channel_count"] == 7')
    analyze(bridge, object_name="M1_Rig", offset=10, limit=2)
    bridge.check("channel pagination", 'len(result["channels"]) == 2 and result["pagination"]["next_offset"] == 12')
    detect_quaternion_flips(bridge, object_name="M1_Rig")
    bridge.check("known sign discontinuity", 'result["metrics"]["sign_discontinuity_count"] == 1')
    bridge.check("antipodal signs have no orientation jump", 'result["metrics"]["orientation_jump_count"] == 0')
    bridge.check("correct adjacent key frames", 'result["groups"][0]["sign_discontinuities"][0]["frame_start"] == 15.0 and result["groups"][0]["sign_discontinuities"][0]["frame_end"] == 16.0')
    detect_quaternion_flips(bridge, object_name="M1_Cube")
    bridge.check("another slot's quaternion does not leak", 'result["metrics"]["quaternion_group_count"] == 0')
    detect_quaternion_flips(bridge, object_name="M1_SlotProbe")
    bridge.check("second slot quaternion detected", 'result["metrics"]["sign_discontinuity_count"] == 1')
    detect_foot_sliding(bridge, "M1_Rig", bones=["FootIK.L", "FootIK.R"], frame_start=1, frame_end=10)
    bridge.check("left measured 1.8 cm", 'abs(result["feet"][0]["max_sliding_m"] - 0.018) < 1e-6')
    bridge.check("right measured 0.22 cm", 'abs(result["feet"][1]["max_sliding_m"] - 0.0022) < 1e-6')
    bridge.check("left fails tolerance", 'result["feet"][0]["status"] == "fail"')
    bridge.check("right passes tolerance", 'result["feet"][1]["status"] == "pass"')
    bridge.check("floating-point boundary does not add false frame", 'result["feet"][0]["contacts"][0]["problematic_frames"] == [7, 8, 9, 10]')
    bridge.state_check()
    bridge.parts.append("bpy.context.scene.unit_settings.scale_length = 0.01")
    detect_foot_sliding(bridge, "M1_Rig", bones=["FootIK.L"], frame_start=1, frame_end=10)
    bridge.check("physical scene-unit conversion", 'abs(result["feet"][0]["max_sliding_m"] - 0.00018) < 1e-7')
    bridge.check("centimeter scene scale changes physical verdict", 'result["feet"][0]["status"] == "pass"')
    bridge.parts.append("bpy.context.scene.unit_settings.scale_length = 1.0")
    bridge.parts.append('bpy.data.objects["M1_Rig"].location = (1, 2, 3)')
    detect_foot_sliding(bridge, "M1_Rig", bones=["FootIK.L"], frame_start=1, frame_end=10, ground_height_m=3.0)
    bridge.check("evaluated world-space object translation", 'abs(result["feet"][0]["max_sliding_m"] - 0.018) < 1e-6 and result["feet"][0]["contact_count"] == 1')
    bridge.parts.append('bpy.data.objects["M1_Rig"].location = (0, 0, 0)')
    bridge.state_check()
    detect_foot_sliding(bridge, "M1_Rig", frame_start=1, frame_end=10)
    bridge.check("IK control candidates detected", '{"FootIK.L", "FootIK.R"}.issubset({item["bone"] for item in result["feet"]})')
    bridge.check("heuristic confidence remains advisory", 'all(item["confidence"] < 1 for item in result["feet"])')
    bridge.state_check()
    # Fault injection after frame_set proves the finally block runs mid-sample.
    failing = ProbeBridge()
    detect_foot_sliding(failing, "M1_Rig", bones=["FootIK.L"], frame_start=1, frame_end=10)
    failing_code = failing.parts[-1].replace(
        "position = evaluated.matrix_world @ pose_bone.head",
        'if frame == 3:\n                    raise RuntimeError("injected evaluation failure")\n                position = evaluated.matrix_world @ pose_bone.head')
    validate_code(failing_code)
    bridge.parts.append("injected_error_observed = False\ntry:\n" +
                        "\n".join("    " + line for line in failing_code.splitlines()) +
                        '\nexcept RuntimeError as injected_error:\n    injected_error_observed = str(injected_error) == "injected evaluation failure"')
    bridge.check("mid-sampling error is real and propagated", "injected_error_observed")
    bridge.state_check()
    bridge.parts.append('print("ANIMATION_PROBE_RESULT=" + json.dumps({"status": "passed", "checks": ' + repr(bridge.checks) + '}))')
    return "\n".join(bridge.parts), bridge.checks


def run(blender):
    started = time.perf_counter()
    script, checks = build_probe()
    fixture_builder = Path(__file__).with_name("production_fixtures.py")
    with tempfile.TemporaryDirectory(prefix="blender-production-animation-probe-") as temporary:
        output = Path(temporary).resolve()
        script_path = output / "probe.py"
        script_path.write_text(script, encoding="utf-8")
        fixture = output / "m1-controlled-fixtures.blend"
        commands = [
            [str(blender), "--background", "--factory-startup", "--python-exit-code", "1",
             "--python", str(fixture_builder), "--", str(output)],
            [str(blender), "--background", "--factory-startup", str(fixture),
             "--python-exit-code", "1", "--python", str(script_path)],
        ]
        for command in commands:
            process = subprocess.run(command, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=180)
            if process.returncode:
                raise RuntimeError(f"Blender exited {process.returncode}\n{process.stdout}\n{process.stderr}")
        if "ANIMATION_PROBE_RESULT=" not in process.stdout:
            raise RuntimeError("Missing Blender verification marker\n" + process.stdout)
        report = {"status": "passed", "check_count": len(checks), "checks": checks,
                  "elapsed_seconds": round(time.perf_counter() - started, 3)}
        print(json.dumps(report, indent=2))
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", required=True, type=Path)
    run(parser.parse_args().blender)
