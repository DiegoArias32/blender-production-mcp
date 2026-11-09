"""M10 tool catalogue. 37 required units plus cinematic_review and cinematic_status.

READ_ONLY tools never change frame, Action, pose or any datablock. TRANSIENT tools sample the candidate timeline
(frame_set) and restore it before returning. Every other tool accepts operation_id (writers: Blender changes or
persistent artifacts such as renders, registry records and ledger events).
"""
from __future__ import annotations

from . import service

REQUIRED = (
    'cinematic_inspect_scene', 'cinematic_register_actor', 'cinematic_inspect_actor', 'cinematic_create_brief',
    'cinematic_plan_beats', 'cinematic_plan_shots', 'cinematic_create_camera', 'cinematic_configure_camera',
    'cinematic_animate_camera', 'cinematic_analyze_composition', 'cinematic_validate_continuity',
    'cinematic_generate_previs', 'cinematic_stage_actors', 'cinematic_plan_actor_paths', 'cinematic_sync_actions',
    'cinematic_plan_interaction', 'cinematic_analyze_contacts', 'cinematic_integrate_facial',
    'cinematic_integrate_secondary', 'cinematic_manage_props', 'cinematic_plan_lighting', 'cinematic_setup_lighting',
    'cinematic_validate_lighting', 'cinematic_manage_timeline', 'cinematic_create_cuts', 'cinematic_plan_focus',
    'cinematic_create_candidate', 'cinematic_verify_integrity', 'cinematic_analyze_actor_collisions',
    'cinematic_analyze_visibility', 'cinematic_analyze_motion', 'cinematic_render_preview', 'cinematic_render_plan',
    'cinematic_quality_report', 'cinematic_refine', 'cinematic_accept', 'cinematic_reject',
)
EXTRA = ('cinematic_review', 'cinematic_status')
NAMES = REQUIRED + EXTRA
READ_ONLY = frozenset({
    'cinematic_inspect_scene', 'cinematic_inspect_actor', 'cinematic_create_brief', 'cinematic_plan_beats',
    'cinematic_plan_shots', 'cinematic_plan_actor_paths', 'cinematic_plan_interaction', 'cinematic_plan_lighting',
    'cinematic_render_plan', 'cinematic_verify_integrity', 'cinematic_status',
})
TRANSIENT = frozenset({
    'cinematic_analyze_composition', 'cinematic_validate_continuity', 'cinematic_analyze_contacts',
    'cinematic_analyze_actor_collisions', 'cinematic_analyze_visibility', 'cinematic_analyze_motion',
})


def register(tool, resources):
    def make(name):
        def execute(cinematic: str | None = None, actor: str | None = None, object_name: str | None = None,
                    brief: dict | None = None, options: dict | None = None, apply: bool = False,
                    *, _operation_id='') -> dict:
            bridge, storage = resources()
            return service.dispatch(name, bridge, storage, _operation_id, cinematic, actor, object_name, brief,
                                    options, apply)
        execute.__name__ = name
        execute.__doc__ = (
            name + ': M10 cinematic production on a candidate scene of a WORKING_COPY/FIXTURE. Planning tools are '
            'pure; mutations propose by default and need apply=true plus the cinematic plan reference. Source Actions, '
            'MASTER files, materials, caches and the source scene are never changed; every change is recorded and '
            'reversible. Technical QA is separate from the mandatory real visual review.')
        return execute
    for name in NAMES:
        tool('production.' + name, read_only=name in READ_ONLY or name in TRANSIENT)(make(name))
