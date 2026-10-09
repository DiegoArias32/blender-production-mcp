"""Deterministic bounded secondary motion, in metres and seconds.

This is a constrained mass/damper approximation driven by sampled primary
motion. It is not a cloth, groom, muscle or general rigid-body solver. No noise
or free-running sinusoid is introduced. Attachment and length projection are
hard constraints; every projection is reported separately from QA measurements.
"""
from __future__ import annotations

import math

from .model import normalize_settings, number, vector

_ZERO = (0.0, 0.0, 0.0)
_IDENTITY = (1.0, 0.0, 0.0, 0.0)


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _scale(a, value):
    return tuple(x * value for x in a)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a, fallback=_ZERO):
    length = _norm(a)
    return _scale(a, 1 / length) if length > 1e-12 else tuple(fallback)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _qmul(a, b):
    w, x, y, z = a
    v, p, q, r = b
    return (w*v-x*p-y*q-z*r, w*p+x*v+y*r-z*q, w*q-x*r+y*v+z*p, w*r+x*q-y*p+z*v)


def _qinv(q):
    return (q[0], -q[1], -q[2], -q[3])


def _quaternion(value, field):
    q = tuple(vector(value, field, 4))
    length = _norm(q)
    if length < 1e-12:
        raise ValueError('SPRING_QUATERNION_INVALID: ' + field)
    return _scale(q, 1 / length)


def _rotate(q, v):
    return _qmul(_qmul(q, (0.0,) + tuple(v)), _qinv(q))[1:]


def _between(a, b):
    a, b = _unit(a, (0, 1, 0)), _unit(b, (0, 1, 0))
    dot = max(-1.0, min(1.0, _dot(a, b)))
    if dot > 1 - 1e-12:
        return _IDENTITY
    if dot < -1 + 1e-12:
        axis = _unit(_cross(a, (1, 0, 0)))
        if _norm(axis) < 1e-10:
            axis = _unit(_cross(a, (0, 0, 1)))
        return (0.0,) + axis
    axis = _cross(a, b)
    return _quaternion((1 + dot,) + axis, 'rotation')


def _angle(a, b):
    return math.acos(max(-1.0, min(1.0, _dot(_unit(a), _unit(b)))))


def _rotvec_quaternion(rotvec):
    angle = _norm(rotvec)
    if angle < 1e-12:
        return _IDENTITY
    return (math.cos(angle / 2),) + _scale(rotvec, math.sin(angle / 2) / angle)


def _orientation_driver(data):
    """Accumulate measured short world rotation increments as an angular proxy."""
    result = [_ZERO]
    for previous, current in zip(data, data[1:]):
        delta = _qmul(current['quaternion'], _qinv(previous['quaternion']))
        if delta[0] < 0:
            delta = _scale(delta, -1)
        sine = _norm(delta[1:])
        angle = 2 * math.atan2(sine, max(0.0, delta[0]))
        increment = _scale(delta[1:], angle / sine) if sine > 1e-12 else _ZERO
        result.append(_add(result[-1], increment))
    return result


def _limit_direction(reference, requested, maximum):
    requested = _unit(requested, reference)
    angle = _angle(reference, requested)
    if angle <= maximum + 1e-12:
        return requested, False
    if maximum <= 1e-12:
        return tuple(reference), True
    axis = _unit(_cross(reference, requested))
    if _norm(axis) < 1e-10:
        axis = _unit(_cross(reference, (1, 0, 0)))
        if _norm(axis) < 1e-10:
            axis = _unit(_cross(reference, (0, 0, 1)))
    q = (math.cos(maximum / 2),) + _scale(axis, math.sin(maximum / 2))
    return _unit(_rotate(q, reference), reference), True


def _positions(positions):
    if not isinstance(positions, (list, tuple)) or not 2 <= len(positions) <= 601:
        raise ValueError('SPRING_SAMPLES_INVALID: 2..601 positions required')
    result = [tuple(vector(p, 'position')) for p in positions]
    if any(_norm(p) > 1e6 for p in result):
        raise ValueError('SPRING_DRIVER_RUNAWAY: coordinates exceed host solver bound')
    return result


def _derivatives(points, fps):
    # Causal finite differences preserve a genuine onset/stop rather than adding
    # periodic excitation. Initial velocity is the first forward difference;
    # initial acceleration is unknown, represented as zero for integration only.
    velocities = [_scale(_sub(points[1], points[0]), fps)]
    velocities += [_scale(_sub(points[i], points[i - 1]), fps) for i in range(1, len(points))]
    accelerations = [_ZERO]
    accelerations += [_scale(_sub(velocities[i], velocities[i - 1]), fps) for i in range(1, len(points))]
    return velocities, accelerations


