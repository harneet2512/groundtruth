# GT Research Sprint — Cross-File Intelligence as a Product

**Date:** 2026-03-28
**Scope:** Agent architecture gap analysis, deterministic signals, ideal context format, scale architecture, product strategy

---

## The Core Finding

**Every AI coding agent — terminal, IDE, autonomous, pipeline, fine-tuned — does RETRIEVAL. None does ANALYSIS.** Retrieval finds code for the agent to read. Analysis computes facts ABOUT code that require understanding the whole codebase. This gap is identical across all architectures. GT fills it.

The gap exists because retrieval-based systems (embeddings, grep, LSP go-to-definition) find CODE. They don't COMPUTE UNDERSTANDING from that code. The agent reads retrieved files, builds its own mental model, and often runs out of context or turns before understanding the full picture. GT computes the understanding directly.

---

## 1. The Universal Analysis Gap Across All Agent Architectures

### What Every Agent Can Do (Retrieval)

| Capability | Terminal CLI | IDE Agents | Autonomous | Pipeline |
|---|---|---|---|---|
| Read files | Yes | Yes | Yes | Yes |
| Text search (grep) | Yes | Yes | Yes | Yes |
| Semantic search (embeddings) | No | Yes (Cursor, Copilot, Continue, Augment, Windsurf) | Partial (Factory) | No |
| Single-query LSP | Claude Code only (Dec 2025) | Via IDE's LSP | No | No |

### What NO Agent Can Do (Analysis)

| Capability | Terminal CLI | IDE Agents | Autonomous | Pipeline | **GT** |
|---|---|---|---|---|---|
| **Precomputed symbol index** | No | No | Factory only (proprietary) | No | **Yes — SQLite** |
| **Cross-file call graph traversal** | No | No | Factory only (proprietary) | No | **Yes — BFS/DFS over refs** |
| **Task-to-file mapping** | No | No | Agentless (LLM guess) | No | **Yes — deterministic** |
| **Proactive briefing** | No | No | No | No | **Yes** |
| **Import/symbol validation** | No | No | No | No | **Yes** |
| **Hallucination risk scoring** | No | No | No | No | **Yes** |
| **Impact analysis (blast radius)** | No | No | No | No | **Yes — pure SQL, <10ms** |

### Agent-by-Agent Analysis

**Claude Code** has LSP tools since Dec 2025 (goToDefinition, findReferences, hover, documentSymbol for 11 languages). But these are point-query, on-demand, one-question-at-a-time. The agent must decide to ask each question. No precomputed index, no batch analysis, no proactive briefing, no validation against an index. Claude Code's LSP is the microscope; GT is the lab report.

**Aider** uses a tree-sitter-based "repo map" — PageRank-ranked symbol summaries compressed to fit a token budget. No import validation, no cross-file behavioral analysis, no MCP support. Integration: precomputed context file injection.

**SWE-agent** has find_file, search_file, search_dir, open, edit + shell. All text-based search. Linter catches syntax errors only. Custom tools can be added; hooks via `AbstractAgentHook`. Integration: custom ACI tool or pre-step context injection.

**OpenHands** has FileEditorTool, TerminalTool, browser. Text-based access only. No semantic understanding. Integration: injected shell command or initial prompt context.

**Codex CLI** has shell_exec + apply_diff. First-class MCP client support (`~/.codex/config.toml`). No built-in code intelligence. Integration: MCP server.

**Cursor** uses bi-encoder retrieval → cross-encoder reranking → top-k chunks to LLM. Embeddings in Turbopuffer. Tree-sitter chunking. Full MCP support. Retrieves SIMILAR chunks — cannot compute cross-file behavioral facts. No validation layer.

**GitHub Copilot** uses local workspace indexing + VS Code's LSP for retrieval context. Copilot Spaces for persistent grounding. MCP support in VS Code. Same retrieval-not-analysis gap.

**Windsurf** has "Codemaps" — structural maps of file relationships. Learns conventions over ~48 hours. MCP support. No behavioral analysis, no validation.

**Augment Code ($252M)** indexes 400K+ files with semantic embeddings. Claims 40% hallucination reduction. MCP Context Engine server. Closest competitor — but retrieves and contextualizes, doesn't compute and validate. Cloud-dependent, closed-source.

**Devin** has Planner/Coder/Critic multi-agent architecture. Critic is LLM reviewing LLM output — not deterministic validation. Closed system, no MCP.

**Factory AI** has HyperCode — multi-resolution graph + latent space. ByteRank retrieval. Most sophisticated codebase understanding among autonomous agents, but proprietary and locked inside their pipeline. 19.27% SWE-bench Full.

