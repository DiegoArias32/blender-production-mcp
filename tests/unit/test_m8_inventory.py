"""Independent M8 public contracts, preserving the verified 201-tool M7 baseline."""
import asyncio
import ast
import json
from pathlib import Path

import pytest

from blender_mcp.production import server


# Frozen before M8 registration; replacing a prior tool cannot hide behind a count.
BASELINE_NAMES = frozenset(
    'production.animation_accept_generated\nproduction.animation_add_follow_through\nproduction.animation_add_overlap\nproduction.animation_adjust_timing\nproduction.animation_analyze\nproduction.animation_analyze_arcs\nproduction.animation_analyze_balance\nproduction.animation_analyze_bezier\nproduction.animation_analyze_body_mechanics\nproduction.animation_analyze_contacts\nproduction.animation_analyze_control_mode\nproduction.animation_analyze_effector_contact\nproduction.animation_analyze_fcurves\nproduction.animation_analyze_foot_phase\nproduction.animation_analyze_motion_quality\nproduction.animation_analyze_pose_silhouette\nproduction.animation_analyze_root_motion\nproduction.animation_analyze_spacing\nproduction.animation_analyze_timing\nproduction.animation_cleanup_fcurves\nproduction.animation_compare_actions\nproduction.animation_create_action\nproduction.animation_create_brief\nproduction.animation_deformation_qa\nproduction.animation_detect_contact_surface\nproduction.animation_detect_foot_sliding\nproduction.animation_detect_limb_pops\nproduction.animation_detect_limb_stretch\nproduction.animation_detect_quaternion_flips\nproduction.animation_duplicate_action\nproduction.animation_fix_foot_sliding\nproduction.animation_fix_quaternion_flips\nproduction.animation_generate\nproduction.animation_generate_attack\nproduction.animation_generate_blocking\nproduction.animation_generate_breakdowns\nproduction.animation_generate_effector_contact\nproduction.animation_generate_foot_contacts\nproduction.animation_generate_hit_reaction\nproduction.animation_generate_idle\nproduction.animation_generate_locomotion\nproduction.animation_generate_pose\nproduction.animation_generate_root_motion\nproduction.animation_generate_start_stop\nproduction.animation_generate_turn\nproduction.animation_generation_compatibility\nproduction.animation_generation_plan\nproduction.animation_generation_render\nproduction.animation_plan_poses\nproduction.animation_polish_curves\nproduction.animation_prepare_root_motion\nproduction.animation_refine_arcs\nproduction.animation_refine_balance\nproduction.animation_refine_generated\nproduction.animation_refinement_plan\nproduction.animation_reject_generated\nproduction.animation_repair_bezier\nproduction.animation_restore_action\nproduction.animation_retime\nproduction.animation_spline\nproduction.animation_validate_blocking\nproduction.animation_validate_pose\nproduction.asset_create_working_copy\nproduction.asset_register_fixture\nproduction.asset_register_master\nproduction.asset_status\nproduction.bake_analyze_ray_distance\nproduction.bake_generate_cage\nproduction.bake_prepare_pairs\nproduction.bake_smoke_test\nproduction.bake_validate\nproduction.character_adjust_shape\nproduction.character_analyze_asymmetry\nproduction.character_analyze_intersections\nproduction.character_analyze_proportions\nproduction.character_analyze_silhouette\nproduction.character_analyze_surface\nproduction.character_capture_candidate_views\nproduction.character_capture_reference_views\nproduction.character_compare_before_after\nproduction.character_compare_reference\nproduction.character_create_candidate\nproduction.character_define_regions\nproduction.character_deformation_qa\nproduction.character_inspect\nproduction.character_lock_regions\nproduction.character_modeling_audit\nproduction.character_redesign_plan\nproduction.character_redesign_session\nproduction.character_render_multiview\nproduction.character_validate_locked_regions\nproduction.cleanup_artifacts\nproduction.create_checkpoint\nproduction.deformation_analyze\nproduction.deformation_compare\nproduction.deformation_test_pose\nproduction.diff_scene\nproduction.facial_accept\nproduction.facial_analyze_asymmetry\nproduction.facial_analyze_bones\nproduction.facial_analyze_curves\nproduction.facial_analyze_eyelids\nproduction.facial_analyze_eyes\nproduction.facial_analyze_hybrid\nproduction.facial_analyze_jaw\nproduction.facial_analyze_layer_conflicts\nproduction.facial_analyze_lips\nproduction.facial_analyze_shape_keys\nproduction.facial_classify_rig\nproduction.facial_compatibility_report\nproduction.facial_coordinate_gaze\nproduction.facial_create_brief\nproduction.facial_deformation_qa\nproduction.facial_generate\nproduction.facial_generate_blink\nproduction.facial_generate_brows\nproduction.facial_generate_coarticulation\nproduction.facial_generate_expression\nproduction.facial_generate_gaze\nproduction.facial_generate_jaw\nproduction.facial_generate_lid_follow\nproduction.facial_generate_lipsync\nproduction.facial_generate_microexpressions\nproduction.facial_generate_saccades\nproduction.facial_inspect\nproduction.facial_integrate_with_body\nproduction.facial_map_visemes\nproduction.facial_mocap_analyze\nproduction.facial_mocap_cleanup\nproduction.facial_mocap_map\nproduction.facial_mocap_retarget\nproduction.facial_plan_blinks\nproduction.facial_plan_expression\nproduction.facial_plan_microexpressions\nproduction.facial_polish_curves\nproduction.facial_prepare_speech\nproduction.facial_refine\nproduction.facial_reject\nproduction.facial_render_closeup\nproduction.facial_render_preview\nproduction.facial_review\nproduction.facial_validate_expression\nproduction.facial_validate_lipsync\nproduction.game_mesh_prepare_normals\nproduction.game_mesh_prepare_package\nproduction.game_mesh_triangulation_preview\nproduction.game_mesh_validate\nproduction.game_mesh_validate_tangents\nproduction.game_mesh_validate_triangulation\nproduction.inspect_armature\nproduction.mesh_candidate_create\nproduction.mesh_candidate_verify\nproduction.mesh_region_define\nproduction.mesh_region_verify\nproduction.mesh_signature\nproduction.mocap_analyze\nproduction.mocap_cleanup\nproduction.mocap_map_skeleton\nproduction.mocap_preview_retarget\nproduction.mocap_retarget\nproduction.operation_status\nproduction.qa_capture_viewport\nproduction.qa_compare_before_after\nproduction.qa_render_multiview\nproduction.qa_render_preview\nproduction.restore_checkpoint\nproduction.retopology_analyze\nproduction.retopology_analyze_deformation_regions\nproduction.retopology_analyze_density\nproduction.retopology_analyze_morph_transfer\nproduction.retopology_budget\nproduction.retopology_compare_silhouette\nproduction.retopology_compare_source\nproduction.retopology_create_candidate\nproduction.retopology_optimize_region\nproduction.retopology_plan\nproduction.retopology_priority_map\nproduction.retopology_project_to_source\nproduction.retopology_transfer_attributes\nproduction.retopology_transfer_morphs\nproduction.retopology_transfer_weights\nproduction.retopology_validate\nproduction.retopology_validate_weights\nproduction.rig_analyze\nproduction.rig_analyze_controls\nproduction.rig_analyze_limbs\nproduction.rig_bake_fk_to_ik\nproduction.rig_bake_ik_to_fk\nproduction.rig_classify\nproduction.rig_compatibility_report\nproduction.rig_map_semantics\nproduction.rig_match_fk_to_ik\nproduction.rig_match_ik_to_fk\nproduction.scene_audit\nproduction.udim_apply_plan\nproduction.udim_plan\nproduction.uv_analyze\nproduction.uv_analyze_texel_density\nproduction.uv_generate\nproduction.uv_pack\nproduction.uv_validate'.splitlines()
)
M8_NAMES = frozenset(
    'physics_accept\nphysics_analyze_accessory\nphysics_analyze_cloth\nphysics_analyze_hair\nphysics_analyze_motion\nphysics_analyze_penetration\nphysics_analyze_soft_region\nphysics_analyze_stability\nphysics_bake_cache\nphysics_bake_secondary_motion\nphysics_cache_plan\nphysics_classify_secondary\nphysics_collision_report\nphysics_compare_before_after\nphysics_compatibility_report\nphysics_create_brief\nphysics_create_candidate\nphysics_generate_accessory_motion\nphysics_generate_hair_secondary\nphysics_generate_secondary_bones\nphysics_generate_soft_secondary\nphysics_inspect_character\nphysics_plan\nphysics_plan_cloth_pins\nphysics_plan_hair\nphysics_prepare_colliders\nphysics_refine\nphysics_reject\nphysics_render_preview\nphysics_review\nphysics_setup_cloth\nphysics_setup_spring_chain\nphysics_simulate_cloth\nphysics_status\nphysics_verify_primary_integrity'.splitlines()
)

