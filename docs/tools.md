# Tools implementadas — Milestones 1 a 9

La versión local **0.9.0** registra **274 tools verificadas por MCP stdio**: 236 heredadas de M1–M8 y **38 de materiales**. M9 está funcionalmente verificado sobre fixtures controladas; el [validación pública y límites](validation.md) documenta la regresión final y la cobertura MCP compuesta, conservando el FULL histórico fallido y su suplemento PASS. Las cifras por milestone que siguen son históricas y conservan sus contratos.

La base M4 de `blender-production` conserva 85 tools: 11 de M1, 18 de M2, 25 de M3, 11 de M3.1
(estado de operaciones, assets, limpieza y base de seguridad de malla) y 20 de M4 (modelado y
rediseño de personajes). M5 registra otras 35 y M6 añade 35: catálogo implementado de **155 tools**.
M6 está cerrado en la entrega local 0.6.0; [validación pública y límites](validation.md).
Los contratos y limitaciones por dominio se describen abajo. Un catálogo implementado no declara por sí solo
el cierre del milestone ni sustituye su informe de verificación. Los análisis
son consultivos; los correctores exigen propuestas y trabajan en copias. Nombres de
objetos, armatures y Actions son identificadores exactos de Blender; no se asume
un personaje o convención de nombres concreta.

## Contrato de resultados

Cada tool devuelve un sobre con `status`, `operation_id`, `tool`, `affected`,
`before`, `after`, `warnings`, `errors`, `metrics`, `checkpoint` y, cuando la
ejecución devuelve datos, `data`. Los campos no pertinentes pueden estar vacíos
o ser `null`. `metrics.elapsed_ms` mide la operación completa; métricas propias
de un análisis/render aparecen en `metrics` y en `data.metrics`.

`status` usa `success`, `warning` o `error`. Las advertencias metodológicas pueden
producir `warning` aun cuando la herramienta funcione correctamente. Los
resultados de contenido —por ejemplo `measurement_status: fail` por foot
sliding— están dentro de `data` y deben revisarse independientemente del estado
de ejecución. Los errores incluyen el mensaje real; no equivalen a una
corrección ni activan reintentos destructivos.

M2 añade `mode`, `target`, `proposal`, `operation`, `correction_id`,
`numeric_verification` y `visual_verification` cuando corresponden. `operation_id`
identifica cada llamada; `correction_id` enlaza el ciclo persistente. `operation`
es la ruta local al `proposal.json` inmutable. Preview/apply/verify/revert exigen
esa referencia con `options` vacío. Las opciones se congelan al proponer.

### Cambios de contrato en M3.1

- `operation_id` en el sobre es la clave de idempotencia del cliente si la envió, o el id generado;
  `execution_id` identifica siempre esta ejecución concreta. Las tools que no son de lectura aceptan
  un argumento opcional `operation_id` (8–128 caracteres `[A-Za-z0-9._:-]`); ver
  [idempotencia](operation-idempotency.md). El bloque `idempotency` informa `state`, `attempt`,
  `replayed` y `reconciled`.
- `error_codes` lista los códigos estructurados (`POSE_POSITION_REQUIRED`, `STALE_PROPOSAL`,
  `CAPACITY_EXCEEDED`, `MASTER_ASSET_READ_ONLY`, `RENDER_GPU_REQUIRED`…) extraídos de `errors`.
- Las propuestas llevan `capacity` con `estimated_samples`, `estimated_memory_bytes`,
  `estimated_runtime_class`, `configured_limit`, `supported` y `payload_bytes` frente al límite de
  200 000 bytes de safe mode. Es una estimación gruesa con tasas medidas, no una promesa de tiempo.
- Los renders QA devuelven `render_backend` (`requested_backend`, `available_backend`,
  `actual_backend`, `fallback_used`, `warning`). Cycles solo se renderiza en GPU; la CPU exige
  `allow_cpu_fallback=true` y queda marcada.
- `animation_analyze_contacts` y `animation_analyze_motion_quality` añaden `schema` y `backend` para
  distinguir sus dos formas de salida (`production.contacts.ground_plane.v1` /
  `production.contacts.surfaces.v1`, `production.motion_quality.rig.v1` /
  `production.motion_quality.limb_chains.v1`).

### Hints MCP fieles (AUD-016)

- `readOnlyHint=true` solo para tools cuyo payload no cambia nada en Blender, ni siquiera de forma
  transitoria. `tests/unit/test_m31_hints.py` lo comprueba inspeccionando el payload enlazado de cada
  tool.
- 15 lectores cambian el frame, la asignación de Action o la pose mientras se ejecutan y lo restauran
  (foot sliding, contactos, root motion, balance, motion quality, refinement plan, fases de pie,
  contacto de efector, pops, stretch, compare_actions, deformation_analyze, deformation_compare,
  animation_deformation_qa, mocap_analyze). Tienen `readOnlyHint=false`, `idempotentHint=true` y
  `meta.production.scene_effects="transient_restored"`. Una llamada interrumpida puede dejar el frame
  o una Action cambiados.
- `destructiveHint=true`: `restore_checkpoint` y `cleanup_artifacts`.

`numeric_verification` puede ser `not_applied`, `pass`, `fail`, `stale` o
`unverified` según fase/resultado. Un `pass` mantiene `visual_verification="required"`:
no afirma aceptación artística. Los snapshots/patches completos permanecen en
registros locales. Consultar [seguridad de correcciones](animation-correction-safety.md).

## Inspección y checkpoints

| Tool | Argumentos principales | Resultado y alcance |
|---|---|---|
| `production.scene_audit` | `object_name=null`, `limit=100`, `offset=0`, `detail="summary"` | Resumen compacto de Blender/escena/origen/unidades/FPS/rango y objetos. `detail="full"` añade transforms/matrices y metadatos completos; snapshots internos conservan todos los metadatos. |
| `production.create_checkpoint` | `description` obligatorio, `operation=null` | Copia nueva `.blend`, manifest, timestamp, origen, SHA256 y snapshot. No reemplaza el archivo activo. |
| `production.restore_checkpoint` | `checkpoint` ruta a manifest, `dry_run=true` | Plan por defecto. Con `false`, checkpoint de recuperación y apertura de otra copia de trabajo, scripts desactivados. |
| `production.diff_scene` | `checkpoint`, `other_checkpoint=null`, `limit=100`, `offset=0` | Compara metadata con escena viva o segundo checkpoint. Reporta añadidos, eliminados y cambios paginados. |
| `production.inspect_armature` | `armature`, `limit=100`, `offset=0` | Huesos rest, jerarquía, deform/no deform, constraints, drivers, meshes ligados y conteos. |
| `production.rig_analyze` | `armature`, `limit=100`, `offset=0` | IK explícito, targets/poles, candidatos FK/control, simetría espacial, constraints/drivers e incidencias. |

Paginación: `limit` entre 1 y 500; `offset` no negativo. Cada respuesta indica
totales y su alcance. El mismo offset puede aplicarse a varias colecciones;
consultar sus respectivos totales.

Un snapshot/diff no contiene ni verifica valores de vértices, pesos, poses,
F-Curves o nodos. Los checkpoints no copian texturas ni otros archivos externos.
Restore solo acepta manifests dentro del almacén de checkpoints del workspace y
verifica su SHA256. Conservar las dependencias externas para poder recuperar
el proyecto completo.

Rig analysis asigna confianza 1.0 a la existencia de una constraint IK explícita;
reporta si está activa. FK y controles son candidatos con evidencias. Las parejas
espaciales usan el eje X local del armature y no establecen izquierda/derecha
anatómica. No deduce automáticamente la semántica de switches IK/FK ni modifica
huesos basándose en la puntuación.

## Análisis de animación

| Tool | Argumentos principales | Resultado y alcance |
|---|---|---|
| `production.animation_analyze` | `object_name=null`, `action_name=null`, `limit=100`, `offset=0` | Actions, slots/canales, keys, interpolaciones, timing y advertencias; admite Actions layered y legacy. |
| `production.animation_detect_foot_sliding` | `armature`, `bones=null`, rango opcional, parámetros de contacto | Candidatos, contactos, desviación horizontal en m/cm, frames problemáticos y métricas. |
| `production.animation_detect_quaternion_flips` | `object_name=null`, `action_name=null`, `limit=100`, `offset=0` | Grupos WXYZ, discontinuidades de hemisferio, cambios grandes de orientación y quaternions inválidos/incompletos. |

`object_name` selecciona la Action activa y el slot del objeto. Una Action
seleccionada por nombre sin objeto se examina en sus slots. El análisis de
canales no aplana la composición NLA.

### Foot sliding: unidades y límites

