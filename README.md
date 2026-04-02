# GroundTruth

**Codebase intelligence for AI coding agents.** GroundTruth indexes your source code into a SQLite call graph with edge confidence scoring, then provides evidence-based briefings, symbol tracing, and validation via MCP.

Works with **Claude Code, Cursor, Codex, Windsurf**, or any MCP client.

![GroundTruth 3D Code City](groundtruth_hero.png)

*3D Code City visualization of home-assistant-core (147K symbols, 127K references)*

---

## Quick Start

```bash
# 1. Install the Python package
pip install -e .

# 2. Index your project
#    Option A: Go indexer (30 languages, parallel, fast)
gt-index -root /path/to/repo -output .groundtruth/graph.db

#    Option B: Python indexer (LSP-based, Python/TS/Go)
groundtruth index /path/to/repo

# 3. Start the MCP server
groundtruth serve --root /path/to/repo
```

### Prerequisites

- **Python 3.11+**
- **Go 1.22+ and GCC** (to build gt-index from source)
- Or: download pre-built gt-index from [Releases](https://github.com/harneet2512/groundtruth/releases)

---

## Performance

| Repository | Files | Index Time | Nodes | Edges | Speedup vs v0.x |
|-----------|-------|------------|-------|-------|-----------------|
| click | 105 | **334ms** | 1,067 | 2,066 | 11x |
| terraform | 3,241 | **7.5s** | 18,247 | 38,963 | 6x |
| cpython | 3,392 | **27s** | 93,516 | 194,872 | -- |
| kubernetes | 18,456 | **1.5 min** | 77,526 | 224,197 | 52x |
| sentry | 16,798 | **56s** | 45,847 | 73,289 | 145x |

---

## How It Works

```
Source code (30 languages)
       |
       v
gt-index (Go binary, tree-sitter, parallel parsing)
  - 6 languages with import-verified resolution (Python, Go, JS, TS, Java, Rust)
  - 24 languages with name-match fallback
  - Edge confidence: 1.0 (verified) to 0.2 (ambiguous)
  - Edge deduplication (25-40% fewer false edges)
       |
       v
graph.db (SQLite)
       |
       +---> MCP server (16 deterministic tools, $0 cost)
       |
       +---> gt_intel.py (7 evidence families, SWE-bench evaluation)
       |
       +---> groundtruth resolve (LSP precision pass)
       |
       +---> 3D Code City visualization
```

---

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

**Cursor / Windsurf / any MCP client** -- same pattern, adjust config format.

---

## MCP Tools (16)

| Tool | Purpose | Cost |
|------|---------|------|
| `groundtruth_find_relevant` | Find files relevant to a task description | $0 |
| `groundtruth_brief` | Proactive briefing before code generation | $0 |
| `groundtruth_validate` | Check proposed code against the index | $0 |
| `groundtruth_trace` | Trace symbol callers/callees (pure graph) | $0 |
| `groundtruth_status` | Health check + stats | $0 |
| `groundtruth_dead_code` | Exported symbols with zero references | $0 |
| `groundtruth_unused_packages` | Installed packages with no imports | $0 |
| `groundtruth_hotspots` | Most-referenced symbols (blast radius) | $0 |
| `groundtruth_orient` | Codebase structure overview | $0 |
| `groundtruth_checkpoint` | Session progress summary | $0 |
| `groundtruth_symbols` | List all symbols in a file | $0 |
| `groundtruth_context` | Symbol usage context with code snippets | $0 |
| `groundtruth_explain` | Deep dive: source, callers, callees | $0 |
| `groundtruth_impact` | Blast radius of modifying a symbol | $0 |
| `groundtruth_patterns` | Coding conventions in sibling files | $0 |
| `groundtruth_do` | Single entry point (auto-routes to best tool) | $0 |

All tools are fully deterministic. Optional AI enhancement: `pip install groundtruth[ai]`

---

## Edge Confidence

Every edge in the call graph has a confidence score (0.0-1.0):

| Confidence | Meaning | Evidence Tier |
|-----------|---------|--------------|
| **1.0** | Same-file or import-verified | `[VERIFIED]` |
| **0.9** | Unique name (only one function with this name) | `[VERIFIED]` |
| **0.6** | Two possible targets | `[WARNING]` |
| **0.4** | 3-5 possible targets | `[INFO]` |
| **0.2** | Highly ambiguous (5+ candidates) | Filtered out |

Use `groundtruth resolve --db graph.db` to see ambiguous edges and which LSP servers can resolve them:

```bash
# Diagnostic mode (show what's ambiguous)
groundtruth resolve --db graph.db

# Live resolution via LSP (upgrade ambiguous edges to verified)
groundtruth resolve --db graph.db --resolve --lang python
```

---

## Language Support

**30 languages** via tree-sitter:

| Tier | Languages | Resolution |
|------|-----------|-----------|
| **Tier 1** (import-verified) | Python, Go, JavaScript, TypeScript, Java, Rust | High fidelity |
| **Tier 2** (name-match) | C, C++, C#, Ruby, Kotlin, PHP, Swift, Scala, Bash, Lua, Elixir, OCaml, Groovy, Elm, HCL, HTML, CSS, TOML, YAML, Markdown, Protobuf, SQL, Svelte, Cue | Name-match with confidence scoring |

---

## Evidence Engine

For SWE-bench evaluation, `gt_intel.py` provides 7 evidence families:

```bash
# Post-edit reminder
python benchmarks/swebench/gt_intel.py \
    --db=graph.db --file=src/core.py --function=process --reminder

# Output:
# <gt-evidence>
# [VERIFIED] CAUTION: 12 callers in 4 files (0.67)
# [WARNING] MUST return Optional[User] (0.33)
# </gt-evidence>
```

| Family | What It Checks |
|--------|---------------|
| IMPORT | Correct import paths for cross-file callees |
| CALLER | How callers use the return value (destructure, check, assert) |
| SIBLING | Behavioral norms from same-class methods |
| TEST | Test functions with assertions referencing the target |
| IMPACT | Blast radius (caller count + critical path detection) |
| TYPE | Return type contracts |
| PRECEDENT | Last git commit touching this function |

---

## 3D Code City Visualization

```bash
# Generate static risk map
groundtruth viz --root /path/to/repo

# Start interactive 3D server
groundtruth city --root /path/to/repo
```

Buildings represent code modules. Height = complexity. Color = risk level. Dependency lines connect related files. Click buildings to inspect symbols.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GT_DEBUG` | (unset) | Set to `1` to enable debug warnings and resource tracking |
| `GT_MAX_FILES` | `5000` | Maximum files for SWE-bench hook indexing |

---

## Development

```bash
pip install -e ".[dev]"

# Tests (648 tests)
pytest tests/ -v

# Lint
ruff check src/ tests/
ruff format --check src/ tests/

# Build Go indexer (requires Go 1.22+ and GCC)
cd gt-index && CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/

# Build with workers flag
gt-index -root . -output graph.db -workers 8
```

---

## Architecture

```
groundtruth/
+-- gt-index/                    # Go indexer (tree-sitter, 30 languages)
|   +-- cmd/gt-index/main.go    # CLI: parallel parse, batch insert, resolve
|   +-- internal/
|       +-- parser/             # Tree-sitter AST extraction
|       +-- resolver/           # 3-stage resolution + confidence scoring
|       +-- store/              # SQLite schema + batch operations
|       +-- specs/              # 30 language specifications
+-- src/groundtruth/             # Python MCP server
|   +-- mcp/server.py           # 16 MCP tools via FastMCP (stdio)
|   +-- index/graph_store.py    # Bridge: Go schema -> SymbolStore interface
|   +-- resolve.py              # LSP precision pass (diagnostic + live)
|   +-- viz/                    # 3D Code City visualization
+-- benchmarks/swebench/
|   +-- gt_intel.py             # Evidence engine (7 families, deterministic)
+-- tests/                      # 648 tests (646 passing)
```

---

## License

MIT

---

*Built by [harneet2512](https://github.com/harneet2512)*
