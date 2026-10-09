"""Real Blender transaction tests; only creates new synthetic fixtures/artifacts."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
# M3.1: BLENDER_PRODUCTION_TEST_ARTIFACTS moves large probe output out of the repository
# (default unchanged: <repo>/test-artifacts).
ARTIFACTS = Path(os.environ.get('BLENDER_PRODUCTION_TEST_ARTIFACTS') or ROOT / 'test-artifacts')


def inside(output):
    import bpy
    import types
    sys.path.insert(0,str(ROOT/'src'))
    sys.path.insert(0,str(ROOT/'tests/blender'))
    # Upstream package initializer imports its MCP server, unavailable in Blender's
    # embedded Python. Load the real production modules as a namespace package;
    # bpy, bridge templates and safe-mode are all real, not mocked.
    package=types.ModuleType('blender_mcp')
    package.__path__=[str(ROOT/'src/blender_mcp')]
    sys.modules['blender_mcp']=package
    from production_fixtures import build
    from blender_mcp.production.common.bridge import build_code, MARKER
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.animation.corrections import correction
    from blender_mcp.production.animation.actions import manage_action, compare_actions, BODY
    from blender_mcp.production.checkpoint import restore_checkpoint
    from blender_mcp.production.common.operations import fingerprint
    from blender_mcp.safe_mode import validate_code

    class LocalBridge:
        def run(self,body,params=None):
            code=build_code(body,params)
            validate_code(code)
            namespace={}
            # Validation exactly matches MCP bridge; execute in isolated test process.
            exec(code.rsplit('print(',1)[0],namespace)
            return namespace['result']

    bridge=LocalBridge()
    storage=Storage(output/'store')
    manifest=build(output)
    from m31_fixture_support import register_open_fixture
    register_open_fixture(bridge,storage,output)
    checks=[]
    def check(name,condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS '+name,flush=True)

    original=bridge.run(BODY+"\nresult={'snapshot':m2_snapshot(bpy.data.actions['M1_Walk'])}")['snapshot']
    cp=manage_action(bridge,storage,'create-test','create','M1_Rig',name='M2_Empty',dry_run=False)
    check('create action preserves active original',cp['source_intact'] and bpy.data.objects['M1_Rig'].animation_data.action.name=='M1_Walk')
    first=manage_action(bridge,storage,'duplicate-test','duplicate','M1_Rig','M1_Walk',name='M2_Copy',dry_run=False)
    second=manage_action(bridge,storage,'duplicate-test','duplicate','M1_Rig','M1_Walk',name='M2_Copy',dry_run=False)
    check('duplicate collision-safe distinct names',first['action']!=second['action'])
    same=compare_actions(bridge,'M1_Rig','M1_Walk',first['action'],1,10)
    check('duplicate pose equivalence',same['metrics']['max_position_difference_m']<1e-7)
    options={'controls':['FootIK.L','FootIK.R'],'confirm_contacts':True,'frame_start':1,'frame_end':10,'blend_frames':3}
    proposal=correction(bridge,storage,'sliding-op','foot_sliding','M1_Rig','propose',options)
    check('proposal leaves source unchanged',original==bridge.run(BODY+"\nresult={'snapshot':m2_snapshot(bpy.data.actions['M1_Walk'])}")['snapshot'])
    preview=correction(bridge,storage,'sliding-preview','foot_sliding',mode='preview',operation=proposal['operation'])
    print(json.dumps(preview,indent=2),flush=True)
    check('sliding preview numeric pass',preview['numeric_verification']=='pass')
    check('preview restores active source',bpy.data.objects['M1_Rig'].animation_data.action.name=='M1_Walk')
    applied=correction(bridge,storage,'sliding-apply','foot_sliding',mode='apply',operation=proposal['operation'])
    check('applied duplicate active',bpy.data.objects['M1_Rig'].animation_data.action.name==applied['working_action'])
    check('sliding reduced at least 80 percent',applied['metrics']['improvement_percent']>=80)
    check('hip/root preserved',applied['metrics']['max_hip_root_error_m']<0.0001)
    verify=correction(bridge,storage,'sliding-verify','foot_sliding',mode='verify',operation=proposal['operation'])
    check('explicit verify source untouched',verify['source_intact'] and verify['candidate_intact'])
    revert=correction(bridge,storage,'sliding-revert','foot_sliding',mode='revert',operation=proposal['operation'])
    check('revert assigns original and retains candidate',revert['reverted'] and applied['working_action'] in bpy.data.actions)
    rejected=correction(bridge,storage,'fk-op','foot_sliding','M1_Rig','propose',{'frame_start':1,'frame_end':10})
    check('unconfirmed control analysis requires manual correction',not rejected['proposal']['supported'])
    try:
        correction(bridge,storage,'fk-apply','foot_sliding',mode='apply',operation=rejected['operation'])
    except ValueError:
        check('unsupported proposal cannot apply',True)
    else:
        raise AssertionError('Unsupported applied')
    stale=correction(bridge,storage,'stale-op','retime','M1_Rig','propose',{'scale_factor':2,'frame_start':1,'frame_end':31})
    bpy.data.actions['M1_Walk']['test_stale']=True
    try:
        correction(bridge,storage,'stale-apply','retime',mode='apply',operation=stale['operation'])
    except ValueError:
        check('stale source rejected before mutation',True)
    else:
        raise AssertionError('Stale proposal applied')
    del bpy.data.actions['M1_Walk']['test_stale']
    retime=correction(bridge,storage,'retime-op','retime','M1_Rig','propose',{'scale_factor':2,'frame_start':1,'frame_end':31})
    retimed=correction(bridge,storage,'retime-preview','retime',mode='preview',operation=retime['operation'])
    check('retimed poses verified',retimed['numeric_verification']=='pass')
    check('FPS unchanged by duration change',bpy.context.scene.render.fps==30)
    root=correction(bridge,storage,'root-op','root_motion','M1_Rig','propose',{'root_bone':'Root','frame_start':1,'frame_end':31})
    rooted=correction(bridge,storage,'root-preview','root_motion',mode='preview',operation=root['operation'])
    check('root preparation horizontal/vertical tolerance',rooted['numeric_verification']=='pass')
    quaternion=correction(bridge,storage,'quat-op','quaternion','M1_Rig','propose',{})
    quaternion_preview=correction(bridge,storage,'quat-preview','quaternion',mode='preview',operation=quaternion['operation'])
    check('quaternion host lifecycle numeric pass',quaternion_preview['numeric_verification']=='pass')
    check('quaternion representation discontinuity eliminated',quaternion_preview['metrics']['hemisphere_changes_before']==1 and quaternion_preview['metrics']['hemisphere_changes_after']==0)
    cleaned=correction(bridge,storage,'cleanup-op','cleanup','M1_Rig','propose',{'channels':['location']})
    cleanup_preview=correction(bridge,storage,'cleanup-preview','cleanup',mode='preview',operation=cleaned['operation'])
    check('cleanup host lifecycle numeric pass',cleanup_preview['numeric_verification']=='pass')
    check('cleanup reduces keys within tolerance',cleanup_preview['metrics']['keys_after']<cleanup_preview['metrics']['keys_before'] and cleanup_preview['metrics']['max_error']<0.000001)
    check('original content remains exactly unchanged',original==bridge.run(BODY+"\nresult={'snapshot':m2_snapshot(bpy.data.actions['M1_Walk'])}")['snapshot'])
    recovery=restore_checkpoint(bridge,storage,applied['checkpoint'],'restore-after-correction',False)
    check('checkpoint restores correction workflow to new file',recovery.get('working_path') is not None or recovery.get('after') is not None)
    check('checkpoint restoration original Action intact',original==bridge.run(BODY+"\nresult={'snapshot':m2_snapshot(bpy.data.actions['M1_Walk'])}")['snapshot'])
    report={'status':'pass','blender_version':bpy.app.version_string,'checks':checks,'count':len(checks),
            'sliding':applied,'retime':retimed,'root_motion':rooted,'quaternion':quaternion_preview,
            'cleanup':cleanup_preview,'original_sha256':fingerprint(original),'fixture':manifest}
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('M2_CORRECTION_REPORT='+str(output/'report.json'))


def run(blender):
    from uuid import uuid4
    output=ARTIFACTS/('m2c-'+uuid4().hex[:8])
    output.mkdir(parents=True)
    command=[blender,'--background','--factory-startup','--disable-autoexec','--python-exit-code','1',
             '--python',str(Path(__file__).resolve()),'--','--inside',str(output)]
    process=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
    (output/'blender.log').write_text(process.stdout+'\n'+process.stderr,encoding='utf-8')
    if process.returncode:
        print(process.stdout[-10000:]+process.stderr[-3000:])
        raise RuntimeError('Blender correction tests failed: '+str(output))
    report=json.loads((output/'report.json').read_text(encoding='utf-8'))
    print(json.dumps({'status':report['status'],'count':report['count'],'report':str(output/'report.json')},indent=2))


if __name__=='__main__':
    arguments=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else sys.argv[1:]
    parser=argparse.ArgumentParser()
    parser.add_argument('--inside',type=Path)
    parser.add_argument('--blender')
    args=parser.parse_args(arguments)
    inside(args.inside) if args.inside else run(args.blender)
