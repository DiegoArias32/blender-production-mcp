"""Handlers of the planning tools (READ_ONLY in tools.py): brief, beats, shots, actor paths, interactions and the
render plan.

None of them changes Blender. The only payloads are reads: scene settings and fingerprint (lifecycle.source_info),
the open file's asset role (current_asset), object presence/bounds/positions and Action presence (PRESENCE) and the
render device preferences (BACKEND). No frame_set, no Action/slot assignment, no pose write, no datablock creation:
tests/unit/test_m31_hints.py inspects the linked payloads and tests/blender/m10_planning_probe.py compares the scene
before and after every call. Nothing is written to the plan ledger either: the results go back to the caller, who
passes brief/beats/shots to cinematic_create_candidate (where normalize_plan validates them again).
"""
from __future__ import annotations

from ..common.asset_safety import current_asset
from . import lifecycle, model, planning, registry, store
from .runtime import COMMON
from .service import handler

PRESENCE = COMMON + r'''
def cm_plan_presence(params):
    # Read-only: matrix_world and dimensions are read as stored, never re-evaluated with frame_set, so the planning
    # tools keep their readOnlyHint. The current frame is reported so the caller knows what the positions mean.
    scene = cm_scene(params['source_scene']) if params.get('source_scene') else bpy.context.scene
    objects = {}
    for name in params['objects']:
        obj = bpy.data.objects.get(name)
        if obj is None:
            objects[name] = None
            continue
        matrix = obj.matrix_world
        euler = matrix.to_euler('XYZ')
        objects[name] = {'type': obj.type, 'in_scene': scene.objects.get(name) is not None,
                         'location': [matrix[0][3], matrix[1][3], matrix[2][3]], 'yaw_deg': math.degrees(euler.z),
                         'dimensions': [obj.dimensions[0], obj.dimensions[1], obj.dimensions[2]]}
    actions = {}
    for name in params['actions']:
        action = bpy.data.actions.get(name)
        actions[name] = None if action is None else {'frame_range': [action.frame_range[0], action.frame_range[1]]}
    return {'scene': scene.name, 'frame_current': scene.frame_current, 'objects': objects, 'actions': actions}

result = cm_plan_presence(params)
'''

BACKEND = COMMON + r'''
def cm_plan_backend(params):
    # Preferences are only read (same source as qa/capture.py qa_render_backend); the device list is the one
    # Blender already holds, nothing is refreshed or enabled.
    system = bpy.context.preferences.system
    addon = bpy.context.preferences.addons.get('cycles')
    compute = addon.preferences.compute_device_type if addon else 'NONE'
    devices = [[d.name, d.type, bool(d.use)] for d in addon.preferences.devices] if addon else []
    return {'gpu_backend': system.gpu_backend, 'cycles_addon': addon is not None, 'compute_device_type': compute,
            'devices': devices, 'preferences_modified': False}

result = cm_plan_backend(params)
'''


def _context(ctx, source_scene):
    info = lifecycle.source_info(ctx, source_scene)
    asset = current_asset(ctx.bridge, ctx.storage)
    return info, registry.asset_key(asset, info['scene'])


def _presence(ctx, scene, request):
    return ctx.bridge.run(PRESENCE, {'source_scene': scene, 'objects': list(request['objects']),
                                     'actions': list(request['actions'])})


def _object_refs(entries, keys):
    refs = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        for key in keys:
            value = entry.get(key)
            if isinstance(value, str):
                refs.append(value)
            elif isinstance(value, dict) and value.get('type') == 'OBJECT':
                refs.append(value.get('object'))
    return refs


def _brief_refs(brief):
    rows = list((brief or {}).get('events') or []) + list((brief or {}).get('interaction_requirements') or [])
    return [r['target']['object'] for r in rows if (r.get('target') or {}).get('type') == 'OBJECT']


def _spatial(ctx, req, opts):
    """Shared setup of the spatial planners: brief (optional), scene, registry cast, timeline and beats."""
    brief = planning.require_brief(req.brief) if req.brief is not None else None
    scene = opts.get('source_scene') or ((brief or {}).get('source') or {}).get('scene')
    info, key = _context(ctx, scene)
    if brief is not None and brief.get('actor_registry_key') not in (None, key):
        model.fail('CINEMATIC_BRIEF_STALE_ASSET', 'the brief was made for actor registry ' + str(brief['actor_registry_key'])
                   + ', the open file uses ' + key + '; create the brief again')
    cast = planning.make_cast(brief, registry.records(ctx.storage, key))
    settings = info['settings']
    if brief is not None:
        # Checked against the live scene: a scene whose fps changed since the brief is refused, never retimed.
        timeline = planning.timeline_for(brief, settings)
    else:
        rate = model.fps_value(settings['fps'], settings['fps_base'])
        frames = settings['frame_end'] - settings['frame_start'] + 1
        timeline = {'fps': settings['fps'], 'fps_base': settings['fps_base'], 'frame_start': settings['frame_start'],
                    'frame_end': settings['frame_end'], 'frames': frames,
                    'duration_s': min(frames / rate, model.MAX_DURATION_S)}
    beats = opts.get('beats')
    if beats is not None:
        if not isinstance(beats, dict) or beats.get('schema') != planning.BEATS_SCHEMA:
            model.fail('CINEMATIC_BEATS_INVALID', 'options.beats must be the result of cinematic_plan_beats')
        planning.validate_beats(beats, timeline, planning.actor_ids(brief) if brief else list(cast['actors']))
    elif brief is not None:
        beats = planning.plan_beats(brief)
    return brief, info, cast, timeline, beats


