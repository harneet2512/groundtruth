# INITIAL_PROMPT.md — GroundTruth Build Kickoff

> Paste each build prompt into Claude Code one at a time. Run the checkpoint before moving on.

---

## Before You Start

1. Put `CLAUDE.md` and `PRD.md` in your project root.
2. Create an empty `PROGRESS.md`.
3. Make sure you have Python 3.11+ and these language servers installed:
   - `npm install -g typescript-language-server typescript` (for TS fixture)
   - `pip install pyright` (for Python fixture)
   - `go install golang.org/x/tools/gopls@latest` (for Go fixture — optional for MVP)

---

## Scaffold + LSP Client

**Paste this into Claude Code:**

```
Read CLAUDE.md and PRD.md in the project root.

Build: Project scaffold + LSP client.

1. Initialize the Python project:
   - Create pyproject.toml exactly as specified in CLAUDE.md
   - Create the full directory structure from CLAUDE.md (all __init__.py files, all directories)
   - Create .gitignore (Python + SQLite + .groundtruth/)
   - pip install -e ".[dev]"

2. Build the LSP client (src/groundtruth/lsp/):
   - config.py: LSP_SERVERS dict mapping extensions to LSPServerConfig(command, language_id). Include: .py, .ts, .tsx, .js, .go, .rs
   - protocol.py: Pydantic models for LSP types: DocumentSymbol, SymbolKind, Position, Range, Location, TextDocumentIdentifier, InitializeParams, InitializeResult
   - client.py: AsyncLSPClient class
     - Spawns a subprocess via asyncio.create_subprocess_exec
     - Sends JSON-RPC requests over stdin, reads responses from stdout
     - Methods: initialize(), shutdown(), document_symbol(uri), references(uri, position), hover(uri, position), definition(uri, position), did_open(uri, text, language_id), did_change(uri, text)
     - Request ID tracking, timeout handling (5s default)
     - Content-Length header framing per LSP spec
   - manager.py: LSPManager class
     - Spawns one LSP server per language detected in the project
     - Routes requests to the correct server based on file extension
     - Handles server crashes (log + restart)
     - Graceful shutdown

3. Write unit tests (tests/unit/test_lsp_client.py):
   - Test JSON-RPC message framing (Content-Length headers)
   - Test request/response matching by ID
   - Test timeout handling
   - Mock subprocess — don't spawn real LSP servers in unit tests

4. Update PROGRESS.md with what was built and any decisions made.

Do NOT build: indexer, store, validators, AI, MCP server, CLI. Only the LSP client layer.
```

**Checkpoint:**
```bash
cd groundtruth
pip install -e ".[dev]"
pytest tests/unit/test_lsp_client.py -v
mypy src/groundtruth/lsp/ --strict
```

---

## SQLite Store + Indexer

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: SQLite store + indexer that uses the LSP client.

1. Build the SQLite store (src/groundtruth/index/):
   - schema.sql: exact schema from CLAUDE.md (symbols, exports, packages, refs, interventions, FTS5)
   - store.py: SymbolStore class
     - init_db(): create tables from schema.sql
     - CRUD: insert_symbol, insert_export, insert_package, insert_ref, insert_intervention
     - Queries: get_symbol_by_name, get_exports_by_module, get_refs_for_symbol, get_imports_for_file, get_importers_of_file, search_symbols_fts
     - All queries use parameterized statements
     - Support both file-backed and in-memory SQLite (for tests)

2. Build the indexer (src/groundtruth/index/indexer.py):
   - Indexer class that takes an LSPManager and SymbolStore
   - index_project(root_path): scan files → group by language → for each file, call LSP → store results
   - index_file(file_path): single file re-index
   - _parse_document_symbols(): convert LSP DocumentSymbol[] into our Symbol model
   - _extract_exports(): determine which symbols are exported (use LSP definition/references to infer visibility)
   - _parse_package_manifest(): read package.json / requirements.txt / go.mod → store packages
   - Handle: file not found, LSP server not available for this language, empty files

3. Build utility modules:
   - src/groundtruth/utils/logger.py: structlog setup
   - src/groundtruth/utils/levenshtein.py: levenshtein_distance(a, b) and find_closest(target, candidates, max_distance=3)
   - src/groundtruth/utils/watcher.py: FileWatcher using watchdog or polling — detects changes, calls indexer.index_file()

4. Write tests:
   - tests/unit/test_store.py: all CRUD + queries with in-memory SQLite
   - tests/unit/test_indexer.py: mock LSP responses, verify correct SQLite records created
   - tests/unit/test_levenshtein.py: distance calculations, find_closest

