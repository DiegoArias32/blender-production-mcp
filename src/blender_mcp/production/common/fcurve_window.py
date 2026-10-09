"""Mutation windows for keyed corrections (M3.1, audit AUD-001).

A correction declares a window [a, b]. Keys it writes live inside that window. Before writing, every
written F-Curve receives exact *fence* keys on the integer frames lo = ceil(a) - 1 and
hi = floor(b) + 1:

- CONSTANT segment: the fence holds the left key's value.
- LINEAR segment: the fence lies on the line.
- BEZIER segment: the segment is split with de Casteljau at the parameter whose x equals the fence
  frame, after the same handle correction Blender applies before evaluating
  (``BKE_fcurve_correct_bezpart``), so the curve outside (lo, hi) is reproduced exactly.
- Beyond the first/last key: the fence holds the constant extrapolated value.

Handles touched by a split, and both handles of the fence keys and their outer neighbours, become
FREE with their exact coordinates, so automatic handle recalculation cannot move the curve outside
the window. Curves using "Continuous Acceleration" smoothing (whose auto handles depend on the whole
run of keys) have every handle frozen. Easing interpolations (SINE…BOUNCE) and non-constant
extrapolation at a fence are refused instead of approximated.

Frames <= lo and >= hi are therefore unchanged by construction and are verified again afterwards;
(lo, a) and (b, hi) are reported as transition intervals.
"""

