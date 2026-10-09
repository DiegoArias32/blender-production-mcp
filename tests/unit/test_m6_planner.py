"""M6 pure planners: timing, grounded contact intent, bounds and provenance."""
from copy import deepcopy
import math
import pytest

from blender_mcp.production.animation.generation.planner import create_brief, plan_poses, analyze_timing, trajectory_metrics


def test_brief_distinguishes_user_values_defaults_and_parsed_intent():
    brief = create_brief({"intent": "heavy overhead attack", "duration_s": 3., "fps": 60})
    assert brief["type"] == "ATTACK" and brief["profile"] == "HEAVY_HUMANOID"
    assert brief["frame_end"] == 181 and brief["attack_type"] == "OVERHEAD"
    assert brief["provenance"]["duration_s"]["source"] == "user_requirement"
    assert brief["provenance"]["frame_start"]["source"] == "inferred_default"
    assert "interpretation" in brief["provenance"]["type"]
    assert brief["apply"] is False and brief["optional_proposals"] == []


@pytest.mark.parametrize("intent", [{}, {"intent": "attack and walk"}, {"type": "JUMP"}, {"type": "ATTACK", "fps": 24},
    {"type": "IDLE", "duration_s": .9}, {"type": "IDLE", "duration_s": 11}, {"type": "TURN", "turn_degrees": 60},
    {"type": "IDLE", "duration_s": float("nan")}, {"type": "IDLE", "fps": True}, {"type": "IDLE", "unknown": 1},
    {"type": "TURN", "root_mode": "IN_PLACE", "root_motion": True}, {"type": "IDLE", "impact_direction": [0, 0, 0]},
    {"type": "LOCOMOTION", "duration_s": 2, "cycle_duration_s": 1},
    {"type": "LOCOMOTION", "speed_m_s": 1, "forward_distance_m": 3, "duration_s": 2}])
def test_invalid_or_ambiguous_intents_reject_before_any_generation(intent):
    with pytest.raises(ValueError):
        create_brief(intent)


@pytest.mark.parametrize("kind", ["IDLE", "ATTACK", "LOCOMOTION", "TURN", "HIT_REACTION", "START_STOP"])
def test_each_required_type_produces_finite_ordered_nonmutating_plan(kind):
    brief = create_brief({"type": kind, "fps": 60, "duration_s": 2.})
    original = deepcopy(brief); plan = plan_poses(brief)
    assert brief == original and plan["apply"] is False and plan["supported"] is True
    assert plan["poses"][0]["frame"] == 1 and plan["poses"][-1]["frame"] == 121
    assert all(a["frame"] < b["frame"] for a, b in zip(plan["poses"], plan["poses"][1:]))
    assert all(p["interpolation"] == "CONSTANT" for p in plan["poses"])
    assert plan["breakdowns"] and plan["capacity"]["payload_bytes"] < 200000
    assert "TEMPORAL_VISUAL_QA" in plan["pipeline"] and plan["rig_validation"] == "required"


def test_heavy_phase_allocation_is_not_uniform_time_scaling():
    light = plan_poses(create_brief({"type": "ATTACK", "duration_s": 3, "profile": "HUMAN"}))
    heavy = plan_poses(create_brief({"type": "ATTACK", "duration_s": 3, "profile": "HEAVY_HUMANOID"}))
    a = analyze_timing(light)["fractions"]; b = analyze_timing(heavy)["fractions"]
    assert b["NEUTRAL"] > a["NEUTRAL"]  # time to maximum anticipation
    assert b["STRIKE"] < a["STRIKE"]  # shorter impact event
    assert b["FOLLOW_THROUGH"] > a["FOLLOW_THROUGH"]  # longer recovery
    assert light["brief"]["duration_s"] == heavy["brief"]["duration_s"]
    commitment = next(p for p in heavy["poses"] if p["phase"] == "COMMITMENT")["semantic"]
    assert commitment["pelvis_offset_m"][1] > .025


def test_asymmetric_mass_changes_compensation_without_changing_pose_schedule_or_rig():
    symmetric = plan_poses(create_brief({"type": "ATTACK", "profile": "CREATURE"}))
    asymmetric = plan_poses(create_brief({"type": "ATTACK", "profile": "ASYMMETRIC_CREATURE", "mass_asymmetry": {"left": 1, "right": 4}}))
    middle_a = symmetric["poses"][3]["semantic"]; middle_b = asymmetric["poses"][3]["semantic"]
    assert middle_a["pelvis_offset_m"][0] != middle_b["pelvis_offset_m"][0]
    assert middle_a["torso_roll_deg"] != middle_b["torso_roll_deg"]
    assert middle_b["arm_swing_deg"]["right"] < middle_a["arm_swing_deg"]["right"]
    assert [p["frame"] for p in symmetric["poses"]] == [p["frame"] for p in asymmetric["poses"]]


