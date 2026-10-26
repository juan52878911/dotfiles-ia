# Order: <name>

> Written and agreed with {owner} **before** the first worker. Afterwards it only changes through
> the "Changes" section, with date, reason and {owner}'s approval.

## Goal and deliverable
- What exists at the end that does not exist today (PR, report, metric). Exactly one.

## Acceptance criterion (frozen)
- Per case/content: input → expected output. One rubric, fixed denominators.
- Hash of the expectations: `shasum -a 256 <file>` = `…`
- "Do not make the baseline worse": judged over 3 runs (min/mean/max), not one.

## Baseline
- One sha/branch: `…`, measured once with the same configuration as the final rounds.

## Budget
- Wall clock: ≤ … h · account spend: ≤ … $ (`delega spend`) · paid external compute: ≤ … USD.
- Per piece: ≤ 2 h and ≤ 10 $. Mid-way review: if more than 60 % of the budget is gone with less than 50 % done, stop and re-plan with {owner}.

## Out of scope
- What does NOT go in (new features, infrastructure, other repos). Anything that appears goes to issues.

## Decisions that are {owner}'s
- List of what the orchestrator does not decide alone (product, criteria, extra spend, client data).

## Changes
| Date | What changed | Why | {owner}'s OK |
|---|---|---|---|
