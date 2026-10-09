# Semantic rig model

M3 separates observed structure, heuristic assignment and mutation eligibility.
`rig_map_semantics`, `rig_classify`, `rig_analyze_controls` and `rig_analyze_limbs`
inspect local rest coordinates, hierarchy, deform flags, constraints, target/pole
relationships, custom control shapes, animation channels, naming and local switch
drivers. Results provide confidence, evidence and alternatives. Confidence is an
evidence score, not a calibrated probability or anatomical certainty.

The current automatic anatomical adapter covers upright biped topology: a pelvis
with bilateral descending three-bone legs and an ascending central torso; lateral
three-bone torso branches are arm candidates. Distal foot children can be toes.
Naming alone never grants mutation permission. Unknown topology remains unknown.
Unnamed sides use the armature-local X convention and confidence is bounded below
the automatic mapping threshold. Explicit `options.mapping` maps semantic roles
to existing exact bone names and records that the evidence was supplied explicitly.

Roles include root, pelvis, spine (and additional spine segments), neck, head,
left/right upper_leg, lower_leg, foot, toe, upper_arm, lower_arm and hand. Controls
separately distinguish explicit IK targets, poles, constrained mechanism bones,
deform bones and uncertain helpers. Twist/roll names are evidence only.

Classifications include mixed IK/FK, switchable IK/FK, IK control rig, FK control
rig, deform-only and unknown. Each limb reports its currently evaluated mode,
candidate controls, constraints and driver/switch relationship. An inactive IK
solver is still observed as rig architecture; it is not mistaken for absent IK.

Supported switch inference recognizes one local SINGLE_PROP driver with direct
or inverse expression. Arbitrary expressions, external drivers, custom solver
graphs and non-biped topology are not silently normalized. Inspections can return
their evidence without making those configurations mutable.

Public collections are paginated (1–500, default 100). Runtime analysis is bounded
at 2048 bones. No vertices or per-frame tracks are returned. The semantic model
is advisory: every operation must validate its explicit adapter and source state.
