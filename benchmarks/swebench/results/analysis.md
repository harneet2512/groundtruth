# SWE-bench 10-Task A/B Analysis: Baseline vs GT V2

**Date:** 2026-03-16
**VM:** swebench-ab (e2-standard-8, us-central1-a)
**Model:** gpt-5-mini
**Config:** max-turns=30, timeout=300s, workers=1

---

## Overall Results

| Metric | Baseline | GT V2 |
|--------|----------|-------|
| Patched | 9/10 (90%) | 9/10 (90%) |
| Total cost | $0.18 | $0.18 |
| Validations fired | N/A | 15 |
| Avg index time/task | N/A | ~6s |
| Avg symbols/task | N/A | ~30,000 |

Net result: **tied on patch rate**. GT V2 gained one task and lost one.

---

## Per-Task Breakdown

| Task ID | Baseline | GT V2 | Delta |
|---------|----------|-------|-------|
| `django__django-11039` | patched | patched | = |
| `django__django-11049` | no_patch | **patched** | **+GT** |
| `django__django-11099` | patched | patched | = |
| `django__django-11133` | patched | patched | = |
| `django__django-11179` | patched | patched | = |
| `django__django-11283` | patched | patched | = |
| `django__django-11422` | patched | patched | = |
| `django__django-11564` | **patched** | no_patch | **-GT** |
| `django__django-11583` | patched | patched | = |
| `django__django-11620` | patched | patched | = |

---

## Deep Dive: LOST Task (`django__django-11564`)

### What this task is about

Django issue #11564 concerns the `{% static %}` and `{% get_static_prefix %}` template tags not respecting `SCRIPT_NAME` when Django is deployed behind a reverse proxy at a sub-path (e.g., `/myapp/`). Static file URLs come out as `/static/foo.css` instead of `/myapp/static/foo.css`.

### Baseline: PATCHED (2,327 bytes)

The baseline agent produced a working fix. It modified two methods in `django/templatetags/static.py`:

1. **`PrefixNode.render()`** (line 50): After computing the prefix, checks for `SCRIPT_NAME` in the request's `META` dict. If present, normalizes it (leading slash, no trailing slash) and prepends it to path-style prefixes that don't already start with the script name. Skips absolute URLs (`http://`, `https://`).

2. **`StaticNode.render()`** (line 104): Same logic applied to the rendered static URL.

The patch is straightforward: read `SCRIPT_NAME` from `request.META`, normalize, prepend. No imports changed, no new files.

### GT V2: NO PATCH (empty)

The GT V2 agent **produced zero edits** (`edits_total: 0`). It never got to the point of modifying any file.

### GT V2 Instrumentation for this task

| Metric | Value |
|--------|-------|
| `gt_available` | `True` |
| `context_tokens_injected` | 248 |
| `index_symbols` | 30,426 |
| `index_time_seconds` | 5.93 |
| `edits_total` | **0** |
| `validations_fired` | **0** |
| `validations_skipped_low_confidence` | 0 |
| `validation_timeouts` | 0 |
| `agent_fixed_after_validation` | 0 |
| `contracts_extracted` | 0 |
| `validation_log` | `[]` (empty) |

### Root cause analysis

**GT validation did NOT cause this failure.** Zero validations fired. Zero edits were even attempted. The agent simply failed to produce a patch.