@pytest.mark.parametrize("root", ["IN_PLACE", "ROOT_MOTION"])
def test_locomotion_closes_root_relative_pose_and_declares_world_stationary_anchors(root):
    plan = plan_poses(create_brief({"type": "LOCOMOTION", "root_mode": root, "forward_distance_m": .6, "duration_s": 2}))
    first = plan["poses"][0]["semantic"]; last = plan["poses"][-1]["semantic"]
    delta = last["root_translation_m"][1]-first["root_translation_m"][1]
    assert math.isclose(delta, .6 if root == "ROOT_MOTION" else 0., abs_tol=1e-8)
    for side in ("left", "right"):
        assert math.isclose(last["feet"][side]["offset_m"][1]-first["feet"][side]["offset_m"][1], delta, abs_tol=1e-8)
    dense = sorted(plan["poses"]+plan["breakdowns"], key=lambda p: p["frame"])
    for a, b in zip(dense, dense[1:]):
        for side in ("left", "right"):
            foot_a = a["semantic"]["feet"][side]; foot_b = b["semantic"]["feet"][side]
            if foot_a["contact"] and foot_b["contact"] and foot_a["anchor_m"] == foot_b["anchor_m"]:
                assert foot_a["offset_m"] == foot_b["offset_m"]
    assert all(c["requires_geometric_confirmation"] for c in plan["contact_intervals"])


@pytest.mark.parametrize("profile", ["HUMAN", "HEAVY_HUMANOID"])
def test_locomotion_elbow_flexion_has_smooth_periodic_seam_and_same_event_range(profile):
    from blender_mcp.production.animation.generation.planner import _semantic
    brief = create_brief({"type": "LOCOMOTION", "profile": profile, "duration_s": 2.})
    epsilon = 1e-3
    for side in ("left", "right"):
        def elbow(fraction):
            return _semantic(brief, "CONTACT", fraction)["elbow_flex_deg"][side]
        outgoing = (elbow(epsilon)-elbow(0.))/epsilon/brief["duration_s"]
        incoming = (elbow(1.)-elbow(1.-epsilon))/epsilon/brief["duration_s"]
        assert abs(incoming-outgoing) < .001
        # Flat curvature at the boundary avoids a sampled endpoint acceleration
        # spike while retaining the same interior flexion extrema.
        first_curvature = (elbow(epsilon)-2*elbow(0.)+elbow(-epsilon))/epsilon**2/brief["duration_s"]**2
        last_curvature = (elbow(1.+epsilon)-2*elbow(1.)+elbow(1.-epsilon))/epsilon**2/brief["duration_s"]**2
        assert abs(first_curvature) < .1
        assert first_curvature == pytest.approx(last_curvature, abs=1e-7)
        assert elbow(0.) == pytest.approx(elbow(1.))
        assert elbow(.125) == pytest.approx(16.)
        assert elbow(.25) == pytest.approx(12.)
        assert all(12. <= elbow(index/256) <= 16. for index in range(257))


def test_impact_direction_controls_body_reaction_and_recovery_has_no_teleport():
    left = plan_poses(create_brief({"type": "HIT_REACTION", "impact_direction": "LEFT"}))
    right = plan_poses(create_brief({"type": "HIT_REACTION", "impact_direction": "RIGHT"}))
    la = next(p for p in left["poses"] if p["phase"] == "ABSORPTION")["semantic"]
    ra = next(p for p in right["poses"] if p["phase"] == "ABSORPTION")["semantic"]
    assert la["pelvis_offset_m"][0] == -ra["pelvis_offset_m"][0] and la["pelvis_offset_m"][0] > 0
    assert left["poses"][0]["semantic"]["root_translation_m"] == [0., 0., 0.]
    assert left["poses"][-1]["semantic"]["pelvis_offset_m"] == [0., 0., 0.]


def test_capacity_refuses_large_rig_before_candidate_or_checkpoint():
    plan = plan_poses(create_brief({"type": "ATTACK", "duration_s": 10, "fps": 60}), {"supported": True, "bone_count": 2048})
    assert plan["supported"] is False and plan["capacity"]["supported"] is False
    assert any("CAPACITY_EXCEEDED" in error for error in plan["errors"])
    assert plan["apply"] is False


