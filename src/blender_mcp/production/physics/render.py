"""Paired stack previews: keep M6+M7 evaluated, toggle owned M8 copies only."""
from __future__ import annotations

from pathlib import Path
import time

from ..qa.capture import _STATE, _verify_images
from ..animation.generation.render import write_apng, write_player
from ..common.storage import sha256, write_json
from .integrity import RUNTIME as INTEGRITY_RUNTIME

VIEWS = ('FRONT','SIDE','THREE_QUARTER')

RUNTIME = INTEGRITY_RUNTIME + _STATE + r'''
def pr_render(params):
    source,primary,deps=px_selection(params)
    owned=[px_object(n) for n in params['owned_objects']]
    target=px_object(params['render_candidate'])
    if any(not o.get('production_m8_owned') or o.get('production_generation_id')!=params['generation_id'] for o in owned+[target]):
        px_fail('PHYSICS_CANDIDATE_OWNERSHIP','preview only composes owned secondary copies')
    source_objects=[px_object(n) for n in params['source_render_objects']]
    deps=px_dependencies(deps+owned+[target]+source_objects)
    prod_require_evaluable(deps);px_validate_objects(deps)
    captured=px_capture(deps)
    scene=bpy.context.scene;before=qa_state(scene);old_camera=scene.camera
    visibility={o.name:o.hide_render for o in owned+[target]+source_objects}
    marker_cameras=[(m,m.camera) for m in scene.timeline_markers]
    engine=scene.render.engine;transparent=scene.render.film_transparent
    shading=scene.display.shading
    shade={'light':shading.light,'color_type':shading.color_type,'background_type':shading.background_type,
           'background_color':list(shading.background_color),'show_shadows':shading.show_shadows,'show_cavity':shading.show_cavity}
    camera=None;camera_data=None;files=[];series=[];settings=[]
    try:
        points=[];frames=params['frames'];step=max(1,len(frames)//8)
        for frame in sorted(set(frames[::step]+[frames[-1]])):
            scene.frame_set(frame);graph=bpy.context.evaluated_depsgraph_get()
            for obj in primary+[source,target]:
                evaluated=obj.evaluated_get(graph)
                if obj.type=='MESH':
                    points.extend(evaluated.matrix_world@Vector(v) for v in evaluated.bound_box)
        if not points:
            # Bone-only previews require explicit visible mesh/driver fixtures in the same scene.
            for obj in scene.objects:
                if obj.type=='MESH' and not obj.get('production_m8_owned'):
                    points.extend(obj.matrix_world@Vector(v) for v in obj.bound_box)
        if not points:
            px_fail('PHYSICS_PREVIEW_GEOMETRY_REQUIRED','no visible geometry to frame')
        minimum=Vector(tuple(min(p[i] for p in points) for i in range(3)))
        maximum=Vector(tuple(max(p[i] for p in points) for i in range(3)))
        centre=(minimum+maximum)/2;span=max(maximum-minimum)*1.35
        if not math.isfinite(span) or span<=0:
            px_fail('PHYSICS_PREVIEW_BOUNDS_INVALID','finite nonempty framing required')
        scene.render.engine='BLENDER_WORKBENCH';scene.render.film_transparent=False
        shading.light='STUDIO';shading.color_type='MATERIAL';shading.background_type='WORLD'
        shading.background_color=(.035,.035,.035);shading.show_shadows=True;shading.show_cavity=True
        backend=qa_render_backend(scene,False);qa_configure(scene,params['width'],params['height'])
        camera_data=bpy.data.cameras.new('M8_QA_'+params['generation_id'][:8]);camera_data.type='ORTHO';camera_data.ortho_scale=span
        camera=bpy.data.objects.new(camera_data.name,camera_data);scene.collection.objects.link(camera)
        for marker,old in marker_cameras:
            marker.camera=None
        directions={'FRONT':Vector((0,-1,.12)),'SIDE':Vector((1,0,.12)),'THREE_QUARTER':Vector((1,-1,.35))}
        for view in params['views']:
            direction=directions[view].normalized();camera.location=centre+direction*span*3
            camera.rotation_euler=(centre-camera.location).to_track_quat('-Z','Y').to_euler()
            setting={'view':view,'location':list(camera.location),'rotation':list(camera.rotation_euler),
                     'ortho_scale':camera_data.ortho_scale,'engine':scene.render.engine,'light':shading.light}
            settings.append(setting)
            for case in ('PRIMARY_ONLY','PRIMARY_PLUS_SECONDARY'):
                for obj in owned+[target]:
                    obj.hide_render=True
                for obj in source_objects:
                    obj.hide_render=case=='PRIMARY_PLUS_SECONDARY'
                target.hide_render=case=='PRIMARY_ONLY'
                row={'view':case+'/'+view,'case':case,'camera_view':view,'files':[],'camera':setting}
                for frame in frames:
                    scene.frame_set(frame);scene.camera=camera
                    path=params['directories'][case][view]+'/frame_'+format(frame,'06d')+'.png'
                    scene.render.filepath=path;bpy.ops.render.render(write_still=True)
                    item={'path':path,'frame':frame,'view':view,'case':case};files.append(item);row['files'].append(item)
                series.append(row)
        result={'status':'ok','files':files,'series':series,'camera_settings':settings,'backend':backend,'errors':[]}
    finally:
        for name,value in visibility.items():
            px_object(name).hide_render=value
        for marker,old in marker_cameras:
            marker.camera=old
        if camera is not None:
            bpy.data.objects.remove(camera,do_unlink=True)
        if camera_data is not None:
            bpy.data.cameras.remove(camera_data)
        qa_restore(scene,before,old_camera);scene.render.engine=engine;scene.render.film_transparent=transparent
        shading.light=shade['light'];shading.color_type=shade['color_type'];shading.background_type=shade['background_type']
        shading.background_color=shade['background_color'];shading.show_shadows=shade['show_shadows'];shading.show_cavity=shade['show_cavity']
        px_restore(deps,captured)
    result['state_restored']=qa_state(scene)==before and scene.render.engine==engine and px_capture(deps)==captured
    if not result['state_restored']:
        px_fail('PHYSICS_RENDER_STATE_NOT_RESTORED','render context differs after preview')
    return result
'''
PAYLOAD = RUNTIME + '\nresult=pr_render(params)\n'


