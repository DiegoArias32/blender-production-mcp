# M10 — Línea de tiempo cinematográfica (área C)

Código: `cinematic_manage_timeline` y `timeline_report(ctx, plan, state)` en
`src/blender_mcp/production/cinematic/cameras.py`. Cortes: [camera-continuity.md](camera-continuity.md). Cámaras:
[camera-systems.md](camera-systems.md). Esquema de eventos que consume la QA: [cinematic-visual-qa.md](cinematic-visual-qa.md) §5.

## 1. Qué hace y qué no

- Cambia el **rango** (`frame_start/frame_end`) de la **escena candidata** y crea/sustituye marcadores narrativos
  `BEAT_<id>` y `EVENT_<id>` **sin cámara**.
- Convierte `options.interactions` (resultado de `cinematic_plan_interaction` o lista de eventos en formato del análisis)
  en `state['interactions']`, **exactamente** las filas que lee `analysis.py`, y crea un marcador `EVENT_<event_id>` sin
  cámara por evento (§4).
- Registra conversiones segundos↔frames explícitas en `state['timeline']['conversions']`.
- Devuelve el informe de cobertura (`timeline_report`, §5).
- **Finaliza** el montaje con `options.finalize=true, apply=true`: CINEMATIC_CANDIDATE → `ASSEMBLED` sólo en el ledger,
  sin escribir en Blender (§6).
- **No** cambia `fps`/`fps_base` de la escena fuente, de la candidata ni de ninguna Action. El payload rechaza una candidata
  cuyo fps difiera del plan (`CINEMATIC_FPS_MISMATCH`). Un brief a otro fps ya se rechaza al planificar (`planning.py`).

## 2. Opciones

| clave | significado |
|---|---|
| `frame_start`, `frame_end` | nuevo rango de la candidata (enteros, `end ≥ start`, ≤ 120 s) |
| `markers` | `[{kind: BEAT|EVENT, id, frame | seconds}]` (exactamente uno de frame/seconds) |
| `replace_markers` | por defecto true: sustituye los marcadores que creó `markers` (**no** los de las interacciones) |
| `interactions` | resultado de `cinematic_plan_interaction` **o** lista de eventos en formato del análisis (§4); `[]` las borra |
| `conversions` | `[{seconds | frame, purpose}]` para registrar conversiones |
| `min_shot_frames` | umbral del informe (por defecto 6, PROPUESTA) |
| `finalize` | booleano; `true` finaliza (§6) y no admite otras claves salvo `min_shot_frames` |

Rechazos: `TIMELINE_RANGE_INVALID`, `CINEMATIC_CAPACITY`, `TIMELINE_CUTS_OUTSIDE` (el rango nuevo dejaría cortes fuera),
`TIMELINE_CUT_START_MISMATCH` (el primer corte dejaría de estar en `frame_start`: recrear o vaciar los cortes antes con
`cinematic_create_cuts {clear: true}`), `TIMELINE_INTERACTION_OUTSIDE` (el rango nuevo dejaría fuera la ventana de una
interacción guardada), `TIMELINE_MARKER_OUTSIDE`, `TIMELINE_MARKER_DUPLICATE` (también si `markers` e `interactions`
comparten nombre), `TIMELINE_MARKERS_INVALID`, `CINEMATIC_NAME_COLLISION`, `TIMELINE_FINALIZE_EXCLUSIVE`,
`CINEMATIC_BOOLEAN_REQUIRED`.

Orden de escritura del rango: Blender empuja `frame_end` cuando `frame_start` lo supera (y al revés), así que el payload
escribe primero el extremo que no provoca el empuje.

## 3. Conversión segundos ↔ frames

Única fuente: `model.seconds_to_frame` / `model.frame_to_seconds`.

- `frame = frame_start + floor(seconds · fps / fps_base + 0,5)` (redondeo half-up), registrando `exact_frame` y
  `rounding_error_frames`.
- `seconds = (frame − frame_start) · fps_base / fps`.

