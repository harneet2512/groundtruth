# GroundTruth — Repository Audit for SWE-bench Readiness

**Date:** 2026-03-14
**Auditor:** Claude Code (automated)

---

## Section 1: Project Structure

### 1.1 src/groundtruth/ directory tree

**Status:** ✅ 46 Python files across 10 subpackages

```
src/groundtruth/
├── __init__.py
├── main.py
├── ai/
│   ├── __init__.py
│   ├── briefing.py
│   ├── client.py
│   ├── prompts.py
│   ├── semantic_resolver.py
│   └── task_parser.py
├── analysis/
│   ├── __init__.py
│   ├── adaptive_briefing.py
│   ├── grounding_gap.py
│   └── risk_scorer.py
├── cli/
│   ├── __init__.py
│   ├── commands.py
│   └── output.py
├── index/
│   ├── __init__.py
│   ├── graph.py
│   ├── indexer.py
│   └── store.py
├── lsp/
│   ├── __init__.py
│   ├── client.py
│   ├── config.py
│   ├── manager.py
│   └── protocol.py
├── mcp/
│   ├── __init__.py
│   ├── server.py
│   └── tools.py
├── stats/
│   ├── __init__.py
│   ├── reporter.py
│   ├── token_tracker.py
│   └── tracker.py
├── utils/
│   ├── __init__.py
│   ├── cache.py
│   ├── levenshtein.py
│   ├── logger.py
│   ├── result.py
│   ├── symbol_components.py
│   └── watcher.py
├── validators/
│   ├── __init__.py
│   ├── import_validator.py
│   ├── orchestrator.py
│   ├── package_validator.py
│   └── signature_validator.py
└── viz/
    ├── __init__.py
    ├── generate_graph_data.py
    └── risk_map_template.py
```

### 1.2 benchmarks/ directory tree

**Status:** ✅ Complete benchmark infrastructure

```
benchmarks/
├── README.md
├── _fixtures.py
├── runner.py
├── runner.ts                        (legacy TypeScript)
├── report.ts                        (legacy TypeScript)
├── types.ts                         (legacy TypeScript)
├── experiments/
│   ├── __init__.py
│   ├── experiment_runner.py
│   ├── models.py
│   ├── analyze_adaptive_improvement.py
│   ├── analyze_grounding_gap.py
│   ├── analyze_risk_correlation.py
│   └── results/
│       ├── adaptive.json
│       ├── adaptive_improvement.json
│       ├── adaptive_improvement.md
│       ├── baseline.json
│       ├── experiment_results.md
│       ├── grounding_gap.json
│       ├── grounding_gap.md
│       ├── risk_correlation.json
│       ├── risk_correlation.md
│       └── standard.json
├── file-relevance-cases/
│   └── find-001.json ... find-020.json    (20 cases)
├── hallucination-cases/
│   ├── invented-symbol/                    (15 cases)
│   ├── missing-package/                    (15 cases)
│   ├── wrong-import-name/
│   │   ├── close-match/                    (15 cases)
│   │   └── no-close-match/                 (10 cases)
│   ├── wrong-module-path/
│   │   ├── symbol-exists-elsewhere/        (15 cases)
│   │   └── module-doesnt-exist/            (5 cases)
│   ├── wrong-language-convention/          (10 cases)
│   └── wrong-signature/                    (15 cases)
├── results/
│   ├── latest.json
│   └── latest.md
├── swebench/
│   ├── __init__.py
│   └── scaffolds.py
└── verify/
    ├── __init__.py
    ├── hallucination_cases.py
    ├── verify.py
    └── results/
        └── .gitkeep
```

### 1.3 tests/ directory tree

**Status:** ✅ 34 test files (32 unit + 2 integration)

```
tests/
├── __init__.py
├── conftest.py
├── unit/
│   ├── __init__.py
│   ├── test_adaptive_briefing.py
│   ├── test_ai_client.py
│   ├── test_briefing.py
│   ├── test_cli.py
│   ├── test_component_matching.py
│   ├── test_diagnostic_cache.py
│   ├── test_experiment_runner.py
│   ├── test_graph.py
│   ├── test_grounding_gap.py
│   ├── test_import_validator.py
│   ├── test_indexer.py
│   ├── test_lsp_client.py
│   ├── test_lsp_manager.py
│   ├── test_lsp_protocol.py
│   ├── test_mcp_server.py
│   ├── test_mcp_tools.py
│   ├── test_orchestrator.py
│   ├── test_package_validator.py
│   ├── test_reasoning_guidance.py
│   ├── test_risk_scorer.py
│   ├── test_semantic_resolver.py
│   ├── test_signature_validator.py
│   ├── test_store.py
│   ├── test_suggestion_fallback.py
│   ├── test_task_parser.py
│   ├── test_token_tracker.py
│   ├── test_tools_explain.py
│   ├── test_tools_impact.py
│   ├── test_tools_patterns.py
│   ├── test_tracker.py
│   ├── test_verify.py
│   └── test_viz_data.py
├── integration/
│   ├── __init__.py
│   ├── test_cross_language.py
│   └── test_mcp_e2e.py
└── fixtures/
    └── project_py/  (20 Python fixture files)
```

