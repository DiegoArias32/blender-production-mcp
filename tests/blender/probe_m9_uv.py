"""Probe M9 sobre Blender propio en background; no usa sockets ni assets reales.

Ejemplo: blender --background --factory-startup --disable-autoexec
--python tests/blender/probe_m9_uv.py -- --output CARPETA_NUEVA
Los payloads pasan por build_code/safe_mode; esto no prueba transporte MCP.
"""
import argparse
import sys,json,types,contextlib,io
from pathlib import Path
repo=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(repo/'src'))
package=types.ModuleType('blender_mcp');package.__path__=[str(repo/'src'/'blender_mcp')];sys.modules['blender_mcp']=package
import bpy
from blender_mcp.production.materials import inspection
from blender_mcp.production.common.bridge import build_code
class Bridge:
    def run(self,body,params=None):
        scope={}
        with contextlib.redirect_stdout(io.StringIO()):exec(build_code(body,params),scope)
        return scope['result']


def write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Directorio nuevo; nunca se sobrescribe')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError('Este probe requiere un Blender propio background con factory-startup y sin .blend abierto')
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    cases={'valid':[(.1,.1),(.9,.1),(.9,.9),(.1,.9)],
        'crossed':[(.1,.1),(3.9,.1),(3.9,.9),(.1,.9)],
        'zero':[(.5,.5)]*4,'outside':[(-1.9,.1),(-1.1,.1),(-1.1,.9),(-1.9,.9)],
        'nonfinite':[(float('nan'),.1),(.9,.1),(.9,.9),(.1,.9)]}
    reports={};checks={};bridge=Bridge()
    for name,coordinates in cases.items():
        mesh=bpy.data.meshes.new(name);mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)],[],[(0,1,2,3)])
        obj=bpy.data.objects.new(name,mesh);bpy.context.scene.collection.objects.link(obj)
        mat=bpy.data.materials.new(name+'Material');mat.use_nodes=True;mesh.materials.append(mat)
        layer=mesh.uv_layers.new(name='UV')
        for item,value in zip(layer.uv,coordinates):item.vector=value
        before=repr([tuple(item.vector) for item in layer.uv])
        raw=inspection.inspect(bridge,obj.name,mat.name)
        report=inspection.report('material_validate_udim',raw)
        reports[name]=report
        checks[name+'_validation']=report['passed'] is (name=='valid')
        checks[name+'_unchanged']=before==repr([tuple(item.vector) for item in layer.uv])
    path=folder/'report.json'
    write_json(path, {'passed':all(checks.values()),'checks':checks,'reports':reports, 'scope':'Blender real, payload build_code+safe_mode local; no socket ni MCP transport'})
    print('M9_UV_AUDIT',str(path),json.dumps(checks))

    if not all(checks.values()):
        raise AssertionError('El probe M9 falló; consultar el report.json conservado')


if __name__ == '__main__':
    main()
