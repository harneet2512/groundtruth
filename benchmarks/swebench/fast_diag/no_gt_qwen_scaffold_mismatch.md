# No-GT Qwen Scaffold Mismatch Analysis — 2026-04-24

## Finding

Fresh no-GT Qwen3-Coder-480B baseline on the frozen astropy-10 suite produces
0/10 resolved, 1/10 patched, 9/10 zero-edit. The failure is not model inability
— it is **model_scaffold_mismatch**: Qwen emits a full solution in a single
turn with dozens of code blocks, but SWE-agent's `thought_action` parser
executes only one action per turn.

## Evidence (from frozen artifact nogt_12907.traj)

- First assistant response: **20,608 characters**
- Code blocks in first response: **43**
- Response structure: THOUGHT → find → grep → sed edit → python test → submit
- Parser behavior: extracts ONE action (the last `submit`)
- Result: `submit` runs `git diff` → empty diff → 0-byte patch
- The 42 intermediate actions (find, grep, sed, test) are never executed

## Per-task classification

| task | steps | exit | first_response | classification |
|---|---|---|---|---|
| 12907 | 4 | submitted | 43 blocks, 20608 chars | model_scaffold_mismatch |
| 13033 | 4 | submitted | multi-block | model_scaffold_mismatch |
| 13236 | 4 | submitted | multi-block | model_scaffold_mismatch |
| 13398 | 4 | submitted | multi-block | model_scaffold_mismatch |
| 13453 | 4 | exit_format | 0 blocks | action_parser_failure |
| 13579 | 4 | submitted | multi-block | model_scaffold_mismatch |
| 13977 | 34 | submitted | multi-block (partial) | model_scaffold_mismatch |
| 14096 | 4 | submitted | multi-block | model_scaffold_mismatch |
| 14182 | 104 | submitted | normal interaction | (only task that worked) |
| 14309 | 4 | submitted | multi-block | model_scaffold_mismatch |

8/10 tasks die on the first turn (steps=4). The agent writes a complete
solution monologue, the parser picks one action (usually the final `submit`),
and the task ends with an empty patch.

## Why this invalidates both baselines

**Historical baseline (5/10):** Cannot be reproduced with the current
scaffold/model/runner. Unknown whether the historical run used a different
SWE-agent version, different Vertex endpoint, different parser, or different
prompt. Not a valid comparison target.

**Fresh no-GT baseline (0/10):** Not a fair intelligence comparator. The 0/10
result measures parser compatibility, not model coding ability. Qwen3-Coder
CAN solve these tasks (its monologue response for 12907 contains a correct
analysis) — it just emits the solution in a format the scaffold can't execute.

## What the GT condition does

The GT condition includes prompt/scaffold stabilization that constrains Qwen
into one-action turns:

1. System prompt includes `PARSING RULE (CRITICAL): Emit exactly ONE fenced
   bash code block when taking an action.`
2. GT's hooks inject `<gt-evidence>` blocks after each action, creating a
   forced interaction rhythm that prevents monologue responses.
3. The GT startup briefing occupies the first turn, structuring the agent's
   entry into the task.

The observed lift from 0/10 → 3/10 (and 1/10 → 7/10 patched) may come from
scaffold stabilization, GroundTruth code intelligence, or both. Ablations
are required to isolate the contribution.

## Required ablations

| ID | Condition | GT evidence | One-action prompt | GT tools |
|---|---|---|---|---|
| A | no_GT_raw | NO | NO | NO |
| B | no_GT_action_format_repair | NO | YES | NO |
| C | no_GT_action_format_repair + test prompt | NO | YES | NO |
| D | GT_prompt_shell_only (scaffold, no intelligence) | NO | YES | YES (empty) |
| E | GT_budget_split_current | YES | YES | YES |

If B or D approaches E on patch/resolution, the lift is scaffold stabilization.
If E beats B/D on resolution, GT intelligence may be adding value.
