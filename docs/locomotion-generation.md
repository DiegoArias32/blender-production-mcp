# Generación de locomotion

`production.animation_generate_locomotion` planea WALK, HEAVY_WALK, RUN o
CUSTOM_CYCLE con speed, stride, duración, root mode y perfil. El soporte inicial
es un ciclo bípedo explícito sobre rig compatible; no un sistema universal de
locomoción o cuadrúpedos.

El planner genera CONTACT, DOWN, PASSING, UP y sus fases opuestas; incluye
pelvis, piernas, torso y counter-motion de brazos. La intención se convierte
mediante controles validados. Una pierna FK sin adapter de foot locking se
informa como limitada/no verificada; no se afirma que su pie quedó anclado.

## IN_PLACE y ROOT_MOTION

IN_PLACE inicial es una marcha/paso estacionario con pies de apoyo fijos y
arco de swing hacia adelante y vuelta al anchor. Es una limitación explícita;
no representa treadmill locomotion con traslado hacia atrás del pie durante
contacto. Speed describe intención de referencia, no traslación real del root.

ROOT_MOTION avanza según distancia/duración; cada anchor de stance permanece
en mundo y el siguiente apoyo avanza con el ciclo. Speed y distancia explícitos
contradictorios se rechazan. Stride/reach deben comprobarse con longitudes del
rig; el planner no demuestra que una zancada sea físicamente alcanzable.

La duración inicial contiene exactamente un ciclo. `cycle_duration_s` distinto
de `duration_s` se rechaza; no se multiplican ciclos silenciosamente. RUN es
limitado: incluye vuelo previsto, pero no simula dinámica ni aterrizaje físico.

## QA de ciclo

- Pose, pelvis y root relativo coinciden en endpoints dentro de tolerancias.
- Root delta corresponde a IN_PLACE o a la distancia ROOT_MOTION solicitada.
- Contactos y sliding tienen evidencia y umbrales configurables en metros.
- Velocidad/continuidad angular, extremidades y deformación se vuelven a medir.
- Preview temporal se reproduce incluyendo el cierre del ciclo; revisar el
  salto entre final e inicio, no sólo frames aislados.

Las limitaciones y métricas reales del demo se registran en el informe de M6;
el mero hecho de producir un ciclo no demuestra walk de producción aprobado.

## Continuidad y blocking

La QA de loop compara posición y orientación corporal relativas a root en los
extremos (C0), y las diferencias vectoriales de velocidad lineal/angular entre
el primer y último intervalo de un frame (C1 muestreada). Los defaults son
0,01 m y 1° para C0; 0,10 m/s y 10°/s para C1. Son tolerancias explícitas,
configurables y registradas; no una prueba matemática de continuidad entre
todas las muestras ni una calificación artística.

Sólo cuando la Action realmente tiene etapa BLOCKING/POSE y todos sus keys son
CONSTANT, C1 se conserva como medición advisory. La QA devuelve
`loop_boundary_continuity=ADVISORY_STEPPED_BLOCKING` y `advisory_gates`; exige
mediciones finitas/no negativas y mantiene C0, contactos, alcance, stretch,
deformación y quaternion inválido/sign flip como gates. Una revisión de ese
blocking permite avanzar; la aceptación final exige QA continua. SPLINE y
CURVES no reciben esta excepción.

La flexión del codo en locomotion usa `12 + 4*sin(4*pi*fraction)^4` grados.
Conserva los eventos de 12–16° y aplana velocidad/aceleración cerca del cierre.
Se corrigió una cúspide real del perfil anterior con valor absoluto, comprobada
en las curvas de antebrazo y en la orientación evaluada de las manos. El primer
intento con seno cuadrado mejoró la diferencia angular, pero todavía falló;
su reporte se conserva. No se elevó el umbral de 10°/s ni se cambiaron tangentes
del runtime para lograr el resultado posterior.

## Calibración de contactos

Los defaults heredados (`contact_height_m=0.03`, `contact_speed_m_s=0.25`) buscan
posibles contactos según proximidad y velocidad **vertical**. No demuestran
contacto físico. Una marcha lenta con swing de 0,04 m puede permanecer dentro
de esa banda mientras el pie está en vuelo, produciendo suspected sliding.
No elevar `sliding_tolerance_m` para ocultarlo ni reportar cero porque el plan
declara un apoyo.

Para las dos fixtures controladas de marcha se verificaron la geometría del
plano horizontal, el marcador en la cabeza del pie y la suela rígidamente
ponderada: los tres están a Z mundial 0,08 m, con altura relativa cero. La QA
específica usa `ground_height_m=0.08`, `contact_height_m=0.005` y
`contact_speed_m_s=0.02`; conserva `sliding_tolerance_m=0.01`, C1 lineal
0,10 m/s y C1 angular 10°/s. Esta calibración de 5 mm y 2 cm/s corresponde a
estas fixtures y debe revisarse para otro personaje/superficie.

En cada pie se midieron 35 muestras de apoyo con la calibración, además de
validar anchors/contactos del backend. El diagnóstico con defaults se guarda
por separado, sobre la misma candidata, e informa el suspected sliding que
clasifica durante vuelo bajo. No se sustituye ni elimina ese diagnóstico.

## Evidencia técnica puntual

La regeneración en Blender 5.2.1 de las dos marchas, exclusivamente sobre la
fixture retenida, terminó con 32 checks PASS. BLOCKING y SPLINE pasaron sus
gates aplicables; BREAKDOWNS conservó mediciones y pasó posteriormente a
SPLINE. No se afirma aceptación visual, cierre de M6 ni calificación de
personajes reales a partir de este probe.

| Fixture | C1 angular anterior → posterior | C1 lineal posterior | Sliding calibrado | Suspected sliding con defaults |
|---|---:|---:|---:|---:|
| WALK IN_PLACE | 19,459 → 3,168°/s | 0,08386 m/s | 7,83×10⁻⁷ m | 0,01500 m, FAIL |
| HEAVY_WALK ROOT_MOTION | 19,462 → 3,171°/s | 0,08719 m/s | 7,97×10⁻⁷ m | 0,02104 m, FAIL |

Root permaneció fijo en IN_PLACE y avanzó aproximadamente 0,08 m en el eje
forward −Y de la segunda fixture. Todas las Actions preexistentes y el SHA256
del `.blend` fuente quedaron iguales; las nuevas Actions se conservan en otro
`.blend`. Reporte y diagnóstico default:

- `test-artifacts/m6/walk-quartic-calibrated-20261004T031000-06c9/report.json`
- `test-artifacts/m6/walk-quartic-calibrated-20261004T031000-06c9/default-contact-diagnostics.json`
- `test-artifacts/m6/walk-quartic-calibrated-20261004T031000-06c9/walk-candidates.blend`

La evidencia anterior, incluidos los fallos, está en
`loop-boundary-20261004T024000-e591/` y
`walk-seam-regeneration-20261004T030000-2f47/` bajo el mismo directorio M6.
