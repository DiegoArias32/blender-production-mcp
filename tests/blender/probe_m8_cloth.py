"""Real M8 cloth/PC2 and collision/stability probes, only fresh factory fixtures.

Run with --background --factory-startup --disable-autoexec --python this-file.
Optional -- OUTPUT_DIRECTORY must be new. Artifacts and failed runs are retained.
"""
import array
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import time
import traceback
import uuid

import bpy

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO/'src'))
def load_backend(name):
    spec=importlib.util.spec_from_file_location('m8_probe_'+name,REPO/'src'/'blender_mcp'/'production'/'physics'/(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
_bundle=subprocess.run([str(REPO/'.venv'/'Scripts'/'python.exe'),'-c',
                       "import json;from blender_mcp.production.physics import qa,cloth;print(json.dumps({'qa':qa.RUNTIME,'cloth':cloth.RUNTIME}))"],
                      cwd=str(REPO),capture_output=True,text=True,encoding='utf-8',check=True)
_runtimes=json.loads(_bundle.stdout)

if '--' in sys.argv:
    OUT = Path(sys.argv[sys.argv.index('--')+1]).resolve()
else:
    OUT = REPO/'test-artifacts'/'m8'/('cloth-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
OUT.mkdir(parents=True,exist_ok=False)
REPORT = {'schema':'m8.cloth_runtime_probe.v1','output_directory':str(OUT),
          'blender':bpy.app.version_string,'build_hash':bpy.app.build_hash.decode(),
          'checks':{},'errors':[]}
NAMESPACE = {}
exec(_runtimes['qa']+_runtimes['cloth'],NAMESPACE)
RNA_GUARDS=[]


def manual_rna_state():
    return {obj.name:{'location':tuple(obj.location),'quaternion':tuple(obj.rotation_quaternion),
                      'keys':{k.name:k.value for k in obj.data.shape_keys.key_blocks} if obj.type=='MESH' and obj.data.shape_keys else {},
                      'pose':{b.name:tuple(b.rotation_quaternion) for b in obj.pose.bones} if obj.type=='ARMATURE' else {}}
            for obj in RNA_GUARDS}


def call(name,params):
    scene=bpy.context.scene
    for guard in RNA_GUARDS:
        if guard.type=='MESH':
            guard.data.shape_keys.key_blocks['Smile'].value=.63
        elif guard.type=='ARMATURE':
            guard.pose.bones['Explicit_Pose_Control'].rotation_quaternion=(.995004165,0,0,.099833417)
    if RNA_GUARDS:
        bpy.context.view_layer.update()
    before=(scene.frame_current,scene.frame_subframe,
            tuple(sorted(o.name for o in bpy.context.selected_objects)),
            bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None)
    protected=manual_rna_state()
    if isinstance(params,dict) and RNA_GUARDS:
        params=dict(params,primary_objects=list(dict.fromkeys(params.get('primary_objects',[])+[o.name for o in RNA_GUARDS])))
    result=NAMESPACE[name](params)
    after=(scene.frame_current,scene.frame_subframe,
           tuple(sorted(o.name for o in bpy.context.selected_objects)),
           bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None)
    if before!=after:
        raise AssertionError('PHYSICS_RUNTIME_STATE_NOT_RESTORED: '+name)
    if protected!=manual_rna_state():
        REPORT.setdefault('state_restore_failures',[]).append({'helper':name,'before':protected,'after':manual_rna_state()})
        raise AssertionError('PHYSICS_MANUAL_PRIMARY_RNA_NOT_RESTORED: '+name)
    result['frame_selection_restored']=True
    result['manual_primary_rna_restored']=True
    return result


def own(obj,generation='probecloth'):
    obj['production_m8_owned']=True
    obj['production_generation_id']=generation
    obj['production_candidate_version']=1
    obj['production_subsystem']='CLOTH'
    return obj


def make_cloth(name,offset=(0,0,1),grid=8):
    n=grid
    points=[((x/n-.5)*1.0,(y/n-.5)*1.0,0) for y in range(n+1) for x in range(n+1)]
    faces=[(y*(n+1)+x,y*(n+1)+x+1,(y+1)*(n+1)+x+1,(y+1)*(n+1)+x) for y in range(n) for x in range(n)]
    mesh=bpy.data.meshes.new(name+'_Mesh')
    mesh.from_pydata(points,[],faces)
    mesh.update()
    obj=bpy.data.objects.new(name,mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location=offset
    group=obj.vertex_groups.new(name='M8_PINS')
    group.add([0,n],1.0,'REPLACE')
    return obj


def copied(obj,name,generation):
    result=obj.copy()
    result.data=obj.data.copy()
    result.name=name
    bpy.context.scene.collection.objects.link(result)
    return own(result,generation)


def collider_set(generation):
    collection=bpy.data.collections.new('M8_COLLIDERS_'+generation)
    collection['production_m8_owned']=True
    collection['production_generation_id']=generation
    collection['production_candidate_version']=1
    scene.collection.children.link(collection)
    bpy.ops.mesh.primitive_cube_add(size=2,location=(0,0,-10))
    collider=own(bpy.context.object,generation)
    collider.name='M8_Far_Collider_'+generation
    collider.modifiers.new('M8_COLLISION','COLLISION')
    collection.objects.link(collider)
    return collection,[collider.name]


def coords(obj):
    depsgraph=bpy.context.evaluated_depsgraph_get()
    evaluated=obj.evaluated_get(depsgraph)
    mesh=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=depsgraph)
    try:
        result=array.array('f',[0])*(len(mesh.vertices)*3)
        mesh.vertices.foreach_get('co',result)
        return result.tobytes()
    finally:
        evaluated.to_mesh_clear()


def capture(name,fn):
    started=time.perf_counter()
    try:
        result=fn()
        REPORT['checks'][name]=result
        REPORT['checks'][name]['elapsed_ms']=(time.perf_counter()-started)*1000
        print('M8_CLOTH_CHECK',name,result.get('passed'),flush=True)
    except Exception as exc:
        REPORT['errors'].append({'check':name,'error':str(exc),'traceback':traceback.format_exc()})
        print('M8_CLOTH_ERROR',name,str(exc),flush=True)
    (OUT/'report.json').write_text(json.dumps(REPORT,indent=2),encoding='utf-8')


scene=bpy.context.scene
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
scene.render.fps=30
scene.render.fps_base=1
scene.unit_settings.system='METRIC'
scene.unit_settings.scale_length=1
scene.frame_start=1
scene.frame_end=18
scene.frame_set(7)
source=make_cloth('M8_Cloth_Source')
candidate=copied(source,'M8_Cloth_Candidate','probecloth')
collider_collection,collider_names=collider_set('probecloth')
manual_face=make_cloth('M8_Manual_Face_Source',offset=(5,0,3))
manual_face.shape_key_add(name='Basis')
manual_smile=manual_face.shape_key_add(name='Smile')
manual_smile.value=.2
manual_smile.keyframe_insert(data_path='value',frame=1)
manual_smile.value=.8
manual_smile.keyframe_insert(data_path='value',frame=18)
manual_smile.value=.63
manual_armature=bpy.data.armatures.new('M8_Manual_Pose_Data')
manual_rig=bpy.data.objects.new('A_M8_Manual_Pose_Source',manual_armature)
scene.collection.objects.link(manual_rig)
bpy.ops.object.select_all(action='DESELECT')
manual_rig.select_set(True)
bpy.context.view_layer.objects.active=manual_rig
bpy.ops.object.mode_set(mode='EDIT')
manual_bone=manual_armature.edit_bones.new('Explicit_Pose_Control')
manual_bone.head=(5,0,0)
manual_bone.tail=(5,1,0)
bpy.ops.object.mode_set(mode='OBJECT')
pose=manual_rig.pose.bones['Explicit_Pose_Control']
pose.rotation_mode='QUATERNION'
pose.rotation_quaternion=(1,0,0,0)
pose.keyframe_insert(data_path='rotation_quaternion',frame=1)
pose.rotation_quaternion=(.980066578,0,0,.198669331)
pose.keyframe_insert(data_path='rotation_quaternion',frame=18)
pose.rotation_quaternion=(.995004165,0,0,.099833417)
RNA_GUARDS.extend((manual_face,manual_rig))
source_hash=hashlib.sha256(coords(source)).hexdigest()
settings={'mass':.3,'tension_stiffness':15.,'compression_stiffness':15.,'shear_stiffness':5.,
          'bending_stiffness':.5,'air_damping':1.,'quality':5,'pin_stiffness':1.,
          'use_collision':True,'collision_quality':2,'distance_min':.015,'use_self_collision':True,
          'self_distance_min':.015,'self_friction':5.}
setup_params={'candidate_name':candidate.name,'generation_id':'probecloth','settings':settings,'pin_group':'M8_PINS',
              'collider_collection_name':collider_collection.name,'collider_names':collider_names}


def native_setup():
    result=call('pc_setup',setup_params)
    result['passed']=result['backend']=='BLENDER_NATIVE_CLOTH' and result['settings']['use_self_collision']
    return result


capture('native_setup',native_setup)
SIM_PARAMS={'candidate_name':candidate.name,'generation_id':'probecloth','source_name':source.name,
            'frame_start':1,'frame_end':18,'fps':30,'gravity':list(scene.gravity),'max_vertices':1000,
            'thresholds':{'max_displacement_m':2.,'max_velocity_m_s':20.,'max_acceleration_m_s2':500.}}
capture('native_guarded_simulation',lambda:call('pc_simulate',SIM_PARAMS))
capture('pin_attachment_and_dense_motion',lambda:call('pq_motion',{
    'candidate_name':candidate.name,'source_name':source.name,'frames':list(range(1,19)),
    'anchor_indices':[0,8],'fps':30,'max_vertices':1000,
    'thresholds':{'max_displacement_m':2.,'max_velocity_m_s':20.,'max_acceleration_m_s2':500.}}))


def heavy_quad_retessellation():
    primary=make_cloth('M8_Cloth_625_Source',offset=(0,0,1.25),grid=24)
    heavy=copied(primary,'M8_Cloth_625_Candidate','probe625')
    collection,names=collider_set('probe625')
    native=dict(settings,mass=.8,tension_stiffness=50.,compression_stiffness=50.,shear_stiffness=20.,bending_stiffness=5.,use_self_collision=False)
    call('pc_setup',{'candidate_name':heavy.name,'generation_id':'probe625','settings':native,'pin_group':'M8_PINS',
                     'collider_collection_name':collection.name,'collider_names':names})
    simulation=call('pc_simulate',dict(SIM_PARAMS,candidate_name=heavy.name,generation_id='probe625',source_name=primary.name))
    result=call('pq_motion',{'candidate_name':heavy.name,'source_name':primary.name,'frames':list(range(1,19)),
                             'anchor_indices':[0,24],'fps':30,'max_vertices':1000,
                             'thresholds':SIM_PARAMS['thresholds']})
    old=scene.frame_current
    changed=[]
    try:
        for frame in range(1,19):
            scene.frame_set(frame)
            source_sample=call('pq_snapshot',primary)
            candidate_sample=call('pq_snapshot',heavy)
            changed.append({'frame':frame,'same_polygon_cycles_and_edges':NAMESPACE['same_topology'](source_sample,candidate_sample),
                            'same_loop_triangles':source_sample['triangles']==candidate_sample['triangles']})
    finally:
        scene.frame_set(old)
    result['retessellation_samples']=changed
    result['triangulation_changes_observed']=sum(not r['same_loop_triangles'] for r in changed)
    result['simulation_passed']=simulation['passed']
    result['passed']=result['passed'] and simulation['passed'] and all(r['same_polygon_cycles_and_edges'] for r in changed)
    return result


capture('heavy_625_quad_retessellation_preserves_topology',heavy_quad_retessellation)


def actual_quad_diagonal_change():
    primary=make_cloth('M8_Quad_Reference',offset=(0,0,5),grid=1)
    deform=copied(primary,'M8_Quad_Bent','probequad')
    deform.data.vertices[1].co=(-.25,.25,.1)
    deform.data.update()
    a=call('pq_snapshot',primary)
    b=call('pq_snapshot',deform)
    result=call('pq_motion',{'candidate_name':deform.name,'source_name':primary.name,'frames':[1,2,3]})
    result['loop_triangles_changed']=a['triangles']!=b['triangles']
    result['polygon_cycles_equal']=a['polygons']==b['polygons']
    result['edges_equal']=a['edges']==b['edges']
    result['reference_triangles']=a['triangles']
    result['candidate_triangles']=b['triangles']
    result['passed']=result['passed'] and result['loop_triangles_changed'] and result['polygon_cycles_equal'] and result['edges_equal']
    changed=copied(primary,'M8_Quad_Topology_Changed','proberealtopo')
    data=bpy.data.meshes.new('M8_Changed_Polygon_Cycle')
    data.from_pydata([tuple(v.co) for v in primary.data.vertices],[],[(0,3,2,1)])
    data.update()
    changed.data=data
    error=None
    try:
        call('pq_motion',{'candidate_name':changed.name,'source_name':primary.name,'frames':[1]})
    except ValueError as exc:
        error=str(exc)
    result['real_polygon_cycle_change_rejected']=error
    result['passed']=result['passed'] and bool(error and error.startswith('PRIMARY_REFERENCE_TOPOLOGY_MISMATCH'))
    return result


capture('actual_quad_diagonal_vs_real_topology_change',actual_quad_diagonal_change)


def portable():
    samples=call('pc_collect',{'candidate_name':candidate.name,'generation_id':'probecloth',
                             'frames':list(range(1,19)),'max_vertices':1000,'max_bytes':1000000})
    (OUT/'samples_internal.json').write_text(json.dumps(samples),encoding='utf-8')
    frames=samples['frames']
    count=frames[0]['vertex_count']
    data=struct.pack('<12siiffi',b'POINTCACHE2\0',1,count,float(frames[0]['frame']),1.,len(frames))+b''.join(bytes.fromhex(f['hex']) for f in frames)
    path=OUT/'cloth.pc2'
    with path.open('xb') as stream:
        stream.write(data)
    baked=copied(candidate,'M8_Cloth_Baked','probecloth')
    playback=call('pc_playback',{'candidate_name':baked.name,'generation_id':'probecloth',
                               'filepath':str(path),'host_path_validated':True,'frame_start':1})
    result=call('pq_bake_compare',{'native_candidate_name':candidate.name,'baked_candidate_name':baked.name,
                                 'frames':list(range(1,19)),'max_vertices':1000,'tolerance_m':.000001})
    unordered=[]
    for frame in (18,9,1,18,9):
        scene.frame_set(frame)
        a,b=coords(candidate),coords(baked)
        unordered.append({'frame':frame,'exact_bytes_equal':a==b,'native_sha256':hashlib.sha256(a).hexdigest(),'baked_sha256':hashlib.sha256(b).hexdigest()})
    result['out_of_order']=unordered
    result['passed']=result['passed'] and all(f['exact_bytes_equal'] for f in unordered)
    result['pc2']={'path':str(path),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'playback':playback}
    scene.frame_set(7)
    fixture=OUT/'cloth_runtime.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(fixture),check_existing=False)
    result['fixture']=str(fixture)
    result['fixture_sha256']=hashlib.sha256(fixture.read_bytes()).hexdigest()
    return result


capture('portable_bake_equivalence',portable)


def negative_explosion():
    bad=copied(source,'M8_Explosion_Candidate','probeexplosion')
    collection,names=collider_set('probeexplosion')
    call('pc_setup',{'candidate_name':bad.name,'generation_id':'probeexplosion','settings':settings,'pin_group':'M8_PINS',
                     'collider_collection_name':collection.name,'collider_names':names})
    bad.location.x=0
    bad.keyframe_insert(data_path='location',frame=1)
    bad.location.x=100
    bad.keyframe_insert(data_path='location',frame=2)
    bad.location.x=100
    bad.keyframe_insert(data_path='location',frame=18)
    params=dict(SIM_PARAMS,candidate_name=bad.name,generation_id='probeexplosion')
    result=call('pc_simulate',params)
    native=bad.modifiers[bad.get('production_m8_cloth_modifier')]
    result['negative_detected']=not result['passed'] and result['frames_processed']==2 and not result['cache_frozen'] and result['solver_disabled'] and not native.show_viewport and not native.show_render
    result['passed']=result['negative_detected']
    return result


capture('negative_explosion_stops_before_bake',negative_explosion)


def negative_minimum_area():
    needle=copied(source,'M8_Needle_Cloth','probeneedle')
    for vertex in needle.data.vertices:
        vertex.co.y*=.005
    collection,names=collider_set('probeneedle')
    call('pc_setup',{'candidate_name':needle.name,'generation_id':'probeneedle','settings':settings,'pin_group':'M8_PINS',
                     'collider_collection_name':collection.name,'collider_names':names})
    params=dict(SIM_PARAMS,candidate_name=needle.name,generation_id='probeneedle',thresholds=dict(SIM_PARAMS['thresholds'],min_area_ratio=.05))
    result=call('pc_simulate',params)
    result['negative_detected']=not result['passed'] and result['frames_processed']==1 and not result['cache_frozen'] and result['solver_disabled'] and 'MIN_AREA_RATIO_BELOW_THRESHOLD' in result['error_codes'] and result['qa'][0]['geometry']['degenerate_fraction']==0
    result['passed']=result['negative_detected']
    return result


capture('negative_min_area_stops_before_bake',negative_minimum_area)


def collision_negative():
    bpy.ops.mesh.primitive_cube_add(size=2,location=(0,0,0))
    collider=bpy.context.object
    collider.name='M8_Closed_Collider'
    test=make_cloth('M8_Penetrating_Surface',offset=(0,0,0))
    result=call('pq_collision',{'candidate_name':test.name,'collider_names':[collider.name],
                              'frames':[1,2,3],'max_vertices':1000})
    result['negative_detected']=not result['passed'] and result['penetration_vertex_frame_pairs']==81*3 and result['max_penetration_depth_m']>=.49
    result['passed']=result['negative_detected']
    # Reversing all face winding must not change inside/outside classification.
    for polygon in collider.data.polygons:
        polygon.flip()
    reversed_result=call('pq_collision',{'candidate_name':test.name,'collider_names':[collider.name],
                                       'frames':[1],'max_vertices':1000})
    result['reversed_normal_count']=reversed_result['penetration_vertex_frame_pairs']
    result['passed']=result['passed'] and reversed_result['penetration_vertex_frame_pairs']==81
    return result


capture('negative_penetration_closed_parity',collision_negative)


def open_unknown():
    open_collider=make_cloth('M8_Open_Collider',offset=(0,0,0))
    result=call('pq_collision',{'candidate_name':source.name,'collider_names':[open_collider.name],
                              'frames':[1],'max_vertices':1000})
    result['negative_detected']=not result['passed'] and any('UNKNOWN' in e for e in result['frames'][0]['errors'])
    result['passed']=result['negative_detected']
    return result


capture('negative_open_collider_unknown_blocks',open_unknown)


def explicit_collision_filter():
    disabled=copied(source,'M8_No_Collision','probenocollision')
    explicit=dict(settings,use_collision=False)
    result=call('pc_setup',{'candidate_name':disabled.name,'generation_id':'probenocollision',
                           'settings':explicit,'pin_group':'M8_PINS'})
    invalid=copied(source,'M8_Undeclared_Collider_Candidate','probeinvalidcollider')
    error=None
    try:
        call('pc_setup',{'candidate_name':invalid.name,'generation_id':'probeinvalidcollider',
                         'settings':settings,'pin_group':'M8_PINS'})
    except ValueError as exc:
        error=str(exc)
    result['missing_colliders_rejected']=error
    result['passed']=result['collisions']['status']=='NOT_APPLICABLE' and bool(error and error.startswith('EXPLICIT_CLOTH_COLLIDERS_REQUIRED')) and not invalid.modifiers
    result['owned_filter_collection']=candidate.modifiers[candidate.get('production_m8_cloth_modifier')].collision_settings.collection.name
    result['passed']=result['passed'] and result['owned_filter_collection']==collider_collection.name
    return result


capture('explicit_collision_collection_or_not_applicable',explicit_collision_filter)


def same_frame_soft_reference():
    primary=make_cloth('M8_Moving_Primary',offset=(0,0,3))
    primary.location.x=0
    primary.keyframe_insert(data_path='location',frame=1)
    primary.location.x=.25
    primary.keyframe_insert(data_path='location',frame=9)
    soft=copied(primary,'M8_Local_Soft','probesoft')
    for index in range(72,81):
        soft.data.vertices[index].co.z+=.05
    args={'candidate_name':soft.name,'source_name':primary.name,'frames':list(range(1,10)),
          'anchor_indices':[0,8],'outside_region_indices':list(range(72)),
          'secondary_displacement_limit_m':.06,'max_vertices':1000}
    result=call('pq_motion',args)
    result['passed']=result['passed'] and abs(result['metrics']['max_secondary_displacement_m']-.05)<1e-6 and result['metrics']['max_secondary_velocity_m_s']<1e-5
    negative=call('pq_motion',dict(args,secondary_displacement_limit_m=.04))
    result['strict_bound_negative']=negative
    result['passed']=result['passed'] and not negative['passed'] and any('SECONDARY_DISPLACEMENT_BOUND_EXCEEDED' in f['errors'] for f in negative['frames'])
    return result


capture('moving_primary_soft_relative_bound',same_frame_soft_reference)


def closed_volume_indicators():
    mesh=bpy.data.meshes.new('M8_Closed_Volume_Data')
    mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0),(0,0,1)],[],[(0,2,1),(0,1,3),(1,2,3),(2,0,3)])
    mesh.update()
    primary=bpy.data.objects.new('M8_Closed_Volume_Primary',mesh)
    scene.collection.objects.link(primary)
    primary.location=(0,0,4)
    primary.location.x=0
    primary.keyframe_insert(data_path='location',frame=1)
    primary.location.x=.25
    primary.keyframe_insert(data_path='location',frame=3)
    changed=copied(primary,'M8_Closed_Volume_Secondary','probevolume')
    for vertex in changed.data.vertices:
        vertex.co.z*=.8
    args={'candidate_name':changed.name,'source_name':primary.name,'frames':[1,2,3]}
    result=call('pq_motion',args)
    result['passed']=result['passed'] and abs(result['geometry_summary']['min_absolute_volume_ratio']-.8)<1e-5 and result['geometry_summary']['inversion_count'] is None and 'CLOSED_VOLUME_CHANGE_MEASURED' in result['geometry_summary']['stability_indicators']
    inverted=copied(primary,'M8_Closed_Volume_Inverted','probeinverted')
    for vertex in inverted.data.vertices:
        vertex.co.z*=-1
    negative=call('pq_motion',dict(args,candidate_name=inverted.name))
    result['negative_global_volume_inversion']=negative
    result['passed']=result['passed'] and not negative['passed'] and negative['geometry_summary']['global_closed_volume_inversion_frames']==[1,2,3] and negative['geometry_summary']['inversion_count'] is None
    return result


