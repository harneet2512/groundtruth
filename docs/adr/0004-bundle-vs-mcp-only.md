# ADR 0004: Bundle as kernel install pointer, not single-file artifact

Date: 2026-04-30
Status: Accepted
Context: Handoff Open Question 4. Today's bundle is `D:\Groundtruth\.tmp_v7_bundle\gt_pretask_brief_v7_full.py` (~84 KB single self-contained Python file).

## Decision

The bundle stays for VM deployment but its role changes: it becomes a thin kernel install pointer that bootstraps the `groundtruth.control` + `groundtruth.adapters` packages on the target VM. It is no longer a single-file artifact that re-implements GT in one Python module.

## Background

The current bundle (`gt_pretask_brief_v7_full.py`) was assembled by `build_v7_bundle.py` as an auto-globbed concatenation of `pretask/*` and `runtime/*` modules into one importable file. This was acceptable when GT's during-task surface was an OH wrapper script that needed a single drop-in import. It does not scale once we have a kernel + multiple adapters with capability matrices and a Decision Trace telemetry contract.

## Alternatives considered

### A. MCP-only deployment (no bundle)

GT runs only as an MCP server. Adapters connect via stdio. No code is shipped to the VM beyond what the MCP host already has.

Pros: One transport. No bundle build step. Aligns with `future_plan.md`'s "no daemon" rule via stdio.

Cons: SWE-bench evaluation runs inside Docker images per task. The MCP server has to live somewhere reachable. For OH-style runtime evidence collection (post-edit hooks, mid-task pull), the adapter needs to be importable in the agent's Python environment, not just the host's. Pure MCP doesn't cover the during-task injection path.

### B. Continue the auto-globbed single-file bundle

Pros: Zero install steps on the VM.

Cons: Once the codebase has `control/`, `adapters/`, `runtime/`, `pretask/`, `mcp/`, `index/`, plus type definitions, plus tests, plus a capability matrix that adapters consult at construction -- a single concatenated file becomes unparseable. Type hints break across module boundaries. Pydantic models can't discover each other. Mypy --strict fails. We tried it; it does not survive the kernel split.

### C. Bundle as kernel install pointer (chosen)

The bundle becomes a small bootstrap script that:

1. Detects whether `groundtruth.control` is importable from the current Python env.
2. If yes, returns a handle pointing to the installed kernel + adapter for the active scaffold.
3. If no, installs the package from a pinned wheel (offline-capable: wheel is shipped alongside the pointer in the same archive).
4. Logs install/bootstrap events into `gt_runtime_telemetry.jsonl` so we know which VM ran which kernel version.

Pros:
- Preserves the "drop one file into the VM" UX.
- Lets `control/` and `adapters/` be normal Python packages with normal imports, types, and tests.
- VM deployment works under network-restricted environments because the wheel is bundled.
- One bootstrap script can serve all scaffolds; the adapter is selected at install time by the active scaffold env.

Cons:
- One indirection vs. a flat single file.
- Wheel must be rebuilt per kernel release (already true for `gt-index` Go binary releases; cadence is the same).

## Consequences

- `build_v7_bundle.py` is renamed (or replaced) to `build_kernel_bundle.py` in a Phase 1 follow-up. Out of scope for Phase 0.
- The adapter file (e.g. `adapters/openhands.py`) must NOT be inlined -- it must be importable from the installed package on the VM.
- ADR is reversible: if MCP-only deployment becomes viable for all target scaffolds in a future phase, the bundle can be retired entirely. We are not betting against that future, only acknowledging it does not work today for during-task injection.
