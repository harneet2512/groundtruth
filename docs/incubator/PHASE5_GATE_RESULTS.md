# Phase 5 — Gate 1 Results

**Date:** 2026-03-22
**Branch:** research/incubator-integration
**Total tests:** 1427 passed, 4 skipped, 0 failed

## Gate 1 Criteria

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | End-to-end fixture test (MCP path) | PASS | test_incubator_e2e.py::TestGate1_EachFlagIndependently |
| 2 | End-to-end CLI test | PASS | test_cli_incubator.py — CLI wiring verified |
| 3 | Flag parity (golden output) | PASS | test_incubator_e2e.py::TestGate1_FlagParity — same object, identical JSON |
| 4 | Each flag independently | PASS | test_incubator_e2e.py::TestGate1_EachFlagIndependently |
| 5 | Accumulated intelligence (log only) | PASS | test_incubator_e2e.py::TestGate1_AccumulatedIntelligence — 2 runs, data logged, no output change |
| 6 | Communication state machine | PASS | test_incubator_e2e.py::TestGate1_CommunicationStateMachine — 3 searches → redirect |
| 7 | Latency (<2x with flags ON) | PASS | 100 enrich() calls < 10ms; full suite 81s vs ~86s baseline |
| 8 | Flag migration compat | PASS | test_incubator_e2e.py::TestGate1_FlagMigrationCompat — old/new/both/neither |
| 9 | No DDL when disabled | PASS | test_incubator_e2e.py::TestGate1_NoDDLWhenDisabled |

## Files Created

```
src/groundtruth/incubator/__init__.py
src/groundtruth/incubator/runtime.py
src/groundtruth/incubator/intel_logger.py
src/groundtruth/incubator/intel_reader.py
src/groundtruth/incubator/abstention_bridge.py
src/groundtruth/foundation/similarity/substrate.py
src/groundtruth/foundation/similarity/substrate_bruteforce.py
src/groundtruth/foundation/similarity/substrate_hnsw.py
```

## Files Modified

```
src/groundtruth/core/flags.py
src/groundtruth/core/ablation.py
src/groundtruth/core/communication.py
src/groundtruth/mcp/server.py
src/groundtruth/mcp/tools/core_tools.py
src/groundtruth/cli/commands.py
src/groundtruth/foundation/similarity/composite.py
```

## Test Coverage

| Test File | Tests | Coverage Area |
|-----------|-------|---------------|
| test_flags.py | 68 | Flag migration, isolation, deprecation |
| test_ablation.py | 18 | AblationConfig, presets, field parity |
| test_communication.py | 18 | Original state machine tests (updated) |
| test_communication_fixes.py | 44 | Threshold, normalization, evidence |
| test_incubator_runtime.py | 16 | Byte parity, side effects, construction |
| test_intel_logger.py | 18 | Summary tables, upserts, cochange |
| test_intel_reader.py | 12 | Decision queries, flag gating |
| test_abstention_bridge.py | 6 | Single authority, fresh/stale |
| test_enrichment_conventions.py | 10 | Convention + state flow enrichment |
| test_cli_incubator.py | 4 | CLI wiring |
| test_substrate_query.py | 10 | SubstrateQuery protocol, brute-force |
| test_incubator_e2e.py | 13 | Gate 1 integration |

**Total new tests:** 237
**Total test suite:** 1427

## Gate 2 Status

Gate 2 (10-task diagnostic) requires fixture repos with realistic diffs.
Deferred to separate work item — Gate 1 is complete and all criteria pass.
