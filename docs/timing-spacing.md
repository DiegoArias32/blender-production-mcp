# Timing y spacing

`production.animation_analyze_timing` devuelve duraciones por fase, frames de
impacto y provenance. `animation_adjust_timing` propone una modificación
controlada; aplicar necesita checkpoint/candidata y verificación. Cambiar
tiempo no cambia FPS de la escena.

El planner inicial permite `phase_fractions` explícitas para fases distintas,
estrictamente crecientes entre 0 y 1. No se aceptan fases que colisionan tras
cuantización. Locomotion inicial conserva su modelo de eventos de un ciclo;
no reinterpretar un reparto arbitrario como contactos validados.

`production.animation_analyze_spacing` usa trayectorias evaluadas en Blender.
La distancia entre muestras y FPS producen velocidad en m/s y aceleración en
m/s². Cambios fuertes pueden ser impacto intencional o defectos; reportar
frames y contexto de fase. Spacing uniforme no es necesariamente un problema
cuando el brief lo solicita.

## Generación y curvas

Blocking es CONSTANT. Breakdowns conservan intención de liderazgo/retraso y
contactos. `animation_spline` genera suavidad de forma selectiva mediante
F-Curves/Bézier, con handles/continuidad revisados. No suavizar globalmente
contactos, holds o impacto.

`animation_polish_curves` reutiliza análisis de F-Curves, Bézier, quaternions y
derivadas. La propuesta debe declarar qué defecto/canales corrige. Preservar
fases, root, contactos, sharpness intencional y ventana de mutación. QA densa
entre keys acota el comportamiento interpolado; no implica igualdad matemática
continua ni aceptación artística.

Start/stop coordina velocidad root con transición, evitando pasar de reposo a
desplazamiento completo en un frame. Medir velocidades reales a ambos lados
de la transición. Un render temporal permite comprobar anticipación, spacing
e impacto; stills aislados no son evidencia suficiente.
