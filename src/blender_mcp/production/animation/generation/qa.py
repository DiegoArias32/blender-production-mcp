"""M6 calls the established analyzers; technical QA never implies artistic acceptance."""
from __future__ import annotations

import time
import math
from .candidate import CandidateBridge
from ...common.m3_transaction import BASE_RUNTIME
from ..service import analyze, detect_foot_sliding, detect_quaternion_flips
from ..insights import run_analysis
from ..bezier import analyze_bezier
from ...contacts.service import analyze_limbs
from ...deformation import run as deformation_run


ROOT_LOOP_READ = r'''
from mathutils import Matrix, Quaternion, Vector
def m6_qa_expected_root_rotation(world_basis,yaw):
    # A declared -Y forward basis may be reflected. Conjugate matrices before
    # converting the proper resulting rotation, rather than discarding reflection.
    rotation=world_basis@Quaternion(Vector((0,0,1)),math.radians(yaw)).to_matrix()@world_basis.inverted()
    return rotation.to_quaternion().normalized()
obj=m2_object(params['object_name'])
brief=params['brief'];mapping=params['mapping'];poses=params['pose_plan']['poses']
start=brief['frame_start'];end=brief['frame_end'];fps=brief['fps']
if end<=start or end-start>600:
    raise ValueError('ROOT_LOOP_WINDOW_INVALID: expected 1..600 frame intervals')
unit=bpy.context.scene.unit_settings.scale_length
if unit<=0 or abs(bpy.context.scene.render.fps/bpy.context.scene.render.fps_base-fps)>1e-6:
    raise ValueError('ROOT_LOOP_CONTEXT_CHANGED: units/FPS differ from plan')
basis=Matrix(params['basis']);object_rotation=obj.matrix_world.to_quaternion().normalized()
world_basis=object_rotation.to_matrix()@basis
roles=[role for role in ('pelvis','chest','head','hand_l','hand_r','foot_l','foot_r') if mapping.get(role)]
if len(roles)!=7:
    raise ValueError('ROOT_LOOP_MAPPING_INCOMPLETE')
frames=list(range(start,end+1));root_points=[];root_rotations=[];relative={role:[] for role in roles};rotations={role:[] for role in roles}
current=obj.animation_data.action if obj.animation_data else None
curves=list(action_curves(current,m2_slot(obj))) if current else []
points=[point for s,l,t,curve in curves for point in curve.keyframe_points]
stepped=bool(current and points and current.get('production_generation_stage') in ('BLOCKING','POSE') and all(point.interpolation=='CONSTANT' for point in points))
captured=m3_capture(obj)
try:
    for frame in frames:
        m3_set_frame(frame)
        evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        root_world=evaluated.matrix_world@evaluated.pose.bones[mapping['root']].matrix
        root_point=root_world.translation*unit;root_rotation=root_world.to_quaternion().normalized()
        root_points.append(root_point);root_rotations.append(root_rotation)
        for role in roles:
            world=evaluated.matrix_world@evaluated.pose.bones[mapping[role]].matrix
            relative[role].append(root_rotation.inverted()@(world.translation*unit-root_point))
            rotations[role].append((root_rotation.inverted()@world.to_quaternion()).normalized())
finally:
    m3_restore(obj,captured)
expected=world_basis@(Vector(poses[-1]['semantic']['root_translation_m'])-Vector(poses[0]['semantic']['root_translation_m']))
actual=root_points[-1]-root_points[0]
yaw=poses[-1]['semantic'].get('root_yaw_deg',0)-poses[0]['semantic'].get('root_yaw_deg',0)
expected_rotation=m6_qa_expected_root_rotation(world_basis,yaw)
actual_rotation=root_rotations[-1]@root_rotations[0].inverted()
root_steps=[(b-a).length for a,b in zip(root_points,root_points[1:])]
semantic_vertical=[p['semantic']['root_translation_m'][2] for p in poses]
vertical=[(world_basis.transposed()@(point-root_points[0])).z for point in root_points]
first_vertical=poses[0]['semantic']['root_translation_m'][2]
minimum_vertical=min(semantic_vertical)-first_vertical;maximum_vertical=max(semantic_vertical)-first_vertical
vertical_error=max(max(0,minimum_vertical-value,value-maximum_vertical) for value in vertical)
def m6_qa_angular_velocity(first,second):
    delta=(second@first.inverted()).normalized()
    if delta.w<0: delta=-delta
    axis,angle=delta.to_axis_angle()
    return axis*math.degrees(angle)*fps
loop_rows=[]
for role in roles:
    track=relative[role];qs=rotations[role]
    first_velocity=(track[1]-track[0])*fps;last_velocity=(track[-1]-track[-2])*fps
    first_angular=m6_qa_angular_velocity(qs[0],qs[1]);last_angular=m6_qa_angular_velocity(qs[-2],qs[-1])
    loop_rows.append({'role':role,'position_error_m':(track[-1]-track[0]).length,
        'orientation_error_deg':m2_angle(qs[-1],qs[0]),'velocity_error_m_s':(last_velocity-first_velocity).length,
        'angular_velocity_error_deg_s':(last_angular-first_angular).length})
result={'status':'measured','errors':[],'metrics':{'sampled_frames':len(frames),
    'intentional_stepped_blocking':stepped,
    'root_mode':brief['root_mode'],'root_expected_displacement_m':list(expected),'root_actual_displacement_m':list(actual),
    'root_displacement_error_m':(actual-expected).length,'root_orientation_error_deg':m2_angle(actual_rotation,expected_rotation),
    'root_max_translation_m':max((point-root_points[0]).length for point in root_points),
    'root_max_step_m':max(root_steps,default=0.),'root_unexpected_vertical_m':vertical_error,
    'root_distance_m':sum(root_steps),'root_average_speed_m_s':sum(root_steps)/((end-start)/fps),
    'loop_required':brief['type'] in ('IDLE','LOCOMOTION'),
    'loop_boundary_continuity':'ADVISORY_STEPPED_BLOCKING' if stepped else 'REQUIRED_CONTINUOUS',
    'loop_position_error_m':max(row['position_error_m'] for row in loop_rows),
    'loop_orientation_error_deg':max(row['orientation_error_deg'] for row in loop_rows),
    'loop_velocity_error_m_s':max(row['velocity_error_m_s'] for row in loop_rows),
    'loop_angular_velocity_error_deg_s':max(row['angular_velocity_error_deg_s'] for row in loop_rows)},
    'loop_roles':loop_rows,'scope':'Integer-frame evaluated root and root-relative body endpoints and one-frame boundary velocities; no artistic score'}
'''


