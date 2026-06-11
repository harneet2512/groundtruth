# gt_gt.md — GroundTruth Architecture (GT itself, layer by layer)

> The single master reference for **GroundTruth's own architecture** — what it
> builds, what it collects from a graph, how it scores and delivers, what is
> hardcoded (and why), what is dynamic, what is enforced, and what is missing.
>
> **Scope rule:** this documents GT *only*. The agent harness (OpenHands /
> mini-swe-agent) appears only as "the surface GT hooks evidence/tools onto," at
> the overall level — never harness-specific plumbing. Branch
> `gt-trial`. Last verified 2026-06-09 (direct code reads, post the 4-surface
> LIPI hardening — §13.7; dated "UPDATED (2026-06-09, …)" notes mark what moved).
>
> Supersedes the architecture content scattered across `DOC_OF_HONOR.md`,
> `we_did.md`, and `BRIEFING.md` (now legacy/feeders; BRIEFING stays the deeper
> localization scratchpad).
>
> **Bugfree program (2026-06-11):** validation run `27367976952`, checkpoints
> 001–010, bug ledger B1–B11 — **§17** (architecture of record). Per-checkpoint
> evidence lives under `.claude/reports/runs/validation_27367976952/`.

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
| 3 CALLS | the §2.3 resolver ladder (~13 rungs + drop/demote gates) → `CALLS` edges; emit `CONTAINS` from parent_id |
| 4 PROPERTIES+ASSERTIONS | insert properties; resolve assertion→tested-function (TCTracer) |
| 4b API EDGES | cross-service route matching → `API_CALL` @0.7 (route JSON in `metadata`) |
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

> **UPDATED (2026-06-09, commits `dd460fe7` + `10368a2f`).** The previous "11-rung ladder"
> description was stale; the live ladder is **~13 rungs + 2 drop/demote gates**, and the
> 4b/CHA/relationship edges changed. What follows is the corrected state (verified by direct
> `resolver.go` / `relationships.go` / `api_edges.go` reads at HEAD).

| Edge | Languages | Confidence model |
|---|---|---|
| **CALLS** | all | 0.2–1.0 (per the ladder below) |
| **CONTAINS** | all (structural) | 1.0, CERTIFIED |
| **EXTENDS** | py/js/ts/java/kotlin/go/rust | 1.0 |
| **IMPLEMENTS** | js/ts/java/kotlin 1.0; **Go CHA 0.6–0.85** (below) | 0.6–1.0 |
| **COMPOSES** | JS/TS only (JSX) | 0.9 |
| **RE_EXPORTS** | JS/TS only (barrels) | 1.0 |
| **HANDLES_ROUTE** | **Python (4c `decorator_route`) only** | 0.95 |
| **API_CALL** | cross-service (4b), any lang | **0.7**, route JSON in `metadata` |

**The CALLS ladder** (`resolver.go`, in fire order) — ~13 rungs:
Strategy **1** same-file unambiguous (1.0; **multi-def same-file → 0.6 CANDIDATE
`same_file_ambiguous`**, unqualified calls only) → **1.5** import-verified (1.0; multi-file
no-same-dir pick → **0.6 `ast_import_ambiguous`**) → **1.75** self/this/Self + inheritance
(1.0/0.95) → **1.9** verified-unique (0.95 — **UNQUALIFIED calls only**, see the reorder note)
→ **1.93** import-scoped type_flow (0.95) → **1.94a declared-type receiver** (`qualifier.m()`
where the caller declared `qualifier`'s type → CHA lookup → `type_flow` **0.9**, evidence
`param_type` — XTA over the language-uniform `param` property) → **1.94** single/few-implementor
(`impl_method`, **CANDIDATE-capped: 1 class=0.6, 2=0.5, 3=0.4** — name-uniqueness never proves
the receiver, so it can never be CERTIFIED) → **1.95** type-flow qualified (0.9) → **1.96**
assignment-flow (PyCG ICSE 2021) → **1.97** return-type bridging → **1.98** unique-method-class
(0.85) → **last-chance gate** (below) → **2** name_match fallback (**2+ candidates ONLY**:
==2→0.6, ≤5→0.4, else 0.2 — **the cc≤1→0.9 row of the old table never occurs on the full
path**: a single unqualified candidate is 1.9 `verified_unique` 0.95; a single qualified-
unresolved candidate is the last-chance demote).

**The 2 drop/demote gates (NEW order, `10368a2f` #B5):** a QUALIFIED call now reaches the
type-aware rungs (1.93/1.94a/1.95/1.96/1.97) **FIRST**; only after every receiver-typing rung
fails does the last-chance gate run: **(a) T2 builtin drop** — receiver never resolved internal
+ builtin/stdlib method name (`join`/`get`/`items`/`loads`…) → the edge is **DROPPED** (single-
AND multi-candidate paths), never a name_match guess; **(b) single-candidate demote** — a
qualified-unresolved call with one global candidate → `name_match` **0.2 SPECULATIVE**
(`name_match_qualified_unresolved`), the stdlib-shadow guard (os.walk → account.walk) preserved.
Previously this demote fired BEFORE 1.93–1.98 and starved e.g. `command.run()` (declared
`command: Command`) of its type_flow resolution.

**Go CHA IMPLEMENTS (`relationships.go`, `10368a2f`):** structural method-set satisfaction with
**name+arity+result-presence fingerprints** (`structural_method_set_arity`): **≥2-method
interface → 0.85**, **1-method interface → 0.6/CANDIDATE** (ambiguous by construction),
**incomplete embedded-interface expansion → abstain** (never match an under-approximated set);
required sets keyed file+name; the edge anchors on the STRUCT's file (survives `-file` reindex
orphan-correctly).

