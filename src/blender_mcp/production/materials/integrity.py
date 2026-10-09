"""Complete bounded M9 dependency signatures, separate from inspection summaries.

Blender runtime digests detect accidental changes, not adversarial authentication.
The host adds SHA-256 of external image/cache bytes without bypassing safe mode.
No image is reloaded and no frame or selection is changed while taking a snapshot.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..common.m3_transaction import BASE_RUNTIME


RUNTIME = BASE_RUNTIME + r'''
def m9_fail(code, detail):
    raise ValueError(code+': '+detail)

def m9_value(value, depth=0):
    if depth>12:
        m9_fail('MATERIAL_CAPACITY','nested custom/RNA value')
    if value is None or isinstance(value,(str,bool,int)):
        return value
    if isinstance(value,float):
        if not math.isfinite(value):
            m9_fail('MATERIAL_NONFINITE','RNA or pixel data')
        return value
    if isinstance(value,set):
        return sorted(value)
    if isinstance(value,dict) or hasattr(value,'items'):
        return {str(k):m9_value(v,depth+1) for k,v in value.items()}
    if isinstance(value,bpy.types.ID):
        m9_fail('MATERIAL_DEPENDENCY_UNSUPPORTED','custom property ID pointer')
    values=list(value)
    if len(values)>2000000:
        m9_fail('MATERIAL_CAPACITY','RNA array exceeds 2 million values')
    return [m9_value(v,depth+1) for v in values]

def m9_custom(item):
    # Ownership markers affect candidate identity but not inherited geometry.
    return {str(k):m9_value(v) for k,v in item.items()}

def m9_local(item):
    if item and (getattr(item,'library',None) or getattr(item,'override_library',None)):
        m9_fail('MATERIAL_LINKED_UNSUPPORTED',item.name)

def m9_deps():
    return {'images':{},'objects':{},'files':{},'groups':{},'nodes':0}

def m9_rna(item,deps,depth=0,omit=None):
    if depth>8:
        m9_fail('MATERIAL_DEPENDENCY_UNSUPPORTED','nested RNA exceeds depth 8')
    result={}
    omitted={'rna_type','name','name_full','users','tag','use_fake_user','use_extra_user',
        'select','show_expanded','show_options','show_preview','show_texture',
        'inputs','outputs','internal_links','node_tree','animation_data','interface',
        'is_valid','error_location','error_rotation','execution_time','info'}
    omitted.update(omit or [])
    for prop in item.bl_rna.properties:
        key=prop.identifier
        if key in omitted or key.startswith('bl_') or (prop.is_readonly and prop.type not in ('POINTER','COLLECTION')):
            continue
        if prop.is_readonly and key not in ('texture_mapping','color_mapping','color_ramp','mapping','image_user','settings','collision_settings','point_cache','effector_weights','elements','curves','points'):
            continue
        try:
            value=item.path_resolve(key)
        except (ValueError,AttributeError,TypeError):
            m9_fail('MATERIAL_DEPENDENCY_UNSUPPORTED','unreadable RNA '+key)
        if prop.type=='POINTER':
            if value is None or not value:
                result[key]=None
            elif isinstance(value,bpy.types.Image):
                result[key]={'image':value.name};m9_image(value,deps)
            elif isinstance(value,bpy.types.Object):
                m9_local(value);deps['objects'][value.name]=value
                result[key]={'object':value.name}
            elif isinstance(value,bpy.types.NodeTree):
                result[key]=m9_graph(value,deps,[])
            elif isinstance(value,bpy.types.ID):
                m9_fail('MATERIAL_DEPENDENCY_UNSUPPORTED','RNA pointer '+key+' '+value.bl_rna.identifier)
            elif depth<8:
                result[key]=m9_rna(value,deps,depth+1)
            else:
                m9_fail('MATERIAL_DEPENDENCY_UNSUPPORTED','pointer '+key)
        elif prop.type=='COLLECTION':
            if len(value)>1024:
                m9_fail('MATERIAL_CAPACITY','RNA collection '+key)
            result[key]=[m9_rna(v,deps,depth+1) for v in value]
        elif prop.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
            result[key]=m9_value(value)
        else:
            m9_fail('MATERIAL_DEPENDENCY_UNSUPPORTED','RNA property '+key)
    return result

def m9_animation(item,deps):
    ad=getattr(item,'animation_data',None)
    if not ad:
        return None
    if ad.drivers:
        # Inspection reports drivers; mutation refuses arbitrary dependency evaluation.
        m9_fail('MATERIAL_DRIVER_UNSUPPORTED','drivers on '+item.name)
    if ad.use_tweak_mode:
        m9_fail('MATERIAL_NLA_TWEAK_UNSUPPORTED',item.name)
    if len(ad.nla_tracks)>64 or sum(len(t.strips) for t in ad.nla_tracks)>128:
        m9_fail('MATERIAL_CAPACITY','animation stack exceeds 64 tracks/128 strips')
    def action_record(action):
        if action is None:
            return None
        m9_local(action)
        curves=action_curves(action,None)
        if sum(len(c.keyframe_points)+len(c.sampled_points) for s,l,t,c in curves)>100000:
            m9_fail('MATERIAL_CAPACITY','Action exceeds 100000 points')
        if any(c.modifiers for s,l,t,c in curves):
            m9_fail('MATERIAL_ANIMATION_UNSUPPORTED','F-Curve modifiers on '+action.name)
        return {'name':action.name,'raw':m2_snapshot(action),'custom':m9_custom(action),
            'slots':[{'handle':s.handle,'identifier':s.identifier,'type':s.target_id_type} for s in getattr(action,'slots',[])],
            'layers':[{'name':l.name,'properties':m9_rna(l,deps),
                'strips':[{'type':s.type,'properties':m9_rna(s,deps)} for s in l.strips]} for l in getattr(action,'layers',[])]}
    tracks=[]
    for track in ad.nla_tracks:
        strips=[]
        for strip in track.strips:
            if strip.type!='CLIP' or strip.strips or strip.fcurves:
                m9_fail('MATERIAL_ANIMATION_UNSUPPORTED','non-CLIP or animated NLA strip')
            strips.append({'name':strip.name,'properties':m9_rna(strip,deps,omit={'action'}),
                'action':action_record(strip.action),'slot':strip.action_slot.handle if strip.action_slot else None})
        tracks.append({'name':track.name,'mute':track.mute,'solo':track.is_solo,'lock':track.lock,'strips':strips})
    return {'action':action_record(ad.action),'slot':ad.action_slot.handle if ad.action_slot else None,
        'blend':ad.action_blend_type,'influence':ad.action_influence,'extrapolation':ad.action_extrapolation,
        'use_nla':ad.use_nla,'tracks':tracks}

def m9_image(image,deps):
    if image.name in deps['images']:
        return deps['images'][image.name]
    m9_local(image)
    if image.source not in ('FILE','GENERATED','TILED'):
        m9_fail('MATERIAL_IMAGE_SOURCE_UNSUPPORTED',image.source)
    if len(image.tiles)>64:
        m9_fail('MATERIAL_CAPACITY','more than 64 image tiles')
    # Pixel buffer is a separate dependency from disk. No reload or active-tile switch.
    if len(image.pixels)>2000000:
        m9_fail('MATERIAL_CAPACITY','cached image exceeds 2 million float samples')
    pixel_digest=m2_digest([m9_value(v) for v in image.pixels])
    paths=[]
    if image.source in ('FILE','TILED') and image.filepath:
        template=bpy.path.abspath(image.filepath,library=image.library)
        if image.source=='TILED':
            for tile in image.tiles:
                path=template.replace('<UDIM>',str(tile.number))
                if '<UVTILE>' in path:
                    offset=tile.number-1001;path=path.replace('<UVTILE>','u'+str(offset%10+1)+'_v'+str(offset//10+1))
                paths.append(path)
        else:
            paths=[template]
    for path in paths:
        deps['files'][path]={'path':path,'kind':'IMAGE','image':image.name}
    packed=[]
    for pack in image.packed_files:
        raw=pack.packed_file.data
        if len(raw)>16000000:
            m9_fail('MATERIAL_CAPACITY','packed image exceeds 16 MB')
        packed.append({'filepath':pack.filepath,'bytes':len(raw),'digest':m2_fast_digest(raw)})
    row={'name':image.name,'source':image.source,'filepath':image.filepath,'paths':paths,
        'size':list(image.size),'channels':image.channels,'depth':image.depth,'is_float':image.is_float,
        'alpha_mode':image.alpha_mode,'colorspace':image.colorspace_settings.name,
        'is_data':image.colorspace_settings.is_data,'is_dirty':image.is_dirty,'has_data':image.has_data,
        'tiles':[{'number':t.number,'size':list(t.size),'channels':t.channels} for t in image.tiles],
        'active_tile':image.tiles.active_index,
        'packed':packed,'cached_pixel_digest':pixel_digest,'cached_pixel_samples':len(image.pixels),
        'cache_coverage':'ACTIVE_TILE_ONLY' if image.source=='TILED' else 'COMPLETE_BUFFER',
        'reload_status':'UNKNOWN_NOT_RELOADED','custom':m9_custom(image)}
    # Tiled cached buffers outside the active tile are inaccessible without transient
    # mutation. File hashes cover every tile; dirty tiled images cannot be certified.
    if image.source=='TILED' and image.is_dirty:
        m9_fail('MATERIAL_TILED_DIRTY_UNSUPPORTED','unsaved cached tile edits cannot be fully signed')
    deps['images'][image.name]=row
    return row

def m9_socket(socket,deps):
    # Socket availability is derived asynchronously after material.copy(). Its
    # authored determinants (node method, graph, defaults) are signed instead.
    row={'identifier':socket.identifier,'name':socket.name,'type':socket.type}
    if hasattr(socket,'default_value'):
        value=socket.default_value
        if isinstance(value,bpy.types.ID):
            m9_fail('MATERIAL_DEPENDENCY_UNSUPPORTED','ID-valued shader socket')
        row['default']=m9_value(value)
    return row

def m9_graph(tree,deps,stack=None):
    if tree is None:
        return None
    m9_local(tree);stack=list(stack or [])
    if tree.name in stack or len(stack)>8:
        m9_fail('MATERIAL_GRAPH_RECURSION','recursive/deep group '+tree.name)
    stack.append(tree.name)
    if len(tree.nodes)>512 or len(tree.links)>2048:
        m9_fail('MATERIAL_CAPACITY','graph exceeds 512 nodes/2048 links')
    deps['nodes']+=len(tree.nodes)
    if deps['nodes']>2048:
        m9_fail('MATERIAL_CAPACITY','nested graph exceeds 2048 visited nodes')
    nodes=[]
    for node in tree.nodes:
        if node.bl_idname in ('ShaderNodeScript','ShaderNodeTexIES'):
            m9_fail('MATERIAL_SHADER_UNSUPPORTED',node.bl_idname)
        row={'name':node.name,'type':node.bl_idname,'mute':node.mute,
            'properties':m9_rna(node,deps,omit={'parent','image','node_tree','location','width','width_hidden','height','dimensions','color','use_custom_color','label'}),
            'inputs':[m9_socket(s,deps) for s in node.inputs],
            'outputs':[m9_socket(s,deps) for s in node.outputs],
            'internal_links':[[l.from_socket.identifier,l.to_socket.identifier] for l in node.internal_links],
            'parent':node.parent.name if node.parent else None,'custom':m9_custom(node)}
        if hasattr(node,'image'):
            row['image']=m9_image(node.image,deps) if node.image else None
        if node.type=='GROUP':
            row['group']=m9_graph(node.node_tree,deps,stack)
            if node.node_tree:deps['groups'][node.node_tree.name]=m2_digest(row['group'])
        nodes.append(row)
    interface=[]
    if hasattr(tree,'interface'):
        for item in tree.interface.items_tree:
            interface.append({'name':item.name,'item_type':item.item_type,
                'properties':m9_rna(item,deps,omit={'parent'})})
    return {'name':tree.name,'nodes':nodes,
        'links':[[l.from_node.name,l.from_socket.identifier,l.to_node.name,l.to_socket.identifier,l.is_muted] for l in tree.links],
        'interface':interface,'animation':m9_animation(tree,deps),'custom':m9_custom(tree)}

def m9_material(material,deps):
    if material is None:return None
    m9_local(material)
    return {'name':material.name,'properties':m9_rna(material,deps,omit={'asset_data','preview','grease_pencil'}),
        'graph':m9_graph(material.node_tree,deps) if material.use_nodes else None,
        'animation':m9_animation(material,deps),'custom':m9_custom(material),
        'cycles':m9_rna(material.cycles,deps) if hasattr(material,'cycles') else None}

def m9_mesh(obj,deps):
    mesh=obj.data;m9_local(mesh)
    if len(mesh.vertices)>100000 or len(mesh.loops)>500000:
        m9_fail('MATERIAL_CAPACITY','mesh exceeds 100000 vertices/500000 loops')
    weights=[[[g.group,g.weight] for g in v.groups] for v in mesh.vertices]
    if sum(len(w) for w in weights)>1000000:
        m9_fail('MATERIAL_CAPACITY','mesh exceeds one million weights')
    shapes=None
    if mesh.shape_keys:
        keys=mesh.shape_keys;m9_local(keys)
        if sum(len(k.data) for k in keys.key_blocks)>2000000:
            m9_fail('MATERIAL_CAPACITY','shape coordinates exceed two million points')
        shapes={'relative':keys.use_relative,'eval_time':keys.eval_time,'animation':m9_animation(keys,deps),
            'keys':[{'name':k.name,'value':k.value,'mute':k.mute,'relative':k.relative_key.name if k.relative_key else None,
                'min':k.slider_min,'max':k.slider_max,'group':k.vertex_group,'interpolation':k.interpolation,
                'coordinates':m2_digest([list(p.co) for p in k.data])} for k in keys.key_blocks]}
    attributes=[]
    for attr in mesh.attributes:
        if attr.name in ('position','.edge_verts','.corner_vert','.corner_edge','.select_vert','.select_edge','.select_poly'):continue
        rows=[]
        for item in attr.data:
            if hasattr(item,'value'):rows.append(m9_value(item.value))
            elif hasattr(item,'vector'):rows.append(m9_value(item.vector))
            elif hasattr(item,'color'):rows.append(m9_value(item.color))
            else:m9_fail('MATERIAL_ATTRIBUTE_UNSUPPORTED',attr.name)
        attributes.append({'name':attr.name,'type':attr.data_type,'domain':attr.domain,'values':m2_digest(rows)})
    return {'vertices':m2_digest([list(v.co) for v in mesh.vertices]),
        'split_normals':m2_digest([list(n.vector) for n in mesh.corner_normals]),'has_custom_normals':mesh.has_custom_normals,
        'edges':m2_digest([list(e.vertices) for e in mesh.edges]),
        'polygons':m2_digest([[list(p.vertices),p.material_index,p.use_smooth] for p in mesh.polygons]),
        'uv': [{'name':uv.name,'active_render':uv.active_render,'values':m2_digest([list(v.uv) for v in uv.data])} for uv in mesh.uv_layers],
        'active_uv':mesh.uv_layers.active_index,'attributes':attributes,
        'groups':[[g.name,g.index,g.lock_weight] for g in obj.vertex_groups],
        'weights':m2_digest(weights),'shape_keys':shapes,'animation':m9_animation(mesh,deps),
        'custom':{k:v for k,v in m9_custom(mesh).items() if k not in ('production_m9_owned','production_generation_id','production_candidate_version','production_source')},
        'counts':{'vertices':len(mesh.vertices),'polygons':len(mesh.polygons)}}

def m9_object(obj,deps):
    m9_local(obj)
    if obj.type not in ('MESH','ARMATURE','EMPTY'):
        m9_fail('MATERIAL_OBJECT_DEPENDENCY_UNSUPPORTED',obj.type+' '+obj.name)
    if obj.parent:deps['objects'][obj.parent.name]=obj.parent
    modifiers=[]
    for mod in obj.modifiers:
        if mod.type in ('NODES','CLOTH','SOFT_BODY','PARTICLE_SYSTEM','FLUID','DYNAMIC_PAINT'):
            m9_fail('MATERIAL_PHYSICS_DEPENDENCY_UNSUPPORTED','native runtime/cache not fully inspectable: '+mod.type)
        row={'name':mod.name,'type':mod.type,'rna':m9_rna(mod,deps)}
        if mod.type=='MESH_CACHE' and mod.filepath:
            path=bpy.path.abspath(mod.filepath);deps['files'][path]={'path':path,'kind':'MESH_CACHE'}
        modifiers.append(row)
    record={'name':obj.name,'type':obj.type,'parent':obj.parent.name if obj.parent else None,
        'parent_type':obj.parent_type,'parent_bone':obj.parent_bone,'matrix_parent_inverse':[list(r) for r in obj.matrix_parent_inverse],
        'channels':m3_channels(obj),'matrix_world':[list(r) for r in obj.matrix_world],
        'constraints':[m9_rna(c,deps) for c in obj.constraints],'modifiers':modifiers,
        'animation':m9_animation(obj,deps),'custom':m9_custom(obj)}
    if obj.type=='MESH':
        record['geometry']=m9_mesh(obj,deps)
        record['slots']=[{'link':s.link,'material':s.material.name if s.material else None} for s in obj.material_slots]
        record['active_material_index']=obj.active_material_index
    if obj.type=='ARMATURE':
        m9_local(obj.data)
        if len(obj.pose.bones)>2048:m9_fail('MATERIAL_CAPACITY','rig exceeds 2048 bones')
        record['rig']={'pose_position':obj.data.pose_position,'ik_solver':obj.pose.ik_solver,'animation':m9_animation(obj.data,deps),
            'bones':[{'name':p.name,'parent':p.parent.name if p.parent else None,
                'rest':[list(r) for r in p.bone.matrix_local],'bone':m9_rna(p.bone,deps,omit={'parent','children','collections'}),
                'pose_configuration':m9_rna(p,deps,omit={'bone','parent','child','constraints','matrix','matrix_basis','custom_shape','custom_shape_transform'}),
                'channels':m3_channels(p),'constraints':[m9_rna(c,deps) for c in p.constraints]} for p in obj.pose.bones]}
    return record

def m9_context():
    scene=bpy.context.scene
    return {'filepath':bpy.data.filepath,'scene':scene.name,'mode':bpy.context.mode,
        'frame':scene.frame_current,'subframe':scene.frame_subframe,
        'range':[scene.frame_start,scene.frame_end],'fps':scene.render.fps,'fps_base':scene.render.fps_base,
        'unit_system':scene.unit_settings.system,'scale_length':scene.unit_settings.scale_length,
        'engine':scene.render.engine,'view_transform':scene.view_settings.view_transform,
        'look':scene.view_settings.look,'exposure':scene.view_settings.exposure,'gamma':scene.view_settings.gamma,
        'stamps':sorted(s.get('production_asset') for s in bpy.data.scenes if isinstance(s.get('production_asset'),str))}

def m9_status(params):
    name=params.get('object_name') or params.get('source_object')
    obj=bpy.data.objects.get(name or '')
    if obj is None or obj.type!='MESH':m9_fail('MATERIAL_OBJECT_REQUIRED','existing mesh object required')
    material=bpy.data.materials.get(params.get('material_name',''))
    if material is None:m9_fail('MATERIAL_NOT_FOUND',str(params.get('material_name')))
    if not any(s.material==material for s in obj.material_slots):
        m9_fail('MATERIAL_NOT_ASSIGNED','material is not assigned to the selected object')
    deps=m9_deps();record=m9_material(material,deps);objrecord=m9_object(obj,deps)
    # Every other assigned source material participates in source preservation.
    other={s.material.name:m9_material(s.material,deps) for s in obj.material_slots if s.material and s.material!=material}
    names=params.get('options',{}).get('image_names',[])
    if not isinstance(names,list) or len(names)>64 or any(not isinstance(n,str) for n in names):
        m9_fail('MATERIAL_IMAGE_DEPENDENCIES_INVALID','image_names requires at most 64 image IDs')
    for image_name in sorted(set(names)):
        image=bpy.data.images.get(image_name)
        if image is None:m9_fail('MATERIAL_IMAGE_NOT_FOUND',image_name)
        m9_image(image,deps)
    related={};pending=dict(deps['objects'])
    while pending:
        next_name=sorted(pending)[0];item=pending.pop(next_name)
        if next_name==obj.name or next_name in related:continue
        if len(related)>=64:m9_fail('MATERIAL_CAPACITY','more than 64 dependency objects')
        related[next_name]=m9_object(item,deps)
        pending.update({k:v for k,v in deps['objects'].items() if k!=obj.name and k not in related})
    assignments=[]
    for owner in bpy.data.objects:
        if owner.get('production_m9_owned') and owner!=obj:continue
        slots=[{'index':i,'link':s.link} for i,s in enumerate(owner.material_slots) if s.material==material]
        if slots:assignments.append({'object':owner.name,'slots':slots})
    context=m9_context()
    raw={'material':record,'object':objrecord,'other_materials':other,'related':related,
        'images':deps['images'],'groups':deps['groups'],'assignments':assignments,'context':context}
    return {'object_name':obj.name,'material_name':material.name,'runtime_fingerprint':m2_digest(raw),
        'material_fingerprint':m2_digest(record),'geometry_fingerprint':m2_digest(objrecord['geometry']),
        'context':context,'asset_stamps':context['stamps'],
        'dependencies':{'images':list(deps['images'].values()),'files':list(deps['files'].values()),
            'objects':sorted(related),'node_groups':sorted(deps['groups'])},
        'metrics':{'nodes_visited':deps['nodes'],'images':len(deps['images']),'dependency_objects':len(related)},
        'warnings':['Tiled cache coverage is active tile only; all external tiles are content-hashed and dirty tiled buffers rejected.'] if any(i['source']=='TILED' for i in deps['images'].values()) else []}

def m9_mutation_guard(params,current=None):
    current=current or m9_status(params)
    expected=params.get('expected_status')
    if not isinstance(expected,dict) or not expected.get('runtime_fingerprint') or expected['runtime_fingerprint']!=current['runtime_fingerprint']:
        m9_fail('MATERIAL_STALE_SOURCE','source, dependency, cached pixels or context changed')
    if bpy.context.mode!='OBJECT':m9_fail('MATERIAL_OBJECT_MODE_REQUIRED','mutation requires Object mode')
    stamps=[]
    for scene in bpy.data.scenes:
        value=scene.get('production_asset')
        if isinstance(value,str):
            try:stamps.append(json.loads(value))
            except ValueError:m9_fail('MATERIAL_ASSET_STAMP_INVALID','invalid scene asset stamp')
    asset=params.get('expected_asset') or {}
    if any(s.get('role')=='MASTER' for s in stamps) or asset.get('role')=='MASTER':
        m9_fail('MASTER_ASSET_READ_ONLY','no se puede modificar un material del maestro')
    if bpy.data.filepath:
        # AssetRegistry is authoritative. FIXTURE registration intentionally has
        # no RNA stamp; WORKING_COPY requires its matching stamp. The service
        # supplies this frozen registry result after require_mutable_asset.
        if asset.get('role') not in ('FIXTURE','WORKING_COPY') or not isinstance(asset.get('asset_id'),str) or not asset['asset_id']:
            m9_fail('MATERIAL_ASSET_UNREGISTERED','el archivo requiere un registro mutable verificado por el servidor')
        actual_path=bpy.path.abspath(bpy.data.filepath).replace('\\','/').casefold()
        expected_path=bpy.path.abspath(asset.get('path') or '').replace('\\','/').casefold()
        if not asset.get('path') or actual_path!=expected_path:
            m9_fail('MATERIAL_ASSET_CHANGED','el archivo abierto no coincide con el registro del plan')
        if any(s.get('asset_id') and s['asset_id']!=asset['asset_id'] for s in stamps):
            m9_fail('MATERIAL_ASSET_STAMP_MISMATCH','el archivo conserva el sello de otro asset')
        if asset['role']=='WORKING_COPY' and not any(s.get('asset_id')==asset['asset_id'] and s.get('role')=='WORKING_COPY' for s in stamps):
            m9_fail('MATERIAL_ASSET_STAMP_MISMATCH','la copia de trabajo perdió su sello de origen')
    elif asset and asset.get('role')!='TEMPORARY':
        m9_fail('MATERIAL_ASSET_CHANGED','el plan registrado no corresponde a la sesión temporal abierta')
    return current
'''

STATUS = RUNTIME + "\nresult=m9_status(params)\n"


def file_dependencies(rows: list[dict]) -> list[dict]:
    """Hash complete external bytes under a finite aggregate budget; never rewrite."""
    result = []
    total = 0
    for row in rows:
        path = Path(row['path'])
        if str(path).startswith(('\\\\', '//')):
            raise ValueError('MATERIAL_REMOTE_DEPENDENCY_UNSUPPORTED: sólo archivos locales')
        item = dict(row)
        if not path.is_file():
            item.update(exists=False, sha256=None, bytes=None)
        else:
            before = path.stat()
            size = before.st_size
            total += size
            if size > 64 * 1024 * 1024 or total > 256 * 1024 * 1024:
                raise ValueError('MATERIAL_CAPACITY: external dependencies exceed 64 MiB/file or 256 MiB total')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (size, before.st_mtime_ns):
                raise ValueError('MATERIAL_DEPENDENCY_CHANGED: el archivo cambió durante su firma')
            item.update(exists=True, sha256=digest.hexdigest(), bytes=size)
        result.append(item)
    return result


def status(bridge, object_name: str, material_name: str, options: dict | None = None) -> dict:
    snapshot = bridge.run(STATUS, {'object_name': object_name, 'material_name': material_name, 'options': options or {}})
    files = file_dependencies(snapshot['dependencies']['files'])
    snapshot['dependencies']['files'] = files
    snapshot['fingerprint'] = hashlib.sha256(json.dumps(
        {'runtime': snapshot['runtime_fingerprint'], 'files': files},
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    snapshot['reload_status'] = 'UNKNOWN_NOT_RELOADED'
    return snapshot
