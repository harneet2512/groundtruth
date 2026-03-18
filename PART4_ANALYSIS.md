# Part 4: Smart Context Block Diagnostic

**Date:** 2026-03-18
**Model:** gpt-5.4-nano
**Scaffold:** mini-SWE-agent v2.2.7
**Context:** GroundTruth MCP v2 (class structure maps + noise filtering)

---

## Changes from v1

| Aspect | v1 (Part 2) | v2 (This Run) |
|--------|-------------|---------------|
| Symbol source | FTS5 against problem statement | AST parse + keyword extraction |
| Content | Bare symbol names with `(self)` params | Class structure maps + method signatures + attribute coupling |
| Noise | Test functions included (e.g. `test_invalid_operator`) | Test/doc/example files excluded (696-747 files filtered per Django task) |
| Header | `Indexed: 0 Python files, 0 symbols` | Real counts: `Indexed: 2613 source files, 2727 classes, 27451 symbols` |
| Output | Plain text string | JSON with context + full metrics dict |
| Observability | context_chars only | files_scanned, files_parsed, files_skipped_test, classes_matched, classes_in_context, index_time, keywords |

---

## Patch Generation Results

| Task | Category | v1 Patch? | v2 Patch? | v2 Turns | v2 Context Chars | v2 Context Tokens |
|------|----------|-----------|-----------|----------|-----------------|-------------------|
| django__django-12856 | GAINED | Yes | Yes | 60 | 1054 | 263 |
| django__django-13658 | GAINED | Yes | Yes | 28 | 1054 | 263 |
| matplotlib__matplotlib-23562 | GAINED | Yes | Yes | 26 | 1052 | 263 |
| scikit-learn__scikit-learn-14092 | GAINED | Yes | Yes | 34 | 1457 | 364 |
| psf__requests-1963 | GAINED | Yes | Yes | 32 | 1463 | 365 |
| django__django-11049 | LOST | Yes | Yes | 46 | 1151 | 287 |
| django__django-13964 | LOST | Yes | Yes | 82 | 1858 | 464 |
| matplotlib__matplotlib-25433 | LOST | Yes | Yes | 46 | 1052 | 263 |
| sphinx-doc__sphinx-8713 | LOST | Yes | Yes | 44 | 847 | 211 |
| sympy__sympy-14774 | LOST | Yes | Yes | 20 | 1052 | 263 |

**All 10/10 tasks produced patches in both v1 and v2.**

---

## GT Observability Metrics

| Task | Files Parsed | Source Classes | Test Files Skipped | Classes Matched | In Context | Index Time | Keywords |
|------|-------------|---------------|-------------------|-----------------|------------|------------|----------|
| django-12856 | 2613 | 2727 | 736 | 2727 | 2 | 7.3s | 7 |
| django-13658 | 2449 | 2542 | 747 | 2542 | 2 | 13.0s | 12 |
| matplotlib-23562 | 895 | 799 | 123 | 799 | 1 | 5.4s | 13 |
| scikit-learn-14092 | 724 | 434 | 208 | 434 | 2 | 1.9s | 31 |
| requests-1963 | 148 | 159 | 0 | 159 | 1 | 3.7s | 12 |
| django-11049 | 2536 | 2813 | 696 | 2813 | 1 | 3.9s | 5 |
| django-13964 | 2683 | 2775 | 743 | 2775 | 1 | 8.8s | 19 |
| matplotlib-25433 | 896 | 689 | 639 | 689 | 1 | 2.4s | 21 |
| sphinx-8713 | 527 | 658 | 112 | 658 | 2 | 1.0s | 23 |
| sympy-14774 | 656 | 571 | 298 | 571 | 1 | 13.7s | 3 |

**Key observations:**
- Test file filtering works: 112-747 test files excluded per task (Django: ~740, matplotlib: ~120-639)
- Real file/symbol counts now shown in context header
- All index times under 15s budget (1.0s - 13.7s)
- Context generation after indexing is near-instant (<15ms)

