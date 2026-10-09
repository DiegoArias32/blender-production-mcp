"""M4 character modeling & redesign services (host side).

Analysis tools only read the mesh. Measurements are kept apart from interpretation: no tool produces
a design or quality score. Mutation happens only through character_create_candidate (a copy) and
character_adjust_shape (edits on that copy), both behind the M3.1 asset guard, checkpoints and ledger.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ..common.asset_safety import current_asset, require_mutable_asset
from ..common.storage import write_json
from ..mesh import service as mesh_service
from ..mesh.runtime import REGION_CAPTURE, SIGNATURE
from ..qa.png_montage import write_montage
from .edit import EDIT_RUNTIME
from .render import RENDER
from .runtime import ASYMMETRY, BONES, INSPECT, INTERSECTIONS, REGIONS, SILHOUETTE, SURFACE
from .transaction import adjust_shape, validate_operation

VIEWS = ('FRONT', 'SIDE', 'THREE_QUARTER', 'BACK')
ROLES = ('locked', 'editable', 'unassigned')
SURFACE_DEFAULTS = {'robust_k': 6.0, 'min_bump': 0.05, 'min_gradient': 0.2, 'pinch_density': 0.9, 'density_ratio': 2.5,
                    'normal_deviation_degrees': 30.0, 'stretch_ratio': 6.0, 'faceting_degrees': 40.0, 'min_cluster': 3,
                    'limit': 50}

PLAN_ANALYZE = EDIT_RUNTIME + r'''
obj = cr_object(params['object_name'])
field = cr_field(obj, params['operation'], params['editable'], params['locked'])
result = {'stats': field['stats'], 'shape_keys': cr_shape_key_safety(obj.data, field['edited'], params['shape_keys'])}
'''


# ------------------------------------------------------------------------------------------ helpers

def _name(value, label='object_name'):
    if not isinstance(value, str) or not value:
        raise ValueError(label + ' must be a nonempty object name')
    return value


def _views(views):
    views = list(views or ('FRONT', 'SIDE', 'THREE_QUARTER'))
    if not views or len(views) > 4 or len(set(views)) != len(views) or any(v not in VIEWS for v in views):
        raise ValueError('views must be 1..4 unique values of ' + ', '.join(VIEWS))
    return views


def _region_defs(bridge, storage, object_name, manifest):
    """Region definitions for analysis: the manifest's when given, else automatic candidates."""
    if manifest:
        data = mesh_service.load_manifest(storage, manifest)
        return [{'region_id': r['name'], 'definition': r['definition']} for r in data['regions']], data
    inferred = bridge.run(REGIONS, {'object_name': object_name})['regions']
    return [{'region_id': r['region_id'], 'definition': r['definition']} for r in inferred], None


# ------------------------------------------------------------------------------------------ inspect

def inspect(bridge, storage, object_name, mirror_fraction=0.005):
    _name(object_name)
    if isinstance(mirror_fraction, bool) or not isinstance(mirror_fraction, (int, float)) or not 0 < mirror_fraction <= 0.1:
        raise ValueError('mirror_fraction must be in (0, 0.1] of the character height')
    data = bridge.run(INSPECT, {'object_name': object_name, 'mirror_fraction': mirror_fraction})
    # Read-only: the compact signature summary is returned, the full signature is not stored here
    # (production.mesh_signature stores it when a persistent record is wanted).
    data['mesh_signature'] = mesh_service.summary(data.pop('signature'))
    data['asset'] = current_asset(bridge, storage)
    data['regions'] = [{k: r[k] for k in ('region_id', 'vertex_count', 'confidence', 'evidence', 'source', 'ambiguous', 'overlap_fraction', 'definition')}
                       for r in data['regions']]
    warnings = []
    topology = data['topology_warnings']
    if topology['non_manifold_edges'] or topology['loose_vertices'] or topology['degenerate_faces']:
        warnings.append('Topology warnings found; topology repair is M5 scope, M4 edits preserve topology as is.')
    if any(r['ambiguous'] for r in data['regions']):
        warnings.append('Some automatic regions are ambiguous (confidence < 0.6): confirm regions before any edit.')
    data['warnings'] = warnings
    data['status'] = 'warning' if warnings else 'success'
    return data


# ------------------------------------------------------------------------------------------ regions

def _validate_region_rows(rows):
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError('regions must be a list of at most 32 region definitions')
    output = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {'region_id', 'role', 'definition', 'tolerance_m', 'intentional', 'note'}:
            raise ValueError('A user region needs region_id, definition and optional role, tolerance_m, intentional, note')
        if not isinstance(row.get('region_id'), str) or not row['region_id']:
            raise ValueError('region_id must be a nonempty string')
        role = row.get('role', 'unassigned')
        if role not in ROLES:
            raise ValueError('role must be locked, editable or unassigned')
        if not isinstance(row.get('intentional', False), bool):
            raise ValueError('intentional must be boolean')
        output.append(row)
    return output


def _capture_manifest(bridge, storage, operation_id, object_name, regions, kind):
    """Capture every region on the object now and store a mesh-region manifest with character metadata."""
    plain = [{'name': r['name'], 'role': r['role'], 'definition': r['definition'], 'tolerance_m': r.get('tolerance_m', 1e-5)}
             for r in regions]
    mesh_service.validate_regions(plain)
    captured = bridge.run(REGION_CAPTURE, {'object_name': object_name,
                                           'regions': [{'name': r['name'], 'definition': r['definition']} for r in regions]})['regions']
    signature = bridge.run(SIGNATURE, {'object_name': object_name})
    rows = []
    for region, capture in zip(regions, captured):
        rows.append(dict(region, tolerance_m=float(region.get('tolerance_m', 1e-5)), capture=capture))
    manifest = {'schema': 'production.mesh_regions.v1', 'kind': kind, 'object': object_name, 'signature': signature,
                'regions': rows}
    path = mesh_service._store(storage, 'regions', operation_id, 'manifest.json', manifest)
    return path, manifest


