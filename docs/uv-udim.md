# M5 UV and UDIM preparation

The UV tools consume a current explicit HIGH/LOW relationship. They operate on the base LOW mesh,
with physical measurements in metres. They never change the HIGH or the previous LOW. Generation,
packing and UDIM application use the common M5 proposal → checkpoint → new Candidate → verification
transaction. Failed candidates remain for inspection; they do not replace the previous relationship.

## Tools

- `production.uv_analyze`: layers, islands, UV triangles, tile summaries, overlap, signed orientation,
  zero UV area, invalid/cross-tile faces, stretch, angular distortion, occupancy and density.
- `production.uv_validate`: the same measured gates with configurable policy.
- `production.uv_analyze_texel_density`: texels/metre distribution and material-region/tile summaries.
- `production.uv_generate`: explicit seam plan and `ANGLE_BASED` unwrap (default) or explicitly selected
  `SMART_PROJECT`, followed by deterministic shelf packing. There is no automatic fallback.
- `production.udim_plan`: explicit semantic assignments with `apply=false`.
- `production.udim_apply_plan`: frozen plan application to a new Candidate.
- `production.uv_pack`: one reproducible pack within each existing tile.

Mutation tools accept `mode=analyze|propose|preview|apply|verify|revert`. Analyze/propose take
`relationship` and `options`; later modes take only the stored `operation`, with empty options.

## Explicit seam signals

`seam_edges` are explicit edge indices. Configurable signals are `material_seams=true`,
`angle_seams=true`, `seam_angle_degrees=60`, and `sharp_seams=false`. Boundary edges are included.
The proposal records the complete local seam list and supporting counts. Hard edges are not
automatically treated as seams unless `sharp_seams` is selected. A closed unsplit surface without
a usable seam plan is refused; the agent must propose an appropriate explicit strategy.

`unwrap_method=ANGLE_BASED` uses Blender's installed `bpy.ops.uv.unwrap`. `SMART_PROJECT` explicitly
uses the installed smart-projection API with the configured angle. Both results must pass the same
QA. Smart projection does not promise that every marked seam becomes an island boundary; semantic
UDIM assignment refuses an island spanning different tile assignments rather than cutting it silently.
An immutable UV declaration in relationship `metadata.uv_immutable` or operation `uv_immutable`
refuses UV mutations (`LOCKED_UV_IMMUTABLE`). M5 geometric region protection does not implicitly
authorize modifying an M4 manifest which also promises immutable UVs.

## Measurements and documented policy

For each world-space triangle, the UV/world Jacobian gives singular-value stretch ratio. The ratio
is scale independent: 1 is locally uniform, larger values indicate anisotropic distortion. Corner
angle differences are measured in degrees. Density is `resolution * sqrt(UV_area / world_area_m2)`.
There is no universal texel-density target and no assumption that uniform density is artistically
desirable. `tile_resolutions` can provide explicit resolution overrides per tile. Material regions
receive their own density summaries.

Overlap uses signed-triangle polygon clipping with a spatial broad phase. Shared edges with zero
intersection area are accepted. Pairwise overlap area is a sum of measured intersections; it is not
a union area. Occupancy is summed triangle UV area and is meaningful as a non-overlapping packing
occupancy only when the overlap gate passes. No artistic score is produced.

Defaults, all configurable:

| Setting | Default | Meaning |
|---|---:|---|
| `resolution` | 1024 | Texture width, 32–8192 |
| `padding_px` | 2 | Minimum island/tile-edge padding at declared resolution |
| `max_stretch_ratio` | 8 | Maximum Jacobian singular-value ratio |
| `overlap_area_tolerance` | 1e-10 | UV intersection area tolerance |
| `allow_overlap` | false | Explicit overlapping/mirrored layout policy |
| `allow_flipped` | false | Explicit negative orientation policy |
| `check_padding` | true | Require conservative padding certification |
| `rotate` | false | Allow 90° packing rotation |

Padding uses a conservative island bounding-box lower bound and distance to tile edges. Interleaved
irregular islands can be rejected even when an exact boundary metric would pass. This limitation
is reported. The deterministic shelf packer gives separated bounding boxes and performs only a
bounded 24-step common-scale search. It does not optimize occupancy indefinitely.

## UDIM assignment

Assignments are explicit `[{region, tile, material_index}]` or `[{region, tile, faces}]`, with exactly
one selector. Every face must receive one tile. Duplicate/incomplete assignments and an island
spanning multiple assigned tiles are rejected. Tiles 1001–2000 are supported with up to 100
configured region assignments; no fixed four-tile layout is assumed. Face coordinates on boundaries
belong to the tile containing the centroid, while actual cross-tile faces are rejected.

## Capacity and limits

At most 2 million base-mesh loops, 3 million tested UV triangle pairs, 1 million island-box pairs
and 65,536 grid cells per UV triangle are permitted. Exhaustion marks analysis incomplete and
validation fails; it never reports success. Detail stays inside Blender; responses contain summary
counts and up to 32 problem/island examples. Linked/overridden meshes and unapproved asset roles
cannot be mutated. Geometry, weights, morphs, materials, transforms, normals and rig/modifier
relationships are independently checked for preservation after a UV operation.

The controlled real Blender probe is `tests/blender/m5_uv_bake_probe.py`. It covers single-tile
generation, three semantic UDIM tiles, explicit smart projection, preservation, overlap and stretch
defects, invalid/incomplete assignments, frozen proposals and MASTER refusal. Evidence paths are
recorded in the final M5 report; older failed attempts remain as diagnostic evidence.
