# Phase 5 Forward: Signal-to-Action Conversion

**Date:** 2026-05-09
**Branch:** `oh-gt-combined` (commit `8865a86`)
**Author:** Session handoff for next builder

---

## The Problem We're Solving

GT sends clean, correct signals to the agent. The agent ignores them.

GT's signal quality is proven: 0 empty evidence blocks, 0 noise markers, 100% real evidence in every injection (verified on 1-task smoke + 30-task run). The 8 mechanical noise fixes + deep architecture changes eliminated all GT-side noise.

But resolve rate went from 6/30 → 4/30. GT is net-zero on outcomes. The agent's behavior is dominated by the model + harness, not by GT's signal. The next phase is making the agent ACT on GT's localization.

---

## What's Been Solved (Signal Quality)

| Fix | What it did | Verified |
|-----|------------|----------|
| Empty L3/L3b suppression | No more cry-wolf empty evidence blocks | 0 `no_evidence` in smoke |
| Reindex hidden | `<gt-reindex>` removed from agent context | 0 in smoke |
| gt_validate spam removed | No more "Verify: gt_validate" on every edit | 0 in smoke |
| Double XML wrapping fixed | Single `<gt-task-brief>` tag pair | exactly 2 in smoke |
| L2 telemetry removed | Machine metadata out of agent context | confirmed |
| L5 advisory removed from agent obs | No more advisory in finish/submit events | confirmed |
| Tool footer emptied | No more useless tool descriptions | confirmed |
| Fallback brief emptied | No filler "GT graph built..." text | confirmed |
| L4 graph-backed filter | SweRank-style: tokens survive only if in graph.db | 3 symbols selected in smoke |
| L3 adaptive threshold | 0.55→0.40 via GT_MIN_CONFIDENCE env var | confirmed |
| L5 percentage checkpoints | 33%/66% of max_iter, quality-gated on unresolved>0 | 2 advisory in smoke |
| L1 curated compact truncation | Lost-in-the-Middle: keep files (start) + constraints (end) | confirmed |
| L5 complete_runtime guard | action_count <= max_iter prevents post-loop contamination | fix committed |

**Result:** GT signal is clean. Every injection contains real evidence. No contamination, no noise, no truncation from GT side.

---

## What's NOT Solved (Signal-to-Action)

### The Data

30-task Phase 4 run (exact same tasks as prior baseline):

| Metric | OH+GT noisy (original) | OH+GT clean (this run) |
|--------|:---:|:---:|
| Resolved | 6/30 (20%) | 4/30 (13%) |
| Patched | 29/30 (97%) | 30/30 (100%) |
| Patch apply errors | ~1 | 7 (after strip: still 7) |
| Tasks with scaffolding (new files) | unknown | 15/30 |

**Resolved tasks (clean run):** beancount-931, beets-5495, briefcase-2075, twine-1225
**Lost from original:** weasyprint-2303 (agent scaffolded, empty patch after strip), xarray-9971 (truncated)
**Regressions caused by:** L5 checkpoint contamination bug (fixed), stochastic model behavior, OH truncation

### Root Causes of Failure (per-task analysis)

**Category 1: Agent scaffolded instead of fixing (15/30 tasks)**
The agent creates `reproduce_issue.py`, `test_fix.py`, `debug_*.py`, `comprehensive_test.yaml` files instead of editing existing source code. In 2 tasks (weasyprint-2303, cfn-lint-3805) the agent created ONLY new files with ZERO edits to existing source — pure scaffolding, no fix attempted.

**Category 2: Patch too large / truncated (2/30 tasks)**
OH's `live_utils.complete_runtime` captures `git diff --cached` via stdout, which truncates at ~30K chars. The standard `complete_runtime` in `run_infer.py` writes to a file first (`> patch.diff`), avoiding truncation. This is a 2-line OH infrastructure fix.

**Category 3: L5 contamination (2/30 tasks — FIXED)**
The L5 checkpoint fired during `complete_runtime`'s post-loop commands, appending `<gt-advisory>` text to the patch. Fixed with `action_count <= max_iter` guard.

