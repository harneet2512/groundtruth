# GroundTruth — True Architecture Layers (2026-06-01)

Reconciled from `DOC_OF_HONOR.md` (base 2026-05-27), `we_did.md` (audit 2026-05-28/29),
and the ICEMAN session (2026-05-31/06-01, 16 commits). Where they conflict, the session
commits are authoritative (latest corrections + verifier-found fixes).

## Session changes (ICEMAN 2026-05-31/06-01)

| Commit | Layer | What changed |
|---|---|---|
| `55ab30eb` | L0.5 | P0 consumer suppress — name_match never a deterministic FACT |
| `e1bc266b` | L0.5/parser | Go receiver methods linked to struct (Consistency pillar) |
| `a3b0515a` | L2.3 | Anchor provenance — title symbols outrank traceback noise |
| `ceaa14d4` | L2.1/L2.3 | 2-tier anchor quality — prose-demotion + code-symbol def-site |
| `6765261d` | L2.1 | Frame parser — installed-package tracebacks no longer dropped |
| `fa152d4a` | L0.9/LSP | Short lang aliases (ts/rs/js/py) for resolve dispatch |
| `664d9b3e` | L0.5 | Rust import keys — strip .rs, fold mod.rs, fix map mutation |
| `cecb4893` | L0.5/parser | Unified 5-lang: scoped_identifier, Rust constructors, imp_qtin |
| `00689e5b` | L0.5/parser | Rust module tree + unified re-export tracking (TS/JS/Rust/Python) |
| `643321d2` | C6 | Unified 5-language LSP enrichment in pre-index |
| `c5812172` | all | Runtime LSP enabled everywhere (GT_LSP_VERIFY=1 default) |
| `4280fe31` | L0.5 | Rust crate/src/module probe in resolveModulePath |
| `571bda88` | L0.5 | Cargo.toml glob expansion + type_flow :: separator (verifier bugs) |

---

## Layer Map

| Layer | Name | Module (LIVE) | Trigger | Status |
|---|---|---|---|---|
| 0 | Graph Foundation | `gt-index` Go binary -> SQLite `graph.db` | Pre-index (GHA/codespace) | WORKING |
| 0.5 | Resolution Pipeline | `resolver.go` Resolve() | During indexing (pass 3) | WORKING (13 strategies post-session) |
| 0.6 | Assertion Resolution | `main.go` resolveAssertionTarget() | During indexing (pass 4) | WORKING |
| 0.7 | Serde Pair Detection | `main.go` detectSerdePairs | During indexing (pass 4d) | WORKING |
| 0.8 | Structural Twin Detection | `main.go` detectStructuralTwins | During indexing (pass 4d) | WORKING |
| 0.9 | Import Extraction | `parser.go` extractImports() | During indexing (pass 2) | WORKING (18 languages) |
| 0.10 | Incremental Reindex | `main.go` runIncremental() | CLI `-file=` flag | WORKING |
| 0.11 | Pre-Index Orchestration | GHA workflow / codespace_run.sh | Before agent starts | WORKING |
| 1 | Path Resolution | `path_resolver.py` | Query-time | WORKING (not swept) |
| 2.1 | L1 Brief (Task Start) | `v1r_brief.py` (LIVE) | Task initialization | WORKING |
| 2.1+ | L1+ Orientation | `composite.py` + wrapper | Task start (with graph.db) | WORKING |
| 2.2 | L3 Post-Edit | `post_edit.py` | Agent edits a file | WORKING |
| 2.3 | L3b Post-View | `post_view.py` | Agent reads a file | WORKING |
| 2.4 | L4a Auto-Query | wrapper | First source file read | RETIRED |
| 2.5 | L5 Scaffold Governor | wrapper | Scaffold file without source edits | WORKING |
| 2.6 | L5b Late Reminder | wrapper + `hooks.py` | Unexamined structural signal | WORKING |
| 2.7 | L6 Reindex | wrapper | Agent edits a file | WORKING |
| 2.8 | L6 Pre-Submit Verify | wrapper | Edit->review transition | WORKING |
| 2.9 | Grep Intercept | wrapper | Agent runs grep/rg | WORKING |
| 3 | Consensus / Localization | wrapper | Agent views brief candidate | WORKING |
| 4.1 | MCP Tools | `server.py` | Agent calls tool | WORKING (0% adoption) |
| 4.2 | L4b Tool-as-Hooks | wrapper via `classify_tool_event()` | OH native tool events | WORKING |
| 4.3 | Stuck Detector Compat | wrapper | Repeated identical observations | WORKING |
| 5.1 | Dedup | wrapper | Every evidence delivery | WORKING |
| 5.2 | Evidence Budget | Various | Every evidence delivery | WORKING |
| 5.3 | Observability | wrapper `_is_hidden_line()` | Every observation | WORKING |
| 5.4 | Delivery Ledger | wrapper `_deliver_or_trace()` | Every evidence delivery | WORKING |
| 5.5 | Condenser | N/A | N/A | DISABLED (by design) |
| 5.6 | Preflight | Various shell scripts | Before/during task | WORKING |

