"""M8 fingerprints for raw sources and the complete primary Action/NLA stack.

Runtime snapshots stay inside Blender; only bounded summaries and equality digests
cross the bridge. Evaluated pose and shape values are not raw-asset signatures.
"""
from __future__ import annotations

from ..common.m3_transaction import BASE_RUNTIME
from ..animation.generation.integrity import INTEGRITY_RUNTIME as MATERIAL_RUNTIME


RUNTIME = BASE_RUNTIME + MATERIAL_RUNTIME + r'''
def px_fail(code, detail):
    raise ValueError(code+': '+detail)

def px_value(value):
    if value is None or isinstance(value,(str,bool,int)):
        return value
    if isinstance(value,float):
        if not math.isfinite(value):
            px_fail('PHYSICS_NONFINITE','raw RNA value')
        return value
    if isinstance(value,set):
        return sorted(value)
    values=list(value)
    if len(values)>32:
        px_fail('PHYSICS_CAPACITY','RNA array exceeds 32 values')
    return [px_value(v) for v in values]

def px_local(item):
    if item is not None and (getattr(item,'library',None) or getattr(item,'override_library',None)):
        px_fail('PHYSICS_LINKED_UNSUPPORTED','linked/override '+item.name)

def px_pointer(item,key):
    # RNA path_resolve returns null StructRNA proxies for some pointers in 5.2.1.
    # These literal accesses preserve Python's real None without leaking addresses.
    if key=='rest_shape_key':
        return item.rest_shape_key
    if key=='object':
        return item.object
    if key=='target':
        return item.target
    if key=='pole_target':
        return item.pole_target
    if key=='collection':
        return item.collection
    if key=='node_group':
        return item.node_group
    if key=='mirror_object':
        return item.mirror_object
    if key=='offset_object':
        return item.offset_object
    return item.path_resolve(key)

def px_collection_record(collection,generation=None,version=None,require_owned=False):
    px_local(collection)
    member_map={o.name:o for o in collection.all_objects}
    members=[member_map[name] for name in sorted(member_map)]
    if len(members)>128:
        px_fail('PHYSICS_CAPACITY','collection exceeds 128 dependency objects')
    if require_owned:
        if collection.get('production_m8_owned') is not True or collection.get('production_generation_id')!=generation or collection.get('production_candidate_version')!=version:
            px_fail('PHYSICS_COLLIDER_OWNERSHIP','collection must belong to this exact generation/version')
        if any(o.get('production_m8_owned') is not True or o.get('production_generation_id')!=generation or o.get('production_candidate_version')!=version for o in members):
            px_fail('PHYSICS_COLLIDER_OWNERSHIP','collection contains a foreign or unowned object')
    return {'name':collection.name,'owned':collection.get('production_m8_owned'),
        'generation_id':collection.get('production_generation_id'),'version':collection.get('production_candidate_version'),
        'objects':[{'name':o.name,'owned':o.get('production_m8_owned'),
                    'generation_id':o.get('production_generation_id'),'version':o.get('production_candidate_version')}
                   for o in members],
        'children':sorted(c.name for c in collection.children)}

def px_rna(item, depth=0):
    # path_resolve reads the installed schema without computed getattr/setattr.
    result={}
    omitted={'rna_type','name','name_full','is_baked','is_baking','is_outdated','info',
             'index','is_valid','error_location','error_rotation','execution_time',
             'select','show_expanded','name_display'}
    for prop in item.bl_rna.properties:
        key=prop.identifier
        nested=prop.type=='POINTER' and key in ('settings','collision_settings','point_cache','effector_weights')
        if key in omitted or prop.type=='COLLECTION' or (prop.is_readonly and not nested):
            continue
        value=px_pointer(item,key) if prop.type=='POINTER' else item.path_resolve(key)
        if prop.type=='POINTER':
            if value is None:
                result[key]=None
            elif isinstance(value,bpy.types.Collection):
                result[key]=px_collection_record(value)
            elif isinstance(value,bpy.types.ID):
                px_local(value)
                result[key]={'name':value.name,'type':value.bl_rna.identifier}
            elif depth<2:
                result[key]=px_rna(value,depth+1)
            else:
                px_fail('PHYSICS_DEPENDENCY_UNSUPPORTED','nested RNA '+key)
        elif prop.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
            result[key]=px_value(value)
        else:
            px_fail('PHYSICS_DEPENDENCY_UNSUPPORTED','RNA '+key)
    return result

def px_curve(curve):
    if curve.modifiers:
        px_fail('PHYSICS_FCURVE_MODIFIER_UNSUPPORTED',curve.data_path)
    if len(curve.keyframe_points)+len(curve.sampled_points)>100000:
        px_fail('PHYSICS_CAPACITY','curve exceeds 100000 points')
    return {'path':curve.data_path,'index':curve.array_index,'mute':curve.mute,
        'lock':curve.lock,'extrapolation':curve.extrapolation,
        'group':curve.group.name if curve.group else None,
        'keys':[[list(p.co),list(p.handle_left),list(p.handle_right),p.interpolation,
                 p.handle_left_type,p.handle_right_type,p.type,p.easing,p.amplitude,p.back,p.period]
                for p in curve.keyframe_points],
        'samples':[list(p.co) for p in curve.sampled_points]}

def px_action(action):
    if action is None:
        return None
    px_local(action)
    curves=action_curves(action,None)
    if sum(len(c.keyframe_points)+len(c.sampled_points) for s,l,t,c in curves)>100000:
        px_fail('PHYSICS_CAPACITY','Action exceeds 100000 points')
    row=m2_snapshot(action)
    for s,l,t,c in curves:
        if c.modifiers:
            px_fail('PHYSICS_FCURVE_MODIFIER_UNSUPPORTED',action.name+' '+c.data_path)
    row['name']=action.name
    row['layer_properties']=[{'name':l.name,'properties':px_rna(l),
        'strips':[{'type':s.type,'properties':px_rna(s)} for s in l.strips]}
        for l in getattr(action,'layers',[])]
    return {'name':action.name,'fingerprint':m2_digest(row),'channels':len(curves),
        'keys':sum(len(c.keyframe_points) for s,l,t,c in curves),
        'slots':[{'handle':s.handle,'identifier':s.identifier,'type':s.target_id_type}
                 for s in getattr(action,'slots',[])]}

def px_animation(item):
    ad=getattr(item,'animation_data',None)
    if not ad:
        return None
    if ad.drivers:
        px_fail('PHYSICS_DRIVER_UNSUPPORTED',item.name)
    if ad.use_tweak_mode:
        px_fail('PHYSICS_NLA_TWEAK_UNSUPPORTED',item.name)
    if len(ad.nla_tracks)>64 or sum(len(t.strips) for t in ad.nla_tracks)>128:
        px_fail('PHYSICS_CAPACITY','primary NLA exceeds 64 tracks/128 strips')
    tracks=[]
    for track in ad.nla_tracks:
        strips=[]
        for strip in track.strips:
            if strip.type!='CLIP' or strip.strips:
                px_fail('PHYSICS_NLA_STRIP_UNSUPPORTED',strip.name)
            if len(strip.fcurves)>64 or sum(len(c.keyframe_points)+len(c.sampled_points) for c in strip.fcurves)>100000:
                px_fail('PHYSICS_CAPACITY','NLA control curves')
            strips.append({'name':strip.name,'properties':px_rna(strip),
                'action':px_action(strip.action),'slot':strip.action_slot.handle if strip.action_slot else None,
                'controls':[{'path':c.data_path,'index':c.array_index,'fingerprint':m2_digest(px_curve(c)),
                             'keys':len(c.keyframe_points)} for c in strip.fcurves]})
        tracks.append({'name':track.name,'mute':track.mute,'solo':track.is_solo,
                       'lock':track.lock,'strips':strips})
    return {'active':px_action(ad.action),'slot':ad.action_slot.handle if ad.action_slot else None,
        'blend':ad.action_blend_type,'influence':ad.action_influence,
        'extrapolation':ad.action_extrapolation,'use_nla':ad.use_nla,'tracks':tracks}

def px_mesh(obj):
    data=obj.data
    for slot in obj.material_slots:
        px_local(slot.material)
    if len(data.vertices)>100000 or len(data.polygons)>250000:
        px_fail('PHYSICS_CAPACITY','raw mesh budget exceeded')
    weights=[sorted([[g.group,float(g.weight)] for g in v.groups]) for v in data.vertices]
    if sum(len(row) for row in weights)>1000000:
        px_fail('PHYSICS_CAPACITY','weight budget exceeded')
    shapes=None
    if data.shape_keys:
        keys=data.shape_keys;px_local(keys)
        if sum(len(k.data) for k in keys.key_blocks)>2000000:
            px_fail('PHYSICS_CAPACITY','shape coordinate budget exceeded')
        shapes={'relative':keys.use_relative,'blocks':[{'name':k.name,
            'relative':k.relative_key.name if k.relative_key else None,'mute':k.mute,
            'group':k.vertex_group,'interpolation':k.interpolation,
            'slider_min':k.slider_min,'slider_max':k.slider_max,
            'coordinates':m2_digest([px_value(p.co) for p in k.data])} for k in keys.key_blocks],
            'animation':px_animation(keys)}
    return {'vertices':m2_digest([px_value(v.co) for v in data.vertices]),
        'edges':m2_digest([list(e.vertices) for e in data.edges]),
        'faces':m2_digest([[list(p.vertices),p.material_index,p.use_smooth] for p in data.polygons]),
        'groups':[[g.name,g.index,g.lock_weight] for g in obj.vertex_groups],
        'weights':m2_digest(weights),'shapes':shapes,
        'materials':[[s.link,m6_integrity_material(s.material)] for s in obj.material_slots]}

def px_pose_configuration(pose):
    # These writable scalar/array families are present in the 5.2.1 RNA probe.
    # Pose transforms/matrices are evaluated state; rotation mode and IK/B-Bone
    # configuration change how the same authored Action deforms the candidate.
    values={}
    for prop in pose.bl_rna.properties:
        name=prop.identifier
        configured=name=='rotation_mode' or name.startswith(('bbone_','lock_ik_','use_ik_','ik_'))
        if not configured or prop.is_readonly:
            continue
        if prop.type not in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
            px_fail('PHYSICS_POSE_CONFIGURATION_UNSUPPORTED','uncertified pose configuration '+name)
        values[name]=px_value(pose.path_resolve(name))
    return values

def px_object_raw(obj):
    px_local(obj);px_local(obj.data)
    if obj.type not in ('MESH','ARMATURE','EMPTY','CURVE','CURVES'):
        px_fail('PHYSICS_OBJECT_UNSUPPORTED',obj.type)
    supported={'ARMATURE','SUBSURF','SOLIDIFY','TRIANGULATE','MIRROR','SMOOTH','DECIMATE','BEVEL',
               'WELD','WEIGHTED_NORMAL','EDGE_SPLIT','CLOTH','COLLISION','MESH_CACHE'}
    for modifier in obj.modifiers:
        if modifier.type not in supported:
            px_fail('PHYSICS_MODIFIER_UNSUPPORTED',modifier.type+' requires a separate certified dependency adapter')
        if modifier.type in ('CLOTH','COLLISION','MESH_CACHE') and obj.get('production_m8_owned') is not True:
            px_fail('PHYSICS_SOURCE_SOLVER_UNSUPPORTED','original external simulation/cache modifier '+modifier.type)
        if modifier.type=='BEVEL' and modifier.profile_type=='CUSTOM':
            px_fail('PHYSICS_MODIFIER_UNSUPPORTED','custom bevel profile is not certified')
    row={'name':obj.name,'type':obj.type,'data':obj.data.name if obj.data else None,
        'parent':obj.parent.name if obj.parent else None,'parent_type':obj.parent_type,'parent_bone':obj.parent_bone,
        'parent_inverse':[list(r) for r in obj.matrix_parent_inverse],
        'basis':[list(r) for r in obj.matrix_basis], 'rotation_mode':obj.rotation_mode,
        'visibility':{'render':obj.hide_render,'viewport':obj.hide_viewport,'hidden':obj.hide_get()},
        'constraints':[{'name':c.name,'type':c.type,'properties':px_rna(c)} for c in obj.constraints],
        'modifiers':[{'name':m.name,'type':m.type,'properties':px_rna(m)} for m in obj.modifiers],
        'animation':px_animation(obj),'data_animation':px_animation(obj.data) if obj.data else None}
    if any(m.type=='COLLISION' for m in obj.modifiers):
        row['collision_settings']=px_rna(obj.collision)
    if obj.type=='MESH':
        row['mesh']=px_mesh(obj)
    elif obj.type=='ARMATURE':
        row['rig']={'pose_position':obj.data.pose_position,'solver':obj.pose.ik_solver,
            'bones':[{'name':b.name,'parent':b.parent.name if b.parent else None,
                'rest':[list(r) for r in b.matrix_local],'head':list(b.head_local),'tail':list(b.tail_local),
                'properties':px_rna(b),
                'pose_configuration':px_pose_configuration(obj.pose.bones[b.name]),
                'locks':[list(obj.pose.bones[b.name].lock_location),list(obj.pose.bones[b.name].lock_rotation),
                         list(obj.pose.bones[b.name].lock_scale)],
                'constraints':[{'name':c.name,'type':c.type,'properties':px_rna(c)}
                    for c in obj.pose.bones[b.name].constraints]} for b in obj.data.bones]}
    elif obj.type in ('CURVE','CURVES'):
        px_fail('UNSUPPORTED_BY_CURRENT_BLENDER_API','native curve geometry requires an inspected adapter')
    return row

def px_object(name):
    obj=bpy.context.scene.objects.get(name)
    if obj is None:
        px_fail('PHYSICS_OBJECT_MISSING',str(name))
    return obj

def px_bound_actions(obj):
    owners=[obj]+([obj.data] if obj.data else [])
    if obj.type=='MESH' and obj.data.shape_keys:
        owners.append(obj.data.shape_keys)
    actions=set()
    for owner in owners:
        ad=getattr(owner,'animation_data',None)
        if ad:
            if ad.action:
                actions.add(ad.action.name)
            actions.update(s.action.name for t in ad.nla_tracks for s in t.strips if s.action)
    return actions

def px_restorable_path(owner,path):
    kind=owner.bl_rna.identifier
    transforms={'location','scale','rotation_euler','rotation_quaternion','rotation_axis_angle'}
    if kind=='Key':
        return path=='eval_time' or any(path==key.path_from_id()+'.value' for key in owner.key_blocks)
    if kind!='Object':
        return False
    if path in transforms:
        return True
    owners=[('',owner)]
    if owner.type=='ARMATURE':
        for bone in owner.pose.bones:
            prefix=bone.path_from_id()
            if path in {prefix+'.'+name for name in transforms}:
                return True
            owners.append((prefix,bone))
    for prefix,item in owners:
        for constraint in item.constraints:
            if path in (constraint.path_from_id()+'.influence',constraint.path_from_id()+'.mute'):
                return True
        local=path[len(prefix):] if prefix and path.startswith(prefix) else path if not prefix else ''
        if local.startswith('[') and local.endswith(']'):
            try:
                key=json.loads(local[1:-1])
            except (ValueError,TypeError):
                return False
            if isinstance(key,str) and key in item:
                value=item[key]
                return isinstance(value,(str,bool,int,float)) and (not isinstance(value,float) or math.isfinite(value))
    return False

def px_validate_animation_owner(owner):
    ad=getattr(owner,'animation_data',None)
    if not ad:
        return
    if ad.drivers:
        px_fail('PHYSICS_DRIVER_UNSUPPORTED',owner.name)
    actions=[]
    if ad.action:
        actions.append((ad.action,ad.action_slot))
    for track in ad.nla_tracks:
        for strip in track.strips:
            if strip.type!='CLIP' or strip.strips:
                px_fail('PHYSICS_NLA_STRIP_UNSUPPORTED',strip.name)
            actions.append((strip.action,strip.action_slot))
    for action,slot in actions:
        if action is None:
            continue
        if getattr(action,'is_action_layered',False) and slot is None:
            px_fail('PHYSICS_ACTION_SLOT_REQUIRED','bound primary Action needs its explicit owner slot')
        for s,l,t,curve in action_curves(action,slot.handle if slot else None):
            if not px_restorable_path(owner,curve.data_path):
                px_fail('PHYSICS_RNA_ANIMATION_UNSUPPORTED',owner.name+' '+curve.data_path)

def px_environment_guard():
    scene=bpy.context.scene
    if scene.rigidbody_world is not None:
        px_fail('PHYSICS_ENVIRONMENT_UNSUPPORTED','global rigid-body world is outside the bounded secondary backend')
    for obj in scene.objects:
        field=getattr(obj,'field',None)
        if field is not None and field.type!='NONE':
            px_fail('PHYSICS_ENVIRONMENT_UNSUPPORTED','global force field '+obj.name)
    if scene.animation_data and (scene.animation_data.action or scene.animation_data.drivers or scene.animation_data.nla_tracks):
        px_fail('PHYSICS_SCENE_ANIMATION_UNSUPPORTED','scene context is animated or driven')
    px_world(scene.world)

def px_validate_objects(objects):
    px_environment_guard()
    for obj in objects:
        if getattr(obj,'rigid_body',None) is not None or getattr(obj,'rigid_body_constraint',None) is not None:
            px_fail('PHYSICS_ENVIRONMENT_UNSUPPORTED','rigid-body source/dependency '+obj.name)
        px_validate_animation_owner(obj)
        if obj.data:
            px_validate_animation_owner(obj.data)
        if obj.type=='MESH':
            if obj.data.shape_keys:
                px_validate_animation_owner(obj.data.shape_keys)
            for slot in obj.material_slots:
                m6_integrity_material(slot.material)

def px_dependencies(seeds):
    queue=list(seeds);found={}
    # Shared slotted M7 Actions bind a rig and Key datablocks to one performance.
    # Follow original owners; a new owned clone must not change the source closure.
    bindings={}
    for obj in bpy.context.scene.objects:
        if obj.get('production_m8_owned') is True:
            continue
        for name in px_bound_actions(obj):
            bindings.setdefault(name,[]).append(obj)
    while queue:
        obj=queue.pop(0)
        if obj.name in found:
            continue
        if len(found)>=128:
            px_fail('PHYSICS_CAPACITY','dependency closure exceeds 128 objects')
        px_local(obj);found[obj.name]=obj
        for name in px_bound_actions(obj):
            queue.extend(bindings.get(name,[]))
        if obj.parent:
            queue.append(obj.parent)
        containers=list(obj.constraints)+list(obj.modifiers)
        if obj.type=='ARMATURE':
            for bone in obj.pose.bones:
                containers.extend(list(bone.constraints))
        for item in containers:
            if getattr(item,'type',None)=='CLOTH':
                collection=item.collision_settings.collection
                if collection is None:
                    px_fail('PHYSICS_COLLIDER_COLLECTION_REQUIRED','global collider scope is not supported')
                px_collection_record(collection,obj.get('production_generation_id'),obj.get('production_candidate_version'),True)
                queue.extend(collection.all_objects)
            for prop in item.bl_rna.properties:
                if prop.type=='COLLECTION' and len(item.path_resolve(prop.identifier)):
                    px_fail('PHYSICS_DEPENDENCY_UNSUPPORTED',item.name+' nonempty collection '+prop.identifier)
                if prop.type!='POINTER' or prop.identifier=='rna_type':
                    continue
                value=px_pointer(item,prop.identifier)
                if isinstance(value,bpy.types.Object):
                    queue.append(value)
                elif isinstance(value,bpy.types.ID) and value is not None:
                    px_fail('PHYSICS_DEPENDENCY_UNSUPPORTED',item.name+' '+prop.identifier)
    return [found[name] for name in sorted(found)]

def px_capture(objects):
    owners=[]
    for obj in objects:
        owners.append(obj)
        if obj.data:
            owners.append(obj.data)
        if obj.type=='MESH' and obj.data.shape_keys:
            owners.append(obj.data.shape_keys)
    return {'objects':{o.name:m3_capture(o) for o in objects},
        'keys':{o.name:{'eval_time':o.data.shape_keys.eval_time,
            'values':{k.name:k.value for k in o.data.shape_keys.key_blocks}}
            for o in objects if o.type=='MESH' and o.data.shape_keys},
        'nla':[(o,t.name,s.name,s.influence,s.strip_time) for o in owners if getattr(o,'animation_data',None)
               for t in o.animation_data.nla_tracks for s in t.strips]}

def px_restore(objects,state):
    if state['objects']:
        first=next(iter(state['objects'].values()))
        bpy.context.scene.frame_set(first['frame'],subframe=first['subframe'])
    for obj in objects:
        record=state['objects'][obj.name]
        m3_set_channels(obj,record['object'])
        for name,values in record['pose'].items():
            bone=obj.pose.bones.get(name)
            if bone is None:
                px_fail('PHYSICS_RESTORE_BONE_MISSING','bone disappeared during state recovery '+name)
            m3_set_channels(bone,values)
        obj.update_tag()
    for obj in objects:
        if obj.name in state['keys']:
            keys=obj.data.shape_keys;record=state['keys'][obj.name]
            keys.eval_time=record['eval_time']
            for name,value in record['values'].items():
                keys.key_blocks[name].value=value
    for owner,track,strip,influence,time in state['nla']:
        item=owner.animation_data.nla_tracks[track].strips[strip]
        item.influence=influence;item.strip_time=time
    bpy.context.view_layer.update()

def px_selection(params):
    source=px_object(params['source_object'])
    primary=[px_object(n) for n in params.get('primary_objects',[]) ]
    if not primary:
        primary=[source]
        if source.type=='MESH':
            primary.extend(m.object for m in source.modifiers if m.type=='ARMATURE' and m.object)
    others=[px_object(n) for n in params.get('colliders',[])]
    if params.get('anchor_object'):
        others.append(px_object(params['anchor_object']))
    chain=params.get('source_chain',params.get('chain'))
    if chain:
        others.append(px_object(chain['armature']))
    deps=px_dependencies([source]+primary+others)
    prod_require_evaluable(deps)
    px_validate_objects(deps)
    return source,list({o.name:o for o in primary}.values()),deps

def px_world(world):
    if world is None:
        return None
    px_local(world);m6_integrity_static(world,'World')
    graph=None
    if world.use_nodes and world.node_tree:
        tree=world.node_tree;m6_integrity_static(tree,'World node tree')
        if len(tree.nodes)>32 or len(tree.links)>64:
            px_fail('PHYSICS_CAPACITY','World graph budget exceeded')
        nodes=[]
        for node in tree.nodes:
            if node.type not in ('BACKGROUND','OUTPUT_WORLD','RGB','VALUE','MIX_SHADER','ADD_SHADER','MATH','CLAMP'):
                px_fail('PHYSICS_DEPENDENCY_UNSUPPORTED','World node '+node.type)
            inputs=[]
            for socket in node.inputs:
                inputs.append({'identifier':socket.identifier,
                    'default':m6_integrity_value(socket.default_value) if hasattr(socket,'default_value') else None})
            nodes.append({'name':node.name,'type':node.type,'properties':m6_integrity_rna(node),'inputs':inputs})
        graph={'nodes':nodes,'links':[[l.from_node.name,l.from_socket.identifier,l.to_node.name,l.to_socket.identifier,
                getattr(l,'is_muted',False)] for l in tree.links]}
    return {'name':world.name,'properties':px_rna(world),'graph':graph}

def px_context(objects=None):
    scene=bpy.context.scene
    if scene.animation_data and (scene.animation_data.action or scene.animation_data.drivers or scene.animation_data.nla_tracks):
        px_fail('PHYSICS_SCENE_ANIMATION_UNSUPPORTED','scene solver context is animated or driven')
    return {'filepath':bpy.data.filepath,
            'stamps':sorted(s.get('production_asset') for s in bpy.data.scenes if isinstance(s.get('production_asset'),str)),
            'scene':scene.name,'view_layer':bpy.context.view_layer.name,
            'fps':scene.render.fps,'fps_base':scene.render.fps_base,
            'units':{'system':scene.unit_settings.system,'scale_length':scene.unit_settings.scale_length},
            'gravity':list(scene.gravity),'use_gravity':scene.use_gravity,
            'world':px_world(scene.world),
            'object_scales':{o.name:px_value(o.scale) for o in (objects or [])},
            'range':[scene.frame_start,scene.frame_end]}

def px_status(params):
    source,primary,deps=px_selection(params)
    state=px_capture(deps)
    try:
        scene=bpy.context.scene;scene.frame_set(scene.frame_start);context=px_context(deps)
        rows={o.name:px_object_raw(o) for o in deps}
        signatures={'source_fingerprint':m2_digest(rows[source.name]),
            'primary_fingerprint':m2_digest({o.name:rows[o.name] for o in primary}),
            'dependency_fingerprint':m2_digest(rows),'context_fingerprint':m2_digest(context)}
        signatures['fingerprint']=m2_digest(signatures)
        actions={}
        for obj in primary:
            row=rows[obj.name]
            actions[obj.name]={'object':row['animation'],'data':row['data_animation'],
                'shape_keys':row.get('mesh',{}).get('shapes',{}).get('animation') if row.get('mesh',{}).get('shapes') else None}
        return dict(signatures,source_object=source.name,primary_objects=[o.name for o in primary],
            dependencies=[o.name for o in deps],primary_actions=actions,context=context,
            source={'name':source.name,'type':source.type,
                'scale':px_value(source.scale),
                'vertices':len(source.data.vertices) if source.type=='MESH' else 0,
                'bones':len(source.data.bones) if source.type=='ARMATURE' else 0},state_restored=True)
    finally:
        px_restore(deps,state)

def px_primary_snapshot(params):
    source,primary,deps=px_selection(params)
    frames=params.get('frames',[bpy.context.scene.frame_start,bpy.context.scene.frame_end])
    if not frames or len(frames)>64 or any(isinstance(f,bool) or not isinstance(f,(int,float)) or not math.isfinite(f) for f in frames):
        px_fail('PHYSICS_CAPACITY','1 to 64 finite sample frames required')
    state=px_capture(deps);samples=[]
    try:
        for frame in frames:
            m3_set_frame(frame);graph=bpy.context.evaluated_depsgraph_get();rows={}
            for obj in primary:
                evaluated=obj.evaluated_get(graph)
                row={'world':[list(r) for r in evaluated.matrix_world]}
                if obj.type=='ARMATURE':
                    row['pose']={b.name:[list(r) for r in b.matrix] for b in evaluated.pose.bones}
                if obj.type=='MESH':
                    mesh=evaluated.to_mesh()
                    try:
                        if len(mesh.vertices)>100000:
                            px_fail('PHYSICS_CAPACITY','evaluated primary geometry')
                        row['geometry']=m2_digest([list(evaluated.matrix_world@v.co) for v in mesh.vertices])
                        row['topology']=m2_digest([list(p.vertices) for p in mesh.polygons])
                    finally:
                        evaluated.to_mesh_clear()
                    if obj.data.shape_keys:
                        row['keys']={k.name:k.value for k in obj.data.shape_keys.key_blocks}
                rows[obj.name]=row
            samples.append({'frame':float(frame),'digest':m2_digest(rows),
                'objects':{name:m2_digest(row) for name,row in rows.items()}})
        return {'primary_objects':[o.name for o in primary],'frames':samples,
            'fingerprint':m2_digest(samples),'state_restored':True}
    finally:
        px_restore(deps,state)

def px_candidate_status(params):
    candidate=px_object(params.get('candidate_object') or params.get('candidate'))
    generation=params.get('generation_id')
    if not candidate.get('production_m8_owned') or (generation and candidate.get('production_generation_id')!=generation):
        px_fail('PHYSICS_CANDIDATE_OWNERSHIP','candidate is not owned by this M8 generation')
    objects=[candidate]
    rig_name=params.get('candidate_rig') or candidate.get('production_candidate_rig')
    if rig_name:
        rig=px_object(rig_name)
        if not rig.get('production_m8_owned') or rig.get('production_generation_id')!=candidate.get('production_generation_id'):
            px_fail('PHYSICS_CANDIDATE_OWNERSHIP','candidate rig ownership mismatch')
        objects.append(rig)
    generation=candidate.get('production_generation_id');version=candidate.get('production_candidate_version')
    name=params.get('collider_collection_name') or candidate.get('production_collider_collection_name')
    collection=None;colliders=[];collision_context=None
    if name:
        collection=bpy.data.collections.get(name)
        if collection is None:
            px_fail('PHYSICS_COLLIDER_COLLECTION_MISSING','owned collider collection disappeared')
        collision_context=px_collection_record(collection,generation,version,True)
        colliders=list(collection.all_objects)
        if 'collider_names' in params and set(params['collider_names'])!={o.name for o in colliders}:
            px_fail('PHYSICS_COLLIDER_MEMBERSHIP_CHANGED','declared collider membership changed')
    elif params.get('collider_names'):
        px_fail('PHYSICS_COLLIDER_COLLECTION_REQUIRED','proxy names require an owned collection')
    owned_collections={}
    for obj in objects:
        for item in obj.users_collection:
            owned_collections[item.name]=px_collection_record(item,generation,version,True)
    objects=px_dependencies(objects+colliders+[px_object(n) for n in params.get('primary_objects',[])]);prod_require_evaluable(objects)
    px_validate_objects(objects)
    state=px_capture(objects)
    try:
        bpy.context.scene.frame_set(bpy.context.scene.frame_start)
        raw={o.name:px_object_raw(o) for o in objects};context=px_context(objects)
        ownership={o.name:{'generation_id':o.get('production_generation_id'),
            'source':o.get('production_source_object'),'version':o.get('production_candidate_version'),
            'subsystem':o.get('production_subsystem'),'collections':sorted(c.name for c in o.users_collection)} for o in objects}
        signed={'raw':raw,'ownership':ownership,'context':context,
                'owned_collections':owned_collections,'collider_collection':collision_context}
        return {'candidate':candidate.name,'candidate_rig':rig_name,'fingerprint':m2_digest(signed),
            'raw_fingerprint':m2_digest(raw),'context_fingerprint':m2_digest(context),'ownership':ownership,
            'collider_collection':collision_context,'owned_collections':owned_collections,'state_restored':True}
    finally:
        px_restore(objects,state)
'''

STATUS = RUNTIME + "\nresult=px_status(params)\n"
PRIMARY = RUNTIME + "\nresult=px_primary_snapshot(params)\n"
CANDIDATE_STATUS = RUNTIME + "\nresult=px_candidate_status(params)\n"


def verify(before: dict, after: dict) -> dict:
    """Compare real measurements; missing evidence never becomes a successful gate."""
    keys = ('source_fingerprint', 'primary_fingerprint', 'dependency_fingerprint', 'context_fingerprint')
    changed = [key for key in keys if not before.get(key) or before.get(key) != after.get(key)]
    return {'passed': not changed, 'changed': changed, 'primary_preserved': 'primary_fingerprint' not in changed}
