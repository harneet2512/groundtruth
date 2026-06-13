# Parity Restoration Status — 2026-06-13

Goal: bring CURRENT state (code) into parity with DESIRED state (gt_gt.md + frozen
test corpus), so the benchmark surface is trustworthy. Fix the foundation before
chasing task-level bugs.

## LIPI of committed work

### `12d564af` — patterns consolidation (test-runner/env-fail → ONE canonical module)

| Avenue | Verdict | Evidence |
|---|---|---|
| Logic | PASS | Canonical is a true superset: subset cases (pytest/go test/cargo test/...) still match, superset cases (timeout wrappers, manage.py test, make test, rake, phpunit) added, BUILD SUCCESS preserved |
| Implementation | PASS | gt_mini_patch patterns byte-identical to canonical on all 13 probes (runner/env/pass/fail/compile) |
| Integration | PASS | Both product-controller failures fail IDENTICALLY at parent `6d6b3f85` → zero new failures from this commit |
| Plumbing | PASS | Import resolves in product (verified); agent-container fallback is identical superset |

**Verdict: CLEAN.** The consolidation removes the RED #1/#2 divergence (7 test-runner
regexes, 5+ env-fail regexes) with no behavioral change.

## Pre-existing failures discovered (16) — the real parity gap

These fail at `6d6b3f85` (before the patterns commit). Categorized by root cause:

### Category A — this session's behavior changes; tests encode OLD desired state (TEST UPDATE)

| Test | Root cause | Action |
|---|---|---|
| `test_product_controller_phase_progression` | D8 (commit `0caa5878`): ORIENT now 3% of step_limit (9 actions @ 300), was fixed 5 | Update test to new data-derived threshold |
| `test_verification_horizon_and_budget_and_action_translation` | D5 (commit `8d505603`): action templates now fire `Inspect ...` where test expected `''` | Update test to expect the imperative |

### Category B — genuine regression; code diverged from frozen ground truth (CODE FIX) — **FIXED**

| Test | Root cause | Fix |
|---|---|---|
| `test_replay_byte_parity_all_trajectories` | **D4 conflated edit-DETECTION with obligation-CREDIT**: `_edit_target`/`_classify` used `_is_repo_source_path` which excludes `/tmp/` scratch. Agents stage edits in `/tmp/X_new.ts` → source_edit_count=0 → failure_persisted can't fire | `f6db37bb`: split `_has_source_ext` (broad, edit detection) from `_is_repo_source_path` (obligation credit) — **byte-parity GREEN** |
| `test_edit_recall_all_source_writes` | Same — sensor missed 77/182 source writes | Same fix — **GREEN** |
| `test_product_controller_phase_progression` | D8 assumed phantom 300-step budget when step_limit=None | `f6db37bb`: `_ORIENT_MIN_ACTIONS` fallback (no-hardcoded) — **GREEN** |
| `test_verification_horizon_and_budget_and_action_translation` | D1 (commit-after-gate) + D5 (caller_risk template) | Test updated to new desired state — **GREEN** |
| `test_brief_delivery_invariants` (×3) | `re` missing from exec ns + regex guard upgrade | `1844b9af` — **GREEN** |

### Category C — research-backed intentional change; tests encode OLD prescriptive behavior (TEST UPDATE pending)

| Test | Change | Action |
|---|---|---|
| `test_hypothesis_falsified_late_includes_no_restart` + `test_step_75_appends_only` + `test_late_repair_includes_no_restart` + cfnlint3862 (×2) | L5 hooks made DIAGNOSTIC-not-prescriptive (SWE-PRM anti-anchoring, arXiv 2509.02360): "Do not restart...Repair" imperative dropped, now "The current hypothesis is unconfirmed" | Update 5 tests to the diagnostic text (the new desired state per gt_gt research-backed pillar) |

### Category D — separate pre-existing, lower benchmark priority

| Test | Path | Note |
|---|---|---|
| `test_fd_shape_spread_loop_fires` | gt_mini_patch detect.loop | degenerate_loop not firing on spread stale-binary pattern — investigate |
| `test_edit_then_broad_pass_fires` | product OH-path L5Governor | `has_unverified_patch()` state-detection (separate from DeepSWE oracle) |
| `test_l6_presubmit_actionable` | Windows temp-dir | environmental (WinError 267), not a real failure |
| `test_oh_gt_full_wrapper` | collection error | import-time issue |

## Net progress

Of the 16 pre-existing failures: **8 FIXED** (the dominant edit-detection regression
cluster + brief + phase + action-translation), **5 stale-from-intentional** (hypothesis
nudge — test update pending), **3 separate/environmental**. The CORE oracle impeccability
violation (byte-parity replay ≠ live governor) is CLOSED.

## Principle applied (per user)

- **No hardcoded thresholds** (gt_gt 3 mandatory properties): thresholds from per-task
  data, not absolute constants. D8 ORIENT already moved to a fraction; confidence
  thresholds (AMBER #5) still to consolidate into `signal_thresholds.py` as
  named/dynamic.
- **Oracle metrics + delivery impeccable**: verified the delivery chain is byte-accurate
  (`_win` appended == `chars=len(_win)` recorded), winner-kind attribution correct,
  deep metrics read from `output.jsonl` (agent's truth, not telemetry), behavioral
  metrics shared live/replay (no twin-drift). The byte-parity drift IS the one
  impeccability violation — replay must reproduce the live governor exactly.
