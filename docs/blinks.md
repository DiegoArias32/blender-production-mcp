# Parpadeos y párpados (M7)

Código: `facial/blinks.py`, `facial/layers.py`. Tools: `facial_analyze_eyelids`, `facial_generate_blink`,
`facial_plan_blinks`, `facial_generate_lid_follow`.

## Forma

| Tipo | Duración | Intensidad | Cierre |
|---|---|---|---|
| FULL | 0,25 s | 1,0 | 35 % del tiempo |
| PARTIAL | 0,20 s | 0,6 | 40 % |
| SLOW | 0,60 s | 1,0 | 40 % + retención 0,08 s |

Duraciones de 100–400 ms y cierre más rápido que apertura, según valores habituales en la literatura de parpadeo
(propuestas para fixtures, no medidas de un personaje). La asimetría (0–0,5) retrasa y debilita el párpado que va
detrás; nunca es un cambio rígido. **El pico cae en un frame entero**: a 30 FPS un pico fraccionario no se vería nunca.

## Planificación

`plan_blinks`: semilla, intervalos aleatorios acotados alrededor de una tasa de contexto (reposo 17/min, habla 22,
atento 8, cansado 25, tenso 20; Bentivoglio et al. 1997 como referencia de rango), mínimo 1,2 s entre parpadeos, más
lentos si cansado y menos frecuentes en ira/sospecha (propuestas). Los cambios de mirada ≥ 15° llevan un parpadeo al
inicio (demo B: parpadeo en el frame 31, el del cambio). No es "cada N segundos": el coeficiente de variación de los
intervalos se informa (> 0 en los tests).

Los parpadeos solapados se combinan con el máximo por frame, no intercalando claves.

## Capacidad medida del rig

`facial_analyze_eyelids` cierra cada párpado al 100 % en Blender y mide el hueco del borde del ojo:

- fixture limpia: hueco 10,27 mm → cierre 1,16 (el párpado pasa 1,6 mm);
- `blink_defect`: cierre 0,55 → `BLINK_INCOMPLETE` (umbral 0,90, propuesta);
- `eye_penetration`: el párpado entra 0,543 mm más en el globo → `EYE_PENETRATION` (umbral 0,2 mm, propuesta).

En la candidata, la QA mide el cierre en el pico de cada parpadeo completo. A mitad de parpadeo, 1–2 polígonos cruzan
facetas del globo sin profundidad medible: se informa como `EYE_SURFACE_CONTACT` (aviso), no como penetración.

## Combinación con otras capas

`lid_close = cerrado + (1 − cerrado) × parpadeo`, donde "cerrado" es expresión + seguimiento de mirada. El parpadeo gana
a los ojos muy abiertos (`blink_vs_wide_eyes`, registrado como conflicto resuelto).
