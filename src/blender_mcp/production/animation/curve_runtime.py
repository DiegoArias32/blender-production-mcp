"""Bounded F-Curve analysis/correction code embedded in the existing safe bridge.

No transport, Action lifecycle, checkpoint or mutation of the source is owned
here. The operation coordinator provides a duplicate to ``m2_curve_apply``.
Tolerances use each channel's native units; angular error is in degrees.
"""
from __future__ import annotations

import inspect


def m2_curve_options(options):
    if options is not None and not isinstance(options, dict):
        raise ValueError("Curve options must be a mapping")
    options = dict(options or {})
    allowed = {"tolerance", "angular_tolerance_degrees", "spike_factor", "spike_min_amplitude",
               "noise_threshold", "samples_per_interval", "limit", "offset", "detail",
               "frame_start", "frame_end", "bones", "channels", "data_paths", "array_indices",
               "severity", "per_channel_tolerances", "strategies", "allow_smoothing",
               "allow_spike_correction", "allow_interpolation_change", "target_interpolation"}
    if set(options) - allowed:
        raise ValueError("Unknown curve options: " + ", ".join(sorted(set(options) - allowed)))
    for name in ("allow_smoothing", "allow_spike_correction", "allow_interpolation_change"):
        if name in options and not isinstance(options[name], bool):
            raise ValueError(name + " must be boolean")
    defaults = {"tolerance": 0.000001, "angular_tolerance_degrees": 0.05,
                "spike_factor": 6.0, "spike_min_amplitude": 0.01,
                "noise_threshold": 0.005, "samples_per_interval": 8,
                "limit": 100, "offset": 0, "detail": "summary"}
    for key, value in defaults.items():
        if key not in options:
            options[key] = value
    for name in ("tolerance", "angular_tolerance_degrees", "spike_factor",
                 "spike_min_amplitude", "noise_threshold"):
        value = options[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(name + " must be a finite nonnegative number")
    if options["angular_tolerance_degrees"] > 5 or options["spike_factor"] < 2:
        raise ValueError("angular_tolerance_degrees must be <= 5 and spike_factor >= 2")
    for name, lower, upper in (("samples_per_interval", 4, 64), ("limit", 1, 200), ("offset", 0, 1000000)):
        value = options[name]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(name + " is outside its supported integer range")
    for name in ("frame_start", "frame_end"):
        value = options.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError(name + " must be finite")
    if options.get("frame_start") is not None and options.get("frame_end") is not None and options["frame_end"] < options["frame_start"]:
        raise ValueError("frame_end must be >= frame_start")
    if options["detail"] not in ("summary", "full"):
        raise ValueError("detail must be summary or full")
    for name in ("bones", "channels", "data_paths"):
        values = options.get(name)
        if values is not None and (not isinstance(values, list) or not values or len(values) > 64 or any(not isinstance(v, str) or not v for v in values)):
            raise ValueError(name + " must contain 1..64 nonempty strings")
    severities = options.get("severity")
    if isinstance(severities, str):
        severities = [severities]
        options["severity"] = severities
    if severities is not None and (not isinstance(severities, list) or not severities or any(v not in ("info", "low", "medium", "high") for v in severities)):
        raise ValueError("severity must select info, low, medium or high")
    indices = options.get("array_indices")
    if indices is not None and (not isinstance(indices, list) or not indices or len(indices) > 32 or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 31 for v in indices)):
        raise ValueError("array_indices must contain indices 0..31")
    tolerances = options.get("per_channel_tolerances", {})
    if not isinstance(tolerances, dict) or len(tolerances) > 256:
        raise ValueError("per_channel_tolerances must be a bounded mapping")
    for key, value in tolerances.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Per-channel tolerances must be finite and nonnegative")
    return options


def m2_curve_identity(row):
    slot, layer, strip, curve = row
    return {"slot_handle": slot, "layer_index": layer, "strip_index": strip,
            "data_path": curve.data_path, "array_index": curve.array_index}


def m2_curve_key(identity):
    return (identity["slot_handle"], identity["layer_index"], identity["strip_index"],
            identity["data_path"], identity["array_index"])


def m2_curve_channel_key(curve):
    return curve.data_path + "[" + str(curve.array_index) + "]"


def m2_curve_tolerance(curve, options):
    return options.get("per_channel_tolerances", {}).get(m2_curve_channel_key(curve), options["tolerance"])


