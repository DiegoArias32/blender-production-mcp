"""Production-layer socket client (M3.1, audit AUD-009/AUD-010).

The upstream ``BlenderConnection.receive_full_response`` retries ``json.loads`` on the whole buffer
after every 8 KB chunk, which is quadratic in the response size (measured by the audit: 1 MB 0.94 s,
4.2 MB 18.5 s). This subclass keeps the same wire protocol (one JSON object per command, no framing)
but detects the end of the object with an incremental brace/string scanner and parses once.

It also classifies failures, because a retry decision depends on it:

- ``BlenderTimeoutError``: the command was sent and no complete answer arrived. Blender may still
  execute or have executed it. Never replay automatically.
- ``BlenderConnectionError``: connection lost. If it happened after sending, the outcome is uncertain.
- ``BlenderExecutionError``: Blender answered with an error. The command ran (and its own ``finally``
  blocks ran); the outcome is known to be a failure.

The upstream module is imported, never modified.
"""
from __future__ import annotations

import json
import logging
import socket

from blender_mcp.server import BlenderConnection

logger = logging.getLogger("BlenderProductionTransport")
TIMEOUT_SECONDS = 180.0


class BlenderTransportError(Exception):
    uncertain = True


class BlenderTimeoutError(BlenderTransportError):
    pass


class BlenderConnectionError(BlenderTransportError):
    pass


class BlenderExecutionError(Exception):
    uncertain = False


class JsonObjectScanner:
    """Finds the end of one top-level JSON object in a byte stream in O(n) total."""

    def __init__(self):
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.started = False
        self.position = 0

    def feed(self, buffer: bytearray) -> int | None:
        """Scan new bytes; return the end offset (exclusive) once the object closes."""
        index = self.position
        length = len(buffer)
        while index < length:
            byte = buffer[index]
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif byte == 0x5C:  # backslash
                    self.escaped = True
                elif byte == 0x22:  # quote
                    self.in_string = False
            elif byte == 0x22:
                self.in_string = True
            elif byte in (0x7B, 0x5B):  # { [
                self.depth += 1
                self.started = True
            elif byte in (0x7D, 0x5D):  # } ]
                self.depth -= 1
                if self.started and self.depth == 0:
                    self.position = index + 1
                    return index + 1
            index += 1
        self.position = index
        return None


class ProductionConnection(BlenderConnection):
    def receive_full_response(self, sock, buffer_size=65536):
        sock.settimeout(TIMEOUT_SECONDS)
        buffer = bytearray()
        scanner = JsonObjectScanner()
        while True:
            try:
                chunk = sock.recv(buffer_size)
            except socket.timeout as exc:
                raise BlenderTimeoutError("TRANSPORT_TIMEOUT: no complete answer from Blender within "
                                          + str(TIMEOUT_SECONDS) + " s; the command may still run") from exc
            if not chunk:
                raise BlenderConnectionError("TRANSPORT_CONNECTION_LOST: Blender closed the connection before a complete answer")
            buffer.extend(chunk)
            end = scanner.feed(buffer)
            if end is not None:
                return bytes(buffer[:end])

    def send_command(self, command_type, params=None):
        with self._lock:
            if not self.sock and not self.connect():
                raise BlenderConnectionError("TRANSPORT_NOT_CONNECTED: cannot connect to Blender (command not sent)")
            sent = False
            try:
                self.sock.sendall(json.dumps({"type": command_type, "params": params or {}}).encode("utf-8"))
                sent = True
                response = json.loads(self.receive_full_response(self.sock).decode("utf-8"))
            except BlenderTransportError:
                self.sock = None
                raise
            except (ConnectionError, OSError) as exc:
                self.sock = None
                if sent:
                    raise BlenderConnectionError("TRANSPORT_CONNECTION_LOST: " + str(exc) + " (after sending; outcome uncertain)") from exc
                raise BlenderConnectionError("TRANSPORT_NOT_SENT: " + str(exc)) from exc
            except json.JSONDecodeError as exc:
                self.sock = None
                raise BlenderConnectionError("TRANSPORT_INVALID_RESPONSE: " + str(exc)) from exc
            if response.get("status") == "error":
                raise BlenderExecutionError(response.get("message", "Unknown error from Blender"))
            return response.get("result", {})
