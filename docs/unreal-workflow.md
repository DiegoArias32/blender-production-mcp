# Workflow Unreal — alcance futuro

Milestone 1 no exporta FBX ni implementa validadores Unreal. Este documento fija el procedimiento posterior; no declara esas capacidades disponibles.

Antes de exportar: validar unidades y transforms, jerarquía/root/deform bones, nombres, Actions y rango, FPS, root motion, tangentes, triangulación, morph targets y ruta de exportación. Crear checkpoint antes de cualquier ajuste de escena requerido por exportación. No arreglar escala ni aplicar transforms de un rig existente por heurística.

Preparar una copia de exportación y conservar el origen. Una exportación correcta debe verificarse por importación/inspección en Unreal de skeleton, escala, animaciones, desplazamiento y morphs. Usar perfil configurable por proyecto; el core permanecerá general.

Próximo milestone requiere autorización y versiones reales de Unreal/Blender antes de fijar parámetros FBX.
