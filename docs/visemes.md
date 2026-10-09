# Visemas (M7)

Código: `facial/model.py` (`VISEMES`, `PHONEME_TO_VISEME`, `viseme_support`). Tool: `facial_map_visemes`.

## Conjunto independiente del rig

REST, A, E, I, O, U, M_B_P, F_V, L, TH, S_Z, SH_CH, R, W_Q. Cada visema es un conjunto de objetivos semánticos
(mandíbula, cierre, redondeo, estiramiento, labio inferior...). Los valores son propuestas para fixtures; el lip-sync
los combina por coarticulación, nunca pose a pose.

M_B_P, F_V y W_Q son **dominantes**: deben alcanzar su objetivo dentro de su intervalo y las vocales vecinas no pueden
diluirlos (M_B_P 1,0, F_V 0,85, W_Q 0,7).

## Fonemas

ARPAbet → visema (los dígitos de acento se ignoran). Un fonema desconocido o un visema fuera del conjunto se rechazan
(`TIMELINE_INVALID`, `UNSUPPORTED_VISEME`).

## Soporte por rig

`viseme_support` mira las salidas que el rig **tiene de verdad**:

- `SUPPORTED`: todas las salidas necesarias usables;
- `APPROXIMATED`: falta alguna (ej. TH sin lengua → solo mandíbula); se informa qué falta;
- `UNSUPPORTED`: no se puede formar. M_B_P sin `lips_close`, A u O bloquean el lip-sync (`VISEME_UNSUPPORTED_BY_RIG`).

`facial_map_visemes` resuelve cada visema a valores concretos de los controles medidos, con el cierre de labios del
modelo medido de hueco.
