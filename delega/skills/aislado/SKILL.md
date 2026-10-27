---
name: aislado
description: Run work in isolation — in a throwaway kindling microVM through delega — instead of on this machine. Use when the user says "isolated", "aislado", "not here", "in a VM", "sandbox it", or when the task runs third-party code, tests that need the network, more than one implementing agent, Playwright/a browser, or a cloned database. Covers when to isolate, the only way to launch (`delega run --node auto` after `delega doctor`), the known errors with their fix command, and what never to do by hand (`ssh <host> kling …`). Answer the user in the language they write in.
---

# Isolated work (kindling through delega)

"Isolated" means: the work runs in an ephemeral microVM cloned from a golden image on a kindling
host, with egress limited to an allowlist, and the VM is deleted at the end. The **only** way in is
`delega` (see the `delega` skill for briefs, caps and review). This skill says *when* to isolate and
*what to do when it goes wrong*, so you never fall back to raw `kling` over ssh.

## When to launch isolated

Isolate (do not run it on this machine) when the task:

- runs **third-party code** (a dependency you do not trust, a repo you did not write, a script from an issue);
- runs **tests that need the network** or that could touch real services;
- needs **more than one implementing agent** at the same time (one VM per worker, no shared checkout);
- drives a **browser** (Playwright, visual checks) — verify in the same golden that implemented, never on the Mac;
- works on a **cloned database** (`--db <name>`: real data stays inside a VM with no internet).

Anti-example: nineteen agents run on the user's laptop, three of them implementing in the same
checkout. That is exactly what isolation is for.

Stay local only for what needs local tools, uncommitted files, or push/PR credentials.

## How (always the same two steps)

1. `delega doctor [repo]` — host reachable, real free slots (VMs whose job is alive), golden and its
   fingerprint. Fix every ✗ with the command it prints before going on.
2. `delega run <repo> <brief> --node auto [--db <name>] [--cap-min N] [--zone 'glob']…` with
   `run_in_background: true`, then one `delega watch`. Commit first: only HEAD travels.

Each job VM gets a TTL (cap + grace + margin): if everything else fails, the host freezes it (or
removes it, with `--db`) on its own. The branch comes back as a file (`kling cp`) and is scanned.

## Known errors → the command that fixes them

| You see | Do |
|---|---|
| `kling does not answer` (doctor / nodes) | Tell the user the host is down; do not retry in a loop. `delega nodes` again later. |
| `N delega VMs with a live job (max_jobs=M); orphans not counted: …` | Wait or `delega queue add`; orphans: `delega gc --node <n>` (lists), then `delega gc --node <n> --yes`. |
| `No contact with <host> … delega adopt <id>` (rc 96) | Nothing was lost: the worker keeps running in its VM. When the host answers: `delega adopt <id>` (waits and brings the branch) or `delega recover <id>` (closes now). |
| rc 97 (broker lost) or `could not fetch <branch>` | `delega recover <id>`: reads the rc in the VM, brings the branch with `kling cp`, scans it, closes the job, deletes the VM. |
| `delega gc` says `kept: it may hold work → delega recover <id>` | Run that `delega recover <id>`; never `kling rm` it by hand. |
| `no golden <name>` | `delega golden build <repo>` (tell the user: 2–11 min). |
| golden has no / a different lockfile fingerprint | Relaunch with `--rebuild-golden`. |
| claude version skew between here and the golden | `delega golden build <repo>` (or `--allow-version-skew` if the user agrees). |
| `doesn't fit: the microVM asks for N MiB` | Wait for a slot or `delega gc`; do **not** raise `golden_mem` or memory settings yourself. |
| `unrecognized arguments: …` after `--zone` | Quote each glob and repeat the flag: `--zone 'src/**' --zone 'web/**'`. |
| rc 98 (worker did not start) | `delega doctor [repo]`; read the job's `stderr.log`; report to the user. |

## Never

- **No `ssh <host> kling …` by hand**, unless a `delega` message prints that exact command as the fix.
  `kling exec` enters as **root** (delega runs the worker as another user, so paths and permissions
  differ) and **cuts its output at 8 MiB** (a bundle or log through stdout arrives truncated). To move
  files out of a VM use `kling cp <vm>:<path> <dest>` — or better, `delega recover`, which already does.
- **Do not edit delega's private configuration** (`~/.config/delega/…`: nodes, `golden_mem`,
  `golden_extra`, egress lists, extra node definitions). What the repo needs goes in the repo; what the
  node needs, the user changes. Propose the change and stop.
- Do not give job VMs internet to "fix" fonts or package installs: declare the dependency in the golden.
- Do not delete VMs that may hold work (`frozen`, `stale lease`): `delega recover` them.
- Do not verify on this machine what was implemented in the VM.

## Installing this skill

It lives in the delega checkout; link it into Claude Code's skills folder (one time):

```bash
ln -s "$PWD/delega/skills/aislado" ~/.claude/skills/aislado
```

Run it from the repository root (or replace `$PWD/delega` with the checkout's path). It needs the
`delega` CLI on `PATH` and a kindling node in delega's config; `delega doctor` says whether both are there.
