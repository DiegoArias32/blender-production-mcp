"""Compact host contracts for the eleven M5 retopology tools."""
from __future__ import annotations

import math
from ..common import m5
from ..mesh.service import validate_regions
from .runtime import RETOPOLOGY_RUNTIME, REDUCTION_RUNTIME, PROJECTION_RUNTIME

DEFAULTS = {'samples': 2048, 'max_distance_m': .01, 'max_normal_angle_degrees': 80.0,
            'ambiguity_distance_m': .0001, 'reject_ambiguous_components': True,
            'silhouette_resolution': 128, 'maximum_silhouette_distance_m': .02,
            'minimum_silhouette_iou': .95, 'area_epsilon_m2': 1e-14,
            'minimum_triangle_quality': .02, 'maximum_sliver_triangles': 0,
            'duplicate_distance_m': 1e-7, 'allow_boundary': False, 'maximum_components': 128,
            'density_grid': 8, 'curvature_weight': 2.0, 'preserve_region_boundaries': True,
            'protected_weight_tolerance': 1e-5, 'region_budget_tolerance_fraction': .05}
STRATEGIES = ('PRESERVE_AND_OPTIMIZE', 'GUIDED_DECIMATION', 'LOCAL_RETOPOLOGY',
              'REMESH_AND_PROJECT', 'HYBRID', 'MANUAL_REQUIRED')


