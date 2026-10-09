# Generación de ataques corporales

`production.animation_generate_attack` admite intención OVERHEAD, SWING,
THRUST, SLAM o CUSTOM_BODY_ATTACK, duración, miembro dominante, dirección,
perfil de peso, root y contacto objetivo cuando exista adapter compatible.

Fases principales: NEUTRAL, ANTICIPATION, COMMITMENT, STRIKE, IMPACT,
FOLLOW_THROUGH y RECOVERY. Frame/time, soporte, root y objetivos de pose están
disponibles antes de generar keys. Los repartos son defaults declarados y
pueden revisarse; no se imponen tiempos universales.

Para heavy, anticipación y recuperación reciben más espacio mientras el evento
de impacto sigue siendo breve. Pelvis puede comprometerse antes que torso;
breakdowns añaden liderazgo/retraso. No bajar todas las velocidades por igual.

Una criatura asimétrica utiliza masas relativas para compensación y
counter-rotation. No renormalizar dimensiones, cambiar rig ni imponer simetría.
La lectura del ataque, inercia, recuperación y silueta requieren preview temporal.

## Gates del ataque

1. Compatibilidad, mapping/controles confiables y alcance de limbs.
2. Blocking: propósito claro, anticipation/impact/recovery y soporte coherentes.
3. Contactos de pie confirmados; target de mano/arma sólo con adapter explícito.
4. Root sin teleport/drift no solicitado; pop/stretch/deformación medidos.
5. Spline/polish preservan sharpness de impacto, holds y anchors.
6. Revisión temporal de tres vistas, observaciones artísticas y lifecycle explícito.

Un OVERHEAD que técnicamente alcanza un target puede tener una silueta pobre,
reacción de torso débil o recuperación robótica. Registrar estas observaciones
sin inventar una puntuación AAA ni declarar arreglado lo que no se ha verificado.