**Agentless** does hierarchical fault localization (file → class → line) via LLM prompting. 27.3% SWE-bench Lite at $0.34/issue. No cross-file analysis — LLM guesses from repo structure string. GT's `find_relevant` could replace entire Phase 1.

### Critical Finding: Claude Code's LSP vs GT

Claude Code now has LSP tools that provide the same raw queries GT uses internally. But GT adds:
1. **Precomputed index** — no per-query LSP server startup/latency
2. **Graph traversal** — BFS/DFS over the complete ref graph, not one-at-a-time queries
3. **Validation layer** — deterministic checking of imports, packages, signatures
4. **Proactive briefing** — agent gets intelligence before writing, not after
5. **Risk scoring** — computed from index, not available via raw LSP
6. **Task-to-file mapping** — natural language → relevant files via graph, not grep

---

## 2. How GT Plugs Into Each Agent Type

### Integration Matrix

| Agent | MCP Support | Integration Path | Latency Constraint | Best Format |
|---|---|---|---|---|
| Claude Code | First-class | `.mcp.json` config | <200ms (Anthropic benchmark) | Structured text |
| Cursor | Full | `mcp.json` | <1s | Structured text |
| Codex CLI | Full | `~/.codex/config.toml` | <1s | Structured text |
| Windsurf | Full | MCP config | <1s | Structured text |
| Continue.dev | Full | MCP or custom context provider | <1s | Structured text |
| VS Code Copilot | Via VS Code | MCP in VS Code | <1s | Structured text |
| Aider | None | Precomputed context file | <30s (precompute) | Natural language |
| SWE-agent | None (extensible ACI) | Custom tool + hooks | <100ms (hook) | JSON |
| OpenHands | None | Shell command / prompt injection | <5s | Natural language |
| Agentless | None | Precompute, inject into Phase 1 | <30s | Structured text |
| Devin | None (closed) | CLI in sandbox | <5s | Natural language |

### GT Must Support All Delivery Surfaces

```
gt-mcp-server       → MCP clients (Claude Code, Cursor, Codex, Windsurf, Continue)
gt-cli              → Terminal agents (bash: gt understand <file>)
gt-python-library   → Agent frameworks (import groundtruth; gt.analyze(repo))
gt-precompute       → Pipeline agents (run once, inject into prompt)
gt-hook             → Scaffold integration (PostToolUse pattern)
```

Same analysis engine. Multiple delivery surfaces. MCP covers ~60% of the market. CLI + precompute covers the rest.

### Priority Integration Order

1. **MCP server** (already built): Covers Claude Code, Cursor, Copilot, Cline, Continue, Codex CLI, Windsurf
2. **PostToolUse hooks for Claude Code**: Already proven in SWE-bench experiments. Transparent — agent never explicitly calls GT
3. **Python library for pipeline agents**: `from groundtruth import find_relevant, validate, trace`. Agentless/MASAI/Kimi-Dev call during localization
4. **CLI for SWE-agent ACI**: Register GT commands alongside SWE-agent's linter (same PostToolUse pattern)
5. **Prompt injection for sandboxed agents**: Pre-computed context for OpenHands/Devin (proven in v8 wrapper)

---

## 3. Deterministic Cross-File Signals — Capability Matrix

### Industry Systems Comparison

| System | Cross-File Facts | Deterministic | Language-Agnostic | Scale | GT Applicability |
|---|---|---|---|---|---|
| **Kythe** (Google) | Defs, refs, call graph triples, types, containment, cross-lang links | Yes | Schema yes, indexers per-lang | Google monorepo | High — validates GT's schema; call graph triple model directly adoptable |
| **Glean** (Meta) | Declarations, xrefs, containment, inheritance, imports/exports via codemarkup | Yes | Abstraction layer yes, indexers per-lang | Meta scale | High — derived predicates concept (computed views) is useful |
| **SCIP** (Sourcegraph) | Defs, refs, hover, implementation relationships, cross-lang symbol IDs | Yes | Format yes, indexers per-lang | Enterprise multi-repo | Very high — GT could consume SCIP indexes as alternative to live LSP |
| **Stack Graphs** (GitHub) | Name resolution paths, scope structure, cross-file import/export edges | Yes | Algorithm yes, rules per-lang | GitHub scale (file-incremental) | Moderate — GT already does name resolution via LSP; file-incrementality concept valuable |
| **Static Call Graphs** | Caller/callee edges (CHA, RTA, VTA precision levels) | Yes | No — deeply per-language | Moderate | Low directly (breaks language-agnostic); GT gets similar data from LSP |
| **Test Impact** (Datadog/MS) | Test-to-source file mapping via coverage or import graph | Yes | Static variant is language-agnostic | Large test suites | Very high — GT already has import graph; natural new tool |
| **Clone Detection** (SourcererCC) | Similar code blocks across files | Yes | Token-based mostly agnostic | 250 MLOC | Moderate — precedent mining for briefings |
| **Git Signals** (code-maat) | Temporal coupling, churn hotspots, ownership, recency | Yes | Completely agnostic | Any repo | Very high — zero new dependencies, enriches risk scoring |

