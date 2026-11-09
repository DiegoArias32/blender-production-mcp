"""M10 measured cinematic QA: composition, continuity, visibility, actor collisions, contacts and motion.

One multi-actor sampler runs inside Blender: one `frame_set` per frame in the candidate scene (inside
`temp_override(scene=candidate)`), evaluated values only, the active camera of that frame (markers switch it — measured),
projection with `cm_camera_matrices`/`cm_project` (depth > 0 required to count as framed), and a joint capture/restore of
every animated object, camera/light data, exposure and `scene.camera`. The payload verifies candidate ownership and the
recorded fingerprints BEFORE the first frame_set, and proves the restoration with a state digest taken before and after.

Everything Blender measures travels as compact per-frame rows; every judgement (findings, intervals, per-shot summaries)
is a pure host function so it can be tested without Blender. Thresholds are PROPOSALS, labelled as such and overridable;
there is no quality score anywhere. Technical findings and creative observations are separate lists.

Two targets:
- PLAN mode (`cinematic` reference): the candidate scene of an assembled plan, its actors, shots and cameras;
- SCENE mode (no reference): a named scene (default: the context scene) with explicit actors — a transient read for
  previs or for inspecting a candidate under controlled conditions. No freshness gate exists there (no plan to compare).
"""
from __future__ import annotations

from copy import deepcopy
import math
import statistics

from ..common.asset_safety import normalize
from ..contacts.runtime import CONTACT_RUNTIME
from ..physics.qa import RUNTIME as PQ_RUNTIME
from . import model, registry, store
from .runtime import COMMON
from .service import handler

SCHEMA = 'production.cinematic.qa.{kind}.v1'
KINDS = ('composition', 'continuity', 'visibility', 'collisions', 'contacts', 'motion')
TOOL_KIND = {'cinematic_analyze_composition': 'composition', 'cinematic_validate_continuity': 'continuity',
             'cinematic_analyze_visibility': 'visibility', 'cinematic_analyze_actor_collisions': 'collisions',
             'cinematic_analyze_contacts': 'contacts', 'cinematic_analyze_motion': 'motion'}
MAX_FRAMES = 600
MAX_LIMIT = 200
ANALYSABLE_STATES = frozenset({'CINEMATIC_CANDIDATE', 'ASSEMBLED', 'REVIEWED', 'ACCEPTED'})
SEVERITY_ORDER = {'ERROR': 0, 'WARNING': 1, 'INFO': 2}
NO_HEAD_RULE_SHOTS = frozenset({'EXTREME_CLOSE_UP', 'INSERT'})

# Proposed minimum projected height (fraction of frame height) of a subject's evaluated bounds per shot type. They are
# starting points for review, not cinematography constants; INSERT/CUSTOM have no actor-size expectation.
SHOT_MIN_HEIGHT = {'ESTABLISHING': 0.03, 'WIDE': 0.15, 'MEDIUM': 0.4, 'CLOSE_UP': 0.7, 'EXTREME_CLOSE_UP': 0.9,
                   'OVER_THE_SHOULDER': 0.3, 'LOW_ANGLE': 0.2, 'HIGH_ANGLE': 0.2, 'TRACKING': 0.15, 'REACTION': 0.4,
                   'INSERT': None, 'CUSTOM': None}

PROPOSALS = {
    'composition': {'centered_tolerance': 0.05, 'thirds_tolerance': 0.05, 'tight_headroom': 0.02, 'overlap_ratio': 0.5,
                    'min_lead_room': 0.2, 'large_negative_space': 0.9, 'direction_min_speed': 0.002,
                    'min_screen_height': SHOT_MIN_HEIGHT},
    'continuity': {'jump_cut_min_angle_deg': 30.0, 'jump_cut_size_ratio': 0.2, 'screen_jump': 0.4,
                   'direction_min_speed': 0.002, 'min_shot_frames': 6, 'axis_degenerate_m': 0.05},
    'visibility': {'min_visible_fraction': 0.5, 'min_occlusion_frames': 2, 'prop_occlusion_fraction': 0.25,
                   'min_screen_height': SHOT_MIN_HEIGHT},
    'collisions': {'tolerance_depth_m': 0.005, 'error_depth_m': 0.02, 'contact_penetration_m': 0.01,
                   'proximity_margin_m': 0.05, 'contact_window_margin_frames': 2, 'max_vertex_frames': 3_000_000},
    'contacts': {'contact_distance_m': 0.02, 'timing_tolerance_frames': 2, 'sliding_tolerance_m': 0.01,
                 'max_penetration_m': 0.005, 'gaze_tolerance_deg': 15.0, 'window_margin_frames': 6,
                 'query_distance_m': 0.3, 'trend_tolerance_m': 0.05, 'max_vertex_frames': 3_000_000},
    'motion': {'jump_ratio': 5.0, 'root_jump_m': 0.2, 'camera_jump_m': 0.5, 'camera_rotation_jump_deg': 20.0,
               'lens_jump_mm': 5.0, 'accel_ratio': 6.0, 'min_accel_m_s2': 20.0, 'light_jump_ratio': 0.5,
               'exposure_jump_ev': 0.5},
}

COMMON_OPTIONS = {'frame_start', 'frame_end', 'step', 'actors', 'actor_bones', 'scene', 'shots', 'thresholds', 'detail',
                  'limit', 'offset'}
OPTIONS = {
    'composition': COMMON_OPTIONS,
    'continuity': COMMON_OPTIONS | {'allow_axis_break', 'axis', 'gaze'},
    'visibility': COMMON_OPTIONS | {'ray_points', 'intended_occlusions', 'facial_events'},
    'collisions': COMMON_OPTIONS | {'props', 'events'},
    'contacts': COMMON_OPTIONS | {'events', 'support_surfaces', 'props'},
    'motion': COMMON_OPTIONS | {'events', 'idle_actors', 'cache_windows'},
}
FLAGS = {
    'composition': {'camera': True, 'screen': True},
    'continuity': {'camera': True, 'screen': True, 'world': True, 'rays': True},
    'visibility': {'camera': True, 'screen': True, 'rays': True},
    'collisions': {'collide': True},
    # Contacts also survey every actor/prop pair (penetration + clearance) so unplanned contacts are found too.
    'contacts': {'events': True, 'collide': True},
    'motion': {'camera': True, 'motion': True},
}
DENSE_KINDS = frozenset({'motion'})
INTERACTION_MODE = {'REACH': 'CONTACT', 'TOUCH_SURFACE': 'CONTACT', 'HOLD_PROP': 'CONTACT', 'HANDOFF_PROP': 'CONTACT',
                    'PREPARE_ATTACK': 'ATTACK', 'APPROACH': 'PROXIMITY', 'RETREAT': 'PROXIMITY', 'GAZE': 'GAZE',
                    'OBSERVE': 'GAZE', 'TURN_TOWARD': 'GAZE', 'REACT': 'TIMING', 'SUPPORT': 'SUPPORT',
                    'CONTACT': 'CONTACT'}
CONTACT_KINDS = frozenset({'REACH', 'TOUCH_SURFACE', 'HOLD_PROP', 'HANDOFF_PROP', 'CONTACT', 'SUPPORT'})

