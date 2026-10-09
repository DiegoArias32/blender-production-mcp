"""Facial performance model (host side): brief, expression dimensions, library, visemes, phonemes.

This is an *animation-control abstraction*, not a psychological model: an emotion word selects a
starting combination of control dimensions that a reviewer can change. Every number below is a
starting proposal for synthetic tests, labelled as such in results; none is claimed as measured truth.
"""
from __future__ import annotations

import math
import re

# ------------------------------------------------------------------ expression dimensions

DIMENSIONS = ('brow_raise_inner', 'brow_raise_outer', 'brow_compress', 'lid_open', 'squint', 'cheek_raise',
              'nose_tension', 'lip_compress', 'lip_corner', 'lip_stretch', 'jaw_tension', 'jaw_open', 'upper_lip_raise',
              'lower_lip_depress', 'lip_pucker')

# dimension -> list of (semantic output, weight). Signed dimensions split by sign.
DIMENSION_OUTPUTS = {
    'brow_raise_inner': {'+': [('brow_inner_raise_L', 1.0), ('brow_inner_raise_R', 1.0)]},
    'brow_raise_outer': {'+': [('brow_outer_raise_L', 1.0), ('brow_outer_raise_R', 1.0)]},
    'brow_compress': {'+': [('brow_lower_L', 1.0), ('brow_lower_R', 1.0)]},
    'lid_open': {'+': [('lid_wide_L', 1.0), ('lid_wide_R', 1.0)], '-': [('lid_close_L', 0.55), ('lid_close_R', 0.55)]},
    'squint': {'+': [('lid_raise_L', 1.0), ('lid_raise_R', 1.0)]},
    'cheek_raise': {'+': [('cheek_raise_L', 1.0), ('cheek_raise_R', 1.0)]},
    'nose_tension': {'+': [('nose_sneer_L', 1.0), ('nose_sneer_R', 1.0)]},
    'lip_compress': {'+': [('lip_press_L', 1.0), ('lip_press_R', 1.0)]},
    'lip_corner': {'+': [('lip_corner_up_L', 1.0), ('lip_corner_up_R', 1.0)],
                   '-': [('lip_corner_down_L', 1.0), ('lip_corner_down_R', 1.0)]},
    'lip_stretch': {'+': [('lip_stretch_L', 1.0), ('lip_stretch_R', 1.0)]},
    'jaw_tension': {'+': [('lip_press_L', 0.35), ('lip_press_R', 0.35), ('jaw_forward', 0.15)]},
    'jaw_open': {'+': [('jaw_open', 1.0)]},
    'upper_lip_raise': {'+': [('upper_lip_raise_L', 1.0), ('upper_lip_raise_R', 1.0)]},
    'lower_lip_depress': {'+': [('lower_lip_depress_L', 1.0), ('lower_lip_depress_R', 1.0)]},
    'lip_pucker': {'+': [('lip_pucker', 1.0)]},
}

# Starting semantic profiles (strength 1.0). Proposals for controlled tests, not universal truth.
LIBRARY = {
    'NEUTRAL': {},
    'ANGER': {'brow_compress': 0.75, 'lid_open': 0.15, 'squint': 0.35, 'nose_tension': 0.35, 'lip_compress': 0.6,
              'jaw_tension': 0.6, 'lip_corner': -0.25, 'upper_lip_raise': 0.15},
    'FEAR': {'brow_raise_inner': 0.8, 'brow_raise_outer': 0.45, 'lid_open': 0.7, 'lip_stretch': 0.5, 'jaw_open': 0.15,
             'lower_lip_depress': 0.2},
    'SADNESS': {'brow_raise_inner': 0.7, 'lid_open': -0.35, 'lip_corner': -0.55, 'lower_lip_depress': 0.15},
    'JOY': {'cheek_raise': 0.6, 'squint': 0.35, 'lip_corner': 0.8, 'lid_open': -0.1, 'jaw_open': 0.1},
    'DISGUST': {'nose_tension': 0.75, 'upper_lip_raise': 0.6, 'brow_compress': 0.35, 'squint': 0.3, 'lip_corner': -0.2},
    'PAIN': {'brow_compress': 0.6, 'brow_raise_inner': 0.35, 'squint': 0.6, 'lid_open': -0.4, 'cheek_raise': 0.4,
             'lip_stretch': 0.45, 'upper_lip_raise': 0.3, 'jaw_open': 0.15},
    'FATIGUE': {'lid_open': -0.45, 'brow_raise_inner': 0.15, 'lip_corner': -0.15, 'jaw_open': 0.05},
    'SUSPICION': {'squint': 0.5, 'brow_compress': 0.35, 'lip_compress': 0.3, 'lid_open': -0.1, 'lip_corner': -0.1},
    'SURPRISE': {'brow_raise_inner': 0.75, 'brow_raise_outer': 0.75, 'lid_open': 0.75, 'jaw_open': 0.35},
}
EMOTION_WORDS = {
    'ANGER': ('angry', 'anger', 'furious', 'rage', 'mad', 'enfadado', 'enojado', 'ira'),
    'FEAR': ('afraid', 'fear', 'scared', 'terrified', 'frightened', 'miedo', 'aterrado'),
    'SADNESS': ('sad', 'sadness', 'grief', 'sorrow', 'triste'),
    'JOY': ('happy', 'joy', 'smiling', 'glad', 'feliz', 'alegre'),
    'DISGUST': ('disgust', 'disgusted', 'repulsed', 'asco'),
    'PAIN': ('pain', 'painful', 'hurt', 'agony', 'dolor'),
    'FATIGUE': ('tired', 'exhausted', 'fatigue', 'weary', 'cansado', 'agotado'),
    'SUSPICION': ('suspicious', 'suspicion', 'wary', 'distrust', 'sospecha'),
    'SURPRISE': ('surprised', 'surprise', 'shocked', 'sorpresa'),
}
STYLE_WORDS = {'restrained': ('restrained', 'controlled', 'contained', 'hiding', 'hide', 'trying to hide', 'subtle', 'contenido'),
               'expressive': ('expressive', 'exaggerated', 'big', 'cartoon', 'broad')}
