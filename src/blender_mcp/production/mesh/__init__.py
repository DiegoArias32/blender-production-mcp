"""M3.1 mesh safety foundation (signature, candidate mesh, region locks). Not an M4 modeling feature."""
from __future__ import annotations

from .service import create_candidate, define_regions, mesh_signature, verify_candidate, verify_regions


def register(tool, resources):
    @tool("production.mesh_signature")
    def signature(object_name: str, *, _operation_id="") -> dict:
        """Compact per-section fingerprints of a mesh (topology, geometry, shape keys, vertex groups/weights, UV/UDIM, normals, materials, armature modifiers, relationships, transform, custom properties). The full signature is stored locally; no per-vertex data is returned."""
        bridge, storage = resources()
        return mesh_signature(bridge, storage, _operation_id, object_name)

    @tool("production.mesh_region_define")
    def region_define(object_name: str, regions: list[dict], *, _operation_id="") -> dict:
        """Define LOCKED/EDITABLE regions by vertex_group, face_set, mask or explicit indices bound to a topology fingerprint; stores a local manifest with the locked data. Never edits the mesh."""
        bridge, storage = resources()
        return define_regions(bridge, storage, _operation_id, object_name, regions)

    @tool("production.mesh_region_verify", read_only=True)
    def region_verify(manifest: str, object_name: str | None = None, *, _operation_id="") -> dict:
        """Verify a region manifest against the mesh (or a candidate): locked positions, shape keys and weights must stay within tolerance; topology/membership changes fail."""
        bridge, storage = resources()
        return verify_regions(bridge, storage, manifest, object_name)

    @tool("production.mesh_candidate_create", retry_safe=True)
    def candidate_create(object_name: str, source_fingerprint: str, *, _operation_id="") -> dict:
        """Create a candidate mesh (object + mesh copy, hidden from render) after checkpoint and asset guard; reports preservation per dependency. Reuses the existing candidate of the same source state instead of duplicating."""
        bridge, storage = resources()
        return create_candidate(bridge, storage, _operation_id, object_name, source_fingerprint)

    @tool("production.mesh_candidate_verify", read_only=True)
    def candidate_verify(object_name: str, candidate_name: str, regions_manifest: str | None = None, *, _operation_id="") -> dict:
        """Compare a candidate mesh with its source (section by section) and optionally verify locked regions. Accept/reject is not implemented (M4)."""
        bridge, storage = resources()
        return verify_candidate(bridge, storage, object_name, candidate_name, regions_manifest)
