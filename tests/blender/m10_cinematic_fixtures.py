"""Synthetic M10 cinematic demo fixtures: environment, creature, humanoids, face, secondary piece and props.

No production assets, no external files and no production lights (lighting belongs to M10). Every builder is
deterministic: same arguments, same geometry, same keys. Conventions shared by every demo:

    units      metres, scene unit scale 1, every object scale exactly 1 (M8 refuses non-unit scales, and a baked
               creature size keeps root motion in real metres)
    axes       actors face -Y (towards a camera placed at -Y), +Z up, character left is +X
    ground     world z = 0; rigs are lowered by the M6 foot height so the proxy feet touch it
    fps        30 by default; every Action is keyed from frame 1 with its own length

The pure helpers (gait, turn, Action specs, layouts, rock jitter, prop geometry) need no Blender and are unit-tested
in tests/unit/test_m10_fixtures.py. Everything that imports bpy runs only inside a headless factory-startup process
(tests/blender/m10_fixtures_probe.py); ``build_demo`` refuses to replace any open file that is not one of its own
previous demos, so it can never clear a user's scene.

Reused without editing: fixtures_m6 (bone rows, armature, IK, proxy skin), m7_facial_fixtures (face on an existing
rig) and fixtures_m8 (closed boxes and mesh helper). Their traps are handled here: m7.build expects the rig at the
world origin (face and neck are parented without compensation), shape keys are born at 1.0 in Blender 5.2.1, and
fixtures_m6 has no offset parameter.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SCHEMA = 'm10.demo_fixture.v1'
FPS = 30
GROUND_Z = 0.0
M6_FOOT_Z = 0.08  # fixtures_m6.GROUND_HEIGHT_M: rest height of the M6 ankle/foot bones
DEMO_KINDS = ('A_creature_reveal', 'B_two_character', 'C_body_face_physics', 'D_editorial', 'E_interaction')
KEYED_BONES = ('root', 'pelvis', 'spine', 'chest', 'head', 'upper_arm.L', 'upper_arm.R', 'forearm.L', 'forearm.R',
               'hand.L', 'hand.R', 'foot_ik.L', 'foot_ik.R')
BONES = {'root': 'root', 'pelvis': 'pelvis', 'spine': 'spine', 'chest': 'chest', 'head': 'head',
         'hands': {'L': 'hand.L', 'R': 'hand.R'}, 'feet': {'L': 'foot.L', 'R': 'foot.R'},
         'foot_controls': {'L': 'foot_ik.L', 'R': 'foot_ik.R'}, 'poles': {'L': 'pole.L', 'R': 'pole.R'},
         'upper_arms': {'L': 'upper_arm.L', 'R': 'upper_arm.R'}, 'forearms': {'L': 'forearm.L', 'R': 'forearm.R'},
         'thighs': {'L': 'thigh.L', 'R': 'thigh.R'}, 'shins': {'L': 'shin.L', 'R': 'shin.R'}}
CREATURE_SCALE_RANGE = (1.0, 3.0)
TURN_RANGE_DEG = (90.0, 180.0)
LIMITATIONS = [
    'Segmented rigid-skin proxies (fixtures_m6); not realistic deformation or production characters',
    'Gaits are authored key poses with two-bone IK feet, not simulated locomotion; planted-foot reach is checked from '
    'the rest geometry and the IK error is measured by the probe',
    'Stalk is one gait cycle whose start and end poses match relative to the root; repeating it is an M10 decision',
    'No production lights: the lantern is an emissive prop marked as a practical anchor; M10 adds the lights',
    'Durations other than A (15 s, task requirement) are proposals for the demos, not user requirements',
]


def _sibling(name):
    """Load a sibling fixture module by path, so this file works from the probe (tests/blender on sys.path) and from
    pytest (loaded with importlib) without editing the reused fixture modules."""
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def m6():
    return _sibling('fixtures_m6')


def m7():
    return _sibling('m7_facial_fixtures')


def m8():
    return _sibling('fixtures_m8')


# ------------------------------------------------------------------ pure helpers (no Blender)

def _finite(value, field, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('M10_FIXTURE_NUMBER_INVALID: ' + field)
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError('M10_FIXTURE_RANGE_INVALID: ' + field + ' in [' + str(minimum) + ', ' + str(maximum) + ']')
    return float(value)


def frame_range(duration_s, fps=FPS, frame_start=1):
    """Scene range for a duration, with the same rounding as cinematic.planning.timeline_for (half up), so a brief
    with this duration and fps lands exactly on the fixture's last frame."""
    duration_s = _finite(duration_s, 'duration_s', 0.1, 120.0)
    end_frame = frame_start + int(math.floor(duration_s * fps + 0.5))
    return [frame_start, end_frame - 1]


def rest_heads(variant='humanoid', scale=1.0):
    return {row[0]: tuple(c * scale for c in row[1]) for row in m6().bone_definitions(variant)}


def leg_geometry(variant='humanoid', scale=1.0):
    """Hip height above the ankle and full leg length from the M6 rest rows (the IK chain thigh+shin)."""
    rows = {row[0]: row for row in m6().bone_definitions(variant)}
    thigh, shin = rows['thigh.L'], rows['shin.L']
    length = math.dist(thigh[1], thigh[2]) + math.dist(shin[1], shin[2])
    return {'hip_height_m': (thigh[1][2] - shin[2][2]) * scale, 'leg_length_m': length * scale}


def gait(distance_m, steps, step_frames, lift_m, crouch_m, bob_m, lead='L', start=1):
    """Root motion walk on -Y with alternating IK steps.

    The root advances linearly (constant speed). Both feet start together at 0 and end together at the full
    distance; each swing lands where the root will be in the middle of that foot's next stance, so a planted foot
    never trails the hips by more than one step of root travel. Keys at contacts and passings (half steps); the
    pelvis is lowest at contacts. Returned forward distances are positive metres along -Y."""
    distance_m = _finite(distance_m, 'distance_m', 0.01, 50.0)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 2 <= steps <= 40:
        raise ValueError('M10_FIXTURE_RANGE_INVALID: steps in [2, 40]')
    if isinstance(step_frames, bool) or not isinstance(step_frames, int) or step_frames < 4 or step_frames % 2:
        raise ValueError('M10_FIXTURE_RANGE_INVALID: step_frames must be an even integer >= 4')
    if lead not in ('L', 'R'):
        raise ValueError('M10_FIXTURE_CHOICE_INVALID: lead must be L or R')
    total = steps * step_frames
    half = step_frames // 2

    def root_at(t):
        return distance_m * t / total

    lands = [distance_m if k >= steps - 2 else root_at((k + 1.5) * step_frames) for k in range(steps)]
    other = 'R' if lead == 'L' else 'L'
    feet = {'L': 0.0, 'R': 0.0}
    rows = [{'frame': start, 't': 0, 'root': 0.0, 'feet': dict(feet), 'lift': {}, 'swing': None, 'phase': 'CONTACT',
             'pelvis_z': -(crouch_m + bob_m)}]
    for k in range(steps):
        foot = lead if k % 2 == 0 else other
        middle = dict(feet)
        middle[foot] = (feet[foot] + lands[k]) / 2.0
        t = k * step_frames + half
        rows.append({'frame': start + t, 't': t, 'root': root_at(t), 'feet': middle, 'lift': {foot: lift_m},
                     'swing': foot, 'phase': 'PASSING', 'pelvis_z': -(crouch_m - bob_m)})
        feet[foot] = lands[k]
        t = (k + 1) * step_frames
        rows.append({'frame': start + t, 't': t, 'root': root_at(t), 'feet': dict(feet), 'lift': {}, 'swing': None,
                     'phase': 'CONTACT', 'pelvis_z': -(crouch_m + bob_m)})
    return rows


