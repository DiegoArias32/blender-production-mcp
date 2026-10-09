"""Safety boundaries of persisted material plans, review artifacts and refinement."""
from copy import deepcopy
from pathlib import Path

import pytest

from blender_mcp.production.common.storage import Storage, sha256
from blender_mcp.production.materials import service as s


@pytest.fixture
def context(tmp_path, monkeypatch):
    storage = Storage(tmp_path / 'store')
    asset = {'asset_id': 'fixture-id', 'role': 'FIXTURE', 'path': '/synthetic/fixture.blend', 'mutable': True}
    status = {'fingerprint': 'source-v1', 'runtime_fingerprint': 'runtime-v1',
              'geometry_fingerprint': 'geo-v1', 'context': {'units': 1}}
    monkeypatch.setattr(s, 'backend_fingerprint', lambda: 'backend-v1')
    monkeypatch.setattr(s, 'current_asset', lambda *a: deepcopy(asset))
    monkeypatch.setattr(s, 'require_mutable_asset', lambda *a: deepcopy(asset))
    monkeypatch.setattr(s.integrity, 'status', lambda *a, **k: deepcopy(status))
    monkeypatch.setattr(s.inspection, 'inspect', lambda *a, **k: {})
    monkeypatch.setattr(s.inspection, 'classify', lambda *a: {'classification': 'UNKNOWN'})
    path, plan, state = s.plan_material(None, storage, 'test-plan', 'Fixture', 'Source', {}, None)
    return storage, path, plan, state, asset, status


def test_plan_is_signed_and_rejects_content_tampering(context):
    storage, path, plan, state, _, _ = context
    text = path.read_text(encoding='utf-8')
    path.write_text(text.replace('Source', 'Changed'), encoding='utf-8')
    with pytest.raises(ValueError, match='STALE_PROPOSAL'):
        s.load(storage, str(path))


@pytest.mark.parametrize('field', ['asset_id', 'role', 'path'])
def test_asset_identity_cannot_change_after_plan(context, field):
    storage, path, plan, state, asset, _ = context
    asset[field] = 'unrelated'
    with pytest.raises(ValueError, match='MATERIAL_ASSET_CHANGED'):
        s.fresh(None, storage, plan, state)


def test_changed_source_blocks_before_any_checkpoint(context, monkeypatch):
    storage, path, plan, state, _, status = context
    status['fingerprint'] = 'source-v2'
    calls = []
    monkeypatch.setattr(s, 'checkpoint', lambda *a: calls.append(a))
    with pytest.raises(ValueError, match='MATERIAL_STALE_SOURCE'):
        s.ensure_candidate(None, storage, path, plan, state, 'must-not-write')
    assert calls == []


def test_new_backend_rejects_old_plan(context, monkeypatch):
    storage, _, plan, state, _, _ = context
    monkeypatch.setattr(s, 'backend_fingerprint', lambda: 'backend-v2')
    with pytest.raises(ValueError, match='MATERIAL_BACKEND_CHANGED'):
        s.fresh(None, storage, plan, state)


def test_uncertain_result_blocks_new_operation_keys(context, monkeypatch):
    storage, path, plan, state, _, _ = context
    state['uncertain_operation'] = True
    s.event(path, state)
    with pytest.raises(ValueError, match='OPERATION_OUTCOME_UNCERTAIN'):
        s.dispatch('material_create_candidate', None, storage, 'brand-new-key', material=str(path), apply=True)


@pytest.mark.parametrize('terminal', ['ACCEPTED', 'REJECTED'])
def test_terminal_candidate_cannot_be_resurrected(context, terminal):
    storage, path, plan, state, _, _ = context
    state['state'] = terminal
    with pytest.raises(ValueError, match='GENERATION_TERMINAL'):
        s.ensure_candidate(None, storage, path, plan, state, 'not-allowed')


