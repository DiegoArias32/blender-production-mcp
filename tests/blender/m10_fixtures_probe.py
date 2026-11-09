"""M10 demo fixtures in real Blender (own background process): build the five demos in new folders, validate each
manifest against the saved scene by evaluation, run a candidate/reject smoke test on the M10 foundation, and save a
small Workbench check render per demo.

    blender -b --factory-startup --disable-autoexec --python tests/blender/m10_fixtures_probe.py -- <out_dir> [kinds]

Every threshold below is a PROPOSAL of this probe (labelled in the report); every number in `measured` comes from
Blender evaluation (frame_set + evaluated depsgraph), never from the fixture's own specification.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import threading
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
package = types.ModuleType('blender_mcp')
package.__path__ = [str(ROOT / 'src' / 'blender_mcp')]
sys.modules.setdefault('blender_mcp', package)

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402
from mathutils.bvhtree import BVHTree  # noqa: E402

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402
import m10_cinematic_fixtures as fx  # noqa: E402

# Thresholds proposed by this probe (not derived from a standard); the measured values are reported next to them.
THRESHOLDS = {'root_motion_tolerance_m': .01, 'root_direction_min_dot': .99, 'static_root_max_m': 1e-6,
              'turn_tolerance_deg': 1.0, 'ik_error_max_m': 1e-3, 'ground_contact_max_m': .005,
              'react_head_back_min_m': .05, 'reach_forward_min_m': .25, 'stare_yaw_tolerance_deg': 3.0,
              'attack_hands_rise_min_m': .5, 'rigid_follow_max_m': 1e-5, 'cycle_pose_max_m': 1e-4,
              'grip_distance_max_m': 1e-6, 'scale_tolerance': 1e-6, 'threshold_source': 'PROPOSAL'}


class LocalBridge:
    """Runs production payloads through build_code (safe mode) inside this Blender, like the M10 foundation probe."""

    def __init__(self):
        self.lock = threading.RLock()
        self.calls = 0
        self.bytes = 0

    def run(self, body, params=None):
        code = build_code(body, params)
        self.calls += 1
        self.bytes += len(code.encode('utf-8'))
        ns = {}
        exec(code.rsplit('print(', 1)[0], ns)
        return ns['result']


REPORT = {'suite': 'm10_fixtures', 'blender': bpy.app.version_string, 'thresholds': THRESHOLDS, 'checks': [],
          'measured': {}, 'metrics': {}, 'renders': [], 'errors': []}
OUT = None


def save_report():
    REPORT['summary'] = {'passed': sum(c['passed'] for c in REPORT['checks']),
                         'failed': sum(not c['passed'] for c in REPORT['checks']), 'errors': len(REPORT['errors'])}
    (OUT / 'm10_fixtures_report.json').write_text(json.dumps(REPORT, indent=1, default=str), encoding='utf-8')


def check(name, condition, data=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'data': data})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)


def world(rig, bone, depsgraph, point='head'):
    evaluated = rig.evaluated_get(depsgraph)
    pose_bone = evaluated.pose.bones[bone]
    return evaluated.matrix_world @ getattr(pose_bone, point)


def bone_matrix(rig, bone, depsgraph):
    evaluated = rig.evaluated_get(depsgraph)
    return evaluated.matrix_world @ evaluated.pose.bones[bone].matrix


def at(scene, frame):
    scene.frame_set(frame)
    return bpy.context.evaluated_depsgraph_get()


def action_digest(action):
    rows = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:
                    rows.append([curve.data_path, curve.array_index,
                                 [[round(p.co.x, 6), round(p.co.y, 6), p.interpolation] for p in curve.keyframe_points]])
    return hashlib.sha256(json.dumps(sorted(rows), default=str).encode('utf-8')).hexdigest()


def action_keys(action):
    frames = [p.co.x for layer in action.layers for strip in layer.strips for bag in strip.channelbags
              for curve in bag.fcurves for p in curve.keyframe_points]
    return frames


def yaw_of(matrix):
    axis = matrix.to_3x3().col[0]
    return math.degrees(math.atan2(axis.y, axis.x))


def mesh_world(obj, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        return [evaluated.matrix_world @ v.co for v in mesh.vertices], [tuple(p.vertices) for p in mesh.polygons]
    finally:
        evaluated.to_mesh_clear()


def closed(obj):
    counts = {}
    for polygon in obj.data.polygons:
        for edge in polygon.edge_keys:
            counts[edge] = counts.get(edge, 0) + 1
    return bool(counts) and all(c == 2 for c in counts.values())


def measure_actions(kind, scene, actor):
    """Evaluate every Action of the actor by making it active (the .blend is already saved and hashed), then put the
    original active Action back."""
    rig = bpy.data.objects[actor['object']]
    body = bpy.data.objects[actor['body']]
    groups = {body.vertex_groups[b].index for b in actor['bones']['feet'].values()}
    foot_vertices = [v.index for v in body.data.vertices if any(g.group in groups and g.weight > 0 for g in v.groups)]
    original = rig.animation_data.action
    s = actor['scale_factor']
    digests = {}
    rows = {}
    for row in actor['actions']:
        action = bpy.data.actions[row['name']]
        keys = action_keys(action)
        digests[row['name']] = action_digest(action)
        rig.animation_data.action = action
        start, end = int(row['frame_range'][0]), int(row['frame_range'][1])
        depsgraph = at(scene, start)
        root0 = world(rig, 'root', depsgraph)
        root_m0 = bone_matrix(rig, 'root', depsgraph)
        head0 = world(rig, 'head', depsgraph, 'tail')
        head_m0 = bone_matrix(rig, 'head', depsgraph)
        side = row.get('reach_side', 'R')
        hand0 = {k: world(rig, 'hand.' + k, depsgraph, 'tail') for k in 'LR'}
        rel0 = {b: world(rig, b, depsgraph) - root0 for b in ('foot_ik.L', 'foot_ik.R', 'head', 'hand.L', 'hand.R', 'pelvis')}
        ik = 0.0
        head_back = 0.0
        feet_low = proxy_low = float('inf')
        for frame in range(start, end + 1):
            depsgraph = at(scene, frame)
            for leg in 'LR':
                ik = max(ik, (world(rig, 'shin.' + leg, depsgraph, 'tail') - world(rig, 'foot_ik.' + leg, depsgraph)).length)
            head_back = max(head_back, world(rig, 'head', depsgraph, 'tail').y - head0.y)
            vertices = mesh_world(body, depsgraph)[0]
            feet_low = min(feet_low, min(vertices[i].z for i in foot_vertices))
            proxy_low = min(proxy_low, min(v.z for v in vertices))
        depsgraph = at(scene, end)
        root1 = world(rig, 'root', depsgraph)
        delta = root1 - root0
        horizontal = Vector((delta.x, delta.y, 0.0))
        measured = {'keyframes': len(keys), 'keyed_range': [min(keys), max(keys)],
                    'slot': action.slots[0].identifier if len(action.slots) else None,
                    'fake_user': action.use_fake_user, 'root_motion_m': horizontal.length,
                    'root_direction_dot_minus_y': (-horizontal.y / horizontal.length) if horizontal.length > 1e-9 else None,
                    'ik_error_max_m': ik, 'root_yaw_deg': yaw_of(bone_matrix(rig, 'root', depsgraph)) - yaw_of(root_m0),
                    'feet_min_z_m': feet_low, 'proxy_min_z_m': proxy_low}
        if row['measure'] == 'HEAD_BACK':
            measured['head_back_max_m'] = head_back
        if row['measure'] == 'HAND_FORWARD':
            measured['hand_forward_m'] = hand0[side].y - world(rig, 'hand.' + side, depsgraph, 'tail').y
        if row['measure'] == 'HAND_LOWERED':
            measured['hand_back_m'] = world(rig, 'hand.' + side, depsgraph, 'tail').y - hand0[side].y
        if row['measure'] == 'HEAD_YAW':
            measured['head_yaw_deg'] = yaw_of(bone_matrix(rig, 'head', depsgraph)) - yaw_of(head_m0)
        if row['measure'] == 'HANDS_RAISED':
            measured['hands_rise_m'] = min(world(rig, 'hand.' + k, depsgraph, 'tail').z - hand0[k].z for k in 'LR')
        if row.get('cyclic_relative_to_root'):
            rel1 = {b: world(rig, b, depsgraph) - root1 for b in rel0}
            measured['cycle_pose_max_m'] = max((rel1[b] - rel0[b]).length for b in rel0)
        rows[row['name']] = measured
        base = kind + ' ' + row['name']
        check(base + ': keys real and exactly in its own range', len(keys) > 0 and measured['keyed_range'] == [start, end]
              and row['keyed_range'] == [start, end], measured['keyed_range'])
        check(base + ': slot bound to the rig and fake user kept', measured['slot'] == 'OB' + rig.name and action.use_fake_user,
              measured['slot'])
        check(base + ': IK reaches its foot targets every frame', ik <= THRESHOLDS['ik_error_max_m'], ik)
        check(base + ': feet never go under the ground (whole-proxy minimum reported)',
              feet_low >= -THRESHOLDS['ground_contact_max_m'], {'feet_min_z_m': feet_low, 'proxy_min_z_m': proxy_low})
        if row['measure'] == 'ROOT_MOTION':
            check(base + ': root motion measured in metres towards -Y',
                  abs(measured['root_motion_m'] - row['root_motion_planned_m']) <= THRESHOLDS['root_motion_tolerance_m']
                  and measured['root_direction_dot_minus_y'] >= THRESHOLDS['root_direction_min_dot'],
                  {'measured_m': measured['root_motion_m'], 'planned_m': row['root_motion_planned_m'],
                   'dot': measured['root_direction_dot_minus_y']})
        else:
            check(base + ': root stays in place', measured['root_motion_m'] <= THRESHOLDS['static_root_max_m'],
                  measured['root_motion_m'])
        if row['measure'] == 'ROOT_YAW':
            check(base + ': turn measured on the root', abs(measured['root_yaw_deg'] - row['yaw_deg']) <= THRESHOLDS['turn_tolerance_deg'],
                  {'measured_deg': measured['root_yaw_deg'], 'planned_deg': row['yaw_deg']})
        if row['measure'] == 'HEAD_BACK':
            check(base + ': startle throws the head back (+Y)', head_back >= THRESHOLDS['react_head_back_min_m'], head_back)
        if row['measure'] == 'HAND_FORWARD':
            check(base + ': reaching hand moves forward (-Y)', measured['hand_forward_m'] >= THRESHOLDS['reach_forward_min_m'],
                  measured['hand_forward_m'])
        if row['measure'] == 'HAND_LOWERED':
            check(base + ': lowering hand comes back (+Y) from the reach', measured['hand_back_m'] >= THRESHOLDS['reach_forward_min_m'],
                  measured['hand_back_m'])
        if row['measure'] == 'HEAD_YAW':
            check(base + ': head oriented towards +X and held', abs(measured['head_yaw_deg'] - row['head_yaw_deg'])
                  <= THRESHOLDS['stare_yaw_tolerance_deg'], measured['head_yaw_deg'])
        if row['measure'] == 'HANDS_RAISED':
            check(base + ': both hands raised', measured['hands_rise_m'] >= THRESHOLDS['attack_hands_rise_min_m'],
                  {'measured_m': measured['hands_rise_m'], 'creature_scale': s})
        if row.get('cyclic_relative_to_root'):
            check(base + ': cycle start and end poses match relative to the root',
                  measured['cycle_pose_max_m'] <= THRESHOLDS['cycle_pose_max_m'], measured['cycle_pose_max_m'])
    check(kind + ' ' + actor['name'] + ': every Action is distinct', len(set(digests.values())) == len(digests), digests)
    rig.animation_data.action = original
    scene.frame_set(scene.frame_start)
    return rows


def rigid_follow(scene, rig, child, bone, frames):
    """Max change of a child (object origin or first evaluated vertex) expressed in the bone's space."""
    samples = []
    for frame in frames:
        depsgraph = at(scene, frame)
        matrix = bone_matrix(rig, bone, depsgraph).inverted()
        evaluated = child.evaluated_get(depsgraph)
        if child.type == 'MESH' and child.modifiers:
            mesh = evaluated.to_mesh()
            point = evaluated.matrix_world @ mesh.vertices[0].co
            evaluated.to_mesh_clear()
        else:
            point = evaluated.matrix_world.translation
        samples.append(matrix @ point)
    return max((p - samples[0]).length for p in samples)


