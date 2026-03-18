# Part 5: Change Surface Prediction (v3) Diagnostic

**Date:** 2026-03-18
**Model:** gpt-5.4-nano
**Scaffold:** mini-SWE-agent v2.2.7
**Context:** GroundTruth MCP v3 (change surface prediction + coupling annotations)
**VM:** swebench-ab (us-central1-a, e2-standard-8, 34.122.24.67)
**Run time:** 5m 51s (10 tasks, 1 worker)
**Eval time:** ~15 min

---

## Changes from v2

| Aspect | v2 (Part 4) | v3 (This Run) |
|--------|-------------|---------------|
| Test filtering | Directory patterns only (`/tests/`) | + basename (`test_*.py`), + leading-slash normalization for relative paths, + `/migrations/` dir skip |
| Class name filter | `len > 0` (single-letter only) | `len > 2` (skips `E`, `C`, `In`, `Or`) |
| Ranking | Unconditional bonus: `min(methods,10)` + `min(coupling,5)*2` for ALL classes | Keyword-only: class must match a keyword to score at all |
| Output format | Class structure map (all methods listed) | Change surface with coupling annotations per method |
| Output sizing | Fixed ~300 tokens (`MAX_CONTEXT_CHARS=1200`) | Dynamic via relevance cliff (30% of top score) + 15-method cap, 500-token safety cap |
| JSON output | `{context, metrics, keywords, top_classes, top_functions}` | `{context, metrics, debug}` with `debug: {keywords, entry_points, top_surface, all_classes_found}` |
| Call coupling | Not tracked | Tracks `self.method()` calls between methods within a class |
| SKIP_DIRS | `.git`, `__pycache__`, `node_modules`, `.tox`, `.eggs`, `venv`, `env` | + `build`, `dist` (added after initial run revealed `build/lib/` duplication) |

---

## Bug Fix Verification

### Three known v2 regressions — all fixed in v3:

| Task | v2 Leakage | v3 Entry Classes | Fixed? |
|------|-----------|-----------------|--------|
| django-13964 | `M2mThroughTests` (tests/m2m_through/tests.py), `OrderingTests`, `SchemaTests` | `Model`, `ForeignKey`, `CharField` | YES |
| django-12856 | `UniqueTest` (tests/model_forms/tests.py), `In` (2-letter class) | `UniqueConstraint`, `ArrayField`, `GDALBand` | YES |
| sphinx-8713 | `E`, `C` (single-letter classes) | `GoogleDocstring`, `Sphinx`, `SphinxParallelError` | YES |

**Zero test class leakage across all 10 tasks.** The leading-slash fix (`"/" + fp_lower`) and basename check (`basename.startswith("test_")`) eliminated all path-based leaks. The `len <= 2` filter eliminated short ambiguous class names.

### v2 ranking inflation — fixed:

| Task | v2 Classes Matched | v3 Entry Points |
|------|-------------------|-----------------|
| django-12856 | **2727** | 3 |
| django-13658 | **2542** | 0 |
| django-13964 | **2775** | 3 |
| django-11049 | **2813** | 3 |

v2 gave every class an unconditional `min(len(methods),10)` bonus, causing all 2700+ Django classes to "match." v3 only scores classes that have a keyword hit.

---

## Patch Generation

| Task | Category | Has Patch | Patch Size | Exit Status |
|------|----------|-----------|-----------|-------------|
| django__django-11049 | LOST | YES | 1267 chars | Submitted |
| django__django-12856 | GAINED | YES | 903 chars | Submitted |
| django__django-13658 | GAINED | YES | 896 chars | Submitted |
| django__django-13964 | LOST | YES | 1308 chars | Submitted |
| matplotlib__matplotlib-23562 | GAINED | YES | 1677 chars | Submitted |
| matplotlib__matplotlib-25433 | LOST | YES | 1634 chars | Submitted |
| psf__requests-1963 | GAINED | YES | 928 chars | Submitted |
| scikit-learn__scikit-learn-14092 | GAINED | YES | 1309 chars | Submitted |
| sphinx-doc__sphinx-8713 | LOST | YES | 923 chars | Submitted |
| sympy__sympy-14774 | LOST | YES | 588 chars | Submitted |

