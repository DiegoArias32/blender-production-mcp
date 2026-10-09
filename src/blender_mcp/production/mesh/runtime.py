"""Blender-side mesh state capture (M3.1 safety foundation for M4; no modeling operations).

Every section returns compact metadata plus a fingerprint. Per-vertex data never leaves Blender
except for explicitly requested locked-region positions (``mr_capture``), which the host stores in a
local manifest and never returns to the model.
"""
from ..common.digest import DIGEST_RUNTIME

MESH_RUNTIME = DIGEST_RUNTIME + r'''
import bpy
import json
import math

MS_MARKER = 'production_candidate'

def ms_mesh_object(name):
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type != 'MESH':
        raise ValueError('MESH_REQUIRED: ' + str(name) + ' is not a mesh object')
    if obj.mode in ('EDIT', 'SCULPT'):
        # Edit Mode keeps edits in a separate BMesh and dynamic-topology sculpting does too: the mesh
        # data read here could be stale. The mode is never changed automatically.
        raise ValueError('MODE_NOT_SUPPORTED: ' + obj.name + ' is in ' + obj.mode + ' mode; return to Object mode first')
    return obj

def ms_floats(collection, attribute, width):
    values = [0.0] * (len(collection) * width)
    collection.foreach_get(attribute, values)
    return values

def ms_ints(collection, attribute, width):
    values = [0] * (len(collection) * width)
    collection.foreach_get(attribute, values)
    return values

def ms_bools(collection, attribute):
    values = [False] * len(collection)
    collection.foreach_get(attribute, values)
    return values

def ms_matrix(matrix):
    return [list(row) for row in matrix]

def ms_topology(me):
    edges = ms_ints(me.edges, 'vertices', 2)
    totals = ms_ints(me.polygons, 'loop_total', 1)
    corners = ms_ints(me.loops, 'vertex_index', 1)
    return {'vertices': len(me.vertices), 'edges': len(me.edges), 'polygons': len(me.polygons),
            'loops': len(me.loops), 'triangles': sum(totals) - 2 * len(totals),
            'fingerprint': m2_digest([len(me.vertices), edges, totals, corners])}

def ms_geometry(me):
    return {'fingerprint': m2_digest(ms_floats(me.vertices, 'co', 3))}

def ms_shape_keys(me):
    key = me.shape_keys
    if key is None:
        return {'present': False, 'names': [], 'keys': [], 'drivers': [], 'fingerprint': m2_digest(None)}
    rows = []
    for block in key.key_blocks:
        rows.append({'name': block.name, 'relative_key': block.relative_key.name if block.relative_key else None,
                     'vertex_group': block.vertex_group, 'slider_min': block.slider_min, 'slider_max': block.slider_max,
                     'mute': block.mute, 'interpolation': block.interpolation, 'value': block.value,
                     'data': m2_digest(ms_floats(block.data, 'co', 3))})
    drivers = []
    if key.animation_data:
        drivers = sorted([d.data_path, d.array_index] for d in key.animation_data.drivers)
    return {'present': True, 'use_relative': key.use_relative, 'names': [row['name'] for row in rows], 'keys': rows,
            'drivers': drivers, 'fingerprint': m2_digest([key.use_relative, rows, drivers])}

def ms_vertex_groups(obj, me):
    groups = [{'name': g.name, 'index': g.index, 'lock_weight': g.lock_weight} for g in obj.vertex_groups]
    counts = [0] * len(groups)
    sums = [0.0] * len(groups)
    pairs = []
    for vertex in me.vertices:
        for element in vertex.groups:
            pairs.append([vertex.index, element.group, element.weight])
            if 0 <= element.group < len(groups):
                counts[element.group] += 1
                sums[element.group] += element.weight
    for group in groups:
        group['weighted_vertices'] = counts[group['index']]
        group['weight_sum'] = sums[group['index']]
    weights = m2_digest(pairs)
    return {'names': [g['name'] for g in groups], 'active_index': obj.vertex_groups.active_index, 'groups': groups,
            'assignments': len(pairs), 'weights_fingerprint': weights, 'fingerprint': m2_digest([groups, weights])}

def ms_uv_layers(me):
    starts = ms_ints(me.polygons, 'loop_start', 1)
    totals = ms_ints(me.polygons, 'loop_total', 1)
    layers = []
    for layer in me.uv_layers:
        values = [0.0] * (2 * len(me.loops))
        layer.uv.foreach_get('vector', values)
        tiles = set()
        outside = 0
        for polygon in range(len(starts)):
            first = starts[polygon]
            count = totals[polygon]
            u = sum(values[2 * c] for c in range(first, first + count)) / count
            v = sum(values[2 * c + 1] for c in range(first, first + count)) / count
            # UDIM tile of the face centroid: 1001 + floor(u) + 10 * floor(v), valid for 0 <= u < 10.
            if u < 0 or v < 0 or u >= 10:
                outside += 1
                continue
            tiles.add(1001 + int(math.floor(u)) + 10 * int(math.floor(v)))
        layers.append({'name': layer.name, 'active': layer.active, 'active_render': layer.active_render,
                       'udim_tiles': sorted(tiles)[:200], 'udim_tile_count': len(tiles),
                       'faces_outside_udim_range': outside, 'data': m2_digest(values)})
    return {'names': [l['name'] for l in layers], 'layers': layers,
            'fingerprint': m2_digest([[l['name'], l['active'], l['active_render'], l['data']] for l in layers])}

def ms_normals(me):
    custom = bool(me.has_custom_normals)
    data = None
    attribute = me.attributes.get('custom_normal')
    if custom and attribute is not None:
        # The stored custom-normal data, not the evaluated corner normals: in Blender 5.2 the stored
        # INT16_2D values are relative to the automatic normals, so moving a vertex changes the
        # evaluated normals (a geometry change) without changing the custom-normal state.
        if attribute.data_type == 'INT16_2D':
            values = [0] * (2 * len(attribute.data))
            attribute.data.foreach_get('value', values)
        else:
            values = [0.0] * (3 * len(attribute.data))
            attribute.data.foreach_get('vector', values)
        data = m2_digest([attribute.domain, attribute.data_type, values])
    sharp = {}
    for name in ('sharp_face', 'sharp_edge'):
        attribute = me.attributes.get(name)
        if attribute is not None:
            values = ms_bools(attribute.data, 'value')
            # Absent and all-False mean the same; Blender drops an all-False flag on leaving Edit Mode.
            if any(values):
                sharp[name] = m2_digest(values)
    return {'domain': me.normals_domain, 'has_custom_normals': custom, 'custom_normals': data, 'sharp': sharp,
            'fingerprint': m2_digest([me.normals_domain, custom, data, sharp])}

def ms_materials(obj, me):
    slots = [{'index': i, 'link': slot.link, 'material': slot.material.name if slot.material else None}
             for i, slot in enumerate(obj.material_slots)]
    indices = ms_ints(me.polygons, 'material_index', 1)
    return {'slots': slots, 'names': [s['material'] for s in slots],
            'fingerprint': m2_digest([slots, m2_digest(indices)])}

def ms_modifiers(obj):
    rows = []
    for modifier in obj.modifiers:
        row = {'name': modifier.name, 'type': modifier.type, 'show_viewport': modifier.show_viewport,
               'show_render': modifier.show_render}
        if modifier.type == 'ARMATURE':
            row.update(object=modifier.object.name if modifier.object else None, vertex_group=modifier.vertex_group,
                       invert_vertex_group=modifier.invert_vertex_group, use_vertex_groups=modifier.use_vertex_groups,
                       use_bone_envelopes=modifier.use_bone_envelopes,
                       use_deform_preserve_volume=modifier.use_deform_preserve_volume,
                       use_multi_modifier=modifier.use_multi_modifier)
        rows.append(row)
    return {'modifiers': rows, 'armature_modifiers': [r for r in rows if r['type'] == 'ARMATURE'],
            'fingerprint': m2_digest(rows)}

def ms_relationships(obj):
    section = {'parent': obj.parent.name if obj.parent else None, 'parent_type': obj.parent_type,
               'parent_bone': obj.parent_bone, 'matrix_parent_inverse': ms_matrix(obj.matrix_parent_inverse),
               'collections': sorted(c.name for c in obj.users_collection),
               'constraints': [[c.name, c.type] for c in obj.constraints],
               'object_action': obj.animation_data.action.name if obj.animation_data and obj.animation_data.action else None,
               'object_drivers': sorted([d.data_path, d.array_index] for d in obj.animation_data.drivers) if obj.animation_data else []}
    section['fingerprint'] = m2_digest(section)
    return section

def ms_transform(obj):
    section = {'location': list(obj.location), 'rotation_mode': obj.rotation_mode,
               'rotation_euler': list(obj.rotation_euler), 'rotation_quaternion': list(obj.rotation_quaternion),
               'scale': list(obj.scale), 'delta_location': list(obj.delta_location),
               'delta_scale': list(obj.delta_scale), 'matrix_world': ms_matrix(obj.matrix_world)}
    section['fingerprint'] = m2_digest(section)
    return section

def ms_value(value):
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if hasattr(value, 'to_dict'):
        return value.to_dict()
    if hasattr(value, 'to_list'):
        return value.to_list()
    if hasattr(value, 'name'):
        return 'ID:' + str(value.name)
    return 'unserializable'

def ms_properties(owner):
    rows = {}
    for key in owner.keys():
        if key == MS_MARKER:
            continue  # the candidate's own provenance marker is not user data
        value = ms_value(owner[key])
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            value = 'unserializable'
        rows[key] = value
    return rows

def ms_custom_properties(obj, me):
    section = {'object': ms_properties(obj), 'mesh': ms_properties(me)}
    section['names'] = {'object': sorted(section['object']), 'mesh': sorted(section['mesh'])}
    section['fingerprint'] = m2_digest([section['object'], section['mesh']])
    return section

def ms_attributes(me):
    kept = ('.sculpt_face_set', '.sculpt_mask')
    covered = ('sharp_face', 'sharp_edge')  # canonicalized in the normals section
    rows = sorted([a.name, a.domain, a.data_type] for a in me.attributes
                  if (not a.name.startswith('.') or a.name in kept) and a.name not in covered)
    return {'attributes': rows, 'fingerprint': m2_digest(rows)}

def ms_signature(obj):
    me = obj.data
    sections = {'topology': ms_topology(me), 'geometry': ms_geometry(me), 'shape_keys': ms_shape_keys(me),
                'vertex_groups': ms_vertex_groups(obj, me), 'uv_layers': ms_uv_layers(me), 'normals': ms_normals(me),
                'materials': ms_materials(obj, me), 'modifiers': ms_modifiers(obj),
                'relationships': ms_relationships(obj), 'transform': ms_transform(obj),
                'custom_properties': ms_custom_properties(obj, me), 'attributes': ms_attributes(me)}
    fingerprints = {name: section['fingerprint'] for name, section in sections.items()}
    return {'schema': 'production.mesh_signature.v1', 'object': obj.name, 'mesh': me.name,
            'linked': bool(obj.library or me.library or obj.override_library), 'sections': sections,
            'fingerprints': fingerprints, 'fingerprint': m2_digest(fingerprints)}

def mr_region_indices(obj, me, definition):
    method = definition['method']
    if method == 'vertex_group':
        group = obj.vertex_groups.get(definition['name'])
        if group is None:
            raise ValueError('REGION_UNDEFINED: vertex group ' + definition['name'] + ' does not exist')
        minimum = definition.get('min_weight', 0.5)
        return [v.index for v in me.vertices if any(e.group == group.index and e.weight >= minimum for e in v.groups)]
    if method == 'mask':
        attribute = me.attributes.get('.sculpt_mask')
        if attribute is None or attribute.domain != 'POINT':
            raise ValueError('REGION_UNDEFINED: the mesh has no sculpt mask')
        values = ms_floats(attribute.data, 'value', 1)
        minimum = definition.get('min_mask', 0.5)
        return [i for i, value in enumerate(values) if value >= minimum]
    if method == 'face_set':
        attribute = me.attributes.get('.sculpt_face_set')
        if attribute is None or attribute.domain != 'FACE':
            raise ValueError('REGION_UNDEFINED: the mesh has no face sets')
        wanted = set(definition['ids'])
        ids = ms_ints(attribute.data, 'value', 1)
        starts = ms_ints(me.polygons, 'loop_start', 1)
        totals = ms_ints(me.polygons, 'loop_total', 1)
        corners = ms_ints(me.loops, 'vertex_index', 1)
        found = set()
        for polygon, face_set in enumerate(ids):
            if face_set in wanted:
                found.update(corners[starts[polygon]:starts[polygon] + totals[polygon]])
        return sorted(found)
    if method == 'indices':
        count = len(me.vertices)
        indices = sorted(set(definition['indices']))
        if any(i < 0 or i >= count for i in indices):
            raise ValueError('REGION_UNDEFINED: explicit vertex index outside the mesh')
        return indices
    if method == 'bounds':
        # Object-space axis-aligned box: compact, recomputed in Blender, never shipped as indices (M4).
        low, high = definition['min'], definition['max']
        co = ms_floats(me.vertices, 'co', 3)
        return [i for i in range(len(me.vertices))
                if low[0] <= co[3 * i] <= high[0] and low[1] <= co[3 * i + 1] <= high[1] and low[2] <= co[3 * i + 2] <= high[2]]
    if method == 'dominant_group':
        # Vertices whose strongest weight, among the listed deform groups, belongs to `groups` (M4).
        # A tie counts for every tied group, so the result never depends on vertex-group order
        # (with first-wins, a centre-line vertex split 50/50 between .L and .R went to .L only).
        among = set(definition['among'])
        wanted = set(definition['groups'])
        names = [g.name for g in obj.vertex_groups]
        found = []
        for vertex in me.vertices:
            best, mine = 0.0, 0.0
            for element in vertex.groups:
                if 0 <= element.group < len(names) and names[element.group] in among:
                    best = max(best, element.weight)
                    if names[element.group] in wanted:
                        mine = max(mine, element.weight)
            if mine > 0 and mine >= best:
                found.append(vertex.index)
        return found
    raise ValueError('Unknown region method: ' + str(method))

def mr_capture(obj, definition):
    me = obj.data
    topology = ms_topology(me)
    if definition['method'] == 'indices' and definition.get('topology_fingerprint') not in (None, topology['fingerprint']):
        raise ValueError('TOPOLOGY_CHANGED: explicit indices are bound to another topology')
    indices = mr_region_indices(obj, me, definition)
    basis = ms_floats(me.vertices, 'co', 3)
    positions = []
    for index in indices:
        positions.extend(basis[3 * index:3 * index + 3])
    keys = {}
    if me.shape_keys is not None:
        for block in me.shape_keys.key_blocks:
            data = ms_floats(block.data, 'co', 3)
            values = []
            for index in indices:
                values.extend(data[3 * index:3 * index + 3])
            keys[block.name] = values
    # Deformation weights of the region, by group name (indices can be renumbered by edits elsewhere).
    names = [g.name for g in obj.vertex_groups]
    weights = [sorted([names[e.group], e.weight] for e in me.vertices[index].groups if 0 <= e.group < len(names))
               for index in indices]
    # Faces fully inside the region: membership, material assignment and UV coordinates (M4).
    members = set(indices)
    starts = ms_ints(me.polygons, 'loop_start', 1)
    totals = ms_ints(me.polygons, 'loop_total', 1)
    corners = ms_ints(me.loops, 'vertex_index', 1)
    materials = ms_ints(me.polygons, 'material_index', 1)
    faces = [p for p in range(len(starts)) if all(corners[c] in members for c in range(starts[p], starts[p] + totals[p]))]
    uv = {}
    for layer in me.uv_layers:
        values = [0.0] * (2 * len(me.loops))
        layer.uv.foreach_get('vector', values)
        uv[layer.name] = m2_digest([values[2 * c:2 * c + 2] for p in faces for c in range(starts[p], starts[p] + totals[p])])
    return {'topology_fingerprint': topology['fingerprint'], 'vertices': topology['vertices'], 'indices': indices,
            'indices_digest': m2_digest(indices), 'positions': positions, 'shape_keys': keys,
            'weights_digest': m2_digest(weights), 'faces_digest': m2_digest(faces), 'face_count': len(faces),
            'materials_digest': m2_digest([materials[p] for p in faces]), 'uv_digests': uv,
            'matrix_world': ms_matrix(obj.matrix_world), 'unit_scale': bpy.context.scene.unit_settings.scale_length}
'''