def validate_scene(kind, manifest, scene):
    measured = {}
    objects = list(scene.objects)
    scales = [o.name for o in objects if any(abs(v - 1) > THRESHOLDS['scale_tolerance'] for v in list(o.scale) + list(o.delta_scale))]
    check(kind + ': every object has unit scale and unit delta scale (M8 guard)', not scales and scene.unit_settings.scale_length == 1.0,
          scales)
    lights = [o.name for o in objects if o.type == 'LIGHT']
    check(kind + ': no production lights in the fixture', not lights, lights)
    duplicates = [o.name for o in objects if re.search(r'\.\d{3}$', o.name)]
    check(kind + ': no auto-suffixed duplicate names', not duplicates, duplicates)
    keys = []
    for obj in objects:
        if obj.type == 'MESH' and obj.data.shape_keys:
            keys += [[obj.name, k.name, k.value] for k in obj.data.shape_keys.key_blocks[1:]]
    nonzero = [k for k in keys if k[2] != 0.0]
    measured['shape_keys_checked'] = len(keys)
    check(kind + ': no shape key left at 1.0 (all non-reference keys at exactly 0)', not nonzero, {'checked': len(keys), 'nonzero': nonzero[:5]})
    check(kind + ': fps, range and units match the manifest', scene.render.fps == manifest['fps'] and scene.render.fps_base == 1.0
          and [scene.frame_start, scene.frame_end] == manifest['frame_range'] and scene.unit_settings.system == 'METRIC',
          [scene.render.fps, scene.frame_start, scene.frame_end])
    expected_objects = sorted(o.name for o in objects)
    check(kind + ': manifest object list equals the saved scene', expected_objects == manifest['objects'], len(expected_objects))
    must_close = [a['body'] for a in manifest['actors']] + [s['object'] for a in manifest['actors'] for s in a['secondary']]
    must_close += [p['object'] for p in manifest['props']] + list(manifest['environment']['objects'].get('rocks', []))
    must_close += list((manifest['environment']['objects'].get('cave') or {}).values())
    must_close += [p['object'] for p in manifest['environment']['practicals']]
    open_meshes = [n for n in must_close if not closed(bpy.data.objects[n])]
    check(kind + ': proxies, secondary pieces, props, rocks and walls are closed meshes', not open_meshes,
          {'checked': len(must_close), 'open': open_meshes})
    depsgraph = at(scene, manifest['frame_range'][0])
    for actor in manifest['actors']:
        rig = bpy.data.objects[actor['object']]
        body = bpy.data.objects[actor['body']]
        vertices, _ = mesh_world(body, depsgraph)
        groups = {body.vertex_groups[b].index for b in actor['bones']['feet'].values()}
        feet = [vertices[v.index].z for v in body.data.vertices if any(g.group in groups and g.weight > 0 for g in v.groups)]
        low = min(feet)
        # The whole-proxy minimum is reported, not judged: a crouched pose tilts the rigid shin rings, whose lower
        # edge then dips under the ground (measured on the creature); only the foot segments define the contact.
        measured[actor['name'] + '_proxy_min_z_m'] = min(v.z for v in vertices)
        measured[actor['name'] + '_feet_min_z_m'] = low
        check(kind + ' ' + actor['name'] + ': proxy feet on the ground at frame 1', abs(low - manifest['ground_z_m'])
              <= THRESHOLDS['ground_contact_max_m'], {'feet_min_z_m': low, 'proxy_min_z_m': min(v.z for v in vertices)})
        check(kind + ' ' + actor['name'] + ': rig is the actor (no parent, no object constraints) and names are prefixed',
              rig.parent is None and not rig.constraints and all(n.startswith(actor['name'] + '_') or n == actor['name']
                                                                  for n in actor['meshes']), actor['meshes'])
        bones = actor['bones']
        explicit = [bones['root'], bones['head'], bones['pelvis'], bones['chest']] + list(bones['hands'].values()) + list(bones['feet'].values())
        check(kind + ' ' + actor['name'] + ': explicit head/hands/root/feet bones exist', all(b in rig.data.bones for b in explicit), explicit)
    actors = manifest['actors']
    for index, first in enumerate(actors):
        for second in actors[index + 1:]:
            # World-space BVH (FromObject is local-space: measured false positives in the M10 API probe).
            a_vertices, a_faces = [], []
            b_vertices, b_faces = [], []
            for names, vertices, faces in ((first['meshes'], a_vertices, a_faces), (second['meshes'], b_vertices, b_faces)):
                for name in names:
                    points, polygons = mesh_world(bpy.data.objects[name], depsgraph)
                    offset = len(vertices)
                    vertices += points
                    faces += [tuple(i + offset for i in p) for p in polygons]
            tree = BVHTree.FromPolygons(b_vertices, b_faces)
            pairs = BVHTree.FromPolygons(a_vertices, a_faces).overlap(tree)
            clearance = min(tree.find_nearest(v)[3] for v in a_vertices)
            label = first['name'] + '/' + second['name']
            measured[label + '_rest_clearance_m'] = clearance
            check(kind + ' ' + label + ': actors do not interpenetrate at frame 1', not pairs,
                  {'overlap_pairs': len(pairs), 'clearance_m': clearance})
    return measured