Cada conversión se guarda con su `purpose` (`marker:EVENT_turn`, `shot 3 start`, `interaction:<id>:end`...). Medido en el
probe (30 fps, `frame_start` 1): 1,5 s → frame 46; 2,0 s → frame 61.

## 4. Interacciones → `state['interactions']`

Fila guardada (exactamente las claves que lee `analysis._events`; las opcionales sólo aparecen si se conocen):

```
{event_id, kind ∈ model.INTERACTION_KINDS, actor_id, frame, frame_start, frame_end,
 effector: {bone, end: TAIL|HEAD|MID} | null,
 target: {actor_id, bone?, end?} | {object} | {surfaces: [..], generic?} | {point: [m]} | null,
 contact: bool, head_bone?, forward_axis?, reach_m?}
```

### 4.1 Desde `cinematic_plan_interaction` (`production.cinematic_interactions.v1`)

| campo | regla |
|---|---|
| timeline | `fps`, `fps_base` y `frame_start` del resultado deben ser los de la candidata; si no, `INTERACTIONS_TIMELINE_MISMATCH` (nunca se reinterpretan frames) |
| `actor_id` | el primero de `actors` (el que actúa); todos deben ser actores del plan (`ACTOR_NOT_IN_PLAN`) |
| `frame` | el planificado; sin tiempo (`UNSCHEDULED`) → `INTERACTION_FRAME_REQUIRED` |
| `frame_end` | con `duration_s`: `frame(time_s + duration_s) − 1` (conversión registrada); sin duración = `frame` |
| `target` | `ACTOR` → `{actor_id}`; `OBJECT` → `{object}`, salvo `TOUCH_SURFACE` → `{surfaces: [objeto]}`; `POINT` → `{point}` |
| prop | un objeto que es el **original** de un prop de `state['props']` se sustituye por su **copia propia** (el original está excluido de la candidata) |
| `effector` | si es un rol del registro (`hand_right`, `hand_left`, `head`, `root`, `pelvis`, `neck`, `foot_*`) → hueso de `record['bones'][rol]`; rol ausente o `UNKNOWN` → `null` + aviso `INTERACTION_EFFECTOR_UNKNOWN`. Cualquier otro nombre es un hueso explícito. `end` = `TAIL` (valor por defecto del análisis, procedencia `INFERRED_DEFAULT`) |
| `head_bone` | sólo en mirada (`OBSERVE`, `TURN_TOWARD`, `GAZE`): `record['bones']['head']`; si no está, se omite |
| `forward_axis` | el plan no lo trae y no se inventa: se omite (el análisis la marca `GAZE_FORWARD_AXIS_REQUIRED`) |
| `reach_m` | sólo en `PREPARE_ATTACK`: `interaction_capabilities.reach_m` del registro (estimación de reposo, declarada); `UNKNOWN` → se omite |
| `contact` | `contact_required` del plan (booleano obligatorio) |

**Nunca se adivina un hueso por su nombre**: ni para el efector ni para la cabeza.

### 4.2 Desde una lista en formato del análisis

Claves cerradas (`EVENT_KEYS`); `kind` debe estar en `model.INTERACTION_KINDS` (no se aceptan `SUPPORT`/`CONTACT`, que el
análisis admite pero el estado no). Se normaliza: `effector: "hueso"` → `{bone, end: TAIL}`; `target: "x"` →
`{actor_id}` si x es actor del plan, si no `{object}`; `contact` ausente → regla del análisis por tipo (procedencia
`INFERRED_DEFAULT`); `head_bone` ausente en mirada → registro; `event_id` ausente → `<KIND>_<índice>` (el mismo nombre
que usaría el análisis). Los huesos de la lista son explícitos.

### 4.3 Validación y marcadores

- Host: ventanas dentro de la línea de tiempo (`INTERACTION_OUTSIDE_TIMELINE`), ids únicos (`INTERACTION_DUPLICATE`),
  `frame_start ≤ frame ≤ frame_end` (`INTERACTION_WINDOW_INVALID`), destino con exactamente un tipo
  (`INTERACTION_TARGET_INVALID`), eje no nulo (`INTERACTION_FORWARD_AXIS_INVALID`), ≤ 64 eventos (capacidad declarada,
  no límite de Blender).
