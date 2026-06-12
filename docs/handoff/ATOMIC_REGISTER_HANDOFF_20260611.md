# Atomic register closure — handoff (2026-06-11)

Branch: `gt-trial`  
Register: `.claude/reports/runs/validation_27367976952/ATOMIC_PRODUCT_BUG_REGISTER_20260612.md`  
Baseline (frozen, never rerun): `.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`

## Summary

| Bucket | Count |
|--------|-------|
| **CLOSED** (code + deterministic tests) | ~55 |
| **PARTIAL** (docs/ops; not launch blockers) | ~8 |
| **LIVE** (infra re-proof only) | **1** — P0-01 |

Stage-1 proof suite: **107 tests** — see `tests/MANIFEST.json`.

**Only launch blocker:** P0-01 — rebuild `gt-substrate` image, pin `GT_SUBSTRATE_DIGEST`, re-dispatch Go/Rust proof from triage run `27387470440`. Do **not** run tenpack GT-on until Go/Rust proof is green.

---

## Verify (local)

```bash
python -m pytest tests/test_adapter_error_scan.py tests/test_lsp_product_verdict.py tests/test_reconcile.py tests/test_artifact_resolver.py tests/test_issue_manifest.py tests/test_deepswe_outcome_unknown.py tests/test_baseline_guard.py tests/test_task_truth.py tests/test_proof_progress_json.py tests/test_dep_store_manifest.py tests/test_phase_policy_module.py tests/test_gt_deep_metrics_task_truth.py tests/test_action_templates.py tests/test_patch_hygiene.py tests/test_verification_horizon_stage_a.py tests/test_verification_horizon_stage_b.py tests/test_verification_horizon_stage_c.py tests/test_phase_detection.py tests/test_deepswe_infra_markers_and_brief_wrap.py tests/test_context_budget.py tests/test_gt_agent_retry.py tests/test_ledger_suppression.py tests/test_obligation_tracker.py -q
```

Expected: **107 passed**.

---

## P0 — what was done and where

| ID | Status | Problem | Solution | Primary files | Tests |
|----|--------|---------|----------|---------------|-------|
| P0-01 | **LIVE** | Go/Rust fail at substrate LSP proof (`LSP_FAIL_NOT_READY` / `LSP_FAIL_NO_WARM`) | Local fixes for dep discovery + toolchain; needs image rebuild + digest pin + GHA re-proof | `dep_store_manifest.py`, `Dockerfile.gt-substrate`, `validate_proof_readiness.py`, `deepswe_full.yml` | `test_dep_store_manifest.py` |
| P0-02 | CLOSED | `GT_RUN_PROOF_FAIL rc=2` opaque | `proof_failure.json` with stage/lang/tool/exception | `scripts/swebench/gt_run_proof.py` | `test_proof_progress_json.py` |
| P0-03 | PARTIAL | Smoke green ≠ DeepSWE task proof | Renamed workflow to substrate preflight; parity job still separate | `gt_language_smoke.yml`, `gt_gt.md` | existing smoke contract tests |
| P0-04 | CLOSED | Hardcoded `/root/go/pkg/mod` misses task GOMODCACHE | Dynamic `go env GOMODCACHE` + structured dep manifest | `dep_store_manifest.py`, `gt_run_proof.py`, `deepswe_full.yml` | `test_dep_store_manifest.py` |
| P0-05 | CLOSED | Rust RA/sysroot/linker mismatch | rust-src, RA bump, gcc in substrate | `docker/Dockerfile.gt-substrate` | proof integration (GHA) |
| P0-06 | CLOSED | Paired metrics bypass `task_truth.json` | Readers prefer reconciled task truth | `compute_paired_metrics.py` | `test_baseline_guard.py`, `test_task_truth.py` |
| P0-07 | CLOSED | Raw graph cert vs reconciled verdict split | `reconciled_substrate_verdict.json` emitted | `task_truth.py`, `reconcile.py` | `test_reconcile.py`, `test_task_truth.py` |
| P0-08 | CLOSED | "Pre-submit gate" overclaim | Renamed to **pre-submit intervention** (inject, not hard block) | `gt_agent.py`, `gt_gt.md` §17.8 | `test_gt_agent_retry.py` |
| P0-09 | CLOSED | Self-verifier retry vs official verifier conflated | `verifier_semantics` + structured adapter witness | `gt_agent.py`, `task_truth.py` | `test_task_truth.py` |
| P0-10 | CLOSED | Policy owner drift (docs vs code) | Owner table in architecture doc | `gt_gt.md` §17.9 | — |
| P0-11 | CLOSED | Phase policy buried in patch globals | Extracted `phase_policy.py` module | `artifact_deepswe/phase_policy.py` | `test_phase_policy_module.py` |
| P0-12 | CLOSED | Runtime ledger vs post-run "consumption" conflated | Renamed suppression heuristic path | `gt_mini_patch.py` | `test_ledger_suppression.py` |
| P0-13 | CLOSED | Horizon calibration from 9 trajectories only | Versioned calibration artifact + loader | `.claude/calibration/horizon_v1.json`, `gt_mini_patch.py` | horizon stage tests |
| P0-14 | CLOSED | Checkpoint docs not in same commit as code | Protocol + CI checker | `CHECKPOINT_PROTOCOL.md`, `check_checkpoint_protocol.py` | — |

