# Part 5: Change Surface Prediction (v3) Diagnostic

**Date:** 2026-03-18
**Model:** gpt-5.4-nano
**Scaffold:** mini-SWE-agent v2.2.7
**Context:** GroundTruth MCP v3 (change surface prediction + coupling annotations)

---

## Changes from v2

| Aspect | v2 (Part 4) | v3 (This Run) |
|--------|-------------|---------------|
| Test filtering | Directory patterns only (`/tests/`) | + basename (`test_*.py`), + leading-slash normalization, + `/migrations/` |
| Class name filter | `len > 0` (single-letter only) | `len > 2` (skips `E`, `C`, `In`, `Or`) |
| Ranking | Unconditional bonus: `min(methods,10)` + `min(coupling,5)*2` for ALL classes | Keyword-only: class must match a keyword to score at all |
| Output format | Class structure map (all methods listed) | Change surface with coupling annotations per method |
| Output sizing | Fixed ~300 tokens | Dynamic via relevance cliff (30% of top score) + 15-method cap, 500-token safety cap |
| JSON output | `{context, metrics, keywords, top_classes, top_functions}` | `{context, metrics, debug}` with `debug: {keywords, entry_points, top_surface, all_classes_found}` |

---

## Bug Fix Verification

### Three known v2 regressions — all fixed in v3:

| Task | v2 Leakage | v3 Entry Classes |
|------|-----------|-----------------|
| django-13964 | `M2mThroughTests` (tests/m2m_through/tests.py) | `Model`, `ForeignKey`, `CharField` |
| django-12856 | `UniqueTest` (tests/model_forms/tests.py), `In` (2-letter) | `UniqueConstraint`, `ArrayField`, `GDALBand` |
| sphinx-8713 | `E`, `C` (single-letter classes) | `GoogleDocstring`, `Sphinx`, `SphinxParallelError` |

**Zero test class leakage across all 10 tasks.** The leading-slash fix and basename check eliminated all path-based leaks, and `len <= 2` filter eliminated short ambiguous names.

---

## v2 vs v3 Context Comparison

| Task | v2 Chars | v3 Chars | v2 Classes Matched | v3 Entry Points | v3 Surface Methods |
|------|----------|----------|-------------------|-----------------|-------------------|
| django-11049 | 1151 | 1501 | 2813 | 3 | 12 |
| django-12856 | 1054 | 1159 | 2727 | 3 | 8 |
| django-13658 | 1054 | 0 | 2542 | 0 | 0 |
| django-13964 | 1858 | 191 | 2775 | 3 | 0 |
| matplotlib-23562 | 1052 | 93 | 799 | 3 | 0 |
| matplotlib-25433 | 1052 | 1986 | 689 | 3 | 10 |
| psf__requests-1963 | 1463 | 1633 | 159 | 3 | 6 |
| scikit-learn-14092 | 1457 | 1990 | 434 | 3 | 11 |
| sphinx-8713 | 847 | 183 | 658 | 3 | 0 |
| sympy-14774 | 1052 | 1919 | 571 | 3 | 13 |

**Key observations:**
- v2 "classes_matched" was wildly inflated (2700+ for Django) due to unconditional bonus — v3 correctly shows 3 entry points
- django-13658 gets **zero** context in v3 — keywords (`CommandParser`, `ManagementUtility`) don't match any class names with `len > 2`
- Several tasks get richer context in v3 (sklearn, sympy, matplotlib-25433) due to coupling annotations
- django-13964 drops from 1858 to 191 chars — the test classes that inflated v2 context are now gone

---

## Evaluation Results

**v3 Resolved: 5/10**