### What's Extractable Per Signal

| Cross-File Signal | Deterministic? | Language-Portable? | Scales? | GT Has It? | Priority |
|---|---|---|---|---|---|
| Static call graph (from LSP refs) | Yes | Yes | Yes | Partial (refs table) | HIGH |
| Test-to-source mapping | Yes | Yes (convention+import) | Yes | No | HIGH |
| Change impact propagation | Yes | Yes (call graph based) | Yes | Partial (trace) | HIGH |
| Git temporal coupling | Yes | Completely agnostic | Yes | No | HIGH |
| Git churn hotspots | Yes | Completely agnostic | Yes | No | MEDIUM |
| Convention/norm mining | Yes | Yes (string analysis) | Yes | Partial (risk_scorer) | MEDIUM |
| Similar code detection | Yes | Yes (AST/token vectors) | Yes | No | MEDIUM |
| Class hierarchy | Yes | Yes (LSP typeHierarchy) | Yes | Minimal | MEDIUM |
| Git blame/ownership | Yes | Completely agnostic | Yes | No | LOW |
| Data flow analysis | Partially | Partially | Partially | No | LOW |

### Key Insight from Kythe/Glean/SCIP

All three systems validate GT's core architecture: **language-neutral schema over per-language analysis, stored in a queryable database, with cross-file reference tracking.** The schema patterns are nearly identical:

- Kythe: `anchor → defines/binding → semantic node`, `anchor → ref/call → callee`
- Glean: `codemarkup.FileEntityXRefLocations` maps references to declarations
- SCIP: `Document → Occurrence → SymbolInformation`
- **GT**: `symbols → refs → exports`

GT is on the right track architecturally. The systems at Google/Meta/Sourcegraph scale prove this pattern works.

---

## 4. The Ideal Context Format

### What Research Says About Decision-Changing Context

**CrossCodeEval (NeurIPS 2023):** Models fail most when needed context is 2+ hops away in the import graph. Providing the *right* 5 files matters far more than 20 random files. Import statements alone are insufficient — models need *signatures* of imported symbols.

**CodeRAG-Bench:** Oracle (perfect) context: StarCoder2 goes from 31.7% → 94.5% on HumanEval (+62.8 points). Retrieved context: same model reaches only 43.9% with BM25. **Current retrieval captures ~20% of oracle's potential.** Optimal: 5 documents. More introduces noise.

**RepoCoder (ICLR 2024):** Iterative retrieval-generation beats single-pass by 12-15%. First generation surfaces what symbols the model *wants* to use, then retrieval corrects against reality.

**SWE-bench analyses:** Success correlates with: (1) correct file localization, (2) understanding existing error handling pattern, (3) knowing which tests exercise the code, (4) understanding type signatures of called functions.

**Google static analysis research:** "All widely deployed static analyses at Google are relatively simple." Complex analyses have low adoption. **Keep checks simple and fast.**

**Meta's Infer:** Switching from batch to diff-time analysis rocketed fix rate from ~20% to 70%+. **Real-time validation (GT's approach) is the right model.**

### Top 10 Cross-File Facts That Change Agent Decisions

Ranked by impact on correctness:

1. **Correct import paths** — exact module path for each symbol. Eliminates #1 hallucination type.
2. **Function signatures with types** — parameter names, types, optionality, return type. Prevents wrong calls.
3. **Error/exception hierarchy** — which errors exist, which functions raise them, expected catch pattern.
4. **Existing patterns in target file** — how the file currently handles similar operations.
5. **Constraints and "DO NOTs"** — things that look right but are wrong. Re-exports that exclude symbols. Deprecated APIs.
6. **Test file locations** — which test file exercises this code, what assertion patterns used.
7. **Related callers** — who else calls the function being modified, usage patterns, blast radius.
8. **Type definitions** — shape of data objects being passed (User, Token, etc.). Prevents field hallucination.
9. **Package availability** — what's actually installed. Prevents phantom dependencies.
10. **Convention signals** — naming, async/sync, ORM pattern. One example > 100 words.

### The Ideal 15-Line GT Output

Task: "fix password reset token invalidation when user changes email in Django"

