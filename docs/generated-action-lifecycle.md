# Lifecycle de Actions generadas

Estados: PLANNED → GENERATED_CANDIDATE → REVIEWED → ACCEPTED o REJECTED.
Una Action fuente es inmutable. Cada etapa/refinamiento crea una nueva candidata
o duplica la anterior, conservando versiones y checkpoints. Nunca se acepta
mediante reemplazar/eliminar una Action existente.

`generation` referencia el `plan.json` dentro del almacén local. SHA verifica
integridad del plan; eventos append-only enlazados mediante hashes registran
estado, candidata/firma, QA, preview, operación y refinamientos. Editar artefactos
para evitar `STALE_PROPOSAL` se rechaza.

## Frescura

Comprobar fuente, rig fingerprint, candidata, escena/archivo, asset stamps,
FPS/unidades y backend. Cambio de rig o de contexto invalida el plan; volver a
analizar. Verificación atómica dentro de Blender debe preceder a crear/modificar
una candidata, además del guard host de rol mutable.

La reproducibilidad también exige que los canales activos no escritos por el
adapter sean neutrales constantes. `UNAUTHORED_POSE_CHANNELS` rechaza valores
RNA, keys o handles Bézier contribuyentes no neutrales, incluso si el fingerprint
no cambia entre dos frames iniciales. La tolerancia es `1e-7` en unidades RNA;
no es una tolerancia de desplazamiento mundial. Escalas, posiciones FK, poles
o controles auxiliares quedan sujetos a esta regla cuando no los genera el
adapter. Los valores dormidos de otra representación de rotación no cuentan
con `rotation_mode` fijo. El rechazo conserva la fuente: no resetea pose ni
añade claves.

La reproducción controlada anterior medía `0.0046000685 m` de diferencia en
cabeza para la misma candidata al evaluar desde frames 1/16, con escala heredada
de pecho. La nueva policy rechazó esas entradas en ambos frames; el probe real
registró 78 checks PASS, 17 fixtures y siete pipelines hasta SPLINE, incluido
el rig de 131 huesos. Esa calificación de compatibilidad no sustituye el
recorrido final por MCP, accepted-read ni la revisión artística. Evidencia:
`test-artifacts/m6/pose-determinism-64292ce3cd/report.json` y
`test-artifacts/m6/pose-determinism-33fa933cac/report.json`.

Un `operation_id` repetido con idéntica petición debe devolver evidencia o
reconciliar el resultado. Un outcome incierto no autoriza generar otra Action
con una clave nueva. Consultar operation_status y recovery checkpoint.

## Revisión y aceptación

`production.animation_accept_generated` distingue revisión de activación.
La revisión registra reviewer, observations, findings, decision,
artifact_reference y `temporal_reviewed=true`, ligada al preview temporal vigente.
Aceptar exige QA técnica y decisión visual ACCEPT; crea checkpoint y activa
la candidata sin sobrescribir otra Action. QA numérica no autoacepta calidad.

`production.animation_reject_generated` conserva candidata/evidencia según
política explícita; no elimina Actions ni assets por conveniencia. ACCEPTED y
REJECTED son terminales: para otro trabajo crear nueva generación.

## Refinamiento limitado

`production.animation_refine_generated`: máximo tres ciclos por generation_id.
Cada ciclo registra hallazgos, cambios propuestos, before/after del objetivo,
QA técnica, render y revisión visual. Si el objetivo no mejora o la revisión
rechaza el cambio, revertir a la candidata anterior y conservar el intento.
Una ejecución fallida/incierta consume su intento; no reiniciar el contador
para prolongar automáticamente el trabajo.

Custom properties de Action contienen IDs/versión/estado y referencias pequeñas,
no informes gigantes. Los informes completos permanecen en artefactos.

Después de la QA técnica, `production_generation_qa_result` registra PASS/FAIL
medido y `production_generation_qa_reference` apunta al `plan.json` existente;
sus eventos enlazados contienen el informe completo vigente. Se conservan
generation_id, operation_id, rig fingerprint, brief/profile, fecha UTC, versión
instalada al generar y estado. No cambiar metadatos históricos de 0.5.0 a 0.6.0
por el mero hecho de cerrar la entrega: describen la ejecución real.
