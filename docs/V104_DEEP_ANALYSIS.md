# GT v1.0.4 Deep Functionality Analysis

Two A/B runs completed. Both delta=0. This document analyzes exactly what each GT component did, why, and what it means.

---

## 1. Pre-Built Index (gt-index Go Binary)

**What it did:** Built a graph.db for every task container. Parsed 1,051-1,064 astropy source files per task in ~5-6 seconds. Produced nodes (functions/classes), edges (calls), properties, and assertions.

**How it worked:**
- Mini-swe-agent: `_inject_v11()` copies the Go binary via `docker cp`, runs `/tmp/gt-index --root=/testbed --output=/tmp/gt_graph.db --max-files=5000`
- SWE-agent: `install.sh` in the GT tool bundle copies gt-index from `/root/tools/groundtruth/bin/` to `/tmp/`, runs the same command

**What the graph contained:**
- Predominantly `same_file` edges (1-39 per task). Zero `import` edges in most tasks, zero `name_match` edges.
- This means the astropy graph is heavily intra-file. Cross-file call resolution via import extractors found nothing because astropy's Python imports are complex (`from . import` relative imports, lazy imports, `__init__.py` re-exports) and the Go indexer's Python import extractor doesn't resolve them.

**Impact on results:** The graph exists and is queryable, but its edge coverage is too shallow for cross-file evidence families (OBLIGATION, NEGATIVE) to fire meaningfully.

---

## 2. Pre-Task Briefing

**What it did:** Extracted identifiers from the issue text (CamelCase, backtick-quoted, file paths), queried graph.db for matching symbols, produced a 3-line briefing prepended to the task prompt.

**How it worked:**
- Fires once per task before the agent starts
- `_generate_briefing()` calls `gt_intel.py --enhanced-briefing --issue-text=@/tmp/issue.txt`
- Extracts symbols like `separability_matrix`, `CompoundModel`, `_coord_matrix`
- Resolves them against graph.db nodes

