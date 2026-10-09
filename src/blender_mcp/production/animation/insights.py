"""Conservative motion analysis and correction planning, never mutation.

Sampled tracks remain internal to the MCP process. Public reports contain
bounded metrics, frame intervals, measured findings and explicit uncertainty.
"""
from __future__ import annotations

import math
from statistics import median

from .algorithms import contact_report
from . import insights_scripts
from .service import analyze, detect_quaternion_flips
from ..rig import rig_analyze


KINDS = frozenset({"contacts", "control_mode", "root_motion", "balance", "motion_quality", "refinement_plan"})
DEFAULTS = {
    "frame_start": None, "frame_end": None, "step": 1, "max_samples": 1000,
    "bones": None, "action_name": None, "root_bone": None, "root_object": False,
    "pelvis_bone": None, "detail": "summary", "limit": 50,
    "ground_height_m": 0.0, "contact_height_m": 0.03, "contact_speed_m_s": 0.25,
    "sliding_tolerance_m": 0.01, "min_contact_frames": 3,
    "support_radius_m": 0.08, "support_bones": None,
    "acceleration_threshold_m_s2": 20.0, "jerk_threshold_m_s3": 200.0,
    "jump_distance_m": 0.25, "derivative_ratio": 4.0,
    "angular_acceleration_threshold_deg_s2": 2000.0,
    "loop_position_tolerance_m": 0.01, "loop_angle_tolerance_degrees": 1.0,
    "vertical_tolerance_m": 0.05,
    "fcurve_options": None,
}


def validate_options(kind, object_name, options):
    if kind not in KINDS:
        raise ValueError("Unknown animation analysis kind: " + str(kind))
    if not isinstance(object_name, str) or not object_name.strip() or len(object_name) > 1024:
        raise ValueError("object_name must be a nonempty object name")
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    unknown = set(options) - DEFAULTS.keys()
    if unknown:
        raise ValueError("Unknown analysis options: " + ", ".join(sorted(unknown)))
    result = dict(DEFAULTS, **options)
    for name, lower, upper in [("step", 1, 1000), ("max_samples", 2, 2000),
                                ("limit", 1, 100), ("min_contact_frames", 2, 10000)]:
        value = result[name]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer in {lower}..{upper}")
    for name in ("frame_start", "frame_end"):
        value = result[name]
        if value is not None and (type(value) is not int or not -1048574 <= value <= 1048574):
            raise ValueError(name + " must be a valid integer Blender frame")
    if result["frame_start"] is not None and result["frame_end"] is not None and result["frame_end"] < result["frame_start"]:
        raise ValueError("frame_end must be >= frame_start")
    for name in ("bones", "support_bones"):
        value = result[name]
        maximum = 32 if name == "bones" else 2
        if value is not None and (not isinstance(value, list) or not 1 <= len(value) <= maximum
                or any(not isinstance(item, str) or not item or len(item) > 1024 for item in value)
                or len(set(value)) != len(value)):
            raise ValueError(name + f" must be a list of 1..{maximum} distinct bone names")
    for name in ("action_name", "root_bone", "pelvis_bone"):
        value = result[name]
        if value is not None and (not isinstance(value, str) or not value or len(value) > 1024):
            raise ValueError(name + " must be a nonempty name or null")
    if type(result["root_object"]) is not bool:
        raise ValueError("root_object must be boolean")
    if result["root_object"] and result["root_bone"]:
        raise ValueError("root_object and root_bone are mutually exclusive")
    if result["detail"] not in {"summary", "events"}:
        raise ValueError("detail must be summary or events")
    if result["fcurve_options"] is not None and not isinstance(result["fcurve_options"], dict):
        raise ValueError("fcurve_options must be an object or null")
    for name in DEFAULTS:
        if name.endswith(("_m", "_s2", "_s3", "_s", "_degrees")) or name == "derivative_ratio":
            value = result[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(name + " must be finite")
            if name != "ground_height_m" and value < 0:
                raise ValueError(name + " must be nonnegative")
    if result["derivative_ratio"] <= 1:
        raise ValueError("derivative_ratio must exceed one")
    if result["support_bones"]:
        result["bones"] = sorted(set(result["bones"] or []) | set(result["support_bones"]))
        if len(result["bones"]) > 32:
            raise ValueError("Combined bone selection exceeds 32")
    return result


def _distance(a, b):
    return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)))


