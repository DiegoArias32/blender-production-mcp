"""Generate controlled, original M1 fixtures in a fresh headless Blender.

Run: blender --background --factory-startup --python production_fixtures.py -- OUTPUT_DIR
Never reads production assets, changes preferences, connects sockets, downloads
assets or overwrites an existing fixture. Blender 5.2.1 API verified locally.
"""

import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def _armature(name, definitions):
    data = bpy.data.armatures.new(name + "Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(rig)
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    for name, head, tail, parent, deform in definitions:
        bone = data.edit_bones.new(name)
        bone.head = head
        bone.tail = tail
        bone.use_deform = deform
        if parent:
            bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.show_in_front = True
    return rig


def _location_key(pose_bone, frame, armature_space_delta):
    # Unparented controller: convert armature displacement to rest-local axes.
    pose_bone.location = pose_bone.bone.matrix_local.to_3x3().inverted() @ Vector(armature_space_delta)
    pose_bone.keyframe_insert(data_path="location", frame=frame)


def _curves(action):
    if getattr(action, "is_action_layered", False):
        return [curve for layer in action.layers for strip in layer.strips
                for bag in strip.channelbags for curve in bag.fcurves]
    return list(action.fcurves)


def build(output_dir):
    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("Fixtures require a fresh headless --factory-startup process")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    blend_path = output_dir / "m1-controlled-fixtures.blend"
    manifest_path = output_dir / "m1-controlled-fixtures.json"
    if blend_path.exists() or manifest_path.exists():
        raise FileExistsError("Fixture output exists; choose a fresh output directory")
    scene = bpy.context.scene
    scene.name = "M1_ControlledFixtures"
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.render.fps = 30
    scene.frame_start = 1
    scene.frame_end = 31
    cube = bpy.data.objects.get("Cube")
    if cube is None:
        bpy.ops.mesh.primitive_cube_add()
        cube = bpy.context.object
    cube.name = "M1_Cube"
    cube.location = (2.4, 0, 0.3)
    cube.scale = (0.3, 0.3, 0.3)
    simple = _armature("M1_SimpleRig", [
        ("Base", (1.2, 0, 0), (1.2, 0, 0.5), None, True),
        ("Tip", (1.2, 0, 0.5), (1.2, 0, 1), "Base", True),
    ])
    definitions = [("Root", (0, 0, 1), (0, 0, 1.3), None, False)]
    for suffix, x in (("L", -0.25), ("R", 0.25)):
        definitions.extend([
            ("Hip." + suffix, (x, 0, 1.8), (x, 0.08, 0.9), "Root", True),
            ("Shin." + suffix, (x, 0.08, 0.9), (x, 0, 0), "Hip." + suffix, True),
            ("Foot." + suffix, (x, 0, 0), (x, -0.22, 0), "Shin." + suffix, True),
            ("Toe." + suffix, (x, -0.22, 0), (x, -0.35, 0), "Foot." + suffix, True),
            ("FootIK." + suffix, (x, 0, 0), (x, 0, 0.2), None, False),
            ("Pole." + suffix, (x, -1, 0.9), (x, -1, 1.1), None, False),
        ])
    definitions.append(("Twist", (0, 0, 1.5), (0, 0, 1.7), "Root", True))
    rig = _armature("M1_Rig", definitions)
    for suffix in ("L", "R"):
        constraint = rig.pose.bones["Shin." + suffix].constraints.new("IK")
        constraint.name = "M1_LegIK." + suffix
        constraint.target = rig
        constraint.subtarget = "FootIK." + suffix
        constraint.pole_target = rig
        constraint.pole_subtarget = "Pole." + suffix
        constraint.chain_count = 2
    left = rig.pose.bones["FootIK.L"]
    right = rig.pose.bones["FootIK.R"]
    for frame, delta in [(1, (0, 0, 0)), (10, (0.018, 0, 0)),
                         (11, (0.02, -0.05, 0.15)), (20, (0.04, -0.1, 0.15)),
                         (21, (0.04, -0.1, 0)), (31, (0.04, -0.1, 0))]:
        _location_key(left, frame, delta)
    for frame, delta in [(1, (0, 0, 0)), (10, (0.0022, 0, 0)),
                         (15, (0.0022, 0, 0)), (16, (0.02, -0.1, 0.15)),
                         (25, (0.02, -0.2, 0.15)), (26, (0.02, -0.2, 0)),
                         (31, (0.02, -0.2, 0))]:
        _location_key(right, frame, delta)
    root = rig.pose.bones["Root"]
    _location_key(root, 1, (0, 0, 0))
    _location_key(root, 31, (0.3, 0, 0))
    twist = rig.pose.bones["Twist"]
    twist.rotation_mode = "QUATERNION"
    for frame, quaternion in [(1, (1, 0, 0, 0)), (15, (1, 0, 0, 0)),
                              (16, (-1, 0, 0, 0)), (31, (-1, 0, 0, 0))]:
        twist.rotation_quaternion = quaternion
        twist.keyframe_insert(data_path="rotation_quaternion", frame=frame)
    rig.animation_data.action.name = "M1_Walk"
    for curve in _curves(rig.animation_data.action):
        for point in curve.keyframe_points:
            point.interpolation = "LINEAR"
    # A second slot exercises Blender 5 layered Actions without cross-slot mixing.
    cube.keyframe_insert(data_path="location", frame=1)
    cube.keyframe_insert(data_path="location", frame=31)
    cube.animation_data.action.name = "M1_CubeAction"
    if getattr(cube.animation_data.action, "is_action_layered", False):
        slot_probe = bpy.data.objects.new("M1_SlotProbe", None)
        bpy.context.collection.objects.link(slot_probe)
        slot_probe.animation_data_create()
        slot_probe.animation_data.action = cube.animation_data.action
        slot_probe.animation_data.action_slot = cube.animation_data.action.slots.new(
            id_type="OBJECT", name=slot_probe.name)
        slot_probe.rotation_mode = "QUATERNION"
        slot_probe.rotation_quaternion = (1, 0, 0, 0)
        slot_probe.keyframe_insert(data_path="rotation_quaternion", frame=1)
        slot_probe.rotation_quaternion = (-1, 0, 0, 0)
        slot_probe.keyframe_insert(data_path="rotation_quaternion", frame=2)
    skin_data = bpy.data.meshes.new("M1_DefectiveSkinData")
    vertices = [(x, y, z) for z in (0.3, 1.3) for y in (-0.12, 0.12) for x in (-0.4, 0.4)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    skin_data.from_pydata(vertices, [], faces)
    skin_data.update()
    skin = bpy.data.objects.new("M1_DefectiveSkin", skin_data)
    bpy.context.collection.objects.link(skin)
    modifier = skin.modifiers.new("M1_Armature", "ARMATURE")
    modifier.object = rig
    influences = ["Hip.L", "Shin.L", "Foot.L", "Toe.L", "Twist"]
    groups = [skin.vertex_groups.new(name=name) for name in influences]
    # Vertices 0,1 intentionally have no weights; vertex 2 has five influences.
    for group in groups:
        group.add([2], 0.2, "REPLACE")
    groups[0].add([3], 0.3, "REPLACE")
    groups[0].add([4, 5, 6, 7], 1.0, "REPLACE")
    material = bpy.data.materials.new("M1_Clay")
    material.diffuse_color = (0.14, 0.42, 0.65, 1.0)
    skin.data.materials.append(material)
    skin.shape_key_add(name="Basis")
    key = skin.shape_key_add(name="M1_TestShape")
    key.data[7].co.z += 0.1
    camera = bpy.data.objects.get("Camera")
    if camera:
        camera.location = (4, -7, 3.5)
        camera.rotation_euler = (Vector((0.6, 0, 0.8)) - camera.location).to_track_quat("-Z", "Y").to_euler()
        scene.camera = camera
    scene.frame_set(7, subframe=0.25)
    bpy.context.view_layer.objects.active = rig
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    rig.select_set(True)
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), check_existing=True)
    manifest = {
        "blender_version": bpy.app.version_string,
        "blend": str(blend_path), "scene": scene.name,
        "armature": rig.name, "simple_armature": simple.name,
        "action": rig.animation_data.action.name,
        "foot_markers": ["FootIK.L", "FootIK.R"],
        "expected_foot_test": {"frame_start": 1, "frame_end": 10,
            "ground_height_m": 0.0, "sliding_tolerance_m": 0.01,
            "left_translation_m": 0.018, "right_translation_m": 0.0022,
            "left_status": "fail", "right_status": "pass"},
        "quaternion_bone": "Twist", "quaternion_sign_flip_frames": [15, 16],
        "root_motion": {"bone": "Root", "frame_start": 1, "frame_end": 31,
                        "displacement_m": [0.3, 0, 0]},
        "defective_skin": {"object": skin.name, "unweighted_vertices": [0, 1],
                           "five_influence_vertex": 2, "unnormalized_vertex": 3},
        "original_frame": 7, "original_subframe": 0.25,
        "slot_fixture": {"action": "M1_CubeAction", "location_object": "M1_Cube",
                         "quaternion_object": "M1_SlotProbe"},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("M1_FIXTURE_MANIFEST=" + str(manifest_path))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir")
    arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    build(parser.parse_args(arguments).output_dir)