5. Update PROGRESS.md.

Do NOT build: validators, AI, MCP server, CLI, graph traversal. Only store + indexer.
```

**Checkpoint:**
```bash
pytest tests/unit/test_store.py tests/unit/test_indexer.py tests/unit/test_levenshtein.py -v
mypy src/groundtruth/index/ src/groundtruth/utils/ --strict
```

---

## Graph Traversal + Validators

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: Import graph traversal + all validators.

1. Build graph traversal (src/groundtruth/index/graph.py):
   - ImportGraph class with SymbolStore dependency
   - find_connected_files(entry_files, max_depth=3): BFS over refs + exports tables. Returns FileNode(path, distance, symbols_involved, reason)
   - find_callers(symbol_name): all files/lines referencing this symbol
   - find_callees(symbol_name, file_path): all symbols this function references
   - get_impact_radius(symbol_name): total files affected
   - All pure SQLite. No AI.

2. Build validators (src/groundtruth/validators/):
   - import_validator.py: parse imports from code string (regex-based for Python, TS, Go patterns), check each against store. Return errors with Levenshtein suggestions.
   - package_validator.py: extract package imports, check against packages table.
   - signature_validator.py: extract function calls, check arg count against stored signatures.
   - orchestrator.py: ValidationOrchestrator runs all three, merges results. Escalation chain: deterministic → Levenshtein → cross-index (search by name across all files) → mark as "needs_ai"

3. Write tests:
   - tests/unit/test_graph.py: BFS from entry file finds correct connected files. Callers/callees correct. Impact radius correct. Use pre-populated in-memory SQLite.
   - tests/unit/test_validators.py: correct code passes, wrong imports caught, missing packages caught, wrong signatures caught, Levenshtein suggestions correct, cross-index resolution works.

4. Update PROGRESS.md.

Do NOT build: AI layer, MCP server, CLI.
```

**Checkpoint:**
```bash
pytest tests/unit/test_graph.py tests/unit/test_validators.py -v
mypy src/groundtruth/index/graph.py src/groundtruth/validators/ --strict
```

---

## AI Layer

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: AI components — task parser, briefing engine, semantic resolver.

1. Build prompts (src/groundtruth/ai/prompts.py):
   - TASK_PARSER_SYSTEM: "Extract symbol names from this task description. Return JSON array of strings. Nothing else."
   - BRIEFING_SYSTEM: "Given these codebase symbols and their signatures, write a compact briefing (<200 tokens) for a developer about to work on this task. Focus on: which functions to use, which files matter, which patterns to follow, common mistakes to avoid."
   - SEMANTIC_RESOLVER_SYSTEM: "The developer wrote code that references a symbol that doesn't exist. Given the error, the surrounding code context, and the list of available symbols from the codebase, determine what the developer likely intended. Return the correct symbol name and import path."

2. Build task parser (src/groundtruth/ai/task_parser.py):
   - parse_task(description, anthropic_client?) -> list[str]
   - If client provided: call Haiku with TASK_PARSER_SYSTEM
   - If no client: fallback — split on camelCase/snake_case boundaries, filter stop words
   - Always returns a list of candidate symbol names

3. Build briefing engine (src/groundtruth/ai/briefing.py):
   - generate_briefing(intent, store, anthropic_client) -> BriefingResult
   - Step 1: extract keywords from intent (deterministic)
   - Step 2: FTS5 query against symbols_fts
   - Step 3: enrich results with signatures, file paths, docs from store
   - Step 4: send to Haiku with BRIEFING_SYSTEM
   - Return: briefing text + relevant_symbols + warnings

4. Build semantic resolver (src/groundtruth/ai/semantic_resolver.py):
   - resolve(error, code_context, available_symbols, anthropic_client) -> ResolutionResult
   - Sends error + context + candidates to Haiku
   - Returns: suggested symbol + import path + confidence + reasoning
   - Only called when deterministic methods exhausted

5. Write tests (tests/unit/test_task_parser.py, test_briefing.py, test_semantic_resolver.py):
   - Mock Anthropic client — return predefined responses
   - Test task parser with and without API key
   - Test briefing produces relevant output given known symbols
   - Test semantic resolver picks correct symbol from candidates

6. Update PROGRESS.md.

