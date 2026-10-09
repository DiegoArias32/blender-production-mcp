# Texturas y gestión de color — M9

`production.texture_inspect`, `texture_validate_channels`, `texture_validate_colorspace` y `texture_dependency_report` operan sobre imágenes existentes y sus dependencias locales. No cargan assets externos por iniciativa propia, no recargan imágenes fuente ni escriben archivos de textura.

La respuesta distingue el datablock Blender del archivo: nombre, source/type, ruta y resolución, formato informado, componentes, alpha, buffer flotante, `has_data`, `is_dirty`, color space, tiles, archivos presentes, bytes y SHA256. La ruta de Blender se resuelve con su contexto de biblioteca antes de comprobar el archivo en el host. Las rutas HTTP/UNC se registran como no verificadas; no se descargan. Un archivo que cambia durante la lectura se rechaza.

Ejemplo de validación dirigida:

```json
{
  "object_name": "M9_metallic",
  "material_name": "M9_metallic_Material",
  "options": {"limit": 10, "offset": 0}
}
```

El color space es interpretación de datos, no una etiqueta decorativa. Base Color suele usar sRGB; Emission requiere una interpretación de color apropiada a sus datos. Roughness, Metallic, AO, Normal, Height y Alpha se consideran datos. Un roughness sRGB se informa como error aunque el llamador lo haya declarado sRGB. Un binding explícito se contrasta además con el nombre real de color space, y el builder verifica su existencia en la API instalada.

No hay equivalencia automática entre PNG, EXR y TIFF. Para PNG se leen dimensiones, profundidad por canal y tipo de color del encabezado IHDR. Es lectura del encabezado, no un decodificador independiente que certifique todos sus pixels. Para EXR/TIFF y otros formatos, la precisión original queda `UNKNOWN` cuando no hay parser calificado. `image.depth` describe el buffer Blender y no se presenta como precisión original del archivo. `is_float` tampoco certifica si el archivo era de 16 o 32 bits por canal.

El SHA256 demuestra el contenido del archivo leído. No demuestra que el buffer cargado sea idéntico al archivo actual: `reload_status` permanece `UNKNOWN` y `reload_performed=false`. Un buffer dirty se informa y no se guarda para esconder la diferencia. Los archivos packed o generated pueden ser utilizables sin una ruta externa; su condición se conserva explícitamente. MOVIE/SEQUENCE/VIEWER no reciben compatibilidad estática universal.

Un binding `component=A` necesita alpha disponible y una normal RGB necesita al menos tres componentes. Usar el mismo datablock simultáneamente como color y como datos produce conflicto semántico. La construcción puede usar copias propias separadas con interpretaciones diferentes; la fuente compartida permanece igual. Las copias se resuelven por procedencia, generación y ownership, no por coincidencia parcial de nombre.

Presupuestos: hasta 100 imágenes/100 tiles por imagen, 512 MiB por archivo y 1 GiB total de dependencias. El hash se lee por bloques, sin transmitir archivos binarios por MCP. Las respuestas de imagen omiten los RGB internos muestreados. El cálculo de memoria es una estimación base; no incluye mipmaps, compresión, duplicados GPU ni overhead. Tiles cuya resolución no es verificable se contabilizan como desconocidos, no como cero certificado.

Ver [normales](normal-tangent-workflow.md) para las medidas de pixels y [UDIM](material-udim.md) para la correspondencia entre tiles y materiales.
