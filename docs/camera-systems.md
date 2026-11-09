# M10 — Sistemas de cámara (área C)

Código: `src/blender_mcp/production/cinematic/cameras.py`. Tests: `tests/unit/test_m10_cameras.py` (sin Blender) y
`tests/blender/m10_cameras_probe.py` (Blender 5.2.1 real, fixture propia). Contrato general:
[cinematic-architecture.md](cinematic-architecture.md). Cortes y línea de tiempo: [cinematic-timeline.md](cinematic-timeline.md)
y [camera-continuity.md](camera-continuity.md).

## 1. Tools

| Tool | `apply=false` | `apply=true` |
|---|---|---|
| `cinematic_create_camera` | propuesta (ajustes normalizados, procedencia) | objeto + datos de cámara propios en `_CIN` |
| `cinematic_configure_camera` | antes / cambio | ajustes, DOF y constraint de seguimiento propia |
| `cinematic_animate_camera` | movimiento, rango, nº de keys | Action propia (slots OBJECT + CAMERA), keys LINEAR por frame |
| `cinematic_plan_focus` | plan de foco, ventanas de pull, umbrales | `dof.focus_object` o `dof.focus_distance` animado + validación medida |

Las cuatro son *writers* (no están en `READ_ONLY` ni en `TRANSIENT`). Una propuesta nunca llama al bridge (test
`test_proposals_do_not_touch_blender`). Toda referencia a cámara es `options.camera_id` (o `object_name` de una cámara
propia): una cámara que no creó esta cinemática se rechaza con `CAMERA_NOT_OWNED`, en el host y otra vez en el payload.

Nombres propios (deterministas, `model.owned_name`): objeto `M10C_<g8>_v<n>_CAM_<id>`, datos `..._CAMD_<id>`, Action
`..._CAMACT_<id>`, constraint `..._TRACK`. `camera_id` largo → `CINEMATIC_NAME_TOO_LONG` (límite de 63 bytes de Blender).

## 2. Disciplina de cada payload

1. **Validar** (propiedad, colisiones de nombre, rango RNA, solapes de keys, targets).
2. **Muestrear** con `cm_capture` → un `frame_set` por frame dentro de `temp_override(scene=candidata)` → `cm_restore`.
   La captura incluye los actores del plan **y todos los objetos de `_CIN`** (ver hecho medido 3.6).
3. **Mutar**.
4. **Medir** de nuevo (evaluado) y devolver resumen compacto + `fingerprints`.

Un rechazo ocurre siempre antes de mutar: la candidata queda utilizable (comprobado en el probe: tras el rechazo directo
del payload por `CAMERA_NOT_OWNED`, el siguiente paso pasa `store.fresh`).

Longitudes de las opciones en **metros**; el payload convierte con `scene.unit_settings.scale_length`. Ángulos en grados.

## 3. Hechos medidos (Blender 5.2.1, background, `--factory-startup`)

Fuente: `informe local privado; véase validation.md` (API) y
`informe local privado; véase validation.md` (probe, 40/40 PASS).

1. **Rangos RNA de `bpy.types.Camera`**: `lens` hard_min 1,0 / soft_max 5000; `sensor_width` y `sensor_height` hard_min 1 /
   soft_max 100; `shift_x/y` soft ±2 (hard ilimitado); `clip_start/end` hard_min 1e-6 (sin máximo); `ortho_scale` hard_min 0 /
   soft 0,001–10000. `CameraDOFSettings`: `focus_distance` 0–5000 (soft), `aperture_fstop` soft 0,1–128, `aperture_blades`
   0–16 (hard), `aperture_rotation` ±π, `aperture_ratio` 0,01–2 (soft). Enums: `type` {PERSP, ORTHO, PANO, CUSTOM},
   `sensor_fit` {AUTO, HORIZONTAL, VERTICAL}, `lens_unit` {MILLIMETERS, FOV}.
2. **Valores por defecto** de una cámara nueva: lens 50, sensor 36×24, AUTO, clip 0,1/1000, ortho 6, f/2,8, blades 0,
   focus 10. Son los `INFERRED_DEFAULT` de `create_camera`.