### 1.4 Total .py files in src/groundtruth/

**Status:** ✅ **46** Python files

### 1.5 Total test files in tests/

**Status:** ✅ **34** test files (32 unit + 2 integration)

---

## Section 2: MCP Server and Tools

### 2.1 Tools registered in server.py

**Status:** ✅ 15 tools registered via `@app.tool()` decorators

1. `groundtruth_find_relevant` — Find relevant files for a task
2. `groundtruth_brief` — Proactive briefing before code generation
3. `groundtruth_validate` — Validate proposed code against the index
4. `groundtruth_trace` — Trace a symbol through the codebase
5. `groundtruth_status` — Health check and stats
6. `groundtruth_dead_code` — Find exported symbols with zero references
7. `groundtruth_unused_packages` — Find packages no file imports
8. `groundtruth_hotspots` — Most referenced symbols
9. `groundtruth_orient` — Codebase orientation
10. `groundtruth_checkpoint` — Session progress summary
11. `groundtruth_symbols` — List all symbols in a file
12. `groundtruth_context` — Show symbol usage context
13. `groundtruth_explain` — Deep dive into a symbol
14. `groundtruth_impact` — Assess blast radius of modifying a symbol
15. `groundtruth_patterns` — Detect coding conventions in sibling files

### 2.2 Handler functions in tools.py

**Status:** ✅ 15 handlers, all with reasoning_guidance, none calling AI directly

| Handler | Signature | reasoning_guidance | _token_footprint | AI Calls |
|---------|-----------|-------------------|-------------------|----------|
| `handle_find_relevant` | `(description, store, graph, task_parser, tracker, entry_points?, max_files?) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | `task_parser.parse()` (disabled) |
| `handle_brief` | `(intent, briefing_engine, tracker, store, graph?, target_file?, adaptive?) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | `briefing_engine.generate_briefing()` (disabled) |
| `handle_validate` | `(proposed_code, file_path, orchestrator, tracker, store, language?, grounding_analyzer?) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | `orchestrator.validate()` (deterministic only) |
| `handle_trace` | `(symbol, store, graph, tracker, direction?, max_depth?) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_status` | `(store, tracker) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_dead_code` | `(store, tracker) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_unused_packages` | `(store, tracker) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_hotspots` | `(store, tracker, limit?) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_orient` | `(store, graph, tracker, risk_scorer, root_path) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_checkpoint` | `(store, tracker, risk_scorer) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_symbols` | `(file_path, store, tracker) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_context` | `(symbol, store, graph, tracker, root_path, limit?) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_explain` | `(symbol, store, graph, tracker, root_path, file_path?) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_impact` | `(symbol, store, graph, tracker, root_path, max_depth?) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |
| `handle_patterns` | `(file_path, store, tracker, root_path) -> dict[str, Any]` | ✅ YES | N/A (via _finalize) | None |

**Note:** `_token_footprint` is added uniformly by the `_finalize()` wrapper in `server.py` (lines 66-71), not in individual handlers.

### 2.3 AI disabled in MCP context

**Status:** ✅ AI disabled via `api_key=None`

From `server.py` lines 57-61:
```python
# AI components get api_key=None — agents ARE the AI
task_parser = TaskParser(store, api_key=None)
briefing_engine = BriefingEngine(store, api_key=None)
lsp_manager = LSPManager(root_path)
orchestrator = ValidationOrchestrator(store, lsp_manager, api_key=None)
```

All three AI-capable components are initialized with `api_key=None`. The AI client's `available` property returns `False` when key is None. Handlers that use AI components fall back to deterministic paths (regex-based parsing for task_parser, FTS5-only for briefing, deterministic-only for validation).

---

## Section 3: SWE-bench Harness Files

### 3.1 benchmarks/swebench/__init__.py

**Status:** ✅ EXISTS — Empty file (0 bytes), package marker only.

### 3.2 benchmarks/swebench/scaffolds.py

**Status:** ✅ EXISTS — 28 lines

Contains one prompt constant. **No baseline prompt exists.** Full content:

