# Archived — retired SWE-agent harness tests

These tests assert the retired SWE-agent steering/ack apparatus
(`material_edit` / `ack_armed` / `steer_delivered` / `ack_engagement`,
budget/canary state machine). Their modules (`swe_agent_state_gt.py`,
`gt_canary_report.py`, `gt_tool_install.sh`) are ABSENT at HEAD — they live
only in `vm_bundle/` / per-task `groundtruth_bundle/` snapshots. The current
product is the OpenHands hook layers (post_edit / post_view / wrapper), not the
SWE-agent state machine.

Archived 2026-05-28 (user-approved retirement). Excluded from default pytest
collection via `norecursedirs` in pyproject.toml. Reversible: `git mv` back +
restore the absent modules if the steering apparatus is revived.

NOTE: CLAUDE.md's TTD section still references this apparatus's metric contract
(delivery_rate / engagement_rate). That reference is now stale vs HEAD — flagged
for the user (CLAUDE.md is the constitution; not edited autonomously).
