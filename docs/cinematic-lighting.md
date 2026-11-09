# M10 — iluminación cinematográfica (área E)

Módulos: `src/blender_mcp/production/cinematic/lighting.py` (handlers + payloads) e `imaging.py` (PNG y luminancia en
Python puro, lado host). Contrato general: `docs/cinematic-architecture.md`.

> Regla de lectura de este documento: **MEDIDO** = sale de un render o de una ejecución real en Blender 5.2.1 con la
> cifra y su origen. **PROPUESTA** = punto de partida artístico o umbral elegido, sin pretensión de verdad universal.
> Ninguna cifra de aquí es una nota de calidad.

## 1. Tools

| Tool | Tipo | Qué hace |
|---|---|---|
| `cinematic_plan_lighting` | READ_ONLY | Lee sujeto y cámara de la candidata (depsgraph de la view layer de la candidata con `cm_depsgraph`, en su frame actual, sin `frame_set`) y devuelve un plan KEY/FILL/RIM/PRACTICAL + world + exposición. |
| `cinematic_setup_lighting` | writer (`store.mutate`) | Crea luces propias en `_CIN`, un world propio asignado sólo a la escena candidata y fija exposición/view transform/look de la candidata. |
| `cinematic_validate_lighting` | writer (artefactos) | Renderiza EEVEE + máscaras Workbench exactas de frames muestreados por plano, mide luminancia en el host y escribe informe, hoja de contactos y hoja de exposición. |

### `cinematic_plan_lighting(cinematic, options)`
Opciones (cerradas): `preset` ∈ `model.MOOD_PRESETS` (por defecto `NEUTRAL_THREE_POINT`), `key_subject` (actor_id; por
defecto el primero), `motivated_sources` `[{position, type?, response?, energy?, color?, temperature?, label?}]`,
`exposure_ev`, `shot` (usa la cámara del plano: `state.shots[].camera_id` → `state.cameras[id].object`), `key_side`
(`RIGHT`|`LEFT`). Devuelve `plan` con `schema=production.cinematic_lighting_plan.v1`, `generation_id`,
`candidate_revision`, luces con `location/target/energy/energy_unit/distance_m/response/value_source=PROPOSAL`, world,
exposición planificada, `energy_model` (medido) y avisos (`SOURCE_LIGHTS_IN_CANDIDATE`, `LIGHTING_NO_CAMERA_AXIS`).

### `cinematic_setup_lighting(cinematic, options, apply)`
Opciones: `plan` **o** `lights` (lista explícita), `world {color, strength}`, `exposure_ev`, `view_transform`, `look`,
`replace`. `apply=false` devuelve la propuesta (nombres, energías) sin tocar Blender.
- Nombres: `M10C_<g8>_v<n>_LGT_<ROL>[_k][_r<rev>]` y `..._WORLD[_r<rev>]`; colisión → `CINEMATIC_NAME_COLLISION`.
- Ya hay luces/world propios y no se pasa `replace=true` → `LIGHTING_ALREADY_SET`. Con `replace` se borran sólo las
  luces/world propios registrados (comprobando la marca de propiedad: `LIGHTING_NOT_OWNED` si no lo son).
- Plan de otra cinemática → `LIGHTING_PLAN_MISMATCH`; plan calculado en otra revisión → aviso
  `LIGHTING_PLAN_OLDER_THAN_CANDIDATE`.
- Look: con AgX debe empezar por `AgX - ` (`LIGHTING_LOOK_INVALID`); validado en host y en Blender.
- El payload valida todo antes de crear; si algo falla a mitad (p. ej. `LIGHTING_AIM_DEGENERATE`) borra lo creado,
  repone el world y la vista, y relanza: la huella de la candidata queda igual que en el ledger (medido en el probe).
- Verifica al final que el world fuente es el mismo objeto con la misma huella y que la escena fuente no cambió.
- Nunca toca materiales, mallas, Actions ni el world fuente.

