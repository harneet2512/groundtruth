# AUDIT_MAP.md — GT Localization System Component Map

Generated: 2026-05-17  
Branch: `general_start`  
Commit: `ea7a8dd`

---

## Component 1: L1 Query Extraction

**Purpose:** Extract searchable tokens, identifiers, paths, stacktraces from issue text to seed retrieval.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/query_preprocessor.py` — `QueryObject` construction via regex; no LLM
  - `_is_camel_case()` :31
  - `_is_error_class_name()` :39
  - `_is_identifier_shaped()` :45
  - `_is_path_shaped()` :49
- `src/groundtruth/pretask/anchors.py` ��� `IssueAnchors` dataclass, `_extract_paths()`, `_extract_raw_identifiers()`
- `src/groundtruth/pretask/traces.py` — `parse_stack_traces()` for stackframe extraction
- `src/groundtruth/pretask/v2_types.py` — `QueryObject`, `HighSignalToken`, `TokenSource` type definitions

**Inputs:** Raw issue text string (from instance.problem_statement)

**Outputs:** `QueryObject` with identifiers, paths, error classes, stackframes. `IssueAnchors` passed to hybrid.py.

**Current Algorithm:**
1. Regex-extract backtick-quoted identifiers, fenced code blocks
2. Identify CamelCase names, error class suffixes
3. Parse stacktraces into `StackFrame` objects
4. Extract path-shaped tokens (`/`-separated or dotted)
5. Stopword filter

**Metrics Emitted:** None directly.

**Artifacts Read/Written:** Reads issue text from instance.

**Failure Modes:**
- Issue text with no code tokens → empty query → BM25 falls back to natural language words
- Non-Python stacktraces may not parse

**Tests:** NOT PROVEN — no dedicated test file found for query_preprocessor.

**Gaps:**
- No structured extraction of error messages vs descriptions
- No multi-language stacktrace parsers beyond Python
- No unit tests visible

---

## Component 2: Candidate Generation (Stage A)

**Purpose:** Build the initial candidate file set from multiple channels before scoring.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/v7_4_brief.py` — `run_v74()` lines 245-367
  - Semantic top-K: `select_anchors()` :281 → returns top files by embedding cosine
  - Graph expansion: `graph_expand_candidates()` :310-311
  - BM25 content: `lexical_file_search()` :342-347
  - Path/name rescue: lines 351-365 (bidirectional substring matching)

**Inputs:**
- `issue_text` (string)
- `repo_root` (path)
- `graph_db` (SQLite path)
- `k_anchor=3`, `k_sem_top=10`, `k_lex_top=10`, `tau_anchor=0.20`, `max_depth=3`, `min_confidence=0.7`

**Outputs:** `candidate_set: set[str]` = `sem_files | graph_expanded | lex_top_paths | path_matched_files`

**Current Algorithm:**
1. Semantic anchors: embed issue + file summaries with `all-MiniLM-L6-v2`, cosine top-10
2. BFS graph expansion from trusted anchors (confidence ≥ 0.7) up to depth 3
3. Cap graph-expanded at `max_graph_expand=20` by reach score
4. BM25 via `lexical_file_search()` — keyword TF-IDF over file content, top-10
5. Path/name: all graph files whose basename ∈ issue_words (bidirectional substring, min 4 chars)

**Metrics Emitted:**
- `candidate_set_size` in V74BriefResult
- `gold_in_candidate_set`, `gold_in_bm25_top20`, `gold_in_graph_expanded`, `gold_in_sem_files` in diagnosis JSON

**Artifacts Read/Written:**
- Reads: `graph.db` (nodes table for file paths, edges for BFS)
- Reads: repo files for BM25 content scoring
- Writes: `l1_ranking_diagnosis_{bug_id}.json` to `GT_DEBUG_DIR`

