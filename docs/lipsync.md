# Lip-sync con timing validado (M7)

Código: `facial/speech.py`, `facial/layers.py`. Tools: `facial_prepare_speech`, `facial_generate_coarticulation`,
`facial_generate_lipsync`, `facial_validate_lipsync`.

## Entrada

Solo timing explícito: fonemas o visemas con inicio y fin (segundos o frames), palabras con acento 0–1 opcionales y una
envolvente de energía opcional. **No existe un alineador local validado**: audio sin timing → `TIMING_INPUT_REQUIRED`.
Nunca se inventan tiempos ni se usan servicios de pago. La energía solo escala la amplitud de la mandíbula (×0,8–1,2);
no crea ni mueve eventos lingüísticos.

Validación: tiempos finitos, fin > inicio, sin solapes, dentro del clip, fonemas conocidos, visemas del conjunto, como
mucho 2.000 segmentos.

## Coarticulación

Funciones de dominancia (Cohen & Massaro, 1993): cada segmento atrae la boca hacia su objetivo con una dominancia que
decae con el tiempo; cada frame es la media ponderada. Las consonantes son más estrechas. Después se imponen los cierres
dominantes en su intervalo. `hard=True` genera el pose a pose de referencia para comparar. Medido en el test unitario:
la tasa máxima de la mandíbula es menor con coarticulación que pose a pose, y los dos cierres M_B_P se alcanzan.

## Énfasis

Las palabras acentuadas abren más la mandíbula (+30 %) y los labios (+20 %), con un leve cabeceo (2°) y subida de ceja
interior (0,2), todo propuesta. Con `BODY_OWNS` el cabeceo se descarta y se informa.

## Capa de habla

SPEECH es dueña de la boca mientras se habla: la expresión conserva el 30 % en la boca y el 70 % en las comisuras (demo C:
la sonrisa leve se mantiene al 70 % frame a frame). Un cierre de labios de la expresión contra una vocal abierta se
registra como conflicto resuelto.

## Validación

- Por canal: pico de cierre en cada M_B_P (≥ 0,9) con error de tiempo en ms, contacto F/V, apertura de vocales,
  `JAW_LIP_CONFLICT`, `MOUTH_POPPING`.
- Por geometría, sobre la candidata en Blender: hueco de labios medido en el centro de cada bilabial (`MBP_NOT_SEALED`
  > 1 mm) y cruce de labios (`LIP_CROSSING` < −1 mm). Umbrales propuestos.

Demo C ("Bob made a map", 2 s/30 FPS): los 5 bilabiales miden 0,0 mm de hueco, error de tiempo máximo 36,7 ms (un
frame a 30 FPS), sin cruce, y la expresión se mantiene debajo.

No se declara precisión lingüística: el timing es el suministrado.
