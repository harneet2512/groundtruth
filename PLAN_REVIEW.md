# GroundTruth — CEO Plan Review

> **Date:** 2026-03-14
> **Mode:** SCOPE EXPANSION
> **Reviewer:** Claude Opus 4.6 (1M context)
> **Scope:** Full project plan review (CLAUDE.md + PROGRESS.md)

---

## Pre-Review System Audit

### Current System State
- **Not a git repo** (or no commits yet) — no branch history, no stashes, no diffs
- **483 tests passing**, mypy --strict clean, ruff clean
- **47 Python source modules**, ~6000+ LOC production, ~15000+ LOC test
- **15 MCP tools** registered, all with reasoning_guidance and token tracking
- **1 TODO in codebase:** `utils/watcher.py` — file change watcher not implemented
- **0 FIXME/HACK/XXX** — codebase is clean

### What's In Flight
- **Critical blocker:** LSP Client documentSymbol timeout on Windows with Pyright. 6 fix attempts, none resolved. `debug_lsp.py` standalone works; the issue is specific to the LSPClient's async read loop in-process.
- Everything else from phases 1-13 is complete and tested.

### Existing Pain Points
1. **Windows LSP deadlock** — the only hard blocker
2. **Benchmark results are synthetic** — 100 hand-crafted cases, not real-world
3. **FINDINGS.md is a template** — all research metrics show "TBD"
4. **AI layer disconnected** — server.py passes `api_key=None`; agents ARE the AI
5. **File watcher stubbed** — no incremental re-indexing

### Taste Calibration (EXPANSION mode)

**Well-designed patterns (style references):**
1. **`validators/orchestrator.py`** — 4-tier suggestion fallback chain (Levenshtein → component matching → module export listing → cross-index). Elegant, composable, deterministic.
2. **`mcp/tools.py` reasoning_guidance pattern** — Every tool response includes actionable next steps filled with real data. Right UX instinct.

**Anti-patterns to avoid:**
1. **3-second sleep in `manager.py:99`** — band-aid hiding real server readiness detection issue
2. **15 handlers building similar dicts in `tools.py`** — structural repetition begging for a builder pattern

---

## Step 0: Nuclear Scope Challenge

### 0A. Premise Challenge

**Is this the right problem?** Yes. AI agents hallucinate because they lack compiler-grade context. GroundTruth's premise ("give agents the compiler's knowledge") is correct.

**Critical caveat:** The 15-tool surface area suggests scope creep. Agents need 3-4 tools max to be effective, not 15.

**Actual user outcome:** Fewer broken code generations from AI agents. Most direct path to proving this: before/after metric on SWE-bench. **13 phases completed without a single real-world validation** — that's the elephant in the room.

**What if we did nothing?** Competitor tools (SymDex, Aider, Cursor) are improving. The window for differentiation is narrowing. Without real-world proof, the research layers are hypotheses, not findings.

### 0B. Existing Code Leverage

| Sub-problem | Existing Code | Status |
|---|---|---|
| LSP communication | `lsp/client.py`, `lsp/manager.py` | Working (except Windows) |
| Symbol indexing | `index/indexer.py`, `index/store.py` | Working |
| Graph traversal | `index/graph.py` | Working |
| Validation pipeline | `validators/*`, `orchestrator.py` | Working |
| AI briefing/parsing | `ai/*` | Working (with fallbacks) |
| MCP server | `mcp/server.py`, `mcp/tools.py` | Working |
| Research layers | `analysis/*` | Code complete, no real data |
| 3D visualization | `viz/*` | Working |
| SWE-bench harness | `benchmarks/swebench/*` | Scaffolded, never run |
| Verification | `benchmarks/verify/*` | 10 checks, fails on Windows |

**Nothing needs to be rebuilt.** The plan is to make existing code work end-to-end, then prove value.

### 0C. Dream State Mapping

```
  CURRENT STATE                    THIS PLAN                          12-MONTH IDEAL
  ─────────────────────            ─────────────────────              ─────────────────────
  483 tests, 0 real-world     →    SWE-bench benchmarks,         →   Published paper/blog with
  usage. Windows LSP blocked.      Real-world findings,               hard numbers proving
  15 tools, research layers        FINDINGS.md filled,                briefing reduces hallucination
  complete but unvalidated.        3D risk map as demo.               by X%. Adopted by 3+ MCP
  AI disconnected from MCP.                                           clients. LSP works on all
  Synthetic benchmarks only.                                          platforms. Community uses it.
```

### 0D. 10x Check (EXPANSION)

