"""High-level physics work units; mutations default to proposal mode."""
from __future__ import annotations
from . import service
from ..common.ledger import is_uncertain

NAMES={
 'physics_inspect_character','physics_classify_secondary','physics_compatibility_report','physics_create_brief','physics_plan',
 'physics_prepare_colliders','physics_analyze_cloth','physics_plan_cloth_pins','physics_setup_cloth','physics_simulate_cloth',
 'physics_analyze_penetration','physics_analyze_hair','physics_plan_hair','physics_generate_hair_secondary',
 'physics_setup_spring_chain','physics_generate_secondary_bones','physics_analyze_accessory','physics_generate_accessory_motion',
 'physics_analyze_soft_region','physics_generate_soft_secondary','physics_verify_primary_integrity',
 'physics_cache_plan','physics_bake_cache','physics_bake_secondary_motion','physics_collision_report','physics_analyze_motion',
 'physics_render_preview','physics_compare_before_after','physics_refine','physics_accept','physics_reject',
 'physics_create_candidate','physics_status','physics_review','physics_analyze_stability'}
READ_ONLY={'physics_create_brief'}
TRANSIENT={'physics_inspect_character','physics_classify_secondary','physics_compatibility_report','physics_analyze_cloth',
 'physics_plan_cloth_pins','physics_analyze_hair','physics_analyze_accessory','physics_analyze_soft_region',
 'physics_verify_primary_integrity','physics_status','physics_analyze_penetration','physics_collision_report',
 'physics_analyze_motion','physics_analyze_stability'}


def register(tool,resources):
    def make(name):
        def execute(object_name:str|None=None,physics:str|None=None,intent:str|None=None,brief:dict|None=None,
                    options:dict|None=None,apply:bool=False,*,_operation_id='')->dict:
            bridge,storage=resources()
            try:
                return service.dispatch(name,bridge,storage,_operation_id,object_name,physics,intent,brief,options,apply)
            except Exception as error:
                if physics and is_uncertain(error):
                    path,plan,state=service.load(storage,physics)
                    state.update(uncertain_operation=True,last_error=repr(error),uncertain_operation_id=_operation_id)
                    service.event(path,state)
                raise
        execute.__name__=name
        execute.__doc__='Inspect, propose, or execute '+name+' on isolated secondary candidates. Body/face preserved. apply defaults false; fixed physics plan and explicit QA/review gates.'
        return execute
    for name in sorted(NAMES):
        tool('production.'+name,read_only=name in READ_ONLY|TRANSIENT)(make(name))
