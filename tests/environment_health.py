"""Environment health check (M3.1, audit AUD-018). Read-only: installs nothing, changes nothing.

Checks, in order, and reports each as pass / fail / skipped with the measured value:
1. interpreter version equals .python-version (the locked runtime);
2. the virtual environment's base interpreter exists (the AUD-018 failure mode);
3. `uv sync --frozen --check` reports the environment matches uv.lock;
4. key package versions (mcp, anyio, pydantic, pytest);
5. Blender executable and version (optional --blender);
6. the production MCP server starts over stdio and lists its tools;
7. optional (--live): a safe native call (production.scene_audit, production.asset_status) against
   the Blender instance listening on BLENDER_HOST:BLENDER_PORT.

Writes the JSON report to --output (default: print only).
Usage: uv run --frozen python tests/environment_health.py [--blender <exe>] [--live] [--output <file>]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def check_python():
    wanted = (ROOT / '.python-version').read_text(encoding='utf-8').strip()
    actual = sys.version.split()[0]
    return {'passed': actual == wanted, 'expected': wanted, 'actual': actual, 'executable': sys.executable}


def check_venv_home():
    config = ROOT / '.venv' / 'pyvenv.cfg'
    if not config.exists():
        return {'passed': False, 'error': 'missing .venv/pyvenv.cfg'}
    home = next((line.split('=', 1)[1].strip() for line in config.read_text(encoding='utf-8').splitlines()
                 if line.startswith('home')), None)
    return {'passed': bool(home) and Path(home).exists(), 'home': home}


def check_lock():
    uv = shutil.which('uv')
    if not uv:
        return {'passed': False, 'error': 'uv not found on PATH'}
    process = subprocess.run([uv, 'sync', '--frozen', '--group', 'dev', '--check'], cwd=ROOT,
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    return {'passed': process.returncode == 0, 'exit_code': process.returncode,
            'output': (process.stdout + process.stderr).strip()[-600:]}


def check_packages():
    versions = {}
    for name in ('mcp', 'anyio', 'pydantic', 'pytest'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {'passed': all(versions.values()), 'versions': versions}


def check_blender(executable):
    if not executable:
        return {'passed': None, 'skipped': 'no --blender given'}
    path = Path(executable)
    if not path.exists():
        return {'passed': False, 'error': 'not found: ' + executable}
    process = subprocess.run([str(path), '--version'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    first = (process.stdout.strip().splitlines() or [''])[0]
    return {'passed': process.returncode == 0 and first.startswith('Blender'), 'version': first}


async def check_mcp(live):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    workdir = Path(tempfile.gettempdir()) / 'bpm31' / 'environment-health'
    workdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, BLENDER_HOST=os.environ.get('BLENDER_HOST', 'localhost'),
               BLENDER_PORT=os.environ.get('BLENDER_PORT', '9876'), BLENDER_MCP_SAFE_MODE='1',
               DISABLE_TELEMETRY='true', BLENDER_PRODUCTION_WORKDIR=str(workdir), PYTHONIOENCODING='utf-8')
    params = StdioServerParameters(command=sys.executable, args=['-m', 'blender_mcp.production.server'], env=env)
    result = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            info = await client.initialize()
            tools = await client.list_tools()
            result['server'] = {'passed': len(tools.tools) > 0, 'name': info.serverInfo.name,
                                'tools': len(tools.tools)}
            if live:
                calls = {}
                for name in ('production.scene_audit', 'production.asset_status'):
                    response = await client.call_tool(name, {})
                    data = (response.structuredContent or {}).get('result', {})
                    calls[name] = {'status': data.get('status'), 'errors': data.get('errors'),
                                   'elapsed_ms': (data.get('metrics') or {}).get('elapsed_ms')}
                result['live_call'] = {'passed': all(c['status'] in ('success', 'warning') for c in calls.values()),
                                       'calls': calls}
            else:
                result['live_call'] = {'passed': None, 'skipped': 'no --live (needs Blender with the addon listening)'}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = {'python': check_python(), 'venv_home': check_venv_home(), 'lock': check_lock(),
              'packages': check_packages(), 'blender': check_blender(args.blender)}
    try:
        report.update(asyncio.run(check_mcp(args.live)))
    except Exception as exc:  # recorded, never hidden
        report['server'] = {'passed': False, 'error': type(exc).__name__ + ': ' + str(exc)[:400]}
    report['passed'] = all(item.get('passed') is not False for item in report.values() if isinstance(item, dict))
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text)
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
