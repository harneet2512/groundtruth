# Checkpoint 008 — Infra classification (B10)

Date: 2026-06-11

## Boundary

Layer fixed: failure-class infra subtypes in `deepswe_outcome.py`.

Out of scope: harness fixes, disk cleanup, agent behavior.

## Bug addressed

`boa` (ENOSPC + zero-byte canonical trajectory) and `arktype` (missing artifacts) were not cleanly sub-classified under INFRA.

## Code changes

- `scripts/verify/deepswe_outcome.py` — `detect_infra_subtype()`, `INFRA_SUBTYPES`, wired into `build_signal_record` / `classify_outcome`
- `tests/fail_closed/test_deepswe_outcome_classify.py` — ENOSPC + trajectory fallback subtype tests

## Verification

```text
python -m pytest tests/fail_closed/test_deepswe_outcome_classify.py -q
(all pass including new infra subtype cases)
```

## Rules

| Signal | Subtype |
|--------|---------|
| `no space left on device` / ENOSPC | `INFRA_ENOSPC` |
| Canonical trajectory 0 bytes + mini present | `INFRA_TRAJECTORY_FALLBACK` |
| No result/trajectory artifacts | `INFRA_MISSING_ARTIFACT` |

## Bug ledger delta

| Bug | Status |
|-----|--------|
| B10 | **FIXED** |
