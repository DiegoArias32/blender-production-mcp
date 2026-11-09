"""Real-Blender validation of the complex-skin support (own background Blender, controlled fixtures only).

Runs the production skin services in-process through a local bridge that executes exactly the payloads the
socket bridge would send (same build_code linker and safe-mode validation). Never contacts another Blender.

    blender -b --factory-startup --disable-autoexec --python tests/blender/skin_probe.py -- <out_dir> <suite>[,<suite>...]

Suites: uv, materials, performance. Results: <out_dir>/skin_<suite>.json.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
package = types.ModuleType('blender_mcp')
package.__path__ = [str(ROOT / 'src' / 'blender_mcp')]
sys.modules.setdefault('blender_mcp', package)

import bpy  # noqa: E402
import numpy as np  # noqa: E402  (probe-side independent check only; never inside payloads)

from blender_mcp.production.common.bridge import build_code  # noqa: E402
from blender_mcp.production.common.storage import Storage  # noqa: E402
from m31_fixture_support import register_open_fixture  # noqa: E402


class LocalBridge:
    """Executes the linked, validated payload in this Blender (what the add-on would execute)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.calls = 0
        self.bytes = 0

    def run(self, body, params=None):
        code = build_code(body, params)
        self.calls += 1
        self.bytes += len(code.encode('utf-8'))
        ns = {}
        exec(code.rsplit('print(', 1)[0], ns)
        return ns['result']


class Probe:
    def __init__(self, out, suite):
        self.out = Path(out)
        self.suite = suite
        self.report = {'suite': suite, 'blender': bpy.app.version_string, 'checks': [], 'metrics': {}, 'errors': []}
        self.bridge = LocalBridge()

    def save(self):
        (self.out / ('skin_' + self.suite + '.json')).write_text(json.dumps(self.report, indent=1, default=str), encoding='utf-8')

    def check(self, name, condition, data=None):
        row = {'name': name, 'passed': bool(condition), 'data': data}
        self.report['checks'].append(row)
        print(('PASS ' if condition else 'FAIL ') + name, flush=True)
        self.save()
        return bool(condition)

    def expect_error(self, name, code, function):
        try:
            result = function()
        except Exception as exc:
            message = str(exc)
            return self.check(name + ' -> ' + code, code in message.split(':')[0] or message.startswith(code),
                              {'error': message[:400]})
        return self.check(name + ' -> ' + code, False, {'unexpected_success': str(result)[:400]})


def fresh_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def skin_mesh(name='SkinProbe', subdivisions=5):
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=subdivisions, radius=1.0)
    obj = bpy.context.active_object
    obj.name = name
    mesh = obj.data
    mesh.uv_layers.new(name='UVMap')
    detail = mesh.uv_layers.new(name='UV_Detail')
    values = np.random.default_rng(7).random(len(mesh.loops) * 2, dtype=np.float32)
    detail.uv.foreach_set('vector', values)
    mesh.uv_layers.active_index = 0
    for name_ in ('Skin', 'Eyes'):
        mat = bpy.data.materials.new(name_)
        mesh.materials.append(mat)
    idx = np.zeros(len(mesh.polygons), dtype=np.int32)
    idx[::3] = 1
    mesh.polygons.foreach_set('material_index', idx)
    return obj


def save_fixture(out, name):
    path = Path(out) / (name + '.blend')
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return path


def tricky_uv(loops, dtype=np.float32):
    i = np.arange(loops, dtype=np.float64)
    u = np.mod(i * 0.6180339887498949, 7.0) - 1.5          # negatives and UDIM columns > 1
    v = np.sin(i * 0.7) * 3.3 + 1e-7 * i                    # full mantissa use
    return np.stack([u, v], axis=1).astype(dtype)


