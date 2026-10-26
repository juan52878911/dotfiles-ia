# Order with real data: <name>

> Agreed with {owner} **before** the first worker. On top of a normal order (`order.md`),
> it pins down which real data goes in and how it is controlled.

## Goal and deliverable
- What exists at the end that does not exist today (PR, report, metric). Only one.

## Data that goes in (OK from {owner}: <date>)
- Project `<org/repo>` · database `<name>` (<engine> <version>) · volume `db-<repo>-<name>` on `<node>`.
- Dump: `<where it comes from>` (command on the host or file), date `<…>`, sha256 `<first 12>`
  (`delega db ls`). Refreshed with `delega db volume refresh` and **only with {owner}'s OK**.
- Anonymised: <yes / no / partly: which columns>. Tables left out: <…> (if needed, a dump with `--exclude-table`).
- Mode: data golden (`delega golden db`, ~1 s start) or volume (one restore per job).

## Acceptance criterion (frozen)
- Input → expected output, measured against the copy. Nothing in the criterion quotes real values.
- Hash of the expectations: `shasum -a 256 <file>` = `…`

## Controls
- VM egress: allow-list (the node's `egress_allow`). Nothing leaves but the branch, notices and report.
- Forbidden-terms scan on the branch and on what comes back; new terms of this domain added to
  `forbidden.txt`: <…>.
- Human review of the diff before integrating: new fixtures or tests? made-up data?
- Audit: `delega audit <branch>` shows volume, sha, golden, VM and cost of every job.

## Budget
- Wall clock: ≤ … h · account spend: ≤ … $ (`delega spend`).

## Out of scope
- Exporting data, reports with data, changes to the real database.

## Changes
| Date | What changed | Why | OK from {owner} |
|---|---|---|---|
