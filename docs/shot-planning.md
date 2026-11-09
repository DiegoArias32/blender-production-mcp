# M10 — Beats, planos, rutas de actores, interacción y plan de render

Código: `src/blender_mcp/production/cinematic/planning.py` (puro) y `planning_handlers.py` (lecturas de Blender).
Brief: `docs/cinematic-briefs.md`. Contrato: `docs/cinematic-architecture.md`.

Las seis tools de planificación son `READ_ONLY`. `cinematic_plan_beats` y `cinematic_plan_shots` no envían ningún
payload; `plan_actor_paths` y `plan_interaction` leen posiciones y bounds (`PRESENCE`); `render_plan` lee las
preferencias de dispositivo (`BACKEND`). Ninguna hace `frame_set`, asigna Actions/slots ni escribe poses.

Toda conversión segundos→frames pasa por `model.seconds_to_frame`:
`frame = frame_start + floor(segundos * fps / fps_base + 0.5)`, y el registro de redondeo viaja en `conversions`.

## 1. Beats (`cinematic_plan_beats(brief, options{beats?, add_establishing?, add_ending?})`)

- **Por defecto**: un beat por evento del brief, en el orden del usuario. Un evento cuyo `kind` ya es un beat lo
  conserva (`USER_REQUIREMENT`); los de interacción se mapean (propuesta): OBSERVE/GAZE/TURN_TOWARD→RECOGNITION,
  PREPARE_ATTACK→THREAT, REACT/RETREAT→REACTION, REACH/TOUCH/HOLD/HANDOFF→INTERACTION. Se antepone ESTABLISHING y se
  añade ENDING como `PROPOSED_CREATIVE_DECISION` (desactivables con `add_establishing/add_ending=false`).
- **Tiempos**: el `time_s` de un evento (o `timing_constraints.at_s`) fija el inicio de su beat. Los bordes no fijados
  se reparten por peso dentro de cada tramo entre bordes fijos (peso 1,0 por beat salvo `weight`): propuesta
  etiquetada en `timing_rule`. Sin solapes; un hueco sólo existe si se declara `gap_before_s` (sale en `gaps`). El
  primer beat siempre empieza en 0. Orden contradictorio o tiempos que no caben → `CINEMATIC_BEAT_TIMES_INCONSISTENT`
  (nunca se reordena en silencio); beat de menos de un frame → `CINEMATIC_BEAT_TOO_SHORT`.
- **Overrides**: `options.beats` como lista = definición completa (`kind` obligatorio; `start_s/end_s/duration_s/
  weight/gap_before_s/purpose/actors/required_actions/camera_intention/staging_constraints/transition/dependencies/
  events`); como dict `{beat_id: cambios}` = parche sobre los beats por defecto (`B01_ESTABLISHING`, …).
- **Campos**: `beat_id, kind, start_s, end_s, duration_s, frame_start, frame_end, duration_frames, gap_before_s,
  purpose, actors, required_actions, camera_intention, staging_constraints, transition, dependencies, events,
  provenance`. Procedencia por campo; una duración pedida queda `USER_REQUIREMENT` aunque su colocación absoluta sea
  propuesta.
- `event_schedule`: tiempo y frame de cada evento (el suyo, o el inicio de su beat como propuesta).
  `constraint_violations`: restricciones de tiempo incumplidas (se informan, no se fuerzan).

## 2. Planos (`cinematic_plan_shots(brief, options{beats?, shots?})`)

- Por defecto un plano por beat; los planos **cubren el timeline exacto** y son contiguos (un hueco declarado de beats
  lo sostiene el plano anterior: `BEAT_GAP_HELD_BY_SHOT`). Lista en `options.shots` = definición completa (frames
  explícitos, segundos o sólo `beat_id`: varios planos del mismo beat se reparten su tramo a partes iguales); dict =
  parche por `shot_id` sin cambiar tiempos. Errores: `CINEMATIC_SHOT_GAP`, `_OVERLAP`, `_COVERAGE`, `_TOO_SHORT`.
