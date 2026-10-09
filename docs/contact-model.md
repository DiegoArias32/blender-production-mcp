# Geometry-aware contacts and conservative locking

`animation_analyze_contacts` retains its M2 ground-plane contract. Supplying
`feet` and `surfaces` selects the M3 adapter; `animation_analyze_foot_phase`
directly uses that adapter. `animation_fix_foot_sliding` routes an immutable
proposal to the backend that created it. M2 safety guards remain unchanged.

## Explicit contact model

Each foot descriptor requires `bone`, `heel`, `sole`, `toe`; markers are XYZ in
bone-local Blender units. Mutation additionally needs `chain:[upper,lower,foot]`
and, for IK, `control`. Semantic inference proposes candidates, never invents
reviewed sole geometry. `confirm_contacts:true` asserts those exact markers and
controls have been reviewed. Example options:

```json
{
  "feet":[{"bone":"Foot.L","control":"ContactControl.L",
    "chain":["UpperLeg.L","LowerLeg.L","Foot.L"],
    "heel":[0,-0.04,0],"sole":[0,0.09,0],"toe":[0,0.22,0]}],
  "surfaces":["Floor"],"frame_start":1,"frame_end":31,
  "confirm_contacts":true
}
```

Names above are examples, not built-in naming requirements. Physical distances
use scene scale_length and evaluated world transforms. BVHs contain evaluated
world-space meshes, built once per operation. A nearest point alone is not
contact. Since M3.1 a marker is in contact only when its **signed** distance along
the oriented support normal lies in `[-max_penetration_m, contact_height_m]`
(slight penetration stays in contact and is reported), its **lateral offset** to
the nearest surface point is at most `contact_height_m` (this rejects coplanar
points beyond a platform edge), and its normal velocity passes the bound.
Surfaces are **two-sided** by default (`surface_orientation="two_sided"`): a face
whose modeled normal points down is flipped toward `support_direction` and reported
as `flipped_for_support`; `"as_modeled"` keeps the strict v0.3.0 behavior. The
query picks the nearest *valid* face among every face in range (not the nearest
face first), and competing surfaces within `ambiguity_margin_m` mark the sample
ambiguous. Generic effectors allow walls. Nearest points support static inclines,
floors, steps and platforms. Animated/parented/constrained geometry, shape keys,
unknown modifiers and external mirror dependencies are refused. The system is
not a collision simulator. Geometry hashes invalidate changed-surface proposals.

HEEL_STRIKE (heel), FOOT_FLAT/MID_STANCE (sole), HEEL_RISE (toe), TOE_OFF and
SWING are heuristic phases with confidence. Each phase reports range, heights,
orientation, horizontal/vertical velocity, surface and tangent drift. Contact
confidence is an evidence score, not a calibrated biomechanical probability.

Each foot reports `contact_status`: `CONTACT_CONFIRMED` (at least
`minimum_contact_samples` contact samples), `NO_CONTACT` (valid surfaces in range,
thresholds never met), `INSUFFICIENT_CONTACT_DATA` (no valid surface near the
markers) or `AMBIGUOUS_SURFACE`. **No contact evidence is not zero sliding**: the
sliding value is `null` unless contact is confirmed, and corrections refuse with
`NO_CONFIRMED_CONTACT` or `AMBIGUOUS_SURFACE` (AUD-005). Measured on the auditor's
inverted-normal floor: v0.3.0 reported 0.0 m sliding; v0.3.1 reports 0.228 m,
`CONTACT_CONFIRMED`, and the IK correction goes 0.0124 → 0.0024 m. Armatures in
`pose_position=REST` are refused (`POSE_POSITION_REQUIRED`).

## Correction

Foot roll rotation remains the original evaluated orientation. Only tangent
translation is locked, changing the anchor from heel to sole to toe. A bounded
triangular temporal kernel blends phase changes; a quintic release returns the
offset to zero in swing. This intentionally permits small transition drift.