| Parámetro | Default | Significado |
|---|---:|---|
| `bones` | `null` | Inferencia conservadora; se recomienda una lista validada de marcadores. Máximo 32. |
| `frame_start` / `frame_end` | rango de escena | Frames enteros a evaluar. |
| `step` | 1 | Separación de muestras; valores mayores pueden omitir contactos breves. |
| `ground_height_m` | 0.0 | Altura mundial Z del plano de contacto del **marcador**. |
| `contact_height_m` | 0.03 | Tolerancia de altura: 3 cm. |
| `contact_speed_m_s` | 0.25 | Máxima velocidad **vertical** para inferir contacto. |
| `sliding_tolerance_m` | 0.01 | Desviación horizontal tolerada: 1 cm. |
| `min_contact_frames` | 3 | Duración mínima del intervalo, expresada en frames originales. |

Estos defaults son puntos de partida configurables para revisión; no constituyen
un estándar universal de calidad. El marcador es la cabeza del hueso evaluado,
incluyendo transforms del objeto, constraints y NLA. No representa
automáticamente la suela del personaje. Convertimos Blender units a metros con
`scene.unit_settings.scale_length`; con unidades `NONE`, la respuesta advierte
que se debe confirmar la escala física.

El contacto usa altura y velocidad vertical, sin excluir velocidades
horizontales altas que justamente podrían ser sliding. El error principal es
la máxima distancia XY desde el primer punto del contacto. También se devuelve
longitud recorrida y desplazamiento neto; un movimiento que vuelve al origen
no oculta su deslizamiento.

La selección automática combina señales y asume un rig erguido en Z mundial;
devuelve confidence y evidencias, sin modificar nada. Elegir `bones` explícitos
para rigs rotados, criaturas o controles ambiguos. Pendientes, escaleras,
plataformas móviles, heel/toe roll y hover intencional requieren revisión. Sin
contactos suficientes, la medición es `inconclusive`. El muestreo está acotado a
aproximadamente 10.000 frames y 100.000 evaluaciones hueso/frame; la respuesta
indica truncamiento de detalles cuando corresponde.

Ejemplo de argumentos con marcadores previamente inspeccionados:

```json
{
  "armature": "Rig",
  "bones": ["validated_marker"],
  "frame_start": 1,
  "frame_end": 24,
  "ground_height_m": 0.08,
  "sliding_tolerance_m": 0.005
}
```

### Quaternions

Se normalizan WXYZ y comparan en la unión de tiempos keyed/baked de los canales.
Producto escalar negativo identifica un cambio de hemisferio candidato.
`q` y `-q` representan la misma orientación: esa señal no demuestra un salto
visual. Cambios de orientación mayores de 90° entre muestras se marcan para
revisión con un umbral documentado. No se resuelven NLA/drivers ni overshoot
entre keys. La herramienta no cambia signos ni elimina keyframes.

## QA visual

| Tool | Argumentos | Comportamiento |
|---|---|---|
| `production.qa_capture_viewport` | `max_size=1280` (4–4096) | OpenGL render de un VIEW_3D de la ventana activa a PNG nuevo. Requiere GUI y región drawable. |
| `production.qa_render_preview` | `frame_start=null`, `frame_end=null`, `step=1`, `width=640`, `height=360`, `max_frames=24` | Checkpoint previo y secuencia PNG acotada con cámara/engine existentes. |

Preview sin ambos extremos produce un único frame actual. Con un extremo
omitido se usa el extremo correspondiente del rango de escena. Un rango vacío
o mayor a `max_frames` falla explícitamente; no se recorta silenciosamente.
Dimensiones permitidas: 4–4096 px; `max_frames`: 1–240. No se crean cámaras,
luces ni materiales. Si falta cámara, se devuelve error.

Viewport usa el shading actual de la vista y conserva su proporción; no es una
captura de escritorio. Ambas tools devuelven rutas locales y dimensiones reales
leídas del PNG. Para evaluarlas, abrir las imágenes mediante las capacidades
visuales del cliente; la presencia de un PNG no certifica calidad de animación.

Se restauran frame/subframe, cámara, ruta, resolución, porcentaje, formato/color,
extensión, border/crop, compositing, sequencer y multiview mediante `finally`.
Blender 5.2 requiere cambiar temporalmente `image_settings.media_type` a `IMAGE`
para PNG; también se restaura, con detección de disponibilidad para otras
versiones. Render Result queda reemplazado. Compositing, sequencer y multiview
se desactivan temporalmente para mantener la salida dentro de la operación QA;
el preview puede diferir del render final compuesto.

Una cancelación/error normal conserva los archivos ya completados y reporta
el estado de restauración. Un timeout/pérdida del socket puede ocurrir mientras
Blender sigue trabajando: no garantiza restauración ni integridad de archivos
parciales. Conservar outputs, verificar Blender y consultar el checkpoint antes
de repetir. Reducir rango/resolución o usar un frame inicial para estimar costo.

## Gestión de Actions M2

| Tool | Argumentos principales | Comportamiento |
|---|---|---|
| `production.animation_create_action` | `name`, `object_name=null`, `assign=false`, `dry_run=true` | Plan o Action nueva con nombre sin colisión y fake-user; checkpoint antes de crear. |
| `production.animation_duplicate_action` | `action_name`, `object_name=null`, `name=null`, `assign=false`, `dry_run=true` | Copia conservando canales/metadatos y original; checkpoint al ejecutar. |
| `production.animation_compare_actions` | `object_name`, `before_action`, `after_action`, rango opcional | Fingerprints y diferencias de poses evaluadas; restaura Action/slot/pose/tiempo. No prueba calidad artística. |
| `production.animation_restore_action` | `object_name`, `action_name`, `dry_run=true` | Reasigna Action conservada con checkpoint; no elimina Action actual. |

Comparación requiere Actions no ambiguas y rechaza composición NLA activa. Su
rango admite hasta 1001 frames enteros. Una Action multi-slot puede analizarse
con herramientas M1; eso no implica que sea elegible para corrección M2.

## Análisis avanzado M2

Estas tools usan `object_name` y `options={}`:

| Tool | Datos principales |
|---|---|
| `production.animation_analyze_contacts` | Candidatos de pie/mano, evidencias, intervalos, altura/velocidad vertical, desplazamiento y confianza. |
| `production.animation_analyze_fcurves` | Keys redundantes, spikes candidatos, derivadas/aceleración, interpolación, ruido, overshoot y quaternion; filtros y paginación. |
| `production.animation_analyze_root_motion` | Root candidato/seleccionado, XYZ, distancias, velocidades, orientación, cambios de dirección y discontinuidades candidatas. |
| `production.animation_analyze_control_mode` | IK explícito, candidatos FK, mixto/deform-only/incierto; evidencia del frame actual. |
| `production.animation_analyze_balance` | Proyección root/pelvis sobre discos/cápsula de uno o dos apoyos; análisis heurístico sin corrección. |
| `production.animation_analyze_motion_quality` | Velocidad, aceleración, variación de derivadas, métricas angulares y consistencia de contactos; sin score artístico. |
| `production.animation_refinement_plan` | Analiza los dominios anteriores, Action y rig, ordena hallazgos y recomienda correcciones. Solo planifica. |

Opciones compartidas de insights: `frame_start/end`, `step=1`, `max_samples=1000`
(hasta 2000), `bones` (hasta 32), `action_name`, `root_bone`, `root_object=false`,
`pelvis_bone`, `support_bones` (1–2), `detail="summary"|"events"`, `limit=50`
(hasta 100), umbrales de contacto/movimiento y `fcurve_options` para el plan.
`action_name` debe coincidir con el activo: no realiza asignaciones. `root_object`
y `root_bone` son excluyentes. Control mode describe el frame actual incluso si
se recibe un rango; lo advierte. Opciones desconocidas se rechazan.

F-Curves tiene opciones propias: `bones`, `channels`, `data_paths`,
`array_indices`, `frame_start/end`, `severity`, `detail="summary"|"full"`,
`limit=100` (máximo 200), `offset=0`, `tolerance`, `per_channel_tolerances`,
`spike_factor`, `spike_min_amplitude`, `noise_threshold` y `samples_per_interval`.
Los filtros de canales usan la propiedad final del data_path, por ejemplo
`location`; las tolerancias usan unidades nativas de cada canal. Ver
[definiciones y defaults](animation-metrics.md).

## Correctores M2

Los cinco correctores comparten argumentos:
`object_name=null`, `mode="analyze"`, `options={}`, `operation=null`.
Modos: `analyze`, `propose`, `preview`, `apply`, `verify`, `revert`.

