"""Negative QA gates and real source-signature algorithms, without Blender."""
import copy
import ast
import hashlib
import json
import math
from types import SimpleNamespace as NS

import pytest

from blender_mcp.production.animation.generation.integrity import INTEGRITY_RUNTIME
from blender_mcp.production.animation.generation.candidate import CandidateBridge
from blender_mcp.production.animation.generation.qa import ROOT_LOOP_READ, _thresholds, evaluate_gates
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.m3_transaction import BASE_RUNTIME


def reports():
    return {
        'animation_analyze_contacts': {'status': 'measured', 'metrics': {'contact_count': 2, 'max_sliding_m': .001}},
        'animation_detect_limb_pops': {'metrics': {'pop_count': 0, 'max_length_ratio': 1.}},
        'animation_detect_quaternion_flips': {'metrics': {key: 0 for key in ('invalid_quaternion_count', 'incomplete_group_count',
            'sign_discontinuity_count', 'orientation_jump_count')}},
        'animation_deformation_qa': {'status': 'ok', 'state_restored': True,
            'metrics': {'meshes': 1, 'evaluations': 12, 'sampled_frames': 12, 'issue_count': 0}},
        'generation_root_loop': {'status': 'measured', 'metrics': {'root_mode': 'ROOT_MOTION',
            'root_displacement_error_m': 0., 'root_orientation_error_deg': 0., 'root_max_translation_m': 1.,
            'root_max_step_m': .05, 'root_unexpected_vertical_m': 0., 'loop_required': True,
            'loop_position_error_m': 0., 'loop_orientation_error_deg': 0., 'loop_velocity_error_m_s': 0.,
            'loop_angular_velocity_error_deg_s': 0., 'intentional_stepped_blocking': False}},
    }


def test_measured_continuous_candidate_passes_all_numeric_gates():
    assert all(evaluate_gates(reports(), _thresholds({}, 0.)).values())


@pytest.mark.parametrize('counter', ['invalid_quaternion_count', 'incomplete_group_count', 'sign_discontinuity_count', 'orientation_jump_count'])
def test_quaternion_problem_without_top_level_error_never_passes(counter):
    data = reports()
    data['animation_detect_quaternion_flips']['metrics'][counter] = 1
    assert not evaluate_gates(data, _thresholds({}, 0.))['no_quaternion_error']


@pytest.mark.parametrize('change,gate', [
    ({'root_displacement_error_m': .03}, 'root_displacement_matches_intent'),
    ({'root_orientation_error_deg': 12.}, 'root_rotation_matches_intent'),
    ({'root_mode': 'IN_PLACE'}, 'in_place_translation'),
    ({'root_max_step_m': 2.}, 'no_root_teleport'),
    ({'root_unexpected_vertical_m': .25}, 'no_unexpected_root_vertical'),
    ({'loop_position_error_m': .1}, 'loop_endpoint_position'),
    ({'loop_orientation_error_deg': 6.}, 'loop_endpoint_rotation'),
    ({'loop_velocity_error_m_s': .4}, 'loop_boundary_velocity'),
    ({'loop_angular_velocity_error_deg_s': 30.}, 'loop_boundary_angular_velocity'),
])
def test_wrong_root_or_loop_cannot_hide_behind_other_passing_analyzers(change, gate):
    data = reports()
    data['generation_root_loop']['metrics'].update(change)
    assert not evaluate_gates(data, _thresholds({}, 0.))[gate]


def test_returned_analyzer_error_and_missing_measurement_fail_completion():
    data = reports()
    data['animation_analyze_bezier'] = {'status': 'error', 'errors': ['Unable to evaluate']}
    assert not evaluate_gates(data, _thresholds({}, 0.))['analyzers_completed']
    data = reports(); del data['generation_root_loop']
    assert not all(evaluate_gates(data, _thresholds({}, 0.)).values())


@pytest.mark.parametrize('changes', [{'issue_count': 1}, {'meshes': 0}, {'evaluations': 0}, {'sampled_frames': 0}])
def test_severe_deformation_or_missing_surface_evidence_fails(changes):
    data = reports(); data['animation_deformation_qa']['metrics'].update(changes)
    assert not evaluate_gates(data, _thresholds({}, 0.))['deformation_measured']