# ----------------------------------------------------------------------------------------------------------------
# Blender payload (safe mode: no lambda, no bpy_extras, literal getattr only). Composed after the M8 collision runtime
# (pq_snapshot / closed_triangles / pq_collision_frame / pq_parity) and the M3 static contact runtime
# (contact_static_surfaces / contact_query); the linker keeps only what ca_run reaches.
# ----------------------------------------------------------------------------------------------------------------
_PAYLOAD = r'''
from mathutils.bvhtree import BVHTree

def ca_r(value):
    return round(float(value), 6)

def ca_v(values):
    return [round(float(v), 6) for v in values]

def ca_target(params):
    # Plan mode: ownership and freshness are verified BEFORE the first frame_set, so a candidate edited outside the
    # cinematic tools is never sampled.
    if params.get('generation_id'):
        scene, src, cin = cm_candidate(params)
        prints = cm_fingerprints(params)
        expected = params.get('expected_fingerprints') or {}
        if expected.get('source') and prints['source'] != expected['source']:
            cm_fail('CINEMATIC_STALE_SOURCE', 'the source scene changed outside the cinematic tools; nothing was sampled')
        if expected.get('candidate') and prints['candidate'] != expected['candidate']:
            cm_fail('CINEMATIC_STALE_CANDIDATE', 'the candidate scene changed outside the cinematic tools; nothing was sampled')
        return scene, cin, prints
    scene = cm_scene(params['scene_name']) if params.get('scene_name') else bpy.context.scene
    return scene, None, {'scene': cm_source_fingerprint(scene)['fingerprint']}

def ca_marker_frame(row):
    return row[1]

def ca_markers(scene):
    rows = [[m.name, m.frame, m.camera.name if m.camera else None] for m in scene.timeline_markers]
    return sorted(rows, key=ca_marker_frame)

def ca_frames(params, scene):
    plan = params['frames']
    start = plan['start'] if plan.get('start') is not None else scene.frame_start
    end = plan['end'] if plan.get('end') is not None else scene.frame_end
    if end < start:
        cm_fail('CINEMATIC_RANGE_INVALID', 'frame_end ' + str(end) + ' < frame_start ' + str(start))
    step = plan.get('step')
    source = 'OPTIONS' if step else 'DEFAULT'
    extra = [int(f) for f in plan.get('extra') or []]
    if plan.get('cuts'):
        for name, frame, camera in ca_markers(scene):
            if camera is not None:
                extra += [frame - 2, frame - 1, frame, frame + 1]
    if plan.get('list') is not None:
        frames = sorted(set([int(f) for f in plan['list']] + extra))
        frames = [f for f in frames if start <= f <= end]
        source = 'EXPLICIT_WINDOWS'
    else:
        count = end - start + 1
        if not step:
            step = 1
            if count > plan['max']:
                if plan.get('dense'):
                    cm_fail('CINEMATIC_DENSE_WINDOW_REQUIRED', str(count) + ' frames need dense sampling; pass frame_start/frame_end spanning at most ' + str(plan['max']) + ' frames')
                step = (count + plan['max'] - 1) // plan['max']
                source = 'AUTO_CAPACITY'
        frames = list(range(start, end + 1, step))
        if frames[-1] != end:
            frames.append(end)
        frames = sorted(set(frames + [f for f in extra if start <= f <= end]))
    if not frames:
        cm_fail('CINEMATIC_SAMPLING_EMPTY', 'no frame to sample in [' + str(start) + ', ' + str(end) + ']')
    if len(frames) > plan['max']:
        cm_fail('CINEMATIC_SAMPLING_CAPACITY', str(len(frames)) + ' frames requested; the sampler is bounded to ' + str(plan['max']))
    return frames, {'frame_start': start, 'frame_end': end, 'step': step if plan.get('list') is None else None,
                    'step_source': source, 'frames_sampled': len(frames)}

def ca_bone_ok(obj, name):
    return obj.type == 'ARMATURE' and obj.data.bones.get(name) is not None

def ca_actor_meshes(scene, obj, names):
    if names:
        return [cm_object(n, ['MESH']) for n in names]
    if obj.type == 'MESH':
        return [obj]
    found = []
    for item in scene.objects:
        if item.type != 'MESH' or item == obj:
            continue
        if item.parent == obj or any(m.type == 'ARMATURE' and m.object == obj for m in item.modifiers):
            found.append(item)
    return sorted(found, key=cm_sort_name)

def ca_actors(scene, params):
    actors = []
    for spec in params['actors']:
        obj = cm_object(spec['object'])
        for key in ('head_bone', 'root_bone'):
            if spec.get(key) and not ca_bone_ok(obj, spec[key]):
                cm_fail('CINEMATIC_ACTOR_BONE_MISSING', obj.name + ' has no bone ' + str(spec[key]))
        meshes = ca_actor_meshes(scene, obj, spec.get('meshes'))
        in_scene = scene.objects.get(obj.name) is not None
        hidden = obj.hide_render if not meshes else all(m.hide_render for m in meshes)
        actors.append({'actor_id': spec['actor_id'], 'object': obj, 'meshes': meshes, 'in_scene': in_scene,
                       'hidden': bool(hidden), 'head_bone': spec.get('head_bone'), 'root_bone': spec.get('root_bone'),
                       'names': set([obj.name] + [m.name for m in meshes]),
                       'meshes_source': 'DECLARED' if spec.get('meshes') else 'DISCOVERED'})
    return actors

def ca_props(scene, cin, params):
    props = []
    names = list(params.get('props') or [])
    if cin is not None:
        for obj in sorted(cin.all_objects, key=cm_sort_name):
            role = str(obj.get('production_m10_role') or '')
            if obj.type == 'MESH' and 'prop' in role.lower() and obj.name not in names:
                names.append(obj.name)
    for name in names:
        obj = cm_object(name, ['MESH'])
        if scene.objects.get(obj.name) is not None:
            props.append(obj)
    return props

def ca_hidden_collections(scene):
    # scene.ray_cast still hits hide_render objects (measured): render visibility is filtered by hand.
    hidden = set()
    pending = [scene.collection]
    while pending:
        collection = pending.pop()
        for child in collection.children:
            if child.hide_render:
                for obj in child.all_objects:
                    hidden.add(obj.name)
            else:
                pending.append(child)
    return hidden

def ca_render_hidden(obj, hidden):
    return obj.name in hidden or obj.hide_render or not obj.visible_camera

def ca_classify(obj, hidden, actors, classes):
    if obj.name in classes:
        return
    owner = None
    for actor in actors:
        if obj.name in actor['names']:
            owner = actor['actor_id']
    classes[obj.name] = {'type': obj.type, 'role': obj.get('production_m10_role'), 'actor_id': owner,
                         'owned': obj.get('production_m10_owner') is not None, 'render_hidden': ca_render_hidden(obj, hidden)}

def ca_capture_objects(scene):
    objects = []
    for obj in scene.objects:
        keys = obj.type == 'MESH' and obj.data.shape_keys is not None
        if obj.animation_data is not None or obj.type in ('ARMATURE', 'CAMERA', 'LIGHT') or keys:
            objects.append(obj)
    if len(objects) > 1024:
        cm_fail('CINEMATIC_SAMPLING_CAPACITY', str(len(objects)) + ' animated objects exceed the 1024 capture bound')
    return objects

def ca_data_state(scene, objects):
    # Camera/light data and scene exposure are flushed by frame_set like object channels; they are restored too.
    cameras = {}
    lights = {}
    for obj in objects:
        data = obj.data
        if obj.type == 'CAMERA':
            cameras[data.name] = [data.lens, data.shift_x, data.shift_y, data.clip_start, data.clip_end, data.ortho_scale,
                                  data.sensor_width, data.sensor_height, data.dof.focus_distance, data.dof.aperture_fstop]
        elif obj.type == 'LIGHT':
            row = [data.energy, list(data.color), data.shadow_soft_size]
            if data.type == 'SPOT':
                row += [data.spot_size, data.spot_blend]
            lights[data.name] = row
    world = None
    if scene.world is not None and scene.world.node_tree is not None:
        world = [[n.name, list(n.inputs['Color'].default_value), n.inputs['Strength'].default_value]
                 for n in scene.world.node_tree.nodes if n.type == 'BACKGROUND']
    return {'cameras': cameras, 'lights': lights, 'world': world, 'exposure': scene.view_settings.exposure,
            'gamma': scene.view_settings.gamma}

def ca_data_restore(scene, state):
    for name, row in state['cameras'].items():
        data = bpy.data.cameras.get(name)
        if data is None:
            continue
        now = [data.lens, data.shift_x, data.shift_y, data.clip_start, data.clip_end, data.ortho_scale,
               data.sensor_width, data.sensor_height, data.dof.focus_distance, data.dof.aperture_fstop]
        if now != row:
            data.lens = row[0]
            data.shift_x = row[1]
            data.shift_y = row[2]
            data.clip_start = row[3]
            data.clip_end = row[4]
            data.ortho_scale = row[5]
            data.sensor_width = row[6]
            data.sensor_height = row[7]
            data.dof.focus_distance = row[8]
            data.dof.aperture_fstop = row[9]
    for name, row in state['lights'].items():
        data = bpy.data.lights.get(name)
        if data is None:
            continue
        if data.energy != row[0]:
            data.energy = row[0]
        if list(data.color) != row[1]:
            data.color = row[1]
        if data.shadow_soft_size != row[2]:
            data.shadow_soft_size = row[2]
        if data.type == 'SPOT' and len(row) == 5:
            if data.spot_size != row[3]:
                data.spot_size = row[3]
            if data.spot_blend != row[4]:
                data.spot_blend = row[4]
    if state['world'] is not None and scene.world is not None:
        for name, color, strength in state['world']:
            node = scene.world.node_tree.nodes.get(name)
            if node is None:
                continue
            if list(node.inputs['Color'].default_value) != color:
                node.inputs['Color'].default_value = color
            if node.inputs['Strength'].default_value != strength:
                node.inputs['Strength'].default_value = strength
    if scene.view_settings.exposure != state['exposure']:
        scene.view_settings.exposure = state['exposure']
    if scene.view_settings.gamma != state['gamma']:
        scene.view_settings.gamma = state['gamma']

def ca_state_digest(scene, objects):
    rows = [scene.frame_current, scene.frame_subframe, scene.camera.name if scene.camera else None]
    for obj in objects:
        pose = {}
        if obj.type == 'ARMATURE' and obj.pose is not None:
            pose = {p.name: m3_channels(p) for p in obj.pose.bones}
        keys = None
        if obj.type == 'MESH' and obj.data.shape_keys is not None:
            keys = [obj.data.shape_keys.eval_time] + [k.value for k in obj.data.shape_keys.key_blocks]
        rows.append([obj.name, m3_channels(obj), pose, keys])
    rows.append(ca_data_state(scene, objects))
    return m2_digest(rows)

def ca_static(scene, cin, actors):
    coverage = {}
    for actor in actors:
        ad = actor['object'].animation_data
        rows = []
        use_nla = None
        if ad is not None:
            use_nla = ad.use_nla
            if ad.action is not None and ad.action_influence > 0.0:
                rows.append(['ACTIVE', ad.action.name, ca_r(ad.action.frame_range[0]), ca_r(ad.action.frame_range[1]),
                             ad.action_extrapolation, None])
            if ad.use_nla:
                solo = [t for t in ad.nla_tracks if t.is_solo]
                for track in (solo if solo else list(ad.nla_tracks)):
                    if track.mute:
                        continue
                    for strip in track.strips:
                        if not strip.mute:
                            rows.append(['STRIP', strip.action.name if strip.action else None, ca_r(strip.frame_start),
                                         ca_r(strip.frame_end), strip.extrapolation, track.name])
        coverage[actor['actor_id']] = {'use_nla': use_nla, 'rows': rows}
    caches = []
    if cin is not None:
        for obj in sorted(cin.all_objects, key=cm_sort_name):
            for mod in obj.modifiers:
                if mod.type == 'MESH_CACHE':
                    caches.append({'object': obj.name, 'modifier': mod.name, 'frame_start': ca_r(mod.frame_start),
                                   'frame_scale': ca_r(mod.frame_scale), 'play_mode': mod.play_mode,
                                   'show_render': mod.show_render})
    lights = sorted(o.name for o in scene.objects if o.type == 'LIGHT')[:64]
    return {'markers': ca_markers(scene), 'coverage': coverage, 'caches': caches, 'lights': lights}

def ca_root(actor, ev):
    if actor['root_bone']:
        bone = ev.pose.bones[actor['root_bone']]
        matrix = ev.matrix_world @ bone.matrix
        return ev.matrix_world @ bone.head, matrix.to_3x3().normalized().to_quaternion()
    return ev.matrix_world.translation.copy(), ev.matrix_world.to_3x3().normalized().to_quaternion()

def ca_geometry(actor, dg):
    obj = actor['object']
    ev = obj.evaluated_get(dg)
    corners = []
    for mesh in actor['meshes']:
        me = mesh.evaluated_get(dg)
        matrix = me.matrix_world
        for corner in me.bound_box:
            corners.append(matrix @ mathutils.Vector((corner[0], corner[1], corner[2])))
    if not corners and obj.type == 'ARMATURE' and ev.pose is not None:
        for bone in list(ev.pose.bones)[:64]:
            corners.append(ev.matrix_world @ bone.head)
            corners.append(ev.matrix_world @ bone.tail)
    if not corners:
        corners.append(ev.matrix_world.translation.copy())
    low = [min(c[i] for c in corners) for i in range(3)]
    high = [max(c[i] for c in corners) for i in range(3)]
    center = mathutils.Vector(((low[0] + high[0]) * 0.5, (low[1] + high[1]) * 0.5, (low[2] + high[2]) * 0.5))
    root, root_rotation = ca_root(actor, ev)
    if actor['head_bone']:
        bone = ev.pose.bones[actor['head_bone']]
        head = ev.matrix_world @ ((bone.head + bone.tail) * 0.5)
    else:
        head = mathutils.Vector((center[0], center[1], high[2] - 0.1 * (high[2] - low[2])))
    return {'corners': corners, 'low': low, 'high': high, 'center': center, 'root': root, 'root_rotation': root_rotation,
            'head': head}

def ca_camera(scene, dg, unit, need_projection):
    cam = scene.camera
    if cam is None:
        return None, None
    ev = cam.evaluated_get(dg)
    matrix = ev.matrix_world
    basis = matrix.to_3x3().normalized()
    forward = basis @ mathutils.Vector((0.0, 0.0, -1.0))
    up = basis @ mathutils.Vector((0.0, 1.0, 0.0))
    row = {'name': cam.name, 'type': cam.type, 'position': ca_v(matrix.translation * unit), 'forward': ca_v(forward),
           'up': ca_v(up), 'rotation': ca_v(basis.to_quaternion())}
    if cam.type != 'CAMERA':
        return row, None
    data = ev.data
    row['lens'] = ca_r(data.lens)
    row['clip'] = [ca_r(data.clip_start * unit), ca_r(data.clip_end * unit)]
    row['projection'] = data.type
    if not need_projection:
        return row, None
    # cm_camera_matrices calls calc_matrix_camera with the depsgraph, which reads the evaluated camera parameters.
    projection, view = cm_camera_matrices(cam, scene, dg)
    return row, {'projection': projection, 'view': view, 'clip': (data.clip_start, data.clip_end),
                 'origin': matrix.translation.copy()}

def ca_screen(geo, proj, unit):
    front = 0
    inside = 0
    xs = []
    ys = []
    depths = []
    for point in geo['corners']:
        q = cm_project(proj['projection'], proj['view'], point)
        if q is None or q[2] <= 0.0:
            continue
        front += 1
        depths.append(q[2])
        xs.append(q[0])
        ys.append(q[1])
        if 0.0 <= q[0] <= 1.0 and 0.0 <= q[1] <= 1.0:
            inside += 1
    total = len(geo['corners'])
    row = {'n': total, 'front': front, 'inside': inside, 'bbox': None, 'depth': None, 'near': False, 'far': False}
    if front:
        row['bbox'] = ca_v([min(xs), min(ys), max(xs), max(ys)])
        row['depth'] = [ca_r(min(depths) * unit), ca_r(max(depths) * unit)]
        # Bounds straddling the camera plane or closer than clip_start are cut by the near plane (conservative test).
        row['near'] = min(depths) < proj['clip'][0] or front < total
        row['far'] = max(depths) > proj['clip'][1]
    for key in ('center', 'head', 'root'):
        q = cm_project(proj['projection'], proj['view'], geo[key])
        row[key] = None if q is None else [ca_r(q[0]), ca_r(q[1]), ca_r(q[2] * unit)]
    row['distance'] = ca_r((geo['center'] - proj['origin']).length * unit)
    return row

def ca_surface_points(actor, dg, count):
    points = []
    meshes = actor['meshes']
    if not meshes:
        return points
    share = max(1, count // len(meshes))
    for mesh in meshes:
        ev = mesh.evaluated_get(dg)
        data = ev.to_mesh()
        try:
            total = len(data.vertices)
            if total:
                stride = max(1, total // share)
                matrix = ev.matrix_world
                for index in range(0, total, stride)[:share]:
                    points.append(matrix @ data.vertices[index].co)
        finally:
            ev.to_mesh_clear()
    return points

def ca_ray(scene, dg, origin, target, names, hidden, skipped, actors, classes):
    direction = target - origin
    total = direction.length
    if total < 1e-9:
        return None
    direction.normalize()
    start = origin.copy()
    travelled = 0.0
    for attempt in range(16):
        remaining = total - travelled
        if remaining <= 1e-6:
            return None
        hit, location, normal, face, obj, matrix = scene.ray_cast(dg, start, direction, distance=remaining)
        if not hit or obj is None:
            return None
        if obj.name in names:
            return None
        step = (location - start).length
        if ca_render_hidden(obj, hidden):
            ca_classify(obj, hidden, actors, classes)
            skipped[obj.name] = skipped.get(obj.name, 0) + 1
            start = location + direction * 1e-4
            travelled += step + 1e-4
            continue
        if travelled + step >= total * (1.0 - 1e-4):
            return None
        ca_classify(obj, hidden, actors, classes)
        return obj.name
    return '__RAY_LIMIT__'

def ca_visibility(scene, dg, actor, geo, proj, params, hidden, skipped, actors, classes):
    origin = proj['origin']
    rays = 0
    clear = 0
    occluders = {}
    for point in ca_surface_points(actor, dg, params['ray_points']):
        q = cm_project(proj['projection'], proj['view'], point)
        if q is None or q[2] <= 0.0 or not (0.0 <= q[0] <= 1.0 and 0.0 <= q[1] <= 1.0):
            continue
        rays += 1
        blocker = ca_ray(scene, dg, origin, point, actor['names'], hidden, skipped, actors, classes)
        if blocker is None:
            clear += 1
        else:
            occluders[blocker] = occluders.get(blocker, 0) + 1
    q = cm_project(proj['projection'], proj['view'], geo['head'])
    head = 'VISIBLE'
    head_by = None
    if q is None or q[2] <= 0.0:
        head = 'BEHIND'
    elif not (0.0 <= q[0] <= 1.0 and 0.0 <= q[1] <= 1.0):
        head = 'OUT_OF_FRAME'
    else:
        head_by = ca_ray(scene, dg, origin, geo['head'], actor['names'], hidden, skipped, actors, classes)
        if head_by is not None:
            head = 'OCCLUDED'
    return {'rays': rays, 'clear': clear, 'occ': occluders, 'head': head, 'head_by': head_by}

def ca_gaze(spec, actors_by_id, geos, dg, proj, unit):
    actor = actors_by_id.get(spec['actor_id'])
    if actor is None or actor['object'].type != 'ARMATURE':
        return None
    ev = actor['object'].evaluated_get(dg)
    bone = ev.pose.bones.get(spec['head_bone'])
    if bone is None:
        cm_fail('CINEMATIC_GAZE_BONE_MISSING', spec['head_bone'])
    origin = ev.matrix_world @ ((bone.head + bone.tail) * 0.5)
    forward = (ev.matrix_world.to_3x3() @ bone.matrix.to_3x3() @ mathutils.Vector(spec['forward_axis'])).normalized()
    target = ca_target_point(spec, actors_by_id, geos, dg, unit)
    row = {'g': spec['index'], 'angle': None, 'screen': None}
    if target is not None and (target - origin).length > 1e-9:
        row['angle'] = ca_r(math.degrees(forward.angle(target - origin, 0.0)))
    if proj is not None:
        a = cm_project(proj['projection'], proj['view'], origin)
        b = cm_project(proj['projection'], proj['view'], origin + forward * 0.25 / unit)
        if a is not None and b is not None and a[2] > 0.0 and b[2] > 0.0:
            row['screen'] = [ca_r(b[0] - a[0]), ca_r(b[1] - a[1])]
    return row

def ca_target_point(spec, actors_by_id, geos, dg, unit):
    if spec.get('target_actor_id'):
        geo = geos.get(spec['target_actor_id'])
        if geo is None:
            target = actors_by_id.get(spec['target_actor_id'])
            if target is None:
                return None
            geo = ca_geometry(target, dg)
        return geo['head']
    if spec.get('target_object'):
        obj = cm_object(spec['target_object'])
        return obj.evaluated_get(dg).matrix_world.translation.copy()
    if spec.get('target_point'):
        # Options speak metres; the scene works in Blender units.
        return mathutils.Vector(spec['target_point']) / unit
    return None

def ca_box(points):
    return ([min(p[i] for p in points) for i in range(3)], [max(p[i] for p in points) for i in range(3)])

def ca_box_gap(a, b):
    gap = 0.0
    for i in range(3):
        separation = max(a[0][i] - b[1][i], b[0][i] - a[1][i], 0.0)
        gap = max(gap, separation)
    return gap

def ca_record(name, snapshot):
    # Same record contract as pq_collider_records (closed via closed_triangles, bbox, three-ray parity BVH), built
    # from the snapshot already taken so each mesh is evaluated once per frame; the open-mesh tree is kept for
    # nearest-distance (clearance) only, never for inside/outside.
    points = snapshot['points']
    finite = all(math.isfinite(v) for point in points for v in point)
    closed = finite and closed_triangles(snapshot['triangles'])
    tree = BVHTree.FromPolygons([Vector(p) for p in points], snapshot['triangles'], all_triangles=True) if finite and snapshot['triangles'] else None
    return {'name': name, 'closed': closed, 'triangle_count': len(snapshot['triangles']),
            'bbox_min': tuple(min(p[i] for p in points) for i in range(3)),
            'bbox_max': tuple(max(p[i] for p in points) for i in range(3)),
            'bvh': tree if closed else None, 'tree': tree}

def ca_entities(actors, props):
    rows = []
    for actor in actors:
        if actor['in_scene'] and actor['meshes']:
            rows.append({'id': actor['actor_id'], 'kind': 'ACTOR', 'meshes': actor['meshes']})
    for prop in props:
        rows.append({'id': 'prop:' + prop.name, 'kind': 'PROP', 'meshes': [prop]})
    return rows

def ca_clearance(points, records):
    best = None
    for record in records:
        if record['tree'] is None:
            continue
        for point in points:
            nearest, normal, face, distance = record['tree'].find_nearest(Vector(point))
            if nearest is not None and (best is None or distance < best):
                best = distance
    return best

def ca_collide(entities, params):
    points = {}
    records = {}
    boxes = {}
    for entity in entities:
        entity_points = []
        entity_records = []
        for mesh in entity['meshes']:
            snapshot = pq_snapshot(mesh, params['max_vertices'], params['max_triangles'])
            entity_records.append(ca_record(mesh.name, snapshot))
            entity_points.extend(snapshot['points'])
        points[entity['id']] = entity_points
        records[entity['id']] = entity_records
        boxes[entity['id']] = ca_box(entity_points)
    rows = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            a = entities[i]['id']
            b = entities[j]['id']
            if entities[i]['kind'] == 'PROP' and entities[j]['kind'] == 'PROP':
                continue
            gap = ca_box_gap(boxes[a], boxes[b])
            if gap > params['proximity_margin_m']:
                continue
            ab = pq_collision_frame(points[a], records[b], params['pq_thresholds'], 0.0)
            ba = pq_collision_frame(points[b], records[a], params['pq_thresholds'], 0.0)
            colliders = ab['colliders'] + ba['colliders']
            unknown = sorted(set(c['collider'] for c in colliders if c['status'] == 'UNKNOWN'))
            count = ab['penetration_count'] + ba['penetration_count']
            depth = max(ab['max_depth_m'] or 0.0, ba['max_depth_m'] or 0.0)
            hit = sorted(set(c['collider'] for c in colliders if c['status'] == 'MEASURED' and c['penetration_count']))
            errors = sorted(set(e for e in ab['errors'] + ba['errors']
                                if not e.startswith('PENETRATION_') and not e.startswith('OPEN_OR_NONMANIFOLD')))
            clearance = None
            if count == 0:
                values = [v for v in (ca_clearance(points[a], records[b]), ca_clearance(points[b], records[a])) if v is not None]
                clearance = min(values) if values else None
            rows.append([a, b, count, ca_r(depth), hit, unknown, errors,
                         ca_r(clearance) if clearance is not None else None, ca_r(gap)])
    return rows

def ca_point(ev, spec):
    if spec.get('bone'):
        bone = ev.pose.bones.get(spec['bone']) if ev.pose is not None else None
        if bone is None:
            cm_fail('CINEMATIC_EVENT_BONE_MISSING', ev.name + ' has no bone ' + spec['bone'])
        end = spec.get('end') or 'TAIL'
        local = bone.tail if end == 'TAIL' else bone.head if end == 'HEAD' else (bone.head + bone.tail) * 0.5
        return ev.matrix_world @ local, ev.matrix_world @ bone.matrix
    return ev.matrix_world.translation.copy(), ev.matrix_world.copy()

def ca_target_records(meshes, params):
    rows = []
    for mesh in meshes:
        rows.append(ca_record(mesh.name, pq_snapshot(mesh, params['max_vertices'], params['max_triangles'])))
    return rows

def ca_event(event, frame, dg, unit, actors_by_id, params, surfaces):
    actor = actors_by_id[event['actor_id']]
    ev = actor['object'].evaluated_get(dg)
    root, rotation = ca_root(actor, ev)
    row = {'e': event['index'], 'f': frame, 'root': ca_v(root * unit)}
    target = event['target']
    target_actor = actors_by_id.get(target.get('actor_id')) if target.get('actor_id') else None
    if target_actor is not None:
        tev = target_actor['object'].evaluated_get(dg)
        troot, trot = ca_root(target_actor, tev)
        row['troot'] = ca_v(troot * unit)
    elif target.get('object'):
        tobj = cm_object(target['object'])
        row['troot'] = ca_v(tobj.evaluated_get(dg).matrix_world.translation * unit)
    if event['mode'] == 'GAZE':
        head = ev.pose.bones.get(event['head_bone']) if ev.pose is not None else None
        if head is None:
            cm_fail('CINEMATIC_GAZE_BONE_MISSING', str(event['head_bone']))
        origin = ev.matrix_world @ ((head.head + head.tail) * 0.5)
        forward = (ev.matrix_world.to_3x3() @ head.matrix.to_3x3() @ mathutils.Vector(event['forward_axis'])).normalized()
        if target_actor is not None:
            point = ca_geometry(target_actor, dg)['head']
        elif target.get('object'):
            point = cm_object(target['object']).evaluated_get(dg).matrix_world.translation.copy()
        else:
            point = mathutils.Vector(target['point']) / unit
        row['angle'] = ca_r(math.degrees(forward.angle(point - origin, 0.0))) if (point - origin).length > 1e-9 else None
        return row
    if event['mode'] in ('PROXIMITY', 'TIMING'):
        return row
    point, frame_matrix = ca_point(ev, event['effector'])
    point_m = point * unit
    row['eff'] = ca_v(point_m)
    if target.get('surfaces'):
        hit = contact_query(point_m, surfaces, {'query_distance_m': params['query_distance_m']}, bool(target.get('generic')))
        if hit is None:
            row['d'] = None
            row['why'] = 'NO_SURFACE_IN_QUERY_DISTANCE'
        else:
            signed = hit['signed_distance_m']
            row['d'] = ca_r(abs(signed))
            row['inside'] = signed < 0.0
            row['depth'] = ca_r(-signed) if signed < 0.0 else 0.0
            row['local'] = ca_v(point_m)
            row['surface'] = hit['surface_object']
        return row
    if target_actor is not None and target.get('bone'):
        tpoint, tmatrix = ca_point(tev, target)
        row['d'] = ca_r((point - tpoint).length * unit)
        row['inside'] = None
        row['local'] = ca_v((tmatrix.inverted() @ point) * unit)
        return row
    if target_actor is not None:
        meshes = target_actor['meshes']
        tmatrix = tev.matrix_world
    else:
        tobj = cm_object(target['object'])
        meshes = [tobj] if tobj.type == 'MESH' else []
        tmatrix = tobj.evaluated_get(dg).matrix_world
    row['local'] = ca_v((tmatrix.inverted() @ point) * unit)
    if not meshes:
        row['d'] = ca_r((point - tmatrix.translation).length * unit)
        row['inside'] = None
        row['why'] = 'TARGET_HAS_NO_MESH_ORIGIN_DISTANCE'
        return row
    records = ca_target_records(meshes, params)
    distance = ca_clearance([tuple(point_m)], records)
    closed = [r for r in records if r['closed']]
    inside = None
    depth = None
    if len(closed) == len(records):
        measured = pq_collision_frame([tuple(point_m)], closed, params['pq_thresholds'], 0.0)
        inside = measured['penetration_count'] > 0
        depth = measured['max_depth_m'] if inside else 0.0
    row['d'] = 0.0 if inside else (ca_r(distance) if distance is not None else None)
    row['surface_distance'] = ca_r(distance) if distance is not None else None
    row['inside'] = inside
    row['depth'] = ca_r(depth) if depth is not None else None
    return row

def ca_motion(scene, dg, unit, actors):
    roots = {}
    for actor in actors:
        if actor['in_scene']:
            root, rotation = ca_root(actor, actor['object'].evaluated_get(dg))
            roots[actor['actor_id']] = ca_v(list(root * unit) + list(rotation))
    lights = {}
    for obj in scene.objects:
        if obj.type == 'LIGHT' and len(lights) < 64:
            lights[obj.name] = ca_r(obj.evaluated_get(dg).data.energy)
    world = None
    if scene.world is not None and scene.world.node_tree is not None:
        world = [ca_r(n.inputs['Strength'].default_value) for n in scene.world.node_tree.nodes if n.type == 'BACKGROUND']
    return {'roots': roots, 'lights': lights, 'exposure': ca_r(scene.view_settings.exposure), 'world': world}

def ca_vertex_frames(entities, frames):
    total = 0
    for entity in entities:
        for mesh in entity['meshes']:
            total += len(mesh.data.vertices)
    return total * len(frames)

# Per-kind setup (before the capture) and per-frame sampler. ca_run is specialised by NAME per kind so the linker
# ships only what that analysis reaches: safe mode forbids calling a function held in a variable.

def ca_setup_view(scene, cin, actors, frames, params):
    markers = ca_markers(scene)
    if params.get('ray_frames_add_cuts') and params.get('ray_frames') is not None:
        params['ray_frames'] = sorted(set(params['ray_frames'] + [m[1] for m in markers if m[2] is not None]))
    return {'static': {'markers': markers}, 'hidden': ca_hidden_collections(scene), 'skipped': {}, 'classes': {}}

def ca_setup_collide(scene, cin, actors, frames, params):
    entities = ca_entities(actors, ca_props(scene, cin, params))
    capacity = {'vertex_frames': ca_vertex_frames(entities, frames), 'limit': params['max_vertex_frames'],
                'limit_source': 'PROPOSAL'}
    if capacity['vertex_frames'] > params['max_vertex_frames']:
        cm_fail('CINEMATIC_COLLISION_CAPACITY', str(capacity['vertex_frames']) + ' vertex-frames exceed '
                + str(params['max_vertex_frames']) + '; narrow the window or raise thresholds.max_vertex_frames')
    return {'static': {'markers': ca_markers(scene)}, 'entities': entities, 'capacity': capacity}

def ca_setup_contacts(scene, cin, actors, frames, params):
    context = ca_setup_collide(scene, cin, actors, frames, params)
    context['surfaces'] = []
    if params.get('support_surfaces'):
        try:
            context['surfaces'] = contact_static_surfaces(params['support_surfaces'])
        except ValueError as exc:
            cm_fail('CINEMATIC_SUPPORT_SURFACE_INVALID', str(exc))
    return context

def ca_setup_motion(scene, cin, actors, frames, params):
    return {'static': ca_static(scene, cin, actors)}

def ca_sample_view(scene, dg, frame, unit, actors, context, params):
    flags = params['flags']
    row = {'f': frame}
    row['cam'], proj = ca_camera(scene, dg, unit, True)
    actors_by_id = {}
    geos = {}
    for actor in actors:
        actors_by_id[actor['actor_id']] = actor
        if actor['in_scene']:
            geos[actor['actor_id']] = ca_geometry(actor, dg)
    rays = flags.get('rays') and (params.get('ray_frames') is None or frame in params['ray_frames'])
    screen = {}
    for actor in actors:
        geo = geos.get(actor['actor_id'])
        if geo is None or proj is None:
            screen[actor['actor_id']] = None
            continue
        item = ca_screen(geo, proj, unit)
        if flags.get('world'):
            item['w'] = {'center': ca_v(geo['center'] * unit), 'root': ca_v(geo['root'] * unit),
                         'head': ca_v(geo['head'] * unit)}
        if rays:
            item['vis'] = ca_visibility(scene, dg, actor, geo, proj, params, context['hidden'], context['skipped'],
                                        actors, context['classes'])
        screen[actor['actor_id']] = item
    row['a'] = screen
    if params.get('gaze'):
        row['gz'] = [ca_gaze(spec, actors_by_id, geos, dg, proj, unit) for spec in params['gaze']]
    return row

def ca_sample_collide(scene, dg, frame, unit, actors, context, params):
    return {'f': frame, 'pairs': ca_collide(context['entities'], params)}

def ca_sample_contacts(scene, dg, frame, unit, actors, context, params):
    actors_by_id = {}
    for actor in actors:
        actors_by_id[actor['actor_id']] = actor
    events = [ca_event(e, frame, dg, unit, actors_by_id, params, context['surfaces']) for e in params['events']
              if e['window'][0] <= frame <= e['window'][1]]
    return {'f': frame, 'pairs': ca_collide(context['entities'], params), 'ev': events}

def ca_sample_motion(scene, dg, frame, unit, actors, context, params):
    row = {'f': frame}
    row['cam'], proj = ca_camera(scene, dg, unit, False)
    row['m'] = ca_motion(scene, dg, unit, actors)
    return row
'''

