"""Bounded shader graph inspection. Full bounded records are internal; public output is paginated."""
from __future__ import annotations

from copy import deepcopy
from . import textures, uv

RUNTIME='import bpy\nimport math\nimport json\n'+textures.RUNTIME+uv.RUNTIME+r'''
def mat9_value(value):
    if isinstance(value,(str,bool,int)) or value is None:return value
    if isinstance(value,float):return value if math.isfinite(value) else {'nonfinite':str(value)}
    values=list(value)
    if len(values)>32:raise ValueError('MATERIAL_SOCKET_CAPACITY: valor RNA demasiado grande')
    return [mat9_value(v) for v in values]

def mat9_animation(owner):
    ad=owner.animation_data if owner else None
    if not ad:return {'action':None,'drivers':0,'nla_tracks':0}
    return {'action':ad.action.name if ad.action else None,'drivers':len(ad.drivers),'nla_tracks':len(ad.nla_tracks)}

def mat9_socket(socket,index):
    row={'name':socket.name,'identifier':socket.identifier,'index':index,'type':socket.type,'linked':socket.is_linked}
    if hasattr(socket,'default_value'):row['value']=mat9_value(socket.default_value)
    return row

def mat9_graph(tree,prefix='',ancestors=None,budget=None):
    ancestors=ancestors or [];budget=budget or {'nodes':0,'links':0,'sockets':0}
    if tree.name in ancestors:raise ValueError('MATERIAL_GROUP_CYCLE: grupo recursivo '+tree.name)
    if len(ancestors)>8:raise ValueError('MATERIAL_GRAPH_CAPACITY: máximo 8 niveles de grupos')
    result={'nodes':[],'links':[],'groups':[],'image_names':[]}
    for node in tree.nodes:
        budget['nodes']+=1;budget['sockets']+=len(node.inputs)+len(node.outputs)
        if budget['nodes']>256 or budget['sockets']>4096:raise ValueError('MATERIAL_GRAPH_CAPACITY: 256 nodos / 4096 sockets; para inspeccionar un grafo mayor usa material_inspect con options.graph_mode="paged"')
        row={'id':prefix+node.name,'name':node.name,'type':node.type,'bl_idname':node.bl_idname,'tree':tree.name,
            'group_path':prefix,'mute':node.mute,'inputs':[mat9_socket(s,i) for i,s in enumerate(node.inputs)],
            'outputs':[mat9_socket(s,i) for i,s in enumerate(node.outputs)]}
        if node.type=='OUTPUT_MATERIAL':row.update(active_output=node.is_active_output,target=node.target)
        if node.type=='BSDF_PRINCIPLED':row['subsurface_method']=node.subsurface_method
        if node.type=='TEX_IMAGE':
            row.update(image=node.image.name if node.image else None,interpolation=node.interpolation,extension=node.extension,projection=node.projection)
            if node.image and node.image.name not in result['image_names']:result['image_names'].append(node.image.name)
        if node.type=='NORMAL_MAP':row.update(space=node.space,uv_map=node.uv_map)
        if node.type=='UVMAP':row['uv_map']=node.uv_map
        if node.type=='BUMP':row['invert']=node.invert
        if node.type=='DISPLACEMENT':row['space']=node.space
        result['nodes'].append(row)
        if node.type=='GROUP' and node.node_tree:
            nested=mat9_graph(node.node_tree,prefix+node.name+'/',ancestors+[tree.name],budget)
            result['groups'].append({'id':prefix+node.name,'tree':node.node_tree.name,'animation':mat9_animation(node.node_tree),
                'nodes':len(nested['nodes']),'links':len(nested['links'])})
            for key in ('nodes','links','groups'):result[key].extend(nested[key])
            for image in nested['image_names']:
                if image not in result['image_names']:result['image_names'].append(image)
    for link in tree.links:
        budget['links']+=1
        if budget['links']>1024:raise ValueError('MATERIAL_GRAPH_CAPACITY: máximo 1024 links')
        result['links'].append({'from_node':prefix+link.from_node.name,'from_socket':link.from_socket.identifier,
            'from_name':link.from_socket.name,'from_index':list(link.from_node.outputs).index(link.from_socket),
            'from_type':link.from_socket.type,'to_node':prefix+link.to_node.name,'to_socket':link.to_socket.identifier,
            'to_name':link.to_socket.name,'to_index':list(link.to_node.inputs).index(link.to_socket),
            'to_type':link.to_socket.type,'valid':link.is_valid,'muted':link.is_muted})
    return result

def mat9_graph_paged(tree,page,caps):
    # Large graphs (GlobalSkin exceeded 256 nodes / 4096 sockets) are walked completely for statistics,
    # but full rows are built only for the requested page and for a bounded set of key nodes, so the
    # payload stays bounded no matter how big the graph is. Validation is not performed in this mode.
    stats={'nodes':0,'links':0,'sockets':0,'groups':0,'max_depth':0,'by_type':{},'muted_nodes':0,
        'invalid_links':0,'muted_links':0,'unconnected_inputs':0}
    nodes=[];links=[];key=[];groups=[];images=[];seen=set()
    key_types={'OUTPUT_MATERIAL':8,'BSDF_PRINCIPLED':16,'TEX_IMAGE':64,'NORMAL_MAP':16,'UVMAP':16,'BUMP':16,'DISPLACEMENT':8}
    key_counts={}
    stack=[(tree,'',[])]
    while stack:
        current,prefix,ancestors=stack.pop(0)
        if current.name in ancestors:raise ValueError('MATERIAL_GROUP_CYCLE: grupo recursivo '+current.name)
        if len(ancestors)>caps['depth']:raise ValueError('MATERIAL_GRAPH_CAPACITY: más de '+str(caps['depth'])+' niveles de grupos')
        stats['max_depth']=max(stats['max_depth'],len(ancestors))
        for node in current.nodes:
            stats['nodes']+=1;stats['sockets']+=len(node.inputs)+len(node.outputs)
            if stats['nodes']>caps['nodes'] or stats['sockets']>caps['sockets']:
                raise ValueError('MATERIAL_GRAPH_CAPACITY: paged limit '+str(caps['nodes'])+' nodes / '+str(caps['sockets'])+' sockets')
            stats['by_type'][node.type]=stats['by_type'].get(node.type,0)+1
            stats['muted_nodes']+=1 if node.mute else 0
            stats['unconnected_inputs']+=sum(1 for s in node.inputs if not s.is_linked)
            index=stats['nodes']-1
            wanted=page['node_offset']<=index<page['node_offset']+page['node_limit']
            is_key=node.type in key_types and key_counts.get(node.type,0)<key_types[node.type]
            if wanted or is_key:
                row={'index':index,'id':prefix+node.name,'name':node.name,'type':node.type,'bl_idname':node.bl_idname,
                    'tree':current.name,'group_path':prefix,'mute':node.mute}
                # Socket rows dominate the payload; build them only when asked (and always for principled,
                # which the classifier reads).
                if (wanted and page['sockets']) or node.type=='BSDF_PRINCIPLED':
                    row.update(inputs=[mat9_socket(s,i) for i,s in enumerate(node.inputs)],
                        outputs=[mat9_socket(s,i) for i,s in enumerate(node.outputs)])
                if node.type=='OUTPUT_MATERIAL':row.update(active_output=node.is_active_output,target=node.target)
                if node.type=='BSDF_PRINCIPLED':row['subsurface_method']=node.subsurface_method
                if node.type=='TEX_IMAGE':row.update(image=node.image.name if node.image else None,interpolation=node.interpolation,extension=node.extension,projection=node.projection)
                if node.type=='NORMAL_MAP':row.update(space=node.space,uv_map=node.uv_map)
                if node.type=='UVMAP':row['uv_map']=node.uv_map
                if wanted:nodes.append(row)
                if is_key:
                    key_counts[node.type]=key_counts.get(node.type,0)+1
                    key.append(row)
            if node.type=='TEX_IMAGE' and node.image and node.image.name not in seen:
                seen.add(node.image.name)
                if len(images)<caps['images']:images.append(node.image.name)
            if node.type=='GROUP' and node.node_tree:
                stats['groups']+=1
                if len(groups)<caps['groups_listed']:
                    groups.append({'id':prefix+node.name,'tree':node.node_tree.name,'depth':len(ancestors)+1,'animation':mat9_animation(node.node_tree)})
                stack.append((node.node_tree,prefix+node.name+'/',ancestors+[current.name]))
        for link in current.links:
            stats['links']+=1
            if stats['links']>caps['links']:raise ValueError('MATERIAL_GRAPH_CAPACITY: paged limit '+str(caps['links'])+' links')
            stats['invalid_links']+=0 if link.is_valid else 1
            stats['muted_links']+=1 if link.is_muted else 0
            index=stats['links']-1
            if page['link_offset']<=index<page['link_offset']+page['link_limit']:
                links.append({'index':index,'from_node':prefix+link.from_node.name,'from_socket':link.from_socket.identifier,
                    'from_name':link.from_socket.name,'to_node':prefix+link.to_node.name,'to_socket':link.to_socket.identifier,
                    'to_name':link.to_socket.name,'valid':link.is_valid,'muted':link.is_muted})
    return {'stats':stats,'node_page':nodes,'link_page':links,'key_nodes':key,'group_list':groups,
        'image_names':images,'images_total':len(seen)}

def mat9_material_paged(material_name,object_name,options):
    obj=bpy.data.objects.get(object_name) if object_name else None
    if object_name and obj is None:raise ValueError('MATERIAL_OBJECT_MISSING: '+object_name)
    material=bpy.data.materials.get(material_name) if material_name else obj.active_material if obj else None
    if material is None:raise ValueError('MATERIAL_NOT_FOUND: selecciona material_name u objeto con material activo')
    if not material.use_nodes or not material.node_tree:
        graph={'stats':{'nodes':0,'links':0,'sockets':0,'groups':0,'max_depth':0,'by_type':{}},'node_page':[],'link_page':[],
            'key_nodes':[],'group_list':[],'image_names':[],'images_total':0}
    else:graph=mat9_graph_paged(material.node_tree,options['page'],options['caps'])
    images=[mat9_image(bpy.data.images[n]) for n in graph['image_names'][:100] if bpy.data.images.get(n)]
    return {'material_name':material.name,'object_name':obj.name if obj else None,'use_nodes':material.use_nodes,
        'linked':bool(material.library or material.override_library),'animation':mat9_animation(material),
        'node_animation':mat9_animation(material.node_tree),'renderer':bpy.context.scene.render.engine,
        'graph_mode':'paged',**graph,'images':images}

def mat9_material(material_name,object_name=None,options=None):
    options=options or {};obj=bpy.data.objects.get(object_name) if object_name else None
    if object_name and obj is None:raise ValueError('MATERIAL_OBJECT_MISSING: '+object_name)
    material=bpy.data.materials.get(material_name) if material_name else obj.active_material if obj else None
    if material is None:raise ValueError('MATERIAL_NOT_FOUND: selecciona material_name u objeto con material activo')
    graph=mat9_graph(material.node_tree) if material.use_nodes and material.node_tree else {'nodes':[],'links':[],'groups':[],'image_names':[]}
    names=sorted(set(graph.pop('image_names')+options.get('image_names',[])))
    if len(names)>100:raise ValueError('MATERIAL_IMAGE_CAPACITY: máximo 100 imágenes')
    images=[];missing=[]
    for name in names:
        image=bpy.data.images.get(name)
        if image:images.append(mat9_image(image))
        else:missing.append(name)
    assigned=[]
    for user in bpy.data.objects:
        ids=[i for i,s in enumerate(user.material_slots) if s.material==material]
        if ids:
            if len(assigned)>=128:raise ValueError('MATERIAL_ASSIGNMENT_CAPACITY: más de 128 objetos asignados')
            assigned.append({'object':user.name,'slots':ids,'polygons':sum(p.material_index in ids for p in user.data.polygons) if user.type=='MESH' else None})
    properties={}
    for name in ('surface_render_method','displacement_method','use_backface_culling','use_transparent_shadow','use_raytrace_refraction','thickness_mode','use_thickness_from_shadow'):
        if material.bl_rna.properties.get(name):properties[name]=mat9_value(material.path_resolve(name))
    result={'material_name':material.name,'object_name':obj.name if obj else None,'use_nodes':material.use_nodes,
        'owned':material.get('production_m9_owned') is True,'generation_id':material.get('production_generation_id'),
        'channel_bindings':json.loads(material.get('production_m9_channels','{}')),
        'linked':bool(material.library or material.override_library),'animation':mat9_animation(material),
        'node_animation':mat9_animation(material.node_tree),'properties':properties,'assigned_objects':assigned,
        'active_material':obj.active_material.name if obj and obj.active_material else None,
        'active_material_index':obj.active_material_index if obj else None,
        'slots':[{'index':i,'link':s.link,'material':s.material.name if s.material else None} for i,s in enumerate(obj.material_slots)] if obj else [],
        'images':images,'missing_image_names':missing,'renderer':bpy.context.scene.render.engine,
        'units':{'system':bpy.context.scene.unit_settings.system,'scale_length':bpy.context.scene.unit_settings.scale_length},
        'uv':mat9_uv(obj,options.get('uv',{})),**graph}
    result['counts']={'nodes':len(result['nodes']),'links':len(result['links']),'groups':len(result['groups']),
        'images':len(images),'assigned_objects':len(assigned)}
    return result
'''
INSPECT=RUNTIME+'\nresult=mat9_material(params.get("material_name"),params.get("object_name"),params.get("options"))\n'
INSPECT_PAGED=RUNTIME+'\nresult=mat9_material_paged(params.get("material_name"),params.get("object_name"),params["options"])\n'