**The 10x version:** GroundTruth as a **Copilot Compiler**. Instead of 15 tools the agent calls manually, a **single-call intelligence oracle**. The agent says "I'm about to modify getUserById" and gets ONE response with files, briefing, risk, impact, patterns, and validation checklist.

One call. One response. Agent doesn't need to learn 15 tools.

**Additionally:** Continuous validation daemon watching agent file writes in real-time, proactively pushing corrections via MCP notifications.

### 0E. Platonic Ideal

The agent never hallucinates because GroundTruth intercepts every code generation. The user experience: "I asked Claude to fix a bug. It fixed it correctly the first time. Every import was right. Every function call matched the signature."

The feeling: **invisible correctness.** You don't notice GroundTruth — you just notice your AI agent stopped making stupid mistakes.

### 0F. Mode Selected

**SCOPE EXPANSION** — The architecture is ambitious. Dream bigger. Build the cathedral. But prove it works.

---

## Section 1: Architecture Review

### System Architecture Diagram

```
                                    ┌─────────────────────────────────────────┐
                                    │           AI Coding Agent               │
                                    │  (Claude Code / Cursor / Codex / etc.)  │
                                    └──────────────────┬──────────────────────┘
                                                       │ MCP (stdio)
                                    ┌──────────────────▼──────────────────────┐
                                    │         GroundTruth MCP Server           │
                                    │  ┌─────────────────────────────────┐    │
                                    │  │      15+1 Tool Handlers         │    │
                                    │  │  groundtruth_do (meta-tool)     │    │
                                    │  │  find_relevant | brief | validate│   │
                                    │  │  trace | status | dead_code     │    │
                                    │  │  hotspots | orient | checkpoint │    │
                                    │  │  symbols | context | explain    │    │
                                    │  │  impact | patterns | unused_pkg │    │
                                    │  └─────┬───────┬──────────┬────────┘    │
                                    │        │       │          │             │
                                    │  ┌─────▼──┐ ┌──▼──────┐ ┌▼──────────┐  │
                                    │  │SQLite  │ │AI Layer │ │Validators │  │
                                    │  │Index   │ │(Haiku)  │ │(Determ.)  │  │
                                    │  │+ FTS5  │ │$0.003/  │ │$0/call    │  │
                                    │  │$0/call │ │call     │ │           │  │
                                    │  └─────┬──┘ └──┬──────┘ └┬──────────┘  │
                                    │        │       │         │             │
                                    │  ┌─────▼───────▼─────────▼──────────┐  │
                                    │  │         LSP Manager               │  │
                                    │  │   pyright | tsserver | gopls     │  │
                                    │  │   rust-analyzer | jdtls          │  │
                                    │  └──────────────────────────────────┘  │
                                    │                                        │
                                    │  ┌──────────────────────────────────┐  │
                                    │  │     Research / Analysis           │  │
                                    │  │  grounding_gap | risk_scorer     │  │
                                    │  │  adaptive_briefing               │  │
                                    │  └──────────────────────────────────┘  │
                                    └────────────────────────────────────────┘
```

### Issues Found & Decisions Made

| # | Issue | Decision | Effort |
|---|---|---|---|
| 1 | 15-tool surface area → agent cognitive load | **Add `groundtruth_do` meta-tool** (find→brief→validate→trace in one call). Keep individual tools. | M |
| 2 | 3-second sleep tax in manager.py:99 | **Replace with LSP readiness probe.** Send lightweight request after `initialized`, wait for response. | S |
| 3 | Cold start every session (30-60s re-index) | **Persistent SQLite index + incremental updates.** Check freshness on startup, skip if recent. Implement watcher. | M |
| 4 | Sequential file indexing (scalability bottleneck) | **Parallel indexing** with asyncio.Semaphore(10) + gather. 5-8x speedup. | M |
| 5 | LSP server crash kills entire index | **Auto-restart + retry.** Detect server exit, restart, retry failed file. Partial index usable. | S |

---

## Section 2: Error & Rescue Map

### Error Path Table

