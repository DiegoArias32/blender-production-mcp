# Agent working protocol

Blender Production MCP is a general-purpose derivative of MCP for Blender. Keep production features modular under `src/blender_mcp/production/`. Read [NOTICE.md](NOTICE.md), [security](docs/security.md), [tools](docs/tools.md) and [validation](docs/validation.md) before making substantive changes. Use the language preferred by the user.

## Scope and evidence

- Work only within the user's authorized task. Access to a project is not permission to modify its master file, publish it, use paid services or download assets.
- Inspect the repository, current diff and relevant documentation first. Preserve user changes and important files; back up before modifying existing configuration.
- Verify the installed Blender version and actual RNA/API. Do not invent properties or assume a specific version. Handle modern Action slots/layers/channelbags where applicable.
- Report real errors, unsupported cases, confidence and uncertainty. Do not claim visual quality from a passing unit test, successful render or zero error in an undefined measurement.
- The public validation summary describes bounded tested scopes. Private historical evidence is not included; do not fabricate it or reconstruct claims by inventing results.

## Before controlling Blender

1. Verify the intended process, file, asset role, units, scale, FPS, frame range, rig, Action and NLA state.
2. Use localhost only, safe mode enabled and telemetry disabled. The inherited socket is unauthenticated.
3. Run one controller per Blender instance: production **or** upstream, and one AI client at a time. A production lease cannot prevent a separate upstream client.
4. Never modify a MASTER. Use a registered fixture or an authorized, stamped working copy; preserve the original file.
5. Create a checkpoint with source, time, description and operation association before structural changes, rig/weight edits, large animation changes, transforms or exports that mutate the scene.
6. Prefer API/bpy operations for deterministic work. Use viewport/renders for visual review and GUI only where it materially helps.

## Mutations and recovery

Follow **inspect → analyze → propose → preview → checkpoint → isolated candidate → apply → verify → visual review → explicit accept/revert**. Use `apply=false` or dry-run when offered. Revalidate source and proposal signatures before applying; a stale proposal requires fresh analysis.

- Preserve source Actions, rigs, materials, UV/UDIM, modifiers, shape keys, weights, custom properties and images unrelated to the task.
- Never raise caller confidence to bypass semantic gates. Naming alone is insufficient for anatomical or IK/FK decisions.
- Preserve pose/mode/FPS and restore temporary state. Tools that change frame or pose temporarily are not pure readers.
- After a timeout, inspect `production.operation_status`, the scene and recovery checkpoint. Do not retry an uncertain mutation under a new operation ID.
- Verify the edited window and unaffected frames separately. Absent contact evidence means sliding is unknown, not zero.
- Never delete source evidence or aggressively remove keys to make metrics pass. Artifact cleanup requires an explicit request and starts with dry-run.

## Domain workflows

**Animation:** inspect rig/control compatibility; create a separate Action; plan poses/blocking, timing and spacing; evaluate contacts, foot sliding, root motion, Bézier/quaternion continuity and deformation. Render comparable views and review actual motion. Weight, balance, anticipation, inertia, follow-through, arcs and silhouette require artistic judgment.

**Retargeting:** inspect source/target, validate semantic mapping/confidence and rest pose, preview, then checkpoint and create a new Action. Reassess contacts, root motion, limb pops/stretch and deformation. Do not restructure a rig to conceal unsupported cases.

**Physics:** use explicit regions, roots, pins, colliders and backend compatibility. Preserve primary animation; measure evaluated results. Bake only technically valid candidates and verify each frame. Review temporal output before acceptance. Refinement limits are persistent; do not create a fresh generation to evade them.

**Materials:** inspect source graph and image dependencies, validate color spaces/channel semantics/normal convention, create a signed plan and isolated candidate. Compare actual EEVEE/Cycles previews with equivalent cameras and lighting. Record visual review against current hashes; presets do not certify realism. Preserve source slots and shared images/groups. Keep UV readiness and displacement uncertainty explicit. Do not weaken signatures or safe mode to support another graph.

## Development and tests

- Reuse Bridge, safe mode, Storage, asset guard, checkpoints and OperationLedger. Pass parameters as data; do not interpolate arbitrary Python into payloads.
- Keep upstream connection/addon changes narrow and reviewed; preserve LICENSE and attribution. Follow [upstream synchronization](docs/upstream-sync.md).
- Return bounded structured data: operation ID, affected items, before/after, metrics with units, warnings/errors and checkpoint as pertinent. Use filters/pagination rather than huge vertex or key dumps.
- Run appropriate tests after meaningful changes. `uv run --frozen pytest -q` is the Blender-free suite. Blender probes are explicit opt-in operations on controlled fixtures.
- Do not use user assets as test fixtures. Never run a probe against an occupied port or save user preferences from factory-startup Blender.
- Document verified changes, limitations and reproduction steps. Keep proposals distinct from implemented or accepted behavior.