Do NOT build: MCP server, CLI.
```

**Checkpoint:**
```bash
pytest tests/unit/test_task_parser.py tests/unit/test_briefing.py tests/unit/test_semantic_resolver.py -v
mypy src/groundtruth/ai/ --strict
```

---

## MCP Server

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: MCP server with all 8 tools wired end-to-end.

1. Build MCP tools (src/groundtruth/mcp/tools.py):
   - Define all 8 tools with exact schemas from CLAUDE.md:
     - groundtruth_find_relevant
     - groundtruth_brief
     - groundtruth_validate
     - groundtruth_trace
     - groundtruth_dead_code (one SQL query: exported symbols with zero references)
     - groundtruth_unused_packages (one SQL query: packages not referenced in any import)
     - groundtruth_hotspots (one SQL query: symbols ordered by usage_count DESC)
     - groundtruth_status
   - Each tool handler: validate input (Pydantic), call the appropriate components, format response
   - The three new analysis tools (dead_code, unused_packages, hotspots) are pure SQL queries on the existing store — no new components needed, just new methods on SymbolStore

2. Build MCP server (src/groundtruth/mcp/server.py):
   - Use the mcp Python SDK (stdio transport)
   - Register all 8 tools
   - On startup: initialize LSP manager, run indexer, open SQLite store
   - Handle tool calls by routing to handlers in tools.py

3. Build entry point (src/groundtruth/main.py):
   - CLI using argparse or click:
     - `groundtruth serve` — start MCP server (stdio)
     - `groundtruth index <path>` — index a project
     - `groundtruth status` — show index stats
     - `groundtruth validate <file>` — validate a single file

4. Wire the full pipeline for each tool:
   - find_relevant: task_parser → store.search → graph.find_connected_files → format response
   - brief: briefing_engine.generate_briefing → format response
   - validate: orchestrator.validate → (if needs_ai) semantic_resolver.resolve → format response
   - trace: graph.find_callers + graph.find_callees + graph.get_impact_radius → format response
   - dead_code: store.get_dead_code() → format response
   - unused_packages: store.get_unused_packages() → format response
   - hotspots: store.get_hotspots(limit) → format response
   - status: store stats + LSP server status → format response

5. Write integration tests (tests/integration/):
   - test_mcp_server.py: spawn server, send MCP requests, verify responses
   - Use the TypeScript fixture project with a real typescript-language-server

6. Update PROGRESS.md.
```

**Checkpoint:**
```bash
pytest tests/ -v
mypy src/ --strict
# Test MCP manually:
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python -m groundtruth.main serve
```

---

## Fixture Projects + Cross-Language Tests

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: All three fixture projects + cross-language integration tests.

1. Create TypeScript fixture project (tests/fixtures/project_ts/) — exact structure from PRD section 5.1. Every function should have a real implementation (not just stubs). Include deliberate confusion points.

2. Create Python fixture project (tests/fixtures/project_py/) — exact structure from PRD section 5.2. Same logical structure as TS but with Python conventions (__init__.py, snake_case, type hints).

3. Create Go fixture project (tests/fixtures/project_go/) — exact structure from PRD section 5.3. Same logical structure with Go conventions (capitalized exports, error returns).

4. Write cross-language integration tests (tests/integration/):
   - For each fixture project, test:
     - Indexing produces correct symbol count
     - find_relevant("fix getUserById/get_user_by_id/GetUserByID") returns correct files
     - trace("getUserById"/"get_user_by_id"/"GetUserByID") returns correct callers
     - validate with wrong imports catches errors
     - validate with correct code passes
   - The SAME test logic should work across all three languages — proving language-agnosticism

5. Update PROGRESS.md.
```

**Checkpoint:**
```bash
pytest tests/integration/ -v --timeout=60
# Should pass for at least TypeScript fixture. Python and Go depend on having those LSP servers installed.
```

---

## Benchmarks + README

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: Hallucination benchmark + file relevance benchmark + README.

1. Create hallucination benchmark cases (benchmarks/hallucination_cases/):
   - 100 JSON files across categories specified in PRD section 6.3
   - 40 TypeScript, 35 Python, 25 Go
   - Each case has: id, language, category, input, expected

2. Create file relevance benchmark cases (benchmarks/):
   - 20 JSON files as specified in PRD section 6.4

3. Build benchmark runner (benchmarks/runner.py):
   - Load all cases
   - For each: index fixture project → run tool → compare to expected
   - Report: detection rate, fix rate (deterministic vs AI), false positives, briefing coverage, latency
   - Output: JSON results + human-readable summary

4. Write README.md:
   - One-paragraph description: what it does, why it exists
   - Architecture diagram (ASCII)
   - Quick start: pip install, configure MCP client (Claude Code, Cursor, Codex)
   - The 5 tools with example inputs/outputs
   - Benchmark results table
   - How it's different from SymDex (the table from CLAUDE.md)
   - Contributing guide

5. Write docs/architecture.md:
   - Why LSP over tree-sitter
   - The three-phase flow (find → brief → validate)
   - Where AI is used and why
   - How to add a new language (one config line)

6. Update PROGRESS.md.
```