```python
"""SWE-bench scaffolds for GroundTruth-assisted agent workflows."""

WITH_GROUNDTRUTH_SYSTEM_PROMPT = """You have access to GroundTruth MCP tools for compiler-grade codebase intelligence.

MANDATORY WORKFLOW — follow these steps in order:

1. Call groundtruth_orient to understand project structure, entry points, and risk areas.
2. Call groundtruth_find_relevant with your task description to identify which files matter.
3. Read the top-ranked files returned by find_relevant.
4. Call groundtruth_explain on the key functions you need to understand or modify.
5. Call groundtruth_brief with your intent and target file for a proactive briefing.
6. Call groundtruth_patterns on your target file to learn directory conventions.
7. Call groundtruth_impact on any symbol you plan to modify to understand blast radius.
8. Write your code changes, following the patterns and conventions discovered.
9. Call groundtruth_validate on your proposed code to check for structural errors.
10. Fix any errors reported by validate, then re-validate until clean.
11. Call groundtruth_checkpoint to review your session progress.
12. Run the project's test suite to verify correctness.

RULES:
- NEVER skip the orient step — it tells you how the project is structured.
- ALWAYS call impact before modifying high-usage symbols (usage_count >= 5).
- ALWAYS call validate after writing code — do not assume correctness.
- Follow reasoning_guidance in every tool response — it contains actionable next steps.
- Check _token_footprint to monitor your context usage.
- If validate reports errors, fix ALL of them before proceeding.
- Match the coding patterns detected by groundtruth_patterns.
"""
```

**Missing:** No `BASELINE_SYSTEM_PROMPT` (needed for A/B comparison in SWE-bench).

### 3.3 benchmarks/swebench/config.py

**Status:** ❌ MISSING
**Impact:** No SWE-bench configuration (model selection, token limits, instance filtering, cost caps).

### 3.4 benchmarks/swebench/agent.py

**Status:** ❌ MISSING
**Impact:** Cannot run the SWE-bench agent loop. This is the core file that drives task execution.

### 3.5 benchmarks/swebench/tools.py

**Status:** ❌ MISSING
**Impact:** No tool definitions for the SWE-bench agent (file read/write, bash execution, etc.).

### 3.6 benchmarks/swebench/groundtruth_bridge.py

**Status:** ❌ MISSING
**Impact:** No bridge between SWE-bench agent and GroundTruth MCP tools. Cannot call GroundTruth from the agent.

### 3.7 benchmarks/swebench/runner.py

**Status:** ❌ MISSING
**Impact:** No orchestrator to run SWE-bench instances, manage Docker containers, or coordinate evaluation.

### 3.8 benchmarks/swebench/evaluate.py

**Status:** ❌ MISSING
**Impact:** Cannot evaluate agent patches against SWE-bench test suites.

### 3.9 benchmarks/swebench/analyze.py

**Status:** ❌ MISSING
**Impact:** No analysis of SWE-bench results (baseline vs GroundTruth comparison).

### 3.10 benchmarks/swebench/cost_tracker.py

**Status:** ❌ MISSING
**Impact:** No cost tracking for SWE-bench API calls.

### 3.11 benchmarks/swebench/results/

**Status:** ❌ MISSING — Directory does not exist.

---

## Section 4: Verification System

### 4.1 benchmarks/verify/verify.py

**Status:** ✅ EXISTS — 10 checks defined

| Check | Function | What It Does |
|-------|----------|-------------|
| 1 | `check_1_index` | Indexes the project via LSP, verifies >0 symbols/files/refs |
| 2 | `check_2_risk_score` | Computes risk scores, validates range [0,1] with ≥2 distinct buckets |
| 3 | `check_3_orient` | Calls `handle_orient`, verifies project metadata and symbol count |
| 4 | `check_4_find_relevant` | Calls `handle_find_relevant`, validates ≥1 file returned + reasoning_guidance |
| 5 | `check_5_brief` | Calls `handle_brief`, verifies briefing text or relevant symbols returned |
| 6 | `check_6_explain` | Calls `handle_explain` on hub symbol, validates name/file/source_code |
| 7 | `check_7_impact` | Calls `handle_impact` on hub symbol, validates impact_summary present |
| 8 | `check_8_patterns` | Calls `handle_patterns` on file with siblings (0 patterns = WARN not FAIL) |
| 9 | `check_9_validate` | Runs hallucination cases, passes if ≥3 of ≥5 cases detected |
| 10 | `check_10_token_tracking` | Verifies session_total > 0 and breakdown has entries from checks 3-9 |

**`_init_components()` signature:**
```python
def _init_components(repo_path: str, db_path: str) -> tuple[
    SymbolStore, ImportGraph, InterventionTracker, TokenTracker,
    TaskParser, BriefingEngine, LSPManager, ValidationOrchestrator,
    RiskScorer, AdaptiveBriefing, Indexer,
]
```
Creates 11 components following the same dependency wiring as `mcp/server.py`.

### 4.2 benchmarks/verify/hallucination_cases.py

**Status:** ✅ EXISTS