M8_WRITERS = frozenset({'physics_create_candidate', 'physics_prepare_colliders', 'physics_setup_cloth',
    'physics_simulate_cloth', 'physics_generate_hair_secondary', 'physics_setup_spring_chain',
    'physics_generate_secondary_bones', 'physics_generate_accessory_motion', 'physics_generate_soft_secondary',
    'physics_bake_cache', 'physics_bake_secondary_motion', 'physics_render_preview', 'physics_review',
    'physics_refine', 'physics_accept', 'physics_reject'})
M8_FRAME_READERS = frozenset({'physics_inspect_character', 'physics_classify_secondary',
    'physics_compatibility_report', 'physics_analyze_cloth', 'physics_analyze_hair', 'physics_analyze_accessory',
    'physics_analyze_soft_region', 'physics_verify_primary_integrity', 'physics_analyze_penetration',
    'physics_collision_report', 'physics_analyze_motion', 'physics_analyze_stability'})


@pytest.fixture(scope='module')
def inventory():
    return {listed.name: listed for listed in asyncio.run(server.mcp.list_tools())}


def test_exact_baseline_retained_and_all_35_m8_tools_registered(inventory):
    assert len(BASELINE_NAMES) == 201 and len(M8_NAMES) == 35
    assert BASELINE_NAMES <= set(inventory), sorted(BASELINE_NAMES-set(inventory))
    expected = {'production.'+name for name in M8_NAMES}
    # M9 extends the public catalogue; retain every exact M8 identity and contract.
    from blender_mcp.production.materials.tools import NAMES as M9_NAMES
    extension = {'production.' + name for name in M9_NAMES}
    assert not (BASELINE_NAMES | expected) & extension
    assert set(inventory) == BASELINE_NAMES | expected | extension
    assert len(set(inventory) - extension) == 236


