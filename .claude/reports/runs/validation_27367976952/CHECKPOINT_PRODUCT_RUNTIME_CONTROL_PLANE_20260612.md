# Checkpoint - product runtime control plane - 2026-06-12

Scope: architecture-behavior parity slice for the mini-swe-agent integration path.

Principle applied:

`architecture behavior == code behavior`

This checkpoint does not claim benchmark readiness. It moves product identity out of
DeepSWE adapter-only code and into `src/groundtruth/runtime`, then verifies that the
adapter delegates to the product behavior.

## LIPI

```text
ID: RUNTIME-CONTROL-PLANE-001
Desired state: GT product behavior for trajectory state, phase policy, context budget,
obligation lifecycle, verification horizon, and graph-to-action translation is owned by
src/groundtruth. artifact_deepswe only adapts mini-swe command/observation flow and
injects the product runtime into the task container.
Current state: Product runtime modules now exist under src/groundtruth/runtime. The
DeepSWE phase policy is a shim. gt_mini_patch delegates phase detection, payload
allow/deny, budget/dedupe, graph-to-action wording, severity, horizon banding, and
horizon rendering to product modules. gt_oracle delegates obligation lifecycle/status
rendering to the product obligation module. gt_agent injects the product runtime files
into /opt/gt/groundtruth/runtime for mini-swe containers.
Logic: Clean for this slice. Phase/event/payload policy is declared once. Legacy
consensus.scope is now an explicit review-transition product payload, not hidden
adapter policy. Verification horizon remains an intervention, not a hard block.
Implementation: Clean for live behavior. Some old adapter code remains below early
delegation returns for compatibility/low-risk migration, but it no longer owns runtime
decisions in the touched paths.
Integration: Clean for mini-swe injection. The adapter ships product runtime modules
beside gt_mini_patch.py and keeps /opt/gt as the import root.
Plumbing: Receipts cover product modules, adapter shims, review-transition behavior,
verification no-leak behavior, context budget dedupe, and adapter install surface.
Verdict: CLOSED for the first product runtime control-plane extraction slice.
Remaining bug, if any: Full B5/B9 task_truth authority and full five-language proof are
not closed by this slice. P0-01 live substrate re-proof remains blocked until substrate
rebuild/digest pin.
Fix boundary: Next slice should move task_truth/runtime summaries to consume these
product runtime objects instead of recomputing phase/obligation/horizon truth elsewhere.
```

## Code Behavior Changed

- Added `src/groundtruth/runtime/context_policy.py` as the single declared
  phase/event/payload allow/deny policy.
- Added `src/groundtruth/runtime/trajectory_state.py` for product-owned state and
  phase derivation.
- Added `src/groundtruth/runtime/context_budget.py` for product-owned payload trim
  and stable fact-id dedupe.
- Added `src/groundtruth/runtime/action_translation.py` for graph fact to next-action
  wording.
- Added `src/groundtruth/runtime/verification_horizon.py` for advisory/urgent/gate/pivot
  semantics and leak-free rendering.
- Added `src/groundtruth/runtime/obligations.py` for shared obligation lifecycle,
  status vectors, and leak-free obligation-status rendering.
- Replaced `artifact_deepswe/phase_policy.py` with a shim importing the product policy.
- Updated `artifact_deepswe/gt_mini_patch.py` to delegate runtime decisions to product
  modules while keeping mini-swe interception and observation rendering in the adapter.
- Updated `artifact_deepswe/gt_oracle.py` to delegate obligation lifecycle/status
  behavior to the product obligation module.
- Updated `artifact_deepswe/gt_agent.py` to inject the product runtime modules into
  the mini-swe task container.

## Receipts

```text
python -m pytest tests/test_phase_policy_module.py tests/test_phase_detection.py tests/test_runtime_product_controller.py tests/test_obligation_tracker.py tests/test_verification_horizon_stage_a.py tests/test_verification_horizon_stage_b.py tests/test_verification_horizon_stage_c.py tests/test_context_budget.py tests/test_action_templates.py tests/test_oracle_lipi_audit_fixes.py -q
82 passed

python -m pytest tests/test_deepswe_adapter_failclosed.py tests/test_deepswe_delivery_surface_fixes.py tests/test_deepswe_infra_markers_and_brief_wrap.py tests/test_verified_adapter.py -q
79 passed

python -m py_compile src/groundtruth/runtime/context_policy.py src/groundtruth/runtime/trajectory_state.py src/groundtruth/runtime/context_budget.py src/groundtruth/runtime/action_translation.py src/groundtruth/runtime/verification_horizon.py src/groundtruth/runtime/obligations.py artifact_deepswe/phase_policy.py artifact_deepswe/gt_agent.py artifact_deepswe/gt_mini_patch.py artifact_deepswe/gt_oracle.py
passed
```

## Non-Claims

- This is not the ten-task benchmark run.
- This does not close P0-01 live Go/Rust substrate proof.
- This does not yet close task_truth B5/B9 authority propagation.
- This does not implement an official verifier-fail repair loop.
