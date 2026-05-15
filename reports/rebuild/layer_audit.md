# GT Layer Audit

Generated: 2026-05-15
Source: verified findings from exploration agents against live source code.

---

## L1 -- Pre-task Brief

| Column | Value |
|--------|-------|
| Source file | `src/groundtruth/pretask/v1r_brief.py` (495 lines) |
| Function/class | `generate_v1r_brief()` lines 324-470 |
| Implemented? | Yes |
| Wired into runtime? | Yes -- wrapper lines 2151-2240 in `scripts/swebench/oh_gt_full_wrapper.py`, injected into system prompt |
| Semantically measured? | No -- only "L1 brief injected" count (fired/not-fired). No semantic metrics exist for candidate follow-through, dampening effect, or non-L1 path promotion. |
| Prior run proves correct behavior? | No -- fired 29/29 but only proved injection, not usefulness. No measurement of whether agent followed L1 candidates, whether L1 dampened agent, whether non-L1 paths were promoted. |
| Status | **wired-but-unmeasured** |

---

## L3 -- Post-Edit Evidence

| Column | Value |
|--------|-------|
| Source file | `src/groundtruth/hooks/post_edit.py` (1585 lines) |
| Function/class | `generate_improved_evidence()` lines 502-689 |
| Implemented? | Yes (single mode only) |
| Wired into runtime? | Yes -- wrapper lines 1707-1775 in `scripts/swebench/oh_gt_full_wrapper.py` |
| Semantically measured? | Partial -- fired count, chars, dedup count tracked. No mode tracking, no suppression reason distribution. |
| Prior run proves correct behavior? | No -- fired 17/29 tasks, 12 got zero evidence. No post_failure or late_repair modes exist. |
| Status | **partially-implemented** (one mode; needs three modes + verification suggestion) |

---

## L3b -- Post-View Navigation

| Column | Value |
|--------|-------|
| Source file | `src/groundtruth/hooks/post_view.py` (388 lines) |
| Function/class | `graph_navigation()` lines 188-336 |
| Implemented? | Yes (no decay logic) |
| Wired into runtime? | Yes -- wrapper lines 1549-1599 in `scripts/swebench/oh_gt_full_wrapper.py` |
| Semantically measured? | No -- fired count and chars only. No iteration band tracking. |
| Prior run proves correct behavior? | No -- 100 fires, 46,476 chars = FLOODING. No iteration decay implemented. |
| Status | **partially-implemented** (works but floods; no decay) |

---

## L4 -- Prefetch

| Column | Value |
|--------|-------|
| Source file | `scripts/swebench/oh_gt_full_wrapper.py` lines 1974-2060 |
| Function/class | `_run_l4_prefetch()` |
| Implemented? | Yes |
| Wired into runtime? | Yes -- line 2289 in wrapper, runs before L3 |
| Semantically measured? | No -- fired count only. No token cap tracking, no noise suppression logging. |
| Prior run proves correct behavior? | No -- fired but no semantic measurement of whether agent used prefetch signal. |
| Status | **wired-but-unmeasured** |

---

## L5 -- Trajectory Governor

| Column | Value |
|--------|-------|
| Source file | `src/groundtruth/trajectory/` (6 files: `governor.py`, `classifier.py`, `hooks.py`, `parsers.py`, `state.py`, `__init__.py`) |
| Function/class | `L5Governor.after_interaction()` in `governor.py` lines 84-128 |
| Implemented? | Partial |
| Wired into runtime? | Yes -- wrapper lines 1523-1539 (CmdRun), 1618-1628 (edit state), 1800-1813 (finish) in `scripts/swebench/oh_gt_full_wrapper.py` |
| Semantically measured? | Partial -- hook fire counts, telemetry JSONL. |
| Prior run proves correct behavior? | No -- 0 new hooks fired despite 211 verification commands. Only reacts to failures, not "broad passing tests." |
| Status | **partially-implemented** (missing unverified-patch detection; `unsafe_finish` does not catch broad-pass-only) |

---

## L6 -- Reindex

| Column | Value |
|--------|-------|
| Source file | `scripts/swebench/oh_gt_full_wrapper.py` lines 1662-1705 |
| Function/class | `make_reindex_command()` lines 668-676 |
| Implemented? | Yes |
| Wired into runtime? | Yes -- runs before L3, 120s timeout, mtime validation |
| Semantically measured? | No -- fired count and ok/fail only. No latency or staleness tracking. |
| Prior run proves correct behavior? | Partial -- works correctly, sequence (L6 before L3) verified. But no quantitative measurement of index freshness impact. |
| Status | **wired-but-unmeasured** |

---

## Hygiene -- Scaffold Strip

| Column | Value |
|--------|-------|
| Source file | `scripts/swebench/oh_gt_full_wrapper.py` lines 1064-1098 |
| Function/class | `_strip_scaffold_files()` |
| Implemented? | Yes |
| Wired into runtime? | Yes -- on finish (line 1820) + late-stage (line 1517), idempotent |
| Semantically measured? | No -- no metrics tracked at all. |
| Prior run proves correct behavior? | Partial -- strips scaffold files correctly by observation, but no quantitative tracking of how many files stripped or impact on submission quality. |
| Status | **wired-but-unmeasured** |

---

## Meta-Logging

| Column | Value |
|--------|-------|
| Source file | `scripts/swebench/oh_gt_full_wrapper.py` |
| Function/class | `_log_gt_interaction()` (JSONL), `GTTelemetry` class |
| Implemented? | Partial |
| Wired into runtime? | Yes -- integrated throughout wrapper |
| Semantically measured? | No -- ok/(ok+fail) utilization is a fake metric (just means "did not crash", not "was useful"). |
| Prior run proves correct behavior? | No -- logging itself works, but the derived utilization metric is meaningless. |
| Status | **partially-implemented** (logs exist but utilization metric is fake) |

---

## Cost Tracking

| Column | Value |
|--------|-------|
| Source file | `scripts/swebench/cost_tracking.py` |
| Function/class | `_cost_callback()`, per-call JSONL |
| Implemented? | Yes |
| Wired into runtime? | Yes -- litellm callback, writes to `/tmp/litellm_costs.jsonl` |
| Semantically measured? | Yes -- per-call cost, model, token counts, reasoning guard |
| Prior run proves correct behavior? | Yes -- comprehensive per-call tracking verified in production runs. |
| Status | **working** |

---

## Summary

| Layer | Status | Semantic Measurement |
|-------|--------|---------------------|
| L1 (Pre-task Brief) | wired-but-unmeasured | None |
| L3 (Post-Edit Evidence) | partially-implemented | Partial (counts only) |
| L3b (Post-View Navigation) | partially-implemented | None |
| L4 (Prefetch) | wired-but-unmeasured | None |
| L5 (Trajectory Governor) | partially-implemented | Partial (counts only) |
| L6 (Reindex) | wired-but-unmeasured | None |
| Hygiene (Scaffold Strip) | wired-but-unmeasured | None |
| Meta-Logging | partially-implemented | Fake metric |
| Cost Tracking | working | Full |

**Key finding:** 1 of 9 layers is fully working with real semantic measurement (Cost Tracking). 4 layers are wired but unmeasured. 4 layers are partially implemented. Zero layers besides Cost Tracking have prior-run evidence proving correct behavioral impact on the agent.
