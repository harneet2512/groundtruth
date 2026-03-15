# CLAUDE.md — GroundTruth

> **Read this entire file before writing any code.**

---

## What It Is

GroundTruth is an MCP server that gives AI coding agents compiler-grade codebase intelligence — for any language, with zero language-specific code.

It connects to the same Language Server Protocol (LSP) servers that power your editor (TypeScript's `tsserver`, Python's `pyright`, Go's `gopls`, Rust's `rust-analyzer`) and builds a complete, verified symbol index in SQLite. Agents call GroundTruth to find relevant files, get briefings before writing code, validate code after writing it, and trace symbol usage across the codebase.

**Why LSP:** The LSP protocol is identical for every language. `textDocument/documentSymbol` returns symbols. `textDocument/references` returns call sites. `textDocument/definition` resolves imports. Same request, same response format, whether the codebase is TypeScript, Python, Go, Rust, Java, or C++. We write the LSP client once. It works everywhere. No adapters. No grammars. No per-language parsing logic.

**Why this exists:** AI coding agents (Claude Code, Cursor, Codex, Windsurf) hallucinate because they operate on partial codebase context — limited by context windows. GroundTruth has the complete picture. It doesn't guess. It knows.

**How it's different from SymDex:** SymDex is a lookup tool — "where is X?" GroundTruth is an intelligence layer — "I'm about to do X, what should I know?" and "I just wrote X, is it correct?" SymDex indexes. GroundTruth prevents mistakes.

**Works with any MCP client:** Claude Code, Cursor, Codex, Windsurf, or any agent that speaks MCP.

---

## Architecture

```
Agent receives a task ("fix getUserById returning null instead of throwing NotFoundError")
       ↓
PHASE 1: Find Relevant Files
  groundtruth_find_relevant({ description: "..." })
  → AI parses task into symbols (~200 tokens)
  → SQLite graph traversal finds connected files (deterministic, free)
  → Returns ranked file list with reasons
       ↓
Agent reads only the relevant files (5 instead of 30)
       ↓
PHASE 2: Proactive Briefing
  groundtruth_brief({ intent: "fix error handling in getUserById" })
  → FTS5 query finds matching symbols
  → AI (Haiku) distills into compact briefing (~$0.003)
  → Agent knows the patterns before writing a single line
       ↓
Agent generates code informed by briefing
       ↓
PHASE 3: Reactive Validation
  groundtruth_validate({ proposed_code: "...", file_path: "..." })
  → Deterministic: check imports, packages, signatures against index ($0)
  → Levenshtein for close name matches ($0)
  → Cross-index search for wrong file paths ($0)
  → AI semantic resolution ONLY when all deterministic methods fail (~$0.003, ~15% of cases)
       ↓
PHASE 4 (optional): Impact Analysis
  groundtruth_trace({ symbol: "getUserById" })
  → Pure SQLite: callers, callees, full dependency chain
  → Zero AI. Zero tokens. <10ms.
```

No daemon. MCP uses stdio — client spawns the process.

---

## The LSP Engine

### How It Works

GroundTruth manages LSP server lifecycles. On project init:

1. Detect which languages are present (file extensions — the ONLY place we look at extensions)
2. Spawn the appropriate language server(s) via stdio (e.g., `pyright --stdio`, `typescript-language-server --stdio`, `gopls serve`)
3. Initialize the LSP session (`initialize` → `initialized`)
4. For each source file, request:
   - `textDocument/documentSymbol` → all symbols (functions, classes, variables, types)
   - `textDocument/references` → where each exported symbol is used
   - `textDocument/hover` → type signatures and documentation
5. Store everything in SQLite
6. Watch for file changes → re-index incrementally via `textDocument/didChange`

### Why This Is Language-Agnostic Without Hardcoding

The LSP protocol defines a universal schema for code intelligence:

```
DocumentSymbol {
  name: string           # "getUserById"
  kind: SymbolKind       # Function, Class, Variable, Interface...
  range: Range           # where it lives
  children: []           # nested symbols
}
```

