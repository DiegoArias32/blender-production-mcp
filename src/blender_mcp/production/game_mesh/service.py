"""Game mesh normal/triangulation/tangent contracts; never alter an authoring mesh in place."""
from __future__ import annotations

import math

from ..common.m5 import m5_read, transaction
from ..common.asset_safety import require_mutable_asset
from ..checkpoint import create_checkpoint
from ..deformation import poses as validate_poses
from .runtime import GAME_RUNTIME, NORMAL_RUNTIME, TRI_RUNTIME

DEFAULTS = {'normal_policy': 'preserve', 'custom_normals': 'preserve', 'confirm_rebuild_custom_normals': False,
            'normal_continuity_degrees': 60.0, 'quad_method': 'FIXED', 'ngon_method': 'EAR_CLIP',
            'triangulation_area_tolerance': 0.001, 'max_distance_m': 0.001, 'max_normal_angle_degrees': 60.0,
            'sample_count': 1024, 'uv_layer': None, 'uv_area_epsilon': 1e-12, 'normal_convention': 'OPENGL', 'limit': 50}
DEFAULTS.update({'stress_poses': [], 'deformation_samples': 1024, 'deformation_error_m': 0.001,
                 'deformation_fraction_tolerance': 0.001, 'collapse_ratio': 0.25, 'stretch_ratio': 2.0,
                 'area_ratio': 0.1, 'deformation_vertex_limit': 250000})


def options(values=None):
    if values is not None and not isinstance(values, dict):
        raise ValueError('options must be an object')
    if set(values or {}) - set(DEFAULTS):
        raise ValueError('Unknown game-mesh options: ' + ', '.join(sorted(set(values) - set(DEFAULTS))))
    output = {**DEFAULTS, **(values or {})}
    choices = {'normal_policy': ('preserve', 'flat', 'smooth'), 'custom_normals': ('preserve', 'rebuild'),
               'quad_method': ('FIXED', 'FIXED_ALTERNATE', 'BEAUTY', 'SHORT_EDGE', 'LONG_EDGE'),
               'ngon_method': ('EAR_CLIP', 'BEAUTY'), 'normal_convention': ('OPENGL', 'DIRECTX')}
    for key, values in choices.items():
        if output[key] not in values:
            raise ValueError(key + ' must be one of ' + ', '.join(values))
    if not isinstance(output['confirm_rebuild_custom_normals'], bool):
        raise ValueError('confirm_rebuild_custom_normals must be boolean')
    if output['custom_normals'] == 'rebuild' and not output['confirm_rebuild_custom_normals']:
        raise ValueError('CUSTOM_NORMAL_REBUILD_REQUIRES_CONFIRMATION')
    for key, low, high in (('normal_continuity_degrees', 0, 180), ('max_normal_angle_degrees', 0, 180),
                            ('triangulation_area_tolerance', 0, 1), ('max_distance_m', 1e-8, 1), ('uv_area_epsilon', 1e-16, 1e-4)):
        value = output[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(key + ' must be finite in ' + str(low) + '..' + str(high))
        output[key] = float(value)
    for key, low, high in (('deformation_error_m', 1e-7, 1), ('deformation_fraction_tolerance', 0, 0.1),
                            ('collapse_ratio', 1e-6, 1), ('stretch_ratio', 1, 100), ('area_ratio', 1e-6, 1)):
        value = output[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(key + ' must be finite in ' + str(low) + '..' + str(high))
    for key, low, high in (('limit', 1, 200), ('sample_count', 16, 4096), ('deformation_samples', 16, 4096), ('deformation_vertex_limit', 1, 500000)):
        if isinstance(output[key], bool) or not isinstance(output[key], int) or not low <= output[key] <= high:
            raise ValueError(key + ' must be an integer in ' + str(low) + '..' + str(high))
    if output['uv_layer'] is not None and (not isinstance(output['uv_layer'], str) or not output['uv_layer']):
        raise ValueError('uv_layer must be a nonempty name or null')
    if output['stress_poses']:
        output['stress_poses'] = validate_poses(output['stress_poses'])
    elif not isinstance(output['stress_poses'], list):
        raise ValueError('stress_poses must be a list')
    return output


def prepare(bridge, storage, operation_id, kind, relationship=None, mode='analyze', values=None, operation=None):
    settings = options(values) if mode in ('analyze', 'propose') else None
    runtime = NORMAL_RUNTIME if kind == 'normals' else TRI_RUNTIME
    return transaction(bridge, storage, operation_id,
                       'game_mesh_prepare_normals' if kind == 'normals' else 'game_mesh_triangulation_preview', runtime,
                       relationship=relationship, mode=mode, options=settings, operation=operation)


def validate(bridge, storage, relationship, kind, values=None, operation_id=''):
    settings = options(values)
    functions = {'normals': 'gm_normal_audit', 'triangulation': 'gm_tri_validation', 'tangents': 'gm_tangents'}
    checkpoint = None
    if kind == 'triangulation' and settings['stress_poses']:
        require_mutable_asset(bridge, storage, 'M5 triangulation explicit stress QA')
        checkpoint = create_checkpoint(bridge, storage, 'Before M5 triangulation explicit stress QA', operation_id, 'game_mesh_validate_triangulation')['manifest']
    suffix = ', True, high, params["relation"]' if kind == 'triangulation' else ''
    result = m5_read(bridge, storage, relationship, GAME_RUNTIME, 'result = ' + functions[kind] + "(low, params['options']" + suffix + ')', settings)
    result['status'] = 'success' if result['passed'] else 'error'
    result['checkpoint'] = checkpoint
    return result


def register(tool, resources):
    @tool('production.game_mesh_prepare_normals')
    def normals(relationship: str | None = None, mode: str = 'analyze', options: dict | None = None, operation: str | None = None, *, _operation_id='') -> dict:
        """Inspect/propose normals on a new candidate; preserve custom normals by default, rebuild only after explicit confirmation."""
        return prepare(*resources(), _operation_id, 'normals', relationship, mode, options, operation)

    @tool('production.game_mesh_triangulation_preview')
    def triangulation(relationship: str | None = None, mode: str = 'analyze', options: dict | None = None, operation: str | None = None, *, _operation_id='') -> dict:
        """Create a deterministic triangulated evaluation candidate; source authoring mesh, weights and morphs remain intact."""
        return prepare(*resources(), _operation_id, 'triangulation', relationship, mode, options, operation)

    @tool('production.game_mesh_validate_triangulation')
    def triangle_qa(relationship: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Independently triangulate two temporary copies and compare reproducibility/counts/geometry/normals/morphs."""
        return validate(*resources(), relationship, 'triangulation', options, _operation_id)

    @tool('production.game_mesh_validate_tangents', read_only=True)
    def tangents(relationship: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Use installed Blender calc_tangents on a copy; reject missing/degenerate UVs or invalid corner tangent frames."""
        return validate(*resources(), relationship, 'tangents', options)