def test_master_rejected_before_checkpoint_and_candidate(context, monkeypatch):
    storage, path, plan, state, _, _ = context
    events = []
    def forbidden(*args):
        raise PermissionError('MASTER_ASSET_READ_ONLY: fixture negative')
    monkeypatch.setattr(s, 'require_mutable_asset', forbidden)
    monkeypatch.setattr(s, 'checkpoint', lambda *a: events.append('checkpoint'))
    monkeypatch.setattr(s.candidate, 'create', lambda *a: events.append('candidate'))
    with pytest.raises(PermissionError, match='MASTER_ASSET_READ_ONLY'):
        s.create_version(None, storage, path, plan, state, 'master-no-write', state['definition'], build=True)
    assert events == []


@pytest.fixture
def rendered(tmp_path):
    folder = tmp_path / 'renders'
    folder.mkdir()
    files = []
    for light in sorted(s.LIGHTS):
        for variant in ('SOURCE', 'CANDIDATE'):
            p = folder / (light + '-' + variant + '.png')
            # Artifact integrity only; these synthetic bytes do not qualify a real visual review.
            p.write_bytes(b'fixture-unit-test:' + light.encode() + variant.encode())
            files.append({'path': str(p), 'sha256': sha256(p), 'lighting': light, 'variant': variant})
    return {'status': 'success', 'output_dir': str(folder), 'files': files,
            'lighting_set': sorted(s.LIGHTS), 'state_restored': True}


def feedback(rendered, decision='ACCEPT'):
    return {'reviewer': 'unit-test-not-artistic-evidence', 'observations': 'Contract testing only',
            'findings': [], 'decision': decision, 'artifact_reference': rendered['output_dir'],
            'lights_reviewed': sorted(s.LIGHTS)}


def test_render_hash_tampering_blocks_review(rendered):
    Path(rendered['files'][0]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='MATERIAL_RENDER_STALE'):
        s.review_gate(feedback(rendered), rendered)


def test_each_light_requires_both_original_and_candidate(rendered):
    rendered['files'][-1] = dict(rendered['files'][0])
    with pytest.raises(ValueError, match='MATERIAL_RENDER_PAIRS_REQUIRED'):
        s.verify_render(rendered)


@pytest.mark.parametrize('field, value', [('reviewer', ''), ('observations', '  '),
                                        ('decision', 'PASS'), ('findings', 'not-a-list'),
                                        ('artifact_reference', 'older-render'),
                                        ('lights_reviewed', ['NEUTRAL'])])
def test_incomplete_or_stale_visual_feedback_is_rejected(rendered, field, value):
    review = feedback(rendered)
    review[field] = value
    with pytest.raises(ValueError):
        s.review_gate(review, rendered)


def test_failed_state_restoration_cannot_be_visual_evidence(rendered):
    rendered['state_restored'] = False
    with pytest.raises(ValueError, match='MATERIAL_VISUAL_RENDER_REQUIRED'):
        s.review_gate(feedback(rendered), rendered)


def test_refinement_limit_persists_even_after_failed_creation(context, monkeypatch, rendered):
    storage, path, plan, state, _, _ = context
    state.update(review=feedback(rendered, 'REFINE'), render=rendered, refinement_cycles=2)
    proposed = deepcopy(state['definition'])
    proposed['parameters']['roughness'] = .2
    def fail(*args, **kwargs):
        raise RuntimeError('controlled construction failure')
    monkeypatch.setattr(s, 'create_version', fail)
    with pytest.raises(RuntimeError, match='controlled'):
        s.refine(None, storage, path, plan, state, 'third-attempt', proposed, None, {'reason': 'highlight adjustment'})
    saved = s.load(storage, str(path))[2]
    assert saved['refinement_cycles'] == 3
    with pytest.raises(ValueError, match='REFINEMENT_LIMIT'):
        s.refine(None, storage, path, plan, saved, 'fourth-attempt', proposed, None, {'reason': 'one more'})


