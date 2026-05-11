# SWE-bench-Live on GitHub Actions + VM — Full Process Log

## What We Built (2026-05-11)

### 1. GitHub Actions Workflows

Two workflows pushed to `general_start` branch:

**`.github/workflows/swebench_eval.yml`** — Run 20-30 SWE-bench-Live tasks in parallel
- `workflow_dispatch` trigger with inputs: gt_commit, task_count, max_iterations, run_name
- Matrix strategy: splits tasks into jobs, up to 20 parallel runners
- Each job: starts LiteLLM proxy → pulls Docker image (GHCR first, Docker Hub fallback) → runs OH+GT wrapper → uploads results
- Final `evaluate` job: merges all output.jsonl, converts to predictions, runs official eval
- GCP auth via `secrets.GCP_SA_KEY` (service account JSON)
- GHCR auth via built-in `secrets.GITHUB_TOKEN`

**`.github/workflows/cache_swebench_images.yml`** — Pre-cache Docker images to GHCR
- Pulls `starryzhang/sweb.eval.x86_64.*` from Docker Hub
- Retags and pushes to `ghcr.io/harneet2512/sweb.eval.x86_64.*`
- 3 parallel jobs (batches of 10 images each)
- Uses `secrets.GITHUB_TOKEN` (has `packages:write` built-in for Actions)

### 2. GHCR Image Cache

**Status: ALL 30 IMAGES CACHED**

Cache workflow run `25687676692` completed successfully — all 3 batch jobs passed in ~14 min.
Images live at `ghcr.io/harneet2512/sweb.eval.x86_64.<org>_1776_<repo>-<id>:latest`.

The eval workflow tries GHCR first (same-network = fast), falls back to Docker Hub if miss.

### 3. VM-Based Inference Run (Phase 1)

**20/20 tasks completed on gt-v1** with pre-gen GT code (commit `fcea7f9`).

Run config:
- VM: gt-v1 (e2-standard-8, 8 vCPU, 32GB RAM)
- Model: Qwen3-Coder-480B via LiteLLM proxy → Vertex AI MaaS
- Agent: OpenHands 0.54.0 CodeActAgent
- Workers: started with 3, bumped to 4 mid-run
- All 20 tasks produced patches

Preliminary custom eval (NOT official — needs rerun): 13/20 RESOLVED, 7 TIMEOUT (300s too short).

---

## Known Issues & Fixes

### Issue 1: `workflow_dispatch` workflows don't show in Actions UI

**Problem:** Workflows on non-default branches don't appear in the Actions tab sidebar for manual triggering.

**Fix:** Trigger via CLI:
```bash
gh workflow run <workflow_id> --repo harneet2512/groundtruth --ref general_start -f <inputs>
```

Find workflow ID:
```bash
gh workflow list --repo harneet2512/groundtruth
```

### Issue 2: `matrix.*` can't be used in job-level `if`

**Problem:** GitHub Actions parser rejects `matrix.batch` in a job-level `if` when parsing `workflow_dispatch` events.

**Error:** `Unrecognized named-value: 'matrix'`

**Fix:** Move the filter from job-level `if` to step-level `if`:
```yaml
# BAD — job-level
jobs:
  myjob:
    if: ${{ inputs.batch == 'all' || inputs.batch == matrix.batch }}

# GOOD — step-level
    steps:
      - name: Do work
        if: ${{ inputs.batch == 'all' || inputs.batch == matrix.batch }}
```

### Issue 3: Two config.toml files in OH

**Problem:** OH reads LLM config from `./config.toml` (CWD) but the GT wrapper writes `selected_ids` to `evaluation/benchmarks/swe_bench/config.toml`. Using `--instance-ids` overwrites the swe_bench one, destroying task filters but NOT the LLM config.

**Fix:** Always write LLM config to the OpenHands ROOT `config.toml`:
```bash
cat > /home/ubuntu/OpenHands-0.54.0/config.toml << 'EOF'
[core]
max_iterations = 100
default_agent = "CodeActAgent"

[llm.eval]
model = "openai/qwen/qwen3-coder-480b-a35b-instruct-maas"
api_key = "sk-gt-local"
base_url = "http://localhost:4000/v1"
temperature = 0.7
top_p = 1.0
max_output_tokens = 8192
caching_prompt = false

[sandbox]
runtime_container_image = "ghcr.io/all-hands-ai/runtime:0.54-nikolaik"
EOF
```

### Issue 4: swebench 4.1.0 doesn't support SWE-bench-Live repos

**Problem:** `python -m swebench.harness.run_evaluation` with `--dataset_name SWE-bench-Live/SWE-bench-Live` fails with `KeyError: 'aiogram/aiogram'` because `MAP_REPO_VERSION_TO_SPECS` only has the original 66 SWE-bench repos.

**What we tried:**
1. `pip install swebench==4.1.0` — 66 repos, no Live support
2. `pip install git+https://github.com/SWE-bench/SWE-bench.git@main` — same 4.1.0, same 66 repos
3. `pip install git+https://github.com/microsoft/SWE-bench-Live.git` — installs as `swebench 1.0.0`, overwrites standard swebench, then `import swebench` fails (circular dep / broken package)
4. Separate venv with microsoft/SWE-bench-Live `--no-deps` on top of swebench 4.1.0 — Live overwrites swebench, same import error