Each edge carries `trust_tier` (CERTIFIED/CANDIDATE/SPECULATIVE) + `evidence_type`; **relationship
(EXTENDS/IMPLEMENTS/COMPOSES/RE_EXPORTS) and API_CALL edges now carry REAL `trust_tier` /
`verification_status`** (previously empty strings — the explicit empty bind defeated the SQL
defaults; `10368a2f` #2 stamps them via the same `tierFor` thresholds).

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

> **UPDATED (2026-06-09, commits `dd460fe7` + `ffc6c7dc` + `10368a2f`).** Three of the old
> bullets are no longer true; struck below with what replaced them, plus new deterministic
> facts the section predated.

- **`IMPORTS` edges**: declared in a schema comment, **never emitted** — imports only feed
  the resolver to *produce* CALLS edges. *(still true)*
- **`DEFINES` / `REFERENCES` / `INHERITS`**: **not implemented at all** (stale comment only;
  the real inheritance edge is `EXTENDS`). *(still true)*
- ~~**`edges.metadata`**: effectively always empty.~~ **STRUCK** — `API_CALL` edges (4b) carry
  a route JSON in `metadata` (`api_edges.go`); CALLS edges still leave it NULL.
- **`verification_status`**: written `'unverified'` at index time and **never flipped** —
  even the LSP pass sets `resolution_method='lsp'` + trust_tier, not this column. Stale by
  design. *(still true — but it is now at least POPULATED on relationship/API_CALL rows, §2.3)*
- ~~`side_effect`/`field_read` only match `self.`/`this.` (miss Go/Rust receiver mutations).~~
  **STRUCK** — both are **receiver-aware** (`recvName`: Go `func (c *Circle)` → `c.field`
  writes/reads count, `parser.go`), and `field_read` **skips call selectors** (`c.Area` of
  `c.Area()` is not a field read; chained-call receivers like `self.x` in `self.x.area()` are
  kept — `10368a2f` #4).
- **Relationship-edge language coverage** (corrected): extraction passes exist for python,
  js/ts, java/kotlin, **go (incl. CHA IMPLEMENTS)**, and **rust** (`relationships.go`);
  COMPOSES/RE_EXPORTS remain JS/TS-only and the remaining Tier-2 languages still get no
  relationship edges. `data_flow`/`param`/IMPLEMENTS **do fire on TS/Rust on the current
  binary** — earlier "0 on TS/Rust" readings were **stale graphs**, not the code.

**Deterministic facts this section predated (now load-bearing):**
- **`-file` incremental restore preserves ALL 11 deterministic methods** (`incremental.go`
  `deterministicRestoreMethods`: lsp, lsp_verified, verified_unique, type_flow, import_type,
  inherited, unique_method, return_type, impl_method, same_file, import) **verbatim with their
  confidence** — the previous `{same_file, import}`-only preserve stripped every lsp/type_flow
  edge to a name_match guess on a single-file reindex (the L6 "LSP-strip", §12). Candidate
  lookups are `ORDER BY id` (deterministic restore, `10368a2f` #6).
- **Closure admission is now AND, not OR** (`closure.go` #B7): an edge enters the transitive
  closure iff `resolution_method ∈ deterministic set` **AND** `confidence ≥ 0.7` — the old
  OR-rule let 0.6 guesses (2-candidate name_match, ambiguous same_file/import, impl_method)
  propagate transitively through a "verified-only" sidecar. `impl_method` and `name_match`
  are categorically excluded.
- **Synthetic File-anchor nodes** (`label='File'`, minted for zero-symbol barrel/re-export
  files): the link-token check requires **line-start on a non-comment line** (no phantom
  nodes from prose containing " from "), and File nodes are **excluded from the call-name
  index** — they can never become `verified_unique` call targets (`10368a2f` #3).
- The cross-language-solid property kinds remain data_flow, param, docstring, return_shape,
  caller_usage (+ now receiver-aware side_effect/field_read).

> **ADDED (2026-06-10, PATH B per-layer health audit run 27260307167) — DELIVERY FACT-FILTER at
> the consumer surfaces.** Two pollution classes were being DELIVERED as deterministic facts
> through L1/L3/L3b despite the §2.3 trust model: (a) **vendored/minified/generated paths**
> (`astropy/extern/jquery/*.min.js` cited as a resolved caller / raw minified jQuery as a
> `[WITNESS]`; `jquery.dataTables.js` in "Related files to inspect"); (b) **bare builtin-shadow
> laundering** — a bare `isinstance(...)` call resolves **`verified_unique` 0.95** when exactly ONE
> project symbol shadows the builtin name (`TableColumns.isinstance`), rendering
> `[CALLERS] isinstance: 1048 verified caller(s) … preserve this interface`. The §2.3 **T2 builtin
> drop fires on QUALIFIED calls only**, and the §2.5 stdlib-shadow guard (55ab30eb) catches the
> qualified `os.walk` shape only — the bare-call shape was the residual. Because PATH A/B substrate
> graphs are FROZEN (read-only, hash-paritied), a resolver fix cannot reach live runs; the
> **consumer fact surface is the operative guard**. Fix (gt_mini_patch.py + v1r_brief.py, fact-filter
> lines only): three composited signals — path-class (`/extern/`,`/vendor/`,`/third_party/`,
> `/node_modules/`,`/dist/`,`/_generated/`,`*.min.js`, protobuf/codegen markers — extends the
> localizer's `_is_generated` W_GEN demote from ranking to DELIVERY), content-class (mean non-blank
> line length > 200 = minified), and name-class (builtin/dunder set mirroring resolver.go
> `builtinMethodNames`/`strongBuiltinMethodNames` + shadowable Python/JS/Go/Rust builtins) — applied
> to every delivered fact: `[WITNESS]`, `[CALLERS]` (incl. the recomputed verified-caller COUNT),
> `[CALLEE]`, contract rows, siblings, consensus scope, co-change, and the brief's
> "Related files to inspect" render. Correct-or-quiet: exclusion suppresses, never invents.
> Tests: `tests/test_factfilter_l5_classifier_fixes.py` (red→green). **Open resolver residual
> (substrate rebuilds only):** extend the T2 builtin drop to bare calls so future graphs stop
> minting `verified_unique` edges onto builtin-shadow definitions.

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

> **UPDATED (2026-06-09 — live `v1r_brief.py`): HIGH has TWO MORE gates** on top of (a)–(c),
> both shipped against confident-wrong steers (the single worst failure mode):
> - **(d) ≥2 DISTINCT issue anchors** must structurally witness the target file
>   (`_distinct_issue_anchors ≥ 2` — KGCompass multi-hop-from-issue-ENTITIES, *plural*; a lone
>   tangential CALLS edge + a weak lexical match no longer earns the imperative steer —
>   the abs-module-cache-flags fix).
> - **(e) ≥2 structural witnesses must converge on the NAMED function**
>   (`_high_func_support ≥ 2` — D-3 calibration; sh-744's lone-edge `stdout` pick, gold
>   `__await__`, demotes to the MEDIUM candidate list instead).
> Failing (d)/(e) falls through to MEDIUM — same files, same order, only the tier label drops.
>
> **Witness-render honesty (commit `dc5844f8`):** a **non-deterministic** witness edge renders
> with an explicit **`(unverified)`** tag (name_match is never displayed as bare fact);
> **grep/path/FTS5 SEEDS render as `grep match: <tok>` / `path match: …` / `fts5 match: …`**
> at conf 0.35 — never minted as `defines X (issue symbol)` (the old rendering fabricated a
> DEFINES fact for a lexical seed). Deterministic sets are single-sourced from `curation_map`.

### 4.2 Scoring weights (current, as shipped)

> **UPDATED (2026-06-09, fusion redesign `5a6e99b4` MERGED + Dim-1 compose `dc5844f8`).**
> `W_SEM=0.15` below is SUPERSEDED — the dense weight is now **dense-LED with a hard floor**:
> - **`W_SEM` default 0.40** (`DEFAULT_WEIGHTS`, `v7_4_brief.py` — the 0.15 was the e5-era
>   throttle bug, §11.8) and **`W_SEM_FLOOR=0.25`** (`GT_W_SEM_FLOOR`, clamped (0,1]) is
>   enforced **LAST**, after ALL weight adaptation: every classification ends with
>   `W_SEM ≥ 0.25 > 0` (the `forbid_no_sem_config` invariant, §11.6). The sparse-graph
>   branch also floors W_SEM instead of zeroing it (`dc5844f8`). One honest carve-out:
>   `enforce_floor=False` (embedder absent / a deliberate sem-zeroing ablation) leaves a
>   dead `W_SEM=0` at 0 — the floor asserts dense participation when the embedder is ON,
>   it never fabricates a dense signal that does not exist.
> - **Dimension 0 (query lexicality) runs FIRST** in `_adapt_weights_for_issue`: a
>   deterministic classifier → `identifier_heavy` (exact surface forms: rule codes, quoted
>   paths/symbols) = **lexical leads** (W_LEX/W_PATH floored up, W_SEM led DOWN to the floor);
>   `nl_gap` (prose) = **dense leads** (`W_SEM = max(0.40, 0.45, W_LEX)`); `mixed` = no change.
> - **Dimension 1 max-COMPOSES over Dim-0** (`dc5844f8`): under `identifier_heavy`, the
>   signal-presence dimension may only RAISE W_LEX/W_PATH (`max()`), never overwrite the
>   Dim-0 lexical lead back down; off `identifier_heavy` the original direct assignment
>   stands byte-identical.
> §11.8's "the #3 fusion redesign implements this floor" is **merged, live in
> `DEFAULT_WEIGHTS`** — no longer integrating/pending.

> **UPDATED (2026-06-10 — PATH B Tier-3b conformance audit, run 27260307167: the 3 RERANK_LOGIC
> defects fixed, all keyed to issue STRUCTURE, no task/gold logic):**
> - **① Qualified dotted/backtick anchors (§4 anchor extraction).** A dotted symbol the issue
>   names verbatim (``` `Class.method` ```) could never survive the bare-name graph cross-check
>   (nodes store bare names) and its components often died independently as homonyms — so the
>   issue's MOST SPECIFIC anchor vanished and `W_CODE_DEF` (0.70, the strongest weight) never
>   engaged; that produced the run's one confident-WRONG HIGH steer. Now `anchors.py
>   _resolve_qualified_dotted` confirms the QUALIFIED pair against the graph (parent-child join
>   → `qualified_name` exact/suffix → same-file containment); a confirmed pair is kept in
>   `symbols`/`code_symbols` (exempt from the generic-hub gate — qualification disambiguates,
>   the opposite of a homonym; unconfirmed dotted tokens stay dropped, correct-or-quiet).
>   `v7_4_brief._compute_code_symbol_scores` resolves the pair to the defining file at full
>   confidence (containing-class file as fallback) instead of 1/n-diluting the bare tail, and
>   `graph_localizer._seed_node_rows` seeds the real definition node for dotted anchors.
> - **② Exact-anchor recall guarantee covers class-likes + reporter-confirmed short names
>   (`v1r_brief._exact_issue_named_files`).** The guarantee was Function/Method-only, so an
>   issue whose TITLE names the defective CLASS verbatim (unique definition — a 1-line grep for
>   any agent) never earned it and the gold could miss every rendered slot. Labels now match
>   `_seed_node_rows`' class-like set (`Function/Method/Class/Interface` — ONE definition of
>   "definition"); the `len<5` short-name shape skip is bypassed ONLY by reporter-confirmed
>   provenance (`title_symbols`/`code_symbols` — BugLocator ICSE 2012 summary weighting), never
>   unconditionally; the dunder/generic/≤3-defining-files gates are unchanged. Both call sites
>   receive the hoisted single-source `IssueAnchors`.
> - **③ Dimension 4 — DENSE-DISPERSION gate (`_apply_dense_dispersion_gate`, run_v74 C/D
>   path).** The dense signal can reach the fusion FLAT (per-task `sem_mad=0.00000000` on 5/10
>   live tasks: all-equal cosines or 1-of-N coverage). A flat dense vector cannot ORDER the
>   candidates, but at the dense-led `W_SEM=0.40` it still arbitrarily boosts whichever 1–2
>   files carry coverage — sinking anchor-defined gold. Detection: MAD of the sem component
>   over the candidate set, normalized by the per-task max (scale-free, QPP score-dispersion —
>   Shtok et al. TOIS 2012 NQC; Cummins et al. SIGIR 2011); flat iff MAD ≤ 0.05·max
>   (`GT_SEM_FLAT_REL_EPS`). Action: W_SEM led DOWN to the floor (floored, NEVER zeroed —
>   §11.6) and the CONTENT/anchor-structural signals led up by max-compose only
>   (`W_CODE_DEF≥0.70, W_FRAME≥0.60, W_LEX≥0.55, W_PATH≥0.50, W_PROX≥0.12`; **W_REACH
>   deliberately untouched** — reach over-promotes hubs and stays subordinated). Healthy
>   dispersion → byte-identical weights. Telemetry: `V74BriefResult.sem_flat_gate_fired` +
>   `sem_dispersion_mad` (8-dp).
> Stage-1 proof: `tests/pretask/test_rerank_localization_fixes.py` (14 deterministic tests,
> 11 red→green on pre-fix code + 3 negative controls); existing fusion/anchors/v74/frame
> suites 75/75 green; the 5 failures in v1r/localizer suites are PRE-EXISTING (identical on
> pre-fix code). One legacy assertion updated to the new contract
> (`test_anchors_resolve_against_graph`: a graph-CONFIRMED dotted anchor is now kept).

**run_v74 DEFAULT_WEIGHTS:** `W_SEM=0.40 (floor 0.25 — see the update note), W_LEX=0.50,
W_REACH=0.05, W_PROX=0.05, W_HUB=0.10, W_COMMIT=0.0, W_PATH=0.45`, plus **`W_FRAME=0.60`**
(stack-trace/typed-path) and **`W_CODE_DEF=0.70`** (backtick code-symbol definition site) —
both no-op when nothing resolves.

**localizer composite:** `W_WITNESS=0.60, W_BM25=0.35, W_PATH_DECAY=0.30, W_LEX=0.30,
W_SUBJECT=0.15, W_DEGREE=0.10`, with **gen −0.5** and **test −0.4** penalties applied post-composite.
The composite feeds the *structural* ranker only; the final sort is grep-floor + 3-way RRF.
`W_CLOSURE` is intentionally **absent** (reach/closure is the wrong ranking lever).

### 4.3 The documented levers are NOT applied
The ranking change-list (W_LEX 0.50→0.60, W_REACH 0.05→0.02, W_BM25/W_PATH_DECAY/W_LEX
0.35/0.30/0.30→0.40/0.15/0.40, min-3 BM25 guarantee, caller-render conf gate, reach min_conf
0.0→0.5) are **research items, not in the binary** — current weights are the values above. They
ship only after measuring `first@5` improves, one variable at a time. Do **not** assume they're live.
*(2026-06-09: the W_SEM lever is the exception — it shipped via the §4.2 fusion redesign. The
levers listed in THIS subsection remain unapplied: W_LEX is still 0.50, W_REACH still 0.05.)*

---

## 5. Layer — Semantic / ONNX (the corrected state)

> **SUPERSEDED IN PART (2026-06-09, CHANGE 2 — commit `5f460f23`).** The model identity below is
> historical. What changed, specifically:
> - **"ONNX e5-small-v2" as the default embedder (both call sites below) is SUPERSEDED** → the default
>   is now **`Alibaba-NLP/gte-modernbert-base`** (Apache-2.0, **768-dim**, code-tuned, multilingual),
>   configurable via `GT_EMBED_MODEL_NAME`/`GT_EMBED_DIM` (`embed.py:45-46,57-58`). **e5-small-v2 (384)
>   is now the runtime FALLBACK** and remains the **pin for the sqlite-vec memory store** (not migrated).
> - **"The model (e5-small-v2 ONNX, ~90MB) is baked once" is SUPERSEDED** → the SUBSTRATE must bake
>   **gte int8 (~143MB)** to match the loader default; baking e5 alone is the audit-found mismatch
>   (3 proof surfaces disagree — `validate_proof_env` wants e5; `proof.embedder_model_path` +
>   `context.model_files_baked` want gte). Reconciled in the substrate stage.
> - **ONNX-input handling changed:** ModernBERT declares only `input_ids`+`attention_mask` (no
>   `token_type_ids`); `embed.py` now introspects `session.get_inputs()` and feeds `token_type_ids`
>   ONLY when declared. Pooling/prefix are per-model (e5 `query:`/`passage:`+mean; gte none+CLS).
> - **Proven lever (why):** per-symbol-MaxSim sibling-MAD gte-768 vs e5-384 on real graphs — Python
>   3.3×, TypeScript 6.3× better separation (biggest on non-Python = the multilingual win).
> Everything else in §5 (`_OnnxEmbedderAdapter`, `GT_FORCE_ONNX`/`GT_REQUIRE_EMBEDDER`/`GT_MODELS_ROOT`
> enforcement, the two-call-site identity) still holds — only the model identity + the bake target moved e5→gte.

> **UPDATED (2026-06-10, commit `466a0c85` — the CAPACITY INVARIANT).** The encode surface now
> carries an architectural invariant, found by LIPI on a live failure (all 5 PATH B Verified
> tasks + the VM sweep box death traced to ONE bug):
> - **Invariant: peak encode memory is O(GT_EMBED_ENCODE_BATCH), CONSTANT in repo size — never
>   O(N passages).** `_embed_prefixed` previously fed the ENTIRE passage list to one
>   `session.run`: ~1.8MB activations/passage × the 4096-passage budget ≈ 7.3GB anon-rss
>   (memcg-kill reproduced live on astropy-13236). The budget bounded COUNT; nothing bounded
>   BYTES. Now chunked at `GT_EMBED_ENCODE_BATCH=32` (~60MB peak). Padding is FIXED at 128
>   tokens, so chunking is **numerically identical** to the single call — determinism holds
>   bit-for-bit; only the resource envelope changed.
> - **Proof-surface rule (integration): a proof run MUST exercise the production surface,
>   including inputs.** The 113-task sweep ran without `GT_ISSUE_FILE` → `emit_brief` (the OOM
>   path) had ZERO coverage in 113 green rows. Sweeps run WITH issue files from now on; a green
>   proof that skipped a production code path proves nothing about it.
> - **Capacity kills are classified, never silent (plumbing):** proof containers run under an
>   explicit `--memory` cap; rc=137 → `GT_PROOF_OOM` (distinct from fail-closed exit 2) in all
>   runners. An uncapped host-OOM kill executes no fail-closed stderr print — the cap converts
>   a silent host kill into a classified container kill.

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
  **UPDATED (2026-06-09, `dc5844f8` ST-hole fix):** under the flag, "required" means the
  **CONFIGURED model, full stop** — the sentence-transformers attempt is SKIPPED in both halves
  and the e5 runtime fallback is skipped too: configured-ONNX-loads-or-RAISE (no silent e5,
  no ST hole around the ONNX surface).
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
| per edit | incremental reindex so the next view/edit sees the new graph — **see the substrate-mode note below** |

> **UPDATED (2026-06-09 — substrate/proof mode reconciliation, `ffc6c7dc` + `gt_mini_patch.py`).**
> On the DeepSWE **substrate/proof path** the "per edit: incremental reindex" row is
> **deliberately OFF**: the substrate's `/gt_artifacts/graph.db` is the AUTHORITATIVE graph the
> gates certified and the host witness fingerprinted — a `-file` reindex would mutate it (or
> fork a divergent copy) and break hook==post-LSP-hash parity, so in substrate mode L6 is gated
> off and the per-turn pillars read the ONE mounted graph unchanged (a per-task graph COPY was
> considered and rejected — it reintroduces the divergent graph the witness would fail).
> L6 reindex stays ENABLED on the non-substrate (OH / preindex/trial) paths.
> Per-turn evidence opens that graph **read-only via sqlite URI `mode=ro`** (+`immutable=1` on
> the truly-ro substrate/proof mount only — never on a mutating legacy graph), with a
> **one-time readability probe** (`GRAPH_UNREADABLE_IN_CONTAINER` printed once on first
> failure, then quiet). Per-view/per-edit evidence dedups **once per (kind, file)** —
> a documented trade vs OH's per-edit re-delivery (quieter, but a second edit to the same
> file gets no refreshed contracts).
> **UPDATED (2026-06-10, PATH B audit):** (a) every per-turn fact surface now passes the §2.5
> DELIVERY FACT-FILTER (vendored/minified/generated paths + builtin/dunder-shadow names are never
> delivered facts); (b) the one-time `[gt-patch:loaded]` loader marker now prints to **stderr**
> (harness log) — it had leaked into agent-visible stdout at MSG 3 on 10/10 tasks (telemetry,
> not agent content).

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

> **UPDATED (2026-06-09, commit `9bf106ca` + `dc5844f8` — post-LIPI gate reality).** The table
> above is the gate *inventory*; enforcement hardened as follows (verified in
> `gt_run_proof.py` / `foundational_gates.py` / `deepswe_full.yml` at HEAD):
> - **`GT_REQUIRE_LSP=1` → exit 2 on BOTH `LSP_INSTALL_MISSING` AND `LSP_FAIL_NO_WARM`** (a
>   launched-but-never-warm server is a FAILURE; a baked-language server missing on PATH is an
>   install gap, never a "valid no-op"). **Per-language certs are persisted AND aggregated**
>   (`aggregate_lsp_verdicts`): on a polyglot repo, EVERY known language must pass — **a
>   sibling language succeeding never masks another language's gap**; no language resolving at
>   all also fails.
> - **LSP cert schema v2 + version-skew=FAIL:** `gt.lsp_certificate.v2` adds
>   `install_missing_reason` + `verdict_hint`; a cert carrying NEITHER field is a v1 cert from
>   a stale binary → classified `LSP_FAIL_CERT_VERSION_SKEW`, never PASS.
> - **`GT_GATES_DELIVER_ALWAYS` is STRICT-by-default on the DeepSWE proof path**
>   (`deepswe_full.yml` pins it `"0"`: any OFF gate fails the process — the proof contract).
>   The OH live-agent path keeps `"1"` (gates as measurement: graph-quality axes never abort
>   the agent; only a DEAD embedder is fatal).
> - **`GT_REQUIRE_EMBEDDER=1` = the CONFIGURED model loads or RAISES** — sentence-transformers
>   and the e5 runtime fallback are SKIPPED under the flag (§5; no silent e5 behind a gte
>   config).
> - **`brief.txt` is the 8th REQUIRED proof artifact** (`REQUIRED_ARTIFACTS`): generation
>   raise or an empty brief = `GT_ARTIFACT_MISSING`, exit 2 — the agent consumes
>   `/gt_artifacts/brief.txt` read-only, there is NO host fallback.
> - **`run_manifest.json` is schema v2 = run shape + PROVENANCE** (`gt.run_manifest.v2`):
>   `gt_git_commit`, `substrate_digest`, `task_repo_commit`, `runtime_flags` (incl.
>   `GT_FORBID_PREBUILT_GRAPH`), `language_distribution` (real per-language node counts from
>   graph.db), `graph_db_sha256`, `cert_versions` — every field recorded-or-null, never guessed.

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

## GT-LAYER VERIFICATION PROTOCOL — WHAT TO CHECK (never call "delivered" a win)

**Origin (2026-06-05, contract-DRIFT live run, beets-5495):** claimed the drift layer
"worked" because the `<gt-drift>` block reached the agent's `output.jsonl`. It was a FALSE
POSITIVE — flagged 4 functions (`_open_state`, `history_get`, `chosen_info`, `unarchive`)
the agent never edited (agent edited only `set_fields`). Verified *delivery*, skipped
*correctness*. Delivered ≠ correct. This checklist catches it.

**Before claiming ANY GT layer works, ALL of 1–3 must hold, on a FAIR probe (4):**

1. **DELIVERED** (necessary, not sufficient) — payload appears in the agent's `output.jsonl`
   observation text (raw), not telemetry/event counts. [AGENT-OBSERVATION rule]

2. **CORRECT** (the step skipped — the one that matters) — the payload's CLAIMS match
   ground truth:
   - DRIFT: every function/contract-change reported MUST match an ACTUAL change in the
     agent's `git diff`. Cross-check `<gt-drift>` names against `git diff` / `edited_files`.
     **Any function flagged that the agent did not edit = false positive = BROKEN.** (Cause:
     `gt-index -file` re-parses the whole file, so an untouched multi-return function gets a
     different primary `return_shape` vs the full-build baseline. Fix `7ded1b36`: scope drift
     to git-diff changed line ranges.)
   - Caller counts must be real → precondition `TestResolve_QualifiedStdlibCall_NotDeterministic`
     GREEN on the run binary (no `name_match` laundered as deterministic).
   - No leakage: no test names, no FAIL_TO_PASS, `assertions` table untouched.

3. **CONSUMED** (engagement ≠ delivery) — after the payload, did the agent ACT on it
   (reference the symbol, revise an edit, restore a behavior)? Scan agent thoughts/actions
   AFTER the delivery. Zero reaction = INERT. Do NOT cite a `utilization_score` the trajectory
   doesn't justify (live run logged `util=0.5` with zero agent reaction — fired≠used theater;
   GT's own `every_next_action_has_reaction: FAIL` agreed).

4. **FAIR PROBE** (causal, not coincidence) — did GT cause it, or did the agent self-solve
   (issue traceback, own grep)? BAD PROBE if the issue text pre-localizes the gold OR the gold
   fix doesn't exercise the layer. **For DRIFT: the gold fix must CHANGE a return/raise/guard
   contract on a function with real callers, on a baseline-FAILURE task.** beets-5495 failed
   twice: issue pre-localized `importer.py:set_fields`, and the fix (`value`→`str(value)`)
   changes a call arg, not a contract → drift correctly empty → proves nothing about value.

5. **RIGHT TRAJECTORY** (the prize) — correct context → consumed → reasoned through → correct
   fix FOR THAT REASON. "Resolved" is a footnote.

**VERDICT GATE:** say a layer "works" only when 1+2+3 hold on a fair probe. Otherwise state
exactly which of {delivered, correct, consumed} passed. "Delivered" alone is reported as
**"delivered; correctness unverified"** — NEVER "works".

---

## 11. Localization redesign — findings + build plan (2026-06-09, run 27214152241)

A 30-task paid agent run (2/30 resolved, **0 GT-caused flips**, leakage 0, substrate GREEN 30/30) +
a full §4 trajectory audit + 4 design probes pinned WHY GT delivers correct-or-quiet context yet
produces no flips. The substrate (embedder/LSP/graph) is ON and consumed — the failures are in the
**ranking composition** and in **dead delivery hooks**, not the substrate.

### 11.1 The verified live localization path (the ONE path)
`oh_gt_full_wrapper → generate_v1r_brief` (`v1r_brief.py:2202`) → **`run_v74`** (`:2239`,
hand-weighted LINEAR SUM `_total_score` `v7_4_brief.py:438-470`) → `ranked_full` (`:1261`) →
`top_records` (`v1r_brief.py:2286`) → `entries` → **`.files`** (`:3051`, what the agent sees).
`graph_localizer.localize()` (`:2451`) is **enrichment only** — it reorders/injects verified-witness
files; it does NOT replace the ranking. `v22_brief`, `graph_map`, the `v2_ranker`/`v8_governor`
chains are **dead on this path** (referenced only by old scripts). `run_v74`'s `focus_set` is computed
but never feeds `.files`.

### 11.2 Root finding A — semantic signal is throttled by GRANULARITY, not weight
The embedder DOES reach `.files` (via `W_SEM=0.15`·sem in the linear sum + a tertiary `_rrf3` tie-break
in localize), but it's near-flat: GT embeds **ONE vector per FILE** from a ≤600/2000-char bag of ≤60–80
symbol `name+signature` (`anchor_select.py:212-258`, `graph_localizer.py:1393-1431`). Sibling files in
a module share that vocabulary → cosines cluster at **0.80–0.84** (code-documented `mad=0.0145`,
`_SUMMARY_VERSION="sym1"`; a prior version hit "0.83886 ×9 collapse"). **Maxing `W_SEM` cannot fix flat
cosines** — the discriminating function is 1 of 60, averaged into noise.
**Fix (CHANGE 1):** embed **per-symbol** (`name+signature+body-snippet`, ≤80 tok); file score =
`0.7·max_i(cos_i) + 0.3·mean(top-k cos_i)`. ColBERT MaxSim (Khattab & Zaharia, SIGIR 2020) + MaxP/Birch
(Dai & Callan, SIGIR 2019). Keep dict[file→float] contract; demand-scope to candidate files; cache by
node-content hash; bump `_SUMMARY_VERSION`. **Validatable at 384-dim alone** (proves granularity is the
lever before any model change).

### 11.3 Root finding B — the embedder is general-text, not code-tuned
`intfloat/e5-small-v2` (33M, 384-dim, general-text) under-discriminates code. **Fix (CHANGE 2):**
`Alibaba-NLP/gte-modernbert-base` (149M, **768-dim**, **Apache-2.0**, ONNX published, CoIR ~71.5 vs
e5-base ~51). Code-IR evidence: CoIR (arXiv 2407.02883, 2024), CodeXEmbed (arXiv 2411.12644, 2024),
CodeSage (ICLR 2024). Risks: `embed.py:82-91` feeds `token_type_ids` unconditionally (ModernBERT's ONNX
doesn't declare it → must introspect `session.get_inputs()`); drop e5's `query:`/`passage:` prefixes;
int8-quantize (~150MB); **pin the sqlite-vec memory store to e5/384** (separate subsystem — don't
migrate). Fallback e5→ZeroEmbedding (correct-or-quiet).

### 11.4 Root finding C — the completeness signal fires into a DEAD hook
The wrapper runs the **OH 0.54 controller** path: `AgentFinishAction` sets `state=FINISHED` **before**
the patched `run_action` → the **finish handler is a dead write** (`emitted=False,
suppressed=finish_handler_dead_write`; `safety/governor.py:80-87`). The leak-free multi-file scope check
(`_check_multi_file_scope`, conf≥0.7 call-graph) is wired into that dead handler — **so aiogram-1594's
correct scope warning never reached the agent. Not missing logic, a dead hook.** `_consensus_scope` /
`_consensus_scope_edited` are already tracked + leak-free (call-graph neighbors ≥0.7, test files
excluded, zero gold/FAIL_TO_PASS).
**Fix (#4, highest FLIP leverage):** re-route the K-of-N completeness warning to the LIVE
`_maybe_fire_presubmit_verify` hook (`oh_gt_full_wrapper.py:1186`, the edit→review transition that DOES
reach the agent); make the one-shot "no source edits" nudge ESCALATE (the cfn-lint-3875 empty-patch
loop), capped at 3 and yielding to OH's stuck detector (preserve the 2026-05-25 stuck-compat skip).

> **STATUS (2026-06-11): STILL OPEN.** `_check_multi_file_scope` remains dead-hooked (`governor.py:424`
> call only from `_handle_finish`). The Stage 3 oracle work (§15.4) re-routes the review-transition
> emission for obligations only — K-of-N multi-file scope is not ported. GAP_ANALYSIS §11 build plan
> confirms: "conspicuously absent from §11.8's status block." §16.5 issue B (review-transition 0/9) is
> the blocking symptom; #4 completeness is a named instance of the same gap.

### 11.5 Root finding D — fusion is not query-adaptive + non-code gold is invisible
`_adapt_weights_for_issue` (`v7_4_brief.py:46-157`) adapts W_FRAME/LEX/PATH/REACH/PROX but **never
W_SEM**. SWE-bench-Live cfn-lint issues quote machine-checkable strings (rule codes `E1010`, paths) →
lexical is decisive there; NL-gap issues favor dense. Evidence: BEIR (NeurIPS 2021), DPR (EMNLP 2020),
Sciavolino entity-centric (EMNLP 2021). And the Go indexer has **no `.json` spec** (`walker.go`) →
schema/data gold (`policy.json`) has zero nodes → invisible to `localize`/FTS5 (though `run_v74`'s
lexical walk already reads `.json`).
**Fix (#3):** deterministic query-type detector (error-code regex `\b[A-Z]\d{3,5}\b` + quoted
paths/code-symbols vs NL-prose ratio) as "Dimension 0" → identifier_heavy = lexical-lead, nl_gap =
dense-lead, mixed = no change; gated non-code candidate rescue (issue-term ∩ path-component) + a
durable `.json` indexer spec.

### 11.6 Cross-cutting constraint (all levers)
`forbid_no_sem_config` (`proof.py:328-340`) RAISES if `effective_w_sem ≤ 0` under proof+require_embedder.
Fusion/granularity changes must down-weight dense to a **floor > 0**, never zero it.

### 11.7 LLM-free boundary
GT borrows only DETERMINISTIC primitives from the SOTA (repo code-graph — RepoGraph ICLR 2025; MaxSim;
RRF). The LLM-agent localizers (Agentless 2024, LocAgent/OrcaLoca 2025) are NOT adopted — they add a
generative LLM, which the ONE-PRODUCT / $0-AI rule forbids.

### 11.8 Prioritized build plan (each Stage-1 deterministic before any flip claim)
1. **CHANGE 1 — symbol-level granularity** (§11.2). Root fix for flat cosines; low risk; e5/384.
2. **#4 — re-route completeness to live hook + escalating loop-breaker** (§11.4). Highest flip
   leverage; logic mostly exists (dead-hooked).
3. **#3 — query-adaptive fusion + non-code inclusion** (§11.5). Stage-1 correctness; cfn-lint + JSON.
4. **CHANGE 2 — e5→gte-modernbert swap** (§11.3). Headroom; higher risk (ONNX `token_type_ids`, 768d).

Validation for each: sibling-pair cosine-MAD harness (for A/B), red→green leak-free K-of-N + loop-break
tests (for C), classifier + candidate-inclusion tests (for D); ≥3 repos/languages (anti-overfit);
8-dp deep logs; then paired Wilcoxon vs the frozen `FINAL_resolved_300_20260531.json`.

**Status (2026-06-09):** CHANGE 1 (per-symbol MaxSim granularity) **DONE + committed**, Stage-1
validated at e5/384 (gold #1 3/7→7/7; synthetic separation 0.67 vs 0.02; 8/8 + regression green) —
proves granularity is the lever WITHOUT a model swap. **Dense-weight policy LOCKED:** dense is
dense-LED with a **substantive `W_SEM_FLOOR` (>0)** — query-adaptive may flex W_SEM toward the floor on
identifier-heavy issues (lexical leads, per BEIR/Sciavolino) but NEVER throttles it below the floor
(the e5-era 0.15 bug); RRF protects dense by rank. NOT a monopoly. The #3 fusion redesign implements
this floor; CHANGE 2 (gte-modernbert swap) then layers model headroom on the proven granularity.
**UPDATE (2026-06-09): #3 is MERGED** (`5a6e99b4` fusion+floor; `dc5844f8` Dim-1 max-compose +
sparse-graph floor) — `W_SEM=0.40` default + `W_SEM_FLOOR=0.25` enforced last are live in
`DEFAULT_WEIGHTS` (§4.2), and CHANGE 2 is committed (`5f460f23`, §5 banner). Not
"integrating/pending" anymore.

**UPDATE (2026-06-10 — Tier-3b audit closure, see the §4.2 2026-06-10 note for full detail):**
the flat-cosine symptom of §11.2 RE-APPEARS at the FUSION INPUT (live: `sem_mad=0.00000000` on
5/10 PATH-B tasks with the embedder cert green — granularity fixed the embedding level, not the
per-task arrival). Closed by **Dimension 4, the dense-dispersion gate** in `run_v74`
(`_apply_dense_dispersion_gate`): scale-free MAD flatness detection (QPP — Shtok TOIS 2012 NQC /
Cummins SIGIR 2011) → W_SEM led to the floor (§11.6 held, never zeroed) + content/anchor-
structural max-compose lean (no W_REACH raise — reach stays subordinated). Plus two §4
extraction/recall fixes: qualified dotted/backtick anchors (`_resolve_qualified_dotted`,
qualified `_compute_code_symbol_scores`, qualified localizer seeds) and the class-like +
provenance-aware exact-name guarantee (`_exact_issue_named_files`). All three keyed to issue
STRUCTURE (backtick/dotted form, exact-anchor-defines-file, flat dispersion) — zero task/gold
logic; Stage-1 red→green in `tests/pretask/test_rerank_localization_fixes.py`.

---

## 12. PER-LAYER ROLE + SUCCESS CRITERION — judge each layer by ITS job, never a generic template

A recurring audit error (2026-06-09): judging every layer by a generic DELIVERED/CONSUMED template and
trusting audit layer-labels, instead of grounding WHAT EACH LAYER IS and judging it by its own
criterion. That produced wrong verdicts (L4 "no-op", L6 "DELIVERED=NO", cert "FAIL"). **Before judging
any layer, look it up here and apply ITS criterion.**

| Layer | What it IS | Correct success criterion | Do NOT mislabel as |
|---|---|---|---|
| **L1** | file RANKER (run_v74 linear sum → `.files`) | gold ranked high AND reached by the agent (fairly, not via issue pre-localization) | "broken localizer" when flat cosines are a **granularity** symptom (§11.2) |
| **L3b** | contract pillar (post-view signatures) | DELIVERED + CORRECT + **relevant to the bug locus** + CONSUMED | "CONSUMED≈0 = agent ignores it" — it's relevance (`start_line` fallback) + post-edit LSP-strip (§11.4 reindex) |
| **L4** | **EVENT hook** (fires on a specific agent event) | its EVENT occurred AND it fired with correct content | "no-op / conditional / dead" when its **event simply didn't occur** in the trajectory |
| **L5 / L5b** | trajectory governor / intervention | the nudge DELIVERED at a LIVE hook AND strong enough to change behavior | "DELIVERED=NO" — the scaffold nudge IS delivered (`emitted=True`); only the over-gated goku family + the L5b goku-deferral are truly undelivered |
| **L6** | **post-edit REINDEXER** (+ presubmit-verify) | reindex fired + updated graph.db + **preserved LSP enrichment** + fresh graph reaches later queries | "DELIVERED=NO / dead" — it's a reindexer, not a deliver-text layer; it fires + works; its real bug is LSP-strip on `-file` reindex |
| **gates / certs** | proof artifacts | reconcile against the **runtime witness** before reporting a FAIL | `GRAPH_FAIL_MISSING_HANDOFF` is a FALSE FAIL — cert is pre-agent; the `graph_witness` proves the handoff |

**Mandatory audit protocol (mine + any spawned agent):** (1) ground each layer's ROLE from this table
FIRST; (2) judge by ITS criterion, never a generic delivered/consumed frame; (3) reconcile any
cert/telemetry FAIL against the runtime witness (`graph_witness`, `output.jsonl`) before calling it
broken; (4) no claim from n < a real sample (the n=2 latency error). "Fired ≠ delivered ≠ consumed ≠
working" — and **"delivered" is the WRONG axis for a reindexer or an event hook.**

> **UPDATED (2026-06-09, commit `10368a2f`): the L6 row's "real bug is LSP-strip on `-file`
> reindex" is FIXED at the store level** — the incremental restore now preserves all 11
> deterministic methods (incl. `lsp`) verbatim on a single-file reindex (§2.5), so LSP
> enrichment survives L6. Two residuals stay true: (a) NEW edges created by the edit still
> resolve structurally only until an LSP server runs (the bake-pyright item, §13.5);
> (b) on the DeepSWE substrate/proof path L6 is **gated OFF by design** (authoritative
> read-only graph, hash parity — §6 note), so "L6 fired" is the wrong expectation there.

> **UPDATED (2026-06-10, PATH B audit run 27260307167): the L5 row's mini-swe port
> (`gt_mini_patch.py`) was BROKEN on two of its three arms and is now classification-gated.**
> The audit measured: `failure_persisted` 1/7 substantively correct — **5 false positives on
> ENVIRONMENT errors** (pip/C-ext/import/py-version shims) told agents with CORRECT fixes "your
> hypothesis is likely wrong", and one firing (django-10097) reinforced reverting a
> **gold-equivalent edit** that had only been checked against a scratch script + STALE visible
> fixture; the `loop` nudge false-fired 1/1 (same command, NEW state each run). Fix
> (Cursor-mentality, correct-or-quiet — parity with the OH governor's
> `classify_observation.is_env_failure` suppression, `governor.py:307`):
> `failure_persisted` now requires ALL of — (1) a real **test-runner invocation**
> (pytest/unittest/runtests.py/manage.py test/go test/cargo test/npm test/jest/… — a scratch
> script's failure can NEVER falsify a hypothesis), (2) **no env/tooling failure marker**
> (ModuleNotFoundError/pip/build/link/network/ImproperlyConfigured/module-attr shims), and
> (3) an explicit **test/assertion FAILURE marker** (bare `Traceback`/`Error:` no longer
> qualifies); uncertain → SILENT. The `loop` nudge signature is now
> **(command, normalized observation)** — it fires only on proven NO-NEW-STATE repetition.
> `scaffold_trap` (4/5 true-positive in the audit) is unchanged. Tests:
> `tests/test_factfilter_l5_classifier_fixes.py` (red→green, incl. the 10554 runtests.py
> true-positive shape still firing).

---

## 13. Session 2026-06-09 — work done + the DeepSWE/mini-swe-agent pivot (FULL-depth, multilingual)

### 13.1 What shipped today (pushed to origin/harneet2512 + hbali-stack gt-trial)
- **Proof/infra:** LSP stamping fix (`9e7edeca` — gate counts lsp_resolved from the FINAL graph + cross-checks the cert; measurement-only, graph==cert proven on 6 tasks); image-pull retry hardening (`dff3144b`, `TASK_IMAGE_PULL_FAIL`); **brief-consume fix (`8d48360a`)** — the agent consumes the in-container `/gt_artifacts/brief.txt`; no host `run_v74` in proof (closed the host-split + dead-finish issue).
- **30-task PAID agent run (`27214152241`):** 30/30 ran, 0 fail, **2/30 resolved** (sh-744, beancount-931), **0 GT-caused flips** (both baseline-coincident self-localizations), **leakage 0**, substrate GREEN 30/30.
- **§4 trajectory audit — 30 ledgers in `task_ledgers/`:** dominant non-resolution = **post-localization implementation correctness + multi-file scope**; L1 mislocalizes the majority (granularity + JSON/data blind spot); leakage 0 everywhere.
- **CHANGE 1 — per-symbol MaxSim granularity (`33970b9f`):** fixes the whole-file-bag flat-cosine collapse; validated **gold #1 3/7→7/7 at e5/384** (granularity is the lever WITHOUT a model swap). See §11.2.
- **Fusion + dense floor (MERGED `5a6e99b4`; Dim-1 compose + sparse-floor `dc5844f8`):** `W_SEM_FLOOR=0.25`, base `W_SEM 0.15→0.40`; query-adaptive Dimension-0 (error-code regex → identifier-heavy lexical-lead; nl_gap dense-lead); dense led/floored, never throttled, lexical-fused. 15/15 tests. See §4.2/§11.5/§11.6.
- **Docs/cleanup:** dead-shim removal (`specificity.py`, `db267869`); gt_gt §11 (findings+plan), §12 (per-layer role table — anti-mislabel safeguard), §11.8 (dense-floor LOCKED).
- **Diagnostic corrections (from code+artifacts, not labels):** L6 = REINDEXER, fires+works but STRIPS LSP (`gt-index -file` is structural-only; pyright absent from the TASK image → post-edit contracts degrade LSP→AST); L3b = relevance bug (`start_line` fallback) + the L6 LSP-strip; L5/L5b scaffold nudge IS delivered (§4 "DELIVERED=NO" was a mislabel) — genuine non-delivery = goku band/cap deadlock + L5b defer-to-goku + the orphaned `multi_file_scope_warning` (dead finish handler); `GRAPH_FAIL_MISSING_HANDOFF` = FALSE FAIL (cert pre-agent; runtime witness proves handoff); **L4 = EVENT hook** (fires on its event; absence ≠ no-op).

### 13.2 The PIVOT: OH+GT (SWE-Live-Lite) → mini-swe-agent+GT (DeepSWE / Datacurve), multilingual
- **Benchmark:** drop SWE-Live-Lite (low mindshare) + skip **saturated** SWE-bench Verified → **Datacurve DeepSWE** (`github.com/datacurve-ai/deep-swe`): **113 tasks / 91 repos / 5 languages (TS/Go/Rust/JS/Python)**, contamination-free (newly-written, not from commits), **unsaturated** (leaderboard 5–70%; gpt-5.5 70%, claude-opus-4.8 58%), long-horizon (~5.5× more code), **Harbor/Pier harness**, frontier attention. Right fit for GT: contamination-free → flips are real; unsaturated → headroom; polyglot → exercises the language-agnostic mandate.
- **Harness CONFIRMED from project code** (`scripts/swebench/gt_deep_metrics.py:117,120`): **DeepSWE = `pier` + `mini-swe-agent`.** GT integrates with mini-swe-agent (GTMiniSweAgent), NOT OpenHands.

### 13.3 The TWO-ENGINE problem (a latent ONE-PRODUCT-RULE violation) — UNIFY mandate
Read from code (not CLAUDE.md):
- **OH path:** `oh_gt_full_wrapper.py` → `v1r_brief.generate_v1r_brief` → `run_v74` + `anchor_select`/`graph_localizer` — the **DEEP, multilingual** (tree-sitter graph + ONNX embedder) engine where **CHANGE 1 + fusion+floor + the embedder live.**
- **mini-swe/DeepSWE path TODAY:** `run_mini_gt_hooked.py` → `graph.db` → **`gt_intel.py`** (sqlite + `ast`, the 7 evidence families, **NO embedder / NO semantic ranker / NO v1r**) + `gt_hook.py` (173KB, `ast` + graph.db-backed analyze via gt_intel). **The levers are NOT here.** And `ast` = **Python-only** → wrong for 5-language DeepSWE.
- **MANDATE:** do NOT fork the product or port fixes between two engines. **UNIFY the DeepSWE/mini-swe path onto `v1r_brief`/`run_v74`** (the deep, multilingual engine). This satisfies the ONE PRODUCT RULE and makes CHANGE 1 + fusion+floor + the embedder apply to DeepSWE by construction.

### 13.4 Integration requirement: FULL OH depth (and more), language-agnostic, on mini-swe-agent
User directive: **full OH depth, and more.** Bring the WHOLE of GT (§1–10), not just localization, to mini-swe-agent:
- Layer-0 graph base (gt-index tree-sitter, 30 langs) → LSP enrichment (§3) → localization+brief (§4 + CHANGE 1/fusion/embedder) → semantic/ONNX (§5, → gte-modernbert) → the hooked tool surface (§6) → the no-silent-fallback gates (§7) → the proof runtime.
- **OH deep hooks to replicate on the mini-swe-agent loop:** L1 brief delivery, L3b contracts (post-view), **L6 post-edit reindex preserving LSP** (bake pyright in the task image), consensus (`<gt-scope>`), L5 governors, completeness forcing. Map onto mini-swe-agent's (simpler than OH's controller) injection points — **port CAPABILITIES, not OH-specific plumbing** (the dead-finish-handler workaround, the OH stuck-detector are OH artifacts, do not carry them over).
  **UPDATED (2026-06-09): the L6 item is reconciled, not ported as-is** — in substrate/proof
  mode L6 single-file reindex is **deliberately OFF** (the mounted graph is authoritative +
  read-only; mutating it breaks witness-hash parity — §6 note), and the restore-level
  LSP-strip is fixed in the indexer itself (`10368a2f`, §2.5/§12), so "preserving LSP" no
  longer requires a live per-edit reindex on the proof path. Per-turn evidence reads the ONE
  mounted graph via `mode=ro(+immutable)` with a one-time readability probe, deduped
  per-(kind,file)-once.
- **Language-agnostic (CLAUDE.md mandate):** tree-sitter graph + multilingual embedder everywhere; **retire the Python-`ast` paths** (`gt_intel`/`gt_hook` ast) for the DeepSWE engine.

### 13.5 Docker imaging of GT — REQUIRED for DeepSWE too
GT already has a portable substrate image from the proof work: `ghcr.io/hbali-stack/gt-substrate@sha256:…` (bakes the GT package + static gt-index tree-sitter+FTS5 + ONNX embedder + node/pyright + model + the `gt-run-proof` entrypoint). For DeepSWE the image must:
1. **Swap e5 → gte-modernbert** (CHANGE 2; multilingual, 768-dim, int8 ONNX).
2. **Bake pyright + the per-language LSP servers (gopls, rust-analyzer, tsserver/typescript-language-server) into the image** — so the post-edit reindex (L6) **preserves LSP across all 5 languages** (the LSP-strip fix; critical for polyglot — §11.4 / 13.1).
3. Be runnable inside the **Pier/Harbor task environment** (DeepSWE's `task.toml` + `environment/Dockerfile`), emitting the same proof certs.
This Docker imaging is in scope for the DeepSWE integration, not only the OH proof path.

### 13.6 Build order (each Stage-1 deterministic before any flip claim)
1. **Integrate the validated fusion+floor into `v1r`** (done-pending).
2. **CHANGE 2** — gte-modernbert multilingual ONNX swap + bake into the substrate image + the per-language LSP servers.
3. **UNIFY the DeepSWE/mini-swe path onto `v1r_brief`/`run_v74`** — full OH depth (brief, contracts, reindex, consensus, completeness) on mini-swe-agent in the Pier harness, language-agnostic; retire the `gt_intel`/`gt_hook` ast paths.
4. **DeepSWE substrate Docker image** (gte-modernbert + multilingual LSP) wired into Pier.
5. **Validate** — GT-off baseline on DeepSWE + paired GT-on (Wilcoxon), Stage-1 deterministic per lever, across all 5 languages (anti-overfit / language-agnostic proof).

### 13.7 2026-06-09 hardening — the 4-reviewer LIPI audit → 4 fix surfaces (+ substrate rebuild)

Four parallel LIPI reviewers (pipeline+gates / localization / delivery / indexer) audited the
whole DeepSWE-proof stack and isolated **62 findings**; everything actionable shipped as **four
fixer commits** (each red→green proven), plus the gopls launch fix that preceded them:

| Surface | Commit | What it closed (verified at HEAD) |
|---|---|---|
| LSP launch | `8ae5584d` | gopls launched with a nonexistent `-stdio` flag → exit-2 before handshake; server stderr now surfaced |
| Pipeline + gates | `9bf106ca` | the P0 green-zero-run chain (below); OH workflows pin `GT_EMBED_MODEL_NAME=e5` (gte stays on substrate); `LSP_FAIL_NO_WARM`/`LSP_INSTALL_MISSING` exit 2; per-language certs + aggregation (no sibling masking); cert schema v2 + version-skew FAIL; `GT_GATES_DELIVER_ALWAYS` strict-by-default on DeepSWE (§7 note); 22 red tests → 226 fail_closed pass |
| Localization | `dc5844f8` | model-keyed embed cache (gte↔e5 switch = miss); sparse-graph W_SEM floor (never 0); witness provenance honesty (`(unverified)` tags + `grep/path/fts5 match:` seeds, §4.1); Dim-1 max-compose (§4.2); ST hole (configured-ONNX-or-raise, §5); 15/17 red→green, 292 regression pass |
| Delivery (per-turn) | `ffc6c7dc` | 5 basename-LIKE pillar queries → exact normalized-relpath (zero cross-attribution); caller counts deterministic+conf≥0.7+non-test ("N verified caller(s)", legacy abstains); signature sanitizers at render sites; `_connect_ro` (mode=ro, immutable on substrate only) + one-time `GRAPH_UNREADABLE_IN_CONTAINER` probe; `DEEPSWE_ADAPTER_FAIL` printed before every raise; 26 fail → 52 pass |
| Indexer | `10368a2f` | 8-bug batch: Go CHA arity matching, 1.9 rung reorder (typed rungs first for qualified calls), deterministic `-file` restore (11 methods, lsp survives), verified-only AND-rule closure (dagster −19.7% closure rows, zero deterministic lost), File-anchor phantom guard, field_read call-selector guard, relationship/API trust stamping, sorted 1.94 pick (§2.3/§2.5) |

**The P0 green-zero-run chain is fail-closed END-TO-END** (each link verified in
`deepswe_full.yml`/`gt_run_proof.py`/`deepswe_outcome.py`): (1) **empty-issue extraction** —
issue read from `instruction.md`, empty → `GT_ISSUE_MISSING`, fail-closed (no silent no-issue
run); (2) **pier swallow** — a `DeepSweAdapterError` pier ends rc=0 on is surfaced by the
jobs-dir grep → `DEEPSWE_ADAPTER_FAIL`; (3) **tee swallow** — `set -o pipefail` +
`${PIPESTATUS[0]}` so `pier | tee` reports pier's rc, not tee's 0; (4) **presence-grep** — the
summary parses the `n_agent_steps` VALUE and requires >0 (the old check counted mere token
presence, so a 0-step run summarized as "agent-ran").

**Aftermath:** the substrate image was rebuilt on the fixed stack (`02b02425` — the image bakes
gt_run_proof/resolve/gates/pretask/gt-index, so the runtime ran PRE-fix code until the rebuild
published a new digest); wave-2 re-fired (`0e2489cc`); the **5-language smoke EXECUTED 5/5**
(`4253da65`, run 27249519490 — identical gt-run-proof command, exit 0 each, warm LSP; all
NO_OP_VALID_WITH_WARM_SERVER because the fixed indexer resolves the tiny fixtures structurally;
real-repo ACTIVE LSP resolution is the 113-sweep's question). Remaining: 113 sweep →
integration audit → 1-task dry → benchmark decision (D2).

---

*End of §1–§15 — gt_gt.md. Localization deep internals: `BRIEFING.md`. Benchmark operation/gates:
`BENCHMARK_RUNBOOK.md`. Fix history: `we_did.md` (legacy).*

---

## 2026-06-10 — TRIAL-FIXES CHANGELOG + WHAT IS SUPERSEDED (PATH B Verified trial, run 27260307167)

The PATH B 10-task Verified trial (deepseek-v4-flash) + its gt_trial.md §4/§5 + Tier-3b audits surfaced
5 defects (substrate GREEN 10/10; all defects in the rerank/delivery/governor consumer stack). Fixed in
commit `7d48304f` (Stage-1 stabilized: 111 tests green, RED→GREEN proofs; Stage-2 paired validation still
owed). **SUPERSEDED, explicitly:**

1. **§4.2 fusion / §4.1 anchors — SUPERSEDED for 3 behaviors:**
   - OLD: dotted/backtick issue symbols (`` `Class.method` ``) were dropped (bare-name graph cross-check +
     homonym hub-gate killed them) → W_CODE_DEF never engaged. **NOW:** `_resolve_qualified_dotted`
     confirms the qualified pair against the graph → engages W_CODE_DEF on the defining file.
   - OLD: the exact-issue-named-file recall guarantee was `Function/Method`-only and skipped short names →
     a title-named gold CLASS never entered the candidate slots. **NOW:** covers `Class/Interface` +
     provenance-confirmed short names (ambiguity gates + promote caps unchanged).
   - OLD: a FLAT dense signal (`sem_mad≈0`) at dense-led `W_SEM=0.40` still arbitrarily boosted
     coverage-carrying files, sinking anchor-defined gold. **NOW:** Dimension-4 `_apply_dense_dispersion_gate`
     leads W_SEM to its floor (never zeroed) + max-composes structural signals when dense is flat.
   - NOT changed: `W_REACH` (hub over-promotion risk — stays subordinated per the original §4.2).
2. **§2.5 / §6 delivery — SUPERSEDED:** OLD: vendored/minified files + builtin/dunder-shadow names could
   render as `[WITNESS]`/`[CALLERS]`/contract FACTS ("isinstance: 1048 verified callers — preserve this
   interface"; minified-jquery callers). **NOW:** consumer fact-filter excludes them at every delivered-fact
   point + recomputes caller counts excluding vendored caller files. (Residual: `resolver.go` builtin-drop is
   qualified-call-only; the consumer filter is the operative guard on FROZEN substrate graphs — flagged for
   the next substrate rebuild.)
3. **§12 L5 row — SUPERSEDED:** OLD: `failure_persisted` fired on bare `Traceback`/`Error:` (5/7 false
   positives on ENV errors; 1 HARM — pushed the agent off a gold-equivalent edit, django-10097); the `loop`
   nudge fired without proven no-new-state. **NOW:** failure_persisted requires real-test-runner + no-env-marker
   + explicit-test-fail (else silent); loop arm gated on no-new-state; `scaffold_trap` unchanged (was working 4/5).
4. **Delivery hygiene:** the `[gt-patch:loaded]` loader banner (leaked into agent-visible stdout 10/10) →
   moved to stderr.

NOT superseded / re-confirmed by the trial: the substrate (LSP warm + edge-conversion 10/10, embedder gte-768
separating, graph det% 68–74, FTS5), the legitimacy gates, zero benchmark-leakage, the §12 `GRAPH_FAIL_MISSING_HANDOFF`
false-fail reconciliation. Unexercised (substrate path): L4, L6, GT_VERIFY — health UNPROVEN, to be tested on the
DeepSWE/multi-language path.

## 14. DESIGN NOTE (2026-06-10, design-track — NOT BUILT): Consistency-pillar extension for feature/greenfield issues

**Source verdict:** `.claude/reports/DEEPSWE_NONPYTHON_DEBUG_CONSUMPTION_VERDICT_20260610T2240Z.md` —
consumption≈0 on the sub-L1 layers is ~60% "(d) wrong-KIND-of-context for feature work": the L3
post-edit contract narrates the EXISTING graph ("`parse`: 8 callers — preserve this interface", boa
[69]/[85]) while the agent is ADDING code; in ADD-mode there is nothing for a preserve-interface
contract to bind to. The killing boa defect was new-API SHAPE (`&mut self` constructors on a
non-mut `Context`) — sibling accessors are `&self`-shaped, a graph-derivable fact GT never delivered.

**Why this is design-track, not a surgical fix (assessed against the existing code, 2026-06-10):**
1. The in-container delivery layer (`artifact_deepswe/gt_mini_patch.py`) is ISSUELESS BY DESIGN
   (":710 — per-view has no issue-anchor context; the host brief owns issue-anchored selection") and
   nothing under `artifact_deepswe/` reads `gt_issue_anchors.json`. "When the issue is
   feature-shaped, lead with siblings instead of the contract" therefore needs a NEW host→container
   channel (serialize issue-shape + `unresolved_code_symbols`; ship + read it) — new plumbing, not a
   connect of two existing things.
2. The existing sibling evidence is NAMES-ONLY (`_sibling_context` → "cancel, is_cancelled, reason";
   post_view-only). The consumable form per the verdict is sibling SHAPE — signatures + receiver
   mutability + return shape. That is a NEW renderer over `nodes.signature` + `properties`, not a
   reuse of the computed string. A names line would not have prevented the boa `&mut self` defect.
3. Dimension-0 (`_classify_issue_lexicality`, v7_4_brief.py) classifies LEXICALITY for fusion
   weights; feature-vs-bugfix is a NEW classification output with its own gate. (A prior one-shot
   attempt built an NL feature-verb regex and was REVERTED for undefined-variable bugs; any NL-verb
   signal must be tertiary, never the gate.)

**The proposal (Consistency-pillar extension — CLAUDE.md "structural twins, parallel patterns"):**
- **Detector (dynamic, hybrid, graph-grounded):** issue-shape from ≥3 deterministic signals —
  (1) greenfield ratio `|unresolved_code_symbols| / |code-provenance tokens|` (the committed anchors
  tier: high ratio = the named API does not exist yet), (2) anchor file-scope = 0 resolved defining
  files, (3) absence of traceback frames (Dim-1 already computes `frame_scores`). Threshold from
  per-task distributions, not hardcoded.
- **Delivery, feature-shaped issues only (correct-or-quiet elsewhere):**
  (a) **closest-analog re-surfacing** — the §11.2 per-symbol MaxSim vectors already exist; rank
  existing functions/files by MaxSim against the unresolved symbols + requirement clauses and
  re-surface the top analog AT FIRST-EDIT TIME ("the cancel/reason pattern you are adding exists at
  abort/mod.rs — mirror it"; boa had this at brief rank 6, turn 1, never re-surfaced);
  (b) **sibling-shape block at post-edit** — for the edited file, render sibling SIGNATURES +
  receiver/mutability/return-shape properties framed as "shape what you add like its siblings",
  REPLACING (not adding to) the preserve-interface caller framing for that edit;
  (c) the preserve-interface contract keeps firing unchanged on non-feature issues (its guard-rail
  role per §12 is not consumption-scored).
- **Research:** example/analog retrieval grounds new-code correctness — Strathcona (Holmes & Murphy,
  ICSE 2005: structural context → example recommendation), MAPO (Zhong et al., ECOOP 2009: mined API
  usage patterns), Aroma (Luan et al., OOPSLA 2019: structural code-recommendation), RepoCoder
  (Zhang et al., EMNLP 2023: repo-level similar-snippet retrieval improves generation). The verdict's
  own ledger data: the one consumed payload class matched the agent's CURRENT decision (go [70]) —
  delivery must intersect the decision moment, hence post-edit/first-edit, not turn-1 narration.
- **Stage-1 proof before any flip claim:** deterministic fixtures where a feature-shaped issue +
  sibling set yields the exact sibling-shape block (and bug-shaped issues yield the unchanged
  contract), ≥3 languages; then the §4 ledger consumption check on a paired run.

**Explicitly NOT done now:** no new channel, no detector, no renderer were built — minimal-diff rule;
this section is the build spec for a dedicated session.

---

## 15. The Stateless Deterministic Delivery Oracle (2026-06-10)

> **Design of record:** `ORACLE_ARCHITECTURE_PLAN.md` (full spec). Evidence base:
> `RESEARCH_BETTER_CONTEXT.md` (7-claim literature pass), `ADVERSARIAL_REAUDIT_27307362054.md`
> (the diagnosis), `task_ledgers/RUN_27307362054_SUMMARY.md` (the 10-task run),
> `FLIP_LEVERS_ON_THE_TABLE.md` (lever inventory). DESIGN — not built; staged plan in §15.4.
> Every citation in this section is fully documented in the consolidated REFERENCES section
> at the bottom of this file.

### 15.1 The diagnosis that motivates it (adversarial re-audit, run 27307362054 — verbatim numbers)

> **UPDATED (2026-06-11):** The 13-claim adversarial fact-check (`LAYER_REPORT_FACTCHECK.md`) confirmed
> these numbers are ground truth and that the intermediate LAYER_CONFIRMATION report inflated them.
> The oracle run (27321848581) reproduced the same ~80%-inert pattern: ~62 delivered units/4 tasks;
> 4–5 consumed-class out of 9 tasks (always where the nudge hit the right obligation); the harm
> class recurred (aiomonitor tailwind detour suppressed this run only because the agent didn't follow
> the brief entry, not because it was absent). See §16.2 for the paired oracle metrics.

From the chronological raw-trajectory re-audit of 4 of the run's 10 tasks (~62 delivered GT
payload units, `ADVERSARIAL_REAUDIT_27307362054.md` "Quantified bottom line"):

| Class | Share | What it was |
|---|---|---|
| **Consumed AND useful** | **~2–5%** | exactly **ONE firm item**: the `no_test_evidence` governor nudge (boa) — short, active, delivered AT THE DECISION POINT (after a 120s zero-output `cargo test`); it changed the agent's verification behavior |
| **Delivered, correct, inert** | **~80%** | nearly all post_view witnesses, scope blocks, contracts — factually fine, zero demonstrable effect on any decision |
| **Wrong / harmful** | **~15%** | 2 HIGH-confidence wrong localization pins (inverted confidence), a fabricated cross-language scope chain (consumed → ~350KB context bomb), wrong-file contract blocks, a truncated killing contract (boa [86] — `T: Trace + 'static` stripped), a harmful nudge detour (csstree) |

Three structural facts explain the distribution: (1) **untimed dumps were not acted on** —
turn-0 evidence had no demonstrable effect by the decisive edit (aiomonitor step 104, boa step
86+). Citation-honest framing (2026-06-10 adversarial verification): Lost-in-the-Middle (Liu
et al., TACL 2024) measured STATIC prompts and found a U-shaped curve in which the BEGINNING is
a FAVORED (primacy) position — it supports recency placement for C1 payloads (and, by primacy,
the C0 front-loaded brief), NOT an anti-front-loading claim; the inertness evidence here is
GT's OWN trajectory audit, not LitM; (2) **the one consumed payload was timed, scoped, and short** — fired on a
trajectory event, at the agent's live decision, as one imperative sentence; (3) **each layer
dumps independently** (L1 prepends; L3/L3b per-edit/per-view; L5 per-turn; consensus its own
block) — nothing decides whether *this payload, now, is the one thing worth the agent's
attention*.

### 15.2 The architecture — GT → oracle → agent

```
Issue → FTS5 → graph → LSP → scoring → ┌──────────────────────────────┐
                                        │  CANDIDATE POOL (all layers) │
   agent trajectory-so-far ──────────►  │  ORACLE: pure decision gate  │ ──► one emission
                                        │  (trigger·confidence·        │     (or silence)
                                        │   relevance·dedup·dose)      │
                                        └──────────────────────────────┘
```

- **Pure function:** `oracle(C, T) → E` where `C` = the candidate pool (every payload all GT
  layers computed, one shared schema), `T` = the trajectory-so-far exactly as it appears in
  `output.jsonl` / `mini-swe-agent.txt`, `E` = `{emit(c, channel, dose)}` with **|E| ≤ 1 per
  turn, or ∅ (silence)**.
- **All layers become candidate PRODUCERS** (L1, L3, L3b, L4, L5, L5b, consensus, plus the new
  SPEC producer); ONE gate decides WHEN / WHERE / HOW-MUCH / WHAT. This is the locked "L5
  decides WHEN, L3/L3b provide WHAT" mandate **generalized across every layer** — not a ninth
  layer; ONE PRODUCT RULE preserved (one candidate schema, one gate, one pipeline).
- **Stateless:** the oracle holds NO memory between turns — delivered-set (content-hash markers
  scanned from T), self-discovered-set (token-intersection with the agent's OWN observations),
  edit events (incl. heredoc/`python3 -c` writes — the current detection gap becomes a sensor
  requirement), test evidence, per-obligation edited?/tested? status, agent phase
  (ORIENT/IMPLEMENT/VERIFY/REVIEW), and failure/loop state are all **deterministically
  recomputed from T each turn**. Same `(C, T)` → same output, bit-for-bit → replayable offline
  against frozen trajectories (what makes calibration and red→green testing possible).
- **Decision function (per turn):** SENSE → TRIGGER → CONFIDENCE (per-task distribution-derived
  floors, never hardcoded) → RELEVANCE (∩ current focus / unmet obligation) → DEDUP →
  BUDGET (≤1 emission, dynamic per-task cap) → RANK (severity × confidence × relevance, 8-dp
  deterministic tie-break) → EMIT (+ full suppression telemetry even on silence).
- **NO LLM (hard rule):** trigger predicates + thresholds + set intersections + dedup hashes.
  Nothing generative, learned, or sampled.

### 15.3 The four per-emission decisions — every citation, fully documented

#### WHEN — decision-point triggers, never ambient narration
A candidate fires only on a sensed trajectory event (`edit_touches_issue_symbol`,
`review_transition`, `repeated_failure`, `no_new_state_loop`, `test_zero_output` /
`test_evidence_gap`, `drift` (whitelist-only), `view` (relevance-gated)). Research:

- Liu, N. F., et al., "Lost in the Middle: How Language Models Use Long Contexts," *TACL* 2024
  (arXiv 2307.03172, 2023). — RECENCY support ONLY (citation-honest, 2026-06-10): the U-shaped
  curve over STATIC prompts shows performance is highest with relevant content at the BEGINNING
  (primacy — which favors the C0 brief) or the END (recency — which favors C1 placement at the
  end of the current context). The paper does NOT show front-loaded content "decaying" as a
  trajectory grows; that extrapolation is retracted. The case for decision-point timing rests
  on GT's own trajectory ground truth, below.
- ORACLE-SWE (arXiv 2604.07789, 2026) — cited honestly for timing: its oracle signals were
  appended to the INITIAL user prompt — **front-loaded — and it works** (Reproduction Test
  dominant; all five signals combined ≥97%). It validates the CONTENT classes (§WHAT), not
  decision-point delivery; do not borrow its authority for the WHEN thesis.
- Iqbal, S. T. & Bailey, B. P., "Effects of Intelligent Notification Management on Users and
  Their Tasks," *CHI* 2008. — Deferring notifications to task breakpoints measurably cuts
  interruption cost (frustration, resumption time) vs immediate delivery.
- Iqbal, S. T. & Bailey, B. P., "Oasis: A Framework for Linking Notification Delivery to the
  Perceptual Structure of Goal-Directed Tasks," *ACM TOCHI* 2010. — A deployed framework
  scheduling notification delivery at perceptually-defined breakpoints of goal-directed tasks;
  breakpoint-aligned delivery reduces disruption. Review-transition and post-edit are the
  agent-loop analogue of those breakpoints.
- Horvitz, E., "Principles of Mixed-Initiative User Interfaces," *CHI* 1999. — A mixed-
  initiative agent should act only when the expected utility of acting exceeds the attention
  cost of the interruption — the oracle's emit-vs-silence calculus.
- Chen, V., et al., "Need Help? Designing Proactive AI Assistants for Programming," *CHI* 2025
  (arXiv 2410.04596). — Proactive programming-assistant suggestions timed to a just-occurred
  problem are preferred and more accepted than mid-task interjections.
- (Scope note: Iqbal & Bailey 2008/2010, Horvitz 1999, and Chen et al. 2025 are HUMAN-subject
  attention/interruption studies — applied here as ANALOGY, not agent evidence. No cited work
  measures breakpoint-timed delivery for LLM agents.)
- Plus GT's own ground truth: the single consumed payload of run 27307362054 fired at
  `test_zero_output`; and the SWE-agent test-guardrail precedent (+10.7pp — Yang et al.,
  *NeurIPS* 2024, already cited at `oh_gt_full_wrapper.py:1195`).

#### WHERE — the consumed channel, as an active check
Two channels, strictly assigned: **C0** (turn-0 front prepend — the L1 orientation brief ONLY,
the single legitimate front-load: it serves the agent's FIRST decision) and **C1** (post-action
observation append — everything correctness-related, rendered as a SHORT ACTIVE CHECK at
recency position, the favored end of the U-curve). No submit channel exists (the submit action
never reaches `env.execute()` on mini — F3); review-transition on C1 is the canonical
pre-submit moment. Research:

- Pu, K., et al., "Assistance or Disruption? Exploring and Evaluating the Design and Trade-offs
  of Proactive AI Programming Support," *CHI* 2025 (arXiv 2502.18658). — Proactive programming
  support carries a measured disruption cost; placement/timing trade-offs determine whether
  support nets positive — the channel-discipline basis (distinct paper from Chen et al.
  2410.04596 above; both CHI 2025; human-subject — analogy).
- TRAJEVAL, arXiv 2603.24631, 2026. — Mid-trajectory feedback injection into INTERCEPTED TOOL
  OBSERVATIONS, dosed at most once per event to avoid flooding — the published agent-side
  mechanism closest to GT's C1 observation-append channel; the external evidence that the
  channel itself is viable. Honesty note: the "SHORT ACTIVE CHECK > passive line" FRAMING
  claim still has no external citation — it rests on exactly ONE consumed payload in GT's own
  data (the boa `no_test_evidence` nudge, n=1) and is stated as a hypothesis, not a rule.

#### HOW-MUCH — confidence-gated dynamic dose; correct-or-quiet
≤1 emission/turn, each ≤ its `dose_cost` (nudge ≈1–3 sentences); per-task dynamic cap railed
(e.g. [4..10]/task, nudges ≤3); tier gating per CLAUDE.md (`[VERIFIED]`≥0.9 free at its
trigger; `[WARNING]` 0.5–0.9 only with ≥2 relevance keys; `[INFO]`<0.5 NEVER on C1). Over-fire
is harm with receipts (csstree detour, ~350KB aiomonitor context bomb, 2026-05-25 "MORE GT =
WORSE" stuck-detector kill). Research:

- Cuconasu, F., et al., "The Power of Noise: Redefining Retrieval for RAG Systems," *SIGIR*
  2024 (arXiv 2401.14887). — Quoted honestly (2026-06-10): what degrades generation is
  semantically-RELATED but non-answer-bearing (DISTRACTING) documents; truly RANDOM documents
  can even IMPROVE accuracy (up to ~30–35%); and relevant content should sit near the query
  (recency). GT's wrong-but-plausible payloads are exactly the harmful distractor class —
  the corrected citation is STRONGER for correct-or-quiet than the earlier misquote. No cited
  paper prescribes ≤1 emission/turn or any numeric dose — those are engineering choices whose
  real basis is GT's own incident record (2026-05-25 "MORE GT = WORSE", the ~350KB aiomonitor
  context bomb).
- Pu, K., et al., "Assistance or Disruption? …," *CHI* 2025 (arXiv 2502.18658). — (same entry
  as WHERE) the measured disruption cost of proactive support bounds the dose.
- Horvitz, E., *CHI* 1999 — (same entry as WHEN) attention cost sits inside the utility
  calculus; silence is a first-class action.

#### WHAT — issue-obligation/constraint over code-map (severity order 1→4)
1. **Obligation candidates** (`issue_verbatim` provenance — the issue's own requirements
   re-positioned at the decision point; zero misdirection risk), 2. **verification-gap
   candidates** (the proven-consumed class), 3. **contract candidates bound to the CURRENT
   edit** (complete declarations incl. bounds — the boa [86] fix; sibling-SHAPE in ADD-mode per
   §14), 4. **code-map narration** (the ~80%-inert class, demoted, anchor/obligation-gated).
   Research:

- REAgent, arXiv 2604.06861, 2026. — Cited honestly (2026-06-10): the mechanism is an
  **LLM PIPELINE** — a requirement-generation LLM agent that autonomously EXPLORES THE
  REPOSITORY, fills a 9-attribute template (incl. root-cause analysis, modification locations,
  impact scope), with assessment/refinement LLM agents sampling at temperature 0.5/0.1,
  delivered FRONT-LOADED. Its +17.4% average resolved belongs to THAT mechanism. **GT's
  `spec.py` is a deterministic regex subset of that content class, re-timed to decision
  points — its effect is UNMEASURED.** The paper validates the content-class DIRECTION only;
  do not attach its effect size to GT's extractor. **(2026-06-11: CONFIRMED NOT an LLM pipeline
  — REAgent and CodeScout are LLM pipelines; GT's extractor is deterministic regex; the
  distinction matters for the ONE PRODUCT RULE / $0-AI constraint.)**
- CodeScout, arXiv 2603.05744, 2026. — Same honesty: **THREE sequential LLM calls** over repo
  + issue (exploration-target identification, per-target LLM analysis, LLM synthesis),
  delivered as FRONT-LOADED preprocessing. Its +20% (up to 27 additional issues on
  SWE-bench-Verified) belongs to that LLM mechanism — not to GT's deterministic extractor,
  whose effect is UNMEASURED. Content-class direction only. **(2026-06-11: CONFIRMED same
  class as REAgent — LLM pipeline, not adopted; GT's spec.py is the deterministic subset.)**
- ORACLE-SWE, arXiv 2604.07789, 2026. — Five oracle signals measured (verified magnitudes,
  SWE-bench-Verified/GPT-4o: baseline ~39.4%; **Reproduction Test +46pp** >> Execution
  Context +12 ≈ Edit Location +11 >> API Usage +8 >> Regression +5; all five combined ≥97%)
  — a reproduction requirement is the dominant signal class. Delivery posture: front-loaded
  (see the WHEN honesty note above).
- Shinn, N., et al., "Reflexion: Language Agents with Verbal Reinforcement Learning," *NeurIPS*
  2023 (arXiv 2303.11366). — Reflect-on-execution-feedback loop: 91.0% vs 80.0% pass@1 on
  HumanEval (+11pp); ablating test execution collapses the gain.
- Chen, X., et al., "Teaching Large Language Models to Self-Debug," *ICLR* 2024 (arXiv
  2304.05128). — Model inspects execution results and explains its own code ("rubber duck");
  execution feedback is necessary — without it self-correction gains are minimal.
- "Type-Constrained Code Generation with Language Models," arXiv 2504.09246, 2025. — Enforcing
  well-typedness at decode time: **+37% pass@1 on repair** (largest effect), >50% fewer
  compilation errors — type constraints carry real signal, but via an enforcement loop GT does
  not have; GT's deliverable form is the complete typed declaration as context.
- "LLM Type Errors" (type-correctness study), arXiv 2510.10216, 2025. — Type errors alone
  account for **33.6%** of failed LLM-generated programs — the failure mass complete contracts
  target.
- Xia, C. S., et al., "Agentless: Demystifying LLM-based Software Engineering Agents," *FSE*
  2025 (arXiv 2407.01489). — Repair-step recipe = issue + **±10 lines** around the edit
  location + test feedback; narrow, decision-bound context beats rich structural dumps — the
  oracle's severity ordering encodes exactly that.
- Yang, J., et al., "SWE-agent: Agent-Computer Interfaces Enable Automated Software
  Engineering," *NeurIPS* 2024 (arXiv 2405.15793). — ACI design; the test-guardrail
  (+10.7pp) is the proven precedent for GT's verification-gap nudges.
- Chen, Z., et al., "LocAgent: Graph-Guided LLM Agents for Code Localization," *ACL* 2025
  (arXiv 2503.09089). — File Acc@5 92.7% (fine-tuned Qwen-2.5-32B); removing structured
  retrieval support causes dramatic drops. / Yu, Z., et al., "OrcaLoca: An LLM Agent Framework
  for Software Issue Localization," *ICML* 2025 (arXiv 2502.00350). — 65.33% function-level
  match; +6.33pp resolved. Together: localization is partially solved at file level; the open
  gap is function/line — WHAT within the file.
- SWE-Explore, arXiv 2606.07297, 2025. — Leading agents reach ~64–68% file hit but only
  **15–20% line-level recall** of ground-truth evidence — agents find neighborhoods, miss
  evidence spans.
- Luan, S., et al., "Aroma: Code Recommendation via Structural Code Search," *OOPSLA/PACMPL*
  2019 (doi 10.1145/3360578). — Structural code-to-code search; clusters/intersects sibling
  method bodies into a distilled common pattern (deliver patterns, not raw snippets).
- Zhong, H., et al., "MAPO: Mining and Recommending API Usage Patterns," *ECOOP* 2009. — Mined
  API usage patterns help programmers more effectively than raw snippet recommendation.
- Zhang, F., et al., "RepoCoder: Repository-Level Code Completion Through Iterative Retrieval
  and Generation," *EMNLP* 2023 (ACL Anthology 2023.emnlp-main.151). — Repo-level retrieval of
  similar snippets improves completion >10%; iterative retrieval bridges the context-target gap.
- Holmes, R. & Murphy, G. C., "Using Structural Context to Recommend Source Code Examples
  (Strathcona)," *ICSE* 2005. — The developer's structural context drives relevant example
  recommendation — the ADD-mode sibling-SHAPE basis.
- "Beyond Localization," arXiv 2603.29067, 2025. — Oracle localization improves all tested
  repair systems but success stays **below 50%** without the verification loop — localization
  is necessary, not sufficient (the §15.6 ceiling).

#### Carried-over architecture citations (already load-bearing in §§2–14, normalized here)
RepoGraph (*ICLR* 2025 — k-hop ego-graph, hub caution) · ColBERT MaxSim (Khattab & Zaharia,
*SIGIR* 2020 — per-symbol late interaction, §11.2) · RRF (Cormack et al., *SIGIR* 2009 — rank
fusion, §8) · XTA (Tip & Palsberg, *OOPSLA* 2000 — set-propagation call graphs, §2.3 rung
1.94a) · CHA (Dean, Grove & Chambers, *ECOOP* 1995 — class-hierarchy dispatch resolution, Go
IMPLEMENTS §2.3) · PyCG (Salis et al., *ICSE* 2021 — assignment-flow resolution, rung 1.96) ·
demand-driven analysis (Heintze & Tardieu, *PLDI* 2001; Sridharan & Bodík, *PLDI* 2006 —
query-scoped residual resolution) · BEIR (Thakur et al., *NeurIPS* 2021) + DPR (Karpukhin et
al., *EMNLP* 2020) + entity-centric (Sciavolino et al., *EMNLP* 2021) — the lexical-vs-dense
adaptive-fusion evidence, §11.5 · approximate JS call graphs (Feldthaus et al., *ICSE* 2013).
Full entries in REFERENCES below.

### 15.4 The staged build plan (Stages 0–7, each red→green, independently shippable/rollbackable)

> **STATUS (2026-06-11):** Stages 0–2 COMPLETE (byte-parity proven, 78 tests — see §16.1). Stage 3 (review-transition obligation emission) code SHIPPED (`e574a91b`) but **0/9 runtime fires** in the oracle run — trigger or routing gap in the deployed build (§16.4 open issue B). Stages 4–5 code SHIPPED (governor FP closure, loop/coherence sensors, TIDE metrics) but **build lag** means the oracle run measured OLD code for `failure_persisted`/loop (7 unpushed commits at run time). Stages 6–7 NOT BUILT.

| Stage | Builds | Metric it moves | Status (2026-06-11) |
|---|---|---|---|
| **0** | Trajectory sensor + offline replay harness (edit detection incl. heredoc/`python3 -c`, test-runner+output capture, phase detection, GT-marker delivered-set) | sensor recall on known events (the currently-missed edits are the red set) | **COMPLETE** — pre-build audit found heredoc already handled; real red set = stateless recompute + delivered-set scan; 78 tests green |
| **1** | Issue-as-SPEC extractor (`obligations[]` + the F2 anchors-file extension) | extraction precision, hand-audited on held-out non-benchmark issues | **COMPLETE** — `spec.py`, `gt_oracle.py`; 3/3 on-target fires in oracle run |
| **2** | Oracle core around L5 ONLY — byte-parity with current governor first, THEN dedup/relevance/budget/attribution discipline | replay red→green vs ledger ground truth (csstree harmful fire suppressed; boa fire preserved; zero new fires) | **COMPLETE** — byte-parity 10/10 trajectories; `_latch_key()` bug found+fixed; `tests/test_oracle_stage2_parity.py` 6 tests |
| **3** | Obligation-aware candidates: Rank-1 test-evidence-gap extension, GT_VERIFY port to mini with obligation checklist, K-of-N scope re-route to review-transition, drift whitelist | consumption rate at review-transition (§4 ledgers) | **CODE SHIPPED** (`e574a91b`) — **0/9 runtime fires** at review-transition; trigger/routing gap (§16.5 issue B) |
| **4** | Route L3/L3b/L4/consensus through the oracle (relevance-gated witnesses, edit-bound `rearm_on_change` contracts, stale-graph honesty, retire the C0 scope-chain dump) | inert rate ~80% → minority at unchanged correctness; harm rate 0 | **NOT BUILT** |
| **5** | Contract completeness: `extractSignature` captures the full declaration header to body-open (where-clauses/bounds/generic constraints) — needs substrate rebuild | held-out typed-language fixtures; boa [86] shape renders `T: Trace + 'static` | **CODE-PRESENT** (`contract_map.py:626–665`) — zero runtime confirmation; §16.5 issue G |
| **6** | Calibration pass: per-class distribution-derived floors, HIGH-pin kills, per-task dose caps, replay-tuning on a grown corpus | precision-of-HIGH ≈ 1.0; per-class trigger precision on HELD-OUT repos | **NOT BUILT** |
| **7** | Paired measurement run + the efficiency instrumentation (prereq: the 4 BLOCKS-BENCHMARK metric bugs closed) | BOTH headline metrics: efficiency deltas (first measurement ever) + flips (paired Wilcoxon) | **NOT DONE** — M13/M01/M03 measured but all non-significant; prereqs (§16.5 A/E/F/K) not closed |

### 15.5 The two-leg foundation (per-stage acceptance gate, CLAUDE.md verbatim)

Every stage ships only if it passes BOTH legs:

- **LEG 1 — GENERALIZATION.** `.claude/CLAUDE.md`: "**Generalized** — works on any repo / agent
  / language / model. No benchmark-shape logic, task IDs, or gold labels." Project CLAUDE.md:
  "If your implementation improves a benchmark by overfitting to benchmark structure, task IDs,
  gold files, FAIL_TO_PASS labels, repeated smoke tasks, specific repos, specific models, or
  specific agent behavior, you must stop and call it out immediately." Oracle mechanisms are
  structural (predicates over harness-output text + issue text + graph provenance); confidence
  floors are per-task distribution-derived; the drift trigger is whitelist-only until a
  held-out non-benchmark corpus certifies precision; tuning splits are by repo+language, never
  by task; gold is measurement-only, post-hoc, never in product logic.
- **LEG 2 — PRODUCT + FLIPS.** `.claude/CLAUDE.md`: "produce **flips** … by delivering
  **correct context** that lets the agent write **correct code**. … **The arrow: correct
  context → correct code → flips.** Flips are the OUTPUT that proves it works, not a feature to
  engineer toward." Core Product Contract verbatim: "reduce turns-to-useful-edit", "reduce
  turns-to-gold-read/edit", "compact, high-precision evidence", "**stay silent when
  uncertain**", "agent-assisting, not agent-controlling"; must NOT "flood the agent with graph
  noise", "delay first useful edit", "increase action count without outcome gain", "inject
  low-confidence evidence as if it is fact". And: "**DEFINITION OF DONE: metrics changed.**" —
  DONE here = the Stage-7 paired measurement (efficiency Δ + flips, Wilcoxon), nothing earlier.

### 15.6 The honest ceiling (bounded by the verification loop)

**Can buy:** harm → ~0 (HIGH-pin kills, fabricated-coordinate kill, C0 scope-dump retirement,
premise-sensed nudges — a wrong confident pin COSTS turns today); inert ~80% → a minority
(correctness content moved to sensed decision points at recency position — the only delivery
shape with a proven consumption in our own data); efficiency deltas finally measured; 2–4 of
the 10 failure shapes become contestable (the obligation/test-evidence class: aiomonitor's
"async", boa's `Trace`, katex's never-thrown validation).

**Cannot buy:** **the verification loop** — the dominant measured lever is write→test→fix with
failure-specific feedback (ORACLE-SWE; Reflexion *NeurIPS* 2023; Self-Debug *ICLR* 2024);
without it, context-only improvement plateaus below ~50% oracle success ("Beyond Localization"
arXiv 2603.29067). GT can say "you never observed your new API execute"; it cannot make the
agent write and iterate the test — that is the harness (pier closes exactly this; the 0/10 was
mini-swe without it). **The unconvertible residue:** awilix's conscious algorithm punt, fd's
ordering corners, arktype's recursive $defs — no deterministic context plausibly converts
these; claiming otherwise would violate this codebase's own rules. Model-capability limits
(~3% even at full oracle signals, ORACLE-SWE) are never filed as a loss cause per the Mandatory
Analysis Rules — the oracle's telemetry exists to keep proving where the context gap actually was.

### 15.7 The 10-task run findings (run 27307362054) that ground this design

> **UPDATED (2026-06-11):** The adversarial re-audit (`ADVERSARIAL_REAUDIT_27307362054.md`)
> REFUTED several claims in the original run summary. See §16.3 for the corrected verdicts. Key
> corrections: "consumed ×10" is REFUTED (~80% inert, 1 firm consumption); "delivered correct
> context ×10" is OVERSTATED (2 HIGH-wrong pins, fabricated coordinate, truncated boa contract);
> and the "FIX-A breach confined to scope-chain" reading is wrong — the brief RANKING surface (entry
> #2) remains unlaundered. The analysis below is amended only where the re-audit changes the fact.

- **0/10 resolved, class=AGENT ×10** (correct under the fixed classifier); 10/10 trajectories
  reached the right files and produced substantial patches that failed on requirement-detail
  edge cases; leakage 0 across all 10 (no hidden-test names, no FAIL_TO_PASS, no GT_META).
- **Bottleneck = the harness:** every loss is spec-compliance/self-verification against
  requirements VERBATIM in the agent's own context (aiomonitor's "async", boa's `Trace`
  sentence, csstree's canonical-order, abs-stepped's `[::2]`); mini-swe-agent has no
  write→test→fix loop against stated requirements — the 0/10 is NOT comparable to the pier
  0.790 board number; a fair comparison needs GT-on-pier or a paired baseline mini arm.
- **Efficiency UNMEASURED — the Stage-7 obligation:** no paired efficiency metric
  (turns-to-gold-read/edit, time-to-first-useful-edit, wasted-detour count) has ever been
  computed; the Core Product Contract's efficiency half is un-claimed until Stage 7 measures it.
- **FIX-A held:** the cross-language disqualifier held 9/10; boa (rust + 657 vendored v8-bench
  JS files, the exact pre-fix launder shape) came back clean on every surface with a direct
  paired before/after kill of the prior run's `deltablue.js` launder; the aiomonitor
  py→vendored-JS scope-chain breach is a confirmed 1-off (close in normal course with a
  vendored/minified-asset filter — and note: it was CONSUMED, not just rendered).
- **The adversarial correction (what the re-audit changed):** the run summary's "delivered,
  factually correct, consumed ×10" was **refuted as stated** — across ~62 payload units,
  demonstrable helpful consumption ≈ 1 (boa's `no_test_evidence` nudge); ~80% inert; ~15%
  wrong/harmful; the ledgers' "BEHAVIORAL" consumption grades are mostly vacuous (interfaces
  preserved that were never at risk; transitions that pre-dated the nudge). Inverted confidence
  is systemic (2 HIGH pins wrong — abs-stepped `New`, aiomonitor
  `commands.py::format_running_task_list`, a fabricated coordinate). The honest verdict:
  **harness-dominant losses with named GT deliverable-context gaps** — in 2 of 4 re-audited
  tasks GT had a concrete in-scope opportunity to deliver the missing fact at the deciding
  moment and was structurally unable to (boa [86] bounds stripping; heredoc edit-detection
  blindness + the stale-graph hole) — not "purely the harness."

  **(2026-06-11 note):** Full corrected verdicts in §16.3. The oracle run (27321848581) reproduced
  the same inverted-confidence pins verbatim (abs-stepped `functions.go::New`; aiomonitor
  `format_running_task_list`) in both arms — the localization scorer is unchanged and the pins
  are deterministically recurring. The aiomonitor vendored-tailwind.js context bomb re-arms
  every run (FIX-A brief-level launder open, §16.5 issue C).

---

## REFERENCES — consolidated (every citation in §§1–15, one line each, alphabetical)

> Format: `Author(s), "Title," *Venue* Year. [URL/ID]`. This section is the single citation
> registry for this document: the short forms used in the body (§2.3, §4.1–4.2, §8, §9, §11,
> §14, §15) and the SCIP/CHA/XTA/PyCG references carried in CLAUDE.md all resolve to an entry
> here. No citation appears in the body without an entry below. Entries marked *(as cited in
> code/CLAUDE.md)* preserve the project's existing attribution where the primary source is a
> blog/tool rather than a paper.

- "Beyond Localization" (oracle-localization ceiling study), arXiv 2603.29067, 2025. — Oracle localization improves all tested repair systems; success stays below 50% without the verification loop.
- Chen, V., et al., "Need Help? Designing Proactive AI Assistants for Programming," *CHI* 2025. [arXiv 2410.04596] — Proactive suggestions timed to a just-occurred problem are preferred over mid-task interjections.
- Chen, X., et al., "Teaching Large Language Models to Self-Debug," *ICLR* 2024. [arXiv 2304.05128] — Execution-feedback self-inspection; without execution feedback, self-correction gains are minimal.
- Chen, Z., et al., "LocAgent: Graph-Guided LLM Agents for Code Localization," *ACL* 2025. [arXiv 2503.09089] — File Acc@5 92.7%; structured retrieval support is load-bearing.
- CodeScout, arXiv 2603.05744, 2026. — Structured problem-statement enhancement via THREE sequential LLM calls over repo+issue, front-loaded; +20% on SWE-bench-Verified belongs to that LLM mechanism — content-class direction only for GT's deterministic extractor (effect UNMEASURED).
- CodeSage: Zhang, D., et al., "Code Representation Learning at Scale," *ICLR* 2024. — Code-tuned embedding evidence for the §11.3 embedder swap.
- CoIR: Li, X., et al., "CoIR: A Comprehensive Benchmark for Code Information Retrieval Models," arXiv 2407.02883, 2024. — Code-IR benchmark grounding gte-modernbert > e5 for code (§11.3).
- CodeXEmbed: Liu, Y., et al., "CodeXEmbed: A Generalist Embedding Model Family for Multilingual and Multi-task Code Retrieval," arXiv 2411.12644, 2024. — Multilingual code-retrieval embedding evidence (§11.3).
- Cormack, G. V., Clarke, C. L. A. & Buettcher, S., "Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods," *SIGIR* 2009. — RRF rank fusion; the fixed k=60 convention (§8).
- CoSIL (LLM-driven code-graph issue localization), arXiv 2025 *(as cited in code, §9)*. — Graph-search issue localization; comparative context for the localizer design.
- Cuconasu, F., et al., "The Power of Noise: Redefining Retrieval for RAG Systems," *SIGIR* 2024. [arXiv 2401.14887] — Semantically-related DISTRACTOR documents degrade generation (random noise can improve it); relevant content belongs near the query. Bounds brief breadth and the oracle's distractor risk (§15.3 HOW-MUCH, quoted honestly).
- Cummins, R., Jose, J. & O'Riordan, C., "Improved Query Performance Prediction Using Standard Deviation," *SIGIR* 2011. — Score-dispersion QPP; basis of the Dimension-4 dense-dispersion gate (§4.2).
- Dai, Z. & Callan, J., "Deeper Text Understanding for IR with Contextual Neural Language Modeling," *SIGIR* 2019. — Passage-level (MaxP) scoring; basis for the per-symbol max+mean file score (§11.2).
- Dean, J., Grove, D. & Chambers, C., "Optimization of Object-Oriented Programs Using Static Class Hierarchy Analysis," *ECOOP* 1995. — CHA: resolve dispatch via the class hierarchy (Go IMPLEMENTS, §2.3; CLAUDE.md indexer lever).
- "Dissecting the SWE-Bench Leaderboards," arXiv 2506.17208, 2025. — Seven architectural groups; universal pattern = staged localize→repair→verify with test execution; no top performer injects contracts/callers.
- Feldthaus, A., et al., "Efficient Construction of Approximate Call Graphs for JavaScript IDE Services," *ICSE* 2013. — Approximate JS call-graph construction; the JS resolution-strategy reference (CLAUDE.md).
- Fox, E. A. & Shaw, J. A., "Combination of Multiple Searches," *TREC-2* 1994. — CombMIN/CombMNZ evidence-combination baselines used alongside RRF (§9).
- Heintze, N. & Tardieu, O., "Demand-Driven Pointer Analysis," *PLDI* 2001. — Just-enough computation for the query variables; the query-time residual-resolution design (CLAUDE.md SCALE).
- Holmes, R. & Murphy, G. C., "Using Structural Context to Recommend Source Code Examples," *ICSE* 2005. — Strathcona: structural context → example recommendation (§14 sibling-SHAPE; §15.3 WHAT).
- Horvitz, E., "Principles of Mixed-Initiative User Interfaces," *CHI* 1999. — Act only when expected utility exceeds attention cost; the oracle's emit-vs-silence calculus.
- Iqbal, S. T. & Bailey, B. P., "Effects of Intelligent Notification Management on Users and Their Tasks," *CHI* 2008. — Defer-to-breakpoint notification delivery measurably cuts interruption cost.
- Iqbal, S. T. & Bailey, B. P., "Oasis: A Framework for Linking Notification Delivery to the Perceptual Structure of Goal-Directed Tasks," *ACM TOCHI* 2010. — Breakpoint-aligned delivery framework; the WHEN trigger model.
- Karpukhin, V., et al., "Dense Passage Retrieval for Open-Domain Question Answering," *EMNLP* 2020. — DPR; dense-retrieval strengths/limits grounding adaptive fusion (§11.5).
- KGCompass (repository-aware knowledge-graph repair), arXiv 2025 *(as cited in code)*. — Path-decay graph traversal (beta=0.85, multi-hop-from-issue-entities); §8 decay constant + §4.1 gate (d).
- Khattab, O. & Zaharia, M., "ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT," *SIGIR* 2020. — MaxSim late interaction; the per-symbol granularity fix (§11.2).
- Lao, N. & Cohen, W. W., "Relational Retrieval Using a Combination of Path-Constrained Random Walks," *Machine Learning* 81(1), 2010. — PRA hub-discounted paths (§9).
- Liu, N. F., et al., "Lost in the Middle: How Language Models Use Long Contexts," *TACL* 2024. [arXiv 2307.03172] — U-shaped long-context attention over STATIC prompts: beginning (primacy) AND end (recency) are favored. Supports C1 recency placement and the C0 front-loaded brief; does NOT support anti-front-loading (§15.3, quoted honestly). Also brief breadth (§8).
- "LLM Type Errors" (type-correctness failure study), arXiv 2510.10216, 2025. — Type errors alone account for 33.6% of failed LLM-generated programs.
- Luan, S., et al., "Aroma: Code Recommendation via Structural Code Search," *OOPSLA/PACMPL* 2019. [doi 10.1145/3360578] — Distilled sibling-pattern recommendation (§14; §15.3 WHAT).
- ORACLE-SWE, arXiv 2604.07789, 2026. — Five oracle signals (Verified/GPT-4o: baseline ~39.4%, Reproduction Test +46pp, combined ≥97%); Reproduction Test dominates — the verification-loop ceiling (§15.6). Signals delivered FRONT-LOADED (initial prompt) — content evidence, not timing evidence (§15.3 WHEN honesty note).
- Pu, K., et al., "Assistance or Disruption? Exploring and Evaluating the Design and Trade-offs of Proactive AI Programming Support," *CHI* 2025. [arXiv 2502.18658] — Measured disruption cost of proactive support; WHERE/HOW-MUCH discipline.
- PyCG: Salis, V., et al., "PyCG: Practical Call Graph Generation in Python," *ICSE* 2021. [arXiv 2103.00587] — Assignment-graph call resolution for dynamic languages (rung 1.96, §2.3; CLAUDE.md).
- REAgent, arXiv 2604.06861, 2026. — LLM requirement-generation pipeline with autonomous repo exploration (temp 0.5/0.1), front-loaded; +17.4% average resolved belongs to that mechanism — content-class direction only for GT's deterministic extractor (effect UNMEASURED).
- RepoCoder: Zhang, F., et al., "RepoCoder: Repository-Level Code Completion Through Iterative Retrieval and Generation," *EMNLP* 2023. [ACL Anthology 2023.emnlp-main.151] — Iterative repo-level similar-snippet retrieval, >10% improvement.
- RepoGraph: Ouyang, S., et al., "RepoGraph: Enhancing AI Software Engineering with Repository-level Code Graph," *ICLR* 2025. — k-hop ego-graph repo representation; hub caution (§9, §11.7).
- Saha, R. K., et al., "Improving Bug Localization Using Structured Information Retrieval (BLUiR)," *ASE* 2013. — Field-level lexical bug localization (§9).
- Sciavolino, C., et al., "Simple Entity-Centric Questions Challenge Dense Retrievers," *EMNLP* 2021. — Identifier/entity-heavy queries favor lexical over dense; Dimension-0 fusion basis (§4.2, §11.5).
- Shinn, N., et al., "Reflexion: Language Agents with Verbal Reinforcement Learning," *NeurIPS* 2023. [arXiv 2303.11366] — +11pp from the reflect-on-execution loop; ablating test execution collapses it.
- Shtok, A., Kurland, O., Carmel, D., et al., "Predicting Query Performance by Query-Drift Estimation (NQC)," *ACM TOIS* 2012. — Normalized query commitment; score-dispersion flatness detection (§4.2 Dimension 4).
- Sourcegraph, "Announcing SCIP," 2022. [sourcegraph.com/blog/announcing-scip] *(as cited in CLAUDE.md)* — Batch whole-project semantic indexing; the scalable language-agnostic precision-pass design reference.
- Sridharan, M. & Bodík, R., "Refinement-Based Context-Sensitive Points-To Analysis for Java," *PLDI* 2006. — Client-driven refinement with IDE-suitable response times; demand-driven residual resolution (CLAUDE.md SCALE).
- SWE-agent: Yang, J., et al., "SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering," *NeurIPS* 2024. [arXiv 2405.15793] — ACI design; the +10.7pp test-guardrail precedent (`oh_gt_full_wrapper.py:1195`).
- SWE-Explore, arXiv 2606.07297, 2025. — ~64–68% file hit vs 15–20% line-level evidence recall; the recall-not-precision gap.
- SWE-PRM (process-reward intervention form), *NeurIPS* 2025 *(as cited in `trajectory/governor.py`)*. — Diagnostic-not-prescriptive intervention framing for the K-of-N scope check (§11.4).
- SWERank, *ICLR* 2025 *(as cited in code)*. — Retrieve→rerank localization; witness hard-negative ordering (§8 witness strengths, §9).
- TRAJEVAL, arXiv 2603.24631, 2026. — Mid-trajectory feedback injected into intercepted tool observations, dosed at most once per event — the published agent-side precedent for the C1 observation-append channel (§15.3 WHERE).
- TCTracer: White, R., Krinke, J. & Tan, R., "Establishing Multilevel Test-to-Code Traceability Links," *ICSE* 2020. — Assertion→tested-function linking (pass 4, §2.1; the `assertions` table).
- Thakur, N., et al., "BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of Information Retrieval Models," *NeurIPS* 2021 (Datasets & Benchmarks). — No single retriever wins across query types; adaptive fusion basis (§11.5).
- Tip, F. & Palsberg, J., "Scalable Propagation-Based Call Graph Construction Algorithms," *OOPSLA* 2000. [web.cs.ucla.edu/~palsberg/paper/oopsla00.pdf] — XTA set-propagation (+88% precision over RTA); rung 1.94a (§2.3); CLAUDE.md indexer lever.
- "Type-Constrained Code Generation with Language Models," arXiv 2504.09246, 2025. — Decode-time well-typedness: +37% repair pass@1, >50% fewer compile errors; the enforcement-vs-information caveat (§15.3 WHAT).
- Weiser, M., "Program Slicing," *ICSE* 1981. — Forward slicing; the `data_flow` property kind (§2.4).
- Xia, C. S., et al., "Agentless: Demystifying LLM-based Software Engineering Agents," *FSE* 2025. [arXiv 2407.01489] — Three-stage localization (file 69.7% / function 52.0% / line 35.3%); repair context = issue + ±10 lines.
- Yu, Z., et al., "OrcaLoca: An LLM Agent Framework for Software Issue Localization," *ICML* 2025. [arXiv 2502.00350] — 65.33% function-level match; +6.33pp resolved over its base framework.
- Zhong, H., et al., "MAPO: Mining and Recommending API Usage Patterns," *ECOOP* 2009. — Mined patterns outperform raw-snippet recommendation (§14, §15.3 WHAT).
- Zhou, J., Zhang, H. & Lo, D., "Where Should the Bugs Be Fixed? More Accurate Information-Retrieval-Based Bug Localization Based on Bug Reports (BugLocator)," *ICSE* 2012. — Title/summary term weighting; the reporter-confirmed short-name provenance gate (§4.2 fix ②).
- "Dissecting the SWE-Bench Leaderboards," arXiv 2506.17208, 2025. — Seven architectural groups; universal pattern = staged localize→repair→verify with test execution; no top performer injects contracts/callers (§16 model-agnosticism note).
- MiniMax-M3 (TokenRouter model), Minimax AI, 2026. — MoE model with $0 prompt-cost tier used in §16 routing validation; proves GT's model-agnosticism (no anthropic/openai dependency).

---

## 16. Session 2026-06-11 — Oracle build (Stages 0–5), 10-task oracle run, adversarial audit

> **Last verified 2026-06-11.** Evidence: `DEEP_TRAJECTORY_ANALYSIS_ORACLE_RUN.md` (9-task
> oracle-arm §4), `ADVERSARIAL_REAUDIT_27307362054.md` (baseline re-audit), `LAYER_REPORT_FACTCHECK.md`
> (13 corrected claims), `.claude/reports/ORACLE_STAGE012_BUILD_20260610T2330Z.md` (oracle build record),
> `.claude/reports/metrics/oracle_vs_baseline_20260611/GT_METRICS_REPORT.md` (paired metrics, N=9).

### 16.1 What was actually built (commits `0e1bd371`→`b65e32c4`, branch `gt-trial`)

| Stage | What it builds | Key commit |
|---|---|---|
| **Stage 0** | Trajectory sensor + offline replay harness — stateless `sense(messages)→DerivedState`; edit-detection incl. heredoc/`python3 -c`/`cat >`; delivered-set scan from frozen trajectory; phase detection (ORIENT/IMPLEMENT/VERIFY/REVIEW); offline `oracle.replay(turns)` for calibration | `0e1bd371` |
| **Stage 1** | Issue-as-SPEC extractor (`gt_oracle.py` `spec.py`) — parses the issue text into `obligations[]` (must-do/raise/throw/preserve sentences); identifies unverified obligation tokens vs observed test output; the F2 anchors-file extension for obligation-localization; `tests/test_oracle_stage1_spec.py` 24 tests | per-commit range |
| **Stage 2** | Oracle core around L5 — pure `oracle_decide(C, T)` function; byte-parity gate: `oracle.replay` over all 10 frozen `tenpack_27307362054` trajectories reproduces the live governor's fire/suppress set EXACTLY (boa `no_test_evidence` FIRE preserved; csstree harmful nudge SUPPRESSED; aiomonitor/awilix SILENCE); in-stage bug found+fixed (`_latch_key()` reconstruction — loop+scaffold_trap shared latch key re-fired every turn ≥25); `tests/test_oracle_stage2_parity.py` 6 tests | `e574a91b` |
| **Stage 3** | Review-transition obligation-STATUS emission — the **#1 lever** per the oracle diagnosis; per-clause obligation table (edited?/tested? recomputed statelessly from T); dedup key = `(content × phase × obligation-status)` not bare content-hash; fires at `review_transition` event; `gt_mini_patch.py` obligation-candidate routing at `:2067–2071` | `e574a91b` |
| **Stage 4** | Loop / coherence detectors — 4-signal composite for no-new-state loop (command + normalized-observation exact-pair), 3-signal coherence for stale-binary/dead-code patterns; TIDE behavioral sensor (`loop_ratio`, `edit_coverage`, `test_coverage` — the TRAJEVAL-family metrics); dynamic escalation (`V` from observed pace, bands from behavioral signals) | `e574a91b` |
| **Stage 5** | Governor FP closure — `failure_persisted` now requires: (1) real test-runner invocation (pytest/unittest/go test/cargo test/npm test/jest/runtests.py), (2) no env/tooling error marker, (3) explicit test/assertion FAILURE marker; the `fd` run's `git apply error:` false-positive blocked; `loop` arm gated on proven no-new-state (command+normalized-obs pair); `scaffold_trap` unchanged (4/5 true-positive in the audit) | `e574a91b` |

**Additional built items (same commit range):**
- **MiniMax-M3 model routing** (`TokenRouter`, `gt_mini_patch.py:1847` region): routes non-critical generation to MiniMax-M3 at $0 prompt cost, proving GT's model-agnosticism (no anthropic/openai dependency at the inference layer).
- **Metrics framework M01–M26** — M01–M19 efficiency + outcome metrics (steps-to-gold-read/edit, wasted steps, suppression rate, localization H@1/P@3, consumption grade, inverted-confidence rate, etc.); **M20–M26 code-quality metrics** added this session: M20 callee-attribution precision (how often `file::symbol` coordinates exist in graph.db), M21 obligation-coverage (fraction of issue-obligation tokens present in at least one emission), M22 clause-granularity (fraction of multi-clause obligations split), M23 review-transition fire rate, M24 false-positive governor rate, M25 loop-detection recall (fraction of ≥3-repeat loops caught), M26 sibling-shape delivery rate (fraction of ADD-mode edits with a sibling-shape block). `compute_paired_metrics.py` (commit `6744bfc7`) — all 19 GT metrics + paired Wilcoxon + bootstrap CI.
- **Self-verification retry gate** (`GT_RETRY_ON_VERIFIER_FAIL`, `gt_agent.py`) — code complete; NOT enabled in any workflow (`deepswe_full.yml`) — the env var is never forwarded. **This is an open plumbing gap**: any run launched without it silently measures oracle-only; the retry loop (the closer for both audited causal trajectory wins) has never fired in a live run.

### 16.2 The 10-task oracle-only run (27321848581) — results

**Setup:** oracle arm (GT-on, Stages 0–5 live), mini-swe-agent, 10 tasks (arktype failed/missing → 9 evaluated), 5 languages (Go ×2, Python ×2, TypeScript ×1, JavaScript ×2, Rust ×2). Run 27307362054 = same-task baseline (prior run, frozen, 0/10 resolved). All tasks SUBMITTED; 0/9 resolved. `failure_class=AGENT ×9` under the §12 reconciliation (cert false-fail corrected). Zero leakage (no hidden-test names, no FAIL_TO_PASS, no GT_META).

**Paired metrics (M-series, oracle vs baseline, N=9, from `GT_METRICS_REPORT.md`):**

| Metric | Baseline | Oracle | Delta | p (Wilcoxon) | Interpretation |
|---|---|---|---|---|---|
| M13 steps-to-gold-edit | lower | higher (+1.89 mean, +4.00 median) | **negative** | p=0.6250 | oracle arm slower to first gold edit; not significant |
| M01 steps-to-first-gold-read | lower | higher (+0.33) | — | p=0.8594 | no improvement |
| M03 total steps | — | 6/9 LONGER | — | p=0.4844 | 6 tasks longer in oracle arm |
| M04 brief P@3 | 0.63 | 0.59 | −0.04 | — | small regression |
| M04 H@1 (boa) | 1 | 0 | −1 | — | inverted-confidence pin regressed after rerank fixes |
| M17 wasted steps | — | fewer (directional) | +dir | p=0.0625 | not significant; only directional win |
| M20 hidden test improvement (aiomonitor) | 1/5 | 4/5 | **+3** | n=1 | causal trajectory win (obligation nudge → async correct) |
| M13 (fd extension) | fail | pass | flip | n=1 | obligation nudge → extension hidden tests all passed |

**Bottom line:** 0 binary flips, 0 regressions on resolution. Primary efficiency metrics non-significant and pointing the wrong direction on most tasks. Two audited causal trajectory wins (aiomonitor async: 1/5→4/5 hidden tests; fd extension: hidden tests passed) — both attributable to the obligation nudge, both 1 retry loop away from plausible resolution; the retry loop was never armed. Noise volume down (chars −9% to −58%); M17 directionally better. **Not "done" by DEFINITION OF DONE.**

### 16.3 What the adversarial audit changed (run 27307362054 corrected verdicts)

The LAYER_REPORT_FACTCHECK.md identified 13 inflated or false claims in the post-run layer report. The corrected picture:

| Claim | Status |
|---|---|
| "GT delivered correct context ×10" | OVERSTATED — 2 HIGH-confidence wrong localization pins (inverted confidence); fabricated `commands.py::format_running_task_list` coordinate; vendored `tailwind.js` at brief entry #2; truncated boa [86] contract |
| "Consumed ×10" | REFUTED — ~80% inert; ~15% wrong/harmful; 1 firm consumption (boa `no_test_evidence` nudge); 2 partial; the "BEHAVIORAL" credits in the original ledgers were vacuous |
| "Oracle fixed confidence inversion" | FALSE — M11 inverted-confidence rate = 1.00 on abs-stepped in BOTH arms; oracle does not touch the brief's pin path |
| "L5 governor 3/3 on-target" | MISATTRIBUTED — the 3/3 on-target fires came from the SPEC obligation producer (`reason="test_evidence_gap"`, `layer="spec"` in `gt_oracle.py`), not L5's `_l5_no_test_evidence_nudge` (different producer, different reason string) |
| "Suppression reduced chars 30–58%" | UNDERSTATED lower bound — actual: −9% (aiomonitor) / −23% (abs) / −58% (awilix); was quoted as 30–58% |
| "Agent life measurably easier" | FALSE — M13 steps-to-gold-edit WORSE in oracle arm (+1.89); 6/9 tasks longer |
| "Post-oracle distribution 40–60% inert, 5–10% useful, <5% harmful" | UNMEASURED — no ground-truth measurement exists; FIX-A brief-level launder still renders at entry #2 |
| Consensus K-of-N verdict "CORRECT on mini" | UNPROVEN — zero live fires in any run to date; code-complete, runtime-unverified |
| Substrate verdict "CORRECT" | INCOMPLETE — `GT_RETRY_ON_VERIFIER_FAIL` set nowhere in any workflow; `instance_id:null` blocks Wilcoxon pairing; cert over-claims `LSP_ACTIVE_VALID` at 0 conversions |
| "Top-1 wrong 3/10 in most recent run" | STALE — the actual most recent (oracle arm) has top-1 wrong on 4/9; P@3 regressed 0.63→0.59 |

### 16.4 Cross-language synthesis from the 9-task oracle audit

The oracle-arm trajectories (9 tasks, 5 languages) confirm and sharpen the §15.7 picture:

**The single dominant failure mode** is language-independent: the agent submits with issue-verbatim obligations unexecuted, after spending its verification budget on tests that do not exercise them. Go = integration with frozen existing behavior; Python = sentence-level semantic inversion; TS/Rust = API-shape compile contracts; JS = output-semantics details / architecture placement. All 9/9 losses are post-localization spec-compliance losses; 0/9 are localization losses.

**Oracle machinery scorecard (9-task run):**

| Mechanism | Fired | Right content | Wrong/defective | Consumed | Verdict |
|---|---|---|---|---|---|
| `test_evidence_gap` / obligation nudge | ~9 (1/task) | always issue-verbatim | **selection**: first-matched token in 4/9 a survivable/already-safe obligation; csstree fired on false "you have edited" premise (heredoc sensor gap, `edits=0.0`) | 4–5/9 — and where it hit the right obligation, those hidden tests PASSED | **the proven lever, mis-aimed and one-shot** |
| review-transition emission | **0 of 9** | — | **THE gap** — the canonical pre-submit moment went silent in every trajectory | — | Stage 3's Stage-1 ship proves the mechanism; zero runtime fires |
| `no_test_evidence` | 1 (boa step 188) | timing correct | satisfiable by pre-existing tests → false confidence | partial | needs obligation-targeted satisfaction |
| `failure_persisted` | 3 | — | **3/3 false positives** (pre-existing failures ×2; fd `git apply error:` ×1) — the Stage 5 FP-closure fix is NOT holding on this build | 0 | classifier fix live in code; build/digest lag means old code ran |
| no-new-state loop nudge | 0 | — | fd's 5× identical stale-binary loop (~75 wasted steps) hit silence | — | sensor not live or build lag |
| L1 brief / localization | 9/9 | 4–5 file-grain correct | **4–5 wrong, 2 RECURRING VERBATIM from the prior run** (abs-stepped `functions.go::New` HIGH wrong pin; aiomonitor `format_running_task_list` HIGH wrong-file coordinate + vendored `tailwind.js` at entry #2) | 1–2 | inverted-confidence is systemic and deterministic-recurring; inverted even after the rerank fixes shipped |

**Plumbing defects measured live:** heredoc edit-detection gap (csstree `edits=0.0`, abs-stepped test-file writes unseen, katex partial); `[gt-patch:loaded]` agent-visible leak (abs-module — the 2026-06-10 stderr fix did NOT hold on this build path); doubled `<gt-task-brief>` wrapper (`brief_delivered: 2.0` on every task); instruction-text corruption (`:end:` → 🔚, echoed into agent code); truncated "Expected behavior" extracts cut mid-token; deep-metrics accounting blind (`gt_injected_tokens_total=0.0`, `per_layer={}` against 20–72KB observed GT text).

### 16.5 Open issues added or re-prioritized by this session

| # | Issue | Priority | Notes |
|---|---|---|---|
| A | **Self-verification retry gate never armed** (`GT_RETRY_ON_VERIFIER_FAIL` in no workflow) | BLOCKS-BENCHMARK | The closer for both causal wins; missing plumbing gap; set the env var in `deepswe_full.yml` |
| B | **Review-transition obligation emission silent 0/9** | Highest product lever | Stage 3 code shipped; zero runtime fires confirm the trigger or routing is wrong in the deployed build |
| C | **FIX-A brief-level launder open** (aiomonitor `tailwind.js` at entry #2 in both arms) | High | Fact-filter protects FACT ROWS; ranking/graph-map/scope-chain surfaces unprotected; re-arms every run |
| D | **Inverted-confidence HIGH-wrong pins systemic** (abs-stepped `functions.go::New`; aiomonitor `commands.py::format_running_task_list`) | High | Both recurring verbatim across two runs; the callee-attribution fabrication bug (wrong file in the coordinate) is distinct from the rerank issue |
| E | **`instance_id: null`** in all outcome.json — blocks paired Wilcoxon | BLOCKS-BENCHMARK | `deepswe_outcome.py:476-479` finds nothing in pier result shape |
| F | **Outcome misclassification** (`cert_fail→failure_class=GT` without §12 witness reconciliation) | BLOCKS-BENCHMARK | `deepswe_outcome.py:256-257`; G4 from GAP_ANALYSIS |
| G | **Contract renderer strips where-clauses / trait bounds** (boa [86]) — Stage 5 spec'd | High | `contract_map.py:626–665` fix code-present; zero runtime confirmation; no run has re-exercised the boa shape since the fix |
| H | **§14 feature-shaped context / ADD-mode sibling-shape producer** — not built | Deep product gap | Design complete (§14); all three audits confirm this is the dominant context-gap for feature tasks (~60% wrong-KIND); nothing built |
| I | **`failure_persisted` FP-closure and loop sensor not holding on deployed build** | Medium | Stage 5 code is committed; build lag (7 unpushed commits) means old code ran in the oracle run; push + substrate rebuild required |
| J | **Obligation selection mis-aimed** (first-matched token, 4/9 survivable obligations) | Medium | Fix: rank candidates by error-identifier/exactness class + `tested?=false` status |
| K | **Patch capture pollution** (csstree `package-lock.json`; arktype ~300KB regenerated artifact) | BLOCKS-BENCHMARK | Harness workspace-diff captures toolchain side-effects; needs exclusion list |

---

## 17. Bugfree program — validation_27367976952, checkpoints 001–010 (2026-06-11)

> **Last verified 2026-06-11.** Branch `gt-trial`. Master handoff:
> `.claude/reports/runs/validation_27367976952/GT_BUGFREE_HANDOFF_FULL.md`.
> This section is the **architecture-of-record** for the bugfree layer-by-layer fix
> program; run-folder checkpoint docs are the per-boundary evidence trail.

### 17.1 Goal and product model

Make GT a **product-quality context provider**, not a benchmark-maxxing patch set.

| State | Meaning |
|---|---|
| **Delivered** | Agent saw the GT text |
| **Used** | Agent's next action followed the GT text |
| **Enforced** | Agent could not submit without satisfying/checking the GT obligation |

Target control loop:

```text
graph / LSP / semantic / issue obligations
  → trusted context store
  → trajectory sensor
  → phase detector
  → context selector
  → small GT intervention
  → consumption/enforcement measurement
```

Product rule: **right context, at the right moment, in the smallest useful form,
with a clear next action, and proof the agent used it.**

Stage discipline (from `CLAUDE.md`):

1. **Stage 1 — Stabilize:** deterministic, stable delivery; prove with controlled tests on the real binary; a given task may never flip — that is irrelevant to Stage 1.
2. **Stage 2 — Flips:** paired GT-on vs frozen baseline only after Stage 1 holds. **Never rerun GT-OFF baseline** — pair against `.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`.

### 17.2 Reference run and current score

| Field | Value |
|---|---|
| Run id | `validation_27367976952` |
| Tasks | 9 evaluated (10-task matrix; arktype infra-missing) |
| Resolved | **0/9** at run time |
| Artifacts | `.claude/reports/runs/validation_27367976952/` (~411MB trajectories) |

Checkpoints **001–010 shipped** (2026-06-11). CP001–003: metrics fallback, no-leak verify, embedder cert truth. CP004–010: evidence oracle waiver, LSP product readiness, task truth, consumption ledger, infra subtypes, path_policy, patch hygiene. See `HANDOFF_AFTER_CHECKPOINT_010.md` and `CONTEXT_GAP_AUDIT_27367976952.md`.

### 17.3 Bug ledger B1–B11

| Bug | Description | Status (2026-06-11) | Next checkpoint |
|---|---|---|---|
| B1 | Deep metrics false zero for GT injection | **FIXED** (`060eccc9`) | — |
| B2 | Embedder cert vs metrics contradiction | **FIXED** (`e013c7be`) | — |
| B3 | Exact test leak in DeepSWE runtime | **FIXED** (`956c32e1`) | — |
| B4 | LSP warm over-credit (Go/Rust 0 conversions) | **FIXED** (`7a554ec8`) | — |
| B5 | Contradictory task truth (cert vs witness vs outcome) | **PARTIAL** (`9a7ce8b4`, `eae6667a`) | cross-run audit |
| B6 | No consumption/agreement ledger (Delivered ≠ Used) | **FIXED** (`6c0b1af6`) | — |
| B7 | Low-value surfaces (vendor/static/generated in brief) | **FIXED** (`f5dee492`) | — |
| B8 | Patch hygiene (lockfile/noise vs source fix) | **FIXED** (`eb5cfe5f`) | — |
| B9 | Outcome schema / `resolved:null` rows | **PARTIAL** (`9a7ce8b4`) | paired metrics unify |
| B10 | Infra/capture classification (ENOSPC, missing artifact) | **FIXED** (`eae6667a`) | — |
| B11 | Runtime evidence delivery (`test_verified_adapter` 2 fails) | **FIXED** (`d7da2bc2`) | — |

### 17.4 Checkpoint map 004–010

Each checkpoint = **one boundary, one commit, one regression test, one LIPI pass, full documentation.**

| CP | Layer / topic | Primary files | Done when |
|---|---|---|---|
| **004** | L4 evidence delivery (B11) | `artifact_deepswe/gt_mini_patch.py`, `tests/test_verified_adapter.py` | **SHIPPED** `d7da2bc2` — 23/23 |
| **005** | LSP product readiness (B4) | `src/groundtruth/resolve.py`, `scripts/metrics/foundational_gates.py`, `tests/fail_closed/test_lsp_liveness.py` | **SHIPPED** `7a554ec8` — 22/22 liveness |
| **006** | Task truth ledger (B5, B9) | `scripts/swebench/task_truth.py`, `scripts/verify/deepswe_outcome.py` | **SHIPPED** `9a7ce8b4` — 3/3; B5/B9 partial |
| **007** | Consumption ledger (B6) | `scripts/swebench/consumption_ledger.py`, `scripts/swebench/gt_deep_metrics.py` | **SHIPPED** `6c0b1af6` — 2/2 |
| **008** | Infra classification (B10) | `scripts/verify/deepswe_outcome.py` | **SHIPPED** `eae6667a` |
| **009** | Surface policy (B7) | `src/groundtruth/delivery/path_policy.py` (+ localizer, brief, post_view) | **SHIPPED** `f5dee492` — 3/3 |
| **010** | Patch hygiene (B8) | `scripts/swebench/package_submission.py`, `convert_to_submission.py` | **SHIPPED** `eb5cfe5f` — 3/3 |

### 17.5 Checkpoint 004 — root cause (B11, verified 2026-06-11)

**Symptom:** `python -m pytest tests/test_verified_adapter.py -q` → 2 failed, 20 passed.
Failures: `test_wrap_execute_appends_evidence_on_fake_env`,
`test_abs_testbed_view_resolves_same_pillar_as_relative`.

**Mechanism:** Oracle route ON by default (`GT_ORACLE_ROUTE≠0`). `_evidence()` builds a
valid `[WITNESS]` body from graph.db, but `l3b.evidence` enters `_oracle_gate_blocks` with
`edit_bound=False`. When `_oracle_focus()` is empty (no `gt_issue_anchors.json`, no prior
edits), the relevance gate suppresses the candidate as `irrelevant` — output stays bare.

**Ruled out:** `_to_repo_rel` (/testbed strip — unit test passes); `_connect_ro` + `immutable=1`
on substrate path (opens OK on Windows temp DB).

**Fix (LIPI-safe, §15.3):** VIEW is relevance-gated, but the **view/edit event bounds** the
evidence — set event-bound waiver on `l3b.evidence` when `_classify(cmd)` is `post_view` or
`post_edit` with a resolved file (same contract as edit-bound contract/cochange candidates).
Do **not** disable the oracle gate globally.

**Pre-flight:**

```powershell
Set-Location <repo>
python -m pytest tests/test_verified_adapter.py -q
```

### 17.6 Documentation protocol (required every checkpoint)

Documentation is part of the deliverable. **§17 is the index; run-folder docs are the evidence.**

#### A. Per-checkpoint (force-add: `git add -f`)

Path: `.claude/reports/runs/validation_27367976952/`

| Artifact | Purpose |
|---|---|
| `CHECKPOINT_00N_<LAYER>_<TOPIC>.md` | Boundary, root cause, code changes, verification output, LIPI checklist, product impact, bug-row delta |
| `HANDOFF_AFTER_CHECKPOINT_00N.md` | Rolling handoff: cumulative commits, updated B4–B11 table, next start command |

**Checkpoint doc template sections:** Date + commit · Boundary (in + out of scope) · Bug addressed · Code changes · Verification (exact commands) · LIPI (§7 / §12 / §15) · Product impact · Bug ledger delta.

#### B. Trajectory context-gap audit

`CONTEXT_GAP_AUDIT_27367976952.md` — after CP004, for adaptix, fd, katex, abs:

- First GT delivery turn → next 3 agent actions → trajectory finding
- **Context gap:** what GT sent vs what was needed (never "model stochasticity")
- Hypothesis whether CP004+ would have changed the decision (evidence-backed)

#### C. Session closeout (after CP010)

| File | Update |
|---|---|
| `GT_BUGFREE_HANDOFF_FULL.md` | Final B4–B11 status, commit stack, next session |
| `LATEST_TASK.md` | Session handoff block |
| `HANDOFF_AFTER_CHECKPOINT_010.md` | Full session summary |
| `PROGRESS.md` | One milestone line per shipped checkpoint |
| **This file (`gt_gt.md` §17.3–17.4)** | Bug ledger + checkpoint status rows |

#### D. Rules

1. Code commit and checkpoint doc in the **same commit** (no doc-less fixes).
2. Force-add gitignored run-folder docs.
3. Include terminal output blocks (copy-paste verification).
4. Cross-link prior checkpoints when a fix depends on an earlier boundary.
5. Grep adjacent surfaces for the same bug pattern before committing (handoff rule).

### 17.7 Execution rules (all checkpoints)

1. One boundary per commit — never mix surfaces.
2. Regression test red→green before commit.
3. LIPI against §7 (gates), §12 (per-layer role), §15 (oracle) for the touched layer.
4. Document before commit (§17.6).
5. Do not rerun GT-OFF baseline.
6. Trajectory wins are judged by **right trajectory**, not resolve alone (`CLAUDE.md`).

### 17.8 Ten missing architectural pieces (handoff reference)

These guide CP006–010 and beyond; not all are closed by 004–010 alone:

1. Trajectory-state controller (phase → speak/silence)
2. Context selection policy (ORIENT / VIEW / EDIT / VERIFY / SUBMIT)
3. Consumption feedback loop (CP007)
4. First-class obligation model (status vector per clause)
5. Pre-submit gate
6. Verifier-fail retry plumbing
7. Trust-gated context surfaces (CP005, CP009)
8. Context budgeting
9. Graph-to-action translation
10. Flip/trajectory scorecard (steps_saved, consumed, gt_caused_flip)

One-sentence diagnosis (unchanged from handoff): GT has substrate and evidence generation,
but lacks a strong just-in-time controller, obligation tracker, trust gate, and
enforcement/retry loop that turn context into trajectory changes.