def m2_curve_in_range(frame, options):
    return (options.get("frame_start") is None or frame >= options["frame_start"]) and (options.get("frame_end") is None or frame <= options["frame_end"])


def m2_curve_rows(obj, action, options, filtered=True):
    slot = getattr(obj.animation_data, "action_slot", None) if obj.animation_data else None
    rows = action_curves(action, slot.handle if slot is not None else None)
    if len(rows) > 4096 or sum(len(row[3].keyframe_points) for row in rows) > 200000:
        raise ValueError("F-Curve budget exceeded: 4096 channels / 200000 keys")
    for row in rows:
        for point in row[3].keyframe_points:
            if not all(math.isfinite(float(value)) for vector in (point.co, point.handle_left, point.handle_right) for value in vector):
                raise ValueError("Non-finite key/handle value in " + row[3].data_path)
    if not filtered:
        return rows
    result_rows = []
    for row in rows:
        curve = row[3]
        if options.get("data_paths") and curve.data_path not in options["data_paths"]:
            continue
        if options.get("channels") and curve.data_path.rsplit(".", 1)[-1] not in options["channels"]:
            continue
        if options.get("bones") and not any(curve.data_path.startswith("pose.bones[" + json.dumps(bone, ensure_ascii=False) + "].") for bone in options["bones"]):
            continue
        if options.get("array_indices") is not None and curve.array_index not in options["array_indices"]:
            continue
        result_rows.append(row)
    return result_rows


def m2_curve_point(point):
    return {"co": [float(v) for v in point.co],
            "handle_left": [float(v) for v in point.handle_left],
            "handle_right": [float(v) for v in point.handle_right],
            "handle_left_type": point.handle_left_type,
            "handle_right_type": point.handle_right_type,
            "interpolation": point.interpolation}


def m2_curve_patch(row, index, after, operation="set"):
    point = row[3].keyframe_points[index]
    return {"curve": m2_curve_identity(row), "key_index": index,
            "frame": float(point.co[0]), "operation": operation,
            "before": m2_curve_point(point), "after": after}


def m2_curve_frames(curves, options):
    keyed = sorted({float(p.co[0]) for c in curves for p in c.keyframe_points})
    if len(keyed) > 25000:
        raise ValueError("Verification frame budget exceeded")
    steps = options["samples_per_interval"]
    count = len(keyed) + max(0, len(keyed) - 1) * (steps - 1)
    if count * max(1, len(curves)) > 200000:
        raise ValueError("Verification evaluation budget exceeded: 200000")
    samples = list(keyed)
    for left, right in zip(keyed, keyed[1:]):
        for fraction in range(1, steps):
            samples.append(left + (right - left) * fraction / steps)
    return sorted(samples)


def m2_curve_angle(a, b):
    if not all(math.isfinite(v) for v in list(a) + list(b)):
        raise ValueError("Non-finite evaluated quaternion")
    an = math.sqrt(sum(v * v for v in a))
    bn = math.sqrt(sum(v * v for v in b))
    if an < 0.00000001 or bn < 0.00000001:
        return None
    dot = abs(sum(x * y for x, y in zip(a, b)) / (an * bn))
    return math.degrees(2 * math.acos(min(1.0, max(-1.0, dot))))


def m2_curve_groups(rows):
    groups = {}
    for row in rows:
        curve = row[3]
        if curve.data_path.endswith("rotation_quaternion"):
            key = (row[0], row[1], row[2], curve.data_path)
            if key not in groups:
                groups[key] = {}
            if curve.array_index in groups[key]:
                raise ValueError("Duplicate quaternion component identity")
            groups[key][curve.array_index] = row
    return groups


