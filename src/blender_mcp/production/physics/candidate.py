"""Owned, separate M8 candidates; original body and facial stacks are untouched."""
from __future__ import annotations

from .integrity import RUNTIME as INTEGRITY_RUNTIME


RUNTIME = INTEGRITY_RUNTIME + r'''
def px_chain_guard(chain):
    rig=px_object(chain['armature'])
    if rig.type!='ARMATURE':
        px_fail('PHYSICS_CHAIN_INVALID','chain armature required')
    names=chain.get('bones',[])
    if not names or len(names)>64 or len(set(names))!=len(names):
        px_fail('PHYSICS_CHAIN_INVALID','1 to 64 distinct explicit secondary bones required')
    for index,name in enumerate(names):
        bone=rig.pose.bones.get(name)
        if bone is None or bone.constraints:
            px_fail('PHYSICS_CHAIN_INVALID','missing/constrained secondary bone '+name)
        if index and (not bone.parent or bone.parent.name!=names[index-1]):
            px_fail('PHYSICS_CHAIN_INVALID','explicit bones must form one ordered chain')
    animation=px_animation(rig)
    ad=rig.animation_data
    actions=[]
    if ad:
        if ad.action:
            actions.append(ad.action)
        actions.extend(s.action for t in ad.nla_tracks for s in t.strips if s.action)
    prefixes=[rig.pose.bones[name].path_from_id() for name in names]
    for action in actions:
        for slot,layer,strip,curve in action_curves(action,None):
            if any(curve.data_path.startswith(prefix+'.') or curve.data_path.startswith(prefix+'[') for prefix in prefixes):
                px_fail('PHYSICS_SECONDARY_PRIMARY_OVERLAP','primary Action already animates secondary chain '+action.name)
    return rig,names

def px_candidate_mark(obj,params,source):
    obj['production_m8_owned']=True
    obj['production_generation_id']=params['generation_id']
    obj['production_source_object']=source.name
    obj['production_candidate_version']=params.get('version',1)
    obj['production_subsystem']=params['subsystem']

def px_candidate_scope(params):
    name=params.get('candidate_name');generation=params.get('generation_id');version=params.get('version',1)
    if not isinstance(name,str) or not name or not isinstance(generation,str) or not generation:
        px_fail('PHYSICS_CANDIDATE_INVALID','candidate_name and generation_id required')
    if len(generation)!=32 or any(c not in '0123456789abcdef' for c in generation):
        px_fail('PHYSICS_GENERATION_ID_INVALID','generation_id must be a 32-character lowercase UUID hex')
    if isinstance(version,bool) or not isinstance(version,int) or version<1:
        px_fail('PHYSICS_CANDIDATE_VERSION_INVALID','version must be an integer >=1')
    return name,generation,version,'M8_'+generation+'_v'+str(version)

def px_make_candidate(params):
    # The atomic recheck occurs in Blender immediately before any new datablock.
    current=px_status(params)
    expected=params.get('expected_status')
    if not isinstance(expected,dict) or not expected.get('fingerprint') or expected['fingerprint']!=current['fingerprint']:
        px_fail('PHYSICS_STALE_SOURCE','reanalyze primary body/facial/source context')
    for scene in bpy.data.scenes:
        stamp=scene.get('production_asset')
        if isinstance(stamp,str) and stamp.startswith('{') and json.loads(stamp).get('role')=='MASTER':
            px_fail('MASTER_ASSET_READ_ONLY','M8 candidate creation on MASTER')
    source=px_object(params['source_object'])
    subsystem=params.get('subsystem')
    if subsystem not in ('CLOTH','HAIR','SPRING_BONE','DANGLING_ACCESSORY','SOFT_REGION','CHAIN_LIKE','CABLE_LIKE'):
        px_fail('UNSUPPORTED_BY_CURRENT_BLENDER_API','secondary candidate subsystem')
    name,generation,version,collection_name=px_candidate_scope(params)
    if bpy.data.objects.get(name) or bpy.data.collections.get(collection_name):
        px_fail('PHYSICS_CANDIDATE_EXISTS','candidate name/generation already exists')
    if source.type not in ('MESH','ARMATURE'):
        px_fail('UNSUPPORTED_BY_CURRENT_BLENDER_API','candidate source must be mesh or armature')
    if source.type=='MESH' and source.data.shape_keys:
        px_fail('PHYSICS_SECONDARY_MORPH_UNSUPPORTED','secondary mesh morphs require a separate adapter; primary morphs are preserved')
    if any(m.type in ('CLOTH','SOFT_BODY','PARTICLE_SYSTEM','NODES') for m in source.modifiers):
        px_fail('PHYSICS_SOURCE_SOLVER_UNSUPPORTED','source already contains a simulation/dynamic system')
    chain=params.get('chain');rig=None;bones=[]
    if chain:
        rig,bones=px_chain_guard(chain)
    if source.type=='ARMATURE' and (not chain or rig!=source):
        px_fail('PHYSICS_CHAIN_INVALID','armature source requires its explicit secondary chain')
    if subsystem in ('HAIR','SPRING_BONE','CHAIN_LIKE','CABLE_LIKE') and not chain:
        px_fail('UNSUPPORTED_BY_CURRENT_BLENDER_API','bone backend requires an explicit secondary chain')
    if rig and source.type=='MESH' and not any(m.type=='ARMATURE' and m.object==rig for m in source.modifiers):
        px_fail('PHYSICS_CHAIN_INVALID','hair mesh must be bound to declared armature')
    created=[];collection=None
    try:
        collection=bpy.data.collections.new(collection_name)
        collection['production_m8_owned']=True;collection['production_generation_id']=generation
        collection['production_candidate_version']=version
        bpy.context.scene.collection.children.link(collection)
        clone=None
        if rig:
            clone=rig.copy();clone.data=rig.data.copy();clone.name=name if source==rig else name+'_Rig'
            collection.objects.link(clone);created.append(clone)
            px_candidate_mark(clone,params,rig);clone.hide_render=True
            if clone.parent==rig:
                px_fail('PHYSICS_CHAIN_INVALID','self-parented candidate rig')
        if source.type=='MESH':
            candidate=source.copy();candidate.data=source.data.copy();candidate.name=name
            collection.objects.link(candidate);created.append(candidate)
            px_candidate_mark(candidate,params,source);candidate.hide_render=True
            if clone:
                for modifier in candidate.modifiers:
                    if modifier.type=='ARMATURE' and modifier.object==rig:
                        modifier.object=clone
                if candidate.parent==rig:
                    candidate.parent=clone
                candidate['production_candidate_rig']=clone.name
        else:
            candidate=clone
        after=px_status(params)
        if after['fingerprint']!=current['fingerprint']:
            px_fail('PHYSICS_PRIMARY_MODIFIED','original source or primary stack changed during copy')
        candidate_params={'candidate_object':candidate.name,'generation_id':generation}
        if clone and clone!=candidate:
            candidate_params['candidate_rig']=clone.name
        status=px_candidate_status(candidate_params)
        return {'candidate':candidate.name,'candidate_object':candidate.name,
            'candidate_rig':clone.name if clone else None,'collection':collection.name,
            'generation_id':generation,'version':params.get('version',1),'subsystem':subsystem,
            'source_map':{o.name:o.get('production_source_object') for o in created},
            'chain':{'armature':clone.name,'bones':bones} if clone else None,
            'before_original_fingerprint':current['fingerprint'],'after_original_fingerprint':after['fingerprint'],
            'primary_preserved':True,'candidate_status':status,'status':'SECONDARY_CANDIDATE'}
    except Exception:
        for obj in reversed(created):
            data=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if data and data.users==0:
                if isinstance(data,bpy.types.Mesh):
                    bpy.data.meshes.remove(data)
                elif isinstance(data,bpy.types.Armature):
                    bpy.data.armatures.remove(data)
        if collection:
            bpy.data.collections.remove(collection)
        raise
'''

CREATE = RUNTIME + "\nresult=px_make_candidate(params)\n"


def create(bridge, params: dict) -> dict:
    """Caller supplies the existing asset guard/checkpoint/operation transaction."""
    return bridge.run(CREATE, params)
