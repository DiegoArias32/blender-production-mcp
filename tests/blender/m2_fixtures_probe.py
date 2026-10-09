"""Verify the M2 fourteen-scenario catalog in isolated Blender, without sockets."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]
# M3.1: BLENDER_PRODUCTION_TEST_ARTIFACTS moves large probe output out of the repository
# (default unchanged: <repo>/test-artifacts).
ARTIFACTS = Path(os.environ.get('BLENDER_PRODUCTION_TEST_ARTIFACTS') or REPO / 'test-artifacts')


def inside(output):
    import bpy
    import types
    sys.path.insert(0,str(Path(__file__).parent))
    package = types.ModuleType("blender_mcp")
    package.__path__ = [str(REPO/"src"/"blender_mcp")]
    sys.modules["blender_mcp"] = package
    from m2_fixtures import build, curves
    from blender_mcp.production.animation.insights import run_analysis
    from blender_mcp.production.animation.service import detect_quaternion_flips
    from blender_mcp.production.animation.curve_ops import analyze_fcurves
    from blender_mcp.production.common.bridge import build_code

    manifest = build(output)
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    class DirectBridge:
        def run(self,body,params):
            build_code(body,params)
            namespace = {"params":params}
            exec(body,namespace)
            return namespace["result"]

    bridge = DirectBridge()
    scenarios = manifest["scenarios"]
    expected = {"clean_walk","left_sliding","bilateral_sliding","intentional_contact_transition",
        "quaternion_discontinuity","mocap_noise","redundant_keys","root_motion_walk","in_place",
        "ik_rig","fk_rig","mixed_ik_fk","balance_issue","fast_valid"}
    check("exact fourteen requested scenarios",set(scenarios)==expected)
    check("visible procedural skin geometry",len(manifest["visible_meshes"])>=16 and all(bpy.data.objects[name].type=="MESH" for name in manifest["visible_meshes"]))
    checkpoint = json.loads(Path(manifest["checkpoint"]).read_text(encoding="utf-8"))
    check("structural checkpoint provenance",checkpoint["source"]==manifest["source_m1"] and checkpoint["operation"]=="m2_fixture_structure")
    check("structural checkpoint hash",hashlib.sha256((Path(manifest["checkpoint"]).parent/"scene.blend").read_bytes()).hexdigest()==checkpoint["sha256"])
    rig = bpy.data.objects[manifest["visible_rig"]]
    source_action = rig.animation_data.action
    source_slot = rig.animation_data.action_slot
    scene = bpy.context.scene
    original_frame,original_subframe = scene.frame_current,scene.frame_subframe
    reports = {}
    def activate(key):
        row = scenarios[key]
        obj = bpy.data.objects[row["object"]]
        action = bpy.data.actions[row["action"]]
        obj.animation_data.action = action
        obj.animation_data.action_slot = action.slots[0]
        scene.frame_set(7,subframe=.25)
        return obj

    try:
        for key,row in scenarios.items():
            action = bpy.data.actions[row["action"]]
            check(key+" keeps first/last keys on every channel",all(float(c.keyframe_points[0].co.x)==1 and float(c.keyframe_points[-1].co.x)==31 for c in curves(action)))
            check(key+" preserved as independent fake-user Action",action.use_fake_user and action["scenario"]==key)
        for key,left,right in [("clean_walk",0,0),("left_sliding",.018,0),("bilateral_sliding",.018,.025)]:
            obj = activate(key)
            report = run_analysis(bridge,"contacts",obj.name,{"frame_start":1,"frame_end":10,"bones":manifest["markers"]})
            reports[key] = report
            values = {item["bone"]:item["max_sliding_m"] for item in report["effectors"]}
            check(key+" physical left sliding",abs(values["ContactControl.L"]-left)<1e-6)
            check(key+" physical right sliding",abs(values["ContactControl.R"]-right)<1e-6)
        obj = activate("intentional_contact_transition")
        contact = run_analysis(bridge,"contacts",obj.name,{"frame_start":1,"frame_end":10,"bones":["ContactControl.L"]})
        check("intentional takeoff contact ends before airborne phase",all(interval["frame_end"]<7 for interval in contact["effectors"][0]["intervals"]))
        obj = activate("quaternion_discontinuity")
        quat = detect_quaternion_flips(bridge,object_name=obj.name)
        check("deliberate quaternion discontinuity exists",quat["metrics"]["sign_discontinuity_count"]==1)
        check("deliberate quaternion is keyed-equivalent orientation",quat["metrics"]["orientation_jump_count"]==0)
        obj = activate("redundant_keys")
        redundant = analyze_fcurves(bridge,obj.name,{"bones":["Pelvis"]})
        check("redundant-key fixture measured",redundant["metrics"]["finding_count"]>0)
        obj = activate("mocap_noise")
        noisy = analyze_fcurves(bridge,obj.name,{"bones":["Pelvis"],"noise_threshold":.001})
        check("mocap-noise fixture has dense keys",noisy["metrics"]["keys_analyzed"]>=93)
        for key,expected_motion in (("root_motion_walk",.12),("in_place",0)):
            obj = activate(key)
            root = run_analysis(bridge,"root_motion",obj.name,{"frame_start":1,"frame_end":31,"root_bone":"MotionBase"})
            reports[key] = root
            check(key+" root displacement",abs(root["distance_m"]-expected_motion)<1e-6)
        for key,expected_ik in (("ik_rig",2),("fk_rig",0),("mixed_ik_fk",1)):
            obj = activate(key)
            mode = run_analysis(bridge,"control_mode",obj.name,{})
            reports[key] = mode
            check(key+" measured constraint count",mode["metrics"]["explicit_active_ik_count"]==expected_ik)
        obj = activate("balance_issue")
        balance = run_analysis(bridge,"balance",obj.name,{"frame_start":1,"frame_end":10,
            "support_bones":manifest["markers"],"pelvis_bone":"Pelvis"})
        check("balance issue produces heuristic review",balance["metrics"]["suspicious_frame_count"]>0 and balance["confidence"]<1)
        obj = activate("fast_valid")
        quality = run_analysis(bridge,"motion_quality",obj.name,{"frame_start":1,"frame_end":31,"bones":["LowerArm.R"]})
        fast_track = next(item for item in quality["tracks"] if item["target"]=="LowerArm.R")
        check("fast intentional rotation measured",fast_track["peak_angular_velocity_deg_s"]>300)
        check("fast intentional rotation not automatically a pop",not any(item["type"]=="angular_acceleration_review" for item in fast_track["findings"]))
        obj = activate("left_sliding")
        residual = 0
        for frame in range(1,32):
            scene.frame_set(frame)
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for suffix in ("L","R"):
                tip = evaluated.matrix_world @ evaluated.pose.bones["LowerLeg."+suffix].tail
                target = evaluated.matrix_world @ evaluated.pose.bones["ContactControl."+suffix].head
                residual = max(residual,(tip-target).length)
        check("demo legs stay reachable through source motion",residual<.01)
        reports["maximum_ik_residual_m"] = residual
    finally:
        rig.animation_data.action = source_action
        rig.animation_data.action_slot = source_slot
        scene.frame_set(original_frame,subframe=original_subframe)
    check("source M1 bytes unchanged",hashlib.sha256(Path(manifest["source_m1"]).read_bytes()).hexdigest()==manifest["source_m1_sha256"])
    check("saved M2 fixture bytes unchanged by analysis",hashlib.sha256(Path(manifest["blend"]).read_bytes()).hexdigest()==manifest["sha256"])
    report = {"status":"passed","check_count":len(checks),"checks":checks,"manifest":str(output/"manifest.json"),
              "blender_version":bpy.app.version_string,"results":reports}
    (output/"verification.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("M2_FIXTURES_PROBE="+json.dumps({"status":"passed","checks":len(checks),"report":str(output/"verification.json")}))


def run(blender):
    started = time.perf_counter()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ARTIFACTS/("m2-fixtures-"+stamp)
    proc = subprocess.run([str(blender),"--background","--factory-startup","--disable-autoexec",
        "--python-exit-code","1","--python",str(Path(__file__).resolve()),"--","--inside",str(output)],
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=240)
    if proc.returncode or "M2_FIXTURES_PROBE=" not in proc.stdout:
        raise RuntimeError(f"Blender exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    report = json.loads((output/"verification.json").read_text(encoding="utf-8"))
    print(json.dumps({"status":report["status"],"checks":report["check_count"],"report":str(output/"verification.json"),
                     "elapsed_seconds":round(time.perf_counter()-started,3)},indent=2))
    return report


if __name__ == "__main__":
    if "--inside" in sys.argv:
        inside(Path(sys.argv[sys.argv.index("--inside")+1]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--blender",required=True,type=Path)
        run(parser.parse_args().blender)
