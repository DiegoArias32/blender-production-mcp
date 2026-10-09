"""Local preservation safety using tiny synthetic files, never real M8 evidence."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "archive_m8_evidence", Path(__file__).resolve().parents[1] / "integration/archive_m8_evidence.py")
ARCHIVE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ARCHIVE
SPEC.loader.exec_module(ARCHIVE)


def selection(tmp_path, content=b'{"signed_path":"C:/original/source.blend"}'):
    source = tmp_path / "source"
    source.mkdir()
    item = source / "plan.json"
    item.write_bytes(content)
    return ARCHIVE.Selection(source, Path("temporary/owned")), item


def test_existing_archive_is_rejected_before_any_copy_or_overwrite(tmp_path):
    selected, source = selection(tmp_path)
    dest = tmp_path / "archive"
    dest.mkdir()
    marker = dest / "report.json"
    marker.write_bytes(b"previous evidence")
    before = source.read_bytes()
    with pytest.raises(ARCHIVE.ArchiveError, match="DESTINATION_EXISTS"):
        ARCHIVE.archive_files([selected], dest, "artifact-manifest.json", True)
    assert marker.read_bytes() == b"previous evidence"
    assert source.read_bytes() == before
    assert list(dest.iterdir()) == [marker]


def test_verified_copy_retains_signed_paths_and_maps_every_file(tmp_path):
    selected, source = selection(tmp_path)
    dest = tmp_path / "archive"
    manifest = ARCHIVE.archive_files([selected], dest, "artifact-manifest.json", True)
    copied = dest / "temporary/owned/plan.json"
    assert copied.read_bytes() == source.read_bytes()
    assert json.loads(copied.read_bytes())["signed_path"] == "C:/original/source.blend"
    assert manifest["files"][0]["original"] == str(source)
    assert manifest["files"][0]["archive"] == str(copied)
    assert manifest["files"][0]["sha256"] == ARCHIVE.digest(source)
    assert manifest["runtime_portability"] == "NOT_CLAIMED_ABSOLUTE_SIGNED_PATHS_RETAINED"
    assert ARCHIVE.verify_manifest(dest / "artifact-manifest.json", dest)["file_count"] == 1


def test_expected_source_sha_mismatch_creates_no_archive(tmp_path):
    selected, source = selection(tmp_path)
    exact_file = ARCHIVE.Selection(source, Path("historical/source.json"), "0" * 64)
    dest = tmp_path / "archive"
    with pytest.raises(ARCHIVE.ArchiveError, match="SOURCE_SHA_MISMATCH"):
        ARCHIVE.archive_files([exact_file], dest, "artifact-manifest.json", True)
    assert not dest.exists()


def test_source_changed_after_inventory_rejects_copy_without_target(tmp_path):
    selected, source = selection(tmp_path)
    snapshot = ARCHIVE.inventory([selected])[0]
    source.write_bytes(b"different source")
    dest = tmp_path / "archive"
    dest.mkdir()
    with pytest.raises(ARCHIVE.ArchiveError, match="SOURCE_CHANGED_BEFORE_COPY"):
        ARCHIVE.copy_snapshot(snapshot, dest)
    assert list(dest.iterdir()) == []


def test_source_changed_during_copy_is_not_given_complete_manifest(tmp_path, monkeypatch):
    selected, source = selection(tmp_path)
    dest = tmp_path / "archive"
    real_digest = ARCHIVE.digest

    def changed_digest(path):
        if Path(path) == source and (dest / "temporary/owned/plan.json").exists():
            source.write_bytes(b"source changed while preservation ran")
        return real_digest(path)

    monkeypatch.setattr(ARCHIVE, "digest", changed_digest)
    with pytest.raises(ARCHIVE.ArchiveError, match="SOURCE_CHANGED_DURING_COPY"):
        ARCHIVE.archive_files([selected], dest, "artifact-manifest.json", True)
    assert not (dest / "artifact-manifest.json").exists()
    assert json.loads((dest / "artifact-manifest.json.incomplete.json").read_text())["status"] == "INCOMPLETE"


def test_destination_checksum_corruption_is_rejected_and_never_overwritten(tmp_path, monkeypatch):
    selected, source = selection(tmp_path)
    snapshot = ARCHIVE.inventory([selected])[0]
    dest = tmp_path / "archive"
    dest.mkdir()
    target = dest / snapshot.relative
    real_digest = ARCHIVE.digest
    monkeypatch.setattr(ARCHIVE, "digest", lambda p: "0" * 64 if Path(p) == target else real_digest(p))
    with pytest.raises(ARCHIVE.ArchiveError, match="COPY_SHA_MISMATCH"):
        ARCHIVE.copy_snapshot(snapshot, dest)
    assert target.read_bytes() == source.read_bytes()
    monkeypatch.setattr(ARCHIVE, "digest", real_digest)
    target.write_bytes(b"preserve existing destination")
    with pytest.raises(FileExistsError):
        ARCHIVE.copy_snapshot(snapshot, dest)
    assert target.read_bytes() == b"preserve existing destination"


def test_outside_symlink_never_recurse_or_copy(tmp_path):
    selected, _ = selection(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "external.txt"
    marker.write_bytes(b"outside the selected root")
    link = selected.source / "redirect"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # Junctions do not require Developer Mode or elevated symlink privileges.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, text=True)
        if result.returncode:
            pytest.skip("Neither symlink nor junction creation is available")
    try:
        with pytest.raises(ARCHIVE.ArchiveError, match="SYMLINK_OR_REPARSE_POINT"):
            ARCHIVE.inventory([selected])
        assert marker.read_bytes() == b"outside the selected root"
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            # Remove only the synthetic junction, never recurse into its target.
            link.rmdir()


def test_archived_tamper_is_detected_before_next_phase(tmp_path):
    selected, _ = selection(tmp_path)
    dest = tmp_path / "archive"
    ARCHIVE.archive_files([selected], dest, "artifact-manifest.json", True)
    (dest / "temporary/owned/plan.json").write_bytes(b"tampered copy")
    with pytest.raises(ARCHIVE.ArchiveError, match="ARCHIVE_SHA_MISMATCH"):
        ARCHIVE.verify_manifest(dest / "artifact-manifest.json", dest)


def test_documentation_is_separate_exclusive_phase(tmp_path):
    selected, _ = selection(tmp_path)
    dest = tmp_path / "archive"
    ARCHIVE.archive_files([selected], dest, "artifact-manifest.json", True)
    docs = tmp_path / "final-docs"
    docs.mkdir()
    (docs / "evidence.json").write_bytes(b'{"commit":null}')
    final = ARCHIVE.Selection(docs, Path("repository/docs"))
    ARCHIVE.archive_files([final], dest, "documentation-manifest.json", False)
    assert (dest / "repository/docs/evidence.json").read_bytes() == b'{"commit":null}'
    with pytest.raises(ARCHIVE.ArchiveError, match="MANIFEST_EXISTS"):
        ARCHIVE.archive_files([final], dest, "documentation-manifest.json", False)


def test_relative_traversal_and_case_duplicate_targets_are_rejected(tmp_path):
    selected, source = selection(tmp_path)
    with pytest.raises(ARCHIVE.ArchiveError, match="INVALID_ARCHIVE_RELATIVE_PATH"):
        ARCHIVE.inventory([ARCHIVE.Selection(source, Path("../escape.json"))])
    with pytest.raises(ARCHIVE.ArchiveError, match="DUPLICATE_ARCHIVE_TARGET"):
        ARCHIVE.inventory([ARCHIVE.Selection(source, Path("same.json")),
                           ARCHIVE.Selection(source, Path("SAME.JSON"))])
