import json
from pathlib import Path

import pytest

from blender_mcp.production.animation.generation import store
from blender_mcp.production.animation.generation.render import _chunk, write_apng
from blender_mcp.production.common.storage import Storage


def test_generation_plan_is_immutable_and_events_are_signed(tmp_path):
    storage = Storage(tmp_path)
    reference, plan = store.save(storage, 'test-m6-store', {'brief': {'type': 'IDLE'}})
    path, reread, state = store.load(storage, reference)
    assert reread == plan and state['state'] == 'PLANNED'
    store.event(path.parent, {'state': 'GENERATED_CANDIDATE', 'candidate_action': 'New'})
    assert store.load(storage, reference)[2]['candidate_action'] == 'New'
    entry = sorted(path.parent.glob('event-*.json'))[-1]
    data = json.loads(entry.read_text())
    data['data']['candidate_action'] = 'ExistingProductionAction'
    entry.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='LEDGER_INVALID'):
        store.load(storage, reference)


def test_plan_tamper_is_rejected(tmp_path):
    storage = Storage(tmp_path)
    reference, _ = store.save(storage, 'test-m6-store', {'object_name': 'Fixture'})
    data = json.loads(Path(reference).read_text())
    data['object_name'] = 'RealMaster'
    Path(reference).write_text(json.dumps(data))
    with pytest.raises(ValueError, match='STALE_PROPOSAL'):
        store.load(storage, reference)


@pytest.mark.parametrize('cycles', [3, 4, 100])
def test_persistent_refinement_counter_cannot_be_extended(cycles):
    with pytest.raises(ValueError, match='REFINEMENT_LIMIT'):
        store.refinement_gate({'state': 'GENERATED_CANDIDATE', 'refinement_cycles': cycles})


@pytest.mark.parametrize('state', ['ACCEPTED', 'REJECTED'])
def test_terminal_candidates_are_retained_and_immutable(state):
    with pytest.raises(ValueError, match='GENERATION_TERMINAL'):
        store.mutable({'state': state})


@pytest.mark.parametrize('render', [None, {'status': 'ok', 'temporal_complete': False}, {'status': 'error', 'temporal_complete': True}])
def test_stills_or_failed_render_cannot_substitute_temporal_evidence(render):
    with pytest.raises(ValueError, match='TEMPORAL_REVIEW_REQUIRED'):
        store.review_gate({}, render)


def test_visual_review_binds_current_artifact_and_explicit_observations():
    render = {'status': 'ok', 'temporal_complete': True, 'output_dir': 'current'}
    review = {'reviewer': 'human', 'observations': ['Weight reads in recovery'], 'findings': [],
              'decision': 'ACCEPT', 'artifact_reference': 'old', 'temporal_reviewed': True}
    with pytest.raises(ValueError, match='VISUAL_REVIEW_STALE'):
        store.review_gate(review, render)
    review['artifact_reference'] = 'current'
    assert store.review_gate(review, render) == review


def test_generation_reference_confined_to_workspace(tmp_path):
    a, b = Storage(tmp_path/'a'), Storage(tmp_path/'b')
    reference, _ = store.save(a, 'test-store-a', {})
    with pytest.raises(ValueError, match='INVALID_GENERATION_REFERENCE'):
        store.load(b, reference)


def test_temporal_apng_preserves_frame_pixels_and_timing(tmp_path):
    import struct
    import zlib
    header = struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)
    files = []
    for i, value in enumerate((b'\xff\x00\x00\xff', b'\x00\xff\x00\xff')):
        path = tmp_path / f'{i}.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\n'+_chunk(b'IHDR', header)+_chunk(b'IDAT', zlib.compress(b'\x00'+value))+_chunk(b'IEND', b''))
        files.append({'path': str(path)})
    output = tmp_path/'animated.png'
    write_apng(files, output, 30)
    raw = output.read_bytes()
    assert b'acTL' in raw and raw.count(b'fcTL') == 2 and b'fdAT' in raw
    with pytest.raises(FileExistsError):
        write_apng(files, output, 30)


def test_required_high_level_inventory_is_35_and_preserves_regression_inventory():
    import asyncio
    from blender_mcp.production.animation.generation.tools import NAMES
    from blender_mcp.production.server import mcp
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    from blender_mcp.production.facial.tools import NAMES as M7_NAMES
    from blender_mcp.production.physics.tools import NAMES as M8_NAMES
    from blender_mcp.production.materials.tools import NAMES as M9_NAMES
    # M7/M8/M9 extend the catalogue; all closed M6 contracts remain present.
    assert len(NAMES) == 35 and len(M7_NAMES) == 46 and len(M8_NAMES)==35 and len(M9_NAMES)==38 and len(names) == 155 + 46 + 35 + 38
    assert {'production.'+n for n in NAMES} <= names
    assert {'production.'+n for n in set(M7_NAMES) | M8_NAMES} <= names
    assert {'production.'+n for n in M9_NAMES} <= names
    assert 'production.scene_audit' in names and 'production.bake_smoke_test' in names