**Failure Modes:**
- `sentence-transformers` unavailable → W_SEM forced to 0, semantic channel dead
- Sparse graph (< 2 edges/file) → weights override to BM25-only mode (line 627 v1r_brief.py)
- Gold file not in any channel → irretrievable regardless of scoring

**Tests:** NOT PROVEN — no dedicated unit test for candidate generation.

**Gaps:**
- Semantic model (`all-MiniLM-L6-v2`) uses first-500-tokens of files — truncation may miss relevant code
- BM25 uses `lexical_file_search()` which tokenizes full content but is called TWICE (once for candidate set, once for scoring) — redundant
- No explicit exact-identifier channel (e.g., "function named X exists in file Y")
- No stacktrace/error-token channel (parsed in query_preprocessor but not used as retrieval signal)

---

## Component 3: BM25/Content Retrieval

**Purpose:** Lexical keyword matching — find files whose content overlaps with issue text.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/hybrid.py` — `lexical_file_search()` (main BM25 implementation)
  - `SignalHit` dataclass :30
  - `_WORD_RE` :48, `_SKIP_DIR_PARTS` :49, `_SOURCE_EXTS` :62
  - BM25 scoring logic (TF-IDF over file tokens)
- Called from `v7_4_brief.py` :342 (candidate generation) and :376 (scoring)

**Inputs:** `issue_text`, `repo_root`, `graph_db`, `IssueAnchors`, `max_files`

**Outputs:** `list[SignalHit]` sorted by BM25 score descending.

**Current Algorithm:**
1. Walk repo, filter by `_SOURCE_EXTS` and `_SKIP_DIR_PARTS`
2. Tokenize issue text into word set
3. For each source file: tokenize content, compute BM25 score (TF × IDF)
4. Return top-N sorted hits

**Metrics Emitted:** Raw scores available via `lex_scores` dict in v7_4_brief, logged in diagnosis JSON as `bm25_raw`.

**Artifacts Read/Written:** Reads all source files in repo (I/O heavy on large repos).

**Failure Modes:**
- Very large repos: walks every file — O(N_files × N_tokens)
- Natural language issues with no code terms → matches on common words, high noise
- Gold file uses different terminology than issue text → BM25 misses it

**Tests:** NOT PROVEN.

**Gaps:**
- No BM25 over file paths/basenames separately (conflated with content scoring)
- No BM25 over symbols/function names (separate channel)
- No IDF computed from corpus — each file scored independently

---

## Component 4: Path/Symbol/Exact Scoring

**Purpose:** Boost files whose path or symbol names match issue identifiers.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/v7_4_brief.py` lines 419-448 — path-name prior scoring
  - Bidirectional substring: `iw in basename or basename in iw` → 0.7
  - Joined (underscore-stripped): `iw in basename.replace("_", "")` → 0.5
  - Directory match: `iw in part_l or part_l in iw` → 0.4
- `src/groundtruth/pretask/anchor_select.py` — symbol-match anchors
  - `_normalize_identifier()` :46 — splits camelCase/snake_case
  - `_extract_issue_tokens()` :66 — regex `[A-Za-z_][A-Za-z0-9_]*` ≥ 3 chars
  - `_issue_word_parts()` :73 — normalized word parts

**Inputs:** `issue_text`, `all_files` (candidate set), path components

**Outputs:** `path_scores: dict[str, float]` — injected as `components["path"]`

**Current Algorithm:**
1. Extract issue words ≥ 4 chars
2. For each candidate file: check basename bidirectional substring → 0.7, joined → 0.5, dir → 0.4
3. Injected into component map, weighted by W_PATH=0.45

**Metrics Emitted:** `path_score` in diagnosis JSON top_20 entries.

**Failure Modes:**
- Short basenames (< 4 chars) never match
- Issue uses different naming than code (e.g., "authentication" vs `auth.py`)
- Over-matching on common words ("test", "util", "main")

**Tests:** NOT PROVEN.