def uv_suite(probe, out):
    from blender_mcp.production.skin import uv_import
    fresh_scene()
    obj = skin_mesh()
    loops = len(obj.data.loops)
    fixture = save_fixture(out, 'skin_uv_fixture')
    storage = Storage(Path(out) / 'store_uv')
    register_open_fixture(probe.bridge, storage, out)
    source = tricky_uv(loops)
    npy = Path(out) / 'uv_source_f32.npy'
    np.save(npy, source)
    co_before = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get('co', co_before)
    detail_before = np.empty(loops * 2, dtype=np.float32)
    obj.data.uv_layers['UV_Detail'].uv.foreach_get('vector', detail_before)

    analysis = uv_import.analyze(probe.bridge, obj.name, str(npy), 'UV_GlobalSkin')
    analysis.pop('_values', None)
    probe.check('analyze is ready and writes nothing', analysis['ready'] and 'UV_GlobalSkin' not in obj.data.uv_layers,
                {k: analysis[k] for k in ('fingerprint', 'source', 'warnings', 'udim_tiles_at_bounds')})

    started = time.perf_counter()
    result = uv_import.apply(probe.bridge, storage, 'skinprobe-apply-1', obj.name, str(npy), 'UV_GlobalSkin')
    probe.report['metrics']['apply_seconds_loops_' + str(loops)] = round(time.perf_counter() - started, 3)
    written = np.empty(loops * 2, dtype=np.float32)
    obj.data.uv_layers['UV_GlobalSkin'].uv.foreach_get('vector', written)
    probe.check('independent numpy check: layer equals source bit for bit',
                np.array_equal(written.view(np.uint32), source.reshape(-1).view(np.uint32)),
                {'loops': loops, 'max_abs_diff': float(np.max(np.abs(written - source.reshape(-1))))})
    probe.check('verification block all true', all(v is True or (isinstance(v, dict) and all(v.values()))
                                                   for v in result['verification'].values()), result['verification'])
    co_after = np.empty_like(co_before); obj.data.vertices.foreach_get('co', co_after)
    detail_after = np.empty_like(detail_before); obj.data.uv_layers['UV_Detail'].uv.foreach_get('vector', detail_after)
    probe.check('geometry and existing UV maps unchanged (independent)',
                np.array_equal(co_after, co_before) and np.array_equal(detail_after, detail_before))
    probe.check('active and render UV unchanged', obj.data.uv_layers.active.name == 'UVMap'
                and next(l.name for l in obj.data.uv_layers if l.active_render) == 'UVMap',
                {'active': obj.data.uv_layers.active.name})
    probe.check('carrier read as float RGBA non-color channel packed', result['carrier_read']['is_float']
                and result['carrier_read']['channels'] == 4, result['carrier_read'])
    probe.check('carrier image removed from bpy.data', not any(i.name.startswith('carrier') for i in bpy.data.images),
                [i.name for i in bpy.data.images])
    probe.check('carrier file deleted, checkpoint kept', result['carrier_removed'] and Path(result['checkpoint']).is_file(),
                {'checkpoint': result['checkpoint'], 'samples': result['exact_samples_checked']})
    probe.report['metrics']['carrier'] = result['carrier']

    # float64 source is rounded to the float32 Blender stores; the rounding is reported, not hidden.
    npy64 = Path(out) / 'uv_source_f64.npy'
    np.save(npy64, tricky_uv(loops, np.float64))
    r64 = uv_import.apply(probe.bridge, storage, 'skinprobe-apply-f64', obj.name, str(npy64), 'UV_F64')
    w64 = np.empty(loops * 2, dtype=np.float32); obj.data.uv_layers['UV_F64'].uv.foreach_get('vector', w64)
    probe.check('float64 source stored as its float32 rounding', np.array_equal(w64, tricky_uv(loops, np.float64).reshape(-1).astype(np.float32)),
                {'reported_rounding': r64['source']['float64_to_float32_max_rounding']})

    # Negatives: every refusal leaves the mesh as it was.
    layers_before = [l.name for l in obj.data.uv_layers]
    probe.expect_error('existing layer name', 'UV_LAYER_EXISTS',
                       lambda: uv_import.apply(probe.bridge, storage, 'skinprobe-neg-exists', obj.name, str(npy), 'UV_Detail'))
    short = Path(out) / 'uv_short.npy'; np.save(short, tricky_uv(loops - 3))
    probe.expect_error('loop count mismatch', 'UV_LOOP_COUNT_MISMATCH',
                       lambda: uv_import.apply(probe.bridge, storage, 'skinprobe-neg-count', obj.name, str(short), 'UV_Short'))
    nan = tricky_uv(loops); nan[5, 1] = np.nan
    nan_path = Path(out) / 'uv_nan.npy'; np.save(nan_path, nan)
    probe.expect_error('NaN source', 'UV_SOURCE_NONFINITE',
                       lambda: uv_import.apply(probe.bridge, storage, 'skinprobe-neg-nan', obj.name, str(nan_path), 'UV_Nan'))
    obj_path = Path(out) / 'uv_object.npy'; np.save(obj_path, np.array([{'u': 1}] * loops, dtype=object), allow_pickle=True)
    probe.expect_error('pickled object array refused without unpickling', 'UV_SOURCE_DTYPE',
                       lambda: uv_import.apply(probe.bridge, storage, 'skinprobe-neg-pickle', obj.name, str(obj_path), 'UV_Obj'))
    fortran = Path(out) / 'uv_fortran.npy'; np.save(fortran, np.asfortranarray(tricky_uv(loops)))
    probe.expect_error('fortran-ordered array', 'UV_SOURCE_LAYOUT',
                       lambda: uv_import.apply(probe.bridge, storage, 'skinprobe-neg-fortran', obj.name, str(fortran), 'UV_F'))
    flat = Path(out) / 'uv_flat.npy'; np.save(flat, tricky_uv(loops).reshape(-1))
    probe.expect_error('flat (N*2,) array', 'UV_SOURCE_SHAPE',
                       lambda: uv_import.apply(probe.bridge, storage, 'skinprobe-neg-flat', obj.name, str(flat), 'UV_Flat'))
    probe.expect_error('invalid layer name', 'UV_LAYER_NAME_INVALID',
                       lambda: uv_import.apply(probe.bridge, storage, 'skinprobe-neg-name', obj.name, str(npy), 'bad|name'))
    probe.expect_error('removing a layer the importer did not create', 'UV_LAYER_NOT_IMPORTED',
                       lambda: uv_import.remove_layer(probe.bridge, storage, obj.name, 'UV_Detail'))
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    probe.expect_error('edit mode', 'UV_OBJECT_MODE_REQUIRED',
                       lambda: uv_import.apply(probe.bridge, storage, 'skinprobe-neg-edit', obj.name, str(npy), 'UV_Edit'))
    bpy.ops.object.mode_set(mode='OBJECT')
    probe.check('refusals left the UV maps untouched', [l.name for l in obj.data.uv_layers] == layers_before, layers_before)

    removed = uv_import.remove_layer(probe.bridge, storage, obj.name, 'UV_F64')
    probe.check('imported layer can be removed; others stay', 'UV_F64' not in obj.data.uv_layers
                and 'UV_GlobalSkin' in obj.data.uv_layers and obj.data.uv_layers.active.name == 'UVMap',
                removed['after']['uv_layers'])

    # MASTER is read only: register this saved file as MASTER in a separate store and try again.
    bpy.ops.wm.save_as_mainfile(filepath=str(Path(out) / 'skin_uv_master.blend'))
    master_store = Storage(Path(out) / 'store_master')
    from blender_mcp.production.common.asset_safety import register_master
    register_master(probe.bridge, master_store)
    probe.expect_error('MASTER refuses the import', 'MASTER_ASSET_READ_ONLY',
                       lambda: uv_import.apply(probe.bridge, master_store, 'skinprobe-neg-master', obj.name, str(npy), 'UV_Master'))
    probe.check('fixture file on disk unchanged by the probe run', fixture.is_file())


