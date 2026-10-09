# Expresiones, asimetría y microexpresiones (M7)

Código: `facial/model.py`, `facial/layers.py`, `facial/micro.py`. Tools: `facial_create_brief`,
`facial_plan_expression`, `facial_generate_expression`, `facial_validate_expression`, `facial_analyze_asymmetry`,
`facial_plan_microexpressions`, `facial_generate_microexpressions`.

## Brief con procedencia

`create_brief` acepta texto y/o estructura y marca cada campo como `USER_REQUIREMENT`, `INFERRED_DEFAULT` o
`PROPOSAL`. Emociones con intensidad (palabras low/medium/high o 0–1), estilo `restrained/neutral/expressive`, mirada,
habla (si se pide habla sin timing → `TIMING_INPUT_REQUIRED`), tensión de mandíbula (propuesta para ira/dolor),
duración 0,5–15 s, FPS {24, 25, 30, 48, 50, 60}, asimetrías declaradas y microexpresiones.

"Ocultar" una emoción se expresa **una vez**, por el estilo contenido (×0,55). Antes también se escalaba la emoción
(×0,6) y la ira quedaba al 21 % de la librería, invisible: corregido y cubierto por un test.

## Modelo de expresión

No es un modelo psicológico: una palabra elige un perfil de partida de 15 **dimensiones de control**
(`brow_compress`, `lid_open`, `squint`, `lip_compress`, `jaw_tension`...), editable con `options.dimensions`. Librería:
NEUTRAL, ANGER, FEAR, SADNESS, JOY, DISGUST, PAIN, FATIGUE, SUSPICION (+SURPRISE). Todos los valores son propuestas
para pruebas controladas. Las dimensiones se convierten en objetivos semánticos y solo se usan las salidas que el rig
tiene; las demás se informan (`EXPRESSION_OUTPUT_UNSUPPORTED`).

Temporal: entrada con smoothstep (0,4 s por defecto), relajación del 8 % en 0,8 s para no congelar el pico
(propuesta) y salida opcional.

## Validación

`facial_validate_expression` resuelve la expresión al pico en controles reales y la **mide en Blender**: penetración del
párpado, cruce de labios, intersecciones nuevas, caras colapsadas o invertidas, clamps del rango del control.

## Asimetría

- **Del rig** (`facial_analyze_asymmetry`): desplazamiento máximo medido de cada par L/R y radios de los ojos. Fixture
  de límites asimétricos → `RIG_CONTROL_ASYMMETRY`; fixture intencional → `EYE_SIZE_ASYMMETRY` 0,86. Se informa y se
  conserva, nunca se corrige.
- **De la actuación**: solo intencional, con área, lado, intensidad (0–0,6], propósito y tiempos. Nunca aleatoria.

## Microexpresiones

Pequeñas, breves y contextuales: 0,2–0,5 s, amplitud ≤ 0,25 del rango, separación ≥ 1,5 s, ≤ 0,5 por segundo (la
duración <0,5 s sigue la literatura de Ekman; el resto son propuestas). Catálogo por contexto (ira: compresión de
labios, apretar mandíbula, fruncir...), con semilla y evitando cierres de habla y parpadeos.

## Medido

Demo A ("angry but restrained", 4 s/30 FPS): QA técnica sin errores. Visualmente se lee sobre todo en la boca (rendija
más fina) y los párpados inferiores. La bajada de cejas al 27 % y la asimetría de ceja de 0,15 **no se leen** en esta
fixture sin arco superciliar (revisión visual registrada).
