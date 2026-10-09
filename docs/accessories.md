# Accesorios anclados — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. El backend de accesorios ofrece rotación secundaria acotada en una copia, alrededor del origen de un anclaje declarado. No representa una simulación rígida general.

## Inspección y contrato

`production.physics_analyze_accessory` identifica jerarquía, parent bone, origen, restricciones, geometría y necesidades de colisión. `physics_generate_accessory_motion` recibe una referencia `physics` al plan vigente y configuración en `options`; la mutación requiere `apply=true`.

El accesorio debe tener un anclaje explícito y un origen adecuado para el giro. Una pouch o un pendant con el origen en su centro, pese a estar conectado visualmente, necesita un adaptador o preparación autorizada; M8 no mueve el origen ni aplica transforms a la fuente para sortear ese problema. Un objeto rígido/fijo se clasifica como `RIGID` o `STATIC` y no se convierte automáticamente en un oscilador.

El plan declara masa proxy, stiffness, damping, lag, libertad de rotación, máximo angular/desplazamiento, rango y colliders. Estos settings describen una aproximación ajustable. Una restricción no soportada se rechaza antes de escribir; no se elimina un constraint original para permitir la generación.

## Movimiento y conservación del anclaje

El driver se evalúa a partir del primario. `solve(..., kind='ACCESSORY')` usa traslación y orientación quaternion completa, incluidos giros alrededor de la dirección longitudinal. Acumula incrementos angulares cortos como proxy y produce un delta de orientación limitado, más el swing debido a la traslación. No añade ruido o una oscilación periódica independiente del driver.

El origen de la salida permanece igual al origen primario correspondiente. La geometría situada bajo o fuera del origen gira alrededor del anclaje. Se escriben canales separados en el objeto candidato; el objeto fuente, cuerpo, cara y parent rig no se retiman ni se reescriben.

La combinación de rotaciones en varios ejes es una aproximación procedural; sus parámetros no constituyen un inertia tensor físico ni un modelo de cuerda/strap completo. Los deltas quaternion se convierten con la base medida del objeto, sin cambiar modos de rotación fuente para forzar compatibilidad.

El muestreo primario mide el mayor radio world de la superficie evaluada respecto al origen en todos los frames. Para respetar un bound de desplazamiento `d`, el solver utiliza un máximo angular interno que satisface `2·r·sin(ángulo/2) ≤ d`; toma el menor entre ese bound geométrico y el ángulo configurado. Radios no finitos, nulos o inválidos se rechazan antes de resolver/copiar. Ese presupuesto conservador no modifica los settings públicos ni sus thresholds: la QA final comprueba la geometría realmente evaluada contra el límite original.

## Gates

La QA compara el origen fuente/candidato en los mismos frames y mide `anchor_origin_detachment_m`. También evalúa las posiciones de la geometría para desplazamiento, velocidad, aceleración y penetración. Conservar el origen por sí solo no garantiza que el cuerpo del accesorio no atraviese el torso.

Se verifica nuevamente la pila corporal/facial mediante firmas completas y muestras evaluadas. El preview compara primario y primario+secundario con cámara, iluminación y timing iguales; requiere tres vistas, velocidad normal y lenta. La revisión registra por separado problemas técnicos y observaciones como demasiado floaty, atraso excesivo o asentamiento insuficiente.

La Action reusable es distinta de las primarias. El [bake](physics-baking.md) debe demostrar equivalencia en el rango completo. Aceptar conserva la copia y artefactos aprobados; no oculta ni reemplaza el accesorio original automáticamente. Rechazar conserva la evidencia sin publicar la candidata.

Un pipeline técnico intermedio (archivo local no distribuido; [validación pública](validation.md)) pasó plan/candidata/colliders/generación/cache/bake en 61 frames de la fixture del accesorio colgante: desplazamiento máximo 0,06583 m, ángulo 11,36652°, error de origen cero y penetración muestreada cero. Bake máximo/RMS cero en todos sus vértices. Conservó la fuente y el backend durante esa pasada. Son medidas de la fixture, sin review artístico ni aceptación.
