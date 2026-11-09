# M10 — Cortes de cámara y continuidad

Este documento tiene dos dueños: la parte de **cortes y cámaras** (secciones 1–4) es del área C
(`cinematic_create_cuts`, `src/blender_mcp/production/cinematic/cameras.py`). La **QA de continuidad**
(`cinematic_validate_continuity`, sección 5) es del área F (`src/blender_mcp/production/cinematic/analysis.py`); antes
vivía aparte en `docs/cinematic-qa-continuity.md`, que ahora sólo remite aquí.

## 1. Cortes nativos con marcadores de cámara

Un corte es un `timeline_marker` llamado `CUT_<shot_id>` con `marker.camera` = cámara propia, en la **escena candidata**
(la escena fuente no se toca). No se usa VSE.

Hechos medidos (Blender 5.2.1):

- `scene.frame_set(f)` pone `scene.camera` = cámara del último marcador con cámara y `frame ≤ f`; antes del primero, la del
  primero. Asignar `scene.frame_current` **no** cambia la cámara (digest de API M10). El render obedece a los marcadores.
- Si se borra el objeto cámara de un marcador, `marker.camera` pasa a `None` y ese marcador **se ignora** al cambiar de
  cámara: en el frame 12 quedó la cámara del marcador anterior (`api_measure.json`, `camera_12_after_delete`).
- Los marcadores narrativos sin cámara (`BEAT_`, `EVENT_`) no alteran la cámara activa: en el probe, `EVENT_turn` en f46
  dejó la cámara del plano S2.
- Blender admite dos marcadores en el mismo frame (`dup_frame_markers`): por eso el solape se valida a mano.
- Probe (`bpm10-cameras-3/m10_cameras.json`): cortes S1@1→A, S2@31→B, S3@61→C; `frame_set` en
  1/15/30/31/45/60/61/75/90 dio exactamente A,A,A,B,B,B,C,C,C, tanto en el payload como medido por el probe aparte.

## 2. `cinematic_create_cuts`

Opciones: `cuts: [{shot_id, camera_id, frame}]` **o** `from_shots: true` (usa `shot_id`, `camera_id`, `frame_start` de
los planos), `min_shot_frames` (por defecto 6, **PROPUESTA**), `clear: true` (quita todos los cortes registrados).

Validación en el host (antes de tocar Blender), en el orden dado:

| código | causa |
|---|---|
| `CUTS_EMPTY`, `CUT_INVALID` | lista vacía o fila mal formada |
| `CAMERA_NOT_OWNED` | `camera_id` no creado por esta cinemática |
| `CUT_OUTSIDE_RANGE` | frame fuera de `[frame_start, frame_end]` de la candidata |
| `CUT_ORDER_INVALID` | frames no crecientes |
| `CUT_OVERLAP` | dos cortes en el mismo frame |
| `CUT_FIRST_NOT_AT_START` | el primer corte no está en `frame_start` |
| `CUT_SHOT_DUPLICATE` | `shot_id` repetido |
| `CUT_TOO_SHORT` | plano más corto que `min_shot_frames` (umbral PROPUESTO, se puede fijar explícitamente) |

Avisos: `CUT_SAME_CAMERA` (dos cortes seguidos a la misma cámara), `CUT_SHOT_MISMATCH` (el corte no coincide con el rango
del plano planificado).

En Blender: comprueba otra vez propiedad y rango, quita sólo los marcadores `CUT_*` registrados, crea los nuevos y
**mide** con `frame_set` (con captura/restauración) que `scene.camera` es la del corte en el primer y el último frame de
cada plano. Cada fallo → `CUT_CAMERA_NOT_ACTIVE`. Luego audita los marcadores (sección 3). Estado:
`state['cuts'] = [{shot_id, camera_id, camera_object, frame, frame_end, frames, marker}]`.

## 3. Cortes rotos (auditoría)

`audit_cut_markers` se ejecuta tras `create_cuts` y en `timeline_report` sobre los marcadores leídos de Blender:

