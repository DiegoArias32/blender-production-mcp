# M5 normals, triangulation and tangent basis

All Blender API behavior was checked against the installed Blender **5.2.1 LTS**. The implementation
uses actual `Mesh.corner_normals`, `Mesh.calc_tangents`, `Mesh.free_tangents` and BMesh triangulation.
No invented tangent pipeline or automatic face reversal is used.

## Normals

`production.game_mesh_prepare_normals` analyzes face/corner normals, smooth/flat face counts, sharp
edges, hard boundaries and large smooth-boundary face-angle indicators. Invalid/non-finite/zero
normal vectors are errors. Large angles are reported as indicators, not automatically repaired.

The default `normal_policy=preserve`, `custom_normals=preserve` keeps custom normals. Optional flat
or smooth preparation operates on a new candidate. `custom_normals=rebuild` requires a separate
`confirm_rebuild_custom_normals=true`; it sets automatic normal directions on the new copy and reports
the change. Existing authoring/source custom normals are never silently discarded.

## Deterministic triangulated evaluation

`production.game_mesh_triangulation_preview` follows analyze/propose/preview/apply/verify on a new
evaluation candidate; the authoring mesh remains unchanged. The default BMesh policy is `FIXED`
for quads and `EAR_CLIP` for n-gons. Alternatives must be explicit and retain deterministic checks.
The public `FIXED_ALTERNATE` choice maps to the installed BMesh API's `ALTERNATE` enum.

`production.game_mesh_validate_triangulation` independently triangulates two temporary copies and
compares connectivity/coordinate fingerprints, expected triangle count, unchanged vertex order,
morph contents, face normals and surface area measured from the installed source `calc_loop_triangles`
tessellation. A non-planar polygon's projected `Polygon.area` does not replace its real triangle
surface area. Non-planar/complex polygon area deviation is checked
against an explicit tolerance (default 0.001 relative area). Custom normals that need rebuilding
require explicit confirmation.
Protected source regions are also tested on triangle centroids and edge midpoints, not only on vertex
positions. A changed diagonal on a non-planar protected quad must stay within its declared surface
tolerance; the proposal is rejected otherwise. Choosing BEAUTY/FIXED never bypasses that measurement.

Apply verification also compares sampled source/candidate surface and normals, and verifies actual
vertex weights remain identical. Identical vertex positions and boundary coverage provide a silhouette
preservation invariant; independent view QA is still needed for non-planar polygon choices. Optional
explicit `stress_poses` evaluate the authoring/evaluation meshes under the same rig poses using
surface/edge/area measurements, and restore all state. The validation tool is marked mutating because
this optional stress evaluation changes pose temporarily and creates a checkpoint.

The controlled probe verifies 8 authoring quads → 16 evaluation triangles, independent reproducibility,
zero source mutation, explicit stress-pose restoration and exact preservation of existing morphs.
For a non-planar protected quad, changing only its diagonal is rejected with **0.089087 m** interior
surface deviation even though vertex positions, weights and UVs are exact. Matching the installed
source diagonal passes with **1.178e-7 m** maximum deviation against its **1e-5 m** tolerance.

## Blender-generated tangent basis

`production.game_mesh_validate_tangents` works on a temporary mesh copy. It requires a named/active
UV layer and rejects zero-area UV triangles before asking Blender to generate tangents. Compatible
triangles/quads are used directly; n-gons are triangulated using the declared policy. For every corner,
it checks finite unit tangent direction, orthogonality to the corner normal and a ±1 bitangent sign.
Generation errors are returned as Blender's actual failure, not a fabricated success.

The declared normal convention is `OPENGL` or `DIRECTX` (default OPENGL). This records the future
handoff convention; it does not change UVs/normals or invoke Unreal/Substance. Final importers must
use matching triangulation, UVs and split/corner normals. Blender's `calc_tangents` pipeline is the
actual tangent source; no importer compatibility claim substitutes for testing that later importer.

Evidence: `test-artifacts/m5-transfer-3fbdd9a2/report.json`, 37/37 domain checks. On the controlled
16-triangle fixture, 48 corner frames are valid; maximum tangent/normal dot is **0** and tangent
length error is about **5.96e-8**. Missing prerequisites/degenerate UVs fail. The fixture and source
remain intact; no authoring triangulation or production texture was created.
