# Atomic product bug register - 2026-06-12

Branch: `gt-trial`

Code HEAD while writing: `0b093e1d` (closure session handoff: `ATOMIC_REGISTER_HANDOFF_20260611.md`)

This is the deeper register after the one-surface map. The prior "21 issues"
answer was too coarse: it counted bug families. Product launch readiness needs
atomic bugs, because a single family can hide several reader/writer mismatches.

Rule for this register:

```text
one row = one launch-relevant way GT can lie, soften, overclaim, or split truth
```

Benchmarks should only run after the earliest violated boundary is deterministic
and named.

## Count

Current atomic launch bugs/gaps identified: **64**.

Severity buckets:

- **P0 launch blocker:** 14
- **P1 product correctness:** 29
- **P2 observability / doc truth:** 21

This number will change as logs from the current run are read. The point is not
the number; the point is that every row has an owner boundary.

## P0 Launch Blockers

| ID | Boundary | Atomic bug | Current code / evidence | Why it blocks launch | Fix shape |
|---|---|---|---|---|---|
| P0-01 | GHA -> substrate proof | Real DeepSWE tasks still fail before agent at `GT substrate proof` | Run `27387470440`: Go (`LSP_FAIL_NOT_READY`) + Rust (`LSP_FAIL_NO_WARM`) at L2 LSP pass; ts arktype proof passed | No trajectory exists on Go/Rust until dep boundary fixed | Fix P0-04/P0-05; re-dispatch matrix |
| P0-02 | Proof failure diagnostics | `GT_RUN_PROOF_FAIL rc=2` collapses many causes | `.github/workflows/deepswe_full.yml` emits one marker for boundary/leak/missing-baked-dep | Launch triage requires substage without reading huge logs | Emit `proof_failure.json` with stage, language, tool, exception, missing artifact |
| P0-03 | Smoke equivalence overclaim | Language smoke green does not prove DeepSWE task proof | `gt_language_smoke.yml` lacks issue/deps/task-image/PATH handoff | A green preflight can hide real-task proof failures | Rename docs/status to "substrate preflight"; add DeepSWE proof parity test |
| P0-04 | Go proof dep-store extraction | **TRIAGED** run `27387470440`: `abs-module-cache-flags` + `abs-stepped-slices` — gopls warm but `project_ready=False`, 73/73 `no package metadata`, verdict `LSP_FAIL_NOT_READY`. Log: `no Go module cache found in task image` — hardcoded `/root/go/pkg/mod` miss on mars-base; empty gomodcache + `GOPROXY=off` | Go proof cannot resolve modules | Dynamic `go env GOMODCACHE` discovery + `dep_store_manifest.json` fail-closed for Go |
| P0-05 | Rust proof LSP toolchain boundary | **TRIAGED** run `27387470440`: `boa-hierarchical-evaluation-cancellation` — `lsp_warm=0`, `LSP_FAIL_NO_WARM`. stderr: missing rust-src from sysroot; proc-macro API 6 vs RA 5 (substrate RA `2024-08-12` vs task rustc 1.92.0); `linker cc not found` | Rust proof cannot warm rust-analyzer | Dynamic cargo/rustup discovery; rust-src in task images; bump RA; bake gcc in substrate |
| P0-06 | Task truth not globally authoritative | `task_truth.json` exists but paired metrics still reads `outcome.json` directly | `compute_paired_metrics.py` `_find_outcome_path()` + `m.resolved = reward > 0` | Reconciled truth can be bypassed by old reports | Make all report readers prefer `task_truth.json` |
| P0-07 | Raw graph cert can outlive reconciliation | Raw `GRAPH_FAIL_MISSING_HANDOFF` can still be visible after witness override | `task_truth.py` reconciles; raw cert remains in artifacts | Users/debuggers can see two verdicts and choose wrong one | Add post-agent `reconciled_substrate_verdict.json`; dashboards read it |
| P0-08 | "Pre-submit gate" overclaim | Code injects interventions; no proven finish/submit hard block | `gt_agent.py` retry note; `gt_mini_patch.py` horizon gate | Docs may claim agent cannot submit when code only warns/injects | Either wire finish hook blocker or rename to pre-submit intervention |
| P0-09 | "Verifier-fail retry" overclaim | Current retry is repo-native visible test retry, not official hidden verifier retry | `gt_agent.py` comments explicitly avoid pier verifier hidden tests | Product architecture can falsely mark official retry built | Split terms: self-verifier retry vs official verifier repair loop |
| P0-10 | Runtime policy owner drift | Docs say CP013/014/015 live in `gt_oracle.py`; code lives mostly in `gt_mini_patch.py` | `gt_gt.md` section 17.9 vs runtime patch code | Fixes can land in wrong surface | Correct docs and add owner table |
| P0-11 | Context policy not first-class | Phase allowlist exists only as globals in patch | `_PHASE_POLICY` in `gt_mini_patch.py` | No shared product policy to test across adapters | Extract policy object/schema and test it |
| P0-12 | Consumption semantics split | Runtime in-memory ledger and post-run ledger both imply "consumption" | `gt_mini_patch.py` `_ledger_note_delivery`; `consumption_ledger.py` post-run | Runtime heuristic can be mistaken for proof | Rename/report separate fields |
| P0-13 | Calibration corpus too small for launch | Horizon thresholds are from 9 frozen failed trajectories | `gt_mini_patch.py` comments mark placeholder calibration | Can be product-fragile on new repos/agents | Treat as default heuristic; add corpus/versioned calibration artifact |
| P0-14 | Checkpoint doc protocol violated | Recent proof commits did not include checkpoint docs in same commit | `gt_gt.md` 17.6 requires docs in same commit | Handoff can drift from code in launch critical path | Backfill exception doc or amend process with follow-up docs |

