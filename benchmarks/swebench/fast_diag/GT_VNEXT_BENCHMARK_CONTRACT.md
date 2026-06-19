# GT vNext Benchmark Contract

**Created:** 2026-04-25
**Purpose:** Freeze the exact configuration for the vNext 4-arm comparison.
**Status:** READY FOR VM RUN (Gate 1 passed, Gate 2: new contract)

> This is NOT a reproduction of V104. GT_V104_COMPLETE_RUN_ANALYSIS.md
> was not recovered. This contract freezes a new baseline and compares
> arms only within this run. Do not compare results to V104 numbers.

---

## Frozen Configuration

### Task Suite

File: `scripts/swebench/frozen_gt_astropy10.txt`

```
astropy__astropy-12907
astropy__astropy-13033
astropy__astropy-13236
astropy__astropy-13398
astropy__astropy-13453
astropy__astropy-13579
astropy__astropy-13977
astropy__astropy-14096
astropy__astropy-14182
astropy__astropy-14309
```

10 tasks, all astropy. Canaries: 12907, 13453, 14309 (historically always-resolved).

### Model

- Name: `qwen3-coder-480b-a35b-instruct-maas`
- Provider: Vertex AI MaaS (or OpenRouter `openai/qwen3-coder`)
- Temperature: **0.2** (matches actual canary configs on VM)
- Top-p: **0.9** (matches canary configs)
- Max output tokens: **8192**

### Runner / Scaffold

- Harness: **SWE-agent v1.1.0** (`/tmp/SWE-agent`)
- Parser: `thought_action`
- LiteLLM proxy: `172.17.0.1:4000` routing to Vertex AI MaaS
- Configs: `/tmp/SWE-agent/config/canary_*.yaml`

### Limits

- `per_instance_call_limit`: 150
- `per_instance_cost_limit`: 0 (unlimited)
- LiteLLM `request_timeout`: 180s

### VM Spec

- Type: GCP `e2-standard-8` (8 vCPU, 32 GB RAM) or Azure equivalent
- Disk: 256 GB SSD (Docker images need ~100 GB)
- Workers: 4 (matching CPU count)

### GT Configuration

- `GT_MAX_FILES`: 5000 (default in `run_mini_gt_hooked.py:131`)
- Bundle commit: current HEAD of `baseline-oh-qwen3coder-live-lite-2026-04-20`
- Indexer: `gt-index-static` (Go binary, tree-sitter)
- Evidence: `gt_intel.py` with `--findings-json` for vNext surfaces

### Eval Command

```bash
python -m swebench.harness.run_evaluation \
    --predictions_path <run_dir>/preds.json \
    --swe_bench_tasks princeton-nlp/SWE-bench_Lite \
    --log_dir <run_dir>/eval_logs \
    --testbed /tmp/swebench_eval
```

---

## Arms

| Arm | Label | GT Intelligence | GT Scaffold | Description |
|---|---|---|---|---|
| **B** | format-repaired baseline | OFF | OFF | Plain mini-SWE-agent + action format repair prompt. No GT hook, no GT binary, no graph.db. |
| **C** | shell-only GT | OFF | ON | GT binary injected, graph.db built, but `gt_intel.py` evidence disabled. Hook structure present but emits nothing. Tests scaffold overhead. |
| **F1** | vNext no-LSP | ON | ON | Full vNext: task_map + event_brief + review_patch. Finding schema, novelty filter, structured output. No LSP. |
| **F2** | vNext LSP-hybrid | ON | ON | Same as F1 + LSP server started for precision edges. Same Finding schema output. |

### Arm B implementation

Use `mini_swebench_pro_baseline.yaml` (or equivalent baseline config). Add the action format repair instruction to the system prompt:

```
Emit exactly one fenced bash block per turn. Do not combine multiple commands.
```

This matches ablation arm B from `ablation_results_qwen_2026-04-24.md`.

### Arm C implementation

