# Seguridad y reproducción

Las restricciones de alcance de las secciones históricas corresponden a cada entrega. M4–M8 fueron autorizados y cerrados posteriormente; sus reportes documentan la calificación acotada sobre fixtures. M9 fue autorizado después y está funcionalmente verificado en 0.9.0 sobre fixtures controladas; ver [validación pública y límites](validation.md). Los proyectos reales, M10 y otros MCP siguen requiriendo autorización nueva y concreta.

El servidor MCP production usa stdio y conecta sólo a IPv4 loopback. Se rechazan hosts de LAN/Internet y puertos fuera de 1024..65535. El addon oficial está enlazado a localhost. El MCP no expone un listener HTTP ni crea reglas de firewall. Los runners de QA pueden servir previews mediante HTTP temporal sólo en 127.0.0.1 (esta calificación usó8768–8771), comprobando puerto libre y cerrando su propio proceso.

**Límite real:** upstream expone un socket local sin autenticación que acepta Python. BLENDER_MCP_SAFE_MODE es un validador AST del recorrido MCP, no un sandbox del proceso Blender ni una frontera frente a otro proceso local. Production valida todas sus plantillas con el validador upstream sin excepciones, fuerza safe mode y desactiva telemetría. No registra tools de servicios externos. No introducir credenciales.

La lease de production impide dos procesos production sobre el mismo puerto. No puede detener un controlador upstream ajeno: mantener una sola configuración habilitada y revisar procesos antes de pruebas. El transporte serializa comandos; production serializa operaciones completas.

Checkpoints usan directorios exclusivos con timestamp UTC y UUID, `intent.json`, copia .blend y `manifest.json` con SHA256. Nunca reutilizan rutas. `copy=True` conserva el archivo activo al crear checkpoint. El manifest registra descripción, operación y archivo origen (null si todavía no fue guardado). Los assets externos **no se empaquetan ni copian**; los checkpoints no reemplazan un backup completo del proyecto.

Restore sólo acepta manifests dentro del almacén configurado, verifica hash y rutas, crea recovery checkpoint y abre el checkpoint con `use_scripts=False`. Blender lo guarda en una ruta nueva de trabajo con `relative_remap=True`. No escribe el checkpoint ni el maestro. Los manifests son datos locales de confianza; no son firmas criptográficas ni un sistema de control de acceso frente a otro usuario con escritura local.

Preview crea checkpoint, reserva rutas nuevas, desactiva temporalmente compositor/sequencer/multiview y restaura ajustes/frame en finally. Reemplaza el buffer Render Result; no se conserva ese buffer. La captura viewport también restaura ajustes, sin guardar el .blend. Los cambios transitorios de frame pueden evaluar drivers existentes: no abrir proyectos no confiables ni habilitar scripts por conveniencia.

El timeout del transporte upstream es 180 s. Un timeout no cancela una operación en Blender: reportar error e incertidumbre, conservar artefactos parciales y recovery, esperar/verificar estado antes de reintentar. No prometer restauración tras desconexión, crash o cierre forzado.

Logs JSONL locales registran operation_id, execution_id, tool, estado, tiempos y errores. No incluyen código ni geometría completa; errores/rutas pueden contener nombres del proyecto. No se envían al exterior. No hay borrado automático de checkpoints ni logs.

## Correcciones M2

Las propuestas son archivos nuevos en `operations`, con opciones, fuente,
snapshot, contexto y SHA256. Preview/apply no admiten nuevas opciones: para
cambiar tolerancias se analiza y propone otra operación. Cambios del Action o
contexto invalidan la propuesta antes de checkpoint/duplicación. El hash detecta
corrupción accidental; no autentica frente a procesos locales con escritura.

Los guards rechazan Actions enlazadas/overrides, composición NLA activa/tweak,
drivers del objeto/datablock, influencia parcial, slots/layers/strips ambiguos y
curvas muted/locked/con modifiers/sampled points. El runtime no hornea ni aplana
esos casos silenciosamente. Ver [seguridad de correcciones](animation-correction-safety.md).

