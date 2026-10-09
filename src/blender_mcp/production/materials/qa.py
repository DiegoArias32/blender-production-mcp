"""Explainable material checks. Unknown assumptions remain explicit; no quality score."""
from __future__ import annotations

import json
import math
from copy import deepcopy
from .model import CHANNELS, SOCKETS, COLOR_CHANNELS
from . import textures, uv
from .textures import issue

SUPPORTED_NODES=frozenset({'BSDF_PRINCIPLED','OUTPUT_MATERIAL','TEX_IMAGE','NORMAL_MAP','BUMP','DISPLACEMENT',
    'UVMAP','TEX_COORD','MAPPING','VALUE','RGB','SEP_COLOR','COMB_COLOR','SEPARATE_COLOR','COMBINE_COLOR','SEPRGB','COMBRGB','SEPXYZ','COMBXYZ',
    'MATH','VECT_MATH','MIX','MIX_RGB','RGBTOBW','CLAMP','INVERT','GAMMA','HUE_SAT','REROUTE','FRAME','GROUP','GROUP_INPUT','GROUP_OUTPUT'})


def resolve_definition(raw,definition):
    """Resolve source IDs only through the builder's owned, signed channel provenance."""
    if not definition:return definition
    result=deepcopy(definition);bindings=raw.get('channel_bindings',{})
    if not raw.get('owned') or not bindings:return result
    images={i['name']:i for i in raw.get('images',[])}
    for channel,declared in result.get('channels',{}).items():
        binding=bindings.get(channel)
        if not binding:continue
        image=images.get(binding.get('candidate_image'))
        if not image or not image.get('owned') or image.get('generation_id')!=raw.get('generation_id') or image.get('source_image')!=declared['image'] or binding.get('source_image')!=declared['image']:
            raise ValueError('MATERIAL_IMAGE_OWNERSHIP_MISMATCH: '+channel)
        if binding.get('component')!=declared.get('component') or binding.get('colorspace')!=declared.get('colorspace'):
            raise ValueError('MATERIAL_CHANNEL_PROVENANCE_MISMATCH: '+channel)
        declared['image']=image['name']
    return result


def _input(node,name):
    return next((s for s in node.get('inputs',[]) if s.get('name')==name),{})


def _incoming(raw,node,name):
    return [l for l in raw.get('links',[]) if l.get('to_node')==node['id'] and l.get('to_name',l.get('to_socket'))==name and not l.get('muted')]


def _origin(raw,node,name,visited=None):
    """Small certified tracing vocabulary; arbitrary graphs remain ambiguous."""
    visited=set(visited or ())
    if (node['id'],name) in visited:return None
    visited.add((node['id'],name));links=_incoming(raw,node,name)
    if len(links)!=1:return None
    link=links[0];source=next((n for n in raw.get('nodes',[]) if n['id']==link['from_node']),None)
    if source is None:return None
    if source['type']=='TEX_IMAGE':
        return {'image':source.get('image'),'component':'A' if link.get('from_name',link.get('from_socket'))=='Alpha' else 'COLOR','via':[source['type']]}
    if source['type'] in {'NORMAL_MAP','BUMP','DISPLACEMENT','SEP_COLOR','SEPARATE_COLOR','SEPRGB','REROUTE','RGBTOBW'}:
        socket={'NORMAL_MAP':'Color','BUMP':'Height','DISPLACEMENT':'Height','SEP_COLOR':'Color','SEPARATE_COLOR':'Color','SEPRGB':'Image','REROUTE':'Input','RGBTOBW':'Color'}[source['type']]
        if source['type']=='BUMP' and name=='Normal':socket='Normal'
        value=_origin(raw,source,socket,visited)
        if value:
            value['via'].append(source['type'])
            if source['type'] in {'SEP_COLOR','SEPARATE_COLOR','SEPRGB'}:
                value['component']={'Red':'R','Green':'G','Blue':'B','R':'R','G':'G','B':'B'}.get(link.get('from_name',link.get('from_socket')), '?')
        return value
    return None


