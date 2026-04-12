# GT v1.0.4 Task Flip Analysis — What Caused Flips and What Would Cause More

## Summary of All Runs

| Task | BL | v1 | v2 | v3 | v4 | v5 | Ever Flipped? |
|------|----|----|----|----|----|----|---------------|
| 12907 | Y | Y | Y | Y | Y | Y | Stable resolve |
| 13033 | N | N | N | N | N | N | Never |
| **13236** | **N** | N | **Y** | N | N | **Y** | **Flipped in v2, v5** |
| 13398 | N | N | N | N | N | N | Never |
| 13453 | Y | Y | Y | Y | Y | Y | Stable resolve |
| 13579 | Y | Y | Y | N | Y | Y | Lost in v3 only |
| 13977 | N | N | N | N | N | N | Never |
| 14096 | Y | Y | Y | ? | ? | ? | Infra-blocked in v3-v5 |
| 14182 | N | N | N | N | N | N | Never |
| 14309 | Y | Y | Y | Y | Y | Y | Stable resolve |

---

## Task 13236: The One GT Flipped (v2, v5)

### What the issue is
Structured ndarray added to `Table` gets auto-converted to `NdarrayMixin` instead of staying as regular `Column`.

### What the gold fix is
Remove the conversion clause in `table.py` lines 1243-1245:
```python
# DELETE these lines:
if (not isinstance(data, Column) and not data_is_mixin
        and isinstance(data, np.ndarray) and len(data.dtype) > 1):
    data = data.view(NdarrayMixin)
```

### Why baseline fails
The baseline agent finds the conversion clause but adds a deprecation warning instead of removing it. It's conservative — it assumes the existing behavior was intentional and only warns about future removal.

### What GT did in v2 (RESOLVED)
- Post-edit evidence fired: `"sibling method in same class (7 total)"` + `"38 callers in 1 files"`
- The SIBLING evidence told the agent that 7 other methods in the same class handle data types without this special NdarrayMixin conversion
- The IMPACT count (38 callers) indicated the change has wide reach but is consistent with class conventions
- Agent's thought after seeing evidence: "we should actually implement the final behavior, which is to remove the clause entirely"
- Result: agent removed the conversion block (correct fix)

### What GT did in v3 (REGRESSED)
- Pre-edit briefing pointed at wrong target: `[VERIFIED] FIX HERE: NdarrayMixin() at ndarray_mixin.py:24`
- This pointed at the NdarrayMixin class DEFINITION, not at the code that USES it
- Agent followed the wrong pointer and added deprecation warning instead

### What GT did in v5 (RESOLVED again)
- Confidence-gated briefing: `[GT] Low confidence. Possibly relevant: astropy/modeling/separable.py`
- Actually pointed at wrong file (separable.py not table.py) but was LOW CONFIDENCE
- Because it was soft, the agent ignored it and explored freely
- Post-edit evidence then fired with SIBLING/IMPACT, same as v2
- Agent found the right fix independently, then GT validated

### Generalized lesson
**GT helped when it VALIDATED the agent's own discovery, not when it DIRECTED the agent.** The sibling pattern evidence ("7 methods in same class don't use NdarrayMixin") reinforced the agent's intuition that the conversion was wrong. The pre-edit briefing was most effective when it was LOW CONFIDENCE — it got out of the way.

---

## Task 13579: Lost Only in v3

### What the issue is
`SlicedLowLevelWCS.pixel_to_world_values` doesn't handle integer-type array indices correctly.

### Why v3 lost it
Pre-edit briefing pointed at `wcs_to_celestial_frame()` in `wcs/utils.py` — wrong file. The fix is in `wcs/wcsapi/wrappers/sliced_wcs.py`. The agent went to the wrong module.

### Why all other versions resolved it
Without the misleading briefing, the agent found the right file through normal grep/explore. Post-edit evidence (SIBLING + IMPACT + PRECEDENT) was supplementary, not directing.

### Generalized lesson
**Wrong pre-edit localization is worse than no localization.** When the issue mentions a symbol (like `pixel_to_world`) that exists in multiple files, pointing at the wrong one consumes agent steps and may lock the agent into a wrong mental model.

---

## What Would Help Unresolved Tasks Flip

### Task 13033: timeseries error message format
**Issue:** Error message format for `_check_required_columns` is misleading.

**Why it doesn't flip:** The fix requires understanding that the error message format matters for specific downstream string parsing. GT's evidence shows the function and its callers but not the STRING FORMAT CONTRACT.

**What would help:**
- OBLIGATION evidence that includes the actual error string pattern: `"expected '{0}' as the first column"` — showing this is a structured format, not free text
- TEST evidence with specific assertion against the error message content
- The graph has 2 same-file callers, but what matters is the error message CONTENT, not the caller count

