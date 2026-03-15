# GroundTruth

MCP server that gives AI coding agents compiler-grade codebase intelligence — for any language, with zero language-specific code.

GroundTruth connects to the same Language Server Protocol (LSP) servers that power your editor and builds a complete, verified symbol index in SQLite. Agents call GroundTruth to find relevant files, get briefings before writing code, validate code after writing it, and trace symbol usage across the codebase.

## How It Works

```
Agent receives a task
       |
PHASE 1: Find Relevant Files
  groundtruth_find_relevant({ description: "..." })
  --> AI parses task into symbols (~200 tokens)
  --> SQLite graph traversal finds connected files (deterministic, free)
  --> Returns ranked file list with reasons
       |
Agent reads only the relevant files (5 instead of 30)
       |
PHASE 2: Proactive Briefing
  groundtruth_brief({ intent: "..." })
  --> FTS5 query finds matching symbols
  --> AI (Haiku) distills into compact briefing (~$0.003)
  --> Agent knows the patterns before writing a single line
       |
Agent generates code informed by briefing
       |
PHASE 3: Reactive Validation
  groundtruth_validate({ proposed_code: "...", file_path: "..." })
  --> Deterministic: check imports, packages, signatures (<10ms, $0)
  --> Levenshtein for close name matches ($0)
  --> Cross-index search for wrong file paths ($0)
  --> AI semantic resolution ONLY when all above fail (~15% of cases)
       |
PHASE 4: Impact Analysis
  groundtruth_trace({ symbol: "getUserById" })
  --> Pure SQLite: callers, callees, full dependency chain
  --> Zero AI. Zero tokens. <10ms.
```

## Quick Start

```bash
# Install from source
pip install -e .

# Or with dev dependencies
pip install -e ".[dev]"
```

### MCP Configuration

**Claude Code** — add to `.claude/mcp.json`:
```json
{
  "mcpServers": {
    "groundtruth": {
      "command": "groundtruth",
      "args": ["serve", "--root", "."]
    }
  }
}
```

**Cursor** — add to `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "groundtruth": {
      "command": "groundtruth",
      "args": ["serve", "--root", "."]
    }
  }
}
```

Works with any MCP client (Codex, Windsurf, etc.) using the same pattern.

## MCP Tools

### groundtruth_find_relevant

Find which files matter for a given task. Pure SQLite graph traversal after initial AI parse.

```json
// Input
{ "description": "fix getUserById returning null instead of throwing NotFoundError" }

// Output
{
  "files": [
    { "path": "src/users/queries.py", "relevance": "high", "reason": "defines getUserById", "distance": 0 },
    { "path": "src/utils/errors.py", "relevance": "high", "reason": "defines NotFoundError", "distance": 0 },
    { "path": "src/routes/users.py", "relevance": "medium", "reason": "imports from users/queries.py", "distance": 1 }
  ],
  "entry_symbols": ["getUserById", "NotFoundError"],
  "graph_depth": 2
}
```

### groundtruth_brief

Proactive briefing before code generation.

```json
// Input
{ "intent": "add JWT auth middleware to the user routes" }

// Output
{
  "briefing": "Auth is handled via middleware in src/middleware/auth.py (authMiddleware). JWT operations are in src/auth/jwt.py: signToken(payload) returns str. Use the middleware pattern, see src/routes/admin.py.",
  "relevant_symbols": [
    { "name": "authMiddleware", "file": "src/middleware/auth.py", "signature": "(request, next) -> Response" }
  ],
  "warnings": ["auth/ barrel export re-exports login/logout but NOT jwt functions"]
}
```

### groundtruth_validate

Post-generation validation against the symbol index.

```json
// Input
{ "proposed_code": "from auth import hashPassword\nimport axios", "file_path": "src/routes/users.py" }

// Output
{
  "valid": false,
  "errors": [
    {
      "type": "wrong_module_path",
      "message": "hashPassword not found in auth/",
      "suggestion": { "source": "deterministic", "fix": "from utils.crypto import hashPassword", "confidence": 0.95 }
    },
    {
      "type": "missing_package",
      "message": "axios is not installed",
      "suggestion": { "source": "deterministic", "fix": "import requests", "confidence": 0.8 }
    }
  ],
  "ai_used": false,
  "latency_ms": 8
}
```

### groundtruth_trace

Trace a symbol through the codebase. Zero AI, pure graph.

