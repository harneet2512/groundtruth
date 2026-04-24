# Expected behavior: budget accounting for hook-internal vs agent-initiated calls

Written from frozen artifact nolsp_13579/trajectory.traj.

## Artifact evidence

The frozen trajectory (4 steps) shows:
1. System prompt tells agent: "Budget per task: orient=1"
2. Agent's first action: calls gt_orient to understand the codebase
3. Response contains: "BUDGET_EXHAUSTED: gt_orient has reached its per-task cap of 1"
4. Agent submits immediately with 0 edits (confused by exhausted budget)

The cause: swe_agent_state_gt.py:2672 calls increment_tool_count("gt_orient")
during the automatic startup briefing. This consumes the 1-call orient budget
before the agent has a chance to act. The agent is promised 1 orient call but
receives 0.

## Expected behaviors

### EB-BUDGET-1: Startup briefing does not consume agent-visible orient budget

After the hook's automatic startup briefing completes:
- agent_tool_counts["gt_orient"] must be 0 (not 1)
- hook_internal_counts["gt_orient"] may be 1 (startup used it)
- The next explicit agent gt_orient call must succeed (not BUDGET_EXHAUSTED)

### EB-BUDGET-2: First explicit agent gt_orient succeeds

Given: startup briefing has already run (hook-internal)
When: agent calls gt_orient for the first time
Then: the call succeeds (returns briefing content, not BUDGET_EXHAUSTED)
And: agent_tool_counts["gt_orient"] becomes 1

### EB-BUDGET-3: Second explicit agent gt_orient fails (cap=1)

Given: agent has called gt_orient once (after startup)
When: agent calls gt_orient a second time
Then: BUDGET_EXHAUSTED (agent cap is 1, count is 1)

### EB-BUDGET-4: Hook-internal calls are still recorded in telemetry

The startup briefing should still emit a checkpoint_startup event and
be visible in telemetry. Splitting the budget does NOT mean hiding the
startup call — it means not charging the agent for it.

### EB-BUDGET-5: Budget report distinguishes internal vs agent-visible

The per-task summary and budget state should report:
- gt_orient_count (agent-initiated only) — this is what the reporter gates on
- gt_orient_internal (hook-initiated) — recorded but not gated
