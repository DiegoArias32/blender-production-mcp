from copy import deepcopy
from blender_mcp.production.materials import qa, inspection, uv


def raw():
    return {'material_name':'SkinByNameOnly','object_name':'Mesh','use_nodes':True,'images':[],
        'nodes':[{'id':'BSDF','type':'BSDF_PRINCIPLED','inputs':[{'name':'Metallic','value':0.0,'linked':False},{'name':'Roughness','value':.5,'linked':False}]},
                 {'id':'Output','type':'OUTPUT_MATERIAL','active_output':True,'inputs':[]}],
        'links':[{'from_node':'BSDF','from_socket':'BSDF','from_name':'BSDF','from_type':'SHADER','to_node':'Output','to_socket':'Surface','to_name':'Surface','to_type':'SHADER','valid':True}],
        'uv':{'available':True,'valid':True,'active_layer':'UVMap','layers':['UVMap'],'material_tiles':{'0':[1001,1002,1011]}},
        'slots':[{'index':0,'material':'SkinByNameOnly'}],'units':{'scale_length':1},'renderer':'BLENDER_EEVEE_NEXT','properties':{'surface_render_method':'DITHERED'}}


def codes(report):return {i['code'] for i in report['issues']}


def test_disconnected_output_fails_not_merely_no_exception():
    data=raw();data['links']=[];report=qa.report('material_quality_report',data)
    assert not report['passed'] and 'SHADER_OUTPUT_DISCONNECTED' in codes(report)


def test_missing_image_node_fails():
    data=raw();data['nodes'].append({'id':'Texture','type':'TEX_IMAGE','image':None})
    assert not qa.report('material_quality_report',data)['passed']


def test_custom_shader_is_unknown_not_certified_compatible():
    data=raw();data['nodes'][0]['type']='BSDF_HAIR_PRINCIPLED'
    report=qa.report('material_compatibility_report',data)
    assert not report['supported'] and 'SHADER_FEATURE_UNSUPPORTED' in codes(report)


def test_direct_normal_rgb_connection_fails():
    data=raw();data['nodes'].append({'id':'Texture','type':'TEX_IMAGE','image':'Normal'})
    data['links'].append({'from_node':'Texture','from_socket':'Color','from_name':'Color','to_node':'BSDF','to_socket':'Normal','to_name':'Normal'})
    assert 'NORMAL_IMAGE_DIRECT_CONNECTION' in codes(qa.report('material_validate_normals',data))


def test_name_only_skin_not_inferred():
    assert inspection.classify(raw())['category']=='GENERIC_DIELECTRIC'
    report=inspection.classify(raw(),{'category':'SKIN'})
    assert report['category']=='SKIN' and report['evidence'][0]['provenance']=='caller'


def test_multiudim_single_material_allowed_and_missing_tile_reported():
    data=raw();data['images']=[{'name':'Color','source':'TILED','tiles':[1001,1002,1011]}]
    assert not uv.validate(data)['issues']
    data['images'][0]['tiles'].remove(1011)
    report=uv.validate(data);assert 'UDIM_TILE_MISSING' in codes(report)
    assert report['bake_readiness']=='UNKNOWN' and report['one_material_per_udim_required'] is False


def test_true_displacement_is_not_bump_certificate():
    data=raw();data['nodes'].append({'id':'Displace','type':'DISPLACEMENT'})
    report=qa.report('material_validate_height',data)
    assert not report['passed'] and 'DISPLACEMENT_GEOMETRY_UNCERTIFIED' in codes(report)


def test_inspection_is_compact_and_paginated_without_mutation():
    data=raw();before=deepcopy(data);result=inspection.compact(data,{'limit':1})
    assert len(result['nodes'])==1 and result['has_more']
    assert data==before


def test_performance_missing_tile_dimensions_stays_unknown():
    data=raw();data['images']=[{'name':'Tiles','source':'TILED','tiles':[1001,1002],'dependencies':[{'file_dimensions':[16,16]},{}]}]
    report=qa.performance(data)
    assert report['unknown_image_memory']==['Tiles'] and report['shader_evaluation_ms'] is None


