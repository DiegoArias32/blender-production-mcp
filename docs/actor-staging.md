# M10 — Colocación de actores (`cinematic_stage_actors`)

Área D de M10. Código: `src/blender_mcp/production/cinematic/performance.py` (`pf_stage`, `normalize_staging`).
Contrato general: [arquitectura cinematográfica](cinematic-architecture.md) §5. Sincronización de clips:
[multi-character-synchronization.md](multi-character-synchronization.md).

## Qué hace

Coloca actores ya registrados en la candidata con **deltas de objeto**, nunca tocando `location/rotation` ni la
Action:

```
options = {actors: [{actor_id, location: [x, y, z] (m, por defecto 0), yaw_deg (por defecto 0)}]}
```

- `location` se **suma** a `delta_location`; `yaw_deg` gira alrededor de Z mundo y se compone a la izquierda del
  delta de rotación que ya hubiera (`R_nuevo = Rz(yaw) @ R_delta_previo`). Con `rotation_mode` QUATERNION se escribe
  `delta_rotation_quaternion`; con modos Euler, `delta_rotation_euler` en el mismo orden que el objeto.
- Cada actor genera una fila `touched` de tipo `delta` (valor previo) **antes** de cambiar nada; `cinematic_reject`
  la deshace. Llamadas sucesivas son acumulativas (cada una añade su fila).
- `apply=false` ejecuta el mismo payload en modo `dry_run`: todas las validaciones, ningún cambio.

## Fórmula medida y por qué hay rechazos

Medido en Blender 5.2.1 (fase de comprensión, `docs/cinematic-architecture.md` §5):
`world = T(loc + delta_location) @ (R_delta @ R_anim)`. El delta de rotación **no** gira la trayectoria de una
`location` animada. Por eso:

| Situación | Código | Motivo |
|---|---|---|
| actor con padre o constraints | `ACTOR_STAGING_UNSUPPORTED` | el delta no coloca el objeto de forma predecible |
| `AXIS_ANGLE` con giro | `ACTOR_STAGING_UNSUPPORTED` | no existe canal delta de eje-ángulo |
| girar un actor cuya Action (activa o en NLA, también pistas silenciadas) anima `location` a nivel de objeto | `ROOT_MOTION_AMBIGUOUS` | giraría el cuerpo pero no su recorrido |
| Action o driver sobre `delta_*` | `ACTOR_DELTA_ANIMATED` | la colocación pelearía con la animación |
| actor con secundario M8 integrado cuya copia no cuelga del actor | `SECONDARY_STAGING_UNSUPPORTED` | la copia PC2 se quedaría atrás (límite declarado) |

La traslación pura (yaw 0) sí se admite con `location` animada: el delta se suma sin girar nada.

## Verificación (medida, no supuesta)

El payload muestrea la matriz mundo **evaluada** de cada actor en 5 frames de la línea de tiempo (inicio, cuartos,
fin) antes y después del cambio, dentro de `temp_override(scene=candidata)`, y comprueba
`t_después = t_antes + location` y `R_después = Rz(yaw) @ R_antes`. El error de rotación se calcula como
`2·asin(‖Ra−Rb‖_F / 2√2)`, exacto para matrices de rotación y bien condicionado cerca de 0 (el `acos` de un producto
de cuaterniones en float32 da ruido de ~0,02°). Si el error supera la tolerancia, la respuesta es
`status=error` con `STAGING_VERIFICATION_FAILED` (el cambio queda registrado y es reversible).

Además informa de **dimensiones** (caja alineada a ejes del objeto del actor y sus mallas evaluadas en el primer
frame muestreado) y de la **separación mínima** entre cada par de actores del plan (distancia entre cajas, frame
donde ocurre y si las cajas se solapan). Es una cifra geométrica, no una prueba de colisión: la QA de colisiones es
del área F. Sólo cuentan objetos que **evalúa la escena candidata**: una malla registrada del actor que se excluyó al
crear la candidata (la fuente de un secundario M8) se sustituye por su copia M10 (`separation.objects` dice qué objetos
entraron). Si se contara la excluida, se leería su matriz obsoleta de la escena fuente (medido abajo).

