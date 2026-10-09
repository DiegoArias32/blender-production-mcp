# Análisis y refinamiento de arcos

`production.animation_analyze_arcs` mide manos, pies, cabeza, pelvis y
effectors configurados sobre la Action candidata evaluada. Resultados compactos:
longitud de trayectoria, desplazamiento neto, velocidad/aceleración, cambio de
dirección y frames a revisar. No se supone que toda trayectoria deba ser circular.

Un efector estacionario no tiene tangente de dirección definida. Un cambio
angular grande cerca de contacto/impacto puede ser intencional; no se convierte
automáticamente en spike. Revisar unidades, FPS, frame y fase.

`production.animation_refine_arcs` es propuesta primero. Preservar contactos,
key phases, impact frame y root intent. El cambio se aplica en otra candidata,
con métricas before/after comparables; un smoothing que destruye el impacto o
desplaza un pie plantado falla aunque reduzca una derivada.

El helper puro `trajectory_metrics` acepta muestras XYZ en metros y frames
crecientes, devuelve distancia/velocidad/aceleración y cambios de dirección.
Muestreo y proxy de bone head no equivalen al recorrido del extremo de arma o
su volumen. Para un arma/contacto personalizado hace falta efector/marker
explícito y adapter validado.

Los arcos se revisan temporalmente en FRONT, SIDE y THREE_QUARTER. Una proyección
puede ocultar un defecto en profundidad; no certificar arcos sólo por una vista
o una gráfica suave.
