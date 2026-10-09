"""The 46 M7 facial units of work (high level; no per-key or per-control tools)."""
from __future__ import annotations

from . import service
from . import blinks as blink_mod
from . import eyes as eye_mod
from . import micro as micro_mod
from . import mocap as mocap_mod
from .model import create_brief
from .runtime import INSPECT

ANALYSES = {'facial_classify_rig': 'classify', 'facial_analyze_shape_keys': 'shape_keys', 'facial_analyze_bones': 'bones',
            'facial_analyze_hybrid': 'hybrid', 'facial_compatibility_report': 'compatibility', 'facial_analyze_eyes': 'eyes',
            'facial_analyze_eyelids': 'eyelids', 'facial_analyze_jaw': 'jaw', 'facial_analyze_lips': 'lips',
            'facial_map_visemes': 'visemes', 'facial_analyze_asymmetry': 'asymmetry'}
GENERATORS = {'facial_generate_expression': 'expression', 'facial_generate_gaze': 'gaze', 'facial_generate_saccades': 'saccades',
              'facial_coordinate_gaze': 'coordinate', 'facial_generate_blink': 'blinks', 'facial_plan_blinks': 'blinks',
              'facial_generate_lid_follow': 'lid_follow', 'facial_generate_brows': 'brows', 'facial_generate_jaw': 'jaw',
              'facial_generate_lipsync': 'speech', 'facial_plan_microexpressions': 'micro', 'facial_generate_microexpressions': 'micro'}
PURE = {'facial_create_brief', 'facial_prepare_speech', 'facial_generate_coarticulation', 'facial_mocap_analyze',
        'facial_analyze_layer_conflicts', 'facial_analyze_curves'}
READ_ONLY = PURE | {'facial_inspect'}
TRANSIENT = set(ANALYSES) | {'facial_validate_expression', 'facial_validate_lipsync', 'facial_deformation_qa', 'facial_mocap_map'}
OTHER = {'facial_plan_expression', 'facial_generate', 'facial_integrate_with_body', 'facial_mocap_retarget', 'facial_mocap_cleanup',
         'facial_polish_curves', 'facial_render_closeup', 'facial_render_preview', 'facial_review', 'facial_refine',
         'facial_accept', 'facial_reject'}
NAMES = sorted(set(ANALYSES) | set(GENERATORS) | READ_ONLY | TRANSIENT | OTHER)


def _plan_without_rig(kind, brief, options):
    """Blink and microexpression schedules need no rig: they plan timing only."""
    o = dict(options or {})
    if kind == 'blinks':
        events, stats = blink_mod.plan_blinks(brief['frame_start'], brief['frame_end'], brief['fps'], o.get('seed', 11),
                                              o.get('context', 'rest'), o.get('gaze_shifts', ()), o.get('speech_intervals', ()),
                                              max(brief['emotions'], key=brief['emotions'].get), o.get('rate_per_min'), o.get('min_gap_s', 1.2))
        return {'status': 'planned', 'events': events, 'stats': stats, 'no_action_created': True}
    events, stats = micro_mod.plan(brief, brief['frame_start'], brief['frame_end'], brief['fps'], o.get('seed', 5), o.get('count'),
                                   o.get('amplitude', 0.18), [tuple(w) for w in o.get('avoid', [])])
    return {'status': 'planned', 'events': events, 'stats': stats, 'no_action_created': True}


