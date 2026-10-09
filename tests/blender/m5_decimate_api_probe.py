"""Installed Blender RNA and weighted-decimation feasibility (isolated, no saved scene)."""
import bpy
from mathutils.kdtree import KDTree

for reverse in (False, True):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24)
    obj = bpy.context.object
    locked = [v.co.copy() for v in obj.data.vertices if v.co.z > .65]
    group = obj.vertex_groups.new(name='explicit_reduction_weight')
    protected = [v.index for v in obj.data.vertices if v.co.z > .65]
    other = [v.index for v in obj.data.vertices if v.co.z <= .65]
    group.add(protected, 1 if reverse else 0, 'REPLACE')
    group.add(other, 0 if reverse else 1, 'REPLACE')
    modifier = obj.modifiers.new('Explicit_GUIDED_DECIMATION', 'DECIMATE')
    modifier.ratio = .4
    modifier.vertex_group = group.name
    modifier.vertex_group_factor = 1000
    modifier.use_collapse_triangulate = True
    graph = bpy.context.evaluated_depsgraph_get()
    result = bpy.data.meshes.new_from_object(obj.evaluated_get(graph), preserve_all_data_layers=True, depsgraph=graph)
    tree = KDTree(len(result.vertices))
    for vertex in result.vertices:
        tree.insert(vertex.co, vertex.index)
    tree.balance()
    print('WEIGHTED_RESULT', {'protected_weight': 1 if reverse else 0, 'vertices': len(result.vertices),
          'max_protected_vertex_distance': max(tree.find(point)[2] for point in locked),
          'groups_in_output': sum(len(v.groups) for v in result.vertices), 'uv': list(result.uv_layers.keys())}, flush=True)
