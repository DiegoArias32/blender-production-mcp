"""Complex-skin support without Blender: .npy reader, EXR carrier layout, viewport schema, paged material options."""
from __future__ import annotations

import asyncio
from array import array
import json
import math
import struct

import pytest

from blender_mcp.production.common.bridge import build_code, payload_bytes
from blender_mcp.production.materials import inspection
from blender_mcp.production.materials.service import dispatch
from blender_mcp.production.skin import NAMES, READ_ONLY, uv_import, uv_runtime, uvio, viewport


def npy_bytes(values, descr='<f4', shape=None, fortran=False, magic=b'\x93NUMPY', version=1):
    shape = shape if shape is not None else (len(values) // 2, 2)
    header = "{'descr': '%s', 'fortran_order': %s, 'shape': %r, }" % (descr, fortran, tuple(shape))
    header = header.encode('latin1')
    pad = 64 - (10 + len(header) + 1) % 64
    header += b' ' * pad + b'\n'
    code = 'f' if descr.endswith('f4') else 'd'
    data = array(code, values).tobytes()
    if version == 1:
        return magic + bytes([1, 0]) + struct.pack('<H', len(header)) + header + data
    return magic + bytes([2, 0]) + struct.pack('<I', len(header)) + header + data


def write(tmp_path, name, blob):
    path = tmp_path / name
    path.write_bytes(blob)
    return path


TRICKY = [-1.5, 0.0, 3.75, 1.0000001, 1e-7, -0.25, 9.999, 2.5, 0.123456789, 7.0]


def test_reader_returns_float32_pairs_and_provenance(tmp_path):
    path = write(tmp_path, 'uv.npy', npy_bytes(TRICKY))
    values, info = uvio.read_uv_array(path, expected_loops=5)
    assert values.typecode == 'f' and len(values) == 10
    assert list(values) == list(array('f', TRICKY))
    assert info['loops'] == 5 and info['dtype'] == '<f4' and len(info['sha256']) == 64
    assert info['bounds'] == {'u_min': min(values[0::2]), 'u_max': max(values[0::2]),
                              'v_min': min(values[1::2]), 'v_max': max(values[1::2])}


def test_reader_accepts_npy_v2_and_reports_float64_rounding(tmp_path):
    path = write(tmp_path, 'uv64.npy', npy_bytes(TRICKY, descr='<f8', version=2))
    values, info = uvio.read_uv_array(path)
    assert list(values) == list(array('f', TRICKY))
    assert 0 < info['float64_to_float32_max_rounding'] < 1e-6


@pytest.mark.parametrize('blob,code', [
    (npy_bytes(TRICKY, magic=b'\x93NUMPZ'), 'UV_SOURCE_FORMAT'),
    (npy_bytes(TRICKY, descr='>f4'), 'UV_SOURCE_DTYPE'),
    (npy_bytes(TRICKY, descr='<i4'), 'UV_SOURCE_DTYPE'),
    (npy_bytes(TRICKY, fortran=True), 'UV_SOURCE_LAYOUT'),
    (npy_bytes(TRICKY, shape=(10,)), 'UV_SOURCE_SHAPE'),
    (npy_bytes(TRICKY, shape=(5, 3)), 'UV_SOURCE_SHAPE'),
    (npy_bytes(TRICKY)[:-4], 'UV_SOURCE_TRUNCATED'),
    (npy_bytes(TRICKY[:-1] + [float('nan')]), 'UV_SOURCE_NONFINITE'),
    (npy_bytes(TRICKY[:-1] + [float('inf')]), 'UV_SOURCE_NONFINITE'),
])
def test_reader_refuses_ambiguous_or_unsafe_files(tmp_path, blob, code):
    path = write(tmp_path, 'bad.npy', blob)
    with pytest.raises(ValueError, match=code):
        uvio.read_uv_array(path)


def test_object_arrays_are_refused_from_the_header_without_unpickling(tmp_path):
    header = b"{'descr': '|O', 'fortran_order': False, 'shape': (5, 2), }"
    header += b' ' * (64 - (10 + len(header) + 1) % 64) + b'\n'
    # The payload is a pickle opcode stream that would run code if it were ever unpickled.
    blob = b'\x93NUMPY\x01\x00' + struct.pack('<H', len(header)) + header + b'cos\nsystem\n(S"echo pwned"\ntR.'
    with pytest.raises(ValueError, match='UV_SOURCE_DTYPE'):
        uvio.read_uv_array(write(tmp_path, 'obj.npy', blob))


def test_reader_checks_path_and_loop_count(tmp_path):
    path = write(tmp_path, 'uv.npy', npy_bytes(TRICKY))
    with pytest.raises(ValueError, match='UV_LOOP_COUNT_MISMATCH'):
        uvio.read_uv_array(path, expected_loops=6)
    with pytest.raises(ValueError, match='UV_SOURCE_PATH'):
        uvio.read_uv_array('uv.npy')
    with pytest.raises(ValueError, match='UV_SOURCE_PATH'):
        uvio.read_uv_array(write(tmp_path, 'uv.bin', npy_bytes(TRICKY)))
    with pytest.raises(ValueError, match='UV_SOURCE_PATH'):
        uvio.read_uv_array(tmp_path / 'missing.npy')


def read_exr(path):
    """Independent minimal reader for the uncompressed scanline files the carrier writes."""
    blob = path.read_bytes()
    assert blob[:4] == b'\x76\x2f\x31\x01' and struct.unpack('<i', blob[4:8])[0] == 2
    pos, attrs = 8, {}
    while blob[pos] != 0:
        end = blob.index(b'\0', pos); name = blob[pos:end].decode(); pos = end + 1
        end = blob.index(b'\0', pos); kind = blob[pos:end].decode(); pos = end + 1
        (size,) = struct.unpack('<i', blob[pos:pos + 4]); pos += 4
        attrs[name] = (kind, blob[pos:pos + size]); pos += size
    pos += 1
    x0, y0, x1, y1 = struct.unpack('<iiii', attrs['dataWindow'][1])
    width, height = x1 - x0 + 1, y1 - y0 + 1
    offsets = struct.unpack('<' + str(height) + 'Q', blob[pos:pos + 8 * height])
    channels, cpos, chl = [], 0, attrs['channels'][1]
    while chl[cpos] != 0:
        end = chl.index(b'\0', cpos); channels.append(chl[cpos:end].decode())
        assert struct.unpack('<i', chl[end + 1:end + 5])[0] == 2   # FLOAT
        cpos = end + 1 + 16
    rows = {}
    for offset in offsets:
        y, size = struct.unpack('<ii', blob[offset:offset + 8])
        data = array('f'); data.frombytes(blob[offset + 8:offset + 8 + size])
        rows[y] = {c: data[i * width:(i + 1) * width] for i, c in enumerate(channels)}
    return attrs, width, height, channels, rows


def test_carrier_exr_layout_puts_data_row_zero_on_the_last_scanline(tmp_path, monkeypatch):
    monkeypatch.setattr(uvio, 'CARRIER_WIDTH', 3)
    values = array('f', [float(i) + 0.5 for i in range(2 * 11)])   # 11 loops -> 6 pixels -> 3 x 2
    info = uvio.write_carrier_exr(tmp_path / 'c.exr', values)
    assert (info['width'], info['height'], info['loops']) == (3, 2, 11)
    attrs, width, height, channels, rows = read_exr(tmp_path / 'c.exr')
    assert channels == ['A', 'B', 'G', 'R'] and attrs['compression'][1] == b'\0' and attrs['lineOrder'][1] == b'\0'
    # Rebuild Blender's bottom-up RGBA buffer: buffer row r is EXR scanline height-1-r.
    buffer = []
    for r in range(height):
        row = rows[height - 1 - r]
        for x in range(width):
            buffer += [row['R'][x], row['G'][x], row['B'][x], row['A'][x]]
    assert buffer[:len(values)] == list(values)
    assert buffer[len(values):] == [0.0] * (len(buffer) - len(values))
    with pytest.raises(FileExistsError):
        uvio.write_carrier_exr(tmp_path / 'c.exr', values)


def test_carrier_refuses_odd_or_non_float32_input(tmp_path):
    with pytest.raises(ValueError):
        uvio.write_carrier_exr(tmp_path / 'a.exr', array('f', [1.0, 2.0, 3.0]))
    with pytest.raises(ValueError):
        uvio.write_carrier_exr(tmp_path / 'b.exr', array('d', [1.0, 2.0]))


def test_exact_samples_are_deterministic_bounded_and_include_both_ends():
    picks = uv_import.sample_indices(4_008_008)
    assert picks == uv_import.sample_indices(4_008_008)
    assert picks[0] == 0 and picks[-1] == 4_008_007 and 4_008_006 in picks
    assert len(picks) <= 2 * uv_import.SAMPLES + 4 and all(0 <= i < 4_008_008 for i in picks)
    assert uv_import.sample_indices(1) == [0]


def test_import_payload_stays_under_the_safe_mode_size_limit():
    values = [0.123456789] * 2
    picks = uv_import.sample_indices(23_000_000)
    params = {'object_name': 'Head', 'layer_name': 'UV_GlobalSkin', 'loops': 23_000_000,
              'fingerprint': {'vertices': 1, 'edges': 1, 'polygons': 1, 'loops': 23_000_000},
              'carrier_path': 'C:/x/carrier.exr', 'carrier_width': 4096, 'carrier_height': 2808,
              'sum_u': 1.0, 'sum_v': 2.0, 'checks': [[i, values[0] * (1 + i % 7)] for i in picks]}
    assert payload_bytes(uv_runtime.IMPORT, params) < 200_000
    build_code(uv_runtime.IMPORT, params)


@pytest.mark.parametrize('body', [uv_runtime.IMPORT, uv_runtime.STATE, uv_runtime.REMOVE, viewport.INSPECT,
                                  viewport.CONFIGURE, viewport.RESTORE, viewport.CAPTURE])
def test_every_skin_payload_passes_safe_mode(body):
    build_code(body, {'object_name': 'x', 'layer_name': 'y', 'spec': {}, 'convertible': [], 'output_path': 'p',
                      'max_size': 10, 'keep': False, 'area_index': 0, 'previous': {}})


@pytest.mark.parametrize('name', ['a|b', '', ' lead', 'x' * 64, 'tab\tname'])
def test_layer_names_are_strict(name):
    with pytest.raises(ValueError, match='UV_LAYER_NAME_INVALID'):
        uv_import._layer_name(name)


@pytest.mark.parametrize('spec,code', [
    ({'unknown': 1}, 'unknown keys'),
    ({'shading': {'type': 'MAGIC'}}, 'shading.type'),
    ({'shading': {'gloss': 1}}, 'unknown shading'),
    ({'shading': {'intensity': 50}}, 'intensity'),
    ({'shading': {'rotate_z_deg': float('nan')}}, 'rotate_z_deg'),
    ({'view': {'orientation': 'FRONT', 'rotation': [1, 0, 0, 0]}}, 'not both'),
    ({'view': {'orientation': 'DIAGONAL'}}, 'orientation'),
    ({'view': {'rotation': [0, 0, 0, 0]}}, 'zero'),
    ({'view': {'target': {'object': 'A', 'point': [0, 0, 0]}}}, 'target'),
    ({'view': {'target': {'point': [0, 0]}}}, 'target.point'),
    ({'view': {'distance': -1}}, 'distance'),
    ({'view': {'perspective': 'CAMERA'}}, 'perspective'),
    ({'area_index': True}, 'area_index'),
    ({'allow_area_conversion': 'yes'}, 'allow_area_conversion'),
])
def test_viewport_spec_is_closed(spec, code):
    with pytest.raises(ValueError, match='VIEWPORT_SPEC_INVALID') as error:
        viewport.validate_spec(spec)
    assert code in str(error.value)


def test_viewport_signature_ignores_float_noise_but_not_real_changes():
    state = {'shading': {'type': 'MATERIAL', 'studio_light': 'forest.exr', 'studiolight_rotate_z': 0.5235987},
             'view': {'distance': 0.4, 'rotation': [0.7071068, 0.7071068, 0.0, 0.0]}, 'overlays': False}
    noisy = json.loads(json.dumps(state)); noisy['view']['distance'] = 0.40000000001
    changed = json.loads(json.dumps(state)); changed['shading']['studio_light'] = 'city.exr'
    assert viewport.signature(state) == viewport.signature(noisy) != viewport.signature(changed)


def test_paged_material_options_are_bounded():
    assert inspection.paged_options({'graph_mode': 'paged'})['page'] == {
        'node_offset': 0, 'node_limit': 50, 'link_offset': 0, 'link_limit': 50, 'sockets': False}
    assert inspection.paged_options({'detail': 'nodes', 'limit': 50})['page']['sockets'] is True
    for bad in ({'limit': 0}, {'limit': 101}, {'detail': 'nodes', 'limit': 51}, {'offset': -1}, {'link_limit': 201},
                {'limit': True}):
        with pytest.raises(ValueError, match='MATERIAL_PAGINATION_INVALID'):
            inspection.paged_options(bad)
    with pytest.raises(ValueError, match='MATERIAL_PAGED_OPTION_UNSUPPORTED'):
        inspection.paged_options({'uv': {}})
    with pytest.raises(ValueError, match='MATERIAL_DETAIL_INVALID'):
        inspection.paged_options({'detail': 'images'})
    assert inspection.PAGED_CAPS['nodes'] > 256 and inspection.PAGED_CAPS['sockets'] > 4096


def test_paged_mode_is_inspection_only_and_full_mode_keeps_its_guard():
    for name in ('material_validate_graph', 'material_build_shader', 'material_quality_report'):
        with pytest.raises(ValueError, match='MATERIAL_GRAPH_MODE_UNSUPPORTED'):
            dispatch(name, None, None, 'op', 'Head', None, options={'graph_mode': 'paged'})
    with pytest.raises(ValueError, match='MATERIAL_PAGED_INSPECTION_ONLY'):
        dispatch('material_inspect', None, None, 'op', 'Head', None, options={'graph_mode': 'paged'}, apply=True)
    with pytest.raises(ValueError, match='MATERIAL_GRAPH_MODE_UNSUPPORTED'):
        inspection.inspect(None, 'Head', None, {'graph_mode': 'paged'})
    assert "budget['nodes']>256 or budget['sockets']>4096" in inspection.RUNTIME


def test_skin_tools_registered_with_honest_hints():
    from blender_mcp.production.server import mcp
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in NAMES:
        tool = tools['production.' + name]
        assert tool.annotations.readOnlyHint is (name in READ_ONLY)
        assert ('operation_id' in tool.inputSchema['properties']) is (name not in READ_ONLY)
        assert tool.annotations.destructiveHint is False
