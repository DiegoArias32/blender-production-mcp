"""Complete M5 humanoid/creature packages in isolated Blender; generated scenes only.

Domain probes exercise the defects and UV/UDIM demo. This probe joins every
required subsystem and renders comparable views. It never contacts a socket.
"""
import argparse,json,subprocess,sys,tempfile,time,traceback,types
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]

def inside(output):
    import bpy
    from mathutils import Vector
    sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests/blender'))
    package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')];sys.modules['blender_mcp']=package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage,write_json
    from blender_mcp.production.common import m5
    from blender_mcp.production.retopology import service as retopo
    from blender_mcp.production.transfer import service as transfer
    from blender_mcp.production.game_mesh import service as game
    from blender_mcp.production.game_mesh.validation import validate as validate_game
    from blender_mcp.production.game_mesh.package import prepare as package_game
    from blender_mcp.production.uv import service as uv
    from blender_mcp.production.bake import service as bake
    from blender_mcp.production.qa import render_preview
    from m31_fixture_support import register_open_fixture
    from m5_fixtures import build_character

    class LocalBridge:
        def __init__(self):self.sizes=[]
        def run(self,body,params=None):
            code=build_code(body,params);self.sizes.append(len(code.encode()))
            ns={};exec(code.rsplit('print(',1)[0],ns);return ns['result']
    bridge=LocalBridge();storage=Storage(output/'s')
    report={'blender':bpy.app.version_string,'checks':{},'demos':{},'timings_s':{},'artifacts':[],
        'limitations':['Synthetic proxy characters; no artistic production claim.','Sampled surface metrics and finite poses.']}
    def save():(output/'report.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf8')
    def check(name,passed,**details):
        report['checks'][name]={'passed':bool(passed),**details};save();print('CHECK',name,'PASS' if passed else 'FAIL',flush=True)
        if not passed:raise RuntimeError('M5_PIPELINE_GATE_FAILED: '+name)
    def timed(name,fn):
        start=time.perf_counter();data=fn();report['timings_s'][name]=round(time.perf_counter()-start,4);save();return data
    def uid():return uuid4().hex
    def cycle(label,fn,relationship,options):
        proposed=fn(relationship=relationship,mode='propose',options=options)
        check(label+'_supported',proposed['proposal']['supported'],proposal=proposed['proposal'])
        applied=timed(label,lambda:fn(mode='apply',operation=proposed['operation']))
        check(label+'_verified',applied['numeric_verification']=='pass' and applied['source_intact'],result=applied)
        return applied
    def ret_fn(kind):return lambda relationship=None,mode='analyze',options=None,operation=None:retopo.correction(bridge,storage,uid(),relationship,mode,options,operation,kind)
    def transfer_fn(kind):return lambda relationship=None,mode='analyze',options=None,operation=None:transfer.transfer(bridge,storage,uid(),kind,relationship,mode,options,operation)
    def game_fn(kind):return lambda relationship=None,mode='analyze',options=None,operation=None:game.prepare(bridge,storage,uid(),kind,relationship,mode,options,operation)
    def uv_fn(kind):return lambda relationship=None,mode='analyze',options=None,operation=None:uv.mutate(bridge,storage,uid(),relationship,mode,options,operation,kind)
    def cage_fn(relationship=None,mode='analyze',options=None,operation=None):return bake.cage(bridge,storage,uid(),relationship,mode,options,operation)
    stress=[{'region':label,'bone':bone,'axis':axis,'angle_degrees':degrees} for label,bone,axis,degrees in
        [('shoulder','upper_arm.L','Z',35),('elbow','forearm.L','Z',60),('knee','shin.L','X',55),
         ('shoulder','upper_arm.R','Z',-35),('elbow','forearm.R','Z',-60),('knee','shin.R','X',55)]]

    def capture(name,high,low):
        scene=bpy.context.scene;old_engine=scene.render.engine;old_camera=scene.camera
        engines={item.identifier for item in scene.render.bl_rna.properties['engine'].enum_items}
        scene.render.engine=next(name for name in ('BLENDER_EEVEE','BLENDER_EEVEE_NEXT') if name in engines)
        old_world=scene.world;world=bpy.data.worlds.new('OwnedM5QAWorld');scene.world=world;world.color=(.16,.16,.16)
        camera_data=bpy.data.cameras.new('OwnedM5QACamera');camera=bpy.data.objects.new('OwnedM5QACamera',camera_data)
        scene.collection.objects.link(camera);camera_data.type='ORTHO';scene.camera=camera
        light_data=bpy.data.lights.new('OwnedM5QALight','AREA');light=bpy.data.objects.new('OwnedM5QALight',light_data)
        scene.collection.objects.link(light);light.location=(3,-4,5);light_data.energy=700;light_data.size=4
        vertices=[high.matrix_world@v.co for v in high.data.vertices]
        lo=Vector(tuple(min(p[k] for p in vertices) for k in range(3)));hi=Vector(tuple(max(p[k] for p in vertices) for k in range(3)))
        center=(lo+hi)/2;camera_data.ortho_scale=max(hi-lo)*1.15
        light.rotation_euler=(center-light.location).to_track_quat('-Z','Y').to_euler()
        states={obj:obj.hide_render for obj in scene.objects if obj.type=='MESH'}
        images=[]
        try:
            for view,direction in [('FRONT',(0,-1,0)),('SIDE',(1,0,0)),('THREE_QUARTER',(1,-1,0))]:
                camera.location=center+Vector(direction).normalized()*4
                camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler()
                for obj,label in ((high,'HIGH'),(low,'LOW')):
                    for mesh in states:mesh.hide_render=mesh is not obj
                    folder=storage.reserve('qa',uid())/'frames';folder.mkdir()
                    captured=render_preview(bridge,folder,width=320,height=320,max_frames=1)
                    check(name+'_'+label+'_'+view+'_render',captured['status']!='error' and captured['state_restored'],result=captured)
                    images.extend(item['path'] for item in captured['files'])
        finally:
            for obj,state in states.items():obj.hide_render=state
            scene.camera=old_camera;scene.render.engine=old_engine
            scene.world=old_world;bpy.data.worlds.remove(world)
            bpy.data.objects.remove(camera,do_unlink=True);bpy.data.cameras.remove(camera_data)
            bpy.data.objects.remove(light,do_unlink=True);bpy.data.lights.remove(light_data)
        return images

    for variant in ('humanoid','creature'):
        info=build_character(output/'fixtures',variant,levels=3);register_open_fixture(bridge,storage,output)
        high=bpy.data.objects[info['body']]
        color=high.data.color_attributes.new(name='SemanticTint',type='FLOAT_COLOR',domain='POINT')
        for vertex in high.data.vertices:color.data[vertex.index].color=(.5+.1*vertex.co.x,.3+.1*vertex.co.z,.4,1)
        source=m5.signature(bridge,high.name);opts=info['options'];prefix=variant
        audit=timed(prefix+'_analysis',lambda:retopo.analyze(bridge,high.name,opts))
        check(prefix+'_source_topology',audit['passed'],metrics=audit['metrics'])
        density=timed(prefix+'_density',lambda:retopo.analyze(bridge,high.name,opts,'density'))
        priority=retopo.analyze(bridge,high.name,opts,'priority');budget=retopo.analyze(bridge,high.name,opts,'budget')
        plan=timed(prefix+'_plan',lambda:retopo.analyze(bridge,high.name,opts,'plan'))
        check(prefix+'_plan',plan['supported'] and budget['supported'] and plan['strategy']=='GUIDED_DECIMATION')
        created=retopo.create_candidate(bridge,storage,uid(),high.name,source['fingerprint'],{'regions':info['regions']})
        previous=created['candidate'];rel=created['relationship']
        reduced=cycle(prefix+'_reduce',ret_fn('reduce'),rel,opts);rel=reduced['relationship']
        projected=cycle(prefix+'_project',ret_fn('project'),rel,opts);rel=projected['relationship']
        top=retopo.compare(bridge,storage,rel,opts,'validate');surface=retopo.compare(bridge,storage,rel,opts)
        silhouette=retopo.compare(bridge,storage,rel,opts,'silhouette')
        check(prefix+'_fidelity',top['passed'] and surface['passed'] and silhouette['passed'])
        weights={'max_distance_m':.005,'max_normal_angle_degrees':60,'stress_poses':stress,
                 'deformation_error_m':.005,'deformation_samples':2048,'influence_limit':8}
        weighted=cycle(prefix+'_weights',transfer_fn('weights'),rel,weights);rel=weighted['relationship']
        weight_report=transfer.validate_weights(bridge,storage,rel,weights,uid())
        check(prefix+'_weights_deformation',weight_report['passed'] and weight_report['deformation_qa']['state_restored'],report=weight_report)
        morph=transfer.analyze_morphs(bridge,storage,rel)
        check(prefix+'_morph_handled',morph['decision']=='NOT_APPLICABLE' and not morph['errors'])
        attributes={'max_distance_m':.005,'max_normal_angle_degrees':60,'attributes':['SemanticTint'],
            'materials':True,'custom_properties':{'object':['character_role'],'mesh':['lod']}}
        attributed=cycle(prefix+'_attributes',transfer_fn('attributes'),rel,attributes);rel=attributed['relationship']
        normals=cycle(prefix+'_normals',game_fn('normals'),rel,{'normal_policy':'preserve'});rel=normals['relationship']
        triangulated=cycle(prefix+'_triangulation',game_fn('triangulation'),rel,{'quad_method':'BEAUTY','ngon_method':'EAR_CLIP'});rel=triangulated['relationship']
        uv_opts={'uv_layer':'GameUV','resolution':512,'padding_px':2,'material_seams':True,'unwrap_method':'SMART_PROJECT',
                 'angle_seams':True,'seam_angle_degrees':50,'max_stretch_ratio':8}
        generated_uv=cycle(prefix+'_uv',uv_fn('generate'),rel,uv_opts);rel=generated_uv['relationship']
        uv_report=uv.analyze(bridge,storage,rel,uv_opts)
        check(prefix+'_UV_valid',uv_report['valid'],report=uv_report)
        tangents=game.validate(bridge,storage,rel,'tangents',{'uv_layer':'GameUV'})
        check(prefix+'_tangents_valid',tangents['passed'],report=tangents)
        bake_opts=dict(uv_opts,geometry_adapter='BOUND_REST_MESH',rest_geometry_tolerance_m=1e-6,
            front_distance_m=.015,rear_distance_m=.015,max_ray_distance_m=.03,
            max_surface_deviation_m=.005,
            extrusion_m=.003394284028923721 if variant=='creature' else .002,
            max_cage_offset_m=.025,samples=512)
        # Fixture-specific measured profile, not a universal cage distance. The
        # creature's two 2mm ray misses were diagnosed; its signed front maximum
        # 1.915427223mm *1.25 +1mm yields this conservative offset without collisions.
        cage=cycle(prefix+'_cage',cage_fn,rel,bake_opts)
        bake_opts['cage_object']=cage['candidate']
        pair=bake.prepare_pairs(bridge,storage,uid(),rel,bake_opts)
        check(prefix+'_bake_ready',pair['readiness']=='READY',report=pair)
        aggregate_opts={'retopology':dict(opts,minimum_triangles=int(opts['target_triangles']*.95),maximum_triangles=int(opts['target_triangles']*1.05)),
            'weights':weights,'attributes':attributes,'game_mesh':{'uv_layer':'GameUV','quad_method':'BEAUTY'},'uv':uv_opts,'bake':bake_opts}
        validated=timed(prefix+'_final_validation',lambda:validate_game(bridge,storage,rel,aggregate_opts,uid()))
        check(prefix+'_all_numeric_subsystems',validated['passed'],report=validated)
        low=bpy.data.objects[validated['signature']['object']]
        images=capture(prefix,high,low)
        package=package_game(bridge,storage,uid(),rel,pair['pair'],options=aggregate_opts,dry_run=False)
        check(prefix+'_package_manifest',Path(package['package']).is_file() and Path(package['handoff']).is_file(),package=package)
        check(prefix+'_source_and_prior_intact',m5.signature(bridge,high.name)['fingerprint']==source['fingerprint'] and bpy.data.objects.get(previous) is not None)
        if variant=='creature':
            protected=projected['metrics']['protected_regions']
            check('intentional_creature_asymmetry_preserved',all(row['max_vertex_displacement_m']<=row['tolerance_m'] for row in protected),protected=protected)
        final=output/'fixtures'/(variant+'_game_mesh.blend');bpy.ops.wm.save_as_mainfile(filepath=str(final))
        report['demos'][variant]={'source':high.name,'low':low.name,'fixture':str(final),'source_triangles':info['triangles'],
            'low_triangles':len(low.data.polygons),'reduction_percent':100*(1-len(low.data.polygons)/info['triangles']),
            'regions':info['regions'],'density':density,'priority':priority,'budget':budget,
            'topology':top,'surface':surface,'silhouette':silhouette,'weights':weight_report,'morphs':morph,
            'attributes':attributed['metrics'],'UV':uv_report,'tangents':tangents,'cage':cage,
            'bake_pair':pair,'validation':validated,'package':package,'images':images,
            'visual_verification':'required','review_note':'Images rendered; review must be recorded after actual inspection.'}
        report['artifacts'].extend(images);save()
    report['payload_max_bytes']=max(bridge.sizes);report['passed']=all(c['passed'] for c in report['checks'].values());save()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--inside',action='store_true');parser.add_argument('--output');parser.add_argument('--blender',default=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe')
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else None)
    output=Path(args.output) if args.output else Path(tempfile.gettempdir())/'bpm5'/('pipeline-'+uuid4().hex[:8]);output.mkdir(parents=True,exist_ok=True)
    if args.inside:
        try:inside(output)
        except Exception:
            (output/'error.txt').write_text(traceback.format_exc(),encoding='utf8');raise
        return
    with (output/'blender.log').open('w',encoding='utf8') as log:
        process=subprocess.run([args.blender,'--background','--factory-startup','--disable-autoexec','--python',str(Path(__file__).resolve()),'--','--inside','--output',str(output)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
    report=json.loads((output/'report.json').read_text()) if (output/'report.json').exists() else {}
    promoted=ROOT/'test-artifacts'/'m5';promoted.mkdir(exist_ok=True)
    (promoted/(output.name+'.json')).write_text(json.dumps(dict(report,output_dir=str(output)),indent=2),encoding='utf8')
    print(json.dumps({'output':str(output),'passed':report.get('passed',False),'checks':len(report.get('checks',{})),'process_exit':process.returncode}))
    if not report.get('passed'):
        print((output/'error.txt').read_text() if (output/'error.txt').exists() else 'No report');sys.exit(1)

if __name__=='__main__':main()
