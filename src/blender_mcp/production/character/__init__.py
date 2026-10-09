"""M4 character modeling & redesign tools.

Analyses are read-only measurements (no art score). Mutation only happens on a candidate mesh made
from a WORKING_COPY/FIXTURE/TEMPORARY file, through the M3.1 asset guard, checkpoints, immutable
proposals and the operation ledger. MASTER files are refused before any checkpoint or change.
"""
from __future__ import annotations

from . import service
from .transaction import adjust_shape


def register(tool, resources):
    @tool("production.character_inspect", read_only=True)
    def character_inspect(object_name: str, mirror_fraction: float = 0.005, *, _operation_id="") -> dict:
        """Character mesh report on the undeformed base mesh: counts, world dimensions, symmetry (mirror X distances), topology warnings, weight coverage, armature/shape-key state, asset role and automatic region candidates with confidence and evidence. No per-vertex data."""
        bridge, storage = resources()
        return service.inspect(bridge, storage, object_name, mirror_fraction)

    @tool("production.character_define_regions")
    def character_define_regions(object_name: str, regions: list[dict] | None = None, include_automatic: bool = True,
                                 *, _operation_id="") -> dict:
        """Store a region manifest: automatic candidates (source=automatic, confidence, evidence) plus explicit user regions {region_id, definition, role locked|editable|unassigned, tolerance_m, intentional}; a user region overrides an automatic one with the same id. Captures positions, faces, materials, UVs, weights and shape keys of every region. Never edits the mesh."""
        bridge, storage = resources()
        return service.define_regions(bridge, storage, _operation_id, object_name, regions, include_automatic)

    @tool("production.character_lock_regions")
    def character_lock_regions(manifest: str, lock: list[str] | None = None, editable: list[str] | None = None,
                               unassign: list[str] | None = None, object_name: str | None = None,
                               tolerance_m: float | None = None, intentional: list[str] | None = None,
                               *, _operation_id="") -> dict:
        """New manifest with changed roles, every region recaptured on the mesh now. Automatic low-confidence regions cannot become editable (REGION_AMBIGUOUS)."""
        bridge, storage = resources()
        return service.lock_regions(bridge, storage, _operation_id, manifest, lock, editable, unassign, object_name,
                                    tolerance_m, intentional)

    @tool("production.character_validate_locked_regions", read_only=True)
    def character_validate_locked_regions(manifest: str, object_name: str | None = None, *, _operation_id="") -> dict:
        """Verify locked regions: positions, shape keys, weights, face membership, material assignment and UVs within tolerance; topology/membership changes fail (stale indices are never reused)."""
        bridge, storage = resources()
        return service.validate_locked_regions(bridge, storage, manifest, object_name)

    @tool("production.character_analyze_silhouette", read_only=True)
    def character_analyze_silhouette(object_name: str, views: list[str] | None = None, rows: int = 200,
                                     manifest: str | None = None, *, _operation_id="") -> dict:
        """Orthographic silhouette measurements per view (FRONT, SIDE, THREE_QUARTER, BACK): height, width, area, centroid, left/right area balance, extremity reach, negative space, width profile and region extents. Measurements only, no score."""
        bridge, storage = resources()
        return service.analyze_silhouette(bridge, storage, object_name, views, rows, manifest)

    @tool("production.character_analyze_proportions", read_only=True)
    def character_analyze_proportions(object_name: str, profile: str = "HUMANOID", manifest: str | None = None,
                                      custom_reference: dict | None = None, *, _operation_id="") -> dict:
        """Proportion measurements (HUMANOID, CREATURE or CUSTOM) with measurement, informational reference, deviation, confidence and interpretation kept separate. Never recommends normalizing a stylized or non-human design."""
        bridge, storage = resources()
        return service.analyze_proportions(bridge, storage, object_name, profile, manifest, custom_reference)

    @tool("production.character_analyze_asymmetry", read_only=True)
    def character_analyze_asymmetry(object_name: str, manifest: str | None = None, intentional_regions: list[str] | None = None,
                                    tolerance_fraction: float = 0.005, *, _operation_id="") -> dict:
        """Mirror asymmetry per region and LEFT/RIGHT pair, classified intentional / likely_intentional / likely_accidental / unknown with the declared thresholds. Symmetry is never enforced."""
        bridge, storage = resources()
        return service.analyze_asymmetry(bridge, storage, object_name, manifest, intentional_regions, tolerance_fraction)

    @tool("production.character_analyze_surface", read_only=True)
    def character_analyze_surface(object_name: str, manifest: str | None = None, options: dict | None = None,
                                  *, _operation_id="") -> dict:
        """Surface indicators: bumps, pinching, abrupt curvature change, noise, density, stretching, faceting, normal irregularity; clustered by region with robust thresholds. Analysis only."""
        bridge, storage = resources()
        return service.analyze_surface(bridge, storage, object_name, manifest, options)

    @tool("production.character_analyze_intersections", read_only=True)
    def character_analyze_intersections(object_name: str, manifest: str | None = None, reference_object: str | None = None,
                                        *, _operation_id="") -> dict:
        """Conservative self-intersection indicator (BVH triangle overlap) by region pair; with reference_object (same topology) reports only new overlaps. Includes confidence and limitations."""
        bridge, storage = resources()
        return service.analyze_intersections(bridge, storage, object_name, manifest, reference_object)

    @tool("production.character_modeling_audit", read_only=True)
    def character_modeling_audit(object_name: str, manifest: str | None = None, profile: str = "HUMANOID",
                                 include_intersections: bool = True, *, _operation_id="") -> dict:
        """Findings (type, region, severity, evidence, suggested_action) from surface, asymmetry, proportions, intersections, topology and weights. No overall quality score."""
        bridge, storage = resources()
        return service.modeling_audit(bridge, storage, object_name, manifest, profile, include_intersections)

    @tool("production.character_redesign_plan", read_only=True)
    def character_redesign_plan(object_name: str, intent: str, manifest: str | None = None, *, _operation_id="") -> dict:
        """Translate a design intent into PROPOSED topology-preserving operations (regions, operation, estimated displacement, locked proximity, risk, dependencies, rationale). apply=false: never mutates; numbers are starting proposals."""
        bridge, storage = resources()
        return service.redesign_plan(bridge, storage, object_name, intent, manifest)

    @tool("production.character_create_candidate", retry_safe=True)
    def character_create_candidate(object_name: str, source_fingerprint: str, *, _operation_id="") -> dict:
        """Candidate mesh (object + mesh copy) from a WORKING_COPY/FIXTURE/TEMPORARY file after asset guard and checkpoint, with a before/after dependency manifest. MASTER refused; an existing candidate of the same source state is reused."""
        bridge, storage = resources()
        return service.create_candidate(bridge, storage, _operation_id, object_name, source_fingerprint)

    @tool("production.character_adjust_shape")
    def character_adjust_shape(object_name: str | None = None, mode: str = "analyze", options: dict | None = None,
                               operation: str | None = None, *, _operation_id="") -> dict:
        """Topology-preserving shape edit on a candidate mesh: analyze/propose (options: regions_manifest, operation {type translate|directional|grab|inflate|scale|smooth|relax|crease, region, strength, direction, space, falloff, preserve_boundary}, shape_keys, tolerance_m), then preview/apply/verify/revert with the proposal reference. Locked movement beyond tolerance, topology/weight/UV/material/normal-state changes and unsafe shape keys fail."""
        bridge, storage = resources()
        return adjust_shape(bridge, storage, _operation_id, object_name, mode, options, operation)

    @tool("production.character_deformation_qa")
    def character_deformation_qa(rig: str, source_mesh: str, candidate_mesh: str, stress_poses: list[dict],
                                 *, _operation_id="") -> dict:
        """Skinning QA after a shape edit: the same explicit stress poses on source and candidate (temporary, restored); fails when the candidate adds collapsed/stretched edges or degenerate triangles."""
        bridge, storage = resources()
        return service.skinning_qa(bridge, storage, _operation_id, rig, source_mesh, candidate_mesh, stress_poses)

    @tool("production.character_render_multiview")
    def character_render_multiview(objects: list[str], views: list[str] | None = None, width: int = 512, height: int = 512,
                                   *, _operation_id="") -> dict:
        """Identical orthographic cameras, resolution and neutral Workbench look for every object and view; scene state restored. Writes PNGs and a montage."""
        bridge, storage = resources()
        return service.render_multiview(bridge, storage, _operation_id, objects, views, width, height)

    @tool("production.character_compare_before_after")
    def character_compare_before_after(before_object: str, after_object: str, views: list[str] | None = None,
                                       width: int = 512, height: int = 512, manifest: str | None = None,
                                       *, _operation_id="") -> dict:
        """Before/after renders with shared cameras and look plus silhouette deltas per view. Visual review stays required."""
        bridge, storage = resources()
        return service.compare_before_after(bridge, storage, _operation_id, before_object, after_object, views, width, height, manifest)

    @tool("production.character_capture_reference_views")
    def character_capture_reference_views(object_name: str, views: list[str] | None = None, width: int = 512, height: int = 512,
                                          concept_art: dict | None = None, *, _operation_id="") -> dict:
        """Store a reference view set (cameras, look, images, silhouettes) and optionally associate concept-art images with views. No image-to-3D."""
        bridge, storage = resources()
        return service.capture_reference_views(bridge, storage, _operation_id, object_name, views, width, height, concept_art)

    @tool("production.character_capture_candidate_views")
    def character_capture_candidate_views(reference_set: str, object_name: str, *, _operation_id="") -> dict:
        """Render a candidate with exactly the cameras, resolution and look of a stored reference view set."""
        bridge, storage = resources()
        return service.capture_candidate_views(bridge, storage, _operation_id, reference_set, object_name)

    @tool("production.character_compare_reference")
    def character_compare_reference(reference_set: str, object_name: str, *, _operation_id="") -> dict:
        """Measurable silhouette differences against a reference view set plus paired images for review; no semantic-equivalence claim."""
        bridge, storage = resources()
        return service.compare_reference(bridge, storage, _operation_id, reference_set, object_name)

    @tool("production.character_redesign_session")
    def character_redesign_session(object_name: str, intent: str, manifest: str | None = None, apply: bool = False,
                                   step: int = 1, rig: str | None = None, stress_poses: list[dict] | None = None,
                                   *, _operation_id="") -> dict:
        """Guided session: inspect, audit, plan and risk (apply=false, default, no mutation). apply=true runs ONE cycle: candidate, propose and preview of plan step N, numeric checks, deformation QA (with rig and stress_poses) and before/after renders. The real apply stays an explicit character_adjust_shape call."""
        bridge, storage = resources()
        return service.redesign_session(bridge, storage, _operation_id, object_name, intent, manifest, apply, step,
                                        stress_poses, rig)
