# GroundTruth Integration for Mini-SWE-Agent (DeepSWE)

This document tells mini-swe-agent what GT is, what it does, and exactly how
to integrate it at the SAME depth as the proven OH wrapper — every layer,
every trigger, every signal, at the right time.

## What GT Is

GT is a deterministic codebase intelligence system. It indexes source code into
a SQLite call graph (`graph.db`), then injects evidence into the agent's
observation stream at precise moments: when the agent reads a file (contract +
callers), when it edits (signature + propagation + tests), when it's stuck
(diagnostic nudge). All evidence is $0 (no LLM), deterministic, and
correct-or-quiet (silent when uncertain, never lies).

## What GT Is NOT

- Not a prompt hack (no "edit X first" directives)
- Not a benchmark trick (no task IDs, gold labels, or SWE-bench-specific logic)
- Not an LLM (all evidence from tree-sitter + SQLite + LSP, zero API calls)

## The Product: One Surface, 5 Languages

GT handles Python, Go, Rust, TypeScript, JavaScript through ONE indexer
(`gt-index`), ONE resolver (`resolver.go` with 13 strategies), ONE LSP
enrichment pipeline (`resolve.py` dispatching to pyright/gopls/rust-analyzer/
typescript-language-server), ONE set of delivery hooks (`post_edit.py`,
`post_view.py`). Language-specific code lives only in tree-sitter specs and
import extractors — the resolution algorithm and all delivery layers are
language-agnostic.

---

## The Full Layer Stack (what must fire, when, and why)

### Phase 0: BEFORE the agent starts

**L0 — Build graph.db**
- Run `gt-index -root=$REPO_ROOT -output=/tmp/graph.db`
- This produces: 7 tables (nodes, edges, properties, assertions, cochanges,
  file_hashes, project_meta), 23 property kinds, 13 resolution strategies
- Schema: v15.2-trust-tier with categorical columns (trust_tier, candidate_count,
  resolution_method, evidence_type)
- The graph is the FOUNDATION — every downstream layer reads it

**C6 — LSP enrichment (offline, per-language)**
- For each language in the graph, run its LSP server to promote name_match
  edges to lsp_verified:
  - Python: `pip install -e . && python -m groundtruth.resolve --db graph.db --root $REPO --resolve --lang python`
  - Go: `go mod download && python -m groundtruth.resolve --resolve --lang go`
  - Rust: `cargo fetch && python -m groundtruth.resolve --resolve --lang rust`
  - TS/JS: `npm install && python -m groundtruth.resolve --resolve --lang typescript`
- Each pass promotes ambiguous edges using textDocument/definition
- Requires `pydantic` installed for the LSP client

**L1 — Generate the brief**
- `from groundtruth.pretask.v1r_brief import generate_v1r_brief`
- `brief = generate_v1r_brief(issue_text, graph_db, repo_root)`
- Produces `<gt-task-brief>` with ranked files + callers + tests + `<gt-graph-map>`
- Uses the v7.4 hybrid ranker (BM25 + graph reach + anchor proximity + hub penalty)
- Anchor extraction with 3-tier provenance: code_symbols (backtick, 300) > title (200) > body (100)
- Prose-only common words (check/set/get) demoted to prevent false graph witnesses
- Frame parser: installed-package tracebacks stripped to repo-relative (W_FRAME=0.60)
- Code-def signal: backtick symbols resolved to definition files (W_CODE_DEF=0.70)
- INJECT this brief into the agent's FIRST instruction/system prompt

**Issue anchors**
- `from groundtruth.pretask.anchors import extract_issue_anchors`
- `anchors = extract_issue_anchors(issue_text, graph_db)`
- Write to `/tmp/gt_issue_anchors.json`: `{symbols, paths, test_names, title_symbols, code_symbols}`
- Downstream hooks (post_view, post_edit) read this file for relevance ranking

### Phase 1: DURING the agent loop — on every FILE READ

When the agent reads/cats/views a source file, fire L3b:

**L3b — Post-View (Contract pillar)**
- `from groundtruth.hooks.post_view import graph_navigation`
- `evidence = graph_navigation(graph_db, file_path, ...)`
- Produces: `[CONTRACT] def func(args) -> RetType` (ALWAYS fires, no edges needed)
  + `Called by: caller.py:45 [controller]` + `Calls into: dep.py::func`
