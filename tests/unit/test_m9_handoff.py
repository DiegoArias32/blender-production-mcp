"""Handoff is a bounded neutral contract, never an implicit readiness override."""
import pytest
from blender_mcp.production.materials.handoff import validate_options


@pytest.mark.parametrize('options', [
    {'readiness': 'READY'}, {'bake_ready': True}, {'run_painter': True},
    {'resolution_targets': {'default': True}}, {'resolution_targets': {'1000': 1024}},
    {'resolution_targets': {'1011': 9000}}, {'expected_maps': ['Normal', 'Normal']},
    {'mesh_maps_expected': ['invented']}, {'asset_version': ''},
])
def test_untrusted_claims_or_invalid_targets_rejected(options):
    with pytest.raises(ValueError):
        validate_options(options)


def test_single_material_many_udims_and_independent_maps_allowed():
    options = validate_options({'resolution_targets': {'1001': 256, '1002': 256, '1011': 512},
                                'expected_maps': ['BaseColor', 'Normal', 'Roughness'],
                                'mesh_maps_expected': ['Thickness']})
    assert len(options['resolution_targets']) == 3
    assert options['mesh_maps_expected'] == ['Thickness']


def relationship_fixture(monkeypatch, coordinates):
    import sys
    from types import SimpleNamespace as N
    from blender_mcp.production.materials.handoff import RELATIONSHIPS
    layer = N(name='UV', data=[N(uv=co) for co in coordinates])
    obj = N(type='MESH', name='Fixture',
            data=N(materials=['Mat'], uv_layers=N(active=layer),
                   polygons=[N(material_index=0, loop_indices=list(range(len(coordinates))))]),
            material_slots=[N(link='DATA', material=N(name='Mat'))])
    monkeypatch.setitem(sys.modules, 'bpy', N(data=N(objects={'Fixture': obj})))
    namespace = {'params': {'object_name': 'Fixture'}}
    exec(RELATIONSHIPS, namespace)
    return namespace['result']


def test_handoff_boundary_matches_shared_m5_tile_contract(monkeypatch):
    result = relationship_fixture(monkeypatch, [(0, 0), (1, 0), (1, 1), (0, 1)])
    assert result['UDIMs'] == [1001]


@pytest.mark.parametrize('coordinates', [
    [(0.2, 0.2), (2.8, 0.2), (2.8, 0.8)],
    [(-1, 0), (-.2, 0), (-.2, .8)],
])
def test_handoff_refuses_incomplete_tile_map_for_crossed_or_outside_face(monkeypatch, coordinates):
    with pytest.raises(ValueError, match='MATERIAL_HANDOFF_UV_AMBIGUOUS'):
        relationship_fixture(monkeypatch, coordinates)


@pytest.mark.parametrize('m5,m9', [('DIRECTX', 'OPENGL'), ('OPENGL', 'DIRECTX')])
def test_handoff_rejects_conflicting_bake_normal_contract_before_checkpoint(monkeypatch, m5, m9):
    from types import SimpleNamespace as N
    from blender_mcp.production.materials import handoff as h, model
    from blender_mcp.production.bake import service as bake
    monkeypatch.setattr(h, 'require_mutable_asset', lambda *a: None)
    monkeypatch.setattr(h.integrity, 'status', lambda *a: {'fingerprint': 'source'})
    monkeypatch.setattr(h, 'current_asset', lambda *a: {'role': 'FIXTURE'})
    monkeypatch.setattr(bake, 'load_pair', lambda *a: {'low': 'Mesh', 'options': {'normal_convention': m5}})
    calls = []
    monkeypatch.setattr(h, 'create_checkpoint', lambda *a: calls.append(a))
    with pytest.raises(ValueError, match='MATERIAL_HANDOFF_NORMAL_CONFLICT'):
        h.create_handoff(N(run=lambda *a: {}), None, 'conflict', 'Mesh', 'Mat',
                         model.normalize_definition({'normal_convention': m9}), {'bake_pair': 'pair'}, apply=True)
    assert calls == []
