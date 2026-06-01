# Session Summary — 2026-06-01

## Branch: gt-consensus-curation
## Commits: 8a9b058c → 1e047a1c (20+ commits)

## What was built

### ONE PIPELINE (FTS5 → Graph BFS → LSP enrichment → Curated Brief)

1. **FTS5 retrieval** — BM25 ranking over function names/signatures/paths in graph.db
   - Go indexer creates `nodes_fts` virtual table (non-fatal if SQLite lacks FTS5)
   - Python-side fallback creates FTS5 at brief time using writable connection
   - Shared DDL constants (`_FTS5_CREATE`, `_FTS5_POPULATE`)

2. **Graph BFS with confidence-weighted path decay** — KGCompass formula
   - `S(f) = 0.85^L(f)` where L = sum(1/confidence) along shortest path
   - Dynamic hop depth: sparse graphs get 3 hops, dense get 2
   - Dynamic confidence floor: high-quality graphs use 0.6, rest use 0.5

3. **Grep-to-seed** — subsumes grep's recall
   - Runs ripgrep for issue tokens, maps hits to graph nodes
   - Dynamic hybrid quality gate (3 signals: diversity, coverage, confidence)
   - Falls back to Python os.walk when rg unavailable

4. **Scope chains** — connected file subgraphs from graph edges
   - Groups top candidates into edit-scope chains
   - Renders "Scope chain (graph-connected, check ALL): A → B → C"

5. **LSP type enrichment** — in the same session as edge verification
   - Queries textDocument/hover on top-50 nodes
   - Stores return types in nodes.return_type
   - Language-filtered (only enriches nodes matching current language)
   - WAL mode + busy_timeout on all connections

6. **Verified candidate guarantee** — graph-witnessed candidates always rendered
   - Even if v7_4 BM25 ranks them below MAX_FILES

7. **Prepend-first delivery** — all GT evidence prepended, not appended
   - Research: Lost-in-the-Middle (TACL 2024), 30% accuracy gain
   - Cap raised from 600 to 2000 chars

### Bugs fixed (from trajectory audit + LIPI reviews)

| Bug | Avenue | Fix |
|-----|--------|-----|
| schema_version not stamped in incremental path | Integration | SetMeta calls after commit |
| L3b host-side fallback unreachable (router_v2) | Integration | Moved inside router_v2 block |
| L3 post-edit --root wrong path | Plumbing | Changed to /tmp/testbed_src |
| FTS5 crashes entire schema creation | Implementation | Separated from main schema |
| Connection leak on early returns | Implementation | Close in all paths |
| FTS5 writable conn invisible to read-only | Plumbing | Use writable conn for query too |
| Go multi-return type parse | Logic | Last balanced paren, not first |
| Score inflation (1.15 → 1.80) | Logic | Normalize by weight sum |
| Rescue fires too easily (Signal 7 × 2) | Logic | Reduced to +1 |
| Assertion path matching inconsistent | Integration | LIKE with basename everywhere |
| OR-JOIN cross product | Implementation | UNION ALL with SUM |
| Test files ranked #1 | Logic | Language-agnostic test filter |
| .json gold files blocked | Logic | Removed from _NON_SOURCE_EXTS |
| Duplicate observations | Integration | Dedup in router_v2 path |
| VERIFY section empty (target_node_id=0) | Plumbing | Edge-join fallback query |
| Assertions sorted longest-first | Logic | Changed to ASC |
| gt-scope leaking internals | Implementation | Translation dict for resolution_method |
| "(unverified)" jargon | Logic + Integration | Removed from all render paths |
| "returns None" uninformative | Implementation | Suppressed in contract_map |
| resolve.py LIKE '%basename' DELETE | Logic | Exact file_path match |
| No WAL mode in resolve.py | Plumbing | Added PRAGMA journal_mode=WAL |
| Enrichment sends wrong-language files | Integration | WHERE n.language=? filter |
| prepend_observation 600-char cap | Implementation | Raised to 2000 |
| Rust impl Trait for Struct parenting | Logic | Read tree-sitter type field |
| impl_method not in strong methods | Plumbing | Added to _STRONG_RESOLUTION_METHODS |

### Rust indexer improvements

1. `linkRustImplMethods` — parents methods to struct, not trait
2. Strategy 1.94 — single/few-implementor method resolution
3. Self:: handling — extended Strategy 1.75

### Framework additions

- **LIPI** — mandatory 4-avenue bug diagnosis (Logic, Implementation, Integration, Plumbing)
- **ONE PRODUCT rule** — GT is one pipeline, never fragment
- **Preflight checks** — 8 checks covering every GT layer (scripts/verify/preflight_pipeline.py)
- **Post-run checks in canary** — GT_FATAL, SchemaMismatch, L3b deliveries, test assertions

## Live proof

| Task | Before | After |
|------|--------|-------|
| weasyprint-2300 (codespace) | Agent: flex.py (WRONG) | Agent: block.py (GOLD) |
| flexget-4244 (codespace) | Agent: SRT files (WRONG) | Agent: viewed next_series_episodes.py (GOLD) |
| gin-gonic__gin-1805 (codespace, Go) | All layers at Python parity | Confirmed |
| loguru-1297 (GHA) | 0 jargon, 0 crashes | Confirmed |

## 30-task GHA results (commit bdd9ede5, before final LIPI fixes)

- 2/27 resolved, 1 flip, 0 regressions (was 1 regression before)
- 8/14 failures = post-localization wrong logic (hidden test gated)

## Canary setup

### Current workflow: `.github/workflows/canary_3arm.yml`
- Inputs: gt_commit, task_ids (comma-separated), arm, max_iterations
- Env: DEEPSEEK_API_KEY from secrets
- Post-run checks: GT_FATAL, SchemaMismatch, L3b deliveries, test assertions, consensus

### To scale to 300-task SWE-Live Lite

1. **Workflow**: Use `swebench_30task.yml` or create `swebench_300task.yml`
   - Matrix strategy with all 300 task IDs
   - Same env vars, same GT flags, same post-run checks
   - Add preflight_pipeline.py run BEFORE agent launch
   - Add trajectory download + Opus reader AFTER each task

2. **Preflight checks per task** (before agent starts):
   - graph_exists: nodes > 0, edges > 0
   - schema_version: project_meta stamped
   - fts5: nodes_fts available
   - edge_quality: verified edge %, resolution methods
   - assertions: table populated
   - brief_generation: produces candidates

3. **Post-run checks per task** (after agent finishes):
   - GT_FATAL = 0
   - SchemaMismatch = 0
   - L3b deliveries > 0
   - No jargon leaks
   - Prepend position (GT at FRONT of observations)

4. **Trajectory analysis** (after eval):
   - Download output.jsonl per task
   - Parse with Python (NOT grep)
   - Check every GT observation from agent's perspective
   - Report: localization HIT/MISS, gold_hit, evidence quality

## Next steps

1. **1-task canary** (weasyprint-2300) — running on GHA with commit 1e047a1c
2. **If passes** → 30-task canary with same commit
3. **Goal 2** — TS/JS/Rust verification on codespace (Goal 2 agent running)
4. **Goal 3** — DeepSWE port using the new pipeline architecture
5. **Rust quality** — verify 61% → 75-82% with indexer fixes on real graphs
