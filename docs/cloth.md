# Cloth nativo y pinning — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. La presencia de un modifier o un bake terminado no constituye calificación del resultado. El backend descrito utiliza propiedades RNA y operadores inspeccionados en Blender 5.2.1; otras versiones deben volver a comprobar compatibilidad.

## Preparación y configuración

El flujo público comprende `physics_analyze_cloth`, `physics_plan_cloth_pins`, `physics_prepare_colliders`, `physics_setup_cloth` y `physics_simulate_cloth`. El plan identifica malla, colliders, grupo de pins, rango, coste y tolerancias antes de crear la candidata. Las llamadas reutilizan `physics` y `options`; la mutación exige `apply=true` y una propuesta fresca.

Los pins deben ser explícitos e inspeccionables. El adaptador requiere un vertex group existente con pesos positivos; no escoge una unión crítica por nombre o por una inferencia débil. Las regiones o bordes propuestos deben revisarse antes de configurarlos. No aplica transforms automáticamente para intentar estabilizar un rig.

El plan congela los índices de pins de peso completo para medir su anclaje contra la fuente en cada frame. Necesita al menos un attachment de peso completo y deja vértices dinámicos. Una máscara vacía, que cubra todo el mesh o sólo tenga pins parciales no satisface ese gate. El preflight rechaza deformadores que propaguen cambios entre vecinos o alteren topología; la identidad de índices sólo se utiliza tras comprobar la pila compatible.

La candidata conserva sus deformadores primarios y recibe un modifier Cloth propio. La configuración activa `use_dynamic_mesh` para que su entrada siga la deformación primaria; las Actions fuente permanecen intactas. Una simulación o sistema de nodes preexistente no se elimina para forzar el adaptador. Los modifiers propios se identifican mediante metadatos del objeto candidato, porque los modifiers de esta API no admiten los custom ID properties usados en un intento inicial.

Los perfiles disponibles son `LIGHT_FABRIC`, `MEDIUM_FABRIC`, `HEAVY_FABRIC`, `LEATHER_LIKE`, `STIFF_CLOTH` y `CUSTOM`. Exponen los settings generados. Mass, stiffness, damping y calidad son propuestas controladas, no valores físicamente correctos para cualquier tela.

| Grupo RNA | Campos configurables del perfil |
| --- | --- |
| Cloth settings | `mass`, `tension_stiffness`, `compression_stiffness`, `shear_stiffness`, `bending_stiffness` |
| Amortiguación | `tension_damping`, `compression_damping`, `shear_damping`, `bending_damping`, `air_damping` |
| Integración y pins | `quality`, `pin_stiffness`; el grupo se asigna mediante `vertex_group_mass` |
| Collision settings | `use_collision`, `distance_min`, `collision_quality`, `use_self_collision`, `self_distance_min`, `self_friction` |

El adaptador lee de nuevo la RNA y rechaza un ajuste clampado fuera de la tolerancia de lectura. Self-collision está desactivada en los perfiles por defecto; habilitarla exige una configuración explícita y evaluar coste, penetración y estabilidad. Habilitarla no demuestra ausencia de autointersecciones.

## Simulación con guard y cache

`pc_simulate` evalúa todos los frames enteros del clip y todos los vértices evaluados de la candidata. Comprueba datos finitos, movimiento, bbox, stretch y geometría en cada paso. Un fallo devuelve `STOPPED_BY_STABILITY_GUARD`, el primer frame y sus motivos; no congela ese resultado como un cache válido.

Sólo tras completar el guard se congela la memoria nativa mediante `ptcache.bake_from_cache` con el contexto real del objeto/point cache. Se comprueban `FINISHED` e `is_baked`. El guard de estabilidad se complementa con penetración, pins, integridad primaria y QA temporal. Un congelado nativo aprobado numéricamente aún requiere mirar el movimiento.

La salida portable se escribe como [PC2](physics-cache.md) desde el host en una ruta reservada. `PointCache.filepath` no redirige la escritura nativa con `use_external=False`; no se usa esa propiedad para afirmar aislamiento de disco. El playback utiliza `MESH_CACHE` en una copia propia, con `frame_start` igual al primer frame del cache. Debe comparar el resultado nativo y el reusable en todo el rango.

La [QA de colisiones](collision-qa.md) es aproximada y la [QA visual](physics-visual-qa.md) temporal es obligatoria. Cloth abierto puede doblar sus normales sin estar invertido: la inversión global de volumen sólo se define para mallas cerradas con orientación consistente. No se interpreta cada pliegue como una cara invertida.

La validación intermedia comunicada de Cloth/QA comprende 68 tests unitarios y un probe de 17 checks aprobados (archivo local no distribuido; [validación pública](validation.md)). Incluye guard, mínimos de área, penetración, anchors, huesos, cache, reapertura y una fixture de 625 vértices/18 frames. Esas cifras identifican ese alcance y esa pasada; no afirman calificación completa de las demos. Los fallos anteriores de triangulación se conservan: la comparación vigente verifica cantidad de vértices, ciclos de polígonos y aristas, y permite cambiar la diagonal evaluada de un quad sin confundirla con un cambio topológico real.

## Benchmark finito de la fixture pesada

Un benchmark de cuatro cargas (archivo local no distribuido; [validación pública](validation.md)) ejecutó el servicio real en Blender background mediante pipes y payloads validados, sin GUI o sockets. Cada escena contiene 625 vértices de Cloth, pins explícitos, actuación primaria y colliders propios. Las cuatro pasaron estabilidad, colisiones, integridad primaria y escritura de cache; la fuente y el backend conservaron sus hashes. Esta pasada no incluyó revisión visual ni medición del tiempo de preview.

La actuación de estas cargas es un driver corporal sintético del builder M8; no contiene una performance facial M7. La evidencia de integridad aquí cubre ese primario presente y no reemplaza el demo combinado M6+M7+M8.

| FPS / duración | Frames | Simulación + calificación | QA de colisión adicional | Cache, incluida transacción | PC2 bytes | Peak working set del proceso tras cache |
| --- | --- | --- | --- | --- | --- | --- |
| 30 / 2 s | 61 | 9,446 s | 0,557 s | 1,648 s | 457.532 | 240,28 MiB |
| 30 / 4 s | 121 | 8,906 s | 1,164 s | 1,115 s | 907.532 | 243,81 MiB |
| 60 / 2 s | 121 | 9,158 s | 1,133 s | 1,087 s | 907.532 | 245,35 MiB |
| 60 / 4 s | 241 | 13,606 s | 1,728 s | 1,241 s | 1.807.532 | 256,89 MiB |

Son mediciones de una pasada y del proceso completo, no un promedio repetido ni memoria exclusiva del solver. Setup/checkpoint/QA tienen costes propios y el primer proceso puede pagar inicializaciones diferentes. No se deduce una regla de rendimiento proporcional a partir de estos cuatro tiempos.

El resultado mostró sensibilidad al FPS: máximo de desplazamiento secundario de 0,48478 m a 30 FPS frente a 0,25345 m a 60 FPS, y mínimo de área de 0,32827 frente a 0,98339. Pasar los bounds revisados de cada caso no demuestra equivalencia física entre FPS. Requiere calibración y revisión temporal para cada actuación/configuración; estas cifras no son tolerancias recomendadas.

Un intento anterior terminó antes de simular porque la ruta del store superaba el límite de Windows (`WinError 206`). Se conservó el fallo y se corrigió sólo el directorio del arnés a una ruta temporal corta. Las cargas físicas medidas son las cuatro de la tabla; no se modificó el backend para hacerlas pasar.
