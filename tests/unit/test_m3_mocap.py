"""Safety contracts for reviewed mocap mappings and dispatch, without Blender.

M3.1 (AUD-003): mapping confidence is computed from both rigs' rest geometry and hierarchy; the
row 'confidence' is a caller hint that can only lower it.
"""
import copy
import math
import pytest

from blender_mcp.production.mocap.mapping import validate_mapping, mapping_evidence, map_body_frame
from blender_mcp.production.mocap.service import BASE_RUNTIME, RUNTIME, ANALYZE, cleanup
from blender_mcp.production.common.bridge import build_code

ROLES = ['root','pelvis','spine','head'] + [side+'_'+part for side in ('left','right') for part in ('upper_arm','lower_arm','hand','upper_leg','lower_leg','foot')]


def rig(prefix, scale=1.0, arm_drop=0.0):
    """Synthetic T-pose biped facing -Y (left = +X); arm_drop lowers the arm tails (A-pose)."""
    rows = [('root', None, (0,0,0), (0,0,.2)), ('pelvis', 'root', (0,0,1), (0,0,1.2)),
            ('spine', 'pelvis', (0,0,1.2), (0,0,1.6)), ('neck', 'spine', (0,0,1.6), (0,0,1.7)),
            ('head', 'neck', (0,0,1.7), (0,0,1.95))]
    for side, sign in (('left', 1), ('right', -1)):
        rows += [(side+'_upper_leg', 'pelvis', (.15*sign,0,1), (.15*sign,-.025,.55)),
                 (side+'_lower_leg', side+'_upper_leg', (.15*sign,-.025,.55), (.15*sign,0,.1)),
                 (side+'_foot', side+'_lower_leg', (.15*sign,0,.1), (.15*sign,-.22,.05)),
                 (side+'_toe', side+'_foot', (.15*sign,-.22,.05), (.15*sign,-.35,.05)),
                 (side+'_upper_arm', 'spine', (.1*sign,0,1.55), (.45*sign,0,1.55-arm_drop)),
                 (side+'_lower_arm', side+'_upper_arm', (.45*sign,0,1.55-arm_drop), (.78*sign,0,1.55-2*arm_drop)),
                 (side+'_hand', side+'_lower_arm', (.78*sign,0,1.55-2*arm_drop), (.93*sign,0,1.55-2.4*arm_drop))]
    bones = {}
    for role, parent, head, tail in rows:
        head = [v*scale for v in head]; tail = [v*scale for v in tail]
        bones[prefix+role] = {'parent': prefix+parent if parent else None, 'head': head, 'tail': tail,
                              'length': math.dist(head, tail)}
    return bones


SOURCE, TARGET = rig('src_'), rig('dst_', scale=1.2, arm_drop=.12)


def mapping():
    return [{'source_bone':'src_'+role,'target_bone':'dst_'+role,'semantic_role':role,'confidence':.95,
             'evidence':['hierarchy','spatial rest pose']} for role in ROLES]


def validate(rows, with_rigs=True, source=None, target=None, **kwargs):
    source = source or SOURCE; target = target or TARGET
    if with_rigs:
        kwargs.update(source_bones=source, target_bones=target)
    return validate_mapping(rows, list(source), list(target), **kwargs)


def swap(rows, first, second):
    rows = copy.deepcopy(rows)
    by_role = {row['semantic_role']: row for row in rows}
    by_role[first]['target_bone'], by_role[second]['target_bone'] = by_role[second]['target_bone'], by_role[first]['target_bone']
    return rows


def test_complete_reviewed_mapping():
    result = validate(mapping(), approved=True)
    assert result['validated'], result['errors']
    assert all(row['computed_confidence'] >= .9 and row['computed_by'] == 'production.mapping_evidence' for row in result['mapping'])


def test_confidence_provenance_is_reported():
    row = validate(mapping(), approved=True)['mapping'][0]
    assert row['caller_hint'] == .95 and row['caller_hint_used_as'].startswith('non_authoritative')
    assert row['evidence'] and row['conflicts'] == []


def test_caller_number_is_not_evidence_without_rig_data():
    result = validate(mapping(), with_rigs=False, approved=True)
    assert not result['validated'] and any(e.startswith('CONFIDENCE_NOT_COMPUTED') for e in result['errors'])


def test_unapproved_never_validated():
    assert not validate(mapping())['validated']


@pytest.mark.parametrize('role',ROLES)
def test_every_required_role_is_required(role):
    assert not validate([row for row in mapping() if row['semantic_role']!=role],approved=True)['validated']


@pytest.mark.parametrize('confidence',[0,.5,.89999])
def test_low_caller_hint_lowers_but_cannot_be_forced_by_approval(confidence):
    rows=mapping();rows[0]['confidence']=confidence
    result = validate(rows,approved=True)
    assert not result['validated'] and any(e.startswith('LOW_CONFIDENCE_MAPPING') for e in result['errors'])


def test_absent_caller_hint_is_allowed():
    rows=mapping()
    for row in rows:
        row.pop('confidence')
    assert validate(rows, approved=True)['validated']


@pytest.mark.parametrize('confidence',[True,-.1,1.1,math.nan,math.inf])
def test_invalid_confidence_rejected(confidence):
    rows=mapping();rows[0]['confidence']=confidence
    with pytest.raises(ValueError): validate(rows,approved=True)


@pytest.mark.parametrize('first,second', [('left_upper_arm', 'right_upper_arm'), ('left_foot', 'right_foot'), ('left_hand', 'right_hand')])
def test_single_left_right_inversion_blocks_even_with_maximum_hint(first, second):
    rows = swap(mapping(), first, second)
    for row in rows:
        row['confidence'] = 1.0
    result = validate(rows, approved=True)
    assert not result['validated']
    assert any(e.startswith(('SIDE_MISMATCH', 'HIERARCHY_CONFLICT')) for e in result['errors'])