| Tool | Opciones de propuesta y soporte |
|---|---|
| `production.animation_fix_foot_sliding` | `controls`, `confirm_contacts=true`, rango, `blend_frames=3`, calibración/umbrales contacto y tolerancias de posición/articulación/stretch/alcance. IK validado de dos huesos, canales location LINEAR. |
| `production.animation_fix_quaternion_flips` | Filtros de curvas/rango y `angular_tolerance_degrees`; WXYZ completo y tiempos alineados, verifica orientación interpolada. |
| `production.animation_cleanup_fcurves` | Filtros, tolerancias y una `strategies` por operación: `redundant`, `small_noise`, `spike`, `interpolation` o `mocap_smoothing`. |
| `production.animation_prepare_root_motion` | `root_bone` explícito, `strategy="in_place"`, rango y `position_tolerance_m`; root sin parent/constraints, location LINEAR. |
| `production.animation_retime` | Uno de `scale_factor` o `target_duration_seconds`, rango y tolerancias; conserva FPS, poses en fase equivalente y relaciones de Action markers. |

Cleanup usa `redundant` por defecto. Las otras estrategias exigen tolerancia
explícita global o por canal; smoothing requiere `allow_smoothing=true`, spikes
`allow_spike_correction=true`, e interpolación
`allow_interpolation_change=true` con `target_interpolation="LINEAR"`.
No cambia quaternion como curvas escalares: ese caso tiene corrector propio.

`analyze` no edita ni crea candidatos. `propose` añade solo el registro local.
`preview` sí crea checkpoint y Action candidata, pero restaura la asignación
original. `apply` crea su duplicado y lo activa tras pasar verificación numérica.
`verify` mide otra vez y detecta cambios. `revert` vuelve al original con
checkpoint, preservando candidato. Para nuevas opciones crear nueva propuesta.

No hay parámetro que fuerce corrección sobre un rig/Action no soportado. Los
límites exactos y recuperación están en [seguridad](animation-correction-safety.md).

## QA multiview M2

| Tool | Argumentos | Comportamiento |
|---|---|---|
| `production.qa_render_multiview` | `object_name`, `options={}` | Cámaras temporales y encuadre del objetivo, checkpoint y PNGs por vista; restaura estado. |
| `production.qa_compare_before_after` | `object_name`, `before_action`, `after_action`, `options={}` | Series `before/` y `after/` con encuadre común, cámara/tiempo/resolución equivalentes y mosaico opcional. |

Defaults: `views=["FRONT","SIDE","THREE_QUARTER"]`, `quality="lightweight"`
(Workbench), 480×480, `max_frames=3`, `step=null`, `margin=1.25`, `montage=true`.
Admite rango, dimensiones 16–1920, hasta 24 frames/vista y slots explícitos
`action_slot` o `before_slot/after_slot`. `quality="scene"` usa el engine de escena.
Se rechaza la composición NLA activa y se exigen slots cuando son ambiguos.

Los ejes de cámara son convenciones mundiales, no identificación automática del
frente anatómico. `comparable=true` certifica equivalencia técnica de artefactos,
no calidad artística. Ver [QA visual](visual-qa.md).

## Fuera de M2

No se expone `animation_refine` ciega, retargeting, edición de pesos, exportación
Unreal ni conversión IK/FK general. Esa fue la restricción histórica de M2. M3, M4, M5 y M6 fueron autorizados después. M7 y el uso de proyectos reales requieren autorización aparte.

## M3: rigging, geometría de contactos, mocap y deformación

Todas llevan `production.`. Los 29 nombres previos y sus defaults se conservan.
Nuevas transacciones usan el mismo ciclo analyze/propose/preview/apply/verify/revert;
modo inicial analyze. Las propuestas incluyen schema y fingerprint del backend:
no reutilizar operaciones creadas con otro código.

| Tool nueva | Entrada principal | Contrato |
|---|---|---|
| rig_classify | object_name, options | Categoría y modo por extremidad, unknown válido |
| rig_map_semantics | object_name, options.mapping opcional | Evidencias, confianza, alternativas; no autoriza mutación |
| rig_analyze_controls | object_name, options | Controles/targets/poles, propiedades y drivers |
| rig_analyze_limbs | object_name, options | Cadenas estructurales y modo/switch por extremidad |
| rig_compatibility_report | object_name, options | Capacidad por función con condiciones y límites |
| rig_match_fk_to_ik / rig_match_ik_to_fk | object_name, mode, options, operation | Adaptador explícito, checkpoint/copia, error mundial de pose |
| rig_bake_fk_to_ik / rig_bake_ik_to_fk | mismos; rango/step | Movimiento muestreado, copia, timing conservado, verify |
| animation_analyze_foot_phase | object_name, options.feet/surfaces | Heel/sole/toe, fases y drift físico con confianza |
| animation_detect_contact_surface | object_name, options.bone/point/surfaces | Punto/normal/distancia a malla estática evaluada |
| animation_analyze_effector_contact | object_name, options | Dwell/contacto, drift y penetración estimada |
| animation_detect_limb_pops | object_name, options.chains | Ángulo/plano, velocidades/aceleraciones, poles y extensión |
| animation_detect_limb_stretch | object_name, options.chains | Longitud mundial vs rest; stretch intencional diferenciado |
| animation_analyze_bezier | object_name, options | Overshoot/tangentes; velocidad alta no implica defecto |
| animation_repair_bezier | object_name, mode, options, operation | Handles seleccionados, copia, desviación/derivadas por segmento |
| mocap_analyze | object_name, options | Jerarquía, canales, root, escala, ruido y pose de reposo probable |
| mocap_map_skeleton | source_armature, target_armature, mapping, mapping_approved=false | Validación uno-a-uno y roles requeridos; no retarget implícito |
| mocap_preview_retarget | object_name, options, operation | Sin operation propone; con operation crea preview y restaura asignación |
| mocap_retarget | object_name, mode, options, operation | Mapping revisado, rest/scale/root, Action nueva y QA |
| mocap_cleanup | object_name, mode, strategy, options, operation, apply=false | Una corrección revisada y medida por propuesta |
| deformation_analyze | object_name, options | Métricas de geometría evaluada contra referencia |
| deformation_compare | object_name, before_action, after_action, options | Unión de muestras relevantes y referencia equivalente |
| deformation_test_pose | object_name, poses, dry_run=true, options | Stress explícito temporal, checkpoint, finally/restore |
| animation_deformation_qa | object_name, action_name opcional, options | Eventos/extremos/velocidad y deformación asociada |

Ampliaciones de tools existentes:
- animation_analyze_contacts: feet+surfaces selecciona geometría M3; sin ellos sigue el plano M2.
- animation_fix_foot_sliding: propuesta M3 por feet+surfaces; IK/FK conservador y Bézier. El kind guardado dirige preview/apply/verify/revert.
- animation_analyze_control_mode: añade semantic_control_modes por extremidad sin retirar campos M2.
- animation_analyze_motion_quality: options.chains selecciona métricas M3 de extremidades; sin chains sigue M2.
- qa_render_multiview / qa_compare_before_after: LOW_ANGLE y FOOT opcionales, framing_bones/framing_meshes; FOOT requiere encuadre explícito. Tres vistas históricas siguen siendo defaults.

Parámetros exactos y garantías:
[Semántica](rig-semantics.md) · [IK/FK](ik-fk.md) · [Contactos](contact-model.md) ·
[Bézier](bezier-correction.md) · [Mocap](mocap-retargeting.md) ·
[Deformación](deformation-qa.md) · [Compatibilidad](rig-compatibility.md).

## M3.1: operaciones, assets, limpieza y base de malla

| Tool | Entrada principal | Contrato |
|---|---|---|
| operation_status | operation_id **o** correction_id | Estado del ledger (NEW/RUNNING/SUCCEEDED/FAILED/UNCERTAIN/REVERTED), checkpoint, candidato esperado, reconciliación. No llama a Blender |
| asset_status | — | Rol del .blend abierto y si se permite mutar |
| asset_register_master | confirm_master=false | Registra el archivo abierto como MASTER de solo lectura (SHA256); no lo modifica |
| asset_register_fixture | path | FIXTURE solo bajo temp, `<workdir>/fixtures` o `BLENDER_PRODUCTION_FIXTURE_ROOTS` |
| asset_create_working_copy | — | Copia sellada en `<workdir>/working`, abierta sin scripts y registrada |
| cleanup_artifacts | dry_run=true, min_age_days=7, keep_last=5, categories | Inventario de ciclo de vida; borra solo lo elegible y con dry_run=false; candidatos solo informados |
| mesh_signature | object_name | Huellas por sección; firma completa en el workspace; nada por vértice al modelo |
| mesh_region_define | object_name, regions | Regiones LOCKED/EDITABLE por grupo, face set, máscara o índices ligados a topología |
| mesh_region_verify | manifest, object_name opcional | Falla si una región bloqueada cambia fuera de tolerancia |
| mesh_candidate_create | object_name, source_fingerprint | Copia de objeto y malla con checkpoint y guard de asset; informe de preservación; reutiliza si ya existe |
| mesh_candidate_verify | object_name, candidate_name, regions_manifest opcional | Comparación por sección y regiones; aceptar o rechazar es M4 |