**Gaps:**
- No exact symbol-name lookup channel (function `foo_bar` in issue → find files defining `foo_bar`)
- Path matching is a scoring boost, not a retrieval channel — can't add files not already in candidate set (WRONG: actually does add via lines 351-365)
- Actually path rescue IS also a candidate generation step (lines 351-365) AND a scoring step (419-448) — confusing dual role

---

## Component 5: Semantic Scoring

**Purpose:** Dense cosine similarity between issue embedding and file content embeddings.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/anchor_select.py` — `select_anchors()` computes semantic scores
  - Uses `all-MiniLM-L6-v2` sentence-transformer model
  - Encodes first ~500 tokens of each file
  - Returns `sem_scores: dict[str, float]` — cosine similarity per file
- `src/groundtruth/pretask/v7_4_brief.py` :273-278 — if model unavailable, W_SEM=0

**Inputs:** Issue text, all source file paths (reads first 500 tokens per file)

**Outputs:** `sem_scores: dict[str, float]` normalized [0, 1]

**Current Algorithm:**
1. Lazy-load `all-MiniLM-L6-v2` (384-dim embeddings)
2. If unavailable: `_ZeroEmbeddingModel` → all zeros → W_SEM=0
3. Encode issue text
4. Encode top-K file summaries
5. Cosine similarity → score per file

**Metrics Emitted:** `sem` component in scored files.

**Failure Modes:**
- `sentence-transformers` not installed → all semantic scores = 0
- Model embedding quality on code tokens (trained primarily on natural language)
- 500-token truncation: relevant code may be deeper in file

**Tests:** NOT PROVEN (relies on external model).

**Gaps:**
- No code-specific embedding model (CodeBERT, UniXcoder) — uses NL model
- Fixed 500-token summary — no adaptive extraction of relevant portions
- Weight W_SEM=0.15 is low — reflects distrust in semantic quality on code

---

## Component 6: Graph Reach/Expansion

**Purpose:** BFS from trusted anchors through call graph to find structurally related files.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/graph_reach.py`
  - `graph_expand_candidates()` — returns set of reachable files
  - `compute_reach()` — BFS with decay, returns `dict[str, ReachRecord]`
  - `_build_file_graph()` :44 — builds adjacency from edges table
  - `ReachRecord` :37 — dataclass with `reach_score`, `min_path_length`, `entered_via_graph`
  - Edge type weights: CALLS=1.0, USES=0.8, IMPORTS=0.6, CONTAINS=0.4, INHERITS=0.4

**Inputs:** `trusted` anchor paths, `graph_db`, `max_depth=3`, `min_confidence=0.7`

**Outputs:** `graph_expanded: set[str]`, `reach_scores: dict[str, ReachRecord]`

**Current Algorithm:**
1. Build file-level adjacency (source→target per edge, confidence ≥ min)
2. BFS from each trusted anchor up to max_depth
3. Accumulate reach_score per file: decay by depth, weight by edge type × confidence
4. Return all reachable files as candidates + individual scores

**Metrics Emitted:** `reach` component, `entered_via` ("graph_rescue"/"both"), `gold_in_graph_expanded`

**Failure Modes:**
- Sparse graph → few/no files reachable → graph signals contribute nothing
- Hub files accumulate high reach from many paths → reach normalization (v7_4_brief :391-403) mitigates
- Only CALLS edges exist in most graph.db files → USES/IMPORTS/INHERITS dead paths

**Tests:** NOT PROVEN.

**Gaps:**
- No bidirectional BFS (callers AND callees) — only follows source→target direction
- Edge confidence floor 0.7 may filter too aggressively on name_match edges (0.6-0.9)
- Large graphs: BFS at depth 3 can explode combinatorially

---

## Component 7: Fusion/Reranking

