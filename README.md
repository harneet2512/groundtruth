# GroundTruth

**AI coding agents hallucinate because they operate on incomplete context.** GroundTruth fixes this by giving agents compiler-grade codebase intelligence *before* they generate code -- not after.

It builds a pre-computed call graph with confidence-scored edges, then delivers targeted evidence at the exact moment an agent needs it: which functions to call, what return types to expect, who depends on this code, and what will break if you change it.

The result: agents write code that actually fits the codebase instead of plausible-looking code that silently breaks.

![GroundTruth Code City](groundtruth_hero.png)

---

## SWE-bench Results

> *Unofficial numbers -- official results pending SWE-bench Verified leaderboard approval.*

| Configuration | Baseline | With GroundTruth | Delta |
|--------------|----------|-----------------|-------|
| Gemini 2.5 Flash | 33.3% | **48.3%** | **+15.0 pp** |
| Gemini 3 Flash | 52.1% | **52.9%** | **+0.8 pp** |

GroundTruth adds zero model calls. The evidence is pre-computed and deterministic. The agent receives structural facts about the codebase, not AI-generated summaries.

---

## The Problem

AI coding agents (Claude Code, Cursor, Codex, Windsurf) fail in predictable ways:

- **Wrong imports** -- hallucinate module paths that don't exist
- **Wrong signatures** -- call functions with the wrong number of arguments
- **Wrong assumptions** -- ignore return types, miss error handling patterns, break callers
- **Wrong context** -- edit a function without knowing who depends on it

These failures happen because agents see a narrow window of code (the files they've read) and guess about everything else. GroundTruth replaces guessing with verified facts from the call graph.

## How It Works

```
1. gt-index parses your codebase (30 languages, tree-sitter)
   → Pre-computes call graph with confidence-scored edges

2. Agent receives a task ("fix getUserById returning null")
   → GroundTruth finds relevant files via graph traversal

3. Before the agent writes code:
   → GroundTruth delivers a briefing: signatures, callers, patterns, tests

4. After the agent edits a file:
   → GroundTruth checks: did you break any callers? wrong return type?

All deterministic. Zero AI calls. Zero tokens. <15ms per query.
```

---

## What Makes This Different

| | GroundTruth | Embedding Search | Context Window |
|---|---|---|---|
| **Knows call relationships** | Yes (pre-computed graph) | No | No |
| **Knows who breaks if you change X** | Yes (blast radius) | No | No |
| **Confidence scoring** | Yes (0.0-1.0 per edge) | No | N/A |
| **Cost per query** | $0 | Embedding API cost | Token cost |
| **Latency** | <15ms | 100-500ms | N/A |
| **Works across files** | Yes (full call graph) | Similarity only | Limited by window |

---

## Performance

| Repository | Files | Index Time | Nodes | Edges |
|-----------|-------|------------|-------|-------|
| click | 105 | **334ms** | 1,067 | 2,066 |
| terraform | 3,241 | **7.5s** | 18,247 | 38,963 |
| cpython | 3,392 | **27s** | 93,516 | 194,872 |
| kubernetes | 18,456 | **1.5 min** | 77,526 | 224,197 |
| sentry | 16,798 | **56s** | 45,847 | 73,289 |

Parallel parsing. Batch SQLite inserts. O(1) resolution lookups. Scales to monorepos.

---

## Quick Start

```bash
# Install
pip install -e .

# Index your project (30 languages)
gt-index -root /path/to/repo -output .groundtruth/graph.db

# Start the MCP server
groundtruth serve --root /path/to/repo
```

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

Works with any MCP client (Cursor, Codex, Windsurf).

---

## 16 MCP Tools

All deterministic. All $0 cost.

| Tool | What It Does |
|------|-------------|
| `groundtruth_brief` | Pre-generation briefing: signatures, patterns, constraints |
| `groundtruth_find_relevant` | Task description → ranked relevant files |
| `groundtruth_validate` | Post-generation check against codebase structure |
| `groundtruth_trace` | Full call chain: who calls X, what does X call |
| `groundtruth_impact` | Blast radius: what breaks if you change X |
| `groundtruth_explain` | Deep dive: source, callers, callees, complexity |
| `groundtruth_hotspots` | Most-referenced symbols (highest break risk) |
| `groundtruth_symbols` | All symbols in a file with imports/importers |
| `groundtruth_context` | Symbol usage patterns with code snippets |
| `groundtruth_patterns` | Coding conventions from sibling files |
| `groundtruth_orient` | Codebase structure overview for new tasks |
| `groundtruth_checkpoint` | Session progress with recommendations |
| `groundtruth_dead_code` | Exported symbols with zero references |
| `groundtruth_unused_packages` | Dependencies with no imports |
| `groundtruth_status` | Index health and stats |
| `groundtruth_do` | Auto-router: infers the right tool from a query |

---

## Edge Confidence

Not all call graph edges are equal. GroundTruth scores every edge:

| Score | Meaning | How It's Used |
|-------|---------|--------------|
| **1.0** | Import-verified or same-file | Evidence marked `[VERIFIED]` |
| **0.9** | Unique name (unambiguous) | Evidence marked `[VERIFIED]` |
| **0.5-0.6** | 2-3 candidates | Evidence marked `[WARNING]` |
| **0.2** | Highly ambiguous | Filtered from evidence |

Agents only receive high-confidence evidence. Low-confidence edges exist in the graph for completeness but don't pollute the agent's context.

---

## Language Support

**30 languages** via tree-sitter. Six with full import resolution:

Python, Go, JavaScript, TypeScript, Java, Rust

24 additional languages with name-match resolution and confidence scoring.

Use `groundtruth resolve --resolve --lang python` to upgrade ambiguous edges via LSP.

---

## 3D Code City

```bash
groundtruth city --root /path/to/repo
```

Buildings = modules. Height = complexity. Color = risk. Lines = dependencies.

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v          # 648 tests
ruff check src/ tests/    # lint
```

Go indexer:
```bash
cd gt-index
CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/
```

---

## License

MIT