def validate_face(kind, scene, actor):
    rig = bpy.data.objects[actor['object']]
    face = actor['face']['objects']
    original = rig.animation_data.action
    rig.animation_data.action = bpy.data.actions[actor['name'] + '_Walk']
    frames = [1, 21, 41, 61]
    eye = rigid_follow(scene, rig, bpy.data.objects[face['eye_L']], 'head', frames)
    skin = rigid_follow(scene, rig, bpy.data.objects[face['face']], 'head', frames)
    neck = rigid_follow(scene, rig, bpy.data.objects[face['neck']], 'chest', frames)
    rig.animation_data.action = original
    scene.frame_set(scene.frame_start)
    check(kind + ' ' + actor['name'] + ': face, eyes and neck ride on the body during root motion',
          max(eye, skin, neck) <= THRESHOLDS['rigid_follow_max_m'], {'eye_m': eye, 'face_m': skin, 'neck_m': neck})
    check(kind + ' ' + actor['name'] + ': face shape keys exist and are named per actor',
          actor['face']['shape_key_count'] >= 30 and actor['face']['shape_key_owner'] == face['face'] + '_Keys',
          actor['face']['shape_key_count'])
    return {'eye_follow_m': eye, 'face_follow_m': skin, 'neck_follow_m': neck}


