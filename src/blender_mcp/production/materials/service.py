"""Material plans and candidate transactions on the existing production safety core."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

from ..animation.generation import store
from ..checkpoint import create_checkpoint
from ..common.asset_safety import current_asset, require_mutable_asset
from ..common.ledger import is_uncertain
from ..common.operations import fingerprint
from ..common.storage import sha256, write_json
from . import candidate, graph, inspection, integrity, model, render

CATEGORY = 'materials'
STATES = {'PLANNED', 'CANDIDATE', 'REVIEWED', 'ACCEPTED', 'REJECTED'}
BUILD_TOOLS = {'material_create_pbr', 'material_build_shader', 'material_apply_profile',
               'material_configure_sss', 'material_configure_transmission',
               'material_configure_opacity', 'material_configure_emission'}
RENDER_TOOLS = {'material_render_preview', 'material_render_multilighting',
                'material_compare_before_after'}
FEATURE_PARAMETERS = {
    'material_configure_sss': {'subsurface_weight', 'subsurface_radius', 'subsurface_scale',
                               'subsurface_ior', 'subsurface_anisotropy', 'subsurface_method', 'base_color'},
    'material_configure_transmission': {'transmission_weight', 'ior', 'roughness'},
    'material_configure_opacity': {'alpha'},
    'material_configure_emission': {'emission_color', 'emission_strength'},
}
LIGHTS = {'NEUTRAL', 'SOFT', 'HARD', 'GRAZING'}


def backend_fingerprint():
    return fingerprint({p.name: sha256(p) for p in sorted(Path(__file__).parent.glob('*.py'))})


def load(storage, reference):
    return store.load(storage, reference, CATEGORY)


def event(path, state):
    store.event(path.parent, state, STATES)


def public(path, plan, state):
    result = {
        'material': str(path), 'generation_id': plan['generation_id'], 'state': state['state'],
        'object_name': plan['object_name'], 'material_name': plan['material_name'],
        'version': state.get('version', 0), 'refinement_cycles': state.get('refinement_cycles', 0),
        'candidate': state.get('candidate'), 'checkpoint': state.get('checkpoint'),
        'technical_qa': state.get('technical_qa'), 'review': state.get('review'),
        'source_preserved': state.get('source_preserved', True),
        'original_assignments_replaced': False, 'uncertain_operation': bool(state.get('uncertain_operation')),
        'mutation_blocked': bool(state.get('mutation_blocked')),
        'last_error': state.get('last_error'), 'pending_operation': state.get('pending_operation'),
    }
    if state.get('render'):
        r = state['render']
        result['render'] = {k: r.get(k) for k in ('status', 'output_dir', 'files', 'lighting_set',
                                                 'render_backend', 'metrics', 'state_restored')}
    return result


def image_options(definition):
    return {'image_names': sorted({v['image'] for v in definition.get('channels', {}).values()})}


def normalized(definition=None, profile=None):
    return model.normalize_definition(definition or {}, profile=profile)


def source_status(bridge, plan, options=None):
    return integrity.status(bridge, plan['object_name'], plan['material_name'],
                            options if options is not None else plan['source_options'])


def candidate_status(bridge, state):
    c = state.get('candidate')
    if not c:
        raise ValueError('MATERIAL_CANDIDATE_REQUIRED: create an owned candidate first')
    return integrity.status(bridge, c['candidate_object'], c['candidate_material'],
                            image_options(state['definition']))


def fresh(bridge, storage, plan, state, *, check_candidate=True, allow_failed=False):
    if state.get('uncertain_operation'):
        raise ValueError('OPERATION_OUTCOME_UNCERTAIN: inspect the ledger, Blender and recovery checkpoint; no automatic retry')
    if state.get('mutation_blocked') and not allow_failed:
        raise ValueError('MATERIAL_MUTATION_FAILED: retain evidence and inspect the candidate before further work')
    if plan['backend_fingerprint'] != backend_fingerprint():
        raise ValueError('MATERIAL_BACKEND_CHANGED: reanalyze with the current implementation')
    now = source_status(bridge, plan)
    if now['fingerprint'] != plan['source_status']['fingerprint']:
        raise ValueError('MATERIAL_STALE_SOURCE: material, image dependency, slots, UV, geometry, animation or physics changed')
    asset = current_asset(bridge, storage)
    if any(asset.get(k) != plan['asset'].get(k) for k in ('asset_id', 'role', 'path')):
        raise ValueError('MATERIAL_ASSET_CHANGED: the file or registered asset role changed')
    if state.get('definition_dependencies'):
        deps = state['definition_dependencies']
        actual = source_status(bridge, plan, image_options(state['definition']))
        if actual['fingerprint'] != deps['fingerprint']:
            raise ValueError('MATERIAL_STALE_TEXTURE_DEPENDENCY: candidate definition inputs changed')
    if check_candidate and state.get('candidate'):
        actual = candidate_status(bridge, state)
        if actual['fingerprint'] != state['candidate_status']['fingerprint']:
            raise ValueError('MATERIAL_STALE_CANDIDATE: candidate edited outside this material plan')
    return now


def plan_material(bridge, storage, operation_id, object_name, material_name, definition, profile):
    if not isinstance(object_name, str) or not object_name:
        raise ValueError('MATERIAL_OBJECT_REQUIRED: use an exact existing mesh object name')
    if not isinstance(material_name, str) or not material_name:
        raise ValueError('MATERIAL_SOURCE_REQUIRED: inspect and select an exact source material')
    value = normalized(definition, profile)
    params = image_options(value)
    snapshot = integrity.status(bridge, object_name, material_name, params)
    asset = current_asset(bridge, storage)
    raw = inspection.inspect(bridge, object_name, material_name)
    data = {
        'schema': 'production.material_plan.v1', 'object_name': object_name,
        'material_name': material_name, 'definition': value, 'source_options': params,
        'source_status': snapshot, 'asset': asset, 'backend_fingerprint': backend_fingerprint(),
        'classification': inspection.classify(raw),
        'preservation': ['source_material', 'source_slots', 'UV', 'geometry', 'shape_keys',
                         'weights', 'body_actions', 'face_actions', 'rig', 'physics', 'image_files'],
    }
    reference, saved = store.save(storage, operation_id, data, CATEGORY, STATES)
    path = Path(reference)
    state = {'state': 'PLANNED', 'definition': value, 'refinement_cycles': 0, 'version': 0,
             'last_version': 0, 'source_preserved': True}
    event(path, state)
    return path, saved, state


def checkpoint(bridge, storage, path, operation_id, label):
    require_mutable_asset(bridge, storage, label)
    return create_checkpoint(bridge, storage, label, operation_id, str(path))['manifest']


def construction_params(plan, state, name, version, expected_source):
    return {
        'object_name': plan['object_name'], 'material_name': plan['material_name'],
        'options': image_options(state['definition']), 'expected_status': expected_source,
        'candidate_name': name, 'generation_id': plan['generation_id'], 'version': version,
        'expected_asset': plan['asset'],
    }


def create_version(bridge, storage, path, plan, state, operation_id, definition, *, build):
    store.mutable(state)
    require_mutable_asset(bridge, storage, 'M9 material candidate construction')
    fresh(bridge, storage, plan, state)
    dependencies = source_status(bridge, plan, image_options(definition))
    version = state.get('last_version', 0) + 1
    name = 'M9_' + plan['generation_id'][:10] + '_v' + str(version)
    safety = checkpoint(bridge, storage, path, operation_id, 'Before isolated material candidate v' + str(version))
    state.update(last_version=version, checkpoint=safety, pending_operation=operation_id)
    event(path, state)
    try:
        draft = dict(state, definition=definition)
        params = construction_params(plan, draft, name, version, dependencies)
        created = candidate.create(bridge, params)
        c = {k: created[k] for k in ('candidate_object', 'candidate_material')}
        c.update(generation_id=plan['generation_id'], version=version)
        if build:
            status = integrity.status(bridge, c['candidate_object'], c['candidate_material'], image_options(definition))
            graph.build(bridge, dict(params, **c, definition=definition, expected_candidate=status))
        new_state = dict(state, candidate=c, definition=definition)
        actual = candidate_status(bridge, new_state)
        after_source = source_status(bridge, plan)
        if after_source['fingerprint'] != plan['source_status']['fingerprint']:
            raise RuntimeError('MATERIAL_SOURCE_PRESERVATION_FAILED: original state changed during construction')
        if source_status(bridge, plan, image_options(definition))['fingerprint'] != dependencies['fingerprint']:
            raise RuntimeError('MATERIAL_DEPENDENCY_CHANGED_DURING_BUILD: external input changed')
        previous = list(state.get('history', []))
        if state.get('candidate'):
            previous.append({'candidate': state['candidate'], 'definition': state['definition'],
                             'review': state.get('review'), 'render': state.get('render')})
        state.update(state='CANDIDATE', candidate=c, version=version, definition=definition,
                     definition_dependencies=dependencies, candidate_status=actual,
                     built=build, technical_qa=None, render=None, review=None, history=previous,
                     pending_operation=None, source_preserved=True)
        event(path, state)
    except Exception as exc:
        state.update(mutation_blocked=True, uncertain_operation=is_uncertain(exc),
                     last_error=repr(exc), pending_operation=operation_id)
        event(path, state)
        raise
    return state


def ensure_candidate(bridge, storage, path, plan, state, operation_id):
    require_mutable_asset(bridge, storage, 'M9 material candidate')
    store.mutable(state)
    fresh(bridge, storage, plan, state)
    if not state.get('candidate'):
        create_version(bridge, storage, path, plan, state, operation_id, state['definition'], build=False)
    return state


def modified_definition(name, state, definition, profile, options):
    if name == 'material_apply_profile':
        if not profile:
            raise ValueError('MATERIAL_PROFILE_REQUIRED')
        return normalized(definition, profile)
    if name in FEATURE_PARAMETERS:
        values = options.get('parameters', {})
        if set(options) - {'parameters'} or not isinstance(values, dict) or not values:
            raise ValueError('MATERIAL_FEATURE_PARAMETERS_REQUIRED: supply explicit parameters only')
        if set(values) - FEATURE_PARAMETERS[name]:
            raise ValueError('MATERIAL_FEATURE_PARAMETER_UNSUPPORTED: ' + ', '.join(sorted(set(values) - FEATURE_PARAMETERS[name])))
        if definition or profile:
            raise ValueError('MATERIAL_FEATURE_DEFINITION_CONFLICT: configure the explicit feature parameters')
        value = deepcopy(state['definition'])
        value['parameters'].update(values)
        return normalized(value)
    if options:
        raise ValueError('MATERIAL_BUILD_OPTIONS_UNSUPPORTED: use the structured definition')
    return normalized(definition, profile) if definition is not None or profile else state['definition']


def build_material(name, bridge, storage, path, plan, state, operation_id, definition, profile, options):
    store.mutable(state)
    require_mutable_asset(bridge, storage, 'M9 material shader construction')
    fresh(bridge, storage, plan, state)
    desired = modified_definition(name, state, definition, profile, options)
    if state.get('built') and desired == state['definition']:
        return state
    if state.get('render') or state.get('review'):
        raise ValueError('MATERIAL_REFINEMENT_REQUIRED: use material_refine after visual QA; at most three cycles')
    return create_version(bridge, storage, path, plan, state, operation_id, desired, build=True)


def technical_qa(bridge, state):
    c = state.get('candidate')
    if not c:
        raise ValueError('MATERIAL_CANDIDATE_REQUIRED')
    raw = inspection.inspect(bridge, c['candidate_object'], c['candidate_material'])
    report = inspection.report('material_quality_report', raw, state['definition'])
    if not isinstance(report.get('passed'), bool):
        raise ValueError('MATERIAL_QA_INCOMPLETE: validation did not return an explicit passed boolean')
    return report


def verify_render(rendered, require_multilight=True):
    if not rendered or rendered.get('status') not in {'ok', 'success'} or rendered.get('state_restored') is not True:
        raise ValueError('MATERIAL_VISUAL_RENDER_REQUIRED: successful restored controlled render required')
    lights = set(rendered.get('lighting_set', []))
    if require_multilight and lights != LIGHTS:
        raise ValueError('MATERIAL_MULTILIGHT_REQUIRED: neutral, soft, hard and grazing views required')
    root = Path(rendered['output_dir']).resolve(strict=True)
    files = rendered.get('files')
    if not isinstance(files, list) or len(files) < 2 * len(lights) or not lights:
        raise ValueError('MATERIAL_RENDER_PAIRS_REQUIRED: at least four source/candidate lighting pairs')
    pairs = set()
    for file in files:
        p = Path(file['path']).resolve(strict=True)
        if not p.is_relative_to(root) or p.suffix.lower() != '.png' or not file.get('sha256'):
            raise ValueError('MATERIAL_RENDER_ARTIFACT_INVALID: confined hashed PNGs required')
        if sha256(p) != file['sha256']:
            raise ValueError('MATERIAL_RENDER_STALE: an image changed after rendering')
        pairs.add((file.get('lighting'), file.get('variant')))
    if not {(light, side) for light in lights for side in ('SOURCE', 'CANDIDATE')} <= pairs:
        raise ValueError('MATERIAL_RENDER_PAIRS_REQUIRED: the source and candidate must share all lighting conditions')
    return True


def review_gate(review, rendered):
    verify_render(rendered)
    required = {'reviewer', 'observations', 'findings', 'decision', 'artifact_reference', 'lights_reviewed'}
    if not isinstance(review, dict) or not required <= set(review):
        raise ValueError('MATERIAL_VISUAL_REVIEW_REQUIRED: record reviewer, observations, findings, decision, artifact_reference and lights_reviewed')
    if review['decision'] not in {'ACCEPT', 'REJECT', 'REFINE'}:
        raise ValueError('MATERIAL_REVIEW_DECISION_INVALID')
    for key in ('reviewer', 'observations'):
        if not isinstance(review[key], str) or not review[key].strip():
            raise ValueError('MATERIAL_REVIEW_OBSERVATIONS_REQUIRED: real nonempty observations required')
    if not isinstance(review['findings'], list) or any(not isinstance(v, str) for v in review['findings']):
        raise ValueError('MATERIAL_REVIEW_FINDINGS_INVALID')
    if review['artifact_reference'] != rendered['output_dir'] or set(review['lights_reviewed']) != LIGHTS:
        raise ValueError('MATERIAL_VISUAL_REVIEW_STALE: review must cover the current four lighting pairs')
    return deepcopy(review)


def render_material(bridge, storage, path, plan, state, operation_id, options):
    store.mutable(state)
    require_mutable_asset(bridge, storage, 'M9 controlled material preview')
    fresh(bridge, storage, plan, state)
    settings = dict(options)
    expected_engine = {'EEVEE': 'BLENDER_EEVEE', 'CYCLES': 'CYCLES'}[state['definition']['renderer']]
    if settings.get('engine', expected_engine) != expected_engine:
        raise ValueError('MATERIAL_RENDERER_CONFLICT: el motor solicitado difiere del contrato validado del material')
    settings.setdefault('engine', expected_engine)
    settings.setdefault('lighting_set', ['NEUTRAL', 'SOFT', 'HARD', 'GRAZING'])
    settings = render.normalize_options(settings)
    qa = technical_qa(bridge, state)
    state['technical_qa'] = qa
    if not qa['passed'] or qa.get('errors'):
        event(path, state)
        raise ValueError('MATERIAL_QA_FAILED: resolve technical defects before visual qualification')
    safety = checkpoint(bridge, storage, path, operation_id, 'Before controlled paired material render')
    state.update(checkpoint=safety, pending_operation=operation_id)
    event(path, state)
    try:
        result = render.render(bridge, storage, operation_id, plan['object_name'], plan['material_name'],
                               state['candidate']['candidate_material'], settings)
        verify_render(result, require_multilight=False)
        fresh(bridge, storage, plan, state)
        state.update(render=result, review=None, state='CANDIDATE', pending_operation=None,
                     render_candidate_fingerprint=state['candidate_status']['fingerprint'])
        event(path, state)
    except Exception as exc:
        state.update(uncertain_operation=is_uncertain(exc), last_error=repr(exc),
                     mutation_blocked=is_uncertain(exc), pending_operation=operation_id)
        event(path, state)
        raise
    return state


def refine(bridge, storage, path, plan, state, operation_id, definition, profile, options):
    require_mutable_asset(bridge, storage, 'M9 bounded material refinement')
    fresh(bridge, storage, plan, state)
    store.refinement_gate(state)
    if not state.get('review'):
        raise ValueError('MATERIAL_REVIEW_REQUIRED: inspect the current preview before refining')
    review_gate(state['review'], state.get('render'))
    if set(options) != {'reason'} or not isinstance(options['reason'], str) or not options['reason'].strip():
        raise ValueError('MATERIAL_REFINEMENT_REASON_REQUIRED: record a concrete visual finding')
    if definition is None and not profile:
        raise ValueError('MATERIAL_REFINEMENT_DEFINITION_REQUIRED')
    desired = normalized(definition, profile)
    if desired == state['definition']:
        raise ValueError('MATERIAL_REFINEMENT_NO_CHANGE')
    # Consume before checkpoint/copy, including a failed attempt. Nothing can roll this back.
    state.update(refinement_cycles=state.get('refinement_cycles', 0) + 1,
                 refinement_reason=options['reason'])
    event(path, state)
    return create_version(bridge, storage, path, plan, state, operation_id, desired, build=True)


def lifecycle(name, bridge, storage, path, plan, state, operation_id, options):
    require_mutable_asset(bridge, storage, 'M9 ' + name)
    store.mutable(state)
    fresh(bridge, storage, plan, state)
    if name == 'material_review':
        review = review_gate(options, state.get('render'))
        if state.get('render_candidate_fingerprint') != state['candidate_status']['fingerprint']:
            raise ValueError('MATERIAL_REVIEW_CANDIDATE_CHANGED')
        state.update(state='REVIEWED', review=review)
        event(path, state)
    elif name == 'material_accept':
        if options:
            raise ValueError('MATERIAL_ACCEPT_OPTIONS_UNSUPPORTED')
        review = review_gate(state.get('review'), state.get('render'))
        if review['decision'] != 'ACCEPT' or state.get('state') != 'REVIEWED':
            raise ValueError('MATERIAL_VISUAL_ACCEPT_REQUIRED')
        qa = technical_qa(bridge, state)
        if not qa['passed'] or qa.get('errors'):
            raise ValueError('MATERIAL_QA_FAILED: acceptance requires fresh valid material QA')
        safety = checkpoint(bridge, storage, path, operation_id, 'Accept verified isolated material candidate')
        fresh(bridge, storage, plan, state)
        state.update(state='ACCEPTED', checkpoint=safety, technical_qa=qa, source_preserved=True)
        event(path, state)
    elif name == 'material_reject':
        if set(options) - {'reason'} or not isinstance(options.get('reason'), str) or not options['reason'].strip():
            raise ValueError('MATERIAL_REJECTION_REASON_REQUIRED')
        safety = checkpoint(bridge, storage, path, operation_id, 'Reject material candidate and preserve evidence')
        fresh(bridge, storage, plan, state)
        # Source slots were never reassigned. Rejection cannot undo edits made by somebody else.
        state.update(state='REJECTED', checkpoint=safety, rejection_reason=options['reason'],
                     candidate_preserved=True, pre_operation_assignments_preserved=True)
        event(path, state)
    elif name == 'material_apply_candidate':
        if options:
            raise ValueError('MATERIAL_ASSIGNMENT_OPTIONS_UNSUPPORTED: source slots cannot be replaced')
        qa = technical_qa(bridge, state)
        if not qa['passed'] or qa.get('errors'):
            raise ValueError('MATERIAL_QA_FAILED')
        safety = checkpoint(bridge, storage, path, operation_id, 'Verify material assignment on owned candidate only')
        fresh(bridge, storage, plan, state)
        state.update(technical_qa=qa, checkpoint=safety, applied_to_owned_candidate=True)
        event(path, state)
    return state


def dispatch(name, bridge, storage, operation_id, object_name=None, material_name=None,
             material=None, definition=None, profile=None, options=None, apply=False):
    if not isinstance(apply, bool):
        raise ValueError('MATERIAL_APPLY_BOOLEAN_REQUIRED')
    if options is not None and not isinstance(options, dict):
        raise ValueError('MATERIAL_OPTIONS_OBJECT_REQUIRED')
    opts = deepcopy(options or {})
    path = plan = state = None
    if material:
        path, plan, state = load(storage, material)
        if object_name not in (None, plan['object_name']) or material_name not in (None, plan['material_name']):
            raise ValueError('MATERIAL_PLAN_TARGET_MISMATCH')
        object_name, material_name = plan['object_name'], plan['material_name']
    try:
        if name == 'material_create_profile':
            return model.create_profile(profile, overrides=definition)
        if name == 'material_validate_profile':
            return model.validate_profile(profile, definition)
        if name == 'material_status':
            if not path:
                raise ValueError('MATERIAL_PLAN_REFERENCE_REQUIRED')
            result = public(path, plan, state)
            try:
                fresh(bridge, storage, plan, state)
                result['freshness'] = 'current'
            except (ValueError, PermissionError) as exc:
                result.update(status='error', freshness='blocked', errors=[{
                    'code': str(exc).split(':', 1)[0], 'message': str(exc)}])
            return result
        from .tools import READ_ONLY
        if name in READ_ONLY:
            desired = normalized(definition, profile) if definition is not None or profile else None
            if path:
                fresh(bridge, storage, plan, state)
                desired = state['definition'] if desired is None else desired
                if state.get('candidate'):
                    object_name = state['candidate']['candidate_object']
                    material_name = state['candidate']['candidate_material']
            raw = inspection.inspect(bridge, object_name, material_name, opts)
            return inspection.report(name, raw, desired, opts)
        if name == 'material_create_handoff':
            from .handoff import create_handoff
            if path:
                fresh(bridge, storage, plan, state)
            return create_handoff(bridge, storage, operation_id, object_name, material_name,
                                  state['definition'] if state else normalized(definition, profile), opts,
                                  apply=apply, candidate=state.get('candidate') if state else None)
        if not path:
            if name not in BUILD_TOOLS | {'material_create_candidate'}:
                raise ValueError('MATERIAL_PLAN_REFERENCE_REQUIRED')
            if opts and name not in FEATURE_PARAMETERS:
                raise ValueError('MATERIAL_PLAN_OPTIONS_UNSUPPORTED')
            if name in FEATURE_PARAMETERS:
                raise ValueError('MATERIAL_PLAN_REFERENCE_REQUIRED: configure an existing candidate plan')
            if apply:
                require_mutable_asset(bridge, storage, 'M9 material construction')
            path, plan, state = plan_material(bridge, storage, operation_id, object_name,
                                              material_name, definition, profile)
        if not apply:
            fresh(bridge, storage, plan, state)
            proposal = public(path, plan, state)
            proposal.update(apply=False, proposed_operation=name, definition=state['definition'])
            if name in BUILD_TOOLS:
                proposal['proposed_definition'] = modified_definition(name, state, definition, profile, opts)
                proposal['changes'] = {'before': state['definition'], 'after': proposal['proposed_definition']}
            return proposal
        if name == 'material_create_candidate':
            if opts or definition is not None or profile:
                # A newly created plan already froze these values; a reference cannot override them.
                if material:
                    raise ValueError('MATERIAL_FROZEN_PLAN: use the stored plan definition')
            ensure_candidate(bridge, storage, path, plan, state, operation_id)
        elif name in BUILD_TOOLS:
            build_material(name, bridge, storage, path, plan, state, operation_id, definition, profile, opts)
        elif name in RENDER_TOOLS:
            if definition is not None or profile:
                raise ValueError('MATERIAL_RENDER_DEFINITION_FROZEN')
            render_material(bridge, storage, path, plan, state, operation_id, opts)
        elif name == 'material_refine':
            refine(bridge, storage, path, plan, state, operation_id, definition, profile, opts)
        elif name in {'material_review', 'material_accept', 'material_reject', 'material_apply_candidate'}:
            if definition is not None or profile:
                raise ValueError('MATERIAL_LIFECYCLE_DEFINITION_FROZEN')
            lifecycle(name, bridge, storage, path, plan, state, operation_id, opts)
        else:
            raise ValueError('MATERIAL_TOOL_UNSUPPORTED: ' + name)
        return public(path, plan, state)
    except Exception as exc:
        if path and is_uncertain(exc):
            state.update(uncertain_operation=True, last_error=repr(exc), pending_operation=operation_id)
            event(path, state)
        raise
