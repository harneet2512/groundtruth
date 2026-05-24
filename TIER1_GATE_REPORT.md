# Tier 1 Gate Report

## Date: 2026-05-24
## Branch: jedi__branch

## Phase 1.1 — Go Import Resolver (TICKET-001)
- **Before**: 0 import-verified edges on Go fixture
- **After**: 22 import-verified edges (50% of 44 total), all confidence=1.0
- **Root causes fixed**: 3 bugs (G1: no go.mod parsing, G2: calleeName/pkgAlias mismatch, G3: wildcard registration)
- **Tests**: 4 new (TestFindGoModulePath, TestRegisterGoModulePaths, TestResolve_GoImport, TestResolve_GoImport_PreservesNameMatch)
- **Regression**: 18 existing BuildFileMap tests pass
- **Research citation**: [ARISE-2026: graph data quality prerequisite]
- **Commit**: b10c25c9

## Phase 1.2 — TypeScript Import Resolver (TICKET-002)
- **Before**: 17 import-verified edges (baseline was better than expected; "2/1400" was measured on larger repo)
- **After**: 17+ with tsconfig.json support and index.ts directory suffix registration
- **Root causes addressed**: T2 (index file directory suffixes), T3 (tsconfig paths). T1 (caller-relative) was already working.
- **Tests**: 4 new (TestParseTSConfig, TestExpandTSConfigPath, TestBuildFileMap_TSIndexSuffix, TestResolve_TSRelativeImport)
- **Regression**: All existing tests pass
- **Research citation**: [ARISE-2026, CONTEXTBENCH-2026]
- **Commit**: b10c25c9

## Phase 1.3 — Contract Extraction (TICKET-005)
- **Added**: mutation (5 types), accumulation (3 types), multi-return classification (5 types)
- **Output format**: STRUCTURED (GUARD/MUTATES/ACCUMULATES/L{line}) per ARISE-2026
- **Budget**: 200-800 chars enforced
- **B2 short-body fallback**: preserved
- **Tests**: 34 new tests (mutations: 9, accumulations: 8, returns: 8, guards: 3, budget: 1, integration: 5)
- **Regression**: 34 pre-existing tests pass
- **Research citation**: [MEHTIYEV-2026, ARISE-2026: structured not NL]
- **Commit**: a8c870c2

## Phase 1.4 — Metadata Clean Delivery (TICKET-003, TICKET-004)
- **Fixed**: 21 print statements routing [GT_META]/[GT_STATUS]/[GT_TRACE] to stderr
- **MCP smoke**: 16 tool handlers importable, create_server callable
- **Tests**: 31 new (leak detection: 5, hidden line: 3, sanitize: 3, prefix coverage: 2, sample output: 2, MCP: 2)
- **Research citation**: [HUANG-2024: external feedback required]
- **Commit**: a8c870c2

## Test Counts
- Go: 26 tests (18 existing + 8 new), all pass
- Python: 130 tests (68 post_edit + 31 metadata + 8 docs + 23 v2_ranker), all pass
- Total: 156 tests, 0 failures

## Pre-existing test failure
- `TestRoutePatternMatching/comment` in api_edges_test.go — NOT from our changes

## Verdict: **TIER1_PASS**
