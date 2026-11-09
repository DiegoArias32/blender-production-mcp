# M10 — Sincronización multi-actor: clips NLA, cara M7 y secundario M8

Área D de M10. Código: `src/blender_mcp/production/cinematic/performance.py`. Tools:
`cinematic_sync_actions`, `cinematic_integrate_facial`, `cinematic_integrate_secondary`, y los proveedores del informe
de calidad `sync_report`, `facial_report`, `secondary_report` (§4). Contrato:
[arquitectura cinematográfica](cinematic-architecture.md) §1 y §5. Colocación: [actor-staging.md](actor-staging.md).

Reglas comunes: toda mutación pasa por `store.mutate` (rol del asset → frescura → checkpoint → payload →
evento); cada payload valida todo antes de mutar y, si algo falla a mitad, deshace sus propias filas antes de
relanzar el error; las Actions fuente, la Action facial M7 y los objetos M8 **nunca** se escriben (digest antes y
después dentro del payload). Las lecturas son siempre valores evaluados en la escena candidata. `apply=false`
ejecuta las mismas validaciones sin cambios. Todo payload que muestrea tiempo captura y restaura los actores del plan
y los objetos propios de `_CIN`. Las respuestas llevan `errors` (bloquean `status=error`) y `warnings` (nunca
bloquean).

## 1. Clips corporales (`cinematic_sync_actions`)

```
options = {clips: [{actor_id, action, slot?, start_frame (entero), action_frame_start?, action_frame_end?,
                    scale = 1, repeat = 1, extrapolation = NOTHING | HOLD | HOLD_FORWARD, retime = false,
                    carry_root = false}],
           root_bones?: {actor_id: hueso}}
```

### Continuidad de raíz entre clips (`carry_root`)

Con una pista por actor, cada clip arranca con la raíz que tiene su propia Action: un clip en el sitio (Stare, React,
Idle) que sigue a uno con desplazamiento o giro devolvía al actor a su posición u orientación de origen. **Medido** en
la demo A (`bpm10-e2e-4`): la raíz saltó **4,0 m** entre los frames 221 y 226 (paso local 0,025 m) y la criatura
reapareció en la boca de la cueva tras el corte. El informe técnico pasó, porque un salto en un corte sólo es INFO; lo
detectó la revisión visual.

`carry_root = true` en un clip lo hace empezar donde el anterior dejó la raíz, en XY y en giro sobre +Z (la altura no
se toca). Por frontera marcada se calcula una corrección de mundo G = T(shift) @ Rz(yaw) entre el último frame del clip
anterior y el primero en que el nuevo está activo (los mismos frames que la comprobación de fronteras). Las
correcciones se acumulan. Para un actor sin padre, mundo = T(L + dloc) @ R_delta @ R @ S, así que G @ mundo vuelve a
ser un delta: `dloc' = t + Rz(L + dloc) − L` y `R_delta' = Rz @ R_delta`. Esos valores se insertan en CONSTANT en una
Action propia `M10C_<g8>_v<n>_ROOT_<actor_id[:8]>`, en una pista propia del mismo nombre situada encima de la corporal.
La primera clave conserva el staging; filas `delta` y `nla_track` previas. Los clips corporales nunca animan `delta_*`
(se rechazan), así que ninguna Action comparte canal con la pista de raíz y nada vuelve a su valor por defecto entre
strips. Un clip sin `carry_root` conserva la corrección acumulada hasta él.

El payload **mide** después cada frontera con la pista activa (`carry.boundaries[*].position_error_m` y
`yaw_error_deg`) y el handler marca `CARRY_ROOT_MISMATCH` por encima de 1e-4 m o 0,01° (PROPUESTA). `frame_set` escribe
el delta evaluado en la propiedad original, igual que las transformaciones animadas. Por eso la huella de la fuente
toma los deltas animados del digest de animación, y al re-sincronizar la base se lee de la Action propia
(`m10_base_delta_*`) antes de sustituir la pista. El informe de calidad comprueba que la pista sigue presente y sin
silenciar (`CARRY_TRACK_CHANGED`).

