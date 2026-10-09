# Semántica facial basada en evidencia (M7)

Código: `facial/zones.py` (compartido literal con Blender), `facial/semantics.py`.

## Salidas semánticas

44 salidas independientes del rig: párpados (`lid_close/wide/raise_L/R`), cejas (`brow_inner_raise`,
`brow_outer_raise`, `brow_lower`), mejillas y nariz, mandíbula (`jaw_open`, `jaw_forward`, `jaw_side_L/R`), labios
(`lips_close`, `lip_pucker`, `lip_funnel`, `lip_press`, `lip_roll_lower/upper`, comisuras arriba/abajo/estiradas,
labio superior/inferior), ojos (`eye_yaw/pitch_L/R` en grados), cabeza/cuello (grados) y lengua. Las salidas
normalizadas van de 0 a 1.

## De la medida a la semántica

Cada probe produce estadísticas del desplazamiento: reparto de energía por zona (párpados, cejas, nariz, mejillas,
comisuras, labios, mandíbula), dirección media normalizada, lado (`left_share`), acercamiento a la boca y
convergencia de labios. Reglas explícitas (`rule_scores`) convierten eso en candidatos con puntuación base y motivo.
Los nombres **solo ajustan**: +0,08 si coinciden, −0,30 si el lado del nombre contradice la deformación
(`SIDE_CONFLICT`). Un nombre sin deformación no crea nada.

Topes de confianza:

- áreas no adyacentes deformadas a la vez (`MIXED_AREAS`) → máximo 0,45;
- valor puesto por un driver (`DRIVEN`) → máximo 0,2: nunca se keyea un canal con driver;
- umbral de uso: 0,60 (`USABLE_CONFIDENCE`).

## Mapeo

Por salida se elige el control mejor puntuado (los huesos de control ganan por 0,005 a los de deformación; un hueso
de deformación copiado por un control nunca se anima). Se guardan confianza, evidencia y hasta tres alternativas.
Recetas de varios controles (`lips_close`, `pucker`, `funnel`) en rigs de huesos. Ojos:

| Método | Cuándo | Cómo se resuelve |
|---|---|---|
| `bone_rotation` | canal de rotación del hueso del ojo | grados por unidad medidos en el probe |
| `shape_key_pair` | claves de mirada en ambos sentidos | clave positiva/negativa = grados / grados al 100 % |
| `aim_target` | ojo con Damped/Track To a un hueso objetivo | se mueve el objetivo en sus ejes de reposo |
| `shape_key_single_direction` | solo un sentido medido | **no usable** (`EYE_MAPPING_LOW_CONFIDENCE`) |

## Híbridos

Una salida nunca se conduce por dos sistemas: si clave y hueso la cubren, mandíbula y ojos prefieren el hueso (movimiento
rígido) y el resto la mayor confianza medida. El perdedor queda en `not_driven` con el motivo. Fixture híbrida: hueso de
mandíbula elegido, `Jaw_Open_Shape` sin conducir, corrector con driver informado.

## Compatibilidad por capacidad

expresiones, mirada, parpadeo, seguimiento de párpado, cejas, mandíbula, labios, lip-sync, mocap, microexpresiones,
cabeza/cuello y lengua: `SUPPORTED`, `LIMITED` o `UNSUPPORTED` con motivo. La calibración puede rebajar una capacidad
con su medida (ej. `LIP_SEAL_INSUFFICIENT` → lip-sync LIMITED en la fixture de huesos: el cierre llega a 4,24 mm y el
reposo deja 4,92 mm).
