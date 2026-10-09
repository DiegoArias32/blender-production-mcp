"""Run in a disposable --background --factory-startup --disable-autoexec Blender.

This validates actual RNA/IK behavior and read-only guarantees, without sockets,
user files, GUI sessions, or third-party Python packages inside Blender.
"""

import ast
import importlib.util
import json
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("production_safe_mode_probe", ROOT / "src/blender_mcp/safe_mode.py")
SAFE_MODE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SAFE_MODE)


def payload(path, name):
    module = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in statement.targets):
            return ast.literal_eval(statement.value)
    raise AssertionError(f"Missing payload {name}")


def run(script, params):
    source = "params = " + repr(params) + "\n" + script
    SAFE_MODE.validate_code(source)
    namespace = {}
    exec(source, namespace)
    return namespace["result"]


SCENE = payload("src/blender_mcp/production/inspect/__init__.py", "SCENE_SCRIPT")
ARMATURE = payload("src/blender_mcp/production/inspect/__init__.py", "ARMATURE_SCRIPT")
RIG = payload("src/blender_mcp/production/rig/__init__.py", "RIG_SCRIPT")

# Factory-startup cube is the simple geometry fixture.
assert bpy.data.objects["Cube"].type == "MESH"
data = bpy.data.armatures.new("ProbeData")
armature = bpy.data.objects.new("ProbeRig", data)
bpy.context.collection.objects.link(armature)
bpy.context.view_layer.objects.active = armature
armature.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
specs = [
    ("J1", (1, 0, 3), (1, 0, 2), None, True),
    ("J2", (1, 0, 2), (1, .1, 1), "J1", True),
    ("K1", (-1, 0, 3), (-1, 0, 2), None, True),
    ("K2", (-1, 0, 2), (-1, .1, 1), "K1", True),
    ("E", (1, .1, 1), (1, .5, 1), None, False),
    ("P", (1, 1, 2), (1, 1, 2.4), None, False),
    ("A", (0, 0, 3), (0, 0, 4), None, True),
    ("B", (0, 0, 4), (0, 0, 5), "A", True),
]
for name, head, tail, parent, deform in specs:
    bone = data.edit_bones.new(name)
    bone.head, bone.tail = head, tail
    bone.use_deform = deform
    if parent:
        bone.parent = data.edit_bones[parent]
bpy.ops.object.mode_set(mode="OBJECT")
ik = armature.pose.bones["J2"].constraints.new("IK")
ik.target, ik.subtarget = armature, "E"
ik.pole_target, ik.pole_subtarget = armature, "P"
ik.chain_count = 2
# Read a driver without creating a driver through the MCP guard.
driver_curve = armature.driver_add('pose.bones["E"].location', 0)
driver_curve.driver.expression = "0.0"
bpy.context.view_layer.update()

before = run(SCENE, {"full": True})
arm = run(ARMATURE, {"armature": "ProbeRig", "offset": 0, "limit": 100})
rig = run(RIG, {"armature": "ProbeRig", "offset": 0, "limit": 100})
page = run(ARMATURE, {"armature": "ProbeRig", "offset": 2, "limit": 2})
scene_page = run(SCENE, {"offset": 0, "limit": 1})
cube = run(SCENE, {"object_name": "Cube", "offset": 0, "limit": 100})
cube_full = run(SCENE, {"object_name": "Cube", "offset": 0, "limit": 100, "detail": "full"})
after = run(SCENE, {"full": True})
assert before == after, "Inspection modified summarized scene data"
assert arm["totals"]["bones"] == 8
assert arm["totals"]["deform_bones"] == 6
assert arm["totals"]["drivers"] == 1
assert rig["ik_chains"][0]["bones_tip_to_root"] == ["J2", "J1"]
assert rig["ik_chains"][0]["active"] is True
assert rig["pole_targets"][0]["bone"] == "P"
assert {item["name"] for item in rig["control_candidates"]} == {"E", "P"}
assert any(chain["bones_root_to_tip"] == ["A", "B"] for chain in rig["fk_chain_candidates"])
assert any(pair["positive_x"] == "J1" and pair["negative_x"] == "K1" for pair in rig["symmetry_pairs"])
assert len(page["bones"]) == 2 and page["pagination"]["totals"]["bones"] == 8
assert len(scene_page["objects"]) == 1 and scene_page["pagination"]["total"] == 4
assert cube["objects"]["Cube"]["geometry"]["vertices"] == 8
assert cube["totals"]["objects"] == 1
assert cube["detail"] == "summary" and cube_full["detail"] == "full"
assert "matrix_world" not in cube["objects"]["Cube"] and "location" not in cube["objects"]["Cube"]
assert "rotation_quaternion" not in cube["objects"]["Cube"] and "modifiers" not in cube["objects"]["Cube"]
assert cube["objects"]["Cube"]["modifier_count"] == len(bpy.data.objects["Cube"].modifiers)
assert cube_full["objects"]["Cube"] == before["objects"]["Cube"]
assert "matrix_world" in before["objects"]["Cube"] and "detail" not in before
for script in [ARMATURE, RIG]:
    try:
        run(script, {"armature": "Cube", "offset": 0, "limit": 100})
    except ValueError:
        pass
    else:
        raise AssertionError("Non-armature target accepted")
print("PRODUCTION_INSPECT_TEST=" + json.dumps({
    "status": "passed", "blender": bpy.app.version_string, "assertions": 23,
    "fixtures": ["cube", "armature", "IK_chain", "pole_target", "FK_candidate", "driver", "symmetry"],
    "bones": arm["totals"], "rig_issues": rig["issues"],
    "read_only_snapshot_match": before == after,
}))
