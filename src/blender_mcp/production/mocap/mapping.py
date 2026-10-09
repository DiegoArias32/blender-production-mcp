"""Skeleton mapping validation with server-computed confidence (M3.1, audit AUD-003).

Caller-supplied confidence is a hint, never authority: it can only LOWER the confidence computed
here from each rig's rest geometry and hierarchy. Hard conflicts block the mapping regardless of
approval. These functions are pure (plain lists/dicts) so they run both on the host and, via
``inspect.getsource``, inside Blender.

Rig data format (armature-local rest space)::

    {bone_name: {'parent': str | None, 'head': [x, y, z], 'tail': [x, y, z], 'length': float}}
"""
import inspect

REQUIRED_ROLES = ('root', 'pelvis', 'spine', 'head', 'left_upper_arm', 'left_lower_arm', 'left_hand',
                  'right_upper_arm', 'right_lower_arm', 'right_hand', 'left_upper_leg', 'left_lower_leg',
                  'left_foot', 'right_upper_leg', 'right_lower_leg', 'right_foot')


def map_vec_sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def map_vec_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def map_vec_cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def map_vec_norm(a):
    length = (a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) ** 0.5
    return [a[0] / length, a[1] / length, a[2] / length] if length > 1e-12 else None


def map_canonical_parent(role):
    """Roles allowed as the nearest *mapped* ancestor of a canonical role (None: must be a root)."""
    side = role.split('_', 1)[0] if role.startswith(('left_', 'right_')) else ''
    body = role[len(side) + 1:] if side else role
    chains = {'upper_leg': ['pelvis'], 'lower_leg': [side + '_upper_leg'], 'foot': [side + '_lower_leg'],
              'toe': [side + '_foot'], 'lower_arm': [side + '_upper_arm'], 'hand': [side + '_lower_arm']}
    if role == 'root':
        return [None]
    if role == 'pelvis':
        return ['root']
    if role == 'spine':
        return ['pelvis']
    if side and body in chains:
        return chains[body]
    if side and body == 'upper_arm':
        return ['spine', 'spine_*', 'chest', 'neck', side + '_shoulder', side + '_clavicle']
    if role in ('neck', 'head'):
        return ['spine', 'spine_*', 'chest', 'neck'] if role == 'head' else ['spine', 'spine_*', 'chest']
    return 'any'


def map_role_matches(role, allowed):
    for option in allowed:
        if option is None:
            if role is None:
                return True
        elif option.endswith('*'):
            if role is not None and role.startswith(option[:-1]):
                return True
        elif role == option:
            return True
    return False


def map_nearest_mapped_ancestor(bones, name, mapped_roles):
    parent = bones[name]['parent']
    while parent is not None:
        if parent in mapped_roles:
            return parent
        parent = bones[parent]['parent']
    return None


def map_body_frame(bones, role_to_bone):
    """Up from pelvis to head; forward from the average foot direction (independent of which foot
    carries which label, so a left/right swap cannot fool it); left = up x forward."""
    evidence = []
    up = None
    if role_to_bone.get('pelvis') in bones and role_to_bone.get('head') in bones:
        up = map_vec_norm(map_vec_sub(bones[role_to_bone['head']]['head'], bones[role_to_bone['pelvis']]['head']))
        evidence.append('up: pelvis->head')
    if up is None:
        up = [0.0, 0.0, 1.0]
        evidence.append('up: assumed armature +Z')
    forward = [0.0, 0.0, 0.0]
    for side in ('left', 'right'):
        for part in ('toe', 'foot'):
            bone = role_to_bone.get(side + '_' + part)
            if bone in bones:
                direction = map_vec_sub(bones[bone]['tail'], bones[bone]['head'])
                along = map_vec_dot(direction, up)
                forward = [forward[i] + direction[i] - along * up[i] for i in range(3)]
                break
    forward = map_vec_norm(forward)
    if forward is None:
        along = -up[1]
        forward = map_vec_norm([0.0 - along * up[0], -1.0 - along * up[1], 0.0 - along * up[2]]) or [0.0, -1.0, 0.0]
        evidence.append('forward: assumed armature -Y (feet directions degenerate)')
    else:
        evidence.append('forward: average foot direction')
    left = map_vec_norm(map_vec_cross(up, forward))
    heights = [map_vec_dot(bone['head'], up) for bone in bones.values()] + [map_vec_dot(bone['tail'], up) for bone in bones.values()]
    height = max(max(heights) - min(heights), 1e-9) if heights else 1.0
    center = bones[role_to_bone['pelvis']]['head'] if role_to_bone.get('pelvis') in bones else [0.0, 0.0, 0.0]
    return {'up': up, 'forward': forward, 'left': left, 'height': height, 'center': center, 'evidence': evidence}


def map_side(bones, name, frame, tolerance=0.02):
    offset = map_vec_dot(map_vec_sub(bones[name]['head'], frame['center']), frame['left'])
    if offset > tolerance * frame['height']:
        return 'left', offset
    if offset < -tolerance * frame['height']:
        return 'right', offset
    return 'center', offset


