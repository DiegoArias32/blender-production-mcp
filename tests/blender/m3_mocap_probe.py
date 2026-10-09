"""Real installed Blender validation of semantic mocap mapping and retarget lifecycle."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
# M3.1: BLENDER_PRODUCTION_TEST_ARTIFACTS moves large probe output out of the repository
# (default unchanged: <repo>/test-artifacts).
ARTIFACTS = Path(os.environ.get('BLENDER_PRODUCTION_TEST_ARTIFACTS') or ROOT / 'test-artifacts')


def coordinate_check(folder):
    import bpy
    import types
    import math
    sys.path.insert(0,str(ROOT/'src'))
    package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')];sys.modules['blender_mcp']=package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.mocap.service import analyze,retarget
    class LocalBridge:
        def run(self,body,params=None):
            namespace={};exec(build_code(body,params).rsplit('print(',1)[0],namespace);return namespace['result']
    bridge=LocalBridge();fixture=json.loads((folder/'fixture.json').read_text(encoding='utf-8'))
    source=bpy.data.objects[fixture['source']];target=bpy.data.objects[fixture['target']]
    source.rotation_euler.z=math.pi/2;target.rotation_euler.z=math.pi/2;source.scale=(2,2,2)
    bpy.context.view_layer.update()
    measured=analyze(bridge,source.name)
    root=measured['root_motion'][0]['displacement_m']
    assert abs(root[0])<1e-6 and abs(root[1]-.4)<1e-6,root
    target.animation_data.action=bpy.data.actions[fixture['target_action']];target.animation_data.action_slot=target.animation_data.action.slots[0]
    options={'source_armature':source.name,'mapping':fixture['mapping'],'mapping_approved':True,'scale_mode':'explicit','scale_factor':1.0,'frame_start':1,'frame_end':21}
    storage=Storage(folder/'s')
    from m31_fixture_support import register_open_fixture
    register_open_fixture(bridge,storage,folder)
    proposal=retarget(bridge,storage,'coords-propose',target.name,'propose',options)
    result=retarget(bridge,storage,'coords-preview',mode='preview',operation=proposal['operation'])
    assert result['numeric_verification']=='pass',result
    displacement=result['metrics']['target_qa']['root_motion']['displacement_m']
    assert abs(displacement[0])<1e-6 and abs(displacement[1]-.4)<1e-6,displacement
    report={'status':'pass','count':3,'checks':['root analysis uses world XYZ under rotated object','explicit scale compensates different uniform object scales','retarget verifies world XYZ after object rotation'],
            'source_world_displacement_m':root,'target_world_displacement_m':displacement,'retarget':result}
    source.scale=(1,1,1);source.rotation_euler=(math.pi/2,0,0);target.rotation_euler=(math.pi/2,0,0)
    upright=source.animation_data.action.copy();upright.name='M3_TiltedWorldInPlace';upright.use_fake_user=True
    root_name=fixture['mapping'][0]['source_bone'];root_path=source.pose.bones[root_name].path_from_id()+'.location'
    for layer in upright.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    if curve.data_path==root_path:
                        for point in curve.keyframe_points:
                            point.co.y=(point.co.x-1)/20*[0,.2,.1][curve.array_index]
    source.animation_data.action=upright;source.animation_data.action_slot=upright.slots[0];bpy.context.scene.frame_set(1)
    in_place=retarget(bridge,storage,'world-in-place',target.name,'propose',dict(options,root_motion='in_place'))
    in_place_preview=retarget(bridge,storage,'world-in-place-preview',mode='preview',operation=in_place['operation'])
    vertical=in_place_preview['metrics']['target_qa']['root_motion']['displacement_m']
    assert in_place_preview['numeric_verification']=='pass' and max(abs(v)for v in vertical[:2])<1e-6 and abs(vertical[2]+.1)<1e-6,vertical
    report['checks'].append('in-place under object X90 removes world horizontal motion and preserves world vertical motion')
    report['world_in_place_displacement_m']=vertical
    source.rotation_euler=(0,0,0);source.location=(0,0,0);bpy.context.scene.frame_set(1)
    source.keyframe_insert(data_path='location',frame=1)
    source.location=(.3,0,0);source.keyframe_insert(data_path='location',frame=21)
    bpy.context.scene.frame_set(1)
    animated_object=analyze(bridge,source.name)
    animated_displacement=animated_object['root_motion'][0]['displacement_m']
    assert abs(animated_displacement[0]-.3)<1e-6,animated_displacement
    report['checks'].append('read-only mocap root analysis includes animated object transforms per frame')
    report['animated_object_root_displacement_m']=animated_displacement
    report['count']=len(report['checks'])
    (folder/'coordinate-check.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('M3_COORDINATE_CHECK='+str(folder/'coordinate-check.json'))


def inside(output):
    import bpy
    import types
    sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests/blender'))
    package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')];sys.modules['blender_mcp']=package
    from m3_mocap_fixtures import build
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage,sha256
    from blender_mcp.production.common.operations import fingerprint
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME
    from blender_mcp.production.mocap.service import analyze,map_skeleton,retarget,cleanup
    from blender_mcp.production.contacts.service import analyze_contacts,analyze_limbs
    from blender_mcp.production.deformation import run as deformation
    from blender_mcp.production.qa.multiview import compare_before_after
    class LocalBridge:
        def run(self,body,params=None):
            namespace={};exec(build_code(body,params).rsplit('print(',1)[0],namespace)
            return namespace['result']
    bridge=LocalBridge();storage=Storage(output/'s');fixture=build(output);checks=[]
    from m31_fixture_support import register_open_fixture
    register_open_fixture(bridge,storage,output)
    def check(name,condition):
        if not condition:raise AssertionError(name)
        checks.append(name);print('PASS '+name,flush=True)
    def snapshot(name):
        return bridge.run(BASE_RUNTIME+"\nresult={'snapshot':m2_snapshot(bpy.data.actions[params['name']])}",{'name':name})['snapshot']
    source_original=snapshot(fixture['source_action']);target_original=snapshot(fixture['target_action']);fixture_hash=sha256(Path(fixture['path']))
    started=time.perf_counter();result=analyze(bridge,fixture['source']);analysis_ms=(time.perf_counter()-started)*1000
    check('analysis reports root motion and all 19 bones',result['bone_count']==19 and abs(result['root_motion'][0]['distance_m']-.2)<1e-6)
    check('analysis source untouched',snapshot(fixture['source_action'])==source_original)
    inferred=map_skeleton(bridge,fixture['source'],fixture['target'])
    check('inferred mapping never auto-approved',not inferred['validated'])
    validated=map_skeleton(bridge,fixture['source'],fixture['target'],fixture['mapping'],True)
    check('explicit fixture mapping validates',validated['validated'])
    options={'source_armature':fixture['source'],'mapping':fixture['mapping'],'mapping_approved':True,
             'frame_start':1,'frame_end':21,'ground_height_m':.12,'contact_height_m':.03}
    proposal=retarget(bridge,storage,'retarget-proposal',fixture['target'],'propose',options)
    print(json.dumps(proposal,indent=2),flush=True)
    from math import acos,degrees
    rest_errors=[]
    for row in fixture['mapping']:
        relative=bpy.data.objects[fixture['source']].data.bones[row['source_bone']].matrix_local.to_3x3().transposed()@bpy.data.objects[fixture['target']].data.bones[row['target_bone']].matrix_local.to_3x3()
        rest_errors.append(degrees(acos(max(-1,min(1,(sum(relative[i][i]for i in range(3))-1)/2)))))
    check('rest compensated retarget matches independent matrix trace angle',proposal['proposal']['supported'] and 10<proposal['proposal']['rest_pose_compensation_max_degrees']<90 and abs(proposal['proposal']['rest_pose_compensation_max_degrees']-max(rest_errors))<.001)
    preview=retarget(bridge,storage,'retarget-preview',mode='preview',operation=proposal['operation'])
    print(json.dumps(preview,indent=2),flush=True)
    check('preview passes dense sampled transform verification',preview['numeric_verification']=='pass')
    check('preview target assignment restored',bpy.data.objects[fixture['target']].animation_data.action.name==fixture['target_action'])
    started=time.perf_counter();applied=retarget(bridge,storage,'retarget-apply',mode='apply',operation=proposal['operation']);retarget_ms=(time.perf_counter()-started)*1000
    check('retarget new Action assigned',applied['numeric_verification']=='pass' and bpy.data.objects[fixture['target']].animation_data.action.name==applied['working_action'])
    check('source and target originals byte-exact snapshots',snapshot(fixture['source_action'])==source_original and snapshot(fixture['target_action'])==target_original)
    check('retarget scale root displacement measured',abs(applied['metrics']['target_qa']['root_motion']['displacement_m'][0]-.24)<1e-6)
    check('retarget retains and reports source sliding',applied['metrics']['target_qa']['foot_sliding_max_m']>.2)
    check('retarget adds no limb stretch or pop',applied['metrics']['target_qa']['limb_stretch_count']==0 and applied['metrics']['target_qa']['limb_pop_count']==0)
    started=time.perf_counter()
    geometry_contacts=analyze_contacts(bridge,fixture['target'],{'feet':fixture['feet'],'surfaces':fixture['surfaces'],'frame_start':1,'frame_end':21})
    geometry_ms=(time.perf_counter()-started)*1000
    check('real geometry sole contacts report retained sliding',geometry_contacts['metrics']['max_contact_sliding_m']>.22)
    limb_qa=analyze_limbs(bridge,fixture['target'],{'chains':[foot['chain']for foot in fixture['feet']],'frame_start':1,'frame_end':21})
    check('full limb QA detects no introduced pops or stretch',limb_qa['metrics']['pop_count']==0 and limb_qa['metrics']['max_length_ratio']<1.01)
    deform_qa=deformation(bridge,fixture['target'],'animation',{'frames':[1,11,21],'max_samples':3,'contact_frames':[1,21]},[applied['working_action']])
    print('DEFORMATION='+json.dumps(deform_qa),flush=True)
    check('deformation QA restores state and completes',deform_qa.get('state_restored') is True and not deform_qa.get('errors'))
    visual=compare_before_after(bridge,storage,'qa-c',fixture['target'],fixture['target_action'],applied['working_action'],
        {'frame_start':1,'frame_end':21,'step':8,'max_frames':3,'width':256,'height':256,'views':['FRONT','SIDE','THREE_QUARTER']})
    print('VISUAL='+json.dumps(visual),flush=True)
    check('three-view three-frame before-after comparable',visual.get('comparable') is True and len(visual['files'])==18)
    verified=retarget(bridge,storage,'retarget-verify',mode='verify',operation=proposal['operation'])
    check('explicit verify repeats numeric QA',verified['numeric_verification']=='pass')
    bpy.ops.wm.save_as_mainfile(filepath=str(output/'retarget-final.blend'),copy=True)
    reverted=retarget(bridge,storage,'retarget-revert',mode='revert',operation=proposal['operation'])
    check('revert restores old Action and retains candidate',bpy.data.objects[fixture['target']].animation_data.action.name==fixture['target_action'] and applied['working_action'] in bpy.data.actions)
    low=[dict(row) for row in fixture['mapping']];low[0]['confidence']=.5
    bad=retarget(bridge,storage,'mapping-low',fixture['target'],'propose',dict(options,mapping=low))
    check('low confidence proposal is unsupported',not bad['proposal']['supported'])
    count=len(bpy.data.actions)
    try:retarget(bridge,storage,'mapping-low-apply',mode='apply',operation=bad['operation'])
    except ValueError:pass
    else:raise AssertionError('low confidence was applied')
    check('low confidence never allocates candidate',len(bpy.data.actions)==count)
    stale=retarget(bridge,storage,'source-stale',fixture['target'],'propose',options)
    bpy.data.actions[fixture['source_action']]['edited']=1
    try:retarget(bridge,storage,'source-stale-apply',mode='apply',operation=stale['operation'])
    except ValueError:pass
    else:raise AssertionError('stale source accepted')
    del bpy.data.actions[fixture['source_action']]['edited']
    check('edited source invalidates proposal before allocation',len(bpy.data.actions)==count)
    target=bpy.data.objects[fixture['target']]
    stale_rest=retarget(bridge,storage,'rest-stale',fixture['target'],'propose',options)
    target.data.bones[fixture['mapping'][1]['target_bone']].use_local_location=False
    try:retarget(bridge,storage,'rest-stale-apply',mode='apply',operation=stale_rest['operation'])
    except ValueError:pass
    else:raise AssertionError('stale inheritance accepted')
    target.data.bones[fixture['mapping'][1]['target_bone']].use_local_location=True
    check('changed target inheritance invalidates proposal',len(bpy.data.actions)==count)
    stale_solver=retarget(bridge,storage,'solver-stale',fixture['target'],'propose',options)
    source=bpy.data.objects[fixture['source']]
    added=source.pose.bones[fixture['mapping'][0]['source_bone']].constraints.new('COPY_LOCATION')
    try:retarget(bridge,storage,'solver-stale-apply',mode='apply',operation=stale_solver['operation'])
    except ValueError:pass
    else:raise AssertionError('stale source constraint accepted')
    source.pose.bones[fixture['mapping'][0]['source_bone']].constraints.remove(added)
    check('changed source constraint invalidates proposal',len(bpy.data.actions)==count)
    target.pose.bones[fixture['mapping'][0]['target_bone']].lock_location[0]=True
    locked=retarget(bridge,storage,'locked-target',fixture['target'],'propose',options)
    check('locked target is unsupported before allocation',not locked['proposal']['supported'] and len(bpy.data.actions)==count)
    target.pose.bones[fixture['mapping'][0]['target_bone']].lock_location[0]=False
    target.animation_data.action=None
    empty=retarget(bridge,storage,'empty-target',fixture['target'],'propose',dict(options,root_motion='in_place'))
    empty_preview=retarget(bridge,storage,'empty-preview',mode='preview',operation=empty['operation'])
    check('unanimated target preview creates new Action safely',empty_preview['numeric_verification']=='pass' and target.animation_data.action is None)
    check('in-place mode preserves zero horizontal displacement',abs(empty_preview['metrics']['target_qa']['root_motion']['displacement_m'][0])<1e-6)
    target.animation_data.action=bpy.data.actions[fixture['target_action']];target.animation_data.action_slot=target.animation_data.action.slots[0]
    # Independent analytic check: local pelvis X under a 90-degree parent turn must become world Y.
    from math import cos,sin,pi
    parent_turn=bpy.data.actions[fixture['source_action']].copy();parent_turn.name='M3_ParentTurnPelvis';parent_turn.use_fake_user=True
    root_path=source.pose.bones[fixture['mapping'][0]['source_bone']].path_from_id()
    pelvis_path=source.pose.bones[fixture['mapping'][1]['source_bone']].path_from_id()
    for layer in parent_turn.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    if curve.data_path==root_path+'.rotation_quaternion':
                        value=[cos(pi/4),0,sin(pi/4),0][curve.array_index]
                        for point in curve.keyframe_points:point.co.y=value
                    if curve.data_path==pelvis_path+'.location' and curve.array_index==0:
                        for point in curve.keyframe_points:point.co.y=.05
    source.animation_data.action=parent_turn;source.animation_data.action_slot=parent_turn.slots[0]
    turned=retarget(bridge,storage,'parent-turn',fixture['target'],'propose',options)
    turned_preview=retarget(bridge,storage,'parent-turn-preview',mode='preview',operation=turned['operation'])
    check('rotated parent pelvis retarget numeric pass',turned_preview['numeric_verification']=='pass')
    previous=target.animation_data.action
    target.animation_data.action=bpy.data.actions[turned_preview['working_action']];target.animation_data.action_slot=target.animation_data.action.slots[0]
    bpy.context.scene.frame_set(11)
    pelvis_head=target.pose.bones[fixture['mapping'][1]['target_bone']].head.copy()
    check('independent pelvis parent-space compensation',abs(pelvis_head.y-.06)<1e-5 and abs(pelvis_head.x-.12)<1e-5)
    target.animation_data.action=previous;target.animation_data.action_slot=previous.slots[0];bpy.context.scene.frame_set(1)
    source=bpy.data.objects[fixture['source']]
    source.animation_data.action=bpy.data.actions[fixture['noisy_action']];source.animation_data.action_slot=source.animation_data.action.slots[0]
    noisy_before=snapshot(fixture['noisy_action'])
    path=source.pose.bones[fixture['mapping'][0]['source_bone']].path_from_id()+'.location'
    cleanup_proposal=cleanup(bridge,storage,'cleanup-proposal',fixture['source'],'propose','curves',
        {'strategies':['mocap_smoothing'],'allow_smoothing':True,'tolerance':.0005,'data_paths':[path],'array_indices':[0]})
    cleanup_preview=cleanup(bridge,storage,'cleanup-preview',mode='preview',operation=cleanup_proposal['operation'])
    print(json.dumps(cleanup_preview,indent=2),flush=True)
    check('noisy mocap cleanup numerically verified',cleanup_preview['numeric_verification']=='pass')
    check('cleanup deviation bounded',cleanup_preview['metrics']['max_error']<=.0005)
    check('noisy source Action intact',snapshot(fixture['noisy_action'])==noisy_before)
    check('original fixture file SHA256 preserved',sha256(Path(fixture['path']))==fixture_hash)
    report={'status':'pass','count':len(checks),'checks':checks,'blender_version':bpy.app.version_string,
            'fixture':fixture,'analysis':result,'mapping':validated,'retarget':applied,'preview':preview,'cleanup':cleanup_preview,
            'geometry_contacts':geometry_contacts,'limb_qa':limb_qa,'deformation_qa':deform_qa,'visual_qa':visual,'fixture_sha256':fixture_hash,
            'benchmarks':{'analysis_ms':analysis_ms,'retarget_transaction_ms':retarget_ms,'bones':19,'baked_frames':21,'verified_frames':41,
                          'geometry_contact_ms':geometry_ms,
                          'retarget_public_bytes':len(json.dumps(applied).encode())},
            'source_sha256':fingerprint(source_original),'target_sha256':fingerprint(target_original)}
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('M3_MOCAP_REPORT='+str(output/'report.json'))


if __name__=='__main__':
    if '--coordinate-check' in sys.argv:
        coordinate_check(Path(sys.argv[sys.argv.index('--coordinate-check')+1]))
    elif '--inside' in sys.argv:
        inside(Path(sys.argv[sys.argv.index('--inside')+1]))
    else:
        parser=argparse.ArgumentParser();parser.add_argument('--blender',required=True);args=parser.parse_args()
        output=ARTIFACTS/('m3m-'+uuid4().hex[:8]);output.mkdir(parents=True)
        process=subprocess.run([args.blender,'--background','--factory-startup','--disable-autoexec','--python',str(Path(__file__).resolve()),'--','--inside',str(output)],capture_output=True,text=True)
        (output/'stdout.txt').write_text(process.stdout,encoding='utf-8');(output/'stderr.txt').write_text(process.stderr,encoding='utf-8')
        print(process.stdout);print(process.stderr);print('ARTIFACTS='+str(output))
        if process.returncode or not(output/'report.json').exists():raise SystemExit(1)
