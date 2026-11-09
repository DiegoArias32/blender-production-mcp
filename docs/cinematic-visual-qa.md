# M10 — QA cinematográfica medida (área F)

Código: `src/blender_mcp/production/cinematic/analysis.py`. Tests: `tests/unit/test_m10_analysis.py` (sin Blender) y
`tests/blender/m10_analysis_probe.py` (Blender 5.2.1 real). Contrato general: `docs/cinematic-architecture.md`.
Continuidad entre planos: `docs/camera-continuity.md` §5 (fusionada allí; `docs/cinematic-qa-continuity.md` sólo remite).

Este documento separa tres cosas que no deben mezclarse:

- **Hechos medidos**: salen de Blender 5.2.1 en el probe, con su cifra y su origen.
- **Propuestas**: umbrales de partida para revisión. Están etiquetados `threshold_source: PROPOSAL` y se pueden
  sobrescribir con `options.thresholds` (entonces aparecen como `USER_REQUIREMENT` en `threshold_overrides`).
- **Observaciones creativas**: describen una medida (por ejemplo "sujeto centrado"). No son errores y nunca son una
  puntuación. No existe ningún "score" en ninguna respuesta (un test lo comprueba).

## 1. Tools (las seis son TRANSIENT)

| Tool | Función exportada (para el informe de calidad) | Qué mide |
|---|---|---|
| `cinematic_analyze_composition` | `composition_report(ctx, plan, state, options)` | encuadre, headroom, tercios, espacio negativo, solapes, dirección de pantalla, distancia, recorte por clip |
| `cinematic_validate_continuity` | `continuity_report(...)` | ver `docs/camera-continuity.md` §5 |
| `cinematic_analyze_visibility` | `visibility_report(...)` | ausencia, fuera de cuadro, clipping, oclusión, prop que tapa, cara oculta en evento, tamaño por tipo de plano |
| `cinematic_analyze_actor_collisions` | `collision_report(...)` | penetración actor-actor y actor-prop, intención frente a accidente, cobertura |
| `cinematic_analyze_contacts` | `contact_report(...)` | eventos de contacto/alcance/proximidad/mirada/soporte y contactos observados no planificados |
| `cinematic_analyze_motion` | `motion_report(...)` | cámara (velocidad, aceleración, jerk, saltos), raíces, huecos de Action, sincronía eventos-clips, cachés, luz |

Las seis hacen `frame_set` en su **primer** payload y restauran todo antes de devolver (lo exige
`tests/unit/test_m31_hints.py`; pasa filtrado a estas tools: 6/6). Nunca aceptan `apply=true`
(`CINEMATIC_ANALYSIS_READ_ONLY`) ni escriben eventos en el ledger: el informe de calidad decide qué guardar.

### Dos modos

- **PLAN** (`cinematic=<plan.json>`): analiza la escena candidata. Estados admitidos: `CINEMATIC_CANDIDATE`,
  `ASSEMBLED`, `REVIEWED`, `ACCEPTED`. Rechaza `uncertain_operation`, código nuevo (`CINEMATIC_BACKEND_CHANGED`) y
  candidata o fuente cambiadas fuera de las tools (`CINEMATIC_STALE_*`) **antes del primer `frame_set`**: las huellas
  se calculan dentro del mismo payload, antes de muestrear. Si el fichero abierto no es el del plan, el resultado se
  descarta (`CINEMATIC_STALE_ASSET`). Los planos son los congelados del plan; `options.shots` se rechaza.
- **SCENE** (sin referencia): mide una escena con nombre (`options.scene`, por defecto la del contexto) y actores
  explícitos (`object_name` u `options.actors`). Sirve para previs o para un control experimental. No hay puerta de
  frescura porque no hay plan con el que comparar; sigue restaurando y verificando la restauración.

### Contrato de proveedor (informe de calidad y refinamiento)

`cinematic_quality_report` (área G, `review.py`) llama a las seis funciones `*_report(ctx, plan, state, options=None)`
por nombre de parámetro y pasa su resultado por `review.normalize_section`. Todas devuelven las mismas claves:

