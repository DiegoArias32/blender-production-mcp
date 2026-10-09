"""Application-neutral M5 contract extension; no Painter invocation or bake relabeling."""
from __future__ import annotations

import math
import inspect
from pathlib import Path

from ..common.asset_safety import current_asset, require_mutable_asset
from ..common.storage import sha256, write_json
from ..checkpoint import create_checkpoint
from . import integrity
from ..uv.algorithms import tile_of

MAP_NAMES = {'base_color': 'BaseColor', 'normal': 'Normal', 'roughness': 'Roughness',
             'metallic': 'Metallic', 'ao': 'AO', 'height': 'Height',
             'opacity': 'Opacity', 'emission': 'Emission'}
ALLOWED_MAPS = set(MAP_NAMES.values()) | {'Curvature', 'Thickness', 'Position', 'WorldSpaceNormal', 'ID'}
OPTIONS = {'bake_pair', 'asset_version', 'expected_maps', 'resolution_targets', 'mesh_maps_expected'}

RELATIONSHIPS = 'import math\n' + inspect.getsource(tile_of) + r'''
import bpy, math
obj = bpy.data.objects.get(params['object_name'])
if obj is None or obj.type != 'MESH':
    raise ValueError('MATERIAL_HANDOFF_MESH_REQUIRED')
if len(obj.data.materials) > 128:
    raise ValueError('MATERIAL_HANDOFF_CAPACITY: at most 128 material slots')
uv = obj.data.uv_layers.active
mapping = []
for index, slot in enumerate(obj.material_slots):
    tiles = set()
    polygons = 0
    for face in obj.data.polygons:
        if face.material_index != index:
            continue
        polygons += 1
        if uv:
            coordinates = []
            for loop in face.loop_indices:
                co = uv.data[loop].uv
                if not all(math.isfinite(float(x)) for x in co):
                    raise ValueError('MATERIAL_UV_NONFINITE')
                coordinates.append((float(co[0]), float(co[1])))
            tile = tile_of(coordinates)
            if tile is None:
                raise ValueError('MATERIAL_HANDOFF_UV_AMBIGUOUS: cara cruzada o fuera de rango; no se inventan tiles intermedios')
            tiles.add(tile)
    mapping.append({'slot':index, 'link':slot.link, 'material':slot.material.name if slot.material else None,
                    'polygons':polygons, 'UDIMs':sorted(tiles)})
result = {'object_name':obj.name, 'active_uv':uv.name if uv else None, 'material_mapping':mapping,
          'UDIMs':sorted(set(t for row in mapping for t in row['UDIMs']))}
'''


def validate_options(options):
    if not isinstance(options, dict) or set(options) - OPTIONS:
        raise ValueError('MATERIAL_HANDOFF_OPTIONS_UNSUPPORTED')
    result = dict(options)
    version = result.get('asset_version')
    if version is not None and (not isinstance(version, str) or not version.strip() or len(version) > 128):
        raise ValueError('MATERIAL_ASSET_VERSION_INVALID')
    for name in ('expected_maps', 'mesh_maps_expected'):
        values = result.get(name)
        if values is not None and (not isinstance(values, list) or len(values) > len(ALLOWED_MAPS)
                                   or any(v not in ALLOWED_MAPS for v in values) or len(set(values)) != len(values)):
            raise ValueError('MATERIAL_HANDOFF_MAPS_INVALID: bounded unique semantic map names required')
    resolutions = result.get('resolution_targets', {'default': 1024})
    if not isinstance(resolutions, dict) or not resolutions or len(resolutions) > 33:
        raise ValueError('MATERIAL_HANDOFF_RESOLUTION_INVALID')
    for tile, value in resolutions.items():
        if tile != 'default' and (not isinstance(tile, str) or not tile.isdigit() or not 1001 <= int(tile) <= 2000):
            raise ValueError('MATERIAL_HANDOFF_TILE_INVALID')
        if isinstance(value, bool) or not isinstance(value, int) or not 32 <= value <= 8192:
            raise ValueError('MATERIAL_HANDOFF_RESOLUTION_INVALID: targets 32..8192; no images generated')
    result['resolution_targets'] = resolutions
    return result


