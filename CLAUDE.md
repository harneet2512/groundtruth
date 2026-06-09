# CLAUDE.md -- GroundTruth

> **Read this entire file before writing any code.**

---

## BASELINE ALREADY EXISTS — NEVER RERUN IT (durable rule)

The GT-OFF (baseline) full-300 verdicts are **already on disk** and are the canonical pairing
reference. **NEVER launch a baseline / GT-off run** — pair every GT-on result against this file:

- **Path:** `.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`
  (`resolved_ids` = the GT-OFF passes). Model: OH CodeActAgent + deepseek-v4-flash. **87/300 resolved.**
- A **positive flip** = GT-on RESOLVES a task whose id is **NOT** in `resolved_ids` (baseline=NO).
  The 213 non-resolved ids are the flip-candidate set.
- A **regression** = GT-on FAILS a task whose id **IS** in `resolved_ids` (baseline=PASS).
- Do not re-measure, re-grade, or re-run the baseline arm for any reason. It is frozen. Only run
  the **GT-on** arm, then diff against this file (paired Wilcoxon on per-task delta).

---

## KEY RULE — RESOLVED IS NOT THE PRIZE; THE TRAJECTORY IS

**Never be proud of a `RESOLVED` verdict. Be proud only of a RIGHT TRAJECTORY.** A resolve at
temp=1.0 is noisy and causally empty on its own — it can come from the agent self-localizing
(it does ~88% of the time without us), from luck, or from coincidence. None of those prove GT
did anything. The thing that counts is the **trajectory being right**:

> GT delivered **correct** context → the agent **consumed** it → it localized / reasoned
> **through** that context → it wrote the **correct fix for that reason**.

- **Right trajectory + resolved** = the real win (GT caused it). **Right trajectory + not
  resolved** (stochastic miss elsewhere) = still a GT win — the context was correct.
- **Wrong/absent trajectory + resolved** = NOT a GT win. Luck or self-localization. Do not
  claim it, do not count it, do not celebrate it.
- Therefore **always read the trajectory from the agent's own observations** (per the
  AGENT-OBSERVATION rule): did the gold reach the delivered brief, did the agent act on the
  GT-delivered evidence, was the edit driven by GT context? Lead every result with the
  trajectory finding, not the pass/fail. "Resolved" is a footnote to "the trajectory was right."

---

## THE TWO-STAGE METHODOLOGY — STABILIZE (1) BEFORE FLIPS (2). NEVER SKIP STAGE 1.

Building GT toward the goal happens in TWO stages, in order. Do not chase Stage 2 before
Stage 1 is solid. Confusing the two is the error that wastes runs.

**STAGE 1 — STABILIZE (do this FIRST).** GT must be a **deterministic, stable** product: it
acts the **same way every time**, delivers the **RIGHT context at the moment it is needed**, and
**adheres to CLAUDE.md + the architecture** (gt_gt / DOC_OF_HONOR). Stage 1 is proven by
**CONTROLLED, DETERMINISTIC verification** — same input → same correct output, on the real
graph/binary, asserted exactly. It is NOT proven by flips. **A given task MAY NEVER FLIP, and
that is irrelevant to Stage 1.** Stage 1 asks one question: *is the context GT delivers correct,
stable, and architecture-adherent?* Prove that, deterministically, before anything else.

**STAGE 2 — FLIPS (only once Stage 1 holds).** A flip = GT provides correct context **when it is
needed**, and that context **CONVERTS the agent — it changes the decision the agent was about to
make**, producing a correct fix the agent would not otherwise have written. Flips are the Stage-2
proof (paired GT-on vs the frozen baseline). They are pursued ONLY after GT is stabilized. Never
fixate on whether a specific task flipped while Stage 1 is unproven.

**NEVER OVERFIT.** Stage-1 determinism/correctness MUST be a GENERAL property — works on any
repo / task / language / agent. A "fix" that makes one specific task's context right (keyed to
that task's files/symbols/shape) is benchmaxxing, not Stage 1. Verify generality (held-out
inputs, multiple shapes), never tune to the task in front of you.

**Practical consequence:** when a lever is built/fixed, prove Stage 1 first — deterministic
unit/real-binary tests that assert the EXACT correct context on controlled inputs, plus an
architecture-adherence (LIPI) pass — and report it as "Stage 1: stabilized/correct" or not.
Only then run the live paired flip experiment (Stage 2). Do not grade a lever by whether one
live task flipped; grade Stage 1 by determinism + correctness + architecture-adherence.

---

## What It Is