**Estimated difficulty:** HIGH — requires string-content analysis beyond structural graph

### Task 13398: Coordinates transform graph documentation
**Issue:** Transform docs need updating for new coordinate frame transformations.

**Why it doesn't flip:** This is a documentation/configuration task, not a code bug. The agent needs to understand the transform graph architecture and know which doc generation pipeline to modify.

**What would help:**
- COMPLETENESS warning: "new transforms added to frame_transforms but not registered in docs"
- Cross-module analysis: changes in builtin_frames/ should trigger doc updates in a known location
- This is beyond what a call graph can provide — it needs ARCHITECTURAL knowledge

**Estimated difficulty:** VERY HIGH — needs domain-specific architectural understanding

### Task 13977: Units Quantity structured dtype
**Issue:** `Quantity.__array_ufunc__` doesn't handle structured dtypes correctly.

**Why it doesn't flip:** The fix requires understanding the NumPy ufunc protocol for structured dtypes. GT shows 91 sibling methods (too broad) and 17 callers (no specific contract). The evidence is diluted by the massive class size.

**What would help:**
- Focused OBLIGATION on the `__array_ufunc__` protocol specifically: "return type must be Quantity for numeric dtypes, object for structured"
- TEST evidence from dtype-specific test cases
- The graph has the caller information but the USAGE PATTERN classification doesn't capture dtype-sensitive behavior

**Estimated difficulty:** HIGH — needs type-aware analysis

### Task 14096: SkyCoord attribute error handling
**Issue:** Custom `SkyCoord` subclass property raises wrong exception type when accessing nonexistent attribute.

**Why it doesn't flip (in v5):** Infrastructure Docker build failure, not a GT limitation. In v1/v2 it resolved correctly.

**What would help:** Fix the Docker standalone Python build issue on this VM.

**Estimated difficulty:** LOW (infra fix, not GT improvement)

### Task 14182: ASCII RST header parsing
**Issue:** RST header parser doesn't handle edge case in column position detection.

**Why it doesn't flip:** GT's evidence was PRECEDENT-only (a black formatting commit — noise). No structural evidence exists because the parsing logic is self-contained with minimal callers.

**What would help:**
- CRITIQUE when the agent's edit changes parsing behavior: "this function now handles position_char differently than before"
- TEST evidence with specific RST table fixtures
- Better: reproduction-feedback loop where the agent's repro script reveals the exact parsing failure

**Estimated difficulty:** MEDIUM — testable but requires parsing-specific evidence

---

## Generalization: What We Learned

### 1. GT works as VALIDATOR, not DIRECTOR
The only successful flip (13236) happened because GT evidence validated the agent's own discovery. Post-edit SIBLING evidence ("7 methods in same class follow a different pattern") confirmed the agent's intuition. Pre-edit direction (v3's `FIX HERE`) actively harmed outcomes.

**Rule:** Post-edit structural validation > pre-edit localization direction.

### 2. Confidence gating is essential
v5's `[GT] Low confidence` soft hint was safe — the agent ignored it and explored freely. v3's `[VERIFIED] FIX HERE` on wrong targets was catastrophic. The confidence tier must gate the ASSERTIVENESS of the message.

**Rule:** Low confidence = soft suggestion. High confidence = structural constraint. Never emit structural constraints on low-confidence targets.

### 3. The sibling pattern is the strongest signal
The SIBLING evidence ("N methods in same class") was the most effective evidence family. It tells the agent "your target function is inconsistent with its peers" — which is a powerful behavioral constraint that doesn't depend on cross-file edges.

**Rule:** Within-class consistency signals are often stronger than cross-file caller signals for Python code.

### 4. Caller count alone is not actionable
Generic IMPACT ("38 callers in 1 file") provides awareness but not a coding decision. The agent needs to know WHAT callers do with the return value, not just HOW MANY there are.

**Rule:** Caller USAGE patterns > caller COUNT. OBLIGATION ("3/4 callers destructure") > IMPACT ("38 callers").

### 5. The unresolved tasks need different kinds of evidence
- 13033: string format contracts (beyond structural graph)
- 13398: architectural knowledge (beyond call graph)
- 13977: type-system awareness (beyond usage pattern classification)
- 14182: parsing-specific test evidence (beyond caller analysis)

**Rule:** The current graph captures structural dependencies well but misses semantic contracts (string formats, type protocols, parsing rules). The next lever is either deeper property extraction or reproduction-feedback loops.

### 6. Infrastructure reliability matters as much as evidence quality
14096 was lost in v3/v4/v5 to Docker build failures. The evidence is irrelevant if the agent can't even start. SWE-agent's standalone Python build is fragile.

**Rule:** Pre-build and cache Docker images for benchmark tasks. Never rely on build-from-source during evaluation.
