# Piel y tejido orgánico — M9

El builder de alto nivel utiliza `production.material_create_pbr` o `material_apply_profile` con `human_skin`/`creature_skin`, más una definición explícita de canales. Esto reutiliza el ciclo seguro común; no introduce una tool duplicada que sólo cambia un preset.

Ejemplo de propuesta para la fixture orgánica:

```json
{
  "object_name": "M9_skin_sss",
  "material_name": "M9_skin_sss_Material",
  "profile": "human_skin",
  "definition": {
    "renderer": "EEVEE",
    "normal_convention": "OPENGL",
    "channels": {
      "base_color": {"image": "fixture_base_color.png", "component": "COLOR", "colorspace": "sRGB"},
      "roughness": {"image": "fixture_roughness.png", "component": "R", "colorspace": "Non-Color"},
      "normal": {"image": "fixture_normal.png", "component": "COLOR", "colorspace": "Non-Color"}
    }
  },
  "apply": false
}
```

Los nombres corresponden a imágenes sintéticas ya cargadas por la fixture; verificarlos mediante inspección. La normalización añade parámetros del perfil como propuestas visibles. Un perfil de criatura conserva categoría propia y no presupone color, fisiología, proporciones ni piel humana.

Capacidades: color base, roughness espacial mediante imagen, microdetalle normal conectado, SSS declarado, height/bump opcional, coat para una propuesta de humedad y validación de dependencias. No se generan texturas detalladas ni se pinta el personaje. Una textura sintética sirve para demostrar conexiones y respuesta de luz, no para afirmar calidad de un asset final.

Con Normal y Height simultáneos se construye Normal Map → input Normal de Bump → Principled Normal, mientras Height alimenta Bump Height. Las dos señales se validan por separado. `normal_strength`, `bump_strength` y `height_distance` quedan explícitos; un detalle conectado con escala incorrecta sigue siendo un problema visual.

SSS necesita escala y espesor comprobables; los radios/Scale propuestos se interpretan en unidades Blender. Humedad no equivale universalmente a disminuir roughness: coat, capa superficial, texturas y referencias deben juzgarse en el contexto del material. Los perfiles `wet_skin` y `wet_creature` son puntos de partida acotados.

La QA debe comparar fuente y candidata con misma geometría, cámara, luz, exposición y renderer. Revisar highlights, variación de roughness, SSS, detalle normal, seams y legibilidad. El original, sus slots, UV, shape keys y animación permanecen intactos. El reporte de M9 registra si la demostración orgánica fue realmente revisada y aceptada; este documento sólo describe el flujo.
