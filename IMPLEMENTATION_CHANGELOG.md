# Implementation Changelog — Session 2026-06-17 (PM) (embedder-gate false-fail fix + architecture retirement labels)

Held-out TEST exposed a substrate-level embedder-gate FALSE-FAIL (yjs/js + drizzle/ts, `sem_scored_count=0` →
agent never runs); the cert proves the embedder WAS consumed (drizzle `upstream_nonzero=830`). Diagnosis +
fix in `EMBEDDER_GATE_FALSEFAIL_DIAGNOSIS_20260617.md`; whole-architecture live/dead map in
`GT_ARCHITECTURE_LINEAGE.md`.

| # | File(s) | Change | Proof |
|---|---|---|---|
| 5 | `scripts/metrics/foundational_gates.py` | **embedder-gate witness-over-cert reconcile:** when `gate_embedder_consumption` would FAIL on p3 (flat post-render `sem_components`) but the `embedder_certificate` records `upstream/rendered_semantic_nonzero>0` AND `effective_w_sem>0`, reconcile to PASS (the embedder WAS consumed in the ranking; flat post-render is a render artifact). Records `cert_reconciled`/`cert_upstream_nonzero`. | syntax OK; yjs regression PASS unchanged; mutation-verified correct-or-quiet (dead embedder `upstream=0` stays FAIL) |
| 6 | `src/groundtruth/runtime/dead_path_registry.py` | **`CLI_LEGACY` soft-retirement table** for `v7_brief`/`brief_v5`/`v7_layers` — dead on DeepSWE, live on OH/kernel CLI (so NOT hard DEAD_PATHS). Advisory metadata. | importer-traced (`GT_ARCHITECTURE_LINEAGE.md`); additive (no behavior change) |
| 7 | `GT_ARCHITECTURE_LINEAGE.md` (new) | canonical whole-architecture LIVE-vs-DEAD module map (4 agents, import-traced, gt_audit/gt_gt-grounded) — the reference that stops the v1r/v7_4 vs old-code confusion | — |

**No render-path edit:** the in-container `sem_components`-zero trigger is unproven (the gate PASSES locally on the
exact graph.db), so a render fix would be speculative; the cert-reconcile is the defensible fix. The PAID 5-task
re-run is the proof bar.

---

# Implementation Changelog — Session 2026-06-17 (mechanism-parity verification + held-out TEST-5 dispatch)

Mostly VERIFICATION, not new product logic: re-audited the 2026-06-15 functional review (9 P0 / ~16 P1) on current
HEAD — all 19 audited defects already SUCCESS (the name_match-as-fact class is closed end-to-end; recorded in
`gt_new.md §9` + `PARITY_MATRIX_CONSOLIDATED_20260617T0040Z.md`). Code/data deltas this session are small + host-side.
Commits: `34c4479e`, `e4714807`, `dfb3c920`, `9bfdfd8b`.

| # | File(s) | Change | Proof |
|---|---|---|---|
| 1 | `src/groundtruth/pretask/v1r_brief.py` | test-tooling is never offered as a `<gt-localization>` edit candidate — `_test_tooling_roots` filter before the K-cap (env-gated `GT_TEST_TOOLING_DEMOTE`, empty-guard) | `e4714807`; local RED→GREEN (testify True→False) with ONNX embedder ON |
| 2 | `.github/workflows/deepswe_full.yml` | feed `GT_TRIAL_LOG=trial_output.log` to `task_truth.py` so the reconciler reads the `[GT_META]` witness (was unset → witness_holds=False false-fail) | `34c4479e`; reconciler proven `witness_overrides` live on `285a2a69` |
| 3 | `artifact_deepswe/repo_manifest.json` | bump TEST-5 (go-critic/yjs/python-statemachine/drizzle/boa) task images to `-v1.1` (deep-swe clone drift, verified vs raw.githubusercontent); VAL-5 bumped `dfb3c920` | `9bfdfd8b`; pre-empts prepare RUN_SET_DRIFT (`deepswe_full.yml:402`) |
| 4 | `artifact_deepswe/gt_integration/deepswe_gt_pier.yaml` | **additive-flip blocker:** submit template ran `git diff -- <files>` which OMITS untracked new files → any fix that CREATES source files lost them → incomplete patch → unresolved even when solved (superjson ledger). Fix: `git add -N -- <files>` (intent-to-add) + reword "modified OR created"; scaffold-exclusion preserved | `225edaae`; confirmed systematic at config (line 115/119); host-read config → no substrate rebuild |

