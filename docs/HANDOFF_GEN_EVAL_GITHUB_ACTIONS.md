# Handoff: Run Generalized GT Eval on GitHub Actions

## Prompt for Next Session

```
I need you to run the generalized GT code against the pre-gen baseline on SWE-bench-Live Lite.

Context:
- GroundTruth (GT) is a deterministic codebase intelligence layer that injects evidence
  into an AI coding agent during SWE-bench tasks. Zero LLM cost, pure graph analysis.
- We just ran the pre-gen GT code (commit fcea7f9) on 20 tasks and got 3/20 RESOLVED
  using the official Microsoft SWE-bench-Live harness.
- The generalized code (branch general_start) adds: hub demotion, V1R brief, adaptive K,
  scaffold strip, improved L3 evidence, L5 redirect at 33%/66%. But it regressed in prior
  VM tests because generate_improved_evidence() (G6) fired unconditionally — Decision 29
  has the fix (gate G6 on brief_candidates existing).

What to do:
1. Read docs/HANDOFF_GEN_EVAL_GITHUB_ACTIONS.md for full context
2. Read docs/HOW_TO_RUN_EVALS.md for the ONLY legitimate eval path
3. Add OPENROUTER_KEY secret to the GitHub repo (value from cloud_access.md on Desktop)
4. Update .github/workflows/swebench_eval.yml to use OpenRouter instead of GCP Vertex
5. Verify Decision 29 fixes A-D are applied in the general_start branch code
6. Run the 20-task eval via GitHub Actions (no VMs needed)
7. Compare against pre-gen baseline: 3/20 RESOLVED (sh-744, weasyprint-2300, weasyprint-2303)

Rules:
- NEVER write custom eval scripts. Use Microsoft's python-only branch harness ONLY.
  See docs/HOW_TO_RUN_EVALS.md.
- The 20 tasks are in benchmarks/smoke_30_split.json (first 20 from t0_ids + v1_ids)
- Every change must pass: "would this help on any private repo?" No benchmaxxing.
- Report cost after every run.

Pre-gen failure breakdown (what to improve):
- 3 RESOLVED (sh-744, weasyprint-2300, weasyprint-2303)
- 9 F2P_ONLY: agent's fix didn't address the actual bug
- 2 P2P_REGRESS: bug was fixed but broke other tests (cfn-lint-4023, pypsa-1091 — near misses)
- 6 F2P+P2P: didn't fix bug AND broke tests

Near-misses that could flip with better GT:
- cfn-lint-4023: F2P passed, 7 P2P regressions (scaffold strip + evidence could save this)
- pypsa-1091: F2P passed, 2 P2P regressions
- loguru-1306: 5/10 F2P pass (halfway there)
- pypsa-1112: 7/13 F2P pass (halfway there)
```

---

**Goal:** Run the generalized GT code (branch `general_start`) on 20 SWE-bench-Live Lite tasks using GitHub Actions + OpenRouter. No VMs. No GCP.

**Pre-gen baseline (official harness): 3/20 RESOLVED** (sh-744, weasyprint-2300, weasyprint-2303). This is the floor to beat.

---

## Step 1: Add OpenRouter secret to GitHub repo

1. Go to https://github.com/harneet2512/groundtruth/settings/secrets/actions
2. Click "New repository secret"
3. Name: `OPENROUTER_KEY`
4. Value: (get from `cloud_access.md` on Desktop — the `open_key` field)
5. Click "Add secret"

## Step 2: Update the eval workflow to use OpenRouter

The current workflow at `.github/workflows/swebench_eval.yml` uses GCP Vertex AI + LiteLLM proxy. It needs to be updated to use OpenRouter directly.

### Changes needed in `swebench_eval.yml`:

**Remove:**
- The `google-github-actions/auth@v2` step
- The "Start LiteLLM proxy" step
- The `GCP_SA_KEY` secret reference
- The `VERTEX_PROJECT` and `VERTEX_LOCATION` env vars

**Replace the OH config.toml with:**
```toml
[core]
max_iterations = 100
default_agent = "CodeActAgent"

[llm.eval]
model = "openai/qwen/qwen3-coder-480b-a35b-instruct-maas"
api_key = "${{ secrets.OPENROUTER_KEY }}"
base_url = "https://openrouter.ai/api/v1"
temperature = 0.7
top_p = 1.0
max_output_tokens = 8192
caching_prompt = false

[sandbox]
runtime_container_image = "ghcr.io/all-hands-ai/runtime:0.54-nikolaik"
```

**Note:** Check if OpenRouter hosts `qwen/qwen3-coder-480b-a35b-instruct-maas` under that exact model ID. If not, find the correct model slug on https://openrouter.ai/models and update accordingly. The OH wrapper passes the model name to litellm which passes it to the API.

**Replace the Docker image pull step** to use GHCR only (already cached):
```yaml
- name: Pull SWE-bench Docker images
  run: |
    echo "${{ secrets.GITHUB_TOKEN }}" | docker login ghcr.io -u ${{ github.actor }} --password-stdin
    # Images already cached in ghcr.io/harneet2512/sweb.eval.x86_64.*
    # OH will pull them automatically via --namespace
```

