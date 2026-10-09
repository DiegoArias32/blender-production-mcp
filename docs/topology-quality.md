# Calidad de topología M5

`retopology_analyze` y `retopology_validate` miden la malla base sin transformar el objeto ni aplicar
modificadores del usuario. Se devuelven recuentos, distribuciones y hasta 30 IDs de ejemplo por
defecto; la topología completa permanece en Blender.

Se informa de vértices/aristas/polígonos/triángulos, tris/quads/ngons, límites abiertos, aristas con
más de dos caras, aristas sueltas, vértices aislados, componentes conexos, valencias, indicadores de
vértices duplicados, áreas, calidad y aspecto de triángulos. La calidad es
`4*sqrt(3)*area/(a²+b²+c²)` (1 en un equilátero); un sliver está por debajo de 0,02 por defecto. El
aspecto es `arista_mayor²/(2*area)`. Una superficie con triángulos de área ≤1e-14 m² es degenerada.
Todos esos límites son configurables y aparecen en la respuesta.

Las direcciones de aristas compartidas detectan inconsistencias entre caras. En componentes
cerrados se calcula volumen orientado: un volumen negativo se informa como orientación hacia
dentro. No se afirma que cualquier cara aislada mirando en cierta dirección esté invertida. Una
superficie abierta no tiene una definición universal de "afuera".

El recuento de bordes detecta límites o posibles agujeros. `allow_boundary=false` (por defecto)
rechaza los bordes abiertos; `true` admite superficies deliberadamente abiertas. No se confunden
aristas de límite con un diagnóstico semántico de agujero. Los duplicados usan posiciones
cuantizadas a 1e-7 m y son indicadores, porque vértices separados pueden ser intencionales.

Los gates rechazan superficies vacías, no-manifold, degeneradas, inconsistentes/invertidas,
slivers por encima de `maximum_sliver_triangles` (0 por defecto), aristas/vértices aislados y más
de `maximum_components` (128 por defecto). Un personaje con varias partes deliberadas puede
configurar ese límite; no se fusionan partes para eludirlo. La calidad de un game mesh no exige
todos los polígonos quad.

Las fixtures introducen agujero, arista no-manifold y sliver por separado con bpy/BMesh para que
los tests comprueben el defecto real, y no una firma fabricada. La criatura de M5 conserva una
conectividad cerrada y cambia su diseño mediante posiciones, sin reparar las fixtures históricas.