```
IMPORTS: PasswordResetToken is in myapp.auth.tokens (NOT django.contrib.auth.tokens)
SIGNATURE: PasswordResetToken.invalidate_for_user(user: User) -> None
SIGNATURE: User.update_email(new_email: str, verified: bool = False) -> None
RAISES: User.update_email raises EmailAlreadyExistsError (from myapp.auth.exceptions)
PATTERN: All token invalidation calls .invalidate_for_user() then .save() — never .delete()
CALLER: update_email called from UserProfileView.patch (line 47) and EmailChangeView.post (line 23)
TYPE: PasswordResetToken has fields: user (FK->User), token (str), created_at (datetime), is_valid (bool)
CONSTRAINT: Token model uses soft-delete (is_valid=False), never hard delete — see migration 0047
TEST: tests/test_auth/test_tokens.py has test_token_invalidated_on_email_change (@skip("TODO"))
RELATED: SessionToken.invalidate_for_user() follows same pattern — myapp.auth.tokens line 89
DO NOT: Do not import from django.contrib.auth.tokens — project overrides with custom model
CONVENTION: All model methods return None and raise on failure (never return bool)
PACKAGE: django-rest-framework installed (use rest_framework.response.Response)
SIGNAL: post_save signal on User model exists in myapp.auth.signals — check for conflict
ERROR HIERARCHY: AuthError -> TokenError -> InvalidTokenError (all in myapp.auth.exceptions)
```

**Why this works:**
- Each line has a category label (IMPORTS, SIGNATURE, PATTERN) — structured headers models parse well
- Each line is one atomic fact — no information buried in paragraphs
- Includes negative constraints (DO NOT, CONSTRAINT) — prevents most common mistakes
- Total: ~250 tokens. Within the "short effective context" sweet spot
- Every fact is actionable — directly changes what the agent writes
- **NONE of it comes from reading one file. ALL is cross-file intelligence.**

---

## 5. Architecture for Any Language at Any Scale

### The LSP Scaling Problem (Critical Architecture Decision)

GT's current architecture uses live LSP queries for indexing. Research confirms this won't scale. **codebase-memory-mcp proves the alternative works:** tree-sitter indexes Linux kernel (28M LOC) in 3 minutes. Sourcegraph's two-tier model (imprecise search for 500+ languages, precise SCIP for ~15) validates the hybrid approach.

| Scale | Files | LSP Index Time | Tree-sitter Index Time | SQLite Query |
|---|---|---|---|---|
| Small | 1K | 1-5 min | <5s | <5ms |
| Medium | 10K | 10-30 min | ~30s | <10ms |
| Large | 100K | Hours (impractical) | ~5 min | <50ms |
| Huge | 1M | Not feasible | ~30 min | <200ms |

**LSP servers are designed for interactive editing, not batch indexing.** Every production system at scale (Sourcegraph, Kythe, Glean, GitHub) uses batch/CI-time indexing, not runtime LSP.

### The Hybrid Architecture

```
Tier 1: Tree-sitter (fast, 80% accurate)
  - Parse every file → AST
  - Extract symbols (names, kinds, locations, signatures)
  - Extract imports (regex + convention-based resolution)
  - Build initial refs table
  - Time: <5s for 1K files, ~30s for 10K files

Tier 2: Heuristic Enhancement (fast, +10% accuracy)
  - Convention-based import resolution (foo.bar → foo/bar.py)
  - Test file mapping (convention + import analysis)
  - Norm mining (statistical patterns from symbol names)
  - Git signal extraction (temporal coupling, churn, ownership)
  - Time: <5s additional

Tier 3: LSP/SCIP (slow, 100% accurate — optional)
  - Live LSP queries for specific symbols on demand
  - SCIP index consumption if available from CI
  - Fills in: exact type resolution, overload resolution, generic instantiation
  - Time: per-query (<200ms) or per-CI-run (minutes)
```

### SQLite Performance Validation

SQLite handles GT's scale comfortably:

- **Billions of rows** tested, B-tree implementation is mature
- **Point lookups**: <1ms regardless of table size (with index)
- **Write speed**: 50K-100K inserts/sec with WAL, 500K+ with tuning
- **FTS5**: 1-10ms for typical queries over millions of documents
- **Graph traversal** (recursive CTE): <50ms for depth-3 at 100K files

Critical pragmas:
```sql
PRAGMA journal_mode = WAL;          -- concurrent reads during writes
PRAGMA synchronous = NORMAL;        -- 10x faster writes
PRAGMA cache_size = -64000;         -- 64MB page cache
PRAGMA mmap_size = 268435456;       -- 256MB mmap
PRAGMA temp_store = MEMORY;         -- temp tables in RAM
```

