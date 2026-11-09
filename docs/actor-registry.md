# M10 — escena, actores e integridad (área B)

Módulo: `src/blender_mcp/production/cinematic/actors.py`. Contrato general: [cinematic-architecture.md](cinematic-architecture.md) §4.
Pruebas: `tests/unit/test_m10_actors.py` (sin Blender) y `tests/blender/m10_actors_probe.py` (Blender real).

## 1. Tools

| Tool | Tipo | Qué hace |
|---|---|---|
| `cinematic_inspect_scene` | READ_ONLY | Inventario compacto y paginado de una escena fuente. Nunca devuelve el grafo entero. |
| `cinematic_register_actor` | escritura **solo en el host** | Mide el actor en Blender (payload de solo lectura) y escribe un registro inmutable. No toca Blender; se permite sobre MASTER. |
| `cinematic_inspect_actor` | READ_ONLY | Registro del actor + comparación con sus huellas actuales → `fresh` / `ACTOR_STALE` con qué cambió. |
| `cinematic_verify_integrity` | READ_ONLY | Integridad de todos los actores de un plan y de la escena fuente frente al ledger. |

Ninguna de las cuatro hace `frame_set`, asigna Actions/slots ni escribe pose. Lo comprueba un test unitario sobre el payload
enlazado (mismas expresiones regulares que `test_m31_hints.py`) y el probe compara el estado completo de Blender antes y
después de cada llamada de solo lectura.

Argumentos que no corresponden a la tool (por ejemplo `object_name` en `inspect_scene`) se rechazan con
`CINEMATIC_ARGUMENT_UNSUPPORTED`; `apply=true` en una tool de solo lectura, con `CINEMATIC_APPLY_UNSUPPORTED`; opciones
desconocidas, con `CINEMATIC_OPTION_UNSUPPORTED`. Todo se valida antes de llamar a Blender.

### 1.1 `cinematic_inspect_scene`

Opciones: `source_scene` (por defecto la escena activa), `limit` (1–200, por defecto 50), `offset` (≥0), `detail`
(`summary` | `full`) y `sections` (subconjunto de las secciones; sirve para paginar una sola). La misma página
(`offset`/`limit`) se aplica a cada sección pedida; `pagination.totals` da el total por sección y `pagination.more` las
secciones con más filas.

Secciones: `markers` (con cámara), `collections`, `cameras` (lens, lens_unit, sensor, clip, DOF), `lights`,
`actor_candidates` (armatures con mallas ligadas y canales de root de su Action activa; mallas animadas no ligadas a un
armature; empties), `actions` (slots, rango, si animan transformación de objeto y qué huesos animan `location`, dueño
M7/M10), `facial_m7` (Actions con `production_facial_owner='M7'` y su lifecycle; pistas `M7_FACE*` en objetos y Keys),
`secondary_m8` (objetos `production_m8_owned`, modifiers `M8_*` —`M8_PC2_PLAYBACK` con su `frame_start`— y pistas
`M8_SECONDARY*`) y `props` (mallas sin animación propia, sin shape keys animadas, sin modifiers de deformación y no
propiedad de M7/M8/M10). Además un resumen de escena: fps, unidades, render, timeline, view transform, world, cámara.

### 1.2 `cinematic_register_actor`

`object_name` (ARMATURE, MESH o EMPTY de la escena fuente) y opciones:

- `role` (obligatoria, `model.ACTOR_ROLES`);
- `bones` (opcional): `{root, pelvis, head, neck, hand_left, hand_right, foot_left, foot_right}` → nombre de hueso;
- `body_actions` (opcional): nombres de Actions corporales. Si se omite: la Action activa y las de pistas NLA que no son
  `M7_FACE*`, `M8_*` ni `M10C_*`, y nunca Actions propiedad de M7/M10;
- `facial` / `secondary` (opcionales): rutas `plan.json` de generaciones M7 / M8 (máximo 8 cada una);
- `source_scene` (opcional): escena donde debe estar el actor (por defecto la activa). Es una clave añadida a la lista
  del encargo porque la clave del registro de un archivo TEMPORARY depende del nombre de la escena.

