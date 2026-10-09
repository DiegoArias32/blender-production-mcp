# QA de materiales M9

La QA técnica valida estructura, semántica, imágenes, color space, UV/UDIM y capacidades del renderer. La revisión visual comprueba la respuesta de superficie bajo iluminación equivalente. Construir nodos correctamente no demuestra realismo. No se calcula una puntuación general de calidad PBR.

Las tools `material_render_preview`, `material_render_multilighting` y `material_compare_before_after` comparten el renderer de `production/materials/render.py`. La transacción de `service.py` exige rol permitido, fuente/candidato vigentes, QA técnica y checkpoint; el renderer garantiza su propia captura y restauración. Una revisión sólo sirve para los artefactos y la candidata que realmente se inspeccionaron. El servicio comprueba hashes, pares fuente/candidato y las cuatro iluminaciones antes de permitir review/accept.

## Estudio controlado

Cada llamada crea una escena, world, copia de malla, cámara, luces y fondo propios. Usa el material fuente y el candidato sin escribir sus nodos ni sus assignments originales. La misma malla temporal, cámara, exposición, gestión de color, resolución y luces produce los pares `SOURCE` / `CANDIDATE`. Se eliminan exclusivamente los datablocks temporales creados por esa llamada, incluso cuando falla un render. Los PNG y `render.json` permanecen como evidencia.

El material analizado se aplica a todos los polígonos de la copia temporal para comparar ese shader. No representa la composición final de un objeto con varios slots. Los índices y materiales originales permanecen intactos.

La copia usa coordenadas de malla **raw** y valores actuales de shape keys. No es una captura de la deformación final: no añade rig, modifiers, Actions ni simulaciones a la malla del estudio. Los materiales o grupos animados/con drivers se rechazan en este adaptador estático. Un resultado favorable no certifica un personaje animado ni la apariencia en Unreal.

Iluminaciones disponibles:

| ID | Configuración | Uso de revisión |
| --- | --- | --- |
| `NEUTRAL` | Dos luces área amplias | Lectura general y color |
| `SOFT` | Fuentes de mayor tamaño | Roughness y transición de highlights |
| `HARD` | Fuente pequeña y fill reducido | Highlights definidos y discontinuidades |
| `GRAZING` | Fuente lateral próxima a la tangente | Normal/bump y lectura superficial |

Las vistas son `FRONT`, `THREE_QUARTER` y `CLOSE_UP`. El default del renderer incluye 3/4 y acercamiento central; el servicio completa las cuatro luces. El acercamiento central no descubre automáticamente la región más interesante: puede ser insuficiente para un asset alargado, con varias piezas o una costura fuera del centro. Registrar esa limitación en la revisión.

Capacidad: resolución entera de 64 a 512 por eje; 1–128 samples; hasta 4 luces de estudio × 3 vistas × 2 variantes = 24 PNG. Hasta 50.000 vértices raw y contexto de 128 objetos/4.096 huesos. No se transmiten imágenes como arrays JSON: se devuelven referencias locales, dimensiones, bytes y SHA256. Las rutas son nuevas y reservadas; no se sobrescriben archivos existentes. Una respuesta incompleta o con dimensiones inesperadas no es `ok`.

## Backend y límites físicos

El default es `BLENDER_EEVEE`, con raytracing `SCREEN` activado únicamente en la escena temporal. Se registra `engine_readback`, backend gráfico, samples y la configuración EEVEE observada. Workbench se rechaza porque no demuestra el shader PBR.

EEVEE aproxima SSS, transmisión y transparencia; la refracción depende de la pantalla, flags del material y espesor. El renderer no cambia los flags del material fuente o candidato para fabricar una respuesta. El dither de transmisión/alpha puede ser visible a pocas muestras. No se afirma equivalencia con transporte de luz de Cycles, ni espesor validado, ni translucidez fotorealista.

Cycles lee dispositivos y preferencias existentes; no habilita dispositivos, cambia el compute backend ni guarda preferencias. Si se exige OptiX/CUDA, debe coincidir el backend configurado y existir un dispositivo habilitado. GPU requerida y no disponible produce rechazo. CPU sólo se ejecuta con `allow_cpu_fallback=true`; `require_gpu=true` impide CPU aunque también se haya solicitado fallback. El reporte declara CPU cuando se usó CPU.

