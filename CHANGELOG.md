# Changelog

## Public distribution — 0.9.0

- First public source distribution with the original MIT license and upstream attribution preserved.
- Public history separated from upstream on 2026-10-08. Inherited commits were replaced by an independent distribution snapshot. The `v0.9.0` tag and downloadable packages were refreshed for this documentation/provenance change; runtime source files are unchanged. Existing clones of the original publication should use a fresh clone to obtain the independent history.
- English/Spanish README, installation guide and configurations for Codex, Claude, Gemini CLI, Cursor and VS Code.
- Portable archive paths, generated tool reference, isolated MCP installation check and Python CI matrix.
- Private development history, local evidence, production scenes and machine-specific configuration excluded.
- The entries below summarize earlier local milestones. Statements about no publication describe those historical milestones, not the current public release. See [validation](docs/validation.md) for qualification scopes and limitations.

## 0.9.0 — Milestone 9, materiales y base PBR — 2026-10-08

- Dominio modular `production/materials/`: inspección, modelo/perfiles, texturas, UV/QA, firma, candidato, grafo, render, lifecycle y handoff. 38 nuevas tools (35 requeridas + review/refine/status); 274 totales. Core upstream, MIT y dependencias runtime conservados.
- Principled Metallic/Roughness con canales/componentes explícitos, imágenes candidatas independientes, normal/bump, SSS, transmission, alpha, emission y coat. 18 perfiles propuestos, sin promesa de realismo por preset.
- Planes/checkpoints firmados, protección MASTER/roles, idempotencia, rechazo stale, hasta tres refinamientos persistentes; fuente/slots/UV/Actions/rig/texturas preservados. Cachés activas no aislables se rechazan.
- Cuatro luces comparables, 64 PNG con ocho reviews reales y cinco demos aceptadas dentro de sus límites. Handoff neutral v2 conserva UNKNOWN/NOT_READY M5 y no ejecuta Painter/Unreal ni genera mapas de producción.
- Regresión: 2.046 PASS / 1 skip heredado, 266 tests nuevos y todos los 1.781 IDs anteriores conservados. MCP: 38/38 herramientas ejercitadas, 141 llamadas; FULL histórico conserva 217/220 con FAIL de dos contratos del arnés, resueltos por suplemento 12/12 PASS. No se ocultan fallos ni se cuentan scopes solapados como tests únicos.
- Corregidos guard FIXTURE sin stamp, copia de píxeles, persistencia de imágenes fixture, restauración de RNA, UNKNOWN mal certificado, cobertura UV, conflicto de motor y normal convention de handoff. Límites de displacement, tangentes, UDIM y calidad visual explícitos.
- [resumen público de validación](docs/validation.md), [resumen público de validación](docs/validation.md), archivo local de evidencia preservado fuera de la distribución pública. No se modificaron assets reales ni se inició M10.

## 0.8.0 — Milestone 8, física y movimiento secundario — 2026-10-08

- Dominio modular `production/physics/`, 35 tools de alto nivel y 236 en total. Reutiliza infraestructura y protocolo upstream sin nuevas dependencias.
- Candidatos con checkpoint, firmas de fuente/cuerpo/cara/dependencias y configuración de pose, restauración conjunta de RNA, límites medidos, cache exclusiva y máximo tres refinamientos.
- Cloth nativo; alternativas explícitas con huesos para hair/cadenas, accesorios y regiones blandas autorizadas. Predicciones del solver separadas de QA sobre geometría evaluada.
- Preview temporal comparable en tres vistas, normal/0,25; revisión real obligatoria y bake equivalente antes de aceptar. Fixtures, pruebas de seguridad y benchmark finito; cinco demos obligatorias y spring revisados normal/0,25 en tres vistas; 37 gates funcionales calificados.
- Tres refinamientos medidos y revisados; cuarto intento bloqueado. Rechazo real del demo combinado por cuerpo oculto conservado y nueva fixture visible repetida por MCP con ACCEPT. Hair conserva limitación visual explícita; no califica groom nativo ni calidad artística universal.
- Auditoría final: captura de RNA también sobre candidatos/KEY; objetivos de refinamiento con dirección ambigua y bool/no finitos rechazados. Fallo de restauración anterior reproducido y corrección comprobada en Blender real.
- [resumen público de validación](docs/validation.md) y [resumen público de validación](docs/validation.md). Entrega local, sin push, M9 ni assets reales.