@handler('cinematic_create_brief')
def create_brief(ctx, req):
    """User intention (structured by the client agent) -> production.cinematic_brief.v1. The server uses no
    language model: it validates, resolves actors against the registry and reports what is missing."""
    opts = model.closed(req.options, {'source_scene'})
    intent = planning.normalize_intent(req.brief)
    info, key = _context(ctx, opts.get('source_scene'))
    records = registry.records(ctx.storage, key)
    presence = _presence(ctx, info['scene'], planning.presence_request(intent, records))
    brief = planning.build_brief(intent, info, records, presence, key)
    blocking = [m['code'] for m in brief['missing_prerequisites'] if m.get('blocking', True)]
    return {'status': 'success' if brief['ready'] else 'needs_input', 'ready': brief['ready'], 'brief': brief,
            'summary': {'actors': len(brief['actors']), 'events': len(brief['events']),
                        'missing_prerequisites': blocking, 'risks': [r['code'] for r in brief['risks']],
                        'open_questions': len(brief['open_questions'])},
            'blender_changed': False,
            'next': ('cinematic_plan_beats(brief=brief)' if brief['ready'] else
                     'resolve missing_prerequisites/open_questions, then call cinematic_create_brief again')}


@handler('cinematic_plan_beats')
def plan_beats(ctx, req):
    result = planning.plan_beats(req.brief, req.options)
    result['blender_changed'] = False
    return result


@handler('cinematic_plan_shots')
def plan_shots(ctx, req):
    result = planning.plan_shots(req.brief, req.options)
    result['blender_changed'] = False
    return result


@handler('cinematic_plan_actor_paths')
def plan_actor_paths(ctx, req):
    opts = model.closed(req.options, planning.PATH_OPTION_KEYS)
    if req.brief is None and opts.get('paths') is None:
        model.fail('CINEMATIC_PATHS_REQUIRED', 'pass options.paths or a brief with APPROACH events')
    brief, info, cast, timeline, beats = _spatial(ctx, req, opts)
    refs = _object_refs(opts.get('paths'), ('target', 'orientation_target')) + _brief_refs(brief)
    presence = _presence(ctx, info['scene'], {'objects': planning.cast_objects(cast, refs), 'actions': []})
    result = planning.plan_paths(opts, cast, presence, timeline, beats, brief)
    result['blender_changed'] = False
    return result


@handler('cinematic_plan_interaction')
def plan_interaction(ctx, req):
    opts = model.closed(req.options, planning.INTERACTION_OPTION_KEYS)
    if req.brief is None and opts.get('interactions') is None:
        model.fail('CINEMATIC_INTERACTIONS_REQUIRED', 'pass options.interactions or a brief')
    brief, info, cast, timeline, beats = _spatial(ctx, req, opts)
    refs = _object_refs(opts.get('interactions'), ('spatial_target',)) + _brief_refs(brief)
    presence = _presence(ctx, info['scene'], {'objects': planning.cast_objects(cast, refs), 'actions': []})
    result = planning.plan_interactions(opts, cast, presence, timeline, beats, brief)
    result['blender_changed'] = False
    return result


@handler('cinematic_render_plan')
def render_plan(ctx, req):
    """Plan only: nothing renders and nothing is written. Cycles is refused without OptiX (read-only check)."""
    opts = model.closed(req.options, planning.RENDER_OPTION_KEYS)
    if req.cinematic:
        if opts.get('shot_plan') is not None:
            model.fail('CINEMATIC_OPTION_CONFLICT', 'a saved cinematic already carries its shots')
        path, plan, state = store.load(ctx.storage, req.cinematic)
        timeline = state.get('timeline') or plan['timeline']
        shots, settings, reference = plan.get('shots') or [], plan['source']['settings'], str(path)
    elif req.brief is not None:
        brief = planning.require_brief(req.brief)
        timeline = planning.brief_timeline(brief)
        shots = []
        if opts.get('shot_plan') is not None:
            shot_plan = opts['shot_plan']
            if not isinstance(shot_plan, dict) or shot_plan.get('schema') != planning.SHOTS_SCHEMA:
                model.fail('CINEMATIC_SHOTS_INVALID', 'options.shot_plan must be the result of cinematic_plan_shots')
            shots = shot_plan.get('shots') or []
            planning.validate_shots(shots, timeline, planning.actor_ids(brief))
        settings, reference = brief.get('source') or {}, None
    else:
        model.fail('CINEMATIC_RENDER_PLAN_SOURCE_REQUIRED', 'pass cinematic= (plan.json) or brief=')
    backend = ctx.bridge.run(BACKEND, {})
    result = planning.render_plan(timeline, shots, opts, settings, backend, reference)
    result['blender_changed'] = False
    return result