def planted_reach(rows, step_frames, crouch_m, bob_m, variant='humanoid', scale=1.0):
    """Worst planted-foot reach of a gait, frame by frame. The hip is taken at its HIGHEST key (conservative) and the
    horizontal offset is exact (linear root, constant planted foot). margin < 0 means the IK would over-extend."""
    legs = leg_geometry(variant, scale)
    total = rows[-1]['t']
    distance = rows[-1]['root']
    hip = legs['hip_height_m'] - (crouch_m - bob_m)
    worst = 0.0
    contacts = [r for r in rows if r['phase'] == 'CONTACT']
    for t in range(total + 1):
        k = min(t // step_frames, len(contacts) - 2)
        swinging = None if t % step_frames == 0 else rows[2 * k + 1]['swing']
        planted = contacts[k]['feet'] if t < contacts[k + 1]['t'] else contacts[k + 1]['feet']
        root = distance * t / total
        for foot, position in planted.items():
            if foot != swinging:
                worst = max(worst, abs(root - position))
    reach = math.sqrt(hip * hip + worst * worst)
    return {'max_planted_offset_m': worst, 'hip_height_m': hip, 'leg_length_m': legs['leg_length_m'],
            'reach_m': reach, 'reach_margin_m': legs['leg_length_m'] - reach}


def turn(angle_deg, step_frames, start=1):
    """In-place turn about +Z (positive = towards +X, the character's left when facing -Y) in two steps: the foot on
    the turn side steps first, the other follows; the head leads the body (anticipation) and settles."""
    angle_deg = _finite(angle_deg, 'turn_deg', -TURN_RANGE_DEG[1], TURN_RANGE_DEG[1])
    if abs(angle_deg) < TURN_RANGE_DEG[0]:
        raise ValueError('M10_FIXTURE_RANGE_INVALID: |turn_deg| in [90, 180]')
    if isinstance(step_frames, bool) or not isinstance(step_frames, int) or step_frames < 4 or step_frames % 2:
        raise ValueError('M10_FIXTURE_RANGE_INVALID: step_frames must be an even integer >= 4')
    lead = 'L' if angle_deg > 0 else 'R'
    trail = 'R' if lead == 'L' else 'L'
    a = angle_deg
    half = step_frames // 2
    table = [(0, 0.0, 0.0, None, 0.0, 0.0), (half, a / 2, 0.0, lead, .25 * a, -.01),
             (step_frames, a, 0.0, None, .15 * a, -.02), (step_frames + half, a, a / 2, trail, .05 * a, -.01),
             (2 * step_frames, a, a, None, 0.0, 0.0)]
    rows = []
    for t, lead_yaw, trail_yaw, swing, head, pelvis in table:
        rows.append({'frame': start + t, 't': t, 'root_yaw_deg': a * t / (2 * step_frames),
                     'feet_yaw_deg': {lead: lead_yaw, trail: trail_yaw}, 'swing': swing, 'head_yaw_deg': head,
                     'pelvis_z': pelvis})
    return rows


def _rot(*pairs):
    return [[axis, float(deg)] for axis, deg in pairs if deg]


def _abduct(side, deg):
    # Raising an arm outwards: about +Y the left arm (+X side) needs a negative angle, the right arm a positive one.
    return ('Y', -deg if side == 'L' else deg)


def _pose(**bones):
    return {name.replace('__', '.'): value for name, value in bones.items()}


def _merge(*poses):
    result = {}
    for pose in poses:
        for bone, row in pose.items():
            target = result.setdefault(bone, {})
            if 't' in row:
                previous = target.get('t', [0.0, 0.0, 0.0])
                target['t'] = [a + b for a, b in zip(previous, row['t'])]
            if 'r' in row:
                target['r'] = list(target.get('r', [])) + [list(r) for r in row['r']]
    return result


def _gait_keys(rows, posture, swing_deg, head_sway_deg=0.0):
    """Key poses from gait rows: root/feet translations in armature space, plus arm swing opposite to the legs."""
    keys = []
    for index, row in enumerate(rows):
        pose = {'root': {'t': [0.0, -row['root'], 0.0]}, 'pelvis': {'t': [0.0, 0.0, row['pelvis_z']]}}
        for side in ('L', 'R'):
            # foot_ik bones have no parent: their head = rest head + translation, all in armature space.
            pose['foot_ik.' + side] = {'t': [0.0, -row['feet'][side], row['lift'].get(side, 0.0)]}
        swing = row['swing']
        if swing:
            # The arm opposite to the swinging leg goes forward (negative X rotation swings a hanging arm to -Y).
            forward = 'R' if swing == 'L' else 'L'
            pose['upper_arm.' + forward] = {'r': _rot(('X', -swing_deg))}
            pose['upper_arm.' + swing] = {'r': _rot(('X', swing_deg))}
            pose['chest'] = {'r': _rot(('Z', (4.0 if swing == 'L' else -4.0)))}
        if head_sway_deg:
            pose['head'] = {'r': _rot(('Z', head_sway_deg * math.sin(index * math.pi / 3.0)))}
        keys.append([row['frame'], _merge(posture, pose)])
    return keys


def humanoid_action_specs(name, turn_deg=90.0, reach_side='R', lower=False):
    """Idle, Turn, React, Walk and Reach of a humanoid (scale 1), each with its own range and distinct motion; Lower
    (the return of the reach) only when asked."""
    if reach_side not in ('L', 'R'):
        raise ValueError('M10_FIXTURE_CHOICE_INVALID: reach_side must be L or R')
    specs = []
    breathe = [(1, 0.0, 0.0, 0.0), (16, 1.5, 3.0, -.005), (31, -1.0, -2.0, 0.0), (46, 1.5, 1.0, -.005), (61, 0.0, 0.0, 0.0)]
    specs.append({'name': name + '_Idle', 'purpose': 'IDLE', 'frame_range': [1, 61], 'root_motion_m': 0.0,
                  'keys': [[f, _pose(chest={'r': _rot(('X', c))}, head={'r': _rot(('Z', h))}, pelvis={'t': [0, 0, p]},
                                     upper_arm__L={'r': _rot(_abduct('L', 3 if c > 0 else 0))},
                                     upper_arm__R={'r': _rot(_abduct('R', 3 if c > 0 else 0))})]
                           for f, c, h, p in breathe],
                  'linear_bones': [], 'measure': 'STATIC_ROOT'})
    step = 18
    rows = turn(turn_deg, step)
    heads = rest_heads('humanoid')
    keys = []
    for row in rows:
        pose = {'root': {'r': _rot(('Z', row['root_yaw_deg']))}, 'pelvis': {'t': [0.0, 0.0, row['pelvis_z']]},
                'head': {'r': _rot(('Z', row['head_yaw_deg']))}, 'chest': {'r': _rot(('Z', .4 * row['head_yaw_deg']))}}
        for side in ('L', 'R'):
            yaw = math.radians(row['feet_yaw_deg'][side])
            x, y, z = heads['foot_ik.' + side]
            # Rotate the rest foot about the root's vertical axis (x = y = 0) and turn the control with it.
            moved = (x * math.cos(yaw) - y * math.sin(yaw), x * math.sin(yaw) + y * math.cos(yaw))
            lift = .06 if row['swing'] == side else 0.0
            pose['foot_ik.' + side] = {'t': [moved[0] - x, moved[1] - y, lift], 'r': _rot(('Z', row['feet_yaw_deg'][side]))}
        keys.append([row['frame'], pose])
    specs.append({'name': name + '_Turn', 'purpose': 'TURN', 'frame_range': [1, 1 + 2 * step], 'root_motion_m': 0.0,
                  'yaw_deg': float(turn_deg), 'keys': keys, 'linear_bones': ['root'], 'measure': 'ROOT_YAW'})
    def flinch(arm, chest, head, pelvis, fore):
        # Startle: chest and head thrown back (negative X), arms up and out, knees give (pelvis down).
        return _pose(chest={'r': _rot(('X', chest))}, spine={'r': _rot(('X', chest * .4))}, head={'r': _rot(('X', head))},
                     pelvis={'t': [0, 0, pelvis]}, upper_arm__L={'r': _rot(('X', -arm), _abduct('L', arm * .6))},
                     upper_arm__R={'r': _rot(('X', -arm), _abduct('R', arm * .6))},
                     forearm__L={'r': _rot(('X', -fore))}, forearm__R={'r': _rot(('X', -fore))})
    specs.append({'name': name + '_React', 'purpose': 'STARTLE', 'frame_range': [1, 31], 'root_motion_m': 0.0,
                  'keys': [[1, flinch(0, 0, 0, 0, 0)], [4, flinch(40, -12, -15, -.04, 60)], [8, flinch(55, -14, -18, -.05, 70)],
                           [16, flinch(50, -12, -15, -.045, 65)], [31, flinch(15, -5, -6, -.02, 20)]],
                  'linear_bones': [], 'measure': 'HEAD_BACK'})
    walk = {'distance_m': 2.2, 'steps': 8, 'step_frames': 10, 'lift_m': .08, 'crouch_m': .08, 'bob_m': .012}
    rows = gait(walk['distance_m'], walk['steps'], walk['step_frames'], walk['lift_m'], walk['crouch_m'], walk['bob_m'])
    keys = _gait_keys(rows, _pose(spine={'r': _rot(('X', 3))}), 18.0)
    specs.append({'name': name + '_Walk', 'purpose': 'WALK', 'frame_range': [1, rows[-1]['frame']],
                  'root_motion_m': walk['distance_m'], 'keys': keys, 'linear_bones': ['root'], 'gait': walk,
                  'measure': 'ROOT_MOTION'})
    side = reach_side

    def reach(arm, fore, chest, head):
        # A hanging arm swings forward (-Y) with a negative X rotation; the chest leans into the reach.
        return _pose(chest={'r': _rot(('X', chest))}, spine={'r': _rot(('X', chest * .4))}, head={'r': _rot(('X', head))},
                     **{'upper_arm__' + side: {'r': _rot(('X', -arm), _abduct(side, -5 if arm > 60 else 0))},
                        'forearm__' + side: {'r': _rot(('X', -fore))}})
    specs.append({'name': name + '_Reach', 'purpose': 'REACH', 'frame_range': [1, 41], 'root_motion_m': 0.0,
                  'reach_side': side, 'keys': [[1, reach(0, 0, 0, 0)], [12, reach(30, 20, 4, 3)], [24, reach(80, 5, 10, 8)],
                                               [41, reach(80, 5, 10, 8)]],
                  'linear_bones': [], 'measure': 'HAND_FORWARD'})
    if lower:
        # Starts on Reach's last pose and ends at rest: after a hand-over the empty hand leaves the prop and the
        # holding hand brings it to the body. Without it both arms stayed extended and the hand-over did not read.
        specs.append({'name': name + '_Lower', 'purpose': 'LOWER_ARM', 'frame_range': [1, 33], 'root_motion_m': 0.0,
                      'reach_side': side, 'keys': [[1, reach(80, 5, 10, 8)], [10, reach(62, 12, 8, 6)],
                                                   [22, reach(18, 12, 2, 1)], [33, reach(0, 0, 0, 0)]],
                      'linear_bones': [], 'measure': 'HAND_LOWERED'})
    return specs


def creature_action_specs(name, scale=1.9):
    """Emerge, Stalk, Stare and AttackPrep of the large asymmetric creature; distances scale with the creature."""
    s = _finite(scale, 'scale', *CREATURE_SCALE_RANGE)
    specs = []
    hunch = _pose(spine={'r': _rot(('X', 10))}, chest={'r': _rot(('X', 18))}, head={'r': _rot(('X', -20))},
                  upper_arm__L={'r': _rot(('X', -15))}, upper_arm__R={'r': _rot(('X', -15))},
                  forearm__L={'r': _rot(('X', -20))}, forearm__R={'r': _rot(('X', -20))})
    emerge = {'distance_m': 4.0, 'steps': 10, 'step_frames': 16, 'lift_m': .10 * s, 'crouch_m': .14 * s, 'bob_m': .02 * s}
    rows = gait(emerge['distance_m'], emerge['steps'], emerge['step_frames'], emerge['lift_m'], emerge['crouch_m'],
                emerge['bob_m'])
    specs.append({'name': name + '_Emerge', 'purpose': 'EMERGE_FROM_DARKNESS', 'frame_range': [1, rows[-1]['frame']],
                  'root_motion_m': emerge['distance_m'], 'gait': emerge, 'linear_bones': ['root'], 'measure': 'ROOT_MOTION',
                  'keys': _gait_keys(rows, hunch, 10.0, head_sway_deg=8.0)})
    crouched = _pose(spine={'r': _rot(('X', 14))}, chest={'r': _rot(('X', 28))}, head={'r': _rot(('X', -30))},
                     upper_arm__L={'r': _rot(('X', -35))}, upper_arm__R={'r': _rot(('X', -35))},
                     forearm__L={'r': _rot(('X', -25))}, forearm__R={'r': _rot(('X', -25))})
    stalk = {'distance_m': 1.2, 'steps': 4, 'step_frames': 16, 'lift_m': .06 * s, 'crouch_m': .22 * s, 'bob_m': .015 * s}
    rows = gait(stalk['distance_m'], stalk['steps'], stalk['step_frames'], stalk['lift_m'], stalk['crouch_m'], stalk['bob_m'])
    specs.append({'name': name + '_Stalk', 'purpose': 'STALK_CYCLE', 'frame_range': [1, rows[-1]['frame']],
                  'root_motion_m': stalk['distance_m'], 'gait': stalk, 'linear_bones': ['root'], 'measure': 'ROOT_MOTION',
                  'cyclic_relative_to_root': True, 'keys': _gait_keys(rows, crouched, 6.0)})

    def stare(yaw, pitch, chest):
        # Still body, head turned towards +X (positive Z) and raised (negative X); only the chest breathes.
        return _merge(hunch, _pose(pelvis={'t': [0, 0, -.06 * s]}, head={'r': _rot(('Z', yaw), ('X', pitch))},
                                   chest={'r': _rot(('X', chest))}))
    specs.append({'name': name + '_Stare', 'purpose': 'STARE', 'frame_range': [1, 91], 'root_motion_m': 0.0,
                  'head_yaw_deg': 25.0, 'linear_bones': [], 'measure': 'HEAD_YAW',
                  'keys': [[1, stare(0, 0, 0)], [10, stare(25, -12, 2)], [40, stare(25, -12, -2)], [70, stare(25, -12, 2)],
                           [91, stare(25, -12, 0)]]})
    def attack(arm, abduct, fore, chest, head, pelvis):
        # Crouch first (pelvis down, chest forward), then rear up with both arms raised and the claws hooked.
        return _pose(pelvis={'t': [0, 0, pelvis * s]}, spine={'r': _rot(('X', chest * .5))}, chest={'r': _rot(('X', chest))},
                     head={'r': _rot(('X', head))}, upper_arm__L={'r': _rot(('X', -arm), _abduct('L', abduct))},
                     upper_arm__R={'r': _rot(('X', -arm), _abduct('R', abduct))},
                     forearm__L={'r': _rot(('X', -fore))}, forearm__R={'r': _rot(('X', -fore))})
    specs.append({'name': name + '_AttackPrep', 'purpose': 'ATTACK_PREPARATION', 'frame_range': [1, 46],
                  'root_motion_m': 0.0, 'linear_bones': [], 'measure': 'HANDS_RAISED',
                  'keys': [[1, attack(15, 0, 20, 18, -20, -.02)], [12, attack(25, 10, 30, 30, -20, -.20)],
                           [30, attack(125, 30, 50, -5, -25, -.16)], [46, attack(125, 30, 50, -5, -25, -.16)]]})
    return specs


def rock_jitter(index, seed):
    """Deterministic radial factor in [0.82, 1.18] from integer hashing (no RNG state, same on every platform)."""
    value = (int(index) * 2654435761 + int(seed) * 40503 + 12345) % 4294967296
    return .82 + .36 * value / 4294967295.0


def prop_geometry(size, kind='box'):
    """Carry prop with its origin at the grip: a horizontal handle bar, two struts and a body below the fist.
    Every part is a closed box (fixtures_m8.closed_box), so the mesh is closed and readable by ray parity."""
    size = _finite(size, 'size', .05, 2.0)
    if kind not in ('box', 'lantern'):
        raise ValueError('M10_FIXTURE_CHOICE_INVALID: kind must be box or lantern')
    half = (.30 * size, .30 * size, .40 * size) if kind == 'lantern' else (.45 * size, .20 * size, .35 * size)
    top = -.10  # fingertips of the M6 hand end ~6 cm below the grip; the body starts below them
    parts = [((0.0, 0.0, 0.0), (.05, .012, .012)),
             ((-.045, 0.0, top / 2), (.006, .006, abs(top) / 2)), ((.045, 0.0, top / 2), (.006, .006, abs(top) / 2)),
             ((0.0, 0.0, top - half[2]), half)]
    vertices, faces, materials = [], [], []
    for index, (center, extent) in enumerate(parts):
        v, f = m8().closed_box(center, extent)
        offset = len(vertices)
        vertices.extend(v)
        faces.extend(tuple(i + offset for i in face) for face in f)
        materials.extend([1 if index == 3 else 0] * len(f))
    return {'vertices': vertices, 'faces': faces, 'materials': materials, 'body_center': [0.0, 0.0, top - half[2]],
            'body_half': list(half), 'grip': [0.0, 0.0, 0.0]}


def demo_layout(kind):
    """What each demo contains (pure). Durations other than A are proposals of this fixture, labelled as such."""
    if kind not in DEMO_KINDS:
        raise ValueError('M10_FIXTURE_DEMO_UNKNOWN: ' + str(kind) + ' not in ' + ', '.join(DEMO_KINDS))
    def human(name, location, role, color, **extra):
        return dict({'kind': 'HUMANOID', 'name': name, 'location': list(location), 'role': role, 'color': color,
                     'turn_deg': 90.0, 'reach_side': 'R', 'face': False, 'secondary': None, 'lower': False}, **extra)
    environment = {'cave': False, 'rocks': True, 'lantern': False, 'source_camera': False}
    props = []
    if kind == 'A_creature_reveal':
        duration, source = 15.0, 'TASK_REQUIREMENT'
        environment = {'cave': True, 'rocks': True, 'lantern': True, 'source_camera': True}
        actors = [{'kind': 'CREATURE', 'name': 'Creature', 'location': [0.0, 6.5, 0.0], 'scale': 1.9, 'role': 'CREATURE',
                   'color': (.30, .24, .22), 'face': False, 'secondary': None}]
    elif kind == 'B_two_character':
        duration, source = 10.0, 'PROPOSAL'
        actors = [human('HumanA', (-1.0, 0.0, 0.0), 'PROTAGONIST', (.12, .43, .65), turn_deg=90.0),
                  human('HumanB', (1.0, 0.0, 0.0), 'SUPPORTING', (.80, .42, .12), turn_deg=-90.0)]
    elif kind == 'C_body_face_physics':
        duration, source = 6.0, 'PROPOSAL'
        environment = {'cave': False, 'rocks': False, 'lantern': False, 'source_camera': False}
        actors = [human('Hero', (0.0, 0.0, 0.0), 'PROTAGONIST', (.10, .55, .50), face=True, secondary='pendant')]
    elif kind == 'D_editorial':
        duration, source = 8.0, 'PROPOSAL'
        environment = {'cave': True, 'rocks': True, 'lantern': False, 'source_camera': True}
        actors = [human('Speaker', (-.6, 0.0, 0.0), 'PROTAGONIST', (.45, .25, .65), face=True, turn_deg=90.0),
                  human('Listener', (.6, .4, 0.0), 'SUPPORTING', (.25, .55, .20), face=True, turn_deg=-90.0)]
    else:
        duration, source = 8.0, 'PROPOSAL'
        # The giver holds the prop in the hand facing the taker (+X is the giver's left), the taker reaches with
        # the hand facing the giver (its right, -X).
        # 1.6 m apart: at 1.0 m the rest hands were ~2 cm apart and the proxies would start interpenetrating.
        actors = [human('Giver', (-.8, 0.0, 0.0), 'PROTAGONIST', (.12, .43, .65), turn_deg=90.0, lower=True),
                  human('Taker', (.8, 0.0, 0.0), 'SUPPORTING', (.80, .42, .12), turn_deg=-90.0, reach_side='R', lower=True)]
        props = [{'name': 'Lantern', 'kind': 'lantern', 'size': .25, 'held_by': {'actor': 'Giver', 'side': 'L'}}]
    names = [a['name'] for a in actors] + [p['name'] for p in props]
    if len(set(names)) != len(names):
        raise ValueError('M10_FIXTURE_NAME_COLLISION: duplicate actor or prop names')
    coverage = []
    if kind == 'D_editorial':
        coverage = [{'shot': 'WIDE', 'subjects': ['Speaker', 'Listener']}, {'shot': 'MEDIUM', 'subjects': ['Speaker']},
                    {'shot': 'CLOSE_UP', 'subjects': ['Speaker'], 'bone': 'head'},
                    {'shot': 'REACTION', 'subjects': ['Listener'], 'bone': 'head'}]
    return {'kind': kind, 'duration_s': duration, 'duration_source': source, 'fps': FPS,
            'frame_range': frame_range(duration), 'environment': environment, 'actors': actors, 'props': props,
            'coverage_proposal': coverage}


def brief_hint(layout, actor_ids=None):
    """Brief for cinematic_create_candidate; actor ids exist only after registration, so they are injected."""
    actor_ids = actor_ids or {}
    return {'schema': 'production.cinematic_brief.v1', 'duration_s': layout['duration_s'], 'fps': layout['fps'],
            'actors': [{'actor_id': actor_ids.get(a['name'], 'REGISTER:' + a['name']), 'role': a['role']}
                       for a in layout['actors']]}


# ------------------------------------------------------------------ Blender builders (bpy imported lazily)

def _collection(name):
    """Make `name` the active collection, so the reused M6/M8 helpers (which link to bpy.context.collection) put
    their objects there."""
    import bpy
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    layer = bpy.context.view_layer.layer_collection.children[collection.name]
    bpy.context.view_layer.active_layer_collection = layer
    return collection


def _move_to(collection, obj):
    for owner in list(obj.users_collection):
        if owner != collection:
            owner.objects.unlink(obj)
    if collection.objects.get(obj.name) is None:
        collection.objects.link(obj)


def _material(name, color, emission=0.0):
    material = m6()._material(name, color)
    if emission:
        shader = material.node_tree.nodes.get('Principled BSDF')
        if shader is not None and 'Emission Strength' in shader.inputs:
            shader.inputs['Emission Color'].default_value = (*color, 1)
            shader.inputs['Emission Strength'].default_value = emission
    return material


def _closed_mesh(name, vertices, faces, materials, face_materials=None):
    import bpy
    data = bpy.data.meshes.new(name + '_Data')
    data.from_pydata([tuple(v) for v in vertices], [], [tuple(f) for f in faces])
    data.update()
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    for material in materials:
        data.materials.append(material)
    if face_materials:
        for polygon, index in zip(data.polygons, face_materials):
            polygon.material_index = index
    obj['fixture_only'] = True
    if obj.name != name:
        raise RuntimeError('M10_FIXTURE_NAME_COLLISION: ' + name)
    return obj


def _look_at(obj, target):
    from mathutils import Vector
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat('-Z', 'Y').to_euler()


def _rock(name, center, radii, seed, material):
    import bmesh
    import bpy
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=1, radius=1.0)
    for index, vertex in enumerate(bm.verts):
        factor = rock_jitter(index, seed)
        vertex.co.x *= radii[0] * factor
        vertex.co.y *= radii[1] * factor
        vertex.co.z = max(vertex.co.z * radii[2] * factor, -.35 * radii[2])
    low = min(v.co.z for v in bm.verts)
    for vertex in bm.verts:
        # Sink 3 cm into the ground: no floating gap a contact or ray test would read as a hole.
        vertex.co.z += -low - .03
    data = bpy.data.meshes.new(name + '_Data')
    bm.to_mesh(data)
    bm.free()
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = center
    data.materials.append(material)
    obj['fixture_only'] = True
    return obj