def m2_curve_findings(row, options):
    curve = row[3]
    points = list(curve.keyframe_points)
    if max(0, len(points) - 1) * options["samples_per_interval"] > 200000:
        raise ValueError("Analysis evaluation budget exceeded")
    issues = []
    tolerance = m2_curve_tolerance(curve, options)
    slopes = []
    for a, b in zip(points, points[1:]):
        delta = float(b.co[0] - a.co[0])
        slopes.append(float(b.co[1] - a.co[1]) / delta if delta > 0 else None)
    def add(kind, severity, index, metrics):
        if m2_curve_in_range(float(points[index].co[0]), options):
            issues.append({"type": kind, "severity": severity,
                           "curve": m2_curve_identity(row), "frame": float(points[index].co[0]),
                           "metrics": metrics, "confidence": 0.6 if severity != "info" else 1.0})
    for index, point in enumerate(points):
        if index and point.co[0] <= points[index - 1].co[0]:
            add("duplicate_or_unordered_frame", "high", index, {})
        if index and point.interpolation != points[index - 1].interpolation:
            add("interpolation_change", "low", index, {"from": points[index - 1].interpolation, "to": point.interpolation})
        if index == 0 or index == len(points) - 1:
            continue
        left, right = points[index - 1], points[index + 1]
        duration = float(right.co[0] - left.co[0])
        if duration <= 0:
            continue
        prediction = float(left.co[1]) + float(right.co[1] - left.co[1]) * float(point.co[0] - left.co[0]) / duration
        deviation = abs(float(point.co[1]) - prediction)
        if left.interpolation == "LINEAR" and point.interpolation == "LINEAR" and deviation <= tolerance:
            add("redundant_linear_key", "info", index, {"linear_error": deviation, "tolerance": tolerance,
                    "flat_region": abs(float(right.co[1] - left.co[1])) <= tolerance})
        before_slope, after_slope = slopes[index - 1], slopes[index]
        neighborhood = [abs(v) for i, v in enumerate(slopes[max(0, index - 4):index + 4], start=max(0, index - 4)) if v is not None and i not in (index - 1, index)]
        baseline = statistics.median(neighborhood) if neighborhood else None
        if before_slope is not None and after_slope is not None:
            local_floor = options["spike_min_amplitude"] / max(duration * 0.5, 0.000001)
            if baseline is not None and before_slope * after_slope < 0 and min(abs(before_slope), abs(after_slope)) > options["spike_factor"] * max(baseline, local_floor) and deviation >= options["spike_min_amplitude"]:
                add("isolated_spike_candidate", "medium", index, {"deviation": deviation, "local_median_slope": baseline, "slope_before": before_slope, "slope_after": after_slope})
            if baseline is not None and max(abs(before_slope), abs(after_slope)) > options["spike_factor"] * max(baseline, local_floor):
                add("extreme_local_derivative", "low", index, {"slope_before": before_slope, "slope_after": after_slope, "local_median_slope": baseline})
            accelerations = [abs(slopes[k] - slopes[k - 1]) for k in range(max(1, index - 4), min(len(slopes), index + 4)) if k not in (index - 1, index, index + 1) and slopes[k] is not None and slopes[k - 1] is not None]
            change = abs(after_slope - before_slope)
            if accelerations and change > options["spike_factor"] * max(statistics.median(accelerations), local_floor):
                add("abrupt_acceleration_candidate", "low", index, {"slope_change": change, "local_median_change": statistics.median(accelerations)})
    evaluation_count = 0
    for index, (left, right) in enumerate(zip(points, points[1:])):
        if not m2_curve_in_range(float(left.co[0]), options) and not m2_curve_in_range(float(right.co[0]), options):
            continue
        span = float(right.co[0] - left.co[0])
        if span <= 0:
            continue
        low, high = sorted((float(left.co[1]), float(right.co[1])))
        samples = [float(curve.evaluate(float(left.co[0]) + span * k / options["samples_per_interval"])) for k in range(1, options["samples_per_interval"])]
        if not all(math.isfinite(value) for value in samples):
            raise ValueError("Non-finite curve evaluation: " + curve.data_path)
        evaluation_count += len(samples)
        overshoot = max([max(low - value, value - high, 0.0) for value in samples] or [0.0])
        if overshoot > max(tolerance, options["spike_min_amplitude"]):
            add("between_key_overshoot", "low", index, {"max_overshoot": overshoot, "interval": [float(left.co[0]), float(right.co[0])]})
        if left.interpolation == "CONSTANT" and high - low > options["spike_min_amplitude"]:
            add("constant_interpolation_jump", "low", index + 1, {"delta": high - low})
    for index in range(2, len(points) - 2):
        window = points[index - 2:index + 3]
        differences = [float(b.co[1] - a.co[1]) for a, b in zip(window, window[1:])]
        amplitude = max(float(p.co[1]) for p in window) - min(float(p.co[1]) for p in window)
        if all(a * b < 0 for a, b in zip(differences, differences[1:])) and tolerance < amplitude <= options["noise_threshold"]:
            add("small_alternating_motion_candidate", "low", index, {"amplitude": amplitude, "interpretation": "May be intentional high-frequency motion; review before smoothing"})
    return issues, {"curve": m2_curve_identity(row), "keys": len(points),
                    "max_absolute_slope_per_frame": max([abs(v) for v in slopes if v is not None] or [0.0]),
                    "finding_count": len(issues), "evaluations": evaluation_count}


