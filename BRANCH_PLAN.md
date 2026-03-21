# Branch Plan: improvement-v0.8

## Capability
Productize GroundTruth's obligation-analysis wedge: test coverage,
CLI surface, fixture corpus, and local developer UX for the obligation
engine (constructor_symmetry, override_contract, caller_contract, shared_state).

## Primary Metric
Obligation engine test coverage: all 4 kinds have positive, negative,
and edge-case tests. False-positive rate on fixture corpus = 0.

## Cheapest Benchmark
Gate 0: `python -m pytest tests/unit/test_obligations.py -v`
Gate 1: obligation diff fixtures with expected output (tests/fixtures/obligation_diffs/)

## Merge Threshold
- All 4 obligation kinds have ≥2 positive and ≥2 negative tests
- Zero false positives on fixture corpus
- CLI diff-check command works end-to-end on fixture diffs
- Gate 0 passes clean

## In Scope
- Obligation engine test suite (all 4 kinds: constructor_symmetry, override_contract, caller_contract, shared_state)
- Obligation diff fixture corpus with expected output
- CLI `check-diff` command: parse a patch, run obligations, print results
- Local developer validation workflow (no Docker, no SWE-bench)

## Out of Scope
- Briefing, adaptive context, semantic resolution (future branches)
- SWE-bench evaluation runs (separate infra)
- MCP tool surface changes
- New obligation kinds beyond the current 4
- README or marketing copy updates

## Kill Condition
N/A — this is productization, not speculative research. Ship incrementally.

## Status
- [x] S1: Obligation engine test suite (test_obligations.py) — 58 tests, all pass
- [x] S2: CLI diff-check command — 12 CLI tests, stdin + --diff-file + exit codes
- [x] S3: Obligation diff fixture corpus — 6 scenarios, parametrized runner
- [x] S4: MCP tool exposure — 4 handlers (check_patch, obligations, scope, confusions)