def build_environment(cave=True, rocks=True, lantern=False, source_camera=False):
    """Ground at z=0, an optional dark cave at +Y (the creature emerges from it towards -Y), rocks, an optional
    emissive lantern prop as a practical anchor and an optional source camera that M10 must never touch. No Light
    objects: production lighting is M10's job."""
    import bpy
    from mathutils import Vector
    scene = bpy.context.scene
    collection = _collection('ENV')
    ground_material = _material('ENV_GroundMaterial', (.06, .055, .05))
    dark = _material('ENV_CaveMaterial', (.015, .015, .018))
    stone = _material('ENV_RockMaterial', (.18, .17, .16))
    objects = {}
    ground = _closed_mesh('ENV_Ground', [(-15, -14, GROUND_Z), (15, -14, GROUND_Z), (15, 20, GROUND_Z), (-15, 20, GROUND_Z)],
                          [(0, 1, 2, 3)], [ground_material])
    objects['ground'] = ground.name
    dark_zone = None
    if cave:
        walls = {}
        for label, center, half in (('Back', (0, 9.5, 2.0), (5.0, .5, 2.0)), ('Left', (-4.5, 6.0, 2.0), (.5, 3.0, 2.0)),
                                    ('Right', (4.5, 6.0, 2.0), (.5, 3.0, 2.0)), ('Roof', (0, 6.0, 4.25), (5.0, 3.5, .25))):
            v, f = m8().closed_box(center, half)
            walls[label] = _closed_mesh('ENV_Cave' + label, v, f, [dark]).name
        objects['cave'] = walls
        dark_zone = {'x_m': [-4.0, 4.0], 'y_m': [3.0, 9.0], 'z_m': [0.0, 4.0], 'mouth_y_m': 3.0,
                     'note': 'interior enclosed by back wall, side walls and roof; open towards -Y'}
    rock_rows = []
    if rocks:
        table = [((-2.6, 1.2, 0), (.60, .50, .45)), ((2.8, 2.0, 0), (.80, .70, .60)), ((-3.2, 4.0, 0), (.90, .80, .80)),
                 ((1.9, -1.8, 0), (.35, .30, .25)), ((3.4, 6.5, 0), (.70, .60, .90))]
        for index, (center, radii) in enumerate(table, start=1):
            rock = _rock('ENV_Rock_%02d' % index, center, radii, index, stone)
            rock_rows.append({'object': rock.name, 'center': list(center), 'radii_m': list(radii)})
    objects['rocks'] = [r['object'] for r in rock_rows]
    practicals = []
    if lantern:
        bpy.context.view_layer.update()
        rock = bpy.data.objects[rock_rows[0]['object']] if rock_rows else None
        top = max((rock.matrix_world @ v.co).z for v in rock.data.vertices) if rock else 0.0
        geometry = prop_geometry(.30, 'lantern')
        prop = build_prop('ENV_Lantern', .30, 'lantern', emission=4.0, collection_name='ENV')
        bottom = geometry['body_center'][2] - geometry['body_half'][2]
        prop.location = (rock.location.x if rock else -2.0, rock.location.y if rock else 1.0, top - bottom - .01)
        bpy.context.view_layer.update()
        practicals.append({'object': prop.name, 'role': 'PRACTICAL_ANCHOR', 'production_light': False,
                           'light_anchor_world': list(prop.matrix_world @ Vector(geometry['body_center'])),
                           'emission_strength': 4.0})
    camera = None
    if source_camera:
        data = bpy.data.cameras.new('ENV_SourceCamera_Data')
        data.lens = 35.0
        cam = bpy.data.objects.new('ENV_SourceCamera', data)
        collection.objects.link(cam)
        cam.location = (0.0, -10.0, 1.7)
        _look_at(cam, (0.0, 4.0, 1.4))
        scene.camera = cam
        camera = {'object': cam.name, 'data': data.name, 'lens': data.lens, 'location': list(cam.location),
                  'rotation_euler': list(cam.rotation_euler), 'purpose': 'source camera; M10 must leave it untouched'}
    world = scene.world or bpy.data.worlds.new('ENV_World')
    scene.world = world
    world.color = (.008, .008, .011)
    if world.node_tree and world.node_tree.nodes.get('Background') is not None:
        world.node_tree.nodes['Background'].inputs['Color'].default_value = (.008, .008, .011, 1)
        world.node_tree.nodes['Background'].inputs['Strength'].default_value = .5
    return {'collection': collection.name, 'objects': objects, 'dark_zone': dark_zone, 'rocks': rock_rows,
            'practicals': practicals, 'source_camera': camera, 'world': world.name, 'ground_z_m': GROUND_Z,
            'production_lights': 0}