def m2_curve_analyze(obj, action, options):
    options = m2_curve_options(options)
    rows = m2_curve_rows(obj, action, options)
    budget = sum(max(0, len(row[3].keyframe_points) - 1) * options["samples_per_interval"] for row in rows)
    quaternion_budget = sum(len({float(p.co[0]) for row in components.values() for p in row[3].keyframe_points}) * 4 for components in m2_curve_groups(rows).values())
    if budget + quaternion_budget > 200000:
        raise ValueError("Analysis evaluation budget exceeded: 200000; filter channels or reduce sampling")
    issues = []
    summaries = []
    for row in rows:
        found, summary = m2_curve_findings(row, options)
        issues.extend(found)
        summaries.append(summary)
    for key, components in m2_curve_groups(rows).items():
        if set(components) != {0, 1, 2, 3}:
            issues.append({"type": "incomplete_quaternion_group", "severity": "medium", "curve": m2_curve_identity(next(iter(components.values()))), "frame": None, "confidence": 1.0, "metrics": {"components": sorted(components)}})
            continue
        curves = [components[i][3] for i in range(4)]
        frames = sorted({float(p.co[0]) for c in curves for p in c.keyframe_points})
        previous = None
        previous_frame = None
        for frame in frames:
            values = [float(c.evaluate(frame)) for c in curves]
            norm = math.sqrt(sum(v * v for v in values))
            if not math.isfinite(norm):
                raise ValueError("Non-finite evaluated quaternion")
            if norm < 0.00000001:
                issues.append({"type": "invalid_quaternion", "severity": "high", "curve": m2_curve_identity(components[0]), "frame": frame, "confidence": 1.0, "metrics": {"norm": norm}})
            elif previous is not None and m2_curve_in_range(frame, options):
                dot = sum(a * b for a, b in zip(previous, values))
                angle = m2_curve_angle(previous, values)
                if dot < 0:
                    issues.append({"type": "quaternion_hemisphere_change", "severity": "medium", "curve": m2_curve_identity(components[0]), "frame": frame, "confidence": 1.0, "metrics": {"frame_pair": [previous_frame, frame], "physical_angle_degrees": angle}})
                if angle is not None and angle > 90:
                    issues.append({"type": "large_keyed_rotation_change", "severity": "low", "curve": m2_curve_identity(components[0]), "frame": frame, "confidence": 0.5, "metrics": {"frame_pair": [previous_frame, frame], "angle_degrees": angle}})
            previous, previous_frame = values if norm >= 0.00000001 else None, frame
    counts = {}
    for issue in issues:
        counts[issue["type"]] = counts.get(issue["type"], 0) + 1
    selected = [issue for issue in issues if options.get("severity") is None or issue["severity"] in options["severity"]]
    rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    def issue_sort_key(issue):
        return (rank[issue["severity"]], issue["curve"]["data_path"], issue["frame"] if issue["frame"] is not None else -10000000)
    selected.sort(key=issue_sort_key)
    start, end = options["offset"], options["offset"] + options["limit"]
    return {"object": obj.name, "action": action.name, "findings": selected[start:end],
            "channels": summaries[start:end] if options["detail"] == "full" else [],
            "metrics": {"channels_analyzed": len(rows), "keys_analyzed": sum(s["keys"] for s in summaries), "finding_count": len(issues), "counts_by_type": counts, "evaluations": sum(s["evaluations"] for s in summaries) + quaternion_budget},
            "pagination": {"offset": start, "limit": options["limit"], "total": len(selected), "next_offset": end if end < len(selected) else None},
            "thresholds": {name: options[name] for name in ("tolerance", "spike_factor", "spike_min_amplitude", "noise_threshold", "samples_per_interval")},
            "warnings": ["Curve-local indicators are review candidates, not artistic quality judgments; constant/fast intentional movement is not automatically a spike", "Metrics use channel units and frames; NLA, constraints and drivers are not flattened", "Dense samples are bounded numerical checks, not an analytic proof between every sample"]}


