"""Real M5 stdio/socket acceptance against a generated Blender instance owned by this run.

Never contacts port 9876, alters preferences or opens a production asset. This runner checks every
new M5 tool schema/call and positive candidate/transfer/UV/UDIM/triangulation/bake cycles. Subsystem
geometry accuracy and larger fixtures are covered by the independent real Blender domain probes.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from blender_mcp.production.common.storage import sha256
from blender_mcp.production.common.bridge import Bridge

ROOT = Path(__file__).resolve().parents[2]
NEW_TOOLS = {
    'retopology_analyze', 'retopology_analyze_density', 'retopology_priority_map', 'retopology_budget',
    'retopology_plan', 'retopology_create_candidate', 'retopology_optimize_region', 'retopology_project_to_source',
    'retopology_compare_source', 'retopology_compare_silhouette', 'retopology_validate',
    'retopology_analyze_deformation_regions', 'retopology_transfer_weights', 'retopology_validate_weights',
    'retopology_analyze_morph_transfer', 'retopology_transfer_morphs', 'retopology_transfer_attributes',
    'game_mesh_prepare_normals', 'game_mesh_triangulation_preview', 'game_mesh_validate_triangulation',
    'game_mesh_validate_tangents', 'uv_analyze', 'uv_generate', 'uv_validate', 'uv_analyze_texel_density',
    'udim_plan', 'udim_apply_plan', 'uv_pack', 'bake_prepare_pairs', 'bake_generate_cage',
    'bake_analyze_ray_distance', 'bake_validate', 'bake_smoke_test', 'game_mesh_validate', 'game_mesh_prepare_package',
}

BUILD = r'''
import bpy,json
from pathlib import Path
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2)
high=bpy.context.object;high.name='M5_MCP_HIGH'
for v in high.data.vertices:v.co*=1.02
for l in list(high.data.uv_layers):high.data.uv_layers.remove(l)
high.data.update()
for i in range(6):high.data.materials.append(bpy.data.materials.new('M5_Semantic_'+str(i)))
for p in high.data.polygons:p.material_index=p.index
color=high.data.color_attributes.new(name='RegionColor',type='FLOAT_COLOR',domain='POINT')
for v in high.data.vertices:color.data[v.index].color=((v.co.x/1.02+1)/2,(v.co.y/1.02+1)/2,(v.co.z/1.02+1)/2,1)
high['semantic_role']='synthetic_bake_cube'
low=high.copy();low.data=high.data.copy();low.name='M5_MCP_LOW';bpy.context.scene.collection.objects.link(low)
for v in low.data.vertices:v.co/=1.02
low.data.update();low.hide_render=True
armature=bpy.data.armatures.new('M5_MCP_RigData');rig=bpy.data.objects.new('M5_MCP_Rig',armature)
bpy.context.scene.collection.objects.link(rig)
for o in bpy.context.selected_objects:o.select_set(False)
rig.select_set(True);bpy.context.view_layer.objects.active=rig;bpy.ops.object.mode_set(mode='EDIT')
bone=armature.edit_bones.new('ExplicitRoot');bone.head=(4,0,0);bone.tail=(4,0,1)
bpy.ops.object.mode_set(mode='OBJECT')
weighted=[]
for label in ('HIGH','LOW'):
 obj=low.copy();obj.data=low.data.copy();obj.name='M5_WEIGHT_'+label;obj.location.x=4
 bpy.context.scene.collection.objects.link(obj)
 mod=obj.modifiers.new('FixtureArmature','ARMATURE');mod.object=rig
 group=obj.vertex_groups.new(name='ExplicitRoot');group.add(list(range(len(obj.data.vertices))),1.0,'REPLACE')
 obj.hide_render=True;weighted.append(obj.name)
rig.hide_render=True
camera_data=bpy.data.cameras.new('M5_MCP_CameraData');camera=bpy.data.objects.new('M5_MCP_Camera',camera_data)
bpy.context.scene.collection.objects.link(camera);camera.location=(4,-6,4)
camera.rotation_euler=(-camera.location).to_track_quat('-Z','Y').to_euler();camera_data.type='ORTHO';camera_data.ortho_scale=4
bpy.context.scene.camera=camera
light_data=bpy.data.lights.new('M5_MCP_LightData','AREA');light=bpy.data.objects.new('M5_MCP_Light',light_data)
bpy.context.scene.collection.objects.link(light);light.location=(1,-3,5);light_data.energy=500;light_data.size=4
bpy.context.scene.render.engine='BLENDER_EEVEE';bpy.context.scene.frame_start=1;bpy.context.scene.frame_end=1
bpy.ops.wm.save_as_mainfile(filepath=str(output/'fixture.blend'))
(output/'fixture.json').write_text(json.dumps({'path':str(output/'fixture.blend'),'high':high.name,'low':low.name,
 'weighted_high':weighted[0],'weighted_low':weighted[1],'rig':rig.name,'triangles':12}),encoding='utf8')
'''


def listening(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(.2)
        return probe.connect_ex(('127.0.0.1', port)) == 0


@asynccontextmanager
async def session(folder, port, log):
    env = dict(os.environ, BLENDER_HOST='localhost', BLENDER_PORT=str(port), BLENDER_MCP_SAFE_MODE='1',
               DISABLE_TELEMETRY='true', BLENDER_PRODUCTION_WORKDIR=str(folder/'s'),
               BLENDER_PRODUCTION_FIXTURE_ROOTS=str(folder), PYTHONIOENCODING='utf-8')
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'blender_mcp.production.server'], env=env)
    async with stdio_client(parameters, errlog=log) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


class Run:
    def __init__(self, folder):
        self.folder = folder
        self.client = None
        self.report = {'status': 'running', 'checks': {}, 'calls': [], 'production_assets_used': False}

    def save(self):
        (self.folder/'report.json').write_text(json.dumps(self.report, indent=2, ensure_ascii=False, default=str), encoding='utf8')

    def check(self, name, passed, **details):
        self.report['checks'][name] = {'passed': bool(passed), **details}
        self.save()
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)

    async def call(self, name, arguments=None):
        entry = {'tool': 'production.'+name, 'arguments': arguments or {}}
        self.report['calls'].append(entry)
        started = time.perf_counter()
        response = await asyncio.wait_for(self.client.call_tool(entry['tool'], entry['arguments']), timeout=300)
        envelope = (response.structuredContent or {}).get('result')
        entry.update(elapsed_ms=round((time.perf_counter()-started)*1000, 2),
                     status=envelope.get('status') if isinstance(envelope, dict) else 'unstructured',
                     errors=envelope.get('errors') if isinstance(envelope, dict) else str(response.content)[:500],
                     payload_bytes=len(json.dumps(envelope, default=str).encode()) if envelope else None)
        self.save()
        if not isinstance(envelope, dict):
            raise AssertionError('Unstructured MCP result: '+name)
        return envelope

    async def good(self, name, arguments=None):
        result = await self.call(name, arguments)
        self.check(name+'_'+str(len(self.report['calls'])), result['status'] != 'error', errors=result.get('errors'))
        if result['status'] == 'error':
            raise AssertionError(name+': '+json.dumps(result['errors'])[:1000])
        return result['data']

    async def cycle(self, name, relationship, options=None, preview=False):
        proposed = await self.good(name, {'relationship': relationship, 'mode': 'propose', 'options': options or {}})
        self.check(name+'_supported', proposed['proposal']['supported'], proposal=proposed['proposal'])
        if not proposed['proposal']['supported']:
            raise AssertionError(name+' proposal unsupported')
        if preview:
            candidate = await self.good(name, {'mode': 'preview', 'operation': proposed['operation']})
            self.check(name+'_preview_numeric', candidate.get('numeric_verification') == 'pass')
        applied = await self.good(name, {'mode': 'apply', 'operation': proposed['operation']})
        verified = await self.good(name, {'mode': 'verify', 'operation': proposed['operation']})
        self.check(name+'_apply_verify', applied.get('numeric_verification') == 'pass' and verified.get('numeric_verification') == 'pass'
                   and applied.get('source_intact') is True, metrics=applied.get('metrics'), candidate=applied.get('candidate'))
        return applied, proposed


async def scenario(run, fixture, info):
    inventory = await run.client.list_tools()
    names = {t.name for t in inventory.tools}
    wanted = {'production.'+name for name in NEW_TOOLS}
    run.check('inventory_M4_retained_all_M5', len(names) == 120 and wanted <= names,
              total=len(names), new_count=len(wanted), missing=sorted(wanted-names))
    run.report['tool_inventory'] = sorted(names)
    await run.good('asset_register_fixture', {'path': str(fixture)})
    status = await run.good('asset_status')
    run.check('fixture_role', status['asset']['role'] == 'FIXTURE')
    audited = await run.good('scene_audit')
    run.report['scene_audit'] = audited
    high = info['high']
    retopo = {'target_triangles': 12, 'minimum_triangles': 12, 'maximum_triangles': 12,
              'strategy': 'PRESERVE_AND_OPTIMIZE', 'max_distance_m': .05, 'max_normal_angle_degrees': 80,
              'maximum_silhouette_distance_m': .1, 'minimum_silhouette_iou': .94, 'silhouette_resolution': 128, 'samples': 128}
    inspected = await run.good('retopology_analyze', {'object_name': high, 'options': retopo})
    for name in ('retopology_analyze_density', 'retopology_priority_map', 'retopology_budget', 'retopology_plan'):
        await run.good(name, {'object_name': high, 'options': retopo})
    creation = {'object_name': high, 'source_fingerprint': inspected['signature']['fingerprint'], 'options': {'target_object': info['low']},
                'operation_id': 'm5-mcp-candidate-'+uuid4().hex[:8]}
    created_envelope = await run.call('retopology_create_candidate', creation)
    retry = await run.call('retopology_create_candidate', creation)
    created = created_envelope['data']
    run.check('candidate_idempotent', created_envelope['status'] != 'error' and created_envelope['execution_id'] == retry['execution_id']
              and created['candidate'] == retry['data']['candidate'])
    relation = created['relationship']
    optimized, optimize_plan = await run.cycle('retopology_optimize_region', relation, retopo, preview=True)
    relation = optimized['relationship']
    for name in ('retopology_compare_source', 'retopology_compare_silhouette', 'retopology_validate'):
        await run.good(name, {'relationship': relation, 'options': retopo})
    projected, projection_plan = await run.cycle('retopology_project_to_source', relation, retopo)
    reverted = await run.good('retopology_project_to_source', {'mode': 'revert', 'operation': projection_plan['operation']})
    run.check('project_revert_retains_authoring_and_child', reverted['reverted'] and reverted['candidate_preserved'] == projected['candidate'])
    await run.good('retopology_analyze_deformation_regions', {'relationship': relation})
    await run.good('retopology_analyze_morph_transfer', {'relationship': relation})
    morphs, _ = await run.cycle('retopology_transfer_morphs', relation)
    relation = morphs['relationship']
    attributes = {'attributes': ['RegionColor'], 'max_distance_m': .05,
                  'custom_properties': {'object': ['semantic_role'], 'mesh': []}}
    transferred, _ = await run.cycle('retopology_transfer_attributes', relation, attributes)
    relation = transferred['relationship']

    # Weight tools receive a separate explicitly related, rigged fixture; the bake demo stays static.
    weighted = await run.good('retopology_analyze', {'object_name': info['weighted_high']})
    weight_created = await run.good('retopology_create_candidate', {'object_name': info['weighted_high'],
                        'source_fingerprint': weighted['signature']['fingerprint'], 'options': {'target_object': info['weighted_low']}})
    weight_result, _ = await run.cycle('retopology_transfer_weights', weight_created['relationship'], {'groups': ['ExplicitRoot'], 'influence_limit': 1})
    await run.good('retopology_validate_weights', {'relationship': weight_result['relationship'], 'options': {'groups': ['ExplicitRoot'], 'influence_limit': 1}})

    normals, _ = await run.cycle('game_mesh_prepare_normals', relation)
    relation = normals['relationship']
    uv_settings = {'padding_px': 4, 'resolution': 256}
    missing = await run.good('uv_analyze', {'relationship': relation})
    run.check('missing_uv_not_valid', not missing['valid'])
    generated, _ = await run.cycle('uv_generate', relation, uv_settings, preview=True)
    relation = generated['relationship']
    for name in ('uv_validate', 'uv_analyze_texel_density'):
        await run.good(name, {'relationship': relation, 'options': uv_settings})
    assignments = [{'region': 'region_'+str(i), 'tile': 1001+i//2, 'material_index': i} for i in range(6)]
    udim_options = dict(uv_settings, assignments=assignments)
    tiles = await run.good('udim_plan', {'relationship': relation, 'options': udim_options})
    run.check('three_udim_tiles_planned', tiles['tiles'] == [1001, 1002, 1003])
    udim, _ = await run.cycle('udim_apply_plan', relation, udim_options)
    packed, _ = await run.cycle('uv_pack', udim['relationship'], uv_settings)
    relation = packed['relationship']
    await run.good('game_mesh_validate_triangulation', {'relationship': relation})
    triangulated, _ = await run.cycle('game_mesh_triangulation_preview', relation)
    relation = triangulated['relationship']
    tangents = await run.good('game_mesh_validate_tangents', {'relationship': relation})
    run.check('tangent_basis_actual', tangents['passed'])
    bake_options = dict(uv_settings, front_distance_m=.09, rear_distance_m=.09, extrusion_m=.08,
                        max_cage_offset_m=.1, max_surface_deviation_m=.05)
    await run.good('bake_analyze_ray_distance', {'relationship': relation, 'options': bake_options})
    cage, _ = await run.cycle('bake_generate_cage', relation, bake_options)
    bake_options['cage_object'] = cage['candidate']
    ready = await run.good('bake_validate', {'relationship': relation, 'options': bake_options})
    run.check('bake_ready', ready['passed'] is True and not ready['errors'] and
              ready['readiness'] in ('READY','READY_WITH_WARNINGS'), report=ready)
    pair = await run.good('bake_prepare_pairs', {'relationship': relation, 'options': bake_options})
    smoke = await run.good('bake_smoke_test', {'pair': pair['pair'], 'allow_cpu_fallback': True,
                           'options': {'smoke_resolution': 64, 'bake_samples': 2, 'maps': ['NORMAL', 'AO']}})
    run.check('real_NORMAL_AO_smoke_three_tiles', smoke['passed'] and smoke['state_restored'] and smoke['source_intact']
              and len(smoke['artifacts']) == 6 and all(Path(a['path']).is_file() for a in smoke['artifacts']), artifacts=smoke['artifacts'])
    validation_options = {'retopology': retopo, 'attributes': attributes, 'uv': uv_settings, 'bake': bake_options}
    validated = await run.good('game_mesh_validate', {'relationship': relation, 'options': validation_options})
    run.check('aggregate_game_mesh_concrete_gates', validated['passed'], subsystems=validated['subsystems'])
    package = await run.good('game_mesh_prepare_package', {'relationship': relation, 'bake_pair': pair['pair'],
                     'triangulated_relationship': relation, 'options': validation_options, 'dry_run': True})
    run.check('package_dry_run_neutral_handoff', package['dry_run'] and package['handoff_plan']['application'] == 'neutral')
    capture = await run.good('qa_capture_viewport', {'max_size': 640})
    run.report['viewport_capture'] = capture

    # Negative target mutation is performed on a controlled copy through an inherited safe tool.
    # This stale source fingerprint is independent of a source name and never issues a mutation.
    stale = await run.call('retopology_create_candidate', {'object_name': high, 'source_fingerprint': 'wrong-fingerprint'})
    run.check('wrong_source_fingerprint_rejected', stale['status'] == 'error' and 'STALE_SIGNATURE' in json.dumps(stale['errors']))
    invoked = {entry['tool'] for entry in run.report['calls']}
    run.check('all_35_new_tools_invoked', wanted <= invoked, missing=sorted(wanted-invoked), calls=len(run.report['calls']))
    run.report['final_relationship'] = relation
    run.report['master_negative_operation'] = optimize_plan['operation']


async def main_async(args, folder, fixture, info):
    run = Run(folder)
    with (folder/'server-stderr.txt').open('w', encoding='utf8') as log:
        async with session(folder, args.port, log) as client:
            run.client = client
            try:
                await scenario(run, fixture, info)
            except Exception as exc:
                run.report['exception'] = repr(exc)[:2000]
                print('EXCEPTION', repr(exc)[:1000], flush=True)
    if 'exception' not in run.report:
        # The FIXTURE role cannot be relabelled MASTER. After the first stdio controller exits,
        # one short, independent bridge command saves/opens a new owned fixture copy. It then
        # disconnects before starting the second stdio controller. There are never two controllers.
        master = folder/'fixture'/'master-negative.blend'
        direct = Bridge(host='localhost', port=args.port)
        try:
            opened = direct.run("import bpy\n"
                "if bpy.data.filepath != params['fixture']:\n    raise ValueError('OWN_FIXTURE_REQUIRED')\n"
                "original=bpy.data.filepath\n"
                "bpy.ops.wm.save_as_mainfile(filepath=params['path'],copy=True)\n"
                "bpy.ops.wm.open_mainfile(filepath=params['path'],load_ui=False,use_scripts=False)\n"
                "result={'opened':bpy.data.filepath==params['path'],'original':original}",
                {'fixture': str(fixture), 'path': str(master)})
        finally:
            direct.close()
        run.check('master_negative_is_generated_new_copy', opened['opened'] and master.is_file())
        with (folder/'master-server-stderr.txt').open('w', encoding='utf8') as log:
            async with session(folder, args.port, log) as client:
                run.client = client
                await run.good('asset_register_master', {'confirm_master': True})
                forbidden = await run.call('retopology_optimize_region', {'mode': 'apply',
                    'operation': run.report['master_negative_operation'], 'operation_id': 'master-negative-'+uuid4().hex[:8]})
                run.check('MASTER_mutation_rejected', forbidden['status'] == 'error' and 'MASTER_ASSET_READ_ONLY' in json.dumps(forbidden['errors']))
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
    folder = ROOT/'test-artifacts'/'m5'/('mcp-'+uuid4().hex[:8])
    (folder/'fixture').mkdir(parents=True)
    expression = 'from pathlib import Path\noutput=Path('+repr(str(folder/'fixture'))+')\n'+BUILD
    subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python-expr', expression],
                   check=True, capture_output=True, text=True, encoding='utf8', errors='replace')
    info = json.loads((folder/'fixture'/'fixture.json').read_text())
    fixture = Path(info['path'])
    digest = sha256(fixture)
    with (folder/'blender-stdout.txt').open('w', encoding='utf8') as blender_log:
        blender = subprocess.Popen([args.blender, '--factory-startup', '--disable-autoexec', str(fixture), '--python',
                    str(ROOT/'tests'/'integration'/'m4_own_blender_start.py'), '--', str(args.port)],
                    stdout=blender_log, stderr=subprocess.STDOUT)
        try:
            deadline = time.time()+90
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
    run.report['status'] = 'pass' if 'exception' not in run.report and run.report['fixture_file_unchanged'] and all(c['passed'] for c in run.report['checks'].values()) else 'fail'
    run.save()
    print('M5_MCP_REPORT='+str(folder/'report.json')+' status='+run.report['status'])
    raise SystemExit(0 if run.report['status'] == 'pass' else 1)


if __name__ == '__main__':
    main()