def _angle(a, b):
    if len(a) != 4 or len(b) != 4 or any(not math.isfinite(v) for v in (*a, *b)):
        return None
    norms = math.sqrt(sum(v*v for v in a) * sum(v*v for v in b))
    if not math.isfinite(norms) or norms < 1e-12:
        return None
    dot = min(1.0, max(-1.0, sum(x*y for x, y in zip(a, b))/norms))
    return math.degrees(2*math.acos(abs(dot)))


def _motion(frames, track, fps, options):
    points = track["positions_m"]
    quats = track["quaternions"]
    if len(points) != len(frames) or len(quats) != len(frames):
        raise ValueError("Motion sample counts must match frames")
    if any(len(point) != 3 or any(not math.isfinite(v) for v in point) for point in points):
        raise ValueError("Motion positions must contain finite XYZ samples")
    if fps <= 0 or any(b <= a for a, b in zip(frames, frames[1:])):
        raise ValueError("Motion frames must increase and FPS must be positive")
    elapsed = [(b-a)/fps for a, b in zip(frames, frames[1:])]
    velocity = [[(b[axis]-a[axis])/dt for axis in range(3)] for a, b, dt in zip(points, points[1:], elapsed)]
    speeds = [math.sqrt(sum(v*v for v in vector)) for vector in velocity]
    acceleration = [[(b[axis]-a[axis])/((left+right)/2) for axis in range(3)]
                    for a, b, left, right in zip(velocity, velocity[1:], elapsed, elapsed[1:])]
    accel = [math.sqrt(sum(v*v for v in vector)) for vector in acceleration]
    jerk = [_distance(a, b)/((elapsed[i]+2*elapsed[i+1]+elapsed[i+2])/4)
            for i, (a, b) in enumerate(zip(acceleration, acceleration[1:]))]
    angles = [_angle(a, b) for a, b in zip(quats, quats[1:])]
    angular = [angle/dt if angle is not None else None for angle, dt in zip(angles, elapsed)]
    angular_accel = [abs(b-a)/((elapsed[i]+elapsed[i+1])/2)
                     if a is not None and b is not None else None
                     for i, (a, b) in enumerate(zip(angular, angular[1:]))]
    typical_accel = median(accel) if accel else 0.0
    typical_jerk = median(jerk) if jerk else 0.0
    typical_angular = median([a for a in angular_accel if a is not None]) if any(a is not None for a in angular_accel) else 0.0
    findings = []
    for i, value in enumerate(accel):
        if value > options["acceleration_threshold_m_s2"] and value > options["derivative_ratio"]*max(typical_accel, 1e-6):
            findings.append({"type": "abrupt_acceleration_review", "frame": frames[i+1],
                             "value_m_s2": value, "severity": "review", "confidence": 0.65})
    for i, value in enumerate(jerk):
        if value > options["jerk_threshold_m_s3"] and value > options["derivative_ratio"]*max(typical_jerk, 1e-6):
            findings.append({"type": "abrupt_derivative_change_review", "frames": [frames[i+1], frames[i+2]],
                             "value_m_s3": value, "severity": "review", "confidence": 0.6})
    for i, value in enumerate(angular_accel):
        if value is not None and value > options["angular_acceleration_threshold_deg_s2"] and value > options["derivative_ratio"]*max(typical_angular, 1e-6):
            findings.append({"type": "angular_acceleration_review", "frame": frames[i+1],
                             "value_deg_s2": value, "severity": "review", "confidence": 0.6})
    distances = [_distance(a, b) for a, b in zip(points, points[1:])]
    typical_distance = median(distances) if distances else 0
    for i, value in enumerate(distances):
        if value > options["jump_distance_m"] and value > options["derivative_ratio"]*max(typical_distance, 1e-6):
            findings.append({"type": "translation_jump_review", "frames": [frames[i], frames[i+1]],
                             "distance_m": value, "severity": "review", "confidence": 0.7})
    delta = [b-a for a, b in zip(points[0], points[-1])] if points else [0, 0, 0]
    duration = (frames[-1]-frames[0])/fps if len(frames) > 1 else 0
    path = sum(distances)
    total_euler = [0.0, 0.0, 0.0]
    for previous, current in zip(track.get("euler_xyz", []), track.get("euler_xyz", [])[1:]):
        for axis in range(3):
            total_euler[axis] += math.degrees((current[axis]-previous[axis]+math.pi) % (2*math.pi)-math.pi)
    direction_changes = []
    for i, (a, b) in enumerate(zip(velocity, velocity[1:])):
        norm = math.hypot(a[0], a[1])*math.hypot(b[0], b[1])
        if norm > 1e-6:
            change = math.degrees(math.acos(max(-1, min(1, (a[0]*b[0]+a[1]*b[1])/norm))))
            if change > 45:
                direction_changes.append({"frame": frames[i+1], "degrees": change})
    closing_angle = _angle(quats[0], quats[-1]) if quats else None
    loop = ("endpoints_match" if _distance(points[0], points[-1]) <= options["loop_position_tolerance_m"]
            and closing_angle is not None and closing_angle <= options["loop_angle_tolerance_degrees"] else "endpoints_differ") if len(frames) > 1 else "inconclusive"
    return {"translation_xyz_m": delta, "distance_m": path,
            "horizontal_distance_m": sum(math.hypot(b[0]-a[0], b[1]-a[1]) for a, b in zip(points, points[1:])),
            "vertical_displacement_m": delta[2], "vertical_range_m": max((p[2] for p in points), default=0)-min((p[2] for p in points), default=0),
            "duration_seconds": duration, "average_velocity_m_s": path/duration if duration else 0,
            "peak_velocity_m_s": max(speeds, default=0), "peak_acceleration_m_s2": max(accel, default=0),
            "peak_jerk_m_s3": max(jerk, default=0), "peak_angular_velocity_deg_s": max((v for v in angular if v is not None), default=0),
            "peak_angular_acceleration_deg_s2": max((v for v in angular_accel if v is not None), default=0),
            "accumulated_euler_xyz_degrees": total_euler, "endpoint_angular_delta_degrees": closing_angle,
            "loop_status": loop, "direction_change_count": len(direction_changes),
            "direction_changes": direction_changes[:options["limit"]], "findings": findings[:options["limit"]],
            "finding_count": len(findings), "findings_truncated": len(findings) > options["limit"],
            "invalid_quaternion_pairs": sum(angle is None for angle in angles)}


