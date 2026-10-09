"""UV/UDIM/bake gates in an isolated headless Blender, generated fixtures only."""
import argparse,json,math,subprocess,sys,tempfile,time,traceback,types
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]


def inside(output):
    import bpy
    sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests/blender'))
    package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')];sys.modules['blender_mcp']=package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common.m5 import create_relationship,signature,M5_RUNTIME
    from blender_mcp.production.common.asset_safety import register_master
    from blender_mcp.production.uv import service as uv
    from blender_mcp.production.bake import service as bake
    from blender_mcp.production.bake.runtime import BAKE_RUNTIME
    from m31_fixture_support import register_open_fixture
    class LocalBridge:
        def __init__(self):self.sizes=[]
        def run(self,body,params=None):
            code=build_code(body,params);self.sizes.append(len(code.encode()))
            namespace={};exec(code.rsplit('print(',1)[0],namespace);return namespace['result']
    bridge=LocalBridge();storage=Storage(output/'s')
    report={'blender':bpy.app.version_string,'checks':{},'timings_s':{},'demo':{},'artifacts':[]}
    def save():
        (output/'report.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf8')
    def check(name,passed,**details):
        report['checks'][name]={'passed':bool(passed),**details};save();print('CHECK',name,'PASS' if passed else 'FAIL',flush=True)
    def timed(name,function):
        start=time.perf_counter();data=function();report['timings_s'][name]=round(time.perf_counter()-start,4);return data
    def refused(name,function,code):
        try:
            result=function();text=json.dumps(result,default=str);passed=code in text and (result.get('status')=='error' or not result.get('proposal',{}).get('supported',True))
        except Exception as exc:text=str(exc);passed=code in text
        check(name,passed,error=text[:500])
    def uid():return uuid4().hex
    def relation(high,low):return create_relationship(bridge,storage,uid(),high.name,signature(bridge,high.name)['fingerprint'],{'target_object':low.name})
    bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_cube_add(size=2);high=bpy.context.object;high.name='M5_Bake_HIGH'
    for v in high.data.vertices:v.co*=1.02
    for layer in list(high.data.uv_layers):high.data.uv_layers.remove(layer)
    high.data.update()
    low=high.copy();low.data=high.data.copy();low.name='M5_Bake_LOW_BASE';bpy.context.scene.collection.objects.link(low)
    for v in low.data.vertices:v.co/=1.02
    for i in range(6):
        material=bpy.data.materials.new('Semantic_'+str(i));low.data.materials.append(material)
    for p in low.data.polygons:p.material_index=p.index
    low.data.update()
    bpy.ops.wm.save_as_mainfile(filepath=str(output/'uv-bake-fixture.blend'))
    register_open_fixture(bridge,storage,output)
    created=relation(high,low);rel=created['relationship'];candidate=created['candidate']
    original=signature(bridge,candidate)
    check('explicit_high_low_candidate',created['source_intact'] and candidate!=low.name)
    missing=uv.analyze(bridge,storage,rel)
    check('missing_uv_never_valid',not missing['valid'] and 'UV_MISSING' in missing['errors'])
    proposed=uv.mutate(bridge,storage,uid(),rel,'propose',{'padding_px':4,'resolution':256},kind='generate')
    check('seam_plan_explicit',proposed['proposal']['supported'] and len(proposed['proposal']['seam_edges'])==12,seam_evidence=proposed['proposal']['seam_evidence'])
    preview=timed('uv_preview',lambda:uv.mutate(bridge,storage,uid(),mode='preview',operation=proposed['operation'],kind='generate'))
    check('uv_preview_new_candidate_preserves_low',preview['numeric_verification']=='pass' and signature(bridge,candidate)['fingerprint']==original['fingerprint'],metrics=preview['metrics'])
    applied=timed('uv_apply',lambda:uv.mutate(bridge,storage,uid(),mode='apply',operation=proposed['operation'],kind='generate'))
    check('uv_apply_generated_and_preserved',applied['numeric_verification']=='pass' and applied['source_intact'],errors=applied.get('errors'))
    verified=uv.mutate(bridge,storage,uid(),mode='verify',operation=proposed['operation'],kind='generate')
    check('uv_verify_remeasures',verified['numeric_verification']=='pass')
    rel=applied['relationship'];uv_object=applied['candidate'];settings={'padding_px':4,'resolution':256}
    audit=timed('uv_analysis',lambda:uv.analyze(bridge,storage,rel,settings))
    check('uv_metrics_positive',audit['valid'] and audit['metrics']['islands']==6 and audit['metrics']['overlap_pairs_reported']==0 and audit['metrics']['world_area_m2']>23.9,metrics=audit['metrics'])
    smartp=uv.mutate(bridge,storage,uid(),rel,'propose',dict(settings,unwrap_method='SMART_PROJECT'),kind='generate')
    smarta=uv.mutate(bridge,storage,uid(),mode='preview',operation=smartp['operation'],kind='generate')
    check('explicit_smart_project_no_fallback',smartp['proposal']['unwrap_method']=='SMART_PROJECT' and smarta['numeric_verification']=='pass' and smarta['source_intact'])
    assignments=[{'region':'region_'+str(i),'tile':1001+i//2,'material_index':i} for i in range(6)]
    options=dict(settings,assignments=assignments)
    plan=uv.plan_udim(bridge,storage,rel,options)
    check('custom_three_tile_plan',plan['tiles']==[1001,1002,1003] and not plan['apply'])
    udimp=uv.mutate(bridge,storage,uid(),rel,'propose',options,kind='udim')
    udima=timed('udim_apply',lambda:uv.mutate(bridge,storage,uid(),mode='apply',operation=udimp['operation'],kind='udim'))
    check('udim_apply_verified',udima['numeric_verification']=='pass',metrics=udima['metrics'],errors=udima.get('errors'))
    rel=udima['relationship'];target=udima['candidate']
    ua=uv.analyze(bridge,storage,rel,dict(settings,expected_tiles=[1001,1002,1003]))
    check('udim_tiles_no_cross_overlap',ua['valid'] and [row['tile'] for row in ua['tiles']]==[1001,1002,1003],tiles=ua['tiles'])
    check('world_texel_density_measured',ua['metrics']['texels_per_meter']['median']>0 and len(ua['regions'])==6)
    packp=uv.mutate(bridge,storage,uid(),rel,'propose',settings,kind='pack')
    packa=uv.mutate(bridge,storage,uid(),mode='apply',operation=packp['operation'],kind='pack')
    check('packing_respects_existing_tiles',packa['numeric_verification']=='pass' and packa['metrics']['tiles']==[1001,1002,1003])
    refused('invalid_udim_rejected',lambda:uv.plan_udim(bridge,storage,rel,{'assignments':[{'region':'x','tile':999,'material_index':0}]}),'UDIM_TILE_INVALID')
    refused('incomplete_udim_rejected',lambda:uv.plan_udim(bridge,storage,rel,{'assignments':[assignments[0]]}),'UDIM_INCOMPLETE_ASSIGNMENT')
    refused('locked_uv_generation_rejected',lambda:uv.mutate(bridge,storage,uid(),rel,'propose',dict(settings,uv_immutable=True),kind='generate'),'LOCKED_UV_IMMUTABLE')
    # Independent defect injections on copied candidates; historical candidate and HIGH stay untouched.
    obj=bpy.data.objects[target]
    defect=obj.copy();defect.data=obj.data.copy();defect.name='UV_Overlap_Defect';bpy.context.scene.collection.objects.link(defect)
    layer=defect.data.uv_layers.active
    for p in defect.data.polygons:
        for c in p.loop_indices:
            old=layer.uv[c].vector;layer.uv[c].vector=(old.x%1,old.y%1)
    dr=relation(high,defect)['relationship'];bad=uv.analyze(bridge,storage,dr,dict(settings,check_padding=False))
    check('uv_overlap_rejected',not bad['valid'] and 'UV_OVERLAP' in bad['errors'],metrics=bad['metrics'])
    stretch=obj.copy();stretch.data=obj.data.copy();stretch.name='UV_Stretch_Defect';bpy.context.scene.collection.objects.link(stretch)
    for loop in stretch.data.uv_layers.active.uv:loop.vector.y*=.001
    sr=relation(high,stretch)['relationship'];st=uv.analyze(bridge,storage,sr,dict(settings,check_padding=False))
    check('uv_stretch_rejected',not st['valid'] and 'UV_STRETCH_EXCESSIVE' in st['errors'],stretch=st['metrics']['stretch_ratio'])
    options=dict(settings,front_distance_m=.09,rear_distance_m=.09,extrusion_m=.08,max_cage_offset_m=.1)
    # A narrowly supported bound mesh must independently prove evaluated geometry equals its base.
    rig_data=bpy.data.armatures.new('M5_BindRigData');rig=bpy.data.objects.new('M5_BindRig',rig_data);bpy.context.scene.collection.objects.link(rig)
    for item in bpy.context.selected_objects:item.select_set(False)
    rig.select_set(True);bpy.context.view_layer.objects.active=rig;bpy.ops.object.mode_set(mode='EDIT')
    bone=rig_data.edit_bones.new('BindRoot');bone.head=(0,0,0);bone.tail=(0,0,1);bpy.ops.object.mode_set(mode='OBJECT')
    target_obj=bpy.data.objects[target];group=target_obj.vertex_groups.new(name='BindRoot');group.add(list(range(len(target_obj.data.vertices))),1.0,'REPLACE')
    mod=target_obj.modifiers.new('Bind','ARMATURE');mod.object=rig;bpy.context.view_layer.update()
    bound=bake.read(bridge,storage,rel,dict(options,geometry_adapter='BOUND_REST_MESH'))
    check('bound_rest_mesh_geometry_verified',bound['passed'] and bound['geometry_adapter'][target]['evaluated_max_delta_m']<1e-7)
    static=bake.read(bridge,storage,rel,options)
    check('rig_requires_explicit_adapter',not static['passed'] and any('BAKE_EVALUATED_GEOMETRY_UNSUPPORTED' in e for e in static['errors']))
    rig.pose.bones['BindRoot'].rotation_mode='XYZ';rig.pose.bones['BindRoot'].rotation_euler.x=.2;bpy.context.view_layer.update()
    moved=bake.read(bridge,storage,rel,dict(options,geometry_adapter='BOUND_REST_MESH'))
    check('deformed_bound_mesh_refused',not moved['passed'] and any('BAKE_DEFORMED_REST_MESH' in e for e in moved['errors']))
    rig.pose.bones['BindRoot'].rotation_euler.x=0;target_obj.modifiers.remove(mod);target_obj.vertex_groups.remove(group);bpy.data.objects.remove(rig,do_unlink=True);bpy.context.view_layer.update()
    rays=timed('ray_analysis',lambda:bake.read(bridge,storage,rel,options,'rays'))
    check('ray_distance_signed_measured',rays['ray_misses']==0 and .019<rays['front_deviation_m']['maximum']<.021,rays=rays)
    cp=bake.cage(bridge,storage,uid(),rel,'propose',options)
    check('cage_plan_supported',cp['proposal']['supported'])
    ca=timed('cage_apply',lambda:bake.cage(bridge,storage,uid(),mode='apply',operation=cp['operation']))
    check('cage_safe_new_child',ca['numeric_verification']=='pass' and ca['relationship']==rel and ca['source_intact'],metrics=ca['metrics'],errors=ca.get('errors'))
    cage_name=ca['candidate'];options['cage_object']=cage_name
    readiness=timed('bake_readiness',lambda:bake.read(bridge,storage,rel,options))
    check('bake_ready_real_tangents',readiness['passed'] and readiness['tangents']['valid'],readiness=readiness['readiness'],errors=readiness['errors'])
    refused('cage_too_close_rejected',lambda:bake.cage(bridge,storage,uid(),rel,'propose',dict(options,extrusion_m=.001)),'CAGE_TOO_CLOSE')
    refused('cage_too_far_rejected',lambda:bake.cage(bridge,storage,uid(),rel,'propose',dict(options,extrusion_m=.2)),'CAGE_TOO_FAR')
    miss=bake.read(bridge,storage,rel,dict(options,front_distance_m=.001,rear_distance_m=.001))
    check('ray_miss_not_ready',not miss['passed'] and miss['readiness']=='NOT_READY' and 'BAKE_RAY_MISSES' in miss['errors'])
    bad_ready=bake.read(bridge,storage,dr,{k:v for k,v in dict(options,check_padding=False).items() if k!='cage_object'})
    check('uv_overlap_never_ready',bad_ready['readiness']=='NOT_READY')
    pair=bake.prepare_pairs(bridge,storage,uid(),rel,options)
    check('explicit_pair_frozen',pair['readiness'] in ('READY','READY_WITH_WARNINGS') and Path(pair['pair']).exists())
    refused('frozen_ray_parameters_cannot_override',lambda:bake.smoke(bridge,storage,uid(),pair['pair'],True,{'front_distance_m':.001}),'FROZEN_BAKE_PAIR')
    # Factory startup config has CPU selected: CPU must be rejected before checkpoint/bake.
    refused('cpu_without_approval_rejected',lambda:bake.smoke(bridge,storage,uid(),pair['pair']),'RENDER_GPU_REQUIRED')
    smoke=timed('smoke_NORMAL_AO_three_tiles',lambda:bake.smoke(bridge,storage,uid(),pair['pair'],True,{'smoke_resolution':64,'bake_samples':2}))
    check('smoke_normal_and_ao',smoke['passed'] and len(smoke['artifacts'])==6,artifacts=smoke['artifacts'],backend=smoke['render_backend'])
    check('smoke_restores_full_source_state',smoke['source_intact'] and smoke['state_restored'])
    check('smoke_outputs_exist',all(Path(a['path']).exists() and Path(a['path']).stat().st_size>64 for a in smoke['artifacts']))
    report['artifacts']=smoke['artifacts'];report['demo']={'uv':ua,'rays':rays,'cage':ca,'readiness':readiness,'smoke':smoke,'relationship':rel,'fixture':str(output/'uv-bake-fixture.blend')}
    stale=bpy.data.objects[target].data.uv_layers.active.uv[0].vector.copy();bpy.data.objects[target].data.uv_layers.active.uv[0].vector.x+=.01
    refused('stale_bake_pair_rejected',lambda:bake.load_pair(bridge,storage,pair['pair']),'STALE_BAKE_PAIR')
    bpy.data.objects[target].data.uv_layers.active.uv[0].vector=stale
    # Nonconstant high surface independently demonstrates that NORMAL transports a real signal.
    vertices=[];faces=[];n=24
    for y in range(n+1):
        for x in range(n+1):
            px,py=-1+2*x/n,-1+2*y/n
            vertices.append((px,py,.12*math.sin(math.pi*(px+1)/2)*math.sin(math.pi*(py+1)/2)))
    for y in range(n):
        for x in range(n):
            i=y*(n+1)+x;faces.append((i,i+1,i+n+2,i+n+1))
    mesh=bpy.data.meshes.new('WaveHighMesh');mesh.from_pydata(vertices,[],faces);mesh.update();wave=bpy.data.objects.new('M5_Wave_HIGH',mesh);bpy.context.scene.collection.objects.link(wave)
    lowmesh=bpy.data.meshes.new('WaveLowMesh');lowmesh.from_pydata([(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0)],[],[(0,1,2,3)]);lowmesh.update()
    lowwave=bpy.data.objects.new('M5_Wave_LOW',lowmesh);bpy.context.scene.collection.objects.link(lowwave);lowmesh.materials.append(bpy.data.materials.new('WaveSemantic'))
    wr=relation(wave,lowwave)['relationship'];wp=uv.mutate(bridge,storage,uid(),wr,'propose',settings,kind='generate');wa=uv.mutate(bridge,storage,uid(),mode='apply',operation=wp['operation'],kind='generate');wr=wa['relationship']
    wopts=dict(settings,allow_open_surface=True,front_distance_m=.2,rear_distance_m=.2,max_ray_distance_m=.5,extrusion_m=.18,max_cage_offset_m=.2,max_surface_deviation_m=.2)
    wcp=bake.cage(bridge,storage,uid(),wr,'propose',wopts);wca=bake.cage(bridge,storage,uid(),mode='apply',operation=wcp['operation']);wopts['cage_object']=wca['candidate']
    wpair=bake.prepare_pairs(bridge,storage,uid(),wr,wopts);wsmoke=timed('wave_normal_smoke',lambda:bake.smoke(bridge,storage,uid(),wpair['pair'],True,{'maps':['NORMAL'],'smoke_resolution':64,'bake_samples':2}))
    channels=wsmoke['artifacts'][0]['metrics']['rgb_distribution']
    check('normal_smoke_transfers_nonconstant_high_signal',wsmoke['passed'] and channels[0]['maximum']-channels[0]['minimum']>.08 and channels[1]['maximum']-channels[1]['minimum']>.08,channels=channels)
    check('wave_cage_and_smoke_sources_preserved',wca['numeric_verification']=='pass' and wsmoke['source_intact'])
    report['wave_demo']={'relationship':wr,'pair':wpair,'cage':wca,'smoke':wsmoke,'high_triangles':len(mesh.polygons)*2,'low_triangles':2}
    report['artifacts']+=wsmoke['artifacts']
    # Finite planar HIGH: a tangentially displaced cage must aim towards its LOW
    # counterpart. A LOW-face-normal witness demonstrably misses this patch.
    raymesh=bpy.data.meshes.new('ObliqueRayHighMesh');raymesh.from_pydata([(0,0,0),(.03,0,0),(0,.03,0)],[],[(0,1,2)]);raymesh.update()
    rayhigh=bpy.data.objects.new('M5_ObliqueRay_HIGH',raymesh);bpy.context.scene.collection.objects.link(rayhigh)
    raylow=rayhigh.copy();raylow.data=raymesh.copy();raylow.name='M5_ObliqueRay_LOW';bpy.context.scene.collection.objects.link(raylow)
    raycage=rayhigh.copy();raycage.data=raymesh.copy();raycage.name='M5_ObliqueRay_CAGE';bpy.context.scene.collection.objects.link(raycage)
    for v in raycage.data.vertices:v.co.x+=.03;v.co.z+=.005
    raycage.data.update()
    oblique=bridge.run(M5_RUNTIME+'\n'+BAKE_RUNTIME+'''\nhigh=m5_obj(params['high']);low=m5_obj(params['low']);cage=m5_obj(params['cage'])
lg=m5_geo(low);cg=m5_geo(cage);ids=lg['triangles'][0];xyz=[lg['points'][i] for i in ids]
center=sum((cg['points'][i] for i in ids),Vector())/3
normal=(xyz[1]-xyz[0]).cross(xyz[2]-xyz[0]).normalized()
result={'face_normal_miss':m5_bvh(high).ray_cast(center,-normal,.1)[0] is None,
    'report':bake_cage_report(high,low,cage,{'front_distance_m':.05,'rear_distance_m':.05})}''',{'high':rayhigh.name,'low':raylow.name,'cage':raycage.name})
    check('native_cage_correspondence_ray_hits_oblique_patch',oblique['face_normal_miss'] and oblique['report']['passed'],details=oblique)
    zero=bridge.run(M5_RUNTIME+'\n'+BAKE_RUNTIME+"\nresult=bake_cage_report(m5_obj(params['high']),m5_obj(params['low']),m5_obj(params['low']),{})",{'high':rayhigh.name,'low':raylow.name})
    check('coincident_cage_undefined_direction_rejected',not zero['passed'] and 'CAGE_RAY_DIRECTION_UNDEFINED' in zero['errors'] and zero['metrics']['undefined_ray_directions']==1,details=zero)
    bpy.ops.wm.save_as_mainfile(filepath=str(output/'uv-bake-demo-final.blend'))
    register_open_fixture(bridge,storage,output)
    masterdir=output/'master';masterdir.mkdir();bpy.ops.wm.save_as_mainfile(filepath=str(masterdir/'fixture-master.blend'))
    register_master(bridge,Storage(output/'master-store'))
    refused('outside_workspace_manifest_rejected',lambda:bake.cage(bridge,Storage(output/'master-store'),uid(),mode='apply',operation=cp['operation']),'MANIFEST_OUTSIDE_WORKSPACE')
    # Real MASTER role on the same operation store, without changing the generated file itself.
    from blender_mcp.production.common.asset_safety import AssetRegistry
    AssetRegistry(storage).register('MASTER',bpy.data.filepath)
    refused('master_uv_mutation_guard',lambda:uv.mutate(bridge,storage,uid(),mode='apply',operation=proposed['operation'],kind='generate'),'MASTER_ASSET_READ_ONLY')
    refused('master_cage_mutation_guard',lambda:bake.cage(bridge,storage,uid(),mode='apply',operation=cp['operation']),'MASTER_ASSET_READ_ONLY')
    report['payload_max_bytes']=max(bridge.sizes)
    report['passed']=all(r['passed'] for r in report['checks'].values());save()
    if not report['passed']:raise RuntimeError('M5 UV/UDIM/bake gate failure')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--inside',action='store_true');parser.add_argument('--output');parser.add_argument('--blender',default=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe');args=parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else None)
    output=Path(args.output) if args.output else ROOT/'test-artifacts'/('m5uv-'+uuid4().hex[:8]);output.mkdir(parents=True,exist_ok=True)
    if args.inside:
        try:inside(output)
        except Exception:
            (output/'error.txt').write_text(traceback.format_exc(),encoding='utf8');raise
        return
    with (output/'blender.log').open('w',encoding='utf8') as log:
        result=subprocess.run([args.blender,'--background','--factory-startup','--disable-autoexec','--python',str(Path(__file__).resolve()),'--','--inside','--output',str(output)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
    report=json.loads((output/'report.json').read_text()) if (output/'report.json').exists() else {}
    print(json.dumps({'output':str(output),'passed':report.get('passed',False),'checks':len(report.get('checks',{})),'process_exit':result.returncode}))
    if not report.get('passed'):print((output/'error.txt').read_text() if (output/'error.txt').exists() else 'No report');sys.exit(1)


if __name__=='__main__':main()
