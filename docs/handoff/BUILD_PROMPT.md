# Build Prompt: Fix GT Noise & Deficiencies

Copy everything below the line and paste into a new Claude Code session at `D:\Groundtruth`:

---

## Context

Branch `oh-gt-combined` has a working OH + GT integration on SWE-bench-Live Lite.
30-task run completed: 6/30 resolved (20%), 29/30 patched (97%).

But a deep audit revealed GT is mostly noise, not signal:
- 65% of GT tokens are noise (2,200/3,410 per task)
- 75% of L3 post-edit evidence blocks are empty but still injected (cry-wolf effect)
- L5 advisory never reaches the agent, corrupts patches
- L1 brief double-wrapped in XML tags
- L2 telemetry injected into agent-visible content
- Agent learned to ignore all GT output by iteration 20

The 6/30 is essentially the OH baseline. GT's actual contribution is near zero.

## Read These Files

1. `docs/handoff/DEFICIENT.md` — **THE BUILD LIST.** Every bug, every fix, every file location.
2. `docs/handoff/DEFICIENT_DEEP.md` — Deep analysis with token math and code references.
3. `scripts/swebench/oh_gt_full_wrapper.py` — THE FILE TO FIX. All noise issues are here.
4. `src/groundtruth/pretask/v7_brief.py` — L1 double-wrapping fix.
5. `src/groundtruth/hooks/post_edit.py` — L3 abstention threshold.
6. `docs/handoff/L4_ARCHITECTURE.md` — L4 prefetch design decisions.

## Your Task: Fix 8 Bugs (40 min)

### FIX 1 (CRITICAL): Stop injecting empty L3/L3b evidence
**File:** `oh_gt_full_wrapper.py:1133-1153`
**What:** Before building the `<gt-evidence>` block, check if hook output has real tags.
If no real evidence, don't inject anything into the observation.
**Check:** `any(tag in hook_out for tag in ("[GT_CHANGE]", "[GT_CONTRACT]", ...))`
**Same for L3b** at line 1029-1038.

### FIX 2: Remove reindex output from agent evidence
**File:** `oh_gt_full_wrapper.py:1135-1137`
**What:** Remove the `<gt-reindex>` sub-block from agent-visible evidence.
Keep running the reindex command but don't show output to agent.

### FIX 3: Remove gt_validate spam
**File:** `oh_gt_full_wrapper.py:1150`
**What:** Remove `f"Verify: gt_validate {rel_p}\n"` from every evidence block.

### FIX 4: Fix double gt-task-brief wrapping
**File:** `src/groundtruth/pretask/v7_brief.py` (find `<gt-task-brief>` in render)
**What:** Remove inner `<gt-task-brief>` tags. Let only `patched_get_instruction` wrap.

### FIX 5: Remove L2 telemetry tag from brief
**File:** `oh_gt_full_wrapper.py:1558-1559`
**What:** Don't append `l2_tag` to the agent-visible brief. Keep in telemetry only.

### FIX 6: Remove L5 advisory (or redesign)
**File:** `oh_gt_full_wrapper.py:1155-1206`
**What:** Remove all 4 advisory injection points. L5 cannot work in OH's event model.
Optional: add iteration-budget checkpoint at 80% (needs iteration counter).

### FIX 7: Remove/shrink tool footer
**File:** `oh_gt_full_wrapper.py:506-524`
**What:** Remove entirely or reduce to 1 line.

### FIX 8: Replace fallback brief with empty
**File:** `oh_gt_full_wrapper.py:1563-1569`
**What:** When brief generation fails, inject nothing (empty string), not "GT graph built..."

## Verification

After all fixes, run 1-task smoke:
```bash
# On gt-t0:
OH_DIR=/home/ubuntu/OpenHands-0.54.0 TASK_COUNT=1 NUM_WORKERS=1 \
  bash /home/ubuntu/Groundtruth/scripts/swebench/oh_gt_full_smoke10.sh --run
```

Then check output.jsonl:
- `grep -c 'no_evidence' output.jsonl` → should be 0
- `grep -c 'gt-reindex' output.jsonl` → should be 0 in evidence blocks
- `grep -c 'gt_validate' output.jsonl` → should be ≤ 1
- `grep -c 'gt-task-brief' output.jsonl` → should be exactly 2
- `grep -c 'gt-advisory' output.jsonl` → should be 0

Then run the 30-task Phase 4 set (IDs in DEFICIENT.md) and compare vs 6/30.

## What NOT to Do

- Do NOT change OH's CodeActAgent code
- Do NOT remove L6 reindex (it works, just hide its output)
- Do NOT remove L3/L3b hooks entirely — just suppress empty blocks
- Do NOT add new features — this is noise removal only
- Do NOT change the L4 prefetch logic (it's working correctly)
- Do NOT re-run without verifying the 1-task smoke first

## Infrastructure

**gt-t0:** OH 0.54.0 at `/home/ubuntu/OpenHands-0.54.0/`, GT at `/home/ubuntu/Groundtruth/` (branch oh-gt-combined)
**gt-v1:** Same setup, OH 0.54.0 freshly installed
**LiteLLM proxy:** port 4000 on both VMs, key `sk-gt-local`
**SSH:** `gcloud compute ssh ubuntu@gt-t0 --zone=us-central1-a --project=project-3d0018fc-54e4-4a4d-97c`
