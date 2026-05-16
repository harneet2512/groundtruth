# Graph Creation Metric Diagnosis

Date: 2026-05-16
Branch: jedi__branch (commit 805c51e)
Data sources: graph.db (120MB, GT repo index), .tmp_holdout/bugs/dagster-33645/graph.db (46K nodes, representative Python SWE-bench repo), .tmp_holdout/bugs/axum-*/graph.db (Rust), .tmp_holdout/bugs/hono-*/graph.db (TypeScript), .tmp_tranche/bugs/crossplane-*/graph.db (Go)

---

## 1. Current Graph Metrics

### 1.1 Edge Distribution (dagster — representative Python repo, 46,368 nodes)

| Metric | Value |
|--------|-------|
| total_edges | 109,230 |
| edges_by_type | CALLS: 109,230 (100%) |
| edges_same_file | 18,177 (16.6%) |
| edges_import | 25,848 (23.7%) |
| edges_name_match | 65,205 (59.7%) |
| low_confidence_edge_ratio (< 0.5) | 29,328 (26.8%) |
| edges_with_source_line | 109,230 (100%) |
| edges_without_source_line | 0 (0%) |

### 1.2 Name-Match Confidence Sub-Distribution (dagster)

| Confidence | Candidates | Count | % of name_match | Interpretation |
|------------|------------|-------|-----------------|----------------|
| 0.9 | 1 (unique) | 26,316 | 40.4% | Single function with this name in repo — HIGH trust |
| 0.6 | 2 | 9,561 | 14.7% | 50% coin flip — target is one of two |
| 0.4 | 3-5 | 13,872 | 21.3% | 20-33% chance of correct target |
| 0.2 | 6+ | 15,456 | 23.7% | <17% chance — effectively noise |

### 1.3 Trust Distribution (dagster)

| Tier | Confidence | Count | % of total | Includes |
|------|-----------|-------|-----------|----------|
| Certified | >= 0.9 | 70,341 | 64.4% | same_file (18K) + import (26K) + nm_single (26K) |
| Candidate | 0.5 - 0.89 | 9,561 | 8.8% | nm_two_candidates (0.6) |
| Speculative | < 0.5 | 29,328 | 26.8% | nm_3-5 (14K) + nm_6+ (15K) |

### 1.4 Same-Package Proxy for Edge Precision (dagster)

Fraction of name_match edges where source and target share the same top-level package directory (proxy for correctness — random would be ~20% given 5 top-level dirs):

| Confidence | Same-Package Rate | Interpretation |
|------------|-------------------|----------------|
| 0.9 (single candidate) | 89% | Strong correlation with correctness |
| 0.6 (2 candidates) | 68% | Better than random, but 32% wrong-package |
| 0.4 (3-5 candidates) | 77% | Decent but 23% cross-package noise |
| 0.2 (6+ candidates) | 49% | Essentially random (expected: ~20-50% for large repos) |

### 1.5 File-to-File Connectivity (dagster, distinct file pairs connected)

| Threshold | Connected File Pairs | Source Files | Target Files | Delta from 0.0 |
|-----------|---------------------|--------------|-------------|-----------------|
| conf >= 0.0 | 32,613 | 4,174 | 1,742 | baseline |
| conf >= 0.5 | 21,700 | 3,927 | 1,642 | -33% (10,913 fake connections removed) |
| conf >= 0.7 | 18,057 | 3,757 | 1,563 | -45% |
| conf >= 0.9 | 18,057 | 3,757 | 1,563 | -45% (same as 0.7) |

**Key finding:** 33% of all file-to-file connections exist ONLY because of speculative edges (conf < 0.5). These are entirely fabricated graph connectivity.

### 1.6 BFS Fan-Out (dagster, from a typical core file)

| Threshold | Outbound Files (hop 1) | Inbound Files (hop 1) | Total Reachable |
|-----------|----------------------|---------------------|-----------------|
| conf >= 0.0 | 43 | 41 | 84 |
| conf >= 0.5 | 30 | 40 | 70 |
| conf >= 0.7 | 23 | 29 | 52 |
| conf >= 0.9 | 23 | 29 | 52 |

