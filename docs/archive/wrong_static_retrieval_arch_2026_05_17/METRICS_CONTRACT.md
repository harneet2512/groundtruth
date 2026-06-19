# METRICS_CONTRACT.md — GT Localization Metrics Specification

Generated: 2026-05-17  
Branch: `general_start`  
Source: `scripts/localization_metrics.py` + `src/groundtruth/pretask/v7_4_brief.py` diagnosis logging

---

## Metric 1: l1_hit@1

**Definition:** Binary — 1 if any gold file (or gold basename) appears in the first position of the L1 brief file list.

**Source Artifact:** `output.jsonl` → history[0:3] → content containing `gt-task-brief`

**Parser Logic:**
```python
# localization_metrics.py:62-63
l1_hit_1 = any(os.path.basename(f) in gold_basenames or f in gold_set for f in l1_files[:1])
```

**Edge Cases:**
- Brief not present in first 3 history entries → l1_files empty → hit=0
- Gold file has same basename as non-gold (e.g., `__init__.py`) → false positive
- Brief file path uses different prefix than gold patch path → full-path check fails, basename may save

**Manual Verification Method:**
1. Open output.jsonl, find `gt-task-brief` content
2. Extract first numbered file
3. Compare against `+++ b/` lines in `test_result.git_patch`
4. Verify basename match is correct (not colliding)

**Expected Range:** 0 or 1 per task. Aggregate: 0-100% across task set.

**Known Failure Modes:**
- Basename collision: `utils/__init__.py` vs `core/__init__.py` both match basename `__init__.py`
- Brief format change: if numbered format changes, parser silently returns empty list

---

## Metric 2: l1_hit@3

**Definition:** Binary — 1 if any gold file appears in positions 1-3 of the L1 brief.

**Source Artifact:** Same as hit@1.

**Parser Logic:**
```python
# localization_metrics.py:64
l1_hit_3 = any(os.path.basename(f) in gold_basenames or f in gold_set for f in l1_files[:3])
```

**Edge Cases:** Same as hit@1, plus: multi-gold tasks may have one gold at rank 4-5 which this misses.

**Manual Verification Method:** Same as hit@1, check first 3 files.

**Expected Range:** 0 or 1. Aggregate typically 0-40% on current 5-task set.

**Known Failure Modes:** Same as hit@1.

---

## Metric 3: l1_hit@5

**Definition:** Binary — 1 if any gold file appears in positions 1-5 of the L1 brief.

**Source Artifact:** Same as hit@1.

**Parser Logic:**
```python
# localization_metrics.py:65
l1_hit_5 = any(os.path.basename(f) in gold_basenames or f in gold_set for f in l1_files[:5])
```

**Edge Cases:** Same as hit@3. With adaptive K always returning 5, this checks the full brief.

**Manual Verification Method:** Same as hit@1, check all 5 files.

**Expected Range:** 0 or 1. Aggregate: measured 20% (1/5) on current set.

**Known Failure Modes:** Same as hit@1.

---

## Metric 4: MRR (Mean Reciprocal Rank)

**Definition:** 1/rank of first gold file in the L1 brief list. 0 if no gold file present.

**Source Artifact:** Same as hit@K.

**Parser Logic:**
```python
# localization_metrics.py:68-72
mrr = 0.0
for i, f in enumerate(l1_files):
    if os.path.basename(f) in gold_basenames or f in gold_set:
        mrr = 1.0 / (i + 1)
        break
```

**Edge Cases:**
- Multiple gold files: only first-appearing counts (may not be highest-ranked gold)
- Gold at rank > len(l1_files): MRR=0 (bounded by brief size)

**Manual Verification Method:** Find gold file rank in brief, compute 1/rank.

**Expected Range:** 0.0 to 1.0 per task. Aggregate mean: measured 0.10 on current set.

**Known Failure Modes:** Same basename collision as hit@K.

---

## Metric 5: candidate_set_contains_gold

**Definition:** Binary — 1 if any gold file is in the full candidate set (before adaptive K cutoff).

**Source Artifact:** `l1_ranking_diagnosis_{task}.json` → `gold_in_candidate_set`

