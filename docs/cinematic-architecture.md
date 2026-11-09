# M10 — arquitectura cinematográfica (contrato de implementación)

Dominio `src/blender_mcp/production/cinematic/`. Reutiliza M1–M9 sin modificar `facial/` ni el store genérico. Fuera
de `cinematic/` sólo cambian el registro de tools en `server.py`, el modo paginado de grafos en `materials/` (soporte
de piel) y la corrección de paridad M8 en `physics/qa.py`, autorizada por el usuario: cambia la `backend_fingerprint`
de M8 y deja `PHYSICS_BACKEND_CHANGED` los planes M8 anteriores (ver [QA de colisiones](collision-qa.md)). Toda la
API Blender usada está medida en 5.2.1 (línea base local no distribuida, véase [validación](validation.md), y la
sección "Hechos de Blender" abajo).

## 1. Principio: candidata en escena propia, fuente intacta

```
SOURCE SCENE (del usuario, nunca se cambian sus ajustes)
 └─ CANDIDATE SCENE  M10C_<g8>_v<n>         (bpy.data.scenes.new; fps = el de la fuente)
     ├─ colección M10C_<g8>_v<n>_SRC        colecciones de la fuente ENLAZADAS ENTERAS (mismos objetos), menos lo excluido
     └─ colección M10C_<g8>_v<n>_CIN        datablocks propios: cámaras, luces, copias de props, copias M8, empties
```

- **Enlace por colecciones (medido):** las colecciones hijas de la escena fuente se enlazan enteras en `_SRC`; así el
  `users_collection` de cada objeto no cambia (M8 lo firma: enlazar objetos sueltos dejaba obsoleta una generación M8
  aceptada). Una colección que contiene un objeto excluido se espeja con una colección propia
  (`M10C_<g8>_v<n>_SRCM<i>`) donde sólo sus objetos se enlazan uno a uno. Los objetos directamente en la colección
  maestra de la fuente también se enlazan uno a uno (`individually_linked_objects` en el estado).
- **Exclusiones al crear la candidata:** todo lo que la cinemática sustituye (originales de props que se mueven o se
  sostienen, mallas fuente de un secundario M8) va en `exclude_objects` de `cinematic_create_candidate`; después ya no se
  puede desenlazar de `_SRC` un objeto que llegó por una colección enlazada. `manage_props`/`integrate_secondary`
  rechazan un original que siga visible en la candidata.
- Ajustes de escena (rango, markers/cortes, `scene.camera`, world, exposición, render) **sólo** en la escena candidata.
  La escena fuente no se toca; su huella se comprueba antes y después.
- Datablocks propios: nombre con prefijo `M10C_<g8>_v<n>_` y propiedades `production_m10_owner = generation_id`,
  `production_m10_version = n`. Colisión de nombre = rechazo (`CINEMATIC_NAME_COLLISION`), nunca sobrescribir.
- Cambios a nivel de objeto en actores compartidos (inevitables: NLA y deltas) se **registran** en
  `state['assembly']['touched']` con su valor previo y se deshacen en orden inverso al rechazar. Las Actions fuente,
  mallas, materiales, rigs, shape keys, UV y cachés **nunca** se escriben: se verifica por huella.
- `g8` = `generation_id[:8]`. `_operation_id` del decorador es un uuid por llamada: los nombres deterministas salen del
  `generation_id` y la versión, nunca del operation_id.

## 2. Ciclo de vida (store genérico, categoría `cinematics`)

`STATES = {PLANNED, CINEMATIC_CANDIDATE, ASSEMBLED, REVIEWED, ACCEPTED, REJECTED}`; `animation.generation.store`
(`save/load/event/mutable/refinement_gate`). Referencia pública: `cinematic` = ruta absoluta de `plan.json`.

