"""Controlled M8 stdio/socket qualification; only the coordinator executes it.

Own factory fixtures, own hidden GUI Blender, a dedicated non-9876 socket and one
controller at a time. Every paired temporal preview requires external, real visual
feedback. No feedback, technical failure or missing measurement can become PASS.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from uuid import uuid4

from run_m5_mcp import Run, session, listening
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.storage import sha256 as sha256_file


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_TOOLS = {
    'physics_inspect_character', 'physics_classify_secondary', 'physics_compatibility_report',
    'physics_create_brief', 'physics_plan', 'physics_prepare_colliders', 'physics_analyze_cloth',
    'physics_plan_cloth_pins', 'physics_setup_cloth', 'physics_simulate_cloth', 'physics_analyze_penetration',
    'physics_analyze_hair', 'physics_plan_hair', 'physics_generate_hair_secondary',
    'physics_setup_spring_chain', 'physics_generate_secondary_bones', 'physics_analyze_accessory',
    'physics_generate_accessory_motion', 'physics_analyze_soft_region', 'physics_generate_soft_secondary',
    'physics_verify_primary_integrity', 'physics_cache_plan', 'physics_bake_cache', 'physics_bake_secondary_motion',
    'physics_collision_report', 'physics_analyze_motion', 'physics_render_preview', 'physics_compare_before_after',
    'physics_refine', 'physics_accept', 'physics_reject',
}
EXTRA_TOOLS = {'physics_create_candidate', 'physics_status', 'physics_review', 'physics_analyze_stability'}
NEW_TOOLS = REQUIRED_TOOLS | EXTRA_TOOLS
VIEWS = ['FRONT', 'SIDE', 'THREE_QUARTER']
DEMOS = (
    ('cloth_demo', 'cloth_heavy'), ('hair_supported', 'hair_bone_cards'),
    ('accessory_demo', 'dangling_accessory'), ('creature_soft_demo', 'soft_region'),
    ('combined_body_face', 'body_face_secondary'), ('spring_chain_check', 'spring_chain'),
)
BUILD = """
import sys
sys.path.insert(0, ROOT + '/tests/blender')
import fixtures_m8
fixtures_m8.build(output, variant, fps=fps, duration_s=duration_s, save=True)
"""


def sha256(path):
    """Runner JSON paths are strings; the shared storage function expects Path."""
    return sha256_file(Path(path))


def record_failure(folder,phase,error,infos,blender=None):
    failure={'status':'fail','phase':phase,'exception':repr(error),
        'traceback':traceback.format_exc(),'workdir':str(folder),
        'blender_gui_started':blender is not None,'blender_pid':blender.pid if blender else None,
        'fixture_builder_metadata':infos,'production_assets_used':False}
    path=folder/'runner-failure.json'
    if path.exists():
        path=folder/('runner-failure-'+uuid4().hex[:8]+'.json')
    _json(path,failure)
    return str(path)


class M8Run(Run):
    def __init__(self,folder):
        super().__init__(folder);self.key_tools=set()

    async def call(self,name,arguments=None):
        values=dict(arguments or {})
        if name in self.key_tools and 'operation_id' not in values:
            values['operation_id']='m8-'+name.removeprefix('physics_')+'-'+uuid4().hex[:12]
        return await super().call(name,values)


def hidden_startup():
    if os.name != 'nt':
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def fixture_options(info):
    result = {key: info[key] for key in ('primary_objects', 'colliders', 'anchor_object', 'anchor_bone',
                                       'pin_group', 'chain') if info.get(key) is not None}
    result['manifest'] = info['manifest']
    return result


def measured_signatures(value):
    """Keep primary/raw content evidence, independently of presentation nesting."""
    wanted = {'source_fingerprint', 'primary_fingerprint', 'dependency_fingerprint',
              'context_fingerprint', 'primary_actions'}
    result = {}
    def visit(item, path='data'):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in wanted:
                    result[path+'.'+key] = child
                else:
                    visit(child, path+'.'+key)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, path+'.'+str(index))
    visit(value)
    return result


def _json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding='utf-8')


def _state(data):
    return data.get('state', data.get('status'))


def _render(data):
    return data.get('render', data)


def require_candidate(data):
    candidate = data.get('candidate')
    if not isinstance(candidate, dict) or not candidate.get('candidate_object'):
        raise AssertionError('PHYSICS_CANDIDATE_RESULT_REQUIRED: expected an owned candidate manifest')
    return candidate


def validate_feedback(feedback, artifact_reference):
    if not isinstance(feedback, dict):
        raise ValueError('Visual feedback must be a JSON object')
    required = {'reviewer', 'observations', 'findings', 'decision', 'artifact_reference', 'temporal_reviewed',
                'views_reviewed', 'speeds_reviewed', 'technical_findings', 'artistic_observations'}
    if required-set(feedback):
        raise ValueError('Real visual feedback is incomplete: '+', '.join(sorted(required-set(feedback))))
    if not isinstance(feedback['reviewer'], str) or not feedback['reviewer'].strip():
        raise ValueError('A real reviewer identity is required')
    if feedback['decision'] not in ('ACCEPT', 'REJECT') or feedback['temporal_reviewed'] is not True:
        raise ValueError('Review every full temporal sequence before deciding')
    if set(feedback['views_reviewed']) != set(VIEWS) or set(feedback['speeds_reviewed']) != {1, .25}:
        raise ValueError('All three views at normal and quarter speed require real review')
    if feedback['artifact_reference'] != artifact_reference:
        raise ValueError('Visual feedback refers to a different preview artifact')
    if not feedback['observations'] or not feedback['artistic_observations']:
        raise ValueError('Real observations are required; an empty acceptance template is not evidence')
    return feedback


async def visual_review(run, label, rendered, timeout_s):
    render = _render(rendered)
    artifact = render.get('output_dir')
    series=render.get('series',render.get('animations',[]))
    if not artifact or not render.get('temporal_complete') or len(series) != 6:
        raise AssertionError('COMPLETE_PAIRED_TEMPORAL_PREVIEW_REQUIRED')
    if not Path(render.get('player', '')).is_file():
        raise AssertionError('TEMPORAL_PLAYER_MISSING')
    token = uuid4().hex[:8]
    feedback = run.folder / ('review-feedback-'+label+'-'+token+'.json')
    request = run.folder / ('visual-request-'+label+'-'+token+'.json')
    _json(request, {'label': label, 'render': render, 'feedback': str(feedback),
        'instructions': 'Inspect PRIMARY_ONLY and PRIMARY_PLUS_SECONDARY full sequences in FRONT, SIDE and THREE_QUARTER at speeds 1 and 0.25. Record real technical findings separately from artistic observations. Do not invent acceptance.',
        'required_feedback': {'reviewer': '', 'observations': [], 'findings': [], 'decision': '',
            'artifact_reference': artifact, 'temporal_reviewed': False, 'views_reviewed': [], 'speeds_reviewed': [],
            'technical_findings': [], 'artistic_observations': []}})
    run.report.setdefault('visual_review_requests', []).append(str(request)); run.save()
    print('M8_VISUAL_REVIEW_REQUEST='+str(request), flush=True)
    deadline = time.monotonic()+timeout_s
    while time.monotonic() < deadline:
        if feedback.is_file():
            result = validate_feedback(json.loads(feedback.read_text(encoding='utf-8')), artifact)
            run.report.setdefault('visual_feedback', []).append({'path': str(feedback), 'sha256': sha256(feedback), **result})
            run.save()
            return result
        await asyncio.sleep(2)
    raise TimeoutError('REAL_VISUAL_REVIEW_PENDING: request '+str(request)+'; no acceptance was inferred')


async def rejection(run, name, arguments, fragment):
    result = await run.call(name, arguments)
    text = json.dumps(result.get('errors', []))
    passed = result['status'] == 'error' and fragment in text
    run.check(name+'_rejected_'+str(len(run.report['calls'])), passed, expected=fragment, errors=result.get('errors'))
    if not passed:
        raise AssertionError(name+' did not safely reject '+fragment+': '+text[:1000])
    return result


async def inventory(run):
    result = await run.client.list_tools()
    lookup = {tool.name: tool for tool in result.tools}
    run.key_tools={name.removeprefix('production.') for name,tool in lookup.items()
        if (tool.meta or {}).get('production',{}).get('accepts_operation_id')}
    wanted = {'production.'+name for name in NEW_TOOLS}
    run.check('M8_inventory_236_with_35_new_tools', len(lookup) == 236 and wanted <= set(lookup),
              total=len(lookup), expected_new=35, missing=sorted(wanted-set(lookup)))
    run.report['tool_inventory'] = sorted(lookup)
    for name in wanted:
        listed = lookup[name]
        props = listed.inputSchema['properties']
        run.check('M8_schema_'+name, listed.outputSchema is not None and '_operation_id' not in props
                  and props.get('apply', {}).get('default') is False and listed.annotations.openWorldHint is False)


async def register_fixture(run, info):
    await run.good('asset_register_fixture', {'path': info['source_path']})
    status = await run.good('asset_status')
    run.check('fixture_role_'+info['variant'], status['asset']['role'] == 'FIXTURE')


async def plan_and_candidate(run, info, label, brief=None):
    source = info['source_object']; options = fixture_options(info)
    inspected = await run.good('physics_inspect_character', {'object_name': source, 'options': options})
    await run.good('physics_classify_secondary', {'object_name': source, 'options': options})
    await run.good('physics_compatibility_report', {'object_name': source, 'options': options})
    intent = 'Controlled '+label+' secondary motion; preserve the authored body and facial performance'
    authored = dict(info['brief'] if brief is None else brief)
    await run.good('physics_create_brief', {'intent': intent, 'brief': authored})
    planned = await run.good('physics_plan', {'object_name': source, 'intent': intent, 'brief': authored,
        'options': options, 'apply': False, 'operation_id': 'm8-plan-'+uuid4().hex[:12]})
    ref = planned['physics']
    run.check('plan_no_Action_'+label, _state(planned) == 'PLANNED' and planned.get('no_action_created') is True)
    run.report.setdefault('plans', {})[label] = ref
    await run.good('physics_status', {'physics': ref})
    arguments = {'physics': ref, 'apply': True, 'operation_id': 'm8-create-'+uuid4().hex[:12]}
    first = await run.call('physics_create_candidate', arguments)
    retry = await run.call('physics_create_candidate', arguments)
    if first['status'] == 'error':
        raise AssertionError('Candidate creation failed: '+json.dumps(first['errors'])[:1000])
    candidate = require_candidate(first['data'])
    run.check('candidate_owned_idempotent_'+label, retry['status'] != 'error'
        and first['execution_id'] == retry['execution_id'] and candidate == retry['data'].get('candidate'), candidate=candidate)
    run.report.setdefault('candidates', {})[label] = candidate
    await run.good('physics_prepare_colliders', {'physics': ref, 'apply': True})
    return ref, candidate, inspected


async def generate(run, ref, info):
    common = {'physics': ref}
    subsystem = info['brief']['subsystem']
    if subsystem == 'CLOTH':
        await run.good('physics_analyze_cloth', common)
        await run.good('physics_plan_cloth_pins', common)
        await run.good('physics_setup_cloth', dict(common, apply=True))
        return await run.good('physics_simulate_cloth', dict(common, apply=True))
    if subsystem == 'HAIR':
        await run.good('physics_analyze_hair', common)
        await run.good('physics_plan_hair', common)
        return await run.good('physics_generate_hair_secondary', dict(common, apply=True))
    if subsystem in ('SPRING_BONE', 'CHAIN', 'CABLE'):
        await run.good('physics_setup_spring_chain', dict(common, apply=True))
        return await run.good('physics_generate_secondary_bones', dict(common, apply=True))
    if subsystem == 'SOFT':
        await run.good('physics_analyze_soft_region', common)
        return await run.good('physics_generate_soft_secondary', dict(common, apply=True))
    await run.good('physics_analyze_accessory', common)
    return await run.good('physics_generate_accessory_motion', dict(common, apply=True))


async def qa_and_cache(run, ref, label):
    integrity_data = await run.good('physics_verify_primary_integrity', {'physics': ref})
    integrity=integrity_data['integrity']
    run.check('primary_body_face_integrity_'+label, integrity.get('passed') is True, result=integrity)
    measurements = {}
    for name in ('physics_analyze_penetration', 'physics_collision_report', 'physics_analyze_motion', 'physics_analyze_stability'):
        measurements[name] = await run.good(name, {'physics': ref})
    await run.good('physics_cache_plan', {'physics': ref})
    cached = await run.good('physics_bake_cache', {'physics': ref, 'apply': True})
    baked = await run.good('physics_bake_secondary_motion', {'physics': ref, 'apply': True})
    deviation = baked.get('bake_deviation')
    run.check('measured_bake_deviation_'+label, isinstance(deviation, dict) and deviation.get('passed') is True,
              bake_deviation=deviation)
    run.report.setdefault('measurements', {})[label] = {'integrity': integrity, 'qa': measurements,
                                                     'cache': cached, 'bake': baked}
    return integrity


async def demo(run, info, label, args):
    await register_fixture(run, info)
    ref, candidate, before = await plan_and_candidate(run, info, label)
    simulated = await generate(run, ref, info)
    qa = simulated.get('technical_qa', {})
    run.check('simulation_technical_qa_'+label, _state(simulated) == 'SIMULATED' and qa.get('passed') is True,
              state=_state(simulated), technical_qa=qa)
    if qa.get('passed') is not True:
        raise AssertionError('Physics technical QA failed; '+label+' cannot be accepted')
    integrity = await qa_and_cache(run, ref, label)
    settings={'width':args.preview_size,'height':args.preview_size,'views':VIEWS}
    await run.good('physics_render_preview', {'physics': ref,'options': settings})
    # Comparison produces the final paired artifact and invalidates the first render's review.
    preview=await run.good('physics_compare_before_after', {'physics': ref,'options':settings})
    feedback = await visual_review(run, label, preview, args.review_timeout)
    reviewed = await run.good('physics_review', {'physics': ref, 'options': {'review': feedback}})
    run.report.setdefault('reviews', {})[label] = reviewed
    if feedback['decision'] == 'ACCEPT':
        accepted = await run.good('physics_accept', {'physics': ref, 'apply': True})
        run.check('accepted_'+label, _state(accepted) == 'ACCEPTED', result=accepted)
    else:
        rejected = await run.good('physics_reject', {'physics': ref, 'apply': True})
        run.check('real_visual_rejection_recorded_'+label, _state(rejected) == 'REJECTED', result=rejected)
        run.check('accepted_'+label, False, reason='Real reviewer rejected this demo')
    after=await run.good('physics_verify_primary_integrity', {'physics':ref})
    actual=after['integrity']
    plan=json.loads(Path(ref).read_text(encoding='utf-8'))
    signatures_before=measured_signatures(plan['source_status'])
    signatures_after={'source_fingerprint':actual.get('source_fingerprint_after'),'evaluated_primary':actual.get('after')}
    unchanged=bool(signatures_before) and actual.get('passed') is True and actual.get('body_and_face_unchanged') is True \
        and actual.get('source_fingerprint_before')==actual.get('source_fingerprint_after') \
        and actual.get('before')==actual.get('after')
    audit = {'label': label, 'source': info['source_path'], 'physics': ref, 'candidate': candidate,
        'before': signatures_before, 'after': signatures_after, 'unchanged': unchanged,
        'raw_source_stack_before':plan['source_status']['fingerprint'],
        'raw_source_stack_after':actual.get('source_fingerprint_after'),
        'evaluated_primary_before':actual.get('before'),'evaluated_primary_after':actual.get('after'),
        'primary_actions_before':plan['source_status']['primary_actions'],
        'scope':'Complete raw body/facial Action/NLA and asset signatures; evaluated primary sampled at the plan frames',
        'integrity_tool_result': actual}
    _json(run.folder/('primary-body-face-audit-'+label+'.json'), audit)
    run.check('source_body_face_signatures_unchanged_'+label, unchanged, audit=str(run.folder/('primary-body-face-audit-'+label+'.json')))
    return ref


async def refinement_limit(run, info, args):
    """Three authorized parameter experiments, each with an honest temporal review.

    Review may accept or reject visual quality. Neither decision permits a fourth
    cycle, and this test never fabricates artistic rejection to exercise a guard.
    The experimental candidate is retired separately from its review decision.
    """
    await register_fixture(run, info)
    brief = json.loads(json.dumps(info['brief']))
    ref, candidate, before = await plan_and_candidate(run, info, 'refinement-limit', brief)
    initial = await generate(run, ref, info)
    run.report['refinement_initial'] = initial
    if initial.get('technical_qa',{}).get('passed') is not True:
        raise AssertionError('REFINEMENT_REQUIRES_QUALIFIED_START: a failed simulation cannot be made reviewable by this test')
    attempts = []
    for cycle, damping in enumerate((12., 20., 28.), 1):
        preview = await run.good('physics_render_preview', {'physics': ref,
            'options': {'width': args.preview_size, 'height': args.preview_size, 'views': VIEWS}})
        feedback = await visual_review(run, 'refinement-'+str(cycle), preview, args.review_timeout)
        await run.good('physics_review', {'physics': ref, 'options': {'review': feedback}})
        if cycle == 1:
            await reject_ambiguous_refinement_targets(run, ref)
        result = await run.good('physics_refine', {'physics': ref, 'apply': True,
            'options': {'target': 'max_velocity_m_s', 'settings': {'damping': damping}},
            'operation_id': 'm8-refine-'+uuid4().hex[:12]})
        attempts.append(result)
        run.report['refinement_attempts'] = attempts; run.save()
    # The last correction is reviewed, too; no replay of earlier feedback.
    preview = await run.good('physics_render_preview', {'physics': ref,
        'options': {'width': args.preview_size, 'height': args.preview_size, 'views': VIEWS}})
    feedback = await visual_review(run, 'refinement-limit-final', preview, args.review_timeout)
    await run.good('physics_review', {'physics': ref, 'options': {'review': feedback}})
    await rejection(run, 'physics_refine', {'physics': ref, 'apply': True,
        'options': {'target': 'max_velocity_m_s', 'settings': {'damping': 36.}}}, 'REFINEMENT_LIMIT')
    rejected = await run.good('physics_reject', {'physics': ref, 'apply': True})
    run.check('refinement_stops_after_three_and_preserves_candidate', len(attempts) == 3 and _state(rejected) == 'REJECTED',
              attempts=attempts, final=rejected)


def checkpoint_inventory(folder):
    root = Path(folder) / 's' / 'checkpoints'
    return {p.relative_to(root).as_posix(): sha256(p)
            for p in sorted(root.rglob('*')) if p.is_file()} if root.is_dir() else {}


def refinement_state(status):
    # Freeze values before the next call; compare the same generation/candidate
    # and its counter, including cached/baked state that must remain reviewable.
    return deepcopy({key: status.get(key) for key in (
        'physics', 'generation_id', 'state', 'version', 'refinement_cycles',
        'candidate', 'candidate_action', 'checkpoint', 'cache', 'bake_deviation',
        'pending_refinement', 'fresh')})


async def reject_ambiguous_refinement_targets(run, ref):
    """Invalid objective direction must fail before consuming a refinement cycle."""
    records = run.report.setdefault('unsupported_refinement_target_checks', [])
    for target in ('min_area_ratio', 'absolute_volume_ratio', 'bbox_growth_ratio'):
        before = refinement_state(await run.good('physics_status', {'physics': ref}))
        if before['fresh'] is not True or before['state'] != 'REVIEWED':
            raise AssertionError('REFINEMENT_NEGATIVE_REQUIRES_FRESH_REVIEWED_CANDIDATE')
        scene_before = await run.good('scene_audit', {'detail': 'summary'})
        checkpoints_before = checkpoint_inventory(run.folder)
        result = await rejection(run, 'physics_refine', {
            'physics': ref, 'apply': True,
            'options': {'target': target, 'settings': {'damping': 12.}},
            'operation_id': 'm8-invalid-refine-'+uuid4().hex[:12]},
            'PHYSICS_REFINEMENT_TARGET_UNSUPPORTED')
        after = refinement_state(await run.good('physics_status', {'physics': ref}))
        scene_after = await run.good('scene_audit', {'detail': 'summary'})
        checkpoints_after = checkpoint_inventory(run.folder)
        totals_before = {k: scene_before['totals'][k] for k in ('objects', 'actions')}
        totals_after = {k: scene_after['totals'][k] for k in ('objects', 'actions')}
        unchanged = (before == after and totals_before == totals_after
                     and checkpoints_before == checkpoints_after)
        evidence = {'target': target, 'physics': ref, 'state_before': before,
            'state_after': after, 'scene_before': totals_before, 'scene_after': totals_after,
            'checkpoints_before': checkpoints_before, 'checkpoints_after': checkpoints_after,
            'error': result.get('errors'), 'unchanged': unchanged}
        records.append(evidence)
        run.check('unsupported_refinement_target_precedes_mutation_'+target, unchanged,
                  target=target, cycles_before=before['refinement_cycles'],
                  cycles_after=after['refinement_cycles'], scene_before=totals_before,
                  scene_after=totals_after, checkpoint_files_before=len(checkpoints_before),
                  checkpoint_files_after=len(checkpoints_after))
        if not unchanged:
            raise AssertionError('INVALID_REFINEMENT_TARGET_MUTATED_STATE: '+target)


def switch_owned_fixture(port, current_path, next_path):
    """Short bridge only between stdio sessions: no simultaneous controllers."""
    bridge = Bridge(host='localhost', port=port)
    try:
        result = bridge.run("import bpy\n"
            "if bpy.data.filepath != params['current']:\n    raise ValueError('OWN_FIXTURE_REQUIRED: no unrelated file may be opened')\n"
            "outcome=bpy.ops.wm.open_mainfile(filepath=params['next'],load_ui=False,use_scripts=False)\n"
            "for scene in bpy.data.scenes:\n    scene.blendermcp_port=params['port']\n"
            "result={'opened': 'FINISHED' in outcome and bpy.data.filepath==params['next']}\n",
            {'current': str(current_path), 'next': str(next_path), 'port':port})
        if not result.get('opened'):
            raise AssertionError('OWN_FIXTURE_SWITCH_FAILED')
    finally:
        bridge.close()


def make_master_copy(port, fixture, path):
    bridge = Bridge(host='localhost', port=port)
    try:
        result = bridge.run("import bpy\n"
            "if bpy.data.filepath != params['fixture']:\n    raise ValueError('OWN_FIXTURE_REQUIRED: only the generated fixture may be copied')\n"
            "outcome=bpy.ops.wm.save_as_mainfile(filepath=params['path'],copy=True)\n"
            "if 'FINISHED' not in outcome:\n    raise ValueError('OWN_MASTER_COPY_FAILED')\n"
            "outcome=bpy.ops.wm.open_mainfile(filepath=params['path'],load_ui=False,use_scripts=False)\n"
            "for scene in bpy.data.scenes:\n    if 'production_asset' in scene:\n        del scene['production_asset']\n"
            "    scene.blendermcp_port=params['port']\n"
            "result={'opened': 'FINISHED' in outcome and bpy.data.filepath==params['path']}\n",
            {'fixture': str(fixture), 'path': str(path), 'port':port})
        if not result.get('opened'):
            raise AssertionError('OWN_MASTER_COPY_FAILED')
    finally:
        bridge.close()


async def master_negative(run, info):
    await run.good('asset_register_master', {'confirm_master': True})
    status = await run.good('asset_status')
    run.check('negative_MASTER_role', status['asset']['role'] == 'MASTER')
    audit_before=await run.good('scene_audit', {'detail':'summary'})
    planned = await run.good('physics_plan', {'object_name': info['source_object'], 'brief': info['brief'],
        'intent': 'Controlled MASTER negative, no mutation authorized', 'options': fixture_options(info), 'apply': False})
    checkpoint_dir = run.folder/'s'/'checkpoints'
    before = sorted(str(p) for p in checkpoint_dir.rglob('*') if p.is_file()) if checkpoint_dir.exists() else []
    await rejection(run, 'physics_create_candidate', {'physics': planned['physics'], 'apply': True}, 'MASTER_ASSET_READ_ONLY')
    after = sorted(str(p) for p in checkpoint_dir.rglob('*') if p.is_file()) if checkpoint_dir.exists() else []
    run.check('MASTER_negative_precedes_checkpoint', before == after)
    audit_after=await run.good('scene_audit', {'detail':'summary'})
    run.check('MASTER_negative_creates_no_Action_or_object', audit_before['totals']['actions']==audit_after['totals']['actions']
        and audit_before['totals']['objects']==audit_after['totals']['objects'],before=audit_before['totals'],after=audit_after['totals'])


async def one_session(run, args, name, callback):
    with (run.folder/(name+'-server-stderr.txt')).open('w', encoding='utf-8') as log:
        async with session(run.folder, args.port, log) as client:
            run.client = client
            await callback()
    run.client = None


async def main_async(args, folder, infos):
    run = M8Run(folder); current = Path(infos[DEMOS[0][1]]['source_path'])
    try:
        for index, (label, variant) in enumerate(DEMOS):
            info = infos[variant]
            if index:
                switch_owned_fixture(args.port, current, info['source_path']); current = Path(info['source_path'])
            async def callback(info=info, label=label, index=index):
                if index == 0:
                    await inventory(run)
                await demo(run, info, label, args)
            await one_session(run, args, label, callback)
        if not args.only_demo:
            info = infos['dangling_accessory']
            switch_owned_fixture(args.port, current, info['source_path']); current = Path(info['source_path'])
            await one_session(run, args, 'refinement', lambda: refinement_limit(run, info, args))
            master = folder/'f'/'master-negative.blend'
            make_master_copy(args.port, current, master); current = master
            master_digest = sha256(master)
            await one_session(run, args, 'master-negative', lambda: master_negative(run, info))
            run.check('negative_MASTER_file_unchanged', sha256(master) == master_digest)
            invoked = {entry['tool'] for entry in run.report['calls']}
            wanted = {'production.'+name for name in NEW_TOOLS}
            run.check('all_35_M8_tools_invoked', wanted <= invoked, missing=sorted(wanted-invoked))
        else:
            run.report['qualification_scope']='TARGETED_DEMO_ONLY: no claim of refinement, MASTER or all-tools execution'
    except Exception as exc:
        run.report['exception'] = repr(exc)[:3000]; run.save()
        print('M8_EXCEPTION='+repr(exc)[:1200], flush=True)
    return run


def main():
    global DEMOS
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--port', type=int, default=9877)
    parser.add_argument('--fps', type=int, choices=(30, 60), default=30)
    parser.add_argument('--duration', type=float, default=2.)
    parser.add_argument('--preview-size', type=int, default=192)
    parser.add_argument('--review-timeout', type=float, default=3600.)
    parser.add_argument('--only-demo',choices=[label for label,variant in DEMOS],default=None)
    args = parser.parse_args()
    if args.only_demo:
        DEMOS=tuple(row for row in DEMOS if row[0]==args.only_demo)
    if args.port == 9876 or not 1024 <= args.port <= 65535:
        raise SystemExit('Refusing reserved/invalid port; use an owned loopback port')
    if listening(args.port):
        raise SystemExit('Dedicated test port is in use; no existing Blender is stopped or contacted')
    if not 1 <= args.duration <= 10 or not 64 <= args.preview_size <= 512 or args.review_timeout <= 0:
        raise SystemExit('Invalid controlled duration, preview size or feedback timeout')
    folder = Path(tempfile.gettempdir())/('bpm8-'+uuid4().hex[:8]); (folder/'f').mkdir(parents=True)
    infos={};source_hashes={};combined_sources={};run=M8Run(folder);blender=None;phase='BUILD_FIXTURES';variant=None
    try:
        for _, variant in DEMOS:
            expression = 'from pathlib import Path\nROOT='+repr(ROOT.as_posix())+'\noutput=Path('+repr(str(folder/'f'))+')\n'
            expression += 'variant='+repr(variant)+'\nfps='+repr(args.fps)+'\nduration_s='+repr(args.duration)+'\n'+BUILD
            built = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python-expr', expression],
                check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180,
                startupinfo=hidden_startup())
            (folder/(variant+'-build.log')).write_text(built.stdout+'\n'+built.stderr, encoding='utf-8')
            infos[variant] = json.loads((folder/'f'/(variant+'.json')).read_text(encoding='utf-8'))
        phase='BUILD_SOURCEHASH'
        for name,info in infos.items():
            source_hashes[name]=sha256(info['source_path'])
        combined_sources = {info['combined_provenance']['source']: info['combined_provenance']['source_sha256_before']
                            for info in infos.values() if info.get('combined_provenance')}
        fixture=Path(infos[DEMOS[0][1]]['source_path']);phase='GUI_STARTUP'
        with (folder/'blender-stdout.txt').open('w', encoding='utf-8') as log:
            blender = subprocess.Popen([args.blender, '--factory-startup', '--disable-autoexec', str(fixture), '--python',
                str(ROOT/'tests'/'integration'/'m4_own_blender_start.py'), '--', str(args.port)],
                stdout=log, stderr=subprocess.STDOUT, startupinfo=hidden_startup())
            deadline = time.monotonic()+90
            while not listening(args.port):
                if time.monotonic()>deadline or blender.poll() is not None:
                    raise RuntimeError('Own controlled Blender did not start its add-on')
                time.sleep(.5)
            phase='MCP_QUALIFICATION';run = asyncio.run(main_async(args, folder, infos))
    except Exception as exc:
        run.report.update(exception=repr(exc)[:3000],failure_phase=phase,
            failure_record=record_failure(folder,phase,exc,infos,blender))
        if phase=='BUILD_FIXTURES' and variant:
            outputs=[]
            for value in (getattr(exc,'stdout',None),getattr(exc,'stderr',None)):
                if value is not None:
                    outputs.append(value.decode('utf-8',errors='replace') if isinstance(value,bytes) else str(value))
            (folder/(variant+'-failed-build.log')).write_text('\n'.join(outputs),encoding='utf-8')
        run.check('runner_phase_'+phase,False,error=repr(exc))
    finally:
        # Only this Popen handle is terminated; no enumeration or unrelated PID.
        if blender is not None and blender.poll() is None:
            blender.terminate()
            try:
                blender.wait(timeout=30)
            except subprocess.TimeoutExpired:
                blender.kill(); blender.wait(timeout=30)
    try:
        files_after = {name: sha256(info['source_path']) for name, info in infos.items()}
        complete=bool(source_hashes) and set(source_hashes)==set(files_after)
        run.check('all_original_fixture_SHA256_unchanged', complete and source_hashes == files_after,
                  before=source_hashes, after=files_after)
        combined_after = {path: sha256(path) for path in combined_sources}
        run.check('genuine_M6_M7_fixture_sources_unchanged', bool(combined_sources) and combined_sources == combined_after,
                  before=combined_sources, after=combined_after)
    except Exception as exc:
        run.report.update(exception=repr(exc)[:3000],verification_failure_record=record_failure(folder,'VERIFY_SOURCEHASH',exc,infos,blender))
        run.check('source_hash_verification_completed',False,error=repr(exc))
    run.report.update(blender_port=args.port, blender_pid_started_and_closed_here=blender.pid if blender else None,
        fixture_sources=infos, workdir=str(folder), user_port_9876_untouched=True,blender_gui_started=blender is not None,
        limitations=['Controlled fixture qualification; no real production character',
            'Visual review comes from feedback files and is never synthesized',
            'Independent real Blender domain probes cover deliberate source/cache mutation negatives'])
    run.report['status'] = 'pass' if 'exception' not in run.report and run.report['checks'] and all(c['passed'] for c in run.report['checks'].values()) else 'fail'
    run.save()
    archive = ROOT/'test-artifacts'/'m8'/folder.name; archive.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(folder/'report.json', archive/'report.json')
    print('M8_MCP_REPORT='+str(archive/'report.json')+' workdir='+str(folder)+' status='+run.report['status'], flush=True)
    raise SystemExit(0 if run.report['status']=='pass' else 1)


if __name__ == '__main__':
    main()