def _contacts(sample, options):
    if not sample["is_armature"]:
        return {"status": "inconclusive", "effectors": [], "metrics": {"effector_count": 0},
                "warnings": ["Contact analysis requires an armature and selected/evidenced markers"]}
    reports = []
    frames, fps = sample["frames"], sample["fps"]
    for candidate in sample["effectors"]:
        points = sample["tracks"][candidate["bone"]]["positions_m"]
        measurement = contact_report(frames, points, fps, options["ground_height_m"], options["contact_height_m"],
            options["contact_speed_m_s"], options["sliding_tolerance_m"], options["min_contact_frames"], options["limit"])
        intervals = []
        for interval in measurement["contacts"]:
            indices = [i for i, frame in enumerate(frames) if interval["frame_start"] <= frame <= interval["frame_end"]]
            heights = [points[i][2] - options["ground_height_m"] for i in indices]
            vertical_speeds = [abs(points[b][2]-points[a][2])*fps/(frames[b]-frames[a]) for a,b in zip(indices, indices[1:])]
            height_margin = 1-min(1, sum(abs(height) for height in heights)/len(heights)/max(options["contact_height_m"], 1e-9))
            speed_margin = 1-min(1, max(vertical_speeds, default=0)/max(options["contact_speed_m_s"], 1e-9))
            contact_confidence = round(0.45+0.2*height_margin+0.2*speed_margin, 3)
            intervals.append({**interval, "effector": candidate["bone"], "average_height_m": sum(heights)/len(heights),
                "max_vertical_velocity_m_s": max(vertical_speeds, default=0),
                "average_vertical_velocity_m_s": sum(vertical_speeds)/len(vertical_speeds) if vertical_speeds else 0,
                "contact_confidence": contact_confidence, "suspected_sliding_m": interval["translation_during_contact_m"],
                "warnings": ["Marker proximity and low vertical speed imply possible contact, not physical proof"]})
        reports.append({**{key:candidate[key] for key in ("bone", "likely_effector", "anatomical_side", "confidence", "evidence", "ik_targets")},
            "selection": candidate.get("selection", "heuristic"), "status": measurement["status"],
            "contact_count": measurement["contact_count"], "failed_contact_count": measurement["failed_contact_count"],
            "max_sliding_m": measurement["max_sliding_m"], "intervals": intervals,
            "intervals_truncated": measurement["contacts_truncated"],
            "behavior_evidence": ["Observed near-plane slow vertical movement"] if intervals else ["No contact interval met thresholds"]})
    return {"status": "measured" if any(item["contact_count"] for item in reports) else "inconclusive",
        "surface": {"kind": "ground_plane", "normal": [0,0,1], "height_m": options["ground_height_m"]},
        "effectors": reports, "thresholds": {key:options[key] for key in ("contact_height_m", "contact_speed_m_s", "sliding_tolerance_m", "min_contact_frames")},
        "metrics": {"effector_count": len(reports), "contact_count": sum(item["contact_count"] for item in reports),
                    "failed_contact_count": sum(item["failed_contact_count"] for item in reports),
                    "max_sliding_m": max((item["max_sliding_m"] for item in reports if item["max_sliding_m"] is not None), default=None)},
        "warnings": ["Bone-head markers need ground calibration; hands may be intentional low gestures", "Slopes, stairs, moving surfaces, heel/toe roll and intentional hovering are not solved", "Anatomy, left/right labels and confidence are heuristic; explicit selection only confirms marker identity"]}


