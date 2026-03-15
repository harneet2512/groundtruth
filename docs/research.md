# Research Context

## AI Code Hallucinations

Large language models frequently generate code that "looks right" but references nonexistent APIs, uses wrong function signatures, or imports from incorrect module paths. These hallucinations are particularly problematic because they compile or pass linting but fail at runtime.

Common hallucination patterns:
- **Wrong import names** — The model invents plausible function names (`authenticate` instead of `login`, `getUser` instead of `getUserById`).
- **Wrong module paths** — The model guesses a module structure that doesn't match the actual project (`./services/auth` instead of `./auth`).
- **Missing packages** — The model assumes popular packages are installed when they aren't.
- **Wrong signatures** — The model uses incorrect argument counts or types, often based on patterns from other projects in training data.

## Existing Approaches

**Static analysis (ESLint, TypeScript compiler):** Catches many errors but requires a full build environment. Slow for real-time validation during code generation.

**LSP-based validation:** Language servers provide real-time diagnostics but are complex to integrate, require persistent connections, and have high overhead for AI coding tool workflows.

**RAG / embedding search:** Retrieval-augmented generation can provide context before generation but doesn't validate the output. Embedding search is approximate and expensive.

## GroundTruth's Approach

GroundTruth combines two strategies that are individually insufficient:

### Proactive Briefing (Pre-Generation)
Instead of RAG over raw code, GroundTruth maintains a complete symbol index and uses FTS5 full-text search to find relevant symbols for a given intent. An AI layer distills the search results into a compact briefing (~150 tokens). This is more precise than embedding-based retrieval because it operates on structured symbol data rather than raw text.

### Reactive Validation (Post-Generation)
Instead of running a full compiler or LSP, GroundTruth performs targeted validation against the symbol index:
1. **Deterministic check** — SQLite lookup in <10ms. Catches most errors.
2. **Levenshtein matching** — Fixes close typos (edit distance <= 3) without AI.
3. **Cross-index search** — Finds symbols that exist in other modules.
4. **AI semantic resolution** — Only fires when all deterministic methods fail (~15-20% of errors).

This layered approach keeps costs low ($0.003 per briefing, $0 for most validations, $0.001 for AI fallback) while maintaining high accuracy.

### Key Differences from Other Tools

| Aspect | TypeScript Compiler | LSP | RAG | GroundTruth |
|--------|-------------------|-----|-----|-------------|
| Speed | Slow (full build) | Medium (incremental) | Medium (embedding lookup) | Fast (<10ms deterministic) |
| Setup | tsconfig required | Language server process | Vector DB + embeddings | Single SQLite file |
| AI cost | None | None | Per-query embedding | $0 for 80%+ of checks |
| Pre-generation help | No | No | Yes (retrieval) | Yes (structured briefing) |
| Post-generation fix | Error only | Error + some fixes | No | Error + deterministic fix + AI fallback |
| Transport | CLI / build step | Persistent connection | API call | MCP (stdio) |
