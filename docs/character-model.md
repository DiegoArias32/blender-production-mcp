# Modelo de personaje (M4)

Qué miden las herramientas de análisis de personaje, sobre qué datos y con qué convenciones. Todas
son de **solo lectura** y devuelven **medidas**, nunca una puntuación artística.

## Convenciones

| Tema | Convención |
|---|---|
| Geometría | Malla base **sin deformar** (pose de bind) de un único objeto malla explícito. No se evalúan modificadores ni la pose actual. |
| Unidades | Metros de mundo: `matrix_world` × `unit_settings.scale_length`. |
| Orientación | Personaje mirando a −Y, +Z arriba. La izquierda del personaje es +X (convención de Blender `.L`). |
| Vistas | Ortográficas. FRONT (derecha de pantalla = +X), BACK (−X), SIDE (+Y), THREE_QUARTER (diagonal +X+Y). |
| Datos | Por vértice nunca sale de Blender; la respuesta es compacta. |
| Modo | Solo modo Object. El modo nunca se cambia automáticamente. |

## `character_inspect`

Recuentos (vértices, aristas, polígonos, triángulos), límites y dimensiones de mundo, padre y
armature, simetría respecto a X=0 (distancia al espejo con KD-tree: fracción simétrica con tolerancia
relativa a la altura, mediana, p95 y máximo), avisos de topología (bordes, no-manifold, aristas sueltas,
vértices sueltos, caras degeneradas, tris/quads/ngons, polos de valencia ≥6), cobertura de pesos
(vértices sin peso de deformación, influencias máximas), shape keys (relativas o absolutas, Basis,
vértices movidos por clave, drivers), rol del asset y **candidatos de región automáticos**
(ver [region-locking](region-locking.md)). Devuelve también el resumen de la firma de malla de M3.1,
cuyo `fingerprint` es la huella de frescura que exige `character_create_candidate`.

## `character_analyze_silhouette`

Rasterización geométrica de triángulos por vista (sin render), con una rejilla de `rows` filas
(50..600, por defecto 200). Por vista: altura, anchura, área, centroide, fracción de área a la
izquierda, alcance máximo de extremidades desde el origen, espacio negativo cerrado (huecos y área),
perfil de anchura en 10 alturas y la extensión proyectada de cada región. En FRONT/BACK, con región
TORSO, también `waist_width_m` y `torso_max_run_width_m` (anchura del tramo continuo que cruza el
centro, para no sumar los brazos).

`named_measurements` combina las extensiones de las regiones: anchura de hombros, torso, cintura y
pelvis, altura de cabeza y relación cabeza/cuerpo, proyección de extremidades, profundidad de la
espalda alta, etc. **Si la región no existe, la medida es `null`; nunca se estima.** El valor depende
de cómo esté definida la región: con regiones por grupo dominante, la "anchura de hombros" incluye la
parte del brazo que domina el hueso del hombro.

## `character_analyze_proportions`

Perfiles:

- **HUMANOID**: compara con un canon de dibujo de figura **informativo** (7,5 cabezas de altura,
  hombros ≈ 2 cabezas, piernas ≈ mitad de la altura, brazo derivado de la regla de Vitruvio
  "envergadura = altura"). Es una aproximación de canon, no antropometría medida, y nunca es un
  objetivo de corrección.
- **CREATURE**: solo medidas; no se aplica ninguna referencia humana.
- **CUSTOM**: compara solo con las referencias que dé el usuario.

Cada fila separa `value` (medida), `reference`, `deviation`, `confidence` e `interpretation`.
`design_recommendations` está siempre vacío. Las longitudes de brazo y pierna salen de los huesos
(ARM+HAND; LEG solo con huesos mayoritariamente verticales, para no contar un conector de cadera
horizontal). Las relaciones izquierda/derecha se informan, no se corrigen.

## `character_analyze_asymmetry`

Métricas por región (área de superficie, volumen de caja, fracción de vértices sin espejo) y por pareja
LEFT_/RIGHT_ (log de la razón de área y volumen). Clasificación con umbrales declarados en la
respuesta:

| Clase | Regla |
|---|---|
| intentional | la región está declarada intencional (manifiesto o parámetro) |
| likely_intentional | desviación ≥ 0,22 (≈ 25 %) |
| likely_accidental | 0,015 ≤ desviación < 0,10 **y** la malla es ≥ 90 % simétrica |
| symmetric | desviación < 0,015 |
| unknown | el resto, incluidas las desviaciones pequeñas en un diseño asimétrico |

Las regiones sin pareja con ≥ 50 % de vértices sin espejo se listan como rasgos asimétricos. **La
simetría nunca se impone.** Los umbrales son heurísticos y no están calibrados con personajes
reales.

## `character_analyze_surface`

Indicadores por vértice, agrupados en clusters conexos y asignados a la región dominante: bulto
inesperado, pellizco, cambio brusco de curvatura, ruido de alta frecuencia, densidad incoherente,
estiramiento, riesgo de facetado (ángulo diedro alto entre caras suaves no marcadas como sharp) e
irregularidad de normales. Los umbrales son relativos a la propia malla (mediana + k·MAD) con suelos
absolutos. Todos los valores se devuelven en `thresholds`. `suspicious_flattening` está declarado pero
**no se calcula** (backlog). Es solo análisis: no hay retopología.

## `character_analyze_intersections`

Indicador de auto-intersección por solapamiento de triángulos no adyacentes (BVH), agrupado por pareja
de regiones, con muestras de localización. Con `reference_object`, que debe tener la misma topología,
solo informa de los solapamientos **nuevos**. Confianza 0,7. Limitaciones: caras que se tocan o son
coplanares son ambiguas, no mide profundidad de penetración y no es un solver de colisiones.

## `character_modeling_audit`

Hallazgos con `type`, `region`, `severity`, `evidence` y `suggested_action`, a partir de superficie,
asimetría probablemente accidental, intersecciones, avisos de topología, huecos de pesos y (con
manifiesto) regiones bloqueadas alteradas. Sin puntuación global: las acciones sugeridas son
operaciones que conservan la topología, o "fuera de alcance M4" (retopología, pesos).
