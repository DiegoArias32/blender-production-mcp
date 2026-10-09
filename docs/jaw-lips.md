# Mandíbula y labios (M7)

Código: `facial/layers.py` (`jaw_part`), `facial/profile.py` (calibración), `facial/resolve.py` (`seal_track`).
Tools: `facial_analyze_jaw`, `facial_generate_jaw`, `facial_analyze_lips`.

## Generación de mandíbula

Modos: `breathing` (≤ 0,12, periodo 2–8 s; propuesta de reposo), `shout`, `roar`, `tension`, `pain`; el habla sale del
lip-sync. Cada modo tiene objetivos de pico propuestos. **Antes de escribir nada, el pico se mide en Blender**:
intersecciones nuevas con cuello u otras mallas, caras colapsadas o invertidas, cruce de labios. Cualquier problema da
`JAW_EXTREME_COLLISION` con las cifras. Medido: en la fixture `jaw_neck_collision` el rugido se rechaza; en la limpia,
`shout` genera y pasa la QA.

Calibración de mandíbula al 100 %: hueco de boca, intersecciones nuevas por malla, colapsos/inversiones. Limpia:
28,3 mm y 0 intersecciones. Con colisión: 57,7 mm, 68 polígonos contra el cuello, 8 colapsados y 208 invertidos
(`JAW_COLLISION`).

## Modelo medido del hueco de labios

Con el bucle del borde de la boca se mide el hueco medio entre labios en varias poses y se ajusta

`hueco(j, c) = g0 + a·j + Σ gᵢ·salidaᵢ − (k − d·j)·c`

con `j` = mandíbula, `c` = control de cierre y `gᵢ` la ganancia medida de cada salida de boca (presión, enrollar,
estirar, comisuras...). Los cuatro extremos fijan `a`, `k` y la interacción `d`; la pose 50/50 valida el modelo (error
< 1,5 mm, propuesta). `lips_close` = 1 significa **sellar lo que deja abierto toda la pose**, no "control al 100 %".

| Fixture | Reposo | Cierre a tope (k) | Interacción d | Error 50/50 |
|---|---|---|---|---|
| ARKit limpia | 4,92 mm | 28,69 mm | 0 | 0,001 mm |
| Híbrida (hueso + clave) | 4,92 mm | 28,69 mm | 4,05 mm | 0,22 mm |
| Huesos | 4,92 mm | 4,24 mm | 0 | 0,30 mm |

Por qué hizo falta: el modelo solo con mandíbula dejaba los labios cruzados −1,1 mm cuando el visema M_B_P también
aprieta; el modelo lineal fallaba 4 mm en el híbrido y cruzaba los labios 24 mm.

Hallazgos:

- `LIP_CROSSING_AT_FULL`: mandíbula y cierre a tope se cruzan (fixture `lip_penetration`: −18,7 mm).
- `LIP_SEAL_INSUFFICIENT`: el cierre a tope no cubre el reposo (huesos) → lip-sync LIMITED y QA de candidata con
  `MBP_NOT_SEALED` (~2 mm).
- `LIP_CLOSURE_UNCALIBRATED`: el modelo no se valida; se usa el valor completo del control y se avisa.

## Controles de labios

Se usan las salidas semánticas medidas (`lip_press`, `pucker`, `funnel`, comisuras...). Un control bilateral compartido
recibe la media de L/R y se informa la asimetría perdida. Nunca se keyean canales con driver ni huesos que anima el cuerpo.
