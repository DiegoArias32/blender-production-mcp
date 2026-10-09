# Security policy

## Supported release

The public 0.9.x line receives reviewed fixes. Blender runtime qualification and known limitations are described in [validation](docs/validation.md); newer upstream code is not automatically imported.

## Report a vulnerability privately

Use GitHub's **Security → Report a vulnerability** for this repository:

https://github.com/DiegoArias32/blender-production-mcp/security/advisories/new

Include the affected version, relevant tool, minimal procedural reproduction, expected behavior and impact. Remove credentials, personal paths and production assets. Do not post private scenes or exploit details in a public issue.

For ordinary bugs, open an issue with a minimal fixture and redacted logs. No response-time or commercial support guarantee is offered.

## Operating boundaries

- Run on the same trusted machine as Blender, with the addon bound to loopback.
- Keep safe mode and telemetry settings as documented.
- Use one controller per Blender instance and explicit working copies.
- Keep your MCP client's normal permissions and approval controls enabled.
- Treat untrusted `.blend` files, scripts and linked resources as untrusted input.
- Safe mode is an AST guardrail, not a sandbox. See the detailed [security design](docs/security.md).
