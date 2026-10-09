# Comparación HIGH ↔ LOW y proyección M5

Las correspondencias usan BVH sobre la geometría base de HIGH en **metros de mundo**, con mapeo
explícito de triángulo, vértices, loops, polígono y componente conexo. Las coordenadas baricéntricas
permiten transferir datos sin casar índices de HIGH y LOW. Las listas de correspondencia son
locales a Blender y las reutilizan transferencia y bake.

`retopology_compare_source` mide en ambos sentidos puntos reproducibles: estratos de índices de
triángulo y muestras baricéntricas de baja discrepancia. Por defecto toma hasta 2048 triángulos por
sentido (32..32768 configurable). Devuelve media, mediana, P95, P99, máximo muestreado en metros y
desviación angular de normales, con el método/contador. No se afirma distancia de Hausdorff total
ni máxima continua; un rasgo pequeño no muestreado puede escapar.

`retopology_project_to_source` presenta primero una propuesta de proyección al punto cercano del
HIGH. Rechaza distancia >`max_distance_m` (1 cm por defecto), normal incompatible
>`max_normal_angle_degrees` (80°) o superficies de componentes distintos cuya distancia difiere
menos de `ambiguity_distance_m` (0,1 mm). Un triángulo singular se rechaza. Estos límites son
configurables; aumentarlos no es una solución automática a ray misses.

La proyección crea otro candidato, deja fijos los vértices protegidos y vuelve a medir topología,
superficie, silueta, regiones y atributos protegidos. No puede proyectar shape keys con seguridad
en este adaptador: devuelve una alternativa manual. La detección de ambigüedad por componentes no
es una certificación universal de correspondencia entre pliegues cercanos de una misma superficie;
las normales/distancias y el QA adicional siguen siendo necesarios.

`retopology_compare_silhouette` rasteriza máscaras mediante raycasts ortográficos para FRONT,
SIDE, THREE_QUARTER y BACK opcional. Ambas geometrías usan **la misma caja, rejilla, dirección y
tamaño de píxel**. Devuelve IoU, área de diferencia simétrica y distancias de los bordes en ambos
sentidos (media/mediana/P95/P99/máximo). Resolución por defecto 128 (32..256); el error inferior a
un píxel no es resoluble. Los defaults son IoU ≥0,95 y máximo de borde ≤2 cm.

Estas medidas no sustituyen a renders equivalentes de antes/después, ni a QA de deformación en las
mismas poses. Cada operación satisfactoria mantiene `visual_verification="required"`.

Las regiones protegidas tienen una comprobación adicional: todos sus vértices se emparejan por
posición y cada triángulo completamente interior aporta cuatro muestras de superficie (centro y
puntos medios). Un quad no coplanar con diagonal distinta puede fallar esta comprobación aunque no
se haya desplazado ningún vértice. `game_mesh_triangulation_preview` debe pasar la misma comprobación antes
de convertir ese candidato en LOW válido.
