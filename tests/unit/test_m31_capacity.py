"""AUD-008: verification budgets are checked at propose time, before checkpoint or candidate.

The capacity functions mirror limits enforced later by the verifiers; these tests pin the arithmetic
at the audit's reproduction sizes. Real-Blender coverage is in tests/blender/m31_workload_probe.py.
"""
from types import SimpleNamespace

from blender_mcp.production.animation.curve_runtime import m2_curve_capacity

OPTIONS = {'samples_per_interval': 8}  # m2_curve_options default


def transform_capacity():
    from blender_mcp.production.animation.transform_ops import TRANSFORM_RUNTIME
    namespace = {}
    source = TRANSFORM_RUNTIME[TRANSFORM_RUNTIME.index('def m2_transform_capacity'):TRANSFORM_RUNTIME.index('def m2_transform_proposal')]
    exec(source, namespace)
    return namespace['m2_transform_capacity']


def rig(bones):
    return SimpleNamespace(type='ARMATURE', pose=SimpleNamespace(bones=[object()] * bones))


def test_m2_transform_capacity_matches_verifier_limits():
    capacity = transform_capacity()
    frames = list(range(1, 1002))
    # 1001 frames -> 2001 half-frame samples; the verifier refuses >100000 bone/frames per pass.
    assert capacity(rig(49), frames)['bone_frame_samples_per_pass'] == 2001 * 49
    assert capacity(rig(49), frames)['supported'] is True
    assert capacity(rig(50), frames)['bone_frame_samples_per_pass'] == 100050
    assert capacity(rig(50), frames)['supported'] is False
    assert capacity(rig(129), list(range(1, 11)))['supported'] is False  # >128 bones


class Curve:
    def __init__(self, path, index, frames):
        self.data_path, self.array_index = path, index
        self.keyframe_points = [SimpleNamespace(co=(float(f), 0.0)) for f in frames]


def patch(curve):
    return {'curve': {'slot_handle': None, 'layer_index': 0, 'strip_index': 0,
                      'data_path': curve.data_path, 'array_index': curve.array_index}}


def test_m2_curve_capacity_counts_verifier_evaluations():
    options = OPTIONS
    curve = Curve('location', 0, range(1, 1002))
    rows = [(None, 0, 0, curve)]
    capacity = m2_curve_capacity(rows, [patch(curve)], 'cleanup', options)
    # m2_curve_frames: 1001 keys + 1000 intervals * 7 interior samples, evaluated before and after.
    assert capacity['evaluations'] == (1001 + 1000 * 7) * 2 and capacity['supported']
    many = [Curve('location', i, range(1, 1002)) for i in range(3)] + [Curve('scale', i, range(1, 1002)) for i in range(10)]
    rows = [(None, 0, 0, c) for c in many]
    assert not m2_curve_capacity(rows, [patch(c) for c in many], 'cleanup', options)['supported']


def test_m2_curve_capacity_for_quaternion_groups():
    options = OPTIONS
    group = [Curve('pose.bones["B"].rotation_quaternion', i, range(1, 1002)) for i in range(4)]
    rows = [(None, 0, 0, c) for c in group]
    capacity = m2_curve_capacity(rows, [patch(group[0])], 'quaternion', options)
    # One group: 8001 frames x 8 curves (before + after) = 64008 <= 200000.
    assert capacity['evaluations'] == (1001 + 1000 * 7) * 8 and capacity['supported']
    groups = [Curve('pose.bones["B%d"].rotation_quaternion' % g, i, range(1, 1002)) for g in range(4) for i in range(4)]
    rows = [(None, 0, 0, c) for c in groups]
    capacity = m2_curve_capacity(rows, [patch(c) for c in groups[::4]], 'quaternion', options)
    assert capacity['evaluations'] == 4 * 64008 and not capacity['supported']


def test_propose_refuses_payload_beyond_safe_mode_limit():
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME, CHECK, EXECUTE, payload_capacity
    from blender_mcp.safe_mode import MAX_CODE_BYTES
    small = {'supported': True, 'samples': [[0.1] * 10]}
    payload_capacity(small, BASE_RUNTIME, EXECUTE, CHECK, {'proposal': small, 'options': {}})
    assert small['supported'] and max(small['capacity']['payload_bytes'].values()) < MAX_CODE_BYTES
    # Proposal data travels inside the script: ~250 kB of samples cannot be applied or verified.
    large = {'supported': True, 'samples': [[0.123456789] * 1000 for _ in range(25)]}
    payload_capacity(large, BASE_RUNTIME, EXECUTE, CHECK, {'proposal': large, 'options': {}})
    assert not large['supported'] and large['reasons'][0].startswith('CAPACITY_EXCEEDED')
    assert min(large['capacity']['payload_bytes'].values()) > MAX_CODE_BYTES


def test_capacity_estimate_uses_measured_rates_and_standard_fields():
    from blender_mcp.production.common.capacity import BONE_FRAMES_PER_SECOND, annotate
    ikfk = annotate({'world_bone_frames_per_pass': 50000, 'passes': 2, 'limit_per_pass': 100000}, True)
    assert set(ikfk) >= {'estimated_samples', 'estimated_memory_bytes', 'estimated_runtime_class', 'configured_limit', 'supported'}
    assert ikfk['estimated_samples'] == 100000 and ikfk['configured_limit'] == 100000
    assert ikfk['estimated_runtime_class'] == ('under_10s' if 100000 / BONE_FRAMES_PER_SECOND < 10 else 'under_60s')
    huge = annotate({'bone_frame_samples_per_pass': 3_000_000, 'limit_per_pass': 100000}, False)
    assert huge['estimated_runtime_class'] == 'timeout_risk' and huge['supported'] is False
    curves = annotate({'evaluations': 1000, 'limit': 200000}, True)
    assert curves['estimated_runtime_class'] == 'not_estimated' and curves['estimated_memory_bytes'] is None