def test_realistic_hundred_bone_rig_has_bounded_supported_estimate():
    plan = plan_poses(create_brief({"type": "ATTACK", "duration_s": 10, "fps": 60}), {"supported": True, "bone_count": 120, "controlled_bone_count": 18})
    assert plan["supported"] and plan["capacity"]["estimated_samples_per_pass"] < 100000
    assert plan["capacity"]["estimated_keyframes"] < 100000


def test_unsupported_compatibility_is_not_overridden_by_a_valid_plan():
    plan = plan_poses(create_brief({"type": "IDLE"}), {"supported": False, "bone_count": 24})
    assert not plan["supported"] and "UNSUPPORTED_GENERATION_RIG" in plan["errors"][0]


def test_phase_customization_is_explicit_and_monotonic():
    fractions = {"NEUTRAL": 0., "ANTICIPATION": .2, "COMMITMENT": .4, "STRIKE": .5, "IMPACT": .6, "FOLLOW_THROUGH": .8, "RECOVERY": 1.}
    plan = plan_poses(create_brief({"type": "ATTACK", "phase_fractions": fractions}))
    assert next(p for p in plan["poses"] if p["phase"] == "IMPACT")["fraction"] == .6
    fractions["COMMITMENT"] = .1
    with pytest.raises(ValueError, match="increase strictly"):
        plan_poses(create_brief({"type": "ATTACK", "phase_fractions": fractions}))


def test_breakdowns_have_pelvis_lead_and_torso_lag_instead_of_equal_lerp():
    plan = plan_poses(create_brief({"type": "ATTACK"}))
    a, b = plan["poses"][1:3]
    middle = next(p for p in plan["breakdowns"] if p["between"] == [a["phase"], b["phase"]])["semantic"]
    assert middle["pelvis_offset_m"][1] != (a["semantic"]["pelvis_offset_m"][1]+b["semantic"]["pelvis_offset_m"][1])/2
    assert middle["torso_pitch_deg"] != (a["semantic"]["torso_pitch_deg"]+b["semantic"]["torso_pitch_deg"])/2


def test_turn_uses_explicit_body_basis_and_coarse_supported_step_contacts():
    plan = plan_poses(create_brief({"intent": "turn 90 degrees right"}))
    assert plan["poses"][-1]["semantic"]["root_yaw_deg"] == 90
    assert any(not p["semantic"]["feet"]["left"]["contact"] for p in plan["poses"]+plan["breakdowns"])
    assert plan["provenance"]["turn_degrees"]["source"] == "user_requirement"


def test_trajectory_metrics_report_physical_units_and_reversal():
    metrics = trajectory_metrics([0, 30, 60], [[0, 0, 0], [1, 0, 0], [0, 0, 0]], fps=30)
    assert metrics["distance_m"] == 2 and metrics["net_displacement_m"] == 0
    assert metrics["max_speed_m_s"] == 1 and metrics["max_turn_degrees"] == 180
    assert metrics["path_to_chord_ratio"] is None


def test_quantization_error_is_reported_and_tampered_frame_end_rejects():
    brief = create_brief({"type": "IDLE", "duration_s": 1.111, "fps": 30})
    assert abs(brief["quantization_error_s"]) <= .5/30
    brief["frame_end"] += 1
    with pytest.raises(ValueError, match="frame_end"):
        plan_poses(brief)


def test_normalized_brief_is_idempotent_preserves_provenance_and_uses_actual_frame_time():
    brief = create_brief({"intent": "heavy overhead attack", "duration_s": 1.111, "fps": 30})
    assert create_brief(brief) == brief
    plan = plan_poses(brief)
    for pose in plan["poses"] + plan["breakdowns"]:
        assert pose["time_s"] == (pose["frame"]-brief["frame_start"])/brief["fps"]
    changed = deepcopy(brief); changed["duration_s"] = 2.
    with pytest.raises(ValueError):
        create_brief(changed)


def test_custom_pose_nan_or_unbound_planted_anchor_rejects_before_runtime():
    template = plan_poses(create_brief({"type": "IDLE"}))["poses"][0]["semantic"]
    bad = deepcopy(template); bad["root_translation_m"][0] = float("nan")
    intent = {"type": "IDLE", "profile": "CUSTOM", "custom_pose_plan": [
        {"fraction": 0., "phase": "BASE", "semantic": bad}, {"fraction": 1., "phase": "END", "semantic": template}]}
    with pytest.raises(ValueError):
        plan_poses(create_brief(intent))
    intent["custom_pose_plan"][0]["semantic"] = deepcopy(template)
    intent["custom_pose_plan"][0]["semantic"]["feet"]["left"]["anchor_m"] = None
    with pytest.raises(ValueError, match="anchor"):
        plan_poses(create_brief(intent))
