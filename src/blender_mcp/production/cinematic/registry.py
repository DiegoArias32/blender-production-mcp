"""Host-side actor registry storage. Registration never touches Blender, so it is allowed on any asset role.

One immutable JSON per actor under <workdir>/cinematic-actors/<asset_key>/actor-<actor_id>.json. Identity is the
actor fingerprint plus object/data names (never the object name alone); re-registering the same object with the same
fingerprint returns the existing actor_id instead of creating a duplicate.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ..common.storage import write_json

_ID = re.compile(r'^[0-9a-f]{16}$')


def asset_key(asset: dict, scene_name: str) -> str:
    if asset.get('asset_id'):
        return re.sub(r'[^A-Za-z0-9_-]', '_', str(asset['asset_id']))[:64]
    return 'TEMPORARY-' + hashlib.sha256(('temporary:' + scene_name).encode('utf-8')).hexdigest()[:16]


def folder(storage, key: str) -> Path:
    path = storage.root / 'cinematic-actors' / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def records(storage, key: str) -> list[dict]:
    rows = []
    for path in sorted(folder(storage, key).glob('actor-*.json')):
        rows.append(json.loads(path.read_text(encoding='utf-8')))
    return rows


def find(storage, key: str, actor_id: str) -> dict:
    if not isinstance(actor_id, str) or not _ID.match(actor_id):
        raise ValueError('ACTOR_ID_INVALID: 16 lowercase hex characters')
    path = folder(storage, key) / ('actor-' + actor_id + '.json')
    if not path.is_file():
        raise ValueError('ACTOR_MISSING: ' + actor_id + ' is not registered for this asset')
    return json.loads(path.read_text(encoding='utf-8'))


def match(storage, key: str, object_name: str, data_name, fingerprint: str):
    """Existing record for the same object, data and fingerprint (idempotent registration), else None."""
    for row in records(storage, key):
        if row['object'] == object_name and row.get('data') == data_name and row['fingerprint'] == fingerprint:
            return row
    return None


def write(storage, key: str, record: dict) -> dict:
    actor_id = hashlib.sha256((key + '|' + record['object'] + '|' + str(record.get('data')) + '|'
                               + record['fingerprint']).encode('utf-8')).hexdigest()[:16]
    record = dict(record, actor_id=actor_id, asset_key=key)
    path = folder(storage, key) / ('actor-' + actor_id + '.json')
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    write_json(path, record)
    return record
