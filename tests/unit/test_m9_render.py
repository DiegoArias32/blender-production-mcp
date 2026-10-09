"""Capacity, backend declarations and artifact confinement for material QA."""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.materials import render


@pytest.mark.parametrize('options',[
    False,[],42,'EEVEE',{'width':True},{'width':63},{'width':513},{'height':63},{'height':513},
    {'samples':True},{'samples':0},{'samples':129},{'samples':float('nan')},
    {'engine':'BLENDER_WORKBENCH'},{'backend':'TPU'},{'unknown':1},
    {'lighting_set':[]},{'lighting_set':['NEUTRAL','NEUTRAL']},{'lighting_set':[{}]},
    {'views':['TOP']},{'views':'FRONT'},{'require_gpu':1},{'allow_cpu_fallback':'true'},
    {'backend':'CPU'},{'backend':'CPU','allow_cpu_fallback':True,'require_gpu':True},
])
def test_bounded_render_rejects_invalid_or_contradictory_request(options):
    with pytest.raises(ValueError):render.normalize_options(options)


def test_cycles_cpu_requires_explicit_permission_and_does_not_claim_gpu():
    value=render.normalize_options({'engine':'CYCLES','backend':'CPU','allow_cpu_fallback':True})
    assert value['allow_cpu_fallback'] is True
    assert value['require_gpu'] is False
    assert value['backend']=='CPU'


def test_material_preview_payload_uses_safe_mode_without_frame_advance():
    code=build_code(render.PAYLOAD,{'source_object':'quoted"name','source_material':'source','candidate_material':'candidate'})
    assert 'def mr_render' in code
    assert '.frame_set(' not in code
    assert len(code.encode('utf-8'))<65536


def test_options_do_not_mutate_caller_and_default_includes_close_up():
    source={'lighting_set':['NEUTRAL']}
    value=render.normalize_options(source)
    assert source=={'lighting_set':['NEUTRAL']}
    assert value['views']==['THREE_QUARTER','CLOSE_UP']


class ImageBridge:
    def __init__(self,escape=False,partial=False):self.escape=escape;self.partial=partial;self.params=None
    def run(self,body,params):
        self.params=params
        directory=Path(params['output_dir'])
        # Enough IHDR bytes for the shared artifact validator; real PNG decoding
        # and rendered content are independently exercised in the Blender probe.
        files=[]
        for lighting in params['lighting_set']:
            for view in params['views']:
                for variant in ('SOURCE','CANDIDATE'):
                    target=(directory.parent if self.escape else directory)/(lighting+'_'+view+'_'+variant+'.png')
                    target.write_bytes(b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',64,64))
                    files.append({'path':str(target),'lighting':lighting,'view':view,'variant':variant})
        return {'status':'ok','state_restored':True,'files':files[:1] if self.partial else files}


def test_host_records_actual_image_hash_and_retains_artifact_manifest(tmp_path):
    bridge=ImageBridge()
    result=render.render(bridge,Storage(tmp_path),'test','mesh','source','candidate',{'width':64,'height':64})
    file=result['files'][0]
    assert result['status']=='ok'
    assert file['sha256']==hashlib.sha256(Path(file['path']).read_bytes()).hexdigest()
    assert Path(result['manifest']).is_file()
    assert result['metrics']['rendered_images']==4
    assert result['metrics']['peak_memory_status']=='UNKNOWN_NOT_MEASURED'


def test_returned_artifact_outside_reserved_image_directory_is_error(tmp_path):
    result=render.render(ImageBridge(True),Storage(tmp_path),'test','mesh','source','candidate',{'width':64,'height':64})
    assert result['status']=='error'
    assert 'outside the reserved output directory' in result['errors'][0]['message']
    assert 'sha256' not in result['files'][0]


def test_bad_reference_is_rejected_before_reserving_storage(tmp_path):
    with pytest.raises(ValueError,match='MATERIAL_RENDER_REFERENCE_INVALID'):
        render.render(ImageBridge(),Storage(tmp_path),'test','mesh','source',False)
    assert not (tmp_path/'material-previews').exists()


def test_partial_render_cannot_be_reported_successful(tmp_path):
    result=render.render(ImageBridge(partial=True),Storage(tmp_path),'test','mesh','source','candidate',{'width':64,'height':64})
    assert result['status']=='error'
    assert any(row.get('code')=='MATERIAL_RENDER_INCOMPLETE' for row in result['errors'])
