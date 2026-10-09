"""Measurements are tested independently of Blender or a socket connection."""

import math
import unittest

from blender_mcp.production.animation import analyze, detect_foot_sliding, detect_quaternion_flips
from blender_mcp.production.animation.algorithms import contact_report, quaternion_report
from blender_mcp.safe_mode import validate_code


class RecordingBridge:
    def __init__(self):
        self.calls = []

    def run(self, body, params):
        validate_code("params = " + repr(params) + "\n" + body)
        self.calls.append((body, params))
        return {"verified": True}


def contacts(points, frames=None, **kwargs):
    options = dict(fps=30.0, ground_height_m=0.0, contact_height_m=0.03,
                   contact_speed_m_s=0.25, sliding_tolerance_m=0.01,
                   min_contact_frames=3)
    options.update(kwargs)
    return contact_report(frames or list(range(1, len(points) + 1)), points, **options)


class ContactMeasurements(unittest.TestCase):
    def test_known_eighteen_mm_sliding(self):
        result = contacts([(i * 0.002, 0, 0) for i in range(10)])
        self.assertEqual(result["status"], "fail")
        contact = result["contacts"][0]
        self.assertAlmostEqual(contact["translation_during_contact_cm"], 1.8)
        self.assertEqual(contact["problematic_frames"], [7, 8, 9, 10])

    def test_stationary_foot_passes(self):
        result = contacts([(1, 2, 0)] * 10)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["max_sliding_m"], 0)

    def test_fast_horizontal_slide_is_not_rejected_as_airborne(self):
        result = contacts([(i * 0.1, 0, 0) for i in range(10)])
        self.assertEqual(result["contact_count"], 1)
        self.assertAlmostEqual(result["max_sliding_m"], 0.9)

    def test_no_contacts_is_inconclusive(self):
        self.assertEqual(contacts([(0, 0, 0.3)] * 10)["status"], "inconclusive")

    def test_single_frame_contact_cannot_pass(self):
        self.assertEqual(contacts([(0, 0, 0)])["status"], "inconclusive")

    def test_backtracking_is_not_hidden_by_net_zero(self):
        result = contacts([(0, 0, 0), (0.02, 0, 0), (0, 0, 0)])
        contact = result["contacts"][0]
        self.assertEqual(contact["status"], "fail")
        self.assertEqual(contact["net_translation_m"], 0)
        self.assertAlmostEqual(contact["horizontal_path_length_m"], 0.04)

    def test_configurable_threshold_and_marker_plane(self):
        result = contacts([(i * 0.002, 0, 0.1) for i in range(10)],
                          ground_height_m=0.1, sliding_tolerance_m=0.02)
        self.assertEqual(result["status"], "pass")

    def test_vertical_speed_blocks_ground_crossing(self):
        result = contacts([(0, 0, i * 0.01) for i in range(10)],
                          contact_height_m=0.2, contact_speed_m_s=0.2)
        self.assertEqual(result["status"], "inconclusive")

    def test_original_frames_determine_duration(self):
        result = contacts([(0, 0, 0)] * 3, frames=[10, 13, 16])
        self.assertEqual(result["contacts"][0]["frame_start"], 10)
        self.assertEqual(result["contacts"][0]["frame_end"], 16)
        self.assertAlmostEqual(result["contacts"][0]["duration_seconds"], 0.2)

    def test_event_details_bounded_but_summary_complete(self):
        result = contacts([(i * 0.001, 0, 0) for i in range(200)])
        contact = result["contacts"][0]
        self.assertEqual(len(contact["problematic_frames"]), 100)
        self.assertTrue(contact["problematic_frames_truncated"])
        self.assertEqual(contact["problematic_frame_count"], 189)

    def test_invalid_samples_rejected(self):
        for points in [[(math.nan, 0, 0)] * 3, [(0, 0)] * 3]:
            with self.assertRaises(ValueError):
                contacts(points)
        with self.assertRaises(ValueError):
            contacts([(0, 0, 0)] * 3, frames=[1, 1, 2])


class QuaternionMeasurements(unittest.TestCase):
    def test_antipodal_representations_are_same_orientation(self):
        result = quaternion_report([1, 2], [(1, 0, 0, 0), (-1, 0, 0, 0)])
        self.assertEqual(result["sign_discontinuity_count"], 1)
        self.assertEqual(result["orientation_jump_count"], 0)
        self.assertTrue(result["sign_discontinuities"][0]["equivalent_orientation"])
        self.assertEqual(result["max_orientation_delta_degrees"], 0)

    def test_real_rotation_jump_separate(self):
        result = quaternion_report([1, 2], [(1, 0, 0, 0), (0, 1, 0, 0)])
        self.assertEqual(result["sign_discontinuity_count"], 0)
        self.assertEqual(result["orientation_jump_count"], 1)
        self.assertEqual(result["max_orientation_delta_degrees"], 180)

    def test_normalization(self):
        result = quaternion_report([1, 2], [(4, 0, 0, 0), (2, 0, 0, 0)])
        self.assertEqual(result["max_orientation_delta_degrees"], 0)

    def test_invalid_quaternion_breaks_comparison_chain(self):
        result = quaternion_report([1, 2, 3], [(1, 0, 0, 0), (0, 0, 0, 0), (-1, 0, 0, 0)])
        self.assertEqual(result["invalid_frames"], [2])
        self.assertEqual(result["sign_discontinuity_count"], 0)

    def test_nonfinite_quaternion_is_reported(self):
        result = quaternion_report([1], [(math.inf, 0, 0, 0)])
        self.assertEqual(result["invalid_frames"], [1])

    def test_zero_and_one_sample_are_safe(self):
        self.assertEqual(quaternion_report([], [])["sign_discontinuity_count"], 0)
        self.assertEqual(quaternion_report([1], [(1, 0, 0, 0)])["orientation_jump_count"], 0)


class BridgeContracts(unittest.TestCase):
    def test_scripts_pass_safe_mode_without_exemptions(self):
        bridge = RecordingBridge()
        self.assertTrue(analyze(bridge)["verified"])
        self.assertTrue(detect_quaternion_flips(bridge)["verified"])
        self.assertTrue(detect_foot_sliding(bridge, "Rig", bones=["marker"])["verified"])

    def test_names_are_parameters_not_code_interpolation(self):
        bridge = RecordingBridge()
        name = 'A"; bpy.ops.wm.quit_blender() #'
        detect_foot_sliding(bridge, name, bones=[name])
        self.assertNotIn(name, bridge.calls[0][0])
        self.assertEqual(bridge.calls[0][1]["armature"], name)

    def test_sampling_inputs_rejected_before_bridge(self):
        bridge = RecordingBridge()
        for options in [dict(step=0), dict(step=True), dict(bones=[]),
                        dict(bones=["a", "a"]), dict(contact_height_m=-1),
                        dict(sliding_tolerance_m=math.nan), dict(frame_start=2, frame_end=1),
                        dict(frame_start=1.5), dict(min_contact_frames=1)]:
            with self.assertRaises(ValueError):
                detect_foot_sliding(bridge, "Rig", **options)
        self.assertEqual(bridge.calls, [])

    def test_pagination_bounds(self):
        for tool in (analyze, detect_quaternion_flips):
            for options in (dict(limit=0), dict(limit=501), dict(offset=-1), dict(limit=True)):
                with self.assertRaises(ValueError):
                    tool(RecordingBridge(), **options)


if __name__ == "__main__":
    unittest.main()