---

## Resolution Pipeline (Layer 0.5) — post-session state

13 strategies (ordered by confidence):

| Stage | Strategy | Confidence | Tier | Languages |
|---|---|---|---|---|
| 1.0 | same_file | 1.0 | CERTIFIED | all |
| 1.25 | import-verified | 1.0 | CERTIFIED | all with extractImports |
| 1.75 | self/this method via class | 1.0 | CERTIFIED | Python/JS/TS/Java |
| 1.9 | verified_unique (demotes qualified stdlib) | 0.95 | CERTIFIED | all |
| 1.93 | import-scoped type_flow (supports ::) | 0.95 | CERTIFIED | all |
| 1.95 | type_flow qualified (supports ::) | 0.9 | CERTIFIED | all incl Rust |
| 1.96 | assignment-flow (PyCG + Rust ::new) | 0.9 | CERTIFIED | all |
| 1.97 | return-type bridging | 0.85 | NEW | all |
| 1.98 | unique-method-class | 0.85 | NEW | all |
| 2.0 | name_match fallback | 0.2-0.6 | SPECULATIVE | all |

Post-session additions: Rust `::` separator in 1.93/1.95, Rust constructor
patterns (`Type::new/default/from`) in 1.96, `scoped_identifier` handling in
parser, Go receiver method parenting, Cargo.toml glob expansion.

---

## Anchor Extraction (post-session)

`extract_issue_anchors` in `anchors.py` — 3-tier provenance:

| Tier | Weight | Source | Research |
|---|---|---|---|
| code_symbols | 300 | Backtick-wrapped (reporter marked as code) | arXiv:2512.07022 2025 |
| title_symbols | 200 | Issue title / ATX headings | BugLocator ICSE 2012 |
| body anchors | 100 | Everywhere else | baseline |
| prose-demoted | REMOVED | Short lowercase words in prose only (check/set/get) | JSS 2025 |

Frame parser: installed-package tracebacks (`site-packages/`) now stripped
to repo-relative paths. W_FRAME=0.60 fires on the most common SWE-bench
pattern. arxiv 2412.03905: deepest in-repo frame = 98.3% bug correlation.

---

## Edge Filtering (post-session)

Categorical `_edge_filter_for_db()` — name_match NEVER a deterministic FACT:

| Signal | Treatment |
|---|---|
| resolution_method IN (same_file, import, verified_unique, type_flow, import_type, lsp_verified, lsp) | FACT — admitted |
| CERTIFIED/CANDIDATE tier with resolution_method != name_match | Admitted |
| trust_tier = SUPPRESSED | Hard excluded |
| name_match (any confidence) | NEVER a fact — suppressed from caller delivery |

---

