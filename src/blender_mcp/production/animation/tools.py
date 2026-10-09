"""M2 registrations, kept separate from the small MCP entry point."""
from __future__ import annotations

from .actions import manage_action, compare_actions
from .corrections import correction
from .curve_ops import analyze_fcurves
from .insights import run_analysis


def register(tool,resources):
    @tool("production.animation_create_action")
    def create_action(name: str, object_name: str | None = None, assign: bool = False,
                      dry_run: bool = True, *, _operation_id="") -> dict:
        """Plan/create a collision-safe Action; checkpoint before creation. Original Actions retained."""
        bridge,storage=resources()
        return manage_action(bridge,storage,_operation_id,"create",object_name,name=name,assign=assign,dry_run=dry_run)

    @tool("production.animation_duplicate_action")
    def duplicate_action(action_name: str, object_name: str | None = None, name: str | None = None,
                         assign: bool = False, dry_run: bool = True, *, _operation_id="") -> dict:
        """Duplicate without overwriting; checkpoint and exact original-content check. Set dry_run=false to create."""
        bridge,storage=resources()
        return manage_action(bridge,storage,_operation_id,"duplicate",object_name,action_name,name,assign,dry_run)

    @tool("production.animation_restore_action")
    def restore_action(object_name: str, action_name: str, dry_run: bool = True, *, _operation_id="") -> dict:
        """Reassign a preserved Action with checkpoint; never delete the current Action."""
        bridge,storage=resources()
        return manage_action(bridge,storage,_operation_id,"restore",object_name,action_name,dry_run=dry_run)

    @tool("production.animation_compare_actions", read_only=True)
    def compare(object_name: str, before_action: str, after_action: str,
                frame_start: int | None = None, frame_end: int | None = None, *, _operation_id="") -> dict:
        """Compare exact Action fingerprints and evaluated poses; restores assignment and frame."""
        return compare_actions(resources()[0],object_name,before_action,after_action,frame_start,frame_end)

    @tool("production.animation_analyze_contacts", read_only=True)
    def contacts(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Ground-plane M2 (schema production.contacts.ground_plane.v1) or explicit feet+surfaces M3 (schema production.contacts.surfaces.v1) phase contacts; the 'schema' field says which. Physical drift/confidence, configurable thresholds."""
        if options and ('surfaces' in options or 'feet' in options):
            from ..contacts.service import analyze_contacts
            return dict(analyze_contacts(resources()[0],object_name,options),
                        schema='production.contacts.surfaces.v1',backend='m3_explicit_surfaces')
        return dict(run_analysis(resources()[0],"contacts",object_name,options or {}),
                    schema='production.contacts.ground_plane.v1',backend='m2_ground_plane')

    @tool("production.animation_fix_foot_sliding")
    def foot_fix(object_name: str | None = None, mode: str = "analyze", options: dict | None = None,
                 operation: str | None = None, *, _operation_id="") -> dict:
        """Analyze/propose/preview/apply/verify/revert. Explicit reviewed controls+confirm_contacts needed; immutable operation required for mutations."""
        bridge,storage=resources()
        advanced=bool(options and ('surfaces' in options or 'feet' in options))
        if operation:
            from ..common.operations import load_proposal
            advanced=load_proposal(storage,operation)[1]['kind']=='advanced_foot_sliding'
        if advanced:
            from ..contacts.service import fix_contacts
            return fix_contacts(bridge,storage,_operation_id,object_name,mode,options,operation)
        return correction(bridge,storage,_operation_id,"foot_sliding",object_name,mode,options,operation)

    @tool("production.animation_fix_quaternion_flips")
    def quaternion_fix(object_name: str | None = None, mode: str = "analyze", options: dict | None = None,
                       operation: str | None = None, *, _operation_id="") -> dict:
        """Correct quaternion representation on a duplicate; reject changed orientation paths beyond tolerance."""
        bridge,storage=resources()
        return correction(bridge,storage,_operation_id,"quaternion",object_name,mode,options,operation)

    @tool("production.animation_analyze_fcurves", read_only=True)
    def curves(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Contextual F-Curve findings with bone/channel/frame/severity/detail filters and native-unit thresholds."""
        return analyze_fcurves(resources()[0],object_name,options)

    @tool("production.animation_cleanup_fcurves")
    def cleanup(object_name: str | None = None, mode: str = "analyze", options: dict | None = None,
                operation: str | None = None, *, _operation_id="") -> dict:
        """Conservative proposal-first key cleanup on duplicate; verify dense samples against per-channel tolerances."""
        bridge,storage=resources()
        return correction(bridge,storage,_operation_id,"cleanup",object_name,mode,options,operation)

    @tool("production.animation_analyze_root_motion", read_only=True)
    def root_analysis(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Hierarchy-based root candidates; world-space distance, displacement, rotation and derivative events."""
        return run_analysis(resources()[0],"root_motion",object_name,options or {})

    @tool("production.animation_prepare_root_motion")
    def root_prepare(object_name: str | None = None, mode: str = "analyze", options: dict | None = None,
                     operation: str | None = None, *, _operation_id="") -> dict:
        """Propose in-place horizontal motion removal for an explicit unparented root; no rig restructuring."""
        bridge,storage=resources()
        return correction(bridge,storage,_operation_id,"root_motion",object_name,mode,options,operation)

    @tool("production.animation_retime")
    def retime(object_name: str | None = None, mode: str = "analyze", options: dict | None = None,
               operation: str | None = None, *, _operation_id="") -> dict:
        """Duplicate-only timing change by scale_factor or target_duration_seconds, range optional; FPS unchanged."""
        bridge,storage=resources()
        return correction(bridge,storage,_operation_id,"retime",object_name,mode,options,operation)

    @tool("production.animation_analyze_control_mode", read_only=True)
    def controls(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """M2 analysis plus per-limb M3 modes, targets, switches and driver relationships; no mutation."""
        from ..rig.semantics import analyze as analyze_rig
        result=run_analysis(resources()[0],"control_mode",object_name,options or {})
        result['semantic_control_modes']=analyze_rig(resources()[0],object_name,{},'control_mode')
        return result

    @tool("production.animation_analyze_balance", read_only=True)
    def balance(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Conservative support-region/pelvis projection heuristic; not biomechanics and never auto-corrects."""
        return run_analysis(resources()[0],"balance",object_name,options or {})

    @tool("production.animation_analyze_motion_quality", read_only=True)
    def motion_quality(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Velocity/acceleration/angular/contact indicators: whole-rig (schema production.motion_quality.rig.v1) or explicit limb chains (schema production.motion_quality.limb_chains.v1); the 'schema' field says which. No artistic quality score."""
        if options and 'chains' in options:
            from ..contacts.service import analyze_limbs
            return dict(analyze_limbs(resources()[0],object_name,options),
                        schema='production.motion_quality.limb_chains.v1',backend='m3_limb_chains')
        return dict(run_analysis(resources()[0],"motion_quality",object_name,options or {}),
                    schema='production.motion_quality.rig.v1',backend='m2_rig_sampling')

    @tool("production.animation_refinement_plan", read_only=True)
    def plan(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Analyze rig, Action, contacts, quaternion/F-Curves, root, balance and motion; recommend ordered corrections without mutation."""
        return run_analysis(resources()[0],"refinement_plan",object_name,options or {})

    @tool("production.qa_render_multiview")
    def multiview(object_name: str, options: dict | None = None, *, _operation_id="") -> dict:
        """Checkpointed lightweight FRONT/SIDE/THREE_QUARTER previews with restored scene settings."""
        from ..qa.multiview import render_multiview
        bridge,storage=resources()
        return render_multiview(bridge,storage,_operation_id,object_name,options)

    @tool("production.qa_compare_before_after")
    def before_after(object_name: str, before_action: str, after_action: str,
                     options: dict | None = None, *, _operation_id="") -> dict:
        """Equivalent cameras/framing/FPS/resolution and range for preserved original and candidate Actions."""
        from ..qa.multiview import compare_before_after
        bridge,storage=resources()
        return compare_before_after(bridge,storage,_operation_id,object_name,before_action,after_action,options)
