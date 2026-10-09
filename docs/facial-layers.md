# Capas faciales (M7)

Código: `facial/tracks.py`, `facial/layers.py`, `facial/resolve.py`, payloads `WRITE`/`CANDIDATE_QA`. Tool:
`facial_analyze_layer_conflicts`.

## Capas lógicas y partes

| Capa | Partes |
|---|---|
| BASE_EXPRESSION | `expression`, `asymmetry`, `jaw`, `mocap` |
| BROWS | `brows`, `speech_brows` |
| GAZE | `gaze`, `saccades`, `lid_follow` |
| BLINK | `blinks` |
| SPEECH | `speech` (+ peso de habla) |
| MICROEXPRESSION | `micro` |
| HEAD_NECK | `head`, `speech_head` |

Cada tool de generación **regenera solo su parte**; las demás se conservan. Cada parte guarda parámetros, procedencia y
hallazgos en el ledger.

## Combinación con reglas explícitas (`combine`)

- Grados (ojos, cabeza, cuello): mirada + cabeza/cuello + expresión.
- Boca durante el habla: `habla·w + expresión·(1 − w·(1 − residual))`, residual 30 % (comisuras 70 %).
- Párpado: `cerrado + (1 − cerrado)·parpadeo`, con "cerrado" = expresión + seguimiento de mirada; muy abierto × (1 − parpadeo).
- Normalizados limitados a 0–1.
- `BODY_OWNS` descarta cabeza/cuello faciales.

Cada choque se registra con regla, frames e intensidad: `expression_closed_lips_vs_open_vowel`, `blink_vs_wide_eyes`,
`head_neck_owned_by_body`. Ninguna capa sobrescribe a otra en silencio.

## Representación en Blender

Una Action solo admite una capa en Blender 5.2.1 (medido), así que las capas lógicas se combinan en el host y la
candidata es **una Action nueva por versión**:

- un slot OBJECT para los huesos faciales y un slot KEY por cada datablock Key;
- una capa y una tira, con F-Curves escritas con `foreach_set`, interpolación LINEAR y verificación de lectura;
- nombre determinista `M7F_<generación>_v<n>`; si ya existe → `FACIAL_CANDIDATE_COLLISION`, nunca se sobrescribe;
- escritura troceada en bloques de ≤ 120 KB de parámetros.

Evaluación: pistas NLA temporales en la armature y en cada Key; la Action del cuerpo sigue activa y sin cambios. Como
quitar una pista NLA deja la pose evaluada, cada evaluación captura y restaura pose y valores de shape keys.

## De semántica a controles (`resolve`)

Claves escaladas a su `full`, huesos con signo y límites medidos, recetas de varios controles, controles bilaterales
compartidos (media y asimetría perdida informada), ojos por método, cabeza/cuello convertidos a los ejes de reposo del
hueso (quaternion o XYZ), cierre de labios con el modelo medido. Errores que bloquean: `DRIVER_CONFLICT` y
`BODY_FACE_CHANNEL_CONFLICT` (el cuerpo ya anima ese hueso).
