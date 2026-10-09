# Connect your AI assistant

Blender Production MCP runs on the same computer as Blender. Your assistant starts the Python server and communicates with it over **stdio**. The server then talks to the **MCP for Blender** addon over a local socket.

```text
Your assistant ── stdio ── blender-production ── localhost:9876 ── Blender addon
```

**Port 9876 is not an HTTP MCP endpoint.** Do not enter `http://localhost:9876/mcp` in a connector dialog. This guide is for local desktop and command-line clients, not their browser-only chat websites.

## Before connecting

1. Complete the repository installation in the [README](../README.md), including `uv sync` and the Blender addon.
2. Enable **MCP for Blender** in Blender, open its sidebar panel, and start the addon server on port **9876**.
3. Open a disposable scene for your first connection test.
4. Choose **one** assistant to control that Blender instance. Stop or disable its MCP connection before switching to another assistant. Keep the upstream `mcp-for-blender` controller disabled while using `blender-production` against the same instance.
5. Back up an existing client configuration before editing it. Add only the `blender-production` entry and preserve every other setting and server.

The examples use these paths:

| Value | Windows example | macOS / Linux example |
|---|---|---|
| Repository | `C:/tools/blender-production-mcp` | `/home/you/tools/blender-production-mcp` |
| Writable work directory | `C:/BlenderProduction/work` | `/home/you/BlenderProduction/work` |
| Launcher | `uv` | `uv` |

Replace the example paths with your actual **absolute** paths. On macOS, a home directory normally starts with `/Users/you`, not `/home/you`. Choose a work directory outside your production assets. JSON accepts the forward slashes shown for Windows; if you use backslashes, escape each as `\\`.

If a desktop app cannot find `uv`, use the full executable path in `command`. Find it with `(Get-Command uv).Source` in PowerShell or `command -v uv` on macOS/Linux. Close and reopen the app after changing PATH.

The launcher is always:

```text
uv --directory ABSOLUTE_REPOSITORY_PATH run --frozen blender-production
```

The client starts this process for you. Running it alone in a terminal can appear to wait without output because it is waiting for MCP messages. There is no website to open.

## Client configuration at a glance

