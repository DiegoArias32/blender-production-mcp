"""Blender payloads for the typed UV import. Every body passes the upstream safe-mode validator."""

INSPECT = r'''
import bpy
from array import array

def sk_mesh_object(name):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError('UV_OBJECT_MISSING: ' + str(name))
    if obj.type != 'MESH':
        raise ValueError('UV_OBJECT_NOT_MESH: ' + obj.name + ' is ' + obj.type)
    if obj.library or obj.override_library or obj.data.library or obj.data.override_library:
        raise ValueError('UV_LINKED_DATA: linked or overridden mesh data is not modified')
    return obj

def sk_mesh_state(obj):
    mesh = obj.data
    return {'object': obj.name, 'mesh': mesh.name, 'mesh_users': mesh.users, 'mode': obj.mode,
            'vertices': len(mesh.vertices), 'edges': len(mesh.edges), 'polygons': len(mesh.polygons),
            'loops': len(mesh.loops), 'uv_layers': [layer.name for layer in mesh.uv_layers],
            'active_uv': mesh.uv_layers.active.name if mesh.uv_layers.active else None,
            'active_render_uv': next((l.name for l in mesh.uv_layers if l.active_render), None),
            'material_slots': [s.material.name if s.material else None for s in obj.material_slots],
            'shape_keys': len(mesh.shape_keys.key_blocks) if mesh.shape_keys else 0,
            'imported_layers': sorted(mesh.get('production_skin_uv_layers', '').split('|')) if mesh.get('production_skin_uv_layers') else []}
'''

