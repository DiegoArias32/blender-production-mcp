# Perfiles de material — M9

`production.material_create_profile` devuelve una configuración inicial con categoría, shader, parámetros, rangos, canales obligatorios/opcionales, dependencias típicas, limitaciones y procedencia. No crea un material Blender. `material_validate_profile` valida el contrato; `material_apply_profile` propone o construye una versión candidata dentro del ciclo material.

```json
{
  "profile": "fabric",
  "definition": {"parameters": {"roughness": 0.65}}
}
```

El ejemplo adapta un punto de partida, no modifica la definición compartida del catálogo. Los defaults proceden de `materials/model.py`; los overrides llevan procedencia del llamador. La normalización conserva metallic fraccional cuando se solicita. La pertenencia a un perfil nunca añade un score de realismo.

| Perfil | Categoría | Intención y límite |
| --- | --- | --- |
| `human_skin` | SKIN | Base dieléctrica con SSS; sin autoría de poros o anatomía |
| `creature_skin` | ORGANIC_CREATURE_TISSUE | Piel orgánica no humana; no asumir fisiología humana |
| `fabric` | FABRIC | Roughness/sheen iniciales; no genera trama ni fibras |
| `leather` | LEATHER | Superficie dieléctrica; acabado por revisar |
| `metal` | METAL | Conductor inicial; mezcla/cobertura fraccional permitida |
| `painted_metal` | PAINTED_METAL | Pintura dieléctrica; desgaste necesita máscara de autoría |
| `rubber` | RUBBER | Superficie dieléctrica rugosa; no simula elasticidad |
| `bone` | BONE | Base clara con SSS acotado; no certifica hueso real |
| `ice` | ICE | Transmisión inicial; espesor y renderer explícitos |
| `snow` | SNOW | Roughness/SSS iniciales; no genera geometría |
| `wet_surface` | WET_SURFACE | Coat y roughness iniciales; humedad contextual |

Variantes limitadas al alcance requerido: `solid_ice`, `frosted_ice`, `compacted_snow`, `wet_fabric`, `wet_skin`, `wet_metal` y `wet_creature`. No constituyen un catálogo infinito ni autorizan ampliar M9 con presets opcionales.

Las categorías del contrato son SKIN, FABRIC, LEATHER, METAL, RUBBER, PLASTIC, BONE, ICE, SNOW, WET_SURFACE, PAINTED_METAL, ORGANIC_CREATURE_TISSUE, GENERIC_DIELECTRIC y CUSTOM. Categoría y perfil son distintos: CUSTOM permite una definición estructurada dentro de los mismos guards, no Python arbitrario.

Los perfiles iniciales no obligan a aportar todas las texturas. Una superficie uniforme puede expresarse con valores; detalles espaciales requieren imágenes verificadas. Ningún perfil fabrica texturas de piel, masks de desgaste ni mapas de escaneado a partir de nada. Los canales opcionales siguen [el contrato PBR](pbr-workflows.md), y cada dependencia declarada debe existir y tener interpretación adecuada.

Un `passed=true` de `material_validate_profile` certifica la coherencia del schema y de sus canales requeridos, no la apariencia del resultado. Hay que inspeccionar fuente, crear candidata, comprobar grafo/UV/texturas, renderizar comparables y registrar la revisión visual.
