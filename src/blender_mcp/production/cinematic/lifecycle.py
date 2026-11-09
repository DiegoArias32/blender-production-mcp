"""Candidate creation and rejection: the frame every other M10 step mutates inside.

create_candidate: immutable plan (PLANNED) -> own scene `M10C_<g8>_v1` with `_SRC` (source objects linked, not
copied) and `_CIN` (owned datablocks) collections -> CINEMATIC_CANDIDATE. The actors' channels and shape keys are
captured first so rejection can restore them after reversing every touched row (removing NLA leaves the last
evaluated pose — measured in M7).
"""
from __future__ import annotations

from ..checkpoint import create_checkpoint
from ..common.asset_safety import current_asset, require_mutable_asset
from ..common.ledger import is_uncertain
from . import model, planning, registry, store
from .runtime import COMMON

SOURCE_INFO = COMMON + r'''
def cm_source_info(params):
    scene = cm_scene(params['source_scene']) if params.get('source_scene') else bpy.context.scene
    missing = [n for n in params.get('exclude_objects') or [] if scene.objects.get(n) is None]
    if missing:
        cm_fail('CINEMATIC_EXCLUDE_UNKNOWN', 'not in the source scene: ' + ', '.join(missing))
    return {'scene': scene.name, 'settings': cm_scene_settings(scene), 'fingerprint': cm_source_fingerprint(scene)['fingerprint'],
            'objects': len(scene.objects), 'filepath': bpy.data.filepath, 'cameras_lights': cm_cameras_lights(scene)}

result = cm_source_info(params)
'''

CREATE = COMMON + r'''
def cm_contains(collection, excluded):
    for obj in collection.all_objects:
        if obj.name in excluded:
            return True
    return False

def cm_link_tree(params, source_collection, target, excluded, counter, individually, wholesale):
    for child in sorted(source_collection.children, key=cm_sort_name):
        if cm_contains(child, excluded):
            counter['mirrors'] += 1
            name = params['mirror_prefix'] + str(counter['mirrors'])
            cm_free_name(bpy.data.collections, name)
            mirror = bpy.data.collections.new(name)
            cm_mark(mirror, params, 'source_mirror')
            mirror['production_m10_mirror_of'] = child.name
            target.children.link(mirror)
            cm_link_tree(params, child, mirror, excluded, counter, individually, wholesale)
        else:
            target.children.link(child)
            wholesale.append(child.name)
    for obj in sorted(source_collection.objects, key=cm_sort_name):
        if obj.name in excluded:
            continue
        target.objects.link(obj)
        individually.append(obj.name)

def cm_create(params):
    source = cm_scene(params['source_scene'])
    before = cm_source_fingerprint(source)
    if before['fingerprint'] != params['source_fingerprint']:
        cm_fail('CINEMATIC_STALE_SOURCE', 'source scene changed since the plan')
    cm_free_name(bpy.data.scenes, params['scene'])
    cm_free_name(bpy.data.collections, params['collections']['src'])
    cm_free_name(bpy.data.collections, params['collections']['cin'])
    actors = [cm_object(n) for n in params['actor_objects']]
    baseline = cm_capture(source, actors)
    scene = bpy.data.scenes.new(params['scene'])
    if scene.name != params['scene']:
        cm_fail('CINEMATIC_NAME_COLLISION', params['scene'])
    cm_mark(scene, params, 'candidate_scene')
    src = bpy.data.collections.new(params['collections']['src'])
    cin = bpy.data.collections.new(params['collections']['cin'])
    cm_mark(src, params, 'source_links')
    cm_mark(cin, params, 'owned_datablocks')
    scene.collection.children.link(src)
    scene.collection.children.link(cin)
    excluded = set(params.get('exclude_objects') or [])
    # Source collections are linked WHOLE into _SRC: that leaves every object's users_collection unchanged, which M8
    # signs in its candidate status (measured: linking objects one by one made accepted M8 generations stale).
    # A collection that contains an excluded object is mirrored by an owned collection instead, so the exclusion is
    # exact; only objects directly in such collections (and in the scene master collection) are linked one by one.
    counter = {'mirrors': 0}
    individually = []
    wholesale = []
    cm_link_tree(params, source.collection, src, excluded, counter, individually, wholesale)
    linked = len(set(o.name for o in src.all_objects))
    render = scene.render
    origin = source.render
    render.fps = origin.fps
    render.fps_base = origin.fps_base
    render.resolution_x = origin.resolution_x
    render.resolution_y = origin.resolution_y
    render.resolution_percentage = origin.resolution_percentage
    render.pixel_aspect_x = origin.pixel_aspect_x
    render.pixel_aspect_y = origin.pixel_aspect_y
    render.engine = origin.engine
    scene.unit_settings.system = source.unit_settings.system
    scene.unit_settings.scale_length = source.unit_settings.scale_length
    scene.unit_settings.length_unit = source.unit_settings.length_unit
    scene.view_settings.view_transform = source.view_settings.view_transform
    scene.view_settings.look = source.view_settings.look
    scene.view_settings.exposure = source.view_settings.exposure
    scene.view_settings.gamma = source.view_settings.gamma
    scene.world = source.world
    scene.frame_start = params['frame_start']
    scene.frame_end = params['frame_end']
    scene.frame_current = params['frame_start']
    after = cm_source_fingerprint(source)
    if after['fingerprint'] != before['fingerprint']:
        cm_fail('CINEMATIC_SOURCE_CHANGED_BY_CANDIDATE', 'creating the candidate changed the source scene')
    for obj in src.all_objects:
        if obj.name in excluded:
            cm_fail('CINEMATIC_EXCLUSION_FAILED', obj.name + ' still reaches the candidate')
    return {'scene': scene.name, 'collections': {'src': src.name, 'cin': cin.name}, 'linked_objects': linked,
            'excluded_objects': sorted(excluded), 'baseline': baseline, 'linked_collections': wholesale,
            'individually_linked_objects': individually, 'mirror_collections': counter['mirrors'],
            'fingerprints': {'source': after['fingerprint'], 'candidate': cm_candidate_fingerprint(scene, src, cin)['fingerprint']}}

result = cm_create(params)
'''

