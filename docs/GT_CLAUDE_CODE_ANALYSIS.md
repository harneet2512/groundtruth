# GT x Claude Code Source Analysis

## Context

GroundTruth (GT) provides deterministic pre-edit behavioral context to AI coding agents. Its proven delta is +14 tasks (+2.8pp) on SWE-bench Verified 500-task with Gemini 2.5 Flash. This analysis reverse-engineers Claude Code's architecture from source to find GT's integration points and unique advantages.

**Repos analyzed:**
- `kuberwastaken-claude-code/spec/` -- 13 architectural spec docs (~1,900 TS files distilled)
- `instructkr-claw-code/src/` -- Python clean-room rewrite

---

## 1. Context Assembly & Query Engine

### How Claude Code Assembles Context

The `QueryEngine.submitMessage()` flow (spec `01_core_entry_query.md:1008-1027`) assembles context in this exact order:

```
1. System Prompt (rebuilt fresh EVERY turn):
   - defaultSystemPrompt | customSystemPrompt
   - + memoryMechanicsPrompt (memory behavior instructions)
   - + relevant memory files (up to 5, selected via Sonnet side-query)
   - + appendSystemPrompt (user additions)

2. User Context (memoized, injected as system-level content):
   - claudeMd (CLAUDE.md files from project root + --add-dir)
   - currentDate ("Today's date is YYYY-MM-DD")

3. System Context (memoized):
   - gitStatus (branch, recent 5 commits, working tree status)

4. Conversation Messages (mutable array):
   - user messages -> assistant messages -> tool_result messages
   - Tool results are UserMessage objects with type: 'tool_result'
```

### The 15-Step Query Loop

```
1.  yield stream_request_start
2.  build queryTracking (chainId/depth)
3.  get messages after compact boundary
4.  applyToolResultBudget           <-- TRUNCATE OLD TOOL RESULTS
5.  snip compact (HISTORY_SNIP)
6.  microcompact                    <-- AGGRESSIVE MESSAGE TRIMMING
7.  context collapse (CONTEXT_COLLAPSE)
8.  build fullSystemPrompt          <-- SYSTEM PROMPT REBUILT
9.  autocompact if needed           <-- CONDITIONAL COMPACTION
10. check blocking token limit
11. CALL MODEL                      <-- API CALL
12. EXECUTE TOOLS                   <-- RUN TOOL CALLS
13. yield messages + tool results
14. handleStopHooks
15. check continuation
```

### Context Overflow Management -- 3-Tier Cascade

| Tier | Trigger | What Happens | What's Preserved |
|------|---------|-------------|-----------------|
| Microcompaction | Unconditional (step 6) | Old tool result content replaced with `'[Old tool result content cleared]'` | Message structure, metadata |
| Autocompaction | Context >90% full | LLM summarizes entire conversation into `<compact_summary>` | Recent turns, semantics |
| Session Memory | Accumulation >40K tokens | Semantic consolidation of older segments | Structure, relevance |

**Clearable tools** (content dropped first): FileRead, Shell, Grep, Glob, WebSearch, WebFetch, FileEdit, FileWrite.

**What gets dropped FIRST:** Oldest tool results (FIFO). System prompt NEVER drops -- rebuilt fresh every turn. CLAUDE.md NEVER drops -- memoized as user context.

### GT Implication

- Tool results have NO special positioning -- they're just conversation messages. There is no "priority slot" GT can target.
- The system prompt is the ONLY guaranteed-to-survive location. CLAUDE.md content lives there.
- GT evidence delivered as tool results WILL be cleared during microcompaction (oldest first).
- **Critical insight:** The format that survives is system prompt content, not tool result content. GT should explore injecting via CLAUDE.md-style mechanisms or memory-style injection rather than pure tool responses.

---

## 2. Tool System Architecture

### MCP Tool Characteristics

Every MCP tool (`03_tools.md:34-39`):
```typescript
{
  isMcp: true
  maxResultSizeChars: 100_000
  permission: 'passthrough'        // ALWAYS asks for permission
  inputSchema: z.object({}).passthrough()  // Accepts any JSON
  output: string                   // Raw text, no structure
}
```

### Per-Tool Size Limits

| Tool | maxResultSizeChars | Notes |
|------|-------------------|-------|
| FileRead | `Infinity` | Never truncated |
| Grep | `20,000` | |
| MCP tools | `100,000` | GT has plenty of room |
| WebFetch | `100,000` | |

### Tool Pool Assembly

```typescript
assembleToolPool(baseTools: Tool[], mcpTools: Tool[]): Tool[]
```

- Built-in tools WIN over MCP tools with the same name (deduplication)
- Tools sorted by name for prompt-cache stability
- No passive injection mechanism for MCP tools -- must be explicitly called

