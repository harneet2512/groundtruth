# GT v1.0.4 v3 Run Analysis -- Pre-Edit Briefing Regression

## Executive Summary

| Run | GT Resolved | BL Resolved | Delta | Key Change |
|-----|-------------|-------------|-------|------------|
| v1 | 5/10 | 5/10 | 0 | No import edges |
| **v2** | **6/10** | **5/10** | **+1** | Import fix + signal ranking |
| v3 | 3/9 (+14096 pending) | 5/10 (v2 BL) | -2 | + pre-edit briefing (REGRESSION) |

**v3 is a regression.** The pre-edit briefing feature, intended to give the agent structural guidance before its first edit, actively damaged outcomes by pointing the agent at wrong targets with false confidence.

**v2 remains the best configuration.**

---

## What Changed Between v2 and v3

| Feature | v2 | v3 |
|---------|----|----|
| Import edges | 10,795 | 10,795 (same binary) |
| Pre-edit briefing | None | `--enhanced-briefing` at task start |
| OBLIGATION import path | Blocked | Fixed (sys.path bootstrap) |
| Refire logic | mtime-based | Content-hash based |
| Telemetry | None | JSONL per cycle |
| PRECEDENT filter | None | Formatting-only filtered |

The only behavioral difference the model experienced was the **pre-edit briefing**: a `[VERIFIED] FIX HERE:` block shown before the agent's first edit.

---

## Per-Task Detailed Results

### Task 12907: separability_matrix (RESOLVED in all runs)

**v3 briefing target:** `separability_matrix()` at `separable.py:66` -- CORRECT target.

**v3 briefing content:**
```
[VERIFIED] FIX HERE: separability_matrix() at astropy/modeling/separable.py:66
  signature: def separability_matrix(transform):
  [VERIFIED] test_custom_separability_matrix() -- called as: original = separability_matrix(ModelDefault(...))
  [VERIFIED] test_custom_model_separable -- assert np.all(separability_matrix(model_c()) == [True, True])
  [VERIFIED] test_separable() -- called as: assert_allclose(separability_matrix(compound_model), result[1])
```

**v3 evidence deliveries:** 2
**v3 patch:** +1 -1 in `separable.py` (correct fix, same as v2)
**Outcome:** Resolved. Briefing pointed at the RIGHT function with concrete test assertions. Agent produced the same correct fix as v2.

---

### Task 13033: timeseries _check_required_columns (UNRESOLVED in all runs)

**v3 briefing target:** `TimeSeries` class -- partially relevant but too broad.

**v3 evidence deliveries:** 4
**v3 patch:** +20 -4 in `core.py` (wrong fix, same pattern as v2)
**Outcome:** Unresolved. The briefing didn't help because the issue is about error message format, not about the TimeSeries class structure. No change from v2.

---

### Task 13236: Table structured ndarray (v2 FLIP -- v3 REGRESSION)

**This is the critical regression.**

**v2 (no briefing, RESOLVED):**
- Post-edit evidence: "sibling method in same class (7 total)" + "38 callers"
- Agent explored freely, found the conversion clause, removed it
- Patch: removed 4 lines (correct)

**v3 (with briefing, UNRESOLVED):**
- Briefing target: `NdarrayMixin()` at `ndarray_mixin.py:24` -- **WRONG FILE**
- The issue mentions "NdarrayMixin" but the fix is in `table.py`'s conversion clause
- Agent followed the briefing, kept the conversion clause, added a deprecation warning instead
- Patch: added 6 lines of warning (wrong fix)

**v2 patch (correct):**
```diff
-        if (not isinstance(data, Column) and not data_is_mixin
-                and isinstance(data, np.ndarray) and len(data.dtype) > 1):
-            data = data.view(NdarrayMixin)
-            data_is_mixin = True
```

**v3 patch (wrong):**
```diff
+            warnings.warn(
+                "In future, structured arrays will be added to tables as "
+                "Column objects instead of NdarrayMixin..."
```

**Root cause:** The `--enhanced-briefing` mode matched the keyword "NdarrayMixin" from the issue text to the `NdarrayMixin` class definition in `ndarray_mixin.py`. But the actual fix is removing the code that USES NdarrayMixin in `table.py`, not changing the NdarrayMixin class itself. The briefing's `[VERIFIED] FIX HERE: NdarrayMixin()` sent the agent down the wrong path with false confidence.

In v2 (no briefing), the agent explored freely, found the conversion clause in `table.py` through its own grep/read workflow, and removed it (correct). The post-edit evidence ("sibling method in same class") then validated the decision. Without the misleading briefing, the agent was more autonomous and produced a better result.

---

### Task 13398: Coordinates transform docs (UNRESOLVED in all runs)

**v3 briefing target:** `matrix_transpose()` -- wrong target (issue is about transform graph docs).
**v3 evidence deliveries:** 2
**Outcome:** Unresolved. No change from v2. This task requires deep domain knowledge.

---

### Task 13453: HTML SoupString (RESOLVED in all runs)

**v3 briefing target:** None matched.
**v3 evidence deliveries:** 7 (all empty: "No high-confidence findings")
**v3 patch:** +6 -1 in `html.py`
**Outcome:** Resolved. Agent solved it without GT guidance (same as v2).

---

### Task 13579: WCS slicing pixel_to_world (v2 RESOLVED -- v3 REGRESSION)

**v2 (no briefing, RESOLVED):**
- Post-edit evidence: SIBLING + IMPACT + PRECEDENT
- Agent found the right file (`sliced_wcs.py`) on its own
- Patch: +86 -9 (correct)