def m2_curve_proposal(obj, action, kind, options):
    explicit_tolerance = isinstance(options, dict) and ("tolerance" in options or bool(options.get("per_channel_tolerances")))
    options = m2_curve_options(options)
    rows = m2_curve_rows(obj, action, options)
    patches = []
    warnings = []
    errors = []
    metrics = {"keys_before": sum(len(row[3].keyframe_points) for row in rows)}
    if any(len(row[3].modifiers) or len(row[3].sampled_points) or row[3].mute for row in rows):
        errors.append("Corrections require unmuted keyed curves without modifiers or sampled points")
    if errors:
        return {"supported": False, "kind": kind, "patches": [], "metrics": metrics, "warnings": warnings, "errors": errors}
    if kind == "quaternion":
        groups = m2_curve_groups(rows)
        for key, components in groups.items():
            if key[3] == "rotation_quaternion":
                owner = obj
            elif key[3].startswith("pose.bones[") and key[3].endswith("].rotation_quaternion"):
                try:
                    owner = obj.path_resolve(key[3][:-len(".rotation_quaternion")])
                except (ValueError, AttributeError):
                    errors.append("Quaternion owner could not be resolved: " + key[3])
                    continue
            else:
                errors.append("Unsupported quaternion owner: " + key[3])
                continue
            if getattr(owner, "rotation_mode", None) != "QUATERNION":
                errors.append("Quaternion channels are not the owner's active rotation mode: " + key[3])
                continue
            if set(components) != {0, 1, 2, 3}:
                errors.append("Quaternion correction requires complete WXYZ channels: " + key[3])
                continue
            curves = [components[i][3] for i in range(4)]
            frames = [[float(p.co[0]) for p in c.keyframe_points] for c in curves]
            if any(values != frames[0] for values in frames[1:]) or any(b <= a for a, b in zip(frames[0], frames[0][1:])):
                errors.append("Quaternion correction requires aligned, unique key times: " + key[3])
                continue
            previous = None
            for index, frame in enumerate(frames[0]):
                values = [float(c.keyframe_points[index].co[1]) for c in curves]
                if math.sqrt(sum(v * v for v in values)) < 0.00000001:
                    errors.append("Invalid zero quaternion at frame " + str(frame))
                    break
                flip = previous is not None and sum(a * b for a, b in zip(previous, values)) < 0
                if flip and not m2_curve_in_range(frame, options):
                    previous = values
                    continue
                if flip:
                    for component in range(4):
                        after = m2_curve_point(curves[component].keyframe_points[index])
                        after["co"][1] *= -1
                        after["handle_left"][1] *= -1
                        after["handle_right"][1] *= -1
                        patches.append(m2_curve_patch(components[component], index, after))
                    values = [-value for value in values]
                previous = values
        warnings.append("Hemisphere continuity repairs can change between-key motion; dense angular verification rejects unsupported path changes")
    elif kind == "cleanup":
        strategies = options.get("strategies", ["redundant"])
        if not isinstance(strategies, list) or not strategies or any(s not in ("redundant", "small_noise", "spike", "interpolation", "mocap_smoothing") for s in strategies):
            raise ValueError("Unsupported cleanup strategy")
        if len(set(strategies)) != len(strategies):
            raise ValueError("Cleanup strategies must be unique")
        if len(strategies) > 1:
            errors.append("Apply one cleanup strategy per reviewed operation, then reanalyze")
        strategy = strategies[0]
        if strategy == "spike" and sum(max(0, len(row[3].keyframe_points) - 1) * options["samples_per_interval"] for row in rows) > 200000:
            raise ValueError("Spike proposal evaluation budget exceeded")
        if strategy != "redundant" and not explicit_tolerance:
            errors.append("Non-default cleanup requires an explicit tolerance or per_channel_tolerances")
        if strategy in ("small_noise", "mocap_smoothing") and options.get("allow_smoothing") is not True:
            errors.append("Smoothing requires allow_smoothing=true and an explicit tolerance")
        if strategy == "spike" and options.get("allow_spike_correction") is not True:
            errors.append("Spike correction requires allow_spike_correction=true and an explicit tolerance")
        if strategy == "interpolation" and (options.get("allow_interpolation_change") is not True or options.get("target_interpolation") != "LINEAR"):
            errors.append("Interpolation normalization supports explicit allow_interpolation_change=true and target_interpolation=LINEAR")
        for row in rows:
            curve = row[3]
            if curve.data_path.endswith("rotation_quaternion"):
                warnings.append("Quaternion channels excluded from scalar cleanup: " + curve.data_path)
                continue
            points = list(curve.keyframe_points)
            tolerance = m2_curve_tolerance(curve, options)
            spike_frames = set()
            if strategy == "spike":
                findings, summary = m2_curve_findings(row, options)
                spike_frames = {f["frame"] for f in findings if f["type"] == "isolated_spike_candidate"}
            for index, point in enumerate(points):
                frame = float(point.co[0])
                if not m2_curve_in_range(frame, options):
                    continue
                if strategy == "interpolation":
                    if index >= len(points) - 1 or not m2_curve_in_range(float(points[index + 1].co[0]), options) or point.interpolation == "LINEAR":
                        continue
                    after = m2_curve_point(point)
                    after["interpolation"] = "LINEAR"
                    patches.append(m2_curve_patch(row, index, after))
                    continue
                if index == 0 or index == len(points) - 1:
                    continue
                left, right = points[index - 1], points[index + 1]
                if left.interpolation != "LINEAR" or point.interpolation != "LINEAR":
                    continue
                span = float(right.co[0] - left.co[0])
                if span <= 0:
                    errors.append("Non-increasing key times: " + curve.data_path)
                    continue
                prediction = float(left.co[1]) + float(right.co[1] - left.co[1]) * float(point.co[0] - left.co[0]) / span
                delta = prediction - float(point.co[1])
                if strategy == "redundant" and abs(delta) <= tolerance:
                    patches.append(m2_curve_patch(row, index, None, "remove"))
                elif strategy in ("small_noise", "mocap_smoothing", "spike"):
                    if strategy == "spike" and frame not in spike_frames:
                        continue
                    if strategy in ("small_noise", "mocap_smoothing"):
                        delta *= 0.5
                    if abs(delta) > tolerance or abs(delta) <= 0.000000000001:
                        continue
                    after = m2_curve_point(point)
                    after["co"][1] += delta
                    after["handle_left"][1] += delta
                    after["handle_right"][1] += delta
                    patches.append(m2_curve_patch(row, index, after))
        warnings.append("Scalar cleanup error is measured in each channel's native units; no quaternion component smoothing")
    else:
        raise ValueError("Unknown curve correction kind")
    if len(patches) > 20000:
        raise ValueError("Patch budget exceeded: 20000 key edits; narrow filters/range")
    metrics["proposed_key_edits"] = len(patches)
    metrics["affected_channels"] = len({m2_curve_key(p["curve"]) for p in patches})
    identities = {}
    for patch in patches:
        identities[m2_curve_key(patch["curve"])] = patch["curve"]
    metrics["affected_channel_ids"] = list(identities.values())[:100]
    metrics["affected_channel_ids_truncated"] = len(identities) > 100
    metrics["affected_frames"] = sorted({p["frame"] for p in patches})[:200]
    metrics["affected_frame_count"] = len({p["frame"] for p in patches})
    capacity = m2_curve_capacity(rows, patches, kind, options)
    metrics["verification_evaluations"] = capacity["evaluations"]
    if not capacity["supported"]:
        errors.append("CAPACITY_EXCEEDED: verification needs " + str(capacity["evaluations"]) + " curve evaluations (limit "
                      + str(capacity["limit"]) + ") or more than 25000 keyed frames per channel group; narrow filters/range")
    return {"supported": not errors, "kind": kind, "patches": patches if not errors else [],
            "metrics": metrics, "warnings": sorted(set(warnings)), "errors": errors, "capacity": capacity}


