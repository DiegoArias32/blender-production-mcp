# QA de deformación M3

El dominio `production/deformation` evalúa meshes reales de Blender con sus
modificadores. No modifica pesos, shape keys, geometría, constraints ni keys de
Actions. Los resultados son indicadores geométricos; no certifican anatomía ni
calidad artística.

## Herramientas

| Tool | Comportamiento |
|---|---|
| `production.deformation_analyze` | Compara la geometría evaluada en `frame` con `reference_frame`. |
| `production.deformation_compare` | Compara dos Actions en los mismos frames y frente a una referencia común de la primera Action. |
| `production.deformation_test_pose` | Propone y, con `dry_run=false`, mide stress poses temporales en controles explícitos. Crea checkpoint antes de cambiar pose. |
| `production.animation_deformation_qa` | Selecciona frames representativos y evalúa deformación en ellos. |

Las tres primeras selecciones se refieren al objeto `object_name`, que puede ser
una armature o una mesh. Stress requiere armature. Para armatures se encuentran
las meshes mediante modificadores Armature activos; `meshes` permite seleccionar
un subconjunto explícito. Nunca se selecciona otro personaje por proximidad.

Ejemplo de opciones de análisis:

```json
{
  "reference_frame": 1,
  "frame": 47,
  "meshes": ["CharacterBody"],
  "regions": {"knee": ["KneeRegion", "LowerLeg"]},
  "region_weight": 0.1,
  "self_intersection": true,
  "detail": "full",
  "limit": 30
}
```

`regions` enlaza nombres de región a grupos de vértices explícitos. La unión de
vértices cuyo peso alcanza `region_weight` forma la región. No interpreta nombres
de huesos como una prueba anatómica. Por defecto la referencia es el inicio de
escena; debe elegirse un frame cuya deformación sea aceptable. No cambia el rig a
rest pose automáticamente.

## Mediciones y umbrales

Se usa `evaluated_get(...).to_mesh()` y se libera siempre la mesh temporal. La
extracción de coordenadas emplea `foreach_get`; los arrays permanecen dentro de
Blender. Coordenadas de mundo multiplicadas por `scale_length` producen metros.
No se envían buffers de vértices al modelo.

| Medida | Significado / default |
|---|---|
| Desplazamiento máximo/medio | Distancia del vértice a la referencia en metros. Descriptiva: una flexión o movimiento rígido válido puede ser grande. |
| Edge collapse | Longitud actual/referencia menor que `collapse_ratio=0.25`. |
| Stretch | Longitud actual/referencia mayor que `stretch_ratio=2.0`. Configurable para rigs con stretch intencional. |
| Triangle collapse | Área actual/referencia menor que `area_ratio=0.1`. |
| Volumen aproximado | Suma de tetraedros con signo, solo para una superficie cerrada con orientación coherente. Alerta si ratio < `volume_ratio=0.6`. |
| Degeneración de referencia | Aristas <= `epsilon_m=1e-8`, triángulos <= epsilon², separados de los problemas introducidos. |
| Self-intersection | Opcional: pares de triángulos sin vértices compartidos detectados por BVH. Indicador de solape, no prueba completa de colisión. |

Los defaults buscan deformaciones graves y se declaran en cada resultado; no
pretenden umbrales universales de personaje. Una alerta requiere inspección
visual/contextual. Stretch configurado no se corrige ni declara fallo automático.
Las regiones parciales no reciben estimaciones de volumen ficticias. La integral
de volumen tampoco es un volumen físico fiable tras auto-intersección.

Se conserva la triangulación de la **referencia** para comparar áreas. Blender
puede cambiar la diagonal de un quad/ngon al deformarse sin cambiar su topología;
comparar sus triangulaciones reevaluadas daría falsos errores. Se comprueban
correspondencia de vértices, aristas y polígonos; si la topología cambia se rechaza
la comparación. Los polígonos no planos siguen siendo una aproximación.

## Stress poses y restauración

`poses` es una lista explícita, con 1..18 entradas:

```json
[
  {"region": "shoulder", "bone": "MappedUpperArm", "axis": "X", "angle_degrees": 65},
  {"region": "knee", "bone": "MappedLowerLeg", "axis": "X", "angle_degrees": 100}
]
```

Regiones admitidas: shoulder, elbow, wrist, hip, knee, ankle. El eje es local y
acepta X/Y/Z o un vector no nulo; ángulo ±180°. No hay grados anatómicos asumidos
para un eje desconocido. Cada entrada se prueba independientemente a partir de
la pose actual. Rechaza controles inexistentes, bloqueados o con constraints;
no fuerza un control IK ambiguo ni anula constraints para simular soporte.