Ruta del registro: `<root>/cinematic-actors/<asset_key>/actor-<actor_id>.json` (escritura exclusiva, `registry.write`).

## 2. Identidad y huella

La identidad **nunca** es el nombre del objeto. El registro guarda:

- `asset_fingerprint` (medido en Blender con `m2_digest`): tipo de objeto; rig = jerarquía, flags y `matrix_local`,
  `head_local`, `tail_local` de cada hueso, `rotation_mode` y cableado de constraints de pose (nombre, tipo, target,
  subtarget) y drivers; y por cada malla ligada (la propia malla, sus hijos MESH y las mallas de la escena con modifier
  Armature apuntando al actor): topología, posiciones de reposo (`mesh.vertices`), UV por capa, shape keys
  (estructura + coordenadas de cada key), slots de material y `material_index`, nombres de vertex groups. Los buffers se
  leen con `foreach_get` y se resumen con `m2_fast_digest` (patrón M5); solo viajan digests.
- `fingerprint` (host, sha256 canónico): `asset_fingerprint` + `role` + huesos resueltos + Actions corporales
  (nombre, slot, digest `m2_digest(m3_source_snapshot(a))`) + referencias M7/M8 (ruta, estado, digest).

Excluido a propósito, porque cambia legítimamente sin que el personaje cambie: transformación de objeto y deltas
(el staging los toca), pila NLA y Action activa (la sincronización M10 los toca), valores de shape keys y `eval_time`
(los escribe la NLA facial en cada frame), canales de pose, influencia/mute de constraints y propiedades custom.

Reglas:

- mismo objeto + mismo dato + misma `fingerprint` → mismo `actor_id`, sin registro nuevo (`created=false`);
- cualquier cambio → registro nuevo; el anterior se conserva intacto y se emite `ACTOR_FINGERPRINT_CHANGED` con la lista
  de secciones que cambiaron (`asset`, `rig`, `mesh_set`, `meshes:<malla>:<sección>`, `role`, `bones`, `body_actions`,
  `facial`, `secondary`);
- restaurar exactamente el contenido devuelve el `actor_id` original (medido, §6).

Campos del registro (contrato §4): `actor_id, object, data, type, role, asset_role, meshes, body_actions[{action, slot,
origin, frame_range, digest, root_motion}], active_action, facial[...], secondary[...], world_transform, dimensions_m,
interaction_capabilities, fingerprint, limitations`, más `integrity` (digests por sección), `identity_sections`,
`asset_fingerprint`, `bones`, `bounds_m`, `mesh_details`, `scene`, `registered_at`, `blender_changed=false`.

## 3. Huesos, alcance, dimensiones y root motion

**Huesos**: explícito (`options.bones`, verificado en el armature; si falta: `ACTOR_BONE_MISSING`) > semántica M3
(`m3_semantic_model` de `rig/semantics.py`, el mismo modelo que `analyze`) con confianza ≥ 0,9 > para `root`, el único
hueso sin padre si hay exactamente uno (`HIERARCHY_SINGLE_ROOT`) > `UNKNOWN`. Una asignación semántica por debajo del
umbral no se usa y se avisa con `ACTOR_SEMANTICS_LOW_CONFIDENCE`.

**Alcance (`reach_m`)**: solo si hay mano explícita o semántica ≥ 0,9; si no, `UNKNOWN`. Es una **estimación**: suma de
longitudes de reposo mano + 2 padres (definición de extremidad M3) × mayor eje de escala del objeto × `scale_length`.
No es un alcance medido en la animación.

**Dimensiones**: AABB en mundo de los datos de reposo (vértices sin deformar de cada malla por su matriz actual + cabezas
y colas de reposo de los huesos). Una malla emparentada a un hueso usa la pose actual de ese hueso.