**Parser Logic:**
```python
# v7_4_brief.py:506
"gold_in_candidate_set": bool(gold_set & set(all_files)),
```

**Edge Cases:**
- Requires GT_DEBUG_DIR env var to be set for file to be written
- Gold files must be provided to `run_v74()` (only available when `gold_files` param passed)
- If gold_files not provided, field is always False (empty gold_set)

**Manual Verification Method:** Open diagnosis JSON, check `gold_in_candidate_set` field. Cross-verify by checking `gold_files` list against `top_20[].path` entries.

**Expected Range:** 0 or 1. Should be high (>80%) for retrieval to work.

**Known Failure Modes:**
- Only available when `GT_DEBUG_DIR` is set — not available from output.jsonl alone
- Requires gold_files to be passed into scorer (only happens in eval mode with known gold)

---

## Metric 6: gold_rank_before/after_fusion

**Definition:** Rank of first gold file before graph fusion (BM25-only rank) vs after (full weighted rank).

**Source Artifact:** `l1_ranking_diagnosis_{task}.json` — derivable from component scores.

**Parser Logic:** NOT CURRENTLY IMPLEMENTED. Would require:
1. Sort candidates by `bm25_raw` alone → find gold rank = "before fusion"
2. Sort by `score` (final) → find gold rank = "after fusion" (already logged as `first_gold_rank`)

**Edge Cases:** Gold not in BM25 top-20 → before_fusion rank unknown.

**Manual Verification Method:** Sort diagnosis top_20 by `bm25_raw` DESC, find gold rank.

**Expected Range:** Rank 1-N or null.

**Known Failure Modes:** NOT IMPLEMENTED — requires post-hoc analysis of diagnosis JSON.

---

## Metric 7: first_gold_view_step

**Definition:** Action number (1-indexed count of non-think/non-message actions) at which agent first views a gold file.

**Source Artifact:** `output.jsonl` → history entries with `action == "read"` and path matching gold.

**Parser Logic:**
```python
# localization_metrics.py:93-97
if action == "read" and path:
    ...
    if first_gold_view is None and (basename in gold_basenames or any(g in path for g in gold_files)):
        first_gold_view = len(actions)
```

**Edge Cases:**
- Agent reads gold file via `cat` command (action != "read") → not counted
- Partial path match: `any(g in path for g in gold_files)` may match substrings incorrectly
- Gold file never viewed → returns None

**Manual Verification Method:** Walk output.jsonl history, find first "read" action where path contains a gold file path.

**Expected Range:** 1 to max_iterations. None if never viewed. Measured: 4-53 on resolving tasks.

**Known Failure Modes:**
- `any(g in path for g in gold_files)` is substring match — `auth.py` would match `/preauth.py`
- CmdRunAction with `cat file.py` not detected as "read"

---

## Metric 8: first_gold_edit_step

**Definition:** Action number at which agent first edits a gold file.

**Source Artifact:** `output.jsonl` → history entries with `action in ("edit", "write")` or `str_replace` in args.

**Parser Logic:**
```python
# localization_metrics.py:100-107
if action in ("edit", "write") or "str_replace" in str(args):
    if path and "TASKS" not in path and "scaffold" not in path:
        ...
        if first_gold_edit is None and (basename in gold_basenames or any(g in path for g in gold_files)):
            first_gold_edit = len(actions)
```

**Edge Cases:**
- Agent edits via `sed` command → not detected
- Scaffold/TASKS filter may exclude legitimate edits with "scaffold" in path
- Same substring match issue as first_gold_view

**Manual Verification Method:** Walk history, find first edit-type action on gold file.

**Expected Range:** first_edit_step to max_iterations. None if never edited gold.

**Known Failure Modes:** Same as first_gold_view + `str_replace` detection relies on string presence in args.

---

## Metric 9: files_viewed_before_gold

**Definition:** Number of file-view actions before first gold file view.

**Source Artifact:** Derived from `first_gold_view_step`.

**Parser Logic:**
```python
# localization_metrics.py:188
"files_viewed_before_gold": first_gold_view if first_gold_view else len(files_viewed),
```

**Edge Cases:**
- If gold never viewed, reports total files viewed (confusing — should be "N/A")
- Counts action_count at gold view, not unique files before gold

