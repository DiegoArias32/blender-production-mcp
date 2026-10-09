# Workflow de animación y refinamiento M2

El ciclo es **ANALYZE → PROPOSE → PREVIEW → APPLY → VERIFY → QA visual → aceptar o REVERT**. Las correcciones importantes trabajan en Actions copiadas y mantienen checkpoint. Un resultado técnico correcto no certifica peso, intención, silueta ni calidad artística.

1. Auditar escena y localizar personaje/armature; revisar unidades, escala, FPS y rango.
2. Inspeccionar jerarquía, constraints, drivers, IK, polos y candidatos FK. Validar confianza y controles antes de editar.
3. Crear checkpoint y trabajar en copia. Crear Action separada sin eliminar las anteriores.
4. Diseñar poses, anticipación, blocking, breakdowns, timing y spacing. Evaluar peso, balance, centro de masa, inercia, silueta, contactos y secundarios.
5. Analizar Action y F-Curves. En Blender moderno se inspeccionan slots/layers/channelbags, además del API legacy cuando existe. NLA se informa y se evalúa en las muestras de pies; no se confunde con un Action aislado.
6. Medir pies explícitos o revisar candidatos. Usar altura del marcador en contacto, no asumir que la cabeza de un hueso coincide con la suela. Suelo plano Z-up; pendientes/plataformas requieren calibración o algoritmo posterior.
7. Detectar discontinuidades quaternion en componentes WXYZ completas. q y −q son la misma orientación; reportar signo y salto angular separadamente.
8. Ejecutar `animation_refinement_plan`, revisar hallazgos y corregir una unidad de trabajo mediante propuesta. Renderizar before/after equivalente, inspeccionar archivos reales, medir otra vez y guardar checkpoint final.

`animation_analyze_control_mode` describe el frame actual; no supone switches constantes durante toda la animación. El plan reúne Action/rig, contactos/sliding, quaternion, F-Curves, root, balance y movimiento; no ejecuta correcciones. Confidence representa evidencia heurística, no probabilidad calibrada.

## Proponer una corrección

Usar el corrector con `mode="analyze"` para revisar soporte/advertencias y después `mode="propose"` para guardar opciones y precondiciones. Para controles y contactos ya revisados, este ejemplo corresponde al fixture generado:

```json
{
  "object_name": "M2_VisibleRig",
  "mode": "propose",
  "options": {
    "controls": ["ContactControl.L"],
    "confirm_contacts": true,
    "frame_start": 1,
    "frame_end": 10,
    "blend_frames": 3,
    "ground_height_m": 0.0,
    "sliding_tolerance_m": 0.01
  }
}
```

Esos nombres no son requisitos para otros rigs. `confirm_contacts` expresa que se revisaron intervalo y control; no enviarlo automáticamente porque el detector encontró drift.

## Previsualizar, aplicar y verificar

Antes de mutar (M3.1): `production.asset_status` debe indicar un rol mutable. Con un archivo real,
registrarlo como MASTER y crear una copia de trabajo (`asset_register_master` y
`asset_create_working_copy`). El rig debe estar en Pose Position (no REST) y en modo Object o Pose.
Para reintentar tras un timeout, usar el mismo `operation_id` o la misma propuesta y consultar
`production.operation_status`; nunca lanzar a ciegas una propuesta nueva.

Guardar el campo `operation` devuelto. Llamar al mismo corrector con `mode="preview"`, esa ruta y sin nuevas opciones. Se valida Action/contexto, crea checkpoint y candidato, corrige la copia y mide. Preview restaura la asignación original y conserva candidato.

Renderizar `qa_compare_before_after` con fuente/candidato. Revisar vistas equivalentes y suficientes frames de entrada al apoyo, apoyo, salida y cuerpo. Inspeccionar PNGs o secuencia real; un mosaico de pocos frames aporta evidencia parcial del timing. Los defectos temporales pequeños (p. ej. una deriva de pie de ~1 cm) no se resuelven en stills de cuerpo entero ni siquiera con encuadre cerrado a 320 px: requieren reproducción continua o una trayectoria, además de la medida numérica.

