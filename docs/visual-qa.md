# Visual QA for animation correction

Milestone 2 adds `production.qa_render_multiview` and
`production.qa_compare_before_after`. They create real local PNG artifacts using
the existing M1 preview implementation. They do not judge whether animation is
beautiful, realistic or acceptable. Inspect the images after numerical checks.

## Public tools and Python interfaces

```python
render_multiview(bridge, storage, operation_id, object_name, options=None)
compare_before_after(bridge, storage, operation_id, object_name,
                     before_action, after_action, options=None)
```

The MCP wrapper owns the operation ID and operation lock. Object and Action
names are exact existing identifiers. Comparison changes only temporary Action
assignment; it never edits, deletes or duplicates either Action.

Example comparison options:

```json
{
  "frame_start": 1,
  "frame_end": 25,
  "step": 12,
  "max_frames": 3,
  "width": 480,
  "height": 480,
  "views": ["FRONT", "SIDE", "THREE_QUARTER"],
  "quality": "lightweight",
  "montage": true
}
```

## Options and budgets

| Option | Default | Meaning / restriction |
|---|---|---|
| `width`, `height` | 480, 480 | Integers 16–1920 pixels. |
| `frame_start`, `frame_end` | scene range | Integer frames; both Actions use the same values. |
| `step` | automatic | Positive integer spacing. Automatic spacing selects at most `max_frames`; consult returned actual frames. |
| `max_frames` | 3 | 1–24 frames per viewpoint. Explicit oversized requests fail. |
| `views` | FRONT, SIDE, THREE_QUARTER | A nonempty list without duplicates, maximum three. |
| `quality` | `lightweight` | Workbench studio/material preview. `scene` preserves the existing render engine and can be expensive. |
| `margin` | 1.25 | Framing multiplier 1.05–3.0. |
| `montage` | true | Create a contact sheet at the middle sampled frame. |
| `action_slot` | null | Explicit existing slot for single-Action rendering. |
| `before_slot`, `after_slot` | null | Explicit slot identifiers for ambiguous comparison Actions. |

Each request is also limited to 72 rendered images and 48 million output pixels.
The contact sheet is limited to a 3×2 grid and 24 million pixels. The defaults
are samples, not full animation playback. To inspect transitions choose a
suitable explicit range/step, within the stated budgets. No scene FPS change
occurs. Sampling can miss movement between frames.

## Matching viewpoints and timing

World +Z is up. FRONT looks from −Y, SIDE from +X, and THREE_QUARTER from
+X/−Y/+Z. These are documented camera conventions, not an inference of a
character's anatomical front. Rotated/nonstandard characters need interpretation.

For every requested frame in both Actions, framing uses evaluated bounds of the
selected object, its descendants, and meshes using its armature. Armature pose
heads/tails are included. The union determines one orthographic camera per view,
shared by before and after. Root travel remains visible against that fixed
frame. The result reports camera transforms, orthographic scale, actual sample
frames, FPS/base and dimensions.

Other scene objects remain visible and may occlude the subject. Controls far
outside the character can enlarge framing. A pure armature has bounds but no
renderable surface; use a controlled mesh fixture or character mesh to inspect
silhouette. Framing is bounded to 512 related objects and 2048 pose bones.

## State preservation and failure behavior

Preflight rejects non-Object mode, linked/overridden targets, active NLA/tweak
mode, non-REPLACE/full-influence Actions and ambiguous slots. Provide a compatible
slot identifier when there is more than one. QA does not create animation_data
on a previously unanimated object merely to compare Actions.

Supported channels are object/bone transforms, object delta transforms, simple
object/bone custom properties, and constraint influence/mute. Other animated
property paths are rejected before assignment because generic RNA restoration
would be unsafe. Raw transform/custom-property/constraint values are also saved
and restored: switching back to an Action does not reset channels absent from
that original Action. Nested animated custom-property paths are not supported.
Both Actions also start evaluation from that same saved unkeyed baseline;
rendering before never inherits values left by the after-Action bounds pass.