This is IDENTICAL across languages. We don't need to know that Python uses `def`, Go uses `func`, Rust uses `fn`. The language server handles all of that. Our code sees the same `DocumentSymbol` regardless.

The only language-aware config is a small mapping:

```python
# This is NOT an adapter. It's a 3-line config per language.
LSP_SERVERS = {
    ".py": {"command": ["pyright-langserver", "--stdio"]},
    ".ts": {"command": ["typescript-language-server", "--stdio"]},
    ".tsx": {"command": ["typescript-language-server", "--stdio"]},
    ".js": {"command": ["typescript-language-server", "--stdio"]},
    ".go": {"command": ["gopls", "serve", "-stdio"]},
    ".rs": {"command": ["rust-analyzer"]},
    ".java": {"command": ["jdtls"]},
}
```

Adding a new language = one line of config. Zero parsing code. Zero adapter logic. The LSP server for that language does all the hard work.

### What We Get For Free From LSP

- **Type-resolved signatures** (not just AST-level syntax)
- **Cross-file references** (who calls what, from where)
- **Import resolution** (the compiler knows the real paths)
- **Diagnostics** (real compiler errors, not heuristic guesses)
- **Hover information** (docs, types, deprecation notices)
- **Go-to-definition** (resolve any symbol to its source)

Tree-sitter gives you syntax. LSP gives you semantics. We chose semantics.

---

## Repository Structure

```
groundtruth/
├── CLAUDE.md
├── PRD.md
├── PROGRESS.md
├── README.md
├── pyproject.toml
├── .gitignore
├── src/
│   └── groundtruth/
│       ├── __init__.py
│       ├── main.py                      # Entry point
│       ├── mcp/
│       │   ├── __init__.py
│       │   ├── server.py                # MCP server (stdio transport)
│       │   └── tools.py                 # Tool definitions + handlers
│       ├── lsp/
│       │   ├── __init__.py
│       │   ├── client.py                # Universal LSP client (JSON-RPC over stdio)
│       │   ├── manager.py               # Spawns/manages LSP server processes
│       │   ├── protocol.py              # LSP message types (DocumentSymbol, etc.)
│       │   └── config.py                # LSP_SERVERS mapping (the only language-aware file)
│       ├── index/
│       │   ├── __init__.py
│       │   ├── indexer.py               # Orchestrates LSP queries → SQLite
│       │   ├── store.py                 # SQLite operations (CRUD, FTS5, graph queries)
│       │   ├── schema.sql               # Table definitions
│       │   └── graph.py                 # Import graph traversal (BFS/DFS)
│       ├── validators/
│       │   ├── __init__.py
│       │   ├── import_validator.py
│       │   ├── package_validator.py
│       │   ├── signature_validator.py
│       │   └── orchestrator.py          # Runs all validators, merges results
│       ├── ai/
│       │   ├── __init__.py
│       │   ├── briefing.py              # Intent → FTS5 → Haiku → briefing
│       │   ├── semantic_resolver.py     # AI fallback when deterministic fails
│       │   ├── task_parser.py           # Natural language → symbol names
│       │   └── prompts.py               # All prompt templates
│       ├── stats/
│       │   ├── __init__.py
│       │   ├── tracker.py               # Logs every intervention to SQLite
│       │   └── reporter.py              # Generates stats summaries
│       ├── analysis/                     # Research layers
│       │   ├── __init__.py
│       │   ├── grounding_gap.py         # Layer 1: Measure briefing → output compliance
│       │   ├── risk_scorer.py           # Layer 2: Predict hallucination-prone code areas
│       │   └── adaptive_briefing.py     # Layer 3: Tailor context based on risk + history
│       ├── viz/                          # Visualization
│       │   ├── __init__.py
│       │   ├── generate_graph_data.py   # SQLite → JSON for 3D graph
│       │   └── risk_map.html            # Three.js 3D hallucination risk map
│       ├── cli/
│       │   ├── __init__.py
│       │   └── commands.py              # setup, status, stats, index, validate
│       └── utils/
│           ├── __init__.py
│           ├── logger.py                # Structured logging
│           ├── levenshtein.py           # String distance
│           ├── cache.py                 # LRU caching for LSP responses
│           └── watcher.py              # File change detection → incremental re-index
├── tests/
│   ├── conftest.py                      # Shared fixtures, in-memory SQLite
│   ├── unit/
│   │   ├── test_lsp_client.py
│   │   ├── test_indexer.py
│   │   ├── test_store.py
│   │   ├── test_graph.py
│   │   ├── test_validators.py
│   │   ├── test_briefing.py
│   │   ├── test_semantic_resolver.py
│   │   ├── test_task_parser.py
│   │   ├── test_grounding_gap.py
│   │   ├── test_risk_scorer.py
│   │   └── test_adaptive_briefing.py
│   ├── integration/
│   │   ├── test_mcp_server.py
│   │   ├── test_briefing_flow.py
│   │   ├── test_validation_flow.py
│   │   └── test_find_relevant_flow.py
│   └── fixtures/
│       ├── project_ts/                  # TypeScript test project
│       ├── project_py/                  # Python test project
│       └── project_go/                  # Go test project
├── benchmarks/
│   ├── README.md
│   ├── runner.py
│   ├── hallucination_cases/
│   ├── swe_bench/
│   │   ├── harness.py
│   │   ├── results/
│   │   └── README.md
│   └── results/
└── docs/
    ├── architecture.md
    └── research.md
```

