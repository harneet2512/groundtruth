# GroundTruth Development

This is the GroundTruth project — an MCP server providing compiler-grade codebase intelligence for AI coding agents, via LSP.

## Project Structure
- `src/groundtruth/` — Source code (Python 3.11+)
- `src/groundtruth/mcp/` — MCP server and tool handlers (groundtruth_find_relevant, groundtruth_brief, groundtruth_validate, groundtruth_trace, groundtruth_status)
- `src/groundtruth/lsp/` — Universal LSP client (JSON-RPC over stdio), server manager, protocol types, config
- `src/groundtruth/index/` — SQLite-backed symbol index (indexer, store, graph traversal)
- `src/groundtruth/validators/` — Deterministic validation (imports, packages, signatures, orchestrator)
- `src/groundtruth/ai/` — AI layer: briefing.py (proactive), semantic_resolver.py (reactive), task_parser.py, prompts.py
- `src/groundtruth/stats/` — Intervention tracking and reporting
- `src/groundtruth/cli/` — CLI commands (setup, status, stats, index, validate)
- `tests/` — Unit, integration, and benchmark tests
- `tests/fixtures/` — Test projects (project_ts/, project_py/, project_go/)

## Key Decisions
- LSP-based, language-agnostic — zero language-specific code
- Python 3.11+, mypy --strict, Pydantic, structlog, pytest
- Two-phase architecture: proactive briefing (AI) + reactive validation (deterministic + AI fallback)
- No daemon — MCP server runs on stdio
- SQLite for symbol graph + intervention tracking (stdlib sqlite3, no external dep)
- AI briefing distills full symbol graph into task-relevant context before generation
- AI semantic resolution fires only when deterministic methods (Levenshtein + cross-index) fail
- Universal MCP — no client-specific code
- Separate tools: groundtruth_brief (briefing) and groundtruth_validate (validation)

# currentDate
Today's date is 2026-03-10.
