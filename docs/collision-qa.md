# Colisiones, penetración y estabilidad — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. Los resultados expresan medidas y cobertura del método. Un resultado numérico aprobado no constituye certificación física ni sustituye el preview temporal.

## Colliders y penetración

`production.physics_prepare_colliders` trabaja con objetos explícitos y copias/proxies propios; conserva las render meshes fuente. La desviación de un proxy respecto a la superficie fuente debe medirse y acompañar sus límites. Menos detalle sólo es útil si conserva una cobertura suficiente del cuerpo. El plan declara qué colliders necesita cada subsystem.

La estrategia `COPY` conserva la pila de deformación admitida. `AABB_PROXY` crea una caja rígida envolvente de ocho vértices y exige geometría sin deformadores ni morphs; no sustituye una render mesh. El método vigente mide distancias de los vértices del proxy hacia la superficie fuente mediante BVH. Es una medida muestreada en esa dirección, no distancia Hausdorff completa ni certificación de contacto continuo.

Un probe controlado de simplificación (archivo local no distribuido; [validación pública](validation.md)) pasó tres casos con Blender real: cubo de ocho vértices → proxy de ocho, desviación cero; esfera de 114 vértices → proxy de ocho, desviación máxima muestreada 0,75066 m aceptada sólo con el threshold explícito de 1 m de esa fixture; la misma esfera rechazó 0,01 m con `PHYSICS_PROXY_DEVIATION_EXCEEDED`. Esos thresholds ilustran el gate de un caso sintético, no una tolerancia propuesta para personajes. Se conservaron SHA256, firmas, geometría y visibilidad render fuente, y se verificó ownership por generación/versión de copias y colección. No hubo review artístico.

`physics_analyze_penetration` y `physics_collision_report` evalúan la candidata frente a los colliders declarados en los mismos frames. El runtime requiere mallas evaluadas finitas, cerradas y con orientación consistente. Comprueba que cada arista triangulada pertenece a dos caras con winding opuesto. Un collider abierto o no manifold devuelve `UNKNOWN` y bloquea el gate; no se interpreta como penetración cero.

El método utiliza BVH y tres rayos de paridad por muestra. Decide inside/outside por mayoría y aproxima la profundidad mediante la distancia a la superficie más próxima. Una normal de la cara más cercana, por sí sola, no decide si el punto está dentro. Informa desacuerdos entre rayos y rechaza truncamiento por capacidad.

| Resultado | Significado |
| --- | --- |
| `frames_affected` | Frames enteros muestreados con penetración detectada |
| `penetration_vertex_frame_pairs` | Suma de muestras penetrantes por frame; no número de defectos únicos |
| `max_penetration_depth_m` | Profundidad aproximada máxima, en metros |
| `affected_sample_duration_seconds` | Cantidad de frames afectados dividida por FPS; duración del muestreo, no intervalo continuo demostrado |
| `coverage`, `confidence`, `limitations` | Representación, cobertura y límites reales del método |

Para mesh se usan sus vértices evaluados. Para armature se usan nueve muestras por segmento de hueso declarado y un `segment_radius_m` opcional. El radio procede del plan; no se infiere grosor físico. Radio cero sólo aporta evidencia de centerline, con confianza débil y grosor desconocido.

Los vértices o segmentos pueden omitir intersecciones entre aristas y caras o entre muestras. Los frames enteros tampoco cubren todos los subframes. `volumetric_collision_freedom_certified` permanece falso. Self-collision nativa de Cloth puede configurarse, pero su mera activación no equivale a una prueba volumétrica de autointersección cero.

## Guard de estabilidad

`physics_analyze_stability` y `physics_analyze_motion` comprueban coordenadas finitas, cambios de topología, desplazamiento, velocidad/aceleración, crecimiento de bbox, stretch de aristas y degeneración. Cloth ejecuta el guard durante la simulación en cada frame antes de congelar el cache. Detectar runaway detiene ese resultado; no se siguen rebakeando parámetros hasta que deje de fallar.

Las derivadas world incluyen movimiento primario. Cuando hay referencia fuente se informan además derivadas del residual secundario, para distinguir el movimiento del cuerpo y el añadido por M8. Los máximos por frame y por clip conservan sus unidades. El límite de desplazamiento secundario del solver se comprueba aparte de un umbral general de estabilidad.

La inversión global se mide por el signo de volumen sólo en una superficie cerrada orientada. En cloth abierto, doblar o rotar una normal es un pliegue posible; el método no declara inversión global para esa superficie.

Los thresholds del runtime son `max_displacement_m`, `max_velocity_m_s`, `max_acceleration_m_s2`, `bbox_growth_ratio`, `max_edge_stretch_ratio`, `degenerate_fraction`, `min_area_ratio`, `max_penetration_depth_m`, `max_penetrating_vertices`, `anchor_detachment_m`, `root_detachment_m` y `bake_max_error_m`. Son finitos, revisables y de origen propuesto. Su significado y unidades deben mantenerse al normalizar el brief/plan; campos desconocidos o booleanos numéricos se rechazan. La QA recibe además `secondary_displacement_limit_m` y, en adaptadores angulares, `secondary_angle_limit_deg`, basados en los settings vigentes.

Los errores técnicos, el estado `UNKNOWN` y [las observaciones artísticas](physics-visual-qa.md) se conservan por separado. No se transforman en un score de realismo ni se omiten para aceptar una candidata.

## Reproducción de probes aislados

Los runners versionados se ejecutan con el Python del proyecto: `tests/blender/probe_m8_proxy.py`, `probe_m8_performance.py` y `probe_m8_positive.py`. Comparten `m8_pipe_harness.py` y `m8_pipe_worker.py`: procesos Blender propios con `--background --factory-startup --disable-autoexec`, comandos validados por `build_code`/safe_mode y pipes locales, sin addon, GUI o puerto de escucha. Sus salidas se guardan en directorios temporales exclusivos y el runner imprime la ruta del reporte.

El runner positivo del caso combinado exige los artefactos controlados M6/M7 aceptados que referencia la evidencia histórica; si faltan, rechaza su construcción en vez de fabricar una actuación equivalente. Ejecutar estos probes no registra una revisión visual ni aceptación artística.