**What actually happened:**
- Every task got a briefing (10/10 = 100% delivery rate)
- Each briefing was exactly 3 lines
- `briefing_targets` was `[]` for most tasks (the FIX HERE pattern didn't match the briefing output format)

**Why it didn't flip tasks:** 3 lines of "here's where the relevant function is" is grep-equivalent. The agent already does `find` and `grep` in its first steps. The briefing confirmed what the agent was about to discover anyway.

---

## 3. Post-Edit Evidence (Hook)

### Mini-swe-agent (monkey-patch hook)

**What it did:** After every agent command that matched `_EDIT_INDICATORS` (sed, cat >, patch, etc.), checked `git diff --name-only` for modified source files, ran `gt_intel.py --reminder` on the first modified file.

**Evidence families that fired:**

| Family | Tasks | What it said | Value added |
|--------|-------|-------------|-------------|
| PRECEDENT | 8/10 | Last git commit touching the function | Navigation aid (grep-equivalent) |
| IMPACT | 7/10 | "N callers in M files" | Awareness of blast radius |
| SIBLING | 4/10 | "sibling method in same class (N total)" | Pattern conformity hint |
| CALLER | 1/10 | Cross-file caller details | Structural constraint |
| TEST | 1/10 | Test assertions referencing target | Behavioral contract |
| OBLIGATION | 0/10 | (never fired) | - |
| NEGATIVE | 0/10 | (never fired) | - |
| CRITIQUE | 0/10 | (never fired) | - |

**Why OBLIGATION never fired:** Requires `caller_usage` properties (destructure, iterate, boolean_check, exception_guard) from the graph. These properties come from the Go indexer's `classifyCallContext()` function which analyzes AST context around call sites. For astropy's same_file-only edges, caller usage classification exists but the `deterministic_only=True` + 2-caller minimum floor filters out everything. No function had 2+ callers with the same classified usage pattern via import-resolved edges.

**Why NEGATIVE never fired:** Checks if callees are unexported (`is_exported=0`) and called from another file. Since all edges are `same_file`, cross-file unexported symbol access doesn't exist in this graph.

**Why CRITIQUE never fired:** `compute_critique_standalone()` compares the function's current signature against the DB signature. It fires when arity increases or a function is removed. The agent's edits in these astropy tasks didn't change function signatures or remove functions. They changed internal logic (e.g., `= 1` to `= right` in separable.py).

### SWE-agent (state_command + gt_check tool)

**What it did:** Two delivery mechanisms:
1. `_state_gt` (runs after every action): Checks file mtimes, computes evidence if a source file was recently modified, injects into `{{gt_evidence}}` template variable
2. `gt_check <file>` (on-demand tool): Agent explicitly calls it to get evidence for a file

**What actually happened:**
- `_state_gt` delivered evidence in **1/10 tasks** (astropy-13236 only)
- `gt_check` was called by the agent in **1/10 tasks** (also astropy-13236)
- The evidence delivered was: `[VERIFIED] CAUTION: 38 callers in 1 files` + `[WARNING] MATCH PATTERN: sibling method in same class (7 total)`

**Why only 1/10 state_command evidence:** The mtime-based detection in `_state_gt` tracks `gt_shown_mtimes.json`. Once evidence is shown for a file, it's not shown again until the mtime changes. SWE-agent uses `str_replace_editor` which modifies files, but the state_command runs in a separate Python process that checks mtimes. The timing and file write granularity means most edits were missed.

**Why gt_check was only called once:** The agent has no system prompt guidance telling it to use `gt_check`. The tool is listed in the available commands, but the agent (Qwen3-Coder) chose to use `str_replace_editor` and `bash` tools almost exclusively. In 10 tasks with 396 total steps, `gt_check` was used exactly once.

---

## 4. Incremental Re-Index

**What it did:** After detecting a file edit, re-indexes only the changed file by computing its SHA-256 hash, comparing against `file_hashes`, deleting old nodes/edges, re-parsing, and re-inserting.

**What actually happened:**
- Mini-swe-agent: Called `_run_incremental_reindex()` before evidence queries. The incremental binary ran in ~1-2ms for changed files, ~0ms for unchanged files.
- SWE-agent: The `_state_gt` script calls `gt-index --incremental` before evidence queries. Observed 1 incremental event (astropy-13236: `{"incremental":true,"files":0,"changed":0,"time_ms":1}` = file unchanged, skipped).

**Why it had no impact:** The incremental re-index is designed for when the agent changes function signatures or adds/removes functions. In these tasks, the agent's edits were internal logic changes (changing a value, adding an if-branch) that don't change the graph structure. The index was correct before and after the edit.

---

## 5. Test File Filter

**What it did:** Added `_is_test_path()` to prevent test files from appearing in TARGET or ALSO evidence. Added `AND n.is_test = 0` to `get_target_node()` queries.

**What actually happened:** Worked correctly. Zero test files appeared in any TARGET evidence across all 20 tasks (10 mini-swe-agent + 10 SWE-agent). This is the one gate that passed cleanly.

**Verified by:** The `is_test` column in graph.db was populated by the Go walker's `IsTestFile()` function, and the SQL filter prevented any test node from being selected as a target.

---

## 6. Edit-Trigger Detection

**What it did:** Controls when evidence/critique fires. Only triggers on strong edit signals (`sed -i`, `cat >`, `patch`, `str_replace_editor`, etc.), not on read-only commands.

**What actually happened:**
- Mini-swe-agent: The edit indicators matched correctly. Evidence fired 5-23 times per task depending on how many edits the agent made. The v1.0.4 fix (gating on `_EDIT_INDICATORS` instead of firing on every `git diff` return) prevented the 1,150-event flood seen in the first run.
- SWE-agent: Uses `str_replace_editor` which is in the indicator list. However, `_state_gt` uses mtime-based detection instead of command-based indicators, so the trigger mechanism is different.

---

## 7. Injection Budget

**What it did:** Caps at 3 injections per task, 5 lines per injection. Prevents evidence flooding.

**What actually happened:**
- Mini-swe-agent: The budget was never exhausted. Most tasks had 5-17 evidence events but these went through `_run_gt_intel` dedup (tracked by `_shown_files`), not the injection budget. The critique budget (`_injection_counts`) was never incremented because critique never fired.
- SWE-agent: The budget lives in the Python-side hook code. Since `_state_gt` is a separate process and `gt_check` was called only once, the budget was irrelevant.

---

## 8. Evidence Ranking

**What it did:** Ranks evidence by: NEGATIVE > OBLIGATION > TEST > CALLER > IMPORT > PRECEDENT > IMPACT > TYPE > SIBLING. Caps per family (NEGATIVE: 2, OBLIGATION: 2, TEST: 3, CALLER: 3, etc.). Token budget of 450.

**What actually happened:** Only PRECEDENT, IMPACT, SIBLING, CALLER, and TEST fired. The ranking worked correctly within those families:
- CALLER (score=2) ranked above IMPACT (score=2) by family priority
- TEST (score=2) ranked highest among fired families
- PRECEDENT consistently appeared (most common family)

**Why the ranking didn't matter:** All fired families produce navigation-level evidence. The high-priority families (NEGATIVE, OBLIGATION) that would produce behavioral constraints never fired. Re-ordering IMPACT vs PRECEDENT doesn't change agent behavior.

---

## 9. Freshness Invariant

**What it did:** Checks if graph.db is stale relative to the source file. Uses SHA-256 hash comparison against `file_hashes` table. If stale, suppresses evidence entirely (returns "SUPPRESS").

**What actually happened:** The freshness check ran on every `gt_intel.py` invocation. Since the graph was built at container start and the incremental reindex updated hashes for changed files, the freshness check always returned "fresh" (hash matched or file not in hash table → mtime fallback → fresh). No evidence was ever suppressed due to staleness.

---

## 10. Standalone Critique

**What it did:** `compute_critique_standalone()` compares the current file on disk against the graph.db to detect:
- BREAKING: Function signature arity increased (added required params)
- STALE: Function removed from file but still referenced

**What actually happened:** Called from `_run_critique()` in the mini-swe-agent hook. Never produced output because:
1. The agent's edits didn't change function signatures (they changed internal logic)
2. The agent didn't remove any functions
3. The regex `def\s+{name}\s*\(` always found the function (it still existed)

The fix for the removal detection bug (splitting into exists vs missing branches) is correct but was never exercised in these runs because no function was removed.

---

## Root Cause: Why Delta = 0

The delta is zero because GT's current evidence is **structurally sound but behaviorally irrelevant** to these tasks.

**What the agent needed to solve the unresolved tasks:**
- **13033 (timeseries):** Understanding that `_check_required_columns` error message format matters for downstream string parsing
- **13236 (table):** Understanding that `__setitem__` on a masked column must preserve mask state
- **13398 (coordinates):** Understanding the transform graph's documentation generation pipeline
- **13977 (units):** Understanding the Quantity `__array_ufunc__` protocol for structured dtypes
- **14182 (io.ascii):** Understanding RST header parsing edge cases

**What GT provided:** "This function has 3 callers in 1 file" and "last commit was 6 months ago." This is true but doesn't help the agent write the correct fix.

**What GT would need to provide to flip these tasks:**
- OBLIGATION: "All 5 callers iterate the return value — return type must remain iterable"
- NEGATIVE: "You imported `validate_input` from `auth` but `auth` doesn't export it"
- CRITIQUE: "You added a required parameter `mode` — 12 callers still use old arity"

These require:
1. **Cross-file edges** (import-resolved, not same_file) — the Go indexer's Python import extractor needs to handle relative imports, `__init__.py` re-exports, and lazy imports
2. **Caller usage classification on cross-file edges** — the `classifyCallContext()` function works but only on same_file edges in practice
3. **Agent edits that change structural contracts** — these tasks involve logic fixes, not API changes

---

## Recommendations

1. **Fix Python import resolution in the Go indexer** — this is the single highest-leverage improvement. Without cross-file edges, OBLIGATION/NEGATIVE/CRITIQUE are structurally unable to fire.

2. **Add guided gt_check usage to the SWE-agent system prompt** — the agent used gt_check exactly once in 10 tasks. Adding "After editing a file, run `gt_check <file>` to verify your changes don't break callers" to the instance template would increase delivery.

3. **Test on repos with stronger cross-file coupling** — astropy is heavily intra-file. Django, Flask, or FastAPI repos would exercise cross-file evidence families much more.

4. **The v1.0.4 infrastructure works** — index builds, evidence delivers, hooks fire, budgets enforce, test files filter, freshness checks, incremental reindex. The plumbing is correct. The gap is edge coverage.
