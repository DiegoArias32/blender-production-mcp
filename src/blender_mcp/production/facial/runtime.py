"""Blender-side facial runtime (M7). Loaded as source through the safe-mode bridge, never imported in bpy.

Evidence, not names: every control (shape key or bone channel) is *probed* — its deformation of the
evaluated facial meshes is measured and summarized in the normalized face frame of ``zones.py``.
Per-vertex data never leaves Blender; payloads return compact statistics.
"""
import inspect

from ..common.m3_transaction import BASE_RUNTIME
from . import zones

ZONES_SOURCE = 'import math\n' + inspect.getsource(zones.fz_zone) + '\n' + inspect.getsource(zones.fz_frame_point) + \
    '\n' + inspect.getsource(zones.fz_frame_vector) + '\n' + inspect.getsource(zones.fz_field_stats) + '\n'

FACIAL_RUNTIME = BASE_RUNTIME + ZONES_SOURCE + r'''
import bmesh
from mathutils import Vector, Matrix, Quaternion, Euler
from mathutils.bvhtree import BVHTree

FA_EYE_TOKENS = ('eye', 'ojo', 'pupil', 'iris', 'eyeball')

def fa_object(name):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError('OBJECT_NOT_FOUND: ' + str(name))
    return obj

def fa_world_coords(obj, evaluated):
    if evaluated:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        ev = obj.evaluated_get(depsgraph)
        me = ev.to_mesh()
        try:
            co = [0.0] * (3 * len(me.vertices))
            me.vertices.foreach_get('co', co)
            matrix = ev.matrix_world.copy()
        finally:
            ev.to_mesh_clear()
    else:
        me = obj.data
        co = [0.0] * (3 * len(me.vertices))
        me.vertices.foreach_get('co', co)
        matrix = obj.matrix_world.copy()
    out = []
    for i in range(0, len(co), 3):
        out.append(matrix @ Vector((co[i], co[i + 1], co[i + 2])))
    return out

def fa_sphere_fit(points):
    n = len(points)
    if n < 12:
        return None
    center = Vector((0.0, 0.0, 0.0))
    for p in points:
        center += p
    center /= n
    distances = [(p - center).length for p in points]
    radius = sum(distances) / n
    if radius <= 1e-9:
        return None
    variance = sum((d - radius) ** 2 for d in distances) / n
    return {'center': center, 'radius': radius, 'fit_error': math.sqrt(variance) / radius}

def fa_bone_tokens(name):
    low = name.lower()
    out = []
    for token in ('head', 'neck', 'jaw', 'eye', 'lid', 'brow', 'lip', 'mouth', 'cheek', 'nose', 'tongue', 'chin', 'target'):
        if token in low:
            out.append(token)
    return out

def fa_side_of_name(name):
    low = name.lower()
    if low.endswith(('.l', '_l', '-l', 'left')) or '.l.' in low or 'left' in low:
        return 'LEFT'
    if low.endswith(('.r', '_r', '-r', 'right')) or '.r.' in low or 'right' in low:
        return 'RIGHT'
    return None

def fa_mesh_bounds(points):
    low = [min(p[k] for p in points) for k in range(3)]
    high = [max(p[k] for p in points) for k in range(3)]
    return low, high

def fa_boundary_loops(obj):
    me = obj.data
    counts = {}
    for poly in me.polygons:
        keys = poly.edge_keys
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    adjacency = {}
    for key, count in counts.items():
        if count == 1:
            a, b = key
            adjacency.setdefault(a, []).append(b)
            adjacency.setdefault(b, []).append(a)
    seen = set()
    loops = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        component = []
        stack = [start]
        seen.add(start)
        while stack:
            v = stack.pop()
            component.append(v)
            for w in adjacency[v]:
                if w not in seen:
                    seen.add(w)
                    stack.append(w)
        if len(component) >= 4:
            loops.append(component)
    return loops

def fa_set(params):
    """Resolve the facial set: armature, facial meshes, eyes, head bone, other meshes. Evidence recorded."""
    if bpy.context.mode != 'OBJECT':
        raise ValueError('MODE_NOT_SUPPORTED: facial tools require Object mode; the mode is never changed automatically')
    options = params.get('options') or {}
    obj = fa_object(params['object_name'])
    evidence = []
    armature = None
    if options.get('armature'):
        armature = fa_object(options['armature'])
        evidence.append('armature given explicitly')
    elif obj.type == 'ARMATURE':
        armature = obj
    elif obj.type == 'MESH':
        for m in obj.modifiers:
            if m.type == 'ARMATURE' and m.object is not None:
                armature = m.object
                evidence.append('armature from Armature modifier of ' + obj.name)
                break
        if armature is None and obj.parent is not None and obj.parent.type == 'ARMATURE':
            armature = obj.parent
            evidence.append('armature from parent of ' + obj.name)
    else:
        raise ValueError('FACIAL_OBJECT_UNSUPPORTED: give a facial mesh or its armature')
    if armature is not None and armature.type != 'ARMATURE':
        raise ValueError('FACIAL_OBJECT_UNSUPPORTED: armature option is not an armature')
    candidates = []
    if options.get('meshes'):
        candidates = [fa_object(n) for n in options['meshes']]
        evidence.append('meshes given explicitly')
    else:
        if obj.type == 'MESH':
            candidates.append(obj)
        if armature is not None:
            for o in bpy.context.scene.objects:
                if o.type != 'MESH' or o in candidates:
                    continue
                deformed = False
                for m in o.modifiers:
                    if m.type == 'ARMATURE' and m.object == armature:
                        deformed = True
                if o.parent == armature or deformed:
                    candidates.append(o)
    for o in candidates:
        if o.type != 'MESH':
            raise ValueError('FACIAL_OBJECT_UNSUPPORTED: ' + o.name + ' is not a mesh')
        if o.library or o.data.library or o.override_library:
            raise ValueError('LINKED_DATA_UNSUPPORTED: ' + o.name + ' is linked or overridden')
    rows = []
    for o in candidates:
        points = fa_world_coords(o, False)
        fit = fa_sphere_fit(points)
        low, high = fa_mesh_bounds(points) if points else ([0, 0, 0], [0, 0, 0])
        rows.append({'object': o, 'points': points, 'fit': fit, 'low': low, 'high': high,
                     'keys': len(o.data.shape_keys.key_blocks) - 1 if o.data.shape_keys else 0})
    size = 0.0
    for row in rows:
        size = max(size, max(row['high'][k] - row['low'][k] for k in range(3)) if row['points'] else 0.0)
    if obj.type == 'MESH' and rows and rows[0]['object'] == obj and rows[0]['points']:
        # Eye size is judged against the given facial mesh, not the whole body the rig also deforms
        # (measured: a 1.8 m body made 11.5 mm eyes fall below 2 % of the reference size).
        size = max(rows[0]['high'][k] - rows[0]['low'][k] for k in range(3))
        evidence.append('size reference: ' + obj.name + ' (' + format(size, '.3f') + ' m)')
    eye_rows = []
    if options.get('eyes'):
        for name in options['eyes']:
            row = [r for r in rows if r['object'].name == name]
            if not row:
                o = fa_object(name)
                pts = fa_world_coords(o, False)
                row = [{'object': o, 'points': pts, 'fit': fa_sphere_fit(pts), 'keys': 0}]
            eye_rows.append(row[0])
        evidence.append('eyes given explicitly')
    else:
        for row in rows:
            fit = row['fit']
            if fit is None or size <= 0:
                continue
            named = any(t in row['object'].name.lower() for t in FA_EYE_TOKENS)
            # Equal radial distance alone also fits rings and cylinders (measured: a 2-ring neck fits with error 0);
            # a sphere must additionally span its diameter on every axis.
            spans = [row['high'][k] - row['low'][k] for k in range(3)]
            round_box = all(abs(s - 2 * fit['radius']) <= 0.25 * 2 * fit['radius'] for s in spans)
            if fit['fit_error'] < 0.06 and round_box and 0.02 * size <= fit['radius'] <= 0.25 * size:
                eye_rows.append(row)
                evidence.append(row['object'].name + ': sphere fit error ' + format(fit['fit_error'], '.3f') +
                                ', radius ' + format(fit['radius'], '.4f') + ' m' + (', eye-like name' if named else ''))
    faces = [r for r in rows if r not in eye_rows]
    face_main = None
    best = -1
    for row in faces:
        score = len(row['points']) + 100000 * row['keys']
        if row['object'] == obj:
            score += 1
        if score > best:
            best, face_main = score, row
    return {'armature': armature, 'rows': rows, 'eyes': eye_rows, 'faces': faces, 'face': face_main,
            'size': size, 'evidence': evidence, 'options': options}

def fa_head_bone(armature, options, eyes):
    if armature is None:
        return None, 'no armature'
    if options.get('head_bone'):
        if options['head_bone'] not in armature.data.bones:
            raise ValueError('HEAD_BONE_NOT_FOUND: ' + options['head_bone'])
        return armature.data.bones[options['head_bone']], 'head bone given explicitly'
    named = [b for b in armature.data.bones if 'head' in b.name.lower() and 'target' not in b.name.lower()]
    if eyes:
        mid = (eyes[0]['fit']['center'] + eyes[-1]['fit']['center']) / 2
        best, best_d = None, 1e30
        for b in (named or list(armature.data.bones)):
            head = armature.matrix_world @ b.head_local
            tail = armature.matrix_world @ b.tail_local
            seg = tail - head
            t = 0.0
            if seg.length_squared > 1e-12:
                t = max(0.0, min(1.0, (mid - head).dot(seg) / seg.length_squared))
            d = (head + seg * t - mid).length
            if d < best_d:
                best, best_d = b, d
        if best is not None:
            return best, ('bone named like head nearest the eyes' if named else 'bone nearest the eye midpoint (no head name)')
    if named:
        return named[0], 'bone named like head'
    return None, 'no head bone evidence'

def fa_frame(fs, head):
    """Face frame from eyes (+ head bone axis for up). Returns frame tuple and evidence."""
    armature = fs['armature']
    eyes = fs['eyes']
    face = fs['face']
    notes = []
    if len(eyes) < 2:
        raise ValueError('EYES_NOT_FOUND: no pair of eye spheres; pass options.eyes to name them')
    a = eyes[0]['fit']['center']
    b = eyes[1]['fit']['center']
    if head is not None:
        up = (armature.matrix_world.to_3x3() @ (head.tail_local - head.head_local)).normalized()
        notes.append('up from head bone ' + head.name)
    else:
        up = Vector((0.0, 0.0, 1.0))
        notes.append('up = world +Z (no head bone)')
    centre = Vector((0.0, 0.0, 0.0))
    for p in face['points']:
        centre += p
    centre /= max(1, len(face['points']))
    mid = (a + b) / 2
    forward = mid - centre
    forward = forward - up * forward.dot(up)
    if forward.length < 1e-9:
        raise ValueError('FACE_FRAME_UNDEFINED: eyes are not in front of the face centre')
    forward = forward.normalized()
    left = up.cross(forward).normalized()
    if (a - b).dot(left) < 0:
        a, b = b, a
        eyes.reverse()
    across = a - b
    iod = across.length
    if iod <= 1e-6:
        raise ValueError('FACE_FRAME_UNDEFINED: eye centres coincide')
    zm = -1.25
    mw = 0.32
    mouth_note = 'default mouth proportion (no mouth opening found): low confidence'
    loops = fa_boundary_loops(face['object'])
    best = None
    for loop in loops:
        pts = [face['points'][i] for i in loop]
        local = [fz_frame_point(p, mid, left, up, forward, iod) for p in pts]
        cx = sum(q[0] for q in local) / len(local)
        cz = sum(q[2] for q in local) / len(local)
        width = max(q[0] for q in local) - min(q[0] for q in local)
        if cz < -0.6 and abs(cx) < 0.4 and (best is None or width > best[2]):
            best = (cx, cz, width)
    if best is not None:
        zm, mw = best[1], best[2] / 2
        mouth_note = 'mouth from boundary loop below the eyes'
    notes.append(mouth_note)
    return (mid, left, up, forward, iod, zm, mw), notes, best is not None

def fa_frame_public(frame, notes, mouth_found):
    mid, left, up, forward, iod, zm, mw = frame
    return {'origin': [round(v, 5) for v in mid], 'left': [round(v, 4) for v in left], 'up': [round(v, 4) for v in up],
            'forward': [round(v, 4) for v in forward], 'iod_m': round(iod, 5), 'mouth_z': round(zm, 4),
            'mouth_half_width': round(mw, 4), 'mouth_found': mouth_found, 'evidence': notes}

def fa_threshold(frame):
    return max(1e-5, frame[4] * 0.002)

def fa_key_rows(obj):
    key = obj.data.shape_keys
    if key is None:
        return []
    rows = []
    drivers = set()
    if key.animation_data is not None:
        for d in key.animation_data.drivers:
            drivers.add(d.data_path)
    for index, block in enumerate(key.key_blocks):
        rows.append({'name': block.name, 'index': index, 'block': block,
                     'relative': block.relative_key.name if block.relative_key else None,
                     'basis': block == key.reference_key, 'value': block.value, 'mute': block.mute,
                     'slider_min': block.slider_min, 'slider_max': block.slider_max,
                     'driven': ('key_blocks["' + block.name + '"].value') in drivers,
                     'vertex_group': block.vertex_group})
    return rows

def fa_probe_key(obj, block, frame):
    ref = block.relative_key
    co = [0.0] * (3 * len(block.data))
    rc = [0.0] * (3 * len(ref.data))
    block.data.foreach_get('co', co)
    ref.data.foreach_get('co', rc)
    m3 = obj.matrix_world.to_3x3()
    mw = obj.matrix_world
    points, vectors = [], []
    for i in range(0, len(co), 3):
        d = Vector((co[i] - rc[i], co[i + 1] - rc[i + 1], co[i + 2] - rc[i + 2]))
        if d.length_squared <= 0:
            continue
        points.append(mw @ Vector((rc[i], rc[i + 1], rc[i + 2])))
        vectors.append(m3 @ d)
    return fz_field_stats(points, vectors, frame, fa_threshold(frame))

def fa_set_channel(pb, path, index, value):
    if path == 'location':
        pb.location[index] = value
    elif path == 'rotation_euler':
        pb.rotation_euler[index] = value
    elif path == 'rotation_quaternion':
        pb.rotation_quaternion[index] = value
    elif path == 'scale':
        pb.scale[index] = value
    else:
        raise ValueError('CHANNEL_UNSUPPORTED: ' + path)

def fa_get_channel(pb, path, index):
    if path == 'location':
        return pb.location[index]
    if path == 'rotation_euler':
        return pb.rotation_euler[index]
    if path == 'rotation_quaternion':
        return pb.rotation_quaternion[index]
    if path == 'scale':
        return pb.scale[index]
    raise ValueError('CHANNEL_UNSUPPORTED: ' + path)

def fa_limits(pb):
    """Local-space Limit constraints of a pose bone as {('rotation'|'location', axis): (min, max)}."""
    out = {}
    for c in pb.constraints:
        if c.mute or c.influence <= 0:
            continue
        if c.type == 'LIMIT_ROTATION' and c.owner_space == 'LOCAL':
            for axis, i in (('x', 0), ('y', 1), ('z', 2)):
                if axis == 'x' and c.use_limit_x:
                    out[('rotation', i)] = (c.min_x, c.max_x)
                if axis == 'y' and c.use_limit_y:
                    out[('rotation', i)] = (c.min_y, c.max_y)
                if axis == 'z' and c.use_limit_z:
                    out[('rotation', i)] = (c.min_z, c.max_z)
        if c.type == 'LIMIT_LOCATION' and c.owner_space == 'LOCAL':
            pairs = ((0, c.use_min_x, c.min_x, c.use_max_x, c.max_x), (1, c.use_min_y, c.min_y, c.use_max_y, c.max_y),
                     (2, c.use_min_z, c.min_z, c.use_max_z, c.max_z))
            for i, use_min, low, use_max, high in pairs:
                if use_min or use_max:
                    out[('location', i)] = (low if use_min else -1e9, high if use_max else 1e9)
    return out

def fa_constraint_rows(armature):
    """Who drives whom inside the armature (copy/track constraints), plus drivers on bones."""
    driven_by = {}
    rows = {}
    for pb in armature.pose.bones:
        cons = []
        for c in pb.constraints:
            target = getattr(c, 'subtarget', '') if hasattr(c, 'subtarget') else ''
            same = hasattr(c, 'target') and c.target == armature
            cons.append({'type': c.type, 'subtarget': target if same else None, 'mute': c.mute,
                         'influence': c.influence})
            if same and target and c.type in ('COPY_TRANSFORMS', 'COPY_ROTATION', 'COPY_LOCATION', 'DAMPED_TRACK',
                                              'TRACK_TO', 'LOCKED_TRACK', 'CHILD_OF', 'IK') and not c.mute:
                driven_by.setdefault(pb.name, []).append({'by': target, 'type': c.type})
        rows[pb.name] = cons
    bone_drivers = []
    if armature.animation_data is not None:
        for d in armature.animation_data.drivers:
            if d.data_path.startswith('pose.bones['):
                bone_drivers.append({'path': d.data_path, 'index': d.array_index})
    return rows, driven_by, bone_drivers

def fa_descendants(bone):
    out = []
    stack = list(bone.children)
    while stack:
        b = stack.pop()
        out.append(b)
        stack.extend(b.children)
    return out

def fa_eye_rotation(eye_obj, rest_matrix, frame):
    """Rotation of an eye object relative to its rest, as yaw/pitch degrees in the face frame."""
    mid, left, up, forward, iod, zm, mw = frame
    delta = eye_obj.matrix_world.to_3x3().normalized() @ rest_matrix.to_3x3().normalized().inverted()
    f2 = delta @ forward
    yaw = math.degrees(math.atan2(f2.dot(left), f2.dot(forward)))
    pitch = math.degrees(math.atan2(f2.dot(up), math.sqrt(f2.dot(left) ** 2 + f2.dot(forward) ** 2)))
    return yaw, pitch

def fa_eye_points_rotation(points_rest, points_now, center, frame):
    """Gaze change of an eyeball mesh from vertex motion (shape-key eyes): rotation of the front pole."""
    mid, left, up, forward, iod, zm, mw = frame
    best, best_dot = None, -2.0
    for i in range(len(points_rest)):
        v = points_rest[i] - center
        if v.length <= 0:
            continue
        d = v.normalized().dot(forward)
        if d > best_dot:
            best, best_dot = i, d
    if best is None:
        return 0.0, 0.0
    f2 = (points_now[best] - center).normalized()
    yaw = math.degrees(math.atan2(f2.dot(left), f2.dot(forward)))
    pitch = math.degrees(math.atan2(f2.dot(up), math.sqrt(f2.dot(left) ** 2 + f2.dot(forward) ** 2)))
    return yaw, pitch

def fa_rest_snapshot(fs):
    rest = {}
    for row in fs['faces'] + fs['eyes']:
        rest[row['object'].name] = fa_world_coords(row['object'], True)
    eyes = {row['object'].name: row['object'].matrix_world.copy() for row in fs['eyes']}
    return rest, eyes

def fa_probe_pose(fs, frame, rest, eye_rest, armature):
    """Displacement statistics of the current (modified) state against the rest snapshot."""
    bpy.context.view_layer.update()
    points, vectors = [], []
    per_mesh = {}
    for row in fs['faces']:
        name = row['object'].name
        now = fa_world_coords(row['object'], True)
        base = rest[name]
        moved = 0
        for i in range(min(len(now), len(base))):
            d = now[i] - base[i]
            if d.length_squared > 0:
                points.append(base[i])
                vectors.append(d)
                moved += 1
        per_mesh[name] = moved
    stats = fz_field_stats(points, vectors, frame, fa_threshold(frame))
    gaze = {}
    for row in fs['eyes']:
        o = row['object']
        yaw, pitch = fa_eye_rotation(o, eye_rest[o.name], frame)
        if abs(yaw) < 0.05 and abs(pitch) < 0.05 and o.data.shape_keys is not None:
            now = fa_world_coords(o, True)
            yaw, pitch = fa_eye_points_rotation(rest[o.name], now, row['fit']['center'], frame)
        gaze[o.name] = [round(yaw, 3), round(pitch, 3)]
    stats['eye_rotation_deg'] = gaze
    stats['meshes'] = per_mesh
    return stats

def fa_probe_rig(fs, frame, head):
    """Probe every animatable channel of facial control bones within their limits."""
    armature = fs['armature']
    if armature is None:
        return [], {}, {}
    cons, driven_by, bone_drivers = fa_constraint_rows(armature)
    facial = []
    if head is not None:
        facial = fa_descendants(head)
    driven_paths = set(d['path'] for d in bone_drivers)
    rest, eye_rest = fa_rest_snapshot(fs)
    state = m3_capture(armature)
    probes = []
    try:
        for bone in facial:
            pb = armature.pose.bones[bone.name]
            limits = fa_limits(pb)
            channels = []
            mode = pb.rotation_mode
            for i in range(3):
                if not pb.lock_location[i]:
                    channels.append(('location', i))
            if mode in ('XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'):
                for i in range(3):
                    if not pb.lock_rotation[i]:
                        channels.append(('rotation_euler', i))
            for path, index in channels:
                kind = 'rotation' if path.startswith('rotation') else 'location'
                limit = limits.get((kind, index))
                if limit is not None and abs(limit[1] - limit[0]) < 1e-9:
                    continue
                if kind == 'rotation':
                    amounts = (-0.21, 0.21) if limit is None else (limit[0], limit[1])
                else:
                    step = frame[4] * 0.10
                    amounts = (-step, step) if limit is None else (max(limit[0], -frame[4]), min(limit[1], frame[4]))
                rest_value = fa_get_channel(pb, path, index)
                for amount in amounts:
                    if abs(amount) < 1e-9:
                        continue
                    fa_set_channel(pb, path, index, rest_value + amount)
                    stats = fa_probe_pose(fs, frame, rest, eye_rest, armature)
                    fa_set_channel(pb, path, index, rest_value)
                    if stats.get('moved_vertices', 0) == 0 and not any(abs(v[0]) + abs(v[1]) > 0.1 for v in stats['eye_rotation_deg'].values()):
                        continue
                    probes.append({'bone': bone.name, 'path': path, 'index': index, 'amount': amount,
                                   'deform': bone.use_deform, 'limit': list(limit) if limit else None,
                                   'driven_by': driven_by.get(bone.name, []),
                                   'driver_on_channel': ('pose.bones["' + bone.name + '"].' + path) in driven_paths,
                                   'stats': stats})
    finally:
        m3_restore(armature, state)
    return probes, cons, driven_by

def fa_probe_keys(fs, frame):
    out = []
    for row in fs['faces'] + fs['eyes']:
        o = row['object']
        for key in fa_key_rows(o):
            if key['basis']:
                continue
            if row in fs['eyes']:
                rest = fa_world_coords(o, False)
                co = [0.0] * (3 * len(key['block'].data))
                key['block'].data.foreach_get('co', co)
                now = [o.matrix_world @ Vector((co[i], co[i + 1], co[i + 2])) for i in range(0, len(co), 3)]
                yaw, pitch = fa_eye_points_rotation(rest, now, row['fit']['center'], frame)
                stats = {'moved_vertices': len(now), 'eye_rotation_deg': {o.name: [round(yaw, 3), round(pitch, 3)]}, 'zones': {}}
            else:
                stats = fa_probe_key(o, key['block'], frame)
            out.append({'mesh': o.name, 'key': key['name'], 'value': key['value'], 'slider_min': key['slider_min'],
                        'slider_max': key['slider_max'], 'relative': key['relative'], 'driven': key['driven'],
                        'mute': key['mute'], 'vertex_group': key['vertex_group'], 'eye_mesh': row in fs['eyes'],
                        'stats': stats})
    return out

def fa_aims(fs):
    """Eye bones that aim at a target control (Damped/Track To): gaze is driven by placing the target."""
    armature = fs['armature']
    out = []
    if armature is None:
        return out
    for index, row in enumerate(fs['eyes']):
        side = 'L' if index == 0 else 'R'
        o = row['object']
        if o.parent != armature or o.parent_type != 'BONE' or not o.parent_bone:
            continue
        pb = armature.pose.bones.get(o.parent_bone)
        if pb is None:
            continue
        chain = [pb]
        for c in pb.constraints:
            if c.type in ('COPY_TRANSFORMS', 'COPY_ROTATION') and c.target == armature and c.subtarget:
                chain.append(armature.pose.bones[c.subtarget])
        for bone in chain:
            for c in bone.constraints:
                if c.type in ('DAMPED_TRACK', 'TRACK_TO', 'LOCKED_TRACK') and not c.mute and c.target == armature and c.subtarget:
                    tb = armature.data.bones[c.subtarget]
                    world = armature.matrix_world @ tb.head_local
                    m = armature.matrix_world.to_3x3() @ tb.matrix_local.to_3x3()
                    out.append({'side': side, 'eye_bone': bone.name, 'eye_object': o.name, 'target_bone': c.subtarget,
                                'target_rest_world': [round(v, 6) for v in world],
                                'target_axes_world': [[round(v, 6) for v in m.col[0]], [round(v, 6) for v in m.col[1]],
                                                      [round(v, 6) for v in m.col[2]]],
                                'eye_center': [round(v, 6) for v in row['fit']['center']],
                                'distance_m': round((world - row['fit']['center']).length, 6), 'constraint': c.type})
    return out

def fa_rig_fingerprint(fs):
    """Digest of everything a facial plan depends on: bones, constraints, limits, keys, eyes, drivers."""
    rows = []
    armature = fs['armature']
    if armature is not None:
        for b in armature.data.bones:
            pb = armature.pose.bones[b.name]
            rows.append([b.name, b.parent.name if b.parent else None, [round(v, 6) for v in b.head_local],
                         [round(v, 6) for v in b.tail_local], round(b.roll if hasattr(b, 'roll') else 0.0, 6), b.use_deform,
                         pb.rotation_mode, [c.type + ':' + str(getattr(c, 'subtarget', '')) + ':' + str(c.mute) for c in pb.constraints],
                         sorted([k[0] + str(k[1]), round(v[0], 6), round(v[1], 6)] for k, v in fa_limits(pb).items())])
        if armature.animation_data is not None:
            rows.append(sorted(d.data_path + str(d.array_index) + d.driver.expression for d in armature.animation_data.drivers))
    for row in fs['faces'] + fs['eyes']:
        o = row['object']
        rows.append([o.name, len(o.data.vertices), len(o.data.polygons), [round(v, 6) for v in o.matrix_world.translation]])
        key = o.data.shape_keys
        if key is not None:
            for block in key.key_blocks:
                co = [0.0] * (3 * len(block.data))
                block.data.foreach_get('co', co)
                rows.append([block.name, block.relative_key.name if block.relative_key else None, block.slider_min,
                             block.slider_max, block.mute, m2_digest([round(v, 6) for v in co])])
            if key.animation_data is not None:
                rows.append(sorted(d.data_path + d.driver.expression for d in key.animation_data.drivers))
    return m2_digest(rows)

def fa_basis_offset(obj):
    """Largest distance between the Basis key and the mesh vertices (they must coincide in Object mode)."""
    key = obj.data.shape_keys
    if key is None:
        return None
    basis = key.reference_key
    n = len(obj.data.vertices)
    a = [0.0] * (3 * n)
    b = [0.0] * (3 * n)
    obj.data.vertices.foreach_get('co', a)
    basis.data.foreach_get('co', b)
    worst = 0.0
    for i in range(0, 3 * n, 3):
        worst = max(worst, math.sqrt((a[i] - b[i]) ** 2 + (a[i + 1] - b[i + 1]) ** 2 + (a[i + 2] - b[i + 2]) ** 2))
    return worst

def fa_bone_info(armature, bone):
    """Rest axes (world) and rotation mode: head/neck rotations are converted into this bone's local space."""
    if armature is None or bone is None:
        return None
    pb = armature.pose.bones[bone.name]
    m = armature.matrix_world.to_3x3() @ bone.matrix_local.to_3x3()
    m.normalize()
    return {'name': bone.name, 'rotation_mode': pb.rotation_mode,
            'axes_world': [[round(v, 6) for v in m.col[0]], [round(v, 6) for v in m.col[1]], [round(v, 6) for v in m.col[2]]],
            'locked': [bool(v) for v in pb.lock_rotation], 'deform': bone.use_deform}

def fa_inspect(params):
    fs = fa_set(params)
    head, head_note = fa_head_bone(fs['armature'], fs['options'], fs['eyes'])
    frame, notes, mouth_found = fa_frame(fs, head)
    armature = fs['armature']
    meshes = []
    for row in fs['rows']:
        o = row['object']
        key = o.data.shape_keys
        meshes.append({'name': o.name, 'role': 'eye' if row in fs['eyes'] else ('face' if row is fs['face'] else 'other'),
                       'vertices': len(o.data.vertices), 'shape_keys': row['keys'],
                       'shape_keys_relative': bool(key.use_relative) if key else None,
                       'basis_offset_m': round(fa_basis_offset(o), 7) if key else None,
                       'basis_muted': bool(key.reference_key.mute) if key else None,
                       'key_action': key.animation_data.action.name if key and key.animation_data and key.animation_data.action else None,
                       'armature_modifier': any(m.type == 'ARMATURE' for m in o.modifiers),
                       'parent': o.parent.name if o.parent else None, 'parent_bone': o.parent_bone or None,
                       'materials': [m.name for m in o.data.materials if m]})
    eyes = []
    for row in fs['eyes']:
        fit = row['fit']
        eyes.append({'object': row['object'].name, 'center': [round(v, 5) for v in fit['center']],
                     'radius_m': round(fit['radius'], 5), 'fit_error': round(fit['fit_error'], 4),
                     'parent': row['object'].parent.name if row['object'].parent else None,
                     'parent_bone': row['object'].parent_bone or None,
                     'look_shape_keys': row['keys']})
    bones = []
    action = None
    channels_by_bone = {}
    if armature is not None:
        ad = armature.animation_data
        action = ad.action if ad else None
        if action is not None:
            for slot_h, layer, strip, curve in action_curves(action, None):
                path = curve.data_path
                if path.startswith('pose.bones["'):
                    bone = path.split('"')[1]
                    channels_by_bone[bone] = channels_by_bone.get(bone, 0) + 1
        cons, driven_by, bone_drivers = fa_constraint_rows(armature)
        facial = fa_descendants(head) if head is not None else []
        for b in facial:
            pb = armature.pose.bones[b.name]
            limits = fa_limits(pb)
            bones.append({'name': b.name, 'deform': b.use_deform, 'parent': b.parent.name if b.parent else None,
                          'rotation_mode': pb.rotation_mode, 'constraints': cons.get(b.name, []),
                          'driven_by': driven_by.get(b.name, []),
                          'limits': [[k[0], k[1], v[0], v[1]] for k, v in limits.items()],
                          'tokens': fa_bone_tokens(b.name), 'side_name': fa_side_of_name(b.name),
                          'animated_channels': channels_by_bone.get(b.name, 0),
                          'custom_properties': [k for k in pb.keys() if not k.startswith('_')]})
    keys = []
    for row in fs['faces'] + fs['eyes']:
        for k in fa_key_rows(row['object']):
            keys.append({'mesh': row['object'].name, 'name': k['name'], 'basis': k['basis'], 'value': round(k['value'], 5),
                         'relative': k['relative'], 'driven': k['driven'], 'mute': k['mute'],
                         'slider': [k['slider_min'], k['slider_max']]})
    nla = []
    if armature is not None and armature.animation_data is not None:
        for track in armature.animation_data.nla_tracks:
            nla.append({'name': track.name, 'mute': track.mute, 'strips': [s.name for s in track.strips]})
    head_name = head.name if head is not None else None
    neck = None
    if head is not None and head.parent is not None:
        neck = head.parent.name
    warnings = []
    active = [k for k in keys if not k['basis'] and abs(k['value']) > 1e-6]
    if active:
        warnings.append('SHAPE_KEYS_ACTIVE_AT_REST: ' + str(len(active)) + ' shape keys are not 0 in the rest state')
    for m in meshes:
        if m['shape_keys_relative'] is False:
            warnings.append('ABSOLUTE_SHAPE_KEYS: ' + m['name'] + ' uses absolute (non-relative) keys; facial animation is unsupported')
        if m['basis_offset_m'] is not None and (m['basis_offset_m'] > 1e-5 or m['basis_muted']):
            warnings.append('BASIS_INVALID: ' + m['name'] + ' Basis differs from the mesh by ' + format(m['basis_offset_m'] * 1000, '.3f') +
                            ' mm' + (' and is muted' if m['basis_muted'] else ''))
    return {'armature': armature.name if armature else None, 'head_bone': head_name, 'head_evidence': head_note,
            'neck_bone': neck, 'face_mesh': fs['face']['object'].name if fs['face'] else None,
            'meshes': meshes, 'eyes': eyes, 'frame': fa_frame_public(frame, notes, mouth_found),
            'set_evidence': fs['evidence'], 'facial_bones': bones, 'shape_keys': keys,
            'body_action': action.name if action else None, 'body_animated_bones': sorted(channels_by_bone),
            'nla_tracks': nla, 'warnings': warnings,
            'action_blend_type': armature.animation_data.action_blend_type if armature and armature.animation_data else None}
'''

INSPECT = FACIAL_RUNTIME + r'''
result = fa_inspect(params)
'''

PROBE = FACIAL_RUNTIME + r'''
fs = fa_set(params)
head, head_note = fa_head_bone(fs['armature'], fs['options'], fs['eyes'])
frame, notes, mouth_found = fa_frame(fs, head)
bone_probes, cons, driven_by = fa_probe_rig(fs, frame, head)
key_probes = fa_probe_keys(fs, frame)
result = {'frame': fa_frame_public(frame, notes, mouth_found), 'head_bone': head.name if head else None,
          'bone_probes': bone_probes, 'key_probes': key_probes,
          'armature': fs['armature'].name if fs['armature'] else None,
          'eyes': [r['object'].name for r in fs['eyes']], 'face_mesh': fs['face']['object'].name if fs['face'] else None,
          'aims': fa_aims(fs), 'rig_fingerprint': fa_rig_fingerprint(fs),
          'head_info': fa_bone_info(fs['armature'], head),
          'neck_info': fa_bone_info(fs['armature'], head.parent if head is not None else None)}
'''
