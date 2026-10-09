"""Bounded isolated-Blender benchmarks; no sockets or user assets.

Measures real 32-bone/1000-frame sampling, 6144-key F-Curve scans, and one frame
from three lightweight viewpoints. All fixture files/artifacts are new.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from uuid import uuid4


REPO = Path(__file__).resolve().parents[2]
# M3.1: BLENDER_PRODUCTION_TEST_ARTIFACTS moves large probe output out of the repository
# (default unchanged: <repo>/test-artifacts).
ARTIFACTS = Path(os.environ.get('BLENDER_PRODUCTION_TEST_ARTIFACTS') or REPO / 'test-artifacts')


def fixture():
    import bpy
    import math
    scene = bpy.context.scene
    scene.name = "M2_Benchmark_ControlledFixture"
    scene.frame_start,scene.frame_end = 1,1000
    scene.render.fps = 24
    armature = bpy.data.armatures.new("M2_BenchmarkData")
    rig = bpy.data.objects.new("M2_BenchmarkRig",armature)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    for index in range(32):
        bone = armature.edit_bones.new(f"B{index:02d}")
        bone.head = ((index%4-1.5)*0.2,0,0.1+(index//4)*0.23)
        bone.tail = ((index%4-1.5)*0.2,0,0.27+(index//4)*0.23)
        if index >= 4:
            bone.parent = armature.edit_bones[f"B{index-4:02d}"]
    bpy.ops.object.mode_set(mode="OBJECT")
    for bone in rig.pose.bones:
        index = int(bone.name[1:])
        for key in range(64):
            frame = 1+999*key/63
            phase = key*math.tau/16+index*0.1
            bone.location = (0.005*math.sin(phase),0.003*math.cos(phase),0)
            bone.keyframe_insert("location",frame=frame,group=bone.name)
    action = rig.animation_data.action
    action.name = "M2_Benchmark_6144Keys"
    curves = [curve for layer in action.layers for strip in layer.strips
              for bag in strip.channelbags for curve in bag.fcurves]
    for curve in curves:
        for key in curve.keyframe_points:
            key.interpolation = "LINEAR"
    # Use existing factory cube as a generated weighted proxy, no asset loads.
    cube = bpy.data.objects["Cube"]
    cube.name = "M2_BenchmarkProxy"
    cube.scale = (0.3,0.12,0.85)
    cube.location.z = 1
    cube.parent = rig
    group = cube.vertex_groups.new(name="B00")
    group.add(list(range(len(cube.data.vertices))),1.0,"REPLACE")
    modifier = cube.modifiers.new("M2_BenchmarkArmature","ARMATURE")
    modifier.object = rig
    scene.frame_set(7,subframe=0.25)
    return rig,curves


def inside(output):
    import bpy
    import types
    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("Benchmark requires an unsaved isolated factory-startup process")
    sys.path.insert(0,str(REPO/"src"))
    source_package = types.ModuleType("blender_mcp")
    source_package.__path__ = [str(REPO/"src"/"blender_mcp")]
    sys.modules["blender_mcp"] = source_package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage,sha256
    from blender_mcp.production.animation.insights import run_analysis
    from blender_mcp.production.animation.curve_ops import analyze_fcurves
    from blender_mcp.production.qa.multiview import render_multiview

    class LocalBridge:
        def __init__(self):
            self.calls = []

        def run(self,body,params=None):
            started = time.perf_counter()
            code = build_code(body,params)
            namespace = {}
            # The removed line is only build_code's transport-result print;
            # its validated bpy payload is executed unchanged.
            exec(code.rsplit("\nprint(",1)[0],namespace)
            result = namespace["result"]
            payload = json.dumps(result,allow_nan=False,separators=(",",":"))
            self.calls.append({"elapsed_ms":round((time.perf_counter()-started)*1000,3),
                               "request_bytes":len(code.encode("utf-8")),
                               "response_bytes":len(payload.encode("utf-8"))})
            return json.loads(payload)

    rig,curves = fixture()
    source = output/"fixture.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    original_hash = sha256(source)
    original_action = rig.animation_data.action
    original_slot = rig.animation_data.action_slot
    original_frame = (bpy.context.scene.frame_current,bpy.context.scene.frame_subframe)
    key_count = sum(len(c.keyframe_points) for c in curves)
    assert len(rig.pose.bones) == 32 and len(curves) == 96 and key_count == 6144
    bridge = LocalBridge()
    storage = Storage(output/"store")
    benchmarks = []

    def measured(name,function):
        start_calls = len(bridge.calls)
        started = time.perf_counter()
        value = function()
        elapsed = (time.perf_counter()-started)*1000
        calls = bridge.calls[start_calls:]
        record = {"name":name,"elapsed_ms":round(elapsed,3),
                  "public_payload_bytes":len(json.dumps(value,separators=(",",":"),allow_nan=False).encode("utf-8")),
                  "bridge_request_bytes":sum(item["request_bytes"] for item in calls),
                  "bridge_response_bytes":sum(item["response_bytes"] for item in calls),
                  "bridge_calls":len(calls),"bridge_execution_ms":round(sum(item["elapsed_ms"] for item in calls),3),
                  "metrics":value.get("metrics",{})}
        benchmarks.append(record)
        return value

    sample = measured("motion_quality_1000_frames_32_bones",lambda:run_analysis(bridge,"motion_quality",rig.name,
                      {"frame_start":1,"frame_end":1000,"max_samples":1000,"bones":[bone.name for bone in rig.pose.bones],
                       "root_bone":"B00","detail":"summary","limit":40}))
    assert sample["frame_restored"] and sample["metrics"]["sample_count"] == 1000
    assert sample["metrics"]["bone_frame_evaluations"] == 32000
    analyzed = measured("fcurve_analysis_96_channels_6144_keys",lambda:analyze_fcurves(bridge,rig.name,
                         {"detail":"summary","limit":20,"frame_start":1,"frame_end":1000}))
    rendered = measured("lightweight_preview_1_frame_3_views",lambda:render_multiview(bridge,storage,"benchmark-render",rig.name,
                         {"frame_start":500,"frame_end":500,"max_frames":1,"width":160,"height":192,"montage":True}))
    assert rendered["status"] == "ok" and rendered["state_restored"] and len(rendered["files"]) == 3
    assert rig.animation_data.action == original_action and rig.animation_data.action_slot == original_slot
    assert original_frame == (bpy.context.scene.frame_current,bpy.context.scene.frame_subframe)
    assert key_count == sum(len(c.keyframe_points) for c in curves)
    assert original_hash == sha256(source)
    report = {"status":"passed","created_at_utc":datetime.now(timezone.utc).isoformat(),
              "blender_version":bpy.app.version_string,"python_version":sys.version.split()[0],
              "os":platform.platform(),"processor":platform.processor(),
              "fixture":{"path":str(source),"sha256":original_hash,"bones":32,"fcurves":96,"keyframes":6144,
                         "sampled_frames":1000,"bone_frame_evaluations":32000},
              "benchmarks":benchmarks,"render":rendered,
              "source_unchanged":True,"action_slot_frame_restored":True,
              "notes":["Wall times include safe-mode validation, JSON serialization and algorithms; socket latency is excluded.",
                       "Sampling necessarily evaluates Blender at each frame; all selected bone tracks share that frame evaluation.",
                       "Internal bridge samples stay local; only public summaries are returned to the model.",
                       "A single cold run describes this machine/fixture and is not a universal performance guarantee."]}
    (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("M2_BENCHMARK_RESULT="+json.dumps({"status":"passed","report":str(output/"report.json"),"benchmarks":benchmarks}))


def run(blender,output):
    output = (output or ARTIFACTS/f"m2bench-{uuid4().hex[:8]}").resolve()
    output.mkdir(parents=True,exist_ok=False)
    command = [str(blender),"--background","--factory-startup","--disable-autoexec","--python-exit-code","1",
               "--python",str(Path(__file__).resolve()),"--","--inside","--output",str(output)]
    process = subprocess.run(command,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=180)
    (output/"blender.stdout.log").write_text(process.stdout,encoding="utf-8")
    (output/"blender.stderr.log").write_text(process.stderr,encoding="utf-8")
    if process.returncode:
        raise RuntimeError(f"Benchmark Blender exited {process.returncode}; logs: {output}\n{process.stdout[-3000:]}\n{process.stderr[-3000:]}")
    reports = [line for line in process.stdout.splitlines() if line.startswith("M2_BENCHMARK_RESULT=")]
    if len(reports) != 1:
        raise RuntimeError(f"Missing unique benchmark report; logs: {output}")
    print(reports[0])


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