- Contract pillar uses 3-tier anchor relevance: code(300) > title(200) > body(100)
- Categorical edge filter: only FACT edges shown as callers (name_match suppressed)
- APPEND this evidence to the observation the agent sees after the file read

**§3 — Consensus (first brief-candidate view)**
- On the FIRST time the agent views a file that was in the brief's candidate list:
- Fire consensus: `<gt-scope files="N"> 1. target.py — primary target ...`
- Shows the connected scope (callers, co-changed, test files)
- Fires ONCE per task, before any source edits

### Phase 2: DURING the agent loop — on every FILE EDIT

When the agent edits a source file (str_replace, sed -i, write, etc.):

**L6 — Reindex FIRST**
- `gt-index -root=$REPO_ROOT -file=$RELATIVE_PATH -output=$GRAPH_DB`
- Incremental: only re-parses the edited file, restores incoming edges
- MUST fire BEFORE L3 post-edit (ordering is load-bearing — L3 reads fresh edges)

**Runtime LSP promotion (GT_LSP_VERIFY=1)**
- After L6 reindex produces new name_match edges, the LSP background promotion
  module promotes them to lsp_verified using the running LSP server
- Non-blocking, triggered on first tool call via `_ensure_lsp_promotion()`

**L3 — Post-Edit (the evidence engine)**
- `from groundtruth.hooks.post_edit import generate_post_edit_evidence`
- Produces 12+ evidence types in priority order (2000 char budget):
  1. `[BEHAVIORAL CONTRACT]` — guards, params, mutations (from properties table)
  2. `[CALLERS]` — caller code lines with usage classification
  3. `Calls into:` — outgoing call edges
  4. `[SIGNATURE]` — function signature + return type
  5. `[OVERRIDE]` — parent class methods (recursive CTE)
  6. `[TEST]` — test assertions covering this function
  7. `[TWIN]` — same function defined elsewhere (completeness signal)
  8. `[SIMILAR]` — fingerprint-similar functions
  9. `[CO-CHANGE]` — files that historically change together
  10. `[COMPLETENESS]` — shared-state obligation (AST-based)
- U-shaped ordering: [SIGNATURE] first (primacy), [TEST] last (recency)
- G7 isolation gate: 0-caller functions still get Contract/Consistency/Completeness
- APPEND this evidence to the observation the agent sees after the edit

**L4b-4 — Obligation check**
- AST-based: finds methods sharing `self.attrs` with the edited method
- `[COMPLETENESS] OtherMethod shares cache, db with EditedMethod`
- Fires regardless of graph quality (no edges needed)

### Phase 3: DURING the agent loop — on specific events

**L5 — Scaffold Governor (no source edits after N actions)**
- If the agent has edited only scaffold files (test_, reproduce_, debug_) and
  0 source files after a threshold (20-35 actions, adaptive by repo size):
- `[GT L5: No Source Edits] Iteration: N/100. You have run N actions with 0 source file edits.`
- DIAGNOSTIC ONLY — no file list, no "edit X first"
- Fires ONCE per task

**L6 Pre-Submit Verify (edit→review transition)**
- When the agent stops editing and starts reviewing (≥1 source edit, then ≥3
  actions without editing):
- `[GT_VERIFY] Tests covering your changed files (N edited) — run before finishing: pytest ...`
- Lists ONLY verified test→target links from assertions table
- Fires ONCE, while agent can still act

**Stuck Detector Compat**
- Fingerprint raw observation BEFORE GT modification
- If same (action, md5(obs)) pair appears in last 8 entries → SKIP GT injection
- Prevents GT from blinding OH's stuck detector (the 25× loop bug)

### Phase 4: AFTER the agent finishes

- Telemetry only — no agent-visible delivery (OH sets state=FINISHED before
  run_action, so any appended content is never read)

---

## Edge Filtering (the categorical rule)

ALL caller/callee evidence uses `_edge_filter_for_db()`:

| Signal | Treatment |
|---|---|
| resolution_method IN (same_file, import, verified_unique, type_flow, import_type, lsp_verified, lsp) | FACT — shown to agent |
| CERTIFIED/CANDIDATE tier with resolution_method != name_match | Shown |
| trust_tier = SUPPRESSED | Hard excluded |
| name_match (any confidence, any tier) | NEVER shown as a fact |