**Manual Verification Method:** Count distinct file reads before first gold read.

**Expected Range:** 0 to total_files_viewed.

**Known Failure Modes:**
- MISLEADING: reports `first_gold_view` (action count) not distinct files viewed. Name implies file count but value is action count.

---

## Metric 10: action_count

**Definition:** Total non-think, non-message, non-recall actions in trajectory.

**Source Artifact:** `output.jsonl` → history.

**Parser Logic:**
```python
# localization_metrics.py:89-90
if action and action not in ("think", "recall", "message"):
    actions.append(i)
...
"action_count": len(actions)
```

**Edge Cases:**
- Empty string action → excluded
- Custom action types (agent-specific) → included if not in exclusion set
- OH 0.54 action types: "run", "read", "write", "edit", "browse" — all counted

**Manual Verification Method:** Count history entries with action not in {"think", "recall", "message", ""}.

**Expected Range:** 10-100 typical. Measured: 26-93 on current 5-task set.

**Known Failure Modes:** Reliable — action field is always present in OH output.

---

## Metric 11: edit_count

**Definition:** Number of file edit actions (excluding scaffold/TASKS files).

**Source Artifact:** `output.jsonl` → history with edit/write/str_replace actions.

**Parser Logic:**
```python
# localization_metrics.py:100-103
if action in ("edit", "write") or "str_replace" in str(args):
    if path and "TASKS" not in path and "scaffold" not in path:
        files_edited.append(...)
...
"edit_count": len(files_edited)
```

**Edge Cases:**
- Multiple edits to same file counted separately
- `str_replace` check on stringified args may match accidentally

**Manual Verification Method:** Count edit-type history entries with valid paths.

**Expected Range:** 1-30 typical.

**Known Failure Modes:** Reliable for standard OH edit actions.

---

## Metric 12: action_economy_vs_baseline

**Definition:** Ratio of GT action_count / baseline action_count for same task.

**Source Artifact:** Requires PAIRED run artifacts (GT + baseline for same task).

**Parser Logic:** NOT IMPLEMENTED in localization_metrics.py. Computed manually:
```
economy = gt_action_count / baseline_action_count
```

**Edge Cases:**
- Baseline unavailable → metric not computable
- Baseline resolves but GT doesn't (or vice versa) → ratio misleading
- Different random seeds → action counts vary naturally

**Manual Verification Method:** Run both arms on same task, divide action counts.

**Expected Range:** 0.5-3.0. <1.0 = GT more efficient. Measured: 0.65-3.21 on current set.

**Known Failure Modes:** NOT IMPLEMENTED — requires separate baseline run artifacts.

---

## Metric 13: edit_file_precision

**Definition:** Fraction of edited files (by basename) that are gold files.

**Source Artifact:** `output.jsonl` → edited files + gold files from patch.

**Parser Logic:**
```python
# localization_metrics.py:147-150
edit_basenames = {bn for _, _, bn in files_edited}
if edit_basenames:
    edit_precision = len(edit_basenames & gold_basenames) / len(edit_basenames)
else:
    edit_precision = 0.0
```

**Edge Cases:**
- Basename collision (editing wrong `__init__.py`) → inflated precision
- Agent edits test files that happen to be gold → counted correctly
- 0 edits → precision = 0.0

**Manual Verification Method:** List edited basenames, intersect with gold basenames, divide.

**Expected Range:** 0.0-1.0. Higher is better.

**Known Failure Modes:** Basename collision (same issue as hit@K).

---

## Metric 14: l3b_visible_events

**Definition:** Count of history entries containing "[GT]" marker text.

**Source Artifact:** `output.jsonl` → history entry content.

**Parser Logic:**
```python
# localization_metrics.py:110-111
if "[GT]" in content:
    gt_events.append((i, content))
...
"l3b_visible_events": len(gt_events)
```

**Edge Cases:**
- Agent generating "[GT]" in its own output → false positive (unlikely)
- GT evidence that doesn't contain "[GT]" marker → not counted
- Counts ALL GT events (L1, L3, L3b combined), not just L3b

**Manual Verification Method:** grep output.jsonl for "[GT]" occurrences.

