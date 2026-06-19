# GT Full-Potential Plan

Date: 2026-04-30 (amended — folds in this conversation's decisions)
Status: locked decisions + sequenced build plan. Single canonical doc; supersedes the split between this file, `localization_docs/v7/HANDOFF_GT_TO_FULL_POTENTIAL.md`, and `whimsical-tinkering-bunny.md` §F+§G.

## Goal

GT is a deterministic control kernel that AI coding agents plug into via thin per-scaffold adapters. The kernel exposes GT's existing capabilities (brief, post-edit observation, mid-task graph queries, validation) through one canonical interface. Adapters translate scaffold-specific events to/from the kernel. Same kernel works on OpenHands, SWE-agent, mini-SWE, Aider with adapter-only changes.

Benchmarks are the proving ground, not the goal.

## Locked Decisions (inviolable)

Any future change that violates one of these requires explicit re-discussion.

1. **No LLM inside GT, ever.** Every kernel and adapter step is deterministic. If a step requires interpretation, it does not belong in GT.
2. **GT does not write to the repo, ever.** Read-only against the agent's working tree. The agent edits; GT observes, validates, replans.
3. **GT does not rely on tests existing.** Validation comes from graph + structural + repo-instruction signals only.
4. **Build adapters, not new scaffolds.** GT plugs into existing scaffolds through adapters <=200 LOC each. We do not build a competing harness.
5. **Paired Wilcoxon n>=15 gate before any change lands as default.** Same scaffold, same model, same tasks; GT-on vs GT-off. Non-regression is the floor.
6. **TTD discipline.** Artifact-first tests, red-before-green, mutation checks. Stress-test layers: happy / boundary / adversarial / mutation per pure function.
7. **Plain language, no marketing jargon.** Describe what something does, not what it is conceptually.
8. **No strawman caveats.** Don't hedge against failure modes that don't apply to the deployment context.
9. **No human-calendar time estimates in plans.** Name the lane and its deliverable, not the duration. Sequencing matters; clock estimates are noise.

## Three-layer scoring framework

GT's quality decomposes into three independently-owned layers. Never mix them in a single score.

| Layer | What it measures | Owner | How to compute |
|---|---|---|---|
| **Layer 1 — GT localization** | Does GT's retrieval pipeline surface the right files? | GT team | From `<iid>_pretask.jsonl` → `gt_plan.cluster_files` (top-N) and `gt_plan.agent_focus_files` (top-3) intersected with dataset gold patch. Metrics: focus_precision, focus_coverage, cluster_precision, cluster_coverage, first_gold_rank, gold_in_focus, gold_in_cluster |
| **Layer 2 — Delivery** | Does the brief make it cleanly into the agent's context? | Bundle / wrapper / kernel adapter | Brief generated rate, brief injection rate, sanitizer corruption rate, brief size distribution, confidence framing rendered correctly |
| **Layer 3 — Agent listening** | Does the agent act on what was delivered? | OH / model / harness — **not GT** | Edit precision, edit coverage, first_gold_step, listening rate (edited gold when GT delivered correctly). Reported only as a downstream signal; GT does not own it. |

Standing rule (per `feedback_localization_metrics_layer.md`): when asked for "localization metrics", default to Layer 1. Agent edit metrics belong to Layer 3 and must be labeled as such.

## Architecture

```
+---------------------------------------------------------+
|  GT control kernel (scaffold-agnostic, deterministic)   |
|  - brief generation                                     |
|  - pre-tool decision (allow / block / visible / audit)  |
|  - post-edit observation                                |
|  - mid-task pull (graph queries via MCP)                |
|  - drift detection (structural signals only)            |
|  - graph-validation-to-replan                           |
|  - telemetry (Decision Trace 7-element record)          |
+---------------------------------------------------------+
        ^           ^           ^           ^
        |           |           |           |
   OH adapter   SWE-ag adp  mini-SWE adp  Aider adp
   <=200 LOC    <=200 LOC   <=200 LOC     <=200 LOC
        ^           ^           ^           ^
        |           |           |           |
   OH's        SWE-agent's   mini-SWE's    Aider's
   hooks +     agent_config  step          pre-prompt
   MCP slots   + hooks       callbacks     hook
```

### Hard rules

- **Kernel is a bridge, not an enhancer.** It exposes existing GT capabilities through one interface. It does NOT invent new agent-side detectors, behavioral heuristics, or steering features beyond what GT already produces.
- Adapters do translation only; no GT logic in an adapter.
- Kernel never imports adapter code; adapters import the kernel.
- Each adapter declares a capability matrix `{block, visible, audit, mid_task_pull, replan_inject}`.
- Capability degradation is graceful: if `block` not supported, decision degrades to `visible`, never crashes; degradation lands in Decision Trace `authority_exercised`.
- New scaffold support = new adapter file. Kernel never modified for one scaffold's quirks.

### Layer 3 is explicitly out of scope

GT does not try to fix agent listening from inside the kernel or adapter. The 25% listen-rate observed on the v7.3 gate is the harness's problem (OH `AgentStuckInLoopError` eagerness, model loop pathology, agent search strategy). The kernel exposes `kernel.pull` so the agent CAN call GT mid-task; whether the agent uses it is the harness/model's problem. Specifically rejected:
- No `repeated_identical_actions` or other agent-loop detectors in kernel
- No prompt rewrites by the kernel/adapter
- No agent-steering features beyond what already exists in GT

## Capability matrix (planning baseline; adapters confirm at build time)

| Capability         | OpenHands | SWE-agent | mini-SWE | Aider | Claude Code (MCP) |
|---|---|---|---|---|---|
| Pre-task brief     | yes       | yes       | yes      | yes   | yes |
| Post-edit observe  | yes (`on_event`) | yes | yes | partial | yes |
| Pre-tool block     | yes (`ConfirmationPolicy`) | partial | yes | no | yes |
| Mid-task pull      | yes (`MCPToolDefinition`) | partial | no | no | yes |
| Replan injection   | via on_event message | via hook | via callback | via prompt | yes |

OH capabilities upgraded from "no/partial" after reading OH SDK paper (arXiv 2511.03690 §4.4/§4.5/§4.9). All 5 OH capabilities are real, not degraded.

## v7.3 verification gate — PASS

Recorded 2026-04-30. Paired n=10 v7.3 fresh vs v7.2 frozen smoke on SWE-bench-Live lite, MiMo-V2-Flash.

| Layer | Verdict | Evidence |
|---|---|---|
| Layer 1 (GT localization) | **non-regression** | bit-identical retrieval output: cluster_files + agent_focus_files match per-task across all 10 tasks (deterministic pipeline; v7.3 only changed render layer) |
| Layer 2 (Delivery) | **non-regression with sanitizer fix** | brief injection 10/10, brief_chars 1134 avg, v7.3 sanitizer cleaned the "telemetry-only file" leak that v7.2 had |
| Layer 3 (Agent listening) | **out of scope per locked rule** | reported only: 25% listen rate when GT delivered correctly. Not gated. |

Wilcoxon p=0.6250 on patch-emission delta; n=10 too small for significance. Non-regression on the primary metric per handoff §4 decision tree → unblocks subsequent phases.

Cost: $0.37 v7.3 fresh + $0.46 v7.2 frozen (sunk yesterday) + ~$0.40 v7.2 fresh re-run (killed mid-task 10 — should have used frozen baseline).

## Layer 1 score and current backlog

Layer 1 score from v7.3 gate: **7.0/10** (n=10).

| Sub-metric | Value |
|---|---|
| Tasks gold in top-3 focus | 8/10 |
| Median first_gold_rank | 2 |
| Avg focus precision | 0.33 |
| Avg focus coverage | 0.49 |
| Hard-miss tasks (gold not in top-8 cluster) | 2/10 |

**Hard-miss backlog (Layer 1 lever):**
- `aws-cloudformation__cfn-lint-3767` — gold not in cluster
- `pydata__xarray-9586` — gold not in cluster

Cutting hard-miss to 0/10 lifts Layer 1 to ~8.0/10. Surgical retrieval tweak only — no v8 rewrite. Diagnose which retrieval module failed (anchors / traces / PPR / hybrid / cochange) per task before any code change.

## Cursor harness blog adoptions (2026-04-30)

Source: Heule + Katz, "Continually improving our agent harness" (Apr 30 2026). All adoptions vetted against locked decisions.

| Adoption | Lands as |
|---|---|
| Keep Rate (fraction of recommended files surviving in final patch) | Report-only verify_report gate `gt_keep_rate` |
| Tool error taxonomy `InvalidArguments / UnexpectedEnvironment / ProviderError / UserAborted / Timeout / Unknown` | `error_class` enum on `gt_kernel_decision.context_evaluated.provenance` |
| Per-tool reliability SLO (Cursor target: 2-3 nines) | Report-only verify_report gate `pull_error_rate_per_tool` |
| Per-model harness tuning (OpenAI patch-edit vs Anthropic str-replace) | `model_hint?: str` field on `RunState`; adapter populates from scaffold metadata |
| Mid-conversation handoff with state summary | `kernel.handoff_state(task_id) → CompactState` deferred to Phase 10 (run-scoped memory) |
| Weekly Automation skill mining logs | Recurring agent for `verify_report append`, deferred to post-Phase-2 |

Explicitly NOT adopted (violates locked decision 1):
- LLM Satisfaction Detection
- Per-model prompt rewrites by the harness
- Mid-conversation auto model switch

## Build Order (sequencing only — no time estimates)

Status as of 2026-04-30:

| Phase | Item | Status |
|---|---|---|
| Pre-0 | v7.3 verification gate | **DONE — PASS** |
| 0 | Kernel API spec + ADRs + telemetry schema + replay fixture format | **DONE** (Phase 0 docs shipped: `docs/kernel/API.md`, `docs/kernel/telemetry.md`, ADRs 0001-0004, fixture SCHEMA.md) |
| 0.5 | Phase 1 skeleton (types, kernel.py stubs, decision_log, adapters/base, fixtures, RED stress tests) | **DONE** (per user-authorized parallelization on 2026-04-30 — kernel.py stubs land + 50 tests xfail RED + 6 fixture pairs) |
| 1 | Phase 1 implementation: fill kernel stubs + ship OH adapter + flip tests to expected-pass + delete deprecated wrappers | NEXT |
| 2 | Paired n=15 gate via OH adapter; gates per `verify_report.py` | After Phase 1 PR lands |
| 2a | Layer 1 hard-miss spike (parallel) | Independent of Phase 1; can run anytime |
| 3 | SWE-agent adapter (proves block/visible/audit on a different scaffold) | After Phase 2 PASS |
| 4 | Drift-aware replan: structural signals only (first_edit_misses_focus, root_scaffold added, graph_distance growth, repeated warnings) | After Phase 3 |
| 5 | Graph-validation-to-replan: broken signature, orphaned caller, undefined symbol → deterministic correction. Replaces test-based validation. | After Phase 4 |
| 6 | Repo-instruction priority: rank AGENTS.md / CLAUDE.md / CONTRIBUTING.md / .github/copilot-instructions.md / README test sections; surface 1-2 relevant lines, full evidence in telemetry | After Phase 5 |
| 7 | Task staging: lite_bugfix / multi_file_api_change / long_horizon modes; control surface tunes per mode | After Phase 6 |
| 8 | mini-SWE / Aider adapters | As needed for product reach |
| 9 | Cross-language adapter maturity: import resolution for the 24 tier-2 languages in gt-index | Independent track |
| 10 | Run-scoped memory + `kernel.handoff_state`: visible repo facts, prior commands, files touched, visible failures, reusable constraints. No hidden tests, no gold patches, no cross-run carryover. | After Phase 7 |
| 11 | Benchmark proof layer: per-task attribution: delivery → control → validation → outcome | Continuous alongside everything; spin up after Phase 2 |

## Success condition

GT is at full potential when:
- A single kernel codebase plugs into 4+ scaffolds via thin adapters
- Paired Wilcoxon shows non-regression (or lift) on Live-Lite n>=15 across all adapters
- Per-task proof layer can answer: was this failure caused by delivery, control, validation, or agent execution
- Same kernel works on at least 6 languages (current tier 1 + 2-3 tier 2 promoted)
- Adding a new scaffold is a one-adapter PR; not a kernel change

## What this plan explicitly does NOT build

- Our own scaffold or harness
- An LLM inside GT (kernel, adapter, or any helper)
- Any GT step that writes to the repo
- Any GT step that requires tests to exist
- Any agent-side detector inside the kernel (no `repeated_identical_actions`, no model-loop heuristics, no agent-steering)
- LLM Satisfaction Detection or any other LLM-driven evaluation
- Per-model prompt rewrites by the harness
- Mid-conversation auto model switch
- A fix for Layer 3 (agent listening) inside GT — that's the harness's problem
- Marketing words anywhere

## Operational specs (closes residual gaps from 2026-04-30 audit)

### OH SDK version pinning
Adapter declares `openhands-sdk>=<version>` in `src/groundtruth/adapters/openhands.py` module docstring AND in the OH-extra of `pyproject.toml` (`groundtruth[openhands]`). Pin is set during Phase 1 against the OH SDK release tag that exposes `ConfirmationPolicy`, `MCPToolDefinition`, and `on_event` per arXiv 2511.03690 §4.4/§4.5/§4.9. ADR 0001 records the pinned version and the class signatures the adapter depends on. CI installs the OH extra and runs the adapter contract tests against that version; bump = new ADR entry.

### Phase 2 paired-gate non-regression threshold
Phase 2 is the n>=15 paired Wilcoxon vs v7.3-only baseline (kernel-off). Default-merge requires ALL of:
- pass@1 paired delta: not statistically negative at p<0.10 (one-sided Wilcoxon signed-rank, alt = "kernel arm worse"). Non-inferiority floor.
- delivery_rate paired delta: kernel arm >= baseline - 0.05 (5pp absolute floor).
- All 13 existing `verify_report.py` strict-conjunctive gates PASS on the kernel arm.
- `degraded_capability_rate == 0` for OH adapter (any degradation indicates the OH SDK assumption broke).

Lift is not required to merge; non-regression is. Lift is reported, not gated.

### `gt_keep_rate` formula
For each task: `|set(brief.agent_focus_files) ∩ set(files_in(final_patch))| / max(|set(brief.agent_focus_files)|, 1)`. Run-level: arithmetic mean over tasks with a non-empty patch. Reported in `verify_report.py` Part 3 as a 4th report-only gate. Differs from `focus_hit_at_1` (which measures the FIRST edit only) — `gt_keep_rate` measures long-tail retention through the final patch. Implementation reads `final_patch` from `gt_output.jsonl` row, parses files via the same `files_in_patch` regex used in `brief_localization_metrics.py`. Defined in `docs/kernel/telemetry.md`.

### LSP roadmap
Deferred. `gt-resolve` is diagnostic-only per CLAUDE.md "Known Limitations #4" and stays that way through Phase 11. No scheduled phase opens it. Re-evaluate only if a Layer 1 hard-miss diagnosis (Phase 2a) traces to ambiguous edges that an LSP resolution would have eliminated. Until then, edge confidence + name-match fallback is the substrate.

### Adapter LOC budget definition
"<=200 LOC" measured as `cloc --quiet --csv src/groundtruth/adapters/<scaffold>.py | tail -1 | awk -F, '{print $5}'` (code lines only, excludes blank + comment). Excluded from the count: docstrings, type alias declarations, `__all__`, imports. Tests, fixtures, and adapter-side helpers in `adapters/_<scaffold>_helpers.py` are NOT counted toward the 200 LOC budget but each helper file has its own <=120 LOC ceiling. Total adapter footprint (file + helpers) <=320 LOC. Verifier checks via `cloc` in CI on every adapter PR.

### `runtime/repo_adapters.py` naming collision
The Python module file stays named `repo_adapters.py` to avoid grep churn across the existing codebase. In ALL docs, ADRs, kernel API surface, and conversation, refer to it as **"language profile"** (singular: `LanguageProfile`, registry: `language_profile_registry`). The module's `RepoAdapter` class gets a docstring banner: "Internally referred to as 'language profile' in docs to disambiguate from kernel-side scaffold adapters; class name preserved for backward-compatible imports." Phase 1 PR adds the docstring; no rename of the class. Future scaffold adapters (OH, SWE-agent, mini-SWE, Aider) use the term "adapter" exclusively.

## Companion docs

- `localization_docs/v7/HANDOFF_GT_TO_FULL_POTENTIAL.md` — operational handoff with parallel agent briefs (Coder / Benchmarker / Verifier). Still valid; locked decisions there match this file (this file is now the canonical 9-decision list).
- `C:\Users\Lenovo\.claude-personal\plans\whimsical-tinkering-bunny.md` — Phase 0 spec + execution plan that triggered this amendment. Contents folded in here; that file remains the execution-history record.
- `docs/kernel/API.md`, `docs/kernel/telemetry.md`, `docs/adr/0001-0004` — Phase 0 deliverables shipped.
- `tests/kernel/fixtures/SCHEMA.md` — replay fixture spec.
- `C:\Users\Lenovo\.claude-personal\projects\D--Groundtruth\memory\` — standing user feedback memories (read every spawn).
