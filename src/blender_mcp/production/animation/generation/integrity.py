"""Bounded source-asset signatures for M6 deformation and visual evidence.

Raw mesh data is hashed inside Blender. Coordinates/weights never leave the
payload. Dynamic mesh/material dependencies require a separate adapter rather
than silently receiving a certificate for an incomplete context.
"""
from __future__ import annotations

from ...common.m3_transaction import BASE_RUNTIME


INTEGRITY_RUNTIME = r'''
def m6_integrity_value(value):
    if value is None or isinstance(value,(str,bool,int)):
        return value
    if isinstance(value,float):
        if not math.isfinite(value):
            raise ValueError('ASSET_CONTEXT_NONFINITE: source property is nonfinite')
        return value
    if isinstance(value,set):
        return sorted(value)
    try:
        values=list(value)
    except TypeError:
        raise ValueError('ASSET_CONTEXT_UNSUPPORTED: property value cannot be certified')
    if len(values)>32:
        raise ValueError('ASSET_CONTEXT_CAPACITY: scalar RNA array exceeds 32 elements')
    return [m6_integrity_value(v) for v in values]

def m6_integrity_rna(item,pointers=False):
    values={}
    for prop in item.bl_rna.properties:
        key=prop.identifier
        if key in ('rna_type','name','name_full') or prop.type=='COLLECTION' or prop.is_readonly:
            continue
        value=item.path_resolve(key)
        if prop.type=='POINTER':
            if not pointers:
                if value is not None:
                    raise ValueError('ASSET_CONTEXT_UNSUPPORTED: external dependency '+key)
                values[key]=None
            else:
                if value is not None and not hasattr(value,'name'):
                    raise ValueError('ASSET_CONTEXT_UNSUPPORTED: unnamed dependency '+key)
                values[key]=None if value is None else {'name':value.name,'type':value.bl_rna.identifier}
        elif prop.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
            values[key]=m6_integrity_value(value)
        else:
            raise ValueError('ASSET_CONTEXT_UNSUPPORTED: RNA property '+key)
    return values

def m6_integrity_static(item,label):
    ad=getattr(item,'animation_data',None)
    if ad and (ad.action or ad.drivers or any(not t.mute and len(t.strips) for t in ad.nla_tracks)):
        raise ValueError('ASSET_CONTEXT_UNSUPPORTED: animated or driven '+label)

def m6_integrity_material(material):
    if material is None:
        return None
    if material.library or material.override_library:
        raise ValueError('ASSET_CONTEXT_UNSUPPORTED: linked material')
    m6_integrity_static(material,'material')
    # node_tree is the one explicitly traversed pointer, not an omitted source.
    scalars={}
    for prop in material.bl_rna.properties:
        if prop.identifier in ('rna_type','node_tree','name','name_full') or prop.is_readonly or prop.type=='COLLECTION':
            continue
        value=material.path_resolve(prop.identifier)
        if prop.type=='POINTER':
            if value is not None:
                raise ValueError('ASSET_CONTEXT_UNSUPPORTED: material dependency '+prop.identifier)
            scalars[prop.identifier]=None
        elif prop.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
            scalars[prop.identifier]=m6_integrity_value(value)
    graph=None
    if material.use_nodes and material.node_tree:
        tree=material.node_tree;m6_integrity_static(tree,'material node tree')
        if len(tree.nodes)>128 or len(tree.links)>512:
            raise ValueError('ASSET_CONTEXT_CAPACITY: material graph exceeds 128 nodes/512 links')
        nodes=[];sockets=0
        supported_nodes={'BSDF_PRINCIPLED','OUTPUT_MATERIAL','RGB','VALUE','MATH','VECT_MATH','MIX_RGB','MIX',
                         'BSDF_DIFFUSE','BSDF_GLOSSY','BSDF_TRANSPARENT','BSDF_TRANSLUCENT','BSDF_REFRACTION',
                         'EMISSION','ADD_SHADER','MIX_SHADER','NORMAL','FRESNEL','LAYER_WEIGHT','UVMAP',
                         'NORMAL_MAP','BUMP','SEPXYZ','COMBXYZ','SEPRGB','COMBRGB','SEP_COLOR','COMB_COLOR',
                         'VECT_TRANSFORM','VECTOR_ROTATE','CLAMP','INVERT','GAMMA','HUE_SAT'}
        for node in tree.nodes:
            if node.type not in supported_nodes:
                raise ValueError('ASSET_CONTEXT_UNSUPPORTED: material node '+node.type+' needs an adapter')
            inputs=[];outputs=[]
            for collection,rows in ((node.inputs,inputs),(node.outputs,outputs)):
                sockets+=len(collection)
                if sockets>1024:
                    raise ValueError('ASSET_CONTEXT_CAPACITY: material socket budget exceeded')
                for socket in collection:
                    row={'name':socket.name,'identifier':socket.identifier,'type':socket.bl_idname}
                    if hasattr(socket,'default_value'):
                        row['default']=m6_integrity_value(socket.default_value)
                    rows.append(row)
            nodes.append({'name':node.name,'type':node.bl_idname,'properties':m6_integrity_rna(node),
                          'inputs':inputs,'outputs':outputs})
        links=[{'from_node':link.from_node.name,'from_socket':link.from_socket.identifier,
                'to_node':link.to_node.name,'to_socket':link.to_socket.identifier,
                'muted':getattr(link,'is_muted',False)} for link in tree.links]
        graph={'nodes':nodes,'links':links}
    return {'name':material.name,'properties':scalars,'graph':graph}

def m6_integrity_mesh(mesh,target):
    if mesh.library or mesh.override_library or mesh.data.library or mesh.data.override_library:
        raise ValueError('ASSET_CONTEXT_UNSUPPORTED: linked/override mesh')
    m6_integrity_static(mesh,'mesh object');m6_integrity_static(mesh.data,'mesh datablock')
    if mesh.constraints or (mesh.parent and mesh.parent!=target):
        raise ValueError('ASSET_CONTEXT_UNSUPPORTED: constrained or externally parented mesh')
    data=mesh.data
    if len(data.polygons)>250000 or len(data.loops)>500000:
        raise ValueError('ASSET_CONTEXT_CAPACITY: topology budget exceeded')
    coordinates=[[float(v) for v in vertex.co] for vertex in data.vertices]
    if any(not math.isfinite(v) for row in coordinates for v in row):
        raise ValueError('ASSET_CONTEXT_NONFINITE: vertex coordinate')
    weights=[];weight_count=0
    for vertex in data.vertices:
        row=sorted([[entry.group,float(entry.weight)] for entry in vertex.groups])
        weight_count+=len(row)
        if weight_count>1000000 or any(not math.isfinite(v[1]) for v in row):
            raise ValueError('ASSET_CONTEXT_CAPACITY: invalid/excessive weight entries')
        weights.append(row)
    polygons=[{'vertices':list(p.vertices),'material':p.material_index,'smooth':p.use_smooth} for p in data.polygons]
    edges=[list(edge.vertices) for edge in data.edges]
    attributes=[];attribute_entries=0
    if len(data.attributes)>32:
        raise ValueError('ASSET_CONTEXT_CAPACITY: maximum 32 mesh attributes')
    for attribute in data.attributes:
        attribute_entries+=len(attribute.data)
        if attribute_entries>2000000:
            raise ValueError('ASSET_CONTEXT_CAPACITY: maximum two million attribute entries')
        # Read each installed RNA schema rather than inventing a field per type.
        rows=[]
        for element in attribute.data:
            row={}
            for prop in element.bl_rna.properties:
                if prop.identifier=='rna_type':
                    continue
                if prop.type not in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
                    raise ValueError('ASSET_CONTEXT_UNSUPPORTED: mesh attribute RNA '+prop.identifier)
                row[prop.identifier]=m6_integrity_value(element.path_resolve(prop.identifier))
            rows.append(row)
        attributes.append({'name':attribute.name,'domain':attribute.domain,'type':attribute.data_type,'fingerprint':m2_digest(rows)})
    shape_keys=None
    if data.shape_keys:
        keys=data.shape_keys;m6_integrity_static(keys,'shape keys')
        if len(keys.key_blocks)>64 or sum(len(k.data) for k in keys.key_blocks)>2000000:
            raise ValueError('ASSET_CONTEXT_CAPACITY: shape-key budget exceeded')
        shape_keys={'relative':keys.use_relative,'evaluation_time':keys.eval_time,'blocks':[]}
        for key in keys.key_blocks:
            points=[[float(v) for v in point.co] for point in key.data]
            if any(not math.isfinite(v) for row in points for v in row):
                raise ValueError('ASSET_CONTEXT_NONFINITE: shape-key coordinate')
            shape_keys['blocks'].append({'name':key.name,'relative':key.relative_key.name if key.relative_key else None,
                'value':key.value,'mute':key.mute,'vertex_group':key.vertex_group,'interpolation':key.interpolation,
                'slider_min':key.slider_min,'slider_max':key.slider_max,'fingerprint':m2_digest(points)})
    modifiers=[]
    supported={'ARMATURE','SUBSURF','SOLIDIFY','BEVEL','MIRROR','WELD','TRIANGULATE','WEIGHTED_NORMAL',
               'DECIMATE','SMOOTH','CORRECTIVE_SMOOTH','LAPLACIANSMOOTH','EDGE_SPLIT'}
    if len(mesh.modifiers)>32 or len(mesh.material_slots)>64:
        raise ValueError('ASSET_CONTEXT_CAPACITY: modifier/material-slot budget exceeded')
    for modifier in mesh.modifiers:
        if modifier.type not in supported:
            raise ValueError('ASSET_CONTEXT_UNSUPPORTED: modifier '+modifier.type)
        if modifier.type=='ARMATURE' and modifier.object!=target:
            raise ValueError('ASSET_CONTEXT_UNSUPPORTED: other Armature binding')
        if modifier.type=='BEVEL' and any(prop.identifier=='profile_type' for prop in modifier.bl_rna.properties) and modifier.path_resolve('profile_type')=='CUSTOM':
            raise ValueError('ASSET_CONTEXT_UNSUPPORTED: custom bevel profile needs an adapter')
        for prop in modifier.bl_rna.properties:
            if prop.type=='POINTER' and not prop.is_readonly and prop.identifier!='rna_type':
                value=modifier.path_resolve(prop.identifier)
                if value is not None and value!=target:
                    raise ValueError('ASSET_CONTEXT_UNSUPPORTED: modifier external dependency '+prop.identifier)
        modifiers.append({'name':modifier.name,'type':modifier.type,'properties':m6_integrity_rna(modifier,True)})
    return {'name':mesh.name,'data':data.name,'matrix_basis':[list(row) for row in mesh.matrix_basis],
        'matrix_parent_inverse':[list(row) for row in mesh.matrix_parent_inverse],
        'parent':mesh.parent.name if mesh.parent else None,'parent_type':mesh.parent_type,'parent_bone':mesh.parent_bone,
        'hide_render':mesh.hide_render,'hide_viewport':mesh.hide_viewport,'hidden':mesh.hide_get(),
        'color':list(mesh.color),'pass_index':mesh.pass_index,
        'vertices':len(data.vertices),'geometry':m2_digest({'co':coordinates,'edges':edges,'polygons':polygons}),
        'groups':[{'name':group.name,'index':group.index,'locked':group.lock_weight} for group in mesh.vertex_groups],
        'weights':m2_digest(weights),'attributes':attributes,'shape_keys':shape_keys,'modifiers':modifiers,
        'materials':[{'link':slot.link,'material':m6_integrity_material(slot.material)} for slot in mesh.material_slots]}

def m6_integrity_mesh_name(mesh):
    return mesh.name

def m6_asset_context(obj,options):
    names=options.get('meshes',[])
    if not isinstance(names,list) or len(names)>16 or any(not isinstance(n,str) or not n for n in names) or len(set(names))!=len(names):
        raise ValueError('ASSET_CONTEXT_SELECTION_INVALID: select 0..16 unique mesh names')
    linked=[mesh for mesh in bpy.context.scene.objects if mesh.type=='MESH' and
            (mesh.parent==obj or any(mod.type=='ARMATURE' and mod.object==obj for mod in mesh.modifiers))]
    explicit=[bpy.context.scene.objects.get(name) for name in names]
    if any(mesh is None or mesh.type!='MESH' for mesh in explicit):
        raise ValueError('ASSET_CONTEXT_SELECTION_INVALID: explicit mesh missing or wrong type')
    if any(mesh not in linked for mesh in explicit):
        raise ValueError('ASSET_CONTEXT_SELECTION_INVALID: explicit mesh must be bound to target')
    # Always include all character meshes, even when QA selects a subset.
    meshes=sorted(linked,key=m6_integrity_mesh_name)
    vertices=sum(len(mesh.data.vertices) for mesh in meshes)
    if len(meshes)>16 or vertices>100000:
        raise ValueError('ASSET_CONTEXT_CAPACITY: maximum 16 character meshes/100000 total vertices')
    rows=[m6_integrity_mesh(mesh,obj) for mesh in meshes]
    # Bone envelope/B-Bone settings are deformation dependencies even if rest matrices agree.
    binding=[]
    for bone in obj.data.bones:
        properties={}
        for prop in bone.bl_rna.properties:
            if prop.is_readonly or prop.type not in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
                continue
            properties[prop.identifier]=m6_integrity_value(bone.path_resolve(prop.identifier))
        binding.append({'name':bone.name,'parent':bone.parent.name if bone.parent else None,'properties':properties})
    payload={'meshes':rows,'bone_binding':binding,'selection':names}
    return {'fingerprint':m2_digest(payload),'meshes':[mesh.name for mesh in meshes],'vertices':vertices,
            'scope':'Raw character geometry, topology, attributes, weights, binding, modifiers, static shape keys and supported static materials',
            'external_dependencies':'unsupported; image textures, node groups and dynamic mesh/material dependencies require an adapter'}

def m6_asset_fingerprint(obj,options):
    return m6_asset_context(obj,options)['fingerprint']
'''


def capture(bridge, object_name, options=None):
    """Return only the compact source-asset signature and certified scope."""
    return bridge.run(BASE_RUNTIME + INTEGRITY_RUNTIME + "\nobj=m2_object(params['object_name'])\nresult=m6_asset_context(obj,params['options'])\n",
                      {'object_name': object_name, 'options': options or {}})
