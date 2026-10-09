"""Read-only, deterministic animation measurements (meters, seconds, degrees).

These functions also run inside Blender after safe-mode validation. No numpy or
third-party package is required. Thresholds are observations, not auto-fix rules.
"""

import math


def contact_report(frames, positions_m, fps, ground_height_m, contact_height_m,
                   contact_speed_m_s, sliding_tolerance_m, min_contact_frames,
                   detail_limit=100):
    """Measure marker slip on a horizontal ground plane.

    Contact requires marker height within ``contact_height_m`` of the supplied
    plane and vertical speed <= ``contact_speed_m_s``. Horizontal speed does not
    gate contact: doing so would hide precisely the sliding we want to measure.
    A marker is a bone head, not a solved sole; users must calibrate the plane
    to that marker. Slope, stairs, moving platforms and heel/toe roll require
    explicit contact annotations or a future surface-aware solver.

    Error is maximum horizontal excursion from the first contact sample, not
    net displacement (which can hide back-and-forth motion). Path length and
    net displacement are also reported. Duration uses original frame numbers.
    A 1e-7 meter comparison epsilon prevents float32 transform noise deciding
    pass/fail exactly on the configured threshold (not a contact tolerance).
    """
    if len(frames) != len(positions_m) or fps <= 0:
        raise ValueError("Matching sample counts and positive FPS are required")
    if any(not math.isfinite(float(value)) for value in
           [fps, ground_height_m, contact_height_m, contact_speed_m_s,
            sliding_tolerance_m]):
        raise ValueError("Contact thresholds must be finite")
    if any(value < 0 for value in
           [contact_height_m, contact_speed_m_s, sliding_tolerance_m]):
        raise ValueError("Contact thresholds cannot be negative")
    if min_contact_frames < 2:
        raise ValueError("min_contact_frames must be at least 2")
    if any(len(point) != 3 or any(not math.isfinite(float(v)) for v in point)
           for point in positions_m):
        raise ValueError("Samples must contain finite XYZ positions")
    if any(frames[index] <= frames[index - 1]
           for index in range(1, len(frames))):
        raise ValueError("Sample frames must be strictly increasing")
    contacts = []
    pending = []
    contact_count = 0
    failed_count = 0
    peak_sliding_m = 0.0
    total_path_m = 0.0
    below_ground_samples = 0
    for index in range(len(frames) + 1):
        contact = False
        if index < len(frames):
            point = positions_m[index]
            if point[2] < ground_height_m - contact_height_m:
                below_ground_samples += 1
            # A central vertical velocity avoids assigning an entire airborne
            # interval to contact, while one-sided endpoints remain measurable.
            lo = max(0, index - 1)
            hi = min(len(frames) - 1, index + 1)
            velocity = 0.0
            if hi > lo:
                elapsed = (frames[hi] - frames[lo]) / fps
                velocity = abs(positions_m[hi][2] - positions_m[lo][2]) / elapsed
            contact = (abs(point[2] - ground_height_m) <= contact_height_m
                       and velocity <= contact_speed_m_s)
        if contact:
            pending.append(index)
        elif pending:
            first = pending[0]
            last = pending[-1]
            if len(pending) >= 2 and frames[last] - frames[first] + 1 >= min_contact_frames:
                anchor = positions_m[first]
                displacements = [math.hypot(positions_m[item][0] - anchor[0],
                                             positions_m[item][1] - anchor[1])
                                 for item in pending]
                path_length = sum(math.hypot(positions_m[item][0] - positions_m[item - 1][0],
                                             positions_m[item][1] - positions_m[item - 1][1])
                                  for item in pending[1:])
                maximum = max(displacements)
                failed = maximum > sliding_tolerance_m + 1e-7
                problematic = [frames[item] for item, distance in zip(pending, displacements)
                               if distance > sliding_tolerance_m + 1e-7]
                contact_count += 1
                failed_count += int(failed)
                peak_sliding_m = max(peak_sliding_m, maximum)
                total_path_m += path_length
                if len(contacts) < detail_limit:
                    contacts.append({
                        "frame_start": frames[first], "frame_end": frames[last],
                        "sample_count": len(pending),
                        "duration_seconds": (frames[last] - frames[first]) / fps,
                        "translation_during_contact_m": maximum,
                        "translation_during_contact_cm": maximum * 100.0,
                        "net_translation_m": displacements[-1],
                        "horizontal_path_length_m": path_length,
                        "status": "fail" if failed else "pass",
                        "problematic_frames": problematic[:detail_limit],
                        "problematic_frame_count": len(problematic),
                        "problematic_frames_truncated": len(problematic) > detail_limit,
                    })
            pending = []
    return {"status": "fail" if failed_count else ("pass" if contact_count else "inconclusive"),
            "contact_count": contact_count, "failed_contact_count": failed_count,
            "contacts": contacts, "contacts_truncated": contact_count > detail_limit,
            # No contact interval means no evidence: the peak is unknown, never 0.0 (AUD-005).
            "max_sliding_m": peak_sliding_m if contact_count else None,
            "total_horizontal_contact_path_m": total_path_m,
            "below_ground_sample_count": below_ground_samples,
            "comparison_epsilon_m": 1e-7}


