# Handoff after checkpoint 004

Date: 2026-06-11
Branch: `gt-trial`

## Cumulative commits (bugfree session)

| Commit | Checkpoint | Summary |
|--------|------------|---------|
| `060eccc9` | 001 | Deep metrics trajectory fallback |
| `956c32e1` | 002 | Sanitize verify guidance (no test leak) |
| `e013c7be` | 003 | Embedder cert truth |
| *(pending)* | **004** | Oracle event-bound waiver for l3b.evidence |

Post-handoff infra: `9bd3bf82` … `7a283c60` (gates, LIPI, LSP WARN, dep mounts).

## Bug status (B4–B11)

| Bug | Status | Next |
|-----|--------|------|
| B4 | PARTIAL | CP005 |
| B5 | OPEN | CP006 |
| B6 | OPEN | CP007 |
| B7 | OPEN | CP009 |
| B8 | OPEN | CP010 |
| B9 | OPEN | CP006 |
| B10 | OPEN | CP008 |
| B11 | **FIXED** | — |

## Next checkpoint

**CP005 — LSP product readiness (B4)**

Pre-flight:

```powershell
Set-Location d:\Groundtruth
python -m pytest tests/fail_closed/test_lsp_liveness.py -q
```

Primary files: `src/groundtruth/resolve.py`, `scripts/metrics/foundational_gates.py`.

## Trajectory notes

After CP004, read adaptix / fd / katex / abs trajectories for `CONTEXT_GAP_AUDIT_27367976952.md` (parallel with CP005–007).
