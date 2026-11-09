# M10 — Brief cinematográfico (`cinematic_create_brief`)

Código: `src/blender_mcp/production/cinematic/planning.py` (`normalize_intent`, `build_brief`) y
`planning_handlers.py` (`create_brief`). Contrato general: `docs/cinematic-architecture.md`. Planificación de beats,
planos, rutas, interacción y render: `docs/shot-planning.md`.

## Qué hace y qué no

- Convierte la **intención estructurada** que escribe el agente cliente en un brief
  `production.cinematic_brief.v1`. El servidor **no usa ningún modelo de lenguaje** ni interpreta texto libre: valida
  tipos, vocabularios (`model.py`) y rangos, resuelve actores contra el registro y dice qué falta.
- Es `READ_ONLY`: lee la escena (`lifecycle.source_info`), el rol del archivo (`current_asset`) y un payload propio de
  presencia (`PRESENCE`: existencia de objetos y Actions, `matrix_world`, `dimensions`, frame actual). No hace
  `frame_set`, no asigna Actions ni slots, no escribe poses, no crea datablocks y no escribe en el ledger.
- **No inventa requisitos críticos de historia.** Si falta la duración, un actor no está registrado, una Action pedida no
  existe o una cara pedida no tiene M7 aceptado, el brief sale con `ready=false`, `missing_prerequisites` y
  `open_questions`. Un brief no listo lo rechazan `cinematic_plan_beats/shots` y `normalize_plan`
  (`CINEMATIC_BRIEF_NOT_READY`).

## Entrada (`brief=` de `cinematic_create_brief`)

Claves cerradas (una desconocida = `CINEMATIC_OPTION_UNSUPPORTED`; un brief ya normalizado = `CINEMATIC_INTENT_INVALID`):

| Clave | Tipo | Notas |
|---|---|---|
| `title`, `description` | texto | opcionales |
| `duration_s` | número 0,1–120 | sin ella: `DURATION_REQUIRED` (bloqueante) |
| `fps`, `fps_base` | entero 1–240 / número | por defecto los de la escena; distinto de la escena → `CINEMATIC_FPS_MISMATCH` (nunca se retimea) |
| `frame_start` | entero | por defecto `scene.frame_start` |
| `actors` | `[{actor_id? , object?, role?, label?}]` | al menos `actor_id` u `object`; `role` ∈ `ACTOR_ROLES` |
| `events` | `[{event_id?, kind, actors?, time_s?, target?, description?, action_refs?}]` | `kind` ∈ `BEAT_KINDS ∪ INTERACTION_KINDS`; `target` = nombre (actor u objeto), `[x,y,z]` o `{actor|object|point}` |
| `atmosphere` | `{mood?, mood_preset?, time_of_day?, weather?, notes?}` | `mood_preset` ∈ `MOOD_PRESETS` |
| `camera_intentions` | lista de texto o `{intent, shot_type?, event_id?, beat_kind?, movement?, subjects?}` | |
| `lighting_intentions` | `{preset?, notes?}` | |
| `interaction_requirements` | `[{kind, actors?, target?, time_s?, event_id?, contact_required?, action_refs?, effector?, description?}]` | `kind` ∈ `INTERACTION_KINDS` |
| `timing_constraints` | `[{event_id, at_s? | not_before_s? | not_after_s?}]` o `[{beat_kind, min_duration_s? | max_duration_s?}]` | se comprueban en `plan_beats`, no se fuerzan |
| `required_performances` | `[{actor, body_actions?, facial?, secondary?}]` | `facial`/`secondary`: `true` o lista de refs |
| `open_questions` | lista de texto | se conservan |

Las referencias a actores aceptan `actor_id`, nombre de objeto o `label`. Un actor que sólo aparece en un evento pero
está registrado se añade al reparto con procedencia `INFERRED_DEFAULT`.

## Salida (brief)

`schema, title, description, duration_s, fps, fps_base, frame_start, actors[{actor_id, role, object, label?}],
events[{event_id, kind, actors, time_s, target, description, action_refs}], atmosphere, camera_intentions,
lighting_intentions, interaction_requirements, timing_constraints, available_performances, missing_prerequisites,
risks, open_questions, ready, timeline, source{scene, fingerprint, fps, fps_base, frame_start, frame_end, resolution,
engine}, actor_registry_key, provenance, provenance_groups`.

- `available_performances[actor_id]`: del registro (`body_actions` con `root_motion` resumido, `facial`, `secondary`,
  `dimensions_m`, `interaction_capabilities`, `limitations`) más `measured`: posición, yaw, altura y radio de huella
  **leídos ahora** de la escena (`SCENE_MEASURED`; máximo de `Object.dimensions` del objeto del actor y sus mallas; radio
  = media diagonal XY).
- `normalize_plan` (usado por `cinematic_create_candidate`) sigue leyendo sólo `schema, fps, fps_base, duration_s,
  frame_start, actors[{actor_id, role}]`; el resto viaja congelado en `plan.json`.

