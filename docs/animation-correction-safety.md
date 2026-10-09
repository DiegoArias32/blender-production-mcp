# Seguridad de correcciones de animación

M2 conserva original, exige precondiciones revisables y separa medición de
aceptación visual. El alcance implementado es conservador: rechazar un caso
no soportado es un resultado válido, no una invitación a borrar rig, constraints
o datos para hacerlo pasar.

## Estado de una operación

| Fase | Efecto |
|---|---|
| `analyze` | Inspecciona soporte y calcula propuesta; no edita Action. |
| `propose` | Guarda propuesta inmutable en la raíz `operations` del workspace. |
| `preview` | Verifica precondiciones, crea checkpoint y candidato, corrige copia, mide y restaura asignación original. |
| `apply` | Verifica propuesta, crea checkpoint y otro candidato, mide y lo activa si pasa. |
| `verify` | Revisa firmas/contexto y vuelve a medir el candidato más reciente. |
| `revert` | Crea checkpoint y vuelve al original; no borra candidato. |

`operation` referencia el archivo `proposal.json`; `correction_id` identifica
el ciclo, mientras `operation_id` cambia con cada llamada. Los parámetros
persistidos no pueden alterarse en preview/apply. Se conserva un fingerprint
SHA256 de la propuesta y snapshots fuente/candidato. El contexto registra
origen, escena, FPS, escala, rango, jerarquía, rest transforms y estado relevante
de constraints. Si cambia, se rechaza la operación stale y se debe analizar otra.

Eventos nuevos registran intención antes de mutar y resultado después. Si el
resultado no llega por timeout, no repetir la mutación: puede seguir ejecutándose.
Inspeccionar Blender, artefactos y checkpoint, y reportar estado no verificado.

## Elegibilidad de Action

El corrector exige Action activa, local y no override, influencia completa y
blend REPLACE. Rechaza NLA activo/tweak, drivers del objeto o datablock y curvas
muted/locked/con modifiers o sampled points. Para Actions layered exige un slot,
una layer y un strip. No hace flatten/bake implícito ni elimina información para
obtener esas condiciones. Los detectores de lectura tienen soporte más amplio.

Las copias conservan contenidos con `Action.copy` y fake-user para retenerlas.
Se comprueba la identidad del snapshot cubierto: keys/handles/interpolación,
canales/slots/layers, markers, flags y custom properties representadas. No se
afirma comparar todos los posibles datos de Blender; modifiers/sampled curves
se rechazan para corrección en lugar de verificar sus parámetros parcialmente.
Las comprobaciones
numéricas no reemplazan un backup de dependencias externas ni validan cualquier
rig con handlers complejos. Checkpoints no empaquetan texturas/linked libraries.

## Foot sliding

Se exige selección explícita de 1–8 controles e `confirm_contacts=true` tras
revisar contactos. Cada control debe ser nondeform, unlocked, sin constraints;
debe ser target de una única constraint IK de dos huesos con influencia completa.
Se verifica relación con pierna/hip y se rechazan controles que son ancestros
entre sí. No se traduce un deform bone FK por una heurística de nombres.

Los canales location objetivo deben ser LINEAR. Se preserva movimiento de
aproximación, se ancla XY al comienzo del apoyo y se aplica una salida suave con
`blend_frames` (1–30; default 3). Z y orientación del control se protegen. Se
transforman offsets world-space a los espacios locales adecuados; no se asume
que XYZ local coincide con el mundo. Contactos/blends solapados ambiguos se rechazan.

Después se mide sliding otra vez en los intervalos propuestos y se verifican trayectorias protegidas,
rotación, hip, cambios articulares, longitud de huesos y residual de alcance IK.
Se usan muestras cada medio frame para detectar distorsión entre keys. El resultado incluye
drift antes/después, mejora y cambios de keys/canales. Una reducción del drift no
basta si falla cualquiera de las protecciones: debe cumplir la tolerancia de
drift y, cuando el drift inicial supera 1e-6 m, reducirlo al menos 80%. Este
criterio conservador del corrector no es un estándar artístico. La verificación visual sigue
siendo necesaria para knee pops, clipping, silueta y toe roll.

No hay soporte automático general para FK, rigs desconocidos, controles
constrained, foot location Bézier, plataformas móviles, pendientes o semántica
completa de heel/toe. La respuesta debe requerir alternativa manual.

## Quaternion y F-Curves

Quaternion correction exige WXYZ completo con tiempos de keys alineados. Ajusta
signos coherentemente en valores y handles, sin convertir rotation modes ni
cambiar el significado de la orientación keyed. La evaluación densa compara
orientación interpolada; si altera el recorrido más allá de tolerancia falla.
Los quaternions cero/indefinidos se reportan; `q` y `-q` son la misma orientación,
pero su interpolación por componentes puede atravesar un valor degenerado.
No presentar muestras indefinidas como error angular cero verificado.

Cleanup propone primero y aplica una estrategia por operación. `redundant`
es conservadora por defecto. Ruido/smoothing/spikes/interpolación requieren
tolerancias explícitas y flags de intención pertinentes. Se mantienen extremos,
identidades de canales y límites de muestreo. Curvas quaternion se excluyen de
cleanup escalar. La verificación devuelve keys antes/después, max_error y
average_error: si rebasa tolerancia, falla y se restaura la asignación original.

La comparación densa es una comprobación numérica acotada, no una demostración
continua entre infinitos tiempos. Los thresholds usan unidades del canal;
un valor razonable para metros no sirve directamente para radians o escalas.

## Root motion y retime

Root preparation implementa `strategy="in_place"` para un root explícito,
no parentado, unconstrained, unlocked y con location LINEAR. Retira su trayectoria
horizontal mundial en una copia; preserva vertical y jerarquía. No crea roots,
renombra bones, convierte ejes ni prepara exportaciones Unreal. Los controles IK
en world-space pueden necesitar refinamiento separado después.

Retime acepta factor 0.05–20 o duración en segundos y rango de 2–1001 frames.
Exige keys en los extremos seleccionados de cada canal para preservar poses.
Admite LINEAR/BEZIER/CONSTANT, transforma tiempos/handles y relaciones de Action
markers; un marker que requeriría frame fraccionario se rechaza. FPS, scene range
y scene markers permanecen iguales. Verifica poses en fases equivalentes,
vuelve a medir contactos y advierte cambios de semántica temporal.

## Aceptación y recuperación

`numeric_verification="pass"` conserva `visual_verification="required"`.
Renderizar before/after con cámaras/timing equivalentes, mirar las imágenes y
registrar aceptación o fallos reales. No hay una tool que certifique belleza,
realismo o calidad AAA. Guardar un checkpoint final al aceptar.

Un error numérico conserva candidato y checkpoint. Revert no procede si fuente,
candidato, contexto o asignación cambiaron inesperadamente; inspeccionar y usar
recuperación por checkpoint en una nueva copia. No sobrescribir un maestro ni
eliminar Actions para ocultar un fallo. Ver [workflow](animation-workflow.md).