## 0.7.0 — Milestone 7, animación facial y actuación — 2026-10-05

- Entrega local en `production/milestone-7`, sin push ni publicación. Los 38 gates A–AL pasan sobre fixtures controladas y Blender 5.2.1 real; los fallos del arnés se conservan.
- 46 herramientas faciales de alto nivel: 201 en total, con los 155 contratos M1–M6 conservados. Hints MCP auditados: 7 de solo lectura, 15 transitorias y restauradas, 24 que escriben.
- Mapeo semántico medido (probe de cada shape key y canal de hueso en un marco facial normalizado; los nombres solo ajustan). Clasificación SHAPE_KEYS/BONES/HYBRID/CUSTOM/UNSUPPORTED/AMBIGUOUS. Calibración geométrica: cierre del párpado, colisión mandíbula-cuello y modelo bilineal del hueco de labios con la ganancia de cada control de boca.
- Capas lógicas (expresión, cejas, mirada, parpadeo, habla, microexpresión, cabeza/cuello) combinadas con reglas explícitas y conflictos registrados. Candidata = Action nueva por versión con slots OBJECT + KEY, evaluada por NLA; la Action del cuerpo nunca se escribe.
- Mirada con convergencia geométrica, VOR y anclaje al mundo frente al movimiento medido de la cabeza; sacadas con semilla; parpadeos por contexto con el pico en un frame entero; lip-sync solo con timing explícito (`TIMING_INPUT_REQUIRED`) y coarticulación de dominancia; mocap facial genérico con limpieza que protege parpadeos, cierres y sacadas.
- QA facial contra una línea base solo-cuerpo: párpados, penetración, sellado bilabial, cruces, intersecciones (nuevas o ampliadas), caras degeneradas y dirección medida de cada ojo. Close-ups en 4 vistas con cámara idéntica, preview temporal con reproductor normal/½/¼, revisión técnica y artística separada, máximo 3 refinamientos.
- Regresión: 1.260 passed, 1 skipped heredado (1.196 heredados + 64 nuevos). Blender real: 18 fixtures de rig (83 checks), 21 negativos (44), 5 demos aceptadas tras revisión visual (36 + 11), rendimiento 30/60 FPS × 1/5/15 s × 3 rigs (36) y MCP stdio 47/47 en un Blender propio (9877).
- Correcciones medidas durante M7: detector de ojos que aceptaba cilindros, atenuación doble de "ocultar", criterio de penetración ocular, modelo de labios lineal → bilineal con ganancias, reparto de la mirada antes del rango, numeración de versiones tras revertir un refinamiento y `__version__` del paquete production desfasado (0.4.0 → 0.7.0). [resumen público de validación](docs/validation.md). M8 no iniciado.

## 0.6.0 — Milestone 6, generación avanzada de animación corporal — 2026-10-03