Detalles: [seguridad de assets](asset-safety.md), [firma de malla](mesh-signature.md),
[validación pública y límites](validation.md). La base de malla no es M4: no esculpe, no remodela y no se
probó con assets reales.

## M4: modelado y rediseño de personajes

Todas las tools usan la malla base sin deformar de un objeto explícito y devuelven medidas compactas.
Las de lectura (`readOnlyHint=true`) no cambian nada. Las que mutan exigen un rol de asset mutable
**antes** de cualquier checkpoint. Un MASTER se rechaza con `MASTER_ASSET_READ_ONLY`.

| Tool | Tipo | Entrada principal | Contrato |
|---|---|---|---|
| character_inspect | lectura | object_name, mirror_fraction | Recuentos, dimensiones, simetría, topología, pesos, shape keys, rol del asset y candidatos de región con confianza y evidencia |
| character_define_regions | artefacto | object_name, regions, include_automatic | Manifiesto de regiones automáticas y del usuario (locked/editable/unassigned); captura posiciones, caras, materiales, UV, pesos y shape keys. No edita |
| character_lock_regions | artefacto | manifest, lock, editable, unassign, intentional | Manifiesto nuevo recapturado; las automáticas ambiguas no pueden ser editables |
| character_validate_locked_regions | lectura | manifest, object_name | Posiciones, shape keys, pesos, caras, material y UV de las regiones bloqueadas; topología cambiada falla |
| character_analyze_silhouette | lectura | object_name, views, rows, manifest | Silueta por vista y medidas con nombre; sin puntuación |
| character_analyze_proportions | lectura | object_name, profile HUMANOID/CREATURE/CUSTOM, custom_reference | Medida, referencia informativa, desviación, confianza e interpretación separadas |
| character_analyze_asymmetry | lectura | object_name, manifest, intentional_regions | intentional/likely_intentional/likely_accidental/symmetric/unknown con umbrales declarados |
| character_analyze_surface | lectura | object_name, manifest, options | Bultos, pellizcos, curvatura, ruido, densidad, estiramiento, facetado y normales, por región |
| character_analyze_intersections | lectura | object_name, manifest, reference_object | Solapamientos BVH por pareja de regiones; solo los nuevos si hay referencia |
| character_modeling_audit | lectura | object_name, manifest, profile | Hallazgos type/region/severity/evidence/suggested_action; sin puntuación |
| character_redesign_plan | lectura | object_name, intent, manifest | Operaciones propuestas con desplazamiento estimado, proximidad bloqueada, riesgo y justificación; apply=false |
| character_create_candidate | mutación (retry_safe) | object_name, source_fingerprint | Malla candidata con checkpoint y manifiesto de dependencias; reutiliza la existente |
| character_adjust_shape | mutación | object_name, mode, options / operation | analyze/propose/preview/apply/verify/revert de una operación que conserva la topología |
| character_deformation_qa | mutación transitoria | rig, source_mesh, candidate_mesh, stress_poses | Mismas poses de estrés en fuente y candidato; falla si el candidato añade defectos |
| character_render_multiview | artefacto | objects, views, width, height | Cámaras, resolución y look idénticos; montaje |
| character_compare_before_after | artefacto | before_object, after_object, views, manifest | Renders equivalentes y deltas de silueta |
| character_capture_reference_views | artefacto | object_name, views, concept_art | Conjunto de referencia (cámaras, imágenes, siluetas), concept art asociado |
| character_capture_candidate_views | artefacto | reference_set, object_name | Render con las cámaras exactas de la referencia |
| character_compare_reference | artefacto | reference_set, object_name | Diferencias de silueta medibles e imágenes emparejadas |
| character_redesign_session | mutación opcional | object_name, intent, manifest, apply=false, step, rig, stress_poses | apply=false: inspección, auditoría, plan y riesgo. apply=true: un único ciclo (candidato, propuesta, preview, QA numérica, de deformación y visual) |

Códigos de rechazo principales: `MASTER_ASSET_READ_ONLY`, `ASSET_ROLE_UNREGISTERED`,
`CANDIDATE_REQUIRED`, `LOCKED_REGION_EDIT`, `REGION_NOT_EDITABLE`, `REGION_AMBIGUOUS`,
`REGION_UNDEFINED`, `TOPOLOGY_CHANGED`, `STALE_PROPOSAL`, `STALE_SIGNATURE`, `SHAPE_KEY_CONFLICT`,
`SHAPE_KEYS_ABSOLUTE_UNSUPPORTED`, `SHAPE_KEY_BASIS_MISMATCH`, `NOTHING_TO_EDIT`, `CAPACITY_EXCEEDED`,
`PLAN_STEP_HIGH_RISK`, `DEFORMATION_REGRESSION`. Errores de verificación: `TOPOLOGY_CHANGED`,
`WEIGHTS_CHANGED`, `UV_CHANGED`, `MATERIALS_CHANGED`, `NORMALS_STATE_CHANGED`, `ATTRIBUTES_CHANGED`,
`SHAPE_KEY_OFFSETS_CHANGED`, `LOCKED_REGION_MODIFIED`, `FACE_FLIPPED`.

Detalles: [modelo](character-model.md), [regiones](region-locking.md),
[rediseño](character-redesign.md), [escultura](sculpt-safety.md), [shape keys](shape-key-safety.md),
[QA visual](character-visual-qa.md).

## M5: retopología, game mesh y bake preparation

M5 añade exactamente 35 tools, manteniendo las 85 anteriores. En las tablas se omite el prefijo
`production.`. **Lectura** significa `readOnlyHint=true`; **operación** significa que el contrato
puede crear artefactos o cambiar Blender, incluso cuando algunas opciones sólo analizan. Hay 16
tools nuevas de lectura y 19 de operación. Las de operación aceptan el `operation_id` público;
`_operation_id` es interno y no aparece en su schema MCP.

### Relación y ciclo compartidos

`relationship` es la ruta al manifiesto local `production.high_low.v1`, nunca un emparejamiento por
nombre. `retopology_create_candidate` lo crea usando el fingerprint M5 devuelto por
`retopology_analyze` en `data.signature.fingerprint`; el fingerprint de una firma M3.1 no es un
sustituto. Un `target_object` opcional se selecciona explícitamente. HIGH y el LOW anterior se
conservan.

Las tools de corrección usan `relationship=null`, `mode="analyze"`, `options=null`,
`operation=null` como defaults. Analyze/propose necesitan la relación; preview/apply/verify/revert
necesitan únicamente el `operation` guardado, sin enviar otra relación ni nuevas opciones. Propose
congela parámetros, backend, firmas y contexto de asset. Preview y apply crean checkpoint y un
nuevo objeto+malla; sólo un resultado verificado puede crear una revisión HIGH/LOW utilizable.
Un cage conserva la relación del LOW y devuelve su candidato por separado.

Ejemplo de llamadas sucesivas, con valores ilustrativos que deben revisarse para el asset:

```json
{"tool":"production.retopology_optimize_region","relationship":"<relationship.json>","mode":"propose","options":{"strategy":"GUIDED_DECIMATION","target_triangles":120000}}
{"tool":"production.retopology_optimize_region","mode":"preview","operation":"<proposal.json>"}
{"tool":"production.retopology_optimize_region","mode":"verify","operation":"<proposal.json>"}
```

Preview no acredita QA visual: abrir los renders/candidato y registrar la revisión. Apply es otro
modo con su propio candidato; revisar también su resultado final. Revert conserva la copia y
devuelve la relación anterior. Un timeout incierto no autoriza otro apply automático ni una nueva
clave para duplicar la operación.

### Retopología — 11 tools

| Tool | Tipo | Entrada principal | Resultado y alcance |
|---|---|---|---|
| retopology_analyze | lectura | object_name, options | Topología base en metros: manifold/boundaries, degenerados, astillas, componentes, normales, valencia, IDs acotados y firma M5 |
| retopology_analyze_density | lectura | object_name, options | Densidad mundial/por rejilla/región, distribución de longitudes y señales de curvatura/prioridad; sin exigir densidad uniforme |
| retopology_priority_map | lectura | object_name, options.regions | Reglas compactas de prioridad/protección; pesos por vértice permanecen en Blender |
| retopology_budget | lectura | object_name, options | Target/min/max y asignaciones regionales absolute/percentage/density_weight; informa presupuestos imposibles |
| retopology_plan | lectura | object_name, options.strategy | Estrategia explícita, riesgos de UV/pesos/morphs/materiales, regiones y presupuesto; apply=false |
| retopology_create_candidate | operación | object_name, source_fingerprint, options | Checkpoint, nuevo objeto+malla, relación HIGH/LOW con integridad y dependencias declaradas |
| retopology_optimize_region | operación | relationship/mode/options/operation | GUIDED_DECIMATION o LOCAL_RETOPOLOGY; protección regional y gates de topología/fidelidad/presupuesto |
| retopology_project_to_source | operación | mismo ciclo | Proyección BVH acotada por distancia/normal/ambigüedad; fija regiones protegidas |
| retopology_compare_source | lectura | relationship, options | Desviación bidireccional media/mediana/P95/P99/máxima muestreada y ángulos de normales |
| retopology_compare_silhouette | lectura | relationship, options.views | Máscaras equivalentes FRONT/SIDE/THREE_QUARTER/BACK, IoU y distancias en metros con resolución declarada |
| retopology_validate | lectura | relationship, options | Topología LOW y preservación de regiones HIGH, incluida superficie interior protegida |