def test_normal_convention_not_guessed():
    data=raw();data['nodes'].append({'id':'Normal','type':'NORMAL_MAP','space':'TANGENT','uv_map':'UVMap',
        'inputs':[{'name':'Strength','value':1.}]})
    report=qa.report('material_validate_normals',data)
    assert 'NORMAL_CONVENTION_UNKNOWN' in codes(report)
    assert report['tangent_validation'].startswith('prerequisites_only')


def test_pbr_actual_component_must_match_declared_contract():
    data=raw();data['nodes'].append({'id':'Texture','type':'TEX_IMAGE','image':'Rough'})
    data['links'].append({'from_node':'Texture','from_socket':'Color','from_name':'Color','to_node':'BSDF','to_socket':'Roughness','to_name':'Roughness'})
    result=qa.report('material_validate_pbr',data,{'channels':{'roughness':{'image':'Rough','component':'R'}}})
    assert 'PBR_CHANNEL_COMPONENT_MISMATCH' in codes(result)


def test_candidate_image_resolution_requires_matching_signed_ownership():
    data=raw();data.update(owned=True,generation_id='g',channel_bindings={
        'roughness':{'source_image':'Source','candidate_image':'Copy','component':'R','colorspace':'Non-Color'}})
    data['images']=[{'name':'Copy','owned':True,'generation_id':'g','source_image':'Source'}]
    definition={'channels':{'roughness':{'image':'Source','component':'R','colorspace':'Non-Color'}}}
    resolved=qa.resolve_definition(data,definition)
    assert resolved['channels']['roughness']['image']=='Copy'
    assert definition['channels']['roughness']['image']=='Source'
    data['images'][0]['generation_id']='other'
    import pytest
    with pytest.raises(ValueError,match='OWNERSHIP_MISMATCH'):qa.resolve_definition(data,definition)


def test_bump_height_is_not_inferred_as_normal_rgb():
    data=raw();data['nodes'] += [{'id':'Height','type':'TEX_IMAGE','image':'Height'},
        {'id':'Bump','type':'BUMP','inputs':[{'name':'Distance','value':.01}]}]
    data['links'] += [{'from_node':'Height','from_name':'Color','from_socket':'Color','to_node':'Bump','to_name':'Height'},
        {'from_node':'Bump','from_name':'Normal','from_socket':'Normal','to_node':'BSDF','to_name':'Normal'}]
    channels=qa.inferred_channels(data)
    assert 'height' in channels and 'normal' not in channels
    assert 'NORMAL_CONVENTION_UNKNOWN' not in codes(qa.report('material_validate_height',data))


def test_combined_bump_plus_normal_maps_trace_independently():
    data=raw();data['nodes'] += [{'id':'HeightTex','type':'TEX_IMAGE','image':'Height'},
        {'id':'NormalTex','type':'TEX_IMAGE','image':'Normal'},
        {'id':'NormalMap','type':'NORMAL_MAP','space':'TANGENT','inputs':[{'name':'Strength','value':1.}]},
        {'id':'Bump','type':'BUMP','inputs':[{'name':'Distance','value':.01}]}]
    data['links'] += [{'from_node':'HeightTex','from_name':'Color','to_node':'Bump','to_name':'Height'},
        {'from_node':'NormalTex','from_name':'Color','to_node':'NormalMap','to_name':'Color'},
        {'from_node':'NormalMap','from_name':'Normal','to_node':'Bump','to_name':'Normal'},
        {'from_node':'Bump','from_name':'Normal','to_node':'BSDF','to_name':'Normal'}]
    channels=qa.inferred_channels(data)
    assert channels['height']['image']=='Height' and channels['normal']['image']=='Normal'


def test_unknown_shader_cannot_pass_technical_acceptance():
    data=raw();data['nodes'][0]['type']='BSDF_HAIR_PRINCIPLED'
    report=qa.report('material_quality_report',data)
    assert report['passed'] is False and report['blocking_unknown']


def normal_raw(samples):
    data=raw();data['images']=[{'name':'NormalImage','colorspace':'Non-Color','colorspace_is_data':True,
        'has_data':True,'channels':4,'rgb_samples':samples,'size':[16,16]}]
    data['nodes'] += [{'id':'Tex','type':'TEX_IMAGE','image':'NormalImage'},
        {'id':'NM','type':'NORMAL_MAP','space':'TANGENT','uv_map':'UVMap','inputs':[{'name':'Strength','value':1.}]}]
    data['links'] += [{'from_node':'Tex','from_name':'Color','to_node':'NM','to_name':'Color'},
        {'from_node':'NM','from_name':'Normal','to_node':'BSDF','to_name':'Normal'}]
    return data


