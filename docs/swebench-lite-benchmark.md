# SWE-bench Lite A/B Benchmark on GCP VM

This document describes how to run the GroundTruth SWE-bench Lite A/B benchmark on a GCP VM: **baseline (no MCP)** vs **with_groundtruth_mcp**, with the same model and task set. The MCP-enabled run is valid only if it produces hard proof of actual GroundTruth tool usage.

## GCP VM setup

1. Create a GCP VM (e.g. Ubuntu 22.04, `e2-standard-4`: 4 vCPU, 16 GB RAM, 100 GB disk).
2. SSH into the VM.
3. Run the bootstrap script once:

   ```bash
   git clone https://github.com/harneet2512/groundtruth.git ~/groundtruth
   cd ~/groundtruth
   # Add OPENAI_API_KEY to .env or export it
   bash scripts/swebench/vm_bootstrap.sh
   source ~/gt-venv/bin/activate
   ```

4. (Optional) Create a $10 budget alert:

   ```bash
   BILLING_ACCOUNT_ID=xxx GCP_PROJECT_ID=yyy bash scripts/swebench/gcp_budget_alert.sh
   ```

## Model and API key

- **Provider:** OpenAI.
- **Model:** Resolved at run time. Use `scripts/swebench/resolve_model.py` to get the exact model ID (e.g. `gpt-5o-mini`).
- **API key:** Set `OPENAI_API_KEY` in `.env` or export it. The runner and scripts load it from the environment.

```bash
# Resolve and export model ID
export MODEL_NAME_EXACT=$(python3 scripts/swebench/resolve_model.py --json | python3 -c "import sys,json; print(json.load(sys.stdin)['MODEL_NAME_EXACT'])")
# Smoke test model (including tool-calling)
python3 scripts/swebench/resolve_model.py --smoke-test
```

## Why Lite before Live

- SWE-bench **Lite** is a smaller, fixed set of tasks; **Live** is larger and more variable.
- We run Lite first to validate the pipeline, MCP proof, and comparison logic.
- Only after Lite is technically valid, MCP-valid, and stable should you consider running Live with the same setup.

## Controlled parallelism

- **Workers** are controlled by `--workers` (default 4). The runner uses a bounded `asyncio.Semaphore(workers)` so at most N tasks run concurrently.
- **Per-task isolation:** Each task gets its own cloned repo directory. For `groundtruth_mcp`, each task spawns its own MCP server process (no shared MCP server).
- **Fair comparison:** Use the same worker count for baseline and with_groundtruth_mcp.
- Start with 4 workers; increase to 6 or 8 only if MCP handshake and substantive tool usage remain stable. If MCP validity degrades, reduce workers.

## MCP isolation and proof

- For **with_groundtruth_mcp**, each task runs with its own GroundTruth MCP server: `groundtruth serve --root <cloned_repo> --db :memory:`.
- Proof artifacts are written under `results/<run>/groundtruth_mcp/proof/<instance_id>/`:
  - `mcp_usage.json` — connection, tools discovered, tools called, substantive count, valid flag.
  - `tool_calls.jsonl` — each tool call with success and latency.
  - `metadata.json` — worker/shard, model, command.
- **Validity rules:** A run is **valid** only if `connection_ok` is true and at least one **substantive** GroundTruth tool call succeeded (e.g. `groundtruth_find_relevant`, `groundtruth_brief`, `groundtruth_validate`, `groundtruth_trace`). Status-only usage does not count.

## Staged execution

1. **Preflight** (model + smoke run):
   ```bash
   bash scripts/swebench/run_preflight.sh
   ```
2. **Smoke** (1–2 tasks, 1 worker, both conditions):
   ```bash
   bash scripts/swebench/run_smoke.sh
   ```
   Proceed only if both conditions produce predictions and `validate_mcp_proof.py` passes for the MCP run.
3. **Stability** (10–20 tasks, 4 workers):
   ```bash
   bash scripts/swebench/run_stability.sh
   ```
   Optionally create `scripts/swebench/lite_task_ids.txt` with one instance ID per line (first 20 used).
4. **Full Lite** (all tasks, 4 workers, then evaluate and analyze):
   ```bash
   bash scripts/swebench/run_lite_full.sh
   ```

## Invalid MCP runs

- If the MCP run has zero successful substantive tool calls, it is **INVALID** and must not be counted in the comparison.
- Run `scripts/swebench/validate_mcp_proof.py <results_dir>/groundtruth_mcp` after each MCP run. Exit code 1 means invalid.
- Do not increase concurrency if it causes MCP validity to drop; reduce workers instead.

## Interpreting results

- **Predictions:** `benchmarks/swebench/results/<stage>/baseline/predictions.jsonl` and `.../groundtruth_mcp/predictions.jsonl`.
- **Cost:** `cost_report.json` in each condition directory.
- **Proof:** `groundtruth_mcp/proof/<instance_id>/mcp_usage.json` and `tool_calls.jsonl`.
- **Comparison:** After both Lite runs, use:
  ```bash
  python3 -m benchmarks.swebench.analyze \
    --baseline benchmarks/swebench/results/lite/baseline \
    --groundtruth benchmarks/swebench/results/lite/groundtruth_mcp \
    --output benchmarks/swebench/results/lite/analysis.json
  ```
  The report includes resolve rate, Wilson CI, and per-task gained/lost.

## VM cleanup

After results are saved or uploaded:

```bash
# Optional: upload to GCS
GCS_BUCKET=my-bucket bash scripts/swebench/vm_cleanup.sh stop

# Stop or delete VM (set GCP_INSTANCE_NAME and GCP_ZONE, or run gcloud from your machine)
bash scripts/swebench/vm_cleanup.sh stop
```

## Exact commands reference

- **Runner:** `python3 -m benchmarks.swebench.runner --mode baseline|groundtruth|groundtruth_mcp --model $MODEL_NAME_EXACT --workers N [--instance-ids id1 id2 ...] --output-dir <dir>`
- **Analyze:** `python3 -m benchmarks.swebench.analyze --baseline <dir> --groundtruth <dir> --output <path>`
- **Validate proof:** `python3 scripts/swebench/validate_mcp_proof.py <groundtruth_mcp_results_dir>`
- **Resolve model:** `python3 scripts/swebench/resolve_model.py [--smoke-test] [--json]`