---

## PROGRESS.md Convention

Multiple LLMs work on this project (Claude Code AND Cursor). After every milestone, update PROGRESS.md:

```markdown
# GroundTruth — Progress

## Last Updated
[date/time]

## Current Phase
[phase]

## Completed
- [x] item

## In Progress
- Working on: [file]
- Blockers: [issues]

## Next Up
- [task]

## Decisions Made
- [deviations from CLAUDE.md and why]
```

Not optional. Update after every significant piece of work.

---

## SQLite Schema

```sql
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- maps from LSP SymbolKind: 'function' | 'class' | 'variable' | 'method' | 'property' | 'interface' | 'enum' | 'type'
    language TEXT NOT NULL,               -- 'python' | 'typescript' | 'go' | 'rust' | etc.
    file_path TEXT NOT NULL,
    line_number INTEGER,
    end_line INTEGER,
    is_exported BOOLEAN DEFAULT FALSE,
    signature TEXT,                        -- from LSP hover
    params TEXT,                           -- JSON: [{ name, type, optional }] — parsed from signature
    return_type TEXT,
    documentation TEXT,                    -- from LSP hover (docstrings, JSDoc, godoc)
    usage_count INTEGER DEFAULT 0,
    last_indexed_at INTEGER NOT NULL
);

CREATE TABLE exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    module_path TEXT NOT NULL,             -- normalized import path
    is_default BOOLEAN DEFAULT FALSE,
    is_named BOOLEAN DEFAULT TRUE
);

CREATE TABLE packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT,
    package_manager TEXT NOT NULL,         -- 'npm' | 'pip' | 'go' | 'cargo'
    is_dev_dependency BOOLEAN DEFAULT FALSE,
    UNIQUE(name, package_manager)
);

CREATE TABLE refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    referenced_in_file TEXT NOT NULL,
    referenced_at_line INTEGER,
    reference_type TEXT NOT NULL           -- 'import' | 'call' | 'type_usage'
);

CREATE TABLE interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    tool TEXT NOT NULL,
    file_path TEXT,
    language TEXT,
    phase TEXT NOT NULL,                   -- 'find_relevant' | 'brief' | 'validate' | 'trace'
    outcome TEXT NOT NULL,                 -- 'valid' | 'fixed_deterministic' | 'fixed_ai' | 'unfixable'
    errors_found INTEGER DEFAULT 0,
    errors_fixed INTEGER DEFAULT 0,
    error_types TEXT,                      -- JSON array
    ai_called BOOLEAN DEFAULT FALSE,
    ai_model TEXT,                         -- 'haiku' | 'sonnet' | null
    latency_ms INTEGER,
    tokens_used INTEGER DEFAULT 0,
    fix_accepted BOOLEAN
);

-- Indexes
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_name_exported ON symbols(name) WHERE is_exported = TRUE;
CREATE INDEX idx_symbols_file ON symbols(file_path);
CREATE INDEX idx_symbols_language ON symbols(language);
CREATE INDEX idx_symbols_usage ON symbols(usage_count DESC);
CREATE INDEX idx_exports_module ON exports(module_path);
CREATE INDEX idx_packages_name ON packages(name);
CREATE INDEX idx_refs_symbol ON refs(symbol_id);
CREATE INDEX idx_refs_file ON refs(referenced_in_file);
CREATE INDEX idx_interventions_timestamp ON interventions(timestamp);

-- Full-text search
CREATE VIRTUAL TABLE symbols_fts USING fts5(name, file_path, signature, documentation);

-- Evidence capture: full interaction lifecycle for proving value
CREATE TABLE validation_exhibits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    session_id TEXT NOT NULL,              -- groups interactions within one agent session
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,

    -- Phase 1: What the agent tried to write
    proposed_code TEXT NOT NULL,           -- the hallucinated code
    errors_detected TEXT NOT NULL,         -- JSON: errors from validate()
    suggestions_returned TEXT NOT NULL,    -- JSON: our fix suggestions

    -- Phase 2: What the agent wrote after our correction
    corrected_code TEXT,                   -- what the agent submitted next (null if no follow-up)
    agent_accepted_fix BOOLEAN,            -- did the corrected code use our suggestion?
    correction_latency_ms INTEGER,         -- time between our response and agent's next submission

    -- Phase 3: Did the correction actually work?
    corrected_code_valid BOOLEAN,          -- did corrected code pass validation?
    lsp_diagnostics_clean BOOLEAN,         -- did LSP report zero errors on the corrected file?
    compile_success BOOLEAN,               -- did the project compile after the correction?

    -- Classification
    hallucination_type TEXT NOT NULL,      -- wrong_module_path, symbol_not_found, missing_package, etc.
    fix_source TEXT NOT NULL,              -- levenshtein, cross_index, ai_semantic
    severity TEXT NOT NULL,                -- would_not_compile, wrong_behavior, style_only

    -- Link to intervention
    intervention_id INTEGER REFERENCES interventions(id)
);

CREATE INDEX idx_exhibits_session ON validation_exhibits(session_id);
CREATE INDEX idx_exhibits_timestamp ON validation_exhibits(timestamp);
CREATE INDEX idx_exhibits_type ON validation_exhibits(hallucination_type);
```

