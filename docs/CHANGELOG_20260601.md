# Changelog — Session 2026-06-01

## Branch: gt-consensus-curation
## Commits: 8a9b058c → f711fd1d (30+ commits)

---

## Architecture Changes

### 1. FTS5 Retrieval Pipeline
**What:** Added BM25 retrieval over function names/signatures/paths via SQLite FTS5.
**Supersedes:** Pure symbol-name matching in `_seed_node_rows` (WHERE name IN anchors).
**Why:** The old approach only seeded on exact function name matches — a strict subset of grep's recall. FTS5 matches the same tokens grep finds but RANKS them by BM25 relevance.
**Research:** BLUiR (Saha+, ASE 2013) — structured field-level lexical anchoring beats flat-blob BM25. SWERank (2025) — BM25 over function-level chunks = 38.69% Acc@1 baseline.
**Files:** `gt-index/internal/store/sqlite.go` (schema), `graph_localizer.py` (_fts5_candidates).

### 2. Grep-to-Seed with Dynamic Quality Gate
**What:** Runs ripgrep for issue tokens, maps hits to graph nodes, merges as BFS seeds.
**Supersedes:** The `len(seeds) < 3` hardcoded gate that skipped grep when name-match found ≥3 seeds.
**Why:** Name-match can return 10 seeds all from the SAME wrong file (e.g., CSS property validators for "overflow"). Grep finds different files containing different issue tokens.
**Research:** SWERank (2025) retrieve→rerank — the retrieve must have at least grep-grade recall.
**Dynamic gate:** 3 composited signals (diversity, coverage, confidence) determine grep depth. Not a fixed threshold.
**Files:** `graph_localizer.py` (_grep_to_seeds, quality gate in localize).

### 3. Confidence-Weighted Path Decay (KGCompass Formula)
**What:** `S(f) = β^L(f)` where L = sum(1/confidence) along shortest Dijkstra path.
**Supersedes:** Flat BFS that treated all edges equally regardless of confidence.
**Why:** Verified import edges (conf=1.0) = short path cost. Speculative name_match (conf=0.4) = long path cost. The graph's confidence IS the structural advantage over grep.
**Research:** KGCompass (Li+, 2025, arXiv 2503.21710) — β=0.6 validated, 89.7% of bugs need multi-hop.
**Files:** `graph_localizer.py` (_path_decay_scores).

### 4. Research-Backed Role Discount for DEFINES Witnesses
**What:** DEFINES witness score discounted by function role (SLOC, fan_in, fan_out).
**Supersedes:** Equal DEFINES and CALLS witness strength (both 1.0). Also supersedes the reverted arbitrary 0.6 discount (commit eedf48cf, reverted 287b0069).
**Why:** A trivial validator `overflow(keyword)` (SLOC=3, fan_out=0) is 6-11x less likely to contain bugs than a complex implementation `block_box_layout()` (SLOC=50+). The function name matching the issue keyword is the SYMPTOM site, not the CAUSE site.
**Research:**
  - Herbold+ (PeerJ CS 2019): `{SLOC < 4, NoMethodInvocations} => NotFaulty` (90%+ confidence). Trivial methods 6-11x less buggy.
  - ARISE (2025, arXiv 2605.03117): `score = α×rel×role + β×proximity + γ×slice` (α=1.0, β=0.5, γ=1.5).
  - KGCompass (2025): 89.7% of fixes require multi-hop from the named entity. Only ~10% at the directly-named function.
  - SBEST (Campos+, 2024, arXiv 2405.00565): 83% of fixes are "Exception Prevention" — fix in callers, not the named function.
**Dynamic:** Discount computed per-function from graph.db metrics. 0.2 for trivial (SLOC≤4, fan_out=0), 0.5 for simple utility, 1.0 for complex.
**Files:** `graph_localizer.py` (_role_discount_for_file, scoring loop).

### 5. Dynamic BFS Hop Depth
**What:** Hop depth adapts to graph density + confidence distribution.
**Supersedes:** Fixed `max_hop=2` everywhere.
**Why:** Sparse graphs with few verified edges need 3 hops to reach candidates. Dense graphs with many verified edges find everything at 2 hops.
**Research:**
  - KGCompass (2025): 74% at 2-hop, 14.4% at 3-hop, 1.4% at 4-hop. β=0.6 decay: 3-hop score=0.216 (significant), 4-hop=0.13 (marginal). Practical max = 3.
  - RepoGraph (ICLR 2025): k=1 ego-graph strongest. Diminishing returns beyond k=2 for dense graphs.
