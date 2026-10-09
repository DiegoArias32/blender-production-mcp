"""Reproducible M3.1 transport/digest benchmark (audit AUD-009).

Measures, on this machine:
- receive time of a single JSON response over a real localhost socket, upstream
  ``BlenderConnection.receive_full_response`` versus ``ProductionConnection``;
- digest throughput: pure-Python SHA-256 (M1-M3 Blender path), hashlib SHA-256 (reference) and the
  M3.1 integer digest, on the host and optionally inside Blender.

Writes ``test-artifacts/m3.1-benchmarks/transport-<id>.json``. Numbers are machine-specific.
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))


def payload(megabytes):
    # Snapshot-like rows: floats, handle pairs and enum strings, as returned by m2_snapshot.
    row = {'co': [12.0, 0.123456789], 'handle_left': [11.5, 0.11], 'handle_right': [12.5, 0.13],
           'interpolation': 'BEZIER', 'left': 'AUTO_CLAMPED', 'right': 'AUTO_CLAMPED'}
    unit = len(json.dumps(row)) + 1
    body = json.dumps({'status': 'success', 'result': {'rows': [row] * int(megabytes * 1048576 / unit)}})
    return body.encode('utf-8')


def serve_once(data):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('127.0.0.1', 0))
    server.listen(1)

    def run():
        client, _ = server.accept()
        client.sendall(data)
        time.sleep(0.2)
        client.close()
        server.close()
    threading.Thread(target=run, daemon=True).start()
    return server.getsockname()[1]


def time_receive(connection_class, data):
    port = serve_once(data)
    sock = socket.create_connection(('127.0.0.1', port))
    connection = connection_class('127.0.0.1', port)
    started = time.perf_counter()
    received = connection.receive_full_response(sock)
    elapsed = time.perf_counter() - started
    sock.close()
    assert json.loads(received) is not None and len(received) == len(data)
    return elapsed


def host_digests():
    import hashlib
    from blender_mcp.production.common.digest import m2_sha256, m2_fast_digest
    rows = []
    for size in (1, 4, 16):
        data = payload(size)
        started = time.perf_counter(); hashlib.sha256(data).hexdigest(); reference = time.perf_counter() - started
        started = time.perf_counter(); m2_fast_digest(data); fast = time.perf_counter() - started
        row = {'megabytes': round(len(data) / 1048576, 3), 'hashlib_sha256_s': reference, 'm2_fast_digest_s': fast}
        if size == 1:
            started = time.perf_counter(); m2_sha256(data); row['pure_python_sha256_s'] = time.perf_counter() - started
        rows.append(row)
    return rows


BLENDER_SCRIPT = r'''
import json, sys, time
sys.path.insert(0, sys.argv[-2])
import types
package = types.ModuleType('blender_mcp'); package.__path__ = [sys.argv[-2] + '/blender_mcp']; sys.modules['blender_mcp'] = package
from blender_mcp.production.common.digest import m2_sha256, m2_fast_digest
row = {'co': [12.0, 0.123456789], 'handle_left': [11.5, 0.11], 'handle_right': [12.5, 0.13], 'interpolation': 'BEZIER', 'left': 'AUTO_CLAMPED', 'right': 'AUTO_CLAMPED'}
out = []
for size in (1, 16):
    data = json.dumps({'rows': [row] * int(size * 1048576 / (len(json.dumps(row)) + 1))}).encode()
    entry = {'megabytes': round(len(data) / 1048576, 3)}
    started = time.perf_counter(); m2_fast_digest(data); entry['m2_fast_digest_s'] = time.perf_counter() - started
    if size == 1:
        started = time.perf_counter(); m2_sha256(data); entry['pure_python_sha256_s'] = time.perf_counter() - started
    out.append(entry)
open(sys.argv[-1], 'w').write(json.dumps({'python': sys.version.split()[0], 'rows': out}))
'''


def blender_digests(blender):
    work = Path(tempfile.gettempdir()) / 'bpm31' / ('bench-' + uuid4().hex[:8])
    work.mkdir(parents=True)
    script = work / 'digest_bench.py'
    script.write_text(BLENDER_SCRIPT, encoding='utf-8')
    output = work / 'digest.json'
    subprocess.run([blender, '--background', '--factory-startup', '--disable-autoexec', '--python', str(script),
                    '--', str(ROOT / 'src'), str(output)], capture_output=True, text=True, check=True)
    return json.loads(output.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender')
    args = parser.parse_args()
    from blender_mcp.server import BlenderConnection
    from blender_mcp.production.common.transport import ProductionConnection
    receive = []
    for size in (1, 2, 4):
        data = payload(size)
        receive.append({'megabytes': round(len(data) / 1048576, 3), 'upstream_s': time_receive(BlenderConnection, data),
                        'production_s': time_receive(ProductionConnection, data)})
    for size in (16, 32):
        data = payload(size)
        receive.append({'megabytes': round(len(data) / 1048576, 3), 'upstream_s': None,
                        'production_s': time_receive(ProductionConnection, data)})
    report = {'python': sys.version.split()[0], 'receive': receive, 'host_digest': host_digests()}
    if args.blender:
        report['blender_digest'] = blender_digests(args.blender)
    folder = ROOT / 'test-artifacts' / 'm3.1-benchmarks'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('transport-' + uuid4().hex[:8] + '.json')
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    print('BENCHMARK=' + str(path))


if __name__ == '__main__':
    main()