def create_handoff(bridge, storage, operation_id, object_name, material_name, definition,
                   options=None, *, apply=False, candidate=None):
    settings = validate_options(options or {})
    if apply:
        require_mutable_asset(bridge, storage, 'M9 material handoff')
    inputs = {'image_names': sorted({v['image'] for v in definition.get('channels', {}).values()})}
    before = integrity.status(bridge, object_name, material_name, inputs)
    relations = bridge.run(RELATIONSHIPS, {'object_name': object_name})
    asset = current_asset(bridge, storage)
    bake = None
    if settings.get('bake_pair'):
        from ..bake.service import load_pair
        bake = load_pair(bridge, storage, settings['bake_pair'])
        if bake['low'] != object_name:
            raise ValueError('MATERIAL_HANDOFF_BAKE_TARGET_MISMATCH: M5 LOW must be the same source game mesh')
        bake_convention = bake.get('options', {}).get('normal_convention', 'OPENGL')
        if definition['normal_convention'] != 'UNKNOWN' and definition['normal_convention'] != bake_convention:
            raise ValueError('MATERIAL_HANDOFF_NORMAL_CONFLICT: la convención declarada contradice el contrato M5; no se invierte ningún canal')
    expected = settings.get('expected_maps', [MAP_NAMES[k] for k in definition.get('channels', {})])
    semantics = [{'channel': key, 'map': MAP_NAMES[key], 'image': value['image'],
                  'component': value['component'], 'colorspace': value['colorspace']}
                 for key, value in definition.get('channels', {}).items()]
    warnings = []
    readiness = bake['readiness'] if bake else 'UNKNOWN'
    if readiness not in ('READY', 'READY_WITH_WARNINGS', 'NOT_READY', 'UNKNOWN'):
        raise ValueError('MATERIAL_HANDOFF_READINESS_INVALID: unsupported M5 readiness state')
    if readiness not in ('READY', 'READY_WITH_WARNINGS'):
        warnings.append('La preparación de bake es ' + readiness + '; este manifiesto no autoriza un bake.')
    if definition.get('normal_convention') == 'UNKNOWN':
        warnings.append('La convención de normales no está declarada; debe resolverse antes del intercambio.')
    if bake:
        warnings.extend(bake.get('report', {}).get('warnings', []))
    handoff = {
        'schema': 'production.texture_handoff.v2', 'extends': 'production.texture_handoff.v1',
        'application': 'neutral', 'asset_id': asset.get('asset_id'),
        'asset_version': settings.get('asset_version', before['geometry_fingerprint']),
        'source_file': asset.get('path'), 'game_mesh': object_name,
        'high_mesh': bake.get('high') if bake else None,
        'geometry_version': before['geometry_fingerprint'], 'geometry_fingerprint': before['geometry_fingerprint'],
        'source_fingerprint': before['fingerprint'], 'source_material': material_name,
        'candidate_material': candidate,
        'material_semantics': {'material': material_name, 'category': definition['category'],
                               'profile': definition.get('profile'), 'composition_verified': False},
        'material_mapping': relations['material_mapping'], 'active_uv': relations['active_uv'],
        'UDIMs': relations['UDIMs'], 'texture_sets': relations['material_mapping'],
        'expected_channels': semantics, 'expected_maps': expected,
        'normal_convention': definition['normal_convention'],
        'bake_normal_convention': bake.get('options', {}).get('normal_convention', 'OPENGL') if bake else None,
        'tangent_space': {'requested_basis': 'MikkTSpace', 'uv_map': relations['active_uv'],
                          'future_importer_equivalence': 'UNKNOWN',
                          'requirement': 'Preserve final triangulation, UVs, split normals and bitangent signs.'},
        'resolution_targets': settings['resolution_targets'],
        'bake_maps_expected': settings.get('mesh_maps_expected', ['Normal', 'AO']),
        'bake_maps_generated': [], 'bake_readiness': readiness,
        'bake_manifest': settings.get('bake_pair'),
        'bake_report': bake.get('report') if bake else None,
        'dependencies': before.get('dependencies', []), 'exports': [], 'warnings': warnings,
        'limitations': ['Mapas esperados son un contrato; no se generaron texturas ni exportaciones.',
                        'No se abrió ni se modificó Substance Painter ni Unreal.',
                        'El perfil no demuestra composición física ni realismo visual.'],
    }
    if integrity.status(bridge, object_name, material_name, inputs)['fingerprint'] != before['fingerprint']:
        raise ValueError('MATERIAL_HANDOFF_STALE: source changed while assembling manifest')
    if not apply:
        return {'apply': False, 'handoff_plan': handoff, 'readiness': readiness, 'warnings': warnings}
    safety = create_checkpoint(bridge, storage, 'M9 material texture handoff', operation_id,
                               'material_create_handoff')['manifest']
    if bake:
        from ..bake.service import load_pair
        load_pair(bridge, storage, settings['bake_pair'])
    if integrity.status(bridge, object_name, material_name, inputs)['fingerprint'] != before['fingerprint']:
        raise ValueError('MATERIAL_HANDOFF_STALE: source changed before writing')
    folder = storage.reserve(CATEGORY, operation_id)
    path = folder / 'texture-handoff.json'
    write_json(path, handoff)
    write_json(folder / 'integrity.json', {'schema': handoff['schema'], 'sha256': sha256(path)})
    return {'handoff': str(path), 'sha256': sha256(path), 'readiness': readiness,
            'checkpoint': safety, 'source_preserved': True, 'warnings': warnings,
            'application': 'neutral', 'apply': True}


CATEGORY = 'material_handoffs'