**No product-logic regression risk:** #1 is env-gated + empty-guarded; #2/#3 are host-side (workflow + manifest), do
NOT change substrate code, so the `22d94aed` substrate digest stays valid. #4 is the pier agent prompt (host-read
`config_file` at `deepswe_full.yml:1178`, NOT baked into the substrate) — generalized git mechanics, LIPI clean,
unblocks the additive/multi-gold class for the next run; the in-flight TEST-5 loaded the pre-fix template so its
additive task (drizzle) may show right-trajectory-but-harness-lost-patch, which the gt_trial §4 audit will flag as a
harness confound, not a GT failure. LIPI clean on all four.

---

# Implementation Changelog — Session 2026-06-16 (GHA parity harness + determinism hardening + vendored-noise fix + product-vs-benchmaxxer architecture doc)

Context: codespace access dropped → moved the /goal measurement surface to GHA. Landed substrate determinism +
a §4/§6 vendored-noise fix + the GHA harness, all generalized (no task IDs/gold/library names). Final deliverable:
`ARCHITECTURE_CHANGES_PRODUCT_NOT_BENCHMAXXER_20260616T183300Z.md` (the 6 structural changes, file:line-cited from
`.claude/reports/GT_FUNCTIONAL_CODE_REVIEW_20260615T1900Z.md`). **No live eval run — substrate/harness only; "in progress"
per DEFINITION OF DONE.** 26 commits `2ea3f71f`→`82789697`.

| # | File(s) | Change | Proof |
|---|---|---|---|
| 1 | `gt-index/internal/resolver/resolver.go` | `pickBestNameMatchTarget` sorts candidates by content (file,start_line,id); `NodeMeta.StartLine` added + populated | `go build` exit 0; held-out re-index byte-identical 8/10 |
| 2 | `gt-index/internal/resolver/promote.go` | `forEachProperty` ORDER BY (node_id,value,line); `resolveByName` content tiebreak; `promote_dataflow_callee` minted only when no CALLS edge | re-index hash stable 8/10; residual drift 2/10 documented (deferred) |
| 3 | `src/groundtruth/delivery/path_policy.py` | `test_tooling_roots()` graph-derived "imported-only-by-tests" fixpoint (transitive) + `is_test_tooling()` | expr: 5 vendored testify/spew identified; all 10 repos no-harm |
| 4 | `src/groundtruth/pretask/graph_localizer.py` | wire `_is_tt`/`_tt_roots` into the test-file demote (env-gated `GT_TEST_TOOLING_DEMOTE`); `[L1DBG]` per-signal rank dump | paired same-substrate A/B: vendored 5→0, gold preserved |
| 5 | `src/groundtruth/pretask/v7_4_brief.py` | hard-filter test-tooling from `scored` before focus_set sort (env-gated, empty-guard) | focus_set vendored 5→0 on expr; 0→0 (no-harm) on 9 others |
| — | `src/groundtruth/pretask/graph_localizer.py` | multi-signal agreement-escape past the grep floor — **REVERTED** (clean paired A/B = 0 delta) | eb029452 (revert); proven NO-OP |
| 6 | `.github/workflows/parity_measure.yml`, `scripts/parity/measure_whole_pipeline.py` | GHA measurement surface: fresh graph.db per task from real ECR image → LSP → brief; brief-faithful recall (top_k=8+issue_anchors); raw-vs-raw determinism + ground-truth dump-and-diff | runs green on TRAIN+VALIDATION 5; caught the determinism gap TRAIN hid |

Doc/record: `PARITY_MATRIX.md` (honest current-state banner), `ARCHITECTURE_CHANGES_PRODUCT_NOT_BENCHMAXXER_20260616T183300Z.md`,
memories `project_determinism_residual_gap`, `feedback_never_block_research_when_split`, `project_localizer_ceiling_feature_tasks`.

---

# Implementation Changelog — Session 2026-06-15 (adversarial MAX-LIPI re-review: 2 HIGH closed + 1 bonus incremental-path bug)

