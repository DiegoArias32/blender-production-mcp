"""Pure policy for channels that the narrow generation adapter never authors."""
from __future__ import annotations

import inspect
import math


def validate_unwritten_pose(channels, curves, written_paths, tolerance=1e-7, limit=16):
    """Require explicit neutral RNA values and constant neutral source curves.

    Active transform channels only are supplied by the Blender adapter. Dormant
    Euler/quaternion/axis-angle storage does not affect the fixed rotation mode.
    This rejects unsupported input; it never resets a pose or changes an Action.
    """
    written = set(written_paths)
    indexed = {row['path']: row for row in channels}
    issues = []
    count = [0]

    def issue(path, reason, component=None):
        count[0] += 1
        if len(issues) < limit:
            issues.append({'path': path, 'reason': reason, 'component': component})

    def neutral(value, target):
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and abs(value-target) <= tolerance)

    for path, row in indexed.items():
        if path in written:
            continue
        values, expected = row['values'], row['neutral']
        if len(values) != len(expected):
            issue(path, 'INVALID_CURRENT_COMPONENTS')
            continue
        for index, (value, target) in enumerate(zip(values, expected)):
            if not neutral(value, target):
                issue(path, 'NON_NEUTRAL_CURRENT_POSE', index)

    for curve in curves:
        path = curve['path']
        if path in written or path not in indexed:
            continue
        expected = indexed[path]['neutral']
        index = curve['index']
        if type(index) is not int or index < 0 or index >= len(expected):
            issue(path, 'INVALID_SOURCE_COMPONENT', index)
            continue
        target = expected[index]
        points = curve['points']
        for at, point in enumerate(points):
            if not neutral(point['value'], target):
                issue(path, 'NON_NEUTRAL_SOURCE_KEY', index)
                break
            # Handles can create a non-neutral segment between neutral keys.
            incoming = at > 0 and points[at-1]['interpolation'] == 'BEZIER'
            outgoing = at+1 < len(points) and point['interpolation'] == 'BEZIER'
            extrapolated = curve.get('extrapolation') == 'LINEAR'
            incoming = incoming or (extrapolated and at == 0)
            outgoing = outgoing or (extrapolated and at == len(points)-1)
            if ((incoming and not neutral(point['left'], target))
                    or (outgoing and not neutral(point['right'], target))):
                issue(path, 'NON_NEUTRAL_SOURCE_TANGENT', index)
                break
    return {'passed': count[0] == 0, 'issue_count': count[0], 'issues': issues,
            'truncated': count[0] > len(issues), 'neutral_tolerance': tolerance,
            'scope': 'Active pose transform channels not authored by the narrow generation adapter'}


POSE_BASELINE_RUNTIME = 'import math\n' + inspect.getsource(validate_unwritten_pose)
