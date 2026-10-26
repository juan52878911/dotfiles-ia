# delega

Run Claude Code **workers** on your other accounts and machines, with hard caps, a daily budget and
a two-way channel — from a Claude Code session that only orchestrates.

delega is a small, dependency-free Python tool (3.11+, standard library only). One orchestrator
session writes briefs and reviews results; headless `claude -p` workers do the heavy reading,
coding and testing on a profile's account, locally or on SSH nodes.

## Why

- **Cost control that is enforced, not requested.** Every job has caps (minutes, assistant messages,
  USD). When one is hit the worker is asked to wrap up and, after a grace period, cut.
- **A budget per day and per profile**, and a quota model that reads the account's 5-hour and weekly
  usage windows and tells you how many workers fit before they fill up.
- **A channel in both directions.** The orchestrator `say`s things to a running worker; the worker
  sends `milestone`, `blocker` and `question` notices; `watch` turns it all into one line per event.
- **Identity and data hygiene.** Each launch verifies the signed-in account matches the profile, and
  branches coming back are scanned for forbidden terms (client names, …) without ever printing them.
- **Provenance.** An append-only audit log ties every branch to its account, node, base and cost.

## Install

As a Claude Code plugin (gives you the orchestration skill):

```
/plugin marketplace add <this repository>
/plugin install delega@delega
```

From a checkout (gives you the CLI; nodes receive a single `delega.pyz` automatically):

```
git clone <this repository> && cd delega
pip install .            # or: PYTHONPATH=src python3 -m delega …, or ./bin/delega
python3 -c "from delega.util import build_pyz; build_pyz('dist/delega.pyz')"   # the artefact nodes run
```

## Quick start

1. `mkdir -p ~/.config/delega && cp config.example.toml ~/.config/delega/config.toml` and edit: one
   profile per account you own, its nodes, its policy.
2. Sign in the profile's account once: `CLAUDE_CONFIG_DIR=~/.claude-work claude /login`.
3. `delega doctor` — a ✓/✗ checklist with the exact command that fixes each ✗.
4. Write a brief (`skills/delega/templates/en/brief.md`) and launch:
   `delega run ~/code/app brief.md --node auto --cap-min 60`.
5. Watch and steer: `delega ps`, `delega watch`, `delega say <job> "use the v2 API"`.
6. Review: `delega scan`, `delega integrate`, `delega spend`, `delega audit <branch>`.

To add a machine, run `nodes/linux.sh` on it (or `nodes/windows-wsl/` for a Windows PC), add a
`[nodes.<name>]` block, then `delega sync` to push the rules bundle.

## Commands

| Command | What it does |
|---|---|
| `run` | launch a job (local or on a node) with caps, identity check and brief lint |
| `ps` / `watch` / `say` / `adopt` | see, follow, talk to, and re-attach to jobs |
| `nodes` / `doctor` | state of the nodes / pre-flight checklist with fixes |
| `quota` | how many workers fit before the usage windows fill up |
| `spend` | list-price cost by day, profile, project or orchestrator, against the budget |
| `scan` | forbidden terms in a branch, brief or text (exit 9 on hits; prints files and counts only) |
| `handoff` | draft a fresh brief to continue a job without an expensive `--resume` |
| `queue` / `tick` | queue briefs with priorities; launch them when quota, budget and capacity allow |
| `integrate` | `git merge-tree` dry run into a target branch, then the project's `verify` commands |
| `sync` | push the profile's rules bundle (CLAUDE.md, hooks, agents, settings) to nodes |
| `audit` | provenance of a job or branch |
| `metrics` | tokens, time per activity and node resources of finished jobs |

The module contracts and file formats are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Configuration reference

`~/.config/delega/config.toml` (or `$DELEGA_CONFIG`); see [config.example.toml](config.example.toml).

