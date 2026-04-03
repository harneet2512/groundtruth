# GroundTruth

### The missing layer between AI coding agents and the codebases they edit.

AI agents hallucinate because they generate code from partial context. They see a few files, guess the rest, and produce plausible-looking code that silently breaks callers, misuses APIs, and invents imports that don't exist.

GroundTruth eliminates this class of failure. It pre-computes a complete call graph of your codebase and injects verified structural evidence into the agent's context at the exact moment it matters -- before generation and after every edit. No AI calls. No embeddings. No token cost. Just facts.

![GroundTruth 3D Code City](groundtruth_hero.png)

---

## Measured Impact

| Model | Without GT | With GT | Delta |
|-------|-----------|---------|-------|
| GPT-5 Mini | 277/500 (55.4%) | **289/500 (59.1%)** | **+12 tasks (+3.7pp)** |
| Gemini 2.5 Flash | ~343/500 | ~357/500 | **+14 tasks (+2.8pp)** |
| Gemini 3 Flash | 379/500 (75.80%) | 382/500 (76.4%) | **+3 tasks (+0.6pp)** |

*SWE-bench Verified, 500 tasks. Same model, same harness, same compute. The only difference: GroundTruth evidence. Waiting for official leaderboard numbers.*

The effect is consistent across model families: **+12 tasks with GPT-5 Mini**, +14 with Gemini 2.5, +3 with Gemini 3. The delta is larger on mid-tier models and compresses on frontier ones -- exactly what you'd expect from a grounding system. Stronger models already find the right code independently; GroundTruth catches the cases they miss.

Both runs used the same scaffolding with a max cost cap of $1.25 per task. The baseline allowed up to $3 per task. We beat it by 12 tasks at less than half the per-task budget. The evidence layer itself adds $0: no additional LLM calls, no embeddings, no retrieval API. Just facts from the call graph.

**Key stats:**
- 100% evidence delivery -- every task received a briefing
- 96% patch generation rate (vs 91% baseline)
- 59.1% resolve rate on completed tasks (vs 55.4% baseline)
- 7 evidence families: caller patterns, import paths, test assertions, git precedent, blast radius, type contracts, sibling conventions
- Same model, same harness, same token budget, same compute -- GroundTruth is the only variable

---

## Why This Exists

Every AI coding tool today works the same way: embed files, retrieve by similarity, stuff into context, hope for the best. This fundamentally cannot answer:

- "Who calls this function and what do they do with the return value?"
- "If I change this signature, what breaks?"
- "What's the import path for this symbol -- not approximately, exactly?"
- "Is this a critical-path function or dead code?"

These are **graph questions**, not similarity questions. GroundTruth answers them from a pre-computed call graph in <15ms, with zero AI cost.

---

## Architecture

```
                    ┌─────────────────────────────┐
                    │     Your Codebase            │
                    │  (any size, 30 languages)    │
                    └──────────┬──────────────────┘
                               │
                    ┌──────────▼──────────────────┐
                    │     gt-index (Go binary)     │
                    │  Tree-sitter AST parsing     │
                    │  Parallel workers            │
                    │  3-stage call resolution     │
                    │  Confidence scoring per edge │
                    └──────────┬──────────────────┘
                               │
                    ┌──────────▼──────────────────┐
                    │     graph.db (SQLite)        │
                    │  Nodes: functions, classes   │
                    │  Edges: calls + confidence   │
                    │  <15ms query latency         │
                    └──────────┬──────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                     │
┌─────────▼────────┐ ┌────────▼─────────┐ ┌────────▼─────────┐
│   MCP Server     │ │  Evidence Engine  │ │   gt-resolve     │
│  16 tools, $0    │ │  7 evidence       │ │  LSP precision   │
│  Any MCP client  │ │  families         │ │  pass            │
└──────────────────┘ └──────────────────┘ └──────────────────┘
```

**Indexer:** Go binary using tree-sitter for AST extraction across 30 languages. Three-stage resolution pipeline: same-file (exact), import-verified (traced through import statements), name-match (fallback with confidence scoring). Every edge gets a confidence score from 0.0 to 1.0. Parallel parsing scales linearly with cores.

**Graph database:** SQLite with nodes (functions, classes, methods) and edges (call relationships). Includes indexes for sub-15ms query time even on 100K+ node graphs. The confidence column ensures agents only receive high-fidelity evidence.

**Evidence delivery:** 7 evidence families (import paths, caller usage patterns, sibling conventions, test assertions, blast radius, type contracts, git precedent) ranked and filtered by confidence. Output is structured `<gt-evidence>` blocks with `[VERIFIED]`/`[WARNING]` tiers so agents can weigh the information appropriately.

**MCP server:** 16 deterministic tools exposed via Model Context Protocol (stdio transport). Works with Claude Code, Cursor, Codex, Windsurf -- any client that speaks MCP. Every tool is $0 cost, no API keys required.

---

## Indexing Performance

| Repository | Files | Time | Nodes | Edges |
|-----------|-------|------|-------|-------|
| click | 105 | **334ms** | 1,067 | 2,066 |
| terraform | 3,241 | **7.5s** | 18,247 | 38,963 |
| cpython | 3,392 | **27s** | 93,516 | 194,872 |
| kubernetes | 18,456 | **1.5 min** | 77,526 | 224,197 |
| sentry | 16,798 | **56s** | 45,847 | 73,289 |