Preview/apply crean checkpoint y escriben exclusivamente en una Action copiada.
Si fallan métricas, devuelven error y restauran asignación/pose fuente cuando la
ejecución alcanza recuperación. El candidato se conserva para diagnóstico.
Revert también guarda checkpoint y rechaza una fuente cambiada o una Action ajena activa.
Desde M3.1 conserva los cambios del usuario posteriores a la propuesta (contexto,
ediciones del candidato) y los informa; solo restaura los canales estáticos que
escribió la propia operación.

Las verificaciones cubren los datos/contexto inspeccionados: no son un sandbox
de rigs arbitrarios ni prueban ausencia de todo efecto de handlers instalados.
No abrir proyectos no confiables ni debilitar guards para forzar soporte.

QA multiview crea sus cámaras temporales después del checkpoint, las retira al
finalizar y restaura cámara, Action/slot, pose, frame y ajustes modificados. Un
timeout puede ocurrir antes de confirmar restauración; no reintentar automáticamente.

El desarrollo M2 usa solo fixtures generadas. Las nuevas tools no autorizan
modificar `.blend` reales, publicar/push, descargar assets, introducir credenciales,
usar servicios de pago ni iniciar M3 dentro del alcance histórico de M2.

## Garantías adicionales M3

Los drivers no se habilitan de forma general. Sólo el adaptador IK/FK explícito
acepta una relación directa/inversa de una propiedad local revisada. Se conserva
el graph; no se eliminan constraints, drivers ni huesos para sortear un rechazo.
Semántica de baja confianza no permite mutación automática.

La propuesta M3 incluye schema y hash de backend además de Action/contexto.
Cambiar código, rest pose, surface evaluada, mapping o fuente invalida la propuesta.
Las dependencias externas no contempladas se rechazan. Parámetros no reconocidos
en correctores se rechazan, evitando tolerancias mal escritas silenciosas.

Contactos estáticos: distancia con signo a la cara válida más cercana, desplazamiento
lateral acotado y dwell/velocidad (M3.1). Puntos coplanares fuera del borde no son
soporte. Sin contacto confirmado el sliding es `null`, nunca 0. Mirror objects externos/superficies
animadas no se aceptan como estáticos. Muestreo finito no es una prueba continua.

Stress poses de deformación guardan checkpoint, suspenden Action/NLA y restauran
pose, propiedades, asignación, frame y settings en finally; nunca cambian pesos.
Las métricas son geométricas y aproximadas, sin afirmaciones anatómicas.

El alcance histórico M3 excluía M4/modelado/rediseño. M4, M5 y M6 se autorizaron después; Substance,
Orchestrator y Unreal Production MCP siguen fuera del alcance actual. No se autorizan assets
reales, push/publicación, software ajeno, credenciales ni servicios de pago.

## Garantías añadidas en M3.1

- **Maestro técnico (AUD-014).** Toda mutación persistente exige un rol mutable
  (WORKING_COPY, FIXTURE registrado o sesión sin guardar) y se comprueba antes del
  checkpoint. Un archivo del usuario no registrado se rechaza con
  `ASSET_ROLE_UNREGISTERED` y un MASTER con `MASTER_ASSET_READ_ONLY`. Los roles nunca
  se deducen del nombre. Ver [seguridad de assets](asset-safety.md).
- **Estado global (AUD-004).** `pose_position=REST`, modos que no son Object/Pose
  y cambios de solver, unidades, FPS o modificadores invalidan o bloquean la
  operación antes de mutar.
- **Idempotencia (AUD-010).** Un reintento con la misma clave o sobre la misma
  propuesta reconcilia en vez de ejecutar dos veces; `production.operation_status`
  responde qué pasó sin tocar Blender. Ver [idempotencia](operation-idempotency.md).
- **Render (AUD-015).** Cycles se renderiza solo en GPU. La CPU exige
  `allow_cpu_fallback=true` y el resultado lo marca (`fallback_used`). Las
  preferencias de Blender solo se leen.
- **Hints (AUD-016).** Las tools que cambian frame, Action o pose de forma
  transitoria no se anuncian como solo lectura. Un cliente no debe
  autoaprobarlas como si lo fueran.
- **Capacidad (AUD-008).** Muestras, memoria estimada y tamaño del payload se
  validan en propose; nada se asigna antes.