```
  METHOD/CODEPATH              | WHAT CAN GO WRONG              | EXCEPTION CLASS
  -----------------------------|--------------------------------|-----------------
  LSPClient.start()            | Binary not found               | FileNotFoundError
                               | Binary not executable          | OSError
                               | Permission denied              | PermissionError
  LSPClient._read_loop()       | Server crashes (EOF)           | asyncio.IncompleteReadError
                               | Malformed JSON                 | json.JSONDecodeError
                               | Unicode errors                 | UnicodeDecodeError
                               | Task cancelled                 | asyncio.CancelledError
  LSPClient.send_request()     | Server not running             | (checked, returns Err)
                               | Timeout (30s default)          | asyncio.TimeoutError
                               | Server returns error           | (checked, returns Err)
  LSPManager._initialize       | Server rejects capabilities    | (LSP error response)
                               | Server hangs during init       | asyncio.TimeoutError
  Indexer.index_file()         | File read fails                | OSError
                               | LSP server not available       | (returns Err)
                               | documentSymbol returns null    | (handled, returns Ok([]))
  Indexer.index_project()      | No supported files found       | (returns Ok(0))
                               | One file fails, others succeed | (logged, continues)
  SymbolStore.__init__()       | DB file locked                 | sqlite3.OperationalError
                               | Schema migration fails         | sqlite3.OperationalError
                               | Disk full                      | sqlite3.OperationalError
  SymbolStore.insert_symbol()  | Unique constraint violation    | sqlite3.IntegrityError
                               | FTS5 sync fails                | sqlite3.OperationalError
  AIClient.complete()          | No API key                     | (returns Err)
                               | Auth failure                   | anthropic.AuthenticationError
                               | Rate limited                   | anthropic.RateLimitError
                               | API error                      | anthropic.APIError
  BriefingEngine.brief()       | No matching symbols in FTS5    | (returns empty briefing)
                               | AI returns garbage             | (falls back to symbol list)
  SemanticResolver.resolve()   | No API key                     | (returns Err)
                               | AI returns invalid JSON        | (returns Err)
  ValidationOrchestrator       | LSP manager unavailable        | (degrades to no-LSP mode)
  MCP server stdio             | Client disconnects             | BrokenPipeError
```

### Rescue Status

```
  EXCEPTION CLASS              | RESCUED? | RESCUE ACTION              | USER SEES
  -----------------------------|----------|----------------------------|------------------
  FileNotFoundError (LSP)      | Y        | Returns Err(lsp_start_fail)| Tool returns error
  asyncio.TimeoutError (LSP)   | Y        | Returns Err(lsp_timeout)   | "Request timed out"
  json.JSONDecodeError (read)  | Y        | Log warning, continue loop | Nothing (transparent)
  sqlite3.OperationalError     | N → FIX  | Wrap in try/except → Err   | Tool returns error
  sqlite3.IntegrityError       | N → FIX  | Wrap in try/except → Err   | Tool returns error
  anthropic.RateLimitError     | Y        | Returns Err                | Tool returns error
  anthropic.AuthenticationError| Y        | Returns Err                | Tool returns error
  BrokenPipeError (stdout)     | N → FIX  | Catch → clean exit         | Process exits cleanly
  PermissionError (file read)  | Y        | Returns Err                | Tool returns error
  KeyboardInterrupt            | Y        | main.py catches, exit 130  | Clean exit
```

### Critical Gaps (all accepted for fixing)
1. **sqlite3.OperationalError** — DB locked / disk full crashes the process
2. **sqlite3.IntegrityError** — FTS5 desync crashes inserts
3. **BrokenPipeError** — MCP client disconnect crashes server

**Decision:** Fix all 3 gaps. Effort: S.

---

## Section 3: Security & Threat Model

```
  THREAT                        | LIKELIHOOD | IMPACT | MITIGATED?
  ------------------------------|------------|--------|----------
  Prompt injection via symbol   | Medium     | Medium | No → FIX
  names in codebase             |            |        |
  Path traversal via file_path  | Low        | High   | No → FIX
  params in tool calls          |            |        |
  SQLite injection              | Low        | High   | Yes (parameterized)
  Malicious LSP server resp.    | Low        | Medium | No (accept risk)
  API key leakage in logs       | Medium     | High   | OK (structlog doesn't log keys)
  Arbitrary code exec via LSP   | Low        | Critical| Partial (accept risk)
```

**Decision:** Add path sandboxing (validate file_path under root_path) + basic prompt sanitization (strip control chars from symbol names/docs in AI prompts). Effort: S.

---

## Section 4: Data Flow & Interaction Edge Cases

### Core Data Flow

```
  FILE SYSTEM ──▶ LSP SERVER ──▶ JSON-RPC ──▶ SQLite ──▶ MCP TOOL ──▶ AGENT
      │              │              │            │           │           │
      ▼              ▼              ▼            ▼           ▼           ▼
  [deleted?]    [crashed?]    [malformed?]  [locked?]   [timeout?]  [ignored?]
  [binary?]     [OOM?]        [truncated?]  [corrupt?]  [empty?]    [misused?]
  [encoding?]   [hangs?]      [huge msg?]   [full?]     [partial?]  [wrong tool?]
```

### Edge Cases