# Paged mode walks the whole graph for statistics but materializes only one page of rows. The caps are
# hard limits on the walk itself (time and memory), not on the response, which stays bounded by the page.
PAGED_CAPS={'nodes':20000,'sockets':400000,'links':50000,'depth':16,'images':500,'groups_listed':200}
PAGED_TOOLS={'material_inspect','material_analyze_nodes'}


def paged_options(options):
    settings=dict(options or {})
    allowed={'graph_mode','limit','offset','link_limit','link_offset','detail'}
    if set(settings)-allowed:raise ValueError('MATERIAL_PAGED_OPTION_UNSUPPORTED: '+str(sorted(set(settings)-allowed)))
    page={}
    detail=settings.get('detail','summary')
    if detail not in {'summary','nodes','links'}:raise ValueError('MATERIAL_DETAIL_INVALID')
    # Rows with every socket and value are ~2 KB each; 50 of them keep a page near 100 KB.
    node_cap=50 if detail=='nodes' else 100
    for key,default,high in (('limit',min(50,node_cap),node_cap),('offset',0,None),('link_limit',50,200),('link_offset',0,None)):
        value=settings.get(key,default)
        if type(value) is not int or value<0 or (high is not None and not 1<=value<=high):
            raise ValueError('MATERIAL_PAGINATION_INVALID: '+key+(' (max '+str(high)+' with detail='+detail+')' if high else ''))
        page[key]=value
    return {'page':{'node_offset':page['offset'],'node_limit':page['limit'],'link_offset':page['link_offset'],
        'link_limit':page['link_limit'],'sockets':detail=='nodes'},'caps':dict(PAGED_CAPS),'detail':detail}