Las estrategias REMESH_AND_PROJECT/HYBRID se clasifican pero requieren trabajo manual; no hay
fallback implícito. Una reducción con shape keys se rechaza. No se reutilizan índices M4 después
de cambiar topología. Ver [retopología](retopology.md), [densidad](density-priority.md),
[calidad topológica](topology-quality.md) y [comparación](high-low-comparison.md).

### Transferencia y deformación — 6 tools

| Tool | Tipo | Entrada principal | Resultado y alcance |
|---|---|---|---|
| retopology_analyze_deformation_regions | operación | relationship, options | Regiones por grupos explícitos/deformación; estrés opcional con checkpoint y restauración |
| retopology_transfer_weights | operación | ciclo, options.groups | Correspondencia espacial baricéntrica y nuevo candidato; QA de sumas/influencias/leakage y poses explícitas opcionales |
| retopology_validate_weights | operación | relationship, options | Límites configurables de influencia/normalización, pares/lados explícitos y QA de estrés opcional |
| retopology_analyze_morph_transfer | lectura | relationship, options | Morphs, dependencias y capacidad exacta; otra topología devuelve manual/later-pipeline required |
| retopology_transfer_morphs | operación | ciclo | Sólo morphs estáticos ya preservados exactamente; no reconstruye shapes sobre topología distinta |
| retopology_transfer_attributes | operación | ciclo, options | Materiales, atributos nombrados POINT/CORNER/FACE soportados y claves explícitas de custom properties, con QA por atributo |

Correspondencia usa triángulos/loops/componentes, nunca igualdad de índices HIGH/LOW. Los lados
y poses no se adivinan por naming. Una cara LOW que cruza una frontera material incompatible se
rechaza. Leer [pesos](weight-transfer.md) y [morphs](morph-transfer.md) para tipos y límites.

### Normales, triangulación y tangentes — 4 tools

| Tool | Tipo | Entrada principal | Resultado y alcance |
|---|---|---|---|
| game_mesh_prepare_normals | operación | ciclo, options | preserve por defecto; flat/smooth explícitos; rebuild de custom normals requiere confirm_rebuild_custom_normals=true |
| game_mesh_triangulation_preview | operación | ciclo, options | Evaluación triangulada nueva y determinista; preserva fuente, pesos/morphs y superficie protegida |
| game_mesh_validate_triangulation | operación | relationship, options | Dos copias temporales independientes; conectividad, área tessellada, geometría, normales/morphs y estrés opcional |
| game_mesh_validate_tangents | lectura | relationship, options.uv_layer | Mesh.calc_tangents instalado sobre copia; finitud, longitud, ortogonalidad y signo por esquina |

OPENGL/DIRECTX declara la convención futura; no certifica un importer ni invoca Unreal. Ver
[triangulación y tangentes](tangents-triangulation.md).

### UV/UDIM — 7 tools

| Tool | Tipo | Entrada principal | Resultado y alcance |
|---|---|---|---|
| uv_analyze | lectura | relationship, options | Capas/islas/tiles, overlap/flips/degenerados, stretch, padding, ocupación y densidad resumidos |
| uv_validate | lectura | relationship, options | Gates configurables; un análisis incompleto no pasa |
| uv_analyze_texel_density | lectura | relationship, options.resolution/tile_resolutions | Texels por metro y distribución por tile/material; sin target universal |
| uv_generate | operación | ciclo, options | Seam plan explícito; ANGLE_BASED o SMART_PROJECT seleccionados; unwrap y packing sobre copia |
| udim_plan | lectura | relationship, options.assignments | Asignaciones semánticas explícitas por material_index o faces; rechaza faltantes/ambigüedad, apply=false |
| udim_apply_plan | operación | ciclo, options.assignments | Aplicación/packing por tile, padding/rotación declarados y preservación del resto de datos |
| uv_pack | operación | ciclo, options | Shelf pack determinista dentro de tiles existentes; búsqueda acotada, sin búsqueda indefinida de ocupación |

`uv_immutable` en la relación no se puede anular enviando false. Padding usa una medida conservadora
por cajas de isla; occupancy es área sumada, interpretada como packing sólo si no hay overlap.
Los límites de loops/pares y el coste indivisible del unwrap se describen en [UV/UDIM](uv-udim.md).

### Bake — 5 tools

| Tool | Tipo | Entrada principal | Resultado y alcance |
|---|---|---|---|
| bake_prepare_pairs | operación | relationship, options | Escribe par HIGH/LOW explícito inmutable con firmas, materiales, UV/UDIM, convención, cage/rayos y readiness |
| bake_analyze_ray_distance | lectura | relationship, options | Distancias front/rear, ángulos y miss witnesses muestreados; propone límites sin ampliarlos silenciosamente |
| bake_validate | lectura | relationship, options, pair opcional | READY/READY_WITH_WARNINGS/NOT_READY con gates concretos; pair opcional debe ser fresco |
| bake_generate_cage | operación | ciclo, options.extrusion_m | Nuevo cage con topología LOW, offset y gates de penetración/miss/intersección; LOW sigue siendo game mesh |
| bake_smoke_test | operación | pair, allow_cpu_fallback=false, options | NORMAL/AO reales, 32–256 px y 1–32 muestras; copias temporales, checkpoint, restauración y métricas de cobertura |

Smoke options sólo pueden variar resolución/muestras/margen/maps/subconjunto de tiles diagnósticos;
geometría, rayos y convención quedan congelados por el pair. BOUND_REST_MESH es un adaptador
explícito estrecho, no una autorización para cambiar rig/pose/transforms. Los warnings se conservan;
NOT_READY no se convierte en éxito. Ver [bake](bake-preparation.md).

### Gates finales y paquete local — 2 tools

| Tool | Tipo | Entrada principal | Resultado y alcance |
|---|---|---|---|
| game_mesh_validate | operación | relationship, options | Agrega topología/budget/superficie/silueta/pesos/deformación/morphs/atributos/normales/tris/tangentes/UV/UDIM/densidad/bake; comprueba firmas inicial/final |
| game_mesh_prepare_package | operación | relationship, bake_pair, triangulated_relationship=null, qa_report=null, options, dry_run=true | Plan o paquete neutral local validado; checkpoint antes de escribir, sin export ni aplicación externa |

El agregador acepta objetos de opciones por dominio: `retopology`, `weights`, `attributes`,
`game_mesh`, `uv`, `bake`. `require_bake_ready` y `require_deformation` son true por defecto. Su
desactivación sirve para analizar un estado intermedio, pero el paquete rechaza gates excluidos.
No hay scoring AAA/artístico. READY_WITH_WARNINGS se acepta sólo con passed=true y errors vacío.
Sin QA visual correspondiente, el paquete queda READY_NUMERIC_VISUAL_PENDING.

`dry_run=true` no escribe el paquete, pero ejecuta validaciones: puede evaluar poses de estrés y
crear sus checkpoints. No significa una operación sin efectos transitorios. Contrato de paquete,
estados y flujo completo: [game-mesh.md](game-mesh.md). Las mejoras propuestas permanecen en
[validación pública y límites](validation.md); no son funcionalidades disponibles.

## M6 — generación corporal, 35 tools verificadas

Nombres completos con prefijo `production.`. Firma común: `object_name`, `intent` o `brief`,
referencia `generation`, `options`, `apply=false` por defecto y `operation_id` del sobre.
`generation` es la ruta íntegra `generations/<reserva>/plan.json`, no una Action ni JSON editable.
Planificar crea metadata, no keyframes. Las etapas `apply=true` usan transacción M3, checkpoint y
Action nueva/duplicada; las fuentes/candidatas anteriores se conservan.

Sólo `animation_create_brief` se anuncia de lectura pura. Compatibilidad y análisis de candidata
pueden cambiar transitoriamente frame/Action/pose; renders/checkpoints escriben artefactos. Un
modo `apply=false` de una tool mutadora no convierte su contrato MCP completo en lectura.

