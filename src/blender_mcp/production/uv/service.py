"""Strict UV/UDIM contract. A proposal is mandatory before candidate application."""
from __future__ import annotations
import math
from .runtime import UV_RUNTIME

ALLOWED={'uv_layer','resolution','tile_resolutions','padding_px','max_stretch_ratio','allow_overlap','allow_flipped','check_padding',
         'overlap_area_tolerance','expected_tiles','assignments','rotate','seam_edges','material_seams','angle_seams','sharp_seams','seam_angle_degrees','uv_immutable','unwrap_method'}


def validate(options=None):
    options=dict(options or {})
    if set(options)-ALLOWED:raise ValueError('Unknown UV options: '+', '.join(sorted(set(options)-ALLOWED)))
    for field,default,low,high in [('padding_px',2.0,0,64),('max_stretch_ratio',8.0,1,1000),('overlap_area_tolerance',1e-10,0,0.001),('seam_angle_degrees',60.0,1,180)]:
        value=options.get(field,default)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ValueError(field+' is outside the documented finite range')
        options[field]=float(value)
    res=options.get('resolution',1024)
    if isinstance(res,bool) or not isinstance(res,int) or not 32<=res<=8192:raise ValueError('resolution must be 32..8192')
    options['resolution']=res
    if options.get('unwrap_method','ANGLE_BASED') not in ('ANGLE_BASED','SMART_PROJECT'):raise ValueError('unwrap_method must be ANGLE_BASED or SMART_PROJECT; no silent fallback')
    if options['padding_px']/res>=.2:raise ValueError('UV_PADDING_TOO_LARGE: padding must be <20% of tile width')
    for name in ('allow_overlap','allow_flipped','check_padding','rotate','material_seams','angle_seams','sharp_seams','uv_immutable'):
        if name in options and not isinstance(options[name],bool):raise ValueError(name+' must be boolean')
    for name in ('uv_layer',):
        if name in options and (not isinstance(options[name],str) or not options[name]):raise ValueError(name+' must be nonempty')
    resolutions=options.get('tile_resolutions',{})
    if not isinstance(resolutions,dict) or len(resolutions)>100:raise ValueError('tile_resolutions must have <=100 tiles')
    for tile,value in resolutions.items():
        if not str(tile).isdigit() or not 1001<=int(tile)<=2000 or isinstance(value,bool) or not isinstance(value,int) or not 32<=value<=8192:raise ValueError('invalid tile resolution')
    if 'expected_tiles' in options:
        if not isinstance(options['expected_tiles'],list) or len(options['expected_tiles'])>100 or any(isinstance(t,bool) or not isinstance(t,int) or not 1001<=t<=2000 for t in options['expected_tiles']):raise ValueError('expected_tiles must be <=100 UDIM tile IDs')
    if 'seam_edges' in options and (not isinstance(options['seam_edges'],list) or len(options['seam_edges'])>100000 or any(isinstance(i,bool) or not isinstance(i,int) or i<0 for i in options['seam_edges'])):raise ValueError('seam_edges must be explicit nonnegative edge indices')
    if 'assignments' in options:
        rows=options['assignments']
        if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError('assignments must have 1..100 rows')
        for row in rows:
            if not isinstance(row,dict) or set(row)-{'region','tile','material_index','faces'} or not isinstance(row.get('region'),str) or not row['region']:raise ValueError('assignment requires region, tile and material_index or faces')
            if isinstance(row.get('tile'),bool) or not isinstance(row.get('tile'),int) or not 1001<=row['tile']<=2000:raise ValueError('UDIM_TILE_INVALID')
            if ('material_index' in row)==('faces' in row):raise ValueError('assignment requires exactly one material_index or faces selector')
            ids=[row['material_index']] if 'material_index' in row else row['faces']
            if not isinstance(ids,list) or len(ids)>100000 or any(isinstance(i,bool) or not isinstance(i,int) or i<0 for i in ids):raise ValueError('assignment selectors must be nonnegative integers')
    return options


def analyze(bridge,storage,relationship,options=None,kind='analyze'):
    from ..common.m5 import m5_read
    data=m5_read(bridge,storage,relationship,UV_RUNTIME,"result=uv_metrics(m5_obj(params['target_name']),params['options'])",validate(options))
    data.update(status='success' if data['valid'] else 'warning',analysis_kind=kind)
    return data


def mutate(bridge,storage,operation_id,relationship=None,mode='analyze',options=None,operation=None,kind='generate'):
    from ..common.m5 import transaction
    settings=validate(options) if mode in ('analyze','propose') else None
    if settings is not None:settings['uv_operation']=kind
    return transaction(bridge,storage,operation_id,'uv_'+kind,UV_RUNTIME,relationship=relationship,mode=mode,options=settings,operation=operation)


def plan_udim(bridge,storage,relationship,options=None):
    from ..common.m5 import m5_read
    settings=validate(options)
    return m5_read(bridge,storage,relationship,UV_RUNTIME,r'''
obj=m5_obj(params['target_name']);layer,values=uv_data(obj,params['options'].get('uv_layer'))
islands,assignment=uv_assignment(obj,values,params['options'])
result={'status':'warning','apply':False,'strategy':'explicit_semantic_assignment_then_deterministic_shelf','tiles':sorted(set(assignment.values())),
    'assignments':params['options']['assignments'],'island_assignment':[{'island':k,'tile':v,'faces':len(islands[k]['faces'])} for k,v in list(assignment.items())[:100]],
    'risk':['Texel density may change during packing; validate after application.','Assignment never cuts an island silently.'],
    'uv_before':uv_metrics(obj,params['options'])}
''',settings)
