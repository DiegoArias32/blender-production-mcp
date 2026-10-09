"""Selected-to-active bake preparation and bounded fixture smoke test, Blender-local."""
from ..uv.runtime import UV_RUNTIME
from ..retopology.helpers import GEOMETRY_RUNTIME
from ..qa.capture import _STATE

BAKE_RUNTIME=UV_RUNTIME+'\n'+GEOMETRY_RUNTIME+'\n'+_STATE+r'''
def bake_measure(high,low,options):
    hg=m5_geo(high);lg=m5_geo(low);hb=m5_bvh(hg)
    count=options.get('samples',256)
    if not hg['triangles'] or not lg['triangles']:raise ValueError('BAKE_EMPTY_GEOMETRY')
    indices=sorted(set(min(len(lg['triangles'])-1,int((i+.5)*len(lg['triangles'])/min(count,len(lg['triangles'])))) for i in range(min(count,len(lg['triangles'])))))
    front=[];rear=[];angles=[];distances=[];misses=[];samples=[]
    front_ray=options.get('front_distance_m',options.get('max_ray_distance_m',.05))
    rear_ray=options.get('rear_distance_m',options.get('max_ray_distance_m',.05))
    for index in indices:
        ids=lg['triangles'][index];xyz=[lg['points'][i] for i in ids]
        center=sum(xyz,Vector())/3
        normal=(xyz[1]-xyz[0]).cross(xyz[2]-xyz[0]).normalized()
        nearest=hb.find_nearest(center)
        if nearest[0] is None:misses.append(index);continue
        signed=(nearest[0]-center).dot(normal)
        distance=nearest[3];distances.append(distance)
        if signed>=0:front.append(signed)
        else:rear.append(-signed)
        angles.append(math.degrees(normal.angle(nearest[1])))
        origin=center+normal*front_ray
        hit=hb.ray_cast(origin,-normal,front_ray+rear_ray)
        if hit[0] is None:misses.append(index)
        samples.append({'triangle':index,'center':center,'normal':normal,'signed':signed,'distance':distance})
    pad=options.get('ray_padding_m',.001)
    suggested_front=max(front or [0])*options.get('ray_multiplier',1.25)+pad
    suggested_rear=max(rear or [0])*options.get('ray_multiplier',1.25)+pad
    return {'surface_distance_m':mg_quantiles(distances),'normal_angle_degrees':mg_quantiles(angles),
        'front_deviation_m':mg_quantiles(front),'rear_deviation_m':mg_quantiles(rear),
        'proposed_front_distance_m':suggested_front,'proposed_rear_distance_m':suggested_rear,
        'proposed_cage_offset_m':suggested_front,'samples':len(indices),'ray_misses':len(misses),
        'miss_triangles':misses[:32],'_samples':samples}

def bake_cage_report(high,low,cage,options):
    if len(low.data.vertices)!=len(cage.data.vertices) or [tuple(p.vertices) for p in low.data.polygons]!=[tuple(p.vertices) for p in cage.data.polygons]:
        return {'passed':False,'errors':['CAGE_TOPOLOGY_MISMATCH'],'metrics':{}}
    unit=bpy.context.scene.unit_settings.scale_length
    offsets=[((cage.matrix_world@a.co)-(low.matrix_world@b.co)).length*unit for a,b in zip(cage.data.vertices,low.data.vertices)]
    samples=bake_measure(high,low,options)
    cg=m5_geo(cage);hb=m5_bvh(high);lg=m5_geo(low)
    misses=0;penetrations=0;tested=0;undefined_directions=0
    for row in samples['_samples']:
        ids=lg['triangles'][row['triangle']]
        center=sum((cg['points'][i] for i in ids),Vector())/3
        signed=(center-row['center']).dot(row['normal'])
        if signed+options.get('cage_tolerance_m',.0001)<row['signed']:penetrations+=1
        # Blender bake.cc calc_point_from_barycentric_cage casts from the cage
        # point towards the corresponding LOW point, not along a face normal.
        direction=row['center']-center
        if direction.length_squared==0:
            undefined_directions+=1;misses+=1
        else:
            hit=hb.ray_cast(center,direction.normalized(),options.get('front_distance_m',.05)+options.get('rear_distance_m',.05))
            if hit[0] is None:misses+=1
        tested+=1
    errors=[]
    if penetrations:errors.append('CAGE_PENETRATION_RISK')
    if misses:errors.append('CAGE_RAY_MISS_RISK')
    if undefined_directions:errors.append('CAGE_RAY_DIRECTION_UNDEFINED')
    if offsets and max(offsets)>options.get('max_cage_offset_m',.1):errors.append('CAGE_TOO_FAR')
    # The BVH overlap result is an indicator and adjacent faces are filtered explicitly.
    cb=m5_bvh(cg);overlaps=cb.overlap(cb);potential=0
    for a,b in overlaps:
        if a<b and not set(cg['triangles'][a]).intersection(cg['triangles'][b]):potential+=1
    if potential:errors.append('CAGE_SELF_INTERSECTION_INDICATOR')
    return {'passed':not errors,'errors':errors,'metrics':{'offset_m':mg_quantiles(offsets),'sampled_penetration_risks':penetrations,
        'sampled_ray_misses':misses,'undefined_ray_directions':undefined_directions,'samples':tested,'self_intersection_pairs_indicator':potential}}

def bake_geometry_adapter(obj,options):
    active=[m for m in obj.modifiers if m.show_render]
    adapter=options.get('geometry_adapter','BASE_STATIC')
    errors=[];delta=0.0
    if obj.constraints or obj.animation_data:errors.append('BAKE_ANIMATED_OBJECT_UNSUPPORTED:'+obj.name)
    if obj.parent and not (adapter=='BOUND_REST_MESH' and obj.parent.type=='ARMATURE' and obj.parent_type=='OBJECT'):
        errors.append('BAKE_PARENT_UNSUPPORTED:'+obj.name)
    if active:
        if adapter!='BOUND_REST_MESH' or len(active)!=1 or active[0].type!='ARMATURE' or not active[0].object:
            errors.append('BAKE_EVALUATED_GEOMETRY_UNSUPPORTED:'+obj.name)
        else:
            rig=active[0].object
            if rig.data.pose_position!='POSE' or rig.constraints or rig.animation_data or any(b.constraints for b in rig.pose.bones):
                errors.append('BAKE_RIG_STATE_UNSUPPORTED:'+rig.name)
            evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());mesh=evaluated.to_mesh()
            try:
                if len(mesh.vertices)!=len(obj.data.vertices) or len(mesh.loops)!=len(obj.data.loops):errors.append('BAKE_EVALUATED_TOPOLOGY_CHANGED:'+obj.name)
                else:
                    unit=bpy.context.scene.unit_settings.scale_length
                    delta=max(((obj.matrix_world @ a.co)-(evaluated.matrix_world @ b.co)).length*unit for a,b in zip(obj.data.vertices,mesh.vertices)) if mesh.vertices else 0.0
                    if delta>options.get('rest_geometry_tolerance_m',1e-6):errors.append('BAKE_DEFORMED_REST_MESH:'+obj.name)
            finally:evaluated.to_mesh_clear()
    return {'adapter':adapter,'evaluated_max_delta_m':delta,'passed':not errors,'errors':errors}

def bake_validate_mesh(high,low,options):
    uv=uv_metrics(low,options)
    rays=bake_measure(high,low,options)
    errors=list(uv['errors']);warnings=[]
    if rays['ray_misses']:errors.append('BAKE_RAY_MISSES')
    if rays['surface_distance_m']['maximum'] is None or rays['surface_distance_m']['maximum']>options.get('max_surface_deviation_m',.05):errors.append('BAKE_SURFACE_DISTANCE_EXCESSIVE')
    if options.get('front_distance_m',.05)+1e-8<rays['front_deviation_m']['maximum'] if rays['front_deviation_m']['maximum'] is not None else False:errors.append('BAKE_FRONT_RAY_TOO_SHORT')
    if options.get('rear_distance_m',.05)+1e-8<rays['rear_deviation_m']['maximum'] if rays['rear_deviation_m']['maximum'] is not None else False:errors.append('BAKE_REAR_RAY_TOO_SHORT')
    if max(options.get('front_distance_m',.05),options.get('rear_distance_m',.05))>options.get('max_ray_distance_m',.2):errors.append('BAKE_RAY_DISTANCE_TOO_LARGE')
    topology=low.data
    if any(len(p.vertices)<3 or p.area<1e-15 for p in topology.polygons):errors.append('BAKE_DEGENERATE_TOPOLOGY')
    # Edge manifold checks distinguish open sheets (explicit policy) from actual non-manifold fans.
    uses=[0]*len(topology.edges)
    for loop in topology.loops:uses[loop.edge_index]+=1
    if any(n>2 or n==0 for n in uses):errors.append('BAKE_NON_MANIFOLD')
    if any(n==1 for n in uses) and not options.get('allow_open_surface',False):errors.append('BAKE_OPEN_BOUNDARY')
    if not low.data.materials or any(m is None for m in low.data.materials):errors.append('BAKE_MATERIAL_SEMANTICS_MISSING')
    if any(p.material_index>=len(low.data.materials) for p in low.data.polygons):errors.append('BAKE_MATERIAL_ID_INVALID')
    tangent={'valid':False,'loops':0,'errors':[]}
    if low.data.uv_layers:
        try:
            low.data.calc_tangents(uvmap=uv['active_layer'])
            invalid=sum(not all(math.isfinite(c) for c in loop.tangent) or loop.tangent.length<.5 or abs(loop.bitangent_sign)<.5 for loop in low.data.loops)
            tangent={'valid':invalid==0,'loops':len(low.data.loops),'invalid_loops':invalid,'basis':'Blender Mesh.calc_tangents + corner normals + bitangent_sign; MikkTSpace workflow'}
            if invalid:errors.append('BAKE_TANGENT_INVALID')
        except Exception as exc:
            tangent['errors']=[str(exc)];errors.append('BAKE_TANGENT_PREREQUISITES_INVALID')
        finally:low.data.free_tangents()
    else:errors.append('BAKE_TANGENT_UV_MISSING')
    # This operator uses the base mesh contract. An evaluated topology-changing modifier cannot be
    # silently mixed with base UV/cage correspondences.
    adapters={}
    for obj in (high,low):
        adapter=bake_geometry_adapter(obj,options);adapters[obj.name]=adapter;errors.extend(adapter['errors'])
        if obj.data.shape_keys and any(abs(b.value)>1e-8 for b in obj.data.shape_keys.key_blocks):errors.append('BAKE_ACTIVE_MORPH_UNSUPPORTED:'+obj.name)
    cage=None
    if options.get('cage_object'):
        cage=bake_cage_report(high,low,m5_obj(options['cage_object']),options)
        errors.extend(cage['errors'])
    if len(uv.get('tiles',[]))>1:warnings.append('Multi-UDIM geometry is prepared; smoke maps are isolated per tile, never a production texture bake.')
    if not cage:warnings.append('No explicit cage; selected-to-active uses declared extrusion/ray distances.')
    errors=list(dict.fromkeys(errors))
    rays.pop('_samples',None)
    return {'readiness':'NOT_READY' if errors else 'READY_WITH_WARNINGS' if warnings else 'READY','passed':not errors,
        'errors':errors,'warnings':warnings,'uv':uv,'tangents':tangent,'rays':rays,'cage':cage,'geometry_adapter':adapters,
        'manifest':{'high':high.name,'low':low.name,'material_semantics':[m.name if m else None for m in low.data.materials],
            'UDIM_tiles':[r['tile'] for r in uv.get('tiles',[])],'normal_space':'TANGENT','normal_convention':options.get('normal_convention','OPENGL'),
            'world_units':'meters','transforms_applied':False}}

def m5_plan(high,low,options,relation):
    offset=options['extrusion_m'];measure=bake_measure(high,low,options)
    reasons=[]
    if not low.get('production_candidate'):reasons.append('NOT_A_CANDIDATE')
    if low.data.shape_keys:reasons.append('CAGE_MORPHS_UNSUPPORTED: cage extraction requires a morph-free low candidate')
    if offset+options.get('cage_tolerance_m',.0001)<measure['front_deviation_m']['maximum'] if measure['front_deviation_m']['maximum'] is not None else False:reasons.append('CAGE_TOO_CLOSE')
    if offset>options.get('max_cage_offset_m',.1):reasons.append('CAGE_TOO_FAR')
    measure.pop('_samples',None)
    return {'supported':not reasons,'reasons':reasons,'errors':reasons,'candidate_role':'CAGE','extrusion_m':offset,'measurements':measure,
        'strategy':'base_corner_normal_extrusion_new_cage','apply':False,'warnings':['Cage has LOW topology; source LOW remains the game mesh, not replaced by the cage.']}

def m5_apply(high,low,candidate,proposal,options):
    unit=bpy.context.scene.unit_settings.scale_length
    normal_matrix=low.matrix_world.to_3x3().inverted().transposed()
    inverse=candidate.matrix_world.inverted().to_3x3()
    offset=options['extrusion_m']/unit
    for vertex in candidate.data.vertices:
        normal=(normal_matrix @ low.data.vertices[vertex.index].normal).normalized()
        vertex.co+=inverse @ (normal*offset)
    candidate.data.update()
    candidate['production_bake_cage']=json.dumps({'high':high.name,'low':low.name,'extrusion_m':options['extrusion_m']})
    return {'extrusion_m':options['extrusion_m'],'cage':candidate.name}

def m5_verify(high,low,candidate,proposal,options):
    report=bake_cage_report(high,low,candidate,options)
    return report

def bake_pixel_report(image,low,uv_name,tile):
    width,height=image.size
    pixels=[0.0]*(width*height*4);image.pixels.foreach_get(pixels)
    layer,values=uv_data(low,uv_name);low.data.calc_loop_triangles()
    expected=set();offx,offy=(tile-1001)%10,(tile-1001)//10
    for tri in low.data.loop_triangles:
        uv=[values[i] for i in tri.loops]
        if tile_of(uv)!=tile:continue
        points=[((p[0]-offx)*width,(p[1]-offy)*height) for p in uv]
        for y in range(max(0,int(min(p[1] for p in points))),min(height,int(math.ceil(max(p[1] for p in points))))):
            for x in range(max(0,int(min(p[0] for p in points))),min(width,int(math.ceil(max(p[0] for p in points))))):
                if triangle_overlap(points,[(x+.49,y+.49),(x+.51,y+.49),(x+.5,y+.51)])>1e-8:expected.add(y*width+x)
    invalid=unchanged=0
    large_jump=0;jump_pairs=0;colors=[]
    for index in expected:
        color=pixels[index*4:index*4+4];colors.extend(color[:3])
        if any(not math.isfinite(v) for v in color):invalid+=1
        if max(abs(color[i]-[.02,.91,.31,0][i]) for i in range(4))<1e-4:unchanged+=1
        x,y=index%width,index//width
        for neighbor in (index+1,index+width):
            if neighbor in expected and (neighbor!=index+1 or x<width-1):
                jump=max(abs(pixels[index*4+c]-pixels[neighbor*4+c]) for c in (0,1,2));jump_pairs+=1
                if jump>.5:large_jump+=1
    return {'expected_covered_pixels':len(expected),'invalid_pixels':invalid,'unchanged_sentinel_pixels':unchanged,
        'coverage_observed_fraction':(len(expected)-unchanged)/len(expected) if expected else None,
        'large_neighbor_color_jumps':large_jump,'neighbor_pairs':jump_pairs,'component_min':min(colors) if colors else None,
        'component_max':max(colors) if colors else None,'rgb_distribution':[{'minimum':min((pixels[i*4+c] for i in expected),default=None),
            'maximum':max((pixels[i*4+c] for i in expected),default=None)} for c in (0,1,2)],
        'limitations':['Sentinel coverage is an indicator; BVH ray witnesses independently check misses.',
        'Color jumps are indicators, not full seam or artistic texture QA.']}

def bake_setting_state(bake):
    return {'use_selected_to_active':bake.use_selected_to_active,'use_clear':bake.use_clear,'use_cage':bake.use_cage,
        'cage_object':bake.cage_object,'cage_extrusion':bake.cage_extrusion,'max_ray_distance':bake.max_ray_distance,
        'normal_space':bake.normal_space,'normal_r':bake.normal_r,'normal_g':bake.normal_g,'normal_b':bake.normal_b,
        'margin':bake.margin,'target':bake.target,'save_mode':bake.save_mode}

def bake_restore_settings(bake,state):
    bake.use_selected_to_active=state['use_selected_to_active'];bake.use_clear=state['use_clear'];bake.use_cage=state['use_cage']
    bake.cage_object=state['cage_object'];bake.cage_extrusion=state['cage_extrusion'];bake.max_ray_distance=state['max_ray_distance']
    bake.normal_space=state['normal_space'];bake.normal_r=state['normal_r'];bake.normal_g=state['normal_g'];bake.normal_b=state['normal_b']
    bake.margin=state['margin'];bake.target=state['target'];bake.save_mode=state['save_mode']

def bake_smoke(high,low,options,paths):
    readiness=bake_validate_mesh(high,low,options)
    if not readiness['passed']:raise ValueError('BAKE_NOT_READY: '+','.join(readiness['errors']))
    scene=bpy.context.scene
    original_engine=scene.render.engine;device=scene.cycles.device;samples=scene.cycles.samples
    bake=scene.render.bake
    bake_state=bake_setting_state(bake)
    selected=list(bpy.context.selected_objects);active=bpy.context.view_layer.objects.active
    hidden={o.name:(o.hide_render,o.hide_get()) for o in bpy.context.scene.objects}
    low_materials=list(low.data.materials);material_indices=[p.material_index for p in low.data.polygons]
    temps=[];images=[];objects=[];meshes=[];reports=[];original_uv=low.data.uv_layers.active.name
    uv_copy=None
    try:
        # Bake writes evaluated UV/material bookkeeping in Blender. Isolate that entire effect on
        # an owned object+mesh, instead of trying to predict every internal UV selection attribute.
        source_low=low;low=source_low.copy();low.data=source_low.data.copy();low.name='M5_SMOKE_LOW_'+options['smoke_id']
        bpy.context.scene.collection.objects.link(low);objects.append(low);meshes.append(low.data)
        scene.render.engine='CYCLES'
        backend=qa_render_backend(scene,options.get('allow_cpu_fallback',False))
        if backend['actual_backend']=='CPU':scene.cycles.device='CPU'
        scene.cycles.samples=options.get('bake_samples',4)
        for other in bpy.context.scene.objects:
            other.select_set(False)
            if other not in (high,low):other.hide_render=True
        high.hide_render=False;low.hide_render=False;high.hide_set(False);low.hide_set(False)
        if options.get('cage_object'):
            cage_obj=m5_obj(options['cage_object']).copy();cage_obj.name='M5_SMOKE_CAGE_'+options['smoke_id']
            bpy.context.scene.collection.objects.link(cage_obj);objects.append(cage_obj)
            cage_obj.hide_render=False;cage_obj.hide_set(False)
        high.select_set(True);low.select_set(True);bpy.context.view_layer.objects.active=low
        material=bpy.data.materials.new('M5_SMOKE_TEMP_'+options['smoke_id']);temps.append(material);material.use_nodes=True
        low.data.materials.clear();low.data.materials.append(material)
        for p in low.data.polygons:p.material_index=0
        original_layer,original_values=uv_data(low,original_uv)
        uv_copy=low.data.uv_layers.new(name='M5_SMOKE_UV_'+options['smoke_id']);low.data.uv_layers.active=uv_copy
        bake.use_selected_to_active=True;bake.use_clear=False;bake.use_cage=bool(options.get('cage_object'))
        bake.cage_object=objects[1] if len(objects)>1 else None
        bake.cage_extrusion=options.get('front_distance_m',.05)/bpy.context.scene.unit_settings.scale_length
        bake.max_ray_distance=(options.get('front_distance_m',.05)+options.get('rear_distance_m',.05))/bpy.context.scene.unit_settings.scale_length
        bake.normal_space='TANGENT';bake.normal_r='POS_X';bake.normal_g='NEG_Y' if options.get('normal_convention','OPENGL')=='DIRECTX' else 'POS_Y';bake.normal_b='POS_Z'
        bake.margin=options.get('bake_margin_px',2);bake.target='IMAGE_TEXTURES';bake.save_mode='INTERNAL'
        for item in paths:
            tile=item['tile'];offx,offy=(tile-1001)%10,(tile-1001)//10
            # Unrequested tile polygons move off the smoke tile; all source coordinates restored.
            for polygon in low.data.polygons:
                face_tile=tile_of([original_values[c] for c in polygon.loop_indices])
                for c in polygon.loop_indices:
                    uv_copy.uv[c].vector=(original_values[c][0]-offx,original_values[c][1]-offy) if face_tile==tile else (20+original_values[c][0],20+original_values[c][1])
            image=bpy.data.images.new('M5_SMOKE_'+options['smoke_id']+'_'+str(tile)+'_'+item['map'],width=options['smoke_resolution'],height=options['smoke_resolution'],alpha=True,float_buffer=True)
            images.append(image);image.generated_color=(.02,.91,.31,0);image.colorspace_settings.name='Non-Color'
            node=material.node_tree.nodes.new('ShaderNodeTexImage');node.image=image;material.node_tree.nodes.active=node
            outcome=bpy.ops.object.bake(type=item['map'],use_selected_to_active=True,uv_layer=uv_copy.name,use_clear=False,
                use_cage=len(objects)>1,cage_object=objects[1].name if len(objects)>1 else '',
                max_ray_distance=bake.max_ray_distance,cage_extrusion=bake.cage_extrusion,normal_space='TANGENT',
                normal_r=bake.normal_r,normal_g=bake.normal_g,normal_b=bake.normal_b,margin=bake.margin)
            if 'FINISHED' not in outcome:raise ValueError('SMOKE_BAKE_FAILED: '+str(outcome))
            image.filepath_raw=item['path'];image.file_format='PNG';image.save()
            metrics=bake_pixel_report(image,low,original_uv,tile)
            reports.append({'map':item['map'],'tile':tile,'path':item['path'],'metrics':metrics})
            material.node_tree.nodes.remove(node)
        return {'status':'success','readiness':readiness['readiness'],'render_backend':backend,'artifacts':reports,'state_restored':True,
            'bake_target_policy':'owned_transient_LOW_object_and_mesh','cage_policy':'owned_transient_short_name_object' if len(objects)>1 else 'explicit_ray_extrusion',
            'passed':all(r['metrics']['expected_covered_pixels']>0 and not r['metrics']['invalid_pixels'] and not r['metrics']['unchanged_sentinel_pixels'] for r in reports),
            'warnings':['Low-resolution diagnostic maps only; not production textures or full seam QA.']}
    finally:
        if uv_copy:low.data.uv_layers.remove(uv_copy)
        low.data.uv_layers.active=low.data.uv_layers.get(original_uv)
        low.data.materials.clear()
        for material in low_materials:low.data.materials.append(material)
        for p,i in zip(low.data.polygons,material_indices):p.material_index=i
        for obj in bpy.context.scene.objects:
            obj.select_set(False)
            if obj.name in hidden:obj.hide_render=hidden[obj.name][0];obj.hide_set(hidden[obj.name][1])
        for obj in selected:obj.select_set(True)
        bpy.context.view_layer.objects.active=active
        scene.render.engine=original_engine;scene.cycles.device=device;scene.cycles.samples=samples
        bake_restore_settings(bake,bake_state)
        for material in temps:bpy.data.materials.remove(material)
        for image in images:bpy.data.images.remove(image)
        for obj in objects:bpy.data.objects.remove(obj,do_unlink=True)
        for mesh in meshes:bpy.data.meshes.remove(mesh)
'''
