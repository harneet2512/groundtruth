# Handoff after checkpoint 005

Date: 2026-06-11 | Branch: `gt-trial`

## Commits (bugfree 004–005)

| Commit | CP | Summary |
|--------|-----|---------|
| `d7da2bc2` | 004 | Oracle event-bound evidence waiver (B11) |
| *(pending)* | 005 | LSP product readiness (B4) |

## Bug status (B4–B11)

| Bug | Status |
|-----|--------|
| B4 | **FIXED** |
| B5–B10 | OPEN |
| B11 | **FIXED** |

## Next: CP006 task_truth.py

```powershell
Set-Location d:\Groundtruth
python -m pytest tests/test_task_truth.py -q
```
