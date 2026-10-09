"""Attach the coordinator's recorded image inspection to NEW fixture-only packages.

Run only after inspecting the images in the selected passed pipeline report. It
never overwrites a package, fixture or source .blend; package creates final copies.
"""
import argparse,json,sys,tempfile,types
from pathlib import Path
from uuid import uuid4
import bpy

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests/blender'))
package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')];sys.modules['blender_mcp']=package
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.storage import Storage,write_json,sha256
from blender_mcp.production.common import m5
from blender_mcp.production.game_mesh.package import prepare
from m31_fixture_support import register_open_fixture

class Bridge:
    def run(self,body,params=None):
        namespace={};exec(build_code(body,params).rsplit('print(',1)[0],namespace);return namespace['result']

parser=argparse.ArgumentParser();parser.add_argument('--pipeline',required=True);parser.add_argument('--review',required=True)
args=parser.parse_args(sys.argv[sys.argv.index('--')+1:]);pipeline_path=Path(args.pipeline).resolve(strict=True)
pipeline=json.loads(pipeline_path.read_text());review=json.loads(Path(args.review).read_text())
assert pipeline['passed'] and review['status']=='reviewed' and review['pipeline']==str(pipeline_path)
base=Path(pipeline['output_dir']).resolve();allowed=(Path(tempfile.gettempdir())/'bpm5').resolve()
assert base.is_relative_to(allowed)
storage=Storage(base/'s');bridge=Bridge();result={'checks':{},'packages':{},'review':args.review}
for variant,demo in pipeline['demos'].items():
    fixture=Path(demo['fixture']).resolve(strict=True);assert fixture.is_relative_to(base)
    fixture_sha=sha256(fixture)
    bpy.ops.wm.open_mainfile(filepath=str(fixture),use_scripts=False,load_ui=False)
    register_open_fixture(bridge,storage,base)
    before=m5.signature(bridge,demo['source']);low=m5.signature(bridge,demo['low'])
    assert low['fingerprint']==demo['validation']['signature']['fingerprint']
    folder=storage.reserve('game_mesh',uuid4().hex);qa=folder/'visual-review.json'
    write_json(qa,{'schema':'production.game_mesh_visual_qa.v1','status':'reviewed',
        'source_object':demo['source'],'target_fingerprint':low['fingerprint'],'images':demo['images'],
        'observations':review['observations'][variant],'limitations':review['limitations'],
        'reviewer':'Codex actual rendered-image inspection','pipeline':str(pipeline_path)})
    def frozen_options(step):
        path=Path(pipeline['checks'][variant+'_'+step+'_verified']['result']['operation']).resolve(strict=True)
        assert path.is_relative_to(storage.root);return json.loads(path.read_text())['options']
    retopo=frozen_options('project');target=retopo['target_triangles']
    retopo.update(minimum_triangles=int(target*.95),maximum_triangles=int(target*1.05))
    uv_options=frozen_options('uv')
    # uv_operation is the saved runtime discriminator, not a public UV option.
    uv_options.pop('uv_operation',None)
    pair=json.loads(Path(demo['bake_pair']['pair']).read_text())
    options={'retopology':retopo,'weights':frozen_options('weights'),'attributes':frozen_options('attributes'),
        'game_mesh':{'uv_layer':'GameUV','quad_method':'BEAUTY'},'uv':uv_options,'bake':pair['options']}
    output=prepare(bridge,storage,uuid4().hex,demo['validation']['relationship'],demo['bake_pair']['pair'],
        qa_report=str(qa),options=options,dry_run=False)
    intact=m5.signature(bridge,demo['source'])['fingerprint']==before['fingerprint']
    assert intact and output['readiness']=='READY_WITH_REVIEW_LIMITS' and sha256(fixture)==fixture_sha
    result['packages'][variant]=dict(output,visual_report=str(qa))
    result['checks'][variant+'_new_reviewed_package']={'passed':True,'source_intact':intact,'fixture_file_intact':True}
result['passed']=True
write_json(ROOT/'test-artifacts/m5/reviewed-packages.json',result)
print('M5_REVIEWED_PACKAGES_PASS',flush=True)
