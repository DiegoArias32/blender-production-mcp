"""M8 native Cloth RNA and bounded cache sampling, verified on Blender 5.2.1.

No file I/O runs inside Blender's safe-mode payload. Float32 samples cross only
the internal bridge to the host cache writer, never the user response. Native
memory caches are frozen only after every frame passes the runaway guard.
"""
from __future__ import annotations

import math


CLOTH_PROPERTIES = frozenset({'mass','tension_stiffness','compression_stiffness','shear_stiffness',
                             'bending_stiffness','tension_damping','compression_damping','shear_damping',
                             'bending_damping','air_damping','quality','pin_stiffness'})
COLLISION_PROPERTIES = frozenset({'use_collision','distance_min','collision_quality','use_self_collision',
                                 'self_distance_min','self_friction','friction'})


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings)-CLOTH_PROPERTIES-COLLISION_PROPERTIES:
        raise ValueError('UNSUPPORTED_CLOTH_SETTING')
    for key, value in settings.items():
        if key in {'use_collision','use_self_collision'}:
            if type(value) is not bool:
                raise ValueError('INVALID_CLOTH_SETTING: '+key)
        elif isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError('INVALID_CLOTH_SETTING: '+key)
        if key in {'quality','collision_quality'} and (type(value) is not int or not 1<=value<=12):
            raise ValueError('CLOTH_SOLVER_CAPACITY_EXCEEDED: '+key)
        if key=='mass' and value<=0:
            raise ValueError('INVALID_CLOTH_SETTING: mass')
    return dict(settings)