REJECT = COMMON + r'''
def cm_reject(params):
    source = cm_scene(params['source_scene'])
    generation = params['generation_id']
    undo_failed = cm_undo(params['touched'])
    actors = [bpy.data.objects.get(n) for n in params['actor_objects']]
    actors = [a for a in actors if a is not None]
    if params.get('baseline'):
        cm_restore(source, actors, params['baseline'])
    for window in bpy.context.window_manager.windows:
        if window.scene.get('production_m10_owner') == generation:
            window.scene = source
    removed = {'objects': [], 'cameras': [], 'lights': [], 'actions': [], 'worlds': [], 'collections': [], 'scenes': []}
    for obj in list(bpy.data.objects):
        if obj.get('production_m10_owner') == generation:
            removed['objects'].append(obj.name)
            bpy.data.objects.remove(obj)
    for data in list(bpy.data.cameras):
        if data.get('production_m10_owner') == generation and data.users == 0:
            removed['cameras'].append(data.name)
            bpy.data.cameras.remove(data)
    for data in list(bpy.data.lights):
        if data.get('production_m10_owner') == generation and data.users == 0:
            removed['lights'].append(data.name)
            bpy.data.lights.remove(data)
    for action in list(bpy.data.actions):
        if action.get('production_m10_owner') == generation:
            removed['actions'].append(action.name)
            bpy.data.actions.remove(action)
    for world in list(bpy.data.worlds):
        if world.get('production_m10_owner') == generation:
            removed['worlds'].append(world.name)
            bpy.data.worlds.remove(world)
    for collection in list(bpy.data.collections):
        if collection.get('production_m10_owner') == generation:
            removed['collections'].append(collection.name)
            bpy.data.collections.remove(collection)
    for scene in list(bpy.data.scenes):
        if scene.get('production_m10_owner') == generation:
            removed['scenes'].append(scene.name)
            bpy.data.scenes.remove(scene)
    after = cm_source_fingerprint(source)
    return {'undo_failed': undo_failed, 'removed': removed, 'source_fingerprint': after['fingerprint']}

result = cm_reject(params)
'''


def source_info(ctx, source_scene=None, exclude_objects=None):
    return ctx.bridge.run(SOURCE_INFO, {'source_scene': source_scene, 'exclude_objects': list(exclude_objects or [])})