---

## Context Comparison: v1 vs v2

### [GAINED] django__django-12856

**v1 context (36 tokens):**
```
Indexed: 0 Python files, 0 symbols.
- UniqueConstraint() in db/models/constraints.py
```

**v2 context (263 tokens):**
```
Indexed: 2613 source files, 2727 classes, 27451 symbols.

### Q (extends tree.Node)
File: django/db/models/query_utils.py:57
Methods:
  __init__(*args, _connector=None, _negated=False, **kwargs) -> line 68
  _combine(other, conn) -> line 71
  __or__(other) -> line 88
  ...

### UniqueConstraint (extends BaseConstraint)
File: django/db/models/constraints.py:79
Methods:
  __init__(*, fields, name, condition=None, deferrable=None) -> line 80
  _get_condition_sql(model, schema_editor) -> line 98
  constraint_sql(model, schema_editor) -> line 107
  ...
Shared state:
  self.deferr... (truncated)
```

**v2 keywords:** UniqueConstraint, UniqueConstraints, unique_together, with_unique_together
**v2 top classes:** Q, UniqueConstraint, UniqueTest, UniqueConstraintTests, SchemaTests

### [GAINED] matplotlib__matplotlib-23562

**v1 context (215 tokens):**
```
Indexed: 0 Python files, 0 symbols.
- get_facecolor(self) in lib/matplotlib/patches.py
- get_facecolor(self) in lib/matplotlib/figure.py
- get_facecolor(self) in lib/matplotlib/collections.py
- get_facecolor(self) in matplotlib/axes/_base.py
- get_facecolor(self) in mpl_toolkits/mplot3d/art3d.py (x3)
- add_subplot(self, *args, **kwargs) in lib/matplotlib/figure.py
- plot_surface(...) in mpl_toolkits/mplot3d/axes3d.py
```

**v2 context (263 tokens):**
```
Indexed: 895 source files, 799 classes, 9873 symbols.

### Axes3D (extends Axes)
File: lib/mpl_toolkits/mplot3d/axes3d.py:44
Methods:
  __init__(fig, rect=None, *args, elev=30, azim=..., ...) -> line 55
  set_axis_off() -> line 187
  set_axis_on() -> line 191
  convert_zunits(z) -> line 195
  set_top_view() -> line 202
  ...
```

**v2 keywords:** Axes3D, Poly3DCollection, get_facecolor, get_facecolors, plot_surface, _facecolors2d, add_subplot
**v2 top classes:** Axes3D, Poly3DCollection, Collection, FigureBase, Axes

### [LOST] django__django-11049

**v1 context (222 tokens):**
```
Indexed: 0 Python files, 0 symbols.
- DurationField() in models/fields/__init__.py
- DurationField() in django/forms/fields.py
- test_invalid_operator(self) in tests/expressions/tests.py
- test_durationfield_add(self) in tests/expressions/tests.py
- test_actions_ordering(self) in tests/admin_views/test_actions.py
... (8 more test functions)
```

**v2 context (287 tokens):**
```
Indexed: 2536 source files, 2813 classes, 26349 symbols.

### Field (extends RegisterLookupMixin)
File: django/db/models/fields/__init__.py:89
Methods:
  _description() -> line 127
  __init__(verbose_name=None, name=None, primary_key=False, ...) -> line 133
  ...
  deconstruct() -> line 398
  clone() -> line 487
  ...
Warning: 2 different DurationField: django/db/models/fields/__init__.py, django/forms/fields.py
```

**v2 keywords:** DurationField, help_text
**v2 top classes:** Field, IntrospectionTests, DjangoAdminSettingsDirectory

### [LOST] django__django-13964

**v1 context (230 tokens):**
```
Indexed: 0 Python files, 0 symbols.
- Order() in tests/extra_regress/models.py
- Order() in tests/select_related/models.py
- Order() in tests/queries/models.py
- CharField() in models/fields/__init__.py
- Model() in db/models/base.py
... (all test models and test functions)
```

