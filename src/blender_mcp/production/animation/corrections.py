"""Checkpointed, duplicate-only correction lifecycle over the upstream bridge."""
from __future__ import annotations

import inspect
import math

from .scripts import ACTION_HELPERS
from .algorithms import contact_report
from .edit_runtime import ACTION_RUNTIME
from .transform_ops import TRANSFORM_RUNTIME
from ..checkpoint import create_checkpoint
from ..common.asset_safety import require_mutable_asset
from ..common.ledger import guarded_cycle, is_uncertain
from ..common.operations import fingerprint, save_proposal, load_proposal, record_event, latest_candidate
from ..common.digest import DIGEST_RUNTIME, runtime_digest

KINDS = {"foot_sliding", "quaternion", "cleanup", "root_motion", "retime"}
MODES = {"analyze", "propose", "preview", "apply", "verify", "revert"}


def runtime(kind):
    body = ACTION_HELPERS + ACTION_RUNTIME + DIGEST_RUNTIME + inspect.getsource(contact_report) + TRANSFORM_RUNTIME
    if kind in {"quaternion", "cleanup"}:
        from .curve_runtime import CURVE_RUNTIME
        return body + CURVE_RUNTIME
    return body + '''
def m2_curve_proposal(obj, action, kind, options):
    raise ValueError("Curve callback not selected")
def m2_curve_apply(obj, action, kind, proposal, options):
    raise ValueError("Curve callback not selected")
def m2_curve_verify(obj, before_action, after_action, kind, proposal, options):
    raise ValueError("Curve callback not selected")
'''


READ = r'''
obj, action = m2_active(params['object_name'])
m2_guard(obj,action)
options = params['options']
kind = params['kind']
proposal = m2_curve_proposal(obj,action,kind,options) if kind in {'quaternion','cleanup'} else m2_transform_proposal(obj,action,kind,options)
result = {'source_action':action.name,'source_snapshot':m2_snapshot(action),'source_slot':m2_slot(obj),
          'source_state':m2_capture_state(obj),'context':m2_context(obj),'proposal':proposal,'before':m2_curve_summary(action)}
'''

EXECUTE = r'''
obj, original = m2_active(params['object_name'])
transient = prod_transient_capture()
m2_guard(obj,original)
if original.name != params['source_action'] or m2_digest(m2_snapshot(original)) != params['source_fingerprint']:
    raise ValueError('STALE_PROPOSAL: source Action changed after proposal; analyze again')
if m2_digest(m2_context(obj)) != params['context_fingerprint']:
    raise ValueError('STALE_PROPOSAL: scene/rig context changed after proposal; analyze again')
original_slot = m2_slot(obj)
original_state=m2_capture_state(obj)
working = original.copy()
working.name = original.name + '__M2_' + params['suffix']
working.use_fake_user = True
verified = False
try:
    m2_assign(obj,working,original_slot)
    kind,proposal,options = params['kind'],params['proposal'],params['options']
    if kind in {'quaternion','cleanup'}:
        changes = m2_curve_apply(obj,working,kind,proposal,options)
        verification = m2_curve_verify(obj,original,working,kind,proposal,options)
    else:
        changes = m2_transform_apply(obj,working,kind,proposal,options)
        verification = m2_transform_verify(obj,original,working,kind,proposal,options)
    source_intact = m2_digest(m2_snapshot(original)) == params['source_fingerprint']
    verified = bool(verification['passed']) and source_intact
    result = {'working_action':working.name, 'source_action':original.name,
        'before':params['before'], 'after':m2_curve_summary(working),
        'working_snapshot':m2_snapshot(working), 'source_intact':source_intact,
        'verification':verification, 'metrics':dict(verification.get('metrics',{}),**changes),
        'numeric_verification':'pass' if verified else 'fail',
        'visual_verification':'required',
        'warnings':proposal.get('warnings',[])+verification.get('warnings',[]),
        'errors':verification.get('errors',[]),
        'status':'warning' if verified else 'error',
        'active_action':working.name if verified and params['mode']=='apply' else original.name}
finally:
    if not verified or params['mode'] != 'apply':
        m2_assign(obj,original,original_slot)
    # Static channels return to their pre-operation values; animated ones re-evaluate from the
    # active Action, so no scratch value written while solving survives.
    m2_restore_state(obj,original_state)
    prod_transient_restore(transient)
changed = prod_transient_diff(transient, prod_transient_capture())
result['transient_context_restored'] = not changed
if changed:
    result['status'] = 'error'; result['numeric_verification'] = 'fail'
    result['errors'] = result['errors'] + ['TRANSIENT_CONTEXT_CHANGED: ' + ', '.join(changed)]
'''

CHECK = r'''
obj = m2_object(params['object_name'])
transient = prod_transient_capture()
source = bpy.data.actions.get(params['source_action'])
working = bpy.data.actions.get(params['working_action'])
if source is None or working is None:
    raise ValueError('CANDIDATE_MISSING: source/candidate Action missing; use checkpoint recovery')
source_intact = m2_digest(m2_snapshot(source))==params['source_fingerprint']
candidate_intact = m2_digest(m2_snapshot(working))==params['working_fingerprint']
active_before=obj.animation_data.action
slot_before=m2_slot(obj)
state_before=m2_capture_state(obj)
written = m2_written_static_paths(source, working)
try:
    # Sign the context against the CURRENT user state (AUD-007): only static channels the
    # correction itself keyed go back to proposal-time values before fingerprinting.
    m2_assign(obj,source,params['source_slot'])
    m2_restore_paths(obj,params['source_state'],written)
    m2_guard(obj,source)
    context_intact=m2_digest(m2_context(obj))==params['context_fingerprint']
finally:
    m2_assign(obj,active_before,slot_before)
    m2_restore_state(obj,state_before)
candidate_intact=candidate_intact and context_intact
result = {'source_intact':source_intact,'candidate_intact':candidate_intact,
          'active_action':obj.animation_data.action.name if obj.animation_data and obj.animation_data.action else None,
          'numeric_verification':params['numeric_verification'] if source_intact and candidate_intact else 'stale',
          'visual_verification':'required',
          'metrics':params['metrics'], 'warnings':['Visual/artistic acceptance is separate from numerical verification'],
          'status':'warning' if source_intact and candidate_intact else 'error',
          'errors':[] if source_intact and candidate_intact else ['STALE_PROPOSAL: Action content or context changed since correction verification']}
if params['mode']=='verify' and source_intact and candidate_intact:
    try:
        m2_assign(obj,working,params['source_slot'])
        if params['kind'] in {'quaternion','cleanup'}:
            measured=m2_curve_verify(obj,source,working,params['kind'],params['proposal'],params['options'])
        else:
            measured=m2_transform_verify(obj,source,working,params['kind'],params['proposal'],params['options'])
        result.update(verification=measured,metrics=measured['metrics'],
            numeric_verification='pass' if measured['passed'] else 'fail',
            status='warning' if measured['passed'] else 'error',errors=measured.get('errors',[]))
    finally:
        m2_assign(obj,active_before,slot_before)
        m2_restore_state(obj,state_before)
if params['mode']=='revert':
    # Reassigning the preserved source is safe while the source itself is unchanged; later user
    # changes (context, candidate edits) are kept and reported, never undone (AUD-007).
    if not source_intact:
        raise ValueError('STALE_PROPOSAL: the source Action changed after the correction; automatic reassignment refused')
    if obj.animation_data.action not in (source,working):
        raise ValueError('UNRELATED_ACTION_ACTIVE: another Action is active; automatic reassignment refused')
    m2_assign(obj,source,params['source_slot'])
    # Revert restores animation data only: residue of the correction's static channels goes back,
    # the frame and any unrelated user state are kept.
    m2_restore_paths(obj,params['source_state'],written)
    bpy.context.scene.frame_set(bpy.context.scene.frame_current,subframe=bpy.context.scene.frame_subframe)
    kept = []
    if not context_intact:
        kept.append('USER_CONTEXT_KEPT: scene/rig state changed after the proposal; those changes were kept')
    if not m2_digest(m2_snapshot(working))==params['working_fingerprint']:
        kept.append('CANDIDATE_EDITS_KEPT: the candidate Action changed after apply; it is preserved as edited')
    result.update(active_action=source.name,reverted=True,candidate_preserved=working.name,
                  restored_static_channels=sorted(written),status='warning',errors=[],
                  numeric_verification=params['numeric_verification'],context_changed_since_proposal=not context_intact,
                  warnings=result['warnings']+kept)
prod_transient_restore(transient)
changed = prod_transient_diff(transient, prod_transient_capture())
result['transient_context_restored'] = not changed
if changed:
    result['status'] = 'error'
    result['errors'] = result['errors'] + ['TRANSIENT_CONTEXT_CHANGED: ' + ', '.join(changed)]
'''


def _validate_options(options):
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Options must contain finite numbers")
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        if isinstance(value, list):
            for item in value:
                finite(item)
    finite(options)
    for key,value in options.items():
        if ("tolerance" in key or key.startswith("max_") or key in {"contact_height_m","contact_speed_m_s"}) and isinstance(value,(int,float)):
            if isinstance(value,bool) or value < 0:
                raise ValueError(key + " must be a nonnegative finite number")


def _validate_transform_options(kind,options):
    common={"frame_start","frame_end","position_tolerance_m"}
    allowed={
        "foot_sliding":common|{"controls","confirm_contacts","blend_frames","ground_height_m",
            "contact_height_m","contact_speed_m_s","sliding_tolerance_m","min_contact_frames",
            "max_joint_step_increase_degrees","stretch_tolerance_m","ik_target_tolerance_m"},
        "root_motion":common|{"root_bone","strategy"},
        "retime":common|{"scale_factor","target_duration_seconds","angular_tolerance_degrees",
            "ground_height_m","contact_height_m","contact_speed_m_s","sliding_tolerance_m"}}
    if kind not in allowed:
        return
    unknown=set(options)-allowed[kind]
    if unknown:
        raise ValueError("Unknown correction options: "+", ".join(sorted(unknown)))
    if "confirm_contacts" in options and type(options["confirm_contacts"]) is not bool:
        raise ValueError("confirm_contacts must be an explicit boolean")
    if "controls" in options and (not isinstance(options["controls"],list) or any(not isinstance(v,str) or not v for v in options["controls"])):
        raise ValueError("controls must contain exact nonempty bone names")


def _public(data):
    # Full exact snapshots and patches stay in local records, never MCP output.
    result = {key:value for key,value in data.items()
              if key not in {"source_snapshot", "working_snapshot", "source_state", "context", "proposal_sha256"}}
    if "proposal" in result:
        proposal = result["proposal"]
        result["proposal"] = {key:value for key,value in proposal.items()
             if key not in {"patches", "offsets", "frames"}}
        result["proposal"]["patch_count"] = len(proposal.get("patches", []))
        if proposal.get("frames"):
            result["proposal"]["frame_range"] = [proposal["frames"][0],proposal["frames"][-1]]
    return result


def _record_result(path,mode,data):
    try:
        data['event']=record_event(path,mode,data)
        data['event_recorded']=True
    except OSError as exc:
        data['event_recorded']=False
        data.setdefault('warnings',[]).append('Operation completed but result log could not be saved: '+str(exc))
        if data.get('status')!='error':
            data['status']='warning'
    return data


def _runtime_params(data):
    compact={key:value for key,value in data.items() if key not in {'source_snapshot','working_snapshot','context'}}
    if 'context' in data:
        compact['context_fingerprint']=runtime_digest(data['context'])
    if 'working_snapshot' in data:
        compact['working_fingerprint']=runtime_digest(data['working_snapshot'])
    return compact


def correction(bridge, storage, operation_id, kind, object_name=None, mode="analyze", options=None, operation=None):
    """Apply requires an immutable proposal reference. Source Action is never edited."""
    if kind not in KINDS or mode not in MODES:
        raise ValueError("Unsupported correction kind or mode")
    options = {} if options is None else options
    _validate_options(options)
    _validate_transform_options(kind,options)
    body = runtime(kind)
    if mode in {"analyze", "propose"}:
        if not object_name or operation:
            raise ValueError("Analyze/propose requires object_name and no prior operation")
        data = bridge.run(body + READ, {"object_name":object_name,"options":options,"kind":kind})
        data.update(kind=kind, object_name=object_name, options=options,
                    source_fingerprint=runtime_digest(data["source_snapshot"]),
                    warnings=data["proposal"].get("warnings", []))
        from ..common.m3_transaction import payload_capacity
        payload_capacity(data["proposal"], body, EXECUTE, CHECK, _runtime_params(data))
        data["warnings"] = data["proposal"].get("warnings", [])
        if mode=="propose":
            path = save_proposal(storage,operation_id,data)
            data.update(operation=path,correction_id=operation_id)
        return dict(_public(data), mode=mode, status="warning", numeric_verification="not_applied",
                    visual_verification="not_applied")
    if not operation or options:
        raise ValueError("Preview/apply/verify/revert require operation reference; options are frozen in the proposal")
    path, saved = load_proposal(storage,operation)
    if saved["kind"] != kind or object_name and object_name != saved["object_name"]:
        raise ValueError("Operation target/type mismatch")
    if not saved["proposal"].get("supported",False):
        raise ValueError("Proposal requires manual/alternative correction; it cannot be applied")
    common = {"operation":str(path),"correction_id":saved["operation_id"],"mode":mode,
              "target":{"object":saved["object_name"],"source_action":saved["source_action"]}}
    if mode=="verify":
        previous = latest_candidate(path)
        from ..common.m3_transaction import check_fields
        data = bridge.run(body+CHECK,_runtime_params({**saved,**check_fields(previous),"mode":mode}))
        data.update(**common,checkpoint=previous.get("checkpoint"),asset=None)
        return _public(_record_result(path,mode,data))
    previous = latest_candidate(path) if mode=="revert" else {}

    def execute(suffix,note):
        if mode!="revert":
            # Preflight staleness before saving checkpoint or allocating a duplicate.
            check = bridge.run(body + r'''
obj,action=m2_active(params['object_name'])
m2_guard(obj,action)
result={'matches':action.name==params['source_action'] and m2_digest(m2_snapshot(action))==params['source_fingerprint'] and m2_digest(m2_context(obj))==params['context_fingerprint']}
''',_runtime_params(saved))
            if not check["matches"]:
                raise ValueError("STALE_PROPOSAL: proposal is stale; source/rig changed. Analyze again")
        # AUD-014: persistent mutation only on a registered working copy / fixture / unsaved session.
        asset = require_mutable_asset(bridge,storage,kind+" "+mode)
        if mode=="revert":
            checkpoint = create_checkpoint(bridge,storage,"Before Action assignment revert",operation_id,kind+"/revert")["manifest"]
        else:
            checkpoint = create_checkpoint(bridge,storage,"Before "+kind+" "+mode,operation_id,kind)["manifest"]
        note(checkpoint=checkpoint)
        record_event(path,'intent',dict(common,checkpoint=checkpoint))
        try:
            if mode=="revert":
                from ..common.m3_transaction import check_fields
                data = bridge.run(body+CHECK,_runtime_params({**saved,**check_fields(previous),"mode":mode}))
                if not data.get("metrics"):
                    data["metrics"] = previous.get("metrics",{})
            else:
                data = bridge.run(body+EXECUTE,_runtime_params({**saved,"mode":mode,"suffix":suffix}))
            data.update(**common,checkpoint=checkpoint,asset=asset)
        except Exception as exc:
            if mode=="revert" and not is_uncertain(exc):
                raise  # Blender answered: CHECK refused before reassigning anything; a retry may run.
            data = dict(common,status="error",checkpoint=checkpoint,asset=asset,
                        errors=[{"type":type(exc).__name__,"message":str(exc)}],
                        warnings=["Do not retry automatically: inspect Blender and checkpoint; interrupted work may still be running. "
                                  "Retrying this same proposal reconciles instead of executing twice (AUD-010)."],
                        numeric_verification="unverified",state_restored="unverified")
        return _public(_record_result(path,mode,data))

    def measure(candidate,working_fingerprint,checkpoint):
        # The response of the original EXECUTE was lost: re-measure the candidate it left behind.
        data = bridge.run(body+CHECK,_runtime_params({**saved,"working_action":candidate,"working_fingerprint":working_fingerprint,
                                                      "numeric_verification":"unverified","metrics":{},"mode":"verify"}))
        record_event(path,mode,dict(common,working_action=candidate,working_fingerprint=working_fingerprint,
            source_action=saved["source_action"],checkpoint=checkpoint,reconciled=True,
            numeric_verification=data.get("numeric_verification"),metrics=data.get("metrics",{})))
        data.update(**common,checkpoint=checkpoint,working_action=candidate,asset=None)
        return _public(data)

    return guarded_cycle(bridge,storage,tool=kind,correction_id=saved["operation_id"],mode=mode,
        request={"operation":str(path),"kind":kind,"mode":mode},body=body,object_name=saved["object_name"],
        source_action=saved["source_action"],candidate_prefix=saved["source_action"]+"__M2_",
        suffix_length=10,previous_candidate=previous.get("working_action"),execute=execute,measure=measure)
