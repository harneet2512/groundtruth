# Deep Analysis: V1R Brief + Router + Evidence Markers + V7.4 Scorer

Date: 2026-05-22
Branch: jedi__branch
Files analyzed:
- `src/groundtruth/pretask/v1r_brief.py` (L1 brief generation)
- `src/groundtruth/router/router.py` (CollaborationRouter, Layer 3)
- `src/groundtruth/config/evidence_markers.py` (marker contract)
- `src/groundtruth/pretask/v7_4_brief.py` (hybrid scorer)
- `src/groundtruth/router/decisions.py` (emission/suppression enums)
- `src/groundtruth/hooks/trace_fields.py` (GATE_MISMATCH definition)
- `src/groundtruth/config/signal_thresholds.py` (threshold constants)

---

## 1. V1R Brief (`v1r_brief.py`) -- L1 Brief Generation Engine

### 1.1 File Ranking Pipeline

The V1R brief is a two-stage system:

**Stage 1: Candidate generation + scoring (delegated to v7.4)**
- Calls `run_v74()` with ablation="C" (full hybrid, no commit prior)
- Parameters: `k_anchor=3`, `k_sem_top=10`, `tau_anchor=0.20`, `max_depth=3`, `min_confidence=0.7`
- Receives `V74BriefResult.ranked_full` -- a list of all scored candidates

**Stage 2: V1R-specific post-processing**
1. **Adaptive K**: Includes candidates while score gap is small. Walks positions 1..8, breaks when gap between position i and i+1 exceeds 2x the median gap. Minimum recall guard: always returns at least min(5, total_candidates).
2. **Non-source filtering**: Removes CHANGELOG.md, README.md, setup.py, .rst/.md/.txt/.csv/.json/.yaml/.yml/.toml/.cfg/.ini files.
3. **Path-match preservation**: Rescues files with path component score >= 0.5 that didn't make top-K. Replaces the lowest-scored entry (up to 2 rescues).
4. **Graph neighbor expansion**: For top-3 files, queries 1-hop callers AND callees at confidence >= 0.7. Adds up to 3 neighbors not already in the list. Neighbor score = parent score * 0.8.
5. **Cross-domain bridging** (Decision 26): If `_detect_overconfident_convergence` fires (all top-5 in <=2 dirs AND BM25 dominates all top-5), expands via git co-change and test co-import from other modules.
6. **Hub demotion**: Reorders so peripheral files appear before hubs. Uses p80 in-degree as threshold. Never suppresses -- only reorders.
7. **Token truncation**: Trims entries from the end until brief fits within MAX_BRIEF_TOKENS=600 (~150 words).

### 1.2 Scoring Formula

Delegated entirely to v7.4. V1R itself does no scoring -- it post-processes v7.4's ranked list.

### 1.3 Function Selection Per File

Two parallel function selection mechanisms:

**`_top_functions()`** (for display in brief):
- Queries `nodes` table for Functions/Methods in file, non-test
- LEFT JOINs to `edges` where confidence >= EDGE_CONFIDENCE_FLOOR (0.7)
- Orders by reference count DESC, then name
- Returns signatures if available, else names
- Limit: MAX_FUNCTIONS_PER_FILE = 3

**`_top_function_names()`** (for contract/spec lookup):
- Same query but returns raw names, not signatures
- Fetches top 20, then prioritizes functions whose names appear in issue_terms
- Issue-matched functions sort before reference-count-ranked ones

### 1.4 Anchors

V1R itself does NOT use anchors directly. It delegates to v7.4, which calls `select_anchors()` from `anchor_select.py`. The anchor system identifies trusted files for graph BFS expansion. V1R passes `k_anchor=3, tau_anchor=0.20`.

### 1.5 EDGE_CONFIDENCE_FLOOR Usage

`EDGE_CONFIDENCE_FLOOR = 0.7` is used in 7 places within v1r_brief.py:

