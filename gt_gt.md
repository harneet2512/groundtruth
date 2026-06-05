# gt_gt.md — GroundTruth Architecture (GT itself, layer by layer)

> The single master reference for **GroundTruth's own architecture** — what it
> builds, what it collects from a graph, how it scores and delivers, what is
> hardcoded (and why), what is dynamic, what is enforced, and what is missing.
>
> **Scope rule:** this documents GT *only*. The agent harness (OpenHands /
> mini-swe-agent) appears only as "the surface GT hooks evidence/tools onto," at
> the overall level — never harness-specific plumbing. Branch
> `gt-consensus-curation`. Last verified 2026-06-03 (direct code reads).
>
> Supersedes the architecture content scattered across `DOC_OF_HONOR.md`,
> `we_did.md`, and `BRIEFING.md` (now legacy/feeders; BRIEFING stays the deeper
> localization scratchpad).

---

## 1. What GT is — ONE pipeline

GT is a deterministic, LLM-free codebase-intelligence layer. It indexes source into
a SQLite graph, then turns an issue into curated evidence the agent reads at the
moment it edits. One pipeline, never separate mechanisms:

```
Issue text
  → FTS5 retrieval            (lexical recall spine)
  → graph traversal           (BFS + path-decay over CALLS/IMPORTS)
  → LSP enrichment            (promote name_match → verified edges)
  → composite scoring         (grep + structural + semantic, RRF-fused)
  → curated brief             (confidence-gated, hub-demoted, abstain-or-quiet)
  → hooked onto the agent     (brief + per-view/per-edit/per-turn evidence)
```

FTS5, graph, LSP, and semantic are **capabilities of this one pipeline**, not four
products. The core stays LLM-free; the only learned component is a small ONNX
embedder used for semantic *retrieval* (§5), never for generation or ranking-as-LLM.

---

## 2. Layer 0 — The graph base: what we collect and how much

GT's edge is the **graph**. This is everything we extract from a repo.

### 2.1 The build — 5 passes + 6 sub-passes (`gt-index`, Go, tree-sitter)
| Pass | Produces |
|---|---|
| 1 STRUCTURE | discover source files by language |
| 2 DEFINITIONS+IMPORTS | parse (NumCPU workers) → nodes; collect calls/imports/properties/assertions; then **PopulateFTS5()** |
| 3 CALLS | 11-rung resolver → `CALLS` edges; emit `CONTAINS` from parent_id |
| 4 PROPERTIES+ASSERTIONS | insert properties; resolve assertion→tested-function (TCTracer) |
| 4b API EDGES | cross-service route matching (`HANDLES_ROUTE`/api) |
| 4c RELATIONSHIPS | `EXTENDS/IMPLEMENTS/COMPOSES/RE_EXPORTS/HANDLES_ROUTE` (regex, per-language) |
| 4d SERDE+TWINS | `serialization_pair`, `structural_twin` (as *properties*) |
| 4e CLOSURE | transitive-closure sidecar over VERIFIED CALLS, depth ≤3 |
| 5 EXTRAS | `project_meta` (schema_version, counts, git_commit, min_confidence…) |
| 5b FILE HASHES | SHA-256 per file → incremental reindex support |
| 5c CO-CHANGE | git-log mining → `cochanges` pairs |

Plus an incremental single-file mode (`-file`) for the runtime reindex, and a
`-rebuild-closure` mode (run **after** the LSP pass so the closure reflects promoted edges).

### 2.2 graph.db schema — 9 tables
`nodes` (label, name, qualified_name, file, start/end_line, signature, return_type,
is_exported, is_test, language, parent_id) · `edges` (source/target, type, source_line,
resolution_method, confidence, **trust_tier**, **candidate_count**, **evidence_type**,
**verification_status**, metadata) · `properties` (node_id, kind, value, line, confidence) ·
`assertions` (test_node_id, target_node_id, resolution_score, kind, expression, expected) ·
`cochanges` (file_a, file_b, count) · `closure` (source, target, depth, min_confidence) ·
`file_hashes` · `project_meta` · `nodes_fts` (FTS5 virtual table over name/qname/signature/path).

### 2.3 Edge types we collect (+ trust model)
| Edge | Languages | Confidence model |
|---|---|---|
| **CALLS** | all | 0.2–1.0 (per the 11-rung resolver) |
| **CONTAINS** | all (structural) | 1.0, CERTIFIED |
| **EXTENDS** | py/js/ts/java/kotlin/go/rust | 1.0 |
| **IMPLEMENTS** | js/ts/java/kotlin/go/rust | 0.8–1.0 |
| **COMPOSES** | JS/TS only (JSX) | 0.9 |
| **RE_EXPORTS** | JS/TS only (barrels) | 1.0 |
| **HANDLES_ROUTE** | Python (4c) + cross-service (4b) | 0.95 |

