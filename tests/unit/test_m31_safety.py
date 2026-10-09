"""M3.1 audit-remediation contracts that do not need Blender (real-Blender cases live in
tests/blender/m31_*_probe.py)."""
import ast
import json
import math
from pathlib import Path

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.payload import link_payload
from blender_mcp.production.common.fcurve_window import WINDOW_RUNTIME
from blender_mcp.production.common.state_guard import STATE_RUNTIME
from blender_mcp.production.common import asset_safety
from blender_mcp.production.common.asset_safety import (AssetRegistry, CURRENT_FILE, current_asset, register_fixture,
                                                        require_mutable_asset, create_working_copy, STAMP_AND_SAVE_COPY)
from blender_mcp.production.common.storage import Storage
from blender_mcp.production.animation.algorithms import contact_report
from blender_mcp.production import server


# ---------------------------------------------------------------- AUD-004 global state

def _templates():
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME, READ, PREFLIGHT, EXECUTE, CHECK
    from blender_mcp.production.animation import scripts, insights_scripts, actions
    from blender_mcp.production.animation.corrections import EXECUTE as M2_EXECUTE, READ as M2_READ, runtime
    from blender_mcp.production.contacts.runtime import CONTACT_RUNTIME
    from blender_mcp.production.deformation.runtime import EXECUTE as DQ
    from blender_mcp.production.qa.multiview import _PREFLIGHT
    from blender_mcp.production.mocap.service import ANALYZE, RUNTIME
    return {'m3_read': BASE_RUNTIME + READ, 'm3_preflight': BASE_RUNTIME + PREFLIGHT, 'm3_execute': BASE_RUNTIME + EXECUTE,
            'm3_check': BASE_RUNTIME + CHECK, 'm2_execute': runtime('foot_sliding') + M2_EXECUTE,
            'm2_read': runtime('cleanup') + M2_READ, 'foot_sliding_m1': STATE_RUNTIME + scripts.FOOT_SLIDING,
            'insights_sample': insights_scripts.SAMPLE, 'compare_actions': actions.BODY + actions.COMPARE,
            'contacts': BASE_RUNTIME + CONTACT_RUNTIME, 'deformation': DQ, 'multiview': _PREFLIGHT,
            'mocap_analyze': BASE_RUNTIME + RUNTIME + ANALYZE}


@pytest.mark.parametrize('name', sorted(_templates()))
def test_every_evaluating_template_requires_pose_position(name):
    body = _templates()[name]
    assert 'prod_require_' in body, name
    assert "pose_position != 'POSE'" in body or 'prod_require_pose_position' in body


def test_state_runtime_passes_unchanged_safe_mode():
    assert build_code('import bpy\n' + STATE_RUNTIME + '\nresult={}')


@pytest.mark.parametrize('message,code', [
    ('POSE_POSITION_REQUIRED: armature "Rig" is in REST', 'POSE_POSITION_REQUIRED'),
    ('{"exception_type": "ValueError", "message": "STALE_PROPOSAL: context changed"}', 'STALE_PROPOSAL'),
    ('plain failure without code', None)])
def test_precondition_codes_reach_the_envelope(message, code):
    assert server.precondition_code(message) == code
    record = server.error_record(ValueError(message))
    assert record.get('code') == code


def test_context_fingerprint_includes_global_state():
    from blender_mcp.production.common.m3_transaction import M3_RUNTIME
    assert "'global_state':prod_global_state" in M3_RUNTIME
    assert "'pose_position'" in STATE_RUNTIME and "'ik_solver'" in STATE_RUNTIME


# ---------------------------------------------------------------- AUD-001 mutation windows

def _window_namespace():
    namespace = {'math': math}
    source = ''.join(ast.get_source_segment(WINDOW_RUNTIME, node) + '\n' for node in ast.parse(WINDOW_RUNTIME).body
                     if isinstance(node, ast.FunctionDef) and node.name in {'fw_fences', 'fw_lerp', 'fw_corrected_handles', 'fw_bezier_split', 'fw_representable'})
    exec(source, namespace)
    return namespace


def _bezier(p0, h1, h2, p3, t):
    return [(1-t)**3*p0[i] + 3*(1-t)**2*t*h1[i] + 3*(1-t)*t*t*h2[i] + t**3*p3[i] for i in range(2)]


@pytest.mark.parametrize('window,fences', [((5, 5), [4, 6]), ((3, 6), [2, 7]), ((5.5, 5.5), [5, 6]), ((2.25, 4.75), [2, 5])])
def test_fences_are_the_integer_frames_just_outside_the_window(window, fences):
    assert _window_namespace()['fw_fences'](*window) == fences


def _value_at(segments, frame):
    for p0, h1, h2, p3 in segments:
        if p0[0] - 1e-12 <= frame <= p3[0] + 1e-12:
            low, high = 0.0, 1.0
            for _ in range(100):
                mid = (low + high) / 2
                if _bezier(p0, h1, h2, p3, mid)[0] < frame:
                    low = mid
                else:
                    high = mid
            return _bezier(p0, h1, h2, p3, (low + high) / 2)[1]
    raise AssertionError(frame)