Rechazos: primer clip del actor con `carry_root` (`CARRY_ROOT_FIRST_CLIP`, también en el host); actor con padre,
constraints o rotación eje-ángulo, o clips que animan la transformación del objeto (`CARRY_ROOT_UNSUPPORTED`); otra
fuente que anime o conduzca `delta_*` (`ACTOR_DELTA_ANIMATED`); actor con secundario M8 integrado, simulado contra el
movimiento sin corregir (`CARRY_ROOT_SECONDARY_UNSUPPORTED`). Después de un `carry_root`, `cinematic_stage_actors`
rechaza el actor con `ACTOR_DELTA_ANIMATED`: hay que hacer el staging antes de sincronizar.

Por actor: una pista propia `M10C_<g8>_v<n>_BODY_<actor_id[:8]>` (`cm_new_track` + fila `nla_track`), la Action activa
desasignada (fila `animdata`; en REPLACE taparía al NLA — medido), un strip por clip con `blend_type=REPLACE`, slot
explícito o resuelto (`OB<objeto>`, o el único slot de objeto) y comprobado contra `action_suitable_slots`, rango de
Action explícito y colocación con `frame_start_ui` (los setters crudos dan un mapeo incoherente — medido). Volver a
sincronizar un actor **sustituye** su pista propia sin filas nuevas (la primera fila `animdata` sigue guardando el
estado original).

**Hueso raíz** (fronteras y huecos): `options.root_bones` primero; si no, `bones.root` del registro del actor; si
tampoco, todos los huesos sin padre del rig (leídos de la armature, nunca adivinados por el nombre); un actor sin
armature usa el origen del objeto. Se guarda en `state.actors[id].root_bone` y `root_source`
(`OPTIONS | REGISTRY | NONE`).

Rechazos (todos medidos en la probe sin cambiar candidata ni ledger):

| Caso | Código |
|---|---|
| actor en modo tweak NLA | `NLA_TWEAK_MODE` |
| driver sobre un canal que anima el clip | `NLA_DRIVER_CONFLICT` |
| strip no CLIP en una pista existente | `NLA_STRIP_UNSUPPORTED` |
| dos clips del mismo actor solapados (un actor toca un clip a la vez) | `CLIP_OVERLAP` |
| `scale ≠ 1` sin `retime=true` | `CLIP_RETIME_REQUIRED` |
| Action con varios slots de objeto y ninguno del actor | `SLOT_AMBIGUOUS` |
| clip que anima `location` de objeto en un actor girado por staging | `ROOT_MOTION_AMBIGUOUS` |
| clip que anima `delta_*` | `ACTOR_DELTA_ANIMATED` |
| actor con secundario M8 integrado sin un clip de la Action primaria con `start − action_frame_start = Δ`, escala 1, que cubra la ventana pedida en su primer ciclo | `SECONDARY_OFFSET_MISMATCH` |

Lo que **mide** y devuelve por clip: mapeo frame de escena → frame de Action en hasta 8 frames enteros
(`a = afs + (f − start)/scale`, con el ciclo de `repeat`; el frame que cierra un ciclo abre el siguiente salvo el final
del strip — medido), error máximo por canal frente a `fcurve.evaluate(a)`, duración nueva (`retime`), canales sin
resolver y propiedades personalizadas animadas. Por actor: **huecos** entre clips y con qué extrapolación se
rellenan, **discontinuidad de raíz** en cada frontera (salto frente al paso local de los frames vecinos →
`ROOT_DISCONTINUITY_REVIEW`, aviso), **canales solapados** con otras pistas (y cuál gana: la de arriba) y el
**reinicio de raíz en huecos NLA** (abajo). Se guardan en el estado los clips, `clip_findings` y
`source_action_digests` (digest de cada Action fuente antes de sincronizar, para que el informe de calidad la
compare otra vez).