| código | detecta |
|---|---|
| `CUT_CAMERA_MISSING` | marcador sin cámara (cámara borrada) o cámara registrada que ya no existe |
| `CUT_CAMERA_NOT_OWNED` | marcador que apunta a una cámara ajena |
| `CUT_OUTSIDE_RANGE` | marcador fuera del rango de la escena |
| `CUT_OVERLAP` | dos cortes en el mismo frame |
| `CUT_FIRST_NOT_AT_START` | el primer corte no está en `frame_start` |
| `CUT_MARKER_MISSING` / `CUT_MARKER_CHANGED` | marcador registrado que falta o cambió de frame/cámara |
| `CUT_MARKER_UNRECORDED` (aviso) | marcador `CUT_*` no creado por la tool |
| `CUT_TOO_SHORT` (aviso) | plano más corto que el umbral propuesto |
| `CUTS_DO_NOT_COVER_TIMELINE` | los cortes registrados no cubren todo el rango |

Medido: borrar desde fuera la cámara B produjo `CUT_CAMERA_MISSING` + `CUT_MARKER_CHANGED`, y la siguiente mutación se
rechazó con `CINEMATIC_STALE_CANDIDATE` (la huella de la candidata detecta la edición externa).

## 4. Límites

- Un corte es instantáneo: no hay fundidos ni transiciones (`model.TRANSITIONS` sólo se planifica; no se renderiza aquí).
- La regla de 180°, saltos de eje y eyeline entre planos **no** se comprueban en esta área: son QA de continuidad (F).
- El umbral de 6 frames es una propuesta, no una regla cinematográfica medida.

## 5. QA de continuidad entre planos (área F)

`cinematic_validate_continuity` (TRANSIENT) y su proveedor para el informe de calidad
`continuity_report(ctx, plan, state, options)` en `src/blender_mcp/production/cinematic/analysis.py`. Muestreador,
restauración, contrato de proveedor y convenciones comunes: `docs/cinematic-visual-qa.md`. Los umbrales son
**PROPUESTAS** (`threshold_source: PROPOSAL`); las reglas de continuidad (180°, dirección de pantalla, eyeline) son
convenciones de oficio: se informan con categoría `CONTINUITY_RULE` y severidad WARNING, nunca como error técnico, y un
cambio de eje no siempre es un fallo.

Entradas que usa de las secciones 1–4: los planos congelados del plan (`shot_id`, rango, `camera_id`), las cámaras
propias `state['cameras'][id]['object']` y todos los markers **con cámara** leídos de la escena candidata (los `CUT_*`
de §2 y cualquier otro; con `frame_set`, que es lo que de verdad cambia la cámara). No lee `state['cuts']`: compara el plan con lo que mide en Blender.

### 5.1 Qué se muestrea

Sólo lo necesario alrededor de cada corte, en la escena candidata y con la cámara activa de cada frame (los markers la
cambian en `frame_set`, medido en §1): para cada plano, sus frames inicial, inicial+1, final−2, final−1 y final; para
cada marker con cámara, m−2, m−1, m y m+1; y el inicio y fin del timeline. Con `options.step` se muestrea además el
rango completo. Rayos de visibilidad sólo en el primer frame de cada plano y en los frames de marker.

### 5.2 Reglas medidas

| Comprobación | Medida | Resultado |
|---|---|---|
| Huecos y solapes del plan | rangos de los planos ordenados | `TIMELINE_GAP`, `SHOT_OVERLAP` (ERROR); `TIMELINE_START_UNCOVERED`, `TIMELINE_END_UNCOVERED` (WARNING) |
| Cortes planificados frente a markers | inicio de cada plano frente a markers con cámara | `PLANNED_CUT_WITHOUT_MARKER`, `CUT_NOT_IN_PLAN` (WARNING); dos planos seguidos con la misma cámara no necesitan marker |
| Cámara de cada plano | `camera_id` del plano → `state['cameras'][id]['object']` frente a la cámara activa medida | `SHOT_CAMERA_MISMATCH` (ERROR) |
| Frames sin cámara | `scene.camera` es None tras `frame_set` | `NO_ACTIVE_CAMERA` (ERROR) |
| Eje de acción (regla de 180°) | línea entre las raíces de los dos primeros sujetos comunes a ambos planos (o `axis` del plano, u `options.axis` = `{actor_a, actor_b}` / `{actor, target_point}`), en el plano del suelo (XY del mundo), con la posición de cada cámara en el último frame antes y el primero después del corte | lados distintos → `AXIS_CROSSED`; cámara a menos de `axis_degenerate_m` de la línea → `ON_AXIS` (sin juicio) |
| Ruptura de eje permitida | `options.allow_axis_break`: `true` (todos los cortes) o lista de frames de corte, `shot_id` del plano entrante o `"S1>S2"` | `AXIS_BREAK_ALLOWED` (INFO); también relaja la inversión de dirección y de eyeline de ese corte |
| Dirección de pantalla | velocidad proyectada del centro de cada sujeto común en los dos últimos frames del plano saliente y los dos primeros del entrante | inversión izquierda/derecha → `SCREEN_DIRECTION_FLIP` |
| Salto de posición en pantalla | distancia en pantalla del centro de cada sujeto común a través del corte | observación creativa `SCREEN_POSITION_JUMP` si supera `screen_jump` |
| Riesgo de jump cut | cambio de rumbo horizontal de la cámara vista desde el sujeto y cambio relativo de su altura en pantalla | observación creativa `JUMP_CUT_RISK` si el ángulo < `jump_cut_min_angle_deg` y el tamaño cambia < `jump_cut_size_ratio` |
| Sujeto visible al empezar cada plano | primer frame del plano: detrás de cámara, fuera de cuadro o todos los rayos ocultos | `SUBJECT_NOT_VISIBLE_AT_SHOT_START` (WARNING) con el motivo |
| Mirada / eyeline (si hay objetivo) | `options.gaze [{actor_id, head_bone?, forward_axis, target_actor_id / target_object / target_point}]`: ángulo 3D al objetivo y dirección horizontal proyectada de la mirada a ambos lados del corte | inversión → `EYELINE_DIRECTION_FLIP` (o `_ALLOWED`) |
| Planos muy cortos | longitud en frames | observación `VERY_SHORT_SHOT` si < `min_shot_frames` |

