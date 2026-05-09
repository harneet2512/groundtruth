# Validation Prompt: Layer-by-Layer GT Audit on Local Docker

Copy everything below the line and paste into a new Claude Code session at `D:\Groundtruth`:

---

## Current State Summary

### Layer Status Table

| Layer | Status | Proven Value | Verdict |
|-------|--------|:---:|---|
| **L1 Brief** | Works | Unknown — accuracy never audited | Core product. **Audit first.** |
| **L3 Post-edit** | 75% empty | Pacing only, content ignored | Deprioritize content. Keep placeholder. |
| **L3b Post-view** | Same as L3 | Pacing only | Deprioritize. |
| **L4 Tools** | 0% usage | Dead | Kill tools. Keep prefetch. |
| **L4 Prefetch** | Works | Unknown | Right pattern, needs quality check. |
| **L5 Gate** | Architecturally broken in OH | Zero | Fires too late, can't catch scaffolding. |
| **L6 Reindex** | Works, zero bugs | Supports L3/L4 | Keep. |
| **Scaffold strip** | Works | Zero resolve lift | Keep as hygiene. |
| **Truncation fix** | Works | Infrastructure fix | Keep permanently. |
| **Interaction logging** | Broken (flush fails) | Zero — no data collected | Fix before anything else. |

### Results Across All Verified Runs (Same 30 Tasks)

| Run | Resolved | Rate | Verified |
|-----|:---:|:---:|:---:|
| OH Baseline (no GT) | 4/30 | 13.3% | YES |
| Noisy GT (original) | 6/30 | 20.0% | NO (prior session memory) |
| Clean GT (eliminate empty) | 3/29 | 10.3% | YES |
| Phase 5 Enforce GT | 3/29 | 10.3% | YES |
| Compression GT | 4/30 | 13.3% | YES |

**GT is net-zero on resolve rate.** The compression fix recovered the clean GT regression but never beat baseline. The unverified "6/30" noisy GT is Fisher's p=0.73 vs baseline — not significant.

### The 30 Phase 4 Task IDs (Frozen Eval Set)

**gt-t0 (20 tasks):**
```
aiogram__aiogram-1594
aws-cloudformation__cfn-lint-3789
aws-cloudformation__cfn-lint-3798
aws-cloudformation__cfn-lint-3821
aws-cloudformation__cfn-lint-3854
aws-cloudformation__cfn-lint-3856
aws-cloudformation__cfn-lint-3862
aws-cloudformation__cfn-lint-3866
aws-cloudformation__cfn-lint-3875
aws-cloudformation__cfn-lint-3890
aws-cloudformation__cfn-lint-4002
aws-cloudformation__cfn-lint-4023
aws-cloudformation__cfn-lint-4032
beancount__beancount-931
beetbox__beets-5495
beeware__briefcase-2075
beeware__briefcase-2085
bridgecrewio__checkov-6893
bridgecrewio__checkov-6895
bridgecrewio__checkov-7002
```

**gt-v1 (10 tasks):**
```
arviz-devs__arviz-2413
aws-cloudformation__cfn-lint-3779
aws-cloudformation__cfn-lint-3805
aws-cloudformation__cfn-lint-4016
delgan__loguru-1306
kozea__weasyprint-2303
pydata__xarray-9760
pydata__xarray-9971
pylint-dev__pylint-10044
pypa__twine-1225
```

**Baseline resolves (verified):** beancount-931, briefcase-2075, weasyprint-2303, twine-1225

## How Our OH Integration Works

Read `docs/handoff/OH_INTEGRATION_LEGITIMACY.md` for the full architecture. Key points:

- GT integrates by monkey-patching two OH functions: `initialize_runtime` (runs once per task) and `run_action` (wraps every agent action)
- No OH source code is modified
- All GT intelligence comes from graph.db (tree-sitter AST index) — zero LLM, zero gold info
- The wrapper is at `scripts/swebench/oh_gt_full_wrapper.py`
- Everything happens DURING the agent loop — nothing post-processes predictions

## Your Task: Layer-by-Layer Additive Validation

**Method:** Start from baseline (no GT). Add ONE layer at a time. Verify no regression before adding the next. Each phase produces measured data. We go FORWARD only when the previous phase shows no regression.

**Research basis for this method:**
- Ablation studies are standard in ML experiment design (Melis et al., 2018, "On the State of the Art of Evaluation in Neural Language Models")
- SWE-agent (Yang et al., NeurIPS 2024) validated each ACI component independently before combining
- The "change one variable at a time" principle from experimental design (Fisher, 1935)