| clave | significado |
|---|---|
| `passed` | `true` sólo si el estado se restauró, no hay hallazgo ERROR y (colisiones) ningún colisionador es `UNKNOWN` |
| `status` | `success` (pasa sin avisos), `warning` (hay avisos o hallazgos que impiden pasar), `error` (sólo si **no** se restauró el estado: el wrapper del servidor trata `error` como fallo de la operación) |
| `findings` | página de hallazgos (ERROR primero; `limit` ≤ 200) con `findings_pagination`, `findings_by_code` y `findings_by_severity` **sobre todos** los hallazgos |
| `failures` | sólo si `passed` es `false`: por qué, sobre todos los hallazgos (`CODIGO: n ERROR finding(s)`, `CINEMATIC_STATE_NOT_RESTORED`, `COLLISION_UNKNOWN_OPEN_MESH`); `normalize_section` lo usa cuando la página no trae filas ERROR, así un FAIL nunca sale sin motivo |
| `metrics` | escalares por análisis (el informe de calidad descarta los no escalares) |
| `state_restored` | restauración verificada en Blender (digest y huellas iguales) |

Los análisis no escriben en el ledger: el informe de calidad guarda su veredicto. En modo PLAN las opciones de la
sección (`section_options.<id>`) pasan por las mismas opciones cerradas que la tool.

## 2. El muestreador (un único `frame_set` por frame)

Orden dentro del payload (todo en `temp_override(scene=escena analizada)`):

1. Propiedad y huellas de la candidata (`cm_candidate`, `cm_fingerprints` frente a `state['fingerprints']`).
2. Plan de frames: rango del timeline o de las opciones, paso declarado, como máximo **600 frames**. Sin paso explícito,
   si el rango supera 600 el paso se calcula y se declara (`step_source: AUTO_CAPACITY`); `motion` exige paso 1 y
   rechaza ventanas mayores (`CINEMATIC_DENSE_WINDOW_REQUIRED` / `CINEMATIC_DENSE_SAMPLING_REQUIRED`).
3. Captura conjunta: canales de objeto y pose, shape keys y tiempos de strip (`cm_capture`), más datos de cámara
   (lens, shift, clip, sensor, foco, f-stop), datos de luz (energía, color, tamaño, spot), world (color y fuerza de
   `Background`), exposición y gamma, y `scene.camera`. Un digest `m2_digest` de todo ello antes y después.
4. Por frame: `scene.frame_set(f)`, depsgraph evaluado de la view layer de la candidata (`cm_depsgraph`: los objetos
   que sólo viven en la candidata tienen `matrix_world` obsoleto sin ella, medido), **cámara activa del frame** (los markers la cambian), valores
   **evaluados** (`evaluated_get`), proyección con `cm_camera_matrices` + `cm_project`. Un punto sólo cuenta como
   "en cuadro" con profundidad > 0 y `0 <= x, y <= 1`.
5. Restauración: un `frame_set` y todos los canales (`cm_restore`), luego datos de cámara/luz/world/exposición y
   `scene.camera`; después se compara el digest y las huellas (`state_restored`). Si difiere: `status: error` y
   `CINEMATIC_STATE_NOT_RESTORED`.

Puntos por actor: esquinas de las bound boxes **evaluadas** de sus mallas (cota superior de la silueta, no la silueta),
cabeza = punto medio del hueso de cabeza si se declara (si no, proxy `BOUNDS_TOP_PROXY`), raíz = hueso raíz declarado
o el origen del objeto. Los huesos nunca se adivinan por el nombre: salen de `options.actor_bones`, del registro
(`head_bone`, `root_bone` o `bones.head/root`) o no se usan. El valor `UNKNOWN` que el registro escribe para un rol no
resuelto se trata como hueso ausente (antes se habría buscado en Blender un hueso llamado `UNKNOWN` y el análisis habría
fallado con `CINEMATIC_ACTOR_BONE_MISSING`).

Compactación: cada respuesta lleva hallazgos paginados (`limit` ≤ 200, `offset`), recuentos por código, resúmenes por
plano y actor (mín/mediana/máx) y, sólo con `detail: samples`, filas de muestra paginadas (≤ 100).

## 3. Qué mide cada análisis