def build_prop(name, size, kind='box', emission=0.0, collection_name='PROPS'):
    """Closed carry prop (box case or lantern) with its origin at the grip; free object, unit scale, no parent."""
    geometry = prop_geometry(size, kind)
    _collection(collection_name)
    color = (.95, .72, .30) if kind == 'lantern' else (.55, .40, .25)
    materials = [_material(name + '_Frame', (.12, .12, .13)), _material(name + '_Body', color, emission)]
    obj = _closed_mesh(name, geometry['vertices'], geometry['faces'], materials, geometry['materials'])
    obj['m10_fixture_prop'] = kind
    obj['grip_at_origin'] = True
    return obj


def _rows_with_root_poles(variant, scale):
    """M6 rows scaled, with the IK poles parented to the root so they travel and turn with root motion (parentless
    poles would stay behind and flip the knees once the root moves)."""
    rows = []
    for name, head, tail, parent, deform in m6().bone_definitions(variant):
        if name.startswith('pole.'):
            parent = 'root'
        rows.append((name, tuple(c * scale for c in head), tuple(c * scale for c in tail), parent, deform))
    return rows


def _scale_body(body, scale):
    from mathutils import Matrix
    body.data.transform(Matrix.Scale(scale, 4))
    body.data.update()


def _curves(action):
    return [c for layer in action.layers for strip in layer.strips for bag in strip.channelbags for c in bag.fcurves]