@pytest.mark.parametrize('segment,split_at', [
    (([1, 0], [2.5, 0], [5.5, 1], [7, 1]), 4),       # AUTO-like easing
    (([1, 0], [1.2, 3], [6.8, -2], [7, 1]), 5.5),   # large handles
    (([0, 2], [30, 5], [-20, -1], [10, 0]), 3),     # handles beyond the span: Blender clamps them
])
def test_bezier_fence_split_reproduces_the_evaluated_curve(segment, split_at):
    ns = _window_namespace()
    p0, h1, h2, p3 = segment
    c1, c2 = ns['fw_corrected_handles'](p0, h1, h2, p3)
    split = ns['fw_bezier_split'](p0, h1, h2, p3, split_at)
    assert abs(split['split'][0] - split_at) < 1e-9
    representable = (ns['fw_representable'](p0, split['left_out'], split['split_in'], split['split'])
                     and ns['fw_representable'](split['split'], split['split_out'], split['right_in'], p3))
    if not representable:
        # Blender would clamp a half's handles again: the runtime refuses (UNSUPPORTED_BEZIER_SPLIT)
        # instead of writing an inexact fence. Only the extreme-handle case is expected here.
        assert h1[0] - p0[0] > p3[0] - p0[0]
        return
    original = [(p0, c1, c2, p3)]
    halves = [(p0, split['left_out'], split['split_in'], split['split']), (split['split'], split['split_out'], split['right_in'], p3)]
    for step in range(81):
        frame = p0[0] + (p3[0] - p0[0]) * step / 80
        assert abs(_value_at(original, frame) - _value_at(halves, frame)) < 1e-9


def test_ik_fk_verification_checks_outside_window_and_protected_bones():
    from blender_mcp.production.rig.ik_fk import IKFK_RUNTIME
    for marker in ('OUTSIDE_WINDOW_CHANGED', 'PROTECTED_BONES_CHANGED', 'UNTOUCHED_CURVES_CHANGED',
                   'WINDOW_SWITCH_MISMATCH', 'MASKED_FK_CHANNEL_MISMATCH', 'fw_prepare(', 'ikfk_verification_frames'):
        assert marker in IKFK_RUNTIME, marker
    # The old side effect on other limbs' constant IK influence curves is gone.
    assert "'CONSTANT' if curve.data_path in paths" not in IKFK_RUNTIME


# ---------------------------------------------------------------- AUD-005 no contact is not zero sliding

def test_no_contact_is_unknown_not_zero():
    frames = list(range(1, 11))
    airborne = contact_report(frames, [[0, 0, .3]] * 10, 30, 0, .03, .25, .01, 3)
    assert airborne['status'] == 'inconclusive' and airborne['max_sliding_m'] is None
    planted = contact_report(frames, [[0, 0, 0]] * 10, 30, 0, .03, .25, .01, 3)
    assert planted['max_sliding_m'] == 0


def test_contact_summary_distinguishes_statuses():
    from blender_mcp.production.contacts.runtime import CONTACT_RUNTIME
    for status in ('CONTACT_CONFIRMED', 'NO_CONTACT', 'INSUFFICIENT_CONTACT_DATA', 'AMBIGUOUS_SURFACE',
                   'NO CONTACT EVIDENCE IS NOT ZERO SLIDING', 'find_nearest_range', 'flipped_for_support'):
        assert status in CONTACT_RUNTIME, status


def test_foot_correction_requires_confirmed_contact():
    from blender_mcp.production.contacts.correction_runtime import FOOT_RUNTIME
    assert 'NO_CONFIRMED_CONTACT' in FOOT_RUNTIME and 'AMBIGUOUS_SURFACE' in FOOT_RUNTIME


@pytest.mark.parametrize('options', [{'surface_orientation': 'mirror'}, {'support_direction': [0, 0]},
                                     {'minimum_contact_samples': 1}, {'support_direction': 'up'}])
def test_contact_option_validation(options):
    from blender_mcp.production.contacts.service import validate_options
    with pytest.raises(ValueError):
        validate_options(options)


# ---------------------------------------------------------------- AUD-014 asset safety

class FakeBlender:
    def __init__(self, filepath='', stamps=()):
        self.filepath = filepath
        self.stamps = list(stamps)
        self.calls = []

    def run(self, body, params=None):
        self.calls.append(body)
        if body == CURRENT_FILE:
            return {'filepath': self.filepath, 'is_dirty': False, 'stamps': list(self.stamps)}
        if body == STAMP_AND_SAVE_COPY:
            Path(params['path']).write_bytes(b'BLENDER-v502 working copy')
            self.filepath = params['path']
            self.stamps = [params['stamp']]
            return {'source_path': 'x', 'working_path': params['path'], 'scripts_enabled': False}
        raise AssertionError('unexpected Blender call')


def test_unsaved_session_is_temporary_and_mutable(tmp_path):
    asset = current_asset(FakeBlender(''), Storage(tmp_path / 'store'))
    assert asset['role'] == 'TEMPORARY' and asset['mutable']


