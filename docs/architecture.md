# Arquitectura — Milestones 1 a 9

Blender Production MCP deriva de `ahujasid/mcp-for-blender`, commit inicial
`7cc602252386b92829bc7364e3a897253610be38` (MIT; copyright Siddharth Ahuja).
Conservamos el addon, el transporte socket, el validador AST y sus atribuciones.
La implementación production vive en módulos propios y utiliza el transporte
upstream sin editar una instalación de uvx/site-packages.

M1–M8 conservan sus cierres históricos. M9 está funcionalmente verificado en la versión local 0.9.0 sobre fixtures controladas: 38 tools nuevas y 274 verificadas por MCP stdio. La regresión conserva los 1.781 IDs baseline y añade 266; resultado final 2.046 PASS y un skip heredado. La cobertura MCP combina una ejecución histórica fallida con su suplemento verificado, sin reescribir el resultado original; ver [validación pública y límites](validation.md).

## Recorrido de una operación

```text
Codex
  │ MCP sobre stdio
  ▼
production/server.py
  │ esquema de argumentos + operation_id + serialización + observabilidad
  ▼
production/{inspect,rig,animation,checkpoint,qa}
  │ plantilla bpy + parámetros serializados como JSON
  ▼
production/common/bridge.py
  │ validate_code upstream + handshake de protocolo + localhost
  ▼
upstream BlenderConnection
  │ socket JSON {type, params}, respuesta {status, result}
  ▼
addon oficial en Blender
  │ cola de comandos; ejecución en hilo principal mediante timer
  ▼
bpy → resultado JSON → bridge → respuesta estructurada MCP
```

El bridge reutiliza `blender_mcp.server.BlenderConnection`; no reimplementa el
socket. Cada payload production debe pasar `blender_mcp.safe_mode.validate_code`,
incluso si el entorno hubiera desactivado safe mode. El servidor production fija
`BLENDER_MCP_SAFE_MODE=1` y `DISABLE_TELEMETRY=true`.

Los parámetros se codifican con `json.dumps(..., allow_nan=False)` y se recuperan
mediante `json.loads` dentro de Blender. No se interpolan nombres de objetos,
rutas ni descripciones como instrucciones Python. El resultado usa un marcador
propio en stdout; el bridge exige exactamente un resultado JSON de tipo objeto.

El handshake exige el mismo protocolo que el código upstream incorporado
(versión 9 en el commit inicial). Un desajuste produce error, sin instalar ni
actualizar addons automáticamente.

## Organización del código

```text
addon.py / src/blender_mcp/bundled/addon.py    Addon upstream
src/blender_mcp/server.py                    MCP/transport upstream
src/blender_mcp/safe_mode.py                 Validador AST upstream
src/blender_mcp/production/
  server.py                                 Entry point, envelope y tools M1
  common/
    bridge.py                               Adaptador y resultados JSON
    storage.py                              Reservas exclusivas, SHA256, manifests
    lease.py                                Exclusión entre procesos production
    operations.py                           Propuestas inmutables y eventos M2
    digest.py                               SHA256 puro compatible con safe mode
  checkpoint/__init__.py                    Copias, restore y diff de metadata
  inspect/__init__.py                       Escena y armature
  rig/__init__.py                           IK explícito y candidatos conservadores
  animation/
    algorithms.py                          Medición pura: contactos y quaternions
    scripts.py                             Lectura/evaluación bpy
    service.py                             Validación de inputs y composición
    tools.py                               Registro de las 18 tools M2
    actions.py / edit_runtime.py            Gestión, guards y snapshots de Actions
    corrections.py                         Ciclo analyze/propose/preview/apply/verify/revert
    transform_ops.py                       Correcciones IK, root in-place y retiming
    curve_ops.py / curve_runtime.py          Análisis, aplicación y verificación de curvas
    insights.py / insights_scripts.py        Muestreo, métricas, balance y plan read-only
  qa/
    capture.py                             Viewport y secuencias PNG
    multiview.py / png_montage.py            Cámaras equivalentes, before/after y mosaicos
tests/unit/                                Algoritmos, contratos y fallos controlados
tests/integration/                         Integración MCP/bridge
tests/blender/                             Escenas sintéticas y Blender real
```

Las nuevas funcionalidades deben entrar en su módulo, sin acumular lógica de
producción dentro del `server.py` upstream. M2 conserva los once contratos M1 y
añade dieciocho tools; no incorpora retarget, edición de pesos ni exportación Unreal.

## Contrato de operaciones M2

El envelope M1 permanece: estado de ejecución, `operation_id` por llamada,
afectados, before/after, métricas, warnings/errors y checkpoint. M2 promociona
`mode`, `target`, `proposal`, `operation`, `correction_id`, `numeric_verification`
y `visual_verification` cuando existen. El estado analítico del contenido
continúa en `data`: ejecutar un análisis no implica que la animación sea correcta.

