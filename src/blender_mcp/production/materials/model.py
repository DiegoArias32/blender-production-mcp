"""Bounded material definitions; profiles are declared starting points, never certificates."""
from __future__ import annotations

from copy import deepcopy
import math

CATEGORIES = frozenset({'SKIN','FABRIC','LEATHER','METAL','RUBBER','PLASTIC','BONE','ICE','SNOW',
    'WET_SURFACE','PAINTED_METAL','ORGANIC_CREATURE_TISSUE','GENERIC_DIELECTRIC','CUSTOM'})
SOCKETS = {'base_color':'Base Color','metallic':'Metallic','roughness':'Roughness','ior':'IOR',
    'alpha':'Alpha','subsurface_weight':'Subsurface Weight','subsurface_radius':'Subsurface Radius',
    'subsurface_scale':'Subsurface Scale','subsurface_ior':'Subsurface IOR',
    'subsurface_anisotropy':'Subsurface Anisotropy','transmission_weight':'Transmission Weight',
    'coat_weight':'Coat Weight','coat_roughness':'Coat Roughness','coat_ior':'Coat IOR','coat_tint':'Coat Tint',
    'emission_color':'Emission Color','emission_strength':'Emission Strength',
    'specular_ior_level':'Specular IOR Level','sheen_weight':'Sheen Weight','sheen_roughness':'Sheen Roughness'}
CHANNELS = {'base_color':'Base Color','metallic':'Metallic','roughness':'Roughness','normal':'Normal',
    'ao':None,'height':None,'opacity':'Alpha','emission':'Emission Color'}
COLOR_CHANNELS = frozenset({'base_color','emission'})
RANGES = {key:(0.,1.) for key in ('metallic','roughness','alpha','subsurface_weight','transmission_weight',
    'coat_weight','coat_roughness','specular_ior_level','sheen_weight','sheen_roughness','bump_strength','height_midlevel')}
RANGES.update(ior=(1.,4.),subsurface_ior=(1.,4.),coat_ior=(1.,4.),subsurface_scale=(0.,1.),
    subsurface_anisotropy=(0.,1.),normal_strength=(0.,10.),height_distance=(0.,1.),emission_strength=(0.,10000.))
VECTOR_PARAMETERS = {'base_color':4,'emission_color':4,'coat_tint':4,'subsurface_radius':3}
DEFAULTS = {'base_color':[.5,.5,.5,1.],'metallic':0.,'roughness':.5,'ior':1.5,'alpha':1.,
    'subsurface_weight':0.,'subsurface_radius':[1.,.2,.1],'subsurface_scale':.005,'subsurface_ior':1.4,
    'subsurface_anisotropy':0.,'transmission_weight':0.,'coat_weight':0.,'coat_roughness':.03,
    'coat_ior':1.5,'coat_tint':[1.,1.,1.,1.],'emission_color':[1.,1.,1.,1.],
    'emission_strength':0.,'specular_ior_level':.5,'sheen_weight':0.,'sheen_roughness':.5,
    'normal_strength':1.,'bump_strength':1.,'height_distance':.01,'height_midlevel':.5,
    'subsurface_method':'RANDOM_WALK_SKIN'}


def _profile(category, values, required=(), optional=None, limitations=()):
    return {'category':category,'parameters':values,'required_channels':list(required),
        'optional_channels':list(optional or CHANNELS),'shader_model':'Principled BSDF / Metallic-Roughness',
        'parameter_ranges':{key:list(RANGES[key]) for key in values if key in RANGES},
        'texture_dependencies':'Canales opcionales deben referenciar imágenes existentes y verificadas.',
        'limitations':['Configuración inicial propuesta; calibrar con escala, texturas y luz del asset.',
            'Un perfil no certifica composición física ni fotorrealismo.',*limitations]}


