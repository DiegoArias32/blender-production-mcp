"""Deterministic metrics, uncertainty and non-mutating analysis contracts."""
import copy
import math

import pytest

from blender_mcp.production.animation import insights
from blender_mcp.production.animation.insights_scripts import SAMPLE
from blender_mcp.production.common.bridge import build_code


def options(**kwargs):
    return insights.validate_options("contacts", "Fixture", kwargs)


def track(points):
    return {"positions_m":points, "quaternions":[[1,0,0,0] for _ in points],
            "euler_xyz":[[0,0,0] for _ in points]}


def sample_fixture():
    frames = list(range(1, 11))
    candidates = [{"bone":name, "likely_effector":"foot", "anatomical_side":"unresolved", "confidence":0.8,
                   "evidence":["Explicit IK target", "Rest-local X symmetry"], "ik_targets":[],
                   "selection":"explicit_marker"} for name in ("a", "b")]
    return {"object":"Fixture", "action":"Take", "is_armature":True, "frames":frames, "fps":30,
            "scale_m":1.0, "frame_restored":True, "effectors":candidates,
            "tracks":{"a":track([[-0.25+i*.002,0,0] for i in range(10)]),
                      "b":track([[.25+i*.0022/9,0,0] for i in range(10)]),
                      "base":track([[i*.01,0,1] for i in range(10)])},
            "object_track":track([[0,0,0] for _ in frames]), "root_bone":"base", "pelvis_bone":"base",
            "root_candidates":[{"bone":"base", "confidence":.85, "descendant_count":4}],
            "repeated_key_intervals":[], "warnings":[]}


class FakeBridge:
    def __init__(self, sample=None):
        self.sample = sample or sample_fixture()
        self.calls = []

    def run(self, body, params):
        build_code(body, params)
        self.calls.append((body, params))
        return copy.deepcopy(self.sample)


@pytest.mark.parametrize("kwargs", [
    {"unexpected":1}, {"frame_start":2, "frame_end":1}, {"step":True},
    {"max_samples":2001}, {"bones":["a","a"]}, {"bones":[]},
    {"support_bones":["a","b","c"]}, {"root_object":True,"root_bone":"base"},
    {"ground_height_m":math.nan}, {"contact_height_m":-1}, {"root_object":1},
    {"derivative_ratio":1}, {"detail":"raw"}, {"limit":101},
    {"acceleration_threshold_m_s2":float("inf")}, {"pelvis_bone":""},
])
def test_options_reject_before_sampling(kwargs):
    bridge = FakeBridge()
    with pytest.raises(ValueError):
        insights.run_analysis(bridge,"contacts","Fixture",kwargs)
    assert not bridge.calls


def test_sample_template_respects_upstream_safe_mode_and_json_names():
    hostile = "Q'; bpy.ops.wm.quit_blender() #"
    code = build_code(SAMPLE, {"object_name":hostile, **options(bones=[hostile])})
    assert hostile not in SAMPLE
    assert "params = json.loads" in code


def test_contacts_metrics_have_units_confidence_behavior_and_no_raw_tracks():
    result = insights.run_analysis(FakeBridge(), "contacts", "Fixture", {})
    left, right = result["effectors"]
    assert left["max_sliding_m"] == pytest.approx(.018)
    assert left["intervals"][0]["status"] == "fail"
    assert right["intervals"][0]["status"] == "pass"
    assert left["intervals"][0]["average_height_m"] == 0
    assert 0 < left["intervals"][0]["contact_confidence"] < 1
    assert left["behavior_evidence"] and result["surface"]["kind"] == "ground_plane"
    assert "tracks" not in result and "positions_m" not in str(result)


def test_contacts_airborne_is_inconclusive():
    sample = sample_fixture()
    for name in ("a","b"):
        sample["tracks"][name]["positions_m"] = [[0,0,1] for _ in sample["frames"]]
    result = insights.run_analysis(FakeBridge(sample), "contacts", "Fixture", {})
    assert result["status"] == "inconclusive" and result["metrics"]["contact_count"] == 0


def test_ground_calibration_changes_contact_without_changing_motion():
    sample = sample_fixture()
    for name in ("a","b"):
        for point in sample["tracks"][name]["positions_m"]:
            point[2] = 3
    assert insights.run_analysis(FakeBridge(sample),"contacts","Fixture",{})["status"] == "inconclusive"
    result = insights.run_analysis(FakeBridge(sample),"contacts","Fixture",{"ground_height_m":3})
    assert result["metrics"]["max_sliding_m"] == pytest.approx(.018)


def test_deliberately_fast_constant_motion_not_a_pop():
    values = track([[i*100,0,0] for i in range(30)])
    result = insights._motion(list(range(30)),values,30,options())
    assert result["peak_velocity_m_s"] == 3000
    assert result["peak_acceleration_m_s2"] == 0
    assert result["findings"] == []


def test_single_pop_detected_with_context_and_configurable_threshold():
    points = [[0,0,0] for _ in range(30)]
    points[15] = [1,0,0]
    result = insights._motion(list(range(30)),track(points),30,options())
    assert result["finding_count"] > 0
    assert any(item["type"] == "translation_jump_review" for item in result["findings"])
    result = insights._motion(list(range(30)),track(points),30,options(
        jump_distance_m=100,acceleration_threshold_m_s2=1e9,jerk_threshold_m_s3=1e12))
    assert result["finding_count"] == 0


