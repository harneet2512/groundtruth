# Next Session Execution Prompt

## Git State

- **Branch:** `gt-trial`
- **Local HEAD:** post-`e7fd256e` (includes D1-D7 fixes + D5 producer fix)
- **Hbali remote HEAD:** `7a283c60` (fix: mount task image dep stores) — **significantly behind local**
- **Status:** Local is many commits AHEAD of hbali (linear, not diverged)
- **Push blocked:** `harneet2512` denied on `hbali-stack` remote. Need `gh auth login` as `hbali-stack` or add `harneet2512` as collaborator.
- **Latest substrate digest:** `ghcr.io/hbali-stack/gt-substrate@sha256:2595578534149e4a0ef468dbcd7cc2e865a6e2cd7c202432a6bf625389943a34` (built from `5e713278`, does NOT contain any D1-D7 fixes — needs rebuild from new HEAD)

## First action: fix auth and push

The immediate blocker is authentication. Either:
1. `gh auth login` as `hbali-stack` (the repo owner), OR
2. Add `harneet2512` as a collaborator on the `hbali-stack` remote

Once auth is resolved:
```bash
git push --no-verify hbali gt-trial
```

After push: rebuild substrate from new HEAD (includes ALL CP001-010 + D1-D7 fixes + D5 producer fix).

---

## Read these files FIRST (in order, cite exact lines before acting):

1. `GT_BUGFREE_STATUS_AND_BUILD_PLAN.md` — PART 0 (core concept) + PART 2 (the 10 pieces) + PART 3 (priority order)
2. `.claude/reports/runs/validation_27367976952/GT_BUGFREE_HANDOFF_FULL.md` — master handoff, product model, trajectory findings
3. `.claude/reports/runs/validation_27367976952/CONTEXT_GAP_AUDIT_27367976952.md` — what the agent needed vs what GT sent
4. `.claude/reports/runs/validation_27367976952/HANDOFF_AFTER_CHECKPOINT_010.md` — verification matrix, open work
5. `gt_gt.md` §15 (oracle architecture) + §12 (per-layer roles)
6. `.claude/CLAUDE.md` — development constitution, LIPI, 3 mandatory properties

## What you are building

GT is a predictive action-aware context delivery system. It has the full code map (graph.db), real-time visibility into every agent action (`_augment_output`), and oracle metrics that describe the agent's state. GT's job: when the agent performs an action, deliver the context THAT action needs — so the agent never searches for it.

The agent already passes 83-94% of tests on most tasks (15/16 adaptix, 43/47 fd, 83/94 katex, 20/24 awilix). It fails on 1-4 specific requirements it didn't verify. The missing pieces are:

1. **Obligation lifecycle tracker** — know which requirements are unedited/edited/tested/satisfied
2. **Pre-submit enforcement** — at >90% budget, always fire the obligation checklist
3. **Phase-aware context selection** — deliver VIEW context on view, EDIT context on edit, VERIFY context at review-transition
4. **Graph-to-action templates** — "X calls Y" → "changing Y risks breaking X"
5. **Context budgeting** — trim payloads, dedupe cross-turn, prefer imperative over explanation

## CRITICAL BUILD RULE: GT surface ONLY

**Build and verify on `artifact_deepswe/` and `src/groundtruth/` ONLY.**

- Do NOT touch `.github/workflows/` (no GHA changes)
- Do NOT rebuild the substrate
- Do NOT trigger paid runs
- Do NOT modify `docker/Dockerfile.gt-substrate`
- Test LOCALLY against the frozen trajectories at `.claude/reports/runs/validation_27367976952/`

The frozen trajectories have full agent behavior data. Use them as test fixtures:
- `deepswe-full-<task>/jobs/*/agent/mini-swe-agent.trajectory.json` — every agent action + observation
- `deepswe-full-<task>/gt_oracle_events_*.jsonl` — what the oracle decided each turn
- `deepswe-full-<task>/gt_deep_metrics_*.json` — per-layer delivery metrics
- `deepswe-full-<task>/gt_artifacts/gt_issue_anchors.json` — extracted obligations

When the code is proven locally → ONE substrate rebuild → ONE 10-task run → audit results.

## Defect Status (D1-D10)

D1-D7 are all **CLOSED**. D8-D10 remain OPEN (cosmetic, no runtime harm).