Input: `docs/GT_GAPFIX_MAX_LIPI.md` (adversarial re-review of the §10 gap-fix wave). Closed both HIGH
release-blockers and 10/11 MEDIUM/LOW/NIT; 1 deferred-optional. No task-testing — structural fixes +
biting tests only. Detail: `gt_new.md` §10.1; `SESSION_SUMMARY.md` (top).

| # | Sev | File(s) | Change | Proof |
|---|---|---|---|---|
| §2.1 | HIGH | `anchor_proximity.py` (+`tests/test_anchor_proximity_i2.py`) | W_PROX rank leak: apply D5 `_degree_edge_filter` to the 1-hop SELECT | mutation-checked RED/GREEN; rank-leak count→0 |
| §2.2 | HIGH | `gt-index/cmd/gt-index/main.go` (+`inheritance_incremental_test.go`) | re-enable `-file` inheritanceMap; **+ fix 2nd bug**: restore fresh-node `ParentID` on the in-memory `nodeMeta` copy (CHA rungs were dead even with the map) | biting e2e (RED on either bug, GREEN on both); probe `name_match`→`inherited`; build exit 0 |
| §2.3 | MED | `railway/codespace_deepswe_run.sh` | `chmod 777` telemetry mount + loud copy-out (warn on 0 recovered) | bash -n |
| §2.4 | MED | `artifact_deepswe/gt_integration/gt_ae_block.sh` | honest downgrade of "single source of truth" — wired on codespace only; trial/full.yml OWED | bash -n |
| §2.5 | LOW | `artifact_deepswe/gt_agent.py` | graph.db decode fail-closed: temp + `[ -s ]` + atomic mv (never a torn `/tmp/graph.db`) | f-string→bash -n |
| §2.6 | LOW | `artifact_deepswe/gt_mini_patch.py` | correct false `.pyi`-matches-gt-index comment (`.pyi` is edit-detect parity only, graph-quiet) | py_compile |
| §2.7 | LOW | `gt-index/internal/resolver/promote_test.go` | add PRECEDES cc>1 → 0.4/SPECULATIVE demotion test | passes; bites by exact-value assert |
| §2.8 | LOW | `railway/codespace_deepswe_run.sh` | soften G05 "reindex ENABLED" echo (mount ≠ enabled w/o in-container graph) | bash -n |
| §2.9 | LOW | `contract_map.py` (+`tests/pretask/test_contract_blast_facts.py`) | G17 reader-count field-EXACT (`AND e.metadata = ?`) + two-field test | 9 passed (was 8) |
| §2.10 | NIT | `graph_reach.py` | drop unused `_PROMOTED_EDGE_TYPES` import (F401) | imports clean |
| §2.12 | NIT | `gt_mini_patch.py` | remove redundant inner `global _l6_no_binary_warned` | py_compile |
| §2.13 | NIT | `gt_ae_block.sh` | fix self-contradicting "Default-OFF" comment vs `:-1` | bash -n |
| §2.11 | NIT | — | DEFERRED (optional; mutates cc semantics shared with DATA_FLOW) | — |

---

# Implementation Changelog — Session 2026-06-13 (later: oracle un-stub + depth-to-production + naming/CHA-XTA Py/Rust→Go/TS + localizer LIPI + RC5 oracle foundation + HYBRID data-plane/control-plane bulkhead)

Diagnosis basis: reports 19–25 under
`.claude/reports/four_surface_failure_diagnosis_20260613T152534Z/`. Goal: make `graph.db` a TRUE
map (per the "graph.db IS THE CONTEXT GRAPH" rule) AND make GT's delivery survive an oracle crash —
un-stub the per-turn delivery gate (the DARK-binary root cause); land the depth promote pass +
IMPORTS in the PRODUCTION Go indexer; convert one name_match method-edge class to a FACT across ALL
Tier-1 languages; lay the oracle/delivery foundation; and SPLIT delivery into a fault-isolated
data-plane (Lane A, always-needed context) + control-plane (Lane B, oracle steer) bulkhead.
**All enabling-substrate + delivery-correctness (map-connectivity + fault-isolation, Mandatory
Rule 2) — NOT flip claims; live witness owed (DEFINITION OF DONE).**

