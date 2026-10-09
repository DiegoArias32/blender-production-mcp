# Client configuration examples

Read the [client setup guide](../../docs/clients.md) before copying these examples.

- Replace `C:/tools/blender-production-mcp` with your installed repository's absolute path.
- Replace `C:/BlenderProduction/work` with a separate writable work directory.
- On macOS/Linux, replace the Windows paths with actual absolute paths for your computer.
- If `uv` is not found by your client, use its absolute executable path.
- Back up the existing settings file and merge **only** the `blender-production` entry. Preserve other settings and servers.
- Use one active controller for each Blender instance; port 9876 is the addon socket, not an HTTP URL.

| File | Destination |
|---|---|
| [codex.toml](codex.toml) | Existing `~/.codex/config.toml`; uses `mcp_servers` |
| [claude-desktop.json](claude-desktop.json) | Claude Desktop → Settings → Developer → Edit Config |
| [cursor.json](cursor.json) | `.cursor/mcp.json` or `~/.cursor/mcp.json` |
| [gemini-settings.json](gemini-settings.json) | Existing `~/.gemini/settings.json` or project `.gemini/settings.json` |
| [vscode-portable.json](vscode-portable.json) | Workspace `.mcp.json` or user `~/.copilot/mcp-config.json` |
| [vscode-legacy.json](vscode-legacy.json) | Existing `.vscode/mcp.json`; this format uses `servers` |

Codex and Claude Code registration commands are in the guide. These files are templates, not installers, and are not automatically loaded merely by cloning the repository.
