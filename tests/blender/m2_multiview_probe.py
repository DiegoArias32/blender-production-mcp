"""Run safe QA against a generated humanoid fixture in isolated Blender.

No socket, user project, downloaded asset or persistent preferences are used.
Artifacts are retained under a unique directory for actual visual inspection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
# M3.1: BLENDER_PRODUCTION_TEST_ARTIFACTS moves large probe output out of the repository
# (default unchanged: <repo>/test-artifacts).
ARTIFACTS = Path(os.environ.get('BLENDER_PRODUCTION_TEST_ARTIFACTS') or Path(__file__).resolve().parents[2] / 'test-artifacts')
from uuid import uuid4


def create_humanoid():
    """Simple weighted prisms make silhouettes/pose differences visible."""
    import bpy
    import mathutils
    scene = bpy.context.scene
    scene.name = "M2_VisualQA_ControlledFixture"
    scene.frame_start, scene.frame_end = 1, 9
    scene.render.fps = 24
    cube = bpy.data.objects.get("Cube")
    if cube:
        bpy.data.objects.remove(cube,do_unlink=True)
    armature = bpy.data.armatures.new("M2_QA_HumanoidData")
    rig = bpy.data.objects.new("M2_QA_Humanoid",armature)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    specs = [
        ("center",(0,0,0.9),(0,0,1.1),None,0.16),
        ("torso",(0,0,1.1),(0,0,1.55),"center",0.22),
        ("head",(0,0,1.55),(0,0,1.87),"torso",0.14),
        ("left_arm",(-0.22,0,1.5),(-0.52,0,1.05),"torso",0.065),
        ("right_arm",(0.22,0,1.5),(0.52,0,1.05),"torso",0.065),
        ("left_thigh",(-0.13,0,0.95),(-0.13,0,0.52),"center",0.085),
        ("right_thigh",(0.13,0,0.95),(0.13,0,0.52),"center",0.085),
        ("left_shin",(-0.13,0,0.52),(-0.13,0,0.1),"left_thigh",0.06),
        ("right_shin",(0.13,0,0.52),(0.13,0,0.1),"right_thigh",0.06),
        ("left_foot",(-0.13,0,0.1),(-0.13,-0.22,0.1),"left_shin",0.07),
        ("right_foot",(0.13,0,0.1),(0.13,-0.22,0.1),"right_shin",0.07),
    ]
    for name,head,tail,parent,radius in specs:
        bone = armature.edit_bones.new(name)
        bone.head,bone.tail = head,tail
        if parent:
            bone.parent = armature.edit_bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    for index,(name,head,tail,parent,radius) in enumerate(specs):
        a,b = mathutils.Vector(head),mathutils.Vector(tail)
        center = (a+b)/2
        direction = (b-a).normalized()
        rotation = direction.to_track_quat("Z","Y")
        length = (b-a).length
        vertices = [tuple(center+rotation@mathutils.Vector((x*radius,y*radius,z*length/2)))
                    for x,y,z in [(-1,-1,-1),(-1,1,-1),(1,1,-1),(1,-1,-1),
                                  (-1,-1,1),(-1,1,1),(1,1,1),(1,-1,1)]]
        mesh = bpy.data.meshes.new("M2_QA_"+name)
        mesh.from_pydata(vertices,[],[(0,1,2,3),(4,7,6,5),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0)])
        obj = bpy.data.objects.new("M2_QA_"+name,mesh)
        scene.collection.objects.link(obj)
        obj.parent = rig
        group = obj.vertex_groups.new(name=name)
        group.add(list(range(8)),1.0,"REPLACE")
        modifier = obj.modifiers.new("M2_QA_Deform","ARMATURE")
        modifier.object = rig
        material = bpy.data.materials.new("M2_QA_Color_"+name)
        material.diffuse_color = (0.10,0.35,0.72,1) if index%2 else (0.8,0.3,0.08,1)
        obj.data.materials.append(material)
    rig.animation_data_create()
    for name,angle in [("M2_QA_Before",0.12),("M2_QA_After",0.58)]:
        action = bpy.data.actions.new(name)
        rig.animation_data.action = action
        bone = rig.pose.bones["left_arm"]
        bone.rotation_mode = "XYZ"
        for frame,value in [(1,0),(5,angle),(9,0)]:
            bone.rotation_euler = (0,0,value)
            bone.keyframe_insert("rotation_euler",frame=frame,group=bone.name)
    rig.animation_data.action = bpy.data.actions["M2_QA_Before"]
    # A channel absent from the original Action can retain evaluated values
    # when Actions are switched. QA must preserve its original raw state.
    rig.animation_data.action = bpy.data.actions["M2_QA_After"]
    constraint = rig.pose.bones["left_arm"].constraints.new("LIMIT_ROTATION")
    constraint.name = "M2_QA_PreservedConstraint"
    for frame,value in [(1,0.0),(5,0.04),(9,0.06)]:
        rig.pose.bones["right_thigh"].location.x = value
        rig.pose.bones["right_thigh"].keyframe_insert("location",frame=frame,index=0)
        rig["qa_custom_channel"] = 20.0+value*100
        rig.keyframe_insert('["qa_custom_channel"]',frame=frame)
        constraint.influence = 0.5+value
        constraint.keyframe_insert("influence",frame=frame)
    rig.animation_data.action = bpy.data.actions["M2_QA_Before"]
    rig.pose.bones["right_thigh"].location.x = 0
    rig["qa_custom_channel"] = 17.0
    constraint.influence = 0.25
    scene.frame_set(3,subframe=0.25)
    return rig


def inside(output):
    import bpy
    import types
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"))
    # Upstream package __init__ eagerly imports MCP/Pydantic. Blender does not
    # need that host environment to exercise production payloads and bpy.
    package = types.ModuleType("blender_mcp")
    package.__path__ = [str(Path(__file__).resolve().parents[2]/"src"/"blender_mcp")]
    sys.modules["blender_mcp"] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage, sha256
    from blender_mcp.production.qa import multiview
    from blender_mcp.production.qa.png_montage import _decode

    class LocalBridge:
        """Execute exactly the safe-mode validated payload, with no socket."""
        fail_render = False

        def run(self,body,params):
            if self.fail_render and body == multiview._RENDER:
                body = body.replace('            preview = mv_preview(',
                                    '            raise RuntimeError("Injected controlled render failure")\n            preview = mv_preview(')
            code = build_code(body,params)
            namespace = {}
            exec(compile(code,"<production-safe-payload>","exec"),namespace)
            return json.loads(json.dumps(namespace["result"]))

    rig = create_humanoid()
    original_action = rig.animation_data.action
    original_slot = rig.animation_data.action_slot
    original_camera = bpy.context.scene.camera
    original_unkeyed = (rig.pose.bones["right_thigh"].location.x,rig["qa_custom_channel"],
                        rig.pose.bones["left_arm"].constraints["M2_QA_PreservedConstraint"].influence)
    original_names = sorted(bpy.data.objects.keys())
    original_action_count = len(bpy.data.actions)
    original_slots = [(a.name,[(s.identifier,s.handle) for s in a.slots]) for a in bpy.data.actions]
    original_curves = []
    for action in bpy.data.actions:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    original_curves += [(action.name,fc.data_path,fc.array_index,[tuple(k.co) for k in fc.keyframe_points]) for fc in bag.fcurves]
    source = output/"controlled-fixture-original.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    source_hash = sha256(source)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.render.resolution_x,scene.render.resolution_y = 915,617
    scene.render.resolution_percentage = 43
    scene.render.pixel_aspect_x,scene.render.pixel_aspect_y = 1.2,0.8
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.filepath = str(output/"never-overwrite-original.exr")
    if hasattr(scene.render.image_settings,"media_type"):
        scene.render.image_settings.media_type = "VIDEO"
        scene.render.image_settings.file_format = "FFMPEG"
    # A camera marker must not replace the temporary matched cameras.
    marker = scene.timeline_markers.new("CameraMarker",frame=5)
    marker.camera = original_camera
    storage = Storage(output/"store")
    bridge = LocalBridge()
    # Test-only observation hook in this disposable process: checks actual
    # values Blender sees at rendering, without changing the production code.
    render_observations = []
    def observe_render(current_scene):
        render_observations.append({"action":rig.animation_data.action.name,
                                    "frame":current_scene.frame_current,
                                    "right_thigh_x":rig.pose.bones["right_thigh"].location.x,
                                    "custom_value":rig["qa_custom_channel"]})
    bpy.app.handlers.render_pre.append(observe_render)
    try:
        result = multiview.compare_before_after(bridge,storage,"m2-qa-comparison",rig.name,
                                                "M2_QA_Before","M2_QA_After",
                                                {"width":160,"height":192,"frame_start":1,"frame_end":9})
    finally:
        bpy.app.handlers.render_pre.remove(observe_render)
    assert result["status"] == "ok",result
    assert result["state_restored"] and result["comparable"],result
    assert result["pose_state_restored"]
    assert original_unkeyed == (rig.pose.bones["right_thigh"].location.x,rig["qa_custom_channel"],
                               rig.pose.bones["left_arm"].constraints["M2_QA_PreservedConstraint"].influence)
    assert len(result["files"]) == 18,result
    assert len(render_observations) == 18
    assert all(item["right_thigh_x"] == 0 and item["custom_value"] == 17
               for item in render_observations if item["action"] == "M2_QA_Before")
    assert result["timing"]["frames"] == [1,5,9]
    assert result["render_engine"] == "BLENDER_WORKBENCH"
    assert rig.animation_data.action == original_action and rig.animation_data.action_slot == original_slot
    assert scene.camera == original_camera and scene.frame_current == 3 and abs(scene.frame_subframe-0.25)<1e-6
    assert scene.render.engine == "CYCLES"
    assert sorted(bpy.data.objects.keys()) == original_names
    assert len(bpy.data.actions) == original_action_count
    assert [(a.name,[(s.identifier,s.handle) for s in a.slots]) for a in bpy.data.actions] == original_slots
    after_curves = []
    for action in bpy.data.actions:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    after_curves += [(action.name,fc.data_path,fc.array_index,[tuple(k.co) for k in fc.keyframe_points]) for fc in bag.fcurves]
    assert after_curves == original_curves
    assert sha256(source) == source_hash
    assert not (output/"never-overwrite-original.exr").exists()
    for item in result["files"]:
        assert _decode(Path(item["path"]))[:2] == (160,192)
    assert _decode(Path(result["montage"]["path"]))[:2] == (480,384)
    before_front = next(s for s in result["series"] if s["label"] == "before" and s["view"] == "FRONT")
    after_front = next(s for s in result["series"] if s["label"] == "after" and s["view"] == "FRONT")
    assert Path(before_front["files"][1]["path"]).read_bytes() != Path(after_front["files"][1]["path"]).read_bytes()
    # Inspect actual pixels as well as declared camera metadata. This catches
    # Blender render-time camera-marker overrides that metadata cannot reveal.
    before_pixels = [_decode(Path(series["files"][1]["path"]))[2]
                     for series in result["series"] if series["label"] == "before"]
    assert before_pixels[0] != before_pixels[1] and before_pixels[1] != before_pixels[2]
    checkpoints = list((storage.root/"checkpoints").glob("*/manifest.json"))
    assert len(checkpoints) == 1
    checkpoint = json.loads(checkpoints[0].read_text(encoding="utf-8"))
    assert "ProductionQA_" not in json.dumps(checkpoint["source"])
    bridge.fail_render = True
    failed = multiview.compare_before_after(bridge,storage,"m2-qa-error",rig.name,
                                            "M2_QA_Before","M2_QA_After",{"width":160,"height":192})
    assert failed["status"] == "error" and failed["state_restored"] and not failed["comparable"],failed
    assert any("Injected controlled render failure" in error["message"] for error in failed["errors"])
    assert rig.animation_data.action == original_action and rig.animation_data.action_slot == original_slot
    assert sorted(bpy.data.objects.keys()) == original_names
    assert original_unkeyed == (rig.pose.bones["right_thigh"].location.x,rig["qa_custom_channel"],
                               rig.pose.bones["left_arm"].constraints["M2_QA_PreservedConstraint"].influence)
    bridge.fail_render = False
    nla = rig.animation_data.nla_tracks.new()
    nla.strips.new("M2_QA_NLA",1,original_action)
    refused = False
    try:
        multiview.render_multiview(bridge,storage,"m2-qa-nla",rig.name)
    except ValueError as exc:
        refused = "NLA" in str(exc)
    assert refused
    rig.animation_data.nla_tracks.remove(nla)
    ambiguous = bpy.data.actions["M2_QA_After"]
    extra_slot = ambiguous.slots.new("OBJECT","AmbiguousExtra")
    refused = False
    try:
        multiview.compare_before_after(bridge,storage,"m2-qa-ambiguous",rig.name,
                                       original_action.name,ambiguous.name)
    except ValueError as exc:
        refused = "Ambiguous" in str(exc)
    assert refused
    # Existing active Action/slot must still be untouched after refusal.
    assert rig.animation_data.action == original_action and rig.animation_data.action_slot == original_slot
    explicit = multiview.compare_before_after(bridge,storage,"m2-qa-explicit-slot",rig.name,
                                              original_action.name,ambiguous.name,
                                              {"after_slot":ambiguous.slots[0].identifier,
                                               "width":80,"height":96,"views":["FRONT"],"max_frames":1})
    assert explicit["status"] == "ok" and explicit["comparable"],explicit
    assert rig.animation_data.action == original_action and rig.animation_data.action_slot == original_slot
    assert len(list((storage.root/"checkpoints").glob("*/manifest.json"))) == 3
    unsupported_action = original_action.copy()
    unsupported_action.name = "M2_QA_UnsupportedProperties"
    rig.animation_data.action = unsupported_action
    rig.keyframe_insert("hide_render",frame=1)
    rig.animation_data.action = original_action
    rig.animation_data.action_slot = original_slot
    refused = False
    try:
        multiview.compare_before_after(bridge,storage,"m2-qa-unsupported",rig.name,
                                       original_action.name,unsupported_action.name)
    except ValueError as exc:
        refused = "cannot safely restore" in str(exc)
    assert refused
    assert len(list((storage.root/"checkpoints").glob("*/manifest.json"))) == 3
    checks = ["18 real PNGs in FRONT/SIDE/THREE_QUARTER before/after directories",
              "matched frame samples 1/5/9 at unchanged 24 FPS",
              "identical camera definitions for both Actions using union bounds",
              "camera markers restored and actual rendered viewpoints differ",
              "Action and slot restored after successful QA",
              "unkeyed original transforms/custom values/constraint influences restored after comparison and failure",
              "actual before renders use original unkeyed baseline without after-Action contamination",
              "Action curves and slot metadata remain unchanged",
              "frame and subframe restored",
              "Cycles engine restored without altering its CPU/GPU preference",
              "render settings including VIDEO/FFMPEG and pixel aspect restored",
              "temporary camera object and datablock removed",
              "original fixture SHA256 unchanged and original render path unwritten",
              "real PNG checksums and pixel buffers verified",
              "montage 480x384 created with before row then after row",
              "deliberately different poses produce different actual pixels",
              "checkpoint contains state before temporary camera structure",
              "injected render failure restores Action/slot/camera/render state",
              "injected failure never claims comparable",
              "active NLA refused without checkpoint/render mutation",
              "ambiguous Action slots refused before mutation",
              "explicit compatible slot succeeds and original assignment is restored",
              "unsupported animated properties refused before mutation/checkpoint"]
    report = {"status":"passed","blender_version":bpy.app.version_string,
              "checks":checks,"check_count":len(checks),"comparison":result,
              "failure_result":failed,"explicit_slot_result":explicit,
              "fixture":str(source),"fixture_sha256":source_hash,
              "output_dir":str(output)}
    (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("M2_QA_PROBE_RESULT="+json.dumps({"status":"passed","check_count":len(checks),
                                           "report":str(output/"report.json"),"montage":result["montage"]["path"]}))


def run(blender,output):
    if output is None:
        # Keep PNG filenames under Blender's Windows backend MAX_PATH limit.
        output = ARTIFACTS/f"m2qa-{uuid4().hex[:12]}"
    output = output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    command = [str(blender),"--background","--factory-startup","--disable-autoexec","--python-exit-code","1",
               "--python",str(Path(__file__).resolve()),"--","--inside","--output",str(output)]
    process = subprocess.run(command,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=180)
    (output/"blender.stdout.log").write_text(process.stdout,encoding="utf-8")
    (output/"blender.stderr.log").write_text(process.stderr,encoding="utf-8")
    if process.returncode:
        raise RuntimeError(f"Blender QA probe exited {process.returncode}; logs: {output}\n{process.stdout[-5000:]}\n{process.stderr[-3000:]}")
    lines = [line for line in process.stdout.splitlines() if line.startswith("M2_QA_PROBE_RESULT=")]
    if len(lines) != 1:
        raise RuntimeError(f"Missing unique QA report; logs: {output}")
    print(lines[0])
    return json.loads(lines[0].split("=",1)[1])


if __name__ == "__main__":
    arguments = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--inside",action="store_true")
    args = parser.parse_args(arguments)
    if args.inside:
        inside(args.output)
    elif args.blender:
        run(args.blender,args.output)
    else:
        parser.error("--blender is required outside Blender")
