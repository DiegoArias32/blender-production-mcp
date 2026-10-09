"""Procedural contact fixtures; only a fresh factory-startup test process."""
from pathlib import Path
import json
import math
import sys
import bpy
from mathutils import Matrix,Vector

sys.path.insert(0,str(Path(__file__).parent))
from production_fixtures import _armature
from m2_fixtures import definitions, add_ik, action_for, curves, segment_mesh, material, duplicate


def plane(name,vertices,faces):
    mesh=bpy.data.meshes.new(name+'Data');mesh.from_pydata(vertices,[],faces);mesh.update()
    obj=bpy.data.objects.new(name,mesh);bpy.context.collection.objects.link(obj)
    return obj


def foot_config():
    return {'bone':'Foot.L','control':'ContactControl.L','heel':[0,-.04,0],
        'sole':[0,.09,0],'toe':[0,.22,0],'chain':['UpperLeg.L','LowerLeg.L','Foot.L']}


def foot_proxy(rig,bone,mat):
    vertices=[bone.matrix_local@Vector((x,y,z)) for z in (.005,.06) for y in (-.04,.22) for x in (-.06,.06)]
    obj=plane('M3_SoleProxy_'+bone.name,vertices,[(0,1,3,2),(4,6,7,5),(0,4,5,1),(2,3,7,6),(0,2,6,4),(1,5,7,3)])
    obj.data.materials.append(mat);group=obj.vertex_groups.new(name=bone.name);group.add(list(range(8)),1,'REPLACE')
    modifier=obj.modifiers.new('Skin','ARMATURE');modifier.object=rig
    return obj