**Expected Range:** 0-15 typical. Budget: L3b=3 + L3=5 + L1=1 = max 9 direct fires.

**Known Failure Modes:**
- MISLEADING NAME: counts ALL GT events, not just L3b. Name suggests L3b-only.

---

## Metric 15: l3b_bridge_events

**Definition:** GT "Next: read X" suggestions where X is a gold file that agent hasn't visited yet.

**Source Artifact:** `output.jsonl` → GT events containing "Next: read" + gold file matching.

**Parser Logic:**
```python
# localization_metrics.py:134-144
if "[GT]" in content:
    if "Next: read" in content:
        ...
        elif any(suggested_rel.endswith(g) or g in suggested_rel for g in gold_files):
            l3b_bridges += 1
```

**Edge Cases:**
- Suggested path format may not match gold path format
- Substring match (`g in suggested_rel`) may false-positive
- Only counts "Next: read" — other navigation suggestions missed

**Manual Verification Method:** Find GT events with "Next: read", check if target is gold file.

**Expected Range:** 0-3 (limited by L3b budget of 3 fires). Measured: 0-1 on current set.

**Known Failure Modes:**
- Very narrow definition: only "Next: read" text format counts
- Actual navigation evidence (callers/callees listing gold files) NOT counted as bridges
- Substring match direction: `g in suggested_rel` can match partial paths

---

## Metric 16: stale_guidance_count

**Definition:** GT "Next: read X" suggestions where X was already viewed by the agent.

**Source Artifact:** `output.jsonl` → GT events + viewed file timeline.

**Parser Logic:**
```python
# localization_metrics.py:135-141
if "Next: read" in content:
    next_part = content.split("Next: read")[-1].strip().split("\n")[0].strip()
    suggested_rel = next_part.split("/workspace/")[-1] if "/workspace/" in next_part else next_part
    if suggested_rel in already_viewed_paths:
        stale_count += 1
    elif any(suggested_rel in vp or vp.endswith(suggested_rel) for vp in already_viewed_paths):
        stale_count += 1
```

**Edge Cases:**
- Path normalization: `/workspace/` prefix stripped but other prefixes not
- Partial path matching (`suggested_rel in vp`) may false-positive on short paths
- "Called by:" evidence NOT counted as stale (correct — relationship updates)

**Manual Verification Method:**
1. Find GT events with "Next: read X"
2. Check if X appears in any prior "read" action path
3. Verify path normalization is consistent

**Expected Range:** 0-5. Target: <3. Measured: 2 on current set.

**Known Failure Modes:**
- Path format mismatch between GT output and OH action paths
- `/workspace/repo_name/` vs relative path ambiguity

---

## Metric 17: late_guidance_count

**Definition:** GT evidence arriving after agent has already made the decision it was meant to inform.

**Source Artifact:** NOT IMPLEMENTED.

**Parser Logic:**
```python
# localization_metrics.py (always returns 0)
late_count = 0  # initialized but never incremented
```

**Edge Cases:** N/A — not implemented.

**Manual Verification Method:** Would require: detecting when agent commits to a code path, then checking if GT evidence arrives after that commitment point.

**Expected Range:** Unknown — not measured.

**Known Failure Modes:** METRIC IS DEAD — always returns 0. No implementation exists.

---

## Metric 18: resolved (downstream outcome)

**Definition:** Binary — whether the task's FAIL_TO_PASS tests pass after agent's patch.

**Source Artifact:** `eval_result.json` from SWE-bench harness.

**Parser Logic:**
```python
# localization_metrics.py:157-172
# Searches multiple paths for eval_result.json
resolved = v.get("resolved", False)
```

**Edge Cases:**
- eval_result.json path varies by harness version
- Multiple tasks in same eval file — matches first dict value
- Eval not run → resolved always False

**Manual Verification Method:** Check eval_result.json for task entry.

**Expected Range:** 0 or 1. Measured: 2/5 on current set.

**Known Failure Modes:** Path search is fragile — relies on relative `../..` navigation.

---

## Metric 19: fix_rate (downstream outcome)

**Definition:** Fraction of FAIL_TO_PASS tests that pass, zeroed if any PASS_TO_PASS test regresses.