**SQLite is correct through 100K files.** PostgreSQL only needed for concurrent multi-user or >10GB indexes.

### Incremental Reindexing Strategy

```
On file save (~20ms):
  1. Hash content → skip if unchanged
  2. Tree-sitter parse (~1ms)
  3. Extract symbols + refs (~1ms)
  4. DELETE + INSERT for that file

On git pull (~5s for 200 changed files):
  1. git diff --name-only → changed files
  2. Batch reindex changed files
  3. Recompute norms for affected directories

On first clone (30s-5min):
  1. Full tree-sitter reindex
  2. Progress bar — user expects to wait

Key metric: reindex must complete before agent's next query (~5-30s gap)
```

### What Breaks at Scale

1. **Cross-file reference resolution** — tree-sitter can't resolve `from foo.bar import baz` to actual file/symbol. Need language-specific heuristics (80% accurate) or compiler (100%). Heuristics sufficient for hallucination prevention.
2. **Full reindex time at 100K+** — tree-sitter parsing + extraction + graph could hit 5-10 min. Need content-hash incremental from day one.
3. **Memory during indexing** — 100K files of symbols in memory: 1-4GB. Need streaming/batching.
4. **Norm mining at scale** — analyzing distributions across whole codebase is batch analytics. Compute once, cache, refresh periodically.

---

## 6. Competitive Landscape

### Direct Competitors

| Competitor | Approach | Weakness GT Exploits |
|---|---|---|
| **Augment Context Engine** ($252M) | Semantic embeddings, 400K+ files, MCP server | Cloud-dependent, closed-source, retrieval not validation |
| **Cursor codebase index** | Embeddings in Turbopuffer, bi-encoder → reranker | Tied to Cursor IDE, 10-30s latency, syntax not semantics |
| **Sourcegraph Cody/SCIP** | Compiler-grade SCIP + Zoekt text search | Requires CI pipeline for SCIP, heavy infrastructure |
| **Continue.dev** | Local embeddings + tree-sitter + ripgrep | Embedding-based, lower quality than compiler-grade |
| **Greptile** | Full repo graph, learns from PR reactions | Embeddings not LSP, no validation layer |
| **Codegen** (YC W25) | Codebase-as-graph, Python SDK | Python/JS/Go only, no MCP, migrations-focused |
| **Factory AI HyperCode** | Multi-resolution graph + latent space | Proprietary, locked inside Factory pipeline |

### Direct MCP Competitors (NEW — Critical Intel)

**codebase-memory-mcp (DeusData)** — **Most serious direct competitor:**
- Single static binary, zero dependencies, 66 languages via tree-sitter
- Persistent knowledge graph (in-memory SQLite + LZ4)
- Call graph resolution across files, architecture overviews, git diff impact mapping
- Indexes Linux kernel (28M LOC) in 3 minutes, sub-ms queries
- Claims 99% fewer tokens vs file-by-file search (412K tokens → 3.4K)
- **What it lacks:** No proactive briefing. No code validation. No AI-powered semantic resolution. No hallucination prevention. Read-only index, not an intelligence layer.
- **GT differentiator:** LSP (compiler-grade types) vs tree-sitter (syntax only). GT validates code, briefs agents, and catches hallucinations. codebase-memory-mcp just answers questions about code structure.

**mcp-language-server (isaacphi)** — LSP proxy over MCP:
- Wraps any stdio LSP server, exposes definition/references/diagnostics/hover
- **No persistence.** No indexing. No graph. Every query hits the LSP server cold.
- GT differentiator: GT indexes into SQLite for instant queries + adds AI briefing + validation + trace.

**Other MCP code intelligence servers:** Code Pathfinder (Python-only AST), mcp-codebase-index (18 query tools), code-graph-mcp, CodeMCP. All early, none with GT's proactive+reactive architecture.

**Greptile** — $25M Series A, building MCP server (upcoming):
- AI code review with full codebase context, used by PostHog/Raycast
- If they ship agent-facing validation, they become a direct competitor with significant funding.

### GT's Unique Position

**No one else does both PROACTIVE (briefing before code generation) and REACTIVE (validation after).** Augment does retrieval. Cursor does retrieval. Sourcegraph does search + navigation. codebase-memory-mcp does read-only indexing. GT does ANALYSIS + VALIDATION — computed facts about cross-file relationships that no retrieval system can produce.

### MCP Ecosystem Context (Updated)