---

## P1 — what was done and where

| ID | Status | Problem | Solution | Primary files | Tests |
|----|--------|---------|----------|---------------|-------|
| P1-01 | CLOSED | Issue source not structured | `issue_manifest.json` | `issue_manifest.py`, `deepswe_full.yml` | `test_issue_manifest.py` |
| P1-02 | CLOSED | Dep-store copy echo-only | `dep_store_manifest.json` | `dep_store_manifest.py` | `test_dep_store_manifest.py` |
| P1-03 | CLOSED | Silent `/testbed` root fallback | Fail-closed if no `.git` | `deepswe_full.yml` | — |
| P1-04 | CLOSED | OOM lacks stage memory context | `rss_kb` in `proof_progress.json` | `gt_run_proof.py` | `test_proof_progress_json.py` |
| P1-05 | CLOSED | Proof stages not persisted | `proof_progress.json` per stage | `gt_run_proof.py` | `test_proof_progress_json.py` |
| P1-06 | CLOSED | LSP warm collapsed to product verdict | Schema forbids single warm bit | `foundational_gates.py` (consumer) | `test_lsp_product_verdict.py` |
| P1-07 | CLOSED | Embedder fallback as product truth | `embedder_product_verdict` vs `embedder_diagnostic_only` | `gt_deep_metrics.py` | `test_gt_deep_metrics_task_truth.py` |
| P1-08 | CLOSED | Reconciliation duplicated | Central `reconcile_graph_handoff()` | `reconcile.py` | `test_reconcile.py` |
| P1-09 | PARTIAL | Brief hash not in delivered metadata | `brief_provenance` + witness hash fields | `task_truth.py`, `artifact_resolver.py` | `test_artifact_resolver.py` |
| P1-10 | CLOSED | Witness grep brittle | `adapter_witness.json` | `gt_agent.py`, `deepswe_full.yml` | — |
| P1-11 | CLOSED | Adapter errors via log grep | `adapter_error_scan.py` | `adapter_error_scan.py` | `test_adapter_error_scan.py` |
| P1-12 | CLOSED | Legacy oracle route in proof mode | Fail if `GT_ORACLE_ROUTE=0` in proof | `gt_mini_patch.py` | `test_phase_policy_module.py` |
| P1-13 | CLOSED | Event-bound candidates bypass policy | Tests for event-bound classes | `gt_mini_patch.py` | `test_phase_detection.py` |
| P1-14 | CLOSED | Ledger mislabels same-turn action | Suppression heuristic only | `gt_mini_patch.py` | `test_ledger_suppression.py` |
| P1-15 | CLOSED | Line-level dedupe only | `_stable_fact_id` + `_DELIVERED_FACT_IDS` | `gt_mini_patch.py` | `test_context_budget.py` |
| P1-16 | CLOSED | Opaque char/token budget | `_last_budget_meta` reporting | `gt_mini_patch.py` | `test_context_budget.py` |
| P1-17 | CLOSED | Narrow action templates | Expanded template coverage | `gt_mini_patch.py` | `test_action_templates.py` |
| P1-18 | CLOSED | Obligations runtime-only | Events → oracle jsonl | `gt_oracle.py`, `gt_mini_patch.py` | `test_obligation_tracker.py` |
| P1-19 | CLOSED | No obligation vector in task truth | `obligation_status` field | `task_truth.py` | `test_task_truth.py` |
| P1-20 | CLOSED | Verification horizon too vague or leaky | Stage A/B/C render tests | `gt_mini_patch.py` | `test_verification_horizon_stage_*.py` |
| P1-21 | CLOSED | Broad npm retry | Targeted retry when safe | `gt_agent.py` | `test_gt_agent_retry.py` |
| P1-22 | CLOSED | Noisy/long test feedback | Sanitizer + truncation metadata | `gt_agent.py` | `test_gt_agent_retry.py` |
| P1-23 | CLOSED | GT note breaks arm neutrality claim | Documented in `verifier_semantics` | `task_truth.py` | `test_task_truth.py` |
| P1-24 | CLOSED | Ambiguous trajectory paths | Strict trajectory match | `gt_deep_metrics.py` | `test_gt_deep_metrics_task_truth.py` |
| P1-25 | CLOSED | Split resolved truth | Deep metrics reads `task_truth.json` | `gt_deep_metrics.py` | `test_gt_deep_metrics_task_truth.py` |
| P1-26 | PARTIAL | "Gold" proxy naming | `steps_to_first_gold_edit` alias; full rename in reports TBD | `compute_paired_metrics.py` | — |
| P1-27 | PARTIAL | INFRA vs unresolved denominator | Wired via task truth classifier; validate on live run | `compute_paired_metrics.py`, `deepswe_outcome.py` | `test_deepswe_outcome_unknown.py` |
| P1-28 | CLOSED | Patch hygiene not always in reports | Auto `patch_hygiene` in `build_task_truth()` | `task_truth.py` | `test_patch_hygiene.py` |
| P1-29 | CLOSED | Artifact path fragmentation | `artifact_resolver.py` | `artifact_resolver.py` | `test_artifact_resolver.py` |