RUNTIME = r'''
import bpy
import math
import array

def pc_candidate(params):
    obj = bpy.data.objects.get(params['candidate_name'])
    if obj is None or obj.type != 'MESH':
        raise ValueError('CLOTH_MESH_CANDIDATE_REQUIRED')
    if not obj.get('production_m8_owned') or obj.get('production_generation_id') != params['generation_id']:
        raise ValueError('SECONDARY_CANDIDATE_OWNERSHIP_MISMATCH')
    if obj.library or obj.override_library or obj.data.library or obj.data.override_library:
        raise ValueError('LINKED_PHYSICS_CANDIDATE_UNSUPPORTED')
    return obj

def pc_modifier(obj):
    owned = [mod for mod in obj.modifiers if mod.type=='CLOTH' and mod.name==obj.get('production_m8_cloth_modifier')]
    if len(owned)!=1:
        raise ValueError('OWNED_CLOTH_MODIFIER_REQUIRED')
    return owned[0]

def pc_settings(settings):
    known = {'mass','tension_stiffness','compression_stiffness','shear_stiffness','bending_stiffness',
             'tension_damping','compression_damping','shear_damping','bending_damping','air_damping',
             'quality','pin_stiffness','use_collision','distance_min','collision_quality',
             'use_self_collision','self_distance_min','self_friction','friction'}
    if not isinstance(settings,dict) or set(settings)-known:
        raise ValueError('UNSUPPORTED_CLOTH_SETTING')
    for key,value in settings.items():
        if key in ('use_collision','use_self_collision'):
            if type(value) is not bool:
                raise ValueError('INVALID_CLOTH_SETTING: '+key)
        elif isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError('INVALID_CLOTH_SETTING: '+key)
        if key in ('quality','collision_quality') and (type(value) is not int or not 1<=value<=12):
            raise ValueError('CLOTH_SOLVER_CAPACITY_EXCEEDED: '+key)
        if key=='mass' and value<=0:
            raise ValueError('INVALID_CLOTH_SETTING: mass')
    return settings

def pc_read_settings(mod):
    s = mod.settings
    c = mod.collision_settings
    return {'mass':s.mass,'tension_stiffness':s.tension_stiffness,
            'compression_stiffness':s.compression_stiffness,'shear_stiffness':s.shear_stiffness,
            'bending_stiffness':s.bending_stiffness,'tension_damping':s.tension_damping,
            'compression_damping':s.compression_damping,'shear_damping':s.shear_damping,
            'bending_damping':s.bending_damping,'air_damping':s.air_damping,
            'quality':s.quality,'pin_stiffness':s.pin_stiffness,'pin_group':s.vertex_group_mass,
            'use_collision':c.use_collision,'distance_min':c.distance_min,
            'collision_quality':c.collision_quality,'use_self_collision':c.use_self_collision,
            'self_distance_min':c.self_distance_min,'self_friction':c.self_friction,'friction':c.friction,
            'collider_collection_name':c.collection.name if c.collection else None}

def pc_setup(params):
    obj = pc_candidate(params)
    if len(obj.data.vertices)<3 or not obj.data.polygons or len(obj.data.vertices)>20000 or len(obj.data.polygons)>40000:
        raise ValueError('CLOTH_SURFACE_CAPACITY_INVALID')
    settings = pc_settings(params.get('settings',{}))
    pin_group = params.get('pin_group','')
    group = obj.vertex_groups.get(pin_group) if pin_group else None
    if group is None or not any(any(g.group==group.index and g.weight>0 for g in v.groups) for v in obj.data.vertices):
        raise ValueError('EXPLICIT_NONEMPTY_CLOTH_PINS_REQUIRED')
    if any(mod.type in ('CLOTH','SOFT_BODY','MESH_CACHE','MESH_SEQUENCE_CACHE','NODES') for mod in obj.modifiers):
        raise ValueError('EXISTING_PHYSICS_OR_UNKNOWN_NODES_UNSUPPORTED')
    collider_names = params.get('collider_names',[])
    collection_name = params.get('collider_collection_name')
    collision_enabled = settings.get('use_collision',True)
    collection = bpy.data.collections.get(collection_name) if collection_name else None
    if collision_enabled:
        if not isinstance(collider_names,list) or not collider_names or len(collider_names)>8 or len(set(collider_names))!=len(collider_names):
            raise ValueError('EXPLICIT_CLOTH_COLLIDERS_REQUIRED')
        if collection is None or not collection.get('production_m8_owned') or collection.get('production_generation_id')!=params['generation_id']:
            raise ValueError('OWNED_CLOTH_COLLIDER_COLLECTION_REQUIRED')
        if set(collection.all_objects.keys())!=set(collider_names):
            raise ValueError('CLOTH_COLLIDER_COLLECTION_MANIFEST_MISMATCH')
        for collider_name in collider_names:
            collider = bpy.data.objects.get(collider_name)
            if collider is None or collider==obj or collider.type!='MESH' or not collider.get('production_m8_owned') or collider.get('production_generation_id')!=params['generation_id']:
                raise ValueError('OWNED_CLOTH_COLLIDER_REQUIRED')
            if not any(m.type=='COLLISION' and m.show_viewport and m.show_render for m in collider.modifiers):
                raise ValueError('CLOTH_COLLIDER_NOT_ENABLED')
    elif collider_names or collection_name:
        raise ValueError('CLOTH_COLLIDERS_EXPLICITLY_DISABLED')
    name = 'M8_CLOTH_'+params['generation_id'][:12]
    if obj.modifiers.get(name):
        raise ValueError('SECONDARY_MODIFIER_NAME_COLLISION')
    mod = obj.modifiers.new(name,'CLOTH')
    obj['production_m8_cloth_modifier'] = mod.name
    s = mod.settings
    c = mod.collision_settings
    c.collection = collection if collision_enabled else None
    s.vertex_group_mass = pin_group
    s.use_dynamic_mesh = True
    if 'mass' in settings: s.mass = settings['mass']
    if 'tension_stiffness' in settings: s.tension_stiffness = settings['tension_stiffness']
    if 'compression_stiffness' in settings: s.compression_stiffness = settings['compression_stiffness']
    if 'shear_stiffness' in settings: s.shear_stiffness = settings['shear_stiffness']
    if 'bending_stiffness' in settings: s.bending_stiffness = settings['bending_stiffness']
    if 'tension_damping' in settings: s.tension_damping = settings['tension_damping']
    if 'compression_damping' in settings: s.compression_damping = settings['compression_damping']
    if 'shear_damping' in settings: s.shear_damping = settings['shear_damping']
    if 'bending_damping' in settings: s.bending_damping = settings['bending_damping']
    if 'air_damping' in settings: s.air_damping = settings['air_damping']
    if 'quality' in settings: s.quality = settings['quality']
    if 'pin_stiffness' in settings: s.pin_stiffness = settings['pin_stiffness']
    if 'use_collision' in settings: c.use_collision = settings['use_collision']
    if 'distance_min' in settings: c.distance_min = settings['distance_min']
    if 'collision_quality' in settings: c.collision_quality = settings['collision_quality']
    if 'use_self_collision' in settings: c.use_self_collision = settings['use_self_collision']
    if 'self_distance_min' in settings: c.self_distance_min = settings['self_distance_min']
    if 'self_friction' in settings: c.self_friction = settings['self_friction']
    if 'friction' in settings: c.friction = settings['friction']
    cache = mod.point_cache
    cache.use_disk_cache = False
    cache.use_external = False
    cache.frame_step = 1
    cache.name = name
    actual = pc_read_settings(mod)
    if any(abs(actual[key]-value)>max(0.00001,abs(value)*0.00001) for key,value in settings.items()):
        raise ValueError('CLOTH_RNA_SETTING_CLAMPED')
    return {'backend':'BLENDER_NATIVE_CLOTH','candidate':obj.name,'modifier':mod.name,
            'settings':pc_read_settings(mod),'dynamic_mesh':s.use_dynamic_mesh,
            'collisions':{'status':'CONFIGURED' if collision_enabled else 'NOT_APPLICABLE',
                          'reason':None if collision_enabled else 'explicit_settings_use_collision_false',
                          'collider_names':collider_names,'collection':collection.name if collection else None},
            'cache_policy':'guarded_native_memory_then_host_PC2','visual_verification':'required'}

def pc_simulate(params):
    obj = pc_candidate(params)
    mod = pc_modifier(obj)
    cache = mod.point_cache
    if not mod.show_viewport or not mod.show_render:
        raise ValueError('CLOTH_MODIFIER_DISABLED')
    scene = bpy.context.scene
    start,end = params['frame_start'],params['frame_end']
    if type(start) is not int or type(end) is not int or end<start or end-start>600:
        raise ValueError('CLOTH_SIMULATION_WINDOW_INVALID')
    if len(obj.data.vertices)>params.get('max_vertices',20000):
        raise ValueError('CLOTH_SIMULATION_CAPACITY_EXCEEDED')
    if cache.is_baked:
        raise ValueError('CLOTH_CACHE_ALREADY_FROZEN_CREATE_NEW_CANDIDATE')
    fps = scene.render.fps/scene.render.fps_base
    if not math.isfinite(fps) or fps<=0 or isinstance(params['fps'],bool) or not isinstance(params['fps'],(int,float)) or not math.isfinite(params['fps']):
        raise ValueError('PHYSICS_FPS_INVALID')
    if not isinstance(params['gravity'],list) or len(params['gravity'])!=3 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in params['gravity']):
        raise ValueError('PHYSICS_GRAVITY_INVALID')
    if abs(fps-params['fps'])>1e-6 or len(params['gravity'])!=3 or any(abs(a-b)>0.00001 for a,b in zip(scene.gravity,params['gravity'])):
        raise ValueError('PHYSICS_CONTEXT_CHANGED')
    thresholds = validate_thresholds(params.get('thresholds'))
    source = pq_object(params['source_name']) if params.get('source_name') else None
    captured_objects,captured_state=pq_capture(params,[obj]+([source] if source else []))
    previous,previous_previous,reference = None,None,None
    previous_reference,older_reference = None,None
    cache.frame_start,cache.frame_end,cache.frame_step = start,end,1
    cache.use_disk_cache = False
    cache.use_external = False
    frames = []
    frozen = False
    freeze_result = []
    try:
        for frame in range(start,end+1):
            scene.frame_set(frame)
            sample = pq_snapshot(obj,params.get('max_vertices',20000))
            if reference is None:
                reference = sample['points']
            primary = pq_snapshot(source,params.get('max_vertices',20000))['points'] if source else reference
            measured = motion_step(sample['points'],primary,previous,previous_previous,fps,sample['edges'],sample['triangles'],thresholds,previous_reference,older_reference)
            measured['frame'] = frame
            frames.append(measured)
            if not measured['passed']:
                # Restoring the caller's later frame must not evaluate a failed
                # live solver again. Preserve the owned modifier and diagnostics.
                mod.show_viewport = False
                mod.show_render = False
                return {'backend':'BLENDER_NATIVE_CLOTH','passed':False,'solver_status':'STOPPED_BY_STABILITY_GUARD',
                        'error_codes':measured['errors'],'frames_processed':len(frames),'requested_frames':end-start+1,
                        'first_failed_frame':frame,'qa':frames,'cache_frozen':False,'thresholds':thresholds,
                        'solver_disabled':True,'disabled_reason':'stability_guard_before_frame_restoration',
                        'visual_verification':'required'}
            previous_previous,previous = previous,sample['points']
            older_reference,previous_reference = previous_reference,primary
        with bpy.context.temp_override(scene=scene,object=obj,active_object=obj,point_cache=cache):
            if not bpy.ops.ptcache.bake_from_cache.poll():
                raise ValueError('CLOTH_FREEZE_CACHE_CONTEXT_UNSUPPORTED')
            freeze_result = sorted(bpy.ops.ptcache.bake_from_cache())
        frozen = cache.is_baked
        if not frozen or 'FINISHED' not in freeze_result:
            raise ValueError('CLOTH_NATIVE_CACHE_FREEZE_FAILED')
    except Exception:
        mod.show_viewport = False
        mod.show_render = False
        raise
    finally:
        px_restore(captured_objects,captured_state)
    return {'backend':'BLENDER_NATIVE_CLOTH','passed':True,'solver_status':'GUARDED_AND_FROZEN',
            'frames_processed':len(frames),'frame_start':start,'frame_end':end,'qa':frames,
            'cache_frozen':frozen,'native_cache':{'name':cache.name,'info':cache.info,'is_baked':cache.is_baked,
                                               'disk_cache':cache.use_disk_cache,'frame_step':cache.frame_step},
            'freeze_operator':'ptcache.bake_from_cache','freeze_result':freeze_result,'thresholds':thresholds,
            'coverage':{'frame_step':1,'all_evaluated_vertices':True},'visual_verification':'required'}

def pc_collect(params):
    obj = pc_candidate(params)
    mod = pc_modifier(obj)
    if not mod.point_cache.is_baked:
        raise ValueError('VALIDATED_FROZEN_NATIVE_CACHE_REQUIRED')
    frames = pq_frames(params)
    if frames[0] < mod.point_cache.frame_start or frames[-1] > mod.point_cache.frame_end:
        raise ValueError('CLOTH_CACHE_RANGE_MISMATCH')
    max_bytes = params.get('max_bytes',16000000)
    if type(max_bytes) is not int or not 1<=max_bytes<=16000000:
        raise ValueError('CLOTH_CACHE_PAYLOAD_CAPACITY_INVALID')
    estimate = len(obj.data.vertices)*len(frames)*12
    if estimate>max_bytes or len(obj.data.vertices)>params.get('max_vertices',20000):
        raise ValueError('CLOTH_CACHE_PAYLOAD_CAPACITY_EXCEEDED')
    scene = bpy.context.scene
    captured_objects,captured_state=pq_capture(params,[obj])
    output = []
    topology = None
    try:
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=depsgraph)
            try:
                if len(mesh.polygons)>40000 or len(mesh.edges)>60000:
                    raise ValueError('CLOTH_CACHE_TOPOLOGY_CAPACITY_EXCEEDED')
                current = {'vertex_count':len(mesh.vertices),'polygons':[list(p.vertices) for p in mesh.polygons],
                           'edges':[list(e.vertices) for e in mesh.edges]}
                if topology is None:
                    topology = current
                elif topology!=current:
                    raise ValueError('CLOTH_CACHE_TOPOLOGY_CHANGED')
                if len(mesh.vertices)>params.get('max_vertices',20000) or len(mesh.vertices)*len(frames)*12>max_bytes:
                    raise ValueError('CLOTH_CACHE_PAYLOAD_CAPACITY_EXCEEDED')
                coords = array.array('f',[0])*(len(mesh.vertices)*3)
                mesh.vertices.foreach_get('co',coords)
                if any(not math.isfinite(value) for value in coords):
                    raise ValueError('NONFINITE_CLOTH_CACHE')
                output.append({'frame':frame,'vertex_count':len(mesh.vertices),'hex':coords.tobytes().hex()})
            finally:
                evaluated.to_mesh_clear()
    finally:
        px_restore(captured_objects,captured_state)
    return {'schema':'production.physics.pc2_samples.internal.v1','frames':output,'topology':topology,
            'coordinate_space':'evaluated_mesh_local_float32_XYZ','byte_order':'little',
            'frame_start':frames[0],'sample_rate':1.0,'not_for_model':True}

def pc_playback(params):
    obj = pc_candidate(params)
    filepath = params.get('filepath','')
    if not filepath or not filepath.lower().endswith('.pc2') or not params.get('host_path_validated'):
        raise ValueError('HOST_VALIDATED_PC2_PATH_REQUIRED')
    if type(params.get('frame_start')) is not int:
        raise ValueError('PC2_FRAME_START_REQUIRED')
    if obj.modifiers.get('M8_PC2_PLAYBACK'):
        raise ValueError('SECONDARY_MODIFIER_NAME_COLLISION')
    # All earlier deformers produce the primary input, then full evaluated PC2
    # coordinates overwrite it. Only our Cloth is disabled on this copied object.
    for native in obj.modifiers:
        if native.type=='CLOTH':
            if native.name!=obj.get('production_m8_cloth_modifier'):
                raise ValueError('UNOWNED_PHYSICS_MODIFIER_UNSUPPORTED')
            native.show_viewport = False
            native.show_render = False
    mod = obj.modifiers.new('M8_PC2_PLAYBACK','MESH_CACHE')
    obj['production_m8_cache_modifier'] = mod.name
    mod.cache_format = 'PC2'
    mod.filepath = filepath
    mod.factor = 1.0
    mod.deform_mode = 'OVERWRITE'
    mod.interpolation = 'LINEAR'
    mod.time_mode = 'FRAME'
    mod.play_mode = 'SCENE'
    mod.frame_start = float(params['frame_start'])
    mod.frame_scale = 1.0
    mod.forward_axis = 'POS_Y'
    mod.up_axis = 'POS_Z'
    mod.flip_axis = (False,False,False)
    mod.vertex_group = ''
    return {'backend':'PC2_MESH_CACHE','candidate':obj.name,'modifier':mod.name,'filepath':mod.filepath,
            'frame_start':mod.frame_start,'frame_scale':mod.frame_scale,'time_mode':mod.time_mode,
            'play_mode':mod.play_mode,'coordinate_space':'evaluated_mesh_local','native_cloth_disabled':True,
            'bake_equivalence':'required','visual_verification':'required'}
'''
