"""Phase 15: lifecycle metadata and cleanup that never removes evidence still needed."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from blender_mcp.production.common.asset_safety import AssetRegistry
from blender_mcp.production.common.ledger import OperationLedger
from blender_mcp.production.common.lifecycle import cleanup, inventory
from blender_mcp.production.common.storage import Storage


def aged(storage, category, days, operation_id='op', files=None):
    folder = storage.reserve(category, operation_id)
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%dT%H%M%S.%fZ')
    (folder / 'reservation.json').write_text(json.dumps({'operation_id': operation_id, 'created_at': stamp}), encoding='utf-8')
    for name, content in (files or {'data.bin': b'x' * 10}).items():
        (folder / name).write_bytes(content)
    return folder


def by_path(rows):
    return {row['path']: row for row in rows}


def test_metadata_fields_and_default_policy(tmp_path):
    storage = Storage(tmp_path)
    old = [aged(storage, 'checkpoints', 30, 'old-%d' % i) for i in range(7)]
    young = aged(storage, 'qa', 1, 'young')
    rows = by_path(inventory(storage))
    row = rows[str(young)]
    assert set(row) >= {'created_at', 'operation_id', 'source', 'status', 'retain_reason', 'eligible_for_cleanup'}
    assert row['retain_reason'].startswith('among the newest') or row['retain_reason'].startswith('younger')
    eligible = [r for r in rows.values() if r['eligible_for_cleanup']]
    # Reservation folder names sort by creation stamp: the 5 newest checkpoints are kept.
    assert len(eligible) == 2 and all(r['category'] == 'checkpoints' for r in eligible)


def test_dry_run_deletes_nothing(tmp_path):
    storage = Storage(tmp_path)
    folders = [aged(storage, 'qa', 60, 'q%d' % i) for i in range(3)]
    result = cleanup(storage, keep_last=0)
    assert result['dry_run'] is True and result['removed'] == []
    assert result['totals']['eligible_folders'] == 3 and all(f.exists() for f in folders)


def test_uncertain_operation_keeps_its_recovery_checkpoint(tmp_path):
    storage = Storage(tmp_path)
    recovery = aged(storage, 'checkpoints', 90, 'uncertain', {'manifest.json': b'{}'})
    ledger = OperationLedger(storage)
    ledger.begin('cycle:apply', 'test', {})
    ledger.finish('cycle:apply', 'UNCERTAIN', result={'checkpoint': str(recovery / 'manifest.json'), 'status': 'error'})
    row = by_path(inventory(storage, keep_last=0))[str(recovery)]
    assert row['eligible_for_cleanup'] is False and 'UNCERTAIN' in row['retain_reason']


def test_applied_proposal_and_registered_working_copy_are_retained(tmp_path):
    storage = Storage(tmp_path)
    proposal = aged(storage, 'operations', 90, 'p', {'proposal.json': b'{}'})
    ledger = OperationLedger(storage)
    ledger.begin('cid:apply', 'test', {})
    ledger.finish('cid:apply', 'SUCCEEDED', result={'operation': str(proposal / 'proposal.json')})
    working = aged(storage, 'working', 90, 'w', {'hero.blend': b'BLENDER'})
    AssetRegistry(storage).register('WORKING_COPY', str(working / 'hero.blend'))
    rows = by_path(inventory(storage, keep_last=0))
    assert not rows[str(proposal)]['eligible_for_cleanup'] and 'applied' in rows[str(proposal)]['retain_reason']
    assert not rows[str(working)]['eligible_for_cleanup'] and 'registered asset' in rows[str(working)]['retain_reason']


def test_explicit_cleanup_removes_only_eligible_and_logs(tmp_path):
    storage = Storage(tmp_path)
    expired = aged(storage, 'qa', 60, 'expired')
    kept = aged(storage, 'qa', 1, 'recent')
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    result = cleanup(storage, dry_run=False, keep_last=0)
    assert [r['path'] for r in result['removed']] == [str(expired)]
    assert not expired.exists() and kept.exists() and outside.exists()
    log = [json.loads(line) for line in (storage.root / 'cleanup.jsonl').read_text(encoding='utf-8').splitlines()]
    assert log[0]['path'] == str(expired) and log[0]['operation_id'] == 'expired'


@pytest.mark.parametrize('kwargs', [{'min_age_days': -1}, {'keep_last': -1}, {'keep_last': True},
                                    {'categories': ['assets']}, {'dry_run': 'false'}])
def test_invalid_policy_is_refused(tmp_path, kwargs):
    with pytest.raises(ValueError):
        cleanup(Storage(tmp_path), **kwargs)
