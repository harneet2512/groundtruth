# LAST_MILE_VERIFY.md — Mechanism Verification

**Date:** 2026-05-18
**Branch:** jedi__branch
**Base commit:** 12cd9b8d (golazo_today.md)
**Prerequisite:** LAST_MILE_AUDIT.md (Step 1-3 completed first)
**Implementation order:** A (observability) → B (semantic + behavioral) → D (disable L6/tools)

---

## Structured Trace Fields

Defined in `src/groundtruth/hooks/trace_fields.py`:
- `TraceEvent` dataclass with: run_id, task_id, mechanism, layer, event_type, step, graph_db_exists, index_ready, evidence_count, confidence, emit_or_suppress, suppression_reason, agent_visible, delivery_surface, payload_hash, payload_tokens, remaining_turns
- `SuppressionReason` enum: NONE, NO_GRAPH_DB, INDEX_NOT_READY, NO_EVIDENCE, LOW_CONFIDENCE, DUPLICATE, STALE, TOO_LATE, BUDGET, NOT_AGENT_VISIBLE, IMPORT_ERROR, SNIPPET_ERROR, DISABLED, GATE_MISMATCH, UNKNOWN_ERROR

Wrapper emits `[GT_TRACE]` log lines at:
- L3 post-edit hook output (emit or suppress with reason)
- Semantic check (emit/NO_EVIDENCE/GATE_MISMATCH/SNIPPET_ERROR)
- All suppression points include explicit reason enum

---

## Bug Audit Results

### BUG-1 (CRITICAL): has_evidence gate silently drops valid evidence

**File:** `scripts/swebench/oh_gt_full_wrapper.py:2938`
**Root cause:** The gate checked for 8 markers but post_edit.py produces 16+ distinct marker types. Missing: `BEHAVIORAL CONTRACT:`, `TEST EXPECTS:`, `TEST:`, `WARNING:`, `TOP CALLER:`, `MUST PRESERVE:`, `[GT_VERIFY]`, `[GT L3:`.

**Impact:** Even when behavioral contract extracted 2 guards + 3 return paths correctly, the output was silently dropped at the wrapper level. This explains the 0/5 behavioral contract delivery rate — the mechanism WORKED but delivery was broken.

**Fix:** Added all 8 missing markers to the gate tuple.

**Proof:** `test_behavioral_contract_recognized` PASSES — output is:
```
BEHAVIORAL CONTRACT:
  GUARD: if os.environ.get('FORCE_COLOR') -> return
  GUARD: if not sys.stderr.isatty() -> return
```

### BUG-2 (HIGH): Semantic check shell one-liner fragile and undiagnosable

**File:** `scripts/swebench/oh_gt_full_wrapper.py:2962-2975`
**Root cause:** A 15-line Python snippet was embedded as an f-string with 4-level escape nesting (`\\\\s+` → `\\s+` → `\s+`), executed via `python3 -c "..."` inside the container. Any crash produced stderr only, which `_run_internal` discards.

**Impact:** 0/5 semantic check delivery. No diagnostic information at all when it failed — no [GT_META] log line for empty output.

**Fix:** Created `src/groundtruth/hooks/semantic_check.py` as a proper module with CLI (`python3 -m groundtruth.hooks.semantic_check --file=X --workspace=Y`). Wrapper now calls the module instead of the one-liner. Added diagnostic logging for empty output and raw output without recognized markers.

**Proof:** 16/16 unit tests pass including `test_detects_added_guard` which creates a git repo, commits, edits a file, and verifies GUARD_ADDED is detected.

### BUG-3 (MEDIUM): Behavioral contract outer exception swallowed

**File:** `src/groundtruth/hooks/post_edit.py:1017`
**Root cause:** `except Exception: pass` around the entire behavioral contract block. Any error (import failure, type error, anything) was invisible.

**Fix:** Changed to `except Exception as _bc_outer_exc: print(f"[GT_META] behavioral_contract_outer_error: ...")`.

### BUG-4 (LOW): No diagnostic logging for empty/unrecognized hook output

**File:** `scripts/swebench/oh_gt_full_wrapper.py:2938-2953`
**Root cause:** When `hook_body` was non-empty but contained no recognized markers, there was no log line. When `_sem_out` was empty, there was no log line.

**Fix:** Added 3 new diagnostic log points:
- `[GT_META] post_edit_evidence_markers` — what markers matched
- `[GT_META] post_edit_no_markers` — hook produced output but nothing recognized
- `[GT_META] post_edit_empty_hook` — hook returned empty
- `[GT_META] semantic_check_empty_output` — semantic check returned nothing
- `[GT_META] semantic_check_raw_output_no_markers` — output exists but no GUARD/RETURN markers

---

## Mechanism Status (After Fix)

| # | Mechanism | Before Fix | After Fix | Evidence |
|---|-----------|-----------|-----------|----------|
| 1 | L1 Brief | WORKS 5/5 | WORKS 5/5 | No change needed |
| 3 | Behavioral contract | 0/5 (BUG-1) | **WORKS** | test_behavioral_contract_recognized PASSES |
| 4 | Constraint framing | 3/5 | 3/5 | Depends on caller evidence (unchanged) |
| 5 | L1 Keyword | WORKS 4/5 | WORKS 4/5 | No change needed |
| 6 | Recall | 4/5 | 4/5 | No change needed |
| 9 | Semantic check | 0/5 (BUG-2) | **WORKS locally** | 16 unit tests pass; in-container needs live verification |
| 10 | Scope | 3/5 | 3/5 | No change needed |

---

## Test Results

