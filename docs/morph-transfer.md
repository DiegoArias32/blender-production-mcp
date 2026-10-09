# M5 morph preservation and honest rejection

`production.retopology_analyze_morph_transfer` reports source key names, relative-key names, affected
vertex counts, maximum world-metre displacement, mask groups, changed topology and current exact
preservation. Results remain compact; shape-key coordinates are never dumped through MCP.

M5 **does not certify topology-changed morph transfer**. A high source containing morphs and a low
target with different topology returns `MANUAL_OR_LATER_PIPELINE_REQUIRED`, unknown correspondence
confidence and a concrete risk. `production.retopology_transfer_morphs` can store that rejected
proposal, but preview/apply is refused before creating a candidate. Source shape keys remain intact.
This satisfies the safety gate by rejection; it is not a claim that morph reconstruction is implemented.

The only supported case is an already exact, same-topology copy of all source morph dependencies,
or a source with no morphs (`NOT_APPLICABLE`). Its transaction retains an exact copy and verifies
relative-key state, coordinates and dependent metadata. A missing key or changed shape-key content
is never treated as success. No key is silently deleted, renamed, flattened or baked into the basis.
Sources with shape-key Actions, drivers or active NLA are also safely rejected: this narrow adapter
does not certify complete animated/driver dependencies. Affected world-metre bounds remain a compact
region indicator, without inventing anatomical labels.

Triangulation is a separate safe operation: it changes faces while retaining all vertex coordinates
and indices, and therefore can preserve existing morph blocks. The actual Blender probe verifies
this exact preservation independently of a topology-reduction transfer.

Evidence: `test-artifacts/m5-transfer-3fbdd9a2/report.json`. The controlled source has `Basis` and
`ControlledBulge`; low topology differs. Analysis and apply rejection report the limitation, and
source coordinates are checked unchanged. A same-topology copied source also passes deterministic
triangulation with its morph contents preserved. A real controlled shape-key driver is also read and
safely rejected by the morph capability without attempting a copy. Future surface-correspondence reconstruction is a
deferred pipeline, not an approved implementation inside M5.