def inspect_paged(bridge,object_name=None,material_name=None,options=None):
    settings=paged_options(options)
    raw=bridge.run(INSPECT_PAGED,{'object_name':object_name,'material_name':material_name,'options':settings})
    raw['images']=textures.dependencies(raw.get('images',[]))
    return raw,settings


def paged_report(raw,settings):
    stats=raw['stats'];page=settings['page']
    result={k:raw.get(k) for k in ('material_name','object_name','use_nodes','linked','animation','node_animation','renderer')}
    result.update(graph_mode='paged',analysis_scope='PAGED_INSPECTION',stats=stats,
        validation='NOT_PERFORMED: paged mode inspects large graphs; graph/texture/normal validation and candidate '
                   'construction need the full mode, which stays limited to 256 nodes / 4096 sockets.',
        nodes=raw['node_page'] if settings['detail'] in {'summary','nodes'} else [],
        links=raw['link_page'] if settings['detail'] in {'summary','links'} else [],
        key_nodes=[{k:n.get(k) for k in ('index','id','type','image','space','uv_map','active_output','target','subsurface_method')}
                   for n in raw['key_nodes']],
        groups=raw['group_list'],images=[{k:i.get(k) for k in ('name','source','size','colorspace','tiles','missing_files')}
                                          for i in raw.get('images',[])],
        images_total=raw['images_total'],images_listed=len(raw['image_names']),
        pagination={'node_offset':page['node_offset'],'node_limit':page['node_limit'],'nodes_total':stats['nodes'],
                    'nodes_has_more':page['node_offset']+page['node_limit']<stats['nodes'],
                    'link_offset':page['link_offset'],'link_limit':page['link_limit'],'links_total':stats['links'],
                    'links_has_more':page['link_offset']+page['link_limit']<stats['links']},
        caps=settings['caps'],passed=None,status='success')
    principled=[n for n in raw['key_nodes'] if n.get('type')=='BSDF_PRINCIPLED']
    result['classification']=classify({'nodes':principled,'images':raw.get('images',[]),'assigned_objects':[]})
    if raw['images_total']>len(raw['image_names']):
        result['status']='warning';result['warnings']=['MATERIAL_IMAGES_TRUNCATED: '+str(raw['images_total'])+' images, '+str(len(raw['image_names']))+' listed']
    return result


