# M10 — previs, preview, revisión, refinamiento y aceptación (área G)

Código: `src/blender_mcp/production/cinematic/review.py`. Contrato general: [arquitectura](cinematic-architecture.md).
Pruebas: `tests/unit/test_m10_review.py` (sin Blender) y `tests/blender/m10_review_probe.py` (Blender 5.2.1 real).

Las seis tools de esta área son **writers** (no están en `READ_ONLY` ni en `TRANSIENT`): renderizan, escriben
artefactos y eventos del ledger, y en `refine`/`accept` mutan la escena candidata a través de `store.mutate`.

| Tool | Qué hace | Estado |
|---|---|---|
| `cinematic_generate_previs` | un frame representativo por plano (Workbench) + `storyboard.html` + `previs.json` | no cambia el estado |
| `cinematic_render_preview` | secuencia completa por los cortes, APNG, `preview.html`, `review_template.json` | REVIEWED → ASSEMBLED (la revisión anterior deja de valer) |
| `cinematic_quality_report` | agrega la QA técnica de las demás áreas; `passed` sin puntuación | no cambia el estado |
| `cinematic_review` | registra la revisión visual humana del preview vigente | → REVIEWED |
| `cinematic_refine` | ≤ 3 ciclos medidos: aplicar, medir antes/después, preview, revisión obligatoria | ASSEMBLED (pendiente de revisión) |
| `cinematic_accept` | marca la escena candidata `ACCEPTED` tras todos los gates | → ACCEPTED (terminal) |

## 1. Flujo

```
ASSEMBLED ─ render_preview ─ quality_report ─ review(ACCEPT) ─► REVIEWED ─ accept(apply) ─► ACCEPTED
                │                                   ▲
                └─ refine(apply) ─ medida ─┬─ no mejora ─► REVERT verificado (estado anterior)
                                           └─ mejora ─► preview ─► review ─┬─ ACCEPT ─► se conserva
                                                                           └─ REJECT/REFINE ─► REVERT
```

Orden recomendado: preview → quality report → revisión → aceptar. Cualquier mutación posterior (incluido un
refinamiento conservado) invalida render, revisión y quality report (`store.invalidate_review`): hay que repetirlos.

## 2. Previs y preview: cómo se renderiza y por qué

- **Escena candidata, nunca la fuente.** El payload hace `with bpy.context.temp_override(scene=candidata):
  scene.frame_set(f)` y `bpy.ops.render.render(write_still=True, scene=candidata)`. La escena de la ventana no cambia.
- **Los cortes son los markers.** No se desactivan: son el montaje. En cada frame se guarda la cámara que eligió
  Blender tras `frame_set`, la que había tras el render y la que predicen los markers (último marker con cámara en o
  antes del frame; antes del primero, la del primero). Si alguna difiere, `cuts_respected=false` y el resultado es
  `error`.
- **Workbench** (`BLENDER_WORKBENCH`, luz STUDIO, color MATERIAL), resolución con el aspecto de la candidata
  (`size` = lado largo). Se informa el backend con `qa_render_backend`.
- **Restauración completa y demostrada.** Antes: `qa_state`, `cm_capture` de todos los objetos de la candidata, valores
  animables de cámaras/luces/world/exposición y la escena de cada ventana. Después: `qa_restore` → `cm_restore` →
  datos animados → cámara. Se compara todo y además el payload recalcula las huellas de fuente y candidata; el host
  exige que coincidan con el ledger (`CINEMATIC_STATE_NOT_RESTORED` si no).
- **Troceado medido.** La primera llamada renderiza 1 frame para medir segundos/imagen (incluye compilación y
  captura/restauración); las siguientes llevan `min(240, 90 s / (s/imagen))` frames. Cada llamada se mide en el host.
- **Artefactos** (en `<workdir>/cinematic-preview/<stamp>/`): `frames/frame_NNNNNN.png`, `sequence.png` (APNG con
  `animation.generation.render.write_apng`), `preview.html` (velocidad normal, 1/2 y 1/4, frame anterior/siguiente,
  teclado, salto por plano, cámara y plano actuales), `result.json`, `intent.json` y `review_template.json`.
- `temporal_complete` sólo con `step == 1`, todos los frames del rango, PNG verificados, cortes respetados y APNG válido.
  Un preview con `step > 1` sirve de borrador, pero no se puede revisar.
- **Previs**: frame representativo = `representative_frame` del plano → frame del beat del plano → centro del plano.
  Sin planos en el plan, un plano por corte de marker. Metadatos por plano: cámara (marker/planificada/renderizada),
  pose (posición, rotación), lente, sensor, shift, DOF, actores visibles con su fracción visible y altura en pantalla,
  tiempo en segundos y propósito narrativo del plan.

## 3. Revisión (`options.review`)

