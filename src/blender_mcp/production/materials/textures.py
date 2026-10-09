"""Read-only image metadata and bounded local dependencies; never reload or rewrite."""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct

from .model import COLOR_CHANNELS

MAX_FILE_BYTES=512*1024*1024
MAX_DEPENDENCY_BYTES=1024*1024*1024
RUNTIME=r'''
def mat9_image(image):
    if image is None:
        return None
    if len(image.tiles)>100:
        raise ValueError('MATERIAL_IMAGE_CAPACITY: como máximo 100 tiles por imagen')
    samples=[];pixel_count=int(image.size[0])*int(image.size[1])
    if image.has_data and pixel_count:
        stride=len(image.pixels)//pixel_count
        if stride>=3:
            count=min(256,pixel_count)
            for index in range(count):
                position=round((pixel_count-1)*index/max(1,count-1))
                values=[float(image.pixels[position*stride+c]) for c in range(3)]
                samples.append([v if math.isfinite(v) else None for v in values])
    paths=[]
    tiled=image.source=='TILED'
    for tile in image.tiles if tiled else [None]:
        number=tile.number if tile else None
        path=bpy.path.abspath(image.filepath,library=image.library) if image.filepath else ''
        if tiled and path:
            path=path.replace('<UDIM>',str(number))
            path=path.replace('<UVTILE>','u'+str((number-1001)%10+1)+'_v'+str((number-1001)//10+1))
        paths.append({'tile':number,'path':path})
    return {'name':image.name,'source':image.source,'type':image.type,'filepath':image.filepath,
        'owned':image.get('production_m9_owned') is True,'generation_id':image.get('production_generation_id'),
        'source_image':image.get('production_m9_source_image') or image.get('production_source'),
        'resolved_path':bpy.path.abspath(image.filepath,library=image.library) if image.filepath else '',
        'file_format':image.file_format,'size':list(image.size),'channels':image.channels,
        'depth_total_bits':image.depth,'is_float':image.is_float,'has_data':image.has_data,
        'is_dirty':image.is_dirty,'alpha_mode':image.alpha_mode,
        'colorspace':image.colorspace_settings.name,'colorspace_is_data':image.colorspace_settings.is_data,
        'packed':bool(image.packed_file) or bool(image.packed_files),
        'library':image.library.filepath if image.library else None,
        'tiles':[t.number for t in image.tiles] if tiled else [],'dependencies':paths,
        'reload_status':'UNKNOWN','reload_performed':False,
        'rgb_samples':samples,'pixel_count':pixel_count,'sample_count':len(samples),
        'sample_scope':'buffer activo solamente; no se cambia tile; muestreo uniforme de hasta 256 pixels',
        'precision_limit':'depth_total_bits describe el buffer Blender; no la precisión original del archivo.'}
'''


