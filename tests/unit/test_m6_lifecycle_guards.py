from copy import deepcopy

import pytest

from blender_mcp.production.animation.generation import service, store
from blender_mcp.production.animation.generation.render import settings_for_temporal
from blender_mcp.production.common.storage import Storage


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    storage=Storage(tmp_path)
    semantic={'root_translation_m':[0,0,0],'pelvis_offset_m':[0,0,0],
              'feet':{side:{'offset_m':[0,0,0],'contact':True,'contact_phase':'FOOT_FLAT'}
                      for side in ('left','right')}}
    poses=[{'frame':frame,'phase':'NEUTRAL','semantic':deepcopy(semantic)} for frame in (1,31)]
    plan={'object_name':'Fixture','brief':{'frame_start':1,'frame_end':31},
          'pose_plan':{'poses':poses,'breakdowns':[]},'options':{},
          'source_action':'Immutable','supported':True,'warnings':[]}
    reference,_=store.save(storage,'controlled-lifecycle',plan)
    monkeypatch.setattr(service,'fresh',lambda *a:None)
    monkeypatch.setattr(service,'require_mutable_asset',lambda *a:None)
    monkeypatch.setattr(service,'create_checkpoint',lambda *a:{'manifest':'owned-checkpoint'})
    monkeypatch.setattr(service,'_mark_action',lambda *a,**k:{'fingerprint':'marked','source_intact':True})
    return storage,reference


def append(lifecycle, **values):
    storage,reference=lifecycle
    path,_,state=store.load(storage,reference)
    state.update(state='GENERATED_CANDIDATE',candidate_action='Candidate',candidate_fingerprint='fp',**values)
    store.event(path.parent,state)
    return state


@pytest.mark.parametrize('stage',['SPLINE','BREAKDOWNS','ARCS','REFINE'])
def test_pose_cannot_bypass_full_blocking_review(lifecycle, stage):
    append(lifecycle,stage='POSE')
    with pytest.raises(ValueError,match='BLOCKING_REVIEW_REQUIRED'):
        service.stage(None,*lifecycle[:1],'next',lifecycle[1],stage,apply=True)


def test_new_pose_invalidates_a_previously_reviewed_full_blocking(lifecycle, monkeypatch):
    storage, reference = lifecycle
    _, plan, _ = store.load(storage, reference)
    reference, _ = store.save(storage, 'new-pose-after-review', dict(plan, mapping={}))
    lifecycle = storage, reference
    append(lifecycle, stage='BLOCKING', technical_qa={'passed':True},
           render={'status':'ok','temporal_complete':True,'output_dir':'owned-preview'})
    service.review_or_accept(None,storage,'review-original-blocking',reference,review())
    assert store.load(storage,reference)[2]['blocking_review_complete'] is True

    calls=[]
    def blender_transaction(*args):
        calls.append(args[6])
        return ({'proposal':{'supported':True},'operation':'owned-proposal'} if args[6]=='propose'
                else {'numeric_verification':'pass','working_action':'NewPose','checkpoint':'owned-checkpoint'})
    monkeypatch.setattr(service,'transaction',blender_transaction)
    monkeypatch.setattr(service,'technical',lambda *a:{'passed':True})
    monkeypatch.setattr(service,'candidate_snapshot',lambda *a:{'fingerprint':'new-pose'})
    service.stage(None,storage,'new-pose',reference,'POSE',apply=True)
    state=store.load(storage,reference)[2]
    assert state['candidate_action']=='NewPose'
    assert state['blocking_review_complete'] is False
    assert calls==['propose','preview']
    with pytest.raises(ValueError,match='BLOCKING_REVIEW_REQUIRED'):
        service.stage(None,storage,'unreviewed-spline',reference,'SPLINE',apply=True)
    assert calls==['propose','preview']


def review():
    return {'reviewer':'Test control flow','observations':['Inspected owned full clip'],'findings':[],
            'decision':'ACCEPT','artifact_reference':'owned-preview','temporal_reviewed':True}


@pytest.mark.parametrize('stage',['POSE','BLOCKING'])
def test_stepped_review_never_certifies_final_acceptance(lifecycle, stage):
    append(lifecycle,stage=stage,technical_qa={'passed':True},render={'status':'ok','temporal_complete':True,'output_dir':'owned-preview'})
    with pytest.raises(ValueError,match='CONTINUOUS_FINAL_QA_REQUIRED'):
        service.review_or_accept(None,lifecycle[0],'accept',lifecycle[1],review(),True)
    assert store.load(*lifecycle)[2]['state']=='REVIEWED'


