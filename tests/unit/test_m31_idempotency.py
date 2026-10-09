"""AUD-010: a retry after a timeout never executes a mutation twice.

The simulated Blender keeps just enough state (Actions, active Action) to count real effects. It can
lose the answer of a command it did execute (the client timed out while Blender kept working) or
lose the request itself (nothing executed).
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from blender_mcp.production import checkpoint as checkpoints
from blender_mcp.production.animation import corrections
from blender_mcp.production.common import m3_transaction
from blender_mcp.production.common.asset_safety import CURRENT_FILE
from blender_mcp.production.common.ledger import RECONCILE, OperationLedger, deterministic_suffix
from blender_mcp.production.common.operations import fingerprint, save_proposal
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.common.transport import BlenderTimeoutError


class SimulatedBlender:
    def __init__(self, source='Walk'):
        self.source = source
        self.actions = [source]
        self.active = source
        self.calls = []
        self.lose_next_answer = False      # Blender executes; the answer never reaches the client
        self.lose_next_request = False     # the request never reaches Blender
        self.verification_passes = True
        self.lock = threading.RLock()

    def classify(self, body):
        for name, template in (('reconcile', RECONCILE), ('execute', m3_transaction.EXECUTE), ('execute', corrections.EXECUTE),
                               ('check', m3_transaction.CHECK), ('check', corrections.CHECK),
                               ('preflight', m3_transaction.PREFLIGHT)):
            if body.endswith(template):
                return name
        if body == CURRENT_FILE:
            return 'current_file'
        if "result={'matches':" in body:
            return 'preflight'
        raise AssertionError('unexpected Blender call')

    def candidates(self):
        return [name for name in self.actions if name != self.source]

    def run(self, body, params=None):
        kind = self.classify(body)
        self.calls.append(kind)
        if kind == 'current_file':
            return {'filepath': '', 'is_dirty': False, 'stamps': []}
        if kind == 'preflight':
            return {'matches': self.active == self.source}
        if kind == 'reconcile':
            prefix = params['candidate_name']
            found = sorted(a for a in self.actions if prefix and (a == prefix or a.startswith(prefix + '.')))
            return {'object_found': True, 'candidates': found, 'active_action': self.active,
                    'working_fingerprint': 'fp-' + found[0] if len(found) == 1 else None}
        if kind == 'check':
            if params['mode'] == 'revert':
                self.active = params['source_action']
                return {'reverted': True, 'status': 'warning', 'metrics': {}, 'warnings': [],
                        'candidate_preserved': params['working_action']}
            return {'numeric_verification': 'pass', 'status': 'warning', 'metrics': {'measured': 1},
                    'verification': {'passed': True}, 'warnings': [], 'errors': []}
        # execute
        if self.lose_next_request:
            self.lose_next_request = False
            raise BlenderTimeoutError('TRANSPORT_TIMEOUT: simulated, request lost')
        marker = '__M3_' if body.endswith(m3_transaction.EXECUTE) else '__M2_'
        name = params['source_action'] + marker + params['suffix']
        self.actions.append(name)
        passed = self.verification_passes
        if params['mode'] == 'apply' and passed:
            self.active = name
        answer = {'working_action': name, 'source_action': params['source_action'], 'working_snapshot': {'name': name},
                  'source_intact': True, 'numeric_verification': 'pass' if passed else 'fail', 'visual_verification': 'required',
                  'status': 'warning' if passed else 'error', 'metrics': {}, 'warnings': [],
                  'errors': [] if passed else ['verification failed'], 'verification': {'passed': passed}}
        if self.lose_next_answer:
            self.lose_next_answer = False
            raise BlenderTimeoutError('TRANSPORT_TIMEOUT: simulated, Blender kept working')
        return answer


@pytest.fixture
def m3(tmp_path, monkeypatch):
    store = Storage(tmp_path / 'store')
    saved_checkpoints = []

    def fake_checkpoint(bridge, storage, description, operation_id, operation=None):
        saved_checkpoints.append(description)
        return {'manifest': 'checkpoint-' + str(len(saved_checkpoints))}
    monkeypatch.setattr(m3_transaction, 'create_checkpoint', fake_checkpoint)
    path = save_proposal(store, 'cycle-1', {'kind': 'test', 'object_name': 'Rig', 'source_action': 'Walk',
                                            'proposal': {'supported': True}, 'transaction_schema': 1,
                                            'backend_fingerprint': fingerprint(m3_transaction.BASE_RUNTIME)})
    blender = SimulatedBlender()

    def call(mode, execution='exec'):
        return m3_transaction.transaction(blender, store, execution, 'test', '', mode=mode, operation=path)
    return blender, store, call, saved_checkpoints


def test_timeout_after_blender_executed_is_reconciled_not_duplicated(m3):
    blender, store, call, saved_checkpoints = m3
    blender.lose_next_answer = True
    first = call('apply')
    assert first['status'] == 'error' and first['idempotency']['state'] == 'UNCERTAIN'
    created = blender.candidates()
    assert len(created) == 1 and blender.active == created[0]

    second = call('apply')
    assert second['idempotency'] == {'key': 'cycle-1:apply', 'state': 'SUCCEEDED', 'attempt': 1,
                                     'replayed': False, 'reconciled': 'executed'}
    assert second['working_action'] == created[0] and second['numeric_verification'] == 'pass'
    assert blender.candidates() == created and blender.calls.count('execute') == 1
    assert len(saved_checkpoints) == 1, 'reconciliation must not save another checkpoint'

    calls = len(blender.calls)
    third = call('apply')
    assert third['idempotency']['replayed'] is True and len(blender.calls) == calls
    # The reconciled candidate is a normal candidate for verify and revert.
    assert call('verify')['numeric_verification'] == 'pass'
    assert call('revert')['status'] != 'error' and blender.active == 'Walk'


def test_timeout_before_blender_received_the_request_executes_once(m3):
    blender, store, call, _ = m3
    blender.lose_next_request = True
    first = call('apply')
    assert first['idempotency']['state'] == 'UNCERTAIN' and blender.candidates() == []
    second = call('apply')
    assert second['idempotency']['state'] == 'SUCCEEDED' and second['idempotency'].get('reconciled') is None
    assert len(blender.candidates()) == 1
    record = OperationLedger(store).get('cycle-1:apply')
    assert record['previous_attempt_reconciled'] == 'not_executed'


def test_plain_retry_replays_without_checkpoint_or_candidate(m3):
    blender, _, call, saved_checkpoints = m3
    call('apply')
    calls = len(blender.calls)
    again = call('apply')
    assert again['idempotency']['replayed'] and 'IDEMPOTENT_REPLAY' in again['warnings'][-1]
    assert len(blender.candidates()) == 1 and len(saved_checkpoints) == 1 and len(blender.calls) == calls


def test_preview_is_idempotent_per_proposal(m3):
    blender, _, call, _ = m3
    call('preview')
    assert call('preview')['idempotency']['replayed']
    assert len(blender.candidates()) == 1 and blender.active == 'Walk'


def test_revert_reopens_apply_as_a_new_attempt(m3):
    blender, store, call, _ = m3
    first = call('apply')
    call('revert')
    assert OperationLedger(store).get('cycle-1:apply')['state'] == 'REVERTED'
    second = call('apply')
    assert second['idempotency']['attempt'] == 2 and second['working_action'] != first['working_action']
    assert second['working_action'].endswith(deterministic_suffix('cycle-1:apply', 2, 8))
    assert call('revert')['idempotency']['key'] == 'cycle-1:revert:2'
    assert call('revert')['idempotency']['replayed']


def test_ambiguous_reconciliation_refuses_and_does_not_execute(m3):
    blender, store, call, _ = m3
    blender.lose_next_answer = True
    call('apply')
    blender.actions.append(blender.candidates()[0] + '.001')
    with pytest.raises(ValueError, match='OPERATION_OUTCOME_UNCERTAIN'):
        call('apply')
    assert blender.calls.count('execute') == 1
    assert OperationLedger(store).get('cycle-1:apply')['state'] == 'UNCERTAIN'


def test_candidate_left_inactive_by_blender_is_reported_failed(m3):
    blender, _, call, _ = m3
    blender.verification_passes = False
    blender.lose_next_answer = True
    call('apply')
    result = call('apply')
    assert result['idempotency']['state'] == 'FAILED' and result['status'] == 'error'
    assert any('APPLY_NOT_ACTIVATED' in str(e) for e in result['errors'])
    assert blender.calls.count('execute') == 1


def test_stale_proposal_leaves_no_ledger_record(m3):
    blender, store, call, _ = m3
    blender.active = 'SomethingElse'
    with pytest.raises(ValueError, match='STALE_PROPOSAL'):
        call('apply')
    assert OperationLedger(store).get('cycle-1:apply') is None
    blender.active = 'Walk'
    assert call('apply')['idempotency']['state'] == 'SUCCEEDED'


def test_record_of_a_dead_server_is_uncertain(tmp_path):
    ledger = OperationLedger(Storage(tmp_path))
    ledger.begin('k:apply', 'test', {'a': 1})
    path = ledger.path('k:apply')
    record = json.loads(path.read_text(encoding='utf-8'))
    record['pid'] = os.getpid() + 1
    path.write_text(json.dumps(record), encoding='utf-8')
    assert ledger.get('k:apply')['state'] == 'UNCERTAIN'
    assert ledger.begin('k:apply', 'test', {'a': 1})[0] == 'uncertain'


def test_key_reused_for_another_request_conflicts(tmp_path):
    ledger = OperationLedger(Storage(tmp_path))
    ledger.begin('client:abc12345', 'production.create_checkpoint', {'description': 'one'})
    with pytest.raises(ValueError, match='IDEMPOTENCY_KEY_CONFLICT'):
        ledger.begin('client:abc12345', 'production.create_checkpoint', {'description': 'two'})


def test_running_key_in_this_process_is_in_progress(tmp_path):
    ledger = OperationLedger(Storage(tmp_path))
    ledger.begin('k', 'test', {})
    assert ledger.begin('k', 'test', {})[0] == 'in_progress'


def test_m2_correction_timeout_retry_is_reconciled(tmp_path, monkeypatch):
    store = Storage(tmp_path / 'store')
    monkeypatch.setattr(corrections, 'create_checkpoint', lambda *a, **k: {'manifest': 'checkpoint'})
    path = save_proposal(store, 'm2-cycle', {'kind': 'retime', 'object_name': 'Rig', 'source_action': 'Walk',
                                             'proposal': {'supported': True}})
    blender = SimulatedBlender()
    blender.lose_next_answer = True
    first = corrections.correction(blender, store, 'e1', 'retime', mode='apply', operation=path)
    assert first['idempotency']['state'] == 'UNCERTAIN'
    second = corrections.correction(blender, store, 'e2', 'retime', mode='apply', operation=path)
    assert second['idempotency']['reconciled'] == 'executed' and second['idempotency']['state'] == 'SUCCEEDED'
    assert len(blender.candidates()) == 1 and blender.calls.count('execute') == 1


# ------------------------------------------------------------------ client operation_id (tool layer)

class CheckpointBlender:
    def __init__(self):
        self.lock = threading.RLock()
        self.saves = 0
        self.lose_next_answer = False

    def run(self, body, params=None):
        from blender_mcp.production.inspect import SCENE_SCRIPT
        if body == SCENE_SCRIPT:
            return {'source_file': None, 'scene': {'frame': 1}, 'objects': {}}
        if body == checkpoints.SAVE_COPY:
            self.saves += 1
            with open(params['path'], 'xb') as output:
                output.write(b'BLENDER-fixture\n{}')
            if self.lose_next_answer:
                self.lose_next_answer = False
                raise BlenderTimeoutError('TRANSPORT_TIMEOUT: simulated')
            return {'source_path': None, 'path': params['path'], 'copy': True}
        raise AssertionError('unexpected Blender call')


def test_client_operation_id_replays_a_finished_checkpoint(tmp_path, monkeypatch):
    from blender_mcp.production import server
    blender, store = CheckpointBlender(), Storage(tmp_path / 'store')
    monkeypatch.setattr(server, 'resources', lambda: (blender, store))
    first = server.checkpoint('Before edit', operation_id='client-op-0001')
    second = server.checkpoint('Before edit', operation_id='client-op-0001')
    assert first['status'] != 'error' and first['operation_id'] == 'client-op-0001'
    assert second['idempotency']['replayed'] and second['checkpoint'] == first['checkpoint']
    assert blender.saves == 1
    status = server.operation_status(operation_id='client-op-0001')
    assert status['data']['operation']['state'] == 'SUCCEEDED'
    assert server.operation_status(operation_id='never-used-01')['data']['operation']['state'] == 'NEW'


def test_client_operation_id_uncertain_is_never_executed_again(tmp_path, monkeypatch):
    from blender_mcp.production import server
    blender, store = CheckpointBlender(), Storage(tmp_path / 'store')
    monkeypatch.setattr(server, 'resources', lambda: (blender, store))
    blender.lose_next_answer = True
    first = server.checkpoint('Before edit', operation_id='client-op-0002')
    assert first['status'] == 'error' and first['idempotency']['state'] == 'UNCERTAIN'
    retry = server.checkpoint('Before edit', operation_id='client-op-0002')
    assert retry['error_codes'] == ['OPERATION_OUTCOME_UNCERTAIN'] and blender.saves == 1
    assert server.operation_status(operation_id='client-op-0002')['data']['operation']['state'] == 'UNCERTAIN'


def test_client_operation_id_conflict_and_format(tmp_path, monkeypatch):
    from blender_mcp.production import server
    blender, store = CheckpointBlender(), Storage(tmp_path / 'store')
    monkeypatch.setattr(server, 'resources', lambda: (blender, store))
    server.checkpoint('First', operation_id='client-op-0003')
    assert server.checkpoint('Second', operation_id='client-op-0003')['error_codes'] == ['IDEMPOTENCY_KEY_CONFLICT']
    assert server.checkpoint('First', operation_id='bad key')['error_codes'] == ['INVALID_OPERATION_ID']
    assert blender.saves == 1


def test_mutating_tools_expose_operation_id_and_run_off_the_event_loop():
    import asyncio
    import inspect
    from blender_mcp.production.server import mcp
    tools = asyncio.run(mcp.list_tools())
    for listed in tools:
        if listed.name == 'production.operation_status':
            continue  # its operation_id is the key being queried
        props = listed.inputSchema['properties']
        # Readers (including transient ones that restore state) need no idempotency key.
        assert ('operation_id' in props) is listed.meta['production']['accepts_operation_id'], listed.name
        assert listed.meta['production']['accepts_operation_id'] is (not listed.annotations.idempotentHint), listed.name
    for registered in mcp._tool_manager.list_tools():
        assert inspect.iscoroutinefunction(registered.fn), registered.name