def _thresholds(options, ground):
    values = dict(ground_height_m=ground, sliding_tolerance_m=options.get('sliding_tolerance_m', .01),
                  contact_height_m=options.get('contact_height_m', .03), contact_speed_m_s=options.get('contact_speed_m_s', .25),
                  root_position_tolerance_m=options.get('root_position_tolerance_m', .01),
                  root_angle_tolerance_deg=options.get('root_angle_tolerance_deg', 1.),
                  root_max_step_m=options.get('root_max_step_m', .25), root_vertical_tolerance_m=options.get('root_vertical_tolerance_m', .05),
                  loop_position_tolerance_m=options.get('loop_position_tolerance_m', .01),
                  loop_angle_tolerance_deg=options.get('loop_angle_tolerance_deg', 1.),
                  loop_velocity_tolerance_m_s=options.get('loop_velocity_tolerance_m_s', .10),
                  loop_angular_velocity_tolerance_deg_s=options.get('loop_angular_velocity_tolerance_deg_s', 10.))
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (key != 'ground_height_m' and value < 0):
            raise ValueError('INVALID_QA_THRESHOLD: ' + key + ' must be finite and nonnegative')
    return values


def _completed(report):
    return isinstance(report, dict) and report.get('status') not in {'error', 'fail', 'failed', 'unsupported'} and not report.get('errors')


def _within(value, limit):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= limit


def _zero_count(value):
    return type(value) is int and value == 0


