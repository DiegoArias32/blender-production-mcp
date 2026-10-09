# Mocap facial genérico (M7)

Código: `facial/mocap.py`. Tools: `facial_mocap_analyze`, `facial_mocap_map`, `facial_mocap_retarget`,
`facial_mocap_cleanup`.

## Formato

`{fps, frame_start, channels: {nombre: [valor por frame]}}` en dict, `.json` o `.csv` (cabecera de canales; columnas
`frame`/`time` ignoradas). Canales normalizados 0–1, todos con el mismo número de frames.

## Análisis

Rango, media, ruido de alta frecuencia (RMS de la segunda diferencia), parpadeos detectados, grupos presentes, canales no
reconocidos y fuera de rango. No modifica nada.

## Mapeo

Los nombres se normalizan a tokens (`eyeBlinkLeft`, `eye_blink_l`, `brow_down.R`...). El lado de una sola letra solo
cuenta tras un separador; antes `mouthPucker` se leía como lado R y se corrigió. Los tokens se mapean a salidas
semánticas del rig medido, nunca a nombres de controles. El ángulo completo de las claves de mirada se **supone** 25°
(`eye_look_full_deg`, configurable); los SDK no lo estandarizan. `mirror` cambia de lado.

Bloqueo: sin mandíbula y parpadeos cubiertos, o con menos del 60 % de canales mapeados → `MOCAP_MAPPING_INCOMPLETE`
(una captura con nombres desconocidos se rechaza sin crear nada).

## Retarget

Neutral restado (mediana de los primeros 10 frames o explícito), rango renormalizado, remuestreo lineal a los FPS del
brief, y rechazo si la captura es más larga que el clip (`MOCAP_LONGER_THAN_CLIP`; nunca se recorta en silencio). La
candidata v1 conserva las pistas crudas.

## Limpieza

Suaviza fuera de lo rápido intencional:

- normalizado: protege frames con cambios ≥ 0,12/frame y todos los parpadeos (> 0,5); rellena picos incompletos
  0,75–0,95 → 1,0 (se informa como desviación);
- grados (ojos): protege solo **sacadas** (tramos monótonos de ≥ 3° en ≤ 4 frames). El jitter alterna signo y se suaviza;
  antes se protegía como si fuera sacada y apenas bajaba (2,53 → 2,44);
- mandíbula: el castañeteo pequeño se sustituye por la tendencia;
- simplificación de claves con tolerancia por unidad.

Informe por canal (claves antes/después, ruido antes/después, desviación máxima y media) y **por unidad**; la reducción
global es la media por canal de `1 − ruido_después / ruido_antes`, para no mezclar grados con normalizados.

## Demo E (captura sintética, 5 s/30 FPS, ruido 0,025, castañeteo 0,03, jitter de ojos 0,04)

- 33/33 canales mapeados (100 %).
- Claves: 4.530 → 1.744.
- Reducción de ruido: normalizados 54,6 %, ojos 42,1 %, media por canal 74,2 %.
- Desviación máxima: 0,061 en normalizados y 1,64° en ojos (jitter eliminado).
- v1 y v2 pasan la QA. En el render, v1 y v2 solo difieren en 79–259 de 262.144 valores de canal por frame: la limpieza
  se aprecia en las curvas, no a 256 px.
