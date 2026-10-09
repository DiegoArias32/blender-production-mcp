# Seguridad de la asistencia de escultura (M4)

La asistencia de escultura de M4 es **limitada y por API**: operaciones de campo deterministas sobre
una malla candidata, con topología constante. No se usan brochas, ni el modo Sculpt, ni dyntopo, ni
remesh, ni multires.

| Operación | Qué hace | Riesgo principal |
|---|---|---|
| `smooth` | Laplaciano ponderado, iterativo | encoge volumen; puede cruzar caras astilla (guardia anti-volteo) |
| `relax` | Laplaciano tangencial (sin componente normal) | desliza la textura sobre la superficie; UV intactas, distorsión visual posible |
| `inflate` / deflate | desplazamiento a lo largo de la normal de vértice | en regiones que contienen un tramo de extremidad, hincha el cilindro (bultos) |
| `grab` | traslación con caída radial en torno a `center`/`radius_m` | estiramiento en el borde del radio |
| `crease` | componente normal del Laplaciano (marca o suaviza pliegues) | intensidad difícil de anticipar sin mirar |
| `translate`, `directional`, `scale` | desplazamiento uniforme, según dirección × normal, o escala por ejes | escalones en el borde si `width_m` es pequeño |

## Garantías

- Topología idéntica: mismos vértices, aristas, caras y bucles. Solo cambian posiciones (y las
  shape keys, con la misma delta; ver [shape-key-safety](shape-key-safety.md)).
- Máscaras y face sets se usan solo como definición de región; nunca se escriben.
- Las regiones bloqueadas tienen peso 0 por construcción, y se verifican después con tolerancia.
- La guardia anti-volteo atenúa los vértices que invertirían una cara, y la verificación falla con
  `FACE_FLIPPED` si aun así queda alguna.
- Normales: no se recalculan. Los datos `custom_normal` y `sharp_edge` almacenados se conservan
  byte a byte (medido en el fixture `humanoid_normals`). Como Blender 5.2 guarda las normales custom
  relativas a las automáticas, la normal efectiva sigue a la geometría editada; si una pieza de
  superficie dura depende de normales absolutas, requiere revisión visual.
- Pesos, grupos, modificador Armature, UV y materiales se comparan antes y después; cualquier cambio
  hace fallar la verificación.

## Límites observados

- Valores de plan relativos a la altura del cuerpo, no al grosor local: en una extremidad fina un
  inflate "pequeño" puede ser grande.
- Un empuje direccional deja una banda de sombreado horizontal en el borde inferior de la región
  (UPPER_BACK del fixture, vista trasera), incluso con caída suave de 7 cm. Mitigar exige una región
  mejor diseñada o una caída más ancha, y revisión visual.
- La interfaz gráfica de escultura no se automatiza. La revisión visual es con renders equivalentes
  (ver [character-visual-qa](character-visual-qa.md)).