## P1 Product Correctness Bugs

| ID | Boundary | Atomic bug | Current code / evidence | Risk | Fix shape |
|---|---|---|---|---|---|
| P1-01 | GHA issue input | Empty issue fails closed, but issue source is not persisted as structured metadata | Workflow writes `/tmp/issue.txt`; artifact copies it | Later debugging sees text but not source precedence/status | Add `issue_manifest.json` with source, length, hash |
| P1-02 | GHA dep stores | Dep-store copy logs are echo-only and not structured | `docker cp ... || echo no cache` | Proof failures may not know if dep stores were present | Emit `dep_store_manifest.json` |
| P1-03 | GHA task root | Repo root detection falls back to `/testbed` | `ROOT=${ROOT:-/testbed}` | A wrong fallback can copy wrong source silently if `.git` detection fails | Fail closed if no `.git` unless task image explicitly supports root |
| P1-04 | Proof memory | rc 137 gets marker, but memory use is not captured | Docker `--memory=10g` and `GT_PROOF_OOM` | OOM diagnosis lacks graph/embedder/LSP stage | Add stage heartbeat before each expensive proof stage |
| P1-05 | Proof stage granularity | `gt-run-proof` does many stages in one process without persistent stage status | `gt_run_proof.py` main does env, index, LSP, certs, brief | Nonzero exit loses exact last successful stage | Write `proof_progress.json` after each stage |
| P1-06 | LSP product status | Readiness fields exist but dashboards can still compress to warm/pass | `foundational_gates.py`, `resolve.py` | Old "warm green" bug can return via reporting | Add schema test that forbids single `lsp_warm` as product verdict |
| P1-07 | Embedder truth | Metrics prefer cert, but fallback local probe still exists | `gt_deep_metrics.py` `_from_embedder` | If cert missing, fallback may compare wrong environment | Mark fallback as diagnostic-only in output, never product verdict |
| P1-08 | Graph handoff | Witness reconciliation is duplicated conceptually in outcome and task truth | `deepswe_outcome.py` and `task_truth.py` | Future change can diverge | Centralize reconciliation function |
| P1-09 | Adapter brief | `_substrate_brief()` consumes `brief.txt`, but brief provenance is not part of delivered instruction metadata | `gt_agent.py` writes `delivered_instruction.txt` | Hard to prove delivered brief hash == substrate brief hash | Add delivered brief hash and substrate brief hash comparison |
| P1-10 | Adapter witness | Workflow checks witness string via grep | `.github/workflows/deepswe_full.yml` witness step | Grep is brittle against formatting changes | Emit structured `adapter_witness.json` |
| P1-11 | Adapter error class | `DeepSweAdapterError` is surfaced by grepping jobs/log | Workflow greps `DeepSweAdapterError` | Pier result schema changes can hide errors | Parse result.json exception field structurally |
| P1-12 | Mini patch route | Legacy path remains behind `GT_ORACLE_ROUTE=0` | `gt_mini_patch.py` legacy appends | Product can accidentally run old behavior | In proof/substrate mode, fail if oracle route is disabled |
| P1-13 | Mini patch policy | Event-bound candidates bypass phase filter | `gt_mini_patch.py` cands filter | Correct for view/edit, but can defeat policy if event classification wrong | Add tests for every event-bound candidate class |
| P1-14 | Mini patch consumption | `_ledger_note_delivery` uses triggering command, not next action | Runtime ledger code | Can mislabel same-turn action as consumption | Restrict runtime ledger wording to suppression heuristic |
| P1-15 | Mini patch dedupe | `_DELIVERED_FACTS` dedup is line-level string only | `_budget_trim()` | Semantically duplicate facts with wording changes survive | Add stable fact ids where possible |
| P1-16 | Mini patch budget | `max_tokens * 4` char proxy is fixed | `_budget_trim(max_tokens=500)` | Non-English/code-heavy payload token estimates can drift | Report actual char + estimated token; cap by char explicitly |
| P1-17 | Action templates | Graph-to-action covers only a few text patterns | `_translate_to_action()` templates | Many graph facts remain un-actionized | Add template coverage for callers/callees/contracts/assertions/cochange |
| P1-18 | Obligation persistence | Obligation lifecycle lives in runtime memory | `ObligationTracker` singleton in patch | Post-run truth cannot fully reconstruct lifecycle | Emit obligation status events to oracle jsonl |
| P1-19 | Obligation evidence | Task truth only stores deep metric summary, not full obligation vector | `task_truth.py` deep_metrics subset | Cannot audit per-clause status from task truth | Add `obligation_status` field to task truth |
| P1-20 | Verification horizon | Exact test names removed, but file paths are also avoided wholesale | `_render_verify_emission()` | Guidance may be too vague for product users | Render behavior + edited module category without exact hidden test names |
| P1-21 | Retry runner | Auto-detected `npm test` can be broad/slow/noisy | `gt_agent.py` `_RETRY_TEST_AUTODETECT` | Retry may burn budget on irrelevant tests | Use repo-native targeted category when safe; keep no exact hidden names |
| P1-22 | Retry feedback | `<test-feedback>` includes command and output tail | `_format_test_feedback()` | Visible test output is okay, but can be long/noisy | Add sanitizer for absolute paths/secrets and deterministic truncation metadata |
| P1-23 | Retry equality | Retry applies to baseline and GT, but GT adds gate note | `_run_with_test_retry()` adds GT note only when not baseline | Harness is not fully arm-neutral after failure | Document this as GT intervention or remove from harness-neutral claim |
| P1-24 | Trajectory fallback | Mini trajectory fallback can use first scoped hit by task id | `_find_miniswe_trajectory()` | Ambiguous paths can attach wrong trajectory in weird artifact dirs | Require exact task dir when running from collected artifacts |
| P1-25 | Deep metrics resolved | Deep metrics can infer resolved from reward.txt, outcome separately infers from result | `gt_deep_metrics.py`, `deepswe_outcome.py` | Split resolved truth | Make deep metrics consume task truth outcome |
| P1-26 | Paired metrics gold proxy | Metrics use edited files as "gold" proxy | `compute_paired_metrics.py` steps-to-gold-edit/read | A wrong patch can define its own "gold" | Rename to steps-to-edited-file or use known gold only when legitimate |
| P1-27 | Paired metrics outcome | `bool(task_record.get("reward",0)>0)` ignores normalized failure class | `compute_paired_metrics.py` | INFRA can become unresolved instead of excluded | Consume task truth denominator fields |
| P1-28 | Patch hygiene adoption | Classifier exists but not guaranteed in all reports | package/convert scripts vs reports | Noise patches can still pollute analysis | Require patch hygiene block in task truth/report summary |
| P1-29 | Artifact integrity | Zero-byte canonical trajectory with mini fallback is classified, but result readers still choose their own path | `task_truth.py` vs other scripts | Partial artifacts can be double-counted or missed | Centralize artifact resolver |

