"""M3 rig tool registrations, isolated from upstream and M2 registration code."""
from __future__ import annotations
from .semantics import analyze
from .ik_fk import operate

def register(tool,resources):
    @tool('production.rig_classify',read_only=True)
    def classify(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Classify limb-level IK/FK, switches, deform/control rigs; unknown is valid."""
        return analyze(resources()[0],object_name,options,'classify')
    @tool('production.rig_map_semantics',read_only=True)
    def semantics(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Hierarchy/rest/constraint/name evidence, confidence and alternatives; no mutation."""
        return analyze(resources()[0],object_name,options,'semantics')
    @tool('production.rig_analyze_controls',read_only=True)
    def controls(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Constraint targets, poles, control candidates, properties and driver relationships."""
        return analyze(resources()[0],object_name,options,'controls')
    @tool('production.rig_analyze_limbs',read_only=True)
    def limbs(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Limb chains, present control mode and explicit switch relationships."""
        return analyze(resources()[0],object_name,options,'limbs')
    @tool('production.rig_compatibility_report',read_only=True)
    def compatibility(object_name:str,options:dict|None=None,*,_operation_id='')->dict:
        """Feature-specific support conditions and limitations; never blanket compatibility."""
        return analyze(resources()[0],object_name,options,'compatibility')
    @tool('production.rig_match_fk_to_ik')
    def fk_ik(object_name:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """Proposal-first explicit two-bone FK to IK pose match, checkpoint and duplicate Action."""
        return operate(*resources(),_operation_id,'fk_to_ik',False,object_name,mode,options,operation)
    @tool('production.rig_match_ik_to_fk')
    def ik_fk(object_name:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """Match evaluated IK pose to FK channels on a duplicate; verify world-space pose."""
        return operate(*resources(),_operation_id,'ik_to_fk',False,object_name,mode,options,operation)
    @tool('production.rig_bake_fk_to_ik')
    def bake_fk_ik(object_name:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """Bake explicit FK limb motion to IK target/pole with sampled error verification."""
        return operate(*resources(),_operation_id,'fk_to_ik',True,object_name,mode,options,operation)
    @tool('production.rig_bake_ik_to_fk')
    def bake_ik_fk(object_name:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """Bake explicit IK limb motion into FK keys without removing solver/driver structure."""
        return operate(*resources(),_operation_id,'ik_to_fk',True,object_name,mode,options,operation)