def inferred_channels(raw):
    shaders=[n for n in raw.get('nodes',[]) if n.get('type')=='BSDF_PRINCIPLED' and not n.get('group_path')]
    if len(shaders)!=1:return {}
    images={i['name']:i for i in raw.get('images',[])};channels={}
    for channel,socket in CHANNELS.items():
        if socket:
            value=_origin(raw,shaders[0],socket)
            if value and value.get('image'):
                image=images.get(value['image'],{})
                channels[channel]={**value,'colorspace':image.get('colorspace')}
    for node in raw.get('nodes',[]):
        if node.get('type') in {'BUMP','DISPLACEMENT'}:
            value=_origin(raw,node,'Height')
            if value and value.get('image'):channels['height']={**value,'colorspace':images.get(value['image'],{}).get('colorspace')}
    return channels


def graph_issues(raw,definition=None):
    issues=[];nodes=raw.get('nodes',[]);links=raw.get('links',[])
    if not raw.get('use_nodes'):issues.append(issue('MATERIAL_NODES_DISABLED','ERROR','El material no usa nodos.'))
    outputs=[n for n in nodes if n.get('type')=='OUTPUT_MATERIAL' and n.get('active_output') and not n.get('group_path')]
    if not outputs:issues.append(issue('SHADER_OUTPUT_MISSING','ERROR','No hay Material Output activo.'))
    if outputs and not any(_incoming(raw,n,'Surface') for n in outputs):
        issues.append(issue('SHADER_OUTPUT_DISCONNECTED','ERROR','La salida Surface activa no recibe un shader.'))
    for link in links:
        if not link.get('valid',True):issues.append(issue('SHADER_LINK_INVALID','ERROR','Blender declara una conexión inválida.',link=link))
        if link.get('to_type')=='SHADER' and link.get('from_type')!='SHADER':
            issues.append(issue('SHADER_SOCKET_TYPE_MISMATCH','ERROR','Un input Shader no recibe salida Shader.',link=link))
    for node in nodes:
        kind=node.get('type')
        if kind not in SUPPORTED_NODES:
            issues.append(issue('SHADER_FEATURE_UNSUPPORTED','UNKNOWN','Nodo inspeccionable, fuera del constructor Principled calificado.',node=node['id'],node_type=kind))
        if kind=='TEX_IMAGE' and not node.get('image'):
            issues.append(issue('SHADER_IMAGE_MISSING','ERROR','Image Texture sin imagen asignada.',node=node['id']))
        if kind=='NORMAL_MAP' and not any(l.get('from_node')==node['id'] and not l.get('muted') for l in links):
            issues.append(issue('NORMAL_MAP_UNCONNECTED','WARNING','Normal Map no alimenta ningún socket.',node=node['id']))
        for socket in node.get('inputs',[]):
            value=socket.get('value')
            if isinstance(value,dict) and 'nonfinite' in value or isinstance(value,list) and any(isinstance(v,dict) and 'nonfinite' in v for v in value):
                issues.append(issue('SHADER_NONFINITE_VALUE','ERROR','Valor no finito en socket.',node=node['id'],socket=socket['name']))
        if kind=='BSDF_PRINCIPLED':
            for link in _incoming(raw,node,'Normal'):
                source=next((n for n in nodes if n['id']==link['from_node']),{})
                if source.get('type')=='TEX_IMAGE':issues.append(issue('NORMAL_IMAGE_DIRECT_CONNECTION','ERROR','RGB de normal conectado directamente; falta interpretación Normal Map.',node=node['id']))
    for owner,label in [(raw.get('animation',{}),'material'),(raw.get('node_animation',{}),'node_tree')]+[(g.get('animation',{}),g.get('id')) for g in raw.get('groups',[])]:
        if owner.get('action') or owner.get('drivers') or owner.get('nla_tracks'):
            issues.append(issue('MATERIAL_ANIMATION_PRESENT','UNKNOWN','Animación/drivers inspeccionados; constructor estático no los transforma.',owner=label))
    if definition:
        expected=definition.get('channels',{});actual=inferred_channels(raw)
        for channel,binding in expected.items():
            if channel=='ao':continue  # Explicit independent data channel; never force AO into Base Color.
            observed=actual.get(channel)
            if not observed or observed.get('image')!=binding.get('image'):
                issues.append(issue('PBR_CHANNEL_CONNECTION_MISMATCH','ERROR','La conexión no demuestra el canal declarado.',channel=channel,expected=binding,observed=observed))
            elif binding.get('component') and observed.get('component')!=binding['component']:
                issues.append(issue('PBR_CHANNEL_COMPONENT_MISMATCH','ERROR','Componente de imagen diferente al contrato.',channel=channel,expected=binding['component'],observed=observed.get('component')))
        shaders=[n for n in nodes if n.get('type')=='BSDF_PRINCIPLED' and not n.get('group_path')]
        if len(shaders)!=1:issues.append(issue('PBR_PRINCIPLED_AMBIGUOUS','ERROR','El contrato requiere un Principled principal explícito.'))
        else:
            if definition.get('parameters',{}).get('subsurface_method') and shaders[0].get('subsurface_method')!=definition['parameters']['subsurface_method']:
                issues.append(issue('SSS_METHOD_MISMATCH','ERROR','Método SSS diferente al contrato.',actual=shaders[0].get('subsurface_method'),expected=definition['parameters']['subsurface_method']))
            for parameter,value in definition.get('parameters',{}).items():
                if parameter not in SOCKETS:continue
                socket=_input(shaders[0],SOCKETS[parameter])
                if not socket:
                    issues.append(issue('PRINCIPLED_SOCKET_UNAVAILABLE','ERROR','Socket ausente en API real.',socket=SOCKETS[parameter]));continue
                if socket.get('linked'):continue
                actual_value=socket.get('value');a=actual_value if isinstance(actual_value,list) else [actual_value];b=value if isinstance(value,list) else [value]
                if len(a)!=len(b) or any(not isinstance(x,(int,float)) or not math.isfinite(x) or abs(x-y)>max(1e-6,abs(y)*1e-6) for x,y in zip(a,b)):
                    issues.append(issue('PBR_PARAMETER_MISMATCH','ERROR','El valor del shader difiere del contrato.',parameter=parameter,expected=value,actual=actual_value))
    return issues


