"""Semantic facial mapping from measured control probes (host side, pure Python).

Each probe (a shape key, or one direction of one bone channel) carries a displacement summary in the
normalized face frame (``zones.fz_field_stats``) or a measured eye rotation. Rules below turn that
evidence into candidate *semantic outputs* with a score; names only add or remove a little
confidence and can never create a mapping on their own. Confidence is computed here, never taken
from the caller.
"""
from __future__ import annotations

import math

USABLE_CONFIDENCE = 0.60

# semantic output -> (semantic area(s), unit, name tokens)
OUTPUTS = {
    'lid_close_L': ('LEFT_UPPER_LID', 'normalized', ('blink', 'close', 'shut')),
    'lid_close_R': ('RIGHT_UPPER_LID', 'normalized', ('blink', 'close', 'shut')),
    'lid_wide_L': ('LEFT_UPPER_LID', 'normalized', ('wide', 'open')),
    'lid_wide_R': ('RIGHT_UPPER_LID', 'normalized', ('wide', 'open')),
    'lid_raise_L': ('LEFT_LOWER_LID', 'normalized', ('squint', 'raise', 'lidraise')),
    'lid_raise_R': ('RIGHT_LOWER_LID', 'normalized', ('squint', 'raise', 'lidraise')),
    'brow_inner_raise_L': ('LEFT_BROW', 'normalized', ('inner', 'worry', 'innerup')),
    'brow_inner_raise_R': ('RIGHT_BROW', 'normalized', ('inner', 'worry', 'innerup')),
    'brow_outer_raise_L': ('LEFT_BROW', 'normalized', ('outer', 'arch')),
    'brow_outer_raise_R': ('RIGHT_BROW', 'normalized', ('outer', 'arch')),
    'brow_lower_L': ('LEFT_BROW', 'normalized', ('down', 'furrow', 'lower')),
    'brow_lower_R': ('RIGHT_BROW', 'normalized', ('down', 'furrow', 'lower')),
    'cheek_raise_L': ('LEFT_CHEEK', 'normalized', ('cheek', 'puff')),
    'cheek_raise_R': ('RIGHT_CHEEK', 'normalized', ('cheek', 'puff')),
    'nose_sneer_L': ('NOSE', 'normalized', ('nose', 'sneer', 'wrinkle')),
    'nose_sneer_R': ('NOSE', 'normalized', ('nose', 'sneer', 'wrinkle')),
    'jaw_open': ('JAW', 'normalized', ('jaw', 'open', 'drop')),
    'jaw_forward': ('JAW', 'normalized', ('forward', 'thrust')),
    'jaw_side_L': ('JAW', 'normalized', ('left', 'shift', 'side')),
    'jaw_side_R': ('JAW', 'normalized', ('right', 'shift', 'side')),
    'lips_close': ('UPPER_LIP+LOWER_LIP', 'normalized', ('close', 'seal')),
    'lip_pucker': ('UPPER_LIP+LOWER_LIP', 'normalized', ('pucker', 'kiss', 'narrow')),
    'lip_funnel': ('UPPER_LIP+LOWER_LIP', 'normalized', ('funnel', 'trumpet', 'round')),
    'lip_press_L': ('LEFT_LIP_CORNER', 'normalized', ('press', 'tight')),
    'lip_press_R': ('RIGHT_LIP_CORNER', 'normalized', ('press', 'tight')),
    'lip_roll_lower': ('LOWER_LIP', 'normalized', ('roll', 'lowerlipin', 'in')),
    'lip_roll_upper': ('UPPER_LIP', 'normalized', ('roll', 'upperlipin', 'in')),
    'lip_corner_up_L': ('LEFT_LIP_CORNER', 'normalized', ('smile', 'grin')),
    'lip_corner_up_R': ('RIGHT_LIP_CORNER', 'normalized', ('smile', 'grin')),
    'lip_corner_down_L': ('LEFT_LIP_CORNER', 'normalized', ('frown', 'sulk')),
    'lip_corner_down_R': ('RIGHT_LIP_CORNER', 'normalized', ('frown', 'sulk')),
    'lip_stretch_L': ('LEFT_LIP_CORNER', 'normalized', ('stretch', 'wide')),
    'lip_stretch_R': ('RIGHT_LIP_CORNER', 'normalized', ('stretch', 'wide')),
    'upper_lip_raise_L': ('UPPER_LIP', 'normalized', ('upper', 'snarl', 'up')),
    'upper_lip_raise_R': ('UPPER_LIP', 'normalized', ('upper', 'snarl', 'up')),
    'lower_lip_depress_L': ('LOWER_LIP', 'normalized', ('lower', 'down')),
    'lower_lip_depress_R': ('LOWER_LIP', 'normalized', ('lower', 'down')),
    'eye_yaw_L': ('LEFT_EYE', 'degrees', ('eye', 'look')),
    'eye_yaw_R': ('RIGHT_EYE', 'degrees', ('eye', 'look')),
    'eye_pitch_L': ('LEFT_EYE', 'degrees', ('eye', 'look')),
    'eye_pitch_R': ('RIGHT_EYE', 'degrees', ('eye', 'look')),
    'tongue_out': ('TONGUE', 'normalized', ('tongue',)),
}
GROUPS = {
    'EYES': ('eye_yaw_L', 'eye_yaw_R', 'eye_pitch_L', 'eye_pitch_R'),
    'LIDS': ('lid_close_L', 'lid_close_R', 'lid_wide_L', 'lid_wide_R', 'lid_raise_L', 'lid_raise_R'),
    'BROWS': ('brow_inner_raise_L', 'brow_inner_raise_R', 'brow_outer_raise_L', 'brow_outer_raise_R', 'brow_lower_L', 'brow_lower_R'),
    'CHEEKS_NOSE': ('cheek_raise_L', 'cheek_raise_R', 'nose_sneer_L', 'nose_sneer_R'),
    'JAW': ('jaw_open', 'jaw_forward', 'jaw_side_L', 'jaw_side_R'),
    'LIPS': ('lips_close', 'lip_pucker', 'lip_funnel', 'lip_press_L', 'lip_press_R', 'lip_roll_lower', 'lip_roll_upper',
             'lip_corner_up_L', 'lip_corner_up_R', 'lip_corner_down_L', 'lip_corner_down_R', 'lip_stretch_L', 'lip_stretch_R',
             'upper_lip_raise_L', 'upper_lip_raise_R', 'lower_lip_depress_L', 'lower_lip_depress_R'),
    'TONGUE': ('tongue_out',),
}
SIDE_SUFFIXES = {'L': ('.l', '_l', '-l', ' l'), 'R': ('.r', '_r', '-r', ' r')}


