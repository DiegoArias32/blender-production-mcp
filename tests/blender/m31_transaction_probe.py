"""Real-Blender transaction-state (AUD-007) and idempotency (AUD-010) qualification.

Uses the generated M3 rig fixture and the IK/FK single-frame match that is known to pass (AUD-001a).
Blender really executes every command; only the transport is made lossy for the idempotency cases:
- lost answer: Blender runs EXECUTE (the candidate exists), then the client sees a timeout;
- lost request: the client sees a timeout and Blender never ran EXECUTE.
Every judgement counts Actions and reads state with bpy directly.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import types
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PROMOTED = ROOT / 'test-artifacts' / 'm3.1-regressions'


def inside(output, label):
    import bpy
    sys.path.insert(0, str(ROOT / 'src'))
    sys.path.insert(0, str(ROOT / 'tests' / 'blender'))
    package = types.ModuleType('blender_mcp')
    package.__path__ = [str(ROOT / 'src/blender_mcp')]
    sys.modules['blender_mcp'] = package
    from blender_mcp.production.common.bridge import build_code
    from blender_mcp.production.common.storage import Storage
    class BlenderTimeoutError(Exception):
        # Same name and flag as common.transport.BlenderTimeoutError, which cannot be imported inside
        # Blender (it depends on the host-only mcp package). The ledger classifies by both.
        uncertain = True
    from blender_mcp.production.common.ledger import OperationLedger
    from blender_mcp.production.common.m3_transaction import EXECUTE
    from blender_mcp.production.rig.ik_fk import operate
    from m3_rig_fixtures import build as build_rig
    from m31_fixture_support import register_open_fixture

    class LossyBridge:
        def __init__(self):
            self.lose_answer = False
            self.lose_request = False
            self.executes = 0

        def run(self, body, params=None):
            code = build_code(body, params)
            if body.endswith(EXECUTE):
                if self.lose_request:
                    self.lose_request = False
                    raise BlenderTimeoutError('TRANSPORT_TIMEOUT: simulated lost request')
                self.executes += 1
            namespace = {}
            exec(code.rsplit('print(', 1)[0], namespace)
            if body.endswith(EXECUTE) and self.lose_answer:
                self.lose_answer = False
                raise BlenderTimeoutError('TRANSPORT_TIMEOUT: simulated lost answer (Blender finished)')
            return namespace['result']

    bridge = LossyBridge()
    storage = Storage(output / 's')
    report = {'label': label, 'blender': bpy.app.version_string, 'checks': {}}

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')

    def check(name, passed, **details):
        report['checks'][name] = {'passed': bool(passed), **details}
        save()
        print('CHECK', name, 'PASS' if passed else 'FAIL', flush=True)

    try:
        build_rig(output / 'rig')
        register_open_fixture(bridge, storage, output)
        rig = bpy.data.objects['M3_MixedRig']
        scene = bpy.context.scene
        source_name = rig.animation_data.action.name
        options = {'chain': ['UpperLeg.R', 'LowerLeg.R', 'Foot.R'], 'ik_target': 'ContactControl.R',
                   'pole_target': 'PoleControl.R', 'confirm_mapping': True, 'frame': 5}

        def candidates():
            return sorted(a.name for a in bpy.data.actions if a.name.startswith(source_name + '__M3_'))

        def propose(tag):
            proposal = operate(bridge, storage, tag, 'fk_to_ik', False, rig.name, 'propose', options)
            assert proposal['proposal'].get('supported'), proposal['proposal'].get('reasons')
            return proposal['operation'], proposal['correction_id']

        def cycle(mode, operation):
            return operate(bridge, storage, 'x', 'fk_to_ik', False, mode=mode, operation=operation)

        # ------------------------------------------------ AUD-007: verify after a user change is stale
        operation, cid = propose('t1')
        applied = cycle('apply', operation)
        check('apply_passes', applied.get('numeric_verification') == 'pass' and rig.animation_data.action.name == applied['working_action'],
              status=applied.get('status'))
        check('verify_passes_before_user_change', cycle('verify', operation).get('numeric_verification') == 'pass')
        rig.location.x += 0.25
        scene.frame_set(7)
        try:
            stale = cycle('verify', operation)
            stale_codes = [str(e)[:120] for e in stale.get('errors', [])]
            stale_ok = stale.get('status') == 'error' and any('STALE' in e for e in stale_codes)
        except ValueError as exc:
            stale_codes, stale_ok = [str(exc)[:200]], 'STALE_PROPOSAL' in str(exc)
        check('verify_after_user_change_is_stale', stale_ok, errors=stale_codes)
        check('verify_keeps_user_change_and_frame', abs(rig.location.x - 0.25) < 1e-9 and scene.frame_current == 7,
              location_x=rig.location.x, frame=scene.frame_current)
        reverted = cycle('revert', operation)
        check('revert_succeeds_despite_user_change', reverted.get('status') != 'error' and rig.animation_data.action.name == source_name,
              warnings=reverted.get('warnings'), status=reverted.get('status'))
        check('revert_keeps_user_change_and_frame', abs(rig.location.x - 0.25) < 1e-9 and scene.frame_current == 7,
              location_x=rig.location.x, frame=scene.frame_current)
        check('revert_reports_kept_context', any('USER_CONTEXT_KEPT' in w for w in reverted.get('warnings', [])))
        rig.location.x -= 0.25
        scene.frame_set(1)

        # ------------------------------------------------ AUD-010: lost answer after Blender executed
        operation, cid = propose('t3')
        before = candidates()
        bridge.lose_answer = True
        first = cycle('apply', operation)
        after_first = candidates()
        check('lost_answer_is_uncertain', first.get('idempotency', {}).get('state') == 'UNCERTAIN' and len(after_first) == len(before) + 1,
              state=first.get('idempotency'), new_candidates=len(after_first) - len(before))
        executes = bridge.executes
        second = cycle('apply', operation)
        check('retry_reconciles_without_executing_again', second.get('idempotency', {}).get('reconciled') == 'executed'
              and second['idempotency']['state'] == 'SUCCEEDED' and bridge.executes == executes and candidates() == after_first,
              idempotency=second.get('idempotency'), executes_before=executes, executes_after=bridge.executes)
        third = cycle('apply', operation)
        check('third_apply_is_a_replay', third.get('idempotency', {}).get('replayed') is True and candidates() == after_first)
        record = OperationLedger(storage).get(cid + ':apply')
        check('ledger_answers_what_happened', record['state'] == 'SUCCEEDED' and record.get('reconciliation', {}).get('candidates') == [second['working_action']],
              ledger_state=record['state'], reconciliation=record.get('reconciliation'))
        check('reconciled_candidate_verifies', cycle('verify', operation).get('numeric_verification') == 'pass')
        cycle('revert', operation)

        # ------------------------------------------------ AUD-010: lost request (nothing executed)
        operation, cid = propose('t4')
        before = candidates()
        bridge.lose_request = True
        first = cycle('apply', operation)
        check('lost_request_is_uncertain_without_candidate', first.get('idempotency', {}).get('state') == 'UNCERTAIN' and candidates() == before)
        second = cycle('apply', operation)
        check('retry_after_lost_request_executes_once', second.get('idempotency', {}).get('state') == 'SUCCEEDED'
              and len(candidates()) == len(before) + 1, idempotency=second.get('idempotency'))
        # Source changed after apply: revert is refused and nothing is reassigned.
        source = bpy.data.actions[source_name]
        curve = next(c for layer in source.layers for strip in layer.strips for bag in strip.channelbags for c in bag.fcurves)
        curve.keyframe_points[0].co.y += 0.5
        active = rig.animation_data.action.name
        try:
            cycle('revert', operation)
            refused = False
        except ValueError as exc:
            refused = 'STALE_PROPOSAL' in str(exc)
        check('revert_refused_when_source_changed', refused and rig.animation_data.action.name == active)
        curve.keyframe_points[0].co.y -= 0.5
    except Exception as exc:
        report['exception'] = repr(exc)
        report['traceback'] = traceback.format_exc()[-4000:]
    report['passed'] = bool(report['checks']) and 'exception' not in report and all(c['passed'] for c in report['checks'].values())
    save()
    print('M31_TRANSACTION_REPORT=' + str(output / 'report.json'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', required=True)
    parser.add_argument('--label', default='transaction')
    args = parser.parse_args()
    run_id = uuid4().hex[:8]
    output = Path(tempfile.gettempdir()) / 'bpm31' / (args.label + '-' + run_id)
    output.mkdir(parents=True)
    process = subprocess.run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--python',
                              str(Path(__file__).resolve()), '--', '--inside', str(output), args.label],
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'stdout.txt').write_text(process.stdout, encoding='utf-8')
    (output / 'stderr.txt').write_text(process.stderr, encoding='utf-8')
    print('\n'.join(line for line in process.stdout.splitlines() if line.startswith(('CHECK', 'M31_'))))
    report_path = output / 'report.json'
    if not report_path.exists():
        print(process.stdout[-4000:])
        print(process.stderr[-4000:])
        raise SystemExit(1)
    PROMOTED.mkdir(parents=True, exist_ok=True)
    promoted = PROMOTED / (args.label + '-' + run_id + '.json')
    shutil.copyfile(report_path, promoted)
    print('PROMOTED=' + str(promoted))
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if report.get('exception'):
        print(report['traceback'])
    raise SystemExit(0 if report.get('passed') else 1)


if __name__ == '__main__':
    if '--inside' in sys.argv:
        position = sys.argv.index('--inside')
        inside(Path(sys.argv[position + 1]), sys.argv[position + 2])
    else:
        main()
