# Blocking y breakdowns

`production.animation_generate_blocking` crea una Candidate Action nueva con
poses principales CONSTANT/stepped. Antes: rol mutable, compatibilidad,
frescura, capacidad, checkpoint y comprobación de colisión. Nunca reemplaza la
Action fuente.

El blocking debe permitir revisar anticipación, soporte, dirección de acción,
impacto, recuperación y silueta con pocas poses. `animation_validate_blocking`
comprueba coherencia del plan/pose, duración, root y contactos. La validación
estructural y la revisión visual de blocking son gates de progresión; un fallo
fundamental debe impedir avanzar automáticamente a spline.

La interpolación CONSTANT puede producir grandes saltos entre poses por diseño.
Los detectores de movimiento deben distinguir esta etapa de spline; no se
declara una animación final aceptable sólo porque el blocking pasó sus poses.

`production.animation_generate_breakdowns` trabaja sobre otra candidata
conservada. No sobrescribe la fuente ni el blocking previo. El planner añade
poses intermedias con liderazgo de pelvis, retraso de torso y arcos de swing,
según fase/contacto. No interpola todos los canales con el mismo factor.

## Qué revisar antes de continuar

- El personaje ejecuta el propósito del brief y utiliza el efector dominante.
- Contactos declarados tienen evidencia geométrica; no confundir proximidad
  con intención ni falta de datos con sliding cero.
- Root, pelvis, targets y poles no presentan alcance imposible o conflicto IK/FK.
- La Action fuente y el rig permanecen intactos.
- Las poses y las tres vistas están ligadas a la misma candidata/firma.

Si el resultado es rechazado, conservar Action, checkpoint e informe. Replantear
brief/poses; no suavizar una estructura incorrecta hasta ocultarla.