def validate_secondary(kind, scene, actor, row):
    piece = bpy.data.objects[row['object']]
    body = bpy.data.objects[actor['body']]
    depsgraph = at(scene, scene.frame_start)
    a_vertices, a_faces = mesh_world(piece, depsgraph)
    b_vertices, b_faces = mesh_world(body, depsgraph)
    pairs = BVHTree.FromPolygons(a_vertices, a_faces).overlap(BVHTree.FromPolygons(b_vertices, b_faces))
    tree = BVHTree.FromPolygons(b_vertices, b_faces)
    clearance = min(tree.find_nearest(v)[3] for v in a_vertices)
    check(kind + ' ' + piece.name + ': M8-compatible pendant (bone parent, mesh, no modifiers, no shape keys)',
          piece.type == 'MESH' and piece.parent == bpy.data.objects[actor['object']] and piece.parent_type == 'BONE'
          and piece.parent_bone == 'chest' and not piece.modifiers and piece.data.shape_keys is None,
          {'parent_bone': piece.parent_bone, 'modifiers': [m.type for m in piece.modifiers]})
    owners = {c.name for c in piece.users_collection}
    actor_owners = {c.name for o in (bpy.data.objects[actor['object']], body) for c in o.users_collection}
    check(kind + ' ' + piece.name + ': own collection, shared with neither the rig nor the body (excluding it from a '
          'cinematic must not re-link them)', not owners & actor_owners,
          {'piece': sorted(owners), 'actor': sorted(actor_owners)})
    check(kind + ' ' + piece.name + ': no initial penetration with the body proxy', len(pairs) == 0,
          {'overlap_pairs': len(pairs), 'clearance_m': clearance})
    return {'overlap_pairs': len(pairs), 'clearance_m': clearance}


