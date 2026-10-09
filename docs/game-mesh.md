# M5 game mesh workflow and local package

M5 prepares a Candidate Game Mesh from an explicit SOURCE HIGH. The master, source HIGH and every
previous LOW remain intact. Optimization, projection, transfer, UV, normals and triangulation create
new objects with new mesh datablocks. The package is a set of local, validated references and
manifests; it does not export FBX, invoke a game engine or start a texturing application.

The supported automatic reduction adapter is explicitly guided Decimate. It does not generate
artist-authored anatomical loops. Manual/unsupported cases are reported rather than converted to a
different strategy. This document describes implemented contracts, not a declaration that every
milestone acceptance gate has passed; see the milestone report and machine evidence for closure.

## Identity and preservation

`retopology_analyze` returns a compact `production.mesh_signature.m5.v1`. Its full fingerprint binds
the supported geometry, topology, UV, weights, morphs, normals, attributes, materials, properties,
modifiers, rig, transforms and units. A separate geometry fingerprint binds topology, coordinates
and transforms. No vertex cloud is sent to the model. Unsupported coverage is reported; shader
graphs, arbitrary handlers and every possible modifier are not universally certified.

`retopology_create_candidate` takes the current M5 fingerprint and creates an immutable
`production.high_low.v1` relationship with explicit object references, asset ID, revision and
integrity hash. HIGH identity is independent of whether it has a rig. LOW identity is independent
of its naming convention. Changing HIGH or LOW geometry invalidates that relationship. LOW
attribute changes can still be analyzed through the same geometric relationship, but invalidate
proposals or bake pairs that froze its previous full signature.

Regions use compact spatial/group/attribute definitions. After reduction, HIGH/LOW correspondence
uses BVH triangles and barycentric coordinates with distance, normal and competing-component
guards. An M4 vertex index does not become a valid LOW vertex index by assumption. Regions marked
protected in relationship metadata cannot be removed or weakened by operation overrides.

## Ordered workflow

1. Confirm authorization for the asset, inspect its role and use a registered fixture or working
   copy. A MASTER is immutable. M5 development itself uses only controlled fixtures.
2. Inspect file origin, units/transforms, mesh topology, dependencies and any rig/morphs.
3. Audit HIGH topology, density, semantic regions and explicit deformation priorities.
4. Define protected features and intentional asymmetry; do not normalize creatures toward humans.
5. Declare the triangle budget and regional minimums/allocations. An impossible protected floor is
   a rejection, not permission to remove protection.
6. Select an explicit strategy and review its attribute/morph/UV consequences.
7. Create the first candidate and HIGH/LOW relationship from the fresh M5 signature.
8. Propose one optimization. Preview creates a checkpoint and another candidate, then measures it.
9. Compare topology, protected interior surfaces, source fidelity and equivalent silhouettes.
10. Inspect comparable views. Apply a reviewed proposal explicitly, then verify its final candidate.
    Keep the previous relationship when any numeric gate fails.
11. If required, propose bounded source projection, repeat preview/apply/verify and inspect it.
12. Propose spatial weight transfer, declaring groups, influence limits and side pairs as applicable.
13. Validate weight sums/influences/leakage and run explicit, mapped stress poses. The rig state is
    restored. A rigged mesh without stress evidence is UNVERIFIED for package acceptance.
14. Analyze morph preservation. Changed-topology morph reconstruction is manual/later-pipeline
    required; never delete source keys to pass this gate.
15. Transfer explicitly selected materials, supported named attributes and custom properties.
16. Prepare normals with preserve as default, then create/validate deterministic triangulated
    evaluation. Existing authoring meshes stay intact. Recheck protected non-planar surfaces.
17. Generate/review UVs and explicit semantic UDIM assignments, pack once with declared settings,
    and validate overlap, flips, degenerates, padding, stretch and texel-density measurements.
18. Validate Blender-generated tangents using the final UV layer and declare the normal convention.
19. Measure bake ray distances, propose/verify an explicit cage where required, freeze the final
    pair and validate readiness. Run bounded NORMAL/AO smoke tests on that exact pair.
20. Render equivalent HIGH/LOW views and relevant stress views, inspect them, run the aggregate
    numeric gates and prepare the local package. Record final signatures, checkpoint, warnings,
    review limits and asset references in the report and Obsidian. Accept or reject explicitly.

Every mutation follows analyze → propose → preview → checkpoint → new candidate → apply → verify.
Checkpoint/candidate creation is performed by the transaction where applicable; the list does not
authorize editing sources in place. Preview and apply have distinct candidates. Numerical success
does not approve silhouette, deformation quality, surface appearance or production suitability.

## Aggregate validation

`production.game_mesh_validate(relationship, options)` groups domain options under `retopology`,
`weights`, `attributes`, `game_mesh`, `uv` and `bake`. Unknown fields fail. The result reports 15
separate subsystems:

| Subsystem | Evidence required |
|---|---|
| topology | LOW manifold/boundary/degenerate/sliver/normal/component checks and protected source regions |
| triangle_budget | Explicit target/max and optional minimum; no declared budget remains UNVERIFIED |
| surface | Deterministic bidirectional source/LOW surface and normal samples within declared tolerances |
| silhouette | Equivalent orthographic masks, IoU and boundary-distance gates at declared resolution |
| weights | Configurable sums/influences and selected leakage/discontinuity policies for rigged meshes |
| deformation | Explicit mapped stress poses and source/candidate evaluated comparison for rigged meshes |
| morphs | Supported exact static preservation; unsupported topology/dependency transfer fails |
| materials_attributes | Explicit selected material/attribute/property correspondence and verification |
| normals | Finite usable normals and declared preparation/preservation policy |
| triangulation | Reproducible triangulated evaluation and applicable preservation measurements |
| tangents | Installed Blender tangent generation with usable UVs, finite orthogonal frames and signs |
| uv | Selected UV layer, overlap/flips/degenerates/stretch/padding and completed checks |
| udim | Current measured tile layout and declared expected tile policy |
| texel_density | Completed physical density measurement; no universal artistic density target |
| bake | Current concrete readiness report and applicable geometry/ray/cage gates |

