# Normal, bump, height y tangentes — M9

`production.material_validate_normals`, `material_validate_height` y `material_validate_tangent_workflow` verifican contratos distintos. Un cable conectado no constituye por sí solo una normal válida.

La normal calificada por el constructor es tangente, RGB de datos y convención `OPENGL` declarada. La herramienta no deduce OpenGL/DirectX a partir del nombre, no inventa la orientación Y y no invierte automáticamente el canal verde. `DIRECTX` sin un adaptador de conversión explícito se rechaza. Object/world-space se inspeccionan, pero permanecen `UNKNOWN` para la certificación tangente M9.

Ejemplo sobre la fixture de normal correcta:

```json
{
  "object_name": "M9_normal_map",
  "material_name": "M9_normal_map_Material",
  "options": {"normal_convention": "OPENGL", "normal_length_tolerance": 0.2}
}
```

Se comprueban Image Texture, Normal Map, conexión real al shader, interpretación Non-Color/data, Strength finita 0–10, capa UV existente y convención. RGB directo hacia Principled Normal es un error. Normal Map desconectado se informa. En una cadena Normal Map → Bump → Principled, la normal y la altura se siguen independientemente: la textura de altura no se etiqueta como normal RGB.

El lector muestrea hasta 256 pixels distribuidos uniformemente por el buffer activo. Para normal tangente codificada comprueba finitud, intervalo RGB 0–1 y longitud del vector `2*RGB-1`. Se informa el error máximo de longitud, muestras inválidas, cobertura y procedencia del umbral. `normal_length_tolerance=0.2` es una propuesta inicial configurable en 0.01–0.5, no una tolerancia física universal. Pixels no finitos, fuera de rango o todas las muestras incompatibles producen ERROR; desvíos parciales generan WARNING porque padding fuera de las islas puede explicarlos.

No se cambia el tile activo. En UDIM esta medición no certifica todos los tiles, todos los pixels, los seams ni todos los texels realmente usados por la malla. Un muestreo correcto tampoco demuestra la convención Y. Sin pixels verificables se devuelve `NORMAL_PIXEL_DATA_UNVERIFIED`, que bloquea la certificación requerida. El detalle de muestras no se devuelve como dump por defecto.

La validación de tangentes se limita a prerrequisitos observados y relaciones UV. No llama `calc_tangents`, no crea una triangulación ni modifica la malla. Por tanto no certifica igualdad de tangentes Blender/Substance/Unreal ni continuidad de todos los bordes. El handoff conserva explícitamente estas suposiciones para una validación posterior.

`height_mode` separa:

| Modo | Alcance |
| --- | --- |
| `NONE` | Sin canal height |
| `BUMP` | Cambia la normal de sombreado; no la silueta ni geometría |
| `DISPLACEMENT` | Conexión geométrica explícita; exige Cycles en el builder |

`height_distance` y `subsurface_scale` se expresan en unidades Blender y se interpretan junto a `scene.unit_settings.scale_length`; no hay conversión silenciosa a metros. Height y height_mode deben concordar. Aunque Cycles admita una conexión Displacement, M9 devuelve `DISPLACEMENT_GEOMETRY_UNCERTIFIED` sin demostrar teselación/deformación: no se crea geometría para hacer pasar este gate. Esta limitación bloquea aceptar una capacidad geométrica no verificada.