- **Transporte (AUD-009).** El cliente de socket de production es una subclase del
  upstream con recepción lineal y errores clasificados (timeout, conexión perdida,
  error de Blender). El core upstream no se modificó. El receptor del addon
  upstream sigue reprocesando el búfer completo en cada bloque recibido: production
  evita enviar a Blender parámetros grandes (la verificación de regiones compara
  en el host).
- **Malla (base para M4).** La firma, la candidata y las regiones bloqueadas solo
  leen la malla fuente o trabajan sobre una copia; no hay operaciones de modelado.

## Garantías añadidas en M4

- **Maestros.** Toda tool M4 que muta (`character_create_candidate`, `character_adjust_shape`
  preview/apply/revert, `character_deformation_qa`, `character_redesign_session apply=true`) comprueba
  el rol del asset antes de cualquier checkpoint o cambio. `character_create_candidate` rechaza un
  MASTER incluso cuando solo reutilizaría un candidato existente. Medido: 6 entradas rechazadas con
  `MASTER_ASSET_READ_ONLY`, 0 checkpoints nuevos y SHA256 del maestro sin cambios.
- **Fuente intacta.** Solo se editan mallas candidatas (`CANDIDATE_REQUIRED`). En las demos, la fuente
  de la copia de trabajo y el archivo maestro no cambian.
- **Regiones.** Las bloqueadas tienen peso 0 por construcción y se verifican con tolerancia.
  Topología, pertenencia, caras, materiales, UV, pesos y shape keys se comparan. Los índices de otra
  topología nunca se reutilizan.
- **Dependencias.** Una edición falla si cambian la topología, los pesos, las UV, los materiales, el
  estado de normales, los atributos o los offsets de shape keys, o si alguna cara gira ≥ 90°. Las
  normales no se recalculan.
- **Deformación.** `character_deformation_qa` aplica las mismas poses de estrés explícitas a fuente y
  candidato, restaura la pose y falla si el candidato añade defectos. Un candidato con pesos rotos a
  mano falló (0 → 36 aristas estiradas).
- **Renders.** Look neutro y cámaras compartidas; estado restaurado y comprobado. Workbench en GPU,
  con el backend informado.
- **Pruebas.** Solo fixtures generados. Los tests reales en Blender usan procesos propios
  (`--background --factory-startup --disable-autoexec`) o una instancia GUI propia en el puerto 9877;
  nunca el Blender ni el controlador del usuario en 9876.

## Garantías y límites M5

- **Nuevo candidato por mutación.** Reducción, proyección, transferencias, UV, normales,
  triangulación y cage nunca escriben directamente en HIGH ni LOW previo. Exigen rol mutable,
  propuesta íntegra, backend y firmas frescas, checkpoint y guard de contexto/firma repetido dentro
  del comando antes de copiar. Objetos enlazados/overrides y dependencias no soportadas se rechazan.
- **Relación geométrica explícita.** HIGH/LOW se registra con objetos, firmas, revisión y SHA256;
  no se empareja por nombres. Un cambio de HIGH o geometría/transform LOW la invalida. Propuestas
  y bake pairs congelan además los atributos LOW actuales. Los manifests sólo se aceptan dentro
  del almacén pertinente. Son integridad local, no autenticación contra un proceso local malicioso.
- **Protección regional.** Omitir una lista de regiones no quita las protecciones registradas;
  cambiarlas o aumentar su tolerancia mediante override se rechaza. Además de vértices, pesos,
  materiales y UV, se mide superficie interior de triángulos protegidos. Una diagonal distinta de
  un quad no coplanar puede fallar aunque no se mueva ningún vértice. Los índices M4 de otra
  topología no se reutilizan para justificar protección.
- **Resultado rechazado.** Un candidato que falle conserva la relación anterior, checkpoint y
  diagnósticos. No se relajan tolerancias ni se eliminan morphs/modifiers para aprobarlo. Revert
  conserva candidatos; no es borrado ni restauración de edits ajenos hechos después de la propuesta.
- **Morphs.** Reducción con shape keys se rechaza antes de aplicar. Sólo se soportan morphs estáticos
  ya conservados exactamente; reconstruirlos sobre otra topología requiere un pipeline posterior.
  La copia con shape-key Action/driver/NLA se rechaza. Triangulación puede conservar vértices y
  morphs existentes, pero debe pasar su propia verificación.