def _unit(direction):
    length = math.sqrt(sum(v * v for v in direction))
    return [v / length for v in direction] if length > 1e-12 else [0.0, 0.0, 0.0]


def _share(zones, names):
    return sum(zones.get(n, 0.0) for n in names)


def side_of(left_share):
    if left_share is None:
        return 'C'
    if left_share >= 0.65:
        return 'L'
    if left_share <= 0.35:
        return 'R'
    return 'C'


def _name_side(name):
    low = name.lower()
    for side, suffixes in SIDE_SUFFIXES.items():
        if low.endswith(suffixes) or ('.' + side.lower() + '.') in low:
            return side
    if 'left' in low:
        return 'L'
    if 'right' in low:
        return 'R'
    return None


def rule_scores(stats, eye_sides=None):
    """Candidate (semantic, base score in 0..1, reason) for one displacement or eye-rotation probe."""
    out = []
    rotation = {k: v for k, v in (stats.get('eye_rotation_deg') or {}).items() if abs(v[0]) + abs(v[1]) > 0.5}
    if rotation and eye_sides:
        for eye, (yaw, pitch) in rotation.items():
            side = eye_sides.get(eye)
            if side is None:
                continue
            total = abs(yaw) + abs(pitch)
            if abs(yaw) >= abs(pitch):
                out.append(('eye_yaw_' + side, abs(yaw) / total, 'eye ' + eye + ' yaw ' + format(yaw, '.2f') + ' deg'))
            else:
                out.append(('eye_pitch_' + side, abs(pitch) / total, 'eye ' + eye + ' pitch ' + format(pitch, '.2f') + ' deg'))
        return out
    zones = stats.get('zones') or {}
    if not zones or not stats.get('moved_vertices'):
        return out
    ux, uy, uz = _unit(stats.get('direction') or [0, 0, 0])
    side = side_of(stats.get('left_share'))
    tm = stats.get('toward_mouth') or 0.0
    lc = stats.get('lips_converge') or 0.0
    cx = abs((stats.get('centroid') or [0, 0, 0])[0])
    for s, tag in (('L', 'LEFT'), ('R', 'RIGHT')):
        upper = zones.get(tag + '_UPPER_LID', 0.0)
        lower = zones.get(tag + '_LOWER_LID', 0.0)
        brow = zones.get(tag + '_BROW', 0.0)
        cheek = zones.get(tag + '_CHEEK', 0.0)
        corner = zones.get(tag + '_LIP_CORNER', 0.0)
        if upper >= 0.45 and uz < -0.4:
            out.append(('lid_close_' + s, upper * min(1.0, -uz / 0.8), 'upper lid zone ' + format(upper, '.2f') + ', moves down'))
        if upper >= 0.45 and uz > 0.4:
            out.append(('lid_wide_' + s, upper * min(1.0, uz / 0.8), 'upper lid zone ' + format(upper, '.2f') + ', moves up'))
        if lower >= 0.45 and uz > 0.4:
            out.append(('lid_raise_' + s, lower * min(1.0, uz / 0.8), 'lower lid zone ' + format(lower, '.2f') + ', moves up'))
        if brow >= 0.40 and side in (s, 'C'):
            inner = cx < 0.62
            if uz > 0.4:
                name = ('brow_inner_raise_' if inner else 'brow_outer_raise_') + s
                out.append((name, brow * min(1.0, uz / 0.8), 'brow zone ' + format(brow, '.2f') + (', inner' if inner else ', outer') + ', moves up'))
            if uz < -0.4:
                out.append(('brow_lower_' + s, brow * min(1.0, -uz / 0.8), 'brow zone ' + format(brow, '.2f') + ', moves down'))
        if cheek >= 0.55 and uz > 0.5 and side == s:
            out.append(('cheek_raise_' + s, cheek * min(1.0, uz / 0.8), 'cheek zone ' + format(cheek, '.2f') + ', moves up'))
        nose = zones.get('NOSE', 0.0)
        if nose >= 0.35 and nose + cheek >= 0.8 and uz > 0.5 and side == s:
            out.append(('nose_sneer_' + s, (nose + cheek) * 0.9 * min(1.0, uz / 0.8), 'nose zone ' + format(nose, '.2f') + ', moves up'))
        if corner >= 0.55 and side == s:
            outward = ux if s == 'L' else -ux
            if uz > 0.5 and outward > -0.2:
                out.append(('lip_corner_up_' + s, corner * min(1.0, uz / 0.8), 'lip corner zone ' + format(corner, '.2f') + ', moves up'))
            if uz < -0.5 and lc > -0.3:
                out.append(('lip_corner_down_' + s, corner * min(1.0, -uz / 0.8), 'lip corner zone ' + format(corner, '.2f') + ', moves down'))
            if outward > 0.6 and tm < -0.5 and abs(uz) < 0.5:
                out.append(('lip_stretch_' + s, corner * min(1.0, outward / 0.8), 'lip corner moves outward'))
        if side == s and uy < -0.6 and lc > 0.2 and corner + zones.get('UPPER_LIP', 0) + zones.get('LOWER_LIP', 0) >= 0.7:
            out.append(('lip_press_' + s, min(1.0, -uy / 0.8) * 0.85, 'one-sided lips move back and toward each other'))
        lips = zones.get('UPPER_LIP', 0.0) + zones.get('LOWER_LIP', 0.0) + corner
        if side == s and zones.get('UPPER_LIP', 0) + corner >= 0.6 and uz > 0.5 and lc < -0.6 and corner < 0.75:
            out.append(('upper_lip_raise_' + s, min(1.0, uz / 0.8) * 0.85, 'upper lip side moves up'))
        if side == s and zones.get('LOWER_LIP', 0) + corner >= 0.6 and uz < -0.5 and lc < -0.6 and corner < 0.75:
            out.append(('lower_lip_depress_' + s, min(1.0, -uz / 0.8) * 0.85, 'lower lip side moves down'))
    jaw = zones.get('JAW', 0.0)
    if jaw >= 0.5:
        if uz < -0.3 and uy < 0.2:
            out.append(('jaw_open', jaw * min(1.0, (-uz + max(0.0, -uy)) / 0.9), 'jaw zone ' + format(jaw, '.2f') + ', moves down/back'))
        if uy > 0.7:
            out.append(('jaw_forward', jaw * min(1.0, uy / 0.8), 'jaw zone moves forward'))
        if ux > 0.7:
            out.append(('jaw_side_L', jaw * min(1.0, ux / 0.8), 'jaw zone moves left'))
        if ux < -0.7:
            out.append(('jaw_side_R', jaw * min(1.0, -ux / 0.8), 'jaw zone moves right'))
    mouth = _share(zones, ('UPPER_LIP', 'LOWER_LIP', 'LEFT_LIP_CORNER', 'RIGHT_LIP_CORNER'))
    if mouth >= 0.55:
        if lc > 1.0 and abs(uy) < 0.75:
            out.append(('lips_close', mouth * min(1.0, lc / 1.6), 'lips converge (' + format(lc, '.2f') + ')'))
        if uy > 0.6 and tm > 0.3 and lc > -0.3:
            out.append(('lip_pucker', mouth * min(1.0, uy / 0.8), 'lips move forward and toward the mouth centre'))
        if uy > 0.6 and lc < -0.3:
            out.append(('lip_funnel', mouth * min(1.0, uy / 0.8), 'lips move forward and part'))
        if side == 'C' and uy < -0.6:
            upper, lower = zones.get('UPPER_LIP', 0.0), zones.get('LOWER_LIP', 0.0)
            if lower > upper and uz > 0.2:
                out.append(('lip_roll_lower', mouth * 0.9, 'lower lip rolls in (back and up)'))
            if upper >= lower and uz < -0.1:
                out.append(('lip_roll_upper', mouth * 0.9, 'upper lip rolls in (back and down)'))
            if lc > 0.4:
                out.append(('lip_press_L', mouth * 0.6, 'lips press back (bilateral)'))
                out.append(('lip_press_R', mouth * 0.6, 'lips press back (bilateral)'))
        if side == 'C' and uz > 0.5 and lc < -0.6:
            out.append(('upper_lip_raise_L', mouth * 0.8, 'upper lip raises (bilateral)'))
            out.append(('upper_lip_raise_R', mouth * 0.8, 'upper lip raises (bilateral)'))
        if side == 'C' and uz < -0.5 and lc < -0.6:
            out.append(('lower_lip_depress_L', mouth * 0.8, 'lower lip depresses (bilateral)'))
            out.append(('lower_lip_depress_R', mouth * 0.8, 'lower lip depresses (bilateral)'))
    return out