| Paso | Tool | Estado |
|---|---|---|
| brief, beats, shots (puros) | `cinematic_create_brief`, `cinematic_plan_beats`, `cinematic_plan_shots` | — |
| plan inmutable + candidata | `cinematic_create_candidate(apply=true)` | PLANNED → CINEMATIC_CANDIDATE |
| montaje (staging, sync, cara, secundario, props, cámaras, luces, foco, timeline, cortes) | tools de montaje con `apply=true` | CINEMATIC_CANDIDATE |
| montaje completo y verificado | `cinematic_manage_timeline(options.finalize=true)` | → ASSEMBLED |
| preview temporal completo + revisión real | `cinematic_render_preview`, `cinematic_review` | → REVIEWED |
| ≤3 refinamientos medidos | `cinematic_refine` | ASSEMBLED/REVIEWED (revisión nueva obligatoria) |
| aceptar / rechazar | `cinematic_accept`, `cinematic_reject` | terminales |

- `apply=false` = propuesta sin tocar Blender. Mutación: `require_mutable_asset` → `fresh()` → `create_checkpoint`
  → mutación → verificación → evento. Una excepción incierta del bridge (`ledger.is_uncertain`) marca
  `uncertain_operation=True` y el plan queda bloqueado para siempre (patrón M8/M9).
- Montaje tras ASSEMBLED o REVIEWED vuelve a CINEMATIC_CANDIDATE e invalida render y revisión.
- Aceptar exige: QA técnica sin errores (`cinematic_quality_report` vigente), revisión ACCEPT del preview vigente,
  integridad de fuente verificada, sin refinamiento pendiente. Aceptar **no** sustituye la escena fuente: deja la
  escena candidata marcada `ACCEPTED`. Rechazar deshace `touched`, borra los datablocks propios y la escena candidata,
  conserva evidencia y checkpoints, y verifica la integridad.

## 3. Estado (claves del store; cada evento guarda el estado completo)

```
state = {
  state, refinement_cycles, version, checkpoint, uncertain_operation, last_error,
  assembly: {
    version, scene, source_scene, collections: {src, cin}, excluded_from_src: [obj],
    owned: {objects: [], data: [[type, name]], actions: [], worlds: [], constraints: [[obj, name]]},
    touched: [ {kind, ...before} ],          # ver 3.1; se deshace en orden inverso
  },
  actors:   {actor_id: {object, staging, clips, facial, secondary, props}},
  cameras:  {camera_id: {object, data, settings, animation, constraints, focus}},
  shots:    [shot rows con camera_id, frame_start, frame_end],
  timeline: {frame_start, frame_end, fps, fps_base, markers: [[name, frame, camera]], conversions: []},
  lighting: {plan, lights: {...}, world, exposure},
  qa: {composition, continuity, visibility, collisions, contacts, motion, lighting, integrity},
  render: {...preview result}, previs: {...}, review: {...}, pending_refinement, last_refinement, history: []
}
```

### 3.1 Registro `touched` (lo único que se escribe fuera de datablocks propios)

| kind | campos `before` | deshacer |
|---|---|---|
| `delta` | object, delta_location, delta_rotation_euler, delta_rotation_quaternion | reponer valores |
| `animdata` | id_type (OBJECT/KEY), id_name, existed, action, slot_identifier, blend, influence, extrapolation, use_nla | reponer; si no existía, `animation_data_clear` sólo si quedó vacía |
| `nla_track` | id_type, id_name, track | borrar la pista propia |
| `nla_mute` | id_type, id_name, track, mute | reponer `mute` |
| `object_link` | collection, object | desenlazar de la colección propia |
| `object_unlink` | collection, object | volver a enlazar en la colección propia |

Tras deshacer: restaurar canales y shape keys capturados (quitar NLA deja la pose evaluada — medido).

## 4. Actores

