# Expected behavior: scaffold mismatch classification

Written from frozen artifact nogt_12907.traj.

## Artifact evidence

nogt_12907.traj: 4 steps. First assistant response is 20,608 chars containing
43 fenced code blocks. The agent writes a full solution monologue (find, grep,
sed, test, submit) in one turn. The parser extracts only one action (the final
`submit`). The 42 intermediate actions are never executed. Result: empty patch.

## Expected behaviors

### EB-SCAFFOLD-1: classify multi-block first response as model_scaffold_mismatch

Given: a trajectory where the first assistant response contains > 5 code blocks
And: total steps <= 6
Then: classify as model_scaffold_mismatch

### EB-SCAFFOLD-2: predict empty patch from multi-block + submit

Given: first response contains multiple code blocks ending with `submit`
And: parser extracts only the last action
Then: patch will be empty (edits never executed)

### EB-SCAFFOLD-3: repaired prompt must reduce first response to <= 1 code block

Given: the same task with an action-format-repair prompt
Then: first assistant response should contain <= 2 code blocks
(This is verified by ablation B, not by a unit test)

### EB-SCAFFOLD-4: negative control — a normal multi-step trajectory is NOT mismatch

Given: a trajectory with > 10 steps and edits
And: each assistant response has 1 code block
Then: NOT model_scaffold_mismatch (normal multi-turn interaction)