`operation` es una ruta a `operations/<reserva>/proposal.json`. Incluye opciones
congeladas, fingerprint del Action fuente, snapshot detallado, slot, pose y
contexto relevante de escena/rig. `correction_id` identifica ese ciclo entre
llamadas. Se valida confinamiento y SHA256; cada evento de intención,
preview/apply/verify/revert usa otro archivo exclusivo. Snapshots y patches
completos permanecen en disco; el modelo recibe summaries, IDs y métricas.

Los comandos de corrección transmiten fingerprints SHA256 compactos de fuente,
candidato y contexto. Blender calcula los mismos digests con un helper Python
validado por safe mode; no se relaja el validador ni se reenvían snapshots
enormes en cada comando. Los snapshots completos permanecen en registros
locales. JSON de parámetros usa separadores compactos. El fingerprint cubre
los datos inspeccionados, no todo el universo de datos de Blender.

`corrections.py` dirige preflight, rechazo stale, checkpoint, copia con nombre
nuevo, aplicación a copia, medición y comprobación del original intacto. Preview
restaura la asignación original conservando candidato; apply deja activo su
candidato si pasa verificación numérica. Verify vuelve a comprobar firmas/contexto
y a medir. Revert crea checkpoint, vuelve al original y conserva candidato.

`curve_runtime.py` ofrece callbacks `m2_curve_proposal`, `m2_curve_apply` y
`m2_curve_verify`; no posee transporte, checkpoints ni creación de duplicados.
`transform_ops.py` respeta esa división. Los helpers compartidos conservan
slot/layer/strip/data_path/array_index como identidad de canal.

No se interpreta el diff de metadata M1 como igualdad de curves. Los cambios se
verifican mediante snapshots de Actions y muestras evaluadas. Una verificación
numérica correcta mantiene `visual_verification="required"`; no hay puntaje
universal ni autoaceptación artística.

## Estado, copias y recuperación

`Storage` usa `BLENDER_PRODUCTION_WORKDIR` o `.production` como raíz. Una reserva
crea un directorio único con timestamp UTC, identificador aleatorio y
`reservation.json`. Los archivos JSON se escriben con apertura exclusiva.

Un checkpoint añade `intent.json` antes de guardar, una copia `scene.blend`
mediante `save_as_mainfile(copy=True)` y `manifest.json` después de verificar el
archivo. El manifest registra origen, descripción, operación, fecha, tamaño,
SHA256 y snapshot de metadata. La ruta del archivo activo debe permanecer igual.
Los assets externos referenciados no se empaquetan ni duplican.

Restore realiza dry-run por defecto. Al aplicar, valida ubicación e integridad
del checkpoint, guarda un checkpoint de recuperación, duplica el destino en un
nuevo directorio `working` y abre esa copia con `load_ui=False, use_scripts=False`.
Conserva el checkpoint y los originales. La recuperación desactiva la carga
automática de scripts; rigs dependientes de código personalizado necesitan
revisión posterior.

`diff_scene` compara snapshots de metadata: transforms, nombres, jerarquías y
conteos. No compara vértices, pesos, poses, valores de keyframes ni grafos de
materiales. Un cambio de nombre aparece como eliminación/adición. Una diferencia
vacía no demuestra igualdad geométrica o visual.

QA utiliza rutas reservadas. `qa_render_preview` guarda un checkpoint antes de
renderizar. Ambos procesos QA restauran frame/subframe, cámara y los ajustes que
cambian mediante `finally`; el Render Result de Blender se reemplaza. Las salidas
PNG se comprueban desde el proceso MCP y reportan dimensiones reales.

## Concurrencia y límites de seguridad

El servidor production serializa el acceso a Blender mediante un RLock. Desde
M3.1 cada tool se ejecuta en un hilo (`anyio.to_thread`): el bucle de eventos de
FastMCP queda libre para `production.operation_status` mientras otra tool espera a
Blender. Upstream serializa cada intercambio socket. El entry point production mantiene además un
lock de sistema operativo por puerto; se libera al terminar el proceso. Este
lock solo coordina procesos production: un upstream independiente no lo respeta.
La configuración operativa debe habilitar un único controlador por instancia.

El bridge acepta localhost/loopback y comprueba resolución IPv4 de loopback.
El addon oficial escucha localhost por defecto. Su socket no tiene autenticación
ni cifrado; cualquier proceso local con acceso al puerto puede ejecutar código.
Safe mode protege los scripts enviados por production, no convierte Blender en
un sandbox ni protege frente a un cliente que acceda directamente al socket.

No hay tools de descarga, servicios de pago ni API keys en production. Las
tools production tampoco incorporan los decoradores de telemetría upstream.

## Evaluación y rendimiento

