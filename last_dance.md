# last_dance.md — What Works, What Doesn't, What to Fix

Tag: `pre_flip_1` (commit 5ae3614f)
Date: 2026-05-18

---

## Current Score

- GT+agent: **3/5 resolved** (official eval) on dev tasks
- Baseline: **0/15** on 30-task partial run, **1/3** on shared 5 tasks
- Positive flips: **+2** (beets + weasyprint over baseline)
- Avg actions: **48** (vs 48 baseline — parity)
- First gold view: **step 2-5** (vs step 26 baseline — 10x faster)

---

## Mechanism Status (Honest)

### WORKS RELIABLY

| Mechanism | Rate | File | Line | Why It Works |
|-----------|------|------|------|-------------|
| L1 Brief | 5/5 | `src/groundtruth/pretask/v7_4_brief.py` | ~419 | Keyword match + graph ranking. Gold in candidates 4/5. |
| L3 Router | 5-14/task | `src/groundtruth/router/router.py` | 98-230 | First-per-file dedup. View/edit separate keys. Edits bypass budget. |
| [5] L1 Keyword | 4/5 | `src/groundtruth/pretask/v7_4_brief.py` | 427 | Exact basename match = 1.0. Proven. |

### WORKS SOMETIMES (graph/timing dependent)

| Mechanism | Rate | File | Line | Why It Doesn't Always Fire |
|-----------|------|------|------|---------------------------|
| [4] Constraint | 3/5 | `scripts/swebench/oh_gt_full_wrapper.py` | ~3013 | Needs hook to produce caller evidence. Graph quality dependent. loguru-1306: no function-level callers. |
| [6] Recall | 4/5 | `scripts/swebench/oh_gt_full_wrapper.py` | ~2555 (cache), ~3007 (inject) | Needs agent to READ file before EDITING it. Direct edits skip recall. |
| [10] Scope | 3/5 | `scripts/swebench/oh_gt_full_wrapper.py` | ~3028 | Needs cross-file callers in graph. Works when graph has data. |
| [1] L4 Symbol | 4/5 | `scripts/swebench/oh_gt_full_wrapper.py` | ~3346 | Issue tokens must match graph node names. loguru-1306: 0/1 (no match). |
| L5 Scaffold | 2/5 | `src/groundtruth/trajectory/governor.py` | ~130 | Fires at 20% iters with 0 edits. Doesn't help when agent edits early but wrong. |

### NOT WORKING (0/5 every run)

| Mechanism | File | Line | Root Cause |
|-----------|------|------|-----------|
| **[9] Semantic** | `scripts/swebench/oh_gt_full_wrapper.py` | ~2965 | In-container Python snippet fails silently. 0 GUARD_ADDED/RETURN_PATH output EVER. Possible causes: (a) git not initialized in container, (b) regex escaping wrong in shell, (c) Python snippet syntax error swallowed by try/except, (d) file path wrong. |
| **[3] Behavioral contract** | `src/groundtruth/hooks/post_edit.py` | ~966 | Priority 0.5 block: queries graph.db for func start/end → reads file → imports `_regex_extract_guards`. Runs IN CONTAINER. Possible causes: (a) graph query returns None for start/end, (b) `from groundtruth.evidence.change import _regex_extract_guards` fails (module not in PYTHONPATH in container), (c) file path doesn't resolve. |

### PRAY AND SPRAY (unreproducible)

| Mechanism | File | Line | Root Cause |
|-----------|------|------|-----------|
| **[2] Tools** | `patches/oh054/apply_gt_tools.py` | patches OH | OH patch marker strings fragile. GHA cache may skip reinstall. 1/5 on one run, 0/5 on others. Agent calls gt_query once then stops — no budget enforcement visible. |

### DEAD (telemetry only)

