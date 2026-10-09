"""M8 payload budgets and complete raw/Action/NLA integrity coverage."""
import ast
import copy
import json
import math
from types import SimpleNamespace as NS

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.digest import runtime_digest
from blender_mcp.production.physics.integrity import RUNTIME, STATUS, PRIMARY, CANDIDATE_STATUS, verify


def helper_namespace(*names):
    tree = ast.parse(RUNTIME)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    selected = [functions[name] for name in names]
    ns = {'math': math, 'm2_digest': runtime_digest, 'bpy': NS(types=NS(ID=type('ID', (), {}), Collection=type('Collection', (), {}))),
          'getattr': getattr}
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<m8-runtime>', 'exec'), ns)
    return ns


def prop(name, kind='FLOAT', readonly=False):
    return NS(identifier=name, type=kind, is_readonly=readonly)


class RNA(NS):
    def path_resolve(self, name):
        return getattr(self, name)


def test_primary_integrity_requires_real_complete_evidence():
    before = {key: 'digest' for key in ('source_fingerprint', 'primary_fingerprint', 'dependency_fingerprint', 'context_fingerprint')}
    assert verify(before, before)['passed']
    for key in before:
        after = dict(before); after[key] = 'changed'
        assert verify(before, after)['changed'] == [key]
    assert not verify({}, {})['passed']
    assert not verify(before, dict(before, primary_fingerprint=None))['primary_preserved']


@pytest.mark.parametrize('body', [STATUS, PRIMARY, CANDIDATE_STATUS], ids=['status', 'primary', 'candidate'])
def test_integrity_payloads_pass_original_safe_mode_with_room_for_backend(body):
    code = build_code(body, {'source_object': 'Fixture', 'primary_objects': ['Body', 'Face'], 'frames': [1, 9]})
    assert len(code.encode()) < 45000
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 9000
    assert 'm3_guard' not in code
    assert 'finally:' in code and 'px_restore' in code


def test_rna_nested_readonly_cloth_settings_are_fingerprinted_cache_status_is_transient():
    ns = helper_namespace('px_fail', 'px_value', 'px_local', 'px_pointer', 'px_rna')
    settings = RNA(mass=.3, bl_rna=NS(properties=[prop('mass')]))
    cache = RNA(frame_start=1, is_baked=False, bl_rna=NS(properties=[prop('frame_start', 'INT'), prop('is_baked', 'BOOLEAN')]))
    modifier = RNA(settings=settings, point_cache=cache, bl_rna=NS(properties=[prop('settings', 'POINTER', True), prop('point_cache', 'POINTER', True)]))
    before = runtime_digest(ns['px_rna'](modifier))
    cache.is_baked = True
    assert runtime_digest(ns['px_rna'](modifier)) == before
    settings.mass = .7
    assert runtime_digest(ns['px_rna'](modifier)) != before
    settings.mass = .3; cache.frame_start = 2
    assert runtime_digest(ns['px_rna'](modifier)) != before


@pytest.mark.parametrize('field', ['co', 'handle_left', 'handle_right', 'interpolation', 'handle_left_type', 'easing'])
def test_action_fingerprint_materially_covers_curve_values_and_handles(field):
    ns = helper_namespace('px_fail', 'px_curve')
    key = NS(co=[1., .2], handle_left=[.5, .1], handle_right=[1.5, .4], interpolation='BEZIER',
        handle_left_type='AUTO', handle_right_type='AUTO', type='KEYFRAME', easing='AUTO', amplitude=.0, back=.0, period=.0)
    curve = NS(data_path='pose.bones["Root"].location', array_index=0, modifiers=[], keyframe_points=[key],
        sampled_points=[], group=None, mute=False, lock=False, extrapolation='CONSTANT')
    before = runtime_digest(ns['px_curve'](curve))
    if field in ('co', 'handle_left', 'handle_right'):
        getattr(key, field)[1] += .001
    else:
        setattr(key, field, 'DIFFERENT')
    assert runtime_digest(ns['px_curve'](curve)) != before


def test_unsupported_fcurve_modifiers_are_rejected_before_snapshot():
    ns = helper_namespace('px_fail', 'px_curve')
    with pytest.raises(ValueError, match='PHYSICS_FCURVE_MODIFIER_UNSUPPORTED'):
        ns['px_curve'](NS(modifiers=[NS(type='NOISE')], data_path='location'))