def validate_props(kind, scene, manifest):
    measured = {}
    for row in manifest['props']:
        prop = bpy.data.objects[row['object']]
        held = row['held_by']
        if held:
            rig = bpy.data.objects[[a for a in manifest['actors'] if a['name'] == held['actor']][0]['object']]
            depsgraph = at(scene, held['frame'])
            evaluated = rig.evaluated_get(depsgraph)
            bone = evaluated.pose.bones[held['bone']]
            grip = evaluated.matrix_world @ bone.head.lerp(bone.tail, .6)
            distance = (prop.matrix_world.translation - grip).length
            measured[prop.name + '_grip_distance_m'] = distance
            check(kind + ' ' + prop.name + ': prop grip sits on ' + held['actor'] + ' ' + held['bone'] + ' at frame 1, free object',
                  distance <= THRESHOLDS['grip_distance_max_m'] and prop.parent is None and not prop.constraints, distance)
    return measured


def validate_environment(kind, scene, manifest):
    env = manifest['environment']
    if env['source_camera']:
        cam = bpy.data.objects[env['source_camera']['object']]
        check(kind + ': source camera is the scene camera', scene.camera == cam, cam.name)
    for row in env['practicals']:
        prop = bpy.data.objects[row['object']]
        strengths = [m.node_tree.nodes['Principled BSDF'].inputs['Emission Strength'].default_value
                     for m in prop.data.materials if m and m.node_tree and m.node_tree.nodes.get('Principled BSDF')]
        check(kind + ': practical lantern is emissive geometry, not a light', max(strengths) > 0 and prop.type == 'MESH', strengths)
    if env['dark_zone']:
        names = list(env['objects']['cave'].values())
        check(kind + ': dark cave walls and roof exist', all(n in bpy.data.objects for n in names), names)


