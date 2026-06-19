# ADR 0002: Validation via graph, not tests

Date: 2026-04-30
Status: Accepted
Context: Locked decision 3 (`future_plan.md`). Open question raised during Phase 0 spec design.

## Decision

`kernel.validate_against_graph(diff, graph) -> ValidationResult` is the canonical validation pass. It does not run tests, does not require tests to exist, and does not gate on test pass/fail. Validation is structural: did the diff break a function signature that callers depend on, did it orphan a caller, did it reference an undefined symbol.

Test-based validation (`runtime.replan` `failing_tests_after_edit` reason) remains available as a downstream signal that adapters with shell access can feed into `ReplanTriggers.failing_tests_after_edit`. But it is not the validation surface the kernel relies on. The kernel's `ValidationResult` is independent.

## Why this matters

### Locked decision 3

Quoting `future_plan.md`:

> **GT does not rely on tests existing.** Real-world repos may not have tests, may not have CI, may have skipped suites. Validation comes from graph + structural + repo-instruction signals only.

This is non-negotiable. Any validation step that fails closed when tests are missing violates this rule.

### SWE-Bench Pro evidence

SWE-Bench Pro (arXiv 2509.16941) directly measured what happens to agents on long-horizon tasks (>3 files, 100+ LOC) when spec/interface augmentation is removed:

> Without spec/interface augmentation, performance is substantially degraded.

The paper's failure-mode analysis names "incorrect file selection," "endless file reading," "wrong solution," and "syntax error" as top failures. None of these are caught by running tests -- they are caught by structural inspection. SWE-Bench Pro's own augmentation provided spec + interface info as static context. Our analog is the call graph, exposed through `validate_against_graph`.

### Why not just rely on tests when they exist

Three reasons:

1. **The agent's own reasoning loop already runs tests when they exist.** The kernel adding test execution on top is duplicate work that doesn't add information unless we're prepared to override the agent on test failures, which violates locked decision 2 (no writes to the repo, no overriding agent intent on its working tree).
2. **Test runs are slow and introduce side effects.** Graph queries are sub-second and read-only. The kernel must stay deterministic and fast on the per-tool decision path; running tests there is impractical.
3. **Test absence is a load-bearing real-world signal.** Production repos with skipped suites, missing CI, or partial coverage are the common case. A validation surface that only works when tests exist is a validation surface that doesn't work.

## Consequences

- `ValidationResult` schema is graph-derived only: `broken_signatures`, `orphaned_callers`, `undefined_symbols`. No `failing_tests` field.
- `ReplanTriggers.failing_tests_after_edit` remains as a separate boolean the adapter sets when it has external evidence; the kernel routes it as one input among several to `kernel.replan`.
- `gt_kernel_decision.policy_applied.rule_id` for validation-driven decisions uses `graph_validation_*` rule ids, not `tests_*` rule ids.
- Graph health assumed: this ADR depends on `graph.db` being current. Stale graphs produce false positives/negatives. Phase 1 includes `graph_db_sha` in every `gt_kernel_decision` provenance record so we can attribute false positives to graph drift.

## Out of scope

- Repo-instruction-based validation (AGENTS.md / CLAUDE.md / CONTRIBUTING.md priority hardening) is Phase 6, not this ADR.