SIGNATURE = MESH_RUNTIME + r'''
result = ms_signature(ms_mesh_object(params['object_name']))
'''

REGION_CAPTURE = MESH_RUNTIME + r'''
obj = ms_mesh_object(params['object_name'])
result = {'regions': [dict(mr_capture(obj, item['definition']), name=item['name']) for item in params['regions']]}
'''

CANDIDATE_FIND = MESH_RUNTIME + r'''
source = ms_mesh_object(params['object_name'])
rows = []
for obj in bpy.data.objects:
    raw = obj.get(MS_MARKER)
    if obj.type != 'MESH' or not isinstance(raw, str):
        continue
    marker = json.loads(raw)
    if marker.get('source_object') == source.name and marker.get('source_fingerprint') == params['source_fingerprint']:
        rows.append(obj.name)
result = {'candidates': sorted(rows)}
'''

CANDIDATE_CREATE = MESH_RUNTIME + r'''
if bpy.context.mode != 'OBJECT':
    raise ValueError('MODE_NOT_SUPPORTED: candidate mesh creation requires Object mode; mode is never changed automatically')
source = ms_mesh_object(params['object_name'])
if source.library or source.data.library or source.override_library:
    raise ValueError('LINKED_MESH_UNSUPPORTED: linked or overridden meshes cannot get a local candidate here')
before = ms_signature(source)
if before['fingerprint'] != params['source_fingerprint']:
    raise ValueError('STALE_SIGNATURE: the source mesh changed after its signature was taken; take a new signature')
name = source.name + '__MESHC_' + params['suffix']
if bpy.data.objects.get(name) is not None:
    raise ValueError('CANDIDATE_EXISTS: ' + name)
candidate = source.copy()
candidate.data = source.data.copy()
candidate.name = name
candidate.data.name = source.data.name + '__MESHC_' + params['suffix']
for collection in source.users_collection:
    collection.objects.link(candidate)
candidate[MS_MARKER] = json.dumps({'source_object': source.name, 'source_mesh': source.data.name,
                                   'source_fingerprint': before['fingerprint'], 'operation_id': params['operation_id']})
# A candidate never changes what the scene renders; it stays visible in the viewport for review.
candidate.hide_render = True
after = ms_signature(source)
result = {'candidate': candidate.name, 'candidate_mesh': candidate.data.name,
          'source_intact': after['fingerprint'] == before['fingerprint'],
          'source_signature': before, 'candidate_signature': ms_signature(candidate),
          'visibility': {'hide_render': candidate.hide_render, 'hide_viewport': candidate.hide_viewport}}
'''

CANDIDATE_PAIR = MESH_RUNTIME + r'''
source = ms_mesh_object(params['object_name'])
candidate = ms_mesh_object(params['candidate_name'])
raw = candidate.get(MS_MARKER)
marker = json.loads(raw) if isinstance(raw, str) else None
if marker is None or marker.get('source_object') != source.name:
    raise ValueError('NOT_A_CANDIDATE: ' + candidate.name + ' is not a registered candidate of ' + source.name)
result = {'marker': marker, 'source_signature': ms_signature(source), 'candidate_signature': ms_signature(candidate)}
'''