def _reset_pose(rig):
    for pose_bone in rig.pose.bones:
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)


def _key_action(rig, spec):
    """One layered Action from an armature-space spec: translations become rest-frame locations and rotations
    about armature axes become rest-frame quaternions (rest^-1 @ R @ rest), so the authored numbers mean metres and
    degrees in the scene, whatever the bone roll."""
    import bpy
    from mathutils import Matrix, Vector
    action = bpy.data.actions.new(spec['name'])
    if action.name != spec['name']:
        raise RuntimeError('M10_FIXTURE_NAME_COLLISION: ' + spec['name'])
    action.use_fake_user = True  # inactive Actions must survive the save
    action['fixture_only'] = True
    action['m10_fixture_purpose'] = spec['purpose']
    rig.animation_data_create()
    rig.animation_data.action = action
    previous = {}
    for frame, pose in spec['keys']:
        for bone in KEYED_BONES:
            # Every Action keys the same bone set, so a clip never inherits a pose left by another clip.
            pose_bone = rig.pose.bones[bone]
            rest = rig.data.bones[bone].matrix_local.to_3x3()
            row = pose.get(bone, {})
            pose_bone.location = rest.inverted() @ Vector(row.get('t', (0.0, 0.0, 0.0)))
            rotation = Matrix.Identity(3)
            for axis, degrees in row.get('r', ()):
                rotation = Matrix.Rotation(math.radians(degrees), 3, axis) @ rotation
            quaternion = (rest.inverted() @ rotation @ rest).to_quaternion()
            if bone in previous and previous[bone].dot(quaternion) < 0:
                quaternion.negate()  # keep the shortest path between keys
            previous[bone] = quaternion.copy()
            pose_bone.rotation_quaternion = quaternion
            pose_bone.keyframe_insert('location', frame=frame)
            pose_bone.keyframe_insert('rotation_quaternion', frame=frame)
    for curve in _curves(action):
        bone = curve.data_path.split('"')[1] if '"' in curve.data_path else None
        if bone in spec['linear_bones']:
            # Constant root speed (and constant turn rate): Bezier ease would fake an acceleration.
            for point in curve.keyframe_points:
                point.interpolation = 'LINEAR'
    _reset_pose(rig)
    keyed = [p.co.x for c in _curves(action) for p in c.keyframe_points]
    return {'name': action.name, 'purpose': spec['purpose'], 'frame_range': list(spec['frame_range']),
            'keyed_range': [min(keyed), max(keyed)], 'keyframes': len(keyed), 'fcurves': len(_curves(action)),
            'slot': action.slots[0].identifier if len(action.slots) else None,
            'root_motion_planned_m': spec['root_motion_m'], 'measure': spec['measure'],
            **{k: spec[k] for k in ('yaw_deg', 'gait', 'reach_side', 'head_yaw_deg', 'cyclic_relative_to_root') if k in spec}}