**Static cases (3):**
1. `static-missing-package` — `import flask_nonexistent_ext`
2. `static-invented-symbol` — `from os.path import nonexistent_func_xyz`
3. `static-wrong-language-package` — `import axios` (Node.js package in Python)

**Dynamic cases (up to 3, generated from index):**
1. `dynamic-mangled-name` — Swaps chars in a real symbol name (Levenshtein test)
2. `dynamic-wrong-path` — Imports real symbol from wrong module
3. `dynamic-invented-export` — Imports `totally_fake_function_xyz` from a real file

### 4.3 Verify run history

**Status:** ❌ NEVER RUN ON REAL REPO

`benchmarks/verify/results/` contains only `.gitkeep`. No JSON output, no saved results.

---

## Section 5: CLI Commands

### 5.1 Subcommands in main.py

**Status:** ✅ 9 subcommands registered

| Subcommand | Arguments |
|-----------|-----------|
| `serve` | `--root`, `--db`, `--no-auto-index` |
| `index` | `path` (positional), `--db`, `--timeout` (default 300), `--exclude` (repeatable), `--force` |
| `status` | `--root`, `--db`, `--json` |
| `stats` | `--root` |
| `validate` | `file` (positional), `--root` |
| `dead-code` | `--root` |
| `risk-map` | `--root`, `--limit` (default 20) |
| `viz` | `--root`, `--db`, `-o/--output`, `--limit` (default 200), `--theme` (dark/light), `--no-bloom`, `--filter` (low/moderate/high/critical) |
| `verify` | `--repo` (required), `-o/--output`, `--checks`, `--verbose`, `--timeout` (default 120) |

Global: `--version`, `--help`

### 5.2 Command functions in commands.py

**Status:** ✅ All 8 command functions fully implemented (no stubs)

| Function | Status |
|----------|--------|
| `index_cmd` | Fully implemented — LSP indexing with async, timeout, force rebuild |
| `status_cmd` | Fully implemented — stats + risk data, JSON output option |
| `serve_cmd` | Fully implemented — MCP server start, auto-indexing |
| `stats_cmd` | Fully implemented — intervention statistics report |
| `validate_cmd` | Fully implemented — file validation via orchestrator |
| `dead_code_cmd` | Fully implemented — finds zero-reference exported symbols |
| `risk_map_cmd` | Fully implemented — hallucination risk scores |
| `verify_cmd` | Fully implemented — imports and runs run_verification() |

### 5.3 --help output

**Status:** ✅ Works

```
usage: groundtruth [-h] [--version]
                   {serve,index,status,stats,validate,dead-code,risk-map,viz,verify}
                   ...

MCP server — compiler-grade codebase intelligence for AI coding agents

positional arguments:
  {serve,index,status,stats,validate,dead-code,risk-map,viz,verify}
    serve               Start the MCP server (stdio)
    index               Index a project
    status              Show index status
    stats               Show intervention stats
    validate            Validate a file against the index
    dead-code           Find unused exported symbols
    risk-map            Show hallucination risk scores
    viz                 Generate 3D Code City risk map
    verify              Run pre-benchmark verification

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
```

### 5.4 --version output

**Status:** ✅ Works

```
groundtruth 0.1.0
```

---

## Section 6: Phase 13 Yellow Tier Verification

### 6.1 stats/token_tracker.py

**Status:** ✅ EXISTS

`TokenTracker` class with methods:
- `estimate_tokens(text: str) -> int` — static, ~4 chars per token
- `track(tool_name: str, response_text: str) -> int` — records usage, returns estimate
- `get_session_total() -> int` — total tokens across all calls
- `get_breakdown() -> dict[str, int]` — usage grouped by tool name
- `get_footprint(tool_name: str, call_tokens: int) -> dict[str, object]` — builds footprint dict

### 6.2 utils/symbol_components.py

**Status:** ✅ EXISTS

Two functions:
- `split_symbol_name(name: str) -> list[str]` — splits camelCase/snake_case/PascalCase/SCREAMING_SNAKE into lowercase components
- `suggest_by_components(name: str, candidates: list[str], min_overlap: int = 1, max_results: int = 5) -> list[tuple[str, float]]` — finds candidates sharing name components, scored by overlap ratio

### 6.3 Component matching in orchestrator.py

**Status:** ✅ Integrated

Line 17: `from groundtruth.utils.symbol_components import suggest_by_components`

Lines 121-133 in `_enrich_with_suggestions` method:
```python
# Component matching
component_matches = suggest_by_components(search_name, all_names)
if component_matches:
    best_name, score = component_matches[0]
    comp_find = self._store.find_symbol_by_name(best_name)
    if isinstance(comp_find, Ok) and comp_find.value:
        comp_sym = comp_find.value[0]
        error["suggestion"] = {
            "source": "deterministic",
            "fix": f"Did you mean '{best_name}' from {comp_sym.file_path}?",
            "confidence": min(0.85, score),
            "reason": f"Component match (score {score:.2f})",
        }
        continue
```