def performance_suite(probe, out):
    from blender_mcp.production.skin import uv_import
    fresh_scene()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=1001, y_subdivisions=1001, size=2)
    obj = bpy.context.active_object
    obj.name = 'SkinPerf'
    obj.data.uv_layers.new(name='UVMap')
    loops = len(obj.data.loops)
    save_fixture(out, 'skin_perf_fixture')
    storage = Storage(Path(out) / 'store_perf')
    register_open_fixture(probe.bridge, storage, out)
    npy = Path(out) / 'uv_perf.npy'
    np.save(npy, tricky_uv(loops))
    timings = {}
    started = time.perf_counter(); values, info = __import__('blender_mcp.production.skin.uvio', fromlist=['x']).read_uv_array(npy, loops)
    timings['read_npy_s'] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    result = uv_import.apply(probe.bridge, storage, 'skinperf-apply', obj.name, str(npy), 'UV_Perf')
    timings['apply_total_s'] = round(time.perf_counter() - started, 3)
    written = np.empty(loops * 2, dtype=np.float32); obj.data.uv_layers['UV_Perf'].uv.foreach_get('vector', written)
    probe.check('performance mesh imported bit for bit', np.array_equal(written, tricky_uv(loops).reshape(-1)),
                {'loops': loops, 'timings': timings, 'carrier': result['carrier'], 'payload_bytes': probe.bridge.bytes})
    probe.report['metrics'].update(loops=loops, **timings)


