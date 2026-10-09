"""Read-only material/UV relations using the existing M5 UV mathematics."""
from __future__ import annotations

from ..uv.runtime import UV_RUNTIME
from ..uv.service import validate as validate_uv_options
from .textures import issue

RUNTIME=UV_RUNTIME+r'''
def mat9_tile_sort(tile):
    return -1 if tile is None else tile

def mat9_uv(obj,options):
    if obj is None or obj.type!='MESH':
        return {'available':False,'reason':'mesh no seleccionada','layers':[],'material_tiles':{}}
    if len(obj.data.loops)>200000:
        raise ValueError('MATERIAL_UV_CAPACITY: presupuesto de 200000 loops para inspección material')
    data=uv_metrics(obj,options)
    data['available']=bool(obj.data.uv_layers)
    data['material_tiles']={}
    if 'UV_NONFINITE' in data.get('errors',[]):
        return data
    if obj.data.uv_layers:
        layer,values=uv_data(obj,options.get('uv_layer'))
        for face in obj.data.polygons:
            tile=tile_of([values[i] for i in face.loop_indices])
            key=str(face.material_index)
            data['material_tiles'].setdefault(key,[])
            if tile not in data['material_tiles'][key]:data['material_tiles'][key].append(tile)
        for key,tiles in data['material_tiles'].items():
            data['material_tiles'][key]=sorted(tiles,key=mat9_tile_sort)
    return data
'''


def options(value=None):
    # Existing M5 options/tolerances are kept verbatim. No rewrite or reclassification of bake readiness.
    return validate_uv_options(value or {})


def validate(raw, channels=None):
    uv=raw.get('uv') or {};issues=[];images=raw.get('images',[])
    needs_uv=bool(channels) or any(n.get('type') in {'TEX_IMAGE','NORMAL_MAP','UVMAP'} for n in raw.get('nodes',[]))
    if needs_uv and not uv.get('available'):
        issues.append(issue('MATERIAL_UV_MISSING','ERROR','El material texturizado requiere UV y no hay una capa activa.'))
    if uv.get('available'):
        for code in uv.get('errors',[]):
            severity='ERROR' if code in {'UV_NONFINITE','UV_ZERO_AREA','UV_CROSS_TILE_OR_OUT_OF_RANGE','UDIM_TILES_MISMATCH'} else 'WARNING'
            issues.append(issue('M5_'+str(code),severity,'Hallazgo UV heredado de M5; no se modifica ni se certifica bake readiness.'))
            if code=='UV_CAPACITY_EXCEEDED':
                issues.append(issue('MATERIAL_UV_UNVERIFIED','UNKNOWN','El análisis UV agotó su presupuesto; no puede certificar la correspondencia material/UV.'))
    selected=[str(s['index']) for s in raw.get('slots',[]) if s.get('material')==raw.get('material_name')]
    actual=set(t for index in selected for t in uv.get('material_tiles',{}).get(index,[]) if t is not None)
    # With an unassigned candidate inspect the designated source object's selected slot relations.
    if not selected:actual=set(t for row in uv.get('material_tiles',{}).values() for t in row if t is not None)
    relevant=[uv.get('material_tiles',{}).get(index,[]) for index in selected] if selected else list(uv.get('material_tiles',{}).values())
    if any(None in row for row in relevant):
        issues.append(issue('MATERIAL_UV_TILE_UNRESOLVED','ERROR','Una cara cruza tiles o sale del dominio; no se infieren tiles completos sólo por sus vértices.'))
    names={b.get('image') for b in (channels or {}).values()}
    image_rows=[]
    for image in images:
        if names and image['name'] not in names:continue
        tiles=set(image.get('tiles',[]))
        if image.get('source')=='TILED':
            missing=sorted(actual-tiles)
            if missing:issues.append(issue('UDIM_TILE_MISSING','ERROR','La imagen no contiene todos los tiles usados por el material.',image=image['name'],tiles=missing))
            if any(type(t) is not int or not 1001<=t<=2000 for t in tiles):
                issues.append(issue('UDIM_TILE_INVALID','ERROR','Tile fuera del intervalo certificado.',image=image['name']))
            image_rows.append({'image':image['name'],'tiles':sorted(tiles),'used_tiles':sorted(actual),'missing_tiles':missing})
        elif len(actual)>1 or any(t!=1001 for t in actual):
            issues.append(issue('UV_SINGLE_IMAGE_MULTITILE','WARNING','Imagen no UDIM con UV fuera de 1001; repetir puede ser intencional.',image=image['name']))
    return {'issues':issues,'active_uv':uv.get('active_layer'),'material_tiles':uv.get('material_tiles',{}),
        'texture_sets':image_rows,'uv_valid':uv.get('valid'),
        'bake_readiness':'UNKNOWN','bake_readiness_limit':'Este análisis de material no sustituye a bake_validate_mesh M5.',
        'one_material_per_udim_required':False}
