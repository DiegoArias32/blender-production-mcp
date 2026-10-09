"""Final M5 subsystem gates. Missing evidence stays UNVERIFIED; there is no quality score."""
from __future__ import annotations
from uuid import uuid4
from ..common.m5 import validate_relationship,signature,m5_read,M5_RUNTIME
from ..common.storage import write_json

FIELDS={'retopology','weights','game_mesh','uv','bake','attributes','require_bake_ready','require_deformation'}

def settings(values=None):
    values=values or {}
    if not isinstance(values,dict) or set(values)-FIELDS:raise ValueError('INVALID_VALIDATION_OPTIONS: unknown validation field')
    result=dict(values)
    for key in ('retopology','weights','game_mesh','uv','bake','attributes'):
        if not isinstance(result.get(key,{}),dict):raise ValueError(key+' must be an options object')
        result.setdefault(key,{})
    for key in ('require_bake_ready','require_deformation'):
        if not isinstance(result.get(key,True),bool):raise ValueError(key+' must be boolean')
        result.setdefault(key,True)
    return result

def verdict(report,passed=None,reason=None):
    value=report.get('passed') if passed is None else passed
    return {'status':'PASS' if value is True else 'FAIL' if value is False else 'UNVERIFIED',
            'passed':value,'reason':reason,'report':report}

def validate(bridge,storage,relationship,options=None,operation_id=''):
    from ..retopology import service as retopology
    from ..transfer import service as transfer
    from ..transfer.runtime import TRANSFER_RUNTIME
    from ..uv import service as uv
    from ..bake import service as bake
    from . import service as game
    opts=settings(options);relation=validate_relationship(bridge,storage,relationship)
    low=signature(bridge,relation['target_object']);subsystems={}
    topology=retopology.compare(bridge,storage,relationship,opts['retopology'],'validate')
    subsystems['topology']=verdict(topology)
    subsystems['triangle_budget']=verdict({'triangles':low['counts']['triangles'],
        'minimum':opts['retopology'].get('minimum_triangles'),
        'maximum':opts['retopology'].get('maximum_triangles',opts['retopology'].get('target_triangles'))},
        (low['counts']['triangles']>=opts['retopology'].get('minimum_triangles',0) and
         low['counts']['triangles']<=opts['retopology'].get('maximum_triangles',opts['retopology'].get('target_triangles',10_000_000)))
        if ('maximum_triangles' in opts['retopology'] or 'target_triangles' in opts['retopology']) else None,
        'A configurable triangle budget must be declared for package acceptance.')
    subsystems['surface']=verdict(retopology.compare(bridge,storage,relationship,opts['retopology'],'surface'))
    subsystems['silhouette']=verdict(retopology.compare(bridge,storage,relationship,opts['retopology'],'silhouette'))
    if low['armatures']:
        weight_report=transfer.validate_weights(bridge,storage,relationship,opts['weights'],operation_id or uuid4().hex)
        subsystems['weights']=verdict(weight_report)
        deformation=weight_report.get('deformation_qa')
        subsystems['deformation']=verdict(deformation or {},
            deformation.get('passed') if deformation else None,'Explicit mapped stress poses required for a rigged mesh.')
    else:
        subsystems['weights']={'status':'NOT_APPLICABLE','passed':True,'reason':'Static mesh, no armature.'}
        subsystems['deformation']={'status':'NOT_APPLICABLE','passed':True,'reason':'Static mesh, no armature.'}
    morph=transfer.analyze_morphs(bridge,storage,relationship)
    subsystems['morphs']=verdict(morph,not bool(morph.get('errors')))
    attr_options=transfer.options(opts['attributes'],'attributes')
    attr_report=m5_read(bridge,storage,relationship,TRANSFER_RUNTIME,
        'proposal=mt_attributes_plan(high,low,params["options"])\n'
        'result=mt_attribute_verify(high,low,low,proposal,params["options"]) if proposal["supported"] else {"passed":False,"errors":proposal.get("errors",[])}',attr_options)
    subsystems['materials_attributes']=verdict(attr_report)
    for kind in ('normals','triangulation','tangents'):
        subsystems[kind]=verdict(game.validate(bridge,storage,relationship,kind,opts['game_mesh']))
    uv_report=uv.analyze(bridge,storage,relationship,opts['uv'])
    subsystems['uv']=verdict(uv_report,uv_report.get('valid'))
    subsystems['udim']=verdict({'tiles':uv_report.get('tiles'),
        'invalid_udims':uv_report.get('invalid_udims',uv_report.get('errors',[]))},
        uv_report.get('valid'))
    subsystems['texel_density']=verdict(uv_report,uv_report.get('valid'))
    bake_report=bake.read(bridge,storage,relationship,opts['bake'])
    bake_ready=(bake_report.get('passed') is True and not bake_report.get('errors') and
                bake_report.get('readiness') in ('READY','READY_WITH_WARNINGS'))
    subsystems['bake']=verdict(bake_report,bake_ready)
    ignored=[]
    if not opts['require_bake_ready']:ignored.append('bake')
    if not opts['require_deformation']:ignored.append('deformation')
    passed=all(item['passed'] is True for key,item in subsystems.items() if key not in ignored)
    intact=signature(bridge,relation['source_object'])['fingerprint']==relation['source_signature']['fingerprint']
    final_low=signature(bridge,relation['target_object'])
    stable=final_low['fingerprint']==low['fingerprint']
    if not intact or not stable:passed=False
    return {'status':'success' if passed else 'error','passed':passed,'relationship':relationship,
        'source_intact':intact,'numeric_verification':'pass' if passed else 'fail','visual_verification':'required',
        'subsystems':subsystems,'ignored_gates':ignored,'signature':low,'final_signature':final_low,'validation_snapshot_stable':stable,
        'errors':([] if passed else ['GAME_MESH_NOT_VALIDATED: inspect concrete subsystem states'])+([] if stable else ['STALE_VALIDATION: LOW changed during aggregation']),
        'warnings':['Sampling metrics and numerical gates do not certify artistic/gameplay quality.']+bake_report.get('warnings',[])}

def register(tool,resources):
    @tool('production.game_mesh_validate')
    def validate_tool(relationship:str,options:dict|None=None,*,_operation_id='')->dict:
        """Aggregate concrete topology/fidelity/deformation/attributes/UV/bake gates; no arbitrary quality score."""
        return validate(*resources(),relationship,options,_operation_id)