3. `DAMPED_TRACK` nace con `track_axis='TRACK_Y'`; `TRACK_TO` con `TRACK_NEGATIVE_Z`/`UP_Y`. El código fija siempre
   `TRACK_NEGATIVE_Z`. Error de puntería medido con DAMPED_TRACK sobre la cabeza en movimiento (f1/45/90): 0,0 rad.
4. Una Action con dos slots anima objeto y datos a la vez; `keyframe_points.add` + `foreach_set('co')` + `fc.update()`
   ordena las keys (medido: añadidas fuera de orden, quedan 1,10,20,30,40). `fc.evaluate(f)` devuelve el valor LINEAR esperado.
5. Tras `frame_set` en la candidata, `obj.evaluated_get(dg).data.dof.focus_distance` vale el valor animado (7,0) mientras
   el datablock original sigue en 10,0: el foco se lee **siempre evaluado**.
6. **Flush a los canales originales**: un bucle de 30 `frame_set` en la escena candidata (que no es la de la ventana)
   **cambió los canales originales** del hueso `root` del actor y de las tres cámaras animadas (probe,
   `probe_frame_set_flushed_original_channels`). Un único `frame_set` de la medición de API no lo hizo. Consecuencia: todo
   muestreo debe restaurar también las cámaras propias; el probe lo hace con `cm_capture/cm_restore` y sin ello la
   siguiente mutación falló con `CINEMATIC_STALE_CANDIDATE` (primera ejecución del probe, fallo de mi método de medida).
7. Resultados del probe:
   - puntería de `look_at` calculada (rotación, sin constraint): error 0,0 rad;
   - DOLLY de 2,0 m con EASE_IN_OUT hacia la cabeza: longitud de trayectoria medida sobre las keys 2,0 m; picos
     3,10 m/s (f16), 11,96 m/s² (f3), 26,6 m/s³ (f4);
   - ORBIT de 90° (LINEAR) alrededor de la raíz en movimiento: radio 6,0085 m, error máximo 8e-7 m; velocidad angular
     93,10 °/s = 90° / (29/30 s);
   - TRACKING: offset fijo (3,0; 1,0; −1,77) m respecto a la raíz evaluada; error máximo 1,2e-7 m (payload) y 3,6e-7 m
     (medido por el probe de forma independiente);
   - HANDHELD (semilla 7, amplitud 0,02 m): desviación máxima por canal 0,0174 m ≤ amplitud;
   - pull de foco: f61 = profundidad de la cabeza (4,2096 m, error 2,4e-7 m frente a la key), f90 = 3,0 m.

## 4. `cinematic_create_camera`

Opciones: `camera_id` (obligatorio), `shot_id?`, `type` (PERSP/ORTHO), `lens_mm`, `lens_unit`, `sensor_width_mm`,
`sensor_height_mm`, `sensor_fit`, `shift_x/y`, `clip_start`, `clip_end` (m), `ortho_scale`, `location` (m, **obligatoria**:
la colocación nunca se infiere), exactamente uno de `rotation_euler_deg` o `look_at`, `track` (NONE/TRACK_TO/DAMPED_TRACK),
`reference_frame?`, `set_scene_camera?` (por defecto fija `scene.camera` de la candidata sólo si estaba vacía).

`look_at`: `{actor_id, bone?}` | `{object, bone?}` | `{point}`. Un actor sin `bone` usa su **raíz**: el único hueso sin
padre de un armature (`CAMERA_TARGET_ROOT_AMBIGUOUS` si hay varios), o el origen del objeto. Con `look_at` la rotación se
calcula con el target **evaluado** en `reference_frame` (o el inicio del plano, o el inicio de la línea de tiempo). Con
`track` además se crea la constraint en la propia cámara (sólo referencia al actor; el actor no se modifica).

Rechazos: `CAMERA_EXISTS`, `CAMERA_PLACEMENT_REQUIRED`, `CAMERA_ORIENTATION_REQUIRED`, `CAMERA_TRACK_TARGET_OBJECT_REQUIRED`,
`CINEMATIC_SHOT_UNKNOWN`, `CINEMATIC_RANGE_INVALID`/`CINEMATIC_CHOICE_INVALID`/`CINEMATIC_NUMBER_INVALID`,
`CAMERA_CLIP_INVALID`, `CAMERA_TARGET_MISSING`, `CAMERA_TARGET_BONE_MISSING`, `CAMERA_LOOK_AT_DEGENERATE`,
`CINEMATIC_NAME_COLLISION`, `CINEMATIC_CAPACITY` (32 cámaras).