def _actor(name, variant, location, scale, color, specs, kind):
    import bpy
    collection = _collection('ACTOR_' + name)
    materials = [_material(name + '_Skin', color), _material(name + '_Hands', tuple(min(1.0, c * 1.5 + .1) for c in color)),
                 _material(name + '_Feet', tuple(c * .6 for c in color)), _material(name + '_Mass', (.42, .16, .13))]
    # Build at unit size (the M6 proxy uses fixed segment radii), then bake the size into bones and vertices so the
    # object scale stays exactly 1 (M8 and staging read real metres).
    rows = _rows_with_root_poles(variant, 1.0)
    rig = m6()._armature(name, rows)
    body = m6()._proxy_mesh(rig, variant, materials)
    if abs(scale - 1.0) > 1e-12:
        from mathutils import Matrix
        rig.data.transform(Matrix.Scale(scale, 4))
        _scale_body(body, scale)
    m6()._add_ik(rig)
    rig['marker_ground_height_m'] = GROUND_Z
    rig['m10_fixture_actor'] = kind
    rig['m10_fixture_scale'] = float(scale)
    body.parent = rig  # the mesh follows staging deltas on the rig; the armature modifier stays relative (identity)
    rig.location = (location[0], location[1], location[2] - M6_FOOT_Z * scale)
    bpy.context.view_layer.update()
    actions = [_key_action(rig, spec) for spec in specs]
    rig.animation_data.action = bpy.data.actions[actions[0]['name']]  # the slot is auto-assigned by identifier
    bpy.context.scene.frame_set(1)
    heads = {b.name: list(b.head_local) for b in rig.data.bones}
    tails = {b.name: list(b.tail_local) for b in rig.data.bones}
    return {'name': name, 'kind': kind, 'object': rig.name, 'data': rig.data.name, 'type': 'ARMATURE',
            'body': body.name, 'meshes': [body.name], 'collection': collection.name, 'variant': variant,
            'location_m': list(location), 'scale_factor': float(scale), 'object_scale': list(rig.scale),
            'height_m': round(tails['head'][2] - M6_FOOT_Z * scale, 6), 'bones': BONES,
            'rest_heads_rig_space': {k: heads[k] for k in ('root', 'head', 'hand.L', 'hand.R', 'foot.L', 'foot.R')},
            'actions': actions, 'active_action': actions[0]['name'], 'face': None, 'secondary': [], 'forward_axis': '-Y'}


