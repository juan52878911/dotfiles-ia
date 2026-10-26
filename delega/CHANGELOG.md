# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Databases in kindling jobs: `[projects."owner/repo".databases.<name>]` (postgres, mariadb, mysql, redis,
  mongo, sqlite) and `delega run --db <name>|all [--db-mode]`. The dump lives only in a kindling volume
  (`delega db volume create|refresh|rm|ls`, streamed from the host or through ssh, verified by sha256,
  refresh keeps a snapshot); each job restores it inside its own VM from a frozen read-only seed sandbox,
  or clones a data golden with the databases loaded and frozen (`delega golden db`, ~1 s instead of
  ~30 s for 85 MB). `delega-db` (`src/delega/data/dbctl.sh`) installs, loads, starts and checks the engines.
- Several accounts on one kindling host: one node per account (own token and golden names); `kindling.json`
  is keyed by host; a database only reaches jobs of its project's profile (`databases.<name>.profile`).
- `delega golden build|db|ls|rm` and `delega sandbox up|exec|shell|env|freeze|thaw|save|rm|ls` on kindling.
- Workers get `context.md` in their system prompt (job id, node, VM, golden, profile, account, caps, budget,
  branch, databases and the rules for real data) and `DELEGA_JOB_ID|NODE|PROFILE|BRANCH` in their environment.
- Audit events `scan`, `db_volume`, `golden_build`, `golden_db`, `golden_rm`, `sandbox_*`; kindling `launch`/`end`
  carry `vm`, `golden` and `dbs` (volume and sha).
- `sync` bundles also carry `skills/`, `rules/`, `commands/`, `output-styles/` and the profile's `bundle_skills`.
- `golden.sh` installs `DB_ENGINES` and `delega-db` in the golden; `VM_DISK`/`VM_MEM`.
- Templates `brief-db.md`, `order-db.md` and `closing.md` (en/es) for work with real data.
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