name_match edges are NEVER presented as confident callers. This prevents the
`os.walk → account.walk` laundering class (the P0 from this session).

---

## Display Rules (what the agent sees)

- NO confidence labels ([VERIFIED]/[WARNING]/[INFO] removed)
- NO prescriptive directives ("edit X first" removed)
- Content-type markers only: [SIGNATURE], [BEHAVIORAL CONTRACT], [CALLERS],
  [CONTRACT], [TEST], [COMPLETENESS], [GT_VERIFY], PRESERVE:
- Correct-or-quiet: silent when uncertain, never guesses

---

## How the OH Wrapper Does It (the reference implementation)

`oh_gt_full_wrapper.py` (~7300 lines) monkey-patches OH's `run_action`:

1. **Classifies** every OH event via `classify_tool_event()`: FileRead → post_view,
   FileEdit → post_edit, bash grep → grep_intercept, bash sed/tee → bash_edit
2. **Routes** to the right hook: L3b for reads, L6→L3 for edits, L5 for scaffolds
3. **Delivers** via `_deliver_or_trace()`: appends/prepends evidence to the
   observation, logs delivery for audit
4. **Deduplicates** via MD5 per-file per-layer (no repeated evidence)
5. **Filters** GT_META/GT_STATUS/GT_TRACE from agent-visible observations

## How Mini-SWE-Agent Should Do It

`gt_agent.py` + `gt_mini_patch.py`:

1. **install_spec()**: inject gt-index binary + gt_hook.py + gt_mini_patch.py
   into the container. Build graph.db. Generate brief.
2. **gt_mini_patch.py**: patches `Environment.execute()` to classify bash
   commands as edit/view, runs gt_hook.py post-edit/post-view, appends
   `<gt-evidence>` to the observation. Same function as OH's wrapper but
   adapted for Pier's agent loop.
3. **run()**: prepends the brief to the instruction. Agent runs normally.
   Evidence appears automatically in observations — agent doesn't need to
   call any GT tools manually.

The integration MUST replicate:
- L0 (graph.db built before agent starts)
- C6 (LSP enrichment — `pip install pydantic` + per-language dep setup)
- L1 (brief in instruction — generated from graph.db + issue text)
- L3b (contract pillar on every file read — automatic, not manual)
- L6→L3 (reindex + post-edit evidence on every file edit — automatic)
- L5 (scaffold diagnostic after threshold — automatic)
- §3 (consensus on first candidate view — automatic)
- Stuck-compat (skip injection on repeated observations)
- Dedup (per-file per-layer, no spam)
- Display rules (no leaks, no labels, correct-or-quiet)

If any of these layers is missing, the integration is INCOMPLETE and the
product is degraded compared to the OH path. The OH path is the reference;
mini-swe-agent must match it.

---

## Files

| File | What |
|---|---|
| `gt-index/cmd/gt-index/main.go` | Indexer entry point |
| `gt-index/internal/resolver/resolver.go` | 13-strategy call resolver |
| `gt-index/internal/parser/parser.go` | Tree-sitter AST extraction |
| `src/groundtruth/pretask/v1r_brief.py` | L1 brief generator |
| `src/groundtruth/pretask/anchors.py` | Issue anchor extraction (3-tier) |
| `src/groundtruth/pretask/v7_4_brief.py` | v7.4 hybrid ranker |
| `src/groundtruth/pretask/traces.py` | Stack-trace frame parser |
| `src/groundtruth/hooks/post_edit.py` | L3 post-edit evidence (12+ types) |
| `src/groundtruth/hooks/post_view.py` | L3b post-view (contract pillar) |
| `src/groundtruth/resolve.py` | LSP enrichment (5 languages) |
| `src/groundtruth/lsp/background_promotion.py` | Runtime LSP promotion |
| `scripts/swebench/oh_gt_full_wrapper.py` | OH reference implementation |
| `artifact_deepswe/gt_agent.py` | Mini-swe-agent GT integration |
| `artifact_deepswe/gt_mini_patch.py` | Observation interception for Pier |
| `benchmarks/swebench/gt_hook.py` | Standalone hook (AST-only fallback) |
