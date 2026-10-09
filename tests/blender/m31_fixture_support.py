"""M3.1 asset guard support for real-Blender probes.

Since AUD-014, persistent mutation is refused on any .blend without an explicit mutable role. Probes
generate their own fixtures, so they register them explicitly as FIXTURE, limited to the probe's own
output folder. Roles are never inferred from file names, and the guard itself is not relaxed.
"""


def register_open_fixture(bridge, storage, allowed_root):
    from blender_mcp.production.common.asset_safety import CURRENT_FILE, register_fixture
    path = bridge.run(CURRENT_FILE, {})['filepath']
    if not path:
        return None  # an unsaved session is TEMPORARY and already mutable
    return register_fixture(bridge, storage, path, allowed_roots=[str(allowed_root)])