**Política (propuesta):** se aceptan valores entre el `hard_min` RNA y el `soft_max` RNA (rango de la UI). Fuera de ese
rango se rechaza como error de entrada aunque Blender lo admitiría. El payload repite la comprobación contra la RNA
instalada (`CAMERA_VALUE_OUT_OF_RANGE`) para detectar cambios de API.

## 5. `cinematic_configure_camera`

Ajustes de la cámara propia + `dof {use_dof, focus_object (nombre | {actor_id,bone} | null), focus_subtarget,
focus_distance_m, aperture_fstop, aperture_blades, aperture_rotation_deg, aperture_ratio}` + `tracking {mode, target,
influence}` (sustituye la constraint propia `..._TRACK`). `clip_start < clip_end` se valida contra los valores vigentes.
Una propiedad ya animada por la Action propia (`lens`, `dof.focus_distance`...) se rechaza con `CAMERA_PROPERTY_ANIMATED`:
un valor estático quedaría tapado por la animación.

## 6. `cinematic_animate_camera`

Opciones: `camera_id`, rango (`shot_id` > `frame_start/frame_end` > plano de la cámara > línea de tiempo), `move`
(`model.CAMERA_MOVES`), `params`, `easing` (`model.EASINGS`), `target`, `replace`.

| move | params | trayectoria (s = easing(t)) |
|---|---|---|
| STATIC | — | pose base; mira al target si lo hay |
| DOLLY | `distance_m` ≠ 0 | a lo largo del eje de vista (o hacia el target en el primer frame) |
| TRUCK | `distance_m` ≠ 0 | eje X local de la pose base |
| CRANE | `height_m` ≠ 0 | eje Z del mundo |
| ORBIT | `angle_deg` ≠ 0, `axis` (Z) | gira el offset inicial alrededor del target **muestreado por frame**; mira al pivote. Target obligatorio |
| PAN | `angle_deg` | guiñada alrededor de Z del mundo, en sitio. Sin target |
| TILT | `angle_deg` | cabeceo alrededor del X local, en sitio. Sin target |
| TRACKING | `offset_m?` | target por frame + offset fijo (por defecto el del primer frame). Target actor/objeto obligatorio |
| HANDHELD | `seed`, `amplitude_m`, `amplitude_deg`, `frequency_hz` | pose base + ruido determinista acotado |

Comunes: `look_at_target` (por defecto true si hay target), `lens_end_mm` (zoom en el slot CAMERA), `start_location`,
`start_rotation_deg` (por defecto la colocación guardada de la cámara).

Easing declarado: LINEAR s=t; EASE_IN s=t²; EASE_OUT s=1−(1−t)²; EASE_IN_OUT s=t²(3−2t).

Escritura: Action propia `..._CAMACT_<id>` (creada la primera vez; las siguientes llamadas añaden keys en rangos que no se
solapen; solape sin `replace` → `CAMERA_ANIMATION_OVERLAP`), slots OBJECT y CAMERA asignados a objeto y datos, una key
LINEAR por frame **sólo** en el rango. Fuera del rango las F-Curves extrapolan constante (se mantiene la primera/última key).

Resultado: `trajectory.keys` (medido evaluando las F-Curves escritas) y `trajectory.evaluated` (medido con `frame_set`,
incluye constraints): velocidad, aceleración y jerk (m/s, m/s², m/s³) y sus equivalentes angulares (°/s, °/s², °/s³, a
partir de vectores de velocidad angular en mundo), con el frame del pico, longitud de trayectoria y giro acumulado.
`verification`: target delante de la cámara, distancia mín./máx., error del offset (TRACKING) y del radio (ORBIT).
No hay umbrales de "buena trayectoria": son medidas.