Las tools públicas de inspección/análisis admiten selección y paginación donde
corresponde. No devuelven buffers de vértices. Los manifests de checkpoints sí
guardan metadata completa y pueden crecer con el número de objetos.

Foot sliding evalúa el rig real y constraints/NLA en los frames muestreados,
restaurando el tiempo original. Limita frames y evaluaciones hueso/frame; la
salida conserva métricas y detalles acotados. Quaternion analysis examina
F-Curves y distingue cambios de representación de cambios de orientación.
Confidence de heurísticas expresa evidencia, no probabilidad calibrada.

El wrapper registra duración en `metrics.elapsed_ms` y escribe eventos JSON a
stderr y `operations.jsonl`. Las métricas específicas viven en `data.metrics`.
Los errores se devuelven explícitamente; un veredicto analítico `fail` describe
el contenido evaluado y no equivale a un fallo de transporte.

Los análisis M2 muestrean matrices evaluadas con restauración de frame/subframe.
Las trayectorias internas no se envían al modelo. Se limitan muestras, huesos,
curvas, puntos y detalle, con conteos de trabajo y tiempos. La evaluación frame
por frame se reserva a resultados afectados por constraints/drivers/NLA; las
métricas posteriores trabajan sobre esas muestras. La verificación de curvas
usa evaluación densa acotada, no igualdad analítica en cada instante continuo.
Consultar [métricas](animation-metrics.md).

El timeout socket heredado es 180 segundos. No cancela un render que ya está
ejecutándose en Blender. Ante pérdida de conexión/timeout pueden quedar archivos
parciales y la restauración de estado no queda verificada. Esperar a que Blender
esté disponible, inspeccionar el estado y usar el checkpoint si procede; no
reintentar automáticamente la operación. Desde M3.1 el reintento explícito con la
misma clave (o sobre la misma propuesta) reconcilia en lugar de ejecutar dos veces
([idempotencia](operation-idempotency.md)). El addon necesita el loop de eventos
GUI para el recorrido socket; tests headless pueden ejecutar las plantillas de
forma directa sin afirmar que verifican ese recorrido.

## M3: módulos y transacciones separados

```text
production/
  common/m3_transaction.py   # lifecycle con schema/backend/source/context guards
  rig/semantics.py           # evidencias, alternativas y confianza
  rig/ik_fk.py               # adaptador explícito y verificación de transforms
  rig/tools.py               # registro modular
  contacts/algorithms.py     # derivadas físicas y cambios de plano
  contacts/runtime.py        # BVH estático y sampling evaluado
  contacts/correction_runtime.py # anchors heel/sole/toe + IK/FK conservador
  contacts/service.py        # contratos y dispatch
  animation/bezier.py        # análisis/repair de handles con cotas locales
  mocap/                     # mapping, rest compensation, retarget y cleanup
  deformation/               # evaluación geométrica y stress reversible
```

M3 reutiliza bridge, storage, checkpoints, Action snapshots y lease; no cambia
addon/protocolo/core upstream. BASE_RUNTIME añade estado completo de transforms,
propiedades y constraints, snapshots RNA y whitelist estricta para los drivers
switch soportados. Un target sin Action puede recibir candidato nuevo y recuperar
su ausencia original de AnimationData. M2 conserva sus guards sin ampliaciones.

Cada backend define propose/context/apply/verify/allowed_drivers. El coordinador
congela opciones, valida fuente/contexto/backend, crea checkpoint y duplica/crea
Action. Preview restaura asignación; apply activa sólo tras verify; fallo restaura
lo recuperable y conserva candidato/checkpoint. Revert fallido también restaura
el estado de entrada. Los logs son append-only; un timeout no autoriza replay.

Trabajo pesado corre dentro de Blender por rango, sin viajes MCP por frame.
BVHs se construyen una vez, se descartan al terminar y se firman con geometría
evaluada. No hay simulación de colisión dinámica. Los resultados públicos son
resúmenes paginados; snapshots/patches quedan en evidencias locales.

## M3.1: remediación de la auditoría y seguridad de producción

```text
production/
  common/state_guard.py      # modo, pose_position, estado global firmado, contexto transitorio
  common/fcurve_window.py    # ventana de mutación: vallas, división Bézier exacta, handles congelados
  common/asset_safety.py     # roles MASTER/WORKING_COPY/FIXTURE/TEMPORARY y sellos
  common/ledger.py           # idempotencia: NEW/RUNNING/SUCCEEDED/FAILED/UNCERTAIN/REVERTED
  common/transport.py        # recepción lineal y errores clasificados (subclase del upstream)
  common/payload.py          # enlazador: solo viajan las funciones alcanzables
  common/capacity.py         # estimación de capacidad con tasas medidas
  common/lifecycle.py        # inventario y limpieza de artefactos (dry_run por defecto)
  mesh/                      # firma de malla, malla candidata, regiones bloqueadas (base para M4)
```

