"""Immutable generation plans and append-only, integrity-checked lifecycle events."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

from ...common.operations import fingerprint
from ...common.storage import write_json

STATES = {'PLANNED', 'GENERATED_CANDIDATE', 'REVIEWED', 'ACCEPTED', 'REJECTED'}
# M7 reuses this ledger for facial performances in its own category with its own candidate state.
FACIAL_STATES = {'PLANNED', 'FACIAL_CANDIDATE', 'REVIEWED', 'ACCEPTED', 'REJECTED'}


def save(storage, operation_id, data, category='generations', states=STATES):
    folder = storage.reserve(category, operation_id)
    payload = dict(data, generation_id=uuid4().hex, operation_id=operation_id,
                   generation_ref=str(folder / 'plan.json'),
                   created_at=datetime.now(timezone.utc).isoformat())
    write_json(folder / 'plan.json', dict(payload, sha256=fingerprint(payload)))
    event(folder, {'state': 'PLANNED', 'refinement_cycles': 0}, states)
    return str(folder / 'plan.json'), payload


def load(storage, reference, category='generations'):
    path = Path(reference).resolve(strict=True)
    if path.name != 'plan.json' or not path.is_relative_to(storage.root / category):
        raise ValueError('INVALID_GENERATION_REFERENCE: use this workspace ' + category + ' plan.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    digest = data.pop('sha256')
    if fingerprint(data) != digest:
        raise ValueError('STALE_PROPOSAL: generation plan integrity failed')
    events = sorted(path.parent.glob('event-*.json'))
    state = {}
    previous = None
    for p in events:
        entry = json.loads(p.read_text(encoding='utf-8'))
        signed = entry.pop('sha256')
        if fingerprint(entry) != signed or entry['previous'] != previous:
            raise ValueError('GENERATION_LEDGER_INVALID: event integrity/lineage failed')
        previous = signed
        state.update(entry['data'])
    return path, data, state


def event(folder, data, states=STATES):
    if data.get('state') not in states:
        raise ValueError('INVALID_GENERATION_STATE')
    events = sorted(Path(folder).glob('event-*.json'))
    previous = json.loads(events[-1].read_text(encoding='utf-8'))['sha256'] if events else None
    row = {'data': data, 'previous': previous, 'at': datetime.now(timezone.utc).isoformat()}
    filename = 'event-' + str(len(events)).zfill(6) + '-' + uuid4().hex[:8] + '.json'
    write_json(Path(folder) / filename, dict(row, sha256=fingerprint(row)))
    return str(Path(folder) / filename)


def mutable(state):
    if state.get('state') in {'ACCEPTED', 'REJECTED'}:
        raise ValueError('GENERATION_TERMINAL: create another generation; retain this evidence')


def refinement_gate(state):
    mutable(state)
    if state.get('refinement_cycles', 0) >= 3:
        raise ValueError('REFINEMENT_LIMIT: three cycles exhausted; manual review required')


def review_gate(review, render):
    if not render or render.get('status') not in {'ok', 'success'} or not render.get('temporal_complete'):
        raise ValueError('TEMPORAL_REVIEW_REQUIRED: render the full temporal candidate first')
    fields = {'reviewer', 'observations', 'findings', 'decision', 'artifact_reference', 'temporal_reviewed'}
    if not isinstance(review, dict) or not fields <= set(review):
        raise ValueError('VISUAL_REVIEW_REQUIRED: reviewer, observations, findings, decision, artifact_reference, temporal_reviewed')
    if review['artifact_reference'] != render.get('output_dir') or review['temporal_reviewed'] is not True:
        raise ValueError('VISUAL_REVIEW_STALE: review must reference the current full temporal artifact')
    if review['decision'] not in {'ACCEPT', 'REJECT', 'REFINE'} or not review['reviewer'] or not review['observations']:
        raise ValueError('VISUAL_REVIEW_INVALID: record observations and an explicit decision')
    return review