capture('closed_volume_indicators_and_global_inversion',closed_volume_indicators)


def attachment_negative():
    dangling=copied(source,'M8_Detached_Origin','probedetach')
    dangling.location.x+=.02
    result=call('pq_motion',{'candidate_name':dangling.name,'source_name':source.name,
                             'frames':[1,2,3],'anchor_mode':'OBJECT_ORIGIN'})
    result['negative_detected']=not result['passed'] and any('ANCHOR_ORIGIN_DETACHMENT_EXCEEDED' in f['errors'] for f in result['frames'])
    result['passed']=result['negative_detected']
    return result


capture('negative_accessory_anchor_origin',attachment_negative)


def actual_object_angle_limit():
    primary=make_cloth('M8_Angular_Primary',offset=(0,0,4),grid=1)
    candidate=copied(primary,'M8_Angular_Candidate','probeangle')
    primary.rotation_mode=candidate.rotation_mode='QUATERNION'
    candidate.rotation_quaternion=(.984807753,0,0,.173648178)
    args={'candidate_name':candidate.name,'source_name':primary.name,'frames':[1,2,3],
          'anchor_mode':'OBJECT_ORIGIN','secondary_angle_limit_deg':25}
    result=call('pq_motion',args)
    strict=call('pq_motion',dict(args,secondary_angle_limit_deg=10))
    result['negative_strict_angle_limit']=strict
    result['passed']=result['passed'] and abs(result['metrics']['max_secondary_angle_deg']-20)<.0001 and not strict['passed'] and any('SECONDARY_ANGLE_BOUND_EXCEEDED' in f['errors'] for f in strict['frames'])
    return result


