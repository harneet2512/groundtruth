# GitHub Actions SWE-bench Setup (One-Time)

## 1. Create a GCP Service Account Key

```bash
# Create SA
gcloud iam service-accounts create swebench-runner \
  --display-name="SWE-bench GitHub Actions" \
  --project=GCP_OLD_PROJECT_PLACEHOLDER

# Grant Vertex AI access
gcloud projects add-iam-policy-binding GCP_OLD_PROJECT_PLACEHOLDER \
  --member="serviceAccount:swebench-runner@GCP_OLD_PROJECT_PLACEHOLDER.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# Create key
gcloud iam service-accounts keys create /tmp/gcp_sa_key.json \
  --iam-account=swebench-runner@GCP_OLD_PROJECT_PLACEHOLDER.iam.gserviceaccount.com
```

## 2. Add GitHub Secret

1. Go to repo Settings > Secrets and variables > Actions
2. New repository secret: `GCP_SA_KEY`
3. Paste contents of `/tmp/gcp_sa_key.json`

## 3. Run the Workflow

1. Go to Actions tab > "SWE-bench Eval (30 tasks parallel)"
2. Click "Run workflow"
3. Fill in:
   - **gt_commit**: The GT commit to evaluate (e.g., `fcea7f9`)
   - **task_count**: 20 or 30
   - **max_iterations**: 100
   - **run_name**: e.g., `pregen_baseline`
4. Click "Run workflow"

## 4. What Happens

- **prepare** job: Builds a matrix of task batches
- **eval** jobs (up to 20 parallel): Each pulls Docker images, starts LiteLLM proxy, runs OH+GT wrapper
- **evaluate** job: Merges all output.jsonl files, runs official swebench evaluation

## 5. Cost

- GitHub Actions: $0 (public repo, unlimited minutes)
- Vertex AI (Qwen3-Coder-480B): ~$0.12/task = ~$3.60 for 30 tasks
- Total: ~$3.60 (no VM cost!)

## 6. Limitations

- GitHub Actions runners have 4 vCPU / 16GB RAM (smaller than gt-v1's 8/32)
- Docker image pulls add ~3-5 min per job
- 6-hour max job timeout (enough for 100 iterations)
- Need to check if SWE-bench Docker images are pullable from GitHub's network