def m2_curve_capacity(rows, patches, kind, options):
    # Mirrors the budgets m2_curve_verify enforces (m2_curve_frames and the running total), so an
    # oversized correction is refused at propose time, before any checkpoint or candidate (AUD-008).
    steps = options["samples_per_interval"]
    changed = {m2_curve_key(p["curve"]) for p in patches}
    groups = []
    if kind == "quaternion":
        for components in m2_curve_groups(rows).values():
            if any(m2_curve_key(m2_curve_identity(row)) in changed for row in components.values()):
                groups.append(([row[3] for row in components.values()], 8))
    else:
        for row in rows:
            if m2_curve_key(m2_curve_identity(row)) in changed:
                groups.append(([row[3]], 2))
    evaluations = 0
    largest = 0
    for curves, factor in groups:
        keyed = len({float(p.co[0]) for c in curves for p in c.keyframe_points})
        largest = max(largest, keyed)
        evaluations += (keyed + max(0, keyed - 1) * (steps - 1)) * factor
    return {"evaluations": evaluations, "limit": 200000, "max_keyed_frames": largest, "keyed_frame_limit": 25000,
            "supported": evaluations <= 200000 and largest <= 25000}


def m2_curve_apply(obj, action, kind, proposal, options):
    options = m2_curve_options(options)
    if proposal.get("supported") is not True or proposal.get("kind") != kind:
        raise ValueError("Unsupported/mismatched curve proposal")
    rows = m2_curve_rows(obj, action, options)
    curves = {m2_curve_key(m2_curve_identity(row)): row[3] for row in rows}
    grouped = {}
    # Preflight every expected value before changing the candidate.
    for patch in proposal["patches"]:
        key = m2_curve_key(patch["curve"])
        if key not in curves or not 0 <= patch["key_index"] < len(curves[key].keyframe_points):
            raise ValueError("Stale proposal: missing channel/key")
        point = curves[key].keyframe_points[patch["key_index"]]
        if m2_curve_point(point) != patch["before"]:
            raise ValueError("Stale proposal: key or handles changed")
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(patch)
    modified = 0
    removed = 0
    def patch_sort_key(patch):
        return patch["key_index"]
    for key, patches in grouped.items():
        curve = curves[key]
        for patch in sorted(patches, key=patch_sort_key, reverse=True):
            point = curve.keyframe_points[patch["key_index"]]
            if patch["operation"] == "remove":
                curve.keyframe_points.remove(point, fast=True)
                removed += 1
            else:
                after = patch["after"]
                point.co = after["co"]
                point.handle_left_type = after["handle_left_type"]
                point.handle_right_type = after["handle_right_type"]
                point.handle_left = after["handle_left"]
                point.handle_right = after["handle_right"]
                point.interpolation = after["interpolation"]
                modified += 1
        curve.update()
    return {"keys_modified": modified, "keys_removed": removed,
            "affected_channels": len(grouped), "warnings": proposal.get("warnings", [])}