def evaluate_gates(reports, thresholds):
    """Pure conservative gate evaluation, also used by negative unit tests."""
    slip = reports.get('animation_analyze_contacts', {}).get('metrics', {})
    limb = reports.get('animation_detect_limb_pops', {}).get('metrics', {})
    deform = reports.get('animation_deformation_qa', {})
    deform_metrics = deform.get('metrics', {})
    quat = reports.get('animation_detect_quaternion_flips', {}).get('metrics', {})
    root = reports.get('generation_root_loop', {}).get('metrics', {})
    loop_required = root.get('loop_required')
    stepped = root.get('intentional_stepped_blocking') is True
    return {'analyzers_completed': bool(reports) and all(_completed(report) for report in reports.values()),
            'contacts_measured': slip.get('contact_count', 0) > 0 and _within(slip.get('max_sliding_m'), math.inf),
            'sliding_within_tolerance': _within(slip.get('max_sliding_m'), thresholds['sliding_tolerance_m']),
            'no_limb_pops': stepped or _zero_count(limb.get('pop_count')),
            'no_unintended_stretch': _within(limb.get('max_length_ratio'), 1.02),
            'deformation_measured': (deform_metrics.get('meshes', 0) > 0 and deform_metrics.get('evaluations', 0) > 0
                                     and deform_metrics.get('sampled_frames', 0) > 0 and deform_metrics.get('issue_count') == 0
                                     and deform.get('state_restored') is True and _completed(deform)),
            'no_quaternion_error': (all(_zero_count(quat.get(key)) for key in ('invalid_quaternion_count', 'incomplete_group_count',
                                                                             'sign_discontinuity_count'))
                                    and (stepped or _zero_count(quat.get('orientation_jump_count')))),
            'root_mode_coherent': root.get('root_mode') in {'IN_PLACE', 'ROOT_MOTION'},
            'root_displacement_matches_intent': _within(root.get('root_displacement_error_m'), thresholds['root_position_tolerance_m']),
            'root_rotation_matches_intent': _within(root.get('root_orientation_error_deg'), thresholds['root_angle_tolerance_deg']),
            'in_place_translation': (root.get('root_mode') == 'ROOT_MOTION' or
                                      (root.get('root_mode') == 'IN_PLACE' and _within(root.get('root_max_translation_m'), thresholds['root_position_tolerance_m']))),
            'no_root_teleport': stepped or _within(root.get('root_max_step_m'), thresholds['root_max_step_m']),
            'no_unexpected_root_vertical': _within(root.get('root_unexpected_vertical_m'), thresholds['root_vertical_tolerance_m']),
            'loop_endpoint_position': loop_required is False or (loop_required is True and _within(root.get('loop_position_error_m'), thresholds['loop_position_tolerance_m'])),
            'loop_endpoint_rotation': loop_required is False or (loop_required is True and _within(root.get('loop_orientation_error_deg'), thresholds['loop_angle_tolerance_deg'])),
            'loop_boundary_velocity': loop_required is False or (loop_required is True and
                _within(root.get('loop_velocity_error_m_s'), math.inf if stepped else thresholds['loop_velocity_tolerance_m_s'])),
            'loop_boundary_angular_velocity': loop_required is False or (loop_required is True and
                _within(root.get('loop_angular_velocity_error_deg_s'), math.inf if stepped else thresholds['loop_angular_velocity_tolerance_deg_s']))}