### `cinematic_validate_lighting(cinematic, options, apply)`
Opciones: `frames` **o** `shots`, `samples_per_shot` (def. 3), `flicker_pairs` (def. true: añade f+1 o f−1 dentro del
plano), `size` (ancho; el alto sigue el aspecto de la candidata; def. 320), `samples` (EEVEE `taa_render_samples`,
def. 16), `thresholds`, `max_frames` (def. 48, máx. 240), `frames_per_call` (def. 16), `key_subject`.
`apply=false` devuelve la selección de frames y el coste en imágenes sin renderizar.

Flujo: `require_mutable_asset` → `fresh` → preflight (cámara o markers con cámara; si no, `LIGHTING_NO_CAMERA`) →
reserva `cinematics-qa/<stamp>/lighting/{eevee,mask}` (ruta > 240 caracteres → `LIGHTING_PATH_TOO_LONG`) → renders EEVEE
troceados → máscaras troceadas → métricas en host → `lighting_report.json`, `contact_sheet.png`, `exposure_sheet.png` →
`state.qa.lighting` y `state.lighting.validation` (no invalida la revisión: no cambia el montaje). El resumen guardado se
describe en §1.1.

Cada llamada al bridge captura y restaura: frame, cámara, ajustes de render (`qa_state/qa_restore`), motor, muestras
EEVEE, `film_transparent`, dither, motion blur, freestyle, AA de Workbench, dispositivo de display, view
transform/look/exposición/gamma/curvas/balance de blancos, el shading de Workbench y `Object.color` de todos los
objetos; además canales de los actores (`cm_capture/cm_restore`). Comprueba en Blender que la huella de candidata y
fuente, los ajustes y los colores son idénticos tras restaurar; si no, `LIGHTING_STATE_NOT_RESTORED`. Un fallo incierto
del transporte marca `uncertain_operation` (bloqueo permanente, patrón M8/M9).

GPU: el backend se informa con `qa_render_backend` y se exige `actual_backend` que empiece por `GPU`
(`LIGHTING_GPU_REQUIRED`). Si el motor de la candidata no es EEVEE se avisa `LIGHTING_QA_ENGINE_DIFFERS`.

### 1.1 Resumen guardado y proveedor del informe de calidad

`validate_lighting` guarda el mismo resumen (`schema: production.cinematic_lighting_validation.v1`) en
`state['qa']['lighting']` y `state['lighting']['validation']`, con la forma de proveedor que lee
`review.normalize_section`:

| clave | contenido |
|---|---|
| `candidate_fingerprint` | `state['fingerprints']['candidate']` vigente al validar; `fresh()` comprobó antes de renderizar que coincide con Blender y cada llamada de render verifica que no cambió |
| `passed`, `status` | `passed` = sin hallazgos ERROR; `status` `error` / `warning` / `success` (como la respuesta de la tool) |
| `state_restored` | `true`: cada llamada de render verificó su restauración o lanzó `LIGHTING_STATE_NOT_RESTORED` |
| `findings` | **agrupados** por (código, severidad) con `count`, hasta 20 referencias de frame, planos, actores, rango de valores y umbral (`findings_format: GROUPED_BY_CODE_AND_SEVERITY`); la lista por frame completa está en `lighting_report.json`. Se agrupa porque el ledger guarda el estado completo en cada evento |
| `findings_total`, `findings_by_severity`, `error_count`, `warning_count` | recuentos sobre todos los hallazgos |
| `metrics` | escalares: `exposure_flicker`, `exposure_flicker_units` (`stops`), `exposure_flicker_status`, `exposure_flicker_pairs`, `max_shot_drift_stops`, `frames`, `shots` |
| `metric_definitions.exposure_flicker` | definición escrita (abajo) |

`metrics.exposure_flicker` (objetivo de refinamiento `exposure_flicker` de `cinematic_refine`): **máximo, sobre los
planos validados, de |log2((media_b + 1e-4)/(media_a + 1e-4))|**, con media = luminancia lineal Rec.709 media del frame
EEVEE de QA, y a, b frames consecutivos del mismo plano con la misma cámara (los pares que cruzan un corte se saltan).
Unidad: **stops**. Sin ningún par medido (`flicker_pairs: false` o planos de un frame) vale `None` con
`exposure_flicker_status: NOT_MEASURED_NO_SAME_SHOT_PAIR`: un parpadeo no medido nunca se lee como 0. El 1e-4 es una
definición para no dividir por 0, no una medida.

