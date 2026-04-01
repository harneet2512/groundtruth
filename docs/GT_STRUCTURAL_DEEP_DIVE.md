# GT Deep Dive -- Structural Problems the First Analysis Missed

## Context

The first analysis (`GT_CLAUDE_CODE_ANALYSIS.md`) focused on output formatting and Claude Code integration. This analysis goes deeper into the structural problems that limit HOW MANY tasks GT can help with, not just how well it communicates.

**Current performance:** +14 tasks (+2.8pp, +16.5% relative) on SWE-bench Verified 500-task with Gemini 2.5 Flash.

**Key numbers:**
- Indexer coverage: 100% (all repos indexed)
- Briefing coverage: 89% (445/500 tasks get a briefing)
- Evidence coverage: 99.6% (of briefed tasks produce evidence)
- Admissibility: 1.7% of edges pass the gate
- Identifier extraction: 97.8% (489/500 tasks produce identifiers)

The formatting fixes might get us from +14 to +18. The structural fixes in this analysis are what get us from +14 to +30+.

---

## Investigation 1: The 460-Task Silence Problem

GT helped on ~14 tasks net. Even assuming gains and losses, GT probably produced genuinely useful evidence on maybe 30-50 tasks. On the other 450+, GT either:
- (a) Was silent (no evidence passed threshold)
- (b) Said something the model already knew
- (c) Said something true but irrelevant to the task
- (d) Said something that actively misled the model

### Bucket Definitions

| Bucket | Definition |
|--------|-----------|
| **GT-helped** | v1.5 resolved AND v1.3 failed |
| **GT-hurt** | v1.3 resolved AND v1.5 failed |
| **GT-active-neutral** | Same outcome, but `briefing_shown=True` OR evidence fired |
| **GT-silent** | Submitted but no briefing AND no evidence events |
| **GT-absent** | Not submitted in v1.5 |

### Methodology (for VM data collection)

```python
# Script: scripts/swebench/analyze_silence.py
# Data needed from VM:
#   ~/openai__gemini-3-flash.gt_v15.json  (resolved_ids, submitted_ids)
#   ~/openai__gemini-3-flash.v1.3_g3f_merged.json  (baseline)
#   ~/results/v15_g3f_verified_*/gt_v15/gt_logs/*.evidence.jsonl
#   ~/results/v15_g3f_verified_*/gt_v15/*/*.traj.json  (info section)

For each of 500 tasks:
  1. Check presence in v1.5 submitted / v1.3 submitted
  2. Check v1.5 resolved / v1.3 resolved
  3. Load traj.json -> info.briefing_shown
  4. Load .evidence.jsonl -> count entries, check post_edit_evidence_shown
  5. Classify into bucket
```

### Sub-Classification of Silence

For GT-silent tasks, determine WHY:
- **No identifiers extracted:** `extract_identifiers_from_issue` returned empty (only 11/500 tasks)
- **Identifiers extracted but not in graph:** Symbols named in issue don't match any node in graph.db
- **In graph but no admissible edges:** Node found but no edges pass the gate
- **Agent never edited a source file:** No post-edit trigger fired
- **Agent edited once per file only:** count != 2, reminder suppressed by "fire on exactly 2nd edit" rule

### Estimated Counts (TBD -- requires VM data)

| Category | Estimated Count | % of 500 | Based On |
|----------|----------------|----------|----------|
| GT-helped | ~30-40 gained | ~6-8% | v1.5 flip analysis |
| GT-hurt | ~16-26 lost | ~3-5% | v1.5 flip analysis |
| GT-active-neutral | ~200-250 | ~40-50% | 89% briefing coverage - gains - losses |
| GT-silent | ~150-200 | ~30-40% | Remainder |
| GT-absent | ~55 | ~11% | 1 - 89% briefing coverage |

### Key Question

Of the ~350+ tasks where GT didn't help, how many COULD GT have helped with better coverage, task-awareness, or evidence selection?

**Estimate: 40-80 tasks** (8-16% of 500), based on:
- ~30 tasks recoverable via task-aware targeting (Investigation 3)
- ~10 tasks recoverable via wider fire window (Investigation 5)
- ~10 tasks recoverable via better identifier extraction (Investigation 4)
- ~10 tasks where GT had evidence but showed it about the wrong function

