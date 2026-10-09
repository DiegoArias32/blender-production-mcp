"""Strict host-side physics briefs and bounded starting parameter proposals.

Profiles below are calibration starting points for controlled fixtures. They are
not universal material properties and do not certify physical or artistic realism.
Native cloth field names were inspected in Blender 5.2.1 RNA before introduction.
"""
from __future__ import annotations

import math
import re

SUBSYSTEMS = ('CLOTH', 'HAIR', 'SPRING_BONE', 'ACCESSORY', 'CHAIN', 'CABLE', 'SOFT')
PROVENANCE = ('USER_REQUIREMENT', 'INFERRED_DEFAULT', 'PROPOSED_PARAMETER')
SPRING_DEFAULTS = {
    'mass': 1.0, 'stiffness': 40.0, 'damping': 8.0, 'substeps': 8,
    'gravity_influence': 0.0, 'lag_s': 0.02, 'max_displacement_m': 0.1,
    'max_angle_deg': 30.0, 'excitation_scale': 1.0,
}
CLOTH_DEFAULTS = {
    'mass': 0.3, 'tension_stiffness': 15.0, 'compression_stiffness': 15.0,
    'shear_stiffness': 5.0, 'bending_stiffness': 0.5, 'tension_damping': 5.0,
    'compression_damping': 5.0, 'shear_damping': 5.0, 'bending_damping': 0.5,
    'air_damping': 1.0, 'quality': 8, 'pin_stiffness': 1.0,
    'use_collision': True, 'distance_min': 0.015, 'collision_quality': 3,
    'use_self_collision': False, 'self_distance_min': 0.015, 'self_friction': 5.0,
}
CLOTH_PROFILES = {
    'LIGHT_FABRIC': dict(CLOTH_DEFAULTS, mass=0.15, bending_stiffness=0.2),
    'MEDIUM_FABRIC': dict(CLOTH_DEFAULTS),
    'HEAVY_FABRIC': dict(CLOTH_DEFAULTS, mass=0.6, bending_stiffness=1.0, air_damping=2.0),
    'LEATHER_LIKE': dict(CLOTH_DEFAULTS, mass=0.45, tension_stiffness=30.0,
                         compression_stiffness=30.0, shear_stiffness=15.0, bending_stiffness=5.0),
    'STIFF_CLOTH': dict(CLOTH_DEFAULTS, tension_stiffness=50.0,
                       compression_stiffness=50.0, shear_stiffness=25.0, bending_stiffness=10.0),
    'CUSTOM': dict(CLOTH_DEFAULTS),
}
QA_DEFAULTS = {
    'max_displacement_m': 0.5, 'max_velocity_m_s': 10.0,
    'max_acceleration_m_s2': 200.0, 'max_penetration_m': 0.01,
    'max_root_detachment_m': 1e-6, 'max_stretch_ratio': 1.25,
    'max_inverted_faces': 0, 'min_area_ratio': 0.05, 'max_extent_ratio': 4.0,
    'settling_tolerance_m': 0.001,
}


def number(value, field, minimum=None, maximum=None, integer=False):
    """Accept only finite JSON numbers, explicitly excluding bool."""
    try:
        finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        raise ValueError('PHYSICS_NUMBER_INVALID: ' + field)
    if integer and int(value) != value:
        raise ValueError('PHYSICS_INTEGER_REQUIRED: ' + field)
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError('PHYSICS_RANGE_INVALID: ' + field)
    return int(value) if integer else float(value)


def vector(value, field, size=3):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError('PHYSICS_VECTOR_INVALID: ' + field)
    return [number(v, field) for v in value]


def _mapping(value, field):
    if not isinstance(value, dict):
        raise ValueError('PHYSICS_OBJECT_REQUIRED: ' + field)
    return dict(value)


def _known(values, allowed, field):
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError('PHYSICS_UNKNOWN_FIELD: ' + field + '.' + sorted(unknown)[0])


