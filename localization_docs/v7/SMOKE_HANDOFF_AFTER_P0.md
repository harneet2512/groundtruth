# GT v7 P0 Smoke Handoff

Date: 2026-05-01

## Purpose

Run the next GT v7 smoke to answer one question:

Did compact, ranked GT focus cause the agent to edit the recommended files earlier and more often, without increasing bad patch shapes?

Do not judge this run by pass@1 alone. Pass/fail is still useful, but the immediate gate is product delivery and adherence.

## Current State

P0 alignment remediation is implemented:

- `gt_plan` is compact by default for CLI and MCP.
- `gt_plan --full` and MCP `gt_plan(full=True)` are diagnostic-only full JSON surfaces.
- Agent-facing compact plan excludes `cluster_files`.
- `gt_plan_served` telemetry records compact vs full, served keys, char count, and broad-plan exposure.
- `gt_usable_delivery` telemetry records whether GT delivery was usable, not just transported.
- `gt_patch_shape` records focus adherence metrics:
  - `agent_focus_files_touched`
  - `edited_ranked_focus_files`
  - `brief_edit_overlap`
  - `focus_hit_at_1`
  - `focus_hit_at_3`
  - `focus_edit_precision`
- `gt_report` leads with:
  - `transport_delivery_rate`
  - `usable_delivery_rate`
  - `adherence_rate`
  - `outcome_rate`
  - focus metrics
- `cluster_touch_rate` is now secondary diagnostics.
- `gt_run_tests --execute` logs `gt_test_validation` for visible repo-native commands.

Verified locally:

```powershell
python -m compileall -q src\groundtruth\runtime src\groundtruth\pretask src\groundtruth\cli src\groundtruth\mcp
python -m pytest tests\pretask tests\unit\test_runtime_patch_auditor.py tests\unit\test_v7_plan_surface.py tests\unit\test_mcp_server.py tests\unit\test_mcp_tools.py -q
python -m ruff check src\groundtruth\runtime\plan_surface.py src\groundtruth\cli\commands.py src\groundtruth\main.py src\groundtruth\mcp\server.py src\groundtruth\runtime\patch_auditor.py src\groundtruth\runtime\report.py src\groundtruth\runtime\test_runner.py src\groundtruth\pretask\v7_brief.py tests\unit\test_v7_plan_surface.py tests\unit\test_runtime_patch_auditor.py tests\pretask\test_v7_brief.py
```

Last result:

- `99 passed`
- Ruff: `All checks passed`

## Pre-Smoke Sanity Checks

Before launching a paired smoke, run:

```powershell
python -m pytest tests\pretask tests\unit\test_runtime_patch_auditor.py tests\unit\test_v7_plan_surface.py -q
```

Expected:

- all tests pass
- compact plan tests prove default output excludes `cluster_files`
- usable-delivery gate rejects broad or oversized payloads
- patch auditor distinguishes cluster touch from focus miss

Optional manual check:

```powershell
python -m groundtruth.main gt_plan --plan <TASK>_v7_plan.json
python -m groundtruth.main gt_plan --plan <TASK>_v7_plan.json --full
```

Expected:

- default output contains only:
  - `task_id`
  - `confidence`
  - `abstain_reason`
  - `agent_focus_files`
  - top `contract_lines`
  - top `constraints`
  - top `expected_side_files`
  - `full_plan_available`
- default output does not contain `cluster_files`
- `--full` output does contain `cluster_files`

## Recommended Smoke

Start with the same n=10 paired smoke used before P0. Keep task set, model, harness, and timeouts as close to the prior run as possible.

Reason:

- The goal is to isolate whether compact ranked delivery changes behavior.
- Changing task sample or scaffold at the same time makes the result harder to interpret.

Do not run n=50 until n=10 shows the telemetry is being captured correctly.

## Smoke Metrics To Collect

For every task, collect:

- resolved or not resolved
- patch produced or empty
- root scaffold files added
- source files touched
- `transport_delivered`
- `usable_delivery_ok`
- `failure_reasons`
- `agent_focus_count`
- `brief_chars`
- `brief_file_mentions_count`
- `agent_focus_files_touched`
- `edited_ranked_focus_files`
- `brief_edit_overlap`
- `focus_hit_at_1`
- `focus_hit_at_3`
- `focus_edit_precision`
- `cluster_touch_rate`
- first gold edit step, if available
- first focus edit step, if available
- first message chars, if harness capture is available

Use `gt_report` after the run:

```powershell
python -m groundtruth.main gt_report --run-dir <RUN_DIR> --json <RUN_DIR>\gt_full_form_report.json --md <RUN_DIR>\gt_full_form_report.md
```

## Go/No-Go Criteria For n=50

Proceed to n=50 only if:

- `usable_delivery_rate >= 0.95`
- no broad full-plan JSON is served by default
- focus hit@3 improves over old v7
- `brief_edit_overlap` improves over old v7
- root scaffold rate does not increase
- empty patch rate does not increase materially
- first-focus or first-gold latency does not regress materially

Do not require pass@1 improvement for this n=10 gate. n=10 is too noisy for that. Require delivery and adherence improvement.

## Failure Interpretation

If `transport_delivery_rate` is high but `usable_delivery_rate` is low:

- GT is reaching the agent, but payload shape is still wrong.
- Inspect `gt_usable_delivery.failure_reasons`.

If `usable_delivery_rate` is high but focus metrics are flat:

- GT is compact but not persuasive/actionable enough.
- Next work should target project instructions, stronger repair constraints, and hook visibility.

If focus metrics improve but outcome does not:

- GT is influencing edits but not yet influencing repair quality.
- Next work should target visible test validation, replan, and instruction extraction.

If cluster touch is high but focus hit@3 is low:

- The agent is editing broad nearby files, not ranked GT targets.
- Treat as an adherence failure.

## Full-Form GT Formulation After Smoke

Use the smoke result to decide the next full-form layer.

P1 work started while the smoke is running:

- Project instruction extraction is implemented as a language-neutral pretask module.
  - Full evidence logs to `gt_project_instructions`.
  - Only compact constraints are merged into the brief/plan constraints.
  - Scoped instruction files take precedence over repo-root instruction files; README test sections are lower precedence.
- Hook truthfulness normalization is implemented for reports.
  - Reports now distinguish `hook_logged`, `hook_visible_to_agent`, `hook_blocked`, and `final_audit_only`.
  - New hook logs explicitly mark those fields; legacy hook logs are normalized from available output/blocking evidence.

Remaining P1 build order:

1. Project instruction extraction
   - done for deterministic extraction and telemetry
   - remaining: tune relevance after smoke results

2. Hook truthfulness and visibility
   - base fields and report aggregation are done
   - remaining: harness-specific confirmation that visible hook output is surfaced in OpenHands/SWE-agent observations

3. Repair validation loop
   - use `gt_run_tests --execute` selectively
   - feed visible failures into `gt_patch_shape` and replan
   - keep hidden tests forbidden

4. Drift-aware replan
   - trigger when edits miss focus files
   - trigger when tests fail after a focus edit
   - trigger when patch shape is root-scaffold or tests-only

5. Benchmark adapters
   - JS/TS, Go, Rust, Java test and side-file conventions

## Operator Notes

Keep full evidence in telemetry. Keep agent-facing payloads compact.

For this smoke, the primary metric is not whether GT talked. It is whether GT delivered a compact ranked plan and the agent followed it.