def test_build_cannot_bypass_refinement_after_render(context, rendered):
    storage, path, plan, state, _, _ = context
    state['render'] = rendered
    proposed = deepcopy(state['definition'])
    proposed['parameters']['roughness'] = .15
    with pytest.raises(ValueError, match='MATERIAL_REFINEMENT_REQUIRED'):
        s.build_material('material_build_shader', None, storage, path, plan, state, 'bypass', proposed, None, {})


def test_material_plan_reference_cannot_target_another_object(context):
    storage, path, *_ = context
    with pytest.raises(ValueError, match='MATERIAL_PLAN_TARGET_MISMATCH'):
        s.dispatch('material_status', None, storage, 'status-test', material=str(path), object_name='Other')


def test_feature_configuration_cannot_change_unrelated_shader_parameters():
    with pytest.raises(ValueError, match='MATERIAL_FEATURE_PARAMETER_UNSUPPORTED'):
        s.modified_definition('material_configure_emission', {'definition': s.normalized()},
                              None, None, {'parameters': {'metallic': 1}})


def test_apply_rejects_non_boolean_before_io():
    with pytest.raises(ValueError, match='MATERIAL_APPLY_BOOLEAN_REQUIRED'):
        s.dispatch('material_create_pbr', None, None, 'bad-bool', apply='yes')


def test_uncertain_status_exposes_recovery_without_allowing_mutation(context):
    storage, path, plan, state, _, _ = context
    state.update(uncertain_operation=True, pending_operation='lost-transport', last_error='socket timeout',
                 checkpoint='preserved-recovery-manifest')
    s.event(path, state)
    report = s.dispatch('material_status', None, storage, 'diagnostic', material=str(path))
    assert report['status'] == 'error' and report['freshness'] == 'blocked'
    assert report['pending_operation'] == 'lost-transport'
    assert report['checkpoint'] == 'preserved-recovery-manifest'
    assert report['errors'][0]['code'] == 'OPERATION_OUTCOME_UNCERTAIN'


def test_feature_dry_run_shows_requested_values_and_does_not_change_ledger(context):
    storage, path, plan, state, _, _ = context
    events = list(path.parent.glob('event-*'))
    result = s.dispatch('material_configure_sss', None, storage, 'dry-run', material=str(path),
                        options={'parameters': {'subsurface_weight': .37}}, apply=False)
    assert result['changes']['before']['parameters']['subsurface_weight'] == 0
    assert result['changes']['after']['parameters']['subsurface_weight'] == .37
    assert list(path.parent.glob('event-*')) == events
    assert s.load(storage, str(path))[2]['definition']['parameters']['subsurface_weight'] == 0


def test_render_cannot_qualify_cycles_material_with_eevee(context, monkeypatch):
    storage, path, plan, state, _, _ = context
    state['definition']['renderer'] = 'CYCLES'
    calls = []
    monkeypatch.setattr(s, 'checkpoint', lambda *a: calls.append('checkpoint'))
    monkeypatch.setattr(s, 'technical_qa', lambda *a: calls.append('qa'))
    with pytest.raises(ValueError, match='MATERIAL_RENDERER_CONFLICT'):
        s.render_material(None, storage, path, plan, state, 'conflict', {'engine': 'BLENDER_EEVEE'})
    assert calls == []


def test_render_default_follows_validated_definition(context, monkeypatch, rendered):
    storage, path, plan, state, _, _ = context
    state.update(candidate={'candidate_material': 'Owned'}, candidate_status={'fingerprint': 'test'})
    state['definition']['renderer'] = 'CYCLES'
    seen = []
    monkeypatch.setattr(s, 'fresh', lambda *a: None)
    monkeypatch.setattr(s, 'technical_qa', lambda *a: {'passed': True, 'errors': []})
    monkeypatch.setattr(s, 'checkpoint', lambda *a: 'unit-checkpoint')
    def fake_render(*args):
        seen.append(args[-1]['engine'])
        return rendered
    monkeypatch.setattr(s.render, 'render', fake_render)
    s.render_material(None, storage, path, plan, state, 'cycles-default', {})
    assert seen == ['CYCLES']
