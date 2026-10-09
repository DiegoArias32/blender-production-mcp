"""Accepted-generation freshness uses independent retained source evidence.

No lifecycle function is mocked. A small known-context bridge supplies the
three existing read RPCs and validates their real Blender payloads first.
Actual RNA/Action restoration remains covered by the controlled Blender probe.
"""
from copy import deepcopy
import ast

import pytest

from blender_mcp.production.animation.generation import service
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.digest import m2_digest


class KnownContextBridge:
    def __init__(self, source_name="Original"):
        self.actions = {"Accepted": {"keys": [[1, 0], [31, 1]], "lifecycle": "ACCEPTED"}}
        if source_name is not None:
            self.actions[source_name] = {"keys": [[1, 0], [31, 0]], "immutable": True}
        self.active = "Accepted"
        self.rig_fingerprint = "retained-rig-assets-units-fps"
        self.filepath = "C:/controlled/m6-fixture.blend"
        self.stamps = ['{"role":"FIXTURE","asset_id":"controlled"}']
        self.supported = True
        self.calls = []

    def run(self, body, params):
        code = build_code(body, params)
        # The accepted-read path must not assign an Action to take its snapshot.
        calls = [node for node in ast.walk(ast.parse(code)) if isinstance(node, ast.Call)]
        assert not any(isinstance(call.func, ast.Name) and call.func.id in {"m3_assign", "m3_restore"}
                       for call in calls)
        self.calls.append(deepcopy(params))
        if set(params) == {"object_name", "options"}:
            return {"supported": self.supported, "rig_fingerprint": self.rig_fingerprint,
                    "source_action": self.active, "source_fingerprint": m2_digest(self.actions.get(self.active)),
                    "filepath": self.filepath, "stamps": deepcopy(self.stamps)}
        if set(params) == {"source_action"}:
            name = params["source_action"]
            if name is not None and name not in self.actions:
                raise ValueError("STALE_PROPOSAL: original source Action missing after acceptance")
            return {"source_action": name, "source_fingerprint": m2_digest(self.actions.get(name))}
        if set(params) == {"object_name", "action_name"}:
            name = params["action_name"]
            if name not in self.actions:
                raise ValueError("CANDIDATE_MISSING: generated Action absent")
            return {"action": name, "fingerprint": m2_digest(self.actions[name]), "summary": {}}
        raise AssertionError("Unexpected read RPC: " + repr(params))


def context(source_name="Original"):
    bridge = KnownContextBridge(source_name)
    plan = {"object_name": "Fixture", "options": {}, "source_action": source_name,
            "source_fingerprint": m2_digest(bridge.actions.get(source_name)),
            "rig_fingerprint": bridge.rig_fingerprint, "filepath": bridge.filepath, "stamps": deepcopy(bridge.stamps)}
    state = {"state": "ACCEPTED", "candidate_action": "Accepted",
             "candidate_fingerprint": m2_digest(bridge.actions["Accepted"])}
    return bridge, plan, state


@pytest.mark.parametrize("source_name", ["Original", None])
@pytest.mark.parametrize("active_candidate", [True, False])
def test_accepted_fresh_reads_original_independently_without_assignment(source_name, active_candidate):
    bridge, plan, state = context(source_name)
    bridge.active = "Accepted" if active_candidate else source_name
    active_before = bridge.active
    result = service.fresh(bridge, plan, state)
    assert result["source_action"] == source_name
    assert result["source_fingerprint"] == plan["source_fingerprint"]
    assert {"source_action": source_name} in bridge.calls
    assert {"object_name": "Fixture", "action_name": "Accepted"} in bridge.calls
    assert bridge.active == active_before


def test_accepted_fresh_still_rejects_changed_immutable_original():
    bridge, plan, state = context()
    bridge.actions["Original"]["keys"][1][1] = 2
    with pytest.raises(ValueError, match="STALE_PROPOSAL: rig/source Action/file/asset"):
        service.fresh(bridge, plan, state)


@pytest.mark.parametrize("source_name", ["Original", None])
def test_accepted_fresh_still_rejects_changed_candidate(source_name):
    bridge, plan, state = context(source_name)
    bridge.actions["Accepted"]["keys"][1][1] = 2
    with pytest.raises(ValueError, match="STALE_PROPOSAL: candidate content"):
        service.fresh(bridge, plan, state)


@pytest.mark.parametrize("field,value", [("rig_fingerprint", "changed-rig-or-assets"),
                                         ("filepath", "C:/controlled/other-fixture.blend"),
                                         ("stamps", ['{"role":"MASTER"}']), ("supported", False)])
def test_accepted_fresh_still_rejects_changed_rig_file_or_asset_context(field, value):
    bridge, plan, state = context()
    setattr(bridge, field, value)
    with pytest.raises(ValueError, match="STALE_PROPOSAL: rig/source Action/file/asset"):
        service.fresh(bridge, plan, state)


@pytest.mark.parametrize("source_name", ["Original", None])
def test_accepted_fresh_rejects_unrelated_active_action(source_name):
    bridge, plan, state = context(source_name)
    bridge.actions["Unrelated"] = {"keys": [[1, 50]]}
    bridge.active = "Unrelated"
    with pytest.raises(ValueError, match="STALE_PROPOSAL: unrelated active Action"):
        service.fresh(bridge, plan, state)
    assert len(bridge.calls) == 1


def test_accepted_fresh_rejects_missing_named_original():
    bridge, plan, state = context()
    del bridge.actions["Original"]
    with pytest.raises(ValueError, match="STALE_PROPOSAL: original source Action missing"):
        service.fresh(bridge, plan, state)


@pytest.mark.parametrize("source_name", ["Original", None])
def test_nonterminal_generation_still_requires_original_active_action(source_name):
    bridge, plan, state = context(source_name)
    state["state"] = "GENERATED_CANDIDATE"
    with pytest.raises(ValueError, match="STALE_PROPOSAL: rig/source Action/file/asset"):
        service.fresh(bridge, plan, state)
    assert not any(set(call) == {"source_action"} for call in bridge.calls)


def test_accepted_fresh_rejects_missing_candidate():
    bridge, plan, state = context()
    del bridge.actions["Accepted"]
    bridge.active = "Original"
    with pytest.raises(ValueError, match="CANDIDATE_MISSING"):
        service.fresh(bridge, plan, state)