| Tool | Unidad de trabajo y límites |
|---|---|
| animation_create_brief | Normaliza intención, perfil/tipo, FPS/duración/rango/root y procedencia; sin conexión ni Action nueva |
| animation_generation_compatibility | Combina mapping, jerarquía, geometría, constraints/IK/FK y capacidad; devuelve confidence/capabilities/reasons |
| animation_plan_poses | Poses principales, breakdowns, holds y contactos semánticos; sin keyframes |
| animation_generation_plan | Guarda generación íntegra con brief/poses/mapping/fingerprints/contexto; sin Action nueva |
| animation_generate_pose | Propone/materializa una pose semántica (`pose` o `pose_index`) en nueva Action; no habilita avance sin blocking completo |
| animation_validate_pose | Evalúa reach, contacto, penetration, stretch y métricas corporales de la candidata; restaura estado |
| animation_analyze_pose_silhouette | Métricas espaciales de silueta por vista; proxy que requiere inspección visual |
| animation_generate_blocking | Nuevas poses CONSTANT, checkpoint y QA; exige review temporal para avanzar |
| animation_validate_blocking | Muestrea candidata evaluada contra poses/contactos; no sustituye review temporal |
| animation_generate_breakdowns | Introduce breakdowns semánticos sobre copia propia después del blocking revisado |
| animation_analyze_timing | Duración/fases vigentes y trayectorias, con procedencia del brief |
| animation_adjust_timing | Phase fractions explícitas; nuevo plan/Action, FPS de escena intacto |
| animation_analyze_spacing | Velocidad/aceleración y cambios de dirección de trayectorias evaluadas |
| animation_analyze_arcs | Señala cambios de dirección de pies/manos/pelvis/cabeza/torso; no impone arcos circulares |
| animation_refine_arcs | Requiere `options.pose_plan` revisado explícitamente; sin reparación artística ciega |
| animation_analyze_body_mechanics | Trayectorias, soporte y asimetría declarada; no es simulación de COM físico |
| animation_refine_balance | Cambios semánticos explícitos mediante `options.pose_plan`; conserva rasgos de criatura |
| animation_generate_root_motion | Root explícito y contactos compatibles, IN_PLACE/ROOT_MOTION; preview/dry-run y QA |
| animation_generate_foot_contacts | Targets IK compatibles y anclas propuestas; contacto/sliding se vuelven a medir |
| animation_generate_effector_contact | Contacto temporal de mano FK mediante adaptador analítico acotado, alcance y superficie estática declarados |
| animation_spline | Suaviza canales corporales y conserva eventos/holds; root/targets location permanecen LINEAR |
| animation_polish_curves | Interpolación/handles y holds conservadores de la candidata; sin borrar keys agresivamente |
| animation_add_overlap | Retraso/magnitud en roles/cadenas/fases declarados; copia candidata y QA |
| animation_add_follow_through | Continuación/decay y duración explícitos en roles/cadenas/fases compatibles; copia y QA |
| animation_generate_locomotion | Receta LOCOMOTION; IN_PLACE acotado o ROOT_MOTION, fases/contactos y QA de loop |
| animation_generate_idle | Receta IDLE con pose/ciclo corporal propuestos; QA de continuidad y review |
| animation_generate_attack | Receta ATTACK con anticipación/commitment/impact/recovery y perfil pesado configurable |
| animation_generate_hit_reaction | Receta HIT_REACTION con dirección/absorción/desplazamiento/recuperación declarados |
| animation_generate_turn | Receta TURN con grados/dirección/root y transferencias de apoyo compatibles |
| animation_generate_start_stop | Receta START_STOP; transición de avance/salida con soporte y root medidos |
| animation_generate | Coordina plan/candidata/blocking y preview; retorna `blocking_review_required` antes de breakdowns/spline |
| animation_generation_render | Secuencia temporal completa PNG/APNG y reproductor local, tres vistas obligatorias y custom adicionales |
| animation_refine_generated | Métrica target/umbral/cambios explícitos, máximo tres intentos persistentes; requiere preview/review de la mejora |
| animation_accept_generated | `apply=false` registra REVIEWED; `apply=true` exige QA continua y visual ACCEPT vigente, luego activa la candidata |
| animation_reject_generated | Dry-run por defecto; rechazo explícito conserva Action/candidatas/evidencia y vuelve el lifecycle terminal |

El brief admite IDLE/ATTACK/LOCOMOTION/TURN/HIT_REACTION/START_STOP y seis perfiles corporales.
Duración 1–10 s, FPS 30/60 que deben coincidir con Blender; límites de muestras/keys/payload antes
de aplicar. Mapping sin certeza, NLA/drivers/controles ambiguos o dependencia fuera del adaptador
se rechazan sin alterar rig, pesos, transforms ni Actions fuente. ROOT/CONTACTS requieren la ruta
IK compatible; FK no recibe foot lock certificado. RUN y criaturas siguen los límites del
adaptador de bípedo, sin generación cuadrúpeda universal.

### Etapas, reviews y refinamiento

`animation_generate(..., apply=true)` primero crea blocking y su render. Una llamada posterior
sólo continúa después de REVIEWED/ACCEPT de ese blocking y de QA estructural. Las tools de etapa
directas también exigen el gate; POSE no lo elude. Cada etapa invalida render/review anteriores.
Cambios de curvas/rig/archivo/asset, geometría, pesos, modifiers, shape keys o materiales dejan
stale la generación. Para cambiar intención base, crear otra generación; no editar plan.json.

Review requerido: `reviewer`, `observations`, `findings`, `decision` (ACCEPT/REJECT/REFINE),
`artifact_reference` igual al output_dir vigente y `temporal_reviewed=true`. Un review de blocking
permite avance y no aceptación final. ACCEPTED/REJECTED no admiten nuevas etapas/refinamientos.
`animation_refine_generated` acepta targets medidos `sliding_m`, `pop_count`, `max_length_ratio`
y `minimum_improvement` positivo finito. Cambios/finding, before/after y artefactos permanecen
en el ledger; una mejora numérica espera revisión temporal. El contador incluye fallos/reversiones
y no se reinicia al deshacer; un ciclo pendiente bloquea otras etapas/render/lifecycle.

### Opciones de QA y render

Las tolerancias siguientes son defaults geométricos explícitos, ajustables en opciones de la
generación. No son umbrales universales de calidad artística; se registran junto a las métricas.

| Opción | Default | Gate |
|---|---:|---|
| sliding_tolerance_m | 0.01 m | Desplazamiento durante contacto medido |
| contact_height_m | 0.03 m | Heurística heredada de contacto |
| contact_speed_m_s | 0.25 m/s | Heurística heredada de contacto |
| ground_height_m | 0 m | Plano estático declarado por el caller |
| root_position_tolerance_m | 0.01 m | Delta root e IN_PLACE sin drift |
| root_angle_tolerance_deg | 1° | Rotación root respecto a intención |
| root_max_step_m | 0.25 m/frame | Salto continuo de raíz |
| root_vertical_tolerance_m | 0.05 m | Vertical fuera del rango semántico propuesto |
| loop_position_tolerance_m | 0.01 m | Pose relativa a root en extremos IDLE/LOCOMOTION |
| loop_angle_tolerance_deg | 1° | Orientación relativa a root en extremos |
| loop_velocity_tolerance_m_s | 0.10 m/s | Diferencia vectorial de velocidad en extremos |
| loop_angular_velocity_tolerance_deg_s | 10°/s | Diferencia vectorial de velocidad angular en extremos |

QA hereda análisis de animación, contactos/sliding, pops/stretch, Bézier, root, balance, calidad
de movimiento, deformación y quaternions; añade root/loop evaluado. Devuelve gates concretos:
errores de analizadores, deformación sin meshes/muestras, quaternion inválido/incompleto/sign flip,
root incoherente y loops discontinuos fallan. Blocking CONSTANT comprobado permite saltos
declarados de pose/root como advisories, sin eximir contacto/reach/stretch/deformación ni quaternion
inválido/sign flip. La aceptación final requiere una candidata con QA continua.

Render usa `width`/`height` (256 px por defecto, 16–1920), `quality=lightweight|scene`, `margin`,
`framing_bones` o `framing_meshes`, `framing_padding_m`, slots explícitos y `allow_cpu_fallback`.
FRONT/SIDE/THREE_QUARTER son obligatorias. `custom_views` admite hasta dos entradas
`{name: 'CUSTOM_...', direction: [x,y,z]}` únicas y finitas, con dirección no nula. Son adicionales:
no reemplazan la terna requerida. Cada vista incluye todos los frames del brief a FPS real.
Capacidad: 601 frames/vista, 1830 imágenes y 200 millones de píxeles; añadir vistas consume el
mismo presupuesto y puede exigir un clip/resolución menor. Se producen PNGs, APNG con timing y
`preview.html` para reproducir, pausar y avanzar frame a frame. Stills/montages no habilitan review.

Ver [generación](animation-generation.md), [brief](animation-brief.md), [poses](pose-planning.md),
[root](root-motion-generation.md), [lifecycle](generated-action-lifecycle.md),
[QA visual](animation-visual-qa.md) y [validación pública y límites](validation.md).

## M7 — animación facial y actuación, 46 tools verificadas

