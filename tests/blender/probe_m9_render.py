"""Run with owned Blender --background --factory-startup --disable-autoexec.

Validates safe-mode payloads, real EEVEE shader images and complete restoration.
This is a direct Blender probe, not a replacement for final MCP transport tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback
import time
import types

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(Path(__file__).parent))
# Blender's Python intentionally has no host MCP SDK installed. Expose package
# paths without executing the upstream host package __init__ in this test only.
package=types.ModuleType('blender_mcp');package.__path__=[str(ROOT/'src'/'blender_mcp')]
sys.modules['blender_mcp']=package

from fixtures_m9 import build
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.storage import Storage,sha256
from blender_mcp.production.materials import render


class LocalBridge:
    def run(self,body,params):
        namespace={}
        import contextlib,io
        with contextlib.redirect_stdout(io.StringIO()):exec(build_code(body,params),namespace)
        return namespace['result']


def snapshot():
    import bpy
    def curves(action):
        rows=[]
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for curve in bag.fcurves:
                        rows.append([curve.data_path,curve.array_index,[[list(p.co),list(p.handle_left),list(p.handle_right),p.interpolation] for p in curve.keyframe_points]])
        return rows
    scene=bpy.context.scene
    return {'filepath':bpy.data.filepath,'scene':scene.name,'frame':scene.frame_current,'subframe':scene.frame_subframe,
            'active':bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
            'selected':sorted(o.name for o in bpy.context.selected_objects),
            'actions':{a.name:curves(a) for a in bpy.data.actions},
            'objects':{o.name:{'location':list(o.location),'rotation':list(o.rotation_euler),'scale':list(o.scale),
                        'matrix':[[float(v) for v in row] for row in o.matrix_world],
                        'slots':[[s.link,s.material.name if s.material else None] for s in o.material_slots],
                        'mods':[[m.name,m.type,m.show_viewport,m.show_render] for m in o.modifiers],
                        'pose':[[p.name,list(p.rotation_quaternion),list(p.location),list(p.scale)] for p in o.pose.bones] if o.type=='ARMATURE' else [],
                        'vertices':[list(v.co) for v in o.data.vertices] if o.type=='MESH' else [],
                        'polygons':[[list(p.vertices),p.material_index] for p in o.data.polygons] if o.type=='MESH' else [],
                        'uv':[[u.name,[list(v.uv) for v in u.data]] for u in o.data.uv_layers] if o.type=='MESH' else [],
                        'weights':[[[g.group,g.weight] for g in v.groups] for v in o.data.vertices] if o.type=='MESH' else [],
                        'keys':[[k.name,k.value,[list(p.co) for p in k.data]] for k in o.data.shape_keys.key_blocks] if o.type=='MESH' and o.data.shape_keys else []}
                       for o in bpy.data.objects},
            'datablocks':{n:sorted(v.name for v in getattr(bpy.data,n)) for n in ('meshes','materials','scenes','cameras','lights','worlds','shape_keys')},
            'cycles_preferences':{'compute_device_type':bpy.context.preferences.addons['cycles'].preferences.compute_device_type,
                                 'devices':[[d.name,d.type,d.use] for d in bpy.context.preferences.addons['cycles'].preferences.devices]}}


def pixel_difference(first,second):
    import bpy
    images=[]
    try:
        for path in (first,second):images.append(bpy.data.images.load(path,check_existing=False))
        if tuple(images[0].size)!=tuple(images[1].size):raise AssertionError('pixel size mismatch')
        a=list(images[0].pixels);b=list(images[1].pixels)
        differences=[abs(x-y) for i,(x,y) in enumerate(zip(a,b)) if i%4!=3]
        return {'max_absolute_rgb':max(differences),'mean_absolute_rgb':sum(differences)/len(differences),
                'rms_rgb':(sum(d*d for d in differences)/len(differences))**.5,
                'samples_changed_above_1e_6':sum(d>1e-6 for d in differences),'rgb_samples':len(differences),
                'scope':'loaded PNG RGB difference; no artistic score'}
    finally:
        for image in images:bpy.data.images.remove(image)


def main(folder):
    import bpy
    folder=Path(folder).resolve();folder.mkdir(exist_ok=False,parents=True)
    report={'status':'RUNNING','checks':[],'errors':[],'blender':bpy.app.version_string}
    def check(name,value,detail=None):
        report['checks'].append({'name':name,'passed':bool(value),'detail':detail})
        if not value:raise AssertionError(name)
    try:
        fixture=build(folder/'fixtures')
        check('23 controlled fixtures including required22 + missingtile',len(fixture['cases'])==23)
        check('source backup byte identical',fixture['source_sha256']==fixture['backup_sha256'])
        texture_defs=list(fixture['texture_definitions'].values())
        check('seven real texture semantics have distinct file bytes',len({d['sha256'] for d in texture_defs})==7)
        check('loaded PNG pixels preserve authored variation',all(d['pixel_validation']['passed'] for d in texture_defs))
        udim_defs=fixture['cases']['single_material_multi_udim']['texture_definitions'][0]['tiles']
        check('five UDIM images have distinct authored colors',len({d['sha256'] for d in udim_defs})==5)
        source=bpy.data.objects[fixture['cases']['source_preservation']['object_name']]
        original=source.active_material;candidate=original.copy();candidate.name='M9_ProbeCandidate'
        candidate.node_tree.nodes.get('Principled BSDF').inputs['Metallic'].default_value=.9
        candidate.node_tree.nodes.get('Principled BSDF').inputs['Roughness'].default_value=.18
        bridge=LocalBridge();storage=Storage(folder/'operations')
        before=snapshot();start=time.perf_counter()
        output=render.render(bridge,storage,'probe-eevee',source.name,original.name,candidate.name,
                             {'width':64,'height':64,'lighting_set':list(render.LIGHTING),'views':list(render.VIEWS)})
        report['render']=output;report['wall_seconds']=time.perf_counter()-start
        after=snapshot()
        def differences(left,right,path=''):
            if type(left)!=type(right):return [{'path':path,'before':str(left)[:300],'after':str(right)[:300]}]
            if isinstance(left,dict):
                return [row for key in sorted(set(left)|set(right)) for row in differences(left.get(key),right.get(key),path+'/'+key)]
            if isinstance(left,list):
                if len(left)!=len(right):return [{'path':path,'before_length':len(left),'after_length':len(right)}]
                return [row for i,(a,b) in enumerate(zip(left,right)) for row in differences(a,b,path+'/'+str(i))]
            return [] if left==right else [{'path':path,'before':left,'after':right}]
        report['state_differences']=differences(before,after)
        check('real EEVEE render succeeded',output['status']=='ok',output['errors'])
        check('24 actual paired images',len(output['files'])==24)
        check('PBR renderer readback',output['render_backend']['engine_readback']=='BLENDER_EEVEE')
        check('source/current RNA and source datablocks restored',before==after)
        check('renderer restoration claim',output['state_restored'])
        check('source blend bytes unchanged',sha256(Path(fixture['source_file']))==fixture['source_sha256'])
        cache=fixture['cases']['source_preservation']['preservation']
        check('source physics cache sentinel unchanged',sha256(Path(cache['cache_path']))==cache['cache_sha256'])
        pairs={}
        for row in output['files']:pairs.setdefault((row['lighting'],row['view']),{})[row['case']]=row['path']
        report['paired_pixel_differences']=[{'lighting':key[0],'view':key[1],**pixel_difference(v['BEFORE'],v['AFTER'])} for key,v in pairs.items()]
        check('shader changed actual RGB in every paired view',all(v['samples_changed_above_1e_6']>0 for v in report['paired_pixel_differences']))
        # In a factory process, Cycles preferences are NONE. An explicit OptiX request
        # must reject, even if the machine physically has an OptiX-capable card.
        rejected=render.render(bridge,storage,'probe-optix-unconfigured',source.name,original.name,candidate.name,
                               {'engine':'CYCLES','backend':'OPTIX','require_gpu':True,'width':64,'height':64})
        report['gpu_unavailable']=rejected
        check('unconfigured GPU request does not fall back silently',rejected['status']=='error' and not rejected['files'])
        check('failed GPU preflight restores scene and preferences',snapshot()==before and rejected['state_restored'])
        check('single material has five UDIM tiles',fixture['cases']['single_material_multi_udim']['image_tiles']==[1001,1002,1011,1012,1021])
        # Blender internally evaluates other scene channels during a render. Cover
        # multiple rigs, an independently posed mesh copy and three collection orders.
        from mathutils import Quaternion
        original_rig=bpy.data.objects[cache['rig']]
        rigs=[original_rig]
        for name in ('M9_ManualRig_Z','M9_ManualRig_A'):
            rig=original_rig.copy();rig.data=original_rig.data.copy();rig.name=name
            scene=bpy.context.scene;scene.collection.objects.link(rig);rigs.append(rig)
        for index,rig in enumerate(rigs):
            control=rig.pose.bones['ExplicitRoot'];control.rotation_mode='QUATERNION'
            control.rotation_quaternion=Quaternion((0,1,0),.02);control.keyframe_insert('rotation_quaternion',frame=1)
            control.rotation_quaternion=Quaternion((0,1,0),.08);control.keyframe_insert('rotation_quaternion',frame=12)
        meshcopy=source.copy();meshcopy.data=source.data.copy();meshcopy.name='M9_ManualCandidateMesh'
        bpy.context.scene.collection.objects.link(meshcopy)
        report['orders']=[]
        for order in (rigs,list(reversed(rigs)),[rigs[1],rigs[0],rigs[2]]):
            for rig in order:
                bpy.context.scene.collection.objects.unlink(rig);bpy.context.scene.collection.objects.link(rig)
            bpy.context.scene.frame_set(5,subframe=.25)
            for index,rig in enumerate(rigs):rig.pose.bones['ExplicitRoot'].rotation_quaternion=Quaternion((0,1,0),.23+.11*index)
            source.location.x=.73;source.data.shape_keys.key_blocks['M9_SourceShape'].value=.63
            meshcopy.location.x=.97;meshcopy.data.shape_keys.key_blocks['M9_SourceShape'].value=.41
            bpy.context.view_layer.update();saved=snapshot()
            row=render.render(bridge,storage,'probe-order-'+str(len(report['orders'])),source.name,original.name,candidate.name,
                {'width':64,'height':64,'lighting_set':['NEUTRAL'],'views':['THREE_QUARTER']})
            check('multiple rig/manual RNA and candidate KEY order '+str(len(report['orders'])),row['status']=='ok' and row['state_restored'] and snapshot()==saved)
            report['orders'].append({'order':[r.name for r in order],'render':row})
        class FailingBridge(LocalBridge):
            def run(self,body,params):
                target='operator=bpy.ops.render.render(write_still=True,scene=scene.name)'
                injected="if len(result['files'])==1:raise RuntimeError('M9_INTENTIONAL_RENDER_FAILURE')\n                    "+target
                return super().run(body.replace(target,injected),params)
        saved=snapshot()
        failed=render.render(FailingBridge(),storage,'probe-injected-failure',source.name,original.name,candidate.name,
            {'width':64,'height':64,'lighting_set':['NEUTRAL'],'views':['THREE_QUARTER']})
        report['injected_failure']=failed
        check('render failure after first image restores all manual RNA',failed['status']=='error' and len(failed['files'])==1 and failed['state_restored'] and snapshot()==saved)
        # These probes measure response, not realistic translucency or skin quality.
        report['advanced_features']=[]
        control=render.render(bridge,storage,'probe-identical-material-control',source.name,original.name,original.name,
            {'width':96,'height':96,'lighting_set':['NEUTRAL'],'views':['THREE_QUARTER'],'samples':32})
        control_difference=pixel_difference(control['files'][0]['path'],control['files'][1]['path'])
        report['identical_control']={'render':control,'pixel_difference':control_difference}
        check('same-material control is pixel deterministic',control['status']=='ok' and control_difference['max_absolute_rgb']==0.)
        for case,socket,disabled in (('skin_sss','Subsurface Weight',0.),('ice_transmission','Transmission Weight',0.),('opacity','Alpha',1.)):
            case_obj=bpy.data.objects[fixture['cases'][case]['object_name']];case_mat=case_obj.active_material
            compared=case_mat.copy();compared.name='M9_Probe_'+case+'_Disabled';compared.node_tree.nodes.get('Principled BSDF').inputs[socket].default_value=disabled
            saved=snapshot()
            row=render.render(bridge,storage,'probe-feature-'+case,case_obj.name,case_mat.name,compared.name,
                {'width':96,'height':96,'lighting_set':['NEUTRAL'],'views':['THREE_QUARTER'],'samples':32})
            difference=pixel_difference(row['files'][0]['path'],row['files'][1]['path']) if len(row['files'])==2 else {}
            report['advanced_features'].append({'feature':case,'render':row,'pixel_difference':difference})
            check('EEVEE actual feature RGB response '+case,row['status']=='ok' and difference['samples_changed_above_1e_6']>0)
            check('advanced feature source and preferences preserved '+case,row['state_restored'] and snapshot()==saved)
        saved=snapshot()
        cpu=render.render(bridge,storage,'probe-approved-cpu',source.name,original.name,candidate.name,
            {'engine':'CYCLES','backend':'CPU','allow_cpu_fallback':True,'width':64,'height':64,
             'lighting_set':['NEUTRAL'],'views':['THREE_QUARTER'],'samples':4})
        report['explicit_cpu']=cpu
        check('explicit CPU render identifies CPU truthfully',cpu['status']=='ok' and cpu['render_backend']['actual_backend']=='CPU')
        check('CPU render restores source context/preferences',cpu['state_restored'] and snapshot()==saved)
        cloth=source.modifiers[cache['physics_modifier']]
        cloth.show_viewport=True
        saved=snapshot();files_before=sorted(str(p) for p in (folder/'operations').rglob('*.png'))
        point_cache_before={'frame_start':cloth.point_cache.frame_start,'frame_end':cloth.point_cache.frame_end,
                            'is_baked':cloth.point_cache.is_baked,'is_outdated':cloth.point_cache.is_outdated,
                            'info':cloth.point_cache.info}
        rejection=None
        try:
            render.render(bridge,storage,'probe-active-native-cache',source.name,original.name,candidate.name,
                {'width':64,'height':64,'lighting_set':['NEUTRAL'],'views':['THREE_QUARTER']})
        except ValueError as exc:rejection=str(exc)
        report['active_native_cache_negative']={'rejection':rejection,'point_cache_before':point_cache_before,
            'point_cache_after':{'frame_start':cloth.point_cache.frame_start,'frame_end':cloth.point_cache.frame_end,
                            'is_baked':cloth.point_cache.is_baked,'is_outdated':cloth.point_cache.is_outdated,
                            'info':cloth.point_cache.info}}
        check('active native cache rejects before RNA, scene or PNG mutation',
              rejection and rejection.startswith('MATERIAL_RENDER_ACTIVE_SIMULATION_UNSUPPORTED') and snapshot()==saved
              and sorted(str(p) for p in (folder/'operations').rglob('*.png'))==files_before
              and point_cache_before==report['active_native_cache_negative']['point_cache_after'])
        # Only this synthetic test harness restores its own fixture configuration.
        # Production renderer never disables a modifier to make a request pass.
        cloth.show_viewport=False
        check('final original blend still byte identical',sha256(Path(fixture['source_file']))==fixture['source_sha256'])
        report['status']='PASS'
    except Exception as exc:
        report['status']='FAIL';report['errors'].append({'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()})
    report['passed']=sum(c['passed'] for c in report['checks']);report['total']=len(report['checks'])
    with (folder/'report.json').open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2)
    print('__M9_RENDER_PROBE__='+json.dumps({'status':report['status'],'passed':report['passed'],'total':report['total'],'report':str(folder/'report.json')}))
    if report['status']!='PASS':raise SystemExit(1)


if __name__=='__main__':
    main(sys.argv[sys.argv.index('--')+1])
