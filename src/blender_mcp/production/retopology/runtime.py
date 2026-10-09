"""M5 audits, priority-guided reduction, source projection and fidelity verification.

All geometry is base/bind geometry in world metres.  Decimate is an explicit strategy, never an
implicit fallback.  Zero reduction weights protect regions and their edge boundary; every protected
source vertex is independently matched by position, with weights checked by group name.  A rejected
candidate remains available for inspection and the transaction never replaces the previous low.
"""
from ..common.m5 import M5_RUNTIME
from .helpers import GEOMETRY_RUNTIME

RETOPOLOGY_RUNTIME = M5_RUNTIME + GEOMETRY_RUNTIME + r'''
RT_VIEWS = {'FRONT': (1, 0, 0), 'SIDE': (0, 1, 0), 'THREE_QUARTER': (.7071067811865476, .7071067811865476, 0),
            'BACK': (-1, 0, 0)}

def rt_exact_protected_triangulation(candidate, weights):
    me = candidate.data
    wanted = []
    for polygon in me.polygons:
        if polygon.loop_total == 4 and all(weights[v] == 0 for v in polygon.vertices):
            points = [candidate.matrix_world @ me.vertices[v].co for v in polygon.vertices]
            normal = (points[1] - points[0]).cross(points[2] - points[0])
            if normal.length > 1e-15 and abs((points[3] - points[0]).dot(normal.normalized())) * bpy.context.scene.unit_settings.scale_length > 1e-7:
                wanted.append(polygon.index)
    if not wanted: return 0
    me.calc_loop_triangles()
    requested = set(wanted)
    diagonals = {}
    for triangle in me.loop_triangles:
        if triangle.polygon_index in requested:
            ids = set(triangle.vertices)
            if triangle.polygon_index in diagonals: diagonals[triangle.polygon_index] &= ids
            else: diagonals[triangle.polygon_index] = ids
    import bmesh
    bm = bmesh.new()
    try:
        bm.from_mesh(me); bm.faces.ensure_lookup_table(); bm.verts.index_update()
        fixed, alternate = [], []
        for index in wanted:
            face = bm.faces[index]
            diagonal = {face.verts[0].index, face.verts[2].index}
            if diagonal == diagonals[index]: fixed.append(face)
            else: alternate.append(face)
        if fixed: bmesh.ops.triangulate(bm, faces=fixed, quad_method='FIXED')
        if alternate: bmesh.ops.triangulate(bm, faces=alternate, quad_method='ALTERNATE')
        bm.to_mesh(me)
        me.update()
    finally:
        bm.free()
    return len(wanted)

def rt_regions(obj, options, relation=None):
    baseline = (relation or {}).get('metadata', {}).get('regions', [])
    merged = {row['name']: row for row in baseline}
    requested = options.get('regions')
    for row in requested if requested is not None else []:
        previous = merged.get(row['name'])
        if previous and (previous.get('protected') or previous.get('role') == 'locked' or previous.get('deformation', False)):
            if (row['definition'] != previous['definition'] or
                not (row.get('protected') or row.get('role') == 'locked' or row.get('deformation', False)) or
                row.get('tolerance_m', 1e-5) > previous.get('tolerance_m', 1e-5)):
                raise ValueError('PROTECTED_REGION_OVERRIDE_REJECTED:' + row['name'])
        merged[row['name']] = row
    rows = list(merged.values())
    result = []
    for row in rows:
        definition = row['definition']
        members = set(mr_region_indices(obj, obj.data, definition))
        if not members:
            raise ValueError('REGION_EMPTY: ' + row['name'])
        result.append(dict(row, members=members))
    return result

def rt_topology(obj, options):
    me = obj.data
    geo = m5_geo(obj)
    edge_faces = array('i', [0]) * len(me.edges)
    edge_direction = array('i', [0]) * len(me.edges)
    valence = array('i', [0]) * len(me.vertices)
    for edge in me.edges:
        for index in edge.vertices: valence[index] += 1
    for polygon in me.polygons:
        ids = list(polygon.vertices)
        for i, loop in enumerate(polygon.loop_indices):
            edge = me.loops[loop].edge_index
            edge_faces[edge] += 1
            edge_direction[edge] += 1 if ids[i] < ids[(i + 1) % len(ids)] else -1
    area_epsilon = options.get('area_epsilon_m2', 1e-14)
    sliver_quality = options.get('minimum_triangle_quality', .02)
    areas, qualities, aspects = [], [], []
    samples = {'zero_area_triangles': [], 'sliver_triangles': [], 'non_manifold_edges': [], 'boundary_edges': []}
    volume = [0.0] * geo['component_count']
    component_closed = [True] * geo['component_count']
    for edge in me.edges:
        count = edge_faces[edge.index]
        if count != 2: component_closed[geo['vertex_components'][edge.vertices[0]]] = False
        if count > 2 and len(samples['non_manifold_edges']) < 30: samples['non_manifold_edges'].append(edge.index)
        if count == 1 and len(samples['boundary_edges']) < 30: samples['boundary_edges'].append(edge.index)
    zero_area, slivers = 0, 0
    for t, ids in enumerate(geo['triangles']):
        a, b, c = [geo['points'][v] for v in ids]
        ab, bc, ca = (b - a).length, (c - b).length, (a - c).length
        area = (b - a).cross(c - a).length * .5
        quality = 4 * math.sqrt(3) * area / max(ab * ab + bc * bc + ca * ca, 1e-30)
        longest = max(ab, bc, ca)
        aspect = longest * longest / max(2 * area, 1e-30)
        areas.append(area); qualities.append(quality); aspects.append(aspect)
        volume[geo['vertex_components'][ids[0]]] += a.dot(b.cross(c)) / 6
        if area <= area_epsilon:
            zero_area += 1
            if len(samples['zero_area_triangles']) < 30: samples['zero_area_triangles'].append(t)
        elif quality < sliver_quality:
            slivers += 1
            if len(samples['sliver_triangles']) < 30: samples['sliver_triangles'].append(t)
    duplicate_tolerance = options.get('duplicate_distance_m', 1e-7)
    cells, duplicates = {}, 0
    for i, point in enumerate(geo['points']):
        key = tuple(int(round(v / duplicate_tolerance)) for v in point)
        if key in cells: duplicates += 1
        else: cells[key] = i
    totals = [p.loop_total for p in me.polygons]
    boundary = sum(1 for count in edge_faces if count == 1)
    non_manifold = sum(1 for count in edge_faces if count > 2)
    inconsistent = sum(1 for i, count in enumerate(edge_faces) if count == 2 and abs(edge_direction[i]) == 2)
    inverted = [i for i, value in enumerate(volume) if component_closed[i] and value < -area_epsilon]
    summary = {'vertices': len(me.vertices), 'edges': len(me.edges), 'polygons': len(me.polygons),
               'triangles': len(geo['triangles']), 'triangle_faces': totals.count(3), 'quads': totals.count(4),
               'ngons': sum(1 for n in totals if n > 4), 'boundary_edges': boundary,
               'non_manifold_edges': non_manifold, 'wire_edges': sum(1 for c in edge_faces if c == 0),
               'isolated_vertices': sum(1 for v in valence if v == 0), 'zero_area_triangles': zero_area,
               'sliver_triangles': slivers, 'duplicate_vertex_indicators': duplicates,
               'inconsistent_normal_edges': inconsistent, 'inward_closed_components': inverted,
               'connected_components': geo['component_count'], 'surface_area_m2': sum(areas),
               'triangle_quality': mg_quantiles(qualities), 'triangle_aspect_ratio': mg_quantiles(aspects),
               'edge_valence': mg_quantiles(list(valence)), 'extraordinary_valence_6_plus': sum(1 for v in valence if v >= 6),
               'component_volumes_m3': volume[:32], 'problem_samples': samples,
               'thresholds': {'area_epsilon_m2': area_epsilon, 'minimum_triangle_quality': sliver_quality,
                              'duplicate_distance_m': duplicate_tolerance}}
    errors = []
    if not geo['triangles']: errors.append('EMPTY_SURFACE')
    if non_manifold: errors.append('NON_MANIFOLD_GEOMETRY')
    if zero_area: errors.append('DEGENERATE_TRIANGLES')
    if inconsistent or inverted: errors.append('INCONSISTENT_OR_INVERTED_NORMALS')
    if not options.get('allow_boundary', False) and boundary: errors.append('OPEN_BOUNDARY_OR_HOLE')
    if slivers > options.get('maximum_sliver_triangles', 0): errors.append('SLIVER_TRIANGLES')
    if summary['wire_edges'] or summary['isolated_vertices']: errors.append('ISOLATED_GEOMETRY')
    if geo['component_count'] > options.get('maximum_components', 128): errors.append('TOO_MANY_COMPONENTS')
    return {'passed': not errors, 'metrics': summary, 'errors': errors,
            'warnings': ['Boundary edges indicate open surfaces or holes; holes are not classified as defects when allow_boundary=true.',
                         'Duplicate vertices use quantized-position indicators; intentional split vertices require review.',
                         'Outward orientation is inferred only for closed components; it is not an arbitrary flipped-face classifier.']}

def rt_density(obj, options, relation=None):
    geo, me = m5_geo(obj), obj.data
    rows = rt_regions(obj, options, relation)
    counts = {r['name']: {'triangles': 0, 'surface_area_m2': 0.0, 'curvature_degrees': []} for r in rows}
    areas = []
    cell_data = {}
    resolution = options.get('density_grid', 8)
    low = [min(p[k] for p in geo['points']) for k in range(3)]
    high = [max(p[k] for p in geo['points']) for k in range(3)]
    spans = [max(high[k] - low[k], 1e-9) for k in range(3)]
    for t, ids in enumerate(geo['triangles']):
        a, b, c = [geo['points'][v] for v in ids]
        area = (b - a).cross(c - a).length * .5
        areas.append(area)
        center = (a + b + c) / 3
        key = tuple(min(resolution - 1, max(0, int((center[k] - low[k]) / spans[k] * resolution))) for k in range(3))
        cell = cell_data.setdefault(key, [0, 0.0])
        cell[0] += 1; cell[1] += area
        for row in rows:
            if sum(v in row['members'] for v in ids) >= 2:
                counts[row['name']]['triangles'] += 1
                counts[row['name']]['surface_area_m2'] += area
    edge_lengths = []
    curvature = [[] for row in rows]
    edge_polygons = [[] for edge in me.edges]
    for polygon in me.polygons:
        for loop in polygon.loop_indices: edge_polygons[me.loops[loop].edge_index].append(polygon.index)
    normal_matrix = obj.matrix_world.to_3x3().inverted().transposed()
    polygon_normals = [normal_matrix @ polygon.normal for polygon in me.polygons]
    for edge in me.edges:
        a, b = edge.vertices
        length = (geo['points'][a] - geo['points'][b]).length
        edge_lengths.append(length)
        neighbors = edge_polygons[edge.index]
        if len(neighbors) == 2:
            angle = mg_angle(polygon_normals[neighbors[0]], polygon_normals[neighbors[1]])
            for row in rows:
                if a in row['members'] and b in row['members']: counts[row['name']]['curvature_degrees'].append(angle)
    region_rows = []
    for row in rows:
        metrics = counts[row['name']]
        area = metrics['surface_area_m2']
        region_rows.append({'region': row['name'], 'triangles': metrics['triangles'], 'surface_area_m2': area,
                            'triangles_per_m2': metrics['triangles'] / area if area > 0 else None,
                            'curvature_degrees': mg_quantiles(metrics['curvature_degrees']),
                            'deformation_priority': row.get('deformation', False), 'visual_priority': row.get('priority', 1.0),
                            'silhouette_priority': row.get('silhouette_priority', 1.0), 'protected': row.get('protected', False)})
    densities = [count / area for count, area in cell_data.values() if area > 1e-14]
    distribution = mg_quantiles(densities)
    uniformity = distribution['p95'] / max(distribution['median'], 1e-12) if densities else None
    return {'object': obj.name, 'triangles': len(geo['triangles']), 'surface_area_m2': sum(areas),
            'triangles_per_m2': len(geo['triangles']) / max(sum(areas), 1e-30), 'edge_length_m': mg_quantiles(edge_lengths),
            'density_cell_distribution': distribution, 'density_grid': resolution, 'density_cells': len(cell_data),
            'p95_to_median_density_ratio': uniformity, 'regions': region_rows,
            'warnings': ['Local density is triangles per actual surface area inside a world-space grid; overlapping regions are reported separately.',
                         'Density variation is measured, not judged universally undesirable. Curvature is adjacent-face dihedral angle.']}

def rt_priority(obj, options, relation=None):
    me = obj.data
    rows = rt_regions(obj, options, relation)
    weight = [1.0] * len(me.vertices)
    protected = set()
    for row in rows:
        priority = row.get('priority', 1.0) * row.get('silhouette_priority', 1.0) * row.get('budget', {}).get('density_weight', 1.0)
        for v in row['members']: weight[v] = max(weight[v], priority)
        if row.get('protected') or row.get('role') == 'locked' or row.get('deformation', False): protected.update(row['members'])
    edge_polygons = [[] for edge in me.edges]
    for polygon in me.polygons:
        for loop in polygon.loop_indices: edge_polygons[me.loops[loop].edge_index].append(polygon.index)
    normal_matrix = obj.matrix_world.to_3x3().inverted().transposed()
    normals = [normal_matrix @ polygon.normal for polygon in me.polygons]
    curvature_weight = options.get('curvature_weight', 2.0)
    material_boundaries = 0
    for edge in me.edges:
        a, b = edge.vertices
        faces = edge_polygons[edge.index]
        if len(faces) != 2 or edge.use_edge_sharp:
            protected.update((a, b))
        if len(faces) == 2:
            if me.polygons[faces[0]].material_index != me.polygons[faces[1]].material_index:
                protected.update((a, b)); material_boundaries += 1
            angle = mg_angle(normals[faces[0]], normals[faces[1]]) / 180
            importance = 1 + curvature_weight * angle
            weight[a] = max(weight[a], importance); weight[b] = max(weight[b], importance)
        for row in rows:
            if (a in row['members']) != (b in row['members']) and options.get('preserve_region_boundaries', True):
                protected.update((a, b))
    reduction = [0.0 if i in protected else 1.0 / weight[i] for i in range(len(weight))]
    minimum = sum(p.loop_total - 2 for p in me.polygons if all(v in protected for v in p.vertices))
    return {'weights': reduction, 'protected': protected, 'regions': rows,
            'summary': {'method': 'explicit semantic/curvature priority, zero-weight protected and boundary vertices',
                        'vertices': len(weight), 'protected_vertices': len(protected), 'editable_vertices': len(weight) - len(protected),
                        'protected_triangle_floor': minimum, 'material_boundary_edges': material_boundaries,
                        'reduction_weight': mg_quantiles(reduction),
                        'regions': [{'name': row['name'], 'vertices': len(row['members']), 'priority': row.get('priority', 1.0),
                                    'protected': bool(row.get('protected') or row.get('role') == 'locked'),
                                    'deformation': row.get('deformation', False)} for row in rows]}}

def rt_budget(obj, options, relation=None):
    count = sum(p.loop_total - 2 for p in obj.data.polygons)
    priority = rt_priority(obj, options, relation)
    target = options.get('target_triangles', count)
    if 'target_reduction' in options:
        region = next((row for row in priority['regions'] if row['name'] == options.get('region')), None)
        if region is None:
            raise ValueError('REGION_REQUIRED: target_reduction needs an explicit editable region')
        region_triangles = sum(p.loop_total - 2 for p in obj.data.polygons if sum(v in region['members'] for v in p.vertices) >= 2)
        target = max(1, int(round(count - region_triangles * options['target_reduction'])))
    minimum = options.get('minimum_triangles', max(1, int(math.floor(target * .95))))
    maximum = options.get('maximum_triangles', max(target, int(math.ceil(target * 1.05))))
    floor = priority['summary']['protected_triangle_floor']
    errors = []
    if not minimum <= target <= maximum: errors.append('INVALID_BUDGET_RANGE')
    if floor > maximum: errors.append('BUDGET_IMPOSSIBLE_PROTECTED_REGIONS')
    if target > count: errors.append('BUDGET_REQUIRES_GEOMETRY_CREATION')
    region_budgets = []
    density = rt_density(obj, options, relation) if priority['regions'] else {'regions': []}
    region_count = {r['region']: r for r in density['regions']}
    weighted = []
    for row in priority['regions']:
        current = region_count[row['name']]['triangles']
        rule = row.get('budget', {})
        if 'absolute' in rule: allocation = rule['absolute']
        elif 'percentage' in rule: allocation = int(round(target * rule['percentage'] / 100))
        else:
            weighted.append((row['name'], current * row.get('priority', 1.0) * rule.get('density_weight', 1.0)))
            allocation = None
        region_budgets.append({'region': row['name'], 'current_triangles': current, 'suggested_triangles': allocation,
                               'protected': row.get('protected', False) or row.get('deformation', False), 'rule': rule})
    fixed = sum(r['suggested_triangles'] or 0 for r in region_budgets)
    total_weight = sum(weight for name, weight in weighted)
    for row in region_budgets:
        if row['suggested_triangles'] is None:
            weight = next(value for name, value in weighted if name == row['region'])
            row['suggested_triangles'] = int(round(max(0, target - fixed) * weight / max(total_weight, 1e-30)))
        if row['protected']: row['suggested_triangles'] = max(row['suggested_triangles'], row['current_triangles'])
    return {'supported': not errors, 'errors': errors, 'current_triangles': count, 'target_triangles': target,
            'minimum_triangles': minimum, 'maximum_triangles': maximum, 'protected_triangle_floor': floor,
            'reduction_ratio': target / max(count, 1), 'region_budgets': region_budgets,
            'warnings': ['Region allocations are proposals; overlapping semantic regions cannot be summed as independent geometry.']}

def rt_silhouette(high, low, options):
    source, target = m5_geo(high), m5_geo(low)
    source_tree, target_tree = m5_bvh(source), m5_bvh(target)
    resolution = options.get('silhouette_resolution', 128)
    combined = source['points'] + target['points']
    output = []
    for view in options.get('views', ['FRONT', 'SIDE', 'THREE_QUARTER']):
        right = Vector(RT_VIEWS[view]); up = Vector((0, 0, 1)); direction = right.cross(up).normalized()
        u = [p.dot(right) for p in combined]; z = [p.z for p in combined]; d = [p.dot(direction) for p in combined]
        left, bottom = min(u), min(z)
        cell = max(max(u) - left, max(z) - bottom, 1e-6) / (resolution - 4)
        left -= 2 * cell; bottom -= 2 * cell
        depth_low, depth_high = min(d) - 2 * cell, max(d) + 2 * cell
        masks = []
        for tree in (source_tree, target_tree):
            mask = bytearray(resolution * resolution)
            for row in range(resolution):
                for col in range(resolution):
                    origin = right * (left + (col + .5) * cell) + up * (bottom + (row + .5) * cell) + direction * depth_low
                    hit = tree.ray_cast(origin, direction, depth_high - depth_low)
                    mask[row * resolution + col] = 1 if hit[0] is not None else 0
            masks.append(mask)
        a, b = masks
        intersection = sum(1 for x, y in zip(a, b) if x and y)
        union = sum(1 for x, y in zip(a, b) if x or y)
        disagreement = sum(1 for x, y in zip(a, b) if x != y)
        boundaries = []
        for mask in masks:
            points = []
            for row in range(1, resolution - 1):
                for col in range(1, resolution - 1):
                    i = row * resolution + col
                    if mask[i] and any(not mask[j] for j in (i - 1, i + 1, i - resolution, i + resolution)):
                        points.append(Vector((col * cell, row * cell, 0)))
            boundaries.append(points)
        distances = []
        for source_boundary, target_boundary in (boundaries, reversed(boundaries)):
            if not target_boundary: continue
            tree = KDTree(len(target_boundary))
            for i, point in enumerate(target_boundary): tree.insert(point, i)
            tree.balance()
            distances.extend(tree.find(point)[2] for point in source_boundary)
        output.append({'view': view, 'iou': intersection / union if union else 0.0,
                       'symmetric_difference_area_m2': disagreement * cell * cell,
                       'source_area_m2': sum(a) * cell * cell, 'target_area_m2': sum(b) * cell * cell,
                       'boundary_distance_m': mg_quantiles(distances), 'cell_m': cell, 'resolution': resolution})
    maximum = options.get('maximum_silhouette_distance_m', .02)
    minimum_iou = options.get('minimum_silhouette_iou', .95)
    errors = []
    for row in output:
        if row['boundary_distance_m']['maximum'] is None or row['boundary_distance_m']['maximum'] > maximum:
            errors.append('SILHOUETTE_DEVIATION_EXCEEDED:' + row['view'])
        if row['iou'] < minimum_iou: errors.append('SILHOUETTE_IOU_BELOW_LIMIT:' + row['view'])
    return {'passed': not errors, 'metrics': {'views': output}, 'errors': errors,
            'thresholds': {'maximum_distance_m': maximum, 'minimum_iou': minimum_iou},
            'warnings': ['Equivalent orthographic ray masks use the same bounds and cell size. Errors below one pixel cannot be resolved.']}

def rt_vertex_semantics(obj):
    me = obj.data
    materials = [set() for v in me.vertices]
    names = [slot.material.name if slot.material else None for slot in obj.material_slots]
    for polygon in me.polygons:
        name = names[polygon.material_index] if polygon.material_index < len(names) else None
        for v in polygon.vertices: materials[v].add(name)
    layers = {}
    for layer in me.uv_layers:
        values = array('f', [0.0]) * (len(me.loops) * 2)
        layer.uv.foreach_get('vector', values)
        rows = [set() for v in me.vertices]
        for loop in me.loops:
            rows[loop.vertex_index].add((round(values[2 * loop.index], 6), round(values[2 * loop.index + 1], 6)))
        layers[layer.name] = rows
    return materials, layers

def rt_protected(high, candidate, options, relation=None):
    source_geo, target_geo = m5_geo(high), m5_geo(candidate)
    target_surface = m5_bvh(target_geo)
    tree = KDTree(len(target_geo['points']))
    for i, point in enumerate(target_geo['points']): tree.insert(point, i)
    tree.balance()
    source_names = [g.name for g in high.vertex_groups]
    target_names = [g.name for g in candidate.vertex_groups]
    source_materials, source_uv = rt_vertex_semantics(high)
    target_materials, target_uv = rt_vertex_semantics(candidate)
    rows, errors = [], []
    for row in rt_regions(high, options, relation):
        if not (row.get('protected') or row.get('role') == 'locked' or row.get('deformation', False)): continue
        tolerance = row.get('tolerance_m', 1e-5)
        worst, weights, material_errors, uv_errors, surface_worst, surface_samples = 0.0, 0.0, 0, 0, 0.0, 0
        for i in row['members']:
            point, j, distance = tree.find(source_geo['points'][i])
            worst = max(worst, distance)
            a = {source_names[e.group]: e.weight for e in high.data.vertices[i].groups if e.group < len(source_names)}
            b = {target_names[e.group]: e.weight for e in candidate.data.vertices[j].groups if e.group < len(target_names)}
            weights = max([weights] + [abs(a.get(name, 0) - b.get(name, 0)) for name in set(a) | set(b)])
            if source_materials[i] != target_materials[j]: material_errors += 1
            if any(name not in target_uv or layer[i] != target_uv[name][j] for name, layer in source_uv.items()): uv_errors += 1
        for ids in source_geo['triangles']:
            if all(v in row['members'] for v in ids):
                a, b, c = [source_geo['points'][v] for v in ids]
                for point in ((a + b + c) / 3, (a + b) / 2, (b + c) / 2, (c + a) / 2):
                    distance = target_surface.find_nearest(point)[3]
                    surface_worst = max(surface_worst, distance); surface_samples += 1
        passed = worst <= tolerance and surface_worst <= tolerance and weights <= options.get('protected_weight_tolerance', 1e-5) and not material_errors and not uv_errors
        rows.append({'region': row['name'], 'vertices': len(row['members']), 'max_vertex_displacement_m': worst,
                     'max_surface_deviation_m': surface_worst, 'surface_samples': surface_samples,
                     'max_weight_error': weights, 'material_mismatch_vertices': material_errors, 'uv_mismatch_vertices': uv_errors,
                     'uv_quantization': 1e-6, 'tolerance_m': tolerance, 'passed': passed})
        if not passed: errors.append('PROTECTED_REGION_MODIFIED:' + row['name'])
    return {'passed': not errors, 'metrics': {'regions': rows}, 'errors': errors}

def rt_plan(high, low, options, relation):
    strategy = options.get('strategy', 'GUIDED_DECIMATION')
    priority = rt_priority(low, options, relation)
    budget = rt_budget(low, options, relation)
    reasons = list(budget['errors'])
    if high.data.shape_keys or low.data.shape_keys:
        reasons.append('MORPH_TOPOLOGY_CHANGE_REQUIRES_MANUAL_OR_LATER_PIPELINE')
    if strategy not in ('GUIDED_DECIMATION', 'PRESERVE_AND_OPTIMIZE', 'LOCAL_RETOPOLOGY'):
        reasons.append('MANUAL_REQUIRED: strategy is not implemented by this conservative adapter')
    if strategy == 'PRESERVE_AND_OPTIMIZE' and budget['target_triangles'] != budget['current_triangles']:
        reasons.append('STRATEGY_BUDGET_MISMATCH: topology reduction requires explicit GUIDED_DECIMATION or LOCAL_RETOPOLOGY')
    if low.data.has_custom_normals and options.get('custom_normals_policy') != 'rebuild':
        reasons.append('CUSTOM_NORMALS_REBUILD_REQUIRES_EXPLICIT_POLICY')
    region = options.get('region')
    local_budget = None
    if region:
        selected = next((row for row in priority['regions'] if row['name'] == region), None)
        if selected is None: reasons.append('REGION_UNDEFINED:' + region)
        elif selected.get('protected') or selected.get('role') == 'locked' or selected.get('deformation', False):
            reasons.append('PROTECTED_REGION_EDIT_REJECTED:' + region)
        elif 'target_reduction' in options:
            current_region = sum(p.loop_total - 2 for p in low.data.polygons if sum(v in selected['members'] for v in p.vertices) >= 2)
            local_budget = {'region': region, 'current_triangles': current_region,
                            'target_triangles': int(round(current_region * (1 - options['target_reduction']))),
                            'tolerance_fraction': options.get('region_budget_tolerance_fraction', .05)}
    if not any(weight > 0 for weight in priority['weights']) and budget['target_triangles'] < budget['current_triangles']:
        reasons.append('BUDGET_IMPOSSIBLE_NO_EDITABLE_GEOMETRY')
    return {'supported': not reasons, 'reasons': reasons, 'strategy': strategy, 'apply': False,
            'reason': 'Explicit priority-guided collapse with unchanged protected/boundary vertices, followed by independent fidelity checks.',
            'budget': budget, 'local_budget': local_budget, 'priority': priority['summary'], 'region': region,
            'attribute_consequences': {'materials': 'slots retained; polygon assignments follow evaluated decimation',
                                       'weights': 'interpolated by Blender, then explicit transfer and QA recommended',
                                       'UV': 'evaluated corner interpolation, UV QA required',
                                       'morphs': 'topology change rejected when any shape keys exist',
                                       'custom_normals': 'rebuild only with explicit policy',
                                       'named_attributes': 'retained by evaluated-mesh data layers; semantic verification still required'},
            'risks': ['Triangle collapse does not create artist-directed quad loops.',
                      'Very low requested budgets may safely fail fidelity or protected-region checks.',
                      'Visual QA, deformation QA and bake preparation remain separate gates.']}

def rt_reduce(high, low, candidate, proposal, options, relation):
    if proposal['strategy'] == 'PRESERVE_AND_OPTIMIZE':
        return {'strategy': proposal['strategy'], 'input_triangles': proposal['budget']['current_triangles'],
                'output_triangles': sum(p.loop_total - 2 for p in candidate.data.polygons), 'changes': 'copy_only'}
    priority = rt_priority(low, options, relation)
    weights = priority['weights']
    selected = options.get('region')
    if selected:
        row = next(row for row in priority['regions'] if row['name'] == selected)
        members = row['members']
        weights = [weight if i in members else 0 for i, weight in enumerate(weights)]
    protected_triangles = rt_exact_protected_triangulation(candidate, weights)
    states = [(modifier, modifier.show_viewport) for modifier in candidate.modifiers]
    group = candidate.vertex_groups.new(name='__M5_REDUCTION_PRIORITY')
    group_name = group.name
    buckets = [[] for i in range(101)]
    for i, weight in enumerate(weights):
        if weight > 0: buckets[max(1, min(100, int(round(weight * 100))))].append(i)
    for bucket in range(1, 101):
        ids = buckets[bucket]
        if ids: group.add(ids, bucket / 100, 'REPLACE')
    modifier = None
    original = candidate.data
    try:
        for item, visible in states: item.show_viewport = False
        modifier = candidate.modifiers.new('M5_EXPLICIT_GUIDED_DECIMATION', 'DECIMATE')
        modifier.decimate_type = 'COLLAPSE'
        modifier.ratio = min(1.0, proposal['budget']['target_triangles'] / max(proposal['budget']['current_triangles'], 1))
        modifier.vertex_group = group.name
        modifier.vertex_group_factor = 1000
        modifier.use_collapse_triangulate = False
        modifier.use_symmetry = False
        graph = bpy.context.evaluated_depsgraph_get()
        edited = bpy.data.meshes.new_from_object(candidate.evaluated_get(graph), preserve_all_data_layers=True, depsgraph=graph)
        edited.name = original.name + '__M5_REDUCED'
        candidate.data = edited
        current_group = candidate.vertex_groups.get(group_name)
        if current_group is not None: candidate.vertex_groups.remove(current_group)
        group = None
    finally:
        if modifier is not None: candidate.modifiers.remove(modifier)
        current_group = candidate.vertex_groups.get(group_name)
        if current_group is not None: candidate.vertex_groups.remove(current_group)
        for item, visible in states: item.show_viewport = visible
    return {'strategy': proposal['strategy'], 'input_triangles': proposal['budget']['current_triangles'],
            'output_triangles': sum(p.loop_total - 2 for p in candidate.data.polygons),
            'protected_nonplanar_quads_exactly_tessellated': protected_triangles}

def rt_verify(high, low, candidate, proposal, options, relation):
    topology = rt_topology(candidate, options)
    surface_options = dict(options, regions=[{k: v for k, v in row.items() if k != 'members'} for row in rt_regions(high, options, relation)])
    surface = m5_compare(high, candidate, surface_options)
    silhouette = rt_silhouette(high, candidate, options)
    protected = rt_protected(high, candidate, options, relation)
    triangles = topology['metrics']['triangles']
    budget = proposal.get('budget')
    errors = topology['errors'] + surface['errors'] + silhouette['errors'] + protected['errors']
    outside = None
    if options.get('region'):
        selected = next(row for row in rt_regions(low, options, relation) if row['name'] == options['region'])
        untouched = [i for i in range(len(low.data.vertices)) if i not in selected['members']]
        if untouched:
            local_options = dict(options, regions=[{'name': 'OUTSIDE_EDITABLE_REGION', 'protected': True,
                'definition': {'method': 'indices', 'indices': untouched}, 'tolerance_m': 1e-5}])
            outside = rt_protected(low, candidate, local_options)
            errors.extend(outside['errors'])
    if budget and not budget['minimum_triangles'] <= triangles <= budget['maximum_triangles']:
        errors.append('TRIANGLE_BUDGET_NOT_MET')
    local_budget = proposal.get('local_budget')
    if local_budget:
        local_region = next(row for row in rt_regions(candidate, options, relation) if row['name'] == local_budget['region'])
        actual = sum(p.loop_total - 2 for p in candidate.data.polygons if sum(v in local_region['members'] for v in p.vertices) >= 2)
        goal, tolerance = local_budget['target_triangles'], local_budget['tolerance_fraction']
        if not (1 - tolerance) * goal <= actual <= (1 + tolerance) * goal:
            errors.append('LOCAL_TRIANGLE_BUDGET_NOT_MET:' + local_budget['region'])
    if (high.data.shape_keys is not None) and candidate.data.shape_keys is None:
        errors.append('MORPH_LOSS_REJECTED')
    regions = rt_regions(candidate, options, relation)
    if any('minimum_density_per_m2' in row or row.get('budget') for row in regions):
        density = rt_density(candidate, options, relation)
        for row in regions:
            metrics = next(value for value in density['regions'] if value['region'] == row['name'])
            if metrics['triangles_per_m2'] is None or metrics['triangles_per_m2'] < row.get('minimum_density_per_m2', 0):
                errors.append('MINIMUM_REGION_DENSITY_NOT_MET:' + row['name'])
            rule = row.get('budget', {})
            region_target = rule.get('absolute')
            if 'percentage' in rule and budget: region_target = budget['target_triangles'] * rule['percentage'] / 100
            tolerance = options.get('region_budget_tolerance_fraction', .05)
            if region_target is not None and not (1 - tolerance) * region_target <= metrics['triangles'] <= (1 + tolerance) * region_target:
                errors.append('REGION_TRIANGLE_BUDGET_NOT_MET:' + row['name'])
    return {'passed': not errors, 'metrics': {'triangles': triangles, 'topology': topology['metrics'],
                                             'surface': surface['metrics'], 'silhouette': silhouette['metrics'],
                                             'protected_regions': protected['metrics']['regions'],
                                             'outside_editable_region': outside['metrics']['regions'] if outside else []}, 'errors': errors,
            'warnings': surface['warnings'] + silhouette['warnings']}
'''

