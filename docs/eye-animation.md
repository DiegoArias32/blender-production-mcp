# Ojos: mirada, convergencia, sacadas y coordinación con la cabeza (M7)

Código: `facial/eyes.py`, `facial/layers.py`, payload `HEAD_TRACK`. Tools: `facial_analyze_eyes`,
`facial_generate_gaze`, `facial_generate_saccades`, `facial_coordinate_gaze`, `facial_generate_lid_follow`.

## Geometría

Ángulos en grados en el marco facial (yaw + a la izquierda del personaje, pitch + arriba). **Cada ojo apunta al mismo
punto 3D**: la convergencia sale de la geometría y nunca se copia el Euler de un ojo al otro. Objetivos:

- `point` (mundo), `angles`/`offscreen` (relativos a la cara en reposo, fijos en el mundo), `camera` (punto delante de la
  cara o, con `use_scene_camera`, la cámara de la escena muestreada por frame), `object` (objeto muestreado por frame).
- Fuera de cámara por defecto: 18° (propuesta, dentro de rangos de ±25–30°; 25° quedaba en el límite medido del rig).

Comprobaciones: `EYE_TARGET_OUT_OF_RANGE` (con la parte de los ojos **después** del reparto con la cabeza),
`IMPOSSIBLE_CONVERGENCE` (> 25° de vergencia, propuesta; se informa antes que el rango porque lo explica),
`CROSS_EYE_RISK` (> 12°), `DIVERGENT_GAZE`, `TARGET_BEHIND_FACE`. Límites: los del rig si existen, el rango medido de las
claves de mirada, o ±30° yaw / +20°/−25° pitch (propuesta).

## Coordinación ojos → cabeza → cuello

Reparto configurable (`NATURAL` 65/28/7 %, `EYES_ONLY`, `HEAD_LEADS`, `RESTRAINED` o ratios propios). Los cambios
pequeños (< 8° yaw y < 6° pitch) son solo de ojos. Los ojos hacen la sacada (21 ms + 2,2 ms/°, aproximación de la
"main sequence"), la cabeza arranca 0,1 s después, dura 0,35 s con ease, y **los ojos contrarrotan** mientras gira
(compensación vestíbulo-ocular, dentro de sus límites). Con `BODY_OWNS` los ojos hacen el 100 %.

Medido (demo B, objetivo 30° izquierda y 12° abajo a 0,6 m, FACE_OWNS): la mirada llega al 90 % en el frame 34 y la
cabeza en el 43; vergencia medida 3,35°; error de mirada medido máximo 1,153° y medio 0,25° (tolerancia 1,5°). Es el
mayor de todas las pruebas: el giro de cabeza con pitch se aproxima como suma de ángulos.

## Mirada anclada al mundo con el cuerpo en movimiento

Si una Action de cuerpo mueve la cabeza, `HEAD_TRACK` muestrea la transformación real de la cabeza (solo cuerpo) por
frame y los ojos se resuelven frame a frame para seguir fijando el objetivo. Los frames que no caben en los límites se
informan como error. Demo D: el golpe de M6 mueve la cabeza hasta 11,4 cm y 6,6°; la mirada medida se desvía 0,089° de
la planificada.

## Sacadas de fijación

Con semilla, acotadas y no perpetuas: frecuencia ≤ 4 Hz (por defecto 1,6), amplitud ≤ 8°, estilos de atención
ATTENTIVE/SEARCHING/NERVOUS/TIRED y cluster alrededor de la mirada base. Se suman a la mirada; si salen de los límites se
informa `EYE_LIMIT_EXCEEDED_BY_SACCADES`.

## Seguimiento del párpado

Los párpados superiores bajan al mirar abajo (ganancia 0,35 por pitch completo, propuesta) y se abren un poco al mirar
arriba. Demo B: cierre medido 0,0 → 0,13 al mirar 12° abajo; visible en la revisión.

## QA medida de la mirada

En cada frame muestreado la QA mide la dirección real de cada ojo evaluado en Blender, quitando el movimiento rígido
de la cabeza, y la compara con los grados planificados (`GAZE_MISMATCH` > 1,5°, propuesta). En las 18 fixtures el
error máximo medido fue 0,000–0,259°.