WINDOW_RUNTIME = r'''
def fw_fences(window_start, window_end):
    return [float(math.ceil(window_start) - 1), float(math.floor(window_end) + 1)]

def fw_freeze(point):
    left = [point.handle_left[0], point.handle_left[1]]
    right = [point.handle_right[0], point.handle_right[1]]
    point.handle_left_type = 'FREE'
    point.handle_right_type = 'FREE'
    point.handle_left = left
    point.handle_right = right

def fw_key_at(curve, frame):
    for point in curve.keyframe_points:
        if abs(point.co[0] - frame) < 1e-6:
            return point
    return None

def fw_neighbors(curve, frame):
    left = None
    right = None
    for point in curve.keyframe_points:
        if point.co[0] < frame - 1e-6:
            left = point
        elif point.co[0] > frame + 1e-6 and right is None:
            right = point
    return left, right

def fw_lerp(a, b, t):
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]

def fw_corrected_handles(p0, h1, h2, p3):
    # Same clamp as BKE_fcurve_correct_bezpart: a handle may not reach past the other key's time.
    span = p3[0] - p0[0]
    h1 = list(h1)
    h2 = list(h2)
    len1 = abs(p0[0] - h1[0])
    len2 = abs(p3[0] - h2[0])
    if len1 + len2 == 0:
        return h1, h2
    if len1 > span:
        factor = span / len1
        h1 = [p0[0] - factor * (p0[0] - h1[0]), p0[1] - factor * (p0[1] - h1[1])]
    if len2 > span:
        factor = span / len2
        h2 = [p3[0] - factor * (p3[0] - h2[0]), p3[1] - factor * (p3[1] - h2[1])]
    return h1, h2

def fw_bezier_split(p0, h1, h2, p3, frame):
    h1, h2 = fw_corrected_handles(p0, h1, h2, p3)
    low = 0.0
    high = 1.0
    for iteration in range(80):
        t = (low + high) / 2
        x = ((1 - t) ** 3) * p0[0] + 3 * ((1 - t) ** 2) * t * h1[0] + 3 * (1 - t) * t * t * h2[0] + t ** 3 * p3[0]
        if x < frame:
            low = t
        else:
            high = t
    t = (low + high) / 2
    a = fw_lerp(p0, h1, t)
    b = fw_lerp(h1, h2, t)
    c = fw_lerp(h2, p3, t)
    d = fw_lerp(a, b, t)
    e = fw_lerp(b, c, t)
    s = fw_lerp(d, e, t)
    return {'left_out': a, 'split_in': d, 'split': s, 'split_out': e, 'right_in': c}

def fw_representable(p0, h1, h2, p3):
    # Blender clamps handles whose time extent exceeds the segment before evaluating; a split half
    # that would need such clamping cannot reproduce the original curve exactly.
    span = p3[0] - p0[0]
    return (span > 0 and p0[0] - 1e-9 <= h1[0] <= p0[0] + span + 1e-9
            and p3[0] - span - 1e-9 <= h2[0] <= p3[0] + 1e-9)

def fw_insert(curve, frame, value):
    point = curve.keyframe_points.insert(frame, value, options={'FAST'})
    point.co = [frame, value]
    return point

def fw_place_fence(curve, frame, report):
    existing = fw_key_at(curve, frame)
    if existing is not None:
        fw_freeze(existing)
        report['existing_fences'] += 1
        return existing
    left, right = fw_neighbors(curve, frame)
    if left is None and right is None:
        raise ValueError('WINDOW_INTERNAL: fence requested on an empty curve')
    if left is None or right is None:
        # Outside the keyed range the curve is the constant extrapolation of the end key.
        end = right if left is None else left
        fence = fw_insert(curve, frame, end.co[1])
        fence.interpolation = 'CONSTANT'
        if left is not None:
            # The former last key now has a successor: keep its segment flat, as extrapolated.
            left.interpolation = 'CONSTANT'
            report['boundary_interpolation_changes'] += 1
        fw_freeze(fence)
        fw_freeze(end)
        report['inserted_fences'] += 1
        return fence
    kind = left.interpolation
    if kind == 'CONSTANT':
        fence = fw_insert(curve, frame, left.co[1])
        fence.interpolation = 'CONSTANT'
    elif kind == 'LINEAR':
        t = (frame - left.co[0]) / (right.co[0] - left.co[0])
        fence = fw_insert(curve, frame, left.co[1] + (right.co[1] - left.co[1]) * t)
        fence.interpolation = 'LINEAR'
    elif kind == 'BEZIER':
        split = fw_bezier_split([left.co[0], left.co[1]], [left.handle_right[0], left.handle_right[1]],
                                [right.handle_left[0], right.handle_left[1]], [right.co[0], right.co[1]], frame)
        if not (fw_representable([left.co[0], left.co[1]], split['left_out'], split['split_in'], split['split'])
                and fw_representable(split['split'], split['split_out'], split['right_in'], [right.co[0], right.co[1]])):
            raise ValueError('UNSUPPORTED_BEZIER_SPLIT: ' + curve.data_path + '[' + str(curve.array_index) + '] has handles too long '
                             'to split exactly at frame ' + str(frame) + '; choose another window or simplify those handles')
        fw_freeze(left)
        fw_freeze(right)
        fence = fw_insert(curve, frame, split['split'][1])
        fence.interpolation = 'BEZIER'
        fence.handle_left_type = 'FREE'
        fence.handle_right_type = 'FREE'
        left.handle_right = split['left_out']
        fence.handle_left = split['split_in']
        fence.handle_right = split['split_out']
        right.handle_left = split['right_in']
    else:
        raise ValueError('UNSUPPORTED_INTERPOLATION: ' + curve.data_path + '[' + str(curve.array_index) + '] uses '
                         + kind + ' across a window fence; convert or choose another window')
    fw_freeze(fence)
    fw_freeze(left)
    fw_freeze(right)
    report['inserted_fences'] += 1
    return fence

def fw_prepare(curve, window_start, window_end):
    """Fence one existing written curve and clear its keys inside (lo, hi)."""
    lo, hi = fw_fences(window_start, window_end)
    report = {'data_path': curve.data_path, 'index': curve.array_index, 'fences': [lo, hi],
              'inserted_fences': 0, 'existing_fences': 0, 'boundary_interpolation_changes': 0,
              'removed_keys': 0, 'all_handles_frozen': False}
    if len(curve.keyframe_points) and curve.extrapolation != 'CONSTANT':
        raise ValueError('UNSUPPORTED_EXTRAPOLATION: ' + curve.data_path + ' uses ' + curve.extrapolation
                         + ' extrapolation; a windowed edit cannot preserve it')
    if len(curve.keyframe_points) and getattr(curve, 'auto_smoothing', 'NONE') != 'NONE':
        for point in curve.keyframe_points:
            fw_freeze(point)
        report['all_handles_frozen'] = True
    fw_place_fence(curve, lo, report)
    fw_place_fence(curve, hi, report)
    for index in range(len(curve.keyframe_points) - 1, -1, -1):
        point = curve.keyframe_points[index]
        if lo + 1e-6 < point.co[0] < hi - 1e-6:
            curve.keyframe_points.remove(point, fast=True)
            report['removed_keys'] += 1
    curve.update()
    return report

def fw_finish(curve, window_start, window_end, interpolation):
    """After window keys exist: the lo fence and every window key use the transition interpolation."""
    lo, hi = fw_fences(window_start, window_end)
    for point in curve.keyframe_points:
        if lo - 1e-6 <= point.co[0] < hi - 1e-6:
            point.interpolation = interpolation
    curve.update()

def fw_curve_value(curve, frame, static):
    return curve.evaluate(frame) if curve is not None else static

def fw_curve_rows(curve):
    return [[list(p.co), list(p.handle_left), list(p.handle_right), p.interpolation, p.handle_left_type,
             p.handle_right_type, p.easing] for p in curve.keyframe_points] + [curve.extrapolation, curve.mute]
'''