- Blender, **antes de escribir**: cada objeto referenciado está en la candidata (`INTERACTION_OBJECT_NOT_IN_CANDIDATE`) y
  cada hueso existe en su armature (`INTERACTION_BONE_MISSING`). Un rechazo no deja la candidata a medias.
- Cada fila pasa por `analysis._events`: las que el análisis ignoraría salen como aviso `INTERACTION_NOT_MEASURABLE` con
  **su** motivo (p. ej. `GAZE_FORWARD_AXIS_REQUIRED`, `EFFECTOR_REQUIRED`); nunca se descartan en silencio.
- Marcadores `EVENT_<event_id>` sin cámara, filas `narrative` con `source: 'interaction'`. Reenviar `interactions`
  sustituye **todos** sus marcadores; `replace_markers` sólo sustituye los de `markers`.
- Procedencia por evento en `state['interaction_conversion'] = {origin, events, provenance, warnings, measurable}`.

## 5. Informe de cobertura (`timeline_report`)

Payload de **sólo lectura** (`TIMELINE_READ_BODY`: sin `frame_set`, sin asignar Actions/slots, sin escribir pose; test
`test_timeline_report_payload_is_read_only`). Lee rango, fps, cámara de escena y marcadores
`[nombre, frame, cámara, cámara_propia]` y comprueba en el host:

| comprobación | código | severidad |
|---|---|---|
| fps de la candidata = plan | `CINEMATIC_FPS_MISMATCH` | error |
| rango de la candidata = ledger | `TIMELINE_RANGE_CHANGED` | error |
| planos contiguos desde `frame_start` hasta `frame_end` | `SHOT_GAP`, `SHOT_OVERLAP`, `SHOTS_START_MISMATCH`, `SHOTS_END_MISMATCH` | error |
| cortes rotos | ver [camera-continuity.md](camera-continuity.md) | error |
| los cortes cubren toda la línea de tiempo | `CUTS_DO_NOT_COVER_TIMELINE` | error |
| sin cortes | `CUTS_MISSING` | aviso (en finalize, error `FINALIZE_CUTS_MISSING`) |
| marcador `CUT_` que el ledger no conoce | `CUT_MARKER_UNRECORDED` | aviso (error en finalize) |
| corte ≠ rango de su plano | `CUT_SHOT_MISMATCH` | aviso |
| marcador narrativo con cámara | `TIMELINE_NARRATIVE_MARKER_HAS_CAMERA` | error |
| otro marcador con cámara (cambia la cámara fuera de los cortes) | `TIMELINE_FOREIGN_CAMERA_MARKER` | error |
| marcador narrativo registrado ausente / en otro frame | `TIMELINE_MARKER_MISSING` / `TIMELINE_MARKER_CHANGED` | error |
| clips de actor (`state['actors'][id]['clips']`) dejan huecos | `ACTOR_CLIP_GAP` (la extrapolación puede rellenarlos) | aviso |
| fila de clip sin rango legible | `ACTOR_CLIP_UNREADABLE` | aviso |
| ventana de caché M8 (`state['actors'][id]['secondary']`) fuera / parcial | `SECONDARY_WINDOW_OUTSIDE` / `SECONDARY_WINDOW_PARTIAL` | error / aviso |

Formato de clip leído (tolerante, porque lo escribe el paso de sincronización): `{frame_start, frame_end}` o
`{start, end}`, o `{start, action_frame_start, action_frame_end, scale, repeat}` → fin = start + (afe − afs)·scale·repeat.
Ventana secundaria: `{window: [a, b]}` o `{frame_start, frame_end}`. Lo ilegible se avisa, nunca se da por cubierto.

Devuelve `{schema: production.cinematic_timeline_report.v1, passed, errors, warnings, error_count, warning_count,
scene, markers{cuts, narrative, other}, shots, cuts, actors, interactions, conversions, threshold}`. `passed` = sin
errores. No escribe en el ledger.

