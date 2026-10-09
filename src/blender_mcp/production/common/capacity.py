"""AUD-008 capability estimate: every propose reports its workload before any checkpoint exists.

The estimate is coarse and measured, never a runtime promise. Its constants come from
``tests/blender/m31_workload_probe.py`` (Blender 5.2.1, Ryzen 7 5700G + RTX 4060, four runs in
``test-artifacts/m3.1-workload/``): evaluating one bone's world matrices at one frame ran at
30 769-64 690 bone-frames/s and allocated 342.8-343.1 bytes of Python memory per bone-frame. The
slowest rate and the largest allocation are used. Other machines, rigs with constraints or drivers,
and heavier modifiers will differ; the class is a coarse bucket against the 180 s socket timeout.
"""
from __future__ import annotations

BONE_FRAMES_PER_SECOND = 30769.2
ALLOC_BYTES_PER_BONE_FRAME = 343.1
SOCKET_TIMEOUT_S = 180.0
BASIS = ('measured world-matrix evaluation: 30769.2 bone-frames/s (slowest of 4 runs), 343.1 B/bone-frame; '
         'tests/blender/m31_workload_probe.py on Blender 5.2.1')


def _samples(capacity: dict):
    """(bone-frame samples for the whole verification, configured per-pass limit) per domain."""
    if 'world_bone_frames_per_pass' in capacity:  # IK/FK
        return capacity['world_bone_frames_per_pass'] * capacity.get('passes', 2), capacity['limit_per_pass']
    if 'protected_bone_frames_per_pass' in capacity:  # M3 foot correction
        per_pass = capacity['protected_bone_frames_per_pass'] or 0
        return per_pass * 2 + capacity.get('propose_effector_samples', 0), capacity['protected_limit']
    if 'bone_frame_samples_per_pass' in capacity:  # M2 transforms
        return capacity['bone_frame_samples_per_pass'] * 2, capacity['limit_per_pass']
    if 'per_pass_samples' in capacity:  # mocap retarget
        return capacity['estimated_samples'], capacity['configured_limit']
    return None, capacity.get('limit')


def annotate(capacity: dict, supported: bool) -> dict:
    """Adds estimated_samples, estimated_memory_bytes, estimated_runtime_class, configured_limit, supported."""
    samples, limit = _samples(capacity)
    capacity['configured_limit'] = limit
    capacity['supported'] = bool(supported)
    if samples is None:
        # Curve-evaluation budgets (M2 cleanup/quaternion) have no measured bone-frame rate.
        capacity.update(estimated_samples=capacity.get('evaluations'), estimated_memory_bytes=None,
                        estimated_runtime_class='not_estimated', estimate_basis='curve evaluations; no measured rate')
        return capacity
    seconds = samples / BONE_FRAMES_PER_SECOND
    capacity.update(estimated_samples=samples, estimated_memory_bytes=int(samples * ALLOC_BYTES_PER_BONE_FRAME),
                    estimated_runtime_class='under_10s' if seconds < 10 else 'under_60s' if seconds < 60
                    else 'under_timeout' if seconds < SOCKET_TIMEOUT_S else 'timeout_risk',
                    estimate_basis=BASIS)
    return capacity