def _name_adjust(name, semantic):
    low = name.lower().replace(' ', '')
    tokens = OUTPUTS[semantic][2]
    delta, notes = 0.0, []
    if any(t in low for t in tokens):
        delta += 0.08
        notes.append('name agrees (' + name + ')')
    wanted = semantic[-1] if semantic[-2:] in ('_L', '_R') else None
    named = _name_side(name)
    if wanted and named and named != wanted:
        delta -= 0.30
        notes.append('SIDE_CONFLICT: name says ' + named + ', deformation is ' + wanted)
    return delta, notes


AREAS = {'LEFT_UPPER_LID': 'EYE_L', 'LEFT_LOWER_LID': 'EYE_L', 'RIGHT_UPPER_LID': 'EYE_R', 'RIGHT_LOWER_LID': 'EYE_R',
         'LEFT_BROW': 'BROW_L', 'RIGHT_BROW': 'BROW_R', 'NOSE': 'NOSE', 'LEFT_CHEEK': 'CHEEK_L', 'RIGHT_CHEEK': 'CHEEK_R',
         'UPPER_LIP': 'MOUTH', 'LOWER_LIP': 'MOUTH', 'LEFT_LIP_CORNER': 'MOUTH', 'RIGHT_LIP_CORNER': 'MOUTH', 'JAW': 'JAW',
         'OTHER': 'OTHER'}
