# Planificación y validación de poses

`production.animation_plan_poses` devuelve intención antes de escribir claves.
`production.animation_generation_plan` la liga a rig, fuente y contexto en un
artefacto inmutable. El plan contiene `brief`, `poses`, `breakdowns`, `phases`,
`holds`, `contact_intervals`, capacidad y provenance. `apply=false` no crea Action.

Cada pose incluye frame entero, tiempo real `(frame-frame_start)/fps`, fase,
objetivo, soporte/contacto previsto y observaciones para silueta. Las fracciones
de fase preservan intención; la cuantización queda visible. Fases que terminan
en el mismo frame se rechazan.

## Contrato semántico del planner

| Campo dentro de `semantic` | Unidad e interpretación |
|---|---|
| `root_translation_m`, `root_yaw_deg` | Movimiento root corporal en metros/grados |
| `pelvis_offset_m`, `pelvis_pitch/roll/yaw_deg` | Compromiso y compensación corporal |
| `torso_pitch/roll/yaw_deg`, `head_pitch/yaw_deg` | Intención de torso/cabeza gruesa |
| `arm_swing_deg`, `arm_abduction_deg`, `elbow_flex_deg` | Valores por `left` y `right` |
| `feet[side].offset_m` | Desplazamiento mundial relativo al rest del efector, expresado en base corporal |
| `feet[side].anchor_m` | Igual a offset durante apoyo; `null` en swing |
| `feet[side].contact/contact_phase` | Intención de apoyo y fase; requiere medición |
| `feet[side].knee_flex_deg` | Intención de flexión, sujeta al solver/alcance |

`space="BODY_WORLD_REST_METRES"` impide sumar root dos veces a los targets IK.
La base lateral/forward/up y su orientación se validan. Rotaciones son
right-handed sobre esa base; no son instrucciones para escribir ciegamente
Euler locales de huesos deformadores.

`production.animation_generate_pose` usa controles compatibles, jerarquía y
constraints. `animation_validate_pose` comprueba alcance, stretch, apoyos,
penetración y proxies de balance; QA de malla/deformación complementa estos
indicadores. Un transform válido no demuestra que la pose sea plausible.

`animation_analyze_pose_silhouette` devuelve indicadores de separación/overlap
proyectados; no es un detector completo de clipping ni de topología de la
silueta renderizada. FRONT/SIDE/THREE_QUARTER y observación visual siguen siendo
necesarios. No hay una puntuación artística universal.

Un plan CUSTOM necesita el contrato completo y finito; apoyos sin anchor,
coordenadas no finitas, frames repetidos o capacidad excesiva fallan antes del
runtime. Ninguna lectura del planner depende de `bpy`.

## Ventana temporal íntegra

`validate_pose_window(plan, brief, stage, options=None)` es el contrato compartido
del host y del payload Blender. El plan completo necesita al menos dos poses y
sus endpoints deben coincidir exactamente con `frame_start`/`frame_end` del
brief. Todos los frames de poses y breakdowns, así como los intervalos opcionales
de holds, fases y contactos, deben ser finitos, no booleanos y estar dentro de
la ventana; los intervalos invertidos se rechazan. POSE valida también el frame
de su override o el índice seleccionado. No se recorta un plan fuera de rango.

El caso ATTACK con plan 1–45 frente a brief 1–31 falla explícitamente, igual que
una key interior o un breakdown fraccional fuera de ventana. Los 13 tests
puntuales del guard pasaron, incluido su payload bajo el validador upstream.
La calificación negativa real por MCP y la cobertura temporal del renderer se
registran aparte; este resultado unitario no declara el gate visual aprobado.
