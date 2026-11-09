"""Cinematic ledger on the generic generation store, freshness, and the one mutation wrapper every assembly step uses.

Order of every mutation (AGENTS.md): asset role -> freshness -> checkpoint -> Blender change -> verification ->
event. An uncertain transport failure marks the plan `uncertain_operation` and blocks it for good (M8/M9 pattern):
never retry a mutation with another key.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
from pathlib import Path

from ..animation.generation import store as gstore
from ..checkpoint import create_checkpoint
from ..common.asset_safety import current_asset, require_mutable_asset
from ..common.ledger import is_uncertain
from ..common.operations import fingerprint
from . import model
from .runtime import COMMON

STATUS_BODY = COMMON + r'''
def cm_status(params):
    source = cm_scene(params['source_scene'])
    result = {'source': cm_source_fingerprint(source), 'filepath': bpy.data.filepath}
    if params.get('scene'):
        scene, src, cin = cm_candidate(params)
        result['candidate'] = cm_candidate_fingerprint(scene, src, cin)
    return result

result = cm_status(params)
'''


@lru_cache(maxsize=1)
def backend_fingerprint() -> str:
    """Plans are bound to the cinematic code this process loaded: a server restarted on changed code refuses older
    plans (CINEMATIC_BACKEND_CHANGED). Computed once per process: the code that runs is the code that was loaded."""
    folder = Path(__file__).parent
    return fingerprint({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob('*.py'))})


def load(storage, reference):
    if not isinstance(reference, str) or not reference:
        raise ValueError('CINEMATIC_REFERENCE_REQUIRED: pass the plan.json returned by cinematic_create_candidate')
    return gstore.load(storage, reference, model.CATEGORY)


def record(path, state):
    gstore.event(Path(path).parent, state, model.STATES)
    return state


def save(storage, operation_id, plan):
    return gstore.save(storage, operation_id, plan, model.CATEGORY, model.STATES)


def require_state(state, allowed):
    gstore.mutable(state)
    if state.get('state') not in allowed:
        raise ValueError('CINEMATIC_STATE_INVALID: ' + str(state.get('state')) + ' not in ' + ', '.join(sorted(allowed)))


def assembly_params(plan, state, extra=None):
    """The identity block every candidate payload receives."""
    assembly = state.get('assembly') or {}
    params = {'generation_id': plan['generation_id'], 'version': assembly.get('version', 1),
              'scene': assembly.get('scene'), 'collections': assembly.get('collections'),
              'source_scene': plan['source']['scene']}
    params.update(extra or {})
    return params


def actor_meshes(actor, state):
    """Meshes the candidate shows for a plan actor: the registered ones minus those excluded when the candidate was
    created, plus the owned copies that stand in for them. An excluded M8 secondary source never reaches the candidate
    scene; outside it, its matrix is the source scene's, not the cinematic's."""
    excluded = set(((state or {}).get('assembly') or {}).get('excluded_objects') or [])
    names = [name for name in actor.get('meshes') or [] if name not in excluded]
    entry = ((state or {}).get('actors') or {}).get(actor['actor_id']) or {}
    for row in entry.get('secondary') or []:
        copy = row.get('copy') or row.get('object')
        if copy and copy not in names:
            names.append(copy)
    return names


def status(bridge, plan, state):
    params = assembly_params(plan, state)
    if not state.get('assembly'):
        params['scene'] = None
    return bridge.run(STATUS_BODY, params)


def fresh(bridge, storage, plan, state):
    """Refuse when the world moved under the plan: uncertain operation, new code, other file, or edits made outside
    the cinematic tools since the last recorded step."""
    if state.get('uncertain_operation'):
        raise ValueError('CINEMATIC_OPERATION_UNCERTAIN: a previous step may or may not have reached Blender; inspect '
                         'the scene and checkpoint ' + str(state.get('checkpoint')) + ' and start a new generation')
    if plan.get('backend_fingerprint') != backend_fingerprint():
        raise ValueError('CINEMATIC_BACKEND_CHANGED: cinematic code changed after this plan; create a new plan')
    asset = current_asset(bridge, storage)
    if asset.get('path') != plan['asset'].get('path') or asset.get('asset_id') != plan['asset'].get('asset_id'):
        raise ValueError('CINEMATIC_STALE_ASSET: the open file is not the file this plan was made for')
    observed = status(bridge, plan, state)
    expected = state.get('fingerprints') or {'source': plan['source']['fingerprint']}
    if observed['source']['fingerprint'] != expected['source']:
        raise ValueError('CINEMATIC_STALE_SOURCE: the source scene changed outside the cinematic tools')
    if state.get('assembly') and observed['candidate']['fingerprint'] != expected.get('candidate'):
        raise ValueError('CINEMATIC_STALE_CANDIDATE: the candidate scene changed outside the cinematic tools')
    return observed


def public(path, plan, state):
    assembly = state.get('assembly') or {}
    return {'cinematic': str(path), 'generation_id': plan['generation_id'], 'state': state.get('state'),
            'revision': state.get('revision', 0), 'refinement_cycles': state.get('refinement_cycles', 0),
            'scene': assembly.get('scene'), 'source_scene': plan['source']['scene'],
            'timeline': state.get('timeline') or plan.get('timeline'), 'actors': sorted((state.get('actors') or {}).keys()),
            'cameras': sorted((state.get('cameras') or {}).keys()), 'shots': len(plan.get('shots') or []),
            'touched': len(assembly.get('touched') or []), 'checkpoint': state.get('checkpoint'),
            'render': (state.get('render') or {}).get('output_dir'), 'review': (state.get('review') or {}).get('decision'),
            'quality_report': (state.get('quality_report') or {}).get('passed'),
            'pending_refinement': bool(state.get('pending_refinement')), 'uncertain_operation': bool(state.get('uncertain_operation'))}


def invalidate_review(state):
    """Any assembly change after ASSEMBLED/REVIEWED drops render, review, quality report and the finalize summary and
    returns to the candidate state: a stale review never accepts a changed sequence."""
    changed = dict(state)
    if changed.get('state') in {'ASSEMBLED', 'REVIEWED'}:
        changed['state'] = 'CINEMATIC_CANDIDATE'
    for key in ('render', 'review', 'quality_report', 'assembled'):
        if changed.get(key) is not None:
            changed[key] = None
    return changed


def mutate(ctx, path, plan, state, label, body, params, allowed=model.ASSEMBLY_STATES, checkpoint=True):
    """Run one assembly mutation and record it. The payload must return a dict with optional `touched` (rows to
    append), `owned` (dict of lists to extend), and `fingerprints` {source, candidate} computed at its end.
    Returns (new_state, payload_result)."""
    require_state(state, allowed)
    require_mutable_asset(ctx.bridge, ctx.storage, 'M10 ' + label)
    fresh(ctx.bridge, ctx.storage, plan, state)
    point = create_checkpoint(ctx.bridge, ctx.storage, 'Before M10 ' + label, ctx.operation_id, 'cinematic_' + label) if checkpoint else None
    try:
        result = ctx.bridge.run(body, params)
    except Exception as exc:
        if is_uncertain(exc):
            record(path, dict(state, uncertain_operation=True, uncertain_step=label,
                              checkpoint=point['manifest'] if point else state.get('checkpoint'),
                              last_error={'type': type(exc).__name__, 'message': str(exc)[:500]}))
        raise
    new = invalidate_review(deepcopy(state))
    assembly = new.setdefault('assembly', {})
    assembly.setdefault('touched', []).extend(result.get('touched') or [])
    owned = assembly.setdefault('owned', {})
    for key, rows in (result.get('owned') or {}).items():
        names = owned.setdefault(key, [])
        names.extend(r for r in rows if r not in names)
    if result.get('fingerprints'):
        new['fingerprints'] = {'source': result['fingerprints']['source'], 'candidate': result['fingerprints']['candidate']}
    if point:
        new['checkpoint'] = point['manifest']
    new['revision'] = new.get('revision', 0) + 1
    new.setdefault('history', []).append({'revision': new['revision'], 'step': label,
                                          'touched': len(result.get('touched') or []),
                                          'owned': {k: len(v) for k, v in (result.get('owned') or {}).items()}})
    return new, result
