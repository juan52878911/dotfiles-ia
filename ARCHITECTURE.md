# delega — architecture and module contracts

delega runs Claude Code **workers** (headless `claude -p`, stream-json) on behalf of an
**orchestrator** (a Claude Code session, usually on another account), on this machine or on
**nodes** reached over SSH. It enforces hard caps per job, a daily budget, account usage
windows, identity checks and a forbidden-terms scan, and gives the orchestrator a two-way
channel with each worker.

Python ≥ 3.11, **stdlib only** (the same code runs on nodes as a single `delega.pyz`).
Every module exposes `main(argv) -> int` (or `main_<command>` when a module serves several
commands) and is reached through `delega <command>` (`src/delega/__main__.py`).

## Concepts

- **Profile** = one account + its policy. Fields: `owner = "self"` (mandatory), `launcher`
  (argv prefix, default `["claude"]`), `config_dir` (exported as `CLAUDE_CONFIG_DIR`),
  `identity.expect_email` / `identity.expect_email_domain`, `repo_match`, `git`, `model`,
  `nodes`, `compute`, `forbidden_terms_file`, `forbidden_engines`, `node_bundle`, `policy.*`.
  A job is born with a profile and never changes it: **no failover or rotation between
  accounts** (`config.validate()` refuses it).
- **Node** = SSH alias + capacity: `ssh`, `max_jobs`, `min_disk_gb`, `profiles`,
  `workdir` (relative to the node's `$HOME`, default `delega`), `jobs_subdir` (default `jobs`),
  `python`, `launcher` (default `["claude"]`; the profile launcher is only used on this machine), `runner = "host" | "docker"`, `config_dirs = {profile = "path"}`, `docker_image`.
- **Compute** = an external provider a job may need (`--requires <name>`): `kind = "gcp-adc"`
  (`adc_path`, refresh-token check without printing it), `"env"` (`var`), `"command"`
  (`check = [argv]`, exit 0 = ok). `fix_hint` is printed when the check fails.
- **Project** = `[projects."owner/repo"]` matched against the `origin` remote: `policy.*`
  overrides (e.g. `budget.day_usd`), `verify = [commands]`, `branch_prefixes`.
- **Policy** keys (dotted under `[policy]`, see `config.DEFAULTS`): `job.max_min`,
  `job.max_turns`, `job.max_usd`, `job.grace_s`, `stall_s`, `answer_wait_s`,
  `resume_max_min`, `max_local_jobs`, `expected_hours`, `node.max_load_pct`,
  `node.min_disk_gb`, `node.min_mem_mb`, `node.reserve_mb_per_job`,
  `permission_mode.default`, `permission_mode.deny`, `budget.day_usd`,
  `quota.window_5h_pct`, `quota.weekly_pct`, `lint.require`, `lease.renew_s`, `lease.stale_s`.
  Read them only through `cfg.policy(key, profile, project)`.

`config.load()` → `Config` with `.locale`, `.state_dir`, `.owner_name`, `.profile(name)`,
`.profile_for(repo_dir)`, `.project_for(repo_dir)`, `.policy(key, profile, project)`,
`.node(name)`, `.nodes_for(profile)`, `.compute`, `.projects`.

## Files

### Orchestrator side: `<state_dir>/<job_id>-<repo>/`

`job_id` = `YYYYMMDD-HHMMSS-<pid>`. Contents: `brief.md`, `meta`, `run.sh` (remote), copies of
the channel files after the job ends, `fetch.log`, `waiter.pid` (pid of the local process
waiting for a remote job).

`meta` is `KEY=VALUE` lines, later wins (`util.read_meta`, `util.add_meta`). Keys:
`NODE` (node name or `local`), `CHANNEL` (local: absolute path of the channel dir; remote: path
relative to the node's `$HOME`), `BRANCH`, `BASE` (sha the job started from), `OWNER`
(orchestrator label), `PROFILE`, `ACCOUNT` (verified email), `PROJECT`, `ZONE` (comma-separated
globs), `CLAUDE_VERSION`, `DELEGA_VERSION`, `PYZ_SHA`, `BUNDLE_SHA`, `CONFIG_SHA`, `SESSION`,
`END` (epoch; present = closed). Legacy keys (`HOST`, `RJOB`, `RAMA`, `DUENO`, `FIN`) are
mapped on read.

Other state files: `sessions.jsonl` (`{session_id, node, worktree, branch, profile, job}`; also
read legacy `sesiones.tsv`: `session_id\thost\twt\tbranch`), `audit.jsonl` (append-only,
`{ts, event, job, profile, account, node, branch, base, project, owner, cost_usd, ...}` with
events `launch`, `end`, `resume`, `handoff`, `integrate`, `refused`), `limits.tsv` (quota
history; legacy `limite.tsv`), `queue.jsonl`, `.select.lock/` (node selection lock dir).

### Channel dir (where the worker runs: local job dir, or `~/<workdir>/<jobs_subdir>/<id>/` on a node)

| File | Writer → reader | Content |
|---|---|---|
| `inbox.jsonl` | orchestrator → broker | `{ts, text}` per message, injected as `[orchestrator] …` |
| `notices.jsonl` | worker → orchestrator | `{ts, kind: milestone|blocker|question, text}` |
| `progress.log` | broker | `ts\tTYPE\ttext` (types below) |
| `state.json` | broker | `{state, start, last_event, last_action, tools, turns, alerts, messages, session_id, rc, pid, model, cost_usd, cap, cap_ts, caps}` |
| `lease.json` | broker | `{pid, host, renewed}` renewed every `lease.renew_s` |
| `events.jsonl` | broker | raw stream-json |
| `result.json` | broker | last `result` event |
| `results.jsonl` | broker | `{ts, result, is_error, cost_usd}` per result |
| `summary.json` | broker at END | `{session_id, rc, model, duration_min, messages, tools, alerts, cost_usd, cap, start, end}` |
| `forbidden.txt` | launch (optional) | terms redacted from notices/progress |
| `notify` | broker | executable the worker calls: `notify milestone "<text>"` |
| `rc` | broker | exit code |

Legacy names (`buzon.jsonl`, `avisos.jsonl`, `progreso.log`, `estado.json`, `eventos.jsonl`,
`salida.json`, `resultados.jsonl`, `resumen.json`, `avisar`) are read through
`util.channel_file()`; new jobs write only the English names.

`progress.log` types: `START`, `TOOL`, `TEXT`, `MESSAGE`, `RESULT`, `END`,
`NOTICE:milestone|blocker|question`, `ALERT:stall|loop|errors|no_answer|undelivered|cap|cap_cut`.
`watch` adds `ALERT:dead` when the broker is gone without END. Legacy Spanish types
(`AVISO:hito`, `ALERTA:atasco`, `RESULTADO`, `FIN`…) must still be understood by `watch`/`ps`.

### On a node: `~/<workdir>/`

`<repo>.git` (bare; HEAD pushed as `refs/heads/delega-base/<id>`), `<jobs_subdir>/<id>/wt`
(worktree on the job branch, default `delega/<id>`), `delega.pyz`, `bundles/<profile>/`.

## Module contracts (public functions other modules may call)

- `broker`: `main(argv)` with `run <channel> <brief> -- <claude argv…>`, `notify <channel> <kind> <text>`,
  `say <channel> <text>`, `status <channel>`, `watch <channel> [label] [--all]`, `limits <channel…>`,
  `metrics <channel>`. Env: `DELEGA_JOB_MAX_MIN`, `DELEGA_JOB_MAX_TURNS`, `DELEGA_JOB_MAX_USD`,
  `DELEGA_JOB_GRACE_S`, `DELEGA_STALL_S`, `DELEGA_ANSWER_WAIT_S`, `DELEGA_LEASE_RENEW_S`,
  `DELEGA_LOCALE`. Removes `CLAUDE_*`/`ANTHROPIC_*` from the child env **except**
  `CLAUDE_CONFIG_DIR`.
- `nodes`: `probe(cfg, node, profile, requires=()) -> dict` with
  `{ok, reason, fix, email, mem_mb, disk_gb, load_pct, containers, claude_version, pyz_sha,
  python, os, compute: {name: "ok"|"expired"|"missing"|"error_…"|"unknown"}}`;
  `local_probe(cfg, profile, requires=())` same shape for this machine; `select(cfg, profile,
  requires, live_counts) -> node name | "local"`. CLI: `delega nodes [--requires X]`.
- `remote`: `ssh(node, cmd, input=None, timeout=…) -> CompletedProcess`, `scp_to(node, src, dst)`,
  `scp_from(node, src, dst)`, `ensure_pyz(cfg, node) -> sha`, `push_head(repo_dir, node, job_id)`,
  `make_worktree(node, repo, job_id, branch)`, `fetch_branch(repo_dir, node, repo, branch)`.
  SSH options: `BatchMode=yes, ConnectTimeout=5, ServerAliveInterval=30`. Never copy
  credentials (`~/.claude*`, `~/.config/gcloud`, `~/.ssh`, `.env`) — a test enforces it.
- `jobs`: `job_dirs(cfg) -> [path]`, `resolve(cfg, pattern) -> path` (`last` = newest),
  `alive(cfg, job_dir) -> bool` (END absent and: local → lease fresh or pid alive; remote →
  `waiter.pid` alive, else meta newer than 12 h), `live_counts(cfg) -> {node: n}`,
  `reconcile(cfg) -> [(job_dir, action)]` (close dead local jobs, report orphaned remote ones),
  `adopt(cfg, job_dir)`; CLI `ps [--all] [job]`, `say <job> <text>`, `watch [--all] [--from-start] [job…]`,
  `adopt <job>`. Default scope = jobs whose `OWNER` equals `DELEGA_OWNER` or the basename of `$PWD`.
- `launch`: `main(argv)` = `delega run <repo> <brief> [--node auto|local|<n>] [--profile p]
  [--requires c]… [--resume sid [--force-resume]] [--mode m] [--model m] [--worktree n]
  [--branch b] [--owner o] [--cap-min n] [--cap-usd n] [--cap-turns n] [--zone glob]…
  [--no-lint] [--over-budget] [--allow-version-skew]`. `lint_brief(text, locale) -> [missing]`,
  `zones_from_brief(text) -> [globs]`.
- `quota`: `limits_from_channels(dirs) -> [records]`, `capacity(cfg, profile) -> dict`; CLI
  `quota [--profile p] [--hours h]`.
- `spend`: `job_cost(job_dir) -> float|None`, `total(cfg, day, profile=None, project=None) -> float`;
  CLI `spend [YYYYMMDD|all] [--by profile|project|owner]`.
- `scan`: `load_terms(path) -> [str]`, `scan_text(text, terms) -> [term]`,
  `redact(text, terms) -> str`, `scan_branch(repo_dir, branch, base, terms) -> {files, commit_lines}`;
  CLI `scan <repo> <branch> [base]` / `scan --text <file>` (exit 9 on hits; never prints the matched text).
- `handoff`: CLI `handoff <job> [out.md]`.
- `queue`: CLI `queue add <repo> <brief> [--prio n] [-- run options]`, `queue ls`, `tick [--adopt]`.
- `integrate`: CLI `integrate <repo> <branch> [--into main]`: `git merge-tree --write-tree` dry run,
  then the project's `verify` commands in a temporary worktree.
- `sync`: CLI `sync [--profile p] [node…]`: tar the profile's `node_bundle` (CLAUDE.md, hooks/,
  agents/, settings.json) into the node's `~/.claude` (or its `config_dirs[profile]`). A `home/` dir in the bundle is unpacked relative to `$HOME`;
  the profile's `bundle_post_sync = ["cmd", …]` run on the node after unpacking (e.g. to merge settings.json).
  A rendered node-rules section is appended to CLAUDE.md; credential-like files are never sent; `--dry-run` lists what would go.
- `audit`: `record(cfg, event, **fields)`; CLI `audit [job|branch]`.
- `metrics`: CLI `metrics start|stop [node…]`, `metrics report [pattern] [--orchestrator transcript.jsonl]`.
- `doctor`: CLI `doctor [repo] [--profile p]`: checklist with ✓/✗ and the exact fix command.

## Tests

`python3 -m unittest discover -s tests` (stdlib unittest). `tests/fakes/claude` emits
stream-json and is driven by env vars (see the file). `tests/fakes/ssh` and `tests/fakes/scp`
run commands locally under a temporary `$HOME` so the remote flow can be tested end to end.