**Environment:** Everything runs locally on Windows in Docker. No GCP VMs. No cloud spend.

### Setup: Local Docker Environment

1. Install Docker Desktop for Windows if not present
2. Pull OpenHands 0.54.0:
```bash
docker pull ghcr.io/all-hands-ai/openhands:0.54.0
```
3. Set up a local LLM proxy (LiteLLM) pointing at your Vertex MaaS endpoint, OR use a local model for faster iteration
4. Pull SWE-bench-Live eval images for the 30 tasks:
```bash
# Each task needs: docker.io/starryzhang/sweb.eval.x86_64.<repo>_<num>_<project>-<issue>:latest
```
5. Clone and set up OH locally:
```bash
git clone https://github.com/All-Hands-AI/OpenHands.git --branch 0.54.0
cd OpenHands && pip install -e .
```
6. Checkout GT code:
```bash
cd D:\Groundtruth
git checkout -b validate_me
git push origin validate_me
```

### Phase 0: L1 Localization Accuracy Audit ($0, no runs needed)

**THIS IS THE HIGHEST PRIORITY. Do this before ANY runs.**

**What:** For each of the 30 tasks, check if the GT brief's candidate files contain at least one actual gold edit file.

**Why (research basis):**
- Agentless (Xia et al., 2024) showed that localization accuracy is the #1 predictor of resolve rate
- Our 5-case diagnosis (2026-04-30) found agents reach gold files 88% of the time WITHOUT GT — but we never checked if GT's L1 brief actually IMPROVES this
- If L1 points at the wrong files, everything downstream is wasted

