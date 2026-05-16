# Baseline Purity Report

**Run:** 25967190337
**Mode:** GT_BASELINE=1
**Commit:** 4a45a3a

## Layer Suppression Verification

| Layer | Code Location | Mechanism | Agent-Visible? |
|-------|--------------|-----------|----------------|
| L1 | oh_gt_full_wrapper.py:3107 | `if _GT_BASELINE: return msg` | NO — instruction returned unmodified |
| L3 | oh_gt_full_wrapper.py:2281 | `if _GT_BASELINE: return obs` | NO — observation returned unmodified |
| L3b | oh_gt_full_wrapper.py:1984 | `if _GT_BASELINE: return obs` | NO — observation returned unmodified |
| L5 | oh_gt_full_wrapper.py:1887,1935,2155,2470,2507 | `not _GT_BASELINE` guards | NO — governor logic skipped |
| L6 | oh_gt_full_wrapper.py:2203-2277 | Runs before baseline check | NO — modifies graph.db only, obs untouched |
| L4 | oh_gt_full_wrapper.py:1853-1856 | Telemetry counter only | NO — no obs modification |

## Per-Task Evidence

| Task | "BASELINE MODE" logged | L3 utilization | L3b utilization | GT chars in obs |
|------|:---:|:---:|:---:|:---:|
| beancount-931 | YES | 0.0 | 0.0 | 0 |
| beets-5495 | YES | 0.0 | 0.0 | 0 |
| xarray-9760 | YES | 0.0 | 0.0 | 0 |
| cfn-lint-3821 | YES | 0.0 | 0.0 | 0 |
| loguru-1306 | YES | 0.0 | 0.0 | 0 |

## Totals

- **L1 visible chars:** 0
- **L3 visible chars:** 0
- **L3b visible chars:** 0
- **L5 visible chars:** 0
- **Total GT visible chars:** 0
- **Telemetry-only confirmation:** YES (L6 runs but doesn't modify observations)
- **Observation mutation check:** PASS (all `return obs` before any append)

## Verdict: PASS

Baseline is pure. No GT content reaches the agent's observation or instruction in any task.
