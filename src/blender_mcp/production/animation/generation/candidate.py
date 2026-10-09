"""Evaluate a candidate using inherited tools inside one restoring Blender payload."""
from __future__ import annotations

from textwrap import indent
import ast
from ...common.m3_transaction import BASE_RUNTIME
from ...common.payload import link_payload


class CandidateBridge:
    def __init__(self, bridge, object_name, action_name):
        self.bridge, self.object_name, self.action_name = bridge, object_name, action_name

    def run(self, body, params=None):
        # Every RPC is independently restoring; no unguarded assignment between calls.
        begin = r'''
_m6_eval_obj=m2_object(params['_m6_eval_object'])
_m6_eval_old=_m6_eval_obj.animation_data.action if _m6_eval_obj.animation_data else None
_m6_eval_slot=m2_slot(_m6_eval_obj)
_m6_eval_state=m3_capture(_m6_eval_obj)
_m6_eval_action=bpy.data.actions.get(params['_m6_eval_action'])
if _m6_eval_action is None: raise ValueError('CANDIDATE_MISSING: retained Action missing')
try:
    m3_assign(_m6_eval_obj,_m6_eval_action)
'''
        end = r'''
finally:
    m3_assign(_m6_eval_obj,_m6_eval_old,_m6_eval_slot)
    m3_restore(_m6_eval_obj,_m6_eval_state)
    if _m6_eval_old is None and not _m6_eval_state['animation_data_existed']:
        _m6_eval_obj.animation_data_clear()
'''
        # Definitions remain at module scope so the shared linker can eliminate duplicates.
        # Nesting all runtime definitions in try would retain two full runtimes and breach
        # the upstream AST budget even though the actual operation is small.
        source = BASE_RUNTIME + body
        tree = ast.parse(source)
        definitions, entries = [], []
        for node in tree.body:
            segment = ast.get_source_segment(source, node, padded=True) or ast.unparse(node)
            constant = False
            if isinstance(node, ast.Assign) and all(isinstance(target, ast.Name) and target.id.isupper() for target in node.targets):
                try:
                    ast.literal_eval(node.value)
                    constant = True
                except (ValueError, TypeError):
                    pass
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Import, ast.ImportFrom)) or constant:
                definitions.append(segment)
            else:
                entries.append(segment)
        wrapped = '\n'.join(definitions)+begin+indent('\n'.join(entries), '    ')+end
        return self.bridge.run(wrapped,
                               dict(params or {}, _m6_eval_object=self.object_name,
                                    _m6_eval_action=self.action_name))
