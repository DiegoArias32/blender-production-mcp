"""M4 acceptance through the real MCP stdio server and a Blender started for this run only.

1. A generated humanoid fixture is built in a background Blender (never a production file).
2. A separate GUI Blender opens it with the upstream add-on on a non-default port (default 9877);
   the user's Blender/controller on 9876 is never contacted.
3. `blender-production` runs over stdio with BLENDER_PORT pointing at that instance and exercises
   the M4 tools end to end, including an idempotent retry with the same operation_id.
4. The GUI Blender started here is closed; the fixture file on disk must be unchanged.
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

ROOT = Path(__file__).resolve().parents[2]
STRESS = [{'region': 'shoulder', 'bone': 'upper_arm.L', 'axis': 'Z', 'angle_degrees': 45},
          {'region': 'elbow', 'bone': 'forearm.L', 'axis': 'Z', 'angle_degrees': 60}]
INTENT = 'Increase upper-body mass and improve shoulder/back transition while preserving the face and rig dependencies'


def listening(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(('127.0.0.1', port)) == 0


@asynccontextmanager
async def session(folder: Path, port: int, log):
    environment = dict(os.environ, BLENDER_HOST='localhost', BLENDER_PORT=str(port), BLENDER_MCP_SAFE_MODE='1',
                       DISABLE_TELEMETRY='true', BLENDER_PRODUCTION_WORKDIR=str(folder / 's'), PYTHONIOENCODING='utf-8',
                       BLENDER_PRODUCTION_FIXTURE_ROOTS=str(folder))
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'blender_mcp.production.server'], env=environment)
    async with stdio_client(parameters, errlog=log) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


class Run:
    def __init__(self, folder):
        self.folder = folder
        self.report = {'status': 'running', 'checks': {}, 'calls': [], 'production_assets_used': False}
        self.client = None

    def save(self):
        (self.folder / 'report.json').write_text(json.dumps(self.report, indent=2, ensure_ascii=False, default=str), encoding='utf-8')

    def check(self, name, passed, **details):
        self.report['checks'][name] = dict({'passed': bool(passed)}, **details)
        self.save()
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)

    async def call(self, name, arguments=None):
        started = time.perf_counter()
        entry = {'tool': 'production.' + name, 'arguments': arguments or {}}
        self.report['calls'].append(entry)
        response = await asyncio.wait_for(self.client.call_tool(entry['tool'], arguments or {}), timeout=300)
        envelope = (response.structuredContent or {}).get('result')
        entry.update(elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                     status=envelope.get('status') if isinstance(envelope, dict) else 'unstructured',
                     errors=envelope.get('errors') if isinstance(envelope, dict) else str(response.content)[:500])
        self.save()
        if not isinstance(envelope, dict):
            raise AssertionError('unstructured result from ' + name)
        return envelope


async def scenario(run: Run, fixture: Path, info: dict):
    tools = await run.client.list_tools()
    names = {t.name for t in tools.tools}
    character = sorted(n for n in names if n.startswith('production.character_'))
    run.check('tools_listed', len(names) == 85 and len(character) == 20, total=len(names), character=character)
    body, rig = info['body'], info['rig']
    registered = await run.call('asset_register_fixture', {'path': str(fixture)})
    status = await run.call('asset_status')
    run.check('fixture_registered', registered['status'] == 'success' and status['data']['asset']['role'] == 'FIXTURE', role=status['data']['asset'].get('role'))
    inspected = await run.call('character_inspect', {'object_name': body})
    data = inspected['data']
    run.check('inspect', inspected['status'] in ('success', 'warning') and data['counts']['vertices'] == info['vertices'],
              counts=data['counts'], regions=len(data['regions']))
    defined = await run.call('character_define_regions', {'object_name': body})
    locked = await run.call('character_lock_regions', {'manifest': defined['data']['manifest'], 'lock': ['FACE', 'HEAD', 'HANDS'],
                                                       'editable': ['SHOULDERS', 'UPPER_BACK', 'TORSO']})
    manifest = locked['data']['manifest']
    run.check('regions', locked['status'] == 'success', manifest=manifest)
    for name, arguments in (('character_analyze_silhouette', {'object_name': body, 'manifest': manifest}),
                            ('character_analyze_proportions', {'object_name': body, 'manifest': manifest}),
                            ('character_analyze_asymmetry', {'object_name': body, 'manifest': manifest}),
                            ('character_analyze_surface', {'object_name': body, 'manifest': manifest}),
                            ('character_analyze_intersections', {'object_name': body, 'manifest': manifest}),
                            ('character_modeling_audit', {'object_name': body, 'manifest': manifest}),
                            ('character_validate_locked_regions', {'manifest': manifest})):
        result = await run.call(name, arguments)
        run.check(name, result['status'] in ('success', 'warning'), errors=result.get('errors'))
    plan = await run.call('character_redesign_plan', {'object_name': body, 'intent': INTENT, 'manifest': manifest})
    steps = [s for s in plan['data']['steps'] if s['status'] == 'proposed']
    run.check('plan_read_only', plan['status'] == 'success' and plan['data']['mutated'] is False and len(steps) == 3,
              goals=[s['goal'] for s in steps])
    session_plan = await run.call('character_redesign_session', {'object_name': body, 'intent': INTENT, 'manifest': manifest})
    run.check('session_default_no_mutation', session_plan['data']['mutated'] is False and 'candidate' not in session_plan['data'])
    fingerprint = data['mesh_signature']['fingerprint']
    key = 'm4-mcp-candidate-' + uuid4().hex[:8]
    first = await run.call('character_create_candidate', {'object_name': body, 'source_fingerprint': fingerprint, 'operation_id': key})
    again = await run.call('character_create_candidate', {'object_name': body, 'source_fingerprint': fingerprint, 'operation_id': key})
    candidate = first['data']['candidate']
    run.check('candidate_and_idempotent_retry', first['status'] == 'success' and again['execution_id'] == first['execution_id']
              and again['data']['candidate'] == candidate, first=first['execution_id'], retry=again['execution_id'],
              idempotency=again.get('idempotency'))
    proposal = await run.call('character_adjust_shape', {'object_name': candidate, 'mode': 'propose',
                                                         'options': {'regions_manifest': manifest, 'operation': steps[1]['operation']}})
    operation = proposal['data']['operation']
    run.check('propose', proposal['data']['proposal']['supported'], stats=proposal['data']['proposal']['stats'])
    preview = await run.call('character_adjust_shape', {'mode': 'preview', 'operation': operation})
    applied = await run.call('character_adjust_shape', {'mode': 'apply', 'operation': operation})
    verified = await run.call('character_adjust_shape', {'mode': 'verify', 'operation': operation})
    run.check('preview_apply_verify', preview['data'].get('numeric_verification') == 'pass'
              and applied['data'].get('numeric_verification') == 'pass' and verified['data'].get('numeric_verification') == 'pass'
              and applied['visual_verification'] == 'required', metrics=applied['data'].get('metrics'))
    regions = await run.call('character_validate_locked_regions', {'manifest': manifest, 'object_name': candidate})
    run.check('locked_regions_on_candidate', regions['data']['passed'], errors=regions['data'].get('errors'))
    locked_edit = await run.call('character_adjust_shape', {'object_name': candidate, 'mode': 'propose',
                                                            'options': {'regions_manifest': manifest,
                                                                        'operation': {'type': 'inflate', 'region': 'FACE', 'strength': 0.01}}})
    run.check('locked_edit_structured_error', locked_edit['status'] == 'error'
              and any('LOCKED_REGION_EDIT' in json.dumps(e) for e in locked_edit['errors']), error_codes=locked_edit.get('error_codes'))
    skin = await run.call('character_deformation_qa', {'rig': rig, 'source_mesh': body, 'candidate_mesh': candidate, 'stress_poses': STRESS})
    run.check('deformation_qa', skin['data']['passed'], poses=len(skin['data']['poses']), regressions=skin['data']['regressions'])
    compare = await run.call('character_compare_before_after', {'before_object': body, 'after_object': candidate, 'manifest': manifest})
    run.check('before_after_renders', compare['data'].get('status') == 'ok' and compare['data']['state_restored']
              and compare['data']['montage'] is not None, backend=compare['data'].get('render_backend'),
              montage=(compare['data'].get('montage') or {}).get('path'))
    reverted = await run.call('character_adjust_shape', {'mode': 'revert', 'operation': operation})
    run.check('revert', reverted['status'] != 'error' and reverted['data'].get('reverted') is True)


async def main_async(args, folder: Path, fixture: Path, info: dict):
    run = Run(folder)
    with (folder / 'server-stderr.txt').open('w', encoding='utf-8') as log:
        async with session(folder, args.port, log) as client:
            run.client = client
            try:
                await scenario(run, fixture, info)
            except Exception as exc:
                run.report['exception'] = repr(exc)[:2000]
                print('EXCEPTION', repr(exc)[:500], flush=True)
    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--port', type=int, default=9877)
    args = parser.parse_args()
    if args.port == 9876:
        raise SystemExit('Refusing 9876: reserved for the user\'s Blender/MCP controller')
    if listening(args.port):
        raise SystemExit('Port ' + str(args.port) + ' is already in use; this runner only talks to a Blender it starts itself')
    folder = ROOT / 'test-artifacts' / 'm4' / ('mcp-' + uuid4().hex[:8])
    (folder / 'fixture').mkdir(parents=True)
    expression = ('import sys, json; sys.path.insert(0, ' + repr(str(ROOT / 'tests' / 'blender')) + '); '
                  'import m4_character_fixtures as f; info = f.build(' + repr(str(folder / 'fixture')) + ", 'humanoid'); "
                  'open(' + repr(str(folder / 'fixture' / 'fixture.json')) + ", 'w').write(json.dumps(info))")
    subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python-expr', expression],
                   check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
    info = json.loads((folder / 'fixture' / 'fixture.json').read_text(encoding='utf-8'))
    fixture = Path(info['path'])
    digest = sha256(fixture)
    blender = subprocess.Popen([args.blender, '--factory-startup', '--disable-autoexec', str(fixture), '--python',
                                str(ROOT / 'tests' / 'integration' / 'm4_own_blender_start.py'), '--', str(args.port)],
                               stdout=open(folder / 'blender-stdout.txt', 'w', encoding='utf-8'), stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 90
        while not listening(args.port):
            if time.time() > deadline or blender.poll() is not None:
                raise SystemExit('Own Blender did not start the add-on on port ' + str(args.port))
            time.sleep(0.5)
        run = asyncio.run(main_async(args, folder, fixture, info))
    finally:
        blender.terminate()
        try:
            blender.wait(timeout=30)
        except subprocess.TimeoutExpired:
            blender.kill()
    run.report['fixture_file_unchanged'] = sha256(fixture) == digest
    run.report['blender_pid_started_and_closed_here'] = blender.pid
    run.report['status'] = 'pass' if ('exception' not in run.report and run.report['fixture_file_unchanged']
                                      and all(c['passed'] for c in run.report['checks'].values())) else 'fail'
    run.save()
    print('M4_MCP_REPORT=' + str(folder / 'report.json') + ' status=' + run.report['status'])
    raise SystemExit(0 if run.report['status'] == 'pass' else 1)


if __name__ == '__main__':
    main()
