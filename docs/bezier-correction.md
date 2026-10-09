# Bézier-aware diagnostics and corrections

`animation_analyze_bezier` evaluates keyed intervals and inspects left/right
handles, interpolation and tangent context. It reports sampled overshoot,
handle-time folding and disproportionate tangent changes. Fast but monotone
AUTO_CLAMPED motion is a negative fixture: speed alone is not a spike. Findings
are review candidates; overshoot may be intentional anticipation.

`animation_repair_bezier` supports an explicit `channels:[{path,index}]` selection
and `strategy:clamp_overshoot`. Analyze/propose/preview/apply/verify/revert reuse
M3 transactions. Only selected handles are clamped; keys are retained. Quaternion
or axis-angle component repair is refused. Use the separate continuity workflow.

Immutable proposals bind source, context and backend fingerprints. A checkpoint
and new duplicate precede edits. `max_deviation` (default .01) limits native
channel error, `max_velocity_increase` (.1) and `max_acceleration_increase` (.05)
limit native units/frame and units/frame². Native units are not always metres:
rotation Euler channels use radians and other properties their own units.

Verification samples **each changed key interval at 129 points**, including
neighbors whose handles Blender updated. It never spreads a fixed sample count
over the complete Action duration: a remote key at frame 100000 cannot conceal
a brief defect. More than 32000 required samples causes refusal. Newly introduced
tangent findings also fail. This is bounded numerical sampling, not a proof of
continuous equality between samples. Source key/handle snapshots remain exact.

Geometry-aware foot correction uses a separate bounded Hermite resampling path.
It samples source-local derivatives around each key rather than estimating them
from a wide secant: that distinction prevented artificial acceleration spikes
in the real Blender fixture. Source interpolation stays intact in the preserved
Action; the candidate uses BEZIER/FREE handles only for affected channels. Motion
verification includes physical derivatives, unchanged foot rotation, limb plane
and length and the same original contact labels. See [contact model](contact-model.md).