Nombres completos con prefijo `production.`. Firma común: `object_name`, `intent` (texto), `brief`, referencia
`facial` (ruta íntegra `facial/<reserva>/plan.json`), `options`, `apply=false` por defecto y `operation_id` del sobre
en las tools que escriben. El mapeo semántico se **mide** en Blender (probe de cada control); los nombres solo ajustan
la confianza. Cada candidata es una Action nueva (slots OBJECT + KEY) evaluada por NLA; la Action del cuerpo nunca se
escribe. Conjunto facial explícito con `options.facial_set = {armature, meshes, eyes, head_bone}`.

Clases MCP (auditadas por `test_m31_hints`):

- **Solo lectura (7)**: `facial_inspect`, `facial_create_brief`, `facial_prepare_speech`, `facial_generate_coarticulation`,
  `facial_mocap_analyze`, `facial_analyze_layer_conflicts`, `facial_analyze_curves`.
- **Transitorias y restauradas (15)**: hacen probe de controles, miden poses o hacen QA por frame
  (`scene_effects=transient_restored`): `facial_classify_rig`, `facial_analyze_shape_keys`, `facial_analyze_bones`,
  `facial_analyze_hybrid`, `facial_compatibility_report`, `facial_analyze_asymmetry`, `facial_analyze_eyes`,
  `facial_analyze_eyelids`, `facial_analyze_jaw`, `facial_analyze_lips`, `facial_map_visemes`,
  `facial_validate_expression`, `facial_validate_lipsync`, `facial_deformation_qa`, `facial_mocap_map`.
- **Escriben ledger, Actions candidatas o artefactos (24)**: el resto.

| Tool | Unidad de trabajo |
|---|---|
| facial_inspect | Conjunto facial, ojos, marco facial, huesos, shape keys, Action del cuerpo, NLA y avisos bloqueantes |
| facial_classify_rig | SHAPE_KEYS/BONES/HYBRID/CUSTOM/UNSUPPORTED/AMBIGUOUS con confianza, evidencia y limitaciones |
| facial_analyze_shape_keys / _bones / _hybrid | Salidas por control con confianza, evidencia y alternativas; drivers; decisiones híbridas |
| facial_compatibility_report | Capacidad a capacidad (SUPPORTED/LIMITED/UNSUPPORTED) con hallazgos de la calibración medida |
| facial_create_brief | Brief con procedencia USER_REQUIREMENT/INFERRED_DEFAULT/PROPOSAL |
| facial_plan_expression | Plan inmutable (sin Action) con dimensiones, objetivos y controles; `apply=true` crea la candidata |
| facial_generate_expression | Parte de expresión + asimetría declarada → candidata nueva |
| facial_validate_expression | Pico de la expresión medido en Blender: párpado, labios, intersecciones, caras, clamps |
| facial_analyze_asymmetry | Asimetría medida del rig (pares L/R, ojos) y declarada en la actuación |
| facial_analyze_eyes | Método por eje, límites, objetivos de aim, separación y distancia mínima de convergencia |
| facial_generate_gaze | Secuencia de objetivos (punto, cámara, ángulos, fuera de cámara, objeto), reparto con cabeza y seguimiento de párpado |
| facial_generate_saccades | Sacadas de fijación con semilla y acotadas por estilo de atención |
| facial_coordinate_gaze | Sin plan: reparto ojos/cabeza/cuello por cambio; con plan: regenera la mirada con ese reparto |
| facial_analyze_eyelids | Cierre al 100 % medido, penetración en el globo, salidas de párpado |
| facial_generate_blink / facial_plan_blinks | Parpadeos explícitos o planificados por contexto; sin plan, solo el calendario |
| facial_generate_lid_follow | Párpados que siguen el pitch de los ojos (requiere mirada) |
| facial_generate_brows | Eventos explícitos de ceja (raise/inner/outer/furrow/flash) |
| facial_analyze_jaw / facial_generate_jaw | Calibración de mandíbula; modos breathing/shout/roar/tension/pain validados en su pico |
| facial_analyze_lips | Modelo medido del hueco de labios, cruces y soporte de visemas |
| facial_map_visemes | 14 visemas resueltos a controles reales con su estado de soporte |
| facial_prepare_speech | Valida el timing; audio sin timing → `TIMING_INPUT_REQUIRED` |
| facial_generate_coarticulation | Compara coarticulación y pose a pose (tasa, cierres, timing) |
| facial_generate_lipsync | Capa SPEECH + énfasis; bloquea visemas que el rig no puede formar |
| facial_validate_lipsync | Validación por canal y hueco de labios medido en los bilabiales |
| facial_plan_microexpressions / _generate_microexpressions | Eventos breves, acotados y contextuales |
| facial_analyze_layer_conflicts | Partes por capa, conflictos resueltos, controles compartidos, clamps y propiedad |
| facial_generate | Todas las capas aplicables desde un brief; `apply=false` planifica, `true` crea la candidata con QA |
| facial_integrate_with_body | Huesos del cuerpo con y sin capa facial (cambio exigido: 0) y digest de la Action del cuerpo |
| facial_mocap_analyze / _map / _retarget / _cleanup | Canales genéricos → semántica → candidata cruda v1 → limpia v2 |
| facial_analyze_curves / facial_polish_curves | Picos/ruido/sobrepasos; pulido que nunca toca ventanas protegidas |
| facial_deformation_qa | QA por frame de la candidata o poses de estrés del rig medidas |
| facial_render_closeup | 4 vistas con cámara idéntica (85 mm), montajes 2×2 y comparación con otra versión |
| facial_render_preview | Secuencia temporal completa, APNG y reproductor normal/½/¼ |
| facial_review | Revisión visual: TECHNICAL FINDINGS frente a ARTISTIC OBSERVATIONS, sin puntuación |
| facial_refine | Máximo 3 ciclos medidos; sin mejora se revierte; con mejora, revisión obligatoria |
| facial_accept / facial_reject | Aceptar crea pistas NLA `M7_FACE` y no toca el cuerpo; rechazar conserva la Action y verifica la restauración |

Umbrales de QA (propuestas, ajustables en `options.qa.thresholds`): cierre de parpadeo ≥ 0,90, penetración del
párpado ≤ 0,2 mm, hueco bilabial ≤ 1 mm, cruce de labios ≥ −1 mm, 0 intersecciones nuevas, 0 caras
colapsadas/invertidas y error de mirada ≤ 1,5°. Renders: close-up 384 px por defecto (128–1024); preview 256 px, como
mucho 901 frames por vista, 1.810 imágenes y 200 Mpx.

Ver [rig](facial-rig.md), [semántica](facial-semantics.md), [expresiones](facial-expressions.md), [ojos](eye-animation.md),
[parpadeos](blinks.md), [mandíbula y labios](jaw-lips.md), [visemas](visemes.md), [lip-sync](lipsync.md),
[mocap](facial-mocap.md), [capas](facial-layers.md), [cuerpo + cara](body-face-integration.md), [QA](facial-qa.md) y
[validación pública y límites](validation.md).

## M8 — physics (0.8.0, fixtures calificadas)

El inventario registrado contiene236tools:201heredadas +35physics. Las31requeridas se conservan, más create_candidate/status/review/analyze_stability. Todas usan `object_name`, `physics` (ruta del plan), `intent`, `brief`, `options` y `apply=false`; las que escriben aceptan operation_id mediante el decorador común. El único reader sin cambios transitorios es create_brief;14readers evaluadores capturan/restauran RNA y no se anuncian como lectura pura. La calificación y sus límites se detallan en el [validación pública y límites](validation.md).

| Trabajo | Tools production.* |
| --- | --- |
| Inspección y plan | physics_inspect_character, physics_classify_secondary, physics_compatibility_report, physics_create_brief, physics_plan, physics_create_candidate, physics_status |
| Cloth/colliders | physics_prepare_colliders, physics_analyze_cloth, physics_plan_cloth_pins, physics_setup_cloth, physics_simulate_cloth, physics_analyze_penetration |
| Hair/huesos | physics_analyze_hair, physics_plan_hair, physics_generate_hair_secondary, physics_setup_spring_chain, physics_generate_secondary_bones |
| Accesorios/soft | physics_analyze_accessory, physics_generate_accessory_motion, physics_analyze_soft_region, physics_generate_soft_secondary |
| Medición | physics_verify_primary_integrity, physics_collision_report, physics_analyze_motion, physics_analyze_stability |
| Cache/bake | physics_cache_plan, physics_bake_cache, physics_bake_secondary_motion |
| QA/lifecycle | physics_render_preview, physics_compare_before_after, physics_review, physics_refine, physics_accept, physics_reject |