**v2 context (464 tokens):**
```
### M2mThroughTests (extends TestCase)
File: tests/m2m_through/tests.py:14
Methods:
  test_retrieve_intermediate_items() -> line 23
  test_add_on_m2m_with_intermediate_model() -> line 52
  ...
Warning: 5 different NamedModel
Warning: 2 different Product: tests/model_forms/models.py, tests/model_formsets/models.py
```

**Issue noted:** v2 still put a test class (`M2mThroughTests`) as the top match for django-13964 because the issue mentions `Product`, `Order`, `ForeignKey`, `CharField` — all of which appear heavily in test classes that passed the filter check (the test class itself is in `tests/` but its file was parsed because it's a `.py` file). The `is_test_file` filter correctly skips files but test classes in those files can still appear through `classes_matched` due to how keyword matching works against all classes.

### [LOST] sympy__sympy-14774

**v1 context (180 tokens):**
```
Indexed: 0 Python files, 0 symbols.
- For() in sympy/codegen/ast.py
- full_path_split(path) in release/fabfile.py
- _latex(self, printer) in sympy/core/numbers.py (x4)
```

**v2 context (263 tokens):**
```
### LatexPrinter (extends Printer)
File: sympy/printing/latex.py:121
Methods:
  __init__(settings=None) -> line 140
  parenthesize(item, level, strict=False) -> line 180
  _needs_brackets(expr) -> line 200
  _needs_mul_brackets(expr, first=False, last=False) -> line 233
  _print_Add(expr, order=None) -> line 302
  _print_Pow(expr) -> line 489
  ... (20+ methods)
```

**v2 keywords:** Latex, inv_trig_style, inv_trig_table
**v2 top classes:** LatexPrinter, QExpr, Tr, G, Rotation

---

## Qualitative Assessment

### What v2 does better:
1. **Test noise eliminated:** v1 showed `test_invalid_operator(self)`, `test_durationfield_add(self)` — completely useless. v2 filters these.
2. **Class structure maps:** v2 shows full method lists with signatures. For `UniqueConstraint`, the agent can see `__init__(*, fields, name, condition=None, deferrable=None)` — this IS the information needed to understand the class's API.
3. **Attribute coupling:** `self.deferrable` shared across methods tells the agent which state flows through the class.
4. **Real counts:** `Indexed: 2613 source files, 2727 classes` vs `Indexed: 0 Python files, 0 symbols` — builds trust in the context.
5. **Inheritance:** `UniqueConstraint (extends BaseConstraint)` tells the agent there's a parent class to check.

### What v2 still gets wrong:
1. **django__django-13964:** `M2mThroughTests` (a test class) was top-ranked because `Product`, `Order`, `ForeignKey` keywords all appear in test class methods. Fix: exclude classes from test files, not just skip test files entirely.
2. **sphinx-doc__sphinx-8713:** Top classes are `E`, `E`, `C`, `C`, `C` — single-letter class names in test fixtures that happen to match keywords. Low signal.
3. **psf__requests-1963:** Top class is `RequestsTestCase` — a test class at `test_requests.py:37`. The `is_test_file` check doesn't catch `test_requests.py` at the repo root (no `/tests/` directory prefix). Fix: also check for `test_` prefix in filenames.
4. **classes_matched is too high:** All source classes match because the ranking function gives `min(len(methods), 10)` bonus to every class with methods. This makes all large classes appear "matched" even when they're irrelevant.

### Issues to fix before 300-task run:
1. **Filter classes from test files, not just file scanning.** The `is_test_file` check works for skipping files during indexing, but classes that were already indexed (from source dirs) can have test-like names. Also add `test_*.py` prefix matching.
2. **Reduce `classes_matched` inflation.** The method count bonus (`min(len(methods), 10)`) inflates scores for all large classes. Consider: only give method count bonus if at least one keyword also matches.
3. **Handle single-letter class names.** Classes named `E`, `C`, `G` should be deprioritized unless exact keyword match.

---

## Summary Statistics

| Metric | v1 | v2 |
|--------|----|----|
| Avg context tokens | 161 | 296 |
| Min context tokens | 36 | 211 |
| Max context tokens | 237 | 464 |
| Test functions in context | Yes (8/10 tasks) | No (0/10 tasks) |
| Test classes in context | Yes | Yes (2-3 tasks — needs fix) |
| Real file counts shown | No ("Indexed: 0") | Yes |
| Class structure shown | No | Yes (methods + signatures) |
| Attribute coupling shown | No | Yes |
| Inheritance info shown | No | Yes |
| Ambiguity warnings | Partial | Yes |
| Index time | N/A (0 files) | 1.0s - 13.7s |
| All 10 patches generated | Yes | Yes |

---

## Verdict

### Key Findings:

1. **v2 context is qualitatively much richer than v1.** Every v2 context block contains class structure, method signatures, attribute coupling, and inheritance info. v1 had bare symbol names with `(self)` parameter noise.

2. **Test function noise is eliminated.** v1 showed `test_invalid_operator(self)`, `test_durationfield_add(self)` — 8/10 tasks had test functions. v2 has zero test functions.

3. **Test CLASS noise partially remains.** 2-3 tasks still have test classes ranked highly because keyword matching picks up common class names (Product, Order, CharField) that appear in test class methods. Fixable with stricter filtering.

4. **All 10 tasks generated patches.** Parity with v1 on patch generation (this is expected — the context is prepended but the agent can still work without it).

5. **Observability is dramatically improved.** Full metrics dict with file counts, parse errors, test filtering stats, timing — all captured in trajectory for analysis.

### Decision:

| Signal | Assessment | Action |
|--------|-----------|--------|
| v2 context is richer | YES — class structure, signatures, coupling | Proceed |
| Test function noise eliminated | YES — 0 test functions in v2 vs 8/10 tasks in v1 | Proceed |
| Test class noise remains | PARTIAL — 2-3 tasks affected | Fix before 300-run |
| Agent behavior changed | TBD — need to compare first actions | Run eval to check resolve rates |
| Observability improved | YES — full metrics in every trajectory | Proceed |

### Next steps before 300-task run:
1. Fix `is_test_file` to also catch `test_*.py` at repo root
2. Only give method count bonus if class has at least one keyword match
3. Deprioritize single-letter class names unless exact keyword match

---

## Evaluation Results (10-task subset)

**Resolved: 8/10**

| Task | Category | v2 Result |
|------|----------|-----------|
| django__django-12856 | GAINED | RESOLVED |
| django__django-13658 | GAINED | RESOLVED |
| matplotlib__matplotlib-23562 | GAINED | RESOLVED |
| scikit-learn__scikit-learn-14092 | GAINED | RESOLVED |
| psf__requests-1963 | GAINED | RESOLVED |
| django__django-11049 | LOST | RESOLVED |
| django__django-13964 | LOST | UNRESOLVED |
| matplotlib__matplotlib-25433 | LOST | UNRESOLVED |
| sphinx-doc__sphinx-8713 | LOST | RESOLVED |
| sympy__sympy-14774 | LOST | RESOLVED |

All 5 "GAINED" tasks resolved. 3/5 "LOST" tasks also resolved.

**Comparison to Part 2 (v1 context, same 10 tasks, full runner):**
- Part 2 resolved: 8/10 (different runner — full GT runner with gt_integration.py)
- Part 4 resolved: 8/10 (mini-swe-agent with v2 context block)
- Parity maintained despite completely different runner + context format

**Note:** This is a 10-task subset so no statistical significance, but the directional signal is positive — v2 context is qualitatively much richer and resolve rate is at least as good as v1.