- **97 million monthly SDK downloads** (Feb 2026), **10,000+ public MCP servers** (MCP.so: 19,075)
- Donated to Linux Foundation's Agentic AI Foundation (Dec 2025) — Google, OpenAI, Anthropic all committed
- Adopted by: Anthropic, OpenAI, Google DeepMind, Cursor, Windsurf, VS Code, Zed, JetBrains
- Major registries: MCP.so, GitHub MCP Registry, Docker MCP Catalog (270+), Cline marketplace, mcpmarket.com, glama.ai/mcp
- JFrog launched enterprise MCP Registry for governance/security
- **Less than 5% of MCP servers are monetized.** The commercial wave is just starting.
- Code intelligence is a nascent category — codebase-memory-mcp is the only serious entrant with traction

---

## 7. GT as a Product

### Market Data

- **AI code tools market:** $7.37B (2025), projected $23.97B by 2030 (26.6% CAGR)
- **Developer trust declining:** Only 29% trust AI output (Stack Overflow 2025, down 11 points from 2024)
- **84% of developers** use or plan to use AI tools — but 46% actively distrust accuracy
- **Copilot suggests wrong dependencies 15% of the time** (Microsoft research)
- **65% report AI "misses relevant context"** during refactoring (Qodo 2025)
- **IEEE Spectrum 2025:** AI coding quality declining — tasks that took 5 hrs with AI now take 7-8
- **Cursor valuation:** $9.9B (June 2025). GitHub Copilot: $400M revenue (248% YoY growth)

### Positioning Statement

"GroundTruth is the MCP server that stops AI coding agents from hallucinating. Install once — every agent gets compiler-grade codebase intelligence: correct imports, real signatures, actual patterns. Works with Claude Code, Cursor, Codex, Windsurf. Any language with an LSP server."

### Three Personas (Priority Order)

1. **Individual power developers using Claude Code / Cursor** (beachhead)
   - Feel import hallucination pain daily
   - pip install, one-line MCP config
   - Distribution: HN, Reddit, awesome-mcp, Claude Code docs

2. **Engineering teams (5-50 devs)**
   - Team-wide `.mcp.json` in repo
   - Consistency across the team's AI usage
   - $10-20/dev/month

3. **Enterprise (50+ devs, large monorepos)**
   - Governance: ensure AI-generated code meets standards
   - $30-50/dev/month, enterprise contracts

### Distribution Strategy

1. **Phase 1 (now):** Open source, `pip install groundtruth-mcp`, one-line Claude Code config
2. **Phase 2:** MCP registry / marketplace — #1 "code intelligence" server
3. **Phase 3:** IDE-native — Cursor, Windsurf, Codex all support MCP
4. **Phase 4:** Enterprise SaaS — dashboard, team stats, CI integration

### Monetization

- **Free:** Open source core, local, single project, deterministic tools (trace, validate without AI, find_relevant without AI)
- **Pro ($15/mo):** AI briefing + semantic resolution (Haiku calls), unlimited symbols
- **Team ($20/seat/mo):** Shared index, team analytics, convention enforcement
- **Enterprise:** Self-hosted, SSO, audit logs

### Competitive Moat

1. **LSP > tree-sitter** for accuracy — compiler-grade types, not just names
2. **MCP-native, agent-agnostic** — not locked to one vendor
3. **Proactive + reactive** — no one else does both briefing AND validation
4. **Accumulated hallucination data** — risk scoring improves with usage
5. **Zero language-specific code** — one config line per language

### Key Risks

1. **codebase-memory-mcp** could add an AI layer. Their tree-sitter approach is faster to index but less accurate.
2. **Greptile** ($25M) is building an MCP server. If they ship agent-facing validation, they're a funded direct competitor.
3. **Context windows growing** — if agents ingest entire codebases into 10M+ windows, "find relevant files" weakens. Validation and briefing remain strong regardless.
4. **Claude Code, Cursor, etc. could build this in-house.** Mitigant: they're AI companies, not code intelligence companies. 50+ language LSP support is not their core competency.

---

## 8. Top 5 Signals for GT v9

Ranked by: impact x extractability x portability x scale

### Signal 1: Test-to-Source Mapping with Coverage Gap Detection

**Impact:** HIGH — "test_tokens.py has 12 tests but no test for invalidation on email change" directly tells the agent what test to write.

**What it provides that reading one file can't:** Which tests exercise which symbols, and what's NOT tested.

**How computed:** Import graph analysis — query refs table for symbols referenced from files matching `test_*`, `*_test.*`, `*_spec.*`. Coverage gaps detected by diffing tested symbols against all exported symbols.

**Output example:**
```
TEST: tests/test_tokens.py covers PasswordResetTokenGenerator (12 tests)
GAP: No test for token invalidation on email change
```

**Deterministic?** Yes. **Language-portable?** Yes (convention + import analysis). **Lines of code:** ~200.