_RUN_TEMPLATE = r'''
def ca_run(params):
    scene, cin, before = ca_target(params)
    source = cm_scene(params['source_scene']) if params.get('source_scene') else scene
    source_before = [source.frame_current, source.frame_subframe, source.camera.name if source.camera else None]
    unit = scene.unit_settings.scale_length
    if not math.isfinite(unit) or unit <= 0.0:
        cm_fail('CINEMATIC_UNIT_SCALE_INVALID', str(unit))
    render = scene.render
    with bpy.context.temp_override(scene=scene):
        frames, coverage = ca_frames(params, scene)
        actors = ca_actors(scene, params)
        context = __SETUP__(scene, cin, actors, frames, params)
        objects = ca_capture_objects(scene)
        baseline = cm_capture(scene, objects)
        data_state = ca_data_state(scene, objects)
        camera_before = scene.camera
        digest_before = ca_state_digest(scene, objects)
        rows = []
        try:
            for frame in frames:
                scene.frame_set(frame)
                # The candidate's own view layer: objects that live only in the candidate (owned cameras, prop
                # copies) keep a stale matrix_world until that layer is evaluated (measured, Blender 5.2.1).
                dg = cm_depsgraph(scene)
                rows.append(__SAMPLE__(scene, dg, frame, unit, actors, context, params))
        finally:
            # One frame_set then every channel (cm_restore), then camera/light data, exposure and scene.camera, which
            # frame_set may have switched through a camera marker.
            cm_restore(scene, objects, baseline)
            ca_data_restore(scene, data_state)
            if scene.camera != camera_before:
                scene.camera = camera_before
            bpy.context.view_layer.update()
        digest_after = ca_state_digest(scene, objects)
    if params.get('generation_id'):
        after = cm_fingerprints(params)
    else:
        after = {'scene': cm_source_fingerprint(scene)['fingerprint']}
    source_after = [source.frame_current, source.frame_subframe, source.camera.name if source.camera else None]
    actor_rows = {}
    for actor in actors:
        actor_rows[actor['actor_id']] = {'object': actor['object'].name, 'meshes': [m.name for m in actor['meshes']],
                                         'meshes_source': actor['meshes_source'], 'in_scene': actor['in_scene'],
                                         'hidden_in_render': actor['hidden'],
                                         'root_source': 'ROOT_BONE' if actor['root_bone'] else 'OBJECT_ORIGIN',
                                         'head_source': 'HEAD_BONE' if actor['head_bone'] else 'BOUNDS_TOP_PROXY'}
    entities = context.get('entities') or []
    return {'scene': scene.name, 'fps': render.fps / render.fps_base, 'unit_scale': unit,
            'resolution': [render.resolution_x * render.resolution_percentage / 100.0,
                           render.resolution_y * render.resolution_percentage / 100.0],
            'coverage': coverage, 'frames': frames, 'actors': actor_rows, 'static': context['static'], 'rows': rows,
            'hidden_skipped': context.get('skipped') or {}, 'objects': context.get('classes') or {},
            'capacity': context.get('capacity') or {},
            'entities': [[e['id'], e['kind'], [m.name for m in e['meshes']]] for e in entities],
            'restoration': {'state_digest_before': digest_before, 'state_digest_after': digest_after,
                            'restored': digest_before == digest_after and source_before == source_after,
                            'source_before': source_before, 'source_after': source_after},
            'fingerprints_before': before, 'fingerprints_after': after, 'filepath': bpy.data.filepath}
'''

ENTRIES = {'composition': ('ca_setup_view', 'ca_sample_view'), 'continuity': ('ca_setup_view', 'ca_sample_view'),
           'visibility': ('ca_setup_view', 'ca_sample_view'), 'collisions': ('ca_setup_collide', 'ca_sample_collide'),
           'contacts': ('ca_setup_contacts', 'ca_sample_contacts'), 'motion': ('ca_setup_motion', 'ca_sample_motion')}
RUNTIME = PQ_RUNTIME + CONTACT_RUNTIME + COMMON + _PAYLOAD
BODIES = {kind: RUNTIME + _RUN_TEMPLATE.replace('__SETUP__', setup).replace('__SAMPLE__', sample)
          + '\nresult = ca_run(params)\n' for kind, (setup, sample) in ENTRIES.items()}


# ----------------------------------------------------------------------------------------------------------------
# Host: options, targets and frame plans.
# ----------------------------------------------------------------------------------------------------------------

def fail(code, detail):
    raise ValueError(code + ': ' + detail)


def _integer(value, field, minimum=-1_048_574, maximum=1_048_574):
    return model.number(value, field, minimum, maximum, integer=True)


def thresholds_for(kind, values=None):
    """Proposed thresholds with per-key provenance. Unknown keys are refused; overrides are recorded."""
    merged = deepcopy(PROPOSALS[kind])
    overrides = []
    if values is None:
        return merged, overrides
    if not isinstance(values, dict):
        fail('CINEMATIC_THRESHOLDS_INVALID', 'thresholds must be an object')
    unknown = sorted(set(values) - set(merged))
    if unknown:
        fail('CINEMATIC_OPTION_UNSUPPORTED', 'unknown thresholds ' + ', '.join(unknown))
    for key, value in values.items():
        if key == 'min_screen_height':
            if not isinstance(value, dict) or set(value) - set(model.SHOT_TYPES):
                fail('CINEMATIC_THRESHOLDS_INVALID', 'min_screen_height maps SHOT_TYPES to numbers or null')
            table = dict(merged[key])
            for shot_type, height in value.items():
                table[shot_type] = None if height is None else model.number(height, 'min_screen_height.' + shot_type, 0, 10)
            merged[key] = table
        elif key in {'min_shot_frames', 'min_occlusion_frames', 'timing_tolerance_frames', 'window_margin_frames',
                     'contact_window_margin_frames', 'max_vertex_frames'}:
            merged[key] = _integer(value, 'thresholds.' + key, 0, 100_000_000)
        else:
            merged[key] = model.number(value, 'thresholds.' + key, 0, 1e9)
        overrides.append(key)
    return merged, sorted(overrides)


def _page(opts):
    limit = _integer(opts.get('limit', 50), 'limit', 1, MAX_LIMIT)
    offset = _integer(opts.get('offset', 0), 'offset', 0, 1_000_000)
    detail = opts.get('detail', 'summary')
    if detail not in ('summary', 'samples'):
        fail('CINEMATIC_CHOICE_INVALID', 'detail must be summary or samples')
    return limit, offset, detail


def _frame_plan(kind, opts, timeline, extra=(), explicit=None, cuts=False):
    plan = {'max': MAX_FRAMES, 'dense': kind in DENSE_KINDS, 'cuts': cuts}
    start = opts.get('frame_start', timeline.get('frame_start') if timeline else None)
    end = opts.get('frame_end', timeline.get('frame_end') if timeline else None)
    plan['start'] = None if start is None else _integer(start, 'frame_start')
    plan['end'] = None if end is None else _integer(end, 'frame_end')
    if plan['start'] is not None and plan['end'] is not None and plan['end'] < plan['start']:
        fail('CINEMATIC_RANGE_INVALID', 'frame_end < frame_start')
    if timeline and plan['start'] is not None and not (timeline['frame_start'] <= plan['start'] and plan['end'] <= timeline['frame_end']):
        fail('CINEMATIC_RANGE_OUTSIDE_TIMELINE', 'the analysis window must lie inside the cinematic timeline ['
             + str(timeline['frame_start']) + ', ' + str(timeline['frame_end']) + ']')
    step = opts.get('step')
    if step is not None:
        step = _integer(step, 'step', 1, 1000)
        if kind in DENSE_KINDS and step != 1:
            fail('CINEMATIC_DENSE_SAMPLING_REQUIRED', kind + ' measures frame-to-frame derivatives; step must be 1')
    plan['step'] = step
    plan['extra'] = sorted(set(int(f) for f in extra))
    plan['list'] = sorted(set(int(f) for f in explicit)) if explicit is not None else None
    if plan['list'] is not None and len(plan['list']) > MAX_FRAMES:
        fail('CINEMATIC_SAMPLING_CAPACITY', str(len(plan['list'])) + ' frames requested; bound ' + str(MAX_FRAMES))
    return plan


def _known_bone(value):
    # The actor registry writes the string 'UNKNOWN' for roles it could not resolve (interaction_capabilities.bones):
    # that is a missing bone, never a bone name to look up in Blender.
    return isinstance(value, str) and bool(value) and value != 'UNKNOWN'


def _bones_for(actor_id, record, overrides):
    """Explicit options first, then registry keys. Bones are never guessed from names."""
    explicit = (overrides or {}).get(actor_id) or {}
    if explicit and (not isinstance(explicit, dict) or set(explicit) - {'head_bone', 'root_bone'}):
        fail('CINEMATIC_OPTION_UNSUPPORTED', 'actor_bones.' + actor_id + ' accepts head_bone, root_bone')
    bones = {}
    source = {}
    registry_bones = record.get('bones') if record and isinstance(record.get('bones'), dict) else {}
    for key, alias in (('head_bone', 'head'), ('root_bone', 'root')):
        if explicit.get(key):
            bones[key] = model.text(explicit[key], 'actor_bones.' + key, 63)
            source[key] = 'OPTIONS'
        elif record and _known_bone(record.get(key)):
            bones[key] = record[key]
            source[key] = 'REGISTRY'
        elif _known_bone(registry_bones.get(alias)):
            bones[key] = registry_bones[alias]
            source[key] = 'REGISTRY'
        else:
            bones[key] = None
            source[key] = 'NONE'
    return bones, source


def _plan_actors(ctx, plan, opts, state=None):
    bones_opt = opts.get('actor_bones')
    if bones_opt is not None and not isinstance(bones_opt, dict):
        fail('CINEMATIC_OPTION_INVALID', 'actor_bones must map actor_id to {head_bone, root_bone}')
    known = {a['actor_id'] for a in plan['actors']}
    for actor_id in (bones_opt or {}):
        if actor_id not in known:
            fail('ACTOR_NOT_IN_PLAN', actor_id)
    actors = []
    for row in plan['actors']:
        try:
            record = registry.find(ctx.storage, plan['actor_registry_key'], row['actor_id'])
        except ValueError:
            record = None
        bones, source = _bones_for(row['actor_id'], record, bones_opt)
        actors.append({'actor_id': row['actor_id'], 'object': row['object'], 'meshes': store.actor_meshes(row, state),
                       'role': row.get('role'), 'head_bone': bones['head_bone'], 'root_bone': bones['root_bone'],
                       'bones_source': source})
    return actors


def _scene_actors(req, opts):
    specs = opts.get('actors')
    if specs is None and req.object_name:
        specs = [req.object_name]
    if not specs:
        fail('CINEMATIC_ACTORS_REQUIRED', 'scene mode needs object_name or options.actors')
    if not isinstance(specs, list) or len(specs) > model.MAX_ACTORS:
        fail('CINEMATIC_ACTORS_INVALID', 'actors must be a list of at most ' + str(model.MAX_ACTORS))
    actors = []
    for index, spec in enumerate(specs):
        if isinstance(spec, str):
            spec = {'object': spec}
        spec = model.closed(spec, {'object', 'actor_id', 'meshes', 'head_bone', 'root_bone'})
        name = model.text(spec.get('object'), 'actors[' + str(index) + '].object', 63)
        meshes = spec.get('meshes') or []
        if not isinstance(meshes, list) or len(meshes) > 16 or any(not isinstance(m, str) for m in meshes):
            fail('CINEMATIC_ACTORS_INVALID', 'meshes must be a list of at most 16 names')
        actor_id = spec.get('actor_id') or name
        bones, source = _bones_for(actor_id, {k: spec[k] for k in ('head_bone', 'root_bone') if spec.get(k)},
                                   opts.get('actor_bones'))
        actors.append({'actor_id': actor_id, 'object': name, 'meshes': meshes, 'role': None,
                       'head_bone': bones['head_bone'], 'root_bone': bones['root_bone'], 'bones_source': source})
    if len({a['actor_id'] for a in actors}) != len(actors):
        fail('CINEMATIC_ACTORS_INVALID', 'actor ids must be unique')
    return actors


def normalize_shots(rows, actor_ids, cameras=None):
    """Plan or explicit shot rows -> [{shot_id, frame_start, frame_end, shot_type, camera_id, camera_object, subjects,
    axis}]. Unknown subject ids are dropped (reported), an empty subject list means every actor."""
    shots = []
    dropped = []
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            fail('CINEMATIC_SHOTS_INVALID', 'shot rows must be objects')
        start = _integer(row.get('frame_start'), 'shots[' + str(index) + '].frame_start')
        end = _integer(row.get('frame_end'), 'shots[' + str(index) + '].frame_end')
        if end < start:
            fail('CINEMATIC_SHOTS_INVALID', 'shot ' + str(row.get('shot_id')) + ' ends before it starts')
        subjects = row.get('subjects') or row.get('actors') or ([row['subject']] if row.get('subject') else [])
        if not isinstance(subjects, list):
            subjects = [subjects]
        kept = [s for s in subjects if s in actor_ids]
        dropped += [s for s in subjects if s not in actor_ids]
        shot_type = row.get('shot_type') or row.get('type')
        camera_id = row.get('camera_id')
        shots.append({'shot_id': str(row.get('shot_id') or 'SHOT_' + str(index + 1)), 'frame_start': start,
                      'frame_end': end, 'shot_type': shot_type if shot_type in model.SHOT_TYPES else 'CUSTOM',
                      'camera_id': camera_id,
                      'camera_object': ((cameras or {}).get(camera_id) or {}).get('object') if camera_id else None,
                      'subjects': kept or list(actor_ids), 'subjects_declared': bool(kept),
                      'axis': row.get('axis') if isinstance(row.get('axis'), dict) else None})
    shots.sort(key=lambda s: (s['frame_start'], s['frame_end']))
    return shots, sorted(set(dropped))