- **Normales y tangentes.** Custom normals se preservan por defecto; reconstrucción exige opción
  explícita y confirmación. Tangentes vienen de `Mesh.calc_tangents` instalado, con UV válida y
  controles de finitud/ortogonalidad/signo. Una base declarada no certifica un importer futuro.
- **QA transitoria.** Estrés de pesos/deformación exige poses explícitas y checkpoint; restaura
  estado y verifica fuentes. Bake usa copias transitorias propias de LOW/cage, no reescribe UV ni
  nodos del original. NORMAL/AO son diagnósticos acotados; CPU exige `allow_cpu_fallback=true` y
  no cambia preferencias guardadas. No hay authoring de texturas finales ni servicios externos.
- **Capacidad e incertidumbre.** Muestras, payloads y presupuestos del dominio son acotados.
  El unwrap nativo no se puede interrumpir por una cota interna fiable; un timeout de socket no
  demuestra que Blender haya terminado. Un resultado UNCERTAIN conserva evidencia y prohíbe un
  segundo apply automático de la misma propuesta. Inspeccionar estado y recuperación antes de
  decidir otra operación; no iniciar otra llamada mientras Blender siga trabajando.
- **Aceptación separada.** `PASS` numérico no prueba calidad artística. El paquete rechaza gates
  fallidos, sin evidencia o excluidos; el estado visual queda pendiente salvo un manifiesto local
  de revisión que corresponda a las firmas e imágenes vigentes. El registro de revisión tampoco
  sustituye una evaluación humana ni exporta a Unreal/Substance.
- **Snapshot coherente.** La validación final compara la firma completa LOW inicial/final y la
  firma HIGH registrada. Un cambio concurrente invalida el resultado agregado. El paquete exige
  LOW/par/triangulación correspondientes y comprueba otra vez el par antes de escribir y después
  del checkpoint; no acepta una validación anterior como autorización permanente.
- **Avisos de bake.** READY_WITH_WARNINGS es aceptable sólo cuando `passed=true`, `errors` está
  vacío y el par sigue fresco. Los avisos permanecen en el informe y paquete. NOT_READY, un estado
  desconocido o una contradicción entre estado y errores se rechazan; no se renombra un fallo como
  aviso para obtener aceptación.
- **Roles de tools.** Las 16 nuevas tools de lectura tienen `readOnlyHint=true`. Las otras 19
  aceptan `operation_id` y no prometen idempotencia universal. Esto incluye validación agregada,
  estrés opcional, triangulación temporal, pares escritos y smoke bake. Los modos analyze/propose
  de una tool mutadora no convierten su contrato MCP completo en una tool de lectura.

Los controles son conservadores y medidos con fixtures controladas. Sampling finito, padding UV
por bounding boxes, correspondencia cercana y estrés con poses concretas tienen límites publicados
en [game mesh](game-mesh.md) y [validación pública y límites](validation.md). No se declara soporte
general de UV/bake de producción de millones de triángulos a partir de una prueba grande de geometría.

## Límite de desarrollo M5

M5 permite solamente fixtures controladas; no autoriza abrir/modificar `.blend` reales. Los
procesos de prueba tienen su propia instancia/puerto y no contactan el Blender del usuario en
9876. No se cambia configuración MCP, addon, preferencias ni procesos ajenos por conveniencia.
No se descargan assets ni se integran servicios externos.

Tras dos correcciones sustanciales del mismo gate fallido por una limitación arquitectónica,
detener ese subsistema y documentar el bloqueo. No declarar el milestone completo si un gate
obligatorio sigue fallando. Una regresión crítica de seguridad debe resolverse antes de cualquier
cierre; no se pospone para obtener una versión o un tag. Las mejoras opcionales se registran como
propuestas en [validación pública y límites](validation.md), sin implementarlas dentro de M5. M6 fue autorizado después; no autoriza automáticamente ese backlog y M7 sigue fuera de alcance.

## Generación corporal M6 — controles verificados en 0.6.0

