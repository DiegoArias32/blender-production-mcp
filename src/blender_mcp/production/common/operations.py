"""Immutable, workspace-confined proposals and append-only operation events."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .storage import write_json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def save_proposal(storage, operation_id, proposal):
    folder = storage.reserve("operations", operation_id)
    path = folder / "proposal.json"
    write_json(path, {**proposal, "operation_id": operation_id,
                      "proposal_sha256": fingerprint(proposal)})
    return str(path)


def load_proposal(storage, path):
    candidate = Path(path).resolve(strict=True)
    if candidate.name != "proposal.json" or not candidate.is_relative_to(storage.root / "operations"):
        raise ValueError("Operation must be a proposal.json in this workspace's animation store")
    data = json.loads(candidate.read_text(encoding="utf-8"))
    payload = {key: value for key, value in data.items() if key not in {"operation_id", "proposal_sha256"}}
    if fingerprint(payload) != data["proposal_sha256"]:
        raise ValueError("Proposal integrity check failed")
    return candidate, data


def record_event(path, mode, data):
    event = Path(path).parent / (mode + "-" + uuid4().hex[:12] + ".json")
    write_json(event, data)
    return str(event)


def latest_candidate(path):
    events = [p for prefix in ("preview-", "apply-")
              for p in Path(path).parent.glob(prefix + "*.json")]
    for event in sorted(events, key=lambda p: p.stat().st_mtime_ns, reverse=True):
        data = json.loads(event.read_text(encoding="utf-8"))
        if data.get("working_action"):
            return data
    raise ValueError("No completed preview/apply candidate exists for this operation")
