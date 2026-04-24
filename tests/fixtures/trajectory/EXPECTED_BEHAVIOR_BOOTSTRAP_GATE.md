# Expected behavior: bootstrap pre-smoke gate

Written from frozen artifacts nolsp_13453/ and nolsp_13579/.

## Artifact evidence

nolsp_13453: 4 telemetry events, cycle=1, 0 material edits, 0 patch bytes.
nolsp_13579: 3 telemetry events, cycle=1, 0 material edits, 0 patch bytes.
Both tasks terminated before the agent reached any exploration or editing phase.
6/10 nolsp tasks showed this pattern on the Qwen3-Coder smoke.

## Expected behaviors

### EB-GATE-1: A task with 0 material edits at cycle <= 2 is a bootstrap failure

Given: telemetry has max_cycle <= 2 and 0 material_edit events
Then: the task is classified as bootstrap_failure
This is already handled by trajectory_classifier (failure_class=bootstrap_infra_failure).

### EB-GATE-2: bootstrap_failure_rate above threshold invalidates the arm

Given: a 10-task arm where >= 3 tasks are bootstrap_failure (rate >= 0.30)
Then: the arm is marked invalid for comparison against baseline
The arm's resolve count must not be used in A/B claims.

### EB-GATE-3: bootstrap_failure_rate below threshold passes the gate

Given: a 10-task arm where <= 1 task is bootstrap_failure (rate <= 0.10)
Then: the arm passes the bootstrap gate

### EB-GATE-4: the gate runs BEFORE any smoke comparison is reported

The gate must be checked after the reporter runs but before verify_report
produces a verdict. If the gate fails, the verify_report verdict is
"INVALID (bootstrap)" not "FAIL".