ADJACENT = {frozenset(p) for p in (('EYE_L', 'BROW_L'), ('EYE_R', 'BROW_R'), ('EYE_L', 'CHEEK_L'), ('EYE_R', 'CHEEK_R'),
                                   ('BROW_L', 'BROW_R'), ('MOUTH', 'JAW'), ('MOUTH', 'CHEEK_L'), ('MOUTH', 'CHEEK_R'),
                                   ('NOSE', 'CHEEK_L'), ('NOSE', 'CHEEK_R'), ('MOUTH', 'NOSE'), ('EYE_L', 'NOSE'), ('EYE_R', 'NOSE'))}
MIXED_SHARE = 0.08


def mixed_areas(zones):
    """Non-adjacent facial areas that both carry a real share of the deformation energy."""
    areas = {}
    for zone, share in (zones or {}).items():
        area = AREAS.get(zone, 'OTHER')
        if area != 'OTHER':
            areas[area] = areas.get(area, 0.0) + share
    big = sorted((a for a, s in areas.items() if s >= MIXED_SHARE), key=lambda a: -areas[a])
    for i in range(len(big)):
        for j in range(i + 1, len(big)):
            if frozenset((big[i], big[j])) not in ADJACENT:
                return [big[i], big[j]], areas
    return [], areas