@pytest.mark.parametrize('mutation', ['shape_coordinate', 'weight', 'topology', 'group', 'material_binding'])
def test_raw_mesh_signature_covers_changes_but_excludes_evaluated_shape_values(mutation):
    ns = helper_namespace('px_fail', 'px_value', 'px_local', 'px_mesh')
    ns['px_animation'] = lambda item: None
    ns['m6_integrity_material'] = lambda item: {'name':item.name} if item else None
    vertices = [NS(co=[0., 0., 0.], groups=[NS(group=0, weight=1.)]), NS(co=[1., 0., 0.], groups=[])]
    basis = NS(name='Basis', relative_key=None, mute=False, vertex_group='', interpolation='KEY_LINEAR',
        slider_min=0., slider_max=1., value=0., data=[NS(co=[0., 0., 0.]), NS(co=[1., 0., 0.])])
    shape = copy.deepcopy(basis); shape.name='Smile'; shape.relative_key=basis
    keys = NS(name='Keys', use_relative=True, key_blocks=[basis, shape], library=None, override_library=None)
    obj = NS(data=NS(vertices=vertices, edges=[NS(vertices=[0, 1])], polygons=[], shape_keys=keys),
        vertex_groups=[NS(name='Pin', index=0, lock_weight=False)], material_slots=[NS(link='DATA', material=NS(name='Fabric'))])
    before = runtime_digest(ns['px_mesh'](obj))
    shape.value = .83
    assert runtime_digest(ns['px_mesh'](obj)) == before
    if mutation == 'shape_coordinate': shape.data[0].co[2] = .03
    elif mutation == 'weight': vertices[0].groups[0].weight = .4
    elif mutation == 'topology': obj.data.edges[0].vertices = [1, 1]
    elif mutation == 'group': obj.vertex_groups[0].name = 'Changed'
    elif mutation == 'material_binding': obj.material_slots[0].material.name = 'Other'
    assert runtime_digest(ns['px_mesh'](obj)) != before


def test_nla_snapshot_includes_controls_and_full_action_references():
    ns = helper_namespace('px_fail', 'px_animation')
    ns['px_action'] = lambda a: {'name': a.name, 'fingerprint': a.fingerprint} if a else None
    ns['px_rna'] = lambda s: {'scale': s.scale, 'repeat': s.repeat, 'influence': s.influence, 'frame_start': s.frame_start}
    ns['px_curve'] = lambda c: {'control': c.value}
    strip = NS(name='Face', type='CLIP', strips=[], fcurves=[NS(value=.2, data_path='influence', array_index=0, keyframe_points=[], sampled_points=[])], action=NS(name='Facial', fingerprint='original'),
        action_slot=NS(handle=7), scale=1., repeat=1., influence=.5, frame_start=1.)
    track = NS(name='FaceLayer', mute=False, is_solo=False, lock=False, strips=[strip])
    owner = NS(name='Rig', animation_data=NS(drivers=[], use_tweak_mode=False, nla_tracks=[track], action=None,
        action_slot=None, action_blend_type='REPLACE', action_influence=1., action_extrapolation='HOLD', use_nla=True))
    before = runtime_digest(ns['px_animation'](owner))
    for field, value in [('scale', 2.), ('repeat', 3.), ('influence', .7), ('frame_start', 5.)]:
        old = getattr(strip, field); setattr(strip, field, value)
        assert runtime_digest(ns['px_animation'](owner)) != before
        setattr(strip, field, old)
    strip.fcurves[0].value=.3
    assert runtime_digest(ns['px_animation'](owner)) != before
    strip.fcurves[0].value=.2; strip.action.fingerprint='changed facial key'
    assert runtime_digest(ns['px_animation'](owner)) != before


class Owned(dict):
    def __init__(self, name, **values):
        super().__init__(values); self.name=name; self.library=None; self.override_library=None


