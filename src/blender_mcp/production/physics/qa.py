"""M8 bounded geometric measurements; numerical QA does not certify visual quality.

All motion distances are world metres. Collision uses evaluated, closed meshes
and three BVH parity rays; it never mistakes a nearest surface normal for inside.
The thresholds belong to the reviewed plan, not universal material constants.
"""
from __future__ import annotations

import inspect
import math

from .integrity import RUNTIME as INTEGRITY_RUNTIME


def _pq_sub(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


def _pq_dot(a, b):
    return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]


def _pq_cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _pq_length(a):
    return math.sqrt(_pq_dot(a, a))


def closed_triangles(triangles):
    """Every triangle edge must belong to two faces with opposite orientation."""
    counts = {}
    winding = {}
    for triangle in triangles:
        for a, b in ((triangle[0], triangle[1]), (triangle[1], triangle[2]), (triangle[2], triangle[0])):
            edge = (min(a, b), max(a, b))
            counts[edge] = counts.get(edge, 0)+1
            winding[edge] = winding.get(edge, 0)+(1 if a < b else -1)
    return bool(counts) and all(counts[e] == 2 and winding[e] == 0 for e in counts)


def same_topology(a, b):
    """Polygon vertex cycles are topology; a deformed quad's diagonal is not."""
    return (a['vertex_count']==b['vertex_count'] and
            a.get('polygons',[])==b.get('polygons',[]) and a['edges']==b['edges'])


def quaternion_angle_deg(a, b):
    """Shortest quaternion angle; chord formula remains accurate near zero."""
    a,b=tuple(a),tuple(b)
    if len(a)!=4 or len(b)!=4 or any(not math.isfinite(v) for v in a+b):
        raise ValueError('SECONDARY_ANGLE_ROTATION_INVALID')
    na=math.sqrt(sum(v*v for v in a))
    nb=math.sqrt(sum(v*v for v in b))
    if na<1e-12 or nb<1e-12:
        raise ValueError('SECONDARY_ANGLE_ROTATION_INVALID')
    a=tuple(v/na for v in a)
    b=tuple(v/nb for v in b)
    if sum(x*y for x,y in zip(a,b))<0:
        b=tuple(-v for v in b)
    difference=math.sqrt(sum((x-y)*(x-y) for x,y in zip(a,b)))
    combined=math.sqrt(sum((x+y)*(x+y) for x,y in zip(a,b)))
    return math.degrees(4*math.atan2(difference,combined))


def geometry_metrics(points, edges, triangles, reference):
    """Area/edge collapse and global closed volume sign; folds are not inversions."""
    if not points or len(points) != len(reference):
        return {'valid': False, 'reason': 'TOPOLOGY_CHANGED'}
    if any(not math.isfinite(v) for point in points+reference for v in point):
        return {'valid': False, 'reason': 'NONFINITE_GEOMETRY'}
    stretch = 1.0
    collapsed_edges = 0
    for a, b in edges:
        rest = _pq_length(_pq_sub(reference[a], reference[b]))
        current = _pq_length(_pq_sub(points[a], points[b]))
        if rest > 1e-12:
            stretch = max(stretch, current/rest)
            collapsed_edges += int(current/rest < 1e-4)
    degenerate = 0
    area_ratios = []
    volume = 0.0
    reference_volume = 0.0
    area = 0.0
    for a, b, c in triangles:
        rest_area = _pq_length(_pq_cross(_pq_sub(reference[b], reference[a]), _pq_sub(reference[c], reference[a])))/2
        current_area = _pq_length(_pq_cross(_pq_sub(points[b], points[a]), _pq_sub(points[c], points[a])))/2
        area += current_area
        if rest_area > 1e-14:
            ratio = current_area/rest_area
            area_ratios.append(ratio)
            degenerate += int(ratio < 1e-4)
        volume += _pq_dot(points[a], _pq_cross(points[b], points[c]))/6
        reference_volume += _pq_dot(reference[a], _pq_cross(reference[b], reference[c]))/6
    closed = closed_triangles(triangles)
    reference_volume_measurable = closed and abs(reference_volume)>1e-12
    volume_ratio = abs(volume/reference_volume) if reference_volume_measurable else None
    inverted = volume*reference_volume<0 if reference_volume_measurable else None
    indicators = []
    if degenerate:
        indicators.append('DEGENERATE_TRIANGLES_MEASURED')
    if collapsed_edges:
        indicators.append('COLLAPSED_EDGES_MEASURED')
    if inverted:
        indicators.append('CLOSED_VOLUME_SIGN_INVERTED')
    if volume_ratio is not None and abs(volume_ratio-1)>0.000001:
        indicators.append('CLOSED_VOLUME_CHANGE_MEASURED')
    return {'valid': True, 'max_edge_stretch_ratio': stretch,
            'collapsed_edges': collapsed_edges, 'degenerate_triangles': degenerate,
            'degenerate_fraction': degenerate/max(1, len(triangles)),
            'min_area_ratio': min(area_ratios) if area_ratios else None,
            'area_measurement_status': 'MEASURED' if area_ratios else 'NOT_APPLICABLE_NO_REFERENCE_TRIANGLE_AREA',
            'surface_area_m2': area,
            'closed_oriented_mesh': closed,
            'signed_volume_m3': volume if closed else None,
            'reference_signed_volume_m3': reference_volume if closed else None,
            'absolute_volume_ratio': volume_ratio,
            'volume_measurement_status': 'MEASURED_CLOSED_VOLUME_RATIO' if reference_volume_measurable else 'UNDEFINED_REFERENCE_VOLUME' if closed else 'NOT_DEFINED_OPEN_SURFACE',
            'closed_volume_inverted': inverted,
            'inversion_count': None,
            'local_face_inversion_certified': False,
            'inversion_measurement_status': 'MEASURED_GLOBAL_CLOSED_VOLUME_SIGN' if reference_volume_measurable else 'UNDEFINED_REFERENCE_VOLUME' if closed else 'UNDEFINED_OPEN_FOLDING_SURFACE',
            'inversion_method': 'closed_oriented_volume_sign' if closed else 'not_defined_for_open_folding_surface',
            'stability_indicators': indicators}