### Reinicio de raíz en huecos NLA (`ROOT_RESET_IN_NLA_GAP`, aviso)

Medido por el área de análisis en Blender 5.2.1: en un frame que ningún strip conduce, los canales animados por NLA
vuelven a su **valor por defecto** (su actor saltó 2,0 m al origen). Reglas de extrapolación (medidas, las mismas que
`analysis.coverage_gaps`): `NOTHING` deja los frames sin conducir; `HOLD` y `HOLD_FORWARD` sostienen hacia delante;
sólo el **primer** strip de la pista sostiene hacia atrás (`HOLD`). Dentro de la línea de tiempo de la candidata se
buscan tres tipos de hueco: antes del primer clip (`BEFORE_FIRST`), entre clips tras un `NOTHING` (`BETWEEN`) y
después del último si es `NOTHING` (`AFTER_LAST`). Si los clips del actor animan transformaciones del objeto o del
hueso raíz, cada hueco es un aviso con el **salto medido** de la raíz evaluada entre el último frame conducido y el
primero sin conducir. Nunca es un error: la decisión (rellenar con un clip o usar `HOLD`/`HOLD_FORWARD`) es del
director.

### Hechos medidos

Sonda de exploración propia (`informe local privado; véase validation.md`):
- `nla_tracks.new()` añade la pista **arriba**; en REPLACE gana la de arriba (105,0 = Action superior en el frame 6).
  `new(prev=t)` inserta justo encima de `t`.
- Strips contiguos con `start == fin anterior` se admiten ([1,11] y [11,21]).
- `AnimData.use_tweak_mode` es asignable desde Python (sirvió para la fixture negativa).

Probe `tests/blender/m10_performance_probe.py` (88/88, todos los handlers M10 cargados; informe en
`informe local privado; véase validation.md`, cifras iguales en `-8`, `-9` y `-10`):
- `A_Walk` en 11 (offset 10): strip [11, 70]; `A_Idle` en 75 con `scale=1,5, retime=true`: strip [75; 118,5],
  duración **43,5** frames (29 × 1,5); `B_Wave` en 11: [11, 70]. Error máximo de canal: **0,0 / 9,3e−10 / 0,0**
  con 30, 20 y 30 comparaciones.
- Lectura independiente: raíz de A en el frame 40 = 1,4745762 (esperado 30/59·3 = 1,4745763), en el 70 = 3,0, en el
  72 (hueco, NOTHING) = 0,0.
- Hueco 70 → 75 detectado; frontera Walk→Idle con salto de raíz **3,0 m** frente a un paso vecino de 0,051 m →
  `ROOT_DISCONTINUITY_REVIEW`.
- `ROOT_RESET_IN_NLA_GAP` (línea de tiempo 1–120): A [1, 10] salto **0,0 m** (el clip empieza en la pose por
  defecto), A [71, 74] **3,0 m**, B [1, 10] 0,0 m, B [71, 120] **0,6 m**; ninguno tras `A_Idle` (`HOLD_FORWARD`).
  Lectura independiente de la cabeza del hueso raíz de A: (0; −1; 3,0) en el 70 y (0; −1; 0,0) en el 71 = 3,0 m, la
  misma cifra que el aviso.
- Hueso raíz: A desde `options.root_bones` (`OPTIONS`), B desde el registro (`REGISTRY`, `root`).
- Digest de todas las Actions fuente idéntico antes y después; tras `cinematic_reject`, las 9 Actions de la fixture
  idénticas, `A_Walk` vuelve a ser la Action activa y no queda ninguna pista M10.

## 2. Cara M7 aceptada (`cinematic_integrate_facial`)