def test_stepped_blocking_only_exempts_declared_discrete_jumps():
    data = reports(); root = data['generation_root_loop']['metrics']
    root.update(intentional_stepped_blocking=True, root_max_step_m=3.)
    data['animation_detect_limb_pops']['metrics']['pop_count'] = 4
    data['animation_detect_quaternion_flips']['metrics']['orientation_jump_count'] = 2
    assert all(evaluate_gates(data, _thresholds({}, 0.)).values())
    data['animation_detect_quaternion_flips']['metrics']['sign_discontinuity_count'] = 1
    assert not evaluate_gates(data, _thresholds({}, 0.))['no_quaternion_error']
    data['animation_deformation_qa']['metrics']['issue_count'] = 1
    assert not evaluate_gates(data, _thresholds({}, 0.))['deformation_measured']


def test_measured_constant_blocking_loop_c1_is_advisory_but_c0_required():
    data = reports(); root = data['generation_root_loop']['metrics']
    root.update(intentional_stepped_blocking=True, loop_velocity_error_m_s=4.719,
                loop_angular_velocity_error_deg_s=524.475)
    assert all(evaluate_gates(data, _thresholds({}, 0.)).values())
    root['loop_position_error_m'] = .02
    assert not evaluate_gates(data, _thresholds({}, 0.))['loop_endpoint_position']
    root['loop_position_error_m'] = 0.
    root['loop_orientation_error_deg'] = 2.
    assert not evaluate_gates(data, _thresholds({}, 0.))['loop_endpoint_rotation']


@pytest.mark.parametrize('value', [None, math.nan, math.inf, -1.])
def test_blocking_loop_c1_still_requires_finite_nonnegative_measurements(value):
    data = reports(); root = data['generation_root_loop']['metrics']
    root.update(intentional_stepped_blocking=True, loop_velocity_error_m_s=value,
                loop_angular_velocity_error_deg_s=value)
    gates = evaluate_gates(data, _thresholds({}, 0.))
    assert not gates['loop_boundary_velocity'] and not gates['loop_boundary_angular_velocity']


def test_real_measured_continuous_walk_boundary_angular_error_is_not_exempt():
    data = reports(); root = data['generation_root_loop']['metrics']
    root.update(intentional_stepped_blocking=False, loop_velocity_error_m_s=.083859867,
                loop_angular_velocity_error_deg_s=19.459088)
    gates = evaluate_gates(data, _thresholds({}, 0.))
    assert gates['loop_boundary_velocity'] and not gates['loop_boundary_angular_velocity']


def test_nonloop_attack_does_not_require_cyclic_pose_but_still_requires_root():
    data = reports(); data['generation_root_loop']['metrics'].update(loop_required=False, loop_position_error_m=1.)
    gates = evaluate_gates(data, _thresholds({}, 0.))
    assert gates['loop_endpoint_position'] and all(gates.values())
    data['generation_root_loop']['metrics']['root_displacement_error_m'] = .3
    assert not all(evaluate_gates(data, _thresholds({}, 0.)).values())


@pytest.mark.parametrize('value', [True, -1., math.nan, math.inf])
def test_invalid_qa_thresholds_rejected_before_sampling(value):
    with pytest.raises(ValueError, match='INVALID_QA_THRESHOLD'):
        _thresholds({'loop_velocity_tolerance_m_s': value}, 0.)


def test_thresholds_are_explicit_and_configurable():
    data = reports(); data['generation_root_loop']['metrics']['loop_velocity_error_m_s'] = .12
    assert not evaluate_gates(data, _thresholds({}, 0.))['loop_boundary_velocity']
    assert evaluate_gates(data, _thresholds({'loop_velocity_tolerance_m_s': .15}, 0.))['loop_boundary_velocity']


class RNA(NS):
    def path_resolve(self, key):
        return getattr(self, key)


def prop(name, kind, readonly=False):
    return NS(identifier=name, type=kind, is_readonly=readonly)


def schema(identifier='Struct', properties=()):
    return NS(identifier=identifier, properties=list(properties))


