"""Definition invariants only; actual IK solver qualification is probe_m6.py."""
import importlib.util
import math
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("fixtures_m6", Path(__file__).resolve().parents[1] / "blender" / "fixtures_m6.py")
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)


@pytest.mark.parametrize("variant", ["humanoid", "heavy", "asymmetric", "constrained"])
def test_body_mapping_resolves_structure_without_caller_confidence(variant):
    rows = fixtures.bone_definitions(variant)
    names = {row[0] for row in rows}
    mapping = fixtures.semantic_mapping(variant)
    assert len(names) == len(rows) == 21
    assert set(mapping.values()) == names
    assert all(isinstance(value, str) for value in mapping.values())
    assert "confidence" not in mapping
    lookup = {row[0]: row for row in rows}
    assert lookup[mapping["root"]][3] is None
    assert lookup[mapping["pelvis"]][3] == mapping["root"]
    for side in ("l", "r"):
        assert lookup[mapping["lower_leg_" + side]][3] == mapping["upper_leg_" + side]
        assert lookup[mapping["foot_" + side]][3] == mapping["lower_leg_" + side]
        assert lookup[mapping["lower_arm_" + side]][3] == mapping["upper_arm_" + side]
        assert lookup[mapping["hand_" + side]][3] == mapping["lower_arm_" + side]
        assert lookup[mapping["foot_control_" + side]][3] is None
        assert lookup[mapping["pole_" + side]][3] is None
        assert not lookup[mapping["foot_control_" + side]][4]
        assert not lookup[mapping["pole_" + side]][4]


@pytest.mark.parametrize("variant", ["humanoid", "heavy", "asymmetric", "constrained", "unsupported"])
def test_fixture_bones_are_finite_nonzero_and_parents_exist(variant):
    rows = fixtures.bone_definitions(variant)
    seen = set()
    for name, head, tail, parent, deform in rows:
        assert all(math.isfinite(value) for value in (*head, *tail))
        assert sum((a - b) ** 2 for a, b in zip(head, tail)) > 1e-8
        assert parent is None or parent in seen
        assert isinstance(deform, bool)
        seen.add(name)


def test_independent_ik_targets_match_foot_markers_and_configured_ground():
    lookup = {row[0]: row for row in fixtures.bone_definitions()}
    for side in ("L", "R"):
        assert lookup["foot." + side][1] == lookup["foot_ik." + side][1]
        assert lookup["foot." + side][1][2] == fixtures.GROUND_HEIGHT_M
        assert lookup["shin." + side][2] == lookup["foot." + side][1]
        assert lookup["thigh." + side][2][1] < 0
        assert lookup["pole." + side][1][1] < lookup["thigh." + side][2][1]
        assert lookup["foot." + side][2][1] < 0


def test_asymmetry_is_geometry_requirement_not_a_pose_normalization():
    rows = {row[0]: row for row in fixtures.bone_definitions("asymmetric")}
    def length(name):
        row = rows[name]
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(row[1], row[2])))
    right = sum(length(name + ".R") for name in ("upper_arm", "forearm", "hand"))
    left = sum(length(name + ".L") for name in ("upper_arm", "forearm", "hand"))
    assert right > left * 1.25
    assert rows["foot.L"][1][2] == rows["foot.R"][1][2]


def test_required_scenarios_and_performance_rigs_are_real_definitions():
    specs = fixtures.scenario_specifications()
    assert len(fixtures.REQUIRED_SCENARIOS) == 15
    assert set(fixtures.REQUIRED_SCENARIOS) <= set(specs)
    assert specs["unsupported_rig"]["expected_compatibility"] == "unsupported"
    assert specs["constrained_rig"]["expected_compatibility"] == "unsupported"
    assert specs["in_place_rig"]["root_mode"] == "IN_PLACE"
    assert len(fixtures.bone_definitions(extra_bones=specs["medium_rig"]["extra_bones"])) == 62
    assert len(fixtures.bone_definitions(extra_bones=specs["large_rig"]["extra_bones"])) == 131
    assert fixtures.semantic_mapping("unsupported") == {}


@pytest.mark.parametrize("extra", [-1, 201, True, 3.5, "10"])
def test_extra_bone_budget_is_explicit_and_bounded(extra):
    with pytest.raises(ValueError):
        fixtures.bone_definitions(extra_bones=extra)


def test_unknown_fixture_variant_rejected():
    with pytest.raises(ValueError):
        fixtures.bone_definitions("production_asset")