def candidates(probe, eye_sides):
    """All scored semantic candidates for one probe, with confidence and evidence."""
    stats = probe['stats']
    if probe.get('key') is not None:
        name = probe['key']
        control = {'type': 'shape_key', 'mesh': probe['mesh'], 'key': name,
                   'min': probe['slider_min'], 'max': probe['slider_max'], 'full': probe['slider_max']}
    else:
        name = probe['bone']
        control = {'type': 'bone', 'bone': probe['bone'], 'path': probe['path'], 'index': probe['index'],
                   'full': probe['amount'], 'limit': probe.get('limit'), 'deform': probe.get('deform'),
                   'calibration': 'rig limit' if probe.get('limit') else 'default probe range (no rig limit)'}
    mixed, areas = mixed_areas(stats.get('zones'))
    rows = []
    for semantic, base, reason in rule_scores(stats, eye_sides):
        delta, notes = _name_adjust(name, semantic)
        confidence = max(0.0, min(0.97, 0.35 + 0.55 * base + delta))
        if mixed:
            confidence = min(confidence, 0.45)
            notes.append('MIXED_AREAS: deforms ' + mixed[0] + ' and ' + mixed[1] + ' together')
        if probe.get('driven'):
            confidence = min(confidence, 0.2)
            notes.append('DRIVEN: value is set by a driver; it cannot be keyed directly')
        if probe.get('driver_on_channel'):
            confidence = min(confidence, 0.2)
            notes.append('DRIVEN: bone channel has a driver')
        evidence = [reason, 'moved vertices ' + str(stats.get('moved_vertices', 0))] + notes
        row = {'semantic': semantic, 'confidence': round(confidence, 3), 'base_score': round(base, 3),
               'control': dict(control), 'evidence': evidence, 'mixed': bool(mixed)}
        if semantic.startswith('eye_'):
            eye = [k for k, v in (stats.get('eye_rotation_deg') or {}).items() if abs(v[0]) + abs(v[1]) > 0.5]
            if eye:
                yaw, pitch = stats['eye_rotation_deg'][eye[0]]
                row['control']['eye'] = eye[0]
                row['control']['degrees_at_full'] = yaw if semantic.startswith('eye_yaw') else pitch
        rows.append(row)
    return rows


def eye_sides(probe_result):
    eyes = probe_result.get('eyes') or []
    return {eyes[0]: 'L', eyes[1]: 'R'} if len(eyes) == 2 else {}


EYE_OUTPUTS = ('eye_yaw_L', 'eye_yaw_R', 'eye_pitch_L', 'eye_pitch_R')
RECIPE_OUTPUTS = ('lips_close', 'lip_pucker', 'lip_funnel')


def _eye_mapping(semantic, rows, aims):
    """Signed degree output: bone channel (linear), shape-key pair, or aim target."""
    rows = sorted(rows, key=_rank, reverse=True)
    best = rows[0]
    control = best['control']
    side = semantic[-1]
    if control['type'] == 'bone' and control['path'] == 'location' and aims.get(side):
        aim = aims[side]
        control = {'type': 'aim_target', 'bone': aim['target_bone'], 'path': 'location', 'eye': aim['eye_object']}
        return dict(best, control=control, method='aim_target', usable=best['confidence'] >= USABLE_CONFIDENCE,
                    aim=aim, alternatives=[_short(r) for r in rows[1:4]])
    if control['type'] == 'bone':
        factor = control['degrees_at_full'] / control['full'] if control['full'] else 0.0
        limit = control.get('limit')
        return dict(best, method='bone_rotation', usable=best['confidence'] >= USABLE_CONFIDENCE and factor != 0,
                    degrees_per_unit=factor, channel_limits=limit, alternatives=[_short(r) for r in rows[1:4]])
    positive = [r for r in rows if r['control']['type'] == 'shape_key' and r['control'].get('degrees_at_full', 0) > 0]
    negative = [r for r in rows if r['control']['type'] == 'shape_key' and r['control'].get('degrees_at_full', 0) < 0]
    if positive and negative:
        pos, neg = positive[0], negative[0]
        confidence = min(pos['confidence'], neg['confidence'])
        return {'semantic': semantic, 'method': 'shape_key_pair', 'confidence': confidence,
                'usable': confidence >= USABLE_CONFIDENCE, 'positive': pos['control'], 'negative': neg['control'],
                'control': pos['control'], 'evidence': pos['evidence'] + neg['evidence'],
                'alternatives': [_short(r) for r in rows if r is not pos and r is not neg][:3]}
    return dict(best, method='shape_key_single_direction', usable=False,
                evidence=best['evidence'] + ['only one gaze direction measured for this axis'],
                alternatives=[_short(r) for r in rows[1:4]])


