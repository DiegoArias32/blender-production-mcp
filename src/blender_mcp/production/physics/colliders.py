"""Owned collision copies and explicit rigid AABB proxies; source meshes stay intact."""
from .integrity import RUNTIME as INTEGRITY_RUNTIME

RUNTIME = INTEGRITY_RUNTIME + r'''
from mathutils.bvhtree import BVHTree

def pcoll_prepare(params):
    current=px_status(params)
    if current['fingerprint']!=params['expected_status']['fingerprint']:
        px_fail('PHYSICS_STALE_SOURCE','collider dependencies changed')
    name=params['collider_collection_name']
    if bpy.data.collections.get(name):
        px_fail('PHYSICS_COLLIDER_EXISTS','use a new candidate version')
    parent=bpy.data.collections.get(params['candidate_collection'])
    if parent is None or parent.get('production_generation_id')!=params['generation_id']:
        px_fail('PHYSICS_CANDIDATE_OWNERSHIP','candidate collection missing')
    collection=bpy.data.collections.new(name);parent.children.link(collection)
    collection['production_m8_owned']=True;collection['production_generation_id']=params['generation_id']
    collection['production_candidate_version']=params['version']
    manifest=[]
    for index,source_name in enumerate(params['colliders']):
        source=px_object(source_name)
        if source.type!='MESH':
            px_fail('PHYSICS_COLLIDER_UNSUPPORTED','closed mesh collider required')
        obj=source.copy();obj.data=source.data.copy();obj.name=params['candidate_name']+'_COL_'+str(index)
        collection.objects.link(obj)
        obj['production_m8_owned']=True;obj['production_generation_id']=params['generation_id']
        obj['production_candidate_version']=params['version'];obj['production_source_object']=source_name
        obj.hide_render=True
        strategy=params.get('proxy_strategy','COPY');deviation=0.0
        original_vertices=len(source.data.vertices)
        if strategy=='AABB_PROXY':
            if source.modifiers or source.data.shape_keys:
                px_fail('PHYSICS_PROXY_LIMITED','AABB supports rigid geometry only; use an animated collision copy')
            coords=[v.co for v in source.data.vertices]
            low=[min(v[i] for v in coords) for i in range(3)];high=[max(v[i] for v in coords) for i in range(3)]
            vertices=[(low[0],low[1],low[2]),(high[0],low[1],low[2]),(high[0],high[1],low[2]),(low[0],high[1],low[2]),
                      (low[0],low[1],high[2]),(high[0],low[1],high[2]),(high[0],high[1],high[2]),(low[0],high[1],high[2])]
            faces=[(0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)]
            data=bpy.data.meshes.new(obj.name+'_Proxy');data.from_pydata(vertices,[],faces);data.update()
            prior=obj.data;obj.data=data
            if prior.users==0:
                bpy.data.meshes.remove(prior)
            tree=BVHTree.FromPolygons([source.matrix_world@v.co for v in source.data.vertices],
                                      [list(p.vertices) for p in source.data.polygons],all_triangles=False)
            deviation=max((tree.find_nearest(obj.matrix_world@v.co)[3] for v in data.vertices),default=0.0)
            if deviation>params.get('max_proxy_deviation_m',.1):
                px_fail('PHYSICS_PROXY_DEVIATION_EXCEEDED','conservative collider exceeds the reviewed surface deviation')
        elif strategy!='COPY':
            px_fail('PHYSICS_PROXY_STRATEGY_UNSUPPORTED','COPY or AABB_PROXY only')
        existing=[m for m in obj.modifiers if m.type=='COLLISION']
        modifier=existing[0] if len(existing)==1 else obj.modifiers.new('M8_COLLISION','COLLISION')
        if len(existing)>1:
            px_fail('PHYSICS_COLLIDER_AMBIGUOUS','multiple collision modifiers')
        modifier.show_viewport=True;modifier.show_render=True
        obj['production_m8_collision_modifier']=modifier.name
        manifest.append({'source':source_name,'candidate':obj.name,'strategy':strategy,'vertices_before':original_vertices,
                         'vertices_after':len(obj.data.vertices),'maximum_proxy_to_source_deviation_m':deviation,
                         'coverage':'rigid enclosing AABB' if strategy=='AABB_PROXY' else 'copied source deformation stack'})
    target=px_object(params['candidate_name']);target['production_collider_collection_name']=collection.name
    if px_status(params)['fingerprint']!=current['fingerprint']:
        px_fail('PHYSICS_PRIMARY_MODIFIED','collider preparation changed original data')
    return {'collider_collection_name':collection.name,'collider_names':[r['candidate'] for r in manifest],
            'manifest':manifest,'source_intact':True}
'''
PREPARE = RUNTIME + '\nresult=pcoll_prepare(params)\n'
