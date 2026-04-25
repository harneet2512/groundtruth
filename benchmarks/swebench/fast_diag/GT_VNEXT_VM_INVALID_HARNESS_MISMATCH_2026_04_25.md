# GT vNext VM Run — INVALID (Harness Mismatch)

**Date:** 2026-04-25
**Commit:** 86df07d / 4d5aa48
**VM:** gt-runner-gcp (n2-standard-16, us-central1-a)
**Output:** vnext_1777104665

---

## Verdict: INVALID RUN

The vNext Decision Interface surfaces (task_map, event_brief, review_patch)
**did not execute** in this run. All GT metadata fields are `n/a` across all arms.

---

## Root Cause

**Harness mismatch:** The vNext surfaces were built into `run_mini_gt_hooked.py`
(mini-SWE-agent harness), but the VM benchmark uses **SWE-agent v1.1.0** with
its own GT integration:

- `swe_agent_state_gt.py` — the hook that runs inside Docker containers
- `tools/groundtruth/` — SWE-agent tool bundle with gt_orient, gt_lookup, gt_impact, gt_check
- Config files: `canary_gt_ds_qwen.yaml`, `canary_nogt_qwen_B.yaml`, etc.

The SWE-agent runner invokes `sweagent run-batch`, which never calls
`run_mini_gt_hooked.py`. The vNext code in `src/groundtruth/schema/`,
`src/groundtruth/mcp/endpoints/`, and the harness modifications to
`run_mini_gt_hooked.py` were completely bypassed.

---

## What Ran

All 4 arms completed 10/10 tasks. The configs used the same model
(qwen3-coder-480b-a35b-instruct-maas via Vertex AI MaaS through LiteLLM proxy).

Arms F1 and F2 used the **OLD GT integration** (swe_agent_state_gt.py),
not the vNext Finding schema or lifecycle surfaces.

---

## Raw Results (scaffold data only, NOT vNext evidence)

### Patch Counts

| Arm | Patched | Zero-edit |
|---|---|---|
| B (baseline) | 5/10 | 5/10 |
| C (shell-only) | 6/10 | 4/10 |
| F1 (old GT nolsp) | 6/10 | 4/10 |
| F2 (old GT lsp) | 6/10 | 4/10 |

### Per-Task Patches

| task | B | C | F1 | F2 |
|---|---|---|---|---|
| 12907 | no | YES | no | YES |
| 13033 | no | YES | YES | no |
| 13236 | YES | no | YES | YES |
| 13398 | YES | YES | YES | no |
| 13453 | no | no | no | YES |
| 13579 | no | no | no | no |
| 13977 | YES | YES | no | YES |
| 14096 | YES | no | YES | no |
| 14182 | YES | YES | YES | YES |
| 14309 | no | YES | YES | YES |

### Resolved Counts (eval complete)

| Arm | Resolved | Patched | Resolved IDs |
|---|---|---|---|
| B (baseline) | **2/10** | 5/10 | 14096, 14182 |
| C (shell-only) | **2/10** | 6/10 | 12907, 14309 |
| F1 (old GT nolsp) | **1/10** | 6/10 | 14309 |
| F2 (old GT lsp) | **3/10** | 6/10 | 12907, 13453, 14309 |

### Per-Task Resolved

| task | B | C | F1 | F2 |
|---|---|---|---|---|
| 12907 | - | RESOLVED | - | RESOLVED |
| 13033 | - | - | - | - |
| 13236 | - | - | - | - |
| 13398 | - | - | - | - |
| 13453 | - | - | - | RESOLVED |
| 13579 | - | - | - | - |
| 13977 | - | - | - | - |
| 14096 | RESOLVED | - | - | - |
| 14182 | RESOLVED | - | - | - |
| 14309 | - | RESOLVED | RESOLVED | RESOLVED |

### Observations (scaffold data only)

- No arm resolves more than 3/10. High stochastic variance at n=10.
- B resolves {14096, 14182}. C resolves {12907, 14309}. Zero overlap.
- F2 resolves the most (3) but this is the OLD GT code, not vNext.
- F1 resolves only 1 — worst of all arms. But again, old GT, not vNext.
- Canary 14309 resolves on C/F1/F2 but not B. Canary 12907 on C/F2 but not B.
- Canary 13453 only on F2.
- These results tell us nothing about vNext because vNext never ran.

---

## vNext Metrics: ALL N/A

Every vNext metric is unavailable because the surfaces never executed:

- task_map_emitted: n/a
- event_brief_emitted: n/a
- review_patch_called_pre_submit: n/a
- submit_paused_for_review: n/a
- review_findings_count: n/a
- review_high_confidence_count: n/a
- findings_fixed: n/a
- findings_acknowledged: n/a
- duplicate_findings_suppressed: n/a
- repeated_signal_rate: n/a
- decision_changed_vs_B: n/a
- agent_had_chance_to_respond_to_review_patch: n/a

---

## What Must Be Ported

To get vNext surfaces into the actual VM benchmark path:

### Target: `swe_agent_state_gt.py` + `tools/groundtruth/` bundle

This is the code that actually runs inside SWE-bench Docker containers
during the benchmark. It needs:

1. **task_map** — port `compute_findings_json()` from gt_intel.py into the
   startup localization brief in swe_agent_state_gt.py

2. **event_brief** — port `--findings-json` structured output and host-side
   novelty filtering into the post-edit micro-update channel

3. **review_patch** — port pre-submit review into the submit interception
   path (the `submit` command handler or `review_on_submit_m` tool)

4. **Novelty filter** — port `_filter_novel_findings` into the per-container
   state tracking

5. **Metadata logging** — add vNext fields to the telemetry JSON:
   task_map_emitted, event_brief_called, review_patch_called_pre_submit,
   submit_paused_for_review, findings_count, etc.

### Files to modify

- `/tmp/SWE-agent/tools/groundtruth/swe_agent_state_gt.py`
- `/tmp/SWE-agent/tools/groundtruth/gt_intel.py` (already has --findings-json)
- `/tmp/SWE-agent/tools/groundtruth/install.sh` (ensure gt-index-static copied)
- Possibly: `/tmp/SWE-agent/config/canary_gt_ds_qwen.yaml` (if template changes needed)

### Proof required before rerun

1. Single dry-run task (13453 or 14309) must show:
   - task_map_emitted=true
   - event_brief_called=true (or skipped with reason)
   - review_patch_called_pre_submit=true
   - agent_had_chance_to_respond_to_review_patch=true
2. At least one vNext metadata record in telemetry
3. No vNext metric inferred from code paths not used by VM

---

## Lessons

1. Architecture contract tests pass locally but verify the wrong code path
2. `run_mini_gt_hooked.py` is a parallel harness, not the production one
3. The actual GT delivery in SWE-agent is through tool bundles, not monkey-patching
4. Test counts (791 passed) mean nothing if the tested code doesn't run on the VM
