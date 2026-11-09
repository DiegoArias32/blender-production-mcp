"""M10 area B: source scene inventory, actor registry and source integrity.

Tools (all registered through service.handler):
- cinematic_inspect_scene    READ_ONLY  compact, paginated inventory of a source scene; never the whole graph.
- cinematic_register_actor   writer     host-side registry record; never changes Blender (allowed on MASTER).
- cinematic_inspect_actor    READ_ONLY  registry record plus a fresh/stale comparison of its current digests.
- cinematic_verify_integrity READ_ONLY  per-actor digests and source/candidate scene fingerprints of a plan.

Exported for the other M10 subsystems:
    integrity_report(ctx, plan, state) -> dict
    actor_record(ctx, actor_id, key=None, scene=None) -> dict
    actor_spec(record) -> dict          (what the digest payload re-measures for one actor)

Identity is never the object name alone: the record carries an asset fingerprint (rig hierarchy and rest matrices,
bound meshes' topology/rest positions/UV/shape keys/material slots and indices) plus every registered input (role,
bones, body Action digests, M7/M8 references). Nothing here calls frame_set or assigns Actions: root motion is read
from raw F-Curves with fcurve.evaluate(frame), digests come from the original datablocks. Only digests travel back
to the host; vertices, keys and curves never do.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import inspect
import math
import re

from ..common.asset_safety import current_asset
from ..common.operations import fingerprint
from ..rig.semantics import SEMANTIC_RUNTIME
from . import model, registry, store
from .runtime import COMMON
from .service import handler

ACTOR_SCHEMA = 'production.cinematic_actor.v1'
SCENE_SCHEMA = 'production.cinematic_scene_inspection.v1'
INTEGRITY_SCHEMA = 'production.cinematic_integrity.v1'
BONE_ROLES = ('root', 'pelvis', 'head', 'neck', 'hand_left', 'hand_right', 'foot_left', 'foot_right')
# Registry role -> semantic role name produced by rig/semantics.py (m3_semantic_model).
SEMANTIC_ROLES = {'root': 'root', 'pelvis': 'pelvis', 'head': 'head', 'neck': 'neck', 'hand_left': 'left_hand',
                  'hand_right': 'right_hand', 'foot_left': 'left_foot', 'foot_right': 'right_foot'}
# AGENTS/M3 convention: semantic assignments below 0.9 need a reviewed explicit mapping. It is a declared gate, not a
# calibrated probability (rig/semantics.py says so in its own warnings).
SEMANTIC_CONFIDENCE_MIN = 0.9
SCENE_SECTIONS = ('markers', 'collections', 'cameras', 'lights', 'actor_candidates', 'actions', 'facial_m7',
                  'secondary_m8', 'props')
DETAILS = ('summary', 'full')
REGISTER_OPTIONS = {'role', 'facial', 'secondary', 'bones', 'body_actions', 'source_scene'}
MAX_REFERENCES = 8
MAX_BODY_ACTIONS = 32
MAX_CHECKED_ROWS = 200
_ACTOR_ID = re.compile(r'^[0-9a-f]{16}$')


def cax_motion_summary(points, frames, fps):
    """Pure function (runs on the host in tests and inside Blender): horizontal root motion from world positions in
    metres, one per sampled frame. Blender is Z-up, so horizontal means XY. No threshold is applied anywhere: the
    dominant axis is the larger end component, NONE only when the end displacement is exactly zero."""
    origin = points[0]
    deltas = [[p[0] - origin[0], p[1] - origin[1], p[2] - origin[2]] for p in points]
    path = 0.0
    excursion = 0.0
    for index in range(1, len(deltas)):
        path += math.hypot(deltas[index][0] - deltas[index - 1][0], deltas[index][1] - deltas[index - 1][1])
    for delta in deltas:
        excursion = max(excursion, math.hypot(delta[0], delta[1]))
    end = deltas[-1]
    if end[0] == 0.0 and end[1] == 0.0:
        axis = 'NONE'
    elif abs(end[0]) > abs(end[1]):
        axis = '+X' if end[0] > 0 else '-X'
    elif abs(end[1]) > abs(end[0]):
        axis = '+Y' if end[1] > 0 else '-Y'
    else:
        axis = 'TIE_XY'
    heights = [delta[2] for delta in deltas]
    distance = math.hypot(end[0], end[1])
    return {'horizontal_displacement_m': [end[0], end[1]], 'horizontal_distance_m': distance,
            'direction': [end[0] / distance, end[1] / distance] if distance > 0.0 else None,
            'vertical_distance_m': max(heights) - min(heights),
            'path_length_m': path, 'max_horizontal_excursion_m': excursion,
            'vertical_offset_range_m': [min(heights), max(heights)], 'dominant_axis': axis,
            'sampled_range': [frames[0], frames[-1]], 'sampled_frames': len(frames),
            'duration_s': (frames[-1] - frames[0]) / fps if fps > 0 else None}


ACTORS_RUNTIME = r'''
from array import array

def cax_scene(name):
    if name:
        scene = bpy.data.scenes.get(name)
        if scene is None:
            cm_fail('CINEMATIC_SCENE_MISSING', name)
        return scene
    return bpy.context.scene

def cax_prop(value):
    # Custom properties travel as JSON; anything that is not a plain scalar is reported as text.
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

def cax_owner(idblock):
    if idblock is None:
        return None
    if idblock.get('production_m10_owner'):
        return 'M10'
    if idblock.get('production_m8_owned'):
        return 'M8'
    if idblock.get('production_facial_owner') == 'M7':
        return 'M7'
    return None

def cax_animated(idblock):
    ad = getattr(idblock, 'animation_data', None)
    if ad is None:
        return False
    if ad.action is not None or len(ad.drivers) > 0:
        return True
    for track in ad.nla_tracks:
        if len(track.strips) > 0:
            return True
    return False

def cax_shape_animated(obj):
    if obj.type != 'MESH' or obj.data.shape_keys is None:
        return False
    return cax_animated(obj.data.shape_keys)

def cax_buffer(collection, field, width, kind):
    # foreach_get into a typed buffer and digest its bytes: the M5 pattern (m5_buffer), measured fast enough for
    # production meshes, and nothing but the digest leaves Blender.
    data = array(kind, [0]) * (len(collection) * width)
    if len(data) > 0:
        collection.foreach_get(field, data)
    return m2_fast_digest(data.tobytes())

def cax_target(constraint):
    target = getattr(constraint, 'target', None)
    return target.name if target is not None else None

def cax_bound_meshes(obj, scene):
    # Meshes that belong to the actor: the object itself when it is a mesh, mesh children, and meshes of the source
    # scene deformed by it through an Armature modifier. M8/M10 copies are never part of a source actor.
    names = []
    if obj.type == 'MESH':
        names.append(obj.name)
    for child in obj.children:
        if child.type == 'MESH' and child.name not in names and cax_owner(child) not in ('M8', 'M10'):
            names.append(child.name)
    if obj.type == 'ARMATURE' and scene is not None:
        for other in scene.objects:
            if other.type != 'MESH' or other.name in names or cax_owner(other) in ('M8', 'M10'):
                continue
            for mod in other.modifiers:
                if mod.type == 'ARMATURE' and mod.object == obj:
                    names.append(other.name)
                    break
    return sorted(names)

def cax_mesh(obj):
    # Rest data only (mesh.vertices, not the evaluated mesh): animated shape-key values and eval_time are excluded on
    # purpose because M7 NLA and every frame change rewrite them; the key coordinates themselves are covered.
    me = obj.data
    keys = me.shape_keys
    shapes = None
    names = []
    if keys is not None:
        names = [block.name for block in keys.key_blocks]
        shapes = m2_digest([keys.use_relative, [[block.name, block.relative_key.name, block.slider_min, block.slider_max,
                                                  block.vertex_group, block.interpolation,
                                                  cax_buffer(block.data, 'co', 3, 'f')] for block in keys.key_blocks]])
    sections = {'topology': m2_digest([len(me.vertices), cax_buffer(me.edges, 'vertices', 2, 'i'),
                                       cax_buffer(me.loops, 'vertex_index', 1, 'i'),
                                       cax_buffer(me.polygons, 'loop_start', 1, 'i'),
                                       cax_buffer(me.polygons, 'loop_total', 1, 'i')]),
                'positions': cax_buffer(me.vertices, 'co', 3, 'f'),
                'uv': m2_digest([[layer.name, cax_buffer(layer.data, 'uv', 2, 'f')] for layer in me.uv_layers]),
                'shape_keys': shapes,
                'material_slots': m2_digest([[slot.link, slot.material.name if slot.material else None]
                                             for slot in obj.material_slots]),
                'material_index': cax_buffer(me.polygons, 'material_index', 1, 'i'),
                'vertex_groups': m2_digest([group.name for group in obj.vertex_groups])}
    return {'object': obj.name, 'data': me.name, 'sections': sections, 'key': keys.name if keys is not None else None,
            'shape_key_names': names[:64],
            'counts': {'vertices': len(me.vertices), 'edges': len(me.edges), 'polygons': len(me.polygons),
                       'uv_layers': len(me.uv_layers), 'shape_keys': len(names), 'material_slots': len(obj.material_slots)}}

def cax_rig(obj):
    # Hierarchy, flags and rest matrices of every bone, pose-bone rotation modes and constraint wiring, and drivers.
    # Pose channels, constraint influence/mute and custom properties are animatable state and stay out.
    rows = []
    for bone in obj.data.bones:
        rows.append([bone.name, bone.parent.name if bone.parent else None, bone.use_deform, bone.use_connect,
                     bone.use_inherit_rotation, bone.inherit_scale, bone.use_local_location, bone.bbone_segments,
                     list(bone.head_local), list(bone.tail_local), [list(row) for row in bone.matrix_local]])
    poses = []
    if obj.pose is not None:
        for pose in obj.pose.bones:
            poses.append([pose.name, pose.rotation_mode,
                          [[c.name, c.type, cax_target(c), getattr(c, 'subtarget', None)] for c in pose.constraints]])
    drivers = []
    if obj.animation_data is not None:
        for curve in obj.animation_data.drivers:
            record = m3_driver_record(curve)
            record.pop('valid', None)
            drivers.append(record)
    return {'digest': m2_digest([rows, poses, drivers]), 'bones': len(rows), 'pose_position': obj.data.pose_position,
            'pose_evaluated': obj.pose is not None, 'drivers': len(drivers),
            'root_bones': [bone.name for bone in obj.data.bones if bone.parent is None][:8]}

def cax_slot(action, obj, identifier):
    slots = list(action.slots)
    if identifier:
        for slot in slots:
            if slot.identifier == identifier:
                return slot
        return None
    for slot in slots:
        if slot.identifier == 'OB' + obj.name:
            return slot
    candidates = [slot for slot in slots if slot.target_id_type in ('OBJECT', 'UNSPECIFIED')]
    if len(candidates) == 1:
        return candidates[0]
    return None

def cax_body_requested(obj, names):
    rows = []
    ad = obj.animation_data
    for name in names:
        action = bpy.data.actions.get(name)
        if action is None:
            cm_fail('ACTOR_ACTION_MISSING', name)
        if cax_owner(action) is not None:
            cm_fail('ACTOR_ACTION_NOT_BODY', name + ' is owned by ' + cax_owner(action))
        slot = ad.action_slot if ad is not None and ad.action == action else None
        if slot is None:
            slot = cax_slot(action, obj, None)
        if slot is None:
            cm_fail('ACTOR_ACTION_SLOT_AMBIGUOUS', name + ': no single OBJECT slot for ' + obj.name)
        rows.append([action, slot, 'REQUESTED'])
    return rows

def cax_body_auto(obj):
    # Default body Actions: the active Action and the strips of non-owned NLA tracks. M7 faces, M8 secondary tracks and
    # M10's own tracks are never body Actions.
    rows = []
    seen = []
    ad = obj.animation_data
    if ad is None:
        return rows
    if ad.action is not None and cax_owner(ad.action) is None:
        rows.append([ad.action, ad.action_slot, 'ACTIVE'])
        seen.append(ad.action.name)
    for track in ad.nla_tracks:
        if track.name.startswith('M7_FACE') or track.name.startswith('M8_') or track.name.startswith('M10C_'):
            continue
        for strip in track.strips:
            action = strip.action
            if action is None or action.name in seen or cax_owner(action) is not None:
                continue
            rows.append([action, strip.action_slot, 'NLA'])
            seen.append(action.name)
    return rows[:32]

def cax_curve_map(action, slot):
    curves = {}
    for handle, layer, strip, curve in action_curves(action, slot.handle if slot is not None else None):
        curves[curve.data_path + '|' + str(curve.array_index)] = curve
    return curves

def cax_has(curves, path):
    for index in range(3):
        if (path + '|' + str(index)) in curves:
            return True
    return False

def cax_vector(curves, path, frame):
    values = []
    for index in range(3):
        curve = curves.get(path + '|' + str(index))
        values.append(curve.evaluate(frame) if curve is not None else 0.0)
    return mathutils.Vector(values)

def cax_bone_path(obj, name):
    if obj.pose is not None and obj.pose.bones.get(name) is not None:
        return obj.pose.bones[name].path_from_id('location')
    return 'pose.bones["' + name + '"].location'

def cax_root_motion(obj, action, slot, bones, unit, fps):
    # Raw F-Curves evaluated with fcurve.evaluate(frame): no frame_set, no Action assignment. Object location is in
    # parent space; a root bone translation is mapped by its rest matrix and the current object matrix. Constant
    # channels cancel in the displacement, so missing axes are read as 0.
    curves = cax_curve_map(action, slot)
    start = float(action.frame_range[0])
    end = float(action.frame_range[1])
    root = bones.get('root', {}).get('bone') if obj.type == 'ARMATURE' else None
    bone = obj.data.bones.get(root) if root else None
    path = cax_bone_path(obj, root) if bone is not None else None
    object_animated = cax_has(curves, 'location')
    bone_animated = path is not None and cax_has(curves, path)
    row = {'source': 'RAW_FCURVES_NO_FRAME_SET', 'action_frame_range': [start, end],
           'duration_frames': end - start, 'duration_s': (end - start) / fps if fps > 0 else None,
           'root_bone': root, 'root_bone_source': bones.get('root', {}).get('source') if root else None,
           'channels': {'object_location': object_animated, 'root_bone_location': bone_animated}}
    if not object_animated and not bone_animated:
        row['status'] = 'NO_ROOT_TRANSLATION_CHANNELS'
        return row
    first = int(math.floor(start))
    last = int(math.ceil(end))
    step = max(1, int(math.ceil((last - first + 1) / 2000.0)))
    frames = list(range(first, last + 1, step))
    if frames[-1] != last:
        frames.append(last)
    world3 = obj.matrix_world.to_3x3()
    parent3 = (obj.parent.matrix_world @ obj.matrix_parent_inverse).to_3x3() if obj.parent is not None else mathutils.Matrix.Identity(3)
    rest3 = bone.matrix_local.to_3x3() if bone is not None else mathutils.Matrix.Identity(3)
    points = []
    for frame in frames:
        point = mathutils.Vector((0.0, 0.0, 0.0))
        if object_animated:
            point = point + parent3 @ cax_vector(curves, 'location', frame)
        if bone_animated:
            point = point + world3 @ (rest3 @ cax_vector(curves, path, frame))
        points.append([point.x * unit, point.y * unit, point.z * unit])
    row.update(cax_motion_summary(points, frames, fps))
    row['bone'] = root
    row['sample_step'] = step
    row['status'] = 'MEASURED'
    return row

def cax_bones(obj, explicit, roles, minimum):
    resolved = {}
    low = {}
    status = 'NOT_REQUIRED'
    for role, name in explicit.items():
        if obj.type != 'ARMATURE':
            cm_fail('ACTOR_BONES_REQUIRE_ARMATURE', obj.name + ' is ' + obj.type)
        if obj.data.bones.get(name) is None:
            cm_fail('ACTOR_BONE_MISSING', role + ' -> ' + name + ' not in ' + obj.name)
        resolved[role] = {'bone': name, 'source': 'EXPLICIT', 'confidence': None}
    missing = [role for role in roles if role not in resolved]
    if obj.type == 'ARMATURE' and missing:
        model = None
        if obj.pose is None:
            # Evaluating the depsgraph here would be a side effect of a registry write; report instead.
            status = 'UNAVAILABLE_POSE_NOT_EVALUATED'
        else:
            try:
                model = m3_semantic_model(obj, {})
                status = 'ANALYZED'
            except Exception as exc:
                status = 'FAILED: ' + str(exc)[:200]
        if model is not None:
            by_role = {}
            for row in model['assignments']:
                by_role[row['semantic']] = row
            for role in missing:
                row = by_role.get(roles[role])
                if row is None:
                    continue
                if row['confidence'] >= minimum:
                    resolved[role] = {'bone': row['bone'], 'source': 'M3_SEMANTICS', 'confidence': row['confidence']}
                else:
                    low[role] = {'bone': row['bone'], 'confidence': row['confidence']}
    if obj.type == 'ARMATURE' and 'root' not in resolved:
        tops = [bone.name for bone in obj.data.bones if bone.parent is None]
        if len(tops) == 1:
            resolved['root'] = {'bone': tops[0], 'source': 'HIERARCHY_SINGLE_ROOT', 'confidence': None}
    return resolved, low, status

def cax_reach(obj, name, unit):
    # Rest chain length hand + two parents (the M3 limb definition), scaled by the largest object scale axis. It is an
    # extended-arm estimate from rest data, not a measured animated reach.
    bone = obj.data.bones.get(name)
    chain = []
    length = 0.0
    while bone is not None and len(chain) < 3:
        chain.append(bone.name)
        length += bone.length
        bone = bone.parent
    scale = obj.matrix_world.to_scale()
    factor = max(abs(scale.x), abs(scale.y), abs(scale.z))
    return {'value_m': length * factor * unit, 'chain': chain}

def cax_bounds(obj, meshes, unit):
    # World AABB of rest data: undeformed mesh vertices through each mesh's current object matrix, plus rest bone
    # heads/tails through the armature matrix. No evaluation and no pose change.
    points = []
    for mesh_obj in meshes:
        me = mesh_obj.data
        if len(me.vertices) == 0:
            continue
        co = array('f', [0]) * (len(me.vertices) * 3)
        me.vertices.foreach_get('co', co)
        xs = co[0::3]
        ys = co[1::3]
        zs = co[2::3]
        for x in (min(xs), max(xs)):
            for y in (min(ys), max(ys)):
                for z in (min(zs), max(zs)):
                    points.append(mesh_obj.matrix_world @ mathutils.Vector((x, y, z)))
    if obj.type == 'ARMATURE':
        for bone in obj.data.bones:
            points.append(obj.matrix_world @ bone.head_local)
            points.append(obj.matrix_world @ bone.tail_local)
    if not points:
        return None
    low = [min(point[axis] for point in points) * unit for axis in range(3)]
    high = [max(point[axis] for point in points) * unit for axis in range(3)]
    return {'min_m': low, 'max_m': high, 'dimensions_m': [high[axis] - low[axis] for axis in range(3)],
            'method': 'REST_DATA_AABB_WORLD'}

def cax_facial(obj, names):
    rows = {}
    for name in names:
        action = bpy.data.actions.get(name)
        if action is None:
            rows[name] = {'exists': False}
            continue
        keys = [slot.identifier[2:] for slot in action.slots if slot.target_id_type == 'KEY']
        tracks = []
        owners = [obj] + [bpy.data.shape_keys.get(key) for key in keys]
        for owner in owners:
            if owner is None or owner.animation_data is None:
                continue
            for track in owner.animation_data.nla_tracks:
                if track.name.startswith('M7_FACE'):
                    tracks.append([owner.name, track.name, track.mute])
        rows[name] = {'exists': True, 'digest': cm_action_digest(action), 'keys': keys, 'm7_tracks': tracks,
                      'owner': cax_prop(action.get('production_facial_owner')),
                      'lifecycle': cax_prop(action.get('production_facial_lifecycle'))}
    return rows

def cax_secondary(groups):
    rows = {}
    for group in groups:
        items = []
        missing = []
        for name in group['names']:
            item = bpy.data.objects.get(name)
            if item is None:
                missing.append(name)
                items.append([name, None])
                continue
            mods = []
            for mod in item.modifiers:
                values = [mod.name, mod.type]
                if mod.type == 'MESH_CACHE':
                    values += [mod.cache_format, mod.frame_start, mod.frame_scale, mod.filepath]
                mods.append(values)
            items.append([item.name, item.type, item.data.name if item.data else None, mods,
                          cax_prop(item.get('production_secondary_state')), cax_prop(item.get('production_generation_id'))])
        rows[group['key']] = {'digest': m2_digest(items), 'missing': missing}
    return rows

def cax_world_transform(obj, unit):
    matrix = obj.matrix_world
    return {'location_m': [value * unit for value in matrix.translation], 'rotation_euler': list(matrix.to_euler()),
            'scale': list(matrix.to_scale()), 'matrix_world': [list(row) for row in matrix],
            'delta_location': list(obj.delta_location), 'delta_rotation_euler': list(obj.delta_rotation_euler)}

def cax_register(params):
    scene = cax_scene(params.get('source_scene'))
    obj = scene.objects.get(params['object'])
    if obj is None:
        if bpy.data.objects.get(params['object']) is None:
            cm_fail('ACTOR_OBJECT_MISSING', params['object'])
        cm_fail('ACTOR_NOT_IN_SCENE', params['object'] + ' is not in scene ' + scene.name)
    if obj.type not in ('ARMATURE', 'MESH', 'EMPTY'):
        cm_fail('ACTOR_TYPE_UNSUPPORTED', obj.name + ' is ' + obj.type + '; actors are ARMATURE, MESH or EMPTY')
    if cax_owner(obj) is not None:
        cm_fail('ACTOR_OWNED_OBJECT', obj.name + ' belongs to ' + cax_owner(obj) + '; register the source object')
    unit = scene.unit_settings.scale_length
    fps = scene.render.fps / scene.render.fps_base
    names = cax_bound_meshes(obj, scene)
    if len(names) > params['max_meshes']:
        cm_fail('ACTOR_CAPACITY', str(len(names)) + ' bound meshes; at most ' + str(params['max_meshes']))
    meshes = [bpy.data.objects[name] for name in names]
    vertices = sum(len(item.data.vertices) for item in meshes)
    if vertices > params['max_vertices']:
        cm_fail('ACTOR_CAPACITY', str(vertices) + ' vertices; at most ' + str(params['max_vertices']))
    mesh_rows = [cax_mesh(item) for item in meshes]
    rig = cax_rig(obj) if obj.type == 'ARMATURE' else None
    bones, low, semantic_status = cax_bones(obj, params['bones'], params['semantic_roles'], params['confidence_min'])
    if params['body_actions'] is None:
        body_rows = cax_body_auto(obj)
    else:
        body_rows = cax_body_requested(obj, params['body_actions'])
    body = []
    for action, slot, origin in body_rows:
        body.append({'action': action.name, 'slot': slot.identifier if slot is not None else None, 'origin': origin,
                     'frame_range': [float(action.frame_range[0]), float(action.frame_range[1])],
                     'digest': cm_action_digest(action),
                     'root_motion': cax_root_motion(obj, action, slot, bones, unit, fps)})
    reach = {}
    for side in ('hand_left', 'hand_right'):
        if side in bones:
            reach[side] = dict(cax_reach(obj, bones[side]['bone'], unit), source=bones[side]['source'])
    ad = obj.animation_data
    limitations = []
    if obj.mode != 'OBJECT':
        limitations.append('MODE_' + obj.mode + ': edit-mode changes are not in the mesh data until Object mode')
    if rig is not None and rig['pose_position'] != 'POSE':
        limitations.append('POSE_POSITION_REST: the armature evaluates its rest pose; animation QA would refuse it')
    if rig is not None and not rig['pose_evaluated']:
        limitations.append('POSE_NOT_EVALUATED: pose-bone constraints are not part of the rig digest')
    return {'scene': scene.name, 'fps': fps, 'unit_scale_length': unit, 'object': obj.name,
            'data': obj.data.name if obj.data else None, 'type': obj.type, 'mode': obj.mode,
            'parent': obj.parent.name if obj.parent else None, 'parent_type': obj.parent_type,
            'parent_bone': obj.parent_bone or None, 'constraints': [[c.name, c.type] for c in obj.constraints],
            'children': sorted(child.name for child in obj.children)[:64], 'mesh_names': names,
            'meshes': [{'object': row['object'], 'data': row['data'], 'key': row['key'], 'counts': row['counts'],
                        'shape_key_names': row['shape_key_names']} for row in mesh_rows],
            'mesh_sections': {row['object']: row['sections'] for row in mesh_rows},
            'rig': rig, 'asset_fingerprint': m2_digest([obj.type, rig['digest'] if rig else None,
                                                        [[row['object'], row['data'], row['sections']] for row in mesh_rows]]),
            'bones': bones, 'low_confidence': low, 'semantic_status': semantic_status,
            'body_actions': body, 'active_action': ad.action.name if ad is not None and ad.action is not None else None,
            'active_slot': ad.action_slot.identifier if ad is not None and ad.action_slot is not None else None,
            'reach': reach, 'bounds': cax_bounds(obj, meshes, unit), 'world_transform': cax_world_transform(obj, unit),
            'facial': cax_facial(obj, params['facial_actions']), 'secondary': cax_secondary(params['secondary']),
            'limitations': limitations}

def cax_actor_digests(spec, scene):
    obj = bpy.data.objects.get(spec['object'])
    if obj is None:
        return {'exists': False}
    meshes = {}
    for name in spec['meshes']:
        item = bpy.data.objects.get(name)
        meshes[name] = cax_mesh(item)['sections'] if item is not None and item.type == 'MESH' else None
    body = {}
    for name, identifier in spec['body_actions']:
        action = bpy.data.actions.get(name)
        if action is None:
            body[name] = None
            continue
        body[name] = {'digest': cm_action_digest(action),
                      'slot': identifier is None or cax_slot(action, obj, identifier) is not None}
    ad = obj.animation_data
    return {'exists': True, 'type': obj.type, 'data': obj.data.name if obj.data else None, 'mode': obj.mode,
            'in_scene': scene is not None and scene.objects.get(obj.name) is not None,
            'rig': cax_rig(obj)['digest'] if obj.type == 'ARMATURE' else None,
            'mesh_set': cax_bound_meshes(obj, scene), 'meshes': meshes, 'body_actions': body,
            'facial': cax_facial(obj, spec['facial_actions']), 'secondary': cax_secondary(spec['secondary']),
            'matrix_world': [list(row) for row in obj.matrix_world],
            'active_action': ad.action.name if ad is not None and ad.action is not None else None}

def cax_cameras_lights(scene):
    # One formula for the plan baseline (lifecycle.SOURCE_INFO) and the later measurement: COMMON's helper.
    result = cm_cameras_lights(scene)
    if result['count'] > 64:
        result['objects'] = None
    return result

def cax_integrity(params):
    source = bpy.data.scenes.get(params['source_scene']) if params.get('source_scene') else bpy.context.scene
    result = {'source_scene_exists': source is not None, 'source': None, 'candidate': None,
              'candidate_status': 'NOT_APPLICABLE', 'cameras_lights': None, 'actors': {}}
    if params['scene_checks'] and source is not None:
        result['source'] = cm_source_fingerprint(source)['fingerprint']
        result['cameras_lights'] = cax_cameras_lights(source)
    if params['scene_checks'] and params.get('scene'):
        scene = bpy.data.scenes.get(params['scene'])
        collections = params.get('collections') or {}
        src = bpy.data.collections.get(collections.get('src') or '')
        cin = bpy.data.collections.get(collections.get('cin') or '')
        if scene is None or not cm_is_owned(scene, params):
            result['candidate_status'] = 'MISSING'
        elif src is None or cin is None:
            result['candidate_status'] = 'COLLECTIONS_MISSING'
        else:
            result['candidate'] = cm_candidate_fingerprint(scene, src, cin)['fingerprint']
            result['candidate_status'] = 'MEASURED'
    for spec in params['actors']:
        result['actors'][spec['actor_id']] = cax_actor_digests(spec, source)
    return result

def cax_page(rows, params):
    return rows[params['offset']:params['offset'] + params['limit']]

def cax_marker_key(marker):
    return (marker.frame, marker.name)

def cax_location(obj, unit):
    return [value * unit for value in obj.matrix_world.translation]

def cax_world_row(world):
    if world is None:
        return None
    row = {'name': world.name, 'use_nodes': world.use_nodes, 'background': None, 'owner': cax_owner(world)}
    if world.use_nodes and world.node_tree is not None:
        for node in world.node_tree.nodes:
            if node.type == 'BACKGROUND':
                row['background'] = {'color': list(node.inputs['Color'].default_value),
                                     'strength': node.inputs['Strength'].default_value}
                break
    return row

def cax_scene_row(scene, unit, count):
    render = scene.render
    view = scene.view_settings
    return {'name': scene.name, 'fps': render.fps, 'fps_base': render.fps_base, 'frame_rate': render.fps / render.fps_base,
            'frame_start': scene.frame_start, 'frame_end': scene.frame_end, 'frame_current': scene.frame_current,
            'units': {'system': scene.unit_settings.system, 'scale_length': unit,
                      'length_unit': scene.unit_settings.length_unit},
            'render': {'engine': render.engine, 'resolution': [render.resolution_x, render.resolution_y,
                                                               render.resolution_percentage],
                       'pixel_aspect': [render.pixel_aspect_x, render.pixel_aspect_y]},
            'view': {'transform': view.view_transform, 'look': view.look, 'exposure': view.exposure, 'gamma': view.gamma},
            'camera': scene.camera.name if scene.camera else None, 'world': cax_world_row(scene.world),
            'objects': count, 'markers': len(scene.timeline_markers), 'owner': cax_owner(scene),
            'm10_candidate_scenes': sorted(item.name for item in bpy.data.scenes if item.get('production_m10_owner'))[:16]}

def cax_camera_row(obj, unit, full):
    data = obj.data
    dof = data.dof
    row = {'name': obj.name, 'data': data.name, 'type': data.type, 'lens': data.lens, 'lens_unit': data.lens_unit,
           'sensor': [data.sensor_width, data.sensor_height, data.sensor_fit], 'clip': [data.clip_start, data.clip_end],
           'dof': {'use_dof': dof.use_dof, 'focus_object': dof.focus_object.name if dof.focus_object else None,
                   'focus_subtarget': dof.focus_subtarget, 'focus_distance': dof.focus_distance,
                   'aperture_fstop': dof.aperture_fstop},
           'location_m': cax_location(obj, unit), 'animated': cax_animated(obj) or cax_animated(data),
           'owner': cax_owner(obj)}
    if full:
        row.update(shift=[data.shift_x, data.shift_y], ortho_scale=data.ortho_scale, angle=data.angle,
                   aperture_blades=dof.aperture_blades, rotation_euler=list(obj.matrix_world.to_euler()))
    return row

def cax_light_row(obj, unit, full):
    data = obj.data
    row = {'name': obj.name, 'data': data.name, 'type': data.type, 'energy': data.energy, 'color': list(data.color),
           'use_shadow': data.use_shadow, 'shadow_soft_size': data.shadow_soft_size,
           'location_m': cax_location(obj, unit), 'animated': cax_animated(obj) or cax_animated(data),
           'owner': cax_owner(obj)}
    if data.type == 'SPOT':
        row['spot'] = [data.spot_size, data.spot_blend]
    elif data.type == 'AREA':
        row['area'] = [data.shape, data.size, data.size_y]
    elif data.type == 'SUN':
        row['angle'] = data.angle
    if full:
        row.update(rotation_euler=list(obj.matrix_world.to_euler()), temperature=[data.use_temperature, data.temperature],
                   exposure=data.exposure)
    return row

def cax_root_channels(obj):
    ad = obj.animation_data
    if ad is None or ad.action is None:
        return None
    try:
        curves = cax_curve_map(ad.action, ad.action_slot)
    except Exception as exc:
        return {'error': str(exc)[:160]}
    roots = [bone.name for bone in obj.data.bones if bone.parent is None]
    return {'object_location': cax_has(curves, 'location'),
            'root_bone_location': [name for name in roots if cax_has(curves, cax_bone_path(obj, name))][:4]}

def cax_used_actions(scene):
    used = []
    for obj in scene.objects:
        owners = [obj]
        if obj.data is not None:
            owners.append(obj.data)
        if obj.type == 'MESH' and obj.data.shape_keys is not None:
            owners.append(obj.data.shape_keys)
        for owner in owners:
            ad = getattr(owner, 'animation_data', None)
            if ad is None:
                continue
            if ad.action is not None:
                used.append(ad.action.name)
            for track in ad.nla_tracks:
                for strip in track.strips:
                    if strip.action is not None:
                        used.append(strip.action.name)
    return set(used)

def cax_action_row(action, used, full):
    row = {'name': action.name, 'frame_range': [float(action.frame_range[0]), float(action.frame_range[1])],
           'users': action.users, 'fake_user': action.use_fake_user, 'slot_count': len(action.slots),
           'slots': [[slot.identifier, slot.target_id_type] for slot in action.slots][:8],
           'used_in_scene': action.name in used, 'owner': cax_owner(action),
           'lifecycle': cax_prop(action.get('production_facial_lifecycle'))}
    try:
        rows = action_curves(action, None)
    except Exception as exc:
        row['curves'] = None
        row['error'] = str(exc)[:160]
        return row
    bones = []
    object_transform = False
    for handle, layer, strip, curve in rows:
        path = curve.data_path
        if path in ('location', 'rotation_euler', 'rotation_quaternion', 'rotation_axis_angle', 'scale'):
            object_transform = True
        elif path.startswith('pose.bones["') and path.endswith('"].location'):
            name = path[12:-11]
            if name not in bones:
                bones.append(name)
    row.update(curves=len(rows), animates_object_transform=object_transform, bones_with_location=len(bones),
               bone_location_sample=bones[:6])
    if full:
        row['layers'] = [layer.name for layer in action.layers]
        row['custom'] = {key: cax_prop(action.get(key)) for key in list(action.keys())[:16]}
    return row

def cax_actor_candidates(objects, unit):
    bound = {}
    for obj in objects:
        if obj.type == 'MESH':
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object is not None:
                    bound.setdefault(mod.object.name, []).append(obj.name)
    deformed = []
    for names in bound.values():
        deformed += names
    rows = []
    for obj in objects:
        owner = cax_owner(obj)
        if owner in ('M8', 'M10'):
            continue
        ad = obj.animation_data
        if obj.type == 'ARMATURE':
            meshes = sorted(set([child.name for child in obj.children if child.type == 'MESH'] + bound.get(obj.name, [])))
            rows.append({'name': obj.name, 'kind': 'ARMATURE', 'data': obj.data.name, 'bones': len(obj.data.bones),
                         'root_bones': [bone.name for bone in obj.data.bones if bone.parent is None][:4],
                         'bound_meshes': meshes[:8], 'bound_mesh_count': len(meshes),
                         'active_action': ad.action.name if ad is not None and ad.action is not None else None,
                         'active_slot': ad.action_slot.identifier if ad is not None and ad.action_slot is not None else None,
                         'root_channels': cax_root_channels(obj),
                         'nla_tracks': [track.name for track in ad.nla_tracks][:8] if ad is not None else [],
                         'nla_track_count': len(ad.nla_tracks) if ad is not None else 0,
                         'parent': obj.parent.name if obj.parent else None, 'constraints': len(obj.constraints),
                         'pose_position': obj.data.pose_position, 'location_m': cax_location(obj, unit)})
        elif obj.type == 'MESH':
            if obj.name in deformed or (obj.parent is not None and obj.parent.type == 'ARMATURE'):
                continue
            cache = any(mod.type == 'MESH_CACHE' for mod in obj.modifiers)
            animated = cax_animated(obj)
            shapes = cax_shape_animated(obj)
            if not (animated or shapes or cache):
                continue
            rows.append({'name': obj.name, 'kind': 'ANIMATED_MESH', 'data': obj.data.name,
                         'vertices': len(obj.data.vertices), 'object_animation': animated,
                         'shape_key_animation': shapes, 'mesh_cache': cache,
                         'parent': obj.parent.name if obj.parent else None, 'owner': owner})
        elif obj.type == 'EMPTY':
            rows.append({'name': obj.name, 'kind': 'EMPTY', 'children': len(obj.children), 'animated': cax_animated(obj),
                         'parent': obj.parent.name if obj.parent else None, 'display': obj.empty_display_type,
                         'location_m': cax_location(obj, unit)})
    return rows

def cax_m7_rows(objects, used):
    rows = []
    for action in sorted(bpy.data.actions, key=cm_sort_name):
        if action.get('production_facial_owner') == 'M7':
            rows.append({'kind': 'ACTION', 'name': action.name,
                         'lifecycle': cax_prop(action.get('production_facial_lifecycle')),
                         'generation': cax_prop(action.get('production_facial_generation')),
                         'version': cax_prop(action.get('production_facial_version')),
                         'qa_result': cax_prop(action.get('production_facial_qa_result')),
                         'used_in_scene': action.name in used})
    for obj in objects:
        owners = [[obj, 'OBJECT']]
        if obj.type == 'MESH' and obj.data.shape_keys is not None:
            owners.append([obj.data.shape_keys, 'KEY'])
        for owner, kind in owners:
            ad = owner.animation_data
            if ad is None:
                continue
            for track in ad.nla_tracks:
                if track.name.startswith('M7_FACE'):
                    rows.append({'kind': 'TRACK', 'object': obj.name, 'owner': owner.name, 'owner_type': kind,
                                 'track': track.name, 'mute': track.mute,
                                 'strips': [[strip.name, strip.action.name if strip.action else None, strip.frame_start,
                                             strip.frame_end, strip.extrapolation] for strip in track.strips][:4]})
    return rows

def cax_m8_rows(scene):
    rows = []
    for obj in sorted(bpy.data.objects, key=cm_sort_name):
        if obj.get('production_m8_owned'):
            rows.append({'kind': 'OBJECT', 'name': obj.name, 'type': obj.type,
                         'in_scene': scene.objects.get(obj.name) is not None,
                         'generation': cax_prop(obj.get('production_generation_id')),
                         'source': cax_prop(obj.get('production_source_object')),
                         'subsystem': cax_prop(obj.get('production_subsystem')),
                         'secondary_state': cax_prop(obj.get('production_secondary_state')),
                         'bake_of': cax_prop(obj.get('production_m8_bake_of')), 'hide_render': obj.hide_render})
        for mod in obj.modifiers:
            if mod.name.startswith('M8_'):
                row = {'kind': 'MODIFIER', 'object': obj.name, 'modifier': mod.name, 'type': mod.type}
                if mod.type == 'MESH_CACHE':
                    row.update(frame_start=mod.frame_start, frame_scale=mod.frame_scale, cache_format=mod.cache_format,
                               play_mode=mod.play_mode, time_mode=mod.time_mode)
                rows.append(row)
        ad = obj.animation_data
        if ad is not None:
            for track in ad.nla_tracks:
                if track.name.startswith('M8_SECONDARY'):
                    rows.append({'kind': 'TRACK', 'object': obj.name, 'track': track.name, 'mute': track.mute})
    return rows

def cax_prop_rows(objects, unit):
    deform = ('ARMATURE', 'MESH_CACHE', 'MESH_SEQUENCE_CACHE', 'LATTICE', 'CURVE', 'HOOK', 'SURFACE_DEFORM',
              'MESH_DEFORM', 'CLOTH', 'SOFT_BODY', 'SHRINKWRAP', 'SIMPLE_DEFORM', 'WARP', 'WAVE', 'CAST', 'SMOOTH',
              'CORRECTIVE_SMOOTH', 'LAPLACIANSMOOTH', 'LAPLACIANDEFORM', 'DISPLACE', 'DATA_TRANSFER')
    rows = []
    for obj in objects:
        if obj.type != 'MESH' or cax_owner(obj) is not None or cax_animated(obj) or cax_shape_animated(obj):
            continue
        if any(mod.type in deform for mod in obj.modifiers):
            continue
        rows.append({'name': obj.name, 'data': obj.data.name, 'vertices': len(obj.data.vertices),
                     'polygons': len(obj.data.polygons), 'dimensions_m': [value * unit for value in obj.dimensions],
                     'location_m': cax_location(obj, unit), 'parent': obj.parent.name if obj.parent else None,
                     'parent_type': obj.parent_type if obj.parent else None, 'parent_bone': obj.parent_bone or None,
                     'material_slots': len(obj.material_slots), 'hide_render': obj.hide_render})
    return rows

def cax_inspect_scene(params):
    scene = cax_scene(params.get('source_scene'))
    unit = scene.unit_settings.scale_length
    full = params['detail'] == 'full'
    wanted = params['sections']
    objects = sorted(scene.objects, key=cm_sort_name)
    used = cax_used_actions(scene)
    sections = {}
    totals = {}
    if 'markers' in wanted:
        rows = [{'name': marker.name, 'frame': marker.frame, 'camera': marker.camera.name if marker.camera else None}
                for marker in sorted(scene.timeline_markers, key=cax_marker_key)]
        totals['markers'] = len(rows)
        sections['markers'] = cax_page(rows, params)
    if 'collections' in wanted:
        rows = [{'name': item.name, 'objects': len(item.objects), 'children': len(item.children),
                 'hide_render': item.hide_render, 'owner': cax_owner(item)} for item in scene.collection.children_recursive]
        totals['collections'] = len(rows)
        sections['collections'] = cax_page(rows, params)
    if 'cameras' in wanted:
        rows = [obj for obj in objects if obj.type == 'CAMERA']
        totals['cameras'] = len(rows)
        sections['cameras'] = [cax_camera_row(obj, unit, full) for obj in cax_page(rows, params)]
    if 'lights' in wanted:
        rows = [obj for obj in objects if obj.type == 'LIGHT']
        totals['lights'] = len(rows)
        sections['lights'] = [cax_light_row(obj, unit, full) for obj in cax_page(rows, params)]
    if 'actor_candidates' in wanted:
        rows = cax_actor_candidates(objects, unit)
        totals['actor_candidates'] = len(rows)
        sections['actor_candidates'] = cax_page(rows, params)
    if 'actions' in wanted:
        rows = sorted(bpy.data.actions, key=cm_sort_name)
        totals['actions'] = len(rows)
        sections['actions'] = [cax_action_row(action, used, full) for action in cax_page(rows, params)]
    if 'facial_m7' in wanted:
        rows = cax_m7_rows(objects, used)
        totals['facial_m7'] = len(rows)
        sections['facial_m7'] = cax_page(rows, params)
    if 'secondary_m8' in wanted:
        rows = cax_m8_rows(scene)
        totals['secondary_m8'] = len(rows)
        sections['secondary_m8'] = cax_page(rows, params)
    if 'props' in wanted:
        rows = cax_prop_rows(objects, unit)
        totals['props'] = len(rows)
        sections['props'] = cax_page(rows, params)
    return {'schema': params['schema'], 'scene': cax_scene_row(scene, unit, len(objects)), 'sections': sections,
            'pagination': {'limit': params['limit'], 'offset': params['offset'], 'totals': totals,
                           'more': sorted(name for name in totals if totals[name] > params['offset'] + params['limit'])}}
''' + inspect.getsource(cax_motion_summary)

SCENE_BODY = COMMON + ACTORS_RUNTIME + r'''
result = cax_inspect_scene(params)
'''

REGISTER_BODY = COMMON + SEMANTIC_RUNTIME + ACTORS_RUNTIME + r'''
result = cax_register(params)
'''

DIGEST_BODY = COMMON + ACTORS_RUNTIME + r'''
result = cax_integrity(params)
'''

SCENE_NAME_BODY = r'''
import bpy
result = {'scene': bpy.context.scene.name}
'''

# Capacity of one registration payload. Proposals sized so the rest-data digests stay well inside the 180 s socket
# timeout; not measured on production characters.
MAX_MESHES = 32
MAX_VERTICES = 2_000_000


# ------------------------------------------------------------------ host-side validation helpers

def _arguments(req, allowed):
    given = {'cinematic': req.cinematic, 'actor': req.actor, 'object_name': req.object_name, 'brief': req.brief}
    extra = sorted(key for key, value in given.items() if value is not None and key not in allowed)
    if extra:
        raise ValueError('CINEMATIC_ARGUMENT_UNSUPPORTED: ' + ', '.join(extra) + ' not accepted by this tool')


def _read_only(req):
    if req.apply:
        raise ValueError('CINEMATIC_APPLY_UNSUPPORTED: read-only tool; it never changes Blender or the ledger')


def _blender_name(value, field):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 63 or any(ord(c) < 32 for c in value):
        raise ValueError('CINEMATIC_NAME_INVALID: ' + field + ' must be a Blender name (1-63 bytes)')
    return value


def _strings(value, field, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError('CINEMATIC_LIST_INVALID: ' + field + ' must be a list of at most ' + str(maximum) + ' strings')
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 1024:
            raise ValueError('CINEMATIC_LIST_INVALID: ' + field + ' items must be non-empty strings')
    if len(set(value)) != len(value):
        raise ValueError('CINEMATIC_LIST_INVALID: ' + field + ' has duplicates')
    return list(value)


def _bones(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('ACTOR_BONES_INVALID: bones must map ' + '/'.join(BONE_ROLES) + ' to bone names')
    unknown = sorted(set(value) - set(BONE_ROLES))
    if unknown:
        raise ValueError('ACTOR_BONE_ROLE_UNSUPPORTED: ' + ', '.join(map(str, unknown)) + '; use ' + ', '.join(BONE_ROLES))
    return {role: _blender_name(name, 'bones.' + role) for role, name in value.items()}


def _actor_id(value):
    if not isinstance(value, str) or not _ACTOR_ID.match(value):
        raise ValueError('ACTOR_ID_INVALID: pass the 16-hex actor_id returned by cinematic_register_actor')
    return value


def short_digest(value):
    """Digests are ~300 characters; reports carry a short label. A prefix of the m521 residue is NOT safe: a small
    payload edit only moves low bits of the residue, so two different digests shared their first 16 hex digits in
    the probe (measured). The label is therefore the byte length plus a sha256 of the whole digest."""
    if isinstance(value, str) and value.startswith('m521:'):
        return 'm521:' + value.split(':')[1] + '#' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
    if isinstance(value, str) and len(value) > 40:
        return 'sha#' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
    return value


# ------------------------------------------------------------------ M7 / M8 references (read-only ledgers)

def _load_facial(storage, reference):
    from ..facial import service as facial_service
    try:
        path, plan, state = facial_service.load(storage, reference)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ValueError('ACTOR_FACIAL_REFERENCE_INVALID: ' + str(exc)[:300]) from exc
    brief = plan.get('brief') or {}
    profile = plan.get('profile') or {}
    return {'ref': str(path), 'generation_id': plan.get('generation_id'), 'state': state.get('state'),
            'object': plan.get('object_name'), 'armature': profile.get('armature'),
            'candidate_action': state.get('candidate_action'), 'candidate_fingerprint': state.get('candidate_fingerprint'),
            'accepted_tracks': state.get('accepted_tracks') or [], 'fps': brief.get('fps'),
            'frame_range': [brief.get('frame_start'), brief.get('frame_end')], 'head_policy': plan.get('head_policy')}


def _names(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [value[key] for key in ('candidate_object', 'candidate_rig') if isinstance(value.get(key), str)]
    return []


def _load_secondary(storage, reference):
    from ..physics import service as physics_service
    try:
        path, plan, state = physics_service.load(storage, reference)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ValueError('ACTOR_SECONDARY_REFERENCE_INVALID: ' + str(exc)[:300]) from exc
    params = plan.get('params') or {}
    related = {plan.get('object_name'), params.get('source_object'), params.get('anchor_object'),
               (params.get('chain') or {}).get('armature')} | set(params.get('primary_objects') or [])
    objects = []
    for name in _names(state.get('candidate')) + _names(state.get('baked_candidate')):
        if name not in objects:
            objects.append(name)
    cache = state.get('cache') or {}
    brief = plan.get('brief') or {}
    return {'ref': str(path), 'generation_id': plan.get('generation_id'), 'state': state.get('state'),
            'object': plan.get('object_name'), 'subsystem': brief.get('subsystem'),
            'related_objects': sorted(name for name in related if isinstance(name, str)), 'objects': objects,
            'cache': {'representation': cache.get('representation'), 'sha256': cache.get('sha256')} if cache else None,
            'bake_deviation_passed': (state.get('bake_deviation') or {}).get('passed'),
            'fps': brief.get('fps'), 'frame_range': [brief.get('frame_start'), brief.get('frame_end')]}


def _match_facial(row, snap, warnings):
    related = {snap['object']} | set(snap['mesh_names'])
    if row['armature'] != snap['object'] and row['object'] not in related:
        raise ValueError('ACTOR_FACIAL_OBJECT_MISMATCH: M7 generation ' + str(row['generation_id']) + ' animates '
                         + str(row['object']) + ' (armature ' + str(row['armature']) + '), not actor ' + snap['object'])
    observed = snap['facial'].get(row['candidate_action']) if row['candidate_action'] else None
    row['action_digest'] = observed.get('digest') if observed and observed.get('exists') else None
    row['m7_tracks'] = observed.get('m7_tracks') if observed else []
    row['fingerprint_matches_ledger'] = (row['action_digest'] is not None
                                         and row['action_digest'] == row['candidate_fingerprint'])
    if row['state'] != 'ACCEPTED':
        warnings.append({'code': 'FACIAL_NOT_ACCEPTED', 'ref': row['ref'], 'state': row['state'],
                         'message': 'recorded as-is; cinematic_integrate_facial requires an ACCEPTED M7 generation'})
    if row['candidate_action'] and row['action_digest'] is None:
        warnings.append({'code': 'FACIAL_ACTION_MISSING', 'ref': row['ref'], 'action': row['candidate_action']})
    elif row['action_digest'] is not None and not row['fingerprint_matches_ledger']:
        warnings.append({'code': 'FACIAL_ACTION_CHANGED', 'ref': row['ref'], 'action': row['candidate_action'],
                         'message': 'the M7 Action no longer matches the fingerprint in its ledger'})


def _match_secondary(row, snap, warnings):
    related = {snap['object']} | set(snap['mesh_names']) | set(snap['children'])
    if not (related & set(row['related_objects'])):
        raise ValueError('ACTOR_SECONDARY_OBJECT_MISMATCH: M8 generation ' + str(row['generation_id']) + ' belongs to '
                         + ', '.join(row['related_objects']) + ', not actor ' + snap['object'])
    observed = snap['secondary'].get(row['key']) or {}
    row['objects_digest'] = observed.get('digest')
    row['objects_missing'] = observed.get('missing') or []
    if row['state'] != 'ACCEPTED':
        warnings.append({'code': 'SECONDARY_NOT_ACCEPTED', 'ref': row['ref'], 'state': row['state'],
                         'message': 'recorded as-is; cinematic_integrate_secondary requires an ACCEPTED M8 generation'})
    if row['objects_missing']:
        warnings.append({'code': 'SECONDARY_OBJECT_MISSING', 'ref': row['ref'], 'objects': row['objects_missing']})


# ------------------------------------------------------------------ record construction

def _capabilities(snap, facial, secondary):
    bones = {role: snap['bones'].get(role) or 'UNKNOWN' for role in BONE_ROLES}
    reach_values = [row['value_m'] for row in snap['reach'].values()]
    moving = [row['action'] for row in snap['body_actions']
              if (row['root_motion'] or {}).get('horizontal_distance_m', 0.0) > 0.0]
    return {'bones': bones, 'low_confidence': snap['low_confidence'], 'semantic_status': snap['semantic_status'],
            'semantic_confidence_min': SEMANTIC_CONFIDENCE_MIN, 'threshold_source': 'PROPOSAL',
            'reach_m': max(reach_values) if reach_values else 'UNKNOWN', 'reach': snap['reach'],
            'reach_method': 'sum of rest bone lengths hand + 2 parents x largest object scale axis x unit scale '
                            '(extended-arm estimate from rest data; UNKNOWN without explicit hands or semantics >= 0.9)',
            'can_hold_props': bool(snap['reach']),
            'facial_performance': any(row['state'] == 'ACCEPTED' for row in facial),
            'secondary_motion': any(row['state'] == 'ACCEPTED' for row in secondary),
            'body_actions_with_horizontal_motion': moving,
            'staging': {'parent': snap['parent'], 'parent_type': snap['parent_type'], 'parent_bone': snap['parent_bone'],
                        'constraints': snap['constraints']}}


def build_record(snap, role, asset, key, facial, secondary):
    """Pure: the immutable registry record for one snapshot. `fingerprint` covers everything the record asserts, so a
    re-registration with identical inputs is idempotent and any change produces a new record."""
    bones = {name: row['bone'] for name, row in sorted(snap['bones'].items())}
    body = [{'action': row['action'], 'slot': row['slot'], 'origin': row['origin'], 'frame_range': row['frame_range'],
             'digest': row['digest'], 'root_motion': row['root_motion']} for row in snap['body_actions']]
    identity = {'asset': snap['asset_fingerprint'], 'role': role, 'bones': bones,
                'body_actions': [[row['action'], row['slot'], row['digest']] for row in body],
                'facial': [[row['ref'], row['state'], row['candidate_action'], row.get('action_digest')] for row in facial],
                'secondary': [[row['ref'], row['state'], row.get('objects_digest')] for row in secondary]}
    integrity = {'object': {'name': snap['object'], 'type': snap['type'], 'data': snap['data']},
                 'rig': snap['rig']['digest'] if snap['rig'] else None, 'mesh_set': list(snap['mesh_names']),
                 'meshes': snap['mesh_sections'],
                 'body_actions': {row['action']: {'digest': row['digest'], 'slot': row['slot']} for row in body},
                 'facial_actions': {row['candidate_action']: row.get('action_digest') for row in facial
                                    if row['candidate_action']},
                 'secondary': {row['key']: {'names': row['objects'], 'digest': row.get('objects_digest')}
                               for row in secondary}}
    limitations = list(snap['limitations']) + [
        'root_motion comes from raw F-Curves (fcurve.evaluate) without frame_set: object rotation animation, '
        'constraints, drivers and parent-bone animation are not composed into it',
        'dimensions come from rest mesh data through current object matrices; bone-parented meshes use the current '
        'pose of their parent bone',
        'integrity digests cover rest data and Action content, not evaluated deformation or NLA composition']
    bounds = snap['bounds']
    return {'schema': ACTOR_SCHEMA, 'object': snap['object'], 'data': snap['data'], 'type': snap['type'], 'role': role,
            'asset_role': asset.get('role'), 'asset_id': asset.get('asset_id'), 'scene': snap['scene'],
            'meshes': list(snap['mesh_names']), 'mesh_details': snap['meshes'], 'body_actions': body,
            'active_action': snap['active_action'], 'active_slot': snap['active_slot'],
            'facial': facial, 'secondary': secondary, 'world_transform': snap['world_transform'],
            'dimensions_m': bounds['dimensions_m'] if bounds else 'UNKNOWN', 'bounds_m': bounds,
            'interaction_capabilities': _capabilities(snap, facial, secondary), 'bones': bones,
            'fps': snap['fps'], 'unit_scale_length': snap['unit_scale_length'],
            'asset_fingerprint': snap['asset_fingerprint'], 'identity_sections': {k: fingerprint(v) for k, v in identity.items()},
            'fingerprint': fingerprint(identity), 'integrity': integrity, 'limitations': limitations,
            'registered_at': datetime.now(timezone.utc).isoformat(), 'blender_changed': False}


def _record_changes(previous, record):
    """Which registered sections differ between two records of the same object (for ACTOR_FINGERPRINT_CHANGED)."""
    changed = [name for name, value in record['identity_sections'].items()
               if (previous.get('identity_sections') or {}).get(name) != value]
    if 'asset' in changed:
        before = previous.get('integrity') or {}
        after = record['integrity']
        if before.get('rig') != after.get('rig'):
            changed.append('rig')
        if before.get('mesh_set') != after.get('mesh_set'):
            changed.append('mesh_set')
        for mesh, sections in after.get('meshes', {}).items():
            old = (before.get('meshes') or {}).get(mesh) or {}
            changed += ['meshes:' + mesh + ':' + name for name, value in sections.items() if old.get(name) != value]
    return changed


def public_record(record, detail='summary'):
    """Compact view: the ~300-character digests stay on disk unless detail='full' asks for them (shortened)."""
    hidden = {'integrity', 'identity_sections', 'mesh_details'}
    result = {key: value for key, value in record.items() if key not in hidden}
    result['mesh_details'] = [{'object': row['object'], 'counts': row['counts']} for row in record.get('mesh_details') or []]
    if detail == 'full':
        integrity = record.get('integrity') or {}
        result['integrity'] = {'rig': short_digest(integrity.get('rig')),
                               'meshes': {name: {k: short_digest(v) for k, v in sections.items()}
                                          for name, sections in (integrity.get('meshes') or {}).items()},
                               'body_actions': {name: short_digest(row['digest'])
                                                for name, row in (integrity.get('body_actions') or {}).items()}}
        result['identity_sections'] = {k: v[:16] for k, v in (record.get('identity_sections') or {}).items()}
    return result


# ------------------------------------------------------------------ registry access for other subsystems

def _registry_key(ctx, key=None, scene=None):
    if key is not None:
        return key
    asset = current_asset(ctx.bridge, ctx.storage)
    if asset.get('asset_id'):
        return registry.asset_key(asset, scene or '')
    if scene is None:
        scene = ctx.bridge.run(SCENE_NAME_BODY, {})['scene']
    return registry.asset_key(asset, scene)


def actor_record(ctx, actor_id, key=None, scene=None):
    """Registry record of one actor (read-only). Without `key`, the key of the open asset is derived exactly as
    lifecycle.build_plan does (asset_id, or TEMPORARY-<scene hash> for unregistered sessions)."""
    _actor_id(actor_id)
    return registry.find(ctx.storage, _registry_key(ctx, key, scene), actor_id)


def actor_spec(record):
    """What the digest payload re-measures for one registered actor."""
    integrity = record.get('integrity') or {}
    return {'actor_id': record['actor_id'], 'object': record['object'],
            'meshes': sorted((integrity.get('meshes') or {}).keys()),
            'body_actions': [[name, row.get('slot')] for name, row in sorted((integrity.get('body_actions') or {}).items())],
            'facial_actions': sorted((integrity.get('facial_actions') or {}).keys()),
            'secondary': [{'key': key, 'names': row.get('names') or []}
                          for key, row in sorted((integrity.get('secondary') or {}).items())]}


def _ledger_rows(storage, record):
    """Re-read the M7/M8 ledgers the record points to: a ledger that moved on (or no longer loads) makes the record
    stale even if the Blender data is untouched."""
    rows = []
    for row in record.get('facial') or []:
        item = 'facial_ledger:' + str(row.get('generation_id'))
        try:
            now = _load_facial(storage, row['ref'])
        except ValueError as exc:
            rows.append((item, 'CHANGED', 'FACIAL_LEDGER_INVALID', row.get('state'), str(exc)[:200]))
            continue
        same = (now['state'], now['candidate_action'], now['candidate_fingerprint']) == \
               (row.get('state'), row.get('candidate_action'), row.get('candidate_fingerprint'))
        rows.append((item, 'UNCHANGED' if same else 'CHANGED', None if same else 'FACIAL_LEDGER_CHANGED',
                     row.get('state'), now['state']))
    for row in record.get('secondary') or []:
        item = 'secondary_ledger:' + str(row.get('generation_id'))
        try:
            now = _load_secondary(storage, row['ref'])
        except ValueError as exc:
            rows.append((item, 'CHANGED', 'SECONDARY_LEDGER_INVALID', row.get('state'), str(exc)[:200]))
            continue
        same = (now['state'], now['objects'], now['cache']) == (row.get('state'), row.get('objects'), row.get('cache'))
        rows.append((item, 'UNCHANGED' if same else 'CHANGED', None if same else 'SECONDARY_LEDGER_CHANGED',
                     row.get('state'), now['state']))
    return rows


def compare_actor(record, observed, ledger_rows=()):
    """Pure: compare one actor's registered integrity with freshly measured digests.
    Returns (checked, changed, info). checked rows: {item, status}; changed rows add code/expected/observed."""
    checked = []
    changed = []
    base = {'actor_id': record['actor_id'], 'object': record['object']}

    def add(item, status, code=None, expected=None, seen=None, **extra):
        checked.append(dict(base, item=item, status=status))
        if status != 'UNCHANGED' and status != 'NOT_APPLICABLE':
            changed.append(dict(base, item=item, status=status, code=code, expected=short_digest(expected),
                                observed=short_digest(seen), **extra))

    integrity = record.get('integrity') or {}
    info = {'moved': None, 'active_action': None}
    if not observed or not observed.get('exists'):
        add('object', 'MISSING', 'ACTOR_OBJECT_MISSING', record['object'], None)
        return checked, changed, info
    expected_object = integrity.get('object') or {}
    if observed['type'] != expected_object.get('type') or observed['data'] != expected_object.get('data'):
        add('object', 'CHANGED', 'ACTOR_OBJECT_REPLACED', [expected_object.get('type'), expected_object.get('data')],
            [observed['type'], observed['data']])
    elif not observed.get('in_scene'):
        add('object', 'CHANGED', 'ACTOR_NOT_IN_SOURCE_SCENE', record.get('scene'), None)
    else:
        add('object', 'UNCHANGED')
    if integrity.get('rig') is not None or observed.get('rig') is not None:
        add('rig', 'UNCHANGED' if observed.get('rig') == integrity.get('rig') else 'CHANGED', 'RIG_CHANGED',
            integrity.get('rig'), observed.get('rig'))
    expected_set = sorted(integrity.get('mesh_set') or [])
    add('mesh_set', 'UNCHANGED' if sorted(observed.get('mesh_set') or []) == expected_set else 'CHANGED',
        'ACTOR_MESH_SET_CHANGED', expected_set, sorted(observed.get('mesh_set') or []))
    for mesh, sections in sorted((integrity.get('meshes') or {}).items()):
        now = (observed.get('meshes') or {}).get(mesh)
        if now is None:
            add('mesh:' + mesh, 'MISSING', 'MESH_MISSING', mesh, None)
            continue
        differing = [name for name, value in sections.items() if now.get(name) != value]
        if not differing:
            add('mesh:' + mesh, 'UNCHANGED')
        for name in differing:
            add('mesh:' + mesh, 'CHANGED', 'MESH_CHANGED', sections[name], now.get(name), section=name)
    for name, row in sorted((integrity.get('body_actions') or {}).items()):
        now = (observed.get('body_actions') or {}).get(name)
        if now is None:
            add('body_action:' + name, 'MISSING', 'BODY_ACTION_MISSING', row.get('digest'), None)
        elif now.get('digest') != row.get('digest'):
            add('body_action:' + name, 'CHANGED', 'BODY_ACTION_CHANGED', row.get('digest'), now.get('digest'))
        elif not now.get('slot'):
            add('body_action:' + name, 'CHANGED', 'BODY_ACTION_SLOT_MISSING', row.get('slot'), None)
        else:
            add('body_action:' + name, 'UNCHANGED')
    for name, digest in sorted((integrity.get('facial_actions') or {}).items()):
        now = (observed.get('facial') or {}).get(name) or {}
        if not now.get('exists'):
            add('facial_action:' + name, 'MISSING', 'FACIAL_ACTION_MISSING', digest, None)
        else:
            add('facial_action:' + name, 'UNCHANGED' if now.get('digest') == digest else 'CHANGED',
                'FACIAL_ACTION_CHANGED', digest, now.get('digest'))
    for key, row in sorted((integrity.get('secondary') or {}).items()):
        now = (observed.get('secondary') or {}).get(key) or {}
        if now.get('missing'):
            add(key, 'MISSING', 'SECONDARY_OBJECT_MISSING', row.get('names'), now.get('missing'))
        else:
            add(key, 'UNCHANGED' if now.get('digest') == row.get('digest') else 'CHANGED', 'SECONDARY_CHANGED',
                row.get('digest'), now.get('digest'))
    for item, status, code, expected, seen in ledger_rows:
        add(item, status, code, expected, seen)
    before = (record.get('world_transform') or {}).get('matrix_world')
    after = observed.get('matrix_world')
    if before and after:
        # Informational only: staging legitimately moves actors (recorded deltas), so motion is not a stale signal.
        info['world_matrix_max_abs_change'] = max(abs(a - b) for ra, rb in zip(after, before) for a, b in zip(ra, rb))
        info['moved'] = info['world_matrix_max_abs_change'] > 0.0
    info['active_action'] = observed.get('active_action')
    return checked, changed, info


def _counts(checked):
    counts = {}
    for row in checked:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    return counts


# ------------------------------------------------------------------ integrity of a cinematic plan

def integrity_report(ctx, plan, state):
    """Read-only integrity report for a cinematic plan: every actor's registered digests (body and facial Actions,
    rig, meshes, shape keys, UV, material slots/indices, M8 objects and M7/M8 ledgers) and the source scene
    fingerprint against the ledger, plus the candidate scene and the source cameras/lights. One bridge call, no
    frame_set. Returns {schema, passed, changed, checked, counts, source, candidate, cameras_lights, actor_reports}."""
    checked = []
    changed = []
    key = plan.get('actor_registry_key')
    records = []
    for row in plan.get('actors') or []:
        try:
            record = registry.find(ctx.storage, key, row['actor_id'])
        except ValueError as exc:
            item = {'actor_id': row.get('actor_id'), 'object': row.get('object'), 'item': 'registry', 'status': 'MISSING'}
            checked.append(item)
            changed.append(dict(item, code='ACTOR_RECORD_MISSING', expected=row.get('fingerprint'), observed=str(exc)[:200]))
            continue
        if record.get('fingerprint') != row.get('fingerprint'):
            item = {'actor_id': row['actor_id'], 'object': row.get('object'), 'item': 'registry', 'status': 'CHANGED'}
            checked.append(item)
            changed.append(dict(item, code='ACTOR_RECORD_MISMATCH', expected=short_digest(row.get('fingerprint')),
                                observed=short_digest(record.get('fingerprint'))))
        records.append(record)
    assembly = state.get('assembly') or {}
    terminal_rejected = state.get('state') == 'REJECTED'
    params = {'source_scene': plan['source']['scene'], 'generation_id': plan['generation_id'],
              'version': assembly.get('version', 1), 'collections': assembly.get('collections'),
              'scene': assembly.get('scene') if assembly and not terminal_rejected else None,
              'scene_checks': True, 'actors': [actor_spec(record) for record in records]}
    observed = ctx.bridge.run(DIGEST_BODY, params)
    fingerprints = state.get('fingerprints') or {}
    if terminal_rejected or not fingerprints.get('source'):
        # A rejected candidate must have restored the source exactly as planned; before any assembly the plan is the
        # only reference.
        expected_source, basis = plan['source']['fingerprint'], 'plan'
    else:
        expected_source, basis = fingerprints['source'], 'ledger'
    source = {'item': 'source_scene', 'basis': basis, 'expected': short_digest(expected_source),
              'observed': short_digest(observed['source'])}
    if not observed['source_scene_exists']:
        source['status'] = 'MISSING'
    else:
        source['status'] = 'UNCHANGED' if observed['source'] == expected_source else 'CHANGED'
    checked.append({'item': 'source_scene', 'status': source['status']})
    if source['status'] != 'UNCHANGED':
        changed.append({'item': 'source_scene', 'status': source['status'], 'code': 'SOURCE_SCENE_CHANGED',
                        'expected': source['expected'], 'observed': source['observed'], 'basis': basis})
    candidate = {'item': 'candidate_scene', 'status': 'NOT_APPLICABLE', 'measurement': observed['candidate_status']}
    if params['scene']:
        if observed['candidate_status'] != 'MEASURED':
            candidate['status'] = 'MISSING'
        else:
            candidate['status'] = 'UNCHANGED' if observed['candidate'] == fingerprints.get('candidate') else 'CHANGED'
        candidate.update(expected=short_digest(fingerprints.get('candidate')), observed=short_digest(observed['candidate']))
        if candidate['status'] != 'UNCHANGED':
            changed.append({'item': 'candidate_scene', 'status': candidate['status'], 'code': 'CANDIDATE_SCENE_CHANGED',
                            'expected': candidate['expected'], 'observed': candidate['observed']})
    checked.append({'item': 'candidate_scene', 'status': candidate['status']})
    lights = observed.get('cameras_lights') or {}
    baseline = (plan.get('source') or {}).get('cameras_lights')
    cameras = {'item': 'source_cameras_lights', 'count': lights.get('count'), 'scene_camera': lights.get('scene_camera')}
    if baseline and isinstance(baseline.get('objects'), dict) and isinstance(lights.get('objects'), dict):
        differing = sorted(name for name in set(baseline['objects']) | set(lights['objects'])
                           if baseline['objects'].get(name) != lights['objects'].get(name))
        cameras.update(status='CHANGED' if differing else 'UNCHANGED', objects_changed=differing, basis='plan')
        if differing:
            changed.append({'item': 'source_cameras_lights', 'status': 'CHANGED', 'code': 'SOURCE_CAMERA_LIGHT_CHANGED',
                            'objects': differing})
    elif source['status'] == 'UNCHANGED':
        # cm_source_fingerprint covers camera/light data digests, transforms and animation of every source object.
        cameras.update(status='UNCHANGED', basis='covered_by_source_fingerprint')
    else:
        cameras.update(status='UNKNOWN', basis='no per-object baseline in the plan; the source fingerprint changed')
    checked.append({'item': 'source_cameras_lights', 'status': cameras['status']})
    actor_reports = []
    for record in records:
        rows = _ledger_rows(ctx.storage, record)
        actor_checked, actor_changed, info = compare_actor(record, observed['actors'].get(record['actor_id']), rows)
        checked += actor_checked
        changed += actor_changed
        actor_reports.append({'actor_id': record['actor_id'], 'object': record['object'],
                              'status': 'UNCHANGED' if not actor_changed else 'CHANGED', 'changed': len(actor_changed),
                              'checked': len(actor_checked), **info})
    return {'schema': INTEGRITY_SCHEMA, 'passed': not changed, 'changed': changed,
            'checked': checked[:MAX_CHECKED_ROWS], 'checked_total': len(checked), 'counts': _counts(checked),
            'source': source, 'candidate': candidate, 'cameras_lights': cameras, 'actor_reports': actor_reports,
            'method': 'rest-data and Action digests measured in Blender without frame_set, compared with the actor '
                      'registry and the cinematic ledger',
            'limitations': ['evaluated deformation, NLA composition and render output are not part of this report',
                            'per-object camera/light attribution needs a per-object baseline in the plan']}


# ------------------------------------------------------------------ handlers

@handler('cinematic_inspect_scene')
def inspect_scene(ctx, req):
    _arguments(req, ())
    _read_only(req)
    opts = model.closed(req.options, {'source_scene', 'limit', 'offset', 'detail', 'sections'})
    limit = model.number(opts.get('limit', 50), 'limit', 1, 200, integer=True)
    offset = model.number(opts.get('offset', 0), 'offset', 0, 1_000_000, integer=True)
    detail = model.choice(opts.get('detail', 'summary'), 'detail', DETAILS)
    sections = opts.get('sections', list(SCENE_SECTIONS))
    if not isinstance(sections, list) or not sections or len(set(sections)) != len(sections):
        raise ValueError('CINEMATIC_LIST_INVALID: sections must be a non-empty list of ' + ', '.join(SCENE_SECTIONS))
    for name in sections:
        model.choice(name, 'sections', SCENE_SECTIONS)
    source_scene = _blender_name(opts['source_scene'], 'source_scene') if 'source_scene' in opts else None
    result = ctx.bridge.run(SCENE_BODY, {'source_scene': source_scene, 'limit': limit, 'offset': offset,
                                         'detail': detail, 'sections': sections, 'schema': SCENE_SCHEMA})
    return {'status': 'success', **result, 'read_only': True}


@handler('cinematic_register_actor')
def register_actor(ctx, req):
    """Host-only registry write: measures the actor in Blender (read-only payload), validates M7/M8 references against
    their ledgers and writes one immutable record. Idempotent; never touches Blender, so it is allowed on MASTER."""
    _arguments(req, {'object_name'})
    opts = model.closed(req.options, REGISTER_OPTIONS)
    if req.object_name is None:
        raise ValueError('ACTOR_OBJECT_REQUIRED: pass object_name (an ARMATURE, MESH or EMPTY of the source scene)')
    object_name = _blender_name(req.object_name, 'object_name')
    if 'role' not in opts:
        raise ValueError('ACTOR_ROLE_REQUIRED: options.role is one of ' + ', '.join(model.ACTOR_ROLES))
    role = model.choice(opts['role'], 'role', model.ACTOR_ROLES)
    bones = _bones(opts.get('bones'))
    body_actions = _strings(opts['body_actions'], 'body_actions', MAX_BODY_ACTIONS) if 'body_actions' in opts else None
    if body_actions is not None:
        for name in body_actions:
            _blender_name(name, 'body_actions')
    facial_refs = _strings(opts.get('facial', []), 'facial', MAX_REFERENCES)
    secondary_refs = _strings(opts.get('secondary', []), 'secondary', MAX_REFERENCES)
    source_scene = _blender_name(opts['source_scene'], 'source_scene') if 'source_scene' in opts else None
    facial = [_load_facial(ctx.storage, ref) for ref in facial_refs]
    secondary = [_load_secondary(ctx.storage, ref) for ref in secondary_refs]
    for index, row in enumerate(secondary):
        row['key'] = 'secondary:' + str(index)
    params = {'object': object_name, 'source_scene': source_scene, 'bones': bones, 'body_actions': body_actions,
              'semantic_roles': SEMANTIC_ROLES, 'confidence_min': SEMANTIC_CONFIDENCE_MIN,
              'facial_actions': sorted({row['candidate_action'] for row in facial if row['candidate_action']}),
              'secondary': [{'key': row['key'], 'names': row['objects']} for row in secondary],
              'max_meshes': MAX_MESHES, 'max_vertices': MAX_VERTICES}
    snap = ctx.bridge.run(REGISTER_BODY, params)
    warnings = []
    for row in facial:
        _match_facial(row, snap, warnings)
    for row in secondary:
        _match_secondary(row, snap, warnings)
    for role_name, row in sorted(snap['low_confidence'].items()):
        warnings.append({'code': 'ACTOR_SEMANTICS_LOW_CONFIDENCE', 'role': role_name, 'bone': row['bone'],
                         'confidence': row['confidence'], 'message': 'below ' + str(SEMANTIC_CONFIDENCE_MIN)
                         + '; pass options.bones explicitly to use it'})
    asset = current_asset(ctx.bridge, ctx.storage)
    key = registry.asset_key(asset, snap['scene'])
    record = build_record(snap, role, asset, key, facial, secondary)
    existing = registry.match(ctx.storage, key, record['object'], record['data'], record['fingerprint'])
    if existing is not None:
        return {'status': 'registered', 'created': False, 'idempotent': True, 'actor_id': existing['actor_id'],
                'asset_key': key, 'actor': public_record(existing), 'warnings': warnings, 'blender_changed': False}
    previous = [row for row in registry.records(ctx.storage, key)
                if row.get('object') == record['object'] and row.get('fingerprint') != record['fingerprint']]
    if previous:
        latest = max(previous, key=lambda row: row.get('registered_at') or '')
        warnings.append({'code': 'ACTOR_FINGERPRINT_CHANGED', 'previous_actor_id': latest.get('actor_id'),
                         'changed': _record_changes(latest, record),
                         'message': 'a new record was written; the previous one is kept unchanged'})
    written = registry.write(ctx.storage, key, record)
    return {'status': 'registered', 'created': True, 'idempotent': False, 'actor_id': written['actor_id'],
            'asset_key': key, 'actor': public_record(written), 'warnings': warnings, 'blender_changed': False}


@handler('cinematic_inspect_actor')
def inspect_actor(ctx, req):
    _arguments(req, {'actor'})
    _read_only(req)
    opts = model.closed(req.options, {'source_scene', 'detail'})
    actor_id = _actor_id(req.actor)
    detail = model.choice(opts.get('detail', 'summary'), 'detail', DETAILS)
    source_scene = _blender_name(opts['source_scene'], 'source_scene') if 'source_scene' in opts else None
    record = actor_record(ctx, actor_id, scene=source_scene)
    observed = ctx.bridge.run(DIGEST_BODY, {'source_scene': source_scene or record.get('scene'), 'scene': None,
                                            'scene_checks': False, 'actors': [actor_spec(record)]})
    checked, changed, info = compare_actor(record, observed['actors'].get(actor_id), _ledger_rows(ctx.storage, record))
    fresh = not changed
    result = {'status': 'success' if fresh else 'warning', 'actor_id': actor_id, 'actor': public_record(record, detail),
              'freshness': 'fresh' if fresh else 'stale', 'code': None if fresh else 'ACTOR_STALE',
              'changed': changed, 'checked': len(checked), 'counts': _counts(checked), **info, 'read_only': True}
    if not fresh:
        result['warnings'] = [{'code': 'ACTOR_STALE', 'changed': [row['item'] for row in changed],
                               'message': 'the actor no longer matches its registry record; register it again'}]
    return result


@handler('cinematic_verify_integrity')
def verify_integrity(ctx, req):
    _arguments(req, {'cinematic'})
    _read_only(req)
    opts = model.closed(req.options, {'detail'})
    detail = model.choice(opts.get('detail', 'summary'), 'detail', DETAILS)
    path, plan, state = store.load(ctx.storage, req.cinematic)
    report = integrity_report(ctx, plan, state)
    if detail == 'summary':
        # Summary keeps only rows that need attention; counts still cover every checked item.
        report['checked'] = [row for row in report['checked']
                             if row['status'] not in ('UNCHANGED', 'NOT_APPLICABLE')][:MAX_CHECKED_ROWS]
    result = {'status': 'success' if report['passed'] else 'warning', **store.public(path, plan, state), **report,
              'read_only': True}
    if not report['passed']:
        result['warnings'] = [{'code': 'CINEMATIC_INTEGRITY_CHANGED', 'items': [row['item'] for row in report['changed']][:50]}]
    return result
