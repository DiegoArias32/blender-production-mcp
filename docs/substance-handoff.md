# Handoff neutral de materiales

`production.material_create_handoff` produce un contrato local `production.texture_handoff.v2`, extensión del contrato v1 de M5. No abre Painter, no crea capas/máscaras, no ejecuta bake ni exporta a Unreal.

Se indican objeto y material fuente exactos, definición PBR estructurada y, opcionalmente, la referencia vigente de un `bake_pair` M5. `apply=false` devuelve el manifiesto propuesto. `apply=true` verifica rol mutable, crea checkpoint y escribe archivos nuevos en el almacén `material_handoffs`, con SHA256 separado. No sobrescribe rutas elegidas por el llamador.

El manifiesto contiene asset ID/versión, archivo fuente, game mesh y HIGH cuando existe un par M5, fingerprint geométrico y de fuente, materiales/slots/polígonos, UV activa y tiles por asignación, semántica PBR, canales esperados con componentes y color spaces, convención de normales, supuestos de tangentes y resoluciones objetivo. Los mapas de bake esperados son configurables; no todos los materiales necesitan todos los mapas. Las listas de archivos generados/exportaciones permanecen vacías cuando no se generaron.

## Readiness sin reinterpretación

Sin un bake pair M5 validado, readiness es **UNKNOWN**. Con uno, se comprueban integridad, HIGH/LOW, firmas y vigencia mediante `bake.service.load_pair`; LOW debe ser el mismo objeto game mesh del manifiesto. Se conserva **NOT_READY**, incluyendo errores de distorsión UV, cuando ése es el resultado M5. No se acepta un argumento del llamador que diga `readiness=READY` para reemplazar la evidencia.

Generar un handoff NOT_READY permite comunicar pendientes; no autoriza un bake. Tampoco se llama a `game_mesh_prepare_package` para eludir sus gates de preparación. La validación de relaciones de materiales no sustituye la preparación de bake M5.

## Contrato de intercambio futuro

- Canales de superficie: BaseColor, Normal, Roughness, Metallic, AO, Height, Opacity y Emission según definición.
- Mapas auxiliares posibles: Curvature, Thickness, Position, WorldSpaceNormal e ID. Son expectativas, no texturas existentes.
- La convención de normales puede quedar UNKNOWN y debe resolverse antes del intercambio. MikkTSpace es una base solicitada; no certifica equivalencia con un importer futuro.
- Resoluciones 32..8192 son objetivos del contrato. La tool no crea imágenes de esas dimensiones.
- Un material puede abarcar varios UDIM; las asignaciones no exigen un material por tile ni cuatro tiles exactos.

Las rutas y firmas describen este workspace. Mover archivos requiere volver a analizar referencias y dependencias; la firma no es autenticación criptográfica frente a otro proceso local con escritura. Un futuro Substance Production MCP puede consumir este contrato de forma independiente, pero su implementación requiere autorización nueva.

## Referencia histórica del contrato M5

El contrato M5 sigue disponible. Los campos específicos de bake, cage y rayos se consultan mediante la referencia M5 conservada; M9 no los inventa ni genera nuevas exportaciones.

# Application-neutral high/low handoff contract

M5 writes references and manifests for a future texturing application. No Substance process,
service, API key, production texturing or application automation is implemented or invoked.

The package contract must identify:

```json
{
  "asset_id": "registered asset identity",
  "geometry_version": "current compact mesh fingerprint",
  "game_mesh": "candidate LOW reference",
  "high_mesh": "explicit SOURCE HIGH reference",
  "triangulated_evaluation_mesh": "deterministic evaluation copy reference",
  "UDIMs": [1001],
  "material_semantics": ["explicit material region"],
  "texture_sets": ["declared semantic sets"],
  "bake_maps_expected": ["Normal", "AO", "Curvature", "Position", "Thickness", "WorldSpaceNormal", "ID"],
  "normal_convention": "OPENGL or DIRECTX",
  "resolution_targets": {"1001": 2048},
  "cage": "candidate CAGE reference",
  "ray_distances_m": {"front": 0.02, "rear": 0.005},
  "bake_pair": "immutable workspace manifest reference",
  "QA": "concrete subsystem report references"
}
```

Numbers above are schema examples, not defaults, approved asset settings or generated evidence.
Actual package values come from verified relationships and subsystem reports. Geometry, UV,
material, weight, morph, normal, tangent, deformation and bake readiness are separate gates; no
single artistic/AAA score is produced. Unsupported morph/rig cases remain explicit blockers.

Only low-resolution NORMAL/AO diagnostic smoke maps are produced in M5. Other listed maps are
future expected outputs, not implemented capabilities. A future application can consume this
neutral contract without importing Blender-specific server/runtime code. It must choose its
own bake settings and review the declared normal convention, tiles, scales and preserved original
references. Starting that integration requires separate explicit user authorization after M5.