def motion_metrics(offsets, fps, driver=None, settings=None):
    """Measures only observable quantities; absent excitation gives UNKNOWN."""
    points = _positions(offsets)
    fps = number(fps, 'fps', integer=True)
    if fps not in (30, 60):
        raise ValueError('PHYSICS_FPS_INVALID: 30 or 60')
    velocities, accelerations = _derivatives(points, fps)
    maximum = max(_norm(p) for p in points)
    out = {'units': {'displacement': 'm', 'velocity': 'm/s', 'acceleration': 'm/s²', 'time': 's'},
           'max_displacement_m': maximum,
           'max_velocity_m_s': max(_norm(v) for v in velocities),
           'max_acceleration_m_s2': max(_norm(a) for a in accelerations),
           'delay_s': None, 'overshoot_m': None, 'oscillation_period_s': None,
           'decay_rate_s_inv': None, 'settling_time_s': None,
           'measurement_status': {key: 'UNKNOWN' for key in
                                  ('delay_s', 'overshoot_m', 'oscillation_period_s', 'decay_rate_s_inv', 'settling_time_s')}}
    if driver is None or maximum < 1e-10:
        return out
    primary = _positions(driver)
    if len(primary) != len(points):
        raise ValueError('SPRING_DRIVER_SAMPLE_MISMATCH')
    driver_v, driver_a = _derivatives(primary, fps)
    if max(_norm(v) for v in driver_v) < 1e-9:
        return out
    excitation = [_scale(a, -1) for a in driver_a]
    if max(_norm(v) for v in excitation) > 1e-9:
        correlations = []
        for lag in range(min(fps, len(points) // 3) + 1):
            source = excitation[:len(points) - lag]
            response = points[lag:]
            denominator = math.sqrt(sum(_dot(x, x) for x in source) * sum(_dot(x, x) for x in response))
            correlations.append(sum(_dot(a, b) for a, b in zip(source, response)) / denominator if denominator > 1e-20 else -1)
        if max(correlations) > 0.2:
            out['delay_s'] = correlations.index(max(correlations)) / fps
            out['measurement_status']['delay_s'] = 'MEASURED_CROSS_CORRELATION'
    moving = [i for i, v in enumerate(driver_v) if _norm(v) > 1e-7]
    last_motion = moving[-1]
    if last_motion >= len(points) - 2:
        return out
    # Decay/period/settling need an observed rest tail; do not manufacture values
    # from configured damping or natural-frequency equations.
    config = settings or {}
    gravity = config.get('gravity', (0, 0, -9.81))
    equilibrium = _scale(gravity, config.get('gravity_influence', 0) * config.get('mass', 1) / config.get('stiffness', 40))
    tail = [_sub(p, equilibrium) for p in points[last_motion + 1:]]
    out['overshoot_m'] = max(_norm(p) for p in tail)
    out['measurement_status']['overshoot_m'] = 'MEASURED_REST_TAIL_EXCURSION'
    tolerance = min(0.001, max(1e-8, maximum * 0.01))
    for i in range(len(tail)):
        if len(tail) - i >= max(3, fps // 4) and all(_norm(p) <= tolerance for p in tail[i:]):
            out['settling_time_s'] = (i + 1) / fps
            out['measurement_status']['settling_time_s'] = 'MEASURED_WITHIN_TOLERANCE'
            break
    axis = max(range(3), key=lambda j: max(abs(p[j]) for p in tail))
    signal = [p[axis] for p in tail]
    crossings = [i for i in range(1, len(signal)) if signal[i - 1] < 0 <= signal[i]]
    if len(crossings) >= 2:
        out['oscillation_period_s'] = sum(b - a for a, b in zip(crossings, crossings[1:])) / ((len(crossings) - 1) * fps)
        out['measurement_status']['oscillation_period_s'] = 'MEASURED_REST_TAIL_CROSSINGS'
    peaks = [(i, abs(signal[i])) for i in range(1, len(signal) - 1)
             if abs(signal[i]) > abs(signal[i - 1]) and abs(signal[i]) >= abs(signal[i + 1]) and abs(signal[i]) > 1e-10]
    if len(peaks) >= 2 and peaks[0][1] > peaks[-1][1]:
        out['decay_rate_s_inv'] = math.log(peaks[0][1] / peaks[-1][1]) / ((peaks[-1][0] - peaks[0][0]) / fps)
        out['measurement_status']['decay_rate_s_inv'] = 'MEASURED_REST_TAIL_PEAKS'
    return out


def integrate_offsets(positions, fps, settings=None):
    """Semi-implicit Euler mass/damper integration with a proven timestep bound.

    x'' = -(k/m)(x + lag*v_driver) -(c/m)x' - excitation*a_driver
          + gravity_influence*gravity. Hard projection removes outward velocity.
    """
    primary = _positions(positions)
    config = normalize_settings('SPRING_BONE', settings, fps=fps)
    fps = int(fps)
    velocities, accelerations = _derivatives(primary, fps)
    if max(_norm(v) for v in velocities) > 10000 or max(_norm(a) for a in accelerations) > 1e7:
        raise ValueError('SPRING_DRIVER_RUNAWAY: primary derivatives exceed host solver bound')
    h = 1 / (fps * config['substeps'])
    k, c = config['stiffness'] / config['mass'], config['damping'] / config['mass']
    gravity = _scale(config.get('gravity', (0, 0, -9.81)), config['gravity_influence'])
    limit = config['max_displacement_m']
    offset, velocity = _ZERO, _ZERO
    offsets = [list(offset)]
    clamps = 0
    for i in range(1, len(primary)):
        for j in range(config['substeps']):
            blend = (j + 1) / config['substeps']
            driver_v = _add(_scale(velocities[i - 1], 1 - blend), _scale(velocities[i], blend))
            target = _scale(driver_v, -config['lag_s'])
            force = _add(_add(_scale(_sub(target, offset), k), _scale(velocity, -c)),
                         _add(_scale(accelerations[i], -config['excitation_scale']), gravity))
            velocity = _add(velocity, _scale(force, h))
            offset = _add(offset, _scale(velocity, h))
            if not all(math.isfinite(v) for v in offset + velocity) or _norm(velocity) > 1e6:
                raise ValueError('SPRING_SOLVER_RUNAWAY: non-finite or unbounded state')
            magnitude = _norm(offset)
            if magnitude > limit:
                normal = _scale(offset, 1 / magnitude)
                offset = _scale(normal, limit)
                outward = _dot(velocity, normal)
                if outward > 0:
                    velocity = _sub(velocity, _scale(normal, outward))
                clamps += 1
        offsets.append(list(offset))
    metrics = motion_metrics(offsets, fps, primary, config)
    metrics.update({'displacement_clamp_count': clamps, 'angle_clamp_count': 0,
                    'substeps': config['substeps'], 'configured_lag_s': config['lag_s'],
                    'timestep_stability_value': h * h * k + 2 * h * c,
                    'finite': True, 'backend': 'BOUNDED_SEMI_IMPLICIT_MASS_DAMPER'})
    return offsets, metrics


def _samples(driver, segment_count=None):
    if not isinstance(driver, (list, tuple)) or not 2 <= len(driver) <= 601:
        raise ValueError('SPRING_SAMPLES_INVALID: 2..601 samples required')
    if not isinstance(driver[0], dict):
        return [{'frame': i + 1, 'position': p, 'quaternion': list(_IDENTITY)}
                for i, p in enumerate(_positions(driver))]
    samples = []
    for i, row in enumerate(driver):
        if not isinstance(row, dict) or set(row) - {'frame', 'position', 'quaternion', 'directions'}:
            raise ValueError('SPRING_SAMPLE_UNKNOWN_FIELD')
        frame = number(row.get('frame', i + 1), 'frame', -1000000, 1000000, integer=True)
        if i and frame != samples[-1]['frame'] + 1:
            raise ValueError('SPRING_SAMPLES_NOT_CONTIGUOUS')
        sample = {'frame': frame, 'position': vector(row.get('position'), 'position'),
                  'quaternion': list(_quaternion(row.get('quaternion', _IDENTITY), 'quaternion'))}
        if 'directions' in row:
            directions = row['directions']
            if not isinstance(directions, (list, tuple)) or segment_count is not None and len(directions) != segment_count:
                raise ValueError('SPRING_DIRECTIONS_INVALID')
            sample['directions'] = []
            for d in directions:
                d = tuple(vector(d, 'directions'))
                if _norm(d) < 1e-12:
                    raise ValueError('SPRING_DIRECTION_ZERO')
                sample['directions'].append(list(_unit(d)))
        samples.append(sample)
    _positions([row['position'] for row in samples])
    return samples


def _delayed(values, index, seconds, fps):
    t = max(0.0, index - seconds * fps)
    a = min(len(values) - 1, int(t))
    b = min(len(values) - 1, a + 1)
    return _add(_scale(values[a], 1 - (t - a)), _scale(values[b], t - a))


def solve_chain(samples, lengths, fps, settings=None):
    """Root-attached segmented approximation with fixed lengths and local deltas.

    Optional per-bone directions are world-space unit directions. Rotations are
    deltas in each sampled primary orientation, with parent secondary deltas
    removed; the Blender adapter remains responsible for rest/basis conversion.
    """
    if not isinstance(lengths, (list, tuple)) or not 1 <= len(lengths) <= 64:
        raise ValueError('SPRING_CHAIN_LENGTHS_INVALID: 1..64 segments')
    lengths = [number(value, 'length_m', 1e-6, 100) for value in lengths]
    data = _samples(samples, len(lengths))
    config = normalize_settings('SPRING_BONE', settings, fps=fps)
    fps = int(fps)
    n = len(lengths)
    directions, primary_nodes = [], []
    for row in data:
        dirs = [tuple(d) for d in row.get('directions', [_rotate(row['quaternion'], (0, 1, 0))] * n)]
        directions.append(dirs)
        nodes = [tuple(row['position'])]
        for d, length in zip(dirs, lengths):
            nodes.append(_add(nodes[-1], _scale(d, length)))
        primary_nodes.append(nodes)
    raw, segment_metrics = [], []
    for j in range(n):
        # Successive segments receive the previous segment's simulated driver,
        # so chain response accumulates through hierarchy instead of copying one
        # oscillator onto every segment.
        primary = [nodes[j + 1] for nodes in primary_nodes]
        if j:
            primary = [_add(p, raw[j - 1][i]) for i, p in enumerate(primary)]
        local_config = dict(config, lag_s=min(1.0, config['lag_s'] * (j + 1)))
        offsets, metrics = integrate_offsets(primary, fps, local_config)
        raw.append([tuple(o) for o in offsets])
        segment_metrics.append(metrics)
    positions, rotations, offsets = [], [], []
    angle_clamps, distance_clamps, max_angle = 0, 0, 0.0
    budget = config['max_displacement_m'] / n
    for i, row in enumerate(data):
        nodes = [tuple(row['position'])]
        deltas, frame_offsets, parent_world_delta = [], [], _IDENTITY
        for j, length in enumerate(lengths):
            reference = directions[i][j]
            lagged = _unit(_delayed([ds[j] for ds in directions], i, config['lag_s'] * (j + 1), fps), reference)
            proposed = _add(_scale(lagged, length), raw[j][i])
            angular_max = math.radians(config['max_angle_deg'])
            displacement_max = 2 * math.asin(min(1.0, budget / (2 * length)))
            direction, clamp = _limit_direction(reference, proposed, min(angular_max, displacement_max))
            if clamp:
                if _angle(reference, _unit(proposed, reference)) > angular_max + 1e-10:
                    angle_clamps += 1
                else:
                    distance_clamps += 1
            endpoint = _add(nodes[-1], _scale(direction, length))
            nodes.append(endpoint)
            frame_offsets.append(list(_sub(endpoint, primary_nodes[i][j + 1])))
            world_delta = _between(reference, direction)
            q = row['quaternion']
            local_delta = _qmul(_qmul(_qinv(q), _qmul(_qinv(parent_world_delta), world_delta)), q)
            if rotations and _dot(local_delta, rotations[-1][j]) < 0:
                local_delta = _scale(local_delta, -1)
            deltas.append(list(local_delta))
            parent_world_delta = world_delta
            max_angle = max(max_angle, math.degrees(_angle(reference, direction)))
        positions.append([list(node) for node in nodes])
        rotations.append(deltas)
        offsets.append(frame_offsets)
    flat_metrics = [motion_metrics([row[j] for row in offsets], fps,
                                  [row[j + 1] for row in primary_nodes], config) for j in range(n)]
    metrics = dict(flat_metrics[-1])
    for key in ('max_displacement_m', 'max_velocity_m_s', 'max_acceleration_m_s2'):
        metrics[key] = max(row[key] for row in flat_metrics)
    metrics.update({'backend': 'ROOT_ATTACHED_SEGMENTED_MASS_DAMPER', 'finite': True,
                    'max_angle_deg': max_angle, 'max_root_detachment_m': 0.0,
                    'max_segment_length_error_m': max(abs(_norm(_sub(row[j + 1], row[j])) - lengths[j])
                                                      for row in positions for j in range(n)),
                    'displacement_clamp_count': distance_clamps + sum(m['displacement_clamp_count'] for m in segment_metrics),
                    'angle_clamp_count': angle_clamps, 'segment_count': n,
                    'configured_lag_s': config['lag_s'], 'segment_metrics': flat_metrics})
    frames = [row['frame'] for row in data]
    return {'schema': 'production.secondary_samples.v1', 'frames': frames,
            'positions': positions, 'rotations': rotations, 'offsets': offsets,
            'tracks': [{'segment': j, 'frames': frames, 'positions': [row[j + 1] for row in positions],
                        'rotation_quaternion': [row[j] for row in rotations], 'offsets': [row[j] for row in offsets]}
                       for j in range(n)], 'metrics': metrics}


def solve(driver, settings=None, fps=30, kind='SPRING_BONE'):
    """Solve one controlled offset track or rotation about a fixed accessory root."""
    if kind not in ('SPRING_BONE', 'HAIR', 'ACCESSORY', 'CHAIN', 'CABLE', 'SOFT'):
        raise ValueError('PHYSICS_SUBSYSTEM_UNSUPPORTED: ' + str(kind))
    data = _samples(driver)
    config = normalize_settings(kind, settings, fps=fps)
    primary = [row['position'] for row in data]
    # Rotation-only drivers excite motion through a one-metre directional probe.
    # The output accessory origin remains exactly equal to its primary origin.
    excitation = [_add(row['position'], _rotate(row['quaternion'], (0, 1, 0))) for row in data]
    offsets, metrics = integrate_offsets(excitation if kind == 'ACCESSORY' else primary, fps, config)
    angular_offsets = [_ZERO] * len(data)
    angular_metrics = None
    if kind == 'ACCESSORY' and config['max_angle_deg'] > 0:
        angular_config = dict(config, gravity_influence=0.0,
                              max_displacement_m=math.radians(config['max_angle_deg']))
        angular_offsets, angular_metrics = integrate_offsets(_orientation_driver(data), fps, angular_config)
    frames = [row['frame'] for row in data]
    rotations, positions, rendered_offsets = [], [], []
    angle_clamps, max_angle = 0, 0.0
    for row, offset in zip(data, offsets):
        q = row['quaternion']
        if kind == 'ACCESSORY':
            reference = _rotate(q, (0, 1, 0))
            requested = _add(reference, offset)
            direction, clamp = _limit_direction(reference, requested, math.radians(config['max_angle_deg']))
            angle_clamps += int(clamp)
            world_delta = _qmul(_rotvec_quaternion(angular_offsets[len(rotations)]), _between(reference, direction))
            if world_delta[0] < 0:
                world_delta = _scale(world_delta, -1)
            rotation_angle = 2 * math.atan2(_norm(world_delta[1:]), world_delta[0])
            maximum = math.radians(config['max_angle_deg'])
            if rotation_angle > maximum + 1e-12:
                world_delta = _rotvec_quaternion(_scale(_unit(world_delta[1:]), maximum))
                rotation_angle = maximum
                angle_clamps += 1
            delta = _qmul(_qmul(_qinv(q), world_delta), q)
            max_angle = max(max_angle, math.degrees(rotation_angle))
            rotations.append(list(delta))
            positions.append(list(row['position']))
            rendered_offsets.append([0.0, 0.0, 0.0])
        else:
            rotations.append(list(_IDENTITY))
            positions.append(list(_add(row['position'], offset)))
            rendered_offsets.append(offset)
    metrics.update({'kind': kind, 'angle_clamp_count': angle_clamps, 'max_angle_deg': max_angle,
                    'max_root_detachment_m': 0.0 if kind == 'ACCESSORY' else None})
    if angular_metrics is not None:
        metrics['angular_measurements'] = {'max_angular_offset_rad': angular_metrics['max_displacement_m'],
              'max_angular_velocity_rad_s': angular_metrics['max_velocity_m_s'],
              'max_angular_acceleration_rad_s2': angular_metrics['max_acceleration_m_s2'],
              'angle_clamp_count': angular_metrics['displacement_clamp_count']}
    return {'schema': 'production.secondary_samples.v1', 'frames': frames,
            'positions': positions, 'rotations': rotations, 'offsets': rendered_offsets,
            'solver_offsets': offsets, 'angular_offsets_rad': [list(v) for v in angular_offsets],
            'rotation_space': 'PRIMARY_LOCAL_DELTA_WXYZ', 'tracks': [{'frames': frames, 'positions': positions,
              'rotation_quaternion': rotations, 'offsets': rendered_offsets}], 'metrics': metrics}