GroundTruth is an MCP server that gives AI coding agents codebase intelligence -- for any language. It indexes source code into a SQLite call graph, then provides evidence-based briefings, validation, and symbol tracing to prevent hallucinations.

**How it works — ONE pipeline, not separate parts:**
1. `gt-index` (Go binary) parses source code with tree-sitter, extracts call graph + FTS5 index + LSP-enriched contracts into `graph.db`
2. At query time: FTS5 retrieval → graph traversal with path decay → LSP-enriched ranking → curated brief
3. MCP server reads `graph.db` and exposes 16 tools (trace, hotspots, symbols, explain, etc.)

**ONE product rule:** Graph, LSP, and FTS5 are capabilities of ONE pipeline — never separate mechanisms. LSP dispatches to the right language server by file extension — it is ONE language intelligence surface, not "4 servers."

**Works with any MCP client:** Claude Code, Cursor, Codex, Windsurf.

---

## Architecture

```
Source code (any language)
       |
       v
gt-index (Go binary, tree-sitter)
  - 30 language specs
  - 6 with import extractors (Python, Go, JS, TS, Java, Rust)
  - 24 with name-match fallback
  - Edge confidence scoring (1.0=verified, 0.2=ambiguous)
  - Edge deduplication
       |
       v
graph.db (SQLite)
  - nodes: functions, classes, methods
  - edges: calls with resolution_method + confidence
       |
       +---> MCP server (16 tools via FastMCP, stdio)
       |       Agent calls groundtruth_trace, groundtruth_hotspots, etc.
       |
       +---> gt_intel.py (SWE-bench evidence engine)
       |       7 evidence families, fully deterministic
       |
       +---> groundtruth resolve (LSP precision pass, diagnostic)
               Shows ambiguous edges, detects installed LSP servers
```

---

## Repository Structure

```
groundtruth/
+-- CLAUDE.md                              # This file
+-- README.md                              # User-facing docs
+-- pyproject.toml                         # Python package config
+-- LICENSE                                # MIT
+-- .mcp.json                              # Claude Code MCP config
+-- gt-index/                              # Go indexer
|   +-- cmd/gt-index/main.go              # CLI entry point
|   +-- internal/
|       +-- parser/parser.go              # Tree-sitter AST extraction
|       +-- resolver/resolver.go          # 3-stage call resolution
|       +-- store/sqlite.go              # SQLite schema + operations
|       +-- specs/                        # 30 language specs
|       +-- types/                        # Shared types
|       +-- walker/                       # File discovery
+-- src/groundtruth/                       # Python package
|   +-- main.py                           # CLI entry point
|   +-- resolve.py                        # LSP precision pass
|   +-- mcp/
|   |   +-- server.py                     # MCP server (FastMCP, stdio)
|   |   +-- tools.py                      # 16 tool handlers
|   +-- index/
|   |   +-- store.py                      # SymbolStore (Python indexer schema)
|   |   +-- graph_store.py                # GraphStore (Go indexer schema bridge)
|   |   +-- graph.py                      # ImportGraph (BFS traversal)
|   +-- ai/                               # AI layer (optional, graceful degradation)
|   +-- analysis/                          # Risk scoring, adaptive briefing
|   +-- lsp/                              # LSP client (used by Python indexer)
|   +-- validators/                        # Import/signature/package validation
|   +-- stats/                            # Intervention tracking
|   +-- viz/                              # 3D Code City visualization
+-- benchmarks/swebench/
|   +-- gt_intel.py                       # Evidence engine (7 families)
|   +-- run_mini_gt_hooked.py             # SWE-bench hook harness
+-- tests/                                # 648 tests (646 passing)
```

---

## graph.db Schema

```sql
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,        -- 'Function', 'Method', 'Class', 'Interface', etc.
    name TEXT NOT NULL,
    qualified_name TEXT,
    file_path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    return_type TEXT,
    is_exported BOOLEAN DEFAULT 0,
    is_test BOOLEAN DEFAULT 0,
    language TEXT NOT NULL,
    parent_id INTEGER REFERENCES nodes(id)
);

CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES nodes(id),
    target_id INTEGER NOT NULL REFERENCES nodes(id),
    type TEXT NOT NULL,         -- 'CALLS', 'IMPORTS'
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,    -- 'same_file', 'import', 'name_match'
    confidence REAL DEFAULT 0.0,  -- 0.0-1.0 (v14+)
    metadata TEXT
);
```

### Edge Confidence Model