def normal_issues(raw,definition=None,options=None):
    options=options or {};issues=[];nodes=raw.get('nodes',[]);images={i['name']:i for i in raw.get('images',[])}
    normals=[n for n in nodes if n.get('type')=='NORMAL_MAP'];channels=(definition or {}).get('channels') or inferred_channels(raw)
    convention=(definition or {}).get('normal_convention') or options.get('normal_convention','UNKNOWN')
    tolerance=options.get('normal_length_tolerance',.2)
    if isinstance(tolerance,bool) or not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or not .01<=tolerance<=.5:
        raise ValueError('NORMAL_TOLERANCE_INVALID: intervalo operativo 0.01..0.5')
    if normals or 'normal' in channels:
        if convention=='UNKNOWN':issues.append(issue('NORMAL_CONVENTION_UNKNOWN','UNKNOWN','No se puede deducir OpenGL/DirectX de los pixels o del nombre.'))
        if convention=='DIRECTX':issues.append(issue('NORMAL_DIRECTX_UNADAPTED','ERROR','Normal Map espera +Y OpenGL; no se invierte verde sin conversión explícita verificada.'))
    for node in normals:
        if node.get('space')!='TANGENT':issues.append(issue('NORMAL_SPACE_UNSUPPORTED','UNKNOWN','Este contrato valida tangentes; object/world requieren un adaptador separado.',node=node['id'],space=node.get('space')))
        binding=_origin(raw,node,'Color');image=images.get((binding or {}).get('image'),{})
        if not binding:issues.append(issue('NORMAL_COLOR_UNVERIFIED','UNKNOWN','No se certificó una imagen RGB conectada a Color.',node=node['id']))
        if image and not image.get('colorspace_is_data') and image.get('colorspace') not in {'Non-Color','Raw'}:
            issues.append(issue('NORMAL_COLORSPACE_MISMATCH','ERROR','La normal usa transferencia de color.',image=image['name']))
        if image and node.get('space')=='TANGENT':
            samples=image.get('rgb_samples',[])
            if not samples:
                issues.append(issue('NORMAL_PIXEL_DATA_UNVERIFIED','UNKNOWN','No se muestrearon los pixels; conexión y metadata no certifican una normal.',image=image['name']))
            else:
                invalid=[];length_errors=[];nonfinite=outside=0
                for index,row in enumerate(samples):
                    if len(row)!=3 or any(v is None or not isinstance(v,(int,float)) or not math.isfinite(v) for v in row):
                        nonfinite+=1;invalid.append(index);continue
                    if any(not 0<=v<=1 for v in row):outside+=1;invalid.append(index);continue
                    deviation=abs(math.sqrt(sum((2*v-1)**2 for v in row))-1.)
                    length_errors.append(deviation)
                    if deviation>tolerance:invalid.append(index)
                measured={'image':image['name'],'samples':len(samples),'invalid_samples':len(invalid),
                    'nonfinite_samples':nonfinite,'out_of_range_samples':outside,
                    'max_length_error':max(length_errors,default=None),'length_tolerance':tolerance,
                    'tolerance_provenance':'caller' if 'normal_length_tolerance' in options else 'proposed_starting_threshold',
                    'scope':image.get('sample_scope','buffer muestreado'),
                    'limitations':'No cubre todos los pixels/tiles ni demuestra convención Y; padding fuera de islas puede explicar desvíos parciales.'}
                if nonfinite or outside or len(invalid)==len(samples):
                    issues.append(issue('NORMAL_VECTOR_SAMPLES_INVALID','ERROR','Pixels muestreados incompatibles con una normal tangente RGB codificada.',**measured))
                elif invalid:
                    issues.append(issue('NORMAL_VECTOR_SAMPLES_SUSPICIOUS','WARNING','Parte de las muestras no aproxima un vector unitario; revisar cobertura UV/padding.',**measured))
                else:issues.append(issue('NORMAL_VECTOR_SAMPLES_CHECKED','INFO','Muestras RGB compatibles con vectores unitarios; certificación limitada al muestreo.',**measured))
        strength=_input(node,'Strength').get('value')
        if not isinstance(strength,(int,float)) or isinstance(strength,bool) or not math.isfinite(strength) or not 0<=strength<=10:
            issues.append(issue('NORMAL_STRENGTH_INVALID','ERROR','Strength fuera del rango operativo documentado 0..10.',node=node['id']))
        if node.get('uv_map') and node['uv_map'] not in raw.get('uv',{}).get('layers',[]):
            # M5 emits layer names; accept dictionaries defensively for future schemas.
            names=[x.get('name') if isinstance(x,dict) else x for x in raw.get('uv',{}).get('layers',[])]
            if node['uv_map'] not in names:issues.append(issue('NORMAL_UV_LAYER_MISSING','ERROR','La capa UV declarada en Normal Map no existe.',uv_map=node['uv_map']))
    if normals and not raw.get('uv',{}).get('available'):
        issues.append(issue('TANGENT_UV_REQUIRED','ERROR','Normal tangent necesita UV válido.'))
    return issues


