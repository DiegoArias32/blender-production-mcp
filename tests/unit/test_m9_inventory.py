"""M9 public catalogue must preserve exact inherited identities and truthful hints."""
import asyncio
import json
from pathlib import Path

import pytest

from blender_mcp.production import server

EXPECTED = frozenset('''material_inspect material_analyze_nodes material_compatibility_report
material_analyze_pbr material_validate_pbr material_create_pbr texture_inspect
texture_validate_channels texture_validate_colorspace texture_dependency_report
material_validate_normals material_validate_height material_validate_tangent_workflow
material_configure_sss material_configure_transmission material_configure_opacity
material_configure_emission material_create_profile material_apply_profile material_validate_profile
material_validate_uv material_validate_udim material_analyze_texture_sets material_create_candidate
material_apply_candidate material_accept material_reject material_build_shader material_validate_graph
material_quality_report material_render_preview material_render_multilighting material_compare_before_after
material_performance_report material_create_handoff material_review material_refine material_status'''.split())
MUTATIONS = frozenset('''material_create_pbr material_configure_sss material_configure_transmission
material_configure_opacity material_configure_emission material_apply_profile material_create_candidate
material_apply_candidate material_accept material_reject material_build_shader material_render_preview
material_render_multilighting material_compare_before_after material_create_handoff material_review material_refine'''.split())
CATALOGUE = {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_exact_236_baseline_identities_and_38_new_units():
    baseline = json.loads((Path(__file__).parents[1] / 'fixtures/m9-baseline-tools.json').read_text())
    inherited = set(baseline['tools'])
    new = {'production.' + name for name in EXPECTED}
    assert len(inherited) == 236 and len(new) == 38
    assert not inherited & new
    assert set(CATALOGUE) == inherited | new


@pytest.mark.parametrize('name', sorted(EXPECTED))
def test_material_mutations_default_to_proposal_and_keep_idempotency(name):
    tool = CATALOGUE['production.' + name]
    props = tool.inputSchema['properties']
    assert {'object_name', 'material_name', 'material', 'definition', 'profile', 'options', 'apply'} <= set(props)
    assert props['apply']['default'] is False
    assert tool.annotations.readOnlyHint is (name not in MUTATIONS)
    assert ('operation_id' in props) is (name in MUTATIONS)
    assert tool.annotations.openWorldHint is False