```
options = {actor_id, facial: <plan.json M7>, start_frame (entero), extrapolation = NOTHING | HOLD | HOLD_FORWARD,
           events?: [{frame (frame de escena, entero), label?}]}   # como máximo 64
```

Gate (antes de mutar): `facial.service.load` (ledger firmado), `state == ACCEPTED` sin refinamiento pendiente ni
operación incierta, mismo fps que la candidata (`fps_base` 1), armature del plan M7 = objeto del actor,
`facial.service.scene_status`: `candidate_fingerprint` igual al aceptado y la pista `M7_FACE <cand>` presente. Un
error de Blender en `scene_status` se trata como no vigente (`FACIAL_STALE`).

- **Desplazamiento 0** y la misma extrapolación que el strip M7 → se **reutiliza** la pista M7 (sin cambios, sin filas).
- **Desplazamiento ≠ 0** (o otra extrapolación) → pista propia `M10C_<g8>_v<n>_FACE_<actor[:8]>` en la armature y en
  cada Key del plan, con el mismo slot que el strip M7, `action_frame_start/end` explícitos (un strip que no pudo
  autoasignar su slot nace de 1 frame — medido en M7) y la pista M7 silenciada (fila `nla_mute`). Nunca se nombra una
  pista `M7_FACE*`.
- Propiedad de cabeza y cuello (`head_policy` del plan M7) frente a la Action **sincronizada**: `BODY_OWNS` rechaza
  una Action facial que anime cabeza/cuello; `FACE_OWNS` rechaza un cuerpo (clips M10 o Action activa) que los anime
  (`HEAD_OWNERSHIP_CONFLICT`). `BLENDED` → `FACIAL_HEAD_POLICY_UNSUPPORTED`: necesita la Action del cuerpo activa en
  COMBINE, que la sincronización NLA retira (límite declarado).
- Una pista propia queda **encima** del cuerpo; si cara y cuerpo animan los mismos canales se rechaza
  (`FACIAL_BODY_CHANNEL_OVERLAP`) porque invertiría la regla medida de M7 (el cuerpo gana en canales solapados).
  Reutilizando la pista M7 (debajo del cuerpo) la regla se conserva y el solape sólo se informa.
- **Eventos faciales**: los frames importantes de la actuación (pico de una frase, un grito). Deben caer dentro de la
  ventana facial `[start_frame, start_frame + duración del strip M7]` (`FACIAL_EVENT_OUTSIDE_WINDOW`, comprobado en
  Blender antes de mutar). Se guardan en `state.facial_events = [{actor_id, frame, label}]`, que el análisis de
  visibilidad lee para `FACE_HIDDEN_AT_EVENT`; integrar de nuevo la cara sustituye los eventos de ese actor. Sin
  eventos: aviso `FACIAL_EVENTS_NOT_DECLARED` (la visibilidad no puede comprobar la cara en sus momentos clave).
- El estado guarda además la huella de la Action facial, las pistas M7, los huesos cabeza/cuello y la ventana, para
  que `facial_report` vuelva a medir sin depender de la respuesta original.

### Hechos medidos (probe, aceptación M7 **SYNTHETIC_ACCEPTANCE_FOR_MECHANISM_TEST**)

La aceptación M7 de la probe es un estado de ledger mínimo escrito sólo en el storage temporal de la probe para
probar el mecanismo; **no es evidencia de revisión**.
- Desplazamiento 0 con HOLD → `REUSED_M7_TRACK`, 0 filas `touched`, mapeo exacto, aviso `FACIAL_EVENTS_NOT_DECLARED`.
- `start_frame=41` (desplazamiento 40) con el evento `{frame: 56, label: 'jaw peak'}`: pistas propias en armature y
  Key, strips [41, 71] (ventana [41, 71]), pistas M7 silenciadas, error 0,0 en 10 comparaciones por dueño; digest de la
  Action facial igual; `state.facial_events = [{actor_id: A, frame: 56, label: 'jaw peak'}]`. Lectura independiente:
  `jawOpen` = 1,0 en el frame 56 (= frame 16 M7), 0,0 en el 16 (pico M7 silenciado); `jaw` = 0,4 en el 56.
