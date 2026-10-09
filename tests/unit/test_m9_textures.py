import hashlib
import struct

from blender_mcp.production.materials import textures


def image(name='Data',space='Non-Color',**kwargs):
    return dict(name=name,source='FILE',colorspace=space,colorspace_is_data=space=='Non-Color',
                has_data=True,channels=4,dependencies=[],**kwargs)


def codes(result):return {i['code'] for i in result['issues']}


def test_reads_actual_png_precision_without_rewriting(tmp_path):
    path=tmp_path/'file.png';payload=b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',32,64)+bytes([16,6,0,0,0])+b'crc!'
    path.write_bytes(payload);before=path.stat().st_mtime_ns
    result=textures.dependencies([image(dependencies_override=True)|{'dependencies':[{'tile':None,'path':str(path)}]}])[0]
    dep=result['dependencies'][0]
    assert dep['sha256']==hashlib.sha256(payload).hexdigest()
    assert dep['file_dimensions']==[32,64] and dep['file_bit_depth_per_channel']==16
    assert path.read_bytes()==payload and path.stat().st_mtime_ns==before


def test_unknown_file_precision_not_inferred_from_extension(tmp_path):
    path=tmp_path/'sample.exr';path.write_bytes(b'arbitrary opaque test bytes')
    dep=textures._file(str(path));assert dep['file_bit_depth_per_channel'] is None
    assert dep['precision_source']=='UNKNOWN'


def test_missing_file_and_empty_buffer_are_errors(tmp_path):
    raw=image()|{'has_data':False,'dependencies':[{'tile':1002,'path':str(tmp_path/'absent.png')} ]}
    result=textures.validate(textures.dependencies([raw]))
    assert {'TEXTURE_FILE_MISSING','TEXTURE_DATA_UNAVAILABLE'}<=codes(result)


def test_generated_and_packed_do_not_require_external_files():
    for values in ({'source':'GENERATED'},{'source':'FILE','packed':True}):
        raw=image()|values|{'dependencies':[{'tile':None,'path':''}]}
        assert 'TEXTURE_FILE_MISSING' not in codes(textures.validate(textures.dependencies([raw])))


def test_roughness_srgb_rejected_even_when_caller_declares_srgb():
    report=textures.validate([image(space='sRGB')],{'roughness':{'image':'Data','colorspace':'sRGB','component':'R'}})
    assert 'TEXTURE_DATA_COLORSPACE' in codes(report)


def test_shared_image_color_data_conflict_is_reported():
    report=textures.validate([image()],{'base_color':{'image':'Data'},'roughness':{'image':'Data'}})
    assert 'TEXTURE_SEMANTIC_COLORSPACE_CONFLICT' in codes(report)


def test_alpha_component_requires_real_channel():
    report=textures.validate([image()|{'channels':3}],{'opacity':{'image':'Data','component':'A'}})
    assert 'TEXTURE_ALPHA_UNAVAILABLE' in codes(report)


def test_udim_mixed_resolution_is_not_silently_normalized():
    row=image()|{'source':'TILED','tiles':[1001,1002],'dependencies':[{'file_dimensions':[16,16]},{'file_dimensions':[32,32]}]}
    assert 'UDIM_RESOLUTION_MISMATCH' in codes(textures.validate([row]))


def test_remote_file_is_not_downloaded():
    assert textures._file('https://invalid.example/texture.png')['exists'] is None


def test_dirty_buffer_has_warning_and_no_claim_of_reload():
    report=textures.validate([image(is_dirty=True)])
    assert 'TEXTURE_BUFFER_DIRTY' in codes(report)
    assert report['reload_status'].startswith('UNKNOWN')
