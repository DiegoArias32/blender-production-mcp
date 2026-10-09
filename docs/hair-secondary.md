# Movimiento secundario de hair — M8

Estado vigente: M8 calificado sobre fixtures controladas, 0.8.0. El backend calificado debe declararse en cada respuesta; detectar curves, strands o un modifier no demuestra que exista un adaptador seguro de dinámica nativa.

## Representación admitida

`production.physics_analyze_hair` inspecciona la representación y sus relaciones. `physics_plan_hair` exige roots, cadena, regiones fijas, longitudes, colliders, configuración y QA. `physics_generate_hair_secondary` usa el backend procedural de huesos explícitos cuando la compatibilidad lo permite.

M8 ofrece un adaptador acotado de cadena de huesos, con cards o mesh strips vinculados al armature cuando corresponde. La cadena declara `armature` y una lista ordenada `bones`; sus miembros deben existir, formar una jerarquía continua y estar libres de constraints incompatibles. La malla debe estar realmente vinculada al armature declarado. Se conservan los huesos/rest pose y pesos de la fuente; el Armature modifier de la copia apunta al rig candidato.

No se ha calificado un adaptador general de groom/curves/strands nativos para simulación dinámica. Esas representaciones devuelven `UNSUPPORTED_BY_CURRENT_BLENDER_API` o una limitación explícita. No se reconstruye un groom ni se afirma que el backend de huesos sea simulación física de strands.

## Roots, cuerpo y cara

La generación toma posiciones y orientaciones primarias evaluadas del anclaje/cabeza. El solver mantiene el root anclado y las longitudes de los segmentos. La respuesta deriva de velocidad, aceleración, orientación y lag configurado; no añade oscilaciones aleatorias. La Action secundaria pertenece al rig copiado y sus canales son distintos de los canales corporales/faciales primarios.

Si una Action primaria o strip NLA ya anima un hueso de la cadena secundaria, la configuración se rechaza como solapamiento. Tampoco se cambia el modo de rotación del rig fuente: el adaptador exige el modo quaternion existente que sabe escribir. El movimiento de cabeza y la actuación facial se conservan y se vuelven a firmar antes de cada mutación.

La pista secundaria usa NLA `REPLACE` únicamente en esos canales disjuntos y escribe quaternions absolutos normalizados: `base_local × delta`. La base proviene del pose local primario muestreado; no se sustituye por identidad. La Action y las pistas corporales/faciales se conservan. Esta elección evita la distorsión angular pequeña medida con `COMBINE` en Blender 5.2.1; no implica reemplazar el movimiento primario ni relajar su verificación.

## QA y uso

Las llamadas usan `physics` como referencia del plan y `options` para configuración revisada; `apply=false` mantiene la propuesta. La QA mide root detachment, desplazamiento, velocidades/aceleraciones, límites y colisiones con cabeza, cuello, hombros o colliders personalizados. Para una malla vinculada puede evaluar los vértices completos. Una cadena sin malla se aproxima mediante muestras de segmentos: su radio debe declararse; una centerline sin grosor ofrece cobertura débil.

Un collider abierto/no manifold produce `UNKNOWN` y bloquea el gate obligatorio. El test de vértices/segmentos puede omitir intersecciones entre aristas y caras. Además de la QA técnica se revisa el preview temporal de tres vistas, en velocidad normal y lenta, observando atraso, rigidez, energía, asentamiento y penetración.

La reutilización emplea una Action separada y/o cache geométrico apropiado; [el bake](physics-baking.md) debe conservar la actuación validada. Las limitaciones de [spring](spring-motion.md) y [colisiones](collision-qa.md) también se aplican a hair basado en huesos. Ninguno de estos gates certifica un groom de producción ni reemplaza su revisión visual.

## Calibración de la fixture de cards

La fixture controlada fija la fila superior exclusivamente al hueso primario de cabeza y declara `M8_ExplicitSecondaryRoots` como máscara de roots. Ninguno de esos seis vértices tiene pesos en los huesos dinámicos. Mantener sólo el root central de una cadena no garantiza que los puntos fuera del eje permanezcan adheridos; la QA comprueba los vértices de la máscara contra la fuente en cada frame.

En esta fixture se propone `max_angle_deg=2` para moderar el shear de los límites entre pesos rígidos de las cards. Es calibración del caso y conserva `PROPOSED_PARAMETER`; no cambia defaults de tools ni tolerancias de QA. Un probe intermedio de esa corrección (archivo local no distribuido; [validación pública](validation.md)) midió root detachment cero, desplazamiento máximo 0,02116 m, stretch 1,08138 y penetración cero. Sin embargo, la orientación evaluada de un hueso llegó a 2,01101° frente al bound de 2°, y la candidata quedó en `SECONDARY_CANDIDATE` al fallar QA. El resultado no se presenta como PASS: demuestra por qué la predicción de dirección del solver y la orientación real de Blender se comprueban por separado.

El diagnóstico comparativo (archivo local no distribuido; [validación pública](validation.md)) aisló el efecto de `COMBINE`: normalizar las keys conservó el exceso; cambiar sólo la mezcla a `REPLACE` conservó la orientación escrita y todos los huesos primarios. Después del cambio del writer, el pipeline vigente de dos fixtures (archivo local no distribuido; [validación pública](validation.md)) pasó generación, QA, cache y bake: máximo angular 2,00000104° con base neutral y 2,00000152° con base secundaria estática de 6°. Ambos cumplen el bound con la tolerancia numérica existente de 0,00001°; roots cero, primaria original y huesos no secundarios de la copia sin desviación, PC2 de 61 frames y error máximo/RMS de bake cero. Archivos fuente y backend permanecieron iguales. Es calificación técnica de fixtures; no incluye revisión visual ni aceptación artística.