**Checkpoint:**
```bash
python benchmarks/runner.py --fixture project_ts
# Should output benchmark results
```

---

## SWE-bench Evaluation (Stretch)

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: SWE-bench evaluation harness.

This is a stretch goal. Only attempt if all previous sections are complete and passing.

1. Build harness (benchmarks/swe_bench/harness.py):
   - Load SWE-bench Pro tasks (from HuggingFace dataset)
   - Filter for TypeScript/JavaScript/Python tasks
   - For each task:
     a. Clone the repo into a temp directory
     b. Index with GroundTruth
     c. Run a simple agent scaffold that uses GroundTruth MCP tools
     d. Apply the generated patch
     e. Run the task's test suite
     f. Record: resolved, tokens_used, files_read, tools_called

2. Build comparison:
   - Run each task with and without GroundTruth
   - Record deltas

3. Output results to benchmarks/swe_bench/results/

4. Update PROGRESS.md and README with results.
```

---

## Research Layer: Grounding Gap + Risk Scoring + Adaptive Briefing

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: All three research layers — grounding gap measurement, risk scoring, adaptive briefing.

IMPORTANT: Only build this after the core tool, fixtures, and benchmarks are complete and passing.

1. Add briefing_logs table to schema.sql (see CLAUDE.md Research Layers section).
   Update store.py with: insert_briefing_log, get_briefing_log, link_briefing_to_validation

2. Update the briefing engine (src/groundtruth/ai/briefing.py):
   - After generating a briefing, log it to briefing_logs with all symbols included
   - Store the briefing_id so it can be linked to subsequent validation

3. Update the validation orchestrator (src/groundtruth/validators/orchestrator.py):
   - After validation, check if there's a recent briefing_log for the same file
   - If yes, link them: set subsequent_validation_id on the briefing_log
   - Compute compliance_rate: compare briefing symbols vs validation output

4. Build grounding gap analysis (src/groundtruth/analysis/grounding_gap.py):
   - GroundingGapAnalyzer class
   - compare_briefing_to_output(briefing_log, validation_result) → GroundingResult
   - aggregate_compliance(all_results) → GroundingReport with averages, breakdowns by category
   - CLI command: groundtruth analyze-grounding → prints report

5. Build risk scorer (src/groundtruth/analysis/risk_scorer.py):
   - RiskScorer class that takes a SymbolStore
   - score_file(path) → RiskScore with factors: naming_ambiguity, import_depth, convention_variance, overloaded_paths, parameter_complexity, isolation_score
   - score_codebase() → list[RiskScore] ranked by overall risk
   - All computed from SQLite queries. Zero AI.
   - CLI command: groundtruth risk-map → prints top 20 riskiest files/symbols

6. Build adaptive briefing (src/groundtruth/analysis/adaptive_briefing.py):
   - AdaptiveBriefing class that takes RiskScorer + SymbolStore
   - enhance_briefing(base_briefing, target_file) → enhanced BriefingResult
   - If naming_ambiguity > 0.5: add exact import paths
   - If import_depth > 2: add re-export chain
   - If past failures exist for this file: add negative examples
   - Wire into briefing engine: after base briefing, run adaptive enhancement

7. Write tests:
   - test_grounding_gap.py: known briefing + known validation → correct compliance
   - test_risk_scorer.py: known symbol index → correct risk scores per factor
   - test_adaptive_briefing.py: high-risk file → briefing contains extra warnings
   - Integration test: full loop brief → validate → gap → risk → adapt → brief again

8. Update PROGRESS.md.

Do NOT modify the core MCP tools or their schemas. The research layers run internally — they enhance the briefing and collect data, but the tool interface stays the same.
```

**Checkpoint:**
```bash
pytest tests/unit/test_grounding_gap.py tests/unit/test_risk_scorer.py tests/unit/test_adaptive_briefing.py -v
mypy src/groundtruth/analysis/ --strict
groundtruth risk-map --fixture tests/fixtures/project_ts
```

---

## Research Experiments

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: Experiment harness to produce the research findings.

IMPORTANT: Only build this after the research layers are implemented and tested.

1. Build experiment runner (benchmarks/experiments/):
   - experiment_runner.py: runs N coding tasks in three configurations:
     a. No GroundTruth (baseline)
     b. GroundTruth with standard briefing (control)
     c. GroundTruth with adaptive briefing (treatment)
   - For each task × config, record: hallucination rate, compliance rate, tokens used

