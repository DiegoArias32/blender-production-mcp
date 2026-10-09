# Métricas de animación

Las métricas son observaciones reproducibles. Ningún número pretende resumir
si una animación es bella, AAA o artísticamente correcta. Los defaults son
puntos de partida configurables y se devuelven junto al resultado.

## Unidades y muestreo

Posiciones evaluadas se transforman a mundo y multiplican por
`scene.unit_settings.scale_length` para metros. Tiempo se deriva de
`fps / fps_base`; duraciones son `(último_frame-primer_frame)/FPS`.
Las velocidades están en m/s, aceleraciones m/s² y variación de aceleración
en m/s³. La rotación usa grados en los informes. Con unidades NONE se advierte
que la escala física necesita confirmación.

Los análisis insights M2 limitan muestras a 1000 por defecto (máximo 2000),
selección a 32 bones y evaluación a 64000 pares bone/frame. Restauran frame y
subframe incluso ante errores normales. No envían trayectorias completas al
modelo; informan summaries y eventos limitados. Steps mayores pueden omitir
contactos, picos y vueltas completas entre muestras.

## Contactos y sliding

Contacto requiere cercanía al plano horizontal Z y baja velocidad **vertical**.
No filtra velocidad horizontal: eso escondería precisamente el sliding.

| Opción | Default | Interpretación |
|---|---:|---|
| `ground_height_m` | 0 | Plano de contacto del marcador, no suela inferida. |
| `contact_height_m` | 0.03 m | Tolerancia de altura del marcador. |
| `contact_speed_m_s` | 0.25 m/s | Límite de velocidad vertical. |
| `sliding_tolerance_m` | 0.01 m | Máxima excursión horizontal tolerada. |
| `min_contact_frames` | 3 | Duración mínima en frames originales. |

`translation_during_contact_m` es el máximo alejamiento XY desde la primera
muestra del apoyo; la misma medida se ofrece en cm. `net_translation_m` mide
solo diferencia inicio/final y `horizontal_path_length_m` acumula el recorrido.
Un pie que se aleja y vuelve tiene desplazamiento neto cero pero sliding real.
Un epsilon de comparación de 1e-7 m evita que ruido float32 decida el verdict
justo en el threshold; no constituye tolerancia anatómica.

La extensión M2 añade altura media, velocidad vertical media/máxima, confidence
de contacto y evidencias de jerarquía, posición, simetría, deformación, targets
IK, nombres y comportamiento observado. Confidence es un score de evidencia
no calibrado. Una selección explícita certifica identidad del marcador, no
anatomía ni contacto físico. Sin apoyos suficientes devuelve inconclusive.

El corrector reporta `before_sliding_cm`, `after_sliding_cm`, mejora absoluta y
porcentual, canales/keys afectados y controles de protección. Defaults de
verificación: posición protegida 0.0001 m, cambio de longitud 0.002 m, residual
IK 0.01 m y aumento del paso angular articular 10°. No son garantías de estética;
se revisan junto a los resultados y QA visual.

El corrector protege orientación del control con máximo error de 0.05°. También
exige drift final dentro de tolerancia y una reducción mínima del 80% cuando el
drift inicial supera 1e-6 m. Ese requisito interno conservador se informa como
criterio de verificación, no como una regla universal para todas las animaciones.

## Quaternions

Normalizar WXYZ y medir `2*acos(abs(dot(q1,q2)))` distingue diferencia física del
signo: q y −q tienen 0° de diferencia. Dot negativo marca candidato de hemisferio;
un cambio keyed mayor de 90° requiere revisión, no demuestra un error. Los
grupos incompletos, norma cero y valores no finitos se reportan separadamente.

Corrección compara fuentes/candidatos con orientación muestreada y tolerancia
angular default 0.05°. Tiempos densos son una comprobación limitada; giros largos
intencionales y muestras indefinidas requieren interpretar las advertencias.

## Curvas

| Opción | Default | Unidad/uso |
|---|---:|---|
| `tolerance` | 0.000001 | Unidad nativa del canal; puede sobrescribirse por canal. |
| `angular_tolerance_degrees` | 0.05 | Quaternion; máximo permitido 5°. |
| `spike_factor` | 6 | Factor relativo contextual; mínimo 2. |
| `spike_min_amplitude` | 0.01 | Unidad nativa, evita llamar spike al ruido numérico. |
| `noise_threshold` | 0.005 | Unidad nativa; candidato a revisión. |
| `samples_per_interval` | 8 | 4–64 muestras, dentro del presupuesto total. |

