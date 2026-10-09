"""M6 high-level generation, staged candidates, freshness and explicit lifecycle."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import time
from uuid import uuid4
from importlib.metadata import version

from . import store
from .candidate import CandidateBridge
from .qa import technical, _thresholds
from .render import temporal
from .window import validate_pose_window
from ...common.m3_transaction import BASE_RUNTIME, transaction
from ...common.operations import fingerprint
from ...common.asset_safety import require_mutable_asset
from ...checkpoint import create_checkpoint
from ...common.storage import write_json


def _runtime(stage=None):
    from .runtime import runtime_for_stage
    return runtime_for_stage(stage)


def compatibility(bridge, object_name, options=None):
    body = BASE_RUNTIME + _runtime() + r'''
obj=m2_object(params['object_name'])
result=m6_compatibility(obj,params['options'])
_source=obj.animation_data.action if obj.animation_data else None
result['source_action']=_source.name if _source else None
result['source_fingerprint']=m2_digest(m3_source_snapshot(_source))
result['filepath']=bpy.data.filepath
result['scene']=bpy.context.scene.name
result['stamps']=[s.get('production_asset') for s in bpy.data.scenes]
'''
    return bridge.run(body, {'object_name': object_name, 'options': options or {}})


def plan_generation(bridge, storage, operation_id, object_name, intent=None, brief=None, options=None):
    from .planner import create_brief, plan_poses
    options = dict(options or {})
    normalized = create_brief(intent or {}) if brief is None else create_brief(brief)
    options['brief'] = normalized
    _thresholds(options, options.get('ground_height_m', 0.))
    comp = compatibility(bridge, object_name, options)
    pose_plan = plan_poses(normalized, comp)
    data = dict(object_name=object_name, brief=normalized, pose_plan=pose_plan,
                compatibility=comp, mapping=comp.get('mapping', options.get('mapping', {})),
                options=options, source_action=comp.get('source_action'), source_fingerprint=comp.get('source_fingerprint'),
                rig_fingerprint=comp.get('rig_fingerprint'), filepath=comp.get('filepath'), stamps=comp.get('stamps'),
                supported=comp.get('supported', False) and pose_plan.get('supported', True),
                warnings=comp.get('warnings', []) + pose_plan.get('warnings', []),
                no_action_created=True)
    reference, saved = store.save(storage, operation_id, data)
    return dict(saved, generation=reference, state='PLANNED', status='planned',
                metrics=pose_plan.get('metrics', {}))


def fresh(bridge, plan, state=None):
    comp = compatibility(bridge, plan['object_name'], plan['options'])
    if state and state.get('state') == 'ACCEPTED':
        # Acceptance intentionally activates the retained candidate. Read its
        # original source independently; do not mistake that activation for an
        # external edit or stop verifying the immutable original Action.
        if comp.get('source_action') not in {plan['source_action'], state.get('candidate_action')}:
            raise ValueError('STALE_PROPOSAL: unrelated active Action after acceptance')
        source = bridge.run(BASE_RUNTIME + r'''
name=params['source_action']
action=bpy.data.actions.get(name) if name is not None else None
if name is not None and action is None:
    raise ValueError('STALE_PROPOSAL: original source Action missing after acceptance')
result={'source_action':name,'source_fingerprint':m2_digest(m3_source_snapshot(action))}
''', {'source_action': plan['source_action']})
        comp = dict(comp, **source)
    if (not comp.get('supported') or comp.get('rig_fingerprint') != plan['rig_fingerprint']
            or comp.get('source_action') != plan['source_action']
            or comp.get('source_fingerprint') != plan['source_fingerprint']
            or comp.get('filepath') != plan['filepath'] or comp.get('stamps') != plan['stamps']):
        raise ValueError('STALE_PROPOSAL: rig/source Action/file/asset context changed; plan again')
    if state and state.get('candidate_action'):
        snapshot = candidate_snapshot(bridge, plan['object_name'], state['candidate_action'])
        if snapshot['fingerprint'] != state.get('candidate_fingerprint'):
            raise ValueError('STALE_PROPOSAL: candidate content changed since this generation event')
    return comp


def candidate_snapshot(bridge, object_name, action_name):
    return bridge.run(BASE_RUNTIME + r'''
obj=m2_object(params['object_name'])
action=bpy.data.actions.get(params['action_name'])
if action is None: raise ValueError('CANDIDATE_MISSING: generated Action absent')
result={'action':action.name,'fingerprint':m2_digest(m2_snapshot(action)), 'summary':m2_curve_summary(action)}
''', {'object_name': object_name, 'action_name': action_name})


def public(path, plan, state):
    qa = state.get('technical_qa')
    render = state.get('render')
    qa_summary = {k:qa[k] for k in ('status','passed','gates','metrics','errors','thresholds','advisory_gates','warnings') if k in qa} if qa else None
    render_summary = {k:render[k] for k in ('status','output_dir','temporal_complete','candidate_action','animations','metrics') if k in render} if render else None
    return dict(generation=str(path), generation_id=plan['generation_id'], object_name=plan['object_name'],
                state=state.get('state'), candidate_action=state.get('candidate_action'),
                candidate_fingerprint=state.get('candidate_fingerprint'), stage=state.get('stage'),
                refinement_cycles=state.get('refinement_cycles', 0), technical_qa=qa_summary,
                render=render_summary, visual_review=state.get('visual_review'),
                checkpoint=state.get('checkpoint'), source_action=plan['source_action'],
                warnings=plan.get('warnings', []), metrics=state.get('metrics', {}),
                evidence_dir=str(path.parent))


def stage(bridge, storage, operation_id, generation, name, options=None, apply=False):
    path, plan, state = store.load(storage, generation)
    store.mutable(state)
    fresh(bridge, plan, state)
    if not plan['supported']:
        raise ValueError('UNSUPPORTED_RIG: generation compatibility/pose plan did not pass')
    change = dict(options or {})
    validate_pose_window(change.get('pose_plan', state.get('pose_plan', plan['pose_plan'])),
                         plan['brief'], name, change)
    if not apply:
        return dict(public(path, plan, state), status='proposed', dry_run=True,
                    proposed_stage=name, proposed_changes=change, no_action_created=True)
    if state.get('pending_refinement'):
        raise ValueError('REFINEMENT_REVIEW_REQUIRED: finish current cycle before another stage')
    if name not in {'POSE', 'BLOCKING'} and not state.get('blocking_review_complete'):
        raise ValueError('BLOCKING_REVIEW_REQUIRED: create and review a full blocking before progression')
    if state.get('candidate_action') and state.get('stage') == 'BLOCKING' and name != 'BLOCKING':
        reviewed = store.review_gate(state.get('visual_review'), state.get('render'))
        if reviewed['decision'] != 'ACCEPT':
            raise ValueError('BLOCKING_REVIEW_REQUIRED: blocking temporal review must permit progression')
        if not state.get('technical_qa', {}).get('passed'):
            raise ValueError('BLOCKING_QA_FAILED: resolve structural/contact QA before progression')
    require_mutable_asset(bridge, storage, 'M6 '+name)
    original = state.get('candidate_action')
    evaluator = CandidateBridge(bridge, plan['object_name'], original) if original else bridge
    stage_plan = deepcopy(change.pop('pose_plan', state.get('pose_plan', plan['pose_plan'])))
    if name == 'TIMING' and change.get('phase_fractions'):
        from .planner import create_brief, plan_poses
        revised_intent = dict(plan['brief']['user_requirements'], phase_fractions=change.pop('phase_fractions'))
        stage_plan = plan_poses(create_brief(revised_intent), plan['compatibility'])
    validate_pose_window(stage_plan, plan['brief'], name, change)
    if name in {'ARCS','BALANCE'} and stage_plan == state.get('pose_plan', plan['pose_plan']):
        raise ValueError('REVISED_POSE_PLAN_REQUIRED: analyze first, then supply explicit reviewed semantic pose changes')
    qa_options = dict(plan['options'], **state.get('qa_options', {}))
    threshold_names = set(_thresholds(qa_options, qa_options.get('ground_height_m', 0.))) | {'contact_tolerance_m', 'maximum_angular_step_deg'}
    inherited = {key:value for key,value in qa_options.items() if key in threshold_names and key != 'ground_height_m'}
    inherited.update({key:value for key,value in change.items() if key in threshold_names and key != 'ground_height_m'})
    for key in inherited:
        change.pop(key, None)
    qa_options.update(inherited)
    _thresholds(qa_options, qa_options.get('ground_height_m', 0.))
    parameters = dict(inherited, brief=plan['brief'], plan=stage_plan, mapping=plan['mapping'], stage=name,
                      generation_id=plan['generation_id'], candidate_name=change.pop('candidate_name', None),
                      ground_height_m=plan['options'].get('ground_height_m', 0.), operation_id=operation_id,
                      created_utc=plan['created_at'], mcp_version=version('blender-production-mcp'),
                      generation_ref=str(path), brief_ref=str(path), **change)
    proposed = transaction(evaluator, storage, operation_id+'-propose', 'animation_generation_'+name.lower(),
                           _runtime(name), plan['object_name'], 'propose', parameters)
    if not proposed['proposal'].get('supported'):
        return dict(public(path, plan, state), status='unsupported', supported=False,
                    reasons=proposed['proposal'].get('reasons', []), proposal=proposed['proposal'], no_action_created=True)
    result = transaction(evaluator, storage, operation_id, 'animation_generation_'+name.lower(),
                         _runtime(name), plan['object_name'], 'preview', {}, proposed['operation'])
    if result.get('numeric_verification') != 'pass':
        return dict(result, generation=str(path), generation_id=plan['generation_id'])
    action = result['working_action']
    measured_plan = dict(plan, pose_plan=stage_plan, stage=name)
    measured = technical(bridge, measured_plan, action, qa_options)
    snapshot = candidate_snapshot(bridge, plan['object_name'], action)
    marked = _mark_action(bridge, plan, {'candidate_action':action, 'candidate_fingerprint':snapshot['fingerprint'],
                                       'technical_qa':measured, 'generation':str(path)}, 'GENERATED_CANDIDATE')
    state.update(state='GENERATED_CANDIDATE', stage=name, candidate_action=action,
                 candidate_fingerprint=marked['fingerprint'], technical_qa=measured,
                 checkpoint=result.get('checkpoint'), render=None, visual_review=None,
                 metrics=result.get('metrics', {}), last_operation=proposed['operation'], pose_plan=stage_plan)
    state['generation'] = str(path)
    state['qa_options'] = {key:value for key,value in qa_options.items() if key in threshold_names}
    if name in {'POSE', 'BLOCKING'}:
        state['blocking_review_complete'] = False
    store.event(path.parent, state)
    return dict(public(path, plan, state), status='candidate_created', dry_run=False,
                numeric_verification=result['numeric_verification'], source_intact=result.get('source_intact'),
                visual_verification='required')


def generate(bridge, storage, operation_id, generation, options=None, apply=False):
    path, plan, state = store.load(storage, generation)
    if not apply:
        return dict(public(path, plan, state), status='planned', stages=['BLOCKING', 'BREAKDOWNS', 'SPLINE'],
                    no_action_created=True, pose_plan=plan['pose_plan'])
    if not state.get('candidate_action'):
        report = stage(bridge, storage, operation_id+'-blocking', generation, 'BLOCKING', options, True)
        if report.get('status') != 'candidate_created':
            return report
        rendered = render_generation(bridge, storage, operation_id+'-blocking-visual', generation)
        return dict(rendered, status='blocking_review_required', accepted=False,
                    next_step='Review the complete blocking temporal preview; animation_accept_generated with explicit review and apply=false records REVIEWED without accepting.')
    if state.get('stage') != 'BLOCKING' or state.get('state') != 'REVIEWED':
        raise ValueError('BLOCKING_REVIEW_REQUIRED: inspect/review blocking before proceeding; use explicit stage tools afterward')
    steps = []
    for index, kind in enumerate(('BREAKDOWNS', 'SPLINE')):
        report = stage(bridge, storage, operation_id+'-'+str(index), generation, kind, options, True)
        steps.append({'stage': kind, 'action': report.get('candidate_action'), 'status': report['status']})
        if report['status'] != 'candidate_created':
            return dict(report, stages=steps)
    return dict(report, stages=steps, accepted=False)


def render_generation(bridge, storage, operation_id, generation, options=None):
    path, plan, state = store.load(storage, generation)
    store.mutable(state); fresh(bridge, plan, state)
    if state.get('pending_refinement'):
        raise ValueError('REFINEMENT_REVIEW_REQUIRED: review the rendered pending candidate before another render')
    if not state.get('candidate_action'):
        raise ValueError('CANDIDATE_REQUIRED: generate blocking first')
    result = temporal(bridge, storage, operation_id, plan, state['candidate_action'], options)
    state.update(render=result, visual_review=None)
    store.event(path.parent, state)
    return dict(public(path, plan, state), status=result['status'], checkpoint=result.get('checkpoint'))


def review_or_accept(bridge, storage, operation_id, generation, review=None, apply=False, reject=False):
    path, plan, state = store.load(storage, generation)
    store.mutable(state); fresh(bridge, plan, state)
    if state.get('pending_refinement'):
        raise ValueError('REFINEMENT_REVIEW_REQUIRED: resolve the pending cycle before lifecycle changes')
    require_mutable_asset(bridge, storage, 'M6 lifecycle')
    if reject:
        checkpoint = create_checkpoint(bridge, storage, 'Before rejecting M6 candidate (retained)', operation_id, 'M6 reject')['manifest']
        metadata = _mark_action(bridge, plan, state, 'REJECTED', activate=False)
        state.update(state='REJECTED', rejection=review or {'reason': 'explicit rejection'}, checkpoint=checkpoint,
                     candidate_fingerprint=metadata.get('fingerprint', state.get('candidate_fingerprint')))
        store.event(path.parent, state)
        return dict(public(path, plan, state), status='rejected', preserved=True)
    visual = store.review_gate(review or state.get('visual_review'), state.get('render'))
    review_checkpoint = create_checkpoint(bridge, storage, 'Before marking M6 candidate reviewed', operation_id+'-review', 'M6 review')['manifest']
    metadata = _mark_action(bridge, plan, state, 'REVIEWED', activate=False)
    state.update(state='REVIEWED', visual_review=visual, checkpoint=review_checkpoint,
                 candidate_fingerprint=metadata['fingerprint'])
    if state.get('stage') == 'BLOCKING':
        state['blocking_review_complete'] = visual['decision'] == 'ACCEPT' and state.get('technical_qa', {}).get('passed') is True
    store.event(path.parent, state)
    if not apply:
        return dict(public(path, plan, state), status='reviewed', accepted=False)
    root_loop = state.get('technical_qa', {}).get('metrics', {}).get('root_loop', {})
    if state.get('stage') in {'POSE', 'BLOCKING'} or root_loop.get('intentional_stepped_blocking') is True:
        raise ValueError('CONTINUOUS_FINAL_QA_REQUIRED: stepped blocking review permits progression, not final acceptance')
    if visual['decision'] != 'ACCEPT' or not state.get('technical_qa', {}).get('passed'):
        raise ValueError('QA_FAILED: acceptance requires passing technical QA and visual ACCEPT decision')
    require_mutable_asset(bridge, storage, 'M6 accept')
    checkpoint = create_checkpoint(bridge, storage, 'Before activating accepted M6 candidate', operation_id, 'M6 accept')['manifest']
    result = _mark_action(bridge, plan, state, 'ACCEPTED', activate=True)
    state.update(state='ACCEPTED', checkpoint=checkpoint, candidate_fingerprint=result['fingerprint'])
    store.event(path.parent, state)
    return dict(public(path, plan, state), status='accepted', **result)


def _mark_action(bridge, plan, state, lifecycle, activate=False):
    if not state.get('candidate_action'):
        return {}
    body = BASE_RUNTIME + _runtime() + r'''
obj=m2_object(params['object_name'])
source=obj.animation_data.action if obj.animation_data else None
candidate=bpy.data.actions.get(params['action_name'])
if bpy.data.filepath!=params['filepath'] or [s.get('production_asset') for s in bpy.data.scenes]!=params['stamps']:
    raise ValueError('STALE_PROPOSAL: asset changed during accept')
if (source.name if source else None)!=params['source_action'] or m2_digest(m3_source_snapshot(source))!=params['source_fingerprint']:
    raise ValueError('STALE_PROPOSAL: source changed during accept')
if candidate is None or m2_digest(m2_snapshot(candidate))!=params['candidate_fingerprint']:
    raise ValueError('STALE_PROPOSAL: candidate changed during accept')
if m6_rig_fingerprint(obj)!=params['rig_fingerprint']:
    raise ValueError('STALE_PROPOSAL: rig changed during lifecycle operation')
candidate['production_generation_lifecycle']=params['lifecycle']
candidate['production_generation_qa_result']=params['qa_result']
candidate['production_generation_qa_reference']=params['qa_reference']
if params['activate']:
    m3_assign(obj,candidate)
    bpy.context.scene.frame_set(bpy.context.scene.frame_current,subframe=bpy.context.scene.frame_subframe)
result={'active_action':candidate.name if params['activate'] else params['source_action'],
    'source_intact':m2_digest(m3_source_snapshot(source))==params['source_fingerprint'],
    'fingerprint':m2_digest(m2_snapshot(candidate))}
'''
    return bridge.run(body, dict(plan, action_name=state['candidate_action'], candidate_fingerprint=state['candidate_fingerprint'],
                                lifecycle=lifecycle, activate=activate,
                                qa_result='PASS' if state.get('technical_qa', {}).get('passed') else 'FAIL',
                                qa_reference=state.get('technical_qa', {}).get('artifact_reference', str(plan.get('generation_ref', '')))
                                  or str(state.get('generation', '')) or 'generation ledger: '+plan['generation_id']))


def refine(bridge, storage, operation_id, generation, options=None, apply=False):
    path, plan, state = store.load(storage, generation)
    store.mutable(state)
    fresh(bridge, plan, state)
    opts = dict(options or {})
    pending = state.get('pending_refinement')
    if not pending and opts.get('review') and state.get('last_refinement', {}).get('reverted'):
        if not state['last_refinement'].get('render'):
            raise ValueError('REFINEMENT_NOT_RENDERED: the rejected proposal never created a reviewable candidate')
        visual = store.review_gate(opts['review'], state['last_refinement']['render'])
        state['last_refinement']['visual_review'] = visual
        store.event(path.parent, state)
        return dict(public(path, plan, state), status='reverted_reviewed', refinement_cycles=state['refinement_cycles'])
    if pending and opts.get('review'):
        visual = store.review_gate(opts['review'], pending['render'])
        improve = pending['improved'] and visual['decision'] == 'ACCEPT'
        cycles = state['refinement_cycles']
        previous = pending['previous']
        state.update(pending['candidate'] if improve else previous)
        state.update(state='GENERATED_CANDIDATE', refinement_cycles=cycles, pending_refinement=None,
                     last_refinement=dict(pending, visual_review=visual, reverted=not improve))
        store.event(path.parent, state)
        return dict(public(path, plan, state), status='refined' if improve else 'reverted', last_refinement=_cycle_public(state['last_refinement']))
    store.refinement_gate(state)
    if pending:
        raise ValueError('REFINEMENT_REVIEW_REQUIRED: current cycle awaits temporal review')
    target = opts.get('target', 'sliding_m')
    if target not in {'sliding_m', 'pop_count', 'max_length_ratio'}:
        raise ValueError('REFINEMENT_TARGET_UNSUPPORTED: use a measured metric; artistic changes need explicit stage review')
    if not apply:
        return dict(public(path, plan, state), status='proposed', target=target, proposed_changes=opts,
                    remaining_cycles=3-state.get('refinement_cycles', 0))
    minimum = opts.get('minimum_improvement', {'sliding_m': .0001, 'pop_count': 1, 'max_length_ratio': .001}[target])
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum <= 0:
        raise ValueError('INVALID_IMPROVEMENT_THRESHOLD: declare a positive finite material improvement')
    previous = deepcopy(state)
    state.update(refinement_cycles=state.get('refinement_cycles', 0)+1, state='GENERATED_CANDIDATE')
    store.event(path.parent, state)  # A failed/uncertain attempt still consumes this generation's cycle.
    result = stage(bridge, storage, operation_id, generation, 'REFINE', opts.get('changes', {}), True)
    if result.get('status') != 'candidate_created':
        previous.update(refinement_cycles=state['refinement_cycles'], last_refinement=dict(
            findings=opts.get('findings', []), proposed_changes=opts.get('changes', {}),
            outcome='safe_rejection', result=result, reverted=True))
        store.event(path.parent, previous)
        return dict(public(path, plan, previous), status='error', errors=result.get('errors', result.get('reasons', ['Refinement could not create a candidate'])))
    _, _, candidate = store.load(storage, generation)
    after = candidate.get('technical_qa', {}).get('metrics', {}).get(target)
    before = previous.get('technical_qa', {}).get('metrics', {}).get(target)
    improved = (isinstance(before, (int, float)) and isinstance(after, (int, float))
                and before-after >= minimum and candidate.get('technical_qa', {}).get('passed') is True)
    rendered = temporal(bridge, storage, operation_id+'-visual', plan, candidate['candidate_action'], opts.get('render_options'))
    cycle = dict(findings=opts.get('findings', []), proposed_changes=opts.get('changes', {}), target=target,
                 before=before, after=after, improved=improved, candidate=deepcopy(candidate), previous=deepcopy(previous),
                 technical_qa=candidate.get('technical_qa'), render=rendered, reverted=not improved)
    cycles = state['refinement_cycles']
    if not improved:
        previous.update(refinement_cycles=cycles, last_refinement=cycle)
        store.event(path.parent, previous)
        return dict(public(path, plan, previous), status='reverted', last_refinement=_cycle_public(cycle))
    previous.update(refinement_cycles=cycles, pending_refinement=cycle)
    store.event(path.parent, previous)
    return dict(public(path, plan, previous), status='visual_review_required', pending_refinement=_cycle_public(cycle))


def _cycle_public(cycle):
    result = {k:cycle.get(k) for k in ('findings', 'target', 'before', 'after', 'improved', 'reverted', 'visual_review')}
    result['candidate_action'] = cycle.get('candidate', {}).get('candidate_action')
    result['render'] = {k:cycle.get('render', {}).get(k) for k in ('status', 'output_dir', 'temporal_complete', 'animations', 'metrics')}
    qa = cycle.get('technical_qa') or {}
    result['technical_qa'] = {k:qa.get(k) for k in ('passed', 'gates', 'metrics', 'errors')}
    return result
