"""M5 relationship and Candidate Mesh transactions, extending M4 guards.

Full content fingerprints remain inside Blender; the host receives compact signatures.
Every mutation derives a new object+mesh. HIGH and previous LOW remain untouched.
"""
from __future__ import annotations
import hashlib,json,math
from pathlib import Path
from uuid import uuid4
from .asset_safety import require_mutable_asset,current_asset
from .ledger import OperationLedger,replayed,idempotency_block,is_uncertain
from .storage import write_json,sha256
from ..mesh.runtime import MESH_RUNTIME

M5_RUNTIME=MESH_RUNTIME+r'''
from array import array

def m5_obj(name):
    return ms_mesh_object(name)

def m5_asset_context():
    return {'filepath':bpy.data.filepath,'stamps':sorted(scene.get('production_asset') for scene in bpy.data.scenes if isinstance(scene.get('production_asset'),str))}

def m5_guard_asset_context(expected):
    if m5_asset_context()!=expected:raise ValueError('ASSET_CONTEXT_CHANGED: open file or asset stamp changed before atomic execution')

def m5_buffer(collection,field,width=1,kind='f'):
    data=array(kind,[0])*(len(collection)*width)
    if data: collection.foreach_get(field,data)
    return m2_fast_digest(data.tobytes())

def m5_attrs(me):
    result={};unsupported=[]
    formats={'FLOAT':('value',1,'f'),'INT':('value',1,'i'),'BOOLEAN':('value',1,'b'),
        'FLOAT_VECTOR':('vector',3,'f'),'FLOAT_COLOR':('color',4,'f'),'BYTE_COLOR':('color',4,'f'),
        'FLOAT2':('vector',2,'f'),'INT8':('value',1,'b'),'INT32_2D':('value',2,'i'),
        'QUATERNION':('value',4,'f'),'FLOAT4X4':('value',16,'f')}
    for a in me.attributes:
        if a.data_type in formats:
            field,width,kind=formats[a.data_type]
            try: content=m5_buffer(a.data,field,width,kind)
            except (TypeError,AttributeError,RuntimeError):
                unsupported.append(a.name);content='unsupported_content'
        elif a.data_type=='STRING': content=m2_digest([str(e.value) for e in a.data])
        else: unsupported.append(a.name);content='unsupported_content'
        result[a.name]={'domain':a.domain,'type':a.data_type,'content':content}
    return result,unsupported

def m5_modifier_state(obj):
    rows=[]
    for mod in obj.modifiers:
        values={'name':mod.name,'type':mod.type,'viewport':mod.show_viewport,'render':mod.show_render,
            'editmode':mod.show_in_editmode,'cage':mod.show_on_cage}
        # Literal RNA access keeps the upstream AST policy intact. Unhandled modifier
        # settings are explicitly reported; transactions must refuse them.
        if mod.type=='ARMATURE':
            values.update(object=mod.object.name if mod.object else None,vertex_group=mod.vertex_group,
                invert=mod.invert_vertex_group,use_vertex_groups=mod.use_vertex_groups,
                envelopes=mod.use_bone_envelopes,preserve_volume=mod.use_deform_preserve_volume,
                multi=mod.use_multi_modifier)
        elif mod.type=='SUBSURF':
            values.update(subdivision_type=mod.subdivision_type,levels=mod.levels,render_levels=mod.render_levels,
                quality=mod.quality,uv_smooth=mod.uv_smooth,boundary_smooth=mod.boundary_smooth,
                creases=mod.use_creases,limit_surface=mod.use_limit_surface)
        elif mod.type=='DECIMATE':
            values.update(decimate_type=mod.decimate_type,ratio=mod.ratio,vertex_group=mod.vertex_group,
                factor=mod.vertex_group_factor,invert=mod.invert_vertex_group,
                triangulate=mod.use_collapse_triangulate,iterations=mod.iterations,
                angle_limit=mod.angle_limit,delimit=sorted(mod.delimit),symmetry=mod.use_symmetry,
                symmetry_axis=mod.symmetry_axis)
        elif mod.type=='TRIANGULATE':
            values.update(quad_method=mod.quad_method,ngon_method=mod.ngon_method,min_vertices=mod.min_vertices,
                keep_custom_normals=mod.keep_custom_normals)
        else:values['unsupported_settings']=True
        rows.append(values)
    return rows

def m5_signature(obj):
    me=obj.data;me.calc_loop_triangles()
    attrs,unsupported=m5_attrs(me)
    topology=m2_digest([m5_buffer(me.edges,'vertices',2,'i'),m5_buffer(me.loops,'vertex_index',1,'i'),
        m5_buffer(me.polygons,'loop_start',1,'i'),m5_buffer(me.polygons,'loop_total',1,'i')])
    geometry=m5_buffer(me.vertices,'co',3)
    morphs={}
    if me.shape_keys:
        morphs={'relative':me.shape_keys.use_relative,'keys':[{'name':b.name,'value':b.value,'relative':b.relative_key.name,
            'coords':m5_buffer(b.data,'co',3)} for b in me.shape_keys.key_blocks],
            'animation':str(me.shape_keys.animation_data.action.name if me.shape_keys.animation_data and me.shape_keys.animation_data.action else None),
            'drivers':len(me.shape_keys.animation_data.drivers) if me.shape_keys.animation_data else 0}
    groups=[g.name for g in obj.vertex_groups]
    weights=m2_digest([groups,[[[e.group,e.weight] for e in v.groups] for v in me.vertices] if groups else []])
    uvs={l.name:m5_buffer(l.data,'uv',2) for l in me.uv_layers}
    me_normals=m5_buffer(me.corner_normals,'vector',3)
    rigs=[]
    for mod in obj.modifiers:
        if mod.type=='ARMATURE' and mod.object:
            rig=mod.object
            rigs.append({'object':rig.name,'matrix':ms_matrix(rig.matrix_world),'pose_position':rig.data.pose_position,
                'rest':[[b.name,b.parent.name if b.parent else None,b.use_deform,ms_matrix(b.matrix_local)] for b in rig.data.bones],
                'pose':[[b.name,ms_matrix(b.matrix)] for b in rig.pose.bones]})
    sections={'topology':topology,'geometry':geometry,'attributes':m2_digest(attrs),'weights':weights,
        'morphs':m2_digest(morphs),'uv':m2_digest([uvs,me.uv_layers.active_index]),
        'normals':m2_digest([me.has_custom_normals,me_normals,m5_buffer(me.polygons,'use_smooth',1,'b')]),
        'materials':m2_digest([ms_materials(obj,me),m5_buffer(me.polygons,'material_index',1,'i')]),
        'properties':m2_digest(ms_custom_properties(obj,me)),'modifiers':m2_digest(m5_modifier_state(obj)),
        'rigs':m2_digest(rigs),'relationships':m2_digest(ms_relationships(obj)),
        'transform':m2_digest([ms_transform(obj),bpy.context.scene.unit_settings.scale_length])}
    return {'schema':'production.mesh_signature.m5.v1','object':obj.name,'mesh':me.name,'sections':sections,
        'fingerprint':m2_digest(sections),'geometry_fingerprint':m2_digest({k:sections[k] for k in ('topology','geometry','transform')}),
        'counts':{'vertices':len(me.vertices),'edges':len(me.edges),'polygons':len(me.polygons),'triangles':len(me.loop_triangles)},
        'shape_keys':list(morphs.get('keys',[]) and [b.name for b in me.shape_keys.key_blocks]),'vertex_groups':groups,
        'animated_shape_keys':bool(me.shape_keys and me.shape_keys.animation_data and (me.shape_keys.animation_data.action or me.shape_keys.animation_data.drivers or me.shape_keys.animation_data.nla_tracks)),
        'materials':[m.name if m else None for m in me.materials],'uv_layers':list(uvs),
        'has_custom_normals':me.has_custom_normals,'unsupported_attributes':unsupported,'armatures':[r['object'] for r in rigs],
        'unsupported_modifiers':[m['name'] for m in m5_modifier_state(obj) if m.get('unsupported_settings')],
        'units':bpy.context.scene.unit_settings.scale_length,'dimensions_m':[float(v)*bpy.context.scene.unit_settings.scale_length for v in obj.dimensions]}

def m5_copy(obj,name,operation_id,role='GAME_MESH'):
    if bpy.context.mode!='OBJECT': raise ValueError('MODE_NOT_SUPPORTED: Object mode required')
    if obj.library or obj.data.library or obj.override_library: raise ValueError('LINKED_MESH_UNSUPPORTED: local candidate required')
    if obj.data.shape_keys and obj.data.shape_keys.animation_data and (obj.data.shape_keys.animation_data.action or obj.data.shape_keys.animation_data.drivers or obj.data.shape_keys.animation_data.nla_tracks):
        raise ValueError('ANIMATED_MORPH_COPY_UNSUPPORTED: Action/driver/NLA dependencies require a manual pipeline')
    if bpy.data.objects.get(name): raise ValueError('CANDIDATE_EXISTS: '+name)
    new=obj.copy();new.data=obj.data.copy();new.name=name;new.data.name=name+'Mesh'
    for c in obj.users_collection:c.objects.link(new)
    new.hide_render=True
    new[MS_MARKER]=json.dumps({'schema':'production.candidate.m5.v1','source_object':obj.name,'source_fingerprint':m5_signature(obj)['fingerprint'],
        'operation_id':operation_id,'role':role})
    new['production_m5_candidate']=json.dumps({'source':obj.name,'role':role,'operation_id':operation_id})
    return new
'''


