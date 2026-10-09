# Contrato PBR — M9

El constructor admite un modelo `METALLIC_ROUGHNESS` basado en Principled BSDF. Los sockets se verifican contra Blender instalado; la API consultada para M9 es Blender 5.2.1 LTS. El soporte de construcción no equivale a realismo visual ni a material listo para otro motor.

El ciclo es analizar → planificar → candidata → construir → validar → renderizar con iluminación controlada → revisión real → hasta tres refinamientos → aceptar/rechazar. Se utilizan `production.material_analyze_pbr`, `material_validate_pbr`, `material_create_pbr`, `material_build_shader`, `material_quality_report` y el ciclo material. `apply=false` es el valor predeterminado. La referencia `data.material` identifica el plan inmutable; nunca editar su JSON para sortear `STALE`.

Definición estructurada real para las imágenes de la fixture:

```json
{
  "schema": "production.material_definition.v1",
  "category": "METAL",
  "workflow": "METALLIC_ROUGHNESS",
  "renderer": "EEVEE",
  "parameters": {"metallic": 0.85, "roughness": 0.3},
  "channels": {
    "base_color": {"image": "fixture_base_color.png", "component": "COLOR", "colorspace": "sRGB"},
    "roughness": {"image": "fixture_roughness.png", "component": "R", "colorspace": "Non-Color"}
  },
  "normal_convention": "UNKNOWN",
  "height_mode": "NONE"
}
```

Comprobar los nombres devueltos por `texture_inspect` antes de reutilizar un ejemplo: Blender puede añadir sufijos si ya existe un datablock. El constructor referencia imágenes ya cargadas; no descarga ni sobrescribe archivos. `normal_convention=UNKNOWN` es válido cuando no hay normal map, pero no permite construir una normal cuya convención no se haya declarado.

Los campos superiores son `schema`, `category`, `workflow`, `renderer`, `parameters`, `channels`, `normal_convention`, `height_mode` y `profile`. La normalización completa defaults explícitos y es estable al repetirse. Rechaza campos desconocidos, booleanos usados como números y valores no finitos. Los perfiles son configuraciones iniciales; no todos requieren texturas.

| Canal | Conexión y semántica |
| --- | --- |
| `base_color` | Color hacia Base Color; `COLOR` |
| `metallic` | Dato hacia Metallic; admite fracción declarada |
| `roughness` | Dato hacia Roughness; determina respuesta del shader |
| `normal` | RGB de datos → Normal Map tangente → Normal |
| `ao` | Dato independiente identificado; no se multiplica automáticamente en Base Color |
| `height` | Bump o Displacement explícito; no son equivalentes |
| `opacity` | Dato hacia Alpha |
| `emission` | Color hacia Emission Color; Strength separado |

Cada binding contiene `image`, `component` y `colorspace`. Componentes admitidos: `COLOR`, `R`, `G`, `B`, `A`. Base Color, Normal y Emission requieren `COLOR`; los canales escalares pueden seleccionar un componente. `COLOR` escalar utiliza conversión explícita de luminancia del builder. AO nunca se interpreta como prueba de iluminación físicamente correcta.

Los rangos siguientes son límites operativos del contrato, no valores universales medidos: pesos, alpha, roughness, metallic, coat, sheen y `height_midlevel` 0–1; IOR 1–4; `normal_strength` 0–10; `subsurface_scale` y `height_distance` 0–1 unidades Blender; `emission_strength` 0–10.000. Base Color/Coat Tint son RGBA 0–1; Emission RGB permite 0–10.000 y alpha 0–1; `subsurface_radius` es un vector de tres componentes 0–100. La API instalada y el renderer pueden imponer restricciones adicionales.

Se conservan material, node tree, slots, UV, geometría, shape keys, pesos, Actions y física originales. La candidata usa copias propias de objetos/materiales/imágenes. Cambiar color space en una imagen compartida cambiaría otros materiales: el builder configura únicamente la copia propia. La aceptación registra una candidata revisada, sin reemplazar silenciosamente las asignaciones originales.
