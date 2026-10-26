---
name: delega
description: Orchestrate coding work by delegating it to Claude Code workers that run on another account of the user's (and/or on other machines) with hard caps. Use when the user says "delegate", "orchestrate", "run it on the node/VM", "with the work account", when `delega doctor` shows a configured profile matches the repo, or at the start of any coding task in a repo that belongs to a profile with `repo_match`. If another installed skill is specific to the repo's organisation and points here, follow that one and use this as its reference for the mechanics. Answer the user in the language they write in.
---

# Orchestrating with delega

This session is the **orchestrator**: it understands the request, splits it, writes briefs, delegates,
reviews and reports. The heavy consumption (reading code, implementing, running tests) is done by
**workers**: headless Claude Code processes launched with `delega run`, on the account of a *profile*
(an account the user owns, used only by them) and on this machine or on a *node* (an SSH box).
Answer the user in the language they write in; keep briefs in the language the workers should use
(`locale` in the config sets the protocol messages).

`{owner}` below is `owner_name` from the config: the human who decides.

## When it applies

- The repo matches a profile (`repo_match`), or the user asks to delegate / use the work account / run on a node.
- If the user says "do it here" or it is a quick question (a read, an answer), do not delegate.

## Split of work

| Here (orchestrator) | Worker |
|---|---|
| Clarify the request with {owner}, write `order.md` | Explore the code in depth |
| Split into pieces and write each brief | Implement, run tests, commit on its branch |
| Review with `git diff --stat`, `git log`, targeted excerpts | Measurements, benchmarks, long reviews |
| Answer notices, decide what the order already covers | Open a PR only if the brief says so (local runs only) |
| Report to {owner} | |

Do not use subagents or side sessions here for heavy work: they consume the orchestrator's account.
Do not generate diagrams, artifacts or images mid-order: each one re-reads the whole orchestrator
context. If {owner} asks, hand it to a worker or leave it for the close.

## 0. Before you start (once per order, ~30 min)

1. `delega doctor <repo>`: identity per profile and node, compute credentials, disk and load, version
   drift, quota, today's spend, forbidden-terms file, bundle. Every ✗ comes with the exact fix command:
   fix it **now** (credentials expiring in the middle of the night cost hours). Whatever needs {owner}
   (logins), ask for it in a single message with the commands doctor printed. Before each **batch** of
   N jobs, `delega doctor --batch N <repo>`: per node disk, load, RAM, account, declared compute (ADC…),
   claude version, `kling ps` and the repo golden; it says whether the batch fits and logs every refused
   node to the audit (`refused`, scope node).
2. `order.md` from `templates/<locale>/order.md`, agreed with {owner}: one deliverable, a **frozen**
   acceptance criterion (with a hash), a single baseline, a budget (hours, account $, paid compute
   USD), what is out of scope and which decisions are theirs. Without it, launch nothing (criteria
   defined after implementing mean redefined criteria and several "final" shas).
3. Decide how many orchestrators: **one**, unless the domains are closed and do not overlap. Each extra
   orchestrator is another decision queue for {owner}.
4. Infrastructure (hypervisors, disks, setting up a new node, drives) is not mixed into a product
   order: do it before, or in another session.

## 1. How to delegate

1. **Brief** from `templates/<locale>/brief.md`, self-contained: the worker does not see this
   conversation. `delega run` rejects a brief without a done criterion, a cap or a closing report
   (`--no-lint` only for tests), and then runs three deterministic rules (policy `lint.branch`,
   `lint.live`, `lint.text`: `error`, `warn` or `off`): a branch the brief names must match the project's
   `branch_pattern` (default: its `branch_prefixes`); a run against an AI provider needs `--requires` or the
   profile's `env_file` path in the brief; and a warning for an "original/previous brief" not attached, an
   absolute time with no rule for when it has passed, and a script named without a path. What worked: a hypothesis and how to refute it, a numeric success
   **and stop** criterion, a minutes cap, "offline first, no paid compute", and a branch prefix the
   repo's hooks accept (read the hooks first).
