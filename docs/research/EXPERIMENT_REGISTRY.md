# Experiment Registry

## EXP-DEPTH-NAMING: graph.db as a TRUE map — depth-to-production + CHA/XTA naming
- **Date:** 2026-06-13 (later session)
- **Commits:** `9860ff7e` (depth LIPI), `9db1fe44` (residual counter), `b5ceaf5d` (Pass 4f production), `ec20d603` (CHA/XTA rung Py+Rust), `71d66378` (CHA/XTA extension Go+TS — closes the DeepSWE non-Python gap)
- **Hypothesis:** Materializing the property→edge promote pass + IMPORTS into the PRODUCTION
  `graph.db` (not copies-only) and converting one typed-field name_match method-edge class to a FACT
  makes `graph.db` a truer map (more resolved CALLS/IMPORTS edges, fewer name-guesses) — the
  enabling-substrate for the agent to reach gold. Per Mandatory Rule 2 this is map-connectivity, NOT
  a claim that any single edge type is THE flip lever.
- **Diagnosis basis:** reports 19–24 (`.claude/reports/four_surface_failure_diagnosis_20260613T152534Z/`).
- **Local verification:** Python promote pass 5/5 red→green (adaptix copy, idempotent); Go
  build/vet + 8 promote tests + resolver tests GREEN (CGO+`sqlite_fts5`, mingw gcc 16.1);
  localizer degree filter strict no-op on 4 live graphs.
- **Live result:** **NONE.** No `output.jsonl` newer than the fixes; every run on disk ran a DARK
  binary (report 22).
- **Result:** **SUBSTRATE-LANDED, UNWITNESSED.** Per DEFINITION OF DONE, not "done."
- **Gate before any benchmark number (report 21):** held-out multi-lang `go test` on a REAL
  toolchain + a REAL production `graph.db` (Codespace) — non-invention/additive/idempotency
  invariants.
- **Next:** rebuild substrate → re-index a Go + Rust task → confirm Pass 4f + CHA rung emit on a
  real graph → paired live tenpack GT-on vs frozen baseline, read chronologically per §4 (task #6).
- **Scope honesty:** report 23 — the richer graph broke the directed structural ceiling on only
  1 of 7 measured tasks (aiomonitor, +1 gold file via a READS edge, NOT a flip); the other 6 are
  anchor/FTS seeding or substrate-absent failures no edge-promotion can bridge. Depth is a RECALL
  (wide multi-file scope) lever, not a localization/leaf-naming lever.

## EXP-ORACLE-HYBRID-DELIVERY: oracle un-stub (DARK-binary fix) + apply_patch detection + hybrid edit-coverage + data-plane/control-plane BULKHEAD
- **Date:** 2026-06-13 (later session)
- **Commits:** `32e4e313` (oracle un-stub — DARK-binary root cause), `a7a4be87` (RC5 — apply_patch `_classify` foundation + ≥3-signal hybrid `edit_coverage_ratio`), `35a3fb17` (HYBRID data-plane/control-plane bulkhead — the two-lane split IMPLEMENTED)
- **Design basis:** reports 24–25 (`.claude/reports/four_surface_failure_diagnosis_20260613T152534Z/24_metrics_validation_tool_binding.md` §"TWO LANES, SEPARATELY BUDGETED"); Nygard *Release It!* (bulkhead); gt_gt §15.2/§15.4 (oracle single-decision-point gate / Stage-3 parity).
- **Hypothesis (delivery-correctness + fault-isolation, NOT flip):** (1) the per-turn delivery gate must
  actually RUN — pre-fix it raised `TypeError` every turn (swallowed), so ZERO per-turn context reached
  the agent on all 8 tasks of run `27465183646` (the DARK binary). (2) GT edit-detection must see the
  agent's DOMINANT edit channel (`apply_patch`/`git-apply`) — else the contract action-hook never fires
  on real edits and every downstream metric is blind. (3) `edit_coverage_ratio` must be a hybrid (≥3
  signals) per the four pillars, not single-lexical (which inverted: 0.0-on-solved, 1.0-on-failed). (4)
  the always-needed context lane must survive an oracle crash — ONE gate crash darkening ALL delivery
  (0/8) is a single-point-of-failure. All feed `verify_horizon_band` SEVERITY, NOT `spec.obligation`.
- **The two-lane decision (IMPLEMENTED `35a3fb17` + fault-proven):** Lane A = context/data-plane
  (`l3.contract`/`l3.cochange`/`l3b.evidence`) delivered EARLY via `_lane_a_deliver`, per-producer
  isolated, OUTSIDE the oracle ≤1/turn winner gate + crash; old gate-pool pushes for these kinds
  REMOVED (the contract has exactly ONE path now). Lane B = oracle steer through `_oracle_gate_blocks`
  AFTER Lane A in ONE outer try/except, ADDS, never suppresses Lane A. One shared ledger
  (`_oracle_delivered_hashes`) dedups cross-lane. The coupling that WAS broken: the single
  `_oracle_gate_blocks` ≤1/turn winner gate (`_SEV_OBLIGATION=5` starving `_SEV_CONTRACT=3`) + the
  swallowed `_ProductHorizonThresholds` stub crash. `32e4e313` un-stubbed the gate; `a7a4be87` made
  edit-detection see `apply_patch`; `35a3fb17` lifted Lane A off the gate + bulkheaded Lane B.
- **Local verification:** `32e4e313` TTD red→green (in-container gate fires, 0→≥1 `gt.oracle_event.v2`);
  `a7a4be87` 16 pytest (12 patch-apply + 4 hybrid) driving the real chain (no injection); `35a3fb17` 31
  pytest GREEN — 7 hybrid incl FAULT-INJECTION (gate raise → contract still delivered) + NEGATIVE
  CONTROL (Lane A neutered + gate crash → contract len 0, reproducing the 0/8 mode, making the proof
  non-vacuous) + 16 RC5 + 8 oracle-LIPI. All 4 LIPI lenses commit_ready.
- **Live result:** **NONE.** Same DARK-binary gap as EXP-DEPTH-NAMING — oracle/delivery path
  UNWITNESSED live; `32e4e313` removes the crash in code but no `output.jsonl` newer than the fixes.
- **Result:** **CORRECTNESS-FIX-LANDED + BULKHEAD IMPLEMENTED + FAULT-PROVEN, UNWITNESSED.** Per
  DEFINITION OF DONE (structurally correct + fault-proven is NOT "done"), not "done."
- **Next:** the single live witness (task #6) discharges this AND EXP-DEPTH-NAMING — read
  `output.jsonl` chronologically for `gt.oracle_event.v2>0` on a live turn, M23 review-transition > 0, a
  `<gt-contract>` block reaching the agent AND surviving a control-plane crash that loses only the steer.
- **Residuals (non-blocking, `35a3fb17`):** dead `_lost` re-arm clauses for the moved kinds; the
  `no_flood` test does not fire a live steer (cross-lane flood owed to the witness).

## EXP-001: Decision 35 Part 2 — Budget Gates
- **Date:** 2026-05-17
- **Commit:** a79393c4
- **Hypothesis:** Capping L3b at 3 fires and L3 at 5 fires eliminates exploration expansion regression
- **Baseline run:** 25975336809 (20 tasks, no GT)
- **Before run:** 25978127934 (1 task, no caps — beancount NOT resolved, L3b=10+)
- **After run:** 25978442722 (2 tasks, with caps — both RESOLVED, L3b=3)
- **Result:** ACCEPT — regression fixed, no negative flips
- **Next:** 5-task paired smoke with baseline-failing tasks to detect positive flips
