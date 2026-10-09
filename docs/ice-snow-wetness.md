# Hielo, nieve y humedad — M9

Estas familias se implementan mediante perfiles y definición PBR controlada. No producen nieve geométrica, hielo volumétrico de simulación, partículas ni efectos ambientales.

`ice`/`solid_ice` proponen una respuesta con transmisión; `frosted_ice` aumenta roughness y reduce transmisión respecto a ese punto de partida. Esto permite una superficie congelada menos transparente, sin afirmar que todo hielo deba verse como vidrio. El espesor, IOR, roughness, detalle y renderer siguen siendo condiciones de la evaluación.

`snow`/`compacted_snow` proponen superficies dieléctricas rugosas con SSS acotado. No se fija roughness cero y no se promete una microestructura de cristales físicamente simulada. La geometría y las texturas existentes determinan cuánto detalle puede verse.

Ejemplo de propuesta:

```json
{
  "object_name": "M9_ice_transmission",
  "material_name": "M9_ice_transmission_Material",
  "profile": "frosted_ice",
  "definition": {"renderer": "EEVEE", "parameters": {"roughness": 0.45, "transmission_weight": 0.35}},
  "apply": false
}
```

Los valores ilustran la configuración inicial declarada; no son constantes físicas universales. El plan congelado y su `data.material` se reutilizan en construcción y QA. Un cambio después de revisar requiere otro ciclo de refinamiento, con máximo tres.

Para humedad hay `wet_surface`, `wet_fabric`, `wet_skin`, `wet_metal` y `wet_creature`. Expresan coat y roughness dentro de la familia, conservando la diferencia entre conductor y dieléctrico. No generan gotas ni mezclas espaciales de zonas secas/mojadas; esas variaciones necesitan mapas de autoría explícitos.

La comparación exige iluminación neutral, suave, direccional y rasante equivalente entre fuente y candidata. Revisar amplitud y estructura de highlights, lectura del relieve, transparencia, SSS, seams y artefactos. EEVEE se declara como tal con sus aproximaciones; Cycles y el dispositivo realmente usado se informan sin afirmar GPU cuando hubo CPU. No extrapolar un render técnico de una esfera a un personaje de producción.

El demo de hielo/nieve/humedad se califica por sus artefactos y revisión real en el reporte M9. La existencia de estos perfiles no prueba todavía su aceptación artística ni habilita M10.
