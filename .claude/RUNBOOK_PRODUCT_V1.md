# Product-v1 Staged Execution Runbook

**Branch:** `jedi__branch`
**HEAD at creation:** `b953231d`
**Patches:** A-F (confidence filter, neighbor cap, G7 silence, dedup, anchor ranking, visible-test bonus)
**Workflow:** `.github/workflows/swebench_30task.yml`
**Model:** DeepSeek V4 Flash
**Runner:** ubuntu-latest (GHA)

---

## Stage 0 -- Preflight

Run before launching any GHA workflow. Every check must pass.

### Checks

| # | Check | Command | Pass condition |
|---|-------|---------|----------------|
| 0a | Branch is jedi__branch | `git branch --show-current` | Output = `jedi__branch` |
| 0b | Local HEAD matches expected | `git rev-parse HEAD` | Starts with `b953231d` (or document if newer commits exist) |
| 0c | Remote HEAD matches local | `git fetch origin jedi__branch && git rev-parse origin/jedi__branch` | Same SHA as local HEAD |
| 0d | Working tree clean (tracked) | `git diff --stat HEAD` | Empty output |
| 0e | No forbidden strings in product code | See forbidden string scan below | Zero hits |
| 0f | Verifier scripts exist | `ls scripts/verify/*.py` | All 5 scripts present |
| 0g | GHA workflow has exactly 5 tasks | Parse `.github/workflows/swebench_30task.yml` | Task array = 5 entries |

### Forbidden string scan

Scan committed product code (not test fixtures, not docs, not this runbook) for strings that indicate benchmark contamination:

```
FAIL_TO_PASS
PASS_TO_PASS
test_patch
gold_patch
oracle
```

**Scope:** `src/groundtruth/`, `scripts/swebench/oh_gt_full_wrapper.py`, `scripts/swebench/convert_to_submission.py`

**Exclusions:** This file, CLAUDE.md, test fixtures, `.github/workflows/`, `benchmarks/`, `scripts/verify/`

**Command:**
```bash
git grep -n "FAIL_TO_PASS\|PASS_TO_PASS\|test_patch\|gold_patch\|oracle" -- \
  src/groundtruth/ scripts/swebench/oh_gt_full_wrapper.py scripts/swebench/convert_to_submission.py \
  ':!*test*' ':!*fixture*'
```

**Pass:** Zero matches.

### GHA task list verification

```bash
grep -A20 "TASKS=(" .github/workflows/swebench_30task.yml | grep '"' | wc -l
```

**Pass:** Exactly 5 tasks. Task list must be:
1. `amoffat__sh-744`
2. `beeware__briefcase-2085`
3. `conan-io__conan-17102`
4. `pallets__flask-5637`
5. `pylint-dev__pylint-10044`

### Verifier existence check

All of these must exist under `scripts/verify/`:
- `stage_runtime_verifier.py`
- `gt_pollution_check.py`
- `patch_integrity_check.py`
- `product_v1_signal_check.py`
- `task_result_summarizer.py`

### Preflight result

- **All checks green:** Proceed to Stage 1.
- **Any check red:** STOP. Report which check failed and why. Do NOT proceed.

---

## Stage 1 -- 5-Task Runtime Proof

**Purpose:** Prove product behavior (patches A-F exercised, no regressions, no pollution). This is NOT about score.

**Tasks (5):**
- `amoffat__sh-744` (control -- previously resolved)
- `beeware__briefcase-2085` (control -- previously resolved)
- `conan-io__conan-17102`
- `pallets__flask-5637`
- `pylint-dev__pylint-10044`

### Launch

```bash
gh workflow run "SWE-bench-Live 30-task (VM baseline)" \
  --ref jedi__branch \
  -f gt_commit=jedi__branch \
  -f max_iterations=100 \
  -f baseline=false \
  -f temperature=0.7 \
  -f top_p=0.8
```

Record the run ID immediately:
```bash
gh run list --workflow swebench_30task.yml --limit 1 --json databaseId,status,conclusion,headBranch
```

### Monitor

Poll until completion:
```bash
gh run list --workflow swebench_30task.yml --limit 1 --json status,conclusion
```

Expected completion: 30-90 minutes depending on task complexity.

### After Completion

#### 1. Download artifacts

```bash
mkdir -p .claude/reports/product_v1/stage1_artifacts
gh run download <RUN_ID> --dir .claude/reports/product_v1/stage1_artifacts
```

#### 2. Run verifiers

```bash
python scripts/verify/stage_runtime_verifier.py \
  --output-dir .claude/reports/product_v1/stage1_artifacts \
  --stage stage1 \
  --branch jedi__branch \
  --head-sha b953231d \
  --run-id <RUN_ID> \
  > .claude/reports/product_v1/stage1_verifier_output.json
```

#### 3. Run individual checks for detailed inspection

```bash
# Pollution check
python scripts/verify/gt_pollution_check.py \
  --output-dir .claude/reports/product_v1/stage1_artifacts \
  > .claude/reports/product_v1/stage1_pollution.json

# Patch integrity
python scripts/verify/patch_integrity_check.py \
  --output-dir .claude/reports/product_v1/stage1_artifacts \
  > .claude/reports/product_v1/stage1_patches.json

# Product-v1 signal check
python scripts/verify/product_v1_signal_check.py \
  --output-dir .claude/reports/product_v1/stage1_artifacts \
  > .claude/reports/product_v1/stage1_signals.json

# Task result summary
python scripts/verify/task_result_summarizer.py \
  --output-dir .claude/reports/product_v1/stage1_artifacts \
  > .claude/reports/product_v1/stage1_summary.json
```

