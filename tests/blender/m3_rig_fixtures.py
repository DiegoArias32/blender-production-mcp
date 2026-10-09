"""Procedural M3 rig fixtures; fresh background Blender only, no external assets."""
from pathlib import Path
from datetime import datetime, timezone
import json
import math
import hashlib
import bpy
from mathutils import Quaternion
from m2_fixtures import definitions, material, segment_mesh, curves
from production_fixtures import _armature


def build(output_dir):
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError('Fresh headless factory-startup required')
    source=output/'before-rig-fixtures.blend'
    if source.exists():
        raise FileExistsError(source)
    bpy.ops.wm.save_as_mainfile(filepath=str(source),copy=True)
    scene=bpy.context.scene;scene.name='M3_RigFixtures'
    scene.frame_start=1;scene.frame_end=9;scene.render.fps=30
    scene.unit_settings.system='METRIC';scene.unit_settings.scale_length=1
    for obj in scene.objects:
        if obj.type=='MESH':
            obj.hide_render=True;obj.hide_set(True)
    scenarios={}
    for label,switch,unusual,deform_only in [('Mixed',False,False,False),('Switch',True,False,False),('Unusual',False,True,False),('DeformOnly',False,False,True)]:
        defs=definitions()
        if deform_only:
            defs=[row for row in defs if row[4]]
            valid={row[0] for row in defs};defs=[(n,h,t,p if p in valid else None,d) for n,h,t,p,d in defs]
        names={row[0]:('j'+str(index*7+11) if unusual else row[0]) for index,row in enumerate(defs)}
        rig=_armature('M3_'+label+'Rig',[(names[n],h,t,names.get(p),d) for n,h,t,p,d in defs])
        rig['fixture_only']=True
        action=bpy.data.actions.new('M3_'+label+'Motion');action.use_fake_user=True
        rig.animation_data_create();rig.animation_data.action=action
        for pose in rig.pose.bones:
            pose.rotation_mode='QUATERNION';pose.ik_stretch=0
        if not deform_only:
            for side in ['L','R']:
                solver=rig.pose.bones[names['LowerLeg.'+side]].constraints.new('IK')
                solver.name='TwoBone.'+side;solver.target=rig;solver.subtarget=names['ContactControl.'+side]
                solver.pole_target=rig;solver.pole_subtarget=names['PoleControl.'+side]
                solver.chain_count=2;solver.use_stretch=False
                solver.influence=1 if side=='L' and not switch else 0
                if switch and side=='L':
                    rig['ik_fk']=0.0
                    curve=solver.driver_add('influence');driver=curve.driver;driver.type='SCRIPTED'
                    var=driver.variables.new();var.name='switch';var.type='SINGLE_PROP'
                    var.targets[0].id=rig;var.targets[0].data_path='["ik_fk"]';driver.expression='switch'
                else:
                    for frame in [1,9]:
                        solver.keyframe_insert(data_path='influence',frame=frame)
        for frame in range(1,10):
            for pose in rig.pose.bones:
                pose.location=(0,0,0);pose.rotation_quaternion=(1,0,0,0);pose.scale=(1,1,1)
            for side,sign in [('L',1),('R',-1)]:
                upper=rig.pose.bones[names['UpperLeg.'+side]]
                lower=rig.pose.bones[names['LowerLeg.'+side]]
                upper.rotation_quaternion=Quaternion((1,0,0),sign*math.radians((frame-1)*1.2))
                lower.rotation_quaternion=Quaternion((1,0,0),sign*math.radians((frame-1)*.7))
                if not deform_only:
                    target=rig.pose.bones[names['ContactControl.'+side]]
                    target.location=(.001*(frame-1),0,.0005*(frame-1))
            for pose in rig.pose.bones:
                for channel in ['location','rotation_quaternion','scale']:
                    pose.keyframe_insert(data_path=channel,frame=frame)
            if switch:
                rig['ik_fk']=0.0;rig.keyframe_insert(data_path='["ik_fk"]',frame=frame)
        for curve in curves(action):
            for key in curve.keyframe_points:
                key.interpolation='CONSTANT' if '.constraints[' in curve.data_path or curve.data_path=='["ik_fk"]' else 'LINEAR'
        scenarios[label.lower()]={'object':rig.name,'action':action.name,'names':names}
        if label!='Mixed':
            rig.hide_set(True);rig.hide_render=True
        else:
            body=material('M3_RigBody',(.1,.45,.7));feet=material('M3_RigFeet',(.95,.4,.08))
            for bone in rig.data.bones:
                if bone.use_deform:
                    size=.06 if bone.name not in {'Pelvis','Chest','Head'} else .14
                    segment_mesh(rig,bone.name,size,size,feet if bone.name.startswith(('Foot','Toe')) else body)
    unknown=_armature('M3_UnknownRig',[('a',(3,0,0),(3,0,1),None,True),('b',(3,0,1),(3,.2,2),'a',True)])
    unknown.hide_set(True);unknown.hide_render=True
    scenarios['unknown']={'object':unknown.name}
    scene.frame_set(5)
    target=output/'m3-rig-fixtures.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(target))
    manifest={'schema':1,'created_at':datetime.now(timezone.utc).isoformat(),'blender':bpy.app.version_string,
        'blend':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'checkpoint':str(source),
        'scenarios':scenarios,'fixture_only':True}
    (output/'fixtures.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest
