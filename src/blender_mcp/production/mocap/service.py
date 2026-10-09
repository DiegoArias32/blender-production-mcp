"""Host-facing mocap workflows; all mutations use immutable M3/M2 transactions."""
from __future__ import annotations

from pathlib import Path

from .mapping import MAPPING_RUNTIME, validate_mapping
from ..common.m3_transaction import BASE_RUNTIME, transaction
from ..common.operations import fingerprint
from ..animation.curve_ops import analyze_fcurves
from ..animation.corrections import correction

RUNTIME = MAPPING_RUNTIME + '\n' + Path(__file__).with_name('runtime_bpy.py').read_text(encoding='utf-8')

ANALYZE = r'''
obj=m2_object(params['object_name'])
if obj.type!='ARMATURE':
    raise ValueError('Mocap analysis requires an armature')
prod_require_evaluable([obj],['mocap source'])
options=mocap_options(params.get('options',{}))
action=obj.animation_data.action if obj.animation_data else None
if not action:
    raise ValueError('Mocap armature has no active Action')
frames=mocap_frames(action,options)
names=list(obj.data.bones.keys())
samples=mocap_sample(obj,frames,names)
unit=bpy.context.scene.unit_settings.scale_length*obj.matrix_world.to_scale()[0]
roots=[bone.name for bone in obj.data.bones if bone.parent is None]
curves=action_curves(action,None)
missing=[];unexpected=[];jitter=[]
for bone in obj.pose.bones:
    prefix=bone.path_from_id()+'.'
    rotation='rotation_quaternion' if bone.rotation_mode=='QUATERNION' else 'rotation_euler'
    dimensions=4 if rotation=='rotation_quaternion' else 3
    indices={row[3].array_index for row in curves if row[3].data_path==prefix+rotation}
    if len(indices)!=dimensions:
        missing.append({'bone':bone.name,'channel':rotation,'missing_components':sorted(set(range(dimensions))-indices),
                        'interpretation':'Absent channels may intentionally use the static pose'})
    if bone.parent and any(sample[bone.name]['basis_location'].length>1e-6 for sample in samples):
        unexpected.append({'bone':bone.name,'max_translation_m':max(sample[bone.name]['basis_location'].length for sample in samples)*unit,
                           'interpretation':'Review pelvis/root semantics before deciding whether translation is unexpected'})
    velocities=[samples[i][bone.name]['head']-samples[i-1][bone.name]['head'] for i in range(1,len(samples))]
    reversals=sum(1 for a,b in zip(velocities,velocities[1:]) if a.dot(b)<0 and max(a.length,b.length)*unit<0.01)
    if reversals>=3:
        jitter.append({'bone':bone.name,'small_direction_reversals':reversals,'confidence':0.55,'interpretation':'Small alternating motion may be deliberate; no automatic deletion'})
height=(max(b.tail_local.z for b in obj.data.bones)-min(b.head_local.z for b in obj.data.bones))*unit
root_motion=[]
root_tracks={name:[] for name in roots[:16]}
scene=bpy.context.scene;old_frame,old_sub=scene.frame_current,scene.frame_subframe
try:
    for frame in frames:
        scene.frame_set(frame)
        evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        for name in root_tracks:
            root_tracks[name].append(evaluated.matrix_world@evaluated.pose.bones[name].head*scene.unit_settings.scale_length)
finally:
    scene.frame_set(old_frame,subframe=old_sub)
for name,points in root_tracks.items():
    root_motion.append({'bone':name,'displacement_m':list(points[-1]-points[0]),'distance_m':sum((b-a).length for a,b in zip(points,points[1:]))})
offset=options.get('offset',0);limit=options.get('limit',50)
if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=200:
    raise ValueError('Pagination requires offset>=0 and limit 1..200')
result={'source_armature':obj.name,'action':action.name,'bone_count':len(names),
        'hierarchy':[{'bone':b.name,'parent':b.parent.name if b.parent else None} for b in obj.data.bones][offset:offset+limit],
        'offset':offset,'limit':limit,'frame_range':[frames[0],frames[-1]],'frames_sampled':len(frames),
        'fps':bpy.context.scene.render.fps/bpy.context.scene.render.fps_base,'root_motion':root_motion[:16],
        'missing_channels':missing[:limit],'missing_channel_bones':len(missing),'translation_candidates':unexpected[:limit],
        'jitter_candidates':jitter[:limit],'height_m':height,'unusual_scale':height<0.1 or height>10,
        'likely_rest_pose':{'classification':'undetermined','confidence':0,'reason':'Rest versus T/A pose requires semantic arm assignments; see skeleton mapping'},
        'retarget_adapter_issues':mocap_guard(obj,True),'source_fingerprint':m2_digest(m2_snapshot(action)),
        'warnings':['Analysis is read-only; missing channels and jitter candidates are not automatically defects'],
        'metrics':{'bones':len(names),'channels':len(curves),'keys':sum(len(row[3].keyframe_points)for row in curves),'bone_frame_samples':len(names)*len(frames)}}
'''


def analyze(bridge, object_name, options=None):
    options = dict(options or {})
    result = bridge.run(BASE_RUNTIME + RUNTIME + ANALYZE, {'object_name': object_name, 'options': options})
    curves = analyze_fcurves(bridge, object_name, {'limit': min(options.get('limit', 50), 200)})
    result['curve_analysis'] = curves
    # Semantic inference is advisory; an explicitly reviewed map remains the mutation gate.
    from ..rig.semantics import analyze as semantic_analyze
    semantics = semantic_analyze(bridge, object_name, {})
    result['semantic_analysis'] = semantics
    assignments = semantics.get('assignments', [])
    feet = [row['bone'] for row in assignments if row.get('semantic') in ('left_foot', 'right_foot')]
    if feet:
        from ..animation.insights import run_analysis
        contact_options = {'bones': feet, 'ground_height_m': options.get('ground_height_m', 0),
                           'contact_height_m': options.get('contact_height_m', .03),
                           'sliding_tolerance_m': options.get('sliding_tolerance_m', .01)}
        contact_options.update({key: options[key] for key in ('frame_start', 'frame_end') if key in options})
        result['contacts_and_sliding'] = run_analysis(bridge, 'contacts', object_name, contact_options)
    else:
        result['contacts_and_sliding'] = {'status': 'inconclusive', 'reason': 'No semantic feet inferred; supply reviewed mapping to retarget analysis'}
    arms = [row for row in assignments if row.get('semantic') in ('left_upper_arm', 'right_upper_arm')]
    if len(arms) == 2:
        result['likely_rest_pose'] = bridge.run(BASE_RUNTIME + r'''
obj=m2_object(params['object_name']);angles=[]
for name in params['arms']:
    bone=obj.data.bones[name];direction=(bone.tail_local-bone.head_local).normalized()
    angles.append(math.degrees(math.asin(min(1,max(-1,direction.z)))))
result={'classification':'T' if max(abs(a) for a in angles)<15 else ('A' if all(-70<a<-15 for a in angles) else 'other'),
        'confidence':0.7,'upper_arm_elevation_degrees':angles,'interpretation':'Rest bone geometry heuristic, not a calibration pose assertion'}
''', {'object_name': object_name, 'arms': [row['bone'] for row in arms]})
    return result


def map_skeleton(bridge, source_armature, target_armature, mapping=None, mapping_approved=False, minimum_confidence=0.9):
    from ..rig.semantics import analyze as semantic_analyze
    source = semantic_analyze(bridge, source_armature, {})
    target = semantic_analyze(bridge, target_armature, {})
    state = bridge.run(BASE_RUNTIME + RUNTIME + r'''
source=m2_object(params['source']);target=m2_object(params['target'])
if source.type!='ARMATURE' or target.type!='ARMATURE':
    raise ValueError('Mapping requires two armatures')
result={'source_names':list(source.data.bones.keys()),'target_names':list(target.data.bones.keys()),
        'source_bones':mocap_rig_bones(source),'target_bones':mocap_rig_bones(target),
        'source_context':mocap_rig_context(source),'target_context':mocap_rig_context(target)}
''', {'source': source_armature, 'target': target_armature})
    rows = mapping
    if rows is None:
        right = {row['semantic']: row for row in target.get('assignments', []) if row.get('bone')}
        rows = []
        for row in source.get('assignments', []):
            match = right.get(row.get('semantic'))
            if match and row.get('bone'):
                rows.append({'source_bone': row['bone'], 'target_bone': match['bone'],
                             'semantic_role': row['semantic'], 'confidence': min(row['confidence'], match['confidence']),
                             'evidence': ['Source: ' + str(item) for item in row.get('evidence', [])] + ['Target: ' + str(item) for item in match.get('evidence', [])]})
        if not rows:
            return {'status': 'unsupported_or_ambiguous', 'validated': False, 'mapping': [],
                    'errors': ['Semantic analysis found no shared unambiguous roles'],
                    'source_semantics': source, 'target_semantics': target}
    # AUD-003: confidence is computed here from both rigs; row 'confidence' values are hints that can
    # only lower it.
    result = validate_mapping(rows, state['source_names'], state['target_names'], mapping_approved, minimum_confidence,
                              state['source_bones'], state['target_bones'])
    result.update(source_armature=source_armature, target_armature=target_armature,
                  source_context_fingerprint=fingerprint(state['source_context']),
                  target_context_fingerprint=fingerprint(state['target_context']),
                  source_semantics=source, target_semantics=target,
                  status='validated' if result['validated'] else 'unsupported_or_ambiguous')
    return result


def retarget(bridge, storage, operation_id, object_name=None, mode='analyze', options=None, operation=None):
    return transaction(bridge, storage, operation_id, 'mocap_retarget', RUNTIME, object_name, mode, options, operation)


def cleanup(bridge, storage, operation_id, object_name=None, mode='analyze', strategy=None,
            options=None, operation=None, apply=False):
    """One explicit measured cleanup per proposal, never an unattended destructive chain."""
    if mode == 'analyze':
        result = analyze(bridge, object_name, {})
        return {'analysis': result, 'apply': False,
                'proposed_steps': ['Review small jitter candidates against intentional motion',
                                   'Reduce proven redundant scalar keys with native-unit tolerances',
                                   'Correct quaternion sign representation only after orientation checks',
                                   'Review explicit contacts/root separately through correction proposals'],
                'warnings': ['No edits applied; choose one strategy and review its immutable proposal']}
    if mode == 'apply' and apply is not True:
        raise ValueError('mocap_cleanup requires explicit apply=true for apply mode')
    if operation:
        if strategy not in (None, 'curves', 'quaternion', 'root_motion', 'foot_sliding', 'contact_lock'):
            raise ValueError('Unsupported cleanup strategy')
        from ..common.operations import load_proposal
        _, saved = load_proposal(storage, operation)
        kind = saved['kind']
        if kind not in ('cleanup', 'quaternion', 'root_motion', 'foot_sliding', 'advanced_foot_sliding'):
            raise ValueError('Operation is not a supported mocap cleanup')
        requested = {'curves': 'cleanup', 'quaternion': 'quaternion', 'root_motion': 'root_motion', 'foot_sliding': 'foot_sliding', 'contact_lock': 'advanced_foot_sliding'}.get(strategy)
        if strategy is not None and requested != kind:
            raise ValueError('Cleanup strategy conflicts with the immutable proposal')
    else:
        kinds = {'curves': 'cleanup', 'quaternion': 'quaternion', 'root_motion': 'root_motion', 'foot_sliding': 'foot_sliding', 'contact_lock': 'advanced_foot_sliding'}
        if strategy not in kinds:
            raise ValueError('Choose curves/quaternion/root_motion/foot_sliding/contact_lock and explicit strategy options')
        kind = kinds[strategy]
    if kind == 'advanced_foot_sliding':
        from ..contacts.service import fix_contacts
        result = fix_contacts(bridge, storage, operation_id, object_name, mode, options, operation)
    else:
        result = correction(bridge, storage, operation_id, kind, object_name, mode, options, operation)
    result['mocap_cleanup'] = {'strategy': kind, 'apply': mode == 'apply',
                             'safety': 'One reviewed operation; original preserved and deviation measured by correction lifecycle'}
    return result


def register(tool, resources):
    @tool('production.mocap_analyze', read_only=True)
    def mocap_analyze(object_name: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Read-only mocap hierarchy, root motion, scale, channel/jitter and rest-pose analysis."""
        return analyze(resources()[0], object_name, options)

    @tool('production.mocap_map_skeleton', read_only=True)
    def mocap_map(source_armature: str, target_armature: str, mapping: list[dict] | None = None,
                  mapping_approved: bool = False, minimum_confidence: float = 0.9, *, _operation_id='') -> dict:
        """Combine semantic evidence; validate one-to-one reviewed required mapping before retarget."""
        return map_skeleton(resources()[0], source_armature, target_armature, mapping, mapping_approved, minimum_confidence)

    @tool('production.mocap_preview_retarget')
    def mocap_preview(object_name: str | None = None, options: dict | None = None,
                      operation: str | None = None, *, _operation_id='') -> dict:
        """Without operation propose; with immutable operation bake checkpointed new preview Action and restore target assignment."""
        bridge, storage = resources()
        return retarget(bridge, storage, _operation_id, object_name, 'preview' if operation else 'propose', options, operation)

    @tool('production.mocap_retarget')
    def mocap_retarget(object_name: str | None = None, mode: str = 'analyze', options: dict | None = None,
                       operation: str | None = None, *, _operation_id='') -> dict:
        """Reviewed mapping → immutable proposal → checkpoint → new Action → dense correspondence/contact/limb/root QA."""
        bridge, storage = resources()
        return retarget(bridge, storage, _operation_id, object_name, mode, options, operation)

    @tool('production.mocap_cleanup')
    def mocap_cleanup(object_name: str | None = None, mode: str = 'analyze', strategy: str | None = None,
                      options: dict | None = None, operation: str | None = None, apply: bool = False, *, _operation_id='') -> dict:
        """Analyze/propose one safe cleanup at a time; apply=false default; exact source and measured motion preservation."""
        bridge, storage = resources()
        return cleanup(bridge, storage, _operation_id, object_name, mode, strategy, options, operation, apply)
