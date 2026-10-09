"""Real-Blender regression probe for the five HIGH findings of the M3 independent audit.

Each scenario keeps the auditor's reproduction as closely as possible and measures the outcome
with checks that do not reuse the production verification code. A scenario is:

- ``unsafe_pass``: the production code reported a numeric PASS (or validated a mapping / reported
  zero sliding) while the independent measurement shows the result is wrong. This is the bug.
- ``safe``: the production code either rejected the input explicitly or produced a result whose
  independent measurement is within tolerance.

Large files (fixtures, checkpoints, candidates) are written outside the repository; only the small
JSON report is promoted into ``test-artifacts/m3.1-audit-regressions/``.
"""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import types
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PROMOTED = ROOT / 'test-artifacts' / 'm3.1-audit-regressions'

OUTSIDE_TOLERANCE_M = 1e-4
DIRECTION_TOLERANCE_DEG = 1.0


def inside(output, label):
    import bpy
    from mathutils import Quaternion, Vector
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(ROOT / 'tests/blender'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage

    class LocalBridge:
        def run(self, body, params=None):
            namespace = {}
            exec(build_code(body, params).rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    storage = Storage(output / 's')
    report = {'label': label, 'blender': bpy.app.version_string, 'python': sys.version.split()[0],
              'scenarios': {}, 'tolerances': {'outside_window_m': OUTSIDE_TOLERANCE_M,
                                              'direction_degrees': DIRECTION_TOLERANCE_DEG}}

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

    try:
        from blender_mcp.production.common.asset_safety import register_fixture
    except ImportError:  # v0.3.0 has no asset guard
        register_fixture = None

    def register(path):
        if register_fixture is not None:
            register_fixture(bridge, storage, str(path))

    def rejection(exc):
        text = str(exc)
        code = text.split(':', 1)[0].strip() if ':' in text else ''
        return {'rejected': True, 'error': text[:400], 'code': code if code.isupper() else None}

    def scenario(name):
        def wrap(function):
            try:
                data = function()
            except Exception as exc:  # recorded, never hidden
                data = {'verdict': 'error', 'exception': repr(exc), 'traceback': traceback.format_exc()[-3000:]}
            report['scenarios'][name] = data
            save()
            print('SCENARIO', name, data.get('verdict'), flush=True)
            return function
        return wrap

    def assign(obj, action):
        obj.animation_data.action = action
        if action is not None and action.slots:
            obj.animation_data.action_slot = action.slots[0]

    # ------------------------------------------------------------------ IK/FK (AUD-001, AUD-004)
    from m3_rig_fixtures import build as build_rig
    from blender_mcp.production.rig.ik_fk import operate
    rig_manifest = build_rig(output / 'rig')
    register(rig_manifest['blend'])
    scene = bpy.context.scene

    def chain_samples(obj, action, frames, names):
        previous = obj.animation_data.action
        assign(obj, action)
        rows = {}
        for frame in frames:
            scene.frame_set(frame)
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            rows[frame] = {n: (evaluated.matrix_world @ evaluated.pose.bones[n].head,
                               evaluated.matrix_world @ evaluated.pose.bones[n].tail) for n in names}
        assign(obj, previous)
        scene.frame_set(5)
        return rows

    def deviation(a, b):
        return {frame: max(max((a[frame][n][0] - b[frame][n][0]).length, (a[frame][n][1] - b[frame][n][1]).length)
                           for n in a[frame]) for frame in a}

    def ikfk_case(name, direction, bake, side, options, window, rest_during_operation=False,
                  rig_name='M3_MixedRig', source_action='M3_MixedMotion', extra=None):
        rig = bpy.data.objects[rig_name]
        chain = ['UpperLeg.' + side, 'LowerLeg.' + side, 'Foot.' + side]
        base = {'chain': chain, 'ik_target': 'ContactControl.' + side, 'pole_target': 'PoleControl.' + side,
                'confirm_mapping': True, **(extra or {})}
        actions_before = len(bpy.data.actions)
        if rest_during_operation:
            rig.data.pose_position = 'REST'
        try:
            proposal = operate(bridge, storage, name, direction, bake, rig_name, 'propose', dict(base, **options))
            if not proposal['proposal'].get('supported'):
                return {'verdict': 'safe', 'candidates_allocated': len(bpy.data.actions) - actions_before,
                        **rejection(Exception(';'.join(proposal['proposal'].get('reasons', []))))}
            preview = operate(bridge, storage, name + 'p', direction, bake, mode='preview', operation=proposal['operation'])
        except Exception as exc:
            return {'verdict': 'safe', 'candidates_allocated': len(bpy.data.actions) - actions_before, **rejection(exc)}
        finally:
            # Measurements always happen in POSE mode: comparing REST against REST proves nothing.
            rig.data.pose_position = 'POSE'
        if preview.get('numeric_verification') != 'pass':
            return {'verdict': 'safe', 'numeric_verification': preview.get('numeric_verification'),
                    'candidates_allocated': len(bpy.data.actions) - actions_before, 'errors': preview.get('errors'),
                    'reported_metrics': {k: v for k, v in preview.get('metrics', {}).items() if isinstance(v, (int, float, str, list)) and not isinstance(v, bool)}}
        frames = list(range(1, 10))
        dev = deviation(chain_samples(rig, bpy.data.actions[source_action], frames, chain),
                        chain_samples(rig, bpy.data.actions[preview['working_action']], frames, chain))
        outside = {f: d for f, d in dev.items() if not window[0] <= f <= window[1]}
        worst = max(outside.values(), default=0.0)
        return {'verdict': 'unsafe_pass' if worst > OUTSIDE_TOLERANCE_M else 'safe',
                'numeric_verification': 'pass', 'window': window, 'max_outside_window_deviation_m': worst,
                'deviation_by_frame_m': dev, 'reported_metrics': {k: v for k, v in preview.get('metrics', {}).items()
                                                                  if isinstance(v, (int, float))}}

    @scenario('AUD-001a_single_frame_fk_to_ik')
    def _():
        return ikfk_case('a1a', 'fk_to_ik', False, 'R', {'frame': 5}, (5, 5))

    @scenario('AUD-001b_bake_fk_to_ik_3_6')
    def _():
        return ikfk_case('a1b', 'fk_to_ik', True, 'R', {'frame_start': 3, 'frame_end': 6, 'step': 1}, (3, 6))

    @scenario('AUD-001c_single_frame_ik_to_fk')
    def _():
        return ikfk_case('a1c', 'ik_to_fk', False, 'L', {'frame': 5}, (5, 5))

    @scenario('AUD-001d_bake_ik_to_fk_3_6')
    def _():
        return ikfk_case('a1d', 'ik_to_fk', True, 'L', {'frame_start': 3, 'frame_end': 6, 'step': 1}, (3, 6))

    def bezier_switch_history():
        # Multi-key BEZIER influence history on the right leg: 0 -> 1 -> 0 -> 1 across the clip.
        rig = bpy.data.objects['M3_MixedRig']
        action = bpy.data.actions['M3_MixedMotion'].copy()
        action.name = 'M31_BezierSwitchHistory'
        action.use_fake_user = True
        path = rig.pose.bones['LowerLeg.R'].constraints['TwoBone.R'].path_from_id('influence')
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for curve in bag.fcurves:
                        if curve.data_path == path:
                            while curve.keyframe_points:
                                curve.keyframe_points.remove(curve.keyframe_points[0])
                            for frame, value in ((1, 0.0), (3, 1.0), (7, 0.0), (9, 1.0)):
                                point = curve.keyframe_points.insert(frame, value)
                                point.interpolation = 'BEZIER'
                                point.handle_left_type = point.handle_right_type = 'AUTO_CLAMPED'
                            curve.update()
        return rig, action

    @scenario('AUD-001e_bezier_multikey_switch_single_frame')
    def _():
        rig, action = bezier_switch_history()
        previous = rig.animation_data.action
        assign(rig, action)
        try:
            return ikfk_case('a1e', 'fk_to_ik', False, 'R', {'frame': 5}, (5, 5), source_action=action.name)
        finally:
            assign(rig, previous)

    @scenario('AUD-001f_bezier_multikey_switch_bake_across_key')
    def _():
        rig, action = bezier_switch_history()
        previous = rig.animation_data.action
        assign(rig, action)
        try:
            return ikfk_case('a1f', 'ik_to_fk', True, 'R', {'frame_start': 2, 'frame_end': 4, 'step': 1}, (2, 4),
                             source_action=action.name)
        finally:
            assign(rig, previous)

    @scenario('AUD-001h_bezier_multikey_switch_bake_half_step')
    def _():
        # Same blend as 001f but sampled every half frame: the subframe blend must now be reproducible.
        rig, action = bezier_switch_history()
        previous = rig.animation_data.action
        assign(rig, action)
        try:
            return ikfk_case('a1h', 'ik_to_fk', True, 'R', {'frame_start': 2, 'frame_end': 4, 'step': .5}, (2, 4),
                             source_action=action.name)
        finally:
            assign(rig, previous)

    @scenario('AUD-001g_custom_switch_driver_single_frame')
    def _():
        switch = {'owner': '$object', 'property': 'ik_fk', 'ik_value': 1, 'fk_value': 0}
        return ikfk_case('a1g', 'fk_to_ik', False, 'L', {'frame': 5}, (5, 5), rig_name='M3_SwitchRig',
                         source_action='M3_SwitchMotion', extra={'switch': switch})

    @scenario('AUD-004a_rest_position_ik_to_fk_bake')
    def _():
        result = ikfk_case('a4a', 'ik_to_fk', True, 'L', {'frame_start': 1, 'frame_end': 9, 'step': 1}, (1, 9),
                           rest_during_operation=True)
        if result.get('numeric_verification') == 'pass':
            # Every frame is inside the window here: compare against the POSE-mode source directly.
            worst = max(result['deviation_by_frame_m'].values())
            result['verdict'] = 'unsafe_pass' if worst > OUTSIDE_TOLERANCE_M else 'safe'
            result['max_pose_mode_deviation_m'] = worst
        return result

    @scenario('AUD-004c_pose_to_rest_after_proposal_ik_fk')
    def _():
        rig = bpy.data.objects['M3_MixedRig']
        chain = ['UpperLeg.L', 'LowerLeg.L', 'Foot.L']
        options = {'chain': chain, 'ik_target': 'ContactControl.L', 'pole_target': 'PoleControl.L',
                   'confirm_mapping': True, 'frame_start': 1, 'frame_end': 9, 'step': 1}
        proposal = operate(bridge, storage, 'a4c', 'ik_to_fk', True, 'M3_MixedRig', 'propose', options)
        actions_before = len(bpy.data.actions)
        rig.data.pose_position = 'REST'
        try:
            preview = operate(bridge, storage, 'a4cp', 'ik_to_fk', True, mode='preview', operation=proposal['operation'])
        except Exception as exc:
            return {'verdict': 'safe', 'candidates_allocated': len(bpy.data.actions) - actions_before, **rejection(exc)}
        finally:
            rig.data.pose_position = 'POSE'
        return {'verdict': 'unsafe_pass' if preview.get('numeric_verification') == 'pass' else 'safe',
                'numeric_verification': preview.get('numeric_verification'), 'errors': preview.get('errors'),
                'candidates_allocated': len(bpy.data.actions) - actions_before,
                'note': 'Safe only if rejected before allocation; a failed verification after allocation is fail-closed but late.'}

    # ------------------------------------------------------------------ CONTACT CORRECTION (AUD-005b)
    from m3_contact_fixtures import build as build_contacts
    from blender_mcp.production.contacts.service import fix_contacts as fix_contacts_early
    bpy.ops.wm.read_factory_settings(use_empty=True)
    contact_fixture = build_contacts(output / 'contacts')
    register(contact_fixture['file'])

    def flip(name):
        import bmesh
        obj = bpy.data.objects[name]
        mesh = bmesh.new(); mesh.from_mesh(obj.data)
        bmesh.ops.reverse_faces(mesh, faces=mesh.faces)
        mesh.to_mesh(obj.data); mesh.free(); obj.data.update()

    def foot_correction_case(name, rig_name, options, surface):
        normal = fix_contacts_early(bridge, storage, name + 'n', rig_name, 'propose', options)
        normal_preview = (fix_contacts_early(bridge, storage, name + 'np', mode='preview', operation=normal['operation'])
                          if normal['proposal'].get('supported') else {})
        actions_before = len(bpy.data.actions)
        flip(surface)
        try:
            proposal = fix_contacts_early(bridge, storage, name, rig_name, 'propose', options)
            if not proposal['proposal'].get('supported'):
                return {'verdict': 'safe', 'rejected': True, 'warnings': proposal['proposal'].get('warnings'),
                        'candidates_allocated': len(bpy.data.actions) - actions_before}
            preview = fix_contacts_early(bridge, storage, name + 'p', mode='preview', operation=proposal['operation'])
        except Exception as exc:
            return {'verdict': 'safe', **rejection(exc)}
        finally:
            flip(surface)
        metrics = preview.get('metrics', {})
        before = metrics.get('sliding_before_m')
        unsafe = preview.get('numeric_verification') == 'pass' and not before
        return {'verdict': 'unsafe_pass' if unsafe else 'safe', 'numeric_verification': preview.get('numeric_verification'),
                'sliding_before_m': before, 'sliding_after_m': metrics.get('sliding_after_m'),
                'normal_floor_reference': {'numeric_verification': normal_preview.get('numeric_verification'),
                                           'sliding_before_m': normal_preview.get('metrics', {}).get('sliding_before_m'),
                                           'sliding_after_m': normal_preview.get('metrics', {}).get('sliding_after_m')},
                'errors': preview.get('errors')}

    @scenario('AUD-005b_inverted_normals_foot_correction_ik')
    def _():
        return foot_correction_case('a5i', contact_fixture['rig'], dict(contact_fixture['options'], confirm_contacts=True),
                                    contact_fixture['options']['surfaces'][0])

    @scenario('AUD-005e_inverted_normals_strict_mode_correction')
    def _():
        options = dict(contact_fixture['options'], confirm_contacts=True, surface_orientation='as_modeled')
        result = foot_correction_case('a5s', contact_fixture['rig'], options, contact_fixture['options']['surfaces'][0])
        return result

    @scenario('AUD-005c_inverted_normals_foot_correction_fk')
    def _():
        fk_options = {'feet': [{'bone': 'terminal', 'chain': ['upper', 'lower', 'terminal'], 'heel': [0, -.03, 0],
                                'sole': [0, .08, 0], 'toe': [0, .2, 0]}], 'surfaces': ['M3_FKFloor'], 'confirm_contacts': True}
        return foot_correction_case('a5f', 'M3_FKContactRig', fk_options, 'M3_FKFloor')

    # ------------------------------------------------------------------ MOCAP (AUD-002, AUD-003, AUD-004)
    from m3_mocap_fixtures import build as build_mocap
    from blender_mcp.production.mocap.service import retarget, map_skeleton
    from blender_mcp.production.contacts.service import analyze_contacts, fix_contacts
    from blender_mcp.production.contacts.runtime import CONTACT_RUNTIME
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME
    (output / 'mocap').mkdir(parents=True, exist_ok=True)
    fx = build_mocap(output / 'mocap')
    register(fx['path'])
    scene = bpy.context.scene
    source = bpy.data.objects[fx['source']]
    target = bpy.data.objects[fx['target']]
    roles = {row['semantic_role']: row for row in fx['mapping']}

    # Auditor's source motion: the left upper arm swings 80 degrees down (world +Y) over frames 1-21.
    arms_down = bpy.data.actions[fx['source_action']].copy()
    arms_down.name = 'M31_LeftArmDown'
    arms_down.use_fake_user = True
    arm = source.pose.bones[roles['left_upper_arm']['source_bone']]
    rest_q = arm.bone.matrix_local.to_quaternion()
    arm_path = arm.path_from_id('rotation_quaternion')
    for layer in arms_down.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    if curve.data_path == arm_path:
                        for point in curve.keyframe_points:
                            angle = math.radians(80) * (point.co.x - 1) / 20
                            point.co.y = (rest_q.inverted() @ Quaternion((0, 1, 0), angle) @ rest_q)[curve.array_index]
    assign(source, arms_down)
    scene.frame_set(1)
    base_options = {'source_armature': fx['source'], 'mapping_approved': True, 'frame_start': 1, 'frame_end': 21}

    def bone_direction(obj, name):
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        pose = evaluated.pose.bones[name]
        return (evaluated.matrix_world @ pose.tail - evaluated.matrix_world @ pose.head).normalized()

    def arm_angles(candidate, target_bone):
        original = target.animation_data.action
        assign(target, bpy.data.actions[candidate])
        rows = {}
        for frame in (1, 11, 21):
            scene.frame_set(frame)
            src = bone_direction(source, roles['left_upper_arm']['source_bone'])
            dst = bone_direction(target, target_bone)
            rows[frame] = math.degrees(src.angle(dst))
        assign(target, original)
        scene.frame_set(1)
        return rows

    state = {}

    @scenario('AUD-002_retarget_rest_T_vs_A')
    def _():
        try:
            proposal = retarget(bridge, storage, 'a2', fx['target'], 'propose', dict(base_options, mapping=fx['mapping']))
            if not proposal['proposal'].get('supported'):
                return {'verdict': 'safe', 'rejected': True, 'errors': proposal['proposal'].get('errors')}
            preview = retarget(bridge, storage, 'a2p', mode='preview', operation=proposal['operation'])
        except Exception as exc:
            return {'verdict': 'safe', **rejection(exc)}
        if preview.get('numeric_verification') != 'pass':
            return {'verdict': 'safe', 'numeric_verification': preview.get('numeric_verification'), 'errors': preview.get('errors')}
        state['candidate'] = preview['working_action']
        angles = arm_angles(preview['working_action'], roles['left_upper_arm']['target_bone'])
        worst = max(angles.values())
        return {'verdict': 'unsafe_pass' if worst > DIRECTION_TOLERANCE_DEG else 'safe', 'numeric_verification': 'pass',
                'left_arm_direction_angle_deg_by_frame': angles, 'max_direction_angle_deg': worst,
                'rest_compensation': preview.get('proposal', {}).get('rest_compatibility') or
                                     proposal['proposal'].get('rest_pose_compensation_max_degrees')}

    @scenario('AUD-003_left_right_swapped_mapping')
    def _():
        swapped = []
        for row in fx['mapping']:
            copy = dict(row)
            role = row['semantic_role']
            if role.startswith('left_'):
                copy['target_bone'] = roles['right_' + role[5:]]['target_bone']
            elif role.startswith('right_'):
                copy['target_bone'] = roles['left_' + role[6:]]['target_bone']
            swapped.append(copy)
        validation = map_skeleton(bridge, fx['source'], fx['target'], swapped, True)
        result = {'map_skeleton_validated': validation.get('validated'), 'map_skeleton_errors': (validation.get('errors') or [])[:6]}
        try:
            proposal = retarget(bridge, storage, 'a3', fx['target'], 'propose', dict(base_options, mapping=swapped))
            supported = proposal['proposal'].get('supported')
            result['proposal_supported'] = supported
            if supported:
                preview = retarget(bridge, storage, 'a3p', mode='preview', operation=proposal['operation'])
                result['numeric_verification'] = preview.get('numeric_verification')
        except Exception as exc:
            result.update(rejection(exc))
        unsafe = result.get('map_skeleton_validated') or result.get('numeric_verification') == 'pass'
        result['verdict'] = 'unsafe_pass' if unsafe else 'safe'
        return result

    @scenario('AUD-004b_retarget_source_rest_after_proposal')
    def _():
        proposal = retarget(bridge, storage, 'a4b', fx['target'], 'propose', dict(base_options, mapping=fx['mapping']))
        if not proposal['proposal'].get('supported'):
            return {'verdict': 'not_applicable', 'reason': 'normal retarget unsupported on this build',
                    'errors': proposal['proposal'].get('errors')}
        source.data.pose_position = 'REST'
        try:
            preview = retarget(bridge, storage, 'a4bp', mode='preview', operation=proposal['operation'])
        except Exception as exc:
            return {'verdict': 'safe', **rejection(exc)}
        finally:
            source.data.pose_position = 'POSE'
        displacement = preview.get('metrics', {}).get('target_qa', {}).get('root_motion', {}).get('displacement_m')
        unsafe = preview.get('numeric_verification') == 'pass'
        return {'verdict': 'unsafe_pass' if unsafe else 'safe', 'numeric_verification': preview.get('numeric_verification'),
                'target_root_displacement_m': displacement, 'errors': preview.get('errors')}

    # ------------------------------------------------------------------ CONTACTS (AUD-005, AUD-006)
    def flip_floor():
        import bmesh
        floor = bpy.data.objects[fx['surfaces'][0]]
        mesh = bmesh.new(); mesh.from_mesh(floor.data)
        bmesh.ops.reverse_faces(mesh, faces=mesh.faces)
        mesh.to_mesh(floor.data); mesh.free(); floor.data.update()

    def sliding_of(result):
        metrics = result.get('metrics', {})
        return metrics.get('max_contact_sliding_m'), result.get('contact_status') or result.get('status')

    @scenario('AUD-005a_inverted_normals_contact_analysis')
    def _():
        if 'candidate' not in state:
            return {'verdict': 'not_applicable', 'reason': 'no retarget candidate to analyze'}
        original = target.animation_data.action
        assign(target, bpy.data.actions[state['candidate']])
        options = {'feet': fx['feet'], 'surfaces': fx['surfaces'], 'frame_start': 1, 'frame_end': 21}
        try:
            normal = analyze_contacts(bridge, fx['target'], options)
            flip_floor()
            try:
                inverted = analyze_contacts(bridge, fx['target'], options)
            finally:
                flip_floor()
        finally:
            assign(target, original)
        normal_sliding, normal_status = sliding_of(normal)
        inverted_sliding, inverted_status = sliding_of(inverted)
        # Unsafe: evidence of absence encoded as a numeric zero while the real sliding is large.
        unsafe = normal_sliding is not None and normal_sliding > .05 and inverted_sliding is not None and inverted_sliding < normal_sliding * .5
        return {'verdict': 'unsafe_pass' if unsafe else 'safe',
                'normal_floor': {'max_contact_sliding_m': normal_sliding, 'status': normal_status,
                                 'phases': sorted({p['phase'] for p in normal.get('phases', [])})},
                'inverted_floor': {'max_contact_sliding_m': inverted_sliding, 'status': inverted_status,
                                   'phases': sorted({p['phase'] for p in inverted.get('phases', [])}),
                                   'feet': inverted.get('feet')}}

    def surface_mesh(name, vertices, faces):
        mesh = bpy.data.meshes.new(name + 'Mesh')
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        scene.collection.objects.link(obj)
        return obj

    def query(names, point, options=None):
        body = BASE_RUNTIME + CONTACT_RUNTIME + "\nresult={'hit':contact_query(params['point'],contact_static_surfaces(params['names']),params['options'])}\n"
        return bridge.run(body, {'names': names, 'point': point, 'options': options or {}})['hit']

    @scenario('AUD-006_contact_surface_selection_matrix')
    def _():
        up = [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)]
        created = [surface_mesh('M31_FloorUp', up, [(0, 1, 2, 3)]),
                   surface_mesh('M31_FloorDown', up, [(3, 2, 1, 0)]),
                   surface_mesh('M31_FloorMixed', [(-1, -1, 0), (0, -1, 0), (0, 1, 0), (-1, 1, 0), (1, -1, 0), (1, 1, 0)],
                                [(0, 1, 2, 3), (2, 5, 4, 1)]),  # second quad clockwise from above: modeled normal -Z
                   surface_mesh('M31_Slope', [(-1, -1, -math.tan(math.radians(20))), (1, -1, -math.tan(math.radians(20))),
                                              (1, 1, math.tan(math.radians(20))), (-1, 1, math.tan(math.radians(20)))], [(0, 1, 2, 3)]),
                   surface_mesh('M31_Step', [(-1, -1, 0), (1, -1, 0), (1, 0, 0), (-1, 0, 0), (1, 0, .2), (-1, 0, .2)],
                                [(0, 1, 2, 3), (3, 2, 4, 5)]),
                   surface_mesh('M31_Wall', [(-1, 0, 0), (1, 0, 0), (1, 0, 2), (-1, 0, 2)], [(0, 1, 2, 3)]),
                   surface_mesh('M31_Decal', [(-1, -1, .0012), (1, -1, .0012), (1, 1, .0012), (-1, 1, .0012)], [(0, 1, 2, 3)])]
        bpy.context.view_layer.update()
        cases = {}
        def case(label, names, point, expect, options=None):
            hit = query(names, point, options)
            ok = expect(hit)
            cases[label] = {'ok': bool(ok), 'hit': None if hit is None else {k: hit.get(k) for k in (
                'surface_object', 'distance', 'signed_distance_m', 'normal_orientation', 'ambiguous', 'rejected_faces', 'confidence')}}
        case('normal_floor', ['M31_FloorUp'], [0, 0, .002], lambda h: h and h['normal_orientation'] == 'as_modeled' and abs(h['signed_distance_m'] - .002) < 1e-6)
        case('inverted_floor_two_sided', ['M31_FloorDown'], [0, 0, .002], lambda h: h and h['normal_orientation'] == 'flipped_for_support' and abs(h['signed_distance_m'] - .002) < 1e-6)
        case('inverted_floor_strict_as_modeled', ['M31_FloorDown'], [0, 0, .002], lambda h: h is None, {'surface_orientation': 'as_modeled'})
        case('mixed_normals_up_half', ['M31_FloorMixed'], [-.5, 0, .002], lambda h: h is not None)
        case('mixed_normals_down_half', ['M31_FloorMixed'], [.5, 0, .002], lambda h: h is not None and h['normal_orientation'] == 'flipped_for_support')
        case('sloped_floor_20deg', ['M31_Slope'], [0, 0, .002], lambda h: h is not None and h['distance'] < .003)
        case('near_stair_riser', ['M31_Step'], [0, -.001, .002], lambda h: h is not None and abs(h['signed_distance_m'] - .002) < 1e-6)
        case('floor_plus_nearby_wall', ['M31_FloorUp', 'M31_Wall'], [0, -.001, .002], lambda h: h is not None and h['surface_object'] == 'M31_FloorUp')
        case('overlapping_surfaces_ambiguous', ['M31_FloorUp', 'M31_Decal'], [0, 0, .005], lambda h: h is not None and h['ambiguous'])
        case('foot_sunk_3cm', ['M31_FloorUp'], [0, 0, -.03], lambda h: h is not None and abs(h['signed_distance_m'] + .03) < 1e-6)
        for obj in created:
            bpy.data.objects.remove(obj, do_unlink=True)
        failed = [k for k, v in cases.items() if not v['ok']]
        return {'verdict': 'unsafe_pass' if failed else 'safe', 'failed_cases': failed, 'cases': cases}

    @scenario('AUD-005d_inverted_normals_strict_mode_analysis')
    def _():
        if 'candidate' not in state:
            return {'verdict': 'not_applicable', 'reason': 'no retarget candidate to analyze'}
        original = target.animation_data.action
        assign(target, bpy.data.actions[state['candidate']])
        options = {'feet': fx['feet'], 'surfaces': fx['surfaces'], 'frame_start': 1, 'frame_end': 21, 'surface_orientation': 'as_modeled'}
        flip_floor()
        try:
            result = analyze_contacts(bridge, fx['target'], options)
        except Exception as exc:
            return {'verdict': 'safe', **rejection(exc)}
        finally:
            flip_floor()
            assign(target, original)
        sliding, status = sliding_of(result)
        # Absence of evidence must stay explicit: numeric zero sliding would be the audit defect.
        return {'verdict': 'unsafe_pass' if sliding == 0 else 'safe', 'max_contact_sliding_m': sliding,
                'contact_status': result.get('contact_status'), 'feet': result.get('feet'), 'warnings': result.get('warnings')}

    report['summary'] = {name: row.get('verdict') for name, row in report['scenarios'].items()}
    save()
    print('M31_AUDIT_REPORT=' + str(output / 'report.json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm31' / (args.label + '-' + run_id)
    output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output), args.label],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'stderr.txt').write_text(process.stderr, encoding='utf-8')
    print('\n'.join(line for line in process.stdout.splitlines() if line.startswith(('SCENARIO', 'M31_'))))
    report_path = output / 'report.json'
    if not report_path.exists():
        print(process.stdout[-4000:]); print(process.stderr[-4000:])
        raise SystemExit(1)
    PROMOTED.mkdir(parents=True, exist_ok=True)
    promoted = PROMOTED / (args.label + '-' + run_id + '.json')
    shutil.copyfile(report_path, promoted)
    print('PROMOTED=' + str(promoted))
    print('ARTIFACTS=' + str(output))


if __name__ == '__main__':
    if '--inside' in sys.argv:
        position = sys.argv.index('--inside')
        inside(Path(sys.argv[position + 1]), sys.argv[position + 2])
    else:
        main()
