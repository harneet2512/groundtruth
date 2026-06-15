# Session Summary

## Date / Time — 2026-06-15 — Adversarial MAX-LIPI re-review: 2 HIGH defects closed + 1 bonus bug

**Objective:** resolve the gaps the adversarial re-review (`docs/GT_GAPFIX_MAX_LIPI.md`) found in the
§10 gap-fix wave — the wave's self-verification had called things "resolved + verified" that were not.
No task-testing (per the no-benchmaxxing mandate); every fix is a structural code change + biting test.

**Result (full write-up: `gt_new.md` §10.1):**
- **HIGH #1 — W_PROX I2 rank leak (RELEASE BLOCKER): FIXED + PINNED.** G01 typed the reach term but its
  sibling `anchor_proximity.compute_anchor_proximity` (feeds the `W_PROX` rank term, 0.05→0.12) gated
  only on `confidence>=0.7` — the 5 promoted DEPTH classes (conf 1.0, cross-file) would shift rank once
  promotion ships. Applied the D5 `_degree_edge_filter`; `tests/test_anchor_proximity_i2.py`
  mutation-checked (RED w/o predicate, GREEN with). **Rank-leak count → 0.**
- **HIGH #2 — G09 `-file` inheritanceMap DISABLED in HEAD: RE-ENABLED + a SECOND independent bug
  found.** A `TEMP-RED-CHECK` had left the wiring stubbed (`_ = allFiles`); the §10 "resolved+verified"
  claim was false against the binary. Re-enabled (reconstruct `[]walker.SourceFile` from
  `allFiles`/`allLangs`, build + `SetInheritanceMap` before `Resolve`). **The fable-mode mutation
  check then refused to go green even with the map** — exposing that the incremental path zeroes the
  fresh nodes' `ParentID` and never restores it on the in-memory copy fed to `BuildNodeMeta` →
  `callerMeta.ParentID==0` → CHA self.method/inherited rungs dead regardless of the map. Fixed the
  ParentID restore. The e2e test was tautological (single-`save` fixture + byte-identical child → SHA
  short-circuit, passed on disabled code); rebuilt to BITE (ambiguous competing `Other.save` + modify
  child before reindex) — RED on either bug alone, GREEN only with both (probe: `name_match`→`inherited`).
- **MEDIUM/LOW/NIT — 10 resolved, 1 deferred-optional (§2.11, shared cc semantics).** chmod 777 + loud
  telemetry copy-out (§2.3); honest downgrade of `gt_ae_block.sh` single-source claim, trial/full.yml
  wiring OWED (§2.4); graph.db decode made fail-closed (temp+non-empty+atomic mv) (§2.5); corrected the
  false `.pyi`-matches-gt-index comment (§2.6); PRECEDES cc>1 SPECULATIVE-demotion test (§2.7); softened
  G05 "ENABLED" echo (§2.8); G17 reader-count made field-EXACT + two-field test (§2.9); dropped unused
  import (§2.10); removed redundant `global` (§2.12); fixed Default-OFF comment (§2.13).

**Gates:** `gt-index` build exit 0; Go resolver+cmd suites green (lone `TestRoutePatternMatching/comment`
fail is PRE-EXISTING/orthogonal); 31 py tests (contract/brief/I2) green; bash -n + py_compile clean;
no import cycle. **Files:** `main.go`, `promote_test.go`, `inheritance_incremental_test.go`,
`anchor_proximity.py`(+test), `graph_reach.py`, `contract_map.py`(+test), `gt_agent.py`,
`gt_mini_patch.py`, `gt_ae_block.sh`, `codespace_deepswe_run.sh`, `gt_new.md`, `GT_GAPFIX_MAX_LIPI.md`.

**OWED (not closeable statically this session):** live agent witness (gt-evidence 0→>0, no-task-testing
constraint); rebuild `gt-index/gt-index-linux` (predates G02/G09/PRECEDES/ParentID — CGO linux
cross-build, infra); actually source `gt_ae_block.sh` from trial/full.yml (paid-run change, verify-gated);
§2.11 same-file cc scoping (optional). Uncommitted — awaiting go-ahead to commit/push to hbali-stack.

---

## Date / Time
2026-06-13 (later) — Oracle un-stub (DARK-binary fix) + Graph-DEPTH to production + NAMING
residual/CHA-XTA matcher (Py/Rust→Go/TS) + localizer LIPI + RC5 oracle apply_patch foundation +
HYBRID data-plane/control-plane delivery bulkhead (**8 commits**)

## Branch / Commit
`gt-trial` — commits `32e4e313`, `9860ff7e`, `9db1fe44`, `b5ceaf5d`, `ec20d603`, `71d66378`,
`a7a4be87`, `35a3fb17` (local; to push to hbali-stack)