def build_creature(name, location, scale=1.9):
    """Large asymmetric creature (M6 'asymmetric' rows, heavier right arm) with Emerge, Stalk, Stare, AttackPrep."""
    scale = _finite(scale, 'scale', *CREATURE_SCALE_RANGE)
    return _actor(name, 'asymmetric', location, scale, (.30, .24, .22), creature_action_specs(name, scale), 'CREATURE')


def build_humanoid(name, location, turn_deg=90.0, reach_side='R', color=(.12, .43, .65), lower=False):
    """Biped with Idle, Turn (turn_deg, positive towards +X), React, Walk and Reach (reach_side hand), plus Lower when
    asked."""
    return _actor(name, 'humanoid', location, 1.0, color, humanoid_action_specs(name, turn_deg, reach_side, lower),
                  'HUMANOID')


class _AtOrigin:
    """Rig at the world origin, rest pose and no Action while m7.build runs: m7 parents face and neck without a
    parent inverse and parents eyes at their current world matrix, which is only consistent with an identity rig in
    rest pose. Everything is put back afterwards; the face is parented so it follows."""

    def __init__(self, rig):
        self.rig = rig

    def __enter__(self):
        import bpy
        self.location = self.rig.location.copy()
        self.action = self.rig.animation_data.action if self.rig.animation_data else None
        if self.rig.animation_data:
            self.rig.animation_data.action = None
        _reset_pose(self.rig)
        self.rig.location = (0.0, 0.0, 0.0)
        bpy.context.view_layer.update()
        return self.rig

    def __exit__(self, *exc):
        import bpy
        self.rig.location = self.location
        if self.action is not None:
            self.rig.animation_data.action = self.action
        bpy.context.view_layer.update()
        bpy.context.scene.frame_set(bpy.context.scene.frame_current)
        return False


def _remove_head_segment(body, bone='head'):
    """Drop the M6 proxy head (one closed component weighted to `head`) so the M7 head is visible instead of being
    buried inside it. Other components stay closed; vertex groups survive through bmesh's deform layer."""
    import bmesh
    group = body.vertex_groups.get(bone)
    if group is None:
        return 0
    indices = {v.index for v in body.data.vertices if any(g.group == group.index and g.weight > 0 for g in v.groups)}
    bm = bmesh.new()
    bm.from_mesh(body.data)
    bm.verts.ensure_lookup_table()
    doomed = [bm.verts[i] for i in sorted(indices)]
    bmesh.ops.delete(bm, geom=doomed, context='VERTS')
    bm.to_mesh(body.data)
    bm.free()
    body.data.update()
    return len(indices)


def add_face(humanoid):
    """M7 'shape_keys' face (ARKit names) on the humanoid's head bone, keys at 0, objects renamed per actor."""
    import bpy
    from mathutils import Vector
    rig = bpy.data.objects[humanoid['object']]
    body = bpy.data.objects[humanoid['body']]
    removed = _remove_head_segment(body)
    with _AtOrigin(rig):
        center = tuple(rig.data.bones['head'].head_local + Vector((0.0, 0.0, 0.10)))
        manifest = m7().build(None, variant='shape_keys', save=False, existing_rig=rig, head_bone='head', center=center)
    name = humanoid['name']
    collection = bpy.data.collections['ACTOR_' + name]
    renamed = {}
    for role, old in (('face', manifest['face']), ('neck', manifest['neck']), ('mouth_interior', manifest['mouth_interior']),
                      ('eye_L', manifest['eyes']['L']), ('eye_R', manifest['eyes']['R'])):
        obj = bpy.data.objects[old]
        new = name + '_' + {'face': 'Face', 'neck': 'Neck', 'mouth_interior': 'MouthInterior', 'eye_L': 'Eye.L',
                            'eye_R': 'Eye.R'}[role]
        obj.name = new
        obj.data.name = new + '_Mesh'
        if obj.data.shape_keys:
            obj.data.shape_keys.name = new + '_Keys'  # 'Key.001' names would make KEY NLA targets ambiguous
        _move_to(collection, obj)
        renamed[role] = obj.name
    neck = bpy.data.objects[renamed['neck']]
    group = neck.vertex_groups.get('neck')
    if group is not None and 'neck' not in rig.data.bones:
        # m7 weights its neck to a 'neck' bone the M6 rig does not have; without a matching group the neck would stay
        # behind under root motion. The chest is the closest M6 bone.
        group.name = 'chest'
    face = bpy.data.objects[renamed['face']]
    keys = face.data.shape_keys
    for obj_name in (renamed['face'], renamed['eye_L'], renamed['eye_R']):
        data = bpy.data.objects[obj_name].data.shape_keys
        if data:
            for block in data.key_blocks[1:]:
                block.value = 0.0  # Blender 5.2.1 creates Python shape keys at 1.0 (measured in M7)
    humanoid['meshes'] += [renamed[k] for k in ('face', 'neck', 'mouth_interior', 'eye_L', 'eye_R')]
    humanoid['face'] = {'variant': 'shape_keys', 'system': manifest['system'], 'objects': renamed,
                        'shape_key_owner': keys.name if keys else None, 'shape_keys': manifest['shape_keys'],
                        'shape_key_count': len(keys.key_blocks) - 1 if keys else 0, 'head_bone': 'head',
                        'center_rig_space': list(center), 'vertices': manifest['vertices'],
                        'proxy_head_vertices_removed': removed, 'neck_group_rebound_to': 'chest'}
    return humanoid['face']


