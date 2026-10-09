# QA visual de personaje (M4)

Los números no bastan: un edit puede pasar toda la verificación numérica y verse mal. Estas
herramientas producen imágenes **comparables**; la conclusión la saca quien las mira.

## Renders equivalentes

`character_render_multiview` y `character_compare_before_after` renderizan con:
- una sola cámara ortográfica temporal por vista (FRONT, SIDE, THREE_QUARTER, BACK), con encuadre
  calculado sobre la **unión** de los objetos comparados, para que antes y después compartan cámara;
- la misma resolución y el mismo look neutro: Workbench, color gris único 0,8, luz de estudio, sin
  sombras, cavity ni contorno, fondo 0,05, para que la luz o el material no se confundan con geometría;
- un objeto visible cada vez (los demás se ocultan temporalmente).

Motor, sombreado, visibilidad, cámara y estado de QA se restauran en `finally`, y el resultado informa
de `state_restored`. Workbench rasteriza en GPU y el backend real se informa (en las ejecuciones M4:
`GPU rasterizer (OPENGL)`, sin fallback). Se genera un montaje de 3×2 como máximo, con PNG de 8 bits.

`character_compare_before_after` añade deltas de silueta por vista (altura, anchura, área, centroide,
balance izquierda/derecha, alcance, perfil de anchuras) y las medidas con nombre, antes y después.

## Referencias

- `character_capture_reference_views` guarda un conjunto de referencia: cámaras, resolución, look,
  imágenes y siluetas, y opcionalmente asocia concept art a vistas. **No hay image-to-3D ni
  comparación de píxeles con el concept.**
- `character_capture_candidate_views` renderiza un candidato con exactamente esas cámaras.
- `character_compare_reference` devuelve diferencias de silueta medibles e imágenes emparejadas,
  sin afirmar equivalencia semántica.

## Qué se vio en M4 (fixtures)

- **Demo A (humanoide):** a cuerpo entero, más masa en la espalda en la vista lateral y hombros algo
  más anchos. En los renders de cerca, el primer plan (inflate de hombros de 1,8 cm) dejaba bultos
  esféricos en la parte alta del brazo que terminaban en el borde de la región. Por eso la regla se
  cambió a una escala en X. Con el plan final (empuje de la espalda, escala de hombros y suavizado)
  no hay bultos; queda una **banda de sombreado horizontal** en el borde inferior de UPPER_BACK en la
  vista trasera.
- **Demo B (criatura):** el suavizado del torso no toca el brazo sobredimensionado ni la joroba
  (desplazamiento 0,0 m). Antes de la guardia anti-volteo, una cara astilla de la unión se volteaba
  174°; con la guardia, 0 caras volteadas (cambio máximo de normal 57°).

Límites: los fixtures están hechos con el modificador Skin y son formas simples; lo visto en ellos no
certifica la calidad sobre un personaje de producción.
