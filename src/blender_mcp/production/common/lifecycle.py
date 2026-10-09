"""Artifact and candidate lifecycle (M3.1, audit AUD-014 "artifacts grow without limit").

Every reserved workspace folder (checkpoints, qa, operations, working) gets lifecycle metadata computed
from what is on disk and in the operation ledger:

    created_at, operation_id, source, status, retain_reason, eligible_for_cleanup

The status is derived, never trusted from a stored flag, because it changes over time (an uncertain
operation still needs its recovery checkpoint; a working copy registered in the asset registry is in
use). Cleanup is planned by default (``dry_run=True``). With ``dry_run=False`` only folders that are
eligible *now* are removed, only inside this workspace, and every removal is logged.

Candidate Actions inside Blender are reported, never deleted here: removing data-blocks is a Blender
mutation that belongs to a checkpointed transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil

from .ledger import OperationLedger

CATEGORIES = ('checkpoints', 'qa', 'operations', 'working')

CANDIDATES = r'''
import bpy
rows = []
for action in bpy.data.actions:
    marker = '__M3_' if '__M3_' in action.name else '__M2_' if '__M2_' in action.name else None
    if marker is None:
        continue
    users = [o.name for o in bpy.data.objects if o.animation_data and o.animation_data.action == action]
    rows.append({'name': action.name, 'marker': marker.strip('_'), 'source_action': action.name.split(marker)[0],
                 'fake_user': action.use_fake_user, 'users': action.users, 'active_on': users})
result = {'candidates': rows, 'filepath': bpy.data.filepath}
'''


def _created(folder: Path) -> tuple[datetime | None, str | None]:
    reservation = folder / 'reservation.json'
    try:
        data = json.loads(reservation.read_text(encoding='utf-8'))
        stamp = datetime.strptime(data['created_at'], '%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc)
        return stamp, data.get('operation_id')
    except (OSError, ValueError, KeyError):
        return None, None


def _size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())


def _references(storage) -> dict[str, str]:
    """Folder path -> reason it must be retained, from the ledger and the asset registry."""
    keep = {}
    ledger = OperationLedger(storage)
    if ledger.folder.exists():
        for path in ledger.folder.glob('*.json'):
            try:
                record = ledger.get(json.loads(path.read_text(encoding='utf-8'))['key'])
            except (OSError, ValueError, KeyError):
                continue
            if record is None:
                continue
            result = record.get('result') or {}
            checkpoint = record.get('checkpoint') or result.get('checkpoint')
            operation = result.get('operation')
            if record['state'] in ('RUNNING', 'UNCERTAIN'):
                reason = 'recovery point of ' + record['state'] + ' operation ' + record['key']
                for item in (checkpoint, operation):
                    if isinstance(item, str):
                        keep[str(Path(item).parent)] = reason
            elif record['state'] == 'SUCCEEDED' and record['key'].endswith(':apply') and isinstance(operation, str):
                keep[str(Path(operation).parent)] = 'proposal of an applied, not reverted candidate (' + record['key'] + ')'
    registry = storage.root / 'assets' / 'registry.json'
    if registry.exists():
        for asset_id, entry in json.loads(registry.read_text(encoding='utf-8')).get('assets', {}).items():
            for key in ('path', 'source_checkpoint'):
                value = entry.get(key)
                if value:
                    keep[str(Path(value).parent)] = 'referenced by registered asset ' + asset_id + ' (' + entry['role'] + ')'
    return {os.path.normcase(os.path.abspath(k)): v for k, v in keep.items()}


def inventory(storage, *, min_age_days: float = 7.0, keep_last: int = 5, categories=None, now=None) -> list[dict]:
    if isinstance(min_age_days, bool) or not isinstance(min_age_days, (int, float)) or not 0 <= min_age_days <= 3650:
        raise ValueError('min_age_days must be 0..3650')
    if isinstance(keep_last, bool) or not isinstance(keep_last, int) or not 0 <= keep_last <= 1000:
        raise ValueError('keep_last must be an integer 0..1000')
    categories = list(categories or CATEGORIES)
    if any(c not in CATEGORIES for c in categories):
        raise ValueError('categories must be a subset of ' + ', '.join(CATEGORIES))
    now = now or datetime.now(timezone.utc)
    references = _references(storage)
    rows = []
    for category in categories:
        parent = storage.root / category
        if not parent.is_dir():
            continue
        folders = sorted((p for p in parent.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
        for rank, folder in enumerate(folders):
            created, operation_id = _created(folder)
            source = None
            manifest = folder / 'manifest.json'
            if manifest.exists():
                try:
                    source = (json.loads(manifest.read_text(encoding='utf-8')).get('source') or {}).get('source_file')
                except (OSError, ValueError):
                    source = None
            reason = references.get(os.path.normcase(os.path.abspath(folder)))
            if reason is None and created is None:
                reason = 'no readable reservation.json (unknown provenance)'
            if reason is None and rank < keep_last:
                reason = 'among the newest ' + str(keep_last) + ' ' + category
            if reason is None and now - created < timedelta(days=min_age_days):
                reason = 'younger than ' + str(min_age_days) + ' days'
            rows.append({'path': str(folder), 'category': category, 'created_at': created.isoformat() if created else None,
                         'operation_id': operation_id, 'source': source, 'bytes': _size(folder),
                         'status': 'retained' if reason else 'expired', 'retain_reason': reason,
                         'eligible_for_cleanup': reason is None})
    return rows


def cleanup(storage, *, dry_run: bool = True, min_age_days: float = 7.0, keep_last: int = 5,
            categories=None, bridge=None) -> dict:
    if not isinstance(dry_run, bool):
        raise ValueError('dry_run must be boolean')
    rows = inventory(storage, min_age_days=min_age_days, keep_last=keep_last, categories=categories)
    eligible = [r for r in rows if r['eligible_for_cleanup']]
    result = {'dry_run': dry_run, 'workspace': str(storage.root),
              'policy': {'min_age_days': min_age_days, 'keep_last': keep_last, 'categories': list(categories or CATEGORIES)},
              'artifacts': rows[:200], 'artifacts_truncated': len(rows) > 200,
              'totals': {'folders': len(rows), 'bytes': sum(r['bytes'] for r in rows), 'eligible_folders': len(eligible),
                         'eligible_bytes': sum(r['bytes'] for r in eligible)},
              'removed': [], 'warnings': []}
    if bridge is not None:
        try:
            result['blender_candidates'] = bridge.run(CANDIDATES, {})
            result['warnings'].append('Candidate Actions are reported only; deleting them is a Blender mutation outside cleanup.')
        except Exception as exc:  # the file inventory stays useful without Blender
            result['warnings'].append('Candidate inventory unavailable: ' + type(exc).__name__ + ': ' + str(exc)[:200])
    if dry_run:
        result['warnings'].append('DRY RUN: nothing was deleted. Re-run with dry_run=false to remove only the eligible folders listed.')
        return result
    root = os.path.normcase(os.path.abspath(storage.root))
    log = storage.root / 'cleanup.jsonl'
    for row in eligible:
        folder = Path(row['path'])
        resolved = os.path.normcase(os.path.abspath(folder))
        if os.path.commonpath([resolved, root]) != root or folder.is_symlink() or folder.parent.name not in CATEGORIES:
            result['warnings'].append('Skipped (outside the workspace or not a reserved folder): ' + row['path'])
            continue
        shutil.rmtree(folder)
        result['removed'].append({'path': row['path'], 'bytes': row['bytes']})
        with log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'removed_at': datetime.now(timezone.utc).isoformat(), **row}, ensure_ascii=False) + '\n')
    return result
