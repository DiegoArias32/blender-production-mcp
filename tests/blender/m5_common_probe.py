"""Real installed RNA/signatures: only unsaved generated meshes, no sockets."""
import sys,types,json
from pathlib import Path
import bpy
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')]
sys.modules['blender_mcp']=package
from blender_mcp.production.common.m5 import M5_RUNTIME
from blender_mcp.production.common.bridge import build_code,payload_bytes

def run(body,params=None):
    code=build_code(M5_RUNTIME+'\n'+body,params)
    namespace={};exec(code.rsplit('print(',1)[0],namespace)
    return namespace['result']

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_uv_sphere_add(segments=24,ring_count=12)
obj=bpy.context.object;obj.name='Generated_M5_Common'
results={}
for mod_type in ('NONE','ARMATURE','DECIMATE','SUBSURF','TRIANGULATE'):
    mod=obj.modifiers.new('RNA_'+mod_type,mod_type) if mod_type!='NONE' else None
    try:
        first=run('result=m5_signature(m5_obj(params["name"]))',{'name':obj.name})
        second=run('result=m5_signature(m5_obj(params["name"]))',{'name':obj.name})
        results[mod_type]={'stable':first==second,'counts':first['counts']}
    finally:
        if mod:obj.modifiers.remove(mod)
source=run('result=m5_signature(m5_obj(params["name"]))',{'name':obj.name})
copy=run('result=m5_signature(m5_copy(m5_obj(params["source"]),params["name"],"fixture-operation"))',{'source':obj.name,'name':'Copy'})
results['copy']={'source_intact':source==run('result=m5_signature(m5_obj(params["name"]))',{'name':obj.name}),
    'different_mesh':copy['mesh']!=source['mesh'],'same_geometry':copy['geometry_fingerprint']==source['geometry_fingerprint']}
attr=obj.data.attributes.new('SemanticColor','FLOAT_COLOR','POINT');attr.data[0].color=(.2,.3,.4,1)
a=run('result=m5_signature(m5_obj(params["name"]))',{'name':obj.name})
attr.data[0].color=(.7,.3,.4,1)
b=run('result=m5_signature(m5_obj(params["name"]))',{'name':obj.name})
results['attribute_content_guard']={'changed':a['fingerprint']!=b['fingerprint'],'unsupported':b['unsupported_attributes']}
results['payload_bytes']=payload_bytes(M5_RUNTIME+'\nresult=m5_signature(m5_obj(params["name"]))',{'name':obj.name})
print('M5_COMMON_REPORT='+json.dumps(results),flush=True)
assert all(row.get('stable',True) for row in results.values() if isinstance(row,dict))
assert results['copy']['source_intact'] and results['copy']['different_mesh'] and results['copy']['same_geometry']
assert results['attribute_content_guard']['changed'] and not results['attribute_content_guard']['unsupported']
