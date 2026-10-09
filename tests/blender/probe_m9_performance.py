"""Finite material workload measurements in an owned Blender factory process.

No benchmark loop, texture downloads or large images. Uses local safe-mode
payload execution; final MCP transport qualification is a separate runner.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0,str(Path(__file__).parent))
from probe_m9_render import LocalBridge,snapshot
from fixtures_m9 import build,_png,_texture
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.materials import inspection,qa,render


def process_memory():
    """Read only this owned process, never another application or its memory."""
    try:
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
                *[(name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')]]
        counters=Counters();counters.cb=ctypes.sizeof(counters)
        current=ctypes.windll.kernel32.GetCurrentProcess;current.restype=wintypes.HANDLE
        query=ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        if not query(current(),ctypes.byref(counters),ctypes.sizeof(counters)):
            raise OSError('GetProcessMemoryInfo failed')
        return {'status':'MEASURED','working_set_bytes':counters.WorkingSetSize,'peak_working_set_bytes':counters.PeakWorkingSetSize,
                'private_commit_bytes':counters.PrivateUsage,
                'scope':'Pico acumulativo de vida del proceso propio; no pico exclusivo por caso ni VRAM GPU.'}
    except Exception as exc:
        return {'status':'UNKNOWN','reason':str(exc),'peak_working_set_bytes':None}


def _tiled(folder,name,semantic,tiles):
    import bpy
    rows=[]
    for tile in tiles:
        image,row=_png(folder,name,semantic,tile=tile);rows.append(row);bpy.data.images.remove(image)
    image=bpy.data.images.load(rows[0]['path'],check_existing=False);image.source='TILED';image.filepath=str(folder/(name+'.<UDIM>.png'))
    for tile in tiles:
        if tile!=1001:image.tiles.new(tile)
    image.colorspace_settings.name='Non-Color';image.reload()
    return image,rows


def workloads(fixture,folder):
    import bpy
    cases=fixture['cases']
    small=bpy.data.objects[cases['dielectric']['object_name']]
    medium=bpy.data.objects[cases['metallic']['object_name']]
    material=medium.active_material;bsdf=material.node_tree.nodes.get('Principled BSDF')
    normal=bpy.data.images[fixture['texture_definitions']['NORMAL']['image']]
    node=_texture(material,normal,'NORMAL');decoder=material.node_tree.nodes.new('ShaderNodeNormalMap');decoder.uv_map='UVMap'
    material.node_tree.links.new(node.outputs['Color'],decoder.inputs['Color']);material.node_tree.links.new(decoder.outputs['Normal'],bsdf.inputs['Normal'])
    height=bpy.data.images[fixture['texture_definitions']['HEIGHT']['image']]
    node=_texture(material,height,'HEIGHT');bump=material.node_tree.nodes.new('ShaderNodeBump');bump.inputs['Distance'].default_value=.008
    material.node_tree.links.new(node.outputs['Color'],bump.inputs['Height']);material.node_tree.links.new(decoder.outputs['Normal'],bump.inputs['Normal'])
    material.node_tree.links.new(bump.outputs['Normal'],bsdf.inputs['Normal'])
    complex_obj=bpy.data.objects[cases['single_material_multi_udim']['object_name']]
    material=complex_obj.active_material;tree=material.node_tree;bsdf=tree.nodes.get('Principled BSDF')
    tiles=[1001,1002,1011,1012,1021]
    roughness,roughness_rows=_tiled(folder,'perf_roughness','ROUGHNESS',tiles)
    normal,normal_rows=_tiled(folder,'perf_normal','NORMAL',tiles)
    rough=_texture(material,roughness,'PERF_ROUGHNESS_UDIM');norm=_texture(material,normal,'PERF_NORMAL_UDIM')
    group=bpy.data.node_groups.new('M9_Performance_RoughnessGroup','ShaderNodeTree')
    group.interface.new_socket(name='Roughness',in_out='INPUT',socket_type='NodeSocketFloat')
    group.interface.new_socket(name='Roughness',in_out='OUTPUT',socket_type='NodeSocketFloat')
    source=group.nodes.new('NodeGroupInput');target=group.nodes.new('NodeGroupOutput');last=source.outputs['Roughness']
    for index in range(12):
        node=group.nodes.new('ShaderNodeMath');node.operation='MULTIPLY';node.inputs[1].default_value=.97 if index%2 else 1.03
        group.links.new(last,node.inputs[0]);last=node.outputs[0]
    group.links.new(last,target.inputs['Roughness'])
    processing=tree.nodes.new('ShaderNodeGroup');processing.node_tree=group
    tree.links.new(rough.outputs['Color'],processing.inputs['Roughness']);tree.links.new(processing.outputs['Roughness'],bsdf.inputs['Roughness'])
    decoder=tree.nodes.new('ShaderNodeNormalMap');decoder.uv_map='UVMap';tree.links.new(norm.outputs['Color'],decoder.inputs['Color'])
    node=_texture(material,height,'HEIGHT');bump=tree.nodes.new('ShaderNodeBump');bump.inputs['Distance'].default_value=.005
    tree.links.new(node.outputs['Color'],bump.inputs['Height']);tree.links.new(decoder.outputs['Normal'],bump.inputs['Normal'])
    tree.links.new(bump.outputs['Normal'],bsdf.inputs['Normal'])
    # AO remains an independently labelled data channel, not multiplied into color.
    ao=bpy.data.images[fixture['texture_definitions']['AO']['image']];_texture(material,ao,'AO_INDEPENDENT')
    bsdf.inputs['Coat Weight'].default_value=.25;bsdf.inputs['Coat Roughness'].default_value=.12
    return [('small',small),('medium',medium),('complex_multi_udim',complex_obj)],{'roughness_tiles':roughness_rows,'normal_tiles':normal_rows}


def main(output):
    import bpy
    folder=Path(output).resolve();folder.mkdir(parents=True,exist_ok=False)
    report={'status':'RUNNING','cases':[],'checks':[],'errors':[],'blender':bpy.app.version_string,
            'transport_scope':'Owned Blender local safe-mode bridge; not MCP throughput benchmark'}
    def check(name,value):
        report['checks'].append({'name':name,'passed':bool(value)})
        if not value:raise AssertionError(name)
    try:
        fixture=build(folder/'fixtures');textures=folder/'benchmark-textures';textures.mkdir()
        jobs,definitions=workloads(fixture,textures);report['extra_textures']=definitions
        bridge=LocalBridge();storage=Storage(folder/'operations')
        for label,obj in jobs:
            material=obj.active_material;before=snapshot();memory_before=process_memory()
            start=time.perf_counter();raw=inspection.inspect(bridge,obj.name,material.name);inspect_seconds=time.perf_counter()-start
            metrics=qa.performance(raw);public=inspection.compact(raw,{'limit':20,'detail':'summary'})
            start=time.perf_counter()
            visual=render.render(bridge,storage,'benchmark-'+label,obj.name,material.name,material.name,
                {'width':192,'height':192,'views':['THREE_QUARTER','CLOSE_UP'],
                 'lighting_set':['NEUTRAL','SOFT','HARD','GRAZING'],'samples':32})
            render_seconds=time.perf_counter()-start
            record={'case':label,'object_name':obj.name,'material_name':material.name,'inspect_seconds':inspect_seconds,
                'render_wall_seconds':render_seconds,'metrics':metrics,'render':visual,'memory_before':memory_before,'memory_after':process_memory(),
                'public_compact_payload_bytes':len(json.dumps(public,ensure_ascii=False,separators=(',',':')).encode()),
                'public_payload_scope':'Compact inspector data only; excludes MCP JSON-RPC/envelope overhead',
                'source_unchanged':snapshot()==before,'distinct_uv_tiles':raw['uv'].get('material_tiles'),
                'group_count':len(raw.get('groups',[]))}
            report['cases'].append(record)
            check(label+' real render and context preservation',visual['status']=='ok' and visual['state_restored'] and record['source_unchanged'])
            check(label+' at most eight image pairs',len(visual['files'])==16)
            check(label+' compact payload bounded',record['public_compact_payload_bytes']<65536)
        check('complex benchmark has real group, multiple texture branches and five UV tiles',
            report['cases'][2]['metrics']['node_count']>=20 and report['cases'][2]['group_count']>=1
            and report['cases'][2]['metrics']['texture_count']>=5 and report['cases'][2]['metrics']['udim_count']>=15)
        report['status']='PASS'
    except Exception as exc:
        report['status']='FAIL';report['errors'].append({'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()})
    report['passed']=sum(c['passed'] for c in report['checks']);report['total']=len(report['checks'])
    with (folder/'report.json').open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2)
    print('__M9_PERFORMANCE__='+json.dumps({'status':report['status'],'passed':report['passed'],'total':report['total'],'report':str(folder/'report.json')}))
    if report['status']!='PASS':raise SystemExit(1)


if __name__=='__main__':main(sys.argv[sys.argv.index('--')+1])