def candidate_smoke(kind, manifest, bridge, storage):
    """The demo is consumable by the M10 foundation: register actors, create the candidate scene, reject it and
    verify the source fingerprint is restored. Calls lifecycle directly so this probe does not import the subsystem
    handler modules other agents are still writing."""
    from blender_mcp.production.cinematic import lifecycle
    from blender_mcp.production.cinematic.service import Context, Request, dispatch
    register_open_fixture(bridge, storage, OUT)
    ids = {}
    roles = (('root', 'root'), ('pelvis', 'pelvis'), ('head', 'head'), ('hand_left', ('hands', 'L')),
             ('hand_right', ('hands', 'R')), ('foot_left', ('feet', 'L')), ('foot_right', ('feet', 'R')))
    for actor in manifest['actors']:
        # Real registry records: the candidate re-measures registered actors and refuses hand-written ones (ACTOR_STALE).
        bones = {role: (actor['bones'][ref] if isinstance(ref, str) else actor['bones'][ref[0]][ref[1]]) for role, ref in roles}
        record = dispatch('cinematic_register_actor', bridge, storage, 'm10fx-register-' + actor['name'],
                          object_name=actor['object'], options={'role': actor['role'], 'bones': bones})
        ids[actor['name']] = record['actor_id']
    brief = fx.brief_hint(fx.demo_layout(kind), ids)
    before = bridge.run(lifecycle.SOURCE_INFO, {'source_scene': manifest['scene']})['fingerprint']
    created = lifecycle.create_candidate(Context(bridge, storage, 'm10fx-' + kind + '-create'),
                                         Request(brief=brief, options={}, apply=True))
    candidate = bpy.data.scenes.get(created['scene'])
    check(kind + ': M10 foundation creates a candidate over the whole demo',
          candidate is not None and created['linked_objects'] == len(manifest['objects'])
          and created['timeline']['frame_end'] == manifest['frame_range'][1],
          {'linked': created['linked_objects'], 'objects': len(manifest['objects']), 'timeline': created['timeline']})
    rejected = lifecycle.reject(Context(bridge, storage, 'm10fx-' + kind + '-reject'),
                                Request(cinematic=created['cinematic'], options={}, apply=True))
    after = bridge.run(lifecycle.SOURCE_INFO, {'source_scene': manifest['scene']})['fingerprint']
    check(kind + ': rejection restores the source fingerprint', rejected['source_restored'] is True and before == after,
          {'removed': rejected['removed'], 'undo_failed': rejected['undo_failed']})
    return before


