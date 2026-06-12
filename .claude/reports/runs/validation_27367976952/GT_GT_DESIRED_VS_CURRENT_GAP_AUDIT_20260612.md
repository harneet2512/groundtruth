# gt_gt desired vs current gap audit - 2026-06-12

Audited HEAD: `51c54d51`

Method: architecture-first. Desired state is `gt_gt.md` + `CLAUDE.md`; current state is
the code at HEAD. The 64-row register audit is the first pass. This document is the
second pass: what remains when we look from the whole architecture downward.

## Executive result

GT is no longer mainly missing primitives. Most primitives exist:

- graph/LSP/embedder proof surfaces
- structured proof failure/progress
- task_truth and reconciliation
- phase policy module
- obligation lifecycle/status events
- verification horizon
- context budget/dedupe
- self-verifier retry
- patch hygiene and artifact resolver

The remaining product bugs are not "no code exists." They are mostly:

1. product truth still drifts across docs/machine files,
2. some semantics are weaker than their architecture names,
3. live proof has not revalidated the substrate boundary,
4. phase/context control is present but not yet a complete architecture-level controller,
5. full phase/controller semantics are still not a single architecture-level contract.

Repair pass on this document closed `ARCH-01`, materially improved `ARCH-04`,
closed `ARCH-05`'s lifecycle vocabulary gap, and closed `ARCH-06`'s enforcement
overclaim by adding hard-block-only semantics. `ARCH-02/ARCH-03` remain partial.

## Architecture capability audit

### One pipeline

Desired state:
Graph, FTS5, LSP, semantic, brief, hooks, and metrics are capabilities of one GT
pipeline, not contradictory products.

Current state:
The code has moved toward one pipeline via `task_truth.json`, `reconcile.py`,
`artifact_resolver.py`, and proof artifacts. Runtime still has multiple surfaces:
`gt_run_proof.py`, `gt_agent.py`, `gt_mini_patch.py`, `gt_oracle.py`, deep metrics,
and workflow collection.

Gap:
The product truth is not yet fully single-surface because docs/machine files drifted
after commit `51c54d51`, and several runtime semantics are still named differently
across docs and code.

Repair:
`ARCH-01` is closed in code by `task_truth.authority` and
`reconciled_substrate_verdict.authority_map`. The product truth surface now states
which artifact owns outcome, substrate, runtime witness, brief delivery, verifier
semantics, obligations, patch hygiene, trajectory integrity, and consumption.

### Trust-gated substrate

Desired state:
Certs, gates, runtime witness, reconciled verdict, and task truth agree.

Current state:
`task_truth.py` and `reconcile.py` make this much stronger. `reconciled_substrate_verdict.json`
is emitted beside task truth.

Gap:
Raw certs still exist and can be read directly. This is acceptable only if all product
dashboards and docs point users to task truth/reconciled verdict.

New bug:
Covered by `P0-07/P0-14/P2-03`, no new row needed.

### LSP product readiness

Desired state:
LSP readiness means useful definition/type resolution under the real task image, not
warm-probe liveness.

Current state:
The code records product verdicts and the GHA proof now fails on real Go/Rust readiness
instead of silently passing warm server probes.

Gap:
P0-01 is not closed until a rebuilt substrate passes Go/Rust proof. Go dep readiness may
still be too cache-centric; Rust rust-src/proc-macro/linker readiness needs live artifact
proof.

New bug:
Covered by `P0-01/P0-04/P0-05`; no new row needed.

### Semantic/embedder truth

Desired state:
Product verdict and diagnostic probe never silently contradict.

Current state:
Deep metrics separates product verdict from diagnostic fallback. Proof image bakes models
and self-tests embedder load/nonzero output.

Gap:
No current architecture bug found in the audited path.

New bug:
None.

### Phase controller

Desired state:
GT has a controller that asks: what phase is the agent in, what context is needed, should
GT speak or stay silent?

Current state:
`phase_policy.py` exists and `gt_mini_patch.py` imports it. This is a policy table, not yet
a full trajectory-state controller.

Gap:
The architecture row "Trajectory-state controller shipped" is too strong. The code has
phase allowlisting and event classification, but not a complete controller object with
state, confidence, speak/silence decision, and observable reason.

New bug:
`ARCH-02`: Replace "trajectory-state controller shipped" wording with "phase policy
module shipped; controller partial." Product completion requires a controller ledger:
phase, trigger, eligible candidates, selected candidate, suppress reason.

### Context selection policy

Desired state:
ORIENT/VIEW/EDIT/VERIFY/SUBMIT each get bounded, phase-specific context.

Current state:
Policy allowlists exist. Some payloads are phase-gated. ORIENT/VIEW policy is still partial
per `gt_gt.md` section 17.8.

Gap:
There is not yet a single phase-to-payload contract used by all surfaces. Some logic still
lives in runtime patch functions and candidate code.

New bug:
`ARCH-03`: Add a canonical context selection contract that maps phase -> allowed payload
families -> max budget -> required next-action field.

### Context budget

Desired state:
Payloads are small, deduped, and action-oriented.

Current state:
Runtime has stable fact ids, delivered fact ids, budget metadata, and oracle event
emission metadata with payload hash, payload size, surface, and an actionability flag.

Gap:
Budgeting is still mostly runtime-local. The first metadata bridge exists, but there is
not yet a product-level invariant that every emitted payload across every surface carries
the full `phase`, `reason`, `next_action`, and `risk_if_ignored` schema.

