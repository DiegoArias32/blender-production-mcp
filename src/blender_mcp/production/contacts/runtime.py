"""Blender geometry sampling; one request builds BVHs and evaluates a full range."""
import inspect
from . import algorithms

PURE_RUNTIME='\nimport math\n'+''.join(inspect.getsource(getattr(algorithms,name))+'\n' for name in
    ('norm','delta','derivative','quaternion_multiply','motion_derivatives','phase_label','intervals','smoothstep','joint_metrics'))

CONTACT_RUNTIME=PURE_RUNTIME+r'''
from mathutils import Vector, Matrix, Quaternion
from mathutils.bvhtree import BVHTree

def contact_frames(options):
    start=float(options.get('frame_start',bpy.context.scene.frame_start))
    end=float(options.get('frame_end',bpy.context.scene.frame_end))
    step=float(options.get('step',1.))
    if not all(math.isfinite(v) for v in (start,end,step)) or end<start or step<.125 or step>20:
        raise ValueError('Contact range invalid; step .125..20 required')
    if (end-start)/step>4000: raise ValueError('Contact sampling exceeds 4001 frames')
    frames=[start+i*step for i in range(int((end-start)/step)+1)]
    if frames[-1]<end-1e-6: frames.append(end)
    return frames

def contact_static_surfaces(names):
    if not isinstance(names,list) or not 1<=len(names)<=32:
        raise ValueError('Explicit 1..32 static surface mesh names required')
    surfaces=[]
    deps=bpy.context.evaluated_depsgraph_get()
    scale=bpy.context.scene.unit_settings.scale_length
    total=0
    for name in names:
        obj=bpy.context.scene.objects.get(name)
        if obj is None or obj.type!='MESH': raise ValueError('Surface mesh missing: '+str(name))
        if obj.parent or obj.constraints or obj.animation_data or obj.data.animation_data or obj.data.shape_keys:
            raise ValueError('Animated/parented/constrained surfaces unsupported: '+name)
        # Only deterministic topology modifiers without external time dependencies.
        if any(m.type not in {'TRIANGULATE','SUBSURF','BEVEL','SOLIDIFY','MIRROR','WEIGHTED_NORMAL'} for m in obj.modifiers):
            raise ValueError('Surface has unsupported potentially dynamic modifier: '+name)
        if any(m.type=='MIRROR' and m.mirror_object for m in obj.modifiers):
            raise ValueError('Surface mirror object dependency unsupported: '+name)
        evaluated=obj.evaluated_get(deps)
        mesh=evaluated.to_mesh()
        try:
            total+=len(mesh.vertices)
            if total>250000: raise ValueError('Contact BVH exceeds 250000 evaluated vertices')
            vertices=[evaluated.matrix_world@v.co*scale for v in mesh.vertices]
            faces=[list(p.vertices) for p in mesh.polygons]
            if not faces: raise ValueError('Surface has no polygons: '+name)
            tree=BVHTree.FromPolygons(vertices,faces,all_triangles=False)
            surfaces.append({'name':name,'tree':tree,'signature':m2_digest({'vertices':[list(v) for v in vertices],'faces':faces})})
        finally: evaluated.to_mesh_clear()
    return surfaces

def contact_support_axis(options):
    axis=Vector(options.get('support_direction',[0,0,1]))
    if axis.length<1e-9: raise ValueError('support_direction must be a nonzero vector')
    return axis.normalized()

def contact_classify(normal,options,generic):
    """Returns (valid support, normal oriented for support, flipped). Orientation is validated per
    face BEFORE choosing a surface (AUD-006) and modeled normals may face either way (AUD-005)."""
    oriented=Vector(normal)
    if generic:
        return True,oriented,False
    up=contact_support_axis(options);alignment=oriented.dot(up);flipped=False
    if alignment<0 and options.get('surface_orientation','two_sided')=='two_sided':
        oriented=-oriented;alignment=-alignment;flipped=True
    return alignment>=float(options.get('minimum_up_normal',.35)),oriented,flipped

def contact_candidate_distance(value):
    return value['distance']

def contact_query(point,surfaces,options,generic=False,diagnostics=None):
    distance=float(options.get('query_distance_m',.3))
    if not 0<distance<=10: raise ValueError('query_distance_m must be >0 and <=10')
    margin=float(options.get('ambiguity_margin_m',.002))
    point=Vector(point);candidates=[];rejected=0
    for surface in surfaces:
        hit,normal,index,absolute=surface['tree'].find_nearest(point,distance)
        if hit is None: continue
        valid,oriented,flipped=contact_classify(normal,options,generic)
        # Nearest face valid: only same-distance alternatives matter (ambiguity). Nearest face
        # invalid (e.g. a stair riser): every face in range competes instead of giving up.
        rows=surface['tree'].find_nearest_range(point,min(distance,absolute+margin) if valid else distance)
        if not rows:
            rows=[(hit,normal,index,absolute)]
        for location,face_normal,face,face_distance in rows:
            ok,oriented,flipped=contact_classify(face_normal,options,generic)
            if not ok:
                rejected+=1;continue
            signed=(point-location).dot(oriented)
            candidates.append({'surface_object':surface['name'],'surface_point':list(location),'surface_normal':list(oriented),
                'modeled_normal':list(face_normal),'distance':face_distance,'signed_distance_m':signed,
                # Offset along the surface to the nearest point: ~0 over the face, large past an edge.
                'lateral_offset_m':math.sqrt(max(0.,face_distance*face_distance-signed*signed)),
                'normal_orientation':'flipped_for_support' if flipped else 'as_modeled','face_index':face})
    if diagnostics is not None:
        diagnostics['queries']+=1;diagnostics['rejected_faces']+=rejected
    if not candidates:
        if diagnostics is not None:
            diagnostics['only_invalid_orientation' if rejected else 'nothing_in_range']+=1
        return None
    candidates.sort(key=contact_candidate_distance)
    best=candidates[0]
    alternatives=[c for c in candidates[1:] if c['distance']-best['distance']<=margin and
        (c['surface_object']!=best['surface_object'] or Vector(c['surface_normal']).angle(Vector(best['surface_normal']),0)>math.radians(10))]
    best.update(ambiguous=bool(alternatives),alternatives=len(alternatives),rejected_faces=rejected,
        confidence=.5 if alternatives else .96,query='evaluated_static_mesh_BVH_nearest_valid_face')
    if diagnostics is not None:
        diagnostics['valid']+=1
        diagnostics['flipped_normals']+=int(best['normal_orientation']=='flipped_for_support')
        diagnostics['ambiguous']+=int(best['ambiguous'])
    return best

def contact_feet(obj,options):
    feet=options.get('feet',[])
    if not isinstance(feet,list) or not 1<=len(feet)<=8:
        raise ValueError('Explicit feet list (1..8), with bone and local heel/toe/sole markers required')
    output=[]
    for raw in feet:
        if not isinstance(raw,dict): raise ValueError('Foot description must be an object')
        if raw.get('bone') not in obj.pose.bones: raise ValueError('Contact bone missing')
        for key in ('heel','sole','toe'):
            if key not in raw or len(raw[key])!=3 or any(not math.isfinite(float(v)) for v in raw[key]):
                raise ValueError('Explicit finite local '+key+' marker needed; units are Blender bone-local units')
        output.append(raw)
    return output

def contact_sample(obj,options,surfaces=None):
    if obj.type!='ARMATURE': raise ValueError('Contact target must be armature')
    prod_require_evaluable([obj],['contact armature'])
    surfaces=surfaces or contact_static_surfaces(options.get('surfaces',[]))
    feet=contact_feet(obj,options);frames=contact_frames(options)
    old_frame,old_sub=bpy.context.scene.frame_current,bpy.context.scene.frame_subframe
    scale=bpy.context.scene.unit_settings.scale_length
    fps=bpy.context.scene.render.fps/bpy.context.scene.render.fps_base
    records={f['bone']:[] for f in feet}
    diagnostics={f['bone']:{'queries':0,'valid':0,'only_invalid_orientation':0,'nothing_in_range':0,
        'rejected_faces':0,'flipped_normals':0,'ambiguous':0} for f in feet}
    try:
        for frame in frames:
            bpy.context.scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame))
            evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for foot in feet:
                bone=evaluated.pose.bones[foot['bone']]
                matrix=evaluated.matrix_world@bone.matrix
                points={key:list(matrix@Vector(foot[key])*scale) for key in ('heel','sole','toe')}
                hits={key:contact_query(point,surfaces,options,diagnostics=diagnostics[foot['bone']]) for key,point in points.items()}
                records[foot['bone']].append({'frame':frame,'points':points,'hits':hits,
                    'orientation':list(matrix.to_quaternion().normalized())})
    finally: bpy.context.scene.frame_set(old_frame,subframe=old_sub)
    threshold=float(options.get('contact_height_m',.01));speed=float(options.get('normal_speed_m_s',.25))
    penetration=float(options.get('max_penetration_m',.05))
    if not 0<threshold<=.2 or not 0<speed<=5: raise ValueError('Contact thresholds outside supported bounds')
    if not 0<=penetration<=.5: raise ValueError('max_penetration_m must be within 0..0.5')
    for name,rows in records.items():
        velocities={key:derivative([r['points'][key] for r in rows],[f/fps for f in frames]) for key in ('heel','toe','sole')}
        previous=None
        for i,row in enumerate(rows):
            contact={};normal_velocity={}
            for key in ('heel','toe','sole'):
                hit=row['hits'][key]
                nv=sum(a*b for a,b in zip(velocities[key][i],hit['surface_normal'])) if hit else 0.
                normal_velocity[key]=nv
                # Signed distance: a planted marker slightly inside the surface is still in contact
                # (and reported as penetration) instead of silently becoming airborne. The lateral
                # offset keeps a coplanar point beyond a platform edge out of contact: its signed
                # distance is ~0 although the nearest surface point is far away.
                contact[key]=bool(hit and -penetration<=hit['signed_distance_m']<=threshold
                                  and hit['lateral_offset_m']<=threshold and abs(nv)<=speed)
            phase=phase_label(contact['heel'],contact['toe'],normal_velocity['heel'],normal_velocity['toe'],previous)
            marker='heel' if phase=='HEEL_STRIKE' else 'toe' if phase in ('HEEL_RISE','TOE_OFF') else 'sole'
            hit=row['hits'][marker]
            row.update(phase=phase,contact=contact,marker=marker,normal_velocity=normal_velocity,
                vertical_velocity_m_s=velocities[marker][i][2],
                horizontal_velocity_m_s=norm(velocities[marker][i][:2]),
                heel_height_m=row['hits']['heel']['signed_distance_m'] if row['hits']['heel'] else None,
                toe_height_m=row['hits']['toe']['signed_distance_m'] if row['hits']['toe'] else None,
                confidence=.92 if phase!='SWING' and hit else .6,
                surface_object=hit['surface_object'] if hit else None)
            previous=phase
    return {'frames':frames,'feet':records,'fps':fps,'surface_signatures':{s['name']:s['signature'] for s in surfaces},'diagnostics':diagnostics}

def contact_foot_status(rows,diagnostics,options):
    """CONTACT_CONFIRMED / AMBIGUOUS_SURFACE / NO_CONTACT / INSUFFICIENT_CONTACT_DATA for one foot."""
    contact_rows=[r for r in rows if r['phase']!='SWING']
    minimum=int(options.get('minimum_contact_samples',2))
    if len(contact_rows)>=minimum:
        ambiguous=sum(1 for r in contact_rows if (r['hits'][r['marker']] or {}).get('ambiguous'))
        if ambiguous*2>len(contact_rows):
            return 'AMBIGUOUS_SURFACE',str(ambiguous)+' of '+str(len(contact_rows))+' contact samples have a competing surface within ambiguity_margin_m'
        return 'CONTACT_CONFIRMED',str(len(contact_rows))+' contact samples on valid support faces'
    if diagnostics.get('valid'):
        return 'NO_CONTACT','valid support surfaces were in range but markers never met the contact thresholds'
    if diagnostics.get('only_invalid_orientation'):
        return 'INSUFFICIENT_CONTACT_DATA','only faces oriented against the support direction were near the markers (surface_orientation='+options.get('surface_orientation','two_sided')+')'
    return 'INSUFFICIENT_CONTACT_DATA','no support surface within query_distance_m of the markers'

def contact_summary(sample,options):
    result=[]
    for name,rows in sample['feet'].items():
        phases=[]
        for interval in intervals(rows):
            chosen=[r for r in rows if interval['start']<=r['frame']<=interval['end']]
            first=chosen[0];marker=first['marker'];hit=first['hits'][marker]
            normal=Vector(hit['surface_normal']) if hit else Vector((0,0,1))
            origin=Vector(first['points'][marker])
            drift=[]
            for r in chosen:
                movement=Vector(r['points'][marker])-origin
                drift.append((movement-normal*movement.dot(normal)).length)
            phases.append({'phase':interval['value'],'frame_range':[interval['start'],interval['end']],
                'heel_height_m':[min((r['heel_height_m'] for r in chosen if r['heel_height_m'] is not None),default=0.),max((r['heel_height_m'] for r in chosen if r['heel_height_m'] is not None),default=0.)],
                'toe_height_m':[min((r['toe_height_m'] for r in chosen if r['toe_height_m'] is not None),default=0.),max((r['toe_height_m'] for r in chosen if r['toe_height_m'] is not None),default=0.)],
                'sole_orientation_quaternion':first['orientation'],'surface_object':first['surface_object'],
                'surface_normal':list(normal),'contact_confidence':min(r['confidence'] for r in chosen),
                'max_horizontal_velocity_m_s':max(r['horizontal_velocity_m_s'] for r in chosen),
                'max_vertical_velocity_m_s':max(abs(r['vertical_velocity_m_s']) for r in chosen),
                'sliding_m':max(drift,default=0.) if interval['value']!='SWING' else None})
        status,reason=contact_foot_status(rows,sample.get('diagnostics',{}).get(name,{}),options)
        confirmed=status in ('CONTACT_CONFIRMED','AMBIGUOUS_SURFACE')
        result.append({'bone':name,'phases':phases,'contact_status':status,'status_reason':reason,
            # No contact evidence is never reported as zero sliding (AUD-005).
            'max_contact_sliding_m':max((p['sliding_m'] or 0. for p in phases),default=0.) if confirmed else None,
            'max_penetration_m':max((max(0.,-(r['hits'][r['marker']] or {}).get('signed_distance_m',0.)) for r in rows if r['phase']!='SWING'),default=0.) if confirmed else None,
            'diagnostics':sample.get('diagnostics',{}).get(name)})
    offset=int(options.get('offset',0));limit=int(options.get('limit',50))
    if not 0<=offset or not 1<=limit<=200: raise ValueError('offset >=0, limit 1..200 required')
    flat=[{'bone':f['bone'],**p} for f in result for p in f['phases']]
    confirmed=[f for f in result if f['max_contact_sliding_m'] is not None]
    statuses=[f['contact_status'] for f in result]
    overall='CONTACT_CONFIRMED' if statuses and all(s=='CONTACT_CONFIRMED' for s in statuses) else next(
        (s for s in ('INSUFFICIENT_CONTACT_DATA','AMBIGUOUS_SURFACE','NO_CONTACT') if s in statuses),'INSUFFICIENT_CONTACT_DATA')
    warnings=['Heel/toe phases are geometric heuristics, not biomechanical certainty; markers and surface normals require review']
    if overall!='CONTACT_CONFIRMED':
        warnings.append('NO CONTACT EVIDENCE IS NOT ZERO SLIDING: '+overall+' for '+', '.join(f['bone'] for f in result if f['contact_status']!='CONTACT_CONFIRMED')+'; sliding is null for those feet')
    if any((f['diagnostics'] or {}).get('flipped_normals') for f in result):
        warnings.append('Some support faces have modeled normals facing away from the support direction; they were treated as two-sided (surface_orientation=two_sided)')
    public={'feet':[{'bone':f['bone'],'contact_status':f['contact_status'],'status_reason':f['status_reason'],
            'max_contact_sliding_m':f['max_contact_sliding_m'],'max_penetration_m':f['max_penetration_m'],'diagnostics':f['diagnostics']} for f in result],
        'contact_status':overall,
        'phases':flat[offset:offset+limit],'total_phases':len(flat),'offset':offset,'limit':limit,
        'metrics':{'frames_sampled':len(sample['frames']),
            'max_contact_sliding_m':max((f['max_contact_sliding_m'] for f in confirmed),default=None),
            'confirmed_feet':len(confirmed),'feet':len(result)},
        'warnings':warnings,
        'thresholds':{'contact_height_m':options.get('contact_height_m',.01),'normal_speed_m_s':options.get('normal_speed_m_s',.25),
            'max_penetration_m':options.get('max_penetration_m',.05),'minimum_up_normal':options.get('minimum_up_normal',.35),
            'surface_orientation':options.get('surface_orientation','two_sided')}}
    if options.get('detail','summary')=='samples':
        all_rows=[{'bone':name,**row} for name,rows in sample['feet'].items() for row in rows]
        public['samples']=all_rows[offset:offset+limit];public['total_samples']=len(all_rows)
    return public

def contact_limb_sample(obj,chains,frames):
    prod_require_evaluable([obj],['limb armature'])
    if not isinstance(chains,list) or not 1<=len(chains)<=16: raise ValueError('Explicit 1..16 chains required')
    for chain in chains:
        if len(chain)!=3 or any(name not in obj.pose.bones for name in chain): raise ValueError('Each chain needs upper, lower and effector bones')
        if obj.pose.bones[chain[1]].parent!=obj.pose.bones[chain[0]] or obj.pose.bones[chain[2]].parent!=obj.pose.bones[chain[1]]:
            raise ValueError('Limb must be an ordered direct-parent chain')
    output={chain[0]:[] for chain in chains}
    scale=bpy.context.scene.unit_settings.scale_length
    old,sub=bpy.context.scene.frame_current,bpy.context.scene.frame_subframe
    try:
        for frame in frames:
            bpy.context.scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame))
            evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            for chain in chains:
                poses=[evaluated.pose.bones[n] for n in chain]
                pole_points=[]
                for pose in poses[:2]:
                    for constraint in pose.constraints:
                        if constraint.type=='IK' and constraint.pole_target:
                            pole_obj=constraint.pole_target.evaluated_get(bpy.context.evaluated_depsgraph_get())
                            pole_pose=pole_obj.pose.bones.get(constraint.pole_subtarget) if pole_obj.type=='ARMATURE' and constraint.pole_subtarget else None
                            point=pole_obj.matrix_world@pole_pose.head if pole_pose else pole_obj.matrix_world.translation
                            pole_points.append({'object':pole_obj.name,'bone':constraint.pole_subtarget,'position':list(point*scale)})
                output[chain[0]].append({'hip':list(evaluated.matrix_world@poses[0].head*scale),
                    'knee':list(evaluated.matrix_world@poses[1].head*scale),'end':list(evaluated.matrix_world@poses[2].head*scale),
                    'rotation':list((evaluated.matrix_world@poses[1].matrix).to_quaternion().normalized()),'poles':pole_points,
                    'lengths':[((evaluated.matrix_world@poses[j].tail)-(evaluated.matrix_world@poses[j].head)).length*scale for j in (0,1)]})
    finally: bpy.context.scene.frame_set(old,subframe=sub)
    return output

def contact_limb_report(obj,options):
    chains=options.get('chains',[]);frames=contact_frames(options)
    sample=contact_limb_sample(obj,chains,frames)
    fps=bpy.context.scene.render.fps/bpy.context.scene.render.fps_base
    limbs=[]
    for chain in chains:
        metrics=joint_metrics(sample[chain[0]],frames,fps,options)
        motion=motion_derivatives([{'position':r['knee'],'rotation':r['rotation']} for r in sample[chain[0]]],frames,fps)
        pole_step=max(((Vector(pb['position'])-Vector(pa['position'])).length for a,b in zip(sample[chain[0]],sample[chain[0]][1:])
            for pa,pb in zip(a['poles'],b['poles'])),default=0.)
        rest=sum(obj.data.bones[n].length for n in chain[:2])*bpy.context.scene.unit_settings.scale_length
        # Object scale must be uniform for a scalar rest length comparison.
        scales=obj.matrix_world.to_scale()
        if max(scales)-min(scales)>1e-5: raise ValueError('Nonuniform object scale makes scalar stretch comparison ambiguous')
        rest*=abs(scales.x)
        ratios=[sum(r['lengths'])/max(rest,1e-15) for r in sample[chain[0]]]
        solver_settings=[{'bone':n,'constraint':c.name,'use_stretch':c.use_stretch,'ik_stretch':obj.pose.bones[n].ik_stretch}
            for n in chain[:2] for c in obj.pose.bones[n].constraints if c.type=='IK']
        permits=options.get('allow_stretch',any(s['use_stretch'] and s['ik_stretch']>0 for s in solver_settings))
        tolerance=float(options.get('stretch_tolerance_ratio',.02))
        if not 0<=tolerance<=2: raise ValueError('stretch_tolerance_ratio must be 0..2')
        stretch=[{'frame':frame,'ratio':ratio,'severity':'info' if permits else 'warning'} for frame,ratio in zip(frames,ratios) if ratio>1+tolerance]
        limbs.append({'chain':chain,'pops':metrics['pops'],'max_joint_step_degrees':metrics['max_joint_step_degrees'],
            'max_bend_plane_step_degrees':metrics['max_bend_plane_step_degrees'],
            'max_pole_step_m':pole_step,'pole_targets':sample[chain[0]][0]['poles'],
            'motion':{key:value for key,value in motion.items() if key.startswith('max_')},
            'max_extension_ratio':metrics['max_extension_ratio'],'max_length_ratio':max(ratios),'stretch_events':stretch[:100],
            'stretch_permitted':bool(permits),'solver_settings':solver_settings,'confidence':.95,
            'status':'warning' if metrics['pops'] or stretch and not permits else 'pass'})
    return {'limbs':limbs,'metrics':{'frames_processed':len(frames),'pop_count':sum(len(l['pops']) for l in limbs),
        'max_length_ratio':max(l['max_length_ratio'] for l in limbs)},
        'warnings':['Localized angular events may be intentional impacts; compare source and candidate at matching samples']}

def contact_effector(obj,options):
    prod_require_evaluable([obj],['effector armature'])
    bone_name=options.get('bone');local=Vector(options.get('point',[0,0,0]))
    if bone_name not in obj.pose.bones: raise ValueError('Explicit effector bone required')
    surfaces=contact_static_surfaces(options.get('surfaces',[]));frames=contact_frames(options)
    old,sub=bpy.context.scene.frame_current,bpy.context.scene.frame_subframe
    scale=bpy.context.scene.unit_settings.scale_length;rows=[]
    try:
        for frame in frames:
            bpy.context.scene.frame_set(math.floor(frame),subframe=frame-math.floor(frame))
            evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            point=evaluated.matrix_world@evaluated.pose.bones[bone_name].matrix@local*scale
            rows.append({'frame':frame,'point':list(point),'surface':contact_query(point,surfaces,options,True)})
    finally: bpy.context.scene.frame_set(old,subframe=sub)
    fps=bpy.context.scene.render.fps/bpy.context.scene.render.fps_base
    velocities=derivative([r['point'] for r in rows],[f/fps for f in frames])
    height=float(options.get('contact_height_m',.005));speed=float(options.get('contact_speed_m_s',.03))
    dwell=float(options.get('minimum_dwell_frames',3.))
    for row,velocity in zip(rows,velocities):
        row['phase']='contact' if row['surface'] and row['surface']['distance']<=height and norm(velocity)<=speed else 'near_or_moving'
    contacts=[]
    for interval in intervals(rows):
        if interval['value']!='contact' or interval['end']-interval['start']+float(options.get('step',1))<dwell: continue
        chosen=[r for r in rows if interval['start']<=r['frame']<=interval['end']]
        if len({r['surface']['surface_object'] for r in chosen})!=1: continue
        origin=Vector(chosen[0]['point'])
        contacts.append({'effector':bone_name,'surface':chosen[0]['surface']['surface_object'],
            'frame_range':[interval['start'],interval['end']],
            'sliding_m':max((Vector(r['point'])-origin).length for r in chosen),
            'penetration_estimate_m':max(max(0.,-r['surface']['signed_distance_m']) for r in chosen),
            'confidence':.85,'surface_normal':chosen[0]['surface']['surface_normal']})
    return {'effector':bone_name,'contacts':contacts,'metrics':{'frames_sampled':len(frames),'contacts':len(contacts)},
        'warnings':['Near a surface is not proof of intended contact; stationary dwell required. Penetration uses oriented local surface normal, not a collision volume.']}
'''
