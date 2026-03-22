# Phase 1 — Parser Abstraction: Gate Results

**Date:** 2026-03-21
**Branch:** `research/foundation-v2`

## Gate 0: All tests pass

```
1099 passed, 4 skipped, 24 warnings in 46.46s
```

No existing test regressions. All 38 new tests pass.

## Gate 1: Phase complete

| Criterion | Status | Evidence |
|-----------|--------|---------|
| SymbolExtractor protocol defined | PASS | `foundation/parser/protocol.py` — Protocol with `parse_file()` and `extract_symbols()` |
| Tree-sitter backend parses Python | PASS | 18 tests covering functions, classes, methods, params, properties, docstrings, constants |
| Python AST backend wraps existing code | PASS | 10 tests covering all symbol types, rejects non-Python |
| Parity: both backends produce same symbols | PASS | 4 parity tests (simple, full fixture, real file, param counts) — all pass |
| Fallback: tree-sitter unavailable → AST used | PASS | Registry falls back to PythonASTExtractor when tree-sitter missing |
| Unsupported language: graceful skip | PASS | `.xyz` file → error in ParsedFile, empty symbol list |
| All new code in new directory | PASS | `src/groundtruth/foundation/parser/` and `tests/foundation/` only |

## Files created

- `src/groundtruth/foundation/__init__.py`
- `src/groundtruth/foundation/parser/__init__.py`
- `src/groundtruth/foundation/parser/protocol.py` — ExtractedSymbol, ParsedFile, SymbolExtractor
- `src/groundtruth/foundation/parser/treesitter_backend.py` — TreeSitterExtractor
- `src/groundtruth/foundation/parser/ast_backend.py` — PythonASTExtractor (wraps existing ast_parser)
- `src/groundtruth/foundation/parser/registry.py` — auto-selection + fallback
- `tests/foundation/__init__.py`
- `tests/foundation/test_parser_protocol.py` — 38 tests

## Files modified (minimal)

- `src/groundtruth/core/flags.py` — added `treesitter_enabled()` flag
- `pyproject.toml` — added `[parsing]`, `[search]`, `[watch]` extras groups

## Key design decisions

1. **tree-sitter 0.25 API** — uses `Language(capsule)` pattern from tree-sitter-python package
2. **Decorated definitions** — tree-sitter wraps `@property` methods in `decorated_definition` nodes; handled explicitly
3. **Parameters exclude self/cls** — both backends strip self/cls from parameter lists
4. **Lines are 0-indexed** — matches existing GT convention
5. **ExtractedSymbol.body_node** — holds the tree-sitter node for downstream analysis (fingerprints, vectors)

## Phase 1 complete. Phase 2 (Representation Substrate) can begin.
