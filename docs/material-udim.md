# Materiales, UV y UDIM — M9

`production.material_validate_uv`, `material_validate_udim` y `material_analyze_texture_sets` reutilizan la lectura y matemática UV de M5. No unwrap, pack, remapean ni corrigen coordenadas. El límite del lector material es 200.000 loops; un cálculo que excede su presupuesto se informa como tal.

Se inspeccionan la capa activa o explícita, slots y polígonos asignados, tiles usados por cada material, imágenes TILED, identificadores reales y presencia/precisión/resolución de los archivos por tile. Se admite una imagen/material para muchos UDIMs, múltiples materiales y single-tile. No se exige un material por tile, ni exactamente cuatro tiles.

Ejemplo para la fixture de cinco tiles y un solo material:

```json
{
  "object_name": "M9_single_material_multi_udim",
  "material_name": "M9_single_material_multi_udim_Material",
  "options": {"uv": {"uv_layer": "UVMap", "resolution": 1024}}
}
```

La fixture usa 1001, 1002, 1011, 1012 y 1021. El lector compara los tiles de los polígonos asignados con los de cada imagen utilizada. `UDIM_TILE_MISSING` indica que falta un identificador requerido; `TEXTURE_FILE_MISSING` indica un archivo ausente incluso cuando el identificador está presente. `<UDIM>` y `<UVTILE>` se resuelven para leer cada dependencia, sin crear ni guardar tiles.

Resoluciones diferentes se informan como `UDIM_RESOLUTION_MISMATCH`, un WARNING que conserva la posibilidad de una elección intencional. La precisión se informa por archivo cuando puede comprobarse; `image.size` no se extrapola a todos los tiles. IDs fuera del intervalo operativo 1001–2000, duplicados y dependencias inesperadas se reportan. Imagen no TILED con UV fuera de 1001 genera aviso porque repetir una textura puede ser intencional.

Los umbrales UV y opciones —resolución, padding, stretch, overlap y capa— conservan las validaciones existentes de M5. Los hallazgos como `UV_PADDING_NOT_CERTIFIED` o stretch excesivo se muestran como WARNING con prefijo M5 y no se arreglan desde materiales. UV no finitas, área cero, caras que cruzan tiles/salen del dominio e identificadores distintos a los solicitados producen ERROR. Un análisis agotado produce UNKNOWN bloqueante: no se certifica cobertura porque no se pudo medir. Nunca se omiten los tiles interiores de una cara cruzada para declarar válida su dependencia. `uv_valid` conserva el resultado del subsistema. Un material visible puede seguir teniendo un asset `NOT_READY` para bake.

El resultado material expresa `bake_readiness=UNKNOWN`: este análisis no reemplaza `bake_validate_mesh`. Si se proporciona un bake pair M5 en el handoff, se verifica su integridad/frescura y se conserva su readiness exacto, incluido NOT_READY. No se transforma un fallo de distorsión/padding en READY porque un shader haya podido renderizar.

La validación de normales sólo muestrea el buffer activo sin cambiar tile: no certifica todos los pixels UDIM. Un futuro pipeline debe verificar seams, cobertura de texels, padding y tangentes con los mapas definitivos. El handoff permanece neutral para un futuro Substance Production MCP; M9 no abre Painter ni automatiza sus bakes.
