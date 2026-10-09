"""Runner guard contracts; real rejection qualification remains an MCP gate."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def runner(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / 'integration'
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location('m8_refinement_runner_test', directory / 'run_m8_mcp.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RejectingRun:
    def __init__(self, folder, mutation=None, fresh=True, code='PHYSICS_REFINEMENT_TARGET_UNSUPPORTED'):
        self.folder = folder
        self.mutation = mutation
        self.code = code
        self.report = {'calls': [], 'checks': {}}
        self.status = {'physics': 'same/plan.json', 'generation_id': 'same-generation',
            'state': 'REVIEWED', 'version': 1, 'refinement_cycles': 0,
            'candidate': {'candidate_object': 'owned-candidate'},
            'candidate_action': 'OwnedMotion', 'checkpoint': 'same/manifest.json',
            'cache': {'sha256': 'cache-digest'}, 'bake_deviation': {'passed': True},
            'pending_refinement': False, 'fresh': fresh}
        self.totals = {'objects': 5, 'actions': 2}
        self.targets = []

    async def good(self, name, arguments=None):
        if name == 'physics_status':
            return deepcopy(self.status)
        assert name == 'scene_audit'
        return {'totals': dict(self.totals)}

    async def call(self, name, arguments):
        assert name == 'physics_refine' and arguments['apply'] is True
        assert arguments['physics'] == 'same/plan.json'
        self.report['calls'].append({'tool': name})
        self.targets.append(arguments['options']['target'])
        if self.mutation == 'cycle':
            self.status['refinement_cycles'] += 1
        elif self.mutation == 'candidate':
            self.status['candidate']['candidate_object'] = 'changed-candidate'
        elif self.mutation == 'action':
            self.totals['actions'] += 1
        elif self.mutation == 'checkpoint':
            root = self.folder / 's/checkpoints'
            root.mkdir(parents=True, exist_ok=True)
            (root / 'unexpected.json').write_text('new checkpoint', encoding='utf-8')
        return {'status': 'error', 'errors': [{'message': self.code}]}

    def check(self, name, passed, **details):
        self.report['checks'][name] = {'passed': bool(passed), **details}


def test_invalid_targets_retain_same_candidate_counter_and_checkpoints(runner, tmp_path):
    root = tmp_path / 's/checkpoints/initial'
    root.mkdir(parents=True)
    (root / 'scene.blend').write_bytes(b'synthetic checkpoint')
    run = RejectingRun(tmp_path)
    original = deepcopy(run.status)
    asyncio.run(runner.reject_ambiguous_refinement_targets(run, 'same/plan.json'))
    assert run.targets == ['min_area_ratio', 'absolute_volume_ratio', 'bbox_growth_ratio']
    assert run.status == original
    assert len(run.report['unsupported_refinement_target_checks']) == 3
    assert all(c['passed'] for c in run.report['checks'].values())
    assert (root / 'scene.blend').read_bytes() == b'synthetic checkpoint'


@pytest.mark.parametrize('mutation', ['cycle', 'candidate', 'action', 'checkpoint'])
def test_rejection_that_mutates_state_stops_before_legal_refinement(runner, tmp_path, mutation):
    run = RejectingRun(tmp_path, mutation=mutation)
    with pytest.raises(AssertionError, match='INVALID_REFINEMENT_TARGET_MUTATED_STATE'):
        asyncio.run(runner.reject_ambiguous_refinement_targets(run, 'same/plan.json'))
    assert run.targets == ['min_area_ratio']
    assert run.report['unsupported_refinement_target_checks'][0]['unchanged'] is False


def test_wrong_error_does_not_count_as_target_guard_pass(runner, tmp_path):
    run = RejectingRun(tmp_path, code='OTHER_ERROR')
    with pytest.raises(AssertionError, match='did not safely reject'):
        asyncio.run(runner.reject_ambiguous_refinement_targets(run, 'same/plan.json'))
    assert not run.report['unsupported_refinement_target_checks']


def test_stale_candidate_blocks_negative_before_mutating_call(runner, tmp_path):
    run = RejectingRun(tmp_path, fresh=False)
    with pytest.raises(AssertionError, match='REFINEMENT_NEGATIVE_REQUIRES_FRESH_REVIEWED_CANDIDATE'):
        asyncio.run(runner.reject_ambiguous_refinement_targets(run, 'same/plan.json'))
    assert run.targets == []