def stale_actors(ctx, records, scene_name):
    """Re-measure registered actors before a plan binds to them: a rig, mesh or Action changed since registration
    makes the record stale (ACTOR_STALE) instead of surfacing only after the candidate exists."""
    from . import actors as actor_module
    if not records:
        return []
    observed = ctx.bridge.run(actor_module.DIGEST_BODY, {'source_scene': scene_name, 'scene': None, 'scene_checks': False,
                                                         'actors': [actor_module.actor_spec(r) for r in records]})
    stale = []
    for record in records:
        checked, changed, info = actor_module.compare_actor(record, observed['actors'].get(record['actor_id']))
        if changed:
            stale.append({'actor_id': record['actor_id'], 'object': record['object'],
                          'changed': [row.get('item') for row in changed][:10]})
    return stale


def actor_objects(actors):
    names = []
    for actor in actors:
        for name in [actor['object']] + list(actor.get('meshes') or []):
            if name not in names:
                names.append(name)
    return names


def build_plan(ctx, req, opts):
    info = source_info(ctx, opts.get('source_scene'), opts.get('exclude_objects'))
    asset = current_asset(ctx.bridge, ctx.storage)
    key = registry.asset_key(asset, info['scene'])
    normalized = planning.normalize_plan(req.brief, opts.get('beats'), opts.get('shots'), info['settings'])
    actors = []
    rows = normalized['brief'].get('actors') or []
    records = [registry.find(ctx.storage, key, row['actor_id']) for row in rows]
    stale = stale_actors(ctx, records, info['scene'])
    if stale:
        raise ValueError('ACTOR_STALE: registered actors changed since registration; register them again: '
                         + '; '.join(r['object'] + ' ' + ','.join(str(c) for c in r['changed']) for r in stale))
    for row, record in zip(rows, records):
        actors.append({'actor_id': record['actor_id'], 'object': record['object'], 'data': record.get('data'),
                       'meshes': record.get('meshes') or [], 'fingerprint': record['fingerprint'],
                       'role': row.get('role') or record.get('role')})
    if len(actors) > model.MAX_ACTORS:
        raise ValueError('CINEMATIC_CAPACITY: at most ' + str(model.MAX_ACTORS) + ' actors')
    return {'schema': model.SCHEMA, 'brief': normalized['brief'], 'beats': normalized['beats'], 'shots': normalized['shots'],
            'timeline': normalized['timeline'], 'actors': actors, 'actor_registry_key': key,
            'source': {'scene': info['scene'], 'fingerprint': info['fingerprint'], 'settings': info['settings'],
                       'filepath': info['filepath'], 'cameras_lights': info['cameras_lights']},
            'asset': {'role': asset.get('role'), 'asset_id': asset.get('asset_id'), 'path': asset.get('path')},
            'exclude_objects': sorted(opts.get('exclude_objects') or []), 'backend_fingerprint': store.backend_fingerprint(),
            'no_action_created': True}


