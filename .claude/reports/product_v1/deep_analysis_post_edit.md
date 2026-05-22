# Deep Analysis: post_edit.py Evidence Generation Pipeline

**File:** `D:\Groundtruth\src\groundtruth\hooks\post_edit.py`
**Lines:** 2822
**Date:** 2026-05-22
**Analyst:** Claude (commissioned by user)

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Section-by-Section Analysis](#2-section-by-section-analysis)
3. [Specific Failure Analysis Questions](#3-specific-failure-analysis-questions)
4. [Bugs and Gaps](#4-bugs-and-gaps)

---

## 1. Architecture Overview

The post-edit hook has **two completely separate evidence paths**:

1. **Improved L3 path** (graph.db-driven, lines 1409-1900): Priority-ordered evidence from graph.db. This is the primary path when graph.db exists.
2. **Legacy fallback path** (lines 2593-2813): Five evidence families (change, contract, pattern, structural, semantic) used when improved L3 produces nothing.

The improved L3 path generates evidence in this priority order:
- P0.5: Behavioral contract (guards + return paths)
- P1: Caller code lines (unseen-first, anchor-boosted, confidence-gated)
- P2: Signature + return type + arity mismatch
- P2b: Interface peers (same method, sibling classes)
- P3: Test assertions (graph.db then file-grep fallback)
- P4: Sibling pattern (same class, different method)
- P5 (supplementary): Structural twins, propagation, co-change, scope
- P6: Issue obligations, mismatch detection, format contracts
- Final: Issue-grounding re-rank, targeted verification suggestion

**Token budget:** 2000 chars (~500 tokens), reduced to 600 chars in late-repair mode (iteration_ratio >= 0.60). Capped at 12 items per function, 3 functions per edit.

---

## 2. Section-by-Section Analysis

### 2.1 Lines 1-100: Imports, Constants, Helpers

#### `_append_gt_log(event, detail)` (L34-43)
- Appends timestamped log lines to `/tmp/gt_hooks.log`.
- No SQL. Returns nothing. Used for observability throughout.

#### `_status_line(kind, detail)` (L46-47)
- Formats `[GT_STATUS] kind:detail` string. Pure formatting.

#### Constants (L54-58)
- `_MAX_EVIDENCE_CHARS = 2000` -- the token budget for evidence output.
- `_BRIEF_CANDIDATES_PATH = "/tmp/gt_brief_candidates.txt"` -- L1 brief file list (not used in improved L3 path after Decision 22 Fix 5 decoupled L3 from L1).
- `_EDITED_FILES_PATH = "/tmp/gt_edited_files.txt"` -- tracks agent's edited files.
- `_ISSUE_TERMS_PATH = "/tmp/gt_issue_terms.txt"` -- issue keywords from wrapper.
- `_ISSUE_ANCHORS_PATH = "/tmp/gt_issue_anchors.json"` -- structured anchors (symbols, paths, test_names).

#### `_load_issue_anchors()` (L61-70)
- Reads `/tmp/gt_issue_anchors.json`. Returns `{symbols, paths, test_names}` dict.
- No SQL. No confidence filtering. Falls back to empty dict on error.

#### `_load_issue_terms()` (L73-80)
- Reads `/tmp/gt_issue_terms.txt`, returns set of lowercase terms.
- No SQL. No confidence filtering.

#### `_compute_caller_relevance(caller, issue_terms)` (L84-90)
- Fraction of issue terms appearing in caller's file path + code.
- Returns 0.5 when no issue terms available (neutral default).
- **No confidence filtering.** Used for relevance ranking, not gating.

#### `_annotate_evidence_header(callers, issue_terms, db_path, file_path)` (L93-154)
- When all callers have 0 keyword overlap with issue terms, queries graph.db for connected files with keyword overlap >= 2.
- **SQL:** `nodes JOIN edges JOIN nodes` with `COALESCE(e.confidence, 0.5) >= 0.7` and `e.type = 'CALLS'`. Returns connected files with keyword matches.
- **Confidence filter:** 0.7 on edges.
- **Uses issue terms:** Yes -- core purpose is keyword overlap.
- **Bug:** The bidirectional join `(e.source_id = n1.id OR e.target_id = n1.id)` with `(n2.id = e.source_id OR n2.id = e.target_id)` produces a self-join: n1 can match as both source and target, and n2 can be n1 itself. The `WHERE n2.file_path NOT LIKE ?` partially mitigates this but doesn't prevent n1 matching on the wrong side.

### 2.2 Lines 157-297: Caller/Usage Extraction Helpers

#### `_extract_usage_contract(callers)` (L157-183)
- Takes first 3 callers, formats their code lines as `CALLERS: file:line code | ...`.
- Truncates code to 90 chars. No SQL. No confidence filtering.
- **Not used by improved L3 path.** Only used in legacy path. Dead in the primary path.

#### `_make_template(line)` / `_detect_structural_twins(file_path, func_start, func_end)` (L195-251)
- Finds lines within a function sharing the same structural pattern (literals replaced with STRING/NUM).
- Requires 2-6 identical templates. Shows top 3 entries of the largest group.
- No SQL (operates on file content). No confidence filtering. No issue terms.
- **Potential gap:** Only reads actual file at `file_path`, doesn't read from the diff. If the file was just edited, it reads the new version, which is correct for "show the pattern" but means it can't detect if the edit broke a pattern.

#### `_detect_edit_propagation(db_path, file_path, func_name, repo_root)` (L254-296)
- **SQL:** `nodes JOIN edges (target_id, type='CALLS', confidence >= 0.7) JOIN nodes (source)` where `nsrc.file_path != nt.file_path AND nsrc.is_test = 0 AND e.source_line > 0`.
- **Confidence filter:** 0.7 hard-coded.
- Returns `[PROPAGATE] N call sites may need updating: file1:line1, file2:line2`.
- **No issue terms used.**

#### `_classify_file_kind(file_path)` (L299-307)
- Pure string matching: test/config/source classification.

#### `_co_change_reminder(file_path, repo_root, edited_files)` (L310-389)
- Runs `git log --name-only -30` to find files that historically co-change.
- Uses `COCHANGE_HIGH_THRESHOLD=5`, `COCHANGE_MEDIUM_THRESHOLD=3` from signal_thresholds.py.
- Filters out already-edited files and doc extensions (.md, .rst, .txt, .lock).
- **No SQL.** Git-based only. No confidence filtering on edges.
- **Uses issue terms:** No.

#### `_scope_completeness(edited_files, file_path, repo_root)` (L392-430)
- Runs `git log --name-only -30 -- file_path`, computes average files per commit.
- Warns if avg > 1.5 and agent has only edited 1 file.
- **No SQL.** No issue terms.

### 2.3 Lines 433-663: Core Query Functions

#### `_read_lines_file(path)` (L433-438)
- Reads file with one path per line. Used to load edited_files.

#### `_read_source_line(full_path, line_no, extra_lines, end_line)` (L442-467)
- Reads a source line + up to `extra_lines` continuation lines.
- Stops at blank lines, lower indentation, or new function definitions.
- Joins with ` | ` separator. This is how caller code snippets are captured.
- **Gap:** The ` | ` join makes multi-line code readable but destroys formatting. When the agent sees `code1 | code2 | code3`, the actual structure (indentation, blocks) is lost.

#### `_read_source_lines(full_path, start, end)` (L471-483)
- Reads lines [start, end] inclusive, preserving newlines. Used for sibling/peer snippets.

#### `_get_callers_from_graph(db_path, file_path, function_name, repo_root, seen_files, limit=5)` (L488-662)
- **The core caller query.** This is the most important function in the file.
- **Primary SQL:** `nodes(target) JOIN edges(CALLS, confidence >= 0.7) JOIN nodes(source)` where `nsrc.file_path != nt.file_path`. Orders by confidence DESC, source_line.
- **Confidence filter:** Primary = 0.7. Fallback = 0.5 (when 0.7 returns empty).
- **Seen-file tracking:** Marks callers from already-visited files as `unseen=0`. Does NOT filter them out -- just annotates for later sorting.
- **Dynamic hops (L594-648):** When only 1 caller exists, checks if it's a thin wrapper (<3 callers itself). If so, appends the wrapper's callers with `[via wrapper]` prefix. These get confidence 0.5 regardless of actual edge confidence.
- **Issue-term ranking (L652-657):** After fetching, sorts callers by issue keyword overlap (descending). This is the only quality filter beyond confidence.
- **Bugs/gaps:**
  - The `LIKE '%path'` match on file_path is fragile. If two files share a suffix (e.g., `utils/config.py` and `src/config.py`), both match.
  - The `limit + 10` in the SQL LIMIT clause fetches extra to account for seen-file filtering, but since seen files are NOT filtered (just annotated), the extra 10 are always returned and then clipped to `limit` in the loop. The extra fetch is unnecessary.
  - Dynamic hops hard-code confidence at 0.5 for all hop-2 callers, even when the actual edges have higher confidence.

### 2.4 Lines 665-952: Signature, Siblings, Interface Peers

#### `_get_signature_from_graph(db_path, file_path, function_name)` (L665-688)
- **SQL:** `SELECT signature, return_type FROM nodes WHERE file_path LIKE ? AND name = ? AND label IN ('Function', 'Method') LIMIT 1`.
- **No confidence filter** (reads nodes table, not edges).
- Returns formatted signature string like `def foo(a, b) -> int`.

#### `_get_siblings_from_graph(db_path, file_path, function_name, repo_root)` (L691-758)
- Finds sibling methods: same parent_id (class), or same file (for top-level functions).
- **SQL (with parent):** `SELECT ... FROM nodes WHERE parent_id = ? AND id != ? AND label IN ('Function', 'Method') ORDER BY start_line LIMIT 3`.
- **SQL (no parent):** `SELECT ... FROM nodes WHERE file_path LIKE ? AND id != ? AND label IN ('Function', 'Method') AND (parent_id IS NULL OR parent_id = 0) ORDER BY start_line LIMIT 3`.
- **No confidence filter** (no edges involved).
- Reads 12 lines of sibling body (skipping def line).
- **Critical gap for Conan question:** `ORDER BY start_line LIMIT 3` returns the first 3 siblings by source position. If the function being edited is at line 500 and `install_build_order()` (which has the `build_args` pattern) is at line 200, it will only be returned if there are fewer than 3 siblings before it. If the class has many methods, the sibling that happens to be relevant to the issue may not appear in the top 3.

#### `_get_interface_peers_from_graph(db_path, file_path, function_name, repo_root, edited_files)` (L761-892)
- Finds same-method implementations across sibling classes sharing a base/interface.
- **SQL chain:**
  1. Find method node -> get parent_id (class_id).
  2. Find EXTENDS/IMPLEMENTS edges from that class (confidence >= 0.5).
  3. Find other classes extending the same parent.
  4. Find same-named method in peer classes.
- Falls back to `_get_name_match_peers` if no inheritance edges.
- Reads 12 lines of peer body.
- Prioritizes already-edited files.
- **No issue term usage.**

#### `_get_name_match_peers(db_path, file_path, function_name, repo_root, edited)` (L895-952)
- Fallback for interface peers: same-named method in same directory.
- **SQL:** `SELECT ... FROM nodes WHERE file_path LIKE '%parent_dir/%' AND name = ? AND label IN ('Function', 'Method') AND file_path NOT LIKE '%norm_path'`.
- Skips dunder methods and common names (setUp, tearDown, main, run).
- **No confidence filter** (no edges involved).

### 2.5 Lines 955-1253: Test Assertions, Signature Analysis, Verification

#### `_get_test_assertions_from_graph(db_path, file_path, function_name)` (L955-997)
- Queries `assertions` table (if it exists) joined with nodes.
- **SQL:** `assertions JOIN nodes (test_node_id) JOIN nodes (target_node_id)` where target matches edited function.
- Returns structured assertion data: kind, expression, expected, test_name, test_file.
- **No confidence filter** (assertions table has no confidence column).
- **In practice:** The `assertions` table is rarely present in graph.db (it requires specific indexer support). This path almost never fires.

#### `_get_test_assertions_from_file(db_path, file_path, function_name, repo_root)` (L1000-1081)
- **The real test assertion path.** Three-stage fallback:
  1. Find test files connected via graph edges (any edge, no confidence filter!), grep for `assert` lines mentioning the function name.
  2. If no assertions found, grep for `assert` lines mentioning issue terms.
  3. If still nothing, use issue anchors' `test_names` to find assertions within named test functions.
- **SQL for test file discovery:** `nodes JOIN edges(target_id) JOIN nodes(source, is_test=1)` -- **NO confidence filter on edges**.
- **What it shows:** Just the assertion line, truncated to 80 chars. No surrounding context lines.
- **This directly answers Q4:** Assertions are shown as single lines only. Format: `test_file: assert_line[:80]`. No context window.

#### `_find_nearest_candidate(file_path, brief_candidates, db_path)` (L1084-1118)
- Finds nearest L1 brief candidate connected to the edited file.
- Uses `COALESCE(e.confidence, 0.5) >= 0.7`.
- Falls back to first candidate if no graph connection found.
- **Not used in the improved L3 path** after Decision 22 Fix 5.

#### `_signature_param_count`, `_signature_has_varargs`, `_signature_default_count` (L1121-1154)
- Pure string parsing of signature strings. No SQL.

#### `_extract_call_arity(code, function_name)` (L1157-1181)
- Approximates how many args a caller passes to function_name.
- Parenthesis-depth tracking. No SQL.
- **Bug:** Doesn't handle kwargs (`foo(a=1, b=2)` counts as 2 args, but if the signature changed to require a positional arg, kwarg callers may still work).

#### `_check_arity_mismatch(new_signature, func_name, callers, edited_files)` (L1184-1253)
- Compares new signature arity against caller call arity.
- Skips callers the agent already edited, and functions with *args/**kwargs.
- Uses `SIGNATURE_HIGH_CONFIDENCE_METHODS` and `SIGNATURE_MEDIUM_CONFIDENCE_METHODS` from signal_thresholds.py.
- **Bug:** Reads `c.get("resolution_method", "")` but the caller dict never contains `resolution_method` -- it contains `confidence` as a string. The resolution_method lookup always falls through to the else branch (`confidence = "medium"`). This means the high-confidence path is dead code.

### 2.6 Lines 1256-1900: generate_improved_evidence (Main Assembly)

This is the heart of the evidence pipeline. Called once per file edit with up to 3 function names.

#### Entry and State Loading (L1409-1465)
- Loads `edited_files` from `/tmp/gt_edited_files.txt`.
- Decay: 3 callers for first 3 edits, 2 callers after.
- `GT_REBUILD_L3` env var controls feature-flagged mode support (experimental, not used in production).
- Late-repair mode: `iteration_ratio >= 0.60` reduces evidence to signature + top 1 caller, capped at 600 chars.

#### Priority 0.5: Behavioral Contract (L1506-1575)
- Queries graph.db for function start/end lines using a **generalized path suffix resolver** (P0-1 change: queries by name only, then matches path components in Python).
- Reads the function body from the actual file.
- Calls `_regex_extract_guards()` from `groundtruth.evidence.change` to find guard clauses (if-raise, if-return patterns).
- Extracts return paths (lines starting with `return`).
- Emits `[BEHAVIORAL CONTRACT]` block with GUARD and return-path lines.
- **Fires when:** body > 20 chars AND (guards >= 1 OR return_paths >= 2).
- **No confidence filtering** (reads nodes for line positions, not edges).
- **No issue terms used** for guard extraction -- it's purely structural.

#### Priority 1: Caller CODE Lines (L1577-1626)
- Calls `_get_callers_from_graph()` (confidence >= 0.7, fallback 0.5).
- Separates unseen vs seen callers, puts unseen first.
- **Patch E anchor boost:** Re-ranks callers by issue anchor overlap (symbols + paths). Callers matching issue anchors get score boost of +2 (symbol match) or +1 (path match).
- **Aggregate confidence:** Uses MIN confidence across all callers (conservative).
- Calls `format_risk_evidence()` to format callers.

#### `format_risk_evidence(callers, function_name, confidence)` (L1361-1406)
- **This directly answers Q2:**
  - confidence >= 0.9 AND >= 3 callers: `[CONTRACT] N callers depend on func()` + top 2 caller code lines.
  - confidence >= 0.9 AND 1-2 callers: `[CONTRACT] callers of func():` + top 2 caller code lines.
  - confidence >= 0.5 AND < 0.9: `[CONTRACT ~] possible callers (unverified):` + top 2 caller code lines.
  - confidence < 0.5 OR no callers: Returns empty list (silence).
- **Always shows top 2 callers.** No quality filter beyond the aggregate confidence tier. The ranking already happened in `_get_callers_from_graph()` (issue-term sorted) and the anchor-boost re-rank.

#### Priority 2: Signature + Arity Mismatch (L1628-1658)
- Appends `[SIGNATURE] sig` line. Annotates with caller count if confidence >= 0.9.
- Runs `_check_arity_mismatch()` for diff-aware arity warnings.

#### Priority 2b: Interface Peers (L1678-1706)
- Skips dunder methods.
- Shows top 2 peers with `[PEER] basename::func(): snippet[:300]`.

#### Priority 3: Test Assertions (L1708-1732)
- Tries graph.db assertions table first, falls back to file grep.
- Shows `[TEST] test_name expects: expression == expected`.

#### Priority 4: Sibling Pattern (L1734-1753)
- Shows first sibling with snippet: `[PATTERN] sibling name() does: snippet[:300]`.
- Falls back to signature if no snippet.
- **Only shows first sibling that has a snippet.** If the first sibling in `ORDER BY start_line` has no snippet but a later one does, it skips to the signature-only display. The `for...else` construct (L1737-1744) means: iterate siblings, break on first with snippet; if none had snippet, show first's signature.

#### G7 Silence Gate (L1755-1772)
- If a function has 0 callers, 0 siblings, 0 peers: structurally isolated.
- For typed signatures (contains `->` or `:`), keeps only `[SIGNATURE]` lines.
- For untyped: suppresses ALL evidence.
- **This is aggressive.** A function could have test assertions but no callers/siblings/peers. The G7 gate would suppress the test assertion evidence.

#### Priority 5: Supplementary (L1774-1808)
- Structural twins, propagation, co-change, scope -- only if < 10 items already.
- Gate raised from 7 to 10 to allow supplementary signals even when primary is rich.

#### Priority 6: Issue Obligations + Mismatch + Format Contracts (L1811-1838)
- `issue_obligations.load_and_check(diff_text)`: Extracts "remove parameter" obligations from issue text, checks if diff actually removed them.
- `mismatch.detect_stale_references(db_path, file_path, func_name, diff_text, repo_root)`: Finds test/caller references to identifiers removed by the diff.
- `format_contract.mine_return_shape(db_path, file_path, func_name, repo_root)`: Mines expected dict keys/attributes from callers.
- All three are **inserted at position 0** (front of evidence), so they appear before callers/signature.
- **Obligation warnings use diff_text. Mismatch uses diff_text. Format uses graph edges with confidence >= 0.5.**

#### Issue-Grounding Re-rank (L1841-1853)
- Scores each evidence line against issue anchors.
- Sorts by score descending (issue-relevant first).
- **This happens AFTER obligations are inserted at position 0, so obligations can be re-ranked below callers if they don't match issue terms.**

#### Cap and Wrap (L1855-1900)
- Cap at 12 items per function.
- Truncate to `_MAX_EVIDENCE_CHARS` (2000 or 600 in late-repair).
- Wrap in `<gt-evidence trigger="post_edit:filepath">` XML tags.
- Appends targeted verification suggestion (`[GT_VERIFY]`) if space remains.

### 2.7 Lines 1900-2822: main(), Argument Parsing, Entry Point

#### `_git_env()` (L1903-1911)
- Sets `safe.directory = *` for containerized git operations.

#### `_detect_workspace_root(provided_root)` (L1914-1948)
- Three-step: git rev-parse, /workspace/*/ scan, fallback.

#### `_is_view_operation()` (L1951-1968)
- Checks `TOOL_INPUT` / `OPENHANDS_TOOL_INPUT` env var for `{"command": "view"}`.
- Skips all processing for view-only operations.

#### `main()` (L2411-2813)
1. Parse args: `--root`, `--db`, `--file`, `--quiet`, `--max-items`, `--diff`, `--old-content`, `--mode`, `--iteration-ratio`, `--structured-output`.
2. Skip view operations.
3. Detect workspace root.
4. Get modified files from `git diff --name-only`.
5. Merge with explicit `--file` arg.
6. Read or reconstruct old content (for diff-aware analysis).
7. If no modified files: exit with "no_modified_files" status.
8. Open GraphStore for language-agnostic evidence.
9. Parse diff for changed line ranges per file.
10. Find changed function names using graph.db node positions, Python AST, or regex fallback.
11. **The _has_edges gate (L2548-2560):**
    - Queries `edges JOIN nodes WHERE n.file_path LIKE ?` to check if the edited file has ANY edges.
    - If `_has_edges OR all_func_names`: calls `generate_improved_evidence()`.
    - **This means the gate is effectively always open when functions are detected.** The `all_func_names` truthiness check bypasses the edge check entirely. The `_has_edges` query is redundant.
12. If improved evidence succeeds: print it and return (skip legacy).
13. Legacy fallback: 5 evidence families (change, contract, pattern, structural, semantic).
14. Apply abstention (`_apply_abstention`): min confidence 0.40, skip private methods.
15. Sort by confidence descending, take top N.

---

## 3. Specific Failure Analysis Questions

### Q1: Why did conan's [PATTERN] sibling evidence NOT show `build_args` serialization in `install_build_order()`?

**Root cause: `ORDER BY start_line LIMIT 3` + "first with snippet" selection.**

`_get_siblings_from_graph()` (L691-758) returns siblings ordered by `start_line` with `LIMIT 3`. It only returns the first 3 sibling methods in source order. If the class containing `install_build_order()` has many methods, the method with the `build_args` serialization pattern may be sibling #4, #5, or later and is never returned.

Then in `generate_improved_evidence()` at L1737-1744, the code does:
```python
for sib in siblings:
    if sib["snippet"]:
        func_parts.append(f"[PATTERN] sibling {sib['name']}() does:\n{sib['snippet'][:300]}")
        break  # <-- only shows FIRST sibling with snippet
```
Even if `install_build_order()` were in the top 3, it would only be shown if it's the first sibling with a non-empty snippet.

Additionally, there is **no issue-term filtering or ranking** on siblings. Unlike callers (which are ranked by issue keyword overlap), siblings are always returned in source-position order. A sibling doing `build_args` serialization would have high issue relevance but no mechanism exists to surface it preferentially.

**Fix needed:** (a) Rank siblings by issue-term overlap instead of source position. (b) Increase the limit or use a smarter selection that considers relevance. (c) Show multiple sibling snippets when issue terms match.

### Q2: How does format_risk_evidence() decide what caller evidence to show?

**It shows the first 2 callers from an already-ranked list. No additional quality filter.**

The pipeline is:
1. `_get_callers_from_graph()` fetches callers with confidence >= 0.7 (fallback 0.5).
2. Separates into unseen (not in edited_files) and seen, puts unseen first.
3. **Patch E anchor boost:** Re-ranks by issue anchor overlap (symbols +2, paths +2/+1).
4. `format_risk_evidence()` receives the ranked list and always shows `callers[:2]`.

The aggregate confidence (MIN across all callers) determines the framing:
- >= 0.9: `[CONTRACT] N callers depend on func()`
- >= 0.5: `[CONTRACT ~] possible callers (unverified):`
- < 0.5: silence

There is no per-caller quality filter in `format_risk_evidence()` itself. The quality gating happened upstream in the SQL (confidence >= 0.7) and the ranking (issue anchors).

### Q3: What is GATE_MISMATCH? Where does it come from?

`GATE_MISMATCH` is defined in `src/groundtruth/hooks/trace_fields.py` as a `SuppressionReason` enum value (line 30). It is part of the structured tracing system (`TraceEvent`).

**It is NOT used anywhere in post_edit.py.** It is a trace/diagnostic enum value available for other hooks/mechanisms to use when logging why a mechanism was suppressed. The name suggests it was designed for cases where a mechanism's gating condition (e.g., "file must have edges") doesn't match the current state. But in the current codebase, only `trace_fields.py` defines it -- no hook code actually sets `suppression_reason = GATE_MISMATCH`.

It is **not in the wrapper** either. It lives exclusively in the trace_fields.py enum and is currently dead infrastructure.

### Q4: When _get_test_assertions_from_file finds assertions, does it show context?

**No. It shows assertion lines only, truncated to 80 characters, with no surrounding context.**

The relevant code at L1035:
```python
assertions.append(f"{test_file}: {stripped[:80]}")
```

This shows: `test_file_path: assert_expression_up_to_80_chars`.

There is no context window, no surrounding lines, no line numbers from the test file. The assertion is shown completely out of context. The agent sees something like:
```
[TEST] tests/test_users.py: assertEqual(result.status_code, 200)
```

But NOT:
```
[TEST] tests/test_users.py:42-45:
    response = client.get("/users/1")
    assertEqual(result.status_code, 200)
    assertEqual(result.json()["name"], "Alice")
```

This is a significant limitation. The assertion alone often doesn't tell the agent what setup produced the expected value. The agent cannot see what arguments were passed or what state was constructed.

---

## 4. Bugs and Gaps

### Bug 1: Dead `resolution_method` lookup in `_check_arity_mismatch()`
**Line 1223:** `res_method = c.get("resolution_method", "")` but caller dicts from `_get_callers_from_graph()` contain `confidence` (as string), not `resolution_method`. The high-confidence path (`SIGNATURE_HIGH_CONFIDENCE_METHODS`) is dead code. Everything falls to `confidence = "medium"`.

### Bug 2: G7 silence gate suppresses test assertions
**Lines 1759-1772:** When a function has 0 callers, 0 siblings, 0 peers, ALL evidence is suppressed (or only signature kept if typed). But test assertions (Priority 3) may have been collected. The gate wipes them. This means framework lifecycle methods (`__init__`, callback handlers) with test coverage but no static callers lose their test evidence.

### Bug 3: Issue-grounding re-rank can demote obligation warnings
**Lines 1816-1817:** Obligation warnings are `insert(0, ow)` -- put first. But lines 1847-1849 re-rank ALL func_parts by issue anchor score. If the obligation warning doesn't match issue anchors well (which is likely since it's a meta-warning like "[OBLIGATION] You should remove parameter X"), it gets ranked below caller code lines that happen to mention issue keywords.

### Bug 4: Dynamic hop confidence is always 0.5
**Line 645:** Hop-2 callers from the thin-wrapper pattern always get `"confidence": "0.5"` regardless of the actual edge confidence. This artificially lowers the aggregate confidence (which uses MIN), potentially downgrading the framing from `[CONTRACT]` to `[CONTRACT ~]`.

### Bug 5: `_get_test_assertions_from_file` uses NO confidence filter on edges
**Lines 1010-1017:** The SQL for finding test files connected to the edited file has NO confidence filter on edges. It uses bare `edges JOIN nodes` without any confidence check. This means test files connected only by name_match with 0.2 confidence are treated the same as import-verified connections.

### Bug 6: Sibling selection ignores issue relevance
As detailed in Q1, siblings are selected by `ORDER BY start_line LIMIT 3` with no issue-term ranking. This is the direct cause of the conan build_args failure.

### Gap 1: No confidence on behavioral contract
The behavioral contract (Priority 0.5) is purely structural -- it finds guard clauses and return paths in the function body. It doesn't query edges, so there's no confidence gating. This means it fires on every function with guards or multiple returns, including functions the agent wrote from scratch in the current edit (which is waste -- the agent already knows what it wrote).

### Gap 2: Late-repair mode too aggressive
When `iteration_ratio >= 0.60`, evidence is cut to signature + 1 caller within 600 chars. This removes behavioral contracts, test assertions, peers, siblings, obligations, and mismatches. Late-stage edits arguably need MORE verification evidence (the agent is probably struggling), not less.

### Gap 3: `_BRIEF_CANDIDATES_PATH` is loaded but unused in improved L3
After Decision 22 Fix 5 decoupled L3 from L1, `_BRIEF_CANDIDATES_PATH` is defined but never read in the improved L3 path. It's only used in `_find_nearest_candidate()` which is itself not called from the improved path. Dead code.

### Gap 4: Structural twins detection reads the NEW file, not OLD
`_detect_structural_twins()` reads the current file content. If the agent just broke a structural pattern (e.g., added a constructor call with wrong arity), the twins detector sees the broken version and may detect it -- but also may not, since the broken line might not match the template. It would be more useful to compare old vs new twin groups.
