# ADR 0001: Kernel module location

Date: 2026-04-30
Status: Accepted
Context: GT full-potential plan (`D:\Groundtruth\future_plan.md`), Phase 0 spec (`whimsical-tinkering-bunny.md`), handoff Open Question 3.

## Decision

The kernel lives at `src/groundtruth/control/`. Adapters live at `src/groundtruth/adapters/<scaffold>.py`.

## Alternatives considered

### A. `src/groundtruth/kernel/`

Pros: Most literal. The word "kernel" appears in the plan and would be searchable.

Cons: `kernel` is loaded with OS-systems connotations (Linux kernel, microkernel) that overshoot what this module is. The module is a thin deterministic decision layer, not a privileged execution context. The plan's own language is "GT control kernel" -- `control` is the qualifier that does the work.

### B. `src/groundtruth/runtime/control/`

Pros: Reuses the existing `runtime/` namespace where `control_policy.py`, `replan.py`, and `patch_auditor.py` already live.

Cons: The kernel is meant to be a thin facade that absorbs `runtime/*` modules over time (see API.md factoring map). Nesting it under `runtime/` makes that absorption awkward -- the kernel would import its parent. We want the kernel to be a sibling that depends on `runtime/` internally during Phase 1, then becomes the import surface for `runtime/*` to be deprecated against.

### C. Top-level package `src/control/`

Pros: Strong visibility.

Cons: Breaks the single-package convention. Everything else lives under `groundtruth/`.

### D. `src/groundtruth/control/` (chosen)

Pros:
- Matches the plan's own naming ("GT control kernel").
- Sibling to `runtime/`, `pretask/`, `mcp/`, `adapters/` -- parallel with the architecture diagram in `future_plan.md`.
- "Control" describes the function (kernel decides allow/block/visible/audit) without claiming to be a kernel in the OS sense.
- Adapters at `src/groundtruth/adapters/` are also a sibling package, not nested under `control/`. Adapters import `control`; `control` never imports `adapters` (locked rule from `future_plan.md`).

## Consequences

- New imports: `from groundtruth.control import kernel, types`, `from groundtruth.adapters.openhands import OpenHandsAdapter`.
- Phase 1 kernel files do not import from `groundtruth.runtime` or `groundtruth.pretask` directly while the skeleton lands; the integration shims happen in the implementation pass once stub bodies are filled. The one exception is `control.decision_log`, which routes through `runtime.telemetry.append_block` because that is the canonical telemetry writer and we are not duplicating it.
- Existing imports (`from groundtruth.runtime.control_policy import decide_control_action`) keep working through Phase 1 -- the kernel is a facade, not a replacement -- so this ADR does not break callers.