Registro host-side (no toca Blender; permitido sobre MASTER; `register_actor` escribe sin `apply` porque sólo crea
un registro host idempotente): `<root>/cinematic-actors/<asset_key>/actor-<actor_id>.json`
(escritura exclusiva). `asset_key` = `asset_id` del rol actual, o `TEMPORARY-<sha16 de la escena>`. Identidad:
`fingerprint` de registro = huella de activo (rig: jerarquía y matrices de reposo; mallas: topología, posiciones, UV,
shape keys, slots y `material_index`) + rol + huesos + Actions corporales + referencias M7/M8, junto con nombre de
objeto y de datos; re-registrar el mismo objeto con la misma huella
devuelve el mismo `actor_id` (idempotente); con otra huella → nuevo registro y aviso `ACTOR_FINGERPRINT_CHANGED`.
Campos: `actor_id, object, data, type (ARMATURE|MESH|EMPTY), role, asset_role, meshes (lista de nombres),
body_actions[{action, slot, frame_range, digest, root_motion}], active_action, facial[{ref, state}],
secondary[{ref, state}], bones {root, pelvis, head, neck, hand_left, hand_right, foot_left, foot_right} (explícitos o
semántica M3 ≥ 0,9; si no, UNKNOWN), world_transform, dimensions_m, interaction_capabilities {reach_m (o UNKNOWN), ...},
integrity, fingerprint, limitations`.

`root_motion` se mide sobre las F-curves crudas (sin `frame_set`): `horizontal_distance_m` (XY entre el primer y el
último frame), `horizontal_displacement_m`, `direction` (unitario o null), `vertical_distance_m`, `duration_frames`
(último − primero), `bone`, `dominant_axis`, `status` (MEASURED / NO_ROOT_TRANSLATION_CHANNELS). Al crear un plan, cada
actor se vuelve a medir: si su rig, mallas o Actions cambiaron desde el registro → `ACTOR_STALE`.

## 5. Sincronización y montaje (Blender 5.2.1, medido)

- **Clips**: por actor, pista `M10C_<g8>_v<n>_BODY` con strips no solapados `{action, slot, start, action_frame_start,
  action_frame_end, scale (=1 salvo `retime` explícito), repeat, extrapolation}`. Mover con `frame_start_ui`; nunca los
  setters crudos `frame_start/frame_end` (mapeo incoherente, medido). `blend_type=REPLACE`. La Action activa del actor
  se desasigna (registrada en `animdata`) porque en REPLACE taparía el NLA (medido). Antes de fijar
  `action_slot` en un armature: si `pose is None`, `view_layer.update()` (fallo de Blender medido: segfault).
- **Staging**: `delta_location` y `delta_rotation_euler/quaternion` (registrados). Fórmula medida:
  `world = T(loc+dloc) @ (R_delta @ R_anim)`: el delta de rotación NO rota la trayectoria de `location` animada. Por eso:
  objeto con padre o constraints → `ACTOR_STAGING_UNSUPPORTED`; rotar un actor cuya Action anima `location` a nivel de
  objeto → `ROOT_MOTION_AMBIGUOUS`; Action que anima `delta_*` → `ACTOR_DELTA_ANIMATED`.
- **Continuidad de raíz (`carry_root`)**: un clip marcado empieza donde el anterior dejó la raíz (XY y giro sobre +Z).
  La corrección de mundo G = T(shift) @ Rz(yaw) se acumula y se escribe como delta en una pista propia
  `M10C_<g8>_v<n>_ROOT_<id8>` (CONSTANT, encima de la corporal), con filas `delta` y `nla_track` previas. Medido: la
  demo A pasa de un salto de 4,0 m a 0,0 m y la demo B conserva el giro de −90° con error de 6e-8 m y 1,6e-6°. Después,
  el staging de ese actor se rechaza (`ACTOR_DELTA_ANIMATED`): staging antes de sincronizar.
- **Cara (M7 ACEPTADA)**: leer `facial.service.load` + `scene_status`; exigir `state==ACCEPTED`, mismo fps que la
  escena, `candidate_fingerprint` sin cambios. Desplazamiento 0 con pista `M7_FACE <cand>` existente → se reutiliza;
  desplazamiento ≠ 0 → pista propia `M10C_<g8>_v<n>_FACE` en armature y en cada Key con `action_frame_start/end`
  explícitos y la pista M7 silenciada (`nla_mute`). Extrapolación explícita (por defecto NOTHING). Nunca nombrar pistas
  `M7_FACE*`. Huella de la Action facial antes/después.