def _save(storage,category,operation_id,filename,data):
    path=storage.reserve(category,operation_id)/filename;write_json(path,data);return str(path)

def candidate_name(source,key):
    # Some Blender operator string arguments are still bounded to 64 UTF-8 bytes.
    # Only new owned candidate names are shortened; authoring names stay intact.
    prefix=source.split('__M5_',1)[0].encode('utf8')[:36].decode('utf8','ignore')
    return prefix+'__M5_'+hashlib.sha256(key.encode()).hexdigest()[:12]

def load(storage,path,category='game_mesh',schema=None):
    p=Path(path).resolve(strict=True)
    if not p.is_relative_to(storage.root/category) or p.suffix!='.json':raise ValueError('MANIFEST_OUTSIDE_WORKSPACE: '+str(p))
    data=json.loads(p.read_text(encoding='utf8'))
    if schema and data.get('schema')!=schema:raise ValueError('INVALID_MANIFEST_SCHEMA: '+str(data.get('schema')))
    if data.get('schema')=='production.high_low.v1':
        integrity=p.with_name('relationship.integrity.json')
        if not integrity.is_file() or sha256(p)!=json.loads(integrity.read_text(encoding='utf8'))['sha256']:
            raise ValueError('STALE_HIGH_LOW_RELATIONSHIP: manifest integrity mismatch')
    return data

