# Leaderboard Prep Plan — benchmark/leaderboard-prep

## 1. Branch Purpose

This branch answers one question:
> Does a minimal GroundTruth intervention improve SWE-bench Lite outcomes under a leaderboard-compatible scaffold?

## 2. What This Branch Proves (If Positive)

> "A narrow deterministic GroundTruth runtime — a single structural completeness check — improved SWE-bench Lite outcomes from X% to Y% under an OpenHands scaffold with Qwen3-Coder."

## 3. What This Branch Does NOT Prove

- The full GroundTruth MCP product was benchmark-validated
- All incubator features (contradictions, conventions, abstention, communication framing) were proven
- LSP-based intelligence improved outcomes (benchmark uses stdlib AST, not LSP)
- Leaderboard score equals product proof

## 4. Base Branch

**master** at `5ee5ca9` (fix: only correct project-local symbols — positive evidence required)

Why:
- Cleanest provenance — no analysis artifacts or research entanglement
- Benchmark uses standalone gt_tool.py, not product code in src/groundtruth/
- research/incubator-integration (127 commits ahead) is too heavy for a narrow benchmark

## 5. Model / Provider / Scaffold

| Property | Value |
|----------|-------|
| Model | Qwen3-Coder-480B |
| Provider | Vertex AI MaaS |
| GCP Project | regal-scholar-442803-e1 |
| Region | us-south1 |
| litellm string | vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas |
| Params | temperature=0.7, top_p=0.8 |
| Cost | Free (MaaS tier) |
| Scaffold | OpenHands |
| Target | SWE-bench Lite (300 tasks) |

## 6. Benchmark Conditions

**Condition 1: Baseline**
- OpenHands scaffold, Qwen3-Coder, SWE-bench Lite
- No GT tools available
- Standard OpenHands prompt

**Condition 2: GT Minimal (check-only)**
- Same scaffold, model, tasks
- gt_tool_check_only.py injected into containers
- gt_check_hardgate.j2 prompt (mandatory pre-submit structural check)
- Only `groundtruth_check` command available — no references, impact, scope, etc.

## 7. Exact Minimal GT Delta

**Tool:** `groundtruth_check` only — structural completeness validator
- Analyzes git diff against AST-based codebase index
- Reports obligation sites sharing state with modified code that were NOT updated
- 5 check types: init-attr consistency, method-call validity, import verification, contradiction detection, pyright diagnostics

**Excluded (and why):**
- gt_impact: zero outcome correlation (58% vs 57% in 500-task eval)
- gt_references: zero outcome correlation
- summary/outline: duplicates cat/grep, wastes step budget
- scope/context/related: exploration tools consuming budget
- diagnose: model already does syntax checks
- gt_autocorrect: too aggressive, hard to attribute
- All incubator features: out of scope

## 8. Benchmark Asset Inventory

| File | Purpose | Status |
|------|---------|--------|
| `benchmarks/swebench/gt_tool_check_only.py` | Stripped check-only GT runtime | NEW — 822 lines |
| `scripts/swebench/prompts/gt_check_hardgate.j2` | Hard-gate prompt template | NEW |
| `scripts/swebench/oh_gt_mount_wrapper.py` | Injection wrapper for check-only tool | NEW |
| `scripts/swebench/oh_run_leaderboard_gt.sh` | GT condition run script | NEW |
| `scripts/swebench/oh_run_leaderboard_baseline.sh` | Baseline condition run script | NEW |
| `scripts/swebench/oh_smoke_leaderboard.sh` | Gate 0 smoke test | NEW |
| `scripts/swebench/oh_setup_proxy.sh` | litellm proxy setup | EXISTING — reuse |
| `scripts/swebench/oh_vm_setup.sh` | VM bootstrap | EXISTING — reuse |
| `scripts/swebench/oh_llm_config_vertex_qwen3.json` | Model config | EXISTING — reuse |
| `scripts/swebench/oh_analyze_results.py` | Results analysis | EXISTING — reuse |