**Root cause:** The dataset is **SWE-bench-Live by Microsoft** (`github.com/microsoft/SWE-bench-Live`), NOT the original SWE-bench by Princeton (`github.com/princeton-nlp/SWE-bench`). Princeton's package (PyPI `swebench`) only has 66 repos from the original benchmark. Microsoft's fork adds the Live repos (aiogram, pypsa, flexget, loguru, weasyprint, etc.).

**Correct install (in separate venv to avoid breaking OH):**
```bash
python3.12 -m venv /home/ubuntu/swebench_live_venv
source /home/ubuntu/swebench_live_venv/bin/activate
git clone https://github.com/microsoft/SWE-bench-Live.git /home/ubuntu/SWE-bench-Live
cd /home/ubuntu/SWE-bench-Live
pip install -e .
# Then manually install any missing deps the editable install skipped
```

**The packaging bug:** Microsoft's repo declares itself as `swebench==1.0.0` in setup.py BUT also lists `swebench` as a dependency. This creates a circular dependency that breaks `import swebench`. The `-e .` editable install may work because it adds the repo directory to sys.path directly.

**Status: NEEDS NEXT SESSION TO FIX.** Custom Docker eval showed 13/20 RESOLVED with 300s timeout. The 7 TIMEOUT tasks (pypsa, arviz, flexget) need 1800s. Rerun with official Microsoft harness once install is working.

### Issue 5: GHCR push needs `write:packages` scope

**Problem:** `gh auth token` default scopes (gist, read:org, repo, workflow) can't push to GHCR.

**Fix:**
```bash
gh auth refresh -h github.com -s gist,read:org,repo,workflow,write:packages
```
Must specify ALL scopes (not just the new one). Opens browser for OAuth.

**Better:** Use GitHub Actions for pushing — `GITHUB_TOKEN` has automatic `packages:write` when `permissions: packages: write` is set in the workflow.

### Issue 6: gt-t0 (n2d-standard-16) zone capacity exhausted

**Problem:** `gcloud compute instances start gt-t0` fails with `ZONE_RESOURCE_POOL_EXHAUSTED` for `n2d-standard-16` in `us-central1-a`.

**Workaround:** Use gt-v1 (e2-standard-8) only — 8 vCPU is enough for 3-4 workers. Or resize gt-t0 to e2-standard-8.

### Issue 7: `CLOUDSDK_CORE_PROJECT` env var override

**Problem:** An env var `CLOUDSDK_CORE_PROJECT=project-c9a6fdd8-8d56-4e88-ad6` (stale, non-existent project) overrides `gcloud config set project`.

**Fix:** Always pass `--project=GCP_OLD_PROJECT_PLACEHOLDER` explicitly on every gcloud command.

---

## Secrets Audit

| Secret | Where | Risk |
|--------|-------|------|
| `GCP_SA_KEY` | GitHub repo secret (for eval workflow) | Auto-masked in logs |
| `GITHUB_TOKEN` | Built-in, auto-rotated | Safe |
| `sk-gt-local` | In YAML (localhost proxy key) | Not a real credential — only works on localhost:4000 within runner |
| GCP Project ID | Env var with fallback default | Not a secret (identifier, not credential). Set `vars.GCP_PROJECT_ID` in repo settings |
| `.gitignore` | Updated to block `*sa_key*.json`, `*credentials*.json` | Prevents accidental commits |

---

## Setup for Next Session

### To run eval via GitHub Actions:

1. Add `GCP_SA_KEY` repo secret (see `docs/github_actions_setup.md`)
2. Go to Actions → "SWE-bench Eval" → Run workflow
3. Fill in: gt_commit, task_count, run_name
4. Results appear as artifacts

### To run eval on VM:

1. Start VM: `gcloud compute instances start gt-v1 --zone=us-central1-a --project=GCP_OLD_PROJECT_PLACEHOLDER`
2. Verify LiteLLM: `bash /home/ubuntu/start_litellm.sh` then health check
3. Write `config.toml` to OH root (see Issue 3 above)
4. Run wrapper: see `docs/swebench_30task_runbook.md`
5. Run eval: FIX THE SWEBENCH INSTALL FIRST (see Issue 4)
6. Stop VM when done

### To cache new Docker images:

Trigger from CLI:
```bash
gh workflow run cache_swebench_images --repo harneet2512/groundtruth --ref general_start -f batch=all
```
Or from Actions tab once workflows are on default branch.

---

## File Locations

| File | Purpose |
|------|---------|
| `.github/workflows/swebench_eval.yml` | Eval workflow |
| `.github/workflows/cache_swebench_images.yml` | Image cache workflow |
| `docs/swebench_30task_runbook.md` | VM run commands + gotchas |
| `docs/github_actions_setup.md` | One-time GCP SA setup |
| `docs/workflows-github.md` | This file — full process log |
| `benchmarks/smoke_30_split.json` | 30-task IDs + VM split |
| `/home/ubuntu/results/pregen_20/` (on gt-v1) | Phase 1 inference output |
