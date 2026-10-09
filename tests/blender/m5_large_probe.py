"""ONE 2M-triangle generated fixture. Timings/payloads, not artistic qualification.

No socket, production assets or repeated giant fixtures. Native UV layout is
measured honestly, including any capacity/readiness rejection. Maximum wall time
is bounded by the host runner, which only owns this Blender child.
"""
import argparse,ctypes,json,subprocess,sys,tempfile,threading,time,traceback,types
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[2]

def inside(output,existing_source=None,repair_native_seam=False,reconstruct_indexed=False,existing_object='M5_SINGLE_LARGE_HIGH'):
    import bpy
    sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests/blender'))
    package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src/blender_mcp')];sys.modules['blender_mcp']=package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common import m5
    from blender_mcp.production.retopology import service as retopo
    from blender_mcp.production.uv import service as uv
    from blender_mcp.production.bake import service as bake
    from m31_fixture_support import register_open_fixture
    class LocalBridge:
        def __init__(self):self.sizes=[];self.responses=[]
        def run(self,body,params=None):
            code=build_code(body,params);self.sizes.append(len(code.encode()));ns={}
            exec(code.rsplit('print(',1)[0],ns);result=ns['result']
            self.responses.append(len(json.dumps(result,default=str).encode()));return result
    bridge=LocalBridge();storage=Storage(output/'s')
    report={'blender':bpy.app.version_string,'checks':{},'measurements':{},'fixture':{},'limitations':[]}
    def save():(output/'report.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf8')
    def measured(name,fn):
        start=time.perf_counter();pc,rc=len(bridge.sizes),len(bridge.responses)
        result=fn();report['measurements'][name]={'duration_s':round(time.perf_counter()-start,4),
            'max_request_bytes':max(bridge.sizes[pc:],default=0),'max_response_bytes':max(bridge.responses[rc:],default=0),
            'report':result};save();print('MEASURED',name,report['measurements'][name]['duration_s'],flush=True);return result
    def check(name,passed,**details):
        report['checks'][name]={'passed':bool(passed),**details};save();print('CHECK',name,'PASS' if passed else 'FAIL',flush=True)
        if not passed:raise RuntimeError('M5_LARGE_GATE_FAILED: '+name)
    def uid():return uuid4().hex
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # Installed operator RNA is queried rather than assuming it accepts this workload.
    rna=bpy.ops.mesh.primitive_uv_sphere_add.get_rna_type()
    if rna.properties['segments'].hard_max<1024 or rna.properties['ring_count'].hard_max<1024:
        raise RuntimeError('INSTALLED_SPHERE_CAPACITY_UNSUPPORTED')
    start=time.perf_counter()
    if existing_source:
        source_path=Path(existing_source).resolve(strict=True)
        allowed=(Path(tempfile.gettempdir())/'bpm5').resolve()
        if not source_path.is_relative_to(allowed):raise ValueError('ONLY_OWN_GENERATED_LARGE_FIXTURE_ALLOWED')
        bpy.ops.wm.open_mainfile(filepath=str(source_path),use_scripts=False,load_ui=False)
        original=bpy.data.objects[existing_object];original.hide_render=True
        high=original.copy();high.data=original.data.copy();high.name='M5_SINGLE_LARGE_HIGH_CONSTRUCTED'
        bpy.context.scene.collection.objects.link(high)
        if reconstruct_indexed:
            # LAST fixture-construction correction, not a production repair tool.
            # Reuse the one workload; preserve its faulty native object/file. Explicit
            # indices close the seam without tolerance-based welding or another primitive.
            from array import array
            from math import sin,cos,pi
            segments=rings=1024
            coordinates=array('f');indices=array('i');starts=array('i');totals=array('i');uvs=array('f')
            longitude=[(cos(2*pi*c/segments),sin(2*pi*c/segments)) for c in range(segments)]
            for row in range(1,rings):
                radius=sin(pi*row/rings);z=cos(pi*row/rings)
                for x,y in longitude:coordinates.extend((radius*x,radius*y,z))
            north=segments*(rings-1);south=north+1
            coordinates.extend((0,0,1,0,0,-1))
            def face(vertices,texcoords):
                starts.append(len(indices));totals.append(len(vertices));indices.extend(vertices)
                for coord in texcoords:uvs.extend(coord)
            for col in range(segments):
                nxt=(col+1)%segments;u=col/segments;un=(col+1)/segments
                face((north,col,nxt),(((u+un)/2,1),(u,1-1/rings),(un,1-1/rings)))
            for row in range(1,rings-1):
                prev=(row-1)*segments;curr=row*segments;vp=1-row/rings;vc=1-(row+1)/rings
                for col in range(segments):
                    nxt=(col+1)%segments;u=col/segments;un=(col+1)/segments
                    face((prev+col,curr+col,curr+nxt,prev+nxt),((u,vp),(u,vc),(un,vc),(un,vp)))
            last=(rings-2)*segments
            for col in range(segments):
                nxt=(col+1)%segments;u=col/segments;un=(col+1)/segments
                face((south,last+nxt,last+col),(((u+un)/2,0),(un,1/rings),(u,1/rings)))
            mesh=bpy.data.meshes.new('M5_SINGLE_LARGE_INDEXED_FIXTURE')
            mesh.vertices.add(north+2);mesh.vertices.foreach_set('co',coordinates)
            mesh.loops.add(len(indices));mesh.loops.foreach_set('vertex_index',indices)
            mesh.polygons.add(len(starts));mesh.polygons.foreach_set('loop_start',starts);mesh.polygons.foreach_set('loop_total',totals)
            if 'calc_edges' not in mesh.bl_rna.functions['update'].parameters:raise RuntimeError('MESH_UPDATE_EDGE_API_UNSUPPORTED')
            mesh.update(calc_edges=True)
            layer=mesh.uv_layers.new(name='UVMap');layer.data.foreach_set('uv',uvs)
            for material in original.data.materials:mesh.materials.append(material)
            old_owned=high.data;high.data=mesh;bpy.data.meshes.remove(old_owned)
            group=high.vertex_groups.get('ProtectedPole') or high.vertex_groups.new(name='ProtectedPole')
            group.add([v.index for v in mesh.vertices if v.co.z>.9995],1,'REPLACE')
            for polygon in mesh.polygons:polygon.use_smooth=True
            del coordinates,indices,starts,totals,uvs
            report['fixture_construction']={'method':'explicit_index_connectivity_of_same_single_spherical_workload',
                'corrective_iteration':2,'segments':segments,'rings':rings,'original_file':str(source_path),
                'original_object_preserved':original.name,'new_native_primitive_generated':False,
                'reason':'Native fixture had 1308 boundary edges; seam welding removed no vertices.'}
        if repair_native_seam:
            # Controlled fixture construction, not an MCP repair capability. Preserve
            # the faulty native source object and file. Weld only verified seam duplicates.
            import bmesh
            bm=bmesh.new();bm.from_mesh(high.data)
            try:
                candidates=[v for v in bm.verts if abs(v.co.x)<2e-6 and v.co.y>.48]
                before=len(bm.verts);bmesh.ops.remove_doubles(bm,verts=candidates,dist=3e-6)
                bm.to_mesh(high.data);high.data.update()
                report['fixture_construction']={'method':'copy_native_fixture_and_weld_verified_longitudinal_duplicates',
                    'weld_distance_m':3e-6,'seam_candidates':len(candidates),'vertices_removed':before-len(bm.verts),
                    'original_file':str(source_path),'original_object_preserved':original.name,'new_native_primitive_generated':False}
            finally:bm.free()
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=1024,ring_count=1024,radius=1)
        high=bpy.context.object;high.name='M5_SINGLE_LARGE_HIGH'
        for polygon in high.data.polygons:polygon.use_smooth=True
        high.data.materials.append(bpy.data.materials.new('LargeFixtureSurface'))
        group=high.vertex_groups.new(name='ProtectedPole')
        ids=[v.index for v in high.data.vertices if v.co.z>.9995];group.add(ids,1,'REPLACE')
    high.data.update();high.data.calc_loop_triangles()
    path=output/'single-large-source.blend';bpy.ops.wm.save_as_mainfile(filepath=str(path))
    register_open_fixture(bridge,storage,output)
    report['fixture']={'source_path':str(path),'source_object':high.name,'reused_source':existing_source,
        'reused_object':existing_object if existing_source else None,'source_triangles':len(high.data.loop_triangles),
        'source_vertices':len(high.data.vertices),'creation_s':round(time.perf_counter()-start,4),'generated_once':True}
    check('one_large_source_2_to_3m',2_000_000<=len(high.data.loop_triangles)<=3_000_000)
    regions=[{'name':'ProtectedPole','definition':{'method':'vertex_group','name':'ProtectedPole'},'protected':True,'priority':8}]
    options={'regions':regions,'strategy':'GUIDED_DECIMATION','target_triangles':125000,
        'minimum_triangles':100000,'maximum_triangles':150000,'minimum_triangle_quality':.0001,
        'samples':256,'silhouette_resolution':32,'maximum_silhouette_distance_m':.07,
        'minimum_silhouette_iou':.95,'max_distance_m':.005,'max_normal_angle_degrees':60}
    source=m5.signature(bridge,high.name)
    audited=measured('analysis',lambda:retopo.analyze(bridge,high.name,options))
    check('large_input_closed_and_valid',audited['passed'],errors=audited.get('errors'))
    plan=measured('retopology_planning',lambda:retopo.analyze(bridge,high.name,options,'plan'))
    check('large_explicit_plan',plan['supported'])
    candidate=measured('candidate_creation',lambda:retopo.create_candidate(bridge,storage,uid(),high.name,source['fingerprint'],{'regions':regions}))
    proposed=retopo.correction(bridge,storage,uid(),candidate['relationship'],'propose',options)
    check('large_reduction_proposal',proposed['proposal']['supported'])
    reduced=measured('candidate_generation',lambda:retopo.correction(bridge,storage,uid(),mode='apply',operation=proposed['operation']))
    check('game_mesh_100k_to_150k',reduced['numeric_verification']=='pass' and 100000<=reduced['after']['triangles']<=150000,errors=reduced.get('errors'))
    rel=reduced['relationship']
    proposed_projection=retopo.correction(bridge,storage,uid(),rel,'propose',options,kind='project')
    projected=measured('projection',lambda:retopo.correction(bridge,storage,uid(),mode='apply',operation=proposed_projection['operation'],kind='project'))
    check('large_projection_verified',projected['numeric_verification']=='pass',errors=projected.get('errors'))
    rel=projected['relationship'];low=bpy.data.objects[projected['candidate']]
    compared=measured('surface_comparison',lambda:retopo.compare(bridge,storage,rel,options))
    check('large_surface_fidelity',compared['passed'])
    uv_report=measured('UV_validation',lambda:uv.analyze(bridge,storage,rel,{'check_padding':False}))
    bake_report=measured('bake_readiness',lambda:bake.read(bridge,storage,rel,{'check_padding':False,'samples':128}))
    if not uv_report['valid']:report['limitations'].append({'large_native_UV_layout':uv_report['errors'],'status':'NOT_CERTIFIED'})
    if not bake_report['passed']:report['limitations'].append({'large_bake_readiness':bake_report['errors'],'status':bake_report['readiness']})
    check('large_source_intact',m5.signature(bridge,high.name)['fingerprint']==source['fingerprint'])
    report['fixture'].update(low_object=low.name,low_triangles=projected['after']['triangles'],
        reduction_percent=100*(1-projected['after']['triangles']/report['fixture']['source_triangles']))
    final=output/'single-large-final.blend';bpy.ops.wm.save_as_mainfile(filepath=str(final));report['fixture']['final_path']=str(final)
    report['passed']=all(c['passed'] for c in report['checks'].values());save()

