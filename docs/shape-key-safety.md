# Seguridad de shape keys (M4)

Editar la malla base de un personaje con shape keys puede romper las expresiones si solo cambia el
Basis. M4 no lo hace nunca en silencio.

## Qué se informa

`character_inspect` y cada propuesta de `character_adjust_shape` informan:
- si hay shape keys, si son relativas o absolutas, y cuál es el Basis;
- por cada clave, cuántos vértices mueve, cuántos de ellos se van a editar y si tiene driver;
- cuántos vértices de la malla no coinciden con el Basis (`basis_mesh_mismatch_vertices`).

## Políticas (`options.shape_keys`)

| Política | Comportamiento |
|---|---|
| `require_unaffected` (por defecto) | Si alguna clave mueve un vértice que se va a editar: `SHAPE_KEY_CONFLICT`, propuesta no soportada |
| `propagate_delta` | La misma delta se suma a la malla y a **cada** clave, de modo que el offset de cada clave respecto a su clave relativa no cambia |

Siempre se rechaza:
- claves absolutas (`use_relative=False`): `SHAPE_KEYS_ABSOLUTE_UNSUPPORTED`;
- malla distinta del Basis: `SHAPE_KEY_BASIS_MISMATCH`, porque el estado no es fiable para propagar.

## Verificación

Tras la edición se comparan los offsets `clave − clave relativa` de todas las claves antes y después
(error máximo ≤ 1e-6 m) y el conjunto de nombres. Además, el Basis debe seguir igual a la malla.

Medido en Blender real (fixture `humanoid_keys`, claves Smile en la cara y Breath en el pecho):
- editar FACE con la política por defecto se rechaza (`SHAPE_KEY_CONFLICT`: Smile mueve 8 de los
  vértices editados);
- con `propagate_delta`, el apply pasa con error de offset 0,0 m y Basis igual a la malla;
- UPPER_BACK, que ninguna clave mueve, se acepta con la política por defecto;
- las claves absolutas y un Basis desincronizado de 2 mm se rechazan.

## Límites

- Una clave cuya relativa no es el Basis se trata igual: se conserva su offset respecto a su propia
  relativa.
- Los drivers de las claves solo se informan; no se evalúan.
- Una propagación que conserva los offsets no garantiza que la expresión se vea igual de bien sobre
  una forma nueva: la revisión visual de las expresiones sigue siendo necesaria.
