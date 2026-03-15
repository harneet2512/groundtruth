# GroundTruth — TODOS

> Updated from CEO Plan Review (2026-03-14, HOLD SCOPE mode)

## P0 — Do First (Prerequisite for Everything)

### Git Init + GitHub + 3-OS CI Matrix
- **What:** Initialize git repo, create GitHub remote, push. Set up GitHub Actions: lint (ubuntu), unit tests (ubuntu + macos + windows), integration (ubuntu with real Pyright)
- **Why:** 11K lines of source with zero version control. No CI means no real LSP testing (Windows is broken, Linux works). This unblocks everything.
- **Context:** CI config already designed in PROGRESS.md Phase 1.3. `.gitignore` is comprehensive. `tests/integration/test_real_lsp.py` already exists (skipped on Windows).
- **Effort:** M
- **Depends on:** Delete node_modules first

### Delete node_modules/
- **What:** Remove 147MB of node_modules from archived TypeScript implementation
- **Why:** Dead weight from old TS codebase. Already excluded by .gitignore but wastes disk and causes confusion. Must be done before git init.
- **Effort:** XS
- **Depends on:** Nothing

## P1 — Must Do

### Fix Windows LSP Timeout
- **What:** Resolve documentSymbol timeout on Windows with Pyright
- **Why:** Core blocker — verification fails on Windows, no local real-world testing possible
- **Context:** 6 fix attempts so far. debug_lsp.py standalone works. Issue is in-process async read loop. CI on Linux unblocks testing even if Windows remains broken.
- **Effort:** M
- **Depends on:** Nothing — but CI on Linux reduces urgency

### SQLite check_same_thread=False
- **What:** Add `check_same_thread=False` to `sqlite3.connect()` in `store.py`
- **Why:** FastMCP may dispatch concurrent tool calls on different threads. Default `check_same_thread=True` will crash with `ProgrammingError`. SQLite WAL mode handles concurrent reads safely.
- **Effort:** XS
- **Depends on:** Nothing

### MCP Handler Error Boundary
- **What:** Wrap tool handler calls in server.py with try/except. On unhandled exception: log full context via structlog, return `{"error": "...", "error_type": "..."}` instead of crashing the MCP response.
- **Why:** 16 tool handlers have no top-level error boundary. Any unhandled exception crashes the MCP response with no useful error message to the agent.
- **Context:** Add the boundary in the @app.tool() wrapper or _finalize. Single choke point covers all 16 tools.
- **Effort:** S
- **Depends on:** Nothing

### Real LSP Integration Test
- **What:** Test that spawns real Pyright, indexes fixture project, verifies symbols indexed
- **Why:** 555 tests pass but none exercise the real LSP path in CI
- **Context:** `tests/integration/test_real_lsp.py` exists. Run on Linux CI where Pyright works.
- **Effort:** M
- **Depends on:** CI setup (P0)

## P2 — Should Do

### Replace Silent except-pass Patterns
- **What:** Change `except Exception: pass` to `logger.warning(...)` in two locations: grounding gap linking (tools.py:389) and language query (tools.py:539)
- **Why:** Zero silent failures. These silently swallow errors, making debugging impossible.
- **Effort:** XS
- **Depends on:** Nothing

### MCP Tool Entry/Exit Logging
- **What:** Add `logger.info("tool_call_start", tool=name, ...)` at entry of each @app.tool() and `logger.info("tool_call_end", tool=name, latency_ms=...)` at exit
- **Why:** Currently no structured log when a tool is called or what arguments it received. "groundtruth_validate hung" is undiagnosable.
- **Effort:** S
- **Depends on:** Nothing

### ToolResponse Builder Adoption
- **What:** Adopt the existing `mcp/response.py` ToolResponse builder in tools.py handlers
- **Why:** DRY. 2042-line tools.py has significant structural repetition building response dicts.
- **Effort:** M
- **Depends on:** Nothing

### Concurrent Per-Symbol LSP Queries
- **What:** asyncio.gather hover+references for all symbols within a file
- **Why:** N+1 sequential LSP queries per file is the main per-file bottleneck
- **Effort:** M
- **Depends on:** LSP timeout fix or CI proving it works on Linux

## P3 — Delight Opportunities (Vision)

### Auto-Fix Mode
- **What:** Return `diff` field with exact code fix for high-confidence deterministic suggestions
- **Why:** "GroundTruth didn't just find the bug, it wrote the fix"
- **Effort:** S (~30 min)

### Progress Streaming During Indexing
- **What:** Emit MCP progress notifications: 'Indexing file 47/123...'
- **Why:** Silence for 30-60s during index is poor UX
- **Effort:** S (~20 min)

### "Did You Mean?" Error Messages
- **What:** Format suggestions as sentences: 'Did you mean `from utils.crypto import hashPassword`?'
- **Why:** Agent can relay directly to user without reformatting JSON
- **Effort:** S (~15 min)

### Codebase Health Score
- **What:** Single 0-100 number in status: risk distribution, dead code ratio, unused packages
- **Why:** Like a credit score for your codebase. Users track over time.
- **Effort:** S (~30 min)

### Agent Workflow Prompt
- **What:** Ship `.claude/commands/groundtruth-workflow.md` teaching optimal GroundTruth usage
- **Why:** Users drop it in their project → agent immediately knows the pattern
- **Effort:** S (~15 min)

### Hallucination Leaderboard
- **What:** Track most-confused symbols across sessions in groundtruth_status
- **Why:** Users discover their codebase's naming problems
- **Effort:** S (~20 min)

## Minor Fixes

### _finalize Double Serialization
- **What:** `_finalize` in server.py calls `json.dumps(result)` twice — once for token tracking, once for output. Serialize once and reuse.
- **Why:** Wasteful, though harmless. Obvious to anyone reading the code.
- **Effort:** XS

## Completed (from prior review)

- [x] Meta-Tool `groundtruth_do` — built, 16th MCP tool
- [x] Persistent Index + Incremental Updates — index_metadata table, freshness check
- [x] Parallel Indexing — asyncio.Semaphore + gather in index_project
- [x] Smart Setup Command — `groundtruth setup` detects languages, checks servers
- [x] LSP Crash Recovery — ensure_server + poison file tracking
- [x] SQLite WAL checkpoint — PRAGMA wal_checkpoint(TRUNCATE) in initialize
- [x] BrokenPipeError Handling — caught in serve_cmd + _write_message
- [x] Path Sandboxing + Prompt Sanitization — validate_path + sanitize_for_prompt
- [x] File Size + Symlink Guards — max_file_size, followlinks=False
- [x] Structured Indexing Metrics — index_complete log event
- [x] LSP Trace File — --lsp-trace flag, JSONL format, rotation
- [x] LSP Readiness Probe — probe_ready method on LSPClient
- [x] MANIFEST_PARSERS Type Safety — proper Callable type