## Objective
Make `graph.db` a TRUE map (per the §"graph.db IS THE CONTEXT GRAPH" rule) AND fix GT's delivery so
the always-needed context reaches the agent even when the oracle dies. Concretely: un-stub the
per-turn delivery gate (the DARK-binary root cause — every run on disk crashed `_augment_output`
every turn → 0 context); land the property→edge depth promote pass + IMPORTS in the PRODUCTION Go
indexer (not copies-only); fix the architecture deviations a 4-avenue LIPI found; kill a false
`LSP_NO_OP_VALID` all-clear that hid unresolved non-Python method edges; convert one specific
name_match method-edge class to a FACT via a CHA/XTA receiver-type rung (extended from Python+Rust
to ALL Tier-1: Go + TS); lay the oracle/delivery foundation (apply_patch edit detection + hybrid
edit-coverage); and SPLIT delivery into a data-plane (Lane A, always-needed context, fault-isolated)
+ control-plane (Lane B, oracle steer) **bulkhead** so an oracle crash loses only the steer, never
the contract (the 0/8 single-point-of-failure fix). All ENABLING-SUBSTRATE + DELIVERY-CORRECTNESS
(map-connectivity + fault-isolation, Mandatory Rule 2) — NOT flip claims; live witness OWED.
Diagnosis basis: reports 19–25 under
`.claude/reports/four_surface_failure_diagnosis_20260613T152534Z/`.

## The framing this session applied (the decision arc)
1. **GENERALIZED, never per-task** — a per-task fix is benchmaxxing; the general property + held-out
   proof is the bar. Every commit is a structural property of code/issues/delivery, no task IDs.
2. **CORRECT-OR-QUIET** — a wrong fact/edge/steer that misdirects is worse than none. The CHA/XTA
   rung ABSTAINS on ambiguity/builtin; IMPORTS emits no edge for stdlib/3rd-party; Lane B never
   suppresses Lane A.
3. **DEPTH vs ACCURACY** — depth = navigable relationship-edge completeness (the promote pass +
   IMPORTS Pass 4f); accuracy = name_match→fact (the CHA/XTA rung). Two distinct levers, both shipped.
4. **DATA-PLANE / CONTROL-PLANE HYBRID** — the oracle is DEMOTED from gate-of-everything to a
   Lane-B steer-decider + shared-ledger-keeper. The always-needed context (contract / consistency /
   completeness — gt_gt §-philosophy "fire on EVERY edit") is Lane A (data plane), delivered EARLY +
   isolated so an oracle crash loses only the steer (the 0/8 SPOF fix, `35a3fb17`).
5. **DEFINITION OF DONE** — enabling-substrate + fault-proven is NOT "done"; the live witness is owed.
6. **gt_gt §12 — judge each layer by its ROLE** (L6 = reindexer, oracle = steer-decider, Lane A =
   contract deliverer), not by delivered/consumed alone.

## Files read
- `gt_gt.md` §1/§2.1/§2.3/§2.5/§2.6/§12 (intent for the promote pass + trust model)
- `CLAUDE.md` "graph.db IS THE CONTEXT GRAPH" + SCALE blocks (XTA/PyCG/CHA/demand-driven research)
- `.claude/CLAUDE.md` four pillars + DEFINITION OF DONE + LIPI 4-avenue rule
- `gt_gt.md` §15.2/§15.4/§16.5-B (oracle single-decision-point gate; Stage-3 parity; "0/9 routing gap")
- reports 19 (depth LIPI), 20 (naming matcher design), 21 (generalization/goal-tie contract),
  22 (Layer-4b audit — DARK-binary root cause), 23 (adaptive localization), 24 (metrics + tool
  binding — §"TWO LANES"), 25 (consumed in the hybrid-bulkhead design)
- `task_ledgers/README.md` (run history; runs `27321848581`, `27342218002`, `27367976952`, `27465183646`)

## Exact decision lines used
- gt_gt §2.6 line 262/282: "DATA_FLOW (annotation) … Not a standalone edge" → drove the D1 demote.
- gt_gt §2.1: "Go indexer owns production writes" → drove copies-only REFERENCE guard then Pass 4f.
- gt_gt §15.2: "the gate is the single decision point" → drove the per-producer try/except wrap so no
  producer can skip the gate (`32e4e313`), and the ONE-outer-try/except Lane-B isolation (`35a3fb17`).
- CLAUDE.md GT Context Philosophy: "Items 1, 2, 4 are ALWAYS needed … must fire on EVERY edit. Only
  item 3 (callers) requires verified graph edges" → drove Lane A (contract/consistency/completeness)
  off the oracle gate onto a robust always-fire path (`35a3fb17`).
- `.claude/CLAUDE.md`: "No benchmark-shape logic, task IDs, or gold labels" → stripped per-repo-%
  framing from the naming matcher (report 20 supersedes 17).
- CLAUDE.md Mandatory Rule 2: "no single evidence type is THE lever" → everything scoped as substrate.

## Research checked
- XTA / RTA — Tip & Palsberg, OOPSLA 2000 (declared-field-type receiver resolution, +88% over RTA).
- CHA — Dean/Grove/Chambers, ECOOP 1995 (`lookupMethodWithInheritance` primitive).
- PyCG — Salis et al., ICSE 2021 (assignment-graph; tracked as residual, not built this session).
- SCIP/Kythe/Stack-Graphs — import model for the IMPORTS fresh-extract (correct-or-quiet, no edge
  for stdlib/3rd-party).