**Purpose:** Combine component scores into final ranking.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/v7_4_brief.py` — `_total_score()` :213-226
  - Formula: `W_SEM*sem + W_LEX*lex + W_REACH*reach*(1-hub_pen) + W_PROX*prox + W_COMMIT*commit + W_PATH*path - W_HUB*hub_pen`
  - Hub penalty subtracted only when `evidence_pre_hub < W_HUB`
- Scoring loop: :451-455
- Sorting: :455 `scored.sort(key=lambda x: x[1], reverse=True)`

**Inputs:** Per-file component dict (sem, lex, reach, anchor_prox, hub_pen, commit, path), weights dict

**Outputs:** Sorted `scored: list[(path, total_score, components)]`

**Current Algorithm:**
Weighted linear combination:
```
score = 0.15*sem + 0.50*lex + 0.05*reach*(1-hub_pen) + 0.05*prox + 0.0*commit + 0.45*path
      - min(0.10, W_HUB) * hub_pen  [only if evidence < W_HUB]
```

**Metrics Emitted:** `score`, all component values in diagnosis JSON.

**Failure Modes:**
- W_LEX + W_PATH = 0.95 dominates — graph/semantic nearly irrelevant
- Hub penalty conditional application complex — if evidence_pre_hub ≥ W_HUB, no penalty
- Non-normalized components mixed (BM25 normalized to [0,1], path is discrete {0, 0.4, 0.5, 0.7})

**Tests:** NOT PROVEN.

**Gaps:**
- Not rank fusion (RRF) — pure weighted sum. Different scales of components.
- BM25 and path scores already normalized [0,1], but semantic may vary
- W_COMMIT=0 (disabled) — commit history unused
- No learning-to-rank or cross-validation of weights

---

## Component 8: Adaptive K

**Purpose:** Determine how many candidates to include in the brief (dynamic cutoff).

**Files/Functions/Lines:**
- `src/groundtruth/pretask/v1r_brief.py` :658-672
  - Minimum 5 candidates (min_k)
  - Score gap analysis: break when gap > 2× median gap
  - Final K = max(gap-determined k, min_k) capped by max_files

**Inputs:** `scores` list from v74 ranked_full, `max_files=5`

**Outputs:** `top_records` — truncated candidate list

**Current Algorithm:**
1. Compute gaps between consecutive scores
2. Find median gap
3. Walk scores; stop when gap > 2× median (natural break)
4. Take max(k, min_k=5) candidates, cap at max_files=5
5. Since min_k=5 and max_files=5, adaptive K almost always returns 5

**Failure Modes:**
- min_k=5 = max_files=5 → adaptive K is effectively ALWAYS 5 (no actual adaptation)
- If all scores are very close (no clear break), returns all 5
- If scores have early large gap, still returns 5 due to min_k floor

**Tests:** NOT PROVEN.

**Gaps:**
- Adaptive K is neutered by min_k = max_files = 5 — always returns exactly 5 (or all if < 5)
- No dynamic expansion when confidence is high (e.g., return 2 if score gap is enormous)
- Logic is dead code given current parameters

---

## Component 9: Hub Handling

**Purpose:** Prevent high-in-degree "hub" files (e.g., __init__.py, utils.py) from dominating rankings.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/hub_penalty.py`
  - `compute_hub_penalties()` :17 — `tanh(in_degree / HUB_SCALE)` where HUB_SCALE=50
  - W_HUB_MAX = 0.10 (hard cap)
- `src/groundtruth/pretask/v7_4_brief.py` :214-226 — hub subtraction in `_total_score()`
- `src/groundtruth/pretask/v1r_brief.py` :711-747 — modulus gate (suppress if all top-3 are hubs)
  - Also hub demotion: if top-1 is 5× above p80, reorder peripheral files first

**Inputs:** `graph_db` (edges table in-degree per file)

**Outputs:** `hub_penalties: dict[str, float]` ∈ [0, 1)

