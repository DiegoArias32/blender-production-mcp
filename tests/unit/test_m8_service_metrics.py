"""M8 observed QA authority and frozen geometric guard contracts, without Blender.

Host solver predictions deliberately disagree with evaluated measurements below.
These tests protect the distinction, rather than validating a prediction by
comparing it to its own formulas. Runtime mask tests use real group memberships
on small doubles; they do not pretend to qualify Blender physics or rendering.
"""
import ast
from copy import deepcopy
import math
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.physics import model, procedural, service


def context(kind='SPRING_BONE', mesh_chain=False):
    brief = model.create_brief(brief={'subsystem': kind, 'duration_s': 1,
             **({'region': 'AuthorizedLobe', 'region_authorized': True} if kind == 'SOFT' else {})})
    chain = {'armature': 'SourceRig', 'bones': ['SecondaryA', 'SecondaryB']}
    has_chain = kind in {'SPRING_BONE','HAIR','CHAIN','CABLE'}
    plan = {'generation_id': 'a'*32, 'object_name': 'SourceMesh' if mesh_chain or not has_chain else 'SourceRig',
            'brief': brief, 'source_status': {'fingerprint': 'original'},
            'params': {'source_object': 'SourceMesh' if mesh_chain or not has_chain else 'SourceRig',
                       'colliders': ['Body'], 'anchor_object': 'SourceRig', 'anchor_bone': 'Attachment',
                       'pin_group': 'ExplicitPins'},
            'guard_selection': {'anchor_indices': [0,4], 'root_indices':[0,1,18,19,36,37],
                                'outside_region_indices': [1,3,8]}}
    if has_chain:
        plan['params']['chain'] = chain
    candidate_rig = 'CopiedRig' if has_chain else None
    candidate_name = 'CopiedMesh' if mesh_chain or not has_chain else 'CopiedRig'
    state = {'state': 'SECONDARY_CANDIDATE', 'candidate': {'candidate_object': candidate_name,
             'candidate_rig': candidate_rig, 'chain': dict(chain, armature='CopiedRig') if has_chain else None},
             'current_settings': deepcopy(brief['settings']),
             'colliders': {'collider_names': ['OwnedBody'], 'collider_collection_name': 'OwnedColliders'},
             'render': {'player': 'old-preview'}, 'review': {'old': True},
             'solver_metrics': {'max_displacement_m': .00001, 'max_velocity_m_s': .00002,
                               'max_acceleration_m_s2': .00003, 'delay_s': .12,
                               'oscillation_period_s': .75, 'finite': True}}
    return plan, state


@pytest.fixture
def qualify_harness(monkeypatch):
    reports = {'motion': {'passed': True, 'metrics': {'max_displacement_m': .045,
                  'max_velocity_m_s': 2.5, 'max_acceleration_m_s2': 90,
                  'max_secondary_displacement_m': .045, 'max_secondary_velocity_m_s': 1.9,
                  'max_secondary_acceleration_m_s2': 65, 'delay_s': None}, 'errors': []},
               'collision': {'passed': True, 'metrics': {'max_penetration_depth_m': 0,
                  'penetration_vertex_frame_pairs': 0}, 'errors': []},
               'primary': {'passed': True, 'body_and_face_unchanged': True}}
    monkeypatch.setattr(service, 'analyze', lambda bridge, plan, state, kind: deepcopy(reports[kind]))
    monkeypatch.setattr(service, 'verify_primary', lambda bridge, plan: deepcopy(reports['primary']))
    saved = []
    monkeypatch.setattr(service, 'refresh_candidate', lambda bridge, plan, state: None)
    monkeypatch.setattr(service, 'event', lambda path, state: saved.append(deepcopy(state)))
    return reports, saved


def test_evaluated_motion_metrics_remain_authoritative_when_host_predicts_less(qualify_harness):
    reports, saved = qualify_harness
    plan, state = context()
    result = service.qualify(None,None,Path('plan.json'),plan,state)
    observed = result['technical_qa']['metrics']
    assert observed['max_displacement_m'] == .045
    assert observed['max_velocity_m_s'] == 2.5
    assert observed['max_acceleration_m_s2'] == 90
    assert observed['max_secondary_velocity_m_s'] == 1.9
    assert observed['delay_s'] is None
    assert 'oscillation_period_s' not in observed
    assert result['technical_qa']['solver_predictions']['oscillation_period_s'] == .75
    assert result['technical_qa']['solver_predictions']['max_velocity_m_s'] == .00002
    assert result['technical_qa']['visual_verification'] == 'required'
    assert result['state'] == 'SIMULATED'
    assert saved[-1]['technical_qa']['metrics'] == observed