def map_name_side(name):
    tokens = ''.join(c if c.isalnum() else ' ' for c in name.lower()).split()
    if any(t in ('left', 'l') for t in tokens):
        return 'left'
    if any(t in ('right', 'r') for t in tokens):
        return 'right'
    return None


def map_role_side(role):
    return 'left' if role.startswith('left_') else 'right' if role.startswith('right_') else None


def mapping_evidence(rows, source_bones, target_bones):
    """Per-row server-computed confidence, evidence and hard conflicts."""
    source_roles = {row['source_bone']: row['semantic_role'] for row in rows if row['source_bone'] in source_bones}
    target_roles = {row['target_bone']: row['semantic_role'] for row in rows if row['target_bone'] in target_bones}
    role_to_source = {row['semantic_role']: row['source_bone'] for row in rows}
    role_to_target = {row['semantic_role']: row['target_bone'] for row in rows}
    source_frame = map_body_frame(source_bones, role_to_source)
    target_frame = map_body_frame(target_bones, role_to_target)
    output = []
    for row in rows:
        role, source, target = row['semantic_role'], row['source_bone'], row['target_bone']
        evidence, conflicts = [], []
        if source not in source_bones or target not in target_bones:
            output.append({'semantic_role': role, 'computed_confidence': 0.0, 'evidence': evidence,
                           'conflicts': ['MISSING_BONE: source or target bone does not exist']})
            continue
        score = 0.0
        # Hierarchy: nearest mapped ancestor must carry the canonical parent role, in both rigs.
        allowed = map_canonical_parent(role)
        ancestor_roles = []
        for bones, roles, bone, label in ((source_bones, source_roles, source, 'source'),
                                          (target_bones, target_roles, target, 'target')):
            ancestor = map_nearest_mapped_ancestor(bones, bone, roles)
            ancestor_role = roles.get(ancestor) if ancestor else None
            ancestor_roles.append(ancestor_role)
            if allowed != 'any' and not map_role_matches(ancestor_role, allowed):
                conflicts.append('HIERARCHY_CONFLICT: ' + label + ' bone ' + bone + ' sits under role '
                                 + str(ancestor_role) + ', expected ' + ' / '.join(str(a) for a in allowed))
        if ancestor_roles[0] != ancestor_roles[1]:
            conflicts.append('HIERARCHY_CONFLICT: nearest mapped ancestor differs (' + str(ancestor_roles[0]) + ' vs '
                             + str(ancestor_roles[1]) + ')')
        if not any(c.startswith('HIERARCHY') for c in conflicts):
            score += 0.6
            evidence.append('hierarchy: nearest mapped ancestor ' + str(ancestor_roles[0]) + ' in both rigs')
        # Side from rest geometry in each rig's own body frame.
        source_side, source_offset = map_side(source_bones, source, source_frame)
        target_side, target_offset = map_side(target_bones, target, target_frame)
        expected = map_role_side(role)
        if expected:
            if source_side == expected and target_side == expected:
                score += 0.25
                evidence.append('side: both bones geometrically ' + expected)
            for side, label, bone in ((source_side, 'source', source), (target_side, 'target', target)):
                if side in ('left', 'right') and side != expected:
                    conflicts.append('SIDE_MISMATCH: ' + label + ' bone ' + bone + ' is geometrically ' + side
                                     + ' but the role is ' + expected)
        elif role in ('root', 'pelvis', 'spine', 'neck', 'head') or role.startswith('spine_'):
            if source_side == 'center' and target_side == 'center':
                score += 0.25
                evidence.append('side: both bones central')
            else:
                conflicts.append('CENTRAL_ROLE_LATERAL: role ' + role + ' mapped to a lateral bone ('
                                 + source_side + ' / ' + target_side + ')')
        elif source_side == target_side:
            score += 0.25
            evidence.append('side: both bones ' + source_side)
        else:
            conflicts.append('SIDE_MISMATCH: source bone is ' + source_side + ', target bone is ' + target_side)
        # Proportion relative to each rig's height.
        source_ratio = source_bones[source]['length'] / source_frame['height']
        target_ratio = target_bones[target]['length'] / target_frame['height']
        spread = max(source_ratio, target_ratio) / max(min(source_ratio, target_ratio), 1e-9)
        if spread > 3.0:
            conflicts.append('PROPORTION_OUTLIER: relative bone length differs ' + str(round(spread, 2)) + 'x')
        elif spread <= 2.0:
            score += 0.1
            evidence.append('proportion: relative length ratio ' + str(round(spread, 3)))
        # Naming is weak evidence only: geometry decides.
        for bone in (source, target):
            named = map_name_side(bone)
            if named and expected and named != expected:
                score -= 0.1
                evidence.append('naming contradicts role side: ' + bone)
            elif named and expected:
                score += 0.025
        confidence = 0.0 if conflicts else round(min(0.99, max(0.0, score)), 3)
        output.append({'semantic_role': role, 'computed_confidence': confidence, 'evidence': evidence,
                       'conflicts': conflicts, 'geometry': {'source_side': source_side, 'target_side': target_side}})
    return {'rows': output, 'source_frame': source_frame, 'target_frame': target_frame}


