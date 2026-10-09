"""Verify the installed package and MCP handshake without connecting to Blender."""
from __future__ import annotations

import asyncio
from importlib.metadata import version
import json
import os
import sys
import tempfile
import socket

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def verify():
    # Reserve an unused loopback port for an isolated controller lease. No addon
    # is contacted: this check must coexist with a user's active MCP client.
    with tempfile.TemporaryDirectory(prefix="blender-production-install-") as directory, socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        environment = dict(os.environ)
        environment.update(BLENDER_HOST="localhost", BLENDER_PORT=str(port),
                           BLENDER_MCP_SAFE_MODE="1", DISABLE_TELEMETRY="true",
                           PYTHONIOENCODING="utf-8", BLENDER_PRODUCTION_WORKDIR=directory)
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "blender_mcp.production.server"], env=environment)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                names = []
                cursor = None
                while True:
                    page = await session.list_tools(cursor=cursor)
                    names.extend(tool.name for tool in page.tools)
                    cursor = page.nextCursor
                    if not cursor:
                        break
                expected = {"production.scene_audit", "production.create_checkpoint",
                            "production.animation_detect_foot_sliding"}
                missing = sorted(expected - set(names))
                if missing:
                    raise RuntimeError(f"Expected production tools are missing: {missing}")
                print(json.dumps({"status": "pass", "package_version": version("blender-production-mcp"),
                    "server": initialized.serverInfo.name, "tool_count": len(names),
                    "blender_connection_tested": False, "scene_modified": False}, indent=2))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(verify(), timeout=45))