def test_prediction_record_is_independent_of_mutable_solver_state(qualify_harness):
    plan, state = context()
    state['solver_metrics']['measurement_status'] = {'delay_s': 'UNKNOWN'}
    result = service.qualify(None,None,Path('plan.json'),plan,state)
    state['solver_metrics']['measurement_status']['delay_s'] = 'forged-later'
    assert result['technical_qa']['solver_predictions']['measurement_status']['delay_s'] == 'UNKNOWN'


@pytest.mark.parametrize('failed_gate', ['motion','collision','primary'])
def test_safe_host_predictions_cannot_accept_a_failed_observed_gate(qualify_harness, failed_gate):
    reports, saved = qualify_harness
    reports[failed_gate]['passed'] = False
    if failed_gate != 'primary':
        reports[failed_gate]['errors'] = ['ACTUAL_EVALUATED_BOUND_EXCEEDED']
    plan, state = context()
    result = service.qualify(None,None,Path('plan.json'),plan,state)
    assert result['technical_qa']['passed'] is False
    assert result['state'] == 'SECONDARY_CANDIDATE'
    assert result['render'] is None and result['review'] is None
    assert saved[-1]['state'] != 'SIMULATED'


@pytest.mark.parametrize('kind', ['HAIR','SPRING_BONE','CHAIN','CABLE','ACCESSORY','SOFT'])
def test_actual_geometry_qa_uses_current_hard_displacement_bound_after_refinement(kind):
    plan, state = context(kind)
    original = plan['brief']['settings']['max_displacement_m']
    state['current_settings']['max_displacement_m'] = original/3
    params = service.qa_params(plan,state)
    assert params['secondary_displacement_limit_m'] == original/3
    assert params['source_name'] == plan['object_name']
    assert params['frames'] == list(range(1,32))


@pytest.mark.parametrize('kind', ['HAIR','SPRING_BONE','CHAIN','CABLE','ACCESSORY'])
def test_actual_orientation_qa_receives_declared_current_angle_constraint(kind):
    plan, state = context(kind)
    state['current_settings']['max_angle_deg'] = 3.5
    params = service.qa_params(plan,state)
    assert params['secondary_angle_limit_deg'] == 3.5


def test_cloth_qa_uses_full_pin_membership_frozen_in_the_plan():
    plan, state = context('CLOTH')
    params = service.qa_params(plan,state)
    assert params['anchor_indices'] == [0,4]
    assert params['source_name'] == 'SourceMesh'
    assert 'secondary_displacement_limit_m' not in params
    assert params['thresholds']['anchor_detachment_m'] == plan['brief']['qa']['max_root_detachment_m']


def test_soft_qa_receives_real_protected_membership_and_the_vector_bound():
    plan, state = context('SOFT')
    params = service.qa_params(plan,state)
    assert params['outside_region_indices'] == [1,3,8]
    assert params['secondary_displacement_limit_m'] == state['current_settings']['max_displacement_m']
    assert 'secondary_angle_limit_deg' not in params
    assert params['source_name'] == 'SourceMesh'


def test_accessory_anchor_is_checked_against_same_frame_source_origin():
    plan, state = context('ACCESSORY')
    params = service.qa_params(plan,state)
    assert params['anchor_mode'] == 'OBJECT_ORIGIN'
    assert params['source_name'] == 'SourceMesh'
    assert params['anchor_object'] == 'SourceRig'
    assert params['anchor_bone'] == 'Attachment'


def test_hair_mesh_qa_preserves_separate_source_and_candidate_rig_mapping():
    plan, state = context('HAIR',mesh_chain=True)
    params = service.qa_params(plan,state)
    assert params['source_name'] == 'SourceMesh'
    assert params['source_chain']['armature'] == 'SourceRig'
    assert params['chain']['armature'] == 'CopiedRig'
    assert params['bone_names'] == ['SecondaryA','SecondaryB']
    assert params['root_indices'] == [0,1,18,19,36,37]


def test_armature_qa_compares_copied_chain_to_original_chain_without_name_guessing():
    plan, state = context('SPRING_BONE')
    params = service.qa_params(plan,state)
    assert params['source_name'] == 'SourceRig'
    assert params['candidate_name'] == 'CopiedRig'
    assert params['bone_names'] == plan['params']['chain']['bones']


def test_qa_summary_reports_worst_minimum_area_and_worst_maximum_stretch():
    result = {'passed': False, 'frames': [
        {'frame': 1, 'passed': True, 'metrics': {'min_area_ratio': 1, 'max_edge_stretch_ratio': 1}},
        {'frame': 2, 'passed': False, 'errors': ['MIN_AREA_RATIO_BELOW_THRESHOLD'],
         'metrics': {'min_area_ratio': .002, 'max_edge_stretch_ratio': 1.4}},
        {'frame': 3, 'passed': True, 'metrics': {'min_area_ratio': .8, 'max_edge_stretch_ratio': 1.1}},
    ]}
    summary = service.summarize_qa(result)
    assert summary['metrics']['min_area_ratio'] == .002
    assert summary['metrics']['max_edge_stretch_ratio'] == 1.4
    assert summary['passed'] is False
    assert summary['errors'] == ['MIN_AREA_RATIO_BELOW_THRESHOLD']