### Composición
Por plano y sujeto: fracción en cuadro y totalmente en cuadro, centro en pantalla (x, y), cabeza en pantalla,
headroom (1 − borde superior de la caja proyectada), altura y área en pantalla de la caja, distancia cámara-actor (m),
dirección de pantalla (velocidad proyectada del centro), espacio de avance (lead room), distancia a la intersección de
tercios más cercana, espacio negativo (1 − unión de cajas, rejilla 64×64) y separación horizontal entre sujetos.

- Hallazgos técnicos: `NO_ACTIVE_CAMERA` y `ACTIVE_CAMERA_NOT_CAMERA` (ERROR), `ACTOR_NOT_IN_SCENE` y
  `ACTOR_HIDDEN_IN_RENDER` (ERROR), `SUBJECT_BEHIND_CAMERA`, `SUBJECT_OUT_OF_FRAME`, `HEAD_OUT_OF_FRAME` (no en
  `EXTREME_CLOSE_UP`/`INSERT`), `NEAR_CLIP_CUT`, `FAR_CLIP_CUT` (WARNING, confianza `BOUNDS_CONSERVATIVE`).
- Observaciones creativas: `SUBJECT_CENTERED`, `NEAR_THIRDS_INTERSECTION`, `TIGHT_HEADROOM`,
  `SUBJECT_TOP_CUT_BY_FRAME`, `LOW_LEAD_ROOM`, `SMALL_FOR_SHOT_TYPE`, `SILHOUETTE_OVERLAP`, `LARGE_NEGATIVE_SPACE`.

### Visibilidad
Rayos `scene.ray_cast` desde la cámara activa a un subconjunto determinista de vértices evaluados de cada actor
(`ray_points`, 16 por defecto, máx. 64) y a la cabeza. Un rayo cuenta como visible si su primer impacto es el propio
actor o si llega al punto sin impactos. Los objetos ocultos en render (objeto o colección `hide_render`,
`visible_camera` desactivado) se **saltan a mano** y se cuentan en `hidden_in_render_skipped`.

- Hallazgos: `ACTOR_ABSENT`, `ACTOR_HIDDEN_IN_RENDER` (ERROR); `OUT_OF_FRAME`, `BEHIND_CAMERA`, `CAMERA_CLIPPING`,
  `EXCESSIVE_OCCLUSION` (con oclusor principal y su clase `ACTOR`/`PROP`/`ENVIRONMENT`/`OWNED_*`),
  `PROP_OCCLUDING_SUBJECT`, `FACE_HIDDEN_AT_EVENT`, `ACTOR_TOO_SMALL_FOR_SHOT_TYPE` (WARNING).
- Oclusión intencionada: `options.intended_occlusions [{actor_id, frame_start, frame_end, occluder?}]` convierte el
  hallazgo en `INTENDED_OCCLUSION` (INFO).
- Eventos faciales: `options.facial_events [{actor_id, frame, label?}]` y `state['facial_events']`; esos frames se
  añaden al muestreo.

### Colisiones entre actores y con props
Por frame: snapshot evaluado de cada malla (`physics.qa.pq_snapshot`, mundo y metros) y registro con el mismo contrato
que `pq_collider_records` (cerrado por `closed_triangles`, bbox, BVH `FromPolygons` en mundo) construido desde ese
mismo snapshot para no evaluar dos veces cada malla; penetración con `pq_collision_frame` (paridad de tres rayos y
profundidad por `find_nearest`) en los dos sentidos de cada par. Pares cuyas cajas están separadas más de
`proximity_margin_m` no se trazan. Props: `options.props` y objetos propios de la candidata con rol que contiene
"prop".

- Intención: sólo la dan eventos planificados con contacto entre ese par (ventana ± `contact_window_margin_frames`).
  Dentro: `INTENDED_CONTACT` (INFO) o `INTENDED_CONTACT_PENETRATION` (WARNING si supera `contact_penetration_m`).
  Fuera: `ACCIDENTAL_COLLISION` (ERROR si la profundidad supera `error_depth_m`, WARNING si supera
  `tolerance_depth_m`) o `ACCIDENTAL_CONTACT_WITHIN_TOLERANCE` (INFO).