def _region_summary(manifest, path):
    return {'status': 'success', 'manifest': path, 'object': manifest['object'],
            'topology_fingerprint': manifest['signature']['sections']['topology']['fingerprint'],
            'regions': [{'region_id': r['name'], 'role': r['role'], 'source': r['source'], 'confidence': r['confidence'],
                         'ambiguous': r.get('ambiguous', False), 'intentional': r.get('intentional', False),
                         'method': r['definition']['method'], 'vertices': len(r['capture']['indices']),
                         'faces': r['capture'].get('face_count'), 'evidence': r.get('evidence', [])}
                        for r in manifest['regions']]}


def define_regions(bridge, storage, operation_id, object_name, regions=None, include_automatic=True):
    """Automatic candidates plus explicit user regions; a user region overrides an automatic one with the same id."""
    _name(object_name)
    user = _validate_region_rows(regions or [])
    rows = {}
    if include_automatic:
        for row in bridge.run(REGIONS, {'object_name': object_name})['regions']:
            if row['vertex_count']:
                rows[row['region_id']] = {'name': row['region_id'], 'role': 'unassigned', 'definition': row['definition'],
                                          'source': 'automatic', 'confidence': row['confidence'], 'evidence': row['evidence'],
                                          'ambiguous': row['ambiguous'], 'intentional': False}
    for row in user:
        rows[row['region_id']] = {'name': row['region_id'], 'role': row.get('role', 'unassigned'), 'definition': row['definition'],
                                  'source': 'user', 'confidence': 1.0, 'evidence': ['explicit user definition'] + ([row['note']] if row.get('note') else []),
                                  'ambiguous': False, 'intentional': row.get('intentional', False),
                                  'tolerance_m': row.get('tolerance_m', 1e-5)}
    if not rows:
        raise ValueError('REGION_EMPTY: no automatic or user region was found')
    path, manifest = _capture_manifest(bridge, storage, operation_id, object_name, list(rows.values()), 'character')
    empty = [r['name'] for r in manifest['regions'] if not r['capture']['indices']]
    result = _region_summary(manifest, path)
    if empty:
        result['warnings'] = ['Regions without vertices: ' + ', '.join(empty)]
        result['status'] = 'warning'
    return result


def lock_regions(bridge, storage, operation_id, manifest, lock=None, editable=None, unassign=None, object_name=None,
                 tolerance_m=None, intentional=None):
    """New manifest: roles changed as requested and every region recaptured on the object *now*."""
    data = mesh_service.load_manifest(storage, manifest)
    changes = {}
    for role, names in (('locked', lock), ('editable', editable), ('unassigned', unassign)):
        for name in names or []:
            if name in changes:
                raise ValueError('Region listed twice: ' + name)
            changes[name] = role
    known = {r['name'] for r in data['regions']}
    missing = sorted(set(changes) - known)
    if missing:
        raise ValueError('REGION_UNDEFINED: ' + ', '.join(missing))
    rows = []
    for region in data['regions']:
        row = {k: v for k, v in region.items() if k != 'capture'}
        if region['name'] in changes:
            row['role'] = changes[region['name']]
            if changes[region['name']] == 'editable' and row.get('source') == 'automatic' and (row.get('ambiguous') or row.get('confidence', 0) < 0.7):
                raise ValueError('REGION_AMBIGUOUS: ' + row['name'] + ' is an automatic low-confidence region; redefine it as a user region to edit it')
        if tolerance_m is not None:
            row['tolerance_m'] = float(tolerance_m)
        if intentional and region['name'] in intentional:
            row['intentional'] = True
        rows.append(row)
    path, fresh = _capture_manifest(bridge, storage, operation_id, object_name or data['object'], rows, 'character')
    result = _region_summary(fresh, path)
    result['previous_manifest'] = str(manifest)
    return result


def validate_locked_regions(bridge, storage, manifest, object_name=None):
    return mesh_service.verify_regions(bridge, storage, manifest, object_name)


# ------------------------------------------------------------------------------------------ silhouette / proportions

def analyze_silhouette(bridge, storage, object_name, views=None, rows=200, manifest=None):
    _name(object_name)
    if isinstance(rows, bool) or not isinstance(rows, int) or not 50 <= rows <= 600:
        raise ValueError('rows must be an integer 50..600')
    regions, _ = _region_defs(bridge, storage, object_name, manifest)
    data = bridge.run(SILHOUETTE, {'object_name': object_name, 'views': _views(views), 'rows': rows, 'regions': regions})
    data['named_measurements'] = named_measurements(data)
    data['observations'] = silhouette_observations(data)
    data['status'] = 'success'
    return data


def _union_extent(view, names):
    extents = [view['regions'][n] for n in names if view['regions'].get(n)]
    if not extents:
        return None
    return {'u_min': min(e['u_min'] for e in extents), 'u_max': max(e['u_max'] for e in extents),
            'v_min': min(e['v_min'] for e in extents), 'v_max': max(e['v_max'] for e in extents)}