def _events(rows, actor_ids, kind_filter=None):
    """Normalize interaction events. Schema: {event_id?, kind, actor_id, frame | frame_start/frame_end, effector?,
    target?, contact?, head_bone?, forward_axis?, tolerance_frames?}. Rows that cannot be measured are reported, never
    silently dropped."""
    events = []
    ignored = []
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            ignored.append({'index': index, 'reason': 'NOT_AN_OBJECT'})
            continue
        kind = row.get('kind')
        if kind not in INTERACTION_MODE:
            ignored.append({'index': index, 'event_id': row.get('event_id'), 'reason': 'KIND_UNSUPPORTED'})
            continue
        if row.get('actor_id') not in actor_ids:
            ignored.append({'index': index, 'event_id': row.get('event_id'), 'reason': 'ACTOR_NOT_ANALYSED'})
            continue
        frame = row.get('frame', row.get('frame_start'))
        if frame is None:
            ignored.append({'index': index, 'event_id': row.get('event_id'), 'reason': 'FRAME_REQUIRED'})
            continue
        start = _integer(row.get('frame_start', frame), 'events[' + str(index) + '].frame_start')
        end = _integer(row.get('frame_end', frame), 'events[' + str(index) + '].frame_end')
        frame = _integer(frame, 'events[' + str(index) + '].frame')
        if end < start or not start <= frame <= end:
            ignored.append({'index': index, 'event_id': row.get('event_id'), 'reason': 'FRAME_WINDOW_INVALID'})
            continue
        target = row.get('target') or {}
        if isinstance(target, str):
            target = {'actor_id': target} if target in actor_ids else {'object': target}
        if not isinstance(target, dict):
            ignored.append({'index': index, 'event_id': row.get('event_id'), 'reason': 'TARGET_INVALID'})
            continue
        if row.get('target_actor_id'):
            target = dict(target, actor_id=row['target_actor_id'])
        if row.get('target_object'):
            target = dict(target, object=row['target_object'])
        surfaces = target.get('surfaces') or ([target['surface']] if target.get('surface') else None)
        if surfaces is not None:
            target = dict(target, surfaces=list(surfaces))
            target.pop('surface', None)
        effector = row.get('effector') or {}
        if isinstance(effector, str):
            effector = {'bone': effector}
        mode = INTERACTION_MODE[kind]
        event = {'event_id': str(row.get('event_id') or kind + '_' + str(index)), 'kind': kind, 'mode': mode,
                 'actor_id': row['actor_id'], 'frame': frame, 'frame_start': start, 'frame_end': end,
                 'target': {k: v for k, v in target.items() if k in {'actor_id', 'object', 'bone', 'end', 'point',
                                                                       'surfaces', 'generic'}},
                 'effector': {k: v for k, v in effector.items() if k in {'bone', 'end', 'object'}},
                 'contact': bool(row.get('contact', kind in CONTACT_KINDS)),
                 'head_bone': row.get('head_bone'), 'forward_axis': row.get('forward_axis'),
                 'reach_m': row.get('reach_m')}
        reason = _event_unmeasurable(event, actor_ids)
        if kind_filter is not None and mode not in kind_filter:
            continue
        if reason:
            ignored.append({'index': index, 'event_id': event['event_id'], 'reason': reason})
            continue
        events.append(event)
    for index, event in enumerate(events):
        event['index'] = index
    return events, ignored


def _event_unmeasurable(event, actor_ids):
    target = event['target']
    if target.get('actor_id') and target['actor_id'] not in actor_ids:
        return 'TARGET_ACTOR_NOT_ANALYSED'
    mode = event['mode']
    has_target = bool(target.get('actor_id') or target.get('object') or target.get('surfaces') or target.get('point'))
    if mode == 'GAZE':
        axis = event.get('forward_axis')
        if not event.get('head_bone'):
            return 'GAZE_HEAD_BONE_REQUIRED'
        if not isinstance(axis, list) or len(axis) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in axis) \
                or math.sqrt(sum(v * v for v in axis)) < 1e-9:
            return 'GAZE_FORWARD_AXIS_REQUIRED'
        return None if has_target else 'TARGET_REQUIRED'
    if mode == 'TIMING':
        return None
    if mode == 'PROXIMITY':
        return None if (target.get('actor_id') or target.get('object')) else 'TARGET_REQUIRED'
    if not event['effector'].get('bone') and not event['effector'].get('object'):
        return 'EFFECTOR_REQUIRED'
    if event['effector'].get('object'):
        return 'EFFECTOR_OBJECT_UNSUPPORTED'
    if target.get('point'):
        return 'POINT_TARGET_UNSUPPORTED_FOR_CONTACT'
    return None if has_target else 'TARGET_REQUIRED'


def _plan_events(plan, state, opts):
    """Events come from ONE source, first found: options.events (explicit, authoritative), state['interactions'],
    plan['interactions'] (a list, or a dict with 'events'/'interactions'). Mixing sources would double-count."""
    if opts.get('events') is not None:
        if not isinstance(opts['events'], list) or len(opts['events']) > 256:
            fail('CINEMATIC_EVENTS_INVALID', 'events must be a list of at most 256 rows')
        return list(opts['events']), ['OPTIONS']
    for owner, label in ((state or {}, 'STATE'), (plan or {}, 'PLAN')):
        value = owner.get('interactions')
        if isinstance(value, dict):
            value = value.get('events') or value.get('interactions')
        if isinstance(value, list) and value:
            return list(value), [label]
    return [], []


def _check_analysable(plan, state):
    if state.get('uncertain_operation'):
        fail('CINEMATIC_OPERATION_UNCERTAIN', 'a previous step may or may not have reached Blender; inspect checkpoint '
             + str(state.get('checkpoint')))
    if state.get('state') not in ANALYSABLE_STATES or not state.get('assembly'):
        fail('CINEMATIC_STATE_INVALID', str(state.get('state')) + ' has no candidate scene to analyse')
    if plan.get('backend_fingerprint') != store.backend_fingerprint():
        fail('CINEMATIC_BACKEND_CHANGED', 'cinematic code changed after this plan; create a new plan')


def plan_target(ctx, plan, state, opts):
    _check_analysable(plan, state)
    actors = _plan_actors(ctx, plan, opts, state)
    ids = [a['actor_id'] for a in actors]
    selected = opts.get('actors')
    if selected is not None:
        if not isinstance(selected, list) or any(a not in ids for a in selected):
            fail('ACTOR_NOT_IN_PLAN', 'actors must list actor_ids of this plan')
    if opts.get('shots') is not None:
        fail('CINEMATIC_PLAN_SHOTS_FIXED', 'plan mode analyses the frozen plan shots; options.shots is for scene mode')
    if opts.get('scene') is not None:
        fail('CINEMATIC_OPTION_UNSUPPORTED', 'options.scene is for scene mode; plan mode analyses the candidate scene')
    params = store.assembly_params(plan, state, {'expected_fingerprints': state.get('fingerprints')})
    shots, dropped = normalize_shots(plan.get('shots'), ids, state.get('cameras'))
    return {'mode': 'PLAN', 'params': params, 'actors': actors, 'selected': selected or ids, 'shots': shots,
            'dropped_subjects': dropped, 'timeline': state.get('timeline') or plan['timeline'], 'plan': plan,
            'state': state, 'cameras': state.get('cameras') or {}, 'asset_path': plan['asset'].get('path')}


def scene_target(req, opts):
    actors = _scene_actors(req, opts)
    ids = [a['actor_id'] for a in actors]
    scene = opts.get('scene')
    if scene is not None:
        scene = model.text(scene, 'scene', 63)
    shots, dropped = normalize_shots(opts.get('shots'), ids) if opts.get('shots') is not None else ([], [])
    return {'mode': 'SCENE', 'params': {'scene_name': scene}, 'actors': actors, 'selected': ids, 'shots': shots,
            'dropped_subjects': dropped, 'timeline': None, 'plan': None, 'state': None, 'cameras': {},
            'asset_path': None}


def _payload_actor(actor):
    return {'actor_id': actor['actor_id'], 'object': actor['object'], 'meshes': actor['meshes'],
            'head_bone': actor['head_bone'], 'root_bone': actor['root_bone']}


def build_params(kind, target, opts, thresholds, extra=None):
    params = dict(target['params'])
    params.update({'kind': kind, 'flags': dict(FLAGS[kind]), 'actors': [_payload_actor(a) for a in target['actors']],
                   'ray_points': 16, 'ray_frames': None, 'gaze': [], 'events': [], 'props': [],
                   'support_surfaces': [], 'max_vertices': 20000, 'max_triangles': 40000,
                   'pq_thresholds': {'max_penetration_depth_m': 0.0, 'max_penetrating_vertices': 0},
                   'proximity_margin_m': thresholds.get('proximity_margin_m', 0.05),
                   'max_vertex_frames': thresholds.get('max_vertex_frames', PROPOSALS['collisions']['max_vertex_frames']),
                   'query_distance_m': thresholds.get('query_distance_m', 0.3)})
    params.update(extra or {})
    return params


def run_payload(ctx, target, params):
    """One bridge call; the first payload of every analysis tool is the sampler (frame_set)."""
    result = ctx.bridge.run(BODIES[params['kind']], params)
    if target['mode'] == 'PLAN':
        observed = normalize(result.get('filepath')) if result.get('filepath') else None
        expected = target['asset_path'] or None
        if observed != expected:
            fail('CINEMATIC_STALE_ASSET', 'the open file is not the file this plan was made for; result discarded')
    return result


# ----------------------------------------------------------------------------------------------------------------
# Host: pure analysis helpers (testable without Blender).
# ----------------------------------------------------------------------------------------------------------------

def finding(code, severity, category, frames=None, shot=None, actor=None, measured=None, threshold=None,
            confidence=None, note=None):
    row = {'code': code, 'severity': severity, 'category': category}
    if frames is not None:
        row['frames'] = list(frames)
    if shot is not None:
        row['shot_id'] = shot
    if actor is not None:
        row['actor_id'] = actor
    if measured is not None:
        row['measured'] = measured
    if threshold is not None:
        row['threshold'] = threshold
        row['threshold_source'] = 'PROPOSAL'
    if confidence is not None:
        row['confidence'] = confidence
    if note is not None:
        row['note'] = note
    return row


def observation(code, shot=None, actor=None, measured=None, threshold=None, note=None):
    row = {'code': code, 'kind': 'CREATIVE_OBSERVATION'}
    if shot is not None:
        row['shot_id'] = shot
    if actor is not None:
        row['actor_id'] = actor
    if measured is not None:
        row['measured'] = measured
    if threshold is not None:
        row['threshold'] = threshold
        row['threshold_source'] = 'PROPOSAL'
    if note is not None:
        row['note'] = note
    return row


def runs(frames, flags):
    """Runs of consecutive SAMPLES where flag is true -> [[first_frame, last_frame, count], ...]."""
    out = []
    current = None
    for frame, flag in zip(frames, flags):
        if flag:
            if current is None:
                current = [frame, frame, 1]
            else:
                current[1] = frame
                current[2] += 1
        elif current is not None:
            out.append(current)
            current = None
    if current is not None:
        out.append(current)
    return out


def frame_runs(frames):
    """Sorted integer frames -> [[start, end], ...] of strictly consecutive frames."""
    out = []
    for frame in sorted(set(frames)):
        if out and frame == out[-1][1] + 1:
            out[-1][1] = frame
        else:
            out.append([frame, frame])
    return out


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _r(value, digits=6):
    return None if value is None else round(float(value), digits)


def stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {'min': _r(min(values)), 'median': _r(statistics.median(values)), 'max': _r(max(values))}