`per_channel_tolerances` usa claves `data_path[array_index]`, por ejemplo
`pose.bones["Pelvis"].location[0]`. Se reportan redundancia, picos aislados,
cambios de derivadas/aceleración, interpolación, ruido alternante, overshoot y
discontinuidades de rotación. Movimiento rápido constante no equivale a spike.
`max_error` y `average_error` comparan curvas fuente/candidata dentro del alcance
verificado. La tolerancia debe elegirse según posición, escala o radians; no
suponer que todos los canales están en metros.

## Root y motion quality

El root automático se propone por jerarquía/cobertura de descendientes; se
puede elegir `root_bone` o `root_object` explícitamente. La posición mundial
del bone ya incluye al objeto y padres: no se suman sus desplazamientos dos veces.

Se reportan XYZ neto, distancia horizontal y total recorrida, variación vertical,
velocidad media/pico, aceleración, variación de derivadas y rotación angular.
Euler XYZ acumulado es una descripción de ejes con unwrap de pasos cortos,
no una rotación total independiente de gimbal. `endpoints_match` solo dice que
posición/orientación final coinciden dentro de tolerancia; no demuestra loop suave.

| Umbral insights | Default |
|---|---:|
| `acceleration_threshold_m_s2` | 20 m/s² |
| `jerk_threshold_m_s3` | 200 m/s³ |
| `angular_acceleration_threshold_deg_s2` | 2000 °/s² |
| `jump_distance_m` | 0.25 m entre muestras |
| `derivative_ratio` | 4 respecto a contexto mediano |
| `loop_position_tolerance_m` | 0.01 m |
| `loop_angle_tolerance_degrees` | 1° |
| `vertical_tolerance_m` | 0.05 m |

Los eventos necesitan superar criterios absolutos y relativos al contexto;
son candidatos de revisión. La velocidad alta constante no se considera mala.
Intervalos de key idénticos pueden indicar mocap legítimo, no keys que deban
borrarse automáticamente. Root vertical puede representar un salto intencional.

## Balance

Modelo inicial: proyección root/pelvis en XY contra discos/cápsula alrededor
de uno o dos marcadores de apoyo. `support_radius_m=0.08` es una aproximación
configurable, no geometría medida de los pies. Seleccionar `support_bones` y
`pelvis_bone` explícitos mejora interpretabilidad. Sin selección fiable devuelve
inconclusive. Frames fuera de región son avisos heurísticos, no pruebas de
imposibilidad física. No calcula centro de masa, fuerzas ni corrección de balance.

## Rendimiento y resultados

Todas las tools conservan `metrics.elapsed_ms` en el envelope. Se registran
muestras, evaluaciones, cantidad de canales/keys y truncamiento pertinente.
Los errores de presupuesto piden reducir alcance en lugar de omitir datos
silenciosamente. Resultados finales y benchmarks: [validación pública y límites](validation.md).

## M3: derivadas y geometría

Contactos en coordenadas mundiales/metros y superficies evaluadas. Distancia BVH
es euclídea; distancia normal firmada sólo estima penetración. Correcciones comparan
las fases originales para no ocultar un fallo cambiando su clasificación.

Velocidad/aceleración lineal: diferencias por intervalo con FPS real. Velocidad
angular: quaternion relativo de arco corto y vector de eje mundial; aceleración
angular: cambio de ese vector, incluyendo inversión de dirección. La primera
muestra replica la primera velocidad; su aceleración inicial no es estimable.
No usar estas métricas como puntuación artística. Pops incluyen ángulo interno,
plano de flexión, extensión y movimiento del pole. Comparar eventos por miembro/frame.

Foot locking compara trayectoria objetivo, roll, huesos protegidos, incremento
pareado de velocidad/aceleración, plano/ángulo de rodilla, cambio de longitud y
drift en fases fuente. Parámetros y unidades en contact-model.md. Stretch se mide
con head/tail mundiales; escala uniforme del objeto no crea un falso stretch.

Bézier: desviación en unidades nativas por canal y derivadas en unidades/frame.
Verificación de 129 muestras por intervalo alterado con presupuesto explícito,
incluidas vecindades modificadas por Blender; no diluir precisión en Actions largas.

Deformación: ratios arista/área/volumen aproximado y candidatos BVH; no son
certificación anatómica ni detección exhaustiva de autointersección. Consultar
[deformation-qa](deformation-qa.md) y [mocap](mocap-retargeting.md).