1. `_top_functions()` -- only counts references with confidence >= 0.7
2. `_top_function_names()` -- same filter
3. `_test_files_for()` -- only links tests via edges with confidence >= 0.7
4. `_issue_relevant_neighbors()` -- only follows edges with confidence >= 0.7
5. `_static_callees()` -- only follows CALLS edges with confidence >= 0.7
6. `generate_v1r_brief()` neighbor expansion -- uses EDGE_CONFIDENCE_FLOOR for 1-hop query
7. Passed as `min_confidence` to `run_v74()` for graph BFS

Separate: `CALLER_CONFIDENCE_FLOOR = 0.9` for `_caller_contract_for_file()` (only shows callers at very high confidence).

All confidence checks use `_has_confidence()` which queries `PRAGMA table_info(edges)` to detect if the column exists (backward compat with old graph.db schemas).

### 1.6 Callers/Callees Selection

**Callers** (`_caller_contract_for_file()`):
- For top 2 functions by name, queries cross-file CALLS edges at confidence >= 0.9
- Reads the actual source line from disk where the call happens
- Returns up to 3 caller code lines formatted as `file:line \`code\``
- Stops early if 3 caller lines found for the first function

**Callees/Neighbors** (`_issue_relevant_neighbors()`):
- Queries BOTH callers and callees of the file (bidirectional 1-hop)
- Ranks neighbors by issue keyword overlap: reads up to 200KB of each neighbor file, counts how many issue terms appear
- Falls back to `_static_callees()` (outgoing CALLS edges) if no issue terms

**Sibling context** (`_sibling_context()`):
- Same-file functions/methods, non-test, excluding the top functions themselves
- Returns up to 5 names -- shows what OTHER functions exist at the same scope level

### 1.7 Additional Per-File Evidence

- **Spec** (`_function_spec()`): Reads the function body, uses `_make_template()` to normalize literals, finds repeated structural patterns (2-8 occurrences of same template). Shows "handles: case1 | case2 | case3" -- the parallel patterns within a function. Relevance-gated against issue text.
- **Co-changes** (`_co_change_files()`): git log last 20 commits, finds files that co-changed >= 2 times with this file. Excludes .md/.rst/.txt/.yml/.yaml.
- **Last change** (`_last_change()`): git log --oneline -1 for the file.
- **Cross-file scope** (Signal 1): Uses thresholds from `signal_thresholds.py`. High confidence requires >= 2 distinct caller files via same_file/import resolution at confidence >= 0.9.

### 1.8 Brief Rendering

`render_brief()` outputs XML-tagged text:
```
<gt-task-brief>
1. path/to/file.py (func1, func2, func3)
   Spec: handles: case1 | case2
   Callers: other.py:42 `some_call(arg)`
   Context: sibling1, sibling2 | Last: abc1234 fix something
   Also changes: related.py, other.py
   Calls: neighbor1.py, neighbor2.py
   Tests: test_file.py
...
Edit path/to/file.py first. Verify: pytest test_file.py
</gt-task-brief>
```

Confidence-gated directive: only says "Edit X first" when top candidate is 30%+ ahead of #2.

### 1.9 Sparse Graph Fallback

If `edges_per_file < 2.0`, switches to BM25-only weights:
`W_SEM=0.0, W_LEX=0.70, W_REACH=0.0, W_PROX=0.0, W_HUB=0.0, W_COMMIT=0.0, W_PATH=0.45`

---

## 2. Router (`router/router.py`) -- CollaborationRouter (Layer 3)

### 2.1 What is the Router?

The `CollaborationRouter` is Layer 3 of FINAL_ARCH_V2. It decides WHEN to emit evidence to the agent during an active coding session (post-brief). It has two entry points:
- `on_view(observed_file)` -- triggered when agent reads a file
- `on_edit(edit_target, function_names)` -- triggered when agent edits a file

The router is NOT the brief generator. It's the post-brief, in-session evidence gating layer.

### 2.2 Suppression Reasons (from decisions.py)