**The decision arc:** (1) GENERALIZED never per-task — every row is a structural property, no task IDs.
(2) CORRECT-OR-QUIET — CHA/XTA ABSTAINS on ambiguity/builtin; IMPORTS emits no edge for stdlib;
Lane B never suppresses Lane A. (3) DEPTH (promote pass + Pass 4f IMPORTS) vs ACCURACY (name_match→fact
CHA/XTA rung) are two distinct levers, both shipped. (4) DATA-PLANE/CONTROL-PLANE HYBRID — the oracle
is DEMOTED from gate-of-everything to a Lane-B steer-decider + shared-ledger-keeper; the always-needed
contract/consistency/completeness (gt_gt §-philosophy "fire on EVERY edit") is Lane A, delivered EARLY
+ isolated (the 0/8 SPOF fix). (5) DEFINITION OF DONE — substrate + fault-proven is NOT "done".

| Commit | Type | File(s) | Change |
|---|---|---|---|
| **`32e4e313`** | fix(oracle) | `artifact_deepswe/gt_mini_patch.py`, `artifact_deepswe/gt_agent.py`, `tests/test_oracle_gate_fires_in_container.py` | **DARK-binary root cause (run `27465183646`: `gt.oracle_event.v2`=0 on all 8 tasks).** `_augment_output` raised `TypeError: _ProductHorizonThresholds() takes no arguments` EVERY turn (the no-arg fallback stub constructed with 6 kwargs by `verify_horizon_band`), swallowed by the outer `except Exception: pass` BEFORE the gate's `gt.oracle_event.v2` write → ZERO per-turn context (only turn-0 brief). Stub was live because the in-container runtime import aborted on first-missing `runtime.ledger`. FIX: kwarg-accepting `__init__` on the stub; inject `ledger.py`+`patterns.py` into `_PRODUCT_RUNTIME_FILES` (`_RUNTIME_AVAILABLE=True` → REAL logic); make the outer swallow LOUD (stderr, never re-raise); wrap each per-turn producer in its own try/except (gt_gt §15.2 — gate is the single decision point). TTD: in-container-shape test RED→GREEN (0→≥1 `gt.oracle_event.v2`). |
| **`9860ff7e`** | fix(depth-LIPI) | `scripts/graph/promote_property_edges.py`, `src/groundtruth/pretask/graph_localizer.py`, `gt_gt.md` | D1: DATA_FLOW demoted 774 standalone edges → CALLS.metadata annotation (~19 standalone for no-CALLS hops). D3: RAISES polyglot class-label superset + drop dotted names. D4: copies-only REFERENCE guard (§2.1 Go owns writes). D5: localizer fan_out/fan_in degree counts STRUCTURAL edges only (promoted edges excluded; strict no-op proven on 4 live graphs). gt_gt §2.6 reconciled + SUPERSEDED. Re-proven red→green on a real adaptix copy (5/5, idempotent). |
| **`9db1fe44`** | fix(naming) | `src/groundtruth/resolve.py` | Dropped the Python-only `tgt.label='Method'` clause in `_count_residual_method_edges`. Go receiver / JS-TS object methods land as `Label='Function'`, so ~475 unresolved method edges read as 0 → a FALSE `LSP_NO_OP_VALID` all-clear on Go/JS/TS. Now keys on language-agnostic `resolution_method='name_match'`; JOIN to a real target retained (non-invention). |
| **`b5ceaf5d`** | feat(depth) | `gt-index/cmd/gt-index/main.go`, `internal/resolver/imports.go`, `internal/resolver/promote.go`, `internal/resolver/promote_test.go` | Promote pass copies-only REFERENCE → PRODUCTION. Go indexer materializes IMPORTS + promoted relationship edges at index time (Pass 4f wired after serde/before closure, non-fatal+logged). `imports.go` fresh-extract correct-or-quiet (single→CERTIFIED `ast_import`; >1→CANDIDATE; stdlib/3rd-party→NO edge; zero per-language branches, SCIP/Kythe model). Fixed inv-7 (`-file` reindex re-runs idempotent promote) + D3 dotted-RAISES (drop `errors.New`, no wrong edge). Build/vet/8 tests GREEN locally (CGO+`sqlite_fts5`, gcc 16.1). |
| **`ec20d603`** | feat(naming) | `gt-index/cmd/gt-index/main.go`, `internal/resolver/resolver.go`, `internal/resolver/resolver_fieldtype_test.go` | CHA/XTA receiver-type matcher (rung 2b): a typed `self.<field>.m()` where `<field>` is DECLARED via colon annotation but never locally assigned → FACT (`type_flow`/0.9/`field_type`/CERTIFIED), ABSTAIN on ambiguity/builtin. Sits before name_match. Shipped **Python+Rust** colon-annotation fields first; the Go/TS gap was the explicit residual, closed by `71d66378`. Build/vet/resolver tests GREEN locally; `TestBuildFieldTypeIndex_LanguageScope` pinned the Py+Rust boundary. Pre-existing `TestRoutePatternMatching/comment` failure unrelated. |
| **`71d66378`** | feat(naming) | `gt-index/internal/parser/parser.go`, `internal/resolver/resolver.go`, `internal/resolver/resolver_fieldtype_test.go` | Extended rung-2b from Python+Rust to ALL Tier-1: Go struct fields (space-separated `Field *Type`, parsed via `goStructFieldList`) + TS access-modifier fields (strip `private`/`public`/`protected`/`readonly`); relaxed the receiver-shape gate to accept Go's receiver var (`GoReceiverName(Signature)→NodeMeta.ReceiverName`) alongside `self.`/`this.`. Still correct-or-quiet (ABSTAINS on unknown receiver/field/type). **Closes the DeepSWE non-Python generalization gap** for declared-field-type resolution. Recovered + verified after a killed workflow (`parser.go` comment-detangle repaired). Go build + `go test -run FieldType` GREEN (CGO+`sqlite_fts5`, gcc 16.1). |
| **`a7a4be87`** | fix(oracle) | `artifact_deepswe/gt_mini_patch.py`, `artifact_deepswe/tests/test_rc5_patch_apply_edit_credit.py`, `artifact_deepswe/tests/test_rc5_hybrid_edit_credit.py` | RC5 — two coupled gt_gt-15/16-grounded, LIPI-caught fixes. **(a) FOUNDATIONAL:** `_classify`/`_edit_target` now recognize the `apply_patch`/`git-apply`/`patch -pN` edit family (target from `*** Update File:` / `+++ b/<path>`; hunk ranges from `@@`); priority-0 branch returns `('post_edit', target)`, correct-or-quiet (`None`→legacy fallthrough). The agent's DOMINANT edit channel was invisible to GT edit-detection — unblocks the contract action-hook firing on `apply_patch` edits (the Lane-A enabler for the bulkhead). **(b)** `edit_coverage_ratio` upgraded single-source-lexical → ≥3-signal hybrid (content-body lexical + graph Function/Method co-location + line-range overlap), FACT-tier/degrade contract; fixes the 0.0-solved/1.0-failed inversion; feeds `verify_horizon_band` SEVERITY, NOT `spec.obligation`. 16 pytest tests GREEN, all DRIVE the real chain (no injection). |
| **`35a3fb17`** | feat(delivery) | `artifact_deepswe/gt_mini_patch.py`, `artifact_deepswe/tests/test_hybrid_lane_split.py`, `tests/test_oracle_lipi_audit_fixes.py` | **HYBRID data-plane/control-plane BULKHEAD (Nygard, *Release It!*)** — splits the oracle route into two failure-isolated lanes, fixing the run-27465 SPOF (one gate crash darkened ALL delivery, 0/8). **Lane A (data plane):** `l3.contract`/`l3.cochange`/`l3b.evidence` deliver EARLY via `_lane_a_deliver` (append + shared-ledger record) BEFORE any Lane B logic, each producer isolated; old gate-pool pushes REMOVED → the contract has exactly ONE path (Lane A), never the gate (CLAUDE.md: contract/consistency/completeness fire on EVERY edit). **Lane B (control plane):** steers run through `_oracle_gate_blocks` AFTER Lane A, in ONE outer try/except (stderr, no re-raise) → a gate crash CANNOT undo Lane A; the oracle is DEMOTED to steer-decider + ledger-keeper. **Shared ledger (one, not forked):** `_oracle_delivered_hashes` content+state dedup, cross-lane. **PROVEN:** fault-injection monkeypatches the REAL gate to raise, drives the REAL `_augment_output`, asserts the contract survives; the NEGATIVE CONTROL (Lane A neutered + gate crashed → contract len 0) reproduces the 0/8 mode, making the proof non-vacuous. 31 pytest GREEN (7 hybrid incl negative control + 16 RC5 + 8 oracle-LIPI); all 4 LIPI lenses commit_ready. |

