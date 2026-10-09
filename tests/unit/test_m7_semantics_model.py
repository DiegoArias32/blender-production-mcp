"""M7 semantic mapping and facial brief: evidence decides, names only nudge; provenance is explicit."""
import pytest

from blender_mcp.production.facial import semantics as S
from blender_mcp.production.facial import model as M


def stats(zones, direction, side_share=0.5, tm=0.0, lc=0.0, centroid=(0.0, 0.0, 0.0), moved=40):
    return {'zones': zones, 'direction': list(direction), 'left_share': side_share, 'toward_mouth': tm, 'lips_converge': lc,
            'centroid': list(centroid), 'moved_vertices': moved}


def key_probe(name, st, driven=False):
    return {'key': name, 'mesh': 'Face', 'slider_min': 0.0, 'slider_max': 1.0, 'driven': driven, 'stats': st}


def test_lid_and_jaw_rules_come_from_displacement_not_names():
    lid = S.rule_scores(stats({'LEFT_UPPER_LID': 0.9}, (0, 0, -1), side_share=1.0))
    assert lid and lid[0][0] == 'lid_close_L'
    jaw = S.rule_scores(stats({'JAW': 0.8, 'LOWER_LIP': 0.2}, (0, -0.2, -1)))
    assert any(r[0] == 'jaw_open' for r in jaw)


def test_name_agreement_only_adjusts_confidence_and_side_conflict_lowers_it():
    st = stats({'LEFT_UPPER_LID': 0.9}, (0, 0, -1), side_share=1.0)
    agreeing = S.candidates(key_probe('eyeBlinkLeft', st), {})[0]
    neutral = S.candidates(key_probe('Key 7', st), {})[0]
    conflict = S.candidates(key_probe('Blink.R', st), {})[0]
    assert agreeing['semantic'] == neutral['semantic'] == conflict['semantic'] == 'lid_close_L'
    assert agreeing['confidence'] > neutral['confidence'] > conflict['confidence']
    assert any('SIDE_CONFLICT' in e for e in conflict['evidence'])
    # A name alone never creates a mapping: no deformation, no candidate.
    assert S.candidates(key_probe('eyeBlinkLeft', stats({}, (0, 0, 0), moved=0)), {}) == []


def test_driven_and_mixed_area_controls_are_capped_below_usable():
    st = stats({'LEFT_UPPER_LID': 0.9}, (0, 0, -1), side_share=1.0)
    driven = S.candidates(key_probe('eyeBlinkLeft', st, driven=True), {})[0]
    assert driven['confidence'] <= 0.2 and driven['confidence'] < S.USABLE_CONFIDENCE
    mixed = S.candidates(key_probe('Key 1', stats({'LEFT_UPPER_LID': 0.6, 'RIGHT_LIP_CORNER': 0.4}, (0, 0, -1), side_share=1.0)), {})
    assert mixed and all(r['confidence'] <= 0.45 for r in mixed)


def test_classification_unsupported_and_ambiguous_block_mutation():
    empty = {'key_probes': [], 'bone_probes': [], 'eyes': ['L', 'R']}
    analysis = S.analyze(empty, {'head_bone': 'head'})
    assert analysis['classification']['type'] == 'UNSUPPORTED'
    assert all(v['status'] == 'UNSUPPORTED' for v in analysis['compatibility'].values())
    mixed = {'key_probes': [key_probe('Key ' + str(i), stats({'LEFT_UPPER_LID': 0.5, 'RIGHT_LIP_CORNER': 0.5}, (0, 0, -1), 1.0)) for i in range(4)],
             'bone_probes': [], 'eyes': ['L', 'R']}
    assert S.analyze(mixed, {'head_bone': 'head'})['classification']['type'] == 'AMBIGUOUS'


def test_brief_records_provenance_and_requires_timing_for_speech():
    brief = M.create_brief('angry but restrained, speaking to someone off-screen to the left')
    assert brief['emotions']['ANGER'] > 0 and brief['style'] == 'restrained'
    assert brief['provenance']['emotions.ANGER'].startswith('USER_REQUIREMENT')
    assert brief['speech']['timing'] == 'TIMING_INPUT_REQUIRED'
    assert brief['gaze']['target'] == 'offscreen' and brief['gaze']['side'] == 'LEFT'
    assert brief['provenance']['jaw'].startswith('PROPOSAL')
    assert brief['provenance']['fps'] == 'INFERRED_DEFAULT'
    with pytest.raises(ValueError, match='FPS_INVALID'):
        M.create_brief('neutral', {'fps': 29})
    with pytest.raises(ValueError, match='DURATION_INVALID'):
        M.create_brief('neutral', {'duration_s': 20})
    with pytest.raises(ValueError, match='ASYMMETRY_INVALID'):
        M.create_brief('neutral', {'asymmetry': [{'area': 'brow', 'side': 'L'}]})
    with pytest.raises(ValueError, match='UNKNOWN_EMOTION'):
        M.create_brief(None, {'emotions': {'BOREDOM': 0.5}})


def test_hiding_is_expressed_by_restraint_once_not_twice():
    open_anger = M.expression_dimensions(M.create_brief('angry'))
    hidden = M.expression_dimensions(M.create_brief('angry but trying to hide it'))
    ratio = hidden['brow_compress'] / open_anger['brow_compress']
    assert ratio == pytest.approx(M.STYLE_SCALE['restrained'], rel=1e-6)


def test_viseme_support_reflects_measured_outputs():
    mapping = {k: {'usable': True} for k in ('jaw_open', 'lip_funnel', 'lip_pucker', 'lip_stretch_L', 'lip_stretch_R')}
    support = M.viseme_support(mapping)
    assert support['M_B_P']['status'] == 'UNSUPPORTED'
    assert support['O']['status'] == 'SUPPORTED'
    assert support['F_V']['status'] == 'APPROXIMATED'
    assert set(M.VISEMES) == {'REST', 'A', 'E', 'I', 'O', 'U', 'M_B_P', 'F_V', 'L', 'TH', 'S_Z', 'SH_CH', 'R', 'W_Q'}
