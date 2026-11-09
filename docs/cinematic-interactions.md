# M10 — Interacciones cinematográficas

Dos áreas comparten este documento: **props y fijación** (área D, `cinematic_manage_props`, este texto) y
**contactos y QA de interacción** (área F, sección reservada al final). Contrato general:
[arquitectura cinematográfica](cinematic-architecture.md) §5.

## Props (`cinematic_manage_props`, área D)

Código: `src/blender_mcp/production/cinematic/performance.py` (`pf_props`, `normalize_props`).

```
options = {props: [{prop_id (≤24, sin espacios), object (el original),
                    placement?: {location: [x, y, z], rotation_deg: [x, y, z]},
                    attach?: [{actor_id, bone ('' = el objeto), frame_start, frame_end}],
                    visibility?: [{frame, visible}]}]}
```

### Qué se crea y qué no se toca

- **El original se excluye al crear la candidata.** Las colecciones de la escena fuente se enlazan enteras en `_SRC`
  (contrato §1), así que un original que llegó por una colección enlazada ya no se puede desenlazar después. Todo prop
  que la cinemática vaya a mover o sostener va en `exclude_objects` de `cinematic_create_candidate`; si sigue en la
  escena candidata, `cinematic_manage_props` rechaza con `CINEMATIC_PROP_NOT_EXCLUDED` antes de mutar. Conviene que los
  props vivan en su propia colección: excluirlos espeja sólo esa colección.
- **Copia propia** `M10C_<g8>_v<n>_PROP_<prop_id>` con `obj.copy()`: comparte la malla del original (no se duplica ni
  se escribe), vive en `_CIN`, marcada M10. Sin padre, sin las constraints heredadas y con los deltas a cero, para que
  su base y sus keys signifiquen colocación en mundo. Su colocación inicial (sin `placement`) es la matriz mundo del
  original **evaluada en la escena fuente** en el frame actual de esa escena (sin `frame_set` en la fuente).
- El **original** no se desenlaza, no se re-emparenta ni se modifica: su huella (matriz, padre, constraints,
  modifiers, animación) se compara antes y después. La tool no escribe filas `touched`.
- Fijación: una constraint `CHILD_OF` propia por ventana (`M10_ATTACH_<i>`) en la copia, con la influencia animada por
  keys CONSTANT de una Action propia `M10C_<g8>_v<n>_PROPACT_<prop_id>` (un slot de objeto). Un traspaso mano a mano
  son dos constraints con ventanas contiguas (`B.inicio = A.fin + 1`).

### Matemática de la fijación (medida)

Medido en 5.2.1: `CHILD_OF` nace con `set_inverse_pending=True` y fijaría su propia inversa en la primera
evaluación; con `set_inverse_pending=False` el mundo es `target @ inverse @ basis` (error 0,0 con inversa identidad,
sonda de exploración). Por eso la inversa se calcula y se escribe explícitamente, con el objetivo
`T(f) = armature_mundo @ pose_bone.matrix` evaluado en la candidata:

- ventana tras una fase libre: `inv = T(inicio)⁻¹` → en `inicio` el prop queda donde estaba (sin salto);
- traspaso: `inv_B = T_B(h)⁻¹ @ T_A(h) @ inv_A` → en el frame `h` B lo deja donde A lo habría llevado;
- suelta (sin ventana contigua): se keyea la base en `fin + 1` con el mundo que tenía en `fin`
  (`T(fin) @ inv @ base`), así el prop se queda donde lo soltaron.

La influencia es `0` en `inicio − 1`, `1` en `inicio`, `0` en `fin + 1`, todo CONSTANT: nunca hay dos `CHILD_OF` a la
vez. `visibility` keyea `hide_render` y `hide_viewport` de la copia (CONSTANT).

### Rechazos

| Caso | Código |
|---|---|
| el objeto es un actor o una de sus mallas | `PROP_IS_ACTOR` |
| datablock propio de M10 | `PROP_IS_OWNED` |
| enlazado / override | `PROP_LINKED_UNSUPPORTED` |
| tiene hijos (la copia los dejaría atrás) | `PROP_HAS_CHILDREN` |
| animado y con `placement`, `attach` o `visibility` (necesitan una Action propia en la copia) | `PROP_ANIMATION_UNSUPPORTED` |
| ventanas solapadas (`B.inicio ≤ A.fin`) | `PROP_ATTACH_OVERLAP` |
| primera ventana no posterior al inicio de la línea de tiempo | `PROP_ATTACH_WINDOW_INVALID` |
| hueso inexistente | `PROP_ATTACH_BONE_MISSING` |
| el original sigue en la escena candidata (no se excluyó al crearla) | `CINEMATIC_PROP_NOT_EXCLUDED` |