```json
// Input
{ "symbol": "getUserById", "direction": "both" }

// Output
{
  "symbol": { "name": "getUserById", "file": "src/users/queries.py", "signature": "(user_id: int) -> User" },
  "callers": [
    { "file": "src/routes/users.py", "line": 47, "context": "user = getUserById(user_id)" }
  ],
  "callees": [
    { "symbol": "db.query", "file": "src/db/client.py" }
  ],
  "dependency_chain": ["src/users/queries.py", "src/db/client.py"],
  "impact_radius": 5
}
```

### groundtruth_status

Health check and intervention statistics.

```json
{
  "indexed": true,
  "languages": ["python", "typescript"],
  "symbols_count": 847,
  "files_count": 123,
  "refs_count": 2341,
  "interventions": { "total": 42, "hallucinations_caught": 18, "ai_calls": 3, "tokens_used": 1240 }
}
```

### groundtruth_dead_code

Find exported symbols with zero references. Pure SQL, zero AI.

```json
{
  "dead_symbols": [
    { "name": "formatLegacyDate", "file": "src/utils/dates.py", "kind": "function" }
  ],
  "total": 1
}
```

### groundtruth_unused_packages

Find installed packages that no file imports.

```json
{
  "unused_packages": [
    { "name": "axios", "version": "1.6.0", "package_manager": "npm" }
  ],
  "total": 1
}
```

### groundtruth_hotspots

Most-referenced symbols in the codebase — the backbone.

```json
{
  "hotspots": [
    { "name": "getUserById", "file": "src/users/queries.py", "usage_count": 14, "kind": "function" }
  ]
}
```

## Benchmark Results

GTBench tests GroundTruth against 100 hallucination cases across 6 categories and 20 file relevance cases across 3 languages (TypeScript, Python, Go).

### Hallucination Detection

| Category | Cases | Detected | Fix OK | AI Needed |
|----------|-------|----------|--------|-----------|
| wrong-import-name/close-match | 15 | 100% | 40% | 60% |
| wrong-import-name/no-close-match | 10 | 100% | 10% | 80% |
| wrong-module-path/symbol-exists-elsewhere | 15 | 47% | 0% | 47% |
| wrong-module-path/module-doesnt-exist | 5 | 0% | 0% | 0% |
| missing-package | 15 | 0% | 0% | 0% |
| wrong-signature | 15 | 0% | 0% | 0% |
| invented-symbol | 15 | 33% | 0% | 33% |
| wrong-language-convention | 10 | 30% | 20% | 10% |

### File Relevance

| Metric | Value |
|--------|-------|
| Cases | 20 |
| Avg Recall | 100% |
| Avg Precision | 47% |

Run the benchmark:
```bash
python benchmarks/runner.py --fixture all
```

See [benchmarks/README.md](benchmarks/README.md) for methodology.

## How It's Different

| Capability | SymDex | Cursor/Claude Code | GroundTruth |
|---|---|---|---|
| Symbol lookup | tree-sitter | Partial (context window) | LSP (compiler-grade) |
| Call graph | Yes | No | Yes |
| Language-agnostic | 12 languages (grammars) | N/A | Any language with LSP (50+) |
| Proactive briefing | No | No | **Yes** |
| Task to relevant files | No | No | **Yes** |
| Code validation | No | No | **Yes** |
| Type-level accuracy | Syntax only | Partial | Full (LSP = the compiler) |
| Per-language code | tree-sitter queries | N/A | **None** (LSP is universal) |

## Adding a New Language

One line in `src/groundtruth/lsp/config.py`:

```python
LSP_SERVERS = {
    ".py": {"command": ["pyright-langserver", "--stdio"]},
    ".ts": {"command": ["typescript-language-server", "--stdio"]},
    ".go": {"command": ["gopls", "serve", "-stdio"]},
    ".rs": {"command": ["rust-analyzer"]},
    ".java": {"command": ["jdtls"]},
    # Add your language here:
    ".rb": {"command": ["solargraph", "stdio"]},
}
```

Zero parsing code. Zero adapter logic. The LSP server for that language does all the hard work.

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests (578 tests)
pytest tests/ -v

# Type check (strict mode)
mypy src/ --strict

# Lint + format
ruff check src/ tests/
ruff format src/ tests/

# Run benchmarks
python benchmarks/runner.py --fixture all
```

## License

MIT
