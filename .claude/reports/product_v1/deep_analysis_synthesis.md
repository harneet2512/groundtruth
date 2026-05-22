# Deep Analysis Synthesis — What GT Actually Does vs What It Needs To Do

## The Scoring System is 95% Lexical

v7_4_brief.py scoring formula: `W_LEX=0.50 + W_PATH=0.45 = 0.95` out of ~1.25 total positive weight. Graph signals (W_REACH + W_PROX = 0.10) contribute 8%.

**GT's brief is essentially BM25 + filename matching with a tiny graph bonus.** The graph.db — which we spent significant effort building, filtering, and tuning — contributes almost nothing to file ranking. The research measured anchor signal at 71-73% hit rate, but the brief scoring already achieves similar results through pure BM25 without the graph.

**Implication:** The graph's value is NOT in file ranking. It's in the EVIDENCE delivered after the agent reaches the right file (callers, contracts, signatures). This confirms the research finding ("graph is for evidence, not localization") but also reveals that Patch E (anchor ranking in callers) matters MORE than any brief-level change.

## The Evidence Pipeline Has 10 Drop Points

Full chain from "agent edits file" to "agent sees evidence" has 31 steps and 10 points where evidence can be silently dropped:

1. GATE_MISMATCH (hook output has no recognized markers)
2. Dedup hash match (same file, same normalized content)
3. Curation gate (L3b suppresses after source edit for non-candidate files)
4. Empty evidence after compaction
5. Prepend 600-char cap truncation (L3b — cuts mid-sentence)
6. L3b 500-char cap
7. L3 2000-char cap
8. Brief 500-token cap
9. Stale next-file suppression
10. `__GT_STRUCTURED__` JSON stripping

**BUG: Dual marker lists.** The live router-v2 L3 path (wrapper line 3629) has an inline `_evidence_markers` tuple that is a SUBSET of `L3_MARKERS` from evidence_markers.py. Missing: `Called by:`, `Calls into:`, `Imported by:`, `[GT] `, `[GT_AUTO]`, `[RECALL]`. Evidence with only these markers gets GATE_MISMATCH-suppressed in live mode.

## Sibling Evidence is Source-Position-Ordered, Not Issue-Relevant

`_get_siblings_from_graph()` uses `ORDER BY start_line LIMIT 3` and shows only the FIRST sibling with a snippet (via `for...break`). No issue-term ranking.

**This is why conan failed.** The `build_args` serialization pattern in `install_build_order()` was a sibling of the functions the agent was editing. But if it wasn't in the first 3 by source position, or if another sibling had a snippet first, it was never shown.

**Fix needed:** Rank siblings by issue-anchor overlap, same as callers (Patch E pattern).

## Test Assertions Shown Without Context

`_get_test_assertions_from_file` extracts bare assertion lines truncated to 80 chars. No surrounding lines, no line numbers, no mock setup context.

For briefcase, the mock setup line `mock_remote = MagicMock()` and the assertion `mock_remote.set_url.assert_called_once_with(new_url=...)` are in different parts of the test. GT shows the assertion line alone — the agent doesn't know what `mock_remote` is or how it was configured.

**Fix needed:** When extracting mock assertions, also extract the mock setup line (the line where the mock object is defined or injected as a fixture parameter).

## G7 Silence Gate Interacts With Test Assertions

The silence gate fires AFTER test assertions are collected (Priority 3). If a function has 0 callers + 0 siblings + 0 peers but HAS test assertions (from file scan), the refined gate now keeps typed signatures but still drops the test assertions.

The priority order is: callers → signature → peers → tests → siblings → supplementary. The silence gate runs after ALL priorities are collected but then wipes everything except typed signatures. This means test assertions found by Patch F (mock patterns) can be discovered and then immediately discarded by Patch C.

**Fix needed:** The silence gate should preserve [TEST] evidence alongside [SIGNATURE] for typed functions. Test assertions are behavioral contracts that matter regardless of caller count.

## Confidence Filter Misses in the Main Caller Query

The main `_get_callers_from_graph` function now uses `conf_filter = "AND e.confidence >= 0.7"` (fixed). But the dynamic hop-2 callers at line ~600 still use `AND e.confidence >= 0.5` unconditionally. These hop-2 callers can drag down the aggregate confidence (computed as MIN across all callers), causing the entire caller set to be framed as "unverified" even when the primary callers are high-confidence.

**Fix needed:** Either filter hop-2 callers at 0.7 too, or exclude hop-2 callers from aggregate confidence computation.

## The Router Budget System Doesn't Match Reality

`DEFAULT_TOTAL_BUDGET=8` for views — but L3b only fires 3 times before `budget_exhausted`. The budget is consumed by the view counter in the wrapper, not by the router's own budget system. This means the router's budget tracking is dead weight — the wrapper enforces its own stricter budget.

The router has `delegate_evidence=True` mode where it ONLY gates on budget/debounce/band, not on graph.db content. In this mode, all evidence decisions come from in-container hooks. This is the live runtime mode — the router is essentially a passthrough.

## What the Research Should Have Measured But Didn't

From the 160-bug dataset:

1. **Mock assertion frequency**: How many Python test files use `mock.assert_called_*` vs plain `assert`? BugsInPy's 501 bugs with test_file could answer this. We never measured it.

2. **Sibling relevance**: When the gold function is edited, how often is a relevant sibling in the first 3 by source position? We measured evidence potential (callers + siblings exist) but not sibling RELEVANCE (does the sibling pattern match what the fix should do?).

3. **Test assertion specificity distribution**: Trivial (`assert x is not None`) vs structural (`assert x == expected`) vs behavioral (`mock.assert_called_with(args)`). We categorized these but never measured the distribution.

4. **Evidence drop rate**: How often does evidence get assembled and then dropped before reaching the agent? The 10 drop points suggest significant evidence loss, but we never measured it.

5. **Scoring weight sensitivity**: The brief is 95% lexical. Would changing W_REACH from 0.05 to 0.30 improve file ranking? We never tested alternative weight distributions on our holdout set.

## Priority Fixes From Deep Analysis

| # | Fix | Impact | Lines |
|---|---|---|---|
| 1 | Rank siblings by issue-anchor overlap | Conan-class failures | ~10 lines in _get_siblings_from_graph |
| 2 | Preserve [TEST] in G7 silence gate alongside [SIGNATURE] | Prevents Patch F evidence from being discarded by Patch C | ~3 lines |
| 3 | Fix dual marker list in wrapper live L3 path | Evidence with [GT]/[RECALL]/Called by: markers GATE_MISMATCH-suppressed | ~5 lines in wrapper |
| 4 | Add mock setup context to test assertion extraction | Agent sees assertion + what the mock object is | ~15 lines |
| 5 | Filter hop-2 callers at 0.7 or exclude from aggregate confidence | Prevents "unverified" label on high-confidence caller sets | ~3 lines |
