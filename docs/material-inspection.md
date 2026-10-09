# Inspección de materiales — M9

Este documento describe el contrato implementado. La aceptación de los demos y el cierre de M9 se establecen mediante el reporte y su evidencia, no por estos ejemplos. El desarrollo utiliza exclusivamente fixtures sintéticas; un material real requiere autorización concreta y una copia de trabajo registrada.

`production.material_inspect`, `production.material_analyze_nodes` y `production.material_compatibility_report` leen materiales existentes sin reasignar slots, recargar imágenes, cambiar frames ni reescribir nodos. Un material puede seleccionarse mediante `material_name` y, para comprobar su relación con geometría y UV, `object_name`. También se acepta la referencia `material` devuelta al crear un plan; si tiene candidata, se inspecciona esa versión y se comprueba su frescura.

Ejemplo sobre la fixture `tests/blender/fixtures_m9.py`:

```json
{
  "object_name": "M9_metallic",
  "material_name": "M9_metallic_Material",
  "options": {"detail": "nodes", "offset": 0, "limit": 10}
}
```

Las opciones `detail` son `summary`, `nodes`, `images` y `assignments`. El valor predeterminado es `summary`, con 20 filas. `limit` admite 1–100 y `offset` es un entero no negativo. Se devuelve `total` y `has_more`; el registro completo acotado se usa internamente para QA, sin convertir la respuesta pública en un dump de todo el grafo.

La inspección cubre slots DATA/OBJECT, material activo, objetos asignados y recuentos de polígonos, nodos, valores de sockets, links reales, grupos anidados, imágenes y archivos locales, Action/drivers/NLA del material y node tree, métodos alpha/displacement disponibles, renderer, unidades y relaciones UV/UDIM. Los links registran identificadores e índices de sockets, validez y mute. Nombres de nodos y propiedades no sustituyen la conexión observada.

Los grupos se recorren con rutas como `Group/Node`. La inspección de un grupo no implica que el constructor pueda editar su lógica: el builder M9 crea un grafo Principled controlado sobre una candidata. Grafos animados, shaders no calificados o dependencias no demostrables se informan como `UNKNOWN` y bloquean la certificación que dependa de ellos; no se borran para forzar compatibilidad.

Límites del lector: 256 nodos, 1.024 links, 4.096 sockets, hasta ocho niveles de grupos, 100 imágenes, 100 tiles por imagen, 128 objetos asignados y 200.000 loops de malla para análisis UV. Exceder un presupuesto produce un error explícito. Las firmas de integridad son un subsistema independiente: nunca se firman sólo las filas paginadas.

La clasificación utiliza estructura y parámetros del shader, dependencias, relaciones de geometría y manifiesto declarado. `options.manifest.category` puede expresar una categoría del contrato; su procedencia se registra como `caller`. No clasifica exclusivamente por nombre. Un Principled con SSS puede corresponder a piel, hueso, nieve u otro material: se devuelven alternativas y confianza, sin presentar composición física como hecho.

Los resultados separan `ERROR`, `WARNING`, `INFO` y `UNKNOWN`. `passed=false` significa un error o desconocimiento bloqueante, no una puntuación artística. Revisar [PBR](pbr-workflows.md), [texturas](texture-color-management.md) y [UV/UDIM](material-udim.md) antes de construir una candidata.
