"""Immutable bake pair manifests and bounded, explicit smoke bakes."""
from __future__ import annotations
import json,math
from pathlib import Path
from uuid import uuid4
from .runtime import BAKE_RUNTIME
from ..uv.service import validate as validate_uv

FIELDS={'samples','front_distance_m','rear_distance_m','max_ray_distance_m','ray_padding_m','ray_multiplier','extrusion_m','max_cage_offset_m',
        'cage_tolerance_m','max_surface_deviation_m','allow_open_surface','cage_object','normal_convention','allow_cpu_fallback','smoke_resolution',
        'bake_samples','bake_margin_px','maps','tiles','geometry_adapter','rest_geometry_tolerance_m'}


def validate(options=None):
    options=dict(options or {})
    uv_fields={k:v for k,v in options.items() if k not in FIELDS}
    checked=validate_uv(uv_fields)
    checked.update({k:v for k,v in options.items() if k in FIELDS})
    for name,default,lo,hi in [('front_distance_m',.05,1e-7,10),('rear_distance_m',.05,0,10),('max_ray_distance_m',.2,1e-7,10),
        ('ray_padding_m',.001,0,.5),('ray_multiplier',1.25,1,4),('extrusion_m',.02,1e-7,1),('max_cage_offset_m',.1,1e-7,1),
        ('cage_tolerance_m',.0001,0,.01),('max_surface_deviation_m',.05,1e-7,1),('rest_geometry_tolerance_m',1e-6,1e-9,.0001)]:
        value=checked.get(name,default)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError(name+' outside finite documented range')
        checked[name]=float(value)
    for name,default,lo,hi in [('samples',256,8,4096),('smoke_resolution',64,32,256),('bake_samples',4,1,32),('bake_margin_px',2,0,16)]:
        value=checked.get(name,default)
        if isinstance(value,bool) or not isinstance(value,int) or not lo<=value<=hi:raise ValueError(name+' outside integer documented range')
        checked[name]=value
    for name in ('allow_cpu_fallback','allow_open_surface'):
        if name in checked and not isinstance(checked[name],bool):raise ValueError(name+' must be boolean')
    if checked.get('normal_convention','OPENGL') not in ('OPENGL','DIRECTX'):raise ValueError('normal_convention must be OPENGL or DIRECTX')
    if checked.get('geometry_adapter','BASE_STATIC') not in ('BASE_STATIC','BOUND_REST_MESH'):raise ValueError('geometry_adapter must be BASE_STATIC or BOUND_REST_MESH')
    if 'cage_object' in checked and (not isinstance(checked['cage_object'],str) or not checked['cage_object']):raise ValueError('cage_object must be nonempty name')
    maps=checked.get('maps',['NORMAL','AO'])
    if not isinstance(maps,list) or not maps or len(maps)>2 or len(set(maps))!=len(maps) or any(m not in ('NORMAL','AO') for m in maps):raise ValueError('maps must be NORMAL/AO (unique)')
    checked['maps']=maps
    if 'tiles' in checked and (not isinstance(checked['tiles'],list) or not 1<=len(checked['tiles'])<=16 or any(isinstance(t,bool) or not isinstance(t,int) or not 1001<=t<=2000 for t in checked['tiles'])):raise ValueError('smoke tiles must have 1..16 valid UDIM IDs')
    return checked


def read(bridge,storage,relationship,options=None,kind='validate'):
    from ..common.m5 import m5_read
    settings=validate(options)
    body="result=bake_validate_mesh(m5_obj(params['high_name']),m5_obj(params['target_name']),params['options'])" if kind=='validate' else "result=bake_measure(m5_obj(params['high_name']),m5_obj(params['target_name']),params['options'])\nresult.pop('_samples',None)"
    report=m5_read(bridge,storage,relationship,BAKE_RUNTIME,body,settings)
    report['status']='success' if report.get('passed',not report.get('ray_misses')) else 'warning'
    return report


def prepare_pairs(bridge,storage,operation_id,relationship,options=None):
    from ..common.m5 import validate_relationship,signature
    from ..common.storage import write_json,sha256
    settings=validate(options);relation=validate_relationship(bridge,storage,relationship)
    report=read(bridge,storage,relationship,settings)
    pair={'schema':'production.bake_pair.v1','pair_id':uuid4().hex,'relationship':relationship,'relationship_id':relation['relationship_id'],
        'high':relation['source_object'],'low':relation['target_object'],'high_signature':signature(bridge,relation['source_object']),
        'low_signature':signature(bridge,relation['target_object']),'options':settings,'readiness':report['readiness'],'report':report,
        'cage_signature':signature(bridge,settings['cage_object']) if settings.get('cage_object') else None}
    folder=storage.reserve('bake',operation_id);path=folder/'pair.json';write_json(path,pair);write_json(folder/'integrity.json',{'sha256':sha256(path)})
    return {'status':'success' if report['passed'] else 'warning','pair':str(path),'readiness':report['readiness'],'report':report,'apply':False}


