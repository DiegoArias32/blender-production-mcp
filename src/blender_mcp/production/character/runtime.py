"""Blender-side character analysis and topology-preserving shape edits (M4).

Everything runs on the undeformed base mesh (bind pose) of one explicit mesh object: the modeling
state, not the current animated pose. Per-vertex data never leaves Blender; payloads return compact
measurements, and every edit field is recomputed deterministically from its parameters.

Built on MESH_RUNTIME (signature sections, region membership, digests) from M3.1.
"""
from ..mesh.runtime import MESH_RUNTIME

CHAR_RUNTIME = MESH_RUNTIME + r'''
import heapq
from mathutils import Vector
from mathutils.kdtree import KDTree
from mathutils.bvhtree import BVHTree

CR_VIEWS = {'FRONT': (1.0, 0.0, 0.0), 'BACK': (-1.0, 0.0, 0.0), 'SIDE': (0.0, 1.0, 0.0),
            'THREE_QUARTER': (0.7071067811865476, 0.7071067811865476, 0.0)}
CR_TOKENS = [('FACE', ('face', 'jaw', 'mouth', 'eye', 'brow', 'cheek')), ('HEAD', ('head', 'skull', 'cranium')),
             ('NECK', ('neck',)), ('CHEST', ('chest', 'ribcage', 'breast')), ('BACK', ('back',)),
             ('TORSO', ('spine', 'torso', 'abdomen', 'belly', 'waist')),
             ('SHOULDER', ('shoulder', 'clavicle', 'collar')), ('HAND', ('hand', 'finger', 'thumb', 'palm', 'wrist')),
             ('ARM', ('arm', 'elbow', 'forearm')), ('FOOT', ('foot', 'toe', 'ankle', 'heel')),
             ('LEG', ('thigh', 'leg', 'knee', 'shin', 'calf')), ('PELVIS', ('pelvis', 'hip', 'hips'))]
CR_LATERAL = ('SHOULDER', 'HAND', 'ARM', 'FOOT', 'LEG')

def cr_object(name):
    return ms_mesh_object(name)

def cr_arrays(obj):
    me = obj.data
    return {'co': ms_floats(me.vertices, 'co', 3), 'normals': ms_floats(me.vertices, 'normal', 3),
            'edges': ms_ints(me.edges, 'vertices', 2), 'starts': ms_ints(me.polygons, 'loop_start', 1),
            'totals': ms_ints(me.polygons, 'loop_total', 1), 'corners': ms_ints(me.loops, 'vertex_index', 1),
            'count': len(me.vertices)}

def cr_neighbors(arrays):
    nb = [[] for _ in range(arrays['count'])]
    edges = arrays['edges']
    for i in range(0, len(edges), 2):
        nb[edges[i]].append(edges[i + 1])
        nb[edges[i + 1]].append(edges[i])
    return nb

def cr_vec(values, i):
    return Vector((values[3 * i], values[3 * i + 1], values[3 * i + 2]))

def cr_world(obj, co):
    matrix = obj.matrix_world
    scale = bpy.context.scene.unit_settings.scale_length
    return [(matrix @ Vector((co[i], co[i + 1], co[i + 2]))) * scale for i in range(0, len(co), 3)]

def cr_bounds(points):
    if not points:
        return {'min': [0.0, 0.0, 0.0], 'max': [0.0, 0.0, 0.0], 'size': [0.0, 0.0, 0.0]}
    low = [min(p[k] for p in points) for k in range(3)]
    high = [max(p[k] for p in points) for k in range(3)]
    return {'min': low, 'max': high, 'size': [high[k] - low[k] for k in range(3)]}

def cr_triangles(me):
    me.calc_loop_triangles()
    return ms_ints(me.loop_triangles, 'vertices', 3)

def cr_side(name):
    low = name.lower()
    if low.endswith(('.l', '_l', '-l')) or low.startswith(('l_', 'left')) or 'left' in low:
        return 'LEFT'
    if low.endswith(('.r', '_r', '-r')) or low.startswith(('r_', 'right')) or 'right' in low:
        return 'RIGHT'
    return None

def cr_token_region(name):
    low = name.lower().replace('upper_arm', 'arm').replace('upperarm', 'arm')
    for region, tokens in CR_TOKENS:
        for token in tokens:
            if token in low:
                if region == 'PELVIS' and cr_side(name) is not None:
                    return 'LEG'
                return region
    return None

def cr_deform_groups(obj):
    names = set()
    for modifier in obj.modifiers:
        if modifier.type == 'ARMATURE' and modifier.object is not None and modifier.object.type == 'ARMATURE':
            names.update(bone.name for bone in modifier.object.data.bones)
    return sorted(g.name for g in obj.vertex_groups if g.name in names)

def cr_centroid_x(co, indices):
    return sum(co[3 * i] for i in indices) / len(indices) if indices else 0.0

def cr_infer_regions(obj, arrays):
    """Candidate regions from vertex-group names (never certain) with geometric side checks."""
    me = obj.data
    co = arrays['co']
    deform = cr_deform_groups(obj)
    regions = []
    buckets = {}
    for name in deform:
        region = cr_token_region(name)
        if region is None:
            continue
        side = cr_side(name)
        key = (side + '_' + region) if (side and region in CR_LATERAL) else region
        buckets.setdefault(key, []).append(name)
    for key in sorted(buckets):
        definition = {'method': 'dominant_group', 'groups': sorted(buckets[key]), 'among': deform}
        indices = mr_region_indices(obj, me, definition)
        evidence = ['deform groups ' + ', '.join(sorted(buckets[key])) + ' named like ' + key.split('_')[-1].lower()]
        confidence = 0.75
        side = key.split('_')[0] if key.startswith(('LEFT_', 'RIGHT_')) else None
        if side and indices:
            x = cr_centroid_x(co, indices)
            if (side == 'LEFT' and x < 0) or (side == 'RIGHT' and x > 0):
                confidence = 0.3
                evidence.append('SIDE_CONFLICT: centroid x ' + format(x, '.3f') + ' contradicts the ' + side + ' name')
            else:
                evidence.append('centroid x ' + format(x, '.3f') + ' agrees with the ' + side + ' name (character left is +X)')
        regions.append({'region_id': key, 'definition': definition, 'vertex_count': len(indices),
                        'confidence': confidence if indices else 0.0, 'evidence': evidence, 'source': 'automatic'})
    for group in obj.vertex_groups:
        if group.name in deform:
            continue
        definition = {'method': 'vertex_group', 'name': group.name, 'min_weight': 0.5}
        indices = mr_region_indices(obj, me, definition)
        if indices:
            regions.append({'region_id': group.name.upper(), 'definition': definition, 'vertex_count': len(indices),
                            'confidence': 0.9, 'evidence': ['explicit non-deform vertex group ' + group.name],
                            'source': 'automatic'})
    # One candidate per id: an explicit named group beats a bone-name guess for the same region.
    unique = {}
    for row in regions:
        kept = unique.get(row['region_id'])
        if kept is None or row['confidence'] > kept['confidence']:
            if kept is not None:
                row['evidence'].append('preferred over a lower-confidence candidate with the same id')
            unique[row['region_id']] = row
    regions = [unique[key] for key in sorted(unique)]
    if not regions:
        box = cr_bounds([cr_vec(co, i) for i in range(arrays['count'])])
        low, high = box['min'], box['max']
        height = high[2] - low[2]
        bands = [('HEAD', 0.87, 1.01), ('TORSO', 0.47, 0.87), ('LEGS', -0.01, 0.47)]
        for name, a, b in bands:
            definition = {'method': 'bounds', 'min': [low[0] - 1.0, low[1] - 1.0, low[2] + a * height],
                          'max': [high[0] + 1.0, high[1] + 1.0, low[2] + b * height]}
            indices = mr_region_indices(obj, me, definition)
            regions.append({'region_id': name, 'definition': definition, 'vertex_count': len(indices), 'confidence': 0.3,
                            'evidence': ['no deform or named groups; height band ' + format(a, '.2f') + '-' + format(b, '.2f')],
                            'source': 'automatic'})
    counts = {}
    for row in regions:
        for index in mr_region_indices(obj, me, row['definition']):
            counts[index] = counts.get(index, 0) + 1
    for row in regions:
        members = mr_region_indices(obj, me, row['definition'])
        shared = sum(1 for index in members if counts[index] > 1)
        row['overlap_fraction'] = shared / len(members) if members else 0.0
        row['ambiguous'] = row['confidence'] < 0.6
    return regions

def cr_topology_warnings(me, arrays):
    edge_index = ms_ints(me.loops, 'edge_index', 1)
    faces_per_edge = [0] * len(me.edges)
    for e in edge_index:
        faces_per_edge[e] += 1
    valence = [0] * arrays['count']
    edges = arrays['edges']
    for v in edges:
        valence[v] += 1
    areas = ms_floats(me.polygons, 'area', 1)
    totals = arrays['totals']
    return {'boundary_edges': sum(1 for c in faces_per_edge if c == 1),
            'non_manifold_edges': sum(1 for c in faces_per_edge if c > 2),
            'wire_edges': sum(1 for c in faces_per_edge if c == 0),
            'loose_vertices': sum(1 for v in valence if v == 0),
            'degenerate_faces': sum(1 for a in areas if a < 1e-12),
            'triangles': sum(1 for t in totals if t == 3), 'quads': sum(1 for t in totals if t == 4),
            'ngons': sum(1 for t in totals if t > 4), 'poles_valence_6_plus': sum(1 for v in valence if v >= 6)}

def cr_weight_coverage(obj, me):
    names = set(cr_deform_groups(obj))
    index_names = [g.name for g in obj.vertex_groups]
    unweighted = 0
    max_influences = 0
    for vertex in me.vertices:
        influences = sum(1 for e in vertex.groups if 0 <= e.group < len(index_names) and index_names[e.group] in names and e.weight > 0)
        max_influences = max(max_influences, influences)
        if names and influences == 0:
            unweighted += 1
    return {'deform_groups': len(names), 'unweighted_vertices': unweighted, 'max_influences': max_influences}

def cr_mirror(arrays, tolerance):
    co = arrays['co']
    count = arrays['count']
    tree = KDTree(count)
    for i in range(count):
        tree.insert((co[3 * i], co[3 * i + 1], co[3 * i + 2]), i)
    tree.balance()
    distances = []
    for i in range(count):
        found = tree.find((-co[3 * i], co[3 * i + 1], co[3 * i + 2]))
        distances.append(found[2])
    ordered = sorted(distances)
    return {'distances': distances, 'plane': 'object-space X = 0', 'tolerance': tolerance,
            'symmetric_fraction': sum(1 for d in distances if d <= tolerance) / count if count else 1.0,
            'median': ordered[count // 2] if count else 0.0, 'p95': ordered[int(count * 0.95)] if count else 0.0,
            'max': ordered[-1] if count else 0.0}

def cr_shape_key_report(me, edited):
    key = me.shape_keys
    if key is None:
        return {'present': False, 'keys': [], 'safe': True, 'strategy': 'none'}
    reference = key.reference_key
    ref = ms_floats(reference.data, 'co', 3)
    mesh_co = ms_floats(me.vertices, 'co', 3)
    mismatch = sum(1 for i in range(len(ref) // 3)
                   if abs(ref[3 * i] - mesh_co[3 * i]) + abs(ref[3 * i + 1] - mesh_co[3 * i + 1]) + abs(ref[3 * i + 2] - mesh_co[3 * i + 2]) > 1e-7)
    rows = []
    for block in key.key_blocks:
        if block == reference:
            continue
        data = ms_floats(block.data, 'co', 3)
        moved = [i for i in range(len(data) // 3)
                 if abs(data[3 * i] - ref[3 * i]) + abs(data[3 * i + 1] - ref[3 * i + 1]) + abs(data[3 * i + 2] - ref[3 * i + 2]) > 1e-7]
        rows.append({'name': block.name, 'relative_key': block.relative_key.name if block.relative_key else None,
                     'moved_vertices': len(moved), 'edited_and_moved': sum(1 for i in moved if i in edited),
                     'has_driver': bool(key.animation_data and any(d.data_path == 'key_blocks["' + block.name + '"].value' for d in key.animation_data.drivers))})
    return {'present': True, 'use_relative': key.use_relative, 'basis': reference.name, 'keys': rows,
            'basis_mesh_mismatch_vertices': mismatch, 'edited_vertices': len(edited)}

def cr_inspect(obj, mirror_fraction):
    me = obj.data
    arrays = cr_arrays(obj)
    points = cr_world(obj, arrays['co'])
    box = cr_bounds(points)
    tolerance = max(box['size'][2], 1e-6) * mirror_fraction
    mirror = cr_mirror(arrays, tolerance)
    signature = ms_signature(obj)
    armature = [m.object.name for m in obj.modifiers if m.type == 'ARMATURE' and m.object is not None]
    return {'object': obj.name, 'mesh': me.name, 'signature': signature,
            'counts': {k: signature['sections']['topology'][k] for k in ('vertices', 'edges', 'polygons', 'triangles')},
            'world_bounds': box, 'dimensions_m': box['size'],
            'parent': obj.parent.name if obj.parent else None, 'armatures': armature,
            'symmetry': {k: mirror[k] for k in ('plane', 'tolerance', 'symmetric_fraction', 'median', 'p95', 'max')},
            'topology_warnings': cr_topology_warnings(me, arrays),
            'deformation': dict(cr_weight_coverage(obj, me), armature_modifiers=signature['sections']['modifiers']['armature_modifiers']),
            'shape_keys': cr_shape_key_report(me, set()),
            'regions': cr_infer_regions(obj, arrays)}

def cr_raster(points, triangles, right, rows):
    us = [p[0] * right[0] + p[1] * right[1] + p[2] * right[2] for p in points]
    vs = [p[2] for p in points]
    umin, umax, vmin, vmax = min(us), max(us), min(vs), max(vs)
    cell = max(vmax - vmin, 1e-9) / rows
    cols = int((umax - umin) / cell) + 2
    grid = bytearray(rows * cols)
    for t in range(0, len(triangles), 3):
        a, b, c = triangles[t], triangles[t + 1], triangles[t + 2]
        ax, ay, bx, by, cx, cy = us[a], vs[a], us[b], vs[b], us[c], vs[c]
        denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denom) < 1e-18:
            continue
        c0 = max(0, int(math.ceil((min(ax, bx, cx) - umin) / cell - 0.5)))
        c1 = min(cols - 1, int(math.floor((max(ax, bx, cx) - umin) / cell - 0.5)))
        r0 = max(0, int(math.ceil((min(ay, by, cy) - vmin) / cell - 0.5)))
        r1 = min(rows - 1, int(math.floor((max(ay, by, cy) - vmin) / cell - 0.5)))
        for r in range(r0, r1 + 1):
            y = vmin + (r + 0.5) * cell
            base = r * cols
            for j in range(c0, c1 + 1):
                x = umin + (j + 0.5) * cell
                l1 = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / denom
                l2 = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / denom
                if l1 >= 0 and l2 >= 0 and l1 + l2 <= 1:
                    grid[base + j] = 1
    return {'grid': grid, 'rows': rows, 'cols': cols, 'cell': cell, 'umin': umin, 'vmin': vmin, 'us': us, 'vs': vs}

def cr_holes(raster):
    grid, rows, cols = raster['grid'], raster['rows'], raster['cols']
    seen = bytearray(rows * cols)
    stack = [r * cols + j for r in range(rows) for j in (0, cols - 1)] + [j for j in range(cols)] + [(rows - 1) * cols + j for j in range(cols)]
    while stack:
        k = stack.pop()
        if seen[k] or grid[k]:
            continue
        seen[k] = 1
        r, j = k // cols, k % cols
        if r > 0: stack.append(k - cols)
        if r < rows - 1: stack.append(k + cols)
        if j > 0: stack.append(k - 1)
        if j < cols - 1: stack.append(k + 1)
    enclosed = [k for k in range(rows * cols) if not grid[k] and not seen[k]]
    components = 0
    visited = set()
    for k in enclosed:
        if k in visited:
            continue
        components += 1
        queue = [k]
        while queue:
            m = queue.pop()
            if m in visited or grid[m] or seen[m]:
                continue
            visited.add(m)
            r, j = m // cols, m % cols
            if r > 0: queue.append(m - cols)
            if r < rows - 1: queue.append(m + cols)
            if j > 0: queue.append(m - 1)
            if j < cols - 1: queue.append(m + 1)
    return {'holes': components, 'hole_area_m2': len(enclosed) * raster['cell'] ** 2}

def cr_run_width(raster, row, center_col):
    grid, cols = raster['grid'], raster['cols']
    base = row * cols
    if not (0 <= center_col < cols) or not grid[base + center_col]:
        return 0.0
    left = center_col
    while left > 0 and grid[base + left - 1]:
        left -= 1
    right = center_col
    while right < cols - 1 and grid[base + right + 1]:
        right += 1
    return (right - left + 1) * raster['cell']

def cr_silhouette_view(obj, points, triangles, view, rows, center_u):
    raster = cr_raster(points, triangles, CR_VIEWS[view], rows)
    grid, cols, cell = raster['grid'], raster['cols'], raster['cell']
    filled_rows = [r for r in range(rows) if any(grid[r * cols:(r + 1) * cols])]
    center_col = int((center_u - raster['umin']) / cell)
    total = 0
    left_area = 0
    su = 0.0
    sv = 0.0
    min_col, max_col = cols, -1
    reach = 0.0
    for r in filled_rows:
        base = r * cols
        for j in range(cols):
            if grid[base + j]:
                total += 1
                su += j
                sv += r
                min_col = min(min_col, j)
                max_col = max(max_col, j)
                reach = max(reach, abs(j + 0.5 - (center_u - raster['umin']) / cell))
                if j < center_col:
                    left_area += 1
    profile = []
    for fraction in (0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95):
        r = min(rows - 1, int(fraction * rows))
        row = grid[r * cols:(r + 1) * cols]
        filled = [j for j in range(cols) if row[j]]
        profile.append({'height_fraction': fraction, 'span_m': ((filled[-1] - filled[0] + 1) * cell) if filled else 0.0,
                        'filled_width_m': len(filled) * cell, 'center_run_width_m': cr_run_width(raster, r, center_col)})
    holes = cr_holes(raster)
    area = total * cell * cell
    return {'view': view, 'height_m': (len(filled_rows)) * cell, 'width_m': (max_col - min_col + 1) * cell if total else 0.0,
            'area_m2': area, 'centroid_u_m': raster['umin'] + (su / total + 0.5) * cell if total else 0.0,
            'centroid_v_m': raster['vmin'] + (sv / total + 0.5) * cell if total else 0.0,
            'left_area_fraction': left_area / total if total else 0.0, 'extremity_reach_m': reach * cell,
            'negative_space': holes, 'profile': profile, 'cell_m': cell, 'rows': rows,
            'raster': raster}

def cr_region_extent(raster, indices):
    if not indices:
        return None
    us = [raster['us'][i] for i in indices]
    vs = [raster['vs'][i] for i in indices]
    return {'u_min': min(us), 'u_max': max(us), 'v_min': min(vs), 'v_max': max(vs),
            'width_m': max(us) - min(us), 'height_m': max(vs) - min(vs)}

def cr_silhouettes(obj, views, rows, region_defs):
    me = obj.data
    arrays = cr_arrays(obj)
    points = cr_world(obj, arrays['co'])
    triangles = cr_triangles(me)
    origin = obj.matrix_world.translation * bpy.context.scene.unit_settings.scale_length
    output = []
    members = {row['region_id']: mr_region_indices(obj, me, row['definition']) for row in region_defs}
    for view in views:
        right = CR_VIEWS[view]
        center_u = origin[0] * right[0] + origin[1] * right[1] + origin[2] * right[2]
        measured = cr_silhouette_view(obj, points, triangles, view, rows, center_u)
        raster = measured.pop('raster')
        measured['regions'] = {name: cr_region_extent(raster, indices) for name, indices in members.items() if indices}
        if view in ('FRONT', 'BACK'):
            torso = members.get('TORSO') or []
            if torso:
                extent = measured['regions']['TORSO']
                cell = raster['cell']
                r0 = max(0, int((extent['v_min'] - raster['vmin']) / cell))
                r1 = min(rows - 1, int((extent['v_max'] - raster['vmin']) / cell))
                center_col = int((center_u - raster['umin']) / cell)
                widths = [cr_run_width(raster, r, center_col) for r in range(r0, r1 + 1)]
                widths = [w for w in widths if w > 0]
                measured['waist_width_m'] = min(widths) if widths else None
                measured['torso_max_run_width_m'] = max(widths) if widths else None
        output.append(measured)
    return {'object': obj.name, 'basis': 'undeformed base mesh (bind pose) in world meters; orthographic projection',
            'views': output, 'world_bounds': cr_bounds(points)}

def cr_stats(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return {'median': 0.0, 'mad': 0.0, 'p95': 0.0, 'max': 0.0}
    median = ordered[n // 2]
    deviations = sorted(abs(v - median) for v in ordered)
    return {'median': median, 'mad': deviations[n // 2], 'p95': ordered[int(n * 0.95)], 'max': ordered[-1]}

def cr_clusters(flags, nb, minimum):
    seen = set()
    clusters = []
    for start in sorted(flags):
        if start in seen:
            continue
        component = []
        queue = [start]
        seen.add(start)
        while queue:
            v = queue.pop()
            component.append(v)
            for w in nb[v]:
                if w in flags and w not in seen:
                    seen.add(w)
                    queue.append(w)
        if len(component) >= minimum:
            clusters.append(component)
    return clusters

def cr_region_of(component, region_members):
    best, best_count = None, 0
    for name, members in region_members.items():
        count = sum(1 for v in component if v in members)
        if count > best_count:
            best, best_count = name, count
    return best

def cr_surface(obj, options, region_defs):
    me = obj.data
    arrays = cr_arrays(obj)
    nb = cr_neighbors(arrays)
    co, normals, n = arrays['co'], arrays['normals'], arrays['count']
    points = [cr_vec(co, i) for i in range(n)]
    nvec = [cr_vec(normals, i) for i in range(n)]
    edge_mean = [0.0] * n
    height = [0.0] * n
    for i in range(n):
        if not nb[i]:
            continue
        mean = Vector((0.0, 0.0, 0.0))
        total = 0.0
        for j in nb[i]:
            mean += points[j]
            total += (points[j] - points[i]).length
        mean /= len(nb[i])
        edge_mean[i] = total / len(nb[i])
        if edge_mean[i] > 0:
            height[i] = (mean - points[i]).dot(nvec[i]) / edge_mean[i]
    noise = [0.0] * n
    normal_dev = [0.0] * n
    density = [1.0] * n
    for i in range(n):
        if not nb[i]:
            continue
        noise[i] = height[i] - sum(height[j] for j in nb[i]) / len(nb[i])
        avg = Vector((0.0, 0.0, 0.0))
        for j in nb[i]:
            avg += nvec[j]
        if avg.length > 1e-12 and nvec[i].length > 1e-12:
            normal_dev[i] = math.degrees(nvec[i].angle(avg, 0.0))
        around = sum(edge_mean[j] for j in nb[i]) / len(nb[i])
        density[i] = edge_mean[i] / around if around > 0 else 1.0
    abs_noise = cr_stats([abs(v) for v in noise])
    gradient = [0.0] * n
    for i in range(n):
        for j in nb[i]:
            gradient[i] = max(gradient[i], abs(height[i] - height[j]))
    gstats = cr_stats(gradient)
    k = options['robust_k']
    bump_t = max(options['min_bump'], abs_noise['median'] + k * abs_noise['mad'])
    grad_t = max(options['min_gradient'], gstats['median'] + k * gstats['mad'])
    flags = {'unexpected_bump': set(), 'local_pinching': set(), 'abrupt_curvature_change': set(), 'noisy_surface': set(),
             'inconsistent_density': set(), 'normal_irregularity': set(), 'stretched_geometry': set(),
             'faceting_risk': set(), 'suspicious_flattening': set()}
    values = {name: {} for name in flags}
    for i in range(n):
        if noise[i] < -bump_t:
            flags['unexpected_bump'].add(i); values['unexpected_bump'][i] = -noise[i] / bump_t
        if noise[i] > bump_t and density[i] < options['pinch_density']:
            flags['local_pinching'].add(i); values['local_pinching'][i] = noise[i] / bump_t
        if gradient[i] > grad_t:
            flags['abrupt_curvature_change'].add(i); values['abrupt_curvature_change'][i] = gradient[i] / grad_t
        if density[i] > options['density_ratio'] or density[i] < 1.0 / options['density_ratio']:
            flags['inconsistent_density'].add(i); values['inconsistent_density'][i] = max(density[i], 1.0 / max(density[i], 1e-9)) / options['density_ratio']
        if normal_dev[i] > options['normal_deviation_degrees']:
            flags['normal_irregularity'].add(i); values['normal_irregularity'][i] = normal_dev[i] / options['normal_deviation_degrees']
    # High-frequency noise: sign alternation of the height residual among neighbours.
    for i in range(n):
        if abs(noise[i]) > bump_t * 0.5 and nb[i]:
            flips = sum(1 for j in nb[i] if noise[j] * noise[i] < 0 and abs(noise[j]) > bump_t * 0.5)
            if flips >= 2:
                flags['noisy_surface'].add(i); values['noisy_surface'][i] = abs(noise[i]) / bump_t
    starts, totals, corners = arrays['starts'], arrays['totals'], arrays['corners']
    smooth = [False] * len(starts)
    me.polygons.foreach_get('use_smooth', smooth)
    for p in range(len(starts)):
        loop = [corners[c] for c in range(starts[p], starts[p] + totals[p])]
        lengths = [(points[loop[(q + 1) % len(loop)]] - points[loop[q]]).length for q in range(len(loop))]
        if min(lengths) > 0 and max(lengths) / min(lengths) > options['stretch_ratio']:
            for v in loop:
                flags['stretched_geometry'].add(v)
                values['stretched_geometry'][v] = max(values['stretched_geometry'].get(v, 0), (max(lengths) / min(lengths)) / options['stretch_ratio'])
    sharp = me.attributes.get('sharp_edge')
    sharp_values = ms_bools(sharp.data, 'value') if sharp is not None else [False] * len(me.edges)
    face_normals = ms_floats(me.polygons, 'normal', 3)
    edge_faces = {}
    edge_index = ms_ints(me.loops, 'edge_index', 1)
    for p in range(len(starts)):
        for c in range(starts[p], starts[p] + totals[p]):
            edge_faces.setdefault(edge_index[c], []).append(p)
    edges = arrays['edges']
    for e, faces in edge_faces.items():
        if len(faces) != 2 or sharp_values[e] or not (smooth[faces[0]] and smooth[faces[1]]):
            continue
        angle = math.degrees(cr_vec(face_normals, faces[0]).angle(cr_vec(face_normals, faces[1]), 0.0))
        if angle > options['faceting_degrees']:
            for v in (edges[2 * e], edges[2 * e + 1]):
                flags['faceting_risk'].add(v)
                values['faceting_risk'][v] = max(values['faceting_risk'].get(v, 0), angle / options['faceting_degrees'])
    members = {row['region_id']: set(mr_region_indices(obj, me, row['definition'])) for row in region_defs}
    world = cr_world(obj, co)
    findings = []
    for kind, flagged in flags.items():
        for component in cr_clusters(flagged, nb, options['min_cluster']):
            ratio = max(values[kind].get(v, 1.0) for v in component)
            center = Vector((0.0, 0.0, 0.0))
            for v in component:
                center += world[v]
            center /= len(component)
            findings.append({'type': kind, 'vertices': len(component), 'region': cr_region_of(component, members),
                             'centroid_world_m': [round(x, 4) for x in center], 'max_ratio_to_threshold': ratio,
                             'severity': 'high' if ratio >= 3 else 'medium' if ratio >= 1.5 else 'low'})
    findings.sort(key=cr_finding_rank)
    return {'object': obj.name, 'thresholds': dict(options, bump_residual=bump_t, curvature_gradient=grad_t),
            'distribution': {'height_residual_abs': abs_noise, 'curvature_gradient': gstats},
            'findings': findings[:options['limit']], 'findings_total': len(findings)}

def cr_finding_rank(row):
    return ({'high': 0, 'medium': 1, 'low': 2}[row['severity']], -row['vertices'])

def cr_overlap_pairs(obj):
    me = obj.data
    triangles = cr_triangles(me)
    points = cr_world(obj, ms_floats(me.vertices, 'co', 3))
    tris = [(triangles[i], triangles[i + 1], triangles[i + 2]) for i in range(0, len(triangles), 3)]
    tree = BVHTree.FromPolygons(points, tris, all_triangles=True, epsilon=0.0)
    pairs = []
    for a, b in tree.overlap(tree):
        if a < b and not set(tris[a]) & set(tris[b]):
            pairs.append((a, b))
    return pairs, tris, points

def cr_intersections(obj, region_defs, reference_name):
    me = obj.data
    pairs, tris, points = cr_overlap_pairs(obj)
    members = {row['region_id']: set(mr_region_indices(obj, me, row['definition'])) for row in region_defs}
    reference_pairs = None
    if reference_name:
        reference = cr_object(reference_name)
        if ms_topology(reference.data)['fingerprint'] != ms_topology(me)['fingerprint']:
            raise ValueError('TOPOLOGY_CHANGED: intersection comparison needs identical topology')
        reference_pairs = set(cr_overlap_pairs(reference)[0])
    new_pairs = [p for p in pairs if reference_pairs is None or p not in reference_pairs]
    by_region = {}
    for a, b in new_pairs:
        ra = cr_region_of(tris[a], members) or 'unassigned'
        rb = cr_region_of(tris[b], members) or 'unassigned'
        key = ' x '.join(sorted({ra, rb}))
        by_region[key] = by_region.get(key, 0) + 1
    samples = []
    for a, b in new_pairs[:10]:
        center = (points[tris[a][0]] + points[tris[a][1]] + points[tris[a][2]]) / 3
        samples.append([round(x, 4) for x in center])
    return {'object': obj.name, 'triangles': len(tris), 'overlap_pairs': len(pairs),
            'reference': reference_name, 'reference_overlap_pairs': len(reference_pairs) if reference_pairs is not None else None,
            'new_overlap_pairs': len(new_pairs) if reference_pairs is not None else None,
            'by_region_pair': by_region, 'sample_locations_world_m': samples,
            'confidence': 0.7, 'limitations': 'BVH triangle-overlap indicator on the base mesh; touching/coplanar faces are ambiguous; not a collision solver'}

def cr_asymmetry(obj, region_defs, intentional, tolerance_fraction):
    me = obj.data
    arrays = cr_arrays(obj)
    co = arrays['co']
    box = cr_bounds([cr_vec(co, i) for i in range(arrays['count'])])
    tolerance = max(box['size'][2], 1e-6) * tolerance_fraction
    mirror = cr_mirror(arrays, tolerance)
    distances = mirror.pop('distances')
    starts, totals, corners = arrays['starts'], arrays['totals'], arrays['corners']
    areas = ms_floats(me.polygons, 'area', 1)
    rows = {}
    for row in region_defs:
        members = set(mr_region_indices(obj, me, row['definition']))
        if not members:
            continue
        points = [cr_vec(co, i) for i in members]
        extent = cr_bounds(points)
        area = sum(areas[p] for p in range(len(starts)) if all(corners[c] in members for c in range(starts[p], starts[p] + totals[p])))
        asym = sum(1 for i in members if distances[i] > tolerance)
        rows[row['region_id']] = {'vertices': len(members), 'surface_area': area,
                                  'bbox_volume': extent['size'][0] * extent['size'][1] * extent['size'][2],
                                  'mirror_mismatch_fraction': asym / len(members),
                                  'max_mirror_distance': max(distances[i] for i in members)}
    pairs = []
    for name in sorted(rows):
        if not name.startswith('LEFT_'):
            continue
        other = 'RIGHT_' + name[5:]
        if other not in rows:
            continue
        left, right = rows[name], rows[other]
        ratio_area = left['surface_area'] / right['surface_area'] if right['surface_area'] > 0 else None
        ratio_volume = left['bbox_volume'] / right['bbox_volume'] if right['bbox_volume'] > 0 else None
        deviation = max(abs(math.log(r)) for r in (ratio_area, ratio_volume) if r) if (ratio_area and ratio_volume) else None
        pairs.append({'left': name, 'right': other, 'surface_area_ratio': ratio_area, 'bbox_volume_ratio': ratio_volume,
                      'log_deviation': deviation})
    return {'object': obj.name, 'mirror': mirror, 'regions': rows, 'pairs': pairs,
            'intentional_regions': sorted(intentional)}

def cr_weights(obj, me, definition):
    if definition['method'] == 'vertex_group':
        group = obj.vertex_groups.get(definition['name'])
        if group is None:
            raise ValueError('REGION_UNDEFINED: vertex group ' + definition['name'] + ' does not exist')
        weights = {}
        for vertex in me.vertices:
            for element in vertex.groups:
                if element.group == group.index and element.weight > 0:
                    weights[vertex.index] = element.weight
        return weights
    return {i: 1.0 for i in mr_region_indices(obj, me, definition)}

def cr_distances(nb, points, sources, limit):
    distance = {s: 0.0 for s in sources}
    heap = [(0.0, s) for s in sources]
    heapq.heapify(heap)
    while heap:
        d, v = heapq.heappop(heap)
        if d > distance.get(v, 1e30) or d > limit:
            continue
        for w in nb[v]:
            nd = d + (points[w] - points[v]).length
            if nd < distance.get(w, 1e30) and nd <= limit:
                distance[w] = nd
                heapq.heappush(heap, (nd, w))
    return distance

def cr_smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)

def cr_newell(values, corners, start, total):
    normal = Vector((0.0, 0.0, 0.0))
    for q in range(total):
        a = corners[start + q]
        b = corners[start + (q + 1) % total]
        ax, ay, az = values[3 * a], values[3 * a + 1], values[3 * a + 2]
        bx, by, bz = values[3 * b], values[3 * b + 1], values[3 * b + 2]
        normal += Vector(((ay - by) * (az + bz), (az - bz) * (ax + bx), (ax - bx) * (ay + by)))
    return normal

def cr_flip_guard(arrays, field, active):
    """Attenuate (x0.5 per round, zero after 8) vertices whose move would reverse an incident face.

    A single boundary vertex next to a sliver face can cross it and flip it (measured on the creature
    fixture: 2.2 cm smooth move, 174 degree normal change); geometry must never fold.
    """
    co, starts, totals, corners = arrays['co'], arrays['starts'], arrays['totals'], arrays['corners']
    moving = set(active)
    faces = [p for p in range(len(starts)) if any(corners[c] in moving for c in range(starts[p], starts[p] + totals[p]))]
    before = {p: cr_newell(co, corners, starts[p], totals[p]) for p in faces}
    attenuated = {}
    for round_index in range(9):
        moved = [co[k] + field[k] for k in range(len(co))]
        flipped = [p for p in faces if before[p].length > 1e-18 and before[p].dot(cr_newell(moved, corners, starts[p], totals[p])) <= 0]
        if not flipped:
            break
        for p in flipped:
            for c in range(starts[p], starts[p] + totals[p]):
                v = corners[c]
                if v in moving:
                    factor = 0.0 if round_index == 8 else 0.5
                    field[3 * v] *= factor
                    field[3 * v + 1] *= factor
                    field[3 * v + 2] *= factor
                    attenuated[v] = attenuated.get(v, 0) + 1
    return attenuated

def cr_field(obj, operation, editable, locked):
    """Deterministic displacement field (object space) for one topology-preserving edit."""
    me = obj.data
    arrays = cr_arrays(obj)
    nb = cr_neighbors(arrays)
    n = arrays['count']
    points = [cr_vec(arrays['co'], i) for i in range(n)]
    normals = [cr_vec(arrays['normals'], i) for i in range(n)]
    region = cr_weights(obj, me, editable)
    locked_set = set()
    for definition in locked:
        locked_set.update(mr_region_indices(obj, me, definition))
    falloff = operation['falloff']
    width = falloff['width_m'] / max(bpy.context.scene.unit_settings.scale_length, 1e-9)
    outside = [i for i in range(n) if i not in region or i in locked_set]
    boundary = cr_distances(nb, points, outside, width) if operation['preserve_boundary'] and width > 0 else {}
    weights = [0.0] * n
    for i, r in region.items():
        if i in locked_set:
            continue
        b = 1.0
        if operation['preserve_boundary'] and width > 0:
            d = boundary.get(i, width)
            b = cr_smoothstep(d / width) if falloff['type'] == 'smooth' else (min(1.0, d / width) if falloff['type'] == 'linear' else (1.0 if d > 0 else 0.0))
        weights[i] = r * b
    kind = operation['type']
    strength = operation['strength']
    field = [0.0] * (3 * n)
    direction = Vector(operation.get('direction') or (0.0, 0.0, 1.0))
    if operation.get('space') == 'world':
        direction = obj.matrix_world.inverted().to_3x3() @ direction
    if direction.length > 0:
        direction = direction.normalized()
    active = [i for i in range(n) if weights[i] > 0]
    pivot = Vector((0.0, 0.0, 0.0))
    if kind == 'scale':
        if operation.get('pivot') in (None, 'region_centroid'):
            total = sum(weights[i] for i in active)
            for i in active:
                pivot += points[i] * weights[i]
            pivot = pivot / total if total > 0 else pivot
        else:
            pivot = Vector(operation['pivot'])
        factors = operation.get('factors') or [strength, strength, strength]
    if kind in ('smooth', 'relax', 'crease'):
        current = [p.copy() for p in points]
        for _ in range(int(operation.get('iterations', 3))):
            updated = [p.copy() for p in current]
            for i in active:
                if not nb[i]:
                    continue
                mean = Vector((0.0, 0.0, 0.0))
                for j in nb[i]:
                    mean += current[j]
                mean /= len(nb[i])
                step = (mean - current[i]) * (weights[i] * strength)
                if kind == 'relax':
                    step = step - normals[i] * step.dot(normals[i])
                elif kind == 'crease':
                    step = -normals[i] * step.dot(normals[i])
                updated[i] = current[i] + step
            current = updated
        for i in active:
            delta = current[i] - points[i]
            field[3 * i:3 * i + 3] = [delta.x, delta.y, delta.z]
    else:
        center = Vector(operation['center']) if kind == 'grab' else None
        for i in active:
            w = weights[i]
            if kind in ('translate', 'grab'):
                delta = direction * (strength * w)
                if kind == 'grab':
                    delta = delta * cr_smoothstep(1.0 - (points[i] - center).length / operation['radius_m'])
            elif kind == 'directional':
                delta = direction * (strength * w * max(0.0, normals[i].dot(direction)))
            elif kind == 'inflate':
                delta = normals[i] * (strength * w)
            elif kind == 'scale':
                offset = points[i] - pivot
                delta = Vector((offset.x * (factors[0] - 1), offset.y * (factors[1] - 1), offset.z * (factors[2] - 1))) * w
            else:
                raise ValueError('Unknown shape operation: ' + str(kind))
            field[3 * i:3 * i + 3] = [delta.x, delta.y, delta.z]
    guarded = cr_flip_guard(arrays, field, active)
    magnitudes = [math.sqrt(field[3 * i] ** 2 + field[3 * i + 1] ** 2 + field[3 * i + 2] ** 2) for i in range(n)]
    edited = set(i for i in range(n) if magnitudes[i] > 1e-12)
    near_locked = cr_distances(nb, points, sorted(locked_set), 1e30) if locked_set else {}
    proximity = min((near_locked.get(i, 1e30) for i in edited), default=None)
    scale = bpy.context.scene.unit_settings.scale_length
    return {'field': field, 'weights': weights, 'edited': edited, 'locked': locked_set, 'region_size': len(region),
            'stats': {'edited_vertices': len(edited), 'region_vertices': len(region), 'locked_vertices': len(locked_set),
                      'max_displacement_m': max(magnitudes) * scale if magnitudes else 0.0,
                      'mean_displacement_m': (sum(magnitudes[i] for i in edited) / len(edited) * scale) if edited else 0.0,
                      'locked_proximity_m': proximity * scale if proximity is not None and proximity < 1e29 else None,
                      'flip_guard_attenuated_vertices': len(guarded)},
            'digest': m2_digest([round(v, 9) for v in field])}

def cr_shape_key_safety(me, edited, policy):
    report = cr_shape_key_report(me, edited)
    reasons = []
    if report['present']:
        if not report['use_relative']:
            reasons.append('SHAPE_KEYS_ABSOLUTE_UNSUPPORTED: absolute shape keys cannot be offset safely')
        if report['basis_mesh_mismatch_vertices']:
            reasons.append('SHAPE_KEY_BASIS_MISMATCH: ' + str(report['basis_mesh_mismatch_vertices']) + ' mesh positions differ from the Basis key')
        conflicts = [row['name'] + ' (' + str(row['edited_and_moved']) + ')' for row in report['keys'] if row['edited_and_moved']]
        if conflicts and policy != 'propagate_delta':
            reasons.append('SHAPE_KEY_CONFLICT: edited vertices are moved by ' + ', '.join(conflicts)
                           + '; use shape_keys="propagate_delta" to keep every key offset, or edit another region')
    report['safe'] = not reasons
    report['reasons'] = reasons
    report['strategy'] = 'none' if not report['present'] else 'propagate_delta: every key receives the same displacement, so each key offset from its relative key is unchanged'
    return report

def cr_apply_field(me, field):
    co = ms_floats(me.vertices, 'co', 3)
    me.vertices.foreach_set('co', [co[k] + field[k] for k in range(len(co))])
    if me.shape_keys is not None:
        for block in me.shape_keys.key_blocks:
            data = ms_floats(block.data, 'co', 3)
            block.data.foreach_set('co', [data[k] + field[k] for k in range(len(data))])
    me.update()

def cr_material_state(me):
    return m2_digest([[m.name if m else None for m in me.materials], ms_ints(me.polygons, 'material_index', 1)])

def cr_key_offsets(me):
    if me.shape_keys is None:
        return {}
    output = {}
    for block in me.shape_keys.key_blocks:
        data = ms_floats(block.data, 'co', 3)
        ref = ms_floats(block.relative_key.data, 'co', 3) if block.relative_key else data
        output[block.name] = [data[k] - ref[k] for k in range(len(data))]
    return output

def cr_verify(obj, before, after, locked, editable, tolerance_m):
    """Compare two mesh datablocks of the same object: what must be preserved, and where it moved."""
    scale = bpy.context.scene.unit_settings.scale_length
    checks = {}
    checks['topology'] = ms_topology(before)['fingerprint'] == ms_topology(after)['fingerprint']
    checks['weights'] = ms_vertex_groups(obj, before)['weights_fingerprint'] == ms_vertex_groups(obj, after)['weights_fingerprint']
    checks['uv'] = ms_uv_layers(before)['fingerprint'] == ms_uv_layers(after)['fingerprint']
    checks['materials'] = cr_material_state(before) == cr_material_state(after)
    checks['normals_state'] = ms_normals(before)['fingerprint'] == ms_normals(after)['fingerprint']
    checks['attributes'] = ms_attributes(before)['fingerprint'] == ms_attributes(after)['fingerprint']
    offsets_before, offsets_after = cr_key_offsets(before), cr_key_offsets(after)
    key_error = 0.0
    for name, values in offsets_before.items():
        other = offsets_after.get(name)
        if other is None or len(other) != len(values):
            key_error = 1e30
            break
        key_error = max([key_error] + [abs(values[k] - other[k]) for k in range(len(values))])
    checks['shape_key_offsets'] = set(offsets_before) == set(offsets_after) and key_error <= 1e-6
    errors = [name.upper() + '_CHANGED' for name, ok in checks.items() if not ok]
    b = ms_floats(before.vertices, 'co', 3)
    a = ms_floats(after.vertices, 'co', 3)
    n = len(b) // 3
    moved = [math.sqrt((a[3 * i] - b[3 * i]) ** 2 + (a[3 * i + 1] - b[3 * i + 1]) ** 2 + (a[3 * i + 2] - b[3 * i + 2]) ** 2) * scale for i in range(n)]
    locked_set = set()
    for definition in locked:
        locked_set.update(mr_region_indices(obj, after, definition))
    region = set(cr_weights(obj, after, editable)) if editable else set(i for i in range(n) if moved[i] > 0)
    edges = ms_ints(after.edges, 'vertices', 2)
    boundary = set()
    for i in range(0, len(edges), 2):
        x, y = edges[i], edges[i + 1]
        if (x in region) != (y in region):
            boundary.add(x if x in region else y)
    locked_max = max((moved[i] for i in locked_set), default=0.0)
    if locked_max > tolerance_m:
        errors.append('LOCKED_REGION_MODIFIED: ' + format(locked_max, '.6g') + ' m > ' + format(tolerance_m, '.6g') + ' m')
    gradient = 0.0
    for i in range(0, len(edges), 2):
        x, y = edges[i], edges[i + 1]
        length = math.sqrt((b[3 * x] - b[3 * y]) ** 2 + (b[3 * x + 1] - b[3 * y + 1]) ** 2 + (b[3 * x + 2] - b[3 * y + 2]) ** 2) * scale
        if length > 0:
            d = math.sqrt(sum(((a[3 * x + k] - b[3 * x + k]) - (a[3 * y + k] - b[3 * y + k])) ** 2 for k in range(3))) * scale
            gradient = max(gradient, d / length)
    fb = ms_floats(before.polygons, 'normal', 3)
    fa = ms_floats(after.polygons, 'normal', 3)
    normal_change = 0.0
    flipped = 0
    for p in range(len(fb) // 3):
        u, v = cr_vec(fb, p), cr_vec(fa, p)
        if u.length > 1e-12 and v.length > 1e-12:
            angle = math.degrees(u.angle(v, 0.0))
            normal_change = max(normal_change, angle)
            if angle >= 90.0:
                flipped += 1
    if flipped:
        errors.append('FACE_FLIPPED: ' + str(flipped) + ' face(s) turned by 90 degrees or more')
    metrics = {'max_displacement_m': max(moved) if moved else 0.0,
               'moved_vertices': sum(1 for m in moved if m > 1e-9),
               'locked_vertices': len(locked_set), 'locked_max_displacement_m': locked_max,
               'boundary_vertices': len(boundary), 'boundary_max_displacement_m': max((moved[i] for i in boundary), default=0.0),
               'outside_region_max_displacement_m': max((moved[i] for i in range(n) if i not in region), default=0.0),
               'max_displacement_gradient': gradient, 'max_face_normal_change_degrees': normal_change, 'flipped_faces': flipped,
               'shape_key_offset_max_error': key_error if key_error < 1e29 else None}
    return {'passed': not errors, 'checks': checks, 'metrics': metrics, 'errors': errors}
'''

