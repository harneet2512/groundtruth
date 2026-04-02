# GroundTruth

Codebase intelligence for AI coding agents. GroundTruth indexes your source code into a SQLite call graph, then provides evidence-based briefings, symbol tracing, and validation via MCP.

Works with Claude Code, Cursor, Codex, Windsurf, or any MCP client.

## Quick Start

```bash
# 1. Install
pip install -e .

# 2. Index your project (requires gt-index binary)
gt-index -root /path/to/repo -output .groundtruth/graph.db

# 3. Start the MCP server
groundtruth serve --root /path/to/repo
```

### Prerequisites

- Python 3.11+
- Go 1.22+ and GCC (to build gt-index from source)
- Or: download pre-built gt-index from [Releases](https://github.com/harneet2512/groundtruth/releases)

## MCP Configuration

**Claude Code** -- add to `.claude/mcp.json`:
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

**Cursor / Windsurf / any MCP client** -- same pattern, adjust config format for your IDE.

## MCP Tools (16)

| Tool | Purpose |
|------|---------|
| `groundtruth_find_relevant` | Find files relevant to a task description |
| `groundtruth_brief` | Proactive briefing before code generation |
| `groundtruth_validate` | Check proposed code against the index |
| `groundtruth_trace` | Trace symbol callers/callees (pure graph) |
| `groundtruth_status` | Health check + stats |
| `groundtruth_dead_code` | Exported symbols with zero references |
| `groundtruth_unused_packages` | Installed packages with no imports |
| `groundtruth_hotspots` | Most-referenced symbols (blast radius) |
| `groundtruth_orient` | Codebase structure overview |
| `groundtruth_checkpoint` | Session progress summary |
| `groundtruth_symbols` | List all symbols in a file |
| `groundtruth_context` | Symbol usage context with code snippets |
| `groundtruth_explain` | Deep dive: source, callers, callees |
| `groundtruth_impact` | Blast radius of modifying a symbol |
| `groundtruth_patterns` | Coding conventions in sibling files |
| `groundtruth_do` | Single entry point (auto-routes to best tool) |

All tools are deterministic ($0 cost). Optional AI enhancement available with `pip install groundtruth[ai]`.

## Language Support

**30 languages** via tree-sitter, in two tiers:

| Tier | Languages | Resolution Quality |
|------|-----------|-------------------|
| **Tier 1** (import-verified) | Python, Go, JavaScript, TypeScript, Java, Rust | High -- import paths verified |
| **Tier 2** (name-match) | C, C++, C#, Ruby, Kotlin, PHP, Swift, Scala, + 16 more | Medium -- matched by function name |

### Edge Confidence

Every edge in the call graph has a confidence score (0.0-1.0):
- **1.0** -- same-file or import-verified (certain)
- **0.9** -- unique name (only one function with this name exists)
- **0.2-0.6** -- ambiguous name match (multiple candidates)

Use `groundtruth resolve --db graph.db` to see ambiguous edges and which LSP servers could resolve them.

## Evidence Engine

For SWE-bench evaluation, `gt_intel.py` provides 7 evidence families:

```bash
# Post-edit reminder
python benchmarks/swebench/gt_intel.py --db=graph.db --file=src/core.py --function=process --reminder

# Output:
# <gt-evidence>
# [VERIFIED] CAUTION: 12 callers in 4 files (0.67)
# [WARNING] MUST return Optional[User] (0.33)
# </gt-evidence>
```

## Development

```bash
pip install -e ".[dev]"

# Tests (648 tests)
pytest tests/ -v

# Lint
ruff check src/ tests/
ruff format --check src/ tests/

# Build Go indexer (requires Go + GCC)
cd gt-index && CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/
```

## License

MIT