Repetir un `prop_id` con el mismo original sustituye la copia y su Action propias.

### Validación medida en cada llamada

En **todos** los frames de cada ventana (de `inicio − 1` a `fin + 1`, hasta ~600): posición mundo evaluada del prop,
**deslizamiento mano-prop** (máximo desplazamiento del prop en el marco del hueso respecto al de `inicio`), deriva de
rotación en ese marco, salto de entrada (`|W(inicio) − W(inicio−1)|`, ventanas tras fase libre), salto de salida
(`|W(fin+1) − W(fin)|`, ventanas que sueltan) y continuidad del traspaso (diferencia entre el prop en `h` y donde lo
habría dejado el portador anterior).

### Hechos medidos (probe `tests/blender/m10_performance_probe.py`, 88/88)

Informe `informe local privado; véase validation.md` (cifras iguales en `-8`,
`-9` y `-10`). Taza (caja) en la colección `Props`, excluida al crear la candidata; cogida por la mano derecha de A en 20–40 y
traspasada a la de B en 41–60 (B sincronizado con offset y recolocado; ambas manos animadas):

| Medida | Ventana A (20–40) | Ventana B (41–60) |
|---|---|---|
| deslizamiento mano-prop | 3,0e−7 m | 7,6e−7 m |
| deriva de rotación | 3,5e−6° | 4,4e−6° |
| salto de entrada / salida | 1,2e−7 m / — | — / 3,4e−7 m |
| frames medidos | 23 | 22 |

Traspaso en el frame 41: error de continuidad **3,2e−7 m** y 3,4e−6°. La copia arranca donde está el original en la
escena fuente: distancia **1,2e−7 m** en el frame 20. La copia comparte la malla del original, el original no cambió,
sigue sin padre y nunca estuvo en la candidata; 0 filas `touched`. Rechazos sin cambiar candidata ni ledger:
`CINEMATIC_PROP_NOT_EXCLUDED` (la taza en una candidata exploratoria que no la excluía, y la `Mug` no excluida en la
candidata principal), `PROP_IS_ACTOR` y `PROP_ATTACH_OVERLAP`. Tras `cinematic_reject` la huella del original es
idéntica y no queda ningún datablock M10. Coste en la fixture: 0,21–0,29 s.

**Hallazgo medido (informativo, fuera del área D):** con la copia ya creada, un `frame_set(30)` en la candidata hecho
**fuera** de las tools (lo que hace el usuario al mover la línea de tiempo) vuelca la influencia keyeada de
`M10_ATTACH_0` en su valor crudo (0,0 → 1,0) y la huella de la candidata deja de coincidir con la registrada; al volver
a poner sólo esa influencia cruda, o al volver al frame 1, coincide otra vez. `cm_candidate_fingerprint` (runtime
compartido) lee la influencia cruda de las constraints; con el actual, dejar la candidata en otro frame desde la GUI
la volvería `CINEMATIC_STALE_CANDIDATE`. Las tools del área restauran ellas mismas esos valores (capturan `_CIN`).

### Propuestas y límites

- Tolerancias `prop_slide_m` y `prop_pop_m` = 1e−5 m, rotación 1e−3° (`threshold_source='PROPOSAL'`).
- No mide contacto real mano-prop (superficies, penetración): eso es del área F. Lo que se verifica es que el prop
  sigue rígidamente al hueso y no salta.
- Props animados sólo se copian tal cual (comparten la Action del original, que no se escribe).
- La colocación inicial se lee en el frame actual de la escena fuente: un original con padre animado arrancaría donde
  esté en ese frame, no en el inicio de la línea de tiempo (no ejercitado).

## Contactos y QA de interacción (área F)

<!-- Sección reservada para el área F (cinematic_plan_interaction, cinematic_analyze_contacts y la QA de
interacción). El área D no escribe aquí. -->

Pendiente del área F.