def _control_mode(rig, limit):
    limbs = []
    active = [chain for chain in rig["ik_chains"] if chain["active"]]
    ik_members = {bone for chain in active for bone in chain["bones_tip_to_root"]}
    for chain in rig["ik_chains"]:
        driven = bool(rig["drivers"])
        mode = "IK" if chain["active"] and chain.get("subtarget") else "uncertain"
        confidence = 0.9 if mode == "IK" and not driven else 0.6
        if 0 < next((c["influence"] for c in rig["constraints"] if c["name"] == chain["constraint"] and c["owner"] == chain["owner"]), 0) < 1:
            mode, confidence = "mixed", 0.7
        limbs.append({"limb": chain["owner"], "bones": chain["bones_tip_to_root"], "mode": mode,
            "controls": [{"object": chain["target"], "bone": chain.get("subtarget")}],
            "constraints": [chain["constraint"]], "confidence": confidence,
            "warnings": ["Mode describes current evaluated constraint influence; keyed or driven switches can change it"]})
    candidates = [chain for chain in rig["fk_chain_candidates"] if not set(chain.get("bones_root_to_tip", [])) & ik_members]
    for chain in candidates:
        names = chain.get("bones_root_to_tip", chain.get("bones", []))
        if names:
            limbs.append({"limb": names[0], "bones": names, "mode": "FK_candidate", "controls": [],
                "constraints": [], "confidence": 0.45, "warnings": ["Hierarchy alone cannot prove animator controls or absence of custom IK"]})
    if active and (len(ik_members) < rig["deform_bones"] or any(item["mode"] == "mixed" for item in limbs)):
        mode = "mixed"
    elif active:
        mode = "IK"
    elif rig["non_deform_bones"] == 0:
        mode = "deform_only"
    else:
        mode = "uncertain"
    return {"armature": rig["armature"], "mode": mode, "limbs": limbs[:limit],
        "confidence": 0.8 if active else 0.45, "metrics": {"limb_count": len(limbs), "explicit_active_ik_count": len(active)},
        "warnings": ["No IK/FK conversion or baking is performed", "Explicit IK target confidence does not imply that translating it is safe", "Analysis is of the current frame; switches may vary over the Action"]}


