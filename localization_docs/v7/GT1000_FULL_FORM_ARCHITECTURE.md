# GT 1000 Full-Form Architecture

Date: 2026-05-01

## Thesis

GT 1000 is not a larger retrieval payload. It is a benchmark execution control
system.

The core product loop is:

1. classify the repair shape
2. deliver compact ranked focus
3. observe agent behavior
4. intervene truthfully when behavior drifts
5. validate with visible repo-native evidence
6. replan from observed evidence
7. report delivery, adherence, validation, and outcome separately

## Research Grounding

- CodeRAG-Bench: retrieval helps only when context is high quality and usable;
  broad context can fail to improve generation.
- SWE-Bench Mobile: agent scaffold and control design can create large
  performance spread; simple defensive guidance can beat complex prompts.
- SWE-ContextBench: experience and memory are useful only when representation,
  retrieval quality, and leakage controls are disciplined.
- Claude Code hooks: hook lifecycle and output semantics determine whether
  feedback is visible, blocking, or only logged.
- Codex AGENTS.md behavior: scoped repository instructions are a first-class
  control surface.

## Product Layers

### 1. Pre-Task Brain

Purpose: decide what the agent should do first.

Components:

- task complexity classifier:
  - `lite_bugfix`
  - `multi_file_api_change`
  - `long_horizon`
- ranked focus files, capped for the first stage
- compact contract lines
- expected side files
- repo instruction extraction
- validation plan

Rules:

- Never dump full `cluster_files` to the default agent-facing surface.
- Keep full evidence in telemetry.
- Render only the first-stage plan until the agent earns expansion.

### 2. Runtime Nervous System

Purpose: know what the agent actually did.

Telemetry:

- files viewed
- files edited
- first focus edit step
- focus hit@1 and hit@3
- brief edit overlap
- root scaffold additions
- tests-only patches
- expected side files missing
- hook visibility/blocking state

Rules:

- Do not claim influence unless the hook was visible or blocking.
- Separate `hook_logged`, `hook_visible_to_agent`, `hook_blocked`, and
  `final_audit_only`.

### 3. Control Policy

Purpose: decide whether to stay quiet, warn, block, or audit.

Current implementation:

- `src/groundtruth/runtime/control_policy.py`

Inputs:

- `gt_patch_shape`
- `gt_replan`
- `gt_test_validation`
- hook capability (`hook_can_block`)
- final audit mode

Outputs:

- `severity`: `pass`, `warn`, `block`, or `audit`
- compact intervention message
- next actions
- visibility/blocking truth fields

Policy:

- Stay quiet when the patch is on plan.
- Warn on moderate drift.
- Block only high-risk benchmark failure patterns when the hook/harness can
  actually feed the message back to the agent.
- Never use hidden tests or gold patches.

### 4. Validation-To-Repair Loop

Purpose: make GT influence repair quality, not just file finding.

Inputs:

- selected visible repo-native tests
- command exit code
- parsed failing test names
- stdout/stderr tail
- timeout state

Behavior:

- If tests fail, convert failure names and relevant output into compact next
  actions.
- Replan only from visible evidence.
- Avoid expanding context unless the current focus path is disproven.

### 5. Memory With Leakage Controls

Purpose: reuse useful run-scoped facts without contaminating benchmarks.

Allowed:

- visible repo facts
- prior commands
- files touched
- visible failures
- reusable repo constraints

Forbidden:

- hidden tests
- gold patches
- cross-run carryover by default
- leaderboard/eval labels

Memory is P2. It should come after the control loop is stable; otherwise memory
can amplify bad policy.

## GT 1000 Build Order

1. Compact delivery and focus adherence
   - done on `gt-v7-p0-p1-alignment`

2. Repo instruction extraction and hook truthfulness
   - done on `gt-v7-p0-p1-alignment`

3. Focus drift replan
   - started on `gt-fullform-drift-validation`

4. Control policy
   - started on `gt-fullform-drift-validation`

5. Harness-specific visible/blocking integration
   - next: OpenHands wrapper
   - then: SWE-agent wrapper

6. Validation-to-repair messages
   - next after harness visibility is confirmed

7. Task complexity staging
   - after n=10/n=50 shows where compact focus underfits

8. Language adapters
   - JS/TS, Go, Rust, Java

9. Run-scoped memory
   - only after leakage guardrails are tested

10. Benchmark dashboard
   - delivery, adherence, validation, outcome, influence, and failure taxonomy

## What 1000 Means

GT 1000 can answer these questions for every benchmark task:

- Did GT arrive?
- Was it usable?
- Did the agent see it?
- Did the agent follow it?
- Did GT intervene when the agent drifted?
- Was the intervention visible or blocking?
- Did visible validation pass?
- Did following GT correlate with outcome?
- If GT failed, did it fail at retrieval, delivery, adherence, validation, or
  repair control?

That is the difference between a context tool and a benchmark execution control
system.
