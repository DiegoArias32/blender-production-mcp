"""Procedural body-animation fixtures; no production files or external assets.

The definition helpers are pure Python so unit tests can check the fixture contract
without pretending to verify Blender's solver. ``build`` is deliberately limited
to a fresh, unsaved factory-startup process. The real solver is checked by probe_m6.
The mesh is a segmented, rigidly weighted low-poly proxy, not a realistic skin.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


GROUND_HEIGHT_M = .08
REQUIRED_SCENARIOS = (
    "standard_humanoid", "heavy_humanoid", "asymmetric_creature",
    "two_bone_ik_legs", "fk_arms", "mixed_ik_fk", "root_motion_rig",
    "in_place_rig", "attack", "idle", "locomotion", "turn",
    "hit_reaction", "constrained_rig", "unsupported_rig",
)


def bone_definitions(variant="humanoid", extra_bones=0):
    """Return rest geometry with combined naming/hierarchy/spatial evidence.

    The explicit semantic mapping below is a fixture specification, never an
    assertion of caller-controlled confidence. Production computes confidence.
    Independent IK targets keep a planted foot fixed when the root moves.
    """
    if variant not in {"humanoid", "heavy", "asymmetric", "constrained", "unsupported"}:
        raise ValueError("Unknown controlled M6 fixture variant")
    if not isinstance(extra_bones, int) or isinstance(extra_bones, bool) or not 0 <= extra_bones <= 200:
        raise ValueError("extra_bones must be an integer in 0..200")
    if variant == "unsupported":
        return [
            ("joint_A", (0, 0, .08), (0, 0, .8), None, True),
            ("joint_B", (0, 0, .8), (.4, .25, 1.2), "joint_A", True),
        ]
    rows = [
        ("root", (0, 0, .08), (0, 0, .24), None, False),
        ("pelvis", (0, 0, 1.0), (0, 0, 1.14), "root", True),
        ("spine", (0, 0, 1.14), (0, 0, 1.30), "pelvis", True),
        ("chest", (0, 0, 1.30), (0, 0, 1.53), "spine", True),
        ("head", (0, 0, 1.53), (0, 0, 1.86), "chest", True),
    ]
    for side, sign in (("L", 1), ("R", -1)):
        x = sign * .165
        # A modest, observable knee bend avoids an ambiguous straight IK limb.
        hip, knee, ankle = (x, 0, 1.0), (x, -.055, .52), (x, 0, .08)
        shoulder = (sign * .19, 0, 1.47)
        elbow = (sign * .43, -.01, 1.19)
        wrist = (sign * .49, -.035, .92)
        hand_tail = (sign * .50, -.065, .77)
        if variant == "asymmetric" and side == "R":
            # Intentional distal mass changes BOTH rest geometry and visible mesh.
            elbow = (sign * .51, -.015, 1.17)
            wrist = (sign * .64, -.045, .79)
            hand_tail = (sign * .68, -.10, .56)
        rows.extend([
            ("thigh." + side, hip, knee, "pelvis", True),
            ("shin." + side, knee, ankle, "thigh." + side, True),
            ("foot." + side, ankle, (x, -.23, .08), "shin." + side, True),
            ("foot_ik." + side, ankle, (x, -.23, .08), None, False),
            ("pole." + side, (x, -.72, .52), (x, -.72, .67), None, False),
            ("upper_arm." + side, shoulder, elbow, "chest", True),
            ("forearm." + side, elbow, wrist, "upper_arm." + side, True),
            ("hand." + side, wrist, hand_tail, "forearm." + side, True),
        ])
    for index in range(extra_bones):
        x = .005 * ((index % 11) - 5)
        z = 1.58 + .002 * (index // 11)
        rows.append(("auxiliary_%03d" % index, (x, .04, z), (x, .04, z + .025), "head", False))
    return rows


def semantic_mapping(variant="humanoid"):
    if variant == "unsupported":
        return {}
    value = {name: name for name in ("root", "pelvis", "spine", "chest", "head")}
    for suffix, side in (("l", "L"), ("r", "R")):
        value.update({
            "upper_arm_" + suffix: "upper_arm." + side,
            "lower_arm_" + suffix: "forearm." + side,
            "hand_" + suffix: "hand." + side,
            "upper_leg_" + suffix: "thigh." + side,
            "lower_leg_" + suffix: "shin." + side,
            "foot_" + suffix: "foot." + side,
            "foot_control_" + suffix: "foot_ik." + side,
            "pole_" + suffix: "pole." + side,
        })
    return value


def scenario_specifications():
    specs = {}
    for name in REQUIRED_SCENARIOS:
        variant = {"heavy_humanoid": "heavy", "asymmetric_creature": "asymmetric",
                   "constrained_rig": "constrained", "unsupported_rig": "unsupported"}.get(name, "humanoid")
        specs[name] = {"variant": variant, "extra_bones": 0,
                       "profile": {"heavy": "HEAVY_HUMANOID", "asymmetric": "ASYMMETRIC_CREATURE"}.get(variant, "HUMAN"),
                       "root_mode": "IN_PLACE" if name == "in_place_rig" else "ROOT_MOTION",
                       "generation_type": name if name in {"attack", "idle", "locomotion", "turn", "hit_reaction"} else None,
                       "expected_compatibility": "unsupported" if variant in {"unsupported", "constrained"} else "supported"}
    specs["medium_rig"] = {"variant": "humanoid", "extra_bones": 41, "profile": "HUMAN",
                           "root_mode": "IN_PLACE", "generation_type": "idle", "expected_compatibility": "supported"}
    specs["large_rig"] = {"variant": "heavy", "extra_bones": 110, "profile": "HEAVY_HUMANOID",
                          "root_mode": "IN_PLACE", "generation_type": "idle", "expected_compatibility": "supported"}
    return specs


def _material(name, color):
    import bpy
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    if shader is not None:
        shader.inputs["Base Color"].default_value = (*color, 1)
        shader.inputs["Roughness"].default_value = .72
    return mat


def _armature(name, rows):
    import bpy
    data = bpy.data.armatures.new(name + "_Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    for bone_name, head, tail, parent, deform in rows:
        bone = data.edit_bones.new(bone_name)
        bone.head, bone.tail = head, tail
        bone.use_deform = deform
        if parent is not None:
            bone.parent = data.edit_bones[parent]
        bone.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    rig.show_in_front = True
    rig.display_type = "WIRE"
    rig["fixture_only"] = True
    rig["m6_fixture_family"] = "procedural_body_animation"
    rig["marker_ground_height_m"] = GROUND_HEIGHT_M
    for pose in rig.pose.bones:
        pose.rotation_mode = "QUATERNION"
        pose.ik_stretch = 0
    return rig


def _add_ik(rig):
    from mathutils import Vector
    for side in ("L", "R"):
        thigh = rig.data.bones["thigh." + side]
        shin = rig.pose.bones["shin." + side]
        solver = shin.constraints.new("IK")
        solver.name = "M6_TwoBoneIK." + side
        solver.target = solver.pole_target = rig
        solver.subtarget, solver.pole_subtarget = "foot_ik." + side, "pole." + side
        solver.chain_count = 2
        solver.use_tail = True
        solver.use_stretch = False
        solver.use_rotation = False
        solver.influence = 1.0
        # Compute the installed solver's pole orientation from actual rest axes.
        a, b = thigh.head_local, thigh.tail_local
        end = rig.data.bones["shin." + side].tail_local
        pole = rig.data.bones["pole." + side].head_local
        normal = (end - a).cross(pole - a)
        projected = normal.cross(b - a).normalized()
        x_axis = thigh.matrix_local.to_3x3() @ Vector((1, 0, 0))
        y_axis = (b - a).normalized()
        solver.pole_angle = math.atan2(y_axis.dot(x_axis.cross(projected)), x_axis.dot(projected))
        rotation = rig.pose.bones["foot." + side].constraints.new("COPY_ROTATION")
        rotation.name = "M6_FootOrientation." + side
        rotation.target, rotation.subtarget = rig, "foot_ik." + side
        rotation.target_space = rotation.owner_space = "POSE"
        rotation.influence = 1.0


def _proxy_mesh(rig, variant, materials):
    import bpy
    from mathutils import Vector
    vertices, faces, face_materials, groups = [], [], [], {}
    for bone in rig.data.bones:
        if not bone.use_deform:
            continue
        head, tail = bone.head_local.copy(), bone.tail_local.copy()
        axis = (tail - head).normalized()
        size = .075
        depth = .070
        if bone.name in {"pelvis", "spine", "chest"}:
            size = {"pelvis": .19, "spine": .17, "chest": .225}[bone.name]
            depth = .11
        elif bone.name == "head":
            size, depth = .135, .125
        elif bone.name.startswith("thigh."):
            size, depth = .095, .09
        elif bone.name.startswith("shin."):
            size, depth = .07, .07
        elif bone.name.startswith("hand."):
            size, depth = .065, .052
        elif bone.name.startswith("foot."):
            # For a horizontal -Y axis the fallback u is vertical and v is X.
            size, depth = .045, .08
            head.z += .045
            tail.z += .045
        if variant in {"heavy", "asymmetric"}:
            if bone.name in {"pelvis", "spine", "chest"}:
                size *= 1.28
                depth *= 1.28
            elif bone.name.startswith(("upper_arm.", "forearm.", "thigh.")):
                size *= 1.23
                depth *= 1.20
        if variant == "asymmetric" and bone.name in {"upper_arm.R", "forearm.R", "hand.R"}:
            size *= 1.80
            depth *= 1.90
        cross = axis.cross(Vector((0, 1, 0)))
        if cross.length < .001:
            cross = axis.cross(Vector((1, 0, 0)))
        u = cross.normalized() * size
        v = axis.cross(u).normalized() * depth
        start = len(vertices)
        # Three octagonal rings give a modest bevel without a modifier dependency.
        for t, radius in ((.025, .85), (.5, 1.0), (.975, .85)):
            center = head.lerp(tail, t)
            for index in range(8):
                angle = index * math.pi / 4
                vertices.append(tuple(center + radius * (u * math.cos(angle) + v * math.sin(angle))))
        component_faces = [tuple(start + index for index in range(7, -1, -1)),
                           tuple(start + 16 + index for index in range(8))]
        for ring in (0, 1):
            component_faces.extend((start + ring * 8 + index,
                                    start + ring * 8 + (index + 1) % 8,
                                    start + (ring + 1) * 8 + (index + 1) % 8,
                                    start + (ring + 1) * 8 + index) for index in range(8))
        faces.extend(component_faces)
        part_material = 2 if bone.name.startswith("foot.") else (1 if bone.name.startswith(("hand.", "forearm.")) else 0)
        if variant == "asymmetric" and bone.name.endswith(".R") and bone.name.startswith(("upper_arm", "forearm", "hand")):
            part_material = 3
        face_materials.extend([part_material] * len(component_faces))
        groups[bone.name] = list(range(start, len(vertices)))
    mesh = bpy.data.meshes.new(rig.name + "_Body_Data")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    body = bpy.data.objects.new(rig.name + "_Body", mesh)
    bpy.context.collection.objects.link(body)
    for material in materials:
        mesh.materials.append(material)
    for polygon, material_index in zip(mesh.polygons, face_materials):
        polygon.material_index = material_index
    for bone_name, indices in groups.items():
        body.vertex_groups.new(name=bone_name).add(indices, 1, "REPLACE")
    modifier = body.modifiers.new("M6_Fixture_Armature", "ARMATURE")
    modifier.object = rig
    modifier.use_deform_preserve_volume = True
    body["fixture_only"] = True
    body["m6_proxy_kind"] = "closed_segmented_rigid_skin"
    body["m6_intentional_asymmetry"] = variant == "asymmetric"
    return body


def _curves(action):
    if action.is_action_layered:
        return [curve for layer in action.layers for strip in layer.strips
                for bag in strip.channelbags for curve in bag.fcurves]
    return list(action.fcurves)


def _source_action(rig, name, end_frame):
    import bpy
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    action["fixture_only"] = True
    action["m6_source_immutable"] = True
    rig.animation_data_create()
    rig.animation_data.action = action
    for pose in rig.pose.bones:
        for frame in (1, end_frame):
            pose.keyframe_insert(data_path="location", frame=frame)
            pose.keyframe_insert(data_path="rotation_quaternion", frame=frame)
            pose.keyframe_insert(data_path="scale", frame=frame)
    for curve in _curves(action):
        for key in curve.keyframe_points:
            key.interpolation = "LINEAR"
    return action


def _stage(scene, materials):
    import bpy
    from mathutils import Vector
    mesh = bpy.data.meshes.new("M6_Ground_Data")
    mesh.from_pydata([(-4, -4, GROUND_HEIGHT_M), (4, -4, GROUND_HEIGHT_M),
                      (4, 4, GROUND_HEIGHT_M), (-4, 4, GROUND_HEIGHT_M)], [], [(0, 1, 2, 3)])
    mesh.update()
    ground = bpy.data.objects.new("M6_Ground", mesh)
    bpy.context.collection.objects.link(ground)
    mesh.materials.append(materials[-1])
    ground["fixture_only"] = True
    cameras = {}
    for view, location in (("FRONT", (0, -4.5, 1.1)), ("SIDE", (4.5, 0, 1.1)),
                           ("THREE_QUARTER", (3.0, -4.5, 1.7))):
        data = bpy.data.cameras.new("M6_Camera_" + view + "_Data")
        camera = bpy.data.objects.new("M6_Camera_" + view, data)
        bpy.context.collection.objects.link(camera)
        camera.location = location
        camera.rotation_euler = (Vector((0, 0, 1.0)) - camera.location).to_track_quat("-Z", "Y").to_euler()
        data.type = "ORTHO"
        data.ortho_scale = 2.6
        cameras[view] = camera.name
    scene.camera = bpy.data.objects[cameras["THREE_QUARTER"]]
    for name, location, energy, size in (("Key", (2, -3, 5), 500, 4), ("Fill", (-3, -1, 3), 250, 3)):
        data = bpy.data.lights.new("M6_" + name + "_Data", "AREA")
        data.energy, data.shape, data.size = energy, "DISK", size
        light = bpy.data.objects.new("M6_" + name, data)
        bpy.context.collection.objects.link(light)
        light.location = location
        light.rotation_euler = (Vector((0, 0, 1)) - light.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x, scene.render.resolution_y = 384, 448
    scene.render.resolution_percentage = 100
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.show_shadows = True
    scene.display.shading.show_cavity = True
    scene.display.shading.background_type = "WORLD"
    scene.world.color = (.035, .035, .045)
    return {"ground": ground.name, "ground_height_m": GROUND_HEIGHT_M, "cameras": cameras}


def activate_fixture(manifest, scenario="heavy_humanoid"):
    """Select a synthetic rig for a controlled preview, without changing its Action."""
    import bpy
    selected = manifest["scenarios"][scenario]
    for item in manifest["scenarios"].values():
        visible = item["object"] == selected["object"]
        bpy.data.objects[item["object"]].hide_set(not visible)
        bpy.data.objects[item["body"]].hide_set(not visible)
        bpy.data.objects[item["body"]].hide_render = not visible
        bpy.data.objects[item["object"]].select_set(visible)
    bpy.context.view_layer.objects.active = bpy.data.objects[selected["object"]]
    bpy.context.view_layer.update()
    return selected


def build(output_dir, fps=30, duration_seconds=2.4, suite=True, primary="heavy_humanoid"):
    """Build and save a fresh synthetic scene with immutable neutral source Actions."""
    import bpy
    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("M6 fixtures require fresh headless factory-startup")
    if fps not in {30, 60} or not 1 <= duration_seconds <= 10:
        raise ValueError("Controlled fixture FPS must be 30/60 and duration 1..10 seconds")
    specs = scenario_specifications()
    if primary not in specs:
        raise ValueError("Unknown primary fixture scenario")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    before, target = output / "before-m6-fixtures.blend", output / "m6-fixtures.blend"
    for path in (before, target, output / "fixtures.json"):
        if path.exists():
            raise FileExistsError(path)
    created = datetime.now(timezone.utc).isoformat()
    # Copy before structural changes; no existing or user .blend is opened.
    bpy.ops.wm.save_as_mainfile(filepath=str(before), copy=True)
    checkpoint = {"schema": 1, "source": None, "created_at": created,
                  "description": "Before adding M6 synthetic rigs, meshes, source Actions and preview stage",
                  "operation": "m6_fixture_structure", "filepath": str(before),
                  "sha256": hashlib.sha256(before.read_bytes()).hexdigest()}
    (output / "checkpoint-before-fixtures.json").write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    scene = bpy.context.scene
    scene.name = "M6_ControlledBodyAnimation"
    scene.frame_start, scene.frame_end = 1, 1 + round(fps * duration_seconds)
    scene.render.fps, scene.render.fps_base = fps, 1.0
    scene.unit_settings.system, scene.unit_settings.scale_length = "METRIC", 1.0
    for obj in tuple(scene.objects):
        # These objects belong to this unsaved factory-startup fixture process.
        bpy.data.objects.remove(obj, do_unlink=True)
    materials = [
        _material("M6_Body", (.12, .43, .65)), _material("M6_Hands", (.32, .70, .82)),
        _material("M6_Feet", (.88, .44, .09)), _material("M6_IntentionalMass", (.61, .15, .12)),
        _material("M6_GroundMaterial", (.075, .082, .096)),
    ]
    stage = _stage(scene, materials)
    scenarios = {}
    for scenario, spec in specs.items():
        if not suite and scenario != primary:
            continue
        label = "M6_" + scenario + "_Rig"
        rig = _armature(label, bone_definitions(spec["variant"], spec["extra_bones"]))
        if spec["variant"] != "unsupported":
            _add_ik(rig)
        if spec["variant"] == "constrained":
            limit = rig.pose.bones["chest"].constraints.new("LIMIT_ROTATION")
            limit.name = "M6_UnsupportedArbitraryConstraint"
            limit.use_limit_x, limit.min_x, limit.max_x = True, -.10, .10
            limit.owner_space = "LOCAL"
        body = _proxy_mesh(rig, spec["variant"], materials[:4])
        action = _source_action(rig, "M6_" + scenario + "_Source", scene.frame_end)
        rig["m6_fixture_profile"] = spec["profile"]
        if spec["variant"] == "asymmetric":
            rig["m6_intentional_mass_side"] = "right"
            rig["m6_intentional_mass_ratio"] = 1.8
        scenarios[scenario] = dict(spec, object=rig.name, body=body.name, action=action.name,
                                  mapping=semantic_mapping(spec["variant"]),
                                  bones=len(rig.data.bones), deform_bones=sum(b.use_deform for b in rig.data.bones),
                                  mesh_vertices=len(body.data.vertices), mesh_triangles=sum(p.loop_total - 2 for p in body.data.polygons),
                                  ground_height_m=GROUND_HEIGHT_M,
                                  contact_markers={"left": {"bone": "foot.L", "local_point": [0, 0, 0]},
                                                   "right": {"bone": "foot.R", "local_point": [0, 0, 0]}} if spec["variant"] != "unsupported" else {},
                                  source_action_role="immutable_fixture_source")
    manifest = {"schema": 1, "fixture_only": True, "created_at": created,
                "blender": bpy.app.version_string, "fps": fps,
                "frame_range": [scene.frame_start, scene.frame_end],
                "scene": scene.name, "blend": str(target), "checkpoint": checkpoint,
                "scenarios": scenarios, "stage": stage,
                "forward_axis": "-Y",
                "limitations": ["Segmented rigid skin proxy; not a realistic deformation or production-character test",
                                "Only simple same-armature two-bone IK legs and unconstrained FK arms",
                                "Ground marker is foot-bone head on a horizontal plane at 0.08 metres"]}
    activate_fixture(manifest, primary)
    scene.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    manifest["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "fixtures.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
