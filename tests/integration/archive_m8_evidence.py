"""Preserve the explicitly selected M8 evidence locally; never migrate a plan.

The default invocation only inventories files. Run ``--phase artifacts`` after
the owned runners stop, then ``--phase docs`` after final documentation, and
``--phase release --commit <SHA>`` after the local commit/tag. No Blender, socket,
dependency installation, deletion, or alteration of source bytes is involved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import NamedTuple


REPOSITORY = Path(__file__).resolve().parents[2]
TEMPORARY = Path(tempfile.gettempdir())
DESTINATION = Path(os.environ.get("BLENDER_PRODUCTION_ARCHIVE_ROOT", str(REPOSITORY.parent / "blender-production-archives"))) / "M8-local"
TEMP_NAMES = (
    "bpm8-256678ca", "bpm8-31b29f56", "bpm8-3edd9b09", "bpm8-a7762bcc",
    "bpm8-ae3a791f", "bpm8-fc462c12", "bpm8base-e8524b25",
    "bpm8bodyinspect-1aacoeyr", "bpm8hairangle-07ff16da", "bpm8hairangle-10dd38cd",
    "bpm8hairangle-afda6961", "bpm8hairreplace-e6e3bde2", "bpm8hairroots-ccd1a1a6",
    "bpm8perf-6613eb30", "bpm8positive-1f63fa99", "bpm8positive-aed9785b",
    "bpm8positive-fab02cc2", "bpm8proxy-8de459c3", "bpm8safe-3120b6a7",
    "bpm8safe-3987443a", "bpm8safe-7722f385", "bpm8safe-9ac56866",
    "bpm8safe-e0974cc7", "bpm8safe-edd18c6a", "bpm8visible-body-cu4mnrvh",
    "bpm8-a233eb61", "bpm8render-pre-4956cbe2", "bpm8render-pre-a7234de1",
    "bpm8render-post-3c98b802", "bpm8nativecapture-833ab0dd",
)
TEST_RECORDS = (
    "m8-baseline-pytest.txt", "m8-baseline-pytest.xml",
    "m8-final-nonunit-r2.txt", "m8-final-nonunit-r2.xml",
    "m8-final-regression-r2.txt", "m8-final-regression-r2.xml",
    "m8-final-regression.txt", "m8-final-regression.xml",
    "m8-regression-in-progress.txt", "m8-regression-in-progress.xml",
    "m8-release-pytest.txt", "m8-release-pytest.xml",
    "m8-final-release-pytest.txt", "m8-final-release-pytest.xml",
)
REPRODUCTION_FILES = (
    "tests/blender/fixtures_m8.py", "tests/blender/m8_pipe_harness.py",
    "tests/blender/m8_pipe_worker.py", "tests/blender/probe_m8_performance.py",
    "tests/blender/probe_m8_positive.py", "tests/blender/probe_m8_proxy.py",
    "tests/blender/probe_m8_cloth.py", "tests/integration/run_m8_mcp.py",
    "tests/blender/probe_m8_render_restore.py",
    "tests/integration/archive_m8_evidence.py",
)
HISTORICAL = (
    ("bpm7/release/fixtures/demoD_m6_copy.blend",
     "2492f0d3b798164aa06f0c607d6e10fdedcb38cb6b893c19076a463ec67cd4a0"),
    ("bpm7/m6src/m6_final.blend",
     "a93fe8f71d860184d98a121179244f737ce16d21375d8a828fb1bcde8d78eab3"),
)
ACCEPTED_FACE = "bpm7/release/store/demoD/facial/20261005T060739.325043Z-f8025db6afa9"
FINAL_FILES = ("README.md", "AGENTS.md", "CHANGELOG.md", "LICENSE", "pyproject.toml", "uv.lock")


class ArchiveError(RuntimeError):
    pass


class Selection(NamedTuple):
    source: Path
    relative: Path
    expected_sha256: str | None = None


# Original machine-specific attachment and backup selections are intentionally private.
EXTRA_SCOPES = ()


class Snapshot(NamedTuple):
    source: Path
    relative: Path
    sha256: str
    size: int
    identity: tuple


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(data)
    return value.hexdigest()


def identity(path):
    info = Path(path).stat()
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def reject_link(path):
    info = Path(path).lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ArchiveError(f"SYMLINK_OR_REPARSE_POINT: {path}")


def reject_ancestor_links(path):
    for part in (Path(path), *Path(path).parents):
        if part.exists() or part.is_symlink():
            reject_link(part)


def inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError as error:
        raise ArchiveError(f"OUTSIDE_SELECTED_ROOT: {path}") from error


def selected_files(selection):
    """Walk one explicit root without following symlinks or Windows junctions."""
    root = selection.source
    reject_ancestor_links(root)
    if root.is_file():
        yield root, selection.relative
        return
    if not root.is_dir():
        raise ArchiveError(f"SOURCE_NOT_REGULAR: {root}")
    pending = [root]
    while pending:
        directory = pending.pop()
        reject_link(directory)
        inside(directory, root)
        for child in sorted(directory.iterdir(), key=lambda x: x.name.lower()):
            reject_link(child)
            inside(child, root)
            if child.is_dir():
                pending.append(child)
            elif stat.S_ISREG(child.stat().st_mode):
                yield child, selection.relative / child.relative_to(root)
            else:
                raise ArchiveError(f"SOURCE_NOT_REGULAR: {child}")


def inventory(selections):
    files = []
    targets = set()
    for selection in selections:
        if selection.relative.is_absolute() or ".." in selection.relative.parts:
            raise ArchiveError(f"INVALID_ARCHIVE_RELATIVE_PATH: {selection.relative}")
        for source, relative in selected_files(selection):
            key = str(relative).replace("\\", "/").lower()
            if key in targets:
                raise ArchiveError(f"DUPLICATE_ARCHIVE_TARGET: {relative}")
            targets.add(key)
            before = identity(source)
            measured = digest(source)
            if identity(source) != before:
                raise ArchiveError(f"SOURCE_CHANGED_DURING_HASH: {source}")
            if selection.expected_sha256 is not None and measured != selection.expected_sha256:
                raise ArchiveError(f"SOURCE_SHA_MISMATCH: {source}")
            files.append(Snapshot(source, relative, measured, before[0], before))
    return sorted(files, key=lambda x: str(x.relative).lower())


def copy_snapshot(item, destination):
    """Exclusive write, hash while copying, reread both ends, preserve any failure."""
    reject_link(item.source)
    if identity(item.source) != item.identity or digest(item.source) != item.sha256:
        raise ArchiveError(f"SOURCE_CHANGED_BEFORE_COPY: {item.source}")
    target = Path(destination) / item.relative
    inside(target, destination)
    # Existing parent directories must not redirect a write outside the archive.
    for parent in reversed(target.parents):
        if parent == Path(destination) or Path(destination) in parent.parents:
            if parent.exists():
                reject_link(parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    copied = hashlib.sha256()
    with item.source.open("rb") as source, target.open("xb") as output:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            output.write(data)
            copied.update(data)
    if (identity(item.source) != item.identity or digest(item.source) != item.sha256
            or copied.hexdigest() != item.sha256):
        raise ArchiveError(f"SOURCE_CHANGED_DURING_COPY: {item.source}")
    if target.stat().st_size != item.size or digest(target) != item.sha256:
        raise ArchiveError(f"COPY_SHA_MISMATCH: {target}")
    return {
        "original": str(item.source), "archive": str(target),
        "relative": item.relative.as_posix(), "bytes": item.size,
        "sha256": item.sha256, "original_mtime_ns": item.identity[1],
    }


def write_json_exclusive(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def archive_files(selections, destination, manifest_name, create_root):
    destination = Path(destination)
    reject_ancestor_links(destination)
    if create_root and destination.exists():
        raise ArchiveError(f"DESTINATION_EXISTS: {destination}")
    if not create_root:
        reject_link(destination)
        if (destination / manifest_name).exists():
            raise ArchiveError(f"MANIFEST_EXISTS: {manifest_name}")
    snapshots = inventory(selections)
    if not create_root:
        occupied = [str(x.relative) for x in snapshots if (destination / x.relative).exists()]
        if occupied:
            raise ArchiveError(f"ARCHIVE_TARGET_EXISTS: {occupied[0]}")
    if create_root:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(exist_ok=False)
    records = []
    try:
        for item in snapshots:
            records.append(copy_snapshot(item, destination))
        # Catch a late modification, new file, or deletion anywhere in a selected
        # tree, including a source already copied earlier in this phase.
        if inventory(selections) != snapshots:
            raise ArchiveError("SOURCE_SELECTION_CHANGED_DURING_ARCHIVE")
        manifest = {
            "schema": "m8.local_preservation.v1", "created_at_utc": timestamp(),
            "status": "VERIFIED_COPY", "source_bytes_unchanged": True,
            "runtime_portability": "NOT_CLAIMED_ABSOLUTE_SIGNED_PATHS_RETAINED",
            "files": records, "file_count": len(records),
            "bytes": sum(x["bytes"] for x in records),
        }
        write_json_exclusive(destination / manifest_name, manifest)
        return manifest
    except Exception as error:
        failure = destination / (manifest_name + ".incomplete.json")
        if not failure.exists():
            write_json_exclusive(failure, {"status": "INCOMPLETE", "error": str(error),
                                          "verified_files": records, "created_at_utc": timestamp()})
        raise


def verify_manifest(path, destination):
    reject_link(path)
    value = json.loads(Path(path).read_text("utf-8"))
    if value.get("status") != "VERIFIED_COPY":
        raise ArchiveError(f"INCOMPLETE_ARCHIVE: {path}")
    for record in value["files"]:
        target = Path(destination) / record["relative"]
        inside(target, destination)
        reject_link(target)
        if target.stat().st_size != record["bytes"] or digest(target) != record["sha256"]:
            raise ArchiveError(f"ARCHIVE_SHA_MISMATCH: {target}")
    return value


def artifact_selections():
    values = [Selection(TEMPORARY / n, Path("temporary") / n) for n in TEMP_NAMES]
    values.append(Selection(REPOSITORY / "test-artifacts/m8", Path("repository/test-artifacts/m8")))
    values.extend(Selection(REPOSITORY / "test-artifacts" / n, Path("repository/test-artifacts") / n)
                  for n in TEST_RECORDS)
    values.extend(Selection(REPOSITORY / n, Path("repository") / n) for n in REPRODUCTION_FILES)
    values.extend(Selection(TEMPORARY / n, Path("historical") / n, expected) for n, expected in HISTORICAL)
    values.append(Selection(TEMPORARY / ACCEPTED_FACE, Path("historical") / ACCEPTED_FACE))
    values.extend(EXTRA_SCOPES)
    return values


def document_selections():
    return [Selection(REPOSITORY / "docs", Path("repository/docs"))] + [
        Selection(REPOSITORY / n, Path("repository") / n) for n in FINAL_FILES]


def require_runner_stopped():
    # Failed historical reports remain valid evidence; only these latest scopes
    # must no longer be writing when the preservation phase starts.
    for name in ("bpm8-31b29f56", "bpm8-256678ca", "bpm8-a233eb61"):
        value = json.loads((TEMPORARY / name / "report.json").read_text("utf-8"))
        if value.get("status", "").lower() == "running":
            raise ArchiveError(f"RUNNER_STILL_RUNNING: {name}")


def release_record(destination, commit, tag):
    if len(commit or "") != 40 or any(c not in "0123456789abcdef" for c in commit.lower()):
        raise ArchiveError("EXACT_COMMIT_SHA_REQUIRED")
    if tag != "milestone-8":
        raise ArchiveError("EXPECTED_LOCAL_TAG_MILESTONE_8")
    record_path = destination / "release-record.json"
    if record_path.exists():
        raise ArchiveError(f"RELEASE_RECORD_EXISTS: {record_path}")
    def git(*args):
        return subprocess.run(["git", *args], cwd=REPOSITORY, check=True,
                              text=True, capture_output=True).stdout.strip()
    if git("rev-parse", "HEAD") != commit or git("rev-parse", "refs/tags/milestone-8^{commit}") != commit:
        raise ArchiveError("COMMIT_HEAD_TAG_MISMATCH")
    manifests = []
    for name in ("artifact-manifest.json", "documentation-manifest.json"):
        verify_manifest(destination / name, destination)
        manifests.append({"path": name, "sha256": digest(destination / name)})
    record = {"schema": "m8.external_release_record.v1", "created_at_utc": timestamp(),
              "commit": commit, "tag": tag, "published": False, "pushed": False,
              "manifests": manifests, "signed_paths_rewritten": False,
              "record_is_external_to_git_commit": True}
    bundle = destination / "blender-production-mcp-milestone8.bundle"
    if bundle.exists():
        reject_ancestor_links(bundle)
        before = identity(bundle)
        git("bundle", "verify", str(bundle))
        bundle_hash = digest(bundle)
        if identity(bundle) != before:
            raise ArchiveError("GIT_BUNDLE_CHANGED_DURING_VERIFICATION")
        record["git_bundle"] = {"path": str(bundle), "sha256": bundle_hash,
                                "bytes": before[0], "git_bundle_verify": "PASS"}
    write_json_exclusive(record_path, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("inventory", "artifacts", "docs", "release"), default="inventory")
    parser.add_argument("--commit")
    parser.add_argument("--tag", default="milestone-8")
    args = parser.parse_args()
    if args.phase == "inventory":
        files = inventory(artifact_selections())
        result = {"phase": "READ_ONLY_INVENTORY", "destination": str(DESTINATION),
                  "destination_exists": DESTINATION.exists(), "file_count": len(files),
                  "bytes": sum(x.size for x in files)}
    elif args.phase == "artifacts":
        require_runner_stopped()
        result = archive_files(artifact_selections(), DESTINATION, "artifact-manifest.json", True)
    elif args.phase == "docs":
        verify_manifest(DESTINATION / "artifact-manifest.json", DESTINATION)
        result = archive_files(document_selections(), DESTINATION, "documentation-manifest.json", False)
    else:
        result = release_record(DESTINATION, args.commit, args.tag)
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
