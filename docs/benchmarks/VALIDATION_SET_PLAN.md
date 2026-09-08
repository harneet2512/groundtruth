# Validation Set Plan

## Purpose

Prevent overfitting to the 5 repeated smoke tasks (beancount-931, beets-5495, xarray-9760, cfn-lint-3821, loguru-1306).

---

## TIER 1 — Current 5-Task Smoke

**Role:** Fast regression detection only.
**Tasks:** beancount-931, beets-5495, xarray-9760, cfn-lint-3821, loguru-1306
**NOT used for:** threshold tuning, flip claims, architecture decisions

---

## TIER 2 — Diverse 10-15 Task Dev Slice

**Purpose:** Generalization check across repos, languages, frameworks.

**Sampling criteria:**
- Different repos from Tier 1 (no beancount, beets, xarray, cfn-lint, loguru)
- Mix of Python + non-Python when SWE-bench-Live Lite supports
- Mix of repo sizes (small <100 files, medium 100-1000, large >1000)
- Include tasks where baseline historically resolves (to detect negative flips)
- Include tasks where baseline historically fails (to detect positive flips)
- No task-specific hardcoding

**Candidate pool (from SWE-bench-Live Lite 300-task dataset):**

Select 10-15 from these repos (must not overlap with Tier 1):
- checkov (infrastructure/config repo — tests policy rules)
- briefcase (build tool — different domain)
- pylint (linter — framework-routing style bugs)
- aiogram (async framework — event-driven)
- weasyprint (PDF/HTML — cross-domain bugs)
- twine (packaging tool — small focused repo)
- marshmallow (serialization library — data contracts)
- pydantic (data validation — type system heavy)
- django (large framework — routing + ORM)
- flask (web framework — request handling)

**Specific selection TBD after checking which tasks have:**
1. Non-trivial graph.db edges (>10 edges for gold file)
2. Test files that reference gold functions (for assertion linking validation)
3. Baseline historical data available

---

## TIER 3 — Cross-Benchmark / Non-Overlap Validation

**Purpose:** Prove repo/tool/model agnostic behavior.

**Sources:**
1. SWE-bench Verified subset (not in Live Lite) — 3 tasks
2. Fresh arbitrary GitHub repos with known test failures — 2 tasks

**Selection criteria:**
- Must have Python OR Go/JS/TS (to test language agnosticism)
- Must have test files (to validate assertion linking)
- Must have >50 source files (to test scale behavior)
- Must NOT be in any prior smoke/dev/gate run

**Candidate repos for fresh validation:**
- click (Python CLI library — existing graph.db available)
- hono (TypeScript web framework — existing graph.db available)
- axum (Rust web framework — existing graph.db available)

---

## Excluded Tasks

| Task | Why excluded |
|------|-------------|
| beancount-931 | Tier 1 — repeated 10+ times |
| beets-5495 | Tier 1 — repeated 10+ times |
| xarray-9760 | Tier 1 — repeated 10+ times |
| cfn-lint-3821 | Tier 1 — repeated 10+ times, 0 graph edges |
| loguru-1306 | Tier 1 — repeated 10+ times |

---

## Metrics to Compare Per Tier

| Metric | How measured | What it proves |
|--------|-------------|---------------|
| resolution (GT-on vs GT-off) | Eval harness | Flip detection |
| action_count delta | [GT_META] Task metrics | Efficiency |
| first_nonzero_diff_iter delta | [GT_META] Task metrics | Orientation speed |
| new_files_created delta | [GT_META] Task metrics | Scaffold reduction |
| l3_assertion_shown | post_edit.py evidence output | Assertion linking works |
| GT visible chars | Wrapper logging | Context budget |