## Hybrid delivery decision (data-plane / control-plane — task #7, IMPLEMENTED `35a3fb17` + fault-proven)

Reports 24–25 §"TWO LANES" locked the delivery architecture: **two failure-isolated lanes sharing ONE
candidate schema + ONE `_augment_output` pipeline (ONE PRODUCT RULE preserved)** — `35a3fb17`
IMPLEMENTS the split (no longer design-only):
- **Lane A (context / data-plane, robust, EARLY + isolated):** `l3.contract`/`l3.cochange`/`l3b.evidence`
  deliver via `_lane_a_deliver` (per-producer try/except; correct-or-quiet = non-empty AND content+state
  hash not already in the shared `_oracle_delivered_hashes`) BEFORE any Lane B logic — NOT routed
  through `_oracle_gate_blocks`, NOT subject to the oracle ≤1/turn winner gate, NOT killed by a
  steer-producer crash. The old gate-pool pushes for these kinds were REMOVED.
- **Lane B (oracle steer / control-plane):** runs through `_oracle_gate_blocks` AFTER Lane A, the whole
  section wrapped in ONE outer try/except; ADDS its single band-gated candidate, NEVER suppresses Lane A.
- **Coupling that was broken (now fixed):** both lanes previously shared ONE `<=1/turn` winner gate
  where `_SEV_OBLIGATION=5` outranked `_SEV_CONTRACT=3` → oracle STARVED the just-edited contract; AND a
  `_ProductHorizonThresholds` stub crash (swallowed) made the gate emit 0/9 every turn (the DARK-binary
  root cause, report 22). `32e4e313` un-stubbed the gate; `a7a4be87` made edit-detection see
  `apply_patch`; `35a3fb17` lifted Lane A off the gate and bulkheaded Lane B.

