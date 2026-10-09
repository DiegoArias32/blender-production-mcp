"""character_adjust_shape: the M4 mutation cycle (analyze/propose/preview/apply/verify/revert).

Reuses the M3.1 safety chain instead of a parallel one: immutable proposals (common/operations),
backend fingerprints, asset-role guard before any checkpoint, checkpoints, the operation ledger with
reconciliation (common/ledger.guarded_cycle) and payload capacity checks. Regions come from a
character region manifest (mesh/service manifest schema). Only candidate meshes can be edited.
"""
from __future__ import annotations

import math

from ..checkpoint import create_checkpoint
from ..common.asset_safety import require_mutable_asset
from ..common.bridge import payload_bytes
from ..common.ledger import guarded_cycle, is_uncertain
from ..common.operations import fingerprint, latest_candidate, load_proposal, record_event, save_proposal
from ..mesh.service import load_manifest, verify_regions
from .edit import CHECK, EDIT_RUNTIME, EXECUTE, PREFLIGHT, READ, RECONCILE

KIND = 'character_adjust_shape'
MODES = ('analyze', 'propose', 'preview', 'apply', 'verify', 'revert')
OPERATIONS = ('translate', 'directional', 'grab', 'inflate', 'scale', 'smooth', 'relax', 'crease')
SHAPE_KEY_POLICIES = ('require_unaffected', 'propagate_delta')