Orden de una mutación: validar opciones → cargar propuesta inmutable → ledger
(repetición/reconciliación) → preflight (estado global, modo, pose_position,
frescura) → rol del asset → checkpoint → candidato con sufijo determinista →
apply → verificación dentro **y fuera** de la ventana → registro. Capacidad y
tamaño del payload se validan en propose, antes de cualquier checkpoint.

Afirmaciones de M3 corregidas por la auditoría: la compensación de rest pose de
M3 no alineaba poses T/A (AUD-002, ver [retarget-math](retarget-math.md)); el
match de un frame y el bake modificaban la interpolación fuera de la ventana
(AUD-001). Ambas están corregidas y medidas en
[validación pública y límites](validation.md).

## M4: modelado y rediseño de personajes

```text
production/
  character/runtime.py       # CHAR_RUNTIME: inspección, regiones candidatas, silueta, superficie,
                             # asimetría, intersecciones, campo de desplazamiento, guardia anti-volteo,
                             # seguridad de shape keys y verificación numérica
  character/edit.py          # plantillas READ/PREFLIGHT/EXECUTE/CHECK/RECONCILE del ciclo de edición
  character/render.py        # renders Workbench neutros con cámaras compartidas y restauración
  character/transaction.py   # character_adjust_shape: esquema de operación, resolución de regiones,
                             # propuesta inmutable, guard de asset, checkpoint, ledger
  character/service.py       # análisis, plan, candidato, QA de deformación, referencias, sesión
  character/__init__.py      # registro de las 20 tools
```

M4 reutiliza y no duplica la cadena de seguridad de M3.1:
- roles de asset (`common/asset_safety.py`);
- firma de malla, candidato y manifiestos de regiones (`mesh/`, ampliados con `bounds`,
  `dominant_group`, el rol `unassigned` y la captura de caras, materiales y UV);
- propuestas inmutables (`common/operations.py`);
- checkpoints;
- ledger con reconciliación (`common/ledger.guarded_cycle`, ahora con plantilla de reconciliación
  propia para mallas);
- capacidad del payload y la QA de deformación de M3 (`deformation.test_pose`).

Decisiones:
- **Nada por vértice cruza el socket.** El campo de desplazamiento se recalcula en Blender a partir de
  los parámetros congelados, y su digest debe coincidir con el de la propuesta. El payload de apply
  medido a 112 896 triángulos es de 36 330 bytes.
- **La malla se versiona, no se sobrescribe.** Preview crea otro objeto; apply asigna un datablock nuevo
  solo si pasa la verificación; revert reasigna el anterior. El candidato previo y el editado se
  conservan con fake user.
- **Análisis en pose de bind y en metros de mundo**, siempre sobre un objeto explícito.
- **Plan de solo lectura**: reglas de palabras clave, con números marcados como propuesta. La sesión
  guiada ejecuta como mucho un ciclo por llamada, y nunca el apply.

## M5: HIGH/LOW, game mesh y preparación de bake

M5 añade 35 tools en módulos separados. Mantiene el addon, protocolo y transporte upstream;
`server.py` sólo registra capacidades y aplica el envelope, serialización y observabilidad comunes.
La reducción de topología usa otra relación que M4: sus vértices ya no comparten necesariamente
índices con HIGH. Los manifiestos M4 siguen siendo válidos para su topología original y no se
reutilizan como índices de una malla reducida.

```text
production/
  common/m5.py              firmas compactas, relación HIGH/LOW y transacción de candidato nuevo
  retopology/
    helpers.py              geometría base en metros, BVH y correspondencias baricéntricas
    runtime.py              topología, densidad, prioridades, reducción, proyección y fidelidad
    service.py / __init__.py validación de opciones y 11 tools
  transfer/
    runtime.py              pesos, atributos, morphs conservados y estrés evaluado
    service.py              contratos y 6 tools
  game_mesh/
    runtime.py / service.py normales, triangulación, tangentes y 4 tools
    validation.py           agregación de gates concretos, sin puntuación universal
    package.py              paquete local e intercambio neutral de texturas
  uv/
    algorithms.py           clipping UV, tiles y packing acotado
    runtime.py / service.py unwrap, análisis, UDIM y preservación
    __init__.py             7 tools
  bake/
    runtime.py / service.py pares, rayos, cage y NORMAL/AO diagnósticos
    __init__.py             5 tools
```

### Firma y relación inmutable

`production.mesh_signature.m5.v1` calcula digests dentro de Blender usando `foreach_get` y
buffers, más recorrido local donde no existe una API bulk. Cubre topología, coordenadas,
atributos soportados, pesos, morphs, UV, normales por esquina, materiales/asignaciones,
custom properties, modifiers admitidos, rig/rest/pose, relaciones, transforms y unidades.
El host recibe firmas y recuentos; los datos por vértice permanecen locales. Los tipos de
atributo o ajustes de modifier no cubiertos se informan; las mutaciones rechazan modifiers
cuyos ajustes no pueden congelarse. No es una firma universal de shader graphs o handlers.