| Client | Transport | Configuration | Verify connection |
|---|---|---|---|
| Codex | stdio | [`codex mcp add` or `config.toml`](#codex) | `codex mcp list`, `/mcp` |
| Claude Code | stdio | `claude mcp add`, shown below | `claude mcp get blender-production`, `/mcp` |
| Claude Desktop | stdio | Developer settings → Edit Config | Developer settings / connector tools |
| Gemini CLI | stdio | `gemini mcp add`, or `~/.gemini/settings.json` | `gemini mcp list`, `/mcp list` |
| Cursor | stdio | `.cursor/mcp.json` or `~/.cursor/mcp.json` | Customize → MCP server |
| VS Code / GitHub Copilot | stdio | Portable `.mcp.json` or `~/.copilot/mcp-config.json` | `MCP: List Servers` |

The entry name is **`blender-production`**. The Blender addon still appears as **MCP for Blender**: these names refer to different parts of the connection.

## Shared JSON configuration

Claude Desktop, Cursor, Gemini CLI, and the portable VS Code configuration use a top-level `mcpServers` object. This is a complete example for an otherwise empty file:

```json
{
  "mcpServers": {
    "blender-production": {
      "command": "uv",
      "args": [
        "--directory",
        "C:/tools/blender-production-mcp",
        "run",
        "--frozen",
        "blender-production"
      ],
      "env": {
        "BLENDER_HOST": "localhost",
        "BLENDER_PORT": "9876",
        "BLENDER_MCP_SAFE_MODE": "1",
        "DISABLE_TELEMETRY": "true",
        "BLENDER_PRODUCTION_WORKDIR": "C:/BlenderProduction/work",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

For an existing file, merge only the `blender-production` member into its existing `mcpServers` object. Do not append a second top-level JSON document or duplicate `mcpServers`. Strict JSON does not allow comments or trailing commas.

These environment variables select the local addon, enable the upstream code guard, disable upstream telemetry, and locate production artifacts. Safe mode is a guardrail, not an operating-system sandbox: review operations and use working copies and checkpoints.

## Codex

Run this as **one line** after replacing both paths:

```text
codex mcp add blender-production --env BLENDER_HOST=localhost --env BLENDER_PORT=9876 --env BLENDER_MCP_SAFE_MODE=1 --env DISABLE_TELEMETRY=true --env "BLENDER_PRODUCTION_WORKDIR=C:/BlenderProduction/work" --env PYTHONIOENCODING=utf-8 -- uv --directory "C:/tools/blender-production-mcp" run --frozen blender-production
```

Alternatively, merge [codex.toml](../examples/clients/codex.toml) into `~/.codex/config.toml` (normally `%USERPROFILE%\.codex\config.toml` on Windows):

```toml
[mcp_servers.blender-production]
command = "uv"
args = ["--directory", "C:/tools/blender-production-mcp", "run", "--frozen", "blender-production"]
startup_timeout_sec = 30
tool_timeout_sec = 300

[mcp_servers.blender-production.env]
BLENDER_HOST = "localhost"
BLENDER_PORT = "9876"
BLENDER_MCP_SAFE_MODE = "1"
DISABLE_TELEMETRY = "true"
BLENDER_PRODUCTION_WORKDIR = "C:/BlenderProduction/work"
PYTHONIOENCODING = "utf-8"
```

Use either registration method. If the table already exists, update that table rather than duplicating it. The CLI command sets the launcher and environment; add the two optional timeout values to the existing server table if desired. Preserve every unrelated setting.

Verify with `codex mcp list`, or `/mcp` in the Codex terminal interface. Reload the MCP connection or restart the client after configuration changes.

The example allows 30 seconds for startup and 300 seconds per tool. These are configurable waiting limits, not a guarantee that a long render will finish or that a timed-out operation has stopped. After a timeout, inspect operation status and Blender before retrying a mutation; do not automatically repeat it under a new operation ID.

[Official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Claude Code

With the repository already installed, run this as **one line** after replacing both paths:

```text
claude mcp add --scope user --env BLENDER_HOST=localhost --env BLENDER_PORT=9876 --env BLENDER_MCP_SAFE_MODE=1 --env DISABLE_TELEMETRY=true --env "BLENDER_PRODUCTION_WORKDIR=C:/BlenderProduction/work" --env PYTHONIOENCODING=utf-8 --transport stdio blender-production -- uv --directory "C:/tools/blender-production-mcp" run --frozen blender-production
```

`--scope user` makes the server available across projects. The `--` separates Claude Code options from the launcher command. Keep `--transport stdio` between the final `--env` and server name so the name is not consumed as another environment assignment.

Verify:

```text
claude mcp get blender-production
claude mcp list
```

Inside Claude Code, use `/mcp` to inspect the server. A project-scoped configuration can require workspace trust and approval. Retain normal tool approvals; no permission-bypass option is necessary.

Claude Code and Claude Desktop have separate configurations. Registering one does not automatically configure the other. [Official Claude Code reference](https://code.claude.com/docs/en/mcp).

## Claude Desktop

Open **Settings → Developer → Edit Config**, then merge the shared JSON entry. The usual locations are:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Opening the file through the application avoids guessing the location for a particular installation. Completely quit Claude Desktop and reopen it to load the change. Inspect the server in Developer settings or its available connector tools.

Copyable example: [claude-desktop.json](../examples/clients/claude-desktop.json). [Official local-server setup](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## Gemini CLI

Run this as **one line**, after replacing both paths:

```text
gemini mcp add --scope user --transport stdio -e BLENDER_HOST=localhost -e BLENDER_PORT=9876 -e BLENDER_MCP_SAFE_MODE=1 -e DISABLE_TELEMETRY=true -e "BLENDER_PRODUCTION_WORKDIR=C:/BlenderProduction/work" -e PYTHONIOENCODING=utf-8 blender-production uv -- --directory "C:/tools/blender-production-mcp" run --frozen blender-production
```

The `--` goes **after `uv`** here. Gemini CLI forwards the following arguments to the launcher. `--scope user` writes to `~/.gemini/settings.json`; its default scope is project. Alternatively, merge [gemini-settings.json](../examples/clients/gemini-settings.json) into your existing settings.

Verify with `gemini mcp list`, or `/mcp list` inside Gemini CLI. A stdio server can appear disconnected when the current folder is untrusted. Review the folder and configuration before accepting the client's trust step. Do not add `--trust` to the registration command: it bypasses tool confirmation prompts.

This configures **Gemini CLI**, not the Gemini website. [Official Gemini CLI reference](https://geminicli.com/docs/tools/mcp-server/). Argument forwarding is also covered by the project's [command implementation](https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/commands/mcp/add.ts) and [parser tests](https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/commands/mcp/add.test.ts).

## Cursor

Merge [cursor.json](../examples/clients/cursor.json) into either:

- `.cursor/mcp.json` in your project, for that project only; or
- `~/.cursor/mcp.json`, for your user account.

Use **Customize** to inspect and enable the server. Review tool requests using the client's normal approval flow. The example uses fixed absolute paths so the server does not depend on which project Cursor currently has open. [Official Cursor MCP documentation](https://cursor.com/docs/mcp).

## VS Code / GitHub Copilot

The current documentation recommends portable configuration for new registrations:

- Workspace: `.mcp.json` at the project root.
- User: `~/.copilot/mcp-config.json`, or `$COPILOT_HOME/mcp-config.json` when that variable is set.

Both use `mcpServers`, so merge [vscode-portable.json](../examples/clients/vscode-portable.json). You can also run **MCP: Add Server** from the Command Palette and choose a portable destination. Use **MCP: List Servers** to inspect the server, start or stop it, and view output. Review workspace configuration before trusting the workspace.

For an existing `.vscode/mcp.json` configuration, use [vscode-legacy.json](../examples/clients/vscode-legacy.json) instead: this format uses **`servers`**, not `mcpServers`. It remains supported for compatibility, but the current guide prefers portable destinations for new configurations. [Official VS Code documentation](https://code.visualstudio.com/docs/agent-customization/mcp-servers).

## First connection test

Use a disposable Blender scene and ask:

> Run `production.scene_audit` and summarize the active scene, Blender version, objects, units, FPS, and any warnings. Do not modify the scene.

Check the returned data against Blender. A client listing the server as connected confirms the stdio connection; a successful Blender-backed tool call also checks the addon connection.

For later editing, identify the target asset, create a working copy and checkpoint, inspect the tool's plan or dry run, then evaluate the measured and visual result. Do not begin with changes to a production master file.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `uv` cannot be found | Use its absolute executable path; reopen the client after changing PATH. |
| Repository or Python environment missing | Correct the absolute repository path and complete `uv sync` there. |
| Server exits immediately | Open client MCP logs; check the actual error, paths, and environment rather than disabling safe mode. |
| Tools appear, but Blender calls fail | Start **MCP for Blender** inside Blender and match its port to `BLENDER_PORT`. |
| Port already in use | Identify the other listener/controller. Only one addon can bind a given address and port. Stop the conflicting test server or deliberately configure a separate Blender instance and port. |
| Connection appears to go to the wrong addon | Check that the active listener is **MCP for Blender**, not another extension using port 9876. |
| Gemini reports disconnected | Inspect `/mcp list` and the current folder's trust state. |
| A preview file is not displayed inline | Open the returned local artifact path. Client image-display capabilities differ. |
| Switching between assistants | Stop or disable the previous client's server before the next one controls the same Blender instance. |

## Verification scope

The configuration formats and commands in this guide were checked against the linked official documentation for this release. The project has local MCP protocol and Blender fixture evidence described in its release documentation. **These examples do not constitute an end-to-end certification of every client, operating system, model, or client version.**

The repository supplies a local server; it does not supply an AI subscription, model credentials, or an HTTP hosting service. Install from this GitHub source checkout as documented. This guide does not assume a package named `blender-production-mcp` has been published to PyPI.
