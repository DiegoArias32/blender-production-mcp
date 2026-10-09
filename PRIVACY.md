# Privacy and local data

The `blender-production` entry point forces `DISABLE_TELEMETRY=true` and `BLENDER_MCP_SAFE_MODE=1`. Its production tool registry does not include external asset downloads, paid services or third-party API integrations. No API key is required by this server.

Blender runs on your computer. Your MCP client launches a local process over standard input/output; that process talks to the Blender addon over a local socket. Keep `BLENDER_HOST=localhost`, bind the addon to localhost, and do not expose port 9876 to a network. The inherited socket protocol has no authentication.

**Local execution does not mean your AI client is offline.** Tool requests and responses may be sent to the model provider selected in Codex, Claude, Gemini or another client. Responses can include scene metadata, object names, error messages, paths, summaries and preview images. Review your client's own privacy settings and data policy before using confidential assets.

The production workflow intentionally keeps local checkpoints, copies, plans, logs, images and operation records. Set `BLENDER_PRODUCTION_WORKDIR` to a private absolute directory you control. Do not publish that directory, real `.blend` files or generated evidence without reviewing their contents. Cleanup is explicit; source assets and audit evidence must not be removed merely to tidy a workspace.

The inherited addon has its own **Allow Telemetry** preference. Leave it off. Configure `DISABLE_TELEMETRY=true` in every client. This policy describes the `blender-production` entry point; launching the separate upstream `mcp-for-blender` server uses upstream behavior and its [privacy policy](https://github.com/ahujasid/mcp-for-blender/blob/main/TERMS_AND_CONDITIONS.md).

If you submit a public issue, remove personal file paths, credentials and confidential project names. Prefer a minimal synthetic fixture and a shortened error report.