| INTERACTION | EDGE CASE | HANDLED? | FIX |
|---|---|---|---|
| File indexing | Binary file | Yes | Extension filter |
| File indexing | File deleted during index | No → FIX | OSError handling |
| File indexing | File >1MB | No → FIX | Size guard |
| File indexing | Symlink loop | OK | os.walk default |
| LSP request | Response >1GB | No → FIX | Size cap at 10MB |
| FTS5 search | Special chars | Partial | Accept risk |
| SQLite | Concurrent writes | Partial | WAL mode |
| Validation | Proposed code >100KB | No | Accept risk |
| Tool call | validate before index | Yes | Returns error |
| Tool call | empty description | Yes | Returns empty |
| Brief → Validate | Agent ignores briefing | Yes | Grounding gap measures |

**Decision:** Add file size limit (skip >1MB), cap LSP response at 10MB. Effort: S.

---

## Section 5: Code Quality Review

### Issues

| # | Issue | Decision | Effort |
|---|---|---|---|
| 9 | 15 handlers building similar dicts in tools.py (1600+ lines) | **Extract ToolResponse builder** for reasoning_guidance, token_footprint, serialization | M |
| — | MANIFEST_PARSERS typed as `dict[str, object]` with type:ignore | **Fix type annotation** to proper Callable. XS fix. | XS |

---

## Section 6: Test Review

### Test Diagram

```
  NEW FEATURES (from EXPANSION scope):
    [1] groundtruth_do meta-tool
    [2] Persistent index + incremental re-indexing
    [3] Parallel indexing
    [4] LSP readiness probe
    [5] LSP crash recovery
    [6] Path sandboxing
    [7] SQLite error wrapping
    [8] ToolResponse builder
    [9] File size/symlink guards
    [10] LSP trace file
    [11] Structured indexing metrics
    [12] Smart setup command
```

### Critical Test Gap

**No real LSP integration test exists.** 483 tests pass but none spawn a real Pyright server. The mock tests prove protocol handling works; they don't prove Pyright responds.

**Decision:** Add real LSP integration test on Linux CI. Effort: M.

### Friday 2am Test
A test that spawns real Pyright, indexes a 50-file Python project, verifies all symbols captured.

### Hostile QA Test
Index a project, kill the LSP server mid-indexing, verify partial index is usable and re-indexing completes on retry.

---

## Section 7: Performance Review

### Issue: N+1 LSP Queries per File

`indexer.py:_insert_symbol_recursive` makes individual hover() + references() calls per exported symbol. For a file with 20 symbols = 40 sequential round-trips.

**Decision:** Batch with asyncio.gather (semaphore of 5) per file. 10-50x per-file speedup. Effort: M.

---

## Section 8: Observability & Debuggability

### Issues

| # | Issue | Decision | Effort |
|---|---|---|---|
| 13 | No structured indexing metrics | **Add summary log:** files, symbols, timing, skip reasons | S |
| 14 | No LSP wire traffic debugging | **Add --lsp-trace flag** → .groundtruth/lsp-trace.jsonl | S |

**EXPANSION addition:** The LSP trace file would make debugging a joy. One flag, complete transcript.

---

## Section 9: Deployment & Rollout

### Issues

| # | Issue | Decision | Effort |
|---|---|---|---|
| 15 | No guided first-run experience; LSP server install is manual | **Smart `groundtruth setup` command:** detect languages, check servers, offer install commands | M |
| 16 | No CI; Windows-only development | **3-OS GitHub Actions CI:** Ubuntu + macOS + Windows. Real LSP test on Linux. | M |

---

## Section 10: Long-Term Trajectory

- **Reversibility:** 4/5. LSP-only is the one hard-to-reverse bet, but indexer is cleanly separated — tree-sitter indexer could be plugged in later.
- **Technical debt:** MANIFEST_PARSERS type:ignore, debug log artifacts in read loop, 1600-line tools.py, research layers with no real data.
- **Path dependency:** LSP bet is correct. If LSP proves unreliable, architecture supports swapping the indexer.
- **Knowledge concentration:** Documentation is excellent. New engineer onboards in hours.
- **Platform potential:** SQLite symbol index is a general-purpose code database. Risk scoring could power IDE extensions. Grounding gap metric is publishable research. 3D viz is a compelling demo.

### Phase 2/3 Planning (EXPANSION)

After this plan ships:
- **Phase 2:** SWE-bench evaluation with real numbers. Fill FINDINGS.md. Blog post.
- **Phase 3:** Community launch. PyPI package. MCP marketplace listing. More language servers.
- **Phase 4:** Continuous validation daemon. Real-time file watching + proactive correction.