**HANDHELD** (aviso `HANDHELD_IS_PROCEDURAL_NOISE` siempre): por canal tres senos con fases de un LCG sembrado y pesos
0,5/0,3/0,2 (suman 1, así que |desviación| ≤ amplitud por construcción). Límites **propuestos**: amplitud ≤ 0,05 m y ≤ 3°,
frecuencia ≤ 8 Hz; valores por defecto **propuestos** 0,01 m, 0,5°, 1 Hz. No sustituye a una cámara operada.

Aviso `CAMERA_ROTATION_FROM_CONSTRAINT`: si la cámara tiene TRACK_TO/DAMPED_TRACK, la rotación keyeada queda tapada;
`trajectory.evaluated` refleja la rotación real.

## 7. `cinematic_plan_focus`

Opciones: `camera_id`, rango (como en animate), `focus: [{frame, target: {actor_id,bone?}|{object,bone?}|{distance_m},
pull_frames?, subject?}]`, `pulls {frames, easing}`, `aperture_fstop?`, `thresholds?`, `replace`.

- Modo **OBJECT**: una sola fila con target objeto/actor → `dof.focus_object` + `focus_subtarget` (sigue al target sin keys).
- Modo **KEYED**: cualquier otro caso → `dof.focus_object = None` y `dof.focus_distance` con una key LINEAR por frame en el
  slot CAMERA de la Action propia. Un pull mezcla la profundidad del target anterior y la del nuevo durante
  `[frame, frame + pull_frames − 1]` con el easing.
- Profundidad = `(target − cámara) · eje de vista` (la distancia que usa Blender con `focus_object`, según su código
  fuente; no lo he medido con render).
- Antes de mutar: target inexistente → `CAMERA_TARGET_MISSING`; target activo detrás de la cámara →
  `FOCUS_TARGET_BEHIND_CAMERA`; foco planificado fuera de `[clip_start, clip_end]` → `FOCUS_OUTSIDE_CLIP`.
- Después (medido, evaluado por frame): círculo de confusión de lente delgada
  `CoC_mm = f² / (N (s − f)) · |d − s| / d` (f focal, N número f, s foco, d profundidad del sujeto).
  - `FOCUS_LOST` (error): fuera de las ventanas de pull, CoC del sujeto > `in_focus_coc_mm`. Sujeto = `subject` de la fila o
    su target; las filas sólo de distancia sin `subject` se listan como `unverified_frames`.
  - `FOCUS_JUMP` (aviso): cambio relativo de foco por frame > `jump_ratio` fuera de los pulls.
  - `DEFOCUS_EXCESSIVE` (aviso, posible decisión artística): CoC de la raíz de un actor > `excessive_coc_ratio` × ancho de
    sensor.
  - `FOCUS_KEYS_MISMATCH` (error): foco evaluado ≠ planificado más de 1 mm.

**Umbrales (todos PROPUESTA, con `threshold_source` en la respuesta):** `in_focus_coc_mm` = diagonal del sensor / 1500
(convención; 0,0288 mm en 36×24), `excessive_coc_ratio` 0,01, `jump_ratio` 0,25, tolerancia de keys 1 mm, pull por
defecto 8 frames EASE_IN_OUT.

Ejemplo medido (probe): pull de la cabeza (f61) a 3,0 m fijos (f76, 8 frames) con la cabeza a ~4,2 m → `FOCUS_LOST` en
f84–f90 (CoC máx. 0,087 mm en f84), nunca dentro de la ventana del pull. Es el caso "foco perdido mientras la cámara
sigue al actor".

## 8. Límites declarados

- PANO/CUSTOM no soportados (sin QA de render para ellos).
- La raíz por defecto de un actor es el hueso sin padre; para foco conviene pasar el hueso de la cabeza.
- `DEFOCUS_EXCESSIVE` mide sólo la raíz de cada actor, no toda su silueta.
- Con `sensor_fit` AUTO/VERTICAL el ancho usado para el ratio de desenfoque es `sensor_width` (aproximación declarada).
- Las keys por frame no se simplifican; la animación entre rangos de distintos planos interpola lineal entre la última key
  de uno y la primera del siguiente (invisible si hay corte, pero existe).
- No se mide la calidad de un movimiento: las cifras de trayectoria son hallazgos técnicos; la revisión visual es aparte.
