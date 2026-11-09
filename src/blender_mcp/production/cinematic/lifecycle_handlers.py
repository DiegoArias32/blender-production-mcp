"""Handlers for candidate creation, rejection and status (the frame the other subsystems mutate inside)."""
from __future__ import annotations

from . import lifecycle, store
from .service import handler


@handler('cinematic_create_candidate')
def create_candidate(ctx, req):
    return lifecycle.create_candidate(ctx, req)


@handler('cinematic_reject')
def reject(ctx, req):
    return lifecycle.reject(ctx, req)


@handler('cinematic_status')
def status(ctx, req):
    """Ledger summary plus a freshness check that never raises: stale plans report why."""
    path, plan, state = store.load(ctx.storage, req.cinematic)
    result = {'status': 'success', **store.public(path, plan, state)}
    if state.get('state') in {'ACCEPTED', 'REJECTED'}:
        result['freshness'] = 'terminal'
        return result
    try:
        store.fresh(ctx.bridge, ctx.storage, plan, state)
        result['freshness'] = 'current'
    except (ValueError, PermissionError, RuntimeError) as exc:
        result.update(status='warning', freshness='stale', stale_reason=str(exc)[:500])
    return result