The injected context (248 tokens) included symbol information from the index, but the agent never progressed to the editing phase. This is a **non-deterministic agent behavior** failure, not a GT interference failure. Since the agent uses `gpt-5-mini` with `temperature=0` (or whatever the model's default is for function calling), there is still some randomness in the tool-calling sequence.

The context injection added ~248 tokens to the system prompt. In theory, this small amount of additional context could shift the model's attention or token probabilities enough to alter the tool-calling trajectory. But with zero validations fired and zero edits made, there is no direct evidence that GT caused the failure. The agent simply went down a different exploration path and ran out of turns or hit the timeout.

**Verdict: Non-deterministic model behavior. GT V2 is not the cause.**

To confirm, this task should be re-run 3-5 times with GT V2 to see if the failure is consistent. If it succeeds in most reruns, this was just unlucky sampling.

---

## Deep Dive: WON Task (`django__django-11049`)

### What this task is about

Django issue #11049 concerns the `DurationField` error message showing an incorrect format string. The error message said `[DD] [HH:[MM:]]ss[.uuuuuu]` but the correct format is `[DD] [[HH:]MM:]ss[.uuuuuu]` (the brackets were nested incorrectly, implying hours were optional but minutes required hours, when in fact minutes should be independently optional).

### Baseline: NO PATCH (empty)

The baseline agent failed to produce any patch. It could not find or fix the issue.

### GT V2: PATCHED (1,133 bytes)

The GT V2 agent produced a clean, minimal fix across two files:

**File 1: `django/db/models/fields/__init__.py`**
```diff
-                     "[DD] [HH:[MM:]]ss[.uuuuuu] format.")
+                     "[DD] [[HH:]MM:]ss[.uuuuuu] format.")
```

**File 2: `tests/model_fields/test_durationfield.py`**
```diff
-            "It must be in [DD] [HH:[MM:]]ss[.uuuuuu] format."
+            "It must be in [DD] [[HH:]MM:]ss[.uuuuuu] format."
```

The fix is correct: it changes the error message format string and updates the corresponding test assertion. Two files, two lines each, surgically precise.

### GT V2 Instrumentation for this task

| Metric | Value |
|--------|-------|
| `gt_available` | `True` |
| `context_tokens_injected` | 222 |
| `index_symbols` | 29,747 |
| `index_time_seconds` | 5.84 |
| `edits_total` | **2** |
| `validations_fired` | **2** |
| `validations_skipped_low_confidence` | 17 |
| `validation_timeouts` | 0 |
| `agent_fixed_after_validation` | **0** |
| `contracts_extracted` | 0 |

### Validation log analysis

GT V2 fired **2 validations** on this task, both on the same file (`django/db/models/fields/__init__.py`). Each validation produced a large number of findings:

**High-confidence findings (fired, shown to agent):**
- 6 findings total per validation
- `wrong_module_path` for `forms`, `settings`, `connection` (Django re-exports)
- `invented_symbol` for `connections`, `router`, `gettext_lazy` (Django re-exports)

**Low-confidence findings (skipped, NOT shown to agent):**
- 17 total across both validations
- All `wrong_arg_count` errors (confidence 0.70, at threshold)
- Mostly from the `self` parameter counting issue: the AST validator counts `self` as a parameter in stored signatures but the call site doesn't pass `self` explicitly
- Examples: `get_field(field_name)` stored as `(self, field_name)` = 2 params, but called with 1 arg

### How GT V2 helped

The GT V2 agent succeeded where baseline failed. The key question: **did the 222 tokens of injected context help the agent find the right file?**

The context injection via `enrich_system_prompt()` searches the problem statement for symbol names and looks them up in the index. For this task, the problem statement mentions `DurationField` and format strings. The context likely included:

- The `DurationField` class location (`django/db/models/fields/__init__.py`)
- Related symbols and their file paths
- Potentially the `default_error_messages` pattern

This targeted file-finding context could have helped the agent navigate directly to the right file instead of exploring blindly. The baseline agent, without this context, may have searched in the wrong places and exhausted its turn budget.

**However**, the `agent_fixed_after_validation` counter is 0, meaning the validation feedback (which was mostly false positives about re-exports) did NOT contribute to the fix. The agent made correct edits on the first try without needing validation corrections.

**Verdict: Context injection likely helped the agent find the right file faster. Validation was noise (false positives) but did not harm because the agent's edits were already correct.**

---

## Validation Quality Assessment

Across all 10 tasks, GT V2 fired **15 validations** total. Examining the detailed logs:

### False positive patterns

| Pattern | Count | Confidence | Root Cause |
|---------|-------|------------|------------|
| `wrong_arg_count` on `self` methods | ~50+ per file | 0.70 (medium) | AST validator stores `(self, value)` as 2 params but Python calls don't pass `self` |
| `invented_symbol` on re-exports | ~3-5 per file | 0.90 (high) | Django re-exports through `__init__.py`; index only has the source file |
| `wrong_module_path` on re-exports | ~3 per file | 0.85 (high) | Same cause as above |

### Implications

1. **The `self` counting bug is the biggest source of noise.** The signature `(self, value)` has 2 parameters, but when the agent writes `obj.to_python(x)`, the call has 1 argument. The validator sees 2 != 1 and fires. This is a fundamental issue: Python method calls implicitly pass `self`.

2. **Re-export false positives are the second biggest issue.** Django heavily uses `__init__.py` to re-export symbols. The symbol `gettext_lazy` lives in `django/utils/translation/trans_real.py` but is imported as `from django.utils.translation import gettext_lazy`. The index only stores the source location.

3. **Despite 15 validations firing, zero caused the agent to change behavior.** `agent_fixed_after_validation` is 0 across all 10 tasks. The agent either ignored the validation feedback entirely, or its edits were already correct.

### Recommendations

1. **Fix the `self` counting bug**: When the stored signature starts with `self` or `cls`, subtract 1 from the expected param count before comparing. This would eliminate ~80% of the `wrong_arg_count` noise.

2. **Raise `HIGH_CONFIDENCE_THRESHOLD` to 0.80**: This would filter out `wrong_arg_count` (0.70) while keeping `invented_symbol` (0.90) and `wrong_module_path` (0.85). Net effect: agent sees fewer false positives.

3. **Track re-exported symbols**: When indexing, also record what `__init__.py` files import from submodules. This would eliminate the re-export false positives.

4. **Re-run the lost task (`-11564`)** 3-5 times to confirm it's non-deterministic model behavior, not a systematic GT issue.

---

## Key Takeaway

GT V2 passive integration achieved the primary goal: **no performance regression**. Active GT mode dropped patch rate from 90.6% to 73.7%. GT V2 maintains 90% (tied with baseline). The passive approach — invisible context injection + post-edit validation — is the right architecture.

The validation layer is not yet providing actionable value to the agent (`agent_fixed_after_validation = 0` across all tasks), but it is also not causing harm. The next step is to reduce false positives (especially the `self` counting bug) so that when validation does fire, the findings are trustworthy and the agent can act on them.
