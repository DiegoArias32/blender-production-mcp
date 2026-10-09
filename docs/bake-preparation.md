# M5 bake preparation and diagnostic smoke maps

M5 prepares a game-mesh bake relationship. It does not author production textures or invoke
Substance. Pairing consumes the explicit high/low relationship, not `_high`/`_low` name guesses.

## Tools and manifests

- `production.bake_prepare_pairs`: freezes current HIGH, LOW and optional CAGE signatures,
  relationship ID/revision, material semantics, UV/UDIM, normal convention and ray parameters into
  a workspace-confined `production.bake_pair.v1` manifest with an integrity record.
- `production.bake_analyze_ray_distance`: deterministic bounded LOW triangle samples against HIGH
  BVH; measures nearest deviation, signed front/rear displacement, normal angle and actual ray
  misses. Returns a proposal with `apply=false` behavior; no geometry mutation.
- `production.bake_generate_cage`: M5 proposal/preview/apply/verify/revert transaction producing a
  new LOW-topology cage. The LOW relationship remains the game mesh.
- `production.bake_validate`: `READY`, `READY_WITH_WARNINGS` or `NOT_READY`, with concrete reasons.
- `production.bake_smoke_test`: NORMAL and/or AO maps from a current pair, 32–256 px, 1–32 samples.

Any source, target UV/attribute or cage change after pairing rejects the pair as stale. Smoke options
may only change diagnostic resolution, sample count, margin, map choice and a declared tile subset.
Changing frozen ray/cage/normal parameters requires preparing a new pair.

## Cage and ray metrics

The cage is a new object+mesh derived from the LOW base topology, extruded along transformed vertex
normals by `extrusion_m`. No universal extrusion is imposed. Measurements include actual world-space
vertex offset, sampled penetration risk, sampled projection miss risk and non-adjacent BVH
self-overlap indicators. Topology correspondence includes exact polygon vertex sequences.

Defaults are proposals and configurable gates, not biomechanical or artistic guarantees:

| Setting | Default | Meaning |
|---|---:|---|
| `samples` | 256 | Deterministic LOW triangles, 8–4096 |
| `front_distance_m` / `rear_distance_m` | 0.05 / 0.05 | Declared finite search band |
| `ray_padding_m` | 0.001 | Extra measured-ray proposal clearance |
| `ray_multiplier` | 1.25 | Safety factor for measured signed distance |
| `extrusion_m` | 0.02 | Candidate cage offset |
| `max_cage_offset_m` | 0.1 | Maximum allowed offset |
| `max_ray_distance_m` | 0.2 | Reject excessively broad ray settings |
| `max_surface_deviation_m` | 0.05 | Maximum sampled nearest deviation |
| `cage_tolerance_m` | 0.0001 | Penetration-risk comparison tolerance |

Front/rear metrics are signed relative to the LOW triangle normal. Blender's actual bake operator
exposes one total maximum ray distance, so smoke uses the declared front+rear band and explicit
cage/extrusion; it does not invent separate Blender API properties. Sampling does not certify every
pixel or compute full Hausdorff distance. Extremely broad rays can capture unrelated surfaces and
are rejected rather than enlarged silently.

## Readiness gates and adapter

Validation checks current relationship, base topology, open/non-manifold policy, material IDs,
UV/UDIM gates, actual installed `Mesh.calc_tangents(uvmap=...)` output, ray misses, cage geometry,
transforms and modifier/pose compatibility. Tangent vectors and bitangent signs must be finite and
usable. No fake tangent basis is generated. A NOT_READY state cannot produce smoke maps or a
READY package.

`geometry_adapter=BASE_STATIC` is the default: object animation/constraints and active modifiers
are rejected. `BOUND_REST_MESH` is an explicit narrow adapter accepting one Armature modifier only
when the rig has no animation/constraints and the evaluated coordinates/topology equal the base
mesh within `rest_geometry_tolerance_m=1e-6` (configurable 1e-9–1e-4). A deformed pose, another
modifier or changed evaluated topology is rejected. It does not silently set rest pose, remove
modifiers, apply transforms or rewrite a rig. Nonzero shape-key deformation is refused.

## Smoke isolation, CPU and artifacts

A smoke operation first validates the frozen pair, requires a mutable registered asset, checks the
actual Cycles backend and saves a checkpoint. CPU is refused unless `allow_cpu_fallback=true`.
That explicit approval and actual backend are reported. No preferences are saved or devices changed.

Blender baking can write internal UV bookkeeping. The smoke target is therefore an owned transient
LOW object+mesh copy; source HIGH/LOW attributes are never rewritten. A transient CAGE object with
a short name uses the original cage's read-only mesh because Blender's bake operator has a shorter
name field than the installed Blender datablock name limit. No original cage is renamed. All owned
copies/images/materials are removed in `finally`; selection, visibility, renderer/device/sample and
bake settings are restored. Output PNGs and the local report remain. Full source signatures are
verified again, and changed sections are reported if restoration fails.

Each UDIM tile receives an isolated local diagnostic image. Other tile faces lie outside the
temporary smoke UV tile. Maps preserve declared tangent convention: `OPENGL` +Y or `DIRECTX` -Y.
UVs, material nodes and polygon material IDs exist only on the owned target. NORMAL/AO are generated
by the installed `bpy.ops.object.bake`, not synthesized diagnostic placeholders.

Artifact analysis reports expected UV-covered pixels, nonfinite pixels, unchanged sentinel pixels,
observed coverage, per-channel distribution and large neighboring color-jump indicators. Independent
BVH ray witnesses measure projection misses. These are bounded technical indicators; there is no
claim of complete seam, pixel-hit, artistic texture or production-texture QA.

The real probe includes a three-tile closed pair, too-close/too-far cages, deliberate ray misses,
invalid UVs, actual tangent checks and a nonconstant 1,152-triangle sinusoidal HIGH over a 2-triangle
LOW. The latter demonstrates real varying NORMAL signal, rather than accepting only a constant
neutral normal map. CPU smoke is explicitly approved in that controlled fixture runner only.