def build_mapping(probe_result):
    """Semantic output -> chosen control(s) (+ alternatives), and per-control best semantic."""
    sides = eye_sides(probe_result)
    pool = []
    for probe in probe_result.get('key_probes', []):
        pool.extend(candidates(probe, sides))
    for probe in probe_result.get('bone_probes', []):
        if probe.get('deform') and probe.get('driven_by'):
            continue  # a deform bone copying a control is never animated directly
        pool.extend(candidates(probe, sides))
    aims = {}
    for aim in probe_result.get('aims', []):
        aims[aim['side']] = aim
    by_semantic = {}
    for row in pool:
        by_semantic.setdefault(row['semantic'], []).append(row)
    mapping = {}
    for semantic, rows in by_semantic.items():
        if semantic in EYE_OUTPUTS:
            mapping[semantic] = _eye_mapping(semantic, rows, aims)
            continue
        rows.sort(key=_rank, reverse=True)
        best = rows[0]
        entry = dict(best, usable=best['confidence'] >= USABLE_CONFIDENCE, alternatives=[_short(r) for r in rows[1:4]],
                     secondary=[])
        if semantic in RECIPE_OUTPUTS and best['control']['type'] == 'bone':
            # A bone rig may need two controls (upper lip down + lower lip up) for one semantic output.
            used = {best['control']['bone']}
            for other in rows[1:]:
                c = other['control']
                if other['confidence'] >= USABLE_CONFIDENCE and c['type'] == 'bone' and c['bone'] not in used:
                    entry['secondary'].append(c)
                    used.add(c['bone'])
                    entry['evidence'] = entry['evidence'] + ['combined with ' + _control_key(c)]
        mapping[semantic] = entry
    controls = {}
    for row in pool:
        key = _control_key(row['control'])
        if key not in controls or row['confidence'] > controls[key]['confidence']:
            controls[key] = {'semantic': row['semantic'], 'confidence': row['confidence'], 'control': row['control']}
    return mapping, controls, pool


def _rank(row):
    control = row['control']
    preference = 0.0
    if control['type'] == 'bone' and not control.get('deform'):
        preference += 0.005
    return row['confidence'] + preference


def _short(row):
    return {'control': _control_key(row['control']), 'confidence': row['confidence']}


def _control_key(control):
    if control['type'] == 'shape_key':
        return control['mesh'] + ':' + control['key']
    if control['type'] == 'aim_target':
        return control['bone'] + ':aim'
    return control['bone'] + ':' + control['path'] + '[' + str(control['index']) + ']' + ('+' if control['full'] > 0 else '-')


def displacing_controls(probe_result):
    """Controls that deform the face at all (keys and bone channel directions)."""
    keys = [p for p in probe_result.get('key_probes', []) if (p['stats'].get('moved_vertices') or 0) > 0]
    bones = {}
    for p in probe_result.get('bone_probes', []):
        if p.get('deform') and p.get('driven_by'):
            continue
        bones.setdefault(p['bone'], []).append(p)
    return keys, bones


def classify(probe_result, mapping, pool):
    """SHAPE_KEYS / BONES / HYBRID / CUSTOM / UNSUPPORTED / AMBIGUOUS with confidence and evidence."""
    keys, bones = displacing_controls(probe_result)
    usable = {k: v for k, v in mapping.items() if v['usable']}
    by_system = {'shape_key': set(), 'bone': set()}
    for semantic, row in usable.items():
        by_system['shape_key' if row['control']['type'] == 'shape_key' else 'bone'].add(semantic)
    evidence = ['displacing shape keys: ' + str(len(keys)), 'displacing control bones: ' + str(len(bones)),
                'usable semantic outputs: ' + str(len(usable))]
    limitations = []
    driven = [p for p in probe_result.get('key_probes', []) if p.get('driven')]
    if driven:
        limitations.append(str(len(driven)) + ' shape keys are driver-controlled and are not keyed directly')
    if not keys and not bones:
        return {'type': 'UNSUPPORTED', 'confidence': 0.95, 'evidence': evidence + ['no control deforms the face'],
                'limitations': ['No shape key or facial bone changes the facial geometry; nothing can be animated safely.']}
    best_per_control = {}
    for row in pool:
        key = _control_key(row['control'])
        best_per_control.setdefault(key, []).append(row['confidence'])
    weak = [k for k, v in best_per_control.items() if max(v) < USABLE_CONFIDENCE]
    mixed = sorted(set(_control_key(r['control']) for r in pool if r.get('mixed')))
    if mixed:
        evidence.append('controls deforming non-adjacent areas together: ' + str(len(mixed)))
    displacing = len(keys) + sum(1 for _ in bones)
    unmapped = displacing - len(best_per_control)
    ambiguous_ratio = (len(weak) + max(0, unmapped)) / max(1, len(best_per_control) + max(0, unmapped))
    evidence.append('controls without a confident semantic: ' + str(len(weak) + max(0, unmapped)))
    if len(usable) < 3 or ambiguous_ratio > 0.5:
        return {'type': 'AMBIGUOUS', 'confidence': round(min(0.95, 0.5 + ambiguous_ratio / 2), 3), 'evidence': evidence,
                'limitations': limitations + ['Most controls move several facial areas or none clearly; automatic mutation is blocked.']}
    if driven and len(driven) > 0.5 * max(1, len(keys)) and not bones:
        return {'type': 'CUSTOM', 'confidence': 0.6, 'evidence': evidence, 'limitations': limitations +
                ['Facial deformation is mostly driver-controlled; only the driving properties could be animated.']}
    kinds = [k for k, v in by_system.items() if v]
    total = sum(len(v) for v in by_system.values())
    if len(kinds) == 2:
        minor = min(len(v) for v in by_system.values())
        confidence = 0.6 + 0.35 * min(1.0, minor / 3)
        return {'type': 'HYBRID', 'confidence': round(confidence, 3), 'evidence': evidence +
                ['shape-key outputs: ' + str(len(by_system['shape_key'])), 'bone outputs: ' + str(len(by_system['bone']))],
                'limitations': limitations}
    kind = 'SHAPE_KEYS' if kinds == ['shape_key'] else 'BONES'
    return {'type': kind, 'confidence': round(min(0.97, 0.6 + 0.02 * total), 3), 'evidence': evidence, 'limitations': limitations}