INSPECT = CHAR_RUNTIME + r'''
result = cr_inspect(cr_object(params['object_name']), params['mirror_fraction'])
'''

SILHOUETTE = CHAR_RUNTIME + r'''
result = cr_silhouettes(cr_object(params['object_name']), params['views'], params['rows'], params['regions'])
'''

SURFACE = CHAR_RUNTIME + r'''
result = cr_surface(cr_object(params['object_name']), params['options'], params['regions'])
'''

ASYMMETRY = CHAR_RUNTIME + r'''
result = cr_asymmetry(cr_object(params['object_name']), params['regions'], params['intentional'], params['tolerance_fraction'])
'''

INTERSECTIONS = CHAR_RUNTIME + r'''
result = cr_intersections(cr_object(params['object_name']), params['regions'], params['reference'])
'''

REGIONS = CHAR_RUNTIME + r'''
obj = cr_object(params['object_name'])
result = {'regions': cr_infer_regions(obj, cr_arrays(obj)), 'topology_fingerprint': ms_topology(obj.data)['fingerprint']}
'''

BONES = CHAR_RUNTIME + r'''
obj = cr_object(params['object_name'])
rigs = [m.object for m in obj.modifiers if m.type == 'ARMATURE' and m.object is not None and m.object.type == 'ARMATURE']
bones = {}
if rigs:
    factor = bpy.context.scene.unit_settings.scale_length * rigs[0].matrix_world.to_scale()[0]
    for bone in rigs[0].data.bones:
        vector = bone.tail_local - bone.head_local
        length = vector.length
        bones[bone.name] = {'length_m': length * factor, 'region': cr_token_region(bone.name), 'side': cr_side(bone.name),
                            'vertical_fraction': abs(vector.z) / length if length > 0 else 0.0, 'deform': bone.use_deform}
result = {'rig': rigs[0].name if rigs else None, 'bones': bones}
'''
