"""High-level material work units; the shared MCP envelope owns client idempotency."""
from __future__ import annotations

from . import service

REQUIRED = {
    'material_inspect', 'material_analyze_nodes', 'material_compatibility_report',
    'material_analyze_pbr', 'material_validate_pbr', 'material_create_pbr',
    'texture_inspect', 'texture_validate_channels', 'texture_validate_colorspace',
    'texture_dependency_report', 'material_validate_normals', 'material_validate_height',
    'material_validate_tangent_workflow', 'material_configure_sss',
    'material_configure_transmission', 'material_configure_opacity', 'material_configure_emission',
    'material_create_profile', 'material_apply_profile', 'material_validate_profile',
    'material_validate_uv', 'material_validate_udim', 'material_analyze_texture_sets',
    'material_create_candidate', 'material_apply_candidate', 'material_accept', 'material_reject',
    'material_build_shader', 'material_validate_graph', 'material_quality_report',
    'material_render_preview', 'material_render_multilighting', 'material_compare_before_after',
    'material_performance_report', 'material_create_handoff',
}
EXTRA = {'material_review', 'material_refine', 'material_status'}
NAMES = REQUIRED | EXTRA
READ_ONLY = {
    'material_inspect', 'material_analyze_nodes', 'material_compatibility_report',
    'material_analyze_pbr', 'material_validate_pbr', 'texture_inspect',
    'texture_validate_channels', 'texture_validate_colorspace', 'texture_dependency_report',
    'material_validate_normals', 'material_validate_height', 'material_validate_tangent_workflow',
    'material_create_profile', 'material_validate_profile', 'material_validate_uv',
    'material_validate_udim', 'material_analyze_texture_sets', 'material_validate_graph',
    'material_quality_report', 'material_performance_report', 'material_status',
}


def register(tool, resources):
    def make(name):
        def execute(object_name: str | None = None, material_name: str | None = None,
                    material: str | None = None, definition: dict | None = None,
                    profile: str | None = None, options: dict | None = None,
                    apply: bool = False, *, _operation_id='') -> dict:
            bridge, storage = resources()
            return service.dispatch(name, bridge, storage, _operation_id, object_name,
                                    material_name, material, definition, profile, options, apply)
        execute.__name__ = name
        execute.__doc__ = (
            name + ': inspect or propose by default; apply=true writes only owned candidate materials. '
            'Use material plan references for lifecycle calls, explicit channels/profiles, '
            'technical QA, controlled lighting and a real visual review before acceptance. '
            'Original slots, images, UVs, geometry and animation remain unchanged.')
        return execute
    for name in sorted(NAMES):
        tool('production.' + name, read_only=name in READ_ONLY)(make(name))