def mask_runtime(source):
    wanted = {'px_fail','pp_guard_selection'}
    nodes = [n for n in ast.parse(procedural.RUNTIME).body if isinstance(n,ast.FunctionDef) and n.name in wanted]
    ns = {'px_object': lambda name: source}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'<m8_mask_guard>','exec'),ns)
    return ns['pp_guard_selection']


def masked_mesh(weights=(1,.5,0,0), modifier='ARMATURE', exists=True):
    vertices = [NS(groups=[NS(group=7,weight=w)] if w else []) for w in weights]
    return NS(type='MESH', data=NS(vertices=vertices), modifiers=[NS(type=modifier)],
              vertex_groups={'DeclaredMask':NS(index=7)} if exists else {})


def test_pin_mask_guards_only_full_weight_attachment_and_leaves_dynamic_vertices():
    guard = mask_runtime(masked_mesh())
    result = guard({'source_object':'Fixture','subsystem':'CLOTH','pin_group':'DeclaredMask'})
    assert result['anchor_indices'] == [0]
    assert result['outside_region_indices'] == []
    assert result['membership_source'] == 'explicit_source_vertex_group'


def test_soft_mask_protects_every_zero_weight_vertex_and_keeps_partial_falloff_selected():
    guard = mask_runtime(masked_mesh())
    result = guard({'source_object':'Fixture','subsystem':'SOFT','region':'DeclaredMask'})
    assert result['outside_region_indices'] == [2,3]
    assert result['anchor_indices'] == []


@pytest.mark.parametrize('kind',['HAIR','SPRING_BONE','CHAIN','CABLE'])
def test_bound_secondary_mesh_qa_has_frozen_full_weight_root_membership(kind):
    guard = mask_runtime(masked_mesh())
    result = guard({'source_object':'Fixture','subsystem':kind,'pin_group':'DeclaredMask'})
    assert result['root_indices'] == [0]
    assert result['anchor_indices'] == []
    assert result['outside_region_indices'] == []


@pytest.mark.parametrize('kind',['HAIR','SPRING_BONE','CHAIN','CABLE'])
def test_bound_secondary_mesh_cannot_omit_or_forge_full_root_membership(kind):
    guard = mask_runtime(masked_mesh((.2,.5,0)))
    with pytest.raises(ValueError,match='SECONDARY_FULL_ROOT_REQUIRED'):
        guard({'source_object':'Fixture','subsystem':kind,'pin_group':'DeclaredMask'})


@pytest.mark.parametrize('modifier', ['SMOOTH','CORRECTIVE_SMOOTH','SUBSURF','MIRROR','NODES','SOLIDIFY'])
@pytest.mark.parametrize('kind', ['CLOTH','SOFT'])
def test_neighbor_or_topology_modifiers_reject_before_any_candidate_mask_write(modifier,kind):
    guard = mask_runtime(masked_mesh(modifier=modifier))
    with pytest.raises(ValueError,match='PHYSICS_MASK_MODIFIER_UNSUPPORTED'):
        guard({'source_object':'Fixture','subsystem':kind,'pin_group':'DeclaredMask','region':'DeclaredMask'})


@pytest.mark.parametrize('weights', [(0,0,0),(1,.5,1),(1,1,1)])
@pytest.mark.parametrize('kind', ['CLOTH','SOFT'])
def test_empty_or_whole_body_masks_are_rejected(weights,kind):
    guard = mask_runtime(masked_mesh(weights))
    with pytest.raises(ValueError,match='PHYSICS_MASK_INVALID'):
        guard({'source_object':'Fixture','subsystem':kind,'pin_group':'DeclaredMask','region':'DeclaredMask'})


def test_partial_cloth_pin_weights_cannot_satisfy_a_fixed_anchor_gate():
    guard = mask_runtime(masked_mesh((.4,.5,0)))
    with pytest.raises(ValueError,match='CLOTH_FULL_PIN_REQUIRED'):
        guard({'source_object':'Fixture','subsystem':'CLOTH','pin_group':'DeclaredMask'})


def test_mask_preflight_payload_passes_existing_safe_mode_without_filesystem_or_mutation():
    # A group name containing syntax remains JSON data in the validated payload.
    code = build_code(procedural.GUARD_SELECTION, {'source_object':'Fixture', 'subsystem':'SOFT',
                                                  'region':'DeclaredMask"; __import__("os")'})
    ast.parse(code)
    assert 'PHYSICS_MASK_MODIFIER_UNSUPPORTED' in code


