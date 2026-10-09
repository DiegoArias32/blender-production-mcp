"""Blender-local UV/UDIM analysis and conservative candidate operations."""
from . import algorithms
import inspect

# Pure functions are shared verbatim with unit tests; no dynamic execution crosses the bridge.
_PURE = '\n\n'.join(inspect.getsource(getattr(algorithms, n)) for n in ('signed_area', 'triangle_overlap', 'tile_of', 'shelf_pack'))

UV_RUNTIME = 'import math\n' + _PURE + r'''
import bpy
from mathutils import Vector

def uv_layer(obj, name=None):
    layer = obj.data.uv_layers.get(name) if name else obj.data.uv_layers.active
    if layer is None:
        raise ValueError('UV_MISSING: an active or explicitly named UV layer is required')
    return layer

def uv_data(obj, name=None):
    me = obj.data
    layer = uv_layer(obj, name)
    values = [0.0] * (2 * len(me.loops))
    layer.uv.foreach_get('vector', values)
    return layer, [(values[2*i], values[2*i+1]) for i in range(len(me.loops))]

def uv_islands(obj, values):
    me = obj.data
    parent = list(range(len(me.polygons)))
    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    seen = {}
    for face in me.polygons:
        ids = list(face.loop_indices)
        for a,b in zip(ids,ids[1:]+ids[:1]):
            va,vb = me.loops[a].vertex_index,me.loops[b].vertex_index
            key = tuple(sorted((va,vb)))
            uv_pair = (values[a],values[b]) if va < vb else (values[b],values[a])
            for other,other_uv in seen.get(key,[]):
                if max(abs(uv_pair[k][j]-other_uv[k][j]) for k in (0,1) for j in (0,1)) < 1e-6:
                    parent[root(face.index)] = root(other)
            seen.setdefault(key,[]).append((face.index,uv_pair))
    rows = {}
    face_island = []
    for face in me.polygons:
        rid = root(face.index)
        face_island.append(rid)
        rows.setdefault(rid,{'faces':[],'loops':[]})['faces'].append(face.index)
        rows[rid]['loops'].extend(face.loop_indices)
    for rid,row in rows.items():
        points=[values[i] for i in row['loops']]
        row['min']=[min(p[k] for p in points) for k in (0,1)]
        row['max']=[max(p[k] for p in points) for k in (0,1)]
        tiles=set(tile_of([values[c] for c in me.polygons[f].loop_indices]) for f in row['faces'])
        row['tiles']=([None] if None in tiles else [])+sorted(t for t in tiles if t is not None)
    return rows,face_island

def uv_tile_order(row):
    return -1 if row['tile'] is None else row['tile']

def uv_metrics(obj,options):
    me=obj.data
    if len(me.loops)>2000000:
        raise ValueError('UV_CAPACITY_EXCEEDED: at most 2 million base-mesh loops per analysis')
    if not me.uv_layers:
        return {'object':obj.name,'layers':[],'active_layer':None,'valid':False,'errors':['UV_MISSING'],'metrics':{}}
    layer,values=uv_data(obj,options.get('uv_layer'))
    if any(not math.isfinite(c) for uv in values for c in uv):
        return {'object':obj.name,'active_layer':layer.name,'valid':False,'errors':['UV_NONFINITE'],'metrics':{}}
    islands,face_island=uv_islands(obj,values)
    me.calc_loop_triangles()
    scale=bpy.context.scene.unit_settings.scale_length
    resolution=options.get('resolution',1024)
    per_tile={}
    per_material={}
    triangles=[]
    zero=flipped=outside=0
    distortions=[]
    densities=[]
    world_area=uv_area=0.0
    angle_errors=[]
    problems=[]
    for tri in me.loop_triangles:
        points=[values[c] for c in tri.loops]
        xyz=[(obj.matrix_world @ me.vertices[i].co)*scale for i in tri.vertices]
        area=(xyz[1]-xyz[0]).cross(xyz[2]-xyz[0]).length*0.5
        signed=signed_area(points)
        ua=abs(signed)
        zero+=int(ua<1e-12)
        flipped+=int(signed < -1e-12)
        tile=tile_of(points)
        outside+=int(tile is None)
        world_area+=area
        uv_area+=ua
        tile_res=options.get('tile_resolutions',{}).get(str(tile),resolution)
        density=math.sqrt(ua/max(area,1e-30))*tile_res
        if area>1e-15 and ua>1e-12:
            densities.append(density)
            a,b=xyz[1]-xyz[0],xyz[2]-xyz[0]
            x=a.length
            y=b.dot(a/x) if x>1e-15 else 0.0
            z=math.sqrt(max(0.0,b.length_squared-y*y))
            if x>1e-15 and z>1e-15:
                du1,dv1=points[1][0]-points[0][0],points[1][1]-points[0][1]
                du2,dv2=points[2][0]-points[0][0],points[2][1]-points[0][1]
                aa,cc=du1/x,dv1/x
                bb,dd=(du2-aa*y)/z,(dv2-cc*y)/z
                s=aa*aa+bb*bb+cc*cc+dd*dd
                det=(aa*dd-bb*cc)**2
                disc=math.sqrt(max(0,s*s-4*det))
                larger,smaller=(s+disc)*0.5,(s-disc)*0.5
                distortion=math.sqrt(larger/max(smaller,1e-30))
                distortions.append(distortion)
                if distortion>options.get('max_stretch_ratio',8.0) and len(problems)<32:
                    problems.append({'polygon':tri.polygon_index,'island':face_island[tri.polygon_index],'stretch_ratio':distortion})
            for i in range(3):
                va,vb=xyz[(i+1)%3]-xyz[i],xyz[(i+2)%3]-xyz[i]
                uva=Vector(points[(i+1)%3])-Vector(points[i]);uvb=Vector(points[(i+2)%3])-Vector(points[i])
                if min(va.length,vb.length,uva.length,uvb.length)>1e-15:
                    angle_errors.append(abs(math.degrees(va.angle(vb)-uva.angle(uvb))))
        row=per_tile.setdefault(str(tile),{'tile':tile,'triangles':0,'uv_area':0.0,'world_area_m2':0.0,'densities':[]})
        row['triangles']+=1;row['uv_area']+=ua;row['world_area_m2']+=area
        if area>1e-15 and ua>1e-12:row['densities'].append(density)
        mat=me.polygons[tri.polygon_index].material_index
        mr=per_material.setdefault(str(mat),{'material_index':mat,'world_area_m2':0.0,'uv_area':0.0})
        mr['world_area_m2']+=area;mr['uv_area']+=ua
        triangles.append({'points':points,'polygon':tri.polygon_index,'island':face_island[tri.polygon_index],'tile':tile,'area':ua})
    # Broad phase has a documented finite pair budget; exhaustion never reports validation pass.
    grid={};pairs=set();overlaps=[];overlap_area=0.0;tested=0;exhausted=False
    cells=max(8,min(256,int(math.sqrt(max(1,len(triangles))/3))))
    for i,tri in enumerate(triangles):
        if tri['area']<1e-12:continue
        points=tri['points'];lo=[min(p[k] for p in points) for k in (0,1)];hi=[max(p[k] for p in points) for k in (0,1)]
        keys=[(x,y) for x in range(math.floor(lo[0]*cells),math.floor(hi[0]*cells)+1) for y in range(math.floor(lo[1]*cells),math.floor(hi[1]*cells)+1)]
        if len(keys)>65536:exhausted=True;break
        candidates=set(j for key in keys for j in grid.get(key,[]))
        for j in candidates:
            if triangles[j]['polygon']==tri['polygon']:continue
            pair=(j,i)
            if pair in pairs:continue
            pairs.add(pair);tested+=1
            if tested>3000000:exhausted=True;break
            a=triangle_overlap(tri['points'],triangles[j]['points'])
            if a>options.get('overlap_area_tolerance',1e-10):
                overlap_area+=a
                if len(overlaps)<32:overlaps.append({'polygon_a':triangles[j]['polygon'],'polygon_b':tri['polygon'],'area':a})
        if exhausted:break
        for key in keys:grid.setdefault(key,[]).append(i)
    requested_padding=options.get('padding_px',2.0)/resolution
    border_padding=min((min(p[0]-math.floor(sum(values[c][0] for c in me.polygons[f].loop_indices)/me.polygons[f].loop_total),
                            math.floor(sum(values[c][0] for c in me.polygons[f].loop_indices)/me.polygons[f].loop_total)+1-p[0],
                            p[1]-math.floor(sum(values[c][1] for c in me.polygons[f].loop_indices)/me.polygons[f].loop_total),
                            math.floor(sum(values[c][1] for c in me.polygons[f].loop_indices)/me.polygons[f].loop_total)+1-p[1])
                        for f in range(len(me.polygons)) for p in [values[c] for c in me.polygons[f].loop_indices]),default=0.0)
    # Packing cells: bounding-box separation certifies padding for this deterministic packer. General
    # non-rectangular arrangements whose boxes overlap are not silently certified.
    box_padding=1.0
    island_rows=list(islands.items())
    padding_pairs=0
    for ai,(rid,a) in enumerate(island_rows):
        for sid,b in island_rows[ai+1:]:
            if a['tiles']!=b['tiles']:continue
            padding_pairs+=1
            if padding_pairs>1000000:exhausted=True;break
            dx=max(a['min'][0]-b['max'][0],b['min'][0]-a['max'][0],0)
            dy=max(a['min'][1]-b['max'][1],b['min'][1]-a['max'][1],0)
            box_padding=min(box_padding,math.hypot(dx,dy))
        if exhausted:break
    minimum_padding=min(border_padding,box_padding)
    def summary(data):
        data=sorted(data)
        return {'min':data[0] if data else None,'median':data[len(data)//2] if data else None,
                'p95':data[min(len(data)-1,int(.95*(len(data)-1)))] if data else None,'max':data[-1] if data else None}
    tiles=[]
    for row in per_tile.values():
        ds=row.pop('densities');row['texels_per_meter']=summary(ds)
        row['packing_occupancy']=row['uv_area']
        tiles.append(row)
    for row in per_material.values():
        row['texels_per_meter']=math.sqrt(row['uv_area']/max(row['world_area_m2'],1e-30))*resolution
    errors=[]
    if zero:errors.append('UV_ZERO_AREA')
    if flipped and not options.get('allow_flipped',False):errors.append('UV_FLIPPED')
    if outside:errors.append('UV_CROSS_TILE_OR_OUT_OF_RANGE')
    if overlaps and not options.get('allow_overlap',False):errors.append('UV_OVERLAP')
    if exhausted:errors.append('UV_CAPACITY_EXCEEDED')
    if distortions and max(distortions)>options.get('max_stretch_ratio',8.0):errors.append('UV_STRETCH_EXCESSIVE')
    if options.get('check_padding',True) and minimum_padding+1e-7<requested_padding:errors.append('UV_PADDING_NOT_CERTIFIED')
    expected=options.get('expected_tiles')
    actual=sorted(row['tile'] for row in tiles if row['tile'] is not None)
    if expected is not None and actual!=sorted(expected):errors.append('UDIM_TILES_MISMATCH')
    return {'object':obj.name,'layers':[{'name':l.name,'active':l==me.uv_layers.active,'active_render':l.active_render} for l in me.uv_layers],
            'active_layer':layer.name,'valid':not errors,'errors':errors,'warnings':['Padding uses conservative island bounding boxes; interleaved layouts may require a different explicit strategy.',
            'Packing occupancy is summed UV area, not a union estimate when overlaps are allowed.'],
            'metrics':{'triangles':len(triangles),'islands':len(islands),'zero_area_triangles':zero,'flipped_triangles':flipped,'invalid_tile_triangles':outside,
            'overlap_pairs_reported':len(overlaps),'overlap_area_pair_sum':overlap_area,'overlap_pairs_tested':tested,'overlap_scan_complete':not exhausted,
            'world_area_m2':world_area,'uv_area':uv_area,'stretch_ratio':summary(distortions),'angle_distortion_degrees':summary(angle_errors),
            'texels_per_meter':summary(densities),'minimum_padding_lower_bound_uv':minimum_padding,'requested_padding_uv':requested_padding},
            'tiles':sorted(tiles,key=uv_tile_order), 'regions':list(per_material.values())[:32],
            'problem_islands':problems,'overlaps':overlaps,'island_summary':[{'island':k,'faces':len(v['faces']),'tiles':v['tiles'],'min':v['min'],'max':v['max']} for k,v in list(islands.items())[:32]]}

def uv_assignment(obj,values,options):
    islands,face_island=uv_islands(obj,values)
    assignments=options.get('assignments',[])
    if not assignments:
        raise ValueError('UDIM_ASSIGNMENT_REQUIRED: explicit material or polygon regions are required')
    face_tiles={}
    for assignment in assignments:
        if 'material_index' in assignment:
            faces=[f.index for f in obj.data.polygons if f.material_index==assignment['material_index']]
        else:faces=assignment['faces']
        if not faces:raise ValueError('UDIM_EMPTY_REGION: '+assignment['region'])
        for face in faces:
            if face<0 or face>=len(obj.data.polygons):raise ValueError('UDIM_FACE_INVALID')
            if face in face_tiles:raise ValueError('UDIM_AMBIGUOUS_ASSIGNMENT: face assigned twice')
            face_tiles[face]=assignment['tile']
    if len(face_tiles)!=len(obj.data.polygons):raise ValueError('UDIM_INCOMPLETE_ASSIGNMENT: every face must have one explicit tile')
    island_tiles={}
    for island,row in islands.items():
        tiles=set(face_tiles[f] for f in row['faces'])
        if len(tiles)!=1:raise ValueError('UDIM_SEAM_REQUIRED: an island spans semantic regions assigned to different tiles; split it in an explicit UV proposal first')
        island_tiles[island]=next(iter(tiles))
    return islands,island_tiles

def uv_pack_mesh(obj,options,udim=False):
    layer,values=uv_data(obj,options.get('uv_layer'))
    if udim:islands,assignment=uv_assignment(obj,values,options)
    else:
        islands,unused=uv_islands(obj,values)
        assignment={k:(row['tiles'][0] if len(row['tiles'])==1 and row['tiles'][0] else 1001) for k,row in islands.items()}
    padding=options.get('padding_px',2.0)/options.get('resolution',1024)
    for tile in sorted(set(assignment.values())):
        members={k:v for k,v in islands.items() if assignment[k]==tile}
        boxes={k:(row['max'][0]-row['min'][0],row['max'][1]-row['min'][1]) for k,row in members.items()}
        positions,scale=shelf_pack(boxes,padding*1.05,options.get('rotate',False))
        offx,offy=(tile-1001)%10,(tile-1001)//10
        for k,row in members.items():
            p=positions[k]
            for loop in row['loops']:
                x,y=values[loop][0]-row['min'][0],values[loop][1]-row['min'][1]
                if p['rotate']:x,y=y,boxes[k][0]-x
                layer.uv[loop].vector=(offx+p['x']+x*scale,offy+p['y']+y*scale)
    obj.data.update()
    return {'islands':len(islands),'tiles':sorted(set(assignment.values())),'strategy':'deterministic_shelf','rotation_allowed':options.get('rotate',False)}

def uv_generate_mesh(obj,options,proposal):
    me=obj.data
    name=options.get('uv_layer','GameUV')
    layer=me.uv_layers.get(name) or me.uv_layers.new(name=name)
    me.uv_layers.active=layer
    for edge in me.edges:edge.use_seam=edge.index in set(proposal['seam_edges'])
    selected=list(bpy.context.selected_objects);active=bpy.context.view_layer.objects.active
    try:
        for other in selected:other.select_set(False)
        obj.hide_set(False);obj.select_set(True);bpy.context.view_layer.objects.active=obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        if options.get('unwrap_method','ANGLE_BASED')=='SMART_PROJECT':
            outcome=bpy.ops.uv.smart_project(angle_limit=math.radians(options.get('seam_angle_degrees',60.0)),
                island_margin=.001,margin_method='ADD',rotate_method='AXIS_ALIGNED',area_weight=0.0,correct_aspect=False,scale_to_bounds=False)
        else:outcome=bpy.ops.uv.unwrap(method='ANGLE_BASED',fill_holes=True,correct_aspect=False,margin=0.001)
        if 'FINISHED' not in outcome:raise ValueError('UV_UNWRAP_FAILED')
    finally:
        if obj.mode=='EDIT':bpy.ops.object.mode_set(mode='OBJECT')
        obj.select_set(False)
        for other in selected:other.select_set(True)
        bpy.context.view_layer.objects.active=active
    return uv_pack_mesh(obj,options)

def uv_seam_plan(obj,options):
    me=obj.data
    faces={}
    for face in me.polygons:
        for loop in face.loop_indices:faces.setdefault(me.loops[loop].edge_index,[]).append(face)
    seams=set(options.get('seam_edges',[]));evidence={'explicit':len(seams),'material_boundary':0,'angle':0,'sharp':0,'boundary':0}
    for edge in me.edges:
        fs=faces.get(edge.index,[])
        reason=None
        if len(fs)!=2:reason='boundary'
        elif options.get('material_seams',True) and fs[0].material_index!=fs[1].material_index:reason='material_boundary'
        elif options.get('angle_seams',True) and math.degrees(fs[0].normal.angle(fs[1].normal))>=options.get('seam_angle_degrees',60.0):reason='angle'
        elif options.get('sharp_seams',False) and edge.use_edge_sharp:reason='sharp'
        if reason:seams.add(edge.index);evidence[reason]+=1
    if any(i<0 or i>=len(me.edges) for i in seams):raise ValueError('UV_SEAM_EDGE_INVALID')
    if not seams:raise ValueError('UV_SEAM_PLAN_REQUIRED: unwrap of a closed unsplit surface is unsupported; provide seams')
    return sorted(seams),evidence

def m5_plan(high,low,options,relation):
    operation=options['uv_operation']
    reasons=[]
    if options.get('uv_immutable',False) or relation.get('metadata',{}).get('uv_immutable',False):reasons.append('LOCKED_UV_IMMUTABLE')
    if low.mode!='OBJECT':reasons.append('MODE_NOT_SUPPORTED')
    if low.data.library or low.library or low.override_library:reasons.append('LINKED_MESH_UNSUPPORTED')
    if not low.get('production_candidate'):reasons.append('NOT_A_CANDIDATE')
    if operation=='generate':
        seams,evidence=uv_seam_plan(low,options)
        proposal={'seam_edges':seams,'seam_evidence':evidence,'unwrap_method':options.get('unwrap_method','ANGLE_BASED'),
            'strategy':'explicit_'+options.get('unwrap_method','ANGLE_BASED').lower()+'_then_deterministic_shelf',
            'attribute_consequences':['UV layer replaced only on the new Candidate; source UVs untouched.','Seam flags rebuilt only on the new Candidate.']}
    else:
        layer,values=uv_data(low,options.get('uv_layer'))
        islands,assignment=uv_assignment(low,values,options) if operation=='udim' else (uv_islands(low,values)[0],{})
        proposal={'strategy':'deterministic_shelf','islands':len(islands),'tile_assignment':[{ 'island':k,'tile':v} for k,v in assignment.items()],
            'attribute_consequences':['UV coordinates changed only on the new Candidate; geometry/weights/morphs/materials preserved.']}
    proposal.update(supported=not reasons,reasons=reasons,apply=False,candidate_role='GAME_MESH')
    return proposal

def m5_apply(high,low,candidate,proposal,options):
    if options['uv_operation']=='generate':return uv_generate_mesh(candidate,options,proposal)
    return uv_pack_mesh(candidate,options,options['uv_operation']=='udim')

def m5_verify(high,low,candidate,proposal,options):
    settings=dict(options)
    if options['uv_operation']=='udim':settings['expected_tiles']=sorted(set(row['tile'] for row in options['assignments']))
    report=uv_metrics(candidate,settings)
    before=m5_signature(low);after=m5_signature(candidate)
    # Mutations must preserve the base geometry and all non-UV dependencies; parent also checks HIGH.
    errors=list(report['errors'])
    for key in ('topology','geometry','weights','morphs','materials','transform','modifiers','rigs','normals'):
        if before['sections'][key]!=after['sections'][key]:errors.append('UV_UNRELATED_ATTRIBUTE_CHANGED:'+key)
    return {'passed':not errors,'metrics':report['metrics'],'uv_report':report,'errors':errors}
'''
