"""Procedural full FK skeletons, mismatched rest arms, root motion and noisy mocap."""
import json
import math
from pathlib import Path


def build(output):
    import bpy
    from mathutils import Vector
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.frame_start = 1; scene.frame_end = 21
    scene.unit_settings.system = 'METRIC'; scene.unit_settings.scale_length = 1
    definitions = [
        ('root', None, (0, 0, 0), (0, 0, .2)),
        ('pelvis', 'root', (0, 0, 1), (0, 0, 1.2)),
        ('spine', 'pelvis', (0, 0, 1.2), (0, 0, 1.6)),
        ('neck', 'spine', (0, 0, 1.6), (0, 0, 1.7)),
        ('head', 'neck', (0, 0, 1.7), (0, 0, 1.95)),
    ]
    for side, sign in (('left', 1), ('right', -1)):
        definitions += [
            (side+'_upper_leg', 'pelvis', (.15*sign,0,1), (.15*sign,-.025,.55)),
            (side+'_lower_leg', side+'_upper_leg', (.15*sign,-.025,.55), (.15*sign,0,.1)),
            (side+'_foot', side+'_lower_leg', (.15*sign,0,.1), (.15*sign,-.22,.05)),
            (side+'_toe', side+'_foot', (.15*sign,-.22,.05), (.15*sign,-.35,.05)),
            (side+'_upper_arm', 'spine', (.1*sign,0,1.55), (.45*sign,0,1.55)),
            (side+'_lower_arm', side+'_upper_arm', (.45*sign,0,1.55), (.78*sign,0,1.55)),
            (side+'_hand', side+'_lower_arm', (.78*sign,0,1.55), (.93*sign,0,1.55)),
        ]
    source_names = {role: 'Capture_' + role for role, _, _, _ in definitions}
    target_names = {role: 'Joint_' + str(index*7+13) for index,(role, _, _, _) in enumerate(definitions)}

    def rig(name, names, scale, mismatch):
        data = bpy.data.armatures.new(name+'Data')
        obj = bpy.data.objects.new(name, data); scene.collection.objects.link(obj)
        bpy.context.view_layer.objects.active=obj; obj.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        for role,parent,head,tail in definitions:
            bone = data.edit_bones.new(names[role]); h=Vector(head); t=Vector(tail)
            if mismatch and ('arm' in role or 'hand' in role):
                for point in (h,t):
                    point.z -= max(0,abs(point.x)-.1)*.35
            bone.head=h*scale;bone.tail=t*scale
            if parent:
                bone.parent=data.edit_bones[names[parent]]
        bpy.ops.object.mode_set(mode='OBJECT');obj.select_set(False)
        for bone in obj.pose.bones:
            bone.rotation_mode='QUATERNION'
        return obj

    source=rig('M3_MocapSource',source_names,1,False)
    target=rig('M3_MocapTarget',target_names,1.2,True)
    # Uniform source motion deliberately retains sliding, plus low-amplitude alternating jitter.
    source.animation_data_create(); action=bpy.data.actions.new('M3_RawMocap')
    source.animation_data.action=action
    for frame in range(1,22):
        phase=(frame-1)/20
        for role,_,_,_ in definitions:
            bone=source.pose.bones[source_names[role]]
            if role=='root':
                bone.location=(phase*.2,0,0)
            angle=0.05*math.sin(phase*math.pi*2) if role in ('left_upper_arm','right_upper_arm') else 0
            bone.rotation_quaternion=(math.cos(angle/2), math.sin(angle/2),0,0)
            bone.keyframe_insert(data_path='location',frame=frame)
            bone.keyframe_insert(data_path='rotation_quaternion',frame=frame)
            bone.keyframe_insert(data_path='scale',frame=frame)
    action.use_fake_user=True
    source.animation_data.action_slot=action.slots[0]
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    for point in curve.keyframe_points:
                        point.interpolation='LINEAR'
    noisy=action.copy();noisy.name='M3_NoisyMocap';noisy.use_fake_user=True
    root_path=source.pose.bones[source_names['root']].path_from_id()+'.location'
    for layer in noisy.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    if curve.data_path==root_path and curve.array_index==0:
                        for point in curve.keyframe_points:
                            point.co.y += .0004*((-1)**int(point.co.x))
    target.animation_data_create();target_action=bpy.data.actions.new('M3_TargetOriginal')
    target.animation_data.action=target_action
    for bone in target.pose.bones:
        for frame in (1,21):
            bone.keyframe_insert(data_path='rotation_quaternion',frame=frame)
            bone.keyframe_insert(data_path='location',frame=frame)
    target_action.use_fake_user=True;target.animation_data.action_slot=target_action.slots[0]
    # Proxy skinned cylinders make QA renders useful without external assets.
    for obj, names, color in ((source,source_names,(.15,.5,.9,1)),(target,target_names,(.9,.35,.08,1))):
        material=bpy.data.materials.new(obj.name+'_ProxyMaterial');material.diffuse_color=color
        for role in ('pelvis','spine','head','left_upper_arm','right_upper_arm','left_lower_arm','right_lower_arm','left_upper_leg','right_upper_leg','left_lower_leg','right_lower_leg','left_foot','right_foot'):
            bone=obj.data.bones[names[role]]
            midpoint=(bone.head_local+bone.tail_local)/2
            bpy.ops.mesh.primitive_uv_sphere_add(segments=8,ring_count=4,location=midpoint)
            mesh=bpy.context.object;mesh.name=obj.name+'_'+role+'_Proxy'
            mesh.scale=(.045,.045,bone.length*.46)
            mesh.rotation_mode='QUATERNION';mesh.rotation_quaternion=Vector((0,0,1)).rotation_difference((bone.tail_local-bone.head_local).normalized())
            bpy.ops.object.transform_apply(location=False,rotation=True,scale=True)
            group=mesh.vertex_groups.new(name=bone.name);group.add(list(range(len(mesh.data.vertices))),1,'REPLACE')
            modifier=mesh.modifiers.new('Armature','ARMATURE');modifier.object=obj
            mesh.data.materials.append(material)
            mesh.hide_render = obj == source
    bpy.ops.mesh.primitive_plane_add(size=6,location=(0,0,0))
    floor=bpy.context.object;floor.name='M3_MocapFloor'
    mapping=[{'source_bone':source_names[role],'target_bone':target_names[role],'semantic_role':role,
              'confidence':1.0,'evidence':['Controlled fixture correspondence verified from hierarchy/rest geometry']}for role,_,_,_ in definitions]
    scene.frame_set(1)
    path=Path(output)/'m3-mocap-fixture.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    feet=[]
    for side in ('left','right'):
        bone=target.data.bones[target_names[side+'_foot']]
        x=bone.head_local.x
        feet.append({'bone':bone.name,'heel':list(bone.matrix_local.inverted()@Vector((x,.025,0))),
                     'sole':list(bone.matrix_local.inverted()@Vector((x,-.12,0))),
                     'toe':list(bone.matrix_local.inverted()@Vector((x,-.3,0))),
                     'chain':[target_names[side+'_'+role]for role in ('upper_leg','lower_leg','foot')]})
    result={'path':str(path),'source':source.name,'target':target.name,'source_action':action.name,
            'target_action':target_action.name,'noisy_action':noisy.name,'mapping':mapping,
            'surfaces':[floor.name],'feet':feet,
            'scenarios':['mocap_source','differently_named_target','rest_pose_mismatch','root_motion','foot_sliding','noisy_mocap'],
            'source_bones':len(definitions),'target_bones':len(definitions)}
    (Path(output)/'fixture.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result