| Resolution Method | Confidence | Meaning |
|---|---|---|
| same_file | 1.0 | Caller and callee in same file |
| import | 1.0 | Verified via import statement |
| name_match (1 candidate) | 0.9 | Only one function with this name exists |
| name_match (2 candidates) | 0.6 | Two possible targets |
| name_match (3-5 candidates) | 0.4 | Several possible targets |
| name_match (5+ candidates) | 0.2 | Highly ambiguous |

Evidence engine filters edges below MIN_CONFIDENCE (0.5) to prevent false positives.

---

## Go Indexer (gt-index)

### 4-Pass Architecture

1. **STRUCTURE** -- Walk filesystem, discover source files by language
2. **DEFINITIONS + IMPORTS** -- Parallel tree-sitter parse (NumCPU workers), batch SQLite insert
3. **CALLS** -- Resolve call references via 3-stage pipeline, compute confidence, deduplicate
4. **EXTRAS** -- Store metadata (build time, file count, workers, etc.)

### 3-Stage Resolution Pipeline

1. **Same-file** (confidence=1.0) -- callee name matches a definition in the same file
2. **Import-verified** (confidence=1.0) -- caller file imports callee name, resolved via file map
3. **Name-match** (confidence=0.2-0.9) -- fallback, matches by name across all files

### Language Support

**Tier 1 (import extractors):** Python, Go, JavaScript, TypeScript, Java, Rust
**Tier 2 (name-match only):** 24 additional languages via tree-sitter specs

### Building

```bash
# Requires Go 1.22+ and GCC (CGO needed for go-sqlite3)
# -tags sqlite_fts5 is MANDATORY: without it the FTS5 virtual table (nodes_fts,
# localizer pipeline stage 1) is silently compiled out and the run degrades to a
# Python name-match rebuild. GT_REQUIRE_FTS5=1 aborts indexing if the tag is missing.
cd gt-index
CGO_ENABLED=1 go build -tags sqlite_fts5 -o gt-index ./cmd/gt-index/

# Usage
gt-index -root /path/to/repo -output graph.db
```

---

## MCP Server

### 16 Tools

| Tool | Purpose | AI Cost |
|---|---|---|
| groundtruth_find_relevant | Find files relevant to a task | $0 (regex fallback) |
| groundtruth_brief | Proactive briefing before code generation | $0 (graph-based) |
| groundtruth_validate | Check proposed code against index | $0 (deterministic) |
| groundtruth_trace | Trace symbol callers/callees | $0 (pure SQL) |
| groundtruth_status | Health check + stats | $0 |
| groundtruth_dead_code | Exported symbols with zero references | $0 |
| groundtruth_unused_packages | Installed packages with no imports | $0 |
| groundtruth_hotspots | Most-referenced symbols | $0 |
| groundtruth_orient | Codebase structure overview | $0 |
| groundtruth_checkpoint | Session progress summary | $0 |
| groundtruth_symbols | List symbols in a file | $0 |
| groundtruth_context | Symbol usage context with snippets | $0 |
| groundtruth_explain | Deep dive into a symbol | $0 |
| groundtruth_impact | Blast radius of modifying a symbol | $0 |
| groundtruth_patterns | Coding conventions in sibling files | $0 |
| groundtruth_do | Single entry point (auto-router) | $0 |

All tools are deterministic. AI layer (anthropic) is optional -- install with `pip install groundtruth[ai]`.

---

## Evidence Engine (gt_intel.py)

### 7 Evidence Families

| Family | Score | What It Produces |
|---|---|---|
| IMPORT | 2 | Correct import paths for cross-file callees |
| CALLER | 1-3 | Cross-file callers with usage classification |
| SIBLING | 1-3 | Behavioral norms from same-class methods |
| TEST | 1-2 | Test functions with assertions |
| IMPACT | 1-2 | Blast radius (caller count + critical path) |
| TYPE | 1-2 | Return type contracts |
| PRECEDENT | 2 | Last git commit touching this function |

### Output Format

```xml
<gt-evidence>
[VERIFIED] CAUTION: 3 callers in 2 files (0.67)
[WARNING] MUST return Optional[User] (0.33)
</gt-evidence>
```

Tiers: `[VERIFIED]` = confidence >= 0.9, `[WARNING]` = 0.5-0.9, `[INFO]` = < 0.5

### Usage

```bash
# Post-edit reminder
gt_intel.py --db=graph.db --file=src/users.py --function=get_user --reminder

# Pre-task briefing
gt_intel.py --db=graph.db --enhanced-briefing --issue-text="fix auth bug"
```