**Source Artifact:** `eval_result.json` → tests_status.

**Parser Logic:**
```python
# localization_metrics.py:163-171
f2p_s = len(f2p.get("success", []))
f2p_t = f2p_s + len(f2p.get("failure", []))
p2p_f = len(p2p.get("failure", []))
fix_rate = 0.0 if p2p_f > 0 else (f2p_s / f2p_t if f2p_t > 0 else 0.0)
```

**Edge Cases:**
- PASS_TO_PASS regression zeroes entire fix_rate (harsh penalty)
- No FAIL_TO_PASS tests → fix_rate = 0.0
- Partial fix (some F2P pass) → fractional rate

**Manual Verification Method:** Count FAIL_TO_PASS successes/total from eval_result.json.

**Expected Range:** 0.0-1.0. Often binary (0 or 1) for single-test tasks.

**Known Failure Modes:** Same path fragility as `resolved`.

---

## Missing Metrics (specified in /goal but not implemented)

| Metric | Status | Blocker |
|--------|--------|---------|
| `gold_rank_before_fusion` | NOT IMPLEMENTED | Requires sorting diagnosis by BM25 only |
| `gold_rank_after_fusion` | PARTIALLY — logged as `first_gold_rank` in diagnosis JSON | Only in diagnosis file, not offline metrics |
| `action_economy_vs_baseline` | NOT IMPLEMENTED | Requires paired baseline run |
| `late_guidance_count` | DEAD (always 0) | No temporal commitment detection |
| Classification taxonomy (instruction_stale, relationship_update, late_telemetry, useful_next_action) | NOT IMPLEMENTED | Requires richer GT event parsing |

---

## Verification Status

| Metric | Has Parser | Has Test | Manually Verified | Status |
|--------|-----------|---------|-------------------|--------|
| l1_hit@1 | YES | NO | PARTIAL (1/5 tasks) | NEEDS TESTS |
| l1_hit@3 | YES | NO | PARTIAL | NEEDS TESTS |
| l1_hit@5 | YES | NO | PARTIAL | NEEDS TESTS |
| MRR | YES | NO | PARTIAL | NEEDS TESTS |
| candidate_set_contains_gold | YES (diagnosis) | NO | NO | NEEDS VERIFICATION |
| gold_rank_before_fusion | NO | NO | NO | NOT IMPLEMENTED |
| gold_rank_after_fusion | PARTIAL (diagnosis) | NO | NO | NEEDS VERIFICATION |
| first_gold_view_step | YES | NO | PARTIAL | NEEDS TESTS |
| first_gold_edit_step | YES | NO | NO | NEEDS VERIFICATION |
| files_viewed_before_gold | YES (MISLEADING) | NO | NO | NEEDS FIX + TESTS |
| action_count | YES | NO | YES | OK |
| edit_count | YES | NO | PARTIAL | NEEDS TESTS |
| action_economy | NO (manual) | NO | PARTIAL | NOT IMPLEMENTED |
| edit_file_precision | YES | NO | NO | NEEDS VERIFICATION |
| l3b_visible_events | YES (MISLEADING NAME) | NO | PARTIAL | NEEDS RENAME |
| l3b_bridge_events | YES | NO | PARTIAL | NEEDS TESTS |
| stale_guidance_count | YES | NO | YES (10 examples) | OK |
| late_guidance_count | DEAD | NO | N/A | NOT IMPLEMENTED |
| resolved | YES | NO | YES | OK |
| fix_rate | YES | NO | PARTIAL | NEEDS TESTS |

---

## Known Issues Requiring Fix

1. **`files_viewed_before_gold`** reports action_count at gold view, not distinct files viewed. Name is misleading.
2. **`l3b_visible_events`** counts ALL GT events (L1+L3+L3b), not just L3b. Should be renamed `gt_visible_events`.
3. **`late_guidance_count`** is always 0 — dead metric, needs implementation or removal.
4. **`l3b_bridge_events`** only counts "Next: read" format — misses caller/callee navigation that points to gold.
5. **Basename matching** in hit@K and edit_precision can produce false positives on common filenames.
6. **first_gold_view substring match** (`any(g in path for g in gold_files)`) can match partial paths incorrectly.
