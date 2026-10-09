# Idempotencia de operaciones (AUD-010)

Código: `src/blender_mcp/production/common/ledger.py`, envoltorio `tool()` de `server.py`,
`transaction()` (M3) y `correction()` (M2). Pruebas: `tests/unit/test_m31_idempotency.py`, más
`tests/blender/m31_transaction_probe.py` en Blender real.

## El problema

Si el cliente pierde la respuesta (timeout del socket, conexión cortada), no sabe si Blender ejecutó
el comando: Blender procesa los comandos de uno en uno y puede terminar después. En v0.3.0, reintentar
volvía a ejecutar y podía duplicar checkpoints, candidatos o previews. No había forma de preguntar qué
había pasado.

## Ledger

Cada operación mutante tiene un registro JSON en `<workdir>/ledger/`, escrito de forma atómica:

| Estado | Significado |
|---|---|
| NEW | sin registro: nunca empezó, o se abandonó antes de llegar a Blender |
| RUNNING | en curso en este proceso; una segunda petición con la misma clave recibe `OPERATION_IN_PROGRESS` |
| SUCCEEDED | terminada; un reintento devuelve el resultado guardado sin llamar a Blender |
| FAILED | terminada con un fallo conocido; un reintento devuelve ese fallo |
| UNCERTAIN | se envió la mutación y no llegó respuesta, o murió el servidor que la inició |
| REVERTED | un apply que se revirtió después; se permite un nuevo intento de apply |

Hay dos fuentes de clave:

1. **Clave del cliente.** Toda herramienta que no es de lectura acepta `operation_id` (8–128
   caracteres `[A-Za-z0-9._:-]`). La misma clave con otros argumentos devuelve
   `IDEMPOTENCY_KEY_CONFLICT`.
2. **Clave derivada del ciclo de propuesta.** Aunque el cliente no mande clave, cada propuesta
   inmutable tiene `<correction_id>:preview`, `<correction_id>:apply` y
   `<correction_id>:revert:<intento de apply>`. Repetir apply sobre la misma propuesta no crea otro
   candidato ni otro checkpoint.

El sobre de respuesta lleva `operation_id` (la clave del cliente, o el id generado), `execution_id`
(id interno de esta ejecución) e `idempotency` (`key`, `state`, `attempt`, `replayed` y, en su caso,
`reconciled`).

## Reconciliación tras un resultado incierto

El sufijo del candidato es determinista: se deriva de la clave y del número de intento. Al reintentar
un preview o apply en UNCERTAIN, el servidor no vuelve a ejecutar. Primero envía una consulta de solo
lectura (`RECONCILE`). Como Blender procesa los comandos del socket en orden (una cola FIFO en el hilo
principal del addon upstream), cuando esa consulta responde, el comando anterior ya terminó.

- **Existe el candidato:** Blender sí ejecutó. Se vuelve a verificar ese candidato con CHECK, se
  registra como evento de la propuesta y el estado pasa a SUCCEEDED. Si el candidato no quedó activo,
  pasa a FAILED con `APPLY_NOT_ACTIVATED`: el apply solo activa tras su propia verificación.
- **No existe:** la petición nunca llegó a mutar. El intento se abandona y se ejecuta una sola vez.
- **Ambiguo** (varios candidatos con ese nombre, objeto ausente): `OPERATION_OUTCOME_UNCERTAIN`. No
  se ejecuta y se indica el checkpoint de recuperación.

En un revert, la reconciliación mira qué Action está activa. Con una clave de cliente sobre una
herramienta sin reconciliación (checkpoint, restore, render, duplicar Action), UNCERTAIN no se vuelve
a ejecutar nunca: se responde `OPERATION_OUTCOME_UNCERTAIN`, y el usuario inspecciona y usa otra
clave. `production.mesh_candidate_create` es idempotente por contenido: si ya hay un candidato para
ese mismo estado de la fuente, lo reutiliza.

## Consulta: "¿qué pasó con la operación X?"

`production.operation_status(operation_id=...)` o `production.operation_status(correction_id=...)`
devuelve el estado, el checkpoint, el nombre de candidato esperado, la reconciliación y el historial.
Nunca llama a Blender. Como las herramientas se ejecutan en un hilo, el bucle de eventos queda libre
para responder a esta consulta mientras otra herramienta espera a Blender.

## Evidencia medida

| Escenario (Blender real, `m31_transaction_probe.py`) | Resultado |
|---|---|
| Blender ejecuta el apply y la respuesta se pierde | estado UNCERTAIN, 1 candidato |
| Reintento con la misma propuesta | `reconciled=executed`, SUCCEEDED, 0 ejecuciones nuevas de EXECUTE, sigue 1 candidato |
| Tercer apply | `replayed=true`, 0 llamadas a Blender |
| La petición se pierde antes de ejecutarse | UNCERTAIN, 0 candidatos; el reintento ejecuta una vez y deja 1 candidato |
| Verify del candidato reconciliado | `pass` |

## Límites

- La reconciliación depende del orden FIFO del addon upstream y de que el reintento vaya a la misma
  instancia de Blender.
- Un servidor que muere deja RUNNING; al leerlo desde otro proceso se trata como UNCERTAIN (se compara
  el PID).
- Las herramientas de análisis no tienen estado: no aceptan `operation_id` y se pueden repetir sin
  riesgo.
