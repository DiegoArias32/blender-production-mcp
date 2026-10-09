from blender_mcp.production.animation.generation.candidate import CandidateBridge
from blender_mcp.production.animation.generation.runtime import GENERATION_RUNTIME, ANALYZE_READ, runtime_for_stage
from blender_mcp.production.common.bridge import build_code
from blender_mcp.production.common.m3_transaction import BASE_RUNTIME, READ, EXECUTE
from blender_mcp.production.contacts.runtime import CONTACT_RUNTIME
from blender_mcp.production.deformation.runtime import EXECUTE as DEFORMATION


class CompileOnly:
    def run(self, body, params):
        code = build_code(body, params)
        return {'bytes': len(code.encode())}


def test_restoring_wrapper_remains_inside_original_ast_and_payload_budgets():
    adapter = CandidateBridge(CompileOnly(), 'M6_Fixture', 'M6_NewCandidate')
    for script in (BASE_RUNTIME+GENERATION_RUNTIME+READ,
                   BASE_RUNTIME+GENERATION_RUNTIME+ANALYZE_READ, DEFORMATION,
                   BASE_RUNTIME+CONTACT_RUNTIME+"\nresult=contact_limb_report(m2_object(params['object_name']),params['options'])\n"):
        result = adapter.run(script, {'object_name':'M6_Fixture', 'options':{}})
        assert result['bytes'] < 200000


def test_each_stage_transaction_retains_original_validator_budget():
    adapter = CandidateBridge(CompileOnly(), 'M6_Fixture', 'M6_NewCandidate')
    for stage in ('POSE','BLOCKING','BREAKDOWNS','TIMING','ARCS','BALANCE','ROOT','CONTACTS','EFFECTOR',
                  'SPLINE','CURVES','OVERLAP','FOLLOW_THROUGH','REFINE'):
        script=BASE_RUNTIME+runtime_for_stage(stage)+EXECUTE
        result=adapter.run(script,{'object_name':'M6_Fixture','options':{'stage':stage}})
        assert result['bytes'] < 200000


def test_evaluation_globals_exist_before_candidate_state_capture():
    import ast
    from blender_mcp.production.common.bridge import compose_code
    class Ordering:
        def run(self,body,params):
            tree=ast.parse(compose_code(body,params))
            assignments=[(i,n) for i,n in enumerate(tree.body) if isinstance(n,ast.Assign)]
            positions={t.id:i for i,n in assignments for t in n.targets if isinstance(t,ast.Name)}
            assert positions['PROD_EVALUATION_MODES'] < positions['_m6_eval_state']
            guarded=next(n for n in tree.body if isinstance(n,ast.Try))
            assert isinstance(guarded.body[0],ast.Expr) and guarded.body[0].value.func.id=='m3_assign'
            return {'checked':True}
    assert CandidateBridge(Ordering(),'Fixture','Candidate').run(BASE_RUNTIME+'\nresult={}\n')['checked']