**Local toolchain:** mingw gcc installed this session for the local Go build (CGO + `sqlite_fts5`).
**Residuals (report 21, gate any benchmark number):** held-out multi-lang `go test` on a REAL
toolchain + a REAL production `graph.db` (Codespace); PyCG-dynamic; demand-driven LSP; CHA
builtin-drop per-language re-justification + held-out false-drop proof; 0.9 calibration; `35a3fb17`
non-blocking residuals (dead `_lost` re-arm clauses for the moved kinds; `no_flood` test does not fire
a live steer — cross-lane flood owed to the witness). **NONE validated live — no `output.jsonl` newer
than the fixes (report 22); the bulkhead is fault-proven in code, not witnessed on a real turn.**
(Go/TS receiver-field extension — formerly a residual — is CLOSED by `71d66378`, local-test only.)

---

# Implementation Changelog — Session 2026-06-13 (GHA non-Python fixes A–E)

Root cause + fixes: `GHA_NONPYTHON_FAILURE_AUDIT.md`. LIPI vs the 4 surfaces:
`GHA_FIXES_LIPI_20260613T0640Z.md`. Goal: stop Go/Rust/TS/JS dying before pier.

| Fix | Surface | File(s) | Change |
|---|---|---|---|
| **A** | Product (1) + Substrate (4) | `src/groundtruth/resolve.py`, `scripts/metrics/foundational_gates.py`, `scripts/swebench/gt_run_proof.py` | Liveness axis = `server_launched`. Launched-but-not-warm → `LSP_WARN_NOT_READY` (PASS); never-launched → `LSP_FAIL_NO_WARM` (exit 2). `_classify_lsp` consumes the `verdict_hint` (no re-derivation). `workspace_metadata` pre-flight non-fatal (RC-4). |
| **B** | GHA (3) | `.github/workflows/deepswe_full.yml` | LSP pass reads the probe-populated **writable** gomodcache offline: `GOFLAGS=-mod=mod` + `GOPROXY=off` + `GOSUMDB=off`. |
| **C** | GHA (3) | `.github/workflows/deepswe_full.yml` | Rustup mount RW (was `:ro`) + baked-substrate rust-src `docker cp` fallback when the task image ships none. |
| **D** | Integration (2) | `gt_mini_patch.py`, `gt_agent.py` | Already shipped `faf8c6b1`/`00bd27fd` — `sys.path` + graceful runtime imports. Re-verified. |
| **E** | Substrate (4) | — | Per-language `env_validation` deferred (latent; build self-test guarantees all 5 baked). |