- Evento en el frame 90 (fuera de [41, 71]) → `FACIAL_EVENT_OUTSIDE_WINDOW`; plan M7 a 24 fps en una candidata a 30
  → `FACIAL_FPS_MISMATCH`; ambos sin cambiar candidata ni ledger.
- La lectura de shape keys debe ser **evaluada** (`Key.evaluated_get(depsgraph)`): 0,5 evaluado frente a 0,0 en el
  original en una escena que no es la de la ventana (sonda de exploración).

## 3. Secundario M8 aceptado

```
options = {actor_id, physics: <plan.json M8>, offset_frames: Δ (entero), window?: [inicio, fin]}
```

**Exclusiones al crear la candidata.** Las colecciones de la escena fuente se enlazan **enteras** en `_SRC`
(contrato §1), así que ya no se puede desenlazar un objeto de `_SRC` después. La malla fuente del secundario y los
objetos M8 (candidata y `<cand>_Baked`) van en `exclude_objects` de `cinematic_create_candidate`. Si siguen en la
escena candidata: `CINEMATIC_SECONDARY_SOURCE_NOT_EXCLUDED` (fuente) o `SECONDARY_M8_OBJECT_IN_CANDIDATE` (objetos
M8), antes de mutar y sin filas. El actor y la fuente del secundario deben vivir en **colecciones distintas**: excluir
la fuente espeja su colección con una colección propia donde el resto de objetos se enlaza uno a uno; si el actor
estuviera en ella, cambiaría su `users_collection` y la aceptación M8 quedaría obsoleta (deducido del mecanismo medido
abajo; esta configuración concreta no se ha probado).

Gate en Δ = 0, **antes de cualquier cambio de M10 sobre el actor**: `physics.service.load`, `ACCEPTED` sin
refinamiento pendiente ni operación incierta, representación `PC2` (`SECONDARY_ACTION` →
`SECONDARY_BACKEND_UNSUPPORTED`: su registro sólo guarda digests y desplazarlo exigiría la copia del rig que M10 no
hace), `bake_deviation.passed`, mismo fps, `physics.service.fresh` y `cache.verify(cache, cache_dependencies)`; un
`RuntimeError` del bridge cuenta como no vigente (`SECONDARY_NOT_CURRENT`). La ventana pedida (por defecto toda la
línea de tiempo) debe caber en `[cache_start + Δ, cache_end + Δ]` (`SECONDARY_WINDOW_OUTSIDE_CACHE`). Los objetos
primarios del plan M8 deben incluir el actor.

**Orden obligatorio**: integrar el secundario **antes** de colocar o sincronizar ese actor. El gate de M8 vuelve a
evaluar la primaria tal como M8 la firmó; los deltas y el NLA de M10 sobre el actor compartido la vuelven obsoleta por
construcción. Si el actor ya tiene staging, clips o filas `touched` → `SECONDARY_GATE_ORDER`. Después,
`sync_actions` **exige** el desplazamiento Δ (ver tabla de rechazos) y vuelve a medir la equivalencia en mundo.

Mutación: copia propia del objeto `<cand>_Baked` (`obj.copy()`, comparte la malla de M8, que nunca se escribe) en
`_CIN`, sin las marcas `production_*` de M8 (si no, la integridad M8 la adoptaría), marcada M10 y con
`hide_render=False` (M8 crea el `_Baked` oculto en render y nunca lo activa: lo que se renderiza es la copia);
`MESH_CACHE.frame_start = cache_start + Δ`. No se desenlaza nada ni se escribe ninguna fila `touched`. Medición: en
cada frame entero del clip cacheado, coordenadas locales evaluadas de **todos** los vértices de la copia en `f + Δ`
(escena candidata) frente al `_Baked` M8 en `f` (escena fuente); más el rango de movimiento de la copia (si no se
mueve, igualdad no prueba nada: `SECONDARY_PLAYBACK_UNPROVEN`). La equivalencia en mundo se mide cuando no depende
del actor (Δ = 0 o copia sin padre); si la copia cuelga del actor queda `PENDING_PRIMARY_OFFSET` con una referencia
compacta (16 frames × 4 vértices + matriz del actor) y `sync_actions` la mide como `A_ahora(f+Δ) · A_ref(f)⁻¹ · ref(f)`
(también tras recolocar).