def create_candidate(ctx, req):
    opts = model.closed(req.options, {'beats', 'shots', 'source_scene', 'exclude_objects'})
    if req.cinematic:
        path, plan, state = store.load(ctx.storage, req.cinematic)
        store.require_state(state, {'PLANNED'})
    else:
        plan = build_plan(ctx, req, opts)
        if not req.apply:
            return {'status': 'proposal', 'apply': False, 'plan': {k: plan[k] for k in ('brief', 'beats', 'shots', 'timeline', 'actors', 'source')},
                    'next': 'call again with apply=true to save the plan and create the candidate scene'}
        path, plan = store.save(ctx.storage, ctx.operation_id, plan)
        from pathlib import Path
        path = Path(path)
        state = {'state': 'PLANNED', 'refinement_cycles': 0}
    if not req.apply:
        return {'status': 'proposal', 'apply': False, **store.public(path, plan, state)}
    require_mutable_asset(ctx.bridge, ctx.storage, 'M10 candidate creation')
    if plan.get('backend_fingerprint') != store.backend_fingerprint():
        raise ValueError('CINEMATIC_BACKEND_CHANGED: cinematic code changed after this plan; create a new plan')
    point = create_checkpoint(ctx.bridge, ctx.storage, 'Before M10 candidate scene', ctx.operation_id, 'cinematic_create_candidate')
    version = 1
    params = {'generation_id': plan['generation_id'], 'version': version, 'source_scene': plan['source']['scene'],
              'source_fingerprint': plan['source']['fingerprint'],
              'scene': model.owned_prefix(plan['generation_id'], version),
              'collections': {'src': model.owned_name(plan['generation_id'], version, 'SRC'),
                              'cin': model.owned_name(plan['generation_id'], version, 'CIN')},
              'actor_objects': actor_objects(plan['actors']), 'exclude_objects': plan['exclude_objects'],
              'mirror_prefix': model.owned_name(plan['generation_id'], version, 'SRCM'),
              'frame_start': plan['timeline']['frame_start'], 'frame_end': plan['timeline']['frame_end']}
    try:
        data = ctx.bridge.run(CREATE, params)
    except Exception as exc:
        if is_uncertain(exc):
            store.record(path, dict(state, uncertain_operation=True, uncertain_step='create_candidate', checkpoint=point['manifest']))
        raise
    state = dict(state, state='CINEMATIC_CANDIDATE', checkpoint=point['manifest'], revision=0,
                 assembly={'version': version, 'scene': data['scene'], 'collections': data['collections'],
                           'source_scene': plan['source']['scene'], 'linked_objects': data['linked_objects'],
                           'excluded_objects': data['excluded_objects'], 'touched': [], 'owned': {},
                           'linked_collections': data['linked_collections'],
                           'individually_linked_objects': data['individually_linked_objects'],
                           'baseline': data['baseline']},
                 fingerprints=data['fingerprints'], timeline=plan['timeline'], actors={}, cameras={}, history=[])
    store.record(path, state)
    return {'status': 'candidate_created', 'apply': True, **store.public(path, plan, state),
            'linked_objects': data['linked_objects'], 'excluded_objects': data['excluded_objects'],
            'linked_collections': data['linked_collections'],
            'individually_linked_objects': data['individually_linked_objects']}


def reject(ctx, req):
    path, plan, state = store.load(ctx.storage, req.cinematic)
    store.require_state(state, model.STATES - {'ACCEPTED', 'REJECTED'})
    if not req.apply:
        assembly = state.get('assembly') or {}
        return {'status': 'proposal', 'apply': False, 'would_undo': len(assembly.get('touched') or []),
                'would_remove_scene': assembly.get('scene'), **store.public(path, plan, state)}
    if not state.get('assembly'):
        state = dict(state, state='REJECTED', rejection={'source_restored': True, 'blender_changed': False})
        store.record(path, state)
        return {'status': 'rejected', **store.public(path, plan, state)}
    asset = require_mutable_asset(ctx.bridge, ctx.storage, 'M10 candidate rejection')
    if asset.get('path') != plan['asset'].get('path'):
        raise ValueError('CINEMATIC_STALE_ASSET: the open file is not the file this plan was made for')
    point = create_checkpoint(ctx.bridge, ctx.storage, 'Before M10 rejection', ctx.operation_id, 'cinematic_reject')
    assembly = state['assembly']
    params = {'generation_id': plan['generation_id'], 'version': assembly['version'], 'source_scene': plan['source']['scene'],
              'touched': assembly.get('touched') or [], 'actor_objects': actor_objects(plan['actors']),
              'baseline': assembly.get('baseline')}
    try:
        data = ctx.bridge.run(REJECT, params)
    except Exception as exc:
        if is_uncertain(exc):
            store.record(path, dict(state, uncertain_operation=True, uncertain_step='reject', checkpoint=point['manifest']))
        raise
    restored = data['source_fingerprint'] == plan['source']['fingerprint'] and not data['undo_failed']
    state = dict(state, state='REJECTED', checkpoint=point['manifest'],
                 rejection={'source_restored': restored, 'source_fingerprint': data['source_fingerprint'],
                            'expected_fingerprint': plan['source']['fingerprint'], 'undo_failed': data['undo_failed'],
                            'removed': data['removed'], 'reason': (req.options or {}).get('reason')})
    store.record(path, state)
    result = {'status': 'rejected' if restored else 'error', **store.public(path, plan, state),
              'source_restored': restored, 'removed': {k: len(v) for k, v in data['removed'].items()},
              'undo_failed': data['undo_failed']}
    if not restored:
        result['errors'] = [{'code': 'CINEMATIC_REJECT_RESTORE_INCOMPLETE',
                             'message': 'source scene differs from the plan after rejection; inspect checkpoint ' + point['manifest']}]
    return result