`dry_run=true` valida y devuelve el plan. `false` crea un checkpoint y después
aplica poses temporales. No duplica Action porque **no escribe keyframes** ni
hornea la pose. En `finally` recupera Action/slot, frame/subframe, rotation modes,
transforms de objeto/huesos y delta transforms; compara el estado recuperado.
Un error de transporte devuelve restauración no verificada y el checkpoint;
nunca reintenta automáticamente.

Análisis/compare restauran esas mismas propiedades tras el muestreo. Rechazan
NLA activo, tweak, drivers del objetivo/datablock, Actions mezcladas, slots/layers
ambiguos y canales ajenos a transforms que no podrían restaurar. Las referencias
externas, handlers y callbacks arbitrarios no están certificados por estas
comprobaciones. Los originales permanecen intactos.

## Selección de frames de animación

`frames` selecciona hasta 32 frames exactos. Sin `frames`, el sistema evalúa
hasta `candidate_frames=128` candidatos uniformes y prioriza:

- extremos de rango;
- eventos de contacto **proporcionados** en `contact_frames` por la tool de contactos;
- rotación máxima respecto al primer candidato;
- flexión máxima entre hueso y padre;
- cambio angular rápido y desplazamiento de extremos.

Devuelve motivos por frame. No afirma detectar contactos por sí mismo. Se mide
geometría solo en los `max_samples=12` frames escogidos. El muestreo finito puede
omitir extremos entre candidatos; usar frames explícitos para verificar un
problema concreto. Compare prioriza candidatos de ambas Actions y construye una
lista común acotada; las dos versiones usan esos mismos frames. Una lista
`frames` explícita se conserva sin heurísticas adicionales.

## Presupuestos y resultados

Hasta 16 meshes, 12 regiones, 100000 vértices por mesh por defecto (máximo 250000),
500000 triángulos evaluados y 2 millones de muestras vértice-frame por defecto
(máximo 8 millones). Self-overlap se limita a 2000 triángulos; al excederlo declara
`budget_exceeded` y no finge una medición. Hasta 100000 muestras bone-frame para
selección de candidatos. Filtros y paginación `offset`/`limit` (máximo 200) acotan
filas e incidencias; `detail=summary` omite las filas por muestra.

Resultados incluyen `status`, `affected`, `state_restored`, `reference`,
`samples`, `metrics`, `issues`, `warnings`, `errors` y checkpoint cuando procede.
El wrapper MCP agrega `operation_id` y observabilidad común. Compare devuelve
`before`, `after` y deltas; las cuentas resumen de mesh evitan contar dos veces
la región y la mesh completa. Cada incidencia identifica frame, mesh, región,
severidad, métricas y confianza heurística.

## Vistas visuales adicionales

QA multiview conserva FRONT/SIDE/THREE_QUARTER por defecto. `LOW_ANGLE` ofrece una
vista baja; `FOOT` requiere `framing_bones` o `framing_meshes` explícito. No supone
qué hueso es pie. `framing_padding_m` (default 0.05 m) amplía el encuadre de huesos.
Máximo tres vistas por solicitud, cámaras compartidas antes/después, mismos
presupuestos de 72 imágenes/48 millones de píxeles y restauración M2.

## Verificación reproducible

`tests/unit/test_m3_deformation.py` prueba invariancia a movimiento rígido,
colapso, stretch configurable, volumen cerrado/orientado, regiones, datos no
finitos, parámetros inválidos y checkpoint previo a stress/timeout.

`tests/blender/m3_deformation_probe.py --blender <ruta>` genera meshes ponderadas
de hombro y rodilla con defectos controlados y Actions válidas. Evalúa geometría
real, selección del frame extremo 47, BVH, before/after, seis stress regions,
restauración exacta, rechazo de mapeo inexistente/control ambiguo, conservación de
Actions y hash del fixture, renders LOW_ANGLE/FOOT y benchmark. Todo sucede en
Blender factory aislado; no toca escenas de producción ni el socket del addon.
Antes de esas pruebas guarda y reabre la fixture: confirma que las seis Actions,
incluidas las auxiliares no asignadas, sobreviven mediante `use_fake_user=true`.
La regresión evita confundir existencia en memoria con persistencia en `.blend`.

La evidencia y métricas definitivas se consolidan en el reporte M3 del proyecto.
