# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Operations commands: `quota`, `spend`, `scan`, `handoff`, `queue`/`tick`, `integrate`, `sync`, `audit`,
  `metrics`, `doctor`.
- Per-profile quota history (`limits.tsv`); legacy `limite.tsv` is read as the default profile.
- `sync` supports `bundle_post_sync` commands and a `home/` directory in the bundle.
- Node bootstrap scripts (`nodes/linux.sh`, `nodes/windows-wsl/`), worker image (`nodes/Dockerfile.worker`),
  resource sampler (`nodes/sample.sh`).
- Claude Code plugin (`.claude-plugin/`) with the orchestration skill and English/Spanish brief and order templates.
- CI: unittest, ruff, shellcheck and `.pyz` build on Linux and macOS, Python 3.11 and 3.13.
