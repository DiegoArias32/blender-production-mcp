"""Full stdio/socket demonstration on generated M2 fixtures only; never a master."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.storage import Storage,sha256
from blender_mcp.production.checkpoint import create_checkpoint
from blender_mcp.production.inspect import snapshot

ROOT=Path(__file__).resolve().parents[2]


@asynccontextmanager
async def session(store,log):
    env=dict(os.environ,BLENDER_HOST='localhost',BLENDER_PORT='9876',BLENDER_MCP_SAFE_MODE='1',
        DISABLE_TELEMETRY='true',BLENDER_PRODUCTION_WORKDIR=str(store),PYTHONIOENCODING='utf-8',
        # M3.1 asset guard: this demo's own workspace is the only extra fixture root.
        BLENDER_PRODUCTION_FIXTURE_ROOTS=str(store))
    params=StdioServerParameters(command=sys.executable,args=['-m','blender_mcp.production.server'],env=env)
    async with stdio_client(params,errlog=log) as (read,write):
        async with ClientSession(read,write) as client:
            await client.initialize()
            yield client


async def run(fixture):
    fixture=fixture.resolve(strict=True)
    if not fixture.is_relative_to(ROOT/'test-artifacts') or fixture.name!='m2-controlled-fixtures.blend':
        raise ValueError('Only generated M2 fixtures inside test-artifacts are permitted')
    folder=ROOT/'test-artifacts'/('m2e-'+uuid4().hex[:8]);folder.mkdir()
    storage=Storage(folder/'s')
    report={'status':'running','fixture':str(fixture),'source_hash':sha256(fixture),'checks':[],'results':[]}
    bridge=Bridge()
    try:
        live=snapshot(bridge)
        source=live.get('source_file')
        if source and not any(Path(source).resolve().is_relative_to(ROOT/name) for name in ('.production','test-artifacts')):
            raise ValueError('Refusing to change away from a production scene')
        report['before_load_checkpoint']=create_checkpoint(bridge,storage,'Before M2 synthetic demonstration','m2-load','load test fixture')
        working=storage.reserve('working','m2-load')/'scene.blend'
        bridge.run('''
import bpy
bpy.ops.wm.open_mainfile(filepath=params['fixture'],use_scripts=False,load_ui=False)
# M3.1 (AUD-015): QA renders Cycles only on the GPU; the working copy's scene uses GPU Compute.
for scene in bpy.data.scenes:
    scene.cycles.device='GPU'
bpy.ops.wm.save_as_mainfile(filepath=params['working'],relative_remap=True)
result={'file':bpy.data.filepath,'blender_version':bpy.app.version_string}
''',{'fixture':str(fixture),'working':str(working)})
    finally:
        bridge.close()
    def save():
        (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    def check(name,value):
        if not value: raise AssertionError(name)
        report['checks'].append(name);save()
    async def call(client,name,arguments=None):
        response=await client.call_tool('production.'+name,arguments or {})
        if response.isError: raise AssertionError(str(response.content))
        envelope=response.structuredContent['result']
        report['results'].append({'tool':name,'response':envelope});save()
        if envelope['status']=='error': raise AssertionError(json.dumps(envelope,indent=2))
        print(name,envelope['status'],flush=True)
        return envelope['data']
    try:
        with (folder/'mcp.log').open('w',encoding='utf-8') as log:
            async with session(storage.root,log) as client:
                inventory=await client.list_tools()
                check('at least 29 historical tools registered over MCP stdio',len(inventory.tools)>=29)
                report['inventory']=[tool.name for tool in inventory.tools]
                # M3.1 (AUD-014): mutation needs an explicit role; register the generated working copy.
                registered=await call(client,'asset_register_fixture',{'path':str(working)})
                check('generated working copy registered as FIXTURE',registered['asset']['role']=='FIXTURE')
                rig='M2_VisibleRig'; original='M2_LeftSliding'
                args={'object_name':rig}
                selected={'frame_start':1,'frame_end':10,'bones':['ContactControl.L','ContactControl.R'],
                    'root_bone':'MotionBase','pelvis_bone':'Pelvis','support_bones':['ContactControl.L','ContactControl.R']}
                await call(client,'scene_audit')
                await call(client,'inspect_armature',{'armature':rig})
                await call(client,'rig_analyze',{'armature':rig})
                await call(client,'animation_analyze',args)
                plan=await call(client,'animation_refinement_plan',dict(args,options=selected))
                for name in ('animation_analyze_contacts','animation_analyze_control_mode','animation_analyze_root_motion',
                             'animation_analyze_balance','animation_analyze_motion_quality'):
                    await call(client,name,dict(args,options=selected))
                await call(client,'animation_analyze_fcurves',args)
                await call(client,'animation_detect_quaternion_flips',args)
                feet=await call(client,'animation_detect_foot_sliding',{'armature':rig,'bones':['ContactControl.L','ContactControl.R'],'frame_start':1,'frame_end':10})
                cp=await call(client,'create_checkpoint',{'description':'M2 before correction demo','operation':'M2 full demo'})
                await call(client,'diff_scene',{'checkpoint':cp['manifest']})
                baseline=await call(client,'animation_compare_actions',dict(args,before_action=original,after_action=original,frame_start=1,frame_end=10))
                await call(client,'animation_create_action',dict(args,name='M2_DemoEmpty',dry_run=False))
                duplicate=await call(client,'animation_duplicate_action',dict(args,action_name=original,name='M2_ReviewCopy',assign=True,dry_run=False))
                proposal=await call(client,'animation_fix_foot_sliding',dict(args,mode='propose',options={
                    'controls':['ContactControl.L','ContactControl.R'],'confirm_contacts':True,'frame_start':1,'frame_end':10,'blend_frames':3}))
                preview=await call(client,'animation_fix_foot_sliding',{'mode':'preview','operation':proposal['operation']})
                check('preview passes numeric safeguards and retains source assignment',preview['numeric_verification']=='pass' and preview['active_action']==duplicate['action'])
                applied=await call(client,'animation_fix_foot_sliding',{'mode':'apply','operation':proposal['operation']})
                verified=await call(client,'animation_fix_foot_sliding',{'mode':'verify','operation':proposal['operation']})
                check('sliding reduced and original duplicate source intact',applied['metrics']['improvement_percent']>=80 and verified['source_intact'])
                comparison=await call(client,'animation_compare_actions',dict(args,before_action=original,after_action=applied['working_action'],frame_start=1,frame_end=13))
                check('original Action exact content fingerprint unchanged',baseline['before_fingerprint']==comparison['before_fingerprint'])
                images=await call(client,'qa_compare_before_after',dict(args,before_action=original,after_action=applied['working_action'],options={
                    'frame_start':1,'frame_end':31,'step':5,'max_frames':7,'width':320,'height':384,'montage':True}))
                check('before-after artifacts use identical cameras and timing',images['comparable'] and images['state_restored'])
                multiview=await call(client,'qa_render_multiview',dict(args,options={'frame_start':10,'frame_end':10,'width':240,'height':288}))
                await call(client,'qa_capture_viewport',{'max_size':640})
                # M1 preview was already exercised by the baseline test on this version.
                # Here a dry range failure is not substituted for a render; use one small frame.
                await call(client,'qa_render_preview',{'frame_start':10,'frame_end':10,'width':160,'height':96,'max_frames':1})
                final=await call(client,'create_checkpoint',{'description':'M2 corrected demo and original preserved','operation':'M2 final demo state'})
                for name,options in [('animation_cleanup_fcurves',{'channels':['location']}),
                    ('animation_fix_quaternion_flips',{}),('animation_prepare_root_motion',{'root_bone':'MotionBase','frame_start':1,'frame_end':31})]:
                    p=await call(client,name,dict(args,mode='propose',options=options))
                    await call(client,name,{'mode':'preview','operation':p['operation']})
                # Retiming with original fixture: generated correction adds keys but range still unchanged.
                await call(client,'animation_restore_action',dict(args,action_name=original,dry_run=False))
                p=await call(client,'animation_retime',dict(args,mode='propose',options={'scale_factor':1.5,'frame_start':1,'frame_end':31}))
                timed=await call(client,'animation_retime',{'mode':'preview','operation':p['operation']})
                check('retime preview pose verification passes',timed['numeric_verification']=='pass')
                restored=await call(client,'restore_checkpoint',{'checkpoint':cp['manifest'],'dry_run':False})
                delta=await call(client,'diff_scene',{'checkpoint':cp['manifest']})
                check('checkpoint restore returns original scene metadata',delta['counts']=={'added':0,'removed':0,'changed':0})
                restored_compare=await call(client,'animation_compare_actions',dict(args,before_action=original,after_action=original,frame_start=1,frame_end=10))
                check('checkpoint restores original animation content',restored_compare['before_fingerprint']==baseline['before_fingerprint'])
                report.update(sliding=applied['metrics'],before_after=images,multiview=multiview,
                    final_checkpoint=final,restoration=restored,plan=plan,retime=timed['metrics'])
                invoked={'production.'+item['tool'] for item in report['results']}-{'production.asset_register_fixture'}
                check('all 29 M1/M2 tools invoked via MCP',len(invoked)==29 and invoked<=set(report['inventory']))
        check('fixture file hash remains unchanged',sha256(fixture)==report['source_hash'])
        report['status']='pass';save()
        print('M2_DEMO_REPORT='+str(folder/'report.json'),flush=True)
    except Exception as exc:
        report.update(status='fail',error=str(exc));save()
        print('M2_DEMO_FAILED_REPORT='+str(folder/'report.json'),flush=True)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,required=True)
    asyncio.run(run(parser.parse_args().fixture))