2. **Where it runs** (`--node`):
   - `--node auto` (the default) tries the profile's `auto_order` group by group (default
     `kindling → host → docker → local`): **kindling first** for isolated work (no `--requires`, not
     `--heavy`, the repo has a golden), then host nodes by free RAM, and this machine only as the last
     resort (up to `max_local_jobs`). A node also needs the right identity, enough disk, load under the
     limit, a free slot (`max_jobs`) and no declared compute `expired` (an expired ADC excludes the node
     even for jobs that do not require Vertex). `delega nodes` shows their state, the fix for each
     refused node and the order.
   - `--requires <compute>` if the job calls an external paid provider (Vertex…): the chosen node must
     pass that compute check, so it goes to a host node. `--heavy` for E2E suites with containers and
     benchmarks: also a host node.
   - `--node local` for what needs local tools, a browser, uncommitted files, or push/PR (nodes have
     no git hosting credentials).
   - `--node <kindling node>` (`runner = "kindling"`): the job runs in an ephemeral microVM cloned
     from a golden image with the repo's dependencies already installed, with egress limited to an
     allowlist, and deleted at the end. Good for isolated, parallel work that needs no containers or
     services; no `--resume` (the VM is gone: use `handoff`) and no `--requires`. The golden records
     the sha256 of the repo's lockfiles; when HEAD's differ, the launch warns, and `--rebuild-golden` (or
     policy `golden.auto_rebuild = true`) rebuilds it first. `delega golden ls` shows fingerprint and age.
   - `--db <name>` (kindling only): the job gets a throwaway copy of a database the project declares
     (`[projects."…".databases.<name>]`), cloned from the data golden (`delega golden db`, ~1 s) or restored
     from the dump volume (`delega db volume create|refresh`). Use `templates/<locale>/brief-db.md` and,
     for the order, `order-db.md`; close with `closing.md`. The worker is told what it may read and what it
     may never copy (fixtures, commits, notices, report). Creating or refreshing a volume with real data
     needs {owner}'s OK and the dump they choose; never paste data into logs, briefs or reports.
   - Only **HEAD** travels to a node: commit first. The worker uses branch `delega/<id>` (or
     `--branch`); when it ends, the branch comes home and is scanned against the forbidden terms.
   - Nodes carry the profile's rules (agents, hooks, skills, rules, CLAUDE.md, plus `bundle_skills`).
     New node or changed rules: `delega sync [node]`; kindling: `delega golden build` again. The bundle's
     `hooks/` should carry dotfiles-ia's `damage-control.py`, `no-cat-grande.py`, `delega-scan.py` and
     `delega-pr-gate.py` (registered in its `settings.json`), so workers on nodes stop at the same gates.
   - Every worker gets the job's facts in its system prompt (`context.md`: id, node, VM, golden, profile,
     account, caps, budget, branch, databases) and `DELEGA_JOB_ID|NODE|PROFILE|BRANCH` in its environment.
3. **Launch** with `run_in_background: true` (you are notified at the end; do not poll):
   `delega run <repo> <brief> --node auto [--requires c] [--heavy] [--rebuild-golden] [--cap-min N] [--cap-usd N] [--cap-turns N] [--zone glob] [--role r] [--model m]`.
   Right after, arm **one** monitor: `delega watch` (only your running jobs); re-arm it only if it
   expires **while you still have live jobs**. It emits `NOTICE:milestone|blocker|question`,
   `ALERT:stall|loop|errors|no_answer|undelivered|cap|cap_cut|dead`, `RESULT` and `END`.

### Owner: each orchestrator sees only its own

Every job is tagged with its orchestrator (`OWNER` = `DELEGA_OWNER`, `--owner`, or the name of the
directory you call from). `ps` and `watch` show only yours (`--all` for everyone); `quota` and
`spend` count the whole account. If two orchestrators share a directory, give each its own
`DELEGA_OWNER` in every call.

### Channel with the worker (while it runs)

- **See**: `delega ps [job]`: state, minutes, tools, cost, notices/alerts, last action, stall and cap warnings.
- **Say**: `delega say <job> "<text>"`: it reaches the worker as "[orchestrator] …" after the
  current tool and overrides the brief (correct course, stop, answer).
- **Questions**: on `NOTICE:question`, decide if the order covers it; if it is {owner}'s call, ask
  them and `say` the answer. The worker waits `answer_wait_s`.
- **Alerts**: `stall` → `ps` and decide; `loop`/`errors` → `say` another approach or tell it to stop
  with a `blocker`; `cap` → it was already asked to wrap up, wait for its report; `cap_cut` → it was
  cut: review the branch and continue with `delega handoff`.