`lighting_report(ctx, plan, state, options=None)` es el proveedor de la sección `lighting` del informe de calidad.
**Nunca renderiza** (la validación es una escritura que exige `apply=true`):

- devuelve el resumen guardado si su `candidate_fingerprint` es el `state['fingerprints']['candidate']` actual;
- si no, `{'passed': None, 'status': 'NOT_RUN', 'reason': 'LIGHTING_VALIDATION_STALE: <motivo>', 'findings': [],
  'metrics': {}, 'state_restored': None, 'candidate_fingerprint', 'validated_fingerprint'}`. Motivos: no hay
  validación, la candidata no tiene huella, el resumen no trae huella (formato anterior) o la huella cambió (cualquier
  mutación del montaje — cámara, luz, actor, corte — cambia la huella de la candidata).
- `options` no admite claves (`CINEMATIC_OPTION_UNSUPPORTED`).

## 2. Hechos medidos (Blender 5.2.1, RTX 4060, background, `--factory-startup`)

Origen principal: `tests/blender/m10_lighting_probe.py`, ejecución del área en
`informe local privado; véase validation.md` (74/74 checks; la ejecución anterior `bpm10-lighting-5` dio
las mismas luminancias exactas) y re-ejecución de la integración en `bpm10-lighting-8` (85/85, §2.4). Scripts de diagnóstico en
`informe local privado; véase validation.md` (calibración) y `bpm10-lighting-2/` (`dbg5.py`–`dbg7.py`).

### 2.1 Respuesta de las luces en EEVEE (calibración)
Plano diffuse blanco (albedo 1, `ShaderNodeBsdfDiffuse`), cámara ortográfica cenital, view transform `Standard`,
exposición 0, 16 spp, EXR 32 bits, valor del píxel central:

| Luz | Medido | Fórmula | Desviación medida |
|---|---|---|---|
| SUN, S W/m² | 0,318359·S | S/π | +0,02 % |
| POINT 100 W a 2 m | 0,025234·P/d² | P/(4π²d²) | −0,4 % |
| POINT 100 W a 4 m | 0,024063·P/d² | P/(4π²d²) | −5,0 % |
| SPOT 100 W a 2 m | idéntico a POINT | el cono no concentra potencia | 0 |
| AREA 0,05 m, 100 W a 2 m | 0,100937·P/d² | P/(π²d²) | −0,4 % |
| AREA 0,05 m, 100 W a 4 m | 0,09625·P/d² | P/(π²d²) | −5,0 % |
| AREA 1 m, 100 W a 2 m | 0,093125·P/d² | P/(π²d²) | −8,1 % (emisor grande) |
| World color c, strength s, sin luces | 0,992·c·s | c·s | −0,8 % |

El plan invierte estas relaciones: elige una *respuesta objetivo* (valor lineal de un blanco diffuse frente a la luz)
y calcula `P = 4π²·r·d²` (POINT/SPOT), `P = π²·r·d²` (AREA), `S = π·r` (SUN). La caída del 5 % a 4 m es observada,
no explicada aquí.

### 2.2 Otros hechos de la API
- Rango RNA medido: `temperature` 800–20000 K (duro); `energy` soft max 1e6 (SUN soft max 10); `spot_size` 1°–180°;
  `spot_blend` 0–1; `angle` 0–π; `size`/`size_y` ≥ 0.
- `use_temperature=True` multiplica el color por el cuerpo negro: 6500 K no es neutro exacto (SUN 1 W/m²:
  (0,332; 0,313; 0,329) frente a 0,318 sin temperatura); 3200 K → (0,535; 0,275; 0,104).
- **`bpy.ops.render.render(write_still=True, scene=nombre)` renderiza el frame de la escena de la VENTANA y además
  mueve el `frame_current` de la candidata a ese frame.** Con la ventana en el frame 1, pedir los frames 31 y 60 de la
  candidata dio la misma imagen y cámara A; con `with bpy.context.temp_override(scene=candidata):
  bpy.ops.render.render(write_still=True)` (o cambiando `window.scene`) se respetan el frame de la candidata y los
  markers de cámara (frame 31 → cámara B) (`dbg5.py`, `dbg6.py`). El contrato decía lo contrario: ver §6.
