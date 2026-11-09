"""Tools added after the M9 closure, listed exactly so every historical inventory test stays exact.

Each historical test asserts its own milestone's identities and then that the whole catalogue equals the
historical set plus these extensions: nothing appears or disappears silently.
"""
from blender_mcp.production.skin import NAMES as SKIN_NAMES, READ_ONLY as SKIN_READ_ONLY
from blender_mcp.production.cinematic.tools import NAMES as M10_NAMES, READ_ONLY as M10_READ_ONLY, TRANSIENT as M10_TRANSIENT

SKIN = frozenset('production.' + name for name in SKIN_NAMES)
CINEMATIC = frozenset('production.' + name for name in M10_NAMES)
EXTENSIONS = SKIN | CINEMATIC
EXTENSION_READERS = frozenset('production.' + name for name in SKIN_READ_ONLY) | frozenset(
    'production.' + name for name in M10_READ_ONLY | M10_TRANSIENT)
EXTENSION_WRITERS = EXTENSIONS - EXTENSION_READERS