2. Build the task set (benchmarks/experiments/tasks/):
   - 50 coding tasks per fixture project (150 total)
   - Each task: { description, target_file, expected_changes }
   - Tasks should cover all risk factor categories (high naming ambiguity, deep imports, etc.)

3. Build analysis scripts (benchmarks/experiments/):
   - analyze_grounding_gap.py: aggregate compliance data, produce summary stats
   - analyze_risk_correlation.py: correlate risk scores with actual hallucination rates
   - analyze_adaptive_improvement.py: A/B comparison of standard vs adaptive briefing
   - All output both JSON (machine-readable) and formatted text (human-readable)

4. Build visualization (optional):
   - If time permits, a simple HTML page or matplotlib charts showing:
     - Compliance rate by hallucination category
     - Risk factor correlation heatmap
     - Adaptive vs standard briefing comparison

5. Write a FINDINGS.md:
   - Template with sections for each research question
   - Placeholder for actual numbers (filled in after running experiments)
   - Clear methodology description

6. Update PROGRESS.md and README with experiment instructions.
```

**Checkpoint:**
```bash
python benchmarks/experiments/experiment_runner.py --tasks 10 --config baseline
python benchmarks/experiments/experiment_runner.py --tasks 10 --config standard
python benchmarks/experiments/experiment_runner.py --tasks 10 --config adaptive
python benchmarks/experiments/analyze_grounding_gap.py
```

---

## 3D Hallucination Risk Map

**Paste this into Claude Code:**

```
Read CLAUDE.md, PRD.md, and PROGRESS.md.

Build: Interactive 3D visualization of codebase hallucination risk.

IMPORTANT: Only build this after the research layers are complete and you have actual risk score data.

1. Build a standalone HTML file (src/groundtruth/viz/risk_map.html) using Three.js:
   - Force-directed 3D graph layout
   - Nodes = files or symbols
   - Edges = import/call relationships (from the refs table)
   - Node SIZE = number of references (heavily-used symbols are bigger)
   - Node COLOR = hallucination risk score:
     - Green (0.0-0.3): safe, agents rarely get this wrong
     - Yellow (0.3-0.6): moderate risk
     - Orange (0.6-0.8): high risk
     - Red (0.8-1.0): hallucination hotspot
   - Edge COLOR = lighter version of the source node's color

2. Interactivity:
   - Orbit controls (rotate, zoom, pan)
   - Click a node → side panel shows:
     - File/symbol name
     - Risk score breakdown (naming_ambiguity, import_depth, etc.)
     - Actual hallucination rate (from grounding gap data if available)
     - Top confusions: "agents confuse this with X (67% of the time)"
     - Number of callers/callees
   - Hover → tooltip with name + risk score
   - Search bar → highlight matching nodes
   - Toggle: show all nodes vs only high-risk nodes (risk > 0.5)
   - Toggle: show edges vs hide edges (dense graphs get cluttered)

3. Data generation (src/groundtruth/viz/generate_graph_data.py):
   - Query SQLite: all symbols, all refs, all risk scores
   - Output: JSON file with nodes[] and edges[] that the HTML consumes
   - CLI command: groundtruth viz --output risk_map.html
   - Opens the generated HTML in the default browser

4. Make it look excellent:
   - Dark background (#0a0a0a)
   - Bloom/glow effect on high-risk nodes (red nodes glow)
   - Smooth animations on click/hover
   - Clean sans-serif typography for the side panel
   - Responsive — works on any screen size

5. Write a test:
   - test_viz_data.py: given known symbols + risk scores, generates correct JSON structure
   - Verify node count matches symbol count, edge count matches ref count
   - Verify color assignments match risk thresholds

6. Update PROGRESS.md and README with a screenshot/GIF of the visualization.
```

**Checkpoint:**
```bash
groundtruth viz --project tests/fixtures/project_ts --output /tmp/risk_map.html
# Opens in browser — should see a 3D graph with colored nodes
pytest tests/unit/test_viz_data.py -v
```

---

## Notes for All Build Steps

- **Read PROGRESS.md first** every time. Continue from where the last step left off.
- **Update PROGRESS.md last** every time. Note what was built, what decisions were made, any blockers.
- **mypy --strict must pass** after every step. No `Any` types.
- **Tests must pass** before moving to the next step.
- If you deviate from CLAUDE.md, document WHY in PROGRESS.md.
- If an LSP server isn't available during testing, skip those tests gracefully (pytest.mark.skipif).