- Un objeto que vive sólo en la escena candidata (cámara o luz propia) conserva `matrix_world` obsoleto hasta que se
  evalúa la view layer de la candidata: una cámara en (0,6; −6; 1,4) se leía (0; 0; 0); con
  `temp_override(scene=candidata, view_layer=candidata.view_layers[0])` + `evaluated_depsgraph_get()` se lee bien
  sin cambiar el frame (`bpm10-lighting-4/dbg8.py`). El plan lo hace así; el probe lo comprueba.
- Tras fijar la rotación de una luz propia, `matrix_basis` es correcta al momento y `matrix_world` no (error de
  1,309 rad leído); el error de orientación medido con `matrix_basis` es 0,0 rad.
- Máscaras Workbench (FLAT + color OBJECT + Standard + AA OFF + dither 0, fondo VIEWPORT negro): 0 píxeles no
  clasificados en 24 máscaras de 320×180 (3 presets × 8 frames).
- Decodificador PNG en Python puro = cargador de imágenes de Blender: diferencia máxima 0 códigos en un frame EEVEE
  de 320×180.
- Renders repetidos del mismo frame dan **ficheros** distintos (metadatos del PNG) con **píxeles** idénticos (0 bytes
  distintos, EEVEE y Workbench; `dbg7.py`). Comparar píxeles, no hashes de fichero.
- Tiempo (pared del host por llamada al bridge, incluye captura/restauración), 320×180, EEVEE 16 spp:
  primera llamada (1 imagen, incluye compilación de shaders) 0,47–0,48 s; después 0,130–0,144 s/imagen (dos
  ejecuciones, tres presets cada una). Workbench (máscara): 0,021–0,025 s/imagen. Validación completa (8 frames EEVEE
  + 8 máscaras + métricas en el host): 2,8–3,1 s.

### 2.3 Medidas del fixture del probe (no son objetivos)
Fixture: actor caja 0,5×0,3×1,8 m con material Principled (0,6; 0,45; 0,35), suelo y fondo grises, una luz fuente
POINT 30 W, dos planos con cortes por marker (S1 cámara A, S2 cámara B). Luminancia media lineal (frames muestreados):

| Preset | Media de frame | Media del sujeto | Contraste sujeto/fondo (stops) | % aplastado (≤0,01) |
|---|---|---|---|---|
| HIGH_KEY | 0,3790 | 0,4293 | 0,14 – 0,26 | 0 |
| NEUTRAL_THREE_POINT | 0,2286 | 0,2796 | 0,24 – 0,40 | 0 |
| LOW_KEY | 0,1459 | 0,1419 | −0,57 – 0,39 | ≤ 0,0017 (S1) / 22,1 (S2) |

El orden LOW < NEUTRAL < HIGH sale medido, como pretendían los presets. El 22,1 % aplastado de LOW_KEY en S2 es el
vacío más allá del borde del fondo, no el sujeto (se ve en la hoja de exposición).

### 2.4 Integración (re-ejecución del probe tras el cambio de contrato)

Evidencia: `informe local privado; véase validation.md` (**85/85 PASS, 0 errores**; la
corrida `bpm10-lighting-7` dio también 85/85 antes de cambiar la etiqueta corta de huella). El actor se registra ahora
con `cinematic_register_actor` (antes el probe escribía el registro a mano; `cinematic_create_candidate` vuelve a medir
cada actor y rechaza registros sin datos de integridad con `ACTOR_STALE`).

- Luminancias medias **idénticas** a `bpm10-lighting-6` (frame 0,228604 / 0,145947 / 0,378984; sujeto 0,279586 /
  0,141876 / 0,429288 para NEUTRAL / LOW_KEY / HIGH_KEY): los cambios de integración no alteran las medidas.
