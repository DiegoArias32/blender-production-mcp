# M5 surface weight transfer and deformation QA

`production.retopology_transfer_weights` uses the explicit high/low relationship and the common M5
immutable transaction. Analyze/propose first; preview/apply derives a **new object and mesh**, creates
a checkpoint and retains HIGH and previous LOW. Source and target content, including actual weights,
are frozen. MASTER is refused before mutation. No user character was used for development.

Correspondence uses the base source's world-metre triangles, a BVH and barycentric interpolation.
Vertex counts and indices may differ. Maximum surface distance, normal angle and ambiguous nearby
components are gates; an unrelated equally near component is refused. Only explicit selected groups,
or existing groups corresponding to armature deform bones, are replaced. Source and target must share
the same armature. Missing/locked groups and invalid source weights are refused. Region groups remain
unchanged. The adapter does not attach/rebuild a rig.

No automatic normalization or influence pruning occurs. Barycentric interpolation preserves source
weight sums; a non-normalized source requires a separate reviewed correction. Verification recomputes
source correspondence and measures actual candidate weights against the predicted values, as well as
sums, unweighted vertices and influences. The configured limits are policy, not universal engine rules.

| Option | Default | Meaning |
|---|---:|---|
| `max_distance_m` | 0.01 m | Correspondence maximum distance |
| `max_normal_angle_degrees` | 60° | Target vertex versus source triangle normal |
| `ambiguity_distance_m` | 0.0001 m | Nearby separate-component ambiguity radius |
| `influence_limit` | 8 | Configurable active influences; no pruning |
| `normalization_tolerance` | 0.0001 | Allowed difference of the sum from 1 |
| `transfer_weight_tolerance` | 0.00001 | Actual versus barycentric expected weights |
| `discontinuity_l1_limit` | 1.5 | Neighboring weight-vector L1 indicator |
| `reject_discontinuities` | false | Indicator becomes a rejection only when requested |
| `leakage_weight_limit` | 0.05 | Wrong-side weight limit for explicit side pairs |
| `deformation_error_m` | 0.005 m | Additional posed P95 surface error above rest baseline |
| `deformation_fraction_tolerance` | 0.001 | Allowed difference in collapse/stretch fractions |
| `deformation_samples` | 1024 | Reproducible vertex samples per surface direction |
| `deformation_vertex_limit` | 250000 | Per-mesh stress evaluation capacity |

`side_pairs` explicitly maps positive/negative groups and `side_plane` specifies world-metre axis,
offset and centre-line margin. Bone names alone never determine a left/right assignment.

`production.retopology_validate_weights` reports unweighted/zero-sum vertices, normalization errors,
maximum/average influences, unexpected bones, edge discontinuity indicators and optional side leakage.
`production.retopology_analyze_deformation_regions` accepts explicit semantic-region group lists or
reports existing deform groups as structural evidence. `minimum_region_vertices` (default 8) is a
declared policy indicator, never proof of anatomically correct joint loops.

Stress poses require explicit region/bone/axis/angle mapping. They run after a checkpoint and restore
the exact channels, Action and frame in `finally`. Source and candidate are evaluated under the **same
pose**. Each mesh's own rest edge/area ratios detect collapse or stretch; sampled bidirectional surface
distance compares high and low without matching vertex indices. Fractions, rather than raw defect
counts, allow different topology resolutions. Active NLA, drivers, constrained/locked controls, REST
pose position and excess evaluation capacity are refused. These geometric measures and finite poses
do not certify artistic or anatomical quality.
Authored polygon/edge connectivity defines topology: Blender may change a bent quad's tessellation
diagonal without changing topology. Edge/area ratios use the reference triangulation on current
vertex positions; surface comparison uses the actual evaluated tessellation.

Real Blender probe: `tests/blender/m5_transfer_probe.py`. Latest domain evidence:
`test-artifacts/m5-transfer-3fbdd9a2/report.json`, 37/37 checks. A 231-vertex weighted source transfers
to a 15-vertex target with independently checked analytic weight error **0**. Shoulder/elbow/knee
stress poses restore correctly. The coarse fixture explicitly allows 0.03 m of posed surface error;
its elbow P95 is about 0.01099 m. This fixture tolerance does **not** replace the 0.005 m default.
Deliberately wrong, normalized weights produce about 0.34081 m elbow P95 and are rejected.
A separate non-planar quad changes its installed tessellation diagonal under an explicit Y=90°
pose. Its identical weighted candidate passes with surface P95 **0**, restores all state, and a
separate authored-polygon connectivity change is still rejected as dynamic topology.

## Explicit material and attribute transfer

`production.retopology_transfer_attributes` shares the transaction. Materials are selected explicitly
by their source slots/face regions, rather than matching object suffixes. A target face whose inset
surface probes cross source material regions is refused, so the centre's material cannot silently
erase a boundary.

Named POINT/CORNER/FACE FLOAT, FLOAT_VECTOR, FLOAT_COLOR, BYTE_COLOR, INT and BOOLEAN attributes are
supported. Continuous values use barycentric interpolation; discrete values use the dominant source
corner; FACE attributes use corresponding source faces. CORNER colors use source triangle **loop**
IDs, preserving the distinction from vertex data. Missing, schema-conflicting, EDGE, internal and
unsupported arbitrary attributes are refused. Custom properties require explicit object/mesh key
lists; production provenance, ID references and unsupported values cannot be copied.

Verification reports maximum error per attribute (1e-5 numeric tolerance; 0.005 for quantized byte
colors), source material semantics and explicit property equality. Source morphs/weights/UVs are
retained by object+mesh copying; transfer never claims arbitrary shader-graph preservation.
