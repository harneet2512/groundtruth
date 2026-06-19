# Potential Problems — Unresolved

## 1. Edit-target class scoring fix doesn't fire because is_exported filter

**Symptom:** pypsa edit-target still shows `Network()` (97 callers) despite class scoring fix (+200 instead of +1000 for Classes).

**Root cause hypothesis:** The edit-target query filters `is_exported = 1`. Python functions like `expanded_capacity()` may not be marked `is_exported=1` by the Go indexer (Python doesn't have a concept of "exported" like Go does). If `expanded_capacity` is filtered out by `is_exported = 1`, it never becomes a candidate, so the class scoring fix is irrelevant.

**Evidence:**
- `[GT_META] l1_issue_symbol_files` found `pypsa/statistics/expressions.py`
- `[GT_META] l1_enhanced: edit_target=Network` — Network still won
- The query: `SELECT id, name, label, signature, start_line FROM nodes WHERE ... AND is_exported = 1 AND is_test = 0`
- Python functions may all be `is_exported = 0` because the Go indexer's export detection is designed for Go (capitalized names) and Java (public keyword)

**Fix needed:** Either:
- Remove `is_exported = 1` filter for Python files (Python has no export concept — everything at module level is importable)
- Or change the Go indexer to mark Python module-level functions as exported

**Impact:** HIGH — this blocks the class-vs-function scoring fix from having any effect on Python repos, which are the majority of SWE-bench tasks.

## 2. 0/13 flips on 13-task smoke

**Symptom:** All session changes (noise reduction, dedup, class scoring, dynamic pattern gate, MISMATCH fix, L5 nudge, tool removal) produced 0 new flips. 4 baseline holds, 0 regressions.

**Interpretation:** Noise reduction prevents harm but doesn't cause resolution. The changes are defensive (Cursor-like silence) but not offensive (agent writes correct fix it wouldn't have otherwise).

**What would actually cause flips:**
- pypsa: agent found the fix commit at entry 91 but never applied it. 67 actions in reproduction loop.
- cfn-lint: agent found the right file at entry 199 of 203. Ran out of iterations.
- Both: wrong edit-target wasted 80-97 actions. If edit-target worked, agent would have 80+ actions to fix.

**Blocker:** is_exported filter (problem #1) prevents correct edit-target on Python repos.

## 3. Dynamic pattern gate: untested at scale

**Status:** Implemented (CodePlan change-may-impact approach). Uses obligation_check shared-state to gate [PATTERN].

**Risk:** obligation_check uses Python AST parsing, which only works on Python files. For Go/JS/TS repos, it returns empty → [PATTERN] never fires. This is acceptable for SWE-bench (Python-only) but not for the generalized product.

## 4. Reindex gate scope: only resets edited file

**Status:** Changed from resetting ALL l3b_file gates to only the edited file.

**Risk:** If editing file A changes the callers of file B (e.g., A adds a new call to B), B's L3b gate should also reset. Currently it doesn't. The agent would need to re-read B to get updated callers, but the gate blocks re-delivery.

**Mitigation:** The hash-based dedup safety net would catch genuinely different content. But if the content didn't change (same callers), the suppression is correct.
