# Mecánica corporal y masa percibida

`production.animation_analyze_body_mechanics` mide trayectorias de root,
pelvis, torso, cabeza y effectors, y devuelve proxies de soporte. No calcula
biomecánica ni un centro de masa físico fiable de cualquier personaje.

La masa percibida depende del compromiso de pelvis, desplazamiento corporal,
anticipación, duración del impacto, retraso de torso, momentum, overlap y
recuperación. HEAVY no se implementa como reducir toda la velocidad a la mitad.
En el planner inicial un ataque heavy reserva más tiempo a anticipación y
recuperación, mantiene el evento de impacto corto y hace liderar a la pelvis.
Estos repartos son defaults explícitos, no reglas universales.

## Asimetría intencional

`mass_asymmetry={"left":1,"right":2}` significa masas relativas como guía de
movimiento. No cambia huesos, proporciones ni geometría. El planner propone
compensación lateral, counter-rotation y respuesta distinta del miembro con
mayor masa. El perfil ASYMMETRIC_CREATURE no se normaliza hacia un humano
simétrico. Mapping, unidades, orientación y alcance deben comprobarse sobre
el rig instalado.

`production.animation_refine_balance` sólo debe modificar cuando el adapter y
la evidencia lo permitan. Una pose de impacto, lunge, stumble o recuperación
puede ser deliberadamente inestable; el proxy fuera del soporte es un indicador
para revisar intención, no una orden automática de corregir.

El proxy pelvis/segmento entre pies no certifica apoyo si ambos están en vuelo.
Interpretar junto con contactos confirmados. Las trayectorias/derivadas medidas
no sustituyen reproducción temporal de peso, recuperación y coherencia.

Overlap y follow-through deben derivar de fases y momentum existentes, con
cadena/delay/strength declarados. No añadir oscilación genérica. Si una
refinación no mejora el problema dirigido, conservar evidencia y revertirla.
