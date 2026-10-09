# Bake reusable y equivalencia — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. Bake significa conservar el movimiento validado en una representación reusable, con evidencia de correspondencia; no basta con que una operación termine sin error.

## Representación y propiedad

`production.physics_bake_secondary_motion` recibe `physics`, opciones revisadas y `apply=true` para crear el resultado propio. Conserva la candidata simulada y las Actions fuente de cuerpo/cara. El resultado reusable es distinto y mantiene referencias a fuente, plan, versión, cache y QA.

| Subsystem | Representación apropiada |
| --- | --- |
| Cloth | PC2 y copia de playback con `MESH_CACHE` |
| Huesos/hair cards/chains/cables | Action secundaria separada y rig/copia propia |
| Accesorio anclado | Action de transforms en una copia, conservando el origen/anclaje |
| Región soft | Action de keys propias y malla candidata con máscara autorizada |

No se fuerza Cloth a una Action de huesos, ni todo movimiento procedural a un cache de Cloth. Una Action reusable o clonada conserva sus slots/layers/channelbags y su propiedad. La generación no cambia el blend, influencia, keys o timing de las capas primarias para acomodar el bake.

## Comparación en el rango completo

`pq_bake_compare` evalúa resultado validado y resultado baked en cada frame entero del rango declarado. Para mesh exige igual cantidad de vértices, aristas y triángulos; compara todos los vértices evaluados en metros world. Informa máximo y RMS por frame, máximo del clip y tolerancia configurada. NaN, topology mismatch o diferencias fuera del bound producen fallo.

Para un armature se declaran los huesos que se comparan. Comprueba mapeo/jerarquía, heads, tails y orientación world; informa error posicional máximo/RMS y máximo angular en grados. `coverage.all_pose_bones` distingue una comparación de todos los huesos de una lista parcial. No se presenta una comprobación parcial como integridad del rig completo.

Las tolerancias de posición y rotación son propuestas configurables; deben corresponder al uso previsto. No son errores máximos universalmente aceptables. La equality medida en frames enteros no certifica cada subframe; esa limitación acompaña el resultado.

La capa soft se compara como geometría evaluada cuando corresponde, incluyendo la región protegida. El accesorio se comprueba como superficie y anclaje; comparar sólo una curva o el origen no asegura equivalencia de su movimiento completo.

## Gates y conservación

El bake exige resultados vigentes, QA aplicable e integridad corporal/facial. Se mide contra la simulación/generación validada, usando la misma ventana, FPS, escala, colliders y contexto. Un mismatch del playback no se oculta recortando el clip o cambiando la cámara.

Debe verificarse la reapertura del cache/Action y una lectura no secuencial cuando la representación lo requiera. [El cache](physics-cache.md) necesita dependencias frescas y contenido intacto antes de evaluarse. No se afirma determinismo bit a bit sin una prueba concreta.

El preview temporal sigue siendo necesario para valorar el resultado reusable y sus límites visuales. Aceptar marca el resultado aprobado y conserva copias y artefactos; no oculta la fuente automáticamente ni escribe sobre un MASTER. Rechazar o fallar la comparación conserva el resultado validado anterior y la evidencia del intento.

M8 no ofrece una exportación universal a motores, Unreal cloth/groom o physics assets. Tampoco cambia el FPS para lograr equivalencia ni reescribe las actuaciones primarias.