- Mallas abiertas o no manifold: `COLLISION_UNKNOWN_OPEN_MESH`, confianza `UNKNOWN`; **nunca 0**, y bloquean
  `passed`.
- Cobertura declarada: frames muestreados, hueco máximo entre muestras, representación, y
  `continuous_collision_freedom_certified: false` siempre. Con paso > 1 la confianza baja a `SPARSE_SAMPLES`.
- Capacidad: vértices × frames ≤ `max_vertex_frames` (propuesta 3.000.000), comprobado en Blender antes de muestrear.
- Métricas (nivel superior de `metrics`, las lee el informe de calidad y el objetivo de refinamiento
  `max_penetration_m` de `cinematic_refine`, sección `staging`):
  - `max_penetration_m` (unidad `m`, `max_penetration_units`): máximo de la profundidad de penetración sobre todos los
    intervalos que penetran de todos los pares, accidentales e intencionados. `max_penetration_status`:
    `MEASURED`; `UNKNOWN_OPEN_MESH` → `None` (un colisionador abierto no mide profundidad, un máximo parcial lo
    subestimaría); `NO_PAIRS` → `None` (menos de dos entidades: nada puede colisionar). Pares cuyas cajas quedan
    separadas más de `proximity_margin_m` no pueden penetrar: cuentan 0,0.
  - `max_accidental_penetration_m` y `max_intended_penetration_m`: el mismo máximo separado por intención.
  - `accidental_intervals` (intervalos `ACCIDENTAL_COLLISION`) y `accidental_sampled_frames` (frames muestreados dentro
    de ellos; con paso > 1 son muestras, no frames). Antes había una `accidental_frames` que en realidad contaba
    intervalos: se sustituyó.
  - `pairs_measured`, `unknown`.

### Contactos
Eventos normalizados (ver §5). Por evento y frame de su ventana ± `window_margin_frames`: punto efector (cola, cabeza o
punto medio de un hueso explícito) frente al objetivo: hueso de otro actor (distancia punto-punto), malla de otro actor
o de un objeto (distancia a la superficie con BVH en mundo y dentro/fuera con `pq_collision_frame`), o superficie
estática (`contacts.runtime.contact_static_surfaces` + `contact_query`, distancia con signo). Mide: frames de contacto,
desfase de inicio y fin frente a la ventana planificada (frames y segundos), distancia en el frame planificado,
penetración, alcanzabilidad (distancia mínima si no hay contacto), deslizamiento relativo **en el espacio del objetivo**
(hueso u objeto; mundo para superficies estáticas), recorrido de la raíz y del efector durante el contacto (sólo
medida). Mirada (`GAZE`/`OBSERVE`/`TURN_TOWARD`): ángulo entre el eje local declarado del hueso de cabeza y la dirección
al objetivo. Proximidad (`APPROACH`/`RETREAT`): tendencia de la distancia entre raíces. Ataque (`PREPARE_ATTACK`):
estado de alcance con `reach_m`.

Además, un sondeo de todos los pares actor/prop marca los intervalos donde se tocan (holgura ≤ `contact_distance_m`) o
se interpenetran, y los empareja con eventos: los que no tienen evento dan `UNPLANNED_CONTACT`.

Hallazgos: `CONTACT_TIMING_OFF`, `CONTACT_PENETRATION`, `RELATIVE_SLIDING`, `TARGET_UNREACHED`, `UNPLANNED_CONTACT`,
`GAZE_NOT_ALIGNED`, `PROXIMITY_TREND_MISMATCH` (WARNING). Los eventos que no se pueden medir se devuelven en
`events_ignored` con su motivo (`EFFECTOR_REQUIRED`, `GAZE_FORWARD_AXIS_REQUIRED`, `KIND_UNSUPPORTED`...), nunca se
descartan en silencio.