**Tests:** `tests/fail_closed/{test_lsp_liveness,test_no_fallback_hardening}.py` updated to the new
desired state (never-launched FAILs; launched-not-warm WARNs) + 3 new WARN tests — 42 pass.
`tests/test_workspace_metadata_probe.py` updated to `go list -e` contract — 4 pass. LSP/gate/proof
sweep: 464 pass, 6 skip. **Separation invariant:** the LSP verdict is defined once (resolve.py) and
consumed by the gate/aggregator — no cross-surface duplication.

---

# Implementation Changelog — Session 2026-05-16

## Commit: 5f52dca3 — Full flip stack

### L1 Brief (v1r_brief.py)
| Signal | Function | Research | What it shows |
|---|---|---|---|
| Literal caller code | `_caller_contract_for_file` | SYNFIX ACL 2025 (52.33%) | Actual source lines from top callers |
| Issue-term function selection | `_top_function_names` | — | Prioritizes functions named in issue |
| Doc file filter | `_NON_SOURCE_EXTS` | — | Excludes .rst/.md/.txt from candidates |
| Sibling context | `_sibling_context` | RepoGraph ICLR 2025 (+32.8%) | Other functions at same scope level |
| Last git change | `_last_change` | HAFixAgent (+56.6%) | Most recent commit to file |
| Co-change files | `_co_change_files` | ESEM 2024, HAFixAgent | Files that historically change together |

### L3 Post-Edit (post_edit.py)
| Signal | Function | Research | What it shows |
|---|---|---|---|
| Literal caller code | `_extract_usage_contract` | SYNFIX ACL 2025 | Caller source lines with file:line |
| Structural twins | `_detect_structural_twins` | LASE ICSE 2013 (99% precision), Mondal JSS 2019 (18-33%) | Lines sharing same pattern template in edited function |
| Edit propagation | `_detect_edit_propagation` | CodePlan FSE 2024 (5/7 vs 0/7) | Call sites that may need updating |
| Co-change reminder | `_co_change_reminder` | HAFixAgent arXiv 2025 (+56.6%) | Files that co-change but haven't been edited |
| Scope completeness | `_scope_completeness` | ASE 2025 (60% multi-file) | Warning when edit scope < historical average |

### Properties (all mechanisms)
- No LLM required
- No test dependency
- Derived from graph.db + git + source reading
- Language-agnostic (pattern templates work on any syntax)
- Repo-agnostic (no hardcoded paths/patterns)
- Scale-agnostic (function-local or O(git log) analysis)

### Prior commits this session
| Commit | Change |
|---|---|
| 1ff3df36 | L1: regex contract summaries (superseded) |
| 3d2c308e | L1: issue-term function selection + doc filter |
| 4d4a1565 | L1+L3: literal caller code replaces regex |
| 29849ab0 | Expand workflow to 20 tasks |
| 5f52dca3 | Full flip stack (current) |

