# GT A/B 5-Smoke Full Utilization Spec

Date: 2026-04-16
Repo: `D:\Groundtruth`
Conditions:
- `gt-nolsp`
- `gt-hybrid`

## Purpose

Define the engineering behavior, confidence policy, and verification metrics for a 5-task smoke A/B run where the goal is not only task success, but full and measurable GT utilization.

This spec is about the behavior of three things:
- GT tools
- hooks
- acknowledgment

It applies to both arms of the A/B test. The only intended difference between arms is the LSP promotion path.

## Definitions

### GT tools

The canonical GT tool surface for the smoke run is:
- `gt_orient`
- `gt_lookup`
- `gt_impact`
- `gt_check`

These tools may be exposed as shell commands, MCP tools, or both. The smoke run is valid only if the agent can actually invoke them and the harness can count them.

### Hooks

Hooks are deterministic lifecycle automation. They run after agent actions and are responsible for:
- always-on telemetry
- material edit detection
- `GT MICRO` emission when confidence clears threshold
- `GT VERIFY` emission on presubmit and periodic edit triggers
- suppression logging

Hooks are not the same thing as GT tools. Hooks should not be treated as optional suggestions.

### Acknowledgment

Acknowledgment is not a keyword-overlap heuristic. It is a trace-level behavioral metric:
- did the next turn notice GT evidence?
- did it change behavior because of the evidence?
- did it choose a GT-relevant action or safer edit because of the evidence?

If acknowledgment is not observable, it must be reported as `not_observed`, not guessed.

## A/B Invariants

Both arms must share the same:
- task set
- model
- step and cost budgets
- prompt templates except for the explicit LSP toggle
- GT hook semantics
- GT telemetry schema
- analysis scripts
- output caps

Allowed delta:
- `gt-hybrid` may enable LSP promotion.
- `gt-nolsp` must not use LSP promotion.

Everything else must be parity-checked and fail the run if it drifts.

## Required Behavioral Contract

### 1) GT tool utilization

The model should behave as if GT tools are the default path for codebase grounding.

Expected behavior:
- `gt_orient` happens at the start of a task or first grounding moment.
- `gt_lookup` happens when symbol identity, call sites, or file ownership are uncertain.
- `gt_impact` happens before risky edits to externally-called functions, interfaces, or cross-file contracts.
- `gt_check` happens after material edits and before submit when validation matters.

What full utilization means:
- GT tools are not merely available.
- They are visible in trajectories.
- They are used on tasks where they add value.
- Their usage is measurable per task and per arm.

### 2) Hook utilization

Hooks should fire on every lifecycle cycle when the state hook is present and healthy.

Expected hook behavior:
- every cycle logs telemetry
- material edit cycles are detected
- evidence is emitted only when the trigger and confidence gates pass
- suppression is logged when evidence is withheld

What full utilization means:
- telemetry is present even when evidence is absent
- `GT MICRO` and `GT VERIFY` are visible in traces for the tasks that qualify
- no silent hook failure

### 3) Acknowledgment behavior

A successful acknowledgment pattern is:
- hook evidence appears
- the next reasoning/action step reflects that evidence
- the agent either narrows scope, checks the risky symbol, or changes the edit plan

Non-successful patterns:
- evidence appears but the next turn ignores it
- evidence appears but the agent repeats the same mistake
- acknowledgment is inferred only from generic words like "GT" with no actual behavioral change

## Confidence Gating

Confidence gates apply to emitted content, not to telemetry.

### Confidence levels

- `verified`
  - direct structural support
  - safe to present as actionable guidance
  - can influence hard constraints

- `likely`
  - supported enough to guide work
  - should be framed as caution or recommendation
  - may influence edit planning

- `tentative`
  - useful for internal scoring
  - should not be surfaced as fact
  - may be logged in telemetry only

- `suppressed`
  - stale, duplicated, contradictory, or too weak
  - do not surface the content
  - do log the suppression reason

### What confidence gating must prevent

- stale graph data being stated as current fact
- ambiguous call paths being presented as verified
- repeated identical evidence from flooding the model
- low-signal hooks being promoted into hard constraints

### What confidence gating must allow

- strong evidence to surface promptly
- weaker evidence to surface as a caution when it is still useful
- telemetry to always capture the decision path

## Hook Policy

### Material edit definition

A material edit is a source edit that changes the code meaningfully enough to affect file content hash or structural diff.

### GT MICRO policy

Emit `GT MICRO` when all of the following hold:
- there was a material edit
- the edit has enough novelty to avoid duplicate spam
- confidence is at least `likely`
- the content can be kept bounded

Micro requirements:
- short
- specific
- action-shaped
- no stale certainty
- no repeated identical guidance more than the configured dedup window

### GT VERIFY policy

Emit `GT VERIFY` when any of the following hold:
- pre-submit
- every 3rd material edit
- loop-risk trigger

Verify requirements:
- deterministic trigger
- budgeted
- deduped
- confidence-gated
- never overrides stronger truthfulness policy

### Telemetry policy