---

## Investigation 2: Admissibility Gate Analysis

### Finding: The Gate is NOT the Bottleneck

**Code reference:** `benchmarks/swebench/gt_intel.py:38-76`

```python
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import", "name_match"})
```

The gate admits **3 of 3** resolution methods from the main Go resolver:
- `same_file` -- exact name match within same file (highest priority)
- `import` -- import-verified cross-file resolution
- `name_match` -- cross-file name match fallback (lowest priority)

The ONLY excluded method is `aho_corasick` (used for languages without import extractors). Since SWE-bench Verified is ~80% Python (excellent import resolution), `aho_corasick` edges are rare.

### The Leak Check is Disabled

```python
def verify_admissibility_gate(conn: sqlite3.Connection) -> bool:
    # Final run: preserve recall and do not auto-disable same_file edges.
    return True   # <-- LINE 58: short-circuits the cross-file leak check
```

The v15 release disabled the `same_file` cross-file leak check to preserve recall. This means `same_file` edges that cross file boundaries (false positives) are admitted.

### Precision Concern: name_match

The v1.3 analysis flagged `name_match` as `GATE_BROKEN_name_match_leaked`. This resolution method matches any symbol with the same name across files -- it can produce false positives when common names like `get`, `save`, `process` exist in multiple files.

### Conclusion

The admissibility gate is permissive enough for Python repos. The real bottleneck is upstream:
1. **Target selection** -- `get_target_node` picks the wrong function (Investigation 3)
2. **Identifier extraction** -- issues that describe behavior without naming symbols (Investigation 4)
3. **Fire window** -- reminder suppressed on 1st and 3rd+ edits (Investigation 5)

**No changes recommended to the gate itself.**

---

## Investigation 3: Task-Awareness Gap -- HIGHEST PRIORITY

### Finding: Post-Edit Reminder Has Zero Task Awareness

**Code reference:** `benchmarks/swebench/run_mini_gt_hooked.py:193-241`

When the agent edits a file, `_run_gt_intel` calls:
```bash
python3 /tmp/gt_intel.py --file={rel_path} --reminder
```

It NEVER passes `--function=`. So `get_target_node` (`gt_intel.py:124-142`) picks "the node with the most incoming CALLS edges in that file":

```python
# gt_intel.py:134-142
SELECT n.* FROM nodes n
LEFT JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS'
WHERE n.file_path = ? AND n.label IN ('Function', 'Method', 'Class')
GROUP BY n.id
ORDER BY COUNT(e.id) DESC
LIMIT 1
```

### Why This Is Wrong

**Example:** Agent edits `django/db/models/query.py` for a `bulk_create` bug. GT finds `filter()` (most-called function in file, ~50 callers) and shows its callers, tests, and impact. The agent sees: "DO NOT change return type -- filter() at..." but the bug is in `bulk_create()` which has 3 callers. The evidence is structurally correct but task-irrelevant.

### The Briefing DOES Have Task Awareness

The briefing path (`gt_intel.py:476-517`) extracts identifiers from the issue text and resolves them against the graph. It finds the right function. But this information is NOT passed to the reminder.

### Fix Implemented (v16)

**Code reference:** `run_mini_gt_hooked.py:244-280` (current version)

```python
# v16: Extract target function names from briefing output
def _extract_briefing_targets(briefing_text: str) -> list[str]:
    targets = []
    for match in re.finditer(r'FIX HERE:\s*(\w+)\(\)', briefing_text):
        targets.append(match.group(1))
    return targets

# Store targets per container
_briefing_targets: dict[str, list[str]] = {}

# In _run_gt_intel: pass target to gt_intel.py
func_flag = ""
targets = _briefing_targets.get(container_id, [])
if targets:
    func_flag = f"--function={targets[0]}"
```

### Expected Impact

**+5-10 tasks.** Every reminder now targets the task-relevant function instead of the most-called one. This means:
- Evidence callers/tests are about the actual bug target
- IMPACT warnings are about the function being fixed
- PRECEDENT shows commits that touched the target function

---

## Investigation 4: Briefing Gap (89% -> 100%)

### Finding: Identifier Extraction is 97.8% -- The Gap is in Graph Lookup