Fila guardada en `state.actors[id].secondary`: `{object: <copia M10 con MESH_CACHE>, window: [cache_start + Δ,
cache_end + Δ] (frames de escena realmente cacheados, los que leen el análisis de movimiento y la línea de tiempo),
requested_window, copy, delta, baked, baked_object_digest, cache {sha256, frame_start, frame_end,
dependencies_fingerprint}, reference, indices, local_equivalence, world_equivalence, …}`.

### Hechos medidos

- **Orden real resuelto (medido):** la aceptación M8 se firma **antes** de crear cualquier candidata. Con las
  colecciones enlazadas enteras (`Accessories`, `Cast`, `Extras`, `Props`; sólo `Ground`, que está en la colección
  maestra, se enlaza suelto), `integrity.CANDIDATE_STATUS` de M8 tras crear la candidata tiene `raw_fingerprint`,
  `context_fingerprint` **y ownership** iguales a los firmados, y `physics.service.fresh` sigue en `FRESH`. Antes de
  este cambio de lifecycle, enlazar el actor suelto en `_SRC` cambiaba `ownership[ActorB].collections` y daba
  `PHYSICS_STALE_CANDIDATE` (medido en `bpm10-performance-6`). Tras `cinematic_reject` la aceptación sigue vigente.
- Candidata exploratoria sin excluir la fuente: `CINEMATIC_SECONDARY_SOURCE_NOT_EXCLUDED`, sin cambiar candidata ni
  ledger.
- Con la fuente excluida (aceptación sintética **SYNTHETIC_ACCEPTANCE_FOR_MECHANISM_TEST**, firmada en el orden real):
  copia con `MESH_CACHE.frame_start = 11` (Δ = 10, clip 1–60), 0 filas `touched`, `hide_render` de la copia `False` y
  del `_Baked` M8 `True`; fila de estado con `window = [11, 70]` y `requested_window = [15, 65]`; equivalencia local
  **0,0 m** en 60 frames × 8 vértices con 0,083 m de movimiento real; lectura independiente del vértice 6 en 4 frames
  idéntica a la muestra PC2 escrita por la probe (diferencia 0,0); objeto `_Baked` M8 sin cambios; tras escalonar B
  (+0,5 m) y sincronizar `B_Wave` en 11, equivalencia en mundo **4,8e−7 m** (16 frames × 4 vértices). Fuera del
  rango cacheado (frame `inicio + Δ − 1`) la copia repite el primer frame (diferencia 0,0).
- Coste en la fixture: 0,68–1,10 s (gate M8 + copia + 60 frames en dos escenas).

## 4. Proveedores del informe de calidad (`sync_report`, `facial_report`, `secondary_report`)

`cinematic_quality_report` (área G) los busca por nombre en este módulo:

```
sync_report(ctx, plan, state, options=None)       # sección 'sync'
facial_report(ctx, plan, state, options=None)     # sección 'facial_timing'
secondary_report(ctx, plan, state, options=None)  # sección 'secondary'
→ {schema, kind, status: PASS | FAIL | NOT_APPLICABLE, passed: bool | None, findings: [{code, severity, …}],
   findings_by_code, metrics: {escalares}, state_restored: bool, restoration: [...], details: [...],
   thresholds, threshold_source: 'PROPOSAL', limitations, visual_verification: 'required'}
```

