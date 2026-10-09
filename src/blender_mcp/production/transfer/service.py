"""Compact host contracts for M5 weight/morph/attribute transfer and deformation regions."""
from __future__ import annotations

import math

from ..common.m5 import m5_read, transaction
from ..common.asset_safety import require_mutable_asset
from ..checkpoint import create_checkpoint
from ..deformation import poses as validate_poses
from .runtime import TRANSFER_RUNTIME, WEIGHT_RUNTIME, ATTRIBUTE_RUNTIME, MORPH_RUNTIME

COMMON = {'max_distance_m': 0.01, 'max_normal_angle_degrees': 60.0, 'ambiguity_distance_m': 0.0001,
          'reject_ambiguous_components': True, 'limit': 50}
WEIGHTS = {'groups': [], 'influence_limit': 8, 'weight_epsilon': 1e-8, 'normalization_tolerance': 1e-4,
           'transfer_weight_tolerance': 1e-5,
           'discontinuity_l1_limit': 1.5, 'reject_discontinuities': False,
           'side_pairs': [], 'side_plane': {}, 'leakage_weight_limit': 0.05,
           'stress_poses': [], 'deformation_samples': 1024, 'deformation_error_m': 0.005,
           'deformation_fraction_tolerance': 0.001, 'collapse_ratio': 0.25, 'stretch_ratio': 2.0, 'area_ratio': 0.1,
           'deformation_vertex_limit': 250000}
ATTRIBUTES = {'attributes': [], 'materials': True, 'custom_properties': {'object': [], 'mesh': []}}
REGIONS = {'regions': {}, 'minimum_region_vertices': 8, 'region_weight': 0.1}