@pytest.mark.parametrize('name', sorted(M8_NAMES))
def test_m8_structured_uniform_input_and_output_contract(name, inventory):
    listed=inventory['production.'+name]
    assert listed.outputSchema is not None
    properties=listed.inputSchema['properties']
    assert {'object_name','physics','intent','brief','options','apply'} <= set(properties)
    assert properties['apply']['default'] is False
    assert '_operation_id' not in properties
    assert listed.annotations.openWorldHint is False
    assert listed.annotations.destructiveHint is False
    assert listed.meta['production']['scene_effects'] in ('none','transient_restored','persistent_or_artifacts')
    assert listed.meta['production']['accepts_operation_id'] is ('operation_id' in properties)
    if listed.annotations.readOnlyHint:
        assert listed.annotations.idempotentHint is True
        assert listed.meta['production']['scene_effects']=='none'


@pytest.mark.parametrize('name', sorted(M8_WRITERS))
def test_persistent_mutations_and_artifacts_keep_operation_keys(name, inventory):
    listed=inventory['production.'+name]
    assert listed.annotations.readOnlyHint is False
    assert 'operation_id' in listed.inputSchema['properties']
    assert listed.meta['production']['scene_effects']=='persistent_or_artifacts'


@pytest.mark.parametrize('name', sorted(M8_FRAME_READERS))
def test_sampling_and_primary_rna_probes_are_not_advertised_readonly(name, inventory):
    listed=inventory['production.'+name]
    assert listed.annotations.readOnlyHint is False
    assert listed.meta['production']['scene_effects'] in ('transient_restored','persistent_or_artifacts')


def test_physics_brief_is_pure_and_defaults_apply_false(inventory):
    listed=inventory['production.physics_create_brief']
    assert listed.annotations.readOnlyHint is True
    assert listed.meta['production']['scene_effects']=='none'
    assert 'operation_id' not in listed.inputSchema['properties']


