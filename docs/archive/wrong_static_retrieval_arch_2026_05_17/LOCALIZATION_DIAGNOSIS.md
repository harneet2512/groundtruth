# LOCALIZATION_DIAGNOSIS.md — 5-Task Blocker Set Analysis

Generated: 2026-05-17  
Source: GHA run 25982583554 artifacts (commit e1289b36)  
Branch: `general_start`

---

## Summary Table

| Task | Gold File | L1 Ranked Candidates | Gold in L1 Brief? | Gold as Neighbor? | L3b Compensated? | Resolved |
|------|-----------|---------------------|-------------------|-------------------|-----------------|----------|
| beancount-931 | `plugins/leafonly.py` | `parser/options.py`, `scripts/example.py` | NO | NOT PROVEN | YES (iter 5) | YES |
| beets-5495 | `importer.py` | EMPTY (0 candidates) | NO | N/A | YES (iter 1 nav) | NO |
| loguru-1297 | `_datetime.py` | `_logger.py`, `_file_sink.py` | NO | YES (Calls: line) | YES (L4 prefetch) | NO |
| loguru-1306 | `_colorama.py` | `_logger.py`, `_colorizer.py` | NO | YES (L3b iter 3) | YES | NO |
| weasyprint-2300 | `layout/block.py` | `layout/flex.py`, `test_table.py`, `boxes.py` | NO | YES (caller edge) | YES (caller in brief) | NO |

**L1 hit@5: 0/5 (0%)**  
**L3b/neighbor compensation: 4/5 tasks show gold as 1-hop neighbor**

---

## Task 1: beancount__beancount-931

**Gold files:** `beancount/plugins/leafonly.py`

**L1 candidates ranked:**
1. `beancount/parser/options.py`
2. `beancount/scripts/example.py`

**Where gold was lost:**
- `leafonly.py` is a small plugin file with few graph edges
- Graph expansion from anchors reaches `parser/` and `scripts/` (high-degree hubs) but not `plugins/leafonly.py`
- BM25: the issue text discusses "leaf-only" semantics — `leafonly.py` should score high on path-name prior ("leafonly" matches "leaf" + "only" in issue)
- **ROOT CAUSE:** Path-name prior requires 4-char minimum words. "leaf" = 4 chars, "only" = 4 chars. Bidirectional: "leaf" in "leafonly" → should match at 0.7. BUT the path rescue adds to candidate set AND the scoring boost applies — so why didn't it rank?
- **HYPOTHESIS:** leafonly.py may not have been in graph.db nodes table (gt-index may not have indexed it if it has no functions). NEEDS VERIFICATION.

**L3b compensation:** YES — L3b navigation event at iteration 5 showed graph connections that included leafonly.py's neighborhood. Agent resolved the task.

**Classification:** CANDIDATE_GENERATION_MISS (gold not in candidate pool) OR GRAPH_MISS (sparse edges prevent expansion to gold)

---

## Task 2: beetbox__beets-5495

**Gold files:** `beets/importer.py`, `docs/changelog.rst`

**L1 candidates ranked:** NONE — brief suppressed entirely.

**Where gold was lost:**
- Run summary shows 0 L1 candidates produced
- Suppression reason: "GT could not rank files with high confidence (0 candidates from graph)"
- **ROOT CAUSE:** Graph density check in `v1r_brief.py:619-629` — if edges_per_file < 2.0, switches to BM25-only weights. But then the modulus gate at :711-747 likely suppressed the brief because top candidates were all hubs.
- `beets/importer.py` is a massive file (hub) with many incoming edges — it would score as a hub and trigger suppression.
- **ALTERNATIVE:** Graph.db for beets may have been too sparse (< 2 edges/file) → BM25-only mode → modulus gate killed it.

**L3b compensation:** YES — agent navigated to `importer.py` on its own at iteration 1. L3 post_edit_contract fired when it edited there.

**Classification:** BRIEF_SUPPRESSED (modulus gate or density gate killed the brief entirely)

---

## Task 3: delgan__loguru-1297

**Gold files:** `loguru/_datetime.py`, `CHANGELOG.rst`

**L1 candidates ranked:**
1. `loguru/_logger.py`
2. `loguru/_file_sink.py`

**Where gold was lost:**
- `_datetime.py` appears in brief metadata (Calls: line) but NOT as a ranked candidate
- This means _datetime.py was in the candidate set but scored below position 2 (or was filtered by adaptive K)
- `_logger.py` is the hub file (most-referenced in loguru) — high BM25 + high graph reach
- `_file_sink.py` directly references datetime handling — high BM25 for issue keywords
- **ROOT CAUSE:** `_datetime.py` has low BM25 score (small utility file, few issue keywords in content) and low graph reach (leaf node, only called by _file_sink.py). W_LEX=0.50 + W_PATH=0.45 dominate, and _datetime.py has neither strong lexical match nor strong path match to issue text.
- Path match: "datetime" in issue text, "datetime" in filename → should score 0.7 on path prior. But _logger.py and _file_sink.py score even higher on W_LEX.

**L3b compensation:** YES — L4 prefetch and L6 reindex both fired on `_datetime.py`. Agent eventually navigated there.

**Classification:** SCORE_DOMINATED (gold in candidate set but outranked by hub + BM25-dominant files)

---

## Task 4: delgan__loguru-1306

**Gold files:** `loguru/_colorama.py`