def _number(value, name, minimum, maximum, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(name + ' must be finite')
    if integer and not isinstance(value, int):
        raise ValueError(name + ' must be an integer')
    if not minimum <= value <= maximum:
        raise ValueError(name + ' outside allowed range ' + str(minimum) + '..' + str(maximum))
    return value


def regions(rows):
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError('regions must be at most 32 explicit definitions')
    if not rows:
        return []
    names = set()
    output = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - {'name', 'definition', 'role', 'protected', 'deformation',
                                                 'priority', 'silhouette_priority', 'tolerance_m', 'budget', 'minimum_density_per_m2'}:
            raise ValueError('Unknown priority region field')
        name = row.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Region names must be unique nonempty strings')
        # Long topology-index lists are deliberately unsupported in compact M5 proposals.
        if row.get('definition', {}).get('method') == 'indices':
            raise ValueError('M5_COMPACT_REGIONS_REQUIRED: use bounds, vertex_group, face_set, mask or dominant_group')
        base = validate_regions([{'name': name, 'role': row.get('role', 'unassigned'),
                                  'definition': row.get('definition'), 'tolerance_m': row.get('tolerance_m', 1e-5)}])[0]
        for flag in ('protected', 'deformation'):
            if not isinstance(row.get(flag, False), bool):
                raise ValueError(flag + ' must be boolean')
            base[flag] = row.get(flag, False)
        for key in ('priority', 'silhouette_priority'):
            base[key] = _number(row.get(key, 1.0), key, .05, 1000)
        if 'minimum_density_per_m2' in row:
            base['minimum_density_per_m2'] = _number(row['minimum_density_per_m2'], 'minimum_density_per_m2', 0, 1e12)
        budget = row.get('budget', {})
        if not isinstance(budget, dict) or len(budget) > 1 or set(budget) - {'absolute', 'percentage', 'density_weight'}:
            raise ValueError('region budget must select absolute, percentage or density_weight')
        if 'absolute' in budget: _number(budget['absolute'], 'absolute budget', 1, 10_000_000, True)
        if 'percentage' in budget: _number(budget['percentage'], 'percentage budget', .001, 100)
        if 'density_weight' in budget: _number(budget['density_weight'], 'density_weight', .001, 1000)
        base['budget'] = budget
        output.append(base); names.add(name)
    return output


def options(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError('options must be an object')
    allowed = set(DEFAULTS) | {'regions', 'target_triangles', 'minimum_triangles', 'maximum_triangles',
                             'strategy', 'region', 'target_reduction', 'custom_normals_policy', 'views'}
    if set(value) - allowed:
        raise ValueError('Unknown retopology options: ' + ', '.join(sorted(set(value) - allowed)))
    result = dict(DEFAULTS, **value)
    ranges = {'samples': (32, 32768, True), 'max_distance_m': (1e-7, 10, False),
              'max_normal_angle_degrees': (0, 180, False), 'ambiguity_distance_m': (0, 1, False),
              'silhouette_resolution': (32, 256, True), 'maximum_silhouette_distance_m': (0, 10, False),
              'minimum_silhouette_iou': (0, 1, False), 'area_epsilon_m2': (1e-20, .01, False),
              'minimum_triangle_quality': (0, 1, False), 'maximum_sliver_triangles': (0, 10_000_000, True),
              'duplicate_distance_m': (1e-10, .01, False), 'maximum_components': (1, 10000, True),
              'density_grid': (2, 32, True), 'curvature_weight': (0, 100, False),
              'protected_weight_tolerance': (0, .01, False), 'target_triangles': (1, 10_000_000, True),
              'minimum_triangles': (1, 10_000_000, True), 'maximum_triangles': (1, 10_000_000, True)}
    ranges['target_reduction'] = (0, .95, False)
    ranges['region_budget_tolerance_fraction'] = (0, .5, False)
    for key, bounds in ranges.items():
        if key in result: _number(result[key], key, *bounds)
    for key in ('reject_ambiguous_components', 'allow_boundary', 'preserve_region_boundaries'):
        if not isinstance(result[key], bool):
            raise ValueError(key + ' must be boolean')
    if 'regions' in result: result['regions'] = regions(result['regions'])
    if 'strategy' in result and result['strategy'] not in STRATEGIES:
        raise ValueError('strategy must be an explicit supported strategy class')
    if 'region' in result and (not isinstance(result['region'], str) or not result['region']):
        raise ValueError('region must name an explicit editable region')
    if result.get('custom_normals_policy', 'preserve') not in ('preserve', 'rebuild'):
        raise ValueError('custom_normals_policy must be preserve or rebuild')
    if 'views' in result and (not isinstance(result['views'], list) or not 1 <= len(result['views']) <= 4
                              or len(set(result['views'])) != len(result['views'])
                              or any(view not in ('FRONT', 'SIDE', 'THREE_QUARTER', 'BACK') for view in result['views'])):
        raise ValueError('views must be unique FRONT, SIDE, THREE_QUARTER, BACK')
    return result


def analyze(bridge, object_name, settings=None, kind='topology'):
    settings = options(settings)
    if not isinstance(object_name, str) or not object_name:
        raise ValueError('object_name required')
    body = {'topology': 'result=rt_topology(obj,params["options"])',
            'density': 'result=rt_density(obj,params["options"])',
            'priority': 'result={"priority":rt_priority(obj,params["options"])["summary"],"apply":False}',
            'budget': 'result=rt_budget(obj,params["options"])',
            'plan': 'result=rt_plan(obj,obj,params["options"],{})'}[kind]
    data = bridge.run(RETOPOLOGY_RUNTIME + '\nobj=m5_obj(params["object_name"])\n' + body
                      + '\nresult["signature"]=m5_signature(obj)', {'object_name': object_name, 'options': settings})
    data['status'] = 'warning' if data.get('errors') or data.get('warnings') or data.get('reasons') else 'success'
    return data


def create_candidate(bridge, storage, operation_id, object_name, source_fingerprint, settings=None):
    settings = settings or {}
    if not isinstance(settings, dict) or set(settings) - {'regions', 'target_object', 'metadata'}:
        raise ValueError('Candidate options allow regions, target_object and metadata')
    metadata = dict(settings.get('metadata', {}))
    if 'regions' in settings: metadata['regions'] = regions(settings['regions'])
    return m5.create_relationship(bridge, storage, operation_id, object_name, source_fingerprint,
                                  {k: settings[k] for k in ('target_object',) if k in settings} | {'metadata': metadata})


def correction(bridge, storage, operation_id, relationship=None, mode='analyze', settings=None, operation=None, kind='reduce'):
    validated = options(settings) if mode in ('analyze', 'propose') else (settings or {})
    runtime = REDUCTION_RUNTIME if kind == 'reduce' else PROJECTION_RUNTIME
    return m5.transaction(bridge, storage, operation_id, 'retopology_' + kind, runtime,
                          relationship=relationship, mode=mode, options=validated, operation=operation)


def compare(bridge, storage, relationship, settings=None, kind='surface'):
    validated = options(settings)
    body = {'surface': 'settings=dict(params["options"],regions=[{k:v for k,v in row.items() if k!="members"} for row in rt_regions(high,params["options"],params["relation"])])\nresult=m5_compare(high,low,settings)',
            'silhouette': 'result=rt_silhouette(high,low,params["options"])',
            'validate': 'result=rt_topology(low,params["options"])\nresult["protected_regions"]=rt_protected(high,low,params["options"],params["relation"])\nresult["passed"]=result["passed"] and result["protected_regions"]["passed"]\nresult["errors"].extend(result["protected_regions"]["errors"])'}[kind]
    result = m5.m5_read(bridge, storage, relationship, RETOPOLOGY_RUNTIME,
                       'high=m5_obj(params["high_name"]);low=m5_obj(params["target_name"])\n' + body, validated)
    result['status'] = 'success' if result['passed'] else 'error'
    return result