PROFILES = {
    'human_skin':_profile('SKIN',{'metallic':0.,'roughness':.45,'subsurface_weight':.25,
        'subsurface_scale':.0015,'base_color':[.5,.25,.18,1.]},limitations=['No produce texturas de piel ni anatomía.']),
    'creature_skin':_profile('ORGANIC_CREATURE_TISSUE',{'metallic':0.,'roughness':.5,
        'subsurface_weight':.15,'subsurface_scale':.002,'base_color':[.18,.28,.16,1.]}),
    'fabric':_profile('FABRIC',{'metallic':0.,'roughness':.75,'sheen_weight':.2}),
    'leather':_profile('LEATHER',{'metallic':0.,'roughness':.5,'coat_weight':.1}),
    'metal':_profile('METAL',{'metallic':1.,'roughness':.3},limitations=['Metallic fraccional permitido para mezcla/cobertura declarada.']),
    'painted_metal':_profile('PAINTED_METAL',{'metallic':0.,'roughness':.4,'coat_weight':.15},
        limitations=['La pintura dieléctrica cubre metal; desgaste espacial necesita una máscara de autoría.']),
    'rubber':_profile('RUBBER',{'metallic':0.,'roughness':.65,'base_color':[.025,.025,.025,1.]}),
    'bone':_profile('BONE',{'metallic':0.,'roughness':.55,'subsurface_weight':.05,'base_color':[.65,.6,.48,1.]}),
    'ice':_profile('ICE',{'metallic':0.,'roughness':.15,'transmission_weight':.75,'ior':1.31},
        limitations=['Transmisión depende del espesor y renderer; no toda superficie congelada es transparente.']),
    'solid_ice':_profile('ICE',{'metallic':0.,'roughness':.12,'transmission_weight':.8,'ior':1.31}),
    'frosted_ice':_profile('ICE',{'metallic':0.,'roughness':.45,'transmission_weight':.35,'ior':1.31}),
    'snow':_profile('SNOW',{'metallic':0.,'roughness':.75,'subsurface_weight':.15,'base_color':[.8,.85,.9,1.]}),
    'compacted_snow':_profile('SNOW',{'metallic':0.,'roughness':.6,'subsurface_weight':.1,'base_color':[.7,.78,.82,1.]}),
    'wet_surface':_profile('WET_SURFACE',{'roughness':.2,'coat_weight':.65,'coat_roughness':.08}),
    'wet_fabric':_profile('FABRIC',{'metallic':0.,'roughness':.35,'coat_weight':.45,'coat_roughness':.12}),
    'wet_skin':_profile('SKIN',{'metallic':0.,'roughness':.25,'subsurface_weight':.15,'coat_weight':.55,'coat_roughness':.08}),
    'wet_metal':_profile('METAL',{'metallic':1.,'roughness':.25,'coat_weight':.65,'coat_roughness':.08}),
    'wet_creature':_profile('ORGANIC_CREATURE_TISSUE',{'metallic':0.,'roughness':.28,'subsurface_weight':.12,
        'coat_weight':.55,'coat_roughness':.08}),
}


def _number(value, name, low, high):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
        raise ValueError('MATERIAL_PARAMETER_INVALID: '+name+' debe estar en el intervalo finito '+str((low,high)))
    return float(value)


def create_profile(profile, overrides=None):
    if not isinstance(profile,str) or profile not in PROFILES:
        raise ValueError('MATERIAL_PROFILE_UNSUPPORTED: '+str(profile))
    data=deepcopy(PROFILES[profile]);data.update(name=profile,schema='production.material_profile.v1',provenance='proposed_starting_configuration')
    if overrides:
        if not isinstance(overrides,dict):raise ValueError('MATERIAL_PROFILE_OVERRIDES_INVALID')
        checked=normalize_definition({'parameters':overrides} if set(overrides)<=set(DEFAULTS) else overrides,profile)
        data['parameters']=checked['parameters'];data['overrides_provenance']='caller'
    return data


