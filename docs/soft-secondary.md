# Movimiento secundario en regiones blandas — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. Este adaptador es un deformer procedural acotado por región. No es simulación anatómica de músculos ni un modelo físico completo de tejido.

## Autorización y selección

`production.physics_analyze_soft_region` inspecciona una región explícita; `physics_generate_soft_secondary` sólo opera con un plan autorizado. El brief usa `subsystem='SOFT'`, `region` y `region_authorized=true`. La clasificación `SOFT_REGION` requiere además un manifiesto con `authorized=true` y una máscara no vacía (`authorized_mask` o `region_mask`). Ambas declaraciones se verifican contra datos reales antes de mutar.

El backend vigente usa un vertex group nombrado en una malla copiada. La máscara debe contener vértices con peso positivo y conservar una región no vacía fuera de la selección. Un grupo que cubre todo el cuerpo no pasa como región autorizada. Los pesos suministran el falloff; el sistema no inventa una selección anatómica.

El preflight congela los índices de peso cero como región exterior protegida. Para que esos índices sigan significando los mismos puntos, sólo admite los modificadores pointwise/topology-preserving explícitamente soportados por el adaptador. Smoothing, subdivisión, mirror y systems de nodes no se permiten para esta ruta: pueden propagar una deformación fuera de la máscara o cambiar correspondencias. La misma condición se verifica de nuevo antes de crear las keys en la copia.

La candidata debe ser una malla sin morphs preexistentes para este adaptador. Si ya existen shape keys o una dependencia incompatible, se rechaza sin eliminar sus datos. La fuente, las regiones protegidas y la asimetría intencional se conservan.

## Deformer y límites

`pp_soft_setup` crea `Basis` y tres keys propias, `M8_SoftX`, `M8_SoftY` y `M8_SoftZ`. Cada key modifica sólo vértices con peso positivo, según el máximo de desplazamiento declarado y su peso. Sus valores se animan en una Action de keys distinta. Las traslaciones procedurales se derivan del driver primario mediante el [solver masa/muelle/amortiguación](spring-motion.md); no proceden de una onda aleatoria.

El desplazamiento world se convierte usando la base real de la copia antes de representarse en canales locales. `max_displacement_m` limita la norma del offset; no se interpreta como tres máximos independientes cuya suma pueda exceder el bound. La escala y transforms se validan, sin aplicar transforms a la fuente.

La configuración expone masa proxy, stiffness, damping, gravedad, lag y máximo de desplazamiento. Palabras como heavy o restrained seleccionan propuestas acotadas; no justifican una oscilación extrema ni convierten los valores en propiedades universales de tejido.

## Verificación

La QA evalúa desplazamiento, velocidad/aceleración, colisiones, stretch, degeneración y, para superficies cerradas orientadas, inversión de volumen. También compara la región exterior protegida contra el primario en los mismos frames. La modificación fuera de la región autorizada es un fallo, aunque la candidata parezca visualmente aceptable.

Debe comprobarse que la asimetría original no se normalizó ni se alteraron el cuerpo y la cara. Las firmas completas de fuente/primario/NLA se complementan con muestras evaluadas; no certifican una trayectoria continua. Se revisan normal y slow motion en tres vistas, con atención a decay, settling, peso aparente y penetración.

El resultado reusable puede conservar la Action secundaria de keys y su copia geométrica. [La comparación del bake](physics-baking.md) verifica correspondencia de la representación elegida. Una región o tolerancia de fixture no constituye autorización para una región de un personaje real.

El pipeline técnico intermedio de región soft (archivo local no distribuido; [validación pública](validation.md)) pasó generación/QA/cache/bake en 61 frames: máximo de desplazamiento 0,03977 m frente al bound revisado de 0,04 m, cambio exterior protegido cero, stretch 1,17132 y penetración muestreada cero. Comparó todos los vértices con error máximo/RMS de bake cero y conservó el archivo fuente. Se registró sólo calificación técnica; el preview, la lectura artística de peso/asimetría y la decisión final siguen siendo gates separados.