- **Secundario (M8 ACEPTADO)**: el gate sólo puede pasar antes de que M10 coloque o sincronice ese actor
  (`SECONDARY_GATE_ORDER`); después, la sincronización exige el mismo Δ y se vuelve a medir la equivalencia. Gate en Δ=0 antes de mutar (`physics.service.load` + `fresh`, `cache.verify` con
  `cache_dependencies`, `bake_deviation.passed`). Soportado: representación PC2 (malla) con Δ entero: copia M10 del
  objeto `_Baked` en `_CIN` con `MESH_CACHE.frame_start = clip_start + Δ`; la fuente secundaria se excluye de `_SRC`;
  equivalencia medida frame a frame contra el baked M8 evaluado en `f` (tolerancia 1e-5 m), con las posiciones de los
  vértices en el **marco del padre** (matriz de pose del hueso padre). Así cuenta tanto la malla (PC2) como la
  Action de objeto: medido en la demo C, el colgante rígido de M8 tiene 91 muestras PC2 idénticas y oscila mediante su
  Action `…_Baked_Motion_0`; la prueba de reproducción por malla local daba 0,0 m y ahora mide 0,023 m. Con Δ ≠ 0 la
  copia desplaza también su Action de objeto con un strip NLA propio (sin medir aún en Blender). Representación
  `SECONDARY_ACTION` (armature) → `SECONDARY_BACKEND_UNSUPPORTED` (límite declarado). fps distinto, retime o ventana
  fuera del clip cacheado → rechazo. Los objetos M8 nunca se mutan.
- **Props**: copia propia (`obj.copy()`, comparte malla) en `_CIN`; el original se excluye de `_SRC`. Fijación por
  constraints `CHILD_OF` en la copia, con influencia animada por keys propias (hand-off = dos constraints). Nunca
  re-emparentar el original.
- **Cámaras**: objeto + data propios en `_CIN`; animación con una Action propia de dos slots (OBJECT + CAMERA, medido);
  track/foco con constraints propias (`TRACK_TO`/`DAMPED_TRACK`) y `dof.focus_object`/`focus_subtarget`.
- **Cortes**: `timeline_markers.new` + `marker.camera` en la escena candidata (cambia `scene.camera` en `frame_set` y
  en render — medido). `frame_current=` no cambia la cámara: muestrear siempre con `frame_set`.
- **Luces/world/exposición**: luces propias en `_CIN`; world propio asignado sólo a la escena candidata (el world fuente
  forma parte del contexto M8); `view_settings.exposure` de la escena candidata.

## 6. Muestreo, QA y render

- Un único muestreador multi-objeto (`cm_sample`): un `frame_set` por frame en la escena candidata, captura y
  restauración conjunta (patrón `px_capture/px_restore`), proyección manual con
  `cam.calc_matrix_camera(depsgraph, x=res_x, y=res_y, scale_x, scale_y)` (equivalente a `world_to_camera_view`,
  diferencia medida ≤5e-8; `bpy_extras` está prohibido), profundidad > 0 obligatoria para contar "en cuadro".
- Lectura de valores tras `frame_set`: **siempre evaluados** (`cm_evaluated(scene, obj)` =
  `obj.evaluated_get(cm_depsgraph(scene))`, el depsgraph de la view layer de esa escena, nunca el del contexto).
  Medido: el primer `frame_set` en una escena que no es la de la ventana evaluó el NLA bien (1,5) pero dejó el hueso
  original en 0,0.
- Visibilidad: `scene.ray_cast` filtrando a mano objetos `hide_render` (siguen bloqueando el rayo — medido).
- Colisiones: BVH en mundo con `FromPolygons` + `matrix_world` (`FromObject` es local — medido) y profundidad por
  paridad de rayos + `find_nearest` (`physics.qa.pq_collision_frame` vía su RUNTIME, sin editar `physics/`).
- Objetos que sólo viven en la candidata tienen `matrix_world` obsoleto hasta evaluar su view layer (medido): usar
  `cm_depsgraph(scene)` (temp_override con `scene` y `view_layer`).