`production.high_low.v1` guarda `relationship_id`, `revision`, objetos fuente/destino explícitos,
firma HIGH, firma LOW inicial, huella de geometría LOW, `asset_id`, roles y metadata. Cada revisión
tiene su propio archivo e integridad SHA256. HIGH puede ser estático o riggeado; los roles no se
deducen de sufijos. Cambiar contenido HIGH o geometría/transform LOW invalida la relación.
Cambiar UV/pesos LOW permite seguir leyendo la misma relación geométrica, pero una propuesta o
un par de bake previo congela la firma completa y queda stale si cambia ese contenido.

### Transacción común de malla

Los backends proporcionan `m5_plan(high, low, options, relation)`,
`m5_apply(high, low, candidate, proposal, options)` y
`m5_verify(high, low, candidate, proposal, options)`. Sólo implementan su dominio; no duplican
checkpoints, roles, ledger ni transporte. `m5_read` valida la relación y enlaza los objetos antes
de ejecutar un análisis.

Analyze devuelve una propuesta compacta; propose guarda `production.m5_operation.v1`, opciones,
firma LOW reciente, contexto de asset y hash del backend. Preview/apply requieren únicamente
esa referencia, sin nuevas opciones ni relación alternativa. El preflight comprueba integridad,
backend, fuentes y rol; crea checkpoint y repite firma/contexto **dentro del mismo comando** antes
de copiar. `m5_copy` crea otro objeto y otro datablock de malla con marcador de ownership.

HIGH y el LOW previo permanecen intactos. Sólo una verificación satisfactoria puede producir una
revisión HIGH/LOW nueva. Un fallo conserva candidato y relación previa; un cage no se convierte
en LOW. Preview también conserva su candidato para inspección. Revert vuelve a la relación
anterior y conserva el candidato, ocultándolo del render. No reasigna ni destruye mallas fuentes.
Verify exige el candidato correspondiente y firmas todavía frescas. El ledger identifica cada
propuesta y modo; repite resultados conocidos sin producir un segundo candidato. Un resultado
UNCERTAIN no se vuelve a ejecutar automáticamente ni se reconcilia como éxito sin evidencia.
La creación inicial tampoco se anuncia como retry-safe: una respuesta perdida puede haber creado
la copia, aunque el cliente no haya recibido su nombre.

### Correspondencia, prioridades y evaluación

La correspondencia BVH devuelve triángulo, vértices, loops, polígono, componente, punto,
baricéntricas, distancia y ángulo de normal. Distancias, normales incompatibles y componentes
competidores ambiguos se rechazan. La lista completa sólo existe dentro de Blender y la usan
transferencia y bake. Esto no identifica universalmente pliegues cercanos de un mismo componente.

Las regiones se describen mediante grupos/bounds/face sets/masks y prioridades compactas. La
reducción recalcula pesos desde reglas congeladas y deja peso cero en regiones protegidas,
deformación y límites pertinentes. Conserva la tessellación instalada de quads protegidos no
coplanares; después verifica también su superficie interior. `GUIDED_DECIMATION` es una estrategia
de colapso explícita, no un generador de loops anatómicos.

Superficie, normales, siluetas, rayos y poses de estrés usan muestras/umbrales declarados. La
silueta usa cajas y rejillas equivalentes y devuelve su tamaño de píxel. Las capacidades agotadas
no se convierten en éxito. Los unwrap nativos de Blender son operaciones indivisibles: no hay una
cota temporal garantizada para UV de millones de triángulos.

`game_mesh_validate` agrega estados por subsistema y exige evidencia. Congela la firma completa LOW
al comenzar y la vuelve a comprobar al terminar: una escena que cambia durante la agregación no
puede producir una validación aceptada. Los estados son PASS, FAIL, UNVERIFIED y NOT_APPLICABLE;
no existe un promedio que oculte un gate fallido.

`game_mesh_prepare_package` requiere todos los gates numéricos aplicables, par bake fresco y
evaluación triangulada equivalente. Acepta READY_WITH_WARNINGS sólo con passed=true y sin errores;
conserva los avisos. Vuelve a comprobar firmas/par antes de escribir y después del checkpoint.
Escribe package.json, texture-handoff.json y sus hashes en una reserva local nueva. No ejecuta un
export ni una aplicación externa. Una revisión visual sigue separada de la verificación numérica.
Ver [game mesh](game-mesh.md), [retopología](retopology.md), [UV/UDIM](uv-udim.md),
[bake](bake-preparation.md) y [validación pública y límites](validation.md).

### Inventario y compatibilidad

