"""Preservación local M9: selección explícita, copias exclusivas y hashes.

Inventariar no copia. Detener los runners propios antes de artifacts; ejecutar
docs después de la documentación final, y release tras crear commit/tag/bundle.
No reescribe las rutas absolutas firmadas ni declara portabilidad del runtime.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys


_SPEC = importlib.util.spec_from_file_location(
    "_m9_archive_m8_helpers", Path(__file__).with_name("archive_m8_evidence.py"))
_HELPERS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _HELPERS
_SPEC.loader.exec_module(_HELPERS)
ArchiveError = _HELPERS.ArchiveError
Selection = _HELPERS.Selection
inventory = _HELPERS.inventory
copy_snapshot = _HELPERS.copy_snapshot
digest = _HELPERS.digest
identity = _HELPERS.identity
timestamp = _HELPERS.timestamp
reject_ancestor_links = _HELPERS.reject_ancestor_links
write_json_exclusive = _HELPERS.write_json_exclusive

REPOSITORY = Path(__file__).resolve().parents[2]
TEMPORARY = Path(tempfile.gettempdir())
VAULT_ROOT = Path(os.environ.get("BLENDER_PRODUCTION_ARCHIVE_ROOT", str(REPOSITORY.parent / "blender-production-archives")))
REQUEST = Path(os.environ.get("BLENDER_PRODUCTION_ARCHIVE_REQUEST", str(REPOSITORY / "test-artifacts/m9/request.md")))
FINAL_FILES = frozenset(("README.md", "AGENTS.md", "CHANGELOG.md", "LICENSE", "pyproject.toml", "uv.lock"))
SCHEMA = "m9.local_preservation.v1"
BUNDLE = "blender-production-mcp-milestone9.bundle"
MANIFESTS = {"artifacts": "artifact-manifest.json", "docs": "documentation-manifest.json"}
RESERVED = frozenset((*MANIFESTS.values(), "release-record.json", BUNDLE))


def relative_to(path, root):
    try:
        return Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return None


def validate_destination(destination):
    destination = Path(destination)
    reject_ancestor_links(destination)
    relative = relative_to(destination, VAULT_ROOT)
    if relative is None or not relative.parts or not relative.parts[0].startswith("M9-"):
        raise ArchiveError("DESTINATION_OUTSIDE_M9_VAULT")
    if relative_to(destination, REPOSITORY) is not None:
        raise ArchiveError("DESTINATION_MUST_BE_EXTERNAL_TO_REPOSITORY")
    return destination.resolve()


def validate_source(source):
    source = Path(source)
    if not source.is_absolute():
        raise ArchiveError("ABSOLUTE_SOURCE_REQUIRED")
    reject_ancestor_links(source)
    repo = relative_to(source, REPOSITORY)
    temp = relative_to(source, TEMPORARY)
    allowed = source.resolve() == REQUEST.resolve()
    if repo is not None and repo.parts:
        parts = repo.parts
        allowed |= (len(parts) == 1 and parts[0] in FINAL_FILES)
        allowed |= parts[0] in {"docs", "tests", "src"}
        allowed |= len(parts) > 1 and parts[0] == "test-artifacts" and parts[1].lower().startswith("m9")
        allowed |= len(parts) > 2 and parts[:2] == (".production", "backups") and parts[2].lower().startswith("m9")
    if temp is not None and temp.parts:
        allowed |= temp.parts[0].lower().startswith("bpm9")
    if not allowed:
        raise ArchiveError(f"SOURCE_OUTSIDE_ALLOWLIST: {source}")
    return source.resolve()


def validate_relative(value):
    if not isinstance(value, str) or not value:
        raise ArchiveError("INVALID_ARCHIVE_RELATIVE_PATH")
    windows = PureWindowsPath(value)
    if windows.anchor or ".." in windows.parts or not windows.parts:
        raise ArchiveError(f"INVALID_ARCHIVE_RELATIVE_PATH: {value}")
    # Avoid ADS, device names and Windows trailing-character aliases.
    for part in windows.parts:
        if (part != part.rstrip(" .") or any(c in part for c in ':*?"<>|')
                or PureWindowsPath(part).is_reserved()):
            raise ArchiveError(f"INVALID_ARCHIVE_RELATIVE_PATH: {value}")
    first = windows.parts[0].lower()
    if first in {x.lower() for x in RESERVED} or first.endswith(".incomplete.json"):
        raise ArchiveError(f"RESERVED_ARCHIVE_TARGET: {value}")
    return Path(*windows.parts)


def load_selection(path):
    path = Path(path)
    reject_ancestor_links(path)
    before = identity(path)
    source_bytes = path.read_bytes()
    value = json.loads(source_bytes.decode("utf-8-sig"))
    if identity(path) != before:
        raise ArchiveError("SELECTION_CHANGED_DURING_READ")
    if not isinstance(value, list) or not value:
        raise ArchiveError("NONEMPTY_SELECTION_LIST_REQUIRED")
    selections = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"source", "relative", "expected_sha256"}:
            raise ArchiveError("INVALID_SELECTION_FIELDS")
        if not isinstance(item.get("source"), str):
            raise ArchiveError("SELECTION_SOURCE_REQUIRED")
        source = validate_source(item["source"])
        relative = validate_relative(item.get("relative"))
        expected = item.get("expected_sha256")
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)):
            raise ArchiveError("INVALID_EXPECTED_SHA256")
        if expected is not None and not source.is_file():
            raise ArchiveError("EXPECTED_SHA256_REQUIRES_FILE_SELECTION")
        selections.append(Selection(source, relative, expected.lower() if expected else None))
    import hashlib
    return selections, {"path": str(path.resolve()), "sha256": hashlib.sha256(source_bytes).hexdigest()}


def verify_manifest(path, destination, *, originals=False):
    reject_ancestor_links(path)
    value = json.loads(Path(path).read_text("utf-8"))
    if value.get("schema") != SCHEMA:
        raise ArchiveError("EXPECTED_M9_MANIFEST_SCHEMA")
    for record in value.get("files", []):
        if record.get("original_sha256") != record.get("sha256") or record.get("copy_sha256") != record.get("sha256"):
            raise ArchiveError("MANIFEST_HASH_FIELDS_MISMATCH")
        relative = validate_relative(record["relative"])
        target = Path(destination) / relative
        reject_ancestor_links(target)
        if Path(record["archive"]).resolve() != target.resolve():
            raise ArchiveError("MANIFEST_ARCHIVE_PATH_MISMATCH")
        if originals:
            source = validate_source(record["original"])
            before = identity(source)
            if digest(source) != record["sha256"] or identity(source) != before:
                raise ArchiveError(f"ORIGINAL_CHANGED_AFTER_ARCHIVE: {source}")
    value = _HELPERS.verify_manifest(path, destination)
    if value.get("file_count") != len(value["files"]) or value.get("bytes") != sum(r["bytes"] for r in value["files"]):
        raise ArchiveError("MANIFEST_TOTALS_MISMATCH")
    return value


def archive_files(selections, destination, phase, selection_record=None):
    if phase not in MANIFESTS:
        raise ArchiveError("COPY_PHASE_REQUIRED")
    destination = validate_destination(destination)
    for selection in selections:
        validate_source(selection.source)
        validate_relative(str(selection.relative))
    name = MANIFESTS[phase]
    if phase == "artifacts" and destination.exists():
        raise ArchiveError(f"DESTINATION_EXISTS: {destination}")
    if phase == "docs":
        verify_manifest(destination / MANIFESTS["artifacts"], destination)
        if (destination / name).exists():
            raise ArchiveError(f"MANIFEST_EXISTS: {name}")
    snapshots = inventory(selections)
    # Reject file/directory conflicts before copying the first byte.
    targets = {str(x.relative).replace("\\", "/").lower() for x in snapshots}
    for item in snapshots:
        for parent in item.relative.parents:
            if parent != Path(".") and str(parent).replace("\\", "/").lower() in targets:
                raise ArchiveError(f"ARCHIVE_TARGET_PREFIX_CONFLICT: {item.relative}")
        target = destination / item.relative
        if target.exists() or any(p.is_file() for p in target.parents):
            raise ArchiveError(f"ARCHIVE_TARGET_EXISTS: {item.relative}")
    if phase == "artifacts":
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(exist_ok=False)
    records = []
    try:
        for item in snapshots:
            record = copy_snapshot(item, destination)
            record.update(original_sha256=item.sha256, copy_sha256=digest(destination / item.relative))
            records.append(record)
        if inventory(selections) != snapshots:
            raise ArchiveError("SOURCE_SELECTION_CHANGED_DURING_ARCHIVE")
        manifest = {
            "schema": SCHEMA, "phase": phase, "created_at_utc": timestamp(),
            "status": "VERIFIED_COPY", "source_bytes_unchanged": True,
            "runtime_portability": "NOT_CLAIMED_ABSOLUTE_SIGNED_PATHS_RETAINED",
            "selection": selection_record, "files": records, "file_count": len(records),
            "bytes": sum(x["bytes"] for x in records),
        }
        write_json_exclusive(destination / name, manifest)
        return manifest
    except Exception as error:
        failure = destination / (name + ".incomplete.json")
        if not failure.exists():
            write_json_exclusive(failure, {"schema": SCHEMA, "phase": phase, "status": "INCOMPLETE",
                "error": str(error), "verified_files": records, "created_at_utc": timestamp()})
        raise


def release_record(destination, commit, tag="milestone-9"):
    destination = validate_destination(destination)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit or ""):
        raise ArchiveError("EXACT_COMMIT_SHA_REQUIRED")
    commit = commit.lower()
    if tag != "milestone-9":
        raise ArchiveError("EXPECTED_LOCAL_TAG_MILESTONE_9")
    record_path = destination / "release-record.json"
    if record_path.exists():
        raise ArchiveError(f"RELEASE_RECORD_EXISTS: {record_path}")

    def git(*args):
        result = subprocess.run(["git", *args], cwd=REPOSITORY, text=True, capture_output=True)
        if result.returncode:
            raise ArchiveError(f"GIT_VERIFICATION_FAILED: {result.stderr.strip()}")
        return result.stdout.strip()

    if git("rev-parse", "HEAD") != commit or git("rev-parse", "refs/tags/milestone-9^{commit}") != commit:
        raise ArchiveError("COMMIT_HEAD_TAG_MISMATCH")
    manifests = []
    for name in MANIFESTS.values():
        path = destination / name
        before = identity(path)
        verify_manifest(path, destination, originals=True)
        checksum = digest(path)
        if identity(path) != before:
            raise ArchiveError("MANIFEST_CHANGED_DURING_VERIFICATION")
        manifests.append({"path": name, "sha256": checksum})
    bundle = destination / BUNDLE
    if not bundle.is_file():
        raise ArchiveError("M9_GIT_BUNDLE_REQUIRED")
    reject_ancestor_links(bundle)
    before = identity(bundle)
    with bundle.open("rb") as stream:
        for line in stream:
            if line in (b"\n", b"\r\n"):
                break
            if line.startswith(b"-"):
                raise ArchiveError("GIT_BUNDLE_REQUIRES_EXTERNAL_PREREQUISITES")
    git("bundle", "verify", str(bundle))
    refs = {ref: sha for sha, ref in (line.split(maxsplit=1) for line in git("bundle", "list-heads", str(bundle)).splitlines())}
    if refs.get("refs/tags/milestone-9") != git("rev-parse", "refs/tags/milestone-9") or commit not in refs.values():
        raise ArchiveError("GIT_BUNDLE_RELEASE_REFS_MISMATCH")
    checksum = digest(bundle)
    if identity(bundle) != before:
        raise ArchiveError("GIT_BUNDLE_CHANGED_DURING_VERIFICATION")
    record = {"schema": "m9.external_release_record.v1", "created_at_utc": timestamp(),
        "commit": commit, "tag": tag, "published": False, "pushed": False,
        "manifests": manifests, "signed_paths_rewritten": False,
        "record_is_external_to_git_commit": True,
        "git_bundle": {"path": str(bundle), "sha256": checksum, "bytes": before[0],
                       "git_bundle_verify": "PASS", "release_refs_verified": True}}
    write_json_exclusive(record_path, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, help="JSON: lista explícita source/relative/expected_sha256 opcional")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--phase", choices=("inventory", "artifacts", "docs", "release"), default="inventory")
    parser.add_argument("--commit", "--commit-sha", "--commitSHA", dest="commit")
    parser.add_argument("--tag", default="milestone-9")
    args = parser.parse_args()
    destination = validate_destination(args.destination)
    if args.phase == "release":
        result = release_record(destination, args.commit, args.tag)
    else:
        if args.selection is None:
            parser.error("--selection es obligatorio para inventory/artifacts/docs")
        selections, selection_record = load_selection(args.selection)
        if args.phase == "inventory":
            files = inventory(selections)
            result = {"phase": "READ_ONLY_INVENTORY", "destination": str(destination),
                "destination_exists": destination.exists(), "selection": selection_record,
                "file_count": len(files), "bytes": sum(x.size for x in files)}
        else:
            result = archive_files(selections, destination, args.phase, selection_record)
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