Telemetry must always include:
- cycle count
- material edit detection
- emit/suppress decision
- suppression reason
- follow/ignore outcome when applicable
- step-limit or budget-limit transitions

## Full Utilization Stats

The smoke run must report these metrics for both arms.

### Base metrics

- `tasks_total`
- `tasks_with_material_edit`
- `resolved_count`
- `submitted_count`
- `unresolved_count`
- `empty_patch_count`
- `one_step_traj_count`

### GT tool metrics

- `gt_orient_count`
- `gt_lookup_count`
- `gt_impact_count`
- `gt_check_count`
- `gt_orient_rate`
- `gt_lookup_rate`
- `gt_impact_rate`
- `gt_check_rate`

Recommended interpretation:
- `gt_orient_rate` should be near 1.0 for healthy runs
- `gt_check_rate` should be near 1.0 on tasks with material edits
- `gt_lookup_rate` and `gt_impact_rate` should rise only when those decisions are actually needed

### Hook metrics

- `hook_cycle_count`
- `material_edit_count`
- `micro_emit_count`
- `micro_suppress_count`
- `verify_emit_count`
- `verify_suppress_count`
- `telemetry_present_count`
- `ack_followed_count`
- `ack_ignored_count`
- `ack_not_observed_count`
- `ack_rate = ack_followed_count / (ack_followed_count + ack_ignored_count)`
- if `ack_followed_count + ack_ignored_count = 0`, treat `ack_rate` as `N/A` and do not block gating on C3 alone

### Confidence / suppression metrics

- `verified_emit_count`
- `likely_emit_count`
- `tentative_internal_count`
- `suppression_exact_dedup`
- `suppression_window_dedup`
- `suppression_compliance`
- `suppression_stale`
- `suppression_no_signal`

### Hybrid-only metrics

- `lsp_promotion_attempt_count`
- `lsp_verified_count`
- `lsp_ambiguous_count`
- `lsp_unresolved_count`
- `lsp_stale_count`
- `lsp_cache_hit_rate`
- `lsp_cache_miss_rate`
- `lsp_warmup_latency`
- `lsp_added_checkpoint_latency`

## 5-Smoke Run Design

### Required experiment shape

Run a 5-task smoke on each arm:
- 5 tasks for `gt-nolsp`
- 5 tasks for `gt-hybrid`

The task list must be identical across arms.

### Smoke goals

The smoke run should prove:
- the harness is wired correctly
- the GT tools are actually being utilized
- hooks fire and emit telemetry
- acknowledgment can be observed in traces
- hybrid promotion is real and measurable

### Smoke run validity checks

A smoke run is invalid if any of these happen:
- state hook is not patched
- telemetry is missing
- GT tools are unavailable or never used
- hybrid promotion never fires in the hybrid arm
- acknowledgment cannot be measured
- the run differs between arms outside the LSP toggle

## Failure Handling

### Missing hook patch

Behavior:
- treat as a hard harness failure
- telemetry should still identify the missing patch path if possible
- do not count the run as valid for hook metrics

### Stale or missing graph data

Behavior:
- suppress strong claims
- emit only safe fallback guidance
- log freshness/staleness status
- do not invent certainty

### GT tool unavailability

Behavior:
- shell path missing -> use MCP path if available
- MCP path missing -> use shell path if available
- both missing -> run is invalid for GT utilization

### Hybrid promotion failure

Behavior:
- if promotion is unavailable in `gt-hybrid`, the arm is not hybrid in practice
- record the failure
- do not pretend parity with baseline

### Acknowledgment failure

Behavior:
- if the next turn does not react to evidence, record `not_observed`
- do not infer acknowledgment from weak keywords
- do not convert a non-reaction into a success

### Budget and step exhaustion

Behavior:
- log the exhaustion point
- preserve telemetry
- submit only if the harness requires it
- do not continue producing unbounded evidence

## Rollout Plan

### Phase 1: behavior parity
- make sure both arms expose the same GT behavior except LSP
- verify hook and tool telemetry in both arms

### Phase 2: smoke-run enforcement
- run the 5-task smoke on both arms
- collect metrics and compare against the utilization contract

### Phase 3: acknowledgment grading
- replace or supplement heuristic acknowledgment with trace-grade grading

### Phase 4: temporal precedent upgrade
- upgrade precedent retrieval to a temporal walk if it improves evidence quality
- keep the family label stable unless metrics justify a rename

### Phase 5: hardening
- add failure gates for missing hook, missing telemetry, and missing promotion in hybrid

## Acceptance Criteria

The work is successful only if:
- both arms pass the same 5-task smoke structure
- `gt_orient`, `gt_check`, `GT MICRO`, and `GT VERIFY` are visible where expected
- `gt_lookup` and `gt_impact` appear when their decision points arise
- telemetry is present on every cycle
- acknowledgment is measurable from trace behavior
- hybrid promotion is non-zero in `gt-hybrid`
- suppression is logged and explainable
- no run is declared successful if hook or telemetry wiring is broken
