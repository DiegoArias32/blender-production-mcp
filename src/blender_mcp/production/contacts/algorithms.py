"""Pure contact/derivative metrics. Thresholds describe evidence, not artistry."""
from __future__ import annotations

import math


def norm(v):
    return math.sqrt(sum(x*x for x in v))


def delta(a,b):
    return [x-y for x,y in zip(a,b)]


def derivative(values, times):
    if len(values) != len(times) or any(b <= a for a,b in zip(times,times[1:])):
        raise ValueError('Derivative requires matching samples at increasing times')
    if len(values)<2:
        return [[0.0]*len(values[0])] if values else []
    output=[]
    for i in range(len(values)):
        first,last=max(0,i-1),min(len(values)-1,i+1)
        output.append([x/(times[last]-times[first]) for x in delta(values[last],values[first])])
    return output


def quaternion_multiply(a,b):
    w,x,y,z=a;v,i,j,k=b
    return [w*v-x*i-y*j-z*k,w*i+x*v+y*k-z*j,w*j-x*k+y*v+z*i,w*k+x*j-y*i+z*v]


def motion_derivatives(samples, frames, fps):
    """Position m, velocity m/s, acceleration m/s²; angular metrics in degrees."""
    if fps <= 0: raise ValueError('FPS must be positive')
    times=[f/fps for f in frames]
    positions=[s['position'] for s in samples]
    interval_velocity=[[v/(times[i+1]-times[i]) for v in delta(b,a)] for i,(a,b) in enumerate(zip(positions,positions[1:]))]
    velocity=([interval_velocity[0]]+interval_velocity) if interval_velocity else ([[0.,0.,0.]] if samples else [])
    acceleration=([[0.,0.,0.]]+[[v/(times[i]-times[i-1]) for v in delta(velocity[i],velocity[i-1])] for i in range(1,len(velocity))]) if velocity else []
    steps=[];angular_vectors=[]
    for a,b in zip(samples,samples[1:]):
        qa,qb=a['rotation'],b['rotation']
        denom=max(norm(qa)*norm(qb),1e-15)
        angle=math.degrees(2*math.acos(min(1.,abs(sum(x*y for x,y in zip(qa,qb)))/denom)))
        steps.append(angle)
        relative=quaternion_multiply(qb,[qa[0],-qa[1],-qa[2],-qa[3]])
        if relative[0]<0: relative=[-v for v in relative]
        axis_length=norm(relative[1:])
        angular_vectors.append([v*angle/max(axis_length,1e-15)/(times[len(steps)]-times[len(steps)-1]) for v in relative[1:]])
    angular_vectors=([angular_vectors[0]]+angular_vectors) if angular_vectors else ([[0.,0.,0.]] if samples else [])
    angular_velocity=[norm(v) for v in angular_vectors]
    angular_acceleration=([[0.,0.,0.]]+[[v/(times[i]-times[i-1]) for v in delta(angular_vectors[i],angular_vectors[i-1])] for i in range(1,len(angular_vectors))]) if angular_vectors else []
    return {'velocity':velocity,'acceleration':acceleration,'angular_steps':steps,
        'angular_velocity':angular_velocity,'angular_velocity_vectors':angular_vectors,
        'angular_acceleration':[norm(v) for v in angular_acceleration],
        'max_speed_m_s':max(map(norm,velocity),default=0.),
        'max_acceleration_m_s2':max(map(norm,acceleration),default=0.),
        'max_angular_velocity_deg_s':max(angular_velocity,default=0.),
        'max_angular_acceleration_deg_s2':max(map(norm,angular_acceleration),default=0.)}


def phase_label(heel_contact,toe_contact,heel_velocity=0.,toe_velocity=0.,previous=None):
    if heel_contact and toe_contact:
        return 'MID_STANCE' if previous in ('FOOT_FLAT','MID_STANCE') else 'FOOT_FLAT'
    if heel_contact: return 'HEEL_STRIKE'
    if toe_contact: return 'TOE_OFF' if toe_velocity>0.03 else 'HEEL_RISE'
    return 'SWING'


