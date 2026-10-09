"""Shared Blender geometry primitives for M5, all in world metres on the base mesh.

The arrays and per-vertex correspondence stay inside Blender.  Callers concatenate this runtime
after common.m5.M5_RUNTIME.  m5_geo(obj) provides points, triangle/loop/polygon mappings and connected
component IDs; m5_bvh accepts that dictionary or a mesh object.  m5_correspondence returns an ordered
local-only row for every low vertex.  m5_compare returns bounded, reproducible sampled summaries;
it deliberately does not claim a Hausdorff distance.
"""

GEOMETRY_RUNTIME = r'''
import math
from array import array
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

def mg_quantiles(values):
    values = sorted(values)
    if not values:
        return {'count': 0, 'mean': None, 'median': None, 'p95': None, 'p99': None, 'maximum': None}
    def q(f):
        at = (len(values) - 1) * f
        low = int(at)
        high = min(low + 1, len(values) - 1)
        return values[low] * (high - at) + values[high] * (at - low) if high != low else values[low]
    return {'count': len(values), 'mean': sum(values) / len(values), 'median': q(.5),
            'p95': q(.95), 'p99': q(.99), 'maximum': values[-1]}

def mg_components(me):
    parents = array('i', range(len(me.vertices)))
    ranks = bytearray(len(me.vertices))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    ends = array('i', [0]) * (len(me.edges) * 2)
    me.edges.foreach_get('vertices', ends)
    for at in range(0, len(ends), 2):
        a, b = find(ends[at]), find(ends[at + 1])
        if a != b:
            if ranks[a] < ranks[b]: a, b = b, a
            parents[b] = a
            if ranks[a] == ranks[b]: ranks[a] += 1
    names = {}
    components = array('i', [0]) * len(me.vertices)
    for i in range(len(parents)):
        root = find(i)
        if root not in names: names[root] = len(names)
        components[i] = names[root]
    return components, len(names)

def m5_geo(obj):
    me = obj.data
    unit = bpy.context.scene.unit_settings.scale_length
    if not math.isfinite(unit) or unit <= 0:
        raise ValueError('INVALID_UNITS: positive finite scale_length required')
    raw = array('f', [0.0]) * (len(me.vertices) * 3)
    me.vertices.foreach_get('co', raw)
    matrix = obj.matrix_world
    points = [(matrix @ Vector((raw[i], raw[i + 1], raw[i + 2]))) * unit for i in range(0, len(raw), 3)]
    me.calc_loop_triangles()
    ids = array('i', [0]) * (len(me.loop_triangles) * 3)
    me.loop_triangles.foreach_get('vertices', ids)
    loops = array('i', [0]) * (len(me.loop_triangles) * 3)
    me.loop_triangles.foreach_get('loops', loops)
    polygon_ids = array('i', [0]) * len(me.loop_triangles)
    me.loop_triangles.foreach_get('polygon_index', polygon_ids)
    triangles = [tuple(ids[i:i + 3]) for i in range(0, len(ids), 3)]
    triangle_loops = [tuple(loops[i:i + 3]) for i in range(0, len(loops), 3)]
    components, count = mg_components(me)
    polygon_components = array('i', [0]) * len(me.polygons)
    for poly in me.polygons:
        polygon_components[poly.index] = components[poly.vertices[0]] if poly.vertices else -1
    return {'object': obj.name, 'points': points, 'triangles': triangles, 'triangle_loops': triangle_loops,
            'triangle_polygons': polygon_ids, 'vertex_components': components,
            'polygon_components': polygon_components, 'component_count': count, 'unit_scale': unit}

def m5_bvh(value):
    geo = value if isinstance(value, dict) else m5_geo(value)
    if not geo['triangles']:
        raise ValueError('EMPTY_SURFACE: a mesh with triangles is required')
    return BVHTree.FromPolygons(geo['points'], geo['triangles'], all_triangles=True)

def m5_barycentric(point, a, b, c):
    ab, ac, ap = b - a, c - a, point - a
    aa, bb, cc = ab.dot(ab), ab.dot(ac), ac.dot(ac)
    d, e = ap.dot(ab), ap.dot(ac)
    determinant = aa * cc - bb * bb
    if determinant <= max(1e-30, aa * cc * 1e-14):
        raise ValueError('DEGENERATE_CORRESPONDENCE: source triangle is singular')
    v, w = (cc * d - bb * e) / determinant, (aa * e - bb * d) / determinant
    weights = [max(0.0, min(1.0, 1 - v - w)), max(0.0, min(1.0, v)), max(0.0, min(1.0, w))]
    total = sum(weights)
    return [value / total for value in weights]

def mg_triangle_normal(geo, triangle):
    a, b, c = [geo['points'][v] for v in geo['triangles'][triangle]]
    normal = (b - a).cross(c - a)
    return normal.normalized() if normal.length > 1e-15 else Vector((0, 0, 0))

def mg_angle(a, b):
    if a.length < 1e-15 or b.length < 1e-15:
        return 180.0
    return math.degrees(math.acos(max(-1.0, min(1.0, a.normalized().dot(b.normalized())))))

def m5_correspondence(high, low, options):
    source, target = m5_geo(high), m5_geo(low)
    tree = m5_bvh(source)
    maximum = float(options.get('max_distance_m', .01))
    normal_limit = float(options.get('max_normal_angle_degrees', 80.0))
    ambiguity = float(options.get('ambiguity_distance_m', .0001))
    reject = options.get('reject_ambiguous_components', True)
    normal_matrix = low.matrix_world.to_3x3().inverted().transposed()
    rows = []
    for i, point in enumerate(target['points']):
        found = tree.find_nearest(point, maximum)
        if found is None or found[0] is None:
            raise ValueError('SURFACE_DISTANCE_EXCEEDED: no corresponding high surface within ' + str(maximum) + ' m at vertex ' + str(i))
        hit, normal, triangle, distance = found
        verts = source['triangles'][triangle]
        component = source['vertex_components'][verts[0]]
        low_normal = normal_matrix @ low.data.vertices[i].normal
        angle = mg_angle(low_normal, normal)
        if angle > normal_limit:
            raise ValueError('NORMAL_CORRESPONDENCE_REJECTED: ' + str(angle) + ' degrees at vertex ' + str(i))
        if reject and source['component_count'] > 1:
            for alternative in tree.find_nearest_range(point, distance + ambiguity):
                other = source['vertex_components'][source['triangles'][alternative[2]][0]]
                if other != component:
                    raise ValueError('AMBIGUOUS_SURFACE_CORRESPONDENCE: another source component is equally near vertex ' + str(i))
        bary = m5_barycentric(hit, *[source['points'][v] for v in verts])
        rows.append({'triangle': triangle, 'vertices': list(verts), 'loops': list(source['triangle_loops'][triangle]),
                     'polygon': int(source['triangle_polygons'][triangle]), 'barycentric': bary, 'point': list(hit),
                     'distance_m': float(distance), 'normal_angle_degrees': angle, 'component': int(component)})
    return rows

def mg_samples(geo, requested):
    count = min(requested, max(len(geo['triangles']), 1))
    samples = []
    for i in range(count):
        triangle = min(len(geo['triangles']) - 1, int((i + .5) * len(geo['triangles']) / count))
        a, b, c = [geo['points'][v] for v in geo['triangles'][triangle]]
        u, v = ((i + 1) * .6180339887498949) % 1, ((i + 1) * .4142135623730951) % 1
        if u + v > 1: u, v = 1 - u, 1 - v
        samples.append((a * (1 - u - v) + b * u + c * v, mg_triangle_normal(geo, triangle), triangle))
    return samples

def m5_compare(high, low, options):
    source, target = m5_geo(high), m5_geo(low)
    source_tree, target_tree = m5_bvh(source), m5_bvh(target)
    sample_count = int(options.get('samples', 2048))
    rows, all_distances, all_angles = [], [], []
    region_rows = []
    for region in options.get('regions', []):
        region_rows.append({'name': region['name'], 'members': set(mr_region_indices(high, high.data, region['definition'])),
                            'distances': [], 'angles': []})
    for label, geo, tree in [('high_to_low', source, target_tree), ('low_to_high', target, source_tree)]:
        distances, angles = [], []
        for point, normal, triangle in mg_samples(geo, sample_count):
            found = tree.find_nearest(point)
            distances.append(float(found[3]))
            angles.append(mg_angle(normal, found[1]))
            source_triangle = triangle if label == 'high_to_low' else found[2]
            source_members = source['triangles'][source_triangle]
            for region in region_rows:
                if sum(v in region['members'] for v in source_members) >= 2:
                    region['distances'].append(distances[-1]); region['angles'].append(angles[-1])
        rows.append({'direction': label, 'surface_m': mg_quantiles(distances), 'normal_angle_degrees': mg_quantiles(angles)})
        all_distances.extend(distances)
        all_angles.extend(angles)
    metrics = {'surface_m': mg_quantiles(all_distances), 'normal_angle_degrees': mg_quantiles(all_angles),
               'directions': rows, 'sampling': {'method': 'deterministic triangle-index strata and low-discrepancy barycentric points',
                                               'requested_per_direction': sample_count, 'hausdorff': False}}
    metrics['regions'] = [{'region': row['name'], 'surface_m': mg_quantiles(row['distances']),
                          'normal_angle_degrees': mg_quantiles(row['angles'])} for row in region_rows]
    limits = {'max_distance_m': float(options.get('max_distance_m', .01)),
              'max_normal_angle_degrees': float(options.get('max_normal_angle_degrees', 80.0))}
    errors = []
    if metrics['surface_m']['maximum'] is None or metrics['surface_m']['maximum'] > limits['max_distance_m']:
        errors.append('SURFACE_DEVIATION_EXCEEDED')
    if metrics['normal_angle_degrees']['maximum'] is None or metrics['normal_angle_degrees']['maximum'] > limits['max_normal_angle_degrees']:
        errors.append('NORMAL_DEVIATION_EXCEEDED')
    return {'passed': not errors, 'metrics': metrics, 'thresholds': limits, 'errors': errors,
            'warnings': ['Sampled two-way surface deviation is not a full Hausdorff distance; very small unsampled features may be missed.']}
'''