def load_pair(bridge,storage,path):
    from ..common.m5 import load,signature,validate_relationship
    from ..common.storage import sha256
    p=Path(path).resolve(strict=True);pair=load(storage,str(p),'bake','production.bake_pair.v1')
    if sha256(p)!=json.loads((p.parent/'integrity.json').read_text())['sha256']:raise ValueError('STALE_BAKE_PAIR: manifest integrity mismatch')
    validate_relationship(bridge,storage,pair['relationship'])
    for kind in ('high','low'):
        if signature(bridge,pair[kind])['fingerprint']!=pair[kind+'_signature']['fingerprint']:raise ValueError('STALE_BAKE_PAIR: '+kind+' changed')
    if pair.get('cage_signature') and signature(bridge,pair['options']['cage_object'])['fingerprint']!=pair['cage_signature']['fingerprint']:raise ValueError('STALE_BAKE_PAIR: cage changed')
    return pair


def cage(bridge,storage,operation_id,relationship=None,mode='analyze',options=None,operation=None):
    from ..common.m5 import transaction
    settings=validate(options) if mode in ('analyze','propose') else None
    return transaction(bridge,storage,operation_id,'bake_cage',BAKE_RUNTIME,relationship=relationship,mode=mode,options=settings,operation=operation)


def smoke(bridge,storage,operation_id,pair,allow_cpu_fallback=False,options=None):
    from ..common.asset_safety import require_mutable_asset
    from ..common.m5 import M5_RUNTIME,signature
    from ..checkpoint import create_checkpoint
    from ..common.storage import write_json
    from ..qa.capture import render_backend
    if not isinstance(allow_cpu_fallback,bool):raise ValueError('allow_cpu_fallback must be boolean')
    if not isinstance(options or {},dict) or set(options or {})-{'smoke_resolution','bake_samples','bake_margin_px','maps','tiles'}:
        raise ValueError('FROZEN_BAKE_PAIR: smoke options may only set diagnostic resolution/samples/margin/maps/tile subset')
    frozen=load_pair(bridge,storage,pair);settings=validate(dict(frozen['options'],**(options or {})))
    settings['allow_cpu_fallback']=allow_cpu_fallback;settings['smoke_id']=operation_id.replace('-','')[:24]
    asset=require_mutable_asset(bridge,storage,'bake_smoke_test')
    # Probe Cycles device availability under a transient engine switch before checkpoint/bake.
    backend=bridge.run(_BACKEND,{'allow_cpu_fallback':allow_cpu_fallback})
    checkpoint=create_checkpoint(bridge,storage,'Before bounded smoke bake',operation_id,'bake_smoke_test')['manifest']
    folder=storage.reserve('bake',operation_id)
    tiles=settings.get('tiles',frozen['report']['manifest']['UDIM_tiles'])
    if not tiles or len(tiles)>16:raise ValueError('SMOKE_UDIM_CAPACITY: 1..16 tiles required')
    if any(t not in frozen['report']['manifest']['UDIM_tiles'] for t in tiles):raise ValueError('SMOKE_UDIM_TILE_UNEXPECTED: pair does not declare tile')
    paths=[{'tile':tile,'map':kind,'path':str(folder/(kind+'_'+str(tile)+'.png'))} for tile in tiles for kind in settings['maps']]
    result=bridge.run(M5_RUNTIME+'\n'+BAKE_RUNTIME+"\nresult=bake_smoke(m5_obj(params['high']),m5_obj(params['low']),params['options'],params['paths'])",
        {'high':frozen['high'],'low':frozen['low'],'options':settings,'paths':paths})
    after={k:signature(bridge,frozen[k]) for k in ('high','low')}
    differences={k:[section for section,value in frozen[k+'_signature']['sections'].items() if after[k]['sections'].get(section)!=value] for k in ('high','low')}
    intact=not any(differences.values())
    result.update(checkpoint=checkpoint,asset=asset,source_intact=intact,source_differences=differences,output_dir=str(folder))
    if not intact:result.update(status='error',passed=False,errors=['SMOKE_SOURCE_CHANGED'])
    write_json(folder/'report.json',result)
    return result


from ..qa.capture import _STATE
_BACKEND=_STATE+r'''
scene=bpy.context.scene;engine=scene.render.engine
try:
    scene.render.engine='CYCLES'
    result=qa_render_backend(scene,params['allow_cpu_fallback'])
finally:scene.render.engine=engine
'''