Las pruebas propias en Blender 5.2.1 LTS verificaron EEVEE/OpenGL, rechazo de OptiX no configurado y Cycles CPU explícito. La capacidad física de la tarjeta y un backend de preferencias habilitado son hechos distintos. Estas pruebas no certifican una ejecución Cycles/OptiX.

## Restauración y simulaciones

El render de otra escena puede reevaluar RNA manual en la escena original aunque el código no llame `frame_set`. Se reprodujo: una ubicación manual pasó de 4,403 a 4,233 y una shape key de 0,63 a 0,383. Se corrigió capturando conjuntamente todos los objetos del contexto, canales de pose, propiedades/constraints soportadas, delta transforms, valores de shape keys y NLA. La restauración reutiliza los canales M3, conserva el frame y actualiza el contexto conjuntamente. Se compara el estado final, no sólo el retorno del operador.

La actualización del dependency graph no permite prometer conservación de una simulación nativa activa. Antes de crear el estudio o restaurar RNA, se rechazan CLOTH, SOFT_BODY, FLUID, DYNAMIC_PAINT, PARTICLE_SYSTEM y OCEAN activos, rigid bodies con mundo activo, y Geometry Nodes con Simulation/Bake. El renderer **no desactiva** modifiers para pasar el guard. Una copia estática autorizada es una alternativa posterior, no una transformación automática.

La fixture de preservación tiene rig, pesos, Actions, shape key, UV, material original, un CLOTH desactivado y un archivo centinela de cache. El centinela comprueba conservación de bytes; no es una simulación bakeada. El negativo de CLOTH activo demuestra rechazo sin nuevos PNG/escenas y sin alterar rangos, `is_baked`, `is_outdated` ni `info` del PointCache. No se certifica renderizar una simulación nativa activa.

`Render Result` de Blender se sustituye; esa excepción temporal se informa. Los materiales, assignments, geometría, Actions, cache de disco y preferencias de la fuente deben permanecer iguales. Timeout o resultado incierto se gestiona por el ledger; no lanzar otra mutación con un ID nuevo para eludir la incertidumbre.

## Evidencia real de fixtures y renderer

`tests/blender/fixtures_m9.py` crea las 22 fixtures requeridas y un negativo adicional de tile ausente. Usa PNG de 32×32 creados localmente, material único de 3/5 tiles UDIM, y múltiples materiales cuando corresponde. Cada PNG se recarga y valida por sus píxeles reales; las siete semánticas tienen hashes distintos. Los cinco tiles de la fixture de material único tienen tintes deterministas distintos. Se guarda copia `.blend` fuente y backup de bytes idénticos. No hay assets descargados ni proyectos reales.

La biblioteca de siete imágenes usa `use_fake_user=True`: conserva incluso los mapas todavía no asignados a nodos. Un smoke MCP detectó que, sin ese flag, Blender descartaba AO al reabrir el `.blend`, aunque el PNG existía. El probe independiente de persistencia SAVE → OPEN pasó **7/7 checks** (`informe local privado; véase validation.md`): siete imágenes presentes, SHA de píxeles float y color spaces idénticos, archivos PNG/source/backup intactos y 23 casos conservados. Su reproducción está en `tests/blender/probe_m9_fixture_persistence.py`. No se reescribió la fixture que originó el fallo.

Probe vigente: `informe local privado; véase validation.md`, **31/31 checks**, con 41 PNG reales. Incluye 24 imágenes pareadas bajo cuatro luces/tres vistas, tres órdenes de rigs/pose manual/shape keys candidatas, fallo intencional después de una imagen, control con material idéntico, SSS/transmisión/alpha, CPU explícito y rechazo de simulación activa. Los 32 tests unitarios pertinentes están en `tests/unit/test_m9_render.py`; resultado `test-artifacts/m9-render/unit-native-guard.xml`.

