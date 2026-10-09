"""Real geometry/curve/IK/FK contact validation, isolated synthetic fixtures."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import time

ROOT=Path(__file__).resolve().parents[2]
# M3.1: BLENDER_PRODUCTION_TEST_ARTIFACTS moves large probe output out of the repository
# (default unchanged: <repo>/test-artifacts).
ARTIFACTS = Path(os.environ.get('BLENDER_PRODUCTION_TEST_ARTIFACTS') or ROOT / 'test-artifacts')


def inside(output):
    import bpy
    import types
    sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests/blender'))
    package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')];sys.modules['blender_mcp']=package
    from m3_contact_fixtures import build,foot_config
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage,sha256
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME
    from blender_mcp.production.contacts.service import analyze_contacts,analyze_limbs,analyze_effector,detect_surface,fix_contacts
    from blender_mcp.production.animation.bezier import analyze_bezier,repair_bezier
    from blender_mcp.production.qa.multiview import compare_before_after
    from blender_mcp.production.checkpoint import create_checkpoint
    class DirectBridge:
        def run(self,body,params=None):
            build_code(body,params or {})
            namespace={'params':params or {},'json':json};exec(body,namespace);return namespace['result']
    bridge=DirectBridge();storage=Storage(output/'s');fixture=build(output)
    from m31_fixture_support import register_open_fixture
    register_open_fixture(bridge,storage,output)
    source_hash=sha256(Path(fixture['file']));checks=[];report={'status':'running','fixture':fixture,'checks':checks}
    def save(): (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    def check(name,condition):
        if not condition: save();raise AssertionError(name)
        checks.append(name);print('PASS '+name,flush=True);save()
    rig=bpy.data.objects[fixture['rig']];source=rig.animation_data.action;source_slot=rig.animation_data.action_slot
    options=fixture['options']
    report['surface']=detect_surface(bridge,rig.name,{'bone':'Foot.L','surfaces':['M3_Slope']})
    check('nearest evaluated slope reports object and sloped normal',report['surface']['contact']['surface_object']=='M3_Slope' and abs(report['surface']['contact']['surface_normal'][1])>.1)
    started=time.perf_counter();analysis=analyze_contacts(bridge,rig.name,options);report['contacts']=analysis
    report['benchmark_contact_ms']=(time.perf_counter()-started)*1000
    phases={r['phase'] for r in analysis['phases']}
    check('heel flat rise swing phases detected',{'HEEL_STRIKE','FOOT_FLAT','HEEL_RISE','SWING'}<=phases)
    no_confirm=fix_contacts(bridge,storage,'no-confirm',rig.name,'propose',options)
    check('unconfirmed contacts cannot mutate',not no_confirm['proposal']['supported'])
    proposal=fix_contacts(bridge,storage,'foot',rig.name,'propose',dict(options,confirm_contacts=True))
    report['proposal']=proposal;check('explicit slope IK contact proposal supported',proposal['proposal']['supported'])
    started=time.perf_counter()
    applied=fix_contacts(bridge,storage,'foot-apply',mode='apply',operation=proposal['operation'])
    report['benchmark_contact_apply_ms']=(time.perf_counter()-started)*1000
    report['applied']=applied;save()
    check('contact correction passes dense trajectory roll derivative knee and stretch gates',applied['numeric_verification']=='pass')
    verified=fix_contacts(bridge,storage,'foot-verify',mode='verify',operation=proposal['operation'])
    check('repeat verification source and candidate intact',verified['source_intact'] and verified['numeric_verification']=='pass')
    check('sliding improves on same source contact labels',applied['metrics']['sliding_after_m']<applied['metrics']['sliding_before_m']*.4)
    check('natural foot roll preserved',applied['metrics']['max_foot_rotation_error_degrees']<.1)
    report['candidate']=applied['working_action']
    report['qa']=compare_before_after(bridge,storage,'visual',rig.name,source.name,applied['working_action'],{'frame_start':1,'frame_end':19,'step':9,'max_frames':3,'width':320,'height':384,'views':['FRONT','SIDE','THREE_QUARTER']})
    check('equivalent before after multiview captures',report['qa']['comparable'] and report['qa']['state_restored'])
    report['foot_qa']=compare_before_after(bridge,storage,'foot-visual',rig.name,source.name,applied['working_action'],{
        'frame_start':3,'frame_end':17,'step':7,'max_frames':3,'width':384,'height':240,
        'views':['SIDE','THREE_QUARTER'],'framing_bones':['Foot.L','Toe.L'],'framing_padding_m':.12,'margin':1.5})
    check('contact closeups use matched cameras',report['foot_qa']['comparable'])
    candidate=rig.animation_data.action
    reverted=fix_contacts(bridge,storage,'foot-revert',mode='revert',operation=proposal['operation'])
    check('revert restores original and preserves candidate',rig.animation_data.action==source and bpy.data.actions.get(candidate.name) is not None)
    rig.animation_data.action=bpy.data.actions['M3_IntentionalFastBezier'];rig.animation_data.action_slot=rig.animation_data.action.slots[0]
    fast=analyze_bezier(bridge,rig.name);report['fast_bezier']=fast
    check('intentional fast clamped Bezier not flagged as overshoot',fast['metrics']['overshoot_findings']==0)
    rig.animation_data.action=bpy.data.actions['M3_BezierHandleOvershoot'];rig.animation_data.action_slot=rig.animation_data.action.slots[0]
    started=time.perf_counter()
    defect=analyze_bezier(bridge,rig.name);report['bezier_defect']=defect
    report['benchmark_bezier_ms']=(time.perf_counter()-started)*1000
    check('artificial Bezier handle overshoot detected',defect['metrics']['overshoot_findings']>0)
    repair=repair_bezier(bridge,storage,'bezier',rig.name,'propose',{'channels':[{'path':'pose.bones["ContactControl.L"].location','index':0}],
        'max_deviation':.02,'max_acceleration_increase':.1})
    repaired=repair_bezier(bridge,storage,'bezier-preview',mode='preview',operation=repair['operation']);report['bezier_repair']=repaired
    check('explicit bounded Bezier repair verified',repaired['numeric_verification']=='pass')
    # A remote key must not dilute validation around the altered short segment.
    long_action=rig.animation_data.action.copy();long_action.name='M3_LongActionOvershoot'
    rig.animation_data.action=long_action;rig.animation_data.action_slot=long_action.slots[0]
    from m2_fixtures import curves
    curve=next(c for c in curves(long_action) if c.data_path=='pose.bones["ContactControl.L"].location' and c.array_index==0)
    curve.keyframe_points.insert(100000,float(curve.keyframe_points[-1].co.y));curve.update()
    long_plan=repair_bezier(bridge,storage,'long-bezier',rig.name,'propose',{'channels':[{'path':curve.data_path,'index':0}],
        'max_deviation':.000001,'max_acceleration_increase':.1})
    long_result=repair_bezier(bridge,storage,'long-preview',mode='preview',operation=long_plan['operation']);report['long_bezier_negative']=long_result
    check('remote key cannot hide short segment repair deviation',long_result['numeric_verification']=='fail' and long_result['metrics']['max_motion_error_native']>.001)
    started=time.perf_counter()
    report['limbs']=analyze_limbs(bridge,'M3_FKContactRig',{'chains':[['upper','lower','terminal']]})
    report['benchmark_action_sampling_ms']=(time.perf_counter()-started)*1000
    check('artificial knee pop detected',report['limbs']['metrics']['pop_count']>0)
    fk=bpy.data.objects['M3_FKContactRig'];fk.animation_data.action=bpy.data.actions['M3_FKSliding'];fk.animation_data.action_slot=fk.animation_data.action.slots[0]
    fk_options={'feet':[{'bone':'terminal','chain':['upper','lower','terminal'],'heel':[0,-.03,0],'sole':[0,.08,0],'toe':[0,.2,0]}],
        'surfaces':['M3_FKFloor'],'confirm_contacts':True}
    fk_plan=fix_contacts(bridge,storage,'fk-plan',fk.name,'propose',fk_options)
    check('explicit unconstrained FK chain safely proposed',fk_plan['proposal']['supported'])
    fk_result=fix_contacts(bridge,storage,'fk-preview',mode='preview',operation=fk_plan['operation']);report['fk']=fk_result;save()
    check('FK correction verified without bone translation',fk_result['numeric_verification']=='pass')
    candidate_fk=bpy.data.actions[fk_result['working_action']]
    from m2_fixtures import curves
    check('FK candidate only has rotation channels',all('rotation_' in c.data_path for c in curves(candidate_fk)))
    # Slope context changes invalidate proposals before duplicate/checkpoint allocation.
    rig.animation_data.action=source;rig.animation_data.action_slot=source_slot;bpy.context.scene.frame_set(1)
    stale=fix_contacts(bridge,storage,'stale-surface',rig.name,'propose',dict(options,confirm_contacts=True))
    floor=bpy.data.objects['M3_Slope'];floor.location.z=.1;bpy.context.view_layer.update()
    rejected=False
    try: fix_contacts(bridge,storage,'stale-apply',mode='apply',operation=stale['operation'])
    except ValueError as exc: rejected='stale' in str(exc)
    finally: floor.location.z=0;bpy.context.view_layer.update()
    check('changed evaluated surface invalidates proposal',rejected)
    floor.keyframe_insert(data_path='location',frame=1)
    rejected=False
    try: analyze_contacts(bridge,rig.name,options)
    except ValueError as exc: rejected='Animated' in str(exc)
    finally: floor.animation_data_clear()
    check('moving surfaces rejected rather than sampled as static',rejected)
    mirror=floor.modifiers.new('ExternalMirror','MIRROR');mirror.mirror_object=fk
    rejected=False
    try: analyze_contacts(bridge,rig.name,options)
    except ValueError as exc: rejected='mirror object' in str(exc)
    finally: floor.modifiers.remove(mirror)
    check('external mirror dependency refused',rejected)
    # Geometry distance avoids false contact over empty coplanar space beyond a platform edge.
    edge_options={'feet':[{'bone':'terminal','chain':['upper','lower','terminal'],'heel':[0,0,0],'sole':[0,0,0],'toe':[0,0,0]}],
        'surfaces':['M3_Step'],'frame_start':1,'frame_end':1}
    fk.location=(-.8,-.6,.16);bpy.context.scene.frame_set(1);bpy.context.view_layer.update()
    edge_candidate=detect_surface(bridge,fk.name,{'bone':'terminal','surfaces':['M3_Step']})
    check('edge adversarial point is coplanar and within nearest search radius',edge_candidate['contact'] is not None
        and edge_candidate['contact']['distance']>.19 and abs(edge_candidate['contact']['signed_distance_m'])<1e-5)
    edge=analyze_contacts(bridge,fk.name,edge_options)
    check('coplanar point outside surface is not contact',all(r['phase']=='SWING' for r in edge['phases']))
    fk.location=(0,0,0)
    fk.scale=(.5,.5,.5);bpy.context.view_layer.update()
    scaled=analyze_limbs(bridge,fk.name,{'chains':[['upper','lower','terminal']],'frame_start':1,'frame_end':1})
    check('world scale does not falsely double limb stretch',abs(scaled['metrics']['max_length_ratio']-1)<1e-5)
    fk.scale=(1,1,1);bpy.context.view_layer.update()
    # Static stair/platform candidate at an explicit local marker.
    hand=bpy.data.objects['M3_HandRig'];hand.animation_data.action=None
    matrix=hand.pose.bones['effector'].matrix.copy();matrix.translation=(0,-.6,.16)
    hand.pose.bones['effector'].matrix=matrix;bpy.context.view_layer.update()
    step=detect_surface(bridge,hand.name,{'bone':'effector','surfaces':['M3_Step'],'generic':True})
    report['stair']=step;check('evaluated stair platform nearest surface found',step['contact'] is not None and step['contact']['surface_object']=='M3_Step')
    hand.pose.bones['effector'].location=(0,0,0)
    # Stretch is reported separately when the solver explicitly permits it.
    solver=rig.pose.bones['LowerLeg.L'].constraints[0];solver.use_stretch=True
    rig.pose.bones['UpperLeg.L'].ik_stretch=1.;rig.pose.bones['LowerLeg.L'].ik_stretch=1.
    original_action=rig.animation_data.action;rig.animation_data.action=None
    control=rig.pose.bones['ContactControl.L'];matrix=control.matrix.copy();matrix.translation=(-.18,0,-1.)
    control.matrix=matrix;bpy.context.view_layer.update()
    stretched=analyze_limbs(bridge,rig.name,{'chains':[['UpperLeg.L','LowerLeg.L','Foot.L']],'frame_start':1,'frame_end':1})
    report['intentional_stretch']=stretched
    check('intentional stretch configuration not automatically failed',stretched['limbs'][0]['stretch_permitted'] and stretched['limbs'][0]['status']=='pass' and stretched['limbs'][0]['max_length_ratio']>1.1)
    solver.use_stretch=False;rig.pose.bones['UpperLeg.L'].ik_stretch=0.;rig.pose.bones['LowerLeg.L'].ik_stretch=0.
    rig.animation_data.action=original_action;rig.animation_data.action_slot=source_slot
    hand=bpy.data.objects['M3_HandRig'];hand.animation_data.action=bpy.data.actions['M3_HandDwell'];hand.animation_data.action_slot=hand.animation_data.action.slots[0]
    hand.pose.bones['effector'].location=(0,0,0);bpy.context.scene.frame_set(1)
    contact=analyze_effector(bridge,hand.name,{'bone':'effector','surfaces':['M3_Wall']})
    report['hand_contact']=contact;check('stationary hand wall dwell detected',len(contact['contacts'])==1)
    hand.animation_data.action=bpy.data.actions['M3_HandNearMoving'];hand.animation_data.action_slot=hand.animation_data.action.slots[0]
    near=analyze_effector(bridge,hand.name,{'bone':'effector','surfaces':['M3_Wall']})
    check('hand moving near wall is not contact',not near['contacts'])
    rig.animation_data.action=source;rig.animation_data.action_slot=source_slot;bpy.context.scene.frame_set(1)
    # A constrained leaf can rotate while its head stays fixed. Head-only
    # protection must not falsely certify that unrelated geometry was preserved.
    before_leaf=create_checkpoint(bridge,storage,'Before adding adversarial protected leaf to synthetic rig','leaf-fixture','controlled fixture structure')
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    rig.select_set(True);bpy.context.view_layer.objects.active=rig
    bpy.ops.object.mode_set(mode='EDIT')
    leaf=rig.data.edit_bones.new('M3_ProtectedLeaf');leaf.head=(0,0,1.8);leaf.tail=(0,.2,1.8);leaf.use_deform=False
    bpy.ops.object.mode_set(mode='OBJECT')
    leaf_pose=rig.pose.bones['M3_ProtectedLeaf'];track=leaf_pose.constraints.new('DAMPED_TRACK')
    track.name='FollowFootWithoutTranslation';track.target=rig;track.subtarget='ContactControl.L';track.track_axis='TRACK_Y'
    bpy.context.view_layer.update()
    guarded=fix_contacts(bridge,storage,'protected-leaf',rig.name,'propose',dict(options,confirm_contacts=True))
    check('stationary leaf adversary reaches numeric verification',guarded['proposal']['supported'])
    blocked=fix_contacts(bridge,storage,'protected-leaf-apply',mode='apply',operation=guarded['operation'])
    report['protected_leaf_negative']=blocked
    check('protected leaf head remains fixed while rotation changes',blocked['metrics']['max_protected_trajectory_error_m']<1e-7
        and blocked['metrics']['max_protected_rotation_error_degrees']>.01 and blocked['metrics']['max_protected_tail_error_m']>.0001)
    check('unrelated orientation mutation fails and restores source',blocked['numeric_verification']=='fail'
        and rig.animation_data.action==source and blocked['source_intact'] and bpy.data.actions.get(blocked['working_action']) is not None)
    check('fixture file bytes remain intact',sha256(Path(fixture['file']))==source_hash)
    report.update(status='pass',count=len(checks),blender=bpy.app.version_string);save()
    print('M3_CONTACT_REPORT='+str(output/'report.json'),flush=True)


if __name__=='__main__':
    args=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else sys.argv[1:]
    parser=argparse.ArgumentParser();parser.add_argument('--inside',type=Path);parser.add_argument('--blender');parsed=parser.parse_args(args)
    if parsed.inside: inside(parsed.inside)
    else:
        output=ARTIFACTS/('m3ct-'+uuid4().hex[:8]);output.mkdir()
        result=subprocess.run([parsed.blender,'--background','--factory-startup','--disable-autoexec','--python-exit-code','1','--python',str(Path(__file__).resolve()),'--','--inside',str(output)],capture_output=True,text=True,encoding='utf8',errors='replace',timeout=240)
        (output/'blender.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf8')
        print(result.stdout[-3500:]+result.stderr[-2000:]);print(str(output/'report.json'))
        sys.exit(result.returncode)