**Root motion** (sin `frame_set`): F-Curves crudas evaluadas con `fcurve.evaluate(frame)` en frames enteros del rango
de la Action (paso que mantiene ≤ 2001 muestras). `location` de objeto en espacio del padre; `location` del hueso root
pasada por su `matrix_local` y la matriz actual del objeto. Devuelve desplazamiento horizontal XY (Z arriba), distancia,
longitud de recorrido, máxima excursión, rango vertical, duración y eje dominante (`+X/-X/+Y/-Y`, `NONE` solo si el
desplazamiento final es exactamente 0, `TIE_XY` en empate). Sin umbrales. No compone rotación animada del objeto,
constraints, drivers ni animación de huesos padre (declarado en `limitations`).

**M7 / M8**: las referencias se validan con `facial.service.load` / `physics.service.load` (solo lectura). M7: el
armature del plan o su objeto debe ser el actor o una de sus mallas (`ACTOR_FACIAL_OBJECT_MISMATCH`); se guarda estado,
Action candidata, su digest actual y si coincide con `candidate_fingerprint` del ledger. M8: el objeto fuente, los
primarios, la cadena o el anchor del plan deben ser el actor, una malla o un hijo (`ACTOR_SECONDARY_OBJECT_MISMATCH`);
se guarda estado, objetos candidata/baked y su digest (modifiers, incluido `MESH_CACHE`). Un estado distinto de
`ACCEPTED` se registra tal cual con aviso (`FACIAL_NOT_ACCEPTED` / `SECONDARY_NOT_ACCEPTED`): la integración lo exigirá.

## 4. Integridad

`integrity_report(ctx, plan, state)` (exportada) hace **una** llamada al bridge y compara:

- por actor del plan: registro presente y con la misma huella que el plan; objeto (existe, mismo tipo y dato, sigue en
  la escena fuente); rig; conjunto de mallas ligadas; por malla, cada sección; cada Action corporal (digest y slot);
  cada Action facial; objetos M8; y relee los ledgers M7/M8 (`*_LEDGER_CHANGED` / `*_LEDGER_INVALID`);
- escena fuente: `cm_source_fingerprint` contra `state['fingerprints']['source']` (base `ledger`), o contra
  `plan['source']['fingerprint']` antes de montar o tras rechazar (base `plan`);
- escena candidata: `cm_candidate_fingerprint` contra el ledger (`NOT_APPLICABLE` sin montaje o tras rechazar);
- cámaras y luces fuente: cubiertas por la huella fuente; si esta cambia y el plan no trae una línea base por objeto, el
  estado es `UNKNOWN` (no se inventa qué objeto cambió).

Salida: `passed`, `changed[]` (con `code`, `item`, `section`, `expected`/`observed` abreviados), `checked[]` (en
`summary` solo las filas que requieren atención; en `full` todas, máximo 200), `counts`, `source`, `candidate`,
`cameras_lights`, `actor_reports` (con `moved` informativo: mover un actor no lo vuelve stale).

`actor_record(ctx, actor_id, key=None, scene=None)` y `actor_spec(record)` también se exportan para otros módulos.

## 5. Límites y propuestas (no medidos)

| Valor | Origen |
|---|---|
| Confianza semántica mínima 0,9 | PROPUESTA: convenio AGENTS/M3; la confianza M3 es una puntuación de evidencia, no una probabilidad calibrada |
| ≤ 32 mallas y ≤ 2.000.000 vértices por actor (`ACTOR_CAPACITY`) | PROPUESTA para mantener el payload lejos del timeout de 180 s; no medido con personajes de producción |
| `limit` ≤ 200, `checked` ≤ 200 filas | PROPUESTA de compacidad de la respuesta |
| ≤ 8 referencias M7/M8, ≤ 32 Actions corporales | PROPUESTA |

## 6. Hechos medidos (Blender 5.2.1 LTS, background, `--factory-startup --disable-autoexec`)