El inventario implementado de esta rama contiene 155 tools, de las cuales 35 corresponden a M6.
La presencia de una tool o de un test unitario no prueba cierre del hito. Blender real, regresiones,
fixtures, cuatro demos, negativos, rendimiento y QA temporal se comprobaron para esta entrega
local, con [validación pública y límites](validation.md). Las operaciones con servicios de pago, descargas y proyectos reales requieren autorización específica del usuario.

- **Fuente y maestro.** Cada etapa modifica una Action candidata nueva o duplicada de una candidata
  de la misma generación. No elimina ni sobrescribe la Action fuente. Antes de cambios persistentes
  verifica el rol mutable y guarda checkpoint. El contexto de archivo/roles se comprueba también
  dentro del payload de ejecución. MASTER continúa siendo de lectura.
- **Mapping y controles.** La confianza proviene de jerarquía, geometría bilateral, parentado y
  constraints reales; el llamador sólo puede reducirla. El adaptador exige torso/root coherentes,
  brazos FK y piernas compatibles. IK requiere target/pole propios, independientes, no deform y
  sin constraints, cadena de dos huesos, influencia completa y stretch desactivado. No keyea switches
  IK/FK ni modifica el rig para salvar un rechazo. FK feet no adquieren foot lock por naming.
- **Evidencia fresca.** Fuente, candidata, rig, archivo, rol y contexto de deformación se vinculan
  a fingerprints. La firma incorpora todas las meshes ligadas: geometría, topología, atributos,
  pesos, binding, modifiers, shape keys y materiales estáticos soportados. Un cambio invalida el
  plan/review anterior. El cálculo usa fuentes y matrices locales; cambiar frame/pose no produce
  una falsa diferencia por geometría evaluada o por parentado a huesos.
- **Límite explícito de firma.** Máximo 16 meshes de personaje y 100 000 vértices en total, 64 shape
  keys por mesh, dos millones de puntos de shape keys por mesh, 128 nodos/512 links de material y
  demás presupuestos publicados por el adaptador. Materiales/shape keys/meshes animados o con drivers,
  imágenes, grupos de nodos, estructuras mutables anidadas no cubiertas, modifiers dinámicos y
  dependencias externas no certificadas se rechazan. No se afirma cobertura universal de shaders.
- **Blocking revisado.** CONSTANT es una intención de blocking comprobada en la Action real. Sus
  saltos discretos de pose/orientación/raíz se informan, mientras contactos, reach, stretch,
  deformación, quaternion inválido/incompleto y sign flip conservan sus gates. La revisión temporal
  permite avanzar; no convierte blocking/POSE en una aceptación final de animación continua.
- **QA completa.** Los errores devueltos por un analizador, aunque no lancen excepción, fallan el
  gate de medición completada. La ausencia de mediciones, contactos o deformación no equivale a 0.
  Root se contrasta con su intención IN_PLACE/ROOT_MOTION; loops IDLE/LOCOMOTION comprueban pose
  relativa a root y velocidades vectoriales lineales/angulares en los extremos. Las tolerancias
  son explícitas y configurables; proxies geométricos y muestreo finito no certifican peso artístico.
- **Secuencia temporal.** Se requieren todos los frames y FRONT/SIDE/THREE_QUARTER. Las direcciones
  custom adicionales no reemplazan esas vistas. La revisión identifica reviewer, observaciones,
  findings, decisión y artifact_reference vigente, con temporal_reviewed=true. Stills, artefactos
  viejos, renders incompletos y review de otra candidata no habilitan aceptación.
- **Ciclos y estados.** GENERATED_CANDIDATE → REVIEWED → ACCEPTED/REJECTED, sin resucitar estados
  terminales. El máximo de tres refinamientos es persistente e incluye intentos revertidos o fallidos.
  Un ciclo pendiente bloquea otra etapa, render o aceptación. Mejorar una métrica no reemplaza la
  revisión temporal; una mejora insuficiente restaura la selección previa y conserva la candidata.
- **Incertidumbre.** Checkpoints, Actions y artefactos parciales se retienen. Un timeout no cancela
  Blender. No reintentar automáticamente ni crear otra operación mientras el resultado anterior
  siga incierto; consultar operation_status y comprobar la instancia/recuperación.