#### 4. Update ARTIFACT_LEDGER.md

Record the run ID, timestamps, and verifier results in `.claude/reports/product_v1/ARTIFACT_LEDGER.md`.

### Stage 1 Gate Criteria

| Gate | Condition | Action on failure |
|------|-----------|-------------------|
| G1 | Zero regressions on control tasks (sh-744, briefcase-2085) | STOP. Report regression. Investigate before any further action. |
| G2 | All invariant checks PASS or NOT_EXERCISED (no FAIL) | STOP. Report which invariant failed. |
| G3 | Pollution check = pass (no GT_META/GT_STATUS in agent observations) | STOP. Fix pollution source. |
| G4 | Patch integrity: all produced patches are well-formed | WARN if malformed, but do not block. |
| G5 | At least 1 Product-v1 signal exercised per task | WARN if all signals are not_exercised. Does not block. |
| G6 | No verifier errors/crashes | STOP. Fix the verifier. |
| G7 | Task count = 5 (no missing artifacts) | STOP. GHA run incomplete. |

**Pass:** G1-G3 and G6-G7 all green. G4-G5 green or WARN.
**Fail:** Any of G1-G3 or G6-G7 red. STOP. Do NOT proceed to Stage 2.

---

## Stage 2 -- 20-30 Task Product Smoke

**REQUIRES EXPLICIT USER APPROVAL BEFORE LAUNCH.**

Only enter Stage 2 if Stage 1 passes ALL mandatory gates (G1-G3, G6-G7).

### Purpose

Compare Product-v1 against jedi__branch baseline (pre product-v1, commit `e55b4029`) on a broader task set.

### Setup

1. Update the GHA workflow task list to include 20-30 tasks (user provides the list).
2. Run TWO workflows:
   - **Baseline arm:** `--ref e55b4029 -f gt_commit=e55b4029 -f baseline=false` (same GT config, old code)
   - **Product-v1 arm:** `--ref jedi__branch -f gt_commit=jedi__branch -f baseline=false`

### Metrics to compute

| Metric | Source | What it shows |
|--------|--------|---------------|
| Resolved count | eval_result.json | Primary outcome |
| Regressions | Compare baseline vs product-v1 per-task | Tasks baseline solved but product-v1 didn't |
| Flips | Compare baseline vs product-v1 per-task | Tasks product-v1 solved but baseline didn't |
| GT injections per task | gt_layer_events JSONL | Evidence delivery volume |
| Dedup count | Structured events with event_type=*dedup* | Patch D exercised |
| G7 silence count | GT_META g7_silence in stderr | Patch C exercised |
| Anchor hits | Structured events mentioning anchors | Patch E exercised |
| Actions per task | output.jsonl history | Economy (fewer = better) |
| Edits per task | output.jsonl history | Edit precision |
| GT visible count | [GT] markers in agent observations | Agent-visible evidence |

### Stage 2 gate criteria

- Zero net regressions (flips >= regressions).
- No new pollution.
- Product-v1 signals exercised on at least 50% of tasks.
- If regressions > 0: investigate each one before declaring pass/fail.

---

## Stop Conditions (Any Stage)

Any of these triggers an immediate STOP:

| Condition | Action |
|-----------|--------|
| REGRESSION on a control task | STOP. Report. Do not proceed. |
| FAIL on any invariant | STOP. Report which invariant and which task. |
| Forbidden string in run logs | STOP. Investigate contamination. |
| Verifier error/crash | STOP. Fix verifier before re-running. |
| Task count mismatch (missing artifacts) | STOP. GHA run incomplete or broken. |
| Code change needed | STOP. Report what needs changing. Await approval. |

---

## Autonomous Actions Allowed

These actions can be taken without asking for approval:

- Run preflight (Stage 0)
- Launch Stage 1 (after preflight passes)
- Download GHA artifacts
- Run all verifier scripts
- Produce reports and update ARTIFACT_LEDGER.md
- Read and analyze output.jsonl, structured events, hook logs
- Spawn log-review analysis (read-only)
- Poll GHA run status

---

## Must Stop for Approval

These actions REQUIRE explicit user approval:

| Action | Why |
|--------|-----|
| Launch Stage 2 | Cost + broader scope |
| Any code change | Product code is frozen for this run |
| Escalating task count beyond 5 | Budget control |
| Dismissing a verifier failure | No silent overrides |
| Updating RESPEC or DECISIONS | Policy changes need review |
| Re-running Stage 1 after a failure | Must explain what changed |
| Pushing any commits | Branch is at known-good state |

---

## Rollback Plan

If Product-v1 causes regressions or invariant failures:

```bash
# Full rollback to pre-product-v1
git reset --hard e55b4029

# Surgical rollback of just the product-v1 commit
git revert e0a50f72

# Rollback the doc commit too
git revert bce63616

# Rollback the test update
git revert b953231d
```

---

## File Locations

| What | Path |
|------|------|
| This runbook | `.claude/RUNBOOK_PRODUCT_V1.md` |
| Verifier scripts | `scripts/verify/` |
| Stage reports | `.claude/reports/product_v1/` |
| Artifact ledger | `.claude/reports/product_v1/ARTIFACT_LEDGER.md` |
| GHA workflow | `.github/workflows/swebench_30task.yml` |
| Product-v1 commit | `e0a50f72` |
| Pre-product-v1 baseline | `e55b4029` |
| Constitution | `.claude/CLAUDE.md` |