### 6.4 reasoning_guidance verification (3 handlers)

**handle_orient** — last lines of return dict:
```python
    return {
        "project": { ... },
        "structure": { ... },
        "build_commands": build_commands,
        "entry_points": entry_points,
        "top_modules": top_modules,
        "risk_summary": risk_summary,
        "reasoning_guidance": " ".join(guidance_parts),
    }
```
✅ `reasoning_guidance` present

**handle_validate** — last lines of return dict:
```python
    return {
        "valid": vr.valid,
        "errors": vr.errors,
        "ai_used": vr.ai_used,
        "latency_ms": vr.latency_ms,
        "reasoning_guidance": guidance,
    }
```
✅ `reasoning_guidance` present

**handle_explain** — last lines of return dict:
```python
    return {
        "symbol": symbol_info,
        "source_code": source_code,
        "dependency_chain": dep_chain,
        "calls_out_to": calls_out,
        "called_by": called_by,
        "side_effects_detected": side_effects,
        "error_handling": error_handling,
        "complexity": complexity,
        "reasoning_guidance": " ".join(guidance_parts),
    }
```
✅ `reasoning_guidance` present

---

## Section 7: Store and Index

### 7.1 SymbolStore public methods

**Status:** ✅ All 5 requested methods exist

Full public method list (36 methods):

| Method | Lines | Status |
|--------|-------|--------|
| `initialize()` | ~148 | ✅ |
| `connection` (property) | 163-167 | ✅ |
| `insert_symbol()` | 171-185 | ✅ |
| `find_symbol_by_name()` | 211 | ✅ |
| `get_symbols_in_file()` | — | ✅ |
| `delete_symbols_in_file()` | — | ✅ |
| `get_symbol_by_id()` | — | ✅ |
| `get_refs_from_file()` | — | ✅ |
| `get_all_symbol_names()` | 316 | ✅ |
| **`get_all_files()`** | **327-336** | **✅ EXISTS** |
| `update_usage_count()` | — | ✅ |
| `insert_export()` | — | ✅ |
| `get_exports_by_module()` | — | ✅ |
| `insert_ref()` | 400 | ✅ |
| `get_refs_for_symbol()` | — | ✅ |
| `get_imports_for_file()` | — | ✅ |
| `get_importers_of_file()` | — | ✅ |
| `insert_package()` | — | ✅ |
| `get_package()` | — | ✅ |
| `get_all_packages()` | — | ✅ |
| `search_symbols_fts()` | — | ✅ |
| `log_intervention()` | — | ✅ |
| `get_stats()` | 624 | ✅ |
| `get_dead_code()` | — | ✅ |
| `get_unused_packages()` | — | ✅ |
| `get_hotspots()` | 715 | ✅ |
| `insert_briefing_log()` | — | ✅ |
| `get_briefing_log()` | — | ✅ |
| `link_briefing_to_validation()` | — | ✅ |
| `update_briefing_compliance()` | — | ✅ |
| `get_recent_briefing_logs()` | — | ✅ |
| `get_briefing_logs_for_file()` | — | ✅ |
| **`get_top_directories()`** | **860-904** | **✅ EXISTS** |
| **`get_entry_point_files()`** | **906-925** | **✅ EXISTS** |
| **`get_symbols_in_line_range()`** | **927-942** | **✅ EXISTS** |
| **`get_sibling_files()`** | **944-960** | **✅ EXISTS** |
| `close()` | — | ✅ |

### 7.2 ImportGraph public methods

**Status:** ✅ All 4 requested methods exist

| Method | Lines | Status |
|--------|-------|--------|
| **`find_connected_files()`** | **45-102** | **✅ EXISTS** — BFS over import relationships |
| **`find_callers()`** | **104-127** | **✅ EXISTS** — files/lines referencing a symbol |
| **`find_callees()`** | **129-156** | **✅ EXISTS** — symbols referenced by code in a file |
| **`get_impact_radius()`** | **158-178** | **✅ EXISTS** — unique impacted files count |

---

## Section 8: AI Layer Status

### 8.1 ai/client.py

**Status:** ✅ Fully implemented

- **Model:** `claude-haiku-4-5-20251001` (line 10)
- **`complete()` method:** Async, takes `system`, `user`, `max_tokens` (default 512). Calls Anthropic SDK. Returns `Result[tuple[str, int], GroundTruthError]`. Handles auth errors, rate limits, API errors.
- **`available` property:** ✅ Returns `self._api_key is not None` — checks for API key.

### 8.2 ai/prompts.py constants

