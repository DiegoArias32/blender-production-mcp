"""Bounded topology-preserving deformation metrics, independent of Blender.

Displacement is descriptive: a legitimate bend can move a vertex far from rest.
Edge/area ratios are local indicators, never an anatomical correctness score.
"""
import math


def dq_sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def dq_dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def dq_cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def dq_length(a):
    return math.sqrt(dq_dot(a, a))


def dq_area(points, triangle):
    a, b, c = [points[i] for i in triangle]
    return dq_length(dq_cross(dq_sub(b, a), dq_sub(c, a))) * 0.5


def dq_volume(points, triangles):
    """Signed closed-surface volume about mesh centroid, translation stable."""
    if not points:
        return 0.0
    center = [sum(p[i] for p in points)/len(points) for i in range(3)]
    return abs(sum(dq_dot(dq_sub(points[a], center), dq_cross(dq_sub(points[b], center),
               dq_sub(points[c], center))) for a, b, c in triangles) / 6.0)


def dq_closed(triangles):
    counts = {}
    oriented = {}
    for a, b, c in triangles:
        for left, right in ((a, b), (b, c), (c, a)):
            edge = tuple(sorted((left, right)))
            counts[edge] = counts.get(edge, 0) + 1
            oriented[edge] = oriented.get(edge, 0) + (1 if left < right else -1)
    return bool(counts) and all(counts[e] == 2 and oriented[e] == 0 for e in counts)


def dq_metrics(reference, current, edges, triangles, thresholds, indices=None):
    if len(reference) != len(current):
        raise ValueError("Topology changed: vertex correspondence is unavailable")
    if any(not math.isfinite(x) for point in reference + current for x in point):
        raise ValueError("Non-finite vertex coordinates")
    selected = set(indices) if indices is not None else set(range(len(reference)))
    if not selected or any(i < 0 or i >= len(reference) for i in selected):
        raise ValueError("Region must select existing vertices")
    epsilon = thresholds["epsilon_m"]
    local_edges = [(a, b) for a, b in edges if a in selected and b in selected]
    local_faces = [(a, b, c) for a, b, c in triangles if a in selected and b in selected and c in selected]
    lengths = [(dq_length(dq_sub(reference[a], reference[b])),
                dq_length(dq_sub(current[a], current[b]))) for a, b in local_edges]
    ratios = [after / before for before, after in lengths if before > epsilon]
    areas = [(dq_area(reference, triangle), dq_area(current, triangle)) for triangle in local_faces]
    area_ratios = [after / before for before, after in areas if before > epsilon * epsilon]
    displacements = [dq_length(dq_sub(current[i], reference[i])) for i in selected]
    full_closed = indices is None and dq_closed(local_faces)
    before_volume = dq_volume(reference, local_faces) if full_closed else None
    after_volume = dq_volume(current, local_faces) if full_closed else None
    volume_ratio = after_volume / before_volume if before_volume is not None and before_volume > epsilon**3 else None
    collapsed = sum(r < thresholds["collapse_ratio"] for r in ratios)
    stretched = sum(r > thresholds["stretch_ratio"] for r in ratios)
    degenerate = sum(r < thresholds["area_ratio"] for r in area_ratios)
    metrics = {"vertices": len(selected), "edges": len(local_edges), "triangles": len(local_faces),
               "max_displacement_m": max(displacements), "mean_displacement_m": sum(displacements)/len(displacements),
               "min_edge_ratio": min(ratios) if ratios else None,
               "max_edge_ratio": max(ratios) if ratios else None,
               "min_triangle_area_ratio": min(area_ratios) if area_ratios else None,
               "collapsed_edges": collapsed, "stretched_edges": stretched,
               "collapsed_triangles": degenerate,
               "reference_degenerate_edges": sum(before <= epsilon for before, after in lengths),
               "reference_degenerate_triangles": sum(before <= epsilon*epsilon for before, after in areas),
               "closed_oriented_surface": full_closed,
               "reference_volume_m3": before_volume, "volume_m3": after_volume,
               "volume_ratio": volume_ratio}
    issues = []
    if collapsed:
        issues.append({"issue": "edge_collapse", "severity": "warning", "confidence": 0.9,
                       "count": collapsed, "threshold": thresholds["collapse_ratio"]})
    if stretched:
        issues.append({"issue": "abnormal_edge_stretch", "severity": "warning", "confidence": 0.85,
                       "count": stretched, "threshold": thresholds["stretch_ratio"]})
    if degenerate:
        issues.append({"issue": "triangle_collapse", "severity": "warning", "confidence": 0.85,
                       "count": degenerate, "threshold": thresholds["area_ratio"]})
    if volume_ratio is not None and volume_ratio < thresholds["volume_ratio"]:
        issues.append({"issue": "approximate_volume_loss", "severity": "warning", "confidence": 0.7,
                       "ratio": volume_ratio, "threshold": thresholds["volume_ratio"]})
    return {"metrics": metrics, "issues": issues}
