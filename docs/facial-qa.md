# QA facial: técnica, geométrica, visual y ciclo de vida (M7)

Código: `facial/service.py`, `facial/scene_runtime.py`, `facial/curves.py`. Tools: `facial_deformation_qa`,
`facial_analyze_curves`, `facial_polish_curves`, `facial_render_closeup`, `facial_render_preview`, `facial_review`,
`facial_refine`, `facial_accept`, `facial_reject`.

## QA técnica de cada candidata

Corre al crear cada versión, en Blender, contra una **línea base solo-cuerpo en el mismo frame**. Así ni el movimiento
rígido de la cabeza ni las intersecciones previas del asset cuentan como defectos faciales. Muestrea ≤ 120 frames más
todos los picos de parpadeo y centros bilabiales.

| Hallazgo | Medida | Umbral (propuesta) |
|---|---|---|
| BLINK_INCOMPLETE | cierre del borde del ojo en el pico | < 0,90 |
| EYE_PENETRATION | aumento de profundidad del párpado en el globo | > 0,2 mm |
| EYE_SURFACE_CONTACT (aviso) | polígonos que cruzan facetas sin profundidad | > 0 |
| MBP_NOT_SEALED | hueco de labios en el centro bilabial | > 1 mm |
| LIP_CROSSING | hueco mínimo de labios | < −1 mm |
| MESH_INTERSECTION | polígonos faciales que pasan a intersecar otra malla | > 0 |
| MESH_INTERSECTION_EXTENDED (aviso) | la línea base ya intersecta | — |
| DEGENERATE_FACES | colapsadas (< 5 % del área) o invertidas (normal > 120°) | > 0 |
| GAZE_MISMATCH / EYE_LIMIT_EXCEEDED | dirección medida del ojo frente a la planificada / límites | > 1,5° |
| SHAPE_KEY_OUT_OF_RANGE, BODY_ACTION_CHANGED, STATE_NOT_RESTORED | — | cualquier caso |

A eso se suma el análisis de canales: picos, ruido fuera de ventanas protegidas, sobrepasos, velocidad, salidas
opuestas, validación de lip-sync, conflictos de capa, clamps y salidas sin soporte. `passed` significa que no hay
errores; los avisos se conservan. No hay puntuación de calidad.

Ventanas protegidas (no se suavizan ni cuentan como ruido): parpadeos, cierres M_B_P y F_V, sacadas y cambios de mirada.
`facial_polish_curves` propone primero y rechaza cualquier pulido que toque una ventana protegida.

`facial_deformation_qa` sin plan: cada control usable al 100 % y combinaciones explícitas (ej. mandíbula + cierre)
medidas en Blender.

## Renders

- Close-up: FRONT, THREE_QUARTER_LEFT, THREE_QUARTER_RIGHT y PROFILE con **la misma cámara**: objetivo, distancia, lente
  de 85 mm, sensor vertical de 24 mm, encuadre de ceja a barbilla con margen del 15 %, Workbench en la GPU (rasterizador
  OpenGL informado), luz STUDIO, color MATERIAL y cavity. Montaje 2×2 por frame. `compare` renderiza otra versión (o el
  solo-cuerpo) con cámaras idénticas.
- Preview temporal: todos los frames, FRONT + THREE_QUARTER_LEFT por defecto, APNG por vista y reproductor HTML local a
  velocidad normal, 1/2 y 1/4, con avance frame a frame. Capacidad: 901 frames por vista, 1.810 imágenes, 200 Mpx.
- Con cuerpo en movimiento la cámara sigue la traslación de la cabeza y mantiene la orientación.

Medido en la pasada final: 0,026–0,028 s por imagen a 256 px (302 imágenes en 8,0–9,1 s; 901 en 26,0 s).

## Revisión visual

`facial_review` exige el preview temporal completo de la candidata actual: revisor, observaciones artísticas, decisión
ACCEPT/REJECT/REFINE, referencia al artefacto y revisión temporal. Rechaza cualquier campo de puntuación. Escribe
`review.json` y `review.md` con dos secciones separadas, TECHNICAL FINDINGS (medidas) y ARTISTIC OBSERVATIONS.

## Refinamiento (máximo 3)

Objetivo medido (`curve_noise`, `spike_count`, `mbp_gap_max_mm`, `eye_penetration_max_mm`, `blink_closure_min`,
`mouth_gap_min_mm`, `new_intersections_max`, `flipped_max`, `lipsync_timing_error_ms`), cambios explícitos (partes o
pulido) y mejora mínima positiva. El ciclo se consume antes de intentarlo, aunque falle. Sin mejora → se revierte (la
versión queda marcada `REFINEMENT_REVERTED`). Con mejora → nuevo preview y revisión obligatoria antes de adoptarla. El
cuarto intento da `REFINEMENT_LIMIT` (probado en Blender).

## Ciclo de vida

PLANNED → FACIAL_CANDIDATE (una Action por versión, se conservan todas) → REVIEWED → ACCEPTED o REJECTED (terminales).
Aceptar exige QA sin errores, revisión ACCEPT del preview actual, integración con el cuerpo sin cambios y checkpoint.
Rechazar conserva la Action con lifecycle REJECTED y verifica la restauración.