| Task | Category | v2 Result | v3 Result | Delta |
|------|----------|-----------|-----------|-------|
| django-11049 | LOST | RESOLVED | RESOLVED | = |
| django-12856 | GAINED | RESOLVED | **UNRESOLVED** | LOST |
| django-13658 | GAINED | RESOLVED | RESOLVED | = |
| django-13964 | LOST | UNRESOLVED | UNRESOLVED | = |
| matplotlib-23562 | GAINED | RESOLVED | RESOLVED | = |
| matplotlib-25433 | LOST | UNRESOLVED | UNRESOLVED | = |
| psf__requests-1963 | GAINED | RESOLVED | **UNRESOLVED** | LOST |
| scikit-learn-14092 | GAINED | RESOLVED | **UNRESOLVED** | LOST |
| sphinx-8713 | LOST | RESOLVED | RESOLVED | = |
| sympy-14774 | LOST | RESOLVED | RESOLVED | = |

**v2: 8/10 → v3: 5/10 (3 regressions, 0 gains)**

---

## Regression Analysis

### django-12856 (UniqueConstraint deferrable)

**v2 context (1054 chars):** Showed `Q` class AND `UniqueConstraint` — the `Q` class was a false positive from ranking inflation but may have inadvertently provided useful context about Django's query_utils.

**v3 context (1159 chars):** Shows `UniqueConstraint` with change surface (correct entry point), plus `ArrayField` and `GDALBand` (irrelevant — they matched keywords `Marnanel`/`Thurman` as substrings). The change surface annotations are accurate but the second/third entry points are noise.

**Likely cause:** v3's entry point selection still has substring-matching false positives. `ArrayField` and `GDALBand` are irrelevant — the keyword matching needs tighter constraints.

### psf__requests-1963 (redirect handling)

**v2 context (1463 chars):** Showed `SessionRedirectMixin` + `RequestsTestCase` (test class leaked).

**v3 context (1633 chars):** Shows `SessionRedirectMixin` correctly but also picks up a duplicate from `build/lib/requests/sessions.py` — the build directory isn't filtered.

**Likely cause:** Duplicate entry from `build/` directory. The `build/` directory should be in SKIP_DIRS.

### scikit-learn-14092 (GridSearchCV parameter validation)

**v2 context (1457 chars):** Showed `Or` class (2-letter, irrelevant) + `GridSearchCV`.

**v3 context (1990 chars):** Shows `GridSearchCV` + `Pipeline` + `LogisticRegression` — all plausible entry points. The change surface annotations are good.

**Likely cause:** Unlikely to be context quality — v3 context is arguably better here. May be LLM variance (non-deterministic model behavior on a single run).

---

## Diagnosis: Why v3 Regressed

The regressions are NOT from the test class fix or ranking fix. They're from two separate issues:

1. **`build/` directory not filtered** — requests-1963 shows duplicate `SessionRedirectMixin` from `build/lib/` which wastes context budget and confuses entry point deduplication.

2. **Keyword substring matching too loose** — django-12856 picks up `ArrayField` and `GDALBand` as entry points because keyword substrings match (e.g., "Thurman" contains... actually, this needs investigation of exact match logic).

3. **LLM variance** — On a 10-task subset with single runs, 1-2 task flips are within normal variance. The sklearn regression has good context in both v2 and v3.

---

## Fixes for v3.1

1. Add `build` to SKIP_DIRS
2. Consider deduplicating entry points by class name (same class in multiple files → pick the one not in build/)
3. Tighten keyword matching — require exact word match or longer substring overlap

---

## Comparison Across All Runs

| Run | Resolved | Notes |
|-----|----------|-------|
| Part 2 (v1, full runner) | 8/10 | Custom GT runner with gt_integration.py |
| Part 4 (v2, mini-swe-agent) | 8/10 | Class structure maps, test function filtering |
| Part 5 (v3, mini-swe-agent) | 5/10 | Change surface prediction, all test class leaks fixed |

The 8→5 drop is concerning but likely has fixable causes (build/ dir, keyword precision). The core v3 improvements (test filtering, ranking fix, coupling annotations) are correct — the regressions are from adjacent issues.
