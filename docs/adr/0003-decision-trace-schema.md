# ADR 0003: Decision Trace 7-element schema

Date: 2026-04-30
Status: Accepted
Context: `future_plan.md` §C, Cursor harness alignment §F (telemetry adoption), AgentTrace mapping (handoff Open Question 2).

## Decision

The `gt_kernel_decision` block follows the Decision Trace 7-element schema verbatim. The seven elements are:

1. `triggering_state`
2. `context_evaluated` (combines context + provenance)
3. `policy_applied`
4. `alternatives_considered`
5. `confidence`
6. `action_selected`
7. `authority_exercised`

This is the canonical record for every kernel decision -- pre-tool gate, drift check, validation pass, replan trigger.

## Why Decision Trace, not bare telemetry

Standard telemetry (counters, latencies, error codes) lets you answer "did it work?" Decision Trace lets you answer "why did it pick that action and what would have made it pick a different one?" For a deterministic policy kernel, the second question is the load-bearing one. We need to be able to:

- Reproduce a decision on replay (locked decision 6 TTD).
- Diff two decisions across config versions (paired Wilcoxon gate, locked decision 5).
- Show alternatives that were available but rejected (debuggability for the verifier lane).
- Mark when the action that fired was downgraded by adapter capability (`authority_exercised.degraded_from`).

The schema is from elixirdata.co's "AI Agent Decision Tracing vs Telemetry" framing. We are not inventing it.

## AgentTrace 3-surface mapping

AgentTrace (arXiv 2602.10133) defines three surfaces for agent observability:

| AgentTrace surface | GT mapping |
|---|---|
| Operational (what happened) | `gt_runtime_telemetry.jsonl` (existing block taxonomy) |
| Cognitive (why decisions were made) | `gt_kernel_decision` block (this ADR) |
| Contextual (environment + state) | `gt_hook_telemetry.jsonl` (existing steering events) |

Decision Trace fits the "cognitive" surface. Hooking it into the existing `runtime/telemetry.py:append_block` writer (rather than a new file) keeps a single replay-able JSONL stream for the proof layer to ingest in Phase 11.

## Cursor tool-error taxonomy adoption

Cursor's harness blog (Apr 30 2026) introduces a tool-error taxonomy:

`InvalidArguments`, `UnexpectedEnvironment`, `ProviderError`, `UserAborted`, `Timeout`, `Unknown`.

We adopt it for the `error_class` field in `gt_kernel_decision.context_evaluated.provenance.error_class` and `gt_pull.error_class`. Two reasons:

1. The categories are well-cut. `Unknown` specifically is alertable separately from agent mistakes -- when we see it, the harness or adapter has a bug, not the agent.
2. Per-tool reliability is a Phase 1 verify_report gate (`pull_error_rate_per_tool`). Without a fixed taxonomy, the gate produces noise instead of signal.

`error_class` is null on the success path and required on every error path. Tests in `tests/kernel/test_decision_log.py` enforce that error paths emit a non-`Unknown` class.

## Schema versioning

`policy_applied.rule_version` is `kernel-X.Y`. Phase 1 ships `kernel-0.1`. Bumps follow the kernel release version; rule additions or threshold changes that change the action distribution under fixed inputs require a minor bump.

The Decision Trace schema itself is not versioned in field 1-7 form -- if we change the seven, that is a new ADR.

## Consequences

- Replay tests become structural: load fixture input, call kernel function, compare output `Decision` plus the emitted `KernelEvent` against frozen JSON.
- The proof layer (Phase 11) can attribute every outcome (delivered / control / validation / agent execution) to a specific `KernelEvent` because the event fields are stable.
- `degraded_from` makes capability-degradation events first-class -- they are not noise; they are a separate gate (`degraded_capability_rate`).