def named_measurements(data):
    """Region-based measurements in meters; absent when the regions are absent (never guessed)."""
    front = next((v for v in data['views'] if v['view'] == 'FRONT'), None)
    side = next((v for v in data['views'] if v['view'] == 'SIDE'), None)
    out = {'overall_height_m': data['world_bounds']['size'][2], 'overall_width_m': data['world_bounds']['size'][0],
           'overall_depth_m': data['world_bounds']['size'][1]}
    if front:
        shoulders = _union_extent(front, ['SHOULDERS', 'LEFT_SHOULDER', 'RIGHT_SHOULDER', 'SHOULDER'])
        out['shoulder_width_m'] = shoulders['u_max'] - shoulders['u_min'] if shoulders else None
        torso = _union_extent(front, ['TORSO', 'CHEST'])
        out['torso_width_m'] = torso['u_max'] - torso['u_min'] if torso else None
        out['waist_width_m'] = front.get('waist_width_m')
        pelvis = _union_extent(front, ['PELVIS'])
        out['pelvis_width_m'] = pelvis['u_max'] - pelvis['u_min'] if pelvis else None
        head = _union_extent(front, ['HEAD'])
        out['head_height_m'] = head['v_max'] - head['v_min'] if head else None
        out['head_body_ratio'] = out['head_height_m'] / out['overall_height_m'] if out['head_height_m'] and out['overall_height_m'] else None
        arms = _union_extent(front, ['LEFT_ARM', 'RIGHT_ARM', 'LEFT_HAND', 'RIGHT_HAND', 'HANDS'])
        out['limb_projection_beyond_torso_m'] = (max(abs(arms['u_min']), abs(arms['u_max'])) - (out['torso_width_m'] or 0) / 2) if arms else None
        out['front_left_area_fraction'] = front['left_area_fraction']
        out['front_negative_space_holes'] = front['negative_space']['holes']
        out['front_extremity_reach_m'] = front['extremity_reach_m']
    if side:
        back = _union_extent(side, ['UPPER_BACK', 'BACK'])
        out['upper_back_depth_extent_m'] = back['u_max'] - back['u_min'] if back else None
        out['side_extremity_reach_m'] = side['extremity_reach_m']
    return out


def silhouette_observations(data):
    notes = []
    for view in data['views']:
        left = view['left_area_fraction']
        if abs(left - 0.5) > 0.08 and view['view'] in ('FRONT', 'BACK'):
            notes.append(view['view'] + ': ' + format(left * 100, '.1f') + '% of the silhouette area is on the left half (measured, not a defect)')
        if view['negative_space']['holes']:
            notes.append(view['view'] + ': ' + str(view['negative_space']['holes']) + ' enclosed negative-space region(s), '
                         + format(view['negative_space']['hole_area_m2'], '.4f') + ' m2')
    return notes


HUMANOID_REFERENCE = {
    # Informational figure-drawing canon, never a correction target. Sources: the academic canon of
    # 7.5 heads average / 8 heads idealized, shoulders about 2 heads wide, legs about half the height;
    # arm length derived from the Vitruvian rule "arm span = height": (7.5 - 2) / 2 / 7.5 = 0.37.
    'height_in_heads': (7.5, 7.0, 8.0),
    'shoulder_width_in_heads': (2.0, 1.5, 2.5),
    'arm_length_to_height': (0.37, 0.33, 0.41),
    'leg_length_to_height': (0.47, 0.42, 0.52),
}
REFERENCE_SOURCE = 'figure-drawing canon approximation (heads, Vitruvian arm span); informational only'


def limb_lengths(bones):
    """Arm = ARM + HAND bones; leg = mostly vertical LEG bones (a horizontal hip connector is not leg length)."""
    lengths = {}
    for info in bones.values():
        side = info['side']
        if side not in ('LEFT', 'RIGHT'):
            continue
        if info['region'] in ('ARM', 'HAND'):
            lengths['arm_' + side] = lengths.get('arm_' + side, 0.0) + info['length_m']
        elif info['region'] == 'LEG' and info['vertical_fraction'] >= 0.5:
            lengths['leg_' + side] = lengths.get('leg_' + side, 0.0) + info['length_m']
    return lengths


def analyze_proportions(bridge, storage, object_name, profile='HUMANOID', manifest=None, custom_reference=None):
    if profile not in ('HUMANOID', 'CREATURE', 'CUSTOM'):
        raise ValueError('profile must be HUMANOID, CREATURE or CUSTOM')
    if custom_reference is not None and (not isinstance(custom_reference, dict) or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in custom_reference.values())):
        raise ValueError('custom_reference must map measurement names to positive numbers')
    silhouette = analyze_silhouette(bridge, storage, object_name, ['FRONT', 'SIDE'], 200, manifest)
    named = silhouette['named_measurements']
    rig = bridge.run(BONES, {'object_name': object_name})
    limbs = limb_lengths(rig['bones'])
    height = named['overall_height_m']
    head = named.get('head_height_m')
    measured = {}
    if head:
        measured['height_in_heads'] = (height / head, 'silhouette HEAD region')
        if named.get('shoulder_width_m'):
            measured['shoulder_width_in_heads'] = (named['shoulder_width_m'] / head, 'silhouette SHOULDER regions')
    for key, value in limbs.items():
        limb, side = key.split('_')
        measured[limb + '_length_to_height_' + side] = (value / height, 'bone lengths of ' + str(rig['rig']))
    rows = []
    for key, (value, source) in sorted(measured.items()):
        base = key.rsplit('_', 1)[0] if key.endswith(('_LEFT', '_RIGHT')) else key
        row = {'measurement': key, 'value': value, 'source': source}
        if profile == 'HUMANOID' and base in HUMANOID_REFERENCE:
            ref, low, high = HUMANOID_REFERENCE[base]
            row.update(reference=ref, reference_range=[low, high], reference_source=REFERENCE_SOURCE, deviation=value / ref - 1.0,
                       interpretation='within the informational range' if low <= value <= high else
                       ('above' if value > high else 'below') + ' the informational humanoid range; may be an intentional design choice')
        elif profile == 'CUSTOM' and custom_reference and base in custom_reference:
            ref = float(custom_reference[base])
            row.update(reference=ref, reference_source='user-provided', deviation=value / ref - 1.0,
                       interpretation='compared with the user reference only')
        else:
            row.update(reference=None, deviation=None, interpretation='measured; no reference applied for this profile')
        row['confidence'] = 0.9 if source.startswith('bone') else (0.8 if manifest else 0.5)
        rows.append(row)
    pairs = []
    for limb in ('arm', 'leg'):
        left, right = limbs.get(limb + '_LEFT'), limbs.get(limb + '_RIGHT')
        if left and right:
            pairs.append({'measurement': limb + '_length', 'left_m': left, 'right_m': right, 'left_to_right_ratio': left / right})
    return {'status': 'success', 'object': object_name, 'profile': profile, 'measurements': rows, 'left_right': pairs,
            'named_measurements': named, 'design_recommendations': [],
            'notes': ['MEASUREMENT only. References are informational; exaggerated or non-human proportions are never corrected automatically.']
                     + (['CREATURE profile: no human reference is applied.'] if profile == 'CREATURE' else [])}


