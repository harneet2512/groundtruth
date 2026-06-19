# Build Prompt: Phase 5 — Make the Agent Listen to GT

Copy everything below the line and paste into a new Claude Code session at `D:\Groundtruth`:

---

## Problem

GroundTruth (GT) sends clean, verified signals to the AI coding agent during SWE-bench-Live tasks. The agent ignores them. GT's signal quality is proven (0 noise, 100% real evidence), but resolve rate is 4/30 — essentially the OH baseline without GT. The agent creates scaffolding files (reproduce_issue.py, test_fix.py, debug_*.py) instead of editing the source files GT identified. GT is net-zero on outcomes.

The problem is signal-to-action conversion: the agent receives correct localization but doesn't act on it.

## Context

**What's been done:**
- 13 noise fixes shipped and verified (empty evidence suppression, reindex hidden, gt_validate spam removed, double XML wrapping fixed, L2 telemetry removed, L5 advisory removed, tool footer emptied, fallback brief emptied, L4 graph-backed filter, L3 adaptive threshold, L5 percentage checkpoints with quality gate, L1 curated compact truncation, L5 complete_runtime contamination guard)
- 30-task Phase 4 run completed: 4/30 resolved (beancount-931, beets-5495, briefcase-2075, twine-1225)
- Previous noisy GT run on same 30 tasks: 6/30 resolved (lost weasyprint-2303 and xarray-9971 to scaffolding/truncation)
- 15/30 tasks the agent created new scaffolding files instead of editing source
- 2 tasks the agent created ONLY new files with ZERO source edits
- OH's `live_utils.complete_runtime` truncates patches at ~30K chars (infrastructure bug)

**Branch:** `oh-gt-combined` on `github.com/harneet2512/groundtruth.git`
**Latest commit:** `1e45ade`
**Key files:**
- `scripts/swebench/oh_gt_full_wrapper.py` — THE wrapper to modify (all layer logic lives here)
- `docs/handoff/PHASE5_FORWARD.md` — detailed analysis, per-layer breakdown, research pointers
- `docs/handoff/DEFICIENT.md` — original 8-bug audit from prior session
- `docs/handoff/DEFICIENT_DEEP.md` — 17-bug deep analysis

**Infrastructure:**
- gt-t0: `/home/ubuntu/OpenHands-0.54.0/` + `/home/ubuntu/Groundtruth/` (OH 0.54.0 + GT)
- gt-v1: same setup
- LiteLLM proxy port 4000 on both, key `sk-gt-local`
- SSH: `gcloud compute ssh ubuntu@gt-t0 --zone=us-central1-a --project=GCP_OLD_PROJECT_PLACEHOLDER`

## Your Task

You need to do TWO things: deep analysis first, then build.

### Part 1: Deep Analysis (do this FIRST, before writing any code)

**A. Trace GT→agent interaction for 6 tasks (3 resolved + 3 unresolved).**

Pick: beancount-931 (resolved), twine-1225 (resolved), beets-5495 (resolved), weasyprint-2303 (unresolved, pure scaffold), cfn-lint-3875 (unresolved, 15 new files), checkov-6893 (unresolved, 10 new files).

For each task, answer:
1. What did L1 brief say? (candidate files)
2. Did the agent open/read any candidate file in its first 5 actions?
3. At what iteration did the agent first create a new file? Which file?
4. How many L3 evidence blocks fired? Did ANY contain actionable information?
5. Did the brief's candidate files contain the actual gold edit file? (check against the SWE-bench-Live dataset)
6. If L5 had fired a redirect ("you haven't edited any candidate files"), at what iteration would it have fired? Would the agent have had enough budget left to pivot?

The output.jsonl files are at:
- gt-t0: `/home/ubuntu/results/phase4_nf_t0/SWE-bench-Live__SWE-bench-Live-lite/CodeActAgent/qwen3-coder-480b-a35b-instruct-maas_maxiter_100/output.jsonl`
- gt-v1: `/home/ubuntu/results/phase4_nf_v1/SWE-bench-Live__SWE-bench-Live-lite/CodeActAgent/qwen3-coder-480b-a35b-instruct-maas_maxiter_100/output.jsonl`

**B. Research frontier approaches to agent behavior steering.**

Read PHASE5_FORWARD.md section on research pointers. The key question: which patterns from SWE-Search, Agentless, CodeR, Moatless, AutoCodeRover can we adopt WITHIN OH's CodeActAgent (without modifying OH source code) via the wrapper?

Specific questions to answer:
- Can we implement SWE-Search's stuck detection deterministically using GT's graph data?
- Can we implement CodeR's "fixer can't create files" constraint via the wrapper?
- Can we implement Moatless's state transitions via observation modification?

**C. Check if scaffolding behavior is model-specific.**
Does Qwen3-Coder scaffold more than other models? Check published SWE-bench results for Qwen3-Coder vs Claude vs GPT-4 on similar frameworks.

### Part 2: Build (only after Part 1 analysis is complete)

Based on the analysis, implement the Inform → Reinforce → Enforce framework:

**Build 1: Fix complete_runtime truncation (2 lines)**
File: OH's `live_utils.complete_runtime` on both VMs.
Change `git diff --no-color --cached {base_commit}` to write to a file first, then FileReadAction.

**Build 2: Submit-time scaffold strip (inside agent loop, ~20 lines)**
File: `oh_gt_full_wrapper.py` — in the finish event handler, BEFORE `complete_runtime` runs.
Get base_commit files via `git ls-tree`, delete any files not in that list, then let `complete_runtime` proceed with only real edits.

**Build 3: L5 localization redirect (~30 lines)**
File: `oh_gt_full_wrapper.py` — store brief candidate files in `config.brief_candidates` during init. At L5 checkpoint, compare `config.edited_files` against candidates. Emit redirect if agent hasn't edited any candidate file.

**Build 4: L3 localization framing (optional, ~10 lines)**
File: `oh_gt_full_wrapper.py` — when L3 evidence fires on a briefed candidate file, prefix with "This edit is to a briefed candidate file." When it fires on a non-candidate file, prefix with "This file was NOT in the brief's candidates."

### Verification

After all builds, run 1-task smoke (max_iter=100) and check:
1. No GT contamination in patch (`grep 'gt-advisory\|gt-evidence' patch`)
2. No new files in final patch (`grep 'new file mode' patch` → 0)
3. L5 redirect fires if agent scaffolds
4. Patch is not truncated

Then run the same 30-task Phase 4 set and compare against 4/30.

**Phase 4 task IDs (20 gt-t0, 10 gt-v1) are in `/tmp/phase4_t0_ids.json` and `/tmp/phase4_v1_ids.json` on the VMs.**

## Legitimacy Constraints (MUST follow)

- Everything that modifies agent behavior must happen DURING the agent loop, not after
- The submit-time strip must happen BEFORE `complete_runtime`'s `git add -A`, not as post-processing on predictions.jsonl
- No task-specific logic (no conditionals on instance_id, repo name, issue text patterns)
- No post-hoc prediction editing
- SWE-agent strips scaffolding at agent-time — that's the precedent. We follow the same pattern
- The truncation fix is an OH infrastructure bug fix, not a benchmark optimization

## What NOT to Do

- Do NOT change OH's CodeActAgent source code
- Do NOT add LLM calls to GT's pipeline (must stay $0 AI, deterministic)
- Do NOT revert the noise fixes — they're proven correct
- Do NOT re-run without the 1-task smoke verification first
- Do NOT overfit to these 30 tasks — every change must be repo-agnostic and language-agnostic
- Do NOT skip the Part 1 analysis and jump to building — the analysis determines WHAT to build