### Movimiento
Muestreo denso (paso 1, ≤ 600 frames). Cámara: por cámara activa y frames consecutivos, paso de posición, velocidad,
aceleración y jerk, rotación por frame y cambio de lens. Un salto con la misma cámara en frames consecutivos no lo
explica un corte: `CAMERA_JUMP_UNEXPLAINED`, `CAMERA_ROTATION_JUMP`, `LENS_JUMP`. Actores: saltos de raíz
(`ROOT_JUMP`; en un corte `ROOT_JUMP_AT_CUT` INFO; en el borde de un hueco de Action se marca
`context: ACTION_GAP_BOUNDARY`) y discontinuidades de velocidad (`VELOCITY_DISCONTINUITY`). Cobertura de Actions:
frames que ninguna Action activa ni strip NLA sin silenciar cubre, respetando la extrapolación (`ACTION_GAP`), y eventos
que caen en esos frames (`EVENT_OUTSIDE_CLIP`). Cachés de física (modificadores `MESH_CACHE` en objetos propios):
huecos frente a la ventana declarada (`PHYSICS_CACHE_GAP`) o `PHYSICS_CACHE_COVERAGE_UNKNOWN` sin ventana. Luz:
cambio relativo de energía de cada luz, exposición y fuerza del world entre frames consecutivos (`LIGHTING_JUMP`,
`EXPOSURE_JUMP`, `WORLD_STRENGTH_JUMP`; en un corte, INFO). El timing de contactos lo mide `contacts`; el informe de
calidad combina ambos.

## 4. Umbrales (todos PROPUESTA)

| Análisis | Umbral | Valor propuesto |
|---|---|---|
| composición | `centered_tolerance`, `thirds_tolerance` | 0,05 / 0,05 (unidades de cuadro) |
| composición | `tight_headroom`, `min_lead_room`, `overlap_ratio`, `large_negative_space` | 0,02 / 0,2 / 0,5 / 0,9 |
| composición, continuidad | `direction_min_speed` | 0,002 cuadro/frame |
| composición, visibilidad | `min_screen_height` por tipo de plano | ESTABLISHING 0,03 · WIDE 0,15 · MEDIUM 0,4 · CLOSE_UP 0,7 · EXTREME_CLOSE_UP 0,9 · OVER_THE_SHOULDER 0,3 · LOW/HIGH_ANGLE 0,2 · TRACKING 0,15 · REACTION 0,4 · INSERT y CUSTOM sin umbral |
| visibilidad | `min_visible_fraction`, `min_occlusion_frames`, `prop_occlusion_fraction` | 0,5 / 2 muestras / 0,25 |
| colisiones | `tolerance_depth_m`, `error_depth_m`, `contact_penetration_m`, `proximity_margin_m` | 0,005 / 0,02 / 0,01 / 0,05 m |
| colisiones | `contact_window_margin_frames`, `max_vertex_frames` | 2 / 3.000.000 |
| contactos | `contact_distance_m`, `max_penetration_m`, `sliding_tolerance_m`, `trend_tolerance_m` | 0,02 / 0,005 / 0,01 / 0,05 m |
| contactos | `timing_tolerance_frames`, `window_margin_frames`, `gaze_tolerance_deg`, `query_distance_m` | 2 / 6 / 15° / 0,3 m |
| movimiento | `root_jump_m`, `camera_jump_m`, `jump_ratio` | 0,2 m / 0,5 m / 5× la mediana |
| movimiento | `camera_rotation_jump_deg`, `lens_jump_mm`, `min_accel_m_s2`, `accel_ratio` | 20° / 5 mm / 20 m/s² / 6× |
| movimiento | `light_jump_ratio`, `exposure_jump_ev` | 0,5 / 0,5 EV |
| continuidad | ver `docs/camera-continuity.md` §5.3 | |

## 5. Contratos de entrada

**Eventos de interacción** (una sola fuente, la primera que exista: `options.events`, `state['interactions']`,
`plan['interactions']` como lista o `{events: [...]}`):

```
{event_id?, kind ∈ INTERACTION_KINDS | SUPPORT | CONTACT, actor_id, frame | frame_start/frame_end,
 effector: {bone, end: TAIL|HEAD|MID} | "bone",
 target: {actor_id, bone?, end?} | {object} | {surfaces: [..], generic?} | {point: [m]} (sólo mirada) | "actor_id",
 contact?: bool (por defecto true en REACH, TOUCH_SURFACE, HOLD_PROP, HANDOFF_PROP, CONTACT, SUPPORT),
 head_bone? y forward_axis? (eje local del hueso, obligatorios para mirada), reach_m? (ataque)}
```

