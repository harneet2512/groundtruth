# LATEST_TASK.md — Session Handoff (2026-06-11)

## STATUS: CP004–CP010 SHIPPED — STAGE 1 STABILIZED FOR BUGFREE BOUNDARIES

Branch: `gt-trial`  
Validation run: `validation_27367976952` (0/9 resolved at run time; artifacts on disk)

### Commits (CP004–CP010)

| Commit | Checkpoint | Summary |
|--------|------------|---------|
| `d7da2bc2` | 004 | Oracle event-bound waiver for `l3b.evidence` (B11) — 23/23 adapter tests |
| `7a554ec8` | 005 | LSP product readiness: `effective_work`, transport vs product gates (B4) |
| `9a7ce8b4` | 006 | `task_truth.json` reconciler (B5/B9 partial) |
| `6c0b1af6` | 007 | `gt_consumption_ledger.json` + deep metrics Used/Enforced columns (B6) |
| `eae6667a` | 008 | Infra subtypes + `write_task_truth` outcome hook (B10) |
| `f5dee492` | 009 | Centralized `path_policy` surface filter (B7) |
| `eb5cfe5f` | 010 | Patch hygiene classification (B8) |

Prior session (CP001–003): `060eccc9`, `956c32e1`, `e013c7be`, `5b3a0d4e`.

### Pre-flight (88 tests)

```powershell
Set-Location d:\Groundtruth
python -m pytest tests/test_verified_adapter.py tests/fail_closed/test_lsp_liveness.py tests/test_task_truth.py tests/test_consumption_ledger.py tests/fail_closed/test_deepswe_outcome_classify.py tests/test_path_policy.py tests/test_patch_hygiene.py -q
```

### Bug ledger (B4–B11)

| Bug | Status |
|-----|--------|
| B4 LSP warm over-credit | **FIXED** |
| B5 task truth contradiction | PARTIAL |
| B6 consumption ledger | **FIXED** |
| B7 surface pollution | **FIXED** |
| B8 patch hygiene | **FIXED** |
| B9 outcome schema | PARTIAL |
| B10 infra classification | **FIXED** |
| B11 evidence delivery | **FIXED** |

### Key docs

- Master handoff: `.claude/reports/runs/validation_27367976952/GT_BUGFREE_HANDOFF_FULL.md`
- Session closeout: `.claude/reports/runs/validation_27367976952/HANDOFF_AFTER_CHECKPOINT_010.md`
- Trajectory context gaps: `.claude/reports/runs/validation_27367976952/CONTEXT_GAP_AUDIT_27367976952.md`
- Architecture of record: `gt_gt.md` §17

### Next work (Stage 2)

1. Smoke re-run Go/Rust tasks (`abs-module-cache-flags`, `fd-deterministic-multi-key-sorting`) — confirm `effective_work > 0` + consumption ledger on live trajectories.
2. Paired GT-on flip experiment vs frozen baseline (never rerun GT-OFF).
3. Trajectory-state controller + obligation model (§17.8) — dominant gap for feature tasks per context-gap audit.

### Context-gap finding (four tasks)

Validation run **already delivered** `post_view` witnesses on adaptix/fd/abs; CP004 closes fake-env suppression, not this run's trajectories. Dominant failures: **obligation/spec shape**, **LSP product readiness on Go/Rust**, **verify leakage** (katex — fixed in CP002 for future runs), **patch pollution**.
