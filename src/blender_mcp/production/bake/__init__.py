"""M5 bake readiness tools; no Substance or production texture authoring."""
from __future__ import annotations
from . import service


def register(tool,resources):
    @tool('production.bake_prepare_pairs')
    def bake_prepare_pairs(relationship:str,options:dict|None=None,*,_operation_id='')->dict:
        """Freeze explicit HIGH/LOW relationship, material semantics, UV/UDIM, normal convention, cage/ray parameters and current signatures in an immutable local pair manifest. No naming-only pairing."""
        bridge,storage=resources();return service.prepare_pairs(bridge,storage,_operation_id,relationship,options)

    @tool('production.bake_analyze_ray_distance',read_only=True)
    def bake_analyze_ray_distance(relationship:str,options:dict|None=None,*,_operation_id='')->dict:
        """Bounded deterministic LOW triangle samples against HIGH BVH: signed front/rear distance, normal angle, nearest deviation, ray-miss witnesses and explicit ray/cage proposal. No enormous automatic distances."""
        bridge,storage=resources();return service.read(bridge,storage,relationship,options,'rays')

    @tool('production.bake_validate',read_only=True)
    def bake_validate(relationship:str,options:dict|None=None,pair:str|None=None,*,_operation_id='')->dict:
        """READY/READY_WITH_WARNINGS/NOT_READY from relation, LOW topology, UV/UDIM, actual Blender tangents, cage/rays, materials/transforms and base/evaluated geometry compatibility. A stale pair never becomes READY."""
        bridge,storage=resources()
        if pair:service.load_pair(bridge,storage,pair)
        return service.read(bridge,storage,relationship,options)

    @tool('production.bake_generate_cage')
    def bake_generate_cage(relationship:str|None=None,mode:str='analyze',options:dict|None=None,operation:str|None=None,*,_operation_id='')->dict:
        """Propose/preview/apply/verify/revert a NEW non-destructive LOW-topology cage with configurable metre extrusion, penetration/miss/max-distance/intersection gates. Original LOW stays the game mesh."""
        bridge,storage=resources();return service.cage(bridge,storage,_operation_id,relationship,mode,options,operation)

    @tool('production.bake_smoke_test')
    def bake_smoke_test(pair:str,allow_cpu_fallback:bool=False,options:dict|None=None,*,_operation_id='')->dict:
        """Bounded NORMAL/AO selected-to-active smoke maps (32..256px,1..32samples) from a current immutable pair. Checkpoint+asset guard; transient settings/material/UV/selection restored. CPU explicitly opt-in; generated diagnostic artifacts, no production texture QA."""
        bridge,storage=resources();return service.smoke(bridge,storage,_operation_id,pair,allow_cpu_fallback,options)
