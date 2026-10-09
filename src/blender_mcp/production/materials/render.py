"""Bounded PBR comparisons in a disposable studio, never in the source scene.

The preview is deliberately a static copy of authored mesh coordinates and current
shape-key values, without evaluation of source modifiers, Actions or simulations.
It demonstrates shader response, not the final animated/deformed asset.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from ..common.storage import sha256, write_json
from ..common.digest import DIGEST_RUNTIME
from ..common.m3_transaction import M3_RUNTIME
from ..qa.capture import _STATE, _verify_images

LIGHTING = ("NEUTRAL", "SOFT", "HARD", "GRAZING")
VIEWS = ("FRONT", "THREE_QUARTER", "CLOSE_UP")

RUNTIME = 'import json\nimport math\nfrom mathutils import Vector\n' + DIGEST_RUNTIME + M3_RUNTIME + _STATE + r'''
def mr_context():
    scene=bpy.context.scene
    return {'scene':scene.name,'frame':scene.frame_current,'subframe':scene.frame_subframe,
            'camera':scene.camera.name if scene.camera else None,'render':qa_state(scene),
            'engine':scene.render.engine,'mode':bpy.context.mode,
            'active':bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
            'selected':sorted(o.name for o in bpy.context.selected_objects),
            'filepath':bpy.data.filepath}

def mr_rna_capture(objects):
    owners=[]
    for obj in objects:
        owners.append(obj)
        if obj.data:owners.append(obj.data)
        if obj.type=='MESH' and obj.data.shape_keys:owners.append(obj.data.shape_keys)
    return {'objects':{obj.name:m3_capture(obj) for obj in objects},
            'delta':{obj.name:{'location':list(obj.delta_location),'scale':list(obj.delta_scale),
                'euler':list(obj.delta_rotation_euler),'quaternion':list(obj.delta_rotation_quaternion)} for obj in objects},
            'keys':{obj.name:{'eval_time':obj.data.shape_keys.eval_time,'values':{key.name:key.value for key in obj.data.shape_keys.key_blocks}}
                    for obj in objects if obj.type=='MESH' and obj.data.shape_keys},
            'nla':[(owner,track.name,strip.name,strip.influence,strip.strip_time) for owner in owners
                if getattr(owner,'animation_data',None) for track in owner.animation_data.nla_tracks for strip in track.strips]}

def mr_rna_restore(objects,state):
    # Rendering another scene still reevaluates authored channels in Blender.
    # Restore all saved channels together, without frame_set or evaluating caches.
    for obj in objects:
        record=state['objects'][obj.name];m3_set_channels(obj,record['object'])
        for name,values in record['pose'].items():m3_set_channels(obj.pose.bones[name],values)
        delta=state['delta'][obj.name];obj.delta_location=delta['location'];obj.delta_scale=delta['scale']
        obj.delta_rotation_euler=delta['euler'];obj.delta_rotation_quaternion=delta['quaternion']
        obj.update_tag()
    for obj in objects:
        if obj.name in state['keys']:
            keys=obj.data.shape_keys;record=state['keys'][obj.name];keys.eval_time=record['eval_time']
            for name,value in record['values'].items():keys.key_blocks[name].value=value
    for owner,track,strip,influence,strip_time in state['nla']:
        item=owner.animation_data.nla_tracks[track].strips[strip];item.influence=influence;item.strip_time=strip_time
    bpy.context.view_layer.update()

def mr_simulation_guard(objects):
    # Even rendering a separate scene can update the original dependency graph.
    # Never deactivate a source modifier to pass this guard.
    scene=bpy.context.scene
    world=scene.rigidbody_world
    if world and world.enabled and any(obj.rigid_body for obj in objects):
        raise ValueError('MATERIAL_RENDER_ACTIVE_SIMULATION_UNSUPPORTED: rigid body activo; usar una copia estática autorizada')
    for obj in objects:
        for modifier in obj.modifiers:
            if not (modifier.show_viewport or modifier.show_render):continue
            if modifier.type in ('CLOTH','SOFT_BODY','FLUID','DYNAMIC_PAINT','PARTICLE_SYSTEM','OCEAN'):
                raise ValueError('MATERIAL_RENDER_ACTIVE_SIMULATION_UNSUPPORTED: '+obj.name+'/'+modifier.name+' ('+modifier.type+'); no se desactiva automáticamente')
            if modifier.type=='NODES' and modifier.node_group:
                pending=[modifier.node_group];seen=set();count=0
                while pending:
                    tree=pending.pop()
                    if tree in seen:continue
                    seen.add(tree)
                    for node in tree.nodes:
                        count+=1
                        if count>4096:raise ValueError('MATERIAL_RENDER_CONTEXT_CAPACITY: inspección de Geometry Nodes supera 4096 nodos')
                        if node.bl_idname in ('GeometryNodeSimulationInput','GeometryNodeSimulationOutput','GeometryNodeBake'):
                            raise ValueError('MATERIAL_RENDER_ACTIVE_SIMULATION_UNSUPPORTED: '+obj.name+'/'+modifier.name+' contiene simulación/bake')
                        if node.type=='GROUP' and node.node_tree:pending.append(node.node_tree)

def mr_material_state(mat):
    tree=mat.node_tree
    return {'name':mat.name,'diffuse':list(mat.diffuse_color),'roughness':mat.roughness,
            'metallic':mat.metallic,'nodes':[
                {'name':n.name,'type':n.bl_idname,'image':n.image.name if n.type=='TEX_IMAGE' and n.image else None,
                 'inputs':[[s.identifier,list(s.default_value) if s.type in ('VECTOR','RGBA') else s.default_value]
                    for s in n.inputs if hasattr(s,'default_value') and s.type in ('VECTOR','RGBA','VALUE','INT','BOOLEAN','STRING')]}
                for n in tree.nodes] if tree else [],
            'links':[[l.from_node.name,l.from_socket.identifier,l.to_node.name,l.to_socket.identifier] for l in tree.links] if tree else []}

def mr_source_state(obj,materials):
    mesh=obj.data
    keys=mesh.shape_keys
    return m2_digest({'context':mr_context(),'matrix':[[float(v) for v in row] for row in obj.matrix_world],
        'slots':[[s.link,s.material.name if s.material else None] for s in obj.material_slots],
        'vertices':[list(v.co) for v in mesh.vertices],
        'polygons':[[list(p.vertices),p.material_index] for p in mesh.polygons],
        'uv':[[u.name,[list(v.uv) for v in u.data]] for u in mesh.uv_layers],
        'groups':[[v.index,[[g.group,g.weight] for g in v.groups]] for v in mesh.vertices],
        'keys':[[k.name,k.value,[list(v.co) for v in k.data]] for k in keys.key_blocks] if keys else [],
        'materials':[mr_material_state(m) for m in materials]})

def mr_static_material(material,visited):
    if material.animation_data and (material.animation_data.action or len(material.animation_data.drivers) or len(material.animation_data.nla_tracks)):
        raise ValueError('MATERIAL_RENDER_ANIMATED_UNSUPPORTED: el preview estático no evalúa materiales animados')
    tree=material.node_tree if hasattr(material,'node_tree') else material
    if tree is None or tree in visited:
        return
    visited.add(tree)
    if tree.animation_data and (tree.animation_data.action or len(tree.animation_data.drivers) or len(tree.animation_data.nla_tracks)):
        raise ValueError('MATERIAL_RENDER_ANIMATED_UNSUPPORTED: node tree animado o con drivers')
    for node in tree.nodes:
        if node.type=='GROUP' and node.node_tree:
            mr_static_material(node.node_tree,visited)

def mr_features(material):
    rows=[]
    if material.node_tree:
        for node in material.node_tree.nodes:
            if node.type=='BSDF_PRINCIPLED':
                row={'node':node.name}
                for name in ('Subsurface Weight','Subsurface Scale','Transmission Weight','Alpha','IOR','Roughness','Metallic'):
                    socket=node.inputs.get(name)
                    row[name]={'value':float(socket.default_value),'linked':socket.is_linked} if socket else None
                rows.append(row)
    return {'principled':rows,'surface_render_method':getattr(material,'surface_render_method',None),
            'use_raytrace_refraction':getattr(material,'use_raytrace_refraction',None),
            'thickness_mode':getattr(material,'thickness_mode',None),'refraction_depth':getattr(material,'refraction_depth',None)}

def mr_backend(scene,options):
    engine=options['engine']
    scene.render.engine=engine
    requested=options['backend']
    if engine=='CYCLES':
        scene.cycles.samples=options['samples']
        scene.cycles.use_denoising=False
        if requested=='CPU':
            if not options['allow_cpu_fallback']:
                raise ValueError('RENDER_CPU_PERMISSION_REQUIRED: allow_cpu_fallback=true requerido')
            scene.cycles.device='CPU'
        else:
            scene.cycles.device='GPU'
        result=qa_render_backend(scene,options['allow_cpu_fallback'])
        addon=bpy.context.preferences.addons.get('cycles')
        compute=addon.preferences.compute_device_type if addon else 'NONE'
        if requested in ('OPTIX','CUDA','HIP','METAL','ONEAPI') and compute!=requested:
            raise ValueError('RENDER_REQUESTED_BACKEND_UNAVAILABLE: backend requerido '+requested+', configurado '+compute)
        if options['require_gpu'] and result['actual_backend']=='CPU':
            raise ValueError('RENDER_GPU_REQUIRED: no se permite CPU cuando require_gpu=true')
        if result['actual_backend']=='CPU':
            scene.cycles.device='CPU'
        result['device_readback']=scene.cycles.device
    else:
        if requested not in ('GPU','AUTO'):
            raise ValueError('RENDER_BACKEND_INCOMPATIBLE: EEVEE usa rasterización GPU, no OptiX/CUDA')
        result=qa_render_backend(scene,False)
        result['device_readback']='EEVEE rasterizer'
        if not hasattr(scene,'eevee') or not hasattr(scene.eevee,'use_raytracing'):
            raise ValueError('MATERIAL_RENDER_API_UNSUPPORTED: no existe configuración EEVEE de raytracing instalada')
        scene.eevee.use_raytracing=True
        scene.eevee.ray_tracing_method='SCREEN'
        scene.eevee.taa_render_samples=options['samples']
        result['eevee_readback']={'use_raytracing':scene.eevee.use_raytracing,
            'ray_tracing_method':scene.eevee.ray_tracing_method,'render_samples':scene.eevee.taa_render_samples}
    result['engine_readback']=scene.render.engine
    result['requested_backend']=requested
    result['preferences_modified']=False
    return result

def mr_render(params):
    source=bpy.data.objects.get(params['source_object'])
    if source is None or source.type!='MESH':
        raise ValueError('MATERIAL_RENDER_MESH_REQUIRED: objeto mesh explícito requerido')
    if bpy.context.mode!='OBJECT':
        raise ValueError('MATERIAL_RENDER_OBJECT_MODE_REQUIRED: no se cambia el modo del usuario')
    if len(source.data.vertices)>50000 or not source.data.vertices:
        raise ValueError('MATERIAL_RENDER_CAPACITY: preview admite 1..50000 vértices de malla raw')
    materials=[bpy.data.materials.get(params['source_material']),bpy.data.materials.get(params['candidate_material'])]
    if any(m is None for m in materials):
        raise ValueError('MATERIAL_RENDER_MATERIAL_REQUIRED: material fuente o candidato no existe')
    for material in materials:
        mr_static_material(material,set())
    original_objects=list(bpy.context.scene.objects)
    if len(original_objects)>128 or sum(len(o.pose.bones) for o in original_objects if o.type=='ARMATURE')>4096:
        raise ValueError('MATERIAL_RENDER_CONTEXT_CAPACITY: captura limitada a 128 objetos y 4096 huesos')
    mr_simulation_guard(original_objects)
    rna_before=mr_rna_capture(original_objects)
    before=mr_source_state(source,materials)
    context_before=mr_context();scene=None;owned_objects=[];mesh=None;world=None;camera_data=None;light_data=[];backdrop_mesh=None;backdrop_material=None
    result={'status':'ok','files':[],'camera_settings':[],'lighting_settings':[],
        'warnings':['Malla raw estática con shape keys actuales; no evalúa modifiers, rig, Actions ni simulaciones.',
                    'Render Result se sustituye; las escenas, materiales fuente y preferencias se conservan.',
                    'La iluminación sintética es controlada; no certifica realismo ni apariencia en Unreal.'],
        'errors':[],'comparison_scope':'STATIC_AUTHORED_MESH_MATERIAL_ONLY','features':[mr_features(m) for m in materials]}
    try:
        scene=bpy.data.scenes.new('M9_QA_'+params['token'])
        scene.unit_settings.system='METRIC';scene.unit_settings.scale_length=bpy.context.scene.unit_settings.scale_length
        world=bpy.data.worlds.new(scene.name+'_World');world.use_nodes=True
        world.node_tree.nodes.get('Background').inputs['Color'].default_value=(.035,.035,.035,1)
        world.node_tree.nodes.get('Background').inputs['Strength'].default_value=.18;scene.world=world
        qa_configure(scene,params['width'],params['height'])
        scene.render.film_transparent=False
        scene.view_settings.view_transform='AgX';scene.view_settings.exposure=0.;scene.view_settings.gamma=1.
        result['render_backend']=mr_backend(scene,params)
        if scene.render.engine=='BLENDER_EEVEE':
            result['warnings'].append('EEVEE: SSS/transmisión/opacidad son aproximaciones dependientes del renderer; no equivalen a transporte volumétrico de Cycles.')
        mesh=source.data.copy();mesh.name=scene.name+'_RawMesh'
        if mesh.shape_keys:
            mesh.shape_keys.animation_data_clear()
        clone=bpy.data.objects.new(scene.name+'_Preview',mesh);owned_objects.append(clone);scene.collection.objects.link(clone)
        clone.matrix_world=source.matrix_world.copy()
        points=[clone.matrix_world@v.co for v in mesh.vertices]
        lo=Vector(tuple(min(p[i] for p in points) for i in range(3)));hi=Vector(tuple(max(p[i] for p in points) for i in range(3)))
        centre=(lo+hi)/2;extent=max(hi-lo)
        if not math.isfinite(extent) or extent<=0:
            raise ValueError('MATERIAL_RENDER_BOUNDS_INVALID: malla sin tamaño finito')
        # One material applied to the disposable geometry; source polygon assignments stay intact.
        mesh.materials.clear();mesh.materials.append(materials[0])
        for polygon in mesh.polygons:
            polygon.material_index=0
        camera_data=bpy.data.cameras.new(scene.name+'_Camera');camera_data.type='ORTHO'
        camera=bpy.data.objects.new(camera_data.name,camera_data);owned_objects.append(camera);scene.collection.objects.link(camera);scene.camera=camera
        # Neutral gridded backdrop makes transparency/alpha measurable to the eye without external images.
        backdrop_mesh=bpy.data.meshes.new(scene.name+'_Backdrop')
        size=extent*3
        backdrop_mesh.from_pydata([(-size,-size,0),(size,-size,0),(size,size,0),(-size,size,0)],[],[(0,1,2,3)])
        backdrop_mesh.update();backdrop=bpy.data.objects.new(backdrop_mesh.name,backdrop_mesh);owned_objects.append(backdrop);scene.collection.objects.link(backdrop)
        backdrop.location=(centre.x,centre.y,lo.z-extent*.12)
        backdrop_material=bpy.data.materials.new(scene.name+'_BackdropMaterial');backdrop_material.use_nodes=True
        bnodes=backdrop_material.node_tree.nodes;bsdf=bnodes.get('Principled BSDF')
        checker=bnodes.new('ShaderNodeTexChecker');checker.inputs['Color1'].default_value=(.11,.12,.13,1)
        checker.inputs['Color2'].default_value=(.2,.22,.24,1);checker.inputs['Scale'].default_value=8.
        backdrop_material.node_tree.links.new(checker.outputs['Color'],bsdf.inputs['Base Color']);bsdf.inputs['Roughness'].default_value=.7
        backdrop_mesh.materials.append(backdrop_material)
        setups={'NEUTRAL':[((-2,-3,3),850.,2.2),((2,1,2),500.,2.)],
                'SOFT':[((-1,-2,3),1000.,4.),((2,0,1),350.,3.)],
                'HARD':[((-2,-2,3),900.,.12),((2,1,1),90.,.2)],
                'GRAZING':[((-3,-.2,.25),650.,.6),((2,2,2),100.,1.5)]}
        directions={'FRONT':Vector((0,-1,.12)),'THREE_QUARTER':Vector((1,-1,.55)),'CLOSE_UP':Vector((.25,-1,.18))}
        for lighting in params['lighting_set']:
            lights=[]
            for index,definition in enumerate(setups[lighting]):
                position,power,area=definition
                data=bpy.data.lights.new(scene.name+'_'+lighting+'_'+str(index),'AREA');light_data.append(data)
                data.energy=power*extent*extent;data.shape='DISK';data.size=area*extent
                lamp=bpy.data.objects.new(data.name,data);owned_objects.append(lamp);lights.append(lamp);scene.collection.objects.link(lamp)
                lamp.location=centre+Vector(position)*extent;lamp.rotation_euler=(centre-lamp.location).to_track_quat('-Z','Y').to_euler()
            result['lighting_settings'].append({'lighting':lighting,'lights':[
                {'position':list(l.location),'energy':l.data.energy,'size':l.data.size,'type':l.data.type} for l in lights]})
            for view in params['views']:
                direction=directions[view].normalized();camera.location=centre+direction*extent*4
                camera.rotation_euler=(centre-camera.location).to_track_quat('-Z','Y').to_euler()
                camera_data.ortho_scale=extent*(.72 if view=='CLOSE_UP' else 1.5)
                camera_data.clip_start=extent*.001;camera_data.clip_end=extent*100
                setting={'view':view,'lighting':lighting,'position':list(camera.location),'rotation':list(camera.rotation_euler),'ortho_scale':camera_data.ortho_scale}
                result['camera_settings'].append(setting)
                for case,material in zip(('BEFORE','AFTER'),materials):
                    mesh.materials[0]=material
                    scene.render.filepath=params['output_dir']+'/'+lighting+'_'+view+'_'+case+'.png'
                    operator=bpy.ops.render.render(write_still=True,scene=scene.name)
                    if 'FINISHED' not in operator:
                        raise RuntimeError('MATERIAL_RENDER_FAILED: operador no terminó')
                    result['files'].append({'path':scene.render.filepath,'lighting':lighting,'view':view,'case':case,
                        'variant':'SOURCE' if case=='BEFORE' else 'CANDIDATE','material':material.name})
            for lamp in lights:
                scene.collection.objects.unlink(lamp)
        result['settings']={'width':params['width'],'height':params['height'],'view_transform':scene.view_settings.view_transform,
                            'exposure':scene.view_settings.exposure,'gamma':scene.view_settings.gamma,'engine':scene.render.engine,
                            'source_unit_scale':scene.unit_settings.scale_length,'source_vertices':len(mesh.vertices),
                            'source_polygons':len(mesh.polygons),'equivalent_pairs':True}
    except Exception as exc:
        result['status']='error';result['errors'].append({'code':str(exc).split(':')[0],'message':str(exc)})
    finally:
        for obj in owned_objects:
            bpy.data.objects.remove(obj,do_unlink=True)
        if mesh is not None:bpy.data.meshes.remove(mesh)
        if backdrop_mesh is not None:bpy.data.meshes.remove(backdrop_mesh)
        if backdrop_material is not None:bpy.data.materials.remove(backdrop_material)
        if camera_data is not None:bpy.data.cameras.remove(camera_data)
        for data in light_data:bpy.data.lights.remove(data)
        if scene is not None:bpy.data.scenes.remove(scene)
        if world is not None:bpy.data.worlds.remove(world)
        mr_rna_restore(original_objects,rna_before)
    after=mr_source_state(source,materials)
    result['state_restored']=before==after and context_before==mr_context() and mr_rna_capture(original_objects)==rna_before
    result['source_fingerprint_before']=before;result['source_fingerprint_after']=after
    if not result['state_restored']:
        result['status']='error';result['errors'].append({'code':'MATERIAL_RENDER_STATE_CHANGED','message':'El estado fuente difiere tras el preview'})
    return result
'''
PAYLOAD = RUNTIME + '\nresult=mr_render(params)\n'


def normalize_options(options: dict | None = None) -> dict:
    if options is not None and not isinstance(options,dict):
        raise ValueError('MATERIAL_RENDER_OPTIONS_INVALID: se requiere un objeto de opciones')
    options = dict(options or {})
    unknown = set(options) - {'width','height','lighting_set','views','engine','backend','samples','require_gpu','allow_cpu_fallback'}
    if unknown:
        raise ValueError('MATERIAL_RENDER_OPTIONS_UNKNOWN: ' + ', '.join(sorted(unknown)))
    result = {'width':192,'height':192,'lighting_set':['NEUTRAL'],'views':['THREE_QUARTER','CLOSE_UP'],
              'engine':'BLENDER_EEVEE','backend':'GPU','samples':32,'require_gpu':False,'allow_cpu_fallback':False}
    result.update(options)
    for key,minimum,maximum in (('width',64,512),('height',64,512),('samples',1,128)):
        if isinstance(result[key],bool) or not isinstance(result[key],int) or not minimum<=result[key]<=maximum:
            raise ValueError(f'MATERIAL_RENDER_CAPACITY: {key} debe ser entero {minimum}..{maximum}')
    for key,allowed in (('lighting_set',LIGHTING),('views',VIEWS)):
        values=result[key]
        if not isinstance(values,list) or not values or any(not isinstance(v,str) or v not in allowed for v in values) or len(values)!=len(set(values)):
            raise ValueError(f'MATERIAL_RENDER_OPTIONS_INVALID: {key}')
    for key in ('require_gpu','allow_cpu_fallback'):
        if not isinstance(result[key],bool):
            raise ValueError('MATERIAL_RENDER_OPTIONS_INVALID: '+key+' debe ser booleano')
    if result['engine'] not in ('BLENDER_EEVEE','CYCLES'):
        raise ValueError('MATERIAL_RENDER_ENGINE_UNSUPPORTED: usar EEVEE o Cycles; Workbench no demuestra PBR')
    if result['backend'] not in ('AUTO','GPU','CPU','OPTIX','CUDA','HIP','METAL','ONEAPI'):
        raise ValueError('MATERIAL_RENDER_BACKEND_INVALID')
    if result['require_gpu'] and result['backend']=='CPU':
        raise ValueError('RENDER_GPU_REQUIRED: CPU contradictorio')
    if result['backend']=='CPU' and not result['allow_cpu_fallback']:
        raise ValueError('RENDER_CPU_PERMISSION_REQUIRED')
    return result


def render(bridge, storage, operation_id, source_object, source_material, candidate_material, options=None):
    """Render paired shader evidence; caller owns asset guard/checkpoint/ledger."""
    params=normalize_options(options)
    for name,value in (('source_object',source_object),('source_material',source_material),('candidate_material',candidate_material)):
        if not isinstance(value,str) or not value or len(value)>255:
            raise ValueError('MATERIAL_RENDER_REFERENCE_INVALID: '+name)
        params[name]=value
    directory=storage.reserve('material-previews',operation_id)
    images=directory/'images';images.mkdir(exist_ok=False)
    params.update(output_dir=str(images),token=directory.name[-12:])
    started=time.perf_counter()
    result=bridge.run(PAYLOAD,params)
    result=_verify_images(result,images)
    expected={(light,view,variant) for light in params['lighting_set'] for view in params['views'] for variant in ('SOURCE','CANDIDATE')}
    observed=[(r.get('lighting'),r.get('view'),r.get('variant')) for r in result.get('files',[])]
    if result.get('status')=='ok' and (len(observed)!=len(expected) or set(observed)!=expected):
        result['status']='error'
        result.setdefault('errors',[]).append({'code':'MATERIAL_RENDER_INCOMPLETE','message':'Faltan pares fuente/candidato en las vistas e iluminaciones solicitadas'})
    if any(r.get('width')!=params['width'] or r.get('height')!=params['height'] for r in result.get('files',[])):
        result['status']='error'
        result.setdefault('errors',[]).append({'code':'MATERIAL_RENDER_DIMENSION_MISMATCH','message':'Las dimensiones PNG difieren de la solicitud'})
    for row in result.get('files',[]):
        path=Path(row['path'])
        if path.is_file() and path.parent.resolve()==images.resolve():
            row['sha256']=sha256(path)
    result.update(output_dir=str(directory),lighting_set=params['lighting_set'],operation_id=operation_id)
    result['metrics']={'render_seconds':time.perf_counter()-started,'rendered_images':len(result.get('files',[])),
                       'requested_images':2*len(params['views'])*len(params['lighting_set']),
                       'pixels':params['width']*params['height']*len(result.get('files',[])),
                       'image_bytes':sum(r.get('bytes',0) for r in result.get('files',[])),
                       'peak_memory_bytes':None,'peak_memory_status':'UNKNOWN_NOT_MEASURED'}
    result['hash']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    write_json(directory/'render.json',result)
    result['manifest']=str(directory/'render.json')
    return result