**Current Algorithm:**
1. Count CALLS edges per target file (in-degree)
2. Penalty = tanh(in_degree / 50)
3. Applied as: `score -= W_HUB * hub_pen` (conditional)
4. Modulus gate: if ALL top-3 have degree > p80, suppress entire brief
5. Reorder: if top-1 is massive hub (> 5× p80), demote behind peripherals

**Failure Modes:**
- HUB_SCALE=50 may be too high for small repos (most files have degree < 10)
- Modulus gate can suppress valid briefs when graph is dense
- Hub demotion reordering only checks first 3 candidates

**Tests:** NOT PROVEN.

**Gaps:**
- No inverse-document-frequency style hub weighting
- Hub detection uses absolute degree, not relative (p80 comparison only in modulus gate)
- Only counts CALLS in-degree; IMPORTS edges not counted

---

## Component 10: L3 Post-Edit Runtime Guidance

**Purpose:** After agent edits a file, show callers, contracts, siblings, tests to verify correctness.

**Files/Functions/Lines:**
- `src/groundtruth/hooks/post_edit.py`
  - Priority: 1. Caller CODE lines, 2. Sibling patterns, 3. Signature, 4. Test assertions
  - `_compute_caller_relevance()` :71 — rank callers by issue-term overlap
  - `_MAX_EVIDENCE_CHARS = 1200` :54 (~300 tokens)
  - `_BRIEF_CANDIDATES_PATH = "/tmp/gt_brief_candidates.txt"` :55

**Budget Gate (in wrapper):**
- `scripts/swebench/oh_gt_full_wrapper.py` — L3 cap = 5 fires total
- Suppress after 75% iteration budget consumed

**Inputs:** Edited file path, `graph_db`, `repo_root`, issue terms

**Outputs:** Evidence string (≤1200 chars) appended to observation

**Current Algorithm:**
1. Query graph.db for callers of edited functions (confidence ≥ 0.9)
2. Read actual source lines at call sites
3. Rank callers by issue-term relevance
4. Add sibling patterns (same class, parallel methods)
5. Add signature/return type
6. Truncate at 1200 chars

**Metrics Emitted:** `[GT_DELIVERY] L3 post_edit` logged by wrapper.

**Artifacts Read/Written:**
- Reads: graph.db, source files (for caller code lines)
- Reads: `/tmp/gt_brief_candidates.txt`, `/tmp/gt_issue_terms.txt`

**Failure Modes:**
- 0 callers in graph → empty evidence (common on sparse graphs)
- All callers are tests → less useful for fix verification
- Budget exhaustion (5 fires) → silent for remaining edits

**Tests:** NOT PROVEN (no dedicated test file found).

**Gaps:**
- Evidence quality varies enormously by graph density
- No fallback when graph is empty (legacy fallback exists but quality unclear)
- Same-file callers not shown (only cross-file)

---

## Component 11: L3b Post-View Runtime Guidance

**Purpose:** When agent reads a file, show graph connections (callers/callees/importers) ranked by issue relevance.

**Files/Functions/Lines:**
- `src/groundtruth/hooks/post_view.py`
  - `graph_navigation()` :188 — main entry point
  - `_score_by_issue_relevance()` :113 — re-rank neighbors by issue term hits
  - `_load_issue_terms()` :104 — reads `/tmp/gt_issue_terms.txt`
  - `_load_visited_files()` :131 — suppress already-visited
  - `_load_brief_candidates()` :140 — knows which files were briefed

**Budget Gate (in wrapper):**
- L3b cap = 3 fires total
- Suppress after 75% iteration

**Inputs:** Viewed file path, `graph_db`, `repo_root`

**Outputs:** Navigation lines (callers/callees ranked by issue relevance)

**Current Algorithm:**
1. Query graph.db for file's callers and callees (confidence ≥ 0.5)
2. Score each neighbor by issue-term presence in their content
3. Suppress already-visited files
4. Return top-5 neighbors as navigation suggestions

**Metrics Emitted:** `[GT_DELIVERY] L3b post_view` logged by wrapper.