def normalize_definition(definition=None, profile=None):
    if definition is None:definition={}
    if not isinstance(definition,dict):raise ValueError('MATERIAL_DEFINITION_INVALID: se requiere un objeto estructurado')
    allowed={'schema','category','workflow','parameters','channels','normal_convention','height_mode','renderer','profile'}
    if set(definition)-allowed:raise ValueError('MATERIAL_DEFINITION_UNSUPPORTED: '+str(sorted(set(definition)-allowed)))
    if definition.get('schema','production.material_definition.v1')!='production.material_definition.v1':
        raise ValueError('MATERIAL_DEFINITION_SCHEMA: versión no soportada')
    declared=definition.get('profile')
    if profile and declared and profile!=declared:raise ValueError('MATERIAL_PROFILE_CONFLICT: dos perfiles diferentes')
    profile=profile or declared
    preset=create_profile(profile) if profile else {'category':'GENERIC_DIELECTRIC','parameters':{}}
    category=definition.get('category',preset['category'])
    if category not in CATEGORIES:raise ValueError('MATERIAL_CATEGORY_UNSUPPORTED: '+str(category))
    if definition.get('workflow','METALLIC_ROUGHNESS')!='METALLIC_ROUGHNESS':
        raise ValueError('MATERIAL_WORKFLOW_UNSUPPORTED: sólo METALLIC_ROUGHNESS está calificado')
    parameters=deepcopy(DEFAULTS);parameters.update(preset['parameters'])
    requested=definition.get('parameters',{})
    if not isinstance(requested,dict) or set(requested)-set(DEFAULTS):raise ValueError('MATERIAL_PARAMETER_UNSUPPORTED: parámetro desconocido')
    parameters.update(deepcopy(requested))
    for key,value in parameters.items():
        if key=='subsurface_method':
            if not isinstance(value,str) or value not in {'BURLEY','RANDOM_WALK','RANDOM_WALK_SKIN','RANDOM_WALK_LEGACY'}:
                raise ValueError('MATERIAL_SSS_METHOD_UNSUPPORTED: '+str(value))
        elif key in VECTOR_PARAMETERS:
            width=VECTOR_PARAMETERS[key]
            if not isinstance(value,(list,tuple)) or len(value)!=width:raise ValueError('MATERIAL_PARAMETER_INVALID: '+key+' longitud '+str(width))
            maximum=100. if key=='subsurface_radius' else 10000. if key=='emission_color' else 1.
            parameters[key]=[_number(v,key,0.,maximum) for v in value]
            if width==4:parameters[key][3]=_number(value[3],key+'.alpha',0.,1.)
        else:parameters[key]=_number(value,key,*RANGES[key])
    channels=definition.get('channels',{})
    if not isinstance(channels,dict) or set(channels)-set(CHANNELS):raise ValueError('MATERIAL_CHANNEL_UNSUPPORTED: canal desconocido')
    checked={}
    for key,row in channels.items():
        if not isinstance(row,dict) or set(row)-{'image','component','colorspace'}:
            raise ValueError('MATERIAL_CHANNEL_INVALID: '+key+' requiere image/component/colorspace')
        image=row.get('image')
        if not isinstance(image,str) or not image.strip() or len(image)>255:raise ValueError('MATERIAL_IMAGE_REQUIRED: '+key)
        component=row.get('component','COLOR' if key in COLOR_CHANNELS|{'normal'} else 'R')
        if component not in {'COLOR','R','G','B','A'}:raise ValueError('MATERIAL_COMPONENT_INVALID: '+key)
        if key in COLOR_CHANNELS|{'normal'} and component!='COLOR':raise ValueError('MATERIAL_COMPONENT_INVALID: '+key+' necesita COLOR')
        colorspace=row.get('colorspace','sRGB' if key in COLOR_CHANNELS else 'Non-Color')
        if not isinstance(colorspace,str) or not colorspace.strip() or len(colorspace)>128:raise ValueError('MATERIAL_COLORSPACE_INVALID: '+key)
        checked[key]={'image':image,'component':component,'colorspace':colorspace}
    renderer=definition.get('renderer','EEVEE')
    if renderer not in {'EEVEE','CYCLES'}:raise ValueError('MATERIAL_RENDERER_UNSUPPORTED: '+str(renderer))
    convention=definition.get('normal_convention','UNKNOWN')
    if convention not in {'OPENGL','DIRECTX','UNKNOWN'}:raise ValueError('MATERIAL_NORMAL_CONVENTION_INVALID')
    height=definition.get('height_mode','BUMP' if 'height' in checked else 'NONE')
    if height not in {'NONE','BUMP','DISPLACEMENT'}:raise ValueError('MATERIAL_HEIGHT_MODE_UNSUPPORTED')
    if ('height' in checked)!=(height!='NONE'):raise ValueError('MATERIAL_HEIGHT_CHANNEL_REQUIRED: height y height_mode deben concordar')
    return {'schema':'production.material_definition.v1','category':category,'workflow':'METALLIC_ROUGHNESS',
        'parameters':parameters,'channels':checked,'normal_convention':convention,'height_mode':height,
        'renderer':renderer,'profile':profile}


def validate_profile(profile, definition=None):
    record=create_profile(profile);normalized=normalize_definition(definition,profile)
    missing=sorted(set(record['required_channels'])-set(normalized['channels']))
    return {'passed':not missing,'profile':record,'definition':normalized,
        'errors':['MATERIAL_PROFILE_CHANNEL_MISSING: '+key for key in missing],
        'warnings':record['limitations'],'certifies_photorealism':False}
