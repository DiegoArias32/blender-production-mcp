"""Focused actual render/RNA recovery, owned background factory process only.

Host Python uses the existing validated pipe backend; no GUI, socket or artist
feedback. Optional --before is an internal saved payload snapshot, to preserve
the pre-fix failure before measuring the current backend in three object orders.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from blender_mcp.production.common.storage import sha256
from blender_mcp.production.physics import integrity,render,service
from m8_pipe_harness import PipeBridge


FIXTURE = r'''
import bpy
from mathutils import Quaternion

def make_rig(name):
    data=bpy.data.armatures.new(name+'_Data')
    obj=bpy.data.objects.new(name,data);bpy.context.scene.collection.objects.link(obj)
    bpy.ops.object.select_all(action='DESELECT');obj.select_set(True)
    bpy.context.view_layer.objects.active=obj;bpy.ops.object.mode_set(mode='EDIT')
    bone=data.edit_bones.new('ExplicitAnimatedControl');bone.head=(0,0,0);bone.tail=(0,0,1)
    bpy.ops.object.mode_set(mode='OBJECT')
    control=obj.pose.bones['ExplicitAnimatedControl'];control.rotation_mode='QUATERNION'
    for frame,angle in ((1,.02),(3,.08)):
        control.rotation_quaternion=Quaternion((0,0,1),angle)
        control.keyframe_insert('rotation_quaternion',frame=frame)
    return obj

def make_mesh(name):
    data=bpy.data.meshes.new(name+'_Data')
    data.from_pydata([(-.25,-.1,0),(.25,-.1,0),(.25,.1,1),(-.25,.1,1)],[],[(0,1,2,3)])
    data.update();obj=bpy.data.objects.new(name,data)
    bpy.context.scene.collection.objects.link(obj)
    return obj

scene=bpy.context.scene;scene.frame_start=1;scene.frame_end=3
source_rig=make_rig(params['source_prefix']+'_SourceRig')
other_rig=make_rig(params['primary_prefix']+'_OriginalRig')
source=make_mesh(params['source_prefix']+'_SourceMesh')
source.vertex_groups.new(name='ExplicitAnimatedControl').add([0,1,2,3],1,'REPLACE')
modifier=source.modifiers.new('ExplicitBinding','ARMATURE');modifier.object=source_rig
for frame,value in ((1,0.),(3,.1)):
    source.location.x=value;source.keyframe_insert('location',index=0,frame=frame)
face=make_mesh(params['primary_prefix']+'_FaceKey');face.location=(.6,0,0)
face.shape_key_add(name='Basis');smile=face.shape_key_add(name='Smile')
smile.data[2].co.z+=.1
for frame,value in ((1,.1),(3,.3)):
    smile.value=value;smile.keyframe_insert('value',frame=frame)
candidate_rig=source_rig.copy();candidate_rig.data=source_rig.data.copy()
candidate_rig.name=params['owned_prefix']+'_CandidateRig';scene.collection.objects.link(candidate_rig)
candidate=source.copy();candidate.data=source.data.copy()
candidate.name=params['owned_prefix']+'_CandidateMesh';scene.collection.objects.link(candidate)
candidate.modifiers['ExplicitBinding'].object=candidate_rig
for obj in (candidate,candidate_rig):
    obj['production_m8_owned']=True;obj['production_generation_id']=params['generation_id']
    obj['production_candidate_version']=1;obj.hide_render=True
candidate['production_candidate_rig']=candidate_rig.name
scene.frame_set(2,subframe=.25)
bpy.ops.object.select_all(action='DESELECT');source.select_set(True)
bpy.context.view_layer.objects.active=source
result={'source_object':source.name,'primary_objects':[source_rig.name,other_rig.name,face.name],
        'original_rigs':[source_rig.name,other_rig.name],'face':face.name,
        'owned_objects':[candidate.name,candidate_rig.name],'render_candidate':candidate.name,
        'source_render_objects':[source.name],'generation_id':params['generation_id']}
'''

MEASURE = r'''
def probe_manual_state(objects):
    return {'rna':px_capture(objects),'scene':qa_state(bpy.context.scene),
            'selection':sorted(o.name for o in bpy.context.selected_objects),
            'active':bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
            'source_authored':m2_digest({o.name:px_object_raw(o) for o in objects if not o.get('production_m8_owned')})}

def probe_arm_manual(params):
    scene=bpy.context.scene;scene.frame_set(2,subframe=.25)
    for index,name in enumerate(params['original_rigs']+[params['owned_objects'][1]]):
        rig=px_object(name)
        rig.pose.bones['ExplicitAnimatedControl'].rotation_quaternion=Quaternion((0,0,1),.21+index*.13)
    px_object(params['source_object']).location.x=.73
    px_object(params['face']).data.shape_keys.key_blocks['Smile'].value=.63
    bpy.context.view_layer.update()

probe_arm_manual(params)
probe_objects=px_dependencies([px_object(n) for n in [params['source_object']]+params['primary_objects']+params['owned_objects']])
probe_before=probe_manual_state(probe_objects)
probe_result=pr_render(params)
probe_after=probe_manual_state(probe_objects)
result={'render_status':probe_result['status'],'state_restored_claim':probe_result['state_restored'],
        'files':probe_result['files'],'series_count':len(probe_result['series']),
        'before':probe_before,'after':probe_after,'manual_rna_restored':probe_before==probe_after,
        'source_authored_unchanged':probe_before['source_authored']==probe_after['source_authored'],
        'animated_candidate_pose_before':probe_before['rna']['objects'][params['owned_objects'][1]]['pose'],
        'animated_candidate_pose_after':probe_after['rna']['objects'][params['owned_objects'][1]]['pose']}
'''

NATIVE_CAPTURE = r'''
originals=[px_object(n) for n in [params['source_object']]+params['primary_objects']]
source_before=m2_digest({o.name:px_object_raw(o) for o in originals})
collection=bpy.data.collections.new('NativeGuard_'+params['generation_id'])
collection['production_m8_owned']=True;collection['production_generation_id']=params['generation_id']
collection['production_candidate_version']=1;bpy.context.scene.collection.children.link(collection)
collider=px_object('M8_StaticColliderSource').copy();collider.data=collider.data.copy()
collider.name='NativeGuardCollider_'+params['generation_id'][:8];collection.objects.link(collider)
collider['production_m8_owned']=True;collider['production_generation_id']=params['generation_id']
collider['production_candidate_version']=1;collider.modifiers.new('ExplicitCollision','COLLISION')
target=px_object(params['render_candidate']);modifier=target.modifiers.new('NativeCaptureProbe','CLOTH')
modifier.collision_settings.collection=collection
modifier.point_cache.use_disk_cache=False;modifier.point_cache.use_external=False
source,primary,deps=px_selection(params)
deps=px_dependencies(deps+[px_object(n) for n in params['owned_objects']]+[target]+[px_object(n) for n in params['source_render_objects']])
prod_require_evaluable(deps);px_validate_objects(deps)
captured=px_capture(deps)
source_after=m2_digest({o.name:px_object_raw(o) for o in originals})
result={'passed':source_before==source_after and px_capture(deps)==captured,
        'native_modifier_type':modifier.type,'point_cache_is_baked':modifier.point_cache.is_baked,
        'dependencies':[o.name for o in deps],'source_authored_unchanged':source_before==source_after,
        'qualification':'RNA_DEPENDENCY_CAPTURE_ONLY_NO_SIMULATION_OR_ARTISTIC_QA'}
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output');parser.add_argument('--before')
    parser.add_argument('--native-only',action='store_true')
    args=parser.parse_args()
    out=Path(args.output).resolve() if args.output else Path(tempfile.gettempdir())/('bpm8render-'+uuid4().hex[:8])
    out.mkdir(parents=True,exist_ok=False)
    report={'schema':'m8.actual_render_rna_restore.v1','created_at':datetime.now(timezone.utc).isoformat(),
            'output':str(out),'backend_before':service.backend_fingerprint(),'cases':[],
            'visual_review':'NOT_PERFORMED_TECHNICAL_RESTORE_PROBE_ONLY','errors':[]}
    def save():
        (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    save()
    bridge=PipeBridge(out/'worker',variant='proxy_cube',fps=30,duration=1)
    fixture_path=Path(bridge.fixture['source_path']);disk_before=sha256(fixture_path)
    try:
        report['blender']=bridge.fixture['blender']
        if args.native_only:
            runs=[('native_guard',('A','B','C'),None)]
        elif args.before:
            legacy=json.loads(Path(args.before).read_text(encoding='utf-8'))
            runs=[('pre_fix',('A','B','C'),legacy['render_payload'])]
        else:
            runs=[('current_'+str(index),order,render.PAYLOAD) for index,order in enumerate(
                [('A','B','C'),('C','A','B'),('B','C','A')],1)]
        for label,order,payload in runs:
            generation=uuid4().hex
            fixture=bridge.run(FIXTURE,{'source_prefix':label+'_'+order[0],
                'primary_prefix':label+'_'+order[1],'owned_prefix':label+'_'+order[2],'generation_id':generation})
            if payload is None:
                measured=bridge.run(integrity.RUNTIME+NATIVE_CAPTURE,fixture)
                report['cases'].append({'case':label,'order':list(order),'params':fixture,'result':measured})
                save();continue
            directories={}
            for case in ('PRIMARY_ONLY','PRIMARY_PLUS_SECONDARY'):
                directories[case]={}
                for view in render.VIEWS:
                    folder=out/label/case/view;folder.mkdir(parents=True,exist_ok=False)
                    directories[case][view]=str(folder)
            params=dict(fixture,frames=[1,2,3],width=64,height=64,views=list(render.VIEWS),directories=directories)
            measured=bridge.run(payload.rsplit('result=pr_render(params)',1)[0]+MEASURE,params)
            images=[Path(row['path']) for row in measured['files']]
            measured['all_rendered_files_verified']=len(images)==18 and all(p.is_file() and p.stat().st_size>8 and p.read_bytes()[:8]==b'\x89PNG\r\n\x1a\n' for p in images)
            measured['passed']=measured['manual_rna_restored'] and measured['source_authored_unchanged'] and measured['all_rendered_files_verified'] and measured['state_restored_claim'] and measured['series_count']==6
            record={'case':label,'order':list(order),'params':params,'result':measured}
            report['cases'].append(record);save()
        report['fixture_sha_before']=disk_before;report['fixture_sha_after']=sha256(fixture_path)
        report['fixture_unchanged']=report['fixture_sha_before']==report['fixture_sha_after']
        report['backend_after']=service.backend_fingerprint()
        report['backend_unchanged']=report['backend_after']==report['backend_before']
        report['passed']=report['fixture_unchanged'] and report['backend_unchanged'] and all(c['result']['passed'] for c in report['cases'])
        if args.before:report['pre_fix_failure_reproduced']=not report['passed'] and any(not c['result']['manual_rna_restored'] for c in report['cases'])
    except Exception as exc:
        report['errors'].append(repr(exc));report['passed']=False
        raise
    finally:
        bridge.close();save()
    print(json.dumps({'report':str(out/'report.json'),'passed':report['passed'],
                      'pre_fix_failure_reproduced':report.get('pre_fix_failure_reproduced'),
                      'cases':[{'case':c['case'],'passed':c['result']['passed']} for c in report['cases']]}))


if __name__=='__main__':main()
