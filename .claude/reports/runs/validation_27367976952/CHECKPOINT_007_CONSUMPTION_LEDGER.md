# Checkpoint 007 — Consumption ledger (B6)

Date: 2026-06-11

## Boundary

Layer fixed: post-run consumption measurement (delivered → used/enforced).

Out of scope: hook delivery logic, oracle gate, outcome classification.

## Bug addressed

Deep metrics counted delivery (`gt_delivery`) but not whether the agent consumed or enforced GT blocks in subsequent turns.

## Code changes

- `scripts/swebench/consumption_ledger.py` — `build_consumption_ledger()`, `ledger_from_trajectory_path()`
- `scripts/swebench/gt_deep_metrics.py` — emits `gt_blocks_delivered/consumed/enforced` + writes `gt_consumption_ledger.json`
- `tests/test_consumption_ledger.py`

## Verification

```text
python -m pytest tests/test_consumption_ledger.py -q
2 passed
```

## LIPI

- Deterministic trajectory scan; no LLM judgment
- Windowed follow-through (default 3 turns)
- Absent trajectory → zero counts (correct-or-quiet)

## Product impact

Enables Used/Enforced columns beside Delivered in deep metrics.

## Bug ledger delta

| Bug | Status |
|-----|--------|
| B6 | **FIXED** (ledger + deep metrics columns) |