After preflight and budget validation, a copy-only checkpoint is saved before
creating a temporary camera. A single validated Blender payload owns setup,
rendering and `finally` cleanup. It restores the original Action and slot,
last-slot identifier, frame/subframe, camera, render engine, image settings,
resolution, pixel aspect, transparency and modified Workbench shading settings.
Camera-marker bindings are temporarily suspended because Blender can resolve
them again inside `render.render`; every marker and original binding is retained
and restored. Only the operation's new camera object/data are deleted.

The rendering loop reuses M1's `_PREVIEW` template, including disabling compositor,
sequencer and multiview secondary outputs. There are no new materials/lights,
downloaded assets or automatic CPU/GPU changes. In particular, selecting
Workbench does not alter Cycles' device preference.

The previous Render Result buffer is replaced. External files referenced by a
checkpoint are not copied or packed. Scene evaluation can run existing drivers.
Use only trusted working copies; this is not a sandbox for untrusted `.blend`.

On transport failure, state is **unverified**, `comparable=false`, and there is no
automatic retry. The response retains the checkpoint and artifact directory.
The upstream 180-second timeout does not cancel Blender. Inspect its state before
retrying. Blender's Windows PNG backend can reject paths of 260+ characters;
the tool refuses those paths and identifies the saved checkpoint. Select a
shorter artifact workdir rather than changing Windows settings.

## Artifacts and meaning of comparable

Every request reserves a fresh directory, even when an operation ID is reused:

```text
qa/<timestamp-unique-id>/
  intent.json
  before/FRONT/frame_000001.png
  before/SIDE/...
  before/THREE_QUARTER/...
  after/FRONT/...
  after/SIDE/...
  after/THREE_QUARTER/...
  montage.png
  result.json
```

Single-Action rendering uses `preview/` in place of before/after. Existing output
is never reused. PNG signatures and real dimensions are verified; montage
decoding also verifies full PNG checksums/pixels with a small standard-library
implementation and no extra package. The montage uses before on the first row,
after on the second, and requested views in column order. Its frame/layout are
reported explicitly.

`comparable=true` means both complete sequences used matching camera definitions,
timing and resolution, artifacts were verified, and recorded state was restored.
It does not imply correction success, source-motion equivalence or visual
acceptance. `state_restored` concerns the explicitly tracked state; it cannot
guarantee recovery after a crash, forced termination or arbitrary external
scripts. Combine measurements and human/agent visual observation before accepting.

## Verification and performance

`tests/unit/test_m2_multiview.py` covers budgets, safe-mode validation, exclusive
outputs, corrupt/missing PNGs, camera/timing mismatches and uncertain transport.
`tests/blender/m2_multiview_probe.py` builds a weighted prism humanoid and tests
real three-view images, before/after pixel differences, camera markers, original
curves/slots, state restoration, injected failure, NLA refusal and explicit slots.
The probe retains real artifacts and a JSON report under `test-artifacts/`.

`tests/blender/m2_benchmark.py` separately measures 1000-frame/32-bone sampling,
6144-key F-Curve analysis and a bounded one-frame/three-view render. It records
wall time, payload byte counts, evaluation counts and fixture/source hashes.
Measurements describe the installed machine and fixture; they are not universal
performance guarantees or quality scores.

Measured on installed Blender 5.2.1 LTS, 2026-09-23 UTC, in an isolated Windows
background process (one run, socket latency excluded):

| Operation | Wall time | Public JSON | Internal bridge response | Work performed |
|---|---:|---:|---:|---|
| Motion quality, 1000 frames × 32 bones | 988.615 ms | 29,865 bytes | 4,008,076 bytes | 32,000 bone/frame evaluations, 33 tracks including object |
| F-Curve analysis | 328.720 ms | 6,538 bytes | 6,538 bytes | 96 channels, 6,144 keys, 42,336 evaluations |
| Workbench preview, 1 frame × 3 views | 990.503 ms | 8,750 bytes | 11,056 bytes across 4 calls | Three 160×192 PNGs, checkpoint and montage |

Local evidence: `test-artifacts/m2bench-879af742/report.json`. The fixture SHA256,
Action/slot assignment, key count and frame/subframe were verified unchanged.
The internal 4 MB sampling response is a performance consideration for larger
jobs; samples are summarized locally rather than exposed as model payload.