class StopBeforeCandidateCopy(RuntimeError):
    pass


def solver_preflight_harness(monkeypatch,kind,data):
    plan,state = context(kind)
    state['state'] = 'PLANNED'
    observed = {}
    monkeypatch.setattr(service,'fresh',lambda *args,**kwargs: {})
    monkeypatch.setattr(service,'prepare_colliders',lambda *args,**kwargs: (_ for _ in ()).throw(StopBeforeCandidateCopy()))
    def solve(driver,settings,fps,solver_kind=None):
        observed['settings'] = deepcopy(settings)
        observed['fps'] = fps
        return {'metrics':{}}
    monkeypatch.setattr(service.spring,'solve',solve)
    monkeypatch.setattr(service.spring,'solve_chain',lambda driver,lengths,fps,settings: solve(driver,settings,fps))
    class Bridge:
        def run(self,body,params):
            assert body == procedural.SAMPLES
            assert params['subsystem'] == kind
            return deepcopy(data)
    return Bridge(),plan,state,observed


def sampled_driver():
    return [{'frame':i,'position':[0,0,0],'quaternion':[1,0,0,0]} for i in range(1,32)]


@pytest.mark.parametrize('radius',[.02,.1,.5,2,10])
def test_accessory_preflight_angle_cap_bounds_every_surface_point_at_measured_radius(monkeypatch,radius):
    data={'samples':sampled_driver(),'lengths':[],'maximum_object_radius_m':radius}
    bridge,plan,state,observed = solver_preflight_harness(monkeypatch,'ACCESSORY',data)
    public_settings = deepcopy(state['current_settings'])
    with pytest.raises(StopBeforeCandidateCopy):
        service.generate_procedural(bridge,None,Path('plan.json'),plan,state,'radius-preflight')
    internal=observed['settings']
    angle=math.radians(internal['max_angle_deg'])
    assert internal['max_angle_deg'] <= public_settings['max_angle_deg']
    # The worst point on the sphere moves along a chord; this is a geometry
    # bound independent of the spring integrator's particular response.
    assert 2*radius*math.sin(angle/2) <= public_settings['max_displacement_m']+1e-12
    assert state['current_settings'] == public_settings
    assert service.qa_params(plan,state)['secondary_displacement_limit_m'] == public_settings['max_displacement_m']
    assert service.qa_params(plan,state)['secondary_angle_limit_deg'] == public_settings['max_angle_deg']


@pytest.mark.parametrize('radius',[True,False,0,-1,float('nan'),float('inf'),'0.5',None])
def test_accessory_invalid_geometry_radius_rejects_before_solver_or_copy(monkeypatch,radius):
    data={'samples':sampled_driver(),'lengths':[],'maximum_object_radius_m':radius}
    bridge,plan,state,observed = solver_preflight_harness(monkeypatch,'ACCESSORY',data)
    with pytest.raises(ValueError,match='ACCESSORY_GEOMETRY_RADIUS_REQUIRED'):
        service.generate_procedural(bridge,None,Path('plan.json'),plan,state,'invalid-radius')
    assert observed == {}


@pytest.mark.parametrize('ratio',[.1,.5,1])
def test_chain_mesh_conservative_rest_lever_budget_keeps_public_limits_unchanged(monkeypatch,ratio):
    data={'samples':sampled_driver(),'lengths':[.2,.2],'mesh_displacement_budget_ratio':ratio}
    bridge,plan,state,observed = solver_preflight_harness(monkeypatch,'HAIR',data)
    public_limit=state['current_settings']['max_displacement_m']
    with pytest.raises(StopBeforeCandidateCopy):
        service.generate_procedural(bridge,None,Path('plan.json'),plan,state,'lever-budget')
    assert observed['settings']['max_displacement_m'] == pytest.approx(public_limit*ratio)
    assert state['current_settings']['max_displacement_m'] == public_limit
    assert service.qa_params(plan,state)['secondary_displacement_limit_m'] == public_limit


@pytest.mark.parametrize('ratio',[True,0,-.1,1.01,float('nan'),float('inf'),'0.5'])
def test_invalid_chain_mesh_budget_rejects_before_solver_or_copy(monkeypatch,ratio):
    data={'samples':sampled_driver(),'lengths':[.2,.2],'mesh_displacement_budget_ratio':ratio}
    bridge,plan,state,observed = solver_preflight_harness(monkeypatch,'HAIR',data)
    with pytest.raises(ValueError,match='SECONDARY_MESH_BUDGET_INVALID'):
        service.generate_procedural(bridge,None,Path('plan.json'),plan,state,'invalid-lever-budget')
    assert observed == {}