def normalize_settings(subsystem, settings=None, profile=None, fps=30):
    """Normalize one supported configuration; reject unused or invented fields."""
    if subsystem not in SUBSYSTEMS:
        raise ValueError('PHYSICS_SUBSYSTEM_UNSUPPORTED: ' + str(subsystem))
    values = _mapping(settings if settings is not None else {}, 'settings')
    if subsystem == 'CLOTH':
        profile = profile or 'MEDIUM_FABRIC'
        if profile not in CLOTH_PROFILES:
            raise ValueError('PHYSICS_PROFILE_UNSUPPORTED: ' + str(profile))
        defaults = CLOTH_PROFILES[profile]
        _known(values, defaults, 'settings')
        result = dict(defaults, **values)
        for key, value in result.items():
            if key.startswith('use_'):
                if type(value) is not bool:
                    raise ValueError('PHYSICS_BOOLEAN_REQUIRED: settings.' + key)
            elif key in ('quality', 'collision_quality'):
                result[key] = number(value, key, 1, 30, integer=True)
            elif key in ('distance_min', 'self_distance_min'):
                result[key] = number(value, key, 0.001, 0.1)
            elif key == 'mass':
                result[key] = number(value, key, 0.001, 20)
            elif key == 'pin_stiffness':
                result[key] = number(value, key, 0, 50)
            else:
                result[key] = number(value, key, 0, 100)
        return result
    if profile not in (None, 'CUSTOM'):
        raise ValueError('PHYSICS_PROFILE_UNSUPPORTED: non-cloth profile')
    _known(values, tuple(SPRING_DEFAULTS) + ('gravity',), 'settings')
    result = dict(SPRING_DEFAULTS, **values)
    bounds = {'mass': (0.001, 100), 'stiffness': (0.001, 10000),
              'damping': (0, 1000), 'gravity_influence': (0, 1),
              'lag_s': (0, 1), 'max_displacement_m': (1e-6, 10),
              'max_angle_deg': (0, 90), 'excitation_scale': (0, 10)}
    for key, limits in bounds.items():
        result[key] = number(result[key], key, *limits)
    result['substeps'] = number(result['substeps'], 'substeps', 1, 64, integer=True)
    if 'gravity' in result:
        result['gravity'] = vector(result['gravity'], 'gravity')
        if math.sqrt(sum(v * v for v in result['gravity'])) > 100:
            raise ValueError('PHYSICS_GRAVITY_INVALID: magnitude exceeds 100 m/s²')
    fps = number(fps, 'fps', 30, 60, integer=True)
    if fps not in (30, 60):
        raise ValueError('PHYSICS_FPS_INVALID: 30 or 60')
    step = 1.0 / (fps * result['substeps'])
    criterion = step * step * result['stiffness'] / result['mass'] + 2 * step * result['damping'] / result['mass']
    if not criterion < 4:
        raise ValueError('SPRING_TIMESTEP_UNSTABLE: increase substeps or reduce stiffness/damping')
    return result


def validate_qa(qa=None):
    values = _mapping(qa if qa is not None else {}, 'qa')
    _known(values, QA_DEFAULTS, 'qa')
    result = dict(QA_DEFAULTS, **values)
    for key, value in result.items():
        if key == 'max_inverted_faces':
            result[key] = number(value, key, 0, 1000000, integer=True)
        elif key in ('min_area_ratio',):
            result[key] = number(value, key, 0, 1)
        elif key in ('max_stretch_ratio', 'max_extent_ratio'):
            result[key] = number(value, key, 1, 100)
        else:
            result[key] = number(value, key, 0, 100000)
    return result


def _word(text, words):
    return any(re.search(r'\b' + re.escape(word) + r'\b', text) for word in words)


