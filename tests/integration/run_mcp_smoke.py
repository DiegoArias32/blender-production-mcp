"""Explicit GUI/stdIO integration test, only for generated M1 fixtures.

Run with repository .venv Python and --fixture tests-artifacts/...blend.
Saves recovery before switching files. Refuses a live non-test project.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.storage import Storage, sha256
from blender_mcp.production.checkpoint import create_checkpoint
from blender_mcp.production.inspect import snapshot

REPO = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def session(root):
    env = dict(os.environ, BLENDER_HOST='localhost', BLENDER_PORT='9876',
               BLENDER_MCP_SAFE_MODE='1', DISABLE_TELEMETRY='true',
               BLENDER_PRODUCTION_WORKDIR=str(root), PYTHONIOENCODING='utf-8')
    params = StdioServerParameters(command=sys.executable,
        args=['-m','blender_mcp.production.server'], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


async def run(fixture):
    fixture = fixture.resolve(strict=True)
    if not fixture.is_relative_to(REPO/'test-artifacts') or fixture.name != 'm1-controlled-fixtures.blend':
        raise ValueError('Only locally generated test-artifacts M1 fixtures are accepted')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    root = REPO/'test-artifacts'/('mcp-'+stamp)
    root.mkdir()
    store = Storage(root/'store')
    report = {'fixture':str(fixture), 'fixture_hash':sha256(fixture), 'checks':[], 'results':[]}
    bridge = Bridge()
    try:
        before = snapshot(bridge)
        source = before.get('source_file')
        if source and not any(Path(source).resolve().is_relative_to(REPO/p) for p in ('.production','test-artifacts')):
            raise ValueError('Refusing to switch away from a non-test live project')
        report['recovery'] = create_checkpoint(bridge, store, 'Before M1 GUI fixture test', 'm1-e2e','load synthetic fixture')
        working = store.reserve('working','m1-e2e')/'scene.blend'
        bridge.run('''
import bpy
bpy.ops.wm.open_mainfile(filepath=params["fixture"], load_ui=False, use_scripts=False)
bpy.ops.wm.save_as_mainfile(filepath=params["working"], relative_remap=True)
result = {"file":bpy.data.filepath}
''', {'fixture':str(fixture),'working':str(working)})
    finally:
        bridge.close()

    async def call(client, name, arguments=None, expected_error=False):
        response = await client.call_tool('production.'+name, arguments or {})
        if response.isError:
            raise AssertionError(str(response.content))
        data = response.structuredContent['result']
        report['results'].append({'tool':name, 'response':data})
        (root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        assert bool(data['status']=='error') == expected_error, data
        assert data['operation_id'] and data['metrics']['elapsed_ms'] >= 0
        print(name, data['status'], data['metrics'], flush=True)
        return data['data'] if not expected_error else data

    try:
        async with session(store.root) as client:
            tools = await client.list_tools()
            expected={'scene_audit','create_checkpoint','restore_checkpoint','diff_scene','inspect_armature',
                'rig_analyze','animation_analyze','animation_detect_foot_sliding',
                'animation_detect_quaternion_flips','qa_capture_viewport','qa_render_preview'}
            assert {'production.'+name for name in expected} <= {item.name for item in tools.tools}
            report['checks'].append('All 11 M1 production tools retained via MCP stdio initialize/list_tools')
            audit = await call(client,'scene_audit')
            assert audit['scene']['name'] == 'M1_ControlledFixtures'
            assert audit['scene']['fps'] == 30
            await call(client,'scene_audit',{'limit':2,'offset':1})
            armature = await call(client,'inspect_armature',{'armature':'M1_Rig','limit':5})
            rig = await call(client,'rig_analyze',{'armature':'M1_Rig'})
            report['checks'].append('scene / armature / rig real Blender summaries')
            animation = await call(client,'animation_analyze',{'object_name':'M1_Rig'})
            feet = await call(client,'animation_detect_foot_sliding',{'armature':'M1_Rig','bones':['FootIK.L','FootIK.R'],'frame_start':1,'frame_end':10})
            markers = {item['bone']:item for item in feet['feet']}
            assert abs(markers['FootIK.L']['max_sliding_m']-0.018) < 1e-5
            assert abs(markers['FootIK.R']['max_sliding_m']-0.0022) < 1e-5
            assert markers['FootIK.L']['status']=='fail'
            assert markers['FootIK.R']['status']=='pass'
            report['checks'].append('foot sliding 1.8 cm fail vs 0.22 cm pass')
            flips = await call(client,'animation_detect_quaternion_flips',{'object_name':'M1_Rig'})
            assert 'hemisphere_discontinuity_candidate' in json.dumps(flips)
            report['checks'].append('raw quaternion sign flip detected')
            await call(client,'animation_detect_quaternion_flips',{'object_name':'M1_SlotProbe'})
            await call(client,'animation_analyze',{'object_name':'M1_Cube'})
            cp = await call(client,'create_checkpoint',{'description':'M1 checkpoint roundtrip','operation':'mcp integration'})
            cp_manifest = cp['manifest']
            cp_hash = sha256(Path(cp['path']))
            delta = await call(client,'diff_scene',{'checkpoint':cp_manifest})
            assert delta['counts']=={'added':0,'removed':0,'changed':0}
            await call(client,'restore_checkpoint',{'checkpoint':cp_manifest})
            viewport = await call(client,'qa_capture_viewport',{'max_size':640})
            assert viewport['state_restored'] is True and viewport['files']
            preview = await call(client,'qa_render_preview',{'frame_start':1,'frame_end':10,'step':9,'width':320,'height':180,'max_frames':2})
            assert preview['state_restored'] is True and len(preview['files'])==2
            assert all(f['width']==320 and f['height']==180 for f in preview['files'])
            report['checks'].append('actual viewport and 2 rendered PNGs; settings restored')
            await call(client,'inspect_armature',{'armature':'MISSING_ARMATURE'},expected_error=True)
        # No MCP process is alive while the deliberate test mutation occurs.
        bridge=Bridge()
        try:
            bridge.run('''
import bpy
obj = bpy.data.objects["M1_Cube"]
obj.location.x += 2.0
result = {"location":list(obj.location)}
''')
        finally:
            bridge.close()
        async with session(store.root) as client:
            delta=await call(client,'diff_scene',{'checkpoint':cp_manifest})
            assert 'M1_Cube' in delta['changed']
            restored=await call(client,'restore_checkpoint',{'checkpoint':cp_manifest,'dry_run':False})
            assert restored['working_path'] != cp['path']
            assert sha256(Path(cp['path'])) == cp_hash
            delta=await call(client,'diff_scene',{'checkpoint':cp_manifest})
            assert not delta['changed'] and not delta['added'] and not delta['removed']
            audit=await call(client,'scene_audit')
            assert audit['scene']['frame']==7
            report['checks'].append('diff detects test mutation; restore roundtrip; checkpoint immutable')
        assert sha256(fixture)==report['fixture_hash']
        report['checks'].append('synthetic fixture source unchanged')
        report['status']='PASS'
    finally:
        (root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print('MCP_REPORT='+str(root/'report.json'),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,required=True)
    asyncio.run(run(parser.parse_args().fixture))