- Tipo propuesto por beat: ESTABLISHING→ESTABLISHING, REVEAL→WIDE, APPROACH→TRACKING, RECOGNITION→CLOSE_UP,
  THREAT→LOW_ANGLE, REACTION→REACTION, INTERACTION→MEDIUM, ENDING→WIDE. No se fuerzan todos los tipos.
- Por plano: `shot_id, camera_id (CAM_<shot>), type, beat_id, frame_start/end, duration_frames, start_s/end_s, subjects,
  framing{subject_region, screen_height_fraction, headroom_fraction, horizontal_position, eye_line, threshold_source},
  lens_mm, camera_height{mode, fraction_of_subject_height, height_m}, camera_angle_deg, movement, easing, focus_target,
  transition, suggested_distance_m, provenance`.
- **Todos los valores de encuadre, lente, altura, ángulo y movimiento son PROPUESTAS** (tabla `SHOT_DEFAULTS`; p. ej.
  CLOSE_UP: cabeza al 60 % de la altura de cuadro, 85 mm). `suggested_distance_m = altura_región * lente /
  (fracción * sensor_vertical)`, con la altura del actor **medida** y la fracción de región (cuerpo 1,0, medio cuerpo
  0,5, cabeza 0,15, detalle 0,08) **propuesta**. Sin altura medida → `UNKNOWN`. Las tools de cámara miden después el
  encuadre real.

## 3. Rutas de actores (`cinematic_plan_actor_paths(brief?, options{paths?, clearance_m?, beats?, allow_retime?})`)

- Por defecto, una ruta por evento APPROACH con objetivo. Cada ruta: `actor, start? (por defecto la posición actual
  medida), target ([x,y,z], actor u objeto), stop_distance_m?, orientation_target?, speed_m_s?, departure_s?,
  arrival_s?, action?`.
- **Sólo Actions corporales registradas con `root_motion` medido** (`horizontal_distance_m`, `duration_frames`).
  Clip propuesto: `{action, slot, action_frame_start/end, start_frame, repeat = distancia / distancia_ciclo, scale (1
  salvo retime explícito), strip_frames, end_frame_exact}` más `staging{delta_location_m, yaw_offset_deg,
  requires_rotation}` para que lo aplique `cinematic_stage_actors`/`sync_actions`. **Nunca se genera locomoción.**
- Sin clip compatible (`IN_PLACE`, `ROOT_MOTION_NOT_MEASURED`, sin Actions) → `ALTERNATIVE_REQUIRED` con
  `STATIC_STAGING_WITH_CUT` (posición antes/después del corte y fronteras de beat candidatas) y
  `LOCOMOTION_ACTION_MISSING` (no bloqueante). Action pedida no registrada → `MISSING_PREREQUISITE`.
- Velocidad pedida distinta de la medida del clip (> tolerancia) → `TIMING_CONFLICT` con alternativas (llegada a
  velocidad nativa, retime explícito con `allow_retime=true`, estático + corte). Llegada posterior al final del beat del
  evento → aviso `ARRIVAL_AFTER_BEAT_END`.
- Objetivo actor: `stop_distance = radio_A + radio_B + clearance_m` con radios medidos; sin medida → `NEEDS_INPUT`.
- **Holgura**: trayectorias rectas a velocidad constante entre los frames de salida y llegada, frame a frame; distancia
  XY mínima entre orígenes por pareja contra `radio_A + radio_B + clearance_m` (sin radios → `UNKNOWN`, `passed=null`).

## 4. Interacción (`cinematic_plan_interaction(brief?, options{interactions?, paths?, beats?})`)

- Por defecto: `interaction_requirements` del brief más eventos de tipo interacción. Cada fila: `event_id, kind, actors,
  time_s, frame, spatial_target, contact_required (por defecto true en TOUCH_SURFACE/HOLD_PROP/HANDOFF_PROP),
  action_refs, effector, success_criteria, reachability, warnings`. **No resuelve animación** (`animation_resolved=false`).
