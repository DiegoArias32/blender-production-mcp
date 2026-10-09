"""Blender-native normal/tangent pipeline and triangulated evaluation copies."""
from ..common.m5 import M5_RUNTIME
from ..retopology.helpers import GEOMETRY_RUNTIME
from ..retopology.runtime import RETOPOLOGY_RUNTIME
from ..transfer.runtime import TRANSFER_RUNTIME

GAME_RUNTIME = TRANSFER_RUNTIME + RETOPOLOGY_RUNTIME + M5_RUNTIME + GEOMETRY_RUNTIME + r'''
import math
import bmesh
from mathutils import Vector

def gm_normal_audit(obj, options):
    mesh = obj.data
    faces = [p.normal.copy() for p in mesh.polygons]
    corners = [n.vector.copy() for n in mesh.corner_normals]
    invalid_faces = [i for i, n in enumerate(faces) if not all(math.isfinite(v) for v in n) or n.length < .99 or n.length > 1.01]
    invalid_corners = [i for i, n in enumerate(corners) if not all(math.isfinite(v) for v in n) or n.length < .99 or n.length > 1.01]
    sharp = mesh.attributes.get('sharp_edge')
    sharp_values = [bool(v.value) for v in sharp.data] if sharp else [False]*len(mesh.edges)
    edge_faces = [[] for _ in mesh.edges]
    for polygon in mesh.polygons:
        for loop in mesh.loops[polygon.loop_start:polygon.loop_start+polygon.loop_total]:
            edge_faces[loop.edge_index].append(polygon.index)
    jumps, hard = [], 0
    for i, indices in enumerate(edge_faces):
        if len(indices) == 2:
            angle = math.degrees(faces[indices[0]].angle(faces[indices[1]], 0))
            if sharp_values[i] or not all(mesh.polygons[index].use_smooth for index in indices):
                hard += 1
            elif angle > options['normal_continuity_degrees']:
                jumps.append((i, angle))
    errors = []
    if invalid_faces:
        errors.append('INVALID_FACE_NORMALS: ' + str(len(invalid_faces)))
    if invalid_corners:
        errors.append('INVALID_CORNER_NORMALS: ' + str(len(invalid_corners)))
    return {'passed': not errors, 'normal_domain': mesh.normals_domain, 'face_normals': len(faces), 'corner_normals': len(corners),
            'has_custom_normals': bool(mesh.has_custom_normals), 'sharp_edges': sum(sharp_values), 'hard_boundaries': hard,
            'smooth_faces': sum(p.use_smooth for p in mesh.polygons), 'flat_faces': sum(not p.use_smooth for p in mesh.polygons),
            'continuity_indicators': len(jumps), 'maximum_smooth_face_angle_degrees': max((r[1] for r in jumps), default=0),
            'invalid_faces': invalid_faces[:options['limit']], 'invalid_corners': invalid_corners[:options['limit']],
            'errors': errors, 'warnings': ['Large adjacent-face angles on smooth boundaries are indicators, not automatic errors.'] if jumps else []}

def gm_triangulate(mesh, options):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        # Blender's modifier calls this FIXED_ALTERNATE; the installed bmesh enum is ALTERNATE.
        quad_method = 'ALTERNATE' if options['quad_method'] == 'FIXED_ALTERNATE' else options['quad_method']
        bmesh.ops.triangulate(bm, faces=list(bm.faces), quad_method=quad_method, ngon_method=options['ngon_method'])
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()

def gm_tri_hash(mesh):
    return m2_digest([ms_floats(mesh.vertices, 'co', 3), [list(p.vertices) for p in mesh.polygons]])

def gm_tri_validation(obj, options, execute_stress=False, high=None, relation=None):
    original = obj.data
    before = ms_signature(obj)
    copies = [original.copy(), original.copy()]
    temporary = None
    try:
        for copy in copies:
            gm_triangulate(copy, options)
        a, b = copies
        same_vertices = ms_floats(original.vertices, 'co', 3) == ms_floats(a.vertices, 'co', 3)
        same_shape_keys = ms_shape_keys(original)['fingerprint'] == ms_shape_keys(a)['fingerprint']
        expected = sum(max(0, len(p.vertices)-2) for p in original.polygons)
        triangles = len(a.polygons)
        normals = [p.normal.length for p in a.polygons]
        bad_normals = sum(not math.isfinite(n) or n < .99 or n > 1.01 for n in normals)
        # Polygon.area on a non-planar quad is not the area of its installed tessellation.
        # Compare the actual source triangle surface against the explicit result triangles.
        original.calc_loop_triangles()
        source_area = sum((original.vertices[t.vertices[1]].co-original.vertices[t.vertices[0]].co).cross(
            original.vertices[t.vertices[2]].co-original.vertices[t.vertices[0]].co).length*.5 for t in original.loop_triangles)
        result_area = sum(p.area for p in a.polygons)
        relative_area_error = abs(source_area-result_area)/max(1e-12, source_area)
        errors = []
        if gm_tri_hash(a) != gm_tri_hash(b):
            errors.append('NONDETERMINISTIC_TRIANGULATION')
        if any(len(p.vertices) != 3 for p in a.polygons) or triangles != expected:
            errors.append('TRIANGLE_COUNT_MISMATCH')
        if not same_vertices:
            errors.append('TRIANGULATION_VERTEX_ORDER_CHANGED')
        if not same_shape_keys:
            errors.append('TRIANGULATION_MORPH_LOSS')
        if bad_normals:
            errors.append('INVALID_TRIANGULATED_NORMALS')
        # Planar quads and convex planar n-gons preserve surface exactly. Nonplanar/n-gon meshes
        # are judged against an explicit area tolerance and need separate source/silhouette QA.
        if relative_area_error > options['triangulation_area_tolerance']:
            errors.append('TRIANGULATION_AREA_DEVIATION')
        if original.has_custom_normals and options['custom_normals'] != 'rebuild':
            errors.append('CUSTOM_NORMAL_REBUILD_REQUIRES_CONFIRMATION')
        stress = {'status': 'not_run', 'reason': 'Explicit stress poses required when rigged deformation matters.', 'passed': None}
        protection = {'passed': True, 'metrics': {'regions': []}, 'errors': []}
        if (high is not None and relation) or (execute_stress and options.get('stress_poses')):
            temporary = obj.copy()
            temporary.data = a
            temporary.name = obj.name + '__M5_TRANSIENT_TRI_QA'
            for collection in obj.users_collection:
                collection.objects.link(temporary)
            temporary.hide_render = True
            if high is not None and relation:
                protection = rt_protected(high, temporary, options, relation)
                errors.extend(protection['errors'])
            if execute_stress and options.get('stress_poses'):
                stress = mt_stress(obj, temporary, options)
                errors.extend(stress['errors'])
        return {'passed': not errors, 'expected_triangles': expected, 'triangles': triangles,
                'deterministic': gm_tri_hash(a) == gm_tri_hash(b), 'triangulation_fingerprint': gm_tri_hash(a),
                'vertices_preserved_exactly': same_vertices, 'morphs_preserved_exactly': same_shape_keys,
                'relative_surface_area_error': relative_area_error, 'invalid_normals': bad_normals,
                'silhouette_preservation': 'exact vertex positions and triangle boundary; independent view QA still required for non-planar polygons',
                'deformation_preservation': 'same vertex indices/weights; optional explicit stress poses verify evaluated surfaces',
                'source_unchanged': ms_signature(obj) == before, 'errors': errors,
                'deformation_qa': stress,
                'protected_regions': protection,
                'warnings': ['Triangulation is an evaluation copy; authoring mesh is never permanently triangulated.']}
    finally:
        if temporary:
            bpy.data.objects.remove(temporary, do_unlink=True)
        for copy in copies:
            bpy.data.meshes.remove(copy)

def gm_tangents(obj, options):
    original = obj.data
    before = ms_signature(obj)
    layer = options.get('uv_layer') or (original.uv_layers.active.name if original.uv_layers.active else None)
    errors = []
    if not layer or original.uv_layers.get(layer) is None:
        return {'passed': False, 'uv_layer': layer, 'errors': ['TANGENT_UV_REQUIRED: explicit or active UV layer missing'],
                'pipeline': 'Blender Mesh.calc_tangents (MikkTSpace-compatible Blender pipeline)'}
    mesh = original.copy()
    try:
        if any(len(p.vertices) > 4 for p in mesh.polygons):
            gm_triangulate(mesh, options)
        mesh.calc_loop_triangles()
        uv = mesh.uv_layers[layer].uv
        degenerate = 0
        for triangle in mesh.loop_triangles:
            a, b, c = [uv[i].vector for i in triangle.loops]
            twice_area = abs((b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x))
            if twice_area <= options['uv_area_epsilon']:
                degenerate += 1
        if degenerate:
            errors.append('TANGENT_DEGENERATE_UV: ' + str(degenerate))
        if not errors:
            try:
                mesh.calc_tangents(uvmap=layer)
            except RuntimeError as exc:
                errors.append('BLENDER_TANGENT_GENERATION_FAILED: ' + str(exc))
        invalid, max_dot, max_length_error = [], 0.0, 0.0
        if not errors:
            corners = [n.vector for n in mesh.corner_normals]
            for loop, normal in zip(mesh.loops, corners):
                tangent = loop.tangent
                max_dot = max(max_dot, abs(tangent.dot(normal)))
                max_length_error = max(max_length_error, abs(tangent.length-1))
                if (not all(math.isfinite(v) for v in tangent) or abs(tangent.length-1) > .001 or
                    abs(tangent.dot(normal)) > .001 or abs(abs(loop.bitangent_sign)-1) > .001):
                    invalid.append(loop.index)
            if invalid:
                errors.append('INVALID_TANGENT_BASIS: ' + str(len(invalid)))
        return {'passed': not errors, 'uv_layer': layer, 'corners': len(mesh.loops), 'triangles': len(mesh.loop_triangles),
                'degenerate_uv_triangles': degenerate, 'invalid_tangent_corners': len(invalid),
                'maximum_normal_tangent_dot': max_dot, 'maximum_tangent_length_error': max_length_error,
                'normal_convention': options['normal_convention'], 'pipeline': 'Blender Mesh.calc_tangents; tangent + corner normal + bitangent sign',
                'compatibility': 'Declare convention in handoff; final importer triangulation and UV/split normals must match.',
                'source_unchanged': ms_signature(obj) == before, 'errors': errors}
    finally:
        mesh.free_tangents()
        bpy.data.meshes.remove(mesh)

def m5_plan(high, low, options, relation):
    if M5_GAME_KIND == 'triangulation':
        audit = gm_tri_validation(low, options, high=high, relation=relation)
        return dict(audit, supported=audit['passed'], strategy='DETERMINISTIC_TRIANGULATED_EVALUATION_COPY',
                    custom_normals=options['custom_normals'], quad_method=options['quad_method'], ngon_method=options['ngon_method'])
    audit = gm_normal_audit(low, options)
    reasons = list(audit['errors'])
    if low.data.has_custom_normals and options['custom_normals'] == 'rebuild' and not options['confirm_rebuild_custom_normals']:
        reasons.append('CUSTOM_NORMAL_REBUILD_REQUIRES_CONFIRMATION')
    return {'supported': not reasons, 'normal_audit': audit, 'strategy': options['normal_policy'],
            'custom_normals': options['custom_normals'], 'errors': reasons,
            'warnings': ['Rebuilding custom normals is explicit and occurs only on a new candidate.'] if options['custom_normals'] == 'rebuild' else []}

def m5_apply(high, low, candidate, proposal, options):
    if M5_GAME_KIND == 'triangulation':
        gm_triangulate(candidate.data, options)
        if low.data.has_custom_normals and options['custom_normals'] == 'rebuild':
            candidate.data.normals_split_custom_set([(0, 0, 0)]*len(candidate.data.loops))
        return {'triangles': len(candidate.data.polygons), 'authoring_mesh': low.name, 'evaluation_copy': candidate.name}
    if options['normal_policy'] != 'preserve':
        smooth = options['normal_policy'] == 'smooth'
        candidate.data.polygons.foreach_set('use_smooth', [smooth]*len(candidate.data.polygons))
    if options['custom_normals'] == 'rebuild':
        candidate.data.normals_split_custom_set([(0, 0, 0)]*len(candidate.data.loops))
    candidate.data.update()
    return {'normal_policy': options['normal_policy'], 'custom_normal_policy': options['custom_normals']}

def m5_verify(high, low, candidate, proposal, options):
    audit = gm_normal_audit(candidate, options)
    errors = list(audit['errors'])
    if M5_GAME_KIND == 'triangulation':
        if len(candidate.data.polygons) != proposal['expected_triangles'] or any(len(p.vertices) != 3 for p in candidate.data.polygons):
            errors.append('TRIANGLE_COUNT_MISMATCH')
        if ms_floats(low.data.vertices, 'co', 3) != ms_floats(candidate.data.vertices, 'co', 3):
            errors.append('TRIANGULATION_GEOMETRY_CHANGED')
        if ms_shape_keys(low.data)['fingerprint'] != ms_shape_keys(candidate.data)['fingerprint']:
            errors.append('TRIANGULATION_MORPH_LOSS')
        if ms_vertex_groups(low, low.data)['weights_fingerprint'] != ms_vertex_groups(candidate, candidate.data)['weights_fingerprint']:
            errors.append('TRIANGULATION_WEIGHTS_CHANGED')
        audit.update(triangles=len(candidate.data.polygons), deterministic_fingerprint=gm_tri_hash(candidate.data),
                     source_comparison=m5_compare(low, candidate, dict(options, samples=options['sample_count'])))
        if not audit['source_comparison']['passed']:
            errors.extend(audit['source_comparison']['errors'])
        protection = rt_protected(high, candidate, options, params.get('relation'))
        audit['protected_regions'] = protection
        errors.extend(protection['errors'])
        if options.get('stress_poses'):
            stress = mt_stress(low, candidate, options)
            audit['deformation_qa'] = stress
            errors.extend(stress['errors'])
    else:
        if ms_floats(low.data.vertices, 'co', 3) != ms_floats(candidate.data.vertices, 'co', 3):
            errors.append('NORMAL_PREPARATION_GEOMETRY_CHANGED')
        if options['custom_normals'] == 'preserve' and ms_normals(low.data)['custom_normals'] != ms_normals(candidate.data)['custom_normals']:
            errors.append('CUSTOM_NORMALS_LOST')
    return {'passed': not errors, 'metrics': audit, 'errors': errors, 'warnings': audit['warnings']}
'''

NORMAL_RUNTIME = GAME_RUNTIME + "\nM5_GAME_KIND = 'normals'\n"
TRI_RUNTIME = GAME_RUNTIME + "\nM5_GAME_KIND = 'triangulation'\n"
