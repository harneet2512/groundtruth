# GroundTruth Architecture

## Why LSP Over Tree-sitter

Tree-sitter parses syntax. LSP resolves semantics. The difference matters:

- **Tree-sitter** gives you `function getUserById(...)` — the name and its AST shape.
- **LSP** gives you the resolved type signature, the import path the compiler uses, the documentation, the diagnostics, and cross-file references.

GroundTruth needs to validate that an import resolves to a real symbol at a real path with the right signature. That requires compiler-grade information. LSP provides it. Tree-sitter does not.

The LSP protocol is also universal. `textDocument/documentSymbol` returns the same `DocumentSymbol` structure whether the language is TypeScript, Python, Go, or Rust. We write the LSP client once. It works everywhere. No per-language adapters. No grammars.

Adding a new language = one line of config. The LSP server for that language handles all the parsing.

## Four-Phase Flow

```
                    FIND          BRIEF         VALIDATE       TRACE
                    ────          ─────         ────────       ─────
Input:          task desc.     intent str     proposed code   symbol name
                    │               │              │              │
Step 1:     AI parse → symbols  keywords →    regex parse →   SQLite lookup
                    │           FTS5 search    imports/calls       │
Step 2:     SQLite graph BFS       │              │           graph.find_callers
                    │          AI distill →    check against      │
Step 3:     rank by distance   compact brief   SQLite index   graph.find_callees
                    │               │              │              │
Step 4:             │               │         Levenshtein →   impact_radius
                    │               │         cross-index →       │
                    │               │         AI fallback         │
                    ▼               ▼              ▼              ▼
Output:     ranked files       briefing +      valid/errors    callers +
            + reasons          warnings        + suggestions   callees + chain
```

**Phase 1 (Find):** One AI call to parse the task, then pure SQLite. Cost: ~$0.001.

**Phase 2 (Brief):** FTS5 keyword search, then one AI call to distill. Cost: ~$0.003.

**Phase 3 (Validate):** Entirely deterministic for ~85% of errors. AI fires only when Levenshtein and cross-index search both fail. Cost: $0 for most calls, ~$0.003 when AI is needed.

**Phase 4 (Trace):** Pure SQLite graph traversal. No AI. Cost: $0. Latency: <10ms.

## Where AI Is Used

GroundTruth uses AI in three places, each behind a deterministic gate:

1. **TaskParser** (`ai/task_parser.py`) — Parses natural language task descriptions into symbol names. Falls back to regex extraction without an API key.

2. **BriefingEngine** (`ai/briefing.py`) — Distills FTS5 search results into a compact briefing. Falls back to raw symbol list without an API key.

3. **SemanticResolver** (`ai/semantic_resolver.py`) — Resolves validation errors when deterministic methods (Levenshtein edit distance, cross-index symbol lookup) both fail. Fires for ~15% of validation errors.

All three use Claude Haiku for speed and cost. The system works without an API key — AI features gracefully degrade to deterministic alternatives.

## SQLite Schema

Five core tables power all operations:

- **`symbols`** — Every symbol in the codebase. Name, kind, language, file path, line range, signature, documentation, usage count. FTS5 virtual table mirrors name/file_path/signature/documentation for keyword search.

- **`exports`** — Maps symbols to their module export paths. Enables import validation (is this symbol exported from this path?).

- **`packages`** — Installed packages from manifests (package.json, requirements.txt, go.mod, Cargo.toml). Enables missing package detection.

- **`refs`** — Every reference to a symbol: which file, which line, what type (import, call, type_usage). Powers the call graph, impact analysis, and usage counts.

- **`interventions`** — Every tool invocation logged with outcome, errors found/fixed, AI usage, and latency. Powers the stats dashboard and research layers.

Key indexes enable sub-millisecond queries: symbol name, file path, exported symbols, module path, reference lookups.

## Validation Pipeline

```
proposed_code
     │
     ├─ ImportValidator ──→ symbol_not_found, wrong_module_path
     ├─ PackageValidator ─→ missing_package
     └─ SignatureValidator → wrong_arg_count
     │
     ▼ (errors without suggestions)
     │
     ├─ Levenshtein match (distance ≤ 3) → deterministic fix
     ├─ Cross-index search (right name, wrong path) → deterministic fix
     └─ SemanticResolver (AI, ~15% of cases) → AI fix
```

Each validator uses regex-based parsing (not a full AST) to extract imports and function calls from the proposed code, then checks them against the SQLite index. Language-specific import syntax (Python's `from X import Y`, Go's grouped imports, JS/TS destructured imports) is handled by a shared import parser.

## Adding a New Language

1. Add one entry to `LSP_SERVERS` in `src/groundtruth/lsp/config.py`:
   ```python
   ".rb": {"command": ["solargraph", "stdio"], "language_id": "ruby"}
   ```

2. That's it. The LSP client speaks the same protocol to all servers. The import parser already handles the common patterns. The validators work against the language-agnostic SQLite schema.

No adapters. No grammars. No per-language parsing logic.