def test_failed_blocking_qa_does_not_unlock_progression(lifecycle):
    append(lifecycle,stage='BLOCKING',technical_qa={'passed':False},render={'status':'ok','temporal_complete':True,'output_dir':'owned-preview'})
    service.review_or_accept(None,lifecycle[0],'review',lifecycle[1],review())
    assert store.load(*lifecycle)[2]['blocking_review_complete'] is False
    with pytest.raises(ValueError,match='BLOCKING_REVIEW_REQUIRED'):
        service.stage(None,lifecycle[0],'breakdown',lifecycle[1],'BREAKDOWNS',apply=True)


@pytest.mark.parametrize('operation',['render','accept'])
def test_pending_refinement_cannot_be_skipped(lifecycle, operation):
    append(lifecycle,stage='SPLINE',pending_refinement={'improved':True})
    fn=service.render_generation if operation=='render' else service.review_or_accept
    with pytest.raises(ValueError,match='REFINEMENT_REVIEW_REQUIRED'):
        fn(None,lifecycle[0],'pending',lifecycle[1])


def test_unrendered_failed_refinement_reports_the_real_reason(lifecycle):
    append(lifecycle,last_refinement={'reverted':True,'outcome':'safe_rejection'})
    with pytest.raises(ValueError,match='REFINEMENT_NOT_RENDERED'):
        service.refine(None,lifecycle[0],'review-rejection',lifecycle[1],{'review':review()})


@pytest.mark.parametrize('minimum',[0,-1,float('inf'),True])
def test_bad_improvement_threshold_never_consumes_a_cycle(lifecycle, minimum):
    append(lifecycle,stage='SPLINE')
    with pytest.raises(ValueError,match='INVALID_IMPROVEMENT_THRESHOLD'):
        service.refine(None,lifecycle[0],'invalid',lifecycle[1],{'minimum_improvement':minimum},True)
    assert store.load(*lifecycle)[2]['refinement_cycles']==0


@pytest.mark.parametrize('decision',['ACCEPT','REJECT'])
def test_pending_refinement_visual_decision_keeps_counter_and_candidate(lifecycle, decision):
    previous={'state':'REVIEWED','stage':'SPLINE','candidate_action':'Previous','candidate_fingerprint':'old'}
    candidate={'state':'GENERATED_CANDIDATE','stage':'REFINE','candidate_action':'Refined','candidate_fingerprint':'new'}
    append(lifecycle,stage='SPLINE',refinement_cycles=3,pending_refinement={'improved':True,'previous':previous,
        'candidate':candidate,'render':{'status':'ok','temporal_complete':True,'output_dir':'owned-preview'}})
    feedback=dict(review(),decision=decision)
    result=service.refine(None,lifecycle[0],'review-third',lifecycle[1],{'review':feedback})
    assert result['candidate_action']==('Refined' if decision=='ACCEPT' else 'Previous')
    assert result['refinement_cycles']==3
    with pytest.raises(ValueError,match='REFINEMENT_LIMIT'):
        service.refine(None,lifecycle[0],'fourth',lifecycle[1],apply=True)


def test_full_temporal_views_allow_bounded_custom_directions():
    values=settings_for_temporal({'custom_views':[{'name':'CUSTOM_REAR','direction':[0,1,.25]}]})
    assert values['views']==['FRONT','SIDE','THREE_QUARTER','CUSTOM_REAR']
    assert values['custom_view_directions']['CUSTOM_REAR']==[0,1,.25]
    with pytest.raises(ValueError,match='THREE_VIEWS_REQUIRED'):
        settings_for_temporal({'views':['FRONT']})


@pytest.mark.parametrize('custom',[[{'name':'../unsafe','direction':[0,1,0]}],
    [{'name':'CUSTOM_EMPTY','direction':[0,0,0]}],[{'name':'CUSTOM_NAN','direction':[0,float('nan'),0]}],
    [{'name':'CUSTOM_BOOL','direction':[True,0,0]}]])
def test_bad_custom_view_rejects_before_checkpoint_or_render(custom):
    with pytest.raises(ValueError,match='CUSTOM_VIEW_INVALID'):
        settings_for_temporal({'custom_views':custom})