def add_secondary_piece(humanoid, kind='pendant'):
    """Hanging closed pendant on the chest bone, the fixtures_m8 _accessory pattern adapted to an M6 rig at any
    location: origin at the anchor, unit scale, no modifiers, no shape keys (M8 refuses secondary morphs)."""
    import bpy
    from mathutils import Matrix, Vector
    if kind != 'pendant':
        raise ValueError('M10_FIXTURE_CHOICE_INVALID: only the pendant secondary piece is built')
    rig = bpy.data.objects[humanoid['object']]
    # Own collection, apart from the rig and body: a cinematic that replaces the pendant with its retimed copy must
    # exclude it, and excluding an object mirrors its collection. In ACTOR_<name> that re-linked the rig and the body
    # one by one, which changed their users_collection and left the accepted M8 generation stale (measured, demo C).
    _collection('ACTOR_' + humanoid['name'] + '_SECONDARY')
    vertices, faces = m8().closed_box((0, 0, -.20), (.065, .035, .13))
    obj = m8()._mesh(humanoid['name'] + '_Pendant', vertices, faces, (.86, .46, .12))
    obj.rotation_mode = 'QUATERNION'
    obj.parent, obj.parent_type, obj.parent_bone = rig, 'BONE', 'chest'
    # 7 cm in front of the chest proxy (front at y = -0.11 in rig space) so the rest pose has no initial penetration.
    anchor_local = Vector((.08, -.17, 1.47))
    bpy.context.view_layer.update()
    anchor = rig.matrix_world @ anchor_local
    obj.matrix_world = Matrix.Translation(anchor)
    obj['attachment_origin_is_anchor'] = True
    obj['m10_fixture_secondary'] = 'DANGLING_ACCESSORY'
    bpy.context.view_layer.update()
    row = {'object': obj.name, 'classification': 'DANGLING_ACCESSORY', 'kind': kind,
           'collection': obj.users_collection[0].name,
           'attachment': {'object': rig.name, 'bone': 'chest'}, 'anchor_world_m': list(anchor),
           'm8_manifest': {'object': obj.name, 'classification': 'DANGLING_ACCESSORY', 'fixture_only': True,
                           'attachment': {'object': rig.name, 'bone': 'chest'},
                           'evidence': ['Controlled fixture specification; relationships and geometry must still be inspected'],
                           'expected_qa': 'NOT_RUN'},
           'm8_brief_hint': {'subsystem': 'ACCESSORY', 'fps': FPS, 'settings': {}, 'qa': {}},
           'colliders_hint': [humanoid['body']]}
    humanoid['secondary'].append(row)
    humanoid['meshes'].append(obj.name)
    return row


def place_in_hand(prop, humanoid, side='R', frame=1):
    """Put the prop's grip on the hand at `frame` (evaluated, with the active Action). The prop stays a free object:
    M10 attaches its own copy with CHILD_OF; the grip matrix in bone space is recorded for that."""
    import bpy
    scene = bpy.context.scene
    scene.frame_set(frame)
    rig = bpy.data.objects[humanoid['object']]
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = rig.evaluated_get(depsgraph)
    bone = evaluated.pose.bones['hand.' + side]
    grip = evaluated.matrix_world @ bone.head.lerp(bone.tail, .6)
    prop.location = grip
    bpy.context.view_layer.update()
    bone_world = evaluated.matrix_world @ bone.matrix
    relative = bone_world.inverted() @ prop.matrix_world
    return {'object': prop.name, 'actor': humanoid['name'], 'bone': 'hand.' + side, 'frame': frame,
            'grip_world_m': list(grip), 'grip_matrix_bone_space': [list(r) for r in relative],
            'attachment_in_source': 'NONE (free object; M10 attaches its own copy with CHILD_OF)'}


def _require_safe_session():
    """Never clear a scene that is not ours: only a headless process, either unsaved or holding one of this module's
    own previous demos (its manifest sits next to it)."""
    import bpy
    if not bpy.app.background:
        raise RuntimeError('M10_FIXTURES_REQUIRE_BACKGROUND: build demos only in an own headless factory-startup process')
    if bpy.data.filepath:
        path = Path(bpy.data.filepath)
        manifest = path.with_suffix('.json')
        try:
            own = json.loads(manifest.read_text(encoding='utf-8')).get('schema') == SCHEMA
        except (OSError, ValueError):
            own = False
        if not own:
            raise RuntimeError('M10_FIXTURES_REFUSE_OPEN_FILE: ' + str(path) + ' is not an M10 demo fixture')


def build_demo(kind, out_dir, save=True):
    """Compose one demo scene, save `<kind>.blend` and `<kind>.json` (exclusive, never overwritten) and return the
    manifest. The scene is rebuilt from factory settings, so the result does not depend on what ran before."""
    import bpy
    layout = demo_layout(kind)
    out = Path(out_dir).resolve()
    blend, manifest_path = out / (kind + '.blend'), out / (kind + '.json')
    if save and (blend.exists() or manifest_path.exists()):
        raise FileExistsError('M10_FIXTURE_OUTPUT_EXISTS: ' + str(blend))
    _require_safe_session()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = 'M10_' + kind
    scene.render.fps, scene.render.fps_base = layout['fps'], 1.0
    scene.unit_settings.system, scene.unit_settings.scale_length = 'METRIC', 1.0
    scene.frame_start, scene.frame_end = layout['frame_range']
    environment = build_environment(**layout['environment'])
    actors = {}
    for spec in layout['actors']:
        if spec['kind'] == 'CREATURE':
            actor = build_creature(spec['name'], spec['location'], spec['scale'])
        else:
            actor = build_humanoid(spec['name'], spec['location'], spec['turn_deg'], spec['reach_side'], spec['color'],
                                   spec.get('lower', False))
        actor['role'] = spec['role']
        if spec['face']:
            add_face(actor)
        if spec['secondary']:
            add_secondary_piece(actor, spec['secondary'])
        actors[spec['name']] = actor
    props = []
    for spec in layout['props']:
        prop = build_prop(spec['name'], spec['size'], spec['kind'])
        row = {'object': prop.name, 'kind': spec['kind'], 'size_m': spec['size'], 'held_by': None, 'scale': list(prop.scale)}
        if spec.get('held_by'):
            row['held_by'] = place_in_hand(prop, actors[spec['held_by']['actor']], spec['held_by']['side'])
        props.append(row)
    _collection('ENV')
    scene.frame_set(layout['frame_range'][0])
    manifest = {'schema': SCHEMA, 'kind': kind, 'fixture_only': True, 'blender': bpy.app.version_string,
                'builder': 'tests/blender/m10_cinematic_fixtures.py', 'scene': scene.name, 'fps': layout['fps'],
                'fps_base': 1.0, 'frame_range': layout['frame_range'], 'duration_s': layout['duration_s'],
                'duration_source': layout['duration_source'], 'units': 'METRIC', 'scale_length': 1.0,
                'forward_axis': '-Y', 'up_axis': '+Z', 'ground_z_m': GROUND_Z, 'environment': environment,
                'actors': list(actors.values()), 'props': props, 'brief_hint': brief_hint(layout),
                'coverage_proposal': layout['coverage_proposal'], 'production_lights': 0,
                'objects': sorted(o.name for o in scene.objects), 'limitations': LIMITATIONS}
    if save:
        out.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        data = blend.read_bytes()
        manifest.update(blend=str(blend), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        with open(manifest_path, 'x', encoding='utf-8') as handle:
            json.dump(manifest, handle, indent=1, default=str)
    return manifest
