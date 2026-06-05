# Session Summary

## Date / Time
2026-06-05 (continued multi-day autonomous session)

## Branch
`gt-consensus-curation` (product code) + `gt-fullrun-shard` (run/CI + gt_gt.md ledger)

## Commit (run branch head)
`2f9a65ae` (guard-verify conclusion). Product fixes: `0118a9a6`/`e907a056` (anchor_prox, inert),
`a6787195`/`e20cdbbe` (test-edit guard), `3b161490` (verifier hardening).

## Objective
Make GroundTruth produce real **flips** (resolve SWE-bench-Live tasks the GT-OFF baseline cannot)
by delivering correct context — proven by paired GT-vs-baseline lift, not "GT delivered."

## Files read
`gt_gt.md`, `BRIEFING.md`, `v1r_brief.py`, `graph_localizer.py`, `v7_4_brief.py`, `post_edit.py`,
`oh_gt_full_wrapper.py`, `.claude/reports/full300_baseline_ohdeepseek_20260531/` (frozen baseline).

## Exact decision lines used
CLAUDE.md "DEFINITION OF DONE: metrics changed"; AGENT-OBSERVATION rule (trust delivered text, not
telemetry); "RESOLVED is not the prize — the trajectory is" (added this session); "never benchmaxx /
validate on holdout"; baseline frozen — never rerun (added this session).

## Research checked
SWERank (issue-named entities = edit target); stack-frame bug-localization (arxiv 2412.03905, W_FRAME);
KGCompass 2025; RRF (Cormack SIGIR 2009). Used to LIPI the localizer, not to justify a tune.

## Implementation changes
- **Test-edit guard** (`post_edit.py` `_test_edit_advisory`): non-leakage advisory on test/fixture
  edits ("fix the source"); 6 tests + 88 regression green. SHIPPED.
- **anchor_prox tier plumb** (`v1r_brief.py`): SHIPPED but proven **INERT** (misdiagnosis from telemetry).
- **Verifier** `check_gold_in_brief.py` hardened (parse only `<gt-task-brief>`, require gold PRIMARY).
- Durable rules added to CLAUDE.md + memory: baseline-frozen, trajectory-not-resolved.

## Metrics before
GT-OFF baseline (frozen): 87/300 resolved. Prior GT-ON net-negative vs baseline.

## Metrics after
- 2 known-failures (weasyprint, matplotlib): resolved GT-on, but **trajectory = self-localization**,
  brief misdirected — NOT GT-caused.
- **Flip measurement (12 baseline-failures, GT-on): 0 flips / 9 gradeable** (3 infra).
- **Guard verify (conan+checkov): 0 flips** (guard didn't fire — dispatch source-ext-gated + stochastic gaming).

## Tests / runs executed
Runs: 27002256876, 27006133706, 27008288798 (instrumented), 27011135159 (12-task flip), 27017458363 (guard).
Local: full tier + post_edit unit suites green; red→green proofs; composite-sort validation on real records.

## Result
**GT functions** (localizes ~half, agent self-localizes rest, contracts real) but produces **0 flips**.
Root cause (evidenced across 11 trajectories): dominant failure is **post-localization implementation
correctness** — hidden-test vocabulary/logic (`"row"` vs `"line"`, `TypeError` vs `NotImplementedError`,
which-of-two-valid-fixes) that a no-leakage layer structurally cannot supply. BUG-3 localization is real
(wrong primary 5/11) but neutralized by agent self-localization; fixing it needs holdout measurement, not
a session-tune. Test-edit guard is correct harm-reduction with ~0 flip yield.

## Regressions
None shipped. The anchor_prox fix is inert (kept as harmless guard, documented as such). Falsified the
naive composite-sort fix on real data BEFORE shipping (avoided a holdout regression / benchmaxx).

## Rollback decision
Nothing requires rollback. anchor_prox guard + test-edit guard are additive and harmless.

## Open blockers
The flip ceiling is structural (post-localization correctness), not a bug. No code fix reaches it.

## Next allowed action (STRATEGIC — user decision)
1. Accept the ceiling; measure GT by harm-reduction / turns-to-gold / curation, not flips, on this benchmark.
2. Re-scope the goal (flips require post-localization correctness, outside GT's no-leakage design).
3. Change the validation surface to one where localization IS the bottleneck (large unfamiliar repos /
   weaker agents) — where GT's strength pays off.
Deferred, low-yield: C localization via `measure_brief.py` holdout; guard dispatch fix for yaml fixtures.