**Planos** (del plan, o `options.shots` en modo escena): `shot_id, frame_start, frame_end, shot_type, camera_id,
subjects | actors | subject, axis?`. Sin sujetos declarados = todos los actores. Sin planos en modo escena: segmentos
medidos de cámara a partir de los markers.

**Ventanas de caché**: `options.cache_windows {objeto: [inicio, fin]}` o filas `state['actors'][*]['secondary']` con
`object` y `window` o `frame_range`.

## 6. Hechos medidos (Blender 5.2.1 LTS, probe `tests/blender/m10_analysis_probe.py`)

Fixture inline: dos armatures con cajas cerradas (A: cuerpo 0,5×0,4×1,7 m y mano 6 cm emparentada al hueso `hand`;
B: 0,5×0,5×1,8 m), clips por NLA en la candidata, cámara A en (0,3; −6; 0,85) y cámara B en (0,3; 6; 0,85) animada,
marker de corte en 48, 24 fps, 1–96. Evidencia: `informe local privado; véase validation.md`
(**46/46 PASS, 0 errores**; corridas anteriores `bpm10-analysis-1` 44/44 y `-2` 45/45 con menos comprobaciones).

Re-ejecución de la integración: `informe local privado; véase validation.md` (**49/49 PASS,
0 errores**). La corrida `bpm10-analysis-4` falló antes de muestrear con `ACTOR_STALE`: el probe escribía registros de
actor a mano y `cinematic_create_candidate` ahora vuelve a medir cada actor; desde `-5` los actores se registran con
`cinematic_register_actor` (huesos `head` explícitos; `root` resuelto por raíz única de la jerarquía). Todas las cifras
de la tabla siguiente salieron idénticas en `-3` y `-5`; las filas marcadas *(-5)* sólo existen en la segunda.

