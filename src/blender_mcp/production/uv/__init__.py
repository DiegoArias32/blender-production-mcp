"""M5 UV/UDIM high-level tools."""
from __future__ import annotations
from . import service


def register(tool,resources):
    @tool('production.uv_analyze',read_only=True)
    def uv_analyze(relationship:str,options:dict|None=None,*,_operation_id='')->dict:
        """Base UV layers/islands/overlap/flips/degenerates/UDIM/stretch/padding/density with bounded tile/material summaries. Requires current explicit high/low relationship."""
        bridge,storage=resources();return service.analyze(bridge,storage,relationship,options)

    @tool('production.uv_validate',read_only=True)
    def uv_validate(relationship:str,options:dict|None=None,*,_operation_id='')->dict:
        """UV gates with configurable overlap/flip/stretch/pixel padding and expected tile policy; incomplete checks cannot pass."""
        bridge,storage=resources();return service.analyze(bridge,storage,relationship,options,'validate')

    @tool('production.uv_analyze_texel_density',read_only=True)
    def uv_analyze_texel_density(relationship:str,options:dict|None=None,*,_operation_id='')->dict:
        """Measure texels/metre from world surface area and UV area; resolution 32..8192, per-tile resolution overrides. Reports distribution and material-region density; no universal density target."""
        bridge,storage=resources();return service.analyze(bridge,storage,relationship,options,'texel_density')

    @tool('production.uv_generate')
    def uv_generate(relationship:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """Explicit seam proposal using configured angle/material/sharp/edge signals; angle-based unwrap and deterministic shelf pack on a NEW Candidate. analyze/propose/preview/apply/verify/revert. UV-immutable locks refuse generation."""
        bridge,storage=resources();return service.mutate(bridge,storage,_operation_id,relationship,mode,options,operation,'generate')

    @tool('production.udim_plan',read_only=True)
    def udim_plan(relationship:str,options:dict,*,_operation_id='')->dict:
        """Plan explicit semantic material_index or faces assignments to configurable UDIM1001+ tiles; reject incomplete/ambiguous/cross-island assignments, apply=false."""
        bridge,storage=resources();return service.plan_udim(bridge,storage,relationship,options)

    @tool('production.udim_apply_plan')
    def udim_apply_plan(relationship:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """Immutable proposal/application of explicit tile assignments with bounded packing, pixel padding and rotation policy on a NEW Candidate; verify geometry/weights/morphs/materials preserved."""
        bridge,storage=resources();return service.mutate(bridge,storage,_operation_id,relationship,mode,options,operation,'udim')

    @tool('production.uv_pack')
    def uv_pack(relationship:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """One deterministic shelf pack per existing tile, configurable padding and rotation, proposal first. Preserves topology and semantic tile assignments; no endless occupancy search."""
        bridge,storage=resources();return service.mutate(bridge,storage,_operation_id,relationship,mode,options,operation,'pack')