- Resumen guardado: `candidate_fingerprint` = huella de la candidata recalculada en Blender tras validar (3 de 3
  presets); `findings` agrupados `LIGHTING_SUBJECT_LOW_CONTRAST` WARNING ×8 / ×6 / ×8; sección `lighting` del informe de
  calidad **PASS** en los tres (sin ERROR), sin ninguna llamada al bridge desde `lighting_report`.
- `metrics.exposure_flicker` (4 pares del mismo plano por preset): NEUTRAL **0,000645**, LOW_KEY **0,00071**, HIGH_KEY
  **0,000405** stops; `max_shot_drift_stops` 0,012646 / 0,012688 / 0,008266. En el fixture ninguna luz está animada:
  lo único que se mueve es el actor (0,5 m en 60 frames); no he separado cuánto de esa cifra aporta él.
- Cambiar el `lens` de la cámara propia A (+5 mm, con `store.mutate`) después de validar cambia la huella de la
  candidata; `lighting_report` devuelve `NOT_RUN` con `LIGHTING_VALIDATION_STALE`, el resumen guardado se conserva y no
  hay llamada al bridge. `review.normalize_section` lo clasifica hoy como `INDETERMINATE` (no `NOT_RUN`).
- **Hecho medido sobre las huellas `m521`**: la huella anterior y la nueva eran distintas pero compartían los últimos
  12 dígitos hex (`83875b5a6204`). Explicación probable, no medida: son restos módulo números de Mersenne y un cambio
  pequeño puede dejar intactos los bits bajos. Por eso el motivo del `NOT_RUN` muestra `sha256:` de 12 dígitos de cada huella completa, no su cola.
- Tiempos de esta corrida: EEVEE 0,398–0,414 s/imagen después de la primera llamada (en `-6` fueron 0,130–0,144),
  máscara 0,031–0,038 s/imagen, validación completa 5,4–5,8 s (antes 2,8–3,1). Al terminar, `nvidia-smi` mostraba la GPU
  al 96 % y 6.492 MiB ocupados por otros procesos; no he aislado la causa, así que estas cifras no son un presupuesto.
- Payloads (`build_code` con los parámetros de peor caso del test unitario): plan 6.816 B / 1.142 nodos AST, setup
  33.101 / 4.016, preflight 9.589 / 713, render (240 frames, 128 objetos en paleta) 82.138 / 5.251; máximo real en el
  probe 42.545 B por llamada (límite propio 140.000 B / 14.000 nodos).

## 3. Propuestas (no medidas)

- Presets (`lighting.PRESETS`): ángulos, distancias (múltiplo del tamaño del sujeto, mínimo 1 m), respuestas, tamaños,
  temperaturas y world por preset. Ejemplos: NEUTRAL KEY a 45°/35°, respuesta 1,0, FILL 0,5 (relación 2:1), RIM 0,8;
  LOW_KEY relación 8:1; HIGH_KEY 1,5:1; HORROR KEY SPOT desde abajo (−35°) a 3200 K; NIGHT_EXTERIOR KEY SUN 0,35 a
  9000 K y +0,5 EV. Son puntos de partida artísticos.
- Punto de mira: 70 % de la altura del sujeto; tamaño mínimo de sujeto 0,3 m.
- Fuentes motivadas: POINT, respuesta 0,4, 2700 K salvo que se indique.
- Umbrales (`lighting.THRESHOLDS`, `threshold_source=PROPOSAL`): recorte ≥ 0,99 y aplastado ≤ 0,01 de luminancia
  lineal; avisos con > 2 % recortado, > 60 % aplastado, sujeto > 5 % recortado o > 25 % aplastado, parpadeo > 0,1 stops
  entre frames contiguos, contraste sujeto/fondo < 0,5 stops, salto de sombra (> 15 % de píxeles del sujeto cambian
  > 1 stop), cobertura < 0,1 %.
- QA a 320 px de ancho, 16 spp; presupuesto de 90 s por llamada (mitad del timeout de 180 s): la primera llamada
  renderiza 1 frame y las siguientes se dimensionan con los s/imagen medidos.

## 4. Definiciones de las métricas