**Add the eval step** using Microsoft's python-only branch:
```yaml
- name: Install SWE-bench-Live harness
  run: |
    git clone --branch python-only --recursive https://github.com/microsoft/SWE-bench-Live.git /tmp/SWE-bench-Live
    cd /tmp/SWE-bench-Live
    pip install -e .

- name: Run official eval
  run: |
    cd /home/ubuntu/OpenHands-0.54.0  # adjust path for Actions runner
    python evaluation/benchmarks/swe_bench/scripts/live/convert.py \
      --output_jsonl $OUTPUT_DIR/output.jsonl > /tmp/preds.jsonl
    
    cd /tmp/SWE-bench-Live
    python -m swebench.harness.run_evaluation \
      --dataset_name SWE-bench-Live/SWE-bench-Live \
      --split lite \
      --namespace starryzhang \
      --predictions_path /tmp/preds.jsonl \
      --max_workers 4 \
      --run_id gen_eval
```

## Step 3: Trigger the workflow

From CLI:
```bash
gh workflow run swebench_eval.yml \
  --repo harneet2512/groundtruth \
  --ref general_start \
  -f gt_commit=general_start \
  -f task_count=20 \
  -f run_name=gen_baseline
```

Or from the Actions tab if workflows are on the default branch.

## Step 4: Read results

Results appear as GitHub Actions artifacts. Download and check:
```bash
gh run download <run_id> --repo harneet2512/groundtruth
cat merged-results-gen_baseline/results.json
```

---

## The 20 Task IDs (same as pre-gen baseline)

```
delgan__loguru-1297,delgan__loguru-1306,flexget__flexget-4306,flexget__flexget-4244,kozea__weasyprint-2300,kozea__weasyprint-2387,kozea__weasyprint-2405,kozea__weasyprint-2398,kozea__weasyprint-2303,pypsa__pypsa-1172,pypsa__pypsa-1112,pypsa__pypsa-1091,pypsa__pypsa-1195,aiogram__aiogram-1594,amoffat__sh-744,arviz-devs__arviz-2413,aws-cloudformation__cfn-lint-3875,aws-cloudformation__cfn-lint-3890,aws-cloudformation__cfn-lint-3855,aws-cloudformation__cfn-lint-4023
```

## Pre-Gen Baseline Failure Breakdown (for comparison)

| Mode | Count | What it means |
|------|-------|---------------|
| RESOLVED | 3 | Bug fixed, no regressions |
| F2P_ONLY | 9 | Patch applied but didn't fix the bug |
| P2P_REGRESS | 2 | Bug fixed but broke other tests |
| F2P+P2P | 6 | Didn't fix bug AND broke other tests |

**Near-misses (would flip with better evidence/scaffold-strip):**
- cfn-lint-4023: F2P passed but 7 P2P regressions
- pypsa-1091: F2P passed but 2 P2P regressions
- loguru-1306: 5/10 F2P pass
- pypsa-1112: 7/13 F2P pass

## What the Generalized Code Changes (vs pre-gen)

The `general_start` branch has these changes over pre-gen (`fcea7f9`):
- Hub demotion (p90 in-degree), sparse BM25 fallback, adaptive K
- V1R brief (hybrid scoring instead of cochange-only)
- `generate_improved_evidence()` (G6) — **MUST be gated on brief_candidates**
- GT_OK injection, GT_CONTEXT framing — **these should be REMOVED (they caused regression)**
- L5 at 33%/66% instead of {15,30,45}
- Scaffold strip
- Interaction logging

**CRITICAL:** If running `general_start` as-is, Decision 29 fixes (A-D) must be applied or the G6 regression will kill results. Check that `post_edit.py` gates `generate_improved_evidence()` on `brief_candidates` existing.

## Files on GitHub

| File | Branch | Purpose |
|------|--------|---------|
| `.github/workflows/swebench_eval.yml` | `general_start` | Eval workflow (needs OpenRouter update) |
| `.github/workflows/cache_swebench_images.yml` | `general_start` | Image cache (DONE, all 30 cached) |
| `docs/HOW_TO_RUN_EVALS.md` | `general_start` | Eval runbook |
| `docs/swebench_30task_runbook.md` | `general_start` | VM run commands |
| `docs/workflows-github.md` | `general_start` | Full process log + known issues |
| `scripts/swebench/oh_gt_full_wrapper.py` | `general_start` | OH+GT integration wrapper |
| `benchmarks/smoke_30_split.json` | `general_start` | 30-task IDs + split |

## What the Next Session Needs to Do

1. Add `OPENROUTER_KEY` secret (Step 1)
2. Update `swebench_eval.yml` to use OpenRouter (Step 2)
3. Verify Decision 29 fixes are in the generalized code
4. Push updated workflow
5. Trigger the run (Step 3)
6. Wait for results (~2-3 hours for 20 tasks)
7. Compare gen results against pre-gen baseline (3/20)
8. If gen > 3: document wins. If gen <= 3: diagnose regressions.

## VM State (if needed)

- gt-v1 is RUNNING with:
  - Pre-gen code on `gen_lab` branch at `fcea7f9`
  - SWE-bench-Live harness at `/home/ubuntu/SWE-bench-Live/.venv/`
  - LiteLLM proxy on port 4000
  - All inference output at `/home/ubuntu/results/pregen_20/`
  - Official eval report at `/home/ubuntu/OpenHands-0.54.0/openai__qwen__qwen3-coder-480b-a35b-instruct-maas.pregen_baseline.json`
- gt-t0 is TERMINATED (zone capacity exhausted for n2d-standard-16)
- **STOP gt-v1 when done to save money:** `gcloud compute instances stop gt-v1 --zone=us-central1-a --project=GCP_OLD_PROJECT_PLACEHOLDER`
