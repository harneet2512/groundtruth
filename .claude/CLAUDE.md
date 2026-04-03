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

## SWE-bench Eval Rules (learned from production runs)

### Before ANY full run (500 tasks)
1. **Disk first**: Resize Azure disk to 256GB+ BEFORE launching. Docker images need ~100GB. Never launch on a 30GB disk.
2. **Deep smoke test**: Run 10 tasks, then check ALL of:
   - Avg evidence lines per briefing (target: <10, red flag: >20)
   - Abstention rate (target: <10%, red flag: >25%)
   - VERIFIED rate (target: >60%, red flag: <40%)
   - Token count per evidence block (target: <500, red flag: >700)
   - Each of the 7 evidence families: check CONTENT not just "fires or not"
   - TEST must have actual assertion values, CALLER must have call line text, PRECEDENT must have before/after
3. **Evaluate the smoke test**: Run swebench harness on the 10 tasks. Checking patches is NOT enough. You need resolved count.
4. **Workers vs CPUs**: Never use more workers than CPU cores. 4 CPUs = 4 workers max. 6 workers on 4 CPUs causes load 30+ and crawls.

### During the run
5. **Monitor disk**: Check every 30 min. If >90% full, prune completed repo images immediately.
6. **Track errors**: Keep a running count of Docker errors. Don't wait until the end to discover 158 errors.
7. **Don't sleep**: Give instant status updates. Never sleep 5+ minutes before responding.

### After the run
8. **Resolve ALL errors**: A run with errors is incomplete. Keep re-running eval rounds until every task has a result.
9. **Correct math**: Resolve rate = resolved / 500 (full benchmark), NOT resolved / completed. 289/500 = 57.8%, not 59.1%.
10. **No internal version numbers in public**: Don't expose v19d, v20 etc. in README, commits, or submissions.

### Evidence quality
11. **Specs not pointers**: "assert func(x) == y" beats "test_foo references function". Every evidence family should deliver behavioral contracts, not navigation aids.
12. **Token budget matters**: 118 avg lines is catastrophic. 5-10 lines is ideal. If the knapsack isn't capping, the evidence floods the agent's context and hurts more than helps.
13. **Test evidence families in smoke, not just hook rate**: The v19d run had 100% hook rate but only IMPORT was firing meaningfully. Caught too late.