def hybrid_plan(mapping, pool):
    """Which system drives each semantic output; conflicting duplicates are reported, never both driven."""
    preferred = {'JAW': 'bone', 'EYES': 'bone'}
    decisions = {}
    conflicts = []
    for group, outputs in GROUPS.items():
        for semantic in outputs:
            rows = [r for r in pool if r['semantic'] == semantic and r['confidence'] >= USABLE_CONFIDENCE]
            systems = sorted(set(r['control']['type'] for r in rows))
            if not rows:
                continue
            if len(systems) == 2:
                choice = preferred.get(group)
                best = {s: max((r for r in rows if r['control']['type'] == s), key=_rank) for s in systems}
                if choice is None:
                    choice = max(systems, key=lambda s: best[s]['confidence'])
                other = [s for s in systems if s != choice][0]
                conflicts.append({'semantic': semantic, 'chosen': _control_key(best[choice]['control']),
                                  'not_driven': _control_key(best[other]['control']),
                                  'reason': ('rigid ' + group.lower() + ' motion prefers the bone' if group in preferred
                                             else 'higher measured confidence')})
                decisions[semantic] = {'system': choice, 'control': _control_key(best[choice]['control'])}
            else:
                best = max(rows, key=_rank)
                decisions[semantic] = {'system': best['control']['type'], 'control': _control_key(best['control'])}
    return decisions, conflicts


CAPABILITIES = ('expressions', 'gaze', 'blink', 'lid_follow', 'brows', 'jaw', 'lips', 'lip_sync', 'facial_mocap',
                'microexpressions', 'head_neck', 'tongue')


