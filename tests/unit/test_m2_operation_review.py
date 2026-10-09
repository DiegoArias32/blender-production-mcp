"""Regression: persistence failure must not disguise a completed mutation."""
import pytest

from blender_mcp.production.animation import corrections


@pytest.mark.parametrize("failure", [OSError("disk full"), PermissionError("write denied")])
def test_result_log_failure_retains_verified_candidate_and_recovery(monkeypatch, failure):
    def fail_record(*args):
        raise failure
    monkeypatch.setattr(corrections, "record_event", fail_record)
    data = {"status": "warning", "working_action": "Duplicate", "checkpoint": "checkpoint/manifest.json",
            "numeric_verification": "pass", "source_intact": True, "warnings": []}
    result = corrections._record_result("proposal.json", "apply", data)
    assert result["numeric_verification"] == "pass"
    assert result["working_action"] == "Duplicate"
    assert result["checkpoint"] == "checkpoint/manifest.json"
    assert result["source_intact"] is True
    assert result["event_recorded"] is False
    assert str(failure) in result["warnings"][-1]