**Category 4: Bad hunks / wrong edits (remaining tasks)**
Agent edited the wrong lines or wrong files. The patch applies but tests fail. This is a model quality issue.

---

## The Forward Framework: Inform → Reinforce → Enforce

### Inform (L1 + L4 — working, iteration 0)

GT's brief gives file localization + prefetch gives evidence. Fires once at start. Agent has the right starting point. No changes needed here.

### Reinforce (L3 + L5 — broken/ignored, mid-loop)

This is the gap. The agent gets GT signal but treats it as decoration.

**L3 current:** Fires real evidence (contracts, patterns) after each source edit. Agent sees `[GT_CONTRACT] function returns Optional[User]` and ignores it.

**L3 forward fix:** Frame evidence relative to the brief's localization. Instead of raw `[GT_CONTRACT]`, emit: "This edit is to a briefed candidate file. Contract: function returns Optional[User]. 3 callers depend on this." The framing connects the evidence to the localization, reinforcing the brief.

**L5 current:** Shows "Files edited: 2, Pending checks: 1" at 33%/66%. Pure status, no direction.

**L5 forward fix:** Compare `config.edited_files` against the brief's candidate file set. Emit: "Progress audit: 33 iterations used. Briefed candidates: [src/rules/condition.py, src/rules/iam.py]. Files you've edited: [reproduce_issue.py, test_fix.py]. You haven't edited any candidate files yet." This is deterministic — it reads from `config.edited_files` and the stored brief candidates. No LLM, $0, repo-agnostic.

### Enforce (submit-time — missing, needs implementation)

**What SWE-agent does:** At submit time, strips all changes to files that didn't exist at `base_commit`. Only modifications to existing repo files survive. The agent can create whatever scaffolding it wants during the loop; the final patch is clean.

**What we need (legitimate, inside agent loop):**
In the OH wrapper's handling of the finish event (or a custom pre-submit hook), before `complete_runtime` runs `git add -A`:
1. Get list of files at `base_commit`: `git ls-tree -r --name-only <base_commit>`
2. Get list of new files: `git ls-files --others --exclude-standard`
3. Delete new files: `rm` anything not in the base file list
4. Then `git add -A` only captures modifications to existing files

This happens DURING the agent's runtime, before patch extraction. It's the same pattern SWE-agent uses. Every top SWE-bench entry does something equivalent.

**Truncation fix (separate, infrastructure):**
Patch `live_utils.complete_runtime` to use file redirect:
```python
# Current (truncates at ~30K):
command=f'git diff --no-color --cached {instance["base_commit"]}'

# Fixed (no truncation):
command=f'git diff --no-color --cached {instance["base_commit"]} > /tmp/patch.diff'
# Then FileReadAction('/tmp/patch.diff')
```

---

## Legitimacy Constraints

**What "legitimate" means for SWE-bench-Live Lite leaderboard:**

1. **No post-hoc prediction editing.** The agent's output is final. You cannot modify predictions.jsonl after the run.
2. **No task-specific logic.** No conditionals on instance_id, repo name, or issue text patterns.
3. **No manual intervention.** The entire pipeline must be automated end-to-end.
4. **Agent-time processing is fair game.** System prompt engineering, observation modification during the loop, wrapper logic, pre-submit cleanup — all legitimate. Every leaderboard entry does this.
5. **Infrastructure bug fixes are fair game.** Fixing `complete_runtime` truncation is fixing an OH bug, not gaming the benchmark.
6. **The submit-time strip must happen INSIDE the agent loop** (before `complete_runtime`), not as post-processing on the output file.

**What other top entries do:**
- **SWE-agent:** `submit` command strips non-source changes at agent-time
- **Agentless:** No file creation capability — agent can only generate patches for existing files
- **CodeR:** Role separation — fixer role cannot create test files
- **Moatless:** State machine constrains transitions, preventing scaffolding phases
- **AutoCodeRover:** API-based context, no free-form shell — can't create arbitrary files