def test_connected_black_normal_pixels_do_not_certify_valid_normal():
    report=qa.report('material_validate_normals',normal_raw([[0.,0.,0.]]*4),options={'normal_convention':'OPENGL'})
    assert not report['passed'] and 'NORMAL_VECTOR_SAMPLES_INVALID' in codes(report)


def test_unit_normal_samples_have_explicit_coverage_limit():
    report=qa.report('material_validate_normals',normal_raw([[.5,.5,1.]]*4),options={'normal_convention':'OPENGL'})
    measured=next(i for i in report['issues'] if i['code']=='NORMAL_VECTOR_SAMPLES_CHECKED')
    assert measured['samples']==4 and 'No cubre todos' in measured['limitations']
    assert report['passed']


def test_partial_normal_padding_suspects_do_not_hide_measurement():
    report=qa.report('material_validate_normals',normal_raw([[.5,.5,1.],[0.,0.,0.]]),options={'normal_convention':'OPENGL','normal_length_tolerance':.1})
    item=next(i for i in report['issues'] if i['code']=='NORMAL_VECTOR_SAMPLES_SUSPICIOUS')
    assert item['invalid_samples']==1 and item['tolerance_provenance']=='caller'


def test_nonfinite_normal_pixel_is_error():
    report=qa.report('material_validate_normals',normal_raw([[None,.5,1.]]),options={'normal_convention':'OPENGL'})
    assert not report['passed'] and 'NORMAL_VECTOR_SAMPLES_INVALID' in codes(report)


def test_no_pixel_samples_cannot_be_reported_as_normal_pass():
    assert not qa.report('material_validate_normals',normal_raw([]),options={'normal_convention':'OPENGL'})['passed']


def test_installed_separate_color_type_traces_exact_red_output():
    data=raw();data['nodes'] += [{'id':'Tex','type':'TEX_IMAGE','image':'Data'},
        {'id':'Split','type':'SEPARATE_COLOR'}]
    data['links'] += [{'from_node':'Tex','from_name':'Color','to_node':'Split','to_name':'Color'},
        {'from_node':'Split','from_name':'Red','to_node':'BSDF','to_name':'Roughness'}]
    actual=qa.inferred_channels(data)['roughness']
    assert actual['image']=='Data' and actual['component']=='R'
    assert not any(i['code']=='SHADER_FEATURE_UNSUPPORTED' for i in qa.graph_issues(data))
    data['links'][-1]['from_name']='Green'
    assert qa.inferred_channels(data)['roughness']['component']=='G'
    assert any(i['code']=='PBR_CHANNEL_COMPONENT_MISMATCH' for i in qa.graph_issues(data,{
        'channels':{'roughness':{'image':'Data','component':'R','colorspace':'Non-Color'}}}))


def test_cross_tile_face_cannot_silently_omit_intermediate_udim_dependencies():
    data=raw();data['uv'].update(valid=False,errors=['UV_CROSS_TILE_OR_OUT_OF_RANGE'],material_tiles={'0':[None]})
    report=qa.report('material_validate_udim',data)
    assert not report['passed'] and 'MATERIAL_UV_TILE_UNRESOLVED' in codes(report)


def test_zero_area_uv_and_incomplete_analysis_block_tangent_certificate():
    for code in ('UV_ZERO_AREA','UV_CAPACITY_EXCEEDED','UV_NONFINITE','UDIM_TILES_MISMATCH'):
        data=raw();data['uv'].update(valid=False,errors=[code])
        result=qa.report('material_validate_tangent_workflow',data)
        assert not result['passed'],code


def test_bake_padding_and_distortion_still_explicit_not_material_topology_failure():
    data=raw();data['uv'].update(valid=False,errors=['UV_PADDING_NOT_CERTIFIED','UV_STRETCH_EXCESSIVE'])
    result=qa.report('material_validate_uv',data)
    assert result['passed'] and result['uv']['uv_valid'] is False
    assert result['uv']['bake_readiness']=='UNKNOWN' and len(result['warnings'])==2