def source_context():
    identity = [[float(i == j) for j in range(4)] for i in range(4)]
    bone = RNA(name='Root', parent=None, use_deform=True, envelope_distance=.25,
               bl_rna=schema('Bone', [prop('use_deform', 'BOOLEAN'), prop('envelope_distance', 'FLOAT')]))
    rig = RNA(name='Rig', type='ARMATURE', data=NS(bones=[bone]), bl_rna=schema('Object'))
    socket = NS(name='Color', identifier='Color', bl_idname='NodeSocketColor', default_value=[.2, .3, .4, 1.])
    node = RNA(name='Shader', type='BSDF_PRINCIPLED', bl_idname='ShaderNodeBsdfPrincipled', inputs=[socket], outputs=[],
               bl_rna=schema('ShaderNodeBsdfPrincipled'))
    tree = NS(nodes=[node], links=[], animation_data=None)
    material = RNA(name='Material', library=None, override_library=None, animation_data=None, use_nodes=True, node_tree=tree,
                   diffuse_color=[.2, .3, .4, 1.], bl_rna=schema('Material', [prop('diffuse_color', 'FLOAT'), prop('use_nodes', 'BOOLEAN')]))
    verts = [NS(co=[0., 0., 0.], groups=[NS(group=0, weight=1.)]), NS(co=[1., 0., 0.], groups=[NS(group=0, weight=1.)]),
             NS(co=[0., 1., 0.], groups=[NS(group=0, weight=1.)])]
    basis = NS(name='Basis', relative_key=None, value=0., mute=False, vertex_group='', interpolation='KEY_LINEAR',
               slider_min=0., slider_max=1., data=[NS(co=list(v.co)) for v in verts])
    shape = copy.deepcopy(basis); shape.name='Shape'; shape.relative_key=basis
    keys = NS(use_relative=True, eval_time=0., key_blocks=[basis, shape], animation_data=None)
    data = NS(name='Mesh', library=None, override_library=None, animation_data=None, vertices=verts,
              edges=[NS(vertices=[0, 1])], polygons=[NS(vertices=[0, 1, 2], material_index=0, use_smooth=True)],
              loops=[NS(), NS(), NS()], attributes=[], shape_keys=keys)
    modifier = RNA(name='Armature', type='ARMATURE', object=rig, show_viewport=True,
                   bl_rna=schema('ArmatureModifier', [prop('object', 'POINTER'), prop('show_viewport', 'BOOLEAN')]))
    mesh = RNA(name='Character', type='MESH', library=None, override_library=None, data=data, animation_data=None,
               constraints=[], parent=rig, parent_type='OBJECT', parent_bone='', matrix_basis=copy.deepcopy(identity),
               matrix_parent_inverse=copy.deepcopy(identity), modifiers=[modifier], vertex_groups=[NS(name='Root', index=0, lock_weight=False)],
               material_slots=[NS(link='DATA', material=material)], hide_render=False, hide_viewport=False,
               color=[1., 1., 1., 1.], pass_index=0, hide_get=lambda: False)
    objects = [rig, mesh]
    class Objects(list):
        def get(self, name):
            return next((obj for obj in self if obj.name == name), None)
    scene = NS(objects=Objects(objects), frame_current=1)
    namespace = {'bpy': NS(context=NS(scene=scene)), 'math': math,
                 'm2_digest': lambda payload: hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}
    exec(INTEGRITY_RUNTIME, namespace)
    return rig, mesh, material, namespace


def signature(rig, namespace, options=None):
    return namespace['m6_asset_context'](rig, options or {})


@pytest.mark.parametrize('mutation', ['geometry', 'weight', 'modifier', 'shape', 'material', 'node', 'binding', 'uv'])
def test_source_changes_invalidate_numeric_and_visual_evidence(mutation):
    rig, mesh, material, ns = source_context(); before = signature(rig, ns)['fingerprint']
    if mutation == 'geometry': mesh.data.vertices[0].co[0] = .02
    elif mutation == 'weight': mesh.data.vertices[0].groups[0].weight = .4
    elif mutation == 'modifier': mesh.modifiers[0].show_viewport = False
    elif mutation == 'shape': mesh.data.shape_keys.key_blocks[1].data[0].co[0] = .03
    elif mutation == 'material': material.diffuse_color[0] = .9
    elif mutation == 'node': material.node_tree.nodes[0].inputs[0].default_value[0] = .8
    elif mutation == 'binding': rig.data.bones[0].envelope_distance = .6
    elif mutation == 'uv':
        value = RNA(vector=[.2, .5], bl_rna=schema('Float2AttributeValue', [prop('vector', 'FLOAT')]))
        mesh.data.attributes = [NS(name='UVMap', domain='CORNER', data_type='FLOAT2', data=[value])]
    assert signature(rig, ns)['fingerprint'] != before


