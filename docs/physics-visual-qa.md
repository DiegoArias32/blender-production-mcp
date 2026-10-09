# QA temporal, revisión y refinamiento — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. Las métricas técnicas y la revisión visual son requisitos distintos. Ni un still ni `numeric_verification=pass` certifican el timing o la calidad artística.

## Preview comparable

`production.physics_render_preview` produce el recorrido temporal y `physics_compare_before_after` compara primario solo con primario+secundario. Usa la misma ventana, cada frame entero, FPS, cámara, iluminación y encuadre. Se conservan vistas `FRONT`, `SIDE` y `THREE_QUARTER`; puede añadirse una vista útil del subsystem.

El player ofrece velocidad normal y slow motion a un cuarto de velocidad. Las dos versiones deben mantener su sincronía. La revisión observa atraso, damping, flutter, rigidez, energía, penetración, asentamiento y continuidad; no sólo la silueta de un frame conveniente.

Durante el preview se compone temporalmente la candidata con el primario. El renderer restaura visibilidad, frame, asignaciones y contexto al terminar. Los originales permanecen intactos. Aceptar posteriormente no activa una sustitución automática del objeto visible fuente.

Las respuestas públicas conservan IDs, rutas, métricas, warnings y referencias a artefactos. No vuelcan keys/trayectorias completas. Un fallo o timeout incierto conserva la evidencia y requiere inspeccionar el estado antes de otra mutación; no se relanza automáticamente un render con otra clave.

## Revisión estructurada

`production.physics_review` registra una revisión vinculada a los artefactos actuales. Debe declarar que se revisó el movimiento temporal, identificar vistas/velocidades y distinguir:

- `TECHNICAL_FINDINGS`: fallos/avisos medidos, frames, colisiones, límites, cache/bake e integridad primaria.
- `ARTISTIC_OBSERVATIONS`: lectura del peso, rigidez aparente, atraso, energía, flutter, damping y asentamiento; observaciones humanas con alcance explícito.

No se genera un score de realismo/AAA ni una aprobación visual automática a partir de métricas. Una observación como «demasiado rígido» puede justificar una propuesta de parámetros, pero no se trata como una medida universal. La aceptación necesita evidencia técnica aplicable, revisión temporal vigente y resultado de bake/cache cuando corresponde.

## Refinamiento limitado

`production.physics_refine` usa la referencia `physics` y propone parámetros dirigidos a un hallazgo concreto. El ciclo es analizar → modificar parámetros revisados → simular/generar → QA de colisiones → estabilidad → integridad → preview → revisión temporal.

El contador es persistente y el máximo es tres ciclos automáticos por solicitud/candidata. Un ciclo fallido también cuenta; repetir claves o producir un cache nuevo no reinicia el límite. Si el objetivo medido no mejora, ese ciclo se revierte al resultado válido anterior, conservando el intento y su evidencia. Después de tres ciclos se detiene y se informan los problemas restantes.

No se rebakea indefinidamente en busca de una apariencia perfecta. Si el mismo gate no relacionado con seguridad falla tras dos implementaciones correctivas sustanciales por una limitación arquitectónica/API, se detiene ese subsystem y se documentan bloqueo, evidencia y fallback seguro. Una regresión de seguridad debe resolverse antes del cierre.

## Aceptación y alcance de evidencia

`physics_accept` registra la aceptación de copias y artefactos propios. `physics_reject` conserva la fuente y la evidencia y restaura el estado aplicable del candidato. Cuerpo, cara, NLA y geometría MASTER no se reescriben para facilitar la decisión.

Las fixtures sintéticas sólo califican las representaciones, bounds y escenas efectivamente probadas. Una animación técnicamente estable puede necesitar otra calibración en una `WORKING_COPY` real autorizada. El reporte debe separar lo visto, lo medido, lo pendiente y las limitaciones del muestreo.
