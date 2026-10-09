# Movimiento secundario — M8

Estado vigente: M8 calificado sobre fixtures controladas, versión 0.8.0. Este documento describe sus contratos y límites. Véanse el [validación pública y límites](validation.md) y la evidencia agregada; no certifica cualquier personaje de producción. El [validación pública y límites](validation.md) conserva la evidencia anterior a los cambios.

## Flujo y propiedad de los datos

M8 recibe una actuación primaria existente de cuerpo y, cuando corresponde, cara. Primero inspecciona la geometría, jerarquía, anclajes, canales y representación secundaria; después propone un plan, crea copias propias y añade movimiento separado. El desarrollo usa fixtures controladas. Un asset real requiere autorización concreta y rol `WORKING_COPY`; `MASTER` permanece inmutable.

El recorrido es: inspección → clasificación → compatibilidad → brief → plan → validación de escala y estado → checkpoint → candidata → configuración → simulación/generación → estabilidad → colisiones → integridad primaria → preview temporal → revisión → refinamiento limitado → cache/bake → comparación → aceptar/rechazar.

El ciclo reutiliza `generation.store`, `OperationLedger`, Bridge/safe_mode, asset guards y checkpoints del proyecto. Su referencia pública es `data.physics`, una ruta al plan congelado. Las llamadas posteriores usan el argumento `physics` con esa referencia; no se edita el archivo del plan para eludir firmas o cambiar parámetros. La firma pública común utiliza `object_name`, `intent`, `brief`, `options`, `apply=False` y `operation_id` según la operación. Omitir `apply` conserva el comportamiento de propuesta. Un `operation_id` repetido se resuelve mediante la arquitectura de idempotencia existente.

La candidata transita por `PLANNED`, `SECONDARY_CANDIDATE`, `SIMULATED`, `REVIEWED` y `ACCEPTED` o `REJECTED`. Conserva identificadores, versiones, contexto, firmas fuente/cuerpo/cara, configuración, colliders, cache, QA y artefactos. Aceptar registra el resultado y conserva sus copias y artefactos; no sustituye automáticamente el objeto fuente visible ni aplica transforms. El preview compone temporalmente el primario con la candidata y restaura el estado al terminar.

## Brief, clasificación y backends

`production.physics_create_brief` normaliza `production.physics_brief.v1`: intent, subsystem, profile, frame range, duración, FPS, gravedad, settings, QA, región y autorización. Cada campo distingue `USER_REQUIREMENT`, `INFERRED_DEFAULT` y `PROPOSED_PARAMETER`. Los perfiles y tolerancias son puntos de partida que necesitan calibración. No representan materiales físicamente universales.

El contrato admite 30/60 FPS, clips de 1 a 10 segundos y hasta 601 muestras enteras. Duración y frames deben coincidir con la rejilla del FPS. Rechaza campos desconocidos, booleanos usados como números y datos no finitos.

| Clasificación | Subsystem del brief | Backend acotado |
| --- | --- | --- |
| `CLOTH` | `CLOTH` | Cloth nativo inspeccionado en Blender 5.2.1; salida portable PC2 |
| `HAIR` | `HAIR` | Cadena explícita de huesos; cards/malla vinculada cuando corresponde |
| `SPRING_BONE` | `SPRING_BONE` | Masa, muelle y amortiguación con límites |
| `DANGLING_ACCESSORY` | `ACCESSORY` | Rotación secundaria alrededor de un origen anclado |
| `CHAIN_LIKE` / `CABLE_LIKE` | `CHAIN` / `CABLE` | Aproximación segmentada con longitudes fijas |
| `SOFT_REGION` | `SOFT` | Morphs secundarios en una copia y máscara autorizada |

`RIGID`, `STATIC`, `UNSUPPORTED` y `AMBIGUOUS` bloquean la configuración automática. Los nombres sólo aportan evidencia secundaria. La confianza se calcula en el servidor; la confianza enviada por el llamador sólo puede reducirla. Un manifiesto no elimina los guards de representación o propiedad.

## Integridad y límites

Las firmas cubren fuente, dependencias, Actions completas, slots/layers/channelbags, NLA y contexto. Incluyen keys, handles, ajustes de strips, influencia, FPS, unidades, gravedad, rango y jerarquía/rest pose del rig. Se revalidan antes de copiar o escribir. Drivers arbitrarios, F-Curve modifiers, dependencias linked/override, fuentes ya simuladas y adaptadores no demostrables se rechazan.