Repair / remaining gap:
`ARCH-04` is PARTIAL. `gt_mini_patch.py` now emits `schema=gt.oracle_event.v2`
with stable `payload_hash`, `payload_chars`, `surface`, and `actionable`; full
cross-surface phase/reason/next-action/risk metadata remains part of `ARCH-03`.

### Obligation model

Desired state:
Issue clauses have lifecycle status and evidence in product truth.

Current state:
`ObligationTracker` exists; status persists to `obligation_status.json`; task truth includes
`obligation_status`. The tracker now supports explicit `satisfied` and `contradicted`
transitions and snapshots include `status_certainty`.

Gap:
The lifecycle vocabulary gap is closed. Strong independent satisfaction proof still
depends on integrations calling `mark_satisfied()` only when they have explicit proof.

Repair:
`ARCH-05` is closed at the model boundary: `tested`, `satisfied`, and
`contradicted` are distinct lifecycle states with certainty labels. Live integrations
must continue to treat `tested` as evidence, not proof of hidden correctness.

### Verification horizon

Desired state:
Behavior-level verification guidance without exact hidden test names.

Current state:
`_render_verify_emission()` does not render exact test names, file paths, or single-test
commands. It uses "narrowest relevant repo test target" language.

Gap:
No leak bug found. Quality/actionability still requires live trajectory audit.

New bug:
None beyond `P1-20` quality monitoring.

### Pre-submit semantics

Desired state:
If GT does not hard-block submit, docs never call it a hard gate.

Current state:
Code says intervention; docs still contain "Pre-submit gate" in places.

Gap:
Semantic drift remains.

New bug:
Covered by `P0-08/P2-01`, no new row needed.

### Retry semantics

Desired state:
Self-verifier retry is not official verifier repair.

Current state:
Code and task truth distinguish the two. Some architecture labels still say
"verifier-fail retry plumbing" without enough qualification.

Gap:
Doc drift, not runtime drift.

New bug:
Covered by `P0-09`, no new row needed.

### Consumption states

Desired state:
Delivered, Used, and Enforced are separate product states.

Current state:
Runtime suppression heuristic is separated from post-run consumption metrics. Deep metrics
has delivery/consumption fields. Consumption ledger now separates
`gt_blocks_verification_followup` from `gt_blocks_hard_enforced`, and
`gt_blocks_enforced` is hard-block-only for backward-compatible readers.

Gap:
No current code path claims a test-looking follow-up is hard enforcement. Real hard-block
enforcement remains zero unless an actual blocker is implemented.

Repair:
`ARCH-06` is closed at the metric boundary. Product surfaces now expose
`gt_enforcement_semantics=hard_block_only` and keep verification follow-up separate.

### Scorecard

Desired state:
Trajectory metrics do not claim flip causality without paired proof.

Current state:
Scorecard tooling exists; live run is blocked. `CLAUDE.md` correctly says right trajectory
matters more than resolved verdict.

Gap:
No live populated post-fix scorecard yet. Metric names like "gold" remain partial.

New bug:
Covered by `P1-26/P1-27`; no new row needed.

### Artifact integrity

Desired state:
Zero-byte/corrupt/missing artifacts classify cleanly.

Current state:
task_truth detects trajectory integrity, mini fallback, patch hygiene, oracle event status,
and artifact paths centrally.

Gap:
No new architecture gap found in the product path.

New bug:
None.

### GHA boundary

Desired state:
GHA cannot hide product failure behind smoke success or stale docs.

Current state:
Digest pinning, dep manifests, proof failure JSON, and task truth collection exist. Smoke
equivalence remains partial, and current-run JSON was stale before this audit.

Gap:
GHA is close, but not launch-ready until P0-01 proof is green. Machine truth is updated
in this repair pass.

New bug:
Covered by `P0-01/P0-03/P2-03/P2-14`; no new row needed.

## New architecture bugs found outside the original 64

| ID | Bug | Why it matters | Fix boundary |
|---|---|---|---|
| ARCH-01 | CLOSED: product truth authority contract added | Prevents future split truth between certs, task_truth, metrics, and docs | `task_truth.authority` + `reconciled_substrate_verdict.authority_map` |
| ARCH-02 | Phase policy shipped, full trajectory-state controller still partial | Current code has allowlists, not a complete speak/silence controller | Controller ledger: phase, reason, eligible, selected, suppressed |
| ARCH-03 | No single phase-to-payload contract across all surfaces | Payload behavior can drift between runtime patch/oracle/brief | Canonical context selection schema |
| ARCH-04 | PARTIAL: oracle event metadata added, full cross-surface schema remains | Makes actionability and consumption harder to prove | Finish canonical phase/reason/next_action/risk schema |
| ARCH-05 | CLOSED: `satisfied`/`contradicted` semantics added | Token/test overlap can overstate clause satisfaction | `ObligationTracker` explicit state methods + `status_certainty` |
| ARCH-06 | CLOSED: enforcement is hard-block-only | Product metrics can imply stronger control than code has | Ledger/deep-metrics/task-truth enforcement semantics |

## Do not benchmark yet

The architecture is not ready for tenpack until:

1. `P0-01` is green on rebuilt substrate.
2. `ARCH-02/ARCH-03/ARCH-04` are either closed or explicitly scoped out of launch.
3. `gt_gt.md` wording remains aligned with controller/intervention/retry owner semantics.