LOW_WORDS = ('slightly', 'a bit', 'little', 'low', 'mild')
HIGH_WORDS = ('very', 'extremely', 'high', 'intense', 'deeply')


def _level(text, start, end):
    window = text[max(0, start - 18):start]
    if any(w in window for w in HIGH_WORDS):
        return 'high', 0.9
    if any(w in window for w in LOW_WORDS):
        return 'low', 0.35
    return 'medium', 0.65


def create_brief(intent=None, brief=None):
    """Structured facial brief with provenance: USER_REQUIREMENT / INFERRED_DEFAULT / PROPOSAL."""
    values = dict(brief or {})
    text = str(intent or values.get('intent') or '').strip()
    if not text and not values:
        raise ValueError('BRIEF_EMPTY: give an intent text or structured brief')
    low = ' ' + text.lower() + ' '
    provenance = {}
    emotions = {}
    if isinstance(values.get('emotions'), dict):
        for name, value in values['emotions'].items():
            key = str(name).upper()
            if key not in LIBRARY:
                raise ValueError('UNKNOWN_EMOTION: ' + str(name) + ' (library: ' + ', '.join(sorted(LIBRARY)) + ')')
            emotions[key] = _strength(value)
            provenance['emotions.' + key] = 'USER_REQUIREMENT'
    for key, words in EMOTION_WORDS.items():
        if key in emotions:
            continue
        for word in words:
            match = re.search(r'\b' + re.escape(word) + r'\b', low)
            if match:
                level, value = _level(low, match.start(), match.end())
                emotions[key] = value
                provenance['emotions.' + key] = 'USER_REQUIREMENT (from words: "' + word + '", ' + level + ')'
                break
    if 'hide' in low or 'hiding' in low:
        # The restrained style already lowers surface intensity; scaling the emotion as well would count the
        # same words twice (measured: 21 % of the library profile, hardly visible on the fixture).
        for key in list(emotions):
            if key in ('FEAR', 'PAIN', 'ANGER'):
                provenance['emotions.' + key] += '; masked ("trying to hide"): expressed through the restrained style'
    style = values.get('style')
    if style:
        provenance['style'] = 'USER_REQUIREMENT'
    else:
        style = 'neutral'
        for name, words in STYLE_WORDS.items():
            if any(w in low for w in words):
                style = name
                provenance['style'] = 'USER_REQUIREMENT (from words)'
                break
        provenance.setdefault('style', 'INFERRED_DEFAULT')
    if style not in ('restrained', 'neutral', 'expressive'):
        raise ValueError('UNKNOWN_STYLE: restrained, neutral or expressive')
    gaze = values.get('gaze') or {}
    if gaze:
        provenance['gaze'] = 'USER_REQUIREMENT'
    elif 'off-screen' in low or 'off screen' in low or 'watching someone' in low:
        gaze = {'target': 'offscreen', 'side': 'LEFT' if 'left' in low else 'RIGHT'}
        provenance['gaze'] = 'USER_REQUIREMENT (from words)'
    else:
        gaze = {'target': 'camera'}
        provenance['gaze'] = 'INFERRED_DEFAULT'
    speech = values.get('speech') or {}
    if speech:
        provenance['speech'] = 'USER_REQUIREMENT'
    elif any(w in low for w in ('speaking', 'talking', 'says', 'saying', 'dialogue', 'line')):
        speech = {'enabled': True, 'timing': 'TIMING_INPUT_REQUIRED'}
        provenance['speech'] = 'USER_REQUIREMENT (from words); timed phonemes/visemes must be supplied'
    else:
        speech = {'enabled': False}
        provenance['speech'] = 'INFERRED_DEFAULT'
    jaw = values.get('jaw') or {}
    if jaw:
        provenance['jaw'] = 'USER_REQUIREMENT'
    elif 'ANGER' in emotions or 'PAIN' in emotions:
        jaw = {'tension': 'medium'}
        provenance['jaw'] = 'PROPOSAL (anger/pain usually carry jaw tension; reviewer may remove)'
    else:
        jaw = {'tension': 'none'}
        provenance['jaw'] = 'INFERRED_DEFAULT'
    duration = values.get('duration_s', 3.0)
    fps = values.get('fps', 30)
    provenance.setdefault('duration_s', 'USER_REQUIREMENT' if 'duration_s' in values else 'INFERRED_DEFAULT')
    provenance.setdefault('fps', 'USER_REQUIREMENT' if 'fps' in values else 'INFERRED_DEFAULT')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0.5 <= duration <= 15:
        raise ValueError('DURATION_INVALID: 0.5..15 s')
    if fps not in (24, 25, 30, 48, 50, 60):
        raise ValueError('FPS_INVALID: 24, 25, 30, 48, 50 or 60')
    asymmetry = values.get('asymmetry') or []
    for row in asymmetry:
        if not isinstance(row, dict) or not {'area', 'side', 'strength', 'purpose'} <= set(row):
            raise ValueError('ASYMMETRY_INVALID: each entry needs area, side, strength and purpose (no random asymmetry)')
    if not emotions:
        emotions = {'NEUTRAL': 1.0}
        provenance['emotions.NEUTRAL'] = 'INFERRED_DEFAULT (no emotion words found)'
    microexpressions = values.get('microexpressions', 'restrained' == style and len(emotions) > 1)
    provenance.setdefault('microexpressions', 'USER_REQUIREMENT' if 'microexpressions' in values else
                          'PROPOSAL (mixed or masked emotions leak in brief microexpressions)')
    return {'schema': 'production.facial_brief.v1', 'intent': text, 'emotions': emotions, 'style': style, 'gaze': gaze,
            'speech': speech, 'jaw': jaw, 'duration_s': float(duration), 'fps': int(fps),
            'frame_start': 1, 'frame_end': 1 + int(round(duration * fps)),
            'asymmetry': asymmetry, 'microexpressions': bool(microexpressions), 'provenance': provenance,
            'note': 'An animation-control brief; emotion words select starting control profiles, not psychological truth.'}