def _file(path):
    if not path:
        return {'path':path,'exists':False,'sha256':None,'bytes':None,'reason':'ruta vacía'}
    if path.startswith(('http:','https:','//','\\\\')):
        return {'path':path,'exists':None,'sha256':None,'bytes':None,'reason':'dependencia remota no inspeccionada'}
    p=Path(path)
    if not p.is_file():return {'path':path,'exists':False,'sha256':None,'bytes':None,'reason':'archivo ausente'}
    first=p.stat()
    if first.st_size>MAX_FILE_BYTES:
        return {'path':str(p),'exists':True,'sha256':None,'bytes':first.st_size,'reason':'MATERIAL_IMAGE_CAPACITY'}
    digest=hashlib.sha256();header=b''
    with p.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):
            if not header:header=chunk[:64]
            digest.update(chunk)
    after=p.stat()
    if (first.st_size,first.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('MATERIAL_IMAGE_CHANGED_DURING_READ: '+str(p))
    result={'path':str(p),'exists':True,'sha256':digest.hexdigest(),'bytes':first.st_size,
        'file_dimensions':None,'file_bit_depth_per_channel':None,'precision_source':'UNKNOWN','format_extension':p.suffix.lower()}
    if len(header)>=33 and header[:8]==b'\x89PNG\r\n\x1a\n' and header[12:16]==b'IHDR':
        width,height=struct.unpack('>II',header[16:24]);depth,color=header[24:26]
        result.update(file_dimensions=[width,height],file_bit_depth_per_channel=depth,precision_source='PNG_IHDR',
            file_color_type=color,file_has_alpha=color in (4,6))
    return result


def dependencies(images):
    """Return enriched copies. Reading does not change Blender reload state."""
    total=0;cache={};result=[]
    for image in images:
        row=dict(image);rows=[]
        for dependency in image.get('dependencies',[]):
            path=dependency['path']
            if path not in cache:
                if total>=MAX_DEPENDENCY_BYTES:raise ValueError('MATERIAL_IMAGE_CAPACITY: presupuesto de lectura de 1 GiB agotado')
                cache[path]=_file(path);total+=cache[path].get('bytes') or 0
                if total>MAX_DEPENDENCY_BYTES:raise ValueError('MATERIAL_IMAGE_CAPACITY: dependencias exceden 1 GiB')
            rows.append(dict(cache[path],tile=dependency.get('tile')))
        row['dependencies']=rows
        # Generated/packed buffers are usable without external files; the source remains explicit.
        row['missing_files']=[d.get('tile') for d in rows if d.get('exists') is False and not row.get('packed') and row.get('source') not in {'GENERATED','VIEWER'}]
        row['dependency_fingerprint']=hashlib.sha256(str(sorted((d.get('path'),d.get('sha256')) for d in rows)).encode()).hexdigest()
        result.append(row)
    return result


def issue(code,severity,message,**context):
    return {'code':code,'severity':severity,'message':message,**context}


def validate(images, channels=None):
    channels=channels or {};by_name={i['name']:i for i in images};issues=[];semantics=[]
    for image in images:
        name=image['name'];source=image.get('source')
        if image.get('missing_files'):
            issues.append(issue('TEXTURE_FILE_MISSING','ERROR','La imagen referencia archivos ausentes.',image=name,tiles=image['missing_files']))
        if not image.get('has_data'):
            issues.append(issue('TEXTURE_DATA_UNAVAILABLE','ERROR','Blender no tiene datos de imagen disponibles.',image=name))
        if image.get('is_dirty'):
            issues.append(issue('TEXTURE_BUFFER_DIRTY','WARNING','Buffer modificado; el archivo no representa necesariamente los pixels cargados.',image=name))
        if source in {'MOVIE','SEQUENCE','VIEWER'}:
            issues.append(issue('TEXTURE_DYNAMIC_UNSUPPORTED','UNKNOWN','Fuente dinámica fuera del constructor M9.',image=name))
        if source=='TILED' and len(image.get('tiles',[]))!=len(set(image.get('tiles',[]))):
            issues.append(issue('TEXTURE_DUPLICATE_TILE','ERROR','Identificadores UDIM repetidos.',image=name))
        dimensions={tuple(d['file_dimensions']) for d in image.get('dependencies',[]) if d.get('file_dimensions')}
        if len(dimensions)>1:
            issues.append(issue('UDIM_RESOLUTION_MISMATCH','WARNING','Tiles con resoluciones distintas; confirmar intención.',image=name))
        if any(d.get('sha256') is None and d.get('exists') is not False for d in image.get('dependencies',[]) if d.get('path')):
            issues.append(issue('TEXTURE_DEPENDENCY_UNVERIFIED','UNKNOWN','No se pudo certificar todo el contenido externo.',image=name))
    usage={}
    for channel,binding in channels.items():
        image=by_name.get(binding.get('image'));expected=binding.get('colorspace');component=binding.get('component','COLOR')
        semantics.append({'channel':channel,'image':binding.get('image'),'component':component,
            'interpretation':'COLOR' if channel in COLOR_CHANNELS else 'DATA','expected_colorspace':expected})
        if image is None:
            issues.append(issue('TEXTURE_IMAGE_MISSING','ERROR','El datablock de imagen no existe.',channel=channel,image=binding.get('image')));continue
        usage.setdefault(image['name'],set()).add('COLOR' if channel in COLOR_CHANNELS else 'DATA')
        if expected and image.get('colorspace')!=expected:
            issues.append(issue('TEXTURE_COLORSPACE_MISMATCH','ERROR','La interpretación de la imagen difiere del contrato.',
                channel=channel,image=image['name'],actual=image.get('colorspace'),expected=expected))
        if channel not in COLOR_CHANNELS and not image.get('colorspace_is_data') and image.get('colorspace') not in {'Non-Color','Raw'}:
            issues.append(issue('TEXTURE_DATA_COLORSPACE','ERROR','Un canal de datos usa una transferencia de color.',channel=channel,image=image['name']))
        if component=='A' and image.get('channels',0)<4:
            issues.append(issue('TEXTURE_ALPHA_UNAVAILABLE','ERROR','El canal alpha no está disponible.',channel=channel,image=image['name']))
        if channel=='normal' and image.get('channels',0)<3:
            issues.append(issue('NORMAL_CHANNELS_INSUFFICIENT','ERROR','Normal RGB necesita al menos tres componentes.',image=image['name']))
    for name,kinds in usage.items():
        if len(kinds)>1:issues.append(issue('TEXTURE_SEMANTIC_COLORSPACE_CONFLICT','ERROR','Una imagen compartida mezcla semántica de color y datos.',image=name))
    return {'issues':issues,'channels':semantics,'texture_count':len(images),
        'external_files':sum(len(i.get('dependencies',[])) for i in images),
        'reload_status':'UNKNOWN: no se recargaron imágenes ni se equiparó el hash externo al buffer Blender.'}


def public_image(image):
    return {key:value for key,value in image.items() if key!='rgb_samples'}
