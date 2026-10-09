"""Builder input remains structured and socket qualification uses actual RNA."""
from types import SimpleNamespace
import ast

import pytest

from blender_mcp.production.materials import graph
from blender_mcp.production.common.bridge import build_code


def test_builder_normalizes_even_without_mcp_service(monkeypatch):
    class Bridge:
        def run(self, body, params):
            assert body == graph.BUILD
            return params
    monkeypatch.setattr(graph.integrity, 'status', lambda *args: {'fingerprint': 'unchanged'})
    params = {'object_name': 'Source', 'material_name': 'SourceMaterial', 'candidate_object': 'Candidate',
        'candidate_material': 'CandidateMaterial', 'expected_status': {'fingerprint': 'unchanged'},
        'expected_candidate': {'fingerprint': 'unchanged'}, 'definition': {'parameters': {'metallic': .37}}}
    result = graph.build(Bridge(), params)
    assert result['definition']['parameters']['metallic'] == .37
    assert result['socket_names']['metallic'] == 'Metallic'
    with pytest.raises(ValueError):
        graph.build(Bridge(), {'definition': {'arbitrary_python': "__import__('os')"}})


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 2, True])
def test_bad_numeric_values_never_reach_blender(value):
    class ForbiddenBridge:
        def run(self, *args):
            raise AssertionError('invalid definition reached Blender')
    with pytest.raises(ValueError):
        graph.build(ForbiddenBridge(), {'definition': {'parameters': {'roughness': value}}})


def test_builder_payload_keeps_upstream_sandbox():
    code = build_code(graph.BUILD, {})
    assert len(code.encode()) < 200000


def test_stale_candidate_external_file_rejected_before_graph_mutation(monkeypatch):
    def current(bridge, object_name, material_name, options):
        return {'fingerprint': 'source unchanged' if object_name == 'Source' else 'new candidate file bytes'}
    monkeypatch.setattr(graph.integrity, 'status', current)
    class NoMutation:
        def run(self, *args):
            raise AssertionError('stale candidate dependency reached graph mutation')
    with pytest.raises(ValueError, match='MATERIAL_STALE_CANDIDATE'):
        graph.build(NoMutation(), {'object_name': 'Source', 'material_name': 'SourceMaterial',
            'candidate_object': 'Candidate', 'candidate_material': 'CandidateMaterial',
            'expected_status': {'fingerprint': 'source unchanged'},
            'expected_candidate': {'fingerprint': 'old candidate file bytes'}})