- Entrega local en `production/milestone-6`, sin push ni publicación. Los 31 gates funcionales se verificaron con reportes de scopes explícitos; los fallos del arnés se conservan.
- 35 herramientas de alto nivel de brief/compatibilidad/poses/blocking/breakdowns/timing/spacing/arcos/body mechanics/root/contactos/spline/overlap/follow-through/recetas/QA/refinamiento/lifecycle; 155 totales con los 120 contratos M1–M5 conservados.
- Planner puro con procedencia, seis tipos/perfiles, semántica en metros/grados, fases/holds/contactos y capacidad. Adaptador corporal acotado, mapping corroborado por estructura/geometría/constraints; sin conversión universal de rig.
- Actions candidatas y checkpoints, proposals/fingerprints/contexto frescos, evaluación restaurable por payload, review de blocking antes de avance y lifecycle terminal sin sobrescribir fuente.
- Integridad de contexto de deformación/visual: geometría/topología/atributos/pesos/binding/modifiers/shape keys/materiales estáticos soportados; dependencias no certificadas rechazadas.
- Gates negativos de quaternions, errores devueltos sin excepción, deformación sin medición, root mode/delta/rotación/teleport/vertical y continuidad root-relative C0/C1. Blocking CONSTANT se distingue de QA continua final.
- Render temporal completo FRONT/SIDE/THREE_QUARTER, hasta dos vistas custom explícitas dentro del presupuesto, PNG/APNG/reproductor local y review enlazada al artefacto vigente.
- Máximo tres intentos de refinamiento persistentes, métricas before/after y mejora material revisada; fallos/reversiones conservan evidencia y contador.
- Documentación del dominio y procedimiento de 25 pasos en AGENTS. Regresión final: 1.196 passed, 1 skipped heredado. Cuatro demos con cinco candidatas ACCEPTED, revisión temporal normal/un cuarto, 17 fixtures, benchmark de 131 bones/10 s/60 FPS y protección MASTER real.
- Reproducibilidad: se rechazan canales activos no escritos que no sean neutrales constantes; plan/poses/holds y render deben cubrir la misma ventana. Lecturas tras aceptación y pausa del reproductor verificadas. [resumen público de validación](docs/validation.md). M7 y proyectos reales no iniciados.

## 0.5.0 — Milestone 5, retopología, game mesh y preparación de bake — 2026-10-01

- 35 herramientas de alto nivel nuevas, 120 totales; core upstream y MIT conservados.
- HIGH/LOW explícito y firmado, candidatas independientes, checkpoints y guardas atómicas de datos/archivo/rol; snapshots finales frescos y resultados inciertos sin ejecución duplicada.
- Reducción guiada y local configurable; regiones protegidas verificadas también en el interior de quads no planos; proyección/superficie/silueta muestreadas.
- Pesos/atributos por correspondencia espacial, stress QA restaurable, morphs incompatibles rechazados; normales/tris/tangentes y UV/UDIM/packing.
- Cage usa dirección barycéntrica real de Blender, readiness conserva warnings legítimos sin aceptar NOT_READY, NORMAL/AO smoke y paquete neutral local con revisión visual registrada.
- 979 tests aprobados + 1 omitido heredado, 58/37/43 checks de dominios, 67 demos, 105 MCP y carga única 2.095.104→125.000 tris medida. Su UV heredada falla stretch y conserva NOT_READY para bake.
- No proyectos reales, Substance, Unreal, M6, nuevas dependencias, push ni publicación. [resumen público de validación](docs/validation.md).

## 0.4.0 — Milestone 4, modelado y rediseño de personajes — 2026-09-29

- 20 herramientas `production.character_*`:
  - análisis medibles sin puntuación artística: inspección, silueta, proporciones HUMANOID/CREATURE/CUSTOM, asimetría, superficie, intersecciones y auditoría;
  - regiones con confianza y evidencia, y roles locked/editable/unassigned;
  - plan de solo lectura, candidato, edición que conserva la topología y QA de deformación;
  - renders equivalentes, referencias y sesión guiada.
- Reutiliza la cadena de M3.1 (roles de asset, firma de malla, candidato, regiones, propuestas inmutables, checkpoints, ledger). La edición solo toca mallas candidatas y versiona el datablock.
- Guardia anti-volteo y error `FACE_FLIPPED`, añadidos tras medir en la criatura una cara volteada 174° que la verificación daba por buena. El ensanche de hombros pasa de inflate a escala tras la revisión visual.
- Shape keys: conflicto rechazado, o delta propagada con error de offset 0,0 m. Claves absolutas y Basis desincronizado, rechazados.
- MASTER rechazado antes de todo en las 6 entradas que mutan (0 checkpoints, SHA256 igual).
- 85 herramientas; 844 tests (780 heredados + 64 nuevos); probe Blender 71/71; MCP stdio 21/21 contra un Blender propio en el puerto 9877; regresiones heredadas en verde.
- [resumen público de validación](docs/validation.md) · [resumen público de validación](docs/validation.md). M5 requiere autorización explícita.

## 0.3.1 — Milestone 3.1, remediación de la auditoría y seguridad de producción — 2026-09-26

