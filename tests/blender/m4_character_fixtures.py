"""Controlled synthetic character fixtures for M4 (no production assets, no external files).

A character is a procedural skeleton turned into one connected quad mesh with Blender's Skin +
Subdivision modifiers (applied), then given: an armature with named bones, deterministic
inverse-distance skin weights, an Armature modifier, two UV layers, three material slots and
explicit region vertex groups (FACE, HEAD, SHOULDERS, UPPER_BACK, HANDS, ...).

Variants (M4 phase 25 fixture list):
    humanoid           1 basic humanoid, 7 locked face, 8 editable shoulders, 9 armature modifier,
                       10 vertex groups, 12 multiple material slots, 13 UV layers
    humanoid_keys      11 shape keys (Smile on the face, Breath on the chest)
    humanoid_normals   14 custom normals + sharp edges
    shoulder_defect    4 shoulder transition step
    pinched_torso      5 pinched waist ring
    noisy_patch        6 noisy upper-back patch (seeded)
    self_intersection  15 left forearm pushed into the torso
    creature           2 asymmetric creature, 3 oversized right arm, back hump, shorter left leg
    dense_humanoid     humanoid with midpoint joints at subdivision 4 (~100-150k triangles, benchmark)

The character faces -Y, +Z is up, T-pose arms along X. Units are meters.
"""
import math
from pathlib import Path
import random

import bpy
from mathutils import Vector

# name: (location, skin radius x, skin radius y)
HUMANOID = {
    'pelvis': ((0, 0, 0.95), 0.14, 0.10), 'spine': ((0, 0, 1.15), 0.13, 0.09), 'chest': ((0, 0, 1.35), 0.16, 0.11),
    'neck': ((0, 0, 1.55), 0.055, 0.055), 'head': ((0, 0, 1.68), 0.10, 0.11), 'head_top': ((0, 0, 1.83), 0.07, 0.08),
    'shoulder.L': ((0.20, 0, 1.45), 0.07, 0.07), 'elbow.L': ((0.46, 0, 1.45), 0.05, 0.05),
    'wrist.L': ((0.70, 0, 1.45), 0.04, 0.035), 'hand.L': ((0.80, 0, 1.45), 0.045, 0.02),
    'hip.L': ((0.09, 0, 0.90), 0.08, 0.08), 'knee.L': ((0.10, 0, 0.50), 0.06, 0.06),
    'ankle.L': ((0.10, 0, 0.08), 0.045, 0.045), 'toe.L': ((0.10, -0.14, 0.03), 0.04, 0.03),
}
EDGES = [('pelvis', 'spine'), ('spine', 'chest'), ('chest', 'neck'), ('neck', 'head'), ('head', 'head_top'),
         ('chest', 'shoulder.L'), ('shoulder.L', 'elbow.L'), ('elbow.L', 'wrist.L'), ('wrist.L', 'hand.L'),
         ('pelvis', 'hip.L'), ('hip.L', 'knee.L'), ('knee.L', 'ankle.L'), ('ankle.L', 'toe.L')]
# bone name: (head joint, tail joint)
BONES = {'pelvis': ('pelvis', 'spine'), 'spine': ('spine', 'chest'), 'chest': ('chest', 'neck'), 'neck': ('neck', 'head'),
         'head': ('head', 'head_top'), 'shoulder.L': ('chest', 'shoulder.L'), 'upper_arm.L': ('shoulder.L', 'elbow.L'),
         'forearm.L': ('elbow.L', 'wrist.L'), 'hand.L': ('wrist.L', 'hand.L'), 'hip.L': ('pelvis', 'hip.L'),
         'thigh.L': ('hip.L', 'knee.L'), 'shin.L': ('knee.L', 'ankle.L'), 'foot.L': ('ankle.L', 'toe.L')}
PARENTS = {'spine': 'pelvis', 'chest': 'spine', 'neck': 'chest', 'head': 'neck', 'shoulder.L': 'chest',
           'upper_arm.L': 'shoulder.L', 'forearm.L': 'upper_arm.L', 'hand.L': 'forearm.L', 'hip.L': 'pelvis',
           'thigh.L': 'hip.L', 'shin.L': 'thigh.L', 'foot.L': 'shin.L'}