Inject `gt-index` and build `graph.db`, but set `GT_EVIDENCE_DISABLED=1` to suppress all evidence output. The hook structure (monkey-patch, container injection) runs but emits nothing.

### Arm F1 implementation

Current `run_mini_gt_hooked.py` with all vNext surfaces active. `--findings-json` mode. Host-side novelty filtering. review_patch fires on git diff/status/submit commands.

### Arm F2 implementation

Same as F1 + `GT_LSP_ENABLED=1`. LSP server started in container after graph.db build. LSP edges promote name_match edges to verified confidence.

---

## Metrics

### Primary

| Metric | Description |
|---|---|
| `resolved` | Tasks where all tests pass after applying patch |
| `patched` | Tasks where a non-empty patch was produced |
| `zero_edit` | Tasks where agent made no file edits |

### vNext Surface Metrics

| Metric | Description |
|---|---|
| `task_map_emitted` | Number of tasks where task_map produced findings |
| `event_brief_emitted` | Number of tasks where event_brief fired at least once |
| `review_patch_called_pre_submit` | Number of tasks where review_patch fired pre-submit |
| `submit_paused_for_review` | Number of tasks where findings were shown before submit |
| `review_findings_count` | Total findings across all tasks |
| `review_high_confidence_count` | Total VERIFIED-tier findings |
| `duplicate_findings_suppressed` | Findings blocked by novelty filter |

### Decision Impact

| Metric | Description |
|---|---|
| `decision_changed_vs_B` | Tasks where agent took different action after GT finding |
| `findings_fixed` | Findings where agent edited the flagged code |
| `findings_acknowledged` | Findings where agent continued without editing |
| `file_choice_accuracy` | Whether agent edited the gold file (vs B) |
| `over_edit_rate` | Files edited beyond gold set |
| `false_positive_findings` | Findings that flagged correct code |

### Infrastructure

| Metric | Description |
|---|---|
| `run_invalid` | Tasks with infra failures |
| `has_patch_rate` | Fraction of tasks producing a patch |
| `repeated_signal_rate` | Fraction of evidence that was repeated (should decrease vs old GT) |

---

## Validity Gates

### Hard gates (run is invalid if any fail)

- All 10 tasks must complete (no killed tasks)
- `run_invalid == 0`
- Canaries {12907, 13453, 14309} must resolve on arm B (proves scaffold works)

### Regression gates (vNext fails regression if any fail)

- F1 `resolved >= B resolved` (no regression vs format-repaired baseline)
- F1 `has_patch_rate >= 0.60`

### Success criteria

- F1 `resolved > B resolved` AND `decision_changed_vs_B >= 1` → **win**
- F1 `resolved == B resolved` AND `decision_changed_vs_B >= 1` → **weak signal**
- F1 `resolved == B resolved` AND `decision_changed_vs_B == 0` → **no signal**
- F1 `resolved < B resolved` → **regression**

### Prohibited claims

- Do not compare to raw no-GT Qwen (scaffold-broken, 0/10)
- Do not compare to V104 numbers (different contract, missing fields)
- Do not claim "GT intelligence works" from n=10
- Do not count post-run telemetry as decision impact
- Do not count review_patch unless it fired pre-submit AND agent could respond

---

## Pre-Run Checklist

- [ ] Gate 1 artifact replay: PASSED (16/16 tests)
- [ ] Gate 2 benchmark contract: THIS DOCUMENT
- [ ] Disk resized to 256 GB
- [ ] Docker images pre-pulled
- [ ] `frozen_gt_astropy10.txt` verified (10 task IDs)
- [ ] All 4 arm configs prepared
- [ ] `verify_report.py` ready for post-run analysis
- [ ] Contract tests pass: `pytest tests/contract/ -v` (all green)

---

## Comparison to V104 (NOT exact reproduction)

Known V104 results (from `verify_results.md`):
- BL: 5/10 resolved
- v2 (best GT): 6/10 resolved

This run uses a different contract (vNext surfaces, different GT delivery, possibly different temperature/config). Results are NOT directly comparable to V104. Compare arms within this run only.
