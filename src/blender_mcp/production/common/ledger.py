"""Operation ledger for mutation idempotency (M3.1, audit AUD-010).

A client that times out cannot know whether Blender executed its request. Replaying blindly can
duplicate Actions, checkpoints and candidates. Every mutating high-level operation therefore runs
under a ledger key:

- a client-supplied ``operation_id`` (any mutating tool), or
- a key derived from an immutable proposal cycle (``<correction_id>:preview`` / ``:apply`` /
  ``:revert:<apply attempt>``) when the client supplies none.

States
------
NEW        no record (the operation never started, or it was abandoned before any mutating command
           reached Blender).
RUNNING    started by this server process; a second request with the same key is refused.
SUCCEEDED  completed; a retry returns the stored result without touching Blender.
FAILED     completed with a known failure; a retry returns the stored failure.
UNCERTAIN  a mutating command was sent and no answer arrived (timeout / connection lost), or the
           server that started it died. A retry never re-executes: it reconciles when the domain
           supports it, otherwise it reports the uncertainty.
REVERTED   a successful apply that was later reverted; a new apply attempt is allowed.

A key reused with different request arguments is refused (IDEMPOTENCY_KEY_CONFLICT).
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

STATES = ('NEW', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNCERTAIN', 'REVERTED')
UNCERTAIN_ERROR_TYPES = ('BlenderTimeoutError', 'BlenderConnectionError', 'TimeoutError')
# Answers that say "not finished yet / not known", never "failed": a retry must be able to resolve them.
UNCERTAIN_CODES = ('OPERATION_IN_PROGRESS', 'OPERATION_OUTCOME_UNCERTAIN')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_fingerprint(request) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def deterministic_suffix(key: str, attempt: int, length: int = 8) -> str:
    """Candidate name suffix derived from the ledger key, so a retry can find a candidate that a
    timed-out request may have created."""
    return hashlib.sha256((key + ':' + str(attempt)).encode()).hexdigest()[:length]


def is_uncertain(error) -> bool:
    if isinstance(error, BaseException):
        return bool(getattr(error, 'uncertain', False)) or type(error).__name__ in UNCERTAIN_ERROR_TYPES
    if isinstance(error, dict):
        return error.get('type') in UNCERTAIN_ERROR_TYPES or error.get('code') in UNCERTAIN_CODES
    return False


class OperationLedger:
    def __init__(self, storage):
        self.folder = Path(storage.root) / 'ledger'

    def path(self, key: str) -> Path:
        return self.folder / (hashlib.sha256(key.encode()).hexdigest()[:40] + '.json')

    def get(self, key: str) -> dict | None:
        path = self.path(key)
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('state') == 'RUNNING' and record.get('pid') != os.getpid():
            # The server that started it is gone; Blender may or may not have finished.
            record['state'] = 'UNCERTAIN'
            record['uncertainty'] = 'started by a server process that is no longer running'
        return record

    def _write(self, key: str, record: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        temporary = path.with_suffix('.tmp-' + uuid4().hex[:8])
        temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        os.replace(temporary, path)

    def begin(self, key: str, tool: str, request) -> tuple[str, dict]:
        """Returns ('execute', record) | ('replay', record) | ('in_progress', record) | ('uncertain', record)."""
        fingerprint = request_fingerprint(request)
        existing = self.get(key)
        history = []
        attempt = 1
        if existing is not None:
            if existing.get('request_fingerprint') != fingerprint:
                raise ValueError('IDEMPOTENCY_KEY_CONFLICT: key ' + key + ' was used for a different request ('
                                 + str(existing.get('tool')) + ')')
            state = existing['state']
            if state in ('SUCCEEDED', 'FAILED'):
                return 'replay', existing
            if state == 'RUNNING':
                return 'in_progress', existing
            if state == 'UNCERTAIN':
                return 'uncertain', existing
            history = existing.get('history', []) + [{k: existing.get(k) for k in ('state', 'attempt', 'started_at', 'finished_at')}]
            attempt = int(existing.get('attempt', 1)) + 1
        record = {'key': key, 'tool': tool, 'state': 'RUNNING', 'request_fingerprint': fingerprint, 'attempt': attempt,
                  'pid': os.getpid(), 'started_at': _now(), 'finished_at': None, 'history': history}
        self._write(key, record)
        return 'execute', record

    def finish(self, key: str, state: str, result=None, **extra) -> dict:
        if state not in STATES:
            raise ValueError('Unknown ledger state: ' + str(state))
        record = self.get(key) or {'key': key}
        record.update(state=state, finished_at=_now(), result=result, **extra)
        self._write(key, record)
        return record

    def abandon(self, key: str) -> None:
        """Nothing mutating reached Blender: forget the attempt so a retry executes normally."""
        record = self.get(key)
        if record is None:
            return
        if record.get('history'):
            previous = record['history'][-1]
            record.update(state=previous.get('state') or 'REVERTED', attempt=previous.get('attempt', 1),
                          history=record['history'][:-1], started_at=previous.get('started_at'),
                          finished_at=previous.get('finished_at'))
            self._write(key, record)
        else:
            self.path(key).unlink()

    def mark(self, key: str, state: str, **extra) -> dict | None:
        record = self.get(key)
        if record is None:
            return None
        record.update(state=state, **extra)
        self._write(key, record)
        return record


def outcome_state(data: dict) -> str:
    errors = data.get('errors') or []
    if data.get('status') == 'error' and any(is_uncertain(e) for e in errors):
        return 'UNCERTAIN'
    if data.get('status') == 'error':
        return 'FAILED'
    return 'SUCCEEDED'


def idempotency_block(record: dict, replayed: bool, reconciled: str | None = None) -> dict:
    block = {'key': record.get('key'), 'state': record.get('state'), 'attempt': record.get('attempt'),
             'replayed': replayed}
    if reconciled:
        block['reconciled'] = reconciled
    return block


def replayed(record: dict) -> dict:
    """Stored outcome of a finished operation; Blender is not contacted."""
    result = dict(record.get('result') or {'status': 'error', 'errors': ['Ledger record has no stored result']})
    result['idempotency'] = idempotency_block(record, True)
    result.setdefault('warnings', [])
    result['warnings'] = list(result['warnings']) + ['IDEMPOTENT_REPLAY: stored result of ' + str(record.get('key'))
                                                     + ' returned; Blender was not called again']
    return result


# --- Proposal cycles (preview / apply / revert) -------------------------------------------------

def cycle_key(ledger: OperationLedger, correction_id: str, mode: str) -> str:
    if mode in ('preview', 'apply'):
        return correction_id + ':' + mode
    applied = ledger.get(correction_id + ':apply')
    return correction_id + ':revert:' + str(applied.get('attempt', 1) if applied else 0)


# Read-only probe run after an uncertain outcome. Blender executes socket commands one at a time in
# arrival order (upstream addon: one FIFO queue drained on the main thread), so when this probe
# answers, a command sent before it has finished.
RECONCILE = r'''
obj=bpy.data.objects.get(params['object_name'])
prefix=params['candidate_name']
found=[a for a in bpy.data.actions if prefix and (a.name==prefix or a.name.startswith(prefix+'.'))]
active=obj.animation_data.action if obj is not None and obj.animation_data else None
result={'object_found':obj is not None,'candidates':sorted(a.name for a in found),
    'active_action':active.name if active else None,
    'working_fingerprint':m2_digest(m2_snapshot(found[0])) if len(found)==1 else None}
'''


def reconcile_outcome(mode: str, found: dict, source_action: str | None, candidate: str | None) -> str:
    """'executed' | 'not_executed' | 'ambiguous' for an uncertain preview/apply/revert."""
    if not found.get('object_found') or len(found.get('candidates', [])) > 1:
        return 'ambiguous'
    if mode in ('preview', 'apply'):
        return 'executed' if found['candidates'] else 'not_executed'
    if found.get('active_action') == source_action:
        return 'executed'
    if candidate and found.get('active_action') == candidate:
        return 'not_executed'
    return 'ambiguous'


def guarded_cycle(bridge, storage, *, tool: str, correction_id: str, mode: str, request: dict, body: str,
                  object_name: str, source_action: str | None, candidate_prefix: str, suffix_length: int,
                  previous_candidate: str | None, execute, measure, reconcile: str | None = None):
    """Idempotent preview/apply/revert of one immutable proposal.

    ``execute(suffix, note)`` performs preflight, asset guard, checkpoint and the Blender mutation
    and returns the public result; it must raise only before the mutation is sent. ``note(**fields)``
    stores recovery facts (checkpoint) in the ledger. ``measure(candidate, working_fingerprint,
    checkpoint)`` re-verifies a candidate found by reconciliation.
    """
    ledger = OperationLedger(storage)
    key = cycle_key(ledger, correction_id, mode)
    action, record = ledger.begin(key, tool, request)
    if action == 'replay':
        return replayed(record)
    if action == 'in_progress':
        raise ValueError('OPERATION_IN_PROGRESS: ' + key + ' is still running in this server; query production.operation_status')
    if action == 'uncertain':
        candidate = candidate_prefix + deterministic_suffix(key, record.get('attempt', 1), suffix_length) if mode != 'revert' else previous_candidate
        # `reconcile` is a complete template for domains whose candidates are not Actions (M4 meshes).
        found = bridge.run(reconcile if reconcile else body + RECONCILE, {'object_name': object_name, 'candidate_name': candidate})
        outcome = reconcile_outcome(mode, found, source_action, candidate)
        if outcome == 'ambiguous':
            ledger.mark(key, 'UNCERTAIN', reconciliation=found)
            raise ValueError('OPERATION_OUTCOME_UNCERTAIN: ' + key + ' cannot be reconciled automatically (' + json.dumps(found)
                             + '); inspect Blender and the recovery checkpoint ' + str(record.get('checkpoint')))
        if outcome == 'executed':
            if mode == 'revert':
                result = {'status': 'warning', 'reverted': True, 'active_action': source_action,
                          'candidate_preserved': previous_candidate, 'checkpoint': record.get('checkpoint'),
                          'warnings': ['Revert outcome reconstructed after an uncertain transport failure']}
                state = 'SUCCEEDED'
            else:
                result = measure(found['candidates'][0], found['working_fingerprint'], record.get('checkpoint'))
                state = outcome_state(result)
                if mode == 'apply' and found.get('active_action') != found['candidates'][0]:
                    # EXECUTE activates the candidate only after its in-Blender verification passed.
                    state = 'FAILED'
                    result['status'] = 'error'
                    result.setdefault('errors', []).append('APPLY_NOT_ACTIVATED: the candidate exists but Blender did not activate it')
                if state == 'UNCERTAIN':
                    state = 'FAILED'
            result['idempotency'] = idempotency_block(dict(record, state=state), False, 'executed')
            ledger.finish(key, state, result=result, reconciliation=found)
            if mode == 'revert':
                ledger.mark(correction_id + ':apply', 'REVERTED', reverted_by=key)
            return result
        # Nothing from the uncertain attempt reached Blender's Action data: run it now.
        ledger.abandon(key)
        action, record = ledger.begin(key, tool, request)
        ledger.mark(key, 'RUNNING', previous_attempt_reconciled='not_executed', reconciliation=found)
    suffix = deterministic_suffix(key, record['attempt'], suffix_length)
    if mode != 'revert':
        ledger.mark(key, 'RUNNING', candidate_name=candidate_prefix + suffix)
    try:
        result = execute(suffix, lambda **fields: ledger.mark(key, 'RUNNING', **fields))
    except BaseException:
        ledger.abandon(key)
        raise
    state = outcome_state(result)
    result['idempotency'] = idempotency_block(dict(record, state=state), False)
    ledger.finish(key, state, result=result)
    if mode == 'revert' and state == 'SUCCEEDED':
        ledger.mark(correction_id + ':apply', 'REVERTED', reverted_by=key)
    return result