### GT Implication

- MCP tool outputs are plain text strings with a 100K char limit. GT's output format should be optimized for this: compact, structured, unmissable.
- There is NO mechanism for an MCP tool to say "this result is high-priority." Everything is equal-authority text.
- MCP tools always require user permission approval. This is UX friction for GT.
- **Conclusion:** There is no passive injection path via MCP. GT must be an active tool. The lost-in-the-middle problem must be solved through OUTPUT FORMAT, not positioning.

---

## 3. Memory Architecture (Three-Layer System)

### Layer 1: Auto Memory

```
~/.claude/projects/<sanitized-git-root>/memory/
  MEMORY.md                  -- index (always loaded, max 200 lines / 25KB)
  <topic>.md                 -- individual memories with YAML frontmatter
```

Four types: `user`, `feedback`, `project`, `reference`

**Memory injection positioning:** Memory content is injected INTO THE SYSTEM PROMPT (`11_special_systems.md:583-607`). `loadMemoryPrompt()` dispatcher adds memory to system prompt sections. This means memory gets the HIGHEST-PRIORITY context position (system prompt is never dropped). Memory is NOT a tool result -- it bypasses the FIFO truncation cascade entirely.

**Memory relevance selection uses a Sonnet side-query:**
```
scanMemoryFiles() -> parse frontmatter headers (first 30 lines only)
-> formatMemoryManifest() -> sideQuery to Sonnet -> select up to 5 files
-> inject full content into system prompt with freshness warnings
```

### Layer 2: Team Memory

Same format, shared across contributors. Heavy path traversal protection (`PathTraversalError`, `sanitizePathKey`, `validateTeamMemWritePath`). Feature-gated (`tengu_herring_clock`).

### Layer 3: Session Memory / Compact Summaries

Synthetic `<compact_summary>` messages in conversation history. Managed by compaction system, not user-editable. Ephemeral (single session).

### Memory Freshness System

```typescript
memoryAgeDays(mtimeMs)         // Floor-rounded days
memoryFreshnessNote(mtimeMs)   // <system-reminder> wrapped staleness caveat
```

Memories >1 day old get: `"This memory is N days old. Memories are point-in-time observations... Verify against current code before asserting as fact."`

### GT Implication

- **THIS IS THE KEY FINDING.** Memory content gets system-prompt-level positioning -- guaranteed to survive all compaction. Tool results don't.
- GT's behavioral fingerprints and mined rules ARE codebase memory. They describe persistent structural facts that shouldn't be dropped.
- GT should explore a hybrid delivery model:
  1. **Persistent codebase facts** (call graphs, behavioral fingerprints, implicit rules) -- delivered as memory-style content
  2. **Task-specific evidence** (obligation engine output, change-relevant findings) -- delivered as tool results (acceptable to be dropped after use)
- The memory format is simple: markdown with YAML frontmatter. GT could generate `.md` files that match this exact format.

---

## 4. KAIROS Background Consolidation

KAIROS is gated behind `feature('KAIROS')`:
- Polling interval: `SESSION_SCAN_INTERVAL_MS = 10 * 60 * 1000` (10 minutes)
- Trigger: 24h elapsed + 5 sessions since last consolidation
- Spawns forked subagent using `buildConsolidationPrompt()`
- Four phases: Orient -> Gather -> Consolidate -> Prune & Index
- Lock-based isolation (file mutex with PID ownership + 1h stale threshold)
- Changes ONLY memory files -- does not mutate main agent state

### GT Implication

- KAIROS's idle-time maintenance is the exact pattern GT needs for index rebuilding
- GT could register as a KAIROS-compatible background task: when the agent is idle, `gt-index` rebuilds the SQLite database
- The signaling pattern is file-based: GT updates its index DB, and the next tool call reads fresh data
- **However:** KAIROS is feature-gated and not available via MCP. GT would need to implement its own staleness detection.

---

## 5. Codebase Understanding Tools

### What Claude Code Has

Claude Code has an LSP tool (`03_tools.md:1884-1903`) with these operations:
- `goToDefinition`, `findReferences`, `hover`, `documentSymbol`
- `workspaceSymbol`, `goToImplementation`
- `prepareCallHierarchy`, `incomingCalls`, `outgoingCalls`

**Critical: LSP is ON-DEMAND, not pre-computed.**
- No persistent codebase graph
- No pre-built call graphs or dependency maps
- No behavioral analysis
- No symbol index
- Each LSP operation is a single point query -- the model must explicitly call it, pay a tool-call turn, and integrate the result itself

### What Claude Code Does NOT Have