def test_mcp_runner_requires_actual_feedback_and_never_autofills_acceptance():
    path=Path(__file__).resolve().parents[1]/'integration'/'run_m8_mcp.py'
    module=ast.parse(path.read_text(encoding='utf-8'))
    functions={n.name:n for n in module.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    validator=ast.get_source_segment(path.read_text(encoding='utf-8'),functions['validate_feedback'])
    assert "feedback['temporal_reviewed'] is not True" in validator
    assert "feedback['artifact_reference'] != artifact_reference" in validator
    review=ast.get_source_segment(path.read_text(encoding='utf-8'),functions['visual_review'])
    assert "'temporal_reviewed': False" in review and "'decision': ''" in review
    assert 'REAL_VISUAL_REVIEW_PENDING' in review


@pytest.mark.parametrize('changes', [
    {'reviewer':''}, {'temporal_reviewed':False}, {'views_reviewed':['FRONT']},
    {'speeds_reviewed':[1]}, {'artifact_reference':'different-preview'},
    {'observations':[]}, {'artistic_observations':[]}, {'decision':'UNKNOWN'},
])
def test_visual_feedback_contract_rejects_missing_or_stale_evidence(changes):
    # Synthetic contract input, never a stored review or a claim of visual QA.
    path=Path(__file__).resolve().parents[1]/'integration'/'run_m8_mcp.py'
    function=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                  if isinstance(n,ast.FunctionDef) and n.name=='validate_feedback')
    ns={'VIEWS':['FRONT','SIDE','THREE_QUARTER']}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<feedback-contract>', 'exec'),ns)
    values={'reviewer':'Unit contract validator','observations':['Contract-only input'], 'findings':[],
        'decision':'REJECT','artifact_reference':'contract-preview','temporal_reviewed':True,
        'views_reviewed':ns['VIEWS'],'speeds_reviewed':[1,.25],
        'technical_findings':[],'artistic_observations':['Contract-only input']}
    values.update(changes)
    with pytest.raises(ValueError):
        ns['validate_feedback'](values,'contract-preview')


def test_runner_file_hashes_accept_json_strings_and_path_objects(tmp_path):
    from blender_mcp.production.common.storage import sha256 as shared_sha256
    path=Path(__file__).resolve().parents[1]/'integration'/'run_m8_mcp.py'
    function=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                  if isinstance(n,ast.FunctionDef) and n.name=='sha256')
    ns={'Path':Path,'sha256_file':shared_sha256}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<runner-hash-contract>','exec'),ns)
    source=tmp_path/'archivo con á.txt';source.write_bytes(b'unchanged fixture bytes')
    assert ns['sha256'](str(source))==ns['sha256'](source)==shared_sha256(source)


def test_builder_failure_is_reported_before_gui_or_socket_start(tmp_path):
    from types import SimpleNamespace as NS
    from uuid import uuid4
    import shutil
    import traceback
    path=Path(__file__).resolve().parents[1]/'integration'/'run_m8_mcp.py'
    selected=[n for n in ast.parse(path.read_text(encoding='utf-8')).body
              if isinstance(n,ast.FunctionDef) and n.name in {'main','_json','record_failure'}]
    class Parser:
        def add_argument(self,*args,**kwargs):pass
        def parse_args(self):
            return NS(blender='no-process',port=9877,fps=30,duration=2,preview_size=192,review_timeout=1,only_demo=None)
    class Reporter:
        def __init__(self,folder):
            self.folder=folder;self.report={'status':'running','checks':{},'calls':[]}
        def save(self):
            (self.folder/'report.json').write_text(json.dumps(self.report),encoding='utf-8')
        def check(self,name,passed,**details):
            self.report['checks'][name]={'passed':bool(passed),**details};self.save()
    calls=[]
    def failed_build(*args,**kwargs):
        calls.append(args);raise RuntimeError('controlled builder failure')
    def forbidden_gui(*args,**kwargs):
        raise AssertionError('GUI must never start after a failed builder')
    ns={'Path':Path,'json':json,'uuid4':uuid4,'traceback':traceback,'shutil':shutil,
        'argparse':NS(ArgumentParser=Parser),'tempfile':NS(gettempdir=lambda:str(tmp_path)),
        'subprocess':NS(run=failed_build,Popen=forbidden_gui),'hidden_startup':lambda:None,
        'listening':lambda port:False,'ROOT':tmp_path/'archive','M8Run':Reporter,
        'DEMOS':[('cloth_demo','cloth_heavy')],'BUILD':'unused in mocked builder'}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'<runner-builder-failure>','exec'),ns)
    with pytest.raises(SystemExit) as result:
        ns['main']()
    assert result.value.code==1 and len(calls)==1
    folder=next(tmp_path.glob('bpm8-*'))
    report=json.loads((folder/'report.json').read_text())
    failure=json.loads((folder/'runner-failure.json').read_text())
    assert report['status']=='fail' and report['failure_phase']=='BUILD_FIXTURES'
    assert failure['blender_gui_started'] is False and report['calls']==[]
    assert 'controlled builder failure' in failure['exception']
