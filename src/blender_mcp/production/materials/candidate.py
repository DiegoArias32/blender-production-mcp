"""Create owned material candidates without reassigning a source object's slots."""
from __future__ import annotations

from .integrity import RUNTIME as INTEGRITY_RUNTIME
from . import integrity


RUNTIME = INTEGRITY_RUNTIME + r'''
def m9_candidate_scope(params):
    generation=params.get('generation_id');version=params.get('version',1);name=params.get('candidate_name')
    if not isinstance(generation,str) or len(generation)!=32 or any(c not in '0123456789abcdef' for c in generation):
        m9_fail('MATERIAL_GENERATION_INVALID','32 lowercase hexadecimal characters required')
    if isinstance(version,bool) or not isinstance(version,int) or version<1:
        m9_fail('MATERIAL_VERSION_INVALID','positive integer version required')
    if not isinstance(name,str) or not name or len(name)>48:
        m9_fail('MATERIAL_CANDIDATE_NAME_INVALID','nonempty name at most 48 characters')
    return generation,version,name,'M9_'+generation+'_v'+str(version)

def m9_owned(obj,material,generation):
    if not obj or not material or obj.get('production_m9_owned') is not True or material.get('production_m9_owned') is not True:
        m9_fail('MATERIAL_CANDIDATE_OWNERSHIP','owned candidate object and material required')
    if obj.get('production_generation_id')!=generation or material.get('production_generation_id')!=generation:
        m9_fail('MATERIAL_CANDIDATE_OWNERSHIP','candidate generation mismatch')
    if not obj.data or obj.data.get('production_m9_owned') is not True or obj.data.get('production_generation_id')!=generation:
        m9_fail('MATERIAL_CANDIDATE_OWNERSHIP','owned candidate mesh required')
    if obj.data.users!=1:
        m9_fail('MATERIAL_CANDIDATE_SHARED','candidate mesh is shared')
    for owner in bpy.data.objects:
        if owner!=obj and any(s.material==material for s in owner.material_slots):
            m9_fail('MATERIAL_CANDIDATE_SHARED','candidate material is assigned to another object')
    if not any(s.material==material for s in obj.material_slots):
        m9_fail('MATERIAL_CANDIDATE_OWNERSHIP','material not assigned to owned object')

def m9_mark(item,generation,version,source):
    item['production_m9_owned']=True
    item['production_generation_id']=generation
    item['production_candidate_version']=version
    item['production_source']=source

def m9_make_candidate(params):
    before=m9_mutation_guard(params)
    generation,version,name,collection_name=m9_candidate_scope(params)
    material_name=name+'_Material'
    if bpy.data.objects.get(name) or bpy.data.meshes.get(name+'_Mesh') or bpy.data.materials.get(material_name) or bpy.data.collections.get(collection_name):
        m9_fail('MATERIAL_CANDIDATE_EXISTS','candidate names are already reserved')
    source=bpy.data.objects.get(params['object_name']);material=bpy.data.materials.get(params['material_name'])
    material_ad=material.animation_data;tree_ad=material.node_tree.animation_data if material.node_tree else None
    if material_ad or tree_ad:
        m9_fail('MATERIAL_ANIMATED_SHADER_UNSUPPORTED','candidate shader builder does not rewrite animated material/node channels')
    selected_indices=[i for i,s in enumerate(source.material_slots) if s.material==material]
    if not selected_indices:m9_fail('MATERIAL_NOT_ASSIGNED','source material assignment disappeared')
    created_obj=None;created_mesh=None;created_material=None;collection=None
    try:
        collection=bpy.data.collections.new(collection_name)
        m9_mark(collection,generation,version,source.name)
        bpy.context.scene.collection.children.link(collection)
        created_obj=source.copy();created_mesh=source.data.copy();created_obj.data=created_mesh
        created_obj.name=name;created_mesh.name=name+'_Mesh'
        m9_mark(created_obj,generation,version,source.name)
        m9_mark(created_mesh,generation,version,source.data.name)
        collection.objects.link(created_obj)
        created_material=material.copy();created_material.name=material_name
        m9_mark(created_material,generation,version,material.name)
        for index in selected_indices:
            # DATA and OBJECT linkage remain as authored, on the separate object/mesh.
            created_obj.material_slots[index].material=created_material
        created_obj.hide_render=True;created_obj.hide_set(True)
        created_obj['production_source_material']=material.name
        created_obj['production_source_object']=source.name
        created_obj['production_material_indices']=selected_indices
        after=m9_status(params)
        if after['runtime_fingerprint']!=before['runtime_fingerprint']:
            m9_fail('MATERIAL_SOURCE_CHANGED','source changed during isolated candidate creation')
        candidate_snapshot=m9_status({'object_name':created_obj.name,'material_name':created_material.name,'options':params.get('options',{})})
        return {'status':'CANDIDATE','generation_id':generation,'version':version,
            'candidate_object':created_obj.name,'candidate_material':created_material.name,
            'collection':collection.name,'source_object':source.name,'source_material':material.name,
            'material_indices':selected_indices,'source_preserved':True,
            'before_original_fingerprint':before['runtime_fingerprint'],'after_original_fingerprint':after['runtime_fingerprint'],
            'candidate_status':candidate_snapshot}
    except Exception:
        # Only datablocks allocated by this attempt may be rolled back.
        if created_obj:bpy.data.objects.remove(created_obj,do_unlink=True)
        if created_mesh and created_mesh.users==0:bpy.data.meshes.remove(created_mesh)
        if created_material and created_material.users==0:bpy.data.materials.remove(created_material)
        if collection:bpy.data.collections.remove(collection)
        raise
'''

CREATE = RUNTIME + "\nresult=m9_make_candidate(params)\n"


def create(bridge, params: dict) -> dict:
    """The caller owns registry validation, checkpoint and idempotency ledger."""
    current = integrity.status(bridge, params['object_name'], params['material_name'], params.get('options'))
    expected = params.get('expected_status', {})
    if not expected.get('fingerprint') or expected['fingerprint'] != current['fingerprint']:
        raise ValueError('MATERIAL_STALE_SOURCE: la fuente o un archivo dependiente cambió')
    return bridge.run(CREATE, params)
