"""Generate public tool reference from registered schemas; no Blender connection."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from blender_mcp.production.server import mcp


async def main():
    tools = await mcp.list_tools()
    groups = defaultdict(list)
    for tool in tools:
        local = tool.name.removeprefix("production.")
        groups[local.split("_")[0]].append(tool)
    lines = ["# Production tool catalog", "",
             f"Generated from the 0.9.0 server registry: **{len(tools)} tools**.", "",
             "Names below are exact MCP names. The client receives each tool's full JSON input schema. "
             "A read-only hint describes the tool annotation, not permission to skip asset or visual-review safeguards. "
             "Some measurements temporarily change and restore scene state.", "",
             "[Installation](installation.md) · [Client setup](clients.md) · [Detailed workflows](tools.md)", ""]
    for group, entries in sorted(groups.items()):
        lines.extend([f"## {group.title()} ({len(entries)})", "",
                      "| Tool | Read-only hint | Description |", "|---|---|---|"])
        for tool in sorted(entries, key=lambda item: item.name):
            description = " ".join((tool.description or "See the tool schema.").split()).replace("|", "\\|")
            annotation = tool.annotations
            readonly = annotation.readOnlyHint if annotation else None
            lines.append(f"| `{tool.name}` | {str(readonly).lower() if readonly is not None else 'unspecified'} | {description} |")
        lines.append("")
    (Path(__file__).resolve().parents[1] / "docs/tool-catalog.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Generated {len(tools)} tools")


if __name__ == "__main__":
    asyncio.run(main())