Claves: `reviewer`, `artistic_observations` (texto o lista), `technical_findings` (opcional, del revisor),
`decision` ACCEPT/REJECT/REFINE, `artifact_reference` (= `output_dir` del preview vigente), `temporal_reviewed: true`,
`shots_reviewed` (todos los planos del preview), `speeds_reviewed` (⊇ {1, 0.25}), `direction_checklist`.

Gates, en orden: puntuaciones prohibidas en cualquier nivel (`score`, `rating`, `aaa_score`, `quality_score` →
`VISUAL_REVIEW_SCORE_FORBIDDEN`); claves desconocidas; `store.review_gate` de M6 (mapea observaciones y hallazgos);
huella del preview = huella del candidato (`VISUAL_REVIEW_STALE`); planos y velocidades (`VISUAL_REVIEW_INCOMPLETE`);
checklist completa para ACCEPT (`VISUAL_REVIEW_CHECKLIST_REQUIRED`); y `store.fresh` (Blender sigue mostrando esa
candidata).

**Guía de dirección artística** (la rellena el revisor; la plantilla trae todo `NOT_FILLED` y nada se autocompleta):
legibilidad del plano, jerarquía visual, énfasis dramático, anticipación, ritmo, composición, movimiento de cámara,
visibilidad, silueta, expresión, mood, transiciones y claridad de acción. Estados `OK`, `ISSUE` (exige nota) o
`NOT_APPLICABLE`. Un REJECT/REFINE puede dejarla parcial.

Se escriben `review.json` y `review.md` (o `review_002…` si el mismo preview se revisa otra vez) en la carpeta del
preview, con **TECHNICAL FINDINGS** (quality report vigente, datos del render y hallazgos del revisor etiquetados como
tales) y **ARTISTIC OBSERVATIONS** (observaciones + checklist) separados. Una revisión con la etiqueta
`SYNTHETIC_REVIEW_FOR_MECHANISM_TEST` queda marcada `synthetic_review: true` y la aceptación lo avisa.

## 4. Refinamiento (`cinematic_refine`)

`options`: `target`, `goal` (si el objetivo lo pide), `scope` {camera, actor, frame_start, frame_end}, `changes`,
`minimum_improvement` (positivo, en unidades de la métrica; obligatorio con `apply=true`), `render` {size}.

| target | dirección | medida (misma definición antes y después) |
|---|---|---|
| `actor_screen_height` | menor | máx. de \|altura proyectada del AABB del actor / alto del frame − goal\| |
| `headroom_error` | menor | máx. de \|(1 − borde superior proyectado) − goal\| |
| `visibility_min_fraction` | mayor | mín. de la fracción de 9 puntos del AABB (8 esquinas + centro) dentro del frame y sin ocluir |
| `camera_max_jerk` | menor | máx. \|3.ª diferencia de la posición evaluada de la cámara\| · fps³ dentro de cada tramo de una misma cámara (los cortes no cuentan) |
| `max_penetration_m` | menor | `metrics.max_penetration_m` del proveedor de staging, si lo publica |
| `exposure_flicker` | menor | `metrics.exposure_flicker` del proveedor de luz, si lo publica |

Cambios admitidos (todo lo demás se rechaza): `camera_settings` (lens, sensor, shift, clip, focus_distance,
aperture_fstop de una cámara **propia**), `camera_offset` (delta de una cámara propia), `light_energy` (luz propia),
`exposure` (de la escena candidata), `actor_offset` (delta de un actor compartido: se registra como fila `touched`
`delta`; padre/constraints → `ACTOR_STAGING_UNSUPPORTED`, deltas animados → `ACTOR_DELTA_ANIMATED`) y `clip_shift`
(mueve con `frame_start_ui` un strip de una pista NLA propia). Un canal animado → `REFINEMENT_CHANNEL_ANIMATED`.

Orden: validación pura (un target o cambio inválido **no consume ciclo**) → refinamiento pendiente →
`refinement_gate` (`REFINEMENT_LIMIT` en el 4.º) → frescura → *preflight* de propiedad/animación en Blender (sin
cambiar nada) → medida "antes" → **consumo y registro del ciclo** → aplicar con `store.mutate` (checkpoint, captura
de valores previos) → medida "después" → si la mejora < `minimum_improvement`: **REVERT** (restaurar lo capturado y
exigir que las huellas de fuente y candidata vuelvan a ser las de antes del ciclo; se recupera el estado anterior:
render, revisión y quality report de esa misma huella) → si mejora: preview temporal y `pending_refinement` hasta la
revisión. ACCEPT conserva el cambio; REJECT/REFINE lo revierte con la misma verificación. Un fallo incierto del
bridge (timeout) durante la medida o el preview bloquea el plan (`uncertain_operation`) en lugar de revertir encima.