El control de mismo material produjo píxeles RGB idénticos. En el probe de respuesta de features, la diferencia máxima RGB fue aproximadamente 0,007843 para SSS, 0,419608 para transmisión y 0,678431 para alpha. Esas diferencias certifican respuesta medible del shader frente al feature deshabilitado bajo la misma luz; no son una puntuación artística. SSS fue muy sutil en esta esfera de 1 m y escala SSS 0,03. La revisión de la demo final exige mirar sus propias imágenes, no heredar esta conclusión como aprobación.

Estos son probes en proceso Blender propio con payload validado por safe mode. La calificación de transporte MCP completo y las demos de ciclo de vida se registran separadamente en el informe del milestone.

## Rendimiento medido y alcance

`tests/blender/probe_m9_performance.py` midió tres casos finitos en un proceso propio, sin competir con los renders MCP del coordinador. Cada caso produjo 16 PNG de 192×192, 32 muestras EEVEE, con dos vistas y cuatro luces. Fuente y contexto quedaron iguales.

| Caso | Nodos / imágenes / tile-dependencias | Inspección | Render completo | JSON interno / resumen público |
| --- | --- | --- | --- | --- |
| Pequeño | 2 / 0 / 0 | 0,147 s | 3,741 s | 7.424 / 1.651 bytes |
| Mediano | 8 / 4 / 0 | 0,135 s | 3,023 s | 78.668 / 2.654 bytes |
| Complejo multi-UDIM | 24 / 5 / 15 | 0,085 s | 3,347 s | 113.273 / 3.906 bytes |

El complejo tiene un grupo con 12 nodos Math, ramas de Base Color/Roughness/Normal, bump, coat y AO independiente. Hay cinco UV tiles distintos; los tres sets tiled suman 15 dependencias de tile. No es un shader de dos nodos renombrado como complejo.

Estimaciones de almacenamiento base de píxeles: 0 / 16.384 / 69.632 bytes; no son memoria GPU residente. El pico working set acumulado del proceso propio fue 572.928.000 / 644.997.120 / 681.955.328 bytes, leído con `GetProcessMemoryInfo`. Es un máximo desde el arranque del proceso, no un pico aislado de cada caso ni VRAM. El resumen público excluye el sobre JSON-RPC/MCP. Tiempos cortos incluyen compilación/caches y no permiten concluir que el caso complejo sea universalmente más rápido.

Evidencia: `informe local privado; véase validation.md`, **10/10 checks** y 48 PNG. Se midió antes de añadir el guard conservador de simulaciones activas; los casos tienen CLOTH desactivado y no dependen de simulación. No se repitió un benchmark ya aprobado para buscar cifras más favorables.

## Fallos y correcciones conservados

El índice `test-artifacts/m9-render/evidence-index-final.json` referencia los originales, sus hashes y el alcance real de cada run. Se preservaron:

- `bpm9render-1a810b29`: fallo del arnés al importar el SDK host dentro del Python de Blender. Se corrigió el empaquetado del probe; no se instaló otro SDK en Blender.
- `bpm9render-75f70844` y `bpm9render-3e2ffdc4`: fallo de restauración de RNA manual reproducido, después corregido.
- `bpm9render-9591690b` y `bpm9render-0db9ede5`: checks de restauración/backend válidos, pero después se descubrió que las texturas generadas eran negras. No califican los canales de textura.
- Error de fixture: cambiar color space después de escribir píxeles descartaba el buffer generado. Todos los PNG antiguos compartían SHA `a705077c…`. Se corrigió el orden metadata → pixels → update/save → recarga → validación de rangos, creando nuevas fixtures; las antiguas no se reescribieron.
- `bpm9render-0f4c9d48`: primer run con texturas corregidas; `bpm9render-919b1589`: añadió diferencias RGB reales y control idéntico. La diferencia de SHA del PNG por sí sola dejó de usarse como demostración de cambio visual.
- `unit-final.xml`: safe mode rechazó `as_pointer` al añadir guards de grupos. Se sustituyó por referencias RNA en conjuntos; el validador upstream permaneció intacto. `unit-native-guard.xml` es el resultado corregido.

El estado vigente exige revisión de los artefactos actuales. Un PASS histórico parcial no se transforma retrospectivamente en una calificación completa de M9.
