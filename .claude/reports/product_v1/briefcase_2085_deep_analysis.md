# Deep Analysis: briefcase-2085 Failure

## What the agent did (chronological)

1. **Read issue, understood the problem correctly** (Think entry 16): `remote.set_url(new_url=template, old_url=remote.url)` fails when git has `insteadOf` rules because `remote.url` returns the transformed URL, not the stored URL.

2. **First edit (entry 49)**: Removed `old_url=remote.url` from `set_url()` call. Simple fix: just set the URL without checking the old one. **This is the correct approach per the gold patch.**

3. **Ran tests → 4 tests failed** (entry 52, exit code 1). The tests still assert `mock_remote.set_url.assert_called_once_with(new_url=..., old_url=...)` — they expect `old_url` in the call.

4. **Agent got confused by test failures**: The issue prompt says "I've already taken care of all changes to any of the test files" (standard SWE-bench preamble). But the tests still assert the old API. Agent thought tests should have been updated, got confused.

5. **Second edit (entry 56)**: REVERTED the correct fix. Changed to a try/except approach — try with `old_url`, catch `GitCommandError`, retry without. This is WRONG because the tests expect `set_url` to be called WITHOUT `old_url` (the gold patch removes `old_url` entirely and the test patches update the assertions accordingly).

6. **Tests passed on second edit** (entry 59, exit code 0) — but only the PASS_TO_PASS tests. The FAIL_TO_PASS tests still fail because the gold test patch expects `set_url(new_url=...)` without `old_url`.

## What GT provided

### L1 Brief
- Listed `base.py` as #1 candidate with callers + callees
- Listed test file `test_update_cookiecutter_cache.py`
- **Good**: correctly identified the file and its test

### L3b (post-view, entry 15)
```
[GT] test_update_cookiecutter_cache:
→ Next: read src/briefcase/commands/base.py
Calls into: src/briefcase/commands/base.py::update_cookiecutter_cache()
[TEST] assert cached_template == "/path/to/template"
[TEST] assert base_command.tools.git.Repo.call_count == 0
[TEST] assert cached_template == base_command.data_path / "templates" / "special-template"
```
**Good**: showed test assertions. But these are the EXISTING assertions (PASS_TO_PASS), not the FAIL_TO_PASS ones.

### L3 post-edit (entry 50, after first edit)
```
<gt-evidence trigger="post_edit:src/briefcase/commands/base.py">
  tests/.../test_update_cookiecutter_cache.py:34 `cached_template = base_command.update_cookiecutter_cache(...)`
  tests/.../test_update_cookiecutter_cache.py:65 `base_command.update_cookiecutter_cache(...)`
[SIGNATURE] def update_cookiecutter_cache(self, template: str, branch="master"):
```
**Incomplete**: showed test CALL SITES but not the test ASSERTIONS about set_url. The callers show how `update_cookiecutter_cache` is called, not what the test expects about `remote.set_url`.

### L3 post-edit (entry 57, after second edit)
```
TWINS: L1008: `"Unable to clone repository..."` | L1024: `"Unable to update origin URL with old URL check; "`
```
This is structural twin evidence from the agent's own error messages — not the test expectations.

## What was missing

**The critical evidence the agent needed:** What the FAIL_TO_PASS tests expect about `remote.set_url`. Specifically:
- `test_existing_repo_template` expects `mock_remote.set_url.assert_called_once_with(new_url="https://...")`  — NO `old_url`
- `test_existing_repo_template_with_different_url` — same
- etc.

GT showed test call sites (how `update_cookiecutter_cache` is called) but NOT the test assertions about `set_url`. The agent needed to see: **"the test expects `set_url` to be called with ONLY `new_url`, without `old_url`"**.

This is a CONTRACT gap: GT showed callers of the function, but not the mock expectations inside the test that define the correct behavior.

## Why the agent reverted the correct fix

The agent's first edit was correct (remove `old_url`). But when tests failed, the agent concluded:
1. Tests expect `old_url` in the call (from reading existing test code)
2. The issue says "tests are already taken care of" but the tests still have `old_url`
3. Therefore: need a backwards-compatible approach (try/except)