def big_skin_material(nodes_per_group=60, groups=3, top_mix=120, images=130):
    """A GlobalSkin-sized graph: nested groups, many mix/math nodes and many image textures."""
    mat = bpy.data.materials.new('BigSkin')
    mat.use_nodes = True
    tree = mat.node_tree
    bsdf = next(n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED')
    inner = None
    for level in range(groups):
        group = bpy.data.node_groups.new('SkinLayer' + str(level), 'ShaderNodeTree')
        group.interface.new_socket('Value', in_out='OUTPUT', socket_type='NodeSocketFloat')
        out = group.nodes.new('NodeGroupOutput')
        previous = None
        for i in range(nodes_per_group):
            math_node = group.nodes.new('ShaderNodeMath')
            math_node.operation = 'MULTIPLY_ADD'
            if previous is not None:
                group.links.new(previous.outputs[0], math_node.inputs[0])
            previous = math_node
        if inner is not None:
            node = group.nodes.new('ShaderNodeGroup'); node.node_tree = inner
            group.links.new(node.outputs[0], previous.inputs[1])
        group.links.new(previous.outputs[0], out.inputs[0])
        inner = group
    top = tree.nodes.new('ShaderNodeGroup'); top.node_tree = inner
    previous = top
    for i in range(top_mix):
        mix = tree.nodes.new('ShaderNodeMix'); mix.data_type = 'RGBA'
        tree.links.new(previous.outputs[0], mix.inputs[0])
        previous = mix
    tree.links.new(previous.outputs[2], bsdf.inputs['Base Color'])
    for i in range(images):
        image = bpy.data.images.new('SkinTex' + format(i, '03d'), 4, 4)
        tex = tree.nodes.new('ShaderNodeTexImage'); tex.image = image
    normal = tree.nodes.new('ShaderNodeNormalMap')
    tree.links.new(normal.outputs[0], bsdf.inputs['Normal'])
    return mat


def independent_counts(tree, seen_depth=0):
    nodes = sockets = links = 0
    for node in tree.nodes:
        nodes += 1; sockets += len(node.inputs) + len(node.outputs)
        if node.type == 'GROUP' and node.node_tree:
            n, s, l = independent_counts(node.node_tree, seen_depth + 1)
            nodes += n; sockets += s; links += l
    links += len(tree.links)
    return nodes, sockets, links


def materials_suite(probe, out):
    from blender_mcp.production.materials import inspection
    from blender_mcp.production.materials.service import dispatch
    fresh_scene()
    bpy.ops.mesh.primitive_uv_sphere_add()
    obj = bpy.context.active_object; obj.name = 'SkinHead'
    mat = big_skin_material()
    obj.data.materials.append(mat)
    save_fixture(out, 'skin_material_fixture')
    storage = Storage(Path(out) / 'store_mat')
    nodes, sockets, links = independent_counts(mat.node_tree)
    probe.report['metrics']['graph'] = {'nodes': nodes, 'sockets': sockets, 'links': links}
    probe.check('fixture graph is larger than the full-mode budget', nodes > 256 or sockets > 4096,
                {'nodes': nodes, 'sockets': sockets, 'links': links})
    probe.expect_error('full mode still refuses it (guard unchanged) and names the paged mode', 'MATERIAL_GRAPH_CAPACITY',
                       lambda: dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, None))
    try:
        dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, None)
    except Exception as exc:
        probe.check('capacity message points to graph_mode=paged', 'graph_mode="paged"' in str(exc), str(exc)[:200])
    started = time.perf_counter()
    first = dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, None, options={'graph_mode': 'paged', 'limit': 100})
    probe.report['metrics']['paged_first_call_s'] = round(time.perf_counter() - started, 3)
    stats = first['stats']
    probe.check('paged stats equal the independent count', (stats['nodes'], stats['sockets'], stats['links']) == (nodes, sockets, links),
                {'stats': stats})
    probe.check('nested group depth measured', stats['max_depth'] == 3 and stats['groups'] == 3, stats)
    covered = []
    offset = 0
    pages = 0
    sizes = []
    while True:
        page = dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, None,
                        options={'graph_mode': 'paged', 'limit': 50, 'offset': offset, 'detail': 'nodes'})
        covered += [n['index'] for n in page['nodes']]
        sizes.append(len(json.dumps(page)))
        pages += 1
        if not page['pagination']['nodes_has_more']:
            break
        offset += 50
    probe.check('pages cover every node exactly once', sorted(covered) == list(range(nodes)),
                {'pages': pages, 'covered': len(covered)})
    probe.report['metrics']['page_response_bytes_max'] = max(sizes)
    probe.check('each page response stays bounded (< 200 KB)', max(sizes) < 200_000, {'max_bytes': max(sizes)})
    link_ids = []
    offset = 0
    while True:
        page = dispatch('material_analyze_nodes', probe.bridge, storage, 'op', obj.name, None,
                        options={'graph_mode': 'paged', 'link_limit': 200, 'link_offset': offset, 'detail': 'links'})
        link_ids += [l['index'] for l in page['links']]
        if not page['pagination']['links_has_more']:
            break
        offset += 200
    probe.check('link pages cover every link exactly once', sorted(link_ids) == list(range(links)), {'links': len(link_ids)})
    key_types = sorted({n['type'] for n in first['key_nodes']})
    probe.check('key nodes always present (output, principled, images, normal map)',
                {'OUTPUT_MATERIAL', 'BSDF_PRINCIPLED', 'TEX_IMAGE', 'NORMAL_MAP'} <= set(key_types), key_types)
    probe.check('all 130 images counted, 130 listed (cap 500)', first['images_total'] == 130 and first['images_listed'] == 130,
                {'total': first['images_total'], 'listed': first['images_listed'], 'detailed': len(first['images'])})
    probe.check('paged mode declares validation not performed', first['analysis_scope'] == 'PAGED_INSPECTION'
                and first['validation'].startswith('NOT_PERFORMED') and first['passed'] is None)
    probe.expect_error('paged mode refused on a validation tool', 'MATERIAL_GRAPH_MODE_UNSUPPORTED',
                       lambda: dispatch('material_validate_graph', probe.bridge, storage, 'op', obj.name, None, options={'graph_mode': 'paged'}))
    probe.expect_error('paged mode refuses apply', 'MATERIAL_PAGED_INSPECTION_ONLY',
                       lambda: dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, None, options={'graph_mode': 'paged'}, apply=True))
    probe.expect_error('bad page size', 'MATERIAL_PAGINATION_INVALID',
                       lambda: dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, None, options={'graph_mode': 'paged', 'limit': 500}))
    # A small graph gives the same counts in both modes.
    small = bpy.data.materials.new('Small'); small.use_nodes = True
    obj.data.materials.append(small); obj.active_material_index = 1
    full = dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, 'Small')
    paged = dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, 'Small', options={'graph_mode': 'paged'})
    probe.check('small graph: paged counts equal full-mode counts',
                full['inspection']['counts']['nodes'] == paged['stats']['nodes']
                and full['inspection']['counts']['links'] == paged['stats']['links'],
                {'full': full['inspection']['counts'], 'paged': paged['stats']})
    probe.check('classification still found the principled BSDF', paged['classification']['category'] != 'CUSTOM'
                or paged['classification']['evidence'], paged['classification'])
    # Huge walk for timing: 2,000+ nodes.
    huge = big_skin_material(nodes_per_group=600, groups=3, top_mix=300, images=10)
    obj.data.materials.append(huge)
    n2, s2, l2 = independent_counts(huge.node_tree)
    started = time.perf_counter()
    big = dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, huge.name, options={'graph_mode': 'paged', 'limit': 50})
    elapsed = round(time.perf_counter() - started, 3)
    probe.report['metrics']['paged_walk'] = {'nodes': n2, 'sockets': s2, 'links': l2, 'seconds': elapsed,
                                             'response_bytes': len(json.dumps(big))}
    probe.check('2k-node graph walked within caps', big['stats']['nodes'] == n2, probe.report['metrics']['paged_walk'])
    probe.check('2k-node graph exceeds both full-mode limits', n2 > 256 and s2 > 4096, {'nodes': n2, 'sockets': s2})
    probe.expect_error('detail=nodes caps the page at 50', 'MATERIAL_PAGINATION_INVALID',
                       lambda: dispatch('material_inspect', probe.bridge, storage, 'op', obj.name, None,
                                        options={'graph_mode': 'paged', 'limit': 100, 'detail': 'nodes'}))