def compatibility(mapping, classification, inspection):
    usable = {k for k, v in mapping.items() if v['usable']}
    report = {}

    def has(*names):
        return all(n in usable for n in names)

    def any_of(*names):
        return any(n in usable for n in names)
    if classification['type'] in ('UNSUPPORTED', 'AMBIGUOUS'):
        for cap in CAPABILITIES:
            report[cap] = {'status': 'UNSUPPORTED', 'reason': 'rig classified ' + classification['type']}
        return report
    expression_parts = [any_of('brow_inner_raise_L', 'brow_outer_raise_L', 'brow_lower_L'),
                        any_of('lid_close_L', 'lid_wide_L', 'lid_raise_L'),
                        any_of('lip_corner_up_L', 'lip_corner_down_L', 'lip_press_L', 'lips_close')]
    report['expressions'] = _status(sum(expression_parts), 3, 'brows, lids and mouth corners')
    report['gaze'] = ({'status': 'SUPPORTED', 'reason': 'yaw and pitch mapped for both eyes'}
                      if has('eye_yaw_L', 'eye_yaw_R', 'eye_pitch_L', 'eye_pitch_R') else
                      {'status': 'LIMITED', 'reason': 'only some eye axes mapped'} if any_of('eye_yaw_L', 'eye_yaw_R') else
                      {'status': 'UNSUPPORTED', 'reason': 'no eye rotation control measured'})
    report['blink'] = ({'status': 'SUPPORTED', 'reason': 'upper lid closing mapped per side'} if has('lid_close_L', 'lid_close_R')
                       else {'status': 'UNSUPPORTED', 'reason': 'no upper-lid closing control'})
    report['lid_follow'] = ({'status': 'SUPPORTED', 'reason': 'lids and eye pitch mapped'}
                            if has('lid_close_L', 'lid_close_R', 'eye_pitch_L', 'eye_pitch_R') else
                            {'status': 'UNSUPPORTED', 'reason': 'needs lid closing and eye pitch'})
    report['brows'] = _status(sum([any_of('brow_inner_raise_L', 'brow_inner_raise_R'), any_of('brow_outer_raise_L', 'brow_outer_raise_R'),
                                   any_of('brow_lower_L', 'brow_lower_R')]), 3, 'inner raise, outer raise and lowering')
    report['jaw'] = ({'status': 'SUPPORTED', 'reason': 'jaw opening mapped'} if has('jaw_open')
                     else {'status': 'UNSUPPORTED', 'reason': 'MISSING_JAW_CONTROL'})
    lip_parts = [has('lips_close'), any_of('lip_pucker', 'lip_funnel'), any_of('lip_stretch_L', 'lip_corner_up_L'),
                 any_of('lip_press_L', 'lip_roll_lower', 'lip_roll_upper')]
    report['lips'] = _status(sum(lip_parts), 4, 'closure, rounding, corners and compression')
    if has('jaw_open') and sum(lip_parts) >= 3:
        report['lip_sync'] = {'status': 'SUPPORTED', 'reason': 'jaw plus closure, rounding and corners; requires validated timing input'}
    elif has('jaw_open'):
        report['lip_sync'] = {'status': 'LIMITED', 'reason': 'jaw-only speech; lip shapes missing'}
    else:
        report['lip_sync'] = {'status': 'UNSUPPORTED', 'reason': 'no jaw control'}
    report['facial_mocap'] = ({'status': 'SUPPORTED_WITH_MAPPING', 'reason': 'channels retarget through the semantic model'}
                              if len(usable) >= 8 else {'status': 'LIMITED', 'reason': 'few semantic outputs to receive channels'})
    report['microexpressions'] = report['expressions'] if report['expressions']['status'] != 'UNSUPPORTED' else \
        {'status': 'UNSUPPORTED', 'reason': 'no expression controls'}
    head = inspection.get('head_bone')
    report['head_neck'] = ({'status': 'SUPPORTED', 'reason': 'head bone ' + head + (' and neck ' + inspection['neck_bone'] if inspection.get('neck_bone') else '')}
                           if head else {'status': 'UNSUPPORTED', 'reason': 'no head bone'})
    report['tongue'] = ({'status': 'SUPPORTED', 'reason': 'tongue control mapped'} if has('tongue_out')
                        else {'status': 'UNSUPPORTED', 'reason': 'no tongue control measured'})
    return report


def _status(count, total, what):
    if count >= total:
        return {'status': 'SUPPORTED', 'reason': what + ' mapped'}
    if count > 0:
        return {'status': 'LIMITED', 'reason': str(count) + '/' + str(total) + ' of ' + what + ' mapped'}
    return {'status': 'UNSUPPORTED', 'reason': 'none of ' + what + ' mapped'}


def analyze(probe_result, inspection=None):
    """Full evidence chain: mapping -> hybrid resolution -> classification -> compatibility."""
    mapping, controls, pool = build_mapping(probe_result)
    decisions, conflicts = hybrid_plan(mapping, pool)
    for semantic, decision in decisions.items():
        entry = mapping.get(semantic)
        if entry is None or semantic in EYE_OUTPUTS:
            continue
        current = 'shape_key' if entry['control']['type'] == 'shape_key' else 'bone'
        if current != decision['system']:
            rows = [r for r in pool if r['semantic'] == semantic and
                    ('shape_key' if r['control']['type'] == 'shape_key' else 'bone') == decision['system']]
            best = max(rows, key=_rank)
            mapping[semantic] = dict(best, usable=best['confidence'] >= USABLE_CONFIDENCE,
                                     alternatives=[_short(entry)] + entry.get('alternatives', [])[:2], secondary=[],
                                     evidence=best['evidence'] + ['hybrid rule: ' + decision['system'] + ' drives this output'])
    not_driven = sorted(set(c['not_driven'] for c in conflicts))
    classification = classify(probe_result, mapping, pool)
    compat = compatibility(mapping, classification, inspection or {'head_bone': probe_result.get('head_bone')})
    return {'mapping': mapping, 'controls': controls, 'pool': pool, 'hybrid': {'decisions': decisions, 'conflicts': conflicts,
            'not_driven': not_driven}, 'classification': classification, 'compatibility': compat}