- Alcance: `interaction_capabilities.reach_m` del registro contra la distancia XY (menos el radio del objetivo si es un
  actor) en el frame del evento, usando las rutas si se pasan. Sin `reach_m` → `UNKNOWN`, nunca 0.
- Criterios: contacto con los valores por defecto del código M3 (`contact_effector`: 0,005 m, 0,03 m/s, 3 frames);
  mirada/encararse 15° y llegada stop+0,10 m son PROPUESTAS; el resto `VISUAL_REVIEW`. Contacto sin `effector` →
  aviso `EFFECTOR_NOT_DECLARED` (regla M3: los contactos no se infieren por nombres).

## 5. Plan de render (`cinematic_render_plan(cinematic? | brief, options{...})`)

- **Nunca renderiza ni crea carpetas.** Etapas: la preview Workbench de **todo** el timeline va siempre primero;
  EEVEE/Cycles (`quality_preset` DRAFT/PRODUCTION) sólo con alcance explícito (`frame_range` o `shots`), si no
  `CINEMATIC_FINAL_SCOPE_REQUIRED`. Preview siempre PNG (la revisión hashea frames). fps distinto del timeline → rechazo.
- **Cycles exige OptiX en GPU** (regla del usuario): lectura de `preferences.addons['cycles']` sin refrescar ni
  activar nada; sin `compute_device_type=OPTIX` con un dispositivo OPTIX activo → `CINEMATIC_RENDER_OPTIX_REQUIRED`. Nunca
  se planifica CPU.
- `output_path`: absoluta, < 200 caracteres, sin `..`, y que **no exista**; si no se da, la reserva la tool de render.
- Carga estimada **sólo** con tasas medidas y citadas, en la configuración exacta medida; lo demás `NOT_MEASURED` (no se
  extrapola por píxeles ni por muestras). Trozos por llamada = `floor(120 s / tasa_máx)` (120 s es propuesta, bajo el
  timeout de socket de 180 s de `common/transport.py`).

| Motor | Resolución | Muestras | s/imagen | Fuente |
|---|---|---|---|---|
| Workbench | 256×256 | — | 0,026–0,028 | `docs/facial-qa.md` (901 imágenes en 26,0 s, close-up facial) |
| Workbench | 320×180 | — | 0,0185–0,0281 | probe API M10 `bpm10-understand-api` (cubo + luz de área) |
| EEVEE | 320×180 | 16 | 0,486–0,7124 | mismo probe, en caliente (el primer render en frío a 64 spp tardó 1,8044 s con compilación de shaders) |
| Cycles | cualquiera | cualquiera | `NOT_MEASURED` | — |

Muestras por preset: EEVEE PRODUCTION 64 (valor por defecto de Blender, medido), EEVEE DRAFT 16 (propuesta: la
configuración medida), Cycles DRAFT 64 / PRODUCTION 256 (propuestas).

## 6. `normalize_plan` (lo usa `cinematic_create_candidate`)

Conserva el contrato `normalize_plan(brief, beats, shots, source_settings) -> {brief, beats, shots, timeline}` y además
valida: brief listo, `actor_id` únicos y roles válidos; beats con `beat_id` únicos, dentro del timeline, ordenados sin
solapes, conversión segundos↔frames coherente, actores del brief, dependencias existentes y timeline igual al del plan
(`CINEMATIC_BEATS_STALE`); planos con `shot_id` únicos (si están), ordenados sin solapes, dentro del timeline,
vocabularios válidos, sujetos del brief y `beat_id` existente. Los campos opcionales de planos sólo se validan si
están, para no romper filas mínimas de otros subsistemas (`{frame_start, frame_end, camera_id}`).

## Hechos medidos (Blender 5.2.1, RTX 4060, proceso propio en background)

Probe `tests/blender/m10_planning_probe.py` → `informe local privado; véase validation.md`.