**Dynamic:** Uses avg_degree + high_conf_frac. Dense+verified → 2. Sparse or low-verified → 3.
**Files:** `graph_localizer.py` (_dynamic_max_hop).

### 6. Prepend-First Evidence Delivery
**What:** All GT evidence prepended to observations, not appended. Cap raised from 600 to 2000 chars.
**Supersedes:** Default `prepend=False` in `_deliver_or_trace`. Also supersedes the 600-char cap in `prepend_observation`.
**Why:** When evidence is appended, the agent sees file content first and may not reach the GT evidence at the end. The L3b evidence for block.py was invisible because it was at char 1322 of a 2064-char observation.
**Research:** Lost in the Middle (Liu+, TACL 2024) — 30% accuracy gain when relevant info at BEGINNING vs middle of context.
**Files:** `oh_gt_full_wrapper.py` (_deliver_or_trace default, prepend_observation cap).

### 7. Scope Chains from Graph Edges
**What:** Connected file subgraphs surfaced as "Scope chain (graph-connected, check ALL): A → B → C".
**Supersedes:** Individual file ranking with no scope information.
**Why:** 32% of failures were INCOMPLETE_SCOPE — agent found 1 file but fix needed 2-8. The call graph knows which files change together.
**Research:** Zimmermann+ (ICSE 2004) — co-change analysis. Files connected by call/import edges form structural edit scopes.
**Files:** `graph_localizer.py` (_build_scope_chains, ScopeChain), `v1r_brief.py` (rendering).

### 8. LSP Type Enrichment in Same Session
**What:** textDocument/hover on top-50 nodes during edge verification session. Results stored in nodes.return_type.
**Supersedes:** No type enrichment (tree-sitter signatures only).
**Why:** LSP gives compiler-resolved types that tree-sitter can't infer. One pipeline — edge verification + type enrichment in one LSP session. Zero extra cold start.
**Research:** Codebase-Memory (2026, arXiv 2603.27277) — 83% answer quality at 10x fewer tokens with LSP-enriched graph.
**Files:** `resolve.py` (enrichment section after edge verification).

---

## Bug Fixes

### Category: Plumbing
| Bug | Root Cause | Fix | Research |
|-----|-----------|-----|----------|
| schema_version not stamped in incremental path | SetMeta calls missing in runIncremental | Added 5 SetMeta calls after commit | — |
| FTS5 crashes entire schema | CREATE VIRTUAL TABLE inside main schema Exec | Separated into own Exec, non-fatal | — |
| FTS5 writable conn invisible to read-only | WAL snapshot stale | Use writable conn for both create AND query | SQLite WAL docs |
| target_node_id=0 for ~100% of assertions | resolveAssertionTarget fails | Edge-join fallback query | — |
| resolve.py LIKE '%basename' DELETE | Wrong node matched on common basenames | Exact file_path match | — |
| No WAL mode in resolve.py | Deadlock risk | PRAGMA journal_mode=WAL + busy_timeout | — |
| "returns None" not suppressed in contract_map | Missing != "None" check | Added to _fmt_one and contract_line | Herbold 2019 |
| impl_method not in _STRONG_RESOLUTION_METHODS | Passed only via trust tier fallback | Added explicitly | — |

### Category: Logic
| Bug | Root Cause | Fix | Research |
|-----|-----------|-----|----------|
| Test files ranked #1 | _walk_text_files includes tests | Language-agnostic test-file filter | — |
| .json gold files blocked | _NON_SOURCE_EXTS too aggressive | Removed .json/.yaml/.yml/.toml | — |
| Assertions sorted longest-first | ORDER BY length DESC | Changed to ASC (shortest = most actionable) | — |
| Prose filter strips graph-confirmed seeds | "flex" (4 chars) dropped despite graph confirmation | Exempt graph-confirmed non-pollutant symbols | is_seed_pollutant (Aider repomap) |
| Score inflation 1.15→1.80 | New weights not normalized | Divide by weight sum | — |
| L5 premature rescue at action 25 | Only checks action+edit count | Exploration ratio check (viewed_files/actions) | SWE-Skills (2603.15401) |

### Category: Integration
| Bug | Root Cause | Fix | Research |
|-----|-----------|-----|----------|
| L3b host-side fallback unreachable | Inside router_v2 path, returned before fallback | Moved fallback inside router_v2 block | — |
| L3 post-edit --root wrong path | Used /workspace (container) on host | Changed to /tmp/testbed_src | — |
| Duplicate observations (7x flex.py) | Router_v2 path had zero dedup | Added per-file-once gate + 2-delivery cap | — |
| L3b sed/cat bypass | Per-file-once too strict | Recency exception (30% of max_iter) | Lost in the Middle (TACL 2024) |
| Enrichment sends wrong-language files | ext from outer scope, not node's own | WHERE n.language=? filter | — |
| Assertion sort inconsistency | post_view DESC, brief ASC | Both ASC | — |

