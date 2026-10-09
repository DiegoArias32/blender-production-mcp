"""Real M5 bake-readiness rejection must survive the M9 neutral handoff.

Run only with a fresh factory-startup Blender and a new synthetic output folder.
Uses the actual host services with a local, safe-mode-validated bpy adapter; no socket.
"""
import argparse
import json
from pathlib import Path
import sys
import traceback
import types
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def run(output):
    import bpy
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(ROOT / 'tests/blender'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage, sha256
    from blender_mcp.production.common import m5
    from blender_mcp.production.bake import service as bake
    from blender_mcp.production.materials import handoff, model
    from m31_fixture_support import register_open_fixture

    class LocalBridge:
        def __init__(self):
            self.payload_sizes = []

        def run(self, body, params=None):
            code = build_code(body, params)
            self.payload_sizes.append(len(code.encode()))
            namespace = {}
            exec(code.rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    storage = Storage(output / 's')
    report = {'scope': 'M5→M9 real bpy service handoff; synthetic fixture, no socket/render',
              'blender': bpy.app.version_string, 'checks': {}, 'errors': []}

    def check(name, passed, **details):
        report['checks'][name] = {'passed': bool(passed), **details}
        if not passed:
            raise AssertionError(name)

    try:
        # The startup Cube is owned by this factory process, not a user project.
        high = bpy.data.objects['Cube']
        high.name = 'M9_Handoff_HIGH'
        low = high.copy()
        low.data = high.data.copy()
        low.name = 'M9_Handoff_LOW'
        bpy.context.scene.collection.objects.link(low)
        material = bpy.data.materials.new('M9_HandoffPlain')
        material.use_nodes = True
        low.data.materials.clear()
        low.data.materials.append(material)
        for vertex in high.data.vertices:
            vertex.co *= 1.02
        high.data.update()
        # A measured severe anisotropy, injected before freezing the relationship.
        for point in low.data.uv_layers.active.data:
            point.uv.y *= .001
        low.data.update()
        fixture = output / 'm9-handoff-fixture.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(fixture))
        register_open_fixture(bridge, storage, output)
        fixture_sha = sha256(fixture)
        relation = m5.create_relationship(bridge, storage, uuid4().hex, high.name,
            m5.signature(bridge, high.name)['fingerprint'], {'target_object': low.name})
        low_name = relation['candidate']
        source_before = {name: m5.signature(bridge, name)['fingerprint'] for name in (high.name, low.name, low_name)}
        options = {'resolution': 256, 'padding_px': 4, 'check_padding': False,
                   'front_distance_m': .09, 'rear_distance_m': .09,
                   'max_stretch_ratio': 8.0}
        pair = bake.prepare_pairs(bridge, storage, uuid4().hex, relation['relationship'], options)
        report['bake_pair'] = pair
        check('m5_real_distortion_not_ready', pair['readiness'] == 'NOT_READY' and
              'UV_STRETCH_EXCESSIVE' in pair['report']['errors'], errors=pair['report']['errors'],
              uv_metrics=pair['report'].get('uv', {}).get('metrics'))
        definition = model.normalize_definition({'category': 'GENERIC_DIELECTRIC'})
        result = handoff.create_handoff(bridge, storage, uuid4().hex, low_name, material.name,
            definition, {'bake_pair': pair['pair'], 'asset_version': 'synthetic-m9-handoff-v1'}, apply=True)
        report['handoff_result'] = result
        manifest = json.loads(Path(result['handoff']).read_text(encoding='utf-8'))
        check('m9_preserves_not_ready', result['readiness'] == manifest['bake_readiness'] == 'NOT_READY')
        check('m9_preserves_real_uv_error', 'UV_STRETCH_EXCESSIVE' in manifest['bake_report']['errors'])
        check('m9_preserves_pair_report_exact', manifest['bake_report'] == pair['report'])
        check('neutral_contract_no_bake_generated', manifest['application'] == 'neutral' and
              manifest['bake_maps_generated'] == [] and manifest['exports'] == [])
        check('handoff_hash_verified', sha256(Path(result['handoff'])) == result['sha256'])
        check('source_signatures_unchanged', all(m5.signature(bridge, name)['fingerprint'] == before
              for name, before in source_before.items()))
        check('source_blend_not_overwritten', sha256(fixture) == fixture_sha)
        # A subsequent changed UV must invalidate the frozen M5 pair, not relabel it.
        candidate = bpy.data.objects[low_name]
        point = candidate.data.uv_layers.active.data[0]
        previous = point.uv.copy()
        point.uv.x += .0123
        try:
            handoff.create_handoff(bridge, storage, uuid4().hex, low_name, material.name,
                definition, {'bake_pair': pair['pair']}, apply=False)
        except ValueError as exc:
            check('stale_m5_pair_rejected', 'STALE_BAKE_PAIR' in str(exc), error=str(exc))
        else:
            check('stale_m5_pair_rejected', False)
        finally:
            point.uv = previous
        # Crossing three tiles is ambiguous: fail before reserving a handoff or
        # checkpoint, instead of inventing the two intermediate tile memberships.
        uv = candidate.data.uv_layers.active.data
        loops = list(candidate.data.polygons[0].loop_indices)
        saved = [uv[index].uv.copy() for index in loops]
        before_files = {str(path.relative_to(storage.root)): sha256(path)
                        for path in storage.root.rglob('*') if path.is_file()}
        before_directories = {str(path.relative_to(storage.root))
                              for path in storage.root.rglob('*') if path.is_dir()}
        try:
            for index, value in zip(loops, ((.1, .1), (2.8, .1), (2.8, .8), (.1, .8))):
                uv[index].uv = value
            try:
                handoff.create_handoff(bridge, storage, uuid4().hex, low_name, material.name,
                    definition, {}, apply=True)
            except ValueError as exc:
                check('three_tile_face_rejected', 'MATERIAL_HANDOFF_UV_AMBIGUOUS' in str(exc),
                      error=str(exc))
            else:
                check('three_tile_face_rejected', False)
            after_files = {str(path.relative_to(storage.root)): sha256(path)
                           for path in storage.root.rglob('*') if path.is_file()}
            after_directories = {str(path.relative_to(storage.root))
                                 for path in storage.root.rglob('*') if path.is_dir()}
            check('three_tile_rejection_before_checkpoint_or_file',
                  before_files == after_files and before_directories == after_directories,
                  unchanged_file_count=len(before_files))
        finally:
            for index, value in zip(loops, saved):
                uv[index].uv = value
        check('source_signatures_restored_after_negative_cases',
              all(m5.signature(bridge, name)['fingerprint'] == before
                  for name, before in source_before.items()) and sha256(fixture) == fixture_sha)
        report['fixture'] = str(fixture)
    except Exception as exc:
        report['errors'].append({'error': str(exc), 'traceback': traceback.format_exc()})
    report['max_payload_bytes'] = max(bridge.payload_sizes, default=0)
    report['status'] = 'PASS' if report['checks'] and all(x['passed'] for x in report['checks'].values()) and not report['errors'] else 'FAIL'
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('M9_HANDOFF', json.dumps({'status': report['status'], 'checks': len(report['checks']), 'errors': report['errors']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    run(output)