---

## MCP Tools

### groundtruth_find_relevant

```
The SWE-bench tool. Given a task, find which files matter.
```

**Input:**
```json
{
  "description": "fix getUserById returning null instead of throwing NotFoundError",
  "entry_points": ["src/users/queries.py"],  // optional
  "max_files": 10                             // optional, default 10
}
```

**How it works:**
1. AI (Haiku) parses task → extracts symbol names + concepts (~200 tokens)
2. Look up symbols in index → find defining files (entry points)
3. BFS over import graph: who imports these files? what do they import?
4. Score by distance from entry + number of relevant symbols
5. Return ranked list

Steps 2-5 are pure SQLite. Zero additional AI cost.

**Response:**
```json
{
  "files": [
    {
      "path": "src/users/queries.py",
      "relevance": "high",
      "reason": "defines getUserById",
      "symbols_involved": ["getUserById"],
      "distance": 0
    },
    {
      "path": "src/utils/errors.py",
      "relevance": "high",
      "reason": "defines NotFoundError — referenced in task description",
      "symbols_involved": ["NotFoundError"],
      "distance": 0
    },
    {
      "path": "src/routes/users.py",
      "relevance": "medium",
      "reason": "imports from users/queries.py",
      "symbols_involved": ["getUserById"],
      "distance": 1
    }
  ],
  "entry_symbols": ["getUserById", "NotFoundError"],
  "graph_depth": 2
}
```