def intervals(rows,key='phase'):
    result=[]
    for row in rows:
        if not result or result[-1]['value']!=row[key]:
            result.append({'value':row[key],'start':row['frame'],'end':row['frame'],'indices':[len(result)]})
        else: result[-1]['end']=row['frame']
    for row in result: row.pop('indices')
    return result


def smoothstep(value):
    t=min(1.,max(0.,value))
    return t*t*t*(10+t*(-15+6*t))


def joint_metrics(samples, frames, fps, options=None):
    """samples contain upper/lower/effector evaluated head/tail in metres."""
    options=options or {}
    angles=[];lengths=[];ratios=[];bend_directions=[]
    for row in samples:
        hip,knee,end=row['hip'],row['knee'],row['end']
        a,b=delta(hip,knee),delta(end,knee)
        total=norm(a)+norm(b)
        angles.append(math.degrees(math.acos(max(-1.,min(1.,sum(x*y for x,y in zip(a,b))/max(norm(a)*norm(b),1e-15))))))
        lengths.append(total);ratios.append(norm(delta(end,hip))/max(total,1e-15))
        axis=delta(end,hip);axis=[v/max(norm(axis),1e-15) for v in axis]
        knee_vector=delta(knee,hip);projection=sum(a*b for a,b in zip(knee_vector,axis))
        bend=[v-a*projection for v,a in zip(knee_vector,axis)]
        bend_directions.append([v/norm(bend) for v in bend] if norm(bend)>1e-7 else None)
    times=[f/fps for f in frames]
    velocities=[v[0] for v in derivative([[v] for v in angles],times)]
    accelerations=[v[0] for v in derivative([[v] for v in velocities],times)]
    jumps=[abs(b-a) for a,b in zip(angles,angles[1:])]
    plane_steps=[math.degrees(math.acos(max(-1.,min(1.,sum(x*y for x,y in zip(a,b)))))) if a and b else 0.
        for a,b in zip(bend_directions,bend_directions[1:])]
    events=[]
    jump_limit=float(options.get('joint_step_degrees',20.))
    for i,jump in enumerate(jumps):
        local=jumps[max(0,i-3):i]+jumps[i+1:i+4]
        contextual=sorted(local)[len(local)//2] if local else 0.
        plane_local=plane_steps[max(0,i-3):i]+plane_steps[i+1:i+4]
        plane_context=sorted(plane_local)[len(plane_local)//2] if plane_local else 0.
        plane_pop=plane_steps[i]>float(options.get('bend_plane_step_degrees',45.)) and (plane_steps[i]>max(1.,plane_context)*float(options.get('context_ratio',4.)) or plane_steps[i]>150.)
        if (jump>jump_limit and jump>max(1.,contextual)*float(options.get('context_ratio',4.))) or plane_pop:
            events.append({'frame':frames[i+1],'severity':'warning','confidence':.82,
                'measurements':{'angle_change_degrees':jump,'angle_degrees':angles[i+1],
                    'angular_velocity_deg_s':velocities[i+1],'angular_acceleration_deg_s2':accelerations[i+1],
                    'extension_ratio':ratios[i+1],'local_median_step_degrees':contextual,'bend_plane_change_degrees':plane_steps[i]},
                'possible_cause':'bend plane / pole direction change' if plane_pop else 'near extension / solver plane change' if ratios[i+1]>.98 else 'localized joint rotation change; may be intentional impact'})
    return {'angles':angles,'steps':jumps,'lengths':lengths,'extension_ratios':ratios,
        'angular_velocity':velocities,'angular_acceleration':accelerations,'pops':events,'bend_plane_steps':plane_steps,
        'max_bend_plane_step_degrees':max(plane_steps,default=0.),
        'max_joint_step_degrees':max(jumps,default=0.),'max_extension_ratio':max(ratios,default=0.)}