- **Wake only on events**: do not write to {owner} or another session "no news". A turn without a
  decision or new data is pure cost.

### Caps per job (enforced by the broker on the node)

Defaults come from policy (`job.max_min`, `job.max_turns`, `job.max_usd`); per job with
`--cap-min/--cap-usd/--cap-turns`. When one is exceeded the worker is asked to wrap up with a report;
if it does not within `job.grace_s`, it is cut. Put the cap in the brief too: the brief's is read by
the model, this one is enforced. Cost is only known at the end of each worker turn: the dollar cap
cuts between turns, minutes and messages cut at any time.

### Budget, quota and spend

- `delega quota` before each batch and every ~15 min while jobs run: how many workers fit without
  the 5 h window passing its target before it resets, nor the weekly one. With quota 0, launch nothing.
- `delega spend [day|all] [--by profile|project|owner]`: list-price cost. `delega run` **refuses to
  launch** once the day's budget (`budget.day_usd`, per profile or project) is reached: that is a
  conversation with {owner}, not an automatic `--over-budget`.
- Plan long batches away from the 5 h window reset (limit cuts cost hours of wall clock).
- `delega queue add <repo> <brief> --prio N` + `delega tick` (e.g. from a timer) launch queued work
  when quota, budget and node capacity allow; `tick --adopt` re-attaches orphaned remote jobs.

## 2. Parallelise, iterate and cut

- Parallelise only pieces **without data dependencies**, each with its own environment, and never
  more paid-compute benchmarks than nodes. More workers do not fix an account limit or a lock.
- **Hypotheses in parallel** on an open problem: 2–3 workers, one per hypothesis, offline first, hard
  cap and a measurable criterion; the one that wins on numbers is integrated, the others are stopped with `say`.
