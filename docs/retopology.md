# Retopología M5

El adaptador trabaja con la geometría base/bind de objetos explícitos, en metros de mundo. No
asume que HIGH esté riggeado ni identifica HIGH/LOW por sufijos: la relación inmutable de
`common/m5.py` registra ambos objetos y sus huellas, unidades, transforms y metadatos. Un cambio del
HIGH o de la geometría/transform del LOW invalida la relación. Los cambios de atributos del LOW no
invalidan la geometría de la relación, pero toda propuesta congela su firma completa.

Flujo: auditar → densidad → prioridad → presupuesto → estrategia → candidato → propose → preview
→ apply → verify → QA visual y de deformación → atributos/UV/bake. La transacción crea checkpoint y
**un nuevo objeto con una nueva malla**; HIGH y LOW anterior permanecen intactos. Una verificación
fallida conserva la relación anterior y el candidato rechazado. Revert conserva el candidato y
devuelve la relación anterior, sin borrar fuentes.

## Estrategias

`GUIDED_DECIMATION` usa explícitamente el modificador Decimate/COLLAPSE de Blender 5.2.1, con
`vertex_group_factor=1000`, sin simetría, sin forzar triangulación global y con pesos de reducción
derivados de prioridades. Antes del colapso, los quads protegidos no coplanares se convierten en
triángulos que conservan exactamente la diagonal calculada por la versión instalada de Blender.
Esto evita que un cambio de diagonal altere la superficie aunque sus cuatro vértices sigan fijos.
Se consultaron la RNA y `bmesh.ops.triangulate` instalados. Esa conservación no sustituye a la
verificación posterior.

`LOCAL_RETOPOLOGY` limita los pesos positivos a una región explícita y verifica también que no
cambien los vértices, pesos, materiales y UV de fuera. Puede pedir `target_reduction` (0..0,95) y
revisa un presupuesto local además del global. `PRESERVE_AND_OPTIMIZE` con presupuesto igual al
actual crea una copia; una reducción con ese nombre se rechaza para no ocultar Decimate.

`REMESH_AND_PROJECT`, `HYBRID` y `MANUAL_REQUIRED` se reconocen como clases de planificación pero
este adaptador devuelve una alternativa manual. No existe fallback silencioso ni promesa de
retopología automática con loops dirigidos por un artista.

## Regiones y atributos

Regiones compactas: `name`, `definition` de M4 (grupo, bounds, face set, mask o dominant_group),
`role`, `protected`, `deformation`, `priority`, `silhouette_priority`, `tolerance_m`, presupuesto
opcional y `minimum_density_per_m2`. Listas de millones de índices no son aceptables en propuestas
MCP. Las regiones de la relación se conservan cuando options omite regiones o proporciona una lista
vacía. Cambiar la definición o quitar protección de una región registrada se rechaza.

Se fijan los vértices protegidos, de deformación, de límites regionales/materiales, abiertos y sharp.
La verificación de regiones compara posiciones por KD-tree, pesos por nombre de grupo, conjuntos de
materiales por vértice y valores de cada UV original a una cuantización de 1e-6. También mide contra
el BVH del candidato el centro y los tres puntos medios de cada triángulo HIGH completamente
incluido en una región protegida. Devuelve `max_surface_deviation_m` y `surface_samples`; la medida
es un muestreo explícito, no una cota continua de toda la superficie. La tolerancia geométrica por
defecto es 1e-5 m; la de pesos es 1e-5. La tessellación puede cambiar la estructura de polígonos de
autoría conservando la superficie protegida, sin exigir quads universales.

La malla evaluada conserva las capas que Blender pueda transportar. El resultado declara que UV y
pesos fueron interpolados y requieren su QA/transferencia específica. Las shape keys provocan
rechazo **antes de mutar**: `MORPH_TOPOLOGY_CHANGE_REQUIRES_MANUAL_OR_LATER_PIPELINE`. Nunca se
eliminan para hacer pasar un gate. Normales personalizadas requieren la política explícita
`custom_normals_policy="rebuild"` antes de permitir una reducción.

## Parámetros y límites

El target es configurable (1..10 millones de triángulos). El rango global por defecto es ±5 % y se
puede especificar con `minimum_triangles`/`maximum_triangles`; el local usa
`region_budget_tolerance_fraction` (por defecto 0,05). Una región bloqueada define un suelo
conservador de caras completamente protegidas. El presupuesto solicitado puede resultar imposible
y entonces se rechaza, sin bajar tolerancias automáticamente.

Las tolerancias por defecto de superficie (1 cm), normales (80°) y silueta (2 cm e IoU 0,95) son
límites técnicos de partida, no criterios universales de producción. Las demos controladas usan
5 mm/50°/25 mm e IoU 0,97, declarados en sus informes. El error de superficie y la silueta son
muestreados: el resultado numérico no certifica una calidad artística o anatómica.

Reproducción: `tests/blender/m5_retopology_probe.py` inicia su propio Blender headless sin sockets,
sin autoexec y con fixtures sintéticas. El historial de fallos permanece en los artefactos; un fallo
de tiempo de espera nunca se reejecuta bajo una nueva clave a ciegas.