| ID | Defect | Status | Commit |
|---|---|---|---|
| D1 | Budget dedup marks "delivered" at production, not after gate | **CLOSED** | `77dc857c` |
| D2 | No oracle state reset between retry attempts | **CLOSED** | `77dc857c` |
| D3 | Pre-submit boost (6.0) loses to horizon (7.8+) | **CLOSED** | `77dc857c` |
| D4 | ObligationTracker = zero signal over stateless | **CLOSED** | `8d505603` |
| D5 | Templates semantically inverted, 2/4 dead | **CLOSED** | `8d505603` + D5 producer fix |
| D6 | Retry classifier unreachable — env regex eats patch failures | **CLOSED** | `e7fd256e` |
| D7 | Ledger judges trigger, not response; mutes obligation | **CLOSED** | `8d505603` + `7da50622` |
| D8 | Phase thresholds invented | **OPEN** | Cosmetic — derive from corpus when prioritized |
| D9 | SEARCH phase missing from policy module | **OPEN** | Cosmetic — no runtime impact |
| D10 | gt_caused labeled causal, is grep heuristic | **OPEN** | Cosmetic — rename when scorecard touched |

The CP011-015 defects that blocked the benchmark are fixed. The next work is the benchmark itself.

---

## Execution Plan: Benchmark Validation

CP011-015 defects are fixed. The next work is the benchmark itself.

### Step 1: Fix Auth and Push

Resolve hbali-stack authentication, then push:
```bash
git push --no-verify hbali gt-trial
```

### Step 2: Rebuild Substrate

Rebuild from new HEAD which includes all D1-D7 fixes, CP001-010, and the delivery engine.

### Step 3: 10-Task Validation Run

Run 10 tasks from the DeepSWE benchmark to verify the fixes work end-to-end.

### Step 4: gt_trial Section 4 Audit

Per-component audit on the 10-task trajectories: chronological read of output.jsonl, per-layer tables (L1, L3b, consensus, L3, L4, L5, L5b, L6), DELIVERED/CORRECT/CONSUMED columns.

### Step 5: Full 113-Task Benchmark (if Step 4 is clean)

Only proceed if the 10-task audit shows no regressions and D1-D7 fixes are functioning correctly.

---

## Verification Commands

```powershell
# All CP001-015 + defect-fix tests
python -m pytest tests/test_verified_adapter.py tests/fail_closed/ tests/test_task_truth.py tests/test_consumption_ledger.py tests/test_path_policy.py tests/test_patch_hygiene.py tests/test_action_templates.py tests/test_context_budget.py tests/test_obligation_tracker.py -q

# Full suite
python -m pytest tests/ -x -q --tb=short
```

All must pass before ANY substrate rebuild or GHA run.

## Rules

1. **GT surface only** — `artifact_deepswe/` and `src/groundtruth/`. No workflow/Dockerfile/substrate.
2. **One boundary per commit** — never mix checkpoints.
3. **Frozen trajectories as test fixtures** — `.claude/reports/runs/validation_27367976952/`
4. **LIPI every fix against gt_gt.md** before committing — all 4 avenues, trace WHAT/WHERE/WHO/WHEN.
5. **No paid runs until all 5 CPs pass locally.**
6. **State-aware dedup already built** (`5e713278`) — the obligation tracker and phase detection plug into the existing behavioral hash.
7. **The oracle metrics are the decision signal** — `_oracle_edited_rels`, `_oracle_tested_tokens`, `_action_count`, `_oracle_nonedit_streak` determine WHAT to deliver and WHEN.
8. **Correct-or-quiet** — if confidence is low or the phase doesn't call for it, stay silent. Wrong context is worse than no context.
9. **No LLM in the pipeline** — templates, not generation. Deterministic, $0.
10. **Definition of done** — metrics changed on a real run. Tests passing means "in progress" not "done."

## After all CPs pass locally

1. Rebuild substrate (ONE build)
2. Run 10-task validation (ONE run) with dep mount fix already in workflow (`7a283c60`)
3. gt_trial §4 audit on the trajectories — per-layer TABLES, chronological read
4. Compare: did obligation coverage improve? Did the agent act on GT's imperative context? Did any task flip?

## Key insight from the trajectories

The agent is CLOSE. It passes most tests. It fails on specific unverified requirements. The obligation tracker + pre-submit gate + phase-aware delivery + action templates + budget trim = the agent gets told "you haven't verified X" at the right moment in the right form, and it verifies X before submitting. That's the path to flips.
