# Integración cuerpo + cara (M7)

Código: `facial/profile.py` (`ownership`), `facial/scene_runtime.py` (`fw_attach`, `fw_integration`, `fw_mark`,
`fw_head_track`). Tool: `facial_integrate_with_body` (y `facial_accept`).

## Propiedad de cabeza y cuello

| Política | Qué pasa | Cuándo se rechaza |
|---|---|---|
| BODY_OWNS (por defecto) | el cuerpo manda; la mirada la hacen los ojos | nunca |
| FACE_OWNS | la capa facial gira cabeza y cuello | si la Action del cuerpo anima cabeza o cuello (`HEAD_OWNERSHIP_CONFLICT`) |
| BLENDED | el cuerpo pasa a `action_blend_type = COMBINE` y suma su movimiento al de la tira facial | — |

La política queda en el plan y se informa en cada resultado. Medido en Blender 5.2.1: con REPLACE la Action activa del
cuerpo gana en los canales solapados aunque la tira sea ADD; con COMBINE en el cuerpo se suman (0,1 + 0,7 = 0,8).

## Qué se garantiza

- La Action del cuerpo nunca se escribe. Su digest se compara antes y después de cada evaluación, aceptación y rechazo,
  y antes de mutar (`BODY_ACTION_CHANGED` si cambió desde el plan).
- `facial_integrate_with_body` evalúa la candidata con el cuerpo en ≤ 60 frames y compara las matrices mundo de todos
  los huesos no faciales con y sin capa facial. Exige 0 cambio (y en cabeza/cuello también, con BODY_OWNS).
- Aceptar crea pistas NLA `M7_FACE <candidata>` en la armature y en cada Key; la Action del cuerpo sigue activa. Rechazar
  no adjunta nada y comprueba que los valores de shape keys y el cuerpo están como en el plan.
- La mirada se ancla al mundo frente al movimiento real de la cabeza (ver [ojos](eye-animation.md)).
- QA de intersecciones: si el estado solo-cuerpo ya intersecta en ese frame, la capa facial que lo amplía se informa
  como `MESH_INTERSECTION_EXTENDED` (aviso, con ambas cifras). La causa está en el cuerpo o el asset.

## Demo D (checkpoint M6 copiado, `M6_hit_reaction_Rig`, 55 frames)

- Digest de la Action del cuerpo idéntico antes, durante y después de aceptar (también tras reabrir el archivo).
- Cambio máximo de matriz en los 19 huesos del cuerpo (sin contar cabeza y cuello): 0; cabeza y cuello también 0 con BODY_OWNS.
- Cabeza movida por el cuerpo hasta 11,4 cm y 6,6°; error de la mirada medida 0,089°.
- Dolor + parpadeo lento + mirada a cámara; propiedad BODY_OWNS explícita.
- El cuerpo proxy de M6 mete la cabeza en el cuello de la fixture en los frames 8–9 (19 polígonos sin cara). Con la capa
  facial se añadía 1 polígono vecino, que hoy se clasifica como `MESH_INTERSECTION_EXTENDED`.

## Límite conocido

Con una pista NLA facial aceptada, los analizadores heredados de M2/M3 que rechazan composición NLA en la armature
seguirán rechazando esa armature. Es el comportamiento esperado de su guard y no se ha relajado.
