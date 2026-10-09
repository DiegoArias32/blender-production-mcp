# Validation and known limits — 0.9.0

This page separates the inherited Blender qualification from checks performed for the public release. The private development archive contains original logs and signed absolute paths; those records are not shipped in the public repository. Counts below are summarized evidence, not a substitute for reproducing tests.

## Qualified development environment

- Windows 11; Blender **5.2.1 LTS**, build `9e2066aef7ef`.
- Server Python **3.11.16**; Blender's embedded Python **3.13.13**.
- Source release **0.9.0**, closed on 2026-10-08.
- **274** registered production tools, including **38** materials/PBR tools.
- Development regression: **2,046 passed, 1 skipped**; 266 new tests over the preceding milestone. The skip concerns optional upstream compression support.
- Procedural fixtures were used; production character assets were not used to establish compatibility.

## Blender/MCP materials qualification

All 38 materials tools were exercised through MCP. Five material candidates were accepted after actual visual review, with eight reviews and 64 PNGs across the qualification scopes.

The original full MCP run remains a **historical failure: 217/220 checks**. Its three failures arose from two runner-call contract issues and the resulting exception. A separate corrected supplement passed **12/12**, verifying explicit normal convention handling and MASTER confirmation/protection. These are overlapping scopes: do not describe them as 229 unique passing tests or relabel the original run as green.

Three handoffs retained `UNKNOWN` without verified bake evidence. A separate handoff fixture retained `NOT_READY` due to UV stretch. No downstream Substance or Unreal execution was certified by those handoffs.

## Public distribution checks

On 2026-10-08, a fresh Windows checkout environment installed from the frozen lockfile passed **2,046 tests with 1 inherited skip** (224.10 seconds). All 150 tracked Python source files under `src/` and the original LICENSE matched the closed 0.9.0 development release byte for byte. The separate MCP install verifier initialized successfully and discovered all **274 tools** without contacting Blender. JSON/TOML client examples parsed successfully and local documentation links were checked.

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