def dispatch(name, bridge, storage, operation_id, object_name=None, intent=None, brief=None, facial=None, options=None, apply=False):
    options = dict(options or {})
    if name == 'facial_create_brief':
        return {'status': 'planned', 'brief': create_brief(intent, brief) if (intent or brief) else create_brief('neutral'),
                'no_action_created': True}
    if name == 'facial_inspect':
        if not object_name:
            raise ValueError('OBJECT_REQUIRED')
        return dict(bridge.run(INSPECT, {'object_name': object_name, 'options': service.facial_set(options)}), status='inspected')
    if name in ANALYSES:
        return service.analyze(bridge, storage, ANALYSES[name], object_name, facial, options)
    if name == 'facial_validate_expression':
        return service.validate_expression(bridge, storage, object_name, intent, brief, facial, options)
    if name == 'facial_deformation_qa':
        return service.deformation_qa(bridge, storage, object_name, facial, options)
    if name == 'facial_validate_lipsync':
        return validate_lipsync(bridge, storage, object_name, facial, options)
    if name == 'facial_prepare_speech':
        return service.prepare_speech(storage, facial, options)
    if name == 'facial_generate_coarticulation':
        return service.coarticulation(storage, facial, options)
    if name == 'facial_mocap_analyze':
        return dict(mocap_mod.analyze(service.mocap_source(options)), status='analyzed', no_action_created=True)
    if name == 'facial_mocap_map':
        return service.mocap_map(bridge, storage, object_name, facial, options)
    if name == 'facial_mocap_retarget':
        return service.mocap_retarget(bridge, storage, operation_id, object_name, facial, options, apply)
    if name == 'facial_mocap_cleanup':
        return service.mocap_cleanup(bridge, storage, operation_id, facial, options, apply)
    if name == 'facial_analyze_layer_conflicts':
        return service.layer_conflicts(storage, _need(facial))
    if name == 'facial_analyze_curves':
        return service.analyze_curves(storage, _need(facial), options)
    if name == 'facial_polish_curves':
        return service.polish_curves(bridge, storage, operation_id, _need(facial), options, apply)
    if name == 'facial_plan_expression':
        return service.plan_expression(bridge, storage, operation_id, object_name, intent, brief, facial, options, apply)
    if name == 'facial_generate':
        return service.generate(bridge, storage, operation_id, object_name, intent, brief, facial, options, apply)
    if name == 'facial_coordinate_gaze' and not facial and not object_name:
        shifts = options.get('shifts') or []
        rows = [dict(s, split=eye_mod.coordinate(s['yaw_deg'], s['pitch_deg'], options.get('coordination', 'NATURAL'),
                                                 options.get('head_policy', 'BODY_OWNS'))) for s in shifts]
        return {'status': 'planned', 'shifts': rows, 'no_action_created': True,
                'note': 'eyes lead, head follows ~100 ms later, neck follows slightly (configurable proposal ratios)'}
    if name in ('facial_plan_blinks', 'facial_plan_microexpressions') and not facial and not object_name:
        normalized = create_brief(intent, brief) if (intent or brief) else create_brief('neutral')
        return dict(_plan_without_rig(GENERATORS[name], normalized, options), brief=normalized)
    if name == 'facial_generate_jaw':
        return generate_jaw(bridge, storage, operation_id, object_name, intent, brief, facial, options, apply)
    if name in GENERATORS:
        return service.generate_kind(bridge, storage, operation_id, GENERATORS[name], object_name, intent, brief, facial, options, apply)
    if name == 'facial_integrate_with_body':
        return service.integrate(bridge, storage, operation_id, _need(facial), options)
    if name == 'facial_render_closeup':
        return service.render_closeup(bridge, storage, operation_id, _need(facial), options)
    if name == 'facial_render_preview':
        return service.render_preview(bridge, storage, operation_id, _need(facial), options)
    if name == 'facial_review':
        return service.review(bridge, storage, operation_id, _need(facial), options)
    if name == 'facial_refine':
        return service.refine(bridge, storage, operation_id, _need(facial), options, apply)
    if name == 'facial_accept':
        return service.accept(bridge, storage, operation_id, _need(facial), options, apply)
    if name == 'facial_reject':
        return service.reject(bridge, storage, operation_id, _need(facial), options, apply)
    raise ValueError('Unknown M7 operation')


def _need(facial):
    if not facial:
        raise ValueError('FACIAL_PLAN_REQUIRED: use the plan.json reference returned by facial_plan_expression or facial_generate')
    return facial