## Implementation changes (8 commits)
0. **`32e4e313`** fix(oracle): restore the per-turn delivery gate — the DARK-binary root cause.
   `_augment_output` raised `TypeError: _ProductHorizonThresholds() takes no arguments` EVERY turn
   (the no-arg runtime-fallback stub was constructed with 6 kwargs by `verify_horizon_band`), and the
   exception was swallowed silently by the outer `except Exception: pass` BEFORE the gate's
   unconditional `gt.oracle_event.v2` telemetry write — so ZERO per-turn context reached the agent
   (only the turn-0 brief) on all 8 tasks of run `27465183646`. The stub was live because the
   in-container runtime import block aborted on the first missing module (`runtime.ledger`). FIX: gave
   the fallback stub a kwarg-accepting `__init__` (stub-signature faithfulness); injected `ledger.py`
   + `patterns.py` into `_PRODUCT_RUNTIME_FILES` (`gt_agent.py`) so `_RUNTIME_AVAILABLE=True` and the
   REAL horizon/ledger logic runs; made the outer swallow LOUD (stderr traceback, never re-raises);
   wrapped each per-turn producer in its own try/except so no single producer can skip the gate
   (gt_gt §15.2 — the gate is the single decision point). TTD artifact-first:
   `tests/test_oracle_gate_fires_in_container.py` RED pre-fix (0 `gt.oracle_event.v2`), GREEN post-fix
   (≥1 record), proven red→green by stashing the source fix. Files: `artifact_deepswe/gt_mini_patch.py`,
   `artifact_deepswe/gt_agent.py`, `tests/test_oracle_gate_fires_in_container.py`.
1. **`9860ff7e`** fix(depth-LIPI): DATA_FLOW demoted from 774 standalone edges → a CALLS.metadata
   annotation (D1); RAISES uses the polyglot class-label superset + drops dotted names (D3);
   copies-only REFERENCE guard (D4); `graph_localizer` fan_out/fan_in degree counts STRUCTURAL edges
   only so promoted edges can't inflate the hub signal (D5, proven strict no-op today); gt_gt §2.6
   reconciled + marked SUPERSEDED. Files: `scripts/graph/promote_property_edges.py`,
   `src/groundtruth/pretask/graph_localizer.py`, `gt_gt.md`.
2. **`9db1fe44`** fix(naming): dropped the Python-only `tgt.label='Method'` clause in
   `_count_residual_method_edges` (`resolve.py`) — Go receiver / JS-TS object methods land as
   `Label='Function'`, so ~475 genuine unresolved method edges read as 0 → a FALSE all-clear on
   Go/JS/TS. Now keys on language-agnostic `resolution_method='name_match'`. The JOIN to a real
   target is retained (non-invention).
3. **`b5ceaf5d`** feat(depth): moved the promote pass from copies-only REFERENCE to PRODUCTION —
   the Go indexer materializes IMPORTS + promoted relationship edges at index time (Pass 4f wired in
   `main.go` after serde/before closure, non-fatal+logged). `imports.go` fresh-extract is
   correct-or-quiet (single→CERTIFIED, >1→CANDIDATE, stdlib/3rd-party→NO edge). Fixed inv-7
   (`-file` reindex re-runs the idempotent promote) and D3 dotted-RAISES (drop `errors.New`, don't
   mint a wrong edge on a same-named class). Files: `main.go`, `imports.go`, `promote.go`,
   `promote_test.go`.
4. **`ec20d603`** feat(naming): CHA/XTA receiver-type matcher (rung 2b) — a typed `self.<field>.m()`
   where `<field>` is DECLARED via a colon annotation but never locally assigned now resolves to a
   FACT (`type_flow`/0.9/`field_type`/CERTIFIED), ABSTAINING on ambiguity/builtin (correct-or-quiet).
   Sits before name_match in the ladder. **Shipped Python+Rust colon-annotation fields first; the
   Go/TS gap was the explicit residual (since closed by `71d66378` below).** Files: `main.go`,
   `resolver.go`, `resolver_fieldtype_test.go`.
5. **`71d66378`** feat(naming): extended the CHA/XTA rung from Python+Rust to ALL Tier-1 — Go struct
   fields (space-separated `Field *Type`, parsed via `goStructFieldList`) + TS access-modifier fields
   (strip `private`/`public`/`protected`/`readonly`), and relaxed the receiver-shape gate to accept
   Go's receiver var (`GoReceiverName(Signature)→NodeMeta.ReceiverName`) alongside `self.`/`this.`.
   Closes the DeepSWE non-Python generalization gap for declared-field-type resolution; still
   correct-or-quiet (ABSTAINS on unknown receiver/field/type). Recovered + verified after a killed
   workflow (`parser.go` comment-detangle repaired). Files: `parser.go`, `resolver.go`,
   `resolver_fieldtype_test.go`.