def test_collection_fingerprint_covers_members_and_ownership_without_raw_output():
    ns = helper_namespace('px_fail', 'px_local', 'px_collection_record')
    generation = 'a'*32
    one = Owned('ColliderOne', production_m8_owned=True, production_generation_id=generation, production_candidate_version=2)
    two = Owned('ColliderTwo', production_m8_owned=True, production_generation_id=generation, production_candidate_version=2)
    collection = Owned('M8_COLLIDERS_'+generation+'_v2', production_m8_owned=True,
        production_generation_id=generation, production_candidate_version=2)
    collection.all_objects=[one]; collection.children=[]
    before=runtime_digest(ns['px_collection_record'](collection, generation, 2, True))
    collection.all_objects.append(two)
    assert runtime_digest(ns['px_collection_record'](collection, generation, 2, True))!=before
    two['production_candidate_version']=1
    with pytest.raises(ValueError, match='PHYSICS_COLLIDER_OWNERSHIP'):
        ns['px_collection_record'](collection, generation, 2, True)
    two['production_candidate_version']=2; collection['production_m8_owned']=False
    with pytest.raises(ValueError, match='PHYSICS_COLLIDER_OWNERSHIP'):
        ns['px_collection_record'](collection, generation, 2, True)


def test_owned_collection_member_order_is_stable():
    ns=helper_namespace('px_fail','px_local','px_collection_record'); generation='b'*32
    members=[Owned(name,production_m8_owned=True,production_generation_id=generation,production_candidate_version=1) for name in ('B','A')]
    collection=Owned('Own',production_m8_owned=True,production_generation_id=generation,production_candidate_version=1)
    collection.all_objects=members;collection.children=[]
    before=ns['px_collection_record'](collection,generation,1,True)
    collection.all_objects=list(reversed(members))
    assert ns['px_collection_record'](collection,generation,1,True)==before


@pytest.mark.parametrize('kind',['NODES','MESH_SEQUENCE_CACHE','SURFACE_DEFORM','UV_PROJECT'])
def test_uncertified_modifier_dependencies_reject_before_raw_signature(kind):
    ns=helper_namespace('px_fail','px_local','px_object_raw')
    obj=Owned('Original');obj.type='MESH';obj.data=None;obj.modifiers=[NS(type=kind)]
    with pytest.raises(ValueError,match='PHYSICS_MODIFIER_UNSUPPORTED'):
        ns['px_object_raw'](obj)


@pytest.mark.parametrize('kind',['MESH_CACHE','CLOTH','COLLISION'])
def test_original_solver_or_external_file_cache_is_not_certified(kind):
    ns=helper_namespace('px_fail','px_local','px_object_raw')
    obj=Owned('Original');obj.type='MESH';obj.data=None;obj.modifiers=[NS(type=kind)]
    with pytest.raises(ValueError,match='PHYSICS_SOURCE_SOLVER_UNSUPPORTED'):
        ns['px_object_raw'](obj)


def test_unknown_constraint_target_collections_reject_even_after_target_changes():
    ns=helper_namespace('px_fail','px_local','px_bound_actions','px_dependencies')
    target=RNA(name='ArmatureConstraint',type='ARMATURE',targets=[NS(target='FirstRig')],
        bl_rna=NS(properties=[prop('targets','COLLECTION')]))
    obj=Owned('Source');obj.data=None;obj.type='EMPTY';obj.parent=None;obj.constraints=[target];obj.modifiers=[];obj.animation_data=None
    ns['bpy']=NS(context=NS(scene=NS(objects=[obj])))
    for name in ('FirstRig','ChangedRig'):
        target.targets[0].target=name
        with pytest.raises(ValueError,match='PHYSICS_DEPENDENCY_UNSUPPORTED.*targets'):
            ns['px_dependencies']([obj])


@pytest.mark.parametrize('kind',['TEX_IMAGE','GROUP','VALTORGB','CURVE_RGB'])
def test_unsupported_external_or_nested_material_nodes_are_not_fingerprinted(kind):
    ns=helper_namespace('m6_integrity_value','m6_integrity_rna','m6_integrity_static','m6_integrity_material')
    node=NS(type=kind)
    material=NS(name='Material',library=None,override_library=None,animation_data=None,use_nodes=True,
        bl_rna=NS(properties=[]),node_tree=NS(animation_data=None,nodes=[node],links=[]))
    with pytest.raises(ValueError,match='ASSET_CONTEXT_UNSUPPORTED'):
        ns['m6_integrity_material'](material)