**v3 (with briefing, UNRESOLVED):**
- Briefing target: `wcs_to_celestial_frame()` at `wcs/utils.py:185` -- **WRONG FILE**
- The actual fix is in `wcs/wcsapi/wrappers/sliced_wcs.py`
- Agent was directed to `utils.py` by the briefing, produced a different (wrong) patch
- Patch: +41 lines in `sliced_wcs.py` (eventually found right file but wrong fix)

**v3 evidence deliveries:** 12 (highest count) -- but the initial misdirection from the briefing may have consumed too many agent steps exploring the wrong area.

**Root cause:** Same as 13236 -- the briefing matched issue keywords to the wrong symbol/file. The issue discusses "pixel_to_world" behavior in sliced WCS, but the briefing resolved "wcs_to_celestial_frame" (a related but wrong function) as the target.

---

### Task 13977: Units Quantity structured dtype (UNRESOLVED in all runs)

**v3 briefing target:** None matched.
**v3 evidence deliveries:** 4
**Outcome:** Unresolved. No change. Deep type system issue.

---

### Task 14096: SkyCoord attribute error (PENDING in v3)

**Status:** Still running at time of eval. Container was active for 20+ minutes.
**v2 result:** Resolved (GT resolved, BL didn't submit).

---

### Task 14182: ASCII RST header parsing (UNRESOLVED in all runs)

**v3 briefing target:** `_get_writer()` -- wrong target (issue is about RST header parsing).
**v3 evidence deliveries:** 7 (mostly PRECEDENT)
**Outcome:** Unresolved. No change.

---

### Task 14309: FITS format identification (RESOLVED in all runs)

**v3 briefing target:** `identify_format()` -- correct target.
**v3 evidence deliveries:** 4
**v3 patch:** +4 lines across 3 files
**Outcome:** Resolved. Agent solved it correctly.

---

## Briefing Accuracy Analysis

| Task | Briefing Target | Correct File | Match? | Result |
|------|----------------|--------------|--------|--------|
| 12907 | `separability_matrix` @ separable.py | separable.py | YES | Resolved |
| 13033 | `TimeSeries` class | timeseries/core.py | PARTIAL | Unresolved |
| **13236** | **`NdarrayMixin` @ ndarray_mixin.py** | **table/table.py** | **WRONG** | **LOST** |
| 13398 | `matrix_transpose` | builtin_frames/ | WRONG | Unresolved |
| 13453 | (none) | io/ascii/html.py | N/A | Resolved |
| **13579** | **`wcs_to_celestial_frame` @ utils.py** | **wcsapi/wrappers/sliced_wcs.py** | **WRONG** | **LOST** |
| 13977 | (none) | units/quantity.py | N/A | Unresolved |
| 14182 | `_get_writer` | io/ascii/rst.py | WRONG | Unresolved |
| 14309 | `identify_format` | io/fits/connect.py | YES | Resolved |

**Briefing accuracy: 2/7 correct, 4/7 wrong, 2 no target.** The wrong targets caused 2 regressions.

---

## The Fundamental Problem

The `--enhanced-briefing` mode works by:
1. Extracting keywords from the issue text (CamelCase, backtick-quoted, file paths)
2. Matching keywords against symbols in graph.db
3. Emitting `[VERIFIED] FIX HERE:` for the best match

This produces **false localization** when the issue mentions a symbol that is RELATED to the bug but is NOT the symbol that needs to be fixed. Examples:
- Issue says "NdarrayMixin" (the class being misused) but the fix is in the code that CALLS NdarrayMixin
- Issue says "pixel_to_world" but the fix is in a WRAPPER around the WCS implementation

The keyword-to-symbol matching has no understanding of whether the matched symbol is the CAUSE of the bug or just a MENTION in the description.

---

## Verdict: v2 is the Production Configuration

| Configuration | Resolved | Delta vs BL |
|---------------|----------|-------------|
| v2 (post-edit only, no briefing) | 6/10 | +1 |
| v3 (post-edit + pre-edit briefing) | 3/9 | -2 vs BL |

**The pre-edit briefing must be disabled or fundamentally redesigned.** It is net negative in its current form.

The v2 configuration (import-resolution fix + signal quality ranking + post-edit evidence only) remains the best tested configuration. It produced GT's first positive delta (+1) with zero regressions.

---

## Recommendations

### Immediate (before next benchmark run)
1. **Disable the pre-edit briefing** in `_state_gt`. Revert to v2 behavior: post-edit evidence only.
2. Keep all other v3 improvements: OBLIGATION import path fix, hash-based refire, telemetry, PRECEDENT filter.

### Future (requires research)
3. **Fix briefing localization**: The `--enhanced-briefing` should target the file/function that needs to CHANGE, not the symbol mentioned in the issue. This requires understanding the difference between "symptom symbol" and "fix location."
4. **Confidence-gated briefing**: Only emit `FIX HERE` when localization confidence is very high (>0.9). For lower confidence, emit `POSSIBLY RELEVANT` or nothing.
5. **Multi-candidate briefing**: Instead of picking one target, show the top 3 candidates with uncertainty markers. Let the agent decide.

### What v2 Got Right
- Post-edit evidence works because it fires AFTER the agent has already found the right file through its own exploration. It validates rather than directs.
- The 13236 flip in v2 happened because SIBLING evidence ("7 methods in same class") reinforced the agent's own discovery, not because GT directed the agent to a file.
- GT is most effective as a VALIDATOR of the agent's decisions, not as a DIRECTOR of where to look.

This is the core lesson: **GT should confirm and constrain, not direct.**
