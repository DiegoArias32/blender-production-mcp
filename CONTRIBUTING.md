# Contributing

Contributions are welcome: bug reports with a small reproduction, client setup corrections, tests, translations and narrowly scoped production tools.

## Set up

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/), clone this repository, then run:

```sh
uv sync --frozen --python 3.11 --group dev
uv run --frozen pytest -q
```

The default pytest suite uses synthetic data, mocks and temporary directories. It does not require a running Blender. The optional `zstandard` integration can remain skipped when its dependency is unavailable. Unit results do not certify visual quality or universal Blender compatibility.

`tests/blender/` and `tests/integration/run_*.py` contain opt-in Blender probes and controlled fixture runners. Read a runner before executing it and supply your Blender executable when its default differs. Use a separate factory-startup instance with `--disable-autoexec` and an unused localhost port. Never point test runners at a production `.blend` or a Blender instance already controlled by another client. See [validation](docs/validation.md) for exact coverage and limitations.

Some historical acceptance runners depend on private, accepted material/animation evidence and cannot be reproduced from this source export alone. In particular, the M8 combined body/face case consumes accepted M7 evidence. The archive scripts preserve historical signed data; they are not general install commands. Their reusable copy/hash helpers are tested with synthetic files. Archive paths can be configured with `BLENDER_PRODUCTION_ARCHIVE_ROOT` and `BLENDER_PRODUCTION_ARCHIVE_REQUEST`; historical selections must be reviewed before use.

## Change the smallest appropriate domain

- Add production behavior under `src/blender_mcp/production/` and its domain module. Preserve the upstream core and [MIT attribution](NOTICE.md).
- Keep safety contracts: asset role checks, checkpoints, explicit plans, source preservation, stale-plan rejection and verified results.
- Use the installed Blender RNA/API for uncertain properties. Do not guess APIs or assume all Actions expose legacy `fcurves` directly.
- Test substantive behavior and rejection paths. Use small fixtures; never check in downloaded or production assets.
- Document metrics, units, thresholds, unsupported cases and real errors. Technical success does not imply artistic approval.
- Keep dependencies justified and update the lockfile when dependencies change.

## Submit

Open an issue describing the problem, or a pull request that explains the resulting behavior and relevant validation. Include the Blender version, operating system and client version when relevant. Remove machine-specific paths, secrets, generated archives and private screenshots from reports.

Contributions are distributed under the repository's MIT license. By contributing, you confirm you have the right to submit the code or documentation. Do not upload assets whose redistribution license you cannot establish.

See [AGENTS.md](AGENTS.md) for the working protocol and [upstream synchronization](docs/upstream-sync.md) before changing the inherited connection or addon.