def _strength(value):
    if isinstance(value, str):
        return {'low': 0.35, 'medium': 0.65, 'high': 0.9}.get(value.lower(), None) or _fail('STRENGTH_INVALID: ' + value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        _fail('STRENGTH_INVALID: 0..1 or low/medium/high')
    return float(value)


def _fail(message):
    raise ValueError(message)


STYLE_SCALE = {'restrained': 0.55, 'neutral': 1.0, 'expressive': 1.3}


def expression_dimensions(brief):
    """Blend library profiles by emotion strength; restrained style scales surface intensity."""
    dims = {d: 0.0 for d in DIMENSIONS}
    for emotion, strength in brief['emotions'].items():
        for dim, value in LIBRARY[emotion].items():
            dims[dim] += value * strength
    scale = STYLE_SCALE[brief['style']]
    for dim in dims:
        dims[dim] = max(-1.0, min(1.0, dims[dim] * scale))
    tension = brief.get('jaw', {}).get('tension')
    if tension in ('low', 'medium', 'high'):
        dims['jaw_tension'] = max(dims['jaw_tension'], {'low': 0.3, 'medium': 0.55, 'high': 0.85}[tension])
    return dims


def semantic_targets(dims):
    """Dimension values -> semantic output targets (0..1)."""
    out = {}
    reasons = {}
    for dim, value in dims.items():
        if abs(value) < 1e-6:
            continue
        rows = DIMENSION_OUTPUTS[dim].get('+' if value > 0 else '-')
        if not rows:
            continue
        for semantic, weight in rows:
            out[semantic] = min(1.0, out.get(semantic, 0.0) + abs(value) * weight)
            reasons.setdefault(semantic, []).append(dim + '=' + format(value, '.2f'))
    return out, reasons


# ------------------------------------------------------------------ visemes and phonemes

VISEMES = {
    # semantic output targets for each rig-independent viseme
    'REST': {},
    'A': {'jaw_open': 0.55, 'lower_lip_depress_L': 0.2, 'lower_lip_depress_R': 0.2},
    'E': {'jaw_open': 0.3, 'lip_stretch_L': 0.35, 'lip_stretch_R': 0.35},
    'I': {'jaw_open': 0.18, 'lip_stretch_L': 0.45, 'lip_stretch_R': 0.45, 'lip_corner_up_L': 0.1, 'lip_corner_up_R': 0.1},
    'O': {'jaw_open': 0.4, 'lip_funnel': 0.6},
    'U': {'jaw_open': 0.15, 'lip_pucker': 0.75},
    'M_B_P': {'jaw_open': 0.05, 'lips_close': 1.0, 'lip_press_L': 0.25, 'lip_press_R': 0.25},
    'F_V': {'jaw_open': 0.1, 'lip_roll_lower': 0.6, 'upper_lip_raise_L': 0.1, 'upper_lip_raise_R': 0.1},
    'L': {'jaw_open': 0.3, 'tongue_out': 0.0},
    'TH': {'jaw_open': 0.15, 'tongue_out': 0.3},
    'S_Z': {'jaw_open': 0.08, 'lip_stretch_L': 0.25, 'lip_stretch_R': 0.25},
    'SH_CH': {'jaw_open': 0.12, 'lip_funnel': 0.45},
    'R': {'jaw_open': 0.15, 'lip_pucker': 0.35},
    'W_Q': {'jaw_open': 0.08, 'lip_pucker': 0.9},
}
# Must reach its target (closure/contact visemes): coarticulation may not dilute them.
DOMINANT = {'M_B_P': 1.0, 'F_V': 0.85, 'W_Q': 0.7}
PHONEME_TO_VISEME = {
    'AA': 'A', 'AE': 'A', 'AH': 'A', 'AO': 'O', 'AW': 'O', 'AY': 'A', 'EH': 'E', 'ER': 'R', 'EY': 'E', 'IH': 'I',
    'IY': 'I', 'OW': 'O', 'OY': 'O', 'UH': 'U', 'UW': 'U', 'B': 'M_B_P', 'M': 'M_B_P', 'P': 'M_B_P', 'F': 'F_V',
    'V': 'F_V', 'L': 'L', 'TH': 'TH', 'DH': 'TH', 'S': 'S_Z', 'Z': 'S_Z', 'SH': 'SH_CH', 'CH': 'SH_CH', 'JH': 'SH_CH',
    'ZH': 'SH_CH', 'R': 'R', 'W': 'W_Q', 'Y': 'I', 'D': 'S_Z', 'T': 'S_Z', 'N': 'S_Z', 'NG': 'E', 'K': 'E', 'G': 'E',
    'HH': 'A', 'SIL': 'REST', 'SP': 'REST', 'PAU': 'REST',
}
CONSONANTS = {'M_B_P', 'F_V', 'L', 'TH', 'S_Z', 'SH_CH', 'R', 'W_Q'}


def viseme_support(mapping):
    """Per viseme: SUPPORTED / APPROXIMATED / UNSUPPORTED from the semantic outputs the rig really has."""
    usable = {k for k, v in mapping.items() if v.get('usable')}
    rows = {}
    for viseme, targets in VISEMES.items():
        needed = [k for k, v in targets.items() if v > 0]
        missing = [k for k in needed if k not in usable]
        if not needed:
            rows[viseme] = {'status': 'SUPPORTED', 'missing': [], 'approximation': None}
        elif not missing:
            rows[viseme] = {'status': 'SUPPORTED', 'missing': [], 'approximation': None}
        elif viseme == 'M_B_P' and 'lips_close' in missing:
            rows[viseme] = {'status': 'UNSUPPORTED', 'missing': missing, 'approximation': None}
        elif len(missing) < len(needed):
            rows[viseme] = {'status': 'APPROXIMATED', 'missing': missing,
                            'approximation': 'uses ' + ', '.join(k for k in needed if k not in missing) + ' only'}
        else:
            rows[viseme] = {'status': 'UNSUPPORTED', 'missing': missing, 'approximation': None}
    return rows
