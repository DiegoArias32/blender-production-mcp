# SSS, transmisión, alpha y emisión — M9

Estas operaciones trabajan sobre un plan/candidata: `production.material_configure_sss`, `material_configure_transmission`, `material_configure_opacity` y `material_configure_emission`. Reciben cambios explícitos en `options.parameters`; con `apply=false` muestran propuesta. Una aplicación crea otra versión propia cuando corresponde y conserva el material fuente. Después de render/review, usar el refinamiento con límite de tres ciclos.

Ejemplo para `material_configure_sss`, sustituyendo `material` por la referencia exacta recibida del plan:

```json
{
  "material": "<data.material de la propuesta vigente>",
  "options": {"parameters": {
    "subsurface_weight": 0.2,
    "subsurface_radius": [1.0, 0.2, 0.1],
    "subsurface_scale": 0.002,
    "subsurface_method": "RANDOM_WALK_SKIN"
  }},
  "apply": false
}
```

La etiqueta entre ángulos es una referencia a reemplazar, no un archivo existente. Los valores numéricos del ejemplo son propuestas de prueba, no parámetros universales de piel.

SSS utiliza `subsurface_weight`, `subsurface_radius`, `subsurface_scale`, `subsurface_ior`, `subsurface_anisotropy`, `subsurface_method` y Base Color. En la API instalada existen BURLEY, RANDOM_WALK, RANDOM_WALK_SKIN y RANDOM_WALK_LEGACY. Que un método exista no significa que todos los sockets estén activos bajo él: se verifican en Blender. Por ejemplo, Subsurface IOR puede estar presente en la enumeración pero indisponible bajo RANDOM_WALK. El contrato predeterminado declara RANDOM_WALK_SKIN y el builder selecciona el método antes de comprobar sockets. No se simula soporte de un socket indisponible.

Scale y Radius necesitan interpretación conjunta con la escala y el espesor del objeto. El informe separa esta suposición de la observación visual. Un SSS construido no demuestra piel natural ni que la luz penetre una distancia físicamente calibrada en un personaje real.

Transmisión acepta `transmission_weight`, `ior` y `roughness`. Se informa la dependencia del espesor y del renderer. En EEVEE la respuesta de reflexión/refracción es aproximada y requiere QA con iluminación controlada; no equivale a un certificado de vidrio/hielo físico. Un IOR aceptado por el schema, cuyo rango operativo es 1–4, no prueba su adecuación a la superficie concreta.

Opacity configura `alpha` y, si corresponde, un canal `opacity`. Blender 5.2.1 expone `surface_render_method` con DITHERED/BLENDED; el constructor usa DITHERED y no asume el antiguo `blend_method` como contrato vigente. Se inspeccionan sombras y render method y se advierte sobre ordenación, superposición y ruido alpha. Alpha/transmisión tienen efectos diferentes y no se intercambian.

Emission configura `emission_color` y `emission_strength`. Su intensidad es un parámetro del shader, no una conversión universal a lúmenes. Emission necesita gestión de color apropiada y revisión con exposición controlada; incrementar Strength no valida contribución luminosa ni evita clipping.

Todas las familias deben pasar grafo, texturas, dependencias y QA. La ejecución de un render y una revisión real siguen siendo pasos separados. La aceptación final de demos pertenece al reporte M9; este documento no la anticipa.
