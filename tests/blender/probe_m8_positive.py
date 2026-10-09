"""Four controlled positive M8 service paths. No GUI, socket or artistic decision."""
import ast, json, os, pathlib, subprocess, sys, tempfile, time, traceback, uuid
import argparse
REPO=pathlib.Path(__file__).resolve().parents[2];sys.path.insert(0,str(REPO/'src'))
from blender_mcp.production.common.bridge import build_code,MARKER
from blender_mcp.production.common.storage import Storage,sha256
from blender_mcp.production.common.asset_safety import register_fixture
from blender_mcp.production.physics import service
from m8_pipe_harness import PipeBridge
Path=pathlib.Path
out=Path(tempfile.gettempdir())/('bpm8positive-'+uuid.uuid4().hex[:8]);out.mkdir()
report={'schema':'m8.positive_service_pipelines.v1','output':str(out),'pipelines':[],'errors':[],
        'backend_before':service.backend_fingerprint(),'artistic_review':'NOT_RUN','accepted':False,
        'scope':'Four controlled service flows through technical QA/cache/bake; no GUI or socket'}
def save():
    (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
allowed_variants=['dangling_accessory','soft_region','body_face_secondary','spring_chain']
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--variant',choices=allowed_variants)
selected=parser.parse_args()
for variant in ([selected.variant] if selected.variant else allowed_variants):
    folder=out/variant;folder.mkdir();entry={'variant':variant,'passed':False,'artistic_review':'NOT_RUN'};bridge=None
    previous_variant=os.environ.get('M8_PIPE_FIXTURE_VARIANT');os.environ['M8_PIPE_FIXTURE_VARIANT']=variant
    try:
        bridge=PipeBridge(folder,30,2,variant=variant);fixture=bridge.fixture;entry['fixture']=fixture
        storage=Storage(folder/'store');register_fixture(bridge,storage,fixture['source_path'],[str(folder/'fixtures')])
        before=sha256(Path(fixture['source_path']))
        combined=fixture.get('combined_provenance');combined_before=sha256(Path(combined['source'])) if combined else None
        options={k:fixture[k] for k in ('manifest','primary_objects','colliders','pin_group','anchor_object','anchor_bone')}
        if fixture.get('chain'):options['chain']=fixture['chain']
        planned=service.plan(bridge,storage,variant+'-plan',fixture['source_object'],None,fixture['brief'],options)
        path,plan,state=service.load(storage,planned['physics']);entry['supported']=plan['supported'];entry['guard_selection']=plan['guard_selection'];entry['physics']=str(path)
        started=time.perf_counter();state=service.ensure_candidate(bridge,storage,path,plan,state,variant+'-candidate');entry['candidate_seconds']=time.perf_counter()-started
        started=time.perf_counter();state=service.prepare_colliders(bridge,storage,path,plan,state,variant+'-colliders');entry['collider_seconds']=time.perf_counter()-started
        started=time.perf_counter();state=service.generate_procedural(bridge,storage,path,plan,state,variant+'-generate');entry['generate_seconds']=time.perf_counter()-started
        entry['technical_qa']=state.get('technical_qa');entry['solver_configuration']=state.get('solver_configuration');entry['state_after_generation']=state['state']
        if not state.get('technical_qa',{}).get('passed'):raise RuntimeError('POSITIVE_TECHNICAL_QA_FAILED')
        started=time.perf_counter();state=service.bake_secondary(bridge,storage,path,plan,state,variant+'-bake');entry['cache_and_bake_seconds']=time.perf_counter()-started
        entry.update(cache=state.get('cache'),bake_deviation=state.get('bake_deviation'),state=state['state'])
        entry['source_before']=before;entry['source_after']=sha256(Path(fixture['source_path']));entry['source_unchanged']=entry['source_before']==entry['source_after']
        if combined:
            entry['historical_m6_m7_before']=combined_before;entry['historical_m6_m7_after']=sha256(Path(combined['source']));entry['historical_m6_m7_unchanged']=entry['historical_m6_m7_before']==entry['historical_m6_m7_after']
        entry['passed']=bool(entry['technical_qa']['passed'] and entry['bake_deviation']['passed'] and entry['source_unchanged'] and entry.get('historical_m6_m7_unchanged',True))
    except Exception as e:
        entry['error']=str(e);entry['traceback']=traceback.format_exc();report['errors'].append({'variant':variant,'error':str(e)})
    finally:
        if bridge:bridge.close()
        if previous_variant is None:os.environ.pop('M8_PIPE_FIXTURE_VARIANT',None)
        else:os.environ['M8_PIPE_FIXTURE_VARIANT']=previous_variant
        report['pipelines'].append(entry);save()
        print('M8_POSITIVE_PIPELINE',variant,entry['passed'],entry.get('error',''),flush=True)
report['backend_after']=service.backend_fingerprint();report['backend_unchanged']=report['backend_after']==report['backend_before'];report['passed']=all(p['passed'] for p in report['pipelines']);save()
print('M8_POSITIVE_REPORT',str(out/'report.json'),report['passed'],flush=True)
