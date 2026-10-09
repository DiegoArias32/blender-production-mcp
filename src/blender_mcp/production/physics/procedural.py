"""Read primary motion; write bounded secondary channels only on owned copies."""
from __future__ import annotations

from .integrity import RUNTIME as INTEGRITY_RUNTIME


RUNTIME = INTEGRITY_RUNTIME + r'''
def pp_guard_selection(params):
    source=px_object(params['source_object']);kind=params['subsystem']
    result={'anchor_indices':[],'root_indices':[],'outside_region_indices':[]}
    masked_chain=kind in ('HAIR','SPRING_BONE','CHAIN','CABLE') and source.type=='MESH'
    if kind not in ('SOFT','CLOTH') and not masked_chain:
        return result
    if source.type!='MESH':
        px_fail('PHYSICS_MESH_REQUIRED','vertex masks require a mesh')
    # Index masks are certified only for pointwise deformation. Smoothing can
    # move protected neighbours; topology modifiers destroy index identity.
    allowed={'ARMATURE','WEIGHTED_NORMAL'}
    if any(m.type not in allowed for m in source.modifiers):
        px_fail('PHYSICS_MASK_MODIFIER_UNSUPPORTED','masked adapters require pointwise, topology-preserving modifiers')
    if not params.get('region' if kind=='SOFT' else 'pin_group'):
        px_fail('PHYSICS_MASK_REQUIRED','explicit vertex-group attachment/region mask missing')
    group=source.vertex_groups.get(params['region'] if kind=='SOFT' else params['pin_group'])
    if group is None:
        px_fail('PHYSICS_MASK_REQUIRED','explicit vertex-group mask missing')
    weights=[next((g.weight for g in v.groups if g.group==group.index),0.0) for v in source.data.vertices]
    if not any(w>0 for w in weights) or all(w>0 for w in weights):
        px_fail('PHYSICS_MASK_INVALID','mask must leave selected and protected vertices')
    if kind=='SOFT':
        result['outside_region_indices']=[i for i,w in enumerate(weights) if w==0]
    elif kind=='CLOTH':
        result['anchor_indices']=[i for i,w in enumerate(weights) if w>=1-1e-6]
        if not result['anchor_indices']:
            px_fail('CLOTH_FULL_PIN_REQUIRED','at least one explicit full-weight attachment is required')
    else:
        result['root_indices']=[i for i,w in enumerate(weights) if w>=1-1e-6]
        if not result['root_indices']:
            px_fail('SECONDARY_FULL_ROOT_REQUIRED','explicit full-weight roots required on the bound mesh')
    result['membership_source']='explicit_source_vertex_group'
    return result

def pp_samples(params):
    source,primary,deps=px_selection(params);saved=px_capture(deps)
    frames=list(range(params['frame_start'],params['frame_end']+1))
    if len(frames)>601:
        px_fail('PHYSICS_CAPACITY','primary samples exceed 601')
    anchor=px_object(params.get('anchor_object') or params['source_object'])
    chain=params.get('chain');samples=[];lengths=[];mesh_budget_ratio=1.0;maximum_radius=0.0
    if chain:
        rig=px_object(chain['armature'])
        lengths=[rig.data.bones[n].length for n in chain['bones']]
    try:
        if chain and source.type=='MESH':
            bpy.context.scene.frame_set(params['frame_start'])
            mesh_to_rig=rig.matrix_world.inverted()@source.matrix_world
            group_names={g.index:g.name for g in source.vertex_groups}
            levers=[1.0]
            for vertex in source.data.vertices:
                for weight in vertex.groups:
                    name=group_names[weight.group]
                    if name in chain['bones'] and weight.weight>0:
                        bone=rig.data.bones[name]
                        levers.append((mesh_to_rig@vertex.co-bone.head_local).length/bone.length)
            # Endpoint bounds alone do not bound off-axis skinned vertices.
            # Reserve a conservative per-segment displacement budget for their
            # longest rest lever arm. Evaluated mesh QA remains authoritative.
            mesh_budget_ratio=min(1.0,len(lengths)/(len(lengths)-1+max(levers)))
        for frame in frames:
            bpy.context.scene.frame_set(frame);graph=bpy.context.evaluated_depsgraph_get()
            evaluated=anchor.evaluated_get(graph);matrix=evaluated.matrix_world.copy()
            if params.get('anchor_bone'):
                bone=evaluated.pose.bones.get(params['anchor_bone']) if anchor.type=='ARMATURE' else None
                if bone is None:
                    px_fail('PHYSICS_ANCHOR_INVALID','explicit anchor bone missing')
                matrix=matrix@bone.matrix
            row={'frame':frame,'position':list(matrix.translation),'quaternion':list(matrix.to_quaternion()),
                 'source_world':[list(r) for r in source.matrix_world],
                 'source_location':list(source.location),'source_rotation_mode':source.rotation_mode,
                 'source_rotation':list(source.rotation_quaternion if source.rotation_mode=='QUATERNION' else source.rotation_euler),
                 'source_scale':list(source.scale)}
            if params.get('subsystem')=='ACCESSORY' and source.type=='MESH':
                evaluated_source=source.evaluated_get(graph);mesh=evaluated_source.to_mesh()
                try:
                    if len(mesh.vertices)>20000:
                        px_fail('PHYSICS_CAPACITY','accessory evaluated vertex budget')
                    maximum_radius=max(maximum_radius,max(((evaluated_source.matrix_world.to_3x3()@v.co).length for v in mesh.vertices),default=0.0))
                finally:
                    evaluated_source.to_mesh_clear()
            if chain:
                er=rig.evaluated_get(graph)
                actual_lengths=[(er.matrix_world.to_3x3()@(er.pose.bones[n].tail-er.pose.bones[n].head)).length for n in chain['bones']]
                if any(abs(a-b)>max(1e-6,b*1e-5) for a,b in zip(actual_lengths,lengths)):
                    px_fail('SECONDARY_CHAIN_SCALE_UNSUPPORTED','evaluated bone lengths must match the fixed-length world-metre solver')
                first=er.pose.bones[chain['bones'][0]]
                row['position']=list(er.matrix_world@first.head)
                row['quaternion']=list((er.matrix_world@first.matrix).to_quaternion())
                row['directions']=[list(((er.matrix_world@er.pose.bones[n].matrix).to_3x3()@Vector((0,1,0))).normalized()) for n in chain['bones']]
                row['bone_basis']=[list((er.matrix_world@er.pose.bones[n].matrix).to_quaternion()) for n in chain['bones']]
                row['bone_modes']=[rig.pose.bones[n].rotation_mode for n in chain['bones']]
                row['bone_base_quaternions']=[list(rig.pose.bones[n].rotation_quaternion) for n in chain['bones']]
            samples.append(row)
        return {'samples':samples,'lengths':lengths,'mesh_displacement_budget_ratio':mesh_budget_ratio,
                'maximum_object_radius_m':maximum_radius,
                'state_restored':True,'frames':len(frames)}
    finally:
        px_restore(deps,saved)

def pp_owned(params):
    obj=px_object(params['candidate_name'])
    if not obj.get('production_m8_owned') or obj.get('production_generation_id')!=params['generation_id']:
        px_fail('PHYSICS_CANDIDATE_OWNERSHIP','secondary write is restricted to an owned copy')
    if params.get('expected_status') and px_status(params)['fingerprint']!=params['expected_status']['fingerprint']:
        px_fail('PHYSICS_STALE_SOURCE','primary stack changed before secondary write')
    return obj

def pp_action(params,obj,id_type):
    name=params['action_name'];action=bpy.data.actions.get(name)
    if params.get('write_mode','create')=='create':
        if action is not None:
            px_fail('PHYSICS_ACTION_EXISTS','never overwrite an existing Action')
        action=bpy.data.actions.new(name);action.use_fake_user=True
        action['production_m8_owned']=True;action['production_generation_id']=params['generation_id']
        action['production_source_object']=params['source_object']
        action['production_mcp_version']=params['mcp_version']
        action.slots.new(id_type=id_type,name=obj.name)
        action.layers.new('M8_SECONDARY').strips.new(type='KEYFRAME')
    if action is None or not action.get('production_m8_owned') or action.get('production_generation_id')!=params['generation_id']:
        px_fail('PHYSICS_ACTION_OWNERSHIP','chunk target is not owned by this generation')
    return action

def pp_write(params):
    obj=pp_owned(params);target=px_object(params.get('candidate_rig') or obj.name)
    if target.get('production_generation_id')!=params['generation_id']:
        px_fail('PHYSICS_CANDIDATE_OWNERSHIP','rig is not owned')
    key_target=obj.data.shape_keys if params.get('soft') else None
    if params.get('soft') and key_target is None:
        px_fail('SOFT_KEY_SETUP_REQUIRED','create authorized candidate keys first')
    owner=key_target or target;id_type='KEY' if key_target else 'OBJECT'
    action=pp_action(params,owner,id_type);slot=action.slots[0]
    bag=action.layers[0].strips[0].channelbag(slot,ensure=True)
    for channel in params['channels']:
        path=channel['path'];index=channel['index'];flat=channel['co']
        if len(flat)%2 or any(not math.isfinite(float(v)) for v in flat):
            px_fail('PHYSICS_CHANNEL_INVALID','finite frame/value pairs required')
        if any(flat[i]<params['frame_start'] or flat[i]>params['frame_end'] for i in range(0,len(flat),2)):
            px_fail('PHYSICS_CHANNEL_WINDOW','key outside the simulation clip')
        if bag.fcurves.find(path,index=index):
            px_fail('PHYSICS_CHANNEL_DUPLICATE','do not overwrite a candidate channel')
        if channel.get('bone'):
            bone=target.pose.bones.get(channel['bone'])
            if bone is None or bone.constraints or channel['bone'] not in params['chain']['bones']:
                px_fail('PHYSICS_CHAIN_INVALID','only declared unconstrained secondary bones')
            if bone.rotation_mode!='QUATERNION' or not path.startswith(bone.path_from_id()+'.rotation_quaternion'):
                px_fail('SECONDARY_ROTATION_MODE_UNSUPPORTED','quaternion bone adapter requires an existing quaternion mode')
        elif key_target:
            if not path.startswith('key_blocks["M8_') or not path.endswith('"].value'):
                px_fail('SOFT_CHANNEL_OWNERSHIP','only newly created secondary shape keys')
        elif path not in ('location','rotation_euler','rotation_quaternion','scale'):
            px_fail('PHYSICS_CHANNEL_OWNERSHIP','only scoped candidate transform channels')
        curve=bag.fcurves.new(path,index=index);curve.keyframe_points.add(len(flat)//2)
        curve.keyframe_points.foreach_set('co',flat)
        for point in curve.keyframe_points:
            point.interpolation='LINEAR'
        curve.update()
    if params.get('finalize'):
        ad=owner.animation_data_create()
        if params.get('candidate_rig') or obj.type=='ARMATURE':
            track=ad.nla_tracks.new();track.name='M8_SECONDARY_'+params['generation_id'][:8]
            strip=track.strips.new(track.name,params['frame_start'],action);strip.action_slot=slot
            # Channels are disjoint from primary animation (px_chain_guard).
            # REPLACE stores absolute base*delta quaternions and avoids the
            # measured small-angle distortion of NLA COMBINE in Blender 5.2.1.
            strip.blend_type='REPLACE';strip.extrapolation='NOTHING';strip.influence=1.0
        else:
            ad.action=action;ad.action_slot=slot
    return {'action':action.name,'channels_written':len(params['channels']),'finalized':bool(params.get('finalize'))}

def pp_soft_setup(params):
    obj=pp_owned(params)
    if obj.type!='MESH' or obj.data.shape_keys:
        px_fail('SOFT_CANDIDATE_UNSUPPORTED','new keys require a mesh copy without pre-existing morphs')
    if params.get('region_authorized') is not True:
        px_fail('SOFT_REGION_AUTHORIZATION_REQUIRED','explicit authorized vertex region required')
    if any(m.type not in ('ARMATURE','WEIGHTED_NORMAL') for m in obj.modifiers):
        px_fail('PHYSICS_MASK_MODIFIER_UNSUPPORTED','soft adapter cannot propagate deformation outside the authorized mask')
    group=obj.vertex_groups.get(params['region'])
    if group is None:
        px_fail('SOFT_REGION_INVALID','named region mask missing')
    weights=[next((g.weight for g in v.groups if g.group==group.index),0.0) for v in obj.data.vertices]
    if not any(w>0 for w in weights) or all(w>0 for w in weights):
        px_fail('SOFT_REGION_INVALID','mask must protect a nonempty region outside the soft selection')
    maximum=params['max_displacement_m']
    obj.shape_key_add(name='Basis')
    for axis,label in enumerate(('X','Y','Z')):
        key=obj.shape_key_add(name='M8_Soft'+label);key.slider_min=-1;key.slider_max=1
        for vertex,weight in zip(key.data,weights):
            vertex.co[axis]+=maximum*weight
    return {'keys':['M8_SoftX','M8_SoftY','M8_SoftZ'],'selected_vertices':sum(w>0 for w in weights),
            'protected_vertices':sum(w==0 for w in weights),'falloff':'explicit vertex-group weights','maximum_m':maximum}

def pp_mark(params):
    obj=pp_owned(params)
    for name in params.get('owned_objects',[obj.name]):
        item=px_object(name)
        if item.get('production_generation_id')!=params['generation_id']:
            px_fail('PHYSICS_CANDIDATE_OWNERSHIP','activation is scoped to this generation')
        item.hide_render=not params.get('enabled',False)
        item['production_secondary_state']=params['state']
    return {'state':params['state'],'candidate':obj.name,'original_visibility_changed':False}
'''

SAMPLES = RUNTIME + '\nresult=pp_samples(params)\n'
WRITE = RUNTIME + '\nresult=pp_write(params)\n'
SOFT_SETUP = RUNTIME + '\nresult=pp_soft_setup(params)\n'
MARK = RUNTIME + '\nresult=pp_mark(params)\n'
GUARD_SELECTION = RUNTIME + '\nresult=pp_guard_selection(params)\n'