## 9. Files In Scope

- `benchmarks/swebench/gt_tool_check_only.py`
- `scripts/swebench/prompts/gt_check_hardgate.j2`
- `scripts/swebench/oh_gt_mount_wrapper.py`
- `scripts/swebench/oh_run_leaderboard_gt.sh`
- `scripts/swebench/oh_run_leaderboard_baseline.sh`
- `scripts/swebench/oh_smoke_leaderboard.sh`
- `LEADERBOARD_PREP_PLAN.md` (this file)

## 10. Files Out of Scope

- `src/groundtruth/**` — product code, not used in benchmark
- `benchmarks/swebench/gt_tool.py` — full tool, not modified
- `scripts/swebench/oh_run_baseline.sh` — original baseline, preserved
- `scripts/swebench/oh_llm_config_vertex_qwen3.json` — model config, unchanged
- `master` branch — all work on benchmark/leaderboard-prep

## 11. Gate Model

### Gate 0 — Infrastructure Smoke (1 task)
- Task: django__django-12856
- Pass: proxy OK, model OK, injection OK, output.jsonl exists, load < 10
- Fail: debug infra, do NOT proceed

### Gate 1 — 10-Task Diagnostic
- 10 tasks spanning django, astropy, sympy
- Pass: both conditions complete, gt_check called >= 7/10, baseline >= 50%
- Fail: revise prompt or diagnose scaffold

### Gate 2 — 50-Task Intermediate
- First 50 from SWE-bench Lite (alphabetical)
- Pass: GT >= baseline (delta >= 0), gt_check rate >= 60%
- Kill: GT < baseline by > 3 tasks (> 6% deficit)

### Gate 3 — 300-Task Full Lite
- All 300 SWE-bench Lite tasks
- Pass: GT > baseline (any positive delta)
- Fail: no submission, document findings

## 12. Pass/Fail Criteria

| Gate | Pass | Kill |
|------|------|------|
| 0 | Both runs complete, injection confirmed, load < 10 | Any crash or config failure |
| 1 | gt_check called >= 7/10, baseline >= 50% | gt_check < 5/10 or baseline < 40% |
| 2 | GT resolve >= baseline, gt_check rate >= 60% | GT < baseline by > 3 tasks |
| 3 | GT > baseline (any positive delta) | GT <= baseline |

## 13. First Implementation Tasks

1. ~~Create branch from master~~ DONE
2. ~~Create gt_tool_check_only.py~~ DONE
3. ~~Create gt_check_hardgate.j2~~ DONE
4. ~~Create oh_gt_mount_wrapper.py~~ DONE
5. ~~Create run scripts~~ DONE
6. ~~Write LEADERBOARD_PREP_PLAN.md~~ DONE (this file)
7. Commit and verify branch integrity
8. **NEXT:** Deploy to GCP VM, run Gate 0

## 14. Risks

| Risk | Mitigation |
|------|-----------|
| Qwen3-Coder MaaS unavailable | Check at Gate 0 |
| OpenHands baseline too weak (< 55%) | Gate 1 credibility check |
| gt_check still net-negative | Hard-gate framing, stripped tool reduces waste |
| VM overload | Stripped tool (34KB vs 108KB), fewer chunks |
| Docker images missing | Budget 45 min for pulls |

## 15. Kill Conditions

- VM load > 50 at any gate → stop immediately
- Model API errors > 20% of tasks → stop immediately
- GT < baseline by > 6% at Gate 2 → kill branch
- Baseline < 50% at Gate 2 → scaffold is broken, fix before GT comparison

## 16. Submission Narrative Guardrails

**Allowed:** "A narrow deterministic GroundTruth runtime improved outcomes under this scaffold."
**Not allowed:** "The full GroundTruth product is proven."

Transparency:
- Disclose gt_tool uses Python stdlib ast, not LSP
- Disclose previous 500-task run showed -0.6% delta on different scaffold
- Disclose exact prompt difference between conditions
- Report with confidence intervals
