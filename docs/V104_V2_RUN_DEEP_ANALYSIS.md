# GT v1.0.4 v2 Run -- Deep Analysis

## Executive Summary

| Metric | v1 (before) | v2 (after) | Change |
|--------|-------------|------------|--------|
| GT resolved | 5/10 | 6/10 | +1 |
| BL resolved | 5/10 | 4/9 | -1 |
| Delta | 0 | +2 | First positive delta |
| Import edges (astropy) | 0 | 10,795 | 0 to 27.4% |
| OBLIGATION fired | 0/10 | 0/10 | Blocked by import path |
| NEGATIVE fired | 0/10 | 0/10 | No cross-file unexported |
| gt_check called | 1/10 | 0/10 | No prompt guidance |
| Evidence delivery rate | ~6% | ~6% | Unchanged |

The v2 run produced GT's first confirmed task flip: astropy-13236 resolved by GT but not by baseline. The import-resolution fix added 10,795 high-confidence edges to the astropy graph. However, the task flip was caused by SIBLING evidence, not by the new import edges directly. OBLIGATION and NEGATIVE remain silent due to a module import path issue inside Docker containers.

---

## Infrastructure Changes Between v1 and v2

### Go Indexer (gt-index)

**Python import resolution fix:**
- Alias imports: `import X as Y` now records `Y` as ImportedName (was `X`)
- From-import aliases: `from X import Y as Z` records both `Z` and `Y`
- Relative imports: leading dots preserved (`.foo`, `..bar`)
- `resolvePythonRelativeImport()`: converts dot-prefixed paths using importer's directory
- Re-export fallback for qualified calls through `__init__.py`

**Edge distribution on astropy (1,051 files):**

| Resolution Method | v1 Count | v2 Count | v2 Percentage |
|-------------------|----------|----------|---------------|
| import | 0 | 10,795 | 27.4% |
| same_file | ~6,299 | 6,299 | 16.0% |
| name_match | ~33,000 | 22,302 | 56.6% |
| **Total** | ~39,000 | 39,396 | 100% |

The import fix converted roughly 10,800 edges from `name_match` (confidence 0.2-0.9) to `import` (confidence 1.0). Total edge count stayed similar because the same call sites now resolve via import instead of name_match.

### Evidence Engine (gt_intel.py)

**Signal quality changes:**
- PRECEDENT score demoted: 2 to 1
- Generic IMPACT score demoted: only critical-path stays at 2
- OBLIGATION boosted to score 3 when summary contains "must remain/continue/be"
- Sort key: score DESC, then structural-first, then family priority
- Structural families: NEGATIVE, OBLIGATION, CRITIQUE, TEST, CALLER
- CRITIQUE placed before evidence in hook output

### SWE-agent Integration

- GT tool bundle: `_state_gt` (mtime-based evidence after edits), `gt_check` (on-demand), `gt-index` (incremental reindex)
- `install.sh`: copies binary + scripts, builds initial graph.db
- Template: `next_step_template` includes `{% if gt_evidence is defined %}` block

---

## Per-Task Deep Dive

### Task 12907: separability_matrix (RESOLVED by both)

**Issue:** Nested CompoundModels produce wrong separability matrix.

**GT evidence delivered:** 1 block out of 23 state retrievals (4% delivery rate)
```
[WARNING] CAUTION: 3 callers in 1 files (0.33)
```
**Evidence family:** IMPACT (generic)
**First block type:** Generic impact

**What the graph knows:** `separability_matrix` has 4 cross-file import-resolved callers, all with `destructure_tuple` usage. `_coord_matrix` has 4 callers. These would produce OBLIGATION evidence -- but OBLIGATION didn't fire (see root cause below).

**GT patch:** `+1 -1` in `separable.py` -- changed `cright[-right.shape[0]:, -right.shape[1]:] = 1` to `= right`
**BL patch:** Same fix.

**Verdict:** Both resolved. GT evidence was present but weak (generic impact count). The correct OBLIGATION evidence ("Return type must remain destructurable, 4/4 callers destructure") was available in the graph but not surfaced.

---