The agent didn't understand that the test_patch (hidden from it, as it should be) CHANGES the test expectations. The correct fix removes `old_url` from the source AND the test assertions. The agent only saw the source side.

## Why the research didn't predict this

### G1 (anchor signal)
Anchors would find: `set_url`, `old_url`, `remote`, `update_cookiecutter_cache`, `insteadOf`. These DO point to the gold file. G1 prediction: anchors hit gold. **Correct — anchors did work for localization.**

### G7 (evidence potential)
`update_cookiecutter_cache` has callers (test files call it) and siblings. G7 classification: MODERATE or RICH. **GT should inject evidence. And it did.**

### What the research MISSED

The research measured:
- Do anchors point to gold files? (YES — 73%)
- Does the graph have callers? (YES — 90%)
- Does GT have evidence to inject? (YES — not POOR)

But the research **never measured**:
- When GT shows caller code, does it show the RIGHT caller context?
- When GT shows test assertions, does it show the SPECIFIC assertions that define correct behavior?
- Is the evidence ACTIONABLE — does it tell the agent what the correct output LOOKS LIKE?

**The briefcase failure is a SPECIFICITY gap, not a COVERAGE gap.** GT covered the right files and functions. But the evidence wasn't specific enough about what the test expects the fix to produce.

## The deeper generalization

The research validated that GT has STRUCTURAL signals (callers exist, tests exist, anchors match). But structural coverage ≠ semantic specificity. The question is not "does GT have evidence about this function?" but "does GT's evidence tell the agent WHAT THE CORRECT FIX LOOKS LIKE?"

For briefcase-2085:
- GT showed: callers call `update_cookiecutter_cache(template=..., branch=...)` → STRUCTURAL
- GT needed to show: tests mock `remote.set_url` and assert it's called with `new_url` only → BEHAVIORAL EXPECTATION

This is the G2 gap that we deprioritized. The visible-test evidence (Patch F) fires [TEST] markers but shows assertion lines FROM THE EXISTING TESTS (PASS_TO_PASS). It doesn't show the mock setup that defines the EXPECTED API behavior.

## What needs to change

1. **Test evidence must show mock setup, not just assertions.** For Python tests with `mock`, the mock configuration (`mock_remote.set_url.assert_called_once_with(new_url=...)`) is the behavioral contract. GT currently extracts lines starting with `assert` but misses `assert_called_once_with`, `assert_called_with`, etc.

2. **Test evidence must include FAIL_TO_PASS-relevant assertions.** But we can't use FAIL_TO_PASS labels. What we CAN do: use issue anchors to identify which test assertions mention the SAME SYMBOLS as the issue text. For briefcase-2085, the issue mentions `set_url` and `old_url` — test lines containing these symbols are the relevant assertions.

3. **The research gap:** G2 was deprioritized because "tests are bonus only." But the briefcase analysis shows that SPECIFIC test expectations (mock.assert_called_with patterns) are the evidence the agent needs to avoid reverting correct fixes. This is not about test COVERAGE — it's about test SPECIFICITY.

## Connection to dataset

The 160-bug research found:
- 82% have test callers in graph (tests exist)
- 0/60 bugs have ONLY test evidence (structural evidence always available)
- Tests "never rescue"

But "never rescue" measured GRAPH-LEVEL test connectivity. It didn't measure whether the TEST CONTENT contains actionable behavioral expectations. The briefcase case proves: test assertions that include mock expectations (`assert_called_with`) are semantically different from structural test connectivity.

The research should have classified test assertions by SPECIFICITY:
- Trivial: `assert result is not None` → useless
- Structural: `assert cached_template == expected_path` → location hint
- Behavioral: `mock.set_url.assert_called_once_with(new_url=...)` → defines correct API usage

Briefcase needs the third type. GT's Patch F extracts lines starting with `assert` but misses `assert_called_once_with` (which doesn't start with `assert` — it starts with a mock object name).
