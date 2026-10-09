"""The 35 high-level M6 units of work; no per-axis/keyframe tools."""
from __future__ import annotations

from . import service, store
from .measurements import observe
from .candidate import CandidateBridge
from ...common.m3_transaction import BASE_RUNTIME

STAGES = {
    'animation_generate_pose': 'POSE', 'animation_generate_blocking': 'BLOCKING',
    'animation_generate_breakdowns': 'BREAKDOWNS', 'animation_adjust_timing': 'TIMING',
    'animation_refine_arcs': 'ARCS', 'animation_refine_balance': 'BALANCE',
    'animation_generate_root_motion': 'ROOT', 'animation_generate_foot_contacts': 'CONTACTS',
    'animation_generate_effector_contact': 'EFFECTOR', 'animation_spline': 'SPLINE',
    'animation_polish_curves': 'CURVES', 'animation_add_overlap': 'OVERLAP',
    'animation_add_follow_through': 'FOLLOW_THROUGH',
}
OBSERVATIONS = {'animation_analyze_timing': 'timing', 'animation_analyze_spacing': 'spacing',
                'animation_analyze_arcs': 'arcs', 'animation_analyze_body_mechanics': 'mechanics'}
VALIDATION = {'animation_validate_pose', 'animation_validate_blocking', 'animation_analyze_pose_silhouette'}
RECIPES = {'animation_generate_locomotion': 'LOCOMOTION', 'animation_generate_idle': 'IDLE',
           'animation_generate_attack': 'ATTACK', 'animation_generate_hit_reaction': 'HIT_REACTION',
           'animation_generate_turn': 'TURN', 'animation_generate_start_stop': 'START_STOP'}
NAMES = sorted(set(STAGES)|set(OBSERVATIONS)|VALIDATION|set(RECIPES)|{
    'animation_create_brief', 'animation_generation_compatibility', 'animation_plan_poses',
    'animation_generation_plan', 'animation_generate', 'animation_generation_render',
    'animation_refine_generated', 'animation_accept_generated', 'animation_reject_generated'})


def dispatch(name, bridge, storage, operation_id, object_name=None, intent=None, brief=None,
             generation=None, options=None, apply=False):
    from .planner import create_brief, plan_poses
    options = dict(options or {})
    if name == 'animation_create_brief':
        return {'status': 'planned', 'brief': create_brief(intent or brief or {}), 'no_action_created': True}
    if name == 'animation_generation_compatibility':
        return service.compatibility(bridge, object_name, options)
    if name == 'animation_plan_poses':
        normalized = create_brief(intent or brief or {})
        comp = service.compatibility(bridge, object_name, dict(options, brief=normalized)) if object_name else None
        return {'status': 'planned', 'pose_plan': plan_poses(normalized, comp), 'no_action_created': True}
    if name == 'animation_generation_plan' or name in RECIPES:
        if generation is None:
            values = dict(intent or brief or {})
            if name in RECIPES:
                values['type'] = RECIPES[name]
            planned = service.plan_generation(bridge, storage, operation_id, object_name, values, options=options)
            if not apply or name == 'animation_generation_plan':
                return planned
            generation = planned['generation']
        return service.generate(bridge, storage, operation_id, generation, options, apply)
    if not generation:
        raise ValueError('GENERATION_REQUIRED: use the immutable reference returned by animation_generation_plan')
    if name in STAGES:
        return service.stage(bridge, storage, operation_id, generation, STAGES[name], options, apply)
    if name == 'animation_generate':
        return service.generate(bridge, storage, operation_id, generation, options, apply)
    if name == 'animation_generation_render':
        return service.render_generation(bridge, storage, operation_id, generation, options)
    if name == 'animation_refine_generated':
        return service.refine(bridge, storage, operation_id, generation, options, apply)
    if name in {'animation_accept_generated', 'animation_reject_generated'}:
        if name.endswith('reject_generated') and not apply:
            path, plan, state = store.load(storage, generation)
            return dict(service.public(path, plan, state), status='proposed', proposed_state='REJECTED', dry_run=True)
        return service.review_or_accept(bridge, storage, operation_id, generation, options.get('review'), apply,
                                        reject=name.endswith('reject_generated') and apply)
    path, plan, state = store.load(storage, generation)
    service.fresh(bridge, plan, state)
    if not state.get('candidate_action'):
        raise ValueError('CANDIDATE_REQUIRED: generate a pose/blocking first')
    if name in OBSERVATIONS:
        return observe(bridge, dict(plan, pose_plan=state.get('pose_plan',plan['pose_plan'])), state, OBSERVATIONS[name], options)
    if name in VALIDATION:
        from .runtime import GENERATION_RUNTIME, ANALYZE_READ, SILHOUETTE_READ
        body = SILHOUETTE_READ if name.endswith('pose_silhouette') else ANALYZE_READ
        params = dict(mapping=plan['mapping'], plan=state.get('pose_plan',plan['pose_plan']), brief=plan['brief'],
                      ground_height_m=plan['options'].get('ground_height_m', 0.), **options)
        return CandidateBridge(bridge, plan['object_name'], state['candidate_action']).run(
            BASE_RUNTIME+GENERATION_RUNTIME+body, {'object_name': plan['object_name'], 'options': params})
    raise ValueError('Unknown M6 operation')


def register(tool, resources):
    for name in NAMES:
        def make(operation):
            def unit(object_name: str | None = None, intent: dict | None = None, brief: dict | None = None,
                     generation: str | None = None, options: dict | None = None, apply: bool = False,
                     *, _operation_id='') -> dict:
                bridge, storage = resources()
                return dispatch(operation, bridge, storage, _operation_id, object_name, intent, brief, generation, options, apply)
            unit.__name__ = operation
            unit.__doc__ = (operation + ': high-level M6 unit. Plan first, apply=false by default; generation is the immutable plan.json reference. '
                            'Explicit mapping and compatible body rig required. Source Actions retained; technical QA and temporal artistic review are distinct. '
                            'options carries declared stage parameters, thresholds or visual review; unsupported adapters return concrete errors.')
            return unit
        tool('production.'+name, read_only=name in {'animation_create_brief'})(make(name))
