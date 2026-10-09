# Generación de root motion y contactos

`production.animation_generate_root_motion` coordina root, pelvis, timing y
apoyos. El brief declara IN_PLACE o ROOT_MOTION, distancia/velocidad y duración.
No se aplica transforms ni se crea/reparenta un root para sortear incompatibilidad.
Root inicial debe ser válido, no parentado y dueño de la jerarquía de pelvis.

En ROOT_MOTION locomotion el planner mueve root y mantiene los anchors de
stance constantes en mundo; los swings avanzan hasta el siguiente apoyo. Los
offsets de pie son WORLD-relative-rest en metros de base corporal. No volver a
sumar root a un target IK independiente: causaría doble desplazamiento.

IN_PLACE evita drift de traslación. TURN puede incluir yaw planificado sin
traslación; foot steps y rotación deben verificarse juntos. La convención
angular/basis debe respetarse, nunca asumir ejes locales de bone por su nombre.

## Contactos generados

`production.animation_generate_foot_contacts` usa intención heel/sole/toe,
plantas y swings. Fases iniciales: HEEL_STRIKE, FOOT_FLAT, MID_STANCE,
HEEL_RISE, TOE_OFF y SWING. Las criaturas pueden declarar otra intención, pero
no se certifica un adapter no soportado.

La detección M2/M3 vuelve a medir altura, normales, velocidad, penetración y
sliding sobre geometría/apoyos reales. `CONTACT_CONFIRMED` es un gate de evidencia.
Sin datos suficientes: sliding `null` e informe inconcluso, nunca cero/pass.
Superficies móviles, inverse dynamics y escaleras arbitrarias quedan fuera del
adapter inicial.

`production.animation_generate_effector_contact` admite un contacto explícito
limitado mediante un adapter validado, target/pole/rango/tolerancia declarados.
Un hand/weapon impact sin alcance o control compatibles se rechaza; no se
retargetea ni se modifica el rig de forma implícita.

QA root reporta XYZ, yaw, distancia/velocidad, vertical inesperado y continuidad.
Locomotion compara endpoints en espacio relativo al root además del delta
mundial intencional. No exigir delta cero a ROOT_MOTION ni ocultar root teleport.