def render_check(kind, manifest, bridge, fingerprint):
    """Workbench check render at the middle frame through a temporary own camera; everything is restored and the
    source fingerprint compared afterwards. Workbench rasterises on the GPU over OpenGL: abort if it is not the
    NVIDIA card (user rule: never render on CPU silently)."""
    from blender_mcp.production.cinematic import lifecycle
    scene = bpy.context.scene
    render, shading = scene.render, scene.display.shading
    saved = {'engine': render.engine, 'res': (render.resolution_x, render.resolution_y, render.resolution_percentage),
             'camera': scene.camera, 'frame': scene.frame_current, 'filepath': render.filepath,
             'light': shading.light, 'color_type': shading.color_type, 'background': shading.background_type,
             'format': render.image_settings.file_format}
    frame = (manifest['frame_range'][0] + manifest['frame_range'][1]) // 2
    depsgraph = at(scene, frame)
    points = []
    for actor in manifest['actors']:
        for name in actor['meshes']:
            points += mesh_world(bpy.data.objects[name], depsgraph)[0]
    for row in manifest['props']:
        points += mesh_world(bpy.data.objects[row['object']], depsgraph)[0]
    low = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    high = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    center, radius = (low + high) / 2, (high - low).length / 2
    data = bpy.data.cameras.new('PROBE_CheckCam_Data')
    data.lens = 35.0
    cam = bpy.data.objects.new('PROBE_CheckCam', data)
    scene.collection.objects.link(cam)
    direction = Vector((.35, -1.0, .30)).normalized()
    cam.location = center + direction * (radius / math.sin(math.atan(18 * 9 / 16 / 35.0)) * 1.15)
    cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()
    path = OUT / 'renders' / (kind + '_f%04d.png' % frame)
    path.parent.mkdir(exist_ok=True)
    started = time.perf_counter()
    try:
        scene.camera = cam
        render.engine = 'BLENDER_WORKBENCH'
        render.resolution_x, render.resolution_y, render.resolution_percentage = 640, 360, 100
        render.image_settings.file_format = 'PNG'
        shading.light, shading.color_type, shading.background_type = 'STUDIO', 'MATERIAL', 'WORLD'
        render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        import gpu
        renderer = gpu.platform.renderer_get()
        if 'NVIDIA' not in renderer:
            raise RuntimeError('GPU_REQUIRED: Workbench rendered on ' + renderer + ', not the RTX 4060; renders aborted')
    finally:
        scene.camera = saved['camera']
        render.engine = saved['engine']
        render.resolution_x, render.resolution_y, render.resolution_percentage = saved['res']
        render.filepath = saved['filepath']
        render.image_settings.file_format = saved['format']
        shading.light, shading.color_type, shading.background_type = saved['light'], saved['color_type'], saved['background']
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(data)
        scene.frame_set(saved['frame'])
    seconds = time.perf_counter() - started
    image = bpy.data.images.load(str(path), check_existing=False)
    size = list(image.size)
    pixels = [0.0] * (size[0] * size[1] * 4)
    image.pixels.foreach_get(pixels)
    bpy.data.images.remove(image)
    lit = sum(1 for i in range(0, len(pixels), 4) if max(pixels[i:i + 3]) > .1) / (size[0] * size[1])
    after = bridge.run(lifecycle.SOURCE_INFO, {'source_scene': manifest['scene']})['fingerprint']
    row = {'kind': kind, 'path': str(path), 'frame': frame, 'size': size, 'seconds': seconds, 'gpu': renderer,
           'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'pixels_above_0_1': lit}
    REPORT['renders'].append(row)
    check(kind + ': check render written on the NVIDIA GPU with visible subjects', path.is_file() and size == [640, 360]
          and lit > .01, row)
    check(kind + ': check render restored the source scene exactly', after == fingerprint, {'before': fingerprint, 'after': after})
    return seconds


def run_demo(kind, bridge, storage):
    timings = {}
    started = time.perf_counter()
    manifest = fx.build_demo(kind, OUT / kind)
    timings['build_and_save_s'] = time.perf_counter() - started
    blend = Path(manifest['blend'])
    on_disk = json.loads((OUT / kind / (kind + '.json')).read_text(encoding='utf-8'))
    check(kind + ': .blend and manifest written, sha256 matches the saved file',
          blend.is_file() and hashlib.sha256(blend.read_bytes()).hexdigest() == manifest['sha256'] == on_disk['sha256']
          and not bpy.data.is_dirty, {'bytes': manifest['bytes'], 'sha256': manifest['sha256']})
    try:
        fx.build_demo(kind, OUT / kind)
        check(kind + ': a second build into the same folder is refused', False, 'unexpected success')
    except FileExistsError as exc:
        check(kind + ': a second build into the same folder is refused', True, str(exc)[:200])
    scene = bpy.context.scene
    started = time.perf_counter()
    measured = validate_scene(kind, manifest, scene)
    validate_environment(kind, scene, manifest)
    measured['actions'] = {}
    for actor in manifest['actors']:
        measured['actions'][actor['name']] = measure_actions(kind, scene, actor)
        if actor['face']:
            measured[actor['name'] + '_face'] = validate_face(kind, scene, actor)
        for row in actor['secondary']:
            measured[row['object']] = validate_secondary(kind, scene, actor, row)
    measured.update(validate_props(kind, scene, manifest))
    timings['validate_s'] = time.perf_counter() - started
    started = time.perf_counter()
    fingerprint = candidate_smoke(kind, manifest, bridge, storage)
    timings['candidate_smoke_s'] = time.perf_counter() - started
    timings['render_s'] = render_check(kind, manifest, bridge, fingerprint)
    measured['blend_bytes'] = manifest['bytes']
    measured['objects'] = len(manifest['objects'])
    REPORT['measured'][kind] = measured
    REPORT['metrics'][kind] = timings
    save_report()


def main():
    global OUT
    args = sys.argv[sys.argv.index('--') + 1:]
    OUT = Path(args[0]).resolve()
    OUT.mkdir(parents=True, exist_ok=False)  # a new folder per run: nothing is ever overwritten
    kinds = args[1].split(',') if len(args) > 1 else list(fx.DEMO_KINDS)
    bridge = LocalBridge()
    storage = Storage(OUT / 'store')
    started = time.perf_counter()
    for kind in kinds:
        try:
            run_demo(kind, bridge, storage)
        except Exception as exc:
            REPORT['errors'].append({'kind': kind, 'error': repr(exc), 'traceback': traceback.format_exc()})
            traceback.print_exc()
            save_report()
            if 'GPU_REQUIRED' in str(exc):
                break
    REPORT['metrics']['total_s'] = time.perf_counter() - started
    REPORT['metrics']['bridge_calls'] = bridge.calls
    REPORT['metrics']['bridge_payload_bytes'] = bridge.bytes
    save_report()
    print('SUMMARY ' + json.dumps(REPORT['summary']), flush=True)


main()