**Failure Modes:**
- 0 edges for viewed file → empty navigation
- Already-visited suppression may hide important re-visit suggestions
- Budget exhaustion (3 fires) → silent for remaining views

**Tests:** NOT PROVEN.

**Gaps:**
- Only 3 fires per task — very limited guidance window
- No "follow-up" mechanism after budget exhausted
- Hub files as neighbors still shown (no filtering)

---

## Component 12: Stale/Late Guidance Classifier

**Purpose:** Classify whether GT evidence is stale (file already visited) or late (decision already made).

**Files/Functions/Lines:**
- `scripts/localization_metrics.py` :113-144 — stale_guidance_count metric
  - Only counts "→ Next: read X" where X already in `already_viewed_paths`
  - Does NOT count "Called by:" or "Calls into:" as stale
- `src/groundtruth/hooks/post_view.py` :131 — `_load_visited_files()` suppresses visited

**Inputs:** Agent history (viewed files timeline), GT evidence events

**Outputs:** `stale_guidance_count`, `late_guidance_count` (late currently always 0)

**Current Algorithm:**
1. Track viewed files chronologically
2. For each GT event containing "Next: read X":
   - If X ∈ already_viewed_paths → stale +1
3. Relationship evidence ("Called by:", "Calls:") → NOT counted as stale

**Metrics Emitted:** `stale_guidance_count` in localization_metrics output.

**Failure Modes:**
- Path normalization: `/workspace/` prefix stripping may miss some matches
- Only measures stale "Next: read" — other stale patterns not counted
- `late_guidance_count` always 0 — not implemented

**Tests:** NOT PROVEN.

**Gaps:**
- `late_guidance_count` is defined but never computed (always 0)
- No classification taxonomy as specified in /goal (instruction_stale, relationship_update, late_telemetry, useful_next_action)
- Measurement only in offline metrics script, not runtime

---

## Component 13: Metrics Logger

**Purpose:** Compute localization quality metrics from run artifacts.

**Files/Functions/Lines:**
- `scripts/localization_metrics.py` — offline metrics harness
  - `compute_task_metrics()` :35 — all metric computation
  - `extract_gold_files_from_patch()` :21 — gold files from git patch
  - `print_metrics_table()` :202 — formatted output

**Inputs:** `output.jsonl` artifacts from OH runs

**Outputs:** Per-task dict with 20 metrics. Printed table.

**Current Algorithm:**
1. Parse output.jsonl (single JSON line)
2. Extract gold files from test_result.git_patch
3. Parse L1 brief files from first history entries containing `gt-task-brief`
4. Walk history: track actions, file views, file edits, GT events
5. Compute hit@K, MRR, first_gold_view, edit_precision, stale count, bridges

**Failure Modes:**
- Brief parsing regex (`^\d+\. `) may miss non-standard brief formats
- Gold file extraction from patch only works if patch exists
- Basename matching for gold can collide (e.g., multiple `__init__.py`)

**Tests:** NOT PROVEN.

**Gaps:**
- No `candidate_set_contains_gold` metric (only available from diagnosis JSON, not from output.jsonl)
- No `gold_rank_before/after_fusion` (requires internal scorer state, not available post-hoc)
- No `action_economy_vs_baseline` (requires paired run comparison)
- `late_guidance_count` always 0
- No unit/smoke tests for metric computation

---

## Component 14: Report Generator

**Purpose:** Produce human-readable localization quality reports.

**Files/Functions/Lines:**
- `scripts/localization_metrics.py` :202 — `print_metrics_table()` (text table)
- `LOCALIZATION_FINAL_REPORT.md` — manual report from prior work

**Inputs:** metrics_list from compute_task_metrics

**Outputs:** Formatted table with aggregates

**Current Algorithm:** Print table with columns: task, hit@1/3/5, MRR, first_gold_view, first_edit, actions, edit_prec, bridges, stale, resolved, fix_rate.