# ------------------------------------------------------------------------------------------ asymmetry / surface / intersections

ASYMMETRY_THRESHOLDS = {'likely_intentional_log_deviation': 0.22, 'accidental_low': 0.015, 'accidental_high': 0.10,
                        'symmetric_fraction_for_accidental': 0.9, 'single_region_feature_fraction': 0.5}


def classify_asymmetry(data, intentional):
    t = ASYMMETRY_THRESHOLDS
    symmetric = data['mirror']['symmetric_fraction']
    rows = []
    for pair in data['pairs']:
        deviation = pair['log_deviation']
        declared = pair['left'] in intentional or pair['right'] in intentional
        if declared:
            kind = 'intentional'
        elif deviation is None:
            kind = 'unknown'
        elif deviation >= t['likely_intentional_log_deviation']:
            kind = 'likely_intentional'
        elif t['accidental_low'] <= deviation < t['accidental_high'] and symmetric >= t['symmetric_fraction_for_accidental']:
            kind = 'likely_accidental'
        elif deviation < t['accidental_low']:
            kind = 'symmetric'
        else:
            kind = 'unknown'
        rows.append(dict(pair, classification=kind))
    features = []
    for name, region in data['regions'].items():
        if name.startswith(('LEFT_', 'RIGHT_')):
            continue
        if region['mirror_mismatch_fraction'] >= t['single_region_feature_fraction']:
            kind = 'intentional' if name in intentional else 'likely_intentional'
            features.append({'region': name, 'mirror_mismatch_fraction': region['mirror_mismatch_fraction'], 'classification': kind})
    return rows, features


def analyze_asymmetry(bridge, storage, object_name, manifest=None, intentional_regions=None, tolerance_fraction=0.005):
    regions, data = _region_defs(bridge, storage, object_name, manifest)
    intentional = set(intentional_regions or [])
    if data:
        intentional.update(r['name'] for r in data['regions'] if r.get('intentional'))
    measured = bridge.run(ASYMMETRY, {'object_name': object_name, 'regions': regions, 'intentional': sorted(intentional),
                                      'tolerance_fraction': tolerance_fraction})
    pairs, features = classify_asymmetry(measured, intentional)
    return {'status': 'success', 'object': object_name, 'mirror': measured['mirror'], 'pairs': pairs,
            'asymmetric_features': features, 'regions': measured['regions'], 'thresholds': ASYMMETRY_THRESHOLDS,
            'notes': ['Heuristic classification; symmetry is not assumed desirable and nothing is corrected automatically.']}


def analyze_surface(bridge, storage, object_name, manifest=None, options=None):
    settings = dict(SURFACE_DEFAULTS)
    for key, value in (options or {}).items():
        if key not in settings or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Unknown or invalid surface option: ' + str(key))
        settings[key] = int(value) if key in ('min_cluster', 'limit') else float(value)
    regions, _ = _region_defs(bridge, storage, object_name, manifest)
    data = bridge.run(SURFACE, {'object_name': object_name, 'options': settings, 'regions': regions})
    data['status'] = 'success'
    data['notes'] = ['Analysis only; thresholds are relative to this mesh (median + k*MAD) with absolute floors. No retopology.']
    return data


def analyze_intersections(bridge, storage, object_name, manifest=None, reference_object=None):
    regions, _ = _region_defs(bridge, storage, object_name, manifest)
    data = bridge.run(INTERSECTIONS, {'object_name': object_name, 'regions': regions, 'reference': reference_object})
    data['status'] = 'success'
    return data


# ------------------------------------------------------------------------------------------ audit / plan

SUGGESTIONS = {'unexpected_bump': 'topology-preserving smooth on the region', 'local_pinching': 'topology-preserving inflate or relax',
               'abrupt_curvature_change': 'topology-preserving smooth transition', 'noisy_surface': 'topology-preserving relax/smooth',
               'stretched_geometry': 'tangential relax; larger fixes are M5 retopology', 'inconsistent_density': 'M5 retopology scope (not an M4 edit)',
               'normal_irregularity': 'relax; check custom normals before any change', 'faceting_risk': 'review sharp edges/shading; not a remodel',
               'suspicious_flattening': 'topology-preserving inflate if unintended'}