```
DUPLICATE    -- already shown for this target
STALE        -- would point at already-viewed file
TOO_LATE     -- late iteration band, broad nav suppressed
NO_NEW_EDGE  -- only already-known edges
BUDGET       -- per-task injection cap reached
LOW_CONFIDENCE -- below threshold
NO_EVIDENCE  -- graph present but empty for target
NO_GRAPH_DB  -- graph.db missing
DEBOUNCE     -- same-kind emission within debounce window
NOT_APPLICABLE -- rule didn't trigger
DISABLED     -- injection disabled
```

### 2.3 Emit vs Suppress Decision Logic

**on_view():**
1. Empty path -> suppress NO_EVIDENCE
2. No graph.db -> suppress NO_GRAPH_DB
3. File already briefed (`view::canon` in dedup set) -> suppress DUPLICATE
4. Total budget reached (default 8) -> suppress BUDGET
5. Late band (iteration >= 75% of max) -> suppress TOO_LATE
6. Same-kind emission within debounce window -> suppress DEBOUNCE
7. **Delegate mode**: If `delegate_evidence=True`, skip graph queries. Just pass through to in-container hook.
8. Query providers: callers, callees, importers for the file
9. All empty -> suppress NO_EVIDENCE
10. Filter out already-visited neighbors
11. All neighbors visited -> suppress STALE
12. Rank remaining by issue relevance
13. Dedup against (target_file, primary_edge_file) pair
14. Emit with evidence text

**on_edit():**
1. Empty path -> suppress NO_EVIDENCE
2. No graph.db -> suppress NO_GRAPH_DB
3. File already got EDIT evidence -> suppress DUPLICATE
4. **Edits BYPASS total budget** -- always fire (critical moment)
5. Debounce check
6. Delegate mode: pass through
7. No function names -> suppress NO_EVIDENCE
8. Query providers in priority order: caller_code > sibling > contract > test
9. Issue-relevance re-sort callers
10. No items at all -> suppress NO_EVIDENCE
11. No actionable evidence types -> suppress LOW_CONFIDENCE
12. Add edit propagation hints
13. Emit

### 2.4 Budget System

- `DEFAULT_TOTAL_BUDGET = 8` -- safety ceiling on total emissions
- **Views are budget-capped. Edits are NOT.** This is a deliberate design choice: edits are the critical moment where contract/caller evidence matters most.
- First-per-file gating: each file gets at most one view emission and one edit emission
- Debounce: configurable `debounce_iters` (default 0 = no debounce)
- Late band: `late_band_ratio = 0.75` -- after 75% of iterations, broad navigation suppressed
- `AMBIGUITY_MARGIN = 0.12` -- defined but not used in current code (placeholder for soft vs hard emission)

### 2.5 Delegate Mode

When `delegate_evidence=True`, the router only gates on budget/debounce/band. It does NOT query graph.db at all. Evidence comes from an in-container hook. The wrapper checks evidence markers after the hook runs and only injects if real evidence was produced. This is the mode used in SWE-bench evaluation.

### 2.6 What is GATE_MISMATCH?

