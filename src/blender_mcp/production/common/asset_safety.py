"""Technical master-asset guard (M3.1, audit AUD-014).

"Never modify a master" used to be a procedure. It is now enforced: persistent mutation is accepted
only when the open .blend has an explicitly registered mutable role.

Roles
-----
MASTER        registered read-only; any persistent mutation is refused.
WORKING_COPY  created by ``create_working_copy`` / ``restore_checkpoint`` under ``<workdir>/working``,
              stamped with its asset_id and registered with lineage.
FIXTURE       a generated test file under an allowed fixture root (system temp directory, the
              workdir's ``fixtures`` folder or ``BLENDER_PRODUCTION_FIXTURE_ROOTS``).
TEMPORARY     an unsaved Blender session (no file on disk can be overwritten by our changes).

Anything else (including every real user file that was never registered) is UNREGISTERED and
refused. Roles are never inferred from file names.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

ROLES = ('MASTER', 'WORKING_COPY', 'FIXTURE', 'TEMPORARY')
MUTABLE_ROLES = frozenset({'WORKING_COPY', 'FIXTURE', 'TEMPORARY'})
STAMP_PROPERTY = 'production_asset'

CURRENT_FILE = r'''
import bpy
stamps = []
for scene in bpy.data.scenes:
    value = scene.get('production_asset')
    if isinstance(value, str):
        stamps.append(value)
result = {'filepath': bpy.data.filepath, 'is_dirty': bpy.data.is_dirty, 'stamps': stamps}
'''

STAMP_AND_SAVE_COPY = r'''
import bpy
original = bpy.data.filepath
outcome = bpy.ops.wm.save_as_mainfile(filepath=params['path'], copy=True, relative_remap=True)
if 'FINISHED' not in outcome or bpy.data.filepath != original:
    raise RuntimeError('WORKING_COPY_FAILED: Blender did not save the working copy')
outcome = bpy.ops.wm.open_mainfile(filepath=params['path'], load_ui=False, use_scripts=False)
if 'FINISHED' not in outcome or bpy.data.filepath != params['path']:
    raise RuntimeError('WORKING_COPY_FAILED: Blender did not open the working copy')
for scene in bpy.data.scenes:
    scene['production_asset'] = params['stamp']
outcome = bpy.ops.wm.save_mainfile()
if 'FINISHED' not in outcome:
    raise RuntimeError('WORKING_COPY_FAILED: Blender did not save the stamped working copy')
result = {'source_path': original, 'working_path': bpy.data.filepath, 'scripts_enabled': False}
'''


def normalize(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path))) if path else ''


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssetRegistry:
    """JSON registry inside the workdir; replaced atomically (temp file + os.replace)."""

    def __init__(self, storage):
        self.root = storage.root
        self.path = storage.root / 'assets' / 'registry.json'

    def load(self) -> dict:
        if not self.path.exists():
            return {'schema': 1, 'assets': {}}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if data.get('schema') != 1 or not isinstance(data.get('assets'), dict):
            raise ValueError('ASSET_REGISTRY_INVALID: unexpected registry format at ' + str(self.path))
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp-' + uuid4().hex[:8])
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, self.path)

    def find_path(self, path: str) -> tuple[str, dict] | tuple[None, None]:
        wanted = normalize(path)
        for asset_id, entry in self.load()['assets'].items():
            if entry.get('path') == wanted:
                return asset_id, entry
        return None, None

    def register(self, role: str, path: str, **metadata) -> dict:
        if role not in ROLES:
            raise ValueError('Unknown asset role: ' + str(role))
        data = self.load()
        wanted = normalize(path)
        for asset_id, entry in data['assets'].items():
            if entry.get('path') == wanted:
                if entry['role'] != role:
                    raise ValueError('ASSET_ROLE_CONFLICT: ' + wanted + ' is already registered as ' + entry['role'])
                return {'asset_id': asset_id, **entry}
        asset_id = uuid4().hex
        entry = {'role': role, 'path': wanted, 'registered_at': _now(), **metadata}
        data['assets'][asset_id] = entry
        self.save(data)
        return {'asset_id': asset_id, **entry}


def fixture_roots(storage) -> list[str]:
    roots = [tempfile.gettempdir(), str(storage.root / 'fixtures')]
    roots += [item for item in os.environ.get('BLENDER_PRODUCTION_FIXTURE_ROOTS', '').split(os.pathsep) if item]
    return [normalize(root) for root in roots]


def _inside(path: str, roots: list[str]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def current_asset(bridge, storage) -> dict:
    info = bridge.run(CURRENT_FILE, {})
    if 'filepath' not in info:
        return {'role': 'UNKNOWN', 'mutable': False, 'path': None, 'reason': 'Blender did not report the open file'}
    path = normalize(info['filepath'])
    if not path:
        return {'role': 'TEMPORARY', 'mutable': True, 'path': None, 'asset_id': None,
                'reason': 'unsaved session: no file on disk can be overwritten by this operation'}
    asset_id, entry = AssetRegistry(storage).find_path(path)
    if entry is None:
        return {'role': 'UNREGISTERED', 'mutable': False, 'path': path, 'asset_id': None,
                'reason': 'file is not registered; create a working copy (asset_create_working_copy) before mutating'}
    stamps = [json.loads(s) for s in info.get('stamps', []) if s.startswith('{')]
    foreign = [s for s in stamps if s.get('asset_id') and s.get('asset_id') != asset_id]
    if foreign:
        return {'role': 'STAMP_MISMATCH', 'mutable': False, 'path': path, 'asset_id': asset_id,
                'reason': 'the file carries the stamp of another registered asset (copied or moved working copy)'}
    if entry['role'] == 'WORKING_COPY' and not any(s.get('asset_id') == asset_id for s in stamps):
        return {'role': 'STAMP_MISMATCH', 'mutable': False, 'path': path, 'asset_id': asset_id,
                'reason': 'registered working copy lost its asset stamp'}
    return {'role': entry['role'], 'mutable': entry['role'] in MUTABLE_ROLES, 'path': path, 'asset_id': asset_id,
            'reason': 'registered ' + entry['role'], 'lineage': entry.get('source_path')}


def require_mutable_asset(bridge, storage, capability: str) -> dict:
    asset = current_asset(bridge, storage)
    if asset['mutable']:
        return asset
    code = {'MASTER': 'MASTER_ASSET_READ_ONLY', 'UNREGISTERED': 'ASSET_ROLE_UNREGISTERED',
            'STAMP_MISMATCH': 'ASSET_STAMP_MISMATCH'}.get(asset['role'], 'ASSET_STATE_UNKNOWN')
    raise PermissionError(code + ': ' + capability + ' refused on ' + str(asset.get('path')) + ' (' + asset['reason'] + ')')


def register_master(bridge, storage) -> dict:
    from .storage import sha256
    info = bridge.run(CURRENT_FILE, {})
    if not info.get('filepath'):
        raise ValueError('An unsaved session cannot be registered as MASTER')
    path = Path(info['filepath'])
    return AssetRegistry(storage).register('MASTER', str(path), sha256=sha256(path), bytes=path.stat().st_size,
                                           unsaved_changes_at_registration=bool(info.get('is_dirty')))


def register_fixture(bridge, storage, path: str, allowed_roots: list[str] | None = None) -> dict:
    """Register the currently open generated test file as FIXTURE (only under a fixture root)."""
    info = bridge.run(CURRENT_FILE, {})
    wanted = normalize(path)
    if normalize(info.get('filepath', '')) != wanted:
        raise ValueError('FIXTURE_NOT_OPEN: register the fixture that is currently open in Blender')
    roots = fixture_roots(storage) + [normalize(root) for root in (allowed_roots or [])]
    if not _inside(wanted, roots):
        raise PermissionError('FIXTURE_OUTSIDE_ALLOWED_ROOTS: ' + wanted + ' is not under ' + ', '.join(roots))
    return AssetRegistry(storage).register('FIXTURE', wanted)


def create_working_copy(bridge, storage, operation_id: str) -> dict:
    from .storage import sha256
    info = bridge.run(CURRENT_FILE, {})
    if not info.get('filepath'):
        raise ValueError('Save the session first: an unsaved session is already TEMPORARY and mutable')
    source = current_asset(bridge, storage)
    folder = storage.reserve('working', operation_id)
    target = folder / Path(info['filepath']).name
    asset_id = uuid4().hex
    stamp = json.dumps({'asset_id': asset_id, 'role': 'WORKING_COPY'})
    result = bridge.run(STAMP_AND_SAVE_COPY, {'path': str(target), 'stamp': stamp})
    data = AssetRegistry(storage).load()
    entry = {'role': 'WORKING_COPY', 'path': normalize(target), 'registered_at': _now(),
             'source_path': source.get('path'), 'source_role': source.get('role'), 'operation_id': operation_id,
             'sha256_at_creation': sha256(target)}
    data['assets'][asset_id] = entry
    AssetRegistry(storage).save(data)
    return {'asset_id': asset_id, **entry, 'scripts_enabled': result.get('scripts_enabled'),
            'warnings': ['The working copy was opened with Python scripts disabled; rigs relying on scripted drivers need review.']}


def register_restored_working_copy(storage, path: str, asset_id: str, checkpoint: str, operation_id: str) -> dict:
    from .storage import sha256
    data = AssetRegistry(storage).load()
    entry = {'role': 'WORKING_COPY', 'path': normalize(path), 'registered_at': _now(), 'source_checkpoint': checkpoint,
             'source_path': None, 'operation_id': operation_id, 'sha256_at_creation': sha256(Path(path))}
    data['assets'][asset_id] = entry
    AssetRegistry(storage).save(data)
    return {'asset_id': asset_id, **entry}