## P2 Observability / Documentation Bugs

| ID | Boundary | Atomic bug | Current code / evidence | Risk | Fix shape |
|---|---|---|---|---|---|
| P2-01 | Docs | `gt_gt.md` section 17.8 says pre-submit gate shipped, but enforcement is not hard submit block | Architecture docs | Overclaim | Reword or implement hard block |
| P2-02 | Docs | `gt_gt.md` section 17.9 primary surface names are inaccurate | Architecture docs | Wrong owner | Correct map |
| P2-03 | Docs | The high-level handoff says CP011-015 shipped at `df4c37c5`, while current local HEAD has later doc commits | Handoff trail | Reader confusion | Add latest local doc commit pointer |
| P2-04 | Docs | One-surface high-level doc existed without the deeper atomic register | Prior commit | Not enough debug depth | This file closes that gap |
| P2-05 | Workflow summary | Language smoke summary wording was fixed, but old logs still contain old wording | Historical GHA logs | Misread old runs | Docs must tie conclusions to commit id |
| P2-06 | Workflow provenance | DeepSWE run provenance exists, but proof failure runs may miss it | Collect step after proof failure may not run full artifacts | Failed jobs lack comparable provenance | Persist minimal provenance before proof |
| P2-07 | Outcome docs | `deepswe_outcome.py` doc says INFRA includes adapter-wire in one old line, but classifier treats adapter as GT | Top doc wording mentions adapter-wire under INFRA, later marker GT | Semantic drift | Clean docstring |
| P2-08 | Outcome marker collision | `find_infra_markers()` protects adapter lines, but new markers could collide | Marker parsing rules | Future false INFRA | Add test for every marker collision |
| P2-09 | Outcome unknowns | `UNKNOWN` remains possible and excluded | classifier | Launch wants no unknowns without reason | Require `unknown_reason` field |
| P2-10 | Task truth path | `write_task_truth()` writes beside trial dir unless override | `task_truth.py` | Collected artifact may not include it unless copied | Ensure GHA collect copies task truth |
| P2-11 | Oracle events | Writable `/gt_out` mount captures events, but absence may be silent | GHA collect copies if exists | Missing oracle telemetry can go unnoticed | Add required/optional status in task truth |
| P2-12 | Delivered instruction | `delivered_instruction.txt` copied best-effort | collect step | Brief delivery proof can be absent silently | Add artifact check after trial if agent ran |
| P2-13 | Brief hash | No structured hash of brief in run manifest to delivered instruction | proof/adapter | Cannot easily prove same brief text | Add `brief_sha256` to manifest and witness |
| P2-14 | Run id docs | Docs mention multiple run ids; no single "current run status" machine file | handoff docs | Humans follow stale run | Add `CURRENT_VALIDATION_RUN.json` |
| P2-15 | Test scope docs | "84 tests" plan-scoped but not connected to exact command in atomic doc | handoff docs | Repro gap | Add command receipts or test manifest |
| P2-16 | Baseline rule | Paired scorecard must never rerun baseline, but scripts can still be run arbitrary | CLI scripts | Accidental baseline rerun | Add guard/docs in benchmark runner path |
| P2-17 | Cost metrics | Deep metrics may recompute DeepSeek cost for unknown model when no recorded cost | `gt_deep_metrics.py` fallback | Cost lie for non-DeepSeek | Mark fallback as estimate and model-scoped |
| P2-18 | Agent step count | `action_count` from api_calls can differ from assistant messages | deep metrics / paired metrics | Efficiency metrics split | Store both `api_calls` and `assistant_steps` |
| P2-19 | Local dirty tree | Many unrelated modified/untracked files exist | `git status` | Accidental commits/reverts risk | Continue staging only explicit files |
| P2-20 | Push guard | Report docs are blocked by pre-push guard | Push to `hbali` failed | Handoff stays local unless force policy decided | Document local commit id; do not bypass guard silently |
| P2-21 | Remote split | `origin` points to `harneet2512`, `hbali` to active repo | `git remote -v` | Wrong push target | Use explicit remote and document failure |