def feature_issues(raw,definition=None,options=None):
    issues=[];nodes=raw.get('nodes',[]);renderer=(definition or {}).get('renderer') or (options or {}).get('renderer') or raw.get('renderer')
    params=(definition or {}).get('parameters',{})
    shaders=[n for n in nodes if n.get('type')=='BSDF_PRINCIPLED']
    values={s['name']:s.get('value') for node in shaders for s in node.get('inputs',[]) if not s.get('linked')}
    sss=params.get('subsurface_weight',values.get('Subsurface Weight',0));trans=params.get('transmission_weight',values.get('Transmission Weight',0))
    alpha=params.get('alpha',values.get('Alpha',1));emission=params.get('emission_strength',values.get('Emission Strength',0))
    if isinstance(sss,(int,float)) and sss>0:
        issues.append(issue('SSS_SCALE_ASSUMPTION','INFO','Subsurface Scale/Radius usan unidades escena; necesita espesor/escala y revisión visual.',scale_length=raw.get('units',{}).get('scale_length')))
    if isinstance(trans,(int,float)) and trans>0:
        issues.append(issue('TRANSMISSION_THICKNESS_ASSUMPTION','WARNING','Transmisión depende del espesor/geometría y renderer; no certifica hielo o vidrio.'))
        if renderer in {'EEVEE','BLENDER_EEVEE','BLENDER_EEVEE_NEXT'}:issues.append(issue('EEVEE_TRANSMISSION_LIMIT','WARNING','Reflexión/refracción EEVEE aproximadas; verificar con luz y espesor controlados.'))
    if isinstance(alpha,(int,float)) and alpha<1 or 'opacity' in (definition or {}).get('channels',{}):
        method=raw.get('properties',{}).get('surface_render_method')
        if method not in {'DITHERED','BLENDED'}:issues.append(issue('OPACITY_RENDER_METHOD_UNSUPPORTED','ERROR','Método alpha no validado en API real.'))
        issues.append(issue('OPACITY_SORTING_LIMIT','WARNING','Superficies superpuestas, sombras y ruido alpha requieren revisión.'))
    if isinstance(emission,(int,float)) and emission>0:
        issues.append(issue('EMISSION_UNITS_LIMIT','INFO','Strength es intensidad de shader, no conversión universal a lúmenes.'))
    for node in nodes:
        if node.get('type')=='DISPLACEMENT':
            if renderer not in {'CYCLES'}:issues.append(issue('DISPLACEMENT_RENDERER_UNSUPPORTED','ERROR','Desplazamiento geométrico no certificado en este renderer.'))
            issues.append(issue('DISPLACEMENT_GEOMETRY_UNCERTIFIED','UNKNOWN','Conexión no demuestra teselación/deformación real; M9 no altera geometría.'))
        if node.get('type')=='BUMP':
            distance=_input(node,'Distance').get('value')
            if not isinstance(distance,(int,float)) or not math.isfinite(distance) or distance<0:
                issues.append(issue('HEIGHT_DISTANCE_INVALID','ERROR','Bump Distance debe ser finita no negativa.'))
    return issues


