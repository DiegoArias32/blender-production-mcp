"""Reproducible M3.1 event-loop benchmark (audit AUD-009/AUD-010).

FastMCP runs synchronous tools inside its asyncio event loop: while one tool waits for Blender, no
other request (for example ``production.operation_status``) is served. The production server now
offloads every tool to a worker thread. This measures, with a 1 s tool, when a concurrent 0.1 s
coroutine finishes: sync tool vs thread-offloaded tool vs the real production registration.

Writes ``test-artifacts/m3.1-benchmarks/event-loop-<id>.json``. Numbers are machine-specific.
"""
import asyncio
from functools import partial
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4

import anyio

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
os.environ.setdefault('BLENDER_PRODUCTION_WORKDIR', str(Path(tempfile.gettempdir()) / 'bpm31' / ('loop-' + uuid4().hex[:8])))


async def tick_after(call, seconds=0.1):
    started = time.perf_counter()

    async def tick():
        await asyncio.sleep(seconds)
        return time.perf_counter() - started
    _, finished = await asyncio.gather(call(), tick())
    return finished


async def main():
    from mcp.server.fastmcp import FastMCP
    probe = FastMCP('probe')

    def slow_sync() -> dict:
        time.sleep(1.0)
        return {}

    async def slow_thread() -> dict:
        await anyio.to_thread.run_sync(partial(time.sleep, 1.0))
        return {}
    probe.tool(name='sync')(slow_sync)
    probe.tool(name='thread')(slow_thread)

    from blender_mcp.production import server
    original = server.OperationLedger.get

    def slow_get(self, key):
        time.sleep(0.5)
        return original(self, key)
    server.OperationLedger.get = slow_get
    try:
        production = await tick_after(lambda: server.mcp.call_tool('production.operation_status', {'operation_id': 'bench-op-0001'}))
    finally:
        server.OperationLedger.get = original
    report = {'python': sys.version.split()[0], 'tick_seconds': 0.1, 'tool_seconds': 1.0,
              'tick_finished_s': {'sync_tool': await tick_after(lambda: probe.call_tool('sync', {})),
                                  'thread_tool': await tick_after(lambda: probe.call_tool('thread', {})),
                                  'production_operation_status_0p5s_ledger_read': production}}
    folder = ROOT / 'test-artifacts' / 'm3.1-benchmarks'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('event-loop-' + uuid4().hex[:8] + '.json')
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    print('BENCHMARK=' + str(path))


if __name__ == '__main__':
    asyncio.run(main())
