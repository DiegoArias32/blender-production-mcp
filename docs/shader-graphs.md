# Grafos de shader M9

El constructor recibe una definición estructurada: categoría, flujo Metallic/Roughness,
parámetros, canales de imágenes existentes, convención de normales, modo de altura y renderer.
No recibe expresiones Python. La normalización vuelve a ejecutarse en el adaptador host,
además de los controles del servicio MCP.

## Fuente y candidata

`material_create_candidate` crea objeto, malla y material propios. Conserva los enlaces DATA/OBJECT
de los slots en la copia; los slots originales no se reasignan. Los grupos de nodos compartidos
no se editan. Los intentos anteriores permanecen como versiones separadas.

Antes de copiar o construir, se comprueban las firmas de fuente y candidata, los archivos
dependientes y el contexto. Dentro de Blender se repiten las comprobaciones de datos/contexto
y rol esperado inmediatamente antes de crear datablocks. El registro host es la autoridad:
FIXTURE puede estar registrado sin sello de escena; WORKING_COPY requiere su sello coincidente.
MASTER, un sello ajeno, un archivo diferente o datos obsoletos bloquean la mutación.

## Construcción determinista

El adaptador crea un Principled BSDF y Material Output con nombres propios. Consulta sockets
y enums de la versión instalada. El método SSS forma parte de la definición; el valor inicial
de los perfiles es `RANDOM_WALK_SKIN`, no una afirmación de composición física.

En Blender 5.2.1, `inputs.get()` puede ocultar sockets no disponibles para el método activo.
Por ejemplo, Subsurface IOR no está activo con RANDOM_WALK. Un parámetro requerido sin socket
aplicable produce un rechazo; no se interpreta como configurado. La firma usa método,
valores y conexiones autorales. Excluye el estado UI derivado `socket.enabled`, que se observó
cambiar después de copiar un material aunque sus datos autorales permanecieran iguales.

Los canales R/G/B usan Separate Color RGB; A usa Alpha; una conversión COLOR a escalar usa
RGB to BW explícito. AO conserva su identidad como dato independiente y no se multiplica
automáticamente por Base Color. Normal tangent usa Normal Map y UV explícita. La convención
OPENGL es necesaria para esta construcción; DIRECTX y UNKNOWN se rechazan sin invertir verde
por heurística. Bump y displacement usan nodos distintos.

La preparación se realiza en un material temporal antes de sustituir los nodos de la candidata.
El árbol fuente nunca se limpia. El servicio conserva checkpoint, candidata y error si ocurre
un fallo durante la transacción; un timeout no autoriza un segundo intento con otra identidad.

## Imágenes

Cada imagen que necesita configuración se duplica antes de modificar color space. En la API
real, `Image.copy()` no conserva necesariamente ediciones no guardadas del buffer: una prueba
con imagen generada produjo una copia negra. El adaptador restaura y verifica todos los valores
del buffer de la copia cuando difieren. No guarda ni sobrescribe archivos de textura fuente.

La firma diferencia contenido externo y buffer cargado. El host calcula SHA-256 de los archivos;
Blender firma los valores accesibles del buffer y sus propiedades. No se afirma que una imagen
esté recargada: el estado es `UNKNOWN_NOT_RELOADED`. En UDIM se firman todos los archivos de tile,
pero la cobertura de píxeles en memoria corresponde al tile activo; buffers tiled sucios se
rechazan. Esta limitación se devuelve explícitamente.

## Límites comprobados

- Grafos acotados: 512 nodos y 2048 conexiones por árbol, profundidad de grupo 8 y hasta 2048
  nodos visitados en la firma completa. Los datos se firman completos dentro del presupuesto;
  superar un límite produce rechazo, no una firma truncada.
- No se reconstruyen shaders con animación de material ni drivers arbitrarios. OSL/Script,
  IES y dependencias no cubiertas se rechazan para las mutaciones calificadas; inspeccionar
  un shader no implica que pueda reconstruirse.
- Las simulaciones nativas cuyo estado/cache no puede firmarse por completo se rechazan.
  Mesh Cache externo tiene sus parámetros, ruta y contenido incorporados a la firma.
- True displacement puede construir la conexión y propiedad de Cycles, pero la QA mantiene
  `DISPLACEMENT_GEOMETRY_UNCERTIFIED` mientras no exista una calificación geométrica adecuada.
  Un grafo conectado no demuestra desplazamiento efectivo ni constituye bump equivalente.
- EEVEE transmission activa la propiedad de refracción del material candidato cuando la API
  la expone; el renderer de estudio controla su propio ray tracing y reporta sus límites.
  No se inventa espesor ni se altera la geometría para mejorar la apariencia.

Las verificaciones Blender de desarrollo están en `test-artifacts/m9-integrity/`: preservación
de fuente, UV, shape keys, pesos, Action/NLA y rig; conexiones/canales; píxeles de imágenes
candidatas; negativos de normal convention, shader animado, driver, candidata obsoleta y
displacement no aplicable. Los FAIL anteriores se conservan junto a los probes corregidos.
Las pruebas de transporte MCP y la aceptación visual se documentan separadamente en el reporte
del milestone; estos probes directos no las sustituyen.
