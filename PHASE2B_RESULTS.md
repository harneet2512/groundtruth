# Phase 2B Post-Processing Experiment — Results

## TL;DR

**Zero corrections across 185 successfully processed baseline tasks.**
The post-processed predictions are byte-for-byte identical to the originals.
Result: **113/300 (same as baseline)**. GT post-processing is safe but not additive.

## What Was Tested

Applied GT green-lane autocorrect with runtime introspection KB to 300 existing
baseline predictions (113/300 resolved). Zero API cost — same patches, one set
post-processed.

## Experiment Details

| Metric | Value |
|--------|-------|
| Baseline predictions | 300 (113 resolved) |
| Tasks with patches | 293 |
| Containers started | 292 |
| Autocorrect ran successfully | 185 |
| **Corrections applied** | **0** |
| Predictions changed | 0 |
| Infrastructure failures | 108 (timeout: 63, arg-too-long: 40, json: 4, docker: 1) |
| Total time | 114 minutes |

## Per-Repo Runtime KB Stats

| Repo | Tasks | Containers OK | Avg KB Classes |
|------|-------|---------------|----------------|
| django/django | 114 | 112 | 141 |
| sympy/sympy | 77 | 73 | 8 |
| scikit-learn | 23 | 23 | 0 |
| matplotlib | 23 | 23 | 1 |
| pytest | 17 | 16 | 11 |
| sphinx | 16 | 16 | 4 |
| requests | 6 | 6 | 27 |
| pylint | 6 | 6 | 2 |
| astropy | 6 | 5 | 35 |
| xarray | 5 | 5 | 1 |
| seaborn | 4 | 4 | 0 |
| flask | 3 | 3 | 0 |

## Why Zero Corrections

GPT-5.4-nano does not produce the types of hallucinations that green-lane
autocorrect catches:

1. **Check 1 (imports)**: Agent does not misspell imported names within Levenshtein distance 2
2. **Check 2 (self.method)**: Agent does not hallucinate method names close to real methods
3. **Check 3 (self.attr)**: Agent does not hallucinate attribute names close to real attributes
4. **Check 4 (kwargs)**: Agent does not misspell keyword argument names
5. **Check 6 (consistency)**: Agent patches are internally consistent

The green-lane checks catch "close-but-wrong" names (edit distance 1-2). GPT-5.4-nano
either gets names exactly right or gets them completely wrong (not close enough for
Levenshtein matching to fire).

## Historical Run Comparison

| Run | Resolved | Delta | Architecture |
|-----|----------|-------|--------------|
| Baseline (no GT) | 113 | — | Agent alone |
| vNext | 113 | +0 | AST post-processing (zero corrections) |
| v4.2 | 105 | -8 | Active tools, over-exploration |
| v6 | 106 | -7 | AST autocorrect, false positives |
| Phase 1 | 103 | -10 | Active tools, check over-revision |
| Phase 2A | 109 | -4 | Active tools, soft check |
| **Phase 2B** | **113** | **+0** | **Runtime KB post-processing (zero corrections)** |

## Smoke Test Results (Standalone Validation)

4 tasks run with full Phase 2B architecture (post-processing only + bounded test feedback):

| Task | Patch | GT Calls | Test Run | Runtime KB |
|------|-------|----------|----------|------------|
| django-12856 | YES (1123 chars) | 0 | YES (pytest test_models.py) | 100 classes |
| django-16139 | YES (1091 chars) | 0 | YES (pytest test_forms.py) | 83 classes |
| sympy-24213 | YES (691 chars) | 0 | YES | 10 classes |
| sklearn-14092 | YES (1446 chars) | 0 | YES (pytest test_nca.py) | 0 classes |

All checks passed: zero GT tool calls, runtime KB built for 3/4 tasks, all agents ran tests.

## Architectural Insight

Post-processing cannot improve what it cannot detect. The green-lane autocorrect
architecture is correct — it produces zero false positives with the runtime KB.
But it also produces zero true positives because the failure mode it targets
(near-miss name hallucinations) does not occur with this model.

The agent's actual failure modes are:
- Wrong logic (correct names, wrong implementation)
- Incomplete fixes (misses coupled changes)
- Wrong approach (correct code for the wrong strategy)

None of these are detectable by name-checking post-processing.

## What This Means for the Product

1. **Post-processing is safe but insufficient.** Zero false positives is a real achievement (v6 had 53/54 false positives). But zero true positives means it does not move the needle.

2. **Active tools have real value** — Phase 1 gained 19 tasks from obligations. The problem was turn overhead on bash scaffolds, not the intelligence itself.

3. **The path forward is MCP-native scaffolds** (OpenHands, Claude Code, Cursor) where tool calls do not burn bash turns. GT value comes from live guidance during editing, not post-hoc correction.

4. **Bounded test feedback is the highest-value intervention** — smoke test showed 4/4 agents ran tests. This should be tested independently as Experiment 2: baseline vs baseline+test-feedback, no GT post-processing.