Sin planos (modo escena) los segmentos salen de los markers con cámara (`shots_source: MEASURED_CAMERA_SEGMENTS`).
Cortes rotos de los markers (cámara borrada, ajena, solapes) los audita `audit_cut_markers` (§3); la continuidad sólo
mide lo que esos markers producen.

### 5.3 Umbrales propuestos

`jump_cut_min_angle_deg` 30°, `jump_cut_size_ratio` 0,2, `screen_jump` 0,4 (unidades de cuadro), `direction_min_speed`
0,002 cuadro/frame, `min_shot_frames` 6, `axis_degenerate_m` 0,05 m. Todos sobrescribibles con `options.thresholds`.
`min_shot_frames` coincide con el umbral propuesto de `CUT_TOO_SHORT` de §2; ninguno de los dos está calibrado.

### 5.4 Hechos medidos (Blender 5.2.1, `tests/blender/m10_analysis_probe.py`)

Evidencia: `informe local privado; véase validation.md` (primera redacción, 46/46) y
`bpm10-analysis-5/m10_analysis.json` (re-ejecución de la integración, 49/49, actores registrados con
`cinematic_register_actor`): las cifras de abajo salieron idénticas en ambas.

- Dos actores sobre el eje X del mundo; cámara A en y = −6 (plano S1, 1–47) y cámara B en y = +6 (S2, 48–96), marker
  en 48. Medido: lados de cámara **[−1, +1]**, distancia de ambas cámaras al eje **6,0 m** → `AXIS_CROSSED` en
  **[47, 48]**. Con `allow_axis_break: [48]` → `AXIS_BREAK_ALLOWED` (INFO) y sin `AXIS_CROSSED`.
- Cortes del plan y markers coinciden y la cámara medida de cada plano es la planificada: ningún `TIMELINE_GAP`,
  `SHOT_OVERLAP`, `SHOT_CAMERA_MISMATCH`, `CUT_NOT_IN_PLAN`, `PLANNED_CUT_WITHOUT_MARKER` ni `NO_ACTIVE_CAMERA`.
- Salto en pantalla de A a través del corte **0,159933** (bajo el umbral propuesto 0,4); B **0,0**.
- Tras cada validación: frames, `scene.camera`, canales, poses y huellas idénticos a los previos.

### 5.5 Límites

- El lado de cámara se mide en el plano del suelo (XY del mundo): planos cenitales puros dan `ON_AXIS`.
- La dirección de pantalla necesita dos muestras a cada lado del corte y un sujeto que se mueva más que
  `direction_min_speed`.
- La mirada exige hueso de cabeza y eje local explícitos: el eje de un hueso no se infiere de su nombre.
- No usa `state['cameras'][id]['animation']['ranges']`: el movimiento de cámara dentro de un plano lo mide
  `cinematic_analyze_motion` (saltos, aceleración, jerk), no la continuidad entre planos.
