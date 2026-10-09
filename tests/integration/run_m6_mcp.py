"""M6 stdio/socket acceptance with an owned fixture and no contact with port 9876."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from urllib.parse import quote

from run_m5_mcp import Run, session, listening
from blender_mcp.production.common.storage import sha256
from blender_mcp.production.animation.generation.tools import NAMES
from blender_mcp.production.common.bridge import Bridge
from blender_mcp.production.common.m3_transaction import BASE_RUNTIME
from copy import deepcopy

ROOT = Path(__file__).resolve().parents[2]


async def visual_review(run, generation, render, label):
    token = uuid4().hex[:8]
    feedback = run.folder / ('review-feedback-'+token+'.json')
    relative = Path(render['output_dir']).relative_to(run.folder)
    request = dict(label=label, generation=generation, render=render, feedback=str(feedback),
                   url='http://127.0.0.1:8768/'+quote(relative.as_posix())+'/preview.html')
    path = run.folder / ('visual-request-'+token+'.json')
    path.write_text(json.dumps(request, indent=2), encoding='utf-8')
    print('VISUAL_REVIEW_REQUEST='+str(path), flush=True)
    deadline = time.time()+1800
    while not feedback.exists():
        if time.time()>deadline: raise TimeoutError('Temporal review was not supplied; candidate remains unaccepted')
        await asyncio.sleep(1)
    review = json.loads(feedback.read_text(encoding='utf-8-sig'))
    run.report.setdefault('visual_reviews', []).append(dict(request=str(path), review=review))
    return review


def fixture_options(spec):
    # Controlled fixtures put the sampled foot marker on the sole plane.
    # The 4 cm swing is intentionally low: the general 3 cm proximity default
    # would classify airborne samples as suspected contact. Use a declared
    # 5 mm plane proximity and 2 cm/s vertical speed for these fixtures only;
    # retain the independent 1 cm sliding failure threshold.
    return {'mapping': spec['mapping'], 'ground_height_m': spec['ground_height_m'],
            'meshes': [spec['body']], 'contact_height_m': .005,
            'contact_speed_m_s': .02, 'sliding_tolerance_m': .01}


def control(run, info, body, params=None):
    # This bridge is exclusive to the runner's owned process/port. All fixture
    # setup is guarded against accidentally contacting a different file.
    return run.bridge.run("import bpy\nif bpy.data.filepath!=params['fixture']: raise ValueError('OWNED_FIXTURE_REQUIRED')\n"+body,
                          dict(params or {}, fixture=info['blend']))


def activate(run, info, scenario):
    spec = info['scenarios'][scenario]
    return control(run, info, r'''
for name in params['bodies']:
    obj=bpy.data.objects[name];obj.hide_render=name!=params['body'];obj.hide_set(name!=params['body'])
obj=bpy.data.objects[params['object']]
for other in bpy.context.selected_objects: other.select_set(False)
obj.hide_set(False);obj.select_set(True);bpy.context.view_layer.objects.active=obj
bpy.context.scene.frame_set(1)
result={'active_object':obj.name,'action':obj.animation_data.action.name}
''', {'body':spec['body'], 'object':spec['object'], 'bodies':[s['body'] for s in info['scenarios'].values()]})


async def negative(run, name, arguments, marker):
    result = await run.call(name, arguments)
    content = json.dumps(result, default=str)
    passed = (result.get('status') == 'error' or result.get('data', {}).get('status') in {'unsupported','error'}) and marker in content
    run.check('safe_rejection_'+marker+'_'+str(len(run.report['calls'])), passed, response=result)
    if not passed: raise AssertionError('Expected safe rejection '+marker)
    run.report.setdefault('rejected_operations', []).append({'tool':name,'marker':marker,'status':result['status']})
    return result


async def demo(run, info, label, scenario, intent, detailed=False):
    activate(run, info, scenario)
    spec = info['scenarios'][scenario]
    options = fixture_options(spec)
    values = dict(intent, fps=30, forward_axis='-Y')
    record = {'intent':values,'fixture':scenario,'stages':[],'observations':{}}
    run.report.setdefault('demos', {})[label] = record
    brief = await run.good('animation_create_brief', {'intent':values})
    record['brief'] = brief
    comp = await run.good('animation_generation_compatibility', {'object_name':spec['object'],'options':options})
    run.check(label+'_compatible', comp.get('supported'), report=comp)
    await run.good('animation_plan_poses', {'object_name':spec['object'],'brief':brief['brief'],'options':options})
    plan = await run.good('animation_generation_plan', {'object_name':spec['object'],'intent':values,'options':options})
    generation = plan['generation']; record['generation'] = generation
    run.check(label+'_planning_nonmutating', plan['no_action_created'] and plan['supported'])
    planned = await run.good('animation_generate', {'generation':generation})
    run.check(label+'_generate_dry_run', planned.get('no_action_created') is True)
    if detailed:
        pose = await run.good('animation_generate_pose', {'generation':generation, 'options':{'pose_index':0},'apply':True})
        run.check(label+'_pose_new_action', pose['status']=='candidate_created' and pose['source_intact'])
        await negative(run,'animation_spline',{'generation':generation,'apply':True},'BLOCKING_REVIEW_REQUIRED')
        blocking = await run.good('animation_generate_blocking', {'generation':generation,'apply':True})
        rendered = await run.good('animation_generation_render', {'generation':generation})
        blocking = dict(blocking, render=rendered['render'])
    else:
        blocking = await run.good('animation_generate', {'generation':generation,'apply':True})
        run.check(label+'_blocking_requires_review', blocking['status']=='blocking_review_required')
    record['blocking'] = blocking
    run.check(label+'_blocking_technical', blocking['technical_qa']['passed'], qa=blocking['technical_qa'])
    if not blocking['technical_qa']['passed']: raise AssertionError('Blocking QA failed '+label)
    await negative(run,'animation_spline',{'generation':generation,'apply':True},'BLOCKING_REVIEW_REQUIRED')
    review = await visual_review(run,generation,blocking['render'],label+' blocking')
    reviewed = await run.good('animation_accept_generated',{'generation':generation,'options':{'review':review}})
    run.check(label+'_blocking_reviewed',reviewed['state']=='REVIEWED')
    if detailed:
        # Exercise the public breakdown tool as well as the orchestrator's
        # internal stage, without changing the reviewed candidate or its QA.
        breakdown_plan = await run.good('animation_generate_breakdowns',{'generation':generation})
        run.check(label+'_public_breakdowns_dry_run',breakdown_plan['dry_run'] and breakdown_plan['no_action_created'])
    generated = await run.good('animation_generate',{'generation':generation,'apply':True})
    record['stages'] += generated.get('stages',[])
    run.check(label+'_breakdown_spline', generated['status']=='candidate_created' and len(generated['stages'])==2)
    if detailed:
        # Reviewed body units, rather than tiny per-axis tools. Intermediate
        # semantic adjustments retain original phase/contact/root requirements.
        adjusted = await run.good('animation_adjust_timing', {'generation':generation,'options':{'phase_fractions':{
            'NEUTRAL':0.,'ANTICIPATION':.34,'COMMITMENT':.49,'STRIKE':.58,'IMPACT':.62,'FOLLOW_THROUGH':.70,'RECOVERY':1.}},'apply':True})
        run.check(label+'_timing_candidate', adjusted['status']=='candidate_created', report=adjusted)
        # Load only the runner's own signed generation ledger for proposed plan
        # edits. The server still validates this as data and creates a new Action.
        from blender_mcp.production.animation.generation import store
        from blender_mcp.production.common.storage import Storage
        _,_,state=store.load(Storage(run.folder/'s'),generation)
        revised=deepcopy(state['pose_plan'])
        for pose in revised.get('breakdowns',[]):
            pose['semantic']['arm_swing_deg']['right'] += .5
        adjusted=await run.good('animation_refine_arcs',{'generation':generation,'options':{'pose_plan':revised},'apply':True})
        run.check(label+'_arc_spacing_candidate',adjusted['status']=='candidate_created',report=adjusted)
        revised=deepcopy(revised)
        for pose in revised['poses']:
            if pose['phase']=='ANTICIPATION':pose['semantic']['torso_roll_deg'] += .25
        adjusted=await run.good('animation_refine_balance',{'generation':generation,'options':{'pose_plan':revised},'apply':True})
        run.check(label+'_balance_candidate',adjusted['status']=='candidate_created',report=adjusted)
        for name in ('animation_generate_root_motion','animation_generate_foot_contacts','animation_spline',
                     'animation_polish_curves','animation_add_overlap','animation_add_follow_through'):
            knobs = {'strength':.15,'delay_frames':1,'chain':['spine','chest','head']} if name.endswith('overlap') else {}
            result = await run.good(name, {'generation':generation,'options':knobs,'apply':True})
            run.check(label+'_'+name+'_candidate',result['status']=='candidate_created',report=result)
            record['stages'].append({'stage':name,'action':result.get('candidate_action'),'metrics':result.get('metrics')})
            generated=result
    for name in ('animation_validate_pose','animation_validate_blocking','animation_analyze_pose_silhouette',
                 'animation_analyze_timing','animation_analyze_spacing','animation_analyze_arcs','animation_analyze_body_mechanics'):
        result=await run.good(name,{'generation':generation})
        record['observations'][name]=result
    record['technical_qa']=generated['technical_qa'];record['candidate']=generated['candidate_action']
    run.check(label+'_full_technical_qa',generated['technical_qa']['passed'],qa=generated['technical_qa'])
    if not generated['technical_qa']['passed']:raise AssertionError('Final integrated QA failed '+label)
    rendered=await run.good('animation_generation_render',{'generation':generation,'options':{
        'custom_views':[{'name':'CUSTOM_REAR','direction':[0,1,.25]}]} if detailed else {}})
    record['render']=rendered['render']
    review=await visual_review(run,generation,rendered['render'],label+' final')
    reviewed=await run.good('animation_accept_generated',{'generation':generation,'options':{'review':review}})
    run.check(label+'_reviewed_lifecycle',reviewed['state']=='REVIEWED')
    if detailed:
        record['refinement_cycles']=[]
        for cycle in range(3):
            result=await run.good('animation_refine_generated',{'generation':generation,'apply':True,
              'options':{'target':'sliding_m','minimum_improvement':.0001,'findings':['Baseline sliding is below 1 mm; a no-change proposal must revert rather than claim improvement.'],'changes':{}}})
            run.check(label+'_refinement_'+str(cycle+1)+'_reverts',result['status']=='reverted' and result['candidate_action']==record['candidate'],report=result)
            cycle_report=result['last_refinement']
            review_cycle=await visual_review(run,generation,cycle_report['render'],label+' reverted refinement '+str(cycle+1))
            await run.good('animation_refine_generated',{'generation':generation,'options':{'review':review_cycle}})
            record['refinement_cycles'].append(cycle_report)
        await negative(run,'animation_refine_generated',{'generation':generation,'apply':True},'REFINEMENT_LIMIT')
    accepted=await run.good('animation_accept_generated',{'generation':generation,'apply':True})
    record['lifecycle']=accepted
    run.check(label+'_accepted_source_preserved',accepted['state']=='ACCEPTED' and accepted['source_intact'] and accepted['active_action']==record['candidate'])
    await negative(run,'animation_refine_generated',{'generation':generation,'apply':True},'GENERATION_TERMINAL')
    return generation


async def scenario(run, info):
    names={t.name for t in (await run.client.list_tools()).tools}
    run.report['tool_inventory']=sorted(names)
    run.check('inventory_155',len(names)==155 and {'production.'+n for n in NAMES}<=names)
    await run.good('asset_register_fixture',{'path':info['blend']})
    run.report['source_snapshots_before']=control(run,info,BASE_RUNTIME+"result={name:m2_digest(m3_source_snapshot(bpy.data.actions[name])) for name in params['actions']}",
                                                {'actions':[s['action'] for s in info['scenarios'].values()]})
    await demo(run,info,'heavy_attack','heavy_humanoid',{'type':'ATTACK','profile':'HEAVY_HUMANOID','attack_type':'OVERHEAD','duration_s':2.4},True)
    await demo(run,info,'walk_in_place','locomotion',{'type':'LOCOMOTION','profile':'HUMAN','locomotion_type':'WALK','duration_s':2.,'cycle_duration_s':2.,'root_mode':'IN_PLACE','stride_length_m':.08,'step_height_m':.04,'speed_m_s':.04})
    await demo(run,info,'heavy_walk_root','root_motion_rig',{'type':'LOCOMOTION','profile':'HEAVY_HUMANOID','locomotion_type':'HEAVY_WALK','duration_s':2.,'cycle_duration_s':2.,'root_mode':'ROOT_MOTION','stride_length_m':.08,'step_height_m':.04,'speed_m_s':.04,'forward_distance_m':.08})
    await demo(run,info,'asymmetric_idle','asymmetric_creature',{'type':'IDLE','profile':'ASYMMETRIC_CREATURE','style':'HEAVY','duration_s':3.,'strength':.8,'mass_asymmetry':{'left':1.,'right':1.8}})
    await demo(run,info,'hit_reaction','hit_reaction',{'type':'HIT_REACTION','profile':'HEAVY_HUMANOID','duration_s':1.8,'impact_direction':'LEFT','severity':.55})
    # Remaining recipes exercise their real nonmutating entrypoint and plan;
    # conservative constraints still reject unsupported mechanics.
    spec=info['scenarios']['idle'];options=fixture_options(spec)
    activate(run,info,'idle')
    for recipe in ('animation_generate_locomotion','animation_generate_idle','animation_generate_attack','animation_generate_hit_reaction','animation_generate_turn','animation_generate_start_stop'):
        value=await run.good(recipe,{'object_name':spec['object'],'intent':{'profile':'HUMAN','fps':30,'duration_s':1,'forward_axis':'-Y'},'options':options})
        run.check(recipe+'_dry_plan',value.get('no_action_created') is True)
    intent={'type':'IDLE','profile':'HUMAN','fps':30,'duration_s':1,'forward_axis':'-Y'}
    collision=await run.good('animation_generation_plan',{'object_name':spec['object'],'intent':intent,'options':options})
    await negative(run,'animation_generate_blocking',{'generation':collision['generation'],'options':{'candidate_name':spec['action']},'apply':True},'CANDIDATE_ACTION_COLLISION')
    await run.good('create_checkpoint',{'description':'Before controlled M6 negative fixture mutations','operation':'m6-negative-fixtures'})
    changed=control(run,info,"obj=bpy.data.objects[params['body']];old=list(obj.data.vertices[0].co);obj.data.vertices[0].co.x+=.001\nresult={'changed':obj.name,'original_coordinate':old}",{'body':spec['body']})
    await negative(run,'animation_generate_blocking',{'generation':collision['generation'],'apply':True},'STALE_PROPOSAL')
    control(run,info,"obj=bpy.data.objects[params['body']];obj.data.vertices[0].co=params['original_coordinate']\nresult={'restored':obj.name}",{'body':spec['body'],'original_coordinate':changed['original_coordinate']})
    for scenario_name in ('unsupported_rig','constrained_rig'):
        row=info['scenarios'][scenario_name]
        value=await run.good('animation_generation_compatibility',{'object_name':row['object'],'options':fixture_options(row)})
        run.check(scenario_name+'_safe_rejection',not value.get('supported'),report=value)
    key='m6-idempotent-'+uuid4().hex[:8]
    args={'generation':collision['generation'],'apply':True,'operation_id':key}
    first=await run.call('animation_generate_blocking',args);again=await run.call('animation_generate_blocking',args)
    run.check('repeated_operation_id_replayed_without_action_duplicate',first.get('data',{}).get('candidate_action') is not None and again.get('idempotency',{}).get('replayed') is True and first.get('data',{}).get('candidate_action')==again.get('data',{}).get('candidate_action'),report=again)
    rejected=await run.good('animation_reject_generated',{'generation':collision['generation'],'apply':True,'options':{'review':{'reason':'Controlled lifecycle rejection; candidate retained'}}})
    run.check('explicit_reject_retains_candidate',rejected['state']=='REJECTED' and rejected['preserved'])
    run.report['rejected_generation']=rejected
    # An effector proposal without an explicit plane target must fail before
    # authoring. Its positive quarter-frame coverage is in the Blender probe.
    effect=await run.good('animation_generation_plan',{'object_name':spec['object'],'intent':intent,'options':options})
    await run.good('animation_generate_blocking',{'generation':effect['generation'],'apply':True})
    await negative(run,'animation_generate_effector_contact',{'generation':effect['generation'],'apply':True},'BLOCKING_REVIEW_REQUIRED')
    _,effect_plan,effect_state=__import__('blender_mcp.production.animation.generation.store',fromlist=['load']).load(
        __import__('blender_mcp.production.common.storage',fromlist=['Storage']).Storage(run.folder/'s'),effect['generation'])
    # Deliberate collapse in this owned test candidate. No source Action/weight
    # is edited. The integrated QA must fail and the signed lifecycle must
    # refuse this externally changed candidate before activation.
    control(run,info,BASE_RUNTIME+r'''
obj=m2_object(params['object']);old=obj.animation_data.action;slot=m2_slot(obj);saved=m3_capture(obj)
try:
    m3_assign(obj,bpy.data.actions[params['candidate']])
    bone=obj.pose.bones[params['chest']]
    for frame,value in [(1,1.),(16,.01),(31,1.)]:
        bone.scale=(value,value,value);bone.keyframe_insert(data_path='scale',frame=frame)
    result={'fixture_candidate':obj.animation_data.action.name,'collapse_scale':.01}
finally:
    m3_assign(obj,old,slot);m3_restore(obj,saved)
''',{'object':spec['object'],'chest':spec['mapping']['chest'],'candidate':effect_state['candidate_action']})
    from blender_mcp.production.animation.generation.qa import technical
    bad_qa=technical(run.bridge,effect_plan,effect_state['candidate_action'],effect_plan['options'])
    run.report['severe_deformation_negative']=bad_qa
    run.check('severe_deformation_fails_integrated_generation_gate',bad_qa['passed'] is False and bad_qa['gates']['deformation_measured'] is False
              and bad_qa['reports']['animation_deformation_qa']['metrics']['issue_count']>0,report=bad_qa)
    await negative(run,'animation_accept_generated',{'generation':effect['generation'],'apply':True},'STALE_PROPOSAL')
    source_after=control(run,info,BASE_RUNTIME+"result={name:m2_digest(m3_source_snapshot(bpy.data.actions[name])) for name in params['actions']}",{'actions':[s['action'] for s in info['scenarios'].values()]})
    run.check('all_original_source_actions_immutable',source_after==run.report['source_snapshots_before'])
    run.report['source_snapshots_after']=source_after
    final_checkpoint=await run.good('create_checkpoint',{'description':'M6 controlled demos, source Actions preserved, generated Actions and QA retained','operation':'m6-final-state'})
    run.report['final_checkpoint']=final_checkpoint
    # A previously registered FIXTURE must never be relabelled MASTER. Save
    # this owned scene to a new path, keeping its original binary and registry.
    master_path=run.folder/('master-negative-'+uuid4().hex[:8]+'.blend')
    if master_path.exists(): raise FileExistsError('Refusing to overwrite MASTER negative fixture')
    source_sha=sha256(Path(info['blend']))
    saved=control(run,info,r'''
status=bpy.ops.wm.save_as_mainfile(filepath=params['path'],copy=False,check_existing=True)
result={'status':sorted(status),'filepath':bpy.data.filepath}
''',{'path':str(master_path)})
    run.check('separate_master_path_preserves_original_fixture_file',saved['status']==['FINISHED']
              and saved['filepath']==str(master_path) and sha256(Path(info['blend']))==source_sha)
    await run.good('asset_register_master',{'confirm_master':True})
    master_status=await run.good('asset_status')
    run.check('separate_file_is_registered_master_read_only',master_status['asset']['role']=='MASTER'
              and master_status['asset']['mutable'] is False)
    if master_status['asset']['role']!='MASTER': raise AssertionError('Separate file must really be MASTER')
    master_plan=await run.good('animation_generation_plan',{'object_name':spec['object'],'intent':intent,'options':options})
    master_info=dict(info,blend=str(master_path))
    action_names=control(run,master_info,"result=sorted(a.name for a in bpy.data.actions)")
    checkpoint_paths=sorted(str(p) for p in (run.folder/'s'/'checkpoints').glob('*/manifest.json'))
    master_sha=sha256(master_path)
    await negative(run,'animation_generate_blocking',{'generation':master_plan['generation'],'apply':True},'MASTER_ASSET_READ_ONLY')
    run.check('master_refusal_before_new_checkpoint_or_action',
              control(run,master_info,"result=sorted(a.name for a in bpy.data.actions)")==action_names
              and sorted(str(p) for p in (run.folder/'s'/'checkpoints').glob('*/manifest.json'))==checkpoint_paths
              and sha256(master_path)==master_sha and sha256(Path(info['blend']))==source_sha)
    run.report['master_negative_fixture']=str(master_path)
    run.check('all_35_m6_tools_called',{'production.'+n for n in NAMES}<={c['tool'] for c in run.report['calls']})


async def execute(args, folder, info):
    run = Run(folder)
    run.bridge=Bridge('localhost',args.port)
    with (folder/'server-stderr.txt').open('w', encoding='utf-8') as log:
        async with session(folder, args.port, log) as client:
            run.client = client
            try:
                await scenario(run, info)
            except Exception as exc:
                run.report['exception'] = repr(exc)
                print('EXCEPTION', repr(exc), flush=True)
            finally:
                run.bridge.close()
    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--port', type=int, default=9877)
    args = parser.parse_args()
    if args.port == 9876 or listening(args.port):
        raise SystemExit('Reserved/busy port; no existing instance is contacted')
    folder = Path(tempfile.gettempdir()) / ('bpm6-'+uuid4().hex[:8])
    (folder/'fixture').mkdir(parents=True)
    expression = 'import sys\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(ROOT/'tests'/'blender'))+')\nfrom fixtures_m6 import build\nbuild('+repr(str(folder/'fixture'))+',fps=30,duration_seconds=3,suite=True)'
    built = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python-expr', expression],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    (folder/'build.txt').write_text(built.stdout+built.stderr, encoding='utf-8')
    if built.returncode: raise RuntimeError('Fixture build failed: '+str(folder/'build.txt'))
    info = json.loads((folder/'fixture'/'fixtures.json').read_text())
    fixture = Path(info['blend']); digest = sha256(fixture)
    with (folder/'blender-stdout.txt').open('w', encoding='utf-8') as log:
        if listening(8768): raise RuntimeError('Own temporal review HTTP port busy; no existing server contacted')
        web = subprocess.Popen([sys.executable, '-m', 'http.server', '8768', '--bind', '127.0.0.1', '--directory', str(folder)],
                               stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        child = subprocess.Popen([args.blender, '--factory-startup', '--disable-autoexec', str(fixture), '--python',
                                  str(ROOT/'tests'/'integration'/'m4_own_blender_start.py'), '--', str(args.port)], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.time()+90
            while not listening(args.port):
                if child.poll() is not None or time.time()>deadline: raise RuntimeError('Owned fixture addon did not start')
                time.sleep(.5)
            run = asyncio.run(execute(args, folder, info))
        finally:
            child.terminate()
            try: child.wait(timeout=30)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
            web.terminate(); web.wait(timeout=10)
    run.report.update(fixture_file_unchanged=sha256(fixture)==digest, blender_pid_started_and_closed_here=child.pid,
                      blender_port=args.port, fixture=info, production_assets_used=False)
    run.report['status'] = 'pass' if 'exception' not in run.report and run.report['fixture_file_unchanged'] and all(r['passed'] for r in run.report['checks'].values()) else 'fail'
    run.save()
    target = ROOT/'test-artifacts'/'m6'/'last-mcp.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'report': str(folder/'report.json'), 'status': run.report['status']}, indent=2), encoding='utf-8')
    print('M6_MCP_REPORT='+str(folder/'report.json')+' status='+run.report['status'])
    raise SystemExit(0 if run.report['status']=='pass' else 1)


if __name__=='__main__': main()
