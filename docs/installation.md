# Install Blender Production MCP

[README](../README.md) · [Client configuration](clients.md) · [Validation](validation.md)

## Requirements and tested versions

- Blender installed locally. The qualified Blender runtime is **5.2.1 LTS on Windows 11**. Other versions are unqualified, not assumed compatible.
- Git and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Python **3.11 recommended**, managed by uv. Do not install the MCP dependencies into Blender's embedded Python.
- A local client supporting MCP stdio: Codex, Claude Desktop/Code, Gemini CLI, Cursor or VS Code.
- A writable directory for checkpoints, plans, previews and logs, with enough free space for copies of your scenes.

The package declares Python >=3.10; the Blender qualification used server Python 3.11.16. The automated test matrix is a separate statement from Blender runtime support.

## 1. Install uv if needed

Check `uv --version` and `uvx --version`. If missing, use Astral's [official standalone installer](https://docs.astral.sh/uv/getting-started/installation/), then reopen your terminal.

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS/Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

These commands execute the official installer; the linked documentation also explains how to inspect it first. No `pip install uv` step is needed.

## 2. Clone and install the tagged source

```sh
git clone https://github.com/DiegoArias32/blender-production-mcp.git
cd blender-production-mcp
git checkout v0.9.0
uv sync --frozen --python 3.11
```

The tag pins the source; `uv.lock` pins resolved dependencies. uv creates `.venv` inside the checkout. The initial sync may download Python and the locked dependencies. Subsequent client launches use that environment.

Use a stable folder, for example `C:/tools/blender-production-mcp`. Paths with spaces work when quoted in commands or represented as one JSON argument. Do not move the folder without updating your client configuration.

There is no public PyPI package release of this fork in this delivery. `uvx mcp-for-blender` starts the upstream product; it does not start this fork's production tools.

## 3. Install the matching Blender addon

From the checkout:

```sh
uv run --frozen mcp-for-blender addon-paths
uv run --frozen mcp-for-blender install-addon
```

This invokes the upstream installer **bundled in this checkout**, keeping addon and bridge protocol aligned. It prints the destination of `blender_mcp.py` and attempts a `.bak` backup when replacing a file; backup failure does not stop the upstream installer. Review the printed version/path and back up custom addon edits before replacing an existing installation. Do not mix this pinned server with an unreviewed latest upstream addon.

If detection fails, select a destination explicitly. PowerShell:

```powershell
$env:BLENDERMCP_ADDONS_DIR = 'C:/path/to/Blender/version/scripts/addons'
uv run --frozen mcp-for-blender install-addon
```

Use the real version-specific user addon directory for your installation. Alternatively, use Blender's **Preferences → Add-ons → Install from Disk** action and select the repository's `addon.py`. The menu label can vary across Blender versions.

1. Open Blender; use a disposable scene for the first connection.
2. Open **Edit → Preferences → Add-ons**, search **MCP for Blender**, and enable it.
3. Leave **Allow Telemetry** unchecked in the addon preferences.
4. In the 3D View, press **N**, select **MCP for Blender**, and start its server if needed.
5. Confirm **Connected on port 9876** and keep the host on loopback.

The addon UI keeps its upstream name. Our client-side server is named **blender-production**. A separate addon named **MCP** by **Blender Lab** is a different project and is not this connection. Two addons cannot safely share the same listening address and port; stop the other listener or use a deliberately separate instance/port.

The [current upstream README](https://github.com/ahujasid/mcp-for-blender#installing-the-blender-addon) describes addon installation. This fork uses its pinned bundled installer rather than automatically upgrading to current upstream.

## 4. Configure one client

Follow the [client guide](clients.md). Every configuration uses:

| Variable | Value | Purpose |
|---|---|---|
| `BLENDER_HOST` | `localhost` | Blender addon on the same computer |
| `BLENDER_PORT` | `9876` | Match the addon port |
| `BLENDER_MCP_SAFE_MODE` | `1` | Enable the upstream validator; production enforces it |
| `DISABLE_TELEMETRY` | `true` | Disable upstream server telemetry; production enforces it |
| `BLENDER_PRODUCTION_WORKDIR` | Absolute writable directory | Local checkpoints and operation evidence |
| `PYTHONIOENCODING` | `utf-8` | Consistent Python stream encoding |

The client launches `uv --directory ABSOLUTE_REPO_PATH run --frozen blender-production`. You do not need to leave a separate terminal server running. **Do not use `http://localhost:9876/mcp`**: the addon socket is not HTTP.

Back up your client configuration before editing it. Add only the new entry, preserving all other settings. Configure as many clients as you like, but start only one controller for a given Blender instance. If you switch clients, stop/disconnect the previous server first.

## 5. Verify in two stages

From the checkout, first verify the Python package and MCP stdio transport:

```sh
uv run --frozen python scripts/verify_install.py
```

Expected: version `0.9.0`, **274** tools, and a successful MCP initialization. This check does not contact Blender, change scenes or require an API key.

Next, connect your chosen client and ask:

> Call production.scene_audit on the current disposable scene. Report Blender version, scene name, object counts, units, FPS and frame range. Do not modify the scene.

Inspect the actual tool result. A client showing "connected" only proves it launched the Python process. A successful scene audit verifies the complete **client → server → addon → Blender** path.

For mutation workflows, follow the asset registration/working-copy procedures in [asset safety](asset-safety.md). Do not label a production master as a fixture to bypass protection.

## Troubleshooting

| Symptom | Check |
|---|---|
| Client cannot find `uv` | Restart the application after installation, or use the absolute `uv.exe`/`uv` path in its configuration. |
| Process starts but Blender is unavailable | Enable the correct addon, start its listener, and match host/port. |
| Running the server in a terminal seems to hang | Normal: it waits for MCP messages on stdin. Let the client launch it. |
| Address/port already in use | Identify the listener. Stop the competing addon/server rather than launching both. |
| Protocol mismatch | Use the matching addon bundled with the tagged fork. Do not silently upgrade one side. |
| Another controller is active | Disconnect the previous client. Inspect processes and lease ownership; do not delete a live lease to force entry. |
| Permission or sandbox error | Allow the chosen client access to the checkout, work directory and local Blender connection. Do not globally disable its safeguards. |
| An operation times out | Inspect `production.operation_status`, Blender and the recovery checkpoint. The operation may still be running. |
| Workflows reject a rig/material | Read the structured reason and capability report. Compatibility rejections are not automatically bugs. |

## Updating and uninstalling

Stop the client, preserve your work directory, fetch the next reviewed release, inspect its notes, check out its tag and run `uv sync --frozen --python 3.11`. Update the bundled addon only when required by that release. Keep rollback copies of custom addon/config changes.

To disconnect, disable/remove only `blender-production` in your client configuration. Disable the addon only if no other workflow needs it. Checkpoints and source scenes are your files; do not delete them as part of routine cleanup. See [upstream sync](upstream-sync.md) for maintainer updates.
