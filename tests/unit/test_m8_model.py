import math

import pytest

from blender_mcp.production.physics.model import (
    CLOTH_PROFILES, PROVENANCE, SPRING_DEFAULTS, create_brief,
    normalize_settings, validate_qa,
)


@pytest.mark.parametrize('intent,subsystem,profile', [
    ('heavy coat with subtle delayed movement', 'CLOTH', 'HEAVY_FABRIC'),
    ('light fabric', 'CLOTH', 'LIGHT_FABRIC'),
    ('leather coat', 'CLOTH', 'LEATHER_LIKE'),
    ('stiff cloth', 'CLOTH', 'STIFF_CLOTH'),
    ('long hair with restrained spring motion', 'HAIR', 'CUSTOM'),
    ('loose strap with moderate spring motion', 'ACCESSORY', 'CUSTOM'),
    ('dangling chain', 'CHAIN', 'CUSTOM'),
    ('loose cable', 'CABLE', 'CUSTOM'),
    ('restrained motion', 'SPRING_BONE', 'CUSTOM'),
])
def test_intent_translation_is_conservative(intent, subsystem, profile):
    result = create_brief(intent)
    assert result['subsystem'] == subsystem
    assert result['profile'] == profile
    assert result['frame_end'] == 91
    assert set(result['provenance'].values()) <= set(PROVENANCE)
    assert result['provenance']['settings.mass'] == 'PROPOSED_PARAMETER'
    assert 'calibration' in result['note']


def test_requirements_defaults_and_proposals_are_separate():
    result = create_brief('hair', {'fps': 60, 'duration_s': 1, 'settings': {'damping': 12},
                                    'qa': {'max_penetration_m': 0.002}})
    assert result['provenance']['fps'] == 'USER_REQUIREMENT'
    assert result['provenance']['settings.damping'] == 'USER_REQUIREMENT'
    assert result['provenance']['settings.mass'] == 'PROPOSED_PARAMETER'
    assert result['provenance']['qa.max_penetration_m'] == 'USER_REQUIREMENT'
    assert result['provenance']['frame_start'] == 'INFERRED_DEFAULT'
    assert create_brief(brief=result) == result


@pytest.mark.parametrize('profile', list(CLOTH_PROFILES))
def test_cloth_profiles_are_bounded_and_expose_rna_values(profile):
    result = create_brief(brief={'subsystem': 'CLOTH', 'profile': profile})
    assert result['settings'] == CLOTH_PROFILES[profile]
    assert result['settings']['use_self_collision'] is False
    assert result['settings']['use_collision'] is True


@pytest.mark.parametrize('field,value', [
    ('fps', True), ('fps', 24), ('fps', 59.9), ('fps', float('nan')),
    ('duration_s', False), ('duration_s', 0.99), ('duration_s', 10.01),
    ('duration_s', float('inf')), ('frame_start', 1.2), ('frame_end', True),
    ('subsystem', 'FLUID'), ('profile', 'PHYSICALLY_PERFECT'), ('gravity', [0, 0, math.nan]),
    ('gravity', [0, 0, True]), ('gravity', [0, 0, -1000]), ('unknown', 1),
    ('region_authorized', 1), ('intent', {}), ('settings', []), ('qa', []),
])
def test_invalid_brief_rejects(field, value):
    with pytest.raises(ValueError):
        create_brief('hair', {field: value})


@pytest.mark.parametrize('subsystem,settings', [
    ('SPRING_BONE', {'mass': True}), ('SPRING_BONE', {'mass': 0}),
    ('SPRING_BONE', {'stiffness': math.inf}), ('SPRING_BONE', {'damping': -1}),
    ('SPRING_BONE', {'substeps': 8.5}), ('SPRING_BONE', {'substeps': 65}),
    ('SPRING_BONE', {'lag_s': 1.01}), ('SPRING_BONE', {'max_angle_deg': 91}),
    ('SPRING_BONE', {'max_displacement_m': 0}), ('SPRING_BONE', {'random_noise': 1}),
    ('SPRING_BONE', {'mass': 10 ** 1000}),
    ('CLOTH', {'quality': True}), ('CLOTH', {'mass': 0}),
    ('CLOTH', {'fake_solver_quality': 2}), ('CLOTH', {'use_collision': 1}),
    ('CLOTH', {'distance_min': math.nan}), ('CLOTH', {'bending_stiffness': -1}),
])
def test_settings_reject_nonfinite_bool_unbounded_and_unknown(subsystem, settings):
    with pytest.raises(ValueError):
        normalize_settings(subsystem, settings)


def test_discrete_timestep_must_be_stable():
    with pytest.raises(ValueError, match='TIMESTEP_UNSTABLE'):
        normalize_settings('SPRING_BONE', {'mass': .001, 'stiffness': 10000, 'damping': 1000, 'substeps': 1})
    assert normalize_settings('SPRING_BONE') == SPRING_DEFAULTS


def test_frame_range_and_duration_contract():
    result = create_brief(brief={'subsystem': 'CHAIN', 'fps': 60, 'frame_start': 11, 'frame_end': 611})
    assert result['duration_s'] == 10
    assert result['frame_end'] - result['frame_start'] + 1 == 601
    for values in ({'duration_s': 2, 'frame_end': 31}, {'frame_end': 11}, {'fps': 60, 'frame_end': 602},
                   {'duration_s': 1.01}):
        with pytest.raises(ValueError):
            create_brief('chain', values)


def test_semantic_restraint_selects_bounded_proposals_and_user_settings_win():
    restrained = create_brief('hair with restrained motion')
    assert restrained['settings']['max_displacement_m'] == .05
    assert restrained['settings']['damping'] == 12
    assert restrained['provenance']['settings.damping'] == 'PROPOSED_PARAMETER'
    explicit = create_brief('hair with restrained motion', {'settings': {'damping': 8}})
    assert explicit['settings']['damping'] == 8
    assert explicit['provenance']['settings.damping'] == 'USER_REQUIREMENT'


def test_ambiguous_intent_requires_an_explicit_subsystem():
    with pytest.raises(ValueError, match='AMBIGUOUS'):
        create_brief('hair and heavy coat')
    assert create_brief('hair and heavy coat', {'subsystem': 'CLOTH'})['subsystem'] == 'CLOTH'


def test_soft_requires_explicit_region_authorization():
    for values in ({}, {'region': 'authorized_mass'}, {'region_authorized': True}):
        with pytest.raises(ValueError, match='AUTHORIZATION_REQUIRED'):
            create_brief('soft mass with restrained jiggle', values)
    result = create_brief('soft mass', {'region': 'left_mutated_mass', 'region_authorized': True})
    assert result['region'] == 'left_mutated_mass'
    assert result['region_authorized'] is True


@pytest.mark.parametrize('qa', [{'score': 99}, {'max_velocity_m_s': math.nan},
                                {'max_acceleration_m_s2': True}, {'max_inverted_faces': .5},
                                {'max_stretch_ratio': .9}, {'min_area_ratio': 2}])
def test_qa_thresholds_are_finite_si_values(qa):
    with pytest.raises(ValueError):
        validate_qa(qa)


def test_empty_intent_and_provenance_unknown_reject():
    with pytest.raises(ValueError, match='EMPTY'):
        create_brief()
    with pytest.raises(ValueError):
        create_brief('hair', {'provenance': {'settings.mass': 'UNIVERSAL_TRUTH'}})
    with pytest.raises(ValueError):
        create_brief('hair', {'provenance': {'unknown': 'USER_REQUIREMENT'}})
