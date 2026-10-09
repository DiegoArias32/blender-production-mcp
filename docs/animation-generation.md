# Generación de animación corporal — M6

M6 añade planificación y creación de Actions candidatas al análisis, corrección y
retarget heredados. El dominio es general; no depende de ningún proyecto privado ni de nombres
concretos de huesos. El informe de cierre y la evidencia local privada (no distribuida; véase [validación](validation.md)) son las
fuentes de resultados verificados; este documento describe el contrato, no
declara aprobado un rig o una animación.

La unidad de trabajo es una animación corporal con brief, plan, candidata y
evidencia. No se ofrece un generador de keyframes aleatorios ni se considera una
Action técnicamente válida como aceptación artística.

## Flujo obligatorio

1. Verificar archivo y rol del asset. Un asset real necesita autorización y
   WORKING_COPY; MASTER permanece inmutable.
2. Inspeccionar rig, controles, Action/NLA, FPS, unidades, deformación y root.
3. Crear brief con requisitos y defaults explícitos.
4. Comprobar compatibilidad por capacidad y crear plan inmutable.
5. Checkpoint y nueva Candidate Action; generar poses/blocking CONSTANT.
6. Validar y revisar visualmente blocking. Si es fundamentalmente incorrecto,
   detener la progresión.
7. Generar breakdowns; revisar timing, spacing, arcos, mecánica, root/contactos.
8. Spline y polish selectivos; overlap/follow-through sólo con propósito concreto.
9. QA técnica y de deformación; preview temporal comparable en tres vistas.
10. Registrar observaciones artísticas; máximo tres ciclos de refinamiento.
11. Aceptar o rechazar explícitamente, conservando fuente y evidencia.

`apply=false` planifica por defecto. Una referencia `generation` identifica el
`plan.json` inmutable dentro del almacén local; no se edita a mano para sortear
un rechazo. Las modificaciones se identifican además mediante `operation_id`.

## Arquitectura

`animation/generation/planner.py` es puro: normaliza brief y construye fases,
poses/breakdowns semánticos, contactos previstos y capacidad. `runtime.py`
convierte intención a controles de un adapter validado mediante la API real de
Blender. `service.py`, `store.py` y `candidate.py` coordinan lifecycle, propuestas,
frescura, copias y evaluación temporal restaurada. `qa.py` llama a analizadores
heredados; `measurements.py` resume trayectorias; `render.py` produce secuencias
PNG/APNG y una página local de reproducción. `tools.py` registra unidades de
trabajo de alto nivel. Upstream/socket/safe-mode permanecen separados.

`window.py` comparte el guard de ventana temporal entre host y payload Blender;
`pose_policy.py` valida los canales activos que el adapter no escribe.
`integrity.py` firma geometría, pesos, binding, shape keys, modifiers y materiales
compatibles sin devolver sus datos completos. Estos guards rechazan entradas
fuera del contrato; no alteran el rig ni corrigen silenciosamente la fuente.

## Alcance y límites

Tipos iniciales: ATTACK, LOCOMOTION, IDLE, TURN, HIT_REACTION y START_STOP.
Perfiles: HUMAN, HEAVY_HUMANOID, HEAVY_CREATURE, CREATURE,
ASYMMETRIC_CREATURE y CUSTOM. Un perfil orienta decisiones, no demuestra
compatibilidad. El adapter inicial es limitado: bípedo validado, brazos FK y
piernas IK de dos huesos; otros casos requieren soporte explícito o se rechazan.

Los canales activos de pose no generados deben mantenerse neutrales y constantes:
por ejemplo, escalas, posiciones FK, poles o controles auxiliares que el adapter
no escribe. Se comprueban los valores RNA, las keys de fuente y los handles
Bézier que contribuyen a los segmentos, con tolerancia `1e-7` en unidades RNA.
La representación de rotación inactiva no afecta esta política cuando
`rotation_mode` está fijo. Un conflicto produce `UNAUTHORED_POSE_CHANNELS`;
no se neutraliza la pose ni se agregan claves para ocultarlo. No se promete
compatibilidad con cualquier pose o animación fuente.

RUN tiene soporte limitado y debe validar sus intervalos de vuelo. IN_PLACE
locomotion usa un ciclo de pasos/marcha con apoyo fijo; no promete un walk
treadmill con pies deslizando hacia atrás. Contactos iniciales usan suelo o
superficies estáticas verificadas. FK sin un adapter de apoyo no se certifica
como foot locking. No hay facial, dedos detallados, cloth/hair, física,
cinemáticas, ML, servicios externos ni M7.

Leer [brief](animation-brief.md), [poses](pose-planning.md),
[lifecycle](generated-action-lifecycle.md) y [QA visual](animation-visual-qa.md).