def performance(raw):
    images=raw.get('images',[]);resident=0;filebytes=0;unknown=[];tiles=0
    for image in images:
        dims=image.get('size',[0,0]);channels=image.get('channels',4);width=4 if image.get('is_float') else 1
        if image.get('source')=='TILED':
            tiles+=len(image.get('tiles',[]));sizes=[d.get('file_dimensions') for d in image.get('dependencies',[])]
            if not sizes or any(not s for s in sizes):unknown.append(image['name'])
            resident+=sum(s[0]*s[1]*channels*width for s in sizes if s)
        else:resident+=dims[0]*dims[1]*channels*width
        filebytes+=sum(d.get('bytes') or 0 for d in image.get('dependencies',[]))
    return {'node_count':len(raw.get('nodes',[])),'link_count':len(raw.get('links',[])),
        'texture_count':len(images),'udim_count':tiles,'image_memory_estimate_bytes':resident,
        'image_memory_limit':'Estimación base sin mipmaps, duplicados GPU, compresión ni overhead; tiles sin tamaño no estimados.',
        'unknown_image_memory':unknown,'texture_file_bytes':filebytes,'shader_evaluation_ms':None,
        'render_timing_status':'UNKNOWN hasta un render medido','peak_memory_bytes':None,
        'payload_internal_bytes':len(json.dumps(raw,ensure_ascii=False,separators=(',',':')).encode())}


