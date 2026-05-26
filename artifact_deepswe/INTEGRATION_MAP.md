# GT Integration Map: jedi__branch (OpenHands)

## Files Changed (GT delta vs master)

| File | Role |
|------|------|
| `scripts/swebench/oh_gt_full_wrapper.py` | All GT↔OH integration: brief injection, hook routing, layer management (~5900 lines) |
| `src/groundtruth/pretask/v1r_brief.py` | V1R hybrid ranking + brief rendering (map-only format with callers, tests, specs) |
| `src/groundtruth/pretask/v22_brief.py` | Latest v8.2.2 RRF brief generator (host-side, no in-container deps) |
| `src/groundtruth/pretask/v7_4_brief.py` | Hybrid scorer: BM25 + semantic + graph reach + hub penalty |
| `src/groundtruth/pretask/anchors.py` | Issue text → search seeds: symbols, file paths, test names |
| `src/groundtruth/hooks/post_edit.py` | Post-edit evidence: caller code lines, siblings, signatures, tests |
| `src/groundtruth/hooks/post_view.py` | Post-view navigation: callers, callees, importers (graph hop) |
| `src/groundtruth/trajectory/governor.py` | L5 trajectory governor (WHEN to intervene) |
| `src/groundtruth/state/agent_state.py` | Canonical agent state tracking (views, edits, searches) |
| `src/groundtruth/router/router.py` | V2 collaboration router (shadow mode, not yet active) |
| `benchmarks/swebench/run_mini_gt_v7.py` | Mini-swe-agent integration (hook injection, monkey-patch) |
| `benchmarks/swebench/run_mini_gt_pro_v10.py` | Pro variant with precomputed ego-graphs |
| `benchmarks/swebench/gt_hook.py` | Self-contained in-container tool (~115KB, ~3800 lines) |

## Injection Mechanism

### L1 Brief (one-shot, before first agent turn)

**Entry:** `patched_get_instruction()` in `oh_gt_full_wrapper.py` line 5418

**Flow:**
1. Extract `problem_statement` from SWE-bench instance (line 5162)
2. Extract issue terms via regex: words >3 chars → `/tmp/gt_issue_terms.txt` (line 5232)
3. Extract anchors via `anchors.py::extract_issue_anchors()` → symbols, paths, test_names (line 5243)
4. Call `generate_task_brief(instance)` (line 5447) which calls v22 or v1r brief pipeline
5. Brief text wrapped in `<gt-task-brief>...</gt-task-brief>` XML tags (line 5637)
6. Prepended to agent's first message: `content = brief + tools_hint + demo + original_content`

### L3 Post-Edit (after every source file edit)

**Entry:** `patched_run_action()` in wrapper, fires on `FileEditorAction`

**Flow:**
1. Extract edited file path from action
2. Query graph.db for functions in that file
3. For each function: get callers, siblings, signature, test assertions
4. Format as compact evidence block (≤300 tokens)
5. Append to observation via `append_observation(obs, evidence_text)`

### L3b Post-View (after every file read)

**Entry:** Same `patched_run_action()`, fires on file read actions

**Flow:**
1. Query graph.db for callers/callees/importers of the viewed file
2. Rank by issue-term relevance
3. Suppress already-visited files
4. Annotate brief candidates with [CANDIDATE]
5. Append graph navigation hints to observation

### L5 Trajectory Governor (event-driven)

**Entry:** Governor checks on every post-edit/post-view event

**Triggers:**
- Non-source edit without source progress → warning
- Same failure persisted after edit → repair contract
- Diff collapsed to zero → redirect
- Agent finishing without verification → unsafe-finish alert

### L6 Reindex (hidden from agent)

Incremental `gt-index` rerun before L3 hooks to keep graph fresh after edits.

## Seed Extraction

**Module:** `src/groundtruth/pretask/anchors.py`

1. **Raw identifiers:** Regex `\b([A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]+)*)\b`
2. **Stopword filter:** Removes common English words and programming keywords
3. **Graph cross-check:** `SELECT DISTINCT name FROM nodes WHERE name IN (...)` against graph.db
4. **File paths:** Extracted via multiple regex patterns (quoted paths, dotted module paths, backtick paths)
5. **Test names:** pytest-style `test_*` patterns

**Output:** `{"symbols": [...], "paths": [...], "test_names": [...]}`

## Briefing Format

### v22 (latest, v8.2.2 RRF):
```xml
<gt-task-brief>
## Focus files (top-5)
[VERIFIED] path/to/foo.py  (rank=1, score=0.812)
[WARNING]  path/to/bar.py  (rank=2, score=0.654)

<gt-focus-functions>
path/to/foo.py:42 — handle_request (rank=1, score=0.901, tier=[VERIFIED])
path/to/foo.py:108 — _validate (rank=2, score=0.755, tier=[VERIFIED])
</gt-focus-functions>
</gt-task-brief>
```

### v1r (fallback, map-only):
```xml
<gt-task-brief>
1. path/to/file.py (func1, func2, func3)
   Spec: handles: case1 | case2
   Callers: path/to/caller.py:42 `caller_code`
   Context: sibling1, sibling2
   Calls: neighbor1, neighbor2
   Tests: test_file1

Edit [top_file] first. Verify: pytest [test_path]
</gt-task-brief>
```

## GT Server Communication

**Direct sqlite3 — no MCP protocol.** All graph.db access is via Python's `sqlite3` module:
- `v1r_brief.py`: `sqlite3.connect(graph_db)` for ranking queries
- `post_edit.py`: `sqlite3.connect(f"file:{db}?mode=ro", uri=True)` for evidence queries
- `anchors.py`: `sqlite3.connect(db_path)` for anchor cross-check

The MCP server (`src/groundtruth/mcp/server.py`) exposes tools for interactive agent use but is NOT used in the benchmark integration. Briefing injection is more effective than optional tool calls (Decision 16, backed by Strands 100% vs 82.5%).

## Admissibility Gate

**Brief-level:**
- Suppressed if `fused_n == 0` (no candidates from graph ranking)
- Suppressed if brief length < 100 chars
- Suppressed if brief contains `[GT_BRIEF_FAILED]` marker
- Suppressed if graph build failed
- Fallback: minimal guidance pointing to gt_search tool

**Edge-level:**
- All brief queries: `confidence >= 0.7`
- All L3/L3b evidence queries: `confidence >= 0.5`
- Hub penalty: `score / log(in_degree + 2)` demotes high-degree nodes
- Sparse graph detection: `edges_per_file < 2.0` → BM25-only mode

**Token-level:**
- Brief: MAX_BRIEF_TOKENS = 600
- L3 evidence: ≤300 tokens per edit
- L3b navigation: iteration-aware decay (1000/640/320/0 char caps)