All of these constrain agent behavior within the loop. None post-process predictions.

---

## Immediate Action Items (next session)

### 1. Fix complete_runtime truncation (2 lines, 5 min)
Patch `live_utils.complete_runtime` on both VMs to use file redirect instead of stdout capture.

### 2. Implement submit-time scaffold strip (20 lines, 15 min)
In the wrapper's finish event handler, before `complete_runtime` runs:
```python
# Get files that existed at base_commit
base_files_cmd = f"git ls-tree -r --name-only {base_commit}"
base_files = set(_run_internal(orig_run_action, base_files_cmd, 30).strip().split('\n'))

# Delete new files
new_files_cmd = "git ls-files --others --exclude-standard"
new_files = _run_internal(orig_run_action, new_files_cmd, 30).strip().split('\n')
for f in new_files:
    if f.strip() and f not in base_files:
        _run_internal(orig_run_action, f"rm -f '{f}'", 10)
```

### 3. Implement L5 localization redirect (30 lines, 20 min)
Store brief candidate files in `config.brief_candidates` during `patched_initialize_runtime`. In the L5 checkpoint, compare `config.edited_files` against `config.brief_candidates`:
```python
briefed = config.brief_candidates  # set of candidate file paths
edited = config.edited_files
edited_briefed = edited & briefed
edited_unbriefed = edited - briefed

if not edited_briefed and briefed:
    advisory += f"\nYou haven't edited any briefed candidate files yet: {sorted(briefed)[:3]}"
    advisory += f"\nYou've edited: {sorted(edited_unbriefed)[:3]}"
```

### 4. Measure brief localization accuracy (research, 30 min)
For each of the 30 tasks, check if the brief's candidate files contain the actual gold edit file. Script: `scripts/audit_l2_localization.py` (already exists, pushed in prior commit). This tells us if the problem is "correct localization, agent ignores" or "wrong localization."

### 5. Re-run 30 tasks with all fixes (60-90 min)
Same 30 Phase 4 tasks, compare against 4/30 current and 6/30 original.

---

## Deep Analysis Required (for the next builder)

Before implementing, the next session should:

1. **Analyze GT→agent interaction for 3 resolved + 3 unresolved tasks.** For each: what did L1 say? Did the agent follow? When did it diverge? What was the first scaffolding action? Could L5 redirect have prevented it?

2. **Research frontier approaches to agent behavior steering.** Key papers:
   - SWE-Search (ICLR 2025): MCTS + value function for stuck detection
   - Agentless (Xia et al., 2024): No agent loop, direct localization→patch
   - CodeR (Chen et al., 2024): Role-based multi-agent separation
   - Moatless (Örbom, 2024): State machine constraining transitions
   - AutoCodeRover (Zhang et al., 2024): API-based structured exploration
   
   Question: which of these patterns can we adopt WITHIN OH's CodeActAgent without modifying OH itself?

3. **Analyze whether the scaffolding behavior is model-specific.** Does Qwen3-Coder scaffold more than Claude/GPT-4? If so, the fix might be model selection, not GT architecture.

4. **Check if the brief's "do not create root-level repro/scaffold files" constraint is even in the agent's context by iteration 30.** OH's context window management might have condensed/dropped it.

---

## Infrastructure State

| Item | State |
|------|-------|
| gt-t0 | OH 0.54.0 + GT oh-gt-combined @ `8865a86`, LiteLLM proxy port 4000 |
| gt-v1 | OH 0.54.0 + GT oh-gt-combined @ `128a21f` (needs pull for L5 fix) |
| Branch | `oh-gt-combined` on `github.com/harneet2512/groundtruth.git` |
| Phase 4 task IDs | In `/tmp/phase4_t0_ids.json` (20) and `/tmp/phase4_v1_ids.json` (10) on respective VMs |
| Results | `~/results/phase4_nf_t0/` and `~/results/phase4_nf_v1/` |
| Clean eval results | `phase4_nf_t0_clean` (3 resolved) and `phase4_nf_v1_clean` (1 resolved) |