def inspect(bridge,object_name=None,material_name=None,options=None):
    settings=dict(options or {})
    allowed={'image_names','uv','limit','offset','detail','manifest','normal_convention','renderer','definition','normal_length_tolerance'}
    if settings.get('graph_mode','full')!='full':
        raise ValueError('MATERIAL_GRAPH_MODE_UNSUPPORTED: graph_mode='+str(settings.get('graph_mode'))+' is only for material_inspect/material_analyze_nodes')
    settings.pop('graph_mode',None)
    if set(settings)-allowed:raise ValueError('MATERIAL_INSPECT_OPTION_UNSUPPORTED: '+str(sorted(set(settings)-allowed)))
    names=settings.get('image_names',[])
    if not isinstance(names,list) or len(names)>100 or any(not isinstance(n,str) or not n for n in names):
        raise ValueError('MATERIAL_IMAGE_SELECTION_INVALID')
    checked={'image_names':names,'uv':uv.options(settings.get('uv'))}
    data=bridge.run(INSPECT,{'object_name':object_name,'material_name':material_name,'options':checked})
    data['images']=textures.dependencies(data.get('images',[]))
    return data


def classify(raw,manifest=None):
    from .model import CATEGORIES
    manifest=manifest or {};principled=[n for n in raw.get('nodes',[]) if n.get('type')=='BSDF_PRINCIPLED']
    values={s['name']:s.get('value') for n in principled for s in n.get('inputs',[]) if not s.get('linked')}
    evidence=[];candidates=[];category='CUSTOM';confidence=.25
    explicit=manifest.get('category')
    if explicit:
        if explicit not in CATEGORIES:raise ValueError('MATERIAL_CATEGORY_UNSUPPORTED: '+str(explicit))
        category=explicit;confidence=.95;evidence.append({'signal':'manifest','value':explicit,'provenance':'caller'})
    elif len(principled)==1:
        metallic=values.get('Metallic');sss=values.get('Subsurface Weight');transmission=values.get('Transmission Weight')
        if isinstance(metallic,(int,float)) and metallic>.8:
            category='METAL';confidence=.75;evidence.append({'signal':'metallic_parameter','value':metallic})
        elif isinstance(sss,(int,float)) and sss>0:
            category='GENERIC_DIELECTRIC';confidence=.6;candidates=['SKIN','ORGANIC_CREATURE_TISSUE','BONE','SNOW']
            evidence.append({'signal':'subsurface_scattering','value':sss})
        elif isinstance(transmission,(int,float)) and transmission>0:
            category='GENERIC_DIELECTRIC';confidence=.6;candidates=['ICE','PLASTIC','CUSTOM']
            evidence.append({'signal':'transmission','value':transmission})
        else:
            category='GENERIC_DIELECTRIC';confidence=.65;evidence.append({'signal':'single_principled','value':True})
    return {'category':category,'confidence':confidence,'evidence':evidence,'alternatives':candidates,
        'geometry_relationships':raw.get('assigned_objects',[]),'image_count':len(raw.get('images',[])),
        'limitations':['La categoría es una propuesta de configuración, no composición física certificada.',
            'Sin manifiesto, el shader no distingue de forma fiable piel, tela, cuero, hielo o tejido orgánico.'],
        'name_only_classification':False}