def test_animated_or_driven_material_parameters_are_not_certified():
    ns=helper_namespace('m6_integrity_value','m6_integrity_rna','m6_integrity_static','m6_integrity_material')
    material=NS(name='Material',library=None,override_library=None,
        animation_data=NS(action=NS(name='DiffuseAnimated'),drivers=[],nla_tracks=[]))
    with pytest.raises(ValueError,match='animated or driven material'):
        ns['m6_integrity_material'](material)


def test_original_chain_controls_source_dependency_selection_after_retarget():
    ns=helper_namespace('px_selection')
    source=NS(name='Hair',type='MESH',modifiers=[])
    source_rig=NS(name='OriginalRig');clone=NS(name='CandidateRig')
    ns['px_object']=lambda name:{'Hair':source,'OriginalRig':source_rig,'CandidateRig':clone}[name]
    ns['prod_require_evaluable']=lambda objects:None
    ns['px_validate_objects']=lambda objects:None
    ns['px_dependencies']=lambda objects:objects
    _,_,deps=ns['px_selection']({'source_object':'Hair','source_chain':{'armature':'OriginalRig','bones':['H']},
        'chain':{'armature':'CandidateRig','bones':['H']}})
    assert source_rig in deps and clone not in deps


@pytest.mark.parametrize('path',['hide_render','modifiers["Subsurf"].levels','delta_location','data.vertices[0].co',
    'rotation_mode','pose.bones["Secondary"].bbone_curveinx'])
def test_nonrestorable_primary_rna_channels_reject(path):
    ns=helper_namespace('px_restorable_path');ns['json']=json
    owner=Owned('Original');owner.bl_rna=NS(identifier='Object');owner.type='EMPTY';owner.constraints=[]
    assert ns['px_restorable_path'](owner,path) is False


def test_restorable_body_face_custom_and_constraint_channels_are_explicit():
    ns=helper_namespace('px_restorable_path');ns['json']=json
    owner=Owned('Original',primitive=.2);owner.bl_rna=NS(identifier='Object');owner.type='ARMATURE'
    constraint=NS(path_from_id=lambda:'constraints["Follow"]')
    owner.constraints=[constraint]
    bone=Owned('Bone',control=.1);bone.constraints=[];bone.path_from_id=lambda:'pose.bones["Bone"]'
    owner.pose=NS(bones=[bone])
    for path in ('location','rotation_quaternion','scale','["primitive"]','constraints["Follow"].influence',
                 'constraints["Follow"].mute','pose.bones["Bone"].rotation_euler','pose.bones["Bone"]["control"]'):
        assert ns['px_restorable_path'](owner,path)
    keyowner=NS(bl_rna=NS(identifier='Key'),key_blocks=[NS(path_from_id=lambda:'key_blocks["Smile"]')])
    assert ns['px_restorable_path'](keyowner,'key_blocks["Smile"].value')
    assert ns['px_restorable_path'](keyowner,'eval_time')
    assert not ns['px_restorable_path'](keyowner,'key_blocks["Smile"].mute')
    owner['array']=[.1,.2]
    assert not ns['px_restorable_path'](owner,'["array"]')


@pytest.mark.parametrize('rigid_kind',['rigid_body','rigid_body_constraint'])
def test_rigid_body_dependency_rejects_before_animation_or_capture(rigid_kind):
    ns=helper_namespace('px_fail','px_validate_objects');ns['px_environment_guard']=lambda:None
    obj=NS(name='Original')
    setattr(obj,rigid_kind,NS())
    with pytest.raises(ValueError,match='PHYSICS_ENVIRONMENT_UNSUPPORTED'):
        ns['px_validate_objects']([obj])


def test_global_rigidbody_and_force_fields_reject_without_disabling_them():
    ns=helper_namespace('px_fail','px_environment_guard');scene=NS(rigidbody_world=NS())
    ns['bpy']=NS(context=NS(scene=scene))
    with pytest.raises(ValueError,match='PHYSICS_ENVIRONMENT_UNSUPPORTED'):
        ns['px_environment_guard']()
    field=NS(type='WIND');obj=NS(name='Wind',field=field)
    scene.rigidbody_world=None;scene.objects=[obj]
    with pytest.raises(ValueError,match='PHYSICS_ENVIRONMENT_UNSUPPORTED'):
        ns['px_environment_guard']()
    assert field.type=='WIND' and scene.objects==[obj]
