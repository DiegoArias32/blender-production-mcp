# Rig compatibility is feature-specific

`rig_compatibility_report` reports semantic mapping, IK detection, FK detection,
IK/FK matching, contact locking, mocap retarget and deformation QA separately.
It exposes support conditions, evidence confidence and reasons. It never treats
an inferred anatomical name as permission to modify a rig.

Inspection supports broader structure than mutation. The first M3 mutation
adapters deliberately restrict local, unparented armatures with one active local
Action slot/layer/strip, full REPLACE influence and no active NLA. Muted/locked,
sampled or modified curves, unsupported animated RNA paths, linked/override data
and arbitrary driver graphs are refused. Each domain applies additional guards.

IK/FK operations require a reviewed exact two-bone adapter; deform-only/FK rigs
need the separate validated correction/retarget path. Retargeting requires a
complete reviewed source/target mapping and compatible rest poses. Static contact
geometry does not imply support for moving collision surfaces. Deformation metrics
describe geometry; they cannot certify anatomical or artistic quality.

Unknown configurations remain `unsupported_or_ambiguous`. The recommended next
step is to inspect and propose a narrow adapter or manual alternative, preserving
the original rig. Never delete constraints/drivers, rename bones, flatten Actions
or apply transforms merely to make a compatibility gate pass.

## M3.1 additions

- Global state is part of compatibility: armatures in `pose_position=REST` are
  refused (`POSE_POSITION_REQUIRED`); transactions require Object or Pose mode;
  pose position, IK solver, units, FPS, frame range, modifiers and object
  transforms are fingerprinted, so changing them makes a proposal stale.
- Rest poses: compatible means identical, or compensable within
  `rest_compensation_max_degrees` per bone with matching body frames (10°); see
  [retarget-math](retarget-math.md). T/A differences are compensated, larger ones
  refused with `REST_POSE_INCOMPATIBLE`.
- Mapping confidence comes from the server (AUD-003), never from the caller.
- Coordinates, measured on fixtures (`m31_coordinates_probe.py`, IK/FK match):
  object scale 0.01 and 3, negative X scale, non-uniform (1,2,1), rotated
  armature, 60 and 29.97 FPS and unit scale 0.01 gave correct results; a rotated
  parent object is refused. This is evidence for those fixtures, not a blanket
  support claim for sheared or constrained production rigs.
- Scale limits are enforced at propose time (see `capacity` in
  [tools](tools.md)); the IK/FK bake window is additionally limited by the
  safe-mode payload size (about 63 frames at step 1 on the 22-bone fixture).
- The asset role is checked before any persistent mutation
  ([asset safety](asset-safety.md)): a rig that is technically compatible still
  cannot be mutated in a MASTER or unregistered file.