def modeling_audit(bridge, storage, object_name, manifest=None, profile='HUMANOID', include_intersections=True):
    report = inspect(bridge, storage, object_name)
    surface = analyze_surface(bridge, storage, object_name, manifest)
    asymmetry = analyze_asymmetry(bridge, storage, object_name, manifest)
    proportions = analyze_proportions(bridge, storage, object_name, profile, manifest)
    intersections = analyze_intersections(bridge, storage, object_name, manifest) if include_intersections else None
    findings = []
    for row in surface['findings']:
        findings.append({'type': 'surface_transition' if row['type'] == 'abrupt_curvature_change' else row['type'],
                         'region': row['region'], 'severity': row['severity'],
                         'evidence': {k: row[k] for k in ('vertices', 'centroid_world_m', 'max_ratio_to_threshold')},
                         'suggested_action': SUGGESTIONS.get(row['type'])})
    for pair in asymmetry['pairs']:
        if pair['classification'] == 'likely_accidental':
            findings.append({'type': 'asymmetry', 'region': pair['left'] + ' / ' + pair['right'], 'severity': 'low',
                             'evidence': {k: pair[k] for k in ('surface_area_ratio', 'bbox_volume_ratio', 'log_deviation')},
                             'suggested_action': 'review; a mirrored topology-preserving adjustment only if unintended'})
    if intersections and intersections['overlap_pairs']:
        findings.append({'type': 'self_intersection', 'region': ', '.join(sorted(intersections['by_region_pair'])) or None,
                         'severity': 'high' if intersections['overlap_pairs'] > 50 else 'medium',
                         'evidence': {k: intersections[k] for k in ('overlap_pairs', 'by_region_pair', 'sample_locations_world_m')},
                         'suggested_action': 'contract or translate the penetrating region (topology-preserving)'})
    topology = report['topology_warnings']
    for key in ('non_manifold_edges', 'loose_vertices', 'degenerate_faces'):
        if topology[key]:
            findings.append({'type': 'topology_warning', 'region': None, 'severity': 'medium', 'evidence': {key: topology[key]},
                             'suggested_action': 'M5 retopology scope; not an M4 edit'})
    if report['deformation']['unweighted_vertices']:
        findings.append({'type': 'skinning_gap', 'region': None, 'severity': 'medium',
                         'evidence': {'unweighted_vertices': report['deformation']['unweighted_vertices']},
                         'suggested_action': 'weight review (outside M4 shape edits)'})
    if manifest:
        locks = validate_locked_regions(bridge, storage, manifest, object_name)
        if not locks['passed']:
            findings.append({'type': 'locked_region_changed', 'region': None, 'severity': 'high', 'evidence': {'errors': locks['errors']},
                             'suggested_action': 'revert the last edit or redefine the lock intentionally'})
    notes = [row['measurement'] + ': ' + row['interpretation'] for row in proportions['measurements'] if row.get('reference') is not None]
    return {'status': 'success', 'object': object_name, 'findings': findings, 'findings_count': len(findings),
            'observations': notes, 'asset': report['asset'],
            'mesh_signature': report['mesh_signature']['fingerprint'], 'deformation': report['deformation'],
            'shape_keys': report['shape_keys'], 'profile': profile,
            'notes': ['No overall quality score. Findings are measured indicators; the artistic decision stays with the user.']}


INTENT_RULES = [
    (('heavier', 'heavy', 'mass', 'bulk', 'bulkier', 'massive', 'upper body', 'upper-body', 'stronger', 'powerful'), [
        {'goal': 'more upper-back volume', 'operation': 'directional', 'regions': ['UPPER_BACK', 'BACK'], 'direction': [0, 1, 0],
         'fraction': 0.02, 'rationale': 'rear mass reads as weight in SIDE and THREE_QUARTER silhouettes'},
        # Scale in X, not inflate: inflating a region that contains an upper-arm segment ballooned the
        # limb into ball-shaped bulges ending at the region edge (M4 close-up renders of the fixture).
        {'goal': 'wider shoulder projection', 'operation': 'scale', 'regions': ['SHOULDERS', 'LEFT_SHOULDER', 'RIGHT_SHOULDER'],
         'factors': [1.04, 1.0, 1.0], 'rationale': 'moves the shoulder masses outward, widening the FRONT silhouette without inflating the arm'}]),
    (('transition', 'shoulder-to-back', 'blend', 'smoother', 'smooth'), [
        {'goal': 'softer shoulder/back transition', 'operation': 'smooth', 'regions': ['SHOULDERS', 'UPPER_BACK', 'LEFT_SHOULDER_TORSO'],
         'fraction': None, 'strength': 0.5, 'iterations': 4, 'rationale': 'reduces abrupt curvature change at the junction'}]),
    (('intimidating', 'menacing', 'imposing', 'threatening', 'aggressive'), [
        {'goal': 'broader shoulder line', 'operation': 'scale', 'regions': ['SHOULDERS'], 'factors': [1.05, 1.0, 1.0],
         'rationale': 'a wider top against a preserved waist strengthens the V silhouette'},
        {'goal': 'raised shoulder mass (head reads lower)', 'operation': 'translate', 'regions': ['SHOULDERS', 'UPPER_BACK'],
         'direction': [0, 0, 1], 'fraction': 0.006, 'rationale': 'lowers the visual head/neck relationship without editing the head'}]),
    (('slimmer', 'thinner', 'leaner', 'lighter'), [
        {'goal': 'less torso volume', 'operation': 'inflate', 'regions': ['TORSO', 'CHEST'], 'fraction': -0.008,
         'rationale': 'contracts the torso along its normals'}]),
    (('torso', 'improve torso'), [
        {'goal': 'cleaner torso transition', 'operation': 'smooth', 'regions': ['LEFT_SHOULDER_TORSO', 'TORSO'], 'fraction': None,
         'strength': 0.4, 'iterations': 3, 'rationale': 'smooths the torso surface without moving locked features'}]),
]
PRESERVE_TOKENS = ('preserv', 'keep', 'protect', 'lock', 'without changing', "don't touch", 'do not touch')


def parse_intent(intent, region_names):
    """Keyword rules. Change words are read before the first preserve word, protected regions after it."""
    text = intent.lower()
    cut = min([text.find(token) for token in PRESERVE_TOKENS if token in text] or [len(text)])
    change, keep = text[:cut], text[cut:]
    matched = []
    for tokens, steps in INTENT_RULES:
        if any(token in change for token in tokens):
            matched.extend(steps)
    preserve = []
    for name in region_names:
        words = name.lower().split('_')
        if re.search(r'\b' + re.escape(' '.join(words)) + r'\b', keep) or re.search(r'\b' + re.escape(words[-1]) + r's?\b', keep):
            preserve.append(name)
    return matched, sorted(set(preserve))


