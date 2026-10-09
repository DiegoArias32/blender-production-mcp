# Seguridad de assets: guard técnico de maestro (AUD-014)

Código: `src/blender_mcp/production/common/asset_safety.py`, `common/lifecycle.py`. Pruebas:
`tests/unit/test_m31_safety.py`, `tests/unit/test_m31_lifecycle.py` y, en Blender real, todos los
probes con fixture registrado.

## Antes y ahora

En v0.3.0, "no tocar el maestro" era una regla escrita. Nada impedía mutar el `.blend` abierto: si el
usuario guardaba, se escribían la Action cambiada y los candidatos. En v0.3.1, toda mutación
persistente comprueba el rol del archivo abierto **después del preflight y antes de crear el
checkpoint**. Si se rechaza, no se crea ningún checkpoint ni ningún candidato.

## Roles

| Rol | Cómo se obtiene | Mutable |
|---|---|---|
| MASTER | `production.asset_register_master(confirm_master=true)` (guarda SHA256 y tamaño) | no: `MASTER_ASSET_READ_ONLY` |
| WORKING_COPY | `production.asset_create_working_copy` o `production.restore_checkpoint(dry_run=false)`: copia en `<workdir>/working`, abierta con scripts desactivados y marcada con su `asset_id` en cada escena | sí |
| FIXTURE | `production.asset_register_fixture(path)`, solo bajo el temp del sistema, `<workdir>/fixtures` o `BLENDER_PRODUCTION_FIXTURE_ROOTS` | sí |
| TEMPORARY | sesión sin guardar (no hay archivo que sobrescribir) | sí |
| UNREGISTERED | cualquier otro archivo, incluidos todos los del usuario | no: `ASSET_ROLE_UNREGISTERED` |

El rol **nunca** se deduce del nombre del archivo. Una copia de trabajo movida o copiada conserva el
sello pero no la ruta registrada, y se rechaza con `ASSET_STAMP_MISMATCH`. El registro
(`<workdir>/assets/registry.json`) se reescribe de forma atómica.

Flujo para un asset real (futuro; en M3.1 solo se probó con fixtures):

```
MASTER → asset_register_master → asset_create_working_copy → mutar la copia
```

## Qué cubre el guard

Apply, preview y revert de todas las correcciones M2 y M3, `animation_create/duplicate/restore_action`
con `dry_run=false`, `deformation_test_pose` y `mesh_candidate_create`. Las herramientas de lectura,
las propuestas, los checkpoints y los renders QA no modifican el archivo del usuario, así que no
pasan por el guard (sus cambios transitorios se restauran y se declaran en los hints, ver
`docs/tools.md`).

## Ciclo de vida de artefactos

`production.cleanup_artifacts` hace inventario de `checkpoints/`, `qa/`, `operations/` y `working/`
con `created_at`, `operation_id`, `source`, `status`, `retain_reason` y `eligible_for_cleanup`.
Con `dry_run=true` (por defecto) no borra nada. Siempre retiene:

- los puntos de recuperación de operaciones RUNNING o UNCERTAIN del ledger;
- las propuestas cuyo apply está SUCCEEDED (su candidato está vivo);
- las carpetas que referencia el registro de assets;
- las `keep_last` más recientes por categoría (5 por defecto) y todo lo más joven que `min_age_days`
  (7 por defecto);
- lo que no tiene `reservation.json` legible.

Con `dry_run=false` borra solo lo elegible en ese momento, solo dentro del workspace, y registra cada
borrado en `cleanup.jsonl`. Las Actions candidatas de Blender solo se informan: borrar data-blocks es
una mutación que corresponde a una transacción con checkpoint.

Crecimiento medido: `test-artifacts` ocupa 638 MB (las mismas cifras que dio la auditoría). Los probes
de M3.1 escriben sus `.blend` en `%TEMP%\bpm31` y promueven solo JSON e imágenes pequeñas de revisión
(`m3.1-*`: 188 KB en regresiones de auditoría). No se borró ninguna evidencia histórica.
