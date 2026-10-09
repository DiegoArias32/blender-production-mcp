"""Probe M9 sobre Blender propio en background; no usa sockets ni assets reales.

Ejemplo: blender --background --factory-startup --disable-autoexec
--python tests/blender/probe_m9_inspection.py -- --output CARPETA_NUEVA
Los payloads pasan por build_code/safe_mode; esto no prueba transporte MCP.
"""
import argparse
import sys, json, contextlib, io, hashlib, time, traceback, types
from pathlib import Path
repo=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(repo/'src'));sys.path.insert(0,str(repo/'tests'/'blender'))
package=types.ModuleType('blender_mcp');package.__path__=[str(repo/'src'/'blender_mcp')];sys.modules['blender_mcp']=package
import bpy
from fixtures_m9 import build
from blender_mcp.production.materials import inspection, model
from blender_mcp.production.common.bridge import build_code

class LocalBridge:
    def run(self,body,params=None):
        scope={}
        with contextlib.redirect_stdout(io.StringIO()):exec(build_code(body,params),scope)
        return scope['result']

def preserve():
    return {'frame':bpy.context.scene.frame_current,'objects':sorted(bpy.data.objects.keys()),
        'materials':sorted(bpy.data.materials.keys()),'actions':sorted(bpy.data.actions.keys()),
        'meshes':{m.name:{'v':[tuple(v.co) for v in m.vertices],'uv':[[tuple(v.vector) for v in l.uv] for l in m.uv_layers],
            'polygons':[(tuple(p.vertices),p.material_index) for p in m.polygons]} for m in bpy.data.meshes},
        'slots':{o.name:[(s.link,s.material.name if s.material else None) for s in o.material_slots] for o in bpy.data.objects},
        'files':{i.name:(i.filepath,i.colorspace_settings.name,i.is_dirty) for i in bpy.data.images}}



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
    fixture=build(folder/'fixture')
    bridge=LocalBridge();before=preserve();results={};all_raw={};started=time.perf_counter()
    for name,case in fixture['cases'].items():
        try:
            raw=inspection.inspect(bridge,case['object_name'],case['material_name'])
            report=inspection.report('material_quality_report',raw)
            all_raw[name]=raw
            results[name]={'passed':report['passed'],'codes':[i['code'] for i in report['issues']],
                'images':len(raw['images']),'counts':raw['counts'],'material_tiles':raw['uv']['material_tiles'],
                'uv_valid':raw['uv']['valid'],'internal_bytes':report['metrics']['payload_internal_bytes']}
        except Exception as error:results[name]={'exception':repr(error),'traceback':traceback.format_exc()}
    after=preserve()
    expected={'missing_image':'SHADER_IMAGE_MISSING','roughness_wrong_colorspace':'TEXTURE_DATA_COLORSPACE',
        'normal_broken_wiring':'NORMAL_IMAGE_DIRECT_CONNECTION','invalid_graph':'SHADER_OUTPUT_DISCONNECTED',
        'unsupported_shader':'SHADER_FEATURE_UNSUPPORTED','missing_udim_tile':'UDIM_TILE_MISSING'}
    checks={name:code in results[name].get('codes',[]) for name,code in expected.items()}
    checks.update(no_exceptions=all('exception' not in r for r in results.values()),source_state_preserved=before==after,
        fixture_file_preserved=hashlib.sha256(Path(fixture['source_file']).read_bytes()).hexdigest()==fixture['source_sha256'])
    checks['unsupported_shader_not_pass']=results['unsupported_shader']['passed'] is False
    normal_case=fixture['cases']['normal_map'];normal_raw=all_raw['normal_map']
    normal_report=inspection.report('material_validate_normals',normal_raw,options={'normal_convention':'OPENGL'})
    checks['real_normal_pixels_pass']=normal_report['passed'] and any(i['code']=='NORMAL_VECTOR_SAMPLES_CHECKED' for i in normal_report['issues'])
    black=bpy.data.images.new('M9_Inspector_BlackNormal',width=4,height=4);black.colorspace_settings.name='Non-Color'
    black.pixels.foreach_set([0.,0.,0.,1.]*16)
    normal_material=bpy.data.materials[normal_case['material_name']]
    normal_texture=next(n for n in normal_material.node_tree.nodes if n.type=='TEX_IMAGE');original_normal_image=normal_texture.image
    normal_texture.image=black
    black_report=inspection.report('material_validate_normals',inspection.inspect(bridge,normal_case['object_name'],normal_case['material_name']),options={'normal_convention':'OPENGL'})
    checks['black_normal_rejected']=not black_report['passed'] and any(i['code']=='NORMAL_VECTOR_SAMPLES_INVALID' for i in black_report['issues'])
    normal_texture.image=original_normal_image
    case=fixture['cases']['metallic'];material=bpy.data.materials[case['material_name']];tree=material.node_tree
    rough_node=next(n for n in tree.nodes if n.type=='TEX_IMAGE' and n.label=='ROUGHNESS');principled=next(n for n in tree.nodes if n.type=='BSDF_PRINCIPLED')
    tree.links.new(rough_node.outputs['Color'],principled.inputs['Base Color'])
    miswired=inspection.inspect(bridge,case['object_name'],case['material_name'])
    miswired_report=inspection.report('material_validate_pbr',miswired,model.normalize_definition(case['material_definition']))
    checks['wrong_declared_channel_rejected']=not miswired_report['passed'] and any(i['code']=='PBR_CHANNEL_CONNECTION_MISMATCH' for i in miswired_report['issues'])
    group=bpy.data.node_groups.new('M9_Inspector_NestedGroup','ShaderNodeTree')
    group.interface.new_socket(name='Shader',in_out='OUTPUT',socket_type='NodeSocketShader')
    group.nodes.new('ShaderNodeTexImage')
    group_output=group.nodes.new('NodeGroupOutput');group_bsdf=group.nodes.new('ShaderNodeBsdfPrincipled')
    group.links.new(group_bsdf.outputs['BSDF'],group_output.inputs['Shader'])
    node=tree.nodes.new('ShaderNodeGroup');node.node_tree=group
    nested=inspection.inspect(bridge,case['object_name'],case['material_name'])
    nested_report=inspection.report('material_analyze_nodes',nested)
    checks['nested_group_inspected']=len(nested['groups'])==1 and any(i['code']=='SHADER_IMAGE_MISSING' and '/' in i['node'] for i in nested_report['issues'])
    report={'checks':checks,'passed':all(checks.values()),'cases':results,'elapsed_s':time.perf_counter()-started,'fixture':fixture,
        'focal':{'normal':normal_report,'black_normal':black_report,'wrong_channel':miswired_report,'nested_group':nested_report},
        'scope':'Blender real, payload build_code+safe_mode local; no socket ni MCP transport aquí'}
    write_json(folder/'raw.json', all_raw)
    write_json(folder/'report.json', report)
    print('M9_INSPECT_REPORT',folder/'report.json',json.dumps(checks))

    if not all(checks.values()):
        raise AssertionError('El probe M9 falló; consultar el report.json conservado')


if __name__ == '__main__':
    main()
