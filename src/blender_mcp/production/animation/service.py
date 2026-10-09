"""Bridge-facing operations; inputs are data, never interpolated Python code."""

import inspect
import math

from . import algorithms, scripts


def _pagination(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")


def analyze(bridge, object_name=None, action_name=None, limit=100, offset=0):
    """Summarize Action channels, slots, keys, interpolation and scene timing.

    object_name selects its active Action and slot; omit it to inspect a named
    Action across every slot. NLA composition is reported, not flattened.
    """
    _pagination(limit, offset)
    return bridge.run(scripts.ANALYZE, {"object_name": object_name,
        "action_name": action_name, "limit": limit, "offset": offset})


def detect_quaternion_flips(bridge, object_name=None, action_name=None,
                            limit=100, offset=0):
    """Analyze complete WXYZ F-Curves without modifying keys or current frame."""
    _pagination(limit, offset)
    body = "import math\n" + inspect.getsource(algorithms.quaternion_report) + scripts.QUATERNIONS
    return bridge.run(body, {"object_name": object_name, "action_name": action_name,
                             "limit": limit, "offset": offset})


def detect_foot_sliding(bridge, armature, bones=None, frame_start=None,
                        frame_end=None, step=1, ground_height_m=0.0,
                        contact_height_m=0.03, contact_speed_m_s=0.25,
                        sliding_tolerance_m=0.01, min_contact_frames=3):
    """Measure inferred contacts with configurable physical thresholds.

    Defaults are review starting points: 3 cm marker height tolerance, 0.25 m/s
    VERTICAL contact speed, 1 cm horizontal slip tolerance, 3-frame duration.
    ground_height_m is the marker's ground elevation, not an inferred mesh sole.
    Bone-head positions include evaluated constraints, NLA and object motion.
    Sampling is bounded and frame/subframe are restored even on failure.
    """
    if not isinstance(armature, str) or not armature:
        raise ValueError("armature must name an armature object")
    if bones is not None and (not isinstance(bones, (list, tuple)) or not bones
        or len(bones) > 32 or any(not isinstance(bone, str) or not bone for bone in bones)
        or len(set(bones)) != len(bones)):
        raise ValueError("bones must contain 1 to 32 distinct bone names")
    if type(step) is not int or not 1 <= step <= 1000:
        raise ValueError("step must be an integer between 1 and 1000")
    if type(min_contact_frames) is not int or min_contact_frames < 2:
        raise ValueError("min_contact_frames must be an integer >= 2")
    for value in (frame_start, frame_end):
        if value is not None and (type(value) is not int or not -1048574 <= value <= 1048574):
            raise ValueError("frame bounds must be integers within Blender's supported range")
    if frame_start is not None and frame_end is not None and frame_end < frame_start:
        raise ValueError("frame_end must be >= frame_start")
    for name, value in {"ground_height_m": ground_height_m,
                        "contact_height_m": contact_height_m,
                        "contact_speed_m_s": contact_speed_m_s,
                        "sliding_tolerance_m": sliding_tolerance_m}.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(name + " must be a finite number")
        if name != "ground_height_m" and value < 0:
            raise ValueError(name + " cannot be negative")
    body = "import math\n" + scripts.STATE_RUNTIME + inspect.getsource(algorithms.contact_report) + scripts.FOOT_SLIDING
    return bridge.run(body, {"armature": armature, "bones": bones,
        "frame_start": frame_start, "frame_end": frame_end, "step": step,
        "ground_height_m": ground_height_m, "contact_height_m": contact_height_m,
        "contact_speed_m_s": contact_speed_m_s, "sliding_tolerance_m": sliding_tolerance_m,
        "min_contact_frames": min_contact_frames})
