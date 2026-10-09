# Attribution and provenance

Blender Production MCP is an independent derivative of [MCP for Blender](https://github.com/ahujasid/mcp-for-blender), created by Siddharth Ahuja and contributors. It is not an official Blender Foundation, OpenAI, Anthropic or Google product.

- Upstream source: `https://github.com/ahujasid/mcp-for-blender`
- Upstream starting commit: `7cc602252386b92829bc7364e3a897253610be38`
- Upstream package version: `2.0.3`
- Addon protocol: `9`
- License: **MIT**. The original copyright notice and complete permission text are preserved in [LICENSE](LICENSE).

The production tools, guarded workflows and validation are implemented in `src/blender_mcp/production/`. The upstream connection, socket protocol, addon, general utilities and their attribution remain available in the source tree. This public repository uses an independent Git history for the Production distribution; upstream provenance is recorded above without importing its commit ancestry. This history layout does not change authorship or licensing of the reused code. Private development notes, machine configuration and local evidence archives are not distributed.

MIT permits use, modification, distribution and commercial use subject to retaining its copyright and permission notice. It supplies the software without warranty. This license covers the software; it does not change the license of assets, textures, models or third-party services you choose to use.

The `mcp-for-blender` command remains available for the bundled addon installer and upstream compatibility. Use **`blender-production`** as the MCP server command for this project. See [installation](docs/installation.md) and [privacy](PRIVACY.md).
