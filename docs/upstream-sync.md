# Keeping the upstream connection maintainable

The upstream base is [ahujasid/mcp-for-blender](https://github.com/ahujasid/mcp-for-blender), commit `7cc602252386b92829bc7364e3a897253610be38`, package 2.0.3, addon protocol 9, MIT. See [attribution](../NOTICE.md).

The public Production repository has **independent Git history**. Keep the upstream remote for comparison and review, preserving its license, attribution and exact source revision. Do not merge upstream ancestry or use `--allow-unrelated-histories`: doing so would import its contributor history again.

## Fetch and inspect

Add the remote once after cloning, if it is not already present:

```sh
git remote -v
git remote add upstream https://github.com/ahujasid/mcp-for-blender.git
git fetch upstream
```

Do not run `remote add` again when `upstream` already exists. Compare the last reviewed upstream revision with its new tip, rather than using the unrelated Production HEAD as a merge base:

```sh
git status --short
git log --oneline 7cc602252386b92829bc7364e3a897253610be38..upstream/main
git diff 7cc602252386b92829bc7364e3a897253610be38 upstream/main -- addon.py src/blender_mcp/server.py src/blender_mcp/safe_mode.py src/blender_mcp/addon_manager.py pyproject.toml
```

The revision above is the initial baseline. After an accepted synchronization, use the newly recorded reviewed upstream revision for the next comparison. Keep a per-file record if an update deliberately imports only part of the upstream changes.

## Apply reviewed changes without importing history

Save local work, back up the environment and installed addon, and create a review branch. Inspect changes to the protocol, threading, safe mode, telemetry and installation. Generate a scoped patch with Git's output option, avoiding shell encoding changes:

```sh
git switch -c maintenance/upstream-review
git diff --binary --output=../upstream-core-review.patch 7cc602252386b92829bc7364e3a897253610be38 upstream/main -- addon.py src/blender_mcp/bundled/addon.py src/blender_mcp/server.py src/blender_mcp/safe_mode.py src/blender_mcp/addon_manager.py
git apply --check ../upstream-core-review.patch
```

Review the patch before applying it. A successful `--check` only establishes that it applies cleanly, not compatibility. Apply reviewed changes with `git apply ../upstream-core-review.patch`, or resolve them manually when incompatible. Commit the resulting maintenance work in this repository, naming the upstream source revisions and preserving applicable notices. Keep temporary patches outside the tracked tree.

Review other changed dependencies and utilities separately. Resolve package name/version/entry point differences deliberately. Preserve the `blender-production` command, production modules, dependency rationale, localhost boundary and MIT notice. Update the lockfile only as required by reviewed changes, then run:

```sh
uv sync --frozen --python 3.11 --group dev
uv run --frozen pytest -q
```

Protocol changes require actual Blender validation on isolated fixtures. The bridge requires an exact protocol match: a new protocol must fail explicitly until compatibility is reviewed. Never silently update the installed addon at server startup. Stop the controller before intentionally installing a reviewed addon and retain a backup.

Run relevant real-Blender probes, compare outputs and inspect previews. Record the upstream revision, affected files, addon version, runtime, tests and known limitations in the resulting change and update `docs/upstream-origin.json` as appropriate. A newer upstream release is not automatically a qualified Production release. Read the [current upstream README](https://github.com/ahujasid/mcp-for-blender#readme) instead of relying on copied installation instructions.