### groundtruth_brief

```
Proactive briefing before code generation. Tell me what I need to know.
```

**Input:**
```json
{
  "intent": "add JWT auth middleware to the user routes",
  "target_file": "src/routes/users.py"  // optional
}
```

**How it works:**
1. Extract keywords from intent (deterministic: split on word boundaries)
2. FTS5 query against symbols_fts → find matching symbols
3. Enrich with file paths, signatures, documentation from SQLite
4. Send to Haiku: "Given these symbols, what should the developer know?"
5. Return compact briefing (~150 tokens)

**Response:**
```json
{
  "briefing": "Auth is handled via middleware in src/middleware/auth.py (authMiddleware). JWT operations are in src/auth/jwt.py: signToken(payload) returns str, decodeToken(token) returns TokenPayload. Don't import auth functions directly in routes — use the middleware pattern. See src/routes/admin.py for the existing pattern.",
  "relevant_symbols": [
    {"name": "authMiddleware", "file": "src/middleware/auth.py", "signature": "(request, next) -> Response"},
    {"name": "signToken", "file": "src/auth/jwt.py", "signature": "(payload: dict) -> str"}
  ],
  "warnings": ["auth/ barrel export re-exports login/logout but NOT jwt functions — import jwt directly"]
}
```

### groundtruth_validate

```
Post-generation validation. Check if code is correct against the index.
```

**Input:**
```json
{
  "proposed_code": "from auth import hashPassword\nimport axios\n...",
  "file_path": "src/routes/users.py",
  "language": "python"  // optional, inferred from file extension
}
```

**How it works:**
1. Parse imports from proposed_code (regex-based, lightweight — NOT a full parser)
2. Check each import against SQLite: does the symbol exist? is it exported from that path?
3. Check package imports against packages table
4. For function calls: check signatures (param count) where possible
5. If check fails → Levenshtein for close name matches
6. If Levenshtein fails → cross-index search (right symbol, wrong file?)
7. If cross-index fails → AI semantic resolver (Haiku): "given the code context, what did the developer mean?"

**Response:**
```json
{
  "valid": false,
  "errors": [
    {
      "type": "wrong_module_path",
      "message": "hashPassword not found in auth/",
      "suggestion": {
        "source": "deterministic",
        "fix": "from utils.crypto import hashPassword",
        "confidence": 0.95,
        "reason": "hashPassword exists in utils/crypto.py, not auth/"
      }
    },
    {
      "type": "missing_package",
      "message": "axios is not installed",
      "suggestion": {
        "source": "deterministic",
        "fix": "import requests  # installed, or use urllib3",
        "confidence": 0.8,
        "reason": "requests is in requirements.txt; axios is a Node.js package"
      }
    }
  ],
  "ai_used": false,
  "latency_ms": 8
}
```

### groundtruth_trace

```
Trace a symbol through the codebase. Zero AI. Pure graph.
```

**Input:**
```json
{
  "symbol": "getUserById",
  "direction": "both",  // 'callers' | 'callees' | 'both'
  "max_depth": 3
}
```

**Response:**
```json
{
  "symbol": {"name": "getUserById", "file": "src/users/queries.py", "signature": "(user_id: int) -> User"},
  "callers": [
    {"file": "src/routes/users.py", "line": 47, "context": "user = getUserById(user_id)"},
    {"file": "src/services/user_service.py", "line": 23, "context": "return getUserById(id)"},
    {"file": "tests/test_users.py", "line": 8, "context": "result = getUserById(1)"}
  ],
  "callees": [
    {"symbol": "db.query", "file": "src/db/client.py"},
    {"symbol": "User", "file": "src/users/types.py"}
  ],
  "dependency_chain": ["src/users/queries.py", "src/db/client.py", "src/users/types.py"],
  "impact_radius": 5
}
```

