import math
import pytest

from blender_mcp.production.materials import model


@pytest.mark.parametrize('name',model.PROFILES)
def test_profile_is_only_starting_point_and_normalization_is_stable(name):
    value=model.normalize_definition({},name)
    assert model.normalize_definition(value)==value
    report=model.validate_profile(name,value)
    assert report['passed'] and report['certifies_photorealism'] is False
    assert report['profile']['provenance']=='proposed_starting_configuration'


@pytest.mark.parametrize('name,value',[
    ('metallic',True),('roughness',math.nan),('alpha',math.inf),('transmission_weight',-1),
    ('ior',.5),('normal_strength',11),('height_distance',-1),('base_color',[1,2,0,1]),
    ('subsurface_radius',[1,2]),('emission_color',[1,1,1,2]),('coat_weight','1')])
def test_bad_numeric_parameters_are_rejected(name,value):
    with pytest.raises(ValueError,match='MATERIAL_PARAMETER_INVALID'):
        model.normalize_definition({'parameters':{name:value}})


def test_fractional_metallic_is_preserved_no_physical_boolean_assumption():
    result=model.normalize_definition({'category':'PAINTED_METAL','parameters':{'metallic':.37}})
    assert result['parameters']['metallic']==.37


def test_data_color_channels_are_distinct_and_ao_is_independent():
    result=model.normalize_definition({'channels':{'base_color':{'image':'Color'},'ao':{'image':'Data'},'roughness':{'image':'Data','component':'G'}}})
    assert result['channels']['base_color']=={'image':'Color','component':'COLOR','colorspace':'sRGB'}
    assert result['channels']['roughness']['colorspace']=='Non-Color'
    assert result['channels']['ao']['component']=='R'


@pytest.mark.parametrize('definition',[
    {'workflow':'SPECULAR_GLOSSINESS'}, {'category':'AAA_SKIN'}, {'parameters':{'script':'do_bad()'}},
    {'channels':{'base_color':{'path':'C:/external.png'}}}, {'channels':{'normal':{'image':'n','component':'G'}}},
    {'height_mode':'DISPLACEMENT'}, {'channels':{'height':{'image':'h'}},'height_mode':'NONE'},
    {'renderer':'WORKBENCH'}, {'normal_convention':'auto'}, {'channels':{'height':{'image':''}}},
    {'schema':'unknown'}, {'profile':'made_up'}])
def test_unqualified_or_inconsistent_definition_is_rejected(definition):
    with pytest.raises(ValueError):model.normalize_definition(definition)


def test_profile_overrides_do_not_change_catalog():
    result=model.create_profile('fabric',{'parameters':{'roughness':.42}})
    assert result['parameters']['roughness']==.42
    assert model.create_profile('fabric')['parameters']['roughness']==.75


def test_normal_convention_is_not_guessed_from_filename():
    assert model.normalize_definition({'channels':{'normal':{'image':'Normal_OpenGL'}}})['normal_convention']=='UNKNOWN'


def test_height_bump_explicit_semantics():
    value=model.normalize_definition({'channels':{'height':{'image':'h'}}})
    assert value['height_mode']=='BUMP'


def test_profile_conflict_is_rejected():
    with pytest.raises(ValueError,match='PROFILE_CONFLICT'):model.normalize_definition({'profile':'metal'},'fabric')


@pytest.mark.parametrize('method',['BURLEY','RANDOM_WALK','RANDOM_WALK_SKIN','RANDOM_WALK_LEGACY'])
def test_sss_method_is_explicit_installed_enum(method):
    assert model.normalize_definition({'parameters':{'subsurface_method':method}})['parameters']['subsurface_method']==method


def test_unknown_sss_method_is_rejected():
    with pytest.raises(ValueError,match='SSS_METHOD_UNSUPPORTED'):
        model.normalize_definition({'parameters':{'subsurface_method':'AUTO_MAGIC'}})