- **Vuelven a medir la candidata actual**: nunca releen lo guardado al aplicar; lo guardado sólo sirve como referencia
  de comparación. `NOT_APPLICABLE` (`passed=None`) si no hay nada integrado de ese tipo, sin llamar a Blender.
- Antes de muestrear: `store.fresh` y, dentro del payload, huellas de fuente y candidata iguales a las registradas
  **antes del primer `frame_set`** (`CINEMATIC_STALE_*`, no se muestrea nada). Una llamada al bridge por actor o por
  copia (payload acotado). Pueden hacer `frame_set` (los llama una tool writer) y restauran frame, cámara, canales,
  shape keys y huellas; `state_restored` compara un digest de todo eso antes y después, y el frame/cámara de la
  escena fuente.
- Veredicto: `FAIL` si hay algún hallazgo `ERROR` o no se restauró; los `WARNING` nunca suspenden. Sin puntuaciones.

| Proveedor | Qué mide ahora | Errores | Avisos |
|---|---|---|---|
| `sync_report` | reconstruye los clips desde los strips de la pista BODY registrada; mapeo en frames muestreados; fronteras; huecos y reinicio de raíz; solapes; digest de las Actions fuente | `SYNC_TRACK_MISSING`, `CLIP_PLACEMENT_CHANGED`, `CLIP_MAPPING_FAILED`, `CLIP_OVERLAP`, `SYNC_TRACK_MUTED`, `SOURCE_ACTION_CHANGED`, `NLA_STRIP_UNSUPPORTED` | `ROOT_RESET_IN_NLA_GAP`, `ROOT_DISCONTINUITY_REVIEW`, `SOURCE_ACTION_BASELINE_MISSING` |
| `facial_report` | digest de la Action facial, pista que la reproduce (M7 reutilizada o propia), mute de la pista M7 tal como se registró, mapeo de la tira, política cabeza/cuello frente al cuerpo actual, eventos dentro de la ventana; ledger M7 todavía `ACCEPTED` | `FACIAL_ACTION_CHANGED`, `M7_TRACK_STATE_CHANGED`, `FACE_TRACK_MISSING`, `FACE_STRIP_MOVED`, `FACIAL_MAPPING_FAILED`, `HEAD_OWNERSHIP_CONFLICT`, `FACIAL_BODY_CHANNEL_OVERLAP`, `FACIAL_EVENT_OUTSIDE_WINDOW`, `FACIAL_NOT_ACCEPTED`/`FACIAL_STALE` | `FACIAL_EVENTS_NOT_DECLARED` |
| `secondary_report` | equivalencia local de la copia en `f + Δ` frente al `_Baked` M8 en `f` (todos los vértices) en los 16 frames de referencia, rango de movimiento, equivalencia en mundo llevada por el actor, offset y fichero del `MESH_CACHE`, `hide_render`, `_Baked` sin cambios; ledger M8 `ACCEPTED` y `cache.verify` | `SECONDARY_EQUIVALENCE_FAILED`, `SECONDARY_WORLD_EQUIVALENCE_FAILED`, `SECONDARY_PLAYBACK_UNPROVEN`, `SECONDARY_CACHE_OFFSET_CHANGED`, `SECONDARY_COPY_HIDDEN`, `M8_OBJECT_CHANGED`, `SECONDARY_CACHE_NOT_CURRENT`, … | `SECONDARY_PRIMARY_NOT_SYNCHRONIZED` |

### Hechos medidos (probe, `bpm10-performance-11`; cifras iguales en `-8`, `-9` y `-10`)