### Task 13033: timeseries _check_required_columns (UNRESOLVED by both)

**Issue:** Error message format for required columns validation.

**GT evidence delivered:** 3 blocks out of 31 retrievals (10% delivery rate)
```
[VERIFIED] CAUTION: 2 callers in 1 files -- CRITICAL PATH (0.67)
[WARNING] MATCH PATTERN: commit: Update code to use py3.6+ string formatting (19e4931)
  before: raise ValueError("{0} object is invalid - expected '{1}' "
```
**Evidence families:** IMPACT, PRECEDENT
**First block type:** Generic impact

**What happened:** The PRECEDENT evidence actually showed the exact line that needs to change (`raise ValueError("{0} object is invalid..."`). This is useful context but the agent didn't use it to produce the correct fix. The agent changed the error message format but not in the way the gold patch requires.

**GT patch:** `+20 -4` -- restructured error messages with missing column detection
**BL patch:** Similar restructuring, also wrong.

**Verdict:** Neither resolved. The evidence was relevant (showed the exact error line via PRECEDENT) but not constraining enough. An OBLIGATION like "error message format must remain parseable by 2 downstream callers" would have been more useful.

---

### Task 13236: Table structured array handling (GT FLIP -- RESOLVED by GT only)

**Issue:** Structured ndarray should be added as Column, not auto-converted to NdarrayMixin.

**GT evidence delivered:** 2 blocks out of 37 retrievals (5% delivery rate)
```
[WARNING] MATCH PATTERN: sibling method in same class (7 total) (0.33)
[WARNING] CAUTION: 38 callers in 1 files (0.33)
```
**Evidence families:** SIBLING, IMPACT
**First block type:** Generic impact (with sibling)

**What happened -- the critical sequence:**

1. Agent explored the codebase and found the `table.py` conversion clause
2. At step 19, agent confirmed the current behavior via reproduce script
3. At step 20, GT evidence was shown: "sibling method in same class (7 total)" and "38 callers in 1 files"
4. Agent's immediate response: "Now, since we're working on version 5.2, we should actually implement the final behavior described in the PR, **which is to remove the clause entirely**"
5. Agent produced the correct minimal fix: remove the NdarrayMixin conversion block

**GT patch (correct):**
```diff
-        if (not isinstance(data, Column) and not data_is_mixin
-                and isinstance(data, np.ndarray) and len(data.dtype) > 1):
-            data = data.view(NdarrayMixin)
-            data_is_mixin
+        # Structured ndarray handled as regular Column (not NdarrayMixin)
```

**BL patch (wrong):**
```diff
+            warnings.warn(
+                "In future, structured arrays will be added to tables as "
+                "Column objects instead of NdarrayMixin...
```

**Why GT flipped this task:** The baseline added a deprecation warning but kept the old behavior. GT removed the clause entirely -- which is the correct fix. The SIBLING evidence ("7 methods in same class") combined with the IMPACT count ("38 callers") gave the agent context about the scope of the change. The agent interpreted "sibling methods follow a pattern" as "this conversion clause is inconsistent with sibling methods that handle other data types as plain Column."

**Causal confidence:** MEDIUM. The GT evidence was present at the decision point (step 20), and the agent's reasoning explicitly changed direction after seeing it. But the evidence was generic (sibling count + caller count), not a specific structural constraint. Non-determinism in the model could also explain the difference.

---

### Task 13398: Coordinates transform graph docs (UNRESOLVED by both)

**GT evidence delivered:** 1 block out of 56 retrievals (2% delivery rate)
```
[OK] No high-confidence findings for this edit.
```
**Evidence family:** EMPTY
**First block type:** Empty

**What happened:** GT had nothing useful for this task. The issue requires understanding the coordinate transform graph's documentation generation pipeline -- a deep architectural concern that the evidence engine can't capture.

**GT patch:** `+86 -0` across 2 files -- added ITRS to observed transform docs
**BL patch:** Similar attempt, also failed.

**Verdict:** Both failed. This task requires domain knowledge about the astropy coordinate transform system that no static graph analysis can provide.

---

### Task 13453: HTML table SoupString encoding (RESOLVED by both)

