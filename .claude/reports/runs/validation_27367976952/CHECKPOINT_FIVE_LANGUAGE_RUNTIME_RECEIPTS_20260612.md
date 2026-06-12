# Checkpoint - five-language runtime receipts - 2026-06-12

Scope: deterministic product proof for runtime controller behavior across five
language families before benchmarking.

This is not a benchmark. It is an architecture-behavior receipt proving the shared
runtime controller behaves consistently for Python, Go, Rust, TypeScript/JavaScript,
and Java-style task paths.

## LIPI

```text
ID: FIVE-LANGUAGE-RUNTIME-001
Desired state: GT runtime control behavior is language-family agnostic for the
mini-swe integration path. File view/edit/test sensing, phase derivation, payload
policy, obligation lifecycle, verification guidance, context budget, and graph-to-action
wording should work without task-specific or benchmark-specific branches.
Current state: Added deterministic five-language fixtures and fixed the product
trajectory controller bug they exposed. command_event now classifies mutating commands
before read commands and uses safer read-command boundaries, so symbols such as
targetedBehavior no longer accidentally match the `rg` read command. Java/Maven
BUILD SUCCESS is now recognized as test evidence.
Logic: Clean. The fix belongs in src/groundtruth/runtime/trajectory_state.py because
state derivation is GT product behavior, not mini-swe adapter behavior or GHA behavior.
Implementation: Clean. No task IDs, no benchmark branches, no language-specific
special casing beyond standard source/test command recognition.
Integration: Clean for deterministic product proof. The mini-swe adapter already
delegates phase detection to the product controller, so this bug fix reaches the
integration path.
Plumbing: Receipts cover five language families plus adapter and truth propagation.
Verdict: CLOSED for deterministic five-language runtime-control proof.
Remaining bug, if any: This does not prove real Go/Rust LSP substrate readiness; P0-01
still needs rebuilt-image live proof.
Fix boundary: Substrate rebuild/re-proof remains the next proof boundary before any
ten-task benchmark.
```

## Bug Found And Fixed

The new receipt found a real controller bug:

- `trajectory_state.command_event()` checked read-command substrings before mutating
  commands.
- A symbol like `targetedBehavior` contained `rg`, so a Python write command was
  misclassified as a view.
- Result: edited files stayed empty, phase derivation could undercount edit state,
  and later GT interventions could be mistimed.

Fix:

- Mutating command detection now runs before read detection.
- Read detection now uses command-prefix/word-boundary matching.
- Java/Maven `BUILD SUCCESS` is recognized as passing test evidence.

## Receipts

```text
python -m pytest tests/test_runtime_five_language_fixtures.py tests/test_runtime_product_controller.py tests/test_phase_detection.py -q
15 passed

python -m pytest tests/test_runtime_five_language_fixtures.py tests/test_phase_policy_module.py tests/test_phase_detection.py tests/test_runtime_product_controller.py tests/test_obligation_tracker.py tests/test_verification_horizon_stage_a.py tests/test_verification_horizon_stage_b.py tests/test_verification_horizon_stage_c.py tests/test_context_budget.py tests/test_action_templates.py tests/test_oracle_lipi_audit_fixes.py tests/test_deepswe_adapter_failclosed.py tests/test_deepswe_delivery_surface_fixes.py tests/test_deepswe_infra_markers_and_brief_wrap.py tests/test_verified_adapter.py tests/test_task_truth.py tests/test_gt_deep_metrics_task_truth.py -q
175 passed
```

## Non-Claims

- This is not a tenpack or benchmark result.
- This does not close P0-01.
- This does not prove rust-analyzer, Go module cache, or baked substrate readiness.