---

## Coding Standards

- Python 3.11+. Type hints. `mypy --strict`.
- Pydantic for data models. `structlog` for logging.
- `pytest` + `pytest-asyncio`. In-memory SQLite for unit tests.
- `ruff` for linting + formatting.
- Go 1.22+. `go-sqlite3` (CGO). `go-tree-sitter`.
- All SQLite queries use parameterized statements.
- Update PROGRESS.md after every milestone.

---

## graph.db IS THE CONTEXT GRAPH — METHOD-CALL EDGES ARE THE MAJORITY (2026-06-07)

graph.db is the **context graph of the codebase** — the agent's MAP of the code: who calls whom, what
reaches what. Its value is the **EDGES** (call relationships), not the nodes. If the edges are wrong or
missing, the agent navigates a false map → **cannot reach gold files → flies blind.** An edge that
stays `name_match` is a NAME GUESS, not a fact: it matched a callee by name across files and is
ambiguous (N functions/methods share that name). Only a *resolved* edge (`import` / `same_file` /
`type_flow` / `lsp` / `verified_unique`) is a fact.

**The 58% method gap (measured live, conan-17123, 7075 nodes):** 4087/7075 nodes (**58%**) are Methods,
so **method calls (`obj.method()`) are the MAJORITY of call edges.** Resolving a method call requires
the RECEIVER'S TYPE — name alone is ambiguous across classes. When that type isn't resolved, the edge
stays `name_match` = speculative. **If the method calls don't convert, ~58% of the context graph is
guesswork → the agent's map is mostly false → it cannot reliably reach gold.** This is not an
optimization; it is whether graph.db functions at all. If it fails on one task, assume it is flying
blind on most.

**The fix MUST be language-agnostic + generalized (per `.claude/CLAUDE.md`). Two generalized levers, no
per-language hacks:**
1. **Indexer-level receiver-type resolution (Go indexer, tree-sitter — all languages, no env):** the
   indexer already emits `type_flow` / `impl_method` / `inherited` / `inheritance` edges; extend it to
   track receiver types so more method calls resolve STRUCTURALLY at index time, before any fallback to
   `name_match`. Most generalized lever — tree-sitter, every language, no per-task environment.
2. **LSP precision pass (dispatched per language via `_KNOWN_SERVERS`):** for the residual, the
   language's own type-aware server (pyright/gopls/rust-analyzer/tsserver/jdtls) resolves the call via
   go-to-definition. Generalized by the dispatch map + by running it where the task's environment lives
   (the eval CONTAINER is the native env for ANY language — no per-language env logic).

