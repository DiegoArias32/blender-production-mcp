"""Small adapter around the unmodified upstream transport and AST validator."""
from __future__ import annotations

import json
import os
import socket
import ipaddress
import logging
import threading

MARKER = "__PRODUCTION_RESULT__="


def compose_code(body: str, params: dict | None = None) -> str:
    from .payload import link_payload
    encoded = json.dumps(params or {}, allow_nan=False, separators=(",", ":"))
    # Only functions reachable from the entry statements travel to Blender (see payload.py).
    body = link_payload(body)
    return f"import json\nparams = json.loads({encoded!r})\n{body}\nprint({MARKER!r} + json.dumps(result, allow_nan=False))"


def build_code(body: str, params: dict | None = None) -> str:
    """Parameters are data, never executable string interpolation."""
    from blender_mcp.safe_mode import validate_code
    code = compose_code(body, params)
    validate_code(code)
    return code


def payload_bytes(body: str, params: dict | None = None) -> int:
    """Size the upstream safe-mode validator will measure (its limit is MAX_CODE_BYTES)."""
    return len(compose_code(body, params).encode("utf-8", errors="replace"))


def loopback_address(host: str) -> str:
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Only localhost/loopback Blender connections are allowed")
    addresses = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_loopback for a in addresses):
        raise ValueError("Blender host must resolve exclusively to IPv4 loopback")
    return addresses[0][4][0]


class Bridge:
    def __init__(self, host: str | None = None, port: int | None = None):
        os.environ["DISABLE_TELEMETRY"] = "true"
        os.environ["BLENDER_MCP_SAFE_MODE"] = "1"
        from .transport import ProductionConnection
        logging.getLogger("BlenderMCPServer").setLevel(logging.WARNING)
        self.host = loopback_address(host or os.environ.get("BLENDER_HOST", "localhost"))
        self.port = int(port or os.environ.get("BLENDER_PORT", "9876"))
        if not 1024 <= self.port <= 65535:
            raise ValueError("Blender port must be between 1024 and 65535")
        # Same upstream wire protocol, linear-time receive and classified transport failures.
        self.connection = ProductionConnection(self.host, self.port)
        self.lock = threading.RLock()
        self._handshake = None

    def command(self, name: str, params: dict | None = None):
        with self.lock:
            if self._handshake is None or self.connection.sock is None:
                from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION
                self._handshake = None
                # Fail closed for mismatched addons, without auto-install/update side effects.
                try:
                    info = self.connection.send_command("get_addon_info")
                    protocol = info.get("protocol_version")
                    if protocol != EXPECTED_ADDON_PROTOCOL_VERSION:
                        raise RuntimeError(f"Addon protocol {protocol}; expected {EXPECTED_ADDON_PROTOCOL_VERSION}")
                except Exception:
                    self.connection.disconnect()
                    raise
                self._handshake = info
            from .transport import BlenderExecutionError
            try:
                return self.connection.send_command(name, params)
            except BlenderExecutionError:
                raise  # Blender answered: the outcome is a known failure, the socket is healthy.
            except Exception:
                self._handshake = None
                self.connection.disconnect()
                raise  # Never replay a command with an uncertain result.

    def run(self, body: str, params: dict | None = None) -> dict:
        response = self.command("execute_code", {"code": build_code(body, params)})
        if response.get("error"):
            raise RuntimeError(response["error"])
        output = response.get("result", "")
        matches = [line[len(MARKER):] for line in output.splitlines() if line.startswith(MARKER)]
        if len(matches) != 1:
            raise RuntimeError(f"Blender returned no unique production result: {str(response)[:2000]}")
        result = json.loads(matches[0])
        if not isinstance(result, dict):
            raise RuntimeError("Production result must be an object")
        return result

    def close(self):
        self.connection.disconnect()