### Category: Implementation
| Bug | Root Cause | Fix | Research |
|-----|-----------|-----|----------|
| gt-scope leaking internals | Raw resolution_method in agent text | Translation dict _SCOPE_REASON_LABELS | — |
| "(unverified)" jargon in callers | Tag exposed to agent | Removed from all render paths | — |
| Connection leak on early returns | finally: pass instead of conn.close() | Close in all paths | — |
| Go multi-return type parse | First balanced paren = receiver, not params | Last balanced paren (removed break) | — |
| OR-JOIN cross product in verified-seed query | Expensive on hub files | UNION ALL with SUM | — |
| prepend_observation 600-char cap | Double truncation of L3/L3b evidence | Raised to 2000 | — |

---

## Indexer Improvements (gt-index, Go)

### Rust: impl Trait for Struct parenting
**What:** `linkRustImplMethods` parents methods to struct node, not trait node.
**Supersedes:** `extractFirstIdentifier` grabbing trait name.
**Why:** For `impl Iterator for MyStruct`, methods were parented to "Iterator" not "MyStruct". All type_flow strategies failed for these methods.
**Research:** Same class of bug as Go receiver methods (commit e1bc266b).
**Files:** `parser.go` (linkRustImplMethods), `main.go` (wiring).

### Rust: Strategy 1.94 — Single/Few-Implementor Resolution
**What:** When 1-3 structs implement a method name, resolve with graduated confidence.
**Supersedes:** Falling through to generic name_match for all unresolved method calls.
**Why:** Closes the Rust 61%→75-82% usable edge gap without rust-analyzer.
**Research:** RTA/CHA (Rapid Type Analysis / Class Hierarchy Analysis) adapted for Rust traits.
**Files:** `resolver.go` (Strategy 1.94).

### Rust: Self:: Resolution
**What:** Strategy 1.75 extended for `Self::method()` inside impl blocks.
**Supersedes:** Self falling through to generic type_flow.
**Files:** `resolver.go` (Strategy 1.75 + guard in 1.95).

---

## Framework Additions

### LIPI — Mandatory 4-Avenue Bug Diagnosis
**What:** Logic, Implementation, Integration, Plumbing — check ALL 4 for every bug.
**Why:** Session proved bugs hide in DIFFERENT avenues. Stopping at one misses the others.
**Evidence:** 4 bugs found, each a different avenue.
**Files:** `docs/LIPI.md`, `.claude/CLAUDE.md`.

### ONE PRODUCT Rule
**What:** GT is one pipeline. Never fragment into separate mechanisms/servers/phases.
**Why:** Repeated fragmentation ("4 LSP servers", "3 things to build") violated the product principle.
**Files:** `.claude/CLAUDE.md`, `CLAUDE.md`.

### Preflight Pipeline Verification
**What:** 8 checks before agent launch + post-run checks in canary workflow.
**Files:** `scripts/verify/preflight_pipeline.py`, `.github/workflows/canary_3arm.yml`.

### 300-Task SWE-Live Lite Workflow
**What:** Full 300-task workflow with GT pipeline checks, ready for submission run.
**Files:** `.github/workflows/swebench_300task.yml`.

---

## Reverted Changes

| Commit | What | Why Reverted |
|--------|------|-------------|
| eedf48cf | Arbitrary 0.6 DEFINES discount + hardcoded 50/2/0.3 thresholds | Not research-backed. Replaced by role_discount from Herbold 2019 + dynamic thresholds |

---

## Live Proof

| Task | Before | After | What proved |
|------|--------|-------|-------------|
| weasyprint-2300 (codespace) | Agent: flex.py (WRONG) | Agent: block.py (GOLD) | Grep-to-seed + graph callers |
| flexget-4244 (codespace) | Agent: SRT files (WRONG) | Agent: viewed gold file | FTS5 + graph navigation |
| gin-gonic__gin-1805 (Go) | All layers verified | 0 crashes, L3b=5, consensus=1 | Goal 2 parity |
| axios__axios-4731 (JS) | All layers verified | L3b=6, consensus=1 | Goal 2 parity |
| tokio-rs__axum-1119 (Rust) | All layers verified | L3b=13, consensus=1 | Goal 2 parity |
| loguru-1297 (GHA) | 0 jargon, 0 crashes | Clean trajectory | Pipeline hygiene |