capture('actual_object_secondary_angle_limit',actual_object_angle_limit)


def make_chain():
    data=bpy.data.armatures.new('M8_Chain_Data')
    rig=bpy.data.objects.new('M8_Chain_Primary',data)
    scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    bpy.context.view_layer.objects.active=rig
    bpy.ops.object.mode_set(mode='EDIT')
    a=data.edit_bones.new('Root_Segment')
    a.head=(0,0,0)
    a.tail=(0,1,0)
    b=data.edit_bones.new('Tip_Segment')
    b.head=(0,1,0)
    b.tail=(0,2,0)
    b.parent=a
    b.use_connect=True
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.location.x=0
    rig.keyframe_insert(data_path='location',frame=1)
    rig.location.x=.25
    rig.keyframe_insert(data_path='location',frame=18)
    return rig


def bone_motion_and_bake():
    primary=make_chain()
    candidate=copied(primary,'M8_Chain_Secondary','probechain')
    candidate.animation_data.action=primary.animation_data.action.copy()
    tip=candidate.pose.bones['Tip_Segment']
    tip.rotation_mode='XYZ'
    tip.rotation_euler.z=0
    tip.keyframe_insert(data_path='rotation_euler',frame=1)
    tip.rotation_euler.z=.1
    tip.keyframe_insert(data_path='rotation_euler',frame=9)
    tip.rotation_euler.z=0
    tip.keyframe_insert(data_path='rotation_euler',frame=18)
    names=['Root_Segment','Tip_Segment']
    result=call('pq_motion',{'candidate_name':candidate.name,'source_name':primary.name,
                             'bone_names':names,'frames':list(range(1,19)),
                             'secondary_displacement_limit_m':.2,'secondary_angle_limit_deg':10})
    strict_angle=call('pq_motion',{'candidate_name':candidate.name,'source_name':primary.name,
                                  'bone_names':names,'frames':list(range(1,19)),
                                  'secondary_displacement_limit_m':.2,'secondary_angle_limit_deg':2})
    result['strict_angle_negative']=strict_angle
    result['passed']=result['passed'] and not strict_angle['passed'] and any('SECONDARY_ANGLE_BOUND_EXCEEDED' in f['errors'] for f in strict_angle['frames'])
    baked=copied(candidate,'M8_Chain_Baked','probechain')
    baked.animation_data.action=candidate.animation_data.action.copy()
    equivalence=call('pq_bake_compare',{'native_candidate_name':candidate.name,'baked_candidate_name':baked.name,
                                      'bone_names':names,'frames':list(range(1,19)),
                                      'tolerance_m':.000001,'rotation_tolerance_deg':.001})
    result['bake_equivalence']=equivalence
    result['passed']=result['passed'] and equivalence['passed'] and equivalence['coverage']['all_pose_bones']
    # An actual written rotation change must fail the angle/end-point gate.
    action=baked.animation_data.action.copy()
    baked.animation_data.action=action
    baked.pose.bones['Tip_Segment'].rotation_euler.z=.5
    baked.pose.bones['Tip_Segment'].keyframe_insert(data_path='rotation_euler',frame=9)
    changed=call('pq_bake_compare',{'native_candidate_name':candidate.name,'baked_candidate_name':baked.name,
                                  'bone_names':names,'frames':list(range(1,19)),
                                  'tolerance_m':.000001,'rotation_tolerance_deg':.001})
    result['negative_bake_change']=changed
    result['passed']=result['passed'] and not changed['passed'] and changed['max_angle_error_deg']>1
    bpy.ops.mesh.primitive_cube_add(size=1,location=(0,1,0))
    collider=bpy.context.object
    collider.name='M8_Chain_Closed_Collider'
    collision=call('pq_collision',{'candidate_name':candidate.name,'bone_names':names,
                                   'segment_radius_m':.02,'collider_names':[collider.name],'frames':[1,2,3]})
    result['negative_bone_penetration']=collision
    result['passed']=result['passed'] and not collision['passed'] and collision['penetration_vertex_frame_pairs']>0 and not collision['coverage']['all_evaluated_vertices'] and collision['coverage']['samples_per_bone_segment']==9
    return result