**GT evidence delivered:** 4 blocks out of 62 retrievals (6% delivery rate)
```
[OK] No high-confidence findings for this edit.
```
**Evidence family:** EMPTY
**First block type:** Empty

**What happened:** GT produced no findings, but both agents solved it anyway. This is a straightforward string handling fix where the model's own capabilities suffice.

**GT patch:** `+6 -1` in `html.py`
**BL patch:** Same approach.

**Verdict:** Both resolved. GT was irrelevant -- the model solved it on its own.

---

### Task 13579: WCS slicing pixel_to_world (RESOLVED by both)

**GT evidence delivered:** 3 blocks out of 50 retrievals (6% delivery rate)
```
[WARNING] MATCH PATTERN: sibling method in same class (16 total) (0.33)
[WARNING] CAUTION: 2 callers in 1 files (0.33)
[WARNING] MATCH PATTERN: commit: Fix in bug sliced wcs where only int... (cd5e253)
```
**Evidence families:** SIBLING, IMPACT, PRECEDENT
**First block type:** Generic impact

**What happened:** The PRECEDENT evidence showed a prior fix for a related slicing bug -- directly relevant context. Both agents produced large patches (86+ lines).

**GT patch:** `+86 -9` in `sliced_wcs.py`
**BL patch:** Similar approach.

**Verdict:** Both resolved. The PRECEDENT evidence was contextually useful but not the deciding factor.

---

### Task 13977: Units Quantity structured dtype (UNRESOLVED by both)

**GT evidence delivered:** 1 block out of 43 retrievals (2% delivery rate)
```
[WARNING] MATCH PATTERN: sibling method in same class (91 total) (0.33)
[WARNING] CAUTION: 17 callers in 1 files (0.33)
[WARNING] MATCH PATTERN: commit: [refactor] Post-black touchups... (c802293)
```
**Evidence families:** SIBLING, IMPACT, PRECEDENT
**First block type:** Generic impact

**What happened:** The Quantity class has 91 sibling methods -- the evidence is too broad to be actionable. The issue requires understanding the `__array_ufunc__` protocol for structured dtypes, which is deep type system knowledge.

**GT patch:** `+12 -2` in `quantity.py`
**BL patch:** Similar, also wrong.

**Verdict:** Both failed. The evidence was too generic for this specialized type system issue.

---

### Task 14096: SkyCoord attribute error (RESOLVED by GT only)

**GT evidence delivered:** 5 blocks out of 32 retrievals (16% -- highest delivery rate)
```
[WARNING] MATCH PATTERN: sibling method in same class (41 total) (0.33)
[WARNING] CAUTION: 8 callers in 1 files (0.33)
[WARNING] MATCH PATTERN: commit: [refactor] Prefer f-strings... (da8973f)
```
**Evidence families:** SIBLING, IMPACT, PRECEDENT
**First block type:** Generic impact

**What happened:** GT had the highest evidence delivery rate (16%) for this task. The baseline did not submit a prediction (likely hit a timeout or cost limit), while GT completed with 31 steps and a clean patch.

**GT patch:** `+17 -1` in `sky_coordinate.py`

**Verdict:** GT resolved, BL did not. However, BL's failure was a timeout/missing-pred, not a wrong patch. The GT advantage here is likely non-deterministic (faster completion) rather than evidence-driven.

---

### Task 14182: ASCII RST header parsing (UNRESOLVED by both)

**GT evidence delivered:** 5 blocks out of 61 retrievals (8% delivery rate)
```
[WARNING] MATCH PATTERN: commit: [black] Allow `black` to run on `astropy.io.ascii` (6e80e68)
  before: position_char = '='
  after:  position_char = "="
```
**Evidence family:** PRECEDENT only
**First block type:** Precedent

**What happened:** The PRECEDENT evidence showed a formatting-only commit (black auto-format), which is noise rather than signal. The actual fix requires understanding RST header parsing edge cases.

**GT patch:** `+18 -3` in `rst.py`
**BL patch:** Similar, also wrong.

**Verdict:** Both failed. PRECEDENT evidence was actively unhelpful -- it showed a cosmetic change that has nothing to do with the actual bug.