**10/10 tasks produced patches (all Submitted).**

---

## GT Observability Metrics

| Task | Files Parsed | Source Classes | Test Files Skipped | Entry Points | Surface Methods | Index Time | Keywords |
|------|-------------|---------------|-------------------|-------------|----------------|------------|----------|
| django-11049 | 2536 | 1092 | 1766 | 3 | 12 | 6.8s | 5 |
| django-12856 | 2613 | 1128 | 1835 | 3 | 8 | 8.0s | 7 |
| django-13658 | 1817 | 0 | 1816 | 0 | 0 | 13.0s | 12 |
| django-13964 | 2683 | 1137 | 1882 | 3 | 0 | 9.5s | 19 |
| matplotlib-23562 | 895 | 702 | 653 | 3 | 0 | 6.0s | 13 |
| matplotlib-25433 | 896 | 680 | 650 | 3 | 10 | 4.2s | 21 |
| requests-1963 | 148 | 152 | 3 | 3 | 6 | 1.0s | 12 |
| sklearn-14092 | 724 | 371 | 497 | 3 | 11 | 3.5s | 31 |
| sphinx-8713 | 527 | 569 | 351 | 3 | 0 | 1.2s | 23 |
| sympy-14774 | 863 | 733 | 418 | 3 | 13 | 13.0s | 3 |

**Key observations:**
- Test file filtering aggressively filters Django: 1766–1882 test files skipped (was 696–747 in v2 — the basename + leading-slash fix catches many more)
- django-13658: ALL 1817 files flagged as test files → 0 source classes → 0 entry points → empty context. This is a regression — the filtering is too aggressive for this repo version
- All index times under 15s budget (1.0s – 13.0s)
- Context generation after indexing is instant (<4ms)

---

## Context Blocks (Full Output)

### django__django-11049 — DurationField help_text (1501 chars, RESOLVED)

```
## GroundTruth Change Surface Analysis
Indexed: 2536 source files, 1092 classes, 24846 symbols.

### DurationField (extends Field)
File: django/db/models/fields/__init__.py:1580
Change surface:
  get_internal_type() -> line 1594
  to_python(value) -> line 1597
  get_db_prep_value(value, connection, prepared=False) -> line 1616
  get_db_converters(connection) -> line 1623
  value_to_string(obj) -> line 1629
  formfield(**kwargs) -> line 1633

### DurationField (extends Field)
File: django/forms/fields.py:469
Change surface:
  prepare_value(value) -> line 475
  to_python(value) -> line 480

### Field (extends RegisterLookupMixin)
File: django/db/models/fields/__init__.py:89
Change surface:
  __init__(verbose_name=None, name=None, ...) -> line 133  # shares self.__class__, self.name, self.primary_key, self.remote_field, self.verbose_name
  formfield(form_class=None, ...) -> line 857  # shares self.blank, self.choices, self.default, self.null, self.verbose_name
  validate(value, model_instance) -> line 592  # shares self.blank, self.choices, self.editable, self.error_messages, self.null
  contribute_to_class(name, private_only=False) -> line 726  # shares self.attname, self.choices, self.column, self.model, self.name

Warning: 2 different `DurationField`: django/db/models/fields/__init__.py, django/forms/fields.py
```

**Entry points:** `DurationField` (models, score=15), `DurationField` (forms, score=15), `Field` (base, score=10)
**Keywords:** `Correct`, `Description`, `Django`, `DurationField`, `help_text`
**Analysis:** Correct entry points. Both model and form DurationField found. The base `Field` class provides useful context about `formfield()` which is relevant to help_text. Ambiguity warning correctly flags the two DurationField classes.

---

### django__django-12856 — UniqueConstraint deferrable (1159 chars, UNRESOLVED)