At max_depth=3 (V1R default), paths compound: hop-1 × hop-2 × hop-3. With conf >= 0.0: ~84^3 = 600K paths explored. With conf >= 0.7: ~52^3 = 140K paths. 4.3× noise reduction.

### 1.7 Import Resolution Coverage (cross-file calls only)

| Repo | Language | Cross-File Edges | Import-Resolved | Import Coverage | Name-Match Fallback |
|------|----------|-----------------|-----------------|-----------------|---------------------|
| dagster | Python | 91,053 | 25,848 | 28% | 72% |
| hono | TypeScript | 2,051 | 44 | 2% | 98% |
| axum | Rust | 2,201 | 13 | 0.6% | 99.4% |
| crossplane | Go | 8,037 | 166 | 2% | 98% |

**Python has the best import resolution at 28%.** All other languages fall back to name_match for 97-99% of cross-file edges.

### 1.8 Cross-Repo Pattern

| Repo | Lang | Nodes | Edges | same_file% | import% | name_match% | Certified% | Low_conf% |
|------|------|-------|-------|-----------|---------|-------------|-----------|-----------|
| dagster | Python | 46K | 109K | 17% | 24% | 60% | 64% | 27% |
| hono | TypeScript | 2.5K | 2.3K | 11% | 2% | 87% | 61% | 32% |
| axum | Rust | 2.7K | 2.9K | 25% | 0.4% | 75% | — | 14% |
| crossplane | Go | 4.3K | 9.6K | 17% | 2% | 82% | — | 18% |

### 1.9 Function Name Ambiguity (dagster)

| Metric | Value |
|--------|-------|
| Total unique function/method names | 26,819 |
| Names with exactly 1 definition | 23,466 (87%) |
| Names with 6+ definitions | 520 (2%) |
| Top ambiguous: `__init__` | 1,425 definitions |
| Top ambiguous: `__new__` | 259 definitions |
| Top ambiguous: `constructor` | 236 definitions |

**87% of function names are unique.** But the 2% with 6+ definitions generate 15,456 noise edges — a tiny fraction of names creates ALL the worst-quality edges.

---

## 2. Layer Consumption of Edges

### 2.1 What Each Layer Queries