def temporal(bridge, storage, operation_id, plan, state, options=None):
    settings=dict(options or {})
    if set(settings)-{'width','height','views'}:
        raise ValueError('PHYSICS_RENDER_OPTION_UNSUPPORTED: temporal window, step and primary stack are fixed by the plan')
    width=settings.get('width',192);height=settings.get('height',192)
    if any(type(n) is not int or not 64<=n<=512 for n in (width,height)):
        raise ValueError('PHYSICS_RENDER_SIZE: integer dimensions 64..512')
    views=settings.get('views',list(VIEWS))
    if len(views)!=3 or set(views)!=set(VIEWS):
        raise ValueError('THREE_VIEWS_REQUIRED: FRONT SIDE THREE_QUARTER')
    brief=plan['brief'];frames=list(range(brief['frame_start'],brief['frame_end']+1))
    images=len(frames)*len(views)*2
    if images>3606 or images*width*height>200_000_000:
        raise ValueError('PHYSICS_PREVIEW_CAPACITY: paired full temporal preview exceeds the declared image/pixel budget')
    folder=storage.reserve('physics-qa',operation_id);directories={}
    for case in ('PRIMARY_ONLY','PRIMARY_PLUS_SECONDARY'):
        directories[case]={}
        for view in views:
            dest=folder/case/view;dest.mkdir(parents=True,exist_ok=False)
            if len(str(dest/'frame_000001.png'))>=260:
                raise ValueError('OUTPUT_PATH_TOO_LONG: use a shorter artifact store')
            directories[case][view]=str(dest)
    candidate=state['candidate'];baked=state.get('baked_candidate')
    target=baked['candidate_object'] if baked else candidate['candidate_object']
    owned=list(candidate['source_map'])+([target] if target not in candidate['source_map'] else [])
    params=dict(plan['params'],generation_id=plan['generation_id'],owned_objects=owned,render_candidate=target,
                source_render_objects=[plan['object_name']],frames=frames,views=views,width=width,height=height,directories=directories)
    write_json(folder/'intent.json',params);began=time.perf_counter()
    result=bridge.run(PAYLOAD,params)
    result.update(output_dir=str(folder),candidate_action=state.get('candidate_action'),timing={'frames':frames,'fps':brief['fps'],'fps_base':1},animations=[])
    for row in result['series']:
        case=row['case'];view=row['camera_view']
        checked=_verify_images({'status':'ok','files':row['files'],'errors':[]},Path(directories[case][view]))
        if checked['status']!='ok' or [x['frame'] for x in row['files']]!=frames:
            raise ValueError('TEMPORAL_ARTIFACT_INVALID: incomplete paired sequence')
        path=folder/(case+'_'+view+'.png');write_apng(row['files'],path,brief['fps'])
        result['animations'].append({'view':case+'/'+view,'path':str(path),'sha256':sha256(path),'frames':len(frames),'fps':brief['fps']})
    result['temporal_complete']=len(result['files'])==images and len(result['series'])==6
    result['metrics']={'images':images,'frames_per_view':len(frames),'paired_views':6,'elapsed_ms':round((time.perf_counter()-began)*1000,2)}
    write_player(result)
    player=folder/'preview.html';html=player.read_text(encoding='utf-8').replace('M6 temporal QA','M8 temporal QA').replace('M6 — Revisión temporal de la candidata','M8 — Primaria / primaria + secundaria')
    html=html.replace('img{width:32%}', 'main{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}figure{margin:0}figcaption{font-size:13px}img{width:100%}body{max-width:900px;margin:12px auto}')
    for case,label in (('PRIMARY_ONLY','Primaria'),('PRIMARY_PLUS_SECONDARY','Primaria + secundaria')):
        for view,caption in (('FRONT','Frontal'),('SIDE','Lateral'),('THREE_QUARTER','Tres cuartos')):
            tag='<img alt="'+case+'/'+view+'" data-view="'+case+'/'+view+'">'
            html=html.replace(tag,'<figure><figcaption>'+label+' · '+caption+'</figcaption>'+tag+'</figure>')
    html=html.replace('<p id="position"></p>','<p id="position"></p><main>').replace('<script>','</main><script>',1)
    html=html.replace("String(frames[at]).padStart(6,'0')", "(frames[at]<0?'-'+String(-frames[at]).padStart(5,'0'):String(frames[at]).padStart(6,'0'))")
    player.write_text(html,encoding='utf-8');result['player']=str(player)
    write_json(folder/'result.json',result)
    return result