def test_consistent_full_mirror_is_blocked():
    # The audit case: every left target swapped with its right counterpart (hierarchy stays isomorphic).
    rows = mapping()
    for part in ('upper_arm','lower_arm','hand','upper_leg','lower_leg','foot'):
        rows = swap(rows, 'left_'+part, 'right_'+part)
    result = validate(rows, approved=True)
    assert not result['validated'] and any(e.startswith('SIDE_MISMATCH') for e in result['errors'])


def test_arm_leg_swap_is_hierarchy_conflict():
    rows = swap(mapping(), 'left_upper_arm', 'left_upper_leg')
    assert any(e.startswith('HIERARCHY_CONFLICT') for e in validate(rows, approved=True)['errors'])


def test_pelvis_root_confusion_is_blocked():
    rows = swap(mapping(), 'root', 'pelvis')
    assert any(e.startswith('HIERARCHY_CONFLICT') for e in validate(rows, approved=True)['errors'])


def test_foot_toe_helper_confusion_is_blocked():
    rows = mapping()
    by_role = {row['semantic_role']: row for row in rows}
    by_role['left_foot']['target_bone'] = 'dst_left_toe'
    rows.append({'source_bone': 'src_left_toe', 'target_bone': 'dst_left_foot', 'semantic_role': 'left_toe'})
    assert any(e.startswith('HIERARCHY_CONFLICT') for e in validate(rows, approved=True)['errors'])


def test_head_helper_confusion_is_blocked():
    target = copy.deepcopy(TARGET)
    target['dst_head_end'] = {'parent': 'dst_head', 'head': [0,0,2.34], 'tail': [0,0,2.5], 'length': .16}
    rows = mapping()
    {row['semantic_role']: row for row in rows}['head']['target_bone'] = 'dst_head_end'
    rows.append({'source_bone': 'src_neck', 'target_bone': 'dst_head', 'semantic_role': 'head_helper'})
    result = validate(rows, approved=True, target=target)
    assert not result['validated'] and any(e.startswith('HIERARCHY_CONFLICT') for e in result['errors'])


def test_forearm_twist_confusion_is_blocked():
    # Series twist: upper_arm -> twist (short) -> forearm -> hand; mapping lower_arm to the twist.
    target = copy.deepcopy(TARGET)
    twist_head = target['dst_left_lower_arm']['head']
    target['dst_left_twist'] = {'parent': 'dst_left_upper_arm', 'head': twist_head,
                                'tail': [twist_head[0]+.03, twist_head[1], twist_head[2]], 'length': .03}
    target['dst_left_lower_arm']['parent'] = 'dst_left_twist'
    rows = mapping()
    {row['semantic_role']: row for row in rows}['left_lower_arm']['target_bone'] = 'dst_left_twist'
    rows.append({'source_bone': 'src_left_toe', 'target_bone': 'dst_left_lower_arm', 'semantic_role': 'left_forearm_extra'})
    result = validate(rows, approved=True, target=target)
    assert not result['validated']
    assert any(e.startswith(('PROPORTION_OUTLIER', 'HIERARCHY_CONFLICT')) for e in result['errors'])


def test_body_frame_is_label_independent():
    frame = map_body_frame(SOURCE, {'pelvis':'src_pelvis','head':'src_head','left_foot':'src_right_foot','right_foot':'src_left_foot'})
    assert frame['left'][0] > .99  # +X stays left even when the foot labels are swapped


@pytest.mark.parametrize('field',['source_bone','target_bone','semantic_role'])
def test_non_bijective_mapping_rejected(field):
    rows=mapping();rows[1][field]=rows[0][field]
    assert not validate(rows,approved=True)['validated']


def test_unknown_bone_rejected():
    rows=mapping();rows[0]['source_bone']='missing'
    assert not validate(rows,approved=True)['validated']


def test_caller_evidence_is_optional_and_not_authoritative():
    rows=mapping();rows[0]['evidence']=[]
    result = validate(rows,approved=True)
    assert result['validated'] and result['mapping'][0]['evidence']  # server evidence replaces it


@pytest.mark.parametrize('threshold',[.5,1.1,True])
def test_confidence_floor_cannot_be_lowered(threshold):
    with pytest.raises(ValueError): validate(mapping(),approved=True,minimum_confidence=threshold)


def test_validation_does_not_modify_caller_mapping():
    rows=mapping();before=copy.deepcopy(rows);validate(rows,approved=True)
    assert rows==before


def test_runtime_passes_actual_upstream_ast_validator():
    code=build_code(BASE_RUNTIME+RUNTIME+ANALYZE,{'object_name':'fixture'})
    assert len(code)<200000


def test_cleanup_apply_requires_explicit_flag_before_bridge():
    with pytest.raises(ValueError,match='apply=true'):
        cleanup(None,None,'unit',mode='apply',strategy='curves')


@pytest.mark.parametrize('strategy',['automatic','erase_noise','unknown'])
def test_cleanup_unknown_strategy_cannot_dispatch(strategy):
    with pytest.raises(ValueError,match='Choose'):
        cleanup(None,None,'unit',mode='propose',strategy=strategy)


def test_cleanup_cannot_relabel_an_existing_operation(tmp_path):
    from blender_mcp.production.common.storage import Storage
    from blender_mcp.production.common.operations import save_proposal
    storage=Storage(tmp_path)
    operation=save_proposal(storage,'original',{'kind':'root_motion'})
    with pytest.raises(ValueError,match='conflicts'):
        cleanup(None,storage,'unit',mode='preview',strategy='quaternion',operation=operation)