IK adapter: explicit independent nondeform target, local full-influence
two-bone solver, no stretch/external pole or unknown limb constraints. FK
adapter: explicit contiguous unscaled unconstrained two-bone chain, supported
inheritance and rotation modes, complete original rotation channels, non-singular
bend plane and reachable targets. It solves rotations only; deform-bone location
is never keyed. Unknown/locked/ambiguous chains return unsupported.

The complete scene range is required for correction. Candidate channels are
resampled to cubic Hermite keys with local source tangents. Original keys,
handles, interpolation and Actions remain untouched. Dense half-step evaluation
checks expected trajectory, preserved roll, protected bones, matched source
phase drift, joint angle and bend-plane steps, knee displacement, length,
linear/angular velocity and acceleration. Verification uses angular vectors:
direction reversals cannot hide behind constant angular speed. Read-only limb
analysis also reports pole movement and permits configured intentional stretch.

Protected bones are compared using world-space head, tail, length, shortest-arc
quaternion orientation, scale components and transform handedness. This includes
unrelated leaf bones: a constraint that rotates a stationary leaf toward the
corrected IK control cannot evade verification through unchanged head position.
The checks cover sampled transforms, not arbitrary handlers or unsampled frames.

## Defaults and limits

Defaults are conservative engineering review limits, not universal artistic
standards. Every physical threshold is configurable; too much correction fails
and restores the source assignment while preserving the candidate/checkpoint.

| Option | Default | Meaning |
|---|---:|---|
| contact_height_m | .01 | nearest-surface distance, 1 cm |
| normal_speed_m_s | .25 | foot normal velocity bound |
| query_distance_m | .3 | nearest search radius |
| minimum_up_normal | .35 | minimum normal component along the support direction |
| surface_orientation | two_sided | `two_sided` or `as_modeled` (strict normals) |
| support_direction | [0,0,1] | world support direction for feet |
| ambiguity_margin_m | .002 | competing-surface margin |
| max_penetration_m | .05 | contact kept (and reported) below the surface down to this depth |
| minimum_contact_samples | 2 | samples needed for CONTACT_CONFIRMED |
| sample_step | .25 | candidate sampling in frames; .125..1 |
| phase_blend_frames / blend_frames | 1 / 3 | phase smoothing / quintic release |
| position_tolerance_m | .001 | expected trajectory error |
| angular_tolerance_degrees | .1 | preserved foot orientation |
| protected_tolerance_m | .0001 | untouched-bone head, tail and length |
| protected_angular_tolerance_degrees | .01 | untouched-bone shortest-arc orientation |
| protected_scale_tolerance | .00001 | absolute world transform scale-component difference |
| sliding_tolerance_m | .002 | accepted drift floor; otherwise ≥60% reduction |
| max_acceleration_increase_m_s2 | 6 | same-sample increase vs source |
| max_velocity_increase_m_s | .25 | same-sample increase vs source |
| max_angular_acceleration_increase_deg_s2 | 200 | vector acceleration increase |
| max_joint_step_increase_degrees | 3 | same-sample joint-step increase |
| max_bend_plane_step_increase_degrees | 5 | same-sample bend-plane increase |
| max_knee_trajectory_change_m | .1 | knee movement budget |
| stretch_tolerance_m | .001 | induced limb-length change |

Analysis: ≤8 feet, ≤32 surfaces, ≤250000 evaluated vertices, ≤4001 frames.
Correction: ≤1600 effector-frame samples; requests exceeding the bridge payload
budget fail rather than lowering precision. `detail:samples`, `offset`, `limit`
page diagnostic samples (max 200); summaries are default.

`animation_analyze_effector_contact` requires an explicit bone/local point.
Default proximity is 5 mm, speed 3 cm/s and dwell 3 frames. A hand passing near a
wall is not classified as stationary contact. Penetration is an oriented local
surface estimate, not closed-volume collision detection. Automatic hand/weapon
correction is outside M3.
