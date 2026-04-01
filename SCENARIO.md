# GroundTruth — Real-World Scenario Guide

## What This Is

GroundTruth (GT) is an MCP server that gives AI coding agents codebase intelligence before they write code. It indexes your project into a SQLite graph, then answers questions like "what files are relevant?", "what should I know before editing this?", and "who calls this function?".

This document shows how to set it up, use it with Claude Code, and test it on real projects.

---

## Quick Start

### 1. Index Your Project

**Option A: Python indexer (works everywhere)**
```bash
cd /path/to/your/project
python -m groundtruth.main index .
# Creates .groundtruth/index.db
```

**Option B: Go indexer (faster, 30+ languages)**
```bash
gt-index --root=/path/to/your/project --output=.groundtruth/graph.db
# Requires: gt-index binary (build from gt-index/ with Go + CGo)
```

### 2. Configure Claude Code

Add `.mcp.json` to your project root:
```json
{
  "mcpServers": {
    "groundtruth": {
      "command": "python",
      "args": ["-m", "groundtruth.main", "serve", "--root", ".", "--no-auto-index"],
      "env": {
        "PYTHONPATH": "/path/to/groundtruth/src"
      }
    }
  }
}
```

Restart Claude Code. GT tools will appear as MCP tools.

### 3. Use GT Tools

Claude Code can now call these tools automatically:

| Tool | Purpose | AI Cost |
|------|---------|---------|
| `groundtruth_status` | Index health check | $0 |
| `groundtruth_find_relevant` | Task description -> relevant files | ~$0.003 |
| `groundtruth_brief` | Proactive briefing before editing | ~$0.003 |
| `groundtruth_validate` | Check code against the index | $0 |
| `groundtruth_trace` | Symbol callers/callees graph | $0 |
| `groundtruth_dead_code` | Unused exported symbols | $0 |
| `groundtruth_hotspots` | Most-referenced symbols | $0 |
| `groundtruth_impact` | Blast radius analysis | $0 |
| `groundtruth_explain` | Deep dive on a symbol | $0 |
| `groundtruth_context` | File-level context | $0 |

---

## Test Scenarios

### Scenario 1: pluggy — Cross-File Dependency Bug

**Repo:** `pytest-dev/pluggy` (~2500 LOC, Python)
**Issue:** `_remove_plugin` only removes the first HookImpl, breaks multi-impl plugins
**Why GT helps:** Need to understand callers of `_remove_plugin` across `_hooks.py` and `_manager.py`

**Setup:**
```bash
git clone https://github.com/pytest-dev/pluggy.git test_scenarios/pluggy
python -m groundtruth.main index test_scenarios/pluggy
```

**GT tools that help:**
- `groundtruth_trace(symbol="_remove_plugin")` — shows all callers and callees
- `groundtruth_brief(intent="fix _remove_plugin for multi-impl plugins")` — surfaces the coupled `get_hookcallers` method
- `groundtruth_impact(file="src/pluggy/_hooks.py")` — shows blast radius

**Expected GT output:**
```
[VERIFIED] _remove_plugin() at src/pluggy/_hooks.py:234 — signature: (self, plugin: _Plugin) -> None
[VERIFIED] Called by: unregister() at _manager.py:147 — callers expect all impls removed
[WARNING] get_hookcallers() at _manager.py:180 — compensates for partial removal
```

### Scenario 2: marshmallow — Variable Naming Confusion

**Repo:** `marshmallow-code/marshmallow` (~4000 LOC, Python)
**Issue:** Nested partial uses `data_key` instead of `attr_name` for prefix
**Why GT helps:** Need to trace `attr_name` vs `field_name` through the Schema hierarchy

**Setup:**
```bash
git clone https://github.com/marshmallow-code/marshmallow.git test_scenarios/marshmallow
python -m groundtruth.main index test_scenarios/marshmallow
```

**GT tools that help:**
- `groundtruth_brief(intent="fix partial nesting with data_key vs attr_name")` — explains the naming distinction
- `groundtruth_trace(symbol="attr_name")` — shows how it flows through `_deserialize`, `_load_fields`
- `groundtruth_context(file="src/marshmallow/schema.py")` — file-level overview

