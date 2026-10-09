"""Exclusive local artifact directories. Never overwrite an existing artifact."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: dict):
    with path.open("x", encoding="utf-8") as dest:
        json.dump(data, dest, indent=2, ensure_ascii=False, allow_nan=False)


class Storage:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("BLENDER_PRODUCTION_WORKDIR", ".production")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def reserve(self, category: str, operation_id: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        parent = self.root / category
        parent.mkdir(exist_ok=True)
        path = parent / f"{stamp}-{uuid4().hex[:12]}"
        path.mkdir(exist_ok=False)
        write_json(path / "reservation.json", {"operation_id": operation_id, "created_at": stamp})
        return path

    def checkpoint_manifest(self, path: str) -> tuple[Path, dict]:
        candidate = Path(path).resolve(strict=True)
        if not candidate.is_relative_to(self.root / "checkpoints") or candidate.name != "manifest.json":
            raise ValueError("Only manifests inside this workspace's checkpoint store are accepted")
        manifest = json.loads(candidate.read_text(encoding="utf-8"))
        blend = (candidate.parent / manifest["filename"]).resolve(strict=True)
        if blend.parent != candidate.parent or blend.suffix != ".blend":
            raise ValueError("Invalid checkpoint file path")
        if sha256(blend) != manifest["sha256"]:
            raise ValueError("Checkpoint integrity check failed")
        return blend, manifest