def signature(bridge,object_name):
    return bridge.run(M5_RUNTIME+'\nresult=m5_signature(m5_obj(params["object_name"]))',{'object_name':object_name})

def validate_relationship(bridge,storage,path):
    r=load(storage,path,schema='production.high_low.v1')
    actual=bridge.run(M5_RUNTIME+'\nresult={"high":m5_signature(m5_obj(params["high_name"])),"low":m5_signature(m5_obj(params["target_name"]))}',
        {'high_name':r['source_object'],'target_name':r['target_object']})
    if actual['high']['fingerprint']!=r['source_signature']['fingerprint']:raise ValueError('STALE_HIGH_LOW_RELATIONSHIP: source high changed')
    if actual['low']['geometry_fingerprint']!=r['target_geometry_fingerprint']:raise ValueError('STALE_HIGH_LOW_RELATIONSHIP: candidate geometry/transform changed')
    return r

def m5_read(bridge,storage,relationship,runtime,body,options=None):
    r=validate_relationship(bridge,storage,relationship)
    return bridge.run(M5_RUNTIME+'\n'+runtime+'\nhigh=m5_obj(params["high_name"]);low=m5_obj(params["target_name"])\n'+body,{'high_name':r['source_object'],'target_name':r['target_object'],'relation':r,'options':options or {}})

def _relation(storage,operation_id,high,low,asset,metadata=None,previous=None):
    r={'schema':'production.high_low.v1','relationship_id':previous['relationship_id'] if previous else uuid4().hex,
        'revision':previous['revision']+1 if previous else 1,'source_object':high['object'],'target_object':low['object'],
        'source_signature':high,'target_signature':low,'target_geometry_fingerprint':low['geometry_fingerprint'],
        'asset_id':asset.get('asset_id'),'source_role':'SOURCE_HIGH','target_role':'CANDIDATE_GAME_MESH',
        'metadata':metadata if metadata is not None else previous.get('metadata',{}) if previous else {},
        'previous_revision':previous.get('revision') if previous else None}
    path=_save(storage,'game_mesh',operation_id,'relationship.json',r)
    write_json(Path(path).with_name('relationship.integrity.json'),{'sha256':sha256(Path(path))})
    return path,r

