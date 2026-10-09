# Brief de animación

`production.animation_create_brief` normaliza intención estructurada sin crear
Action ni cambiar Blender. `create_brief` también acepta un brief normalizado
con `production.animation_brief.v1`; preserva provenance y rechaza inconsistencia
entre duración, FPS, frames y valores registrados.

Ejemplo de entrada explícita para un fixture compatible:

```json
{
  "type": "ATTACK",
  "profile": "HEAVY_HUMANOID",
  "attack_type": "OVERHEAD",
  "duration_s": 2.4,
  "fps": 30,
  "frame_start": 1,
  "dominant_side": "RIGHT",
  "root_mode": "IN_PLACE",
  "forward_axis": "+Y"
}
```

También se acepta `intent`, por ejemplo `heavy overhead attack`. El intérprete
es pequeño y determinista; una intención ambigua necesita tipo explícito.
No consulta servicios generativos. Campos estructurados explícitos prevalecen.

## Contrato

| Campo | Alcance inicial |
|---|---|
| `type` | IDLE, ATTACK, LOCOMOTION, TURN, HIT_REACTION, START_STOP |
| `profile` | HUMAN, HEAVY_HUMANOID, HEAVY_CREATURE, CREATURE, ASYMMETRIC_CREATURE, CUSTOM |
| `fps` | 30 o 60; ejecución exige coincidencia con la escena |
| `duration_s` | 1–10 segundos |
| `frame_start/end` | Enteros; end se deriva de duración × FPS |
| `root_mode` | IN_PLACE o ROOT_MOTION |
| `forward_distance_m`, `speed_m_s` | Intención física en metros; valores incoherentes en locomotion se rechazan |
| `mass_asymmetry` | Masas relativas izquierda/derecha, no modificación de geometría |
| `impact_direction` | Dirección nombrada o vector no nulo normalizado |
| `phase_fractions` | Reparto explícito y estrictamente creciente de fases |
| `contact_requirements` | Intención de contactos; no sustituye confirmación geométrica |
| `custom_pose_plan` | Plan semántico completo, finito y acotado; no código Python |

Aliases `animation_type`, `duration_seconds` y `root_motion` se normalizan; las
versiones incompatibles del mismo dato se rechazan. Los frames contienen ambos
endpoints. `actual_duration_s` y `quantization_error_s` hacen visible la
cuantización al frame, sin modificar FPS.

Cada valor tiene `provenance`: `user_requirement`, `inferred_default` u
`optional_proposal`, con valor y derivación cuando corresponda. Un default es
un punto inicial configurable, nunca una preferencia aprobada del usuario.
La interpretación del texto queda identificada. Cambiar un valor normalizado
sin actualizar su provenance se rechaza.

Los vectores están en metros y las rotaciones en grados alrededor de ejes
corporales declarados, no ejes locales supuestos de cada hueso. El runtime
inspecciona y convierte rest transforms, escala y controles. No se asigna alta
confianza por recibir nombres o una puntuación del llamador.
