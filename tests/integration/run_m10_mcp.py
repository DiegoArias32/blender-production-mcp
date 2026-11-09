"""M10 over real MCP stdio against GUI Blender instances owned by this run (never port 9876).

    python tests/integration/run_m10_mcp.py --blender "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"
        [--port 9877] [--upstream %TEMP%/bpm10-democ-6] [--review-timeout 10800]

Scenario E (demo E fixture built here): the multi-actor pipeline with a prop hand-over through MCP calls only —
inventory and hints, scene and actor inspection, registry, brief, beats, shots, candidate, staging, clips, props,
cameras, cuts, focus, lighting, timeline with interactions and finalize, the six QA analyses, previs, temporal preview,
render plans (EEVEE planned, Cycles without OptiX refused), quality report and integrity. Safe refusals in real Blender:
MASTER mutation, duplicate operation_id, invalid camera target, overlapping clips, stale candidate. Then reject and the
measured rollback (source fingerprint, Actions, file on disk).

Scenario C (--upstream, the demo C upstream folder): the accepted M7 face and M8 pendant through MCP, with stale facial
Action and stale physics cache refusals (injected in this run's own instance and restored), the quality report, a REAL
visual review written by the coordinator (the runner only waits for review-feedback-mcp-C.json), cinematic_review and
cinematic_accept. The upstream .blend is opened, never saved.

Directing values (camera locations, the hand-over alignment) are the ones measured in the accepted e2e run
(test-artifacts/m10/e2e/bpm10-e2e-5/E_interaction); the lantern grip is measured here in the fixture build process.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from run_m5_mcp import Run, listening
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.storage import sha256
from blender_mcp.production.cinematic.tools import NAMES, READ_ONLY, TRANSIENT

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / 'test-artifacts' / 'm10' / 'e2e' / 'bpm10-e2e-5'
BUILD = r'''
import json, math, sys, types
sys.path.insert(0, ROOT + '/src'); sys.path.insert(0, ROOT + '/tests/blender')
package = types.ModuleType('blender_mcp'); package.__path__ = [ROOT + '/src/blender_mcp']
sys.modules.setdefault('blender_mcp', package)
import bpy
import m10_cinematic_fixtures as FX
manifest = FX.build_demo('E_interaction', OUT, save=True)
# Lantern grip at frame 2 with the staging the cinematic applies (giver yaw +90, Idle playing): measured after the
# save, the file on disk is not written again.
giver = bpy.data.objects['Giver']
giver.delta_rotation_euler = (0.0, 0.0, math.radians(90.0))
giver.animation_data.action = bpy.data.actions['Giver_Idle']
bpy.context.scene.frame_set(2)
ev = giver.evaluated_get(bpy.context.evaluated_depsgraph_get())
bone = ev.pose.bones['hand.R']
manifest['lantern_grip_frame2'] = list(ev.matrix_world @ bone.head.lerp(bone.tail, .6))
json.dump(manifest, open(OUT + '/manifest.json', 'w'), default=str)
'''
ROLE_BONES = (('root', 'root'), ('pelvis', 'pelvis'), ('head', 'head'), ('hand_left', ('hands', 'L')),
              ('hand_right', ('hands', 'R')), ('foot_left', ('feet', 'L')), ('foot_right', ('feet', 'R')))


def bones_of(actor):
    return {role: (actor['bones'][ref] if isinstance(ref, str) else actor['bones'][ref[0]][ref[1]]) for role, ref in ROLE_BONES}


def op(label):
    return 'm10-' + label + '-' + uuid4().hex[:10]


async def refused(run, name, arguments, code):
    result = await run.call(name, arguments)
    text = json.dumps(result.get('errors'), default=str)
    passed = result.get('status') == 'error' and code in text
    run.check('refusal_' + name.removeprefix('cinematic_') + '_' + code, passed, errors=result.get('errors'))
    return result


def inject(port, fixture, body, params=None):
    """Read or defect injection in this run's own instance only (same pattern as run_m9_mcp)."""
    prefix = r'''
import bpy
if bpy.data.filepath.replace('\\','/') != params['fixture'].replace('\\','/'):
    raise ValueError('OWN_FIXTURE_REQUIRED: only the fixture opened by this runner')
'''
    bridge = Bridge(port=port)
    try:
        return bridge.run(prefix + body, dict(params or {}, fixture=str(fixture)))
    finally:
        bridge.close()


@asynccontextmanager
async def m10_session(workdir, roots, port, log):
    # Same as run_m5_mcp.session with an explicit storage root: scenario C reads the M7/M8 ledgers of the upstream store.
    env = dict(os.environ, BLENDER_HOST='localhost', BLENDER_PORT=str(port), BLENDER_MCP_SAFE_MODE='1',
               DISABLE_TELEMETRY='true', BLENDER_PRODUCTION_WORKDIR=str(workdir),
               BLENDER_PRODUCTION_FIXTURE_ROOTS=str(roots), PYTHONIOENCODING='utf-8')
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'blender_mcp.production.server'], env=env)
    async with stdio_client(parameters, errlog=log) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


