"""Bounded visual QA using the current scene and upstream's validated bridge.

The caller must reserve output locations within its operation workspace. These
helpers also reject existing target files. They never create cameras or alter
materials, geometry, modifiers, poses, or the scene's chosen render engine.
"""

from .capture import capture_viewport, render_backend, render_preview

__all__ = ["capture_viewport", "render_backend", "render_preview"]