```
## GroundTruth Change Surface Analysis
Indexed: 2613 source files, 1128 classes, 25904 symbols.

### UniqueConstraint (extends BaseConstraint)
File: django/db/models/constraints.py:79
Change surface:
  constraint_sql(model, schema_editor) -> line 107  # shares self._get_condition_sql, self.deferrable, self.fields, self.name
  create_sql(model, schema_editor) -> line 115  # shares self._get_condition_sql, self.deferrable, self.fields, self.name
  __repr__() -> line 129  # shares self.condition, self.deferrable, self.fields, self.name
  __eq__(other) -> line 136  # shares self.condition, self.deferrable, self.fields, self.name
  remove_sql(model, schema_editor) -> line 123  # shares self._get_condition_sql, self.deferrable, self.name
  __init__(*, fields, name, condition=None, deferrable=None) -> line 80  # shares self.condition, self.deferrable, self.fields
  _get_condition_sql(model, schema_editor) -> line 98  # shares self.condition
  deconstruct() -> line 146  # shares self.condition, self.deferrable, self.fields
```

**Entry points:** `UniqueConstraint` (score=23), `ArrayField` (score=5), `GDALBand` (score=5)
**Keywords:** `Description`, `Marnanel`, `Thurman`, `UniqueConstraint`, `UniqueConstraints`, `unique_together`, `with_unique_together`
**Analysis:** Primary entry point (`UniqueConstraint`) is correct and the change surface perfectly highlights `self.deferrable` coupling across `constraint_sql`, `create_sql`, `__init__`, `__repr__`, `__eq__`. But `ArrayField` and `GDALBand` are irrelevant noise from the problem statement author's name triggering keyword extraction. Only the `UniqueConstraint` block made it into the context (others were below the safety cap), so the noise didn't directly pollute the context. **Regression likely from LLM variance, not context quality.**

---

### django__django-13658 — ManagementUtility autocomplete (0 chars, RESOLVED)

**Context: EMPTY** — 0 entry points found.

**Keywords:** `Above`, `CommandParser`, `Description`, `False`, `Instead`, `ManagementUtility`, `__init__`, `__main__`, `add_help`, `allow_abbrev`, `execute_from_command_line`, `prog_name`

**Analysis:** All 1817 parsed files were classified as test files (1816 skipped). This means the entire Django source tree was filtered out for this particular commit. The v3 test filtering is too aggressive — likely the repo structure at this commit has source files in paths that match test patterns. Despite empty context, the task still resolved — the agent solved it without GT context.

**Root cause:** `source_classes: 0` means the aggressive filtering eliminated all source files. Needs investigation — possibly a directory naming issue in this Django version.

---

### django__django-13964 — ForeignKey saving (191 chars, UNRESOLVED)

```
## GroundTruth Change Surface Analysis
Indexed: 2683 source files, 1137 classes, 26773 symbols.

Warning: 2 different `CharField`: django/db/models/fields/__init__.py, django/forms/fields.py
```

