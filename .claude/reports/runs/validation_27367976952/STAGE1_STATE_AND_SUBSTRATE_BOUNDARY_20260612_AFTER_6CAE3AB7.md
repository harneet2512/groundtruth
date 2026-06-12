# Stage 1 state and substrate boundary after `6cae3ab7`

Date: 2026-06-12  
Branch: `gt-trial`  
Code head while writing: `6cae3ab7`

## Why this document exists

The code moved forward again after the architecture-first LIPI pass:

- `6cae3ab7` closed a real runtime-truth gap on the mini-swe path
- top-level handoff pointers were still behind the current head
- the remaining Stage 1 blocker is still the Go/Rust substrate proof boundary

This document is the current one-surface view:

```text
desired state = gt_gt.md + CLAUDE.md
current state = code at 6cae3ab7
remaining launch blocker = rebuilt substrate + green Go/Rust re-proof
```

## Current Stage 1 status

### Closed in code

These are now true on the product path:

1. Product runtime control plane exists under `src/groundtruth/runtime/`
   - phase/context policy
   - trajectory state
   - context budget
   - action translation
   - obligation lifecycle
   - verification horizon

2. DeepSWE adapter delegates to product logic instead of owning it outright
   - `artifact_deepswe/phase_policy.py` is a shim
   - `gt_mini_patch.py` delegates phase/budget/horizon/action behavior
   - `gt_oracle.py` delegates obligation lifecycle/rendering

3. `task_truth.json` is now the product truth surface for runtime-control summaries
   - trajectory state summary
   - obligation lifecycle summary
   - verification horizon summary
   - consumption summary
   - enforcement semantics
   - adapter witness

4. Wrong-phase suppression is now durable product truth
   - before `6cae3ab7`, wrong-phase candidates were filtered out silently
   - after `6cae3ab7`, they are written to `GT_RUNTIME_LEDGER` / `gt_runtime_ledger.jsonl`
   - `task_truth.runtime_control.runtime_ledger_summary` can surface them

5. Dead delegated branches were removed from the live adapter helpers
   - `_budget_trim`
   - `verify_horizon_band`
   - `_render_verify_emission`

6. Graph-to-action translation is stronger than before
   - verified caller contract facts now translate into caller-risk instructions
   - still not full graph-risk/action coverage for every evidence shape

### Still partial in architecture terms

These are not launch blockers by themselves, but they are still not the full desired state:

1. **Trajectory-state controller**
   - phase policy exists
   - full controller ledger is still partial
   - we still lack one explicit product object that says:
     - phase
     - trigger
     - eligible payloads
     - selected payload
     - suppression reasons

2. **Context selection contract**
   - phase allowlists exist
   - there is still no single canonical phase -> payload family -> budget -> next-action contract used everywhere

3. **Graph-to-action completeness**
   - product action translation exists
   - richer caller/callee/contract/cochange risk wording is still partial

4. **Full suppression truth**
   - wrong-phase suppression is now durable
   - not every suppression class is yet surfaced through the same runtime-truth path

## Substrate boundary audit

The substrate boundary is now much closer to the desired architecture than before.

### What is correct now

#### Workflow ownership is mostly clean

`.github/workflows/deepswe_full.yml` now does orchestration work:

- materialize task repo
- extract task dependency stores read-only
- mount repo + dep stores into the pinned substrate
- collect proof artifacts

It no longer tries to repair Rust by mutating the task image with `rustup component add rust-src`.

#### Proof runtime owns LSP readiness policy

`scripts/swebench/gt_run_proof.py` now owns per-language readiness budgets through:

- `lsp_ready_budget_seconds()`
- `aggregate_lsp_verdicts()`

The workflow only passes:

- `GT_LSP_READY_BUDGET_S_OVERRIDE`

This matches the desired architecture:

```text
workflow orchestrates
proof runtime owns proof policy
substrate image owns baked closure
task image owns task dependency state
```

#### Dependency-store extraction is structured

The workflow writes `dep_store_manifest.json` through:

- `scripts/swebench/dep_store_manifest.py`

This means Go/Rust substrate failures now have structured evidence instead of shell-only logs.

### What is still unproven

#### P0-01 remains live

We still do not have the final evidence required by the Stage 1 goal:

- rebuilt substrate image
- pinned new `GT_SUBSTRATE_DIGEST`
- green re-proof on the previously failing Go/Rust tasks

Without that, we still cannot say the product path is benchmark-ready.

#### Workspace metadata now owns Go/Rust readiness truth

The remaining substrate semantics are now cleaner than this document originally captured.

Go no longer uses "non-empty copied `gomodcache`" as product truth.
Instead:

- workflow still extracts/cache-mount evidence
- `dep_store_manifest.json` still records declared and copied paths
- `gt_run_proof.py` now owns an offline workspace metadata probe:
  - Go: `go list ./...`
  - Rust: `cargo metadata --format-version=1 --no-deps`

That matches the more general Stage 1 product claim:

```text
package metadata and workspace loading must be ready
```

The remaining open item is live proof, not local boundary semantics.

## LIPI

```text
ID: CP017
Desired state:
Top-level docs, machine truth, and the current code head all point to the same Stage 1 state,
and the substrate boundary is described by the correct owner layers.

Current state:
Code reached 6cae3ab7, but top-level handoff pointers still referenced older heads and older
checkpoint state.

Logic:
If the handoff surface points to stale heads, the team debugs the wrong state even when the
code is correct. That is a real product/debuggability bug.

Implementation:
This document captures the current Stage 1 state after 6cae3ab7 and restates the substrate
boundary in owner terms.

Integration:
Must be paired with pointer updates in CURRENT_VALIDATION_RUN, LATEST_TASK, and gt_gt.md.

Plumbing:
Run-folder doc is gitignored and must be force-added when committed.

Verdict:
Closed for current documentation slice once pointers are updated to this head.

Remaining bug, if any:
P0-01 remains live. This document does not replace substrate rebuild/re-proof.

Fix boundary:
Documentation truth surface only.
```

## Next work from here

1. Update top-level pointers to `6cae3ab7`
2. Keep the runtime-truth checkpoint in the handoff chain
3. Rebuild `gt-substrate`
4. Pin new `GT_SUBSTRATE_DIGEST`
5. Re-proof:
   - `abs-module-cache-flags`
   - `abs-stepped-slices`
   - `boa-hierarchical-evaluation-cancellation`
6. Only after green substrate proof: prepare the 10-task GT-on benchmark