def validate_mapping(rows, source_names, target_names, approved=False, minimum_confidence=0.9,
                     source_bones=None, target_bones=None):
    """Structural validation plus server-computed confidence.

    Without rig data (``source_bones``/``target_bones``) confidence cannot be computed and the mapping
    can never validate: a caller-declared number is not evidence.
    """
    required = set(REQUIRED_ROLES)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
        raise ValueError('mapping must contain 1..256 reviewed rows')
    if isinstance(minimum_confidence, bool) or not 0.9 <= minimum_confidence <= 1:
        raise ValueError('minimum_confidence must be between 0.9 and 1')
    errors, warnings, roles, sources, targets = [], [], set(), set(), set()
    clean = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {'source_bone', 'target_bone', 'semantic_role', 'confidence', 'evidence'}:
            raise ValueError('Invalid mapping row fields')
        role, source, target = row.get('semantic_role'), row.get('source_bone'), row.get('target_bone')
        hint = row.get('confidence')
        if not isinstance(role, str) or not isinstance(source, str) or not isinstance(target, str):
            raise ValueError('Mapping identifiers must be strings')
        if hint is not None and (isinstance(hint, bool) or not isinstance(hint, (int, float)) or not 0 <= hint <= 1):
            raise ValueError('Mapping confidence hint must be finite and between 0 and 1')
        if role in roles or source in sources or target in targets:
            errors.append('Mapping must be one-to-one with unique roles: ' + role)
        roles.add(role); sources.add(source); targets.add(target)
        if source not in source_names or target not in target_names:
            errors.append('Missing source or target bone: ' + role)
        evidence = row.get('evidence', [])
        if evidence is not None and (not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence)):
            errors.append('Mapping evidence must be a list of strings: ' + role)
        clean.append({'source_bone': source, 'target_bone': target, 'semantic_role': role,
                      'caller_hint': hint, 'caller_evidence': list(evidence or [])})
    for role in sorted(required - roles):
        errors.append('Required mapping missing: ' + role)
    computed = None
    if source_bones is not None and target_bones is not None and not errors:
        computed = mapping_evidence([{'source_bone': r['source_bone'], 'target_bone': r['target_bone'],
                                      'semantic_role': r['semantic_role']} for r in clean], source_bones, target_bones)
        for row, evidence in zip(clean, computed['rows']):
            effective = evidence['computed_confidence']
            if row['caller_hint'] is not None:
                effective = min(effective, row['caller_hint'])
            row.update(confidence=effective, computed_confidence=evidence['computed_confidence'],
                       computed_by='production.mapping_evidence', evidence=evidence['evidence'],
                       conflicts=evidence['conflicts'], geometry=evidence.get('geometry'),
                       caller_hint_used_as='non_authoritative: may only lower the computed confidence')
            errors.extend(evidence['conflicts'])
            if effective < minimum_confidence and not evidence['conflicts']:
                errors.append('LOW_CONFIDENCE_MAPPING: ' + row['semantic_role'] + ' computed '
                              + str(evidence['computed_confidence']) + (' (lowered by caller hint)' if effective < evidence['computed_confidence'] else ''))
    elif not errors:
        errors.append('CONFIDENCE_NOT_COMPUTED: rig data is required; caller-declared confidence is not evidence')
    if approved is not True:
        errors.append('mapping_approved=true is required after source/target mapping review')
    result = {'validated': not errors, 'supported': not errors, 'mapping': clean,
              'required_roles': sorted(required), 'errors': errors, 'warnings': warnings,
              'minimum_confidence': minimum_confidence, 'mapping_approved': approved is True,
              'confidence_model': 'server-computed from hierarchy, rest-geometry side, central alignment and proportion; caller hints can only lower it'}
    if computed is not None:
        result['body_frames'] = {'source': {k: computed['source_frame'][k] for k in ('up', 'forward', 'left', 'evidence')},
                                 'target': {k: computed['target_frame'][k] for k in ('up', 'forward', 'left', 'evidence')}}
    return result


_FUNCTIONS = (map_vec_sub, map_vec_dot, map_vec_cross, map_vec_norm, map_canonical_parent, map_role_matches,
              map_nearest_mapped_ancestor, map_body_frame, map_side, map_name_side, map_role_side,
              mapping_evidence, validate_mapping)
MAPPING_RUNTIME = ('REQUIRED_ROLES = ' + repr(REQUIRED_ROLES) + '\n\n'
                   + '\n\n'.join(inspect.getsource(function) for function in _FUNCTIONS))
