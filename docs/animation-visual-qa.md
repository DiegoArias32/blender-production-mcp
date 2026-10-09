# QA visual y temporal de animación

Datos exactos mediante MCP/bpy; evaluación visual mediante viewport/renders y
reproducción de la secuencia. Las dos evidencias se complementan. No se confunde
haber generado un PNG con revisar una animación.

`production.animation_generation_render` produce vistas equivalentes FRONT,
SIDE y THREE_QUARTER y vistas útiles configuradas. Las secuencias PNG conservan
frame/FPS; APNG y `preview.html` local permiten reproducción sin servicios
externos. Se restauran asignación Action/slot, frame, pose y contexto de render.

El reproductor usa tiempo transcurrido y el FPS del brief, independiente de la
frecuencia de callbacks del navegador. Permite velocidad normal y un cuarto,
pausa y navegación por frame. Tras una demora del navegador recupera el tiempo
saltando imágenes; para inspeccionar cada imagen usar pausa/frame o APNG. Las
pruebas ejecutan el JavaScript real con 30/60 FPS, callbacks omitidos y demoras.
Esto valida el reloj, no garantiza que una pantalla lenta muestre cada frame.

Presupuesto M6 explícito: hasta 601 frames por vista, 1.830 imágenes y 200
millones de píxeles. La duración/FPS del brief deben coincidir con la escena.
Capacidad excesiva se rechaza antes de renderizar; no truncar silenciosamente
frames. M1/M2 conservan sus propios límites heredados.

Cycles en CPU requiere permiso explícito `allow_cpu_fallback=true`; backend
real/fallback se informa. No editar preferencias globales, addons ajenos o el
Blender de producción para facilitar previews. Timeout puede dejar Blender
trabajando: no repetir render automáticamente; conservar evidencia parcial.

## Informe estructurado

Separar:

- **Métricas técnicas:** contactos/sliding en metros, root, pops/stretch,
  deformación, velocidades/continuidad, frames y thresholds.
- **Observaciones artísticas:** peso, readability, silueta, timing, spacing,
  anticipación, overlap, follow-through, stiffness, balance y momentum.

Registrar reviewer, artefacto actual, secuencia revisada, hallazgos, límites y
decisión ACCEPT/REJECT/REFINE. No asignar un score AAA arbitrario. La decisión
no debe referir una candidata o render anterior a la última modificación.

Stills ayudan a revisar poses/silueta. No bastan para timing, spacing, pops,
percepción de peso, recuperación o continuidad de arcos. Revisar también final
→ inicio en ciclos, y antes/después equivalentes en refinamientos.

Los proxies de silhouette/bone heads no descartan clipping de malla, defects
ocultos ni detalles menores que la resolución. Documentar estos límites junto
con la decisión. El informe de cierre incluye sólo observaciones realmente
revisadas y rutas verificadas.

Antes de trabajar con una WORKING_COPY, calibrar marker/sole, plano y unidades;
registrar tolerancias de contacto y sliding por separado. La proximidad al suelo
es una heurística: puede clasificar un swing bajo como posible contacto. La
[calibración de locomotion](locomotion-generation.md) conserva ese diagnóstico
y la medición con el plano verificado, sin elevar la tolerancia de sliding.
Confidence de mapping describe evidencia estructural; no es una probabilidad
estadística calibrada ni sustituye la validación del rig.
