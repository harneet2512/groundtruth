# Checkpoint 006 — Task truth ledger (B5, B9)

Date: 2026-06-11

## Boundary

Layer fixed: per-task truth reconciliation only.

Out of scope: evidence delivery (CP004), LSP gates (CP005), consumption ledger (CP007), infra subtypes (CP008), path policy (CP009), patch hygiene (CP010).

## Bug addressed

Certs, `[GT_META]` witness, deep metrics, outcome, and trajectory integrity could contradict (e.g. `GRAPH_FAIL_MISSING_HANDOFF` vs runtime handoff witness). No single reconciled artifact existed per task.

## Code changes

- `scripts/swebench/task_truth.py` — `build_task_truth()`, `reconcile_graph_handoff()`, `write_task_truth()`
- `scripts/verify/deepswe_outcome.py` — writes `task_truth.json` at end of CLI run
- `tests/test_task_truth.py` — witness override + build smoke

## Verification

```text
python -m pytest tests/test_task_truth.py -q
3 passed
```

## LIPI (§12)

- Witness-over-cert rule: `GRAPH_FAIL_MISSING_HANDOFF` reconciled when `gt_prebuilt_active ∧ hook_hash_match`
- Fail-closed: unreconciled cert fails still classify GT
- Deterministic: no task-id logic

## Product impact

One JSON artifact per task for debugging contradictions between pre-agent certs and runtime witness.

## Bug ledger delta

| Bug | Status |
|-----|--------|
| B5 | PARTIAL (ledger ships; full cross-run audit TBD) |
| B9 | PARTIAL (trajectory integrity fields present) |