### Signal 2: Git Temporal Coupling + Churn Hotspots

**Impact:** HIGH — Files that always change together should appear in `find_relevant` even without direct symbol dependency. Churn hotspots get enhanced briefings.

**What it provides:** Hidden coupling that pure static analysis misses. "tokens.py and signals.py change together 80% of the time — check signals.py too."

**How computed:** `git log --name-only` → co-change frequency matrix. Churn = commits per file in time window. Zero new dependencies.

**Output example:**
```
CO-CHANGE: tokens.py and signals.py change together (80% coupling, last 3 months)
HOTSPOT: auth/tokens.py — 14 commits in 30 days, high churn
```

**Deterministic?** Yes. **Language-portable?** Completely (git only). **Lines of code:** ~150.

### Signal 3: Enhanced Call Graph with Usage Classification

**Impact:** HIGH — Not just "47 callers" but "47 callers destructure return as tuple — don't change return shape." Converts raw ref count into behavioral constraint.

**What it provides:** HOW callers use a symbol, not just THAT they reference it. This is the difference between "impact count" and "behavioral contract."

**How computed:** For each caller (from refs table), extract the surrounding context lines, classify usage pattern (destructuring, error handling, null check, type assertion). Statistical aggregation across all callers.

**Output example:**
```
CALLERS: 47 callers of getUserById — 38 destructure as (user, err), 9 ignore error
CONTRACT: All callers expect tuple return; 81% guard against None
```

**Deterministic?** Yes (regex/AST pattern matching on call sites). **Language-portable?** Mostly (patterns differ per language). **Lines of code:** ~300.

### Signal 4: Sibling Function Discovery (Precedent Mining)

**Impact:** MEDIUM-HIGH — "There's a similar function at utils/auth.py:45 — follow that pattern" gives the agent a concrete precedent instead of inventing from scratch.

**What it provides:** The nearest existing implementation that does something analogous. Reduces hallucination by anchoring generation in real code.

**How computed:** Find functions in the same file/directory/module with similar signatures (same parameter count, similar parameter names, similar return type). Levenshtein on parameter names + structural similarity.

**Output example:**
```
PRECEDENT: getPostById (same file, line 89) follows the same pattern — throw NotFoundError, return single entity
```

**Deterministic?** Yes. **Language-portable?** Yes (signature comparison is language-agnostic after LSP extraction). **Lines of code:** ~200.

### Signal 5: Constraint Framing (Impact + Criticality Context)

**Impact:** MEDIUM-HIGH — Not just "14 references" but "auth critical path, all password resets depend on this." Converts raw numbers into risk assessment.

**What it provides:** The BUSINESS context of a symbol's importance. Critical path identification from the call graph topology.

**How computed:** Graph analysis — symbols on paths from entry points (routes, handlers, main) to security/auth/payment modules get "critical path" classification. Combined with ref count and churn data.

**Output example:**
```
CRITICAL PATH: getUserById is on the auth critical path (login → session → getUserById)
BLAST RADIUS: Changes affect 5 files, 3 route handlers, 1 middleware
```

**Deterministic?** Yes. **Language-portable?** Yes (graph topology is language-agnostic). **Lines of code:** ~250.

---

## Recommendations — What to Build Next

### Immediate (This Sprint)

1. **Test-to-source mapping** — query existing refs table for test file references. Add to briefing output. ~200 LOC, highest impact for lowest effort.

2. **Git signal extraction** — `git log` parsing for temporal coupling and churn. Feed into `find_relevant` ranking and risk scoring. ~150 LOC, zero new dependencies.

### Next Sprint

3. **Sibling function discovery** — find similar implementations for the briefing's "PRECEDENT" line. ~200 LOC.

4. **Usage classification** — categorize how callers use a symbol's return value for the briefing's "CONTRACT" line. ~300 LOC.

### V9 Architecture Decision

5. **Hybrid indexing** — tree-sitter for fast extraction + LSP for semantic queries on demand + optional SCIP consumption. This is the path to scaling beyond 10K files while maintaining accuracy.

### Do NOT Build

