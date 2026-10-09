# Base de seguridad de malla para M4: firma, candidata y regiones bloqueadas

**No es M4.** No hay esculpido, remodelado, retopología ni rediseño. Es la infraestructura de
seguridad que M4 necesitará. Solo se probó con fixtures generados; nunca con el personaje real.

Código: `src/blender_mcp/production/mesh/` (`runtime.py` en Blender, `service.py` en el host).
Pruebas: `tests/unit/test_m31_mesh.py`, más `tests/blender/m31_mesh_probe.py` en Blender real.

## Firma de malla (`production.mesh_signature`)

Cada sección devuelve metadatos compactos y una huella (`m2_fast_digest` sobre JSON canónico; detecta
cambios accidentales, no es criptográfica):

| Sección | Contenido |
|---|---|
| topology | vértices, aristas, polígonos, loops, triángulos; huella de aristas, tamaños de polígono y vértices por esquina |
| geometry | huella de las posiciones |
| shape_keys | nombres y orden, clave relativa, grupo, rangos, mute, interpolación, valor, huella de datos por clave y drivers |
| vertex_groups | nombres y orden, índice, lock, vértices pesados y suma de pesos por grupo, huella de todas las asignaciones |
| uv_layers | nombres, activa, activa en render, huella de datos, teselas UDIM por centroide de cara (1001 + ⌊u⌋ + 10·⌊v⌋) y caras fuera de rango |
| normals | dominio, `has_custom_normals`, huella del atributo guardado `custom_normal` y de `sharp_face`/`sharp_edge` |
| materials | slots (link, material) y huella de `material_index` |
| modifiers | nombre, tipo, visibilidad; en ARMATURE, objeto, grupo, inversión, grupos y envelopes, preserve volume, multi-modifier |
| relationships | padre, tipo, hueso padre, `matrix_parent_inverse`, colecciones, constraints, Action y drivers del objeto |
| transform | location, rotación (modo, euler y quaternion), escala, deltas y `matrix_world` |
| custom_properties | propiedades del objeto y de la malla (sin el marcador de candidata) |
| attributes | atributos genéricos, más `.sculpt_face_set` y `.sculpt_mask` |

Por MCP solo sale un resumen (5 088 bytes en el fixture de 152k triángulos). La firma completa se guarda
en `<workdir>/signatures/`. Ningún dato por vértice va al modelo.

Decisiones basadas en medidas (Blender 5.2.1):

- **Normales custom:** se firma el atributo guardado `custom_normal` (CORNER, INT16_2D), no las
  normales evaluadas. Al mover un vértice, el dato guardado no cambió y las normales evaluadas sí, así
  que un cambio de geometría no se confunde con un cambio de normales custom.
- **Flags sharp:** al salir de Edit Mode, Blender eliminó `sharp_edge` porque era todo False. Ausente y
  todo False se tratan como lo mismo.
- **Modo:** en Edit o Sculpt Mode los datos de malla pueden estar desfasados. Se rechaza con
  `MODE_NOT_SUPPORTED` y nunca se cambia el modo automáticamente.

## Malla candidata (`production.mesh_candidate_create` / `mesh_candidate_verify`)

```
FUENTE → CANDIDATA (copia de objeto + malla) → [mutación futura, M4] → VERIFICAR → decisión (M4)
```

Requisitos: Object Mode, malla no enlazada ni con override, un rol de asset mutable y la
`source_fingerprint` de una firma reciente (`STALE_SIGNATURE` si la fuente cambió). Antes de crearla
se guarda un checkpoint. La candidata comparte colecciones con la fuente, se oculta solo del render
(`hide_render`) y lleva un marcador con el origen. Si ya hay una candidata del mismo estado de la
fuente, se reutiliza en lugar de duplicarla.

Preservación, informada dependencia por dependencia y medida en el fixture:

| Dependencia | Resultado en el fixture |
|---|---|
| relación de objeto (padre, tipo, inversa) | preservada |
| modificador Armature (objeto y opciones) | preservado |
| vertex groups y pesos | preservados (comparados peso a peso) |
| shape keys (datos por clave y driver) | preservadas, con bloque de claves propio (no compartido) |
| material slots | preservados |
| UV layers (incluidas las teselas 1001/1002) | preservadas |
| normales custom | preservadas |
| custom properties | preservadas |

**No verificado** (se declara, no se da por preservado): drivers o constraints de *otros* objetos que
apuntan a la fuente, entradas de modificadores distintos de Armature más allá de nombre, tipo y
visibilidad, y el contenido de la Action del objeto (solo se compara su asignación). Aceptar o
rechazar una candidata no está implementado: es una capacidad de M4.

## Regiones bloqueadas (`production.mesh_region_define` / `mesh_region_verify`)

Una región es LOCKED o EDITABLE, se define por `vertex_group` (peso mínimo), `face_set`, `mask`
(máscara de esculpido) o `indices` explícitos **ligados a una huella de topología**, y nunca por nombre
de objeto. El manifiesto (`<workdir>/regions/*/manifest.json`) guarda índices, posiciones,
shape keys y la huella de pesos de las regiones bloqueadas, más la firma completa.

La verificación de una región LOCKED falla si:

- `TOPOLOGY_CHANGED`: la topología ya no permite casar los índices;
- `REGION_MEMBERSHIP_CHANGED`: la definición selecciona ahora otros vértices;
- `LOCKED_REGION_MODIFIED`: la distancia en el mundo (con `matrix_world` y la escala de unidades)
  supera `tolerance_m` (1e-5 m por defecto, máximo 0,01 m);
- `LOCKED_SHAPE_KEY_MODIFIED` o `SHAPE_KEY_MISSING`;
- `NEW_SHAPE_KEY_MOVES_LOCKED_REGION`: una shape key nueva desplaza vértices bloqueados;
- `LOCKED_WEIGHTS_CHANGED`: cambian los pesos de deformación de la región.

Los cambios en regiones EDITABLE se informan, no hacen fallar la verificación. La verificación compara
en el host: no se reenvían posiciones a Blender, porque el receptor upstream del addon reprocesa el
búfer completo en cada bloque recibido.

## Evidencia (fixture de 76 729 vértices y 152 352 triángulos, Blender 5.2.1)

Los 23 checks pasan (`test-artifacts/m3.1-mesh/mesh-150k-final-d7dcbf64.json`). Tiempos en proceso (sin transporte por socket):

| Medida | Valor |
|---|---|
| firma | 2,145 s y 2,127 s (dos ejecuciones); respuesta completa 13 260 bytes; resumen MCP 5 088 bytes |
| crear candidata (checkpoint incluido) | 6,36 s |
| definir 3 regiones | 3,76 s; manifiesto local de 16,4 MB (16 437 936 bytes) |
| verificar regiones sobre la candidata | 1,72 s |
| detección | vértice movido 0,1 mm → solo cambia `geometry`; peso editado → cambia `vertex_groups` |
| región bloqueada | 1 mm → falla; peso → falla; shape key nueva → falla; topología → falla; 5 cm en región editable → pasa |
| fuente | huella idéntica tras todas las ediciones de la candidata |