def motion_step(points, reference, previous, previous_previous, fps, edges, triangles, thresholds,
                previous_reference=None, previous_previous_reference=None):
    """One dense frame guard; nonfinite data fails before arithmetic or summaries."""
    geometry = geometry_metrics(points, edges, triangles, reference)
    if not geometry['valid']:
        return {'passed': False, 'errors': [geometry['reason']], 'geometry': geometry}
    if previous is not None and len(previous) != len(points):
        return {'passed': False, 'errors': ['TOPOLOGY_CHANGED'], 'geometry': geometry}
    if previous_previous is not None and (previous is None or len(previous_previous) != len(points)):
        return {'passed': False, 'errors': ['TOPOLOGY_CHANGED'], 'geometry': geometry}
    if any(not math.isfinite(v) for history in (previous, previous_previous) if history is not None for point in history for v in point):
        return {'passed': False, 'errors': ['NONFINITE_MOTION_HISTORY'], 'geometry': geometry}
    if any(len(history)!=len(points) or any(not math.isfinite(v) for point in history for v in point)
           for history in (previous_reference,previous_previous_reference) if history is not None):
        return {'passed': False, 'errors': ['PRIMARY_REFERENCE_HISTORY_INVALID'], 'geometry': geometry}
    displacement = max(_pq_length(_pq_sub(a, b)) for a, b in zip(points, reference))
    velocity = max((_pq_length(_pq_sub(a, b))*fps for a, b in zip(points, previous)), default=0.0) if previous is not None else 0.0
    acceleration = max((_pq_length((a[0]-2*b[0]+c[0], a[1]-2*b[1]+c[1], a[2]-2*b[2]+c[2]))*fps*fps
                        for a, b, c in zip(points, previous, previous_previous)), default=0.0) if previous_previous is not None else 0.0
    ref_diagonal = _pq_length(tuple(max(p[i] for p in reference)-min(p[i] for p in reference) for i in range(3)))
    diagonal = _pq_length(tuple(max(p[i] for p in points)-min(p[i] for p in points) for i in range(3)))
    bbox_growth = diagonal/max(ref_diagonal, 1e-12)
    measured = {'max_displacement_m': displacement, 'max_velocity_m_s': velocity,
                'max_acceleration_m_s2': acceleration, 'bbox_growth_ratio': bbox_growth,
                'max_edge_stretch_ratio': geometry['max_edge_stretch_ratio'],
                'degenerate_fraction': geometry['degenerate_fraction']}
    errors = [key.upper()+'_EXCEEDED' for key, value in measured.items() if value > thresholds[key]]
    measured['min_area_ratio'] = geometry['min_area_ratio']
    if geometry['min_area_ratio'] is not None and geometry['min_area_ratio']<thresholds['min_area_ratio']:
        errors.append('MIN_AREA_RATIO_BELOW_THRESHOLD')
    measured['absolute_volume_ratio'] = geometry['absolute_volume_ratio']
    residual = [_pq_sub(a,b) for a,b in zip(points,reference)]
    previous_residual = [_pq_sub(a,b) for a,b in zip(previous,previous_reference)] if previous is not None and previous_reference is not None else None
    older_residual = [_pq_sub(a,b) for a,b in zip(previous_previous,previous_previous_reference)] if previous_previous is not None and previous_previous_reference is not None else None
    measured['max_secondary_displacement_m'] = displacement
    measured['max_secondary_velocity_m_s'] = max((_pq_length(_pq_sub(a,b))*fps for a,b in zip(residual,previous_residual)),default=0.0) if previous_residual is not None else None
    measured['max_secondary_acceleration_m_s2'] = max((_pq_length((a[0]-2*b[0]+c[0],a[1]-2*b[1]+c[1],a[2]-2*b[2]+c[2]))*fps*fps
                                                     for a,b,c in zip(residual,previous_residual,older_residual)),default=0.0) if older_residual is not None else None
    if geometry['closed_volume_inverted']:
        errors.append('CLOSED_VOLUME_INVERTED')
    return {'passed': not errors, 'errors': errors, 'metrics': measured, 'geometry': geometry}


