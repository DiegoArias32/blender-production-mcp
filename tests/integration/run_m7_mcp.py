"""M7 stdio/socket acceptance against a GUI Blender owned by this run (never port 9876, never a real asset).

    python tests/integration/run_m7_mcp.py --blender "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"

The runner builds a controlled facial fixture, starts its own Blender with the repository add-on on a test
port, and drives the production server over stdio: inventory, analysis, plan, candidate, QA, close-up and
temporal renders, a real visual review (requested from the reviewer through a file, never invented), accept,
idempotent retry and safe rejections. The fixture .blend on disk must be unchanged at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from run_m5_mcp import Run, session, listening
from blender_mcp.production.common.storage import sha256
from blender_mcp.production.facial.tools import NAMES, READ_ONLY, TRANSIENT

ROOT = Path(__file__).resolve().parents[2]
BUILD = r'''
import sys, json
sys.path.insert(0, ROOT + '/tests/blender')
import m7_facial_fixtures as fx
manifest = fx.build(output, 'shape_keys', save=True)
'''
PHRASE = {'units': 'seconds', 'phonemes': [
    {'phoneme': 'SIL', 'start': 0.0, 'end': 0.2}, {'phoneme': 'B', 'start': 0.2, 'end': 0.28}, {'phoneme': 'AA', 'start': 0.28, 'end': 0.42},
    {'phoneme': 'B', 'start': 0.42, 'end': 0.5}, {'phoneme': 'SIL', 'start': 0.5, 'end': 0.9}],
    'words': [{'word': 'Bob', 'start': 0.2, 'end': 0.5, 'stress': 0.6}], 'source': 'controlled runner timeline'}


async def visual_review(run, render):
    token = uuid4().hex[:8]
    feedback = run.folder / ('review-feedback-' + token + '.json')
    request = {'render': render, 'feedback': str(feedback),
               'instructions': 'Look at the close-up montages and preview frames; write reviewer, artistic_observations, decision.'}
    path = run.folder / ('visual-request-' + token + '.json')
    path.write_text(json.dumps(request, indent=2), encoding='utf-8')
    print('VISUAL_REVIEW_REQUEST=' + str(path), flush=True)
    deadline = time.time() + 3600
    while not feedback.exists():
        if time.time() > deadline:
            raise TimeoutError('No visual review supplied; the candidate stays unaccepted')
        await asyncio.sleep(2)
    review = json.loads(feedback.read_text(encoding='utf-8-sig'))
    run.report.setdefault('visual_reviews', []).append({'request': str(path), 'review': review})
    return review


async def rejection(run, name, arguments, code):
    result = await run.call(name, arguments)
    passed = result.get('status') == 'error' and code in json.dumps(result.get('errors'), default=str)
    run.check('safe_rejection_' + code, passed, response=result.get('errors'))
    return passed


async def scenario(run, fixture, info):
    inventory = await run.client.list_tools()
    tools = {t.name: t for t in inventory.tools}
    facial = {'production.' + n for n in NAMES}
    run.check('inventory_201_with_46_facial', len(tools) == 201 and facial <= set(tools), total=len(tools))
    hints = all(tools['production.' + n].annotations.readOnlyHint is (n in READ_ONLY) for n in NAMES)
    transient = all(tools['production.' + n].meta['production']['scene_effects'] == 'transient_restored' for n in TRANSIENT)
    run.check('facial_hints_match_payload_effects', hints and transient)
    await run.good('asset_register_fixture', {'path': str(fixture)})
    status = await run.good('asset_status')
    run.check('fixture_role', status['asset']['role'] == 'FIXTURE')
    face = info['face']
    inspected = await run.good('facial_inspect', {'object_name': face})
    run.check('inspect_eyes_and_keys', len(inspected['eyes']) == 2 and len(inspected['shape_keys']) > 30)
    classified = await run.good('facial_classify_rig', {'object_name': face})
    run.check('classified_shape_keys', classified['classification']['type'] == 'SHAPE_KEYS', classification=classified['classification'])
    compat = await run.good('facial_compatibility_report', {'object_name': face})
    run.check('compatibility_reported', compat['compatibility']['lip_sync']['status'] == 'SUPPORTED', compat=compat['compatibility'])
    for name in ('facial_analyze_eyes', 'facial_analyze_eyelids', 'facial_analyze_jaw', 'facial_analyze_lips', 'facial_map_visemes',
                 'facial_analyze_shape_keys', 'facial_analyze_asymmetry'):
        await run.good(name, {'object_name': face})
    await run.good('facial_create_brief', {'intent': 'angry but restrained, speaking'})
    prepared = await run.good('facial_prepare_speech', {'options': {'timeline': PHRASE, 'fps': 30, 'duration_s': 1.0}})
    run.check('speech_prepared', prepared['segment_count'] == 5)
    await run.good('facial_generate_coarticulation', {'options': {'timeline': PHRASE, 'fps': 30, 'duration_s': 1.0}})
    await run.good('facial_mocap_analyze', {'options': {'source': {'fps': 30, 'channels': {'jawOpen': [0, 0.4, 0.2], 'eyeBlinkLeft': [0, 1, 0]}}}})
    planned = await run.good('facial_plan_expression', {'object_name': face, 'intent': 'angry but restrained, speaking',
                                                        'brief': {'duration_s': 1.5, 'fps': 30, 'speech': {'enabled': True}},
                                                        'operation_id': 'm7-mcp-plan-' + uuid4().hex[:8]})
    ref = planned['facial']
    run.check('plan_created_no_action', planned['no_action_created'] is True and planned['state'] == 'PLANNED')
    generate = {'facial': ref, 'apply': True, 'operation_id': 'm7-mcp-generate-' + uuid4().hex[:8],
                'options': {'speech': {'timeline': PHRASE}, 'saccades': {'seed': 3}, 'blinks': {'seed': 4}}}
    first = await run.call('facial_generate', generate)
    retry = await run.call('facial_generate', generate)
    created = first['data']
    run.check('candidate_created', first['status'] != 'error' and created['status'] == 'candidate_created', qa=created.get('technical_qa'))
    run.check('generate_idempotent', retry['execution_id'] == first['execution_id'] and retry['data']['candidate_action'] == created['candidate_action'])
    run.check('technical_qa_passed', created['technical_qa']['passed'], errors=created['technical_qa']['errors'])
    lipsync = await run.good('facial_validate_lipsync', {'facial': ref})
    run.check('lipsync_geometry_sealed', lipsync['passed'], geometry=lipsync.get('geometry'))
    for name in ('facial_analyze_layer_conflicts', 'facial_analyze_curves'):
        await run.good(name, {'facial': ref})
    integrated = await run.good('facial_integrate_with_body', {'facial': ref})
    run.check('integration_body_unchanged', integrated['integration']['passed'])
    close = await run.good('facial_render_closeup', {'facial': ref, 'options': {'size': 384}})
    preview = await run.good('facial_render_preview', {'facial': ref, 'options': {'size': 256}})
    run.check('renders_complete', close['status'] == 'rendered' and preview['render']['temporal_complete'])
    review = await visual_review(run, {'closeup': close['closeup'], 'preview': preview['render']})
    review['artifact_reference'] = preview['render']['output_dir']
    review['temporal_reviewed'] = True
    reviewed = await run.good('facial_review', {'facial': ref, 'options': {'review': review}})
    run.check('review_separates_technical_and_artistic', {'TECHNICAL_FINDINGS', 'ARTISTIC_OBSERVATIONS'} <= set(reviewed['review']))
    if review['decision'] == 'ACCEPT':
        accepted = await run.good('facial_accept', {'facial': ref, 'apply': True})
        run.check('accepted_separate_layer', accepted['status'] == 'accepted' and accepted['body_unchanged'], tracks=accepted.get('tracks'))
        await rejection(run, 'facial_refine', {'facial': ref, 'apply': True, 'options': {'target': 'curve_noise'}}, 'GENERATION_TERMINAL')
    await rejection(run, 'facial_prepare_speech', {'options': {'timeline': {'audio': 'line.wav'}}}, 'TIMING_INPUT_REQUIRED')
    await rejection(run, 'facial_generate_gaze', {'object_name': face, 'options': {'sequence': [{'frame': 1, 'target': {'type': 'angles', 'yaw_deg': 40}}]}},
                    'EYE_TARGET_OUT_OF_RANGE')
    unknown = {'fps': 30, 'channels': {'chan_01': [0, 1, 0], 'chan_02': [1, 0, 1]}}
    mapped = await run.good('facial_mocap_map', {'object_name': face, 'options': {'source': unknown}})
    run.check('mocap_map_reports_blocked', mapped['status'] == 'blocked' and mapped['reason'].startswith('MOCAP_MAPPING_INCOMPLETE'))
    await rejection(run, 'facial_mocap_retarget', {'object_name': face, 'apply': True, 'operation_id': 'm7-mcp-mocap-' + uuid4().hex[:8],
                                                   'options': {'source': unknown}}, 'MOCAP_MAPPING_INCOMPLETE')


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
    # Short work folder: Windows MAX_PATH would refuse nested render frames under the OneDrive repository path
    # (measured: OUTPUT_PATH_TOO_LONG). The report is copied into test-artifacts at the end.
    folder = Path(tempfile.gettempdir()) / ('bpm7mcp-' + uuid4().hex[:8])
    (folder / 'fixture').mkdir(parents=True)
    expression = 'from pathlib import Path\nROOT=' + repr(ROOT.as_posix()) + '\noutput=Path(' + repr(str(folder / 'fixture')) + ')\n' + BUILD
    subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python-expr', expression],
                   check=True, capture_output=True, text=True, encoding='utf8', errors='replace')
    info = json.loads((folder / 'fixture' / 'shape_keys.json').read_text())
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
    archive = ROOT / 'test-artifacts' / 'm7' / folder.name
    archive.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(folder / 'report.json', archive / 'report.json')
    print('M7_MCP_REPORT=' + str(archive / 'report.json') + ' workdir=' + str(folder) + ' status=' + run.report['status'])
    raise SystemExit(0 if run.report['status'] == 'pass' else 1)


if __name__ == '__main__':
    main()