def redesign_plan(bridge, storage, object_name, intent, manifest=None):
    """Translate an intent into explicit PROPOSED topology-preserving operations. Never mutates."""
    if not isinstance(intent, str) or not intent.strip():
        raise ValueError('intent must describe the desired change')
    data = mesh_service.load_manifest(storage, manifest) if manifest else None
    names = [r['name'] for r in data['regions']] if data else []
    roles = {r['name']: r['role'] for r in data['regions']} if data else {}
    matched, preserve = parse_intent(intent, names)
    topology = bridge.run(REGIONS, {'object_name': object_name})['topology_fingerprint']
    silhouette = analyze_silhouette(bridge, storage, object_name, ['FRONT'], 120, manifest)
    reference = silhouette['named_measurements']['overall_height_m']
    steps = []
    seen = set()
    for rule in matched:
        # First editable region the rule names; a locked/unassigned one is reported instead of edited.
        present = [n for n in rule['regions'] if n in names]
        editable = [n for n in present if roles[n] == 'editable']
        region = (editable or present or [None])[0]
        if (rule['operation'], region) in seen:
            continue  # two rules asking for the same operation on the same region: one step
        seen.add((rule['operation'], region))
        step = {'step': len(steps) + 1, 'goal': rule['goal'], 'visual_rationale': rule['rationale'], 'apply': False,
                'numbers_are': 'a starting proposal to review, not artistic truth'}
        if region is None:
            step.update(status='requires_region', missing_any_of=rule['regions'])
            steps.append(step)
            continue
        row = next(r for r in data['regions'] if r['name'] == region)
        operation = {'type': rule['operation'], 'region': region, 'falloff': {'type': 'smooth', 'width_m': round(0.04 * reference, 4)},
                     'preserve_boundary': True}
        if rule['operation'] in ('translate', 'directional', 'inflate'):
            operation['strength'] = round(rule['fraction'] * reference, 4)
        if 'direction' in rule:
            operation['direction'] = rule['direction']
        if rule['operation'] == 'scale':
            operation['factors'] = rule['factors']
        if rule['operation'] == 'smooth':
            operation['strength'] = rule['strength']
            operation['iterations'] = rule['iterations']
        risks = []
        if row['role'] == 'locked':
            step.update(status='blocked_locked_region', operation=operation)
            steps.append(step)
            continue
        if row['role'] != 'editable':
            risks.append('REGION_NOT_EDITABLE: mark ' + region + ' editable (character_lock_regions editable=[...]) before this step')
        locked = [r['definition'] for r in data['regions'] if r['role'] == 'locked']
        analysis = bridge.run(PLAN_ANALYZE, {'object_name': object_name, 'operation': validate_operation(operation),
                                             'editable': row['definition'], 'locked': locked, 'shape_keys': 'require_unaffected'})
        proximity = analysis['stats']['locked_proximity_m']
        if proximity is not None and proximity < operation['falloff']['width_m']:
            risks.append('edited vertices within ' + format(proximity, '.3f') + ' m of a locked region')
        if not analysis['shape_keys']['safe']:
            risks.extend(analysis['shape_keys']['reasons'])
        if analysis['stats'].get('flip_guard_attenuated_vertices'):
            risks.append('FLIP_GUARD: ' + str(analysis['stats']['flip_guard_attenuated_vertices'])
                         + ' vertices attenuated to avoid folded faces (thin geometry near the region)')
        step.update(status='proposed' if row['role'] == 'editable' else 'needs_editable_region', operation=operation, regions=[region], estimated_displacement_m=analysis['stats']['max_displacement_m'],
                    affected_vertices=analysis['stats']['edited_vertices'], locked_region_proximity_m=proximity,
                    risk='high' if any('SHAPE_KEY' in r for r in risks) else ('medium' if risks else 'low'), risk_reasons=risks,
                    dependencies={'topology': 'preserved by construction', 'weights_uv_materials': 'preserved by construction; verified after edit',
                                  'shape_keys': analysis['shape_keys']['strategy']})
        steps.append(step)
    unlocked_preserve = [n for n in preserve if data and next(r for r in data['regions'] if r['name'] == n)['role'] != 'locked']
    return {'status': 'warning' if not matched else 'success', 'object': object_name, 'intent': intent, 'apply': False,
            'mutated': False, 'steps': steps, 'preserve_requested': preserve,
            'preserve_not_locked_yet': unlocked_preserve,
            'topology_fingerprint': topology,
            'warnings': (['Intent not mapped to any known operation; describe the change in terms of mass, width, transition or volume.'] if not matched else [])
                        + (['Regions to preserve are not locked yet: ' + ', '.join(unlocked_preserve)] if unlocked_preserve else [])}


# ------------------------------------------------------------------------------------------ candidate

def create_candidate(bridge, storage, operation_id, object_name, source_fingerprint):
    # M4 refuses a MASTER up front, even when an existing candidate would only be reused.
    require_mutable_asset(bridge, storage, 'character_create_candidate')
    report = mesh_service.create_candidate(bridge, storage, operation_id, object_name, source_fingerprint)
    report['dependency_manifest'] = {'before': report['source_signature'], 'after': report['candidate_signature'],
                                     'preservation': report['preservation'], 'not_verified': report['not_verified']}
    return report


# ------------------------------------------------------------------------------------------ renders

def _render(bridge, storage, operation_id, objects, views, width, height, cameras=None, margin=1.1):
    for value, low, high, label in ((width, 64, 2048, 'width'), (height, 64, 2048, 'height')):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(label + ' must be an integer 64..2048')
    folder = storage.reserve('qa', operation_id)
    result = bridge.run(RENDER, {'objects': objects, 'views': views, 'width': width, 'height': height, 'cameras': cameras,
                                 'output_dir': str(folder).replace('\\', '/'), 'operation_id': operation_id, 'margin': margin})
    result['output_dir'] = str(folder)
    for item in result.get('files', []):
        path = Path(item['path'])
        item['exists'] = path.exists()
        item['bytes'] = path.stat().st_size if path.exists() else 0
    if any(not item['exists'] for item in result.get('files', [])):
        result['status'] = 'error'
        result.setdefault('errors', []).append({'type': 'MissingRender', 'message': 'A rendered PNG is missing'})
    return result, folder


def _montage(result, folder, objects, views, name):
    if result.get('status') != 'ok' or len(objects) > 2 or len(views) > 3:
        return None
    rows = []
    for obj in objects:
        rows.append([Path(next(f['path'] for f in result['files'] if f['object'] == obj and f['view'] == v)) for v in views])
    path = folder / name
    width, height = write_montage(rows, path)
    return {'path': str(path), 'rows': objects, 'columns': views, 'width': width, 'height': height}