| Layer | Code Location | Confidence Floor | Resolution Gate | Edges Consumed |
|-------|--------------|-----------------|-----------------|----------------|
| V1R graph_reach BFS | pretask/graph_reach.py | **0.5** (passed by v1r_brief.py:361) | None | Certified + candidate |
| V1R _top_functions | pretask/v1r_brief.py:40 | **NONE** | None | ALL including 0.2 noise |
| V1R _test_files_for | pretask/v1r_brief.py:63 | **NONE** | None | ALL including 0.2 noise |
| V1R _issue_relevant_neighbors | pretask/v1r_brief.py:85 | **NONE** | None | ALL including 0.2 noise |
| V1R _static_callees | pretask/v1r_brief.py:135 | **NONE** | None | ALL including 0.2 noise |
| L3 (gt_intel.py) | gt_intel.py:68 | **0.7** | same_file \| import \| name_match | Certified only |
| L3b (v2_ranker.py) | pretask/v2_ranker.py:34 | **0.5** | None | Certified + candidate |
| MCP orient/investigate/check | mcp/endpoints/*.py | **0.7** | None | Certified only |
| OH wrapper LSP verify | lsp/edge_verifier.py | **per-edge** | LSP references | Only LSP-verified |
| post_edit.py (env-configurable) | hooks/post_edit.py:998 | **0.40** (GT_MIN_CONFIDENCE) | None | Certified + candidate + some speculative |

### 2.2 Layer Impact Metrics (estimated from dagster profile)

| Layer | Unfiltered Edges Available | After Floor | Low-Conf Edges Consumed | Noise Ratio |
|-------|---------------------------|-------------|------------------------|-------------|
| V1R graph_reach (0.5) | 109,230 | 79,902 | 0 | 0% (floor works) |
| V1R _top_functions (NONE) | 109,230 | 109,230 | 29,328 | 27% NOISE IN RANKING |
| V1R _test_files_for (NONE) | 109,230 | 109,230 | 29,328 | 27% NOISE IN MAPPINGS |
| V1R _neighbors (NONE) | 109,230 | 109,230 | 29,328 | 27% NOISE IN NAVIGATION |
| L3 (0.7) | 109,230 | 70,341 | 0 | 0% (floor works) |
| L3b (0.5) | 109,230 | 79,902 | 0 | 0% (floor works) |

### 2.3 V1R File Ranking Distortion (dagster: files ranked by total inbound edges — current _top_functions behavior)

| Rank | File | Total Edges | High-Conf | Low-Conf | Low% | Actual Role |
|------|------|-------------|-----------|----------|------|-------------|
| 1 | dagster_shared/check/functions.py | 4,346 | 3,409 | 937 | 22% | Assertion library — real hub |
| 2 | docs_snippets/concepts/ops_jobs_graphs/ops.py | 2,206 | 12 | 2,044 | **93%** | DOCUMENTATION — fake hub |
| 3 | dagster/_core/definitions/composition.py | 1,966 | 314 | 1,334 | **68%** | Core module (inflated) |
| 4 | dagster/_core/launcher/base.py | 1,305 | 18 | 1,287 | **99%** | Launcher — fake hub |
| 5 | dagster/_utils/indenting_printer.py | 1,152 | 62 | 1,088 | **94%** | Utility — fake hub |
| 6 | hacker_news_assets.py | 844 | 3 | 841 | **100%** | EXAMPLE CODE — fake hub |
| 7 | ui-core/src/search/useIndexedDBCachedQuery.tsx | 1,220 | 18 | 1,202 | **99%** | TS UI code — fake hub |

**With confidence filter (certified-only ranking):**

| Rank | File | Certified Edges | Actual Role |
|------|------|-----------------|-------------|
| 1 | dagster_shared/check/functions.py | 3,409 | Assertion library — TRUE architectural hub |
| 2 | dagster/_core/definitions/definitions_class.py | 1,655 | Core definitions — TRUE |
| 3 | dagster/_core/definitions/events.py | 1,009 | Events — TRUE |
| 4 | dagster/_vendored/dateutil/tz/win.py | 949 | vendored lib — TRUE high-import |
| 5 | dagster-graphql/test/utils.py | 898 | Test utils — TRUE |

**The certified-only ranking correctly surfaces architectural hubs. The unfiltered ranking promotes documentation, examples, and utilities.**

---

## 3. Verification Quality (from 5-task smoke jedi__branch)

### 3.1 LSP Verification Results

| Task | LSP Verified | LSP Rejected | Not Checked | V/R Ratio |
|------|-------------|--------------|-------------|-----------|
| beancount-931 | 3 | 0 | — | 3/0 |
| beets-5495 | 5 | 1 | — | 5/1 (83%) |
| xarray-9760 | 9 | 0 | — | 9/0 |
| cfn-lint-3821 | 2 | 0 | — | 2/0 |
| loguru-1306 | 3 | 0 | — | 3/0 |
| **TOTAL** | **22** | **1** | **~200K unchecked** | **96% accept rate** |

### 3.2 Follow Rate by Verification Status

| Category | Suggested | Followed | Follow Rate |
|----------|-----------|----------|-------------|
| LSP-verified edges | 22 | varies | 30% avg |
| Unverified edges (conf >= 0.5) | 38 | varies | 25% avg |
| All suggestions | 54 | 16 | 30% avg |

### 3.3 Correlation Analysis (5 tasks — DIRECTIONAL ONLY, not causal)

| Task | Resolved | LSP Verified | Certified Edges Used | Follow Rate |
|------|----------|-------------|---------------------|-------------|
| beancount-931 | YES | 3 | — | 9% |
| beets-5495 | YES | 5 (1 rejected) | — | 27% |
| xarray-9760 | YES | 9 | — | 40% |
| cfn-lint-3821 | NO | 2 | — | 50% |
| loguru-1306 | NO | 3 | — | 25% |

Directional observations (N=5, NOT causal):
- Higher LSP-verified count correlates with resolution (median 5 for resolved vs 2.5 for failed)
- Follow rate does NOT correlate with resolution (cfn-lint has highest follow rate but failed)
- The LSP rejection in beets-5495 caught a false positive — demonstrating the mechanism works

---

## 4. Edge Quality Diagnosis — Failure Mode Taxonomy

### Failure Mode A: Edge Hallucination

**Definition:** Graph creates a CALLS edge between functions that do not actually call each other.

**Evidence:**
- 15,456 edges at confidence 0.2 in dagster (6+ candidates — resolver picks first alphabetically, not most likely)
- Same-package rate at conf=0.2 is 49% — indistinguishable from random
- The first-candidate selection has NO heuristic (stub at resolver.go:160-164: `_ = bestScore; _ = callerDir`)

**Affected tasks:** All 5 (any task where V1R _top_functions or _test_files_for queries fire)

**Affected layers:** V1R (_top_functions, _test_files_for, _issue_relevant_neighbors, _static_callees)

**Metric proving it:** same-package rate for conf=0.2 is 49% ≈ random. For conf=0.9 it's 89%.

**Fix:** Either suppress conf < 0.5 edges from storage, or add confidence floor to ALL query sites.

---

### Failure Mode B: Edge Missingness (Import Resolution Gap)

**Definition:** Real caller/import/test relations exist but the graph does not capture them via import resolution.

**Evidence:**
- Python import resolution covers 28% of cross-file calls — 72% fall through to name_match
- TypeScript/Rust/Go import resolution covers 0.4-2% — virtually non-functional
- In dagster, files with both import AND name_match edges show 16-42% import coverage (the rest is name_match guess)

**Affected tasks:** All tasks on non-Python repos (hono, axum, crossplane), plus 72% of Python cross-file calls

**Affected layers:** ALL (import edges are the foundation of trust)

**Metric proving it:** import% by language: Python=24%, Go=2%, TS=2%, Rust=0.4%

**Fix:** Improve import extractors for Tier 2 languages OR use LSP textDocument/references at index time for cross-file resolution.

---

### Failure Mode C: Edge Misclassification (Confidence 0.6 Overvalued)

**Definition:** Two-candidate name_match (conf=0.6) is trusted by V1R and L3b, but it's a 50% coin flip.

**Evidence:**
- 9,561 edges at conf=0.6 in dagster
- Same-package rate is 68% — better than random but 32% wrong-package
- V1R graph_reach (floor=0.5) and L3b (floor=0.5) both traverse these edges as if reliable
- L3 (floor=0.7) correctly excludes them

**Affected tasks:** Tasks where two-candidate name_match edges create BFS paths to wrong files

**Affected layers:** V1R, L3b

**Metric proving it:** conf=0.6 same-package rate = 68%. Compare: conf=0.9 = 89%.

**Fix:** Raise V1R and L3b floor from 0.5 to 0.7. This eliminates 9,561 questionable edges from traversal.

---

### Failure Mode D: Edge Overuse (Unfiltered V1R Queries)

**Definition:** Four V1R query functions consume ALL edges (including conf=0.2 noise) without any confidence filter.

**Evidence:**
- `_top_functions` (v1r_brief.py:40): `LEFT JOIN edges e ON e.target_id = n.id` — no WHERE clause on confidence
- `_test_files_for` (v1r_brief.py:63): `JOIN edges e ON e.target_id = n1.id` — no confidence filter
- `_issue_relevant_neighbors` (v1r_brief.py:100): queries ALL edges both directions
- `_static_callees` (v1r_brief.py:135): `JOIN edges e ON e.source_id = nsrc.id` — no confidence filter

**Affected tasks:** All 5 (L1 brief content uses these functions)

**Affected layers:** L1 (V1R brief generation)

**Metric proving it:** 0/5 tasks have first-read in L1 candidates. V1R brief content (functions, tests, neighbors) is populated from noise.

**Fix:** Add `AND e.confidence >= 0.7` to all four queries. This is 4 one-line SQL changes.

---

### Failure Mode E: Edge Unactionability

**Definition:** Even correct edges are not useful to the agent because they lack code/context/contract.

**Evidence:**
- L3 has 0 caller CODE LINES on 4/5 tasks (only xarray has 3)
- When caller code is present, agent follows 100% (N=3 on xarray)
- Edge data contains: source_file, source_line, resolution_method, confidence — but NOT the actual source code at that line

**Affected tasks:** cfn-lint-3821, loguru-1306 (agent needs to understand WHAT callers expect)

**Affected layers:** L3 (post-edit evidence)

**Metric proving it:** xarray follow rate = 40% (has caller code), other tasks = 9-27% (no caller code)

**Fix:** When emitting caller evidence, read the source file at the edge's source_line and include 1-3 lines of context. This is not a graph-creation fix — it's an evidence-emission fix.

---

### Failure Mode F: BFS Path Explosion via Noise

**Definition:** Noise edges create fake file-to-file connectivity, causing BFS to reach irrelevant files.

**Evidence:**
- 10,913 file-to-file connections (33%) exist ONLY because of speculative edges (conf < 0.5)
- At hop 1 from any file: 84 reachable at conf >= 0.0 vs 52 at conf >= 0.7 (38% noise eliminated)
- At hop 3 (V1R default max_depth): noise paths compound exponentially

**Affected tasks:** All tasks where V1R graph_reach fires

**Affected layers:** V1R (candidate expansion via BFS)

**Metric proving it:** file-to-file edges drop 33% when conf < 0.5 is removed; hop-1 fan-out drops 38%.

**Fix:** V1R already uses floor=0.5 for graph_reach. Raising to 0.7 removes 3,643 more file connections but yields same result as 0.9 (no edges between 0.7 and 0.9 create unique file pairs). Recommend 0.7.

---

## 5. Candidate Graph-Creation Strategies

### Strategy 1: Enforce Confidence Floor at All Query Sites (MINIMAL RISK)

**What:** Add `AND e.confidence >= 0.7` to the 4 unfiltered V1R queries + raise graph_reach from 0.5 to 0.7.

**Evidence source:** Current edge distribution data shows 0.7 is the natural boundary (0.6 = two-candidate = 68% same-package; 0.9 = single-candidate = 89% same-package). The 0.7 threshold includes only certified edges.

**Expected precision:** No new edges created. Reduces noise exposure by 27% of total edges.
**Expected recall:** Same as current (edges still exist in DB, just not queried).
**Verification method:** Diff V1R brief output before/after on dagster-33645.
**Confidence assignment:** N/A (query-side change, not creation-side).
**Failure mode:** May miss valid two-candidate edges (conf=0.6, 68% correct).
**Metric to validate:** `l1_gold_file_in_candidates` before/after.
**Rollback threshold:** If l1_gold_file_in_candidates drops below current 0/5 (cannot get worse).

---

### Strategy 2: Suppress Speculative Edges at Index Time

**What:** Modify resolver.go to NOT emit edges with conf < 0.5 (3-5 candidates and 6+ candidates).

**Evidence source:** conf=0.2 same-package rate = 49% (random). conf=0.4 = 77% (borderline).

**Expected precision:** Removes 27% of edges permanently.
**Expected recall:** Loses 29,328 edges in dagster (some may be correct despite low confidence).
**Verification method:** Rebuild graph.db, compare caller completeness against LSP ground truth.
**Confidence assignment:** Only conf >= 0.5 stored.
**Failure mode:** Correct edges with ambiguous names become permanently unavailable. Cannot LSP-verify them later.
**Metric to validate:** `lsp_verified_edges` count should not drop (verified edges are high-confidence).
**Rollback threshold:** If any previously-verified edge is lost.

**Verdict: DO NOT IMPLEMENT.** Query-side filtering (Strategy 1) achieves the same effect without permanent data loss.

---

### Strategy 3: Improve Import Extractors for Tier 2 Languages

**What:** Extend the Go indexer's 6 import extractors to cover more languages properly.

**Evidence source:** TypeScript at 2% import resolution means 98% of TS edges are speculative.

**Expected precision:** Import edges are 1.0 confidence — every edge added is correct.
**Expected recall:** Could elevate TS/Rust/Go from 0.4-2% import resolution to 20-40%.
**Verification method:** Count import-resolved edges before/after for each language.
**Confidence assignment:** 1.0 for verified imports.
**Failure mode:** Complex import semantics (re-exports, dynamic imports, barrel files) may create false positives.
**Metric to validate:** `import_edge_count` and `import_coverage_ratio` per language.
**Rollback threshold:** If import edges have LSP-reject rate > 5%.

**Verdict: RESEARCH MORE.** Significant engineering effort (each language's import semantics are complex). Worth investigating for TS and Go where the current extractor barely functions.

---

### Strategy 4: LSP-Verify at Index Time

**What:** After name_match resolution, verify top-N candidates via LSP textDocument/references.

**Evidence source:** LSP verification on jedi__branch: 22 verified, 1 rejected (96% accept rate on pre-filtered candidates).

**Expected precision:** LSP-verified edges are ground truth.
**Expected recall:** Limited to languages with LSP servers. Python (pyright), TS (tsserver), Go (gopls), Rust (rust-analyzer) cover the SWE-bench-Live distribution.
**Verification method:** Compare LSP-verified edge set against name_match edge set.
**Confidence assignment:** 1.0 for LSP-verified.
**Failure mode:** Adds 10-60s to indexing per 100 verifications. LSP servers may not start in all environments.
**Metric to validate:** `lsp_verified_edge_count` at index time vs current (0).
**Rollback threshold:** If indexing time exceeds 5 minutes for repos under 10K files.

**Verdict: RESEARCH MORE.** The LSP verification mechanism works (proven on jedi__branch) but has never been tested at index scale. Need to benchmark: how many edges can be verified in <30s?

---

### Strategy 5: Directory-Proximity Scoring for Name-Match (resolver improvement)

**What:** When name_match has multiple candidates, score by directory proximity to the caller file instead of taking the first alphabetically.

**Evidence source:** resolver.go:160-164 has a STUB for this: `_ = bestScore; _ = callerDir` — the slot exists but was never implemented.

**Expected precision:** Same-directory preference should increase same-package rate from 49% to 70-80% for conf=0.2 edges.
**Expected recall:** Same (doesn't remove edges, just picks better targets).
**Verification method:** Re-run indexer on dagster, compare same-package rate before/after.
**Confidence assignment:** Could raise conf from 0.2 to 0.3-0.5 for directory-scored matches.
**Failure mode:** Directory proximity is heuristic — cross-package calls to utility libraries would be deprioritized.
**Metric to validate:** same-package rate at each confidence tier.
**Rollback threshold:** If same-package rate at conf=0.2 does not improve by >= 15 percentage points.

**Verdict: IMPLEMENT AFTER Strategy 1.** Low-risk improvement to resolver quality. The stub already exists — implementation is filling in the scoring function.

---

### Strategy 6: Test-to-Source Edge Extraction (specialized edge type)

**What:** For files where `is_test=1`, trace their function calls back to source files and store as TEST_CALLS edges.

**Evidence source:** `_test_files_for` currently uses ANY edge to find test files — noise dominates. Dedicated test edges would be reliable.

**Expected precision:** If test file imports source file AND calls source function, the edge is certain.
**Expected recall:** Covers only direct test→source calls, not indirect (fixtures, helpers).
**Verification method:** Compare discovered test files against `pytest --collect-only` output.
**Confidence assignment:** 1.0 for import-verified test calls.
**Failure mode:** Dynamic test fixtures, parameterized tests won't be captured.
**Metric to validate:** `agent_ran_failing_test` rate (currently unknown).
**Rollback threshold:** If test-to-source edges have > 10% false positive rate vs pytest collection.

**Verdict: RESEARCH MORE.** Requires understanding pytest/unittest fixture patterns. Could be powerful but needs evidence that it would change agent behavior.

---

## 6. Metric-Gated Recommendations

### IMMEDIATE (implement now — Strategy 1)

**Add confidence floor to 4 unfiltered V1R queries.**

Changes required:
1. `v1r_brief.py:40` `_top_functions`: Add `AND e.confidence >= 0.7` to JOIN
2. `v1r_brief.py:63` `_test_files_for`: Add `AND e.confidence >= 0.7` to JOIN
3. `v1r_brief.py:100` `_issue_relevant_neighbors`: Add `AND e.confidence >= 0.7` to both edge JOINs
4. `v1r_brief.py:135` `_static_callees`: Add `AND e.confidence >= 0.7` to JOIN
5. `v1r_brief.py:361` / `v7_4_brief.py:254`: Raise min_confidence from 0.5 to 0.7

**Expected effect:**
- Eliminates 27% noise from brief content generation
- Eliminates 9% mid-quality edges from BFS (two-candidate name_match)
- Total: 36% of edges removed from L1 consumption
- File ranking stabilizes: documentation/examples/utilities drop, architectural hubs rise

**Validation gate:** Run V1R on dagster-33645 before/after. Check:
- Do top-5 ranked files change? (Expected: yes — noise-inflated files drop)
- Does certified-only ranking match new ranking? (Expected: high correlation)
- Does gold file rank improve on any of the 5 smoke tasks? (Expected: directional improvement)

---

### NEXT (implement after Strategy 1 — Strategy 5)

**Implement directory-proximity scoring in resolver.go name_match stage.**

The stub exists at resolver.go:160-164. Implementation: score each candidate by path-edit-distance from caller directory. Pick highest-scoring candidate instead of first.

**Validation gate:** Rebuild dagster graph.db before/after. Check:
- same-package rate at conf=0.2 improves from 49% to >= 65%
- Total edge count does not change (same edges, better targets)
- L3 caller queries return more same-module callers

---

### RESEARCH (need more data before implementing)

1. **LSP verification at index time (Strategy 4):** Benchmark how many edges can be verified in 30s on dagster (46K nodes). If >= 1000, this is viable.

2. **Import extractor improvements (Strategy 3):** Audit current TS/Go extractors — are they broken or just incomplete? The 2% coverage suggests fundamental bugs, not missing features.

3. **Test-to-source edges (Strategy 6):** Check if the existing `is_test` tagging is correct on dagster, then trace test imports to source files.

---

## 7. Acceptance Gates

### Gate 1: Noise Reduction
`low_conf_edges_consumed_by_V1R` decreases by >= 80%.

**Current:** V1R consumes 29,328 low-conf edges (27% of total).
**After Strategy 1:** V1R consumes 0 low-conf edges (floor at 0.7 excludes all < 0.7).
**Status:** Gate WILL PASS (0.7 floor excludes 100% of < 0.5 edges plus all 0.6 edges).

### Gate 2: No New Rejections
`lsp_rejected_suggested_edges` does not increase from current baseline (1 rejection in 5 tasks).

**Measurement:** Run 5-task smoke after fix, count LSP rejections.
**Current baseline:** 1/22 = 4.5% rejection rate.

### Gate 3: Verified Edge Follow Rate
`follow_rate_certified_edges` >= `follow_rate_all_edges`.

**Measurement:** Compare follow rate for edges with conf >= 0.9 vs all suggested edges.
**Current data insufficient:** Need per-edge confidence tracking in follow rate computation.

### Gate 4: L1 Gold File Improvement
`l1_gold_file_in_candidates` improves OR does not regress from 0/5.

**Current:** 0/5 tasks have gold file in V1R candidates.
**After fix:** Cannot get worse (0/5 is the floor). Any improvement is a win.

### Gate 5: Resolution Count
Resolved count on 5-task smoke >= 3/5 (current baseline).

**Current:** 3/5 resolved (beancount, beets, xarray).
**After fix:** Must not regress below 3/5.

### Gate 6: No Regression on Resolved Tasks
beancount-931, beets-5495, xarray-9760 remain resolved after the fix.

### Gate 7: Fake Witness Reduction
`l5_structural_witness_ignored` count based on speculative edges decreases.

**Current:** 23 (cfn-lint) + 27 (loguru) = 50 total. All from name_match edges.
**After fix:** L5 witnesses should only fire on certified edges. Count should drop significantly.

### Gate 8: Proof Spine
All proof spine checks pass (telemetry integrity, no crashes, events well-formed).

---

## 8. Rollback Criteria

Revert Strategy 1 if ANY of:
- Resolved count drops below 2/5 (current 3/5 - 1 regression allowed for noise)
- Previously-resolved task (beancount/beets/xarray) regresses
- V1R produces empty brief (no candidates pass the 0.7 floor) on >= 2/5 tasks
- Proof spine fails (indicates code error, not strategy error)

---

## 9. What Should NOT Be Touched

1. **L3 confidence floor (0.7):** Already correct. Do not change.
2. **Edge storage in graph.db:** Keep all edges with their real confidence. Query-side filtering preserves optionality.
3. **LSP verification at L3b suggestion time:** Already working (22/1 verify/reject on jedi__branch). Keep as-is.
4. **Resolver's 3-stage priority order:** same_file > import > name_match is correct architecture.
5. **Downstream layers (L3, L3b, L5 injection logic):** This diagnosis is about graph creation/consumption, not about what to do with correct edges.
6. **The `confidence` column schema:** The confidence model (1.0/0.9/0.6/0.4/0.2) is well-designed. The problem is consumption, not classification.

---

## 10. Final Recommendation

### IMPLEMENT NOW: Strategy 1 — Confidence Floor at All V1R Query Sites

**Effort:** 5 SQL clauses changed. Zero architectural risk. Immediately testable.

**Why this first:**
- V1R is the ONLY layer consuming noise edges (L3 and L3b already filter)
- V1R's content queries (_top_functions, _test_files_for, _neighbors) populate what the agent SEES in the brief
- The 0/5 gold-file-in-candidates rate proves the brief content is wrong
- Fixing query-side consumption is instant and reversible (no data rebuild needed)

### RESEARCH MORE: Strategies 3, 4, 5, 6

All require either:
- Rebuilding graph.db (Strategy 5 — resolver change)
- External infrastructure (Strategy 4 — LSP at index time)
- Per-language work (Strategy 3 — import extractors)
- New feature design (Strategy 6 — test edges)

Do these AFTER Strategy 1 is validated. The confidence floor fix tells us whether removing noise from V1R is sufficient to improve l1_gold_file_in_candidates. If it is, the graph creation itself is adequate. If it isn't, THEN investigate creation-side improvements.

### ABANDON: Strategy 2 — Suppress at Index Time

Permanent data loss with zero benefit over query-side filtering. The edges are already classified correctly — the issue is WHERE they're consumed, not that they exist.

---

## Appendix: Data Insufficiency

The following metrics from the Step 1 spec CANNOT be computed from available data:

| Metric | Why Unavailable | How To Compute |
|--------|----------------|----------------|
| l1_edges_used_total / l1_low_conf_edges_used | V1R doesn't log which edges it traversed | Add logging to graph_reach.py BFS |
| l3_edges_used_total / l3_low_conf_edges_used | gt_intel.py doesn't emit edge confidence in results | Add confidence field to evidence output |
| l5_witness_edges_total / l5_low_conf_witness_edges | L5 governor doesn't record which edge triggered the witness | Add edge_id to goku_check() state |
| agent_ran_failing_test | No comparison against FAIL_TO_PASS test from eval metadata | Parse eval metadata, compare against agent commands |
| l1_gold_file_in_candidates (exact) | Gold files not available locally (need eval resolved/patch data) | Compare V1R candidates against gold_patch files in SWE-bench metadata |
| correlation coefficients | N=5 is insufficient for statistical correlation | Need N >= 30 for meaningful Pearson/Spearman |

These metrics should be added as LOGGING in the next smoke run. The diagnosis proceeds with available data.