- Corrige los cinco hallazgos HIGH de la auditoría M3, medidos en Blender real antes y después: ventana IK/FK (fuera de la ventana 0,47 m → 0,0 m), retarget T/A (19,29° → 0,0°), confianza de mapping calculada por el servidor (swap izquierda/derecha → rechazado), `pose_position=REST` bloqueado y contactos con normales invertidas (sliding 0,0 → 0,228 m confirmado).
- Guard técnico de maestro con roles de asset, idempotencia con ledger y `production.operation_status`, capacidad y tamaño de payload validados en propose, render QA solo en GPU con backend informado, hints MCP fieles y esquemas etiquetados.
- Transporte: recepción lineal y errores clasificados en una subclase de production; digest entero (16 MB en 0,34 s); tools en hilo. El core upstream no cambia.
- Base de seguridad de malla para M4 (firma, malla candidata, regiones bloqueadas), probada con un fixture de 152 352 triángulos. No es M4.
- 65 herramientas (54 conservadas y 11 nuevas); 780 tests (línea base heredada: 633); probes históricos M1/M2/M3 y demos MCP en verde.
- [resumen público de validación](docs/validation.md) · [resumen público de validación](docs/validation.md). M4 requiere autorización explícita.

## 0.3.0 — Milestone 3, Real-World Rigging, Contacts & Mocap — 2026-09-25

- 54 herramientas: 29 contratos M1/M2 conservados y 25 nuevos. Core upstream y MIT intactos.
- Semántica con confianza, compatibilidad por capacidad y matching/bake IK/FK en adaptadores explícitos.
- Contactos heel/sole/toe sobre geometría estática, locking IK/FK conservador, curvas Bézier y validación de derivadas/pops/stretch.
- Mocap con mapping aprobado, compensación de rest pose/escala/root, Action nueva, QA y cleanup acotado.
- QA geométrico de deformaciones, stress poses restaurables y encuadres LOW_ANGLE/FOOT.
- Protección de huesos ajenos completa, verificación de fixtures tras reabrir, 633 tests y 160 checks Blender M3.
- Tres demos visuales, 128 comprobaciones MCP, originales/checkpoints verificados.
- [resumen público de validación](docs/validation.md). M4 requiere autorización explícita.

## 0.2.0 — Milestone 2, Advanced Animation Correction & QA

- 29 tools conservando los 11 contratos M1 y upstream separado.
- Propuestas inmutables, snapshots de Action/contexto, checkpoints y corrección en copias con verify/revert.
- Gestión de Actions, contactos, control IK/FK, F-Curves, root motion, balance aproximado, motion quality y plan.
- Sliding IK, quaternions, cleanup con tolerancias, retiming y preparación horizontal in-place de root explícito.
- QA FRONT/SIDE/THREE_QUARTER y before/after equivalente con restauración comprobada y artefactos PNG.
- Catálogo procedural de 14 escenarios, tests positivos/adversariales y distinción entre verificación numérica y visual.
- Resultados finales, rendimiento y límites: [resumen público de validación](docs/validation.md). No iniciar M3 sin autorización.

## 0.1.0 — Milestone 1, 2026-09-22

- Fork local derivado de mcp-for-blender 2.0.3 / 7cc602252386b92829bc7364e3a897253610be38, MIT conservada.
- Entry point production, módulos common/checkpoint/inspect/rig/animation/qa y 11 herramientas de alto nivel.
- Checkpoints con intención, manifiesto SHA256, restore dry-run/copia de trabajo y diff de metadatos.
- Auditoría, rig/armature, Actions modernas, contactos y sliding físico, discontinuidades quaternion.
- Viewport PNG y previews con restauración de estado, errores explícitos y logs locales.
- Fixtures sintéticas y tests unitarios/integración/Blender; consultar evidencia actual en docs/test-results.md.

Esta versión inicial no incluía funciones M2; se conserva como registro histórico.

## Cierre formal de Milestone 1 — 2026-09-22

- Verificación visual confirmada por el usuario; pendiente inicial cerrado.
- Estado fixture final preservado mediante copia del último archivo de prueba verificado y SHA256.
- Reporte y evidencia de cierre, documentación enlazada y respaldo de entrega en Obsidian.
- Sin cambios funcionales, proyectos reales modificados ni inicio de M2.