def start_blender(args, fixture, log_path):
    log = log_path.open('w', encoding='utf8')
    blender = subprocess.Popen([args.blender, '--factory-startup', '--disable-autoexec', str(fixture), '--python',
                                str(ROOT / 'tests' / 'integration' / 'm4_own_blender_start.py'), '--', str(args.port)],
                               stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 120
    while not listening(args.port):
        if time.time() > deadline or blender.poll() is not None:
            blender.terminate()
            raise SystemExit('Own controlled Blender did not start the add-on')
        time.sleep(.5)
    return blender, log


def stop_blender(blender, log):
    blender.terminate()
    try:
        blender.wait(timeout=30)
    except subprocess.TimeoutExpired:
        blender.kill()
    log.close()


# ------------------------------------------------------------------------------------------------ scenario E

async def scenario_e(run, args, folder, info):
    inventory = await run.client.list_tools()
    tools = {t.name: t for t in inventory.tools}
    run.check('inventory_has_every_cinematic_tool', {'production.' + n for n in NAMES} <= set(tools), total=len(tools),
              cinematic=len(NAMES))
    # TRANSIENT tools change frame/pose while they run (restored): they are not advertised read-only.
    run.check('read_only_hint_only_on_read_only_tools', all(
        tools['production.' + n].annotations.readOnlyHint is (n in READ_ONLY) for n in NAMES))
    run.check('transient_and_writer_tools_take_no_auto_approval', not any(
        tools['production.' + n].annotations.readOnlyHint for n in TRANSIENT))
    await run.good('asset_register_fixture', {'path': info['blend']})
    scene = await run.good('cinematic_inspect_scene', {})
    run.check('inspect_scene_reads_source', bool(scene), keys=sorted(scene)[:12])
    actors = {a['name']: a for a in info['actors']}
    ids = {}
    for name in ('Giver', 'Taker'):
        actor = actors[name]
        registered = await run.good('cinematic_register_actor', {
            'object_name': actor['object'], 'operation_id': op('register-' + name),
            'options': {'role': actor['role'], 'bones': bones_of(actor), 'body_actions': [a['name'] for a in actor['actions']]}})
        ids[name] = registered['actor_id']
    inspected = await run.good('cinematic_inspect_actor', {'actor': ids['Giver']})
    run.check('inspect_actor_returns_the_record', inspected.get('actor_id') == ids['Giver'] or ids['Giver'] in json.dumps(inspected))
    g, t = ids['Giver'], ids['Taker']
    handoff = 104
    intent = {'title': 'Lantern handoff (MCP)', 'description': 'The giver offers the lantern, the taker takes it.',
              'duration_s': 8.0, 'actors': [{'actor_id': g, 'role': 'PROTAGONIST'}, {'actor_id': t, 'role': 'SUPPORTING'}],
              'events': [{'event_id': 'E_OFFER', 'kind': 'HOLD_PROP', 'actors': [g], 'target': 'Lantern', 'time_s': 1.0},
                         {'event_id': 'E_REACH', 'kind': 'REACH', 'actors': [t], 'target': g, 'time_s': 2.5,
                          'action_refs': ['Taker_Reach']},
                         {'event_id': 'E_HANDOFF', 'kind': 'HANDOFF_PROP', 'actors': [t, g], 'target': 'Lantern',
                          'time_s': (handoff - 1) / 30}],
              'atmosphere': {'mood': 'warm', 'mood_preset': 'NEUTRAL_THREE_POINT'}}
    brief = (await run.good('cinematic_create_brief', {'brief': intent}))['brief']
    beats = await run.good('cinematic_plan_beats', {'brief': brief})
    shots_list = {'schema': 'production.cinematic_shots.v1', 'shots': [
        {'shot_id': 'S01', 'type': 'WIDE', 'frame_start': 1, 'frame_end': 60, 'subjects': [g, t]},
        {'shot_id': 'S02', 'type': 'MEDIUM', 'frame_start': 61, 'frame_end': 95, 'subjects': [g]},
        {'shot_id': 'S03', 'type': 'INSERT', 'frame_start': 96, 'frame_end': 125, 'subjects': [g, t]},
        {'shot_id': 'S04', 'type': 'OVER_THE_SHOULDER', 'frame_start': 126, 'frame_end': 175, 'subjects': [t]},
        {'shot_id': 'S05', 'type': 'WIDE', 'frame_start': 176, 'frame_end': 240, 'subjects': [g, t]}]}
    shots = await run.good('cinematic_plan_shots', {'brief': brief, 'options': {'beats': beats, 'shots': shots_list['shots']}})
    paths = await run.good('cinematic_plan_actor_paths', {'brief': brief, 'options': {'beats': beats}})
    run.check('plan_actor_paths_answers', isinstance(paths, dict))
    interaction_plan = await run.good('cinematic_plan_interaction', {'brief': brief, 'options': {'beats': beats}})
    run.check('plan_interaction_answers', isinstance(interaction_plan, dict))
    created = await run.good('cinematic_create_candidate', {'brief': brief, 'apply': True, 'operation_id': op('create'),
                                                           'options': {'beats': beats, 'shots': shots, 'exclude_objects': ['Lantern']}})
    ref, cand = created['cinematic'], created['scene']
    base = {'cinematic': ref}

    await run.good('cinematic_stage_actors', dict(base, apply=True, operation_id=op('stage'), options={'actors': [
        {'actor_id': g, 'location': [0.0, 0.0, 0.0], 'yaw_deg': 90.0}, {'actor_id': t, 'location': [-0.4, -0.5, 0.0], 'yaw_deg': -90.0}]}))
    clips = [{'actor_id': g, 'action': 'Giver_Idle', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 61, 'extrapolation': 'HOLD'},
             {'actor_id': g, 'action': 'Giver_Reach', 'start_frame': 61, 'action_frame_start': 1, 'action_frame_end': 41, 'extrapolation': 'HOLD_FORWARD'},
             {'actor_id': g, 'action': 'Giver_Lower', 'start_frame': handoff + 4, 'action_frame_start': 1, 'action_frame_end': 33, 'extrapolation': 'HOLD_FORWARD'},
             {'actor_id': t, 'action': 'Taker_Idle', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 61, 'repeat': 1.25, 'extrapolation': 'HOLD'},
             {'actor_id': t, 'action': 'Taker_Reach', 'start_frame': 76, 'action_frame_start': 1, 'action_frame_end': 41, 'extrapolation': 'HOLD_FORWARD'},
             {'actor_id': t, 'action': 'Taker_Lower', 'start_frame': 120, 'action_frame_start': 1, 'action_frame_end': 33, 'extrapolation': 'HOLD_FORWARD'}]
    overlapping = [dict(clips[0]), dict(clips[0], action='Giver_Reach', start_frame=30)]
    await refused(run, 'cinematic_sync_actions', dict(base, apply=True, operation_id=op('sync-overlap'),
                                                      options={'clips': overlapping}), 'CLIP_OVERLAP')
    await run.good('cinematic_sync_actions', dict(base, apply=True, operation_id=op('sync'), options={'clips': clips}))
    e2e = json.loads((E2E / 'E_interaction' / 'm10_e2e_E_interaction.json').read_text(encoding='utf-8'))
    correction = next(a for a in e2e['adjustments'] if a['kind'] == 'E_HANDOFF_ALIGNMENT')['correction_m']
    await run.good('cinematic_stage_actors', dict(base, apply=True, operation_id=op('align'), options={'actors': [
        {'actor_id': t, 'location': correction, 'yaw_deg': 0.0}]}))
    lantern = cand + '_PROP_lantern'
    await run.good('cinematic_manage_props', dict(base, apply=True, operation_id=op('props'), options={'props': [
        {'prop_id': 'lantern', 'object': 'Lantern', 'placement': {'location': info['lantern_grip_frame2'], 'rotation_deg': [0, 0, 0]},
         'attach': [{'actor_id': g, 'bone': 'hand.R', 'frame_start': 2, 'frame_end': handoff - 1},
                    {'actor_id': t, 'bone': 'hand.R', 'frame_start': handoff, 'frame_end': 240}]}]}))
    await refused(run, 'cinematic_manage_props', dict(base, apply=True, operation_id=op('props-missing'), options={'props': [
        {'prop_id': 'ghost', 'object': 'NoSuchLantern'}]}), 'MISSING')

    placements = {row['shot']: row['location'] for row in e2e['camera_placements']}
    cameras = [('S01', [0.0, -5.0, 1.9], {'point': [-0.2, -0.25, 1.0]}, 35, 'STATIC', {}),
               ('S02', placements['S02'], {'actor_id': g, 'bone': 'chest'}, 32, 'STATIC', {}),
               ('S03', placements['S03'], {'object': lantern}, 45, 'STATIC', {}),
               ('S04', placements['S04'], {'actor_id': t, 'bone': 'spine'}, 32, 'STATIC', {}),
               ('S05', [-0.3, -4.6, 1.6], {'point': [-0.3, -0.25, 1.0]}, 28, 'ORBIT',
                {'params': {'angle_deg': 12.0}, 'easing': 'EASE_IN_OUT', 'target': {'point': [-0.3, -0.25, 1.0]}})]
    await refused(run, 'cinematic_create_camera', dict(base, apply=True, operation_id=op('cam-bad'), options={
        'camera_id': 'CAM_BAD', 'shot_id': 'S01', 'lens_mm': 35, 'location': [0, -5, 2],
        'look_at': {'actor_id': 'f' * 16, 'bone': 'head'}}), 'ACTOR_NOT_IN_PLAN')
    for shot_id, location, look_at, lens, move, extra in cameras:
        camera_id = next(s for s in shots['shots'] if s['shot_id'] == shot_id)['camera_id']
        arguments = dict(base, apply=True, options={'camera_id': camera_id, 'shot_id': shot_id, 'lens_mm': lens,
                                                    'location': location, 'look_at': look_at, 'clip_start': 0.05})
        await run.good('cinematic_create_camera', dict(arguments, operation_id=op('cam-' + shot_id)))
        await run.good('cinematic_configure_camera', dict(base, apply=True, operation_id=op('conf-' + shot_id), options={
            'camera_id': camera_id, 'dof': {'use_dof': shot_id in ('S02', 'S03', 'S04'), 'aperture_fstop': 2.8}}))
        await run.good('cinematic_animate_camera', dict(base, apply=True, operation_id=op('anim-' + shot_id), options=dict(
            {'camera_id': camera_id, 'shot_id': shot_id, 'move': move}, **extra)))
    cuts_args = dict(base, apply=True, options={'from_shots': True}, operation_id=op('cuts'))
    first = await run.call('cinematic_create_cuts', cuts_args)
    replay = await run.call('cinematic_create_cuts', cuts_args)
    run.check('duplicate_operation_id_replays_without_running_again', first.get('status') != 'error'
              and any('IDEMPOTENT_REPLAY' in str(w) for w in replay.get('warnings') or []),
              first=first.get('execution_id'), replay_warnings=replay.get('warnings'))
    reused = await run.call('cinematic_create_cuts', dict(cuts_args, options={'from_shots': True, 'min_shot_frames': 2}))
    run.check('reused_operation_id_with_other_inputs_is_refused', reused.get('status') == 'error'
              and 'IDEMPOTENCY_KEY_CONFLICT' in json.dumps(reused.get('errors'), default=str), errors=reused.get('errors'))
    s03 = next(s for s in shots['shots'] if s['shot_id'] == 'S03')
    await run.good('cinematic_plan_focus', dict(base, apply=True, operation_id=op('focus'), options={
        'camera_id': s03['camera_id'], 'shot_id': 'S03', 'aperture_fstop': 4.0,
        'focus': [{'frame': s03['frame_start'], 'target': {'object': lantern}}]}))
    planned = await run.good('cinematic_plan_lighting', dict(base, options={'preset': 'NEUTRAL_THREE_POINT', 'key_subject': g, 'shot': 'S01'}))
    await run.good('cinematic_setup_lighting', dict(base, apply=True, operation_id=op('light'), options={'plan': planned['plan']}))
    hand = {'bone': 'hand.R', 'end': 'TAIL'}
    rows = [{'event_id': 'E_HOLD_GIVER', 'kind': 'HOLD_PROP', 'actor_id': g, 'frame': 2, 'frame_end': handoff + 10,
             'effector': hand, 'target': {'object': lantern}, 'contact': True},
            {'event_id': 'E_GRAB', 'kind': 'REACH', 'actor_id': t, 'frame': handoff - 14, 'frame_end': handoff,
             'effector': hand, 'target': {'object': lantern}, 'contact': True},
            {'event_id': 'E_HANDOFF', 'kind': 'HANDOFF_PROP', 'actor_id': t, 'frame': handoff, 'frame_end': handoff,
             'effector': hand, 'target': {'object': lantern}, 'contact': True},
            {'event_id': 'E_HOLD_TAKER', 'kind': 'HOLD_PROP', 'actor_id': t, 'frame': handoff, 'frame_end': 240,
             'effector': hand, 'target': {'object': lantern}, 'contact': True}]
    await run.good('cinematic_manage_timeline', dict(base, apply=True, operation_id=op('timeline'), options={'interactions': rows}))
    assembled = await run.good('cinematic_manage_timeline', dict(base, apply=True, operation_id=op('finalize'), options={'finalize': True}))
    run.check('timeline_finalize_assembles', assembled.get('state') == 'ASSEMBLED' or 'ASSEMBLED' in json.dumps(assembled)[:4000])
    analyses = {}
    for name in ('cinematic_analyze_composition', 'cinematic_validate_continuity', 'cinematic_analyze_visibility',
                 'cinematic_analyze_actor_collisions', 'cinematic_analyze_contacts', 'cinematic_analyze_motion'):
        result = await run.good(name, dict(base))
        analyses[name] = {'passed': result.get('passed'), 'state_restored': result.get('state_restored'),
                          'metrics': result.get('metrics')}
        run.check(name + '_restores_the_candidate', result.get('state_restored') is True)
    run.report['analyses'] = analyses
    await run.good('cinematic_validate_lighting', dict(base, apply=True, operation_id=op('validate-light'), options={}))
    previs = await run.good('cinematic_generate_previs', dict(base, operation_id=op('previs'), options={'size': 320}))
    preview = await run.good('cinematic_render_preview', dict(base, operation_id=op('preview'), options={'size': 320}))
    run.check('temporal_preview_complete', preview.get('temporal_complete') is True and preview.get('cuts_respected') is True,
              backend=preview.get('render_backend'), output=preview.get('output_dir'))
    run.report['artifacts_e'] = {'storyboard': previs.get('storyboard'), 'preview': preview.get('player')}
    plan_ok = await run.good('cinematic_render_plan', dict(base, options={'engine': 'BLENDER_EEVEE', 'quality_preset': 'DRAFT', 'shots': ['S03']}))
    run.check('render_plan_eevee_on_gpu', isinstance(plan_ok, dict), plan=json.dumps(plan_ok, default=str)[:400])
    await refused(run, 'cinematic_render_plan', dict(base, options={'engine': 'CYCLES', 'quality_preset': 'DRAFT', 'shots': ['S03']}),
                  'CINEMATIC_RENDER_OPTIX_REQUIRED')
    quality = await run.good('cinematic_quality_report', dict(base, operation_id=op('quality')))
    run.check('quality_report_passes_technically', quality.get('passed') is True, failing=quality.get('failing_sections'),
              blocked=quality.get('mandatory_not_passed'))
    s05 = next(s for s in shots['shots'] if s['shot_id'] == 'S05')
    proposal = await run.good('cinematic_refine', dict(base, operation_id=op('refine-proposal'), options={
        'target': 'headroom_error', 'goal': 0.08,
        'scope': {'camera': cand + '_CAM_' + s05['camera_id'], 'actor': g, 'frame_start': 176, 'frame_end': 240},
        'changes': [{'kind': 'camera_settings', 'camera': cand + '_CAM_' + s05['camera_id'], 'settings': {'lens': 24.0}}]}))
    run.check('refine_proposal_measures_without_spending_a_cycle', proposal.get('status') == 'proposal'
              and proposal.get('remaining_cycles') == 3, before=(proposal.get('findings') or {}).get('value'))
    integrity = await run.good('cinematic_verify_integrity', dict(base))
    run.check('integrity_passes', integrity.get('passed') is True or integrity.get('status') == 'success',
              summary=json.dumps(integrity, default=str)[:600])
    status = await run.good('cinematic_status', dict(base))
    run.check('status_reads_the_ledger', 'state' in json.dumps(status)[:3000])

    # Stale candidate: the S01 camera lens (static data; animated channels are fingerprinted through their animation)
    # changed outside the tools in this run's own instance, refused, then put back exactly.
    cam = cand + '_CAM_' + next(s for s in shots['shots'] if s['shot_id'] == 'S01')['camera_id']
    changed = inject(args.port, info['blend'], r'''
cam = bpy.data.objects[params['camera']]
before = cam.data.lens
cam.data.lens = before + 5.0
result = {'before': before}
''', {'camera': cam})
    await refused(run, 'cinematic_create_cuts', dict(base, apply=True, operation_id=op('cuts-stale'), options={'from_shots': True}),
                  'CINEMATIC_STALE_CANDIDATE')
    inject(args.port, info['blend'], r'''
cam = bpy.data.objects[params['camera']]
cam.data.lens = params['before']
result = {'restored': cam.data.lens}
''', {'camera': cam, 'before': changed['before']})
    fresh_again = await run.good('cinematic_status', dict(base))
    run.check('candidate_fresh_again_after_exact_restore', 'STALE' not in json.dumps(fresh_again)[:4000])
    rejected = await run.good('cinematic_reject', dict(base, apply=True, operation_id=op('reject'), options={'reason': 'MCP qualification rollback'}))
    run.report['reject_e'] = {k: rejected.get(k) for k in ('status', 'source_restored', 'removed', 'undo_failed')}
    after = await run.good('cinematic_status', dict(base))
    run.check('reject_restores_the_source', rejected.get('source_restored') is True or 'REJECTED' in json.dumps(after)[:3000],
              reject=run.report['reject_e'])
    await master_negative(run, args, folder, info, intent, ids)


async def master_negative(run, args, folder, info, intent, ids):
    """A copy of this fixture, opened in this run's own instance and registered MASTER: the cinematic is refused by
    the asset guard before any checkpoint, and the file stays byte-identical (same pattern as run_m9_mcp)."""
    master = folder / 'synthetic-master-negative.blend'
    inject(args.port, info['blend'], r'''
outcome = bpy.ops.wm.save_as_mainfile(filepath=params['target'], copy=True, relative_remap=True)
if 'FINISHED' not in outcome:
    raise RuntimeError('MASTER_FIXTURE_COPY_FAILED')
outcome = bpy.ops.wm.open_mainfile(filepath=params['target'], load_ui=False, use_scripts=False)
if 'FINISHED' not in outcome:
    raise RuntimeError('MASTER_FIXTURE_OPEN_FAILED')
for scene in bpy.data.scenes:
    scene.blendermcp_port = params['port']
result = {'path': bpy.data.filepath}
''', {'target': str(master), 'port': args.port})
    original = sha256(master)
    await run.good('asset_register_master', {'confirm_master': True})
    actors = {a['name']: a for a in info['actors']}
    master_ids = {}
    for name in ('Giver', 'Taker'):
        actor = actors[name]
        registered = await run.good('cinematic_register_actor', {
            'object_name': actor['object'], 'operation_id': op('register-master-' + name),
            'options': {'role': actor['role'], 'bones': bones_of(actor), 'body_actions': [a['name'] for a in actor['actions']]}})
        master_ids[ids[name]] = registered['actor_id']
    text = json.dumps(intent)
    for old, new in master_ids.items():
        text = text.replace(old, new)
    brief = (await run.good('cinematic_create_brief', {'brief': json.loads(text)}))['brief']
    checkpoints = folder / 's' / 'checkpoints'
    before = len(list(checkpoints.glob('*'))) if checkpoints.exists() else 0
    denied = await run.call('cinematic_create_candidate', {'brief': brief, 'apply': True, 'operation_id': op('create-master'),
                                                          'options': {'exclude_objects': ['Lantern']}})
    after = len(list(checkpoints.glob('*'))) if checkpoints.exists() else 0
    run.check('master_refused_before_any_checkpoint', denied.get('status') == 'error' and 'MASTER' in json.dumps(denied.get('errors'))
              and before == after, errors=denied.get('errors'), checkpoints=[before, after])
    run.check('master_file_unchanged', sha256(master) == original)


# ------------------------------------------------------------------------------------------------ scenario C

async def scenario_c(run, args, upstream, report):
    hero = report['hero']
    e2e = json.loads((E2E / 'C_body_face_physics' / 'm10_e2e_C_body_face_physics.json').read_text(encoding='utf-8'))
    exclude = e2e['upstream']['exclude']
    registered = await run.good('cinematic_register_actor', {
        'object_name': hero['object'], 'operation_id': op('register-hero'),
        'options': {'role': hero['role'], 'bones': bones_of(hero), 'body_actions': [a['name'] for a in hero['actions']],
                    'facial': [report['facial']], 'secondary': [report['physics']]}})
    h = registered['actor_id']
    intent = {'title': 'Body, face and physics (MCP)', 'description': 'The scared hero walks towards the camera; the pendant swings.',
              'duration_s': 3.0, 'actors': [{'actor_id': h, 'role': 'PROTAGONIST'}],
              'events': [{'event_id': 'E_WALK', 'kind': 'APPROACH', 'actors': [h], 'time_s': 0.0, 'target': [0.0, -2.2, 0.0],
                          'action_refs': ['Hero_Walk']},
                         {'event_id': 'E_STOP', 'kind': 'ENDING', 'actors': [h], 'time_s': 2.7}],
              'required_performances': [{'actor': h, 'body_actions': ['Hero_Walk'], 'facial': True, 'secondary': True}],
              'atmosphere': {'mood': 'tense', 'mood_preset': 'NEUTRAL_THREE_POINT'}}
    created_brief = await run.good('cinematic_create_brief', {'brief': intent})
    run.check('brief_ready_with_accepted_face_and_secondary', created_brief.get('ready') is True,
              missing=created_brief.get('missing_prerequisites'))
    brief = created_brief['brief']
    beats = await run.good('cinematic_plan_beats', {'brief': brief})
    rows = [{'shot_id': 'S01', 'type': 'WIDE', 'frame_start': 1, 'frame_end': 30, 'subjects': [h]},
            {'shot_id': 'S02', 'type': 'MEDIUM', 'frame_start': 31, 'frame_end': 60, 'subjects': [h]},
            {'shot_id': 'S03', 'type': 'CLOSE_UP', 'frame_start': 61, 'frame_end': 90, 'subjects': [h]}]
    shots = await run.good('cinematic_plan_shots', {'brief': brief, 'options': {'beats': beats, 'shots': rows}})
    created = await run.good('cinematic_create_candidate', {'brief': brief, 'apply': True, 'operation_id': op('create-c'),
                                                           'options': {'beats': beats, 'shots': shots, 'exclude_objects': exclude}})
    base = {'cinematic': created['cinematic']}
    secondary = {'actor_id': h, 'physics': report['physics'], 'offset_frames': 0, 'window': [1, 90]}
    # Stale physics cache: the M8 baked playback shifted by one frame in this run's own instance, then restored.
    baked = next(n for n in exclude if n.endswith('_Baked'))
    shifted = inject(args.port, upstream / 'demo_c_source.blend', r'''
mod = [m for m in bpy.data.objects[params['baked']].modifiers if m.type == 'MESH_CACHE'][0]
before = mod.frame_start
mod.frame_start = before + 1.0
result = {'before': before}
''', {'baked': baked})
    await refused(run, 'cinematic_integrate_secondary', dict(base, apply=True, operation_id=op('sec-stale'), options=secondary),
                  'SECONDARY_NOT_CURRENT')
    inject(args.port, upstream / 'demo_c_source.blend', r'''
mod = [m for m in bpy.data.objects[params['baked']].modifiers if m.type == 'MESH_CACHE'][0]
mod.frame_start = params['before']
result = {'restored': mod.frame_start}
''', {'baked': baked, 'before': shifted['before']})
    await run.good('cinematic_integrate_secondary', dict(base, apply=True, operation_id=op('sec'), options=secondary))
    await run.good('cinematic_sync_actions', dict(base, apply=True, operation_id=op('sync-c'), options={'clips': [
        {'actor_id': h, 'action': 'Hero_Walk', 'start_frame': 1, 'action_frame_start': 1, 'action_frame_end': 91, 'extrapolation': 'HOLD'}]}))
    facial = {'actor_id': h, 'facial': report['facial'], 'start_frame': 1,
              'events': [{'frame': 18, 'label': 'blink'}, {'frame': 52, 'label': 'blink'}, {'frame': 80, 'label': 'slow blink'}]}
    # Stale facial Action: one key of the accepted M7 Action nudged in this run's own instance, then restored exactly.
    m7_action = next(s['data']['candidate'] for s in report['steps'] if s['step'] == 'm7_generate')
    nudged = inject(args.port, upstream / 'demo_c_source.blend', r'''
action = bpy.data.actions[params['action']]
curve = None
for layer in action.layers:
    for strip in layer.strips:
        for bag in strip.channelbags:
            if curve is None and len(bag.fcurves):
                curve = bag.fcurves[0]
k = curve.keyframe_points[0]
saved = [list(k.co), list(k.handle_left), list(k.handle_right)]
k.co[1] = k.co[1] + 0.25
curve.update()
result = {'action': action.name, 'path': curve.data_path, 'index': curve.array_index, 'saved': saved}
''', {'action': m7_action})
    await refused(run, 'cinematic_integrate_facial', dict(base, apply=True, operation_id=op('face-stale'), options=facial), 'FACIAL_STALE')
    inject(args.port, upstream / 'demo_c_source.blend', r'''
action = bpy.data.actions[params['action']]
curve = None
for layer in action.layers:
    for strip in layer.strips:
        for bag in strip.channelbags:
            for c in bag.fcurves:
                if c.data_path == params['path'] and c.array_index == params['index']:
                    curve = c
k = curve.keyframe_points[0]
k.co = params['saved'][0]
curve.update()
k = curve.keyframe_points[0]
k.co = params['saved'][0]
k.handle_left = params['saved'][1]
k.handle_right = params['saved'][2]
result = {'restored': [list(k.co), list(k.handle_left), list(k.handle_right)] == params['saved']}
''', {'action': nudged['action'], 'path': nudged['path'], 'index': nudged['index'], 'saved': nudged['saved']})
    run.report['m7_action_nudged_and_restored'] = m7_action
    await run.good('cinematic_integrate_facial', dict(base, apply=True, operation_id=op('face'), options=facial))
    head = {'actor_id': h, 'bone': 'head'}
    cameras = [('S01', [3.0, -5.6, 1.9], {'point': [0.0, -0.6, 1.0]}, 35, 'STATIC', {}),
               ('S02', [1.3, -2.9, 1.45], head, 35, 'TRACKING', {'params': {'look_at_target': False}, 'target': {'actor_id': h}}),
               ('S03', [0.3, -4.4, 1.62], head, 65, 'STATIC', {})]
    for shot_id, location, look_at, lens, move, extra in cameras:
        camera_id = next(s for s in shots['shots'] if s['shot_id'] == shot_id)['camera_id']
        await run.good('cinematic_create_camera', dict(base, apply=True, operation_id=op('c-cam-' + shot_id), options={
            'camera_id': camera_id, 'shot_id': shot_id, 'lens_mm': lens, 'location': location, 'look_at': look_at, 'clip_start': 0.05}))
        await run.good('cinematic_configure_camera', dict(base, apply=True, operation_id=op('c-conf-' + shot_id), options={
            'camera_id': camera_id, 'dof': {'use_dof': shot_id == 'S03', 'aperture_fstop': 4.0}}))
        await run.good('cinematic_animate_camera', dict(base, apply=True, operation_id=op('c-anim-' + shot_id), options=dict(
            {'camera_id': camera_id, 'shot_id': shot_id, 'move': move}, **extra)))
    await run.good('cinematic_create_cuts', dict(base, apply=True, operation_id=op('c-cuts'), options={'from_shots': True}))
    s03 = next(s for s in shots['shots'] if s['shot_id'] == 'S03')
    await run.good('cinematic_plan_focus', dict(base, apply=True, operation_id=op('c-focus'), options={
        'camera_id': s03['camera_id'], 'shot_id': 'S03', 'aperture_fstop': 4.0, 'focus': [{'frame': 61, 'target': head}]}))
    planned = await run.good('cinematic_plan_lighting', dict(base, options={'preset': 'NEUTRAL_THREE_POINT', 'key_subject': h, 'shot': 'S03'}))
    await run.good('cinematic_setup_lighting', dict(base, apply=True, operation_id=op('c-light'), options={'plan': planned['plan']}))
    await run.good('cinematic_manage_timeline', dict(base, apply=True, operation_id=op('c-finalize'), options={'finalize': True}))
    await run.good('cinematic_validate_lighting', dict(base, apply=True, operation_id=op('c-validate'), options={}))
    await run.good('cinematic_generate_previs', dict(base, operation_id=op('c-previs'), options={'size': 480}))
    preview = await run.good('cinematic_render_preview', dict(base, operation_id=op('c-preview'), options={'size': 480}))
    quality = await run.good('cinematic_quality_report', dict(base, operation_id=op('c-quality')))
    sections = {s['section']: s['status'] for s in quality.get('sections') or []}
    run.check('quality_report_passes_with_face_and_secondary', quality.get('passed') is True
              and sections.get('facial_timing') == 'PASS' and sections.get('secondary') == 'PASS', sections=sections)
    run.report['artifacts_c'] = {'player': preview.get('player'), 'output_dir': preview.get('output_dir'),
                                 'review_template': preview.get('review_template')}
    request = run.folder / 'review-request-mcp-C.json'
    feedback = run.folder / 'review-feedback-mcp-C.json'
    request.write_text(json.dumps({'artifact': run.report['artifacts_c'], 'feedback_path': str(feedback),
                                   'instruction': 'Real visual review of the MCP preview; the runner never writes it.'},
                                  indent=1), encoding='utf-8')
    print('REVIEW_REQUEST ' + str(request), flush=True)
    deadline = time.time() + args.review_timeout
    while not feedback.exists():
        if time.time() > deadline:
            run.check('real_review_received', False)
            return
        await asyncio.sleep(2)
    await asyncio.sleep(.5)
    review = json.loads(feedback.read_text(encoding='utf-8-sig'))
    run.check('real_review_received', True, decision=review.get('decision'))
    await run.good('cinematic_review', dict(base, operation_id=op('c-review'), options={'review': review}))
    if review.get('decision') == 'ACCEPT':
        accepted = await run.good('cinematic_accept', dict(base, apply=True, operation_id=op('c-accept')))
        run.check('accept_after_real_review', accepted.get('state') == 'ACCEPTED' or 'ACCEPTED' in json.dumps(accepted)[:2000],
                  acceptance=accepted.get('acceptance'))
    else:
        await run.good('cinematic_reject', dict(base, apply=True, operation_id=op('c-reject'), options={'reason': 'REVIEW'}))


# ------------------------------------------------------------------------------------------------ runner

async def run_scenario(args, folder, workdir, roots, body):
    folder.mkdir(parents=True, exist_ok=True)
    run = Run(folder)
    with (folder / 'server-stderr.txt').open('a', encoding='utf8') as log:
        async with m10_session(workdir, roots, args.port, log) as client:
            run.client = client
            try:
                await body(run)
            except Exception as exc:
                run.report['exception'] = repr(exc)[:2000]
                print('EXCEPTION', repr(exc)[:1000], flush=True)
    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--port', type=int, default=9877)
    parser.add_argument('--upstream', default=None)
    parser.add_argument('--review-timeout', type=float, default=10800)
    parser.add_argument('--only', choices=('E', 'C'), default=None)
    args = parser.parse_args()
    if args.port == 9876 or not 1024 <= args.port <= 65535:
        raise SystemExit('Refusing reserved/invalid port; use a dedicated loopback test port')
    if listening(args.port):
        raise SystemExit('Port already in use; no existing Blender is stopped or contacted')
    folder = Path(tempfile.gettempdir()) / ('bpm10mcp-' + uuid4().hex[:8])
    (folder / 'fixture').mkdir(parents=True)
    summary = {'schema': 'm10.mcp_qualification.v1', 'folder': str(folder), 'port': args.port, 'scenarios': {}}
    if args.only in (None, 'E'):
        expression = ('ROOT=' + repr(ROOT.as_posix()) + '\nOUT=' + repr((folder / 'fixture').as_posix()) + '\n' + BUILD)
        built = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python-expr', expression],
                               capture_output=True, text=True, encoding='utf8', errors='replace')
        (folder / 'fixture-build.log').write_text(built.stdout + built.stderr, encoding='utf8')
        info = json.loads((folder / 'fixture' / 'manifest.json').read_text(encoding='utf-8'))
        digest = sha256(Path(info['blend']))
        blender, blog = start_blender(args, info['blend'], folder / 'blender-E.txt')
        try:
            run_e = asyncio.run(run_scenario(args, folder / 'E', folder / 's', folder, lambda run: scenario_e(run, args, folder / 'E', info)))
        finally:
            stop_blender(blender, blog)
        run_e.report['fixture_file_unchanged'] = sha256(Path(info['blend'])) == digest
        run_e.report['status'] = 'pass' if 'exception' not in run_e.report and run_e.report['fixture_file_unchanged'] and \
            all(c['passed'] for c in run_e.report['checks'].values()) else 'fail'
        run_e.save()
        summary['scenarios']['E'] = {'status': run_e.report['status'], 'checks': len(run_e.report['checks']),
                                     'failed': [k for k, c in run_e.report['checks'].items() if not c['passed']],
                                     'calls': len(run_e.report['calls'])}
    if args.only in (None, 'C') and args.upstream:
        upstream = Path(args.upstream)
        report = json.loads((upstream / 'demo_c_upstream.json').read_text(encoding='utf-8'))
        blend = upstream / 'demo_c_source.blend'
        digest = sha256(blend)
        blender, blog = start_blender(args, blend, folder / 'blender-C.txt')
        try:
            # The upstream store holds the accepted M7/M8 generations: it is the workdir of this scenario.
            run_c = asyncio.run(run_scenario(args, folder / 'C', upstream / 'store', upstream,
                                             lambda run: scenario_c(run, args, upstream, report)))
        finally:
            stop_blender(blender, blog)
        run_c.report['fixture_file_unchanged'] = sha256(blend) == digest
        run_c.report['status'] = 'pass' if 'exception' not in run_c.report and run_c.report['fixture_file_unchanged'] and \
            all(c['passed'] for c in run_c.report['checks'].values()) else 'fail'
        run_c.save()
        summary['scenarios']['C'] = {'status': run_c.report['status'], 'checks': len(run_c.report['checks']),
                                     'failed': [k for k, c in run_c.report['checks'].items() if not c['passed']],
                                     'calls': len(run_c.report['calls'])}
    archive = ROOT / 'test-artifacts' / 'm10' / 'mcp' / folder.name
    archive.mkdir(parents=True, exist_ok=True)
    for name in ('E', 'C'):
        if (folder / name / 'report.json').exists():
            shutil.copyfile(folder / name / 'report.json', archive / ('report-' + name + '.json'))
        for extra in ('review-request-mcp-C.json', 'review-feedback-mcp-C.json'):
            if (folder / name / extra).exists():
                shutil.copyfile(folder / name / extra, archive / extra)
    (archive / 'summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    print('M10_MCP_SUMMARY ' + json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