`options` del plan admite manifest, primary_objects, colliders, pin_group, chain, anchor_object/bone, proxy_strategy/max_proxy_deviation_m, segment_radius_m, max_vertices y thresholds. Son referencias explícitas, no nombres anatómicos inferidos. Chain declara armature y bones ordenados; meshes secundarias requieren una máscara de roots explícita. Cloth requiere pins y colliders; soft requiere región nombrada autorizada y colliders. Fuente con transform unitario, FPS30/60, clips1–10s y≤601muestras. No aplicar transforms para pasar preflight.

Settings/profile/qa son propuestas calibrables del brief. Native cloth usa sólo propiedades RNA inspeccionadas. Hair/cadenas usa un backend procedural limitado de huesos; accesorios guardan origen/anclaje; soft crea keys propias sobre una copia compatible, protege weight0 y mide el exterior. Groom/curves generales y anatomía muscular no están calificados. Las decisiones y limitaciones se detallan en [secondary-motion](secondary-motion.md) y documentos de dominio.

Los writers con apply=false devuelven propuesta; inspección/plan/review/render tienen su semántica propia documentada. La referencia physics carga plan/events firmados; no editar JSON ni sustituir opciones de una propuesta congelada. Refinement acepta settings/target/minimum_relative_improvement, máximo3, con reversion si no mejora y nueva revisión temporal. Render exige los tres views y conserva todo el rango; widths/heights64–512, máximo3606imágenes y200Mpx. La respuesta de bake resume desviación/cobertura; frames completos permanecen en events. Valores UNKNOWN no se convierten en cero.


El refinamiento sólo admite objetivos con dirección explícita de minimización (errores o magnitudes de desplazamiento, velocidad, aceleración, ángulo, anclaje, región y penetración). Área mínima, volumen, crecimiento de bbox, stretch y objetivos desconocidos se rechazan antes de consumir ciclos o crear copias/checkpoints. Métricas bool/no finitas tampoco se aceptan. La captura de preview incluye el candidato y su representación bakeada, además de los originales; se verifica el estado RNA completo restaurado.

## M9 — materiales y fundamentos PBR, 38 tools verificadas en 0.9.0

Los nombres completos llevan `production.`. Firma común: `object_name`, `material_name`, `material` (ruta íntegra al plan), `definition`, `profile`, `options`, `apply=false`; el sobre común añade `operation_id` a las operaciones pertinentes. `material` identifica el plan, no el nombre del datablock. Una propuesta con `apply=false` no construye ni instala un shader en la fuente. Para ejecutar construcción, render o lifecycle se usa el plan vigente y `apply=true`.

| Unidad de trabajo | Tools |
| --- | --- |
| Inspección/compatibilidad | `material_inspect`, `material_analyze_nodes`, `material_compatibility_report` |
| Workflow PBR | `material_analyze_pbr`, `material_validate_pbr`, `material_create_pbr` |
| Imágenes/canales | `texture_inspect`, `texture_validate_channels`, `texture_validate_colorspace`, `texture_dependency_report` |
| Normal/altura/tangentes | `material_validate_normals`, `material_validate_height`, `material_validate_tangent_workflow` |
| Features Principled | `material_configure_sss`, `material_configure_transmission`, `material_configure_opacity`, `material_configure_emission` |
| Perfiles | `material_create_profile`, `material_apply_profile`, `material_validate_profile` |
| UV/UDIM | `material_validate_uv`, `material_validate_udim`, `material_analyze_texture_sets` |
| Candidatas | `material_create_candidate`, `material_apply_candidate`, `material_accept`, `material_reject` |
| Shader graph | `material_build_shader`, `material_validate_graph` |
| QA técnica | `material_quality_report` |
| QA visual | `material_render_preview`, `material_render_multilighting`, `material_compare_before_after` |
| Rendimiento | `material_performance_report` |
| Handoff neutral | `material_create_handoff` |
| Estado/revisión/refinamiento | `material_status`, `material_review`, `material_refine` |

Las 35 tools requeridas se acompañan de tres herramientas de lifecycle necesarias: 21 readers y 17 writers. Los readers devuelven datos consultivos; los writers crean candidatas/checkpoints/artefactos. El render restaura la escena, pero persiste artefactos: su contrato MCP es writer, con `readOnlyHint=false` e `idempotentHint=false`.

### Definiciones, inspección y capacidades

`definition` declara `category`, `workflow='METALLIC_ROUGHNESS'`, `parameters`, `channels`, `normal_convention`, `height_mode`, `renderer` y perfil opcional. Un binding es `{image: nombre_existente, component: COLOR|R|G|B|A, colorspace: nombre}`. Canales: `base_color`, `metallic`, `roughness`, `normal`, `ao`, `height`, `opacity`, `emission`. Color y emisión requieren interpretación de color; datos requieren interpretación lineal/Non-Color según contrato. La convención de normales es `OPENGL`, `DIRECTX` o `UNKNOWN`; las dos últimas no reciben conversión automática silenciosa.

La validación combina wiring real, sockets, imágenes, valores y contexto. Metallic fraccional está permitido bajo el modelo declarado. AO permanece identificado independientemente; no se multiplica por Base Color por defecto. Normal Map, bump y displacement son rutas distintas. La configuración de displacement de Cycles no certifica teselación ni deformación real; `DISPLACEMENT_GEOMETRY_UNCERTIFIED` bloquea aceptar como comportamiento demostrado.

`material_inspect`/`material_analyze_nodes` admiten `options.detail=summary|nodes|images|assignments`, `offset` y `limit` de 1 a 100. Capacidad interna: 256 nodos, 1.024 links, 4.096 sockets, grupos hasta 8 niveles y 100 imágenes. Se entregan estadísticas y referencias, no píxeles o geometría masiva. La QA distingue ERROR/WARNING/INFO/UNKNOWN y no inventa un score PBR.

Los perfiles son configuraciones iniciales, no prueba de composición. Piel humana/criatura, tela, cuero, metal/pintura, goma, hueso, hielo/nieve y wetness usan parámetros revisables; no generan texturas ni geometría. UV/UDIM permite materiales múltiples y un material que abarca varios tiles, sin reescribir UVs ni alterar readiness de M5.

### Lifecycle y revisión

`PLANNED → CANDIDATE → REVIEWED → ACCEPTED/REJECTED`. El plan y sus dependencias son inmutables; no editar su JSON. `material_create_candidate` crea Object/Mesh/Material propios con checkpoint. Cada cambio de shader mantiene una versión separada e invalida el render/review anterior. `material_apply_candidate` verifica la asignación en la copia propia: nunca reemplaza slots de la fuente. Accept/reject conservan fuente y evidencia; estados terminales no se reabren.

Review obligatorio: `reviewer`, `observations`, `findings` (lista), `decision=ACCEPT|REJECT|REFINE`, `artifact_reference` igual al `output_dir` actual y `lights_reviewed` con NEUTRAL/SOFT/HARD/GRAZING. La revisión debe proceder de imágenes realmente inspeccionadas. Accept vuelve a comprobar QA y frescura. `material_refine` requiere review, cambio explícito de definición/perfil y `options.reason`; consume uno de tres intentos persistentes antes de crear otra versión. No garantiza mejora artística por generar nodos.

### Renders y handoff

Las tres tools de render producen la misma unidad de QA con cuatro iluminaciones pareadas. Opciones: `width`, `height` (64–512; default 192), `views` (FRONT/THREE_QUARTER/CLOSE_UP; default 3/4+close-up), `samples` (1–128; default 32), `engine=BLENDER_EEVEE|CYCLES`, `backend`, `require_gpu` y `allow_cpu_fallback`. GPU explícita no disponible produce rechazo; CPU debe autorizarse y se reporta como CPU. Workbench no se acepta para PBR.

El estudio compara material fuente/candidato sobre la misma copia raw estática, aplicando el shader analizado a todos sus polígonos. No reproduce la composición original de varios slots ni la deformación/animación final. Rechaza simulación nativa activa antes de crear el estudio; nunca desactiva el modifier original para pasar. Devuelve PNG locales, SHA256, backend observado, iluminación, tiempos y estado restaurado; los límites físicos de EEVEE permanecen explícitos.

`material_create_handoff` produce el contrato neutral `production.texture_handoff.v2`. `options` admite `bake_pair` M5, `asset_version`, `expected_maps`, `resolution_targets` y `mesh_maps_expected`. Sin bake pair verificado el estado es UNKNOWN; `NOT_READY` se conserva. Los tamaños son objetivos de intercambio, no autorización para generar imágenes. No se abre Painter ni se crean stacks/masks o materiales Unreal.

Referencias: [inspección](material-inspection.md), [PBR](pbr-workflows.md), [color](texture-color-management.md), [normal/tangentes](normal-tangent-workflow.md), [SSS/transmisión/opacity](sss-transmission-opacity.md), [perfiles](material-profiles.md), [piel](skin-organic-materials.md), [hielo/nieve/wetness](ice-snow-wetness.md), [UDIM](material-udim.md), [QA/backend/rendimiento](material-qa.md) y [handoff](substance-handoff.md).