def validate_thresholds(values=None):
    """Proposed defaults must be disclosed and remain configurable by the plan."""
    defaults = {'max_displacement_m': 2.0, 'max_velocity_m_s': 100.0,
                'max_acceleration_m_s2': 2000.0, 'bbox_growth_ratio': 10.0,
                'max_edge_stretch_ratio': 4.0, 'degenerate_fraction': 0.05,'min_area_ratio':0.05,
                'max_penetration_depth_m': 0.005, 'max_penetrating_vertices': 0,
                'anchor_detachment_m': 0.002, 'root_detachment_m': 0.002,
                'bake_max_error_m': 0.00001}
    values = {} if values is None else values
    if not isinstance(values, dict) or set(values)-set(defaults):
        raise ValueError('INVALID_QA_THRESHOLD: unknown threshold')
    defaults.update(values)
    for key, value in defaults.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('INVALID_QA_THRESHOLD: '+key)
    if type(defaults['max_penetrating_vertices']) is not int:
        raise ValueError('INVALID_QA_THRESHOLD: max_penetrating_vertices must be integer')
    if defaults['min_area_ratio']>1:
        raise ValueError('INVALID_QA_THRESHOLD: min_area_ratio must be in 0..1')
    return defaults


_PURE_FUNCTIONS = (_pq_sub, _pq_dot, _pq_cross, _pq_length, closed_triangles, same_topology,quaternion_angle_deg,
                   geometry_metrics, motion_step, validate_thresholds)