- Huellas: un bucle de `frame_set` escribe los valores evaluados en los canales originales de los objetos animados
  (medido); por eso la huella usa el digest de animación en lugar de `matrix_basis` para todo objeto con Action, NLA o
  drivers (`cm_basis`).
- Render: Workbench (rasterizador GPU) para previs/preview; EEVEE para QA de iluminación; ambos se informan con
  `qa_render_backend`. Cycles sólo en `render_plan` y exige OptiX (regla del usuario: abortar sin GPU). Cada llamada al
  bridge < 180 s: renders troceados. `render.opengl` no funciona en background (medido): no se usa.
- NLA: en un hueco entre strips con extrapolación NOTHING los canales vuelven a su valor por defecto (medido: un
  actor saltó 2,0 m al origen); `sync_actions` lo avisa como hallazgo.
- Revisión: `review_gate` genérico + vistas/planos/velocidades revisados {1, 0.25}; prohibidos score/rating; informe con
  TECHNICAL FINDINGS y ARTISTIC OBSERVATIONS separados.
- Refinamiento: tras un ciclo conservado la validación de luz queda `LIGHTING_VALIDATION_STALE` (ligada a la huella).
  Medido en la demo A: el informe posterior bloquea la aceptación hasta repetir `cinematic_validate_lighting`.
- Un cambio de montaje (`store.invalidate_review`) borra render, revisión, informe y el resumen `assembled`.
- Conjuntos de mallas de un actor (análisis, revisión, iluminación): `store.actor_meshes` quita lo excluido al crear la
  candidata y añade las copias propias del secundario.

## 7. Contrato de código

- `tools.py`: `NAMES` (37 requeridas + `cinematic_review`, `cinematic_status`), `READ_ONLY`, `TRANSIENT`; firma común
  `(cinematic=None, actor=None, object_name=None, brief=None, options=None, apply=False)`; todo pasa por
  `service.dispatch(name, ctx, ...)`, que llama a `HANDLERS[name](ctx, request)`.
- `ctx = Context(bridge, storage, operation_id)`; `request = Request(cinematic, actor, object_name, brief, options, apply)`.
- Errores: `CODIGO: detalle` (UPPER_SNAKE) para que `error_codes` los recoja.
- Payloads: `runtime.COMMON` (helpers compartidos) + funciones del subsistema + `result = fn(params)`; pasar
  `build_code` (safe mode), < 140.000 bytes y < 14.000 nodos AST (margen bajo los límites 200.000 / 20.000).
- Respuestas compactas: nada de vértices ni keys completas; muestras resumidas y paginadas.

## 8. Hechos de Blender 5.2.1 usados (medidos en esta fase)

Una Action admite una sola capa y varios slots; `ActionSlots.new(id_type, name)`; `strip.channelbag(slot, ensure=True)`;
`NlaStrips.new(name, start:int, action)` autoasigna slot si coincide el identificador (o es el único); valores por defecto
de strip: HOLD, REPLACE, influence leída 0.0 pero evalúa al 100 % sin `use_animated_influence`. Markers con cámara:
`frame_set` cambia la cámara; el render obedece al marker. Escenas compartiendo objetos: `scenes.new` +
`collection.children.link`; render de otra escena con
`with bpy.context.temp_override(scene=candidata): bpy.ops.render.render(write_still=True)` — **no** `scene=name`
(medido: renderiza el frame y la cámara de la escena de la ventana). Cámara:
`lens, lens_unit, sensor_width/height, sensor_fit, shift_x/y, clip_start/end, dof.{use_dof, focus_object,
focus_subtarget, focus_distance, aperture_fstop, aperture_blades}`. Luces: `energy, color, use_shadow,
shadow_soft_size, spot_size, spot_blend, shape, size, size_y, angle, temperature`. World nuevo con `Background`/`Strength`.
`view_transform` AgX por defecto; los looks de AgX llevan prefijo `AgX - `. Máscaras Workbench exactas sólo con
`view_transform='Standard'`, `display.render_aa='OFF'`, `dither_intensity=0`. `Render Result.pixels` vacío en background:
guardar y releer. EEVEE id `BLENDER_EEVEE`, rasteriza en la RTX 4060.
