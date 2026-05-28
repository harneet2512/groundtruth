# Weasyprint-2300 Resolve Regression Analysis

## Summary

| | Gen6 | Cursor Rerun |
|---|---|---|
| Resolved | True | False |
| History entries | 159 | 195 |
| GT injections | 29 | 37 |
| GT types | 9 | 10 (added [TEST], [CATCHES]) |
| git_patch | Real fix to block.py + flex.py | .bak copy of flex.py only |
| Edit method | python3 -c / sed commands | str_replace_editor (failed) |

## Root Cause Classification

**PATCH_FAILURE_UNRELATED_TO_GT**

The agent failed to produce a working patch due to a mechanical tool-use error, not because GT interfered.

### Evidence

1. **Gen6 success path:** Agent used `python3 -c` commands and `sed` to make edits (not `str_replace_editor`). Produced a patch that modified `weasyprint/layout/block.py` and `flex.py`. Tests passed.

2. **Cursor rerun failure path:** Agent attempted `str_replace_editor` at e124 but received `Missing required parameters for function 'str_replace_editor': {'path'}`. This is a tool-call formatting error by the model, not a GT problem.

3. **Agent created a `.bak` file:** The git patch shows `flex.py.bak` was created (a full 1102-line copy of the original). This is the agent backing up a file before editing — but it never completed the actual edit.

4. **Task list shows incomplete work:** Gen6 TASKS.md has all 7 items checked. Cursor rerun has item 1 "in progress" and items 2-6 "pending."

5. **GT evidence was comparable or better:**
   - Gen6: 29 GT injections, 9 types
   - Cursor: 37 GT injections, 10 types (also got [TEST] and [CATCHES])
   - GT did NOT suppress any previously-delivered evidence
   - No [COMPLETENESS] in either run (correct — PRIOR-004 fix applies)
   - No vendor JS, no hidden leaks in either run

6. **No GT interference signal:** GT never told the agent to edit the wrong file, never showed wrong callers, never showed misleading contracts. The additional evidence (tests, catches) was correct and relevant.

## Conclusion

This is **model nondeterminism / tool-use failure**. DeepSeek chose a different editing strategy in the cursor rerun (str_replace_editor vs python3 -c) and made a tool-call formatting error. GT evidence was comparable or better in the cursor rerun.

GT changes in the cursor-mode commit chain did NOT cause this regression.

### Supporting evidence for nondeterminism

- Both runs used the same model (deepseek-v4-flash), same maxiter (100)
- The agent took fundamentally different approaches: gen6 used command-line editing, cursor rerun attempted IDE-style editing
- The failure is a missing `path` parameter in a tool call — a model formatting error unrelated to GT context

## Impact on Benchmark Gate

- Architecture-quality gate: **PASSED** (PRIOR-004 fixed, no under-delivery, no truth bugs)
- Resolve-rate gate: **regression explained** — model tool-use failure, not GT interference
- Recommendation: this regression does NOT block the cursor-mode architecture changes
