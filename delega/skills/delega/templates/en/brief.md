# <Piece>: <goal in one sentence>

Order: `<path to order.md>` (acceptance criterion frozen; you do not change it).

## Hypothesis
- What you think is happening and **how to refute it quickly** (offline first: saved data, tests, deterministic A/B; no paid compute yet).

## Done when
- A numeric or checkable criterion: command + expected output, a passing test, a metric ≥ threshold.
- Stop criterion: if it does not improve after 2 attempts, stop and send a `blocker` notice (it becomes a "known limitation").

## Zone
- Files/folders you touch. **Do not touch**: …
- The repo's CLAUDE.md rules. Branch `<prefix allowed by the repo's hooks>/<name>`; commit; no push to the main branch, no merge.

## Environment
- Ports/containers with their own names (`<piece>-db`…); when you finish, tear down what you started.
- Baseline: `<one sha or branch>` — the same for every machine.

## Cap
- ≤ <N> wall-clock minutes · paid compute: <0 | N calls / ≤ X USD> — before each live round, send a `milestone` notice saying how many calls it will make.
- No subagents unless <…> (each subagent multiplies the cost).

## Report
Finish with a report of at most 15 lines: what you changed, branch and sha, what you verified by
running it (with numbers), what stays unverified and what you recommend next.