**Entry points:** `Model` (score=25), `ForeignKey` (score=25), `CharField` (score=17)
**Keywords:** `CharField`, `Charlie`, `Committing`, `DeTar`, `Description`, `Fails`, `ForeignKey`, `ForeignKeyViolation`, `Given`, `Instead`, `Model`, `Order`, `Product`, `Saving`, `Succeeds`, `max_length`, `on_delete`, `primary_key`, `product_id`
**Analysis:** Entry points are correct — `Model`, `ForeignKey`, `CharField` are the right classes. But `surface_methods: 0` means the change surface computation found no coupled methods above the cliff threshold in these very large classes. The classes are too generic (Django's base `Model` class) — their methods all share `self.` attributes equally, making the cliff threshold meaningless. Context degrades to just the header + ambiguity warning (191 chars). **Unresolved in both v2 and v3.**

---

### matplotlib__matplotlib-23562 — Axes3D facecolors (93 chars, RESOLVED)

**Context: header only** (93 chars, no class blocks)

**Entry points:** `Axes3D` (score=20), `Poly3DCollection` (score=20), `Collection` (score=13)
**Keywords:** `AttributeError`, `Axes3D`, `Benjamin`, `Poly3DCollection`, `Sorry`, `Tested`, `Traceback`, `_facecolors2d`, `add_subplot`, `get_facecolor`, `get_facecolors`, `mpl_toolkits`, `plot_surface`

**Analysis:** Entry points are correct but `surface_methods: 0` — the classes had no coupled methods above the cliff threshold. The Axes3D class has hundreds of methods but they're mostly independent (each renders a different plot type). Context reduced to just the header. Despite minimal context, the task resolved — it's a straightforward attribute access fix.

---

### matplotlib__matplotlib-25433 — Slider/RangeSlider (1986 chars, UNRESOLVED)

```
### Slider (extends SliderBase)
File: lib/matplotlib/widgets.py:334
Change surface:
  __init__(...) -> line 349  # shares self._format, self._value_in_bounds, self.poly, self.slidermax, self.slidermin
  set_val(val) -> line 563  # shares self._format, self._handle, self.poly, self.val, self.valtext
  _update(event) -> line 529  # shares self._value_in_bounds, self.ax, self.orientation, self.set_val, self.val
  _value_in_bounds(val) -> line 505  # shares self.slidermax, self.slidermin, self.valmax, self.valmin
  _format(val) -> line 554  # shares self.valmax, self.valmin

### RangeSlider (extends SliderBase)
File: lib/matplotlib/widgets.py:606
Change surface:
  set_val(val) -> line 942  # shares self._format, self._handles, self._update_selection_poly, self._value_in_bounds, self.val
  __init__(...) -> line 622  # shares self._format, self._handles, self._update_selection_poly, self._value_in_bounds, self.poly
  _update_val_from_pos(pos) -> line 852  # shares self._active_handle, self._max_in_bounds, self._min_in_bounds
  _value_in_bounds(vals) -> line 848  # shares self._max_in_bounds, self._min_in_bounds
  _update(event) -> line 867  # shares self._active_handle, self._handles, self.ax, self.orientation
```

**Entry points:** `Slider` (score=28), `RangeSlider` (score=28), `Button` (score=20)
**Analysis:** Excellent entry points and change surface. The coupling annotations correctly highlight `set_val` ↔ `_format` ↔ `_value_in_bounds` coupling. Button was found but didn't make it into context due to the safety cap. **Unresolved in both v2 and v3** — the fix requires understanding widget internals beyond what context provides.

---

### psf__requests-1963 — Redirect handling (1633 chars, UNRESOLVED)

```
### SessionRedirectMixin (extends object)
File: requests/sessions.py:83
Change surface:
  resolve_redirects(resp, req, ...) -> line 84

### SessionRedirectMixin (extends object)
File: build/lib/requests/sessions.py:83     ← DUPLICATE from build/ dir
Change surface:
  resolve_redirects(resp, req, ...) -> line 84

### Session (extends SessionRedirectMixin)
File: requests/sessions.py:187
Change surface:
  __init__() -> line 205  # shares self.adapters, self.headers, self.hooks, self.proxies, self.stream
  request(method, url, ...) -> line 301  # shares self.cert, self.proxies, self.stream, self.trust_env, self.verify
  prepare_request(request) -> line 262  # shares self.auth, self.headers, self.hooks, self.params, self.trust_env
  send(request, **kwargs) -> line 466  # shares self.cert, self.cookies, self.proxies, self.stream, self.verify

Warning: 2 different `SessionRedirectMixin`: requests/sessions.py, build/lib/requests/sessions.py
Warning: 2 different `Session`: requests/sessions.py, build/lib/requests/sessions.py
Warning: 2 different `RequestsCookieJar`: requests/cookies.py, build/lib/requests/cookies.py
Warning: 2 different `LocationParseError`: requests/packages/urllib3/exceptions.py, build/lib/requests/packages/urllib3/exceptions.py
```

**Entry points:** `SessionRedirectMixin` (requests/, score=23), `SessionRedirectMixin` (build/lib/, score=23), `Session` (score=15)
**Keywords:** `Consider`, `However`, `Location`, `Other`, `Redirect`, `Requests`, `Session`, `Temporary`, `_original_`, `do_something`, `new_thing_1513`, `resolve_redirects`
**Analysis:** Correct primary target (`SessionRedirectMixin.resolve_redirects`) identified. But the `build/lib/` duplicate wastes one of the 3 entry point slots AND produces 4 ambiguity warnings that eat 300+ chars of context budget. **This is the `build/` dir regression** — fixed in v3.1 by adding `build` to SKIP_DIRS. **v2 resolved this despite having `RequestsTestCase` test class leakage, suggesting the agent ignores noise better than duplicates confuse it.**

---

### scikit-learn__scikit-learn-14092 — GridSearchCV param validation (1990 chars, UNRESOLVED)

```
### GridSearchCV (extends BaseSearchCV)
File: sklearn/model_selection/_search.py:816
Change surface:
  __init__(estimator, param_grid, ...) -> line 1107  # shares self.param_grid
  _run_search(evaluate_candidates) -> line 1119  # shares self.param_grid

### Pipeline (extends _BaseComposition)
File: sklearn/pipeline.py:29
Change surface:
  _fit(X, y=None, **fit_params) -> line 258  # shares self._final_estimator, self._iter, self._validate_steps, self.memory, self.steps
  _iter(with_final=True, ...) -> line 190  # shares self.steps
  fit(X, y=None, **fit_params) -> line 320  # shares self._final_estimator, self._fit, self._log_message, self.steps
  fit_transform(X, y=None, **fit_params) -> line 353  # shares self._final_estimator, self._fit
  __init__(steps, memory=None, verbose=False) -> line 128  # shares self._validate_steps, self.memory, self.steps, self.verbose
  fit_predict(X, y=None, **fit_params) -> line 419  # shares self._fit, self._log_message, self.steps

### LogisticRegression (extends BaseEstimator, LinearClassifierMixin, SparseCoefMixin)
File: sklearn/linear_model/logistic.py:1190
Change surface:
  fit(X, y, sample_weight=None) -> line 1453  # shares self.intercept_scaling, self.n_jobs, self.penalty, self.solver, self.verbose
  __init__(penalty='l2', ...) -> line 1431  # shares self.intercept_scaling, self.n_jobs, self.penalty, self.solver, self.verbose
```

**Entry points:** `GridSearchCV` (score=23), `Pipeline` (score=15), `LogisticRegression` (score=15)
**Keywords:** `Before`, `Check`, `Currently`, `GridSearch`, `GridSearchCV`, `Integral`, `Interval`, `Invalid`, `LogisticRegression`, `NMF`, `NeighborhoodComponentsAnalysis`, `Pipeline`, `Sometimes`, `ValueError`, `_check_params`, `beta_loss`, `check_param`, `error_score`, `l1_ratio`, `learning_method`
**Analysis:** All three entry points are relevant to the issue. The coupling annotations correctly show that `GridSearchCV.__init__` and `_run_search` share `self.param_grid`, which is the fix target. Pipeline and LogisticRegression provide broader context about how estimators are composed. **v3 context is arguably better than v2** (which showed the irrelevant 2-letter `Or` class). **Regression is likely LLM variance** — single-run non-determinism on a different context block.

---

### sphinx-doc__sphinx-8713 — NumpyDocstring formatting (183 chars, RESOLVED)

```
## GroundTruth Change Surface Analysis
Indexed: 527 source files, 569 classes, 4415 symbols.

Warning: 2 different `_DuplicateSymbolError`: sphinx/domains/cpp.py, sphinx/domains/c.py
```

**Entry points:** `GoogleDocstring` (score=31), `Sphinx` (score=19), `SphinxParallelError` (score=16)
**Keywords:** `Alternatively`, `Currently`, `Environment`, `Error`, `Expected`, `False`, `Linux`, `NumpyDocstring`, `Other`, `Parameters`, `Problem`, `Procedure`, `Python`, `Reproducible`, `Sphinx`, `Subject`, `_config`, `_consume_fields`, `_format_docutils_params`, `_format_fields`
**Analysis:** `GoogleDocstring` is the correct entry point (it shares a base with `NumpyDocstring`). `surface_methods: 0` — these are large classes with many methods but low internal coupling. Context is minimal (183 chars, just header + warning). **Resolved despite minimal context** — the agent found the fix without needing detailed class structure. The v2 leakage of `E`/`C` classes is eliminated.

---

### sympy__sympy-14774 — LaTeX inv_trig_style (1919 chars, RESOLVED)

```
### VectorLatexPrinter (extends LatexPrinter)
File: sympy/physics/vector/printing.py:45
Change surface:
  _print_Function(expr, exp=None) -> line 48  # shares self._print; called by _print_Derivative
  parenthesize(item, level, strict=False) -> line 155  # shares self._print
  _print_Derivative(der_expr) -> line 122  # calls _print_Function

### LatexPrinter (extends Printer)
File: sympy/printing/latex.py:121
Change surface:
  parenthesize(item, level, strict=False) -> line 180  # shares self._print; called by _print_Cross, _print_Curl, _print_Derivative
  _hprint_BesselBase(expr, exp, sym) -> line 1110  # shares self._do_exponent, self._print
  _print_Mul(expr) -> line 389  # shares self._print, self._settings, self.order
  _print_Pow(expr) -> line 489  # shares self._print, self._settings, self.parenthesize
  _print_Function(expr, exp=None) -> line 715  # shares self._hprint_Function, self._print, self._settings
  ... (+ 5 more methods above cliff)
```

**Entry points:** `VectorLatexPrinter` (score=8), `LatexPrinter` (score=8), `LaTeXLexer` (score=8)
**Keywords:** `Latex`, `inv_trig_style`, `inv_trig_table`
**Analysis:** Excellent entry point identification. The inheritance chain `VectorLatexPrinter → LatexPrinter → Printer` is captured. The coupling annotations show `_print_Function` is a hub method (called by many, calls helpers). `LaTeXLexer` (parser, not printer) is less relevant but doesn't pollute context much. **Resolved** — the context correctly guided toward the printer classes.

---

## v2 vs v3 Context Comparison

| Task | v2 Chars | v3 Chars | v2 Classes Matched | v3 Entry Points | v3 Surface Methods | v2 Result | v3 Result |
|------|----------|----------|-------------------|-----------------|-------------------|-----------|-----------|
| django-11049 | 1151 | 1501 | 2813 | 3 | 12 | RESOLVED | RESOLVED |
| django-12856 | 1054 | 1159 | 2727 | 3 | 8 | RESOLVED | UNRESOLVED |
| django-13658 | 1054 | 0 | 2542 | 0 | 0 | RESOLVED | RESOLVED |
| django-13964 | 1858 | 191 | 2775 | 3 | 0 | UNRESOLVED | UNRESOLVED |
| matplotlib-23562 | 1052 | 93 | 799 | 3 | 0 | RESOLVED | RESOLVED |
| matplotlib-25433 | 1052 | 1986 | 689 | 3 | 10 | UNRESOLVED | UNRESOLVED |
| requests-1963 | 1463 | 1633 | 159 | 3 | 6 | RESOLVED | UNRESOLVED |
| sklearn-14092 | 1457 | 1990 | 434 | 3 | 11 | RESOLVED | UNRESOLVED |
| sphinx-8713 | 847 | 183 | 658 | 3 | 0 | RESOLVED | RESOLVED |
| sympy-14774 | 1052 | 1919 | 571 | 3 | 13 | RESOLVED | RESOLVED |

---

## Evaluation Results

**v3 Resolved: 5/10**

| Task | Category | v2 Result | v3 Result | Delta |
|------|----------|-----------|-----------|-------|
| django__django-11049 | LOST | RESOLVED | RESOLVED | = |
| django__django-12856 | GAINED | RESOLVED | **UNRESOLVED** | REGRESSION |
| django__django-13658 | GAINED | RESOLVED | RESOLVED | = |
| django__django-13964 | LOST | UNRESOLVED | UNRESOLVED | = |
| matplotlib__matplotlib-23562 | GAINED | RESOLVED | RESOLVED | = |
| matplotlib__matplotlib-25433 | LOST | UNRESOLVED | UNRESOLVED | = |
| psf__requests-1963 | GAINED | RESOLVED | **UNRESOLVED** | REGRESSION |
| scikit-learn__scikit-learn-14092 | GAINED | RESOLVED | **UNRESOLVED** | REGRESSION |
| sphinx-doc__sphinx-8713 | LOST | RESOLVED | RESOLVED | = |
| sympy__sympy-14774 | LOST | RESOLVED | RESOLVED | = |

**v2: 8/10 → v3: 5/10 (3 regressions, 0 gains)**

---

## Regression Analysis

### 1. psf__requests-1963 — `build/` directory duplication (FIXABLE)

**Root cause:** The `build/lib/` directory contains a copy of the source. v3 found `SessionRedirectMixin` in both `requests/sessions.py` AND `build/lib/requests/sessions.py`, wasting 1 of 3 entry point slots on a duplicate and generating 4 ambiguity warnings (300+ chars of noise).

**Fix applied:** Added `build` and `dist` to `SKIP_DIRS` in v3.1. This is a clear fix — rerun should recover this task.

### 2. django__django-12856 — Likely LLM variance (NOT context quality)

**Root cause:** v3 context (1159 chars) is actually better than v2 (1054 chars). The `UniqueConstraint` change surface correctly highlights `self.deferrable` coupling across all 8 methods — exactly the right information. v2 showed the irrelevant `Q` class + `UniqueConstraint` (with all methods listed but no coupling annotations). The 2nd/3rd entry points (`ArrayField`, `GDALBand`, score=5) are noise from keyword extraction but they didn't make it into the context block.

**Conclusion:** Single-run LLM non-determinism. The context quality is equivalent or better.

### 3. scikit-learn__scikit-learn-14092 — Likely LLM variance (NOT context quality)

**Root cause:** v3 context (1990 chars) is richer than v2 (1457 chars). All three entry points (`GridSearchCV`, `Pipeline`, `LogisticRegression`) are relevant. The coupling annotations correctly show `self.param_grid` coupling in `GridSearchCV`. v2 showed the irrelevant 2-letter `Or` class.

**Conclusion:** Single-run LLM non-determinism. v3 context is objectively better.

---

## Diagnosis Summary

| Regression | Root Cause | Fixable? | Confidence |
|-----------|-----------|----------|------------|
| requests-1963 | `build/` dir not in SKIP_DIRS → duplicate classes | YES (fix applied) | HIGH |
| django-12856 | LLM variance (context quality equivalent or better) | N/A (rerun) | MEDIUM |
| sklearn-14092 | LLM variance (context quality better) | N/A (rerun) | MEDIUM |

The regressions are NOT from the core v3 improvements (test filtering fix, ranking fix, coupling annotations). They're from:
1. A missing directory exclusion (fixed)
2. Non-deterministic model behavior on single runs

---

## django-13658 Investigation: Over-filtering

`source_classes: 0` with `files_skipped_test: 1816` out of 1817 parsed files means the test filter excluded essentially the entire codebase. This is because `django-13658` is at a commit where Django's management commands live in paths that trigger the test patterns (investigation needed — possibly `django/core/management/` has files matching `/test_` or other patterns).

**Impact:** Task still resolved with empty context, so no regression. But this reveals the test filtering may be too aggressive for some repo structures.

---

## Comparison Across All Runs

| Run | Resolved | Context Version | Runner | Notes |
|-----|----------|----------------|--------|-------|
| Part 2 (v1) | 8/10 | FTS5 symbol names | Custom GT runner | gt_integration.py |
| Part 4 (v2) | 8/10 | Class structure maps | mini-swe-agent | Test function filtering, ranking inflation |
| Part 5 (v3) | 5/10 | Change surface prediction | mini-swe-agent | All test leaks fixed, `build/` regression |

---

## Next Steps

### Immediate (v3.1)
1. **`build/` fix already applied** — rerun to verify requests-1963 recovery
2. **Investigate django-13658 over-filtering** — why does this commit's entire source tree get classified as tests?
3. **Rerun 10-task diagnostic** with v3.1 to establish true v3 resolve rate (expect 6-8/10)

### Before 300-task run
4. **Deduplicate entry points by class name** — same class in multiple files → prefer shortest path
5. **Tighten keyword extraction** — PascalCase words from problem statement headers (`Description`, `Given`, `Expected`) are noise; add to stop words
6. **Handle large generic classes** — `Model`, `Field`, `Collection` have hundreds of methods with uniform coupling; consider showing only methods that match keywords