## Procedencia: tres valores separados

`provenance` (campo → valor) y `provenance_groups` (valor → campos; las tres claves siempre presentes y sin campos
compartidos):

| Valor | Cuándo |
|---|---|
| `USER_REQUIREMENT` | lo escribió el usuario (duración, eventos, roles declarados, tiempos dados) |
| `INFERRED_DEFAULT` | sale de la escena, del registro o de una regla fija (fps/fps_base/frame_start de la escena, rol registrado, `event_id` E01…, actor añadido desde un evento) |
| `PROPOSED_CREATIVE_DECISION` | decisión creativa propuesta (p. ej. `lighting_intentions = {preset: mood_preset}` cuando el usuario sólo dio la atmósfera) |

## Prerrequisitos que faltan (bloqueantes salvo indicación)

| Código | Causa |
|---|---|
| `DURATION_REQUIRED` | no hay `duration_s` |
| `ACTOR_NOT_REGISTERED` | referencia sin registro (registrar con `cinematic_register_actor`) |
| `ACTOR_AMBIGUOUS` | varios registros para el mismo objeto (huella cambiada): pasar `actor_id` |
| `ACTOR_OBJECT_MISMATCH` | `actor_id` registrado para otro objeto |
| `ACTOR_OBJECT_MISSING` / `ACTOR_NOT_IN_SOURCE_SCENE` | el objeto registrado no está en el archivo / en la escena fuente |
| `ACTION_NOT_AVAILABLE` | Action pedida no registrada para el actor; el detalle distingue "existe en el archivo, re-registrar" de "no existe; M10 nunca genera animación corporal" |
| `ACTION_WITHOUT_ACTOR` | un evento nombra una Action pero ningún actor |
| `FACIAL_PERFORMANCE_NOT_ACCEPTED` | cara pedida sin generación M7 `ACCEPTED` registrada |
| `SECONDARY_NOT_ACCEPTED` | secundario pedido sin M8 `ACCEPTED` registrado |
| `TARGET_OBJECT_MISSING` | un target no es actor ni objeto de la escena fuente |

Riesgos (no bloquean): `EVENTS_UNSCHEDULED` (los tiempos los propone `plan_beats`), `NO_STORY_EVENTS`,
`ACTOR_WITHOUT_BODY_ACTIONS`, `NO_LOCOMOTION_ACTION` (un APPROACH/RETREAT sin clip con root motion medido),
`ACTIONS_SHORTER_THAN_TIMELINE`.

## Hechos medidos (Blender 5.2.1, proceso propio `--factory-startup --disable-autoexec`)

Probe `tests/blender/m10_planning_probe.py`, salida `informe local privado; véase validation.md`:
**30/30 PASS, 0 errores** (la ejecución anterior, `bpm10-planning-2`, dio el mismo resultado).

- Ejemplo del encargo (criatura emerge, se acerca, mira y prepara ataque; 15 s a 30 fps) con dos actores registrados:
  brief `ready`, `timeline` 1–450 (450 frames).
- Brief incompleto (actor `Ghost` sin registrar, `Creature_Run` inexistente, `Orphan_Action` existente pero no
  registrada, cara del héroe sin M7 aceptado): exactamente esos cuatro prerrequisitos, `ready=false`.
- Brief a 24 fps sobre escena a 30 → `CINEMATIC_FPS_MISMATCH`.
- Medidas de entrada leídas por el payload y comparadas con `bpy` directo: `Creature` (armature) `dimensions`
  [0,0; 0,5; 1,5], `CreatureBody` [0,8; 1,6; 1,2] → altura 1,5 m, radio 0,894 m. **Ojo**: el bounding box de la armature
  cuenta en el máximo; en este fixture los huesos son más altos que la malla.
- `cinematic_create_brief`: 3 payloads por llamada, máximo 8.578 bytes y 1.629 nodos AST; 31,0–90,5 ms por llamada en el
  LocalBridge del probe (la primera llamada es la de 90,5 ms; no se midió por qué; sin socket, no representa la latencia
  MCP real).
- Escena antes/después de todas las llamadas de planificación: huella fuente, frame, poses, Actions activas y conteo de
  datablocks **idénticos**. Control negativo: mover un objeto y cambiar de frame sí cambia `fingerprint`, `frame` y `pose`.

## Propuestas (no medidas)

- `lighting_intentions` derivado de `atmosphere.mood_preset` cuando el usuario no pidió iluminación.
- Que el máximo de las `dimensions` del objeto y sus mallas represente la altura/huella del actor (aproximación de caja).

## Límites

- El servidor no comprueba la semántica del texto (`description`, `mood`): sólo se guarda.
- `available_performances` depende de lo que `cinematic_register_actor` midió; si el registro no trae `root_motion`, las
  rutas no pueden usar ese clip (`ROOT_MOTION_NOT_MEASURED`).
- Las posiciones son las del frame actual de la escena, sin evaluar (no se hace `frame_set` en una tool de lectura).
