"""Real Blender M3 rig transactions and semantic tests, no GUI/socket."""
import argparse
import os
from pathlib import Path
import json
import sys
import subprocess
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
# M3.1: BLENDER_PRODUCTION_TEST_ARTIFACTS moves large probe output out of the repository
# (default unchanged: <repo>/test-artifacts).
ARTIFACTS = Path(os.environ.get('BLENDER_PRODUCTION_TEST_ARTIFACTS') or ROOT / 'test-artifacts')

def inside(output):
    import bpy
    import types
    sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests/blender'))
    package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')];sys.modules['blender_mcp']=package
    from m3_rig_fixtures import build
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME,transaction
    from blender_mcp.production.rig.semantics import analyze
    from blender_mcp.production.rig.ik_fk import operate
    from blender_mcp.production.qa.multiview import compare_before_after
    from blender_mcp.production.checkpoint import create_checkpoint,restore_checkpoint
    class LocalBridge:
        def run(self,body,params=None):
            namespace={};exec(build_code(body,params).rsplit('print(',1)[0],namespace);return namespace['result']
    bridge=LocalBridge();storage=Storage(output/'s');manifest=build(output)
    from m31_fixture_support import register_open_fixture
    register_open_fixture(bridge,storage,output)
    report={'status':'running','blender':bpy.app.version_string,'checks':[],'results':{},'benchmark_ms':{},'fixture':manifest}
    def save():
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    def check(name,condition):
        save()
        if not condition:
            raise AssertionError(name)
        report['checks'].append(name);print('PASS '+name,flush=True)
    try:
        for kind in ['semantics','classify','controls','limbs','control_mode','compatibility']:
            started=time.perf_counter();value=analyze(bridge,'M3_MixedRig',{},kind);report['results'][kind]=value
            report['benchmark_ms'][kind]=round((time.perf_counter()-started)*1000,3)
            check('rig '+kind+' structured',value['armature']=='M3_MixedRig')
        check('mixed limbs classified',report['results']['classify']['classification']=='mixed_ik_fk')
        unusual=analyze(bridge,'M3_UnusualRig');report['results']['unusual']=unusual
        check('unusual names still infer feet',{'left_foot','right_foot'}<={a['semantic'] for a in unusual['assignments']})
        check('unnamed anatomical confidence bounded',all(a['confidence']<.9 for a in unusual['assignments'] if a['semantic'].endswith('_foot')))
        unknown=analyze(bridge,'M3_UnknownRig',{},'compatibility')
        check('unknown rig explicit unsupported',unknown['classification']=='unknown' and unknown['capabilities']['ik_fk_matching']=='unsupported_or_ambiguous')
        switch=analyze(bridge,'M3_SwitchRig',{},'control_mode')
        check('custom switch relationship identified',any(l['switches'] for l in switch['limbs']))
        original=bridge.run(BASE_RUNTIME+"\nresult={'snapshot':m2_snapshot(bpy.data.actions['M3_MixedMotion'])}")['snapshot']
        results={}
        for direction,side in [('fk_to_ik','R'),('ik_to_fk','L')]:
            for bake in [False,True]:
                name=direction+('_bake' if bake else '_match')
                options={'chain':['UpperLeg.'+side,'LowerLeg.'+side,'Foot.'+side],
                    'ik_target':'ContactControl.'+side,'pole_target':'PoleControl.'+side,'confirm_mapping':True,
                    'frame':5,'frame_start':1,'frame_end':9,'step':1}
                proposal=operate(bridge,storage,name,direction,bake,'M3_MixedRig','propose',options)
                report['results'][name+'_proposal']=proposal
                check(name+' supported',proposal['proposal']['supported'])
                started=time.perf_counter();preview=operate(bridge,storage,name+'p',direction,bake,mode='preview',operation=proposal['operation'])
                report['benchmark_ms'][name]=round((time.perf_counter()-started)*1000,3)
                report['results'][name]=preview;save()
                check(name+' numerically verified',preview['numeric_verification']=='pass')
                check(name+' original assignment restored',bpy.data.objects['M3_MixedRig'].animation_data.action.name=='M3_MixedMotion')
                verified=operate(bridge,storage,name+'v',direction,bake,mode='verify',operation=proposal['operation'])
                check(name+' independently verified',verified['numeric_verification']=='pass')
                results[name]=preview
        check('original Action exact snapshot preserved',original==bridge.run(BASE_RUNTIME+"\nresult={'snapshot':m2_snapshot(bpy.data.actions['M3_MixedMotion'])}")['snapshot'])
        options={'chain':['UpperLeg.L','LowerLeg.L','Foot.L'],'ik_target':'ContactControl.L','pole_target':'PoleControl.L',
            'confirm_mapping':True,'frame_start':1,'frame_end':9,'step':1,
            'switch':{'owner':'$object','property':'ik_fk','ik_value':1,'fk_value':0}}
        proposal=operate(bridge,storage,'switch','fk_to_ik',True,'M3_SwitchRig','propose',options)
        report['results']['switch_proposal']=proposal
        check('validated driver adapter supported',proposal['proposal']['supported'])
        applied=operate(bridge,storage,'switcha','fk_to_ik',True,mode='apply',operation=proposal['operation'])
        report['results']['switch_apply']=applied
        check('switch bake applies candidate',applied['numeric_verification']=='pass' and bpy.data.objects['M3_SwitchRig'].animation_data.action.name==applied['working_action'])
        source_action=bpy.data.actions['M3_SwitchMotion'];source_action['stale_probe']=True
        try:
            operate(bridge,storage,'switchstale','fk_to_ik',True,mode='revert',operation=proposal['operation'])
        except ValueError:
            check('stale revert preserves active candidate',bpy.data.objects['M3_SwitchRig'].animation_data.action.name==applied['working_action'])
        else:
            raise AssertionError('Stale revert accepted')
        del source_action['stale_probe']
        reverted=operate(bridge,storage,'switchr','fk_to_ik',True,mode='revert',operation=proposal['operation'])
        check('revert preserves candidate and switch source',reverted['reverted'] and bpy.data.objects['M3_SwitchRig']['ik_fk']==0 and applied['working_action'] in bpy.data.actions)
        options['confirm_mapping']=False
        unsupported=operate(bridge,storage,'unsafe','fk_to_ik',True,'M3_SwitchRig','propose',options)
        check('unreviewed mapping not mutable',not unsupported['proposal']['supported'])
        try:
            operate(bridge,storage,'unsafeapply','fk_to_ik',True,mode='apply',operation=unsupported['operation'])
        except ValueError:
            check('unsupported mapping cannot apply',True)
        else:
            raise AssertionError('Unsupported mapping applied')
        switch_rig=bpy.data.objects['M3_SwitchRig'];driver=switch_rig.animation_data.drivers[0].driver
        expression=driver.expression;driver.expression='switch*0.5';options['confirm_mapping']=True
        try:
            rejected=operate(bridge,storage,'complexdriver','fk_to_ik',True,'M3_SwitchRig','analyze',options)
            check('arbitrary driver expression rejected',not rejected['proposal']['supported'])
        finally:
            driver.expression=expression;switch_rig.update_tag();bpy.context.view_layer.update()
        empty=bpy.data.objects.new('M3_EmptyTarget',None);bpy.context.collection.objects.link(empty)
        backend=r'''
def m3_allowed_drivers(obj,options):
    return []
def m3_context(obj,options):
    return m3_context_base(obj)
def m3_propose(obj,source,options):
    return {'supported':True,'candidate_policy':'new'}
def m3_apply(obj,source,candidate,proposal,options):
    obj.location.x=.5
    obj.keyframe_insert(data_path='location',frame=5)
    return {'keys_created':3}
def m3_verify(obj,source,candidate,proposal,options):
    m3_assign(obj,candidate);m3_set_frame(5)
    error=abs(obj.location.x-.5)
    return {'passed':error<1e-6,'metrics':{'max_position_error_m':error},'warnings':[]}
'''
        p=transaction(bridge,storage,'empty','empty_action',backend,empty.name,'propose')
        preview=transaction(bridge,storage,'emptyp','empty_action',backend,mode='preview',operation=p['operation'])
        check('noAction preview creates candidate and restores absent animation data',preview['numeric_verification']=='pass' and empty.animation_data is None and empty.location.x==0)
        verify=transaction(bridge,storage,'emptyv','empty_action',backend,mode='verify',operation=p['operation'])
        check('noAction independent verification restores noAction state',verify['numeric_verification']=='pass' and empty.animation_data is None)
        applied_empty=transaction(bridge,storage,'emptya','empty_action',backend,mode='apply',operation=p['operation'])
        check('noAction apply activates new candidate',applied_empty['numeric_verification']=='pass' and empty.animation_data.action.name==applied_empty['working_action'])
        transaction(bridge,storage,'emptyr','empty_action',backend,mode='revert',operation=p['operation'])
        check('noAction revert restores noData and original transforms',empty.animation_data is None and empty.location.x==0)
        empty.hide_render=True;empty.keyframe_insert(data_path='hide_render',frame=5)
        try:
            transaction(bridge,storage,'untracked','empty_action',backend,empty.name,'analyze')
        except ValueError:
            check('unrestorable animated RNA rejected',True)
        else:
            raise AssertionError('Unrestorable RNA accepted')
        visual=compare_before_after(bridge,storage,'rigvisual','M3_MixedRig','M3_MixedMotion',results['fk_to_ik_bake']['working_action'],
            {'frame_start':1,'frame_end':9,'step':4,'max_frames':3,'width':256,'height':320,'montage':True})
        report['results']['demo_a_visual']=visual
        check('demo A comparable eighteen multiview images',visual['comparable'] and visual['state_restored'] and len(visual['files'])==18)
        final=create_checkpoint(bridge,storage,'M3 Demo A original and IK/FK candidates','rigfinal','M3 Demo A')
        report['results']['final_checkpoint']=final
        restored=restore_checkpoint(bridge,storage,final['manifest'],'rigrestore',False)
        check('checkpoint restored to a new work copy',bpy.data.filepath!=manifest['blend'])
        check('checkpoint restored original animation exactly',original==bridge.run(BASE_RUNTIME+"\nresult={'snapshot':m2_snapshot(bpy.data.actions['M3_MixedMotion'])}")['snapshot'])
        report.update(status='pass',count=len(report['checks']));save()
        print('M3_RIG_REPORT='+str(output/'report.json'))
    except Exception as exc:
        report.update(status='fail',error=str(exc));save();raise

def run(blender):
    output=ARTIFACTS/('m3rig-'+uuid4().hex[:8]);output.mkdir(parents=True)
    process=subprocess.run([blender,'--background','--factory-startup','--disable-autoexec','--python-exit-code','1',
        '--python',str(Path(__file__).resolve()),'--','--inside',str(output)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=240)
    (output/'blender.log').write_text(process.stdout+'\n'+process.stderr,encoding='utf-8')
    if process.returncode:
        print(process.stdout[-15000:]+process.stderr[-3000:]);raise RuntimeError(str(output))
    report=json.loads((output/'report.json').read_text());print(json.dumps({'status':report['status'],'count':report['count'],'report':str(output/'report.json')}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--inside',type=Path);parser.add_argument('--blender')
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else sys.argv[1:])
    inside(args.inside) if args.inside else run(args.blender)