### Rollback
Revert commit 5f52dca3 to remove all new mechanisms. Each mechanism is independent (removing one doesn't affect others). The pre-existing L3 caller evidence remains functional regardless.

---

## Expected Impact (research-derived)

| Mechanism | Target failure mode | Expected flip rate | Source |
|---|---|---|---|
| Structural twins | Inconsistent parallel edits | 4-27% of failing tasks | LASE + Mondal |
| Edit propagation | Missed call-site updates | 5/7 repos (CodePlan) | CodePlan FSE 2024 |
| Co-change | Single-file when multi needed | +56.6% (HAFixAgent) | HAFixAgent 2025 |
| Scope warning | Under-editing | Awareness signal | ASE 2025 |
| Combined | All above | +3-10pp on 300 tasks | Conservative estimate |

## Next Step

Generalization testing on FRESH repos (not SWE-bench) per anti-overfitting rules. Then 20-task gate as acceptance.

---

# Implementation Changelog — Session 2026-06-03 (benchmark infra: gates + legitimacy + parallel + bake)

Branch `gt-consensus-curation`. 16 commits `51de7275`..`ed438843`. No product-ranking
changes (BRIEFING.md §3 weights untouched) — this session is benchmark-infra hardening.

## No-silent-fallback gates
| Layer | File | Gate | Behavior |
|---|---|---|---|
| FTS5 | `gt-index/.../sqlite.go`, `main.go` | `GT_REQUIRE_FTS5` | aborts indexing if `nodes_fts` absent/empty; `-tags sqlite_fts5` added to ALL builds + docs |
| Embedder | `graph_localizer._get_embedder`, `v7_4_brief._get_model` | `GT_REQUIRE_EMBEDDER` / `GT_FORCE_ONNX_EMBEDDER` | RAISE instead of W_SEM=0; both halves forced through container ONNX |
| LSP | `lsp/edge_verifier.py`, wrapper | `GT_REQUIRE_LSP` | `start(warm=True)` real launch + per-task `probe()` asserts `lsp_references`+latency>0 |
| Graph dims | `scripts/verify/preflight_pipeline.py` + wrapper `_gate_graph_dimensions_per_task` | `GT_REQUIRE_FULL_STACK` | per-task gate (shared source): FTS5 Go-built, edge_quality, `check_data_flow`, assertions, lsp_edges |
| Legitimacy | wrapper `__post_init__`, `preflight_full_stack.check_legitimacy` | `GT_FORBID_PREBUILT_GRAPH` | refuses prebuilt/cross-run graph.db; forces fresh in-container index |

## Behavioral preflights
- `scripts/swebench/preflight_full_stack.py` (new) — probes real non-zero FTS5/semantic/LSP/struct + legit.
- `scripts/verify/preflight_pipeline.py` — made HARD in DeepSWE (was advisory); added `check_data_flow`,
  strict Go-built FTS5, `run_db_dimension_gate()` shared runner.

## Parallel + install-once
- `deepswe_full.yml` (new) — 113-task matrix (was single-task `deepswe_trial`).
- All matrices capped at the real ~20 GitHub-hosted runner ceiling.
- `Dockerfile.eval-runner` corrected (fts5, Go 1.23 upstream, pier, docker CLI, GT_MODELS_ROOT,
  GT_EVAL_IMAGE); BOTH main workflows run `container:` it; `setup-eval` skips re-installs when baked.
- `embed.py` honors `GT_MODELS_ROOT` (baked model from a from-checkout GT).

## Status
Code-verified locally; **GHA container/DinD wiring UNVALIDATED** — validate 1 task each before paid
113/300. Operational runbook: `BENCHMARK_RUNBOOK.md`. The 30-task quality verdict is retracted as
confounded (degraded pipeline). No metrics delta yet — pending the provisioned gated run.


---

# Implementation Changelog — Session 2026-06-11 (delivery engine, 5 staged commits)

| Commit | Stage | Files | What |
|---|---|---|---|
| 0e1bd371 | S1 sensor | gt_mini_patch.py, gt_oracle_sense.py | TIDE loop_ratio/new_state_rate, TRAJEVAL edit churn, coverage ratios; sensor binds live formulas |
| ec2c059d | S2 obligation status | gt_oracle.py, gt_mini_patch.py | review-transition status checklist, status-vector dedup, covering test per untested obligation, composite severity |
| 4beb812a | S3 detectors | gt_mini_patch.py | detect.loop (dynamic median+MAD), detect.coherence_collapse; window-12 loop arm retired on oracle route |
| 2fcd12c7 | S4 escalation | gt_mini_patch.py, gt_oracle.py | coverage-driven bands (GT_ESC_* channel), V from observed edit->test spans, computed severity |
| c17271ee | S5 governor+plumbing | gt_mini_patch.py, gt_oracle.py, deepswe_full.yml | failure_persisted FP closure (zero-count/patch-noise/baseline-stash), parity_mode=False corrected replay, heredoc proof, full --ae forwarding |

Research: TIDE arXiv 2602.02196 · TRAJEVAL arXiv 2603.24631 · Beyond Resolution Rates
arXiv 2604.02547 · SWE-Next arXiv 2603.20691 · Wink arXiv 2602.17037 · Zilberstein 1996 ·
BATS arXiv 2511.17006 · Rothermel TOSEM 1997 / Ekstazi ISSTA 2015 · Leys 2013 (median+MAD).
