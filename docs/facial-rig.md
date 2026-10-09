# Rig facial: detección, inspección y clasificación (M7)

Código: `facial/runtime.py` (payloads `INSPECT` y `PROBE`), `facial/semantics.py`, `facial/profile.py`.
Tools: `facial_inspect`, `facial_classify_rig`, `facial_analyze_shape_keys`, `facial_analyze_bones`,
`facial_analyze_hybrid`, `facial_compatibility_report`.

## Conjunto facial

Se parte de una malla facial o de su armature. Sin opciones explícitas se resuelve así:

- armature: opción `armature`, modificador Armature de la malla, o su padre;
- mallas: la dada y todas las que la armature deforma o hijas de ella;
- ojos: mallas cuyo ajuste de esfera tiene error < 0,06, **caja con cada eje ≈ diámetro (±25 %)** y radio entre el
  2 % y el 25 % de la malla facial dada. El criterio de caja se añadió al medir que un cilindro de dos anillos ajusta
  una esfera con error 0; el tamaño de referencia es la malla facial (un cuerpo de 1,8 m dejaba los ojos de 11,5 mm
  por debajo del 2 %);
- cabeza: hueso dado, o el hueso con "head" más cercano al punto medio de los ojos.

`options.facial_set = {armature, meshes, eyes, head_bone}` fija todo explícitamente (la demo D lo usa para excluir el
cuerpo proxy de M6, que lleva su propia cabeza en el mismo sitio).

## Marco facial normalizado

Origen en el punto medio de los ojos, unidad = distancia interocular (IOD), `x` izquierda del personaje, `z` arriba
(eje del hueso de cabeza), `y` hacia delante. La boca se mide con el bucle de borde bajo los ojos. Todo lo que se
compara entre rigs (zonas, direcciones, cámaras) vive en este marco.

## Inspección (solo lectura)

Mallas con rol, ojos (centro, radio, error de ajuste), huesos faciales con límites, constraints, drivers y canales
animados por el cuerpo, shape keys (relativas, Basis, sliders, drivers), Action del cuerpo, pistas NLA y avisos:

- `SHAPE_KEYS_ACTIVE_AT_REST` (claves ≠ 0 en reposo; Blender 5.2.1 crea las claves por Python con valor 1,0, medido);
- `ABSOLUTE_SHAPE_KEYS` y `BASIS_INVALID` (Basis distinta de la malla o silenciada) **bloquean** cualquier mutación.

## Probe y clasificación

Cada shape key y cada dirección de cada canal de hueso de control se prueba: se mide el campo de desplazamiento de las
mallas evaluadas en el marco facial (o la rotación del ojo). Rango de prueba: el límite del rig o ±0,21 rad / ±0,1 IOD
por defecto (propuesta). El estado se captura y restaura siempre (`m3_capture`/`m3_restore`).

Clasificación con confianza, evidencia y limitaciones: `SHAPE_KEYS`, `BONES`, `HYBRID`, `CUSTOM` (casi todo por
drivers), `UNSUPPORTED` (nada deforma la cara) y `AMBIGUOUS` (menos de 3 salidas usables o más de la mitad de los
controles sin semántica segura). `UNSUPPORTED` y `AMBIGUOUS` bloquean la mutación.

Medido sobre las 18 fixtures de rig (Blender 5.2.1): ARKit, nombres propios y nombres ofuscados → SHAPE_KEYS 0,97
con 41 salidas usables; huesos → BONES 0,97 (34); híbrido → HYBRID 0,95 (41); parciales: párpados 10, mandíbula 8,
labios 21; ambiguo → AMBIGUOUS 0,833; sin controles → UNSUPPORTED 0,95. Perfil completo (probe + inspección +
calibración) 0,34–4,19 s según rig en la pasada final (varía entre ejecuciones; una anterior llegó a 7,1 s).

## Huella del rig

`fa_rig_fingerprint`: huesos, jerarquía, constraints, límites, drivers, recuentos de malla y digest de los datos de
cada shape key. Cualquier cambio entre plan y aplicación da `STALE_RIG_FINGERPRINT`.

## Límites

- Deformadores no soportados (lattice, corrective smooth, geometry nodes en la cara) no se modelan: la clasificación
  mide lo que deforman, pero no se garantiza su evaluación en todos los casos.
- Los controles deben estar a 0 en reposo (los valores escritos son absolutos).
- Lengua: ninguna fixture la tiene; la capacidad se informa UNSUPPORTED.