- More error type detectors (diminishing returns)
- More norms from the same file (single-file intelligence is not GT's value)
- Embedding-based search (table stakes, every competitor does it)
- Web dashboard (premature)
- Multi-repo support (premature)

---

## The Context Stack — GT's Architectural Position

```
Layer 4: Adaptive/Learned    (Greptile: learn from PR reactions; GT: intervention history)
Layer 3: Semantic/AI         (briefings, semantic resolution — $0.003/call)
Layer 2: Graph/Analysis      (call graph, import graph, impact, constraints — deterministic, free)
Layer 1: Symbol/Compiler     (signatures, types, docs — deterministic, free)
Layer 0: Text/Embedding      (keyword search, embedding similarity — what everyone else does)
```

**GT starts at Layer 1 (compiler-grade) while competitors start at Layer 0 (text).** The path forward: strengthen Layers 2-3 with cross-file analysis signals, not by duplicating Layer 0 capabilities.

**The gap between oracle context and retrieved context is 62.8 percentage points (CodeRAG-Bench).** GT's compiler-grade index is closer to oracle than any embedding-based system for the signals that matter most. Be the oracle for deterministic signals. Use AI only for the remaining semantic gap.

---

## Sources

### Agent Architecture
- [Claude Code Tools Reference](https://code.claude.com/docs/en/tools-reference)
- [Claude Code LSP / Code Intelligence Plugins](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code with LSP: from searching text to understanding code](https://antoniocortes.com/en/2026/03/10/claude-code-with-lsp-from-searching-text-to-understanding-code/)
- [Aider Repo Map (tree-sitter)](https://aider.chat/2023/10/22/repomap.html)
- [SWE-agent Paper (NeurIPS 2024)](https://arxiv.org/abs/2405.15793)
- [OpenHands Software Agent SDK](https://arxiv.org/html/2511.03690v1)
- [Codex CLI Features](https://developers.openai.com/codex/cli/features)
- [How Cursor Indexes Your Codebase](https://towardsdatascience.com/how-cursor-actually-indexes-your-codebase/)
- [How Copilot Understands Your Workspace](https://code.visualstudio.com/docs/copilot/reference/workspace-context)
- [Augment Code Context Engine MCP](https://www.augmentcode.com/product/context-engine-mcp)
- [Devin 2.0 Explained](https://www.analyticsvidhya.com/blog/2025/04/devin-2-0/)
- [Factory Code Droid Technical Report](https://factory.ai/news/code-droid-technical-report)
- [Agentless Paper](https://arxiv.org/abs/2407.01489)

### Cross-File Signals
- [Kythe Schema Overview](https://kythe.io/docs/schema-overview.html)
- [Kythe Callgraphs](https://kythe.io/docs/schema/callgraph.html)
- [Indexing code at scale with Glean - Meta](https://engineering.fb.com/2024/12/19/developer-tools/glean-open-source-code-indexing/)
- [SCIP - a better code indexing format than LSIF](https://sourcegraph.com/blog/announcing-scip)
- [Introducing stack graphs - GitHub](https://github.blog/open-source/introducing-stack-graphs/)
- [PyCG: Practical Call Graph Generation in Python](https://arxiv.org/abs/2103.00587)
- [How Test Impact Analysis Works - Datadog](https://docs.datadoghq.com/tests/test_impact_analysis/how_it_works/)
- [The Rise of Test Impact Analysis - Martin Fowler](https://martinfowler.com/articles/rise-test-impact-analysis.html)
- [SourcererCC: Scaling Code Clone Detection](https://arxiv.org/abs/1512.06448)
- [code-maat (Adam Tornhill)](https://github.com/adamtornhill/code-maat)
- [Sourcegraph Architecture](https://sourcegraph.com/docs/admin/architecture)

### Research
- [CrossCodeEval (NeurIPS 2023)](https://arxiv.org/abs/2310.11248)
- [RepoCoder (ICLR 2024)](https://arxiv.org/html/2303.12570)
- [RepoHyper (FORGE 2025)](https://arxiv.org/html/2403.06095v1)
- [CodeRAG-Bench](https://arxiv.org/html/2406.14497v1)
- [CodePlan (FSE 2024)](https://arxiv.org/abs/2309.12499)
- [Lessons from Building Static Analysis Tools at Google - CACM](https://cacm.acm.org/magazines/2018/4/226371)
- [Scaling Static Analyses at Facebook - CACM](https://cacm.acm.org/research/scaling-static-analyses-at-facebook/)
- [Sourcegraph Cody Context Retrieval](https://arxiv.org/html/2408.05344v1)

### Product/Market
- [MCP Servers Are the New SaaS](https://dev.to/krisying/mcp-servers-are-the-new-saas-how-im-monetizing-ai-tool-integrations-in-2026-2e9e)
- [MCP Server Economics](https://zeo.org/resources/blog/mcp-server-economics-tco-analysis-business-models-roi)
- [Continue.dev Accuracy Limits](https://blog.continue.dev/accuracy-limits-of-codebase-retrieval/)
- [Qodo Context Engine](https://www.qodo.ai/blog/introducing-qodo-aware-deep-codebase-intelligence-for-enterprise-development/)
- [Greptile AI Code Review](https://www.greptile.com/)
