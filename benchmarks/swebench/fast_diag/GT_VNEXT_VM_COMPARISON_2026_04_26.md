# GT vNext VM 4-Arm Comparison

**Date:** 2026-04-26
**Contract:** GT_VNEXT_BENCHMARK_CONTRACT.md (Path B — new frozen contract)
**VM:** gt-runner-gcp (n2-standard-16, us-central1-a)
**Run:** parallel 4-arm, 1 worker per arm
**Output:** vnext_par_1777162525

---

## Results

### Resolved Counts

| Arm | Resolved | Patched | Zero-edit | Resolved IDs |
|---|---|---|---|---|
| **B** (format-repaired baseline) | **1/10** | 5/10 | 5/10 | 13453 |
| **C** (shell-only GT) | **2/10** | 7/10 | 3/10 | 12907, 13579 |
| **F1** (vNext no-LSP) | **3/10** | 7/10 | 3/10 | 13453, 13579, 14309 |
| **F2** (vNext LSP-hybrid) | **1/10** | 4/10 | 6/10 | 14096 |

### Per-Task Comparison

| task | B | C | F1 | F2 | B resolved | C resolved | F1 resolved | F2 resolved |
|---|---|---|---|---|---|---|---|---|
| 12907 | no(1) | YES(34) | no(4) | no(1) | - | RESOLVED | - | - |
| 13033 | no(13) | YES(46) | YES(13) | YES(37) | - | - | - | - |
| 13236 | no(1) | YES(14) | YES(29) | YES(17) | - | - | - | - |
| 13398 | YES(21) | YES(25) | no(1) | no(11) | - | - | - | - |
| 13453 | YES(21) | no(1) | YES(31) | no(1) | RESOLVED | - | RESOLVED | - |
| 13579 | no(1) | YES(38) | YES(33) | no(1) | - | RESOLVED | RESOLVED | - |
| 13977 | YES(23) | YES(44) | YES(37) | no(1) | - | - | - | - |
| 14096 | YES(81) | no(1) | YES(151) | YES(93) | - | - | - | RESOLVED |
| 14182 | YES(23) | YES(28) | no(1) | YES(15) | - | - | - | - |
| 14309 | no(1) | no(3) | YES(10) | no(1) | - | - | RESOLVED | - |

### F1 vs B: Decision Changes

| task | B | F1 | F1 change |
|---|---|---|---|
| 13453 | RESOLVED | RESOLVED | same |
| 13579 | no patch | RESOLVED | **+1 F1 wins** |
| 14309 | no patch | RESOLVED | **+1 F1 wins** |
| 12907 | no patch | no patch | same |
| 13398 | patched | no patch | B had patch, F1 didn't |
| 14182 | patched | no patch | B had patch, F1 didn't |

**F1 gains +2 resolved vs B** (13579, 14309). B retains 13453. No task where B resolved and F1 didn't.

---

## vNext Surface Metrics (F1)

| Metric | Value |
|---|---|
| task_map_emitted | **9/10** (all except 14096 which timed out) |
| event_brief_called | **6/10** (on tasks with edits) |
| review_patch_called_pre_submit | 0/10 (novelty suppressed all — see note) |
| Surface-tagged task_map deliveries | 9 |
| Surface-tagged event_brief deliveries | 10+ across 6 tasks |
| review_patch visible in obs | 5 tasks (via submit wrapper) |

**Note on review_patch:** In normal mode (GT_REVIEW_PATCH_FORCE_SHOW=OFF), review_patch runs but novelty suppression drops all findings that were already shown by event_brief. The review_patch code fires inside the submit wrapper (proven in dry-runs with force_show), but produces 0 novel findings at submit time.

---

## Validity Gate Status

| Gate | Status |
|---|---|
| All 10 tasks completed per arm | **PASS** (40/40 submitted) |
| run_invalid == 0 | **PASS** (0 errors) |
| Canary 13453 resolves on B | **PASS** |
| F1 resolved >= B resolved | **PASS** (3 >= 1) |
| F1 has_patch_rate >= 0.60 | **PASS** (7/10 = 0.70) |

---

## Analysis

### F1 (vNext no-LSP) vs B (baseline): +2 resolved

F1 resolves 3 tasks vs B's 1. The two tasks F1 gains:

- **13579**: B produced no patch (1 step — instant submit). F1 ran 33 steps with GT evidence, produced a patch that resolved.
- **14309**: B produced no patch (1 step). F1 ran 10 steps with GT evidence, produced a patch that resolved.

Both gains are on tasks where B's agent submitted immediately without working. F1's GT surfaces (task_map briefing + event_brief evidence) may have given the agent enough context to engage with the task instead of giving up.

### F2 (vNext LSP) vs B: no improvement

F2 resolved only 1 task (14096) which B did not. But F2 also missed 13453 (which B resolved). Net: F2 = B = 1 resolved, different tasks. F2 had high zero-edit rate (6/10) suggesting the LSP overhead may have caused more instant-submits.

### C (shell-only) vs B: +1 resolved

C resolved 2 vs B's 1, gaining 12907 and 13579 but losing 13453. The scaffold structure alone (GT prompt format, tool availability) adds some value.

### Stochastic variance

At n=10 with temperature=0.2, the model's behavior is still stochastic. Many tasks show 1-step instant-submits on some arms but multi-step runs on others. The F1 vs B comparison is the most favorable (+2), but within the range of stochastic variance for n=10.

---

## Conclusion: **WEAK SIGNAL**

F1 (vNext no-LSP) beats B (format-repaired baseline) by +2 resolved (3 vs 1). Both gains are on tasks where B's agent didn't engage but F1's agent did — potentially influenced by GT task_map briefing. However:

1. n=10 is too small for statistical significance
2. The gains could be stochastic (B had 5 instant-submits, F1 had 3)
3. F2 (LSP) did not outperform B
4. review_patch novelty suppression means the pre-submit surface added no incremental signal beyond event_brief
5. No task shows a clear "GT finding changed the agent's edit" — the gains are "agent worked vs didn't work"

**Not a proven win.** The signal is directionally positive for F1 but not conclusive. A larger sample (n=50+) with multiple runs would be needed to distinguish GT intelligence from stochastic engagement differences.

---

## Configuration

- Model: qwen3-coder-480b-a35b-instruct-maas via Vertex AI MaaS
- Temperature: 0.2
- Step limit: 150
- Runner: SWE-agent v1.1.0
- LiteLLM proxy: 172.17.0.1:4000
- Task suite: frozen_gt_astropy10 (10 tasks, SWE-bench Verified)
- GT_VNEXT: 1 (F1, F2), OFF (B, C)
- GT_REVIEW_PATCH_FORCE_SHOW: OFF (normal mode)
- GT_LSP_ENABLED: 1 (F2 only)
