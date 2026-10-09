# Regiones y bloqueo (M4)

Las regiones son el contrato entre el análisis y la edición: una edición solo puede tocar una región
**editable** y nunca mueve una región **bloqueada**. Se reutiliza el manifiesto de M3.1
(`production.mesh_regions.v1`, `mesh/service.py`); no hay un sistema paralelo.

## Definiciones

| Método | Campos | Uso |
|---|---|---|
| `vertex_group` | name, min_weight | grupo explícito (recomendado para regiones del usuario) |
| `dominant_group` | groups, among | vértices cuyo peso más fuerte entre `among` pertenece a `groups` (candidatos automáticos por huesos). Un empate cuenta para todos los grupos empatados, así el resultado no depende del orden de los grupos. |
| `bounds` | min, max | caja en espacio de objeto |
| `face_set` | ids | face sets de sculpt |
| `mask` | min_mask | máscara de sculpt |
| `indices` | indices, topology_fingerprint | índices explícitos ligados a una huella de topología |

Las definiciones son compactas y se recalculan dentro de Blender; nunca viajan listas de vértices
salvo en el método `indices`.

## Origen, confianza y ambigüedad

`character_inspect` y `character_define_regions` proponen **candidatos automáticos**
(`source=automatic`):

- grupos de deformación con nombre de hueso reconocible: HEAD, FACE, NECK, CHEST, BACK, TORSO, PELVIS
  y LEFT_/RIGHT_ SHOULDER, ARM, HAND, LEG, FOOT. Confianza 0,75. Baja a 0,3 si el centroide
  contradice el lado del nombre (`SIDE_CONFLICT`).
- grupos de vértices explícitos que no deforman (p. ej. FACE, SHOULDERS, MUTATED_ARM): confianza 0,9.
  Ante ids repetidos gana el de mayor confianza.
- si no hay grupos: bandas de altura HEAD/TORSO/LEGS con confianza 0,3.

Cada candidato trae `evidence` y `overlap_fraction`. Una región con confianza < 0,6 es `ambiguous`.
Una región del usuario (`source=user`, confianza 1,0) **sustituye** al candidato con el mismo id. Las
regiones de criatura, extremidades de más o sobredimensionadas y regiones personalizadas se definen
como regiones del usuario con cualquier nombre.

## Roles

| Rol | Significado |
|---|---|
| `locked` | protegida: cualquier cambio fuera de tolerancia falla |
| `editable` | se puede editar; sus cambios se informan, no fallan |
| `unassigned` | registrada y capturada, pero no editable; sus cambios se informan |

`character_lock_regions` crea un manifiesto **nuevo**, recapturado en el momento del bloqueo. Una
región automática ambigua o con confianza < 0,7 no puede pasar a editable (`REGION_AMBIGUOUS`): hay
que redefinirla como región del usuario.

## Qué se captura y se verifica

Para cada región: índices y su digest, posiciones, cada shape key, digest de pesos de deformación y,
desde M4, las caras totalmente dentro de la región (digest y recuento), su asignación de material y
sus UV por capa. `character_validate_locked_regions` recaptura y compara:

| Código | Causa |
|---|---|
| `TOPOLOGY_CHANGED` | la topología ya no permite casar índices; los índices guardados **nunca** se reutilizan |
| `REGION_MEMBERSHIP_CHANGED` | la definición selecciona otros vértices |
| `LOCKED_REGION_MODIFIED` | desplazamiento de mundo > tolerancia (por defecto 1e-5 m) |
| `LOCKED_SHAPE_KEY_MODIFIED`, `NEW_SHAPE_KEY_MOVES_LOCKED_REGION`, `SHAPE_KEY_MISSING` | shape keys |
| `LOCKED_WEIGHTS_CHANGED` | pesos |
| `REGION_FACE_MEMBERSHIP_CHANGED`, `LOCKED_MATERIAL_CHANGED`, `LOCKED_UV_CHANGED` | caras, materiales, UV |

Medido en Blender real (`tests/blender/m4_character_probe.py`): un movimiento manual de 0,5 mm de un
vértice bloqueado, un cambio de UV, uno de material y uno de peso en caras o vértices bloqueados se
detectan. Una subdivisión de una arista invalida un manifiesto de índices (`TOPOLOGY_CHANGED`) tanto
en la validación como en la propuesta.

## Máscaras, face sets y huellas

Máscaras y face sets son definiciones de región como cualquier otra; se leen, nunca se crean ni se
modifican. Una región por índices solo es válida con la huella de topología con la que se definió. Por
eso `character_adjust_shape` rechaza un manifiesto definido sobre otra topología.