**Status:** ✅ 6 prompt constants defined

1. `TASK_PARSER_SYSTEM` — System prompt for extracting symbol names from natural language
2. `TASK_PARSER_USER` — User prompt template for task description parsing
3. `BRIEFING_SYSTEM` — System prompt for briefing generation
4. `BRIEFING_USER` — User prompt template for briefing
5. `SEMANTIC_RESOLVER_SYSTEM` — System prompt for semantic resolution fallback
6. `SEMANTIC_RESOLVER_USER` — User prompt template for semantic resolution

### 8.3 AI initialization in server.py

**Status:** ✅ AI disabled — all `api_key=None`

From `server.py` lines 57-61:
```python
# AI components get api_key=None — agents ARE the AI
task_parser = TaskParser(store, api_key=None)
briefing_engine = BriefingEngine(store, api_key=None)
lsp_manager = LSPManager(root_path)
orchestrator = ValidationOrchestrator(store, lsp_manager, api_key=None)
```

No AI client is passed to any handler. Handlers that could use AI (find_relevant, brief, validate) fall back to deterministic paths.

---

## Section 9: Test Suite

### 9.1 Test collection

**Status:** ✅ 448 tests collected

| Test File | Count |
|-----------|-------|
| `tests/integration/test_cross_language.py` | 24 |
| `tests/integration/test_mcp_e2e.py` | 42 |
| `tests/unit/test_adaptive_briefing.py` | 5 |
| `tests/unit/test_ai_client.py` | 5 |
| `tests/unit/test_briefing.py` | 7 |
| `tests/unit/test_cli.py` | 9 |
| `tests/unit/test_component_matching.py` | 16 |
| `tests/unit/test_diagnostic_cache.py` | 6 |
| `tests/unit/test_experiment_runner.py` | 22 |
| `tests/unit/test_graph.py` | 15 |
| `tests/unit/test_grounding_gap.py` | 6 |
| `tests/unit/test_import_validator.py` | 8 |
| `tests/unit/test_indexer.py` | 18 |
| `tests/unit/test_lsp_client.py` | 12 |
| `tests/unit/test_lsp_manager.py` | 8 |
| `tests/unit/test_lsp_protocol.py` | 23 |
| `tests/unit/test_mcp_server.py` | 3 |
| `tests/unit/test_mcp_tools.py` | 32 |
| `tests/unit/test_orchestrator.py` | 13 |
| `tests/unit/test_package_validator.py` | 7 |
| `tests/unit/test_reasoning_guidance.py` | 16 |
| `tests/unit/test_risk_scorer.py` | 13 |
| `tests/unit/test_semantic_resolver.py` | 6 |
| `tests/unit/test_signature_validator.py` | 6 |
| `tests/unit/test_store.py` | 38 |
| `tests/unit/test_suggestion_fallback.py` | 7 |
| `tests/unit/test_task_parser.py` | 8 |
| `tests/unit/test_token_tracker.py` | 6 |
| `tests/unit/test_tools_explain.py` | 10 |
| `tests/unit/test_tools_impact.py` | 10 |
| `tests/unit/test_tools_patterns.py` | 10 |
| `tests/unit/test_tracker.py` | 7 |
| `tests/unit/test_verify.py` | 11 |
| `tests/unit/test_viz_data.py` | 19 |

### 9.2 Test run results

**Status:** ✅ All 448 tests passed in ~44 seconds

15 warnings (all benign `RuntimeWarning` about unawaited mock coroutines in LSP client tests).

### 9.3 mypy --strict output

**Status:** ⚠️ 4 errors in 1 file

```
src\groundtruth\cli\commands.py:126: error: Argument "risk_scores" to "render_risk_summary" has incompatible type "list[object]"; expected "list[RiskScore]"  [arg-type]
src\groundtruth\cli\commands.py:163: error: Argument "risk_scores" to "render_status_json" has incompatible type "list[object]"; expected "list[RiskScore]"  [arg-type]
src\groundtruth\cli\commands.py:172: error: Argument "risk_scores" to "render_risk_summary" has incompatible type "list[object]"; expected "list[RiskScore]"  [arg-type]
src\groundtruth\cli\commands.py:246: error: "Coroutine[Any, Any, Ok[ValidationResult] | Err[GroundTruthError]]" has no attribute "value"  [attr-defined]
Found 4 errors in 1 file (checked 46 source files)
```

**Issues:**
1. Lines 126, 163, 172: Type narrowing issue — `_gather_risk_data()` returns `list[object]` instead of `list[RiskScore]`
2. Line 246: Missing `await` — `orchestrator.validate()` is async but result is accessed without awaiting

---

## Section 10: Dependencies and Environment

### 10.1 pyproject.toml dependencies

