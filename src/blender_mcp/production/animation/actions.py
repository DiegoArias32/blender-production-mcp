"""Action creation/duplication/reassignment with checkpoints and content checks."""
from __future__ import annotations

from .scripts import ACTION_HELPERS
from .edit_runtime import ACTION_RUNTIME
from ..checkpoint import create_checkpoint
from ..common.asset_safety import require_mutable_asset
from ..common.operations import fingerprint

BODY = ACTION_HELPERS + ACTION_RUNTIME

MANAGE = r'''
obj = m2_object(params['object_name']) if params.get('object_name') else None
source = bpy.data.actions.get(params.get('action_name','')) if params['kind']!='create' else None
if params['kind']!='create' and source is None:
    raise ValueError('Source Action not found')
if obj and (obj.library or obj.override_library):
    raise ValueError('Linked/override object assignments are unsupported')
if obj and obj.animation_data and (obj.animation_data.use_tweak_mode or any(not track.mute and len(track.strips) for track in obj.animation_data.nla_tracks)):
    raise ValueError('Action assignment during NLA composition requires manual preparation')
if params.get('assign') and obj is None:
    raise ValueError('assign requires object_name')
if source and params.get('assign') and getattr(source,'is_action_layered',False) and len(source.slots)!=1:
    raise ValueError('Assignment requires an unambiguous single Action slot')
before = m2_snapshot(source) if source else None
result={'dry_run':params['dry_run'],'source_action':source.name if source else None,
        'previous_active_action':obj.animation_data.action.name if obj and obj.animation_data and obj.animation_data.action else None,
        'warnings':[],'plan':params['kind']+' Action without deleting or overwriting existing Actions'}
if not params['dry_run']:
    if params['kind']=='create':
        action=bpy.data.actions.new(params['name'])
        if obj and hasattr(action,'slots'):
            action.slots.new(id_type='OBJECT',name=obj.name)
        action.use_fake_user=True
    elif params['kind']=='duplicate':
        action=source.copy()
        action.name=params['name'] or source.name+'__Working'
        action.use_fake_user=True
    else:
        action=source
    if params.get('assign'):
        slot=action.slots[0].handle if getattr(action,'is_action_layered',False) else None
        m2_assign(obj,action,slot)
        bpy.context.scene.frame_set(bpy.context.scene.frame_current,subframe=bpy.context.scene.frame_subframe)
    result.update(action=action.name,source_intact=(before==m2_snapshot(source)) if source else True,
                  after=m2_curve_summary(action),assigned=params.get('assign',False),
                  preservation='Action.copy preserves channels/metadata; duplicate fake-user enabled to retain it')
    if source and not result['source_intact']:
        raise RuntimeError('Original Action snapshot changed unexpectedly')
'''


def manage_action(bridge, storage, operation_id, kind, object_name=None, action_name=None,
                  name=None, assign=False, dry_run=True):
    if kind not in {"create","duplicate","restore"}:
        raise ValueError("Unknown Action operation")
    if kind=="create" and (not isinstance(name,str) or not name.strip()):
        raise ValueError("New Action name is required")
    if kind=="restore":
        assign=True
    params=dict(kind=kind,object_name=object_name,action_name=action_name,name=name,
                assign=assign,dry_run=True)
    preview=bridge.run(BODY+MANAGE,params)
    if dry_run:
        return preview
    asset=require_mutable_asset(bridge,storage,"Action "+kind)
    checkpoint=create_checkpoint(bridge,storage,"Before Action "+kind,operation_id,"animation_"+kind+"_action")
    try:
        return dict(bridge.run(BODY+MANAGE,dict(params,dry_run=False)),checkpoint=checkpoint["manifest"],asset=asset)
    except Exception as exc:
        return {"status":"error","checkpoint":checkpoint["manifest"],
                "errors":[{"type":type(exc).__name__,"message":str(exc)}],
                "warnings":["Do not retry automatically; inspect scene and checkpoint before recovery"]}


COMPARE = r'''
obj=m2_object(params['object_name'])
prod_require_evaluable([obj],['compared object'])
first=bpy.data.actions.get(params['before_action']); second=bpy.data.actions.get(params['after_action'])
if first is None or second is None:
    raise ValueError('Both Actions must exist')
if not obj.animation_data or not obj.animation_data.action:
    raise ValueError('Comparison requires an active Action for reliable restoration')
if obj.animation_data.use_tweak_mode or any(not track.mute and len(track.strips) for track in obj.animation_data.nla_tracks):
    raise ValueError('NLA composition is not supported by Action comparison')
for action in (first,second):
    if getattr(action,'is_action_layered',False) and len(action.slots)!=1:
        raise ValueError('Comparison requires unambiguous single-slot Actions')
start=params.get('frame_start')
end=params.get('frame_end')
start=math.floor(min(first.frame_range[0],second.frame_range[0])) if start is None else start
end=math.ceil(max(first.frame_range[1],second.frame_range[1])) if end is None else end
if type(start) is not int or type(end) is not int or end<start or end-start>1000:
    raise ValueError('Comparison range must be at most 1001 integer frames')
frames=list(range(start,end+1))
names=list(obj.pose.bones.keys()) if obj.type=='ARMATURE' else ['']
original=obj.animation_data.action; slot=m2_slot(obj)
original_state=m2_capture_state(obj)
try:
    first_slot=first.slots[0].handle if getattr(first,'is_action_layered',False) else None
    second_slot=second.slots[0].handle if getattr(second,'is_action_layered',False) else None
    m2_assign(obj,first,first_slot); before=m2_sample(obj,frames,names)
    m2_assign(obj,second,second_slot); after=m2_sample(obj,frames,names)
    positions=[math.dist(a['position'],b['position']) for name in names for a,b in zip(before[name],after[name])]
    rotations=[m2_angle(a['rotation'],b['rotation']) for name in names for a,b in zip(before[name],after[name])]
    result={'before':m2_curve_summary(first),'after':m2_curve_summary(second),
        'before_snapshot':m2_snapshot(first),'after_snapshot':m2_snapshot(second),
        'metrics':{'max_position_difference_m':max(positions,default=0),
                   'average_position_difference_m':sum(positions)/len(positions) if positions else 0,
                   'max_orientation_difference_degrees':max(rotations,default=0),
                   'bone_frame_samples':len(frames)*len(names)},
        'frame_range':[start,end],'warnings':['Differences measure motion changes, not artistic quality']}
finally:
    m2_assign(obj,original,slot)
    m2_restore_state(obj,original_state)
    bpy.context.scene.frame_set(bpy.context.scene.frame_current,subframe=bpy.context.scene.frame_subframe)
'''


def compare_actions(bridge,object_name,before_action,after_action,frame_start=None,frame_end=None):
    data=bridge.run(BODY+COMPARE,dict(object_name=object_name,before_action=before_action,
        after_action=after_action,frame_start=frame_start,frame_end=frame_end))
    data['before_fingerprint']=fingerprint(data.pop('before_snapshot'))
    data['after_fingerprint']=fingerprint(data.pop('after_snapshot'))
    data['identical_content']=data['before_fingerprint']==data['after_fingerprint']
    return data