## 5-Language Graph Quality (post-session, measured)

| Language | det% static | import% | type_flow | method parenting | LSP server |
|---|---|---|---|---|---|
| Python | 47% | 18% | 2% | N/A (lexical nesting) | pyright |
| Go | 52% | 18% | — | 91% (receiver methods) | gopls |
| TypeScript | 49% | 22% | — | N/A | typescript-language-server |
| Rust | 33% | 2% | 5% (NEW: :: fix) | 100% (impl_item) | rust-analyzer |
| JavaScript | ~49% | similar to TS | — | N/A | typescript-language-server |

Runtime LSP enabled (`GT_LSP_VERIFY=1`) — promotes name_match -> lsp_verified
on every L6 reindex. Offline C6 enrichment runs per-language with dep setup.

---

## Four Pillars

| Pillar | Fires | Edge-Dependent? | Layers |
|---|---|---|---|
| Contract (signature, return type) | ALWAYS | No | L3 (priority 0.5+2), L3b (always-fire `_contract_pillar`), L1+ |
| Consistency (twins, patterns, siblings) | ALWAYS | No | L3, L4b-4 obligation check |
| Callers (who uses this) | When edges exist | YES | L3 (P1), L3b, Grep intercept, Curation map |
| Completeness (co-change, scope) | ALWAYS | No | L3, L4b-4, Consensus |

---

## Agent-Facing Display Rules

- No confidence tier labels ([VERIFIED]/[WARNING]/[INFO] removed)
- No prescriptive directives (diagnostic facts only)
- Content-type markers only: [SIGNATURE], [BEHAVIORAL CONTRACT], [CALLERS], [CONTRACT], [TEST], [COMPLETENESS], [CO-CHANGE], [SIMILAR], [OVERRIDE], [GT_VERIFY], PRESERVE:
- U-shaped ordering: [SIGNATURE] first (primacy), [TEST]/[COMPLETENESS] last (recency)
- Correct-or-quiet: assert only verifiable facts, silent when uncertain

---

## Delivery Topology

```
[PRE-INDEX] (GHA/codespace, before agent)
    gt-index -root=/testbed -> graph.db
    C6 LSP enrichment (per-language, with dep setup)
    GT_PREBUILT_GRAPH_DB -> wrapper
    |
    v
Issue text -> anchor extraction (3-tier provenance)
    |
    v
L1 Brief (v1r_brief.py) + L1+ Orientation (composite.py)
    + Frame parser (W_FRAME=0.60) + Code-def (W_CODE_DEF=0.70)
    |
    v
Agent loop (all layers gated on `not _GT_BASELINE`)
    |
    +-- Agent views file --> Consensus (if brief candidate, before edits)
    |                    --> L3b Post-View (Contract always-fire + callers)
    |
    +-- Agent edits file --> L6 Reindex (gt-index -file, FIRST)
    |                    --> Runtime LSP promotion (GT_LSP_VERIFY=1)
    |                    --> L3 Post-Edit (12+ evidence types, 2000 char budget)
    |                    --> L4b-4 Obligation check
    |
    +-- Agent edits scaffold --> L5 Scaffold Governor (diagnostic only)
    |
    +-- Edit->review transition --> L6 Pre-Submit Verify (ONCE, tests only)
    |
    +-- Repeated identical obs --> Stuck Detector Compat (skip GT)
```

---

## Live Trajectory Results (4 tasks, this session)

| Task | Edits | GT layers correct | Brief #1 | Decisive GT signal |
|---|---|---|---|---|
| beets-5495 | 2 (dual-site) | All | correct | [TWIN] two-site completeness |
| flask-5637 | 3 | All (post-fix) | wrong->fixed | Consensus override |
| loguru-1297 | 1 | All | wrong->fixed (frame parser) | [CONTRACT] aware_now() |
| cfn-lint-3767 | 0 | All | correct | L5 diagnostic (agent over-explored) |