**L1 candidates ranked:**
1. `loguru/_logger.py`
2. `loguru/_colorizer.py`

**Where gold was lost:**
- `_colorizer.py` (rank 2) is NOT `_colorama.py` (gold) — similar names, different files
- Issue discusses color handling — `_colorizer.py` has high BM25 for color keywords
- `_colorama.py` is a thin wrapper around the `colorama` library — minimal content, few issue keywords
- **ROOT CAUSE:** BM25 content match for `_colorizer.py` >> `_colorama.py`. The colorizer handles color logic (many color terms), the colorama wrapper just imports and re-exports.
- Path prior: "colorama" in issue → "colorama" in `_colorama.py` filename → should score 0.7. But W_LEX dominates.
- `_colorama.py` is a 20-line file with almost no keyword content — BM25 score ≈ 0.

**L3b compensation:** YES — L3b navigation at iteration 3 correctly showed `_colorama.py` as a callee of `_colorizer.py`. Agent navigated there.

**Classification:** SCORE_DOMINATED (gold has high path-name match but near-zero BM25, outranked by content-rich neighbors)

---

## Task 5: kozea__weasyprint-2300

**Gold files:** `weasyprint/layout/block.py`

**L1 candidates ranked:**
1. `weasyprint/layout/flex.py`
2. `tests/layout/test_table.py`
3. `weasyprint/formatting_structure/boxes.py`

**Where gold was lost:**
- `block.py` appears as a CALLER of `flex.py` in the brief text: `"Callers: weasyprint/layout/block.py:82 result = flex_layout("`
- This proves `block.py` is in the candidate set (it's a graph-expanded neighbor of flex.py)
- But `block.py` scored below position 3 in the final ranking
- `flex.py` scores higher because the issue discusses flex layout behavior
- `block.py` is a general layout file — BM25 for the specific issue keywords is lower
- **ROOT CAUSE:** Issue mentions "flex" behavior triggering a block layout bug. The primary symptom file (flex.py) ranks higher than the actual fix file (block.py) because BM25 matches the symptom description, not the fix location.
- Path prior: "block" appears in issue? If issue discusses "block context" then path prior would fire. Likely not matching because issue describes flex-specific behavior.

**L3b compensation:** YES — The brief ITSELF shows block.py as a caller. Agent would see this edge. L3b also showed block.py via navigation.

**Classification:** SYMPTOM_VS_FIX (BM25 ranks symptom-described file over fix-target file)

---

## Failure Taxonomy

| Class | Count | Description |
|-------|-------|-------------|
| SCORE_DOMINATED | 2 | Gold in candidate set but outranked by W_LEX-dominant files |
| SYMPTOM_VS_FIX | 1 | BM25 matches symptom description, not fix location |
| BRIEF_SUPPRESSED | 1 | Modulus/density gate killed entire brief |
| CANDIDATE_GENERATION_MISS | 1 | Gold possibly not in candidate pool at all |

---

## Key Insights for Implementation (Step 4)

1. **Gold is a 1-hop neighbor in 4/5 tasks** — The ranking engine puts ADJACENT files (callers/callees of gold) at the top instead of gold itself. This is actually useful for GT-agent collaboration: the brief seeds the neighborhood, and the agent + L3b find the actual gold via navigation.

2. **BM25 dominance (W_LEX=0.50) ranks symptom files over fix files** — Issue text describes the BUG (symptoms), not the FIX (location). BM25 naturally matches symptom descriptions → wrong file. This is a fundamental limitation of content-based retrieval for bug localization.

3. **Path-name prior (W_PATH=0.45) helps but can't overcome BM25 for small files** — `_colorama.py` has path match 0.7 but zero BM25, so total score ≈ 0.45 * 0.7 = 0.315. Meanwhile `_colorizer.py` has BM25 ≈ 0.8 → total ≈ 0.50 * 0.8 = 0.40 from BM25 alone.

4. **Brief suppression on beets is a bug** — A task with a clear gold file should never produce an empty brief. The modulus gate or density gate is overly aggressive.

5. **L3b navigation compensates for L1 miss in 4/5 cases** — The collaboration model works: L1 gets the neighborhood, L3b guides the agent the last hop. The question is whether this collaboration is FASTER than baseline (measured by first_gold_view_step).

---

## Recommended Fixes (Priority Order)

1. **Fix brief suppression** — beets-5495 should NEVER produce an empty brief. Disable modulus gate when BM25-only mode is active (density < 2 edges/file) since hub detection requires graph data.

2. **Guarantee path-match candidates survive into brief** — If a file has path_score > 0.5 (strong name match), it should not be pushed below position 5 regardless of BM25 competition. This fixes loguru-1306 (colorama) and potentially loguru-1297 (datetime).

3. **Add callee expansion to brief candidates** — In weasyprint-2300, block.py is already known as a CALLER of ranked flex.py. If gold is 1-hop from rank-1, including top callees/callers in the final brief would capture it.

4. **Do NOT reduce W_LEX** — BM25 is the strongest single signal for getting NEAR the gold file. The problem is not that BM25 is too strong, but that it finds symptom files. Fix by adding diversity (ensure path-match files survive) rather than reducing BM25.

5. **Consider reporting 1-hop neighbors as "also relevant"** — The brief already shows "Calls:" and "Callers:" lines. These ALREADY contain gold in 3/5 tasks. The metrics (hit@K) should potentially account for this.
