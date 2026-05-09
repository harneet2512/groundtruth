# Build Prompt: Phase 6 — Verify Noisy GT Numbers + Fix Interaction Logging + Determine GT's Value

Copy everything below the line and paste into a new Claude Code session at `D:\Groundtruth`:

---

## Problem

We've run the same 30 SWE-bench-Live tasks 4 times with different GT configurations. The results show GT provides ZERO positive flips over baseline:

| Run | Resolved | Source |
|-----|:---:|---|
| Baseline (no GT) | 4/30 | Verified this session, eval on VMs |
| Noisy GT (original) | 6/30 | **UNVERIFIED — cited from memory of prior session** |
| Clean GT (eliminate) | 3/29 | Verified this session |
| Compression GT | 4/30 | Verified this session |

The "6/30 noisy GT" number was NEVER independently verified in this session. It came from the previous session's DEFICIENT.md. The output.jsonl files are pulled locally but the evals were run on VMs that are now stopped.

**Three things must happen:**
1. VERIFY the noisy GT "6/30" by re-evaluating from local output.jsonl files
2. FIX the gt_interactions logging (currently 0 for all tasks — flush never fires)
3. DETERMINE: does GT provide ANY value at all, or is it net-zero?

## What's Local (D:\Groundtruth\results\extracted\)

55 output.jsonl files from all runs, pulled from both VMs. Key directories:

**Noisy GT runs (the ones claiming 6/30 — MUST VERIFY):**
- `oh_gt_p4_exact_t0/` — noisy GT, gt-t0 tasks (20)
- `oh_gt_p4_exact_t0_b/` — retry batch
- `oh_gt_p4_exact_t0_c/` — retry batch  
- `oh_gt_p4_exact_v1/` — noisy GT, gt-v1 tasks (10)

**Baseline (verified 4/30):**
- `baseline_t0/` + `baseline_t0_final/` — merged = 20 tasks
- `baseline_v1/` — 10 tasks

**Compression GT (verified 4/30):**
- `compress_t0/` — 20 tasks
- `compress_v1/` — 10 tasks

**Clean GT / Phase 5 (verified 3/29):**
- `phase4_nf_t0/` + `phase4_nf_t0_retry/` — noise-fixed
- `phase5_t0/` + `phase5_t0_retry/` — enforce
- `phase4_nf_v1/`, `phase5_v1/`

## Task 1: Verify Noisy GT "6/30"

The 30 Phase 4 task IDs:
**gt-t0 (20):** aiogram__aiogram-1594, aws-cloudformation__cfn-lint-3789, 3798, 3821, 3854, 3856, 3862, 3866, 3875, 3890, 4002, 4023, 4032, beancount__beancount-931, beetbox__beets-5495, beeware__briefcase-2075, beeware__briefcase-2085, bridgecrewio__checkov-6893, 6895, 7002
**gt-v1 (10):** arviz-devs__arviz-2413, aws-cloudformation__cfn-lint-3779, 3805, 4016, delgan__loguru-1306, kozea__weasyprint-2303, pydata__xarray-9760, 9971, pylint-dev__pylint-10044, pypa__twine-1225

Steps:
1. Find which output.jsonl files contain the noisy GT results for these exact 30 tasks. The run names with "p4_exact" or "phase4_match" are likely candidates.
2. Merge the gt-t0 batches (exact_t0 + exact_t0_b + exact_t0_c) to get all 20 tasks.
3. You CANNOT run SWE-bench eval locally on Windows. You need to start the VMs (`gcloud compute instances start gt-t0 gt-v1 --zone=us-central1-a --project=project-3d0018fc-54e4-4a4d-97c`), push the merged predictions, and run eval there.
4. Compare per-task: which tasks resolved in noisy GT but NOT in baseline? Those are the actual GT flips (if any).

## Task 2: Fix gt_interactions Logging

The logging code exists in `scripts/swebench/oh_gt_full_wrapper.py` (commit `0c3d0d6`) but the flush never fires. The issue:

- `_flush_interaction_log` is called in two places:
  1. `event.kind == "finish"` — doesn't fire on max_iter timeout
  2. `action_count > max_iter` — fires during complete_runtime, BUT the `_flush_interaction_log` call is AFTER `_strip_scaffold_files`, and if strip crashes or the instance_ref is stale, the flush silently fails

Fix: The interaction log should be written to a FILE on the container (`/tmp/gt_interactions.jsonl`) instead of relying on instance_ref dict injection. Write to file on every interaction (append mode), not just on flush. Then pull the file during artifact collection.

Or simpler: write `config.interaction_log` to `instance_ref` BEFORE the finish event, not during it. Add the flush to `patched_run_action` on EVERY action (overwrite, not append — just keep writing the latest state).

## Task 3: Per-Task GT→Agent Analysis

Once logging works, run 5-10 tasks with interaction logging and analyze:
- For each GT injection (L3/L3b/L5): what did GT send?
- What was the agent's NEXT action after seeing the GT injection?
- Did the agent follow GT's guidance or ignore it?
- For resolved tasks: did GT evidence contribute to the fix?
- For unresolved tasks: did GT evidence mislead or was it ignored?

This is the data needed to determine if GT provides any value at the agent behavior level.

## Infrastructure

**VMs are STOPPED to save credit ($95 remaining of $300).**
- Start: `gcloud compute instances start gt-t0 gt-v1 --zone=us-central1-a --project=project-3d0018fc-54e4-4a4d-97c`
- Stop after work: `gcloud compute instances stop gt-t0 gt-v1 --zone=us-central1-a --project=project-3d0018fc-54e4-4a4d-97c`
- **STOP VMs when not actively running tasks. $36/day when both running.**
- Both VMs at commit `0c3d0d6` on branch `oh-gt-combined`
- Config.toml on gt-t0 may need `sudo rm` before baseline runs (permission issue)
- LiteLLM proxy needs restart after VM start: check `curl localhost:4000/health`

## Key Finding This Session

GT is net-zero on resolve rate. Baseline (no GT) gets 4/30. Compression GT gets 4/30. Same 4 tasks. The noisy GT "6/30" may be stochastic variance, not GT contribution — but we MUST verify it before concluding.

The compression fix (JetBrains NeurIPS 2025 "Complexity Trap" — compress don't eliminate) recovered the clean GT regression (3→4) back to baseline. Structural observation presence matters. But it doesn't produce positive flips.

## What NOT to Do

- Do NOT run more 30-task experiments without verifying the noisy GT numbers first
- Do NOT leave VMs running when not actively needed
- Do NOT call patch-apply failures "errors" — the agent produced a wrong patch, that's unresolved not an error
- Do NOT iterate on these 30 tasks for tuning — use a different 30 for dev if needed
- Do NOT assume the "6/30 noisy GT" is real until independently verified