### New tests (22 total, all passing)

```
tests/unit/test_semantic_check.py          16 passed
tests/unit/test_evidence_gate.py            6 passed
```

### Key test cases

| Test | What it proves |
|------|---------------|
| `test_behavioral_contract_recognized` | Contract output is produced AND gate recognizes it |
| `test_fires_with_two_guards` | Guard extraction works on loguru-like code |
| `test_detects_added_guard` | Git-based before/after comparison detects new guards |
| `test_loguru_colorize_pattern` | `_regex_extract_guards` handles FORCE_COLOR pattern |
| `test_output_always_recognized` | Whatever post_edit produces, the gate catches it |

---

## What Still Needs Live Verification

1. **Semantic check in-container**: The module (`python3 -m groundtruth.hooks.semantic_check`) must be importable inside the SWE-bench container. The `groundtruth` package is uploaded to `/tmp/gt_tools/` — need to verify PYTHONPATH includes it.

2. **Behavioral contract db_path in container**: The query `SELECT start_line, end_line FROM nodes WHERE name=? AND file_path=?` needs `file_path` to match what's in graph.db. If the indexer stored paths differently from what post_edit receives, the query returns no rows.

3. **Actual delivery to agent**: With BUG-1 fixed, behavioral contract should now reach the agent. But we need to verify the agent sees it in the observation (not truncated, not buried under other output).

---

## Files Changed

| File | Change | Lines |
|------|--------|-------|
| `scripts/swebench/oh_gt_full_wrapper.py` | Added 8 markers to has_evidence gate | ~2938-2945 |
| `scripts/swebench/oh_gt_full_wrapper.py` | Diagnostic logging for hook output | ~2949-2953 |
| `scripts/swebench/oh_gt_full_wrapper.py` | Replaced semantic check one-liner with module call | ~2962 |
| `scripts/swebench/oh_gt_full_wrapper.py` | Diagnostic logging for empty semantic output | ~2993-2996 |
| `src/groundtruth/hooks/semantic_check.py` | NEW — standalone semantic check module | 100 lines |
| `src/groundtruth/hooks/post_edit.py` | Behavioral contract except:pass → logging | ~1017 |
| `tests/unit/test_semantic_check.py` | NEW — 16 tests for semantic check | 170 lines |
| `tests/unit/test_evidence_gate.py` | NEW — 6 tests for evidence gate | 170 lines |

---

## Before/After Summary

| Metric | Before (BUG-1+BUG-2) | After Fix |
|--------|----------------------|-----------|
| Behavioral contract delivery | 0/5 | Should be >=3/5 (functions with 2+ guards) |
| Semantic check delivery | 0/5 | Should be >=3/5 (files with return paths) |
| Evidence markers recognized | 8 | 16 |
| Diagnostic log coverage | 0 failure cases | 5 failure cases |
| Silent exception handlers | 2 | 0 |
| Unit tests for mechanisms | 0 | 22 |

---

## Live Verification Plan

### Smoke test (2-task)
1. Push fixes to jedi__branch
2. Run loguru-1306 + beancount-931 with GT (not baseline)
3. Grep for `[GT_TRACE] mech=` lines — every mechanism emission/suppression has a trace
4. Verify: `mech=L3_post_edit action=emit markers=.*BEHAVIORAL CONTRACT` appears
5. Verify: `mech=semantic_check` shows either `action=emit` or `reason=NO_EVIDENCE` (never missing)

### Smoke test (5-task) — only after 2-task passes
6. Run 5-task set from last_mile.md
7. Record per-task table:

| task | mechanism | fired/suppressed | step | evidence_count | confidence | agent_visible | delivery_surface | suppression_reason | next_5_actions | first_gold_view_step | action_count | resolved |
|------|-----------|-----------------|------|---------------|-----------|--------------|-----------------|-------------------|---------------|---------------------|-------------|----------|
| (to fill from live run) | | | | | | | | | | | | |

### Success criteria
- Semantic check emits with agent_visible=true on >= 1 task
- Behavioral contract emits with agent_visible=true on >= 1 task
- No `reason=UNKNOWN_ERROR` in any trace line
- No mechanism with 0 trace lines (every mechanism path is instrumented)
- Before/after metrics shown (resolve rate, action count)

### Files Changed (Complete)

| File | Change | Lines |
|------|--------|-------|
| `scripts/swebench/oh_gt_full_wrapper.py` | Added 8 markers to has_evidence gate (live + legacy) | ~2938-2945, ~3125-3133 |
| `scripts/swebench/oh_gt_full_wrapper.py` | [GT_TRACE] structured trace at L3 post-edit | ~2947-2972 |
| `scripts/swebench/oh_gt_full_wrapper.py` | [GT_TRACE] structured trace at semantic check | ~2998-3018 |
| `scripts/swebench/oh_gt_full_wrapper.py` | Replaced semantic check one-liner with module call | ~2982-2985 |
| `src/groundtruth/hooks/semantic_check.py` | NEW — standalone semantic check module | 100 lines |
| `src/groundtruth/hooks/trace_fields.py` | NEW — TraceEvent + SuppressionReason enum | 95 lines |
| `src/groundtruth/hooks/post_edit.py` | Behavioral contract except:pass → logging | ~1017 |
| `tests/unit/test_semantic_check.py` | NEW — 16 tests for semantic check | 170 lines |
| `tests/unit/test_evidence_gate.py` | NEW — 6 tests for evidence gate | 170 lines |
| `tests/unit/test_post_edit_improved.py` | Updated 3 tests for behavioral contract priority | ~275-354 |