def render_multiview(bridge, storage, operation_id, objects, views=None, width=512, height=512):
    if not isinstance(objects, list) or not 1 <= len(objects) <= 4 or any(not isinstance(o, str) or not o for o in objects):
        raise ValueError('objects must list 1..4 mesh object names')
    views = _views(views)
    result, folder = _render(bridge, storage, operation_id, objects, views, width, height)
    result['montage'] = _montage(result, folder, objects[:2], views[:3], 'montage.png')
    return result


def _silhouette_delta(before, after):
    rows = []
    for a, b in zip(before['views'], after['views']):
        row = {'view': a['view']}
        for key in ('height_m', 'width_m', 'area_m2', 'centroid_u_m', 'centroid_v_m', 'left_area_fraction', 'extremity_reach_m'):
            row[key] = {'before': a[key], 'after': b[key], 'delta': b[key] - a[key]}
        row['profile_width_delta_m'] = [{'height_fraction': p['height_fraction'], 'delta': q['span_m'] - p['span_m']}
                                        for p, q in zip(a['profile'], b['profile'])]
        rows.append(row)
    return rows


def compare_before_after(bridge, storage, operation_id, before_object, after_object, views=None, width=512, height=512, manifest=None):
    views = _views(views)
    result, folder = _render(bridge, storage, operation_id, [before_object, after_object], views, width, height)
    result['montage'] = _montage(result, folder, [before_object, after_object], views[:3], 'before_after.png')
    regions, _ = _region_defs(bridge, storage, before_object, manifest)
    before = bridge.run(SILHOUETTE, {'object_name': before_object, 'views': views, 'rows': 200, 'regions': regions})
    after = bridge.run(SILHOUETTE, {'object_name': after_object, 'views': views, 'rows': 200, 'regions': regions})
    result['silhouette_delta'] = _silhouette_delta(before, after)
    result['named_measurements'] = {'before': named_measurements(before), 'after': named_measurements(after)}
    result['equivalence'] = {'shared_cameras': True, 'same_resolution': [width, height], 'same_look': result.get('look')}
    result['visual_verification'] = 'required'
    return result


def capture_reference_views(bridge, storage, operation_id, object_name, views=None, width=512, height=512, concept_art=None):
    views = _views(views)
    art = {}
    for view, path in (concept_art or {}).items():
        if view not in views or not isinstance(path, str) or not Path(path).is_file():
            raise ValueError('concept_art must map a captured view to an existing image file')
        art[view] = str(Path(path).resolve())
    result, folder = _render(bridge, storage, operation_id, [object_name], views, width, height)
    if result.get('status') != 'ok':
        return result
    silhouette = bridge.run(SILHOUETTE, {'object_name': object_name, 'views': views, 'rows': 200,
                                         'regions': bridge.run(REGIONS, {'object_name': object_name})['regions']})
    manifest = {'schema': 'production.character_reference_views.v1', 'object': object_name, 'views': views,
                'resolution': [width, height], 'cameras': result['cameras'], 'look': result.get('look'),
                'images': {f['view']: f['path'] for f in result['files']}, 'concept_art': art,
                'silhouette': silhouette}
    path = folder / 'reference_views.json'
    write_json(path, manifest)
    return {'status': 'success', 'reference_set': str(path), 'images': manifest['images'], 'concept_art': art,
            'cameras': result['cameras'], 'render_backend': result.get('render_backend'),
            'notes': ['Concept art is only associated with a view; no image-to-3D and no pixel equivalence claims.']}


def _load_reference(storage, reference_set):
    path = Path(reference_set).resolve(strict=True)
    if path.name != 'reference_views.json' or not path.is_relative_to(storage.root / 'qa'):
        raise ValueError('reference_set must be a reference_views.json inside this workspace')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 'production.character_reference_views.v1':
        raise ValueError('Unknown reference view schema')
    return data


def capture_candidate_views(bridge, storage, operation_id, reference_set, object_name):
    reference = _load_reference(storage, reference_set)
    width, height = reference['resolution']
    result, folder = _render(bridge, storage, operation_id, [object_name], reference['views'], width, height, reference['cameras'])
    result['reference_set'] = str(reference_set)
    result['equivalence'] = {'cameras_from_reference': True, 'resolution': [width, height]}
    return result


def compare_reference(bridge, storage, operation_id, reference_set, object_name):
    reference = _load_reference(storage, reference_set)
    captured = capture_candidate_views(bridge, storage, operation_id, reference_set, object_name)
    silhouette = bridge.run(SILHOUETTE, {'object_name': object_name, 'views': reference['views'], 'rows': 200,
                                         'regions': bridge.run(REGIONS, {'object_name': object_name})['regions']})
    pairs = []
    for f in captured.get('files', []):
        pairs.append({'view': f['view'], 'reference_image': reference['images'].get(f['view']), 'candidate_image': f['path'],
                      'concept_art': reference['concept_art'].get(f['view'])})
    return {'status': captured.get('status', 'ok'), 'object': object_name, 'reference_object': reference['object'],
            'measurable_differences': _silhouette_delta(reference['silhouette'], silhouette),
            'named_measurements': {'reference': named_measurements(reference['silhouette']), 'candidate': named_measurements(silhouette)},
            'visual_evidence': pairs, 'render_backend': captured.get('render_backend'),
            'notes': ['Measurable silhouette differences plus images for human review; no semantic-equivalence score.']}


# ------------------------------------------------------------------------------------------ skinning QA

