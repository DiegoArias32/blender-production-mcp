"""Shared Blender payload helpers for M10. Composed with the M3 base runtime; every body passes safe mode.

Conventions (docs/cinematic-architecture.md):
- the candidate lives in its own scene `M10C_<g8>_v<n>`; owned datablocks carry `production_m10_owner/version/role`;
- object-level changes to shared actors are returned as `touched` rows (value before the change) so the host can
  record them and `cm_undo` can reverse them;
- every evaluation runs inside `bpy.context.temp_override(scene=candidate)` and restores frame, channels, shape keys
  and NLA strip time with one frame_set (restoring object by object lost manual values — documented in M8).
"""
from __future__ import annotations

from ..common.m3_transaction import BASE_RUNTIME

COMMON = BASE_RUNTIME + r'''
import math
import mathutils

def cm_fail(code, detail):
    raise ValueError(code + ': ' + str(detail))

def cm_scene(name):
    scene = bpy.data.scenes.get(name)
    if scene is None:
        cm_fail('CINEMATIC_SCENE_MISSING', name)
    return scene

def cm_object(name, types=None):
    obj = bpy.data.objects.get(name)
    if obj is None:
        cm_fail('CINEMATIC_OBJECT_MISSING', name)
    if types and obj.type not in types:
        cm_fail('CINEMATIC_OBJECT_TYPE', obj.name + ' is ' + obj.type + ', expected ' + '/'.join(types))
    return obj

def cm_is_owned(idblock, params):
    return idblock is not None and idblock.get('production_m10_owner') == params['generation_id']

def cm_mark(idblock, params, role):
    idblock['production_m10_owner'] = params['generation_id']
    idblock['production_m10_version'] = int(params['version'])
    idblock['production_m10_role'] = role
    return idblock

def cm_candidate(params):
    """Candidate scene and its two collections, verified as owned by this generation and version."""
    scene = cm_scene(params['scene'])
    if not cm_is_owned(scene, params) or scene.get('production_m10_version') != int(params['version']):
        cm_fail('CINEMATIC_CANDIDATE_NOT_OWNED', scene.name)
    src = bpy.data.collections.get(params['collections']['src'])
    cin = bpy.data.collections.get(params['collections']['cin'])
    if src is None or cin is None or not cm_is_owned(src, params) or not cm_is_owned(cin, params):
        cm_fail('CINEMATIC_CANDIDATE_COLLECTIONS_MISSING', scene.name)
    return scene, src, cin

def cm_free_name(collection, name):
    if collection.get(name) is not None:
        cm_fail('CINEMATIC_NAME_COLLISION', name + ' already exists; nothing is overwritten')
    return name

def cm_new_object(params, name, data, collection, role):
    cm_free_name(bpy.data.objects, name)
    obj = bpy.data.objects.new(name, data)
    if obj.name != name:
        cm_fail('CINEMATIC_NAME_COLLISION', name)
    collection.objects.link(obj)
    cm_mark(obj, params, role)
    if data is not None:
        cm_mark(data, params, role)
    return obj

def cm_new_action(params, name, role):
    cm_free_name(bpy.data.actions, name)
    action = bpy.data.actions.new(name)
    if action.name != name:
        cm_fail('CINEMATIC_NAME_COLLISION', name)
    return cm_mark(action, params, role)

def cm_id(id_type, name):
    if id_type == 'OBJECT':
        return cm_object(name)
    if id_type == 'KEY':
        key = bpy.data.shape_keys.get(name)
        if key is None:
            cm_fail('CINEMATIC_KEY_MISSING', name)
        return key
    if id_type == 'CAMERA':
        data = bpy.data.cameras.get(name)
        if data is None:
            cm_fail('CINEMATIC_CAMERA_DATA_MISSING', name)
        return data
    cm_fail('CINEMATIC_ID_TYPE_UNSUPPORTED', id_type)

def cm_depsgraph(scene):
    with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
        return bpy.context.evaluated_depsgraph_get()

def cm_evaluated(scene, obj):
    # Read evaluated values, never the original datablock: inside temp_override(scene=candidate) the first
    # frame_set evaluated the NLA correctly (1.5) but left the original pose at 0.0 (measured, Blender 5.2.1).
    # The depsgraph is the scene's own view layer (cm_depsgraph): the context one can belong to the window's scene,
    # and objects that only live in the candidate kept a stale matrix_world until their own view layer was
    # evaluated (measured by the analysis area).
    return obj.evaluated_get(cm_depsgraph(scene))

def cm_ensure_pose(obj):
    # Blender 5.2.1 segfaults when an action slot is assigned on an armature whose pose was never evaluated
    # (measured, exit 139). Evaluate first.
    if obj is not None and getattr(obj, 'type', None) == 'ARMATURE' and obj.pose is None:
        bpy.context.view_layer.update()

# ---- touched records: value before an object-level change, reversed by cm_undo ----

def cm_touch_delta(obj):
    return {'kind': 'delta', 'object': obj.name, 'delta_location': list(obj.delta_location),
            'delta_rotation_euler': list(obj.delta_rotation_euler),
            'delta_rotation_quaternion': list(obj.delta_rotation_quaternion), 'delta_scale': list(obj.delta_scale)}

def cm_touch_animdata(idblock, id_type):
    ad = idblock.animation_data
    if ad is None:
        return {'kind': 'animdata', 'id_type': id_type, 'id_name': idblock.name, 'existed': False}
    return {'kind': 'animdata', 'id_type': id_type, 'id_name': idblock.name, 'existed': True,
            'action': ad.action.name if ad.action else None,
            'slot_identifier': ad.action_slot.identifier if ad.action_slot else None,
            'blend': ad.action_blend_type, 'influence': ad.action_influence,
            'extrapolation': ad.action_extrapolation, 'use_nla': ad.use_nla}

def cm_touch_track(idblock, id_type, track_name):
    return {'kind': 'nla_track', 'id_type': id_type, 'id_name': idblock.name, 'track': track_name}

def cm_touch_mute(idblock, id_type, track):
    return {'kind': 'nla_mute', 'id_type': id_type, 'id_name': idblock.name, 'track': track.name, 'mute': track.mute}

def cm_touch_link(collection, obj):
    return {'kind': 'object_link', 'collection': collection.name, 'object': obj.name}

def cm_touch_unlink(collection, obj):
    return {'kind': 'object_unlink', 'collection': collection.name, 'object': obj.name}

def cm_new_track(idblock, id_type, track_name, params):
    ad = idblock.animation_data_create()
    if ad.nla_tracks.get(track_name) is not None:
        cm_fail('CINEMATIC_NAME_COLLISION', 'NLA track ' + track_name)
    if ad.use_tweak_mode:
        cm_fail('NLA_TWEAK_MODE', idblock.name + ' is in NLA tweak mode')
    track = ad.nla_tracks.new()
    track.name = track_name
    return track

def cm_undo(touched):
    """Reverse touched rows in reverse order. Returns rows that could not be reversed (never raises midway)."""
    failed = []
    for row in reversed(touched):
        try:
            kind = row['kind']
            if kind == 'delta':
                obj = cm_object(row['object'])
                obj.delta_location = row['delta_location']
                obj.delta_rotation_euler = row['delta_rotation_euler']
                obj.delta_rotation_quaternion = row['delta_rotation_quaternion']
                obj.delta_scale = row['delta_scale']
            elif kind == 'nla_track':
                idblock = cm_id(row['id_type'], row['id_name'])
                ad = idblock.animation_data
                track = ad.nla_tracks.get(row['track']) if ad else None
                if track is not None:
                    ad.nla_tracks.remove(track)
            elif kind == 'nla_mute':
                idblock = cm_id(row['id_type'], row['id_name'])
                track = idblock.animation_data.nla_tracks.get(row['track'])
                track.mute = row['mute']
            elif kind == 'animdata':
                idblock = cm_id(row['id_type'], row['id_name'])
                if not row['existed']:
                    ad = idblock.animation_data
                    if ad is not None and ad.action is None and len(ad.nla_tracks) == 0 and len(ad.drivers) == 0:
                        idblock.animation_data_clear()
                else:
                    ad = idblock.animation_data_create()
                    action = bpy.data.actions.get(row['action']) if row['action'] else None
                    if row['id_type'] == 'OBJECT':
                        cm_ensure_pose(idblock)
                    ad.action = action
                    if action is not None and row['slot_identifier']:
                        slot = action.slots.get(row['slot_identifier'])
                        if slot is not None and ad.action_slot != slot:
                            ad.action_slot = slot
                    ad.action_blend_type = row['blend']
                    ad.action_influence = row['influence']
                    ad.action_extrapolation = row['extrapolation']
                    ad.use_nla = row['use_nla']
            elif kind == 'object_unlink':
                collection = bpy.data.collections.get(row['collection'])
                obj = bpy.data.objects.get(row['object'])
                if collection is not None and obj is not None and collection.objects.get(obj.name) is None:
                    collection.objects.link(obj)
            elif kind == 'object_link':
                collection = bpy.data.collections.get(row['collection'])
                obj = bpy.data.objects.get(row['object'])
                if collection is not None and obj is not None and collection.objects.get(obj.name) is not None:
                    collection.objects.unlink(obj)
            else:
                failed.append({'row': row, 'error': 'unknown kind'})
        except Exception as exc:
            failed.append({'row': row, 'error': str(exc)})
    return failed

# ---- capture / restore of shared objects around an evaluation ----

def cm_capture(scene, objects):
    owners = []
    for obj in objects:
        owners.append(obj)
        if obj.type == 'MESH' and obj.data.shape_keys:
            owners.append(obj.data.shape_keys)
    return {'frame': scene.frame_current, 'subframe': scene.frame_subframe,
            'camera': scene.camera.name if scene.camera else None,
            'objects': {o.name: {'object': m3_channels(o),
                                 'pose': {p.name: m3_channels(p) for p in o.pose.bones} if o.type == 'ARMATURE' and o.pose else {}}
                        for o in objects},
            'keys': {o.name: {'eval_time': o.data.shape_keys.eval_time,
                              'values': {k.name: k.value for k in o.data.shape_keys.key_blocks}}
                     for o in objects if o.type == 'MESH' and o.data.shape_keys},
            'nla': [[o.name, isinstance(o, bpy.types.Key), t.name, s.name, s.influence, s.strip_time]
                    for o in owners if o.animation_data for t in o.animation_data.nla_tracks for s in t.strips]}

def cm_restore(scene, objects, state):
    # One frame_set first, then every channel (restoring the frame per object lost manual values in M8).
    scene.frame_set(state['frame'], subframe=state['subframe'])
    for obj in objects:
        record = state['objects'].get(obj.name)
        if record is None:
            continue
        m3_set_channels(obj, record['object'])
        for bone_name, values in record['pose'].items():
            bone = obj.pose.bones.get(bone_name)
            if bone is not None:
                m3_set_channels(bone, values)
        obj.update_tag()
    for obj in objects:
        record = state['keys'].get(obj.name)
        if record is not None:
            keys = obj.data.shape_keys
            keys.eval_time = record['eval_time']
            for key_name, value in record['values'].items():
                block = keys.key_blocks.get(key_name)
                if block is not None:
                    block.value = value
    for owner_name, is_key, track_name, strip_name, influence, strip_time in state['nla']:
        owner = bpy.data.shape_keys.get(owner_name) if is_key else bpy.data.objects.get(owner_name)
        if owner is None or owner.animation_data is None:
            continue
        track = owner.animation_data.nla_tracks.get(track_name)
        strip = track.strips.get(strip_name) if track else None
        if strip is not None:
            strip.influence = influence
            strip.strip_time = strip_time
    bpy.context.view_layer.update()

# ---- camera projection without bpy_extras (forbidden by safe mode); equivalence measured <= 5e-8 ----

def cm_camera_matrices(cam, scene, depsgraph):
    render = scene.render
    width = render.resolution_x * render.resolution_percentage / 100.0
    height = render.resolution_y * render.resolution_percentage / 100.0
    projection = cam.calc_matrix_camera(depsgraph, x=int(round(width)), y=int(round(height)),
                                        scale_x=render.pixel_aspect_x, scale_y=render.pixel_aspect_y)
    view = cam.evaluated_get(depsgraph).matrix_world.inverted()
    return projection, view

def cm_project(projection, view, point):
    """(x, y) in [0, 1] frame coordinates (0, 0 bottom-left) and depth in front of the camera (> 0 visible side)."""
    p = mathutils.Vector((point[0], point[1], point[2], 1.0))
    v = view @ p
    clip = projection @ v
    depth = -v.z
    if abs(clip.w) < 1e-12:
        return None
    return [(clip.x / clip.w + 1.0) * 0.5, (clip.y / clip.w + 1.0) * 0.5, depth]

# ---- fingerprints (digests computed inside Blender; only digests travel) ----

def cm_action_digest(action):
    return m2_digest(m3_source_snapshot(action)) if action else None

def cm_camera_digest(data):
    dof = data.dof
    return m2_digest([data.type, data.lens, data.lens_unit, data.sensor_width, data.sensor_height, data.sensor_fit,
                      data.shift_x, data.shift_y, data.clip_start, data.clip_end, data.ortho_scale, dof.use_dof,
                      dof.focus_object.name if dof.focus_object else None, dof.focus_subtarget, dof.focus_distance,
                      dof.aperture_fstop, dof.aperture_blades])

def cm_light_digest(light):
    row = [light.type, light.energy, list(light.color), light.use_shadow, light.shadow_soft_size,
           light.use_temperature, light.temperature]
    if light.type == 'SPOT':
        row += [light.spot_size, light.spot_blend]
    if light.type == 'AREA':
        row += [light.shape, light.size, light.size_y]
    if light.type == 'SUN':
        row += [light.angle]
    return m2_digest(row)

def cm_animation_digest(idblock):
    ad = getattr(idblock, 'animation_data', None)
    if ad is None:
        return None
    tracks = []
    for track in ad.nla_tracks:
        tracks.append([track.name, track.mute, [[s.name, s.action.name if s.action else None, s.frame_start, s.frame_end,
                       s.action_frame_start, s.action_frame_end, s.scale, s.repeat, s.blend_type, s.extrapolation, s.mute,
                       cm_action_digest(s.action)] for s in track.strips]])
    return m2_digest([ad.action.name if ad.action else None, cm_action_digest(ad.action),
                      ad.action_slot.identifier if ad.action_slot else None, ad.action_blend_type, ad.action_influence,
                      ad.action_extrapolation, ad.use_nla, tracks])

def cm_scene_settings(scene):
    render = scene.render
    view = scene.view_settings
    return {'name': scene.name, 'frame_start': scene.frame_start, 'frame_end': scene.frame_end,
            'frame_current': scene.frame_current, 'fps': render.fps, 'fps_base': render.fps_base,
            'camera': scene.camera.name if scene.camera else None, 'world': scene.world.name if scene.world else None,
            'engine': render.engine, 'resolution': [render.resolution_x, render.resolution_y, render.resolution_percentage],
            'pixel_aspect': [render.pixel_aspect_x, render.pixel_aspect_y],
            'units': {'system': scene.unit_settings.system, 'scale_length': scene.unit_settings.scale_length},
            'view': {'transform': view.view_transform, 'look': view.look, 'exposure': view.exposure, 'gamma': view.gamma},
            'markers': [[m.name, m.frame, m.camera.name if m.camera else None] for m in scene.timeline_markers]}

def cm_transform_animated(obj):
    ad = obj.animation_data
    return ad is not None and (ad.action is not None or len(ad.nla_tracks) > 0 or len(ad.drivers) > 0)

def cm_basis(obj):
    # frame_set loops write evaluated values back into the original channels of animated objects (measured,
    # Blender 5.2.1): an animated transform is fingerprinted by its animation digest, never by the current matrix.
    return None if cm_transform_animated(obj) else [list(r) for r in obj.matrix_basis]

def cm_source_fingerprint(scene):
    """Scene-level state the user owns: settings, markers, membership, transforms, deltas and animation stacks."""
    rows = []
    for obj in sorted(scene.objects, key=cm_sort_name):
        # An animated delta (carry_root track) is flushed by frame_set like any animated channel: it is fingerprinted
        # through the animation digest, the static value otherwise.
        animated = cm_animated_paths(obj)
        rows.append([obj.name, obj.type, obj.data.name if obj.data else None, obj.parent.name if obj.parent else None,
                     cm_basis(obj), None if 'delta_location' in animated else list(obj.delta_location),
                     None if 'delta_rotation_euler' in animated else list(obj.delta_rotation_euler),
                     None if 'delta_rotation_quaternion' in animated else list(obj.delta_rotation_quaternion),
                     [c.name for c in obj.constraints], obj.hide_render,
                     cm_animation_digest(obj),
                     cm_camera_digest(obj.data) if obj.type == 'CAMERA' else cm_light_digest(obj.data) if obj.type == 'LIGHT' else None])
    settings = cm_scene_settings(scene)
    settings.pop('frame_current')
    return {'settings': settings, 'objects': len(rows), 'fingerprint': m2_digest([settings, rows])}

def cm_sort_name(item):
    return item.name

def cm_cameras_lights(scene):
    objects = {}
    for obj in sorted(scene.objects, key=cm_sort_name):
        if obj.type == 'CAMERA':
            objects[obj.name] = m2_digest([obj.type, obj.data.name, cm_camera_digest(obj.data), cm_basis(obj),
                                           cm_animation_digest(obj), cm_animation_digest(obj.data)])
        elif obj.type == 'LIGHT':
            objects[obj.name] = m2_digest([obj.type, obj.data.name, cm_light_digest(obj.data), cm_basis(obj),
                                           cm_animation_digest(obj), cm_animation_digest(obj.data)])
    return {'digest': m2_digest(objects), 'count': len(objects), 'objects': objects,
            'scene_camera': scene.camera.name if scene.camera else None}

def cm_animated_paths(obj):
    # Paths keyed by the object's Actions (active and NLA strips) or driven: like transforms, frame_set writes their
    # evaluated value into the original property (measured on keyed CHILD_OF influences), so the fingerprint leaves
    # the value to the animation digest.
    paths = set()
    ad = obj.animation_data
    if ad is None:
        return paths
    for d in ad.drivers:
        paths.add(d.data_path)
    actions = [ad.action]
    for track in ad.nla_tracks:
        for s in track.strips:
            actions.append(s.action)
    for action in actions:
        if action is None:
            continue
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for curve in bag.fcurves:
                        paths.add(curve.data_path)
    return paths

def cm_constraint_row(c, animated=None):
    keyed = animated is not None and ('constraints["' + c.name + '"].influence') in animated
    row = [c.name, c.type, c.mute, None if keyed else c.influence]
    target = getattr(c, 'target', None)
    row.append(target.name if target is not None else None)
    row.append(getattr(c, 'subtarget', None))
    return row

def cm_world_digest(world):
    if world is None:
        return None
    rows = [world.name, world.use_nodes]
    if world.use_nodes and world.node_tree:
        for node in world.node_tree.nodes:
            if node.type == 'BACKGROUND':
                rows.append([node.name, list(node.inputs['Color'].default_value), node.inputs['Strength'].default_value])
    return m2_digest(rows)

def cm_candidate_fingerprint(scene, src, cin):
    """Everything the cinematic tools own: candidate scene settings and markers, the source membership linked into
    it, and every owned object (transform, constraints, animation, camera/light data)."""
    rows = []
    for obj in sorted(cin.all_objects, key=cm_sort_name):
        data_digest = None
        if obj.type == 'CAMERA':
            data_digest = [cm_camera_digest(obj.data), cm_animation_digest(obj.data)]
        elif obj.type == 'LIGHT':
            data_digest = [cm_light_digest(obj.data), cm_animation_digest(obj.data)]
        animated = cm_animated_paths(obj)
        rows.append([obj.name, obj.type, cm_basis(obj), obj.hide_render,
                     [cm_constraint_row(c, animated) for c in obj.constraints], cm_animation_digest(obj), data_digest,
                     obj.parent.name if obj.parent else None])
    members = sorted(o.name for o in src.objects)
    settings = cm_scene_settings(scene)
    settings.pop('frame_current')
    return {'fingerprint': m2_digest([settings, members, rows, cm_world_digest(scene.world)]),
            'owned_objects': len(rows), 'source_members': len(members)}

def cm_fingerprints(params):
    source = cm_scene(params['source_scene'])
    scene, src, cin = cm_candidate(params)
    return {'source': cm_source_fingerprint(source)['fingerprint'],
            'candidate': cm_candidate_fingerprint(scene, src, cin)['fingerprint']}
'''