**Failure Modes:** None critical — output formatting only.

**Tests:** NOT PROVEN.

**Gaps:**
- No structured JSON output for CI consumption
- No comparison mode (GT vs baseline side-by-side)
- No historical tracking (run-over-run comparison)

---

## Component 15: V1R Brief Renderer

**Purpose:** Render final brief text from scored FileEntry objects.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/v1r_brief.py` :583-604 — `render_brief()`
  - Format: `<gt-task-brief>` XML wrapper
  - Per file: rank, path, (functions), Spec:, Callers:, Context:, Also changes:, Calls:, Tests:

**Inputs:** `list[FileEntry]`

**Outputs:** String brief text (≤400 tokens target)

**Current Algorithm:**
1. Number each file 1-N
2. Add function names in parentheses
3. Optional lines: Spec, Callers, Context, Also changes, Calls, Tests
4. Wrap in `<gt-task-brief>` tags
5. Truncate entries if > MAX_BRIEF_TOKENS

**Failure Modes:**
- Token estimate is approximate (char/4 heuristic likely)
- Rich metadata (Spec, Callers) can blow past 400 tokens with 5 files

**Tests:** NOT PROVEN.

**Gaps:**
- Fixed format — no adaptation based on issue type
- No confidence indicators per file in rendered output

---

## Component 16: Density Gate

**Purpose:** Detect sparse graphs and switch to BM25-only weights.

**Files/Functions/Lines:**
- `src/groundtruth/pretask/v1r_brief.py` :619-629
  - If `edges_per_file < 2.0` → override weights to `W_LEX=0.70, W_COMMIT=0.30`
  - Disables semantic, reach, proximity, hub penalty

**Inputs:** `graph.db` edge/file counts

**Outputs:** Modified `weights` dict passed to `run_v74()`

**Current Algorithm:**
1. Count total edges and distinct file_paths in nodes
2. If ratio < 2.0: force BM25-dominant weights

**Failure Modes:**
- Threshold 2.0 is arbitrary — may be too aggressive on medium-density graphs
- W_COMMIT=0.30 but commit scoring not implemented (always 0) → effectively W_LEX=0.70 alone
- No W_PATH in sparse override → path-name matching disabled in sparse mode

**Tests:** NOT PROVEN.

**Gaps:**
- Sparse override doesn't include W_PATH — path matching lost on sparse graphs
- W_COMMIT=0.30 is dead weight (commit scorer disabled globally)
- No logging when density gate activates

---

## Component 17: Wrapper/Plumbing (OH Integration)

**Purpose:** Wire GT layers into OpenHands agent lifecycle — inject brief, augment observations, manage budget gates.

**Files/Functions/Lines:**
- `scripts/swebench/oh_gt_full_wrapper.py`
  - `install_graph_and_hook()` :1649 — upload gt-index, build graph.db inside container
  - `generate_task_brief()` :2840 — orchestrate L1 brief generation
  - Brief runner script :2952-2996 — inline Python run inside container
  - `fused_n` / `ranked_count` check :3035-3040 — determines if brief is valid
  - `append_observation()` :1454 — augment agent observations with GT evidence
  - L3b post-view hook :2164 — fires on FileReadObservation
  - L3 post-edit hook :2512 — fires on FileEditObservation
  - Budget gates: L3b cap=3 (:2164 area), L3 cap=5 (:2512 area)

**Firing Time:** 
- `install_graph_and_hook`: once at task start (before agent)
- `generate_task_brief`: once at task start (injected into instruction)
- `append_observation`: on every agent observation (filtered by type)
- L3b: on file read observations (budget=3)
- L3: on file edit observations (budget=5)

**Evidence Shown to Agent:**
- L1: `<gt-task-brief>` XML block with ranked files + metadata
- L3b: Graph navigation text appended to read result
- L3: Caller code + contracts appended to edit result

**Metrics Logged:**
- `[GT_META] L1 brief injected (N chars)`
- `[GT_DELIVERY] L3b post_view: evidence_len=N`
- `[GT_DELIVERY] L3 post_edit: evidence_len=N`
- `[GT_META] Brief runner raw output (N chars)`
- `[GT_META] Brief runner stderr`

**Failure Modes:**
- `fused_n` always 0 when `fused_candidates` key missing (FIXED: now checks `ranked_count`)
- Brief runner crashes silently (stderr captured to `/tmp/gt_brief_stderr.log`)
- gt-index binary upload failure (fallback: check PATH)
- Graph.db empty after indexing (retry with alt root)

**Correct FINAL_ARCH Layer:** Plumbing (spans all layers — delivery mechanism)

---

## Component 18: Validation (eval harness integration)

**Purpose:** Run SWE-bench eval after agent finishes to determine resolve status.

**Files/Functions/Lines:**
- `.github/workflows/swebench_30task.yml` :183-230 — eval harness step
- `scripts/swebench/convert_to_submission.py` — converts output.jsonl to predictions
- eval_result.json artifact — contains resolved/tests_status

**Firing Time:** After agent completes (post-run step in GHA workflow)

**Evidence Shown to Agent:** None (agent already finished)

**Metrics Logged:** eval_result.json with FAIL_TO_PASS/PASS_TO_PASS test counts

**Failure Modes:** 
- No patch produced → eval skipped ("no_patch" status)
- Container timeout during eval → "eval_no_report" status
- PASS_TO_PASS regression zeroes fix_rate

**Correct FINAL_ARCH Layer:** Layer E (metrics/telemetry — measures outcome, invisible to agent)

---

## Firing Time Summary (all components)

| Component | Fires When | FINAL_ARCH Layer |
|-----------|-----------|------------------|
| Query Extraction | Pre-task (issue text parsed) | Layer A |
| Candidate Generation | Pre-task (before agent starts) | Layer A |
| BM25 Retrieval | Pre-task (scores all files) | Layer A |
| Path/Symbol Scoring | Pre-task (boosts path matches) | Layer A |
| Semantic Scoring | Pre-task (embedding cosine) | Layer A |
| Graph Reach | Pre-task (BFS from anchors) | Layer A |
| Fusion/Reranking | Pre-task (weighted sum) | Layer A |
| Adaptive K | Pre-task (determines brief size) | Layer A |
| Hub Handling | Pre-task (demotes hubs) | Layer A |
| Graph Neighbor Expansion | Pre-task (adds 1-hop callers/callees) | Layer A |
| Brief Renderer | Pre-task (formats output) | Layer A |
| Density Gate | Pre-task (sparse graph detection) | Layer A |
| L3b Post-View | On file read (budget=3) | Layer B |
| L3 Post-Edit | On file edit (budget=5) | Layer C (combined C+D per OH constraint) |
| Stale/Late Classifier | Offline metrics computation | Layer E |
| Metrics Logger | Continuous (all events) | Layer E |
| Report Generator | Offline (post-run analysis) | Layer E |
| Validation/Eval | Post-agent (eval harness) | Layer E |

---

## Summary of Critical Gaps

| Gap | Severity | Component |
|-----|----------|-----------|
| No unit tests for any component | HIGH | All |
| `late_guidance_count` never computed | MEDIUM | 12 |
| `candidate_set_contains_gold` not in offline metrics | MEDIUM | 13 |
| Adaptive K is dead code (min=max=5) | LOW | 8 |
| W_COMMIT=0.30 in sparse override but scorer disabled | LOW | 16 |
| BM25 called twice (candidate gen + scoring) | LOW (perf) | 2, 3 |
| No exact-identifier retrieval channel | MEDIUM | 2 |
| No stacktrace-file resolution | MEDIUM | 2 |
| Sparse override drops W_PATH | MEDIUM | 16 |
| No stale/late taxonomy as specified | MEDIUM | 12 |