La comparación adicional de matrices, geometría evaluada y valores de keys toma hasta 64 frames. Esa evidencia de muestreo complementa las firmas; no certifica todos los subframes ni calidad física. `production.physics_verify_primary_integrity` debe conservar el cuerpo y la cara, incluidos sus NLA.

Las métricas de `technical_qa.metrics` proceden de la geometría y orientación realmente evaluadas por Blender. Los cálculos del solver host se conservan aparte en `technical_qa.solver_predictions`; no sobrescriben desplazamientos, velocidades o errores observados. Una predicción estable no permite aprobar un guard evaluado fallido. Los mínimos, como `min_area_ratio`, deben resumir el peor mínimo del clip, mientras los máximos de movimiento conservan su peor máximo.

La validación intermedia de integridad/inspección/candidatas comunicada por el coordinador comprende 75 tests unitarios y 31 checks del probe controlado. Su alcance incluye NLA corporal/facial, cambios de handles/shape keys, propiedades de copia, colliders y rechazo de dependencias no firmables. El probe de integridad (archivo local no distribuido; [validación pública](validation.md)) es evidencia de ese subsistema, no regresión final ni cierre M8. El entorno también rechaza force fields o rigidbody worlds globales incompatibles; no los desactiva para hacer pasar el plan.

Un pipeline técnico combinado (archivo local no distribuido; [validación pública](validation.md)) usó una copia genuina del demo corporal M6 con tres capas NLA faciales aceptadas en M7 y añadió un accesorio M8 separado. Conservó sus firmas primarias y el SHA256 del archivo histórico M6/M7; midió 0,00618 m de desplazamiento secundario, origen anclado sin error y bake máximo/RMS cero en 61 frames. Esa pasada no realizó review/aceptación artística y su plan de accesorio no declaró colliders (`NOT_APPLICABLE`); no constituye evidencia de colisión cero del caso combinado ni cierra su demo temporal.

La revisión posterior encontró que ese archivo heredaba la visibilidad del close-up M7: el body M6 estaba oculto y el preview mostraba sólo cabeza/accesorio. En una copia controlada se probó primero hacerlo visible; un still FRONT (archivo local no distribuido; [validación pública](validation.md)) confirmó que su cubo de cabeza ocultaba el rostro M7. La fixture vigente crea una proxy visible derivada del body exacto ligado al rig, de 360 vértices, excluyendo sólo las 24 del componente rígido de cabeza identificado por la vinculación facial. Conserva geometría y pesos completos del body original, las Actions, las capas faciales y visibilidad de los otros rigs; el archivo histórico permanece intacto. No se aplica esta preparación a assets de producción.

El pipeline técnico de la fixture visible (archivo local no distribuido; [validación pública](validation.md)) pasó nuevamente generación/cache/bake y comprobó colisiones contra la proxy corporal cerrada explícita en 61 frames: penetraciones/depth cero, anclaje cero, bake máximo/RMS cero e integridad primaria e histórica. El still de la nueva fixture muestra cuerpo y rostro, pero ese resultado estático no sustituye la nueva revisión temporal del coordinador ni cierra el gate AH por sí solo. Se conserva el fallo visual anterior y un fallo de construcción corregido de los grupos de vértices de la nueva proxy.

La QA técnica y [la revisión temporal](physics-visual-qa.md) son gates distintos. No se calcula un score de realismo. Después de completar el alcance autorizado se detiene el trabajo: M8 no autoriza M9, materiales/PBR, Substance, Unreal ni orquestación.


## Cierre del demo combinado

La repetición MCP dirigida `bpm8-256678ca` pasó 70/70 checks y 26 llamadas. Se revisó la secuencia completa normal y0,25 enFRONT/SIDE/THREE_QUARTER: cuerpoM6 y rostroM7 visibles, accesorio con respuesta pequeña, ACCEPT acotado a infraestructura. Se conserva el REJECT anterior `bpm8-31b29f56`, sin transformar su reporte FAIL enPASS. El cuerpo/cara/NLA originales y SHA histórico siguen intactos; microgestos faciales no se juzgan en detalle a192px. Véase el reporte final para artefactos y límites.
