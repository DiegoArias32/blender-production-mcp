# Densidad, prioridades y presupuestos M5

La densidad local es **triángulos por m² de superficie**, calculada a partir de áreas de mundo. Un
grid espacial (8³ por defecto, `density_grid` 2..32) agrupa centroides de triángulos y suma su área
real; no se divide por volumen de caja. Se informa de distribución local, longitudes de arista en
metros y P95/mediana. Una densidad adaptativa no es un defecto por sí sola.

Para cada región explícita se devuelve área, triángulos por m², ángulos diedros de caras vecinas y
prioridades de deformación, visuales y de silueta. Un triángulo pertenece a una región si al menos
dos de sus vértices están dentro; las regiones pueden solaparse y sus recuentos no se suman como
geometría independiente.

La prioridad por vértice combina `priority * silhouette_priority * density_weight` y un indicador
de curvatura `1 + curvature_weight * dihedral/180` (peso de curvatura 2 por defecto). La reducción
usa el inverso de la prioridad; las regiones protegidas/deformables y los límites conservadores
tienen peso de reducción cero. Cara, manos, articulaciones o detalle crítico de bake reciben
prioridad mediante el manifiesto, no por una regla anatómica universal. La asimetría no se corrige.

`retopology_budget` acepta target y rango mínimo/máximo. Una región puede expresar un presupuesto
absoluto, un porcentaje o un peso de densidad. El plan informa de asignaciones y de su solapamiento;
el apply verifica los presupuestos absolutos/porcentuales y los suelos de densidad solicitados.
Con `density_weight` se orienta la reducción en vez de prometer una cuota exacta por una región
solapada. Un target incompatible con protección se rechaza; nunca se desbloquea una zona para
conseguir el número.

El plan y cada operación almacenan las reglas compactas y fingerprints. Los arrays de pesos de
millones de vértices se recalculan determinísticamente dentro de Blender y nunca viajan en MCP.
La prioridad no es una puntuación artística.