- **Zero pre-edit behavioral context.** The agent writes code, reads files, and hopes.
- **No blast radius analysis.** The agent must manually trace via file reads.
- **No behavioral fingerprints.** No knowledge of what callers DO with return values.
- **No implicit rule mining.** Conventions discovered ad-hoc.
- **No codebase orientation.** Must explore from scratch each session.

### What GT Provides That Claude Code Cannot

| Capability | Claude Code | GroundTruth |
|------------|------------|-------------|
| Symbol lookup | LSP point query (1 turn each) | Pre-computed SQLite (<10ms) |
| Call graph | `incomingCalls`/`outgoingCalls` (per-symbol, 1 turn each) | Full graph traversal, bidirectional, multi-hop |
| Behavioral fingerprints | None | READ/WRITE/RETURN/RAISE per function |
| Implicit rules | None | Mined from 80%+ method patterns |
| System shape | None | What dependents DO with a symbol |
| Blast radius | Manual tracing | Instant `groundtruth_impact` |
| Dead code | None | `groundtruth_dead_code` (pure SQL) |
| Hotspots | None | `groundtruth_hotspots` (pure SQL) |
| Proactive briefing | None | `groundtruth_brief` before code generation |
| Code validation | None | `groundtruth_validate` against index |

**GT is to Claude Code what a flight briefing is to a pilot's instruments.** CC gives the agent instruments (LSP, file reads, grep) that it must learn to use turn-by-turn. GT gives the agent a pre-computed briefing: "here's what you need to know before you touch this code."

---

## 6. Error Handling & Confidence

### Claude Code's Approach

| Error | Recovery |
|-------|----------|
| `max_output_tokens` | Retry up to 3x, incrementing budget |
| Prompt too long | Reactive compact or return `blocking_limit` |
| Streaming fallback | Tombstone orphaned messages, retry |
| `FallbackTriggeredError` | Switch to fallback model, retry |

**No confidence layer on tool outputs.** Information is treated as-is. Memory gets freshness warnings (age-based caveats), but tool results get nothing.

### GT's Advantage

GT's `[VERIFIED]`/`[WARNING]`/`[INFO]` trust tiers with confidence scores (0.00-1.00) are MORE sophisticated than CC's approach. CC has no concept of tool result confidence. GT can say "this is graph-verified" or "this is a heuristic" -- CC cannot.

---

## 7. Side-by-Side Comparison

| Concern | Claude Code | GT Current | GT Better? | Action |
|---------|-------------|-----------|------------|--------|
| **Context positioning** | Tool results = FIFO messages. Memory = system prompt. | Evidence as tool result text | CC better | Explore memory-style injection |
| **Output format** | Plain text. No authority markers. MCP = raw string. | `<gt-evidence>` with tiers | GT better | Keep compact imperative format |
| **Precision** | No precision layer. All tool results equal. | Default-deny admissibility (98.3% rejected) | GT better | Keep default-deny |
| **Behavioral context** | Zero. LSP is point-query-only. | 7 evidence families | GT better | This IS GT's moat |
| **Index freshness** | No index (LSP is live). | Must rebuild after edits | CC better | Mtime-based staleness + incremental rebuild |
| **Active vs passive** | MCP = active only. Memory = passive. | Monkey-patch hook (passive in SWE-bench) | Tie | Accept active-tool model for MCP |
| **Memory/persistence** | 3-layer: auto + team + session | No persistent cross-session state | CC better | Generate memory-format files |
| **Confidence signaling** | None on tool results. Freshness on memories. | `[VERIFIED]`/`[WARNING]` tiers | GT better | Surface in output text |
| **Codebase modeling** | Ad-hoc (Glob + Grep + FileRead + LSP) | Pre-computed SQLite call graphs, 30+ languages | GT better | GT's unique value |

---

## 8. Concrete Recommendations

### R1: Compact Imperative Output Format (HIGH PRIORITY)

Restructure output from informational prose to imperative, scannable format:
```
[VERIFIED] getUserById() callers expect NotFoundError on missing user (7/7 callers catch it)
[VERIFIED] db.query() returns Optional[Row] -- never raw None
[WARNING] test_user_service.py:42 asserts return type is dict, but 5/6 callers destructure as tuple
```

**Evidence from CC:** Tool results are raw text. The model reads and decides. Imperative format is more actionable than descriptive.

### R2: Mtime-Based Staleness Detection

On each GT tool call, compare file mtimes against last index timestamp. If modified files are relevant, either rebuild incrementally or mark evidence as `[STALE]`.

**Evidence from CC:** CC's LSP is always-live. GT's value drops to negative with stale data.

### R3: Trust Tiers in Output Text

Prefix each evidence item with `[VERIFIED]`, `[WARNING]`, or `[INFO]`. The model reads this and weights accordingly.

**Evidence from CC:** CC has NO confidence layer on tool results. GT's tiers give the model a reason to trust GT's output.