## 6. Finalize (CINEMATIC_CANDIDATE → ASSEMBLED)

`cinematic_manage_timeline(options={finalize: true}, apply=true)`. Con `apply=false` devuelve la lista de compuertas y lo
que el ledger ya permite saber (`ledger_errors`, cobertura) **sin llamar a Blender**.

Compuertas (todas se evalúan y el rechazo las lista juntas):

| compuerta | fuente | códigos |
|---|---|---|
| estado | ledger | `CINEMATIC_STATE_INVALID` si no es CINEMATIC_CANDIDATE (directo, sin lista); terminal → `GENERATION_TERMINAL` |
| frescura | `store.fresh` | `CINEMATIC_STALE_SOURCE`, `CINEMATIC_STALE_CANDIDATE`; `CINEMATIC_OPERATION_UNCERTAIN`, `CINEMATIC_BACKEND_CHANGED` y `CINEMATIC_STALE_ASSET` paran antes de leer nada más de Blender |
| línea de tiempo | `timeline_report` | cada error del informe (§5) + `CUT_MARKER_UNRECORDED` promovido a error |
| cortes | ledger | `FINALIZE_CUTS_MISSING`, `FINALIZE_CUT_CAMERA_UNKNOWN` (el corte no apunta a una cámara de `state['cameras']`) |
| planos | ledger | `FINALIZE_SHOTS_MISSING`, `FINALIZE_SHOT_WITHOUT_CUT`; aviso `FINALIZE_CUT_SHOT_UNPLANNED` |
| integridad | `actors.integrity_report` | cada fila `changed` con su código (`BODY_ACTION_CHANGED`, `SOURCE_SCENE_CHANGED`, `CANDIDATE_SCENE_CHANGED`...) |

Si alguna falla: `CINEMATIC_NOT_ASSEMBLABLE: <n> gate failure(s): [compuerta] CODIGO detalle; ...` (hasta 12, con el
total). El ledger no cambia.

Si todas pasan: se registra (`store.record`, sin `store.mutate`, sin checkpoint, sin tocar Blender) `state = 'ASSEMBLED'`
y `state['assembled'] = {schema: production.cinematic_assembled.v1, candidate_fingerprint, source_fingerprint, revision,
gates, timeline_report{passed, error_count, warning_count, warnings, scene, markers, shots, cuts, threshold},
integrity{passed, checked_total, counts, source, candidate, cameras_lights, actors}, shots: [[shot_id, camera_id,
inicio, fin]], interactions, warnings, blender_changed: false}`. `revision` no sube (Blender no cambió); `history` gana
una fila `finalize`.

Cualquier paso de montaje posterior pasa por `store.invalidate_review` y devuelve el estado a CINEMATIC_CANDIDATE: hay
que volver a finalizar. `state['assembled']` puede quedar entonces del montaje anterior (ver §9): quien lo consuma debe
comparar su `candidate_fingerprint` con `state['fingerprints']['candidate']`.

## 7. Estado

`state['timeline'] = {fps, fps_base, frame_start, frame_end, frames, duration_s, markers: [[nombre, frame, cámara]],
narrative: [{name, kind, id, frame, source}], conversions: [...]}` (`source` = `frame` | `seconds` | `interaction`).
`markers` refleja lo leído en Blender tras cada paso de cortes o de línea de tiempo. Además `state['interactions']`
(§4), `state['interaction_conversion']` y, tras finalizar, `state['assembled']` (§6).

## 8. Hechos medidos (Blender 5.2.1 LTS, probe `tests/blender/m10_cameras_probe.py`, corrida `bpm10-cameras-7`: 54/54)

- Interacciones planificadas con `cinematic_plan_interaction` real y convertidas: `touch` (TOUCH_SURFACE, frame 40, 0,3 s)
  → ventana 40–48, efector `hand_right` → `hand.R` del registro, destino `{surfaces: ['Ground']}`; `look` (OBSERVE) →
  `head_bone: 'head'` del registro y aviso `GAZE_FORWARD_AXIS_REQUIRED`; `react` (REACT) sin destino.