Each subsystem reports PASS, FAIL, UNVERIFIED or NOT_APPLICABLE. Static meshes without an armature
mark weight/deformation gates NOT_APPLICABLE; a missing applicable measurement stays UNVERIFIED.
These are evidence states, not quality scores. The UV/UDIM/density states reference the same current
UV analysis and its configured policies rather than three independent certificates.

`require_bake_ready` and `require_deformation` default to true. Setting either false permits
inspection of an intermediate result, with the excluded gate recorded in `ignored_gates`. A final
package rejects all exclusions. READY_WITH_WARNINGS is accepted only with `passed=true` and an empty
error list; warnings are retained. NOT_READY and contradictory readiness states fail.

The aggregator captures full LOW identity at the start, checks full LOW identity again at the end
and confirms HIGH remains unchanged. `validation_snapshot_stable=false` prevents acceptance even
if separate earlier measurements passed. Numerical success retains `visual_verification=required`.

## Package contract

```text
production.game_mesh_prepare_package(
    relationship,
    bake_pair,
    triangulated_relationship=null,
    qa_report=null,
    options=null,
    dry_run=true
)
```

The bake pair must refer to the final LOW in the same relationship identity and remain fresh,
including its cage. The triangulated relationship must have the same HIGH and an actual triangle
evaluation mesh. If it refers to a separate LOW, sampled geometric equivalence must pass. Omitting
it is valid only when the final relationship itself meets that requirement. The implementation
reruns aggregate validation and checks pair/LOW freshness before writing and after its checkpoint.

Default dry run returns `package_plan` and `handoff_plan`, without writing a package. Validation can
still evaluate stress poses and create their checkpoints, so dry run is not a promise of no
transient Blender effects. A non-dry-run write additionally requires a mutable asset and creates
a final checkpoint before reserving a new local output directory.

Written files:

- `package.json`, schema `production.game_mesh_package.v1`: asset identity, geometry version,
  source HIGH, candidate LOW, triangulated evaluation references, UV/UDIM measurements,
  weight/morph/material reports, cage/pair, aggregate QA, visual status, warnings and no exports.
- `texture-handoff.json`, schema `production.texture_handoff.v1`: neutral application identity,
  HIGH/LOW references, geometry version, UDIMs/material semantics/texture sets, declared normal
  convention, resolution targets and cage/ray references.
- `integrity.json`: SHA256 values for both files. The tool response also returns the checkpoint.

The handoff lists Normal, AO, Curvature, Position, Thickness, WorldSpaceNormal and ID as **expected
future maps**. `bake_maps_generated` is empty in that contract. This does not erase separate smoke
artifacts or claim the other maps were produced. M5 produces only bounded NORMAL/AO diagnostics.
The files are local references, not self-contained exports or packed external assets.

## Visual review binding

Without `qa_report`, package readiness is `READY_NUMERIC_VISUAL_PENDING`. Optional visual evidence
must be a local JSON under the game_mesh store, with schema `production.game_mesh_visual_qa.v1`,
the final full `target_fingerprint`, matching `source_object` and a nonempty `images` list whose
files exist. A stale candidate review is rejected.

A reviewed manifest can record `status="reviewed"`, reviewer, observations and explicit limits;
the package then reports `READY_WITH_REVIEW_LIMITS`. That is a recorded review, not an automatic
artistic approval. Matching image paths/fingerprint proves association, not the truth of a written
judgment. Inspect silhouette, hard-edge shading, material boundaries, intersections and deformation
from comparable views, and state unresolved concerns honestly. A still image does not validate a
temporal defect. Do not invent a score or accept a candidate only because every tool executed.

## Limits and recovery

- Surface/normal/cage and silhouette measurements are finite samples, not continuous Hausdorff,
  exhaustive ray-hit or artistic guarantees. Fine details can escape samples or raster resolution.
- Competing disconnected surfaces are guarded. Closely folded surfaces in one connected component
  can still require explicit regions and manual correspondence review.
- Protected quads can retain their vertex positions while changing interior surface through a new
  diagonal. M5 additionally checks protected triangle centers/midpoints; this is still sampled QA.
- The shelf packer and conservative bounding-box padding are deterministic and bounded, not an
  optimal atlas solver. Summed occupancy requires no-overlap validation to be meaningful.
- The bake adapter supports static base geometry or explicitly validated single-armature bind/rest
  geometry. It does not flatten arbitrary evaluated stacks, animated rigs or morph dependencies.
- Failed candidates, previous relationships, checkpoints and diagnostics remain. Revert hides the
  owned candidate and returns the previous relationship; it does not delete or restore arbitrary
  subsequent user edits.
- If Blender times out after a mutation was sent, its outcome is UNCERTAIN. Query the ledger,
  inspect the owned instance and checkpoint, and do not execute a second apply automatically.
- The large controlled geometry probe establishes measured costs for that fixture and profile.
  It does not establish fast UV/bake support for arbitrary multi-million-triangle production assets.

Implementation details: [architecture](architecture.md), [security](security.md),
[tool catalog](tools.md), [retopology](retopology.md), [weights](weight-transfer.md),
[morphs](morph-transfer.md), [UV/UDIM](uv-udim.md), [triangulation/tangents](tangents-triangulation.md),
[bake](bake-preparation.md) and [neutral handoff](substance-handoff.md).