Todo payload que evalúa tiempo captura y restaura **todos** los actores del plan (no sólo los colocados) y también
los objetos propios de `_CIN`: evaluar la escena candidata vuelca valores animados en los originales compartidos y en
los canales crudos de los objetos propios (cámaras con keys, influencia keyeada de constraints de props), que la
huella de la candidata lee.

## Hechos medidos (probe `tests/blender/m10_performance_probe.py`, Blender 5.2.1, 88/88 checks)

Fixture inline controlada (dos armatures de 6 huesos, Actions LINEAR de 60 frames, 30 fps, actores registrados con el
`cinematic_register_actor` real y todos los handlers M10 cargados), informe
`informe local privado; véase validation.md` (cifras iguales en
`bpm10-performance-8`, `-9` y `-10`; la separación sólo desde `-10`):

- ActorA: `location=(0; −1; 0)`, `yaw=90°`; ActorB: `location=(0,5; 0; 0)`. Error máximo medido en 5 frames:
  **0,0 m y 0,0°** (float32 exacto en esta construcción). Lectura independiente (fuera de la tool): cabeza del hueso
  `hand_R` en el frame 1 pasa de (0,45; 0; 1,2) a (3,4e−8; −0,55; 1,2) = `Rz(90°)·p + (0,−1,0)` exacto.
- `delta_rotation_euler` usa el orden de `rotation_mode` del objeto: con ZXY, error 3,0e−8 frente a 0,239 si se
  supone XYZ (sonda de exploración propia).
- Rechazos medidos sin cambiar la candidata ni el ledger (huella de candidata y `revision` iguales):
  `ACTOR_STAGING_UNSUPPORTED` (empty con padre), `ROOT_MOTION_AMBIGUOUS` (empty con `location` animada y yaw 30°),
  `ACTOR_DELTA_ANIMATED` (Action sobre `delta_location`), `SECONDARY_GATE_ORDER` (integrar el secundario de B después
  de colocarlo).
- Separación A–B: **2,986 m** como mínimo (frame 31) contando la copia M10 del colgante de B; dimensiones de A
  0,28 × 0,80 × 1,80 m y de B 0,80 × 0,34 × 1,80 m. En la ejecución anterior (`bpm10-performance-9`), contando el
  colgante excluido de la candidata, salía 2,895 m: ese objeto no lo evalúa la candidata, así que su matriz no sigue los frames muestreados (no se
  midió cuánto contribuye cada frame a la diferencia).
- Coste en la fixture: 0,18–0,21 s por llamada con checkpoint. No extrapolable a rigs reales.
- `cinematic_reject` devolvió los deltas a 0 y la huella de la escena fuente idéntica a la del plan.

## Propuestas (no medidas en personajes reales)

`THRESHOLDS` (todas `threshold_source='PROPOSAL'`): posición 1e−5 m y rotación 1e−3°. Las construcciones exactas
de la probe dan ~1e−7; el margen es de dos órdenes de magnitud.

## Límites declarados

- Sólo giro alrededor de Z mundo (`yaw_deg`). Inclinar un actor no está soportado.
- La separación es por cajas alineadas: dos actores con cajas solapadas pueden no tocarse.
- Empties sin geometría informan dimensiones 0.
- Un actor con secundario M8 integrado cuya copia no cuelga del actor no se puede recolocar (ver
  [sincronización](multi-character-synchronization.md#3-secundario-m8-aceptado)).
- La lista `meshes` del registro de un actor sigue nombrando la fuente M8 excluida de la candidata (el registro mide
  la escena fuente). Esta tool la sustituye por la copia; las demás áreas que usen `meshes` deben hacer lo mismo
  (pendiente fuera del área D).