def compact(raw,options=None):
    options=options or {};limit=options.get('limit',20);offset=options.get('offset',0);detail=options.get('detail','summary')
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:
        raise ValueError('MATERIAL_PAGINATION_INVALID: limit 1..100 y offset >=0')
    if detail not in {'summary','nodes','images','assignments'}:raise ValueError('MATERIAL_DETAIL_INVALID')
    result={k:deepcopy(raw.get(k)) for k in ('material_name','object_name','use_nodes','linked','animation','node_animation',
        'properties','active_material','active_material_index','counts','renderer','units','missing_image_names')}
    rows=raw.get('nodes',[]) if detail in {'summary','nodes'} else raw.get('images',[]) if detail=='images' else raw.get('assigned_objects',[])
    result.update(detail=detail,offset=offset,limit=limit,total=len(rows),has_more=offset+limit<len(rows))
    if detail=='summary':
        result['nodes']=[{k:n.get(k) for k in ('id','type','image','space','uv_map')} for n in rows[offset:offset+limit]]
        result['images']=[{k:i.get(k) for k in ('name','source','size','colorspace','tiles','missing_files')} for i in raw.get('images',[])[:limit]]
        result['slots']=raw.get('slots',[])[:limit]
    else:result[detail]=deepcopy([textures.public_image(r) for r in rows[offset:offset+limit]] if detail=='images' else rows[offset:offset+limit])
    result['classification']=classify(raw,options.get('manifest'))
    result['uv_summary']={k:raw.get('uv',{}).get(k) for k in ('active_layer','valid','available','errors','material_tiles')}
    return result


def report(name,raw,definition=None,options=None):
    from .qa import report as qa_report
    result=qa_report(name,raw,definition,options)
    if name.removeprefix('production.') in {'material_inspect','material_analyze_nodes'}:
        result['inspection']=compact(raw,options)
    return result
