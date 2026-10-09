"""Compact timing, spacing, arc and mechanics observations from evaluated candidates."""
from __future__ import annotations

import math
from statistics import mean

from .candidate import CandidateBridge
from ...common.m3_transaction import BASE_RUNTIME

SAMPLE = r'''
obj=m2_object(params['object_name'])
prod_require_evaluable([obj],['generation measurements'])
names=params['bones'];frames=params['frames']
if len(names)>12 or len(frames)>601: raise ValueError('CAPACITY_EXCEEDED: measurement sample budget')
unit=bpy.context.scene.unit_settings.scale_length
state=m3_capture(obj)
tracks={n:[] for n in names}
try:
    for frame in frames:
        m3_set_frame(frame)
        evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        for name in names:
            tracks[name].append(list(evaluated.matrix_world@evaluated.pose.bones[name].head*unit))
    result={'frames':frames,'tracks':tracks,'fps':bpy.context.scene.render.fps/bpy.context.scene.render.fps_base}
finally:
    m3_restore(obj,state)
'''


def _distance(a, b):
    return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)))


def observe(bridge, plan, state, kind, options=None):
    if options:
        raise ValueError('UNSUPPORTED_OBSERVATION_OPTIONS: M6 measurements use the full brief window and explicit semantic body roles')
    brief, mapping = plan['brief'], plan['mapping']
    roles = [r for r in ('root', 'pelvis', 'chest', 'head', 'hand_l', 'hand_r', 'foot_l', 'foot_r') if mapping.get(r)]
    frames = list(range(brief['frame_start'], brief['frame_end']+1))
    data = CandidateBridge(bridge, plan['object_name'], state['candidate_action']).run(
        BASE_RUNTIME+SAMPLE, {'object_name': plan['object_name'], 'bones': [mapping[r] for r in roles], 'frames': frames})
    result = {'status': 'measured', 'kind': kind, 'candidate_action': state['candidate_action'],
              'frame_range': [frames[0], frames[-1]], 'fps': data['fps'], 'findings': [], 'metrics': {},
              'warnings': ['Measured body/bone proxies require temporal artistic interpretation; no quality score.']}
    for role in roles:
        track = data['tracks'][mapping[role]]
        distances = [_distance(a, b) for a, b in zip(track, track[1:])]
        speeds = [d*data['fps'] for d in distances]
        accelerations = [(b-a)*data['fps'] for a, b in zip(speeds, speeds[1:])]
        bends = []
        for a, b, c in zip(track, track[1:], track[2:]):
            v = [y-x for x, y in zip(a, b)]; w = [y-x for x, y in zip(b, c)]
            divisor = math.sqrt(sum(x*x for x in v)*sum(x*x for x in w))
            bends.append(math.degrees(math.acos(max(-1., min(1., sum(x*y for x, y in zip(v, w))/divisor)))) if divisor > 1e-12 else 0.)
        result['metrics'][role] = {'path_m': sum(distances), 'net_displacement_m': _distance(track[0], track[-1]),
                                   'peak_step_m': max(distances, default=0.), 'peak_velocity_m_s': max(speeds, default=0.),
                                   'peak_acceleration_m_s2': max(map(abs, accelerations), default=0.),
                                   'peak_direction_change_deg': max(bends, default=0.),
                                   'loop_endpoint_m': _distance(track[0], track[-1]),
                                   'loop_velocity_delta_m_s': abs(speeds[-1]-speeds[0]) if speeds else 0.}
        if kind == 'arcs':
            ranked = sorted(enumerate(bends), key=lambda r: r[1], reverse=True)[:4]
            result['findings'].extend({'role': role, 'frame': frames[i+1], 'direction_change_deg': angle,
                                        'severity': 'review', 'reason': 'Sharp change may be an intended impact or contact transition.'}
                                       for i, angle in ranked if angle > 15.)
    phases = plan['pose_plan'].get('phases', [])
    if kind == 'timing':
        result['phases'] = phases
        result['duration_seconds'] = (frames[-1]-frames[0])/data['fps']
        result['timing_provenance'] = brief.get('provenance')
    if kind == 'mechanics':
        pelvis = data['tracks'][mapping['pelvis']]
        left, right = (data['tracks'][mapping['foot_'+side]] for side in ('l', 'r'))
        ground = plan['options'].get('ground_height_m', 0.)
        margins = []
        for p, l, r in zip(pelvis, left, right):
            # Signed horizontal distance to support segment, not a physical COM solver.
            dx, dy = r[0]-l[0], r[1]-l[1]
            t = max(0., min(1., ((p[0]-l[0])*dx+(p[1]-l[1])*dy)/max(dx*dx+dy*dy, 1e-12)))
            margins.append(math.hypot(p[0]-l[0]-t*dx, p[1]-l[1]-t*dy))
        result['metrics']['pelvis_support_proxy'] = {'maximum_distance_m': max(margins),
                                                    'mean_distance_m': mean(margins), 'ground_height_m': ground,
                                                    'not_physical_center_of_mass': True}
        result['intentional_asymmetry'] = brief.get('mass_asymmetry')
        result['warnings'].append('Support segment includes both feet; use confirmed contacts to interpret airborne intervals.')
    return result