RUNTIME = INTEGRITY_RUNTIME+'\nimport bpy\nimport math\nfrom mathutils import Vector\nfrom mathutils.bvhtree import BVHTree\n' + '\n'.join(inspect.getsource(fn) for fn in _PURE_FUNCTIONS) + r'''

def pq_capture(params,extra_objects):
    selection=dict(params)
    selection['source_object']=params.get('source_object') or params.get('source_name') or params.get('native_candidate_name') or params.get('candidate_name')
    source,primary,deps=px_selection(selection)
    extra=list(extra_objects)
    if params.get('candidate_rig'):
        extra.append(px_object(params['candidate_rig']))
    deps=px_dependencies(deps+extra)
    prod_require_evaluable(deps)
    px_validate_objects(deps)
    return deps,px_capture(deps)

def pq_object(name):
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type != 'MESH':
        raise ValueError('PHYSICS_MESH_REQUIRED: '+str(name))
    return obj

def pq_frames(params):
    frames = params.get('frames')
    if frames is None:
        frames = list(range(params['frame_start'], params['frame_end']+1))
    if not isinstance(frames, list) or not frames or len(frames) > 601 or any(type(f) is not int for f in frames):
        raise ValueError('PHYSICS_QA_WINDOW_INVALID')
    if frames != list(range(frames[0], frames[-1]+1)):
        raise ValueError('DENSE_CHRONOLOGICAL_QA_REQUIRED')
    return frames

def pq_secondary_object(name):
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type not in ('MESH','ARMATURE'):
        raise ValueError('PHYSICS_SECONDARY_OBJECT_REQUIRED')
    return obj

def pq_bone_names(obj,params):
    names = params.get('bone_names',[])
    if not isinstance(names,list) or not names or len(names)>256 or any(not isinstance(name,str) or not name for name in names) or len(set(names))!=len(names):
        raise ValueError('EXPLICIT_SECONDARY_BONE_NAMES_REQUIRED')
    if any(obj.pose.bones.get(name) is None for name in names):
        raise ValueError('SECONDARY_BONE_MAPPING_INVALID')
    return names

def pq_bone_snapshot(obj,params,segment_samples=False):
    names = pq_bone_names(obj,params)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    unit = bpy.context.scene.unit_settings.scale_length
    if not math.isfinite(unit) or unit<=0:
        raise ValueError('PHYSICS_UNIT_SCALE_INVALID')
    points = []
    roots = []
    edges = []
    for name in names:
        bone = evaluated.pose.bones[name]
        head = evaluated.matrix_world@bone.head*unit
        tail = evaluated.matrix_world@bone.tail*unit
        if segment_samples:
            points.extend(tuple(head.lerp(tail,index/8)) for index in range(9))
        else:
            index = len(points)
            points.extend((tuple(head),tuple(tail)))
            edges.append((index,index+1))
            if not bone.parent or bone.parent.name not in names:
                roots.append(index)
    return {'points':points,'edges':edges,'triangles':[],'polygons':[],'vertex_count':len(points),'polygon_count':0,
            'geometry_type':'ARMATURE','root_indices':roots,'bone_names':names}

def pq_motion_snapshot(obj,params):
    return pq_bone_snapshot(obj,params) if obj.type=='ARMATURE' else pq_snapshot(obj,params.get('max_vertices',20000))

def pq_secondary_angle(obj,source,params):
    graph=bpy.context.evaluated_depsgraph_get()
    if obj.type=='ARMATURE' or params.get('candidate_rig'):
        candidate_rig=pq_secondary_object(params['candidate_rig']) if params.get('candidate_rig') else obj
        source_rig=source if obj.type=='ARMATURE' else pq_secondary_object(params.get('source_chain',{}).get('armature'))
        if candidate_rig.type!='ARMATURE' or source_rig.type!='ARMATURE':
            raise ValueError('SECONDARY_ANGLE_RIG_REFERENCE_REQUIRED')
        names=pq_bone_names(candidate_rig,params)
        pq_bone_names(source_rig,params)
        a,b=candidate_rig.evaluated_get(graph),source_rig.evaluated_get(graph)
        angles=[quaternion_angle_deg((a.matrix_world@a.pose.bones[n].matrix).to_quaternion(),
                                     (b.matrix_world@b.pose.bones[n].matrix).to_quaternion()) for n in names]
        return {'max_secondary_angle_deg':max(angles),'angle_reference_space':'same_frame_bone_world_quaternion',
                'bone_names':names,'bone_angles_deg':dict(zip(names,angles))}
    return {'max_secondary_angle_deg':quaternion_angle_deg(obj.evaluated_get(graph).matrix_world.to_quaternion(),
                                                          source.evaluated_get(graph).matrix_world.to_quaternion()),
            'angle_reference_space':'same_frame_object_world_quaternion','bone_names':[]}

def pq_snapshot(obj, max_vertices=20000, max_triangles=40000):
    if type(max_vertices) is not int or not 1<=max_vertices<=20000 or type(max_triangles) is not int or not 1<=max_triangles<=40000:
        raise ValueError('PHYSICS_QA_CAPACITY_INVALID')
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        if not mesh.vertices or len(mesh.vertices) > max_vertices:
            raise ValueError('PHYSICS_QA_CAPACITY_EXCEEDED')
        mesh.calc_loop_triangles()
        if len(mesh.loop_triangles) > max_triangles:
            raise ValueError('PHYSICS_COLLIDER_TRIANGLE_CAPACITY_EXCEEDED')
        unit = bpy.context.scene.unit_settings.scale_length
        if not math.isfinite(unit) or unit <= 0:
            raise ValueError('PHYSICS_UNIT_SCALE_INVALID')
        matrix = evaluated.matrix_world.copy()
        points = [tuple((matrix@v.co)*unit) for v in mesh.vertices]
        edges = [tuple(e.vertices) for e in mesh.edges]
        triangles = [tuple(t.vertices) for t in mesh.loop_triangles]
        polygons = [tuple(p.vertices) for p in mesh.polygons]
        return {'points': points, 'edges': edges, 'triangles': triangles,'polygons':polygons,
                'vertex_count': len(points), 'polygon_count': len(mesh.polygons)}
    finally:
        evaluated.to_mesh_clear()

def pq_parity(bvh, point, direction, epsilon, max_crossings):
    origin = Vector(point)
    hits = 0
    for index in range(max_crossings):
        location, normal, face, distance = bvh.ray_cast(origin, direction)
        if location is None:
            return hits % 2, False
        hits += 1
        origin = location+direction*epsilon
    return None, True

def pq_collision_frame(points, collider_records, thresholds, sample_radius_m=0.0):
    directions = [Vector((1.0,0.371,0.217)).normalized(),
                  Vector((0.193,1.0,0.419)).normalized(),
                  Vector((0.337,0.157,1.0)).normalized()]
    epsilon = 0.0000001
    total = 0
    max_depth = 0.0
    disagreements = 0
    pairs = []
    errors = []
    if any(not math.isfinite(v) for point in points for v in point):
        return {'passed': False, 'errors': ['NONFINITE_COLLISION_SAMPLE'], 'penetration_count': 0,
                'max_depth_m': None, 'ray_disagreement_points': 0, 'colliders': []}
    for record in collider_records:
        if not record['closed']:
            errors.append('OPEN_OR_NONMANIFOLD_COLLIDER_UNKNOWN: '+record['name'])
            pairs.append({'collider': record['name'], 'status': 'UNKNOWN', 'penetration_count': None, 'max_depth_m': None})
            continue
        count = 0
        depth = 0.0
        bvh = record['bvh']
        for point in points:
            if any(point[i]<record['bbox_min'][i]-epsilon-sample_radius_m or point[i]>record['bbox_max'][i]+epsilon+sample_radius_m for i in range(3)):
                continue
            nearest, normal, face, distance = bvh.find_nearest(Vector(point))
            if nearest is None:
                errors.append('COLLIDER_SURFACE_EMPTY: '+record['name'])
                break
            if distance <= epsilon and sample_radius_m<=0:
                continue
            votes = []
            exhausted = False
            for direction in directions:
                vote, truncated = pq_parity(bvh, point, direction, epsilon, min(40002,record['triangle_count']+2))
                exhausted = exhausted or truncated
                if vote is not None:
                    votes.append(vote)
            if exhausted or len(votes) != 3:
                errors.append('COLLISION_RAY_CAPACITY_EXCEEDED: '+record['name'])
                continue
            disagreements += int(len(set(votes)) > 1)
            if sum(votes) >= 2:
                count += 1
                depth = max(depth, distance+sample_radius_m)
            elif sample_radius_m>distance:
                count += 1
                depth = max(depth,sample_radius_m-distance)
        total += count
        max_depth = max(max_depth, depth)
        pairs.append({'collider': record['name'], 'status': 'MEASURED', 'penetration_count': count, 'max_depth_m': depth})
    if total > thresholds['max_penetrating_vertices']:
        errors.append('PENETRATION_COUNT_EXCEEDED')
    if max_depth > thresholds['max_penetration_depth_m']:
        errors.append('PENETRATION_DEPTH_EXCEEDED')
    return {'passed': not errors, 'errors': errors, 'penetration_count': total,
            'max_depth_m': max_depth, 'ray_disagreement_points': disagreements, 'colliders': pairs}

def pq_collider_records(objects, params):
    records = []
    for obj in objects:
        sample = pq_snapshot(obj, params.get('max_collider_vertices',20000), params.get('max_collider_triangles',40000))
        finite = all(math.isfinite(v) for point in sample['points'] for v in point)
        closed = finite and closed_triangles(sample['triangles'])
        records.append({'name': obj.name, 'closed': closed, 'triangle_count': len(sample['triangles']),
                        'bbox_min': tuple(min(p[i] for p in sample['points']) for i in range(3)),
                        'bbox_max': tuple(max(p[i] for p in sample['points']) for i in range(3)),
                        'bvh': BVHTree.FromPolygons([Vector(p) for p in sample['points']], sample['triangles'], all_triangles=True) if closed else None})
    return records

def pq_collision(params):
    obj = pq_secondary_object(params['candidate_name'])
    colliders = [pq_object(name) for name in params.get('collider_names',[])]
    if not colliders:
        raise ValueError('EXPLICIT_COLLIDERS_REQUIRED')
    if len(colliders) > 8 or obj in colliders:
        raise ValueError('PHYSICS_COLLIDER_SET_INVALID')
    frames = pq_frames(params)
    thresholds = validate_thresholds(params.get('thresholds'))
    radius = params.get('segment_radius_m',0.0) if obj.type=='ARMATURE' else 0.0
    if isinstance(radius,bool) or not isinstance(radius,(int,float)) or not math.isfinite(radius) or not 0<=radius<=10:
        raise ValueError('SECONDARY_SEGMENT_RADIUS_INVALID')
    if obj.type=='ARMATURE':
        pq_bone_names(obj,params)
    scene = bpy.context.scene
    captured_objects,captured_state=pq_capture(params,[obj]+colliders)
    results = []
    try:
        for frame in frames:
            scene.frame_set(frame)
            sample = pq_bone_snapshot(obj,params,segment_samples=True) if obj.type=='ARMATURE' else pq_snapshot(obj,params.get('max_vertices',20000))
            records = pq_collider_records(colliders,params)
            measured = pq_collision_frame(sample['points'],records,thresholds,radius)
            measured['frame'] = frame
            results.append(measured)
    finally:
        px_restore(captured_objects,captured_state)
    affected = [r['frame'] for r in results if r['penetration_count'] > 0]
    fps = scene.render.fps/scene.render.fps_base
    if not math.isfinite(fps) or fps<=0:
        raise ValueError('PHYSICS_FPS_INVALID')
    return {'schema': 'production.physics.collision_qa.v1', 'passed': all(r['passed'] for r in results),
            'frames': results, 'frames_affected': affected, 'affected_sample_duration_seconds': len(affected)/fps,
            'max_penetration_depth_m': max((r['max_depth_m'] for r in results if r['max_depth_m'] is not None),default=None),
            'penetration_vertex_frame_pairs': sum(r['penetration_count'] for r in results),
            'thresholds': thresholds, 'threshold_origin': 'PROPOSED_PARAMETER',
            'coverage': {'sampled_frames': len(frames), 'frame_step': 1, 'all_evaluated_vertices': obj.type=='MESH',
                         'representation': 'evaluated_mesh_vertices' if obj.type=='MESH' else 'bone_segment_samples',
                         'samples_per_bone_segment':9 if obj.type=='ARMATURE' else None,
                         'bone_names':params.get('bone_names',[]) if obj.type=='ARMATURE' else []},
            'segment_radius_m':radius if obj.type=='ARMATURE' else None,
            'radius_confidence':'DECLARED' if obj.type=='ARMATURE' and radius>0 else 'UNKNOWN' if obj.type=='ARMATURE' else 'NOT_APPLICABLE',
            'volumetric_collision_freedom_certified':False,
            'confidence': 'approximate_three_ray_majority_closed_surface' if obj.type=='MESH' else 'limited_sampled_segment_clearance' if radius>0 else 'weak_centerline_only_unknown_radius',
            'limitations': ['Vertex samples can miss edge/face intersections.', 'Finite frames do not certify continuous collision freedom.',
                            'Bone segment radius/clearance sampling is approximate; zero radius is centerline evidence only.',
                            'Open/nonmanifold colliders return UNKNOWN and block the mandatory gate.'],
            'visual_verification': 'required'}

def pq_motion(params):
    obj = pq_secondary_object(params['candidate_name'])
    source = pq_secondary_object(params['source_name']) if params.get('source_name') else None
    if obj.type=='ARMATURE' and source is None:
        raise ValueError('SAME_FRAME_PRIMARY_REFERENCE_REQUIRED')
    if source is not None and source.type!=obj.type:
        raise ValueError('PRIMARY_REFERENCE_OBJECT_TYPE_MISMATCH')
    frames = pq_frames(params)
    thresholds = validate_thresholds(params.get('thresholds'))
    scene = bpy.context.scene
    fps = scene.render.fps/scene.render.fps_base
    if not math.isfinite(fps) or fps<=0:
        raise ValueError('PHYSICS_FPS_INVALID')
    if params.get('fps') is not None and abs(params['fps']-fps)>1e-6:
        raise ValueError('PHYSICS_FPS_CHANGED')
    captured_objects,captured_state=pq_capture(params,[obj]+([source] if source else []))
    previous = None
    previous_previous = None
    previous_reference = None
    older_reference = None
    reference = None
    results = []
    anchors = params.get('anchor_indices',[])
    roots = params.get('root_indices',[])
    if (anchors or roots) and source is None:
        raise ValueError('PRIMARY_REFERENCE_REQUIRED_FOR_ATTACHMENTS')
    origin_anchor = params.get('anchor_mode')=='OBJECT_ORIGIN'
    outside = params.get('outside_region_indices',[])
    secondary_limit = params.get('secondary_displacement_limit_m')
    angle_limit=params.get('secondary_angle_limit_deg')
    if (origin_anchor or outside or secondary_limit is not None or angle_limit is not None) and source is None:
        raise ValueError('SAME_FRAME_PRIMARY_REFERENCE_REQUIRED')
    if secondary_limit is not None and (isinstance(secondary_limit,bool) or not isinstance(secondary_limit,(int,float)) or not math.isfinite(secondary_limit) or secondary_limit<0):
        raise ValueError('SECONDARY_DISPLACEMENT_LIMIT_INVALID')
    if angle_limit is not None and (isinstance(angle_limit,bool) or not isinstance(angle_limit,(int,float)) or not math.isfinite(angle_limit) or not 0<=angle_limit<=180):
        raise ValueError('SECONDARY_ANGLE_LIMIT_INVALID')
    try:
        for frame in frames:
            scene.frame_set(frame)
            sample = pq_motion_snapshot(obj,params)
            if reference is None:
                reference = sample['points']
                if obj.type=='ARMATURE' and not roots:
                    roots = sample['root_indices']
            primary_sample = pq_motion_snapshot(source,params) if source else None
            if primary_sample and not same_topology(sample,primary_sample):
                raise ValueError('PRIMARY_REFERENCE_TOPOLOGY_MISMATCH')
            primary = primary_sample['points'] if source else reference
            measured = motion_step(sample['points'],primary,previous,previous_previous,fps,sample['edges'],sample['triangles'],thresholds,previous_reference,older_reference)
            if secondary_limit is not None and measured.get('metrics',{}).get('max_secondary_displacement_m',0)>secondary_limit+1e-7:
                measured['passed'] = False
                measured['errors'].append('SECONDARY_DISPLACEMENT_BOUND_EXCEEDED')
            if angle_limit is not None and measured.get('metrics'):
                angle=pq_secondary_angle(obj,source,params)
                measured['metrics']['max_secondary_angle_deg']=angle['max_secondary_angle_deg']
                measured['angle_reference_space']=angle['angle_reference_space']
                measured['bone_angles_deg']=angle.get('bone_angles_deg')
                if angle['max_secondary_angle_deg']>angle_limit+0.00001:
                    measured['passed']=False
                    measured['errors'].append('SECONDARY_ANGLE_BOUND_EXCEEDED')
            if measured.get('metrics'):
                for label, indices, threshold in (('anchor',anchors,'anchor_detachment_m'),('root',roots,'root_detachment_m')):
                    if any(type(i) is not int or i<0 or i>=len(primary) for i in indices):
                        raise ValueError('ATTACHMENT_VERTEX_INDEX_INVALID')
                    distance = max((_pq_length(_pq_sub(sample['points'][i],primary[i])) for i in indices),default=0.0)
                    measured['metrics'][label+'_detachment_m'] = distance if indices else None
                    if indices and distance > thresholds[threshold]:
                        measured['passed'] = False
                        measured['errors'].append(label.upper()+'_DETACHMENT_EXCEEDED')
                if origin_anchor:
                    depsgraph = bpy.context.evaluated_depsgraph_get()
                    origin = obj.evaluated_get(depsgraph).matrix_world.translation
                    primary_origin = source.evaluated_get(depsgraph).matrix_world.translation
                    origin_error = (origin-primary_origin).length*scene.unit_settings.scale_length
                    measured['metrics']['anchor_origin_detachment_m'] = origin_error
                    if origin_error>thresholds['anchor_detachment_m']:
                        measured['passed'] = False
                        measured['errors'].append('ANCHOR_ORIGIN_DETACHMENT_EXCEEDED')
                if outside:
                    if any(type(i) is not int or i<0 or i>=len(primary) for i in outside):
                        raise ValueError('OUTSIDE_REGION_VERTEX_INDEX_INVALID')
                    outside_error = max(_pq_length(_pq_sub(sample['points'][i],primary[i])) for i in outside)
                    measured['metrics']['outside_region_max_error_m'] = outside_error
                    if outside_error>0.000001:
                        measured['passed'] = False
                        measured['errors'].append('OUTSIDE_AUTHORIZED_REGION_CHANGED')
            measured['frame'] = frame
            results.append(measured)
            previous_previous, previous = previous,sample['points']
            older_reference,previous_reference = previous_reference,primary
    finally:
        px_restore(captured_objects,captured_state)
    return {'schema': 'production.physics.motion_qa.v1', 'passed': all(r['passed'] for r in results),
            'frames': results, 'thresholds': thresholds, 'threshold_origin': 'PROPOSED_PARAMETER',
            'reference': 'same_frame_primary' if source else 'first_sample',
            'secondary_displacement_limit_m': secondary_limit,
            'secondary_angle_limit_deg':angle_limit,
            'angle_reference_space':next((r['angle_reference_space'] for r in results if 'angle_reference_space' in r),None),
            'secondary_derivatives': 'same_frame_primary_residual_in_world_metres' if source else 'first_sample_relative',
            'world_derivatives_include_primary_motion': True,
            'reference_area_triangulation':'candidate_evaluated_triangles_on_both_coordinate_sets',
            'coverage': {'sampled_frames': len(frames), 'frame_step': 1, 'all_evaluated_vertices': obj.type=='MESH',
                         'representation':'evaluated_mesh_vertices' if obj.type=='MESH' else 'evaluated_bone_heads_tails',
                         'bone_names':params.get('bone_names',[]) if obj.type=='ARMATURE' else []},
            'metrics': {key: max((r['metrics'][key] for r in results if r.get('metrics',{}).get(key) is not None),default=None)
                        for key in ('max_displacement_m','max_velocity_m_s','max_acceleration_m_s2','max_secondary_displacement_m','max_secondary_velocity_m_s','max_secondary_acceleration_m_s2','max_secondary_angle_deg','bbox_growth_ratio','max_edge_stretch_ratio','degenerate_fraction','anchor_detachment_m','root_detachment_m','anchor_origin_detachment_m','outside_region_max_error_m')},
            'geometry_summary':{'min_area_ratio':min((r['geometry']['min_area_ratio'] for r in results if r.get('geometry',{}).get('min_area_ratio') is not None),default=None),
                                'min_absolute_volume_ratio':min((r['geometry']['absolute_volume_ratio'] for r in results if r.get('geometry',{}).get('absolute_volume_ratio') is not None),default=None),
                                'max_absolute_volume_ratio':max((r['geometry']['absolute_volume_ratio'] for r in results if r.get('geometry',{}).get('absolute_volume_ratio') is not None),default=None),
                                'inversion_count':None,'local_face_inversion_certified':False,
                                'global_closed_volume_inversion_frames':[r['frame'] for r in results if r.get('geometry',{}).get('closed_volume_inverted')],
                                'stability_indicators':sorted(set(i for r in results for i in r.get('geometry',{}).get('stability_indicators',[])))},
            'limitations': ['Global volume inversion applies only to a closed consistently oriented mesh.',
                            'Global volume sign cannot count arbitrary local face inversions; inversion_count remains UNKNOWN.',
                            'Open cloth can fold and rotate face normals without inversion.', 'Finite frames do not certify a continuous trajectory.'],
            'visual_verification': 'required'}

def pq_armature_bake_frame(native,baked,names,tolerance,angle_tolerance):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    a = native.evaluated_get(depsgraph)
    b = baked.evaluated_get(depsgraph)
    unit = bpy.context.scene.unit_settings.scale_length
    if not math.isfinite(unit) or unit<=0:
        raise ValueError('PHYSICS_UNIT_SCALE_INVALID')
    squared = []
    angles = []
    for name in names:
        pa,pb = a.pose.bones.get(name),b.pose.bones.get(name)
        if pa is None or pb is None or (pa.parent.name if pa.parent else None)!=(pb.parent.name if pb.parent else None):
            raise ValueError('BAKE_BONE_MAPPING_MISMATCH')
        for x,y in ((a.matrix_world@pa.head,b.matrix_world@pb.head),(a.matrix_world@pa.tail,b.matrix_world@pb.tail)):
            if any(not math.isfinite(v) for v in tuple(x)+tuple(y)):
                raise ValueError('NONFINITE_BAKE_GEOMETRY')
            squared.append((x-y).length_squared*unit*unit)
        wa,wb = a.matrix_world@pa.matrix,b.matrix_world@pb.matrix
        angle = math.degrees(wa.to_quaternion().rotation_difference(wb.to_quaternion()).angle)
        angle = min(angle,abs(360-angle))
        if not math.isfinite(angle):
            raise ValueError('NONFINITE_BAKE_ROTATION')
        angles.append(angle)
    maximum = math.sqrt(max(squared))
    rms = math.sqrt(sum(squared)/len(squared))
    maximum_angle = max(angles)
    return {'max_error_m':maximum,'rms_error_m':rms,'max_angle_error_deg':maximum_angle,
            'bones':len(names),'passed':maximum<=tolerance and maximum_angle<=angle_tolerance}

def pq_bake_compare(params):
    native = bpy.data.objects.get(params['native_candidate_name'])
    baked = bpy.data.objects.get(params['baked_candidate_name'])
    if native is None or baked is None or native.type!=baked.type or native.type not in ('MESH','ARMATURE'):
        raise ValueError('BAKE_OBJECT_TYPE_UNSUPPORTED')
    frames = pq_frames(params)
    tolerance = params.get('tolerance_m',0.00001)
    if isinstance(tolerance,bool) or not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError('BAKE_TOLERANCE_INVALID')
    names = params.get('bone_names',[])
    angle_tolerance = params.get('rotation_tolerance_deg',0.01)
    if isinstance(angle_tolerance,bool) or not isinstance(angle_tolerance,(int,float)) or not math.isfinite(angle_tolerance) or angle_tolerance<0:
        raise ValueError('BAKE_ANGLE_TOLERANCE_INVALID')
    if native.type=='ARMATURE' and (not isinstance(names,list) or not names or len(names)>256 or len(set(names))!=len(names)):
        raise ValueError('EXPLICIT_BAKE_BONE_NAMES_REQUIRED')
    scene = bpy.context.scene
    captured_objects,captured_state=pq_capture(params,[native,baked])
    results = []
    try:
        for frame in frames:
            scene.frame_set(frame)
            if native.type=='ARMATURE':
                measured = pq_armature_bake_frame(native,baked,names,tolerance,angle_tolerance)
                measured['frame'] = frame
                results.append(measured)
                continue
            a = pq_snapshot(native,params.get('max_vertices',20000))
            b = pq_snapshot(baked,params.get('max_vertices',20000))
            if not same_topology(a,b):
                raise ValueError('BAKE_TOPOLOGY_MISMATCH')
            if any(not math.isfinite(v) for point in a['points']+b['points'] for v in point):
                raise ValueError('NONFINITE_BAKE_GEOMETRY')
            squared = [_pq_dot(_pq_sub(p,q),_pq_sub(p,q)) for p,q in zip(a['points'],b['points'])]
            maximum = math.sqrt(max(squared))
            rms = math.sqrt(sum(squared)/len(squared))
            results.append({'frame': frame,'max_error_m': maximum,'rms_error_m': rms,'vertices': len(squared),'passed': maximum <= tolerance})
    finally:
        px_restore(captured_objects,captured_state)
    return {'schema': 'production.physics.bake_equivalence.v1','passed': all(r['passed'] for r in results),
            'frames': results,'max_error_m': max(r['max_error_m'] for r in results),
            'max_rms_error_m': max(r['rms_error_m'] for r in results),'tolerance_m': tolerance,
            'max_angle_error_deg':max((r.get('max_angle_error_deg',0) for r in results)),
            'rotation_tolerance_deg':angle_tolerance if native.type=='ARMATURE' else None,
            'coverage': {'frame_step':1,'all_evaluated_vertices':native.type=='MESH','sampled_frames':len(frames),
                         'bone_names':names if native.type=='ARMATURE' else [],
                         'all_pose_bones':native.type=='ARMATURE' and set(names)==set(native.pose.bones.keys())==set(baked.pose.bones.keys())},
            'limitations':['Equality is measured at every integer frame, not every continuous subframe.'],
            'visual_verification':'required'}
'''