- Marcadores `EVENT_touch` (40), `EVENT_look` (20), `EVENT_react` (70) sin cámara; `BEAT_intro` y `EVENT_turn` (de
  `markers`) se conservan; el informe pasa.
- `cinematic_analyze_contacts` sin opciones lee los eventos del estado: `events_source = ['STATE']`, `look` en
  `events_ignored` con `GAZE_FORWARD_AXIS_REQUIRED`, estado restaurado. `touch` mide `NO_CONTACT` (en la fixture la mano no
  llega al suelo: es la medida correcta, no un fallo).
- Un efector `NoSuchBone` se rechaza en el payload con `INTERACTION_BONE_MISSING` y marcadores y ledger quedan iguales.
- Una clave de `Actor_Walk` movida 0,1 → finalize rechaza con 3 compuertas: `[fresh] CINEMATIC_STALE_SOURCE`,
  `[integrity] SOURCE_SCENE_CHANGED`, `[integrity] BODY_ACTION_CHANGED`.
- Restaurada la clave → finalize registra ASSEMBLED: integridad 8/8 UNCHANGED, informe sin errores ni avisos, 3 planos con
  cámara. Marcadores, frame, cámara activa, nº de objetos/Actions/escenas y huellas fuente/candidata idénticos antes y
  después (huella medida con el payload de estado del store). Coste con LocalBridge en la fixture: 0,20 s y 5 llamadas al
  bridge (no extrapolable a escenas reales).
- Tras `cinematic_create_cuts {clear: true}` el estado vuelve a CINEMATIC_CANDIDATE y finalize lista
  `FINALIZE_CUTS_MISSING` + 3 × `FINALIZE_SHOT_WITHOUT_CUT`.
- Cámara B borrada desde fuera → finalize lista `CINEMATIC_STALE_CANDIDATE`, `CUT_CAMERA_MISSING` (×2: marcador y
  ledger), `CUT_MARKER_CHANGED` y `CANDIDATE_SCENE_CHANGED` en un solo rechazo.
- Fallo de medida propio encontrado y corregido (corrida `bpm10-cameras-5` → `-6`): tras `fcurve.update()` una referencia
  Python a una `KeyframePoint` obtenida antes ya no escribe la clave real; la "restauración" no se aplicó (la clave quedó
  en 2,3 en lugar de 2,2). Re-obteniendo la clave tras `update()`, co y ambos manejadores vuelven bit a bit.
- Payload `TIMELINE_BODY` en el peor caso sintético (84 marcadores, 128 filas de comprobación con nombres de 63 caracteres):
  91.013 B / 2.820 nodos AST; caso base 16.098 B; `TIMELINE_READ_BODY` 4.110 B / 546 nodos (límite del encargo 140.000 /
  14.000).

Anteriores (corrida `bpm10-cameras-3`, siguen pasando): 3 planos contiguos (1–30, 31–60, 61–90), 3 cortes, 2 marcadores
narrativos → `passed: true`; con una cámara borrada desde fuera → `CUT_CAMERA_MISSING` y `CUT_MARKER_CHANGED`.

## 9. Límites y pendientes

- `state['assembled']` no se borra al volver a CINEMATIC_CANDIDATE: `store.invalidate_review` sólo limpia render,
  revisión e informe de calidad (cambio propuesto en `store.py`, no aplicado: fuera del área C).
- La mirada necesita `forward_axis` (eje local del hueso de la cabeza) y ni el plan ni el registro lo guardan: hoy sólo
  llega con la lista en formato del análisis. Propuesta: guardarlo en el registro del actor.
- El destino de un actor no lleva hueso desde el plan (el plan no lo tiene): el análisis mide contra sus mallas.
- `reach_m` del registro es una estimación por longitudes de reposo (área B), no un alcance medido.
- Capacidad de 64 eventos y `end = TAIL` por defecto son PROPUESTAS/convenios declarados, no medidas.
- Si el área de planificación guarda `state['shots']`, informe y finalize lo prefieren sobre `plan['shots']`.
