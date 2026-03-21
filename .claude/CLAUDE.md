# GroundTruth — Project Instructions

## What This Is

GroundTruth is a repository-grounded intelligence layer for AI coding agents, delivered as an MCP server.

**Current honest wedge:** obligation analysis (what MUST change when a symbol changes), diff checking, structural references, and repo-grounded context injection. These are deterministic, evidence-backed, and measurable.

**What is NOT fully delivered yet:** full "repository-specific judgment" — briefing, adaptive context, semantic resolution are partially built but not yet proven at scale. Do not overclaim.

**Architecture:** Python 3.11+, LSP-based (language-agnostic), SQLite symbol graph, MCP stdio transport. No daemon. No per-language code.

## Current Product Reality

- **Strongest value:** obligation engine (`src/groundtruth/validators/obligations.py`) — coupled-change detection, constructor symmetry, override/caller contracts
- **Validation and contradiction handling** are precision-sensitive. False positives destroy trust. Default to evidence-backed behavior; when uncertain, say nothing rather than guess
- **Prefer product truth over hype.** README claims must not exceed current proof

## Development Lanes

- **Productization lane:** `main` branch. Ship features with tests and evidence.
- **Research lane:** separate branches (e.g., `improvement-v*`). Each must have `BRANCH_PLAN.md`.

## Evaluation Ladder

Never pay the full-run tax for every experiment.

| Gate | What | When |
|------|------|------|
| 0 | Unit + fixture + targeted capability tests | Every change |
| 1 | Microbench / fixture eval | New logic |
| 2 | 10-task diagnostic | Promising results from Gate 1 |
| 3 | 50-task intermediate | Clear signal from Gate 2 |
| 4 | 300-task authoritative run + Docker eval | Merge/promotion candidates only |

## Branch Policy

Every research branch must have `BRANCH_PLAN.md` at root with:
- **Capability:** what this branch adds
- **Primary metric:** how to measure success
- **Cheapest benchmark:** minimum eval to validate
- **Merge threshold:** what numbers earn a merge
- **Kill condition:** when to abandon

See `docs/branch-workflow.md` for the full branch lifecycle, templates, and gate progression guide.

## Coding Rules

- Prefer deterministic evidence over heuristics
- Prefer high precision over noisy "smart" checks
- No benchmark-specific hardcoding
- No broad claims that exceed current proof
- Keep tool surfaces small and composable
- When editing validation logic, explicitly think about false positives
- When editing obligation logic, add tests first or alongside
- Python 3.11+, type hints, Pydantic models, structlog, pytest
- SQLite queries use parameterized statements (no f-strings)
- Update `PROGRESS.md` after every milestone

## Workflow

- When asked to build a feature: first classify as productization vs research
- When asked to evaluate: choose the cheapest valid eval first (use `/eval-ladder`)
- When editing validators: think about false positive rate before writing code
- When starting a research branch: create `BRANCH_PLAN.md` first (use `/branch-plan`)

## Shipping Wedge Sequence (improvement-v0.8)

Build order for the obligation-analysis wedge:

1. **Obligation tests** — `tests/unit/test_obligations.py`
   All 4 kinds (constructor_symmetry, override_contract, caller_contract, shared_state)
   with positive, negative, and edge-case coverage.

2. **Obligation diff fixtures** — `tests/fixtures/obligation_diffs/`
   Known-good diffs with expected obligation output. Gate 1 eval.

3. **check-diff CLI** — `src/groundtruth/cli/commands.py`
   `groundtruth check-diff <patch>` runs obligations against a diff and prints results.
   End-to-end validation target for Gate 0.

## Output Expectations

- Concise plans with exact file paths
- Explicit assumptions stated up front
- No vague "done" claims without evidence
- When reporting eval results: raw numbers, not narratives

## Before You Start Coding

1. **Check branch:** Are you on the right branch for this work?
2. **Check lane:** Is this productization (main) or research (feature branch)?
3. **Check tests:** Do existing tests pass? `python -m pytest tests/unit/ -x -q`
4. **Check obligations:** If touching obligation/validation code, read the existing tests first
5. **Check PROGRESS.md:** What was the last milestone?

## Key Paths

- Entry point: `src/groundtruth/main.py` → CLI via `groundtruth.main:cli`
- MCP server: `src/groundtruth/mcp/server.py`
- MCP tools: `src/groundtruth/mcp/tools.py`
- Obligation engine: `src/groundtruth/validators/obligations.py`
- Symbol store: `src/groundtruth/index/store.py`
- AST parser: `src/groundtruth/index/ast_parser.py`
- CLI commands: `src/groundtruth/cli/commands.py`
- Tests: `tests/unit/`, `tests/integration/`, `tests/fixtures/`

## MCP Server (Project-Local)

Not yet configured in `.mcp.json`. To add later:

```json
// .mcp.json at repo root
{
  "mcpServers": {
    "groundtruth": {
      "command": "python",
      "args": ["-m", "groundtruth.main", "serve"],
      "env": {}
    }
  }
}
```

Verify the exact serve command works before adding this file.
