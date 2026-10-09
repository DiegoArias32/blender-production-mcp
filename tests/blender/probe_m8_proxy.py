"""Host-side finite AABB proxy qualification using installed Blender RNA.

Run with the project Python: python tests/blender/probe_m8_proxy.py
Creates only three owned factory processes, short temporary fixture/store paths,
and machine-readable evidence. Does not perform visual review or acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import traceback
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO/'src'))
from m8_pipe_harness import PipeBridge
from blender_mcp.production.common.asset_safety import register_fixture,require_mutable_asset
from blender_mcp.production.common.storage import Storage,sha256
from blender_mcp.production.physics import candidate,colliders,integrity,service

SOURCE_AUDIT = r'''
import bpy
objects={}
for name in params['names']:
    obj=bpy.data.objects[name]
    objects[name]={'data':obj.data.name,'vertices':len(obj.data.vertices),'polygons':len(obj.data.polygons),
        'render_visible':not obj.hide_render,'scale':list(obj.scale),'modifiers':[m.type for m in obj.modifiers]}
result={'objects':objects}
'''
OWNERSHIP_AUDIT = r'''
import bpy
collection=bpy.data.collections[params['collection']]
objects=[]
for obj in collection.objects:
    objects.append({'name':obj.name,'generation_id':obj.get('production_generation_id'),
        'version':obj.get('production_candidate_version'),'owned':bool(obj.get('production_m8_owned')),
        'source':obj.get('production_source_object'),'vertices':len(obj.data.vertices),
        'render_visible':not obj.hide_render})
result={'collection':collection.name,'generation_id':collection.get('production_generation_id'),
    'version':collection.get('production_candidate_version'),'owned':bool(collection.get('production_m8_owned')),
    'objects':objects}
'''


def main():
    output=Path(tempfile.gettempdir())/('bpm8proxy-'+uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    report={'schema':'m8.controlled_proxy_probe.v1','output':str(output),'checks':{},'errors':[],
            'backend_before':service.backend_fingerprint(),'visual_review':'NOT_RUN'}
    for label,variant,limit,expect_reject in (
        ('static_cube_exact','proxy_cube',.01,False),
        ('static_sphere_simplified','proxy_sphere',1.0,False),
        ('sphere_deviation_reject','proxy_sphere',.01,True),
    ):
        folder=output/label
        folder.mkdir()
        bridge=None
        entry={'variant':variant,'max_proxy_deviation_m':limit,'expected_rejection':expect_reject,'passed':False}
        try:
            bridge=PipeBridge(folder,30,1,variant=variant)
            fixture=bridge.fixture
            entry['fixture']=fixture
            storage=Storage(folder/'store')
            register_fixture(bridge,storage,fixture['source_path'],[str(folder/'fixtures')])
            require_mutable_asset(bridge,storage,'M8 controlled proxy probe')
            generation=uuid4().hex
            params={'source_object':fixture['source_object'],'primary_objects':fixture['primary_objects'],
                    'colliders':fixture['colliders'],'pin_group':fixture['pin_group'],
                    'generation_id':generation,'version':1,'candidate_name':'M8_ProxyCandidate_'+generation[:8],
                    'subsystem':'CLOTH','proxy_strategy':'AABB_PROXY','max_proxy_deviation_m':limit}
            before=bridge.run(integrity.STATUS,params)
            audit_before=bridge.run(SOURCE_AUDIT,{'names':fixture['primary_objects']})
            source_sha=sha256(Path(fixture['source_path']))
            created=candidate.create(bridge,dict(params,expected_status={'fingerprint':before['fingerprint']}))
            params.update(candidate_collection=created['collection'],
                          collider_collection_name='M8_COLLIDERS_'+generation+'_v1',
                          expected_status={'fingerprint':before['fingerprint']})
            preparation=None
            try:
                preparation=bridge.run(colliders.PREPARE,params)
            except Exception as exc:
                entry['rejection']=str(exc)
                if not expect_reject or 'PHYSICS_PROXY_DEVIATION_EXCEEDED' not in str(exc):
                    raise
            if expect_reject and preparation is not None:
                raise AssertionError('PROXY_DEVIATION_REJECTION_REQUIRED')
            after=bridge.run(integrity.STATUS,params)
            audit_after=bridge.run(SOURCE_AUDIT,{'names':fixture['primary_objects']})
            ownership=bridge.run(OWNERSHIP_AUDIT,{'collection':params['collider_collection_name']})
            source_after=sha256(Path(fixture['source_path']))
            entry.update(preparation=preparation,source_sha256_before=source_sha,source_sha256_after=source_after,
                         source_fingerprint_before=before['fingerprint'],source_fingerprint_after=after['fingerprint'],
                         source_audit_before=audit_before,source_audit_after=audit_after,ownership=ownership)
            owns=ownership['owned'] and ownership['generation_id']==generation and ownership['version']==1
            owns=owns and all(o['owned'] and o['generation_id']==generation and o['version']==1 for o in ownership['objects'])
            source_intact=source_sha==source_after and before['fingerprint']==after['fingerprint'] and audit_before==audit_after
            if not expect_reject:
                measured=preparation['manifest'][0]['maximum_proxy_to_source_deviation_m']
                entry['measured_max_proxy_vertex_deviation_m']=measured
                if variant=='proxy_cube':
                    assert measured==0.0
                else:
                    assert 0<measured<=limit
                    assert fixture['source_collider_vertices']>8
                assert len(ownership['objects'])==1 and ownership['objects'][0]['vertices']==8
            entry['source_intact']=source_intact
            entry['ownership_verified']=owns
            entry['passed']=bool(source_intact and owns)
        except Exception as exc:
            entry['error']=str(exc)
            entry['traceback']=traceback.format_exc()
            report['errors'].append({'check':label,'error':str(exc)})
        finally:
            if bridge:bridge.close()
            report['checks'][label]=entry
            (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
            print('M8_PROXY',label,entry['passed'],entry.get('error',''),flush=True)
    report['backend_after']=service.backend_fingerprint()
    report['backend_unchanged']=report['backend_before']==report['backend_after']
    report['passed']=all(e['passed'] for e in report['checks'].values()) and report['backend_unchanged']
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    print('M8_PROXY_REPORT',str(output/'report.json'),report['passed'],flush=True)
    return report


if __name__=='__main__':
    main()