def technical(bridge, plan, action_name, options=None):
    opts = options or {}
    began = time.perf_counter()
    brief, mapping = plan['brief'], plan['mapping']
    obj = plan['object_name']
    candidate = CandidateBridge(bridge, obj, action_name)
    start, end = brief['frame_start'], brief['frame_end']
    feet = [mapping.get('foot_l'), mapping.get('foot_r')]
    feet = [n for n in feet if n]
    window = {'frame_start': start, 'frame_end': end}
    ground = opts.get('ground_height_m', plan.get('options', {}).get('ground_height_m', 0.0))
    thresholds = _thresholds(opts, ground)
    contacts = {key: thresholds[key] for key in ('ground_height_m', 'sliding_tolerance_m', 'contact_height_m', 'contact_speed_m_s')}
    chains = []
    for suffix in ('l', 'r'):
        for kind in ('leg', 'arm'):
            values = [mapping.get('upper_'+kind+'_'+suffix), mapping.get('lower_'+kind+'_'+suffix),
                      mapping.get(('foot' if kind == 'leg' else 'hand')+'_'+suffix)]
            if all(values):
                chains.append(values)
    reports, errors = {}, []
    requests = {
        'animation_analyze': lambda: analyze(candidate, obj, action_name, limit=12),
        'animation_analyze_contacts': lambda: run_analysis(candidate, 'contacts', obj, dict(window, bones=feet, **contacts)),
        'animation_detect_foot_sliding': lambda: detect_foot_sliding(candidate, obj, feet, **window, **contacts),
        'animation_detect_limb_pops': lambda: analyze_limbs(candidate, obj, dict(window, chains=chains)),
        'animation_detect_limb_stretch': lambda: analyze_limbs(candidate, obj, dict(window, chains=chains)),
        'animation_analyze_bezier': lambda: analyze_bezier(candidate, obj, {'limit': 12}),
        'animation_analyze_root_motion': lambda: run_analysis(candidate, 'root_motion', obj, dict(window, root_bone=mapping.get('root'))),
        'animation_analyze_balance': lambda: run_analysis(candidate, 'balance', obj, dict(window, pelvis_bone=mapping.get('pelvis'), support_bones=feet, **contacts)),
        'animation_analyze_motion_quality': lambda: run_analysis(candidate, 'motion_quality', obj, dict(window, bones=feet, root_bone=mapping.get('root'))),
        'animation_deformation_qa': lambda: deformation_run(candidate, obj, 'animation', dict(window, meshes=opts.get('meshes', []), max_samples=12)),
        'animation_detect_quaternion_flips': lambda: detect_quaternion_flips(candidate, obj, action_name, limit=12),
        'generation_root_loop': lambda: candidate.run(BASE_RUNTIME + ROOT_LOOP_READ,
                {'object_name': obj, 'brief': brief, 'mapping': mapping, 'pose_plan': plan['pose_plan'],
                 'basis': plan['compatibility']['body_basis_armature']}),
    }
    for name, run in requests.items():
        t = time.perf_counter()
        try:
            reports[name] = run()
            reports[name]['elapsed_ms_m6'] = round((time.perf_counter()-t)*1000, 2)
            if not _completed(reports[name]):
                errors.append({'tool': name, 'message': 'Analyzer returned unsuccessful status or errors',
                               'details': reports[name].get('errors', []), 'status': reports[name].get('status')})
        except Exception as exc:
            reports[name] = {'status': 'error', 'errors': [str(exc)]}
            errors.append({'tool': name, 'message': str(exc)})
    # Concrete measurements gate pass, including errors returned without exceptions.
    slip = reports['animation_analyze_contacts'].get('metrics', {})
    limb = reports['animation_detect_limb_pops'].get('metrics', {})
    root = reports['generation_root_loop'].get('metrics', {})
    gates = evaluate_gates(reports, thresholds)
    return {'status': 'pass' if all(gates.values()) else 'fail', 'passed': all(gates.values()),
            'gates': gates, 'reports': reports, 'errors': errors, 'thresholds': thresholds,
            'advisory_gates': (['loop_boundary_velocity', 'loop_boundary_angular_velocity']
                               if root.get('intentional_stepped_blocking') and root.get('loop_required') else []),
            'metrics': {'elapsed_ms': round((time.perf_counter()-began)*1000, 2),
                        'sliding_m': slip.get('max_sliding_m'), 'pop_count': limb.get('pop_count'),
                        'max_length_ratio': limb.get('max_length_ratio'), 'root_loop': root},
            'visual_verification': 'required',
            'warnings': ['Contact and balance are geometric proxies; temporal artistic review remains required.',
                         'Root/loop tolerances are explicit geometric QA defaults, configurable; no artistic score.',
                         'Loop boundary velocity compares one-frame root-relative linear/angular velocity; sampled continuity is not a continuous-time certificate.']
                        + (['CONSTANT BLOCKING/POSE: authored stepped limb/orientation/root jumps and loop boundary velocities are advisory; loop endpoint pose remains required and continuous stages must pass all jump/continuity gates.']
                           if root.get('intentional_stepped_blocking') else [])}
