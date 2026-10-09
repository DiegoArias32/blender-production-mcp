"""Gate N runner: execute every historical M1/M2/M3 real-Blender probe against the current code.

Large probe output goes to %TEMP%\\bpm31\\historical-<id> (BLENDER_PRODUCTION_TEST_ARTIFACTS); only this
summary is promoted to test-artifacts/m3.1-regressions/. A probe passes when it exits with code 0;
its own report decides that, this runner never reinterprets it.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PROBES = [
    ('M1 inspect', ['{blender}', '--background', '--factory-startup', '--disable-autoexec', '--python', 'tests/blender/inspect_probe.py']),
    ('M1 animation', ['{python}', 'tests/blender/animation_probe.py', '--blender', '{blender}']),
    ('M1 qa', ['{python}', 'tests/blender/qa_probe.py', '--blender', '{blender}']),
    ('M2 fixtures', ['{python}', 'tests/blender/m2_fixtures_probe.py', '--blender', '{blender}']),
    ('M2 insights', ['{python}', 'tests/blender/m2_insights_probe.py', '--blender', '{blender}']),
    ('M2 curves', ['{python}', 'tests/blender/m2_curves_probe.py', '--blender', '{blender}']),
    ('M2 corrections', ['{python}', 'tests/blender/m2_correction_probe.py', '--blender', '{blender}']),
    ('M2 transform adversarial', ['{python}', 'tests/blender/m2_transform_adversarial.py', '--blender', '{blender}']),
    ('M2 multiview', ['{python}', 'tests/blender/m2_multiview_probe.py', '--blender', '{blender}']),
    ('M2 benchmark', ['{python}', 'tests/blender/m2_benchmark.py', '--blender', '{blender}']),
    ('M3 rig', ['{python}', 'tests/blender/m3_rig_probe.py', '--blender', '{blender}']),
    ('M3 contacts', ['{python}', 'tests/blender/m3_contacts_probe.py', '--blender', '{blender}']),
    ('M3 mocap', ['{python}', 'tests/blender/m3_mocap_probe.py', '--blender', '{blender}']),
    ('M3 deformation', ['{python}', 'tests/blender/m3_deformation_probe.py', '--blender', '{blender}']),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--only', nargs='*')
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    artifacts = Path(tempfile.gettempdir()) / 'bpm31' / ('historical-' + run_id)
    artifacts.mkdir(parents=True)
    env = dict(os.environ, BLENDER_PRODUCTION_TEST_ARTIFACTS=str(artifacts), PYTHONIOENCODING='utf-8')
    rows = []
    for name, template in PROBES:
        if args.only and name not in args.only:
            continue
        command = [part.format(blender=args.blender, python=sys.executable) for part in template]
        started = time.perf_counter()
        process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        elapsed = round(time.perf_counter() - started, 1)
        rows.append({'probe': name, 'command': ' '.join(command[1:]) if command[0] == sys.executable else ' '.join(command),
                     'exit_code': process.returncode, 'passed': process.returncode == 0, 'seconds': elapsed,
                     'stdout_tail': process.stdout[-1500:], 'stderr_tail': process.stderr[-1500:]})
        print(('PASS ' if process.returncode == 0 else 'FAIL ') + name + ' (' + str(elapsed) + ' s)', flush=True)
    report = {'run_id': run_id, 'artifacts': str(artifacts), 'probes': rows,
              'passed': all(r['passed'] for r in rows), 'python': sys.version.split()[0]}
    folder = ROOT / 'test-artifacts' / 'm3.1-regressions'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('historical-' + run_id + '.json')
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('HISTORICAL_REPORT=' + str(path))


if __name__ == '__main__':
    main()