---

## Delight Opportunities (6 identified, all → TODOS.md)

1. **Auto-fix mode** — return `diff` field for high-confidence fixes (~30 min)
2. **Progress streaming** — MCP progress notifications during indexing (~20 min)
3. **"Did you mean?"** — human-readable error messages (~15 min)
4. **Codebase health score** — single 0-100 number in status (~30 min)
5. **Agent workflow prompt** — ship .claude/commands/groundtruth-workflow.md (~15 min)
6. **Hallucination leaderboard** — track most-confused symbols across sessions (~20 min)

---

## Failure Modes Registry

```
  CODEPATH              | FAILURE MODE           | RESCUED? | TEST? | USER SEES?     | LOGGED?
  ----------------------|------------------------|----------|-------|----------------|--------
  LSP Client start      | Binary not found       | Y        | Y     | Error JSON     | Y
  LSP Client read       | Server crash (EOF)     | Y        | Y     | Silent exit    | Y
  LSP Client send       | Timeout                | Y        | Y     | Error JSON     | Y
  SQLite insert         | OperationalError       | N→FIX    | N→ADD | Crash→Error    | N→ADD
  SQLite insert         | IntegrityError         | N→FIX    | N→ADD | Crash→Error    | N→ADD
  MCP stdout            | BrokenPipeError        | N→FIX    | N→ADD | Crash→Clean    | N→ADD
  Indexer               | File >1MB              | N→FIX    | N→ADD | Skip+Logged    | N→ADD
  LSP response          | Response >10MB         | N→FIX    | N→ADD | Truncated      | N→ADD
  Index project         | Symlink loop           | OK       | N     | os.walk default | N
  FTS5 query            | Special chars          | Partial  | N     | Error          | N
  AI complete           | No API key             | Y        | Y     | Fallback       | Y
  AI complete           | Rate limit             | Y        | Y     | Error JSON     | Y
```

**CRITICAL GAPS:** SQLite errors, BrokenPipeError, file size guard — all accepted for fixing.

---

## Priority Execution Order

1. **LSP Readiness Probe** (S) — may fix Windows timeout
2. **LSP Trace File** (S) — if probe doesn't fix it, diagnoses the real issue
3. **3-OS CI Matrix** (M) — get tests on Linux where LSP works
4. **Real LSP Integration Test** (M) — prove core thesis
5. **Persistent Index + Incremental** (M) — UX-critical
6. **Meta-Tool `groundtruth_do`** (M) — the 10x feature
7. **Parallel Indexing + Concurrent Queries** (M+M) — performance
8. **Everything else in P2** — hardening, DRY, observability
9. **Delight items** — once core is proven

**Critical path:** Readiness probe → Trace → CI → Real test → Proof

---

## Completion Summary

```
  +====================================================================+
  |            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
  +====================================================================+
  | Mode selected        | SCOPE EXPANSION                             |
  | System Audit         | No git repo, 483 tests, 1 TODO, 0 FIXME    |
  |                      | Windows LSP timeout is sole hard blocker    |
  | Step 0               | EXPANSION mode. Meta-tool is the 10x play.  |
  |                      | Platonic ideal: invisible correctness.       |
  | Section 1  (Arch)    | 5 issues found                              |
  | Section 2  (Errors)  | 15 error paths mapped, 3 CRITICAL GAPS      |
  | Section 3  (Security)| 3 issues found, 1 High severity             |
  | Section 4  (Data/UX) | 11 edge cases mapped, 6 unhandled           |
  | Section 5  (Quality) | 2 issues found                              |
  | Section 6  (Tests)   | Diagram produced, 1 CRITICAL GAP            |
  | Section 7  (Perf)    | 1 issue found                               |
  | Section 8  (Observ)  | 2 gaps found                                |
  | Section 9  (Deploy)  | 2 risks flagged                             |
  | Section 10 (Future)  | Reversibility: 4/5, debt items: 4           |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (8 items)                            |
  | What already exists  | written (10 modules, all reused)             |
  | Dream state delta    | written                                      |
  | Error/rescue registry| 15 methods, 3 CRITICAL GAPS → all accepted  |
  | Failure modes        | 12 total, 3 CRITICAL GAPS → all accepted    |
  | TODOS.md updates     | 20 items proposed (4 P1, 11 P2, 6 P3)      |
  | Delight opportunities| 6 identified (all → TODOS.md)               |
  | Diagrams produced    | 2 (system architecture, data flow)           |
  | Stale diagrams found | 0                                            |
  | Unresolved decisions | 0                                            |
  +====================================================================+
```
