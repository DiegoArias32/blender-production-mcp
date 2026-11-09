"""Dispatch for the M10 tools: one handler per tool name, registered by the subsystem modules.

Handlers have the signature handler(ctx: Context, req: Request) -> dict. Unknown or not-yet-implemented tools fail
with CINEMATIC_NOT_IMPLEMENTED instead of pretending to succeed.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

HANDLERS = {}
SUBSYSTEMS = ('lifecycle_handlers', 'planning_handlers', 'actors', 'cameras', 'performance', 'lighting', 'analysis',
              'review')


@dataclass(frozen=True)
class Context:
    bridge: object
    storage: object
    operation_id: str


@dataclass(frozen=True)
class Request:
    cinematic: str | None = None
    actor: str | None = None
    object_name: str | None = None
    brief: dict | None = None
    options: dict | None = None
    apply: bool = False


def handler(name):
    def register(function):
        if name in HANDLERS:
            raise RuntimeError('duplicate cinematic handler ' + name)
        HANDLERS[name] = function
        return function
    return register


_loaded = False


def load_handlers():
    global _loaded
    if _loaded:
        return
    for module in SUBSYSTEMS:
        try:
            import_module(__package__ + '.' + module)
        except ModuleNotFoundError as exc:
            if exc.name != __package__ + '.' + module:
                raise
    _loaded = True


def dispatch(name, bridge, storage, operation_id, cinematic=None, actor=None, object_name=None, brief=None,
             options=None, apply=False):
    if not isinstance(apply, bool):
        raise ValueError('CINEMATIC_APPLY_BOOLEAN_REQUIRED')
    if options is not None and not isinstance(options, dict):
        raise ValueError('CINEMATIC_OPTIONS_OBJECT_REQUIRED')
    if brief is not None and not isinstance(brief, dict):
        raise ValueError('CINEMATIC_BRIEF_OBJECT_REQUIRED')
    load_handlers()
    function = HANDLERS.get(name)
    if function is None:
        raise ValueError('CINEMATIC_NOT_IMPLEMENTED: ' + name)
    return function(Context(bridge, storage, operation_id),
                    Request(cinematic, actor, object_name, brief, dict(options or {}), apply))