Probe `tests/blender/m10_actors_probe.py`, salida `informe local privado; véase validation.md`:
**51/51 checks PASS, 0 errores**, 76 llamadas al bridge, 3,65 s el probe completo. Fixture inline: biped `HeroRig`
(18 huesos) con caja ligada, criatura `CreatureRig` (3 huesos, rotada 90°) con cara de shape keys y una Action/pista
`M7_FACE` sintética, prop `Crate`, objeto M8 sintético con `M8_PC2_PLAYBACK`, cámara, luz de área, marker y world.

- Las tres tools de solo lectura dejaron idéntico el estado de Blender (frame, cámara, pose de 21 huesos, 2 shape keys,
  digests de 3 Actions, matrices de 8 objetos, NLA, vértices, escenas) en las 12 llamadas comprobadas. El registro
  también.
- Semántica M3 en el biped: manos 0,93 (≥ 0,9, usadas), root resuelto; cabeza 0,88 (< 0,9) → `UNKNOWN` con aviso.
- Alcance estimado: 0,6000000536 m para 0,1 + 0,25 + 0,25 m de huesos (error de float32).
- AABB de reposo: [1,5999999, 0,45000002, 1,7999999] m frente a [1,6, 0,45, 1,8] de construcción.
- Root motion desde F-Curves crudas: `HeroRig` [0,0, 3,0] m, `+Y`; comparado con la posición evaluada del hueso root en
  los frames 1 y 60 (con `frame_set` en el propio probe): diferencia 0,0. Criatura rotada 90°: [-2,0, -8,74e-08] m,
  `-X` (el residuo en Y es del coseno de 90° en float32).
- Idempotencia: segundo registro igual → mismo `actor_id`, 1 solo fichero. Mover un vértice 0,01 → registro nuevo con
  `changed = [asset, meshes:HeroBody:positions]`; restaurar el valor exacto → `actor_id` original.
- `verify_integrity`: PASS antes de la candidata (base `plan`, 16 ítems sin cambio), después de crearla, y después de una
  mutación registrada que pasa la Action activa a una pista NLA desplazada 10 frames (la huella fuente cambia, `Hero_Walk`
  y el rig no). Detecta: vértice movido en la cara (`MESH_CHANGED/positions`, que la huella de escena **no** ve);
  coordenada de shape key (`MESH_CHANGED/shape_keys`, solo esa sección); lente de la cámara fuente
  (`SOURCE_SCENE_CHANGED`, cámaras `UNKNOWN`). Tras restaurar y tras rechazar la candidata vuelve a PASS.
- Fallo propio encontrado y corregido: abreviar un digest m521 por los primeros 16 hex del residuo **no** distingue
  valores: el cambio de lente produjo `expected` y `observed` con el mismo prefijo (`m521:bbe:ab84d7acd8b77c08`) porque
  una edición pequeña solo altera bits bajos del residuo. Ahora la etiqueta es `m521:<longitud>#<sha256[:16]>` del digest
  completo; la comparación siempre fue con el digest entero.
- Tamaño de payload (safe mode, con los parámetros máximos que admiten las tools): inventario 19.732 B / 3.877 nodos
  AST; registro 43.275 B / 9.019; integridad con 8 actores × 32 mallas × 32 Actions 31.940 B / 4.245. Límites del
  encargo: 140.000 B / 14.000 nodos. Tiempos en la fixture (LocalBridge, sin socket): inventario 0,18 s (primera
  llamada) y 0,05 s (una sección); registro 0,10–0,22 s; `inspect_actor` 0,05–0,13 s; `verify_integrity` 0,06 s. No son
  extrapolables a personajes de producción.

## 7. Limitaciones conocidas

- Las huellas son de datos de reposo y contenido de Actions; no certifican deformación evaluada, composición NLA ni
  render.
- Los cambios de Edit Mode no están en `mesh.vertices` hasta volver a Object Mode (se declara en `limitations`).
- Sin línea base por objeto en el plan, un cambio de cámara/luz fuente se detecta pero no se atribuye.
- Las generaciones M7/M8 del probe son ledgers sintéticos de fixture: prueban la validación de referencias y digests,
  no la integración real (áreas de otros agentes).
