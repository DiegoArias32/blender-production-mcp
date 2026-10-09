"""Exact, shared host/Blender bounds for a generated animation's brief.

The complete semantic plan remains in the brief window even for a single-pose
stage. This prevents unseen animation tails from escaping temporal preview QA.
"""
from __future__ import annotations

import inspect
import math


def validate_pose_window(plan, brief, stage, options=None):
    """Reject mismatched/nonfinite authored windows; never clip or mutate data.

    Self-contained apart from math/builtins so the same function can run in the
    guarded Blender payload. Point intervals are legal; pose-plan endpoints must
    exactly equal the brief endpoints. POSE overrides use their own authored
    frame, while pose_index follows the runtime's zero-based selection contract.
    """
    def frame(value, label):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('GENERATION_WINDOW_INVALID: '+label+' must be a finite numeric frame')
        return float(value)

    if not isinstance(plan, dict) or not isinstance(brief, dict):
        raise ValueError('GENERATION_WINDOW_INVALID: structured plan and brief required')
    if not isinstance(stage, str) or not stage.strip():
        raise ValueError('GENERATION_WINDOW_INVALID: stage must be a nonempty string')
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError('GENERATION_WINDOW_INVALID: options must be a dictionary')
    start = frame(brief.get('frame_start'), 'brief.frame_start')
    end = frame(brief.get('frame_end'), 'brief.frame_end')
    if end <= start:
        raise ValueError('GENERATION_WINDOW_INVALID: brief window must have increasing endpoints')

    def inside(value, label):
        value = frame(value, label)
        if value < start or value > end:
            raise ValueError('GENERATION_WINDOW_INVALID: '+label+' lies outside the complete brief window')
        return value

    def rows(field, required=False):
        values = plan.get(field, [])
        if not isinstance(values, list) or (required and len(values) < 2):
            raise ValueError('GENERATION_WINDOW_INVALID: '+field+' must be a list'+(' with at least two poses' if required else ''))
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise ValueError('GENERATION_WINDOW_INVALID: '+field+'['+str(index)+'] must be a dictionary')
        return values

    poses = rows('poses', True)
    breakdowns = rows('breakdowns')
    for field, values in (('poses', poses), ('breakdowns', breakdowns)):
        for index, pose in enumerate(values):
            inside(pose.get('frame'), field+'['+str(index)+'].frame')
    if poses[0]['frame'] != start or poses[-1]['frame'] != end:
        raise ValueError('GENERATION_WINDOW_INVALID: full pose plan endpoints must exactly match the brief window')

    interval_counts = {}
    for field in ('holds', 'phases', 'contact_intervals'):
        values = rows(field)
        interval_counts[field] = len(values)
        for index, interval in enumerate(values):
            label = field+'['+str(index)+']'
            first = inside(interval.get('frame_start'), label+'.frame_start')
            last = inside(interval.get('frame_end'), label+'.frame_end')
            if last < first:
                raise ValueError('GENERATION_WINDOW_INVALID: '+label+' interval endpoints are reversed')

    selected_frame = None
    if stage.strip().upper() == 'POSE':
        override = options.get('pose')
        if override is not None:
            if not isinstance(override, dict):
                raise ValueError('GENERATION_WINDOW_INVALID: POSE override must be a dictionary')
            selected_frame = inside(override.get('frame'), 'options.pose.frame')
        else:
            index = options.get('pose_index', 0)
            if type(index) is not int or index < 0 or index >= len(poses):
                raise ValueError('POSE_INDEX_INVALID: select an existing pose inside the brief window')
            selected_frame = inside(poses[index].get('frame'), 'selected POSE frame')

    return {'status': 'validated', 'frame_start': start, 'frame_end': end,
            'pose_count': len(poses), 'breakdown_count': len(breakdowns),
            'selected_pose_frame': selected_frame, 'interval_counts': interval_counts}


WINDOW_RUNTIME = 'import math\n' + inspect.getsource(validate_pose_window)