**Runtime dependencies:**
- `anthropic>=0.30.0`
- `mcp>=1.0.0`
- `pydantic>=2.0.0`
- `structlog>=24.0.0`

**Dev dependencies:**
- `pytest>=8.0.0`
- `pytest-asyncio>=0.23.0`
- `mypy>=1.10.0`
- `ruff>=0.4.0`
- `coverage>=7.0.0`

**Build system:** `hatchling`

### 10.2 SWE-bench package availability

| Package | Status | Notes |
|---------|--------|-------|
| `openai` | ✅ Installed (v2.8.0) | Not in pyproject.toml but available in env |
| `swebench` | ❌ NOT installed | `ModuleNotFoundError` |
| `datasets` | ✅ Installed (v4.0.0) | Not in pyproject.toml but available in env |

### 10.3 Docker

**Status:** ✅ Available — Docker Desktop, client v29.1.3, context `desktop-linux`

### 10.4 Pyright

**Status:** ❌ NOT available — `pyright: command not found`

Note: Project uses `mypy` for type checking (configured in pyproject.toml). Pyright is used as an LSP *server* (spawned by GroundTruth for Python projects), not as a development tool.

---

## Section 11: LSP Configuration

### 11.1 LSP_SERVERS dictionary

**Status:** ✅ 8 extensions mapped to 6 language servers

```python
LSP_SERVERS: dict[str, LSPServerConfig] = {
    ".py":  LSPServerConfig(command=["pyright-langserver", "--stdio"]),
    ".ts":  LSPServerConfig(command=["typescript-language-server", "--stdio"]),
    ".tsx": LSPServerConfig(command=["typescript-language-server", "--stdio"]),
    ".js":  LSPServerConfig(command=["typescript-language-server", "--stdio"]),
    ".jsx": LSPServerConfig(command=["typescript-language-server", "--stdio"]),
    ".go":  LSPServerConfig(command=["gopls", "serve", "-stdio"]),
    ".rs":  LSPServerConfig(command=["rust-analyzer"]),
    ".java": LSPServerConfig(command=["jdtls"]),
}
```

**Languages:** Python, TypeScript, TSX, JavaScript, JSX, Go, Rust, Java

### 11.2 LSP Manager start method

**Status:** ✅ `ensure_server(ext)` is the primary method

```python
async def ensure_server(self, ext: str) -> Result[LSPClient, GroundTruthError]:
```

Acquires or starts an LSP server for a file extension. Gets config, creates LSPClient, calls `client.start()`, runs LSP initialize handshake, caches and returns.

### 11.3 LSP Client high-level methods

**Status:** ✅ 7 request methods + 3 lifecycle methods

**LSP requests:**
1. `document_symbol(uri)` → `list[DocumentSymbol]` — `textDocument/documentSymbol`
2. `references(uri, line, character, include_declaration)` → `list[Location]` — `textDocument/references`
3. `hover(uri, line, character)` → `Hover | None` — `textDocument/hover`
4. `definition(uri, line, character)` → `list[Location]` — `textDocument/definition`
5. `signature_help(uri, line, character)` → `SignatureHelp | None` — `textDocument/signatureHelp`
6. `get_diagnostics(uri, timeout)` → `list[Diagnostic]` — waits for `publishDiagnostics`
7. `open_and_get_diagnostics(uri, language_id, text, timeout)` → `list[Diagnostic]` — open + diagnostics

**Lifecycle:**
- `did_open(uri, language_id, version, text)`
- `did_change(uri, version, text)`
- `did_close(uri)`
- `shutdown()`

---

## Section 12: Existing Benchmark Infrastructure

### 12.1 benchmarks/runner.py

**Status:** ✅ EXISTS — 610 lines

**GTBench** — GroundTruth Hallucination Detection Benchmark. Loads 100 hallucination cases + 20 file relevance cases from JSON. Creates per-language in-memory SQLite stores from `_fixtures.py`. Runs `ValidationOrchestrator.validate()` on each case. Measures: detection rate, fix rate, AI needed, briefing coverage. Outputs `results/latest.md` + `results/latest.json`.

### 12.2 benchmarks/_fixtures.py

**Status:** ✅ EXISTS

Provides per-language fixture data:
- `TS_SYMBOLS`, `PY_SYMBOLS`, `GO_SYMBOLS` — ~11 symbols each with names, kinds, file paths, signatures
- `TS_REFS`, `PY_REFS`, `GO_REFS` — ~6 references each
- `LANG_CONFIG` dict — per-language metadata (query_func, error_class, dead_symbol, unused_pkg, file paths)
- `populate_store(store, config)` — inserts symbols/refs/packages into SQLite

### 12.3 benchmarks/experiments/

**Status:** ✅ EXISTS — Complete experiment framework with saved results

