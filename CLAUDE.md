# CLAUDE.md -- GroundTruth

> **Read this entire file before writing any code.**
> **Read GT_AIM.md for product thesis and strategic direction.**

---

## Product Thesis (see GT_AIM.md for full detail)

GT prevents **any error caused by incomplete codebase understanding** — not narrow categories.
- Tiered confidence: HIGH (1 target) → MEDIUM (3 options) → LOW (scope) → SILENT
- Delivery: conditional push (hooks, zero-turn-cost) + pull (MCP tools)
- Never hallucinates. Quality of determinism is the moat.
- Dynamic confidence (z-score per repo), NOT static thresholds.
- Solves the broad category so one model update can't kill us.

---

## What It Is

GroundTruth is an MCP server that gives AI coding agents deterministic codebase intelligence -- for any language. It indexes source code into a SQLite call graph, then provides evidence-based briefings, validation, and symbol tracing to prevent hallucinations.

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
       |       Shows ambiguous edges, detects installed LSP servers
       |
       +---> LSP resolver (v1.0.1 — edge verification)
       |       Reads call_sites table, sends textDocument/definition
       |       Writes lsp-verified edges (confidence 1.0)
       |
       +---> ego_graph.py (v1.0.1 — structural navigation)
               BFS from seed entities through verified edges
               Returns structural map for agent navigation
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
|   +-- lsp/                              # LSP client + edge resolver
|   |   +-- sync_client.py               # Synchronous LSP client (v1.0.1)
|   |   +-- servers.py                    # 30+ language server configs (v1.0.1)
|   |   +-- resolver.py                   # LSP edge resolution pipeline (v1.0.1)
|   +-- ego_graph.py                      # Ego-graph BFS + structural map (v1.0.1)
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
    resolution_method TEXT,    -- 'same_file', 'import', 'name_match', 'lsp' (v1.0.1)
    confidence REAL DEFAULT 0.0,  -- 0.0-1.0 (v14+)
    metadata TEXT
);
```

### call_sites Table (v1.0.1)

```sql
CREATE TABLE call_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_node_id INTEGER NOT NULL REFERENCES nodes(id),
    callee_name TEXT NOT NULL,
    line INTEGER NOT NULL,       -- 1-indexed from tree-sitter
    col INTEGER NOT NULL,        -- 0-indexed column from tree-sitter
    file_path TEXT NOT NULL,
    resolved BOOLEAN DEFAULT 0   -- set to 1 after LSP resolution
);
```

Written by Go indexer for all calls that fall to name-match (Stage 3).
Read by LSP resolver to send `textDocument/definition` at exact positions.

### Edge Confidence Model

| Resolution Method | Confidence | Meaning |
|---|---|---|
| same_file | 1.0 | Caller and callee in same file |
| import | 1.0 | Verified via import statement |
| lsp | 1.0 | Verified by LSP textDocument/definition (v1.0.1) |
| name_match (1 candidate) | 0.9 | Only one function with this name exists |
| name_match (2 candidates) | 0.6 | Two possible targets |
| name_match (3-5 candidates) | 0.4 | Several possible targets |
| name_match (5+ candidates) | 0.2 | Highly ambiguous (downgraded to 0.2 if LSP available) |

Ego-graph filters by resolution method, not confidence number. If LSP edges exist for a language,
only verified edges (same_file, import, lsp) are traversed.

---

## Go Indexer (gt-index)

### 5-Pass Architecture

1. **STRUCTURE** -- Walk filesystem, discover source files by language
2. **DEFINITIONS + IMPORTS** -- Parallel tree-sitter parse (NumCPU workers), batch SQLite insert
3. **CALLS** -- Resolve call references via 3-stage pipeline, compute confidence, deduplicate, write unresolved to call_sites table
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
