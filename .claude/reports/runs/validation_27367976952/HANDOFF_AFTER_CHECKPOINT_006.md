# Handoff after checkpoint 006

Date: 2026-06-11 | Branch: `gt-trial`

## Commits (bugfree 004–006)

| Commit | CP | Summary |
|--------|-----|---------|
| `d7da2bc2` | 004 | Oracle event-bound evidence waiver (B11) |
| `7a554ec8` | 005 | LSP product readiness (B4) |
| *(pending)* | 006 | task_truth.json reconciler (B5/B9) |

## Bug status (B4–B11)

| Bug | Status |
|-----|--------|
| B4 | **FIXED** |
| B5 | PARTIAL |
| B6 | OPEN |
| B7–B10 | OPEN |
| B9 | PARTIAL |
| B11 | **FIXED** |

## Next: CP007 consumption ledger

```powershell
Set-Location d:\Groundtruth
python -m pytest tests/test_consumption_ledger.py -q
```