class MemoryCounters(ctypes.Structure):
    _fields_=[('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong),('PeakWorkingSetSize',ctypes.c_size_t),
        ('WorkingSetSize',ctypes.c_size_t),('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
        ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--inside',action='store_true');parser.add_argument('--output');parser.add_argument('--existing-source');parser.add_argument('--existing-object',default='M5_SINGLE_LARGE_HIGH');parser.add_argument('--repair-native-seam',action='store_true');parser.add_argument('--reconstruct-indexed',action='store_true');parser.add_argument('--blender',default=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe')
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else None)
    output=Path(args.output) if args.output else Path(tempfile.gettempdir())/'bpm5'/('large-'+uuid4().hex[:8]);output.mkdir(parents=True,exist_ok=True)
    if args.inside:
        try:inside(output,args.existing_source,args.repair_native_seam,args.reconstruct_indexed,args.existing_object)
        except Exception:(output/'error.txt').write_text(traceback.format_exc(),encoding='utf8');raise
        return
    peak=0;timeout=False;start=time.perf_counter()
    with (output/'blender.log').open('w',encoding='utf8') as log:
        arguments=[args.blender,'--background','--factory-startup','--disable-autoexec','--python',str(Path(__file__).resolve()),'--','--inside','--output',str(output)]
        if args.existing_source:arguments+=['--existing-source',args.existing_source,'--existing-object',args.existing_object]
        if args.repair_native_seam:arguments.append('--repair-native-seam')
        if args.reconstruct_indexed:arguments.append('--reconstruct-indexed')
        process=subprocess.Popen(arguments,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        handle=ctypes.windll.kernel32.OpenProcess(0x410,False,process.pid) if sys.platform=='win32' else None
        try:
            while process.poll() is None:
                if handle:
                    counters=MemoryCounters();counters.cb=ctypes.sizeof(counters)
                    if ctypes.windll.psapi.GetProcessMemoryInfo(handle,ctypes.byref(counters),counters.cb):peak=max(peak,counters.PeakWorkingSetSize)
                if time.perf_counter()-start>1800:
                    timeout=True;process.terminate();break
                time.sleep(2)
            process.wait(timeout=30)
        finally:
            if handle:ctypes.windll.kernel32.CloseHandle(handle)
    report=json.loads((output/'report.json').read_text()) if (output/'report.json').exists() else {}
    report.update(output_dir=str(output),host_peak_working_set_bytes=peak or None,wall_time_s=round(time.perf_counter()-start,4),host_timeout=timeout)
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    promoted=ROOT/'test-artifacts'/'m5';promoted.mkdir(exist_ok=True)
    (promoted/(output.name+'.json')).write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps({'output':str(output),'passed':report.get('passed',False),'checks':len(report.get('checks',{})),'process_exit':process.returncode,'peak_bytes':peak}))
    if not report.get('passed'):
        print((output/'error.txt').read_text() if (output/'error.txt').exists() else 'No report');sys.exit(1)

if __name__=='__main__':main()