| Hecho | Cifra medida |
|---|---|
| B apuntado por la cámara A al centro de su caja evaluada | centro en pantalla x = y = **0,5** (mín = mediana = máx, 6 decimales); distancia **6,0 m** |
| Headroom de B en S1 | **0,113527**; coincide con la proyección analítica de la cara cercana (profundidad 5,75 m, 50 mm, sensor 36 mm, 16:9) |
| Corte en 48 con cámaras a ambos lados de la línea A-B | lados `[-1, 1]`, distancia al eje 6,0 m en ambos → `AXIS_CROSSED`; con `allow_axis_break: [48]` → `AXIS_BREAK_ALLOWED` (INFO) |
| Muro `hide_render` delante de B (S2) | `scene.ray_cast` lo impacta: **1.246** rayos saltados a mano; B visible **1,0** (mín/mediana/máx) |
| Control con ese muro visible (modo escena) | B visible mediana **0,0**; `GhostWall` recibe el **91,9 %** de los rayos |
| Muro visible delante de A en S2 | `EXCESSIVE_OCCLUSION` por `Wall` (ENVIRONMENT), visible mínimo **0,25**; cara oculta en el evento de 70 |
| Mano de A 2 mm dentro de B | los 4 vértices interiores los tapa el propio B (`ActorB_Body`, clase ACTOR): coherente |
| Cuerpos interpenetrados | `ACCIDENTAL_COLLISION` ERROR en **33–51**, profundidad máx. **0,22 m** |
| Toque planificado (evento `touch`, 63–72) | `INTENDED_CONTACT` INFO en **64–72**, profundidad **0,002 m**, 4 vértices |
| Contacto del toque | primero 64, último 72, desfase **[+1, 0]** frames (ON_TIME), penetración del efector **0,002 m**, deslizamiento relativo **0,0 m** |
| Mirada de A hacia B con ejes locales opuestos del hueso de cabeza | **0,0°** con +X local y **180,000005°** con −X: el +X local de ese hueso vertical es el +X del mundo |
| Teletransporte de B | `ROOT_JUMP` en 84→85, **1,0 m** |
| Salto de la cámara B | `CAMERA_JUMP_UNEXPLAINED` en 89→90, **0,8 m** (19,2 m/s); el corte de 48 no cuenta como salto |
| Energía de luz 1000 → 5000 | `LIGHTING_JUMP` en 69→70 |
| Strip de A desde 5 con `HOLD_FORWARD` | `ACTION_GAP` **[1, 4]** |
| **En ese hueco A no conserva su último valor**: los canales animados por NLA vuelven a su valor por defecto | A en el origen (location 0): colisiona con B (**0,11 m**, frames 1–4) y salta **2,0 m** en 4→5 (`context: ACTION_GAP_BOUNDARY`) |
| Restauración | 9 análisis: estado exacto antes = después (frames, `scene.camera`, loc/rot/escala de todos los objetos, todos los huesos, lens/clip, energía de luz) y huellas iguales; el plan sigue fresco al final |
| Candidata editada fuera (+0,1 m en una cámara propia) | `CINEMATIC_STALE_CANDIDATE` antes de muestrear; frame sin cambiar |
| `metrics.max_penetration_m` de colisiones *(-5)* | **0,22 m** (`MEASURED`): intervalos accidentales 2 (1–4 a 0,11 m y 33–51 a 0,22 m), 23 frames muestreados accidentales; intencionado (toque) **0,002 m** |
| Proveedor `collision_report` → `review.normalize_section` *(-5)* | sección `staging` **FAIL** con `max_penetration_m` 0,22 y las filas `ACCIDENTAL_COLLISION` como errores; estado restaurado |
| Objetivo de refinamiento `max_penetration_m` vía `review.measure_target` *(-5)* | valor **0,22 m**, fuente `PROVIDER analysis.collision_report` (sin eventos guardados el toque cuenta como accidental; el máximo no cambia) |
| `service.dispatch('cinematic_analyze_motion')` *(-5)* | llega al handler con todos los subsistemas importados (en `-3` también) |
| Tamaño de payload | *(-5)* máximo **55.243 B** por llamada; *(-3)* máximo **54.005 B** por llamada en el probe; peor caso sintético (8 actores, 64 eventos, 600 frames, `build_code` del código actual, con `cm_depsgraph`): contacts **80.045 B / 11.290 nodos AST**, resto ≤ 68.842 B / 9.143 nodos (antes de la integración 78.811 / 11.194 y ≤ 67.979 / 9.051; límite propio 140.000 / 14.000) |
| Tiempo | *(-3)* 0,12–0,76 s por análisis y ≤ 0,092 s de ejecución por payload; *(-5)* 0,17–1,13 s por análisis y ≤ 0,107 s por payload, **en esta fixture trivial** (96 frames, cajas de 8 vértices). No es representativo de rigs reales; no usarlo como presupuesto. |

## 7. Límites declarados

- Las cajas evaluadas son una cota superior de la silueta: área, solape y recorte por clip son conservadores.
- Los rayos van a vértices muestreados: un oclusor fino entre muestras puede escaparse. Los materiales transparentes
  no se modelan (toda superficie bloquea).
- Colisión por vértices: no detecta cruces arista-cara; frames finitos no certifican ausencia continua de colisión.
- Contacto y mirada sólo con efectores, objetivos y ejes explícitos. La confianza es evidencia, no probabilidad.
- La longitud de un PC2 no la expone RNA: sin ventana declarada la cobertura de caché es `UNKNOWN`.
- Hallazgos técnicos ≠ calidad artística: todas las respuestas llevan `visual_verification: required`.

## 8. Pendiente (no implementado, sin decidir)

- El registro de actores ya guarda `bones` {root, head, hand_left, ...} resueltos (explícitos, semántica M3 ≥ 0,9 o
  raíz única de la jerarquía); este módulo lee `bones.head` y `bones.root`. Las manos para los efectores siguen
  llegando por los eventos (`effector.bone`), no del registro.
- No hay contrato de almacenamiento de eventos de interacción: se leen de `state['interactions']`/`plan['interactions']`
  con el esquema de §5 (petición al área de interacción/sincronía).
- `state['facial_events']` como fuente de eventos faciales importantes (petición al área facial).
