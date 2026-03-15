# GroundTruth

MCP server that gives AI coding agents codebase intelligence via Language Server Protocol (LSP).

GroundTruth connects to LSP servers (the same ones that power your editor) and builds a symbol index in SQLite. Agents call GroundTruth to find relevant files, get briefings before writing code, validate code after writing it, and trace symbol usage.

**Current language support:**
- **Python** — full AST-based validation (imports, signatures, packages) without LSP
- **TypeScript/JavaScript** — regex-based import and signature validation without LSP
- **Any language with an LSP server** — full validation when LSP is running (Go, Rust, Java, etc.)

## How It Works

```
Agent receives a task
       |
PHASE 1: Find Relevant Files
  groundtruth_find_relevant({ description: "..." })
  → AI parses task into symbols (~200 tokens)
  → SQLite graph traversal finds connected files (deterministic, free)
       |
PHASE 2: Proactive Briefing
  groundtruth_brief({ intent: "..." })
  → FTS5 query + AI distills into compact briefing (~$0.003)
       |
PHASE 3: Reactive Validation
  groundtruth_validate({ proposed_code: "...", file_path: "..." })
  → Deterministic: check imports, packages, signatures (<10ms, $0)
  → Levenshtein + cross-index for close matches ($0)
  → AI semantic resolution when all above fail (~15% of cases)
  → Returns grounding record with machine-checkable evidence
       |
PHASE 4: Impact Analysis
  groundtruth_trace({ symbol: "getUserById" })
  → Pure SQLite: callers, callees, dependency chain (<10ms)
```

## Quick Start

```bash
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

Works with any MCP client (Cursor, Codex, Windsurf) using the same pattern.

## MCP Tools

| Tool | Purpose | AI Cost |
|------|---------|---------|
| `groundtruth_find_relevant` | Find files relevant to a task | ~200 tokens (task parse) |
| `groundtruth_brief` | Proactive briefing before code generation | ~$0.003 |
| `groundtruth_validate` | Check proposed code against the index | $0 (deterministic) |
| `groundtruth_trace` | Trace symbol callers/callees | $0 (pure SQL) |
| `groundtruth_status` | Health check + stats | $0 |
| `groundtruth_dead_code` | Exported symbols with zero references | $0 |
| `groundtruth_unused_packages` | Installed packages with no imports | $0 |
| `groundtruth_hotspots` | Most-referenced symbols | $0 |

### groundtruth_validate (with grounding records)

Validation now returns a **grounding record** — structured evidence for why code was accepted or rejected:

```json
{
  "valid": false,
  "errors": [
    {
      "type": "wrong_module_path",
      "message": "hashPassword not found in auth/",
      "suggestion": { "fix": "from utils.crypto import hashPassword", "confidence": 0.95 }
    }
  ],
  "grounding_record": {
    "target_file": "src/routes/users.py",
    "evidence_count": 3,
    "verified_count": 2,
    "violated_count": 1,
    "confidence": 0.667,
    "violated_invariants": ["hashPassword not found in auth/"]
  }
}
```

## Benchmark Results (GTBench)

100 hallucination cases across 8 categories, 20 file relevance cases across 3 languages.

### Hallucination Detection

| Category | Cases | Detected | Fix OK | AI Needed |
|----------|-------|----------|--------|-----------|
| wrong-signature | 15 | **100%** | 100% | 0% |
| missing-package | 15 | **100%** | 0% | 0% |
| wrong-import-name/close-match | 15 | **100%** | 93% | 0% |
| wrong-import-name/no-close-match | 10 | **100%** | 30% | 10% |
| wrong-module-path/module-doesnt-exist | 5 | **100%** | 100% | 0% |
| wrong-module-path/symbol-exists-elsewhere | 15 | **100%** | 100% | 0% |
| invented-symbol | 15 | **100%** | 53% | 47% |
| wrong-language-convention | 10 | **100%** | 100% | 0% |
| **Overall** | **100** | **100%** | **70%** | **8%** |

**Notes:**
- All 3 languages (TypeScript, Python, Go) have full AST/regex validation
- "Fix OK" = deterministic suggestion contains the correct symbol/import
- "AI Needed" = error detected but requires semantic resolver for a fix

### File Relevance

| Metric | Value |
|--------|-------|
| Cases | 20 |
| Avg Recall | 100% |
| Avg Precision | 100% |

```bash
python benchmarks/runner.py --fixture all
```

## Adding a New Language

One line in `src/groundtruth/lsp/config.py`:

```python
LSP_SERVERS = {
    ".py": {"command": ["pyright-langserver", "--stdio"]},
    ".ts": {"command": ["typescript-language-server", "--stdio"]},
    ".go": {"command": ["gopls", "serve", "-stdio"]},
    ".rs": {"command": ["rust-analyzer"]},
}
```

For Python and TypeScript, AST/regex validation works without LSP. For other languages, LSP must be running for full validation.

## Development

```bash
pip install -e ".[dev]"

# Tests (588 tests)
pytest tests/ -v

# Type check
mypy src/ --strict

# Lint
ruff check src/ tests/

# Benchmarks
python benchmarks/runner.py --fixture all
```

## License

MIT