def mirrored(table):
    out = {}
    for name, value in table.items():
        out[name] = value
        if name.endswith('.L'):
            (x, y, z), rx, ry = value
            out[name[:-2] + '.R'] = ((-x, y, z), rx, ry)
    return out


def mirror_pairs(pairs):
    out = list(pairs)
    for a, b in pairs:
        if a.endswith('.L') or b.endswith('.L'):
            out.append((a.replace('.L', '.R'), b.replace('.L', '.R')))
    return out


def skeleton(variant):
    joints = mirrored(HUMANOID)
    edges = mirror_pairs(EDGES)
    if variant == 'creature':
        # Oversized right arm (radius x2.2, 25 % longer), hump on the left upper back, shorter left leg.
        for name, factor in (('shoulder.R', 2.0), ('elbow.R', 2.2), ('wrist.R', 2.3), ('hand.R', 2.0)):
            (x, y, z), rx, ry = joints[name]
            joints[name] = ((x * 1.25 - 0.05, y, z - 0.03), rx * factor, ry * factor)
        joints['hump'] = ((0.10, 0.14, 1.40), 0.11, 0.09)
        edges.append(('chest', 'hump'))
        for name in ('knee.L', 'ankle.L', 'toe.L'):
            (x, y, z), rx, ry = joints[name]
            joints[name] = ((x, y, 0.95 - (0.95 - z) * 0.85), rx, ry)
    if variant == 'dense_humanoid':
        # Performance fixture: one midpoint joint per skeleton edge doubles the skin segments, so
        # subdivision level 4 lands in the requested 100k-150k triangle range (level 4 alone: ~93k).
        for a, b in list(edges):
            (pa, ra, sa), (pb, rb, sb) = joints[a], joints[b]
            middle = 'mid_' + a + '_' + b
            joints[middle] = (tuple((u + v) / 2 for u, v in zip(pa, pb)), (ra + rb) / 2, (sa + sb) / 2)
            edges.remove((a, b))
            edges.extend([(a, middle), (middle, b)])
    if variant == 'self_intersection':
        # Left forearm folded forward so the hand enters the chest.
        joints['elbow.L'] = ((0.30, -0.12, 1.30), 0.05, 0.05)
        joints['wrist.L'] = ((0.08, -0.10, 1.28), 0.04, 0.035)
        joints['hand.L'] = ((0.0, -0.05, 1.28), 0.045, 0.02)
    return joints, edges


def skin_mesh(name, joints, edges, levels):
    names = list(joints)
    index = {n: i for i, n in enumerate(names)}
    mesh = bpy.data.meshes.new(name + '_skeleton')
    mesh.from_pydata([joints[n][0] for n in names], [(index[a], index[b]) for a, b in edges], [])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.modifiers.new('Skin', 'SKIN')
    for n in names:
        mesh.skin_vertices[0].data[index[n]].radius = (joints[n][1], joints[n][2])
    mesh.skin_vertices[0].data[index['pelvis']].use_root = True
    subdivision = obj.modifiers.new('Subdivision', 'SUBSURF')
    subdivision.levels = levels
    subdivision.render_levels = levels
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    body = bpy.data.meshes.new_from_object(evaluated)
    obj.modifiers.clear()
    obj.data = body
    bpy.data.meshes.remove(mesh)
    body.name = name + 'Mesh'
    for polygon in body.polygons:
        polygon.use_smooth = True
    return obj