def test_pose_or_frame_changes_do_not_invalidate_raw_asset_signature():
    rig, mesh, _, ns = source_context(); before = signature(rig, ns)
    ns['bpy'].context.scene.frame_current = 90
    mesh.matrix_world = [[999] * 4] * 4  # evaluated transform is deliberately not read
    assert signature(rig, ns) == before
    assert set(before) == {'fingerprint', 'meshes', 'vertices', 'scope', 'external_dependencies'}
    assert before['vertices'] == 3 and 'co' not in before


@pytest.mark.parametrize('type_name', ['VALTORGB', 'GROUP', 'TEX_IMAGE', 'CURVE_RGB'])
def test_unhandled_nested_or_external_shader_dependencies_are_not_certified(type_name):
    rig, _, material, ns = source_context(); material.node_tree.nodes[0].type = type_name
    with pytest.raises(ValueError, match='ASSET_CONTEXT_UNSUPPORTED'):
        signature(rig, ns)


def test_dynamic_shapes_and_capacity_reject_without_source_mutation():
    rig, mesh, _, ns = source_context(); original = copy.deepcopy(mesh.data.vertices)
    mesh.data.shape_keys.animation_data = NS(action=NS(name='DrivenShape'), drivers=[], nla_tracks=[])
    with pytest.raises(ValueError, match='animated or driven shape keys'):
        signature(rig, ns)
    mesh.data.shape_keys.animation_data = None
    mesh.data.vertices = [mesh.data.vertices[0]] * 100001
    with pytest.raises(ValueError, match='100000 total vertices'):
        signature(rig, ns)
    assert original[0].co == mesh.data.vertices[0].co


def test_integrity_and_root_loop_payloads_pass_unmodified_upstream_safe_mode():
    integrity = build_code(BASE_RUNTIME + INTEGRITY_RUNTIME + "\nresult=m6_asset_context(m2_object(params['object_name']),params['options'])\n",
                           {'object_name': 'Fixture', 'options': {}})
    root = build_code(BASE_RUNTIME + ROOT_LOOP_READ, {'object_name': 'Fixture'})
    assert len(integrity.encode()) < 200000 and len(root.encode()) < 200000


def test_root_loop_sampler_also_passes_safe_mode_inside_restoring_candidate_bridge():
    class ValidatingBridge:
        def run(self, body, params):
            code = build_code(body, params)
            assert len(code.encode()) < 200000 and 'finally:' in code
            return {'status': 'validated'}
    assert CandidateBridge(ValidatingBridge(), 'Fixture', 'Candidate').run(
        BASE_RUNTIME + ROOT_LOOP_READ, {'object_name': 'Fixture'})['status'] == 'validated'


@pytest.mark.parametrize('forward,expected', [(1., math.pi/2), (-1., -math.pi/2)])
def test_root_turn_expected_rotation_preserves_reflected_forward_basis(forward, expected):
    class Matrix3:
        def __init__(self, rows): self.rows = rows
        def __matmul__(self, other):
            return Matrix3([[sum(self.rows[i][k]*other.rows[k][j] for k in range(3)) for j in range(3)] for i in range(3)])
        def inverted(self): return Matrix3([list(row) for row in zip(*self.rows)])
        def to_quaternion(self): return QuaternionZ(None, math.atan2(self.rows[1][0], self.rows[0][0]))
    class QuaternionZ:
        def __init__(self, axis, angle): self.angle = angle
        def to_matrix(self):
            c, s = math.cos(self.angle), math.sin(self.angle)
            return Matrix3([[c, -s, 0.], [s, c, 0.], [0., 0., 1.]])
        def normalized(self): return self
    function = next(node for node in ast.parse(ROOT_LOOP_READ).body if isinstance(node, ast.FunctionDef)
                    and node.name == 'm6_qa_expected_root_rotation')
    namespace = {'math': math, 'Quaternion': QuaternionZ, 'Vector': tuple}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<root-rotation-helper>', 'exec'), namespace)
    basis = Matrix3([[1., 0., 0.], [0., forward, 0.], [0., 0., 1.]])
    result = namespace['m6_qa_expected_root_rotation'](basis, 90.)
    assert result.angle == pytest.approx(expected)