def generate_jaw(bridge, storage, operation_id, object_name, intent, brief, facial, options, apply):
    """Jaw performance validated at its peak in Blender (neck/chin penetration, extremes, lip consistency)."""
    from . import layers as L
    plan, state, path = service._plan_like(bridge, storage, object_name, facial, options)
    normalized = plan.get('brief') or service.brief_from(intent, brief)
    part = L.jaw_part(plan['profile'], normalized, options)
    findings, metrics = service.validate_jaw_part(bridge, plan, part)
    if findings:
        raise ValueError('JAW_EXTREME_COLLISION: ' + '; '.join(f['type'] + ' ' + str({k: v for k, v in f.items() if k not in ('type', 'severity')})
                                                               for f in findings)[:500])
    if not apply:
        return {'status': 'proposed', 'dry_run': True, 'part': service.part_summary(part), 'peak_geometry': metrics,
                'no_action_created': True}
    return service.generate_kind(bridge, storage, operation_id, 'jaw', object_name, intent, brief, facial, options, True)


def validate_lipsync(bridge, storage, object_name, facial, options):
    """Channel-level lip-sync checks plus measured lip gaps at bilabial closures when a candidate exists."""
    o = dict(options or {})
    if not facial:
        plan, state, path = service._plan_like(bridge, storage, object_name, None, o)
        from . import layers as L
        brief, _ = service.speech_context(storage, None, o)
        speech, head, brows = L.speech_parts(plan['profile'], brief, o)
        parts = {'speech': speech}
        combined, conflicts = L.compose(parts, brief, 'BODY_OWNS')
        check = service.speech_mod.validate_lipsync(combined, speech['prepared'], brief['fps'], approximated=speech.get('approximations'))
        return {'status': 'validated', 'mode': 'semantic (no candidate)', **check, 'no_action_created': True}
    path, plan, state = service.load(storage, facial)
    if not (state.get('parts') or {}).get('speech'):
        raise ValueError('SPEECH_LAYER_REQUIRED: facial_generate_lipsync first')
    combined, conflicts, _ = service.compose_tracks(plan, state['parts'], state.get('polish'))
    speech = state['parts']['speech']
    check = service.speech_mod.validate_lipsync(combined, speech['prepared'], plan['brief']['fps'], approximated=speech.get('approximations'))
    geometry = None
    if state.get('candidate_action'):
        service.fresh(bridge, plan, state)
        qa = state.get('technical_qa') or {}
        geometry = {'mbp_gap_max_mm': qa.get('metrics', {}).get('mbp_gap_max_mm'),
                    'mouth_gap_min_mm': qa.get('metrics', {}).get('mouth_gap_min_mm'),
                    'findings': [f for f in qa.get('findings', []) if f['type'] in ('MBP_NOT_SEALED', 'LIP_CROSSING')],
                    'thresholds': {k: qa.get('thresholds', {}).get(k) for k in ('mbp_gap_mm', 'lip_crossing_mm')}}
    passed = check['passed'] and (geometry is None or not geometry['findings'])
    return {'status': 'validated', 'passed': passed, 'channel_level': check, 'geometry': geometry, 'no_action_created': True,
            'note': 'timing comes from the supplied timeline; linguistic accuracy is not claimed'}


def register(tool, resources):
    for name in NAMES:
        def make(operation):
            def unit(object_name: str | None = None, intent: str | None = None, brief: dict | None = None,
                     facial: str | None = None, options: dict | None = None, apply: bool = False, *, _operation_id='') -> dict:
                bridge, storage = resources()
                return dispatch(operation, bridge, storage, _operation_id, object_name, intent, brief, facial, options, apply)
            unit.__name__ = operation
            unit.__doc__ = (operation + ': M7 facial unit. Evidence-based semantic mapping (never names only); plan first, '
                            'apply=false by default; facial is the immutable plan.json reference. Candidates are new Actions '
                            'evaluated through NLA tracks; the body Action is never modified. Speech needs explicit timed '
                            'phonemes/visemes (TIMING_INPUT_REQUIRED otherwise). Technical QA and artistic review stay separate.')
            return unit
        tool('production.' + name, read_only=name in READ_ONLY or name in TRANSIENT)(make(name))