El catálogo M5 contiene 120 tools: las 85 heredadas y 35 nuevas (11 de retopología, 6 de
transferencia, 4 de normales/triangulación/tangentes, 7 de UV/UDIM, 5 de bake y 2 de validación/
paquete). Las nuevas tools tienen output schema estructurado. Dieciséis se anuncian de lectura;
las otras diecinueve pueden crear artefactos o cambiar Blender, incluso sólo transitoriamente.
La validación agregada no es de lectura porque puede ejecutar estrés de deformación. Consultar
[contratos](tools.md) antes de aprobar una operación por su nombre.

Las pruebas separan algoritmos/contratos unitarios, transporte MCP e integración Blender real.
Los runners usan escenas procedurales y rutas de salida nuevas. El inventario implementado y los
diagramas de arquitectura no declaran un milestone cerrado: el informe y su evidencia registran
qué versión, gates, demos y límites se verificaron realmente.

## Generación corporal M6 — entrega local 0.6.0

El dominio `animation/generation/` reutiliza el protocolo/addon upstream y la transacción M3;
no crea otro transporte ni altera los límites de las tools históricas. La rama registra 35
tools de generación y conserva las 120 anteriores: 155 en total. M6 está cerrado en 0.6.0;
la calificación y sus límites se registran en [validación pública y límites](validation.md).

```text
animation/generation/
  planner.py       Brief, procedencia, fases, poses/contactos y capacidad; Python puro
  runtime.py       Compatibilidad geométrica y backend de candidato M3 dentro de Blender
  integrity.py     Firma interna de meshes/pesos/binding/materiales estáticos soportados
  window.py        Validación compartida host/Blender de poses y ventana completa
  pose_policy.py   Neutralidad constante de canales activos no escritos por el adapter
  candidate.py     Evaluación temporal de candidata y restauración en un solo payload
  service.py       Freshness, etapas, checkpoints, revisión, refinamiento y lifecycle
  store.py         plan.json íntegro y eventos encadenados de generación
  measurements.py  Timing/spacing/arcos y proxies corporales de la Action evaluada
  qa.py            Analizadores M1–M3 y gates específicos de raíz/loops M6
  render.py        Secuencia temporal completa, APNG, vistas explícitas y reproductor
  tools.py         35 unidades de trabajo de alto nivel
```

El brief usa `production.animation_brief.v1`; el plan semántico,
`production.animation_pose_plan.v1`. Rotaciones semánticas se expresan en grados y
traslaciones corporales en metros. El runtime las convierte según rest pose, orientación,
unidades y controles comprobados. Los pies describen desplazamientos mundiales desde su
posición de reposo en la base corporal; no se vuelve a sumar root a una ancla plantada.
Una base forward `-Y` puede contener reflexión: las rotaciones se conjugan como matrices
antes de convertir el resultado a quaternion.

El plan de generación congela brief, mapping, fuente, contexto de rig/asset y opciones.
Cada etapa aplicada crea una Action independiente; propose/apply=false no crea Action. Se conserva la fuente y
candidatas anteriores. Snapshots, slots, hash del backend y contexto se comprueban dentro
de la ejecución antes de copiar. Los parámetros siguen viajando como JSON. Los payloads
se enlazan por funciones alcanzables y pasan el validador upstream sin excepciones.

La firma de asset incluye las meshes vinculadas, aunque QA seleccione un subconjunto:
coordenadas/topología/atributos, pesos, binding, modifiers, shape keys estáticas y materiales
del adaptador. Los datos completos se procesan dentro de Blender; el host recibe hashes,
recuentos y alcance. Se usan datos fuente y transforms locales, no geometría evaluada ni
`matrix_world` de una mesh parentada a un hueso. Cambiar frame/pose no invalida la firma.
Cambiar pesos, topología, materiales o binding invalida plan, medición y review previo.
Dependencias animadas, grupos de nodos, imágenes y estructuras no certificadas requieren
otro adaptador y producen un rechazo explícito.

`CandidateBridge` asigna y evalúa la Action candidata, luego restaura Action/slot, frame y
canales en el mismo comando con `finally`. QA combina once analizadores heredados y un
muestreo root/loop: desplazamiento/rotación según intención, drift IN_PLACE, saltos de raíz,
vertical inesperada, pose relativa a root y velocidades vectoriales en los extremos de loops.
Deformación sigue siendo muestreo geométrico acotado; ninguna medida certifica calidad artística.

El lifecycle es PLANNED → GENERATED_CANDIDATE → REVIEWED → ACCEPTED/REJECTED. Blocking exige
una revisión temporal explícita y QA estructural antes de avanzar; saltos CONSTANT declarados
se informan como blocking, no como animación final continua. Una etapa nueva invalida render
y review anteriores. La aceptación vuelve a comprobar fuente/candidata/rig/asset dentro de
Blender antes de activar la candidata. Los estados terminales conservan la evidencia y no
admiten refinamiento posterior.

