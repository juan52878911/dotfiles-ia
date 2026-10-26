# Job closing: <branch>

Filled in by the orchestrator when the worker comes back, before integrating or reporting to {owner}.

## Provenance
- [ ] `delega audit <branch>`: profile, account, node, VM, golden, volume(s) and sha, cost. They match the order.
- [ ] `delega spend` within the order's budget.

## What came back
- [ ] Clean scan: `delega scan <repo> <branch>` (exit 0). Otherwise it is not integrated.
- [ ] Diff reviewed by hand: no fixture, seed, test, snapshot or doc with data copied from the database
      (look for emails, phone numbers, personal names, long ids, specific amounts).
- [ ] Commit messages, notices (`notices.jsonl`) and report free of real values.
- [ ] New non-code files (`*.sql`, `*.csv`, `*.json`, `*.dump`) justified.

## Verification
- [ ] `delega integrate <repo> <branch>`: dry-run merge and the project's `verify` commands green.
- [ ] What was checked against the copy is in the report with numbers (counts, timings), no data.

## Clean-up
- [ ] The VM is gone (`delega sandbox ls` / `kling ps` on the host) unless `keep_vm` on purpose.
- [ ] Dev sandboxes frozen or removed (`delega sandbox freeze|rm`).
- [ ] If the dump changed: `delega golden db` so nobody works on a stale golden.

## Report to {owner}
- What was done, branch and sha, what was verified, what is left, cost. No data.
