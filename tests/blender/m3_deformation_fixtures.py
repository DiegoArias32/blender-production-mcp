"""Procedural weighted shoulder/knee tubes; no external or production assets."""
FIXTURE = r'''
import bpy
import math

def dq_fixture_tube(label, x, defect_axis):
    data = bpy.data.armatures.new(label + "_Skeleton")
    rig = bpy.data.objects.new(label + "_Rig", data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    base = data.edit_bones.new("Q7_Base")
    base.head = (x, 0, 0)
    base.tail = (x, 0, 1)
    joint = data.edit_bones.new("Z9_Joint")
    joint.head = (x, 0, 1)
    joint.tail = (x, 0, 2)
    joint.parent = base
    joint.use_connect = True
    bpy.ops.object.mode_set(mode="OBJECT")
    vertices = []
    faces = []
    sides, rings = 16, 17
    for ring in range(rings):
        z = 2.0*ring/(rings-1)
        for side in range(sides):
            angle = 2*math.pi*side/sides
            vertices.append((x + .25*math.cos(angle), .25*math.sin(angle), z))
    for ring in range(rings-1):
        for side in range(sides):
            nxt = (side+1) % sides
            faces.append((ring*sides+side, ring*sides+nxt, (ring+1)*sides+nxt, (ring+1)*sides+side))
    faces.append(tuple(reversed(range(sides))))
    faces.append(tuple((rings-1)*sides+side for side in range(sides)))
    mesh_data = bpy.data.meshes.new(label + "_WeightedTube")
    mesh_data.from_pydata(vertices, [], faces)
    mesh_data.update()
    mesh = bpy.data.objects.new(label + "_Mesh", mesh_data)
    bpy.context.scene.collection.objects.link(mesh)
    first = mesh.vertex_groups.new(name="Q7_Base")
    second = mesh.vertex_groups.new(name="Z9_Joint")
    region = mesh.vertex_groups.new(name="JointRegion")
    for vertex in mesh_data.vertices:
        weight = max(0.0, min(1.0, (vertex.co.z - .5)))
        if weight < 1:
            first.add([vertex.index], 1-weight, "REPLACE")
        if weight > 0:
            second.add([vertex.index], weight, "REPLACE")
        if .5 <= vertex.co.z <= 1.5:
            region.add([vertex.index], 1, "REPLACE")
    arm = mesh.modifiers.new("WeightedSkin", "ARMATURE")
    arm.object = rig
    arm.use_deform_preserve_volume = False
    bone = rig.pose.bones["Z9_Joint"]
    bone.rotation_mode = "XYZ"
    for frame, angle in [(1, 0), (12, 20), (24, 45), (47, 180), (60, 0)]:
        bone.rotation_euler = (0, 0, 0)
        bone.rotation_euler[defect_axis] = math.radians(angle)
        bone.keyframe_insert(data_path="rotation_euler", frame=frame)
    bad = rig.animation_data.action
    bad.name = label + "_Collapse"
    bad.use_fake_user = True
    good = bad.copy()
    good.name = label + "_Valid"
    good.use_fake_user = True
    for layer in good.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    for point in curve.keyframe_points:
                        point.co.y *= 0.08
                        point.handle_left.y *= 0.08
                        point.handle_right.y *= 0.08
                        point.interpolation = "LINEAR"
    for layer in bad.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    for point in curve.keyframe_points:
                        point.interpolation = "LINEAR"
    flat = good.copy()
    flat.name = label + "_StaticValid"
    flat.use_fake_user = True
    for layer in flat.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    for point in curve.keyframe_points:
                        point.co.y = 0
                        point.handle_left.y = 0
                        point.handle_right.y = 0
    material = bpy.data.materials.new(label + "_Material")
    material.diffuse_color = (.14, .42, .82, 1) if defect_axis == 1 else (.85, .33, .12, 1)
    mesh.data.materials.append(material)
    rig.select_set(False)
    return {"rig": rig.name, "mesh": mesh.name, "bad_action": bad.name, "good_action": good.name,
            "flat_action": flat.name, "bone": bone.name, "defect_frame": 47, "vertices": len(vertices)}

def dq_create_fixtures():
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 60
    scene.render.fps = 30
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    fixtures = {"shoulder": dq_fixture_tube("M3_Shoulder", 0, 1),
                "knee": dq_fixture_tube("M3_Knee", 3, 0)}
    scene.frame_set(1)
    scene["production_fixture"] = "M3 controlled weighted deformation fixtures"
    return fixtures
'''