El backend heredado de multiview restaura cámara, Action/slot, pose, frame y ajustes. M6 no cambia
las preferencias guardadas. El renderer temporal tiene su propio presupuesto y mantiene los
límites históricos M1–M5. Ver [lifecycle](generated-action-lifecycle.md),
[QA temporal](animation-visual-qa.md) y [validación pública y límites](validation.md).

## Animación facial M7 — controles verificados en 0.7.0

- **Maestro y fuente.** Toda mutación facial pasa por `require_mutable_asset` y checkpoint; un MASTER se rechaza antes de
  crear nada (probado en Blender: `MASTER_ASSET_READ_ONLY`, 0 Actions). Cada versión es una Action nueva con nombre
  determinista; si el nombre existe → `FACIAL_CANDIDATE_COLLISION`, sin sobrescribir.
- **Cuerpo intacto.** La Action del cuerpo nunca se escribe: se evalúa la cara por NLA. Su digest se comprueba en el
  plan, antes de mutar (`BODY_ACTION_CHANGED`), en la QA, al integrar (cambio de matrices de huesos del cuerpo exigido 0)
  y al aceptar o rechazar. `FACE_OWNS` con el cuerpo animando cabeza o cuello → `HEAD_OWNERSHIP_CONFLICT`.
- **Mapeo con evidencia.** La confianza la calcula el servidor a partir de la deformación medida; los nombres solo ajustan.
  Rigs `UNSUPPORTED`/`AMBIGUOUS`, claves absolutas, Basis inválida, ojos poco fiables o sin mandíbula bloquean la
  mutación con su código. Nunca se keyea un canal con driver ni un hueso que anima el cuerpo.
- **Frescura.** Plan íntegro (sha256), huella del rig (huesos, constraints, límites, drivers, datos de shape keys), digest
  del cuerpo, archivo, sellos de asset y huella de la candidata. Cualquier cambio → `STALE_*`. Ledger con eventos encadenados.
- **Timing de habla.** No hay reconocimiento de voz ni alineador: audio sin timing → `TIMING_INPUT_REQUIRED`. No se usan
  servicios de pago. La energía solo escala la mandíbula.
- **Restauración.** Probe, calibración, QA, integración y render capturan y restauran pose, valores de shape keys, frame,
  cámara y ajustes de render; el resultado informa `state_restored`.
- **Renders en GPU.** Workbench (rasterizador de la GPU, backend informado); nunca CPU.
- **Revisión y ciclos.** Aceptar exige QA sin errores, revisión ACCEPT del preview actual e integración sin cambios en el
  cuerpo. Máximo 3 refinamientos persistentes. ACCEPTED/REJECTED son terminales. Sin puntuaciones artísticas.
- **Instancias.** Los probes usan Blender propio en background y el runner MCP un Blender propio en 9877; nunca se toca 9876
  ni las preferencias del usuario.

## Protección M8

Toda mutación usa asset guard y checkpoint antes de crear/configurar candidatos; MASTER se rechaza antes del checkpoint. Las fuentes corporales/faciales se firman y se conservan. Los colliders pertenecen a una colección con generación/versión y miembros exactos; un collider abierto devuelve UNKNOWN, no colisión cero. AABB_PROXY es una alternativa explícita para geometría rígida compatible, con desviación medida y máximo permitido; no aplica transforms ni simplifica el render mesh original.

Las máscaras de pins/roots/regiones son explícitas. Full-weight es un anclaje duro; falloff parcial no equivale a fijación. Se rechazan modifiers de topología/vecindad cuando impedirían proteger los índices. Desplazamiento, ángulo, anchors y región exterior se miden sobre la evaluación real. El solver reserva un budget por brazo de palanca en meshes ponderadas y acota el ángulo de accesorios por 2r·sin(θ/2); esas predicciones no sustituyen los guards de Blender.

Los planes, events, cache y copias bakeadas se verifican al reabrir el estado. Tamper, cambios de cuerpo/cara/configuración/FPS/escala/dependencias o source/bake producen STALE y bloquean la reutilización. Las rutas de cache están confinadas al store, son exclusivas y se verifica contenido y manifest. Preflight limita transferencias de muestras de mesh a16MB; no se autoriza una simulación cuya cache no pueda transferirse. El PointCache instalado exige frame_end≥1: ventanas cloth incompatibles se rechazan.

