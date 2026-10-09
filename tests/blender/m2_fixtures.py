"""Generate fourteen original M2 animation scenarios and a visible test biped.

Usage: blender --background --factory-startup --disable-autoexec --python
       tests/blender/m2_fixtures.py -- NEW_OUTPUT_DIRECTORY

All assets are procedural. A source M1 fixture is saved and checkpointed before
structural additions; its bytes remain unchanged. Existing outputs are refused.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).parent))
from production_fixtures import build as build_m1, _armature


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def curves(action):
    if action.is_action_layered:
        return [curve for layer in action.layers for strip in layer.strips
                for bag in strip.channelbags for curve in bag.fcurves]
    return list(action.fcurves)


def definitions():
    bones = [
        ("MotionBase", (0,0,0), (0,0,.18), None, False),
        ("Pelvis", (0,0,1.04), (0,0,1.22), "MotionBase", True),
        ("Chest", (0,0,1.22), (0,0,1.58), "Pelvis", True),
        ("Head", (0,0,1.58), (0,0,1.88), "Chest", True),
    ]
    for suffix, x in (("L", -.18), ("R", .18)):
        direction = -1 if x < 0 else 1
        bones.extend([
            ("UpperLeg."+suffix,(x,0,1.04),(x,-.13,.53),"Pelvis",True),
            ("LowerLeg."+suffix,(x,-.13,.53),(x,0,0),"UpperLeg."+suffix,True),
            ("Foot."+suffix,(x,0,0),(x,-.22,0),"LowerLeg."+suffix,True),
            ("Toe."+suffix,(x,-.22,0),(x,-.32,0),"Foot."+suffix,True),
            ("ContactControl."+suffix,(x,0,0),(x,-.22,0),None,False),
            ("PoleControl."+suffix,(x,-.9,.5),(x,-.9,.68),None,False),
            ("UpperArm."+suffix,(direction*.16,0,1.49),(direction*.42,0,1.18),"Chest",True),
            ("LowerArm."+suffix,(direction*.42,0,1.18),(direction*.45,-.05,.9),"UpperArm."+suffix,True),
            ("Hand."+suffix,(direction*.45,-.05,.9),(direction*.45,-.09,.76),"LowerArm."+suffix,True),
        ])
    return bones


def add_ik(rig, sides=("L","R")):
    for suffix in sides:
        solver = rig.pose.bones["LowerLeg."+suffix].constraints.new("IK")
        solver.name = "M2_TwoBoneIK."+suffix
        solver.target = rig
        solver.subtarget = "ContactControl."+suffix
        solver.pole_target = rig
        solver.pole_subtarget = "PoleControl."+suffix
        solver.chain_count = 2
        solver.use_stretch = False
        rotation = rig.pose.bones["Foot."+suffix].constraints.new("COPY_ROTATION")
        rotation.name = "M2_FootOrientation."+suffix
        rotation.target = rig
        rotation.subtarget = "ContactControl."+suffix
        rotation.target_space = "POSE"
        rotation.owner_space = "POSE"


def local_displacement(pose, delta):
    # All target controls and MotionBase are unparented; avoid changing rest axes.
    return pose.bone.matrix_local.to_3x3().inverted() @ Vector(delta)


def insert_location(pose, frame, delta):
    pose.location = local_displacement(pose,delta)
    pose.keyframe_insert(data_path="location",frame=frame)


def action_for(rig, name):
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    action["fixture_family"] = "Blender Production MCP M2"
    rig.animation_data_create()
    rig.animation_data.action = action
    for pose in rig.pose.bones:
        pose.location = (0,0,0)
        pose.rotation_mode = "QUATERNION"
        pose.rotation_quaternion = (1,0,0,0)
        pose.scale = (1,1,1)
        # First and last pose for every touched channel enables bounded retime.
        for frame in (1,31):
            pose.keyframe_insert(data_path="location",frame=frame)
            pose.keyframe_insert(data_path="rotation_quaternion",frame=frame)
    return action


def linearize(action):
    for curve in curves(action):
        for key in curve.keyframe_points:
            key.interpolation = "LINEAR"


def walk(rig, name, left_slip=0, right_slip=0, root_travel=-.12):
    action = action_for(rig,name)
    left = rig.pose.bones["ContactControl.L"]
    right = rig.pose.bones["ContactControl.R"]
    for frame, delta in [(1,(0,0,0)),(10,(left_slip,0,0)),(11,(left_slip,-.01,.06)),
                         (15,(left_slip,-.08,.14)),(19,(left_slip,-.15,.06)),
                         (20,(left_slip,-.16,0)),(31,(left_slip,-.16,0))]:
        insert_location(left,frame,delta)
    for frame, delta in [(1,(0,0,0)),(10,(right_slip,0,0)),(20,(right_slip,0,0)),
                         (21,(right_slip,-.01,.06)),(25,(right_slip,-.08,.14)),
                         (29,(right_slip,-.15,.06)),(30,(right_slip,-.16,0)),(31,(right_slip,-.16,0))]:
        insert_location(right,frame,delta)
    insert_location(rig.pose.bones["MotionBase"],1,(0,0,0))
    insert_location(rig.pose.bones["MotionBase"],31,(0,root_travel,0))
    for frame in (1,8,16,24,31):
        for suffix,sign in (("L",1),("R",-1)):
            pose = rig.pose.bones["UpperArm."+suffix]
            angle = math.radians(12)*math.sin((frame-1)/30*2*math.pi)*sign
            pose.rotation_quaternion = (math.cos(angle/2),math.sin(angle/2),0,0)
            pose.keyframe_insert(data_path="rotation_quaternion",frame=frame)
    linearize(action)
    return action


def duplicate(rig, source, name):
    action = source.copy()
    action.name = name
    action.use_fake_user = True
    rig.animation_data.action = action
    if action.slots:
        rig.animation_data.action_slot = action.slots[0]
    return action


def material(name, color):
    value = bpy.data.materials.new(name)
    value.diffuse_color = (*color,1)
    value.use_nodes = True
    shader = value.node_tree.nodes.get("Principled BSDF")
    if shader:
        shader.inputs["Base Color"].default_value = (*color,1)
        shader.inputs["Roughness"].default_value = .65
    return value


def segment_mesh(rig, bone_name, radius, depth, mat):
    bone = rig.data.bones[bone_name]
    head, tail = bone.head_local, bone.tail_local
    axis = (tail-head).normalized()
    cross = axis.cross(Vector((0,1,0)))
    if cross.length < .01:
        cross = axis.cross(Vector((1,0,0)))
    u = cross.normalized()*radius
    v = axis.cross(u).normalized()*depth
    # Procedural bevel-free octagonal cross-sections keep the visible proxy light.
    verts = []
    for center in (head+axis*.025,tail-axis*.025):
        for i in range(8):
            angle = i*math.pi/4
            verts.append(center+u*math.cos(angle)+v*math.sin(angle))
    faces = [tuple(range(7,-1,-1)),tuple(range(8,16))]
    faces.extend((i,(i+1)%8,(i+1)%8+8,i+8) for i in range(8))
    mesh = bpy.data.meshes.new("M2_Visible_"+bone_name)
    mesh.from_pydata(verts,[],faces)
    mesh.update()
    obj = bpy.data.objects.new("M2_Visible_"+bone_name,mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(mat)
    group = obj.vertex_groups.new(name=bone_name)
    group.add(list(range(len(verts))),1,"REPLACE")
    modifier = obj.modifiers.new("M2_Armature","ARMATURE")
    modifier.object = rig
    obj["fixture_only"] = True
    return obj


def build(output_dir):
    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("M2 fixture generation requires fresh headless factory-startup")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True,exist_ok=False)
    m1 = build_m1(output/"m1-source")
    m1_path = Path(m1["blend"])
    original_hash = digest(m1_path)
    checkpoint_dir = output/"checkpoint-before-m2-structure"
    checkpoint_dir.mkdir()
    checkpoint_path = checkpoint_dir/"scene.blend"
    before_source = bpy.data.filepath
    status = bpy.ops.wm.save_as_mainfile(filepath=str(checkpoint_path),copy=True,relative_remap=True)
    if "FINISHED" not in status or bpy.data.filepath != before_source:
        raise RuntimeError("Fixture checkpoint did not preserve original source")
    checkpoint = {"created_at":datetime.now(timezone.utc).isoformat(),"source":before_source,
        "description":"Before adding M2 synthetic rig, visible proxy meshes and scenario Actions",
        "operation":"m2_fixture_structure", "filename":"scene.blend", "sha256":digest(checkpoint_path)}
    (checkpoint_dir/"manifest.json").write_text(json.dumps(checkpoint,indent=2),encoding="utf-8")
    for obj in bpy.context.scene.objects:
        if obj.type not in {"CAMERA","LIGHT"}:
            obj.hide_render = True
            obj.hide_set(True)
    rig = _armature("M2_VisibleRig",definitions())
    rig["fixture_only"] = True
    rig["marker_ground_height_m"] = 0.0
    rig.show_in_front = True
    add_ik(rig)
    scene = bpy.context.scene
    scene.name = "M2_ControlledFixtures"
    scene.frame_start,scene.frame_end = 1,31
    scene.render.fps = 30
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scenarios = {}

    def record(key, action, target=rig, expected=None, description=""):
        action["scenario"] = key
        scenarios[key] = {"object":target.name,"action":action.name,"frame_range":[1,31],
                          "description":description,"expected":expected or {}}

    clean = walk(rig,"M2_CleanWalk")
    record("clean_walk",clean,expected={"sliding_first_contact_m":0.0},description="Stable planted markers followed by controlled swing phases")
    left = walk(rig,"M2_LeftSliding",left_slip=.018)
    record("left_sliding",left,expected={"left_sliding_m":.018,"right_sliding_m":0.0,"measurement_frames":[1,10]})
    bilateral = walk(rig,"M2_BilateralSliding",left_slip=.018,right_slip=.025)
    record("bilateral_sliding",bilateral,expected={"left_sliding_m":.018,"right_sliding_m":.025,"measurement_frames":[1,10]})
    transition = duplicate(rig,clean,"M2_IntentionalTransition")
    for frame,delta in [(1,(0,0,0)),(4,(0,0,0)),(5,(0,-.01,.01)),(6,(0,-.025,.04)),(7,(0,-.05,.09)),(10,(0,-.08,.14))]:
        insert_location(rig.pose.bones["ContactControl.L"],frame,delta)
    linearize(transition)
    record("intentional_contact_transition",transition,description="Intentional progressive takeoff; contact end must not be locked blindly")
    quaternion = duplicate(rig,clean,"M2_QuaternionDiscontinuity")
    hand = rig.pose.bones["Hand.L"]
    for frame,value in [(1,(1,0,0,0)),(15,(1,0,0,0)),(16,(-1,0,0,0)),(31,(-1,0,0,0))]:
        hand.rotation_quaternion = value
        hand.keyframe_insert(data_path="rotation_quaternion",frame=frame)
    linearize(quaternion)
    record("quaternion_discontinuity",quaternion,expected={"antipodal_frames":[15,16],"physical_keyed_angle_degrees":0})
    noise = duplicate(rig,clean,"M2_MocapNoise")
    pelvis = rig.pose.bones["Pelvis"]
    for frame in range(1,32):
        pelvis.location = (.003*(-1 if frame%2 else 1),0,0)
        pelvis.keyframe_insert(data_path="location",frame=frame)
    linearize(noise)
    record("mocap_noise",noise,expected={"noise_amplitude_m":.003},description="Deliberate small alternating pelvis noise; smoothing requires explicit tolerance")
    redundant = duplicate(rig,clean,"M2_RedundantKeys")
    for frame in range(1,32):
        pelvis.location = (0,0,0)
        pelvis.keyframe_insert(data_path="location",frame=frame)
    linearize(redundant)
    record("redundant_keys",redundant,expected={"flat_pelvis_keys_per_component":31})
    root_walk = duplicate(rig,clean,"M2_RootMotionWalk")
    record("root_motion_walk",root_walk,expected={"root_bone":"MotionBase","translation_xyz_m":[0,-.12,0]})
    in_place = walk(rig,"M2_InPlace",root_travel=0)
    record("in_place",in_place,expected={"root_displacement_m":0})
    ik_action = duplicate(rig,clean,"M2_IKFixture")
    record("ik_rig",ik_action,expected={"explicit_active_ik":2})
    fk_rig = _armature("M2_FKFixture",definitions())
    fk_rig.hide_render = True
    fk_rig.hide_set(True)
    fk = walk(fk_rig,"M2_FKWalk")
    for suffix in ("L","R"):
        for frame,angle in [(1,0),(16,.25 if suffix=="L" else -.25),(31,0)]:
            pose = fk_rig.pose.bones["UpperLeg."+suffix]
            pose.rotation_quaternion = (math.cos(angle/2),math.sin(angle/2),0,0)
            pose.keyframe_insert(data_path="rotation_quaternion",frame=frame)
    linearize(fk)
    record("fk_rig",fk,fk_rig,{"explicit_active_ik":0},"FK-only hierarchy; controls are not presumed to drive deform bones")
    mixed_rig = _armature("M2_MixedFixture",definitions())
    mixed_rig.hide_render = True
    mixed_rig.hide_set(True)
    add_ik(mixed_rig,sides=("L",))
    mixed = walk(mixed_rig,"M2_MixedWalk")
    record("mixed_ik_fk",mixed,mixed_rig,{"explicit_active_ik":1},"One explicit IK leg and one FK leg; no automatic conversion")
    balance = duplicate(rig,clean,"M2_BalanceIssue")
    for frame in (1,31):
        pelvis.location = (.6,0,0)
        pelvis.keyframe_insert(data_path="location",frame=frame)
    linearize(balance)
    record("balance_issue",balance,expected={"pelvis_offset_m":.6},description="Intentional support-proxy outlier, not a validated physical impossibility")
    fast = duplicate(rig,clean,"M2_IntentionalFast")
    arm = rig.pose.bones["LowerArm.R"]
    for frame in range(1,32):
        angle = math.radians(12*(frame-1))
        arm.rotation_quaternion = (math.cos(angle/2),0,math.sin(angle/2),0)
        arm.keyframe_insert(data_path="rotation_quaternion",frame=frame)
    linearize(fast)
    record("fast_valid",fast,expected={"relative_arm_angular_speed_deg_s":360},description="Intentional uniformly fast full arm rotation; not automatically a spike")
    rig.animation_data.action = left
    rig.animation_data.action_slot = left.slots[0]
    rig.hide_set(False)
    body = material("M2_Body",(.13,.37,.60))
    joints = material("M2_Joints",(.09,.13,.19))
    accent = material("M2_Accent",(.95,.39,.10))
    visible = []
    for bone in rig.data.bones:
        if not bone.use_deform:
            continue
        size = .06
        depth = .06
        mat = body
        if bone.name == "Pelvis":
            size,depth = .20,.12
        elif bone.name == "Chest":
            size,depth = .21,.11
        elif bone.name == "Head":
            size,depth = .12,.11
        elif bone.name.startswith(("Foot","Toe")):
            size,depth,mat = .07,.04,accent
        elif bone.name.startswith("Hand"):
            size,depth,mat = .045,.03,joints
        visible.append(segment_mesh(rig,bone.name,size,depth,mat).name)
    bpy.ops.mesh.primitive_plane_add(size=8,location=(0,0,-.045))
    floor = bpy.context.object
    floor.name = "M2_Ground"
    floor.data.materials.append(material("M2_GroundMaterial",(.12,.14,.17)))
    camera = scene.camera
    camera.location = (2.9,-5.3,2.25)
    camera.rotation_euler = (Vector((0,-.1,.9))-camera.location).to_track_quat("-Z","Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = 2.4
    scene.render.resolution_x,scene.render.resolution_y = 640,480
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 16
    scene.cycles.use_denoising = True
    light = next((obj for obj in scene.objects if obj.type=="LIGHT"),None)
    if light:
        light.data.energy = 1000
        light.location = (2,-3,4)
    scene.world.color = (.2,.2,.2)
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    scene.frame_set(7,subframe=.25)
    final = output/"m2-controlled-fixtures.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(final),relative_remap=True)
    if digest(m1_path) != original_hash:
        raise RuntimeError("Original M1 fixture changed")
    manifest = {"schema_version":1,"blender_version":bpy.app.version_string,"blend":str(final),"sha256":digest(final),
        "source_m1":str(m1_path),"source_m1_sha256":original_hash,"checkpoint":str(checkpoint_dir/"manifest.json"),
        "scene":scene.name,"visible_rig":rig.name,"visible_meshes":visible,"default_action":left.name,
        "markers":["ContactControl.L","ContactControl.R"],"root_bone":"MotionBase","pelvis_bone":"Pelvis",
        "fps":30,"frame_range":[1,31],"ground_height_m":0,"scenarios":scenarios,
        "notes":["Synthetic fixtures only; no external assets", "F-Curve full-range fixtures have endpoint keys on every animated channel", "Visible proxy geometry is diagnostic, not production character artwork"]}
    (output/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    print("M2_FIXTURE_MANIFEST="+str(output/"manifest.json"))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir")
    args = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
    build(parser.parse_args(args).output_dir)
