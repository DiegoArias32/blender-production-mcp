"""Complex-skin support over real MCP stdio against a GUI Blender owned by this run (never port 9876).

    python tests/integration/run_skin_mcp.py --blender "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"

Builds a controlled fixture (skin-like head, a large layered material, a per-loop UV .npy) whose saved UI shows a
Python console where the 3D view was, the situation that broke the GlobalSkin capture. Then it drives the
production server: inventory, viewport inspect/capture/configure/restore with area conversion, comparable
signatures, typed UV import with its refusals, and paged inspection of the large material. The fixture file on
disk must be unchanged at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from run_m5_mcp import Run, session, listening
from blender_mcp.production.common.storage import sha256
from blender_mcp.production.skin import NAMES

ROOT = Path(__file__).resolve().parents[2]
BUILD = r'''
import bpy, json, numpy as np
bpy.ops.wm.read_factory_settings(use_empty=False)
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
bpy.ops.mesh.primitive_uv_sphere_add(segments=96, ring_count=48, radius=0.12, location=(0, 0, 1.6))
head = bpy.context.active_object; head.name = 'SkinHead'
bpy.ops.object.shade_smooth()
skin = bpy.data.materials.new('Skin'); skin.use_nodes = True
bsdf = next(n for n in skin.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
bsdf.inputs['Base Color'].default_value = (0.8, 0.55, 0.45, 1)
bsdf.inputs['Subsurface Weight'].default_value = 0.3
bsdf.inputs['Roughness'].default_value = 0.45
head.data.materials.append(skin)
big = bpy.data.materials.new('GlobalSkinLike'); big.use_nodes = True
tree = big.node_tree; prev = None
for i in range(320):
    node = tree.nodes.new('ShaderNodeMath'); node.operation = 'MULTIPLY_ADD'
    if prev is not None:
        tree.links.new(prev.outputs[0], node.inputs[0])
    prev = node
head.data.materials.append(big)
bpy.ops.object.camera_add(location=(0, -1.2, 1.65), rotation=(1.5708, 0, 0))
loops = len(head.data.loops)
i = np.arange(loops, dtype=np.float64)
np.save(output + '/uv_source.npy', np.stack([np.mod(i * 0.618, 3.0), np.sin(i) * 0.5 + 0.5], axis=1).astype(np.float32))
screen = bpy.data.screens['Layout']
view = next(a for a in screen.areas if a.type == 'VIEW_3D')
view.type = 'CONSOLE'
areas = [a.type for a in screen.areas]
bpy.ops.wm.save_as_mainfile(filepath=output + '/skin_fixture.blend')
json.dump({'path': output + '/skin_fixture.blend', 'loops': loops, 'areas': areas,
           'console_index': areas.index('CONSOLE')}, open(output + '/skin_fixture.json', 'w'))
'''


def png_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def rejection(run, name, arguments, code):
    result = await run.call(name, arguments)
    passed = result.get('status') == 'error' and code in json.dumps(result.get('errors'), default=str)
    run.check('safe_rejection_' + code, passed, response=result.get('errors'))
    return passed


async def scenario(run, fixture, info):
    inventory = await run.client.list_tools()
    tools = {t.name: t for t in inventory.tools}
    run.check('inventory_includes_skin_tools', {'production.' + n for n in NAMES} <= set(tools), total=len(tools))
    run.check('viewport_inspect_is_read_only', tools['production.viewport_inspect'].annotations.readOnlyHint is True)
    await run.good('asset_register_fixture', {'path': str(fixture)})
    inspected = await run.good('viewport_inspect')
    run.check('gui_without_view3d_detected', inspected['gui'] and not any(a['type'] == 'VIEW_3D' for a in inspected['areas']),
              areas=[a['type'] for a in inspected['areas']], hdris=inspected['studio_lights'])
    hdri = 'forest.exr' if 'forest.exr' in inspected['studio_lights'] else inspected['studio_lights'][0]
    other = next(h for h in inspected['studio_lights'] if h != hdri)
    console = next(a['index'] for a in inspected['areas'] if a['type'] == 'CONSOLE')
    spec = {'shading': {'type': 'MATERIAL', 'studio_light': hdri, 'rotate_z_deg': 30, 'intensity': 1.0,
                        'background_alpha': 0.0},
            'view': {'perspective': 'PERSP', 'orientation': 'FRONT', 'target': {'object': 'SkinHead'},
                     'distance': 'fit', 'fit_margin': 1.3, 'lens_mm': 85},
            'overlays': False, 'show_gizmo': False}
    await rejection(run, 'viewport_capture', {'spec': spec}, 'VIEWPORT_NO_VIEW3D')
    await rejection(run, 'viewport_capture', {'spec': {**spec, 'area_index': console}}, 'VIEWPORT_AREA_NOT_VIEW3D')
    converted = await run.good('viewport_capture', {'spec': {**spec, 'area_index': console, 'allow_area_conversion': True},
                                                    'max_size': 768})
    run.check('converted_capture_written_and_area_restored', converted['files'] and converted['state_restored']
              and converted['after_type'] == 'CONSOLE', file=converted['files'], signature=converted['signature'])
    after = await run.good('viewport_inspect')
    run.check('console_back_after_capture', [a['type'] for a in after['areas']] == [a['type'] for a in inspected['areas']])
    configured = await run.good('viewport_configure', {'spec': {**spec, 'area_index': console, 'allow_area_conversion': True}})
    run.check('configure_converted_area_persistently', configured['previous_area_type'] == 'CONSOLE'
              and configured['state']['shading']['studio_light'] == hdri, signature=configured['signature'])
    run.check('configure_signature_equals_capture_signature', configured['signature'] == converted['signature'])
    first = await run.good('viewport_capture', {'spec': spec, 'max_size': 768})
    second = await run.good('viewport_capture', {'spec': spec, 'max_size': 768})
    run.check('same_spec_same_signature', first['signature'] == second['signature'] == configured['signature'])
    run.check('same_spec_restores_configured_state', first['state_restored'] and second['state_restored'])
    run.report['capture_png_sha256'] = {'converted': png_digest(converted['files'][0]['path']),
                                        'first': png_digest(first['files'][0]['path']), 'second': png_digest(second['files'][0]['path'])}
    changed = await run.good('viewport_capture', {'spec': {**spec, 'shading': {**spec['shading'], 'studio_light': other}}, 'max_size': 768})
    run.check('different_hdri_changes_signature', changed['signature'] != first['signature'], hdri=other)
    side = await run.good('viewport_capture', {'spec': {**spec, 'view': {**spec['view'], 'orientation': 'THREE_QUARTER_LEFT'}}, 'max_size': 768})
    run.check('orientation_changes_signature', side['signature'] != first['signature'])
    run.report['captures'] = {k: v['files'][0]['path'] for k, v in (('converted', converted), ('first', first), ('second', second),
                                                                    ('other_hdri', changed), ('three_quarter', side))}
    await rejection(run, 'viewport_capture', {'spec': {**spec, 'shading': {**spec['shading'], 'studio_light': 'nope.exr'}}},
                    'VIEWPORT_HDRI_MISSING')
    await rejection(run, 'viewport_capture', {'spec': {**spec, 'view': {**spec['view'], 'target': {'object': 'Missing'}}}},
                    'VIEWPORT_TARGET_MISSING')
    await rejection(run, 'viewport_configure', {'spec': {'shading': {'type': 'MAGIC'}}}, 'VIEWPORT_SPEC_INVALID')
    restored = await run.good('viewport_restore', {'restore_record': configured['restore_record']})
    final = await run.good('viewport_inspect')
    run.check('restore_brings_console_back', restored['restored_type'] == 'CONSOLE'
              and [a['type'] for a in final['areas']] == [a['type'] for a in inspected['areas']])

    npy = str(Path(info['path']).parent / 'uv_source.npy')
    analysis = await run.good('mesh_import_uv_layer', {'object_name': 'SkinHead', 'source_path': npy, 'layer_name': 'UV_GlobalSkin'})
    run.check('uv_analyze_ready', analysis['ready'] and analysis['source']['loops'] == info['loops'])
    imported = await run.good('mesh_import_uv_layer', {'object_name': 'SkinHead', 'source_path': npy, 'layer_name': 'UV_GlobalSkin',
                                                      'apply': True, 'operation_id': 'skin-mcp-uv-' + uuid4().hex[:8]})
    verification = imported['verification']
    run.check('uv_import_verified', all(v is True or (isinstance(v, dict) and all(v.values())) for v in verification.values()),
              verification=verification, carrier=imported['carrier'])
    await rejection(run, 'mesh_import_uv_layer', {'object_name': 'SkinHead', 'source_path': npy, 'layer_name': 'UV_GlobalSkin',
                                                  'apply': True}, 'UV_LAYER_EXISTS')
    await rejection(run, 'mesh_import_uv_layer', {'object_name': 'SkinHead', 'source_path': 'relative.npy', 'layer_name': 'UV_X'},
                    'UV_SOURCE_PATH')
    await rejection(run, 'mesh_remove_imported_uv_layer', {'object_name': 'SkinHead', 'layer_name': 'UVMap'}, 'UV_LAYER_NOT_IMPORTED')
    removed = await run.good('mesh_remove_imported_uv_layer', {'object_name': 'SkinHead', 'layer_name': 'UV_GlobalSkin'})
    run.check('imported_uv_removed', 'UV_GlobalSkin' not in removed['after']['uv_layers'])

    await rejection(run, 'material_inspect', {'object_name': 'SkinHead', 'material_name': 'GlobalSkinLike'}, 'MATERIAL_GRAPH_CAPACITY')
    paged = await run.good('material_inspect', {'object_name': 'SkinHead', 'material_name': 'GlobalSkinLike',
                                                'options': {'graph_mode': 'paged', 'limit': 50, 'detail': 'nodes'}})
    run.check('paged_material_over_mcp', paged['stats']['nodes'] == 322 and paged['pagination']['nodes_has_more']
              and paged['analysis_scope'] == 'PAGED_INSPECTION', stats=paged['stats'])


async def main_async(args, folder, fixture, info):
    run = Run(folder)
    with (folder / 'server-stderr.txt').open('w', encoding='utf8') as log:
        async with session(folder, args.port, log) as client:
            run.client = client
            try:
                await scenario(run, fixture, info)
            except Exception as exc:
                run.report['exception'] = repr(exc)[:2000]
                print('EXCEPTION', repr(exc)[:1000], flush=True)
    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--port', type=int, default=9877)
    args = parser.parse_args()
    if args.port == 9876 or not 1024 <= args.port <= 65535:
        raise SystemExit('Refusing reserved/invalid port; use a dedicated loopback test port')
    if listening(args.port):
        raise SystemExit('Port already in use; no existing Blender is stopped or contacted')
    folder = Path(tempfile.gettempdir()) / ('bpm10skin-' + uuid4().hex[:8])
    (folder / 'fixture').mkdir(parents=True)
    expression = 'output=' + repr((folder / 'fixture').as_posix()) + '\n' + BUILD
    built = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python-expr', expression],
                           capture_output=True, text=True, encoding='utf8', errors='replace')
    (folder / 'fixture-build.log').write_text(built.stdout + built.stderr, encoding='utf8')
    info = json.loads((folder / 'fixture' / 'skin_fixture.json').read_text())
    fixture = Path(info['path'])
    digest = sha256(fixture)
    with (folder / 'blender-stdout.txt').open('w', encoding='utf8') as blender_log:
        blender = subprocess.Popen([args.blender, '--factory-startup', '--disable-autoexec', str(fixture), '--python',
                                    str(ROOT / 'tests' / 'integration' / 'm4_own_blender_start.py'), '--', str(args.port)],
                                   stdout=blender_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 90
            while not listening(args.port):
                if time.time() > deadline or blender.poll() is not None:
                    raise SystemExit('Own controlled Blender did not start the add-on')
                time.sleep(.5)
            run = asyncio.run(main_async(args, folder, fixture, info))
        finally:
            blender.terminate()
            try:
                blender.wait(timeout=30)
            except subprocess.TimeoutExpired:
                blender.kill()
    run.report['fixture_file_unchanged'] = sha256(fixture) == digest
    run.report['blender_pid_started_and_closed_here'] = blender.pid
    run.report['blender_port'] = args.port
    run.report['status'] = 'pass' if 'exception' not in run.report and run.report['fixture_file_unchanged'] and \
        all(c['passed'] for c in run.report['checks'].values()) else 'fail'
    run.save()
    archive = ROOT / 'test-artifacts' / 'm10' / 'skin' / folder.name
    archive.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(folder / 'report.json', archive / 'report.json')
    print('SKIN_MCP_REPORT=' + str(archive / 'report.json') + ' workdir=' + str(folder) + ' status=' + run.report['status'])
    raise SystemExit(0 if run.report['status'] == 'pass' else 1)


if __name__ == '__main__':
    main()