def armature(name, joints):
    data = bpy.data.armatures.new(name + 'Data')
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    bones = {}
    table = dict(BONES)
    for bone, (head, tail) in list(BONES.items()):
        if bone.endswith('.L'):
            table[bone[:-2] + '.R'] = (head.replace('.L', '.R'), tail.replace('.L', '.R'))
    for bone, (head, tail) in table.items():
        edit = data.edit_bones.new(bone)
        edit.head = joints[head][0]
        edit.tail = joints[tail][0]
        bones[bone] = edit
    for bone in table:
        parent = PARENTS.get(bone.replace('.R', '.L'))
        if parent:
            bones[bone].parent = bones[parent.replace('.L', '.R') if bone.endswith('.R') else parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    return rig, {bone: (Vector(joints[h][0]), Vector(joints[t][0])) for bone, (h, t) in table.items()}


def segment_distance(p, a, b):
    ab = b - a
    t = max(0.0, min(1.0, (p - a).dot(ab) / max(ab.length_squared, 1e-12)))
    return (p - (a + ab * t)).length


def mirror_name(bone):
    return bone[:-2] + ('.R' if bone.endswith('.L') else '.L') if bone.endswith(('.L', '.R')) else bone


def skin_weights(body, rig, segments, symmetric):
    groups = {bone: body.vertex_groups.new(name=bone) for bone in segments}
    for vertex in body.data.vertices:
        point = vertex.co.copy()
        flip = False
        if symmetric:
            # Weights of a symmetric character are computed on the +X side and mirrored, and centre-line
            # vertices use x = 0 exactly: otherwise float noise and name order break L/R ties unevenly.
            if abs(point.x) <= 1e-5:
                point.x = 0.0
            elif point.x < 0:
                point.x, flip = -point.x, True
        ranked = sorted((segment_distance(point, a, b), bone) for bone, (a, b) in segments.items())[:2]
        if symmetric and point.x == 0.0:
            # An exact L/R tie keeps both sides with equal weight.
            nearest = ranked[0][0]
            ranked = [row for row in sorted((segment_distance(point, a, b), bone) for bone, (a, b) in segments.items())
                      if row[0] - nearest <= 1e-9 or row in ranked]
        raw = [(1.0 / (d ** 4 + 1e-9), mirror_name(bone) if flip else bone) for d, bone in ranked]
        total = sum(w for w, _ in raw)
        for w, bone in raw:
            groups[bone].add([vertex.index], w / total, 'REPLACE')
    modifier = body.modifiers.new('Armature', 'ARMATURE')
    modifier.object = rig
    body.parent = rig


def uv_layers(body):
    mesh = body.data
    corners = [0] * len(mesh.loops)
    mesh.loops.foreach_get('vertex_index', corners)
    points = [mesh.vertices[i].co for i in corners]
    height = max(p.z for p in points) or 1.0
    main = mesh.uv_layers.new(name='UVMap')
    values = []
    for p in points:
        values.extend(((math.atan2(p.x, -p.y) / (2 * math.pi)) + 0.5, p.z / height))
    main.uv.foreach_set('vector', values)
    detail = mesh.uv_layers.new(name='UVDetail')
    values = []
    for p in points:
        values.extend((p.x + 0.5, p.z / height))
    detail.uv.foreach_set('vector', values)
    main.active = True
    main.active_render = True


def materials(body):
    mesh = body.data
    for name, color in (('Skin', (0.8, 0.6, 0.5, 1)), ('Armor', (0.3, 0.35, 0.4, 1)), ('Cloth', (0.2, 0.25, 0.2, 1))):
        material = bpy.data.materials.new(name)
        material.diffuse_color = color
        mesh.materials.append(material)
    for polygon in mesh.polygons:
        c = polygon.center
        polygon.material_index = 0 if (c.z > 1.58 or abs(c.x) > 0.66) else 1 if c.z > 0.92 else 2


def region_groups(body, variant):
    mesh = body.data
    rules = {
        'FACE': lambda p: p.z > 1.60 and p.y < -0.02,
        'HEAD': lambda p: p.z > 1.56,
        'SHOULDERS': lambda p: 1.36 < p.z < 1.56 and 0.10 < abs(p.x) < 0.32,
        'UPPER_BACK': lambda p: 1.22 < p.z < 1.52 and p.y > 0.0 and abs(p.x) < 0.20,
        'HANDS': lambda p: abs(p.x) > 0.66 and p.z > 1.3,
        'TORSO': lambda p: 0.92 < p.z < 1.52 and abs(p.x) < 0.20,
    }
    if variant == 'creature':
        rules['MUTATED_ARM'] = lambda p: p.x < -0.22 and p.z > 1.2
        rules['LEFT_SHOULDER_TORSO'] = lambda p: 1.1 < p.z < 1.56 and -0.05 < p.x < 0.32 and not (p.y > 0.06 and p.x > 0.0 and p.z > 1.3)
        rules['HUMP'] = lambda p: p.y > 0.06 and p.x > 0.0 and 1.28 < p.z < 1.52
        rules['HANDS'] = lambda p: p.x > 0.66 and p.z > 1.3
    for name, rule in rules.items():
        group = body.vertex_groups.new(name=name)
        members = [v.index for v in mesh.vertices if rule(v.co)]
        if members:
            group.add(members, 1.0, 'REPLACE')


def apply_defect(body, variant):
    mesh = body.data
    rng = random.Random(4242)
    for vertex in mesh.vertices:
        p = vertex.co
        if variant == 'shoulder_defect' and 0.16 < p.x < 0.24 and p.z > 1.40:
            p.z -= 0.02  # hard step on the left shoulder / arm junction
        elif variant == 'pinched_torso' and abs(p.z - 1.20) < 0.02 and abs(p.x) < 0.2:
            p.x *= 0.62
            p.y *= 0.62
        elif variant == 'noisy_patch' and p.y > 0.03 and 1.25 < p.z < 1.45 and abs(p.x) < 0.1:
            p += vertex.normal * rng.uniform(-0.004, 0.004)
    mesh.update()


def shape_keys(body):
    body.shape_key_add(name='Basis', from_mix=False)
    smile = body.shape_key_add(name='Smile', from_mix=False)
    breath = body.shape_key_add(name='Breath', from_mix=False)
    for vertex in body.data.vertices:
        p = vertex.co
        if p.z > 1.62 and p.y < -0.04 and p.z < 1.70:
            smile.data[vertex.index].co.z += 0.004 * min(1.0, abs(p.x) / 0.05)
        if 1.28 < p.z < 1.48 and p.y < 0 and abs(p.x) < 0.16:
            breath.data[vertex.index].co.y -= 0.01 * (1 - abs(p.z - 1.38) / 0.1)


def custom_normals(body):
    mesh = body.data
    normals = []
    for vertex in mesh.vertices:
        n = vertex.normal.copy()
        if vertex.co.z > 1.56:
            n = (n + Vector((0, -0.3, 0.2))).normalized()
        normals.append(n)
    mesh.normals_split_custom_set_from_vertices(normals)
    for edge in mesh.edges:
        a, b = (mesh.vertices[i].co for i in edge.vertices)
        if abs(a.z - 1.35) < 0.01 and abs(b.z - 1.35) < 0.01:
            edge.use_edge_sharp = True


def build(folder, variant='humanoid', levels=2):
    """Build one fixture in a fresh factory scene and save <folder>/<variant>.blend."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 24
    joints, edges = skeleton(variant)
    name = 'Creature' if variant == 'creature' else 'Hero'
    body = skin_mesh(name + 'Body', joints, edges, 4 if variant == 'dense_humanoid' else levels)
    rig, segments = armature(name + 'Rig', joints)
    body.name = name + 'Body'
    apply_defect(body, variant)
    skin_weights(body, rig, segments, variant not in ('creature', 'self_intersection'))
    uv_layers(body)
    materials(body)
    region_groups(body, variant)
    if variant == 'humanoid_keys':
        shape_keys(body)
    if variant == 'humanoid_normals':
        custom_normals(body)
    body['character_role'] = 'fixture'
    body.data['lod'] = 0
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (variant + '.blend')
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    triangles = sum(len(p.vertices) - 2 for p in body.data.polygons)
    return {'path': str(path), 'variant': variant, 'body': body.name, 'rig': rig.name,
            'vertices': len(body.data.vertices), 'triangles': triangles,
            'groups': [g.name for g in body.vertex_groups], 'materials': [m.name for m in body.data.materials],
            'uv_layers': [u.name for u in body.data.uv_layers],
            'shape_keys': [k.name for k in body.data.shape_keys.key_blocks] if body.data.shape_keys else []}