def quaternion_report(frames, quaternions, detail_limit=100):
    """Distinguish antipodal representation changes from orientation changes.

    Quaternions are WXYZ, normalized before comparison. Negative dot products
    flag sign/hemisphere discontinuity candidates; q and -q have zero physical
    rotation difference. A >90 degree orientation change between adjacent key
    times is a documented review heuristic, not an error verdict. Sparse keys,
    intentional long rotations and interpolation can require visual inspection.
    """
    if len(frames) != len(quaternions):
        raise ValueError("Matching quaternion sample counts are required")
    if any(frames[index] <= frames[index - 1] for index in range(1, len(frames))):
        raise ValueError("Sample frames must be strictly increasing")
    flips = []
    jumps = []
    invalid = []
    flip_count = 0
    jump_count = 0
    invalid_count = 0
    previous = None
    previous_frame = None
    max_angle = 0.0
    for frame, values in zip(frames, quaternions):
        if len(values) != 4 or any(not math.isfinite(float(value)) for value in values):
            magnitude = 0.0
        else:
            magnitude = math.sqrt(sum(value * value for value in values))
        if magnitude < 1e-12 or not math.isfinite(magnitude):
            invalid_count += 1
            if len(invalid) < detail_limit:
                invalid.append(frame)
            previous = None
            previous_frame = None
            continue
        normalized = [value / magnitude for value in values]
        if previous is not None:
            dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(previous, normalized))))
            angle = math.degrees(2.0 * math.acos(abs(dot)))
            max_angle = max(max_angle, angle)
            detail = {"frame_start": previous_frame, "frame_end": frame,
                      "normalized_dot": dot, "orientation_delta_degrees": angle}
            if dot < 0:
                flip_count += 1
                if len(flips) < detail_limit:
                    flips.append(dict(detail, kind="hemisphere_discontinuity_candidate",
                                      equivalent_orientation=angle < 0.01))
            if angle > 90.0:
                jump_count += 1
                if len(jumps) < detail_limit:
                    jumps.append(dict(detail, kind="large_orientation_change_review"))
        previous = normalized
        previous_frame = frame
    return {"sign_discontinuity_count": flip_count, "sign_discontinuities": flips,
            "orientation_jump_count": jump_count, "orientation_jumps": jumps,
            "invalid_quaternion_count": invalid_count, "invalid_frames": invalid,
            "max_orientation_delta_degrees": max_angle,
            "details_truncated": max(flip_count, jump_count, invalid_count) > detail_limit,
            "orientation_jump_review_threshold_degrees": 90.0}
