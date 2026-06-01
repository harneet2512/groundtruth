# CLAUDE.md -- GroundTruth

> **Read this entire file before writing any code.**

---

## What It Is

GroundTruth is an MCP server that gives AI coding agents codebase intelligence -- for any language. It indexes source code into a SQLite call graph, then provides evidence-based briefings, validation, and symbol tracing to prevent hallucinations.

**How it works:**
1. `gt-index` (Go binary) parses source code with tree-sitter, extracts functions/classes/calls/imports, writes `graph.db`
2. MCP server reads `graph.db` and exposes 16 tools (trace, hotspots, symbols, explain, etc.)
3. `gt_intel.py` (evidence engine) queries `graph.db` for SWE-bench evaluation

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
cd gt-index
CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/

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
