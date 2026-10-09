"""M5 high-level retopology tool registration."""
from __future__ import annotations
from . import service


def register(tool, resources):
    @tool('production.retopology_analyze', read_only=True)
    def retopology_analyze(object_name: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Base-mesh topology audit in world metres: manifoldness, open boundaries, degenerate/sliver triangles, normal consistency, components, valence and bounded problem IDs. No raw topology dump."""
        bridge, storage = resources()
        return service.analyze(bridge, object_name, options)

    @tool('production.retopology_analyze_density', read_only=True)
    def retopology_analyze_density(object_name: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Surface/world-unit density, grid distribution, edge-length quantiles and explicit-region curvature/deformation/visual/silhouette priorities. Density variation is measured, not universally penalized."""
        bridge, storage = resources()
        return service.analyze(bridge, object_name, options, 'density')

    @tool('production.retopology_priority_map', read_only=True)
    def retopology_priority_map(object_name: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Reusable compact priority rule set: explicit regions, curvature, protected/deformation vertices, material/sharp/boundary protection. Per-vertex reduction weights remain local and are recomputed."""
        bridge, storage = resources()
        return service.analyze(bridge, object_name, options, 'priority')

    @tool('production.retopology_budget', read_only=True)
    def retopology_budget(object_name: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Configurable triangle target/min/max and region absolute/percentage/density-weight allocations. Protected-region floor produces an explicit safe-impossibility report."""
        bridge, storage = resources()
        return service.analyze(bridge, object_name, options, 'budget')

    @tool('production.retopology_plan', read_only=True)
    def retopology_plan(object_name: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Explicit retopology strategy and budget, protected/deformation priorities, attribute/UV/weight/morph consequences and risks; apply=false. Unsupported remesh/hybrid cases report manual-required."""
        bridge, storage = resources()
        return service.analyze(bridge, object_name, options, 'plan')

    @tool('production.retopology_create_candidate')
    def retopology_create_candidate(object_name: str, source_fingerprint: str, options: dict | None = None,
                                   *, _operation_id='') -> dict:
        """Create checkpointed Candidate Game Mesh and immutable HIGH→LOW relationship using the fresh M5 signature. Copies dependencies; MASTER refused. Optional target_object is explicit, not name-matched."""
        bridge, storage = resources()
        return service.create_candidate(bridge, storage, _operation_id, object_name, source_fingerprint, options)

    @tool('production.retopology_optimize_region')
    def retopology_optimize_region(relationship: str | None = None, mode: str = 'analyze', options: dict | None = None,
                                  operation: str | None = None, *, _operation_id='') -> dict:
        """Explicit GUIDED_DECIMATION/LOCAL_RETOPOLOGY on a new child candidate: global target plus semantic/curvature priorities and zero-weight protection. Analyze→propose→preview/apply→verify/revert; frozen operation after proposal. Shape-key topology changes rejected; budget/surface/silhouette/protected checks gate acceptance."""
        bridge, storage = resources()
        return service.correction(bridge, storage, _operation_id, relationship, mode, options, operation)

    @tool('production.retopology_project_to_source')
    def retopology_project_to_source(relationship: str | None = None, mode: str = 'analyze', options: dict | None = None,
                                    operation: str | None = None, *, _operation_id='') -> dict:
        """Bounded BVH source projection on a new candidate. Distance/normal/competing-component correspondence gates reject unrelated surfaces; protected vertices remain fixed. Same immutable proposal/checkpoint cycle as optimization."""
        bridge, storage = resources()
        return service.correction(bridge, storage, _operation_id, relationship, mode, options, operation, 'project')

    @tool('production.retopology_compare_source', read_only=True)
    def retopology_compare_source(relationship: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Reproducible sampled bidirectional HIGH↔LOW mean/median/P95/P99/max world-metre deviation and normal-angle quantiles. Does not claim full Hausdorff equivalence."""
        bridge, storage = resources()
        return service.compare(bridge, storage, relationship, options)

    @tool('production.retopology_compare_silhouette', read_only=True)
    def retopology_compare_silhouette(relationship: str, options: dict | None = None, *, _operation_id='') -> dict:
        """Equivalent orthographic FRONT/SIDE/THREE_QUARTER/BACK ray masks with shared bounds: IoU, disagreement area and symmetric boundary-distance metrics in metres, with configurable gates and raster resolution."""
        bridge, storage = resources()
        return service.compare(bridge, storage, relationship, options, 'silhouette')

    @tool('production.retopology_validate', read_only=True)
    def retopology_validate(relationship: str, options: dict | None = None, *, _operation_id='') -> dict:
        """LOW topology and explicit protected-source-region verification: manifold/open boundary/degenerates/slivers/normal consistency/component/valence reports. Does not require all-quads and does not certify deformation or artistic quality."""
        bridge, storage = resources()
        return service.compare(bridge, storage, relationship, options, 'validate')