def _root(sample, options):
    name = sample["root_bone"]
    track = sample["tracks"][name] if name else sample["object_track"]
    candidate = next((item for item in sample["root_candidates"] if item["bone"] == name), None)
    confidence = 1.0 if options["root_bone"] or options["root_object"] else (candidate["confidence"] if candidate else 0.5)
    motion = _motion(sample["frames"], track, sample["fps"], options)
    warnings = ["World-space root motion includes object and parent transforms; object and bone motion are not added twice", "Euler XYZ accumulation is a coordinate summary, not a gimbal-free total rotation; shortest quaternion deltas can miss undersampled long turns", "Endpoint matching alone does not prove a seamless loop"]
    if motion["vertical_range_m"] > options["vertical_tolerance_m"]:
        warnings.append("Root vertical motion exceeds configured review tolerance; jumps may be intentional")
    return {"target": {"object": sample["object"], "bone": name}, "confidence": confidence,
            "confidence_kind": "explicit selection certainty" if options["root_bone"] or options["root_object"] else "hierarchy evidence score",
            "candidates": sample["root_candidates"], **motion,
            "metrics": {"distance_m": motion["distance_m"], "peak_velocity_m_s": motion["peak_velocity_m_s"], "finding_count": motion["finding_count"]},
            "thresholds": {key:options[key] for key in ("acceleration_threshold_m_s2", "jerk_threshold_m_s3", "jump_distance_m", "derivative_ratio", "loop_position_tolerance_m", "loop_angle_tolerance_degrees", "vertical_tolerance_m")},
            "warnings": warnings}


def _segment_distance(point, start, end):
    dx, dy = end[0]-start[0], end[1]-start[1]
    length2 = dx*dx+dy*dy
    t = max(0, min(1, ((point[0]-start[0])*dx+(point[1]-start[1])*dy)/length2)) if length2 else 0
    return math.hypot(point[0]-start[0]-t*dx, point[1]-start[1]-t*dy)


def _balance(sample, contacts, options):
    warnings = ["Heuristic root/pelvis projection against marker disks/capsule; not center of mass, force balance or biomechanics", "Support radius is a configurable proxy, not a measured foot shape; dynamic animation can be balanced outside this region"]
    selected = options["support_bones"]
    if selected is None:
        selected = [item["bone"] for item in contacts["effectors"] if item["likely_effector"] == "foot" and item["contact_count"]]
    if not sample["is_armature"] or not 1 <= len(selected) <= 2 or not sample["pelvis_bone"]:
        return {"status": "inconclusive", "confidence": 0.0, "metrics": {"suspicious_frame_count": 0}, "findings": [],
                "warnings": warnings+["Provide one or two validated support_bones and pelvis_bone for the biped proxy model"]}
    contact_lookup = {item["bone"]:item for item in contacts["effectors"]}
    projection = sample["tracks"][sample["pelvis_bone"]]["positions_m"]
    findings, no_support = [], 0
    for i, frame in enumerate(sample["frames"]):
        support = [name for name in selected if name in contact_lookup and any(interval["frame_start"] <= frame <= interval["frame_end"] for interval in contact_lookup[name]["intervals"])]
        if not support:
            no_support += 1
            continue
        points = [sample["tracks"][name]["positions_m"][i] for name in support]
        distance = _segment_distance(projection[i], points[0], points[-1])
        outside = distance-options["support_radius_m"]
        if outside > 1e-7:
            findings.append({"frame":frame, "support_bones": support, "outside_support_proxy_m": outside,
                             "type": "projection_outside_support_proxy", "severity": "review", "confidence": 0.55 if options["pelvis_bone"] else 0.35})
    truncated_contacts = any(item.get("intervals_truncated") for item in contacts["effectors"])
    if truncated_contacts:
        warnings.append("Contact interval details truncated; balance coverage incomplete. Narrow the frame range")
    return {"status": "heuristic_review" if findings else ("inconclusive" if truncated_contacts or no_support == len(sample["frames"]) else "no_proxy_outliers"),
        "confidence": 0.55 if options["pelvis_bone"] else 0.35, "model": "biped_marker_capsule",
        "pelvis_proxy": sample["pelvis_bone"], "support_bones": selected, "support_radius_m": options["support_radius_m"],
        "findings": findings[:options["limit"]], "findings_truncated": len(findings)>options["limit"],
        "metrics": {"suspicious_frame_count": len(findings), "unsupported_sample_count": no_support,
                    "max_outside_support_proxy_m": max((item["outside_support_proxy_m"] for item in findings), default=0)}, "warnings": warnings}


