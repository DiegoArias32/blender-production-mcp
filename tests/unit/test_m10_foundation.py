"""M10 foundation without Blender: catalogue, hints, validators, time conversion, registry, dispatch, payload budgets."""
from __future__ import annotations

import ast
import asyncio

import pytest

from blender_mcp.production.cinematic import lifecycle, model, registry, store
from blender_mcp.production.cinematic.runtime import COMMON
from blender_mcp.production.cinematic.service import HANDLERS, dispatch, load_handlers
from blender_mcp.production.cinematic.tools import NAMES, READ_ONLY, REQUIRED, TRANSIENT
from blender_mcp.production.common.bridge import build_code, payload_bytes
from blender_mcp.production.common.storage import Storage


def test_catalogue_has_the_37_required_units_plus_review_and_status():
    assert len(REQUIRED) == 37 and len(set(REQUIRED)) == 37
    assert set(NAMES) - set(REQUIRED) == {'cinematic_review', 'cinematic_status'}
    assert READ_ONLY <= set(NAMES) and TRANSIENT <= set(NAMES) and not READ_ONLY & TRANSIENT


def test_registered_hints_follow_the_declared_sets():
    from blender_mcp.production.server import TRANSIENT_READERS, mcp
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in NAMES:
        tool = tools['production.' + name]
        assert tool.annotations.readOnlyHint is (name in READ_ONLY)
        assert (('production.' + name) in TRANSIENT_READERS) is (name in TRANSIENT)
        assert ('operation_id' in tool.inputSchema['properties']) is (name not in READ_ONLY | TRANSIENT)
        assert tool.annotations.destructiveHint is False


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), '3', None])
def test_numbers_refuse_bool_nonfinite_and_text(value):
    with pytest.raises(ValueError, match='CINEMATIC_NUMBER_INVALID'):
        model.number(value, 'x')


def test_closed_options_refuse_unknown_keys():
    with pytest.raises(ValueError, match='CINEMATIC_OPTION_UNSUPPORTED: unknown keys b'):
        model.closed({'a': 1, 'b': 2}, {'a'})


def test_seconds_to_frame_is_explicit_and_reproducible():
    frame, record = model.seconds_to_frame(15.0, 30)
    assert frame == 451 and record['rounding_error_frames'] == 0
    frame, record = model.seconds_to_frame(0.51, 24)
    assert frame == 13 and abs(record['exact_frame'] - 13.24) < 1e-9 and record['rounding_error_frames'] < 0
    assert model.frame_to_seconds(451, 30) == 15.0
    assert model.seconds_to_frame(1.0, 30, 1.001)[0] == 31


def test_owned_names_are_deterministic_and_bounded():
    gid = 'c5f51533' + '0' * 24
    assert model.owned_prefix(gid, 1) == 'M10C_c5f51533_v1'
    assert model.owned_name(gid, 1, 'CAM_A') == 'M10C_c5f51533_v1_CAM_A'
    with pytest.raises(ValueError, match='CINEMATIC_NAME_TOO_LONG'):
        model.owned_name(gid, 1, 'X' * 60)


def test_registry_is_idempotent_and_never_identifies_by_name_alone(tmp_path):
    storage = Storage(tmp_path)
    key = registry.asset_key({'asset_id': 'abc-123'}, 'Scene')
    first = registry.write(storage, key, {'object': 'Rig', 'data': 'RigData', 'fingerprint': 'f1'})
    again = registry.write(storage, key, {'object': 'Rig', 'data': 'RigData', 'fingerprint': 'f1'})
    changed = registry.write(storage, key, {'object': 'Rig', 'data': 'RigData', 'fingerprint': 'f2'})
    assert first['actor_id'] == again['actor_id'] != changed['actor_id']
    assert registry.match(storage, key, 'Rig', 'RigData', 'f1')['actor_id'] == first['actor_id']
    assert registry.find(storage, key, first['actor_id'])['fingerprint'] == 'f1'
    with pytest.raises(ValueError, match='ACTOR_MISSING'):
        registry.find(storage, key, '0' * 16)
    with pytest.raises(ValueError, match='ACTOR_ID_INVALID'):
        registry.find(storage, key, 'Rig')
    assert registry.asset_key({}, 'Scene').startswith('TEMPORARY-')


def test_dispatch_rejects_bad_inputs_and_unknown_tools():
    with pytest.raises(ValueError, match='CINEMATIC_APPLY_BOOLEAN_REQUIRED'):
        dispatch('cinematic_status', None, None, 'op', apply='yes')
    with pytest.raises(ValueError, match='CINEMATIC_OPTIONS_OBJECT_REQUIRED'):
        dispatch('cinematic_status', None, None, 'op', options=[1])
    with pytest.raises(ValueError, match='CINEMATIC_NOT_IMPLEMENTED'):
        dispatch('cinematic_unknown', None, None, 'op')
    load_handlers()
    assert {'cinematic_create_candidate', 'cinematic_reject', 'cinematic_status'} <= set(HANDLERS)


def test_review_and_render_invalidate_on_assembly_change():
    state = {'state': 'REVIEWED', 'render': {'output_dir': 'x'}, 'review': {'decision': 'ACCEPT'},
             'quality_report': {'passed': True}, 'assembled': {'candidate_fingerprint': 'F'}}
    changed = store.invalidate_review(state)
    assert changed['state'] == 'CINEMATIC_CANDIDATE' and changed['render'] is None and changed['review'] is None
    assert changed['quality_report'] is None and changed['assembled'] is None and state['state'] == 'REVIEWED'


def test_actor_meshes_drop_excluded_sources_and_add_owned_copies():
    actor = {'actor_id': 'a' * 16, 'object': 'Hero', 'meshes': ['Hero_Body', 'Hero_Pendant']}
    state = {'assembly': {'excluded_objects': ['Hero_Pendant']},
             'actors': {'a' * 16: {'secondary': [{'copy': 'M10C_g_v1_SEC', 'object': 'M10C_g_v1_SEC'}]}}}
    assert store.actor_meshes(actor, state) == ['Hero_Body', 'M10C_g_v1_SEC']
    assert store.actor_meshes(actor, None) == ['Hero_Body', 'Hero_Pendant']
    assert store.actor_meshes(dict(actor, meshes=[]), {}) == []


def test_backend_fingerprint_is_stable_within_a_process():
    assert store.backend_fingerprint() == store.backend_fingerprint()


@pytest.mark.parametrize('body', [lifecycle.CREATE, lifecycle.REJECT, lifecycle.SOURCE_INFO, store.STATUS_BODY], ids=['create', 'reject', 'source_info', 'status'])
def test_foundation_payloads_pass_safe_mode_within_budget(body):
    params = {'source_scene': 'Scene', 'scene': 'M10C_x_v1', 'collections': {'src': 'a', 'cin': 'b'}, 'generation_id': 'g',
              'version': 1, 'actor_objects': [], 'touched': [], 'baseline': None, 'source_fingerprint': 'f',
              'frame_start': 1, 'frame_end': 2}
    code = build_code(body, params)
    assert payload_bytes(body, params) < 140_000
    assert sum(1 for _ in ast.walk(ast.parse(code))) < 14_000


def test_common_runtime_never_uses_forbidden_constructs():
    for forbidden in ('import bpy_extras', 'from bpy_extras', 'import numpy', '__class__', 'lambda ', 'driver_add('):
        assert forbidden not in COMMON