| Mechanism | File | Line | Root Cause |
|-----------|------|------|-----------|
| **[7] L6 Auto-consumer** | `scripts/swebench/oh_gt_full_wrapper.py` | ~2886 | Tracks caller count delta in `config.evidence_cache`. Prints `[GT_META]` log. NEVER injects into agent observation. Agent can't see it. |
| **[8] Adaptive L5** | `src/groundtruth/trajectory/governor.py` | ~133 | Governor inits BEFORE B-7 download. `os.environ["GT_GRAPH_DB"]` set after download but governor already cached threshold=20. |

---

## What To Fix (Priority Order)

### 1. [9] Semantic — diagnose in-container snippet failure

**File:** `scripts/swebench/oh_gt_full_wrapper.py` lines 2965-3002

**Diagnosis needed:** Add explicit error logging to the `except Exception: pass` block. Change to `except Exception as e: print(f"[GT_META] semantic error: {e}", flush=True)`. Then check: does git exist? Does the file path resolve? Does the regex produce output?

**Test:** Run the exact Python snippet manually in a SWE-bench container and see what error it produces.

### 2. [3] Behavioral contract — diagnose in-container import failure

**File:** `src/groundtruth/hooks/post_edit.py` lines 966-1005

**Diagnosis needed:** Add logging inside the try/except block. Check: does `from groundtruth.evidence.change import _regex_extract_guards` work in the container's PYTHONPATH? Does the graph query for func start/end return valid values?

**Test:** Add `print(f"[GT_META] behavioral: func_start={func_start} func_end={func_end}")` before the guard extraction.

### 3. [8] Adaptive L5 — fix init timing

**File:** `src/groundtruth/trajectory/governor.py` line ~133
**File:** `scripts/swebench/oh_gt_full_wrapper.py` line ~3702

**Fix:** The governor is initialized at line 3644 BEFORE the B-7 download at line 3699. Move governor init AFTER download, or invalidate `_cached_scaffold_threshold` after download.

### 4. [7] L6 Auto-consumer — make agent-visible

**File:** `scripts/swebench/oh_gt_full_wrapper.py` line ~2886

**Fix:** After detecting caller count delta, inject a 1-line message into the NEXT agent observation: "[GT] Caller count changed after edit: {old} → {new} callers"

### 5. [2] Tools — ensure OH patch applies reliably

**File:** `patches/oh054/apply_gt_tools.py`
**File:** `.github/actions/setup-eval/action.yml`

**Fix:** Add a verification step after patching: `python3 -c "from openhands.agenthub.codeact_agent.tools.gt_tools import GtQueryTool; print('GT tools registered')"`. If it fails, the patch didn't apply.

### 6. [4] Constraint — add file-level caller fallback

**File:** `src/groundtruth/hooks/post_edit.py` (in `_get_callers_from_graph`)

**Fix:** When function-level caller query returns empty, fall back to file-level query (same as L3b post_view uses). The callers exist at file level.

---

## Files That Matter

| File | What It Does | Lines That Matter |
|------|-------------|-------------------|
| `scripts/swebench/oh_gt_full_wrapper.py` | ALL integration | 2500-3060 (L3b/L3 delivery), 3346-3434 (L4), 3644-3710 (init + B-7) |
| `src/groundtruth/router/router.py` | WHEN to emit | 98-230 (on_view/on_edit), 385-394 (_accept dedup) |
| `src/groundtruth/hooks/post_edit.py` | WHAT evidence | 759-805 (format_risk_evidence), 966-1005 (behavioral contract) |
| `src/groundtruth/hooks/post_view.py` | WHAT on read | 415-446 (caller code + source line) |
| `src/groundtruth/trajectory/governor.py` | L5 decisions | 130-155 (scaffold trap), 370-430 (multi-file scope) |
| `src/groundtruth/pretask/v7_4_brief.py` | L1 brief | 419-448 (keyword scoring) |
| `patches/oh054/apply_gt_tools.py` | OH tool patch | All (fragile marker matching) |
| `scripts/deep_metrics.py` | Measurement | All (instant metrics for any run) |
| `fliperachu.md` | Analysis | All (causal analysis + evidence hierarchy) |