def _quality(sample, contacts, options):
    tracks = {"object":sample["object_track"], **sample["tracks"]}
    reports = [{"target": name, **_motion(sample["frames"], track, sample["fps"], options)} for name,track in tracks.items()]
    return {"tracks": reports[:options["limit"]], "repeated_key_intervals": sample["repeated_key_intervals"][:options["limit"]],
        "metrics": {"track_count": len(reports), "finding_count": sum(item["finding_count"] for item in reports),
            "peak_velocity_m_s": max((item["peak_velocity_m_s"] for item in reports), default=0),
            "stationary_contact_failed_count": contacts["metrics"].get("failed_contact_count", 0)},
        "thresholds": {key: options[key] for key in ("acceleration_threshold_m_s2", "jerk_threshold_m_s3", "angular_acceleration_threshold_deg_s2", "jump_distance_m", "derivative_ratio")},
        "warnings": ["Metrics are objective indicators, not artistic quality scores", "Fast or uniformly sampled motion is not automatically erroneous; findings require absolute and contextual derivative thresholds", "Identical key intervals often reflect intentional mocap sampling", "Shortest quaternion differences do not recover complete turns between sparse samples"]}


def run_analysis(bridge, kind: str, object_name: str, options: dict) -> dict:
    """Analyze current evaluated scene with bounded options; never assign Actions.

    ``options`` accepts the documented DEFAULTS keys. Physical thresholds use
    meters/seconds/degrees. ``action_name`` is a precondition, not an assignment.
    """
    opts = validate_options(kind, object_name, options)
    if kind == "control_mode":
        context = bridge.run(insights_scripts.CONTROL_CONTEXT, {"object_name":object_name,"action_name":opts["action_name"]})
        result = _control_mode(rig_analyze(bridge, object_name, limit=500), opts["limit"])
        result.update(context)
        if opts["frame_start"] is not None or opts["frame_end"] is not None:
            result["warnings"].append("frame_start/end do not change control-mode evaluation; this report describes the current frame only")
        return result
    sample = bridge.run(insights_scripts.SAMPLE, {"object_name":object_name, **opts})
    if not sample.get("frame_restored"):
        raise RuntimeError("Animation sampling did not restore frame/subframe")
    contacts = _contacts(sample, opts) if kind in {"contacts", "balance", "motion_quality", "refinement_plan"} else None
    if kind == "contacts":
        result = contacts
    elif kind == "root_motion":
        result = _root(sample, opts)
    elif kind == "balance":
        result = _balance(sample, contacts, opts)
    elif kind == "motion_quality":
        result = _quality(sample, contacts, opts)
    else:
        action = analyze(bridge, object_name=object_name, action_name=opts["action_name"], limit=opts["limit"])
        rig = _control_mode(rig_analyze(bridge, object_name, limit=500), opts["limit"]) if sample["is_armature"] else {"mode":"object", "confidence":1.0}
        quaternion = detect_quaternion_flips(bridge, object_name=object_name, action_name=opts["action_name"], limit=opts["limit"])
        from .curve_ops import analyze_fcurves
        curve_options = {"limit":opts["limit"], "detail":"summary"}
        for key in ("frame_start", "frame_end"):
            if opts[key] is not None:
                curve_options[key] = opts[key]
        curve_options.update(opts["fcurve_options"] or {})
        curves = analyze_fcurves(bridge, object_name, curve_options) if sample["action"] else {
            "status":"inconclusive", "findings":[], "metrics":{"finding_count":0},
            "warnings":["Target has no active Action; raw F-Curve analysis unavailable"]}
        root = _root(sample, opts)
        balance = _balance(sample, contacts, opts)
        quality = _quality(sample, contacts, opts)
        issues = []
        for effector in contacts["effectors"]:
            for interval in effector["intervals"]:
                if interval["status"] == "fail":
                    issues.append({"type":"foot_sliding" if effector["likely_effector"] == "foot" else "contact_sliding", "severity":"high", "effector":effector["bone"],
                        "frames":[interval["frame_start"],interval["frame_end"]], "measured_sliding_m":interval["suspected_sliding_m"],
                        "confidence":min(effector["confidence"],interval["contact_confidence"]),
                        "recommended_tool":"production.animation_fix_foot_sliding", "required_mode":"propose"})
        for group in quaternion.get("groups", []):
            if group.get("sign_discontinuity_count"):
                issues.append({"type":"quaternion_continuity", "severity":"review", "channel":group["data_path"],
                    "confidence":0.8, "recommended_tool":"production.animation_fix_quaternion_flips", "required_mode":"propose"})
        for finding in root["findings"]:
            issues.append({**finding,"recommended_tool":"production.animation_analyze_root_motion"})
        for finding in curves["findings"]:
            issues.append({**finding, "recommended_tool":"production.animation_cleanup_fcurves", "required_mode":"propose"})
        for motion in quality["tracks"]:
            if motion["target"] != (sample["root_bone"] or "object"):
                issues.extend({**finding,"target":motion["target"],"recommended_tool":"production.animation_analyze_motion_quality"} for finding in motion["findings"])
        if balance["metrics"].get("suspicious_frame_count"):
            issues.append({"type":"balance_proxy_outliers", "severity":"review", "confidence":balance["confidence"],
                           "recommended_tool":"production.animation_analyze_balance", "automatic_correction":False})
        ranks = {"high":0, "medium":1, "review":1, "low":2, "info":3}
        issues.sort(key=lambda item: (ranks.get(item["severity"], 4), -item.get("confidence",0), item["type"]))
        analyses = {"action":action, "control_mode":rig, "contacts":contacts, "quaternions":quaternion,
                    "fcurves":curves, "root_motion":root, "balance":balance, "motion_quality":quality}
        if opts["detail"] == "summary":
            analyses = {
                "action":{"metrics":action["metrics"], "scene":action["scene"], "warnings":action["warnings"]},
                "control_mode":rig,
                "contacts":{"metrics":contacts["metrics"], "surface":contacts.get("surface"),
                    "effectors":[{key:item[key] for key in ("bone","confidence","contact_count","max_sliding_m")} for item in contacts["effectors"]],
                    "warnings":contacts["warnings"]},
                "quaternions":{"metrics":quaternion["metrics"], "warnings":quaternion["warnings"]},
                "fcurves":{"metrics":curves["metrics"], "thresholds":curves.get("thresholds"), "warnings":curves["warnings"]},
                "root_motion":{key:root[key] for key in ("target","confidence","translation_xyz_m","horizontal_distance_m","loop_status","metrics","warnings")},
                "balance":{key:balance[key] for key in ("status","confidence","metrics","warnings")},
                "motion_quality":{"metrics":quality["metrics"],"thresholds":quality["thresholds"],"warnings":quality["warnings"]},
            }
        result = {"status":"plan_only", "action":sample["action"], "issues":issues[:opts["limit"]], "issues_truncated":len(issues)>opts["limit"],
            "recommended_order":["Inspect rig controls and validate marker/ground calibration", "Create checkpoint and duplicate Action", "Review quaternion and F-Curve proposals", "Review root motion and timing", "Review contact/sliding correction only for validated IK controls", "Re-measure motion, contacts and balance proxies", "Render equivalent before/after views and visually inspect", "Accept explicitly or restore original Action"],
            "analyses":analyses,
            "metrics":{"issue_count":len(issues)}, "warnings":["Plan only: no checkpoint, Action duplication, key edit or automatic correction has been performed", "Findings are ranked by measured threshold breaches and evidence, not an arbitrary animation-quality score"]}
    result.setdefault("metrics", {}).update({"sample_count":len(sample["frames"]), "bone_frame_evaluations":len(sample["tracks"])*len(sample["frames"])})
    result.update({"object":object_name, "action":sample["action"], "frame_range":[sample["frames"][0],sample["frames"][-1]],
                   "fps":sample["fps"], "sample_step":opts["step"], "frame_restored":True})
    result["warnings"] = list(dict.fromkeys(sample["warnings"] + result.get("warnings", [])))
    return result