def _finite(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(name + ' must be finite in ' + str(low) + '..' + str(high))
    return float(value)


def _names(values, name, limit=128):
    if not isinstance(values, list) or len(values) > limit or any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
        raise ValueError(name + ' must contain unique nonempty names, at most ' + str(limit))
    return list(values)


def options(values=None, kind='weights'):
    defaults = {**COMMON, **(WEIGHTS if kind in ('weights', 'deformation') else ATTRIBUTES if kind == 'attributes' else {})}
    if kind == 'deformation':
        defaults.update(REGIONS)
    if values is not None and not isinstance(values, dict):
        raise ValueError('options must be an object')
    if set(values or {}) - set(defaults):
        raise ValueError('Unknown transfer options: ' + ', '.join(sorted(set(values) - set(defaults))))
    output = {**defaults, **(values or {})}
    for name, low, high in (('max_distance_m', 1e-7, 10), ('max_normal_angle_degrees', 0, 180), ('ambiguity_distance_m', 0, 1)):
        output[name] = _finite(output[name], name, low, high)
    for name in ('reject_ambiguous_components', 'materials', 'reject_discontinuities'):
        if name in output and not isinstance(output[name], bool):
            raise ValueError(name + ' must be boolean')
    for name, low, high in (('limit', 1, 200), ('influence_limit', 1, 256), ('deformation_samples', 16, 4096),
                            ('minimum_region_vertices', 1, 10000), ('deformation_vertex_limit', 1, 500000)):
        if name in output and (isinstance(output[name], bool) or not isinstance(output[name], int) or not low <= output[name] <= high):
            raise ValueError(name + ' must be an integer in ' + str(low) + '..' + str(high))
    for name, low, high in (('weight_epsilon', 0, 0.01), ('normalization_tolerance', 1e-8, 0.1),
                            ('transfer_weight_tolerance', 1e-8, 0.01),
                            ('discontinuity_l1_limit', 0, 2), ('leakage_weight_limit', 0, 1), ('deformation_error_m', 1e-7, 1),
                            ('deformation_fraction_tolerance', 0, 0.1), ('collapse_ratio', 1e-6, 1),
                            ('stretch_ratio', 1, 100), ('area_ratio', 1e-6, 1), ('region_weight', 1e-8, 1)):
        if name in output:
            output[name] = _finite(output[name], name, low, high)
    if 'groups' in output:
        output['groups'] = _names(output['groups'], 'groups', 256)
        if output['stress_poses']:
            output['stress_poses'] = validate_poses(output['stress_poses'])
        elif not isinstance(output['stress_poses'], list):
            raise ValueError('stress_poses must be a list')
        if not isinstance(output['side_pairs'], list) or len(output['side_pairs']) > 32:
            raise ValueError('side_pairs must contain at most 32 explicit positive/negative group pairs')
        for row in output['side_pairs']:
            if not isinstance(row, dict) or set(row) != {'positive', 'negative'} or not all(isinstance(v, str) and v for v in row.values()) or row['positive'] == row['negative']:
                raise ValueError('side_pairs need distinct explicit positive and negative group names')
        plane = output['side_plane']
        if not isinstance(plane, dict) or set(plane) - {'axis', 'offset_m', 'margin_m'}:
            raise ValueError('side_plane needs axis, offset_m and margin_m')
        axis = plane.get('axis', 0)
        if isinstance(axis, bool) or axis not in (0, 1, 2):
            raise ValueError('side_plane.axis must be 0, 1 or 2')
        output['side_plane'] = {'axis': axis, 'offset_m': _finite(plane.get('offset_m', 0), 'offset_m', -10000, 10000),
                                'margin_m': _finite(plane.get('margin_m', 0.001), 'margin_m', 0, 100)}
    if 'attributes' in output:
        output['attributes'] = _names(output['attributes'], 'attributes', 32)
        props = output['custom_properties']
        if not isinstance(props, dict) or set(props) != {'object', 'mesh'}:
            raise ValueError('custom_properties must explicitly name object and mesh key lists')
        output['custom_properties'] = {k: _names(v, 'custom_properties.' + k, 32) for k, v in props.items()}
        if not output['materials'] and not output['attributes'] and not any(output['custom_properties'].values()):
            raise ValueError('NOTHING_TO_TRANSFER: select materials, named attributes or explicit custom properties')
    if 'regions' in output:
        regions = output['regions']
        if not isinstance(regions, dict) or len(regions) > 32 or any(not isinstance(k, str) or not k for k in regions):
            raise ValueError('regions must map at most 32 explicit region labels to deformation groups')
        output['regions'] = {k: _names(v, 'regions.' + k, 32) for k, v in regions.items()}
        if any(not v for v in output['regions'].values()):
            raise ValueError('Each deformation region needs at least one explicit group')
    return output


def transfer(bridge, storage, operation_id, kind, relationship=None, mode='analyze', values=None, operation=None):
    runtimes = {'weights': WEIGHT_RUNTIME, 'attributes': ATTRIBUTE_RUNTIME, 'morphs': MORPH_RUNTIME}
    settings = options(values, kind) if mode in ('analyze', 'propose') else None
    return transaction(bridge, storage, operation_id, 'retopology_transfer_' + kind, runtimes[kind],
                       relationship=relationship, mode=mode, options=settings, operation=operation)


def validate_weights(bridge, storage, relationship, values=None, operation_id=''):
    settings = options(values, 'weights')
    checkpoint = None
    if settings['stress_poses']:
        require_mutable_asset(bridge, storage, 'M5 weight stress QA')
        checkpoint = create_checkpoint(bridge, storage, 'Before M5 explicit weight stress QA', operation_id, 'retopology_validate_weights')['manifest']
    result = m5_read(bridge, storage, relationship, TRANSFER_RUNTIME,
                     "result = mt_weight_report(low, params['options'])\n"
                     "if params['options'].get('stress_poses'):\n"
                     "    result['deformation_qa'] = mt_stress(high, low, params['options'])\n"
                     "    result['passed'] = result['passed'] and result['deformation_qa']['passed']\n"
                     "    result['errors'].extend(result['deformation_qa']['errors'])\n",
                     settings)
    result.update(status='success' if result['passed'] else 'error', checkpoint=checkpoint)
    return result


def deformation_regions(bridge, storage, relationship, values=None, operation_id=''):
    settings = options(values, 'deformation')
    checkpoint = None
    if settings['stress_poses']:
        require_mutable_asset(bridge, storage, 'M5 deformation-region stress QA')
        checkpoint = create_checkpoint(bridge, storage, 'Before M5 explicit deformation-region QA', operation_id, 'retopology_analyze_deformation_regions')['manifest']
    result = m5_read(bridge, storage, relationship, TRANSFER_RUNTIME,
                     "result = mt_deformation_regions(low, params['options'])\n"
                     "if params['options'].get('stress_poses'):\n"
                     "    result['stress'] = mt_stress(high, low, params['options'])\n"
                     "    result['passed'] = result['passed'] is not False and result['stress']['passed']\n"
                     "    result['errors'] = result['stress']['errors']\n", settings)
    result.update(status='error' if result.get('passed') is False else 'warning' if result.get('passed') is None else 'success', checkpoint=checkpoint)
    return result


def analyze_morphs(bridge, storage, relationship, values=None):
    settings = options(values, 'morphs')
    result = m5_read(bridge, storage, relationship, TRANSFER_RUNTIME, 'result = mt_morph_analysis(high, low)', settings)
    result['status'] = 'warning' if result['errors'] else 'success'
    return result


def register(tool, resources):
    @tool('production.retopology_analyze_deformation_regions')
    def regions(relationship: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Inspect explicit/deform-group articulation regions; optional checkpointed stress poses restore the rig."""
        return deformation_regions(*resources(), relationship, options, _operation_id)

    @tool('production.retopology_transfer_weights')
    def weights(relationship: str | None = None, mode: str = 'analyze', options: dict | None = None, operation: str | None = None, *, _operation_id='') -> dict:
        """Propose barycentric surface transfer to a new candidate; verify sums/influences/leakage and optional explicit poses."""
        return transfer(*resources(), _operation_id, 'weights', relationship, mode, options, operation)

    @tool('production.retopology_validate_weights')
    def weight_qa(relationship: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Bounded weight QA with configurable influence/normalization limits and explicit side pairs; optional stress QA."""
        return validate_weights(*resources(), relationship, options, _operation_id)

    @tool('production.retopology_analyze_morph_transfer', read_only=True)
    def morphs(relationship: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Measure source morphs and preservation; topology-changed morph transfer reports manual/later-pipeline required."""
        return analyze_morphs(*resources(), relationship, options)

    @tool('production.retopology_transfer_morphs')
    def morph_transfer(relationship: str | None = None, mode: str = 'analyze', options: dict | None = None, operation: str | None = None, *, _operation_id='') -> dict:
        """Only exact already-preserved morph dependencies are supported; changed-topology morphs are safely rejected."""
        return transfer(*resources(), _operation_id, 'morphs', relationship, mode, options, operation)

    @tool('production.retopology_transfer_attributes')
    def attributes(relationship: str | None = None, mode: str = 'analyze', options: dict | None = None, operation: str | None = None, *, _operation_id='') -> dict:
        """Explicit material/POINT/CORNER/FACE attribute/custom-property transfer with per-attribute QA on a new candidate."""
        return transfer(*resources(), _operation_id, 'attributes', relationship, mode, options, operation)