**TRACED (2026-06-07, live pyright on conan-17123): the LSP resolves method calls CORRECTLY — the
failure is SCALE, not correctness.** 98% of name_match edges (**7147/7326**) are method calls; targets
are ultra-common names (`join` 1106, `get` 354, `exists` 316, `items` 254, `append` 228, `loads` 189,
`split` 122...) that name_match points at an arbitrary same-named method = garbage. The trace proved
pyright `definition` returns the RIGHT target (`super().__init__` → argparse/builtins/the real local
class; `StringIO.__init__` → stdlib), so the resolve already CLEANS them (stdlib→delete, internal→
correct — the gate-check's "Deleted 18 / Corrected 7"). **But it queries the LSP per-edge (~5/sec) and
caps at `--max-edges 500`** → on a 7147-edge repo it cleans ~7% and 93% stay garbage → the agent's
method map stays mostly false. That is the "flying blind."

**Efficiency design for big/huge repos (research-backed — do NOT per-edge-query the LSP at scale):**
- **SCIP batch indexing (Sourcegraph) — the scalable, language-agnostic precision pass.** A SCIP indexer
  (`scip-python` built directly on pyright; `scip-typescript`, `scip-go`, `scip-clang`, ...) runs the
  compiler's type analysis **ONCE over the whole project** and emits ALL defs/refs in one language-
  agnostic protobuf index — ~10–20% of LSIF size, ~10× faster in CI, **incremental on changed files**.
  GT resolves ALL name_match edges from that single index instead of N per-edge LSP round-trips. One
  indexer per language, same SCIP format → generalized by construction.
  (sourcegraph.com/blog/announcing-scip · github.com/sourcegraph/scip-python)
- **Indexer-level CHA/RTA (no env, every language, fastest).** Resolve `self.m()` / `super().__init__()`
  structurally via the class hierarchy GT already stores (`inheritance`/`inherited`/`impl_method` edges)
  — classic Class Hierarchy Analysis / Rapid Type Analysis — so the call never falls to name_match.
- **Never name_match builtin methods** (`join/get/append/items/split/loads`): a high-candidate-count
  name_match is not a fact — drop it or floor confidence at index time so it never pollutes graph.db or
  the closure. (Consumers already filter `<0.5`, but the *connectivity* must be real, not just filtered.)

**OPTIMIZED MECHANISM — propagate over graph.db's EXISTING facts; do NOT re-analyze or per-edge-LSP the
bulk.** graph.db already stores the resolver's inputs, paid for at index time (measured conan-17123):
hierarchy (`inheritance`/`inherited`/`impl_method` edges), declared types (`signature` on 6304/7075
nodes + `param` 5481 properties), assignments (`data_flow` 5284 properties), partial type resolution
(`type_flow` 4229). The indexer extracts all this, then **discards it and string-matches method calls
into `name_match`.** The fix is a propagation PASS over those existing facts — **XTA set-propagation**
(Tip & Palsberg, OOPSLA 2000; XTA +88% precision over RTA) for static langs over `signature`+hierarchy;
**PyCG assignment-graph** (Salis et al., ICSE 2021; 99.2% prec, *ignores external libs*) for dynamic
langs over `data_flow`. No re-parse, no LSP, no SCIP for the bulk. SCIP/LSP are demoted to the residual.

## SCALE — fast on big/huge repos (research-backed: DEMAND-DRIVEN, not exhaustive)
Exhaustively resolving every method-call edge per-edge is hours at scale (~5/sec × 500k edges). The
proven answer is to NOT resolve the whole repo — two tiers:
1. **Index-time (whole repo, ONCE, amortized, cached, parallel):** the cheap propagation above — CHA for
   `self`/`super` (free over the hierarchy) + XTA/assignment-graph over the existing facts. Near-linear
   (PyCG ~0.38 s / 1k LoC → ~6 min / 1M LoC), inside the existing parallel parse pass, written to
   graph.db. **INCREMENTAL:** `-file` reindex (SHA-256 short-circuit, `incremental.go`) re-propagates only
   the changed subgraph — the Bazel/Nx monorepo pattern (file-hash change detection → rebuild only what's
   necessary; 60–80% CI-time reduction). Whole-repo cost paid once, never repeated unchanged.
2. **Query-time (DEMAND-DRIVEN, scoped to the issue):** the expensive precision (LSP/type-inference for
   the residual propagation can't statically resolve) runs ONLY on the issue-relevant unresolved edges —
   the candidate subgraph the brief touches, not the repo. **Demand-driven analysis** (Heintze & Tardieu,
   *Demand-Driven Pointer Analysis*, PLDI 2001 — "just enough computation for the query variables," proven
   optimal, no wasted work; Sridharan & Bodík, PLDI 2006 — client-driven refinement, "response times
   suitable for IDEs"). Cost = O(issue-relevant residual) — a handful of edges — **bounded, independent of
   repo size.**

**Net time:** index-time bulk = minutes, amortized + incremental; per-query residual = seconds, constant
in repo size. No whole-repo per-edge LSP, no per-language SCIP toolchain. This is the only design that is
**generalized** (2 algorithm classes over the uniform tree-sitter graph), **precision-first** (XTA/PyCG,
correct-or-quiet), AND **scalable** (amortized propagation + demand-driven residual + incremental).

**Research basis:** XTA/RTA — [Tip & Palsberg, OOPSLA 2000](http://web.cs.ucla.edu/~palsberg/paper/oopsla00.pdf);
CHA (static-typed only) — Dean/Grove/Chambers, ECOOP 1995; PyCG (dynamic, ignores external libs) —
[Salis et al., ICSE 2021](https://arxiv.org/abs/2103.00587); JS approx CG — Feldthaus et al., ICSE 2013;
demand-driven — Heintze & Tardieu, PLDI 2001 + Sridharan & Bodík, PLDI 2006; incremental — Bazel/Nx
file-hash scoping; uniform multilingual tree-sitter + TypeRegistry + stdlib stubs (Graphify/ACER 2023).

---

## Live GHA log streaming (ngrok SSE) — 2026-06-07

`scripts/log_relay.py` tees a run to an ngrok-tunnelled SSE stream so a live GHA run is watchable with
one `curl.exe -N '<url>'` — **no `gh api` polling**. Wired into `swebench_30task.yml`'s agent step
(`… | tee full_run.log | python -u scripts/log_relay.py`); `${PIPESTATUS[0]}` (the wrapper exit) is
unchanged. Enable by setting the `NGROK_AUTHTOKEN` repo secret (no-op passthrough without it — never
breaks a run). **Full watch-protocol in `gt_trial.md` §3.1.** Harness note: read in **foreground bounded
chunks** (`curl.exe -N --max-time 55 "<url>"`, Windows `curl.exe` not the PS `curl` alias), never one
unbounded blocking call — the Bash/PS tool returns output only on command completion.

---

## Known Limitations

1. **70-80% of edges are name_match** on large repos -- confidence scoring mitigates but doesn't eliminate false positives
2. **Large repo indexing is slow** -- 78-136 min for 14K+ file repos
3. **Tier 2 languages** have no import resolution -- edges are speculative
4. **gt-resolve** is diagnostic only in v1.0.0 -- live LSP resolution is planned
5. **GraphStore bridge** is read-only -- Go indexer owns writes

---

## What NOT to Build

- No vector embeddings (FTS5 + graph queries are sufficient)
- No VS Code extension (MCP is universal)
- No web dashboard
- No daemon process (MCP stdio)
- No custom LSP server (we're a CLIENT)

## TTD Definition

TTD means Test-Trace-Driven debugging for GroundTruth benchmark plumbing.

In this project, TTD does NOT mean "write random unit tests."
It means:
- Start from a failed observed run artifact.
- Identify the exact trace/event/schema field that proves the failure.
- Write a behavior test that reproduces the reader/writer mismatch.
- Fix the writer, reader, or schema contract so the same artifact now passes.
- Only rerun smoke/canary after the test proves the failure mode is closed.

For the reference failure (smoke v5, 2026-04-24 06:18 UTC), the observed artifact is:
- raw counts show material_edit_total, ack_armed_total, steer_delivered_total, ack_engagement_total > 0
- verify_report still computes delivery_rate = 0.00 and engagement_rate = 0.00
- therefore this is a summary emission / rate denominator contract bug, not proof that steering was absent

Required TTD behavior:
- Do not classify a FAIL as a model behavior failure until the metric contract is proven correct.
- Do not rerun smoke as a substitute for fixing the metric contract.
- Add an end-to-end test that feeds a representative arm summary into verify_report and asserts nonzero delivery_rate / engagement_rate when the raw event chain is present.
- The test must fail before the fix and pass after the fix.

## Mandatory Analysis Rules

1. **NEVER claim "model failure" or "model stochasticity" as root cause.** If a task did not resolve, GT failed to deliver sufficient context. The model is frontier-class. Trace what context was missing or wrong. "Model behavior" is not a valid conclusion.

2. **NEVER claim any single evidence type (test assertions, callers, contracts, etc.) is THE lever for flips.** At this stage all evidence types are hypotheses under research. No evidence family is proven primary. If analysis suggests one type matters more, record it as secondary research to validate — do not build architecture around unproven claims.

3. **Context gap analysis is mandatory on every non-resolved task.** Compare: what did GT send vs what would the agent have needed to write the correct fix. The delta is the product bug.

---

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health

---

> **SUPERSEDED (2026-06-09) — historical only.** Current state of record: `gt_gt.md` §11–§13 +
> `LATEST_TASK.md` + `SESSION_SUMMARY.md`. What changed since this block: DeepSWE is the LIVE surface
> NOW (Datacurve DeepSWE, 113 tasks / 5 langs / pier + mini-swe-agent) — not "after OH is bug-free."
> `gt_hook.py`'s Python-`ast` path is being RETIRED (70% of DeepSWE is non-Python) in favor of the
> tree-sitter graph + the deep `v1r` engine (now carrying per-symbol MaxSim granularity + dense-floor
> fusion + the open-source code embedder). Decisions here are grounded on research+code+performance,
> not on this (or any) doc. Read the block below only for history.

## ICEMAN session (2026-05-31) — v15.2 census + pivot to the DeepSWE goal

Branch `gt-consensus-curation`. Commit `c8f59cc6` (`fix(lsp+localizer)`). Live testing
is **GitHub Codespaces ONLY** (never gcloud, never local, never GHA — no visibility).

### Shipped (c8f59cc6 — 2 files, the 8 dirty files untouched)
- `resolve.py` `_LANG_TO_EXT`: the runtime LSP precision pass was a **NO-OP for ALL 5
  languages incl Python** (`ext=f".{language}"`→`.python`, not a `LSP_SERVERS` key → `Err`
  → every edge skipped; the Pyright handshake fix was dead code). One lang→ext map fixes
  all. 71 resolve/lsp tests pass.
- `graph_localizer.py` generated-file demote (`W_GEN` + `_is_generated`): protobuf/codegen
  no longer out-ranks gold. Harm-reduction, hit@k-neutral. 23 localizer tests pass.

### Findings on the CORRECTED v15.2 binary (holdout, 60 tasks, 4 langs) — census collapsed
- **B phantom callers: RESOLVED by v15.2 itself** (name_match demotion + categorical gate;
  brief emits no laundered `Run() in …`). The earlier B=10% was a stale-binary artifact.
- **A localization: at its STRUCTURAL CEILING.** Localizer reaches gold 30/60; 24/30 misses
  are gold structurally unreachable from issue anchors (deeper-hop recovers 0, path-seed 1).
  Two research-motivated hypotheses **FALSIFIED by data** and reverted/withheld:
  hub-penalty (regressed python hit@1 8→6); module-degrade (11% precision → would misdirect
  89%). The localizer's abstain→grep-fallback is validated correct-or-quiet. **LSP enrichment
  is NOT the A lever (M2=0).**
- **C contract pillar:** suppression is correct under the granularity principle (post-view
  degrades to the graph-map pillar, not a function dump); a beets-5495 trajectory test defends
  it. Reverted an always-fire attempt that broke 2 trajectory tests.

### The DeepSWE goal (Path 3 ≠ the OH path audited above)
- DeepSWE = `pier run` + `GTMiniSweAgent` + **`gt_hook.py`** (agent-*invoked* CLI:
  `understand`/`verify`), 5 languages, **does NOT use graph.db**. The OH-path `beets-5495`
  run died on `docker buildx` (infra, not GT) — off-goal.
- `gt_hook.py` is **Python-`ast`-centric** (`_parse_safe`=`ast.parse`) → its AST evidence
  will degrade on go/rust/ts/js (the DeepSWE generalization gap, for AFTER OH is bug-free).

### Live OH run (codespaces, beets-5495, GT-on) — RESULTS
- **GT WORKS on OH** (the user strategy: nail OH first, then port to mini-swe-agent/DeepSWE).
  Pre-index graph 4827n/13940e; **consensus fired & correct** (`importer.py — primary target`,
  agent-visible); the agent patched the GOLD file `importer.py::ImportTask`. Localization is
  solved on the OH path — "no consensus" was wrong; it fires.
- **DOMINANT BUG found → fixed → proven red→green:** the contract pillar delivered GENERIC
  top-of-file functions (`progress_write`/`_setup_logging`) instead of the issue function
  `set_fields`. Root cause: a `LIMIT` applied BEFORE relevance-ranking cut the issue function
  (the 39th of 102 funcs in importer.py) in any large file — anchors were perfect, the fetch
  never included it. Fix: anchor-matched functions sort to the front of the fetch —
  L3b `_contract_pillar` (`3afba4d3`) + L1 `_top_function_names` (`14a5749c`). `set_fields` now
  surfaces in BOTH layers (proven on the real graph). Generalized (pure SQL, language-agnostic);
  regression tests pass.
- Secondary (logged): orientation hub-bias (`library.py::write` — did NOT misdirect; consensus
  overrode); cost telemetry `$0` (litellm unmapped `deepseek-v4-flash`); `sentence-transformers`
  absent in-container → semantic rank=0.
- Infra: live testing = **GitHub Codespaces only**. OH-runtime `buildx` needs docker data-root on
  `/tmp` (the 32G `/` fills to 99%) — set `/etc/docker/daemon.json` `data-root=/tmp/docker`.

### P0 stdlib-shadow laundering — CLOSED end-to-end, proven 5-language (commit `55ab30eb`)
The whole-architecture bucket (all ~28 layers, reconciled vs `DOC_OF_HONOR.md` + `we_did.md`,
saved `.tmp_gt_full_bucket_20260531T1905Z.md`) surfaced ONE high-harm bug: **`verified_unique`
launders a qualified stdlib call** (`os.walk` → uniquely-named project `account.walk` stamped
`CERTIFIED 0.95`). Today's 16:09 audit `6c4848c4` "CONFIRM open" was a **stale-binary artifact**
(`gt-index-t1t2.exe`, v15.1, pre-demote). On the CURRENT binary it is FIXED, two halves:
- **① resolver demote** (already committed `c7e7e5d0`): qualified-unresolved unique → `name_match`/
  `SPECULATIVE`/`name_match_qualified_unresolved`, not `verified_unique`. Parser DOES populate
  `CalleeQualified`, so `qualifiedUnresolved` fires.
- **② consumer suppression** (was the dirty `post_edit.py` hunk → committed `55ab30eb`): the
  categorical FACT filter dropped its `name_match AND candidate_count<=1` admit clause + now
  excludes name_match from the CERTIFIED/CANDIDATE clause. name_match is NEVER a deterministic fact.
- **Current binary REBUILT in codespace** (go1.26.1, gcc13.3, exit 0) — closes the ① stale-binary
  risk on the live path.

**Proof (execution, not audit) — 5 languages:**
- synthetic `shadowpkg_{go,rust,ts,js}` fixtures: all demote/no-launder (rust emits no edge).
- RED→GREEN: committed filter `admits=1 (LEAK)` → fix `admits=0 (clean)`, py/go/ts/js.
- 60 REAL holdout graphs (v15.2): 307K `name_match_qualified_unresolved` edges exist in real
  go/python/rust/ts; **100% of cc<=1 name_match are qualified_unresolved (OTHER=0)** → suppression
  strips ONLY stdlib-shadow, zero legitimate; **28–54% deterministic FACT callers retained** (not a
  nuke). Real-task delivery: **551 false callers avoided across go/ts/python, 0 laundered**.

### 5-lang generalization findings (this turn)
- **P0 fix generalizes cleanly** (the product question — answered at indexer + consumer level).
- **Contract pillar relevance is language-sensitive** (NOT a bug, correct-or-quiet): fires 3/3 python,
  1/2 ts, mixed rust, **0/0 on both go tasks** (generic `GetX` names don't match issue anchors/terms).
  Research item: cross-language relevance signal, not a harm.
- **Live-agent 5-lang is image-gated:** codespace has ONLY `beetbox_beets-5495` (python) image; full
  agent-level 5-lang needs 4 more Docker builds. Product-level 5-lang proof stands.
- Remaining bucket (low-harm, surfaced): ④ path_resolver not swept, ⑤ C1 guard-clip, ⑤ scaffold
  gate misses `.md`/`.openhands`, ⑤ cost telemetry `$0`, ④ `__GT_STRUCTURED__` leak (wrapper dispatch).

### WHOLE-GT-LAYER READINESS MATRIX across 5 languages (reframed from localization)
Exercised EVERY graph-driven layer on real holdout graphs (go=crossplane, rust=axum, ts=hono,
python=dagster/marimo) + synthetic js. `.tmp_layer_readiness_matrix.py`. Net: **all layers READY on
all 5 langs** after fixes. Two false alarms dismissed, one real gap fixed:
- L0/L0.4/L0.5(P0)/L0.6/L1-L3-delivery/L3b: **READY 5-lang** (delivery `laundered=0` everywhere).
- DISMISSED: python L0 "BUG" + L0.6 "EMPTY" = **stale marimo graph** (schema=None, assertions pass
  never ran); a CURRENT python graph (dagster-33645) has 32157 assertions / 18961 linked → python READY.
- DISMISSED: L3b contract `QUIET` on go = correct-or-quiet (generic `GetX` names don't match anchors).
- **FIXED (commit `e1bc266b`): L3 Consistency pillar was DEAD on Go.** Go receiver methods
  (`func (r *T) M()`) are top-level in the AST → walkNode labeled all 1890 crossplane receiver
  methods `Function`/parent_id=0, so siblings + self/type_flow silently no-op'd on a Tier-1 lang.
  `linkGoReceiverMethods()` parents them by receiver type (same-file). Verified codespace: toy fixture
  per-struct correct; real gt-index self-index 33/36 (91%) parented; `go test ./internal/parser` PASS;
  the 1 resolver failure (TestRoutePatternMatching, api_edges) is PRE-EXISTING (fails on base too).

### Live-agent 5-lang = INFRA-GATED (honest)
`railway/codespace_run.sh` clones SWE-bench-Live `--branch python-only`; `starryzhang` namespace has
only python task images (codespace has just `beets-5495`). A formal multi-lang **agent** eval needs
custom per-repo images or the **DeepSWE path** (`pier`+GTMiniSweAgent+gt_hook.py — the actual 5-lang
harness). Product-level 5-lang is PROVEN (indexer demote + consumer suppress + delivery + readiness
matrix on real graphs). Re-running beets live no-op'd (harness loaded prior finished output.jsonl).

### Commits this session: `55ab30eb` (P0 consumer), `b83c36c9` (record+5lang fixtures), `e1bc266b` (Go methods).
