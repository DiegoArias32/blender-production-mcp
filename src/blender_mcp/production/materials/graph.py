"""Deterministic Principled builder on owned candidates with installed RNA checks."""
from __future__ import annotations

from .candidate import RUNTIME as CANDIDATE_RUNTIME
from .model import normalize_definition, SOCKETS
from . import integrity


RUNTIME = CANDIDATE_RUNTIME + r'''
def m9_socket_required(node,name,output=False):
    socket=(node.outputs if output else node.inputs).get(name)
    if socket is None:
        m9_fail('MATERIAL_SOCKET_UNSUPPORTED',node.bl_idname+'.'+name+' not present in installed Blender')
    return socket

def m9_enum(item,name,value):
    prop=item.bl_rna.properties.get(name)
    if not prop or prop.type!='ENUM' or value not in {e.identifier for e in prop.enum_items}:
        m9_fail('MATERIAL_API_UNSUPPORTED',name+'='+str(value))
    return value

def m9_build_shader(params):
    before=m9_mutation_guard(params)
    obj=bpy.data.objects.get(params.get('candidate_object',''))
    material=bpy.data.materials.get(params.get('candidate_material',''))
    generation=params.get('generation_id');m9_owned(obj,material,generation)
    candidate_params={'object_name':obj.name,'material_name':material.name,'options':params.get('options',{})}
    candidate_before=m9_status(candidate_params)
    expected=params.get('expected_candidate')
    if not isinstance(expected,dict) or expected.get('runtime_fingerprint')!=candidate_before['runtime_fingerprint']:
        m9_fail('MATERIAL_STALE_CANDIDATE','candidate or its dependencies changed')
    definition=params['definition'];values=definition['parameters'];channels=definition['channels']
    if material.get('production_m9_definition'):
        if material['production_m9_definition']==json.dumps(definition,sort_keys=True):
            return {'status':'BUILT','candidate_object':obj.name,'candidate_material':material.name,
                'idempotent':True,'source_preserved':True,'candidate_status':candidate_before}
        m9_fail('MATERIAL_REFINEMENT_REQUIRES_NEW_CANDIDATE','previous graph retained; create a new candidate version')
    if material.animation_data or (material.node_tree and material.node_tree.animation_data):
        m9_fail('MATERIAL_ANIMATED_SHADER_UNSUPPORTED','candidate material has animation')
    if channels and not obj.data.uv_layers:
        m9_fail('MATERIAL_UV_REQUIRED','image channels require an explicit active UV layer')
    if 'normal' in channels and definition['normal_convention']!='OPENGL':
        m9_fail('MATERIAL_NORMAL_CONVENTION_UNSUPPORTED','explicit OPENGL required; no guessed green inversion')
    if definition['height_mode']=='DISPLACEMENT' and definition['renderer']!='CYCLES':
        m9_fail('MATERIAL_DISPLACEMENT_UNSUPPORTED','true displacement is qualified only for Cycles')
    if values.get('transmission_weight',0)>0 and definition['renderer']=='EEVEE' and not material.bl_rna.properties.get('use_raytrace_refraction'):
        m9_fail('MATERIAL_TRANSMISSION_UNSUPPORTED','la API instalada no expone refracción EEVEE en material')
    images={}
    for channel,binding in channels.items():
        image=bpy.data.images.get(binding['image'])
        if image is None or not image.has_data or not len(image.pixels):
            m9_fail('MATERIAL_IMAGE_UNAVAILABLE',binding['image'])
        if image.source=='TILED' and image.is_dirty:
            m9_fail('MATERIAL_TILED_DIRTY_UNSUPPORTED','dirty tiled image')
        m9_enum(image.colorspace_settings,'name',binding['colorspace'])
        images[channel]=image
    # Validate socket/property availability on staging nodes before touching the candidate.
    staging=bpy.data.materials.new(material.name+'_BuildStaging')
    staging.use_nodes=True
    made_images=[]
    try:
        nodes=staging.node_tree.nodes;nodes.clear();links=staging.node_tree.links
        shader=nodes.new('ShaderNodeBsdfPrincipled');shader.name='M9_Principled'
        shader.subsurface_method=m9_enum(shader,'subsurface_method',values.get('subsurface_method','RANDOM_WALK_SKIN'))
        output=nodes.new('ShaderNodeOutputMaterial');output.name='M9_Output'
        links.new(m9_socket_required(shader,'BSDF',True),m9_socket_required(output,'Surface'))
        for key,socket_name in params['socket_names'].items():
            if key in values:
                m9_socket_required(shader,socket_name).default_value=values[key]
        uv=None
        if channels:
            uv=nodes.new('ShaderNodeUVMap');uv.name='M9_UV';uv.uv_map=obj.data.uv_layers.active.name
        semantics={};channel_outputs={};image_copies={}
        for channel,binding in channels.items():
            source_image=images[channel];copy_key=source_image.name+'|'+binding['colorspace']
            if copy_key not in image_copies:
                candidate_image=source_image.copy();candidate_image.name=material.name+'_'+channel+'_Image'
                made_images.append(candidate_image)
                m9_mark(candidate_image,generation,obj.get('production_candidate_version',1),source_image.name)
                candidate_image['production_m9_source_image']=source_image.name
                candidate_image.colorspace_settings.name=binding['colorspace']
                # Blender Image.copy() does not copy unsaved pixel edits. Restore
                # the complete bounded buffer on the new image, never its file.
                source_pixels=list(source_image.pixels)
                if source_image.source=='TILED':
                    candidate_image.tiles.active_index=source_image.tiles.active_index
                    if list(candidate_image.pixels)!=source_pixels:
                        m9_fail('MATERIAL_TILED_COPY_UNSUPPORTED','el buffer del tile activo no se conservó al duplicar')
                elif list(candidate_image.pixels)!=source_pixels:
                    if len(candidate_image.pixels)!=len(source_pixels):
                        m9_fail('MATERIAL_IMAGE_COPY_UNSUPPORTED','la copia cambió el tamaño del buffer')
                    candidate_image.pixels.foreach_set(source_pixels)
                    candidate_image.update()
                    if list(candidate_image.pixels)!=source_pixels:
                        m9_fail('MATERIAL_IMAGE_COPY_UNSUPPORTED','la copia no conservó todos los píxeles')
                image_copies[copy_key]=candidate_image
            candidate_image=image_copies[copy_key]
            node=nodes.new('ShaderNodeTexImage');node.name='M9_Texture_'+channel;node.label=channel
            node.image=candidate_image;node['production_channel']=channel
            node.interpolation='Linear';node.extension='REPEAT'
            links.new(m9_socket_required(uv,'UV',True),m9_socket_required(node,'Vector'))
            component=binding['component'];source_socket=m9_socket_required(node,'Alpha' if component=='A' else 'Color',True)
            if component in ('R','G','B'):
                split=nodes.new('ShaderNodeSeparateColor');split.name='M9_Component_'+channel;split.mode='RGB'
                links.new(source_socket,m9_socket_required(split,'Color'))
                source_socket=m9_socket_required(split,{'R':'Red','G':'Green','B':'Blue'}[component],True)
            elif component=='COLOR' and channel not in ('base_color','normal','emission'):
                luminance=nodes.new('ShaderNodeRGBToBW');luminance.name='M9_Luminance_'+channel
                links.new(source_socket,m9_socket_required(luminance,'Color'))
                source_socket=m9_socket_required(luminance,'Val',True)
            channel_outputs[channel]=source_socket
            semantics[channel]={'source_image':source_image.name,'candidate_image':candidate_image.name,
                'component':component,'colorspace':candidate_image.colorspace_settings.name,'node':node.name,
                'purpose':'INDEPENDENT_DATA_NOT_MULTIPLIED_INTO_BASE_COLOR' if channel=='ao' else channel}
        for channel,target in {'base_color':'Base Color','metallic':'Metallic','roughness':'Roughness','opacity':'Alpha','emission':'Emission Color'}.items():
            if channel in channel_outputs:links.new(channel_outputs[channel],m9_socket_required(shader,target))
        normal_socket=None
        if 'normal' in channel_outputs:
            normal=nodes.new('ShaderNodeNormalMap');normal.name='M9_Normal';normal.space='TANGENT'
            normal.uv_map=obj.data.uv_layers.active.name
            m9_socket_required(normal,'Strength').default_value=values['normal_strength']
            links.new(channel_outputs['normal'],m9_socket_required(normal,'Color'))
            normal_socket=m9_socket_required(normal,'Normal',True)
        if 'height' in channel_outputs and definition['height_mode']=='BUMP':
            bump=nodes.new('ShaderNodeBump');bump.name='M9_Bump'
            m9_socket_required(bump,'Strength').default_value=values['bump_strength']
            m9_socket_required(bump,'Distance').default_value=values['height_distance']
            links.new(channel_outputs['height'],m9_socket_required(bump,'Height'))
            if normal_socket:links.new(normal_socket,m9_socket_required(bump,'Normal'))
            normal_socket=m9_socket_required(bump,'Normal',True)
        if normal_socket:links.new(normal_socket,m9_socket_required(shader,'Normal'))
        if 'height' in channel_outputs and definition['height_mode']=='DISPLACEMENT':
            displacement=nodes.new('ShaderNodeDisplacement');displacement.name='M9_Displacement';displacement.space='OBJECT'
            m9_socket_required(displacement,'Scale').default_value=values['height_distance']
            m9_socket_required(displacement,'Midlevel').default_value=values['height_midlevel']
            links.new(channel_outputs['height'],m9_socket_required(displacement,'Height'))
            links.new(m9_socket_required(displacement,'Displacement',True),m9_socket_required(output,'Displacement'))
            m9_enum(material,'displacement_method','DISPLACEMENT')
        if 'opacity' in channels or values.get('alpha',1)<1:
            m9_enum(material,'surface_render_method','DITHERED')
        # Staging is complete and source/candidate still match. Copy only supported
        # nodes into the owned candidate; no source tree, group, image or UV is edited.
        if m9_status(params)['runtime_fingerprint']!=before['runtime_fingerprint']:
            m9_fail('MATERIAL_STALE_SOURCE','source changed before candidate graph commit')
        if m9_status(candidate_params)['runtime_fingerprint']!=candidate_before['runtime_fingerprint']:
            m9_fail('MATERIAL_STALE_CANDIDATE','candidate changed before graph commit')
        material.use_nodes=True;destination=material.node_tree;destination.nodes.clear();copied={}
        for node in nodes:
            copied_node=destination.nodes.new(node.bl_idname);copied_node.name=node.name;copied_node.label=node.label
            if node.type=='BSDF_PRINCIPLED':copied_node.subsurface_method=node.subsurface_method
            for input_socket in node.inputs:
                if hasattr(input_socket,'default_value') and not input_socket.is_unavailable:
                    m9_socket_required(copied_node,input_socket.name).default_value=input_socket.default_value
            if node.type=='TEX_IMAGE':
                copied_node.image=node.image;copied_node.interpolation=node.interpolation;copied_node.extension=node.extension
                copied_node['production_channel']=node.get('production_channel')
            if node.type=='UVMAP':copied_node.uv_map=node.uv_map
            if node.type=='NORMAL_MAP':copied_node.space=node.space;copied_node.uv_map=node.uv_map
            if node.bl_idname=='ShaderNodeSeparateColor':copied_node.mode=node.mode
            if node.type=='DISPLACEMENT':copied_node.space=node.space
            copied[node.name]=copied_node
        for link in links:
            destination.links.new(m9_socket_required(copied[link.from_node.name],link.from_socket.name,True),
                m9_socket_required(copied[link.to_node.name],link.to_socket.name))
        if definition['height_mode']=='DISPLACEMENT':material.displacement_method='DISPLACEMENT'
        else:material.displacement_method='BUMP'
        if 'opacity' in channels or values.get('alpha',1)<1:material.surface_render_method='DITHERED'
        if values.get('transmission_weight',0)>0 and definition['renderer']=='EEVEE':material.use_raytrace_refraction=True
        material['production_m9_definition']=json.dumps(definition,sort_keys=True)
        material['production_m9_channels']=json.dumps(semantics,sort_keys=True)
        material['production_normal_convention']=definition['normal_convention']
        material['production_height_mode']=definition['height_mode']
        material['production_renderer']=definition['renderer']
        material['production_category']=definition['category']
        after=m9_status(params)
        if after['runtime_fingerprint']!=before['runtime_fingerprint']:
            m9_fail('MATERIAL_SOURCE_CHANGED','source changed during graph build')
        result_status=m9_status(candidate_params)
        return {'status':'BUILT','candidate_object':obj.name,'candidate_material':material.name,
            'idempotent':False,'source_preserved':True,'channels':semantics,'candidate_status':result_status,
            'subsurface_method':shader.subsurface_method,
            'before_original_fingerprint':before['runtime_fingerprint'],'after_original_fingerprint':after['runtime_fingerprint'],
            'metrics':{'nodes':len(destination.nodes),'links':len(destination.links),'new_images':len(made_images)},
            'warnings':['Profiles and socket values are starting configurations, not physical composition or photorealism certification.',
                'Height distance and SSS scale use Blender units; inspect scene scale before interpreting real-world distance.']}
    except Exception:
        # Candidate remains inspectable on a failed commit; never retry with a new
        # operation ID blindly. Only unused staging images are removed here.
        raise
    finally:
        bpy.data.materials.remove(staging)
        for image in made_images:
            if image.users==0:bpy.data.images.remove(image)
'''

BUILD = RUNTIME + "\nresult=m9_build_shader(params)\n"


def build(bridge, params: dict) -> dict:
    """Revalidate structured input even when called outside the MCP service."""
    payload = dict(params)
    payload['definition'] = normalize_definition(params.get('definition'))
    payload['socket_names'] = dict(SOCKETS)
    current = integrity.status(bridge, params['object_name'], params['material_name'], params.get('options'))
    expected = params.get('expected_status', {})
    if not expected.get('fingerprint') or expected['fingerprint'] != current['fingerprint']:
        raise ValueError('MATERIAL_STALE_SOURCE: la fuente o un archivo dependiente cambió')
    current_candidate = integrity.status(bridge, params['candidate_object'], params['candidate_material'], params.get('options'))
    expected_candidate = params.get('expected_candidate', {})
    if not expected_candidate.get('fingerprint') or expected_candidate['fingerprint'] != current_candidate['fingerprint']:
        raise ValueError('MATERIAL_STALE_CANDIDATE: la candidata o un archivo dependiente cambió')
    return bridge.run(BUILD, payload)
