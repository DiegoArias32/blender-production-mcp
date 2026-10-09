"""Dead-function elimination for Blender payloads (M3.1).

Payloads are composed by concatenating shared runtimes. The upstream safe-mode validator caps a
script at 20000 AST nodes, and the composed contact-correction payload had already reached that
ceiling. Rather than weakening the validator, each payload keeps only what its entry statements can
reach: imports, top-level non-function statements and every function transitively referenced from
them. Later definitions of a name replace earlier ones exactly as Python would, so backend overrides
(``m3_context``, ``m3_related_objects``...) keep their meaning.

Original source text of every kept statement is reused verbatim (``ast.get_source_segment``), so the
validator still sees the real code, comments included.
"""
from __future__ import annotations

import ast
from functools import lru_cache


def _referenced_names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


@lru_cache(maxsize=256)
def link_payload(source: str) -> str:
    # Templates are constant strings, so the linked form is cached per template.
    tree = ast.parse(source)
    last_definition: dict[str, int] = {}
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            last_definition[node.name] = index
    roots = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    pending = set().union(*(_referenced_names(node) for node in roots)) if roots else set()
    keep: set[int] = set()
    while pending:
        name = pending.pop()
        index = last_definition.get(name)
        if index is None or index in keep:
            continue
        keep.add(index)
        pending |= _referenced_names(tree.body[index])
    parts = []
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and index not in keep:
            continue
        segment = ast.get_source_segment(source, node, padded=True)
        parts.append(segment if segment is not None else ast.unparse(node))
    return "\n".join(parts) + "\n"
