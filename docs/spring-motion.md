# Spring bones, chains y cables — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. El solver host es una aproximación controlada de masa, muelle y amortiguación. No es un solver general de cuerdas, joints o rigid bodies.

## Modelo e integración

`production.physics_setup_spring_chain` valida una cadena explícita; `physics_generate_secondary_bones` genera una Action separada en una copia propia. Las clasificaciones `SPRING_BONE`, `CHAIN_LIKE` y `CABLE_LIKE` comparten el backend segmentado acotado. Su manifiesto contiene `chain.armature` y `chain.bones`, en orden jerárquico. No se deducen nombres anatómicos como certezas.

Los parámetros son `mass`, `stiffness`, `damping`, `substeps`, `gravity_influence`, `lag_s`, `max_displacement_m`, `max_angle_deg` y `excitation_scale`. Los valores iniciales de `SPRING_DEFAULTS` son propuestas de calibración para fixtures. Las distancias se expresan en metros, el tiempo en segundos y los límites angulares en grados.

`integrate_offsets(positions, fps, settings)` integra con Euler semiimplícito y substeps. La excitación procede de diferencias finitas de velocidad/aceleración primaria y del atraso configurado. La gravedad sólo contribuye con su influencia explícita. El desplazamiento satisface:

`x'' = -(k/m)(x + lag·v_primary) - (c/m)x' - excitation·a_primary + gravity_influence·gravity`.

Antes de integrar se exige `h²·k/m + 2·h·c/m < 4`, con `h = 1/(fps·substeps)`. Una combinación inestable se rechaza; no se oculta con un clamp posterior. También se rechazan NaN, infinitos y drivers o estados que excedan los bounds del adaptador.

La proyección del desplazamiento elimina la componente de velocidad dirigida hacia fuera del límite. Se informan `displacement_clamp_count` y `angle_clamp_count`: los clamps son intervenciones del control, no pruebas de calidad física. Sin movimiento primario y con gravedad desactivada, la salida es neutral y no introduce ruido.

## Cadena y orientación

`solve_chain(samples, lengths, fps, settings)` recibe frames contiguos, posición XYZ, quaternion WXYZ y direcciones world opcionales por hueso. Mantiene el root exactamente sobre el driver y longitudes fijas. La respuesta se acumula por jerarquía y lag; no copia un único oscilador a todos los segmentos. Proyecta las direcciones dentro del máximo angular y distribuye el presupuesto de desplazamiento a lo largo de la cadena.

El writer almacena `base_local × delta` normalizado como quaternion absoluto de cada hueso secundario, en una Action nueva y una pista NLA `REPLACE` cuyos canales son disjuntos de la actuación primaria. Conserva bases locales no neutrales y rechaza cualquier solapamiento de canales. La mezcla `COMBINE` mostró una distorsión pequeña de las rotaciones evaluadas en Blender 5.2.1; el diagnóstico (archivo local no distribuido; [validación pública](validation.md)) y la comprobación posterior con base de 6° (archivo local no distribuido; [validación pública](validation.md)) sustentan la elección actual. La QA sigue midiendo la orientación mundial frente a la primaria del mismo frame, además de la geometría evaluada; ninguna tolerancia se amplía para hacer pasar el writer.

El adaptador de Blender convierte las direcciones simuladas usando las bases/rest pose medidas del rig. No supone que un eje local coincide con un eje corporal. Revalida jerarquía, longitudes, constraints, modos y ausencia de canales primarios en los huesos secundarios antes de escribir. Los resultados JSON del solver son muestras intermedias; la salida reusable son canales propios de una Action distinta y, para un rig copiado, una capa NLA secundaria separada.

Las longitudes world evaluadas deben coincidir con las longitudes fijas declaradas en todos los frames del driver. Una escala o deformación jerárquica que las cambie se rechaza como `SECONDARY_CHAIN_SCALE_UNSUPPORTED` antes de crear la candidata. Tras escribir, la QA recibe los límites actuales de desplazamiento y ángulo para comprobarlos en Blender; los máximos predichos del solver no sustituyen esas medidas.

Para una malla vinculada, el presupuesto interno se reduce según el brazo de palanca de los vértices ponderados respecto a sus huesos. Un endpoint limitado no controla automáticamente un vértice fuera del eje. La proporción conservadora se registra en `solver_configuration`; no aumenta el límite público y la QA sigue evaluando todos los vértices de la malla. Una proporción inválida o no finita se rechaza antes de resolver/copiar.

Chains y cables son aproximaciones segmentadas con raíces declaradas. No se ofrecen redes arbitrarias de joints, tensión de cuerda continua, topología cambiante ni un solver universal de ropes.

## Mediciones y revisión

`motion_metrics` informa máximos de desplazamiento, velocidad y aceleración. Delay, overshoot, periodo, decay y settling sólo se informan cuando las muestras permiten medirlos. La ausencia de excitación o de una cola en reposo mantiene `None` y `UNKNOWN`; el periodo no se inventa usando únicamente la stiffness configurada. El método y las unidades acompañan los valores.

La QA geométrica comprueba root, longitudes, límites y colisiones en los frames evaluados; el radio de una cadena sin superficie es una declaración del plan, no grosor inferido. Se revisa el preview temporal y [la equivalencia del bake](physics-baking.md). La amortiguación técnica no certifica timing artístico, y el muestreo entero no garantiza todos los subframes.

El pipeline técnico intermedio de spring (archivo local no distribuido; [validación pública](validation.md)) midió 0,09989 m de desplazamiento en una fixture cuyo bound público era 0,1 m, roots sin separación, stretch 1,05618 y ángulo máximo 7,63608°. Pasó colisiones muestreadas y bake geométrico de todos los vértices en 61 frames, con error máximo/RMS cero y fuente intacta. No incluyó review artístico ni aceptación; esas cifras no se proponen como calibración universal.