def report(name,raw,definition=None,options=None):
    options=options or {};name=name.removeprefix('production.');definition=resolve_definition(raw,definition)
    channels=(definition or {}).get('channels') or inferred_channels(raw)
    tex=textures.validate(raw.get('images',[]),channels);uv_report=uv.validate(raw,channels)
    groups={'graph':graph_issues(raw,definition),'texture':tex['issues'],'uv':uv_report['issues'],
        'normal':normal_issues(raw,definition,options),'features':feature_issues(raw,definition,options)}
    scopes={'texture_inspect':('texture',),'texture_validate_channels':('texture','graph'),
        'texture_validate_colorspace':('texture',),'texture_dependency_report':('texture',),
        'material_validate_normals':('normal','texture','graph'),
        'material_validate_height':('features','graph','texture'),
        'material_validate_tangent_workflow':('normal','uv'),
        'material_validate_uv':('uv',),'material_validate_udim':('uv','texture'),
        'material_analyze_texture_sets':('uv','texture'),'material_validate_graph':('graph',),
        'material_analyze_nodes':('graph',),'material_performance_report':(),
        'material_compatibility_report':('graph','texture','normal','features')}
    issues=[i for key in scopes.get(name,tuple(groups)) for i in groups[key]]
    for image in raw.get('missing_image_names',[]):issues.append(issue('TEXTURE_IMAGE_MISSING','ERROR','Datablock ausente.',image=image))
    errors=[i for i in issues if i['severity']=='ERROR'];warnings=[i for i in issues if i['severity']=='WARNING'];unknown=[i for i in issues if i['severity']=='UNKNOWN']
    blocking_unknown={'SHADER_FEATURE_UNSUPPORTED','MATERIAL_ANIMATION_PRESENT','NORMAL_CONVENTION_UNKNOWN',
        'NORMAL_SPACE_UNSUPPORTED','NORMAL_COLOR_UNVERIFIED','NORMAL_PIXEL_DATA_UNVERIFIED','DISPLACEMENT_GEOMETRY_UNCERTIFIED','TEXTURE_DEPENDENCY_UNVERIFIED','TEXTURE_DYNAMIC_UNSUPPORTED','MATERIAL_UV_UNVERIFIED'}
    unresolved=[i for i in unknown if i['code'] in blocking_unknown]
    result={'passed':not errors and not unresolved,'status':'warning' if errors or warnings or unknown else 'success',
        'material_name':raw.get('material_name'),'object_name':raw.get('object_name'),
        'errors':errors[:100],'warnings':warnings[:100],'error_count':len(errors),'warning_count':len(warnings),
        'issues':issues[:100],'issue_count':len(issues),'unknown':unknown[:50],
        'blocking_unknown':unresolved[:50],
        'channels':tex['channels'],'connections':inferred_channels(raw),'uv':uv_report,
        'metrics':performance(raw),'physical_plausibility':'requires_context','visual_realism':'requires_review',
        'tangent_validation':'prerequisites_only; no calc_tangents, no topology or UV mutation',
        'source_modified':False,'images_reloaded':False}
    if name=='material_compatibility_report':
        result['supported']=not errors and not unknown and not raw.get('linked')
        result['scope']='Constructor estático Principled; inspección de otros grafos no equivale a compatibilidad para mutación.'
    if name.startswith('texture_'):
        limit=options.get('limit',20);offset=options.get('offset',0)
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:raise ValueError('MATERIAL_PAGINATION_INVALID')
        result.update(images=[textures.public_image(i) for i in raw.get('images',[])[offset:offset+limit]],total_images=len(raw.get('images',[])),
            has_more=offset+limit<len(raw.get('images',[])),reload_status=tex['reload_status'])
    return result
