"""Start the upstream add-on inside a Blender launched *for this test* on a non-default port.

Usage (GUI Blender, opening a generated fixture):
    blender --factory-startup <fixture.blend> --python tests/integration/m4_own_blender_start.py -- 9877

Why a separate port: the user's own Blender and MCP controller may already own 9876 (lease). This
instance never touches it; the production server is then pointed at this port with BLENDER_PORT.
The add-on is loaded from the repository file, not installed into the user's preferences.
"""
import importlib.util
from pathlib import Path
import sys

import bpy

PORT = int(sys.argv[sys.argv.index('--') + 1]) if '--' in sys.argv else 9877
if PORT == 9876:
    raise SystemExit('Refusing 9876: that port belongs to the user\'s Blender/MCP controller')
ADDON = Path(__file__).resolve().parents[2] / 'addon.py'
spec = importlib.util.spec_from_file_location('blendermcp_m4_test_addon', ADDON)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.register()
for scene in bpy.data.scenes:
    # The auto-start timer reads the scene port when it fires, after this script.
    scene.blendermcp_port = PORT
print('M4_OWN_BLENDER_ADDON_PORT=' + str(PORT), flush=True)