def clipped_area(bbox):
    if bbox is None:
        return 0.0
    x0, y0, x1, y1 = max(0.0, bbox[0]), max(0.0, bbox[1]), min(1.0, bbox[2]), min(1.0, bbox[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def clipped_height(bbox):
    if bbox is None:
        return 0.0
    return max(0.0, min(1.0, bbox[3]) - max(0.0, bbox[1]))


def overlap_ratio(a, b):
    """Clipped screen-bounds intersection over the smaller clipped area (0 when either is off screen)."""
    if a is None or b is None:
        return 0.0
    x0, y0 = max(a[0], b[0], 0.0), max(a[1], b[1], 0.0)
    x1, y1 = min(a[2], b[2], 1.0), min(a[3], b[3], 1.0)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    smaller = min(clipped_area(a), clipped_area(b))
    return inter / smaller if smaller > 1e-12 else 0.0


def union_coverage(boxes, grid=64):
    """Fraction of the frame covered by the union of clipped boxes (grid estimate, resolution 1/grid)."""
    boxes = [b for b in boxes if b is not None and clipped_area(b) > 0.0]
    if not boxes:
        return 0.0
    covered = 0
    for i in range(grid):
        x = (i + 0.5) / grid
        for j in range(grid):
            y = (j + 0.5) / grid
            if any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes):
                covered += 1
    return covered / (grid * grid)


def thirds_distance(point):
    """Distance in frame units from a screen point to the nearest rule-of-thirds intersection."""
    if point is None:
        return None
    return min(math.hypot(point[0] - x, point[1] - y) for x in (1 / 3, 2 / 3) for y in (1 / 3, 2 / 3))


def axis_side(p1, p2, camera):
    """Signed side (ground plane XY) of the camera relative to the action line p1->p2, and the distance (m)."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return 0, 0.0
    cross = dx * (camera[1] - p1[1]) - dy * (camera[0] - p1[0])
    distance = abs(cross) / length
    return (1 if cross > 0 else -1 if cross < 0 else 0), distance


def bearing_deg(center, camera):
    """Horizontal bearing of the camera seen from a subject, degrees in (-180, 180]."""
    return math.degrees(math.atan2(camera[1] - center[1], camera[0] - center[0]))


def angle_between_deg(a, b):
    d = (a - b + 180.0) % 360.0 - 180.0
    return abs(d)


def quaternion_angle(a, b):
    dot = abs(sum(x * y for x, y in zip(a, b)))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na < 1e-12 or nb < 1e-12:
        return None
    dot = min(1.0, dot / (na * nb))
    return math.degrees(2.0 * math.acos(dot))


def distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def measured_segments(frames_rows, markers, start, end):
    """Camera segments from the measured active camera. Exact cut frames come from camera markers (frame_set switches
    scene.camera at a marker, measured); rows confirm which camera was active."""
    cuts = sorted({m[1] for m in markers if m[2] is not None and start < m[1] <= end})
    bounds = [start] + cuts + [end + 1]
    segments = []
    by_frame = {r['f']: ((r.get('cam') or {}).get('name')) for r in frames_rows}
    for index in range(len(bounds) - 1):
        s, e = bounds[index], bounds[index + 1] - 1
        if e < s:
            continue
        cams = [by_frame[f] for f in sorted(by_frame) if s <= f <= e]
        camera = statistics.mode(cams) if cams else None
        segments.append({'shot_id': 'SEG_' + str(index + 1), 'frame_start': s, 'frame_end': e, 'shot_type': 'CUSTOM',
                         'camera_id': None, 'camera_object': camera, 'subjects': None, 'subjects_declared': False,
                         'axis': None, 'measured': True})
    return segments


def resolve_shots(target, result):
    shots = target['shots']
    coverage = result['coverage']
    if shots:
        return [dict(s, measured=False) for s in shots], 'PLAN' if target['mode'] == 'PLAN' else 'OPTIONS'
    segments = measured_segments(result['rows'], result['static']['markers'], coverage['frame_start'], coverage['frame_end'])
    ids = list(result['actors'])
    for segment in segments:
        segment['subjects'] = ids
    return segments, 'MEASURED_CAMERA_SEGMENTS'


def shot_rows(rows, shot):
    return [r for r in rows if shot['frame_start'] <= r['f'] <= shot['frame_end']]


def actor_series(rows, actor_id):
    return [((r.get('a') or {}).get(actor_id)) for r in rows]


def _sorted_findings(rows):
    return sorted(rows, key=lambda f: (SEVERITY_ORDER.get(f.get('severity'), 3), (f.get('frames') or [0])[0],
                                       f.get('code', ''), str(f.get('actor_id'))))


def screen_velocity(frames, points):
    """Per-sample screen velocity (frame units per frame) from consecutive samples with valid points."""
    out = [None] * len(frames)
    for i in range(1, len(frames)):
        a, b = points[i - 1], points[i]
        if a is None or b is None or frames[i] == frames[i - 1]:
            continue
        gap = frames[i] - frames[i - 1]
        out[i] = ((b[0] - a[0]) / gap, (b[1] - a[1]) / gap)
    return out


def direction_label(vx, minimum):
    if vx is None:
        return None
    if vx > minimum:
        return 'SCREEN_RIGHT'
    if vx < -minimum:
        return 'SCREEN_LEFT'
    return 'STATIC'


# ---- composition ----

def analyze_composition(target, result, thresholds):
    shots, shots_source = resolve_shots(target, result)
    findings = []
    observations = []
    per_shot = []
    actors = result['actors']
    rows = result['rows']
    for actor_id, info in actors.items():
        if actor_id not in target['selected']:
            continue
        if not info['in_scene']:
            findings.append(finding('ACTOR_NOT_IN_SCENE', 'ERROR', 'TECHNICAL', actor=actor_id,
                                    note='the actor object is not linked into the analysed scene'))
        elif info['hidden_in_render']:
            findings.append(finding('ACTOR_HIDDEN_IN_RENDER', 'ERROR', 'TECHNICAL', actor=actor_id))
    no_camera = [r['f'] for r in rows if r.get('cam') is None]
    for run in frame_runs(no_camera):
        findings.append(finding('NO_ACTIVE_CAMERA', 'ERROR', 'TECHNICAL', frames=run))
    not_camera = [r['f'] for r in rows if r.get('cam') and r['cam']['type'] != 'CAMERA']
    for run in frame_runs(not_camera):
        findings.append(finding('ACTIVE_CAMERA_NOT_CAMERA', 'ERROR', 'TECHNICAL', frames=run))
    for shot in shots:
        srows = shot_rows(rows, shot)
        if not srows:
            continue
        frames = [r['f'] for r in srows]
        subjects = [s for s in shot['subjects'] if s in target['selected']]
        boxes_by_frame = []
        for r in srows:
            boxes_by_frame.append([((r.get('a') or {}).get(s) or {}).get('bbox') for s in subjects])
        negative = [1.0 - union_coverage(b) for b in boxes_by_frame]
        for actor_id in subjects:
            series = actor_series(srows, actor_id)
            valid = [s for s in series if s is not None]
            if not valid:
                continue
            inside = [bool(s and s['inside'] > 0) for s in series]
            full = [bool(s and s['inside'] == s['n']) for s in series]
            centers = [s['center'] if s and s['center'] and s['center'][2] > 0 else None for s in series]
            heads = [s['head'] if s and s['head'] and s['head'][2] > 0 else None for s in series]
            headroom = [1.0 - s['bbox'][3] if s and s['bbox'] else None for s in series]
            height = [clipped_height(s['bbox']) if s else None for s in series]
            area = [clipped_area(s['bbox']) if s else None for s in series]
            velocity = screen_velocity(frames, centers)
            vx = median([v[0] for v in velocity if v is not None])
            direction = direction_label(vx, thresholds['direction_min_speed'])
            lead = []
            for s, v in zip(series, velocity):
                if s is None or s['bbox'] is None or v is None:
                    continue
                if v[0] > thresholds['direction_min_speed']:
                    lead.append(1.0 - s['bbox'][2])
                elif v[0] < -thresholds['direction_min_speed']:
                    lead.append(s['bbox'][0])
            focus = heads if any(heads) else centers
            thirds = [thirds_distance(p) for p in focus]
            row = {'shot_id': shot['shot_id'], 'actor_id': actor_id, 'shot_type': shot['shot_type'],
                   'frames': [shot['frame_start'], shot['frame_end']], 'samples': len(srows),
                   'in_frame_fraction': _r(sum(inside) / len(inside)),
                   'fully_in_frame_fraction': _r(sum(full) / len(full)),
                   'screen_center_x': stats([c[0] if c else None for c in centers]),
                   'screen_center_y': stats([c[1] if c else None for c in centers]),
                   'head_screen': stats([h[1] if h else None for h in heads]),
                   'headroom': stats(headroom), 'screen_height': stats(height), 'screen_bounds_area': stats(area),
                   'distance_m': stats([s['distance'] if s else None for s in series]),
                   'screen_direction': direction, 'screen_velocity_x': _r(vx),
                   'lead_room': stats(lead), 'thirds_distance': stats(thirds),
                   'negative_space': stats(negative)}
            per_shot.append(row)
            for run in runs(frames, [s is not None and s['front'] == 0 for s in series]):
                findings.append(finding('SUBJECT_BEHIND_CAMERA', 'WARNING', 'TECHNICAL', frames=run[:2], shot=shot['shot_id'],
                                        actor=actor_id, measured={'samples': run[2]}))
            for run in runs(frames, [s is not None and s['front'] > 0 and s['inside'] == 0 for s in series]):
                findings.append(finding('SUBJECT_OUT_OF_FRAME', 'WARNING', 'TECHNICAL', frames=run[:2], shot=shot['shot_id'],
                                        actor=actor_id, measured={'samples': run[2]}))
            for code, key in (('NEAR_CLIP_CUT', 'near'), ('FAR_CLIP_CUT', 'far')):
                for run in runs(frames, [bool(s and s[key]) for s in series]):
                    findings.append(finding(code, 'WARNING', 'TECHNICAL', frames=run[:2], shot=shot['shot_id'], actor=actor_id,
                                            measured={'samples': run[2]}, confidence='BOUNDS_CONSERVATIVE',
                                            note='evaluated bounds cross the camera clip range; the mesh itself may not'))
            if shot['shot_type'] not in NO_HEAD_RULE_SHOTS:
                flags = [bool(s and s['inside'] > 0 and (s['head'] is None or s['head'][2] <= 0 or not
                              (0.0 <= s['head'][0] <= 1.0 and 0.0 <= s['head'][1] <= 1.0))) for s in series]
                for run in runs(frames, flags):
                    findings.append(finding('HEAD_OUT_OF_FRAME', 'WARNING', 'TECHNICAL', frames=run[:2], shot=shot['shot_id'],
                                            actor=actor_id, measured={'samples': run[2],
                                                                      'head_source': actors[actor_id]['head_source']}))
            cx = row['screen_center_x']
            if cx and abs(cx['median'] - 0.5) <= thresholds['centered_tolerance']:
                observations.append(observation('SUBJECT_CENTERED', shot['shot_id'], actor_id,
                                                {'screen_center_x_median': cx['median']}, thresholds['centered_tolerance']))
            if row['thirds_distance'] and row['thirds_distance']['median'] <= thresholds['thirds_tolerance']:
                observations.append(observation('NEAR_THIRDS_INTERSECTION', shot['shot_id'], actor_id,
                                                {'thirds_distance_median': row['thirds_distance']['median']},
                                                thresholds['thirds_tolerance']))
            if row['headroom'] and 0.0 <= row['headroom']['median'] < thresholds['tight_headroom']:
                observations.append(observation('TIGHT_HEADROOM', shot['shot_id'], actor_id,
                                                {'headroom_median': row['headroom']['median']}, thresholds['tight_headroom']))
            if row['headroom'] and row['headroom']['median'] < 0.0:
                observations.append(observation('SUBJECT_TOP_CUT_BY_FRAME', shot['shot_id'], actor_id,
                                                {'headroom_median': row['headroom']['median']}))
            if row['lead_room'] and row['lead_room']['median'] < thresholds['min_lead_room']:
                observations.append(observation('LOW_LEAD_ROOM', shot['shot_id'], actor_id,
                                                {'lead_room_median': row['lead_room']['median'], 'direction': direction},
                                                thresholds['min_lead_room']))
            minimum = thresholds['min_screen_height'].get(shot['shot_type'])
            if minimum is not None and row['screen_height'] and row['screen_height']['median'] < minimum:
                observations.append(observation('SMALL_FOR_SHOT_TYPE', shot['shot_id'], actor_id,
                                                {'screen_height_median': row['screen_height']['median'],
                                                 'shot_type': shot['shot_type']}, minimum))
        for i in range(len(subjects)):
            for j in range(i + 1, len(subjects)):
                ratios = [overlap_ratio(b[i], b[j]) for b in boxes_by_frame]
                over = runs(frames, [r > thresholds['overlap_ratio'] for r in ratios])
                separation = []
                for r in srows:
                    a = ((r.get('a') or {}).get(subjects[i]) or {}).get('center')
                    b = ((r.get('a') or {}).get(subjects[j]) or {}).get('center')
                    if a and b and a[2] > 0 and b[2] > 0:
                        separation.append(abs(a[0] - b[0]))
                if over:
                    observations.append(observation('SILHOUETTE_OVERLAP', shot['shot_id'], [subjects[i], subjects[j]],
                                                    {'max_overlap_ratio': _r(max(ratios)), 'runs': over[:10],
                                                     'screen_separation_x': stats(separation)}, thresholds['overlap_ratio']))
        neg = stats(negative)
        if neg and neg['median'] >= thresholds['large_negative_space']:
            observations.append(observation('LARGE_NEGATIVE_SPACE', shot['shot_id'], None, {'negative_space': neg},
                                            thresholds['large_negative_space']))
    metrics = {'shots': len(shots), 'subject_rows': len(per_shot)}
    return {'shots_source': shots_source, 'per_shot': per_shot, 'findings': findings, 'observations': observations,
            'metrics': metrics}


# ---- continuity ----

def _allowed_break(allow, cut_frame, previous, current):
    if allow is True:
        return True
    if not allow:
        return False
    keys = {cut_frame, str(cut_frame), current['shot_id'], previous['shot_id'] + '>' + current['shot_id']}
    return any(item in keys for item in allow)


def _axis_points(axis, row_a, actors_world, events_target=None):
    """World points (m) of the action line from an axis spec {actor_a, actor_b} | {actor, target_point}."""
    if axis.get('actor_a') and axis.get('actor_b'):
        a = (actors_world.get(axis['actor_a']) or {}).get('root')
        b = (actors_world.get(axis['actor_b']) or {}).get('root')
        return a, b
    if axis.get('actor') and axis.get('target_point'):
        return (actors_world.get(axis['actor']) or {}).get('root'), axis['target_point']
    return None, None


def _world(row):
    return {aid: (item or {}).get('w') or {} for aid, item in (row.get('a') or {}).items()}


def _sample_at_or_before(rows, frame):
    best = None
    for r in rows:
        if r['f'] <= frame and (best is None or r['f'] > best['f']):
            best = r
    return best


def _sample_at_or_after(rows, frame):
    best = None
    for r in rows:
        if r['f'] >= frame and (best is None or r['f'] < best['f']):
            best = r
    return best


def _screen_dx(rows, actor_id, first, last):
    a = _sample_at_or_after(rows, first)
    b = _sample_at_or_before(rows, last)
    if a is None or b is None or b['f'] <= a['f']:
        return None
    pa = ((a.get('a') or {}).get(actor_id) or {}).get('center')
    pb = ((b.get('a') or {}).get(actor_id) or {}).get('center')
    if not pa or not pb or pa[2] <= 0 or pb[2] <= 0:
        return None
    return (pb[0] - pa[0]) / (b['f'] - a['f'])


def analyze_continuity(target, result, thresholds, opts):
    shots, shots_source = resolve_shots(target, result)
    rows = sorted(result['rows'], key=lambda r: r['f'])
    findings = []
    observations = []
    cuts = []
    coverage = result['coverage']
    markers = result['static']['markers']
    allow = opts.get('allow_axis_break', False)
    if not (allow is True or allow is False or isinstance(allow, list)):
        fail('CINEMATIC_OPTION_INVALID', 'allow_axis_break must be true/false or a list of cut frames / shot ids')
    # Temporal continuity of the shot list.
    for previous, current in zip(shots, shots[1:]):
        if current['frame_start'] > previous['frame_end'] + 1:
            findings.append(finding('TIMELINE_GAP', 'ERROR', 'TECHNICAL',
                                    frames=[previous['frame_end'] + 1, current['frame_start'] - 1],
                                    shot=current['shot_id'], note='frames between shots belong to no shot'))
        elif current['frame_start'] <= previous['frame_end']:
            findings.append(finding('SHOT_OVERLAP', 'ERROR', 'TECHNICAL',
                                    frames=[current['frame_start'], previous['frame_end']], shot=current['shot_id']))
    if shots and target['timeline']:
        if shots[0]['frame_start'] > target['timeline']['frame_start']:
            findings.append(finding('TIMELINE_START_UNCOVERED', 'WARNING', 'TECHNICAL',
                                    frames=[target['timeline']['frame_start'], shots[0]['frame_start'] - 1]))
        if shots[-1]['frame_end'] < target['timeline']['frame_end']:
            findings.append(finding('TIMELINE_END_UNCOVERED', 'WARNING', 'TECHNICAL',
                                    frames=[shots[-1]['frame_end'] + 1, target['timeline']['frame_end']]))
    for shot in shots:
        length = shot['frame_end'] - shot['frame_start'] + 1
        if length < thresholds['min_shot_frames']:
            observations.append(observation('VERY_SHORT_SHOT', shot['shot_id'], None, {'frames': length},
                                            thresholds['min_shot_frames']))
    no_camera = [r['f'] for r in rows if r.get('cam') is None]
    for run in frame_runs(no_camera):
        findings.append(finding('NO_ACTIVE_CAMERA', 'ERROR', 'TECHNICAL', frames=run))
    marker_cuts = sorted({m[1] for m in markers if m[2] is not None and coverage['frame_start'] < m[1] <= coverage['frame_end']})
    if shots_source != 'MEASURED_CAMERA_SEGMENTS':
        planned = {s['frame_start'] for s in shots[1:]}
        for frame in sorted(planned - set(marker_cuts)):
            same = [s for s in shots if s['frame_start'] == frame]
            previous = [s for s in shots if s['frame_end'] == frame - 1]
            if same and previous and same[0].get('camera_object') and same[0]['camera_object'] == previous[0].get('camera_object'):
                continue
            findings.append(finding('PLANNED_CUT_WITHOUT_MARKER', 'WARNING', 'TECHNICAL', frames=[frame, frame]))
        for frame in sorted(set(marker_cuts) - planned):
            findings.append(finding('CUT_NOT_IN_PLAN', 'WARNING', 'TECHNICAL', frames=[frame, frame]))
        for shot in shots:
            if not shot.get('camera_object'):
                continue
            measured = [((r.get('cam') or {}).get('name')) for r in shot_rows(rows, shot)]
            wrong = [r['f'] for r in shot_rows(rows, shot) if ((r.get('cam') or {}).get('name')) != shot['camera_object']]
            if wrong:
                findings.append(finding('SHOT_CAMERA_MISMATCH', 'ERROR', 'TECHNICAL', frames=[wrong[0], wrong[-1]],
                                        shot=shot['shot_id'], measured={'expected': shot['camera_object'],
                                                                        'observed': sorted(set(str(m) for m in measured))}))
    gaze_specs = target.get('gaze') or []
    for previous, current in zip(shots, shots[1:]):
        cut = current['frame_start']
        before = _sample_at_or_before(rows, cut - 1)
        after = _sample_at_or_after(rows, cut)
        record = {'from': previous['shot_id'], 'to': current['shot_id'], 'frame': cut,
                  'cameras': [((before or {}).get('cam') or {}).get('name'), ((after or {}).get('cam') or {}).get('name')]}
        allowed = _allowed_break(allow, cut, previous, current)
        common = [a for a in previous['subjects'] if a in current['subjects']]
        axis = current.get('axis') or opts.get('axis') or ({'actor_a': common[0], 'actor_b': common[1]} if len(common) >= 2 else None)
        record['axis'] = axis
        if before is not None and after is not None and before.get('cam') and after.get('cam') and axis:
            p1, p2 = _axis_points(axis, before, _world(before))
            q1, q2 = _axis_points(axis, after, _world(after))
            if p1 and p2 and q1 and q2:
                side_a, dist_a = axis_side(p1, p2, before['cam']['position'])
                side_b, dist_b = axis_side(q1, q2, after['cam']['position'])
                record['camera_side'] = [side_a, side_b]
                record['camera_axis_distance_m'] = [_r(dist_a), _r(dist_b)]
                if dist_a < thresholds['axis_degenerate_m'] or dist_b < thresholds['axis_degenerate_m']:
                    record['axis_status'] = 'ON_AXIS'
                elif side_a != side_b:
                    record['axis_status'] = 'AXIS_BREAK_ALLOWED' if allowed else 'AXIS_CROSSED'
                    findings.append(finding('AXIS_BREAK_ALLOWED' if allowed else 'AXIS_CROSSED',
                                            'INFO' if allowed else 'WARNING', 'CONTINUITY_RULE', frames=[cut - 1, cut],
                                            shot=current['shot_id'],
                                            measured={'camera_side': [side_a, side_b], 'axis': axis},
                                            note='180-degree rule: the camera changed side of the action line'
                                            + (' (allowed by options.allow_axis_break)' if allowed else '')))
                else:
                    record['axis_status'] = 'SAME_SIDE'
            else:
                record['axis_status'] = 'AXIS_POINTS_UNAVAILABLE'
        else:
            record['axis_status'] = 'UNDEFINED'
        jumps = []
        for actor_id in common:
            sa = ((before or {}).get('a') or {}).get(actor_id) if before else None
            sb = ((after or {}).get('a') or {}).get(actor_id) if after else None
            dx_a = _screen_dx(rows, actor_id, previous['frame_end'] - 2, previous['frame_end'])
            dx_b = _screen_dx(rows, actor_id, current['frame_start'], current['frame_start'] + 1)
            label_a = direction_label(dx_a, thresholds['direction_min_speed'])
            label_b = direction_label(dx_b, thresholds['direction_min_speed'])
            if label_a in ('SCREEN_LEFT', 'SCREEN_RIGHT') and label_b in ('SCREEN_LEFT', 'SCREEN_RIGHT') and label_a != label_b:
                findings.append(finding('SCREEN_DIRECTION_FLIP_ALLOWED' if allowed else 'SCREEN_DIRECTION_FLIP',
                                        'INFO' if allowed else 'WARNING', 'CONTINUITY_RULE', frames=[cut - 1, cut],
                                        shot=current['shot_id'], actor=actor_id,
                                        measured={'before': label_a, 'after': label_b, 'velocity_x': [_r(dx_a), _r(dx_b)]}))
            if sa and sb and sa.get('center') and sb.get('center') and sa['center'][2] > 0 and sb['center'][2] > 0:
                jump = math.hypot(sb['center'][0] - sa['center'][0], sb['center'][1] - sa['center'][1])
                jumps.append({'actor_id': actor_id, 'screen_jump': _r(jump)})
                if jump > thresholds['screen_jump']:
                    observations.append(observation('SCREEN_POSITION_JUMP', current['shot_id'], actor_id,
                                                    {'screen_jump': _r(jump), 'cut_frame': cut}, thresholds['screen_jump']))
                wa = _world(before).get(actor_id, {}).get('center')
                wb = _world(after).get(actor_id, {}).get('center')
                if wa and wb and before.get('cam') and after.get('cam'):
                    change = angle_between_deg(bearing_deg(wa, before['cam']['position']), bearing_deg(wb, after['cam']['position']))
                    ha, hb = clipped_height(sa.get('bbox')), clipped_height(sb.get('bbox'))
                    size = abs(ha - hb) / max(ha, hb) if max(ha, hb) > 1e-9 else 0.0
                    if change < thresholds['jump_cut_min_angle_deg'] and size < thresholds['jump_cut_size_ratio']:
                        observations.append(observation('JUMP_CUT_RISK', current['shot_id'], actor_id,
                                                        {'camera_angle_change_deg': _r(change), 'size_change_ratio': _r(size)},
                                                        {'jump_cut_min_angle_deg': thresholds['jump_cut_min_angle_deg'],
                                                         'jump_cut_size_ratio': thresholds['jump_cut_size_ratio']}))
        record['screen_jumps'] = jumps
        for actor_id in current['subjects']:
            item = ((after or {}).get('a') or {}).get(actor_id) if after else None
            reason = None
            if item is None:
                reason = 'NOT_SAMPLED_OR_ABSENT'
            elif item['front'] == 0:
                reason = 'BEHIND_CAMERA'
            elif item['inside'] == 0:
                reason = 'OUT_OF_FRAME'
            elif item.get('vis') and item['vis']['rays'] and item['vis']['clear'] == 0:
                reason = 'OCCLUDED'
            if reason:
                findings.append(finding('SUBJECT_NOT_VISIBLE_AT_SHOT_START', 'WARNING', 'TECHNICAL', frames=[cut, cut],
                                        shot=current['shot_id'], actor=actor_id, measured={'reason': reason}))
        if gaze_specs:
            gaze_rows = []
            for spec in gaze_specs:
                ga = next((g for g in (before or {}).get('gz') or [] if g and g['g'] == spec['index']), None)
                gb = next((g for g in (after or {}).get('gz') or [] if g and g['g'] == spec['index']), None)
                sx_a = ga['screen'][0] if ga and ga.get('screen') else None
                sx_b = gb['screen'][0] if gb and gb.get('screen') else None
                gaze_rows.append({'actor_id': spec['actor_id'], 'angle_deg': [ga and ga['angle'], gb and gb['angle']],
                                  'screen_dx': [sx_a, sx_b]})
                if sx_a is not None and sx_b is not None and abs(sx_a) > 1e-4 and abs(sx_b) > 1e-4 and (sx_a > 0) != (sx_b > 0):
                    findings.append(finding('EYELINE_DIRECTION_FLIP_ALLOWED' if allowed else 'EYELINE_DIRECTION_FLIP',
                                            'INFO' if allowed else 'WARNING', 'CONTINUITY_RULE', frames=[cut - 1, cut],
                                            shot=current['shot_id'], actor=spec['actor_id'],
                                            measured={'screen_dx': [sx_a, sx_b]}))
            record['gaze'] = gaze_rows
        cuts.append(record)
    if shots and shots_source != 'MEASURED_CAMERA_SEGMENTS':
        first = shots[0]
        after = _sample_at_or_after(rows, first['frame_start'])
        for actor_id in first['subjects']:
            item = ((after or {}).get('a') or {}).get(actor_id) if after else None
            if item is not None and (item['front'] == 0 or item['inside'] == 0):
                findings.append(finding('SUBJECT_NOT_VISIBLE_AT_SHOT_START', 'WARNING', 'TECHNICAL',
                                        frames=[first['frame_start'], first['frame_start']], shot=first['shot_id'],
                                        actor=actor_id, measured={'reason': 'OUT_OF_FRAME' if item['front'] else 'BEHIND_CAMERA'}))
    return {'shots_source': shots_source, 'cuts': cuts, 'findings': findings, 'observations': observations,
            'metrics': {'shots': len(shots), 'cuts': len(cuts), 'marker_cuts': marker_cuts}}


# ---- visibility ----

def _occluder_class(name, result, target):
    info = result['objects'].get(name) or {}
    if info.get('actor_id'):
        return 'ACTOR'
    role = str(info.get('role') or '').lower()
    if 'prop' in role or name in (target.get('props') or []):
        return 'PROP'
    if info.get('owned'):
        return 'OWNED_' + (str(info.get('role')) if info.get('role') else 'DATABLOCK').upper()
    return 'ENVIRONMENT' if name != '__RAY_LIMIT__' else 'UNKNOWN_RAY_LIMIT'


def _intended(intended, actor_id, frame, occluder=None):
    for row in intended:
        if row['actor_id'] == actor_id and row['frame_start'] <= frame <= row['frame_end'] \
                and (row.get('occluder') is None or row.get('occluder') == occluder):
            return True
    return False


def analyze_visibility(target, result, thresholds, facial_events, intended):
    shots, shots_source = resolve_shots(target, result)
    rows = sorted(result['rows'], key=lambda r: r['f'])
    findings = []
    per_shot = []
    actors = result['actors']
    for actor_id, info in actors.items():
        if actor_id not in target['selected']:
            continue
        if not info['in_scene']:
            findings.append(finding('ACTOR_ABSENT', 'ERROR', 'TECHNICAL', actor=actor_id,
                                    note='the actor object is not linked into the analysed scene'))
        elif info['hidden_in_render']:
            findings.append(finding('ACTOR_HIDDEN_IN_RENDER', 'ERROR', 'TECHNICAL', actor=actor_id))
    for shot in shots:
        srows = shot_rows(rows, shot)
        frames = [r['f'] for r in srows]
        for actor_id in [s for s in shot['subjects'] if s in target['selected']]:
            series = actor_series(srows, actor_id)
            if not any(series):
                continue
            fractions = []
            occluders = {}
            for s in series:
                vis = (s or {}).get('vis')
                if vis and vis['rays']:
                    fractions.append(vis['clear'] / vis['rays'])
                    for name, count in vis['occ'].items():
                        occluders[name] = occluders.get(name, 0) + count
                else:
                    fractions.append(None)
            total_rays = sum(((s or {}).get('vis') or {}).get('rays', 0) for s in series)
            row = {'shot_id': shot['shot_id'], 'actor_id': actor_id, 'shot_type': shot['shot_type'],
                   'frames': [shot['frame_start'], shot['frame_end']], 'samples': len(srows),
                   'visible_fraction': stats(fractions), 'rays': total_rays,
                   'occluders': [{'object': n, 'class': _occluder_class(n, result, target), 'rays': c,
                                  'share': _r(c / total_rays) if total_rays else None}
                                 for n, c in sorted(occluders.items(), key=lambda kv: -kv[1])[:8]],
                   'screen_height': stats([clipped_height(s['bbox']) if s else None for s in series])}
            per_shot.append(row)
            for run in runs(frames, [s is not None and s['front'] > 0 and s['inside'] == 0 for s in series]):
                findings.append(finding('OUT_OF_FRAME', 'WARNING', 'TECHNICAL', frames=run[:2], shot=shot['shot_id'],
                                        actor=actor_id, measured={'samples': run[2]}))
            for run in runs(frames, [s is not None and s['front'] == 0 for s in series]):
                findings.append(finding('BEHIND_CAMERA', 'WARNING', 'TECHNICAL', frames=run[:2], shot=shot['shot_id'],
                                        actor=actor_id, measured={'samples': run[2]}))
            for run in runs(frames, [bool(s and (s['near'] or s['far'])) for s in series]):
                findings.append(finding('CAMERA_CLIPPING', 'WARNING', 'TECHNICAL', frames=run[:2], shot=shot['shot_id'],
                                        actor=actor_id, measured={'samples': run[2]}, confidence='BOUNDS_CONSERVATIVE'))
            low = [f is not None and f < thresholds['min_visible_fraction'] for f in fractions]
            for run in runs(frames, low):
                if run[2] < thresholds['min_occlusion_frames']:
                    continue
                names = {}
                for r, s in zip(srows, series):
                    if run[0] <= r['f'] <= run[1] and s and s.get('vis'):
                        for name, count in s['vis']['occ'].items():
                            names[name] = names.get(name, 0) + count
                main = max(names.items(), key=lambda kv: kv[1])[0] if names else None
                values = [f for r, f in zip(srows, fractions) if run[0] <= r['f'] <= run[1] and f is not None]
                planned = all(_intended(intended, actor_id, f, main) for f in range(run[0], run[1] + 1))
                findings.append(finding('INTENDED_OCCLUSION' if planned else 'EXCESSIVE_OCCLUSION',
                                        'INFO' if planned else 'WARNING', 'TECHNICAL', frames=run[:2], shot=shot['shot_id'],
                                        actor=actor_id, measured={'samples': run[2], 'min_visible_fraction': _r(min(values)),
                                                                  'occluder': main,
                                                                  'occluder_class': _occluder_class(main, result, target) if main else None},
                                        threshold=thresholds['min_visible_fraction']))
            for name, count in occluders.items():
                if _occluder_class(name, result, target) == 'PROP' and total_rays and count / total_rays >= thresholds['prop_occlusion_fraction']:
                    findings.append(finding('PROP_OCCLUDING_SUBJECT', 'WARNING', 'TECHNICAL',
                                            frames=[shot['frame_start'], shot['frame_end']], shot=shot['shot_id'],
                                            actor=actor_id, measured={'prop': name, 'share': _r(count / total_rays)},
                                            threshold=thresholds['prop_occlusion_fraction']))
            minimum = thresholds['min_screen_height'].get(shot['shot_type'])
            if minimum is not None and row['screen_height'] and row['screen_height']['median'] < minimum:
                findings.append(finding('ACTOR_TOO_SMALL_FOR_SHOT_TYPE', 'WARNING', 'TECHNICAL',
                                        frames=[shot['frame_start'], shot['frame_end']], shot=shot['shot_id'], actor=actor_id,
                                        measured={'screen_height_median': row['screen_height']['median'],
                                                  'shot_type': shot['shot_type']}, threshold=minimum))
    events = []
    by_frame = {r['f']: r for r in rows}
    for event in facial_events:
        r = by_frame.get(event['frame'])
        item = ((r or {}).get('a') or {}).get(event['actor_id']) if r else None
        vis = (item or {}).get('vis')
        status = vis['head'] if vis else 'NOT_SAMPLED'
        events.append({'actor_id': event['actor_id'], 'frame': event['frame'], 'head': status,
                       'occluder': vis.get('head_by') if vis else None, 'label': event.get('label')})
        if status not in ('VISIBLE', 'NOT_SAMPLED'):
            findings.append(finding('FACE_HIDDEN_AT_EVENT', 'WARNING', 'TECHNICAL', frames=[event['frame'], event['frame']],
                                    actor=event['actor_id'], measured={'head': status, 'occluder': vis.get('head_by'),
                                                                       'head_source': actors[event['actor_id']]['head_source']},
                                    note=event.get('label')))
    return {'shots_source': shots_source, 'per_shot': per_shot, 'findings': findings, 'observations': [],
            'facial_events': events,
            'metrics': {'shots': len(shots), 'hidden_skipped_rays': sum(result['hidden_skipped'].values())}}


# ---- collisions ----

def _contact_windows(events, margin):
    windows = []
    for event in events:
        if not event['contact']:
            continue
        other = event['target'].get('actor_id') or ('prop:' + event['target']['object'] if event['target'].get('object') else None)
        if other is None:
            continue
        windows.append({'pair': frozenset({event['actor_id'], other}), 'frames': [event['frame_start'] - margin,
                                                                                event['frame_end'] + margin],
                        'event_id': event['event_id']})
    return windows


def pair_series(rows):
    """Payload pair rows -> {(a, b): {frame: measurement}}. Pairs farther apart than the margin are absent."""
    series = {}
    for r in rows:
        for a, b, count, depth, hit, unknown, errors, clearance, gap in r.get('pairs') or []:
            series.setdefault((a, b), {})[r['f']] = {'count': count, 'depth': depth, 'hit': hit, 'unknown': unknown,
                                                     'errors': errors, 'clearance': clearance, 'gap': gap}
    return series


def classify_collisions(rows, events, thresholds, step):
    """Per-pair penetration rows -> interval findings. Intent comes only from planned contact events."""
    windows = _contact_windows(events, thresholds['contact_window_margin_frames'])
    series = pair_series(rows)
    frames = [r['f'] for r in rows]
    findings = []
    observations = []
    pairs = []
    for (a, b), by_frame in sorted(series.items()):
        def intended_at(frame):
            for w in windows:
                if w['pair'] == frozenset({a, b}) and w['frames'][0] <= frame <= w['frames'][1]:
                    return w['event_id']
            return None
        state = []
        for frame in frames:
            value = by_frame.get(frame)
            if value is None:
                state.append(None)
            elif value['unknown']:
                state.append('UNKNOWN')
            elif value['count'] > 0:
                state.append('INTENDED' if intended_at(frame) else 'ACCIDENTAL')
            else:
                state.append('CLEAR')
        summary = {'pair': [a, b], 'frames_measured': sum(1 for s in state if s is not None),
                   'penetrating_frames': sum(1 for s in state if s in ('INTENDED', 'ACCIDENTAL')),
                   'max_depth_m': _r(max((v['depth'] for v in by_frame.values()), default=0.0)),
                   'min_clearance_m': _r(min((v['clearance'] for v in by_frame.values() if v['clearance'] is not None), default=None)),
                   'unknown_colliders': sorted({n for v in by_frame.values() for n in v['unknown']})}
        pairs.append(summary)
        for label in ('ACCIDENTAL', 'INTENDED', 'UNKNOWN'):
            for run in runs(frames, [s == label for s in state]):
                values = [by_frame[f] for f in frames if run[0] <= f <= run[1] and f in by_frame]
                depth = max(v['depth'] for v in values)
                meshes = sorted({m for v in values for m in v['hit']})
                measured = {'samples': run[2], 'max_depth_m': _r(depth), 'max_penetrating_vertices': max(v['count'] for v in values),
                            'meshes': meshes}
                confidence = 'MEASURED_VERTEX_PARITY_DENSE' if step == 1 else 'SPARSE_SAMPLES'
                if label == 'UNKNOWN':
                    findings.append(finding('COLLISION_UNKNOWN_OPEN_MESH', 'WARNING', 'TECHNICAL', frames=run[:2],
                                            actor=[a, b], measured={'samples': run[2], 'colliders': sorted({n for v in values for n in v['unknown']})},
                                            confidence='UNKNOWN', note='open or non-manifold mesh: inside/outside cannot be measured; never read as zero'))
                elif label == 'INTENDED':
                    event_id = intended_at(run[0])
                    measured['event_id'] = event_id
                    over = depth > thresholds['contact_penetration_m']
                    findings.append(finding('INTENDED_CONTACT_PENETRATION' if over else 'INTENDED_CONTACT',
                                            'WARNING' if over else 'INFO', 'TECHNICAL', frames=run[:2], actor=[a, b],
                                            measured=measured, threshold=thresholds['contact_penetration_m'],
                                            confidence=confidence))
                else:
                    severity = 'ERROR' if depth > thresholds['error_depth_m'] else 'WARNING' if depth > thresholds['tolerance_depth_m'] else 'INFO'
                    findings.append(finding('ACCIDENTAL_COLLISION' if severity != 'INFO' else 'ACCIDENTAL_CONTACT_WITHIN_TOLERANCE',
                                            severity, 'TECHNICAL', frames=run[:2], actor=[a, b], measured=measured,
                                            threshold={'tolerance_depth_m': thresholds['tolerance_depth_m'],
                                                       'error_depth_m': thresholds['error_depth_m']},
                                            confidence=confidence))
        clear = [v['clearance'] for v in by_frame.values() if v['clearance'] is not None]
        if clear and min(clear) < thresholds['proximity_margin_m']:
            near = [f for f in frames if f in by_frame and by_frame[f]['clearance'] is not None
                    and by_frame[f]['clearance'] < thresholds['proximity_margin_m']]
            observations.append(observation('NEAR_MISS', None, [a, b], {'min_clearance_m': _r(min(clear)),
                                                                     'frames': frame_runs(near)[:10]},
                                            thresholds['proximity_margin_m']))
        errors = sorted({e for v in by_frame.values() for e in v['errors']})
        if errors:
            findings.append(finding('COLLISION_MEASUREMENT_INCOMPLETE', 'WARNING', 'TECHNICAL', actor=[a, b],
                                    measured={'errors': errors}))
    return pairs, findings, observations


def analyze_collisions(target, result, thresholds, events):
    rows = sorted(result['rows'], key=lambda r: r['f'])
    step = result['coverage'].get('step')
    pairs, findings, observations = classify_collisions(rows, events, thresholds, step)
    frames = [r['f'] for r in rows]
    gaps = [b - a for a, b in zip(frames, frames[1:])]
    coverage = {'representation': 'evaluated mesh vertices of each entity against the other entity\'s closed evaluated '
                                  'mesh (three-ray parity majority, depth by find_nearest; physics.qa.pq_collision_frame)',
                'entities': result.get('entities'), 'frames_sampled': len(frames), 'max_sample_gap_frames': max(gaps) if gaps else 0,
                'continuous_collision_freedom_certified': False,
                'pairs_skipped_by_bounds': 'pairs whose world bounds are farther apart than proximity_margin_m are not ray-tested'}
    metrics = {'pairs_measured': len(pairs),
               'accidental_sampled_frames': sum(f['measured']['samples'] for f in findings
                                                if f['code'] == 'ACCIDENTAL_COLLISION'),
               'accidental_intervals': sum(1 for f in findings if f['code'] == 'ACCIDENTAL_COLLISION'),
               'unknown': any(f['code'] == 'COLLISION_UNKNOWN_OPEN_MESH' for f in findings)}
    # A measured pair row implies two entities even when a synthetic result omits the entity list.
    entities = max(len(result.get('entities') or []), 2 if pairs else 0)
    metrics.update(penetration_metrics(findings, entities))
    return {'pairs': pairs, 'findings': findings, 'observations': observations, 'collision_coverage': coverage,
            'metrics': metrics}


ACCIDENTAL_CODES = frozenset({'ACCIDENTAL_COLLISION', 'ACCIDENTAL_CONTACT_WITHIN_TOLERANCE'})
INTENDED_CODES = frozenset({'INTENDED_CONTACT', 'INTENDED_CONTACT_PENETRATION'})


def penetration_metrics(findings, entities):
    """Top-level penetration depth for the quality report and the `max_penetration_m` refinement target: the max over
    every penetrating interval of every pair (accidental and intended), in metres. None when it is unknown (an open or
    non-manifold collider measures no depth, so a partial max would understate it) or when fewer than two entities
    exist (nothing can collide). Pairs whose bounds stay apart by more than proximity_margin_m cannot penetrate: 0.0."""
    accidental = [f['measured']['max_depth_m'] for f in findings if f['code'] in ACCIDENTAL_CODES]
    intended = [f['measured']['max_depth_m'] for f in findings if f['code'] in INTENDED_CODES]
    unknown = any(f['code'] == 'COLLISION_UNKNOWN_OPEN_MESH' for f in findings)
    if entities < 2:
        status, value = 'NO_PAIRS', None
    elif unknown:
        status, value = 'UNKNOWN_OPEN_MESH', None
    else:
        status, value = 'MEASURED', _r(max(accidental + intended, default=0.0))
    return {'max_penetration_m': value, 'max_penetration_units': 'm', 'max_penetration_status': status,
            'max_accidental_penetration_m': None if value is None else _r(max(accidental, default=0.0)),
            'max_intended_penetration_m': None if value is None else _r(max(intended, default=0.0))}


# ---- contacts ----

def analyze_contacts(target, result, thresholds, events):
    rows = sorted(result['rows'], key=lambda r: r['f'])
    fps = result['fps']
    findings = []
    reports = []
    by_event = {e['index']: [] for e in events}
    for r in rows:
        for item in r.get('ev') or []:
            by_event.setdefault(item['e'], []).append(item)
    for event in events:
        samples = sorted(by_event.get(event['index'], []), key=lambda s: s['f'])
        report = {'event_id': event['event_id'], 'kind': event['kind'], 'mode': event['mode'], 'actor_id': event['actor_id'],
                  'target': event['target'], 'planned': {'frame': event['frame'], 'window': [event['frame_start'], event['frame_end']]},
                  'samples': len(samples)}
        frames = [s['f'] for s in samples]
        if not samples:
            report['status'] = 'NOT_SAMPLED'
            reports.append(report)
            continue
        if event['mode'] == 'GAZE':
            angles = [s.get('angle') for s in samples]
            aligned = [a is not None and a <= thresholds['gaze_tolerance_deg'] for a in angles]
            inside = [a for s, a in zip(samples, aligned) if event['frame_start'] <= s['f'] <= event['frame_end']]
            first = next((s['f'] for s, ok in zip(samples, aligned) if ok), None)
            report.update(angle_deg=stats(angles), aligned_fraction_in_window=_r(sum(inside) / len(inside)) if inside else None,
                          first_aligned_frame=first, timing_offset_frames=None if first is None else first - event['frame_start'])
            if not any(inside):
                findings.append(finding('GAZE_NOT_ALIGNED', 'WARNING', 'TECHNICAL', frames=[event['frame_start'], event['frame_end']],
                                        actor=event['actor_id'], measured={'event_id': event['event_id'], 'angle_deg': report['angle_deg']},
                                        threshold=thresholds['gaze_tolerance_deg']))
            report['status'] = 'MEASURED'
            reports.append(report)
            continue
        roots = [s['root'] for s in samples]
        troots = [s.get('troot') for s in samples]
        root_distance = [distance(a, b) if b else None for a, b in zip(roots, troots)]
        report['root_distance_m'] = stats(root_distance)
        if event['mode'] in ('PROXIMITY', 'TIMING'):
            if event['mode'] == 'PROXIMITY' and root_distance[0] is not None and root_distance[-1] is not None:
                change = root_distance[-1] - root_distance[0]
                report['root_distance_change_m'] = _r(change)
                expected = -1 if event['kind'] == 'APPROACH' else 1
                if change * expected < -thresholds['trend_tolerance_m']:
                    findings.append(finding('PROXIMITY_TREND_MISMATCH', 'WARNING', 'TECHNICAL',
                                            frames=[frames[0], frames[-1]], actor=event['actor_id'],
                                            measured={'event_id': event['event_id'], 'root_distance_change_m': _r(change),
                                                      'expected': 'DECREASING' if expected < 0 else 'INCREASING'},
                                            threshold=thresholds['trend_tolerance_m']))
            report['status'] = 'MEASURED' if event['mode'] == 'PROXIMITY' else 'TIMING_ONLY'
            reports.append(report)
            continue
        dist = [s.get('d') for s in samples]
        contact = [s.get('inside') is True or (d is not None and d <= thresholds['contact_distance_m']) for s, d in zip(samples, dist)]
        contact_frames = [f for f, c in zip(frames, contact) if c]
        planned = next((s for s in samples if s['f'] == event['frame']), None)
        depths = [s.get('depth') for s in samples if s.get('depth') is not None]
        report.update(distance_m=stats(dist), contact_frames=frame_runs(contact_frames),
                      distance_at_planned_frame_m=planned.get('d') if planned else None,
                      max_penetration_m=_r(max(depths)) if depths else (None if any(s.get('inside') is None for s in samples) else 0.0),
                      penetration_measured=all(s.get('inside') is not None for s in samples))
        if event['mode'] == 'ATTACK':
            reach = event.get('reach_m') if isinstance(event.get('reach_m'), (int, float)) else thresholds['contact_distance_m']
            report['reach_m'] = reach
            report['reach_status'] = 'IN_REACH' if planned and planned.get('d') is not None and planned['d'] <= reach else 'OUT_OF_REACH'
        if not contact_frames:
            report['status'] = 'NO_CONTACT'
            if event['contact']:
                closest = min(((d, f) for d, f in zip(dist, frames) if d is not None), default=(None, None))
                findings.append(finding('TARGET_UNREACHED' if closest[0] is not None else 'EVENT_NOT_MEASURABLE', 'WARNING',
                                        'TECHNICAL', frames=[event['frame_start'], event['frame_end']], actor=event['actor_id'],
                                        measured={'event_id': event['event_id'], 'min_distance_m': _r(closest[0]),
                                                  'at_frame': closest[1]}, threshold=thresholds['contact_distance_m']))
            reports.append(report)
            continue
        first, last = contact_frames[0], contact_frames[-1]
        offset_start = first - event['frame_start']
        offset_end = last - event['frame_end']
        report.update(first_contact_frame=first, last_contact_frame=last, timing_offset_frames=[offset_start, offset_end])
        tolerance = thresholds['timing_tolerance_frames']
        if abs(offset_start) > tolerance or abs(offset_end) > tolerance:
            report['timing'] = 'EARLY' if offset_start < -tolerance else 'LATE' if offset_start > tolerance else 'DURATION_MISMATCH'
            findings.append(finding('CONTACT_TIMING_OFF', 'WARNING', 'TECHNICAL', frames=[first, last], actor=event['actor_id'],
                                    measured={'event_id': event['event_id'], 'offset_frames': [offset_start, offset_end],
                                              'offset_seconds': [_r(offset_start / fps), _r(offset_end / fps)]},
                                    threshold=tolerance))
        else:
            report['timing'] = 'ON_TIME'
        if not event['contact']:
            findings.append(finding('UNPLANNED_CONTACT', 'WARNING', 'TECHNICAL', frames=[first, last], actor=event['actor_id'],
                                    measured={'event_id': event['event_id']}))
        if report['max_penetration_m'] and report['max_penetration_m'] > thresholds['max_penetration_m']:
            findings.append(finding('CONTACT_PENETRATION', 'WARNING', 'TECHNICAL', frames=[first, last], actor=event['actor_id'],
                                    measured={'event_id': event['event_id'], 'max_penetration_m': report['max_penetration_m']},
                                    threshold=thresholds['max_penetration_m']))
        local = [s.get('local') for s in samples if s['f'] in contact_frames and s.get('local')]
        if len(local) >= 2:
            sliding = max(distance(p, local[0]) for p in local)
            report['relative_sliding_m'] = _r(sliding)
            report['sliding_frame'] = 'TARGET_LOCAL' if not event['target'].get('surfaces') else 'WORLD_STATIC_SURFACE'
            if sliding > thresholds['sliding_tolerance_m']:
                findings.append(finding('RELATIVE_SLIDING', 'WARNING', 'TECHNICAL', frames=[first, last], actor=event['actor_id'],
                                        measured={'event_id': event['event_id'], 'relative_sliding_m': _r(sliding)},
                                        threshold=thresholds['sliding_tolerance_m']))
        contact_roots = [s['root'] for s in samples if s['f'] in contact_frames]
        effectors = [s['eff'] for s in samples if s['f'] in contact_frames and s.get('eff')]
        report['root_motion_during_contact'] = {
            'root_travel_m': _r(sum(distance(a, b) for a, b in zip(contact_roots, contact_roots[1:]))),
            'effector_world_travel_m': _r(sum(distance(a, b) for a, b in zip(effectors, effectors[1:]))),
            'note': 'measured only; whether the root should move while the contact holds is a review decision'}
        report['status'] = 'CONTACT'
        reports.append(report)
    survey = contact_survey(rows, events, thresholds)
    for row in survey:
        if row['planned_event'] is None:
            findings.append(finding('UNPLANNED_CONTACT', 'WARNING', 'TECHNICAL', frames=row['frames'], actor=row['pair'],
                                    measured={k: row[k] for k in ('samples', 'min_clearance_m', 'max_depth_m')},
                                    threshold=thresholds['contact_distance_m'],
                                    note='surfaces touch or interpenetrate with no planned contact event for this pair'))
    return {'events': reports, 'observed_contacts': survey, 'findings': findings, 'observations': [],
            'metrics': {'events': len(events), 'with_contact': sum(1 for r in reports if r.get('status') == 'CONTACT'),
                        'observed_contact_intervals': len(survey),
                        'unplanned_contact_intervals': sum(1 for r in survey if r['planned_event'] is None)}}


def contact_survey(rows, events, thresholds):
    """Intervals where an actor/prop pair touches (clearance <= contact_distance_m) or interpenetrates, matched against
    planned contact events of the same pair (window widened by window_margin_frames)."""
    rows = sorted(rows, key=lambda r: r['f'])
    frames = [r['f'] for r in rows]
    windows = _contact_windows(events, thresholds['window_margin_frames'])
    out = []
    for (a, b), by_frame in sorted(pair_series(rows).items()):
        flags = []
        for frame in frames:
            value = by_frame.get(frame)
            flags.append(bool(value and (value['count'] > 0 or (value['clearance'] is not None
                                                                 and value['clearance'] <= thresholds['contact_distance_m']))))
        for run in runs(frames, flags):
            values = [by_frame[f] for f in frames if run[0] <= f <= run[1] and f in by_frame]
            planned = None
            for window in windows:
                if window['pair'] == frozenset({a, b}) and window['frames'][0] <= run[1] and run[0] <= window['frames'][1]:
                    planned = window['event_id']
            clear = [v['clearance'] for v in values if v['clearance'] is not None]
            out.append({'pair': [a, b], 'frames': run[:2], 'samples': run[2], 'planned_event': planned,
                        'min_clearance_m': _r(min(clear)) if clear else None,
                        'max_depth_m': _r(max(v['depth'] for v in values)),
                        'unknown_colliders': sorted({n for v in values for n in v['unknown']})})
    return out


# ---- motion ----

def coverage_gaps(rows, start, end):
    """Frames in [start, end] that no active Action and no unmuted NLA strip drives, honouring extrapolation.

    rows: [kind ('ACTIVE'|'STRIP'), action, frame_start, frame_end, extrapolation, track]. NLA rules (measured in 5.2.1,
    docs/cinematic-architecture.md): HOLD keeps the first/last pose on both sides, HOLD_FORWARD only after the strip,
    NOTHING leaves the frames undriven. Blender applies HOLD backwards only for the first strip of a track; later strips
    behave as HOLD_FORWARD. The active Action is treated like a strip spanning its frame range."""
    covered = set()
    first_in_track = {}
    for row in rows:
        if row[0] == 'STRIP' and (row[5] not in first_in_track or row[2] < first_in_track[row[5]]):
            first_in_track[row[5]] = row[2]
    for kind, action, s, e, extrapolation, track in rows:
        lo, hi = math.ceil(s), math.floor(e)
        covered.update(range(max(start, lo), min(end, hi) + 1))
        if extrapolation in ('HOLD', 'HOLD_FORWARD'):
            covered.update(range(max(start, hi), end + 1))
        backwards = extrapolation == 'HOLD' and (kind == 'ACTIVE' or first_in_track.get(track) == s)
        if backwards:
            covered.update(range(start, min(end, lo) + 1))
    return frame_runs([f for f in range(start, end + 1) if f not in covered])


def _steps(values):
    return [None] + [distance(a, b) if a is not None and b is not None else None for a, b in zip(values, values[1:])]


def analyze_motion(target, result, thresholds, events, idle, cache_windows):
    rows = sorted(result['rows'], key=lambda r: r['f'])
    frames = [r['f'] for r in rows]
    fps = result['fps']
    findings = []
    observations = []
    cut_frames = {frames[i] for i in range(1, len(rows))
                  if ((rows[i].get('cam') or {}).get('name')) != ((rows[i - 1].get('cam') or {}).get('name'))}
    cameras = {}
    for i in range(1, len(rows)):
        a, b = rows[i - 1].get('cam'), rows[i].get('cam')
        if not a or not b or a['name'] != b['name'] or frames[i] - frames[i - 1] != 1:
            continue
        cameras.setdefault(b['name'], []).append({'f': frames[i], 'd': distance(a['position'], b['position']),
                                                  'rot': quaternion_angle(a['rotation'], b['rotation']),
                                                  'lens': abs((b.get('lens') or 0.0) - (a.get('lens') or 0.0)),
                                                  'p0': a['position'], 'p1': b['position']})
    camera_metrics = {}
    for name, steps in cameras.items():
        typical = median([s['d'] for s in steps]) or 0.0
        limit = max(thresholds['camera_jump_m'], thresholds['jump_ratio'] * typical)
        speeds = [s['d'] * fps for s in steps]
        accel = [abs(b - a) * fps for a, b in zip(speeds, speeds[1:])]
        jerk = [abs(b - a) * fps for a, b in zip(accel, accel[1:])]
        camera_metrics[name] = {'max_speed_m_s': _r(max(speeds, default=0.0)), 'max_accel_m_s2': _r(max(accel, default=0.0)),
                                'max_jerk_m_s3': _r(max(jerk, default=0.0)),
                                'max_rotation_deg_per_frame': _r(max((s['rot'] or 0.0 for s in steps), default=0.0)),
                                'max_lens_change_mm_per_frame': _r(max((s['lens'] for s in steps), default=0.0))}
        for s in steps:
            if s['d'] > limit:
                findings.append(finding('CAMERA_JUMP_UNEXPLAINED', 'WARNING', 'TECHNICAL', frames=[s['f'] - 1, s['f']],
                                        measured={'camera': name, 'step_m': _r(s['d']), 'typical_step_m': _r(typical)},
                                        threshold={'camera_jump_m': thresholds['camera_jump_m'], 'jump_ratio': thresholds['jump_ratio']},
                                        note='same camera on consecutive frames: not explained by a cut'))
            if s['rot'] is not None and s['rot'] > thresholds['camera_rotation_jump_deg']:
                findings.append(finding('CAMERA_ROTATION_JUMP', 'WARNING', 'TECHNICAL', frames=[s['f'] - 1, s['f']],
                                        measured={'camera': name, 'rotation_deg': _r(s['rot'])},
                                        threshold=thresholds['camera_rotation_jump_deg']))
            if s['lens'] > thresholds['lens_jump_mm']:
                findings.append(finding('LENS_JUMP', 'WARNING', 'TECHNICAL', frames=[s['f'] - 1, s['f']],
                                        measured={'camera': name, 'lens_change_mm': _r(s['lens'])}, threshold=thresholds['lens_jump_mm']))
        for index, value in enumerate(accel):
            if value > max(thresholds['min_accel_m_s2'], thresholds['accel_ratio'] * (median(accel) or 0.0)) \
                    and all(s['d'] <= limit for s in steps[index:index + 2]):
                observations.append(observation('CAMERA_ACCELERATION_SPIKE', None, None,
                                                {'camera': name, 'frame': steps[index + 1]['f'], 'accel_m_s2': _r(value)},
                                                thresholds['min_accel_m_s2']))
    coverage = result['coverage']
    gaps = {}
    for actor_id, info in result['static']['coverage'].items():
        if actor_id not in target['selected'] or actor_id in idle:
            continue
        found = coverage_gaps(info['rows'], coverage['frame_start'], coverage['frame_end'])
        gaps[actor_id] = found
        for run in found:
            findings.append(finding('ACTION_GAP', 'WARNING', 'TECHNICAL', frames=run, actor=actor_id,
                                    measured={'clips': len([r for r in info['rows'] if r[0] == 'STRIP']),
                                              'active_action': any(r[0] == 'ACTIVE' for r in info['rows'])},
                                    note='no Action or NLA strip drives the actor here: NLA-animated channels fall back to their default values '
                                         '(measured in 5.2.1: object location 0, the actor jumps to the origin)'))
    actor_metrics = {}
    for actor_id in result['actors']:
        if actor_id not in target['selected']:
            continue
        positions = [((r.get('m') or {}).get('roots') or {}).get(actor_id) for r in rows]
        points = [p[:3] if p else None for p in positions]
        steps = _steps(points)
        valid = [s for s in steps if s is not None]
        typical = median(valid) or 0.0
        limit = max(thresholds['root_jump_m'], thresholds['jump_ratio'] * typical)
        speeds = [None if s is None else s * fps for s in steps]
        accel = [None] * len(speeds)
        for i in range(1, len(speeds)):
            if speeds[i] is not None and speeds[i - 1] is not None:
                accel[i] = abs(speeds[i] - speeds[i - 1]) * fps
        jumps = []
        for i, s in enumerate(steps):
            if s is not None and s > limit:
                at_cut = frames[i] in cut_frames
                jumps.append(frames[i])
                at_gap = any(frames[i - 1] == run[1] or frames[i] == run[0] for run in gaps.get(actor_id) or [])
                findings.append(finding('ROOT_JUMP_AT_CUT' if at_cut else 'ROOT_JUMP', 'INFO' if at_cut else 'WARNING',
                                        'TECHNICAL', frames=[frames[i - 1], frames[i]], actor=actor_id,
                                        measured={'step_m': _r(s), 'typical_step_m': _r(typical),
                                                  'root_source': result['actors'][actor_id]['root_source'],
                                                  'context': 'ACTION_GAP_BOUNDARY' if at_gap else None},
                                        threshold={'root_jump_m': thresholds['root_jump_m'], 'jump_ratio': thresholds['jump_ratio']}))
        accel_values = [a for a in accel if a is not None]
        accel_limit = max(thresholds['min_accel_m_s2'], thresholds['accel_ratio'] * (median(accel_values) or 0.0))
        spikes = [frames[i] for i, a in enumerate(accel) if a is not None and a > accel_limit
                  and not any(abs(frames[i] - j) <= 1 for j in jumps)]
        for frame in spikes:
            findings.append(finding('VELOCITY_DISCONTINUITY', 'WARNING', 'TECHNICAL', frames=[frame - 1, frame], actor=actor_id,
                                    measured={'accel_m_s2': _r(accel[frames.index(frame)])},
                                    threshold={'min_accel_m_s2': thresholds['min_accel_m_s2'], 'accel_ratio': thresholds['accel_ratio']}))
        actor_metrics[actor_id] = {'max_speed_m_s': _r(max((s for s in speeds if s is not None), default=0.0)),
                                   'max_accel_m_s2': _r(max(accel_values, default=0.0)), 'root_jumps': len(jumps),
                                   'velocity_discontinuities': len(spikes),
                                   'root_source': result['actors'][actor_id]['root_source']}
    for event in events:
        runs_ = gaps.get(event['actor_id']) or []
        if any(run[0] <= f <= run[1] for run in runs_ for f in (event['frame'], event['frame_start'], event['frame_end'])):
            findings.append(finding('EVENT_OUTSIDE_CLIP', 'WARNING', 'TECHNICAL', frames=[event['frame_start'], event['frame_end']],
                                    actor=event['actor_id'], measured={'event_id': event['event_id']},
                                    note='a planned event falls on frames no clip drives (synchronisation error)'))
    caches = []
    for cache in result['static']['caches']:
        window = cache_windows.get(cache['object'])
        row = dict(cache, declared_window=window)
        if window is None:
            row['coverage'] = 'UNKNOWN'
            findings.append(finding('PHYSICS_CACHE_COVERAGE_UNKNOWN', 'INFO', 'TECHNICAL', measured={'object': cache['object']},
                                    note='no cached window declared; the PC2 frame count is not readable from Blender RNA'))
        else:
            missing = frame_runs([f for f in range(coverage['frame_start'], coverage['frame_end'] + 1)
                                  if not window[0] <= f <= window[1]])
            row['gaps'] = missing
            for run in missing:
                findings.append(finding('PHYSICS_CACHE_GAP', 'WARNING', 'TECHNICAL', frames=run,
                                        measured={'object': cache['object'], 'window': window}))
        caches.append(row)
    lights = {}
    for i in range(1, len(rows)):
        if frames[i] - frames[i - 1] != 1:
            continue
        a, b = rows[i - 1].get('m') or {}, rows[i].get('m') or {}
        at_cut = frames[i] in cut_frames
        for name, energy in (b.get('lights') or {}).items():
            before = (a.get('lights') or {}).get(name)
            if before is None:
                continue
            ratio = abs(energy - before) / max(abs(before), 1e-6)
            lights.setdefault(name, []).append(ratio)
            if ratio > thresholds['light_jump_ratio']:
                findings.append(finding('LIGHTING_JUMP_AT_CUT' if at_cut else 'LIGHTING_JUMP', 'INFO' if at_cut else 'WARNING',
                                        'TECHNICAL', frames=[frames[i - 1], frames[i]],
                                        measured={'light': name, 'energy': [before, energy], 'relative_change': _r(ratio)},
                                        threshold=thresholds['light_jump_ratio']))
        if a.get('exposure') is not None and b.get('exposure') is not None:
            change = abs(b['exposure'] - a['exposure'])
            if change > thresholds['exposure_jump_ev']:
                findings.append(finding('EXPOSURE_JUMP_AT_CUT' if at_cut else 'EXPOSURE_JUMP', 'INFO' if at_cut else 'WARNING',
                                        'TECHNICAL', frames=[frames[i - 1], frames[i]], measured={'exposure_change_ev': _r(change)},
                                        threshold=thresholds['exposure_jump_ev']))
        for index, (wa, wb) in enumerate(zip(a.get('world') or [], b.get('world') or [])):
            ratio = abs(wb - wa) / max(abs(wa), 1e-6)
            if ratio > thresholds['light_jump_ratio']:
                findings.append(finding('WORLD_STRENGTH_JUMP', 'INFO' if at_cut else 'WARNING', 'TECHNICAL',
                                        frames=[frames[i - 1], frames[i]], measured={'strength': [wa, wb]},
                                        threshold=thresholds['light_jump_ratio']))
    return {'cameras': camera_metrics, 'actors': actor_metrics, 'cuts': sorted(cut_frames), 'action_gaps': gaps,
            'caches': caches, 'lights': {n: {'max_relative_change': _r(max(v))} for n, v in lights.items()},
            'findings': findings, 'observations': observations,
            'metrics': {'cameras': len(camera_metrics), 'actors': len(actor_metrics), 'cuts': len(cut_frames)}}


# ----------------------------------------------------------------------------------------------------------------
# Host: report assembly.
# ----------------------------------------------------------------------------------------------------------------

LIMITATIONS = {
    'composition': ['Screen bounds come from evaluated object bounds (8 corners per mesh): an upper bound of the silhouette, '
                    'not the silhouette itself.', 'Clip tests are conservative (bounds, not polygons).',
                    'Creative observations describe measurements; they are not errors and not a score.'],
    'continuity': ['Axis side is measured on the ground plane (world XY) at the last sample before and the first sample '
                   'after each cut.', 'Screen direction needs two samples on each side of a cut.',
                   'Eyeline needs explicit gaze specs (head bone + forward axis); bone axes are never inferred.'],
    'visibility': ['Rays go from the camera to a deterministic subset of evaluated vertices (options.ray_points) and to the '
                   'head point; thin occluders between samples can be missed.',
                   'Objects hidden in render (object or collection hide_render, visible_camera off) are skipped by hand '
                   'because scene.ray_cast still hits them (measured).',
                   'Transparent materials are not modelled: any surface blocks the ray.'],
    'collisions': ['Vertex samples can miss edge-face intersections.', 'Finite frames never certify continuous collision freedom.',
                   'Open or non-manifold meshes return UNKNOWN, never zero.', 'Intent comes only from planned contact events.'],
    'contacts': ['Contact is distance-based on evaluated geometry at sampled frames; confidence is evidence, not probability.',
                 'Relative sliding is measured in the target bone/object space (or world for static surfaces).',
                 'Effectors and targets must be explicit (bones are never guessed).'],
    'motion': ['Derivatives use consecutive integer frames only.', 'Cache coverage needs a declared window (PC2 length is not '
               'exposed by RNA).', 'Light, exposure and world values are the ones evaluated after frame_set.'],
}


def _thresholds_kind(kind, opts):
    return thresholds_for(kind, opts.get('thresholds'))


def _ray_points(opts):
    return _integer(opts.get('ray_points', 16), 'ray_points', 1, 64)


def _intended_rows(opts, actor_ids):
    rows = opts.get('intended_occlusions') or []
    if not isinstance(rows, list) or len(rows) > 64:
        fail('CINEMATIC_OPTION_INVALID', 'intended_occlusions must be a list of at most 64 rows')
    out = []
    for index, row in enumerate(rows):
        row = model.closed(row, {'actor_id', 'frame_start', 'frame_end', 'occluder'})
        if row.get('actor_id') not in actor_ids:
            fail('ACTOR_NOT_ANALYSED', 'intended_occlusions[' + str(index) + ']')
        start = _integer(row.get('frame_start'), 'intended_occlusions.frame_start')
        end = _integer(row.get('frame_end'), 'intended_occlusions.frame_end')
        out.append({'actor_id': row['actor_id'], 'frame_start': start, 'frame_end': end, 'occluder': row.get('occluder')})
    return out


def _facial_rows(opts, state, actor_ids):
    rows = list(opts.get('facial_events') or [])
    if state and isinstance(state.get('facial_events'), list):
        rows += state['facial_events']
    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get('actor_id') not in actor_ids:
            fail('CINEMATIC_FACIAL_EVENT_INVALID', 'facial_events[' + str(index) + '] needs an analysed actor_id and frame')
        out.append({'actor_id': row['actor_id'], 'frame': _integer(row.get('frame'), 'facial_events.frame'),
                    'label': str(row.get('label'))[:80] if row.get('label') else None})
    if len(out) > 128:
        fail('CINEMATIC_FACIAL_EVENT_INVALID', 'at most 128 facial events')
    return out


def _gaze_specs(opts, actors):
    rows = opts.get('gaze') or []
    if not isinstance(rows, list) or len(rows) > 8:
        fail('CINEMATIC_OPTION_INVALID', 'gaze must be a list of at most 8 specs')
    ids = {a['actor_id'] for a in actors}
    out = []
    for index, row in enumerate(rows):
        row = model.closed(row, {'actor_id', 'head_bone', 'forward_axis', 'target_actor_id', 'target_object', 'target_point'})
        if row.get('actor_id') not in ids:
            fail('ACTOR_NOT_ANALYSED', 'gaze[' + str(index) + ']')
        actor = next(a for a in actors if a['actor_id'] == row['actor_id'])
        head = row.get('head_bone') or actor['head_bone']
        if not head:
            fail('GAZE_HEAD_BONE_REQUIRED', 'gaze[' + str(index) + '] needs head_bone (option or registry)')
        axis = model.vector(row.get('forward_axis'), 'gaze.forward_axis', 3, -1, 1)
        if math.sqrt(sum(v * v for v in axis)) < 1e-6:
            fail('GAZE_FORWARD_AXIS_REQUIRED', 'gaze[' + str(index) + '].forward_axis must be non-zero (bone local axis)')
        if row.get('target_actor_id') and row['target_actor_id'] not in ids:
            fail('ACTOR_NOT_ANALYSED', 'gaze[' + str(index) + '].target_actor_id')
        spec = {'index': index, 'actor_id': row['actor_id'], 'head_bone': head, 'forward_axis': axis,
                'target_actor_id': row.get('target_actor_id'), 'target_object': row.get('target_object'),
                'target_point': model.vector(row['target_point'], 'gaze.target_point') if row.get('target_point') else None}
        if not (spec['target_actor_id'] or spec['target_object'] or spec['target_point']):
            fail('TARGET_REQUIRED', 'gaze[' + str(index) + '] needs a target')
        out.append(spec)
    return out


def _axis_option(axis, ids):
    """Action line: {actor_a, actor_b} (two actors) or {actor, target_point (m)}."""
    if axis is None:
        return None
    axis = model.closed(axis, {'actor_a', 'actor_b', 'actor', 'target_point'})
    if axis.get('actor_a') or axis.get('actor_b'):
        if axis.get('actor_a') not in ids or axis.get('actor_b') not in ids or axis['actor_a'] == axis['actor_b']:
            fail('CINEMATIC_AXIS_INVALID', 'actor_a and actor_b must be two different analysed actors')
        return axis
    if axis.get('actor') in ids and axis.get('target_point') is not None:
        model.vector(axis['target_point'], 'axis.target_point')
        return axis
    fail('CINEMATIC_AXIS_INVALID', 'axis needs {actor_a, actor_b} or {actor, target_point}')


def _cache_windows(opts, state):
    windows = {}
    for actor in ((state or {}).get('actors') or {}).values():
        for row in (actor or {}).get('secondary') or []:
            if isinstance(row, dict) and row.get('object') and isinstance(row.get('window') or row.get('frame_range'), list):
                window = row.get('window') or row.get('frame_range')
                if len(window) == 2:
                    windows[row['object']] = [int(window[0]), int(window[1])]
    explicit = opts.get('cache_windows') or {}
    if not isinstance(explicit, dict):
        fail('CINEMATIC_OPTION_INVALID', 'cache_windows maps object name to [frame_start, frame_end]')
    for name, window in explicit.items():
        if not isinstance(window, list) or len(window) != 2:
            fail('CINEMATIC_OPTION_INVALID', 'cache_windows.' + str(name))
        windows[name] = [_integer(window[0], 'cache_windows.start'), _integer(window[1], 'cache_windows.end')]
    return windows


def _names(value, field, maximum=32):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(v, str) or not v for v in value):
        fail('CINEMATIC_OPTION_INVALID', field + ' must be a list of at most ' + str(maximum) + ' names')
    return list(value)


def _status(findings, restored, unknown_blocks=False):
    errors = [f for f in findings if f['severity'] == 'ERROR']
    warnings = [f for f in findings if f['severity'] == 'WARNING']
    passed = restored and not errors and not unknown_blocks
    if not restored:
        return 'error', False
    return ('success' if passed and not warnings else 'warning'), passed


def _paginate(rows, limit, offset):
    return rows[offset:offset + limit], {'total': len(rows), 'offset': offset, 'limit': limit,
                                         'next_offset': offset + limit if offset + limit < len(rows) else None}


def _counts(rows, key='code'):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + 1
    return dict(sorted(out.items()))


def _failures(findings, restored, unknown):
    """Why passed is false, over ALL findings (not only the returned page), so a FAIL is never reported bare."""
    out = []
    if not restored:
        out.append('CINEMATIC_STATE_NOT_RESTORED: frame, scene.camera, channels or fingerprints differ after sampling')
    for code, count in _counts([f for f in findings if f['severity'] == 'ERROR']).items():
        out.append(code + ': ' + str(count) + ' ERROR finding(s)')
    if unknown:
        out.append('COLLISION_UNKNOWN_OPEN_MESH: an open or non-manifold collider leaves collision freedom unknown')
    return out


def _finish(kind, target, result, analysis, thresholds, overrides, opts, extra=None):
    limit, offset, detail = _page(opts)
    restoration = result['restoration']
    unchanged = result['fingerprints_before'] == result['fingerprints_after']
    restored = bool(restoration['restored']) and unchanged
    findings = _sorted_findings(analysis.pop('findings'))
    observations = analysis.pop('observations')
    unknown = kind == 'collisions' and analysis['metrics'].get('unknown')
    status, passed = _status(findings, restored, unknown)
    page, pagination = _paginate(findings, limit, offset)
    obs_page, obs_pagination = _paginate(observations, limit, offset)
    report = {'status': status, 'passed': passed, 'kind': kind, 'schema': SCHEMA.format(kind=kind), 'mode': target['mode'],
              'scene': result['scene'], 'fps': result['fps'], 'unit_scale': result['unit_scale'],
              'resolution': result['resolution'], 'coverage': result['coverage'],
              'actors': {k: v for k, v in result['actors'].items() if k in target['selected']},
              'state_restored': restored,
              'restoration': {'state_digest_equal': restoration['state_digest_before'] == restoration['state_digest_after'],
                              'source_frame_and_camera_equal': restoration['source_before'] == restoration['source_after'],
                              'fingerprints_equal': unchanged},
              'findings': page, 'findings_pagination': pagination, 'findings_by_code': _counts(findings),
              'findings_by_severity': _counts(findings, 'severity'),
              'creative_observations': obs_page, 'creative_observations_pagination': obs_pagination,
              'thresholds': thresholds, 'threshold_source': 'PROPOSAL',
              'threshold_overrides': {k: 'USER_REQUIREMENT' for k in overrides},
              'limitations': LIMITATIONS[kind], 'visual_verification': 'required',
              'technical_findings_are_not_artistic_judgement': True}
    if target['dropped_subjects']:
        report['ignored_shot_subjects'] = target['dropped_subjects']
    for key, value in analysis.items():
        if key in {'per_shot', 'pairs', 'events', 'cuts'} and isinstance(value, list):
            report[key], report[key + '_pagination'] = _paginate(value, limit, offset)
        else:
            report[key] = value
    if result['hidden_skipped']:
        report['hidden_in_render_skipped'] = dict(sorted(result['hidden_skipped'].items()))
    if detail == 'samples':
        report['samples'], report['samples_pagination'] = _paginate(result['rows'], min(limit, 100), offset)
    if not restored:
        report['errors'] = [{'code': 'CINEMATIC_STATE_NOT_RESTORED',
                             'message': 'frame, scene.camera, channels or fingerprints differ after the analysis; inspect '
                                        'the candidate before any further step'}]
    if not passed:
        report['failures'] = _failures(findings, restored, unknown)
    report.update(extra or {})
    return report


def _report(kind, ctx, target, opts):
    thresholds, overrides = _thresholds_kind(kind, opts)
    ids = [a['actor_id'] for a in target['actors']]
    extra = {}
    frame_plan_extra = []
    explicit = None
    cuts = False
    events = []
    ignored = []
    event_sources = []
    if kind in ('collisions', 'contacts', 'motion'):
        rows, event_sources = _plan_events(target['plan'], target['state'], opts) if target['mode'] == 'PLAN' \
            else ((opts.get('events') or []), ['OPTIONS'] if opts.get('events') else [])
        if not isinstance(rows, list) or len(rows) > 256:
            fail('CINEMATIC_EVENTS_INVALID', 'events must be a list of at most 256 rows')
        events, ignored = _events(rows, ids)
    params_extra = {}
    if kind == 'continuity':
        cuts = True
        shots = target['shots']
        for shot in shots:
            frame_plan_extra += [shot['frame_start'] - 2, shot['frame_start'] - 1, shot['frame_start'], shot['frame_start'] + 1,
                                 shot['frame_end'] - 2, shot['frame_end'] - 1, shot['frame_end']]
        timeline = target['timeline']
        start = opts.get('frame_start', timeline['frame_start'] if timeline else None)
        end = opts.get('frame_end', timeline['frame_end'] if timeline else None)
        if opts.get('step') is None and start is not None and end is not None:
            explicit = [f for f in frame_plan_extra if start <= f <= end] + [start, end]
        _axis_option(opts.get('axis'), ids)
        gaze = _gaze_specs(opts, target['actors'])
        target['gaze'] = gaze
        params_extra['gaze'] = gaze
        ray_frames = sorted({s['frame_start'] for s in shots})
        params_extra['ray_frames'] = ray_frames
        params_extra['ray_frames_add_cuts'] = True
    if kind == 'visibility':
        params_extra['ray_points'] = _ray_points(opts)
        facial = _facial_rows(opts, target['state'], ids)
        frame_plan_extra += [e['frame'] for e in facial]
        intended = _intended_rows(opts, ids)
    if kind == 'collisions':
        props = _names(opts.get('props'), 'props')
        target['props'] = props
        params_extra.update(props=props, max_vertex_frames=thresholds['max_vertex_frames'],
                            proximity_margin_m=thresholds['proximity_margin_m'])
        margin = thresholds['contact_window_margin_frames']
        frame_plan_extra += [f for e in events if e['contact'] for f in range(e['frame_start'] - margin, e['frame_end'] + margin + 1)]
    if kind == 'contacts':
        margin = thresholds['window_margin_frames']
        timeline = target['timeline']
        for event in events:
            lo, hi = event['frame_start'] - margin, event['frame_end'] + margin
            if timeline:
                lo, hi = max(lo, timeline['frame_start']), min(hi, timeline['frame_end'])
            event['window'] = [lo, hi]
        if events and opts.get('step') is None and opts.get('frame_start') is None and opts.get('frame_end') is None:
            # Dense windows around the planned events; without events the whole range is surveyed.
            explicit = sorted({f for e in events for f in range(e['window'][0], e['window'][1] + 1)})
        props = _names(opts.get('props'), 'props')
        target['props'] = props
        surfaces = sorted({s for e in events for s in e['target'].get('surfaces') or []}
                          | set(_names(opts.get('support_surfaces'), 'support_surfaces')))
        if len(surfaces) > 32:
            fail('CINEMATIC_OPTION_INVALID', 'at most 32 support surfaces')
        params_extra.update(events=events, support_surfaces=surfaces, query_distance_m=thresholds['query_distance_m'],
                            props=props, max_vertex_frames=thresholds['max_vertex_frames'],
                            proximity_margin_m=thresholds['contact_distance_m'])
    if kind == 'motion':
        idle = _names(opts.get('idle_actors'), 'idle_actors', model.MAX_ACTORS)
        windows = _cache_windows(opts, target['state'])
    plan = _frame_plan(kind, opts, target['timeline'], extra=frame_plan_extra, explicit=explicit, cuts=cuts)
    params = build_params(kind, target, opts, thresholds, params_extra)
    params['frames'] = plan
    if kind == 'continuity' and params.get('ray_frames') is not None:
        params['ray_frames'] = sorted(set(params['ray_frames']))
    result = run_payload(ctx, target, params)
    if kind == 'continuity':
        markers = [m[1] for m in result['static']['markers'] if m[2] is not None]
        analysis = analyze_continuity(target, result, thresholds, opts)
        extra['marker_cut_frames'] = markers
    elif kind == 'composition':
        analysis = analyze_composition(target, result, thresholds)
    elif kind == 'visibility':
        analysis = analyze_visibility(target, result, thresholds, facial, intended)
    elif kind == 'collisions':
        analysis = analyze_collisions(target, result, thresholds, events)
        extra['capacity'] = result['capacity']
    elif kind == 'contacts':
        analysis = analyze_contacts(target, result, thresholds, params['events'])
    else:
        analysis = analyze_motion(target, result, thresholds, events, idle, windows)
    if kind in ('collisions', 'contacts', 'motion'):
        extra['events_source'] = event_sources
        extra['events_ignored'] = ignored
    return _finish(kind, target, result, analysis, thresholds, overrides, opts, extra)


def _plan_report(kind, ctx, plan, state, options):
    opts = model.closed(options, OPTIONS[kind])
    target = plan_target(ctx, plan, state, opts)
    return _report(kind, ctx, target, opts)


def composition_report(ctx, plan, state, options=None):
    return _plan_report('composition', ctx, plan, state, options)


def continuity_report(ctx, plan, state, options=None):
    return _plan_report('continuity', ctx, plan, state, options)


def visibility_report(ctx, plan, state, options=None):
    return _plan_report('visibility', ctx, plan, state, options)


def collision_report(ctx, plan, state, options=None):
    return _plan_report('collisions', ctx, plan, state, options)


def contact_report(ctx, plan, state, options=None):
    return _plan_report('contacts', ctx, plan, state, options)


def motion_report(ctx, plan, state, options=None):
    return _plan_report('motion', ctx, plan, state, options)


REPORTS = {'composition': composition_report, 'continuity': continuity_report, 'visibility': visibility_report,
           'collisions': collision_report, 'contacts': contact_report, 'motion': motion_report}


def run_tool(kind, ctx, req):
    """Handler body shared by the six TRANSIENT tools. Analysis never applies changes and records no ledger event:
    the quality report (or the caller) decides what to keep."""
    if req.apply:
        fail('CINEMATIC_ANALYSIS_READ_ONLY', 'analysis tools sample and restore; apply=true is not accepted')
    if req.brief is not None:
        fail('CINEMATIC_OPTION_UNSUPPORTED', 'analysis tools take no brief')
    opts = model.closed(req.options, OPTIONS[kind])
    if req.cinematic:
        path, plan, state = store.load(ctx.storage, req.cinematic)
        if req.actor:
            if opts.get('actors') is not None:
                fail('CINEMATIC_OPTION_CONFLICT', 'pass either actor or options.actors')
            opts['actors'] = [req.actor]
        target = plan_target(ctx, plan, state, opts)
        report = _report(kind, ctx, target, opts)
        report['cinematic'] = str(path)
        report['generation_id'] = plan['generation_id']
        report['plan_state'] = state.get('state')
        return report
    if req.actor:
        fail('CINEMATIC_REFERENCE_REQUIRED', 'actor ids belong to a plan; in scene mode pass object_name or options.actors')
    target = scene_target(req, opts)
    report = _report(kind, ctx, target, opts)
    report['note'] = 'SCENE mode: no plan, no freshness gate; measurements of the named scene only'
    return report


@handler('cinematic_analyze_composition')
def analyze_composition_tool(ctx, req):
    return run_tool('composition', ctx, req)


@handler('cinematic_validate_continuity')
def validate_continuity_tool(ctx, req):
    return run_tool('continuity', ctx, req)


@handler('cinematic_analyze_visibility')
def analyze_visibility_tool(ctx, req):
    return run_tool('visibility', ctx, req)


@handler('cinematic_analyze_actor_collisions')
def analyze_collisions_tool(ctx, req):
    return run_tool('collisions', ctx, req)


@handler('cinematic_analyze_contacts')
def analyze_contacts_tool(ctx, req):
    return run_tool('contacts', ctx, req)


@handler('cinematic_analyze_motion')
def analyze_motion_tool(ctx, req):
    return run_tool('motion', ctx, req)