- Ejemplo del encargo, 15 s a 30 fps, REVEAL a 2 s y PREPARE_ATTACK a 11 s: beats ESTABLISHING 1–60, REVEAL 61–150,
  APPROACH 151–240, RECOGNITION 241–330, THREAT 331–390, ENDING 391–450; cada `frame_start` recalculado en el probe con
  la fórmula coincide; contiguos. Planos S01–S06 contiguos 1–450.
- Root motion del fixture medido en el probe sobre el hueso raíz evaluado: `Creature_Walk` 1,5 m en 30 frames;
  `Creature_Idle` y `Hero_Idle` 0,0 m.
- Ruta de la criatura (de [0,−6,0] hacia el héroe en [0,2,0]): parada 1,486 m (radios medidos + 0,30 m propuesto),
  distancia 6,514 m, `Creature_Walk` × 4,3427, salida 5,0 s (frame 151), llegada 9,343 s (frame 281) → aviso
  `ARRIVAL_AFTER_BEAT_END` (el beat APPROACH acaba a 8,0 s). Holgura mínima 1,486 m en el frame 281 = la requerida.
- El héroe sólo tiene un clip en el sitio → `STATIC_STAGING_WITH_CUT` con detalle `IN_PLACE`.
- Interacciones del ejemplo programadas en los frames 151, 241 y 331. Sin `reach_m` → `UNKNOWN`.
- Plan de render: preview Workbench 320×180 de 450 frames; estimación 8,3–12,6 s **calculada** con la tasa medida del
  probe API (no medida sobre este fixture). Cycles en un proceso factory (compute `NONE`) → rechazado; tras activar OptiX
  en memoria en ese mismo proceso (sin guardar preferencias) el plan Cycles se acepta con backend
  `GPU OPTIX: NVIDIA GeForce RTX 4060` y carga `NOT_MEASURED`.
- RNA de 5.2.1: `ImageFormatSettings.media_type` {IMAGE, MULTI_LAYER_IMAGE, VIDEO}, `file_format` incluye PNG, OPEN_EXR,
  FFMPEG; `FFmpegSettings.format` MPEG4 y `codec` H264 existen; cámara por defecto `sensor_width` 36 mm y
  `sensor_fit` AUTO; `taa_render_samples` por defecto 64.
- Payloads: `PRESENCE` ≤ 8.581 bytes / 1.629 nodos, `BACKEND` 1.163 bytes / 173 nodos (límites del proyecto 140.000 /
  14.000). Ningún payload enviado contiene `frame_set`, asignación de Action/slot ni escritura de pose.
- Escena antes/después de todas las llamadas: idéntica (huella, frame, poses, Actions activas, conteos); el control
  negativo detecta un cambio de frame y un objeto movido.

## Propuestas etiquetadas (no medidas)

`PROPOSALS` en `planning.py`: umbral "en el sitio" 0,05 m, tolerancia de velocidad 10 %, holgura 0,30 m, tolerancia
vertical 0,05 m, llegada +0,10 m, encararse 15°, yaw 0,5°, presupuesto por llamada de render 120 s, preview de 320 px
de lado largo, primer trozo de 10 frames. Más `SHOT_DEFAULTS`, `REGION_FRACTION`, el mapeo evento→beat y beat→plano, las
frases de `purpose` por tipo y las muestras de los presets salvo EEVEE PRODUCTION.

## Límites

- Las trayectorias son rectas y a velocidad constante: la holgura es una comprobación de planificación, no QA de
  colisión (eso lo mide `cinematic_analyze_actor_collisions` sobre la escena evaluada).
- El clip propuesto asume que la raíz del clip arranca en el origen del objeto y que `duration_frames` es el tramo en que
  se midió `horizontal_distance_m`; `cinematic_stage_actors` lo comprueba al aplicar.
- El alcance compara orígenes, no la mano: es una señal temprana, no la QA de contacto.
- Las tasas de render vienen de escenas simples; una escena real será más lenta y hay que medirla con un primer trozo.
