"""Real Blender M2 analysis checks in an isolated factory-startup process.

No socket, user preferences, external assets or existing production .blend are
used. Evidence and generated fixtures remain under test-artifacts by default.
"""
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
    sys.path.insert(0, str(REPO / "src"))
    sys.path.insert(0, str(Path(__file__).parent))
    # The upstream package __init__ eagerly imports the MCP network server.
    # Headless tests load only source submodules; Blender needs no MCP install.
    source_package = types.ModuleType("blender_mcp")
    source_package.__path__ = [str(REPO / "src" / "blender_mcp")]
    sys.modules["blender_mcp"] = source_package
    from production_fixtures import build, _armature
    from blender_mcp.production.animation.insights import run_analysis
    from blender_mcp.production.common.bridge import build_code

    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError("Probe requires an unsaved factory-startup process")
    output.mkdir(parents=True, exist_ok=False)
    fixture = build(output)
    fixture_path = Path(fixture["blend"])
    source_hash = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    # New anonymous fixture proves classification uses hierarchy/constraints,
    # not semantic names. This isolated generated scene owns all data below.
    rig = _armature("M2_Anonymous", [
        ("A", (0,0,1), (0,0,1.2), None, False),
        ("B", (-.25,0,1.8), (-.25,0,.9), "A", True),
        ("C", (-.25,0,.9), (-.25,0,0), "B", True),
        ("D", (-.25,0,0), (-.25,0,.2), None, False),
        ("E", (.25,0,1.8), (.25,0,.9), "A", True),
        ("F", (.25,0,.9), (.25,0,0), "E", True),
        ("G", (.25,0,0), (.25,0,.2), None, False),
    ])
    for owner, target in (("C","D"),("F","G")):
        constraint = rig.pose.bones[owner].constraints.new("IK")
        constraint.target = rig
        constraint.subtarget = target
        constraint.chain_count = 2
    fast = bpy.data.objects.new("M2_FastIntentional", None)
    bpy.context.collection.objects.link(fast)
    for frame, x in ((1,0),(31,3000)):
        fast.location = (x,0,0)
        fast.keyframe_insert(data_path="location",frame=frame)
    if fast.animation_data.action.is_action_layered:
        fast_curves = [curve for layer in fast.animation_data.action.layers for strip in layer.strips
                       for bag in strip.channelbags for curve in bag.fcurves]
    else:
        fast_curves = fast.animation_data.action.fcurves
    for curve in fast_curves:
        for key in curve.keyframe_points:
            key.interpolation = "LINEAR"
    bpy.context.scene.frame_set(7,subframe=.25)

    def state():
        actions = {}
        for action in bpy.data.actions:
            if action.is_action_layered:
                curves = [(bag.slot_handle,li,si,curve) for li,layer in enumerate(action.layers)
                          for si,strip in enumerate(layer.strips) for bag in strip.channelbags for curve in bag.fcurves]
            else:
                curves = [(None,None,None,curve) for curve in action.fcurves]
            actions[action.name] = [(slot,li,si,c.data_path,c.array_index,
                                    [(list(k.co),list(k.handle_left),list(k.handle_right),k.interpolation) for k in c.keyframe_points])
                                   for slot,li,si,c in curves]
        return {"frame":bpy.context.scene.frame_current,"subframe":bpy.context.scene.frame_subframe,
                "source":bpy.data.filepath,"actions":actions,
                "assignments":{obj.name:obj.animation_data.action.name if obj.animation_data and obj.animation_data.action else None for obj in bpy.data.objects}}

    initial = state()
    calls = []

    class DirectBridge:
        def run(self, body, params):
            build_code(body,params)
            namespace = {"params":params}
            exec(body,namespace)
            calls.append(body)
            return namespace["result"]

    bridge = DirectBridge()
    checks, results = [], {}

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    common = {"frame_start":1,"frame_end":10,"bones":["FootIK.L","FootIK.R"]}
    contact = run_analysis(bridge,"contacts","M1_Rig",common)
    results["contacts"] = contact
    feet = {item["bone"]:item for item in contact["effectors"]}
    check("physical sliding left 1.8 cm",abs(feet["FootIK.L"]["max_sliding_m"]-.018)<1e-6)
    check("physical sliding right .22 cm",abs(feet["FootIK.R"]["max_sliding_m"]-.0022)<1e-6)
    check("contact interval includes velocity and confidence",0<feet["FootIK.L"]["intervals"][0]["contact_confidence"]<1 and feet["FootIK.L"]["intervals"][0]["max_vertical_velocity_m_s"]==0)
    check("read only frame and action preservation after sampling",initial==state())
    anonymous = run_analysis(bridge,"contacts","M2_Anonymous",{"frame_start":1,"frame_end":5})
    results["anonymous_contacts"] = anonymous
    check("anonymous effectors detected via IK and symmetry",{"D","G"}.issubset({item["bone"] for item in anonymous["effectors"]}))
    check("anonymous effectors do not claim anatomical side",all(item["anatomical_side"]=="unresolved" for item in anonymous["effectors"]))
    control = run_analysis(bridge,"control_mode","M1_Rig",{})
    results["control_mode"] = control
    check("control analysis discovers two real IK solvers",control["metrics"]["explicit_active_ik_count"]==2)
    fk = run_analysis(bridge,"control_mode","M1_SimpleRig",{})
    check("deform-only fixture refuses to claim verified IK",fk["mode"]=="deform_only")
    root = run_analysis(bridge,"root_motion","M1_Rig",{"frame_start":1,"frame_end":31})
    results["root_motion"] = root
    check("root candidate determined by hierarchy",root["target"]["bone"]=="Root")
    check("root displacement .3 meters",abs(root["translation_xyz_m"][0]-.3)<1e-6)
    check("root average speed .3 m/s",abs(root["average_velocity_m_s"]-.3)<1e-6)
    check("root endpoint loop status remains conservative",root["loop_status"]=="endpoints_differ")
    anon_root = run_analysis(bridge,"root_motion","M2_Anonymous",{"frame_start":1,"frame_end":5})
    check("root detection independent of names",anon_root["target"]["bone"]=="A")
    object_root = run_analysis(bridge,"root_motion","M1_Rig",{"frame_start":1,"frame_end":31,"root_object":True})
    check("explicit object root does not double-count bone motion",object_root["target"]["bone"] is None and object_root["distance_m"]<1e-9)
    balance = run_analysis(bridge,"balance","M1_Rig",{**common,"support_bones":["FootIK.L","FootIK.R"],"pelvis_bone":"Root"})
    results["balance"] = balance
    check("biped balance is a heuristic proxy",balance["model"]=="biped_marker_capsule" and balance["confidence"]<1)
    check("balanced fixture no support-proxy outliers",balance["metrics"]["suspicious_frame_count"]==0)
    quality = run_analysis(bridge,"motion_quality","M2_FastIntentional",{"frame_start":1,"frame_end":31})
    results["fast_motion_quality"] = quality
    check("intentional fast motion measured 3000m/s",abs(quality["metrics"]["peak_velocity_m_s"]-3000)<1e-5)
    check("intentional fast linear motion never classified as pop",quality["metrics"]["finding_count"]==0)
    plan = run_analysis(bridge,"refinement_plan","M1_Rig",{**common,"support_bones":["FootIK.L","FootIK.R"],"pelvis_bone":"Root"})
    results["refinement_plan"] = plan
    check("refinement plan finds contact sliding",any(item["type"]=="foot_sliding" for item in plan["issues"]))
    check("refinement plan is plan only",plan["status"]=="plan_only")
    check("refinement plan includes real F-Curve analysis",plan["analyses"]["fcurves"]["metrics"]["channels_analyzed"]>0)
    check("refinement plan compacts nested summaries","channels" not in plan["analyses"]["action"])
    check("all analyses preserve all action curves and assignments",initial==state())
    for label, name, opts in [
        ("unknown Action rejected","M1_Rig",{"action_name":"DOES_NOT_EXIST"}),
        ("missing bone rejected","M1_Rig",{"bones":["MISSING"]}),
        ("frame budget rejected","M1_Rig",{"frame_start":1,"frame_end":100,"max_samples":2}),
        ("bone on non-armature rejected","M1_Cube",{"bones":["MISSING"]}),
    ]:
        caught = False
        try:
            run_analysis(bridge,"contacts",name,opts)
        except ValueError:
            caught = True
        check(label,caught)
    check("failed preconditions preserve state",initial==state())
    # Inject a deterministic error during evaluation, with the exact validated
    # production template. Tests restoration without requiring a failing rig.
    from blender_mcp.production.animation.insights_scripts import SAMPLE
    from blender_mcp.production.animation.insights import validate_options
    broken = SAMPLE.replace("evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())",
                            'if frame == 3:\n            raise RuntimeError("Injected sample failure")\n        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())')
    caught = False
    try:
        bridge.run(broken,{"object_name":"M1_Rig",**validate_options("contacts","M1_Rig",common)})
    except RuntimeError as error:
        caught = str(error)=="Injected sample failure"
    check("sample failure is propagated",caught)
    check("finally restores state after sample failure",initial==state())
    check("saved source fixture SHA256 unchanged",hashlib.sha256(fixture_path.read_bytes()).hexdigest()==source_hash)
    report = {"status":"passed","blender_version":bpy.app.version_string,"check_count":len(checks),
              "checks":checks,"results":results,"fixture":str(fixture_path),"fixture_sha256":source_hash,
              "validated_payload_calls":len(calls)}
    (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("M2_INSIGHTS_PROBE="+json.dumps({"status":"passed","check_count":len(checks),"report":str(output/"report.json")}))


def run(blender, output=None):
    started = time.perf_counter()
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        output = ARTIFACTS/("m2-insights-"+stamp)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("Probe output must be new")
    process = subprocess.run([str(blender),"--background","--factory-startup","--disable-autoexec",
        "--python-exit-code","1","--python",str(Path(__file__).resolve()),"--","--inside",str(output)],
        capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=180)
    if process.returncode or "M2_INSIGHTS_PROBE=" not in process.stdout:
        raise RuntimeError(f"Blender exit {process.returncode}\n{process.stdout}\n{process.stderr}")
    report = json.loads((output/"report.json").read_text(encoding="utf-8"))
    print(json.dumps({"status":report["status"],"checks":report["check_count"],"report":str(output/"report.json"),
                      "elapsed_seconds":round(time.perf_counter()-started,3)},indent=2))
    return report


if __name__ == "__main__":
    if "--inside" in sys.argv:
        inside(Path(sys.argv[sys.argv.index("--inside")+1]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--blender",required=True,type=Path)
        parser.add_argument("--output",type=Path)
        arguments = parser.parse_args()
        run(arguments.blender,arguments.output)