### Scenario 3: rich — Multi-File Rendering Fix

**Repo:** `Textualize/rich` (~15K LOC, Python)
**Issue:** Background style broken with soft wrap
**Why GT helps:** Fix spans `segment.py` (data model) and `console.py` (rendering)

**Setup:**
```bash
git clone https://github.com/Textualize/rich.git test_scenarios/rich
python -m groundtruth.main index test_scenarios/rich
```

**GT tools that help:**
- `groundtruth_find_relevant(description="background style broken with soft wrap")` — surfaces both `segment.py` and `console.py`
- `groundtruth_trace(symbol="Segment")` — shows the rendering pipeline
- `groundtruth_hotspots()` — reveals `Segment` as a high-usage backbone symbol

---

## How GT Differs From Built-In Tools

| What Agent Does Today | What GT Adds |
|----------------------|--------------|
| `grep` for function name | `groundtruth_trace` shows callers + callees + blast radius |
| Read file to understand structure | `groundtruth_brief` gives pre-computed behavioral context |
| Guess which files matter | `groundtruth_find_relevant` ranks by graph distance |
| Hope imports are correct | `groundtruth_validate` checks against the index |
| Read one file at a time | `groundtruth_context` gives file overview with dependencies |

**Key difference:** GT's tools are deterministic graph queries (SQLite), not AI. They return compiler-grade facts in <10ms. The agent doesn't spend turns exploring — GT already knows.

---

## Architecture

```
Your Project
    |
    v
gt-index (Go binary)  OR  Python indexer (LSP-based)
    |
    v
.groundtruth/graph.db  OR  .groundtruth/index.db  (SQLite)
    |
    v
MCP Server (python -m groundtruth.main serve)
    |  stdio
    v
Claude Code / Cursor / any MCP client
    |
    v
Agent calls groundtruth_* tools during task execution
```

**Indexing:** ~1-5 seconds for typical projects (1K-10K files)
**Tool latency:** <10ms (pure SQLite queries)
**Token cost:** $0 for most tools, ~$0.003 for AI-enhanced briefings

---

## Verified Results

### SWE-bench Verified (500 tasks, Gemini 2.5 Flash)
- **+14 tasks** resolved with GT vs baseline (+2.8pp, +16.5% relative)
- Evidence delivered: upfront briefing + post-edit reminder
- Zero false positive rate (default-deny admissibility gate)
- Indexing: 100% coverage, 30+ languages via tree-sitter

### What GT Catches
- Wrong import paths (symbol exists in a different file)
- Wrong function signatures (arg count mismatch)
- Missing callers (changes that break downstream code)
- Convention violations (naming patterns, return type contracts)

---

## Development Notes

### Two Indexers
- **Python indexer** (`src/groundtruth/index/indexer.py`): Uses Python AST + optional LSP. Works on any platform. Schema: `symbols`, `refs`, `exports`, `packages`.
- **Go indexer** (`gt-index/`): Uses tree-sitter. Faster, 30+ languages. Requires CGo. Schema: `nodes`, `edges`.
- **Bridge** (`src/groundtruth/index/graph_store.py`): `GraphStore` class translates Go schema to Python interface. Auto-detected by MCP server.

### MCP Server
- Entry: `python -m groundtruth.main serve`
- Uses `FastMCP` from the official MCP SDK
- 16 tools registered
- Output wrapped in `<gt-evidence>` XML tags with `[VERIFIED]`/`[WARNING]` tiers
- Structured logging via `structlog`

### CLI Commands
```
groundtruth serve      — Start MCP server (stdio)
groundtruth index      — Build symbol index
groundtruth status     — Show index health
groundtruth validate   — Check a file against index
groundtruth dead-code  — Find unused symbols
groundtruth risk-map   — Hallucination risk scores
groundtruth viz        — 3D Code City visualization
groundtruth city       — Interactive CityView server
```
