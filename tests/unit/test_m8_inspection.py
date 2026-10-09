"""Confidence is evidence-based and explicit authorization scopes soft motion."""
import ast

import pytest

from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.physics.inspection import INSPECT, classify, compatibility, inspect


def summary(**changes):
    value = {'object': 'Fixture', 'source_supported': True, 'features': [], 'geometry': {'vertices': 81},
        'hierarchy': {}, 'rig_relationship': [], 'collision_requirements': ['explicit colliders']}
    value.update(changes)
    return value


def test_geometry_and_partial_attachment_evidence_identifies_cloth():
    report = classify(summary(features=['OPEN_SURFACE', 'THIN_GEOMETRY', 'PARTIAL_BOUNDARY_GROUP']))
    assert report['classification'] == 'CLOTH' and report['automatic_setup_allowed']
    assert report['confidence'] == .83


def test_names_and_caller_confidence_cannot_create_evidence():
    report = classify(summary(object='hair_cloth_soft_chain'), {'confidence': 1.})
    assert report['classification'] == 'AMBIGUOUS' and not report['automatic_setup_allowed']
    assert any(e['kind'] == 'NAME_SECONDARY_ONLY' for e in report['evidence'])
    cloth = summary(features=['OPEN_SURFACE', 'THIN_GEOMETRY', 'PARTIAL_BOUNDARY_GROUP'])
    assert classify(cloth, {'confidence': .3})['confidence'] == .3
    assert classify(cloth, {'confidence': 1.})['confidence'] == .83


def test_unattached_open_sheet_and_edge_chain_block_automatic_setup():
    for features in (['OPEN_SURFACE', 'THIN_GEOMETRY'], ['CONNECTED_EDGE_STRUCTURE']):
        report = classify(summary(features=features))
        assert report['classification'] == 'AMBIGUOUS' and not report['automatic_setup_allowed']


def test_manifest_object_scope_and_soft_authorized_mask():
    manifest = {'objects': {'Fixture': {'classification': 'SOFT_REGION', 'authorized': True, 'authorized_mask': [1, 2]}}}
    assert classify(summary(), manifest)['automatic_setup_allowed']
    assert not classify(summary(object='Other'), manifest)['automatic_setup_allowed']
    for entry in ({'classification': 'SOFT_REGION'}, {'classification': 'SOFT_REGION', 'authorized': True},
                  {'classification': 'SOFT_REGION', 'authorized': False, 'authorized_mask': [1]}):
        report = classify(summary(), entry)
        assert report['classification'] == 'AMBIGUOUS' and not report['automatic_setup_allowed']


def test_native_hair_never_claimed_supported_and_chain_required():
    assert not classify(summary(), {'classification': 'HAIR'})['automatic_setup_allowed']
    classified = classify(summary(chain={'armature': 'Rig', 'bones': ['Hair']}), {'classification': 'HAIR'})
    report = compatibility(summary(), classified)
    assert report['capabilities']['hair_secondary']['status'] == 'LIMITED'
    assert report['capabilities']['native_hair']['status'] == 'UNSUPPORTED_BY_CURRENT_BLENDER_API'


def test_explicit_api_absence_and_unsupported_source_disable_capabilities():
    for report in (compatibility(summary(), api={}), compatibility(summary(source_supported=False))):
        assert report['capabilities']['cloth']['status'] == 'UNSUPPORTED_BY_CURRENT_BLENDER_API'
        assert not report['automatic_setup_allowed']


@pytest.mark.parametrize('confidence', [True, -1, 2, float('nan'), float('inf')])
def test_invalid_caller_confidence_rejects(confidence):
    with pytest.raises(ValueError, match='PHYSICS_CONFIDENCE_INVALID'):
        classify(summary(), {'confidence': confidence})


def test_inspection_payload_and_options_are_safe_data():
    class Bridge:
        def run(self, body, params):
            code = build_code(body, params)
            assert len(code.encode()) < 50000 and sum(1 for _ in ast.walk(ast.parse(code))) < 9000
            assert params['chain']['bones'] == ['Escaped"Bone']
            return {'inspected': True}
    assert inspect(Bridge(), 'Fixture', {'chain': {'armature': 'Rig', 'bones': ['Escaped"Bone']}})['inspected']
