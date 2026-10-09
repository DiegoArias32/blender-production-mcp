# Safe IK/FK matching and baking

Four tools expose one explicit adapter: `rig_match_fk_to_ik`,
`rig_match_ik_to_fk`, `rig_bake_fk_to_ik`, `rig_bake_ik_to_fk`.
Defaults analyze; propose writes an immutable operation, preview checkpoints and
creates a retained candidate while restoring the source, apply activates a
numerically verified candidate, verify measures it again, and revert restores
the preserved source without deleting the candidate. Source/context fingerprints
must match. Original Action channels are never overwritten.
The exact transaction/backend implementation is fingerprinted too: after an
implementation change, generate and review a new proposal instead of replaying an
older operation against different correction code.

The supported adapter uses an existing two-bone IK constraint on the lower bone,
explicit upper/lower/end bones, nondeform unparented unconstrained target and pole,
no solver stretch/limits, and no extra proximal constraints. A full pose-space
COPY_ROTATION from target to end bone is permitted. An explicit local custom
switch may drive influence through a validated direct/inverse SINGLE_PROP driver;
the exact switch path/values must match its real Blender relationship. Otherwise
the existing solver influence is keyed in the candidate. Constraints and drivers
are preserved, not removed. Extra constraints/driver architectures fail closed.

Options:

```json
{
  "chain": ["UpperLeg.L", "LowerLeg.L", "Foot.L"],
  "ik_target": "ContactControl.L",
  "pole_target": "PoleControl.L",
  "confirm_mapping": true,
  "frame_start": 1,
  "frame_end": 9,
  "step": 1,
  "position_tolerance_m": 0.001,
  "rotation_tolerance_degrees": 0.5
}
```

Matching uses `frame` (default current frame). Baking uses the range and positive
sampling step, bounded at 241 times. Verification covers the key times, their
midpoints and every integer frame inside the window (M3.1, AUD-011). Defaults of
1 mm and 0.5 degrees are configurable engineering tolerances, not a universal
artistic standard. Baking preserves FPS/timing.

### Mutation window (M3.1, AUD-001)

v0.3.0 inserted isolated keys; Blender then recomputed the AUTO handles of the
neighbouring keys and changed the motion **outside** the verified frames (the
auditor measured 0.47 m at unkeyed frames while verification passed). Since M3.1 a
proposal declares `mutation_window`: fence keys at ⌈a⌉−1 and ⌊b⌋+1 hold the source
curve (BEZIER segments are split exactly with de Casteljau using Blender's own
handle clamp; FREE handles are frozen; a non-representable split is refused with
`UNSUPPORTED_BEZIER_SPLIT`), keys are written only inside, and verification
checks (a) untouched curves are identical, (b) written curves match the source
outside the fences within 1e-5, and (c) every bone's world transform outside the
window matches within 1e-5 m / 0.01° (all frames within 10 of the fences, then a
stride so at most 300 outside frames are evaluated on long clips). The switch value inside the window is also
verified, and with a COPY_ROTATION-masked end bone the masked FK rotation is
reported. Measured: outside-window deviation 0.4736/0.4736/0.2385/0.2320 m →
0.0 m; multi-key Bézier switch histories: 0.0 m for single frames; bakes that
cross a Bézier switch key fail closed at verification.

### Preconditions added in M3.1

- `pose_position=REST` is refused (`POSE_POSITION_REQUIRED`), and Object/Pose mode
  is required (`MODE_NOT_SUPPORTED`).
- Capacity is checked at propose time: world verification samples (≤100 000
  bone/frames per pass) **and the payload size**. Proposal data travels inside the
  safe-mode script (limit 200 000 bytes); on the 22-bone fixture the payload grows
  ~2 185 bytes per frame, so a bake window at step 1 is limited to about 63 frames.
  Larger windows are refused with `CAPACITY_EXCEEDED` before any checkpoint.
- Coordinate regressions (`tests/blender/m31_coordinates_probe.py`): FK→IK
  single-frame match was measured correct (≤4.7e-6 m at the frame, 0.0 m outside)
  with object scale 0.01/3, negative X scale, non-uniform (1,2,1) scale, rotated
  armature, 60 and 29.97 FPS and unit scale 0.01. A rotated parent is refused
  explicitly (object parenting unsupported). These are fixture results, not a
  general guarantee for sheared rigs.

FK to IK transfers the evaluated endpoint matrix and derives the pole plane from
the observed bend. A bounded search against Blender's actual solver accommodates
rest axes and pole angle without guessing their relationship. Near-straight limbs
are rejected as ambiguous. IK to FK writes evaluated local pose transforms after
changing the validated mode; it does not blindly translate deform bones.

Verification compares evaluated world-space heads/tails, rotation (q/-q equivalent)
and lengths. Reports include maximum/mean position and angular errors, frames,
affected controls/bones and key counts. Failed candidates remain for diagnosis,
the source assignment is restored, and the checkpoint is returned. Visual QA is
required even when sampled numerical checks pass. Solver execution can exceed the
socket timeout on large ranges; begin with a small reviewed range, never retry an
uncertain mutation automatically.

## Verified controlled fixture

`tests/blender/m3_rig_probe.py` runs the real installed Blender in an isolated
factory-startup process. The final domain run passed 43 checks on Blender 5.2.1
LTS (`test-artifacts/m3rig-92e3411f/report.json`). It includes mixed rig inference,
unusual names, an explicit custom switch, both matching directions, both baking
directions, stale/unknown/unsupported rejection, no-Action transaction recovery,
18 comparable diagnostic renders and checkpoint restore. The fixture file SHA256
and exact original Action contents remain unchanged.

| Operation | Max position error (m) | Max rotation error (degrees) |
|---|---:|---:|
| FK to IK match | 0.00000158055 | 0.00361136 |
| FK to IK bake | 0.000263185 | 0.0318613 |
| IK to FK match | 0.00000150801 | 0.0192654 |
| IK to FK bake | 0.00000211000 | 0.0231248 |
| Custom switch FK to IK bake | 0.000212236 | 0.0347148 |

Bakes cover 9 frames with 17 verification times. The final measured domain
latencies (including checkpoint/transport-template work in a local test bridge)
were 110.775 ms for semantics, 4561.240 ms for FK to IK bake and 4113.486 ms for
IK to FK bake. These are controlled fixture measurements, not a general runtime
guarantee. No per-frame MCP round trips occur.

Before/after renders at frames 1, 5 and 9 were inspected in FRONT, SIDE and
THREE_QUARTER views. The diagnostic segmented proxy retained silhouette, bends
and foot orientation at those samples. Discrete images do not certify continuous
playback or anatomy on production characters. `visual-review.json` records the
inspection scope and verifies pixel equivalence of the final renders to the
inspected run.