Un timeout deja la operación incierta en ledger y en el estado de la generación; cambiar operation_id no permite repetirla automáticamente. No se aceptan scores de realismo inventados ni revisión de otro artefacto. Aceptar exige QA técnica, revisión temporal real en tres vistas normal/0,25 y bake equivalente. Las aproximaciones, UNKNOWN y límites de cobertura permanecen visibles.

## Protección M9 — materiales, verificación funcional 0.9.0

Toda escritura de candidata o artefacto pertinente reutiliza el guard de assets, checkpoint y ledger. MASTER se rechaza; una copia de trabajo real requiere autorización concreta del asset. El desarrollo utiliza fixtures propias. No se modifica el material fuente, sus slots, UV, geometría, shape keys, pesos, Actions, rig o archivos de imágenes para corregir un problema de shader.

`SOURCE MATERIAL → CANDIDATE MATERIAL → VERIFY → ACCEPT/REJECT` crea materiales y objetos propios. La asignación candidata permanece aislada. `material_apply_candidate` no reemplaza slots del original; accept marca la candidata revisada y reject conserva candidata/evidencia, sin intentar deshacer ediciones ajenas. Las versiones, fuente, backend, nodos/grupos, imágenes/tile y dependencias verificadas forman parte de los guards de frescura. Los archivos externos se identifican por contenido; cambiar una imagen invalida evidencia anterior.

El builder acepta definiciones estructuradas con canales, componentes, color spaces y parámetros permitidos. Consulta la API instalada; no ejecuta expresiones Python proporcionadas dentro de una definición ni habilita scripts/drivers para forzar un shader. Las imágenes deben existir y cumplir el contrato: no descarga texturas, no cambia silenciosamente el color space de una imagen fuente y no sobreescribe su archivo. Normal DirectX sin conversión verificada, convención desconocida o datos incompatibles se informan/rechazan; no se invierte verde por una heurística de nombres.

La validación UV/UDIM es de lectura. No requiere un material por tile ni exactamente cuatro tiles. El manifiesto de handoff extiende M5 y conserva `NOT_READY`; no autoriza bake, lanzamiento de Painter, layers/masks, publicación ni creación de materiales Unreal. Los objetivos de resolución de un manifest no implican generar texturas de esa resolución.

Antes de renderizar se comprueba capacidad y se rechaza simulación nativa activa del contexto. El renderer no apaga CLOTH, FLUID, Soft Body, rigid bodies o zonas de simulación/bake de Geometry Nodes para pasar. El estudio es temporal, compara copias raw estáticas y restaura objetos, poses, shape keys y NLA conjuntamente, incluso ante error conocido. `Render Result` se sustituye y se informa. La prueba con CLOTH desactivado y cache centinela no se presenta como garantía sobre simulación activa.

EEVEE usa su rasterizador real; Cycles sólo usa GPU habilitada en preferencias existentes. Si se exige un backend específico y no está disponible, se rechaza. CPU necesita `allow_cpu_fallback=true` y se declara como CPU; `require_gpu=true` lo impide. No se guardan ni alteran preferencias para satisfacer una petición. SSS/refraction/alpha se reportan con las limitaciones reales del renderer.

Una review exige artefactos actuales y cuatro pares de iluminación fuente/candidata verificados por hash; aceptar exige además QA técnica vigente. Máximo tres refinamientos por generación, incluyendo intentos fallidos; no crear otra generación para evadir el límite. Un cambio material invalida su review. Timeout/desconexión conserva incertidumbre y bloquea repetición automática, aunque cambie `operation_id`.

Ver [QA de materiales](material-qa.md) para el fallo real de restauración corregido, la corrección de PNG generados negros y los límites de los probes. El [validación pública y límites](validation.md) distingue los probes focales, la regresión final y la cobertura MCP compuesta: el FULL histórico conserva su resultado FAIL; el suplemento verifica las correcciones pendientes. La verificación sobre fixtures no autoriza modificar proyectos reales ni acredita compatibilidad universal.