def create_relationship(bridge,storage,operation_id,object_name,source_fingerprint,options=None):
    options=options or {};asset=require_mutable_asset(bridge,storage,'retopology_create_candidate')
    context=bridge.run(M5_RUNTIME+'\nresult=m5_asset_context()')
    high=signature(bridge,object_name)
    if high['fingerprint']!=source_fingerprint:raise ValueError('STALE_SIGNATURE: source high fingerprint changed')
    target=options.get('target_object',object_name)
    name=candidate_name(target,operation_id)
    existing=bridge.run(M5_RUNTIME+'\nobj=bpy.data.objects.get(params["name"])\nresult={"exists":obj is not None,"signature":m5_signature(obj) if obj else None}',{'name':name})
    if existing['exists']:raise ValueError('CANDIDATE_EXISTS: reuse the original operation result; no second copy created')
    from ..checkpoint import create_checkpoint
    cp=create_checkpoint(bridge,storage,'Before M5 game mesh candidate',operation_id,'retopology_create_candidate')['manifest']
    low=bridge.run(M5_RUNTIME+'\nm5_guard_asset_context(params["expected_asset_context"])\nhigh=m5_obj(params["high_name"])\n'
        'if m5_signature(high)["fingerprint"]!=params["expected_high_fingerprint"]:raise ValueError("STALE_SIGNATURE: high changed before copying")\n'
        'obj=m5_copy(m5_obj(params["target"]),params["name"],params["operation_id"])\nresult=m5_signature(obj)',
        {'high_name':object_name,'expected_high_fingerprint':high['fingerprint'],'expected_asset_context':context,'target':target,'name':name,'operation_id':operation_id})
    if signature(bridge,object_name)['fingerprint']!=high['fingerprint']:raise ValueError('SOURCE_CHANGED: candidate creation altered high')
    path,r=_relation(storage,operation_id,high,low,asset,options.get('metadata',{}))
    return {'status':'success','candidate':name,'relationship':path,'source_intact':True,'signature':low,'checkpoint':cp,
        'dependency_manifest':{'copied':['object','mesh','UV','weights','materials','shape_keys','properties','armature relationship'],
            'not_verified':['external objects targeting source','arbitrary shader graphs']},'warnings':['HIGH and previous LOW are preserved. Topology-changing steps declare attribute consequences separately.']}


def _public_proposal(proposal):
    output={k:v for k,v in proposal.items() if not k.startswith('_')}
    size=len(json.dumps(output,allow_nan=False).encode())
    if size>40000:
        return {'supported':proposal.get('supported',False),'strategy':proposal.get('strategy'),
                'warnings':proposal.get('warnings',[]),'detail':'Detailed proposal stored locally; payload budget exceeded.'}
    return output