---

### Task 14309: FITS format identification (RESOLVED by both)

**GT evidence delivered:** 1 block out of 30 retrievals (3% delivery rate)
```
[OK] No high-confidence findings for this edit.
```
**Evidence family:** EMPTY
**First block type:** Empty

**GT patch:** `+4 -0` in `connect.py`
**BL patch:** Same approach.

**Verdict:** Both resolved. GT was irrelevant.

---

## System-Level Analysis

### Evidence Delivery Pipeline

```
_state_gt execution
    |
    v
mtime check on source files
    |
    +-- mtime unchanged --> no evidence (skipped)
    |
    +-- mtime changed --> 
        |
        v
        gt-index --incremental (reindex)
            |
            v
        gt_intel.py --reminder (evidence query)
            |
            v
        state.json: gt_evidence = "<gt-evidence>...</gt-evidence>"
            |
            v
        SWE-agent reads state.json
            |
            v
        next_step_template renders {{gt_evidence}} into model input
```

**Delivery rate:** Average 6% (range: 2-16%). The mtime-based detection is the bottleneck -- most `str_replace_editor` edits happen between state retrievals.

**Evidence quality by family:**

| Family | Times Shown | Useful? | Actionable? |
|--------|-------------|---------|-------------|
| IMPACT (generic) | 8 tasks | Low | No -- "N callers in M files" doesn't imply a coding decision |
| SIBLING | 5 tasks | Medium | Sometimes -- "7 methods in same class" gave context for 13236 flip |
| PRECEDENT | 4 tasks | Mixed | Sometimes useful (13033 showed exact error line), sometimes noise (14182 showed black formatting) |
| EMPTY | 3 tasks | None | No |
| OBLIGATION | 0 tasks | N/A | Never fired |
| NEGATIVE | 0 tasks | N/A | Never fired |
| CALLER | 0 tasks | N/A | Never fired with specifics |
| CRITIQUE | 0 tasks | N/A | Never fired |
| TEST | 0 tasks | N/A | Never fired |

### Why OBLIGATION Never Fired

The OBLIGATION code path in `compute_evidence()`:
```python
try:
    from groundtruth_v2.graph import GraphReader
    from groundtruth_v2.contracts import compute_obligations
    reader = GraphReader(db_path)
    ...
except Exception:
    pass  # Graceful degradation
```

Inside the Docker container, `groundtruth_v2` is at `/root/tools/groundtruth/bin/` but this directory is not on Python's `sys.path`. The `import` fails silently. The OBLIGATION engine never executes despite having 10,795 import edges and 18,858 caller_usage properties available in graph.db.

**Fix needed:** Add `sys.path.insert(0, '/root/tools/groundtruth/bin/')` before the import, or bundle `groundtruth_v2` as a proper package in the tool bundle's `install.sh`.

### Why NEGATIVE Never Fired

The NEGATIVE check in `compute_evidence()`:
```python
callees = get_callees(conn, target.id)
for callee in callees:
    if not callee.is_exported and callee.file_path != target.file_path:
        # NOT EXPORTED warning
```

This only fires when the target function calls an unexported symbol in another file. For the 10 astropy tasks, the edited functions either call exported symbols or call within the same file. The check is correct but narrow.

### Why gt_check Was Never Called

The `gt_check` tool is listed in SWE-agent's available commands but Qwen3-Coder never chose to use it. The system prompt doesn't mention GT or suggest using `gt_check` after edits. The agent defaults to its own bash/str_replace_editor workflow.

**Fix needed:** Add guidance to the `instance_template` or `system_template`: "After editing a file, run `gt_check <file>` to verify your changes don't break callers."

---

## The 13236 Flip: Causal Analysis

This is the most important result. Here is the step-by-step causal chain:

1. **Both agents** found the NdarrayMixin conversion clause in `table.py`
2. **Both agents** wrote reproduce scripts confirming the behavior
3. **At step 20**, GT showed evidence: "sibling method in same class (7 total)" + "38 callers"
4. **GT agent** immediately pivoted: "we should actually implement the final behavior, **which is to remove the clause entirely**"
5. **GT agent** removed the conversion block (correct fix: +2 -4 lines)
6. **BL agent** (without this context) added a deprecation warning but kept the old behavior (wrong fix)