def build(output):
    if bpy.data.filepath or not bpy.app.background: raise RuntimeError('Fresh factory-startup required')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for obj in list(bpy.data.objects): bpy.data.objects.remove(obj,do_unlink=True)
    scene=bpy.context.scene;scene.frame_start=1;scene.frame_end=31;scene.render.fps=30
    scene.unit_settings.system='METRIC';scene.unit_settings.scale_length=1
    rig=_armature('M3_ContactRig',definitions());add_ik(rig)
    for side in ('L','R'):
        for pose in (rig.pose.bones['UpperLeg.'+side],rig.pose.bones['LowerLeg.'+side]): pose.ik_stretch=0
    blue=material('M3_ProxyBlue',(.08,.38,.6));orange=material('M3_ContactOrange',(.9,.32,.035))
    for bone in rig.data.bones:
        if bone.name.startswith('Foot.'): foot_proxy(rig,bone,orange)
        elif bone.use_deform and not bone.name.startswith('Toe.'): segment_mesh(rig,bone.name,.065 if 'Leg' in bone.name else .045,.06,blue)
    slope=-.15
    floor=plane('M3_Slope',[(-2,-2,-2*slope),(2,-2,-2*slope),(2,2,2*slope),(-2,2,2*slope)],[(0,1,2,3)])
    floor.color=(.18,.2,.23,1)
    plane('M3_Step',[(-1,-.8,.16),(1,-.8,.16),(1,-.4,.16),(-1,-.4,.16)],[(0,1,2,3)]).hide_render=True
    plane('M3_Wall',[(-1,-.8,0),(1,-.8,0),(1,-.8,2),(-1,-.8,2)],[(0,1,2,3)]).hide_render=True
    plane('M3_FKFloor',[(1,-1,0),(3,-1,0),(3,1,0),(1,1,0)],[(0,1,2,3)]).hide_render=True
    action=action_for(rig,'M3_SlopeBezierRoll')
    control=rig.pose.bones['ContactControl.L']
    rest=control.bone.matrix_local.copy()
    slope_angle=math.atan(slope)
    right=rig.pose.bones['ContactControl.R']
    right.matrix=Matrix.Rotation(slope_angle,4,'X')@right.bone.matrix_local
    for frame in (1,31):
        right.keyframe_insert(data_path='location',frame=frame);right.keyframe_insert(data_path='rotation_quaternion',frame=frame)
    for frame in (1,3,5,9,13,16,19,21,25,28,31):
        extra=math.radians(-12*(5-frame)/4) if frame<5 else math.radians(15*(frame-13)/6) if frame<=19 and frame>13 else 0
        rotation=Matrix.Rotation(slope_angle+extra,4,'X')@rest
        pivot=Vector((0,-.04,0)) if frame<=13 else Vector((0,.22,0))
        base=Matrix.Rotation(slope_angle,4,'X')@rest
        anchor=base@pivot
        translation=anchor-rotation.to_3x3()@pivot
        translation.x+=.025*min(frame-1,18)/18
        if frame>19:
            translation.y-=.2*(frame-19)/12
            translation.z+=.18*math.sin((frame-19)/12*math.pi)
        rotation.translation=translation
        control.matrix=rotation
        control.keyframe_insert(data_path='location',frame=frame)
        control.keyframe_insert(data_path='rotation_quaternion',frame=frame)
    for curve in curves(action):
        for key in curve.keyframe_points:
            key.interpolation='BEZIER';key.handle_left_type='AUTO_CLAMPED';key.handle_right_type='AUTO_CLAMPED'
    fixture_actions={'slope_bezier_heel_toe':action.name}
    fast=duplicate(rig,action,'M3_IntentionalFastBezier')
    for curve in curves(fast):
        if 'ContactControl.L' in curve.data_path and curve.data_path.endswith('location'):
            for key in curve.keyframe_points: key.co.y*=10
            curve.update()
    fixture_actions['intentional_fast_bezier']=fast.name
    overshoot=duplicate(rig,action,'M3_BezierHandleOvershoot')
    curve=next(c for c in curves(overshoot) if c.data_path.endswith('["ContactControl.L"].location') and c.array_index==0)
    a,b=curve.keyframe_points[0],curve.keyframe_points[1]
    a.handle_right_type='FREE';b.handle_left_type='FREE'
    a.handle_right.y+=.02;b.handle_left.y+=.02;curve.update()
    fixture_actions['handle_overshoot']=overshoot.name
    # A separate unconstrained chain exercises rotation-only FK solving.
    fk=_armature('M3_FKContactRig',[
        ('upper',(2,0,1),(2,-.2,.5),None,True),
        ('lower',(2,-.2,.5),(2,0,0),'upper',True),
        ('terminal',(2,0,0),(2,-.22,0),'lower',True)])
    fk.rotation_mode='QUATERNION'
    for p in fk.pose.bones: p.rotation_mode='QUATERNION'
    for frame in (1,10,20,31):
        for p in fk.pose.bones: p.rotation_quaternion=(1,0,0,0)
        fk.pose.bones['upper'].rotation_quaternion=Matrix.Rotation(math.radians((frame-1)/30),4,'Z').to_quaternion()
        for p in fk.pose.bones:
            p.keyframe_insert(data_path='rotation_quaternion',frame=frame)
    fk.animation_data.action.name='M3_FKSliding';fk.animation_data.action.use_fake_user=True
    fixture_actions['fk_sliding']=fk.animation_data.action.name
    pop=duplicate(fk,fk.animation_data.action,'M3_KneePop')
    lower=fk.pose.bones['lower']
    for frame,angle in ((1,0),(14,0),(15,45),(16,0),(31,0)):
        lower.rotation_quaternion=Matrix.Rotation(math.radians(angle),4,'X').to_quaternion()
        lower.keyframe_insert(data_path='rotation_quaternion',frame=frame)
    fixture_actions['knee_pop']=pop.name
    # Independent effector supports wall dwell and near-wall negative tests.
    hand=_armature('M3_HandRig',[('effector',(0,-.8,1),(0,-.8,1.2),None,False)])
    for frame in (1,31): hand.pose.bones['effector'].keyframe_insert(data_path='location',frame=frame)
    hand.animation_data.action.name='M3_HandDwell';hand.animation_data.action.use_fake_user=True
    fixture_actions['hand_contact']=hand.animation_data.action.name
    near=duplicate(hand,hand.animation_data.action,'M3_HandNearMoving')
    for frame,value in ((1,-.2),(31,.2)):
        hand.pose.bones['effector'].location.x=value
        hand.pose.bones['effector'].keyframe_insert(data_path='location',frame=frame)
    for curve in curves(near):
        for key in curve.keyframe_points: key.interpolation='LINEAR'
    fixture_actions['hand_near_moving']=near.name
    rig.animation_data.action=action;rig.animation_data.action_slot=action.slots[0]
    bpy.context.view_layer.objects.active=rig;scene.frame_set(1)
    for obj in bpy.context.selected_objects: obj.select_set(False)
    rig.select_set(True)
    path=output/'m3-contact-fixtures.blend'
    if path.exists(): raise FileExistsError(path)
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    manifest={'file':str(path),'rig':rig.name,'action':action.name,'actions':fixture_actions,
        'options':{'feet':[foot_config()],'surfaces':[floor.name],'frame_start':1,'frame_end':31}}
    (output/'fixture.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    return manifest