def test_root_distance_is_path_not_net_and_euler_unwraps():
    values = track([[0,0,0],[1,0,0],[0,0,0]])
    values["euler_xyz"] = [[0,0,math.radians(v)] for v in (179,-179,-177)]
    result = insights._motion([1,2,3],values,30,options())
    assert result["translation_xyz_m"] == [0,0,0]
    assert result["horizontal_distance_m"] == 2
    assert result["accumulated_euler_xyz_degrees"][2] == pytest.approx(4)
    assert result["loop_status"] == "endpoints_match"


def test_invalid_quaternion_never_claims_zero_orientation_delta():
    values = track([[0,0,0],[0,0,0]])
    values["quaternions"][1] = [0,0,0,0]
    result = insights._motion([1,2],values,30,options())
    assert result["invalid_quaternion_pairs"] == 1
    assert result["endpoint_angular_delta_degrees"] is None
    assert result["loop_status"] == "endpoints_differ"


def test_balance_biped_support_has_no_center_of_mass_claim():
    result = insights.run_analysis(FakeBridge(), "balance", "Fixture", {"support_bones":["a","b"], "pelvis_bone":"base"})
    assert result["status"] == "no_proxy_outliers"
    assert result["confidence"] < 1 and result["model"] == "biped_marker_capsule"
    assert any("not center of mass" in item for item in result["warnings"])


def test_balance_displacement_only_proposes_review_never_mutates():
    sample = sample_fixture()
    sample["tracks"]["base"] = track([[3,0,1] for _ in sample["frames"]])
    result = insights.run_analysis(FakeBridge(sample), "balance", "Fixture", {"support_bones":["a","b"], "pelvis_bone":"base", "limit":2})
    assert result["status"] == "heuristic_review"
    assert result["metrics"]["suspicious_frame_count"] == 10
    assert result["findings_truncated"] and len(result["findings"]) == 2
    assert all(finding["severity"] == "review" for finding in result["findings"])


def test_failed_time_restoration_is_not_success():
    sample = sample_fixture()
    sample["frame_restored"] = False
    with pytest.raises(RuntimeError, match="restore"):
        insights.run_analysis(FakeBridge(sample),"root_motion","Fixture",{})


def test_control_mode_does_not_infer_ik_from_names():
    rig = {"armature":"FootIK", "ik_chains":[], "fk_chain_candidates":[{"bones_root_to_tip":["IK_L","IK_R"]}],
           "deform_bones":2, "non_deform_bones":0, "drivers":[], "constraints":[]}
    result = insights._control_mode(rig,50)
    assert result["mode"] == "deform_only"
    assert result["limbs"][0]["mode"] == "FK_candidate"


def test_control_mode_partial_influence_is_mixed():
    rig = {"armature":"Rig", "ik_chains":[{"owner":"b", "constraint":"c", "active":True,
            "subtarget":"target", "target":"Rig", "bones_tip_to_root":["b","a"]}],
           "fk_chain_candidates":[], "deform_bones":2, "non_deform_bones":1, "drivers":[],
           "constraints":[{"name":"c","owner":"b","influence":.5}]}
    result = insights._control_mode(rig,50)
    assert result["mode"] == "mixed" and result["limbs"][0]["mode"] == "mixed"


def test_motion_quality_is_not_artistic_quality_score():
    result = insights.run_analysis(FakeBridge(),"motion_quality","Fixture",{})
    assert "score" not in result and "rating" not in result
    assert result["metrics"]["stationary_contact_failed_count"] == 1
    assert "positions_m" not in str(result)


def test_refinement_plan_integrates_fcurves_compactly_and_orders_severity(monkeypatch):
    from blender_mcp.production.animation import curve_ops
    monkeypatch.setattr(insights,"analyze",lambda *a,**k:{"metrics":{"key_count":2},"scene":{"fps":30},"warnings":[]})
    monkeypatch.setattr(insights,"detect_quaternion_flips",lambda *a,**k:{"groups":[],"metrics":{},"warnings":[]})
    monkeypatch.setattr(insights,"rig_analyze",lambda *a,**k:{"armature":"Fixture","ik_chains":[],
        "fk_chain_candidates":[],"deform_bones":1,"non_deform_bones":0,"drivers":[],"constraints":[]})
    monkeypatch.setattr(curve_ops,"analyze_fcurves",lambda *a,**k:{"findings":[{"type":"redundant_key_candidate","severity":"info","confidence":1}],
        "metrics":{"finding_count":1},"thresholds":{"tolerance":.0001},"warnings":[]})
    result = insights.run_analysis(FakeBridge(),"refinement_plan","Fixture",{})
    assert result["status"] == "plan_only"
    assert result["analyses"]["fcurves"]["metrics"]["finding_count"] == 1
    assert result["issues"][0]["severity"] == "high"
    assert any(item["recommended_tool"] == "production.animation_cleanup_fcurves" for item in result["issues"])
    assert "positions_m" not in str(result) and "channels" not in result["analyses"]["action"]