6. **`a7a4be87`** fix(oracle): RC5 — two coupled, gt_gt-15/16-grounded, LIPI-caught fixes. (a) FOUNDATIONAL:
   `_classify`/`_edit_target` now recognize the `apply_patch`/`git-apply`/`patch -pN` edit family (parse
   target from `*** Update File:` / `+++ b/<path>`, hunk ranges from `@@`); priority-0 branch returns
   `('post_edit', target)`, correct-or-quiet (`None`→legacy fallthrough). The agent's DOMINANT edit
   channel was previously invisible to GT edit-detection — this unblocks the contract action-hook firing
   on `apply_patch` edits (the Lane-A enabler for task #7). (b) `edit_coverage_ratio` upgraded
   single-source-lexical → ≥3-signal hybrid (content-body lexical + graph Function/Method co-location +
   line-range overlap), FACT-tier/degrade contract, fixing the 0.0-solved/1.0-failed inversion; feeds
   `verify_horizon_band` SEVERITY, NOT `spec.obligation`. Files: `artifact_deepswe/gt_mini_patch.py`
   (+ 2 new test files under `artifact_deepswe/tests/`). The first Lane-A foundation stone — the
   contract action-hook could not fire on the agent's dominant edit channel until edit-detection saw it.
7. **`35a3fb17`** feat(delivery): the HYBRID data-plane/control-plane **bulkhead** (Nygard, *Release
   It!*) — splits `_augment_output`'s oracle route into two failure-isolated lanes, fixing the run-27465
   single-point-of-failure where ONE gate crash darkened ALL delivery (0/8). **LANE A (data plane):**
   `l3.contract` / `l3.cochange` / `l3b.evidence` deliver EARLY via `_lane_a_deliver` (append + record
   to the shared ledger) BEFORE any Lane B logic, each producer isolated; the old gate-pool pushes for
   these were REMOVED, so the contract now has exactly ONE delivery path (Lane A), never the gate
   (CLAUDE.md: contract/consistency/completeness fire on EVERY edit). **LANE B (control plane):** the
   steers go through `_oracle_gate_blocks` AFTER Lane A, wrapped in ONE outer try/except (stderr-only,
   no re-raise) so a gate/filter crash CANNOT undo Lane A's already-committed delivery — the oracle is
   DEMOTED to steer-decider + ledger-keeper, not bypassed. **SHARED LEDGER (one, not forked):**
   `_oracle_delivered_hashes` content+state dedup → cross-lane byte-identical re-sends suppressed.
   **PROVEN, not asserted:** the fault-injection test monkeypatches the REAL `_oracle_gate_blocks` to
   raise, drives the REAL `_augment_output`, asserts the contract survives; the NEGATIVE CONTROL (Lane A
   neutered + gate crashed → contract LOST, len 0) reproduces the 0/8 mode exactly and makes the proof
   non-vacuous (a silent revert of the lane ordering FAILS the test). 31 pytest GREEN locally (7 hybrid
   incl negative control + 16 RC5 + 8 oracle-LIPI). All 4 LIPI lenses commit_ready, zero blocking bugs.
   Files: `artifact_deepswe/gt_mini_patch.py`, `artifact_deepswe/tests/test_hybrid_lane_split.py`,
   `tests/test_oracle_lipi_audit_fixes.py`.

## The hybrid delivery decision (data-plane / control-plane — task #7, IMPLEMENTED `35a3fb17`)
Reports 24–25 §"TWO LANES" locked the delivery architecture as **two failure-isolated lanes sharing ONE
candidate schema + ONE `_augment_output` pipeline (ONE PRODUCT RULE preserved)**; `35a3fb17` IMPLEMENTS
the lane split (no longer design-only):
- **Lane A — context / data-plane (robust, delivered EARLY + isolated):** `l3.contract` / `l3.cochange`
  / `l3b.evidence` deliver via `_lane_a_deliver` (per-producer try/except; correct-or-quiet =
  non-empty AND content+state hash not already in the shared `_oracle_delivered_hashes`) BEFORE any
  Lane B logic — NOT routed through `_oracle_gate_blocks`, NOT subject to the oracle's ≤1/turn winner
  gate, NOT killed by a steer-producer crash. The old gate-pool pushes for these kinds were REMOVED:
  the contract has exactly ONE delivery path now.
- **Lane B — oracle steer / control-plane:** the steers run through `_oracle_gate_blocks` AFTER Lane A,
  the entire section wrapped in ONE outer try/except (stderr-only, never re-raises) — it ADDS its
  single band-gated candidate and NEVER suppresses Lane A.
- **The coupling that was broken (now fixed):** both lanes previously shared ONE `<=1/turn` winner gate
  (`_oracle_gate_blocks`) where `_SEV_OBLIGATION=5` outranked `_SEV_CONTRACT=3`, so the oracle STARVED
  the just-edited contract; AND the `_ProductHorizonThresholds` stub crash (swallowed) made the gate
  emit 0/9 every turn (the DARK-binary root cause, report 22). `32e4e313` un-stubbed the gate;
  `a7a4be87` made edit-detection see `apply_patch` so Lane A could fire on it; `35a3fb17` lifted Lane A
  off the gate entirely and bulkheaded Lane B. **Implemented + fault-proven; live witness OWED.**

## Metrics before / after
- **Before:** per-turn oracle gate raised `TypeError` EVERY turn → `gt.oracle_event.v2`=0 on all 8
  tasks of run `27465183646` (only the turn-0 brief reached the agent — the DARK binary); ONE gate
  crash darkened ALL delivery (0/8 SPOF); DATA_FLOW = 774 standalone edges on the adaptix copy (93.7%
  duplicating an existing CALLS pair); residual non-Python method-edge count read 0 (false all-clear);
  promote pass + IMPORTS lived in copies only, wired NOWHERE in `main.go` (Pass 4d → 4e, no 4f);
  closure CALLS-only.
- **After (LOCAL build/test only):** the gate no longer raises (kwarg stub + ledger/patterns injected →
  `_RUNTIME_AVAILABLE=True`; TTD red→green proves ≥1 `gt.oracle_event.v2` in the in-container shape);
  Lane A delivers the contract independently of the gate (fault-injection: gate crash → contract still
  len 171; negative control → len 0); DATA_FLOW rides CALLS.metadata (~19 standalone for no-CALLS
  hops); residual counter language-agnostic; Pass 4f materializes IMPORTS + promoted edges into a
  PRODUCTION `graph.db`; CHA/XTA rung emits CERTIFIED field-type edges across ALL Tier-1 (Python +
  Rust + Go + TS); GT edit-detection now sees the `apply_patch` channel (`_classify` returns
  `('post_edit', target)`); `edit_coverage_ratio` is a ≥3-signal hybrid (inversion fixed). **No live
  agent-behavior metric moved — see Result.**

## Tests / runs executed
- `32e4e313`: `tests/test_oracle_gate_fires_in_container.py` drives `_augment_output` in the
  in-container stub shape — RED pre-fix (gate writer silent, 0 `gt.oracle_event.v2`), GREEN post-fix
  (≥1 record). Proven red→green by stashing the source fix (artifact-first TTD).
- `9860ff7e`: Python REFERENCE pass re-proven red→green on a real adaptix copy (5/5 assertions,
  idempotent); localizer degree filter strict no-op on 4 live graphs.
- `b5ceaf5d`: Go build/vet + 8 promote tests GREEN locally (CGO + `sqlite_fts5`, mingw gcc 16.1);
  dotted-RAISES decoy test proven RED→GREEN.
- `ec20d603`: build/vet + resolver tests GREEN locally; `TestBuildFieldTypeIndex_LanguageScope`
  pins the Python+Rust boundary (went RED when the `71d66378` Go/TS extension landed — then updated).
  One pre-existing full-package failure (`TestRoutePatternMatching/comment`, `api_edges`) fails on
  base too — unrelated.
- `71d66378`: Go build + `go test ./internal/resolver -run FieldType` GREEN locally (CGO+`sqlite_fts5`,
  gcc 16.1); `resolver_fieldtype_test.go` extended to assert Go struct-field + TS access-modifier
  resolution + receiver-var shape. Recovered after a killed workflow (parser comment-detangle repaired).
- `a7a4be87`: 16 pytest tests GREEN (12 patch-apply in `test_rc5_patch_apply_edit_credit.py` + 4
  hybrid in `test_rc5_hybrid_edit_credit.py`) — these DRIVE the real
  `_classify`→`_edit_target`→`_augment_output`→`edit_coverage_ratio` chain (no injection — the
  discipline the original RC5 test lacked).
- `35a3fb17`: 31 pytest GREEN locally — 7 hybrid in `test_hybrid_lane_split.py`
  (`fault_injection` = gate raise → contract still delivered; `fault_injection_phase_filter_crash`;
  `fault_injection_negative_control` = Lane A neutered + gate crash → contract NOT delivered, len 0;
  `no_double_ship` + `no_double_ship_lane_a_self_dedup`; `no_flood`; `lane_b_single_steer`) + 16 RC5
  + 8 oracle-LIPI (`tests/test_oracle_lipi_audit_fixes.py` updated to the bulkhead semantics). All
  DRIVE the real `_augment_output`; the negative control makes the fault-injection proof non-vacuous.
- **NO live eval run dispatched this session.** No `output.jsonl` newer than the fixes exists.

## Result
Enabling-substrate landed in the production indexer + the residual/CHA fixes; the DARK-binary
per-turn-gate crash is FIXED in code (`32e4e313`); the always-needed context lane is now
fault-isolated from an oracle crash (`35a3fb17`, fault-injection + negative-control proven). Per
DEFINITION OF DONE (metrics changed) and the AGENT-OBSERVATION rule, **NOTHING here is "working" yet**
— all of it is code-fixed + fault-proven, metrics-UNWITNESSED. Report 22 confirms every run on disk
executed on the DARK binary (the gate crashed every turn); `32e4e313` removes that crash but there is
no post-fix agent observation yet. The live witness is OWED (see Open blockers / task #6): only an
`output.jsonl` showing `gt.oracle_event.v2>0` + a real `<gt-contract>` block reaching the agent on a
live turn — AND a control-plane crash that loses only the steer — discharges it.

## Regressions
None observed locally. The 8-commit set is additive (new resolver rung before name_match; new Pass 4f;
Lane A is a NEW early delivery path, Lane B unchanged behavior except crash-isolation). Risks tracked
as residuals (report 21): IMPORTS source-anchor (`ORDER BY id LIMIT 1`) + multi-candidate target
resolution are laundering vectors UNLESS made explicit uniform decisions — all correct-or-quiet SAFE
in direction (failure = silence/under-connect, never a confident wrong edge). CHA builtin-drop set
needs per-language re-justification + held-out false-drop proof. `35a3fb17` residuals (non-blocking):
dead `_lost` re-arm clauses for the moved kinds; the `no_flood` test does not fire a live steer
(cross-lane flood owed to the witness). 3 PRE-EXISTING `gt_mini_patch` failures (baseline-identical,
git-stash A/B classified): `test_lang_family_classifier`, `test_no_test_evidence_wired_into_augment_output`,
`test_unmet_obligations_raise_severity` (order-dependent intra-file contamination — fails on base too).

## Rollback decision
Per-commit revert available (`git revert <sha>`). Eight layered commits — revert in reverse order if a
regression surfaces: `35a3fb17` (hybrid bulkhead) → `a7a4be87` (RC5 oracle) → `71d66378` (CHA Go/TS) →
`ec20d603` (CHA Py/Rust) → `b5ceaf5d` (Pass 4f production) → `9db1fe44` (residual counter) →
`9860ff7e` (LIPI fixes) → `32e4e313` (oracle un-stub). The two naming commits are coupled (`71d66378`
extends `ec20d603`); revert both together. `35a3fb17` builds on `a7a4be87`+`32e4e313` (the oracle/
delivery chain); revert top-down. No rollback taken.

## Open blockers
- **THE GATE (report 21, undischarged):** held-out multi-lang `go build -tags sqlite_fts5 + go test`
  on a REAL toolchain (Codespace) on a REAL production `graph.db` — non-invention / additive /
  idempotency invariants — must precede ANY benchmark number. Local mingw build is not that gate.
- **Live witness owed (task #6):** run the trial on the post-fix binary, read `output.jsonl`
  chronologically, witness `gt.oracle_event.v2>0` + a real `<gt-contract>` block reaching the agent
  on a live turn AND a control-plane crash that loses only the steer. Until then no "working"/flip
  claim is permitted. The single witness discharges BOTH the depth/naming substrate AND the oracle/
  hybrid delivery path.
- **Hybrid delivery lane decoupling (task #7) is IMPLEMENTED + fault-proven (`35a3fb17`), NOT yet
  witnessed live.** Lane A delivers off the gate; Lane B is bulkheaded. The fault-injection +
  negative-control prove the SPOF is closed in code; the live cross-lane flood + a real steer firing
  alongside a surviving contract are owed to the witness.
- CHA builtin-drop set needs per-language re-justification + a held-out false-drop proof (report 21).

## Next allowed action
1. Push the 8 commits to hbali-stack → rebuild substrate (Go indexer changed) → re-index a Go + a
   Rust task → confirm Pass 4f materializes IMPORTS/promoted edges + the CHA rung emits on a real
   graph (now across Go/TS too) → THEN a paired live tenpack GT-on vs the frozen baseline on a
   baseline-fails id, read chronologically per §4. The SAME live witness discharges the oracle un-stub
   (`32e4e313`), the RC5 path (`a7a4be87`), AND the hybrid bulkhead (`35a3fb17`) — assert
   `gt.oracle_event.v2>0`, a `<gt-contract>` block reaching the agent, and a control-plane crash that
   loses only the steer (task #6).

---

# Session Summary (HYBRID lane-split — folded into the 8-commit arc above)

The data-plane/control-plane bulkhead (`35a3fb17`) and the oracle un-stub (`32e4e313`) are now
integrated as items 0 and 7 of the 8-commit arc at the top of this file (the earlier standalone
6-commit-era draft of this section, with its provisional "6 tests / 22 passed" counts, is superseded
by the final `35a3fb17` figures: 7 hybrid tests incl the negative control, 31 pytest total). See the
top section for the authoritative record.

---

# Session Summary (GHA non-Python fixes — same day, earlier)

## Date / Time
2026-06-13 — GHA non-Python failure fixes (FIX-A/B/C/D/E); LSP liveness verdict refined; LIPI vs the 4 surfaces

## Branch / Commit
`gt-trial` — local changes (to be committed + pushed to hbali-stack)

## Objective
Make Go/Rust/TS/JS reach the agent as reliably as Python on the DeepSWE GHA pipeline. Root-caused
in `GHA_NONPYTHON_FAILURE_AUDIT.md`: NOT Python-centric product logic, but env-provisioning
(gopls needs `go list`, RA needs `cargo metadata`+`rust-src`) colliding with a fail-closed
LSP-liveness gate. Apply FIX-A…E, document, LIPI against the 4 layers of separation.

## Implementation changes
1. **FIX-A (Surfaces 1+4, highest impact)** — LSP liveness axis is `server_launched`, not warm.
   Launched-but-not-warm (cold RA, gopls-no-metadata) → `LSP_WARN_NOT_READY` (**PASS**, deliver-
   always); only **never-launched** → `LSP_FAIL_NO_WARM` (exit 2). `resolve.py` verdict +
   `foundational_gates._classify_lsp` (consumes the hint) + `gt_run_proof.aggregate_lsp_verdicts`
   docstring + **`workspace_metadata` pre-flight made non-fatal (RC-4)**.
2. **FIX-B (Surface 3)** — Go probe populates the **writable** gomodcache (live GOPROXY,
   `d8fe8b37`); the LSP pass reads it offline with `GOFLAGS=-mod=mod` + `GOPROXY=off` + `GOSUMDB=off`
   (no proxy stampede, offline-deterministic, probe/pass agree).
3. **FIX-C (Surface 3)** — dropped `:ro` on the rustup mount + **baked-substrate rust-src fallback**
   (`docker cp` from `/opt/gt/rustup` when the task image ships none). Bounded, non-fatal.
4. **FIX-D (Surface 2)** — already shipped (`faf8c6b1`/`00bd27fd`): `sys.path` + graceful runtime
   imports. Re-verified, no change.
5. **FIX-E (Surface 4)** — per-language `env_validation` deferred (needs language list pre-index;
   no current impact; build self-test guarantees all 5 baked).

## Tests
- `tests/fail_closed/test_lsp_liveness.py` — **23 pass** (+1 WARN test; never-launched still FAILs).
- `tests/fail_closed/test_no_fallback_hardening.py` — **19 pass** (genuine-fail e2e repointed to a
  never-launched mock; +2 launched-not-warm WARN e2e).
- `tests/test_workspace_metadata_probe.py` — **4 pass** (updated to the `go list -e` + env contract).
- LSP/gate/proof sweep — **464 pass, 6 skip**.

## Result
All 5 fixes applied on their correct surface; no product logic leaked into GHA; no concept
duplicated across surfaces (LIPI verdict in `GHA_FIXES_LIPI_20260613T0640Z.md`). gt_gt.md §7 +
`GHA_NONPYTHON_FAILURE_AUDIT.md` updated.

## Regressions
None. The one stale test (`test_workspace_metadata_probe`, stale from `d8fe8b37`) fixed.

## Open blockers
- Substrate rebuild + digest pin required (FIX-A/B/C touch baked `resolve.py`/`foundational_gates.py`/
  `gt_run_proof.py` + the workflow); then re-proof Go/Rust to confirm they reach the agent.

## Next allowed action
1. Commit + push to hbali-stack → rebuild substrate → re-proof a Go + Rust task → confirm WARN-not-fail
   lets them reach pier → tenpack GT-on vs frozen baseline.

---

# Session Summary (prior)

## Date / Time
2026-06-12 — LSP proof boundary fixes (P0-04/05/02/06/07/11); Stage 1 code complete; tenpack not run

## Branch / Commit
`gt-trial` — local uncommitted changes (not pushed this session)

## Objective
Fix substrate proof failures from run `27387470440` (Go empty gomodcache, Rust LSP warm fail); document + LIPI each bug; finalize register; **do not** dispatch tenpack.

## What shipped

1. **P0-04** — Dynamic `GOMODCACHE` discovery + `dep_store_manifest.py` fail-closed for Go.
2. **P0-05** — Rust dep discovery, `rust-src`, RA `2026-06-08`, `gcc` in substrate Dockerfile.
3. **P0-02** — `proof_progress.json` + `proof_failure.json` substage tracking in `gt_run_proof.py`.
4. **P0-06/07** — `task_truth.json` authority; `reconciled_substrate_verdict.json`; metrics prefer task_truth.
5. **P0-11** — `artifact_deepswe/phase_policy.py` extracted; injected via `gt_agent.py`.
6. **Docs** — Bug register, `gt_gt.md` §17.10, `dispatch_tenpack_gt_on.sh` (script only).

## Tests
**17/17 passed** (dep_store_manifest, proof_progress, phase_policy, task_truth, gt_deep_metrics).

## Live runs
**None dispatched** (per user instruction).

## Open blockers
- Substrate image rebuild + digest pin required for P0-05 to take effect live.
- Go/Rust proof matrix must go green before Stage 2 tenpack.

## Next allowed action
1. Commit/push → rebuild substrate → re-proof 3 tasks → tenpack GT-on vs frozen baseline.

## Docs
- `.claude/reports/runs/validation_27387470440/GT_LSP_PROOF_HANDOFF.md`
- `.claude/reports/runs/validation_27387470440/ATOMIC_PRODUCT_BUG_REGISTER_20260612.md`
- `LATEST_TASK.md`

---

# Session Summary (previous)

## Date / Time
2026-06-12 — CP011–015 trajectory controller shipped + tenpack dispatch blocked on substrate proof

## Branch / Commit
`gt-trial` @ `df4c37c5` (pushed to `hbali-stack/groundtruth`).

## Objective
Ship §17.8 controller stack; validate on 10-task bugfree tenpack.

## What shipped
Commit `df4c37c5`: CP011–015, P6 verifier retry, P7 trajectory scorecard. Plan-scoped pytest **84/84** before push.

## Live runs
- Language smoke `27385688504`: pass on pinned digest.
- Tenpack `27386082651`: **failed** at substrate proof (`gt-run-proof rc=2`).

## Next allowed action
Triage substrate proof → fix (this session) → re-dispatch tenpack.

---

# Session Summary — HYBRID LANE-SPLIT (data-plane / control-plane bulkhead) — SUPERSEDED, see top

> **SUPERSEDED by the 8-commit arc at the top of this file (commit `35a3fb17`).** This was the
> in-flight draft written before the commit landed; its provisional counts ("6 tests", "22 passed",
> "122 gt_mini_patch tests pass") are stale. Final figures: **7 hybrid tests** in
> `test_hybrid_lane_split.py` (incl the negative control), **31 pytest GREEN** (7 hybrid + 16 RC5 +
> 8 oracle-LIPI). Retained below for history.

## Date / Time
2026-06-13 (gt-trial branch)

## Objective
Implement the data-plane/control-plane bulkhead (Nygard, *Release It!*) in
`artifact_deepswe/gt_mini_patch.py::_augment_output` oracle route: Lane A
(contract/evidence/cochange) delivers EARLY + isolated; Lane B (oracle steer
gate) runs AFTER, wrapped so a gate crash cannot undo Lane A's delivery.

## Implementation changes
- `_oracle_bstate()` + `_oracle_content_hash()` helpers hoisted (shared dedup
  key for both lanes); `_oracle_gate_blocks` now calls `_oracle_bstate()`.
- `_lane_a_deliver(out, cmd, lane_a, *, krel, event)`: per-producer try/except;
  correct-or-quiet gate = non-empty AND content+state hash not already in the
  shared `_oracle_delivered_hashes`; appends + `_ledger_note_delivery` +
  `_runtime_ledger_record`; commits l3b.evidence budget on delivery (D1 re-wire).
- `_ProductSignalOutcome` fallback stub gains `SUPPRESSED_DUPLICATE` (parity).
- `_augment_output` oracle block: l3.contract + l3.cochange + l3b.evidence
  collected into `lane_a` and delivered via `_lane_a_deliver` BEFORE the Lane B
  steer pool; entire Lane B section (phase filter + gate + latch re-arm + winner
  append) wrapped in ONE try/except (never re-raises; GT_META to stderr).

## Tests / runs executed
- NEW: `artifact_deepswe/tests/test_hybrid_lane_split.py` (6 tests, all DRIVE
  real `_augment_output`): fault_injection (gate raise -> contract still
  delivered), fault_injection_phase_filter_crash, no_double_ship (+self-dedup),
  no_flood (+lane_b_single_steer). Negative control proven: Lane A neutered +
  gate crash -> contract len 0 (0/8 mode reproduced); Lane A intact -> len 171.
- `python -m pytest artifact_deepswe/tests/test_hybrid_*.py artifact_deepswe/tests/test_rc5_*.py -q` -> 22 passed.
- Updated `tests/test_oracle_lipi_audit_fixes.py::test_outranked_cochange_rearms_and_delivers_later`
  to the new bulkhead semantics (contract+cochange both deliver turn 1; turn 2
  dedup-suppressed). 8/8 in that file.

## Result
Lane split landed; data plane survives a control-plane crash (proven). 122
gt_mini_patch tests pass.

## Regressions
None introduced. 3 PRE-EXISTING failures (baseline-identical, classified by
git-stash A/B): `test_lang_family_classifier` (missing `_lang_family` attr),
`test_no_test_evidence_wired_into_augment_output`, and
`test_unmet_obligations_raise_severity` (order-dependent intra-file
contamination — fails on baseline too).

## Open blockers / Next allowed action
DEFINITION OF DONE: unit-green is NOT done. The LIVE WITNESS run on the post-fix
binary is owed (task #6) — assert the contract reaches the agent on a real
DeepSWE turn and a control-plane crash loses only the steer.

---

## 2026-06-15 (cont.) — /goal rust-parity pass: LSP dark-gap root-caused + fixed; live witness billing-blocked

**Branch:** gt-trial **Commit:** `fa728e46` (this pass) on the chain `…8ab990e3 → fa728e46`
**Objective:** continue the 5-language parity loop (/goal) — run rust on the ONE substrate, §4-audit it,
fix every gap it surfaces, generalized. (py: gt_caused(FAIR-PROBE) · go: substrate-parity ✓ · rust: this pass.)

**What this pass established (decisive, evidence-pinned):**
- **fixes #1 (5-lang type-def label widening) + #3 (Rust/Go RAISES) GENERALIZE on the live rust graph:**
  rust type-defs **4224** (Class 2056 + ImplBlock 2168), RAISES **409**, all depth edge-types present.
  Rust depth+nodes+embedder are at parity with go.
- **The rust LSP "dark" gap (lsp=0, det_pct 65.7%) is a PLUMBING defect, NOT GT logic.** LIPI avenue-4:
  rust-analyzer spawns `cargo metadata`; `cargo`/`rustc` were not on PATH (`exit 127`) → no project model
  → 500 empty go-to-def probes → `project_ready=False` → lsp=0. The readiness budget was a red herring;
  the earlier "budget didn't apply" reading came off a STALE cert (07:55 < re-run 08:11) — corrected.
- **Proof the GT LSP code is correct:** go's `gopls` converts **331 CALL edges** via the IDENTICAL
  resolve.py path (archived go graph). rust just lacked its toolchain on PATH (rust isn't system-installed).

**Fix (commit `fa728e46`, both delivery paths, generalized):** prepend the extracted
`…/rustup/toolchains/*/bin` to PATH before the rust LSP pass (`railway/codespace_deepswe_run.sh`) and lead
the in-container PATH with it (`.github/workflows/deepswe_full.yml`). LIPI-clean (4 avenues). Proven at the
binary: cargo `exit 127 → 101` once the toolchain hit PATH; rust-src present; rustc 1.92.0 resolves.

**Metrics before:** rust lsp=0, det_pct 65.66%, name_match 11517. **Metrics after:** UNWITNESSED — see blocker.

**OPEN BLOCKER (hard, external — needs user action):** the hbali-stack codespace
`sturdy-space-yodel-j7x5479vpjxhq59g` hit **HTTP 402 (billing) mid-validation** and will not restart. This
blocks ALL remaining live /goal work: the rust `lsp 0→>0` witness, the rust trajectory §4 (component
tables), and the ts + js runs. harneet2512's codespace is stale and out-of-policy (run only on hbali-stack).
Resolve hbali-stack codespace/Actions billing, then re-run rust to witness lsp 0→>0 + det_pct climb, and
proceed to ts/js.

**Not done (honest):** rust LSP gap is root-caused + fixed but NOT witnessed closed — it remains flagged by
`gt_layer_audit.py` (correct: green is a hypothesis until the observable). `gt_caused` for rust = PENDING
(trajectory blocked). Parity is NOT yet proven on all 5 langs.

**Next allowed action:** (user) restore hbali-stack billing → re-run rust on the ONE substrate → confirm
`lsp>0`/det_pct↑ → trajectory §4 → ts run → js run → converge. No new code needed for rust before the witness.
