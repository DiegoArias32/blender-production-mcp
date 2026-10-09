"""Controlled synthetic facial fixtures for M7 (no production assets, no external files).

A procedural head (latitude/longitude ellipsoid with nose, brows, cheeks, lips and chin), two
spherical eyeballs with iris/pupil caps seen through lid openings, a mouth opening, a hinged jaw
and a neck cylinder. The same geometry is driven by one of several facial systems:

    shape_keys     ARKit-like shape keys on the face + eye-look shape keys on the eyeballs
    custom_keys    the same deformations under non-ARKit names (mapping must not need ARKit)
    obfuscated     the same deformations named "Key 1".."Key N" (evidence must come from displacement)
    bones          deform bones driven by CTRL bones (Copy Transforms) with Limit constraints
    hybrid         jaw/eye bones + shape keys for lids/brows/lips, a jaw-driven corrective and a
                   redundant jaw shape key (the analyzer must not drive JAW through both systems)
    eye_target     bones whose eyes aim at a target control with Damped Track
    ambiguous      shape keys that each move several facial areas at once
    unsupported    a head with no facial controls

Defect switches (on top of a system): blink_incomplete, eye_penetration, lip_penetration,
jaw_neck_collision; intentional asymmetry and asymmetric control limits are separate switches.

Every lid vertex rides on a sphere around the eyeball centre (radius r_eye + gap) when it closes, so a
clean blink never enters the eye; the defects break exactly that rule. Units: meters, face looks -Y,
+Z up, character left is +X.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import random

HEAD_RADII = (0.075, 0.088, 0.108)
EYE_UW = (0.33, 0.14)
EYE_OPENING = (0.13, 0.065)
EYE_RADIUS = 0.0115
LID_GAP = 0.0008
MOUTH_W = -0.44
MOUTH_OPENING = (0.22, 0.013)
JAW_ANGLE_DEG = 18.0
RINGS, SEGMENTS = 64, 96

ARKIT = {
    'blink_L': 'eyeBlinkLeft', 'blink_R': 'eyeBlinkRight', 'squint_L': 'eyeSquintLeft', 'squint_R': 'eyeSquintRight',
    'wide_L': 'eyeWideLeft', 'wide_R': 'eyeWideRight', 'brow_down_L': 'browDownLeft', 'brow_down_R': 'browDownRight',
    'brow_inner_up': 'browInnerUp', 'brow_outer_up_L': 'browOuterUpLeft', 'brow_outer_up_R': 'browOuterUpRight',
    'cheek_L': 'cheekSquintLeft', 'cheek_R': 'cheekSquintRight', 'sneer_L': 'noseSneerLeft', 'sneer_R': 'noseSneerRight',
    'jaw_open': 'jawOpen', 'jaw_forward': 'jawForward', 'jaw_left': 'jawLeft', 'jaw_right': 'jawRight',
    'mouth_close': 'mouthClose', 'pucker': 'mouthPucker', 'funnel': 'mouthFunnel',
    'smile_L': 'mouthSmileLeft', 'smile_R': 'mouthSmileRight', 'frown_L': 'mouthFrownLeft', 'frown_R': 'mouthFrownRight',
    'stretch_L': 'mouthStretchLeft', 'stretch_R': 'mouthStretchRight', 'press_L': 'mouthPressLeft', 'press_R': 'mouthPressRight',
    'roll_lower': 'mouthRollLower', 'roll_upper': 'mouthRollUpper', 'upper_up_L': 'mouthUpperUpLeft',
    'upper_up_R': 'mouthUpperUpRight', 'lower_down_L': 'mouthLowerDownLeft', 'lower_down_R': 'mouthLowerDownRight',
}
CUSTOM = {
    'blink_L': 'Blink.L', 'blink_R': 'Blink.R', 'squint_L': 'LidRaise.L', 'squint_R': 'LidRaise.R', 'wide_L': 'LidWide.L',
    'wide_R': 'LidWide.R', 'brow_down_L': 'BrowFurrow.L', 'brow_down_R': 'BrowFurrow.R', 'brow_inner_up': 'BrowWorry',
    'brow_outer_up_L': 'BrowArch.L', 'brow_outer_up_R': 'BrowArch.R', 'cheek_L': 'CheekPuffUp.L', 'cheek_R': 'CheekPuffUp.R',
    'sneer_L': 'NoseWrinkle.L', 'sneer_R': 'NoseWrinkle.R', 'jaw_open': 'Jaw_Drop', 'jaw_forward': 'Jaw_Thrust',
    'jaw_left': 'Jaw_Shift.L', 'jaw_right': 'Jaw_Shift.R', 'mouth_close': 'Lips_Seal', 'pucker': 'Lips_Kiss',
    'funnel': 'Lips_Trumpet', 'smile_L': 'Grin.L', 'smile_R': 'Grin.R', 'frown_L': 'Sulk.L', 'frown_R': 'Sulk.R',
    'stretch_L': 'LipsWide.L', 'stretch_R': 'LipsWide.R', 'press_L': 'LipsTight.L', 'press_R': 'LipsTight.R',
    'roll_lower': 'LowerLipIn', 'roll_upper': 'UpperLipIn', 'upper_up_L': 'Snarl.L', 'upper_up_R': 'Snarl.R',
    'lower_down_L': 'LowerLipDown.L', 'lower_down_R': 'LowerLipDown.R',
}
EYE_KEYS = {'look_up': 'eyeLookUp', 'look_down': 'eyeLookDown', 'look_in': 'eyeLookIn', 'look_out': 'eyeLookOut'}
EYE_LOOK_DEG = 25.0

VARIANTS = {
    'shape_keys': {'system': 'shape_keys', 'names': 'arkit'},
    'custom_keys': {'system': 'shape_keys', 'names': 'custom'},
    'obfuscated': {'system': 'shape_keys', 'names': 'obfuscated'},
    'bones': {'system': 'bones'},
    'hybrid': {'system': 'hybrid'},
    'eye_target': {'system': 'bones', 'eye_target': True},
    'asymmetric_controls': {'system': 'bones', 'asymmetric_limits': True},
    'intentional_asymmetry': {'system': 'shape_keys', 'names': 'arkit', 'intentional_asymmetry': True},
    'blink_defect': {'system': 'shape_keys', 'names': 'arkit', 'defects': ['blink_incomplete']},
    'eye_penetration': {'system': 'shape_keys', 'names': 'arkit', 'defects': ['eye_penetration']},
    'lip_penetration': {'system': 'shape_keys', 'names': 'arkit', 'defects': ['lip_penetration']},
    'jaw_neck_collision': {'system': 'shape_keys', 'names': 'arkit', 'defects': ['jaw_neck_collision']},
    'ambiguous': {'system': 'ambiguous'},
    'unsupported': {'system': 'none'},
    'conflicting_driver': {'system': 'shape_keys', 'names': 'arkit', 'driver_conflict': True},
    # Partial rigs: only one facial area is controllable (eyes keep their look keys).
    'eyelid_rig': {'system': 'shape_keys', 'names': 'arkit', 'subset': ('blink', 'wide', 'squint')},
    'jaw_rig': {'system': 'shape_keys', 'names': 'arkit', 'subset': ('jaw',)},
    'lip_rig': {'system': 'shape_keys', 'names': 'arkit', 'subset': ('mouth_close', 'pucker', 'funnel', 'smile', 'frown', 'stretch',
                                                                      'press', 'roll', 'upper_up', 'lower_down')},
}


# ------------------------------------------------------------------ pure helpers (unit-testable)

def smoothstep(edge0, edge1, x):
    if edge0 == edge1:
        return 1.0 if x >= edge1 else 0.0
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


def gauss(x, mu, sigma):
    return math.exp(-0.5 * ((x - mu) / sigma) ** 2)


def bump(u, w, cu, cw, su, sw):
    return gauss(u, cu, su) * gauss(w, cw, sw)


def eye_rho(u, w, side):
    ue, we = EYE_UW[0] * side, EYE_UW[1]
    return math.sqrt(((u - ue) / EYE_OPENING[0]) ** 2 + ((w - we) / EYE_OPENING[1]) ** 2)


def mouth_rho(u, w):
    return math.sqrt((u / MOUTH_OPENING[0]) ** 2 + ((w - MOUTH_W) / MOUTH_OPENING[1]) ** 2)


def rim_w(u, side, upper=True):
    ue, we = EYE_UW[0] * side, EYE_UW[1]
    x = min(1.0, abs(u - ue) / EYE_OPENING[0])
    offset = EYE_OPENING[1] * math.sqrt(max(0.0, 1 - x * x))
    return we + offset if upper else we - offset


def displacement(u, w, front):
    """Feature height (m) along the outward normal on the front of the head."""
    fw = smoothstep(0.15, 0.55, front)
    h = 0.016 * bump(u, w, 0, -0.05, 0.10, 0.22) + 0.006 * bump(u, w, 0, -0.18, 0.07, 0.08)
    for side in (-1, 1):
        h += 0.005 * bump(u, w, 0.33 * side, 0.33, 0.22, 0.06)
        h += 0.005 * bump(u, w, 0.40 * side, -0.12, 0.15, 0.12)
        h -= 0.004 * bump(u, w, EYE_UW[0] * side, EYE_UW[1], 0.17, 0.10)
    h += 0.006 * bump(u, w, 0, -0.38, 0.20, 0.05) + 0.006 * bump(u, w, 0, -0.50, 0.18, 0.05)
    h += 0.008 * bump(u, w, 0, -0.72, 0.18, 0.10)
    return h * fw


# ------------------------------------------------------------------ geometry (Blender)

def head_point(center, u, w, front_sign=-1.0, asym=None):
    """Surface point at unit-sphere coordinates (u, w) on the front (y<0) or back."""
    from mathutils import Vector
    ax, ay, az = HEAD_RADII
    rest = max(0.0, 1.0 - u * u - w * w)
    d = Vector((u, front_sign * math.sqrt(rest), w))
    normal = Vector((d.x / ax, d.y / ay, d.z / az)).normalized()
    front = -d.y
    h = displacement(u, w, front)
    if asym:
        h += asym(u, w, front)
    return Vector(center) + Vector((ax * d.x, ay * d.y, az * d.z)) + normal * h, normal


def scar(u, w, front):
    """A healed scar ridge on the left cheek, deliberately one-sided (intentional asymmetry)."""
    return 0.003 * bump(u, w, 0.42, -0.02, 0.05, 0.16) * smoothstep(0.15, 0.55, front)


def eye_center(center, side, scale=1.0):
    surface, normal = head_point(center, EYE_UW[0] * side, EYE_UW[1])
    return surface - normal * (EYE_RADIUS * scale - 0.0010)


def build_head(center, options):
    """Lat/long head with eye and mouth openings; returns (verts, faces, per-vertex (u, w, front))."""
    ax, ay, az = HEAD_RADII
    from mathutils import Vector
    verts, uvw = [], []
    asym = scar if options.get('intentional_asymmetry') else None
    for i in range(1, RINGS):
        theta = math.pi * i / RINGS
        for j in range(SEGMENTS):
            phi = 2 * math.pi * j / SEGMENTS
            d = Vector((math.sin(theta) * math.cos(phi), math.sin(theta) * math.sin(phi), math.cos(theta)))
            front = -d.y
            normal = Vector((d.x / ax, d.y / ay, d.z / az)).normalized()
            h = displacement(d.x, d.z, front)
            if asym:
                h += asym(d.x, d.z, front)
            verts.append(Vector(center) + Vector((ax * d.x, ay * d.y, az * d.z)) + normal * h)
            uvw.append((d.x, d.z, front))
    top = len(verts); verts.append(Vector(center) + Vector((0, 0, az))); uvw.append((0.0, 1.0, 0.0))
    bottom = len(verts); verts.append(Vector(center) + Vector((0, 0, -az))); uvw.append((0.0, -1.0, 0.0))

    def index(i, j):
        return (i - 1) * SEGMENTS + (j % SEGMENTS)

    faces = []
    for j in range(SEGMENTS):
        faces.append((top, index(1, j + 1), index(1, j)))
        faces.append((bottom, index(RINGS - 1, j), index(RINGS - 1, j + 1)))
    for i in range(1, RINGS - 1):
        for j in range(SEGMENTS):
            faces.append((index(i, j), index(i, j + 1), index(i + 1, j + 1), index(i + 1, j)))

    def opened(face):
        us = [uvw[k][0] for k in face]; ws = [uvw[k][1] for k in face]; fs = [uvw[k][2] for k in face]
        u, w, f = sum(us) / len(us), sum(ws) / len(ws), sum(fs) / len(fs)
        if f < 0.2:
            return False
        scale = options.get('eye_scale', {})
        for side in (-1, 1):
            factor = scale.get(side, 1.0)
            ue, we = EYE_UW[0] * side, EYE_UW[1]
            if ((u - ue) / (EYE_OPENING[0] * factor)) ** 2 + ((w - we) / (EYE_OPENING[1] * factor)) ** 2 < 1:
                return True
        return mouth_rho(u, w) < 1
    faces = [f for f in faces if not opened(f)]
    return verts, faces, uvw


def region_weights(uvw):
    """Smooth per-vertex weights of each facial area (also used for bone skinning)."""
    rows = {}
    for k, (u, w, f) in enumerate(uvw):
        frontish = smoothstep(0.15, 0.45, f)
        r = {}
        for side, tag in ((1, 'L'), (-1, 'R')):
            rho = eye_rho(u, w, side)
            r['lid_up_' + tag] = smoothstep(1.9, 1.0, rho) * (1.0 if w >= EYE_UW[1] - 0.005 else 0.0) * frontish
            r['lid_low_' + tag] = smoothstep(1.6, 1.0, rho) * (1.0 if w < EYE_UW[1] - 0.005 else 0.0) * frontish
            side_u = u * side
            fv = gauss(w, 0.33, 0.07) * (1.0 if 0.06 < side_u < 0.66 else 0.0) * frontish
            r['brow_in_' + tag] = fv * smoothstep(0.42, 0.16, side_u)
            r['brow_out_' + tag] = fv * smoothstep(0.24, 0.50, side_u)
            r['cheek_' + tag] = bump(u, w, 0.40 * side, -0.10, 0.10, 0.08) * frontish
            r['sneer_' + tag] = bump(u, w, 0.10 * side, -0.12, 0.05, 0.05) * frontish
            r['corner_' + tag] = bump(u, w, MOUTH_OPENING[0] * side, MOUTH_W, 0.07, 0.05) * frontish
            r['lipside_' + tag] = (1.0 if side_u > 0 else 0.0)
        lips = smoothstep(0.30, 0.16, abs(u)) * frontish
        r['lip_up'] = lips * gauss(w, MOUTH_W + 0.035, 0.035) * (1.0 if w >= MOUTH_W else 0.0)
        r['lip_low'] = lips * gauss(w, MOUTH_W - 0.035, 0.035) * (1.0 if w < MOUTH_W else 0.0)
        r['jaw'] = smoothstep(MOUTH_W + 0.012, MOUTH_W - 0.05, w) * smoothstep(0.78, 0.48, abs(u)) * smoothstep(-0.35, 0.15, f)
        rows[k] = r
    return rows


def shape_deltas(verts, uvw, center, options):
    """Absolute target positions per semantic shape (dict name -> list of Vector)."""
    from mathutils import Vector, Matrix
    weights = region_weights(uvw)
    defects = set(options.get('defects', []))
    eyes = {1: eye_center(center, 1, options.get('eye_scale', {}).get(1, 1.0)),
            -1: eye_center(center, -1, options.get('eye_scale', {}).get(-1, 1.0))}
    jaw_angle = math.radians(JAW_ANGLE_DEG * (2.3 if 'jaw_neck_collision' in defects else 1.0))
    hinge = Vector(center) + Vector((0, 0.015, -0.035 if 'jaw_neck_collision' not in defects else -0.06))
    shapes = {}

    # Shapes are built on the same (possibly asymmetric) surface as the head; otherwise every key would also
    # flatten the scar through the tiny tails of the region weights (measured: right-side keys moving the left cheek).
    asym = scar if options.get('intentional_asymmetry') else None

    def on_surface(u, w, front_sign=-1.0):
        return head_point(center, u, w, front_sign, asym)[0]

    def ride_eye(point, side, radius_offset):
        e = eyes[side]
        v = point - e
        radius = EYE_RADIUS * options.get('eye_scale', {}).get(side, 1.0) + radius_offset
        if v.length < radius:
            point = e + v.normalized() * radius
        return point

    def lid_shape(side, tag, fraction, inside):
        """Upper lid band slides down toward the lower rim at each u, riding over the eyeball."""
        scale = options.get('eye_scale', {}).get(side, 1.0)
        out = []
        for k, p in enumerate(verts):
            c = weights[k]['lid_up_' + tag]
            if c <= 0:
                out.append(p.copy()); continue
            u, w, f = uvw[k]
            up, low = rim_w(u, side, True), rim_w(u, side, False)
            q = on_surface(u, w + c * ((low + (1 - fraction) * (up - low)) - up))
            e = eyes[side]
            v = q - e
            if inside and c > 0.6:
                q = e + v.normalized() * (EYE_RADIUS * scale - 0.0015)  # defect: lid sinks into the eye
            elif v.length < EYE_RADIUS * scale + LID_GAP:
                q = e + v.normalized() * (EYE_RADIUS * scale + LID_GAP)
            out.append(q)
        return out

    for side, tag in ((1, 'L'), (-1, 'R')):
        fraction = 0.95 if 'blink_incomplete' not in defects else 0.45
        shapes['blink_' + tag] = lid_shape(side, tag, fraction, 'eye_penetration' in defects and side == 1)
        wide, squint = [], []
        for k, p in enumerate(verts):
            u, w, f = uvw[k]
            c_up = weights[k]['lid_up_' + tag]; c_low = weights[k]['lid_low_' + tag]
            wide.append(ride_eye(on_surface(u, w + 0.025 * c_up), side, LID_GAP) if c_up > 0 else p.copy())
            if c_low > 0:
                dw = c_low * 0.55 * (EYE_UW[1] - rim_w(u, side, False))  # 0.35 before the visual review
                squint.append(ride_eye(on_surface(u, w + dw), side, LID_GAP))
            else:
                squint.append(p.copy())
        shapes['wide_' + tag], shapes['squint_' + tag] = wide, squint

    def moved(fn):
        out = []
        for k, p in enumerate(verts):
            u, w, f = uvw[k]
            du, dw, dy = fn(k, u, w, f)
            if du == 0 and dw == 0 and dy == 0:
                out.append(p.copy())
            else:
                q = on_surface(u + du, w + dw) if f > 0.05 else p.copy()
                out.append(q + Vector((0, dy, 0)))
        return out

    W = weights
    # Expression magnitudes were doubled after the first visual review: a 40 % restrained expression moved at most
    # 1.8 mm (measured maxima at full value: brows 4.6-6.2 mm, cheeks 3.7, sneer 2.4, lip press 2.0) and was invisible
    # in the close-up renders. G scales the sliding and the outward relief of the expression shapes together.
    G = 2.0
    shapes['brow_inner_up'] = moved(lambda k, u, w, f: (0, G * 0.05 * (W[k]['brow_in_L'] + W[k]['brow_in_R']), 0))
    for side, tag in ((1, 'L'), (-1, 'R')):
        shapes['brow_outer_up_' + tag] = moved(lambda k, u, w, f, t=tag: (0, G * 0.05 * W[k]['brow_out_' + t], 0))
        # Sliding skin along a smooth surface is invisible in shaded renders (measured on the first previews), so the
        # brow, cheek and nose shapes also bulge outward (-Y) the way compressed soft tissue does.
        shapes['brow_down_' + tag] = moved(lambda k, u, w, f, t=tag, s=side: (
            G * -0.02 * W[k]['brow_in_' + t] * s, G * -0.035 * (0.8 * W[k]['brow_in_' + t] + 0.5 * W[k]['brow_out_' + t]),
            G * -0.0015 * W[k]['brow_in_' + t]))
        shapes['cheek_' + tag] = moved(lambda k, u, w, f, t=tag: (0, G * 0.03 * W[k]['cheek_' + t], G * -0.002 * W[k]['cheek_' + t]))
        shapes['sneer_' + tag] = moved(lambda k, u, w, f, t=tag: (0, G * 0.025 * W[k]['sneer_' + t], G * -0.001 * W[k]['sneer_' + t]))
        shapes['smile_' + tag] = moved(lambda k, u, w, f, t=tag, s=side: (G * 0.02 * s * W[k]['corner_' + t], G * 0.04 * W[k]['corner_' + t], G * 0.002 * W[k]['corner_' + t]))
        shapes['frown_' + tag] = moved(lambda k, u, w, f, t=tag: (0, G * -0.035 * W[k]['corner_' + t], 0))
        shapes['stretch_' + tag] = moved(lambda k, u, w, f, t=tag, s=side: (G * 0.03 * s * W[k]['corner_' + t], 0, 0))
        shapes['press_' + tag] = moved(lambda k, u, w, f, t=tag: (
            0, G * (-0.010 * W[k]['lip_up'] + 0.010 * W[k]['lip_low']) * W[k]['lipside_' + t], G * 0.0015 * (W[k]['lip_up'] + W[k]['lip_low']) * W[k]['lipside_' + t]))
        shapes['upper_up_' + tag] = moved(lambda k, u, w, f, t=tag: (0, G * 0.025 * W[k]['lip_up'] * W[k]['lipside_' + t], 0))
        shapes['lower_down_' + tag] = moved(lambda k, u, w, f, t=tag: (0, G * -0.025 * W[k]['lip_low'] * W[k]['lipside_' + t], 0))
    shapes['pucker'] = moved(lambda k, u, w, f: (-0.30 * u * (W[k]['lip_up'] + W[k]['lip_low']), 0, -0.005 * (W[k]['lip_up'] + W[k]['lip_low'])))
    shapes['funnel'] = moved(lambda k, u, w, f: (-0.15 * u * (W[k]['lip_up'] + W[k]['lip_low']), 0.02 * W[k]['lip_up'] - 0.02 * W[k]['lip_low'],
                                                  -0.004 * (W[k]['lip_up'] + W[k]['lip_low'])))
    shapes['roll_lower'] = moved(lambda k, u, w, f: (0, 0.012 * W[k]['lip_low'], 0.003 * W[k]['lip_low']))
    shapes['roll_upper'] = moved(lambda k, u, w, f: (0, -0.010 * W[k]['lip_up'], 0.003 * W[k]['lip_up']))
    # ARKit-like convention: mouthClose closes the lips *while the jaw is open*; alone it overlaps them.
    # 0.2424 was calibrated from measured gaps (rest 4.92 mm, jaw gain 23.37 mm, 118.95 mm per unit lift) so that
    # jawOpen=1 + mouthClose=1 meets at ~0 mm; the defect keeps 0.40, which crosses the lips at full jaw + close.
    close_lift = 0.2424 if 'lip_penetration' not in defects else 0.40
    shapes['mouth_close'] = moved(lambda k, u, w, f: (0, close_lift * W[k]['lip_low'] - 0.02 * W[k]['lip_up'], 0))
    rot = Matrix.Rotation(jaw_angle, 4, 'X')
    jaw, forward, left, right = [], [], [], []
    for k, p in enumerate(verts):
        wj = W[k]['jaw']
        if wj <= 0:
            for target in (jaw, forward, left, right):
                target.append(p.copy())
            continue
        rotated = hinge + (rot @ (p - hinge))
        jaw.append(p.lerp(rotated, wj))
        forward.append(p + Vector((0, -0.004 * wj, 0)))
        left.append(p + Vector((0.004 * wj, 0, 0)))
        right.append(p + Vector((-0.004 * wj, 0, 0)))
    shapes['jaw_open'], shapes['jaw_forward'], shapes['jaw_left'], shapes['jaw_right'] = jaw, forward, left, right
    return shapes, eyes, hinge


def _material(name, color):
    import bpy
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.diffuse_color = color
    return material


def make_eye(name, center, side, scale, look_keys, collection):
    import bmesh
    import bpy
    from mathutils import Matrix, Vector
    mesh = bpy.data.meshes.new(name + 'Mesh')
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=EYE_RADIUS * scale)
    bmesh.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0), matrix=Matrix.Rotation(math.radians(90), 3, 'X'))  # poles on Y
    bm.to_mesh(mesh)
    bm.free()
    for material in (_material('M7_Sclera', (0.92, 0.92, 0.9, 1)), _material('M7_Iris', (0.18, 0.32, 0.45, 1)),
                     _material('M7_Pupil', (0.02, 0.02, 0.02, 1))):
        mesh.materials.append(material)
    for poly in mesh.polygons:
        forward = -poly.normal.y
        poly.material_index = 2 if forward > math.cos(math.radians(14)) else 1 if forward > math.cos(math.radians(34)) else 0
        poly.use_smooth = True
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = center
    if look_keys:
        obj.shape_key_add(name='Basis', from_mix=False)
        rest = [v.co.copy() for v in mesh.vertices]
        tag = 'Left' if side == 1 else 'Right'
        # In = toward the nose. The left eye sits at +X, so its "in" is -X (rotation about +Z by -angle).
        # Rotation about +X by +a turns the front pole (-Y) downward, so looking up is -a (measured).
        rotations = {'look_up': ('X', -1), 'look_down': ('X', 1), 'look_in': ('Z', -side), 'look_out': ('Z', side)}
        for key, (axis, sign) in rotations.items():
            block = obj.shape_key_add(name=look_keys[key] + tag, from_mix=False)
            matrix = Matrix.Rotation(math.radians(EYE_LOOK_DEG) * sign, 3, axis)
            for v, co in zip(block.data, rest):
                v.co = matrix @ co
            block.value = 0.0
    return obj


def make_neck(center, collection, collision=False):
    import bmesh
    import bpy
    from mathutils import Matrix, Vector
    mesh = bpy.data.meshes.new('M7_NeckMesh')
    bm = bmesh.new()
    # Starts just below the head (no intersection at rest) and behind the chin, so a clean jaw opening never
    # reaches it; the collision defect lowers the hinge and over-rotates the jaw into it.
    top = center[2] - 0.112
    bottom = center[2] - 0.20
    bmesh.ops.create_cone(bm, cap_ends=True, segments=32, radius1=0.030, radius2=0.030, depth=top - bottom)
    bmesh.ops.translate(bm, verts=bm.verts, vec=(center[0], center[1] + 0.045, (top + bottom) / 2))
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(_material('M7_Skin', (0.78, 0.62, 0.55, 1)))
    obj = bpy.data.objects.new('M7_Neck', mesh)
    collection.objects.link(obj)
    return obj


def make_mouth_interior(center, collection):
    import bmesh
    import bpy
    from mathutils import Vector
    mesh = bpy.data.meshes.new('M7_MouthInteriorMesh')
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=12, radius=1.0)
    bmesh.ops.scale(bm, vec=(0.024, 0.016, 0.011), verts=bm.verts)
    surface = head_point(center, 0.0, MOUTH_W)[0]
    bmesh.ops.translate(bm, verts=bm.verts, vec=(surface.x, surface.y + 0.034, surface.z - 0.002))
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(_material('M7_MouthInterior', (0.25, 0.05, 0.06, 1)))
    obj = bpy.data.objects.new('M7_MouthInterior', mesh)
    collection.objects.link(obj)
    return obj


def ensure_rig(center, collection, existing=None, head_bone='head'):
    """Return (rig, head_bone_name). Creates a small body chain unless an existing rig is given."""
    import bpy
    if existing is not None:
        return existing, head_bone
    data = bpy.data.armatures.new('M7_RigData')
    rig = bpy.data.objects.new('M7_FaceRig', data)
    collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    rows = [('root', (0, 0, 0), (0, 0, 0.15), None), ('spine', (0, 0, 1.0), (0, 0, 1.30), 'root'),
            ('chest', (0, 0, 1.30), (0, 0, center[2] - 0.20), 'spine'),
            ('neck', (0, 0, center[2] - 0.20), (0, 0, center[2] - 0.10), 'chest'),
            ('head', (0, 0, center[2] - 0.10), (0, 0, center[2] + 0.12), 'neck')]
    for name, head, tail, parent in rows:
        bone = data.edit_bones.new(name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    return rig, head_bone


def add_face_bones(rig, head_bone, center, eyes, hinge, options):
    """Deform DEF-* bones with CTRL-* controls (Copy Transforms) and Limit constraints."""
    import bpy
    from mathutils import Vector
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    eb = rig.data.edit_bones
    head = eb[head_bone]
    rows = []
    hybrid = options.get('system') == 'hybrid'
    rows.append(('jaw', hinge, hinge + Vector((0, -0.06, -0.02))))
    for side, tag in ((1, 'L'), (-1, 'R')):
        e = eyes[side]
        rows.append(('eye.' + tag, e, e + Vector((0, -0.02, 0))))
        if not hybrid:
            rows.append(('lid_up.' + tag, e, e + Vector((0, -0.015, 0.004))))
            rows.append(('lid_low.' + tag, e, e + Vector((0, -0.015, -0.004))))
            for part, u in (('brow_in', 0.15), ('brow_out', 0.48)):
                p = head_point(center, u * side, 0.33)[0]
                rows.append((part + '.' + tag, p, p + Vector((0, -0.01, 0))))
            p = head_point(center, MOUTH_OPENING[0] * side, MOUTH_W)[0]
            rows.append(('lip_corner.' + tag, p, p + Vector((0, -0.01, 0))))
            p = head_point(center, 0.40 * side, -0.10)[0]
            rows.append(('cheek.' + tag, p, p + Vector((0, -0.01, 0))))
    if not hybrid:
        for part, w in (('lip_up', MOUTH_W + 0.03), ('lip_low', MOUTH_W - 0.03)):
            p = head_point(center, 0.0, w)[0]
            rows.append((part, p, p + Vector((0, -0.01, 0))))
    created = []
    for name, h, t in rows:
        for prefix, deform in (('DEF-', True), ('CTRL-', False)):
            if name.startswith('eye.') and options.get('eye_target') and prefix == 'CTRL-':
                continue
            bone = eb.new(prefix + name)
            bone.head, bone.tail = h, t
            bone.parent = head if not name.startswith(('lip_low',)) else head
            bone.use_deform = deform
            created.append(prefix + name)
    if options.get('eye_target'):
        target = eb.new('CTRL-eye_target')
        mid = (eyes[1] + eyes[-1]) / 2
        target.head, target.tail = mid + Vector((0, -0.40, 0)), mid + Vector((0, -0.42, 0))
        target.parent = head
        target.use_deform = False
        for side, tag in ((1, 'L'), (-1, 'R')):
            child = eb.new('CTRL-eye_target.' + tag)
            child.head = eyes[side] + Vector((0, -0.40, 0)); child.tail = child.head + Vector((0, -0.02, 0))
            child.parent = target
            child.use_deform = False
    bpy.ops.object.mode_set(mode='OBJECT')
    for pb in rig.pose.bones:
        pb.rotation_mode = 'XYZ'
    limits = {
        'jaw': ('ROT', {'x': (0.0, math.radians(18))}),  # same opening as the jawOpen shape key (18 deg)
        'eye': ('ROT', {'x': (-math.radians(25), math.radians(25)), 'z': (-math.radians(32), math.radians(32))}),
        'lid_up': ('ROT', {'x': (-math.radians(8), math.radians(55))}),
        'lid_low': ('ROT', {'x': (-math.radians(20), math.radians(5))}),
        'brow_in': ('LOC', {'z': (-0.006, 0.008)}), 'brow_out': ('LOC', {'z': (-0.005, 0.008)}),
        'lip_corner': ('LOC', {'x': (-0.004, 0.006), 'z': (-0.006, 0.007)}),
        'cheek': ('LOC', {'z': (0.0, 0.005)}), 'lip_up': ('LOC', {'z': (-0.004, 0.005)}),
        'lip_low': ('LOC', {'z': (-0.005, 0.006)}),
    }
    for name in [n for n in created if n.startswith('CTRL-')]:
        base = name[5:].split('.')[0]
        if base not in limits:
            continue
        kind, axes = limits[base]
        pb = rig.pose.bones[name]
        c = pb.constraints.new('LIMIT_ROTATION' if kind == 'ROT' else 'LIMIT_LOCATION')
        c.owner_space = 'LOCAL'
        for axis, (low, high) in axes.items():
            if options.get('asymmetric_limits') and name.endswith('.R') and base == 'brow_out':
                high = high * 0.5
            if kind == 'ROT':
                setattr(c, 'use_limit_' + axis, True); setattr(c, 'min_' + axis, low); setattr(c, 'max_' + axis, high)
            else:
                setattr(c, 'use_min_' + axis, True); setattr(c, 'use_max_' + axis, True)
                setattr(c, 'min_' + axis, low); setattr(c, 'max_' + axis, high)
        if kind == 'LOC':
            c.use_transform_limit = True
    for name in [n for n in created if n.startswith('DEF-')]:
        pb = rig.pose.bones[name]
        control = 'CTRL-' + name[4:]
        if control in rig.pose.bones:
            c = pb.constraints.new('COPY_TRANSFORMS')
            c.target, c.subtarget = rig, control
            c.target_space = c.owner_space = 'LOCAL'
        elif name.startswith('DEF-eye.') and options.get('eye_target'):
            c = pb.constraints.new('DAMPED_TRACK')
            c.target, c.subtarget = rig, 'CTRL-eye_target.' + name.split('.')[-1]
            c.track_axis = 'TRACK_Y'
    return created


def skin_face(face, rig, head_bone, uvw, options):
    weights = region_weights(uvw)
    hybrid = options.get('system') == 'hybrid'
    groups = {}

    def group(name):
        if name not in groups:
            groups[name] = face.vertex_groups.new(name=name)
        return groups[name]
    mapping = {'jaw': 'DEF-jaw'}
    if not hybrid:
        mapping.update({'lid_up_L': 'DEF-lid_up.L', 'lid_up_R': 'DEF-lid_up.R', 'lid_low_L': 'DEF-lid_low.L',
                        'lid_low_R': 'DEF-lid_low.R', 'brow_in_L': 'DEF-brow_in.L', 'brow_in_R': 'DEF-brow_in.R',
                        'brow_out_L': 'DEF-brow_out.L', 'brow_out_R': 'DEF-brow_out.R', 'corner_L': 'DEF-lip_corner.L',
                        'corner_R': 'DEF-lip_corner.R', 'cheek_L': 'DEF-cheek.L', 'cheek_R': 'DEF-cheek.R',
                        'lip_up': 'DEF-lip_up', 'lip_low': 'DEF-lip_low'})
    for k in range(len(uvw)):
        row = weights[k]
        assigned = {}
        for region, bone in mapping.items():
            value = row[region]
            if region == 'lip_low':
                value = value * (1 - row['jaw'])
            if value > 0.02:
                assigned[bone] = value
        total = sum(assigned.values())
        if total > 1:
            assigned = {b: v / total for b, v in assigned.items()}
            total = 1.0
        for bone, value in assigned.items():
            group(bone).add([k], value, 'REPLACE')
        if total < 1:
            group(head_bone).add([k], 1 - total, 'REPLACE')
    modifier = face.modifiers.new('Armature', 'ARMATURE')
    modifier.object = rig


def obfuscate(face):
    for index, block in enumerate(face.data.shape_keys.key_blocks[1:], start=1):
        block.name = 'Key ' + str(index)


def build(folder, variant='shape_keys', save=True, existing_rig=None, head_bone='head', center=None):
    """Build one facial fixture in the current scene (fresh factory scene unless a rig is given)."""
    import bpy
    from mathutils import Vector
    spec = dict(VARIANTS[variant])
    if existing_rig is None:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.context.scene.render.fps = 30
    scene = bpy.context.scene
    collection = scene.collection
    if center is None:
        if existing_rig is not None:
            head = existing_rig.data.bones[head_bone]
            center = tuple(existing_rig.matrix_world @ head.head_local + Vector((0, 0, 0.10)))
        else:
            center = (0.0, 0.0, 1.65)
    options = dict(spec)
    if spec.get('intentional_asymmetry'):
        options['eye_scale'] = {1: 0.86}
    verts, faces, uvw = build_head(center, options)
    mesh = bpy.data.meshes.new('M7_FaceMesh')
    mesh.from_pydata([tuple(v) for v in verts], [], faces)
    mesh.update()
    for poly in mesh.polygons:
        poly.use_smooth = True
    mesh.materials.append(_material('M7_Skin', (0.78, 0.62, 0.55, 1)))
    face = bpy.data.objects.new('M7_Face', mesh)
    collection.objects.link(face)
    neck = make_neck(center, collection)
    interior = make_mouth_interior(center, collection)
    system = spec['system']
    shapes, eyes, hinge = shape_deltas(verts, uvw, center, options)
    eye_scale = options.get('eye_scale', {})
    look = EYE_KEYS if system == 'shape_keys' else None
    eye_objects = {side: make_eye('M7_Eye.' + tag, eyes[side], side, eye_scale.get(side, 1.0), look, collection)
                   for side, tag in ((1, 'L'), (-1, 'R'))}
    rig, head_name = ensure_rig(center, collection, existing_rig, head_bone)
    names = {}
    if system in ('shape_keys', 'hybrid', 'ambiguous'):
        face.shape_key_add(name='Basis', from_mix=False)
        if system == 'hybrid':
            table = {k: v for k, v in CUSTOM.items() if not k.startswith('jaw')}
            table['jaw_open'] = 'Jaw_Open_Shape'  # redundant with the jaw bone on purpose
        elif system == 'ambiguous':
            table = {}
        else:
            table = ARKIT if spec.get('names', 'arkit') == 'arkit' else CUSTOM
        if spec.get('subset'):
            table = {k: v for k, v in table.items() if k.startswith(spec['subset'])}
        for semantic, name in table.items():
            block = face.shape_key_add(name=name, from_mix=False)
            for v, co in zip(block.data, shapes[semantic]):
                v.co = co
            block.slider_min, block.slider_max = 0.0, 1.0
            block.value = 0.0  # Blender 5.2.1 creates Python shape keys at value 1.0 (measured)
            names[semantic] = name
        if system == 'ambiguous':
            combos = [('blink_L', 'smile_R'), ('brow_outer_up_L', 'frown_R'), ('cheek_L', 'pucker')]  # far areas, similar energy
            for index, (a, b) in enumerate(combos, start=1):
                block = face.shape_key_add(name='Key ' + str(index), from_mix=False)
                for v, base, p, q in zip(block.data, verts, shapes[a], shapes[b]):
                    v.co = base + (p - base) + (q - base)
                block.value = 0.0
        if spec.get('names') == 'obfuscated':
            obfuscate(face)
        if system == 'hybrid':
            block = face.shape_key_add(name='Jaw_Corrective', from_mix=False)
            for v, co, base in zip(block.data, shapes['press_L'], verts):
                v.co = base + (co - base) * 0.3
            block.value = 0.0
    # matrix_world is only valid after a depsgraph update; with an existing rig no operator has run yet
    # (measured: eyes re-parented with an identity matrix ended at the world origin).
    bpy.context.view_layer.update()
    face.parent = rig
    face.parent_type = 'OBJECT'
    neck.parent = interior.parent = rig
    if system in ('bones', 'hybrid'):
        add_face_bones(rig, head_name, center, eyes, hinge, options)
        skin_face(face, rig, head_name, uvw, options)
        for side, tag in ((1, 'L'), (-1, 'R')):
            eye = eye_objects[side]
            matrix = eye.matrix_world.copy()
            eye.parent = rig
            eye.parent_type = 'BONE'
            eye.parent_bone = 'DEF-eye.' + tag
            eye.matrix_world = matrix
        matrix = interior.matrix_world.copy()
        interior.parent_type = 'BONE'; interior.parent_bone = 'DEF-jaw'
        interior.matrix_world = matrix
    else:
        # Eyes and mouth interior follow the head bone, so a body Action that moves the head carries them along.
        for item in list(eye_objects.values()) + [interior]:
            matrix = item.matrix_world.copy()
            item.parent = rig
            item.parent_type = 'BONE'
            item.parent_bone = head_name
            item.matrix_world = matrix
    neck_group = neck.vertex_groups.new(name='neck')
    neck_group.add(list(range(len(neck.data.vertices))), 1.0, 'REPLACE')
    neck_mod = neck.modifiers.new('Armature', 'ARMATURE'); neck_mod.object = rig
    if system in ('shape_keys', 'ambiguous', 'none'):
        group = face.vertex_groups.new(name=head_name)
        group.add(list(range(len(face.data.vertices))), 1.0, 'REPLACE')
        modifier = face.modifiers.new('Armature', 'ARMATURE'); modifier.object = rig
    if system == 'hybrid':
        fc = face.data.shape_keys.key_blocks['Jaw_Corrective'].driver_add('value')
        driver = fc.driver
        driver.type = 'SCRIPTED'
        variable = driver.variables.new()
        variable.name = 'jaw'
        variable.type = 'TRANSFORMS'
        variable.targets[0].id = rig
        variable.targets[0].bone_target = 'CTRL-jaw'
        variable.targets[0].transform_type = 'ROT_X'
        variable.targets[0].transform_space = 'LOCAL_SPACE'
        driver.expression = 'jaw * 2.6'
    if spec.get('driver_conflict'):
        fc = face.data.shape_keys.key_blocks[ARKIT['smile_L']].driver_add('value')
        fc.driver.type = 'AVERAGE'
        variable = fc.driver.variables.new()
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id_type = 'OBJECT'
        variable.targets[0].id = rig
        variable.targets[0].data_path = 'location.x'
    face['m7_fixture'] = variant
    manifest = {'variant': variant, 'system': system, 'face': face.name, 'rig': rig.name, 'neck': neck.name,
                'eyes': {tag: eye_objects[side].name for side, tag in ((1, 'L'), (-1, 'R'))},
                'mouth_interior': interior.name, 'center': list(center),
                'eye_centers': {tag: list(eyes[side]) for side, tag in ((1, 'L'), (-1, 'R'))},
                'jaw_hinge': list(hinge), 'shape_keys': names, 'defects': spec.get('defects', []),
                'vertices': len(face.data.vertices), 'triangles': sum(len(p.vertices) - 2 for p in face.data.polygons),
                'expected': expected(variant)}
    if save:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (variant + '.blend')
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
        manifest['path'] = str(path)
        (folder / (variant + '.json')).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def expected(variant):
    """Fixture specification used ONLY by tests to judge production results (never read by production)."""
    system = VARIANTS[variant]['system']
    return {'classification': {'shape_keys': 'SHAPE_KEYS', 'bones': 'BONES', 'hybrid': 'HYBRID', 'ambiguous': 'AMBIGUOUS',
                               'none': 'UNSUPPORTED'}[system]}


# ------------------------------------------------------------------ timed speech and mocap fixtures (pure)

def viseme_phrase():
    """Controlled ARPAbet timeline for "Bob made a map" (seconds), authored by hand, not recognised."""
    rows = [('SIL', 0.00, 0.20), ('B', 0.20, 0.28), ('AA', 0.28, 0.42), ('B', 0.42, 0.50), ('SIL', 0.50, 0.58),
            ('M', 0.58, 0.68), ('EY', 0.68, 0.84), ('D', 0.84, 0.92), ('AH', 0.92, 1.02), ('M', 1.02, 1.12),
            ('AE', 1.12, 1.28), ('P', 1.28, 1.38), ('SIL', 1.38, 1.80)]
    words = [{'word': 'Bob', 'start': 0.20, 'end': 0.50, 'stress': 0.6}, {'word': 'made', 'start': 0.58, 'end': 0.92, 'stress': 0.4},
             {'word': 'a', 'start': 0.92, 'end': 1.02, 'stress': 0.1}, {'word': 'map', 'start': 1.02, 'end': 1.38, 'stress': 0.9}]
    return {'schema': 'production.timed_phonemes.v1', 'units': 'seconds', 'alphabet': 'ARPABET',
            'phonemes': [{'phoneme': p, 'start': s, 'end': e} for p, s, e in rows], 'words': words,
            'source': 'controlled fixture authored by hand; no speech recognition'}


MOCAP_CHANNELS = ['eyeBlinkLeft', 'eyeBlinkRight', 'eyeLookUpLeft', 'eyeLookUpRight', 'eyeLookDownLeft', 'eyeLookDownRight',
                  'eyeLookInLeft', 'eyeLookInRight', 'eyeLookOutLeft', 'eyeLookOutRight', 'eyeSquintLeft', 'eyeSquintRight',
                  'eyeWideLeft', 'eyeWideRight', 'browDownLeft', 'browDownRight', 'browInnerUp', 'browOuterUpLeft',
                  'browOuterUpRight', 'jawOpen', 'mouthClose', 'mouthPucker', 'mouthFunnel', 'mouthSmileLeft',
                  'mouthSmileRight', 'mouthFrownLeft', 'mouthFrownRight', 'mouthPressLeft', 'mouthPressRight',
                  'cheekSquintLeft', 'cheekSquintRight', 'noseSneerLeft', 'noseSneerRight']


def synthetic_mocap(fps=30, seconds=5.0, noise=0.0, chatter=0.0, eye_jitter=0.0, seed=7, naming='arkit', drop=()):
    """Smooth synthetic facial capture with blinks, speech-like jaw, brows and gaze; optional seeded noise."""
    rng = random.Random(seed)
    frames = int(round(seconds * fps)) + 1
    rows = []
    blinks = [0.9, 2.6, 4.1]
    for i in range(frames):
        t = i / fps
        values = {name: 0.0 for name in MOCAP_CHANNELS}
        blink = max((max(0.0, 1 - abs(t - b) / 0.075) for b in blinks), default=0.0)
        values['eyeBlinkLeft'] = values['eyeBlinkRight'] = blink
        speech = 0.5 * max(0.0, math.sin(2 * math.pi * 3.2 * t)) * (1.0 if 1.0 < t < 3.6 else 0.0)
        values['jawOpen'] = speech
        values['mouthClose'] = 0.0
        values['mouthFunnel'] = 0.25 * max(0.0, math.sin(2 * math.pi * 1.1 * t)) * (1.0 if 1.0 < t < 3.6 else 0.0)
        values['browInnerUp'] = 0.35 * smoothstep(0.5, 1.5, t) * (1 - smoothstep(3.5, 4.5, t))
        values['mouthSmileLeft'] = values['mouthSmileRight'] = 0.2 * smoothstep(3.8, 4.6, t)
        gaze = 0.4 if 2.0 < t < 3.0 else 0.0
        values['eyeLookOutLeft'] = values['eyeLookInRight'] = gaze
        for name in values:
            if noise:
                values[name] += rng.gauss(0, noise)
        if chatter:
            values['jawOpen'] += chatter * math.sin(2 * math.pi * 11.0 * t)
        if eye_jitter:
            for name in ('eyeLookOutLeft', 'eyeLookInRight'):
                values[name] += rng.gauss(0, eye_jitter)
        for name in values:
            values[name] = min(1.0, max(0.0, values[name]))
        rows.append(values)
    names = [n for n in MOCAP_CHANNELS if n not in drop]
    if naming == 'snake':
        rename = {n: ''.join('_' + c.lower() if c.isupper() else c for c in n) for n in names}
    elif naming == 'unknown':
        rename = {n: 'chan_' + str(i).zfill(2) for i, n in enumerate(names)}
    else:
        rename = {n: n for n in names}
    return {'schema': 'production.facial_mocap.v1', 'fps': fps, 'frame_start': 1, 'units': 'normalized_0_1',
            'source': 'synthetic fixture (seed ' + str(seed) + ')',
            'channels': {rename[n]: [round(r[n], 6) for r in rows] for n in names}}