| Section | Keys |
|---|---|
| `[general]` | `locale` (`en`\|`es`), `owner_name`, `state_dir`, `default_profile` |
| `[policy]` | `job.max_min`, `job.max_turns`, `job.max_usd`, `job.grace_s`, `stall_s`, `answer_wait_s`, `resume_max_min`, `max_local_jobs`, `expected_hours`, `node.max_load_pct`, `node.min_disk_gb`, `node.min_mem_mb`, `node.reserve_mb_per_job`, `permission_mode.default`/`.deny`, `budget.day_usd`, `quota.window_5h_pct`, `quota.weekly_pct`, `lint.require`, `lease.renew_s`/`.stale_s` |
| `[profiles.<p>]` | `owner = "self"` (required), `launcher`, `config_dir`, `identity.expect_email` / `.expect_email_domain`, `repo_match`, `git`, `model`, `nodes`, `compute`, `forbidden_terms_file`, `forbidden_engines`, `node_bundle`, `bundle_post_sync`, `policy.*` |
| `[nodes.<n>]` | `ssh`, `max_jobs`, `min_disk_gb`, `profiles`, `workdir`, `jobs_subdir`, `python`, `runner` (`host`\|`docker`\|`kindling`), `config_dirs`, `docker_image`; kindling: `golden`, `vm_home`, `vm_user`, `vm_repo`, `golden_base_ref`, `egress_allow`, `token_command`, `token_env`, `account`, `keep_vm` |
| `[compute.<c>]` | `kind` (`gcp-adc`\|`env`\|`command`), its parameters, `fix_hint` |
| `[projects."owner/repo"]` | `verify`, `branch_prefixes`, `policy.*` |

Every policy value resolves in this order: environment variable `DELEGA_<KEY>` → profile → project →
global `[policy]` → built-in default.

`sync` ships the profile's `node_bundle` directory: `CLAUDE.md`, `hooks/`, `agents/`, `settings.json`
go to the node's `~/.claude` (or `config_dirs[profile]`); a `home/` directory is unpacked relative to
`$HOME`; `bundle_post_sync = ["cmd", …]` run on the node afterwards (e.g. to merge `settings.json`).
A node-rules section (caps, shared-machine limits, no client data, never bypass hooks) is appended to
`CLAUDE.md`. Credentials are never sent.

## Terms of use

delega automates *your own* Claude Code usage; it must not become a way around anyone's limits.

- **One account per person.** Every profile has `owner = "self"`: an account assigned to you and used
  only by you. delega refuses configurations with `failover` or `rotate`, and a job never moves from
  one profile to another.
- **No sharing, pooling or rotating subscriptions** to dodge usage limits — yours or anyone else's.
  The quota and budget features exist to *stay under* limits, not to evade them.
- **Always through the official `claude` binary.** delega launches it; it does not call the API with
  extracted tokens, and it never copies credentials to nodes (each node signs in on its own).
- **Check your provider's terms** (Anthropic's usage policy and the terms of your plan, and any
  cloud provider you route through) before running automated or unattended workers, and keep to them.

## How it compares

These tools overlap in places but solve different problems; the list is a sketch, not a ranking.

- **claude-squad** — a terminal UI that manages several interactive agent sessions in tmux/worktrees on
  one machine. delega is non-interactive and caps-first, and spans accounts and SSH nodes.
- **Conductor** — a Mac app to run and review multiple Claude Code agents in parallel workspaces. It is
  a GUI for people watching; delega is a CLI/skill driven by an orchestrator session.
- **vibe-kanban** — a kanban-style board that dispatches tasks to coding agents. delega has no board:
  work is briefs and a queue, with budget, quota and identity checks built in.
- **container-use** — gives agents isolated container environments via MCP. It isolates *where* an
  agent runs; delega governs *how much* it may spend and *who* it runs as (they combine: `runner = "docker"`).
- **Claude Code agent teams / subagents** — coordination inside one Claude Code session and account.
  delega runs separate sessions on a different account/machine so the orchestrator's own usage stays small.
- **Cloud sessions (Claude Code on the web)** — hosted, managed sessions. delega runs on hardware you
  control, which matters when data must stay on your machines or compute needs private networks.

## Development

```
python3 -m unittest discover -s tests -v
ruff check .
shellcheck nodes/*.sh nodes/windows-wsl/*.sh
```

Stdlib only; Python ≥ 3.11. User-facing messages go through `delega.i18n.t()` with keys in both
`src/delega/i18n/en.json` and `es.json`.

## License

Apache-2.0, see [LICENSE](LICENSE).
