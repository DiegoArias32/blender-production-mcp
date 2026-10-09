"""Blender-side M5 transfers. All modifications are restricted to transaction-created copies."""
from ..common.m5 import M5_RUNTIME
from ..retopology.helpers import GEOMETRY_RUNTIME

TRANSFER_RUNTIME = M5_RUNTIME + GEOMETRY_RUNTIME + r'''
import math
from mathutils import Vector, Quaternion
from mathutils.bvhtree import BVHTree

def mt_rig(obj):
    rigs = [m.object for m in obj.modifiers if m.type == 'ARMATURE' and m.object is not None]
    if len(set(r.name for r in rigs)) > 1:
        raise ValueError('MULTIPLE_ARMATURES_UNSUPPORTED: transfer requires one explicit armature')
    return rigs[0] if rigs else None

def mt_groups(obj, options):
    requested = options.get('groups', [])
    rig = mt_rig(obj)
    names = requested or ([b.name for b in rig.data.bones if b.use_deform and obj.vertex_groups.get(b.name)] if rig else [])
    if not names:
        raise ValueError('WEIGHT_SOURCE_REQUIRED: select explicit deformation groups or provide an armature with deform groups')
    missing = [n for n in names if obj.vertex_groups.get(n) is None]
    if missing:
        raise ValueError('WEIGHT_GROUP_MISSING: ' + ', '.join(missing))
    return names

def mt_weights(obj, names):
    index = {g.index: g.name for g in obj.vertex_groups if g.name in names}
    return [{index[e.group]: float(e.weight) for e in v.groups if e.group in index and e.weight > 0}
            for v in obj.data.vertices]

def mt_weight_report(obj, options):
    rig = mt_rig(obj)
    names = mt_groups(obj, options)
    weights = mt_weights(obj, names)
    sums = [sum(row.values()) for row in weights]
    counts = [sum(w > options['weight_epsilon'] for w in row.values()) for row in weights]
    tolerance = options['normalization_tolerance']
    empty = [i for i, total in enumerate(sums) if total <= options['weight_epsilon']]
    norm = [i for i, total in enumerate(sums) if abs(total - 1) > tolerance]
    invalid = [i for i, row in enumerate(weights) if any(not math.isfinite(w) or w < 0 or w > 1 + tolerance for w in row.values())]
    excessive = [i for i, count in enumerate(counts) if count > options['influence_limit']]
    deform = set(b.name for b in rig.data.bones if b.use_deform) if rig else set(names)
    unexpected = [n for n in names if n not in deform]
    jumps = []
    for edge in obj.data.edges:
        a, b = edge.vertices
        union = set(weights[a]) | set(weights[b])
        jump = sum(abs(weights[a].get(n, 0) - weights[b].get(n, 0)) for n in union)
        if jump > options['discontinuity_l1_limit']:
            jumps.append((edge.index, jump))
    leakage = []
    unit = bpy.context.scene.unit_settings.scale_length
    plane = options.get('side_plane', {})
    if options.get('side_pairs'):
        paired = {n for row in options['side_pairs'] for n in (row['positive'], row['negative'])}
        if paired - set(names):
            raise ValueError('SIDE_PAIR_GROUP_MISSING: pairs must refer to selected existing deformation groups')
        axis, offset, margin = plane.get('axis', 0), plane.get('offset_m', 0), plane.get('margin_m', 0.001)
        for vertex, row in zip(obj.data.vertices, weights):
            side = (obj.matrix_world @ vertex.co)[axis] * unit - offset
            for pair in options['side_pairs']:
                wrong = pair['positive'] if side < -margin else pair['negative'] if side > margin else None
                if wrong and row.get(wrong, 0) > options['leakage_weight_limit']:
                    leakage.append(vertex.index)
                    break
    errors = []
    for code, values in [('UNWEIGHTED_VERTICES', empty), ('WEIGHT_NORMALIZATION_ERROR', norm), ('INVALID_WEIGHTS', invalid),
                         ('EXCESSIVE_INFLUENCES', excessive), ('UNEXPECTED_DEFORM_GROUPS', unexpected), ('LEFT_RIGHT_WEIGHT_LEAKAGE', leakage)]:
        if values:
            errors.append(code + ': ' + str(len(values)))
    if jumps and options['reject_discontinuities']:
        errors.append('WEIGHT_DISCONTINUITY: ' + str(len(jumps)))
    return {'passed': not errors, 'vertices': len(weights), 'groups': names, 'unweighted_vertices': len(empty),
            'zero_sum_vertices': len(empty), 'normalization_errors': len(norm), 'invalid_weights': len(invalid),
            'maximum_influences': max(counts, default=0), 'average_influences': sum(counts)/max(1, len(counts)),
            'excessive_influence_vertices': len(excessive), 'influence_limit': options['influence_limit'],
            'maximum_normalization_error': max((abs(v-1) for v in sums), default=0), 'unexpected_bones': unexpected,
            'weight_discontinuity_indicators': len(jumps), 'maximum_edge_weight_l1': max((r[1] for r in jumps), default=0),
            'left_right_leakage_vertices': len(leakage), 'problem_vertices': sorted(set(empty+norm+invalid+excessive+leakage))[:options['limit']],
            'errors': errors, 'warnings': ['Weight discontinuities are indicators, not proof of incorrect skinning.'] if jumps else []}

def mt_weight_plan(high, low, options):
    names = mt_groups(high, options)
    source = mt_weight_report(high, dict(options, groups=names))
    if not source['passed']:
        return {'supported': False, 'errors': ['BAD_WEIGHT_SOURCE: ' + '; '.join(source['errors'])], 'source_weights': source}
    rig_a, rig_b = mt_rig(high), mt_rig(low)
    if rig_a is not rig_b:
        return {'supported': False, 'errors': ['ARMATURE_RELATIONSHIP_MISMATCH: source and target must share the same armature']}
    if any(low.vertex_groups.get(n) and low.vertex_groups[n].lock_weight for n in names):
        return {'supported': False, 'errors': ['LOCKED_WEIGHT_GROUP: explicit deformation groups cannot be replaced while locked']}
    rows = m5_correspondence(high, low, options)
    source_weights = mt_weights(high, names)
    predicted = []
    for row in rows:
        values = {}
        for index, factor in zip(row['vertices'], row['barycentric']):
            for name, weight in source_weights[index].items():
                values[name] = values.get(name, 0) + factor*weight
        predicted.append(values)
    return {'supported': True, 'groups': names, 'source_weights': source,
            'correspondence': {'vertices': len(rows), 'maximum_distance_m': max((r['distance_m'] for r in rows), default=0),
                               'maximum_normal_angle_degrees': max((r['normal_angle_degrees'] for r in rows), default=0)},
            'predicted_digest': m2_digest(predicted), 'normalization': 'none: barycentric interpolation preserves the source sums',
            'warnings': ['Only selected deformation groups are replaced. Region and unrelated vertex groups remain unchanged.']}

def mt_apply_weights(high, low, candidate, proposal, options):
    rows = m5_correspondence(high, low, options)
    names = proposal['groups']
    source_weights = mt_weights(high, names)
    all_indices = list(range(len(candidate.data.vertices)))
    for name in names:
        group = candidate.vertex_groups.get(name) or candidate.vertex_groups.new(name=name)
        if group.lock_weight:
            raise ValueError('LOCKED_WEIGHT_GROUP: ' + name)
        group.remove(all_indices)
    assigned = []
    for index, row in enumerate(rows):
        values = {}
        for vertex, factor in zip(row['vertices'], row['barycentric']):
            for name, weight in source_weights[vertex].items():
                values[name] = values.get(name, 0) + factor*weight
        for name, weight in values.items():
            if weight > 0:
                candidate.vertex_groups[name].add([index], weight, 'REPLACE')
        assigned.append(values)
    if m2_digest(assigned) != proposal['predicted_digest']:
        raise ValueError('STALE_CORRESPONDENCE: recomputed weight interpolation changed')
    return {'transferred_vertices': len(rows), 'transferred_groups': len(names), 'automatic_normalization': False}

def mt_morph_analysis(high, low):
    keys = high.data.shape_keys
    source_topology = ms_topology(high.data)['fingerprint']
    target_topology = ms_topology(low.data)['fingerprint']
    rows = []
    unit = bpy.context.scene.unit_settings.scale_length
    animated = bool(keys and keys.animation_data and (keys.animation_data.action or keys.animation_data.drivers or
                    any(not t.mute and t.strips for t in keys.animation_data.nla_tracks)))
    if keys:
        for key in keys.key_blocks:
            relative = key.relative_key or keys.key_blocks[0]
            deltas = [(high.matrix_world.to_3x3() @ (item.co-base.co)).length*unit for item, base in zip(key.data, relative.data)]
            affected = [high.matrix_world @ base.co * unit for base, delta in zip(relative.data, deltas) if delta > 1e-8]
            bounds = {'min_m': [min(p[axis] for p in affected) for axis in range(3)],
                      'max_m': [max(p[axis] for p in affected) for axis in range(3)]} if affected else None
            rows.append({'name': key.name, 'relative_key': relative.name, 'affected_vertices': sum(d > 1e-8 for d in deltas),
                         'maximum_displacement_m': max(deltas, default=0), 'vertex_group': key.vertex_group,
                         'affected_region_world_bounds': bounds})
    intact = ms_shape_keys(high.data)['fingerprint'] == ms_shape_keys(low.data)['fingerprint']
    feasible = not keys or (source_topology == target_topology and intact and not animated)
    return {'present': bool(keys), 'keys': rows[:100], 'key_count': len(rows), 'topology_changed': source_topology != target_topology,
            'already_preserved_exactly': bool(keys) and intact and not animated, 'transfer_feasible': feasible,
            'animated_dependencies_supported': not animated,
            'correspondence_confidence': 1.0 if feasible else None,
            'decision': 'EXACT_COPY_PRESERVED' if keys and feasible else 'NOT_APPLICABLE' if not keys else 'MANUAL_OR_LATER_PIPELINE_REQUIRED',
            'risk': 'none for exact static dependency preservation' if feasible else 'Topology-changed or animated/driven morph dependencies are not certified by this adapter.',
            'errors': [] if feasible else ['MANUAL_OR_LATER_PIPELINE_REQUIRED: topology-changed or animated/driven morph dependencies require a separately validated pipeline']}

def mt_attribute_spec(attribute):
    if attribute.data_type in ('FLOAT_COLOR', 'BYTE_COLOR'):
        return 'color', 4
    if attribute.data_type == 'FLOAT_VECTOR':
        return 'vector', 3
    if attribute.data_type in ('FLOAT', 'INT', 'BOOLEAN'):
        return 'value', 1
    return None

def mt_attribute_values(attribute):
    spec = mt_attribute_spec(attribute)
    if spec is None:
        raise ValueError('ATTRIBUTE_TYPE_UNSUPPORTED: ' + attribute.name + ' ' + attribute.data_type)
    prop, width = spec
    values = []
    for item in attribute.data:
        value = item.color if prop == 'color' else item.vector if prop == 'vector' else item.value
        values.append(list(value) if width > 1 else value)
    return values

def mt_attributes_plan(high, low, options):
    requested = options['attributes']
    rows, errors = [], []
    for name in requested:
        attribute = high.data.attributes.get(name)
        if attribute is None:
            errors.append('ATTRIBUTE_MISSING: ' + name)
            rows.append({'name': name, 'status': 'missing'})
            continue
        if name.startswith('.') or name in ('position', 'custom_normal', 'sharp_edge', 'sharp_face'):
            errors.append('INTERNAL_ATTRIBUTE_REFUSED: ' + name)
            rows.append({'name': name, 'status': 'internal_refused'})
            continue
        if attribute.domain not in ('POINT', 'CORNER', 'FACE') or mt_attribute_spec(attribute) is None:
            errors.append('ATTRIBUTE_TRANSFER_UNSUPPORTED: ' + name + ' ' + attribute.domain + '/' + attribute.data_type)
            rows.append({'name': name, 'status': 'unsupported'})
            continue
        existing = low.data.attributes.get(name)
        if existing and (existing.domain != attribute.domain or existing.data_type != attribute.data_type):
            errors.append('ATTRIBUTE_SCHEMA_CONFLICT: ' + name)
            rows.append({'name': name, 'status': 'schema_conflict'})
            continue
        values = mt_attribute_values(attribute)
        flat = [v for value in values for v in value] if values and isinstance(values[0], list) else values
        if any(isinstance(v, float) and not math.isfinite(v) for v in flat):
            errors.append('NONFINITE_ATTRIBUTE: ' + name)
            continue
        rows.append({'name': name, 'domain': attribute.domain, 'data_type': attribute.data_type, 'status': 'supported',
                     'source_digest': m2_digest(values), 'policy': 'barycentric' if attribute.data_type not in ('INT', 'BOOLEAN') else 'dominant_corner'})
    properties = []
    for owner_kind in ('object', 'mesh'):
        owner = high if owner_kind == 'object' else high.data
        for name in options['custom_properties'][owner_kind]:
            if name.startswith('production_') or name not in owner.keys():
                errors.append('CUSTOM_PROPERTY_REFUSED: ' + owner_kind + ':' + name)
                continue
            value = ms_value(owner[name])
            if value == 'unserializable' or (isinstance(value, str) and value.startswith('ID:')):
                errors.append('CUSTOM_PROPERTY_TYPE_UNSUPPORTED: ' + owner_kind + ':' + name)
                continue
            properties.append({'owner': owner_kind, 'name': name, 'value': value})
    if not errors:
        m5_correspondence(high, low, options)
        if options['materials']:
            boundary = mt_material_boundaries(high, low, options)
            if boundary['crossed_faces']:
                errors.append('MATERIAL_BOUNDARY_CROSSED: ' + str(boundary['crossed_faces']) + ' low faces span distinct source semantics')
    return {'supported': not errors, 'attributes': rows, 'properties': properties, 'materials': options['materials'],
            'source_materials': [slot.material.name if slot.material else None for slot in high.material_slots],
            'errors': errors, 'warnings': ['Arbitrary/EDGE/internal attributes are never transferred implicitly.']}

def mt_sample_value(values, indices, factors, discrete):
    if discrete:
        return values[indices[factors.index(max(factors))]]
    if isinstance(values[indices[0]], list):
        return [sum(values[index][channel]*factor for index, factor in zip(indices, factors)) for channel in range(len(values[indices[0]]))]
    return sum(values[index]*factor for index, factor in zip(indices, factors))

def mt_point_rows(high, low, points, normals, options):
    # Attribute corners can lie at the same vertex across a seam. The nearest source triangle and
    # its corner IDs are kept, so CORNER attributes use source corner data, never vertex indices.
    geo = m5_geo(high)
    tree = m5_bvh(geo)
    rows = []
    for point, normal in zip(points, normals):
        hit, hit_normal, triangle, distance = tree.find_nearest(point)
        if hit is None or distance > options['max_distance_m']:
            raise ValueError('ATTRIBUTE_CORRESPONDENCE_DISTANCE: source surface outside maximum distance')
        angle = math.degrees(normal.normalized().angle(hit_normal.normalized(), 0)) if normal.length else 180
        if angle > options['max_normal_angle_degrees']:
            raise ValueError('ATTRIBUTE_CORRESPONDENCE_NORMAL: source normal outside allowed angle')
        vertices = geo['triangles'][triangle]
        if options.get('reject_ambiguous_components', True) and geo['component_count'] > 1:
            component = geo['vertex_components'][vertices[0]]
            for alternative in tree.find_nearest_range(point, distance + options['ambiguity_distance_m']):
                other = geo['vertex_components'][geo['triangles'][alternative[2]][0]]
                if component != other:
                    raise ValueError('AMBIGUOUS_ATTRIBUTE_CORRESPONDENCE: another source component is equally near')
        rows.append({'vertices': list(vertices), 'loops': list(geo['triangle_loops'][triangle]),
                     'polygon': geo['triangle_polygons'][triangle],
                     'barycentric': m5_barycentric(hit, *[geo['points'][v] for v in vertices])})
    return rows

def mt_material_boundaries(high, low, options):
    geo = m5_geo(high)
    tree = m5_bvh(geo)
    unit = bpy.context.scene.unit_settings.scale_length
    crossed = []
    for polygon in low.data.polygons:
        center = low.matrix_world @ polygon.center * unit
        points = [center] + [(low.matrix_world @ low.data.vertices[i].co * unit)*.75 + center*.25 for i in polygon.vertices]
        semantics = set()
        for point in points:
            found = tree.find_nearest(point, options['max_distance_m'])
            if found[0] is None:
                raise ValueError('MATERIAL_CORRESPONDENCE_DISTANCE')
            semantics.add(high.data.polygons[geo['triangle_polygons'][found[2]]].material_index)
        if len(semantics) > 1:
            crossed.append(polygon.index)
    return {'crossed_faces': len(crossed), 'problem_faces': crossed[:options['limit']],
            'sampling': 'face centre plus inset corners; material boundary must not cut a low face'}

def mt_attribute_expected(high, low, proposal, options):
    points = [low.matrix_world @ v.co * bpy.context.scene.unit_settings.scale_length for v in low.data.vertices]
    nmatrix = low.matrix_world.to_3x3().inverted().transposed()
    normals = [nmatrix @ v.normal for v in low.data.vertices]
    vertex_rows = m5_correspondence(high, low, options)
    corner_rows, face_rows = None, None
    expected = {}
    for spec in proposal['attributes']:
        source = high.data.attributes[spec['name']]
        values = mt_attribute_values(source)
        discrete = source.data_type in ('INT', 'BOOLEAN')
        if source.domain == 'POINT':
            mapped = [mt_sample_value(values, r['vertices'], r['barycentric'], discrete) for r in vertex_rows]
        elif source.domain == 'CORNER':
            if corner_rows is None:
                positions = [points[loop.vertex_index] for loop in low.data.loops]
                corner_normals = [nmatrix @ n.vector for n in low.data.corner_normals]
                corner_rows = mt_point_rows(high, low, positions, corner_normals, options)
            mapped = [mt_sample_value(values, r['loops'], r['barycentric'], discrete) for r in corner_rows]
        else:
            if face_rows is None:
                unit = bpy.context.scene.unit_settings.scale_length
                face_rows = mt_point_rows(high, low, [low.matrix_world @ p.center * unit for p in low.data.polygons],
                                          [nmatrix @ p.normal for p in low.data.polygons], options)
            mapped = [values[r['polygon']] for r in face_rows]
        expected[spec['name']] = mapped
    if proposal['materials']:
        if face_rows is None:
            unit = bpy.context.scene.unit_settings.scale_length
            face_rows = mt_point_rows(high, low, [low.matrix_world @ p.center * unit for p in low.data.polygons],
                                      [nmatrix @ p.normal for p in low.data.polygons], options)
        expected['__materials__'] = [high.data.polygons[r['polygon']].material_index for r in face_rows]
    return expected

def mt_apply_attributes(high, low, candidate, proposal, options):
    expected = mt_attribute_expected(high, low, proposal, options)
    for spec in proposal['attributes']:
        attribute = candidate.data.attributes.get(spec['name']) or candidate.data.attributes.new(spec['name'], spec['data_type'], spec['domain'])
        prop, width = mt_attribute_spec(attribute)
        data = expected[spec['name']]
        flat = [v for row in data for v in row] if width > 1 else data
        attribute.data.foreach_set(prop, flat)
    if proposal['materials']:
        candidate.data.materials.clear()
        for slot in high.material_slots:
            candidate.data.materials.append(slot.material)
        candidate.data.polygons.foreach_set('material_index', expected['__materials__'])
    for row in proposal['properties']:
        owner = candidate if row['owner'] == 'object' else candidate.data
        owner[row['name']] = row['value']
    candidate.data.update()
    return {'transferred_attributes': len(proposal['attributes']), 'transferred_material_faces': len(expected.get('__materials__', [])),
            'transferred_properties': len(proposal['properties'])}

def mt_attribute_verify(high, low, candidate, proposal, options):
    expected = mt_attribute_expected(high, low, proposal, options)
    rows, errors = [], []
    for spec in proposal['attributes']:
        attribute = candidate.data.attributes.get(spec['name'])
        if attribute is None:
            errors.append('ATTRIBUTE_LOST: ' + spec['name'])
            continue
        actual, wanted = mt_attribute_values(attribute), expected[spec['name']]
        a = [v for row in actual for v in row] if actual and isinstance(actual[0], list) else actual
        b = [v for row in wanted for v in row] if wanted and isinstance(wanted[0], list) else wanted
        error = max((abs(float(x)-float(y)) for x, y in zip(a, b)), default=0)
        tolerance = 0.005 if spec['data_type'] == 'BYTE_COLOR' else 1e-5
        passed = len(actual) == len(wanted) and error <= tolerance
        rows.append({'name': spec['name'], 'passed': passed, 'maximum_value_error': error})
        if not passed:
            errors.append('ATTRIBUTE_TRANSFER_ERROR: ' + spec['name'])
    if proposal['materials']:
        expected_slots = proposal['source_materials']
        actual_slots = [slot.material.name if slot.material else None for slot in candidate.material_slots]
        if actual_slots != expected_slots or list(candidate.data.polygons[i].material_index for i in range(len(candidate.data.polygons))) != expected['__materials__']:
            errors.append('MATERIAL_SEMANTICS_TRANSFER_ERROR')
    for row in proposal['properties']:
        owner = candidate if row['owner'] == 'object' else candidate.data
        if row['name'] not in owner.keys() or ms_value(owner[row['name']]) != row['value']:
            errors.append('CUSTOM_PROPERTY_TRANSFER_ERROR: ' + row['name'])
    return {'passed': not errors, 'metrics': {'attributes': rows}, 'errors': errors}

def mt_evaluated(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        mesh.calc_loop_triangles()
        unit = bpy.context.scene.unit_settings.scale_length
        points = [evaluated.matrix_world @ v.co * unit for v in mesh.vertices]
        triangles = [tuple(t.vertices) for t in mesh.loop_triangles]
        edges = [tuple(e.vertices) for e in mesh.edges]
        polygons = [tuple(p.vertices) for p in mesh.polygons]
        return {'points': points, 'triangles': triangles, 'edges': edges, 'polygons': polygons}
    finally:
        evaluated.to_mesh_clear()

def mt_deform_metrics(reference, current, options):
    # Blender can change an evaluated quad's tessellation diagonal as it bends. Authored polygon
    # connectivity remains identical; use the reference diagonal for comparable local ratios.
    if (len(reference['points']) != len(current['points']) or reference['polygons'] != current['polygons'] or
        reference['edges'] != current['edges']):
        raise ValueError('DYNAMIC_TOPOLOGY_DEFORMATION_UNSUPPORTED')
    epsilon = 1e-12
    edges = [(current['points'][a]-current['points'][b]).length / max(epsilon, (reference['points'][a]-reference['points'][b]).length)
             for a, b in reference['edges']]
    areas = []
    for a, b, c in reference['triangles']:
        before = (reference['points'][b]-reference['points'][a]).cross(reference['points'][c]-reference['points'][a]).length
        after = (current['points'][b]-current['points'][a]).cross(current['points'][c]-current['points'][a]).length
        areas.append(after/max(epsilon, before))
    return {'collapsed_edge_fraction': sum(r < options['collapse_ratio'] for r in edges)/max(1, len(edges)),
            'stretched_edge_fraction': sum(r > options['stretch_ratio'] for r in edges)/max(1, len(edges)),
            'collapsed_triangle_fraction': sum(r < options['area_ratio'] for r in areas)/max(1, len(areas)),
            'minimum_edge_ratio': min(edges, default=1), 'maximum_edge_ratio': max(edges, default=1),
            'minimum_area_ratio': min(areas, default=1)}

def mt_surface_deform(a, b, samples):
    trees = [BVHTree.FromPolygons(item['points'], item['triangles'], all_triangles=True) for item in (a, b)]
    distances = []
    for item, tree in ((a, trees[1]), (b, trees[0])):
        points = item['points']
        indices = [int(i*(len(points)-1)/max(1, min(samples, len(points))-1)) for i in range(min(samples, len(points)))]
        for i in indices:
            hit = tree.find_nearest(points[i])
            if hit[0] is None:
                raise ValueError('DEFORMATION_SURFACE_MISSING')
            distances.append(hit[3])
    distances.sort()
    return {'samples': len(distances), 'mean_m': sum(distances)/max(1, len(distances)),
            'p95_m': distances[int((len(distances)-1)*.95)] if distances else 0,
            'maximum_m': max(distances, default=0)}

def mt_stress(high, low, options):
    poses = options.get('stress_poses', [])
    rig = mt_rig(low)
    if not poses:
        return {'status': 'not_run', 'reason': 'Explicit stress poses are required when deforming geometry is present.', 'passed': None}
    if len(high.data.vertices) > options['deformation_vertex_limit'] or len(low.data.vertices) > options['deformation_vertex_limit']:
        raise ValueError('DEFORMATION_CAPACITY_EXCEEDED: stress evaluation exceeds the declared vertex budget')
    if rig is None or mt_rig(high) is not rig:
        raise ValueError('STRESS_RIG_REQUIRED: both meshes must share an explicit armature')
    if rig.data.pose_position != 'POSE' or rig.mode != 'OBJECT' or bpy.context.mode != 'OBJECT':
        raise ValueError('POSE_POSITION_REQUIRED: deformation QA requires Object mode and armature POSE')
    if rig.animation_data and (rig.animation_data.drivers or rig.animation_data.use_tweak_mode or
        (rig.animation_data.use_nla and any(not t.mute and t.strips for t in rig.animation_data.nla_tracks))):
        raise ValueError('STRESS_ANIMATION_UNSUPPORTED: active NLA/drivers cannot be temporarily posed')
    for row in poses:
        bone = rig.pose.bones.get(row['bone'])
        if bone is None or bone.constraints or any(bone.lock_rotation) or bone.lock_rotation_w:
            raise ValueError('EXPLICIT_UNLOCKED_STRESS_CONTROL_REQUIRED: ' + row['bone'])
    original = {b.name: {'mode': b.rotation_mode, 'location': list(b.location), 'scale': list(b.scale),
                         'euler': list(b.rotation_euler), 'quaternion': list(b.rotation_quaternion), 'axis_angle': list(b.rotation_axis_angle)} for b in rig.pose.bones}
    frame = (bpy.context.scene.frame_current, bpy.context.scene.frame_subframe)
    action = rig.animation_data.action if rig.animation_data else None
    source_signature, target_signature = m5_signature(high), m5_signature(low)
    def restore():
        for name, values in original.items():
            bone = rig.pose.bones[name]
            bone.rotation_mode = values['mode']
            bone.location, bone.scale = values['location'], values['scale']
            bone.rotation_euler, bone.rotation_quaternion, bone.rotation_axis_angle = values['euler'], values['quaternion'], values['axis_angle']
        bpy.context.view_layer.update()
    rows, errors = [], []
    try:
        references = [mt_evaluated(obj) for obj in (high, low)]
        baseline = mt_surface_deform(*references, options['deformation_samples'])
        for pose in poses:
            restore()
            bone = rig.pose.bones[pose['bone']]
            initial = bone.matrix_basis.to_quaternion()
            bone.rotation_mode = 'QUATERNION'
            bone.rotation_quaternion = initial @ Quaternion(Vector(pose['axis']), math.radians(pose['angle_degrees']))
            bpy.context.view_layer.update()
            current = [mt_evaluated(obj) for obj in (high, low)]
            a, b = [mt_deform_metrics(reference, evaluated, options) for reference, evaluated in zip(references, current)]
            surface = mt_surface_deform(*current, options['deformation_samples'])
            reasons = []
            for name in ('collapsed_edge_fraction', 'stretched_edge_fraction', 'collapsed_triangle_fraction'):
                if b[name] > a[name] + options['deformation_fraction_tolerance']:
                    reasons.append(name + ': candidate regression')
            if surface['p95_m'] > baseline['p95_m'] + options['deformation_error_m']:
                reasons.append('posed surface P95 exceeds baseline plus allowed deformation error')
            rows.append({'pose': pose, 'source': a, 'target': b, 'surface': surface, 'passed': not reasons, 'reasons': reasons})
            errors.extend('DEFORMATION_REGRESSION: ' + reason for reason in reasons)
    finally:
        restore()
    restored = (m5_signature(high) == source_signature and m5_signature(low) == target_signature and
                (bpy.context.scene.frame_current, bpy.context.scene.frame_subframe) == frame and
                (not rig.animation_data or rig.animation_data.action is action))
    if not restored:
        errors.append('DEFORMATION_STATE_NOT_RESTORED')
    return {'status': 'success' if not errors else 'error', 'passed': not errors, 'state_restored': restored,
            'reference_surface': baseline, 'poses': rows, 'errors': errors,
            'criterion': 'same explicit poses, surface correspondence and within-mesh area/edge ratios; no cross-mesh vertex-index comparison',
            'warnings': ['Finite stress poses and geometric indicators do not certify anatomical or artistic quality.']}

def mt_deformation_regions(obj, options):
    rig = mt_rig(obj)
    groups = options.get('regions', {}) or ({b.name: [b.name] for b in rig.data.bones if b.use_deform and obj.vertex_groups.get(b.name)} if rig else {})
    names = {g.index: g.name for g in obj.vertex_groups}
    unit = bpy.context.scene.unit_settings.scale_length
    points = [obj.matrix_world @ v.co * unit for v in obj.data.vertices]
    rows = []
    for name, wanted in groups.items():
        members = {v.index for v in obj.data.vertices if any(names.get(e.group) in wanted and e.weight >= options['region_weight'] for e in v.groups)}
        lengths = [(points[a]-points[b]).length for e in obj.data.edges for a, b in [e.vertices] if a in members and b in members]
        rows.append({'region': name, 'groups': wanted, 'vertices': len(members), 'internal_edges': len(lengths),
                     'mean_edge_length_m': sum(lengths)/max(1, len(lengths)), 'minimum_vertices': options['minimum_region_vertices'],
                     'sufficient_for_declared_policy': len(members) >= options['minimum_region_vertices'],
                     'confidence': 1.0 if options.get('regions') else 0.7,
                     'evidence': 'explicit groups' if options.get('regions') else 'armature deform groups; anatomy not inferred'})
    return {'object': obj.name, 'armature': rig.name if rig else None, 'regions': rows[:options['limit']], 'region_count': len(rows),
            'passed': all(r['sufficient_for_declared_policy'] for r in rows) if rows else None,
            'warnings': ['The configured vertex count is a policy indicator, not a universal joint-loop requirement.']}

def m5_plan(high, low, options, relation):
    if M5_TRANSFER_KIND == 'weights':
        return mt_weight_plan(high, low, options)
    if M5_TRANSFER_KIND == 'attributes':
        return mt_attributes_plan(high, low, options)
    analysis = mt_morph_analysis(high, low)
    return dict(analysis, supported=analysis['transfer_feasible'])

def m5_apply(high, low, candidate, proposal, options):
    if M5_TRANSFER_KIND == 'weights':
        return mt_apply_weights(high, low, candidate, proposal, options)
    if M5_TRANSFER_KIND == 'attributes':
        return mt_apply_attributes(high, low, candidate, proposal, options)
    if not proposal['transfer_feasible']:
        raise ValueError('MANUAL_OR_LATER_PIPELINE_REQUIRED')
    return {'morph_policy': proposal['decision'], 'morphs_preserved': proposal['key_count']}

def m5_verify(high, low, candidate, proposal, options):
    if M5_TRANSFER_KIND == 'weights':
        report = mt_weight_report(candidate, dict(options, groups=proposal['groups']))
        rows = m5_correspondence(high, candidate, options)
        expected = mt_weights(high, proposal['groups'])
        actual = mt_weights(candidate, proposal['groups'])
        maximum_error = 0.0
        for index, row in enumerate(rows):
            for name in proposal['groups']:
                wanted = sum(expected[v].get(name, 0)*factor for v, factor in zip(row['vertices'], row['barycentric']))
                maximum_error = max(maximum_error, abs(wanted-actual[index].get(name, 0)))
        report['maximum_correspondence_weight_error'] = maximum_error
        if maximum_error > options['transfer_weight_tolerance']:
            report['passed'] = False
            report['errors'].append('WEIGHT_CORRESPONDENCE_ERROR: actual weights differ from source surface interpolation')
        if options.get('stress_poses'):
            stress = mt_stress(high, candidate, options)
            report['deformation_qa'] = stress
            report['errors'].extend(stress['errors'])
            report['passed'] = report['passed'] and stress['passed']
        return {'passed': report['passed'], 'metrics': report, 'errors': report['errors'], 'warnings': report['warnings']}
    if M5_TRANSFER_KIND == 'attributes':
        return mt_attribute_verify(high, low, candidate, proposal, options)
    analysis = mt_morph_analysis(high, candidate)
    return {'passed': analysis['transfer_feasible'], 'metrics': analysis, 'errors': analysis['errors']}
'''

WEIGHT_RUNTIME = TRANSFER_RUNTIME + "\nM5_TRANSFER_KIND = 'weights'\n"
ATTRIBUTE_RUNTIME = TRANSFER_RUNTIME + "\nM5_TRANSFER_KIND = 'attributes'\n"
MORPH_RUNTIME = TRANSFER_RUNTIME + "\nM5_TRANSFER_KIND = 'morphs'\n"
