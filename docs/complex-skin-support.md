# Soporte de piel compleja y vistas comparables

Pedido por el usuario el 2026-10-23 a partir de una prueba con un grafo de piel grande (GlobalSkin) en un personaje de
producción privado (2026-10-20). Tres
necesidades, tres capacidades. Código: `src/blender_mcp/production/skin/` y `materials/inspection.py`.

| Necesidad | Tools | Estado |
|---|---|---|
| Aplicar UV externas por loop a una capa secundaria | `production.mesh_import_uv_layer`, `production.mesh_remove_imported_uv_layer` | Probado en Blender 5.2.1 y por MCP |
| Configurar/inspeccionar la vista 3D para comparar con la misma luz | `production.viewport_inspect`, `production.viewport_configure`, `production.viewport_restore`, `production.viewport_capture` | Probado en Blender GUI propio por MCP |
| Inspeccionar grafos de material grandes | `material_inspect` / `material_analyze_nodes` con `options.graph_mode="paged"` | Probado en Blender 5.2.1 y por MCP |

## Importación tipada de UV

**Por qué así.** El modo seguro no deja `open` ni `numpy` dentro de Blender, y el socket upstream parsea JSON con coste
cuadrático (medido antes: 4,2 MB → 18,5 s). Millones de UV no pueden viajar como JSON. El servidor lee el `.npy` (sin
pickle: la cabecera se interpreta con `ast.literal_eval` y sólo se aceptan `<f4`/`<f8`, C-order, forma `(loops, 2)`),
escribe un OpenEXR float32 sin compresión con dos loops por píxel RGBA y Blender lo carga por `bpy.data.images.load`,
que el modo seguro sí permite. No se debilita ningún guard.

**Verificación dentro de Blender**, en la misma llamada:

- suma exacta (`math.fsum`) de u y de v comparada con la del servidor, más ~2.000 posiciones deterministas comparadas
  valor a valor (ambos extremos, paso uniforme y dispersión LCG); el payload queda < 200 KB;
- la capa escrita se relee y debe ser idéntica a lo cargado;
- se copian antes y se comparan después: posiciones, `vertex_index` de cada loop, `material_index` de cada polígono y
  todas las capas UV existentes; también los slots de material y la UV activa y la de render;
- si algo no cuadra, la capa nueva se elimina antes de devolver el error (`UV_IMPORT_VERIFICATION_FAILED`).

**Reglas.** `apply=false` sólo compara el archivo con la malla. `apply=true` exige WORKING_COPY/FIXTURE (MASTER →
`MASTER_ASSET_READ_ONLY`), modo objeto, nombre nuevo (nunca sobrescribe: `UV_LAYER_EXISTS`), ≤ 8 UV maps, y crea un
checkpoint antes. El EXR portador se borra tras el éxito (el `.npy` del usuario se queda) y su SHA queda en
`import.json`. `mesh_remove_imported_uv_layer` sólo elimina capas que creó el importador (marca en la malla).
Una malla compartida por varios objetos se avisa (`UV_SHARED_MESH`). Un `.npy` float64 se guarda como su redondeo
float32 y el error máximo se informa.

**Medido** (Blender 5.2.1, RTX 4060 irrelevante aquí, CPU Ryzen 7 5700G):

| Caso | Resultado |
|---|---|
| Icoesfera, 15.360 loops, valores negativos y UDIM > 1 | capa idéntica bit a bit a la fuente (comprobación numpy independiente); 0,136 s |
| Rejilla de 4.008.004 loops | lectura `.npy` 1,20 s; importación completa (incl. checkpoint de archivo pequeño) 4,34 s; EXR 4096 × 490, 32,1 MB; payload 76 KB |
| 11 negativos (nombre existente, loops distintos, NaN, pickle, Fortran, forma plana, nombre inválido, capa ajena, modo edición, MASTER, ruta relativa) | rechazados sin tocar la malla |

No se midió una malla de producción de ≈ 23 M loops; el checkpoint de un `.blend` de 900 MB añadiría su copia.

## Vista 3D comparable

`viewport_inspect` lista áreas y el estado de cada VIEW_3D, y los HDRI de Material Preview. `viewport_capture`
configura un área desde un esquema cerrado (sombreado, HDRI, rotación/intensidad/fondo, perspectiva, orientación
FRONT/BACK/LEFT/RIGHT/TOP/BOTTOM/THREE_QUARTER_*, o cuaternión, objetivo, distancia o `fit`, lente, overlays, gizmos),
hace un render OpenGL de **esa** área y restaura el estado anterior salvo `keep=true`. Devuelve una firma SHA-256 del
estado efectivo (floats redondeados a 1e-6): misma firma = misma luz, HDRI, vista y lente.

Si la ventana no tiene VIEW_3D (la consola de la prueba original) se rechaza con `VIEWPORT_NO_VIEW3D`; sólo con
`area_index` y `allow_area_conversion=true` se convierte esa área y luego vuelve a su tipo. `viewport_configure` deja la
vista configurada y guarda el estado previo; `viewport_restore` lo devuelve. Sin GUI: `VIEWPORT_GUI_REQUIRED`.

**Medido por MCP** (Blender GUI propio, puerto 9877): tres capturas con la misma especificación —una de ellas
convirtiendo la consola— dieron **el mismo PNG byte a byte** (SHA-256 `14a4265c…`); cambiar HDRI u orientación cambió la
firma; la consola volvió tras capturar y tras `viewport_restore`. Revisión visual de las capturas: el HDRI y la
orientación cambian el brillo y el reflejo como se espera; `background_alpha=0` deja el fondo gris liso. Una esfera
sólo demuestra que luz y vista se controlan y repiten; no juzga la piel.

## Grafos de material grandes

El modo completo conserva su límite (256 nodos / 4.096 sockets) porque alimenta la validación y la construcción de
candidatas; su error ahora indica el modo paginado. `graph_mode="paged"` recorre el grafo entero (con grupos anidados)
para estadísticas —recuentos por tipo, profundidad, links inválidos/silenciados, imágenes— y construye filas sólo para la
página pedida y para nodos clave acotados (salidas, Principled, texturas, normal maps, UV maps). Límites del recorrido:
20.000 nodos, 400.000 sockets, 50.000 links, 16 niveles. Página: 100 nodos resumidos o 50 con sockets, 200 links.
**No valida**: `analysis_scope=PAGED_INSPECTION`, `passed=null`; las tools de validación y construcción rechazan el
modo paginado (`MATERIAL_GRAPH_MODE_UNSUPPORTED`).

**Medido**: grafo de 439 nodos con 3 grupos anidados → las páginas cubren cada nodo y cada link exactamente una vez;
estadísticas iguales a un recuento independiente; página máxima 118 KB. Grafo de 2.119 nodos / 11.479 sockets /
2.105 links recorrido en 0,048 s, respuesta de 27,6 KB.

## Evidencia

`test-artifacts/m10/skin/probes/*.json` (probes en Blender propio en segundo plano), `test-artifacts/m10/skin/bpm10skin-eb0b8758/report.json`
(MCP real, 40 comprobaciones), `test-artifacts/m10/skin/captures/*.png`, `tests/unit/test_skin_support.py` (47 tests).
