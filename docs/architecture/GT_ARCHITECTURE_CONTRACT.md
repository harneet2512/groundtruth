# GT Architecture Contract

This document defines the structural invariants for the GroundTruth Decision Interface. Code changes that violate these invariants are rejected regardless of test count.

---

## 1. Lifecycle Surfaces

Three surfaces. No more, no fewer.

| Surface | When | Agent can respond? | Required |
|---|---|---|---|
| `task_map` | Pre-task — before first file read or edit | Yes — agent has not started working | Yes |
| `event_brief` | During work — after a file is edited | Yes — agent is mid-task, can revise | Yes |
| `review_patch` | Pre-submit — before the agent's final submission | Yes — agent can fix or ACK before submitting | Yes |

### 1.1 Completeness rule

A surface is **complete** only if the agent receives the output AND can take action based on it before the outcome is locked.

- Pre-task: output must be prepended to or available before the agent's first action.
- During work: output must be appended to the command result the agent sees.
- Pre-submit: output must arrive before the agent's submission is captured. The agent must be able to edit, fix, or ACK after receiving it.

### 1.2 Post-run is telemetry

Any surface that fires after `agent.run()` returns is **telemetry**, not **decision support**. It must be labeled as such in code and metadata. It does not count toward "surface is active."

`review_patch` firing post-run is a known gap. It must be moved into the submit path (intercepting the agent's submit command or adding a pre-submit step to the agent loop) before vNext can be called complete.

---

## 2. Finding Schema

All GT signals normalize to `Finding`. No other output schema for agent-facing content.

### 2.1 Required fields

Every Finding must have:
- `kind` — closed enum from `FindingKind`
- `severity` — error | warning | note
- `confidence` — float 0.0–1.0
- `location.file` — non-empty string
- `location.line` or `location.symbol` — at least one
- `message` — non-empty, one-line imperative
- `why_now` — file_opened | file_changed | patch_ready | always
- `agent_action` — fix_required | verify | read | acknowledge

### 2.2 Agent-facing output

Text with structural markers: `[TIER] [kind] message @ file:line (confidence) — ACTION`

Wrapped in `<gt-evidence surface="SURFACE_NAME">...</gt-evidence>`.

### 2.3 Prohibited output

- No `reasoning_guidance` footer appended to surface output.
- No `[OK] No findings` noise. Empty findings = empty string.
- No `<gt-evidence>` wrapper around empty content.
- No cross-tool pointers ("Call groundtruth_X").
- No `safe_changes` / `unsafe_changes` static lists.

---

## 3. Novelty Suppression

### 3.1 Rule

A finding shown in surface N must not be repeated in surface N+1 unless the underlying code changed between the two calls.

### 3.2 Identity

Finding identity = `(kind, location.file, location.line, location.symbol)`. Same identity across surfaces = same finding.

### 3.3 Implementation

`NoveltyFilter` is per-session (MCP) or per-container (hook harness). Shared across all three surfaces within one task.

---

## 4. Benchmark Validity Gates

### 4.1 Required arms

No benchmark result is valid without at least:

| Arm | Purpose |
|---|---|
| **B** — format-repaired baseline | Proves the scaffold works without GT intelligence |
| **shell-only** — scaffold-only control | Proves GT delivery mechanism alone doesn't help |
| **F-noLSP** — vNext no-LSP | The system under test |

### 4.2 Prohibited comparisons

- Do not compare against raw broken Qwen (scaffold-broken).
- Do not claim GT intelligence lift from `n < 30`.
- Do not run full benchmark (500 tasks) until `n=30–50` shows stable signal.

### 4.3 Success criteria

Success requires behavioral evidence, not just resolved count:
- `decision_changed_vs_B` — at least one task where GT finding changed the agent's action
- `repeated_signal_rate` decreased vs current GT
- `findings_fixed_or_acknowledged` > 0 in review_patch
- No regression in `resolved` vs format-repaired baseline

---

## 5. TTD Artifact-First Requirement

Tests must come from frozen artifacts (traces, gold patches, regressions), not from reading the implementation.

- Red before green.
- Negative controls required.
- Mutation checks: if the fix is reverted, the test must fail.
- Do not classify a FAIL as a model behavior failure until the metric contract is proven correct.

---

## 6. No-AI-Layer Constraint

All Finding production must be deterministic. No LLM calls in the Finding pipeline.

- AST index, import graph, ripgrep/git-grep, diff parser, obligations, contradictions — all deterministic.
- AI layer (anthropic) is optional for briefing/semantic resolution but never for Finding emission.
- If a Finding requires an LLM to produce, it is not a Finding — it is a suggestion, and it does not use the Finding schema.

---

## 7. No-Hard-Restriction Constraint

GT must not block agent actions. No file-edit gates, no forced-stop on high-severity findings.

Binding awareness means: the agent may proceed, but it cannot silently ignore a `fix_required` finding. The mechanism is structural markers in the output, not tooling gates.

---

## 8. Binding Awareness

For findings with `agent_action == fix_required`:

- The `review_patch` surface appends a `BINDING: N finding(s) require explicit fix or ACK before submit.` footer.
- The agent is expected to either fix the issue or include `ACK_GT` with a reason.
- This is a communication convention, not an enforcement mechanism.

---

## 9. Contract Test Requirements

The following tests must exist and pass before vNext is considered complete:

| Test | What it proves |
|---|---|
| `test_review_patch_agent_can_respond` | review_patch output reaches the agent before submission is captured |
| `test_no_surface_complete_without_agent_response` | every surface's output is delivered where the agent can act on it |
| `test_no_empty_gt_evidence_wrapper` | empty findings produce empty string, not `<gt-evidence></gt-evidence>` |
| `test_no_reasoning_guidance_footer` | no `---\n` + guidance appended to surface output |
| `test_repeated_findings_suppressed` | second call to same surface with same data produces zero findings |
| `test_benchmark_requires_baseline_arms` | benchmark runner refuses to start without B and shell-only arms configured |
| `test_finding_has_required_fields` | every Finding has all fields from §2.1 |
| `test_no_ok_noise` | surfaces return `""` when no findings, never `[OK]` |
| `test_post_run_labeled_telemetry` | any post-run surface call is tagged as telemetry in metadata |

---

## 10. Versioning

This contract is versioned by git. Changes require updating the contract first, then the code.

Current status: **review_patch is post-run only (telemetry, not decision support)**. vNext is not complete until this gap is closed.
