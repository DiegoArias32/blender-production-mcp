# Validation and known limits — 0.10.0

This page separates the inherited Blender qualification from checks performed for the public release. The private development archive contains original logs and signed absolute paths; those records are not shipped in the public repository. Counts below are summarized evidence, not a substitute for reproducing tests.

## Qualified development environment

- Windows 11; Blender **5.2.1 LTS**, build `9e2066aef7ef`.
- Server Python **3.11.16**; Blender's embedded Python **3.13.13**.
- Source release **0.10.0**, closed on 2026-10-27 (0.9.0 closed on 2026-10-08).
- **319** registered production tools: the 274 of 0.9.0 (including **38** materials/PBR tools), **39** cinematic tools and **6** complex-skin tools.
- Development regression: **2,698 passed, 1 skipped**; 652 new tests over 0.9.0, with all 2,047 test IDs of the 0.9.0 baseline still present. The skip concerns optional upstream compression support.
- Procedural fixtures were used; production character assets were not used to establish compatibility.

## Blender/MCP cinematics qualification (0.10.0)

- Real Blender probes in own background processes: 11 suites, **783/783 checks**.
- MCP over stdio against an own Blender GUI instance on an isolated port: two scenarios with **87/87** and **34/34** checks (65 and 30 calls). All 39 cinematic tools were called through MCP; the second scenario waited for a real visual review before acceptance.
- Five demo cinematics (creature reveal, two characters, body + face + secondary motion, editorial coverage and a prop hand-over) were accepted after a real visual review. Seven rejected takes are kept in the local evidence. In every accepted demo the source Actions kept their digests and the fixture file on disk kept its SHA-256.
- The 20 required rejection/diagnostic scenarios (MASTER mutation, stale sources, camera clipping, unreachable targets, broken cuts, unsupported render backend and others) were each confirmed by real-Blender checks: 51 checks in total.
- Measured clip continuity with `carry_root`: error ≤ 6e-8 m and 1.6e-6° at the clip boundaries. Without it, an in-place clip sent an actor back 4.0 m at a cut, and the technical QA passed; the visual review caught it.
- Review method correction: several recorded reviews declared 1x and 1/4 playback that had not been fully performed at that moment. This is recorded separately, without rewriting the reviews; where the playback was done later, the decision did not change.
- Complex skin support: MCP qualification 40/40 checks and Blender probes 45/45; a UV import of 4,008,004 loops took 4.34 s and was bit-identical.
- M10-only benchmark in an own background Blender (1–3 simple actors × 3/15 s × 30/60 FPS): 8.13–35.56 s per configuration; Workbench temporal preview 0.018–0.025 s per image at 320 px; largest MCP response 15,334 bytes.

## Blender/MCP materials qualification (0.9.0)

All 38 materials tools were exercised through MCP. Five material candidates were accepted after actual visual review, with eight reviews and 64 PNGs across the qualification scopes.

The original full MCP run remains a **historical failure: 217/220 checks**. Its three failures arose from two runner-call contract issues and the resulting exception. A separate corrected supplement passed **12/12**, verifying explicit normal convention handling and MASTER confirmation/protection. These are overlapping scopes: do not describe them as 229 unique passing tests or relabel the original run as green.

Three handoffs retained `UNKNOWN` without verified bake evidence. A separate handoff fixture retained `NOT_READY` due to UV stretch. No downstream Substance or Unreal execution was certified by those handoffs.

## Public distribution checks

For 0.9.0: on 2026-10-08, a fresh Windows checkout environment installed from the frozen lockfile passed **2,046 tests with 1 inherited skip** (224.10 seconds). All 150 tracked Python source files under `src/` and the original LICENSE matched the closed 0.9.0 development release byte for byte. The separate MCP install verifier initialized successfully and discovered all **274 tools** without contacting Blender. JSON/TOML client examples parsed successfully and local documentation links were checked.

The public checkout includes the full Python regression suite, controlled Blender probes and an installation verifier. See the current [GitHub Actions results](https://github.com/DiegoArias32/blender-production-mcp/actions/workflows/tests.yml) for Python/OS test results. Those jobs do **not** install Blender or qualify Blender runtime behavior on each operating system.

`scripts/verify_install.py` initializes the real stdio MCP process and lists tools using an isolated temporary controller port. It never calls a Blender tool or connects to an addon. A successful run establishes package/transport discovery only. The first `production.scene_audit` from your chosen client is a separate connectivity test.

Client setup syntax was checked against current official documentation. **Codex was used during local Blender qualification. Claude, Gemini CLI, Cursor and VS Code have not all been exercised end to end in this release session.** Their instructions are interoperability recipes, not a blanket certification.

## Material and rendering limits

- Profiles provide editable starting values; they do not certify physical composition or realism.
- Shader construction supports an explicit OpenGL/+Y normal convention. Unknown or incompatible conventions are rejected rather than silently flipped.
- UDIM pixel sampling is limited to the active tile. Tangent checks establish prerequisites, not universal engine equivalence.
- Bump is covered. Geometric displacement is not visually certified and may retain `UNKNOWN`.
- Controlled previews may be low resolution and noisy. Wax-like skin, frosted/opaque ice, matte snow without granular detail and varnished-looking wet surfaces were observed in fixtures. They are not photorealism claims.
- Some active native simulation/cache dependencies cannot be safely isolated for material previews and are rejected.
- CPU rendering fallback requires explicit permission in the relevant tool call.

## Cinematic limits

- Body animation is not generated: clips of existing Actions are combined through NLA. There are no NLA transitions or additive layers, so a clip change is a pose jump that motion QA reports as a warning.
- `carry_root` is refused for an actor with integrated M8 secondary motion. `repeat > 1` with root motion returns the root to the start of each cycle.
- An orbit targeted at `{actor_id}` aims at the actor root (the feet).
- The temporal preview is Workbench: no depth of field and no final materials. Lighting is validated separately with EEVEE on the GPU. Cycles is only planned, and only with OptiX; CPU rendering is never planned.
- Mesh–mesh collision QA has a declared capacity of 3,000,000 vertex-frames; larger scenes need an explicit sampling step.
- At most 8 actors per brief. Thresholds are proposals, not calibrated on production characters. Only Blender 5.2.1 LTS was tested.

## Workflow limits

- Rig adapters are constrained. Supported biped/IK patterns do not imply every humanoid, creature or custom rig is supported.
- Semantic naming is evidence, not proof. Low-confidence mappings do not authorize mutation.
- Retargeting can preserve defects from the source; analyze contacts and motion afterward.
- No contact evidence means unknown sliding, not zero sliding.
- F-Curve corrections use analyze/propose/apply cycles; they do not aggressively remove keys without review.
- Game mesh tools include guided decimation and bounded UV operations; they are not universal automatic retopology.
- Lip-sync requires explicit timing. No speech recognizer or external generative service is provided by the production entry point.
- Numerical QA and visual acceptance are distinct. Review full sequences for temporal defects.

Some historical acceptance runners refer to evidence from earlier local runs. Reproduce their prerequisite fixtures and review steps; do not manufacture approval JSON to force them to pass. The regular pytest suite uses synthetic data and does not require the private archive.