REDUCTION_RUNTIME = RETOPOLOGY_RUNTIME + r'''
def m5_plan(high, low, options, relation):
    return rt_plan(high, low, options, relation)

def m5_apply(high, low, candidate, proposal, options):
    return rt_reduce(high, low, candidate, proposal, options, params['relation'])

def m5_verify(high, low, candidate, proposal, options):
    return rt_verify(high, low, candidate, proposal, options, params['relation'])
'''

PROJECTION_RUNTIME = RETOPOLOGY_RUNTIME + r'''
def m5_plan(high, low, options, relation):
    reasons = []
    if low.data.shape_keys is not None:
        reasons.append('MORPH_PROJECTION_REQUIRES_MANUAL_OR_LATER_PIPELINE')
    if any(row.get('protected') or row.get('role') == 'locked' for row in rt_regions(high, options, relation)):
        pass
    try:
        rows = m5_correspondence(high, low, options)
        distances = mg_quantiles([row['distance_m'] for row in rows])
    except ValueError as exc:
        reasons.append(str(exc)); distances = None
    return {'supported': not reasons, 'reasons': reasons, 'strategy': 'BOUNDED_NEAREST_SURFACE_PROJECTION',
            'projection_distance_m': distances, 'apply': False,
            'risks': ['Normal, distance and competing source-component gates reject ambiguous correspondence.']}

def m5_apply(high, low, candidate, proposal, options):
    rows = m5_correspondence(high, low, options)
    unit = bpy.context.scene.unit_settings.scale_length
    inverse = candidate.matrix_world.inverted()
    source_geo = m5_geo(high)
    locked = set()
    for region in rt_regions(high, options, params['relation']):
        if region.get('protected') or region.get('role') == 'locked' or region.get('deformation', False): locked.update(region['members'])
    protected_tree = KDTree(len(locked)) if locked else None
    if protected_tree:
        for index, v in enumerate(sorted(locked)): protected_tree.insert(source_geo['points'][v], index)
        protected_tree.balance()
    moved = 0
    for vertex, row in zip(candidate.data.vertices, rows):
        old_world = (candidate.matrix_world @ vertex.co) * unit
        if protected_tree and protected_tree.find(old_world)[2] <= 1e-5:
            continue
        new_co = inverse @ (Vector(row['point']) / unit)
        if (new_co - vertex.co).length > 1e-10: moved += 1
        vertex.co = new_co
    candidate.data.update()
    return {'projected_vertices': moved, 'projection_distance_m': mg_quantiles([row['distance_m'] for row in rows])}

def m5_verify(high, low, candidate, proposal, options):
    return rt_verify(high, low, candidate, proposal, options, params['relation'])
'''
