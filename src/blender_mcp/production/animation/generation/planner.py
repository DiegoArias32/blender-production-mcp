"""Pure, bounded body-animation intent and pose planning for M6.

No Blender state or Actions are touched here. Angles are right-handed degrees
about body lateral/forward/up axes, not bone-local Euler channels. Vectors are
metres in that body basis. ``feet.offset_m`` is a world displacement from the
effector's rest position (expressed in the body basis), so an IK runtime must
not add root displacement a second time. Every default is disclosed.

Templates are configurable animation heuristics. They neither certify artistic
quality nor replace installed-rig constraints, reach or contact verification.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re

TYPES = frozenset({"IDLE", "ATTACK", "LOCOMOTION", "TURN", "HIT_REACTION", "START_STOP"})
PROFILES = frozenset({"HUMAN", "HEAVY_HUMANOID", "HEAVY_CREATURE", "CREATURE", "ASYMMETRIC_CREATURE", "CUSTOM"})
ATTACK_TYPES = frozenset({"OVERHEAD", "SWING", "THRUST", "SLAM", "CUSTOM_BODY_ATTACK"})
LOCOMOTION_TYPES = frozenset({"WALK", "HEAVY_WALK", "RUN", "CUSTOM_CYCLE"})
CONTACT_PHASES = ("HEEL_STRIKE", "FOOT_FLAT", "MID_STANCE", "HEEL_RISE", "TOE_OFF", "SWING")
SEMANTIC_ROLES = ("root", "pelvis", "spine", "chest", "head", "left_upper_arm", "left_lower_arm", "left_hand",
                  "right_upper_arm", "right_lower_arm", "right_hand", "left_upper_leg", "left_lower_leg", "left_foot",
                  "right_upper_leg", "right_lower_leg", "right_foot")

_ALLOWED = frozenset({"intent", "type", "animation_type", "profile", "style", "fps", "duration_s", "duration_seconds",
    "frame_start", "root_mode", "root_motion", "speed_m_s", "forward_distance_m", "stride_length_m", "cycle_duration_s",
    "locomotion_type", "attack_type", "dominant_side", "turn_degrees", "turn_direction", "impact_direction",
    "severity", "strength", "mass_asymmetry", "contact_requirements", "phase_fractions", "forward_axis", "transition",
    "stance_width_m", "step_height_m", "max_bone_frame_samples", "max_keyframes", "custom_pose_plan"})


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("INVALID_ANIMATION_BRIEF: " + name + " must be finite in " + str(low) + ".." + str(high))
    return float(value)


def _enum(value, name, choices):
    if not isinstance(value, str) or value.upper() not in choices:
        raise ValueError("INVALID_ANIMATION_BRIEF: unsupported " + name + ": " + str(value))
    return value.upper()


def _direction(value):
    if isinstance(value, str):
        directions = {"FORWARD": [0., 1., 0.], "BACKWARD": [0., -1., 0.], "LEFT": [1., 0., 0.],
                      "RIGHT": [-1., 0., 0.], "UP": [0., 0., 1.], "DOWN": [0., 0., -1.]}
        if value.upper() not in directions:
            raise ValueError("INVALID_ANIMATION_BRIEF: unknown impact_direction")
        return directions[value.upper()]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("INVALID_ANIMATION_BRIEF: impact_direction requires a named direction or finite vector")
    result = [_number(v, "impact_direction", -100, 100) for v in value]
    size = math.sqrt(sum(v*v for v in result))
    if size < 1e-9:
        raise ValueError("INVALID_ANIMATION_BRIEF: impact_direction cannot be zero")
    return [v / size for v in result]


def _parse_intent(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("INVALID_ANIMATION_BRIEF: intent must be a nonempty string of at most 2048 characters")
    text = value.lower()
    patterns = {"ATTACK": r"\b(attack|ataque|overhead|slam|thrust|swing)\b",
                "LOCOMOTION": r"\b(walk|caminar|caminata|run|correr|locomotion)\b",
                "IDLE": r"\b(idle|reposo|quieto)\b", "TURN": r"\b(turn|giro|girar)\b",
                "HIT_REACTION": r"\b(hit|reaction|reaccion|reacción|impacto)\b",
                "START_STOP": r"\b(start|stop|arranque|parada)\b"}
    hits = [kind for kind, pattern in patterns.items() if re.search(pattern, text)]
    if "START_STOP" in hits and "LOCOMOTION" in hits:
        hits.remove("LOCOMOTION")
    if len(hits) != 1:
        raise ValueError("ANIMATION_INTENT_AMBIGUOUS: supply one explicit supported animation type")
    values = {"type": hits[0]}
    heavy = bool(re.search(r"\b(heavy|pesado|pesada)\b", text))
    creature = bool(re.search(r"\b(creature|criatura)\b", text))
    asymmetric = bool(re.search(r"\b(asymmetric|asimetrico|asimétrica|asimetrica)\b", text))
    if asymmetric:
        values["profile"] = "ASYMMETRIC_CREATURE"
    elif heavy:
        values["profile"] = "HEAVY_CREATURE" if creature else "HEAVY_HUMANOID"
    elif creature:
        values["profile"] = "CREATURE"
    if values["type"] == "ATTACK":
        for token in ("OVERHEAD", "SLAM", "THRUST", "SWING"):
            if re.search(r"\b" + token.lower() + r"\b", text):
                values["attack_type"] = token
                break
    if values["type"] == "LOCOMOTION":
        values["locomotion_type"] = "RUN" if re.search(r"\b(run|correr)\b", text) else "HEAVY_WALK" if heavy else "WALK"
    if values["type"] == "TURN":
        number = re.search(r"\b(45|90|180)\b", text)
        if number:
            values["turn_degrees"] = int(number.group(1))
        if re.search(r"\b(right|derecha)\b", text):
            values["turn_direction"] = "RIGHT"
        elif re.search(r"\b(left|izquierda)\b", text):
            values["turn_direction"] = "LEFT"
    if values["type"] == "HIT_REACTION":
        for token in ("LEFT", "RIGHT", "FORWARD", "BACKWARD"):
            if re.search(r"\b" + token.lower() + r"\b", text):
                values["impact_direction"] = token
                break
    if values["type"] == "START_STOP":
        values["transition"] = "STOP" if re.search(r"\b(stop|parada)\b", text) else "START"
    return values


def create_brief(intent: dict) -> dict:
    """Normalize explicit intent, preserving the origin of every chosen value.

    Natural-language intent is a small deterministic parser; explicit structured
    fields override its interpretation. Unknown/ambiguous types fail, never fall
    through to random key generation. Duration is represented by integer frame
    intervals and its quantization error is reported.
    """
    if isinstance(intent, dict) and intent.get("schema") == "production.animation_brief.v1":
        clean = {key: deepcopy(value) for key, value in intent.items() if key in _ALLOWED and value is not None}
        # Preserve style-only heavy requests when normalizing a stored brief.
        if "style" not in clean and intent.get("heavy"):
            clean["style"] = "HEAVY"
        validated = create_brief(clean)
        if not set(validated) <= set(intent):
            raise ValueError("INVALID_ANIMATION_BRIEF: normalized brief is incomplete")
        for key in ("frame_start", "frame_end", "fps", "type", "profile", "root_mode", "heavy", "dominant_side", "turn_direction", "forward_axis"):
            if intent.get(key) != validated[key]:
                raise ValueError("INVALID_ANIMATION_BRIEF: normalized " + key + " is inconsistent")
        provenance = intent.get("provenance")
        if not isinstance(provenance, dict) or any(not isinstance(row, dict) or row.get("source") not in
            {"user_requirement", "inferred_default", "optional_proposal"} for row in provenance.values()):
            raise ValueError("INVALID_ANIMATION_BRIEF: normalized provenance is missing or invalid")
        # A changed stored value cannot retain an old claim about what the user asked for.
        for key, row in provenance.items():
            if key in validated and row.get("value") != intent.get(key):
                raise ValueError("INVALID_ANIMATION_BRIEF: normalized provenance value changed for " + key)
        json.dumps(intent, allow_nan=False)
        return deepcopy(intent)
    if not isinstance(intent, dict) or set(intent) - _ALLOWED:
        raise ValueError("INVALID_ANIMATION_BRIEF: intent must be an object with supported fields")
    supplied = deepcopy(intent)
    parsed = _parse_intent(supplied["intent"]) if "intent" in supplied and not ("type" in supplied or "animation_type" in supplied) else {}
    if "type" in supplied and "animation_type" in supplied and supplied["type"].upper() != supplied["animation_type"].upper():
        raise ValueError("INVALID_ANIMATION_BRIEF: type and animation_type conflict")
    values = {**parsed, **supplied}
    if "animation_type" in values:
        values["type"] = values["animation_type"]
    if "duration_seconds" in values:
        if "duration_s" in values and values["duration_s"] != values["duration_seconds"]:
            raise ValueError("INVALID_ANIMATION_BRIEF: duration aliases conflict")
        values["duration_s"] = values["duration_seconds"]
    if "root_motion" in values:
        if type(values["root_motion"]) is not bool:
            raise ValueError("INVALID_ANIMATION_BRIEF: root_motion must be a boolean")
        alias = "ROOT_MOTION" if values["root_motion"] else "IN_PLACE"
        if "root_mode" in values and values["root_mode"].upper() != alias:
            raise ValueError("INVALID_ANIMATION_BRIEF: root options conflict")
        values["root_mode"] = alias
    if "type" not in values:
        raise ValueError("ANIMATION_TYPE_REQUIRED: explicit type or interpretable intent required")
    kind = _enum(values["type"], "type", TYPES)
    style = values.get("style", "")
    if style and (not isinstance(style, str) or style.upper() not in {"HEAVY", "STANDARD", "CUSTOM"}):
        raise ValueError("INVALID_ANIMATION_BRIEF: style must be HEAVY, STANDARD or CUSTOM")
    profile = _enum(values.get("profile", "HEAVY_HUMANOID" if style.upper() == "HEAVY" else "HUMAN"), "profile", PROFILES)
    heavy = profile in {"HEAVY_HUMANOID", "HEAVY_CREATURE"} or style.upper() == "HEAVY"
    defaults = {"IDLE": 4., "ATTACK": 2.4, "LOCOMOTION": 2., "TURN": 1.8, "HIT_REACTION": 1.8, "START_STOP": 2.}
    duration = _number(values.get("duration_s", defaults[kind]), "duration_s", 1, 10)
    fps = values.get("fps", 30)
    if type(fps) is not int or fps not in {30, 60}:
        raise ValueError("INVALID_ANIMATION_BRIEF: fps must be 30 or 60")
    start = values.get("frame_start", 1)
    if type(start) is not int or not -100000 <= start <= 100000:
        raise ValueError("INVALID_ANIMATION_BRIEF: frame_start must be a bounded integer")
    frame_intervals = int(math.floor(duration * fps + .5))
    root_mode = _enum(values.get("root_mode", "IN_PLACE"), "root_mode", {"IN_PLACE", "ROOT_MOTION"})
    stride = _number(values.get("stride_length_m", .45 if heavy else .6), "stride_length_m", .01, 5)
    cycle = _number(values.get("cycle_duration_s", duration), "cycle_duration_s", .5, 10)
    if kind == "LOCOMOTION" and abs(cycle - duration) > .5 / fps:
        raise ValueError("INVALID_ANIMATION_BRIEF: initial M6 locomotion plans exactly one cycle; cycle_duration_s must equal duration_s")
    default_speed = stride / cycle if kind == "LOCOMOTION" else .25 if kind == "START_STOP" else 0.
    speed = _number(values.get("speed_m_s", default_speed), "speed_m_s", 0, 15)
    distance = _number(values.get("forward_distance_m", speed * duration), "forward_distance_m", 0, 20)
    if "forward_distance_m" in values and "speed_m_s" in values and kind == "LOCOMOTION" and abs(distance - speed*duration) > 1e-7:
        raise ValueError("INVALID_ANIMATION_BRIEF: locomotion distance/speed/duration disagree")
    if "forward_distance_m" in values and "speed_m_s" not in values:
        speed = distance / duration
    turn = values.get("turn_degrees", 90)
    if isinstance(turn, bool) or turn not in {45, 90, 180}:
        raise ValueError("INVALID_ANIMATION_BRIEF: initial turn_degrees supports 45, 90 or 180")
    mass = values.get("mass_asymmetry", {"left": 1., "right": 2.} if profile == "ASYMMETRIC_CREATURE" else {"left": 1., "right": 1.})
    if not isinstance(mass, dict) or set(mass) != {"left", "right"}:
        raise ValueError("INVALID_ANIMATION_BRIEF: mass_asymmetry requires left and right relative body masses")
    mass = {side: _number(mass[side], "mass_asymmetry." + side, .1, 10) for side in ("left", "right")}
    contacts = values.get("contact_requirements", [])
    if not isinstance(contacts, list) or len(contacts) > 16 or any(not isinstance(item, dict) for item in contacts):
        raise ValueError("INVALID_ANIMATION_BRIEF: contact_requirements must contain at most 16 objects")
    if len(json.dumps(contacts, allow_nan=False)) > 16000:
        raise ValueError("CAPACITY_EXCEEDED: contact requirements exceed 16000 bytes")
    custom = values.get("custom_pose_plan")
    if custom is not None and (not isinstance(custom, list) or not 2 <= len(custom) <= 64):
        raise ValueError("INVALID_ANIMATION_BRIEF: custom_pose_plan must contain 2..64 semantic poses")
    brief = {"schema": "production.animation_brief.v1", "type": kind, "profile": profile, "heavy": heavy,
        "fps": fps, "duration_s": duration, "actual_duration_s": frame_intervals/fps,
        "frame_start": start, "frame_end": start+frame_intervals, "root_mode": root_mode,
        "speed_m_s": speed, "forward_distance_m": distance, "stride_length_m": stride, "cycle_duration_s": cycle,
        "attack_type": _enum(values.get("attack_type", "OVERHEAD"), "attack_type", ATTACK_TYPES),
        "locomotion_type": _enum(values.get("locomotion_type", "HEAVY_WALK" if heavy else "WALK"), "locomotion_type", LOCOMOTION_TYPES),
        "dominant_side": _enum(values.get("dominant_side", "RIGHT"), "dominant_side", {"LEFT", "RIGHT"}).lower(),
        "turn_degrees": float(turn), "turn_direction": _enum(values.get("turn_direction", "RIGHT"), "turn_direction", {"LEFT", "RIGHT"}),
        "impact_direction": _direction(values.get("impact_direction", "BACKWARD")),
        "severity": _number(values.get("severity", .5), "severity", 0, 1),
        "strength": _number(values.get("strength", 1.), "strength", 0, 2), "mass_asymmetry": mass,
        "contact_requirements": deepcopy(contacts), "phase_fractions": deepcopy(values.get("phase_fractions")),
        "transition": _enum(values.get("transition", "START"), "transition", {"START", "STOP"}),
        "forward_axis": _enum(values.get("forward_axis", "+Y"), "forward_axis", {"+Y", "-Y"}),
        "stance_width_m": _number(values.get("stance_width_m", .3), "stance_width_m", .01, 3),
        "step_height_m": _number(values.get("step_height_m", .07 if heavy else .12), "step_height_m", 0, 1),
        "max_bone_frame_samples": int(_number(values.get("max_bone_frame_samples", 100000), "max_bone_frame_samples", 1, 200000)),
        "max_keyframes": int(_number(values.get("max_keyframes", 100000), "max_keyframes", 1, 100000)),
        "custom_pose_plan": deepcopy(custom), "apply": False}
    provenance = {}
    aliases = {"type": ("animation_type",), "duration_s": ("duration_seconds",), "root_mode": ("root_motion",)}
    for key, value in brief.items():
        if key in {"schema", "apply", "actual_duration_s", "frame_end", "heavy", "custom_pose_plan"}:
            continue
        source = "user_requirement" if key in supplied or any(a in supplied for a in aliases.get(key, ())) else "inferred_default"
        if key in parsed and key not in supplied:
            source = "user_requirement"
        provenance[key] = {"source": source, "value": deepcopy(value)}
        if key in parsed and key not in supplied:
            provenance[key]["interpretation"] = "deterministic interpretation of supplied intent"
        if key == "speed_m_s" and "forward_distance_m" in supplied and "speed_m_s" not in supplied:
            provenance[key]["derived_from"] = ["forward_distance_m", "duration_s"]
        if key == "profile" and "style" in supplied and "profile" not in supplied:
            provenance[key]["derived_from"] = ["style"]
    brief.update(provenance=provenance, user_requirements=supplied, optional_proposals=[],
        units={"vectors": "metres", "angles": "degrees", "time": "seconds", "body_axes": ["lateral_left", "forward", "up"],
               "rotations": "right-handed around body axes; not bone-local Euler channels"},
        quantization_error_s=frame_intervals/fps-duration,
        limitations=["Deterministic body-animation heuristics; not artistic certification or biomechanics simulation.",
                     "Rig controls, reach, constraints and contact feasibility must be validated before keys."])
    if kind == "LOCOMOTION" and root_mode == "IN_PLACE":
        brief["limitations"].append("Initial in-place locomotion is a planted-foot step/march cycle, not treadmill translation; speed is reference intent only.")
    if kind == "LOCOMOTION" and brief["locomotion_type"] == "RUN":
        brief["limitations"].append("RUN is limited to planned alternating support and flight intent on a validated biped; no physics simulation.")
    return brief


def _smooth(value):
    t = max(0., min(1., value))
    return t*t*(3.-2.*t)


def _phase_points(brief):
    kind, heavy = brief["type"], brief["heavy"]
    points = {
        "ATTACK": [("NEUTRAL", 0.), ("ANTICIPATION", .32 if heavy else .18), ("COMMITMENT", .48 if heavy else .36),
                   ("STRIKE", .58 if heavy else .52), ("IMPACT", .62 if heavy else .58), ("FOLLOW_THROUGH", .70 if heavy else .76), ("RECOVERY", 1.)],
        "IDLE": [("BASE_POSE", 0.), ("WEIGHT_SHIFT", .27), ("SECONDARY_BODY_MOTION", .61), ("SETTLE", .83), ("RETURN", 1.)],
        "TURN": [("NEUTRAL", 0.), ("WEIGHT_SHIFT", .18), ("STEP_FIRST", .40), ("STEP_SECOND", .70), ("RECOVERY", 1.)],
        "HIT_REACTION": [("NEUTRAL", 0.), ("IMPACT", .06), ("ABSORPTION", .24 if heavy else .18),
                         ("DISPLACEMENT", .47 if heavy else .36), ("RECOVERY", 1.)],
        "START_STOP": [("NEUTRAL", 0.), ("WEIGHT_SHIFT", .22), ("TRANSITION", .5), ("COMMITMENT", .76), ("RECOVERY", 1.)],
    }
    if kind == "LOCOMOTION":
        points = {**points, "LOCOMOTION": [("CONTACT", 0.), ("DOWN", .125), ("PASSING", .25), ("UP", .375),
            ("CONTACT_OPPOSITE", .5), ("DOWN_OPPOSITE", .625), ("PASSING_OPPOSITE", .75), ("UP_OPPOSITE", .875), ("CONTACT", 1.)]}
    result = points[kind]
    custom = brief.get("phase_fractions")
    if custom is not None:
        names = {name for name, value in result}
        if not isinstance(custom, dict) or set(custom) != names or kind == "LOCOMOTION":
            raise ValueError("INVALID_POSE_PLAN: phase_fractions must provide each distinct phase; locomotion preserves its cycle event model")
        result = [(name, _number(custom[name], "phase fraction", 0, 1)) for name, value in result]
        if result[0][1] != 0 or result[-1][1] != 1 or any(b[1] <= a[1] for a, b in zip(result, result[1:])):
            raise ValueError("INVALID_POSE_PLAN: phase fractions must increase strictly from zero to one")
    return result


def _neutral():
    return {"root_translation_m": [0., 0., 0.], "root_yaw_deg": 0., "pelvis_offset_m": [0., 0., 0.],
        "pelvis_pitch_deg": 0., "pelvis_roll_deg": 0., "pelvis_yaw_deg": 0.,
        "torso_pitch_deg": 0., "torso_roll_deg": 0., "torso_yaw_deg": 0., "head_pitch_deg": 0., "head_yaw_deg": 0.,
        "arm_swing_deg": {"left": 0., "right": 0.}, "arm_abduction_deg": {"left": 0., "right": 0.},
        "elbow_flex_deg": {"left": 8., "right": 8.},
        "feet": {side: {"offset_m": [0., 0., 0.], "anchor_m": [0., 0., 0.], "contact": True,
            "contact_phase": "FOOT_FLAT", "knee_flex_deg": 0., "space": "BODY_WORLD_REST_METRES"} for side in ("left", "right")}}


def _validate_semantic(value):
    reference = _neutral()
    if not isinstance(value, dict) or set(value) != set(reference):
        raise ValueError("INVALID_POSE_PLAN: custom semantic pose must implement the complete documented contract")
    for key, example in reference.items():
        raw = value[key]
        if key == "feet":
            if not isinstance(raw, dict) or set(raw) != {"left", "right"}:
                raise ValueError("INVALID_POSE_PLAN: feet require left and right semantic effectors")
            for side in ("left", "right"):
                foot = raw[side]
                if not isinstance(foot, dict) or set(foot) != set(reference["feet"][side]):
                    raise ValueError("INVALID_POSE_PLAN: invalid foot semantic contract")
                if type(foot["contact"]) is not bool or foot["contact_phase"] not in CONTACT_PHASES or foot["space"] != "BODY_WORLD_REST_METRES":
                    raise ValueError("INVALID_POSE_PLAN: invalid foot contact intent or space")
                _number(foot["knee_flex_deg"], "knee_flex_deg", 0, 180)
                if not isinstance(foot["offset_m"], list) or len(foot["offset_m"]) != 3:
                    raise ValueError("INVALID_POSE_PLAN: foot offset requires XYZ metres")
                for coordinate in foot["offset_m"]:
                    _number(coordinate, "foot coordinate", -20, 20)
                if foot["contact"] and foot["anchor_m"] != foot["offset_m"] or not foot["contact"] and foot["anchor_m"] is not None:
                    raise ValueError("INVALID_POSE_PLAN: planted foot needs an equal anchor; swing has no anchor")
        elif isinstance(example, list):
            if not isinstance(raw, list) or len(raw) != 3:
                raise ValueError("INVALID_POSE_PLAN: vector requires XYZ metres")
            for coordinate in raw:
                _number(coordinate, key, -20, 20)
        elif isinstance(example, dict):
            if not isinstance(raw, dict) or set(raw) != {"left", "right"}:
                raise ValueError("INVALID_POSE_PLAN: bilateral angle requires left and right")
            for angle in raw.values():
                _number(angle, key, -360, 360)
        else:
            _number(raw, key, -360, 360)


def _foot_cycle(brief, fraction, side):
    # World-space stance anchor is constant until the swing, even while root advances.
    off = 0. if side == "left" else .5
    phase = (fraction + off) % 1.
    cycle = math.floor(fraction + off + 1e-10)
    flight = brief["locomotion_type"] == "RUN"
    duty = .45 if flight else .6
    delta = brief["forward_distance_m"] if brief["root_mode"] == "ROOT_MOTION" else 0.
    anchor = delta * (cycle - off + duty/2.)
    if phase < duty:
        contact_phase = "HEEL_STRIKE" if phase < .04 else "FOOT_FLAT" if phase < .15 else "MID_STANCE" if phase < duty-.12 else "HEEL_RISE"
        return {"offset_m": [0., anchor, 0.], "anchor_m": [0., anchor, 0.], "contact": True,
            "contact_phase": contact_phase, "knee_flex_deg": 2. + 5.*math.sin(math.pi*phase/duty), "space": "BODY_WORLD_REST_METRES"}
    t = (phase-duty)/(1.-duty)
    forward = anchor + delta*_smooth(t)
    if brief["root_mode"] == "IN_PLACE":
        forward = brief["stride_length_m"]*.25*math.sin(math.pi*t)**2
    height = brief["step_height_m"]*math.sin(math.pi*t)**2
    return {"offset_m": [0., forward, height], "anchor_m": None, "contact": False, "contact_phase": "TOE_OFF" if t < .08 else "SWING",
        "knee_flex_deg": 5.+35.*math.sin(math.pi*t), "space": "BODY_WORLD_REST_METRES"}


def _attack_semantic(brief, phase):
    s = _neutral(); side = brief["dominant_side"]; other = "left" if side == "right" else "right"
    sign = -1. if side == "right" else 1.; power = brief["strength"]
    levels = {"NEUTRAL": (0., 0., 0.), "ANTICIPATION": (-10., 30., -.025), "COMMITMENT": (-3., 65., .025),
              "STRIKE": (12., 110., .05), "IMPACT": (19., 125., .06), "FOLLOW_THROUGH": (11., 145., .04), "RECOVERY": (0., 0., 0.)}
    forward_lean, arm, commitment = levels[phase]
    s["torso_pitch_deg"] = -forward_lean*power
    s["pelvis_offset_m"][1] = commitment*power*(1.4 if brief["heavy"] else 1.)
    # Pelvis commits ahead of the torso: mass is represented by phase relationships.
    s["pelvis_pitch_deg"] = -max(0., forward_lean)*.25*power
    s["torso_yaw_deg"] = sign*(-18. if phase == "ANTICIPATION" else 12. if phase in {"STRIKE", "IMPACT"} else 5. if phase == "FOLLOW_THROUGH" else 0.)*power
    kind = brief["attack_type"]
    if kind in {"OVERHEAD", "SLAM", "CUSTOM_BODY_ATTACK"}:
        # Raising then swinging through, not a generic sine motion.
        s["arm_swing_deg"][side] = (150. if phase == "COMMITMENT" else 165. if phase == "ANTICIPATION" else 70. if phase in {"STRIKE", "IMPACT"} else 20. if phase == "FOLLOW_THROUGH" else 0.)*power
    elif kind == "THRUST":
        s["arm_swing_deg"][side] = min(90., arm)*power
    else:
        s["arm_swing_deg"][side] = min(100., arm)*power
        s["arm_abduction_deg"][side] = (55. if phase == "ANTICIPATION" else 25. if phase in {"STRIKE", "IMPACT"} else 0.)*power
    s["elbow_flex_deg"][side] = (75. if phase == "ANTICIPATION" else 45. if phase == "COMMITMENT" else 12. if phase in {"STRIKE", "IMPACT"} else 25. if phase == "FOLLOW_THROUGH" else 8.)*power
    s["arm_swing_deg"][other] = -min(30., arm*.2)*power
    s["head_pitch_deg"] = -s["torso_pitch_deg"]*.35
    return s


def _semantic(brief, phase, fraction):
    kind = brief["type"]; s = _neutral()
    if kind == "ATTACK":
        s = _attack_semantic(brief, phase)
    elif kind == "LOCOMOTION":
        s["feet"] = {side: _foot_cycle(brief, fraction, side) for side in ("left", "right")}
        body_phase = math.sin(4.*math.pi*fraction)
        s["pelvis_offset_m"][2] = -.02*(1.-math.cos(4.*math.pi*fraction))
        s["pelvis_roll_deg"] = 2.*math.sin(2.*math.pi*fraction)
        s["torso_pitch_deg"] = -5. if brief["heavy"] else -2.
        s["torso_yaw_deg"] = -3.*math.sin(2.*math.pi*fraction)
        for side, sign in (("left", 1.), ("right", -1.)):
            s["arm_swing_deg"][side] = sign*(12. if brief["heavy"] else 20.)*math.sin(2.*math.pi*fraction)
            # Quartic periodic flexion preserves the event range and flattens
            # velocity/acceleration near the seam for sparse spline sampling.
            s["elbow_flex_deg"][side] = 12.+body_phase**4*4.
    elif kind == "IDLE":
        levels = {"BASE_POSE": (0., 0., 0.), "WEIGHT_SHIFT": (.018, -.8, .3), "SECONDARY_BODY_MOTION": (-.009, -1.8, -.6),
                  "SETTLE": (.004, -.4, .2), "RETURN": (0., 0., 0.)}
        lateral, lean, roll = levels[phase]
        s["pelvis_offset_m"][0] = lateral*brief["strength"]
        s["torso_pitch_deg"] = lean*brief["strength"]; s["torso_roll_deg"] = roll*brief["strength"]
        s["head_pitch_deg"] = -lean*.2; s["arm_swing_deg"] = {"left": lean*.3, "right": -lean*.2}
    elif kind == "HIT_REACTION":
        levels = {"NEUTRAL": 0., "IMPACT": .15, "ABSORPTION": 1., "DISPLACEMENT": .7, "RECOVERY": 0.}
        amount = levels[phase]*brief["severity"]*brief["strength"]
        direction = brief["impact_direction"]
        s["pelvis_offset_m"] = [direction[0]*.05*amount, direction[1]*.08*amount, -.035*amount]
        s["torso_pitch_deg"] = -direction[1]*18.*amount; s["torso_roll_deg"] = direction[0]*12.*amount
        s["head_pitch_deg"] = -s["torso_pitch_deg"]*.25
        s["arm_swing_deg"] = {"left": -direction[1]*12.*amount, "right": -direction[1]*12.*amount}
    elif kind == "TURN":
        yaw = brief["turn_degrees"]*(1. if brief["turn_direction"] == "RIGHT" else -1.)
        s["root_yaw_deg"] = yaw*_smooth(fraction)
        s["torso_yaw_deg"] = (-8. if brief["turn_direction"] == "RIGHT" else 8.)*math.sin(math.pi*fraction)
        s["pelvis_offset_m"][2] = -.02*math.sin(math.pi*fraction)**2
        for side, begin, end, sign in (("left", .15, .48, 1.), ("right", .48, .82, -1.)):
            t = max(0., min(1., (fraction-begin)/(end-begin)))
            angle = math.radians(yaw)*_smooth(t)
            x = sign*brief["stance_width_m"]*.5
            offset = [x*(math.cos(angle)-1.), x*math.sin(angle), brief["step_height_m"]*math.sin(math.pi*t)**2]
            contact = not begin < fraction < end
            s["feet"][side].update(offset_m=offset, anchor_m=offset if contact else None, contact=contact,
                                   contact_phase="FOOT_FLAT" if contact else "SWING", knee_flex_deg=20.*math.sin(math.pi*t))
    elif kind == "START_STOP":
        amount = _smooth(fraction) if brief["transition"] == "START" else 1.-_smooth(fraction)
        s["torso_pitch_deg"] = -8.*amount
        s["pelvis_offset_m"][1] = .02*math.sin(math.pi*fraction)
        s["arm_swing_deg"] = {"left": 12.*amount*math.sin(2.*math.pi*fraction), "right": -12.*amount*math.sin(2.*math.pi*fraction)}
        for side in ("left", "right"):
            s["feet"][side] = _foot_cycle({**brief, "locomotion_type": "WALK"}, fraction, side)
    if brief["root_mode"] == "ROOT_MOTION":
        if kind == "LOCOMOTION":
            progress = fraction
        elif kind == "START_STOP":
            # Integrals of smoothstep velocity: continuous velocity at transition endpoints.
            progress = fraction**3-.5*fraction**4 if brief["transition"] == "START" else fraction-fraction**3+.5*fraction**4
        else:
            progress = _smooth(fraction)
        s["root_translation_m"][1] = brief["forward_distance_m"]*progress
    mass = brief["mass_asymmetry"]
    imbalance = (mass["right"]-mass["left"])/(mass["left"]+mass["right"])
    activity = math.sin(math.pi*fraction)**2
    if kind == "LOCOMOTION":
        activity = 1.
    # Deliberate body mass is preserved; only movement compensation is proposed.
    s["pelvis_offset_m"][0] += imbalance*.035*activity
    s["torso_roll_deg"] += imbalance*7.*activity
    s["torso_yaw_deg"] -= imbalance*5.*activity
    if imbalance:
        large = "right" if imbalance > 0 else "left"
        s["arm_swing_deg"][large] *= 1.-.25*abs(imbalance)
    return s


def _interpolate(first, second, t):
    if isinstance(first, dict):
        return {key: _interpolate(value, second[key], t) for key, value in first.items()}
    if isinstance(first, list):
        return [_interpolate(a, b, t) for a, b in zip(first, second)]
    if isinstance(first, (int, float)) and not isinstance(first, bool):
        return first+(second-first)*t
    return deepcopy(first if t < .5 else second)


def _phase_objective(phase):
    return {"ANTICIPATION": "Prepare support and mass before the main action", "COMMITMENT": "Lead with pelvis and coordinated torso",
        "IMPACT": "Preserve a short readable main event", "FOLLOW_THROUGH": "Continue established momentum before recovery",
        "RECOVERY": "Recover support and avoid instantaneous reversal", "CONTACT": "Establish reviewed foot support",
        "WEIGHT_SHIFT": "Move body mass over the planned support", "ABSORPTION": "Absorb impact before displacement",
        "DISPLACEMENT": "Displace body gradually in the specified impact direction"}.get(phase, "Maintain declared phase, support and readable body intent")


def _contact_intervals(poses):
    intervals = []
    for side in ("left", "right"):
        active = None
        for index, pose in enumerate(poses):
            foot = pose["semantic"]["feet"][side]
            if foot["contact"]:
                if active is None:
                    active = {"side": side, "frame_start": pose["frame"], "frame_end": pose["frame"],
                              "phase": foot["contact_phase"], "anchor_m": deepcopy(foot["anchor_m"]), "intent": "PLANTED",
                              "space": "BODY_WORLD_REST_METRES", "requires_geometric_confirmation": True}
                else:
                    active["frame_end"] = pose["frame"]
            if active is not None and (not foot["contact"] or index == len(poses)-1):
                intervals.append(active); active = None
    return intervals


def plan_poses(brief: dict, compatibility: dict | None = None) -> dict:
    """Create semantic key poses and meaningful, bounded breakdown intents.

    This plan is entirely non-mutating. Blocking poses are CONSTANT; breakdowns
    lead pelvis before torso and introduce configured swing arcs. Contacts in
    the plan are intentions; installed Blender QA must confirm them.
    """
    if not isinstance(brief, dict):
        raise ValueError("INVALID_POSE_PLAN: brief must be an object")
    brief = create_brief(brief)
    if compatibility is not None and not isinstance(compatibility, dict):
        raise ValueError("INVALID_POSE_PLAN: compatibility must be an object")
    required = {"type", "profile", "fps", "duration_s", "frame_start", "frame_end", "root_mode", "mass_asymmetry"}
    if not required <= set(brief) or brief["type"] not in TYPES or brief["profile"] not in PROFILES:
        raise ValueError("INVALID_POSE_PLAN: invalid normalized brief")
    points = _phase_points(brief); start = brief["frame_start"]; intervals = brief["frame_end"]-start
    poses = []
    if brief.get("custom_pose_plan"):
        for raw in brief["custom_pose_plan"]:
            if not isinstance(raw, dict) or set(raw)-{"fraction", "phase", "semantic"} or not {"fraction", "phase", "semantic"} <= set(raw):
                raise ValueError("INVALID_POSE_PLAN: custom poses require fraction, phase and semantic")
            fraction = _number(raw["fraction"], "custom pose fraction", 0, 1)
            if not isinstance(raw["phase"], str) or not raw["phase"] or not isinstance(raw["semantic"], dict):
                raise ValueError("INVALID_POSE_PLAN: invalid custom semantic pose")
            _validate_semantic(raw["semantic"])
            frame = start+round(intervals*fraction)
            poses.append({"frame": frame, "time_s": (frame-start)/brief["fps"],
                "phase": raw["phase"], "fraction": fraction, "semantic": deepcopy(raw["semantic"]), "interpolation": "CONSTANT"})
        if poses[0]["fraction"] != 0 or poses[-1]["fraction"] != 1:
            raise ValueError("INVALID_POSE_PLAN: custom pose plan must span the complete interval")
    else:
        for phase, fraction in points:
            frame = start+round(intervals*fraction)
            poses.append({"frame": frame, "time_s": (frame-start)/brief["fps"],
                "phase": phase, "fraction": fraction, "semantic": _semantic(brief, phase, (frame-start)/intervals), "interpolation": "CONSTANT",
                "objective": _phase_objective(phase), "support_state": "REVALIDATE_PLANNED_CONTACTS",
                "silhouette_notes": ["Separate dominant effector from torso in reviewed views; no automatic artistic score"]})
    if any(b["frame"] <= a["frame"] for a, b in zip(poses, poses[1:])):
        raise ValueError("INVALID_POSE_PLAN: key phases collide after frame quantization; lengthen duration or adjust phases")
    breakdowns = []
    for a, b in zip(poses, poses[1:]):
        frame = (a["frame"]+b["frame"]+1)//2
        if frame in {a["frame"], b["frame"]}:
            continue
        fraction = (frame-start)/intervals
        if brief["type"] in {"LOCOMOTION", "TURN", "START_STOP"}:
            semantic = _semantic(brief, a["phase"], fraction)
        else:
            semantic = _interpolate(a["semantic"], b["semantic"], .5)
            # Deliberate lead/lag, not blind equal-channel linear interpolation.
            semantic["pelvis_offset_m"] = _interpolate(a["semantic"]["pelvis_offset_m"], b["semantic"]["pelvis_offset_m"], .65)
            semantic["torso_pitch_deg"] = _interpolate(a["semantic"]["torso_pitch_deg"], b["semantic"]["torso_pitch_deg"], .35)
        breakdowns.append({"frame": frame, "time_s": (frame-start)/brief["fps"], "fraction": fraction,
            "phase": "BREAKDOWN_"+a["phase"]+"_TO_"+b["phase"], "between": [a["phase"], b["phase"]],
            "semantic": semantic, "interpolation": "LINEAR", "objective": "Pelvis leads torso; preserve support anchors and intended arc"})
    dense = sorted(poses+breakdowns, key=lambda pose: pose["frame"])
    contacts = _contact_intervals(dense)
    phases = [{"phase": a["phase"], "frame_start": a["frame"], "frame_end": b["frame"],
               "duration_s": (b["frame"]-a["frame"])/brief["fps"], "objective": _phase_objective(a["phase"])} for a, b in zip(poses, poses[1:])]
    bones = (compatibility or {}).get("bone_count", 24)
    if type(bones) is not int or not 1 <= bones <= 2048:
        raise ValueError("INVALID_POSE_PLAN: compatibility bone_count must be 1..2048")
    controls = (compatibility or {}).get("controlled_bone_count", min(bones, len(SEMANTIC_ROLES)+4))
    if type(controls) is not int or not 1 <= controls <= bones:
        raise ValueError("INVALID_POSE_PLAN: invalid controlled_bone_count")
    verification_frames = intervals+1+len(dense)*2
    samples = bones*verification_frames
    key_count = controls*7*len(dense)+4*len(dense)
    capacity = {"bone_count": bones, "controlled_bone_count": controls, "frame_count": intervals+1,
        "key_pose_count": len(poses), "breakdown_count": len(breakdowns), "estimated_keyframes": key_count,
        "estimated_samples_per_pass": samples, "estimated_samples": samples*3,
        "estimated_memory_bytes": int(samples*343.1), "configured_limit": brief["max_bone_frame_samples"],
        "max_keyframes": brief["max_keyframes"], "supported": samples <= brief["max_bone_frame_samples"] and key_count <= brief["max_keyframes"],
        "estimate_basis": "Conservative bone/frame and channel/key count; 343.1 B per sampled bone/frame from M3.1. Actual rig may cost more.",
        "estimated_runtime_class": "under_10s" if samples*3/30769.2 < 10 else "under_60s" if samples*3/30769.2 < 60 else "timeout_risk"}
    errors = []
    if not capacity["supported"]:
        errors.append("CAPACITY_EXCEEDED: requested generation/QA exceeds configured samples or key budget")
    if compatibility is not None and compatibility.get("supported") is False:
        errors.append("UNSUPPORTED_GENERATION_RIG: compatibility refused generation")
    holds = [{"phase": p["phase"], "frame_start": p["frame"], "frame_end": min(p["frame"]+1, brief["frame_end"]),
              "reason": "Blocking event hold; spline must explicitly preserve event timing"} for p in poses if p["phase"] in {"NEUTRAL", "IMPACT", "BASE_POSE"}]
    result = {"schema": "production.animation_pose_plan.v1", "apply": False, "supported": not errors, "errors": errors,
        "brief": brief, "poses": poses, "breakdowns": breakdowns, "phases": phases, "holds": holds,
        "contact_intervals": contacts, "capacity": capacity, "provenance": deepcopy(brief["provenance"]),
        "required_roles": list(SEMANTIC_ROLES), "rig_validation": "required" if compatibility is None else "supplied_report_revalidated_at_execution",
        "pipeline": ["BRIEF", "COMPATIBILITY", "POSE_PLAN", "NEW_CANDIDATE_ACTION", "BLOCKING", "BLOCKING_QA", "VISUAL_BLOCKING_REVIEW",
            "BREAKDOWNS", "TIMING", "SPACING", "ARCS", "BODY_MECHANICS", "ROOT_CONTACTS", "SPLINE", "POLISH", "TECHNICAL_QA", "TEMPORAL_VISUAL_QA", "MAX_3_REFINEMENTS", "ACCEPT_OR_REJECT"],
        "root_intent": {"mode": brief["root_mode"], "expected_translation_m": deepcopy(poses[-1]["semantic"]["root_translation_m"]),
                        "expected_yaw_deg": poses[-1]["semantic"]["root_yaw_deg"]},
        "warnings": deepcopy(brief["limitations"])+["Planned contacts are intent; geometric confirmation and numeric/visual QA are mandatory."]}
    encoded = len(json.dumps(result, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    result["capacity"]["payload_bytes"] = encoded
    if encoded > 200000:
        result["supported"] = result["capacity"]["supported"] = False
        result["errors"].append("CAPACITY_EXCEEDED: pose plan exceeds safe-mode 200000-byte payload")
    return result


def analyze_timing(plan: dict) -> dict:
    """Measure authored phase allocation; heavy comparisons are explanatory only."""
    phases = deepcopy(plan["phases"]); total = sum(p["duration_s"] for p in phases)
    return {"schema": "production.animation_timing_plan.v1", "duration_s": total, "phases": phases,
        "fractions": {p["phase"]: p["duration_s"]/total if total else 0. for p in phases},
        "impact_frames": [p["frame"] for p in plan["poses"] if p["phase"] == "IMPACT"],
        "warnings": ["Phase duration is not perceived weight; inspect leading mass, momentum and recovery in temporal preview."]}


def trajectory_metrics(frames, positions_m, fps=30):
    """Compact actual-trajectory spacing/arcs metrics; no circular-arc assumption."""
    if not isinstance(frames, (list, tuple)) or len(frames) != len(positions_m) or len(frames) < 2:
        raise ValueError("INVALID_TRAJECTORY: equal arrays with at least two samples required")
    fps = _number(fps, "fps", 1, 240)
    points = [[_number(v, "trajectory coordinate", -1e9, 1e9) for v in p] for p in positions_m]
    if any(len(p) != 3 for p in points) or any(not math.isfinite(float(f)) for f in frames) or any(b <= a for a, b in zip(frames, frames[1:])):
        raise ValueError("INVALID_TRAJECTORY: finite increasing frames and XYZ points required")
    vectors = [[b[i]-a[i] for i in range(3)] for a, b in zip(points, points[1:])]
    distances = [math.sqrt(sum(v*v for v in row)) for row in vectors]
    dt = [(b-a)/fps for a, b in zip(frames, frames[1:])]
    speeds = [d/t for d, t in zip(distances, dt)]
    accelerations = [abs(b-a)/((dt[i]+dt[i+1])/2.) for i, (a, b) in enumerate(zip(speeds, speeds[1:]))]
    turns = []
    for a, b in zip(vectors, vectors[1:]):
        la = math.sqrt(sum(v*v for v in a)); lb = math.sqrt(sum(v*v for v in b))
        turns.append(math.degrees(math.acos(max(-1., min(1., sum(x*y for x, y in zip(a, b))/(la*lb))))) if la*lb > 1e-12 else None)
    chord = math.dist(points[0], points[-1]); length = sum(distances)
    return {"samples": len(points), "distance_m": length, "net_displacement_m": chord, "max_speed_m_s": max(speeds),
        "max_acceleration_m_s2": max(accelerations, default=0.), "max_turn_degrees": max((a for a in turns if a is not None), default=0.),
        "path_to_chord_ratio": length/chord if chord > 1e-9 else None, "stationary_segments": sum(d < 1e-9 for d in distances),
        "warnings": ["Sampled trajectory changes are indicators; intentional sharp impacts must be interpreted using phases and contacts."]}
