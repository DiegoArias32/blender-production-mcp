"""Read-only entry point for bounded, context-sensitive F-Curve inspection."""
from .curve_runtime import CURVE_RUNTIME
from .scripts import ACTION_HELPERS


ANALYZE_FCURVES = ACTION_HELPERS + CURVE_RUNTIME + r'''
obj = bpy.data.objects.get(params["object_name"])
if obj is None:
    raise ValueError("Object not found: " + params["object_name"])
if obj.animation_data is None or obj.animation_data.action is None:
    raise ValueError("Object has no active Action")
result = m2_curve_analyze(obj, obj.animation_data.action, params["options"])
'''


def analyze_fcurves(bridge, object_name, options=None):
    """Analyze the selected object's active Action without changing its state.

    Options: bones, channels, data_paths, array_indices, frame_start/end,
    severity, detail=summary|full, limit/offset, tolerance,
    per_channel_tolerances (``data_path[index]``), spike_factor,
    spike_min_amplitude, noise_threshold, samples_per_interval.
    Magnitude defaults are review starting points in native channel units,
    not universal biomechanical limits. The runtime validates every option.
    """
    if not isinstance(object_name, str) or not object_name:
        raise ValueError("object_name must be a nonempty exact object name")
    if options is not None and not isinstance(options, dict):
        raise ValueError("options must be a mapping")
    return bridge.run(ANALYZE_FCURVES, {"object_name": object_name, "options": options or {}})