**100% deterministic. Zero AI. Zero tokens. <10ms.**

### groundtruth_status

```
Health check + stats.
```

**Response:**
```json
{
  "indexed": true,
  "languages": ["python", "typescript"],
  "lsp_servers": {"python": "running", "typescript": "running"},
  "symbols_count": 847,
  "files_count": 123,
  "refs_count": 2341,
  "last_indexed": "2026-03-10T14:30:00Z",
  "interventions": {
    "total": 42,
    "hallucinations_caught": 18,
    "ai_calls": 3,
    "tokens_used": 1240
  }
}
```

### groundtruth_dead_code

```
Find exported symbols with zero references. Pure SQL. Zero AI.
```

**Response:**
```json
{
  "dead_symbols": [
    {"name": "formatLegacyDate", "file": "src/utils/dates.py", "kind": "function", "last_indexed": "2026-03-10"},
    {"name": "DeprecatedLogger", "file": "src/utils/logging.py", "kind": "class", "last_indexed": "2026-03-10"}
  ],
  "total": 2,
  "note": "These exported symbols have zero references anywhere in the codebase."
}
```

### groundtruth_unused_packages

```
Find installed packages that no file imports. Pure SQL. Zero AI.
```

**Response:**
```json
{
  "unused_packages": [
    {"name": "axios", "version": "1.6.0", "package_manager": "npm"},
    {"name": "colorama", "version": "0.4.6", "package_manager": "pip"}
  ],
  "total": 2
}
```

### groundtruth_hotspots

```
Most referenced symbols in the codebase — the backbone. Pure SQL. Zero AI.
```

**Response:**
```json
{
  "hotspots": [
    {"name": "getUserById", "file": "src/users/queries.py", "usage_count": 14, "kind": "function"},
    {"name": "AppError", "file": "src/utils/errors.py", "usage_count": 11, "kind": "class"},
    {"name": "db", "file": "src/db/client.py", "usage_count": 9, "kind": "variable"}
  ],
  "note": "High-usage symbols have the biggest blast radius if hallucinated."
}
```

---

## AI Layer

### Task Parser (`src/groundtruth/ai/task_parser.py`)

Parses a natural language task description into likely symbol names.

- Input: "fix getUserById returning null instead of throwing NotFoundError"
- Output: `["getUserById", "NotFoundError"]`
- One Haiku call, ~200 tokens in, ~50 tokens out
- **Fallback without API key:** split on camelCase/snake_case boundaries, filter stop words

### Briefing Engine (`src/groundtruth/ai/briefing.py`)

Intent → FTS5 → enrich with signatures/docs → Haiku distills → briefing.

Haiku receives: the matching symbols, their signatures, their file paths, their documentation. Haiku returns: a compact briefing (<200 tokens) telling the agent what it needs to know.

### Semantic Resolver (`src/groundtruth/ai/semantic_resolver.py`)