def viewport_background_suite(probe, out):
    from blender_mcp.production.skin import viewport
    fresh_scene()
    storage = Storage(Path(out) / 'store_vp')
    inspected = viewport.inspect(probe.bridge)
    probe.check('background Blender reports gui=false and still lists HDRIs', inspected['gui'] is False
                and len(inspected['studio_lights']) > 0, {'hdris': inspected['studio_lights']})
    probe.expect_error('capture refused without a window', 'VIEWPORT_GUI_REQUIRED',
                       lambda: viewport.capture(probe.bridge, storage, 'vp-bg', {'shading': {'type': 'MATERIAL'}}))
    probe.expect_error('configure refused without a window', 'VIEWPORT_GUI_REQUIRED',
                       lambda: viewport.configure(probe.bridge, storage, 'vp-bg2', {}))


SUITES = {'uv': uv_suite, 'performance': performance_suite, 'materials': materials_suite,
          'viewport_background': viewport_background_suite}


def main():
    args = sys.argv[sys.argv.index('--') + 1:]
    out = Path(args[0]); out.mkdir(parents=True, exist_ok=True)
    for name in args[1].split(','):
        probe = Probe(out, name)
        try:
            SUITES[name](probe, out)
        except Exception as exc:
            probe.report['errors'].append({'error': repr(exc), 'traceback': traceback.format_exc()})
            print('ERROR ' + name + ' ' + repr(exc), flush=True)
            traceback.print_exc()
        probe.report['summary'] = {'passed': sum(c['passed'] for c in probe.report['checks']),
                                   'failed': sum(not c['passed'] for c in probe.report['checks']),
                                   'errors': len(probe.report['errors'])}
        probe.save()
        print('SUMMARY ' + name + ' ' + json.dumps(probe.report['summary']), flush=True)


main()
