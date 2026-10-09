import json
import pytest
from blender_mcp.production.common.bridge import Bridge, build_code, loopback_address
from blender_mcp.production.common.storage import Storage, sha256, write_json
from blender_mcp.production.common.lease import ControllerLease
from blender_mcp.safe_mode import SandboxViolation


def test_script_parameters_cannot_inject_code():
    code = build_code('result = params', {'name': '\"); import os; os.remove("x") #'})
    scope = {}
    exec(code, scope)
    assert scope['result']['name'].startswith('\"); import os')


def test_unsafe_body_rejected_even_without_environment_opt_in():
    with pytest.raises(SandboxViolation):
        build_code('import os\nresult = {}')


@pytest.mark.parametrize('host', ['0.0.0.0', '192.168.1.2', 'example.com'])
def test_non_loopback_rejected(host):
    with pytest.raises(ValueError):
        loopback_address(host)


def test_unique_artifact_directories(tmp_path):
    storage = Storage(tmp_path)
    assert storage.reserve('qa','same') != storage.reserve('qa','same')


def test_checkpoint_integrity_and_path_scope(tmp_path):
    storage = Storage(tmp_path)
    folder = storage.reserve('checkpoints','test')
    scene = folder/'scene.blend'
    scene.write_bytes(b'BLENDER-fixture')
    manifest = folder/'manifest.json'
    write_json(manifest, {'filename':'scene.blend','sha256':sha256(scene)})
    assert storage.checkpoint_manifest(str(manifest))[0] == scene
    scene.write_bytes(b'tampered')
    with pytest.raises(ValueError,match='integrity'):
        storage.checkpoint_manifest(str(manifest))
    foreign = tmp_path/'foreign.json'
    foreign.write_text('{}')
    with pytest.raises(ValueError,match='Only manifests'):
        storage.checkpoint_manifest(str(foreign))


def test_second_controller_refused():
    with ControllerLease(19876):
        with pytest.raises(RuntimeError, match='Another production'):
            with ControllerLease(19876):
                pass
    with ControllerLease(19876):
        pass


class TransportDouble:
    """Reconnects like upstream without opening sockets or running commands."""

    def __init__(self):
        from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION
        self.protocol = EXPECTED_ADDON_PROTOCOL_VERSION
        self.sock = None
        self.calls = []
        self.disconnects = 0
        self.fail_command = None

    def send_command(self, name, params=None):
        self.calls.append(name)
        if self.sock is None:
            self.sock = object()
        if name == self.fail_command:
            self.fail_command = None
            raise ConnectionError("synthetic uncertain transport result")
        if name == "get_addon_info":
            return {"protocol_version": self.protocol}
        return {"received": name}

    def disconnect(self):
        self.disconnects += 1
        self.sock = None


@pytest.fixture
def mocked_bridge(monkeypatch):
    from blender_mcp.production.common import transport as production_transport
    transport = TransportDouble()
    # M3.1: the bridge uses the production subclass of the upstream connection.
    monkeypatch.setattr(production_transport, "ProductionConnection", lambda host, port: transport)
    return Bridge(host="127.0.0.1", port=19878), transport


def test_live_connection_reuses_verified_handshake(mocked_bridge):
    bridge, transport = mocked_bridge
    bridge.command("get_scene_info")
    bridge.command("get_scene_info")
    assert transport.calls == ["get_addon_info", "get_scene_info", "get_scene_info"]


def test_missing_socket_requires_new_handshake_before_next_command(mocked_bridge):
    bridge, transport = mocked_bridge
    bridge.command("get_scene_info")
    transport.sock = None  # Blender was closed/restarted after a valid handshake.
    bridge.command("get_scene_info")
    assert transport.calls == ["get_addon_info", "get_scene_info", "get_addon_info", "get_scene_info"]


def test_uncertain_command_is_not_replayed_and_next_call_rehandshakes(mocked_bridge):
    bridge, transport = mocked_bridge
    transport.fail_command = "execute_code"
    with pytest.raises(ConnectionError, match="uncertain"):
        bridge.command("execute_code", {"code": "mutation payload placeholder"})
    assert transport.calls == ["get_addon_info", "execute_code"]
    assert transport.disconnects == 1 and transport.sock is None
    assert bridge._handshake is None
    bridge.command("get_scene_info")
    assert transport.calls == ["get_addon_info", "execute_code", "get_addon_info", "get_scene_info"]


def test_incompatible_reconnected_addon_remains_blocked_on_repeated_calls(mocked_bridge):
    bridge, transport = mocked_bridge
    bridge.command("get_scene_info")
    transport.sock = None
    transport.protocol = "incompatible-test-protocol"
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Addon protocol"):
            bridge.command("execute_code", {"code": "must never reach addon"})
    assert "execute_code" not in transport.calls
    assert transport.calls.count("get_addon_info") == 3


def test_failed_reconnect_handshake_cannot_retain_previous_trust(mocked_bridge):
    bridge, transport = mocked_bridge
    bridge.command("get_scene_info")
    transport.sock = None
    transport.fail_command = "get_addon_info"
    with pytest.raises(ConnectionError, match="uncertain"):
        bridge.command("execute_code")
    transport.protocol = "incompatible-test-protocol"
    with pytest.raises(RuntimeError, match="Addon protocol"):
        bridge.command("execute_code")
    assert "execute_code" not in transport.calls