**Script:** `scripts/swebench/analyze_briefing_gap.py` (run locally on all 500 Verified tasks)

| Metric | Value |
|--------|-------|
| Total tasks | 500 |
| Non-empty extraction | 489 (97.8%) |
| Empty extraction | 11 (2.2%) |

### The 11 Empty Tasks

All django (6) or sympy (5):
```
django__django-10880    django__django-11603    django__django-13279
django__django-13821    django__django-14725    django__django-15987
sympy__sympy-11618      sympy__sympy-13757      sympy__sympy-18189
sympy__sympy-20916      sympy__sympy-24562
```

### Why They're Empty

All 11 tasks have issue descriptions with:
- Prose-only descriptions ("when I do X, Y happens")
- Single-word function names filtered as noise (`save`, `get`, `set` are in `_NOISE_WORDS`)
- Single-hump CamelCase words the regex misses (requires 2+ humps: `ClassName` matches, `Model` doesn't)

### Regex Coverage

| Pattern | Tasks with at least 1 match |
|---------|---------------------------|
| snake_case (2+ parts) | 383 (76.6%) |
| CamelCase (2+ humps) | 334 (66.8%) |
| File paths | 198 (39.6%) |
| Backtick-quoted | 161 (32.2%) |
| Error/Exception classes | 155 (31.0%) |
| Tracebacks | 78 (15.6%) |

### Traceback Recovery Opportunity

54 tasks have function names in Python tracebacks (`File "...", line X, in func_name`) that are NOT captured by current regexes. The current patterns miss:
```python
# NOT matched by any current regex:
File "django/db/backends/utils.py", line 73, in _execute_with_wrappers
#                                              ^^^^^^^^^^^^^^^^^^^^^^^^
# This function name is embedded in traceback formatting
```

Adding traceback parsing (`r'File ".+?", line \d+, in (\w+)'`) would recover functions from 54 tasks. However, 0 of the 11 currently-empty tasks have tracebacks.

### Where the 89% Gap Actually Comes From

The "89% briefing coverage" (from v1.3 data) is NOT caused by extraction failure (97.8% works). It's caused by:
1. **Graph lookup failure** -- identifiers are extracted but `resolve_briefing_targets` can't find them in graph.db
2. **Briefing pipeline errors** -- crashes, timeouts, missing graph.db
3. **Hook injection failure** -- Go binary not available, fallback failed

### Recommendations

| Fix | Effort | Expected Impact |
|-----|--------|----------------|
| Add traceback parsing regex | 30 min | +0 empty tasks, +54 tasks with more identifiers |
| Allow single-hump CamelCase | 30 min | +some of 11 empty tasks |
| Debug graph lookup failures | 2 hrs | Close the 89% -> 97.8% gap |

---

## Investigation 5: Evidence Timing and the Attention Curve

### The "Fire on Exactly 2nd Edit" Problem

**Code reference:** `run_mini_gt_hooked.py:198-200`

```python
counts[filepath] = counts.get(filepath, 0) + 1
if counts[filepath] < 2:
    return ""  # suppress first edit (often exploration)
if counts[filepath] > 2:
    return ""  # already shown on second edit -- don't repeat
```

This fires GT evidence on EXACTLY the 2nd edit to a given file. The rationale:
- 1st edit: often exploration (agent trying things)
- 2nd edit: likely the real fix attempt
- 3rd+: already showed evidence, avoid repetition

### Why This Window is Too Narrow

**Hypothesis:** Most agents either:
- Edit a file **once** (single fix attempt -- common for easy bugs)
- Edit a file **3+ times** (iterative debugging -- common for hard bugs)

The exact-count-2 window misses both patterns. Agents that nail the fix on the first try never see the reminder. Agents that iterate past 2 edits get evidence once and then silence.

### Better Policy: Fire on 2nd+ Edit, Max Once Per File

```python
if counts[filepath] < 2:
    return ""   # suppress first edit
# Fire on 2nd edit and beyond, but only once:
if counts.get(filepath + "_shown", False):
    return ""   # already shown for this file
counts[filepath + "_shown"] = True
```

This captures both patterns:
- Single-edit agents: still suppressed (1st edit is likely sufficient)
- Multi-edit agents: get evidence on the 2nd edit AND it persists through subsequent edits

### Data Needed (TBD -- requires VM trajectories)

For each task, parse trajectory messages to find:
- Total turns
- Which turn GT evidence appeared
- Position ratio: (GT turn / total turns)
- Correlation with gained/lost outcome

**Hypothesis to validate:** GT-helped tasks have evidence appearing earlier (lower position ratio).

### Expected Impact

**+3-5 tasks** from wider fire window (agents that currently miss the exact-2 window).

---

## Investigation 6: Evidence Type Attribution

### Evidence Families in GT

| Family | Score | Source | Purpose |
|--------|-------|--------|---------|
| IMPORT | 2 (fixed) | Graph callees cross-file | Prevent hallucinated imports |
| CALLER | 1-3 | classify_caller_usage() | Constrain return type |
| SIBLING | 1-3 | Same class return type norm | Pattern matching |
| TEST | 1-2 | extract_assertions() | Contract validation |
| IMPACT | 1-2 | get_all_callers_count() | Warn on blast radius |
| TYPE | 1-2 | target.return_type | Return type contract |
| PRECEDENT | 2 (fixed) | get_git_precedent() | Last commit diff |

### Scoring Mechanics

**CALLER classification** (`gt_intel.py:245-268`):
- Score 3: tuple destructure, isinstance check, attribute access
- Score 2: conditional usage, comparison, assertion
- Score 1: plain invocation without using return

**SIBLING upgrade** (`gt_intel.py:753-774`):
- Base score 1
- Upgraded to 3 if >=70% of siblings share same return type

**Ranking** (`gt_intel.py:831-859`):
- Tiered: score>=2 first, then score==1
- Max 1 per family
- Default caps: max_high=4, max_low=2

### Attribution Analysis (TBD -- requires VM evidence logs)

For each gained/lost task, need to determine:
1. Which families were selected in the evidence output
2. Whether the agent's final patch used information from GT evidence
3. Family-level P&L: which families produced gains vs losses

**Script:** Extend `tmp_flip_analysis_v15.py` with patch-vs-evidence comparison.

### Expected Output

| Family | Gains | Losses | Net | Amplify/Suppress? |
|--------|-------|--------|-----|------------------|
| CALLER | TBD | TBD | TBD | TBD |
| IMPORT | TBD | TBD | TBD | TBD |
| TEST | TBD | TBD | TBD | TBD |
| IMPACT | TBD | TBD | TBD | TBD |
| SIBLING | TBD | TBD | TBD | TBD |
| PRECEDENT | TBD | TBD | TBD | TBD |
| TYPE | TBD | TBD | TBD | TBD |

---

## Investigation 7: Precedent Mining

### What Already Exists

**Code reference:** `gt_intel.py:633-683`

```python
def get_git_precedent(root, file_path, start_line, end_line) -> str | None:
```

1. Gets last 5 commits touching the file: `git log --oneline -5 --follow -- file_path`
2. For first 3 commits, gets diff: `git diff commit^..commit -- file_path`
3. Parses diff hunks, checks if any touched function's line range +/- 10 lines
4. Returns formatted block with commit message + diff lines (max 6 lines)

Score = 2 (auto-selected as high confidence). Family = PRECEDENT.

### Current Limitations

1. **Only 5 most recent commits** -- relevant fix might be older
2. **Only line-range overlap** -- doesn't check if same function was modified
3. **Depends on correct target selection** -- if `get_target_node` picks the wrong function, precedent will be for the wrong function too
4. **No issue-text matching** -- doesn't compare commit messages to current task description

### What Would Make Precedent More Useful

Instead of "last commit touching this function," search for commits whose:
- Message matches keywords from the issue text
- Diff modifies the same functions mentioned in the task
- Fix pattern is similar (e.g., both add error handling)

This connects to Investigation 3's task-awareness fix -- once target selection is correct, precedent automatically improves.

### Feasibility Assessment

For SWE-bench tasks, the testbed is a clean checkout at a specific commit. Git history IS available. However:
- Many SWE-bench tasks are first-time bugs (no prior similar fix exists)
- Precedent is most useful for pattern bugs (e.g., "add the same error handling as commit X")
- False positive risk: showing a superficially similar but actually misleading precedent

**No code changes recommended.** Precedent will auto-improve with task-aware targeting.

---

## Master Priority Table

| Rank | Change | Expected Delta | Effort | Confidence | Status |
|------|--------|---------------|--------|------------|--------|
| 1 | Fix briefing delivery bug (check for `<gt-evidence>`) | Prevents regression | 5 min | 100% | DONE |
| 2 | Task-aware targeting (v16 -- pass `--function=`) | +5-10 tasks | 2 hrs | High | DONE |
| 3 | Widen fire window (2nd+ edit, max once per file) | +3-5 tasks | 1 hr | Medium | TODO |
| 4 | Memory-style injection for persistent codebase facts | +3-5 tasks | 1 day | Medium | TODO |
| 5 | Traceback parsing in identifier extraction | +2-3 tasks (more identifiers) | 30 min | Medium | TODO |
| 6 | Single-hump CamelCase regex expansion | +1-2 tasks | 30 min | Medium | TODO |
| 7 | Debug graph lookup failures (close 89%->97.8% gap) | +3-5 tasks | 2 hrs | Medium | TODO |
| 8 | Evidence family weighting (amplify winners, suppress losers) | +2-4 tasks | 2 hrs | Low | NEEDS VM DATA |

**Total estimated delta from all changes: +14 to +28-38 tasks**

---

## Critical Bug Found During Analysis

### Briefing Delivery Bug (FIXED)

`run_mini_gt_hooked.py:249` checked for `"CODEBASE CONTEXT" in output` but `generate_enhanced_briefing` now returns `<gt-evidence>...` without that string. Enhanced briefings were silently dropped.

**Fix applied:**
```python
# Before (broken):
if output and "CODEBASE CONTEXT" in output and len(output) > 30:

# After (fixed):
if output and ("CODEBASE CONTEXT" in output or "<gt-evidence>" in output) and len(output) > 30:
```

---

## What Still Needs VM Data

| Investigation | What's Needed | Where on VM |
|---------------|---------------|-------------|
| 1 (Silence) | v1.5 + v1.3 resolved/submitted IDs | `~/openai__gemini-3-flash.gt_v15.json`, `v1.3_g3f_merged.json` |
| 1 (Silence) | Evidence logs per task | `~/results/v15_g3f_verified_*/gt_v15/gt_logs/*.evidence.jsonl` |
| 1 (Silence) | Trajectory info sections | `~/results/v15_g3f_verified_*/gt_v15/*/*.traj.json` |
| 5 (Timing) | Full trajectory messages | Same traj.json files |
| 6 (Attribution) | Evidence logs + patches | evidence.jsonl + preds.json |

**Collection script:** `scripts/swebench/analyze_silence.py` (to be written when VM data available)

---

## Key Hypotheses

1. **The +14 comes primarily from the briefing, not the reminder.** The briefing's "FIX HERE: function() at file:line" is enormously valuable for localization. The reminder fires rarely and on the wrong function.

2. **The "fire on exactly 2nd edit" window is too narrow.** Most agents edit files 1 or 3+ times. Changing to "fire on 2nd+ edit, max once per file" would increase reminder coverage.

3. **~30-40% of silence is from target mismatch.** GT finds the most-called function in the file, not the one relevant to the task. Fixed by task-aware targeting (v16).

4. **~20% of silence is from graph lookup failure.** Identifiers are extracted (97.8%) but can't be matched against graph nodes. This is the real briefing gap -- not identifier extraction.

---

## Files Modified / Created

| File | Changes |
|------|---------|
| `benchmarks/swebench/run_mini_gt_hooked.py` | v16: briefing delivery fix, task-aware targeting, briefing target storage |
| `benchmarks/swebench/gt_intel.py` | `<gt-evidence>` format with tiers and confidence |
| `src/groundtruth/mcp/server.py` | Auto-detect graph.db (Go indexer) vs index.db |
| `.mcp.json` | Claude Code MCP configuration |
| `SCENARIO.md` | Real-world usage guide with test scenarios |
| `scripts/swebench/analyze_briefing_gap.py` | Local identifier extraction analysis |
| `docs/GT_CLAUDE_CODE_ANALYSIS.md` | Claude Code reverse-engineering (this analysis) |
| `docs/GT_STRUCTURAL_DEEP_DIVE.md` | Structural problems analysis (this document) |