### R4: Maximize Evidence Density (~150 tokens)

Cap GT output at ~150 tokens per tool call. Each line = a self-contained fact. No headers, no prose.

**Evidence from CC:** Shorter output survives longer before truncation. Compressed context outperforms verbose.

### R5: Memory-Compatible Files for Persistent Facts

GT generates markdown files in Claude Code's memory format containing persistent codebase facts (hotspots, conventions, dependency map). These get system-prompt-level positioning.

**Evidence from CC:** Memory content NEVER gets dropped. Codebase facts that don't change per-task should use this channel.

### R6: Two-Tier Tool Architecture

1. `groundtruth_context` -- called ONCE at task start. ~100 tokens.
2. `groundtruth_check` -- called after each edit. ~50 tokens.

**Evidence from CC:** Concise one-shot context tool called early gets maximum attention.

### R7: `<system-reminder>` Tags

Wrap critical GT evidence in `<system-reminder>` XML tags. Claude Code uses these for high-priority system content.

**Evidence from CC:** The model is trained to treat `<system-reminder>` content as authoritative.

### Priority Ranking

1. **R1** (compact format) -- highest ROI, immediate
2. **R4** (density/token cap) -- pairs with R1
3. **R3** (trust tiers) -- pairs with R1
4. **R7** (`<system-reminder>` tags) -- small effort, high signal
5. **R2** (staleness detection) -- critical for correctness
6. **R6** (two-tier tools) -- architecture improvement
7. **R5** (memory-compatible files) -- highest ceiling, most complex

---

## 9. GT's 8 Unique Advantages Over Claude Code

### 1. Pre-Computed Call Graphs vs On-Demand Point Queries
CC's LSP tool provides `incomingCalls`/`outgoingCalls` but requires explicit invocation (burns a turn), returns raw data, and has no persistent graph. GT pre-computes the full call graph into SQLite, traverses it in <10ms.

### 2. Behavioral Fingerprinting -- CC Has Nothing
GT computes what functions READ/WRITE/RETURN/RAISE. When an agent modifies `getUserById()`, CC doesn't know that 7/7 callers catch `NotFoundError`. GT does.

### 3. Implicit Rule Mining -- CC Has Nothing
GT mines implicit codebase rules from 80%+ method patterns. CC discovers conventions only by reading code ad-hoc.

### 4. System Shape Analysis -- CC Has Nothing
GT maps what dependents DO with a symbol -- destructure return? check errors? pass through? CC must manually trace.

### 5. Default-Deny Admissibility -- CC Has No Precision Layer
GT's admissibility gate rejects 98.3% of edges with zero false positive leakage. CC treats all tool results equally.

### 6. Deterministic Certainty Layers -- CC Has No Confidence
GT's `[VERIFIED]`/`[WARNING]` tiers provide compiler-grade certainty. CC has no concept of tool result confidence.

### 7. Zero Turn Cost
GT delivers behavioral context WITHOUT burning agent turns (in passive mode). CC's LSP tool costs a turn per query.

### 8. Multi-Family Evidence Synthesis
GT synthesizes 7 evidence families (IMPORT, CALLER, SIBLING, TEST, IMPACT, TYPE, PRECEDENT) into a single output. CC has no evidence synthesis.

---

## Hook System -- Potential GT Integration Path

Claude Code's hook system (`07_hooks.md`, `11_special_systems.md:1307-1404`) supports 4 hook types:

1. **BashCommandHook** (`type: 'command'`) -- shell command on lifecycle events
2. **PromptHook** (`type: 'prompt'`) -- LLM prompt with `$ARGUMENTS`
3. **HttpHook** (`type: 'http'`) -- POST to URL
4. **AgentHook** (`type: 'agent'`) -- verification prompt via Haiku

**Hook events:** PreToolUse, PostToolUse, PostResponse

**GT integration via hooks:** A `PreToolUse` hook on `Write`/`Edit` tools could call GT before file modifications, injecting evidence into the tool execution context. This would be the Claude Code equivalent of GT's monkey-patch hook in SWE-bench.

---

## Source Files Referenced

| Spec File | Key Sections |
|-----------|-------------|
| `01_core_entry_query.md` | Query loop (885-908), submitMessage (1008-1027), recovery (911-921) |
| `03_tools.md` | MCP tools (34-39), tool pool (271-282), LSP (1884-1903), maxResultSizeChars |
| `05_components_agents_permissions_design.md` | Agent architecture, coordinator mode (248-272) |
| `06_services_context_state.md` | Session state (97-156) -- zero codebase indexing |
| `07_hooks.md` | Hook lifecycle events |
| `11_special_systems.md` | Memory system (339-607), KAIROS (531-533), hooks (1307-1404) |