Cada generación admite tres intentos de refinamiento como máximo. El contador se conserva
en los eventos, incluso después de rechazo, reversión o fallo. Una mejora requiere métrica
material según umbral declarado y una nueva revisión temporal; mejorar una métrica no
acepta automáticamente una candidata. Un ciclo pendiente bloquea otras etapas/lifecycle.

El renderer M6 exige todos los frames y las tres vistas principales. Permite dos vistas
custom adicionales bajo el mismo presupuesto: 601 frames/vista, 1830 imágenes y 200 millones
de píxeles. Produce PNGs, APNG y HTML local; un montage/still no habilita aceptación temporal.
El límite histórico de 24 frames de `qa_render_multiview` permanece intacto.

## Animación facial M7 — entrega local 0.7.0

El dominio `facial/` reutiliza el transporte upstream, el ledger de M6 (categoría `facial`, estado
`FACIAL_CANDIDATE`), el guard de assets M3.1 y los checkpoints. Registra 46 tools nuevas: 201 en total.

```text
facial/
  zones.py          Marco facial normalizado y zonas; fuente compartida literal con Blender
  runtime.py        Conjunto facial, marco, INSPECT, PROBE de controles y huella del rig (Blender)
  scene_runtime.py  WRITE, evaluación NLA temporal, QA geométrica, CAPABILITY, HEAD_TRACK, render, MARK, STATUS
  semantics.py      Reglas de evidencia → salidas semánticas, confianza, híbridos, clasificación, compatibilidad
  profile.py        Perfil = inspección + mapeo + calibración medida (párpados, mandíbula, hueco de labios)
  model.py          Brief con procedencia, dimensiones de expresión, librería, visemas y fonemas
  tracks.py         Pistas, combinación de capas con reglas explícitas y registro de conflictos
  layers.py         Partes por capa: expresión, asimetría, mandíbula, cejas, mirada, sacadas, párpados, habla, micro
  eyes.py           Geometría de mirada, convergencia, coordinación con VOR, mirada anclada al mundo, sacadas
  blinks.py, speech.py, micro.py, mocap.py, curves.py   Generadores y análisis puros
  resolve.py        Semántica → canales reales (claves, huesos, ojos, cabeza, sellado de labios)
  service.py        Plan, versiones de candidata, QA técnica, renders, revisión, refinamiento, lifecycle
  tools.py          46 unidades de trabajo
```

Flujo: PROBE (transitorio y restaurado) → `semantics.analyze` → calibración CAPABILITY → perfil → plan inmutable →
partes de capa (puras) → `combine` → `resolve` → `WRITE` (Action nueva, troceada) → `CANDIDATE_QA` (línea base
solo-cuerpo y candidata por NLA en los mismos frames) → render/revisión → `MARK`. Los datos por vértice nunca salen de
Blender; el host recibe estadísticas, huellas y medidas.

Decisiones medidas en Blender 5.2.1: una Action solo admite una capa (las capas lógicas se combinan en el host); una
Action admite varios slots (OBJECT + KEY); con REPLACE la Action activa del cuerpo gana en los canales solapados aunque la
tira sea ADD; COMBINE en el cuerpo suma; quitar una pista NLA deja la pose evaluada (por eso se restaura explícitamente).

Cada payload pasa el validador safe-mode upstream sin excepciones: como mucho 33 KB y 7.915 nodos AST (límites
200 KB / 20.000). `zones.py` se envía con `inspect.getsource` y el enlazador conserva solo lo alcanzable.

## M8 — dominio de física y movimiento secundario

M8 se califica por pruebas y QA reales de alcance explícito; el [validación pública y límites](validation.md) reúne la evidencia. `physics/tools.py` aporta unidades de trabajo de alto nivel y `service.py` coordina el flujo. `model.py` y `spring.py` normalizan el brief y resuelven respuesta acotada; `inspection.py` clasifica por evidencia; `integrity.py` firma datos y configuración real de pose; `candidate.py` crea copias; `colliders.py` prepara proxies; `cloth.py` usa Cloth/PointCache reales; `procedural.py` escribe canales secundarios disjuntos; `qa.py` mide geometría evaluada; `cache.py` escribe PC2 exclusivo; `render.py` compone previews comparables.

Se reutilizan Bridge/ProductionConnection, safe_mode, asset guard, checkpoint, OperationLedger y generation.store. No se reconstruye el protocolo, el addon ni el sistema de transacciones. El ciclo propio es PLANNED → SECONDARY_CANDIDATE → SIMULATED → REVIEWED → ACCEPTED/REJECTED, con events firmados y máximo tres refinamientos. Los snapshots completos se mantienen en artefactos; las respuestas resumen métricas y referencias.