Tras un ciclo conservado la validación de luz deja de ser vigente (`LIGHTING_VALIDATION_STALE`: está ligada a la
huella de la candidata). Antes de aceptar: `cinematic_validate_lighting(apply=true)` y otra vez
`cinematic_quality_report`. Medido en la demo A (e2e-5): sin revalidar, el informe no pasó y bloqueó la aceptación.

## 5. Informe técnico de calidad (`cinematic_quality_report`)

Cada sección busca su proveedor por nombre (import perezoso); los parámetros se casan por nombre
(`ctx`, `plan`, `state`, `options`, `req`, …). Si no hay proveedor: `NOT_RUN` con el motivo, **nunca PASS**.

| sección | proveedor | obligatoria cuando |
|---|---|---|
| integridad de fuentes | `actors.integrity_report` (o handler `cinematic_verify_integrity`) | siempre |
| staging | `analysis.collision_report` | ≥ 2 actores |
| sincronización | `performance.sync_report` / `analysis.sync_report` | algún actor tiene clips |
| contactos | `analysis.contact_report` | hay props o interacciones |
| timing facial | `performance.facial_report` | algún actor tiene cara integrada |
| secundario | `performance.secondary_report` | algún actor tiene secundario |
| continuidad / encuadre / visibilidad / movimiento | `analysis.continuity/composition/visibility/motion_report` | siempre |
| luz | `lighting.lighting_report`, o la validación registrada `state.qa.lighting` de la misma revisión del candidato | hay `state.lighting` |
| render | preview vigente (huella, completo, cortes, restauración) | siempre |

Veredicto de un proveedor: PASS sólo con `passed: true` explícito, o `status` success/ok **con** lista `findings` sin
errores; `proposal` = NOT_RUN; sin veredicto = INDETERMINATE. Un proveedor que lanza una excepción en una sección
obligatoria es ERROR; en una opcional es NOT_RUN con el motivo. `passed` = ninguna sección FAIL/ERROR y todas las
obligatorias PASS o NOT_APPLICABLE. ARTISTIC OBSERVATIONS va vacío: lo escribe el revisor. Se guarda
`quality_report.json/.md` y `state.quality_report` con la huella del candidato.

## 6. Aceptación (`cinematic_accept`)

Gates: sin refinamiento pendiente; estado REVIEWED; preview completo de la huella actual; revisión ACCEPT de ese
preview; quality report de la huella actual con `passed`; integridad de fuentes recalculada en el momento (PASS; si no
hay proveedor, `CINEMATIC_INTEGRITY_UNAVAILABLE`). Con `apply=true`: `store.mutate` (rol de asset, frescura,
checkpoint) marca la escena candidata `production_m10_lifecycle = ACCEPTED`. Esa propiedad no entra en la huella del
candidato y se verifica que la huella no cambió. **No sustituye la escena fuente** ni cambia la escena de la ventana.
ACCEPTED es terminal (`GENERATION_TERMINAL` en cualquier tool posterior).

## 7. Hechos medidos (Blender 5.2.1, `m10_review_probe.py`, tres ejecuciones el 2026-10-23)

Fixture inline: rig de un hueso con malla deformada (1,6 m), paredes roja y verde, 60 frames a 30 fps, dos cámaras
propias y dos markers de corte (1 → A, 31 → B). Salidas en `informe local privado; véase validation.md`.
Resultado final: **45/45 checks, 0 errores** (la 1.ª ejecución dio 39/41: los dos fallos eran del propio test — una
búsqueda de la subcadena "score" que encontraba la clave `no_score`, y una cámara de prueba que no era cámara).

- **Cortes**: segmentos renderizados `[[CAM_A, 1, 30], [CAM_B, 31, 60]]`; el píxel superior central de los 60 PNG es
  rojo en 1–30 y verde en 31–60 (cámara A ve la pared roja, B la verde). Previs: plano 1 → frame 15 cámara A, plano
  2 → frame 45 cámara B.
- **Restauración**: tras previs, preview y cada medida, huellas de fuente y candidata idénticas; `frame_current`,
  `scene.camera`, motor, resolución, `filepath` y escena de ventana idénticos.
- **Render**: Workbench a 192×108, 60 imágenes en 2 llamadas (1 + 59): 0,0141 / 0,0163 / 0,0148 s por imagen
  (medido en el host, incluye captura y restauración); llamada más larga 0,75–0,85 s. APNG con 60 frames (`acTL` = 60
  = número de `fcTL`). `gpu.platform.renderer_get()` tras el render: `NVIDIA GeForce RTX 4060/PCIe/SSE2`, backend
  `OPENGL`; `qa_render_backend` informa `GPU rasterizer (OPENGL)`.