Si la propuesta es adecuada, `mode="apply"` con la misma referencia crea su duplicado y lo activa tras pasar verificación numérica. No promueve silenciosamente un preview modificado. `mode="verify"` revisa firmas/contexto y vuelve a medir: un cambio intermedio puede invalidar la operación.

Evaluar visualmente la versión aplicada. Si falla, `mode="revert"` crea checkpoint y reasigna el original, conservando candidato. Desde M3.1, revert conserva los cambios hechos después por el usuario (frame, transforms, ediciones del candidato) y los informa (`USER_CONTEXT_KEPT`, `CANDIDATE_EDITS_KEPT`). Solo se rechaza si la Action fuente cambió o si hay otra Action activa; en ese caso, inspeccionar y recuperar el checkpoint, sin forzar ignorando errores. En cambio, `verify` sí da `stale` ante cualquier cambio de contexto: el candidato ya no se mide contra lo propuesto.

La aceptación visual se registra en el informe de trabajo; no hay autoaceptación artística. Guardar checkpoint final y archivo de trabajo nuevo al aceptar. No sobrescribir un maestro. No acumular retiming, smoothing y foot locking en una mutación opaca.

## Foot sliding

Posiciones world-space de cabezas de huesos evaluadas tras constraints/NLA/object motion. Conversión a metros por unit_settings.scale_length. Umbrales iniciales configurables: altura 0.03 m, **velocidad vertical** 0.25 m/s, drift horizontal tolerado 0.01 m y duración mínima 3 frames. Son puntos de partida documentados, no estándares universales. La velocidad horizontal no descarta un contacto: eso ocultaría sliding rápido.

Los contactos son inferencias cinemáticas, no detección física infalible. Se reportan intervalos, desplazamiento respecto al inicio, longitud recorrida, frames problemáticos y estado. Step >1 reduce precisión temporal; se informa como limitación. El muestreo restaura frame/subframe; se limita para proteger el hilo principal.

Las pruebas sintéticas permiten separar errores del detector de calidad artística. Un walk mecánico de prueba no equivale a una animación final de videojuego.

## Criterios visuales y límites

Valorar peso, balance, anticipación, acción principal, follow-through, overlap, arcos, spacing, timing, silueta, inercia, aceleración/desaceleración y contactos. En criaturas pesadas, usar desplazamiento corporal, recuperación, torso y secundarios además del timing.

Después de foot correction observar rodilla, tobillo, toe roll, pelvis y alcance IK. Las métricas buscan cambios bruscos, stretching y divergencias de trayectoria; no resuelven toda intención del pie o clipping. Después de quaternion/cleanup revisar entre keys. Después de retime revisar velocidad vertical de contactos, que cambia aunque la trayectoria espacial se conserve.

FK ambiguo, NLA activo, drivers, Actions complejas, plataformas móviles o foot location Bézier requieren preparación/alternativa manual. No hacer conversión/baking IK/FK general, retargeting ni trabajo M3 dentro del alcance histórico de M2.

Ver [tools](tools.md), [seguridad de correcciones](animation-correction-safety.md), [métricas](animation-metrics.md) y [validación pública y límites](validation.md).

## M3: flujo para corregir y retargetear

Verificar copia → scene/rig/compatibility → Action y modo por extremidad →
superficies/contactos → Bézier → pops/stretch → root → checkpoint y copia →
propuesta concreta → preview → aplicar corrección revisada → volver a medir →
deformación → vistas equivalentes → aceptación visual o revert.

Mapping de baja confianza detiene mutación automática. Los marcadores locales
heel/sole/toe requieren revisión; un nombre anatómico no determina la suela.
Los adaptadores M3 añaden soporte conservador FK y Bézier manteniendo el camino
M2 intacto. No convertir rigs ambiguos, aplicar transforms ni borrar estructura.

Mocap: analizar fuente/destino → semántica → mapping y confianza → rest pose →
preview → gate de autorización si falta → checkpoint → Action nueva → retarget →
contactos/foot locking propuesto → root → limb pops/stretch → deformación → visual.
Retarget que conserva sliding heredado no equivale a mocap limpiado. Reportar cada
problema y sus alternativas; no encadenar correcciones sin propuestas medibles.