Fires ONLY when:
1. Import validation fails (symbol not found at specified path), AND
2. Levenshtein has no close match (edit distance > 3), AND
3. Cross-index search found nothing (symbol name doesn't exist anywhere)

When it fires: sends the error + surrounding code context + full list of potentially related symbols from the index. Haiku reasons about what the developer intended.

This fires for ~15% of validation errors. The other 85% are handled deterministically at zero cost.

### Prompts (`src/groundtruth/ai/prompts.py`)

All prompt templates live here. Centralized, testable, versionable.

---

## Import Graph (`src/groundtruth/index/graph.py`)

Pure deterministic. No AI.

```python
class ImportGraph:
    def __init__(self, store: SymbolStore):
        self.store = store

    def find_connected_files(self, entry_files: list[str], max_depth: int = 3) -> list[FileNode]:
        """BFS from entry files over import relationships."""

    def find_callers(self, symbol_name: str) -> list[Reference]:
        """All files/lines that reference this symbol."""

    def find_callees(self, symbol_name: str, file_path: str) -> list[Reference]:
        """All symbols referenced by this function."""

    def get_impact_radius(self, symbol_name: str) -> ImpactResult:
        """If this symbol changes, how many files break?"""
```

All implemented as recursive SQLite queries on the `refs` + `exports` tables.

---

## How GroundTruth Is Different

| Capability | SymDex | Cursor/Claude Code | GroundTruth |
|---|---|---|---|
| Symbol lookup | Yes (tree-sitter) | Partial (context window) | Yes (LSP — compiler-grade) |
| Call graph | Yes | No | Yes |
| Language-agnostic | 12 languages (tree-sitter grammars) | N/A | Any language with an LSP server (50+) |
| Proactive briefing | **No** | **No** | **Yes** |
| Task → relevant files | **No** | **No** | **Yes** |
| Code validation | **No** | **No** | **Yes** |
| Semantic resolution | Embedding similarity | Regenerate and hope | AI reasoning over complete index |
| Type-level accuracy | Syntax only (tree-sitter) | Partial | Full (LSP = the compiler) |
| Hardcoded per-language logic | Yes (tree-sitter queries) | N/A | **No** — LSP protocol is universal |

The key differences:
1. **LSP > tree-sitter for accuracy.** Tree-sitter parses syntax. LSP resolves semantics. GroundTruth knows the actual types, not just the names.
2. **Proactive > reactive.** Nobody else briefs the agent before generation. GroundTruth does.
3. **Validation is novel.** SymDex finds symbols. GroundTruth checks if your code is wrong and tells you exactly what's right.
4. **Zero language-specific code.** SymDex has 12 tree-sitter grammars with language-specific extraction logic. GroundTruth has a 10-line config mapping extensions to LSP commands.

---

## Coding Standards

- Python 3.11+. Type hints everywhere. `mypy --strict` must pass.
- Pydantic for all data models and external input validation.
- Result pattern: return `Result[T, Error]` (use a simple dataclass), never raise for expected failures.
- `structlog` for structured logging.
- `pytest` + `pytest-asyncio`. In-memory SQLite for unit tests. Mocked LLM responses.
- All SQLite queries use parameterized statements (no f-strings).
- `asyncio` for LSP communication (JSON-RPC is async).
- `ruff` for linting + formatting.
- Update PROGRESS.md after every milestone.

---

## pyproject.toml

```toml
[project]
name = "groundtruth"
version = "0.1.0"
description = "MCP server — compiler-grade codebase intelligence for AI coding agents"
requires-python = ">=3.11"
dependencies = [
    "anthropic>=0.30.0",
    "mcp>=1.0.0",
    "pydantic>=2.0.0",
    "structlog>=24.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "mypy>=1.10.0",
    "ruff>=0.4.0",
    "coverage>=7.0.0",
]

[project.scripts]
groundtruth = "groundtruth.main:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.mypy]
strict = true
python_version = "3.11"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Note:** We use the standard library `sqlite3` module — no external SQLite dependency needed. For LSP communication, we use `asyncio.subprocess` to spawn language servers and communicate over stdin/stdout with JSON-RPC. No heavy LSP client library required.

---

## Research Layers

Three layers that turn GroundTruth from a tool into a platform that produces findings about AI agent behavior. These are built AFTER the core tool works.

### Layer 1: Grounding Gap Measurement (`src/groundtruth/analysis/grounding_gap.py`)

**The question:** When you give an AI agent correct codebase context, how often does it actually use it?

Every time a briefing is followed by a validation, we can compare:

```python
@dataclass
class GroundingResult:
    briefing_symbols: list[str]       # symbols we told the agent about
    output_symbols: list[str]         # symbols the agent actually used
    correct_usages: int               # agent used a briefed symbol correctly
    ignored_symbols: int              # briefed but agent didn't use
    hallucinated_despite_briefing: int # agent hallucinated something the briefing covered
    compliance_rate: float            # correct_usages / len(briefing_symbols)
```

**Additional schema (add to schema.sql):**

```sql
CREATE TABLE briefing_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    intent TEXT NOT NULL,
    briefing_text TEXT NOT NULL,
    briefing_symbols TEXT NOT NULL,       -- JSON array of symbol names in briefing
    target_file TEXT,
    subsequent_validation_id INTEGER REFERENCES interventions(id),
    compliance_rate REAL,                 -- 0.0 to 1.0
    symbols_used_correctly TEXT,          -- JSON array
    symbols_ignored TEXT,                 -- JSON array
    hallucinated_despite_briefing TEXT    -- JSON array
);
```

The key metric: **compliance_rate across hundreds of interactions.** If this is 0.72, agents ignore 28% of correct context. That's a finding nobody has published.

### Layer 2: Hallucination Risk Scoring (`src/groundtruth/analysis/risk_scorer.py`)

**The question:** Can we predict which parts of a codebase will cause hallucinations, before any code is generated?

Computed from the symbol index — no AI needed:

```python
@dataclass
class RiskScore:
    file_path: str
    overall_risk: float               # 0.0 (safe) to 1.0 (hallucination magnet)
    factors: dict[str, float]         # breakdown by factor

