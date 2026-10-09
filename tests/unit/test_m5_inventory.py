"""Independent public tool contract and annotation inventory, retaining all 85 inherited tools."""
import asyncio
from blender_mcp.production.server import mcp

M5_READERS={'retopology_analyze','retopology_analyze_density','retopology_priority_map','retopology_budget',
    'retopology_plan','retopology_compare_source','retopology_compare_silhouette','retopology_validate',
    'retopology_analyze_morph_transfer','game_mesh_validate_tangents',
    'uv_analyze','uv_validate','uv_analyze_texel_density','udim_plan','bake_analyze_ray_distance','bake_validate'}
M5_WRITERS={'retopology_create_candidate','retopology_optimize_region','retopology_project_to_source',
    'retopology_analyze_deformation_regions','retopology_transfer_weights','retopology_validate_weights',
    'retopology_transfer_morphs','retopology_transfer_attributes','game_mesh_prepare_normals',
    'game_mesh_triangulation_preview','game_mesh_validate_triangulation','uv_generate','udim_apply_plan','uv_pack',
    'bake_prepare_pairs','bake_generate_cage','bake_smoke_test','game_mesh_validate','game_mesh_prepare_package'}
M5_NAMES=M5_READERS|M5_WRITERS

def test_all_required_m5_high_level_tools_have_structured_contracts():
    tools=asyncio.run(mcp.list_tools());lookup={t.name:t for t in tools}
    from blender_mcp.production.animation.generation.tools import NAMES as M6_NAMES
    from blender_mcp.production.facial.tools import NAMES as M7_NAMES
    from blender_mcp.production.physics.tools import NAMES as M8_NAMES
    from blender_mcp.production.materials.tools import NAMES as M9_NAMES
    assert len(M9_NAMES)==38 and {'production.'+name for name in M9_NAMES}<=set(lookup)
    assert len(tools)==120+len(M6_NAMES)+len(M7_NAMES)+len(M8_NAMES)+len(M9_NAMES) and len(M5_NAMES)==35
    for name in M5_NAMES:
        t=lookup['production.'+name]
        assert t.outputSchema is not None
        assert t.annotations.readOnlyHint is (name in M5_READERS)
        assert t.annotations.idempotentHint is (name in M5_READERS)
        assert t.annotations.openWorldHint is False
        assert '_operation_id' not in t.inputSchema['properties']
        if name in M5_WRITERS:assert 'operation_id' in t.inputSchema['properties']
        if 'mode' in t.inputSchema['properties']:assert t.inputSchema['properties']['mode']['default']=='analyze'
