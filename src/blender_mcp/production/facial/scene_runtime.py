"""Blender-side facial candidate writing, temporary composition, geometric QA and close-up renders (M7).

Loaded as source through the safe-mode bridge. Design decisions measured on Blender 5.2.1 (see
docs/milestone-7-baseline.md):
- an Action may hold one layer only, but several slots: the facial candidate is ONE new Action with an
  OBJECT slot for the armature (facial bones) and one KEY slot per Key datablock (shape keys);
- it is evaluated through NLA tracks on the armature and on each Key datablock, so the body Action stays
  active and untouched; overlapping channels of the active body Action win over the strip;
- removing an NLA track leaves the last evaluated pose behind, so every attach captures pose channels and
  shape-key values first and restores them explicitly.
Geometry QA always compares against a body-only baseline evaluated at the same frame, so rigid head motion
and pre-existing intersections of the asset are never reported as facial defects.
"""
from ..qa.capture import _STATE
from .runtime import FACIAL_RUNTIME

SCENE_RUNTIME = FACIAL_RUNTIME + _STATE + r'''

def fw_find_slot(action, id_type, name):
    ident = ('OB' if id_type == 'OBJECT' else 'KE') + name
    for slot in action.slots:
        if slot.identifier == ident:
            return slot
    return None

def fw_curve_path(ch):
    if ch['kind'] == 'bone':
        return 'pose.bones["' + ch['bone'] + '"].' + ch['path']
    return 'key_blocks["' + ch['key'] + '"].value'

def fw_key_id(mesh_name):
    obj = bpy.data.objects.get(mesh_name)
    if obj is None or obj.type != 'MESH' or obj.data.shape_keys is None:
        raise ValueError('SHAPE_KEYS_MISSING: ' + str(mesh_name))
    return obj.data.shape_keys.name

def fw_write(params):
    """Create (or append to) the facial candidate Action. Never overwrites an existing Action."""
    name = params['candidate_name']
    armature = bpy.data.objects.get(params['armature']) if params.get('armature') else None
    key_ids = {}
    for mesh_name in params['meshes']:
        key_ids[mesh_name] = fw_key_id(mesh_name)
    if params['mode'] == 'create':
        if bpy.data.actions.get(name) is not None:
            raise ValueError('FACIAL_CANDIDATE_COLLISION: an Action named ' + name + ' already exists; nothing is overwritten')
        action = bpy.data.actions.new(name)
        if action.name != name:
            raise ValueError('FACIAL_CANDIDATE_COLLISION: Blender renamed the candidate to ' + action.name)
        action.use_fake_user = True
        action['production_facial_owner'] = 'M7'
        action['production_facial_token'] = params['token']
        action['production_facial_generation'] = params['generation_id']
        action['production_facial_lifecycle'] = 'FACIAL_CANDIDATE'
        action['production_facial_version'] = params['version']
        if armature is not None and params['needs_armature_slot']:
            action.slots.new(id_type='OBJECT', name=armature.name)
        for mesh_name in params['meshes']:
            action.slots.new(id_type='KEY', name=key_ids[mesh_name])
        layer = action.layers.new('M7_FACE')
        layer.strips.new(type='KEYFRAME')
    else:
        action = bpy.data.actions.get(name)
        if action is None or action.get('production_facial_token') != params['token']:
            raise ValueError('FACIAL_CANDIDATE_MISMATCH: append target is not this operation candidate')
    strip = action.layers[0].strips[0]
    written = 0
    curves = 0
    for ch in params['channels']:
        if ch['kind'] == 'bone':
            slot = fw_find_slot(action, 'OBJECT', armature.name) if armature is not None else None
        else:
            slot = fw_find_slot(action, 'KEY', key_ids[ch['mesh']])
        if slot is None:
            raise ValueError('FACIAL_SLOT_MISSING: ' + ch.get('bone', ch.get('mesh', '?')))
        bag = strip.channelbag(slot, ensure=True)
        path = fw_curve_path(ch)
        if bag.fcurves.find(path, index=ch['index']) is not None:
            raise ValueError('FACIAL_CHANNEL_DUPLICATE: ' + path + '[' + str(ch['index']) + ']')
        curve = bag.fcurves.new(path, index=ch['index'])
        flat = ch['co']
        count = len(flat) // 2
        curve.keyframe_points.add(count)
        curve.keyframe_points.foreach_set('co', flat)
        for point in curve.keyframe_points:
            point.interpolation = 'LINEAR'
        curve.update()
        back = [0.0] * (2 * count)
        curve.keyframe_points.foreach_get('co', back)
        worst = 0.0
        for i in range(2 * count):
            worst = max(worst, abs(back[i] - flat[i]))
        if worst > 1e-4:
            raise ValueError('FACIAL_WRITE_VERIFY_FAILED: ' + path + ' differs by ' + str(worst))
        written += count
        curves += 1
    return {'action': action.name, 'curves_written': curves, 'keys_written': written,
            'slots': [s.identifier for s in action.slots], 'frame_range': list(action.frame_range),
            'fingerprint': m2_digest(m2_snapshot(action))}

def fw_capture_keys(key_names):
    out = {}
    for name in key_names:
        key = bpy.data.shape_keys.get(name)
        if key is None:
            raise ValueError('SHAPE_KEYS_MISSING: ' + name)
        out[name] = [[b.name, b.value] for b in key.key_blocks]
    return out

def fw_restore_keys(saved):
    for name, rows in saved.items():
        key = bpy.data.shape_keys.get(name)
        for block_name, value in rows:
            key.key_blocks[block_name].value = value

def fw_attach(action, armature, key_names, head_policy, track_name):
    """Evaluate a candidate on top of the body through new NLA tracks; returns what detach must undo."""
    state = {'arm': m3_capture(armature) if armature is not None else None, 'keys': fw_capture_keys(key_names),
             'tracks': [], 'blend': None, 'created': []}
    owners = []
    if armature is not None:
        owners.append((armature, fw_find_slot(action, 'OBJECT', armature.name)))
    for name in key_names:
        owners.append((bpy.data.shape_keys.get(name), fw_find_slot(action, 'KEY', name)))
    start = int(math.floor(action.frame_range[0]))
    for owner, slot in owners:
        if slot is None:
            continue
        if owner.animation_data is None:
            state['created'].append(owner)
        ad = owner.animation_data_create()
        if ad.use_tweak_mode:
            raise ValueError('NLA_TWEAK_MODE: leave NLA tweak mode before facial evaluation')
        track = ad.nla_tracks.new()
        track.name = track_name
        strip = track.strips.new(track_name, start, action)
        strip.action_slot = slot
        strip.blend_type = 'REPLACE'
        state['tracks'].append((ad, track))
    if armature is not None and head_policy == 'BLENDED' and armature.animation_data is not None:
        state['blend'] = armature.animation_data.action_blend_type
        armature.animation_data.action_blend_type = 'COMBINE'
    return state

def fw_detach(state, armature):
    for ad, track in state['tracks']:
        ad.nla_tracks.remove(track)
    if state['blend'] is not None:
        armature.animation_data.action_blend_type = state['blend']
    for owner in state['created']:
        owner.animation_data_clear()
    if armature is not None:
        m3_restore(armature, state['arm'])
    fw_restore_keys(state['keys'])
    bpy.context.view_layer.update()

def fw_key_names(fs):
    names = []
    for row in fs['faces'] + fs['eyes']:
        key = row['object'].data.shape_keys
        if key is not None and key.name not in names:
            names.append(key.name)
    return names

def fw_body_digest(armature):
    if armature is None or armature.animation_data is None or armature.animation_data.action is None:
        return {'action': None, 'digest': None}
    action = armature.animation_data.action
    return {'action': action.name, 'digest': m2_digest(m3_source_snapshot(action))}

def fw_pose_digest(armature, names):
    rows = []
    for name in names:
        m = armature.matrix_world @ armature.pose.bones[name].matrix
        rows.append([name, [round(v, 6) for col in m.col for v in col]])
    return rows

# ---------------------------------------------------------------- geometric QA

def fq_landmarks(face_obj, frame):
    """Lid and mouth margins from boundary loops: column pairs (upper vertex, lower vertex) at rest."""
    mid, left, up, forward, iod, zm, mw = frame
    pts = fa_world_coords(face_obj, False)
    out = {}
    widest = -1.0
    for loop in fa_boundary_loops(face_obj):
        local = [fz_frame_point(pts[i], mid, left, up, forward, iod) for i in loop]
        cx = sum(q[0] for q in local) / len(local)
        cz = sum(q[2] for q in local) / len(local)
        xs = [q[0] for q in local]
        half = (max(xs) - min(xs)) / 2
        name = None
        if cz < -0.6 and abs(cx) < 0.4:
            if half > widest:
                name = 'mouth'
                widest = half
        elif math.hypot(cx - 0.5, cz) < 0.35:
            name = 'eye_L'
        elif math.hypot(cx + 0.5, cz) < 0.35:
            name = 'eye_R'
        if name is None or half <= 1e-6:
            continue
        upper = [k for k in range(len(loop)) if local[k][2] >= cz]
        lower = [k for k in range(len(loop)) if local[k][2] < cz]
        pairs = []
        for k in upper:
            if abs(local[k][0] - cx) > 0.7 * half or not lower:
                continue
            best, best_d = None, 1e30
            for j in lower:
                d = abs(local[j][0] - local[k][0])
                if d < best_d:
                    best, best_d = j, d
            if best is not None and best_d < 0.25 * half:
                pairs.append([loop[k], loop[best]])
        if pairs:
            out[name] = {'pairs': pairs, 'center': [round(cx, 4), round(cz, 4)], 'half_width': round(half, 4),
                         'vertices': len(loop)}
    return out

def fq_align(armature, head_name, head_rest):
    """World matrix that removes the current rigid head motion (maps points back to the rest head)."""
    if armature is None or head_name is None:
        return Matrix.Identity(4)
    now = armature.matrix_world @ armature.pose.bones[head_name].matrix
    return head_rest @ now.inverted()

def fq_gaps(points, landmark, frame, align):
    up = frame[2]
    iod = frame[4]
    gaps = []
    for i, j in landmark['pairs']:
        gaps.append(((align @ points[i]) - (align @ points[j])).dot(up) / iod)
    return gaps

def fq_tree(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        mw = ev.matrix_world.copy()
        co = [0.0] * (3 * len(me.vertices))
        me.vertices.foreach_get('co', co)
        verts = [mw @ Vector((co[i], co[i + 1], co[i + 2])) for i in range(0, len(co), 3)]
        polys = [tuple(p.vertices) for p in me.polygons]
    finally:
        ev.to_mesh_clear()
    return BVHTree.FromPolygons(verts, polys, all_triangles=False, epsilon=0.0), verts

def fq_polys(obj):
    """Polygon normals (world) and areas of the evaluated mesh."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        n = len(me.polygons)
        normals = [0.0] * (3 * n)
        areas = [0.0] * n
        me.polygons.foreach_get('normal', normals)
        me.polygons.foreach_get('area', areas)
        m3 = ev.matrix_world.to_3x3()
    finally:
        ev.to_mesh_clear()
    out = []
    for i in range(n):
        v = m3 @ Vector((normals[3 * i], normals[3 * i + 1], normals[3 * i + 2]))
        out.append((v.normalized() if v.length > 0 else v, areas[i]))
    return out

def fq_context(fs, frame, head, others):
    """Everything QA needs, computed once at the rest state of the asset."""
    face = fs['face']['object']
    armature = fs['armature']
    head_rest = None
    if armature is not None and head is not None:
        head_rest = armature.matrix_world @ head.matrix_local
    eyes = []
    face_rest = fa_world_coords(face, False)
    for index, row in enumerate(fs['eyes']):
        center = row['fit']['center']
        radius = row['fit']['radius']
        near = [i for i, p in enumerate(face_rest) if (p - center).length < radius * 1.6]
        eyes.append({'side': 'L' if index == 0 else 'R', 'object': row['object'], 'center': center.copy(),
                     'radius': radius, 'rest_inverse': row['object'].matrix_world.inverted(), 'near': near,
                     'rest_matrix': row['object'].matrix_world.copy(), 'rest_points': fa_world_coords(row['object'], True)})
    pairs = []
    for row in fs['eyes']:
        pairs.append([face, row['object'], 'face/' + ('eye_L' if row is fs['eyes'][0] else 'eye_R')])
    for name in others:
        o = bpy.data.objects.get(name)
        if o is not None and o.type == 'MESH' and o != face:
            pairs.append([face, o, 'face/' + o.name])
    return {'face': face, 'armature': armature, 'head': head.name if head is not None else None, 'head_rest': head_rest,
            'landmarks': fq_landmarks(face, frame), 'eyes': eyes, 'pairs': pairs, 'frame': frame}

def fq_eye_angles(eye, align, frame):
    """Measured gaze of one evaluated eye relative to its rest, with rigid head motion removed (degrees)."""
    mid, left, up, forward, iod, zm, mw = frame
    delta = (align @ eye['object'].matrix_world).to_3x3().normalized() @ eye['rest_matrix'].to_3x3().normalized().inverted()
    f2 = delta @ forward
    yaw = math.degrees(math.atan2(f2.dot(left), f2.dot(forward)))
    pitch = math.degrees(math.atan2(f2.dot(up), math.sqrt(f2.dot(left) ** 2 + f2.dot(forward) ** 2)))
    if abs(yaw) < 0.05 and abs(pitch) < 0.05 and eye['object'].data.shape_keys is not None:
        now = [align @ p for p in fa_world_coords(eye['object'], True)]
        yaw, pitch = fa_eye_points_rotation(eye['rest_points'], now, eye['center'], frame)
    return [round(yaw, 3), round(pitch, 3)]

def fq_measure(ctx):
    """Raw geometry of the current evaluated state (compared later with a baseline at the same frame)."""
    frame = ctx['frame']
    armature = ctx['armature']
    align = fq_align(armature, ctx['head'], ctx['head_rest']) if ctx['head_rest'] is not None else Matrix.Identity(4)
    trees = {}
    face_tree, face_points = fq_tree(ctx['face'])
    trees[ctx['face'].name] = face_tree
    gaps = {}
    for name, landmark in ctx['landmarks'].items():
        gaps[name] = fq_gaps(face_points, landmark, frame, align)
    depth = {}
    for eye in ctx['eyes']:
        center = eye['object'].matrix_world @ (eye['rest_inverse'] @ eye['center'])
        worst = -1e9
        for i in eye['near']:
            worst = max(worst, eye['radius'] - (face_points[i] - center).length)
        depth[eye['side']] = worst
    overlaps = {}
    for a, b, label in ctx['pairs']:
        if b.name not in trees:
            trees[b.name] = fq_tree(b)[0]
        # Facial polygons that intersect the other mesh: moving an already-intersecting polygon is not new.
        overlaps[label] = set(p[0] for p in trees[a.name].overlap(trees[b.name]))
    gaze = {}
    for eye in ctx['eyes']:
        gaze[eye['side']] = fq_eye_angles(eye, align, frame)
    head_deg = None
    if ctx['head_rest'] is not None:
        mid, left, up, forward, iod, zm, mw = frame
        now = armature.matrix_world @ armature.pose.bones[ctx['head']].matrix
        f2 = (now.to_3x3().normalized() @ ctx['head_rest'].to_3x3().normalized().inverted()) @ forward
        head_deg = [round(math.degrees(math.atan2(f2.dot(left), f2.dot(forward))), 3),
                    round(math.degrees(math.atan2(f2.dot(up), math.sqrt(f2.dot(left) ** 2 + f2.dot(forward) ** 2))), 3)]
    return {'head_deg': head_deg, 'gaps': gaps, 'eye_depth': depth, 'overlaps': overlaps, 'polys': fq_polys(ctx['face']), 'gaze': gaze}

def fq_compare(base, now, ctx, options):
    """Facial-only change between a baseline and the current state, in mm and counts."""
    iod_mm = ctx['frame'][4] * 1000.0
    out = {'eyes': {}, 'landmarks': {}, 'overlap_new': {}, 'overlap_baseline': {}, 'collapsed': 0, 'flipped': 0}
    for side, value in now['eye_depth'].items():
        before = base['eye_depth'][side]
        out['eyes'][side] = {'penetration_mm': round(max(0.0, value) * 1000, 3),
                             'penetration_increase_mm': round(max(0.0, value - max(0.0, before)) * 1000, 3)}
    for name, gaps in now['gaps'].items():
        rest = base['gaps'][name]
        mean_now = sum(gaps) / len(gaps)
        mean_rest = sum(rest) / len(rest)
        row = {'gap_mean_mm': round(mean_now * iod_mm, 3), 'gap_min_mm': round(min(gaps) * iod_mm, 3),
               'gap_max_mm': round(max(gaps) * iod_mm, 3), 'baseline_gap_mean_mm': round(mean_rest * iod_mm, 3)}
        if name.startswith('eye') and mean_rest > 1e-9:
            row['closure'] = round(1.0 - mean_now / mean_rest, 4)
        out['landmarks'][name] = row
    for label, pairs in now['overlaps'].items():
        new = pairs - base['overlaps'][label]
        out['overlap_new'][label] = len(new)
        out['overlap_baseline'][label] = len(base['overlaps'][label])
    collapse = options.get('collapse_area_ratio', 0.05)
    flip = options.get('flip_dot', -0.5)
    for (n0, a0), (n1, a1) in zip(base['polys'], now['polys']):
        if a0 > 1e-12 and a1 < collapse * a0:
            out['collapsed'] += 1
        elif a0 > 1e-12 and n0.dot(n1) < flip:
            out['flipped'] += 1
    return out

def fq_apply_pose(armature, pose):
    """A control pose: {'keys': [[mesh, key, value]], 'bones': [[bone, path, index, value]]} (absolute values)."""
    for mesh_name, key_name, value in pose.get('keys', []):
        obj = bpy.data.objects.get(mesh_name)
        obj.data.shape_keys.key_blocks[key_name].value = value
    for bone, path, index, value in pose.get('bones', []):
        fa_set_channel(armature.pose.bones[bone], path, index, value)
    bpy.context.view_layer.update()

def fq_capability(params):
    """Measure explicit control poses against the rest state (eyelid closure, jaw/neck, lips...)."""
    fs = fa_set(params)
    head, note = fa_head_bone(fs['armature'], fs['options'], fs['eyes'])
    frame, notes, mouth_found = fa_frame(fs, head)
    others = [r['object'].name for r in fs['faces'] if r is not fs['face']]
    ctx = fq_context(fs, frame, head, others)
    armature = fs['armature']
    keys_saved = fw_capture_keys(fw_key_names(fs))
    arm_state = m3_capture(armature) if armature is not None else None
    rows = []
    try:
        base = fq_measure(ctx)
        for pose in params['poses']:
            fq_apply_pose(armature, pose)
            now = fq_measure(ctx)
            rows.append({'name': pose['name'], 'metrics': fq_compare(base, now, ctx, params.get('qa_options') or {})})
            fw_restore_keys(keys_saved)
            if armature is not None:
                m3_restore(armature, arm_state)
    finally:
        fw_restore_keys(keys_saved)
        if armature is not None:
            m3_restore(armature, arm_state)
    landmarks = {k: {'center': v['center'], 'half_width': v['half_width'], 'columns': len(v['pairs'])}
                 for k, v in ctx['landmarks'].items()}
    return {'poses': rows, 'landmarks': landmarks, 'iod_m': round(frame[4], 6), 'pairs': [p[2] for p in ctx['pairs']],
            'restored': keys_saved == fw_capture_keys(fw_key_names(fs))}

def fq_candidate(params):
    """Per-frame facial QA of the candidate against the body-only baseline at the same frames."""
    fs = fa_set(params)
    head, note = fa_head_bone(fs['armature'], fs['options'], fs['eyes'])
    frame, notes, mouth_found = fa_frame(fs, head)
    others = [r['object'].name for r in fs['faces'] if r is not fs['face']]
    ctx = fq_context(fs, frame, head, others)
    armature = fs['armature']
    action = bpy.data.actions.get(params['candidate'])
    if action is None:
        raise ValueError('FACIAL_CANDIDATE_MISSING: ' + str(params['candidate']))
    scene = bpy.context.scene
    key_names = fw_key_names(fs)
    body_before = fw_body_digest(armature)
    frames = params['frames']
    base = {}
    saved_frame = scene.frame_current
    saved_sub = scene.frame_subframe
    keys_saved = fw_capture_keys(key_names)
    arm_state = m3_capture(armature) if armature is not None else None
    try:
        for f in frames:
            scene.frame_set(f)
            base[f] = fq_measure(ctx)
    finally:
        scene.frame_set(saved_frame, subframe=saved_sub)
        fw_restore_keys(keys_saved)
        if armature is not None:
            m3_restore(armature, arm_state)
    state = fw_attach(action, armature, key_names, params.get('head_policy', 'BODY_OWNS'), 'M7_FACE_QA')
    rows = []
    ranges = []
    try:
        for f in frames:
            scene.frame_set(f)
            now = fq_measure(ctx)
            row = fq_compare(base[f], now, ctx, params.get('qa_options') or {})
            row['frame'] = f
            row['gaze_deg'] = now['gaze']
            row['head_deg'] = now['head_deg']
            for name in key_names:
                key = bpy.data.shape_keys.get(name)
                for block in key.key_blocks:
                    if block.value < block.slider_min - 1e-6 or block.value > block.slider_max + 1e-6:
                        ranges.append([f, name, block.name, round(block.value, 5)])
            rows.append(row)
    finally:
        scene.frame_set(saved_frame, subframe=saved_sub)
        fw_detach(state, armature)
    body_after = fw_body_digest(armature)
    restored = keys_saved == fw_capture_keys(key_names)
    return {'frames': rows, 'key_range_violations': ranges[:50], 'body_before': body_before, 'body_after': body_after,
            'body_unchanged': body_before == body_after, 'state_restored': restored, 'iod_m': round(frame[4], 6),
            'landmarks': sorted(ctx['landmarks']), 'pairs': [p[2] for p in ctx['pairs']]}

def fw_integration(params):
    """Body bones with and without the facial layer at the same frames: the body must not change."""
    fs = fa_set(params)
    armature = fs['armature']
    if armature is None:
        return {'armature': None, 'body_unchanged': True, 'frames': 0}
    head, note = fa_head_bone(armature, fs['options'], fs['eyes'])
    facial = set(b.name for b in fa_descendants(head)) if head is not None else set()
    neck = head.parent.name if head is not None and head.parent is not None else None
    owned = set()
    if head is not None:
        owned.add(head.name)
    if neck:
        owned.add(neck)
    body = [b.name for b in armature.pose.bones if b.name not in facial and b.name not in owned]
    action = bpy.data.actions.get(params['candidate'])
    scene = bpy.context.scene
    key_names = fw_key_names(fs)
    before = fw_body_digest(armature)
    base = {}
    saved = (scene.frame_current, scene.frame_subframe)
    arm_state = m3_capture(armature)
    try:
        for f in params['frames']:
            scene.frame_set(f)
            base[f] = (fw_pose_digest(armature, body), fw_pose_digest(armature, sorted(owned)))
    finally:
        scene.frame_set(saved[0], subframe=saved[1])
        m3_restore(armature, arm_state)
    state = fw_attach(action, armature, key_names, params.get('head_policy', 'BODY_OWNS'), 'M7_FACE_INTEGRATION')
    worst_body = 0.0
    worst_owned = 0.0
    try:
        for f in params['frames']:
            scene.frame_set(f)
            now_body = fw_pose_digest(armature, body)
            now_owned = fw_pose_digest(armature, sorted(owned))
            for (n0, m0), (n1, m1) in zip(base[f][0], now_body):
                worst_body = max(worst_body, max(abs(a - b) for a, b in zip(m0, m1)))
            for (n0, m0), (n1, m1) in zip(base[f][1], now_owned):
                worst_owned = max(worst_owned, max(abs(a - b) for a, b in zip(m0, m1)))
    finally:
        scene.frame_set(saved[0], subframe=saved[1])
        fw_detach(state, armature)
    after = fw_body_digest(armature)
    return {'body_action_before': before, 'body_action_after': after, 'body_action_unchanged': before == after,
            'body_bones': len(body), 'facial_bones': len(facial), 'head_neck': sorted(owned),
            'max_body_matrix_change': round(worst_body, 9), 'max_head_neck_matrix_change': round(worst_owned, 6),
            'frames': len(params['frames']), 'nla_tracks_after': [t.name for t in armature.animation_data.nla_tracks] if armature.animation_data else []}

def fw_head_track(params):
    """Per-frame head transform (body only: no facial layer attached) and per-frame target object positions."""
    armature = bpy.data.objects.get(params['armature']) if params.get('armature') else None
    head = params.get('head_bone')
    scene = bpy.context.scene
    saved = (scene.frame_current, scene.frame_subframe)
    state = m3_capture(armature) if armature is not None else None
    names = list(params.get('objects') or [])
    rows, objects = [], {}
    for name in names:
        objects[name] = []
    rest_inv = None
    if armature is not None and head:
        rest_inv = (armature.matrix_world @ armature.data.bones[head].matrix_local).inverted()
    worst_t, worst_r = 0.0, 0.0
    try:
        for f in params['frames']:
            scene.frame_set(f)
            if rest_inv is not None:
                m = (armature.matrix_world @ armature.pose.bones[head].matrix) @ rest_inv
                rows.append([round(m[r][c], 7) for r in range(3) for c in range(4)])
                worst_t = max(worst_t, m.to_translation().length)
                worst_r = max(worst_r, math.degrees(m.to_quaternion().angle))
            for name in names:
                o = scene.camera if name == '__scene_camera__' else bpy.data.objects.get(name)
                if o is None:
                    raise ValueError('GAZE_OBJECT_NOT_FOUND: ' + name)
                objects[name].append([round(v, 6) for v in o.matrix_world.translation])
    finally:
        scene.frame_set(saved[0], subframe=saved[1])
        if armature is not None:
            m3_restore(armature, state)
    return {'frames': list(params['frames']), 'head': rows or None, 'objects': objects,
            'max_head_translation_m': round(worst_t, 6), 'max_head_rotation_deg': round(worst_r, 4)}

# ---------------------------------------------------------------- lifecycle

def fw_mark(params):
    action = bpy.data.actions.get(params['candidate'])
    if action is None:
        raise ValueError('FACIAL_CANDIDATE_MISSING: ' + str(params['candidate']))
    if m2_digest(m2_snapshot(action)) != params['fingerprint']:
        raise ValueError('STALE_PROPOSAL: facial candidate changed since it was recorded')
    fs = fa_set(params)
    armature = fs['armature']
    before = fw_body_digest(armature)
    if before != params['body']:
        raise ValueError('BODY_ACTION_CHANGED: the body Action differs from the plan; nothing is attached')
    action['production_facial_lifecycle'] = params['lifecycle']
    action['production_facial_qa_result'] = params['qa_result']
    tracks = []
    if params['lifecycle'] == 'ACCEPTED':
        key_names = fw_key_names(fs)
        start = int(math.floor(action.frame_range[0]))
        owners = []
        if armature is not None:
            owners.append((armature, fw_find_slot(action, 'OBJECT', armature.name)))
        for name in key_names:
            owners.append((bpy.data.shape_keys.get(name), fw_find_slot(action, 'KEY', name)))
        for owner, slot in owners:
            if slot is None:
                continue
            ad = owner.animation_data_create()
            for t in ad.nla_tracks:
                if t.name.startswith('M7_FACE'):
                    raise ValueError('FACIAL_LAYER_EXISTS: ' + t.name + ' is already on ' + owner.name + '; reject or remove it manually')
            track = ad.nla_tracks.new()
            track.name = 'M7_FACE ' + action.name
            strip = track.strips.new('M7_FACE ' + action.name, start, action)
            strip.action_slot = slot
            strip.blend_type = 'REPLACE'
            tracks.append([owner.name, track.name])
        if params.get('head_policy') == 'BLENDED' and armature is not None and armature.animation_data is not None:
            action['production_facial_previous_blend'] = armature.animation_data.action_blend_type
            armature.animation_data.action_blend_type = 'COMBINE'
        bpy.context.scene.frame_set(bpy.context.scene.frame_current, subframe=bpy.context.scene.frame_subframe)
    after = fw_body_digest(armature)
    return {'lifecycle': params['lifecycle'], 'tracks': tracks, 'body_unchanged': before == after,
            'fingerprint': m2_digest(m2_snapshot(action))}

def fw_status(params):
    """Lightweight freshness: rig fingerprint, body Action digest, candidate fingerprint, file and stamps."""
    fs = fa_set(params)
    result = {'rig_fingerprint': fa_rig_fingerprint(fs), 'body': fw_body_digest(fs['armature']),
              'filepath': bpy.data.filepath, 'stamps': [s.get('production_asset') for s in bpy.data.scenes]}
    name = params.get('candidate')
    if name:
        action = bpy.data.actions.get(name)
        result['candidate_fingerprint'] = m2_digest(m2_snapshot(action)) if action is not None else None
    tracks = []
    for row in [fs['armature']] if fs['armature'] is not None else []:
        if row.animation_data is not None:
            tracks += [t.name for t in row.animation_data.nla_tracks if t.name.startswith('M7_FACE')]
    for name in fw_key_names(fs):
        key = bpy.data.shape_keys.get(name)
        if key.animation_data is not None:
            tracks += [t.name for t in key.animation_data.nla_tracks if t.name.startswith('M7_FACE')]
    result['facial_tracks'] = tracks
    result['key_values'] = m2_digest(fw_capture_keys(fw_key_names(fs)))
    return result

# ---------------------------------------------------------------- close-up renders

def fr_render(params):
    """Identical perspective cameras (lens, distance, target, light) for every view; Workbench on the GPU."""
    fs = fa_set(params)
    armature = fs['armature']
    scene = bpy.context.scene
    action = bpy.data.actions.get(params['candidate']) if params.get('candidate') else None
    if params.get('candidate') and action is None:
        raise ValueError('FACIAL_CANDIDATE_MISSING: ' + params['candidate'])
    key_names = fw_key_names(fs)
    head_name = params.get('head_bone')
    before = qa_state(scene)
    shading = scene.display.shading
    saved = {'engine': scene.render.engine, 'light': shading.light, 'color_type': shading.color_type,
             'background_type': shading.background_type, 'background_color': list(shading.background_color),
             'show_shadows': shading.show_shadows, 'show_cavity': shading.show_cavity,
             'film_transparent': scene.render.film_transparent}
    original_camera = scene.camera
    markers = [(m, m.camera) for m in scene.timeline_markers]
    camera_data = None
    camera = None
    state = None
    result = {'status': 'ok', 'files': [], 'series': [], 'errors': [], 'cameras': params['cameras']}
    try:
        if action is not None:
            state = fw_attach(action, armature, key_names, params.get('head_policy', 'BODY_OWNS'), 'M7_FACE_RENDER')
        camera_data = bpy.data.cameras.new('M7FacialQA_' + params['operation_id'])
        camera_data.type = 'PERSP'
        camera_data.lens = params['lens_mm']
        camera_data.sensor_fit = 'VERTICAL'
        camera_data.sensor_height = params['sensor_mm']
        camera_data.clip_start = params['clip'][0]
        camera_data.clip_end = params['clip'][1]
        camera = bpy.data.objects.new('M7FacialQA_' + params['operation_id'], camera_data)
        scene.collection.objects.link(camera)
        for m, c in markers:
            m.camera = None
        scene.render.engine = 'BLENDER_WORKBENCH'
        shading.light = 'STUDIO'
        shading.color_type = 'MATERIAL'
        shading.background_type = 'VIEWPORT'
        shading.background_color = (0.06, 0.06, 0.065)
        shading.show_shadows = False
        # Cavity shading (identical for every view and candidate) makes small facial relief readable.
        shading.show_cavity = True
        scene.render.film_transparent = False
        result['render_backend'] = qa_render_backend(scene, False)
        qa_configure(scene, params['width'], params['height'])
        head_rest = None
        if head_name and armature is not None:
            head_rest = armature.matrix_world @ armature.data.bones[head_name].head_local
        for view in params['cameras']:
            files = []
            direction = Vector(view['direction']).normalized()
            rotation = (-direction).to_track_quat('-Z', 'Y')
            for f in params['frames']:
                scene.frame_set(f)
                offset = Vector((0.0, 0.0, 0.0))
                if params.get('follow_head') and head_rest is not None:
                    offset = (armature.matrix_world @ armature.pose.bones[head_name].head) - head_rest
                camera.location = Vector(view['target']) + offset + direction * view['distance_m']
                camera.rotation_mode = 'QUATERNION'
                camera.rotation_quaternion = rotation
                scene.camera = camera
                bpy.context.view_layer.update()
                path = params['directories'][view['view']] + '/frame_' + format(f, '06d') + '.png'
                scene.render.filepath = path
                done = bpy.ops.render.render(write_still=True)
                if 'FINISHED' not in done:
                    raise RuntimeError('Render operator did not finish: ' + str(done))
                files.append({'path': path, 'frame': f})
            result['series'].append({'view': view['view'], 'files': files})
            result['files'].extend(files)
    except Exception as exc:
        result['status'] = 'error'
        result['errors'].append({'type': str(type(exc)), 'message': str(exc)})
    finally:
        if state is not None:
            fw_detach(state, armature)
        for m, c in markers:
            m.camera = c
        qa_restore(scene, before, original_camera)
        scene.render.engine = saved['engine']
        shading.light = saved['light']
        shading.color_type = saved['color_type']
        shading.background_type = saved['background_type']
        shading.background_color = saved['background_color']
        shading.show_shadows = saved['show_shadows']
        shading.show_cavity = saved['show_cavity']
        scene.render.film_transparent = saved['film_transparent']
        if camera is not None:
            bpy.data.objects.remove(camera, do_unlink=True)
        if camera_data is not None:
            bpy.data.cameras.remove(camera_data)
    after = qa_state(scene)
    result['state_restored'] = after == before
    if not result['state_restored']:
        result['status'] = 'error'
        result['errors'].append({'type': 'StateRestorationError', 'message': 'render settings differ after restoration'})
    result['metrics'] = {'images': len(result['files']), 'views': len(params['cameras']), 'frames': len(params['frames'])}
    return result
'''

WRITE = SCENE_RUNTIME + r'''
result = fw_write(params)
'''
CAPABILITY = SCENE_RUNTIME + r'''
result = fq_capability(params)
'''
CANDIDATE_QA = SCENE_RUNTIME + r'''
result = fq_candidate(params)
'''
INTEGRATION = SCENE_RUNTIME + r'''
result = fw_integration(params)
'''
MARK = SCENE_RUNTIME + r'''
result = fw_mark(params)
'''
HEAD_TRACK = SCENE_RUNTIME + r'''
result = fw_head_track(params)
'''
STATUS = SCENE_RUNTIME + r'''
result = fw_status(params)
'''
RENDER = SCENE_RUNTIME + r'''
result = fr_render(params)
'''
