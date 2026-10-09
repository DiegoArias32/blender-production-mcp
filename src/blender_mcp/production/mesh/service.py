"""Mesh safety foundation for a future M4 (M3.1 phases 18-20). No sculpting, remodeling or redesign.

- ``mesh_signature``: compact per-section fingerprints of a mesh object; the full signature is stored
  in the workspace, never returned to the model.
- candidate mesh: SOURCE -> CANDIDATE (object + mesh copy) -> [future mutation] -> VERIFY -> decision.
  The source is never edited; preservation is reported per declared dependency, and anything this
  code cannot verify is reported as ``not_verified`` instead of "preserved".
- regions: named LOCKED / EDITABLE vertex sets defined by vertex group, face set, sculpt mask or
  explicit indices bound to a topology fingerprint. Locked-region verification fails when positions,
  shape keys or deformation weights change beyond an explicit tolerance, or when membership/topology
  can no longer be matched.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ..common.asset_safety import require_mutable_asset
from ..common.ledger import deterministic_suffix
from ..common.storage import write_json
from .runtime import CANDIDATE_CREATE, CANDIDATE_FIND, CANDIDATE_PAIR, REGION_CAPTURE, SIGNATURE

# Declared candidate dependencies (phase 19) and the signature sections that prove each one.
PRESERVATION = {
    'object_relationship': ('relationships',),
    'armature_modifier': ('modifiers',),
    'vertex_groups': ('vertex_groups',),
    'shape_keys': ('shape_keys',),
    'material_slots': ('materials',),
    'uv_layers': ('uv_layers',),
    'normals': ('normals',),
    'custom_properties': ('custom_properties',),
    'topology_and_geometry': ('topology', 'geometry', 'attributes'),
    'transform': ('transform',),
}
NOT_VERIFIED = ['drivers or constraints on other objects that target the source object',
                'geometry-node, hook and other non-armature modifier inputs beyond name/type/visibility',
                'object-level Action content (only the assignment is compared)']
# 'unassigned' (M4): recorded and captured, never edited and never failing verification.
ROLES = ('locked', 'editable', 'unassigned')


def summary(signature: dict) -> dict:
    sections = signature['sections']
    topology = sections['topology']
    return {'schema': signature['schema'], 'object': signature['object'], 'mesh': signature['mesh'],
            'linked': signature['linked'], 'fingerprint': signature['fingerprint'], 'fingerprints': signature['fingerprints'],
            'counts': {k: topology[k] for k in ('vertices', 'edges', 'polygons', 'loops', 'triangles')},
            'shape_keys': sections['shape_keys']['names'][:100],
            'vertex_groups': sections['vertex_groups']['names'][:100],
            'uv_layers': [{k: l[k] for k in ('name', 'active', 'active_render', 'udim_tiles', 'udim_tile_count',
                                             'faces_outside_udim_range')} for l in sections['uv_layers']['layers']],
            'normals': {k: sections['normals'][k] for k in ('domain', 'has_custom_normals')},
            'materials': sections['materials']['names'][:100],
            'armature_modifiers': sections['modifiers']['armature_modifiers'],
            'parent': {k: sections['relationships'][k] for k in ('parent', 'parent_type', 'parent_bone')},
            'custom_properties': sections['custom_properties']['names']}


def _store(storage, category, operation_id, name, data) -> str:
    folder = storage.reserve(category, operation_id)
    path = folder / name
    write_json(path, data)
    return str(path)


def mesh_signature(bridge, storage, operation_id, object_name) -> dict:
    signature = bridge.run(SIGNATURE, {'object_name': object_name})
    return {'status': 'success', 'signature': summary(signature),
            'stored_signature': _store(storage, 'signatures', operation_id, 'signature.json', signature)}


def compare(source: dict, candidate: dict) -> dict:
    """Per declared dependency: preserved True/False with the differing sections."""
    report = {}
    for dependency, sections in PRESERVATION.items():
        differing = [s for s in sections if source['sections'][s]['fingerprint'] != candidate['sections'][s]['fingerprint']]
        report[dependency] = {'preserved': not differing, 'differing_sections': differing}
    return report


# ------------------------------------------------------------------------------------ regions

def validate_regions(regions) -> list[dict]:
    if not isinstance(regions, list) or not 1 <= len(regions) <= 32:
        raise ValueError('regions must be a list of 1..32 region definitions')
    names = set()
    output = []
    for region in regions:
        if not isinstance(region, dict) or set(region) - {'name', 'role', 'definition', 'tolerance_m'}:
            raise ValueError('Each region needs name, role, definition and optional tolerance_m')
        name, role, definition = region.get('name'), region.get('role'), region.get('definition')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Region names must be unique nonempty strings')
        if role not in ROLES:
            raise ValueError('Region role must be locked, editable or unassigned')
        tolerance = region.get('tolerance_m', 1e-5)
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or not 0 <= tolerance <= 0.01:
            raise ValueError('tolerance_m must be finite, 0..0.01 m')
        if not isinstance(definition, dict):
            raise ValueError('Region definition must be an object')
        method = definition.get('method')
        allowed = {'vertex_group': {'method', 'name', 'min_weight'}, 'face_set': {'method', 'ids'},
                   'mask': {'method', 'min_mask'}, 'indices': {'method', 'indices', 'topology_fingerprint'},
                   'bounds': {'method', 'min', 'max'}, 'dominant_group': {'method', 'groups', 'among'}}
        if method not in allowed or set(definition) - allowed[method]:
            raise ValueError('Region method must be vertex_group, face_set, mask, indices, bounds or dominant_group with its own fields')
        if method == 'bounds' and not all(isinstance(definition.get(k), list) and len(definition[k]) == 3
                                          and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in definition[k])
                                          for k in ('min', 'max')):
            raise ValueError('bounds regions need finite object-space min and max [x, y, z]')
        if method == 'dominant_group' and not all(isinstance(definition.get(k), list) and definition[k] and len(definition[k]) <= 256
                                                  and all(isinstance(v, str) and v for v in definition[k]) for k in ('groups', 'among')):
            raise ValueError('dominant_group regions need nonempty groups and among lists of vertex-group names')
        if method == 'vertex_group' and (not isinstance(definition.get('name'), str) or not definition['name']):
            raise ValueError('vertex_group regions need the group name (object names are never used)')
        for key in ('min_weight', 'min_mask'):
            value = definition.get(key, 0.5)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
                raise ValueError(key + ' must be in (0, 1]')
        if method == 'face_set' and (not isinstance(definition.get('ids'), list) or not definition['ids']
                                     or any(isinstance(i, bool) or not isinstance(i, int) for i in definition['ids'])):
            raise ValueError('face_set regions need a nonempty list of integer ids')
        if method == 'indices':
            indices = definition.get('indices')
            if not isinstance(indices, list) or not 1 <= len(indices) <= 2_000_000 or any(isinstance(i, bool) or not isinstance(i, int) for i in indices):
                raise ValueError('indices regions need 1..2000000 integer vertex indices')
            if not isinstance(definition.get('topology_fingerprint'), str):
                raise ValueError('indices regions must be bound to a topology_fingerprint (from mesh_signature)')
        names.add(name)
        output.append({'name': name, 'role': role, 'definition': definition, 'tolerance_m': float(tolerance)})
    return output


def define_regions(bridge, storage, operation_id, object_name, regions) -> dict:
    regions = validate_regions(regions)
    captured = bridge.run(REGION_CAPTURE, {'object_name': object_name, 'regions': regions})['regions']
    signature = bridge.run(SIGNATURE, {'object_name': object_name})
    empty = [r['name'] for r in captured if not r['indices']]
    if empty:
        raise ValueError('REGION_EMPTY: no vertex matches ' + ', '.join(empty))
    manifest = {'schema': 'production.mesh_regions.v1', 'object': object_name, 'signature': signature,
                'regions': [dict(region, capture=capture) for region, capture in zip(regions, captured)]}
    path = _store(storage, 'regions', operation_id, 'manifest.json', manifest)
    return {'status': 'success', 'manifest': path, 'object': object_name,
            'topology_fingerprint': signature['sections']['topology']['fingerprint'],
            'regions': [{'name': r['name'], 'role': r['role'], 'method': r['definition']['method'],
                         'vertices': len(c['indices']), 'indices_digest': c['indices_digest'], 'tolerance_m': r['tolerance_m']}
                        for r, c in zip(regions, captured)]}


def load_manifest(storage, path) -> dict:
    candidate = Path(path).resolve(strict=True)
    if candidate.name != 'manifest.json' or not candidate.is_relative_to(storage.root / 'regions'):
        raise ValueError('Region manifests must be manifest.json files inside this workspace')
    data = json.loads(candidate.read_text(encoding='utf-8'))
    if data.get('schema') != 'production.mesh_regions.v1':
        raise ValueError('Unknown region manifest schema')
    return data


def _world_distance(matrix, a, b, unit_scale):
    delta = [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
    world = [sum(matrix[row][col] * delta[col] for col in range(3)) for row in range(3)]
    return math.sqrt(sum(v * v for v in world)) * unit_scale


def _max_displacement(before, after, matrix, unit_scale):
    worst, worst_vertex = 0.0, None
    for position in range(0, len(before), 3):
        distance = _world_distance(matrix, after[position:position + 3], before[position:position + 3], unit_scale)
        if distance > worst:
            worst, worst_vertex = distance, position // 3
    return worst, worst_vertex


def compare_region(region: dict, current: dict) -> dict:
    stored = region['capture']
    tolerance = region['tolerance_m']
    failures = []
    row = {'name': region['name'], 'role': region['role'], 'tolerance_m': tolerance, 'vertices': len(stored['indices'])}
    if current['topology_fingerprint'] != stored['topology_fingerprint']:
        failures.append('TOPOLOGY_CHANGED: vertex indices can no longer be matched to the locked region')
    elif current['indices_digest'] != stored['indices_digest']:
        failures.append('REGION_MEMBERSHIP_CHANGED: the region definition now selects other vertices')
    else:
        matrix, unit = current['matrix_world'], current['unit_scale']
        distance, vertex = _max_displacement(stored['positions'], current['positions'], matrix, unit)
        row['max_displacement_m'] = distance
        row['worst_vertex'] = stored['indices'][vertex] if vertex is not None else None
        if distance > tolerance:
            failures.append('LOCKED_REGION_MODIFIED: ' + format(distance, '.6g') + ' m > ' + format(tolerance, '.6g') + ' m')
        keys = {}
        for name, values in stored['shape_keys'].items():
            if name not in current['shape_keys']:
                failures.append('SHAPE_KEY_MISSING: ' + name)
                continue
            keys[name] = _max_displacement(values, current['shape_keys'][name], matrix, unit)[0]
            if keys[name] > tolerance:
                failures.append('LOCKED_SHAPE_KEY_MODIFIED: ' + name)
        basis = current['positions']
        for name in sorted(set(current['shape_keys']) - set(stored['shape_keys'])):
            # A new shape key must not move locked vertices away from the basis.
            keys[name] = _max_displacement(basis, current['shape_keys'][name], matrix, unit)[0]
            if keys[name] > tolerance:
                failures.append('NEW_SHAPE_KEY_MOVES_LOCKED_REGION: ' + name)
        row['shape_key_max_displacement_m'] = keys
        if current['weights_digest'] != stored['weights_digest']:
            failures.append('LOCKED_WEIGHTS_CHANGED: deformation weights of the region differ')
        # M4 captures: absent in M3.1 manifests, compared only when both sides have them.
        if 'faces_digest' in stored and 'faces_digest' in current:
            if current['faces_digest'] != stored['faces_digest']:
                failures.append('REGION_FACE_MEMBERSHIP_CHANGED: faces inside the locked region differ')
            elif current['materials_digest'] != stored['materials_digest']:
                failures.append('LOCKED_MATERIAL_CHANGED: material assignment of the locked faces differs')
            for layer, digest in stored.get('uv_digests', {}).items():
                if current.get('uv_digests', {}).get(layer) != digest:
                    failures.append('LOCKED_UV_CHANGED: ' + layer)
    row['changed'] = bool(failures)
    row['failures'] = failures
    row['passed'] = region['role'] != 'locked' or not failures
    return row


def verify_regions(bridge, storage, manifest_path, object_name=None) -> dict:
    manifest = load_manifest(storage, manifest_path)
    target = object_name or manifest['object']
    regions = manifest['regions']
    request = []
    for region in regions:
        definition = dict(region['definition'])
        if definition['method'] == 'indices':
            definition['topology_fingerprint'] = None  # compared on the host, after capture
        request.append({'name': region['name'], 'definition': definition})
    try:
        captured = bridge.run(REGION_CAPTURE, {'object_name': target, 'regions': request})['regions']
    except Exception as exc:
        if 'REGION_UNDEFINED' not in str(exc):
            raise
        return {'status': 'error', 'passed': False, 'object': target, 'manifest': str(manifest_path),
                'errors': ['REGION_UNDEFINED: a region definition no longer resolves on ' + target + ' (' + str(exc)[:300] + ')'],
                'regions': []}
    rows = [compare_region(region, current) for region, current in zip(regions, captured)]
    passed = all(row['passed'] for row in rows)
    return {'status': 'success' if passed else 'error', 'passed': passed, 'object': target, 'manifest': str(manifest_path),
            'regions': rows, 'errors': [f for row in rows if row['role'] == 'locked' for f in row['failures']],
            'warnings': ['Editable/unassigned region changes are reported, not failed.'] if any(r['changed'] for r in rows if r['role'] != 'locked') else []}


# ------------------------------------------------------------------------------------ candidate

def create_candidate(bridge, storage, operation_id, object_name, source_fingerprint) -> dict:
    if not isinstance(source_fingerprint, str) or not source_fingerprint.startswith('m521:'):
        raise ValueError('source_fingerprint from production.mesh_signature is required (freshness check)')
    existing = bridge.run(CANDIDATE_FIND, {'object_name': object_name, 'source_fingerprint': source_fingerprint})['candidates']
    if existing:
        # Content idempotency: this exact source state already has a candidate; never make a second one.
        pair = bridge.run(CANDIDATE_PAIR, {'object_name': object_name, 'candidate_name': existing[0]})
        return dict(_candidate_report(object_name, existing[0], pair['source_signature'], pair['candidate_signature'], None),
                    status='warning', reused=True, candidates_for_this_source=existing,
                    warnings=['CANDIDATE_REUSED: a candidate for this exact source state already exists; no new object was created'])
    asset = require_mutable_asset(bridge, storage, 'mesh candidate')
    from ..checkpoint import create_checkpoint
    checkpoint = create_checkpoint(bridge, storage, 'Before candidate mesh', operation_id, 'mesh_candidate')['manifest']
    data = bridge.run(CANDIDATE_CREATE, {'object_name': object_name, 'source_fingerprint': source_fingerprint,
                                         'suffix': deterministic_suffix(object_name + ':' + source_fingerprint, 1), 'operation_id': operation_id})
    report = _candidate_report(object_name, data['candidate'], data['source_signature'], data['candidate_signature'], checkpoint)
    report.update(asset=asset, visibility=data['visibility'], source_intact=data['source_intact'], reused=False)
    if not data['source_intact']:
        report['status'] = 'error'
        report['errors'] = ['SOURCE_CHANGED: the source mesh changed while creating the candidate; recover checkpoint']
    return report


def _candidate_report(object_name, candidate, source, candidate_signature, checkpoint) -> dict:
    preservation = compare(source, candidate_signature)
    preserved = all(item['preserved'] for item in preservation.values())
    return {'status': 'success' if preserved else 'error', 'source': object_name, 'candidate': candidate,
            'checkpoint': checkpoint, 'preservation': preservation, 'not_verified': NOT_VERIFIED,
            'source_signature': summary(source), 'candidate_signature': summary(candidate_signature),
            'errors': [] if preserved else ['PRESERVATION_FAILED: ' + ', '.join(k for k, v in preservation.items() if not v['preserved'])]}


def verify_candidate(bridge, storage, object_name, candidate_name, regions_manifest=None) -> dict:
    pair = bridge.run(CANDIDATE_PAIR, {'object_name': object_name, 'candidate_name': candidate_name})
    source = pair['source_signature']
    candidate = pair['candidate_signature']
    report = {'status': 'success', 'source': object_name, 'candidate': candidate_name,
              'source_unchanged_since_candidate': source['fingerprint'] == pair['marker']['source_fingerprint'],
              'differences': {name: source['fingerprints'][name] != candidate['fingerprints'][name] for name in source['fingerprints']},
              'candidate_signature': summary(candidate), 'errors': [], 'warnings': []}
    if not report['source_unchanged_since_candidate']:
        report['status'] = 'error'
        report['errors'].append('SOURCE_CHANGED: the source mesh no longer matches the state the candidate was made from')
    if regions_manifest:
        regions = verify_regions(bridge, storage, regions_manifest, candidate_name)
        report['regions'] = regions
        if not regions['passed']:
            report['status'] = 'error'
            report['errors'].extend(regions['errors'])
    else:
        report['warnings'].append('No region manifest given: locked regions were not verified')
    report['decision'] = 'not_recorded: accepting or rejecting a candidate mesh is an M4 capability'
    return report