- Recién creada la candidata, los tres informes dan `NOT_APPLICABLE` sin ninguna llamada al bridge.
- `secondary_report` **antes** de sincronizar B: equivalencia local 0,0 m (16 frames), pero en mundo **0,131 m** porque
  la primaria todavía no lleva el offset → `FAIL` con `SECONDARY_WORLD_EQUIVALENCE_FAILED` y el aviso
  `SECONDARY_PRIMARY_NOT_SYNCHRONIZED`; estado restaurado. **Después** de sincronizar `B_Wave` con Δ = 10: `PASS`,
  local 0,0 m, mundo **4,8e−7 m**, rango de movimiento 0,083 m.
- `sync_report` tras sincronizar: `PASS`; 2 actores, 3 clips, 80 comparaciones, error máximo de canal **9,3e−10**,
  1 hueco entre clips, 4 `ROOT_RESET_IN_NLA_GAP` (salto máximo 3,0 m), 1 `ROOT_DISCONTINUITY_REVIEW`, 3 Actions
  fuente comparadas y 0 cambiadas; restauración completa (digest, frame/cámara de la fuente y huellas iguales en las dos
  llamadas).
- `facial_report`: con la pista M7 reutilizada, `PASS` con un solo hallazgo (`FACIAL_EVENTS_NOT_DECLARED`), 2 pistas,
  20 comparaciones, error 0,0; con pistas propias y el evento del frame 56, `PASS` sin hallazgos (M7 silenciada como se
  registró, `BODY_OWNS` respetado por el cuerpo sincronizado).
- `review.run_section` resuelve las secciones `sync`, `facial_timing` y `secondary` a `performance.*_report`,
  obligatorias en esta candidata, las tres `PASS`; la candidata sigue fresca después.
- Coste en la fixture (host + LocalBridge, no extrapolable): `sync_report` 0,38–0,50 s, `facial_report` 0,12–0,38 s,
  `secondary_report` 0,12–0,40 s.

## Presupuesto de payloads (medido con `build_code`, peor caso de los tests unitarios)

`sync` 125.078 B / 9.342 nodos AST (8 actores × 32 clips); `stage` 66.493 / 6.422; `props` 61.041 / 7.641; `face`
34.405 / 6.737; `secondary` 31.024 / 5.990; `sync_report` 33.604 / 6.567; `face_report` 29.965 / 5.737;
`secondary_report` 29.696 / 4.885. Todos por debajo de 140.000 B y 14.000 nodos. Payload real máximo en la probe:
51.089 B.

## Propuestas (no medidas en producción)

`THRESHOLDS` con `threshold_source='PROPOSAL'`: canal 1e−5; equivalencia secundaria 1e−5 m (mismo valor que el
`bake_max_error_m` por defecto de M8); salto de raíz para revisión `max(0,05 m, 4 × paso vecino)`.

## Límites declarados

- Un clip a la vez por actor (sin mezclas ni transiciones entre clips del mismo actor).
- La verificación de mapeo compara hasta 64 canales y 8 frames por clip; no certifica subframes.
- `ROOT_RESET_IN_NLA_GAP` mide la raíz como un punto (cabeza del hueso raíz u origen del objeto), no la silueta; se
  avisa aunque otra pista anime los mismos canales (lo dice `also_animated_by_other_tracks`): el salto medido es lo que
  cuenta.
- `BLENDED` no soportado; Actions faciales sobre otros rigs no validadas (una pista por plan M7).
- Secundario: sólo PC2 de malla, Δ entero, sin retime ni cambio de fps; una copia sin padre en el actor no admite
  recolocación posterior (`SECONDARY_STAGING_UNSUPPORTED`); fuera de la ventana la copia muestra el primer/último
  frame cacheado. `secondary_report` mide en los 16 frames de referencia guardados, no en todos los cacheados.
- La lista `meshes` del registro del actor sigue nombrando la fuente M8 excluida; el staging la sustituye por la copia
  (ver [actor-staging.md](actor-staging.md)), las demás áreas todavía no.
- Las aceptaciones M7/M8 de la probe son sintéticas: prueban el mecanismo, no una revisión visual.