---

## P2 — what was done and where

| ID | Status | Problem | Solution | Primary files | Tests |
|----|--------|---------|----------|---------------|-------|
| P2-01 | CLOSED | Doc overclaim pre-submit gate | §17.8 intervention wording | `gt_gt.md` | — |
| P2-02 | CLOSED | Wrong primary surfaces in docs | §17.9 owner table | `gt_gt.md` | — |
| P2-03 | CLOSED | Stale HEAD in handoff trail | `CURRENT_VALIDATION_RUN.json` pointer | `.claude/CURRENT_VALIDATION_RUN.json` | — |
| P2-04 | CLOSED | No atomic register | This register + handoff | register file | — |
| P2-05 | PARTIAL | Old GHA log wording | Docs tie conclusions to commit id | handoff docs | — |
| P2-06 | CLOSED | Failed proof lacks provenance | `run_provenance.json` before proof | `deepswe_full.yml` | — |
| P2-07 | CLOSED | INFRA docstring drift | Cleaned `deepswe_outcome.py` doc | `deepswe_outcome.py` | — |
| P2-08 | CLOSED | Marker collision risk | Collision tests | `deepswe_outcome.py` | `test_deepswe_infra_markers_and_brief_wrap.py` |
| P2-09 | CLOSED | UNKNOWN without reason | `unknown_reason` required | `deepswe_outcome.py` | `test_deepswe_outcome_unknown.py` |
| P2-10 | CLOSED | task_truth not collected | GHA collect step | `deepswe_full.yml` | `test_task_truth.py` |
| P2-11 | CLOSED | Silent missing oracle events | `oracle_events_status` in task truth | `task_truth.py` | `test_task_truth.py` |
| P2-12 | CLOSED | Silent missing delivered instruction | Collect + witness check | `deepswe_full.yml` | — |
| P2-13 | CLOSED | No brief hash in manifest | `brief_sha256` in run manifest | `gt_run_proof.py` | `test_proof_progress_json.py` |
| P2-14 | CLOSED | No machine current-run file | `CURRENT_VALIDATION_RUN.json` | `.claude/CURRENT_VALIDATION_RUN.json` | — |
| P2-15 | CLOSED | Test command not pinned | `tests/MANIFEST.json` | `tests/MANIFEST.json` | — |
| P2-16 | CLOSED | Accidental baseline rerun | Guard in paired metrics CLI | `compute_paired_metrics.py` | `test_baseline_guard.py` |
| P2-17 | CLOSED | Cost fallback unlabeled | `cost_estimate` flag | `gt_deep_metrics.py` | `test_gt_deep_metrics_task_truth.py` |
| P2-18 | CLOSED | api_calls vs assistant steps | Separate `assistant_steps` | `gt_deep_metrics.py` | `test_gt_deep_metrics_task_truth.py` |
| P2-19–21 | PARTIAL | Dirty tree / push guard / remotes | Process: stage explicit paths only | this handoff | — |

---

## New modules (this commit)

| Module | Role |
|--------|------|
| `scripts/swebench/reconcile.py` | Single graph-handoff reconciliation |
| `scripts/swebench/artifact_resolver.py` | Canonical artifact paths + brief provenance |
| `scripts/swebench/issue_manifest.py` | Structured GHA issue input metadata |
| `scripts/swebench/dep_store_manifest.py` | Go/Rust dep-store manifest for proof |
| `scripts/swebench/adapter_error_scan.py` | Structural adapter error detection |
| `scripts/swebench/validate_proof_readiness.py` | Pre-flight before live proof |
| `artifact_deepswe/phase_policy.py` | Canonical phase allowlist policy |
| `scripts/ci/check_checkpoint_protocol.py` | Checkpoint doc protocol checker |

---

## Operator next steps

1. **Rebuild substrate** from `docker/Dockerfile.gt-substrate`; record digest.
2. Pin **`GT_SUBSTRATE_DIGEST`** in `.github/workflows/deepswe_full.yml` and `LATEST_TASK.md`.
3. Re-dispatch Go + Rust proof tasks from triage **`27387470440`**; confirm `proof_progress.json` ends at PASS.
4. Only then: tenpack GT-on (paired against frozen baseline JSON — never rerun GT-off).

---

## Files in this commit (intentional scope)

Code, workflows, tests, machine files, register update, and this handoff. Unrelated untracked workspace artifacts were **not** staged (P2-19).