class RiskScorer:
    def score_file(self, file_path: str) -> RiskScore
    def score_symbol(self, symbol_name: str) -> RiskScore
    def score_codebase(self) -> list[RiskScore]  # ranked by risk
```

**Risk factors (all computed from SQLite):**

| Factor | Computed from | Why it causes hallucinations |
|---|---|---|
| `naming_ambiguity` | Symbols within Levenshtein distance ≤ 3 | AI confuses similar names |
| `import_depth` | Max re-export chain length | AI guesses wrong path |
| `convention_variance` | camelCase/snake_case mix in same dir | AI uses wrong convention |
| `overloaded_paths` | Similar module names (auth/, middleware/auth) | AI imports from wrong module |
| `parameter_complexity` | Param count + optional params | AI gets signatures wrong |
| `isolation_score` | Symbols with zero/few references | AI invents calls to unused code |

**The novel finding:** Correlate risk scores with actual hallucination rates from Layer 1. "Files with `naming_ambiguity > 0.7` have 3x the hallucination rate."

### Layer 3: Adaptive Briefing (`src/groundtruth/analysis/adaptive_briefing.py`)

**The question:** Can we tailor context delivery based on what actually reduces hallucinations?

Modifies the briefing engine's behavior based on accumulated data:

```python
class AdaptiveBriefing:
    def enhance_briefing(self, base_briefing: BriefingResult, target_file: str) -> BriefingResult:
        risk = self.risk_scorer.score_file(target_file)
        
        if risk.factors["naming_ambiguity"] > 0.5:
            base_briefing.include_full_import_paths = True
        
        if risk.factors["import_depth"] > 2:
            base_briefing.include_reexport_chain = True
        
        past_failures = self.get_past_failures(target_file)
        if past_failures:
            base_briefing.include_negative_examples = True
        
        return base_briefing
```

**The feedback loop:** Briefing → Agent writes code → Validation → Grounding gap measured → Risk scores updated → Next briefing adapts. Over time, GroundTruth gets better at preventing hallucinations in areas it's seen fail before.

---

## What NOT to Build

- No tree-sitter (LSP is strictly better for our use case)
- No per-language adapters or parsers
- No vector embeddings (FTS5 + AI is simpler and more accurate for our queries)
- No VS Code extension
- No web dashboard
- No multi-repo support
- No streaming validation
- No daemon process (MCP stdio)
- No custom LSP server (we're a CLIENT, not a server)
