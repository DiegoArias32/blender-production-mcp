# Cache de simulación — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. La disponibilidad de un archivo de cache no demuestra que la simulación haya pasado estabilidad, colisiones, integridad o revisión temporal.

## Dependencias y frescura

`production.physics_cache_plan` declara representación, rango, configuración, dependencias, destino y gates. `physics_bake_cache` conserva la candidata y escribe sólo datos propios. Las llamadas posteriores reutilizan `physics`; el plan y sus firmas no se editan manualmente.

El cache se vincula a la fuente y a la actuación corporal/facial relevante. Cambios de Action, keys/handles, NLA, geometría, colliders, configuración física, FPS, escala, gravedad o ventana invalidan el resultado. Los datos se revalidan antes de reutilizarlo. Un cache stale no se rebautiza como fresco: exige volver a analizar y generar una propuesta vigente.

`cache.verify` comprueba pertenencia al store `physics-cache`, referencia al manifest, hash SHA256, tamaño y fingerprint de dependencias. Devuelve `FRESH` cuando esas comprobaciones coinciden; cambios de contenido o entradas relevantes devuelven `STALE_CACHE`. El control de frescura complementa la propiedad de la candidata y la verificación de integridad primaria.

## Cloth: memoria nativa y PC2 portable

El backend Cloth simula con guard en cada frame entero y congela la memoria mediante `ptcache.bake_from_cache` sólo si el guard termina correctamente. A partir del congelado se recogen muestras de topología fija. Las coordenadas grandes son datos internos para cache; no se vuelcan al modelo como respuesta pública.

`write_pc2(storage, operation_id, frames, dependencies, frame_start)` escribe desde el host en una carpeta reservada y un archivo exclusivo. Valida frames completos/contiguos, cantidad de vértices constante, bytes y coordenadas finitas. El formato almacena XYZ local de mesh en float32 little-endian y una muestra por frame. Los límites del adaptador son 601 muestras, 20.000 vértices y 256 MiB por cache; son bounds de capacidad de M8, no recomendaciones de densidad artística.

El servicio aplica además un preflight de transferencia más estricto para mesh: hasta 16.000.000 bytes de muestras float32 estimadas en la ruta vigente. Una combinación de vértices y frames puede caber en el formato PC2 y aun así exceder el transporte permitido; se rechaza antes de crear la candidata. El plan informa su capacidad efectiva y no confunde el límite del archivo con el del recorrido completo.

El manifest `production.physics_cache.v1` conserva representación `PC2`, estado, ruta, SHA256, frames, vértices, bytes, dependencias y settings de playback. La escritura de filesystem permanece en el host; no se amplía la allowlist upstream con primitivas arbitrarias de archivos.

La copia de playback añade `MESH_CACHE` propio con `cache_format='PC2'`, `deform_mode='OVERWRITE'`, `time_mode='FRAME'`, `play_mode='SCENE'`, factor 1 y escala de tiempo 1. Sólo se desactiva el Cloth propio en esa copia. Sus deformadores anteriores se conservan y el cache representa la geometría evaluada completa.

`frame_start` debe ser el primer frame del clip. Un probe intermedio con `frame_start=0` adelantó una muestra y midió 0,15 m de error; el ajuste se corrigió y la evidencia fallida se conserva. Ese número describe ese probe, no una tolerancia general. `PointCache.filepath` tampoco redirige el cache nativo cuando `use_external=False`; por ello no se usa esa propiedad para fingir un directorio portable propio.

## Otras representaciones y reapertura

Los backends procedurales ya producen canales en Actions separadas. Su manifest vincula entradas y artefactos; no se fuerza cada subsystem a un point cache Cloth. [El bake reusable](physics-baking.md) conserva la representación apropiada y exige comparación con las muestras validadas.

La validación debe incluir reapertura, evaluación fuera de orden y el rango completo. La igualdad de una fixture no demuestra determinismo universal de Blender o de futuras configuraciones. Variaciones observadas se documentan con métricas, contexto y método. Un timeout incierto no autoriza repetir la mutación con otro `operation_id`.

En el probe controlado de reapertura (archivo local no distribuido; [validación pública](validation.md)), Blender 5.2.1 conservó el congelado nativo de 18 frames al guardar/reabrir esa fixture (`is_baked=true`). El playback PC2 también se verificó contra las muestras anteriores al cierre, en el rango completo y fuera de orden, con bytes y archivos fuente intactos. Es evidencia limitada a esa configuración; no se promete persistencia universal del cache de memoria. La ruta portable mantiene su propia validación y no necesita inferir la frescura de PC2 a partir del flag nativo.

El benchmark de 625 vértices (archivo local no distribuido; [validación pública](validation.md)) permitió otra comprobación acotada sin nuevas ejecuciones: el prefijo de movimiento compartido por clips de 2 y 4 segundos produjo coordenadas PC2 idénticas entre procesos propios al mismo FPS. Se compararon 61 frames a 30 FPS y 121 a 60 FPS; máximo/RMS medidos fueron cero y los bytes de esos prefijos coincidieron. Esa reproducibilidad del prefijo compartido no se extrapola a otros rigs, solvers o FPS; los resultados entre 30 y 60 FPS de la fixture difirieron.