## Corrected Answer To "How Many Bugs?"

At product-launch depth, I identify **64 atomic bugs/gaps**, not 21.

The important split:

- **11 original bug families** from the trajectory audit.
- **10 architectural missing-piece families** from the desired-state analysis.
- **64 atomic launch bugs/gaps** after expanding those families across actual
  code boundaries and report readers.

The most important are not the largest ones. The most dangerous are the small
split-truth bugs:

- smoke green while DeepSWE proof fails
- raw cert fail while task truth reconciles
- self-verifier retry named like official verifier retry
- pre-submit intervention named like hard submit blocker
- task truth written but paired metrics still reads old outcome
- runtime policy implemented in a different file than docs say

Those are exactly the kinds of meniscule bugs that make a product fail launch
even when most code exists.

## Closure matrix (2026-06-11 local session)

Status key: **CLOSED** = code+tests landed; **PARTIAL** = semantics/docs fixed, residual work;
**LIVE** = needs substrate rebuild + GHA re-proof.

| ID | Status | Evidence |
|---|---|---|
| P0-01 | LIVE | Go/Rust dep fixes local; `validate_proof_readiness.py`; re-proof after substrate pin |
| P0-02 | CLOSED | `proof_progress.json` / `proof_failure.json` in `gt_run_proof.py` |
| P0-03 | PARTIAL | Workflow renamed substrate preflight; DeepSWE parity still separate job |
| P0-04 | CLOSED | Dynamic GOMODCACHE + `dep_store_manifest.py` |
| P0-05 | CLOSED | Rust dep discovery, rust-src, RA bump, gcc in substrate Dockerfile |
| P0-06 | CLOSED | `compute_paired_metrics.py` prefers `task_truth.json` |
| P0-07 | CLOSED | `reconciled_substrate_verdict.json` |
| P0-08 | CLOSED | `pre_submit_intervention` wording in `gt_agent.py` |
| P0-09 | CLOSED | `verifier_semantics` + `adapter_witness.json` |
| P0-10 | CLOSED | Owner table in `gt_gt.md` §17.9 |
| P0-11 | CLOSED | `artifact_deepswe/phase_policy.py` |
| P0-12 | CLOSED | `_ledger_note_suppression_heuristic` rename |
| P0-13 | CLOSED | `.claude/calibration/horizon_v1.json` + loader |
| P0-14 | CLOSED | `CHECKPOINT_PROTOCOL.md` + `scripts/ci/check_checkpoint_protocol.py` |
| P1-01 | CLOSED | `issue_manifest.py` + GHA step |
| P1-02 | CLOSED | `dep_store_manifest.json` |
| P1-03 | CLOSED | ROOT fail-closed in `deepswe_full.yml` |
| P1-05 | CLOSED | `proof_progress.json` |
| P1-08 | CLOSED | `scripts/swebench/reconcile.py` |
| P1-09 | PARTIAL | `brief_provenance` in task_truth; adapter witness hash |
| P1-10 | CLOSED | `adapter_witness.json` |
| P1-12 | CLOSED | `GT_ORACLE_ROUTE=0` forbidden in `GT_PROOF_MODE` |
| P1-14 | CLOSED | suppression heuristic rename |
| P1-24 | CLOSED | strict trajectory match in `gt_deep_metrics.py` |
| P1-26 | PARTIAL | `steps_to_first_gold_edit` export alias |
| P1-27 | PARTIAL | task_truth resolved path; denominator via outcome classifier |
| P1-29 | CLOSED | `artifact_resolver.py` |
| P2-09 | CLOSED | `unknown_reason` in `deepswe_outcome.py` |
| P2-11 | CLOSED | `oracle_events_status` in task_truth |
| P2-13 | CLOSED | `brief_sha256` in `run_manifest.json` |
| P2-14 | CLOSED | `.claude/CURRENT_VALIDATION_RUN.json` |
| P2-15 | CLOSED | `tests/MANIFEST.json` |
| P2-16 | CLOSED | baseline guard in `compute_paired_metrics.py` |
| P1-04 | CLOSED | `rss_kb` heartbeat in `proof_progress.json` (Unix + psutil fallback) |
| P1-06 | CLOSED | `tests/test_lsp_product_verdict.py` |
| P1-07 | CLOSED | `embedder_product_verdict` / `embedder_diagnostic_only` |
| P1-11 | CLOSED | `adapter_error_scan.py` + GHA step |
| P1-13 | CLOSED | `tests/test_phase_detection.py` event-bound policy |
| P1-15 | CLOSED | `_stable_fact_id` + `_DELIVERED_FACT_IDS` in `gt_mini_patch.py` |
| P1-16 | CLOSED | `_last_budget_meta` char/token reporting |
| P1-17 | CLOSED | `tests/test_action_templates.py` |
| P1-18 | CLOSED | obligation events → `GT_ORACLE_EVENTS` jsonl |
| P1-19 | CLOSED | `obligation_status` in `task_truth.json` |
| P1-20 | CLOSED | `tests/test_verification_horizon_stage_*.py` |
| P1-21 | CLOSED | `tests/test_gt_agent_retry.py` targeted retry |
| P1-22 | CLOSED | `tests/test_gt_agent_retry.py` feedback sanitizer |
| P1-23 | CLOSED | `verifier_semantics` in `task_truth.json` documents GT note |
| P1-25 | CLOSED | `gt_deep_metrics.build` prefers `task_truth.json` |
| P1-28 | CLOSED | auto `patch_hygiene` in `build_task_truth()` |
| P2-01 | CLOSED | `gt_gt.md` §17.8 row 5 intervention wording |
| P2-02 | CLOSED | `gt_gt.md` §17.9 owner table corrected |
| P2-03 | CLOSED | `.claude/CURRENT_VALIDATION_RUN.json` local HEAD pointer |
| P2-06 | CLOSED | `run_provenance.json` before proof in GHA |
| P2-07 | CLOSED | `deepswe_outcome.py` INFRA docstring |
| P2-08 | CLOSED | `tests/test_deepswe_infra_markers_and_brief_wrap.py` |
| P2-10 | CLOSED | collect writes `task_truth.json` |
| P2-12 | CLOSED | collect copies `delivered_instruction` + witness |
| P2-17 | CLOSED | `cost_estimate` flag on DeepSeek recompute |
| P2-18 | CLOSED | `assistant_steps` in `gt_deep_metrics.py` |

**Still LIVE (infra, not code):** P0-01 — substrate image rebuild + `GT_SUBSTRATE_DIGEST` pin + Go/Rust re-proof.

**Still PARTIAL / ops-only:** P0-03 (DeepSWE parity is separate job from smoke); P1-09 brief hash parity witness; P1-26 steps-to-edited-file rename; P1-27 denominator via task_truth (wired, needs live run); P2-05 historical log wording; P2-19–21 dirty tree / push guard / remote split (process, not product bugs).

## Next Work

1. **P0-01 LIVE:** Rebuild substrate image + pin `GT_SUBSTRATE_DIGEST`; re-dispatch Go/Rust matrix from run `27387470440`.
2. Confirm substrate proof PASS on all 5 languages before any tenpack GT-on run.
3. Optional polish: P0-03 DeepSWE proof parity job; P1-09 brief hash witness; P1-26 metric rename in reports.
