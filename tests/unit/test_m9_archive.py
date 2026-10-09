"""Archivo M9 sobre archivos/Git sintéticos; nunca toca evidencia ni vault reales."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "archive_m9_evidence", Path(__file__).resolve().parents[1] / "integration/archive_m9_evidence.py")
ARCHIVE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ARCHIVE
SPEC.loader.exec_module(ARCHIVE)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "repository"
    repo.mkdir()
    vault = tmp_path / "vault"
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(ARCHIVE, "REPOSITORY", repo)
    monkeypatch.setattr(ARCHIVE, "VAULT_ROOT", vault)
    monkeypatch.setattr(ARCHIVE, "TEMPORARY", temporary)
    source = repo / "test-artifacts/m9/test.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"signed_path":"C:/original/material/plan.json"}', encoding="utf-8")
    destination = vault / "M9-fixture"
    return repo, source, destination


def selections(source, relative="repository/test-artifacts/m9/test.json"):
    return [ARCHIVE.Selection(source, Path(relative))]


def test_selection_rejects_files_outside_allowlist_and_windows_aliases(setup, tmp_path):
    repo, source, _ = setup
    secret = repo / ".env"
    secret.write_text("private", encoding="utf-8")
    listing = tmp_path / "selection.json"
    listing.write_text(json.dumps([{"source": str(secret), "relative": "env"}]))
    with pytest.raises(ARCHIVE.ArchiveError, match="SOURCE_OUTSIDE_ALLOWLIST"):
        ARCHIVE.load_selection(listing)
    for relative in ("../escape", "C:/escape", "payload:stream", "NUL", "withdot.", "release-record.json"):
        listing.write_text(json.dumps([{"source": str(source), "relative": relative}]))
        with pytest.raises(ARCHIVE.ArchiveError, match="INVALID_ARCHIVE|RESERVED_ARCHIVE"):
            ARCHIVE.load_selection(listing)


def test_selected_temporary_logs_are_allowed_but_other_temporary_files_are_not(setup):
    allowed = ARCHIVE.TEMPORARY / "bpm9-fixture.stdout.txt"
    allowed.write_text("preserved failure")
    assert ARCHIVE.validate_source(allowed) == allowed
    other = ARCHIVE.TEMPORARY / "unrelated.txt"
    other.write_text("unrelated")
    with pytest.raises(ARCHIVE.ArchiveError, match="SOURCE_OUTSIDE_ALLOWLIST"):
        ARCHIVE.validate_source(other)


@pytest.mark.parametrize("relative", ["same.json", "same.json/child.json"])
def test_selection_conflict_creates_no_archive(setup, relative):
    _, source, destination = setup
    second = ARCHIVE.Selection(source, Path(relative))
    with pytest.raises(ARCHIVE.ArchiveError, match="DUPLICATE_ARCHIVE_TARGET|ARCHIVE_TARGET_PREFIX_CONFLICT"):
        ARCHIVE.archive_files(selections(source, "same.json") + [second], destination, "artifacts")
    assert not destination.exists()


def test_m9_manifest_retains_bytes_records_both_hashes_and_rejects_overwrite(setup):
    _, source, destination = setup
    manifest = ARCHIVE.archive_files(selections(source), destination, "artifacts")
    record = manifest["files"][0]
    assert manifest["schema"] == "m9.local_preservation.v1"
    assert record["original_sha256"] == record["copy_sha256"] == ARCHIVE.digest(source)
    assert Path(record["archive"]).read_bytes() == source.read_bytes()
    assert json.loads(Path(record["archive"]).read_bytes())["signed_path"] == "C:/original/material/plan.json"
    with pytest.raises(ARCHIVE.ArchiveError, match="DESTINATION_EXISTS"):
        ARCHIVE.archive_files(selections(source), destination, "artifacts")
    assert ARCHIVE.verify_manifest(destination / "artifact-manifest.json", destination, originals=True)["file_count"] == 1


def test_failure_preserves_copied_bytes_and_records_incomplete_m9(setup, monkeypatch):
    _, source, destination = setup
    original = ARCHIVE.copy_snapshot

    def failure(item, target):
        original(item, target)
        raise ARCHIVE.ArchiveError("DELIBERATE_AFTER_COPY_FAILURE")

    monkeypatch.setattr(ARCHIVE, "copy_snapshot", failure)
    with pytest.raises(ARCHIVE.ArchiveError, match="DELIBERATE"):
        ARCHIVE.archive_files(selections(source), destination, "artifacts")
    assert (destination / "repository/test-artifacts/m9/test.json").read_bytes() == source.read_bytes()
    assert not (destination / "artifact-manifest.json").exists()
    failed = json.loads((destination / "artifact-manifest.json.incomplete.json").read_text())
    assert failed["status"] == "INCOMPLETE"
    assert failed["schema"] == "m9.local_preservation.v1"


def test_final_verification_rejects_modified_original_and_m8_schema(setup):
    _, source, destination = setup
    ARCHIVE.archive_files(selections(source), destination, "artifacts")
    manifest_path = destination / "artifact-manifest.json"
    source.write_text("source changed")
    with pytest.raises(ARCHIVE.ArchiveError, match="ORIGINAL_CHANGED_AFTER_ARCHIVE"):
        ARCHIVE.verify_manifest(manifest_path, destination, originals=True)
    manifest = json.loads(manifest_path.read_text())
    manifest["schema"] = "m8.local_preservation.v1"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ARCHIVE.ArchiveError, match="EXPECTED_M9_MANIFEST_SCHEMA"):
        ARCHIVE.verify_manifest(manifest_path, destination)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


def prepare_release(setup):
    repo, source, destination = setup
    git(repo, "init", "--quiet")
    readme = repo / "README.md"
    readme.write_text("synthetic M9 fixture")
    git(repo, "add", "README.md")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture")
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "milestone-9")
    ARCHIVE.archive_files(selections(source), destination, "artifacts")
    ARCHIVE.archive_files(selections(readme, "repository/README.md"), destination, "docs")
    return repo, destination, commit


def test_release_requires_exact_head_tag_and_bundle(setup):
    repo, destination, commit = prepare_release(setup)
    with pytest.raises(ARCHIVE.ArchiveError, match="EXACT_COMMIT_SHA_REQUIRED"):
        ARCHIVE.release_record(destination, commit[:10])
    with pytest.raises(ARCHIVE.ArchiveError, match="COMMIT_HEAD_TAG_MISMATCH"):
        ARCHIVE.release_record(destination, "0" * 40)
    with pytest.raises(ARCHIVE.ArchiveError, match="M9_GIT_BUNDLE_REQUIRED"):
        ARCHIVE.release_record(destination, commit)
    git(repo, "bundle", "create", str(destination / ARCHIVE.BUNDLE), "--all")
    released = ARCHIVE.release_record(destination, commit)
    assert released["schema"] == "m9.external_release_record.v1"
    assert released["commit"] == commit
    assert released["git_bundle"]["release_refs_verified"]
    assert released["record_is_external_to_git_commit"]
    assert not released["pushed"]
    with pytest.raises(ARCHIVE.ArchiveError, match="RELEASE_RECORD_EXISTS"):
        ARCHIVE.release_record(destination, commit)


def test_release_rejects_bundle_without_m9_tag(setup):
    repo, destination, commit = prepare_release(setup)
    git(repo, "bundle", "create", str(destination / ARCHIVE.BUNDLE), "HEAD")
    with pytest.raises(ARCHIVE.ArchiveError, match="GIT_BUNDLE_RELEASE_REFS_MISMATCH"):
        ARCHIVE.release_record(destination, commit)
    assert not (destination / "release-record.json").exists()


def test_release_rejects_incremental_bundle_even_when_local_git_can_verify_it(setup):
    repo, destination, first = prepare_release(setup)
    (repo / "extra.txt").write_text("second synthetic commit")
    git(repo, "add", "extra.txt")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "second")
    git(repo, "tag", "--force", "milestone-9")
    commit = git(repo, "rev-parse", "HEAD")
    bundle = destination / ARCHIVE.BUNDLE
    git(repo, "bundle", "create", str(bundle), "--all", "^" + first)
    git(repo, "bundle", "verify", str(bundle))
    with pytest.raises(ARCHIVE.ArchiveError, match="GIT_BUNDLE_REQUIRES_EXTERNAL_PREREQUISITES"):
        ARCHIVE.release_record(destination, commit)
    assert not (destination / "release-record.json").exists()