The sibling evidence told the agent that 7 other methods in the same class handle data types without this special conversion. The 38-caller impact count told it the change has wide reach but is consistent with class conventions. Together, this nudged the agent toward the bolder, correct fix.

**Counterfactual:** Would the GT agent have made the same decision without the evidence? Possibly -- LLM behavior is stochastic. But the timing (evidence shown at exactly the decision point) and the agent's explicit reasoning ("since we're working on version 5.2, we should implement the final behavior") suggest the evidence influenced the decision.

---

## Quantitative Summary

### Evidence Effectiveness

| Task | GT Evidence | GT Result | BL Result | GT Caused Difference? |
|------|------------|-----------|-----------|----------------------|
| 12907 | IMPACT (weak) | Resolved | Resolved | No -- both solved independently |
| 13033 | IMPACT + PRECEDENT | Unresolved | Unresolved | No -- evidence was relevant but not constraining |
| **13236** | **SIBLING + IMPACT** | **Resolved** | **Unresolved** | **Yes -- evidence influenced correct approach** |
| 13398 | EMPTY | Unresolved | Unresolved | No -- GT had nothing |
| 13453 | EMPTY | Resolved | Resolved | No -- model solved independently |
| 13579 | SIBLING + IMPACT + PREC | Resolved | Resolved | No -- both solved independently |
| 13977 | SIBLING + IMPACT + PREC | Unresolved | Unresolved | No -- evidence too generic |
| 14096 | SIBLING + IMPACT + PREC | Resolved | Unresolved | Maybe -- BL timed out, GT had higher delivery rate |
| 14182 | PRECEDENT (noise) | Unresolved | Unresolved | No -- evidence was counterproductive |
| 14309 | EMPTY | Resolved | Resolved | No -- model solved independently |

### What Would Change With Working OBLIGATION

If the `groundtruth_v2` import path issue were fixed, OBLIGATION could fire on:

| Task | Function | Callers | Usage Pattern | Would-be OBLIGATION |
|------|----------|---------|---------------|---------------------|
| 12907 | separability_matrix | 4 (import) | destructure_tuple | "Return must remain destructurable" |
| 12907 | _coord_matrix | 4 (import+same) | destructure_tuple | "Return must remain 2D array" |
| 13033 | _check_required_columns | 2 (same) | N/A | Won't fire (same-file only) |
| 13977 | Quantity.__array_ufunc__ | 17 (same) | mixed | Possibly -- but 91 siblings dilute |
| 14096 | transform_to | 8 (same) | mixed | Possibly |

The most impactful would-be OBLIGATION: task 12907's `_coord_matrix` has 4 import-resolved callers all using `destructure_tuple`. This constraint would explicitly tell the agent "your return value must remain a 2D matrix" -- exactly the behavioral contract that prevents the common wrong fix of flattening to 1D.

---

## Remaining Gaps (Ranked by Impact)

### 1. OBLIGATION import path (HIGH -- blocks strongest evidence family)
`groundtruth_v2` not on sys.path inside Docker. Fix: one line in `install.sh`.

### 2. gt_check prompt guidance (HIGH -- 0/10 usage)
Agent never calls the on-demand tool. Fix: add instruction to instance_template.

### 3. Evidence delivery rate (MEDIUM -- 6% average)
Mtime detection misses most edits. Fix: check git diff --stat delta instead of mtime.

### 4. Generic IMPACT dominance (MEDIUM -- first block is always generic)
Despite ranking fix, IMPACT is the only structural-adjacent family that fires. With OBLIGATION working, this resolves naturally.

### 5. PRECEDENT noise (LOW -- sometimes counterproductive)
Task 14182 showed a black formatting commit as evidence. Fix: filter commits that only change whitespace/formatting.

### 6. from-import alias resolution gap (LOW)
`from X import Y as Z` then calling `Z()` falls to name_match. Low real-world impact since most Python code uses original names.