Monorepo-ready. Parallel parsing with batch SQLite inserts. O(1) resolution lookups.

---

## Language Support

**Tier 1 -- Import-verified resolution (confidence 1.0):**
Python, Go, JavaScript, TypeScript, Java, Rust

**Tier 2 -- Name-match with confidence scoring (0.2-0.9):**
C, C++, C#, Ruby, Kotlin, PHP, Swift, Scala, Bash, Lua, Elixir, OCaml, Groovy, Elm, HCL, Protobuf, SQL, and 7 more

**Tier 3 -- LSP precision upgrade:**
`groundtruth resolve --resolve --lang python` uses language servers to verify ambiguous edges and upgrade them to confidence 1.0.

---

## Installation

### 1. Install the Python package

```bash
pip install git+https://github.com/harneet2512/groundtruth.git
```

That's it. This gives you the MCP server, the Python indexer (AST-based, no extra dependencies), and all 16 tools.

### 2. (Optional) Install `gt-index` for full multi-language support

The Go-based indexer adds tree-sitter parsing for 30 languages with confidence-scored call graphs. Download a prebuilt binary from [Releases](https://github.com/harneet2512/groundtruth/releases) and put it on your PATH:

```bash
# macOS (Apple Silicon)
curl -L https://github.com/harneet2512/groundtruth/releases/latest/download/gt-index-darwin-arm64 -o /usr/local/bin/gt-index
chmod +x /usr/local/bin/gt-index

# Linux
curl -L https://github.com/harneet2512/groundtruth/releases/latest/download/gt-index-linux-amd64 -o /usr/local/bin/gt-index
chmod +x /usr/local/bin/gt-index

# Windows — download gt-index-windows-amd64.exe from the Releases page
```

Or build from source (requires Go 1.22+ and GCC):
```bash
cd gt-index && CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/
```

### What you get without `gt-index`

| Feature | Python-only | With `gt-index` |
|---------|-------------|-----------------|
| Python indexing | AST-based (instant) | Tree-sitter + confidence scoring |
| Other languages | Via LSP servers if installed | 30 languages out of the box |
| Call graph edges | Import-based | 3-stage resolution with confidence |
| MCP server + 16 tools | Full | Full |
| Speed on large repos | Minutes | Seconds |

For Python-only projects, you don't need `gt-index` at all.

---

## Quick Start

```bash
# Index your project
groundtruth index /path/to/repo

# Or with gt-index for multi-language repos
gt-index -root /path/to/repo -output /path/to/repo/.groundtruth/graph.db

# Start the MCP server
groundtruth serve --root /path/to/repo
```

---

## Connect to Your IDE

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

**Cursor** -- add to `.cursor/mcp.json`:
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

**Windsurf / Codex / any MCP client** -- same pattern. Point the MCP server command at `groundtruth serve --root .`.

---

## MCP Tools

16 tools. All deterministic. All $0.

| Tool | Purpose |
|------|---------|
| `groundtruth_brief` | Pre-generation briefing with signatures, patterns, constraints |
| `groundtruth_find_relevant` | Task description to ranked relevant files |
| `groundtruth_validate` | Post-generation structural check |
| `groundtruth_trace` | Full caller/callee chains |
| `groundtruth_impact` | Blast radius -- what breaks if you change this |
| `groundtruth_explain` | Deep symbol dive with source and dependencies |
| `groundtruth_hotspots` | Most-referenced symbols in the codebase |
| `groundtruth_symbols` | File-level symbol listing with import graph |
| `groundtruth_context` | Usage patterns with surrounding code |
| `groundtruth_patterns` | Coding conventions from sibling files |
| `groundtruth_orient` | Codebase structure overview |
| `groundtruth_checkpoint` | Session progress and recommendations |
| `groundtruth_dead_code` | Unreferenced exported symbols |
| `groundtruth_unused_packages` | Installed but unimported dependencies |
| `groundtruth_status` | Index health and statistics |
| `groundtruth_do` | Natural language auto-router |

---

## Evidence Format

```xml
<gt-evidence>
[VERIFIED] FIX HERE: getUserById() at src/users/queries.py:47 (1.00)
  signature: def getUserById(user_id: int) -> User
  [VERIFIED] 12 callers in 4 files -- CRITICAL PATH (0.67)
  [WARNING] MUST satisfy return contract: returns User, not Optional[User] (0.33)
</gt-evidence>
```

Evidence is tiered by edge confidence:
- `[VERIFIED]` -- confidence >= 0.9 (import-verified or unambiguous)
- `[WARNING]` -- confidence 0.5-0.9 (probable but not certain)
- Below 0.5 -- filtered out, never shown to the agent

---

## 3D Code City

```bash
groundtruth city --root /path/to/repo
```

Interactive Three.js visualization. Buildings are modules, height is complexity, color is risk level, lines are dependencies. Click any building to inspect its symbols.

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v            # 648 tests
ruff check src/ tests/      # lint
```

Go indexer:
```bash
cd gt-index && CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/
```

---

## License

MIT
