"""Complex-skin support: typed UV import, comparable viewport captures. Large material graphs are
inspected through material_inspect/material_analyze_nodes with options.graph_mode="paged"."""
from __future__ import annotations

READ_ONLY = frozenset({"viewport_inspect"})
NAMES = ("mesh_import_uv_layer", "mesh_remove_imported_uv_layer", "viewport_inspect",
         "viewport_configure", "viewport_restore", "viewport_capture")


def register(tool, resources):
    from . import uv_import, viewport

    @tool("production.mesh_import_uv_layer")
    def mesh_import_uv_layer(object_name: str, source_path: str, layer_name: str, apply: bool = False,
                             *, _operation_id="") -> dict:
        """Import external per-loop UVs from an absolute .npy path (float32/float64, C-ordered, shape (loops, 2),
        no pickle) into a NEW UV map. apply=false only checks the file against the mesh. apply=true needs a
        WORKING_COPY/FIXTURE, creates a checkpoint, refuses existing names, verifies bit-exact transport and that
        geometry, topology, material indices, existing UV maps and the active/render UV are unchanged."""
        bridge, storage = resources()
        if not isinstance(apply, bool):
            raise ValueError("UV_APPLY_BOOLEAN_REQUIRED")
        if not apply:
            report = uv_import.analyze(bridge, object_name, source_path, layer_name)
            report.pop("_values", None)
            return report
        return uv_import.apply(bridge, storage, _operation_id, object_name, source_path, layer_name)

    @tool("production.mesh_remove_imported_uv_layer")
    def mesh_remove_imported_uv_layer(object_name: str, layer_name: str, *, _operation_id="") -> dict:
        """Remove a UV map that production.mesh_import_uv_layer created on this mesh. Any other UV map is refused."""
        bridge, storage = resources()
        return uv_import.remove_layer(bridge, storage, object_name, layer_name)

    @tool("production.viewport_inspect", read_only=True)
    def viewport_inspect(*, _operation_id="") -> dict:
        """Windows, areas and every VIEW_3D state (shading, HDRI, view, lens, overlays) plus the available
        Material Preview HDRIs. Background Blender reports gui=false."""
        return viewport.inspect(resources()[0])

    @tool("production.viewport_configure")
    def viewport_configure(spec: dict, *, _operation_id="") -> dict:
        """Persistently set one VIEW_3D: {area_index?, allow_area_conversion?, shading: {type, studio_light,
        rotate_z_deg, intensity, background_alpha, background_blur, use_scene_lights, use_scene_world},
        view: {perspective, orientation|rotation, target: {object|point}, distance|'fit', fit_margin, lens_mm},
        overlays, show_gizmo}. Returns the state signature and a restore record for viewport_restore."""
        bridge, storage = resources()
        return viewport.configure(bridge, storage, _operation_id, spec)

    @tool("production.viewport_restore")
    def viewport_restore(restore_record: str, *, _operation_id="") -> dict:
        """Put back the viewport state saved by viewport_configure (including a converted area's type)."""
        bridge, storage = resources()
        return viewport.restore(bridge, storage, restore_record)

    @tool("production.viewport_capture")
    def viewport_capture(spec: dict | None = None, max_size: int = 1280, keep: bool = False,
                         *, _operation_id="") -> dict:
        """Configure one VIEW_3D from spec (same schema as viewport_configure), capture it with an OpenGL
        viewport render, then restore the previous UI state unless keep=true. Equal signatures mean equal
        shading, HDRI, view and lens, so captures are comparable. Needs a GUI Blender."""
        bridge, storage = resources()
        return viewport.capture(bridge, storage, _operation_id, spec, max_size=max_size, keep=keep)
