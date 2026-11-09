"""Pure M10 vocabulary, strict validation and explicit time conversion (no Blender).

Every number that reaches Blender passes through these validators: finite, typed, bounded. Seconds and frames are
converted only by `seconds_to_frame`/`frame_to_seconds`, which record their rounding so a timeline can be
reproduced exactly.
"""
from __future__ import annotations

import math
import re

SCHEMA = 'production.cinematic.v1'
CATEGORY = 'cinematics'
STATES = frozenset({'PLANNED', 'CINEMATIC_CANDIDATE', 'ASSEMBLED', 'REVIEWED', 'ACCEPTED', 'REJECTED'})
ASSEMBLY_STATES = frozenset({'CINEMATIC_CANDIDATE', 'ASSEMBLED', 'REVIEWED'})
PROVENANCE = ('USER_REQUIREMENT', 'INFERRED_DEFAULT', 'PROPOSED_CREATIVE_DECISION')
FPS_ALLOWED = (24, 25, 30, 48, 50, 60)
MAX_DURATION_S = 120.0
MAX_ACTORS = 8
MAX_SHOTS = 64
MAX_CAMERAS = 32
MAX_LIGHTS = 32

SHOT_TYPES = ('ESTABLISHING', 'WIDE', 'MEDIUM', 'CLOSE_UP', 'EXTREME_CLOSE_UP', 'OVER_THE_SHOULDER', 'LOW_ANGLE',
              'HIGH_ANGLE', 'TRACKING', 'REACTION', 'INSERT', 'CUSTOM')
CAMERA_MOVES = ('STATIC', 'DOLLY', 'TRUCK', 'CRANE', 'ORBIT', 'PAN', 'TILT', 'TRACKING', 'HANDHELD')
EASINGS = ('LINEAR', 'EASE_IN', 'EASE_OUT', 'EASE_IN_OUT')
TRANSITIONS = ('CUT', 'MATCH_CUT', 'HOLD', 'FADE_IN', 'FADE_OUT')
BEAT_KINDS = ('ESTABLISHING', 'REVEAL', 'APPROACH', 'RECOGNITION', 'THREAT', 'REACTION', 'INTERACTION', 'ENDING', 'CUSTOM')
INTERACTION_KINDS = ('OBSERVE', 'APPROACH', 'TURN_TOWARD', 'REACH', 'TOUCH_SURFACE', 'PREPARE_ATTACK', 'REACT', 'RETREAT',
                     'HOLD_PROP', 'HANDOFF_PROP', 'GAZE')
LIGHT_ROLES = ('KEY', 'FILL', 'RIM', 'PRACTICAL', 'AMBIENT', 'BOUNCE')
LIGHT_TYPES = ('POINT', 'SUN', 'SPOT', 'AREA')
MOOD_PRESETS = ('NEUTRAL_THREE_POINT', 'HIGH_KEY', 'LOW_KEY', 'RIM_DRAMATIC', 'HORROR_UNDERLIGHT', 'NIGHT_EXTERIOR',
                'PRACTICAL_ONLY')
ACTOR_ROLES = ('PROTAGONIST', 'ANTAGONIST', 'CREATURE', 'SUPPORTING', 'EXTRA', 'PROP', 'CUSTOM')
NAME = re.compile(r'^[A-Za-z0-9_.\- ]{1,48}$')


def fail(code: str, detail: str):
    raise ValueError(code + ': ' + detail)


def number(value, field, minimum=None, maximum=None, integer=False, code='CINEMATIC_NUMBER_INVALID'):
    """Finite number (never bool), optionally integer and bounded."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        fail(code, field + ' must be a finite number')
    if integer and int(value) != value:
        fail('CINEMATIC_INTEGER_REQUIRED', field + ' must be an integer')
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        fail('CINEMATIC_RANGE_INVALID', field + ' must be in [' + str(minimum) + ', ' + str(maximum) + ']')
    return int(value) if integer else float(value)


def vector(value, field, size=3, minimum=-1e6, maximum=1e6):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        fail('CINEMATIC_VECTOR_INVALID', field + ' must be a list of ' + str(size) + ' numbers')
    return [number(v, field, minimum, maximum) for v in value]


def choice(value, field, options):
    if value not in options:
        fail('CINEMATIC_CHOICE_INVALID', field + ' must be one of ' + ', '.join(options))
    return value


def name(value, field):
    if not isinstance(value, str) or not NAME.match(value) or value.strip() != value:
        fail('CINEMATIC_NAME_INVALID', field + ' must be 1-48 characters [A-Za-z0-9_.- ]')
    return value


def text(value, field, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail('CINEMATIC_TEXT_INVALID', field + ' must be non-empty text of at most ' + str(maximum) + ' characters')
    return value.strip()


def closed(options, allowed, code='CINEMATIC_OPTION_UNSUPPORTED'):
    """Unknown keys are refused, never ignored."""
    options = dict(options or {})
    unknown = sorted(set(options) - set(allowed))
    if unknown:
        fail(code, 'unknown keys ' + ', '.join(unknown))
    return options


def fps_value(fps, fps_base=1.0):
    fps = number(fps, 'fps', 1, 240, integer=True)
    fps_base = number(fps_base, 'fps_base', 0.001, 10.0)
    return fps / fps_base


def seconds_to_frame(seconds, fps, fps_base=1.0, frame_start=1):
    """Scene frame for a time in seconds measured from frame_start. Rounding: half up. Returns (frame, record)."""
    seconds = number(seconds, 'seconds', 0, MAX_DURATION_S)
    rate = fps_value(fps, fps_base)
    exact = frame_start + seconds * rate
    frame = int(math.floor(exact + 0.5))
    return frame, {'seconds': seconds, 'fps': fps, 'fps_base': fps_base, 'frame_start': frame_start,
                   'exact_frame': exact, 'frame': frame, 'rounding_error_frames': frame - exact,
                   'rule': 'frame = frame_start + floor(seconds * fps / fps_base + 0.5)'}


def frame_to_seconds(frame, fps, fps_base=1.0, frame_start=1):
    frame = number(frame, 'frame', -1_048_574, 1_048_574, integer=True)
    return (frame - frame_start) / fps_value(fps, fps_base)


def provenance(fields, user, defaults, proposals):
    """Provenance per field: what the user asked, what was inferred, what the planner proposes."""
    result = {}
    for key in fields:
        if key in user:
            result[key] = 'USER_REQUIREMENT'
        elif key in proposals:
            result[key] = 'PROPOSED_CREATIVE_DECISION'
        elif key in defaults:
            result[key] = 'INFERRED_DEFAULT'
    return result


def short(generation_id: str) -> str:
    return generation_id[:8]


def owned_prefix(generation_id: str, version: int) -> str:
    return 'M10C_' + short(generation_id) + '_v' + str(int(version))


def owned_name(generation_id: str, version: int, suffix: str) -> str:
    """Deterministic name of an owned datablock; Blender names are limited to 63 bytes."""
    value = owned_prefix(generation_id, version) + '_' + suffix
    if len(value.encode('utf-8')) > 63:
        fail('CINEMATIC_NAME_TOO_LONG', value)
    return value
