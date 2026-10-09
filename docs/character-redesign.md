# Rediseño de personaje (M4)

ANALYZE → PROPOSE → gate de usuario/seguridad → CANDIDATE → MODIFY → MEASURE → VISUAL QA →
ACCEPT o REJECT. El flujo de los assets es siempre MASTER → WORKING COPY → CANDIDATE MESH →
operación M4 → verificación → aceptar o rechazar. **Un MASTER nunca se modifica.**

## Plan: `character_redesign_plan`

Traduce una intención en texto a **operaciones propuestas** que conservan la topología. No muta nada:
`apply=false` y `mutated=false` siempre. Usa reglas de palabras clave, sin modelo semántico:

| Palabras (antes de la cláusula "preservar") | Paso propuesto |
|---|---|
| mass, bulk, heavier, upper body, stronger… | `directional` +Y en UPPER_BACK (2 % de la altura); `scale` X 1,04 en SHOULDERS |
| transition, smoother, blend… | `smooth` (fuerza 0,5, 4 iteraciones) en SHOULDERS / UPPER_BACK / LEFT_SHOULDER_TORSO |
| intimidating, menacing, imposing… | `scale` X 1,05 en SHOULDERS; `translate` +Z 0,6 % en SHOULDERS/UPPER_BACK |
| slimmer, leaner… | `inflate` negativo en TORSO/CHEST |
| torso | `smooth` en LEFT_SHOULDER_TORSO / TORSO |

Lo que aparece después de "preserving/keep/protect/lock…" se lee como regiones a conservar
(`preserve_requested`); si todavía no están bloqueadas se avisa. Para cada paso se elige la primera
región **editable** que nombre la regla. El plan evalúa en Blender el campo de desplazamiento
(`PLAN_ANALYZE`) y devuelve: regiones, tipo de operación, desplazamiento estimado, vértices afectados,
distancia a la región bloqueada más cercana, riesgo y motivos, dependencias y justificación visual.
**Los números son propuestas de partida, no verdad artística.**

"Ensanchar hombros" era un `inflate` y pasó a ser un `scale` X. En los renders de cerca del fixture, el
inflate de una región que contiene un tramo de brazo lo hinchaba en bultos esféricos que terminaban en
el borde de la región. La escala desplaza la masa hacia fuera sin inflar el brazo.

## Candidato: `character_create_candidate`

Reutiliza la malla candidata de M3.1: una copia de objeto y malla que no se renderiza, con checkpoint,
guard de asset, sufijo determinista e idempotencia por contenido. Si ya hay un candidato para el mismo
estado de la fuente, se reutiliza en vez de duplicarse. Añade un `dependency_manifest` antes/después
con la preservación por dependencia y lo que no se puede verificar. En M4 un MASTER se rechaza
**antes** de todo, incluso cuando solo se reutilizaría un candidato existente.

## Edición: `character_adjust_shape`

Modos `analyze`, `propose`, `preview`, `apply`, `verify` y `revert`. Solo se editan mallas
candidatas (`CANDIDATE_REQUIRED`).

- **Operación explícita:** `type` (translate, directional, grab, inflate, scale, smooth, relax, crease),
  `region`, `strength` (metros ±0,5; factor de escala 0,5..2; 0..1 para smooth/relax/crease),
  `direction`, `space` (object/world), `falloff` {smooth|linear|constant, width_m}, `preserve_boundary`,
  e `iterations`, `pivot`, `factors`, `center` y `radius_m` cuando aplican. Un campo desconocido se
  rechaza.
- **Campo determinista:** peso de región × atenuación geodésica desde el borde (Dijkstra por aristas
  hasta `width_m`). Los vértices bloqueados tienen peso 0 por construcción. El campo se recalcula en
  Blender a partir de los parámetros congelados, y su digest debe coincidir con el de la propuesta
  (`STALE_PROPOSAL`).
- **Guardia anti-volteo:** si el movimiento de un vértice invierte una cara incidente, su
  desplazamiento se reduce a la mitad por ronda (y a cero en la novena). Se informa como
  `flip_guard_attenuated_vertices` y en los avisos de la propuesta. Medido en la criatura: sin guardia,
  un vértice de borde de peso 0,251 junto a una cara astilla de 4,9e-5 m² se movía 2,2 cm y la volteaba
  174°.
- **Versionado de la malla:** la vista previa crea un objeto aparte `<obj>__M4P_<sufijo>`. Apply crea
  el datablock `<malla>__M4E_<sufijo>` y lo asigna **solo si pasa la verificación**; el anterior se
  conserva con fake user. Revert reasigna el anterior y conserva el editado.
- **Orden de una mutación:** propuesta inmutable → ledger (repetición o reconciliación) → preflight
  de frescura → rol del asset → checkpoint → ejecución → verificación numérica → verificación de
  regiones.

### Verificación numérica (falla si algo no se cumple)

Topología, pesos, UV, materiales, estado de normales (datos custom almacenados y sharp), atributos,
offsets de shape keys (≤ 1e-6), desplazamiento bloqueado ≤ tolerancia y **ninguna cara girada ≥ 90°**
(`FACE_FLIPPED`). Además mide desplazamiento máximo, vértices movidos, desplazamiento en borde y fuera
de la región, gradiente máximo de desplazamiento por arista y cambio máximo de normal de cara. Con
`numeric_verification=pass`, `visual_verification` sigue siendo `required`.

## Sesión: `character_redesign_session`

Por defecto, `apply=false`: inspección, auditoría, plan y riesgo, sin mutar nada. Con `apply=true` y
`step=N` ejecuta **un único ciclo**: guard de asset primero, candidato, propuesta y preview del paso N,
verificación numérica, QA de deformación (con `rig` y `stress_poses` explícitos) y renders antes/después.
Los pasos de riesgo alto se rechazan (`PLAN_STEP_HIGH_RISK`). El apply real sigue siendo una llamada
explícita a `character_adjust_shape(mode="apply")`.

## Aceptar o rechazar

Aceptar = conservar el candidato aplicado. La fuente nunca se sustituye automáticamente; volcar el
candidato sobre la fuente de la copia de trabajo es una decisión manual del usuario (ver
[validación pública y límites](validation.md)). Rechazar = `revert`: el candidato vuelve a su malla anterior y la
editada se conserva como evidencia.