- **Medida del refinamiento** (`actor_screen_height`, goal 0,75, cámara A a 6 m): lente 35 mm → altura 0,4768 del
  frame (cálculo a mano: 0,477), error 0,2732; lente 55 mm → error 0,000745 (mejora 0,2725 ≥ 0,02: se conserva tras
  revisión). `shift_x` 0,05 → mejora 0,0 exacta → REVERT verificado. Actor desplazado −3 m → error 0,2562 (empeora)
  → REVERT verificado; la fila `touched` `delta` queda registrada y el delta vuelve a (0, 0, 0) en las dos escenas.
- **Generación B**: exposure, camera_offset, light_energy y clip_shift (+5 frames) aplicados en un ciclo,
  `visibility_min_fraction` 1,0 → 1,0 → REVERT verificado (valores y huellas); un ciclo que mejora (lente) revertido
  por una revisión REJECT; `cinematic_reject` posterior restaura la huella de la fuente del plan.
- **Gates**: puntuación (también anidada), artefacto ajeno, revisión de un preview anterior, falta 0,25×, falta un
  plano, ACCEPT sin checklist, edición externa de la candidata, target inválido y cámara de la fuente (no propia: sin
  consumir ciclo), 4.º ciclo → `REFINEMENT_LIMIT`, aceptar antes del quality report, con decisión REFINE o con un
  refinamiento pendiente; tras aceptar, `refine` y `render_preview` → `GENERATION_TERMINAL`.
- **Quality report con los proveedores reales** (actor registrado con `cinematic_register_actor`): integridad,
  continuidad, encuadre, visibilidad, movimiento y render PASS; staging y contactos PASS (opcionales en esta fixture);
  sincronización, timing facial, secundario y luz NOT_RUN (opcionales aquí: la fixture no los usa). `passed = true` y
  la aceptación se hizo con esos proveedores reales; sólo las revisiones son sintéticas.
- **La fuente nunca cambió**: huella final de la fuente = la del plan; claves de `Actor_Walk` idénticas; sin markers
  ni cámara en la escena fuente.
- **Payloads** (test unitario, 600 frames y 8 cambios): render 36.097 B / 6.691 nodos AST; measure 30.339 / 5.698;
  apply 23.406 / 3.990; revert 18.094 / 3.013; mark 14.901 / 2.221; info 5.548 / 458. Límite del contrato: 140.000 B y
  14.000 nodos.

## 8. Propuestas (no medidas; etiquetadas `PROPOSAL` o `INFERRED_DEFAULT`)

`size` por defecto 480 px; presupuesto 90 s por llamada de render, máx. 240 frames por llamada; medida en bloques de
600 frames; máx. 7.200 frames (120 s × 60 fps) y 2.000 Mpx por preview; velocidades obligatorias 1 y 0,25 (el
reproductor ofrece además 1/2). `minimum_improvement` y `goal` los declara el usuario: el sistema no inventa umbrales.

## 9. Límites conocidos

- Las métricas propias usan el AABB mundial de las mallas evaluadas (no la silueta) y 9 puntos de visibilidad; un rayo
  que choca con el propio actor cuenta como visible. `camera_max_jerk` mide sólo posición, no rotación.
- `max_penetration_m` y `exposure_flicker` dependen de que el proveedor publique esa clave en `metrics`; hoy
  `analysis.collision_report` no la publica en el nivel superior → `REFINEMENT_TARGET_UNAVAILABLE`.
- Sincronización, timing facial y secundario no tienen proveedor en `performance.py`: cuando se usan son obligatorias
  y quedan NOT_RUN, así que el informe no puede pasar hasta que exista el proveedor.
- La luz se toma de la validación registrada por `cinematic_validate_lighting` sólo si es de la misma `revision`;
  cualquier mutación (también una reversión) la deja caducada aunque la huella vuelva a ser la misma.
- Un `clip_shift` conservado no actualiza las filas de clips que `performance` guarda en `state.actors`.
- El APNG usa `round(fps)` como retardo (`write_apng`); el reproductor HTML usa el fps exacto. El render sustituye el
  `Render Result` de Blender. Workbench no juzga materiales ni luz.
- Cada previs/preview/mutación crea un checkpoint (.blend completo). Un timeout en previs/preview se registra pero no
  bloquea el plan (no hay mutación en vuelo; la frescura detecta un estado no restaurado); en `refine` sí lo bloquea.
- La checklist no garantiza que la revisión sea real: sólo la estructura. Las revisiones de la probe son sintéticas.

## 10. Cómo hacer la revisión real

1. `cinematic_render_preview` y abrir `preview.html` (o `sequence.png`).
2. Ver la secuencia completa a velocidad normal y a 1/4; recorrer cada plano y los cortes frame a frame.
3. Copiar `review_template.json`, rellenar observaciones, la checklist y la decisión; `temporal_reviewed: true`.
4. `cinematic_quality_report`, luego `cinematic_review` con ese objeto y, si procede, `cinematic_accept` con
   `apply=true`.