def transaction(bridge,storage,operation_id,kind,runtime,relationship=None,mode='analyze',options=None,operation=None):
    if mode not in ('analyze','propose','preview','apply','verify','revert'):raise ValueError('INVALID_MODE: M5 correction mode')
    options=options or {};code=M5_RUNTIME+'\n'+runtime;backend=hashlib.sha256(code.encode()).hexdigest()
    if mode in ('analyze','propose'):
        if not relationship:raise ValueError('RELATIONSHIP_REQUIRED: explicit high/low manifest required')
        r=validate_relationship(bridge,storage,relationship)
        planned=bridge.run(code+'\nhigh=m5_obj(params["high_name"]);low=m5_obj(params["target_name"])\nresult={"proposal":m5_plan(high,low,params["options"],params["relation"]),"target_signature":m5_signature(low),"asset_context":m5_asset_context()}',
            {'high_name':r['source_object'],'target_name':r['target_object'],'relation':r,'options':options})
        proposal=planned['proposal'];public=_public_proposal(proposal)
        if mode=='analyze':return {'status':'warning','proposal':public,'warnings':proposal.get('warnings',[]),'source_intact':True}
        record={'schema':'production.m5_operation.v1','id':uuid4().hex,'kind':kind,'backend':backend,'relationship':relationship,'relation':r,
            'target_signature':planned['target_signature'],'asset_context':planned['asset_context'],'proposal':proposal,'options':options}
        folder=storage.reserve('game_mesh',operation_id);path=folder/'proposal.json';write_json(path,record)
        write_json(folder/'integrity.json',{'sha256':sha256(path)})
        return {'status':'warning','operation':str(path),'correction_id':record['id'],'proposal':public,'source_intact':True,'warnings':proposal.get('warnings',[])}
    if not operation:raise ValueError('OPERATION_REQUIRED: stored immutable proposal required')
    p=Path(operation).resolve(strict=True);record=load(storage,str(p),schema='production.m5_operation.v1')
    integrity=json.loads((p.parent/'integrity.json').read_text())
    if sha256(p)!=integrity['sha256'] or record['backend']!=backend or record['kind']!=kind:raise ValueError('STALE_PROPOSAL: proposal integrity/backend/kind changed')
    if relationship or options:raise ValueError('FROZEN_OPTIONS: use stored operation with empty options')
    if not record['proposal'].get('supported'):raise ValueError('UNSUPPORTED_OR_AMBIGUOUS: '+str(record['proposal'].get('errors',record['proposal'].get('warnings',[]))))
    r=record['relation'];options=record['options'];proposal=record['proposal']
    ledger=OperationLedger(storage);key='m5:'+record['id']+':'+mode
    if mode=='verify':
        require_mutable_asset(bridge,storage,kind+' verify (temporary evaluation)')
        previous=ledger.get('m5:'+record['id']+':apply') or ledger.get('m5:'+record['id']+':preview')
        if not previous or not previous.get('result',{}).get('candidate'):raise ValueError('CANDIDATE_REQUIRED: apply or preview first')
        result=previous['result'];candidate=result['candidate']
        validate_relationship(bridge,storage,record['relationship'])
        signatures=bridge.run(M5_RUNTIME+'\nresult={"low":m5_signature(m5_obj(params["low"])),"candidate":m5_signature(m5_obj(params["candidate"]))}',{'low':r['target_object'],'candidate':candidate})
        if signatures['low']['fingerprint']!=record['target_signature']['fingerprint'] or signatures['candidate']['fingerprint']!=result['candidate_signature']['fingerprint']:
            raise ValueError('STALE_PROPOSAL: original low or candidate changed since verification')
        verified=bridge.run(code+'\nm5_guard_asset_context(params["expected_asset_context"])\nresult=m5_verify(m5_obj(params["high_name"]),m5_obj(params["target_name"]),m5_obj(params["candidate"]),params["proposal"],params["options"])',
            {'high_name':r['source_object'],'target_name':r['target_object'],'candidate':candidate,'proposal':proposal,'options':options,'relation':r,'expected_asset_context':record['asset_context']})
        intact=signature(bridge,r['source_object'])['fingerprint']==r['source_signature']['fingerprint'] and signature(bridge,r['target_object'])['fingerprint']==record['target_signature']['fingerprint']
        passed=verified['passed'] and intact
        return dict(result,status='warning' if passed else 'error',source_intact=intact,numeric_verification='pass' if passed else 'fail',metrics=verified.get('metrics',{}),errors=verified.get('errors',[])+([] if intact else ['SOURCE_CHANGED: verification altered source']),visual_verification='required')
    require_mutable_asset(bridge,storage,kind+' '+mode)
    action,state=ledger.begin(key,kind,{'operation':str(p),'mode':mode})
    if action=='replay':return replayed(state)
    if action in ('in_progress','uncertain'):raise ValueError('OPERATION_OUTCOME_UNCERTAIN: inspect operation_status/checkpoint; no second candidate created')
    sent=False
    try:
        validate_relationship(bridge,storage,record['relationship'])
        if signature(bridge,r['target_object'])['fingerprint']!=record['target_signature']['fingerprint']:raise ValueError('STALE_PROPOSAL: previous low content changed')
        if record['target_signature'].get('unsupported_modifiers') or r['source_signature'].get('unsupported_modifiers'):
            raise ValueError('UNSUPPORTED_MODIFIER_SETTINGS: cannot freeze arbitrary modifier RNA safely')
        from ..checkpoint import create_checkpoint
        checkpoint=create_checkpoint(bridge,storage,'Before M5 '+kind+' '+mode,operation_id,kind)['manifest'];ledger.mark(key,'RUNNING',checkpoint=checkpoint)
        if mode=='revert':
            applied=ledger.get('m5:'+record['id']+':apply') or ledger.get('m5:'+record['id']+':preview')
            candidate=(applied or {}).get('result',{}).get('candidate')
            if candidate:
                if signature(bridge,candidate)['fingerprint']!=applied['result']['candidate_signature']['fingerprint']:
                    raise ValueError('STALE_PROPOSAL: candidate changed before revert')
                sent=True
                bridge.run(M5_RUNTIME+'\nm5_guard_asset_context(params["expected_asset_context"])\nobj=m5_obj(params["candidate"])\n'
                    'if m5_signature(obj)["fingerprint"]!=params["expected_candidate_fingerprint"]:raise ValueError("STALE_PROPOSAL: candidate changed before revert")\n'
                    'obj.hide_render=True\nresult={"preserved":obj.name}',{'candidate':candidate,'expected_asset_context':record['asset_context'],'expected_candidate_fingerprint':applied['result']['candidate_signature']['fingerprint']})
            outcome={'status':'success','reverted':True,'candidate_preserved':candidate,'relationship':record['relationship'],'checkpoint':checkpoint,'source_intact':True}
        else:
            name=candidate_name(r['target_object'],key)
            ledger.mark(key,'RUNNING',candidate_name=name)
            args={'high_name':r['source_object'],'target_name':r['target_object'],'candidate':name,'operation_id':operation_id,'proposal':proposal,'options':options,'relation':r,
                'expected_high_fingerprint':r['source_signature']['fingerprint'],'expected_low_fingerprint':record['target_signature']['fingerprint'],'expected_asset_context':record['asset_context']}
            sent=True
            data=bridge.run(code+r'''
m5_guard_asset_context(params['expected_asset_context'])
high=m5_obj(params['high_name']);low=m5_obj(params['target_name'])
source_before=m5_signature(high);low_before=m5_signature(low)
if source_before['fingerprint']!=params['expected_high_fingerprint'] or low_before['fingerprint']!=params['expected_low_fingerprint']:
    raise ValueError('STALE_PROPOSAL: high or previous low changed before atomic mutation')
candidate=m5_copy(low,params['candidate'],params['operation_id'],params['proposal'].get('candidate_role','GAME_MESH'))
changes=m5_apply(high,low,candidate,params['proposal'],params['options'])
verification=m5_verify(high,low,candidate,params['proposal'],params['options'])
source_after=m5_signature(high);low_after=m5_signature(low)
intact=source_before['fingerprint']==source_after['fingerprint'] and low_before['fingerprint']==low_after['fingerprint']
if not intact:
    verification['passed']=False;verification.setdefault('errors',[]).append('SOURCE_CHANGED: high or previous low changed')
result={'candidate':candidate.name,'signature':m5_signature(candidate),'changes':changes,'verification':verification,'source_intact':intact}
''',args)
            passed=data['verification']['passed'];relation_path=record['relationship']
            if passed and data['source_intact'] and proposal.get('candidate_role','GAME_MESH')!='CAGE':
                relation_path,_=_relation(storage,operation_id,r['source_signature'],data['signature'],current_asset(bridge,storage),previous=r)
            outcome={'status':'warning' if passed else 'error','candidate':data['candidate'],'candidate_signature':data['signature'],
                'relationship':relation_path,'operation':str(p),'checkpoint':checkpoint,'source_intact':data['source_intact'],
                'numeric_verification':'pass' if passed else 'fail','visual_verification':'required','before':record['target_signature']['counts'],
                'after':data['signature']['counts'],'metrics':dict(data['changes'] or {},**data['verification'].get('metrics',{})),
                'errors':data['verification'].get('errors',[]),'warnings':['Original HIGH and previous LOW retained; visual QA required.']}
        ledger.finish(key,'SUCCEEDED' if outcome.get('numeric_verification')!='fail' else 'FAILED',result=outcome)
        outcome['idempotency']=idempotency_block(dict(state,state='SUCCEEDED' if outcome.get('numeric_verification')!='fail' else 'FAILED'),False)
        return outcome
    except BaseException as exc:
        if sent or is_uncertain(exc):
            # A command may have created a partial candidate. Preserve it and never
            # execute again under this proposal after losing the outcome.
            ledger.mark(key,'UNCERTAIN',uncertainty=str(exc),mutation_sent=sent)
        else:ledger.abandon(key)
        raise