`GATE_MISMATCH` is a `SuppressionReason` in `trace_fields.py` (the hook-level trace system, distinct from the router's own `decisions.py` SuppressionReason enum). It represents a situation where the gate/condition check at the hook level doesn't match the expected emission criteria. It's defined as a trace event classification for diagnostic purposes. It is NOT used in the router's decision logic -- it's in the hooks tracing layer.

---

## 3. Evidence Markers (`config/evidence_markers.py`)

### 3.1 Recognized Markers

**L3B_MARKERS** (post-view navigation + structural signals):
```
"Called by:", "Calls into:", "Imported by:", "Next:",
"[GT] ", "[CONTRACT]", "[CONTRACT ~]", "[PEER]", "[PATTERN]",
"[SIGNATURE]", "[TEST]", "[GT_VERIFY",
"[PROPAGATE]", "[CO-CHANGE]", "[SCOPE]",
"[BEHAVIORAL CONTRACT]", "[RECALL]",
"[GT_AUTO]", "[MISMATCH]", "[FORMAT]", "[GT_CONTRACT",
```

**L3_MARKERS** (post-edit evidence, superset of L3b):
All of L3b plus:
```
"[GT_CHANGE]", "[GT_CONTRACT]", "[GT_PATTERN]",
"[GT_STRUCTURAL]", "[GT_SEMANTIC]", "[GT_COUPLING]",
"[GT L3:", "[TWINS]",
"GUARD_ADDED:", "GUARD_REMOVED:",
"SIGNATURE:", "SIBLING:", "CALLERS:", "WARNING:",
"TOP CALLER:", "MUST PRESERVE:", "TEST EXPECTS:", "TEST:",
```

**RESCUE_MARKERS**: `("[GT]",)` -- minimal rescue payload marker.

### 3.2 Delivery Gate (`has_gt_evidence()`)

Simple substring check: `any(m in text for m in markers)`. Takes a `layer` parameter ("l3b", "l3", or "rescue") to select which marker set.

### 3.3 Where Used

Only imported in test files (`test_oh_gt_full_wrapper.py`, `test_evidence_markers.py`, `test_delivery_invariant.py`). The wrapper uses these markers to decide if hook output contains real evidence before injecting it into agent context.

---

## 4. V7.4 Brief (`pretask/v7_4_brief.py`) -- Hybrid Scorer

### 4.1 Scoring Formula

```
total_score = W_SEM * sem
            + W_LEX * lex
            + W_REACH * reach * max(0, 1 - hub_pen)
            + W_PROX * anchor_prox
            + W_COMMIT * commit
            + W_PATH * path
            - min(W_HUB_MAX, W_HUB) * hub_pen   [only if evidence_pre_hub < W_HUB]
```

**Default weights:**
| Weight | Value | Signal |
|--------|-------|--------|
| W_SEM | 0.15 | Dense cosine (sentence-transformer all-MiniLM-L6-v2) |
| W_LEX | 0.50 | Normalized BM25 score |
| W_REACH | 0.05 | Graph BFS reachability from trusted anchors |
| W_PROX | 0.05 | Proximity to trusted anchors in call graph |
| W_HUB | 0.10 | Hub penalty: tanh(in_degree / HUB_SCALE) |
| W_COMMIT | 0.00 | Commit prior (disabled in ablation C) |
| W_PATH | 0.45 | Path/filename match to issue terms |

**Observation:** W_LEX (0.50) + W_PATH (0.45) = 0.95 out of the total possible ~1.3 non-penalty score. BM25 and filename match dominate. Graph signals (W_REACH + W_PROX = 0.10) are marginal. Semantic embedding (W_SEM = 0.15) is modest. The scorer is overwhelmingly a lexical + filename system.

### 4.2 BM25 + Reach + Hub Penalty Combination

- BM25: Computed by `lexical_file_search()` from `hybrid.py`. Normalized to [0,1] by dividing by max BM25 score.
- Reach: BFS from trusted anchors through graph at `min_confidence=0.7`. Hub penalties discount intermediate nodes. Normalized to [0,1] by dividing by max reach score.
- Hub penalty: `tanh(in_degree / HUB_SCALE)` from `hub_penalty.py`. Applied conditionally: only subtracted if `evidence_pre_hub < W_HUB` (prevents over-penalizing files that have strong independent evidence).
- The reach contribution is `W_REACH * reach * max(0, 1 - hub_pen)` -- reach through hub nodes is attenuated.

### 4.3 `min_confidence=0.7` Controls

Passed to:
- `graph_expand_candidates()` -- only follows edges >= 0.7 during BFS expansion
- `compute_reach()` -- only accumulates reach through edges >= 0.7

This means name_match edges with <=2 candidates (confidence 0.6) are excluded from graph expansion. Only same_file (1.0), import (1.0), and single-candidate name_match (0.9) edges participate.

### 4.4 Candidate Set Construction

1. **Semantic top-K**: Top k_sem_top=10 by cosine similarity (or 0 if no sentence-transformers)
2. **Graph expansion**: BFS from trusted anchors, max_depth=3, min_confidence=0.7
3. **BM25 top-K**: Top 10 by BM25 score
4. **Path rescue**: Files whose basename contains issue keywords (bidirectional substring)
5. Union of all four sources

### 4.5 Ablation Variants

| Variant | Active Signals |
|---------|---------------|
| A | SEM only |
| B0 | Graph only (symbol-match anchors) |
| B1 | Graph only (semantic anchors) |
| C | SEM + LEX + REACH + PROX + HUB + PATH (default) |
| D | C + COMMIT |

V1R uses ablation "C" exclusively.

### 4.6 Semantic Fallback

If sentence-transformers is not installed, `_ZeroEmbeddingModel` produces zero vectors. W_SEM is forced to 0.0. Ranking then driven by BM25 (0.50) + PATH (0.45) + graph (0.10) = pure lexical + filename + graph.

---

## 5. Gap Analysis: 4 Failure Modes

### 5.1 Briefcase Mock Assertions

**Likely failure mode:** Briefcase is a cross-platform app packaging library. Mock assertion failures typically involve:
- Missing mock targets (wrong import path for patching)
- Incomplete mock setup (missing return values or side effects)
- Cross-module mocking where the mock must match the imported name, not the source name

**V1R gaps:**
- `_caller_contract_for_file()` at CALLER_CONFIDENCE_FLOOR=0.9 only shows verified cross-file callers. Mock targets often have import-verified edges (confidence 1.0), so these would show up correctly. However, the brief shows the CALL SITE code line, not the mock target path. For mock failures, the agent needs to know the exact import path used by the test, which is a different question than "who calls this function."
- `_function_spec()` detects parallel patterns (repeated template lines), but mock assertion patterns are typically in test files (which are excluded by `is_test=0` filters everywhere). The spec mechanism cannot surface mock patterns.
- No mechanism to show "this function is mocked in tests as X" -- the test_files_for() only shows which test files exist, not how they mock.

### 5.2 Conan Design Patterns

**Likely failure mode:** Conan (C/C++ package manager) uses design patterns like command pattern, plugin pattern, settings model. Failures involve:
- Violating the pattern (e.g., adding a command without registering it)
- Missing required methods in pattern implementations
- Breaking the settings contract

**V1R gaps:**
- `_sibling_context()` shows parallel functions in the same file -- this IS the mechanism that should catch pattern violations. If a class has `do_something_a()`, `do_something_b()`, `do_something_c()`, siblings shows all of them. This is the strongest mechanism for design pattern consistency.
- But sibling context only shows function NAMES, not their structure. The agent sees "siblings: method_a, method_b, method_c" but not "each method follows pattern X."
- `_function_spec()` could help here -- it detects repeated structural patterns WITHIN a function. But cross-function patterns (each method has the same guard clause) are invisible.
- The EDGE_CONFIDENCE_FLOOR=0.7 could be problematic for Conan repos if the graph is C/C++ (Tier 2 language, name-match only). All edges would be name_match, and with many same-named functions across files, confidence could be 0.4-0.6 -- below the floor. This would make callers/callees/tests empty.

### 5.3 Flask Error Chains

**Likely failure mode:** Flask error handling involves:
- Error handler registration (`@app.errorhandler`)
- Exception propagation through middleware/before_request/after_request
- Blueprint error handler inheritance
- Custom exception classes with `__init__` contracts

**V1R gaps:**
- Error handler chains are typically NOT call graph edges. `@app.errorhandler(404)` registers a callback -- the call edge from Flask's dispatch to the handler is inside Flask's own code, not visible in the user's repo graph.
- `_issue_relevant_neighbors()` ranks neighbors by issue keyword overlap. If the issue mentions "error" or "500", files containing those words rank higher. This is reasonable but blunt.
- `_co_change_files()` would be valuable if error handlers and their triggers historically co-change. This mechanism exists and works.
- The test linkage (`_test_files_for()`) at confidence >= 0.7 should work for Flask repos since Flask is Python (Tier 1, import resolution). Tests that import error handlers should link correctly.
- Missing: no concept of decorator-based registration patterns. The graph captures CALLS but not "this function is registered as a handler for X."

### 5.4 Pylint Test Fixtures

**Likely failure mode:** Pylint test infrastructure uses:
- Pytest fixtures with complex scope/autouse/params
- Custom checker test classes with specific test data files
- Functional test pattern: test file + .py data file + .txt expected output
- Conftest.py fixture inheritance

**V1R gaps:**
- `is_test=0` filter everywhere means test infrastructure is invisible. Pylint's test fixtures ARE the edit target in many fixture-related issues, but the brief explicitly excludes them from function ranking, callers, callees, and neighbors.
- If the gold file is a test file or conftest.py, the V1R brief cannot rank it as a top candidate because `_NON_SOURCE` filtering and `is_test` filtering would exclude it (though conftest.py is not in the explicit exclude list, functions in it might be marked is_test).
- `_test_files_for()` shows which test files reference a source file, but NOT which source files a test file references (the query is unidirectional: finds test nodes that have edges pointing AT the source file).
- Pylint is Python (Tier 1), so import resolution should work. But fixture inheritance through conftest.py involves pytest's built-in fixture resolution, which is not a normal import -- it's implicit injection. These edges would be name_match at best.

---

## 6. Router Gaps Related to Failure Modes

The router operates post-brief (in-session), so its gaps are different:

1. **Delegate mode blindness**: In SWE-bench evaluation, `delegate_evidence=True` means the router doesn't query graph.db at all. It only gates on budget/debounce/band. Evidence quality depends entirely on the in-container hook, and the router cannot assess whether the hook's output is useful.

2. **Edit bypass of budget**: Edits always fire, which is correct for the common case. But if the agent edits 15 files (scaffolding trap), each edit gets evidence emissions, potentially flooding context. There's no "too many edits" guard.

3. **Late band suppression of views**: After 75% of iterations, `on_view()` is suppressed. This means if the agent discovers the correct file late, it gets no graph navigation hints. This could hurt tasks where the agent circles back to the right area late in the session.

---

## 7. Evidence Marker Completeness

The marker system is a substring check. Key observation: the L3_MARKERS list contains both structured markers (`[GT_CONTRACT]`) and legacy unstructured ones (`SIGNATURE:`, `CALLERS:`). This suggests the hook output format has evolved over time and the marker check must handle multiple generations.

**Gap:** No negative marker checking. There's no mechanism to detect when a hook emits ONLY metadata/diagnostic content without actionable evidence. The `has_gt_evidence()` function would return True for `[GT] some_diagnostic_metadata` even if it contains no useful evidence.

---

## 8. Weight Distribution Concern

The effective scoring is dominated by two signals:
- W_LEX = 0.50 (BM25 keyword matching)
- W_PATH = 0.45 (filename/path matching to issue terms)

Combined = 0.95 out of ~1.25 total positive weight.

Graph signals (W_REACH=0.05, W_PROX=0.05) contribute ~8% of total score.
Semantic embedding (W_SEM=0.15) contributes ~12%.

This means the V7.4 scorer is essentially a **BM25 + filename matcher with a small graph bonus**. For repos where the correct file has a non-obvious name and the issue text doesn't contain keywords that appear in the file, this scorer will fail. The graph could rescue such cases, but at 8% weight, it would need an improbably high reach score to overcome a BM25 miss.

The sparse graph fallback (W_LEX=0.70, W_PATH=0.45) makes this even more extreme -- pure lexical at that point.