CALLS resolution is an **11-rung ladder** (`resolver.go`): Strategy 1 same-file (1.0) →
1.5 import-verified (1.0) → 1.75 self/this/Self+inheritance → 1.9 verified-unique (0.95) →
1.93 import-scoped type_flow (0.95) → 1.94 single/few-implementor (0.4–0.85) → 1.95 type-flow
qualified (0.9) → 1.96 assignment-flow (PyCG ICSE 2021) → 1.97 return-type bridging → 1.98
unique-method-class (0.85) → **2 name_match fallback** (cc≤1→0.9, ==2→0.6, ≤5→0.4, else 0.2).
Each edge carries `trust_tier` (CERTIFIED/CANDIDATE/SPECULATIVE) + `evidence_type`.

### 2.4 Property kinds — the "dimensions of understanding" (~23, per function)
| kind | captures |
|---|---|
| **data_flow** | per-parameter forward slice (`param → foo(param) | param==0 | return param`) — **language-agnostic** (Weiser ICSE'81) |
| guard_clause | early if-raise/return/throw/panic (first 5 stmts); Rust `?` |
| conditional_return | `if cond: return X` / else-return |
| return_shape | none/tuple/collection/value; Rust `Result/Option` |
| exception_type / exception_flow / exception_handler | raised/caught types + where |
| param | structured `name:type [required]` / `opt=default` |
| side_effect / field_read | `self.x =` writes / reads |
| caller_usage | how a call's return is used (destructure/iterate/bool-check/guard) |
| docstring | preceding comment / first body string |
| call_order | method-call sequences on a receiver |
| boundary_condition | comparisons vs len()/0/None/nil, index access |
| visibility / class_field / class_decorator | structure |
| security_tag | auth/authz keywords (word-boundary) |
| concurrency_pattern / resource_pattern / config_read | locks/goroutines/channels · with/defer · env/getenv |
| fingerprint | complexity proxy |
| **serialization_pair** | to/from-X partner fns |
| **structural_twin** | parallel CRUD pairs (create_user/delete_user) |

Plus **co-change** (git), **closure** (transitive reach), **FTS5** (BM25 retrieval).

### 2.5 What is missing / schema-present-but-dead (honest)
- **`IMPORTS` edges**: declared in a schema comment, **never emitted** — imports only feed
  the resolver to *produce* CALLS edges.
- **`DEFINES` / `REFERENCES` / `INHERITS`**: **not implemented at all** (stale comment only;
  the real inheritance edge is `EXTENDS`).
- **`edges.metadata`**: effectively always empty.
- **`verification_status`**: written `'unverified'` at index time and **never flipped** —
  even the LSP pass sets `resolution_method='lsp'` + trust_tier, not this column. Stale by design.
- **Relationship edges are language-uneven**: COMPOSES/RE_EXPORTS are **JS/TS-only**; the 23
  Tier-2 languages get **zero** EXTENDS/IMPLEMENTS/COMPOSES/RE_EXPORTS. `side_effect`/`field_read`
  only match `self.`/`this.` (miss Go/Rust receiver mutations). The cross-language-solid kinds
  are data_flow, param, docstring, return_shape, caller_usage.

---

## 3. Layer — LSP enrichment

`groundtruth.resolve` (offline / host-side Python, NOT in the Go binary) promotes the
graph's *guesses* into *facts*:

- Selects only **low-confidence CALLS edges** (`confidence < floor AND type='CALLS'`, ascending)
  — i.e. the name_match rows; same_file/import (1.0) are never touched.
- Dispatches to a language server by extension via **`_LANG_TO_EXT`** (py/ts/tsx/js/jsx/go/rust/
  java/c/cpp/ruby/kotlin) — *this map's earlier bug made the pass a no-op for all 5 languages;
  fixed.*
- For each edge asks the server for the definition, matches the node by **exact
  file+name+line-range** (not basename LIKE, to avoid mod.rs/index.ts collisions), then:
  - same target → **verified** (`confidence=1.0, resolution_method='lsp', trust_tier=CERTIFIED`)
  - different target → **corrected** (re-points `target_id`)
  - no node → **deleted** (false positive removed)
- Promotion to `resolution_method='lsp'` is what the closure's verified set then admits;
  re-run `-rebuild-closure` after, or the closure goes stale.

LSP enrichment is delivered to GT as the `confidence`/`resolution_method` columns —
verified edges get cheap path cost (higher reach), name_match is expensive/suppressed.

---

## 4. Layer — Localization + Brief

The live brief the agent receives is **`generate_v1r_brief`** — not `localize()` or
`run_v74` alone. Two scorers feed a gated renderer:

```
issue → ① run_v74 (candidate gen + scoring)
        ② localize  (grep-spine + 3-ranker RRF + agreement → confidence tiers)
        ③ render_brief (hub demotion, ~5-cap + min-guarantee, confidence-gated
                        show/suppress, abstain → grep-fallback, token budget)
      → curated brief → agent
```

- **① run_v74**: `select_anchors` (semantic_top ∪ graph_expand) → candidate union (BM25/path/
  frame/code-def) → linear-sum score, weights per-task adapted by `_adapt_weights_for_issue`.
- **② localize**: 4 seeders (exact-name → path → **grep (recall spine)** → FTS5/BM25), 1..`max_hop`
  BFS recording per-file witnesses, path-decay, composite `_raw_score` feeding the **structural**
  ranker only, then **3-way RRF** of grep/structural/semantic with an **agreement** vote.
- **③ render_brief**: hub demotion (`_is_generated −0.5`, `_is_test −0.4`, grep-spine promote-only),
  dynamic K-cap with a min-candidate floor (never collapse to 1), confidence-gated show/suppress,
  abstain→grep note when nothing anchors; the token budget trims evidence **detail**, never the file list.

### 4.1 Confidence tiers (`_localization_header`) — "how many of grep/semantic/structural agree"
- **HIGH** (`Edit target: file :: fn` + guard line + reason): needs **(a)** an issue-anchored
  verified non-DEFINES edge, **(b)** agreement ≥ 2, **(c)** non-hub (per-task in-degree ≤ p80).
  If every eligible candidate is a hub → no HIGH, fall through (correct-or-quiet).
- **MEDIUM** (`Candidate edit targets (reason over these):` list): agreement ≥ 1.
- **LOW** (region summary or flat list): agreement < 1.

### 4.2 Scoring weights (current, as shipped)
**run_v74 DEFAULT_WEIGHTS:** `W_SEM=0.15, W_LEX=0.50, W_REACH=0.05, W_PROX=0.05, W_HUB=0.10,
W_COMMIT=0.0, W_PATH=0.45`, plus **`W_FRAME=0.60`** (stack-trace/typed-path) and
**`W_CODE_DEF=0.70`** (backtick code-symbol definition site) — both no-op when nothing resolves.

**localizer composite:** `W_WITNESS=0.60, W_BM25=0.35, W_PATH_DECAY=0.30, W_LEX=0.30,
W_SUBJECT=0.15, W_DEGREE=0.10`, with **gen −0.5** and **test −0.4** penalties applied post-composite.
The composite feeds the *structural* ranker only; the final sort is grep-floor + 3-way RRF.
`W_CLOSURE` is intentionally **absent** (reach/closure is the wrong ranking lever).

### 4.3 The documented levers are NOT applied
The ranking change-list (W_LEX 0.50→0.60, W_REACH 0.05→0.02, W_BM25/W_PATH_DECAY/W_LEX
0.35/0.30/0.30→0.40/0.15/0.40, min-3 BM25 guarantee, caller-render conf gate, reach min_conf
0.0→0.5) are **research items, not in the binary** — current weights are the values above. They
ship only after measuring `first@5` improves, one variable at a time. Do **not** assume they're live.

---

## 5. Layer — Semantic / ONNX (the corrected state)

> This supersedes the old "semantic is OFF in both halves / `embed.py` gitignored" note.
> Semantic is now **ON via the container ONNX path in BOTH halves.**

There are two semantic call sites; both now resolve to the **same** container ONNX surface:
- **run_v74 `_get_model`**: sentence-transformers (`all-MiniLM-L6-v2`) → **ONNX e5-small-v2 via
  `_OnnxEmbedderAdapter`** → `_ZeroEmbeddingModel`.
- **localize `_get_embedder`**: sentence-transformers (`st-codesearch-distilroberta`) → **ONNX
  e5-small-v2 via `_OnnxEmbedderAdapter`** → `None`.

Enforcement:
- **`GT_FORCE_ONNX_EMBEDDER=1`** skips sentence-transformers so both halves use the *identical*
  container ONNX `_OnnxEmbedderAdapter` (e5-small-v2, no torch) — one surface, consistent numbers.
- **`GT_REQUIRE_EMBEDDER=1`** makes both halves **raise** instead of silently zeroing W_SEM.
- **`GT_MODELS_ROOT`** points the loader at a baked/pre-fetched model dir (`embed.py`), so a
  from-checkout GT finds the model with no per-run HuggingFace download.

The model (e5-small-v2 ONNX, ~90MB) is baked once via `scripts/setup_models.py`. The
loader (`groundtruth.memory.enrich.embed`) is shipped (the `Memory/` gitignore that once
excluded it is fixed).

---

## 6. The tools GT hooks onto the agent (overall)

GT delivers evidence by **hooking onto the agent's actions**, at the overall level:

| Moment | What GT hooks in |
|---|---|
| task start | the prepended brief (`<gt-task-brief>`, `<gt-graph-map>`, `<gt-localization>`, `<gt-orientation>`) |
| per source-view | contracts + graph-navigation (`Called by:` / `Calls into:` / `[CONTRACT]` / `[RAISES]`) |
| per edit | post-edit contract evidence (`[SIGNATURE]`, `[BEHAVIORAL CONTRACT]`, `[CALLERS]`, `[TWIN]`, `[COMPLETENESS]`, `PRESERVE:`) |
| per turn | trajectory governor (test-failure nudges, scaffold/loop redirects) |
| per edit | incremental reindex so the next view/edit sees the new graph |

### The hooked tool surface
GT registers an MCP tool surface (FastMCP, stdio): the **16 core** —
`groundtruth_find_relevant, brief, validate, trace, status, dead_code, unused_packages,
hotspots, orient, checkpoint, symbols, context, explain, impact, patterns, do` — plus ~7 newer
additions (`groundtruth_task_map, event_brief, review_patch, investigate, orient_v2, check_v2,
status_v2`, and `gt_plan` / `gt_contract` / `gt_run_tests`).

**Reality, stated plainly:** in benchmark mode the tool *instructions* are **suppressed** because
agent tool-adoption measured ~0%. So the tools are **registered but passive** — GT's real
delivery is the *passive* brief + per-view/per-edit hooks above, not agent-invoked tool calls.

---

## 7. The gates — no silent fallback (a paid run proves the stack is live or aborts)

| Gate | Asserts | On failure |
|---|---|---|
| `GT_REQUIRE_FTS5=1` | `nodes_fts` Go-built (`-tags sqlite_fts5`) + populated + a real MATCH returns rows | `gt-index` aborts the index build |
| `GT_REQUIRE_EMBEDDER=1` | a real embedder loads + yields finite non-zero vectors (not Zero) | `_get_embedder`/`_get_model` raise |
| `GT_FORCE_ONNX_EMBEDDER=1` | both halves on the identical container ONNX surface | (behavioral, not abort) |
| `GT_REQUIRE_LSP=1` | server **launches** (`start(warm=True)`) AND a real probe resolves via `method=='lsp_references'`, `latency>0` | wrapper raises (no 0ms confidence-filter fallback) |
| `GT_REQUIRE_FULL_STACK=1` | per-task graph-base dimension gate: graph_exists, schema, fts5, edge_quality, **data_flow enriched**, assertions, lsp_enrichment, lsp_edges | raises on any degraded dimension |
| `GT_FORBID_PREBUILT_GRAPH=1` | fresh in-container per-task index; refuses prebuilt/cross-run graph.db | clears the prebuilt path; preflight fails if contradicted |

These exist because a prior run silently degraded (FTS5 rebuilt, semantic=0, LSP 0ms) and
produced confounded results. The gates are opt-in; the benchmark workflows arm them.

---

## 8. Hardcoded vs Dynamic — every fixed value, with its reason

### Hardcoded (and WHY)
| Param | Value | Reason it's fixed |
|---|---|---|
| RRF fusion `k` | 60 | the SIGIR-2009 Cormack convention — fixed so fusion is reproducible across rankers |
| path-decay `beta` | 0.85 | KGCompass decay constant (3-hop≈0.61, 4-hop≈0.52) — a published decay, not a tuned knob |
| closure `MaxDepth` | 3 | impact/trace reach bound; beyond 3 hops decays to noise (BFS-explosion guard) |
| `MAX_FILES` (brief list) | 5 | Lost-in-the-Middle TACL 2024 / Power-of-Noise SIGIR 2024 — more files dilute the prepend |
| `MAX_BRIEF_TOKENS` | 600 | prepend budget; keeps the brief above the "middle" of context |
| `EDGE_CONFIDENCE_FLOOR` | 0.7 | the fact/guess boundary — below it an edge is a name_match guess, not a deterministic fact |
| run_v74 `min_confidence` | 0.7 | same boundary, applied at candidate admission |
| localize `top_k` | 8 | retrieve→rerank candidate window (SWERank-style); enough recall without hub flood |
| sparse-graph trigger | edges/file < 2.0 | below this the graph is too thin to rank on → switch to BM25-only weights |
| `_TOP_N_AGREE` | 3 | the RRF agreement vote is over each ranker's own **top-3** |
| seed-quality midpoint | 0.5 | a 3-signal composite gate; 0.5 is the neutral midpoint, not a benchmark-tuned number |
| witness strengths | verified 1.0 / defines 0.55 / name_match 0.45 | encode the fact→guess ordering (SWERank hard-negative) |

These are **research constants or structural boundaries**, not per-benchmark tuning. None is
derived from gold labels, task IDs, or FAIL_TO_PASS.

### Dynamic (data-derived per task)
| Param | Driven by |
|---|---|
| `max_hop` (BFS depth) | graph density + verified-edge ratio: dense+verified → 2, sparse/low-conf → 3 |
| confidence floor (localize) | per-task score distribution: 0.6 if p50≥0.8 else 0.5 (never below name_match floor) |
| K-cap (HIGH/MED/LOW breadth) | the evidenced-candidate count, railed `[3..6]` |
| adaptive-K cut (file list) | per-task **median score gap** (cut where gap > 2× median) |
| grep recall budget | repo size: `max(15, min(60, nodes/60))`, ×1.6 when seed quality is low |
| HIGH hub-gate | per-task **in-degree p80** (a hub here is relative to this repo, not an absolute degree) |
| run_v74 weights | `_adapt_weights_for_issue`: signal-presence, single/multi-file scope, graph-determinism % |

This is the Dynamic + Hybrid + Confidence-gated contract: thresholds scale with the actual
per-task data; ranking composites ≥3 independent signals; tiers are explicit confidence gates.

---

## 9. Research basis (as the code cites it)

KGCompass 2025 (path-decay/hops) · RepoGraph ICLR 2025 (k-hop ego-graph, hub caution) ·
SWERank ICLR 2025 (retrieve→rerank, witness hard-negative) · BLUiR ASE 2013 (field-level
lexical) · Lao & Cohen 2010 PRA (hub-discounted paths) · RRF Cormack SIGIR 2009 + CombMIN
Fox&Shaw TREC-2 1994 (rank fusion) · Lost-in-the-Middle TACL 2024 / Power-of-Noise SIGIR 2024
(brief breadth, prepending) · PyCG ICSE 2021 (assignment-flow resolution) · Weiser ICSE'81
(forward slicing / data_flow) · TCTracer (assertion→tested-fn linking) · plus
Agentless/LocAgent/CoSIL (localization), QPP/NQC (score-separation gating).

---

## 10. Known gaps / not built

- **Dimensions**: IMPORTS/DEFINES/REFERENCES edges unimplemented; relationship edges
  language-uneven (Tier-2 langs get none); `verification_status`/`metadata` columns inert.
- **Localization recall**: when the gold shares no lexical/structural signal with the issue's
  surface terms (symptom-vs-cause), the candidate set can miss it — a ranker/recall research item.
- **Hardcoded structural-reach params** (beta, MaxDepth) are research constants, not yet
  per-task dynamic — flagged against the Dynamic pillar; deeper-BFS variants were falsified, so
  any change must be a per-task stop-criterion, not a bigger constant.
- **Generalization**: the cross-language property/edge coverage is thin outside Python/JS/TS;
  Go/Rust get CALLS-dominant graphs.
- **The §4.3 ranking levers** remain unvalidated/unapplied.

---

## §11 — LIVE FAILURE LEDGER (paired GT-vs-baseline; documented per change)

> **Verification rule (lesson from the 2026-06-05 full run):** gate every phase on
> **paired GT-vs-baseline lift (flips > regressions)**, NOT on "GT delivered." Delivery
> passed the phased rollout while GT was net-negative (16 vs 17 paired, 1 flip / 2 regressions).
> Verifier/spawned agents MUST read THIS file as the spec. Before any code change → read
> CLAUDE.md; after any change/finding → document it here immediately.

### Known-failure regression harness (known-correct answers)
- `kozea__weasyprint-2300` — gold `weasyprint/layout/block.py::block_box_layout` (add
  `or new_box.is_flex_item` to the early-return guard). Baseline (GT-off) RESOLVED; GT-on REGRESSED.
- `matplotlib__matplotlib-28933` — gold `lib/matplotlib/lines.py::set_xy1/set_xy2`
  (`*args,**kwargs` + `_api.warn_deprecated`). Baseline RESOLVED; GT-on REGRESSED.

### BUG-1 — L1 brief drops the ranker's rank-2 (gold) file. Avenue: INTEGRATION.
Two independent rankers; the brief renders the WRONG one. `v74.ranked_full`
(`v7_4_brief.py:990,1047-1097`, composite-scored, the list the diagnostic logs) put
`lines.py` at **#2 (0.935, bm25 87.67)**. But `<gt-localization>` is rendered PURELY from
`graph_localizer.localize()`'s `loc.candidates` (`v1r_brief.py:1626`, `shown=cands[:K]`
`:1683`, render `:1801-1807`) — a different ranker whose **grep-floor primary sort**
(`graph_localizer.py:1944-1952`, `top_k=8`) is dominated by the issue's EXAMPLE symbol
`set_xlim` (→ `_base.py/_axes.py/pyplot.py`) and truncates the true target `lines.py`
(`set_xy1/set_xy2/AxLine`) out of top-8. `v74.ranked_full` (with lines.py #2) is computed +
logged then IGNORED for the localization header. All candidates were `top_unverified`
(`l1_candidates_with_primary_witness_count=0`) — localizer had NO structural authority yet
still owned the order. D-3 `_high_func_support` did NOT touch this (HIGH-tier function-name
only; matplotlib rendered MEDIUM). **Fix (correct-or-quiet):** when the localizer is
witnessless (0 verified witnesses), union `loc.candidates` with the `v74.ranked_full` head
(tests already stripped by `_is_test_file` `:1917-1936`) before the `_localization_header`/
`shown` cut, deduped — no-op when the localizer has verified witnesses. Files: `v1r_brief.py`
(1626-1808, 1848-2306), `graph_localizer.py` (1291-1952), `v7_4_brief.py` (990,1047-1097).

### BUG-2 — in-container gt-index returns EMPTY graph (0 nodes). Avenue: IMPLEMENTATION (+PLUMBING).
In-container `gt-index -root` ran Pass1 (1883 files) + Pass2 (parsed 11s) then DIED at the
FTS5 populate step (`store/sqlite.go:281-318 PopulateFTS5` recovery "nodes_fts clear/insert
failed → DROP+recreate"); binary built **WITHOUT `-tags sqlite_fts5`** → `CREATE VIRTUAL
TABLE … fts5` fails → `/tmp/gt_index.db` left with 0 nodes. Proof it's build-path-specific:
the L6 single-file reindex (`gt-index -file=`, Tx path `main.go:823-1057`) rebuilt the SAME
db to **17611 nodes**. `GT_REQUIRE_FTS5=1` preflight (`main.go:297-304`) NOT enforced
in-container. PLUMBING compound: host-graph download gated `if nc>0 and not _host_graph_db`
(`oh_gt_full_wrapper.py:4066`) → nc==0 → host fallback never ran → per-edit L3 hooks read the
empty db (`semantic_check graph_db=False NO_EVIDENCE`). **Fix:** (1) build container gt-index
with `-tags sqlite_fts5` + enforce `GT_REQUIRE_FTS5=1` (abort loud, not silent-empty);
(2) node/edge commit durable BEFORE FTS5 so an FTS failure can't strand 0 nodes
(`main.go:270,290`); (3) decouple host-graph fallback from `nc>0` — when in-container build
is empty, fall back to `_host_graph_db` for per-edit hooks; (4) raise the `index_out[:400]`
truncation (`oh_gt_full_wrapper.py:4050`) to capture the abort line.

### Verification harness for BUG-1/BUG-2 (the 2 known-failures)
The gate is **paired GT-vs-baseline lift**, not "GT delivered" (delivery ≠ engagement). For
the known-failures the baseline verdict is NO on both, so a GT *resolve* on either is a real
flip; a brief that now surfaces gold is the necessary precondition we check first.
- **Targeted dispatch:** `swebench_300task.yml` gained a `task_ids` allowlist input (commit
  `c3ea008c`) — runs EXACTLY a comma-separated set, overriding num_tasks/task_offset, so the
  2 known-failures run without first-N-sorted slicing. Dispatch:
  `gh workflow run swebench_300task.yml --ref gt-fullrun-shard -f task_ids="kozea__weasyprint-2300,matplotlib__matplotlib-28933"`.
- **Brief-correctness check:** `scripts/verify/check_gold_in_brief.py` reads the FIRST
  agent-facing instruction out of `output.jsonl` and asserts the gold basename appears in the
  delivered brief, reporting its rank in the localization list + the confidence tier. Gold-aware
  ONLY as harness (`block.py` for weasyprint-2300, `lines.py` for matplotlib-28933) — never in
  product logic. Pairs with `check_brief_delivery.py` (hygiene: tags, no [GT_*] leak, balanced
  contracts). PASS criterion for BUG-1: gold present in brief on BOTH tasks.
- **Verify run dispatched:** run `27002256876` on `gt-fullrun-shard` (BUG-1 fix `47bcdf2b`
  active). Both agent jobs cleared Stage-0 preflight and ran.

#### BASELINE ALREADY EXISTS — NEVER RERUN IT
The GT-OFF full-300 verdicts are frozen on disk:
`.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`
(`resolved_ids`, **87/300**, OH CodeActAgent + deepseek-v4-flash). **NEVER launch a baseline /
GT-off run.** Pair every GT-on result against this file: positive flip = GT-on RESOLVES an id
NOT in `resolved_ids`; regression = GT-on FAILS an id IN `resolved_ids`. Run the GT-on arm only.

#### Verify-run RESULTS (run 27002256876) — corrected paired framing
**The 2 "known-failures" are baseline PASSES that GT REGRESSED, not flip candidates.** The
GT-OFF baseline (above, 87/300 resolved) **resolved BOTH** weasyprint-2300 and matplotlib-28933. Prior GT-ON FAILED them →
they were GT-caused regressions. So a GT-on-post-fix RESOLVE = **harm-reduction (un-regressing
GT's own damage), NOT a positive flip.** Honest accounting requires this distinction.
- **weasyprint-2300: RESOLVED GT-on-post-fix.** Baseline=PASS, GT-pre-fix=FAIL → GT-post-fix=PASS.
  The BUG-1 fix REMOVED a GT regression. Mechanism proven end-to-end: gold `weasyprint/layout/block.py`
  was in the v74 candidate set (rank 11, sem 0.832), reached the delivered brief (`brief_chars=7318`,
  `check_gold_in_brief.py` PASS), and the agent **edited gold block.py** → resolved. Cost ≈ $0.44
  (prompt 5.68M / cache-read 5.60M / completion 25.8k). `l1_ranking_diagnosis` `gold_files=[]` so its
  `is_gold:False`/`gold_in_candidate_set:False` are LABEL ARTIFACTS (diagnosis ran without gold), not misses.
- **matplotlib-28933: RESOLVED GT-on-post-fix.** Baseline=PASS, GT-pre-fix=FAIL → GT-post-fix=PASS
  (regression removed). The EXACT BUG-1 target: gold `lib/matplotlib/lines.py` (v74 rank-2,
  previously dropped by the grep-floor header) now reaches the delivered brief (`brief_chars=7766`,
  verifier PASS), agent **edited gold lines.py** (+`.pyi`+repro) → resolved. Graph healthy
  (`nodes=17611 edges=35904`, FTS5 present). Cost ≈ $0.20 proxy.
- **Both known-failures green. Net: BUG-1 fix removed GT's regressions on BOTH tasks** (the prior
  paired run's net-negative "2 regressions" — at least these two are now closed). Real cost for the
  pair = **$0.40** (balance $61.06→$60.66). This is HARM-REDUCTION, not positive flips.

#### TRAJECTORY-CAUSALITY CORRECTION (RESOLVED ≠ GT caused it)
Per the KEY RULE (CLAUDE.md "RESOLVED IS NOT THE PRIZE; THE TRAJECTORY IS"), the resolves above
do NOT establish GT causality — reading the agent's own action sequence shows **self-localization**:
- **weasyprint-2300** (86 actions): first touches gold `block.py` at **action 33** (deep, after
  independent exploration). Early actions followed the brief's TOP entries `properties.py`#1 /
  `flex.py`#2 (both NON-gold) and the agent **edited non-gold `flex.py`**. Gold sat at brief rank 11.
- **matplotlib-28933** (79 actions): touches gold `lines.py` at **action 4**, but via its OWN
  `grep "class AxLine"` (issue-text self-localization), not by reading the brief.
What the run actually proved: **(1) DELIVERY fixed** — gold now appears in the brief text (BUG-1's
narrow target). **(2) NOT proven** — that the agent CONSUMED the brief to localize; evidence points
to self-localization (the same ~88% competence by which the GT-OFF baseline already passed both).
**(3) Possible residual harm** — the brief's top-ranked non-gold `flex.py`/`properties.py` coincide
with the agent's early non-gold `flex.py` edit. Earlier "mechanism proven end-to-end" was an
overclaim (led with verdict, not trajectory). Open question for the flip work: does the agent ever
localize THROUGH the brief, or always around it?

#### BUG-3 (the REAL one) — the brief's PRIMARY EDIT-TARGET block misdirects; BUG-1 fixed the wrong path
Reading the actual `<gt-task-brief>` the agent received (not the surrounding instruction) overturns
"BUG-1 delivery fixed." The brief's DOMINANT content is the `1. <file> (defs) … EDIT-TARGET
CONTRACTS (<file>)` block — a DIFFERENT code path than the localization-header fallback BUG-1
patched. It misdirects on BOTH tasks, in two distinct modes:
- **matplotlib-28933 (rendering/integration):** v74 ranked gold `lib/matplotlib/lines.py` at **rank 2**
  (rank 1 = `tests/test_lines.py`, correctly test-excluded). Yet the brief's primary target is
  `lib/matplotlib/axes/_base.py` — **not even v74 top-3** — with full set_xlim/set_ylim contracts, and
  gold `lines.py` appears **0 times** in the brief. The issue's EXAMPLE symbols (`set_xlim/set_ylim`)
  live in `_base.py` → example-symbol/hub misdirection. **BUG-1's fix did not touch this path; v74-rank-2
  gold is still dropped from the brief.**
- **weasyprint-2300 (ranking quality):** brief primary target = `css/validation/properties.py` (= v74
  **rank 1**, non-gold, sem 0.83+anchor_prox 1.0); gold `block.py` is v74 **rank 11**, surfacing in the
  brief only as one node in the "Scope chain" enumeration (count=4, never as a target). Here v74 itself
  mis-ranked (non-gold above gold).
- **Verifier false-positive (fixed):** `check_gold_in_brief.py` scanned the whole instruction and matched
  `lines.py`/`block.py` as a substring in issue text / scope-chain → PASS. It must parse ONLY the
  `<gt-task-brief>` block AND require gold to be the PRIMARY edit-target (or in the rendered candidate
  list), not present-anywhere.
- **Net:** GT's brief is misdirecting (wrong primary target + heavy scaffolding on it); both tasks
  resolved only because the agent IGNORED the brief and self-localized (wp: gold at action 33/86 after
  editing non-gold flex.py; mpl: gold at action 4 via its own `grep "class AxLine"`). The real levers:
  (1) the edit-target selection must not pick example-symbol hubs over the issue's true subject;
  (2) v74 ranking must lift gold (wp rank 11) — or the brief must present top-N evenhandedly instead of
  over-committing to rank-1 with contracts. Locate the edit-target selection in `v1r_brief.py` (the
  `EDIT-TARGET CONTRACTS` renderer) — that is BUG-3's site, not the localization header.

#### BUG-3 full LIPI root-cause chain (the mechanism, all 4 avenues)
The brief drops gold via the `[INFO]` tier-filter (`render_brief` line 1224 filters out `[INFO]`
entries; both briefs ended with ONE surviving candidate = the witnessed non-gold hub).
`_entry_confidence_tier` (v1r_brief.py:1042-1106) assigns the tier:
- **Logic:** retention is keyed on a verified graph WITNESS (`witness_verified`→`[VERIFIED]`, 1086)
  or `issue_match`(function_names)/`path_match`. Gold `lines.py` entered via `graph_rescue` with NO
  verified witness, so it depended on `issue_match` — which failed.
- **Implementation:** `issue_match` checks `entry.function_names` against issue text, and
  `function_names` is **ref_count-ranked** (`_top_functions` ORDER BY ref_count DESC, line 227). The
  issue's subject functions are the ones being FIXED — freshly-added / low-traffic (`set_xy1/set_xy2`),
  so they're NOT in the ref-ranked function_names → `issue_match=False`. **GT structurally excludes
  exactly the functions bug-fix issues are about.**
- **Integration:** v74 computed `anchor_prox=1.0` for gold `lines.py` (it CORRECTLY matched the issue
  anchors, rank 2, lex 1.0/reach 1.0) — but that signal is NOT propagated into `_entry_confidence_tier`;
  the tier recomputes from function_names and the witness-centric localizer view overrides it.
- **Plumbing:** `FileEntry` has no `anchor_prox` field — the one signal that correctly identified gold
  dies at the FileEntry boundary; the tier never sees it.
**FIX (precise, generalized, constitution-mandated — implements CLAUDE.md "never gate edge-free
issue-subject context behind a connectivity check"):** plumb v74 `anchor_prox` onto `FileEntry`; in
`_entry_confidence_tier`, a file with `anchor_prox >= ~0.5` (issue-anchor proximity = edge-independent
subject evidence) earns ≥`[WARNING]` so it survives the `[INFO]` filter. Verify FREE via red→green unit
test (synthetic FileEntry: anchor_prox=1.0, no witness, gold fn absent from function_names → currently
`[INFO]`, must become `[WARNING]`) + keep the beets-5495 trajectory test green. End-to-end (brief
surfaces gold as primary) needs a graph → gate that on a paired re-run. NOTE: graph.db is NOT in the
artifacts, so local end-to-end repro needs a rebuild; the unit test is the free proof of the logic fix.
- **BUG-2 CLOSED (stale-binary artifact, confirmed).** In-container build logged
  `GT graph sanity OK: nodes=2349 edges=4004` + `FTS5: nodes_fts exists, querying directly`.
  Current code already commits nodes before a non-fatal PopulateFTS5, builds `-tags sqlite_fts5`
  (workflow FATAL-on-fail), uploads that binary into each container, and arms `GT_REQUIRE_FTS5=1`
  fail-closed. The §11 BUG-2 "nodes committed AFTER FTS5 → 0 nodes" does not match current code.
- **Implication for the GOAL:** this harness proves harm-reduction, NOT positive flips. Positive
  flips require GT-on RESOLVE on tasks where baseline=NO — a DIFFERENT task set (the 213 baseline
  failures), not these 2. Re-scale must measure GT-on vs GT-off PAIRED on baseline-failure tasks.

---

*End — gt_gt.md. Localization deep internals: `BRIEFING.md`. Benchmark operation/gates:
`BENCHMARK_RUNBOOK.md`. Fix history: `we_did.md` (legacy).*
