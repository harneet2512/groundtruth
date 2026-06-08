# task_ledgers/

One ledger per SWE-bench task audited under the **gt_trial.md §4** protocol. Each ledger is the
per-gt_gt-COMPONENT audit of that task's trajectory — **REAL VALUES, `GT SENT` ↔ `AGENT DID`,
read (never grep) from `output.jsonl`**. Mandated by `CLAUDE.md` + `gt_trial.md §4`.

## Format (per gt_trial.md §4) — every ledger contains, per task:
- **PREREQS table** — substrate P1 resolution / P2 graph.db / P3 embedder, the **8-dp real values**
  verbatim from the gate-deep JSON, `GREEN?`, and **how it reached the agent** (substrate numbers
  are telemetry-only → they reach the agent ONLY as the brief's resolved-edge lines).
- **One table PER COMPONENT** (L1 · L3b · consensus · L3/GT_VERIFY · L4 · L5 · L5b · L6), columns:
  `turn | GT SENT (verbatim bytes the agent saw) | AGENT DID (verbatim action at/after) | D/C/C`.
- **per-table verdict** (D/C/C + leakage count) + a **cross-component line** (leakage MUST be 0;
  consumed-count; fair-probe-count).

**Append-only across runs** — never overwrite a prior run's task ledger; add the new run's audit
section under a dated heading.

## Index
| task | run | resolved | flip | ledger |
|---|---|---|---|---|
| amoffat__sh-744 | 27107841613 (2026-06-07) | yes | no (baseline pass) | [amoffat__sh-744.md](amoffat__sh-744.md) |
| aws-cloudformation__cfn-lint-3749 | 27107841613 | no | no | [cfn-lint-3749.md](cfn-lint-3749.md) |
| aws-cloudformation__cfn-lint-3764 | 27107841613 | no | no | [cfn-lint-3764.md](cfn-lint-3764.md) |
| aws-cloudformation__cfn-lint-3767 | 27107841613 | no | no | [cfn-lint-3767.md](cfn-lint-3767.md) |
| aws-cloudformation__cfn-lint-3768 | 27107841613 | no | no | [cfn-lint-3768.md](cfn-lint-3768.md) |
| aiogram__aiogram-1594 | 27107841613 | no (no_patch) | no | [aiogram-1594.md](aiogram-1594.md) |
| arviz-devs__arviz-2413 | 27107841613 | no (no_patch) | no | [arviz-2413.md](arviz-2413.md) |
| aws-cloudformation__cfn-lint-3779 | 27107841613 | no (no_patch) | no | [cfn-lint-3779.md](cfn-lint-3779.md) |
| aws-cloudformation__cfn-lint-3770 | 27107841613 | CANCELLED (timeout ~67m) | n/a | [cfn-lint-3770.md](cfn-lint-3770.md) |
| aws-cloudformation__cfn-lint-3789 | 27107841613 | CANCELLED (timeout ~67m) | n/a | [cfn-lint-3789.md](cfn-lint-3789.md) |