**Files:**
- `models.py` — `ExperimentConfig` enum (BASELINE/STANDARD/ADAPTIVE), `ExperimentTask`, `ExperimentResult`, `ExperimentReport`
- `experiment_runner.py` — Runs 3 configs against hallucination cases, measures detection/fix/compliance rates
- `analyze_grounding_gap.py` — Standard vs adaptive briefing coverage analysis
- `analyze_risk_correlation.py` — Risk score vs hallucination rate correlation
- `analyze_adaptive_improvement.py` — Paired A/B comparison by case

**Saved results in `experiments/results/`:**
- `baseline.json`, `standard.json`, `adaptive.json` — per-config raw results
- `experiment_results.md` — combined report
- `grounding_gap.json` + `.md` — coverage analysis
- `risk_correlation.json` + `.md` — factor correlation
- `adaptive_improvement.json` + `.md` — A/B improvement

---

## Summary

### Ready for SWE-bench:
- [x] 15 MCP tools registered and working
- [x] reasoning_guidance in all 15 tool responses
- [x] _token_footprint in all responses (via _finalize)
- [x] AI disabled in MCP context (api_key=None) — agents are the AI
- [x] Component matching fallback chain in validator
- [x] Token tracking across sessions
- [x] 448 tests passing
- [x] CLI with 9 subcommands, all fully implemented
- [x] LSP client with 7 request methods (6 languages configured)
- [x] SQLite store with 36 public methods
- [x] ImportGraph with callers/callees/impact/connected_files
- [x] 100 hallucination benchmark cases + 20 file relevance cases
- [x] Experiment framework with saved results
- [x] Pre-benchmark verification system (10 checks)
- [x] WITH_GROUNDTRUTH_SYSTEM_PROMPT (12-step workflow)
- [x] Docker available
- [x] openai + datasets packages installed
- [ ] BASELINE_SYSTEM_PROMPT — **MISSING** (needed for A/B comparison)
- [ ] SWE-bench agent loop (`agent.py`) — **MISSING**
- [ ] SWE-bench runner/orchestrator (`runner.py`) — **MISSING**
- [ ] GroundTruth bridge (`groundtruth_bridge.py`) — **MISSING**
- [ ] SWE-bench tool definitions (`tools.py`) — **MISSING**
- [ ] SWE-bench evaluation (`evaluate.py`) — **MISSING**
- [ ] SWE-bench analysis (`analyze.py`) — **MISSING**
- [ ] SWE-bench config (`config.py`) — **MISSING**
- [ ] SWE-bench cost tracker (`cost_tracker.py`) — **MISSING**
- [ ] `swebench` Python package — **NOT INSTALLED**
- [ ] `pyright` command — **NOT AVAILABLE** (needed as LSP server for Python repos)
- [ ] Verification never run on real repo
- [ ] mypy --strict not clean (4 errors in commands.py)

### Missing files that need to be built:
1. `benchmarks/swebench/config.py` — Model selection, token limits, instance filtering, cost caps
2. `benchmarks/swebench/agent.py` — The agent loop that executes SWE-bench tasks
3. `benchmarks/swebench/tools.py` — Tool definitions for the agent (file I/O, bash, etc.)
4. `benchmarks/swebench/groundtruth_bridge.py` — Bridge between agent and GroundTruth MCP tools
5. `benchmarks/swebench/runner.py` — Orchestrator for running SWE-bench instances
6. `benchmarks/swebench/evaluate.py` — Patch evaluation against SWE-bench test suites
7. `benchmarks/swebench/analyze.py` — Results analysis and baseline comparison
8. `benchmarks/swebench/cost_tracker.py` — API cost tracking
9. `benchmarks/swebench/results/` — Results directory (create)
10. Baseline system prompt in `scaffolds.py` — For A/B comparison

### Issues found:
1. **mypy --strict fails** — 4 errors in `commands.py`: 3 type narrowing issues (`list[object]` vs `list[RiskScore]`) and 1 missing `await` on async `orchestrator.validate()` call (line 246)
2. **`swebench` package not installed** — Required to load SWE-bench dataset and evaluate patches
3. **`pyright` not on PATH** — GroundTruth spawns `pyright-langserver --stdio` for Python files; if not installed, LSP indexing of Python repos will fail
4. **Verify never tested on real repo** — The verification system exists but has never been exercised against a real codebase
5. **No baseline prompt** — `scaffolds.py` has `WITH_GROUNDTRUTH_SYSTEM_PROMPT` but no `BASELINE_SYSTEM_PROMPT` for control comparison
6. **Legacy TypeScript files** — `runner.ts`, `report.ts`, `types.ts` still in benchmarks/ (dead code)
7. **100 hallucination cases, not 105** — Actual count is 100 JSON files (PROGRESS.md says 100, directory listing confirms)