- **Cut**: at most 2 iterations per hypothesis; the third is a "known limitation" and goes to an
  issue. A criterion is redefined **at most once**, in writing in `order.md` (Changes section, with
  {owner}'s OK) and **before** measuring again.
- **Re-measure** a sha only if its diff touches the path of the affected cases.
- **Wiring audit** after integrating fixes: an independent worker checks that each fix reaches every
  path the user sees.
- **Final sha** = suites + typecheck + E2E + a no-regression round, **and then** the PR. Never the
  other way round. Judge "do not make it worse" over 3 runs with one deterministic scorer.

## 3. Review, fix, close

1. **Review** what is on disk, not just the report: `git log --oneline -5`,
   `git diff --stat main...<branch>`, and the parts you doubt. `delega scan <repo> <branch>` looks for
   forbidden terms (it prints files and counts, never the text); exit 9 = hits: do not integrate or
   publish until cleaned. `delega integrate <repo> <branch> --into main` does a merge dry run and runs
   the project's `verify` commands in a throw-away worktree.
2. **Fix**:
   - Finished less than `resume_max_min` ago: `delega run … --resume <SESSION_ID>` (returns to the same
     node and worktree).
   - Stopped for longer, `--resume` is refused (it would re-send the whole context without cache).
     Use `delega handoff <job>` → fill in the draft → launch it as a new job (with `--branch` it
     continues on its commits on the same node). `--force-resume` only when it really pays off.
3. **Clean** the node's worktree and branches once the branch is integrated, and watch disk (`delega nodes`).
4. **Report** to {owner}: branch, `SESSION_ID`, job directory, cost (`delega spend`), what stays
   unverified. `delega audit <job|branch>` shows provenance (account, node, base, cost).

## 4. With {owner}

- Group their decisions in **one** message, with options and your recommendation; do not use them as
  a router between sessions.
- A new request mid-order (another project, another machine, a prototype) is not folded into the
  current order: propose a separate order with its own criterion and budget.
- If an instruction contradicts a recent one (scope, cap, machines), confirm which one holds before
  rewriting briefs.
- If they will be away, ask for a hard deadline and what you may decide alone; the rest waits.

## Fixed rules

- **One account per person.** delega runs only profiles with `owner = "self"`. Never share, pool or
  rotate accounts to dodge limits; jobs never move between profiles. Workers always run through the
  official `claude` binary.
- The account must match the profile's expected identity at every launch; the tool aborts otherwise.
- **Client data**: never in commits, fixtures, artefacts or notices. Names go in the profile's
  `forbidden_terms_file`; real corpora stay outside the repo from day one. Scan local work before publishing.
- **Paid external compute**: offline first; before each live round, a declared number of calls and a
  maximum USD per round.
- **Model per role** (`[roles.*]`, `--role`): worker = Sonnet (the default role); reviewer = Opus or Fable,
  read-only, always with `--cap-usd` (the role sets `require_cap_usd`), one per epic; summarizer/scout = Haiku
  with `--cap-usd 1`. Workers launch no subagents unless the brief allows it with a cap. You, the
  orchestrator, run on Opus without `[1m]` (Sonnet for fewer than 4 briefs) and keep context under 300k.
- **Before a PR**: `delega integrate <repo> <branch>` must end clean/verified on the branch's current sha;
  the `delega-pr-gate` hook refuses `gh pr create` from `delega/*` or `orq/*` otherwise.
- **Nodes**: load under the limit; never an E2E suite and a benchmark on the same node; enough RAM and disk.
- **Guards**: neither orchestrator nor worker bypasses hooks or classifier blocks; send a `blocker`
  and let {owner} decide.
- Nothing irreversible without {owner}'s confirmation (force push, deletions, publishing packages, merging PRs).
- Do not kill workers by matching their prompt text: stop the background task (or `say` it to stop).
- Changing delega does not affect running jobs: they keep the version they started with. Change, then
  relaunch what needs it.
- `permission_mode.default` is `auto`; `plan` makes the worker only propose; modes listed in
  `permission_mode.deny` (e.g. `bypassPermissions`) are refused.

## Command reference

| Command | For |
|---|---|
| `delega doctor [repo] [--profile p]` | Pre-flight checklist with fixes |
| `delega doctor --batch N [repo]` | Node preconditions before a batch; refused nodes to the audit |
| `delega nodes [--requires c]` | State of the nodes, exact fixes and the `--node auto` order |
| `delega quota [--profile p] [--hours h]` | How many workers fit |
| `delega spend [day\|all] [--by profile\|project\|owner]` | Cost and budget |
| `delega run <repo> <brief> [options]` | Launch |
| `delega ps [--all] [job]` · `watch` · `say <job> "…"` · `adopt <job>` | Channel |
| `delega handoff <job> [out.md]` | Continue without `--resume` |
| `delega scan <repo> <branch> [base]` | Forbidden terms in a branch (exit 9 on hits) |
| `delega integrate <repo> <branch> [--into main]` | Merge dry run + verify commands |
| `delega queue add\|ls` · `delega tick [--adopt]` | Queue with priorities |
| `delega sync [--profile p] [--tar file] [node…]` | Rules bundle to the nodes (or to a tar for a golden) |
| `delega audit [job\|branch]` | Provenance from the audit log (VM, golden, volumes and sha too) |
| `delega db ls` · `db volume create\|refresh <name> --from-host-cmd "…"` · `db volume rm <name> --yes` | Dump volumes on the kindling host |
| `delega golden build [repo]` · `golden db [repo]` · `golden ls` · `golden rm <name> --yes` | Golden images (plain / with the databases loaded) |
| `delega sandbox up <name> [repo] [--db n]` · `exec <name> -- cmd` · `shell` · `env` · `freeze\|thaw\|rm` · `save <name> <tpl>` · `ls` | Dev microVMs |
| `delega metrics start\|stop\|report` | Resources, tokens and times |

Policy keys (all under `[policy]`, overridable per profile/project or with `DELEGA_<KEY>`):
`job.max_min`, `job.max_turns`, `job.max_usd`, `job.grace_s`, `stall_s`, `answer_wait_s`,
`resume_max_min`, `max_local_jobs`, `expected_hours`, `node.*`, `budget.day_usd`, `quota.*`, `lint.require`, `lint.branch|live|text`, `lint.live_keywords`, `lease.*`.

Files of each job live in `<state_dir>/<id>-<repo>/` (on a node, `~/<workdir>/jobs/<id>/`): `meta`,
`brief.md`, `inbox.jsonl`, `notices.jsonl`, `progress.log`, `state.json`, `events.jsonl`, `result.json`,
`results.jsonl`, `summary.json` (final figures: minutes, messages, tools, cost, cap).
