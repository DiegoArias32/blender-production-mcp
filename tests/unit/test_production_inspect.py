"""Inspection requests fail locally on bad inputs and honor the upstream guard."""

import pytest

from blender_mcp.production.inspect import (
    ARMATURE_SCRIPT,
    SCENE_SCRIPT,
    inspect_armature,
    scene_audit,
    snapshot,
)
from blender_mcp.production.rig import RIG_SCRIPT, rig_analyze
from blender_mcp.safe_mode import validate_code


class RecordingBridge:
    def __init__(self):
        self.calls = []

    def run(self, script, params):
        self.calls.append((script, params))
        return {"received": params}


@pytest.mark.parametrize("script", [SCENE_SCRIPT, ARMATURE_SCRIPT, RIG_SCRIPT])
def test_all_inspection_payloads_clear_upstream_safe_mode(script):
    validate_code("params = {}\n" + script)


@pytest.mark.parametrize("limit,offset", [(0, 0), (501, 0), (True, 0), (1, -1), (2.5, 0), (1, False)])
@pytest.mark.parametrize("function", [inspect_armature, rig_analyze])
def test_invalid_pagination_never_reaches_blender(function, limit, offset):
    bridge = RecordingBridge()
    with pytest.raises(ValueError):
        function(bridge, "Rig", limit, offset)
    assert not bridge.calls


@pytest.mark.parametrize("function", [inspect_armature, rig_analyze])
@pytest.mark.parametrize("name", ["", "  ", None, 8])
def test_missing_armature_name_fails_before_socket(function, name):
    bridge = RecordingBridge()
    with pytest.raises(ValueError):
        function(bridge, name)
    assert not bridge.calls


def test_scene_exact_name_filter_and_pagination_are_preserved():
    bridge = RecordingBridge()
    result = scene_audit(bridge, 'Mesh "Quoted" é', 10, 20)
    assert result["received"] == {"object_name": 'Mesh "Quoted" é', "limit": 10, "offset": 20,
                                   "detail": "summary"}
    # Names travel as data; never interpolate them into executable code.
    assert 'Mesh "Quoted" é' not in bridge.calls[0][0]


def test_internal_snapshot_is_complete_and_distinct_from_public_page():
    bridge = RecordingBridge()
    snapshot(bridge)
    assert bridge.calls[0][1] == {"full": True}


@pytest.mark.parametrize("detail", ["summary", "full"])
def test_scene_audit_preserves_requested_detail_level(detail):
    bridge = RecordingBridge()
    scene_audit(bridge, detail=detail)
    assert bridge.calls[0][1]["detail"] == detail
    assert "full" not in bridge.calls[0][1]  # Detail must not disable public pagination.


@pytest.mark.parametrize("detail", ["verbose", "", "FULL", None, [], 1])
def test_invalid_detail_never_reaches_blender(detail):
    bridge = RecordingBridge()
    with pytest.raises(ValueError, match="detail"):
        scene_audit(bridge, detail=detail)
    assert not bridge.calls


@pytest.mark.parametrize("name", ["", "   ", 4])
def test_scene_rejects_invalid_optional_name(name):
    bridge = RecordingBridge()
    with pytest.raises(ValueError):
        scene_audit(bridge, name)
    assert not bridge.calls