def create_brief(intent=None, brief=None):
    """Translate explicit words conservatively and label every normalized field."""
    values = _mapping(brief if brief is not None else {}, 'brief')
    allowed = ('schema', 'intent', 'subsystem', 'profile', 'duration_s', 'frame_start',
               'frame_end', 'fps', 'gravity', 'settings', 'qa', 'region',
               'region_authorized', 'provenance', 'note')
    _known(values, allowed, 'brief')
    if intent is not None and not isinstance(intent, str) or 'intent' in values and not isinstance(values['intent'], str):
        raise ValueError('PHYSICS_INTENT_INVALID: text required')
    text = (intent if intent is not None else values.get('intent', '')).strip()
    if not text and not values:
        raise ValueError('PHYSICS_BRIEF_EMPTY: intent or structured brief required')
    if len(text) > 4000:
        raise ValueError('PHYSICS_INTENT_INVALID: at most 4000 characters')
    if 'schema' in values and values['schema'] != 'production.physics_brief.v1':
        raise ValueError('PHYSICS_SCHEMA_INVALID')
    provenance = {}
    low = text.lower()
    inferred = []
    for name, words in (
        ('CLOTH', ('cloth', 'coat', 'fabric', 'tela', 'abrigo')),
        ('HAIR', ('hair', 'cabello', 'pelo')),
        ('CHAIN', ('chain', 'cadena')),
        ('CABLE', ('cable', 'rope', 'cuerda')),
        ('SOFT', ('soft', 'jiggle', 'blando', 'blanda')),
        ('ACCESSORY', ('accessory', 'pendant', 'pouch', 'strap', 'accesorio', 'colgante', 'correa')),
        ('SPRING_BONE', ('spring', 'bone', 'antenna', 'tail', 'hueso', 'antena', 'cola')),
    ):
        if _word(low, words):
            inferred.append(name)
    subsystem = values.get('subsystem')
    if subsystem is None:
        # A strap described as spring-like is still an accessory. Hair spring
        # bones select HAIR; competing material/object types remain ambiguous.
        if len(inferred) > 1 and 'SPRING_BONE' in inferred:
            inferred.remove('SPRING_BONE')
        if len(inferred) > 1:
            raise ValueError('PHYSICS_INTENT_AMBIGUOUS: specify one subsystem')
        subsystem = inferred[0] if inferred else 'SPRING_BONE'
        provenance['subsystem'] = 'USER_REQUIREMENT' if inferred else 'INFERRED_DEFAULT'
    else:
        if not isinstance(subsystem, str):
            raise ValueError('PHYSICS_SUBSYSTEM_UNSUPPORTED')
        subsystem = subsystem.upper()
        provenance['subsystem'] = 'USER_REQUIREMENT'
    if subsystem not in SUBSYSTEMS:
        raise ValueError('PHYSICS_SUBSYSTEM_UNSUPPORTED: ' + str(subsystem))
    profile = values.get('profile')
    if profile is not None:
        if not isinstance(profile, str):
            raise ValueError('PHYSICS_PROFILE_UNSUPPORTED')
        profile = profile.upper()
        provenance['profile'] = 'USER_REQUIREMENT'
    elif subsystem == 'CLOTH':
        profile = 'MEDIUM_FABRIC'
        for words, label in ((('heavy', 'pesado'), 'HEAVY_FABRIC'),
                             (('light', 'ligero'), 'LIGHT_FABRIC'),
                             (('leather', 'cuero'), 'LEATHER_LIKE'),
                             (('stiff', 'rigid', 'rígido'), 'STIFF_CLOTH')):
            if _word(low, words):
                profile = label
                provenance['profile'] = 'USER_REQUIREMENT'
                break
        provenance.setdefault('profile', 'INFERRED_DEFAULT')
    else:
        profile = 'CUSTOM'
        provenance['profile'] = 'INFERRED_DEFAULT'
    fps = number(values.get('fps', 30), 'fps', integer=True)
    if fps not in (30, 60):
        raise ValueError('PHYSICS_FPS_INVALID: 30 or 60')
    start = number(values.get('frame_start', 1), 'frame_start', -1000000, 1000000, integer=True)
    if 'frame_end' in values:
        end = number(values['frame_end'], 'frame_end', -1000000, 1000000, integer=True)
        duration = (end - start) / fps
        if 'duration_s' in values and abs(number(values['duration_s'], 'duration_s') - duration) > 1e-8:
            raise ValueError('PHYSICS_DURATION_FRAME_MISMATCH')
    else:
        duration = number(values.get('duration_s', 3.0), 'duration_s', 1, 10)
        end = start + int(round(duration * fps))
        if abs(duration - (end - start) / fps) > 1e-8:
            raise ValueError('PHYSICS_DURATION_FRAME_ALIGNMENT_REQUIRED: duration must match the FPS sample grid')
    if not 1 <= duration <= 10 or end - start + 1 > 601:
        raise ValueError('PHYSICS_DURATION_INVALID: 1..10 seconds and at most 601 samples')
    gravity = vector(values.get('gravity', [0.0, 0.0, -9.81]), 'gravity')
    if math.sqrt(sum(v * v for v in gravity)) > 100:
        raise ValueError('PHYSICS_GRAVITY_INVALID')
    settings_input = _mapping(values.get('settings', {}), 'settings')
    proposed_input = {}
    if subsystem != 'CLOTH':
        if _word(low, ('subtle', 'restrained', 'sutil', 'contenido', 'contenida')):
            proposed_input.update(max_displacement_m=0.05, damping=12.0)
        if subsystem == 'SOFT' and _word(low, ('heavy', 'pesado', 'pesada')):
            proposed_input.update(mass=2.0, stiffness=30.0, damping=12.0)
    settings = normalize_settings(subsystem, dict(proposed_input, **settings_input), profile, fps)
    qa_input = _mapping(values.get('qa', {}), 'qa')
    qa = validate_qa(qa_input)
    region = values.get('region')
    if region is not None and (not isinstance(region, (str, dict)) or not region):
        raise ValueError('PHYSICS_REGION_INVALID: explicit non-empty region')
    authorized = values.get('region_authorized', False)
    if type(authorized) is not bool:
        raise ValueError('PHYSICS_BOOLEAN_REQUIRED: region_authorized')
    if subsystem == 'SOFT' and (not region or not authorized):
        raise ValueError('SOFT_REGION_AUTHORIZATION_REQUIRED: explicit region and region_authorized=true')
    result = {'schema': 'production.physics_brief.v1', 'intent': text, 'subsystem': subsystem,
              'profile': profile, 'duration_s': duration, 'frame_start': start, 'frame_end': end,
              'fps': fps, 'gravity': gravity, 'settings': settings, 'qa': qa,
              'region': region, 'region_authorized': authorized,
              'note': 'Parameter profiles and QA tolerances are calibration proposals, not universal physical values.'}
    for field in ('intent', 'duration_s', 'frame_start', 'frame_end', 'fps', 'gravity', 'region', 'region_authorized'):
        provenance[field] = 'USER_REQUIREMENT' if field in values or field == 'intent' and intent is not None else 'INFERRED_DEFAULT'
    for key in settings:
        provenance['settings.' + key] = 'USER_REQUIREMENT' if key in settings_input else 'PROPOSED_PARAMETER'
    for key in qa:
        provenance['qa.' + key] = 'USER_REQUIREMENT' if key in qa_input else 'PROPOSED_PARAMETER'
    # Normalized briefs may be transported again without laundering proposal
    # provenance into user authorization. Only our finite labels are accepted.
    if 'provenance' in values:
        old = _mapping(values['provenance'], 'provenance')
        _known(old, provenance, 'provenance')
        if any(value not in PROVENANCE for value in old.values()):
            raise ValueError('PHYSICS_PROVENANCE_INVALID')
        provenance.update(old)
    result['provenance'] = provenance
    return result