IMPORT = INSPECT + r'''
import math

def sk_float_buffer(count):
    return array('f', bytes(4 * count))

def sk_int_buffer(count):
    return array('i', bytes(4 * count))

def sk_import(params):
    obj = sk_mesh_object(params['object_name'])
    mesh = obj.data
    if obj.mode != 'OBJECT':
        raise ValueError('UV_OBJECT_MODE_REQUIRED: ' + obj.name + ' is in ' + obj.mode + ' mode')
    before = sk_mesh_state(obj)
    if before['loops'] != params['loops']:
        raise ValueError('UV_LOOP_COUNT_MISMATCH: mesh has ' + str(before['loops']) + ' loops, source has ' + str(params['loops']))
    if params['fingerprint'] and params['fingerprint'] != {k: before[k] for k in ('vertices', 'edges', 'polygons', 'loops')}:
        raise ValueError('UV_STALE_TOPOLOGY: mesh counts changed since analysis')
    layer_name = params['layer_name']
    if layer_name in before['uv_layers'] or mesh.attributes.get(layer_name) is not None:
        raise ValueError('UV_LAYER_EXISTS: ' + layer_name + ' already exists; nothing is overwritten')
    if len(mesh.uv_layers) >= 8:
        raise ValueError('UV_LAYER_CAPACITY: Blender meshes hold at most 8 UV maps')
    loops = before['loops']
    # Geometry, topology, materials and every existing UV map are copied before the change and compared after it.
    co_before = sk_float_buffer(len(mesh.vertices) * 3); mesh.vertices.foreach_get('co', co_before)
    idx_before = sk_int_buffer(loops); mesh.loops.foreach_get('vertex_index', idx_before)
    mat_before = sk_int_buffer(len(mesh.polygons)); mesh.polygons.foreach_get('material_index', mat_before)
    uv_before = {}
    for layer in mesh.uv_layers:
        values = sk_float_buffer(loops * 2); layer.uv.foreach_get('vector', values); uv_before[layer.name] = values
    image = bpy.data.images.load(params['carrier_path'], check_existing=False)
    carrier_name = image.name
    try:
        image.colorspace_settings.name = 'Non-Color'
        image.alpha_mode = 'CHANNEL_PACKED'
        width, height = image.size[0], image.size[1]
        if (width, height) != (params['carrier_width'], params['carrier_height']):
            raise ValueError('UV_CARRIER_SIZE: Blender read ' + str((width, height)))
        pixels = sk_float_buffer(width * height * 4)
        image.pixels.foreach_get(pixels)
        source = pixels[:loops * 2]
        probe = {'is_float': image.is_float, 'channels': image.channels, 'colorspace': image.colorspace_settings.name,
                 'alpha_mode': image.alpha_mode, 'width': width, 'height': height}
    finally:
        bpy.data.images.remove(bpy.data.images[carrier_name])
    # Exactly rounded per-channel sums (order independent) plus deterministic exact samples: a
    # transport error (row order, colour transform, alpha premultiplication) changes either.
    sums = [math.fsum(source[0::2]), math.fsum(source[1::2])]
    if sums != [params['sum_u'], params['sum_v']]:
        raise ValueError('UV_CARRIER_MISMATCH: per-channel sums ' + repr(sums) + ' != source ' + repr([params['sum_u'], params['sum_v']]))
    checks = params['checks']
    bad = [[i, source[i], v] for i, v in checks if source[i] != v][:8]
    if bad:
        raise ValueError('UV_CARRIER_MISMATCH: carrier values differ from the source ' + str(bad))
    active_index = mesh.uv_layers.active_index
    render_name = before['active_render_uv']
    layer = mesh.uv_layers.new(name=layer_name, do_init=False)
    if layer is None or layer.name != layer_name:
        raise ValueError('UV_LAYER_CREATE_FAILED: ' + layer_name)
    layer.uv.foreach_set('vector', source)
    readback = sk_float_buffer(loops * 2); layer.uv.foreach_get('vector', readback)
    mesh.uv_layers.active_index = active_index
    for item in mesh.uv_layers:
        item.active_render = item.name == render_name
    names = set(before['imported_layers']) | {layer_name}
    mesh['production_skin_uv_layers'] = '|'.join(sorted(names))
    co_after = sk_float_buffer(len(mesh.vertices) * 3); mesh.vertices.foreach_get('co', co_after)
    idx_after = sk_int_buffer(loops); mesh.loops.foreach_get('vertex_index', idx_after)
    mat_after = sk_int_buffer(len(mesh.polygons)); mesh.polygons.foreach_get('material_index', mat_after)
    preserved = {}
    for name, values in uv_before.items():
        after = sk_float_buffer(loops * 2); mesh.uv_layers[name].uv.foreach_get('vector', after)
        preserved[name] = after == values
    after_state = sk_mesh_state(obj)
    result = {'status': 'success', 'before': before, 'after': after_state, 'layer': layer_name, 'carrier': probe,
              'verification': {'readback_exact': readback == source, 'geometry_unchanged': co_after == co_before,
                               'topology_unchanged': idx_after == idx_before, 'material_indices_unchanged': mat_after == mat_before,
                               'existing_uv_unchanged': preserved,
                               'active_uv_unchanged': after_state['active_uv'] == before['active_uv'],
                               'active_render_uv_unchanged': after_state['active_render_uv'] == before['active_render_uv'],
                               'material_slots_unchanged': after_state['material_slots'] == before['material_slots']}}
    stats = {'u_min': min(readback[0::2]), 'u_max': max(readback[0::2]), 'v_min': min(readback[1::2]), 'v_max': max(readback[1::2])}
    result['written_bounds'] = stats
    failed = [k for k, v in result['verification'].items() if v is not True and not (isinstance(v, dict) and all(v.values()))]
    if failed:
        # Undo our own layer before reporting: a partially verified import is never left behind.
        mesh.uv_layers.remove(mesh.uv_layers[layer_name])
        if before['imported_layers']:
            mesh['production_skin_uv_layers'] = '|'.join(before['imported_layers'])
        elif 'production_skin_uv_layers' in mesh:
            del mesh['production_skin_uv_layers']
        raise ValueError('UV_IMPORT_VERIFICATION_FAILED: ' + ', '.join(failed))
    return result

result = sk_import(params)
'''

STATE = INSPECT + r'''
result = sk_mesh_state(sk_mesh_object(params['object_name']))
'''

REMOVE = INSPECT + r'''
def sk_remove(params):
    obj = sk_mesh_object(params['object_name'])
    mesh = obj.data
    if obj.mode != 'OBJECT':
        raise ValueError('UV_OBJECT_MODE_REQUIRED: ' + obj.name + ' is in ' + obj.mode + ' mode')
    before = sk_mesh_state(obj)
    name = params['layer_name']
    if name not in before['imported_layers']:
        raise ValueError('UV_LAYER_NOT_IMPORTED: ' + name + ' was not created by production.mesh_import_uv_layer; it is not removed')
    if name not in before['uv_layers']:
        raise ValueError('UV_LAYER_MISSING: ' + name)
    active = before['active_uv']; render = before['active_render_uv']
    mesh.uv_layers.remove(mesh.uv_layers[name])
    remaining = [n for n in before['imported_layers'] if n != name]
    if remaining:
        mesh['production_skin_uv_layers'] = '|'.join(remaining)
    else:
        del mesh['production_skin_uv_layers']
    if active and active != name and active in mesh.uv_layers:
        mesh.uv_layers.active = mesh.uv_layers[active]
    for item in mesh.uv_layers:
        item.active_render = item.name == render
    return {'status': 'success', 'removed': name, 'before': before, 'after': sk_mesh_state(obj)}

result = sk_remove(params)
'''