def m2_curve_verify(obj, before_action, after_action, kind, proposal, options):
    options = m2_curve_options(options)
    before_rows = m2_curve_rows(obj, before_action, options, filtered=False)
    after_rows = m2_curve_rows(obj, after_action, options, filtered=False)
    before_map = {m2_curve_key(m2_curve_identity(row)): row[3] for row in before_rows}
    after_map = {m2_curve_key(m2_curve_identity(row)): row[3] for row in after_rows}
    errors = []
    warnings = []
    metrics = {"keys_before": sum(len(r[3].keyframe_points) for r in before_rows),
               "keys_after": sum(len(r[3].keyframe_points) for r in after_rows),
               "max_error": 0.0, "average_error": 0.0, "evaluations": 0,
               "affected_channels": len({m2_curve_key(p["curve"]) for p in proposal["patches"]})}
    if set(before_map) != set(after_map):
        return {"passed": False, "status": "fail", "metrics": metrics, "warnings": [], "errors": ["Channel identity changed"]}
    changed = {m2_curve_key(p["curve"]) for p in proposal["patches"]}
    for key in set(before_map) - changed:
        if [m2_curve_point(p) for p in before_map[key].keyframe_points] != [m2_curve_point(p) for p in after_map[key].keyframe_points]:
            errors.append("Untargeted channel changed: " + key[3])
    differences = []
    if kind == "cleanup":
        for key in changed:
            before, after = before_map[key], after_map[key]
            frames = m2_curve_frames([before, after], options)
            if metrics["evaluations"] + len(frames) * 2 > 200000:
                raise ValueError("Total verification evaluation budget exceeded")
            values = [abs(float(before.evaluate(frame)) - float(after.evaluate(frame))) for frame in frames]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Non-finite candidate curve evaluation")
            differences.extend(values)
            metrics["evaluations"] += len(frames) * 2
            if max(values or [0.0]) > m2_curve_tolerance(before, options):
                errors.append("Motion error exceeds channel tolerance: " + m2_curve_channel_key(before))
    elif kind == "quaternion":
        before_groups, after_groups = m2_curve_groups(before_rows), m2_curve_groups(after_rows)
        metrics["undefined_source_samples"] = 0
        metrics["hemisphere_changes_before"] = 0
        metrics["hemisphere_changes_after"] = 0
        metrics["max_keyed_angular_jump_before_degrees"] = 0.0
        metrics["max_keyed_angular_jump_after_degrees"] = 0.0
        metrics["quaternion_intervals"] = []
        for group_key, components in before_groups.items():
            if not any(m2_curve_key(m2_curve_identity(row)) in changed for row in components.values()):
                continue
            if set(components) != {0, 1, 2, 3} or set(after_groups.get(group_key, {})) != {0, 1, 2, 3}:
                errors.append("Incomplete quaternion group during verification")
                continue
            before = [components[i][3] for i in range(4)]
            after = [after_groups[group_key][i][3] for i in range(4)]
            frames = m2_curve_frames(before + after, options)
            if metrics["evaluations"] + len(frames) * 8 > 200000:
                raise ValueError("Total angular verification evaluation budget exceeded")
            keyed = sorted({float(p.co[0]) for c in before for p in c.keyframe_points})
            for left, right in zip(keyed, keyed[1:]):
                a_left = [float(c.evaluate(left)) for c in before]
                a_right = [float(c.evaluate(right)) for c in before]
                if sum(a * b for a, b in zip(a_left, a_right)) < 0 and len(metrics["quaternion_intervals"]) < 100:
                    b_left = [float(c.evaluate(left)) for c in after]
                    b_right = [float(c.evaluate(right)) for c in after]
                    metrics["quaternion_intervals"].append({"data_path": group_key[3], "frame_pair": [left, right],
                        "angular_jump_before_degrees": m2_curve_angle(a_left, a_right),
                        "angular_jump_after_degrees": m2_curve_angle(b_left, b_right)})
            for curve_set, suffix in ((before, "before"), (after, "after")):
                previous = None
                for frame in keyed:
                    values = [float(c.evaluate(frame)) for c in curve_set]
                    if previous is not None:
                        if sum(a * b for a, b in zip(previous, values)) < 0:
                            metrics["hemisphere_changes_" + suffix] += 1
                        jump = m2_curve_angle(previous, values)
                        if jump is not None:
                            metric_name = "max_keyed_angular_jump_" + suffix + "_degrees"
                            metrics[metric_name] = max(metrics[metric_name], jump)
                    previous = values
            for frame in frames:
                a = [float(c.evaluate(frame)) for c in before]
                b = [float(c.evaluate(frame)) for c in after]
                angle = m2_curve_angle(a, b)
                metrics["evaluations"] += 8
                if angle is None:
                    if math.sqrt(sum(v * v for v in b)) < 0.00000001:
                        errors.append("Candidate contains a zero quaternion at " + str(frame))
                        continue
                    metrics["undefined_source_samples"] += 1
                    neighbors = [(left, right) for left, right in zip(keyed, keyed[1:]) if left < frame < right]
                    if not neighbors:
                        errors.append("Source orientation undefined at keyed/outside frame " + str(frame))
                        continue
                    left, right = neighbors[0]
                    qleft = [float(c.evaluate(left)) for c in before]
                    qright = [float(c.evaluate(right)) for c in before]
                    endpoints_angle = m2_curve_angle(qleft, qright)
                    if endpoints_angle is None or endpoints_angle > 0.0001:
                        errors.append("Undefined source orientation cannot be inferred between differing endpoint orientations")
                        continue
                    angle = m2_curve_angle(qleft, b)
                    warnings.append("Undefined original between-key quaternion compared against equivalent endpoint orientation")
                differences.append(angle)
                if angle > options["angular_tolerance_degrees"]:
                    errors.append("Between-key orientation changed beyond angular tolerance at " + str(frame))
            if metrics["hemisphere_changes_after"] > metrics["hemisphere_changes_before"]:
                errors.append("Quaternion hemisphere continuity worsened")
        metrics["max_angular_error_degrees"] = max(differences or [0.0])
        metrics["average_angular_error_degrees"] = sum(differences) / len(differences) if differences else 0.0
    else:
        raise ValueError("Unknown curve correction kind")
    metrics["max_error"] = max(differences or [0.0])
    metrics["average_error"] = sum(differences) / len(differences) if differences else 0.0
    warnings.append("Verification samples keys plus bounded subframes; visual review remains required")
    return {"passed": not errors, "status": "pass" if not errors else "fail", "metrics": metrics,
            "warnings": sorted(set(warnings)), "errors": sorted(set(errors))[:100]}


_FUNCTIONS = (m2_curve_options, m2_curve_identity, m2_curve_key,
              m2_curve_channel_key, m2_curve_tolerance, m2_curve_in_range,
              m2_curve_rows, m2_curve_point, m2_curve_patch, m2_curve_frames,
              m2_curve_angle, m2_curve_groups, m2_curve_findings,
              m2_curve_analyze, m2_curve_proposal, m2_curve_capacity, m2_curve_apply, m2_curve_verify)

CURVE_RUNTIME = "import math\nimport statistics\nimport json\n" + "\n\n".join(inspect.getsource(function) for function in _FUNCTIONS)