def test_unregistered_file_is_refused(tmp_path):
    master = tmp_path / 'hero.blend'; master.write_bytes(b'x')
    with pytest.raises(PermissionError, match='ASSET_ROLE_UNREGISTERED'):
        require_mutable_asset(FakeBlender(str(master)), Storage(tmp_path / 'store'), 'test')


def test_registered_master_is_read_only(tmp_path):
    master = tmp_path / 'hero.blend'; master.write_bytes(b'x')
    storage = Storage(tmp_path / 'store')
    asset_safety.register_master(FakeBlender(str(master)), storage)
    with pytest.raises(PermissionError, match='MASTER_ASSET_READ_ONLY'):
        require_mutable_asset(FakeBlender(str(master)), storage, 'test')


def test_role_is_not_inferred_from_the_file_name(tmp_path):
    fake_copy = tmp_path / 'working_copy_fixture_temp.blend'; fake_copy.write_bytes(b'x')
    assert current_asset(FakeBlender(str(fake_copy)), Storage(tmp_path / 'store'))['role'] == 'UNREGISTERED'


def test_working_copy_flow_registers_stamps_and_allows_mutation(tmp_path):
    master = tmp_path / 'hero.blend'; master.write_bytes(b'x')
    storage = Storage(tmp_path / 'store')
    blender = FakeBlender(str(master))
    asset_safety.register_master(blender, storage)
    copy = create_working_copy(blender, storage, 'op')
    assert copy['role'] == 'WORKING_COPY' and copy['source_role'] == 'MASTER'
    assert require_mutable_asset(blender, storage, 'test')['role'] == 'WORKING_COPY'
    # A moved/copied working copy keeps its stamp but not its registered path: refused.
    moved = FakeBlender(str(tmp_path / 'elsewhere.blend'), blender.stamps)
    AssetRegistry(storage).register('FIXTURE', str(tmp_path / 'elsewhere.blend'))
    with pytest.raises(PermissionError, match='ASSET_STAMP_MISMATCH'):
        require_mutable_asset(moved, storage, 'test')


def test_fixture_registration_is_limited_to_fixture_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(asset_safety.tempfile, 'gettempdir', lambda: str(tmp_path / 'tmp'))
    outside = tmp_path / 'projects' / 'hero.blend'
    with pytest.raises(PermissionError, match='FIXTURE_OUTSIDE_ALLOWED_ROOTS'):
        register_fixture(FakeBlender(str(outside)), Storage(tmp_path / 'store'), str(outside))
    inside = tmp_path / 'tmp' / 'fixture.blend'
    assert register_fixture(FakeBlender(str(inside)), Storage(tmp_path / 'store'), str(inside))['role'] == 'FIXTURE'


def test_mutating_transaction_refuses_master_before_checkpoint(tmp_path):
    from blender_mcp.production.common.m3_transaction import transaction, BASE_RUNTIME
    from blender_mcp.production.common.operations import save_proposal, fingerprint
    master = tmp_path / 'hero.blend'; master.write_bytes(b'x')
    storage = Storage(tmp_path / 'store')
    asset_safety.register_master(FakeBlender(str(master)), storage)
    path = save_proposal(storage, 'p', {'kind': 'test', 'object_name': 'Rig', 'source_action': None,
                                        'proposal': {'supported': True}, 'transaction_schema': 1,
                                        'backend_fingerprint': fingerprint(BASE_RUNTIME)})

    class Bridge(FakeBlender):
        def run(self, body, params=None):
            if body == CURRENT_FILE:
                return super().run(body, params)
            self.calls.append(body)
            return {'matches': True}
    bridge = Bridge(str(master))
    with pytest.raises(PermissionError, match='MASTER_ASSET_READ_ONLY'):
        transaction(bridge, storage, 'id', 'test', '', mode='apply', operation=path)
    assert not (storage.root / 'checkpoints').exists()


# ---------------------------------------------------------------- payload linker

def test_linker_keeps_reachable_last_definitions_only():
    source = ("import math\nX = 3\n"
              "def helper():\n    return 1\n"
              "def unused():\n    return 2\n"
              "def backend():\n    return 'default'\n"
              "def backend():\n    return helper()\n"
              "result = backend() + X\n")
    linked = link_payload(source)
    namespace = {}
    exec(linked, namespace)
    assert namespace['result'] == 4
    assert 'def unused' not in linked and linked.count('def backend') == 1 and 'import math' in linked


def test_linked_payloads_stay_under_safe_mode_node_budget():
    from blender_mcp.production.common.m3_transaction import BASE_RUNTIME, EXECUTE, CHECK
    from blender_mcp.production.contacts.correction_runtime import FOOT_RUNTIME
    from blender_mcp.production.rig.ik_fk import IKFK_RUNTIME
    from blender_mcp.production.mocap.service import RUNTIME
    for runtime in (FOOT_RUNTIME, IKFK_RUNTIME, RUNTIME):
        for entry in (EXECUTE, CHECK):
            code = build_code(BASE_RUNTIME + runtime + entry, {})
            assert sum(1 for _ in ast.walk(ast.parse(code))) < 18000