def _finite(value, name, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(name + ' must be within ' + str(low) + '..' + str(high))
    return float(value)


def _vector(value, name, nonzero=True):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(name + ' must be [x, y, z]')
    vector = [_finite(v, name) for v in value]
    if nonzero and sum(v * v for v in vector) < 1e-18:
        raise ValueError(name + ' must be nonzero')
    return vector


def validate_operation(operation):
    """Explicit safe schema: region, strength, direction, space, falloff, preserve_boundary."""
    if not isinstance(operation, dict):
        raise ValueError('operation must be an object')
    allowed = {'type', 'region', 'strength', 'direction', 'space', 'falloff', 'preserve_boundary', 'pivot', 'factors',
               'center', 'radius_m', 'iterations'}
    unknown = set(operation) - allowed
    if unknown:
        raise ValueError('Unknown operation fields: ' + ', '.join(sorted(unknown)))
    kind = operation.get('type')
    if kind not in OPERATIONS:
        raise ValueError('operation.type must be one of ' + ', '.join(OPERATIONS))
    if not isinstance(operation.get('region'), str) or not operation['region']:
        raise ValueError('operation.region must name an editable region of the manifest')
    output = {'type': kind, 'region': operation['region'], 'space': operation.get('space', 'object'),
              'preserve_boundary': operation.get('preserve_boundary', True)}
    if output['space'] not in ('object', 'world'):
        raise ValueError('operation.space must be object or world')
    if not isinstance(output['preserve_boundary'], bool):
        raise ValueError('preserve_boundary must be boolean')
    falloff = operation.get('falloff', {'type': 'smooth', 'width_m': 0.05})
    if not isinstance(falloff, dict) or set(falloff) - {'type', 'width_m'} or falloff.get('type', 'smooth') not in ('smooth', 'linear', 'constant'):
        raise ValueError('falloff must be {type: smooth|linear|constant, width_m}')
    output['falloff'] = {'type': falloff.get('type', 'smooth'), 'width_m': _finite(falloff.get('width_m', 0.05), 'falloff.width_m', 0.0, 1.0)}
    if kind in ('translate', 'directional', 'grab', 'inflate'):
        output['strength'] = _finite(operation.get('strength'), 'strength (meters)', -0.5, 0.5)
    elif kind == 'scale':
        if operation.get('factors') is not None:
            output['factors'] = [_finite(v, 'factors', 0.5, 2.0) for v in _vector(operation['factors'], 'factors')]
            output['strength'] = 1.0
        else:
            output['strength'] = _finite(operation.get('strength'), 'strength (scale factor)', 0.5, 2.0)
        pivot = operation.get('pivot', 'region_centroid')
        output['pivot'] = pivot if pivot == 'region_centroid' else _vector(pivot, 'pivot', nonzero=False)
    else:
        output['strength'] = _finite(operation.get('strength', 0.5), 'strength', 0.0, 1.0)
        iterations = operation.get('iterations', 3)
        if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= 50:
            raise ValueError('iterations must be an integer 1..50')
        output['iterations'] = iterations
    if kind in ('translate', 'directional', 'grab'):
        output['direction'] = _vector(operation.get('direction'), 'direction')
    if kind == 'grab':
        output['center'] = _vector(operation.get('center'), 'center', nonzero=False)
        output['radius_m'] = _finite(operation.get('radius_m'), 'radius_m', 1e-6, 10.0)
    return output


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError('options must be an object')
    unknown = set(options) - {'regions_manifest', 'operation', 'shape_keys', 'tolerance_m'}
    if unknown:
        raise ValueError('Unknown adjust_shape options: ' + ', '.join(sorted(unknown)))
    if not isinstance(options.get('regions_manifest'), str):
        raise ValueError('regions_manifest (from character_define_regions / character_lock_regions) is required')
    policy = options.get('shape_keys', 'require_unaffected')
    if policy not in SHAPE_KEY_POLICIES:
        raise ValueError('shape_keys must be require_unaffected or propagate_delta')
    return {'regions_manifest': options['regions_manifest'], 'operation': validate_operation(options.get('operation')),
            'shape_keys': policy, 'tolerance_m': _finite(options.get('tolerance_m', 1e-5), 'tolerance_m', 0.0, 0.01)}


def resolve_regions(manifest, operation):
    """Editable definition for the operation and every locked definition, with ambiguity refusals."""
    regions = {row['name']: row for row in manifest['regions']}
    target = regions.get(operation['region'])
    if target is None:
        raise ValueError('REGION_UNDEFINED: ' + operation['region'] + ' is not in the manifest')
    if target['role'] == 'locked':
        raise ValueError('LOCKED_REGION_EDIT: ' + target['name'] + ' is locked; unlock it explicitly before editing')
    if target['role'] != 'editable':
        raise ValueError('REGION_NOT_EDITABLE: ' + target['name'] + ' has role ' + target['role'] + '; mark it editable explicitly')
    if target.get('source', 'user') == 'automatic' and (target.get('ambiguous') or target.get('confidence', 0) < 0.7):
        raise ValueError('REGION_AMBIGUOUS: ' + target['name'] + ' is an automatic low-confidence region; confirm it as a user region first')
    locked = [row['definition'] for row in manifest['regions'] if row['role'] == 'locked']
    return target['definition'], locked


def _public(data):
    hidden = {'field', 'weights'}
    return {k: v for k, v in data.items() if k not in hidden}


def adjust_shape(bridge, storage, operation_id, object_name=None, mode='analyze', options=None, operation=None):
    if mode not in MODES:
        raise ValueError('mode must be one of ' + ', '.join(MODES))
    body = EDIT_RUNTIME
    if mode in ('analyze', 'propose'):
        if not object_name or operation:
            raise ValueError('Analyze/propose requires object_name (a candidate mesh) and no operation')
        settings = validate_options(options)
        manifest = load_manifest(storage, settings['regions_manifest'])
        editable, locked = resolve_regions(manifest, settings['operation'])
        params = {'object_name': object_name, 'operation': settings['operation'], 'editable': editable, 'locked': locked,
                  'shape_keys': settings['shape_keys']}
        data = bridge.run(READ, params)
        manifest_topology = manifest['signature']['sections']['topology']['fingerprint']
        reasons = []
        if data['topology_fingerprint'] != manifest_topology:
            reasons.append('TOPOLOGY_CHANGED: the region manifest was defined on another topology; stale region indices refused')
        if not data['shape_keys']['safe']:
            reasons.extend(data['shape_keys']['reasons'])
        if data['stats']['edited_vertices'] == 0:
            reasons.append('NOTHING_TO_EDIT: the operation moves no vertex (region empty, fully locked or zero strength)')
        proposal = {'supported': not reasons, 'reasons': reasons, 'operation': settings['operation'],
                    'regions_manifest': settings['regions_manifest'], 'editable_region': settings['operation']['region'],
                    'locked_regions': [row['name'] for row in manifest['regions'] if row['role'] == 'locked'],
                    'stats': data['stats'], 'shape_keys': data['shape_keys'],
                    'warnings': ['Displacement values are the requested starting proposal, not artistic truth; review visually.']
                    + (['FLIP_GUARD: ' + str(data['stats']['flip_guard_attenuated_vertices']) + ' vertices were attenuated so no face folds; '
                        'the local change is smaller than requested'] if data['stats'].get('flip_guard_attenuated_vertices') else [])}
        record = dict(data, kind=KIND, object_name=object_name, options=settings, proposal=proposal, editable=editable,
                      locked=locked, tolerance_m=settings['tolerance_m'], transaction_schema=1,
                      backend_fingerprint=fingerprint(body))
        from blender_mcp.safe_mode import MAX_CODE_BYTES
        size = payload_bytes(EXECUTE, _execute_params(record, 'apply', '0' * 8, operation_id))
        proposal['capacity'] = {'payload_bytes': size, 'payload_limit_bytes': MAX_CODE_BYTES, 'supported': size <= MAX_CODE_BYTES}
        if size > MAX_CODE_BYTES:
            proposal['supported'] = False
            reasons.append('CAPACITY_EXCEEDED: apply payload ' + str(size) + ' bytes > ' + str(MAX_CODE_BYTES))
        if mode == 'propose':
            record['operation'] = save_proposal(storage, operation_id, record)
            record['correction_id'] = operation_id
        return dict(_public(record), mode=mode, status='warning', numeric_verification='not_applied',
                    visual_verification='not_applied', warnings=proposal['warnings'] + reasons)
    if not operation or options:
        raise ValueError('Preview/apply/verify/revert need the immutable operation reference and no new options')
    path, saved = load_proposal(storage, operation)
    if saved.get('kind') != KIND or (object_name and object_name != saved['object_name']):
        raise ValueError('Operation type/target mismatch')
    if not saved['proposal'].get('supported'):
        raise ValueError('unsupported_or_ambiguous: ' + '; '.join(saved['proposal'].get('reasons', [])))
    if saved.get('transaction_schema') != 1 or saved.get('backend_fingerprint') != fingerprint(body):
        raise ValueError('Operation backend changed; analyze/propose again')
    common = {'operation': str(path), 'correction_id': saved['operation_id'], 'mode': mode,
              'target': {'object': saved['object_name'], 'mesh': saved['mesh']}}
    if mode == 'verify':
        previous = latest_candidate(path)
        data = bridge.run(CHECK, _check_params(saved, previous, 'verify'))
        data.update(common, checkpoint=previous.get('checkpoint'))
        data['regions_verification'] = _regions(bridge, storage, saved)
        return _record(path, mode, data)
    previous = latest_candidate(path) if mode == 'revert' else {}

    def execute(suffix, note):
        if mode != 'revert' and not bridge.run(PREFLIGHT, {'object_name': saved['object_name'],
                                                           'signature_fingerprint': saved['signature_fingerprint']})['matches']:
            raise ValueError('STALE_PROPOSAL: the candidate changed after the proposal; analyze again')
        asset = require_mutable_asset(bridge, storage, KIND + ' ' + mode)
        checkpoint = create_checkpoint(bridge, storage, 'Before M4 shape ' + mode, operation_id, KIND)['manifest']
        note(checkpoint=checkpoint)
        record_event(path, 'intent', dict(common, checkpoint=checkpoint))
        try:
            if mode == 'revert':
                data = bridge.run(CHECK, _check_params(saved, previous, 'revert'))
            else:
                data = bridge.run(EXECUTE, _execute_params(saved, mode, suffix, operation_id))
            data.update(common, checkpoint=checkpoint, asset=asset)
        except Exception as exc:
            if mode == 'revert' and not is_uncertain(exc):
                raise
            data = dict(common, status='error', checkpoint=checkpoint, asset=asset,
                        errors=[{'type': type(exc).__name__, 'message': str(exc)}], numeric_verification='unverified',
                        warnings=['No automatic retry: retrying this proposal reconciles instead of executing twice.'])
        if mode != 'revert' and data.get('status') != 'error':
            regions = _regions(bridge, storage, saved, data.get('preview_object') or saved['object_name'])
            data['regions_verification'] = regions
            if not regions['passed']:
                data['status'] = 'error'
                data.setdefault('errors', []).extend(regions['errors'])
        return _record(path, mode, data)

    def measure(candidate, working_fingerprint, checkpoint):
        data = bridge.run(CHECK, _check_params(saved, {'working_action': candidate, 'working_fingerprint': working_fingerprint,
                                                      'previous_mesh': saved['mesh'], 'mode': mode}, 'verify'))
        record_event(path, mode, dict(common, working_action=candidate, working_fingerprint=working_fingerprint,
                                      previous_mesh=saved['mesh'], checkpoint=checkpoint, reconciled=True,
                                      numeric_verification=data.get('numeric_verification'), metrics=data.get('metrics', {})))
        data.update(common, checkpoint=checkpoint, working_action=candidate)
        return data

    base = saved['mesh'].split('__M4E_')[0]
    return guarded_cycle(bridge, storage, tool=KIND, correction_id=saved['operation_id'], mode=mode,
                         request={'operation': str(path), 'kind': KIND, 'mode': mode}, body=body,
                         object_name=saved['object_name'], source_action=saved['mesh'], candidate_prefix=base + '__M4E_',
                         suffix_length=8, previous_candidate=previous.get('working_action'), execute=execute,
                         measure=measure, reconcile=RECONCILE)


def _execute_params(saved, mode, suffix, operation_id):
    return {'object_name': saved['object_name'], 'operation': saved['options']['operation'], 'editable': saved['editable'],
            'locked': saved['locked'], 'shape_keys': saved['options']['shape_keys'], 'tolerance_m': saved['tolerance_m'],
            'signature_fingerprint': saved['signature_fingerprint'], 'field_digest': saved['field_digest'],
            'mode': mode, 'suffix': suffix, 'operation_id': operation_id}


def _check_params(saved, previous, mode):
    return {'object_name': saved['object_name'], 'previous_mesh': previous.get('previous_mesh', saved['mesh']),
            'working_action': previous['working_action'], 'working_fingerprint': previous['working_fingerprint'],
            'editable': saved['editable'], 'locked': saved['locked'], 'tolerance_m': saved['tolerance_m'],
            'numeric_verification': previous.get('numeric_verification'), 'event_mode': previous.get('mode'), 'mode': mode}


def _regions(bridge, storage, saved, target=None):
    return verify_regions(bridge, storage, saved['options']['regions_manifest'], target or saved['object_name'])


def _record(path, mode, data):
    try:
        data['event'] = record_event(path, mode, data)
    except OSError as exc:
        data.setdefault('warnings', []).append('Result log could not be saved: ' + str(exc))
    return data