**How:**
1. Get gold edit files from SWE-bench-Live dataset:
```python
from datasets import load_dataset
ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="lite")
for row in ds:
    if row["instance_id"] in TASK_IDS:
        # Parse row["patch"] to extract modified file paths
        gold_files = extract_files_from_patch(row["patch"])
```
2. Generate GT briefs for each task (offline — just run the brief generator against each repo's graph.db):
```python
from groundtruth.pretask.v7_brief import generate_brief
# For each task, build graph.db from the repo at base_commit, generate brief
```
3. Compare: for each task, is ANY gold file in the brief's top-5 candidates?
4. Report:
   - **Precision:** what % of brief candidates are actual gold files
   - **Recall:** what % of gold files appear in the brief's candidates
   - **First-gold-rank:** where does the first gold file appear in the ranking (1st? 3rd? not present?)

**Decision gate:**
- If recall >= 60%: L1 is pointing at the right files. Problem is downstream (agent behavior). Proceed to Phase 1.
- If recall < 40%: L1 is pointing at wrong files. STOP. Fix localization before anything else.
- If 40-60%: Mixed. Proceed to Phase 1 but flag localization improvement as parallel work.

### Phase 1: Baseline (No GT) — Establish Floor

**What:** Run the 30 tasks with pure OH + Qwen3-Coder, zero GT wrapper.

**Why:** We need a clean local baseline. The prior baseline (4/30) was on GCP VMs. Local Docker may behave differently (disk I/O, network, container lifecycle). We cannot compare GT runs against a baseline from a different environment.

**How:**
```bash
cd OpenHands
python -m evaluation.benchmarks.swe_bench.run_infer \
  -l qwen3 --agent-cls CodeActAgent \
  --dataset SWE-bench-Live/SWE-bench-Live --split lite \
  --max-iterations 100 --eval-num-workers 2 \
  --eval-output-dir results/baseline_local \
  --eval-ids [TASK_IDS]
```

**Measure:**
- Resolved count
- Patch rate (how many tasks produced a non-empty patch)
- Per-task: resolved / unresolved / patch didn't apply

**Decision gate:** Record the number. This is your floor. Every subsequent phase must be >= this number.

### Phase 2: +L1 Brief Only — Test Localization Value

**What:** Enable ONLY L1 brief injection. No L3, no L3b, no L4, no L5, no L6, no scaffold strip. Just the brief in the initial instruction.

**Why (research basis):**
- SWE-agent validates each ACI component independently
- The brief is our "map" — we need to know if the map alone changes behavior
- Agentless showed localization is the highest-leverage intervention

**How:** Modify `oh_gt_full_wrapper.py`:
- Keep `patched_get_instruction` (injects brief)
- DISABLE `patched_run_action` entirely (comment out the wrapper or make it pass-through)
- No hooks, no evidence, no checkpoints, no strip

**Measure:**
- Resolved count vs baseline
- Per-task: did the agent open/edit any brief candidate file in its first 5 actions?
- Flips: tasks that resolved with brief but NOT in baseline, and vice versa

**Decision gate:**
- If resolved >= baseline: L1 is not hurting. Proceed.
- If resolved < baseline: L1 is actively harmful. STOP. Investigate which tasks regressed and why.

### Phase 3: +L1 +L3/L3b Compression Pacing — Test Observation Presence

**What:** Add L3/L3b post-edit/post-view hooks with compression placeholders (`[GT_OK] No concerns.`). NO real evidence content — just the placeholder on every edit/view.

**Why (research basis):**
- JetBrains NeurIPS 2025 "The Complexity Trap": observation slot presence matters independently of content
- Our own data: eliminating empty evidence HURT (3/29), compressing to placeholders RECOVERED (4/30)
- This tests whether pacing adds value on top of the brief

**How:** Enable `patched_run_action` but modify L3/L3b to ALWAYS emit `[GT_OK] No concerns.` regardless of hook output. Do not run the actual evidence families — just emit the placeholder.

**Measure:**
- Resolved count vs Phase 2
- Does the agent's iteration pattern change? (measure: iteration of first source edit, iteration of first scaffold file creation)

**Decision gate:**
- If resolved >= Phase 2: Pacing helps or is neutral. Proceed.
- If resolved < Phase 2: Pacing hurts. Revert. Investigate.

### Phase 4: +L1 +L3/L3b Real Evidence — Test Content Value

**What:** Now enable the actual L3/L3b evidence families (CHANGE, CONTRACT, PATTERN, STRUCTURAL, SEMANTIC). When they produce real evidence, inject it. When they produce nothing, inject the `[GT_OK]` placeholder.

**Why:** This isolates whether the CONTENT of L3 evidence adds value beyond the pacing signal.

**How:** Enable the full L3/L3b hook pipeline. Keep compression for empty results.

**Measure:**
- Resolved count vs Phase 3
- How many evidence blocks had real content vs placeholder?
- For tasks where real evidence fired: did the agent's behavior change?

**Decision gate:**
- If resolved > Phase 3: Content adds value. Keep real evidence.
- If resolved == Phase 3: Content is neutral. Keep it (doesn't hurt, might help on other repos).
- If resolved < Phase 3: Content hurts (cry-wolf or attention damage). Revert to placeholder-only.

### Phase 5: +L4 Prefetch — Test Pre-computed Evidence

**What:** Add L4 prefetch (pre-computed gt_query results for issue-relevant symbols, injected into the brief).

**Why (research basis):**
- AutoCodeRover (Zhang et al., ISSTA 2024): pre-computed structured context outperforms agent-driven search
- Our data: 0% tool usage means the agent will never discover this evidence on its own. Prefetch is the only delivery path.

**How:** Enable `_run_l4_prefetch` in `patched_initialize_runtime`. Append prefetch block to brief.

**Measure:**
- Resolved count vs Phase 4
- Token count of brief (brief + prefetch combined)
- Did prefetch evidence appear relevant to the actual bug?

**Decision gate:** Same as Phase 4.

### Phase 6: +L6 Reindex + Scaffold Strip — Test Infrastructure

**What:** Add L6 (incremental reindex) and scaffold strip (new file deletion before submit).

**Why:** These are infrastructure/hygiene layers. They shouldn't change resolve rate but should clean up patches.

**How:** Enable L6 reindex commands and the `_strip_scaffold_files` function in the finish/post-loop handler.

**Measure:**
- Resolved count vs Phase 5 (should be identical)
- Patch size comparison (scaffold strip should reduce patch sizes)
- Any patch-apply failures that were previously passing?

**Decision gate:**
- If resolved >= Phase 5 AND patch sizes reduced: Keep both.
- If resolved < Phase 5: One of these is breaking something. Test each independently.

### Phase 7: Full GT — Final Comparison

**What:** Run with all layers enabled (L1 + L3/L3b + L4 prefetch + L6 + scaffold strip). This is the "compression GT" configuration.

**Measure:**
- Resolved count vs baseline (Phase 1)
- Per-task flip analysis: which tasks flipped resolved/unresolved compared to baseline?
- Total token overhead from GT per task

**Final comparison table:**

| Phase | Config | Resolved | Delta vs Baseline |
|-------|--------|:---:|:---:|
| 1 | Baseline | X/30 | 0 |
| 2 | +L1 | X/30 | ? |
| 3 | +L1+Pacing | X/30 | ? |
| 4 | +L1+L3 real | X/30 | ? |
| 5 | +L1+L3+L4 | X/30 | ? |
| 6 | +L1+L3+L4+L6+strip | X/30 | ? |
| 7 | Full GT | X/30 | ? |

## Interaction Logging (Fix BEFORE Phase 3)

The current logging code writes to `config.interaction_log` in memory but never flushes to disk. Fix this BEFORE Phase 3:

**The fix:** Write to a file inside the container on EVERY interaction, not batched at flush:
```python
def _log_gt_interaction(config, layer, trigger, ev_type, gt_sent):
    import json
    entry = {
        "iter": config.action_count,
        "layer": layer,
        "trigger": trigger,
        "type": ev_type,
        "gt_sent": gt_sent[:300]
    }
    # Write immediately to container file — no flush dependency
    try:
        with open("/tmp/gt_interactions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
```

Then add `/tmp/gt_interactions.jsonl` to the artifact pull in `_pull_hook_logs`.

**Verify logging works in a 1-task smoke BEFORE scaling to 30.**

## Git Branch

1. Create `validate_me` branch from current `oh-gt-combined`:
```bash
git checkout oh-gt-combined
git checkout -b validate_me
git push origin validate_me
```
2. All validation work happens on `validate_me`
3. Do NOT merge back to `oh-gt-combined` until all phases complete

## What NOT to Do

- Do NOT skip Phase 0 (L1 audit). It's $0, 30 minutes, and determines everything.
- Do NOT add multiple layers at once. One layer per phase. No exceptions.
- Do NOT proceed to the next phase if the current phase shows regression.
- Do NOT run 30 tasks with broken logging. Fix logging, verify with 1-task smoke, then scale.
- Do NOT modify OH source code. Everything goes through the wrapper.
- Do NOT post-process predictions.jsonl after runs. All processing happens during the agent loop.
- Do NOT use task-specific logic (no conditionals on instance_id, repo name, issue patterns).
- Do NOT trust numbers from prior sessions without re-verifying. The "6/30 noisy GT" is UNVERIFIED.
- Do NOT call patch-apply failures "errors" — they are wrong patches (agent behavior), not infrastructure failures. The only real infra failure is briefcase-2085 (container stuck).
- Do NOT propose L5 hard rejection gates — OH's CodeActAgent has no action rejection mechanism.
- Do NOT propose L4 "MANDATORY" tool directives — 0% tool usage across all runs proves LLMs ignore directive labels.
- Do NOT propose L3 "active contrast" evidence — the graph is 70-80% name_match at low confidence, evidence families already run and produce nothing for most repos. You cannot generate useful sibling analysis from a sparse graph.

## Key Files

| File | Purpose |
|------|---------|
| `scripts/swebench/oh_gt_full_wrapper.py` | THE wrapper. All GT↔OH integration. |
| `src/groundtruth/pretask/v7_brief.py` | L1 brief generation |
| `src/groundtruth/hooks/post_edit.py` | L3 evidence (5 families) |
| `src/groundtruth/hooks/post_view.py` | L3b structural coupling |
| `docs/handoff/OH_INTEGRATION_LEGITIMACY.md` | Full integration architecture |
| `docs/handoff/final_tccomp.md` | Complete run autopsy with all data |
| `docs/handoff/PHASE5_FORWARD.md` | Inform→Reinforce→Enforce framework |
| `docs/handoff/PHASE6_BUILD_PROMPT.md` | Prior handoff (noisy GT verification) |

## Research References Used in This Methodology

1. **JetBrains NeurIPS 2025** "The Complexity Trap" — observation slot presence matters independently of content. Compress don't eliminate.
2. **SWE-agent (Yang et al., NeurIPS 2024)** — ACI component ablation methodology. Validate each component independently.
3. **Agentless (Xia et al., 2024)** — Localization accuracy is the #1 predictor of resolve rate.
4. **AutoCodeRover (Zhang et al., ISSTA 2024)** — Pre-computed structured context outperforms agent-driven search.
5. **Lost in the Middle (Liu et al., TACL 2024)** — LLMs attend to start+end of context. Brief goes at start, constraints at end.
6. **Fisher (1935)** — Change one variable at a time in experimental design.