capture('bone_motion_bake_and_segment_collision',bone_motion_and_bake)


def reopen_portable():
    report=REPORT['checks']['portable_bake_equivalence']
    fixture=Path(report['fixture'])
    initial_hash=hashlib.sha256(fixture.read_bytes()).hexdigest()
    pc2=Path(report['pc2']['path'])
    pc2_hash=hashlib.sha256(pc2.read_bytes()).hexdigest()
    expected=json.loads((OUT/'samples_internal.json').read_text(encoding='utf-8'))['frames']
    bpy.ops.wm.open_mainfile(filepath=str(fixture),use_scripts=False)
    RNA_GUARDS.clear()
    playback=bpy.data.objects['M8_Cloth_Baked']
    native=bpy.data.objects['M8_Cloth_Candidate']
    native_cache=native.modifiers[native.get('production_m8_cloth_modifier')].point_cache
    native_reopen_state={'is_baked':native_cache.is_baked,'is_outdated':native_cache.is_outdated,
                         'is_frame_skip':native_cache.is_frame_skip,'info':native_cache.info}
    measured=[]
    for frame in list(range(1,19))+[18,9,1,18,9]:
        bpy.context.scene.frame_set(frame)
        actual=coords(playback)
        wanted=bytes.fromhex(expected[frame-1]['hex'])
        measured.append({'frame':frame,'exact_bytes_equal':actual==wanted,'sha256':hashlib.sha256(actual).hexdigest()})
    bpy.context.scene.frame_set(7)
    return {'passed':all(r['exact_bytes_equal'] for r in measured),
            'samples':measured,'fixture_sha256_unchanged':hashlib.sha256(fixture.read_bytes()).hexdigest()==initial_hash,
            'pc2_sha256_unchanged':hashlib.sha256(pc2.read_bytes()).hexdigest()==pc2_hash,
            'native_memory_cache_state_immediately_after_reopen':native_reopen_state,
            'expected_reference':'before_close_guarded_native_samples','native_memory_cache_not_required':True}


capture('portable_reopen_full_and_unordered_frames',reopen_portable)
scene=bpy.context.scene
source=bpy.data.objects['M8_Cloth_Source']
scene.frame_set(7)
REPORT['primary_source_geometry_unchanged']=hashlib.sha256(coords(source)).hexdigest()==source_hash
REPORT['all_required_passed']=not REPORT['errors'] and REPORT['primary_source_geometry_unchanged'] and all(r.get('passed') for r in REPORT['checks'].values())
(OUT/'report.json').write_text(json.dumps(REPORT,indent=2),encoding='utf-8')
print('M8_CLOTH_REPORT',str(OUT/'report.json'),REPORT['all_required_passed'],flush=True)
