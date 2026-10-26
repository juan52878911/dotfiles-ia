# <Piece>: <goal in one sentence> (with a cloned database)

Order: `<path to order-db.md>` (acceptance criterion frozen; you do not change it).
Launched with `delega run <repo> <this brief> --node kindling --db <name>` (or `--db all`).

## Data
- You have a **throwaway copy** of `<name>` (<engine> <version>) inside your microVM; the
  connection variables are in your environment and in delega's context (`DATABASE_URL`, `PG*`…).
- It is the project's **real data**. It only exists in this VM and is deleted with it; what you
  write to the database goes nowhere.
- **You may**: read the schema, count, aggregate, run migrations and tests against the copy,
  measure queries (`EXPLAIN`), check that a change works with real data.
- **You may not**: copy rows, values, samples, ids, emails, names or screenshots into any file of
  the repo (fixtures, seeds, tests, snapshots, docs, comments), commit messages, notices or the
  report. Fixtures are written by hand with made-up data that mimics the shape, not the content.
- No dumps (`pg_dump`, `SELECT … INTO OUTFILE`, `mongoexport`…) and no data files in the worktree:
  what comes back (branch, notices, report) goes through the forbidden-terms scan.

## Hypothesis
- What you think is happening and **how to refute it quickly** against the copy (a query, a test, an EXPLAIN).

## Done when
- A checkable criterion: command + expected output (counts, timings, a test passing against the copy).
- Stop criterion: if it does not improve after 2 attempts, stop and send a `blocker` notice.

## Zone
- Files/folders you touch. **Do not touch**: …
- Branch `<allowed prefix>/<name>`; commit; no push, no merge.

## Cap
- ≤ <N> min wall clock · ≤ <X> USD · no subagents except <…>.

## Report
End with a report of ≤15 lines **with no data**: what you changed, branch and sha, what you
verified against the copy (schema, counts and timings only: "query X goes from 2.1 s to 40 ms over
~1.2 M rows of `orders`"), what is left unverified and what you recommend. If a finding needs a
real value to be understood, describe it without the value ("some emails in `customers.email` are
upper case").
