# Matemática del retarget con poses de reposo distintas (AUD-002)

Implementación: `src/blender_mcp/production/mocap/runtime_bpy.py` (`mocap_rest_compatibility`,
`mocap_expected`). Verificación independiente: `tests/blender/m31_retarget_matrix_probe.py`.

## El defecto de v0.3.0

M3 transfería la orientación de cada hueso como un *delta* respecto al reposo:

```
Q_t = Q_s · R_s⁻¹ · R_t
```

`Q_s` es la orientación en espacio de armature del hueso fuente en el frame, y `R_s` y `R_t` son
las orientaciones de reposo (`bone.matrix_local`) de fuente y destino. Si las poses de reposo
difieren (fuente en T, destino en A), el delta conserva esa diferencia: un brazo que en la fuente
apunta a +Y termina 19,29° desviado en el destino (medido por la auditoría y reproducido en
`test-artifacts/m3.1-audit-regressions/baseline-v0.3.0-fcdb8f50.json`), y la verificación daba `pass`
porque comparaba contra la misma fórmula.

## La corrección

Para cada hueso mapeado, `d_s` y `d_t` son los ejes Y (dirección del hueso) en reposo, en espacio de
armature. La compensación es la rotación mínima que lleva la dirección de reposo del destino a la de
la fuente:

```
C   = rotation(d_t → d_s)
Q_t = Q_s · R_s⁻¹ · C · R_t
```

Aplicada al reposo del destino, `Q_s · R_s⁻¹` lleva la dirección de reposo de la fuente a su
dirección animada, y `C` antepone la alineación de reposo. Por construcción, el eje Y animado del
destino coincide con el de la fuente en cada frame. El giro sobre el propio eje (roll/twist) es el
del destino: la convención de roll de cada rig se conserva y se informa, no se "corrige".

Root y pelvis no se compensan: sus ejes son convenciones del rig, no poses. Mantienen la
transferencia por delta y el desplazamiento (root en espacio de mundo con `root_motion`, pelvis
rotando el desplazamiento local con la orientación animada del padre).

## Clasificación previa (propose)

`mocap_rest_compatibility` clasifica cada hueso antes de cualquier checkpoint:

| Clase | Condición | Efecto |
|---|---|---|
| `identical` | diferencia ≤ 1e-4° | `C` = identidad |
| `compensated` | 1e-4° < diferencia ≤ `rest_compensation_max_degrees` (60° por defecto, máximo 90°) | se aplica `C` |
| `incompatible` | diferencia mayor, o `rest_compensation=none` y diferencia > `rest_direction_tolerance_degrees` (2°) | `REST_POSE_INCOMPATIBLE`, propuesta no soportada |
| `convention` | root o pelvis | delta, solo se informa |

También se bloquea con `REST_POSE_INCOMPATIBLE` si los marcos corporales (dirección frontal y
vertical calculadas de pelvis→cabeza y de los pies) difieren más de 10°: la transferencia en espacio
de armature giraría al personaje.

Los umbrales 60°, 90°, 2° y 10° son valores de configuración elegidos, no medidos. Lo medido es
el comportamiento en los casos de la matriz siguiente.

## Verificación que no es tautológica

Al verificar, M3.1 ya no compara contra la misma fórmula. Mide en el mundo, por frame y por hueso,
el ángulo entre la dirección del hueso fuente y la del hueso destino (`POSE_CORRESPONDENCE_FAILED`
si supera la tolerancia), y aparte los eventos de pop o flexión introducidos (`INTRODUCED_LIMB_EVENTS`).

## Evidencia (Blender 5.2.1)

| Caso | v0.3.0 | v0.3.1 |
|---|---|---|
| AUD-002, fuente T / destino A, brazo 80° | `pass`, error de dirección 19,29° | `pass`, error 0,0° |
| Matriz (`m31_retarget_matrix_probe.py`): T→T, A→A, T→A, A→T, asimétrico, armature rotado, padre rotado, escala distinta | — | 10/10 casos correctos o rechazados de forma explícita; error independiente 0,0° en todos los compensados |

La medida es de dirección del hueso. Que el ángulo sea pequeño no demuestra que el movimiento sea
correcto visualmente: el roll, el contacto y la silueta requieren la QA visual (ver
[validación pública](validation.md), QA focalizada).
