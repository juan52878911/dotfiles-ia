# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- `runner = "kindling"` nodes: each job runs in an ephemeral microVM cloned from a golden image
  (`nodes/kindling/golden.sh`); only HEAD's commits on top of the golden travel, the branch comes back
  as a bundle, the VM is deleted at the end, and the worker's token (`token_command`) reaches it by
  process only, never written to disk. `--resume` and `--requires` are refused on these nodes.
- Broker `DELEGA_KEEP_ENV` (legacy `ORQ_CONSERVAR`): comma list of `CLAUDE_*`/`ANTHROPIC_*` variables
  the worker keeps.
- `delega sync --tar <file>`: write the node bundle to a tar without contacting nodes.
- Operations commands: `quota`, `spend`, `scan`, `handoff`, `queue`/`tick`, `integrate`, `sync`, `audit`,
  `metrics`, `doctor`.
- Per-profile quota history (`limits.tsv`); legacy `limite.tsv` is read as the default profile.
- `sync` supports `bundle_post_sync` commands and a `home/` directory in the bundle.
- Node bootstrap scripts (`nodes/linux.sh`, `nodes/windows-wsl/`), worker image (`nodes/Dockerfile.worker`),
  resource sampler (`nodes/sample.sh`).
- Claude Code plugin (`.claude-plugin/`) with the orchestration skill and English/Spanish brief and order templates.
- CI: unittest, ruff, shellcheck and `.pyz` build on Linux and macOS, Python 3.11 and 3.13.
