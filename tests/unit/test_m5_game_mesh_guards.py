"""Aggregation/package acceptance rejects stale evidence before publishing manifests."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from blender_mcp.production import checkpoint
from blender_mcp.production.bake import service as bake
from blender_mcp.production.common import m5
from blender_mcp.production.common.storage import Storage, sha256, write_json
from blender_mcp.production.game_mesh import package, validation


@pytest.fixture
def mesh_case(tmp_path, monkeypatch):
    """Small immutable manifest with real bake-pair integrity/freshness checks."""
    storage = Storage(tmp_path / 'workspace')
    low = {'object': 'LOW', 'fingerprint': 'low-before', 'geometry_fingerprint': 'low-geometry',
           'counts': {'polygons': 12, 'triangles': 12}, 'armatures': [], 'materials': ['Body']}
    high = dict(deepcopy(low), object='HIGH', fingerprint='high-before', geometry_fingerprint='high-geometry')
    cage = dict(deepcopy(low), object='CAGE', fingerprint='cage-before')
    evaluation = dict(deepcopy(low), object='TRI_LOW', fingerprint='tri-before')
    state = {'HIGH': high, 'LOW': low, 'CAGE': cage, 'TRI_LOW': evaluation}
    relationship = {'relationship_id': 'high-low', 'asset_id': 'fixture', 'source_object': 'HIGH',
                    'target_object': 'LOW', 'source_signature': deepcopy(high)}
    tri_relationship = dict(deepcopy(relationship), relationship_id='high-tri', target_object='TRI_LOW')
    events = []

    def signature(bridge, name):
        return deepcopy(state[name])

    def relation(bridge, store, path):
        assert store is storage
        return deepcopy(tri_relationship if path == 'tri-relationship' else relationship)

    for module in (m5, validation, package):
        monkeypatch.setattr(module, 'signature', signature)
        monkeypatch.setattr(module, 'validate_relationship', relation)
    pair = {'schema': 'production.bake_pair.v1', 'pair_id': 'bake-pair', 'relationship': 'relationship',
            'relationship_id': relationship['relationship_id'], 'high': 'HIGH', 'low': 'LOW',
            'high_signature': deepcopy(high), 'low_signature': deepcopy(low), 'cage_signature': deepcopy(cage),
            'readiness': 'READY', 'options': {'cage_object': 'CAGE', 'front_distance_m': .01,
                                            'rear_distance_m': .01, 'max_ray_distance_m': .02},
            'report': {'passed': True, 'errors': [], 'warnings': [],
                       'manifest': {'UDIM_tiles': [1001], 'texture_sets': ['Body']}}}
    folder = storage.root / 'bake' / 'frozen-pair'
    folder.mkdir(parents=True)
    pair_path = folder / 'pair.json'
    write_json(pair_path, pair)
    write_json(folder / 'integrity.json', {'sha256': sha256(pair_path)})

    report = {'passed': True, 'ignored_gates': [], 'signature': deepcopy(low),
              'subsystems': {'uv': {'report': {'tiles': [1001]}}, 'weights': {'status': 'NOT_APPLICABLE'},
                             'morphs': {'status': 'NOT_APPLICABLE'}, 'materials_attributes': {'passed': True}}}

    def aggregate(*args):
        events.append('aggregate')
        return deepcopy(report)

    def mutable(*args):
        events.append('asset_guard')
        return {'asset_id': 'fixture'}

    def checkpoint_created(*args):
        events.append('checkpoint')
        return {'manifest': 'fixture-checkpoint'}

    monkeypatch.setattr(package, 'validate', aggregate)
    monkeypatch.setattr(package, 'require_mutable_asset', mutable)
    monkeypatch.setattr(checkpoint, 'create_checkpoint', checkpoint_created)
    return SimpleNamespace(bridge=object(), storage=storage, state=state, report=report,
                           pair=pair_path, events=events, aggregate=aggregate, checkpoint=checkpoint_created)


def prepare(case, **kwargs):
    return package.prepare(case.bridge, case.storage, 'package-test', 'relationship', str(case.pair),
                           dry_run=False, **kwargs)


def forbid_package_writes(case, monkeypatch):
    """A rejection must happen before allocating or writing any package output."""
    def reserve(*args):
        pytest.fail('rejected package attempted to reserve an output directory')

    def write(*args):
        pytest.fail('rejected package attempted to write a manifest')

    monkeypatch.setattr(case.storage, 'reserve', reserve)
    monkeypatch.setattr(package, 'write_json', write)


def aggregate_dependencies(monkeypatch, on_bake_read=None, bake_report=None):
    """Keep subsystem evidence valid so only the aggregate snapshot guard is exercised."""
    from blender_mcp.production.retopology import service as retopology
    from blender_mcp.production.transfer import service as transfer
    from blender_mcp.production.uv import service as uv
    from blender_mcp.production.game_mesh import service as game

    monkeypatch.setattr(retopology, 'compare', lambda *a: {'passed': True})
    monkeypatch.setattr(transfer, 'analyze_morphs', lambda *a: {'errors': []})
    monkeypatch.setattr(transfer, 'options', lambda values, kind: dict(values))
    monkeypatch.setattr(validation, 'm5_read', lambda *a: {'passed': True})
    monkeypatch.setattr(game, 'validate', lambda *a: {'passed': True})
    monkeypatch.setattr(uv, 'analyze', lambda *a: {'valid': True, 'tiles': [1001], 'invalid_udims': []})

    def bake_read(*args):
        if on_bake_read:
            on_bake_read()
        return deepcopy(bake_report) if bake_report is not None else {'passed': True, 'readiness': 'READY', 'errors': []}

    monkeypatch.setattr(bake, 'read', bake_read)


def test_aggregate_rejects_low_changed_after_individually_passing_subsystems(mesh_case, monkeypatch):
    def concurrent_edit():
        mesh_case.state['LOW']['fingerprint'] = 'low-concurrently-edited'

    aggregate_dependencies(monkeypatch, concurrent_edit)
    report = validation.validate(mesh_case.bridge, mesh_case.storage, 'relationship',
                                 {'retopology': {'maximum_triangles': 12}})
    assert all(row['passed'] is True for row in report['subsystems'].values())
    assert report['signature']['fingerprint'] == 'low-before'
    assert report['final_signature']['fingerprint'] == 'low-concurrently-edited'
    assert report['validation_snapshot_stable'] is False
    assert report['source_intact'] is True
    assert report['passed'] is False and report['numeric_verification'] == 'fail'
    assert any('STALE_VALIDATION' in error for error in report['errors'])


def test_aggregate_accepts_unchanged_snapshot_with_all_required_evidence(mesh_case, monkeypatch):
    aggregate_dependencies(monkeypatch)
    report = validation.validate(mesh_case.bridge, mesh_case.storage, 'relationship',
                                 {'retopology': {'maximum_triangles': 12}})
    assert report['passed'] is True and report['validation_snapshot_stable'] is True
    assert report['signature']['fingerprint'] == report['final_signature']['fingerprint']


@pytest.mark.parametrize('readiness,numeric_pass,expected', [
    ('NOT_READY', True, False), ('READY_WITH_WARNINGS', False, False),
    ('READY_WITH_WARNINGS', True, True),
])
def test_aggregate_bake_readiness_requires_ready_state_and_numeric_pass_preserving_warnings(
        mesh_case, monkeypatch, readiness, numeric_pass, expected):
    warning = 'Controlled ray warning remains visible in accepted evidence.'
    bake_report = {'passed': numeric_pass, 'readiness': readiness, 'errors': [], 'warnings': [warning]}
    aggregate_dependencies(monkeypatch, bake_report=bake_report)
    report = validation.validate(mesh_case.bridge, mesh_case.storage, 'relationship',
                                 {'retopology': {'maximum_triangles': 12}})
    assert report['passed'] is expected and report['subsystems']['bake']['passed'] is expected
    assert report['subsystems']['bake']['report']['warnings'] == [warning]
    assert report['subsystems']['bake']['report']['readiness'] == readiness


@pytest.mark.parametrize('phase', ['after_validation', 'after_checkpoint'])
@pytest.mark.parametrize('object_name', ['LOW', 'CAGE'])
def test_package_rejects_stale_bake_pair_before_writes(mesh_case, monkeypatch, phase, object_name):
    # The real load_pair compares a frozen, integrity-protected manifest with the current
    # LOW/cage fingerprints. Changing a ray dependency must invalidate acceptance too.
    def edit():
        mesh_case.state[object_name]['fingerprint'] += '-edited'

    if phase == 'after_validation':
        def validate_then_edit(*args):
            result = mesh_case.aggregate(*args)
            edit()
            return result
        monkeypatch.setattr(package, 'validate', validate_then_edit)
    else:
        def checkpoint_then_edit(*args):
            result = mesh_case.checkpoint(*args)
            edit()
            return result
        monkeypatch.setattr(checkpoint, 'create_checkpoint', checkpoint_then_edit)
    initial_files = sorted(str(path.relative_to(mesh_case.storage.root))
                           for path in mesh_case.storage.root.rglob('*') if path.is_file())
    forbid_package_writes(mesh_case, monkeypatch)
    with pytest.raises(ValueError, match='STALE_BAKE_PAIR'):
        prepare(mesh_case)
    assert ('checkpoint' in mesh_case.events) is (phase == 'after_checkpoint')
    assert sorted(str(path.relative_to(mesh_case.storage.root))
                  for path in mesh_case.storage.root.rglob('*') if path.is_file()) == initial_files


def test_package_rejects_different_triangulated_low_when_surface_fidelity_fails(mesh_case, monkeypatch):
    comparisons = []

    def excessive_deviation(bridge, store, relationship, runtime, body, options):
        comparisons.append((relationship, body, options))
        return {'passed': False, 'metrics': {'surface_m': {'maximum': .5}},
                'errors': ['SURFACE_DEVIATION: 0.5 m exceeds explicit tolerance']}

    monkeypatch.setattr(m5, 'm5_read', excessive_deviation)
    forbid_package_writes(mesh_case, monkeypatch)
    with pytest.raises(ValueError, match='TRIANGULATED_EVALUATION_MISMATCH'):
        prepare(mesh_case, triangulated_relationship='tri-relationship',
                options={'retopology': {'max_distance_m': .001}})
    assert len(comparisons) == 1
    relationship, body, options = comparisons[0]
    assert relationship == 'relationship' and 'm5_compare(low,' in body
    assert options['tri_object'] == 'TRI_LOW' and options['max_distance_m'] == .001
    assert not mesh_case.events


@pytest.mark.parametrize('flag,gate', [('require_bake_ready', 'bake'), ('require_deformation', 'deformation')])
def test_package_cannot_ignore_a_mandatory_gate_even_if_aggregate_reports_pass(mesh_case, monkeypatch, flag, gate):
    mesh_case.report['ignored_gates'] = [gate]
    forbid_package_writes(mesh_case, monkeypatch)
    with pytest.raises(ValueError, match='GAME_MESH_NOT_VALIDATED'):
        prepare(mesh_case, options={flag: False})
    assert mesh_case.events == ['aggregate']


def test_fresh_package_writes_local_manifests_after_rechecking_pair_and_checkpoint(mesh_case):
    result = prepare(mesh_case)
    assert result['status'] == 'success' and result['source_intact'] is True
    assert result['readiness'] == 'READY_NUMERIC_VISUAL_PENDING'
    assert mesh_case.events == ['aggregate', 'asset_guard', 'checkpoint']
    package_path, handoff_path = Path(result['package']), Path(result['handoff'])
    assert package_path.is_file() and handoff_path.is_file()
    assert (package_path.parent / 'integrity.json').is_file()
    assert package_path.is_relative_to(mesh_case.storage.root / 'game_mesh')


@pytest.mark.parametrize('readiness,numeric_pass,expected', [
    ('NOT_READY', True, False), ('READY_WITH_WARNINGS', False, False),
    ('READY_WITH_WARNINGS', True, True),
])
def test_package_bake_pair_readiness_cannot_replace_numeric_verification(
        mesh_case, monkeypatch, readiness, numeric_pass, expected):
    # This is the test's generated frozen manifest; integrity is recalculated exactly as
    # when producing a real pair, so the readiness guard cannot pass via integrity failure.
    pair = json.loads(mesh_case.pair.read_text(encoding='utf8'))
    pair['readiness'] = readiness
    pair['report']['passed'] = numeric_pass
    pair['report']['warnings'] = ['Controlled bake warning']
    mesh_case.pair.write_text(json.dumps(pair), encoding='utf8')
    (mesh_case.pair.parent / 'integrity.json').write_text(json.dumps({'sha256': sha256(mesh_case.pair)}), encoding='utf8')
    if not expected:
        forbid_package_writes(mesh_case, monkeypatch)
        with pytest.raises(ValueError, match='BAKE_NOT_READY'):
            prepare(mesh_case)
        assert not mesh_case.events
    else:
        result = prepare(mesh_case)
        assert result['status'] == 'success'
        frozen = bake.load_pair(mesh_case.bridge, mesh_case.storage, str(mesh_case.pair))
        assert frozen['report']['warnings'] == ['Controlled bake warning']
        manifest = json.loads(Path(result['package']).read_text(encoding='utf8'))
        assert manifest['bake_manifest'] == str(mesh_case.pair)