- Luminancia: Y Rec.709 sobre valores lineales decodificados de sRGB del PNG de 8 bits (después del view transform).
- Contraste: `log2((media sujeto + 1e-4)/(media no-actor + 1e-4))`; el 1e-4 es una definición para no dividir por 0.
- Parpadeo: cambio de la media del frame en stops entre frames contiguos del mismo plano; se salta si cambia la cámara.
- `exposure_flicker`: el máximo de |parpadeo| sobre todos los planos validados (§1.1), en stops.
- Deriva: rango (stops) de las medias de los frames muestreados de un plano (sin umbral: el movimiento lo cambia).
- Salto de sombra (proxy): fracción de píxeles del actor presentes en ambos frames contiguos cuya luminancia cambia más
  de `shadow_change_stops`. El movimiento también mueve píxeles: requiere revisión visual.
- `LIGHTING_MASK_INEXACT` es ERROR: una máscara con píxeles no clasificados invalida el contraste.

## 5. Hallazgos técnicos y observaciones artísticas (probe, mirado en las hojas de contactos)

Técnicos (de los números): los tres presets dan `LIGHTING_SUBJECT_LOW_CONTRAST` en casi todos los frames
(0,14–0,40 stops en NEUTRAL/HIGH): el sujeto tiene un valor parecido al fondo gris. Sin recortes en ningún preset.

Artísticos (mirando las imágenes, opinión, no medida): NEUTRAL modela poco una caja y el fondo recibe una mancha
diagonal de luz arriba a la izquierda en S1; HIGH_KEY es plano y el sujeto casi se funde con el fondo (lo esperable en
high key, pero aquí sin separación); LOW_KEY oscurece bien pero en S1 el sujeto queda más oscuro que el fondo
(−0,57 stops), lo contrario de lo que suele buscarse. El corte S1→S2 se ve correcto (cámara B lateral). Ningún
material se tocó para compensar.

## 6. Límites conocidos

- El plan lee el sujeto en el frame actual de la candidata; no muestrea la trayectoria del plano.
- La máscara colorea el objeto del actor y sus `meshes` del registro; hijos no registrados cuentan como fondo.
- Las luces fuente enlazadas en `_SRC` también iluminan la candidata (se avisa); para excluirlas hay que pasarlas en
  `exclude_objects` al crear la candidata. Nunca se editan.
- La QA es EEVEE; si la candidata renderiza en Cycles el resultado final puede diferir (se avisa).
- Dentro de un payload no se puede leer el nombre del renderizador GL (`import gpu` prohibido por safe mode): un driver
  GL por software no se detectaría allí; el probe sí lo comprueba (`gpu.platform.renderer_get()` = RTX 4060).
- (Resuelto en la integración) `cm_light_digest` ya incluye `use_temperature` y `temperature`: un cambio externo de
  temperatura de una luz propia cambia la huella de la candidata.
- La validación queda obsoleta con cualquier mutación del montaje. En `cinematic_refine` con objetivo
  `exposure_flicker`, la medida "después" la pide `review.measure_target` al proveedor justo tras aplicar el cambio: como
  `lighting_report` no renderiza, devuelve `NOT_RUN` y el refinamiento falla con `REFINEMENT_TARGET_UNAVAILABLE` (y se
  revierte). Para que ese objetivo funcione, el área G tiene que volver a validar la luz (render EEVEE) entre aplicar y
  medir; está anotado como pendiente de integración, no resuelto aquí.
- Sin ráfagas de frames: el parpadeo se mide en pares contiguos, no en secuencias largas.

## 7. Verificación

- `uv run --frozen pytest -q -p no:cacheprovider tests/unit/test_m10_lighting.py tests/unit/test_m10_imaging.py tests/unit/test_m10_foundation.py`
  (incluye el resumen con huella y `exposure_flicker`, `lighting_report` vigente/obsoleto, y la sección `lighting` de
  `review.run_section` + `review.measure_target` con un bridge falso que escribe los PNG)
- `tests/unit/test_m31_hints.py -k cinematic`
- `blender -b --factory-startup --disable-autoexec --python tests/blender/m10_lighting_probe.py -- <out>`
