"""Bounded 2D UV metrics. Areas use signed orientation; overlap uses polygon clipping."""
from __future__ import annotations
import math


def signed_area(points):
    return sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(points, points[1:] + points[:1])) * 0.5


def triangle_overlap(a, b, epsilon=1e-12):
    """Intersection area, ignoring shared boundaries. Exact for the two linear UV triangles."""
    subject = [tuple(p) for p in a]
    clip = list(b) if signed_area(list(b)) >= 0 else list(reversed(b))
    for start, end in zip(clip, clip[1:] + clip[:1]):
        if not subject:
            return 0.0
        output = []
        previous = subject[-1]
        prev_d = (end[0] - start[0]) * (previous[1] - start[1]) - (end[1] - start[1]) * (previous[0] - start[0])
        for current in subject:
            cur_d = (end[0] - start[0]) * (current[1] - start[1]) - (end[1] - start[1]) * (current[0] - start[0])
            if (cur_d >= -epsilon) != (prev_d >= -epsilon):
                ratio = prev_d / (prev_d - cur_d)
                output.append((previous[0] + ratio * (current[0] - previous[0]), previous[1] + ratio * (current[1] - previous[1])))
            if cur_d >= -epsilon:
                output.append(current)
            previous, prev_d = current, cur_d
        subject = output
    return abs(signed_area(subject)) if len(subject) >= 3 else 0.0


def tile_of(points, epsilon=1e-7):
    """Face vertices on a tile boundary belong to the tile containing the centroid."""
    center = [sum(p[k] for p in points) / len(points) for k in (0, 1)]
    u, v = math.floor(center[0]), math.floor(center[1])
    if not 0 <= u < 10 or v < 0 or v > 99:
        return None
    if any(p[0] < u - epsilon or p[0] > u + 1 + epsilon or p[1] < v - epsilon or p[1] > v + 1 + epsilon for p in points):
        return None
    return 1001 + u + 10 * v


def shelf_pack(boxes, margin=0.01, rotate=False):
    """Reproducible shelf packing. A bounded search for a common scale, not an occupancy optimizer."""
    if not boxes:
        return {}, 1.0
    def box_order(k):
        return (-max(boxes[k]), -min(boxes[k]), str(k))
    order = sorted(boxes, key=box_order)

    def fit(scale):
        x, y, row = margin, margin, 0.0
        result = {}
        for key in order:
            w, h = boxes[key]
            turned = bool(rotate and h > w)
            if turned:
                w, h = h, w
            w, h = max(w * scale, margin * 0.1), max(h * scale, margin * 0.1)
            if x + w > 1 - margin + 1e-12:
                x, y, row = margin, y + row + margin, 0.0
            if w > 1 - 2 * margin or y + h > 1 - margin + 1e-12:
                return None
            result[key] = {'x': x, 'y': y, 'rotate': turned}
            x += w + margin
            row = max(row, h)
        return result

    low, high = 0.0, max(1.0, (1 - 2 * margin) / max(max(boxes[k]) for k in boxes))
    for _ in range(24):
        middle = (low + high) * 0.5
        if fit(middle) is None:
            high = middle
        else:
            low = middle
    output = fit(low)
    if output is None or low <= 1e-12:
        raise ValueError('UV_PACK_IMPOSSIBLE: islands do not fit with requested padding')
    return output, low
