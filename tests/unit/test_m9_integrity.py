"""M9 integrity must observe content, independent of paths and public summaries."""
import ast
import hashlib
import json
from types import SimpleNamespace

import pytest

from blender_mcp.production.materials import integrity
from blender_mcp.production.common.bridge import build_code


def functions(*names):
    tree = ast.parse(integrity.RUNTIME)
    code = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
    namespace = {'json': json}
    exec(compile(code, '<M9 bounded runtime functions>', 'exec'), namespace)
    return namespace


def test_same_size_image_bytes_change_fingerprint(tmp_path):
    image = tmp_path / 'fixture.bin'
    image.write_bytes(b'abcd')
    first = integrity.file_dependencies([{'path': str(image), 'kind': 'IMAGE'}])
    image.write_bytes(b'abce')
    second = integrity.file_dependencies([{'path': str(image), 'kind': 'IMAGE'}])
    assert first[0]['bytes'] == second[0]['bytes'] == 4
    assert first[0]['sha256'] != second[0]['sha256']
    assert image.read_bytes() == b'abce'


def test_missing_image_is_explicit_not_empty_success(tmp_path):
    result = integrity.file_dependencies([{'path': str(tmp_path / 'missing.png'), 'kind': 'IMAGE'}])
    assert result[0]['exists'] is False
    assert result[0]['bytes'] is None
    assert result[0]['sha256'] is None


def test_external_file_budget_before_read(tmp_path):
    path = tmp_path / 'large.bin'
    with path.open('wb') as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match='MATERIAL_CAPACITY'):
        integrity.file_dependencies([{'path': str(path)}])


def test_remote_dependency_not_opened():
    with pytest.raises(ValueError, match='MATERIAL_REMOTE_DEPENDENCY_UNSUPPORTED'):
        integrity.file_dependencies([{'path': r'\\remote-host\share\image.png'}])


def test_host_snapshot_signs_additional_image_files(tmp_path):
    path = tmp_path / 'new_channel.png'
    path.write_bytes(b'small synthetic image fixture')
    class Bridge:
        def run(self, body, params):
            assert params['options'] == {'image_names': ['NewImage']}
            assert body == integrity.STATUS
            return {'runtime_fingerprint': 'all cached scene bytes', 'dependencies': {'files': [{'path': str(path)}]}}
    first = integrity.status(Bridge(), 'Object', 'Material', {'image_names': ['NewImage']})
    path.write_bytes(b'changed fixture bytes')
    second = integrity.status(Bridge(), 'Object', 'Material', {'image_names': ['NewImage']})
    assert first['fingerprint'] != second['fingerprint']
    assert second['reload_status'] == 'UNKNOWN_NOT_RELOADED'
    assert second['dependencies']['files'][0]['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('role', ['MASTER', 'UNREGISTERED', 'UNKNOWN'])
def test_atomic_guard_rejects_nonmutable_saved_asset(role):
    ns = functions('m9_fail', 'm9_mutation_guard')
    current = {'runtime_fingerprint': 'same'}
    ns['bpy'] = SimpleNamespace(context=SimpleNamespace(mode='OBJECT'),
        data=SimpleNamespace(filepath='fixture.blend', scenes=[{'production_asset': json.dumps({'role': role})}]))
    with pytest.raises(ValueError, match='MASTER_ASSET_READ_ONLY|MATERIAL_ASSET_UNREGISTERED'):
        ns['m9_mutation_guard']({'expected_status': current}, current)


def test_atomic_guard_rejects_stale_before_any_bpy_access():
    ns = functions('m9_fail', 'm9_mutation_guard')
    with pytest.raises(ValueError, match='MATERIAL_STALE_SOURCE'):
        ns['m9_mutation_guard']({'expected_status': {'runtime_fingerprint': 'old'}}, {'runtime_fingerprint': 'new'})


def test_status_payload_preserves_upstream_safe_mode():
    code = build_code(integrity.STATUS, {'object_name': 'Fixture', 'material_name': 'Source', 'options': {'image_names': []}})
    assert len(code.encode()) < 200000


def test_derived_socket_ui_update_is_not_an_authored_change():
    ns = functions('m9_socket')
    ns['bpy'] = SimpleNamespace(types=SimpleNamespace(ID=type('FakeID', (), {})))
    ns['m9_value'] = lambda value: value
    socket = SimpleNamespace(identifier='Subsurface IOR', name='Subsurface IOR', type='VALUE',
        default_value=1.4, enabled=True, hide_value=False)
    initial = ns['m9_socket'](socket, {})
    socket.enabled = False  # Blender material.copy() can defer this derived UI update.
    assert ns['m9_socket'](socket, {}) == initial
    socket.default_value = 1.5
    assert ns['m9_socket'](socket, {}) != initial


def atomic_asset_namespace(stamps=()):
    ns = functions('m9_fail', 'm9_mutation_guard')
    ns['bpy'] = SimpleNamespace(context=SimpleNamespace(mode='OBJECT'),
        path=SimpleNamespace(abspath=lambda value: value),
        data=SimpleNamespace(filepath='C:/Fixtures/current.blend',
            scenes=[{'production_asset': json.dumps(s)} for s in stamps]))
    current = {'runtime_fingerprint': 'same'}
    params = {'expected_status': current,
        'expected_asset': {'role': 'FIXTURE', 'asset_id': 'registered-fixture', 'path': 'c:\\fixtures\\current.blend'}}
    return ns, current, params


def test_host_registered_fixture_needs_no_scene_stamp():
    ns, current, params = atomic_asset_namespace()
    assert ns['m9_mutation_guard'](params, current) == current


def test_master_registry_result_blocks_without_scene_stamp():
    ns, current, params = atomic_asset_namespace()
    params['expected_asset']['role'] = 'MASTER'
    with pytest.raises(ValueError, match='MASTER_ASSET_READ_ONLY'):
        ns['m9_mutation_guard'](params, current)


def test_expected_asset_must_match_current_file():
    ns, current, params = atomic_asset_namespace()
    params['expected_asset']['path'] = 'C:/Fixtures/other.blend'
    with pytest.raises(ValueError, match='MATERIAL_ASSET_CHANGED'):
        ns['m9_mutation_guard'](params, current)


def test_working_copy_requires_matching_scene_stamp():
    ns, current, params = atomic_asset_namespace()
    params['expected_asset']['role'] = 'WORKING_COPY'
    with pytest.raises(ValueError, match='MATERIAL_ASSET_STAMP_MISMATCH'):
        ns['m9_mutation_guard'](params, current)
    ns['bpy'].data.scenes = [{'production_asset': json.dumps({'asset_id': 'registered-fixture', 'role': 'WORKING_COPY'})}]
    assert ns['m9_mutation_guard'](params, current) == current


def test_foreign_stamp_blocks_registered_fixture():
    ns, current, params = atomic_asset_namespace([{'asset_id': 'foreign-asset', 'role': 'WORKING_COPY'}])
    with pytest.raises(ValueError, match='MATERIAL_ASSET_STAMP_MISMATCH'):
        ns['m9_mutation_guard'](params, current)
