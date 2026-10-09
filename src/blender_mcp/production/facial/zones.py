"""Facial zones in a normalized face frame, shared verbatim by host code and the Blender payload.

Coordinates: origin at the midpoint between the eye centres, unit = inter-ocular distance (IOD),
x = character left, z = up, y = forward (out of the face). The mouth landmark is measured per rig
(``mouth`` = (0, zm), half-width ``mw``); nothing here assumes a particular rig or naming.

Restricted style on purpose (no lambda/class/imports beyond math): the module source is shipped into
safe-mode Blender payloads with ``inspect.getsource``.
"""
import math

ZONES = ('LEFT_UPPER_LID', 'RIGHT_UPPER_LID', 'LEFT_LOWER_LID', 'RIGHT_LOWER_LID', 'LEFT_BROW', 'RIGHT_BROW',
         'NOSE', 'LEFT_CHEEK', 'RIGHT_CHEEK', 'LEFT_LIP_CORNER', 'RIGHT_LIP_CORNER', 'UPPER_LIP', 'LOWER_LIP',
         'JAW', 'LEFT_EYE', 'RIGHT_EYE', 'OTHER')


def fz_zone(x, z, zm, mw):
    """Zone of one point. Priority: lip corners, lips, lids, brows, nose, cheeks, jaw."""
    for side, tag in ((1.0, 'LEFT'), (-1.0, 'RIGHT')):
        if math.hypot(x - side * mw, z - zm) < 0.22:
            return tag + '_LIP_CORNER'
    if abs(x) < mw * 1.15 and zm <= z < zm + 0.34:
        return 'UPPER_LIP'
    if abs(x) < mw * 1.15 and zm - 0.34 <= z < zm:
        return 'LOWER_LIP'
    for side, tag in ((1.0, 'LEFT'), (-1.0, 'RIGHT')):
        dx = x - side * 0.5
        if math.hypot(dx, z) < 0.40 and z < 0.30:
            return tag + ('_UPPER_LID' if z >= -0.03 else '_LOWER_LID')
    for side, tag in ((1.0, 'LEFT'), (-1.0, 'RIGHT')):
        if 0.22 <= z <= 0.95 and 0.0 <= x * side <= 1.10:
            return tag + '_BROW'
    if abs(x) < 0.28 and zm + 0.30 < z < -0.12:
        return 'NOSE'
    for side, tag in ((1.0, 'LEFT'), (-1.0, 'RIGHT')):
        if 0.28 <= x * side <= 1.10 and zm * 0.95 <= z < -0.22:
            return tag + '_CHEEK'
    if z < zm - 0.30 and abs(x) < 1.2:
        return 'JAW'
    return 'OTHER'


def fz_frame_point(point, origin, left, up, forward, iod):
    """World point -> (x, y, z) in the normalized face frame."""
    dx = point[0] - origin[0]
    dy = point[1] - origin[1]
    dz = point[2] - origin[2]
    return ((dx * left[0] + dy * left[1] + dz * left[2]) / iod,
            (dx * forward[0] + dy * forward[1] + dz * forward[2]) / iod,
            (dx * up[0] + dy * up[1] + dz * up[2]) / iod)


def fz_frame_vector(vector, left, up, forward, iod):
    return ((vector[0] * left[0] + vector[1] * left[1] + vector[2] * left[2]) / iod,
            (vector[0] * forward[0] + vector[1] * forward[1] + vector[2] * forward[2]) / iod,
            (vector[0] * up[0] + vector[1] * up[1] + vector[2] * up[2]) / iod)


def fz_field_stats(points, vectors, frame, threshold_m):
    """Summary of a displacement field (world points/vectors) in face units.

    Returns energy shares per zone, the energy-weighted centroid and mean direction, left/right share
    and mouth-relative motion terms used to tell close from open, pucker from stretch, etc.
    """
    origin, left, up, forward, iod, zm, mw = frame
    energy_total = 0.0
    shares = {}
    cx = cy = cz = 0.0
    mx = my = mz = 0.0
    left_energy = 0.0
    moved = 0
    peak = 0.0
    toward_mouth = 0.0
    vertical_to_mouth = 0.0
    for index in range(len(points)):
        v = vectors[index]
        length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        if length <= threshold_m:
            continue
        moved += 1
        if length > peak:
            peak = length
        p = fz_frame_point(points[index], origin, left, up, forward, iod)
        d = fz_frame_vector(v, left, up, forward, iod)
        e = length * length
        energy_total += e
        zone = fz_zone(p[0], p[2], zm, mw)
        shares[zone] = shares.get(zone, 0.0) + e
        cx += p[0] * e
        cy += p[1] * e
        cz += p[2] * e
        mx += d[0] * e
        my += d[1] * e
        mz += d[2] * e
        if p[0] > 0:
            left_energy += e
        radial = math.hypot(p[0], p[2] - zm)
        if radial > 1e-9:
            toward_mouth += -((p[0] * d[0] + (p[2] - zm) * d[2]) / radial) * e
        if p[2] >= zm:
            vertical_to_mouth += -d[2] * e
        else:
            vertical_to_mouth += d[2] * e
    if energy_total <= 0:
        return {'moved_vertices': 0, 'max_displacement_m': 0.0, 'energy': 0.0, 'zones': {}}
    scale = 1.0 / energy_total
    rms = math.sqrt(energy_total / moved)
    return {'moved_vertices': moved, 'max_displacement_m': peak, 'energy': energy_total,
            'rms_displacement_m': rms,
            'zones': {k: round(v * scale, 4) for k, v in shares.items()},
            'centroid': [round(cx * scale, 4), round(cy * scale, 4), round(cz * scale, 4)],
            'direction': [round(mx * scale / (rms / iod), 4), round(my * scale / (rms / iod), 4), round(mz * scale / (rms / iod), 4)],
            'left_share': round(left_energy * scale, 4),
            'toward_mouth': round(toward_mouth * scale / (rms / iod), 4),
            'lips_converge': round(vertical_to_mouth * scale / (rms / iod), 4)}
