"""Exercise the actual registry-only FIXTURE and separately registered MASTER."""
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
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    sys.path.insert(0, str(ROOT / 'src'))
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage, sha256
    from blender_mcp.production.common.asset_safety import register_fixture, register_master, current_asset
    from blender_mcp.production.materials import candidate, graph, integrity, model

    class LocalBridge:
        def run(self, body, params=None):
            code = build_code(body, params)
            namespace = {}
            exec(code.rsplit('print(', 1)[0], namespace)
            return namespace['result']

    bridge = LocalBridge()
    storage = Storage(output / 's')
    report = {'checks': {}, 'errors': [], 'blender': bpy.app.version_string}

    def check(name, passed):
        report['checks'][name] = bool(passed)
        if not passed:
            raise AssertionError(name)

    try:
        obj = bpy.data.objects['Cube']
        material = bpy.data.materials.new('SourceMaterial')
        material.use_nodes = True
        obj.data.materials.clear()
        obj.data.materials.append(material)
        path = output / 'registered-without-stamp.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
        register_fixture(bridge, storage, str(path))
        check('fixture_has_no_scene_stamp', all(not s.get('production_asset') for s in bpy.data.scenes))
        role = current_asset(bridge, storage)
        check('fixture_host_registry_is_mutable', role['role'] == 'FIXTURE' and role['mutable'])
        source_sha = sha256(path)
        source = integrity.status(bridge, obj.name, material.name)
        params = {'object_name': obj.name, 'material_name': material.name, 'options': {},
            'expected_status': source, 'expected_asset': role,
            'candidate_name': 'RegisteredFixtureCandidate', 'generation_id': uuid4().hex, 'version': 1}
        made = candidate.create(bridge, params)
        check('registry_only_fixture_candidate_succeeds', made['source_preserved'])
        candidate_snapshot = integrity.status(bridge, made['candidate_object'], made['candidate_material'])
        result = graph.build(bridge, dict(params, candidate_object=made['candidate_object'],
            candidate_material=made['candidate_material'], expected_candidate=candidate_snapshot,
            definition=model.normalize_definition({'parameters': {'metallic': .63}})))
        check('registry_only_fixture_graph_succeeds', result['source_preserved'])
        check('source_state_and_file_preserved', integrity.status(bridge, obj.name, material.name)['fingerprint'] == source['fingerprint'] and sha256(path) == source_sha)
        # A fresh pathname, not a conversion of the registered fixture to MASTER.
        master_path = output / 'separate-synthetic-master.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(master_path))
        register_master(bridge, storage)
        master = current_asset(bridge, storage)
        check('separate_master_registered', master['role'] == 'MASTER' and not master['mutable'])
        master_sha = sha256(master_path)
        before = (len(bpy.data.objects), len(bpy.data.materials), len(bpy.data.meshes))
        params.update(expected_status=integrity.status(bridge, obj.name, material.name), expected_asset=master,
            candidate_name='ForbiddenMasterCandidate', generation_id=uuid4().hex)
        try:
            candidate.create(bridge, params)
        except ValueError as exc:
            check('master_blocked_before_datablocks', 'MASTER_ASSET_READ_ONLY' in str(exc) and
                before == (len(bpy.data.objects), len(bpy.data.materials), len(bpy.data.meshes)))
            report['master_error'] = str(exc)
        else:
            check('master_blocked_before_datablocks', False)
        check('master_file_unchanged', sha256(master_path) == master_sha)
    except Exception as exc:
        report['errors'].append({'error': str(exc), 'traceback': traceback.format_exc()})
    report['status'] = 'PASS' if report['checks'] and all(report['checks'].values()) and not report['errors'] else 'FAIL'
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('M9_ASSET_GUARD', json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    run(output)
