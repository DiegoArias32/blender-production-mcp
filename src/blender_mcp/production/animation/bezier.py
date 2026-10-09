"""Bézier diagnostics and explicit overshoot repair through M3 transactions."""
from __future__ import annotations

from ..common.m3_transaction import BASE_RUNTIME, transaction

BEZIER_RUNTIME=r'''
def bezier_curves(obj,action,options):
    if action is None: raise ValueError('Active Action required')
    paths=options.get('paths',[])
    bones=options.get('bones',[])
    limit=int(options.get('limit',50));offset=int(options.get('offset',0))
    if not 1<=limit<=200 or offset<0: raise ValueError('limit 1..200, offset>=0 required')
    allowed={obj.pose.bones[n].path_from_id() for n in bones if obj.type=='ARMATURE' and n in obj.pose.bones}
    if len(allowed)!=len(bones): raise ValueError('Bone filter references missing bone')
    return [c for _,_,_,c in action_curves(action,m2_slot(obj)) if (not paths or c.data_path in paths)
        and (not bones or any(c.data_path.startswith(p+'.') for p in allowed))]

def bezier_findings(obj,action,options):
    rows=[];segments=0;keys=0
    eps=float(options.get('overshoot_tolerance',.0001))
    ratio=float(options.get('tangent_context_ratio',8.))
    if eps<0 or ratio<1: raise ValueError('Invalid Bézier thresholds')
    for curve in bezier_curves(obj,action,options):
        points=list(curve.keyframe_points);keys+=len(points)
        slopes=[abs((b.co.y-a.co.y)/(b.co.x-a.co.x)) for a,b in zip(points,points[1:]) if b.co.x>a.co.x]
        for i,(a,b) in enumerate(zip(points,points[1:])):
            if a.interpolation!='BEZIER': continue
            segments+=1;duration=b.co.x-a.co.x
            if duration<=0: continue
            low,high=min(a.co.y,b.co.y),max(a.co.y,b.co.y)
            samples=[curve.evaluate(a.co.x+duration*j/32) for j in range(33)]
            overshoot=max(0.,max(samples)-high,low-min(samples))
            outside=a.handle_right.x<a.co.x or a.handle_right.x>b.co.x or b.handle_left.x<a.co.x or b.handle_left.x>b.co.x
            if overshoot>eps or outside:
                rows.append({'path':curve.data_path,'index':curve.array_index,'frame_range':[float(a.co.x),float(b.co.x)],
                    'issue':'handle_overshoot' if overshoot>eps else 'handle_time_fold',
                    'max_overshoot_native':overshoot,'severity':'review','confidence':.85,
                    'reason':'Between-key values exceed endpoint interval; may be intended anticipation'})
        for i,key in enumerate(points[1:-1],1):
            left_dx=key.co.x-key.handle_left.x;right_dx=key.handle_right.x-key.co.x
            if left_dx<1e-7 or right_dx<1e-7: continue
            ls=(key.co.y-key.handle_left.y)/left_dx;rs=(key.handle_right.y-key.co.y)/right_dx
            context=slopes[max(0,i-2):min(len(slopes),i+2)]
            reference=max(max(context,default=0.),float(options.get('slope_floor',.001)))
            if abs(rs-ls)>reference*ratio and points[i-1].interpolation=='BEZIER' and key.interpolation=='BEZIER':
                rows.append({'path':curve.data_path,'index':curve.array_index,'frame':float(key.co.x),
                    'issue':'tangent_discontinuity','slope_change_native_per_frame':abs(rs-ls),
                    'local_slope_reference':reference,'severity':'review','confidence':.75})
    limit=int(options.get('limit',50));offset=int(options.get('offset',0))
    return {'findings':rows[offset:offset+limit],'total_findings':len(rows),'offset':offset,'limit':limit,
        'metrics':{'bezier_segments':segments,'keys_analyzed':keys,'overshoot_findings':sum(r['issue']=='handle_overshoot' for r in rows),
            'tangent_findings':sum(r['issue']=='tangent_discontinuity' for r in rows)},
        'warnings':['Fast motion alone is not an error. Overshoot can be intentional; no automatic artistic label.']}

def m3_allowed_drivers(obj,options):
    return []

def m3_context(obj,options):
    return m3_context_base(obj)

def m3_propose(obj,source,options):
    if options.get('strategy','clamp_overshoot')!='clamp_overshoot':
        return {'supported':False,'warnings':['Only explicitly selected clamp_overshoot supported']}
    analysis=bezier_findings(obj,source,dict(options,offset=0,limit=200))
    channels=options.get('channels',[])
    if not isinstance(channels,list) or not channels:
        return {'supported':False,'analysis':analysis,'warnings':['Explicit channels [{path,index}] required; no broad repair']}
    selected={(c['path'],int(c['index'])) for c in channels}
    if any('rotation_quaternion' in path or 'rotation_axis_angle' in path for path,index in selected):
        return {'supported':False,'warnings':['Quaternion component Bézier repair unsupported; use quaternion continuity tool']}
    patches=[]
    for curve in bezier_curves(obj,source,options):
        if (curve.data_path,curve.array_index) not in selected: continue
        points=list(curve.keyframe_points)
        for i,(a,b) in enumerate(zip(points,points[1:])):
            if a.interpolation!='BEZIER': continue
            low,high=min(a.co.y,b.co.y),max(a.co.y,b.co.y)
            if max(a.handle_right.y,b.handle_left.y)>high+1e-8 or min(a.handle_right.y,b.handle_left.y)<low-1e-8:
                patches.append({'path':curve.data_path,'index':curve.array_index,'frame':float(a.co.x),
                    'next_frame':float(b.co.x),'right':[a.co.x+(b.co.x-a.co.x)/3,min(high,max(low,a.handle_right.y))],
                    'left':[b.co.x-(b.co.x-a.co.x)/3,min(high,max(low,b.handle_left.y))]})
    return {'supported':True,'patches':patches,'patch_count':len(patches),'analysis':analysis,
        'warnings':['Only reviewed handle endpoints are clamped. Candidate rejected if dense deviation exceeds max_deviation.']}

def m3_apply(obj,source,candidate,proposal,options):
    curves={(c.data_path,c.array_index):c for _,_,_,c in action_curves(candidate,m2_slot(obj))}
    for patch in proposal['patches']:
        curve=curves[(patch['path'],patch['index'])]
        a=next(k for k in curve.keyframe_points if abs(k.co.x-patch['frame'])<1e-5)
        b=next(k for k in curve.keyframe_points if abs(k.co.x-patch['next_frame'])<1e-5)
        a.handle_right_type='FREE';b.handle_left_type='FREE'
        a.handle_right=patch['right'];b.handle_left=patch['left'];curve.update()
    return {'keys_created':0,'handles_modified':2*len(proposal['patches'])}

def m3_verify(obj,source,candidate,proposal,options):
    left={(c.data_path,c.array_index):c for _,_,_,c in action_curves(source,None)}
    right={(c.data_path,c.array_index):c for _,_,_,c in action_curves(candidate,None)}
    error=0.;maximum_velocity_increase=0.;maximum_acceleration_increase=0.;evaluations=0
    selected={(p['path'],p['index']) for p in proposal['patches']}
    for key in selected:
        a,b=left[key],right[key]
        points=list(a.keyframe_points);candidate_points=list(b.keyframe_points)
        if len(points)!=len(candidate_points): raise ValueError('Handle repair unexpectedly changed key count')
        for i,(pa,pb) in enumerate(zip(points,points[1:])):
            ca,cb=candidate_points[i],candidate_points[i+1]
            changed=(list(pa.handle_right)!=list(ca.handle_right) or list(pb.handle_left)!=list(cb.handle_left)
                or list(pa.co)!=list(ca.co) or list(pb.co)!=list(cb.co))
            if not changed: continue
            lo,hi=float(pa.co.x),float(pb.co.x)
            if hi<=lo: raise ValueError('Nonpositive key interval unsupported')
            count=129;dt=(hi-lo)/(count-1)
            if evaluations+count>32000: raise ValueError('Per-segment verification budget exceeded; select fewer channels')
            av=[a.evaluate(lo+i*dt) for i in range(count)];bv=[b.evaluate(lo+i*dt) for i in range(count)]
            error=max(error,max(abs(x-y) for x,y in zip(av,bv)));evaluations+=count
            da=[(y-x)/dt for x,y in zip(av,av[1:])];db=[(y-x)/dt for x,y in zip(bv,bv[1:])]
            maximum_velocity_increase=max(maximum_velocity_increase,max((abs(y)-abs(x) for x,y in zip(da,db)),default=0.))
            aa=[(y-x)/dt for x,y in zip(da,da[1:])];ab=[(y-x)/dt for x,y in zip(db,db[1:])]
            maximum_acceleration_increase=max(maximum_acceleration_increase,max((abs(y)-abs(x) for x,y in zip(aa,ab)),default=0.))
    before=bezier_findings(obj,source,options);after=bezier_findings(obj,candidate,options)
    tolerance=float(options.get('max_deviation',.01))
    accel=float(options.get('max_acceleration_increase',.05))
    errors=[]
    if error>tolerance: errors.append('Dense native-channel deviation exceeds reviewed tolerance')
    if maximum_acceleration_increase>accel: errors.append('Acceleration increase exceeds reviewed native-unit/frame² tolerance')
    if maximum_velocity_increase>float(options.get('max_velocity_increase',.1)): errors.append('Velocity increase exceeds reviewed native-unit/frame tolerance')
    if after['metrics']['tangent_findings']>before['metrics']['tangent_findings']: errors.append('New tangent discontinuity introduced')
    return {'passed':not errors,'metrics':{'max_motion_error_native':error,'samples':evaluations,
        'max_velocity_increase_native_per_frame':maximum_velocity_increase,
        'max_acceleration_increase_native_per_frame2':maximum_acceleration_increase,
        'findings_before':before['total_findings'],'findings_after':after['total_findings']},'errors':errors,'warnings':[]}
'''


def analyze_bezier(bridge,object_name,options=None):
    options=validate_options(options)
    return bridge.run(BASE_RUNTIME+BEZIER_RUNTIME+"\nobj=m2_object(params['object_name'])\nresult=bezier_findings(obj,obj.animation_data.action if obj.animation_data else None,params['options'])\n",
        {'object_name':object_name,'options':options or {}})


def repair_bezier(bridge,storage,operation_id,object_name=None,mode='analyze',options=None,operation=None):
    return transaction(bridge,storage,operation_id,'bezier_repair',BEZIER_RUNTIME,object_name,mode,validate_options(options),operation)


def validate_options(options):
    from .corrections import _validate_options
    options={} if options is None else options
    _validate_options(options)
    allowed={'paths','bones','limit','offset','overshoot_tolerance','tangent_context_ratio','slope_floor',
        'strategy','channels','max_deviation','max_acceleration_increase','max_velocity_increase'}
    if set(options)-allowed: raise ValueError('Unknown Bézier options: '+', '.join(sorted(set(options)-allowed)))
    return options