La firma cruda de fuente incluye Action/NLA, geometría, modifiers, dependencias y configuración estática de pose (rotation mode, IK y B-Bone). La frescura compara además muestras primarias evaluadas. El estado transitorio se restaura con un único frame_set y luego todos los canales/RNA de objetos, poses y keys; restaurar frame por objeto perdía valores manuales. La fuente no se reasigna a otra Action para renderizar.

En Blender 5.2.1 se midió una distorsión de quaternions pequeños con NLA COMBINE. El writer de huesos usa REPLACE sobre canales secundarios disjuntos, con base_local × delta normalizado; un solapamiento con canales primarios se rechaza. La fuente y los huesos no secundarios deben permanecer iguales. PC2 se reproduce con frame_start igual al primer frame; cero adelantaba la cache. La equivalencia se mide en todos los frames enteros, sin prometer continuidad entre muestras.


La auditoría final amplió la captura del renderer a la unión de originales y candidatos, incluyendo dependencias y KEY; la igualdad de RNA se comprueba después. La dirección de objetivos de refinamiento es una allowlist de minimización, sin interpretar como mejora la caída de área o cambios de volumen.

## M9 — materiales y fundamentos PBR

`production/materials/` incorpora un dominio independiente sobre Bridge, safe mode, asset guard, checkpoints, OperationLedger y `animation/generation/store`. El core upstream y el protocolo del addon permanecen separados. El servidor sólo registra las unidades de trabajo; no contiene la lógica de construcción de shaders.

| Módulo | Responsabilidad |
| --- | --- |
| `model.py` | Definición de material, canales semánticos, parámetros y perfiles con procedencia propuesta |
| `inspection.py` | Inspección acotada de grafos/grupos/slots y resumen paginado |
| `textures.py` | Datos de imágenes, archivos/tile, huellas, color spaces y canales |
| `uv.py` | Relaciones UV/material/textura/UDIM; validación sin escribir coordenadas |
| `qa.py` | Hallazgos técnicos por alcance, capacidad y rendimiento sin quality score |
| `integrity.py` | Firmas de fuente/candidato/dependencias y snapshots completos dentro de capacidad |
| `graph.py` | Construcción determinista de nodos y sockets verificados en Blender instalado |
| `candidate.py` | Copias propias de Object/Mesh/Material y asignación exclusivamente candidata |
| `render.py` | Estudio temporal PBR, pares de iluminación equivalente y restauración |
| `service.py` | Plan inmutable, lifecycle, versiones, gates, review y máximo tres refinamientos |
| `handoff.py` | Extensión neutral del contrato M5 para intercambio futuro de texturas |
| `tools.py` | 35 tools requeridas más `material_status`, `material_review` y `material_refine` |

El lifecycle es `PLANNED → CANDIDATE → REVIEWED → ACCEPTED/REJECTED`. El plan congela fuente, definición, backend y dependencias; los eventos conservan versiones y evidencia. Una edición externa de nodos, imágenes, geometría, Actions, cache o backend invalida su reutilización. La construcción escribe una candidata nueva; el material fuente y sus slots permanecen iguales. `material_apply_candidate` verifica la asignación ya aislada y no instala el material en el original.

Los bindings de canales son datos: nombre de imagen existente, componente y color space. `normal_convention`, `height_mode`, modelo y renderer se declaran explícitamente. Se distingue color de datos, AO de Base Color, normal de bump y bump de desplazamiento geométrico. Los grupos se inspeccionan de forma acotada; soportar su lectura no significa aceptar cualquier shader para edición automática.

La inspección entrega internamente registros limitados y el cliente recibe resumen/filtrado/paginación. Los PNG, snapshots y manifests permanecen en almacenamiento local exclusivo. UV/UDIM reutiliza la base M5; un material puede abarcar tres, cinco u otro conjunto válido de tiles, y un tile puede convivir con varios materiales. El handoff conserva los estados de bake de M5, incluyendo `NOT_READY`/`UNKNOWN`.

El renderer usa una escena propia y copia raw estática, sin evaluar la animación como producto final. EEVEE y Cycles informan su backend real; Workbench se rechaza para PBR. Aunque se renderice otro scene, Blender puede reevaluar RNA original: se capturan/restauran conjuntamente objetos/poses/KEY/NLA. Las simulaciones nativas activas se rechazan previamente; no se desactivan para sortear el guard. Los pares usan la misma geometría, cámara y configuración. La review se vincula a hashes y cuatro iluminaciones vigentes; una puntuación automática no sustituye mirar las imágenes.

Ver [inspección](material-inspection.md), [PBR](pbr-workflows.md), [color](texture-color-management.md), [UDIM](material-udim.md), [QA/restauración/backend](material-qa.md) y [contrato neutral](substance-handoff.md). El [validación pública y límites](validation.md) registra la verificación funcional 0.9.0, sus evidencias de alcance explícito y los fallos históricos conservados.