def skinning_qa(bridge, storage, operation_id, rig, source_mesh, candidate_mesh, stress_poses):
    """Same explicit stress poses on source and candidate; the candidate must not add deformation issues."""
    from ..deformation import test_pose
    run = test_pose(bridge, storage, operation_id, rig, stress_poses, dry_run=False,
                    values={'meshes': [source_mesh, candidate_mesh], 'max_vertices': 250000, 'max_vertex_samples': 8000000,
                            'detail': 'full', 'limit': 200})
    whole = [row for row in run.get('rows', []) if row['region'] == 'mesh']
    comparisons, regressions = [], []
    for index, sample in enumerate(run.get('samples', [])):
        chunk = {row['mesh']: row['metrics'] for row in whole[2 * index:2 * index + 2]}
        a, b = chunk.get(source_mesh), chunk.get(candidate_mesh)
        if not a or not b:
            continue
        label = sample['pose']['region'] + ' ' + sample['pose']['bone'] + ' ' + format(sample['pose']['angle_degrees'], 'g') + ' deg'
        row = {'pose': label}
        for key in ('collapsed_edges', 'stretched_edges', 'collapsed_triangles'):
            row[key] = {'source': a[key], 'candidate': b[key]}
            if b[key] > a[key]:
                regressions.append(label + ': ' + key + ' ' + str(a[key]) + ' -> ' + str(b[key]))
        for key in ('min_edge_ratio', 'max_edge_ratio', 'min_triangle_area_ratio', 'volume_ratio'):
            row[key] = {'source': a[key], 'candidate': b[key]}
        comparisons.append(row)
    complete = run.get('status') != 'error' and len(comparisons) == len(stress_poses)
    passed = complete and not regressions
    errors = []
    if regressions:
        errors.append('DEFORMATION_REGRESSION: ' + '; '.join(regressions))
    if not complete:
        errors.append('DEFORMATION_QA_INCOMPLETE: ' + json.dumps(run.get('errors', []))[:300])
    return {'status': 'success' if passed else 'error', 'passed': passed, 'poses': comparisons, 'regressions': regressions,
            'checkpoint': run.get('checkpoint'), 'state_restored': run.get('state_restored'),
            'criterion': 'under the same explicit stress pose no collapsed/stretched/degenerate count may exceed the source',
            'errors': errors}


# ------------------------------------------------------------------------------------------ session

def redesign_session(bridge, storage, operation_id, object_name, intent, manifest=None, apply=False, step=1,
                     stress_poses=None, rig=None):
    """inspect -> audit -> regions -> plan -> risk [-> candidate -> propose -> preview -> verify -> deformation QA -> multiview].

    One proposal/application cycle per call at most. apply=False never mutates. apply=True creates the
    candidate and a PREVIEW of one plan step; the real APPLY stays an explicit character_adjust_shape call.
    """
    if not isinstance(apply, bool):
        raise ValueError('apply must be boolean')
    if apply:
        # Fail before any analysis, candidate reuse or checkpoint on a MASTER / unregistered file.
        require_mutable_asset(bridge, storage, 'character_redesign_session apply')
    inspection = inspect(bridge, storage, object_name)
    audit = modeling_audit(bridge, storage, object_name, manifest, include_intersections=False)
    output = {'object': object_name, 'intent': intent, 'apply': apply, 'mutated': False,
              'inspection': {k: inspection[k] for k in ('counts', 'dimensions_m', 'asset', 'shape_keys', 'topology_warnings')},
              'audit': {'findings': audit['findings'], 'findings_count': audit['findings_count']}}
    if not manifest:
        output.update(status='warning', next_step='Define and lock regions (character_define_regions, character_lock_regions) before planning edits.')
        return output
    plan = redesign_plan(bridge, storage, object_name, intent, manifest)
    output['plan'] = plan
    proposed = [s for s in plan['steps'] if s.get('status') == 'proposed']
    output['risk'] = {'steps': [{'step': s['step'], 'risk': s['risk'], 'reasons': s['risk_reasons']} for s in proposed]}
    if not apply:
        output.update(status='success', next_step='Review the plan; call again with apply=true and step=N for one preview cycle.')
        return output
    chosen = next((s for s in proposed if s['step'] == step), None)
    if chosen is None:
        output.update(status='error', errors=['PLAN_STEP_UNAVAILABLE: step ' + str(step) + ' is not a proposed step'])
        return output
    if chosen['risk'] == 'high':
        output.update(status='error', errors=['PLAN_STEP_HIGH_RISK: ' + '; '.join(chosen['risk_reasons'])])
        return output
    candidate = create_candidate(bridge, storage, operation_id + '-candidate', object_name, inspection['mesh_signature']['fingerprint'])
    output['candidate'] = {k: candidate.get(k) for k in ('candidate', 'status', 'preservation', 'reused', 'checkpoint')}
    if candidate['status'] == 'error':
        output.update(status='error', errors=candidate['errors'])
        return output
    output['mutated'] = True
    options = {'regions_manifest': manifest, 'operation': chosen['operation']}
    proposal = adjust_shape(bridge, storage, operation_id + '-propose', candidate['candidate'], 'propose', options)
    output['proposal'] = {k: proposal.get(k) for k in ('operation', 'correction_id', 'proposal', 'warnings')}
    if not proposal['proposal']['supported']:
        output.update(status='error', errors=proposal['proposal']['reasons'])
        return output
    preview = adjust_shape(bridge, storage, operation_id + '-preview', mode='preview', operation=proposal['operation'])
    output['preview'] = {k: preview.get(k) for k in ('status', 'numeric_verification', 'metrics', 'errors', 'preview_object',
                                                     'regions_verification', 'idempotency')}
    if preview.get('status') == 'error' or not preview.get('preview_object'):
        output.update(status='error', errors=preview.get('errors', []))
        return output
    if stress_poses and rig:
        output['deformation_qa'] = skinning_qa(bridge, storage, operation_id + '-skin', rig, object_name, preview['preview_object'], stress_poses)
    else:
        output['deformation_qa'] = {'status': 'not_run', 'reason': 'explicit rig and stress_poses are required for deformation QA'}
    output['multiview'] = compare_before_after(bridge, storage, operation_id + '-qa', object_name, preview['preview_object'], manifest=manifest)
    output.update(status='warning', next_step='Review preview, deformation QA and before/after renders; apply only with an explicit '
                  'character_adjust_shape(mode="apply", operation=...) call.', visual_verification='required')
    return output
